from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from bot.config import Settings
from bot.polymarket.models import FillRecord, OrderBook, OrderRecord, OrderRequest, OrderSide, OrderStatus


def polymarket_taker_fee_usdc(shares: float, price: float, fee_rate: float) -> float:
    if shares <= 0 or price <= 0 or fee_rate <= 0:
        return 0.0
    return round(shares * fee_rate * price * (1 - price), 5)


class PaperBroker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.orders: dict[str, OrderRecord] = {}
        self.fills: list[FillRecord] = []

    def place_limit_order(self, request: OrderRequest, book: OrderBook) -> OrderRecord:
        order_id = f"paper-{uuid4()}"
        if self.settings.paper_order_style == "maker" and request.side == OrderSide.BUY:
            now = datetime.now(UTC)
            record = OrderRecord(
                order_id=order_id,
                request=request,
                status=OrderStatus.OPEN,
                execution_style="maker",
                expires_at=now + timedelta(seconds=self.settings.paper_maker_fill_window_seconds),
                created_at=now,
                updated_at=now,
            )
            self.orders[order_id] = record
            return record
        executable_price = book.best_ask if request.side == OrderSide.BUY else book.best_bid
        if executable_price is None:
            record = OrderRecord(order_id=order_id, request=request, status=OrderStatus.REJECTED)
            self.orders[order_id] = record
            return record

        slippage = self.settings.paper_slippage_cents / 100
        fill_price = executable_price + slippage if request.side == OrderSide.BUY else executable_price - slippage
        allowed = fill_price <= request.price if request.side == OrderSide.BUY else fill_price >= request.price
        if not allowed:
            record = OrderRecord(order_id=order_id, request=request, status=OrderStatus.OPEN)
            self.orders[order_id] = record
            return record

        top_liquidity = book.asks[0].notional if request.side == OrderSide.BUY and book.asks else book.bids[0].notional if book.bids else 0.0
        filled = min(request.size_usdc, max(0.0, top_liquidity * self.settings.paper_fill_ratio))
        status = OrderStatus.FILLED if filled >= request.size_usdc else OrderStatus.PARTIALLY_FILLED
        record = OrderRecord(order_id=order_id, request=request, status=status, filled_size_usdc=filled, avg_fill_price=fill_price)
        self.orders[order_id] = record
        shares = filled / fill_price if fill_price > 0 else 0.0
        fee = polymarket_taker_fee_usdc(shares, fill_price, self.settings.paper_taker_fee_rate) if self.settings.paper_enable_fees else 0.0
        self.fills.append(
            FillRecord(
                order_id=order_id,
                market_id=request.market_id,
                token_id=request.token_id,
                side=request.side,
                price=fill_price,
                size_usdc=filled,
                fee_usdc=fee,
                metadata=request.metadata,
            )
        )
        return record

    def hydrate_orders(self, orders: list[OrderRecord]) -> None:
        for order in orders:
            self.orders[order.order_id] = order

    def reconcile_maker_order(
        self,
        order: OrderRecord,
        book: OrderBook | None,
        now: datetime | None = None,
    ) -> tuple[OrderRecord, FillRecord | None]:
        """Fill a resting paper bid only after a later ask trades through it."""
        now = now or datetime.now(UTC)
        if order.status not in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}:
            return order, None
        if order.execution_style != "maker":
            return order, None
        if order.expires_at is not None and now >= order.expires_at:
            order.status = OrderStatus.CANCELED
            order.request.reason = "maker fill window expired"
            order.updated_at = now
            self.orders[order.order_id] = order
            return order, None
        if book is None or book.best_ask is None or book.best_ask > order.request.price:
            return order, None

        remaining = max(0.0, order.request.size_usdc - order.filled_size_usdc)
        top_liquidity = book.asks[0].notional if book.asks else 0.0
        filled = min(remaining, max(0.0, top_liquidity * self.settings.paper_fill_ratio))
        if filled <= 0:
            return order, None
        order.filled_size_usdc += filled
        order.avg_fill_price = order.request.price
        order.status = OrderStatus.FILLED if order.filled_size_usdc >= order.request.size_usdc else OrderStatus.PARTIALLY_FILLED
        order.updated_at = now
        fill = FillRecord(
            order_id=order.order_id,
            market_id=order.request.market_id,
            token_id=order.request.token_id,
            side=order.request.side,
            price=order.request.price,
            size_usdc=filled,
            fee_usdc=0.0,
            metadata={**order.request.metadata, "execution_style": "maker", "maker_rebate_usdc": 0.0},
            created_at=now,
        )
        self.fills.append(fill)
        self.orders[order.order_id] = order
        return order, fill

    def place_sell_order(self, request: OrderRequest, book: OrderBook, shares: float) -> tuple[OrderRecord, float, float]:
        """Simulate closing a position by selling `shares` at the bid.

        Returns (order, proceeds_usdc, fee_usdc). Used for early EXIT signals; buy
        entries continue to go through place_limit_order.
        """
        order_id = f"paper-exit-{uuid4()}"
        best_bid = book.best_bid
        if best_bid is None or shares <= 0:
            record = OrderRecord(order_id=order_id, request=request, status=OrderStatus.REJECTED)
            self.orders[order_id] = record
            return record, 0.0, 0.0

        slippage = self.settings.paper_slippage_cents / 100
        fill_price = max(0.0, best_bid - slippage)
        if fill_price < request.price:  # limit floor not met
            record = OrderRecord(order_id=order_id, request=request, status=OrderStatus.OPEN)
            self.orders[order_id] = record
            return record, 0.0, 0.0

        proceeds = shares * fill_price
        fee = polymarket_taker_fee_usdc(shares, fill_price, self.settings.paper_taker_fee_rate) if self.settings.paper_enable_fees else 0.0
        record = OrderRecord(order_id=order_id, request=request, status=OrderStatus.FILLED, filled_size_usdc=proceeds, avg_fill_price=fill_price)
        self.orders[order_id] = record
        self.fills.append(
            FillRecord(
                order_id=order_id,
                market_id=request.market_id,
                token_id=request.token_id,
                side=OrderSide.SELL,
                price=fill_price,
                size_usdc=proceeds,
                fee_usdc=fee,
                metadata=request.metadata,
            )
        )
        return record, proceeds, fee

    def cancel_order(self, order_id: str, reason: str = "") -> OrderRecord | None:
        record = self.orders.get(order_id)
        if record and record.status in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}:
            record.status = OrderStatus.CANCELED
            record.request.reason = reason or record.request.reason
            record.updated_at = datetime.now(UTC)
        return record
