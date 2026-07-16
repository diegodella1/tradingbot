from __future__ import annotations

from bot.config import Settings
from bot.execution.paper_broker import polymarket_taker_fee_usdc
from bot.polymarket.models import MarketContext, OrderBook, OutcomeSide, Signal, SignalAction
from bot.strategy.base import Strategy
from bot.strategy.calibration import ProbabilityModel, build_features


def kelly_fraction(probability: float, price: float) -> float:
    if price <= 0 or price >= 1 or probability <= price:
        return 0.0
    return max(0.0, (probability - price) / (1 - price))


class MomentumBookImbalanceStrategy(Strategy):
    def __init__(self, settings: Settings):
        self.settings = settings
        self.config_snapshot = settings.strategy_config_snapshot()
        # A calibrated model (trained via `cli calibrate`) supersedes the hand-tuned
        # heuristic when present; otherwise we fall back to the heuristic scoring.
        # Models trained with an older feature layout are ignored, not misapplied.
        model = ProbabilityModel.load(settings.probability_model_path)
        self.model: ProbabilityModel | None = model if model is not None and model.is_compatible() else None

    def evaluate(self, context: MarketContext) -> Signal:
        signal = self._evaluate_primary(context)
        metadata = dict(signal.metadata or {})
        metadata["primary_reason"] = signal.reason
        metadata["failed_gates"] = self._failed_gates(context)
        return signal.model_copy(update={"metadata": metadata})

    def _evaluate_primary(self, context: MarketContext) -> Signal:
        if not self.settings.enable_experimental_strategy:
            return self._hold("experimental strategy disabled", context)
        if not context.market.is_tradeable:
            return self._hold("market not verified tradeable", context)
        if not context.up_book or not context.down_book:
            return self._hold("missing order book", context)

        max_age = 3 if context.market.market_type.value == "5m" else 5
        if not context.btc.is_fresh(max_age):
            return self._hold("stale BTC feed", context)
        min_seconds_to_close = self.settings.minimum_seconds_to_close_for(context.market.market_type.value)
        if context.market.seconds_to_close is None or context.market.seconds_to_close < min_seconds_to_close:
            return self._hold("too close to market close", context)
        if context.btc.realized_volatility > self.settings.max_realized_volatility:
            # In extreme volatility regimes the outcome is close to a coin flip.
            return self._hold("volatility regime too extreme", context)
        if (
            self.settings.min_abs_change_since_open > 0
            and abs(context.btc.change_since_open) < self.settings.min_abs_change_since_open
        ):
            # Tiny moves can flip on the Coinbase-vs-Chainlink divergence: the
            # resolution oracle may sit on the other side of our proxy price.
            return self._hold("move since open too small vs oracle divergence", context)

        up = self._candidate(SignalAction.BUY_UP, context.up_book, context)
        down = self._candidate(SignalAction.BUY_DOWN, context.down_book, context)
        # A high-confidence HOLD (for example, expensive side with negative edge)
        # must never suppress an executable positive-EV candidate on the other side.
        return max(
            [up, down],
            key=lambda signal: (signal.action != SignalAction.HOLD, signal.confidence),
        )

    def _failed_gates(self, context: MarketContext) -> list[str]:
        """Return all failed safety/edge gates without changing primary decision.

        Production historically stored only the first rejection. That made a large
        `too_close` count look actionable even when the same candidate also had
        negative edge. Diagnostics evaluate both sides independently so tuning can
        use a real funnel instead of guessing from first-failure telemetry.
        """
        failures: list[str] = []
        market_type = context.market.market_type.value
        if not self.settings.enable_experimental_strategy:
            failures.append("strategy.experimental_disabled")
        if not context.market.is_tradeable:
            failures.append("market.not_tradeable")
        if not context.up_book or not context.down_book:
            failures.append("market.missing_order_book")
        max_age = 3 if market_type == "5m" else 5
        if not context.btc.is_fresh(max_age):
            failures.append("feed.stale_btc")
        min_seconds = self.settings.minimum_seconds_to_close_for(market_type)
        if context.market.seconds_to_close is None or context.market.seconds_to_close < min_seconds:
            failures.append("market.too_close")
        if context.btc.realized_volatility > self.settings.max_realized_volatility:
            failures.append("market.volatility_extreme")
        if self.settings.min_abs_change_since_open > 0 and abs(context.btc.change_since_open) < self.settings.min_abs_change_since_open:
            failures.append("market.move_too_small")
        if context.up_book:
            failures.extend(self._candidate_failed_gates("UP", SignalAction.BUY_UP, context.up_book, context))
        if context.down_book:
            failures.extend(self._candidate_failed_gates("DOWN", SignalAction.BUY_DOWN, context.down_book, context))
        return failures

    def _candidate_failed_gates(
        self,
        side: str,
        action: SignalAction,
        book: OrderBook,
        context: MarketContext,
    ) -> list[str]:
        prefix = f"{side}."
        failures: list[str] = []
        market_type = context.market.market_type.value
        if market_type == "5m" and not self.settings.enable_5m_scout:
            failures.append(prefix + "scout_disabled")
        ask = book.best_ask
        if ask is None:
            return failures + [prefix + "missing_ask"]
        if ask < self.settings.minimum_entry_price_for(market_type) or ask > self.settings.max_entry_price:
            failures.append(prefix + "price_outside_band")
        if book.spread is None or book.spread * 100 > self.settings.max_spread_cents:
            failures.append(prefix + "spread_too_wide")
        if book.top_liquidity_usdc < self.settings.min_orderbook_liquidity_usdc:
            failures.append(prefix + "insufficient_liquidity")

        wants_up = action == SignalAction.BUY_UP
        direction = 1 if wants_up else -1
        momentum_15 = direction * context.btc.momentum_15s
        momentum_60 = direction * context.btc.momentum_60s
        open_move = direction * context.btc.change_since_open
        book_support = direction * book.imbalance
        if momentum_15 <= 0 or momentum_60 <= 0 or open_move <= 0:
            failures.append(prefix + "momentum_not_aligned")
        if book_support < self.settings.minimum_book_imbalance_for(market_type):
            failures.append(prefix + "book_not_supportive")

        probability, _ = self._estimate_probability(context, book, ask, direction)
        min_probability, min_net_edge_cents = self.settings.price_bucket_requirements(market_type, ask)
        if probability < min_probability:
            failures.append(prefix + "probability_below_minimum")
        edge_cents = (probability - ask) * 100
        shares = 1 / ask if ask > 0 else 0.0
        fee = (
            polymarket_taker_fee_usdc(shares, ask, self.settings.paper_taker_fee_rate)
            if shares > 0 and self.settings.paper_enable_fees
            else 0.0
        )
        break_even = (1 + fee) / shares if shares > 0 else 1.0
        net_edge_cents = (probability - break_even) * 100
        confidence = max(0.0, min(1.0, abs(probability - 0.5) * 2 + max(0.0, book_support)))
        if confidence < self.settings.minimum_confidence_for(market_type):
            failures.append(prefix + "confidence_below_minimum")
        if edge_cents < self.settings.min_edge_cents:
            failures.append(prefix + "edge_below_minimum")
        if net_edge_cents < min_net_edge_cents:
            failures.append(prefix + "net_edge_below_minimum")
        max_trade_size = min(
            self.settings.max_position_usdc,
            self.settings.max_market_position_usdc,
            self.settings.paper_bankroll_usdc * self.settings.max_trade_pct_for(market_type),
        )
        recommended_size = min(
            self.settings.size_tier_usdc(probability, net_edge_cents),
            max_trade_size,
            self.settings.paper_bankroll_usdc
            * kelly_fraction(probability, ask)
            * self.settings.kelly_fraction_multiplier,
        )
        if recommended_size < self.settings.min_kelly_size_usdc:
            failures.append(prefix + "kelly_below_minimum")
        if 0.47 <= ask <= 0.53 and confidence < 0.85:
            failures.append(prefix + "near_even_low_confidence")
        return failures

    def _candidate(self, action: SignalAction, book: OrderBook, context: MarketContext) -> Signal:
        market_type = context.market.market_type.value
        if market_type == "5m" and not self.settings.enable_5m_scout:
            return self._hold("5m scout disabled", context, {"candidate_action": action.value})
        ask = book.best_ask
        if ask is None:
            return self._hold("missing ask", context, {"candidate_action": action.value})
        base_metadata = self._policy_metadata(context, {"candidate_action": action.value, "market_price": ask})
        min_entry_price = self.settings.minimum_entry_price_for(market_type)
        if ask < min_entry_price or ask > self.settings.max_entry_price:
            return Signal(action=SignalAction.HOLD, reason="price outside entry band", metadata=base_metadata)

        spread = book.spread
        if spread is None or spread * 100 > self.settings.max_spread_cents:
            return Signal(action=SignalAction.HOLD, reason="spread too wide", metadata=base_metadata | {"spread": spread})
        if book.top_liquidity_usdc < self.settings.min_orderbook_liquidity_usdc:
            return Signal(action=SignalAction.HOLD, reason="insufficient book liquidity", metadata=base_metadata | {"book_liquidity_usdc": book.top_liquidity_usdc})

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
        min_book_imbalance = self.settings.minimum_book_imbalance_for(market_type)
        if momentum_15 <= 0 or momentum_60 <= 0 or open_move <= 0:
            return Signal(action=SignalAction.HOLD, reason="BTC momentum not aligned", metadata=base_metadata | {"features": features})
        if book_support < min_book_imbalance:
            return Signal(action=SignalAction.HOLD, reason="book imbalance not supportive", metadata=base_metadata | {"features": features})

        estimated_probability, probability_source = self._estimate_probability(context, book, ask, direction)
        min_probability, min_net_edge_cents = self.settings.price_bucket_requirements(market_type, ask)
        if estimated_probability < min_probability:
            # Underdogs with nominal edge lost consistently in production data
            # (0% WR below p=0.6); winning often requires being the favorite.
            return Signal(
                action=SignalAction.HOLD,
                reason="estimated probability below minimum",
                metadata=base_metadata | {"features": features, "estimated_probability": estimated_probability, "min_probability": min_probability},
            )
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
        net_edge_cents = net_edge * 100
        confidence = max(0.0, min(1.0, abs(estimated_probability - 0.5) * 2 + max(0.0, book_support)))
        kelly = kelly_fraction(estimated_probability, ask)
        max_trade_size = min(
            self.settings.max_position_usdc,
            self.settings.max_market_position_usdc,
            self.settings.paper_bankroll_usdc * self.settings.max_trade_pct_for(market_type),
        )
        tier_size = self.settings.size_tier_usdc(estimated_probability, net_edge_cents)
        recommended_size = min(
            tier_size,
            max_trade_size,
            self.settings.paper_bankroll_usdc * kelly * self.settings.kelly_fraction_multiplier,
        )
        min_confidence = self.settings.minimum_confidence_for(market_type)
        metadata = base_metadata | {
            "market_type": market_type,
            "estimated_probability": estimated_probability,
            "probability_source": probability_source,
            "min_probability": min_probability,
            "market_price": ask,
            "edge": edge,
            "edge_cents": edge * 100,
            "net_edge": net_edge,
            "net_edge_cents": net_edge_cents,
            "min_net_edge_cents": min_net_edge_cents,
            "profit_if_win_per_1": profit_if_win_per_1,
            "break_even_probability_after_fees": break_even_probability_after_fees,
            "estimated_fee_per_1": estimated_fee_per_1,
            "ev_usdc_per_1": edge / ask if ask > 0 else 0.0,
            "kelly_fraction": kelly,
            "max_trade_pct": self.settings.max_trade_pct_for(market_type),
            "max_trade_size_usdc": max_trade_size,
            "size_tier_usdc": tier_size,
            "recommended_size_usdc": recommended_size,
            "features": features,
        }

        if confidence < min_confidence:
            return Signal(action=SignalAction.HOLD, confidence=confidence, reason=f"{OutcomeSide.UP if wants_up else OutcomeSide.DOWN} confidence too low", metadata=metadata)
        if edge * 100 < self.settings.min_edge_cents:
            return Signal(action=SignalAction.HOLD, confidence=confidence, reason=f"{OutcomeSide.UP if wants_up else OutcomeSide.DOWN} edge below threshold", metadata=metadata)
        if net_edge_cents < min_net_edge_cents:
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

    def _estimate_probability(self, context: MarketContext, book: OrderBook, price: float, direction: int) -> tuple[float, str]:
        """Estimate P(chosen side wins). Uses the calibrated model if loaded, else the heuristic."""
        if self.model is not None:
            features = build_features(
                momentum_15s=context.btc.momentum_15s,
                momentum_60s=context.btc.momentum_60s,
                change_since_open=context.btc.change_since_open,
                realized_volatility=context.btc.realized_volatility,
                book_imbalance=book.imbalance,
                implied=price,
                sign=direction,
                seconds_to_close=float(context.market.seconds_to_close or 0.0),
            )
            return max(0.01, min(0.99, self.model.predict_proba(features))), "calibrated"

        momentum_15 = direction * context.btc.momentum_15s
        momentum_60 = direction * context.btc.momentum_60s
        open_move = direction * context.btc.change_since_open
        book_support = direction * book.imbalance
        momentum_score = max(-1.0, min(1.0, (momentum_15 * 6000) + (momentum_60 * 3000)))
        open_score = max(-1.0, min(1.0, open_move / 35))
        book_score = max(-1.0, min(1.0, book_support * 2.5))
        volatility_penalty = min(0.08, context.btc.realized_volatility * 80)
        raw_probability = 0.5 + (0.12 * momentum_score) + (0.08 * open_score) + (0.06 * book_score) - volatility_penalty
        return max(0.01, min(0.99, raw_probability)), "heuristic"

    def evaluate_exit(self, context: MarketContext, held_side: OutcomeSide, entry_price: float) -> Signal:
        """Emit EXIT when an open position's win probability deteriorates.

        Disabled by default: when ``hold_to_resolution`` is true or exit signals are
        off, positions are always held to settlement (returns HOLD).
        """
        if self.settings.hold_to_resolution or not self.settings.enable_exit_signals:
            return Signal(action=SignalAction.HOLD, reason="hold to resolution")
        book = context.up_book if held_side == OutcomeSide.UP else context.down_book
        if book is None or book.best_bid is None:
            return Signal(action=SignalAction.HOLD, reason="missing exit book")
        direction = 1 if held_side == OutcomeSide.UP else -1
        probability, source = self._estimate_probability(context, book, book.best_bid, direction)
        metadata = {"estimated_probability": probability, "probability_source": source, "entry_price": entry_price}
        if probability < self.settings.exit_min_probability:
            # Protective sell floor that still tolerates simulated slippage at the bid.
            sell_floor = max(0.0, book.best_bid - 2 * (self.settings.paper_slippage_cents / 100))
            return Signal(
                action=SignalAction.EXIT,
                confidence=max(0.0, min(1.0, 1 - probability)),
                max_price=sell_floor,
                reason="win probability deteriorated",
                metadata=metadata,
            )
        return Signal(action=SignalAction.HOLD, reason="position still favorable", metadata=metadata)

    def _hold(self, reason: str, context: MarketContext, metadata: dict | None = None) -> Signal:
        return Signal(action=SignalAction.HOLD, reason=reason, metadata=self._policy_metadata(context, metadata))

    def _policy_metadata(self, context: MarketContext, metadata: dict | None = None) -> dict:
        payload = {
            "policy_version": self.settings.policy_version,
            "config_snapshot": self.config_snapshot,
            "market_type": context.market.market_type.value,
            "seconds_to_close": context.market.seconds_to_close,
            "probability_source": "calibrated" if self.model is not None else "heuristic",
        }
        if metadata:
            payload.update(metadata)
        return payload
