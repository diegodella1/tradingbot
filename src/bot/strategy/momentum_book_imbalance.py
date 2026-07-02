from __future__ import annotations

from bot.config import Settings
from bot.execution.paper_broker import polymarket_taker_fee_usdc
from bot.polymarket.models import MarketContext, OrderBook, OutcomeSide, Signal, SignalAction
from bot.strategy.base import Strategy


def kelly_fraction(probability: float, price: float) -> float:
    if price <= 0 or price >= 1 or probability <= price:
        return 0.0
    return max(0.0, (probability - price) / (1 - price))


class MomentumBookImbalanceStrategy(Strategy):
    def __init__(self, settings: Settings):
        self.settings = settings

    def evaluate(self, context: MarketContext) -> Signal:
        if not self.settings.enable_experimental_strategy:
            return Signal(action=SignalAction.HOLD, reason="experimental strategy disabled")
        if not context.market.is_tradeable:
            return Signal(action=SignalAction.HOLD, reason="market not verified tradeable")
        if not context.up_book or not context.down_book:
            return Signal(action=SignalAction.HOLD, reason="missing order book")

        max_age = 3 if context.market.market_type.value == "5m" else 5
        if not context.btc.is_fresh(max_age):
            return Signal(action=SignalAction.HOLD, reason="stale BTC feed")
        if context.market.seconds_to_close is None or context.market.seconds_to_close < self.settings.min_seconds_to_close:
            return Signal(action=SignalAction.HOLD, reason="too close to market close")

        up = self._candidate(SignalAction.BUY_UP, context.up_book, context)
        down = self._candidate(SignalAction.BUY_DOWN, context.down_book, context)
        return max([up, down], key=lambda signal: signal.confidence)

    def _candidate(self, action: SignalAction, book: OrderBook, context: MarketContext) -> Signal:
        ask = book.best_ask
        if ask is None:
            return Signal(action=SignalAction.HOLD, reason="missing ask")
        if ask < self.settings.min_entry_price or ask > self.settings.max_entry_price:
            return Signal(action=SignalAction.HOLD, reason="price outside entry band")

        spread = book.spread
        if spread is None or spread * 100 > self.settings.max_spread_cents:
            return Signal(action=SignalAction.HOLD, reason="spread too wide")
        if book.top_liquidity_usdc < self.settings.min_orderbook_liquidity_usdc:
            return Signal(action=SignalAction.HOLD, reason="insufficient book liquidity")

        wants_up = action == SignalAction.BUY_UP
        direction = 1 if wants_up else -1
        features = {
            "momentum_15s": context.btc.momentum_15s,
            "momentum_60s": context.btc.momentum_60s,
            "change_since_open": context.btc.change_since_open,
            "realized_volatility": context.btc.realized_volatility,
            "book_imbalance": book.imbalance,
            "seconds_to_close": context.market.seconds_to_close,
        }
        momentum_15 = direction * context.btc.momentum_15s
        momentum_60 = direction * context.btc.momentum_60s
        open_move = direction * context.btc.change_since_open
        book_support = direction * book.imbalance
        if momentum_15 <= 0 or momentum_60 <= 0 or open_move <= 0:
            return Signal(action=SignalAction.HOLD, reason="BTC momentum not aligned", metadata={"features": features})
        if book_support < self.settings.min_book_imbalance:
            return Signal(action=SignalAction.HOLD, reason="book imbalance not supportive", metadata={"features": features})

        momentum_score = max(-1.0, min(1.0, (momentum_15 * 6000) + (momentum_60 * 3000)))
        open_score = max(-1.0, min(1.0, open_move / 35))
        book_score = max(-1.0, min(1.0, book_support * 2.5))
        volatility_penalty = min(0.08, context.btc.realized_volatility * 80)
        raw_probability = 0.5 + (0.12 * momentum_score) + (0.08 * open_score) + (0.06 * book_score) - volatility_penalty
        estimated_probability = max(0.01, min(0.99, raw_probability))
        edge = estimated_probability - ask
        shares_per_1 = 1 / ask if ask > 0 else 0.0
        profit_if_win_per_1 = shares_per_1 - 1
        estimated_fee_per_1 = (
            polymarket_taker_fee_usdc(shares_per_1, ask, self.settings.paper_taker_fee_rate)
            if self.settings.paper_enable_fees
            else 0.0
        )
        break_even_probability_after_fees = (1 + estimated_fee_per_1) / shares_per_1 if shares_per_1 > 0 else 1.0
        net_edge = estimated_probability - break_even_probability_after_fees
        confidence = max(0.0, min(1.0, abs(estimated_probability - 0.5) * 2 + max(0.0, book_support)))
        kelly = kelly_fraction(estimated_probability, ask)
        recommended_size = min(
            self.settings.paper_trade_size_usdc,
            self.settings.max_position_usdc,
            self.settings.paper_bankroll_usdc * kelly * self.settings.kelly_fraction_multiplier,
        )
        metadata = {
            "estimated_probability": estimated_probability,
            "market_price": ask,
            "edge": edge,
            "edge_cents": edge * 100,
            "net_edge": net_edge,
            "net_edge_cents": net_edge * 100,
            "profit_if_win_per_1": profit_if_win_per_1,
            "break_even_probability_after_fees": break_even_probability_after_fees,
            "estimated_fee_per_1": estimated_fee_per_1,
            "ev_usdc_per_1": edge / ask if ask > 0 else 0.0,
            "kelly_fraction": kelly,
            "recommended_size_usdc": recommended_size,
            "features": features,
        }

        if confidence < self.settings.min_confidence:
            return Signal(action=SignalAction.HOLD, confidence=confidence, reason=f"{OutcomeSide.UP if wants_up else OutcomeSide.DOWN} confidence too low", metadata=metadata)
        if profit_if_win_per_1 < self.settings.min_profit_if_win_usdc:
            return Signal(action=SignalAction.HOLD, confidence=confidence, reason="profit if win below minimum", metadata=metadata)
        if edge * 100 < self.settings.min_edge_cents:
            return Signal(action=SignalAction.HOLD, confidence=confidence, reason=f"{OutcomeSide.UP if wants_up else OutcomeSide.DOWN} edge below threshold", metadata=metadata)
        if net_edge * 100 < self.settings.min_net_edge_cents:
            return Signal(action=SignalAction.HOLD, confidence=confidence, reason="net edge after fees below threshold", metadata=metadata)
        if recommended_size < self.settings.min_kelly_size_usdc:
            return Signal(action=SignalAction.HOLD, confidence=confidence, reason="Kelly size below minimum", metadata=metadata)
        if 0.47 <= ask <= 0.53 and confidence < 0.85:
            return Signal(action=SignalAction.HOLD, confidence=confidence, reason="near 0.50 without high confidence", metadata=metadata)

        max_price = min(ask + 0.01, estimated_probability - (self.settings.min_edge_cents / 100), 0.98)
        metadata["max_price"] = max_price
        return Signal(
            action=action,
            confidence=confidence,
            max_price=max_price,
            size_usdc=recommended_size,
            reason="positive EV momentum/book decision",
            metadata=metadata,
        )
