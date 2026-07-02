from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from bot.config import Settings
from bot.polymarket.models import MarketContext, OrderBook, Signal, SignalAction


@dataclass
class RiskState:
    daily_pnl_usdc: float = 0.0
    consecutive_losses: int = 0
    last_loss_at: datetime | None = None
    market_exposure: dict[str, float] = field(default_factory=dict)
    token_exposure: dict[str, float] = field(default_factory=dict)
    trades_by_market: dict[str, int] = field(default_factory=dict)
    websocket_connected: bool = True


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str


class RiskManager:
    def __init__(self, settings: Settings, state: RiskState | None = None):
        self.settings = settings
        self.state = state or RiskState()

    def validate(self, signal: Signal, context: MarketContext) -> RiskDecision:
        if signal.action == SignalAction.HOLD:
            return RiskDecision(False, signal.reason or "hold")
        if context.kill_switch_active or self.settings.kill_switch_file.exists():
            return RiskDecision(False, "kill switch active")
        if context.geoblocked:
            return RiskDecision(False, "geoblock blocks trading")
        if abs(context.clock_skew_seconds) > self.settings.clock_skew_max_seconds:
            return RiskDecision(False, "clock skew too high")
        if not context.market.is_tradeable:
            return RiskDecision(False, "ambiguous or inactive market")
        if context.market.start_time and context.market.start_time > datetime.now(UTC):
            return RiskDecision(False, "market has not started")
        if not self.state.websocket_connected:
            return RiskDecision(False, "websocket disconnected")
        if context.market.seconds_to_close is None or context.market.seconds_to_close < self.settings.min_seconds_to_close:
            return RiskDecision(False, "market close too near or unknown")

        max_age = 3 if context.market.market_type.value == "5m" else 5
        if not context.btc.is_fresh(max_age):
            return RiskDecision(False, "stale BTC price feed")

        book = self._book_for_signal(signal, context)
        if book is None:
            return RiskDecision(False, "missing order book")
        if book.spread is None or book.spread * 100 > self.settings.max_spread_cents:
            return RiskDecision(False, "spread too wide")
        if book.top_liquidity_usdc < self.settings.min_orderbook_liquidity_usdc:
            return RiskDecision(False, "liquidity too low")

        if signal.size_usdc <= 0 or signal.size_usdc > self.settings.max_position_usdc:
            return RiskDecision(False, "trade size exceeds max position")
        current_market_exposure = self.state.market_exposure.get(context.market.market_id, 0.0)
        if current_market_exposure > 0:
            return RiskDecision(False, "market already has open paper position")
        if current_market_exposure + signal.size_usdc > self.settings.max_market_position_usdc:
            return RiskDecision(False, "market exposure limit hit")
        if sum(self.state.market_exposure.values()) > 0 and context.market.market_id not in self.state.market_exposure:
            return RiskDecision(False, "one open position limit hit")
        if sum(self.state.market_exposure.values()) + signal.size_usdc > self.settings.paper_bankroll_usdc:
            return RiskDecision(False, "paper bankroll limit hit")
        if sum(self.state.market_exposure.values()) + signal.size_usdc > self.settings.max_position_usdc * self.settings.max_open_markets:
            return RiskDecision(False, "total exposure limit hit")
        if self.state.daily_pnl_usdc <= -abs(self.settings.max_daily_loss_usdc):
            return RiskDecision(False, "daily loss limit hit")
        if self.state.consecutive_losses >= self.settings.max_consecutive_losses:
            return RiskDecision(False, "consecutive loss limit hit")
        if self.state.last_loss_at and datetime.now(UTC) - self.state.last_loss_at < timedelta(seconds=self.settings.cooldown_after_loss_seconds):
            return RiskDecision(False, "loss cooldown active")
        if self.state.trades_by_market.get(context.market.market_id, 0) >= self.settings.max_trades_per_market:
            return RiskDecision(False, "max trades per market hit")
        return RiskDecision(True, "approved")

    @staticmethod
    def _book_for_signal(signal: Signal, context: MarketContext) -> OrderBook | None:
        if signal.action == SignalAction.BUY_UP:
            return context.up_book
        if signal.action == SignalAction.BUY_DOWN:
            return context.down_book
        return context.up_book or context.down_book

    def record_trade(self, market_id: str, size_usdc: float, token_id: str | None = None) -> None:
        self.state.market_exposure[market_id] = self.state.market_exposure.get(market_id, 0.0) + size_usdc
        if token_id:
            self.state.token_exposure[token_id] = self.state.token_exposure.get(token_id, 0.0) + size_usdc
        self.state.trades_by_market[market_id] = self.state.trades_by_market.get(market_id, 0) + 1
