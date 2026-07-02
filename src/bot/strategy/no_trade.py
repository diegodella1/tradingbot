from __future__ import annotations

from bot.polymarket.models import MarketContext, Signal, SignalAction
from bot.strategy.base import Strategy


class NoTradeStrategy(Strategy):
    def evaluate(self, context: MarketContext) -> Signal:
        return Signal(action=SignalAction.HOLD, reason="default no-trade strategy")

