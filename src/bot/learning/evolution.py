from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime
from typing import Any


EVOLUTION_TARGET_WIN_RATE = 0.68
EVOLUTION_MIN_DECISION_SAMPLE = 50


def record_evolution_event(
    conn: sqlite3.Connection,
    *,
    event_key: str,
    policy_version: str | None,
    event_type: str,
    title: str,
    summary: str,
    occurred_at: str | None = None,
    source: str = "recorded",
    metrics: dict[str, Any] | None = None,
    config_delta: dict[str, Any] | None = None,
    reason: str | None = None,
    evidence_sha256: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO policy_evolution_events (
          event_key, policy_version, event_type, source, occurred_at, title,
          summary, metrics_json, config_delta_json, reason, evidence_sha256
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_key,
            policy_version,
            event_type,
            source,
            occurred_at or datetime.now(UTC).isoformat(),
            title,
            summary,
            json.dumps(metrics, sort_keys=True) if metrics is not None else None,
            json.dumps(config_delta, sort_keys=True) if config_delta is not None else None,
            reason,
            evidence_sha256,
        ),
    )


def config_delta(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    previous = previous or {}
    changed: dict[str, Any] = {}
    for key in sorted(set(previous) | set(current)):
        before = previous.get(key)
        after = current.get(key)
        if before != after:
            changed[key] = {"before": before, "after": after}
    return changed


def record_policy_checkpoints(
    conn: sqlite3.Connection,
    policy_version: str,
    bankroll_usdc: float,
    *,
    interval: int = 10,
) -> None:
    rows = conn.execute(
        """
        SELECT id, status, size_usdc, avg_price, fee_usdc, realized_pnl_usdc,
               COALESCE(settled_at, updated_at) AS occurred_at,
               COALESCE(
                 break_even_probability,
                 avg_price * (1 + COALESCE(fee_usdc, 0) / NULLIF(size_usdc, 0))
               ) AS break_even
        FROM positions
        WHERE policy_version = ? AND status IN ('WON', 'LOST')
        ORDER BY COALESCE(settled_at, updated_at), id
        """,
        (policy_version,),
    ).fetchall()
    for count in range(interval, len(rows) + 1, interval):
        subset = rows[:count]
        metrics = _metrics(subset, bankroll_usdc)
        occurred_at = subset[-1]["occurred_at"]
        record_evolution_event(
            conn,
            event_key=f"checkpoint:{policy_version}:{count}",
            policy_version=policy_version,
            event_type="checkpoint",
            title=f"{count}-trade checkpoint",
            summary=_checkpoint_summary(metrics),
            occurred_at=occurred_at,
            metrics=metrics,
            reason="Automatic cumulative checkpoint from verified settlements.",
        )


def build_evolution_payload(conn: sqlite3.Connection, bankroll_usdc: float) -> dict[str, Any]:
    policy_rows = conn.execute(
        "SELECT * FROM policy_versions ORDER BY COALESCE(activated_at, created_at), version"
    ).fetchall()
    policies = [_policy_summary(conn, row, bankroll_usdc) for row in policy_rows]
    policy_by_version = {item["version"]: item for item in policies}
    active = next((item for item in policies if item["is_active"]), None)

    rows = conn.execute(
        """
        SELECT id, COALESCE(policy_version, 'legacy') AS policy_version, status,
               size_usdc, avg_price, COALESCE(fee_usdc, 0) AS fee_usdc,
               realized_pnl_usdc, break_even_probability,
               COALESCE(settled_at, updated_at) AS occurred_at
        FROM positions
        WHERE status IN ('WON', 'LOST')
        ORDER BY COALESCE(settled_at, updated_at), id
        """
    ).fetchall()
    series = _evolution_series(rows, bankroll_usdc)
    latest_by_policy: dict[str, dict[str, Any]] = {}
    for point in series:
        latest_by_policy[point["policy_version"]] = point
    for version, point in latest_by_policy.items():
        if version in policy_by_version:
            policy_by_version[version]["metrics"] = _point_metrics(point)

    milestones = [_event_dict(row) for row in conn.execute(
        "SELECT * FROM policy_evolution_events ORDER BY occurred_at, id"
    ).fetchall()]
    current_point = latest_by_policy.get(active["version"]) if active else None
    current = {
        "policy_version": active["version"] if active else None,
        "status": active["status"] if active else "none",
        "metrics": _point_metrics(current_point) if current_point else _empty_metrics(),
        "fill_rate": active["fill_rate"] if active else None,
        "sample_state": _sample_state(current_point["trade_number_policy"] if current_point else 0),
    }
    current["plain_summary"] = _plain_summary(current)
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "mode": "paper",
        "target": {
            "objective": "positive_pnl",
            "reference_win_rate": EVOLUTION_TARGET_WIN_RATE,
            "minimum_decision_sample": EVOLUTION_MIN_DECISION_SAMPLE,
            "note": "68% is a reference. Positive expectancy and risk gates decide promotion.",
        },
        "current": current,
        "policies": policies,
        "series": series,
        "milestones": milestones,
    }


