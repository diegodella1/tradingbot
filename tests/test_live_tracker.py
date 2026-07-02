from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.execution.live_tracker import LiveOrderTracker
from bot.polymarket.models import OrderRecord, OrderRequest, OrderSide, OrderStatus


def _order(order_id: str, status: OrderStatus, created_at: datetime) -> OrderRecord:
    request = OrderRequest(market_id="m1", token_id="up-token", side=OrderSide.BUY, price=0.5, size_usdc=1.0)
    return OrderRecord(order_id=order_id, request=request, status=status, created_at=created_at)


def test_tracker_records_open_orders_only():
    tracker = LiveOrderTracker()
    tracker.record(_order("o1", OrderStatus.OPEN, datetime.now(UTC)))
    tracker.record(_order("o2", OrderStatus.REJECTED, datetime.now(UTC)))
    assert tracker.open_order_ids() == ["o1"]


def test_tracker_mark_terminal_removes_order():
    tracker = LiveOrderTracker()
    tracker.record(_order("o1", OrderStatus.OPEN, datetime.now(UTC)))
    tracker.mark("o1", OrderStatus.FILLED)
    assert tracker.open_order_ids() == []


def test_tracker_stale_orders_by_age():
    tracker = LiveOrderTracker()
    now = datetime.now(UTC)
    tracker.record(_order("old", OrderStatus.OPEN, now - timedelta(seconds=60)))
    tracker.record(_order("fresh", OrderStatus.OPEN, now))
    stale = tracker.stale_orders(max_age_seconds=20, now=now)
    assert [order.order_id for order in stale] == ["old"]
