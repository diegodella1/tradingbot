from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR

from bot.execution.paper_broker import PaperBroker
from bot.execution.risk_manager import RiskManager
from bot.polymarket.models import MarketContext, OrderBook, OrderRequest, OrderSide, OutcomeSide, Signal, SignalAction


MAKER_PRICE_TICK = Decimal("0.01")


def paper_maker_bid_price(best_bid: float, signal_max_price: float, offset_cents: float) -> float | None:
    """Return a passive maker bid, floored to the paper market's one-cent tick."""
    adjusted_bid = Decimal(str(best_bid)) - (Decimal(str(offset_cents)) / Decimal("100"))
    capped_bid = min(adjusted_bid, Decimal(str(signal_max_price)))
    price = capped_bid.quantize(MAKER_PRICE_TICK, rounding=ROUND_FLOOR)
    return float(price) if price >= MAKER_PRICE_TICK else None


class OrderManager:
    def __init__(self, risk_manager: RiskManager, paper_broker: PaperBroker):
        self.risk_manager = risk_manager
        self.paper_broker = paper_broker

    def execute_paper_signal(self, signal: Signal, context: MarketContext):
        decision = self.risk_manager.validate(signal, context)
        if not decision.approved:
            return None, decision

        token_side = OutcomeSide.UP if signal.action == SignalAction.BUY_UP else OutcomeSide.DOWN
        book: OrderBook | None = context.up_book if token_side == OutcomeSide.UP else context.down_book
        if book is None:
            return None, decision

        price = signal.max_price
        if self.paper_broker.settings.paper_order_style == "maker":
            if book.best_bid is None:
                return None, decision
            price = paper_maker_bid_price(
                book.best_bid,
                signal.max_price,
                self.paper_broker.settings.paper_maker_bid_offset_cents,
            )
            if price is None:
                return None, decision

        request = OrderRequest(
            market_id=context.market.market_id,
            token_id=context.market.tokens[token_side].token_id,
            side=OrderSide.BUY,
            price=price,
            size_usdc=max(
                1e-6,
                min(signal.size_usdc * decision.size_multiplier, self.paper_broker.settings.paper_max_trade_size_usdc),
            ),
            reason=signal.reason,
            metadata={
                **(signal.metadata or {}),
                "risk_decision": decision.reason,
                "risk_size_multiplier": decision.size_multiplier,
                "execution_style": self.paper_broker.settings.paper_order_style,
                "signal_max_price": signal.max_price,
                "paper_maker_bid_offset_cents": self.paper_broker.settings.paper_maker_bid_offset_cents,
                "posted_price": price,
            },
        )
        order = self.paper_broker.place_limit_order(request, book)
        if order.filled_size_usdc > 0:
            self.risk_manager.record_trade(context.market.market_id, order.filled_size_usdc, request.token_id)
        return order, decision

    def execute_exit_signal(self, signal: Signal, context: MarketContext, position: dict):
        """Close an open paper position at the bid. `position` needs token_id, shares, cost_usdc, fee_usdc."""
        if signal.action != SignalAction.EXIT:
            return None, 0.0
        token_side = OutcomeSide.UP if position.get("side") == OutcomeSide.UP.value else OutcomeSide.DOWN
        book: OrderBook | None = context.up_book if token_side == OutcomeSide.UP else context.down_book
        if book is None:
            return None, 0.0

        shares = float(position.get("shares") or 0.0)
        cost = float(position.get("cost_usdc") or 0.0)
        entry_fee = float(position.get("fee_usdc") or 0.0)
        request = OrderRequest(
            market_id=context.market.market_id,
            token_id=str(position.get("token_id")),
            side=OrderSide.SELL,
            price=signal.max_price,
            size_usdc=max(cost, 1e-6),
            reason=signal.reason,
            metadata={**(signal.metadata or {}), "exit_position_id": position.get("id")},
        )
        order, proceeds, exit_fee = self.paper_broker.place_sell_order(request, book, shares)
        if order.filled_size_usdc <= 0:
            return order, 0.0
        realized = proceeds - cost - entry_fee - exit_fee
        return order, realized
