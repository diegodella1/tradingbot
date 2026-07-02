from __future__ import annotations

from bot.strategy.momentum_book_imbalance import MomentumBookImbalanceStrategy, kelly_fraction
from bot.strategy.no_trade import NoTradeStrategy
from bot.polymarket.models import SignalAction


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
    settings.max_entry_price = 0.90
    settings.min_profit_if_win_usdc = 0.70
    context.up_book.asks[0].price = 0.66
    context.up_book.bids[0].price = 0.65
    context.up_book.asks[0].size = 1000
    context.up_book.bids[0].size = 2000
    context.btc.current_price = 102
    context.btc.market_open_price = 100
    context.btc.momentum_15s = 0.003
    context.btc.momentum_60s = 0.004

    signal = MomentumBookImbalanceStrategy(settings).evaluate(context)

    assert signal.action == SignalAction.HOLD
    assert "profit if win" in signal.reason


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
