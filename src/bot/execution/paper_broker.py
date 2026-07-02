from __future__ import annotations

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
            )
        )
        return record

    def cancel_order(self, order_id: str, reason: str = "") -> OrderRecord | None:
        record = self.orders.get(order_id)
        if record and record.status in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}:
            record.status = OrderStatus.CANCELED
            record.request.reason = reason or record.request.reason
        return record
