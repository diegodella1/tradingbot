from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import structlog

from bot.btc.price_feed import CoinbaseBtcFeed
from bot.config import Settings
from bot.execution.order_manager import OrderManager
from bot.execution.paper_broker import PaperBroker
from bot.execution.risk_manager import RiskManager
from bot.monitoring.alerts import send_alert
from bot.monitoring.regime import regime_snapshot
from bot.polymarket.clob import ClobClient
from bot.polymarket.gamma import GammaClient, rejection_reason
from bot.polymarket.models import BtcMarketState, MarketContext, OutcomeSide, SignalAction
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
        risk.state = repo.hydrate_risk_state(settings.loss_streak_window_minutes)
        try:
            while not stop.is_set():
                cycles += 1
                await _paper_cycle(settings, gamma, clob, btc_feed, broker, risk, strategy, repo, log, realtime)
                if settings.outcome_backfill_cycles > 0 and cycles % settings.outcome_backfill_cycles == 0:
                    await _run_outcome_backfill(gamma, repo, log)
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


async def _run_outcome_backfill(gamma, repo: Repository, log) -> None:
    """Refetch closed markets without a verified winner so settlements can complete."""
    from bot.polymarket.backfill import backfill_outcomes

    try:
        result = await backfill_outcomes(gamma, repo.conn)
        repo.save_health_event(
            "outcome_backfill",
            "ok",
            f"pending={result['pending']} refreshed={result['refreshed']} verified={result['verified']}",
        )
        if result["refreshed"]:
            log.info("outcome_backfill", **{k: v for k, v in result.items() if k != "errors"})
    except Exception as exc:
        repo.save_health_event("outcome_backfill", "blocked", str(exc))
        log.warning("outcome_backfill_failed", error=str(exc))


def _acquire_paper_loop_lock(sqlite_path: Path) -> FileLock:
    lock_path = sqlite_path.with_suffix(sqlite_path.suffix + ".paper.lock")
    try:
        return FileLock(lock_path).acquire()
    except LockAlreadyHeld:
        raise RuntimeError(f"paper loop already running; lock held at {lock_path}") from None


async def _paper_cycle(settings: Settings, gamma, clob, btc_feed, broker, risk, strategy, repo: Repository, log, realtime: RealtimeMarketData | None = None) -> None:
    _sync_risk_state(settings, repo, risk)
    try:
        btc_state, btc_source = await _btc_state_for_cycle(settings, btc_feed, realtime)
        _track_feed_degradation(settings, repo, realtime, using_rest=btc_source == "rest")
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
            streamed_up = realtime.get_book(up_token) if realtime else None
            streamed_down = realtime.get_book(down_token) if realtime else None
            up_book = streamed_up or await clob.get_order_book(up_token, market.market_id)
            down_book = streamed_down or await clob.get_order_book(down_token, market.market_id)
            book_source = "websocket" if streamed_up and streamed_down else ("mixed" if streamed_up or streamed_down else "rest")
            repo.save_snapshot(up_book)
            repo.save_snapshot(down_book)
        except Exception as exc:
            repo.save_health_event("market_feed", "blocked", f"{market.market_id}: {exc}")
            state_markets.append({"type": market_type.value, "status": "book_error", "question": market.question})
            continue

        market_open_price = (
            _market_open_price(repo, market, chainlink=realtime.chainlink if realtime else None)
            or btc_state.market_open_price
            or btc_state.current_price
        )
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
                "book_source": book_source,
                "book_fresh": True,
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
            "btc": btc_state.model_dump(mode="json")
            | {
                "source": btc_source,
                "age_seconds": _btc_age_seconds(btc_state),
                "websocket_connected": bool(realtime and realtime.btc_connected),
            },
            "markets": state_markets,
        },
    )


async def _btc_state_for_cycle(
    settings: Settings,
    btc_feed: CoinbaseBtcFeed,
    realtime: RealtimeMarketData | None,
) -> tuple[BtcMarketState, str]:
    """Use streamed BTC only while fresh; otherwise refresh through REST.

    A disconnected Coinbase socket can retain its last in-memory price forever.
    Checking only ``current_price`` therefore turns a recoverable disconnect into
    permanent HOLDs. Freshness, not mere presence, selects the data source.
    """
    streamed = btc_feed.state
    max_age = 3.0 if settings.enable_5m_scout and "5m" in settings.market_types else 5.0
    if realtime is not None and streamed.current_price is not None and streamed.is_fresh(max_age):
        return streamed, "websocket"
    return await btc_feed.poll_once(), "rest"

def _btc_age_seconds(state: BtcMarketState) -> float | None:
    if state.price_timestamp is None:
        return None
    return max(0.0, (datetime.now(UTC) - state.price_timestamp).total_seconds())


