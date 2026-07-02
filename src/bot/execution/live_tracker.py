from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from bot.polymarket.models import OrderRecord, OrderStatus

OPEN_STATUSES = {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}


@dataclass
class PendingOrder:
    order_id: str
    market_id: str
    token_id: str
    created_at: datetime
    status: OrderStatus


class LiveOrderTracker:
    """Tracks live orders so the loop can reconcile fills and cancel stale ones."""

    def __init__(self) -> None:
        self.pending: dict[str, PendingOrder] = {}

    def record(self, order: OrderRecord) -> None:
        if not order.order_id or order.status not in OPEN_STATUSES:
            return
        self.pending[order.order_id] = PendingOrder(
            order_id=order.order_id,
            market_id=order.request.market_id,
            token_id=order.request.token_id,
            created_at=order.created_at,
            status=order.status,
        )

    def mark(self, order_id: str, status: OrderStatus) -> None:
        pending = self.pending.get(order_id)
        if pending is None:
            return
        if status in OPEN_STATUSES:
            pending.status = status
        else:
            self.pending.pop(order_id, None)

    def stale_orders(self, max_age_seconds: float, now: datetime | None = None) -> list[PendingOrder]:
        current = now or datetime.now(UTC)
        return [
            order
            for order in self.pending.values()
            if order.status in OPEN_STATUSES and (current - order.created_at).total_seconds() >= max_age_seconds
        ]

    def open_order_ids(self) -> list[str]:
        return [order.order_id for order in self.pending.values() if order.status in OPEN_STATUSES]
