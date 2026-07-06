from __future__ import annotations

from bot.backtest import recommend, recommended_env_lines, run_sweep, run_walk_forward_sweep, split_rows_by_market
from bot.backtest.dataset import TrainingRow
from bot.strategy.calibration import FEATURE_NAMES, ProbabilityModel, build_features

N_EXTRAS = len(FEATURE_NAMES) - 1


def _flat_model() -> ProbabilityModel:
    """Predicts exactly the implied price (anchor only)."""
    return ProbabilityModel(weights=[0.0] * N_EXTRAS, bias=0.0, means=[0.0] * N_EXTRAS, stds=[1.0] * N_EXTRAS)


def _row(market_id: str, side: str, ask: float, label: int, seconds: float, epoch: float, market_type: str = "5m") -> TrainingRow:
    sign = 1 if side == "UP" else -1
    return TrainingRow(
        features=build_features(0.001, 0.001, 5.0, 0.0001, 0.1, ask, sign=sign, seconds_to_close=seconds),
        label=label,
        epoch=epoch,
        created_at="",
        market_id=market_id,
        market_type=market_type,
        side=side,
        ask=ask,
        seconds_to_close=seconds,
    )


def test_run_sweep_one_trade_per_market():
    model = _flat_model()
    rows = [
        # Market m1: two decisions pass p>=0.55; only the first should trade.
        _row("m1", "UP", 0.70, 1, 400, 1.0),
        _row("m1", "UP", 0.72, 1, 350, 2.0),
        # Market m2: winner side priced at 0.60.
        _row("m2", "UP", 0.60, 1, 400, 3.0),
    ]
    cells = run_sweep(rows, model, fee_rate=0.0, min_probabilities=(0.55,), min_seconds=(45,), price_bands=((0.10, 0.90),))
    all_markets = next(cell for cell in cells if not cell.only_15m)
    assert all_markets.trades == 2
    assert all_markets.wins == 2


def test_run_sweep_gates_filter_trades():
    model = _flat_model()
    rows = [
        _row("m1", "UP", 0.30, 1, 400, 1.0),   # blocked by min_probability 0.55 (flat model predicts 0.30)
        _row("m2", "UP", 0.70, 0, 100, 2.0),   # blocked by min_seconds 300
        _row("m3", "UP", 0.95, 1, 400, 3.0),   # blocked by price band 0.10-0.90
        _row("m4", "UP", 0.70, 1, 400, 4.0),   # passes
    ]
    cells = run_sweep(rows, model, fee_rate=0.0, min_probabilities=(0.55,), min_seconds=(300,), price_bands=((0.10, 0.90),))
    cell = next(c for c in cells if not c.only_15m)
    assert cell.trades == 1
    assert cell.wins == 1


def test_run_sweep_picks_strongest_side_per_decision():
    model = _flat_model()
    # Same decision timestamp: DOWN is priced higher, so it is the candidate.
    rows = [
        _row("m1", "UP", 0.30, 1, 400, 1.0),
        _row("m1", "DOWN", 0.68, 0, 400, 1.0),
    ]
    cells = run_sweep(rows, model, fee_rate=0.0, min_probabilities=(0.55,), min_seconds=(45,), price_bands=((0.10, 0.90),))
    cell = next(c for c in cells if not c.only_15m)
    assert cell.trades == 1
    assert cell.wins == 0  # DOWN lost


def test_recommend_requires_target_wr_and_min_trades():
    model = _flat_model()
    rows = [_row(f"m{i}", "UP", 0.70, 1 if i % 4 else 0, 400, float(i)) for i in range(40)]
    cells = run_sweep(rows, model, fee_rate=0.07, min_probabilities=(0.55,), min_seconds=(45,), price_bands=((0.10, 0.90),))
    best = recommend(cells, target_win_rate=0.70, min_trades=20)
    assert best is not None
    assert best.win_rate is not None and best.win_rate >= 0.70
    assert recommend(cells, target_win_rate=0.99, min_trades=20) is None


def test_split_rows_by_market_is_chronological_and_disjoint():
    rows = []
    for i in range(10):
        rows.append(_row(f"m{i}", "UP", 0.70, 1, 400, float(i)))
        rows.append(_row(f"m{i}", "DOWN", 0.30, 0, 400, float(i)))
    train, test = split_rows_by_market(rows, split=0.8)
    train_markets = {row.market_id for row in train}
    test_markets = {row.market_id for row in test}
    assert not train_markets & test_markets
    assert len(train_markets) == 8
    assert len(test_markets) == 2
    assert max(row.epoch for row in train) < min(row.epoch for row in test)


def test_run_walk_forward_sweep_reports_both_cell_sets():
    rows = [_row(f"m{i}", "UP", 0.70, 1 if i % 4 else 0, 400, float(i)) for i in range(50)]
    report = run_walk_forward_sweep(
        rows, fee_rate=0.0, min_probabilities=(0.55,), min_seconds=(45,), price_bands=((0.10, 0.90),)
    )
    assert report["train_markets"] == 40
    assert report["test_markets"] == 10
    train_cell = next(c for c in report["train_cells"] if not c.only_15m)
    test_cell = next(c for c in report["test_cells"] if not c.only_15m)
    assert train_cell.trades <= 40
    assert test_cell.trades <= 10
    # trades in test cells only come from held-out markets
    assert train_cell.trades + test_cell.trades <= 50


def test_run_walk_forward_sweep_needs_enough_data():
    import pytest

    rows = [_row("m1", "UP", 0.70, 1, 400, 1.0)]
    with pytest.raises(ValueError):
        run_walk_forward_sweep(rows, fee_rate=0.0)


def test_recommended_env_lines_include_market_filter_when_15m_only():
    model = _flat_model()
    rows = [_row(f"m{i}", "UP", 0.70, 1, 400, float(i), market_type="15m") for i in range(25)]
    cells = run_sweep(rows, model, fee_rate=0.0, min_probabilities=(0.60,), min_seconds=(300,), price_bands=((0.55, 0.90),))
    best = recommend(cells, target_win_rate=0.70, min_trades=20)
    assert best is not None
    lines = recommended_env_lines(best)
    assert "MIN_ESTIMATED_PROBABILITY=0.6" in lines
    assert "MIN_SECONDS_TO_CLOSE=300" in lines
    if best.only_15m:
        assert "MARKET_TYPES=15m" in lines
