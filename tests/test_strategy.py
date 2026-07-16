from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.strategy.momentum_book_imbalance import MomentumBookImbalanceStrategy, kelly_fraction
from bot.strategy.no_trade import NoTradeStrategy
from bot.polymarket.models import MarketType, Signal, SignalAction


def test_no_trade_strategy_returns_hold(context):
    signal = NoTradeStrategy().evaluate(context)
    assert signal.action == SignalAction.HOLD


def test_experimental_strategy_returns_hold_when_disabled(settings, context):
    settings.enable_experimental_strategy = False
    signal = MomentumBookImbalanceStrategy(settings).evaluate(context)
    assert signal.action == SignalAction.HOLD
    assert "disabled" in signal.reason


def test_strategy_does_not_emit_exit_for_hold_to_resolution(settings, context):
    settings.enable_experimental_strategy = True
    settings.hold_to_resolution = True
    context.btc.momentum_15s = -0.01
    context.btc.momentum_60s = -0.02
    context.btc.market_open_price = 110
    context.btc.current_price = 100

    signal = MomentumBookImbalanceStrategy(settings).evaluate(context)

    assert signal.action != SignalAction.EXIT


def test_kelly_fraction_requires_positive_edge():
    assert kelly_fraction(0.52, 0.55) == 0.0
    assert round(kelly_fraction(0.60, 0.50), 4) == 0.2


def test_strategy_uses_positive_ev_kelly_size(settings, context):
    settings.enable_experimental_strategy = True
    settings.min_edge_cents = 1
    settings.min_net_edge_cents = 1
    settings.min_confidence = 0.1
    settings.kelly_fraction_multiplier = 1.0
    settings.min_kelly_size_usdc = 0.01
    context.up_book.asks[0].price = 0.40
    context.up_book.bids[0].price = 0.39
    context.up_book.asks[0].size = 1000
    context.up_book.bids[0].size = 2000
    context.btc.current_price = 102
    context.btc.market_open_price = 100
    context.btc.momentum_15s = 0.003
    context.btc.momentum_60s = 0.004

    signal = MomentumBookImbalanceStrategy(settings).evaluate(context)

    assert signal.action == SignalAction.BUY_UP
    assert signal.metadata["edge"] > 0
    assert signal.metadata["kelly_fraction"] > 0
    assert signal.size_usdc > 0
    assert signal.metadata["net_edge"] > 0
    assert signal.metadata["profit_if_win_per_1"] > 0
    assert signal.metadata["policy_version"] == settings.policy_version
    assert signal.metadata["config_snapshot"]["policy_version"] == settings.policy_version
    assert signal.metadata["break_even_probability_after_fees"] > 0


def test_strategy_blocks_low_probability_longshots(settings, context):
    settings.enable_experimental_strategy = True
    settings.min_entry_price = 0.10
    context.up_book.asks[0].price = 0.03
    context.btc.current_price = 102
    context.btc.market_open_price = 100
    context.btc.momentum_15s = 0.003
    context.btc.momentum_60s = 0.004

    signal = MomentumBookImbalanceStrategy(settings).evaluate(context)

    assert signal.action == SignalAction.HOLD
    assert "entry band" in signal.reason


def test_strategy_blocks_low_payout_expensive_entries(settings, context):
    settings.enable_experimental_strategy = True
    settings.min_confidence = 0.1
    settings.min_edge_cents = 1
    settings.max_entry_price = 0.90
    settings.min_net_edge_cents = 25
    context.up_book.asks[0].price = 0.60
    context.up_book.bids[0].price = 0.59
    context.up_book.asks[0].size = 1000
    context.up_book.bids[0].size = 2000
    context.btc.current_price = 102
    context.btc.market_open_price = 100
    context.btc.momentum_15s = 0.003
    context.btc.momentum_60s = 0.004

    signal = MomentumBookImbalanceStrategy(settings).evaluate(context)

    assert signal.action == SignalAction.HOLD
    assert "net edge" in signal.reason


