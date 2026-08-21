from __future__ import annotations

import bisect
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from bot.polymarket.gamma import convert_gamma_market
from bot.polymarket.models import OutcomeSide
from bot.storage.db import _verified_winner_token_id


@dataclass
class TrainingRow:
    """One (decision snapshot, side) pair labeled with the verified market outcome.

    Unlike the recorded BUY signals, this dataset covers every decision cycle
    (including HOLDs) and BOTH sides of each market, removing the selection
    bias of training only on trades the old gates happened to take. The
    implied price per side comes from the order-book snapshot nearest in time
    to the decision.
    """

    features: list[float]
    label: int
    epoch: float
    created_at: str
    market_id: str
    market_type: str
    side: str
    ask: float
    seconds_to_close: float


def _epoch(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _resolved_markets(conn: sqlite3.Connection) -> dict[str, dict]:
    """Markets with a verified winner, mapped to their UP/DOWN token ids."""
    resolved: dict[str, dict] = {}
    for row in conn.execute("SELECT market_id, market_type, raw_json FROM markets").fetchall():
        raw = row["raw_json"]
        try:
            market = convert_gamma_market(json.loads(raw)) if raw else None
        except (TypeError, json.JSONDecodeError):
            market = None
        if market is None:
            continue
        up = market.tokens.get(OutcomeSide.UP)
        down = market.tokens.get(OutcomeSide.DOWN)
        if up is None or down is None:
            continue
        winner_token = _verified_winner_token_id(raw)
        if winner_token == up.token_id:
            winner = "UP"
        elif winner_token == down.token_id:
            winner = "DOWN"
        else:
            continue
        resolved[row["market_id"]] = {
            "market_type": row["market_type"] or "unknown",
            "tokens": {"UP": up.token_id, "DOWN": down.token_id},
            "winner": winner,
        }
    return resolved


def _snapshot_index(conn: sqlite3.Connection) -> dict[str, list[tuple[float, float, float]]]:
    """Per-token snapshots as (epoch, best_ask, imbalance), sorted by time."""
    index: dict[str, list[tuple[float, float, float]]] = {}
    rows = conn.execute("SELECT token_id, best_ask, imbalance, created_at FROM market_snapshots").fetchall()
    for row in rows:
        timestamp = _epoch(row["created_at"])
        if timestamp is None or row["best_ask"] is None:
            continue
        index.setdefault(row["token_id"], []).append((timestamp, float(row["best_ask"]), float(row["imbalance"] or 0.0)))
    for entries in index.values():
        entries.sort()
    return index


def _prior_snapshot(
    index: dict[str, list[tuple[float, float, float]]],
    token_id: str,
    timestamp: float,
    max_gap_seconds: float,
) -> tuple[float, float] | None:
    """Latest known (best_ask, imbalance) at or before `timestamp`, or None."""
    entries = index.get(token_id)
    if not entries:
        return None
    position = bisect.bisect_right(entries, (timestamp, float("inf"), float("inf"))) - 1
    if position < 0:
        return None
    snapshot = entries[position]
    if timestamp - snapshot[0] > max_gap_seconds:
        return None
    return snapshot[1], snapshot[2]


def build_training_rows(
    conn: sqlite3.Connection,
    snapshot_max_gap_seconds: float = 30.0,
    sample_bucket_seconds: int = 15,
) -> list[TrainingRow]:
    """Build a market-safe dataset with one observation per side/time bucket."""
    from bot.strategy.calibration import build_features

    markets = _resolved_markets(conn)
    snapshots = _snapshot_index(conn)
    rows_by_bucket: dict[tuple[str, str, int], TrainingRow] = {}
    decisions = conn.execute(
        "SELECT market_id, metadata_json, created_at FROM strategy_decisions ORDER BY created_at ASC"
    ).fetchall()
    for decision in decisions:
        market = markets.get(decision["market_id"])
        if market is None:
            continue
        try:
            metadata = json.loads(decision["metadata_json"] or "{}")
        except json.JSONDecodeError:
            continue
        features = metadata.get("features") or {}
        if not features:
            continue
        timestamp = _epoch(decision["created_at"])
        if timestamp is None:
            continue
        seconds_to_close = float(features.get("seconds_to_close") or 0.0)
        for side, sign in (("UP", 1), ("DOWN", -1)):
            snapshot = _prior_snapshot(snapshots, market["tokens"][side], timestamp, snapshot_max_gap_seconds)
            if snapshot is None:
                continue
            ask, imbalance = snapshot
            if not 0.01 <= ask <= 0.99:
                continue
            row = TrainingRow(
                features=build_features(
                    momentum_15s=float(features.get("momentum_15s") or 0.0),
                    momentum_60s=float(features.get("momentum_60s") or 0.0),
                    change_since_open=float(features.get("change_since_open") or 0.0),
                    realized_volatility=float(features.get("realized_volatility") or 0.0),
                    book_imbalance=imbalance,
                    implied=ask,
                    sign=sign,
                    seconds_to_close=seconds_to_close,
                ),
                label=1 if market["winner"] == side else 0,
                epoch=timestamp,
                created_at=str(decision["created_at"]),
                market_id=decision["market_id"],
                market_type=market["market_type"],
                side=side,
                ask=ask,
                seconds_to_close=seconds_to_close,
            )
            bucket = int(timestamp // max(1, sample_bucket_seconds))
            key = (row.market_id, row.side, bucket)
            previous = rows_by_bucket.get(key)
            if previous is None or row.epoch < previous.epoch:
                rows_by_bucket[key] = row
    rows = list(rows_by_bucket.values())
    rows.sort(key=lambda row: row.epoch)
    return rows
