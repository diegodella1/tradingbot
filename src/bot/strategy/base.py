from __future__ import annotations

from abc import ABC, abstractmethod

from bot.polymarket.models import MarketContext, Signal


class Strategy(ABC):
    @abstractmethod
    def evaluate(self, context: MarketContext) -> Signal:
        raise NotImplementedError

