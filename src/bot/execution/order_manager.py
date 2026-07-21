from __future__ import annotations

from bot.execution.paper_broker import PaperBroker
from bot.execution.risk_manager import RiskManager
from bot.polymarket.models import MarketContext, OrderBook, OrderRequest, OrderSide, OutcomeSide, Signal, SignalAction


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
            price = book.best_bid
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
