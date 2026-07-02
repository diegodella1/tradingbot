from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import sqrt


@dataclass(frozen=True)
class Tick:
    price: float
    timestamp: datetime


class RollingTicks:
    def __init__(self, max_age_seconds: int = 3600):
        self.max_age = timedelta(seconds=max_age_seconds)
        self.ticks: deque[Tick] = deque()

    def add(self, price: float, timestamp: datetime | None = None) -> None:
        now = timestamp or datetime.now(UTC)
        self.ticks.append(Tick(price=price, timestamp=now))
        self._trim(now)

    def _trim(self, now: datetime) -> None:
        cutoff = now - self.max_age
        while self.ticks and self.ticks[0].timestamp < cutoff:
            self.ticks.popleft()

    def price_ago(self, seconds: int) -> float | None:
        if not self.ticks:
            return None
        target = self.ticks[-1].timestamp - timedelta(seconds=seconds)
        candidate = self.ticks[0]
        for tick in self.ticks:
            if tick.timestamp <= target:
                candidate = tick
            else:
                break
        return candidate.price

    def momentum(self, seconds: int) -> float:
        if not self.ticks:
            return 0.0
        old = self.price_ago(seconds)
        if old in (None, 0):
            return 0.0
        return (self.ticks[-1].price - old) / old

    def realized_volatility(self, seconds: int = 60) -> float:
        cutoff = datetime.now(UTC) - timedelta(seconds=seconds)
        prices = [tick.price for tick in self.ticks if tick.timestamp >= cutoff]
        if len(prices) < 2:
            return 0.0
        returns = [(prices[i] - prices[i - 1]) / prices[i - 1] for i in range(1, len(prices)) if prices[i - 1] > 0]
        if len(returns) < 2:
            return 0.0
        mean = sum(returns) / len(returns)
        variance = sum((item - mean) ** 2 for item in returns) / (len(returns) - 1)
        return sqrt(variance)

