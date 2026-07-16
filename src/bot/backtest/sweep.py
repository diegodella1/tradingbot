from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from bot.backtest.dataset import TrainingRow
from bot.config import Settings
from bot.execution.paper_broker import polymarket_taker_fee_usdc
from bot.strategy.momentum_book_imbalance import kelly_fraction
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
    min_net_edge_cents: float
    trades: int
    wins: int
    pnl_usdc: float
    gross_profit_usdc: float = 0.0
    gross_loss_usdc: float = 0.0
    max_drawdown_pct: float = 0.0
    days: int = 1
    windows: int = 0
    profitable_windows: int = 0

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.trades if self.trades else None

    @property
    def roi(self) -> float | None:
        return self.pnl_usdc / self.trades if self.trades else None

    @property
    def profit_factor(self) -> float | None:
        if self.gross_loss_usdc > 0:
            return self.gross_profit_usdc / self.gross_loss_usdc
        return None if self.gross_profit_usdc <= 0 else float("inf")

    @property
    def trades_per_day(self) -> float:
        return self.trades / max(1, self.days)


@dataclass
class _Simulation:
    trades: int
    wins: int
    pnl_usdc: float
    gross_profit_usdc: float
    gross_loss_usdc: float
    max_drawdown_pct: float
    days: int
    windows: int
    profitable_windows: int


def _trade_pnl(ask: float, won: bool, fee_rate: float, size_usdc: float = 1.0) -> float:
    shares = size_usdc / ask
    fee = polymarket_taker_fee_usdc(shares, ask, fee_rate)
    return (shares - size_usdc - fee) if won else -(size_usdc + fee)


def _simulate(
    scored: list[tuple[TrainingRow, float]],
    min_probability: float,
    min_seconds: int,
    band: tuple[float, float],
    only_15m: bool,
    min_net_edge_cents: float,
    fee_rate: float,
    settings: Settings | None,
) -> _Simulation:
    """One trade per market: the first chronological decision passing all gates."""
    trades = wins = 0
    pnl = 0.0
    gross_profit = gross_loss = 0.0
    peak = equity = float(settings.paper_bankroll_usdc if settings else 100.0)
    max_drawdown = 0.0
    traded_markets: set[str] = set()
    hourly_epochs: list[float] = []
    daily_counts: dict[str, int] = {}
    daily_pnl: dict[str, float] = {}
    if scored:
        first_day = datetime.fromtimestamp(scored[0][0].epoch, UTC).date()
        last_day = datetime.fromtimestamp(scored[-1][0].epoch, UTC).date()
        days = max(1, (last_day - first_day).days + 1)
    else:
        days = 1
    decisions: dict[tuple[str, float], list[tuple[TrainingRow, float]]] = {}
    for row, probability in scored:
        decisions.setdefault((row.market_id, row.epoch), []).append((row, probability))
    ordered_decisions = sorted(decisions.values(), key=lambda candidates: candidates[0][0].epoch)
    for candidates in ordered_decisions:
        eligible_candidates: list[tuple[TrainingRow, float]] = []
        for row, probability in candidates:
            if only_15m and row.market_type != "15m":
                continue
            if probability < min_probability or row.seconds_to_close < min_seconds or not band[0] <= row.ask <= band[1]:
                continue
            if settings is not None and not _passes_live_gates(row, probability, min_probability, min_net_edge_cents, fee_rate, settings):
                continue
            eligible_candidates.append((row, probability))
        if not eligible_candidates:
            continue
        row, probability = max(
            eligible_candidates,
            key=lambda item: _candidate_confidence(item[0], item[1]),
        )
        if row.market_id in traded_markets:
            continue
        day = datetime.fromtimestamp(row.epoch, UTC).date().isoformat()
        if settings is not None:
            hourly_epochs = [epoch for epoch in hourly_epochs if epoch > row.epoch - 3600]
            if settings.max_trades_per_hour > 0 and len(hourly_epochs) >= settings.max_trades_per_hour:
                continue
            if settings.max_trades_per_day > 0 and daily_counts.get(day, 0) >= settings.max_trades_per_day:
                continue
        trade_pnl = _trade_pnl(row.ask, bool(row.label), fee_rate)
        trades += 1
        wins += row.label
        pnl += trade_pnl
        gross_profit += max(0.0, trade_pnl)
        gross_loss += abs(min(0.0, trade_pnl))
        equity += trade_pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak if peak > 0 else 0.0)
        traded_markets.add(row.market_id)
        hourly_epochs.append(row.epoch)
        daily_counts[day] = daily_counts.get(day, 0) + 1
        daily_pnl[day] = daily_pnl.get(day, 0.0) + trade_pnl
    return _Simulation(
        trades=trades,
        wins=wins,
        pnl_usdc=pnl,
        gross_profit_usdc=gross_profit,
        gross_loss_usdc=gross_loss,
        max_drawdown_pct=max_drawdown,
        days=days,
        windows=len(daily_pnl),
        profitable_windows=sum(1 for value in daily_pnl.values() if value > 0),
    )


