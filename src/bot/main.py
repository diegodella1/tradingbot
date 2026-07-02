from __future__ import annotations

import asyncio
import fcntl
import logging
import signal
from contextlib import suppress
from datetime import datetime
from pathlib import Path

import structlog

from bot.btc.price_feed import CoinbaseBtcFeed
from bot.config import Settings
from bot.execution.order_manager import OrderManager
from bot.execution.paper_broker import PaperBroker
from bot.execution.risk_manager import RiskManager
from bot.polymarket.clob import ClobClient
from bot.polymarket.gamma import GammaClient, rejection_reason
from bot.polymarket.models import MarketContext, OutcomeSide
from bot.storage.db import connect, init_db
from bot.storage.repositories import Repository
from bot.strategy.momentum_book_imbalance import MomentumBookImbalanceStrategy
from bot.strategy.no_trade import NoTradeStrategy


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[structlog.processors.TimeStamper(fmt="iso"), structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )


async def run_paper_once(settings: Settings) -> None:
    log = structlog.get_logger()
    gamma = GammaClient(settings)
    clob = ClobClient(settings)
    try:
        discovered = await gamma.discover_btc_updown()
        strategy = NoTradeStrategy()
        risk = RiskManager(settings)
        broker = PaperBroker(settings)
        manager = OrderManager(risk, broker)
        for market_type, markets in discovered.items():
            if not markets:
                log.info("no_market", market_type=market_type.value)
                continue
            market = markets[0]
            up_book = await clob.get_order_book(market.tokens[OutcomeSide.UP].token_id, market.market_id)
            down_book = await clob.get_order_book(market.tokens[OutcomeSide.DOWN].token_id, market.market_id)
            context = MarketContext(market=market, up_book=up_book, down_book=down_book)
            signal = strategy.evaluate(context)
            order, decision = manager.execute_paper_signal(signal, context)
            log.info("paper_cycle", market=market.question, signal=signal.model_dump(), risk=decision.reason, order=order.model_dump() if order else None)
    finally:
        await gamma.close()
        await clob.close()


async def run_paper_loop(settings: Settings, max_cycles: int | None = None) -> None:
    log = structlog.get_logger()
    lock_file = _acquire_paper_loop_lock(settings.sqlite_path)
    init_db(settings.sqlite_path)
    gamma = GammaClient(settings)
    clob = ClobClient(settings)
    btc_feed = CoinbaseBtcFeed(settings)
    broker = PaperBroker(settings)
    risk = RiskManager(settings)
    strategy = MomentumBookImbalanceStrategy(settings) if settings.enable_experimental_strategy else NoTradeStrategy()
    stop = asyncio.Event()

    def _stop() -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, _stop)

    cycles = 0
    with connect(settings.sqlite_path) as conn:
        repo = Repository(conn)
        risk.state = repo.hydrate_risk_state()
        try:
            while not stop.is_set():
                cycles += 1
                await _paper_cycle(settings, gamma, clob, btc_feed, broker, risk, strategy, repo, log)
                if max_cycles is not None and cycles >= max_cycles:
                    break
                await asyncio.sleep(settings.paper_loop_interval_seconds)
        finally:
            repo.save_health_event("paper_loop", "stopped", f"cycles={cycles}")
            with suppress(Exception):
                await asyncio.wait_for(gamma.close(), timeout=2)
            with suppress(Exception):
                await asyncio.wait_for(clob.close(), timeout=2)
            with suppress(Exception):
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()


