from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.execution.paper_broker import PaperBroker
from bot.polymarket.models import BookLevel, OrderBook, OrderRequest, OrderSide, OrderStatus
from bot.storage.db import connect, init_db
from bot.storage.repositories import Repository


def _maker_settings(settings):
    settings.paper_order_style = "maker"
    settings.paper_maker_fill_window_seconds = 60
    settings.paper_max_trade_size_usdc = 0.25
    return settings


def test_maker_order_rests_then_fills_at_posted_bid_without_fee(settings, book):
    broker = PaperBroker(_maker_settings(settings))
    order = broker.place_limit_order(
        OrderRequest(market_id="m1", token_id="up-token", side=OrderSide.BUY, price=book.best_bid, size_usdc=0.25),
        book,
    )

    assert order.status == OrderStatus.OPEN
    assert broker.fills == []
    later = OrderBook(
        market_id="m1",
        token_id="up-token",
        bids=[BookLevel(price=0.48, size=100)],
        asks=[BookLevel(price=0.49, size=100)],
    )
    updated, fill = broker.reconcile_maker_order(order, later, now=order.created_at + timedelta(seconds=30))

    assert updated.status == OrderStatus.FILLED
    assert fill is not None
    assert fill.price == 0.49
    assert fill.size_usdc == 0.25
    assert fill.fee_usdc == 0


def test_maker_order_cancels_after_fill_window(settings, book):
    broker = PaperBroker(_maker_settings(settings))
    order = broker.place_limit_order(
        OrderRequest(market_id="m1", token_id="up-token", side=OrderSide.BUY, price=book.best_bid, size_usdc=0.25),
        book,
    )
    updated, fill = broker.reconcile_maker_order(order, book, now=order.expires_at + timedelta(microseconds=1))

    assert updated.status == OrderStatus.CANCELED
    assert fill is None


def test_open_maker_order_survives_restart_and_reserves_exposure(settings, book):
    settings = _maker_settings(settings)
    init_db(settings.sqlite_path)
    broker = PaperBroker(settings)
    order = broker.place_limit_order(
        OrderRequest(
            market_id="m1",
            token_id="up-token",
            side=OrderSide.BUY,
            price=book.best_bid,
            size_usdc=0.25,
            metadata={"policy_version": "v4"},
        ),
        book,
    )
    with connect(settings.sqlite_path) as conn:
        repo = Repository(conn)
        repo.save_order(order)
        restored = repo.open_maker_orders()
        state = repo.hydrate_risk_state()

    assert len(restored) == 1
    assert restored[0].order_id == order.order_id
    assert restored[0].expires_at == order.expires_at
    assert state.market_exposure["m1"] == 0.25
    assert state.trades_today == 0
    assert state.trades_last_hour == 0


def test_regime_snapshot_can_isolate_policy(settings):
    from bot.monitoring.regime import regime_snapshot

    init_db(settings.sqlite_path)
    now = datetime.now(UTC).isoformat()
    with connect(settings.sqlite_path) as conn:
        for version, status, pnl in (("old", "LOST", -1.0), ("v4", "WON", 0.6)):
            conn.execute(
                """
                INSERT INTO positions (
                  market_id, token_id, size_usdc, avg_price, status,
                  realized_pnl_usdc, policy_version, settled_at, updated_at
                ) VALUES (?, ?, 1, 0.60, ?, ?, ?, ?, ?)
                """,
                (version, version, status, pnl, version, now, now),
            )
        conn.commit()
        snapshot = regime_snapshot(conn, min_trades=1, policy_version="v4")

    assert snapshot["trades"] == 1
    assert snapshot["win_rate"] == 1.0
    assert snapshot["healthy"] is True