def _candidate_confidence(row: TrainingRow, probability: float) -> float:
    return max(0.0, min(1.0, abs(probability - 0.5) * 2 + max(0.0, row.features[4])))


def _passes_live_gates(
    row: TrainingRow,
    probability: float,
    min_probability: float,
    min_net_edge_cents: float,
    fee_rate: float,
    settings: Settings,
) -> bool:
    momentum_15, momentum_60, open_move, _, book_support = row.features[:5]
    if momentum_15 <= 0 or momentum_60 <= 0 or open_move <= 0:
        return False
    if book_support < settings.minimum_book_imbalance_for(row.market_type):
        return False
    bucket_probability = min_probability
    bucket_net_edge = min_net_edge_cents
    if row.market_type == "15m":
        if settings.danger_zone_min_price <= row.ask < settings.danger_zone_max_price:
            bucket_probability = max(bucket_probability, settings.danger_zone_min_probability)
            bucket_net_edge = max(bucket_net_edge, settings.danger_zone_min_net_edge_cents)
        elif row.ask >= settings.danger_zone_max_price:
            bucket_probability = max(bucket_probability, settings.high_price_min_probability)
            bucket_net_edge = max(bucket_net_edge, settings.high_price_min_net_edge_cents)
    if probability < bucket_probability:
        return False
    edge_cents = (probability - row.ask) * 100
    shares = 1 / row.ask
    fee = polymarket_taker_fee_usdc(shares, row.ask, fee_rate)
    break_even = (1 + fee) / shares
    net_edge_cents = (probability - break_even) * 100
    confidence = max(0.0, min(1.0, abs(probability - 0.5) * 2 + max(0.0, book_support)))
    if confidence < settings.minimum_confidence_for(row.market_type):
        return False
    if edge_cents < settings.min_edge_cents or net_edge_cents < bucket_net_edge:
        return False
    max_trade_size = min(
        settings.max_position_usdc,
        settings.max_market_position_usdc,
        settings.paper_bankroll_usdc * settings.max_trade_pct_for(row.market_type),
    )
    recommended_size = min(
        settings.size_tier_usdc(probability, net_edge_cents),
        max_trade_size,
        settings.paper_bankroll_usdc * kelly_fraction(probability, row.ask) * settings.kelly_fraction_multiplier,
    )
    return recommended_size >= settings.min_kelly_size_usdc


