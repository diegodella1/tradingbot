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

        request = OrderRequest(
            market_id=context.market.market_id,
            token_id=context.market.tokens[token_side].token_id,
            side=OrderSide.BUY,
            price=signal.max_price,
            size_usdc=signal.size_usdc,
            reason=signal.reason,
        )
        order = self.paper_broker.place_limit_order(request, book)
        if order.filled_size_usdc > 0:
            self.risk_manager.record_trade(context.market.market_id, order.filled_size_usdc, request.token_id)
        return order, decision