def _evolution_series(rows, bankroll_usdc: float) -> list[dict[str, Any]]:
    states: dict[str, dict[str, float | int]] = {}
    global_pnl = 0.0
    output: list[dict[str, Any]] = []
    for global_number, row in enumerate(rows, start=1):
        version = str(row["policy_version"] or "legacy")
        state = states.setdefault(
            version,
            {
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "pnl": 0.0,
                "gross_profit": 0.0,
                "gross_loss": 0.0,
                "break_even_sum": 0.0,
                "break_even_count": 0,
                "equity": bankroll_usdc,
                "peak": bankroll_usdc,
                "max_drawdown": 0.0,
            },
        )
        pnl = float(row["realized_pnl_usdc"] or 0)
        state["trades"] = int(state["trades"]) + 1
        won = row["status"] == "WON"
        state["wins"] = int(state["wins"]) + int(won)
        state["losses"] = int(state["losses"]) + int(not won)
        state["pnl"] = float(state["pnl"]) + pnl
        state["gross_profit"] = float(state["gross_profit"]) + max(0.0, pnl)
        state["gross_loss"] = float(state["gross_loss"]) + abs(min(0.0, pnl))
        break_even = _break_even(row)
        if break_even is not None:
            state["break_even_sum"] = float(state["break_even_sum"]) + break_even
            state["break_even_count"] = int(state["break_even_count"]) + 1
        state["equity"] = float(state["equity"]) + pnl
        state["peak"] = max(float(state["peak"]), float(state["equity"]))
        drawdown = float(state["peak"]) - float(state["equity"])
        state["max_drawdown"] = max(float(state["max_drawdown"]), drawdown)
        global_pnl += pnl
        wins = int(state["wins"])
        trades = int(state["trades"])
        ci_low, ci_high = _wilson_interval(wins, trades)
        gross_loss = float(state["gross_loss"])
        point = {
            "position_id": int(row["id"]),
            "occurred_at": row["occurred_at"],
            "policy_version": version,
            "trade_number_global": global_number,
            "trade_number_policy": trades,
            "outcome": row["status"],
            "entry_price": float(row["avg_price"] or 0),
            "stake_usdc": float(row["size_usdc"] or 0),
            "trade_pnl_usdc": pnl,
            "cumulative_pnl_usdc": float(state["pnl"]),
            "global_pnl_usdc": global_pnl,
            "wins": wins,
            "losses": int(state["losses"]),
            "win_rate": wins / trades,
            "win_rate_ci95_low": ci_low,
            "win_rate_ci95_high": ci_high,
            "breakeven_win_rate": (
                float(state["break_even_sum"]) / int(state["break_even_count"])
                if state["break_even_count"]
                else None
            ),
            "profit_factor": float(state["gross_profit"]) / gross_loss if gross_loss else None,
            "max_drawdown_usdc": float(state["max_drawdown"]),
            "max_drawdown_pct": float(state["max_drawdown"]) / bankroll_usdc if bankroll_usdc > 0 else 0.0,
        }
        point["why_it_matters"] = _point_explanation(point)
        output.append(point)
    return output


def _policy_summary(conn: sqlite3.Connection, row, bankroll_usdc: float) -> dict[str, Any]:
    version = row["version"]
    attempts, filled = conn.execute(
        """
        SELECT COUNT(*), COUNT(DISTINCT CASE WHEN EXISTS (
          SELECT 1 FROM fills f WHERE f.order_id = orders.order_id
        ) THEN order_id END)
        FROM orders WHERE policy_version = ? AND execution_style = 'maker'
        """,
        (version,),
    ).fetchone()
    return {
        "version": version,
        "status": row["status"],
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "activated_at": row["activated_at"],
        "evaluated_at": row["evaluated_at"],
        "rejection_reason": row["rejection_reason"],
        "config": _load_json(row["config_json"], {}),
        "config_sha256": row["config_sha256"],
        "evidence_sha256": row["evidence_sha256"],
        "model_sha256": row["model_sha256"],
        "fill_rate": int(filled or 0) / int(attempts or 0) if attempts else None,
        "metrics": _empty_metrics(),
    }


def _event_dict(row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "event_key": row["event_key"],
        "policy_version": row["policy_version"],
        "event_type": row["event_type"],
        "source": row["source"],
        "occurred_at": row["occurred_at"],
        "title": row["title"],
        "summary": row["summary"],
        "metrics": _load_json(row["metrics_json"], {}),
        "config_delta": _load_json(row["config_delta_json"], {}),
        "reason": row["reason"],
        "evidence_sha256": row["evidence_sha256"],
    }


