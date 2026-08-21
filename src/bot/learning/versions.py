from __future__ import annotations

import json
import hashlib
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from bot.config import Settings
from bot.learning.evolution import (
    EVOLUTION_MIN_DECISION_SAMPLE,
    config_delta,
    record_evolution_event,
    record_policy_checkpoints,
)


PAPER_CONFIG_KEYS = {
    "enable_experimental_strategy",
    "market_types",
    "enable_5m_scout",
    "min_confidence",
    "min_estimated_probability",
    "min_probability_15m",
    "min_probability_5m",
    "min_entry_price",
    "min_entry_price_15m",
    "max_entry_price",
    "min_edge_cents",
    "min_net_edge_cents",
    "min_net_edge_15m_cents",
    "min_net_edge_5m_cents",
    "min_seconds_to_close",
    "min_seconds_to_close_5m",
    "min_seconds_to_close_15m",
    "max_trades_per_hour",
    "max_trades_per_day",
    "paper_trade_size_usdc",
    "paper_order_style",
    "paper_maker_fill_window_seconds",
    "paper_maker_bid_offset_cents",
    "paper_max_trade_size_usdc",
    "paper_experiment_enabled",
    "paper_experiment_stop_loss_usdc",
    "paper_experiment_max_drawdown_pct",
    "paper_experiment_min_fills",
    "paper_experiment_min_profit_factor",
    "paper_experiment_min_fill_rate",
    "max_daily_loss_usdc",
    "max_consecutive_losses",
    "min_book_imbalance",
    "min_kelly_size_usdc",
    "max_trade_pct_15m",
}

CHALLENGER_MIN_OOS_TRADES = 30
CHALLENGER_MIN_TEST_MARKETS = 30
CHALLENGER_MIN_PROFIT_FACTOR = 1.25
CHALLENGER_MAX_DRAWDOWN_PCT = 0.03
CHALLENGER_MIN_TRADES_PER_DAY = 2.0
CHALLENGER_MAX_TRADES_PER_DAY = 6.0


@dataclass(frozen=True)
class PromotionDecision:
    status: str
    reason: str
    metrics: dict[str, Any]


def register_candidate(
    conn: sqlite3.Connection,
    version: str,
    config: dict[str, Any],
    oos_metrics: dict[str, Any] | None = None,
    *,
    evidence_sha256: str | None = None,
    model_sha256: str | None = None,
) -> None:
    clean = _validated_config(config)
    clean_oos = _validated_oos_metrics(oos_metrics)
    _validate_optional_sha256(evidence_sha256, "evidence_sha256")
    _validate_optional_sha256(model_sha256, "model_sha256")
    now = datetime.now(UTC).isoformat()
    existing = conn.execute(
        """
        SELECT config_json, oos_metrics_json, evidence_sha256, model_sha256
        FROM policy_versions WHERE version = ?
        """,
        (version,),
    ).fetchone()
    config_json = json.dumps(clean, sort_keys=True)
    oos_json = json.dumps(clean_oos, sort_keys=True) if clean_oos else None
    config_sha256 = hashlib.sha256(config_json.encode("utf-8")).hexdigest()
    if existing:
        if (
            existing["config_json"] != config_json
            or existing["oos_metrics_json"] != oos_json
            or existing["evidence_sha256"] != evidence_sha256
            or existing["model_sha256"] != model_sha256
        ):
            raise ValueError(f"policy version is immutable: {version}")
        return
    conn.execute(
        """
        INSERT INTO policy_versions (
            version, status, is_active, config_json, config_sha256,
            oos_metrics_json, evidence_sha256, model_sha256, created_at
        )
        VALUES (?, 'candidate', 0, ?, ?, ?, ?, ?, ?)
        """,
        (version, config_json, config_sha256, oos_json, evidence_sha256, model_sha256, now),
    )
    record_evolution_event(
        conn,
        event_key=f"policy:{version}:registered",
        policy_version=version,
        event_type="registered",
        title="Policy registered",
        summary="Immutable paper candidate registered with out-of-sample evidence.",
        occurred_at=now,
        config_delta=config_delta(None, clean),
        evidence_sha256=evidence_sha256,
    )
    conn.commit()


def active_policy(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM policy_versions WHERE is_active = 1 ORDER BY activated_at DESC LIMIT 1"
    ).fetchone()
    return _policy_dict(row) if row else None


