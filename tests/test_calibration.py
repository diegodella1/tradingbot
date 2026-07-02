from __future__ import annotations

from bot.strategy.calibration import (
    FEATURE_NAMES,
    ProbabilityModel,
    accuracy,
    build_features,
    fit_logistic,
    log_loss,
    walk_forward,
)
from bot.strategy.momentum_book_imbalance import MomentumBookImbalanceStrategy
from bot.polymarket.models import SignalAction

N_EXTRAS = len(FEATURE_NAMES) - 1


def _flat_model(bias: float) -> ProbabilityModel:
    """Model whose prediction is driven only by the bias (anchor at implied=0.5 is 0)."""
    return ProbabilityModel(weights=[0.0] * N_EXTRAS, bias=bias, means=[0.0] * N_EXTRAS, stds=[1.0] * N_EXTRAS)


def _separable_dataset():
    rows = []
    labels = []
    for _ in range(40):
        # Positive momentum -> win; negative -> loss. Clearly separable.
        # implied fixed at 0.5 => the anchor contributes logit(0.5) = 0.
        rows.append(build_features(0.01, 0.01, 5.0, 0.0, 0.2, 0.5, sign=1, seconds_to_close=300))
        labels.append(1)
        rows.append(build_features(0.01, 0.01, 5.0, 0.0, 0.2, 0.5, sign=-1, seconds_to_close=300))
        labels.append(0)
    return rows, labels


def test_build_features_layout_matches_names():
    features = build_features(0.01, 0.02, 5.0, 0.0001, 0.2, 0.55, sign=1, seconds_to_close=300)
    assert len(features) == len(FEATURE_NAMES)
    assert features[-1] == 0.55  # implied stays last (anchor)


def test_build_features_time_scaling():
    early = build_features(0, 0, 10.0, 0.0001, 0, 0.5, sign=1, seconds_to_close=900)
    late = build_features(0, 0, 10.0, 0.0001, 0, 0.5, sign=1, seconds_to_close=30)
    move_idx = FEATURE_NAMES.index("move_per_sqrt_sec")
    # The same move is more decisive with less time remaining.
    assert late[move_idx] > early[move_idx]


def test_fit_logistic_learns_separable_signal():
    rows, labels = _separable_dataset()
    model = fit_logistic(rows, labels, epochs=400)
    assert accuracy(model, rows, labels) == 1.0
    assert log_loss(model, rows, labels) < 0.3


def test_model_is_anchored_to_market_price():
    model = _flat_model(bias=0.0)
    low = build_features(0, 0, 0, 0, 0, 0.3, sign=1, seconds_to_close=300)
    high = build_features(0, 0, 0, 0, 0, 0.8, sign=1, seconds_to_close=300)
    assert abs(model.predict_proba(low) - 0.3) < 1e-9
    assert abs(model.predict_proba(high) - 0.8) < 1e-9


def test_probability_model_roundtrip(tmp_path):
    rows, labels = _separable_dataset()
    model = fit_logistic(rows, labels, epochs=100)
    path = tmp_path / "model.json"
    model.save(path)
    loaded = ProbabilityModel.load(path)
    assert loaded is not None
    assert loaded.is_compatible()
    sample = rows[0]
    assert abs(loaded.predict_proba(sample) - model.predict_proba(sample)) < 1e-9


def test_load_missing_model_returns_none(tmp_path):
    assert ProbabilityModel.load(tmp_path / "does_not_exist.json") is None


def test_old_feature_layout_is_incompatible():
    old = ProbabilityModel(
        weights=[0.0] * 6,
        bias=0.0,
        means=[0.0] * 6,
        stds=[1.0] * 6,
        feature_names=["m15", "m60", "open_move", "volatility", "book", "implied"],
    )
    assert not old.is_compatible()


def test_walk_forward_reports_market_baseline():
    rows, labels = _separable_dataset()
    report = walk_forward(rows, labels, train_fraction=0.8)
    assert report["train_samples"] + report["test_samples"] == len(rows)
    assert report["market_log_loss"] > 0
    assert report["test_log_loss"] < report["market_log_loss"]  # separable => beats flat market prob


def test_strategy_uses_calibrated_model_when_present(tmp_path, settings, context):
    model_path = tmp_path / "model.json"
    _flat_model(bias=10.0).save(model_path)
    settings.probability_model_path = model_path
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
    context.btc.momentum_60s = 0.004

    signal = MomentumBookImbalanceStrategy(settings).evaluate(context)

    assert signal.action == SignalAction.BUY_UP
    assert signal.metadata["probability_source"] == "calibrated"
    assert signal.metadata["estimated_probability"] > 0.9


def test_strategy_ignores_incompatible_model(tmp_path, settings, context):
    old = ProbabilityModel(
        weights=[0.0] * 6,
        bias=10.0,
        means=[0.0] * 6,
        stds=[1.0] * 6,
        feature_names=["m15", "m60", "open_move", "volatility", "book", "implied"],
    )
    model_path = tmp_path / "model.json"
    old.save(model_path)
    settings.probability_model_path = model_path
    settings.enable_experimental_strategy = True

    strategy = MomentumBookImbalanceStrategy(settings)

    assert strategy.model is None


def test_strategy_falls_back_to_heuristic_without_model(settings, context):
    settings.enable_experimental_strategy = True
    settings.min_confidence = 0.1
    settings.min_edge_cents = 1
    settings.min_net_edge_cents = 1
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

    assert signal.metadata["probability_source"] == "heuristic"
