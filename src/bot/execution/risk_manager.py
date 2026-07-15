from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from bot.config import Settings
from bot.polymarket.models import MarketContext, OrderBook, OutcomeSide, Signal, SignalAction


@dataclass
class RiskState:
    daily_pnl_usdc: float = 0.0
    consecutive_losses: int = 0
    last_loss_at: datetime | None = None
    market_exposure: dict[str, float] = field(default_factory=dict)
    token_exposure: dict[str, float] = field(default_factory=dict)
    trades_by_market: dict[str, int] = field(default_factory=dict)
    websocket_connected: bool = True
    regime_healthy: bool = True
    regime_blocked: bool = False
    trades_last_hour: int = 0
    recent_5m_pnl_usdc: float = 0.0
    recent_5m_settled_count: int = 0
    recent_pnl_usdc: float = 0.0
    recent_settled_count: int = 0
    last_settled_at: datetime | None = None


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str
    size_multiplier: float = 1.0


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
        if self.settings.max_trades_per_hour > 0 and self.state.trades_last_hour >= self.settings.max_trades_per_hour:
            return RiskDecision(False, "hourly trade limit hit")
        if context.market.market_type.value == "5m":
            if not self.settings.enable_5m_scout:
                return RiskDecision(False, "5m scout disabled")
            if (
                self.state.recent_5m_settled_count >= 2
                and self.state.recent_5m_pnl_usdc <= -abs(self.settings.disable_5m_after_recent_loss_usdc)
            ):
                return RiskDecision(False, "5m scout paused after recent losses")
        min_seconds_to_close = self.settings.minimum_seconds_to_close_for(context.market.market_type.value)
        if context.market.seconds_to_close is None or context.market.seconds_to_close < min_seconds_to_close:
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
        probability = _float_metadata(signal, "estimated_probability")
        break_even = _float_metadata(signal, "break_even_probability_after_fees")
        min_edge_cents = _float_metadata(signal, "min_net_edge_cents")
        margin_cents = max(self.settings.min_break_even_margin_cents, min_edge_cents or 0.0)
        if probability is not None and break_even is not None and probability < break_even + (margin_cents / 100):
            return RiskDecision(False, "probability below break-even after fees")
        current_market_exposure = self.state.market_exposure.get(context.market.market_id, 0.0)
        if current_market_exposure > 0:
            return RiskDecision(False, "market already has open paper position")
        if current_market_exposure + signal.size_usdc > self.settings.max_market_position_usdc:
            return RiskDecision(False, "market exposure limit hit")
        token_id = self._token_id_for_signal(signal, context)
        if token_id is not None:
            current_token_exposure = self.state.token_exposure.get(token_id, 0.0)
            if current_token_exposure + signal.size_usdc > self.settings.max_token_position_usdc:
                return RiskDecision(False, "token exposure limit hit")
        if sum(self.state.market_exposure.values()) > 0 and context.market.market_id not in self.state.market_exposure:
            return RiskDecision(False, "one open position limit hit")
        if sum(self.state.market_exposure.values()) + signal.size_usdc > self.settings.paper_bankroll_usdc:
            return RiskDecision(False, "paper bankroll limit hit")
        if sum(self.state.market_exposure.values()) + signal.size_usdc > self.settings.max_position_usdc * self.settings.max_open_markets:
            return RiskDecision(False, "total exposure limit hit")
        if self.state.daily_pnl_usdc <= -abs(self.settings.max_daily_loss_usdc):
            return RiskDecision(False, "daily loss limit hit")
        now = datetime.now(UTC)
        size_multiplier = 1.0
        if (
            self.state.recent_settled_count >= self.settings.drawdown_lookback_trades
            and self.state.recent_pnl_usdc <= -abs(self.settings.drawdown_pause_loss_usdc)
            and (
                self.state.last_settled_at is None
                or now - self.state.last_settled_at < timedelta(seconds=self.settings.drawdown_pause_seconds)
            )
        ):
            size_multiplier = min(size_multiplier, self.settings.drawdown_size_multiplier)
        loss_cooldown = timedelta(seconds=self.settings.cooldown_after_loss_seconds)
        if self.state.consecutive_losses >= self.settings.max_consecutive_losses:
            if self.state.last_loss_at is None or now - self.state.last_loss_at < loss_cooldown:
                return RiskDecision(False, "consecutive loss limit hit")
        if self.state.regime_blocked:
            return RiskDecision(False, "regime stop active: rolling WR below breakeven")
        if self.state.last_loss_at and now - self.state.last_loss_at < loss_cooldown:
            return RiskDecision(False, "loss cooldown active")
        if self.state.trades_by_market.get(context.market.market_id, 0) >= self.settings.max_trades_per_market:
            return RiskDecision(False, "max trades per market hit")
        if size_multiplier < 1.0:
            return RiskDecision(True, "approved with drawdown size reduction", size_multiplier=size_multiplier)
        return RiskDecision(True, "approved")

    @staticmethod
    def _book_for_signal(signal: Signal, context: MarketContext) -> OrderBook | None:
        if signal.action == SignalAction.BUY_UP:
            return context.up_book
        if signal.action == SignalAction.BUY_DOWN:
            return context.down_book
        return context.up_book or context.down_book

    @staticmethod
    def _token_id_for_signal(signal: Signal, context: MarketContext) -> str | None:
        side = None
        if signal.action == SignalAction.BUY_UP:
            side = OutcomeSide.UP
        elif signal.action == SignalAction.BUY_DOWN:
            side = OutcomeSide.DOWN
        if side is None:
            return None
        token = context.market.tokens.get(side)
        return token.token_id if token else None

    def record_trade(self, market_id: str, size_usdc: float, token_id: str | None = None) -> None:
        self.state.market_exposure[market_id] = self.state.market_exposure.get(market_id, 0.0) + size_usdc
        if token_id:
            self.state.token_exposure[token_id] = self.state.token_exposure.get(token_id, 0.0) + size_usdc
        self.state.trades_by_market[market_id] = self.state.trades_by_market.get(market_id, 0) + 1
        self.state.trades_last_hour += 1


def _float_metadata(signal: Signal, key: str) -> float | None:
    try:
        value = (signal.metadata or {}).get(key)
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