def apply_active_policy(settings: Settings, conn: sqlite3.Connection) -> Settings:
    policy = active_policy(conn)
    if policy is None:
        has_registry = conn.execute("SELECT 1 FROM policy_versions LIMIT 1").fetchone() is not None
        if has_registry:
            return settings.model_copy(
                update={
                    "enable_experimental_strategy": False,
                    "policy_mode": "observe",
                }
            )
        return settings.model_copy(update={"policy_mode": "unmanaged"})
    config = _validated_config(policy["config"])
    config["policy_version"] = policy["version"]
    config["policy_model_sha256"] = policy["model_sha256"]
    config["policy_mode"] = "active"
    return settings.model_copy(update=config)


def evaluate_policy(conn: sqlite3.Connection, version: str, settings: Settings) -> PromotionDecision:
    rows = conn.execute(
        """
        SELECT status, size_usdc, avg_price, fee_usdc, realized_pnl_usdc,
               COALESCE(break_even_probability, avg_price * (1 + fee_usdc / NULLIF(size_usdc, 0))) AS break_even
        FROM positions
        WHERE policy_version = ? AND status IN ('WON', 'LOST')
        ORDER BY settled_at, id
        """,
        (version,),
    ).fetchall()
    metrics = policy_metrics(rows, settings.paper_bankroll_usdc)
    policy_row = conn.execute("SELECT config_json FROM policy_versions WHERE version = ?", (version,)).fetchone()
    config = json.loads(policy_row["config_json"] or "{}") if policy_row else {}
    if config.get("paper_experiment_enabled"):
        maker = conn.execute(
            """
            SELECT COUNT(*) AS attempts,
                   COUNT(DISTINCT CASE WHEN EXISTS (
                       SELECT 1 FROM fills f WHERE f.order_id = orders.order_id
                   ) THEN order_id END) AS filled
            FROM orders WHERE policy_version = ? AND execution_style = 'maker'
            """,
            (version,),
        ).fetchone()
        attempts = int(maker["attempts"] or 0)
        filled = int(maker["filled"] or 0)
        metrics["maker_attempts"] = attempts
        metrics["maker_filled_orders"] = filled
        metrics["maker_fill_rate"] = filled / attempts if attempts else None
        hard_stop = (
            metrics["pnl_usdc"] <= -abs(settings.paper_experiment_stop_loss_usdc)
            or metrics["max_drawdown_pct"] >= settings.paper_experiment_max_drawdown_pct
        )
        mature_failures = []
        if metrics["trades"] >= settings.paper_experiment_min_fills:
            if metrics["pnl_usdc"] <= 0:
                mature_failures.append("cumulative PnL is not positive")
            if float(metrics["profit_factor"] or 0) < settings.paper_experiment_min_profit_factor:
                mature_failures.append("profit factor below experiment floor")
            if metrics["win_rate"] is None or metrics["breakeven_win_rate"] is None or metrics["win_rate"] <= metrics["breakeven_win_rate"]:
                mature_failures.append("win rate does not beat break-even")
            if float(metrics["maker_fill_rate"] or 0) < settings.paper_experiment_min_fill_rate:
                mature_failures.append("maker fill rate below experiment floor")
        if hard_stop or mature_failures:
            reason = "; ".join(mature_failures) or "experiment hard loss/drawdown stop"
            return PromotionDecision(status="stopped", reason=reason, metrics=metrics)
        return PromotionDecision(
            status="paper_active",
            reason=f"collecting maker experiment fills {metrics['trades']}/{settings.paper_experiment_min_fills}",
            metrics=metrics,
        )
    reasons = _failed_gates(metrics, settings)
    interim_failures = _interim_failures(metrics, settings)
    if interim_failures:
        status = "stopped"
        reasons = interim_failures
    elif metrics["max_drawdown_pct"] > settings.policy_max_drawdown_pct:
        status = "stopped"
    elif metrics["trades"] < settings.policy_min_forward_trades:
        status = "paper_active"
    elif reasons:
        status = "rejected"
    else:
        status = "validated"
    reason = "; ".join(reasons) if reasons else ("collecting forward settlements" if status == "paper_active" else "all promotion gates passed")
    return PromotionDecision(status=status, reason=reason, metrics=metrics)