def test_strategy_blocks_positive_gross_edge_but_low_net_edge(settings, context):
    settings.enable_experimental_strategy = True
    settings.min_confidence = 0.1
    settings.min_edge_cents = 1
    settings.min_net_edge_cents = 20
    settings.min_profit_if_win_usdc = 0.1
    settings.min_kelly_size_usdc = 0.01
    context.up_book.asks[0].price = 0.50
    context.up_book.bids[0].price = 0.49
    context.up_book.asks[0].size = 1000
    context.up_book.bids[0].size = 2000
    context.btc.current_price = 102
    context.btc.market_open_price = 100
    context.btc.momentum_15s = 0.003
    context.btc.momentum_60s = 0.004

    signal = MomentumBookImbalanceStrategy(settings).evaluate(context)

    assert signal.action == SignalAction.HOLD
    assert "net edge" in signal.reason


def test_strategy_allows_sub_dollar_kelly_size(settings, context):
    settings.enable_experimental_strategy = True
    settings.min_confidence = 0.1
    settings.min_edge_cents = 1
    settings.min_net_edge_cents = 1
    settings.min_profit_if_win_usdc = 0.1
    settings.paper_bankroll_usdc = 100
    settings.paper_trade_size_usdc = 1
    settings.kelly_fraction_multiplier = 0.01
    settings.min_kelly_size_usdc = 0.01
    context.up_book.asks[0].price = 0.40
    context.up_book.bids[0].price = 0.39
    context.up_book.asks[0].size = 1000
    context.up_book.bids[0].size = 2000
    context.btc.current_price = 102
    context.btc.market_open_price = 100
    context.btc.momentum_15s = 0.003
    context.btc.momentum_60s = 0.004

    signal = MomentumBookImbalanceStrategy(settings).evaluate(context)

    assert signal.action == SignalAction.BUY_UP
    assert 0 < signal.size_usdc < 1


def test_strategy_blocks_low_estimated_probability(settings, context):
    settings.enable_experimental_strategy = True
    settings.min_confidence = 0.1
    settings.min_estimated_probability = 0.95  # above what the heuristic can produce here
    context.up_book.asks[0].price = 0.40
    context.up_book.bids[0].price = 0.39
    context.up_book.asks[0].size = 1000
    context.up_book.bids[0].size = 2000
    context.btc.current_price = 102
    context.btc.market_open_price = 100
    context.btc.momentum_15s = 0.003
    context.btc.momentum_60s = 0.004

    signal = MomentumBookImbalanceStrategy(settings).evaluate(context)

    assert signal.action == SignalAction.HOLD
    assert "probability below minimum" in signal.reason


def test_strategy_uses_15m_specific_probability_gate(settings, context):
    settings.enable_experimental_strategy = True
    settings.min_probability_15m = 0.65
    settings.min_probability_5m = 0.90
    context.market.market_type = MarketType.FIFTEEN_MINUTE
    context.market.question = "Bitcoin Up or Down - 15 minute"
    context.market.slug = "bitcoin-up-or-down-15m"
    context.market.end_time = context.market.start_time.replace() + (context.market.end_time - context.market.start_time)
    context.up_book.asks[0].price = 0.40
    context.up_book.bids[0].price = 0.39
    context.up_book.asks[0].size = 1000
    context.up_book.bids[0].size = 2000
    context.btc.current_price = 102
    context.btc.market_open_price = 100
    context.btc.momentum_15s = 0.003
    context.btc.momentum_60s = 0.004

    signal = MomentumBookImbalanceStrategy(settings).evaluate(context)

    assert signal.action == SignalAction.BUY_UP
    assert signal.metadata["market_type"] == "15m"
    assert signal.metadata["min_probability"] == 0.65


def test_strategy_blocks_5m_when_scout_disabled(settings, context):
    settings.enable_experimental_strategy = True
    settings.enable_5m_scout = False

    signal = MomentumBookImbalanceStrategy(settings).evaluate(context)

    assert signal.action == SignalAction.HOLD
    assert "5m scout disabled" in signal.reason


def test_strategy_reports_all_failed_gates_without_changing_primary_reason(settings, context):
    settings.enable_experimental_strategy = True
    settings.enable_5m_scout = False
    context.btc.price_timestamp = datetime.now(UTC) - timedelta(seconds=30)

    signal = MomentumBookImbalanceStrategy(settings).evaluate(context)

    assert signal.reason == "stale BTC feed"
    assert signal.metadata["primary_reason"] == "stale BTC feed"
    assert "feed.stale_btc" in signal.metadata["failed_gates"]
    assert "UP.scout_disabled" in signal.metadata["failed_gates"]
    assert "DOWN.scout_disabled" in signal.metadata["failed_gates"]


