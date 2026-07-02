from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress
from pathlib import Path

import structlog

from bot.btc.price_feed import CoinbaseBtcFeed
from bot.config import Settings
from bot.execution.order_manager import OrderManager
from bot.execution.paper_broker import PaperBroker
from bot.execution.risk_manager import RiskManager
from bot.polymarket.clob import ClobClient
from bot.polymarket.gamma import GammaClient, rejection_reason
from bot.polymarket.models import MarketContext, OutcomeSide, SignalAction
from bot.polymarket.realtime import RealtimeMarketData
from bot.storage.db import connect, init_db, refresh_settlements
from bot.storage.repositories import Repository
from bot.strategy.momentum_book_imbalance import MomentumBookImbalanceStrategy
from bot.strategy.no_trade import NoTradeStrategy
from bot.util.filelock import FileLock, LockAlreadyHeld


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
    lock = _acquire_paper_loop_lock(settings.sqlite_path)
    init_db(settings.sqlite_path)
    gamma = GammaClient(settings)
    clob = ClobClient(settings)
    btc_feed = CoinbaseBtcFeed(settings)
    broker = PaperBroker(settings)
    risk = RiskManager(settings)
    strategy = MomentumBookImbalanceStrategy(settings) if settings.enable_experimental_strategy else NoTradeStrategy()
    realtime = RealtimeMarketData(settings, btc_feed) if settings.enable_websocket_feeds else None
    stop = asyncio.Event()

    def _stop() -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, _stop)

    cycles = 0
    if realtime is not None:
        with suppress(Exception):
            await realtime.start()
    with connect(settings.sqlite_path) as conn:
        repo = Repository(conn)
        risk.state = repo.hydrate_risk_state()
        try:
            while not stop.is_set():
                cycles += 1
                await _paper_cycle(settings, gamma, clob, btc_feed, broker, risk, strategy, repo, log, realtime)
                if max_cycles is not None and cycles >= max_cycles:
                    break
                await asyncio.sleep(settings.paper_loop_interval_seconds)
        finally:
            repo.save_health_event("paper_loop", "stopped", f"cycles={cycles}")
            if realtime is not None:
                with suppress(Exception):
                    await realtime.stop()
            with suppress(Exception):
                await asyncio.wait_for(gamma.close(), timeout=2)
            with suppress(Exception):
                await asyncio.wait_for(clob.close(), timeout=2)
            with suppress(Exception):
                lock.release()


def _acquire_paper_loop_lock(sqlite_path: Path) -> FileLock:
    lock_path = sqlite_path.with_suffix(sqlite_path.suffix + ".paper.lock")
    try:
        return FileLock(lock_path).acquire()
    except LockAlreadyHeld:
        raise RuntimeError(f"paper loop already running; lock held at {lock_path}") from None


async def _paper_cycle(settings: Settings, gamma, clob, btc_feed, broker, risk, strategy, repo: Repository, log, realtime: RealtimeMarketData | None = None) -> None:
    _sync_risk_state(settings, repo, risk)
    try:
        if realtime is not None and btc_feed.current_price is not None:
            btc_state = btc_feed.state  # streamed price
        else:
            btc_state = await btc_feed.poll_once()  # REST fallback (or until WS warms up)
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

    await _apply_realtime_state(realtime, discovered, risk)

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
            up_token = market.tokens[OutcomeSide.UP].token_id
            down_token = market.tokens[OutcomeSide.DOWN].token_id
            up_book = (realtime.get_book(up_token) if realtime else None) or await clob.get_order_book(up_token, market.market_id)
            down_book = (realtime.get_book(down_token) if realtime else None) or await clob.get_order_book(down_token, market.market_id)
            repo.save_snapshot(up_book)
            repo.save_snapshot(down_book)
        except Exception as exc:
            repo.save_health_event("market_feed", "blocked", f"{market.market_id}: {exc}")
            state_markets.append({"type": market_type.value, "status": "book_error", "question": market.question})
            continue

        market_open_price = _market_open_price(repo, market) or btc_state.market_open_price or btc_state.current_price
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
        _maybe_exit_position(settings, market, context, strategy, broker, risk, repo, log)
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


async def _apply_realtime_state(realtime: RealtimeMarketData | None, discovered, risk: RiskManager) -> None:
    """Refresh CLOB websocket subscriptions and set the websocket_connected risk gate."""
    if realtime is None:
        risk.state.websocket_connected = True  # REST mode does not gate on websockets
        return
    tokens: list[str] = []
    for markets in discovered.values():
        if markets:
            market = markets[0]
            tokens.append(market.tokens[OutcomeSide.UP].token_id)
            tokens.append(market.tokens[OutcomeSide.DOWN].token_id)
    with suppress(Exception):
        await realtime.ensure_subscription(tokens)
    risk.state.websocket_connected = realtime.connected


def _maybe_exit_position(settings: Settings, market, context, strategy, broker, risk, repo: Repository, log) -> None:
    """Evaluate and execute an early EXIT for an open position (no-op unless enabled)."""
    if settings.hold_to_resolution or not settings.enable_exit_signals:
        return
    if not hasattr(strategy, "evaluate_exit"):
        return
    position = repo.get_open_position(market.market_id)
    if not position:
        return
    held_side = next((side for side, token in market.tokens.items() if token.token_id == position["token_id"]), None)
    if held_side is None:
        return
    exit_signal = strategy.evaluate_exit(context, held_side, float(position["avg_price"] or 0))
    if exit_signal.action != SignalAction.EXIT:
        return
    position_ctx = {
        "side": held_side.value,
        "token_id": position["token_id"],
        "shares": position["shares"],
        "cost_usdc": position["size_usdc"],
        "fee_usdc": position["fee_usdc"],
    }
    order, realized = OrderManager(risk, broker).execute_exit_signal(exit_signal, context, position_ctx)
    if order is None or order.filled_size_usdc <= 0:
        return
    repo.save_order(order)
    for fill in broker.fills:
        if fill.order_id == order.order_id:
            repo.save_exit_fill(fill, realized)
    repo.close_position(int(position["id"]), realized)
    log.info("paper_exit", market=market.question, realized_usdc=realized, reason=exit_signal.reason)


def _sync_risk_state(settings: Settings, repo: Repository, risk: RiskManager) -> None:
    previous_exposure = sum(risk.state.market_exposure.values())
    refresh_settlements(repo.conn)
    risk.state = repo.hydrate_risk_state()
    current_exposure = sum(risk.state.market_exposure.values())
    if previous_exposure > 0 and current_exposure == 0:
        repo.save_health_event("risk_state", "synced", f"cleared stale paper exposure {previous_exposure:.2f} USDC")


def _market_open_price(repo: Repository, market) -> float | None:
    """Resolve a stable per-market BTC open price.

    Once recorded it never drifts. If the bot started after the market opened (no
    tick at/after start), it falls back to the tick nearest the start time instead
    of the live price, keeping change_since_open meaningful.
    """
    if market.start_time is None:
        return None
    persisted = repo.get_market_open_price(market.market_id)
    if persisted is not None:
        return persisted
    start_iso = market.start_time.isoformat()
    row = repo.conn.execute(
        "SELECT price FROM btc_ticks WHERE created_at >= ? ORDER BY created_at ASC LIMIT 1",
        (start_iso,),
    ).fetchone()
    price = float(row["price"]) if row else repo.nearest_btc_tick(start_iso)
    if price is not None:
        repo.save_market_open_price(market.market_id, price, source="tick")
    return price