def evaluate_and_transition(conn: sqlite3.Connection, version: str, settings: Settings) -> PromotionDecision:
    previous_row = conn.execute(
        "SELECT status FROM policy_versions WHERE version = ?", (version,)
    ).fetchone()
    decision = evaluate_policy(conn, version, settings)
    now = datetime.now(UTC).isoformat()
    record_policy_checkpoints(conn, version, settings.paper_bankroll_usdc)
    conn.execute(
        """
        UPDATE policy_versions
        SET status = ?, is_active = CASE WHEN ? IN ('rejected', 'stopped') THEN 0 ELSE is_active END,
            rejection_reason = ?, evaluated_at = ?,
            activated_at = CASE WHEN ? = 'paper_active' THEN COALESCE(activated_at, ?) ELSE activated_at END
        WHERE version = ?
        """,
        (
            decision.status,
            decision.status,
            None if decision.status in {"paper_active", "validated"} else decision.reason,
            now,
            decision.status,
            now,
            version,
        ),
    )
    conn.commit()
    previous_status = previous_row["status"] if previous_row else None
    if decision.status != previous_status and decision.status in {"stopped", "rejected", "validated"}:
        record_evolution_event(
            conn,
            event_key=f"policy:{version}:{decision.status}",
            policy_version=version,
            event_type=decision.status,
            title=f"Policy {decision.status}",
            summary=decision.reason,
            occurred_at=now,
            metrics=decision.metrics,
            reason=decision.reason,
        )
        conn.commit()
    if decision.status in {"stopped", "rejected"}:
        _enter_no_trade(conn, version, decision.reason)
    return decision


def _enter_no_trade(conn: sqlite3.Connection, stopped_version: str, reason: str) -> None:
    """Cancel pending paper orders and leave registry without an active policy."""
    now = datetime.now(UTC).isoformat()
    with conn:
        conn.execute(
            "UPDATE orders SET status='CANCELED', reason='policy stopped; no-trade', updated_at=? "
            "WHERE policy_version=? AND execution_style='maker' AND status IN ('OPEN','PARTIALLY_FILLED')",
            (now, stopped_version),
        )
        record_evolution_event(
            conn,
            event_key=f"policy:{stopped_version}:no-trade",
            policy_version=stopped_version,
            event_type="no_trade_entered",
            title="Paper entries suspended",
            summary="No active policy remains; data collection continues without entries.",
            occurred_at=now,
            reason=reason,
        )