def test_executable_side_beats_higher_confidence_hold(settings, context, monkeypatch):
    strategy = MomentumBookImbalanceStrategy(settings)
    hold = Signal(
        action=SignalAction.HOLD,
        confidence=0.99,
        reason="negative edge",
    )
    buy = Signal(
        action=SignalAction.BUY_DOWN,
        confidence=0.80,
        reason="positive EV momentum/book decision",
        size_usdc=0.75,
    )
    monkeypatch.setattr(strategy, "_candidate", lambda action, book, ctx: hold if action == SignalAction.BUY_UP else buy)
    monkeypatch.setattr(strategy, "_failed_gates", lambda ctx: [])
    settings.enable_experimental_strategy = True

    signal = strategy.evaluate(context)

    assert signal.action == SignalAction.BUY_DOWN


def test_strategy_blocks_5m_below_scout_probability_gate(settings, context):
    settings.enable_experimental_strategy = True
    settings.min_probability_15m = 0.60
    settings.min_probability_5m = 0.80
    context.up_book.asks[0].price = 0.40
    context.up_book.bids[0].price = 0.39
    context.up_book.asks[0].size = 1000
    context.up_book.bids[0].size = 2000
    context.btc.current_price = 102
    context.btc.market_open_price = 100
    context.btc.momentum_15s = 0.003
    context.btc.momentum_60s = 0.004

    signal = MomentumBookImbalanceStrategy(settings).evaluate(context)

    assert signal.action == SignalAction.HOLD
    assert "probability below minimum" in signal.reason


def test_strategy_blocks_15m_below_specific_entry_floor(settings, context):
    settings.enable_experimental_strategy = True
    settings.min_entry_price_15m = 0.65
    context.market.market_type = MarketType.FIFTEEN_MINUTE
    context.up_book.asks[0].price = 0.60
    context.up_book.bids[0].price = 0.59

    signal = MomentumBookImbalanceStrategy(settings).evaluate(context)

    assert signal.action == SignalAction.HOLD
    assert "entry band" in signal.reason


def test_strategy_tightens_15m_danger_zone(settings, context):
    settings.enable_experimental_strategy = True
    settings.min_entry_price_15m = 0.65
    settings.danger_zone_min_price = 0.70
    settings.danger_zone_max_price = 0.75
    settings.danger_zone_min_probability = 0.90
    settings.danger_zone_min_net_edge_cents = 20
    context.market.market_type = MarketType.FIFTEEN_MINUTE
    context.up_book.asks[0].price = 0.72
    context.up_book.bids[0].price = 0.71
    context.up_book.asks[0].size = 1000
    context.up_book.bids[0].size = 2000
    context.btc.current_price = 102
    context.btc.market_open_price = 100
    context.btc.momentum_15s = 0.003
    context.btc.momentum_60s = 0.004

    signal = MomentumBookImbalanceStrategy(settings).evaluate(context)

    assert signal.action == SignalAction.HOLD
    assert "probability below minimum" in signal.reason


def test_strategy_sizes_by_timeframe_cap(settings, context):
    settings.enable_experimental_strategy = True
    settings.paper_bankroll_usdc = 100
    settings.max_trade_pct_15m = 0.02
    settings.max_trade_pct_5m = 0.0075
    settings.kelly_fraction_multiplier = 1.0
    context.up_book.asks[0].price = 0.40
    context.up_book.bids[0].price = 0.39
    context.up_book.asks[0].size = 1000
    context.up_book.bids[0].size = 2000
    context.btc.current_price = 102
    context.btc.market_open_price = 100
    context.btc.momentum_15s = 0.003
    context.btc.momentum_60s = 0.004

    signal_5m = MomentumBookImbalanceStrategy(settings).evaluate(context)
    context.market.market_type = MarketType.FIFTEEN_MINUTE
    signal_15m = MomentumBookImbalanceStrategy(settings).evaluate(context)

    assert signal_5m.action == SignalAction.BUY_UP
    assert signal_5m.size_usdc <= 0.75
    assert signal_15m.action == SignalAction.BUY_UP
    assert signal_15m.size_usdc <= 2.0
    assert signal_15m.size_usdc >= signal_5m.size_usdc


