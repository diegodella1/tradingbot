from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from bot.config import Settings
from bot.execution.paper_broker import polymarket_taker_fee_usdc
from bot.polymarket.gamma import convert_gamma_market
from bot.polymarket.models import OutcomeSide, SignalAction
from bot.storage.db import _verified_winner_token_id

MARKET_TYPES = ("5m", "15m")


@dataclass
class BacktestSample:
    """A single historical entry signal joined to its verified market outcome.

    Backtesting works on stored ``strategy_decisions`` (which record every input
    feature and the strategy's estimated probability) joined to the verified
    winner from ``markets.raw_json``. This avoids re-running ``Strategy.evaluate``
    against ``datetime.now``-coupled models and order-book depth that snapshots do
    not retain, while still measuring true signal quality hold-to-resolution.
    """

    market_id: str
    market_type: str
    action: str
    entry_price: float
    estimated_probability: float | None
    edge: float | None
    net_edge: float | None
    confidence: float | None
    recommended_size_usdc: float
    won: bool | None
    created_at: str = ""
    features: dict = field(default_factory=dict)


def _load_json(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _opt_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _action_side(action: str) -> OutcomeSide | None:
    if action == SignalAction.BUY_UP.value:
        return OutcomeSide.UP
    if action == SignalAction.BUY_DOWN.value:
        return OutcomeSide.DOWN
    return None


def _market_outcomes(conn: sqlite3.Connection) -> dict[str, dict]:
    outcomes: dict[str, dict] = {}
    for row in conn.execute("SELECT market_id, market_type, raw_json FROM markets").fetchall():
        raw = row["raw_json"]
        winner_side: OutcomeSide | None = None
        try:
            market = convert_gamma_market(json.loads(raw)) if raw else None
        except (TypeError, json.JSONDecodeError):
            market = None
        if market:
            winner_token = _verified_winner_token_id(raw)
            if winner_token:
                for side, token in market.tokens.items():
                    if token.token_id == winner_token:
                        winner_side = side
        outcomes[row["market_id"]] = {"market_type": row["market_type"], "winner_side": winner_side}
    return outcomes


def load_samples(conn: sqlite3.Connection) -> list[BacktestSample]:
    outcomes = _market_outcomes(conn)
    samples: list[BacktestSample] = []
    rows = conn.execute(
        """
        SELECT market_id, market_type, action, estimated_probability, market_price,
               edge, confidence, recommended_size_usdc, metadata_json, created_at
        FROM strategy_decisions
        WHERE action IN ('BUY_UP', 'BUY_DOWN')
        ORDER BY created_at ASC
        """
    ).fetchall()
    for row in rows:
        entry_price = _opt_float(row["market_price"])
        if not entry_price or entry_price <= 0:
            continue
        side = _action_side(row["action"])
        info = outcomes.get(row["market_id"], {})
        winner_side = info.get("winner_side")
        won = None if winner_side is None or side is None else (winner_side == side)
        metadata = _load_json(row["metadata_json"])
        samples.append(
            BacktestSample(
                market_id=row["market_id"],
                market_type=row["market_type"] or info.get("market_type") or "unknown",
                action=row["action"],
                entry_price=entry_price,
                estimated_probability=_opt_float(row["estimated_probability"]),
                edge=_opt_float(row["edge"]),
                net_edge=_opt_float(metadata.get("net_edge")),
                confidence=_opt_float(row["confidence"]),
                recommended_size_usdc=float(row["recommended_size_usdc"] or 0),
                won=won,
                created_at=str(row["created_at"] or ""),
                features=metadata.get("features") or {},
            )
        )
    return samples


def sample_pnl(sample: BacktestSample, fee_rate: float, enable_fees: bool, size_usdc: float = 1.0) -> tuple[float, float]:
    """Realized hold-to-resolution PnL and fee for a resolved sample (0.0 if unresolved)."""
    price = sample.entry_price
    if sample.won is None or price <= 0:
        return 0.0, 0.0
    shares = size_usdc / price
    fee = polymarket_taker_fee_usdc(shares, price, fee_rate) if enable_fees else 0.0
    pnl = (shares - size_usdc - fee) if sample.won else -(size_usdc + fee)
    return pnl, fee


def _avg(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return sum(present) / len(present) if present else None


def _summary(items: list[BacktestSample], settings: Settings, size_usdc: float) -> dict:
    resolved = [sample for sample in items if sample.won is not None]
    wins = sum(1 for sample in resolved if sample.won)
    pnl = 0.0
    fees = 0.0
    for sample in resolved:
        sample_result, sample_fee = sample_pnl(sample, settings.paper_taker_fee_rate, settings.paper_enable_fees, size_usdc)
        pnl += sample_result
        fees += sample_fee
    cost = len(resolved) * size_usdc
    avg_edge = _avg([sample.edge for sample in items])
    return {
        "signals": len(items),
        "resolved": len(resolved),
        "unresolved": len(items) - len(resolved),
        "wins": wins,
        "losses": len(resolved) - wins,
        "win_rate": (wins / len(resolved)) if resolved else None,
        "pnl_usdc": pnl,
        "fees_usdc": fees,
        "roi": (pnl / cost) if cost > 0 else None,
        "avg_edge_cents": (avg_edge * 100) if avg_edge is not None else None,
        "avg_confidence": _avg([sample.confidence for sample in items]),
        "avg_estimated_probability": _avg([sample.estimated_probability for sample in items]),
    }


def summarize(samples: list[BacktestSample], settings: Settings, size_usdc: float = 1.0) -> dict[str, dict]:
    groups: dict[str, list[BacktestSample]] = {"all": list(samples)}
    for market_type in MARKET_TYPES:
        groups[market_type] = [sample for sample in samples if sample.market_type == market_type]
    return {name: _summary(items, settings, size_usdc) for name, items in groups.items()}


def calibration(samples: list[BacktestSample], bins: int = 10) -> list[dict]:
    resolved = [sample for sample in samples if sample.won is not None and sample.estimated_probability is not None]
    buckets: list[dict] = []
    for index in range(bins):
        low = index / bins
        high = (index + 1) / bins
        group = [
            sample
            for sample in resolved
            if low <= sample.estimated_probability < high or (index == bins - 1 and sample.estimated_probability == 1.0)  # type: ignore[operator]
        ]
        predicted = _avg([sample.estimated_probability for sample in group]) if group else None
        actual = (sum(1 for sample in group if sample.won) / len(group)) if group else None
        buckets.append(
            {
                "bin_low": round(low, 3),
                "bin_high": round(high, 3),
                "count": len(group),
                "predicted": predicted,
                "actual": actual,
            }
        )
    return buckets


def _bucket_table(
    resolved: list[BacktestSample],
    keyfn,
    settings: Settings,
    size_usdc: float,
) -> list[dict]:
    groups: dict[str, list[BacktestSample]] = {}
    for sample in resolved:
        groups.setdefault(keyfn(sample), []).append(sample)
    table = []
    for key in sorted(groups):
        items = groups[key]
        wins = sum(1 for sample in items if sample.won)
        pnl = sum(sample_pnl(sample, settings.paper_taker_fee_rate, settings.paper_enable_fees, size_usdc)[0] for sample in items)
        table.append({"bucket": key, "n": len(items), "wins": wins, "win_rate": wins / len(items), "pnl_usdc": pnl})
    return table


def _seconds_bucket(sample: BacktestSample) -> str:
    seconds = sample.features.get("seconds_to_close")
    if seconds is None:
        return "sin dato"
    for limit in (60, 120, 180, 300, 600):
        if float(seconds) < limit:
            return f"<{limit}s"
    return ">=600s"


def bucket_breakdown(samples: list[BacktestSample], settings: Settings, size_usdc: float = 1.0) -> dict[str, list[dict]]:
    """WR/PnL by actionable dimensions, to show where the strategy wins and loses."""
    resolved = [sample for sample in samples if sample.won is not None]
    return {
        "entry_price": _bucket_table(resolved, lambda s: f"{int(s.entry_price * 10) / 10:.1f}", settings, size_usdc),
        "seconds_to_close": _bucket_table(resolved, _seconds_bucket, settings, size_usdc),
        "market_type": _bucket_table(resolved, lambda s: s.market_type, settings, size_usdc),
        "estimated_probability": _bucket_table(
            resolved, lambda s: f"{int((s.estimated_probability or 0) * 10) / 10:.1f}", settings, size_usdc
        ),
    }


def run_backtest(conn: sqlite3.Connection, settings: Settings, size_usdc: float = 1.0) -> dict:
    samples = load_samples(conn)
    return {
        "sample_count": len(samples),
        "resolved_count": sum(1 for sample in samples if sample.won is not None),
        "size_usdc": size_usdc,
        "summary": summarize(samples, settings, size_usdc),
        "calibration": calibration(samples),
    }