def run_sweep(
    rows: list[TrainingRow],
    model: ProbabilityModel,
    fee_rate: float,
    min_probabilities: tuple[float, ...] = DEFAULT_MIN_PROBABILITIES,
    min_seconds: tuple[int, ...] = DEFAULT_MIN_SECONDS,
    price_bands: tuple[tuple[float, float], ...] = DEFAULT_PRICE_BANDS,
    min_net_edges: tuple[float, ...] = (0.0,),
    settings: Settings | None = None,
) -> list[SweepCell]:
    """Grid-search entry gates against historical decisions scored by the model.

    Emulates real bot behavior (one trade per market, first qualifying signal,
    hold to resolution, taker fees). Both sides pass the full gate set before
    confidence chooses between executable candidates.
    """
    scored = sorted(
        ((row, model.predict_proba(row.features)) for row in rows),
        key=lambda item: item[0].epoch,
    )

    cells: list[SweepCell] = []
    for min_probability in min_probabilities:
        for seconds in min_seconds:
            for band in price_bands:
                for min_net_edge in min_net_edges:
                    for only_15m in (False, True):
                        result = _simulate(scored, min_probability, seconds, band, only_15m, min_net_edge, fee_rate, settings)
                        cells.append(
                            SweepCell(
                                min_probability=min_probability,
                                min_seconds_to_close=seconds,
                                price_band=band,
                                only_15m=only_15m,
                                min_net_edge_cents=min_net_edge,
                                trades=result.trades,
                                wins=result.wins,
                                pnl_usdc=result.pnl_usdc,
                                gross_profit_usdc=result.gross_profit_usdc,
                                gross_loss_usdc=result.gross_loss_usdc,
                                max_drawdown_pct=result.max_drawdown_pct,
                                days=result.days,
                                windows=result.windows,
                                profitable_windows=result.profitable_windows,
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
    min_net_edges: tuple[float, ...] = (0.0,),
    settings: Settings | None = None,
) -> dict:
    """Out-of-sample sweep: fit the model on the first 80% of markets, then grid-search
    gates on BOTH sets with that model. Only the test cells are honest estimates; the
    train cells are reported to expose the in-sample optimism per configuration.
    """
    train_rows, test_rows = split_rows_by_market(rows, split)
    if not train_rows or not test_rows:
        raise ValueError("not enough data to split train/test")
    model = fit_logistic([row.features for row in train_rows], [row.label for row in train_rows])
    grids = {
        "min_probabilities": min_probabilities,
        "min_seconds": min_seconds,
        "price_bands": price_bands,
        "min_net_edges": min_net_edges,
        "settings": settings,
    }
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


def recommend_policy(
    cells: list[SweepCell],
    *,
    target_min_per_day: float = 2.0,
    target_max_per_day: float = 6.0,
    min_trades: int = 20,
    min_profit_factor: float = 1.10,
    max_drawdown_pct: float = 0.15,
) -> SweepCell | None:
    """Choose a paper-only 15m policy under explicit profitability/risk gates."""
    eligible = [
        cell
        for cell in cells
        if cell.only_15m
        and cell.trades >= min_trades
        and cell.pnl_usdc > 0
        and (cell.roi or 0) > 0
        and (cell.profit_factor or 0) >= min_profit_factor
        and cell.max_drawdown_pct <= max_drawdown_pct
        and cell.windows > 0
        and cell.profitable_windows > cell.windows / 2
        and target_min_per_day <= cell.trades_per_day <= target_max_per_day
    ]
    if not eligible:
        return None
    midpoint = (target_min_per_day + target_max_per_day) / 2
    return max(
        eligible,
        key=lambda cell: (
            cell.pnl_usdc,
            cell.profit_factor or 0,
            -cell.max_drawdown_pct,
            -abs(cell.trades_per_day - midpoint),
            cell.min_seconds_to_close,
        ),
    )


def policy_config(cell: SweepCell) -> dict:
    """Allowlisted paper overrides matching the selected sweep cell."""
    return {
        "market_types": ["15m"],
        "enable_5m_scout": False,
        "min_estimated_probability": cell.min_probability,
        "min_probability_15m": cell.min_probability,
        "min_entry_price": cell.price_band[0],
        "min_entry_price_15m": cell.price_band[0],
        "max_entry_price": cell.price_band[1],
        "min_net_edge_cents": cell.min_net_edge_cents,
        "min_net_edge_15m_cents": cell.min_net_edge_cents,
        "min_seconds_to_close": cell.min_seconds_to_close,
        "min_seconds_to_close_15m": cell.min_seconds_to_close,
        "max_trades_per_hour": 2,
        "max_trades_per_day": 6,
    }


def policy_evidence(cell: SweepCell, report: dict) -> dict:
    profit_factor = cell.profit_factor
    return {
        "method": "chronological_market_split_80_20_full_gates",
        "train_markets": report["train_markets"],
        "test_markets": report["test_markets"],
        "trades": cell.trades,
        "wins": cell.wins,
        "win_rate": cell.win_rate,
        "pnl_usdc": cell.pnl_usdc,
        "roi": cell.roi,
        "profit_factor": 999.0 if profit_factor == float("inf") else profit_factor,
        "max_drawdown_pct": cell.max_drawdown_pct,
        "trades_per_day": cell.trades_per_day,
        "windows": cell.windows,
        "profitable_windows": cell.profitable_windows,
        "target_entries_per_day": [2, 6],
        "config": policy_config(cell),
    }


def recommended_env_lines(cell: SweepCell) -> list[str]:
    lines = [
        f"MIN_ESTIMATED_PROBABILITY={cell.min_probability}",
        f"MIN_SECONDS_TO_CLOSE={cell.min_seconds_to_close}",
        f"MIN_ENTRY_PRICE={cell.price_band[0]}",
        f"MAX_ENTRY_PRICE={cell.price_band[1]}",
        f"MIN_NET_EDGE_CENTS={cell.min_net_edge_cents}",
        "MAX_TRADES_PER_DAY=6",
    ]
    if cell.only_15m:
        lines.append("MARKET_TYPES=15m")
    return lines