def test_strategy_uses_quality_size_tiers(settings, context):
    settings.enable_experimental_strategy = True
    settings.paper_bankroll_usdc = 100
    settings.kelly_fraction_multiplier = 1.0
    context.market.market_type = MarketType.FIFTEEN_MINUTE
    settings.size_tier_good_probability = 0.95
    settings.size_tier_strong_probability = 0.96
    settings.size_tier_max_probability = 0.97
    context.up_book.asks[0].price = 0.40
    context.up_book.bids[0].price = 0.39
    context.up_book.asks[0].size = 1000
    context.up_book.bids[0].size = 2000
    context.btc.current_price = 102
    context.btc.market_open_price = 100
    context.btc.momentum_15s = 0.003
    context.btc.momentum_60s = 0.004

    base_signal = MomentumBookImbalanceStrategy(settings).evaluate(context)
    settings.size_tier_max_probability = 0.60
    settings.size_tier_max_net_edge_cents = 1
    max_signal = MomentumBookImbalanceStrategy(settings).evaluate(context)

    assert base_signal.action == SignalAction.BUY_UP
    assert base_signal.size_usdc <= settings.size_tier_base_usdc
    assert max_signal.action == SignalAction.BUY_UP
    assert max_signal.size_usdc > base_signal.size_usdc
    assert max_signal.size_usdc <= settings.size_tier_max_usdc


def test_strategy_blocks_extreme_volatility_regime(settings, context):
    settings.enable_experimental_strategy = True
    settings.max_realized_volatility = 0.001
    context.btc.realized_volatility = 0.005
    context.btc.current_price = 102
    context.btc.market_open_price = 100
    context.btc.momentum_15s = 0.003
    context.btc.momentum_60s = 0.004

    signal = MomentumBookImbalanceStrategy(settings).evaluate(context)

    assert signal.action == SignalAction.HOLD
    assert "volatility" in signal.reason


def test_strategy_blocks_tiny_move_since_open(settings, context):
    settings.enable_experimental_strategy = True
    settings.min_abs_change_since_open = 15.0
    context.btc.current_price = 100008  # only +8 USD vs open
    context.btc.market_open_price = 100000
    context.btc.momentum_15s = 0.003
    context.btc.momentum_60s = 0.004

    signal = MomentumBookImbalanceStrategy(settings).evaluate(context)

    assert signal.action == SignalAction.HOLD
    assert "oracle divergence" in signal.reason


def test_strategy_allows_move_above_min_abs_change(settings, context):
    settings.enable_experimental_strategy = True
    settings.min_abs_change_since_open = 15.0
    settings.min_edge_cents = 1
    settings.min_net_edge_cents = 1
    settings.min_confidence = 0.1
    settings.kelly_fraction_multiplier = 1.0
    settings.min_kelly_size_usdc = 0.01
    context.up_book.asks[0].price = 0.40
    context.up_book.bids[0].price = 0.39
    context.up_book.asks[0].size = 1000
    context.up_book.bids[0].size = 2000
    context.btc.current_price = 100040  # +40 USD vs open, clears the gate
    context.btc.market_open_price = 100000
    context.btc.momentum_15s = 0.003
    context.btc.momentum_60s = 0.004

    signal = MomentumBookImbalanceStrategy(settings).evaluate(context)

    assert signal.action == SignalAction.BUY_UP


def test_strategy_requires_aligned_btc_momentum(settings, context):
    settings.enable_experimental_strategy = True
    settings.min_confidence = 0.1
    settings.min_kelly_size_usdc = 0.01
    context.up_book.asks[0].price = 0.40
    context.up_book.bids[0].price = 0.39
    context.up_book.asks[0].size = 1000
    context.up_book.bids[0].size = 2000
    context.btc.current_price = 102
    context.btc.market_open_price = 100
    context.btc.momentum_15s = 0.003
    context.btc.momentum_60s = -0.004

    signal = MomentumBookImbalanceStrategy(settings).evaluate(context)

    assert signal.action == SignalAction.HOLD
    assert "momentum" in signal.reason
