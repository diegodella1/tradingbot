from __future__ import annotations

import asyncio
import signal as signal_module
from contextlib import suppress

import structlog

from bot.config import Settings
from bot.btc.price_feed import CoinbaseBtcFeed, exception_detail
from bot.execution.live_broker import LiveBroker
from bot.execution.live_tracker import LiveOrderTracker
from bot.execution.risk_manager import RiskManager
from bot.monitoring.alerts import send_alert
from bot.polymarket.clob import ClobClient
from bot.polymarket.gamma import GammaClient
from bot.polymarket.geoblock import GeoblockClient
from bot.polymarket.models import (
    FillRecord,
    MarketContext,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OutcomeSide,
    SignalAction,
)
from bot.polymarket.realtime import RealtimeMarketData
from bot.storage.db import connect, init_db
from bot.storage.repositories import Repository
from bot.strategy.momentum_book_imbalance import MomentumBookImbalanceStrategy
from bot.strategy.no_trade import NoTradeStrategy
from bot.util.filelock import FileLock, LockAlreadyHeld

# Reuse the paper-loop helpers to keep a single source of truth for shared logic.
from bot.main import _apply_realtime_state, _btc_state_for_cycle, _market_open_price, _sync_risk_state, _track_feed_degradation


async def _preflight(settings: Settings) -> None:
    if not settings.enable_live_trading:
        raise RuntimeError("ENABLE_LIVE_TRADING=false; live loop blocked")
    if not settings.live_auth_ready:
        raise RuntimeError("live credentials incomplete; live loop blocked")
    geoblock = await GeoblockClient(settings).check()
    if geoblock.blocked:
        raise RuntimeError(f"geoblock blocks live trading: {geoblock.reason}")


def _acquire_live_lock(settings: Settings) -> FileLock:
    lock_path = settings.sqlite_path.with_suffix(settings.sqlite_path.suffix + ".live.lock")
    try:
        return FileLock(lock_path).acquire()
    except LockAlreadyHeld:
        raise RuntimeError(f"live loop already running; lock held at {lock_path}") from None


async def run_live_loop(settings: Settings, max_cycles: int | None = None) -> None:
    log = structlog.get_logger()
    await _preflight(settings)
    lock = _acquire_live_lock(settings)
    init_db(settings.sqlite_path)

    gamma = GammaClient(settings)
    clob = ClobClient(settings)
    btc_feed = CoinbaseBtcFeed(settings)
    risk = RiskManager(settings)
    broker = LiveBroker(settings, risk)
    tracker = LiveOrderTracker()
    observer_only = settings.policy_mode == "observe"
    strategy = (
        MomentumBookImbalanceStrategy(settings, observer_only=observer_only)
        if settings.enable_experimental_strategy or observer_only
        else NoTradeStrategy()
    )
    realtime = RealtimeMarketData(settings, btc_feed) if settings.enable_websocket_feeds else None
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal_module.SIGINT, signal_module.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    send_alert(settings, "live_loop_started", mode="live", strategy=strategy.__class__.__name__)
    if realtime is not None:
        with suppress(Exception):
            await realtime.start()

    cycles = 0
    with connect(settings.sqlite_path) as conn:
        repo = Repository(conn, raw_sample_seconds=settings.telemetry_sample_seconds)
        risk.state = repo.hydrate_risk_state(settings.loss_streak_window_minutes)
        try:
            while not stop.is_set():
                cycles += 1
                await _live_cycle(settings, gamma, clob, btc_feed, broker, tracker, risk, strategy, repo, log, realtime)
                if max_cycles is not None and cycles >= max_cycles:
                    break
                await asyncio.sleep(settings.live_loop_interval_seconds)
        finally:
            await _cancel_all_open(settings, broker, tracker, repo, log)
            repo.save_health_event("live_loop", "stopped", f"cycles={cycles}")
            send_alert(settings, "live_loop_stopped", cycles=cycles)
            if realtime is not None:
                with suppress(Exception):
                    await realtime.stop()
            else:
                with suppress(Exception):
                    await btc_feed.stop()
            with suppress(Exception):
                await asyncio.wait_for(gamma.close(), timeout=2)
            with suppress(Exception):
                await asyncio.wait_for(clob.close(), timeout=2)
            with suppress(Exception):
                lock.release()