def _track_feed_degradation(settings: Settings, repo: Repository, realtime: RealtimeMarketData | None, using_rest: bool) -> None:
    """Alert once when the BTC feed silently falls back from WebSocket to REST polling.

    REST polling degrades momentum signals (one tick per cycle instead of a
    stream). Warm-up is not degradation: nothing fires until the WS streamed
    at least once.
    """
    if realtime is None:
        return
    if not using_rest:
        realtime.ever_streamed = True
        if realtime.rest_fallback_active:
            realtime.rest_fallback_active = False
            repo.save_health_event("btc_feed", "recovered", "websocket stream restored")
            send_alert(settings, "btc_feed_recovered", source="websocket")
        return
    if realtime.ever_streamed and not realtime.rest_fallback_active:
        realtime.rest_fallback_active = True
        repo.save_health_event("btc_feed", "degraded", "websocket down; polling REST fallback")
        send_alert(settings, "btc_feed_degraded", fallback="rest", websocket_connected=realtime.connected)


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
    # Paper can safely use the REST books fetched by `_paper_cycle`; live remains
    # fail-closed unless the market websocket itself is connected.
    risk.state.websocket_connected = realtime.market_connected or not risk.settings.enable_live_trading


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
    previous_regime_healthy = risk.state.regime_healthy
    refresh_settlements(repo.conn, retention_days=settings.data_retention_days)
    risk.state = repo.hydrate_risk_state(settings.loss_streak_window_minutes)
    current_exposure = sum(risk.state.market_exposure.values())
    if previous_exposure > 0 and current_exposure == 0:
        repo.save_health_event("risk_state", "synced", f"cleared stale paper exposure {previous_exposure:.2f} USDC")
    _apply_regime_control(settings, repo, risk, previous_regime_healthy)


def _apply_regime_control(settings: Settings, repo: Repository, risk: RiskManager, previously_healthy: bool) -> None:
    """Compare rolling WR vs breakeven; alert on degradation, optionally stop entries."""
    snapshot = regime_snapshot(repo.conn, settings.regime_window_trades, settings.regime_min_trades)
    risk.state.regime_healthy = bool(snapshot["healthy"])
    risk.state.regime_blocked = settings.enable_regime_stop and not snapshot["healthy"]
    if not snapshot["evaluated"]:
        return
    detail = (
        f"wr={snapshot['win_rate']:.3f} breakeven={snapshot['breakeven_win_rate']:.3f} "
        f"trades={snapshot['trades']} pnl={snapshot['rolling_pnl_usdc']:+.2f}"
    )
    if previously_healthy and not snapshot["healthy"]:
        repo.save_health_event("regime", "degraded", detail)
        send_alert(
            settings,
            "regime_degraded",
            win_rate=round(snapshot["win_rate"], 3),
            breakeven=round(snapshot["breakeven_win_rate"], 3),
            trades=snapshot["trades"],
            rolling_pnl_usdc=round(snapshot["rolling_pnl_usdc"], 2),
            stop_enabled=settings.enable_regime_stop,
        )
    elif not previously_healthy and snapshot["healthy"]:
        repo.save_health_event("regime", "recovered", detail)


def _market_open_price(repo: Repository, market, chainlink=None) -> float | None:
    """Resolve a stable per-market BTC open price.

    Preference order:
    1. Persisted value (never drifts once recorded).
    2. Chainlink oracle boundary tick: BTC up/down markets resolve against the
       Chainlink BTC/USD stream, whose first tick at/after the window start IS
       the official "Price to Beat". Persisted with source="chainlink".
    3. Coinbase tick proxy (source="tick"): first tick at/after start, else the
       nearest tick, keeping change_since_open meaningful on late starts.

    While the Chainlink feed is fresh but has not yet delivered the boundary
    tick of a just-opened window, returns None instead of locking in the proxy.
    """
    if market.start_time is None:
        return None
    persisted = repo.get_market_open_price(market.market_id)
    if persisted is not None:
        return persisted
    if chainlink is not None and chainlink.is_fresh():
        price = chainlink.first_tick_at_or_after(market.start_time)
        if price is not None:
            repo.save_market_open_price(market.market_id, price, source="chainlink")
            return price
        if (datetime.now(UTC) - market.start_time).total_seconds() < 90:
            return None  # wait for the oracle tick before persisting the proxy
    start_iso = market.start_time.isoformat()
    row = repo.conn.execute(
        "SELECT price FROM btc_ticks WHERE created_at >= ? ORDER BY created_at ASC LIMIT 1",
        (start_iso,),
    ).fetchone()
    price = float(row["price"]) if row else repo.nearest_btc_tick(start_iso)
    if price is not None:
        repo.save_market_open_price(market.market_id, price, source="tick")
    return price