def _acquire_paper_loop_lock(sqlite_path: Path):
    lock_path = sqlite_path.with_suffix(sqlite_path.suffix + ".paper.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        raise RuntimeError(f"paper loop already running; lock held at {lock_path}") from None
    lock_file.write(str(datetime.now().isoformat()))
    lock_file.flush()
    return lock_file


async def _paper_cycle(settings: Settings, gamma, clob, btc_feed, broker, risk, strategy, repo: Repository, log) -> None:
    _sync_risk_state(settings, repo, risk)
    try:
        btc_state = await btc_feed.poll_once()
        if btc_state.current_price is not None:
            repo.save_btc_tick(btc_state.current_price)
    except Exception as exc:
        repo.save_health_event("btc_feed", "blocked", str(exc))
        repo.set_state("paper_loop", {"status": "blocked", "reason": f"btc_feed: {exc}"})
        log.warning("paper_btc_feed_failed", error=str(exc))
        return

    try:
        discovered = await gamma.discover_btc_updown()
        with suppress(Exception):
            for item in getattr(gamma, "last_raw_markets", [])[-200:]:
                rejected = rejection_reason(item, settings.market_types)
                if rejected:
                    market_type, reason = rejected
                    repo.save_discovery_rejection(market_type, str(item.get("question") or item.get("title") or ""), str(item.get("slug") or ""), reason)
    except Exception as exc:
        repo.save_health_event("market_discovery", "blocked", str(exc))
        repo.set_state("paper_loop", {"status": "blocked", "reason": f"market_discovery: {exc}"})
        log.warning("paper_discovery_failed", error=str(exc))
        return

    any_market = False
    state_markets = []
    for market_type, markets in discovered.items():
        market = markets[0] if markets else None
        if market is None:
            repo.save_health_event("market_discovery", "no_market", market_type.value)
            state_markets.append({"type": market_type.value, "status": "no_market"})
            continue

        any_market = True
        repo.save_market(market)
        try:
            up_book = await clob.get_order_book(market.tokens[OutcomeSide.UP].token_id, market.market_id)
            down_book = await clob.get_order_book(market.tokens[OutcomeSide.DOWN].token_id, market.market_id)
            repo.save_snapshot(up_book)
            repo.save_snapshot(down_book)
        except Exception as exc:
            repo.save_health_event("market_feed", "blocked", f"{market.market_id}: {exc}")
            state_markets.append({"type": market_type.value, "status": "book_error", "question": market.question})
            continue

        market_open_price = _market_open_price(repo, market.start_time) or btc_state.market_open_price or btc_state.current_price
        market_btc_state = btc_state.model_copy(update={"market_open_price": market_open_price})
        context = MarketContext(market=market, up_book=up_book, down_book=down_book, btc=market_btc_state)
        signal = strategy.evaluate(context)
        repo.save_signal(market.market_id, signal)
        repo.save_strategy_decision(market, signal)
        order, decision = OrderManager(risk, broker).execute_paper_signal(signal, context)
        repo.save_risk_event(market.market_id, decision)
        if order:
            repo.save_order(order)
            for fill in broker.fills:
                if fill.order_id == order.order_id:
                    repo.save_fill(fill)
        repo.save_learning_note(
            f"{market_type.value} {signal.action.value}: {decision.reason}; signal={signal.reason}",
            "paper,risk,market-real",
        )
        state_markets.append(
            {
                "type": market_type.value,
                "status": "ok",
                "market_id": market.market_id,
                "question": market.question,
                "slug": market.slug,
                "liquidity": market.liquidity,
                "volume": market.volume,
                "seconds_to_close": market.seconds_to_close,
                "up_bid": up_book.best_bid,
                "up_ask": up_book.best_ask,
                "down_bid": down_book.best_bid,
                "down_ask": down_book.best_ask,
                "signal": signal.model_dump(mode="json"),
                "risk": decision.reason,
                "order": order.model_dump(mode="json") if order else None,
            }
        )
        log.info("paper_real_market_cycle", market=market.question, signal=signal.model_dump(), risk=decision.reason)

    repo.save_health_event("paper_loop", "ok" if any_market else "no_market", "real-market paper cycle")
    repo.set_state(
        "paper_loop",
        {
            "status": "ok" if any_market else "no_market",
            "mode": "paper",
            "strategy": strategy.__class__.__name__,
            "btc": btc_state.model_dump(mode="json"),
            "markets": state_markets,
        },
    )


def _sync_risk_state(settings: Settings, repo: Repository, risk: RiskManager) -> None:
    previous_exposure = sum(risk.state.market_exposure.values())
    init_db(settings.sqlite_path)
    risk.state = repo.hydrate_risk_state()
    current_exposure = sum(risk.state.market_exposure.values())
    if previous_exposure > 0 and current_exposure == 0:
        repo.save_health_event("risk_state", "synced", f"cleared stale paper exposure {previous_exposure:.2f} USDC")


def _market_open_price(repo: Repository, start_time: datetime | None) -> float | None:
    if start_time is None:
        return None
    row = repo.conn.execute(
        "SELECT price FROM btc_ticks WHERE created_at >= ? ORDER BY created_at ASC LIMIT 1",
        (start_time.isoformat(),),
    ).fetchone()
    return float(row["price"]) if row else None