def activate_paper_experiment(conn: sqlite3.Connection, version: str, settings: Settings) -> PromotionDecision:
    """Explicitly activate a paper-only maker challenger after evidence gates pass."""
    row = conn.execute(
        "SELECT status, is_active, config_json, oos_metrics_json, evidence_sha256, model_sha256 FROM policy_versions WHERE version = ?",
        (version,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown policy version: {version}")
    config = json.loads(row["config_json"] or "{}")
    if row["status"] == "paper_active" and bool(row["is_active"]):
        return PromotionDecision("paper_active", "paper-only maker experiment already active", json.loads(row["oos_metrics_json"] or "{}"))
    if row["status"] != "candidate":
        raise ValueError(f"policy {version} is {row['status']}, not candidate")
    if settings.enable_live_trading:
        return PromotionDecision("candidate", "paper experiment requires live trading disabled", {})
    if settings.policy_require_evidence_hash and not row["evidence_sha256"]:
        return PromotionDecision("candidate", "OOS evidence checksum is required", {})
    if config.get("paper_order_style") != "maker" or not config.get("paper_experiment_enabled"):
        return PromotionDecision("candidate", "explicit maker experiment config is required", {})
    oos = json.loads(row["oos_metrics_json"] or "{}")
    failures = _challenger_failures(oos, row["model_sha256"])
    failures.extend(_configured_model_failures(settings, row["model_sha256"]))
    if failures:
        return PromotionDecision("candidate", "; ".join(failures), oos)
    previous = active_policy(conn)
    if previous is not None and previous["version"] != version:
        ready, reason = predecessor_experiment_ready(conn, previous["version"])
        if not ready:
            return PromotionDecision("candidate", reason, oos)
    now = datetime.now(UTC).isoformat()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO paper_state (key, value_json, updated_at) VALUES ('paper_experiment_previous_policy', ?, ?)",
            (json.dumps({"version": previous["version"] if previous else None}), now),
        )
        conn.execute("UPDATE policy_versions SET is_active = 0 WHERE is_active = 1")
        conn.execute(
            "UPDATE policy_versions SET status='paper_active', is_active=1, activated_at=?, rejection_reason=NULL WHERE version=?",
            (now, version),
        )
        record_evolution_event(
            conn,
            event_key=f"policy:{version}:activated",
            policy_version=version,
            event_type="activated",
            title="Paper experiment activated",
            summary="Guarded maker experiment became the active paper policy.",
            occurred_at=now,
            config_delta=config_delta(previous["config"] if previous else None, config),
            evidence_sha256=row["evidence_sha256"],
        )
    return PromotionDecision("paper_active", "explicit paper-only maker experiment activated", json.loads(row["oos_metrics_json"] or "{}"))


def predecessor_experiment_ready(
    conn: sqlite3.Connection,
    version: str,
    *,
    min_fills: int = 50,
) -> tuple[bool, str]:
    """Allow a successor only after its predecessor matures or reaches a terminal state."""
    row = conn.execute("SELECT status FROM policy_versions WHERE version = ?", (version,)).fetchone()
    if row is None:
        return False, f"predecessor policy not found: {version}"
    trades = int(
        conn.execute(
            "SELECT COUNT(*) FROM positions WHERE policy_version = ? AND status IN ('WON', 'LOST')",
            (version,),
        ).fetchone()[0]
    )
    if row["status"] in {"stopped", "rejected", "validated"}:
        return True, f"predecessor {version} is {row['status']} with {trades} fills"
    if trades >= min_fills:
        return True, f"predecessor {version} matured with {trades}/{min_fills} fills"
    return False, f"predecessor {version} still collecting fills {trades}/{min_fills}"


def _restore_previous_policy(conn: sqlite3.Connection, stopped_version: str) -> None:
    state = conn.execute("SELECT value_json FROM paper_state WHERE key = 'paper_experiment_previous_policy'").fetchone()
    if not state:
        return
    previous = json.loads(state["value_json"] or "{}").get("version")
    if not previous or previous == stopped_version:
        return
    with conn:
        conn.execute(
            "UPDATE policy_versions SET is_active=1, status=CASE WHEN status='stopped' THEN 'paper_active' ELSE status END WHERE version=?",
            (previous,),
        )
        conn.execute(
            "UPDATE orders SET status='CANCELED', reason='experiment stopped; rollback', updated_at=? "
            "WHERE policy_version=? AND execution_style='maker' AND status IN ('OPEN','PARTIALLY_FILLED')",
            (datetime.now(UTC).isoformat(), stopped_version),
        )
        restored_at = datetime.now(UTC).isoformat()
        record_evolution_event(
            conn,
            event_key=f"policy:{stopped_version}:rollback:{previous}",
            policy_version=stopped_version,
            event_type="rollback",
            title="Experiment rolled back",
            summary=f"Pending maker orders canceled and {previous} restored.",
            occurred_at=restored_at,
            reason="Automatic safety rollback after experiment stop.",
        )


def rollback_paper_experiment(conn: sqlite3.Connection, version: str, reason: str = "manual rollback") -> str | None:
    target = conn.execute("SELECT is_active FROM policy_versions WHERE version = ?", (version,)).fetchone()
    if target is None:
        raise ValueError(f"unknown policy version: {version}")
    if not bool(target["is_active"]):
        active = active_policy(conn)
        return active["version"] if active else None
    now = datetime.now(UTC).isoformat()
    with conn:
        conn.execute(
            "UPDATE policy_versions SET status='stopped', is_active=0, rejection_reason=?, evaluated_at=? WHERE version=?",
            (reason, now, version),
        )
        record_evolution_event(
            conn,
            event_key=f"policy:{version}:manual-stop",
            policy_version=version,
            event_type="stopped",
            title="Experiment stopped manually",
            summary=reason,
            occurred_at=now,
            reason=reason,
        )
    _restore_previous_policy(conn, version)
    active = active_policy(conn)
    return active["version"] if active else None


def stop_active_policy(
    conn: sqlite3.Connection,
    version: str,
    reason: str = "manual fail-closed stop",
) -> None:
    """Explicitly stop one active policy without restoring any predecessor."""
    target = conn.execute(
        "SELECT is_active FROM policy_versions WHERE version = ?",
        (version,),
    ).fetchone()
    if target is None:
        raise ValueError(f"unknown policy version: {version}")
    if not bool(target["is_active"]):
        raise ValueError(f"policy {version} is not active")
    now = datetime.now(UTC).isoformat()
    with conn:
        conn.execute(
            "UPDATE policy_versions SET status='stopped', is_active=0, rejection_reason=?, evaluated_at=? WHERE version=?",
            (reason, now, version),
        )
        record_evolution_event(
            conn,
            event_key=f"policy:{version}:manual-no-trade",
            policy_version=version,
            event_type="stopped",
            title="Policy stopped manually",
            summary=reason,
            occurred_at=now,
            reason=reason,
        )
    _enter_no_trade(conn, version, reason)


def activate_candidate(conn: sqlite3.Connection, version: str, settings: Settings) -> PromotionDecision:
    row = conn.execute(
        "SELECT status, oos_metrics_json, evidence_sha256, model_sha256 FROM policy_versions WHERE version = ?",
        (version,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown policy version: {version}")
    if row["status"] != "candidate":
        raise ValueError(f"policy {version} is {row['status']}, not candidate")
    if settings.policy_require_evidence_hash and not row["evidence_sha256"]:
        return PromotionDecision("candidate", "OOS evidence checksum is required", {})
    oos = json.loads(row["oos_metrics_json"] or "{}")
    failures = _challenger_failures(oos, row["model_sha256"])
    failures.extend(_configured_model_failures(settings, row["model_sha256"]))
    if failures:
        return PromotionDecision("candidate", "; ".join(failures), oos)
    previous = active_policy(conn)
    if previous is not None and previous["version"] != version:
        ready, reason = predecessor_experiment_ready(conn, previous["version"])
        if not ready:
            return PromotionDecision("candidate", reason, oos)
    now = datetime.now(UTC).isoformat()
    with conn:
        conn.execute(
            """
            UPDATE policy_versions
            SET is_active = 0,
                status = CASE WHEN status = 'paper_active' THEN 'stopped' ELSE status END,
                rejection_reason = CASE
                    WHEN status = 'paper_active' THEN 'superseded by a newer paper policy'
                    ELSE rejection_reason
                END,
                evaluated_at = ?
            WHERE is_active = 1
            """,
            (now,),
        )
        conn.execute(
            """
            UPDATE policy_versions
            SET status='paper_active', is_active=1, activated_at=?, rejection_reason=NULL
            WHERE version=?
            """,
            (now, version),
        )
        config_row = conn.execute(
            "SELECT config_json, evidence_sha256 FROM policy_versions WHERE version = ?", (version,)
        ).fetchone()
        config = json.loads(config_row["config_json"] or "{}")
        record_evolution_event(
            conn,
            event_key=f"policy:{version}:activated",
            policy_version=version,
            event_type="activated",
            title="Paper policy activated",
            summary="OOS gates passed and candidate became active in paper.",
            occurred_at=now,
            config_delta=config_delta(previous["config"] if previous else None, config),
            evidence_sha256=config_row["evidence_sha256"],
        )
    return PromotionDecision("paper_active", "OOS gates passed; activated in paper only", oos)


def auto_promote_best_candidate(conn: sqlite3.Connection, settings: Settings) -> PromotionDecision | None:
    if not settings.paper_auto_promote:
        return None
    champion = active_policy(conn)
    if champion and champion["config"].get("paper_experiment_enabled"):
        champion_settlements = int(
            conn.execute(
                "SELECT COUNT(*) FROM positions WHERE policy_version = ? AND status IN ('WON','LOST')",
                (champion["version"],),
            ).fetchone()[0]
        )
        if champion_settlements < EVOLUTION_MIN_DECISION_SAMPLE:
            return None
    candidates = [item for item in list_policies(conn) if item["status"] == "candidate"]
    eligible = [item for item in candidates if not _challenger_failures(item["oos_metrics"], item["model_sha256"])]
    if not eligible:
        return None
    best = max(eligible, key=lambda item: (float(item["oos_metrics"].get("pnl_usdc") or 0), int(item["oos_metrics"].get("trades") or 0)))
    return activate_candidate(conn, best["version"], settings)


def ensure_baseline_policy(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any] | None:
    """Register the configured baseline without trusting it on a fresh database."""
    active = active_policy(conn)
    if active is not None:
        return active
    existing = conn.execute("SELECT * FROM policy_versions WHERE version = ?", (settings.policy_version,)).fetchone()
    if existing is not None:
        if existing["status"] in {"paper_active", "validated"}:
            conn.execute("UPDATE policy_versions SET is_active = 1 WHERE version = ?", (settings.policy_version,))
            conn.commit()
            return active_policy(conn)
        return None
    config = {
        key: value
        for key, value in settings.strategy_config_snapshot().items()
        if key in PAPER_CONFIG_KEYS
    }
    config_json = json.dumps(config, sort_keys=True)
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO policy_versions (
            version, status, is_active, config_json, config_sha256, created_at, activated_at
        ) VALUES (?, 'candidate', 0, ?, ?, ?, NULL)
        """,
        (
            settings.policy_version,
            config_json,
            hashlib.sha256(config_json.encode("utf-8")).hexdigest(),
            now,
        ),
    )
    record_evolution_event(
        conn,
        event_key=f"policy:{settings.policy_version}:registered",
        policy_version=settings.policy_version,
        event_type="registered",
        title="Baseline policy registered",
        summary="Configured baseline registered as an inactive observer candidate.",
        occurred_at=now,
        config_delta=config_delta(None, config),
    )
    conn.commit()
    return None


def list_policies(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM policy_versions ORDER BY created_at DESC").fetchall()
    return [_policy_dict(row) for row in rows]


def policy_metrics(rows, bankroll_usdc: float) -> dict[str, Any]:
    trades = len(rows)
    pnls = [float(row["realized_pnl_usdc"] or 0) for row in rows]
    volume = sum(float(row["size_usdc"] or 0) for row in rows)
    pnl = sum(pnls)
    wins = sum(1 for row in rows if row["status"] == "WON")
    gross_profit = sum(max(0.0, value) for value in pnls)
    gross_loss = abs(sum(min(0.0, value) for value in pnls))
    break_evens = [float(row["break_even"]) for row in rows if row["break_even"] is not None]
    win_rate = wins / trades if trades else None
    win_rate_ci = _wilson_interval(wins, trades)
    equity = peak = bankroll_usdc
    max_drawdown = 0.0
    for value in pnls:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "trades": trades,
        "wins": wins,
        "win_rate": win_rate,
        "win_rate_ci95_low": win_rate_ci[0],
        "win_rate_ci95_high": win_rate_ci[1],
        "breakeven_win_rate": sum(break_evens) / len(break_evens) if break_evens else None,
        "pnl_usdc": pnl,
        "volume_usdc": volume,
        "roi": pnl / volume if volume else None,
        "expectancy_usdc": pnl / trades if trades else None,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "max_drawdown_usdc": max_drawdown,
        "max_drawdown_pct": max_drawdown / bankroll_usdc if bankroll_usdc > 0 else 0.0,
    }


def _wilson_interval(wins: int, trades: int, z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    if trades == 0:
        return None, None
    probability = wins / trades
    denominator = 1 + (z * z / trades)
    center = (probability + z * z / (2 * trades)) / denominator
    margin = z * math.sqrt((probability * (1 - probability) + z * z / (4 * trades)) / trades) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def _failed_gates(metrics: dict[str, Any], settings: Settings) -> list[str]:
    reasons = []
    if metrics["trades"] < settings.policy_min_forward_trades:
        reasons.append(f"forward sample {metrics['trades']}/{settings.policy_min_forward_trades}")
        return reasons
    for key in ("pnl_usdc", "roi", "expectancy_usdc"):
        if float(metrics[key] or 0) <= 0:
            reasons.append(f"{key} is not positive")
    if float(metrics["profit_factor"] or 0) < settings.policy_min_profit_factor:
        reasons.append(f"profit factor below {settings.policy_min_profit_factor:.2f}")
    if metrics["win_rate"] is None or metrics["breakeven_win_rate"] is None or metrics["win_rate"] <= metrics["breakeven_win_rate"]:
        reasons.append("win rate does not beat fee-adjusted break-even")
    if metrics["max_drawdown_pct"] > settings.policy_max_drawdown_pct:
        reasons.append(f"drawdown exceeds {settings.policy_max_drawdown_pct:.0%}")
    return reasons


def _interim_failures(metrics: dict[str, Any], settings: Settings) -> list[str]:
    trades = int(metrics["trades"] or 0)
    pnl = float(metrics["pnl_usdc"] or 0)
    drawdown = float(metrics["max_drawdown_pct"] or 0)
    if pnl <= -abs(settings.policy_interim_stop_loss_usdc):
        return [f"interim hard stop: PnL {pnl:+.2f} USDC"]
    if drawdown >= settings.policy_interim_max_drawdown_pct:
        return [f"interim hard stop: drawdown {drawdown:.1%}"]
    if trades < settings.policy_interim_min_trades:
        return []
    win_rate = metrics["win_rate"]
    break_even = metrics["breakeven_win_rate"]
    under_break_even = win_rate is None or break_even is None or win_rate <= break_even
    if pnl < 0 and float(metrics["profit_factor"] or 0) < 1 and under_break_even:
        return ["interim gate failed: negative PnL, profit factor below 1, and win rate below break-even"]
    return []


def _validated_config(config: dict[str, Any]) -> dict[str, Any]:
    unknown = set(config) - PAPER_CONFIG_KEYS
    if unknown:
        raise ValueError(f"unsupported paper policy keys: {', '.join(sorted(unknown))}")
    try:
        validated = Settings.model_validate(config)
    except ValidationError as exc:
        raise ValueError(f"invalid paper policy config: {exc}") from exc
    clean = {key: getattr(validated, key) for key in config}
    maker_offset = float(clean.get("paper_maker_bid_offset_cents") or 0)
    if not 0 <= maker_offset <= 99 or not math.isfinite(maker_offset):
        raise ValueError("paper_maker_bid_offset_cents must be between 0 and 99")
    unit_interval_keys = {
        "min_confidence",
        "min_estimated_probability",
        "min_probability_15m",
        "min_probability_5m",
        "min_entry_price",
        "min_entry_price_15m",
        "max_entry_price",
        "paper_experiment_max_drawdown_pct",
        "paper_experiment_min_fill_rate",
        "max_trade_pct_15m",
        "min_book_imbalance",
    }
    for key in unit_interval_keys & clean.keys():
        value = float(clean[key])
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"{key} must be between 0 and 1")
    nonnegative_keys = {
        "min_edge_cents",
        "min_net_edge_cents",
        "min_net_edge_15m_cents",
        "min_net_edge_5m_cents",
        "paper_trade_size_usdc",
        "paper_max_trade_size_usdc",
        "paper_experiment_stop_loss_usdc",
        "paper_experiment_min_profit_factor",
        "max_daily_loss_usdc",
        "min_kelly_size_usdc",
    }
    for key in nonnegative_keys & clean.keys():
        value = float(clean[key])
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{key} must be non-negative")
    positive_integer_keys = {
        "max_trades_per_hour",
        "max_trades_per_day",
        "paper_maker_fill_window_seconds",
        "paper_experiment_min_fills",
        "min_seconds_to_close",
        "min_seconds_to_close_5m",
        "min_seconds_to_close_15m",
        "max_consecutive_losses",
    }
    for key in positive_integer_keys & clean.keys():
        if clean[key] is not None and int(clean[key]) < 1:
            raise ValueError(f"{key} must be at least 1")
    if "market_types" in clean and not clean["market_types"]:
        raise ValueError("market_types cannot be empty")
    if "market_types" in clean and not set(clean["market_types"]) <= {"5m", "15m"}:
        raise ValueError("market_types must contain only 5m and/or 15m")
    min_entry = float(clean.get("min_entry_price", validated.min_entry_price))
    max_entry = float(clean.get("max_entry_price", validated.max_entry_price))
    if min_entry > max_entry:
        raise ValueError("min_entry_price cannot exceed max_entry_price")
    return clean


def _validated_oos_metrics(metrics: dict[str, Any] | None) -> dict[str, Any] | None:
    if metrics is None:
        return None
    required = {"trades", "pnl_usdc", "roi", "windows", "profitable_windows"}
    missing = required - set(metrics)
    if missing:
        raise ValueError(f"missing OOS metrics: {', '.join(sorted(missing))}")
    clean = {
        "trades": int(metrics["trades"]),
        "pnl_usdc": float(metrics["pnl_usdc"]),
        "roi": float(metrics["roi"]),
        "windows": int(metrics["windows"]),
        "profitable_windows": int(metrics["profitable_windows"]),
    }
    if clean["trades"] < 0 or clean["windows"] < 1:
        raise ValueError("OOS trades must be non-negative and windows must be positive")
    if not 0 <= clean["profitable_windows"] <= clean["windows"]:
        raise ValueError("OOS profitable_windows must be between zero and windows")
    if not all(math.isfinite(clean[key]) for key in ("pnl_usdc", "roi")):
        raise ValueError("OOS PnL and ROI must be finite")
    for key in ("profit_factor", "max_drawdown_pct", "trades_per_day"):
        if key in metrics and metrics[key] is not None:
            clean[key] = float(metrics[key])
            if not math.isfinite(clean[key]):
                raise ValueError(f"OOS {key} must be finite")
    for key in ("test_markets", "validation_markets", "train_markets"):
        if key in metrics and metrics[key] is not None:
            clean[key] = int(metrics[key])
            if clean[key] < 0:
                raise ValueError(f"OOS {key} must be non-negative")
    if "method" in metrics:
        clean["method"] = str(metrics["method"])
    if isinstance(metrics.get("selection_metrics"), dict):
        clean["selection_metrics"] = dict(metrics["selection_metrics"])
    validation = metrics.get("model_validation")
    if validation is not None:
        if not isinstance(validation, dict):
            raise ValueError("OOS model_validation must be an object")
        required_validation = {
            "test_log_loss",
            "market_log_loss",
            "test_brier_score",
            "market_brier_score",
            "beats_market",
        }
        missing_validation = required_validation - set(validation)
        if missing_validation:
            raise ValueError(f"missing OOS model validation: {', '.join(sorted(missing_validation))}")
        clean_validation = {
            key: float(validation[key])
            for key in required_validation - {"beats_market"}
        }
        if not all(math.isfinite(value) for value in clean_validation.values()):
            raise ValueError("OOS model validation metrics must be finite")
        clean_validation["beats_market"] = bool(validation["beats_market"])
        clean["model_validation"] = clean_validation
    return clean


def _challenger_failures(oos: dict[str, Any], model_sha256: str | None) -> list[str]:
    failures: list[str] = []
    windows = int(oos.get("windows") or 0)
    profitable_windows = int(oos.get("profitable_windows") or 0)
    if int(oos.get("trades") or 0) < CHALLENGER_MIN_OOS_TRADES:
        failures.append(f"OOS trades below {CHALLENGER_MIN_OOS_TRADES}")
    if float(oos.get("pnl_usdc") or 0) <= 0 or float(oos.get("roi") or 0) <= 0:
        failures.append("OOS PnL and ROI must be positive")
    if float(oos.get("profit_factor") or 0) < CHALLENGER_MIN_PROFIT_FACTOR:
        failures.append(f"OOS profit factor below {CHALLENGER_MIN_PROFIT_FACTOR:.2f}")
    drawdown = float(oos["max_drawdown_pct"]) if oos.get("max_drawdown_pct") is not None else math.inf
    if drawdown > CHALLENGER_MAX_DRAWDOWN_PCT:
        failures.append(f"OOS drawdown exceeds {CHALLENGER_MAX_DRAWDOWN_PCT:.0%}")
    frequency = float(oos.get("trades_per_day") or 0)
    if not CHALLENGER_MIN_TRADES_PER_DAY <= frequency <= CHALLENGER_MAX_TRADES_PER_DAY:
        failures.append(
            f"OOS frequency must be {CHALLENGER_MIN_TRADES_PER_DAY:g}-{CHALLENGER_MAX_TRADES_PER_DAY:g}/day"
        )
    if windows <= 0 or profitable_windows <= windows / 2:
        failures.append("OOS requires a majority of profitable windows")
    if int(oos.get("test_markets") or 0) < CHALLENGER_MIN_TEST_MARKETS:
        failures.append(f"OOS test markets below {CHALLENGER_MIN_TEST_MARKETS}")
    validation = oos.get("model_validation") or {}
    test_log_loss = float(validation["test_log_loss"]) if validation.get("test_log_loss") is not None else math.inf
    market_log_loss = float(validation["market_log_loss"]) if validation.get("market_log_loss") is not None else -math.inf
    test_brier = float(validation["test_brier_score"]) if validation.get("test_brier_score") is not None else math.inf
    market_brier = float(validation["market_brier_score"]) if validation.get("market_brier_score") is not None else -math.inf
    model_beats_market = (
        bool(validation.get("beats_market"))
        and test_log_loss < market_log_loss
        and test_brier < market_brier
    )
    if not model_beats_market:
        failures.append("probability model does not beat the market OOS")
    if not model_sha256:
        failures.append("approved probability model checksum is required")
    return failures


def _configured_model_failures(settings: Settings, expected_sha256: str | None) -> list[str]:
    """Require activation to bind the policy to the exact approved runtime model."""
    if not settings.require_approved_probability_model:
        return []

    from bot.strategy.calibration import ProbabilityModel

    try:
        model_bytes = settings.probability_model_path.read_bytes()
        model = ProbabilityModel.from_json(model_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
        return ["configured probability model is missing or invalid"]
    if not model.is_trade_approved():
        return ["configured probability model is diagnostic-only"]
    actual_sha256 = hashlib.sha256(model_bytes).hexdigest()
    if not expected_sha256 or actual_sha256 != expected_sha256:
        return ["candidate model checksum does not match the configured runtime model"]
    return []


def _validate_optional_sha256(value: str | None, name: str) -> None:
    if value is None:
        return
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value.lower()):
        raise ValueError(f"{name} must be a 64-character hexadecimal SHA-256")


def _policy_dict(row) -> dict[str, Any]:
    return {
        "version": row["version"],
        "status": row["status"],
        "is_active": bool(row["is_active"]),
        "config": json.loads(row["config_json"]),
        "config_sha256": row["config_sha256"],
        "oos_metrics": json.loads(row["oos_metrics_json"] or "{}"),
        "rejection_reason": row["rejection_reason"],
        "evidence_sha256": row["evidence_sha256"],
        "model_sha256": row["model_sha256"],
        "created_at": row["created_at"],
        "activated_at": row["activated_at"],
        "evaluated_at": row["evaluated_at"],
    }