async def _live_cycle(settings, gamma, clob, btc_feed, broker, tracker, risk, strategy, repo, log, realtime) -> None:
    _sync_risk_state(settings, repo, risk)
    try:
        btc_state, btc_source = await _btc_state_for_cycle(settings, btc_feed, realtime)
        _track_feed_degradation(settings, repo, realtime, using_rest=btc_source == "rest")
        if btc_state.current_price is not None:
            repo.save_btc_tick(btc_state.current_price)
    except Exception as exc:
        detail = exception_detail(exc)
        repo.save_health_event("btc_feed", "blocked", detail)
        log.warning("live_btc_feed_failed", error=detail)
        return

    try:
        discovered = await gamma.discover_btc_updown()
    except Exception as exc:
        repo.save_health_event("market_discovery", "blocked", str(exc))
        log.warning("live_discovery_failed", error=str(exc))
        return

    await _apply_realtime_state(realtime, discovered, risk)
    await _reconcile_orders(settings, broker, tracker, repo, log)

    any_market = False
    for market_type, markets in discovered.items():
        market = markets[0] if markets else None
        if market is None:
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
        if signal.action not in (SignalAction.BUY_UP, SignalAction.BUY_DOWN):
            continue
        await _place_live_order(settings, broker, tracker, risk, repo, context, signal, log)

    repo.save_health_event("live_loop", "ok" if any_market else "no_market", "live cycle")
    repo.set_state("live_loop", {"status": "ok" if any_market else "no_market", "mode": "live", "strategy": strategy.__class__.__name__, "open_orders": tracker.open_order_ids()})


async def _place_live_order(settings, broker, tracker, risk, repo, context, signal, log) -> None:
    side = OutcomeSide.UP if signal.action == SignalAction.BUY_UP else OutcomeSide.DOWN
    token = context.market.tokens.get(side)
    if token is None:
        return
    price = _live_order_price(settings, context, side, signal)
    if price is None:
        return
    request = OrderRequest(
        market_id=context.market.market_id,
        token_id=token.token_id,
        side=OrderSide.BUY,
        price=price,
        size_usdc=signal.size_usdc,
        reason=signal.reason,
    )
    order = await broker.place_limit_order(request, context, signal)
    repo.save_order(order)
    if order.status == OrderStatus.REJECTED:
        return
    tracker.record(order)
    if order.status in {OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED}:
        _record_live_fill(repo, risk, order)
    send_alert(settings, "live_order_placed", market=context.market.question, action=signal.action.value, price=request.price, size=request.size_usdc, status=order.status.value)


def _live_order_price(settings, context, side: OutcomeSide, signal) -> float | None:
    """Taker crosses at the signal's max price; maker joins the best bid.

    Maker orders pay zero fee and earn the 20% rebate on Polymarket crypto
    markets; the existing stale-order cancellation handles unfilled rests.
    """
    if settings.live_order_style != "maker":
        return signal.max_price
    book = context.up_book if side == OutcomeSide.UP else context.down_book
    if book is None or book.best_bid is None:
        return None
    return min(book.best_bid, signal.max_price)


def _record_live_fill(repo: Repository, risk: RiskManager, order) -> None:
    filled = order.filled_size_usdc or order.request.size_usdc
    price = order.avg_fill_price or order.request.price
    repo.save_fill(
        FillRecord(
            order_id=order.order_id,
            market_id=order.request.market_id,
            token_id=order.request.token_id,
            side=order.request.side,
            price=price,
            size_usdc=filled,
        )
    )
    risk.record_trade(order.request.market_id, filled, order.request.token_id)


async def _reconcile_orders(settings, broker, tracker, repo, log) -> None:
    for order_id in list(tracker.open_order_ids()):
        status = await broker.get_order_status(order_id)
        if status is None:
            continue
        tracker.mark(order_id, status)
        with suppress(Exception):
            repo.conn.execute("UPDATE orders SET status = ? WHERE order_id = ?", (status.value, order_id))
            repo.conn.commit()
        if status == OrderStatus.FILLED:
            log.info("live_order_filled", order_id=order_id)
    await _cancel_stale(settings, broker, tracker, repo, log)


async def _cancel_stale(settings, broker, tracker, repo, log) -> None:
    stale = tracker.stale_orders(settings.cancel_unfilled_after_seconds)
    for pending in stale:
        with suppress(Exception):
            await broker.cancel_order(pending.order_id)
        tracker.mark(pending.order_id, OrderStatus.CANCELED)
        with suppress(Exception):
            repo.conn.execute("UPDATE orders SET status = ? WHERE order_id = ?", (OrderStatus.CANCELED.value, pending.order_id))
            repo.conn.commit()
        log.info("live_order_canceled_stale", order_id=pending.order_id, age_limit=settings.cancel_unfilled_after_seconds)


async def _cancel_all_open(settings, broker, tracker, repo, log) -> None:
    for order_id in list(tracker.open_order_ids()):
        with suppress(Exception):
            await broker.cancel_order(order_id)
        tracker.mark(order_id, OrderStatus.CANCELED)
        with suppress(Exception):
            repo.conn.execute("UPDATE orders SET status = ? WHERE order_id = ?", (OrderStatus.CANCELED.value, order_id))
            repo.conn.commit()
