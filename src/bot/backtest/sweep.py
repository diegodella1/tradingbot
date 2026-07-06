from __future__ import annotations

from dataclasses import dataclass

from bot.backtest.dataset import TrainingRow
from bot.execution.paper_broker import polymarket_taker_fee_usdc
from bot.strategy.calibration import ProbabilityModel, fit_logistic

DEFAULT_MIN_PROBABILITIES = (0.55, 0.60, 0.65, 0.70, 0.75)
DEFAULT_MIN_SECONDS = (45, 180, 300, 420)
DEFAULT_PRICE_BANDS = ((0.10, 0.90), (0.45, 0.90), (0.55, 0.90))


@dataclass
class SweepCell:
    min_probability: float
    min_seconds_to_close: int
    price_band: tuple[float, float]
    only_15m: bool
    trades: int
    wins: int
    pnl_usdc: float

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.trades if self.trades else None


def _trade_pnl(ask: float, won: bool, fee_rate: float, size_usdc: float = 1.0) -> float:
    shares = size_usdc / ask
    fee = polymarket_taker_fee_usdc(shares, ask, fee_rate)
    return (shares - size_usdc - fee) if won else -(size_usdc + fee)


def _simulate(
    by_market: dict[str, list[tuple[TrainingRow, float]]],
    min_probability: float,
    min_seconds: int,
    band: tuple[float, float],
    only_15m: bool,
    fee_rate: float,
) -> tuple[int, int, float]:
    """One trade per market: the first chronological decision passing all gates."""
    trades = wins = 0
    pnl = 0.0
    for entries in by_market.values():
        for row, probability in entries:
            if only_15m and row.market_type != "15m":
                continue
            if probability < min_probability:
                continue
            if row.seconds_to_close < min_seconds:
                continue
            if not band[0] <= row.ask <= band[1]:
                continue
            trades += 1
            wins += row.label
            pnl += _trade_pnl(row.ask, bool(row.label), fee_rate)
            break
    return trades, wins, pnl


def run_sweep(
    rows: list[TrainingRow],
    model: ProbabilityModel,
    fee_rate: float,
    min_probabilities: tuple[float, ...] = DEFAULT_MIN_PROBABILITIES,
    min_seconds: tuple[int, ...] = DEFAULT_MIN_SECONDS,
    price_bands: tuple[tuple[float, float], ...] = DEFAULT_PRICE_BANDS,
) -> list[SweepCell]:
    """Grid-search entry gates against historical decisions scored by the model.

    Emulates real bot behavior (one trade per market, first qualifying signal,
    hold to resolution, taker fees). Only the strongest side per decision is a
    candidate, mirroring `evaluate()` picking the best of UP/DOWN.
    """
    scored: dict[str, list[tuple[TrainingRow, float]]] = {}
    best_by_decision: dict[tuple[str, float], tuple[TrainingRow, float]] = {}
    for row in rows:
        probability = model.predict_proba(row.features)
        key = (row.market_id, row.epoch)
        current = best_by_decision.get(key)
        if current is None or probability > current[1]:
            best_by_decision[key] = (row, probability)
    for row, probability in sorted(best_by_decision.values(), key=lambda item: item[0].epoch):
        scored.setdefault(row.market_id, []).append((row, probability))

    cells: list[SweepCell] = []
    for min_probability in min_probabilities:
        for seconds in min_seconds:
            for band in price_bands:
                for only_15m in (False, True):
                    trades, wins, pnl = _simulate(scored, min_probability, seconds, band, only_15m, fee_rate)
                    cells.append(
                        SweepCell(
                            min_probability=min_probability,
                            min_seconds_to_close=seconds,
                            price_band=band,
                            only_15m=only_15m,
                            trades=trades,
                            wins=wins,
                            pnl_usdc=pnl,
                        )
                    )
    return cells


def split_rows_by_market(rows: list[TrainingRow], split: float = 0.8) -> tuple[list[TrainingRow], list[TrainingRow]]:
    """Chronological train/test split at market boundaries (no market straddles both sets)."""
    first_epoch: dict[str, float] = {}
    for row in rows:
        current = first_epoch.get(row.market_id)
        if current is None or row.epoch < current:
            first_epoch[row.market_id] = row.epoch
    ordered_markets = sorted(first_epoch, key=lambda market_id: first_epoch[market_id])
    cut = max(1, int(len(ordered_markets) * split))
    train_markets = set(ordered_markets[:cut])
    train = [row for row in rows if row.market_id in train_markets]
    test = [row for row in rows if row.market_id not in train_markets]
    return train, test


def run_walk_forward_sweep(
    rows: list[TrainingRow],
    fee_rate: float,
    split: float = 0.8,
    min_probabilities: tuple[float, ...] = DEFAULT_MIN_PROBABILITIES,
    min_seconds: tuple[int, ...] = DEFAULT_MIN_SECONDS,
    price_bands: tuple[tuple[float, float], ...] = DEFAULT_PRICE_BANDS,
) -> dict:
    """Out-of-sample sweep: fit the model on the first 80% of markets, then grid-search
    gates on BOTH sets with that model. Only the test cells are honest estimates; the
    train cells are reported to expose the in-sample optimism per configuration.
    """
    train_rows, test_rows = split_rows_by_market(rows, split)
    if not train_rows or not test_rows:
        raise ValueError("not enough data to split train/test")
    model = fit_logistic([row.features for row in train_rows], [row.label for row in train_rows])
    grids = {"min_probabilities": min_probabilities, "min_seconds": min_seconds, "price_bands": price_bands}
    return {
        "model": model,
        "train_rows": len(train_rows),
        "test_rows": len(test_rows),
        "train_markets": len({row.market_id for row in train_rows}),
        "test_markets": len({row.market_id for row in test_rows}),
        "train_cells": run_sweep(train_rows, model, fee_rate, **grids),
        "test_cells": run_sweep(test_rows, model, fee_rate, **grids),
    }


def recommend(cells: list[SweepCell], target_win_rate: float = 0.70, min_trades: int = 20) -> SweepCell | None:
    """Best cell by PnL subject to WR >= target and enough trades to be meaningful.

    Ties (identical trade sets) are broken toward the stricter time gate:
    production data showed entries under 180s to close win only ~39%.
    """
    eligible = [cell for cell in cells if cell.trades >= min_trades and (cell.win_rate or 0) >= target_win_rate]
    if not eligible:
        return None
    return max(eligible, key=lambda cell: (cell.pnl_usdc, cell.win_rate or 0, cell.min_seconds_to_close))


def recommended_env_lines(cell: SweepCell) -> list[str]:
    lines = [
        f"MIN_ESTIMATED_PROBABILITY={cell.min_probability}",
        f"MIN_SECONDS_TO_CLOSE={cell.min_seconds_to_close}",
        f"MIN_ENTRY_PRICE={cell.price_band[0]}",
        f"MAX_ENTRY_PRICE={cell.price_band[1]}",
    ]
    if cell.only_15m:
        lines.append("MARKET_TYPES=15m")
    return lines