def _metrics(rows, bankroll_usdc: float) -> dict[str, Any]:
    pnls = [float(row["realized_pnl_usdc"] or 0) for row in rows]
    trades = len(rows)
    wins = sum(1 for row in rows if row["status"] == "WON")
    gross_profit = sum(max(0.0, value) for value in pnls)
    gross_loss = abs(sum(min(0.0, value) for value in pnls))
    break_evens = [float(row["break_even"]) for row in rows if row["break_even"] is not None]
    equity = peak = bankroll_usdc
    max_drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    ci_low, ci_high = _wilson_interval(wins, trades)
    return {
        "trades": trades,
        "wins": wins,
        "losses": trades - wins,
        "win_rate": wins / trades if trades else None,
        "win_rate_ci95_low": ci_low,
        "win_rate_ci95_high": ci_high,
        "breakeven_win_rate": sum(break_evens) / len(break_evens) if break_evens else None,
        "pnl_usdc": sum(pnls),
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "max_drawdown_usdc": max_drawdown,
        "max_drawdown_pct": max_drawdown / bankroll_usdc if bankroll_usdc > 0 else 0.0,
    }


def _point_metrics(point: dict[str, Any] | None) -> dict[str, Any]:
    if point is None:
        return _empty_metrics()
    return {
        "trades": point["trade_number_policy"],
        "wins": point["wins"],
        "losses": point["losses"],
        "win_rate": point["win_rate"],
        "win_rate_ci95_low": point["win_rate_ci95_low"],
        "win_rate_ci95_high": point["win_rate_ci95_high"],
        "breakeven_win_rate": point["breakeven_win_rate"],
        "pnl_usdc": point["cumulative_pnl_usdc"],
        "profit_factor": point["profit_factor"],
        "max_drawdown_usdc": point["max_drawdown_usdc"],
        "max_drawdown_pct": point["max_drawdown_pct"],
    }


def _empty_metrics() -> dict[str, Any]:
    return {
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": None,
        "win_rate_ci95_low": None,
        "win_rate_ci95_high": None,
        "breakeven_win_rate": None,
        "pnl_usdc": 0.0,
        "profit_factor": None,
        "max_drawdown_usdc": 0.0,
        "max_drawdown_pct": 0.0,
    }


def _break_even(row) -> float | None:
    stored = row["break_even_probability"]
    if stored is not None:
        return float(stored)
    size = float(row["size_usdc"] or 0)
    price = float(row["avg_price"] or 0)
    fee = float(row["fee_usdc"] or 0)
    return price * (1 + fee / size) if size > 0 and price > 0 else None


def _wilson_interval(wins: int, trades: int, z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    if trades == 0:
        return None, None
    probability = wins / trades
    denominator = 1 + z * z / trades
    center = (probability + z * z / (2 * trades)) / denominator
    margin = z * math.sqrt((probability * (1 - probability) + z * z / (4 * trades)) / trades) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def _sample_state(trades: int) -> str:
    if trades < 20:
        return "early"
    if trades < EVOLUTION_MIN_DECISION_SAMPLE:
        return "developing"
    return "decision_ready"


def _plain_summary(current: dict[str, Any]) -> str:
    metrics = current["metrics"]
    if metrics["trades"] == 0:
        return "No verified settlements yet. The experiment is collecting evidence."
    wr = float(metrics["win_rate"] or 0)
    be = float(metrics["breakeven_win_rate"] or 0)
    pnl = float(metrics["pnl_usdc"] or 0)
    if current["sample_state"] != "decision_ready":
        return f"Early evidence: {metrics['trades']} settlements. Results can still move sharply."
    if pnl > 0 and wr > be:
        return "The active policy is profitable and winning above its payout-adjusted break-even rate."
    return "The active policy is not yet clearing both profitability and break-even gates."


def _checkpoint_summary(metrics: dict[str, Any]) -> str:
    wr = metrics["win_rate"]
    pnl = float(metrics["pnl_usdc"] or 0)
    return f"WR {wr:.1%}, PnL {pnl:+.4f} USDC, PF {_format_pf(metrics['profit_factor'])}."


def _point_explanation(point: dict[str, Any]) -> str:
    trades = int(point["trade_number_policy"])
    if trades < 20:
        return "Early sample; one result can move the win rate materially."
    wr = float(point["win_rate"] or 0)
    be = float(point["breakeven_win_rate"] or 0)
    if wr <= be:
        return "Win rate is below payout-adjusted break-even, so accuracy alone is not producing positive expectancy."
    if float(point["cumulative_pnl_usdc"] or 0) <= 0:
        return "Win rate clears break-even, but realized payout mix has not produced cumulative profit yet."
    return "Win rate clears break-even and cumulative PnL is positive; more evidence is still required for promotion."


def _format_pf(value: float | None) -> str:
    return "--" if value is None else f"{value:.2f}"


def _load_json(value: str | None, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default
