from __future__ import annotations

import json

import pytest

from bot.learning.versions import (
    activate_paper_experiment,
    active_policy,
    activate_candidate,
    apply_active_policy,
    evaluate_and_transition,
    evaluate_policy,
    ensure_baseline_policy,
    policy_metrics,
    predecessor_experiment_ready,
    register_candidate,
    rollback_paper_experiment,
)
from bot.storage.db import connect, init_db


def _candidate(conn, version: str = "v-test") -> None:
    register_candidate(
        conn,
        version,
        {"market_types": ["15m"], "enable_5m_scout": False, "min_entry_price_15m": 0.65},
        {"trades": 25, "pnl_usdc": 1.5, "roi": 0.06, "windows": 3, "profitable_windows": 2},
        evidence_sha256="a" * 64,
    )


def _settled(conn, version: str, count: int, wins: int, win_pnl: float = 0.5, loss_pnl: float = -1.0) -> None:
    for index in range(count):
        status = "WON" if index < wins else "LOST"
        pnl = win_pnl if status == "WON" else loss_pnl
        conn.execute(
            """
            INSERT INTO positions (
              market_id, token_id, size_usdc, avg_price, shares, fee_usdc, status,
              realized_pnl_usdc, policy_version, break_even_probability, settled_at, updated_at
            ) VALUES (?, 'token', 1, 0.65, 1.538, 0.02, ?, ?, ?, 0.67, ?, ?)
            """,
            (f"m-{version}-{index}", status, pnl, version, f"2026-01-01T00:{index % 60:02d}:00+00:00", f"2026-01-01T00:{index % 60:02d}:00+00:00"),
        )
    conn.commit()


def test_candidate_activation_is_paper_only_and_applies_allowlisted_config(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        _candidate(conn)
        decision = activate_candidate(conn, "v-test", settings)
        updated = apply_active_policy(settings, conn)

    assert decision.status == "paper_active"
    assert updated.policy_version == "v-test"
    assert updated.market_types == ["15m"]
    assert updated.enable_live_trading is settings.enable_live_trading


def test_candidate_rejects_live_or_unknown_config(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        with pytest.raises(ValueError, match="unsupported paper policy keys"):
            register_candidate(conn, "unsafe", {"enable_live_trading": True}, {"trades": 20, "pnl_usdc": 1, "roi": 0.1, "windows": 1, "profitable_windows": 1})


def test_candidate_rejects_negative_maker_bid_offset(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        with pytest.raises(ValueError, match="paper_maker_bid_offset_cents"):
            register_candidate(
                conn,
                "unsafe-offset",
                {"paper_order_style": "maker", "paper_maker_bid_offset_cents": -1},
                {"trades": 20, "pnl_usdc": 1, "roi": 0.1, "windows": 1, "profitable_windows": 1},
            )


def test_policy_version_is_immutable(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        _candidate(conn)
        with pytest.raises(ValueError, match="immutable"):
            register_candidate(
                conn,
                "v-test",
                {"market_types": ["5m"]},
                {"trades": 25, "pnl_usdc": 1.5, "roi": 0.06, "windows": 3, "profitable_windows": 2},
            )


def test_activation_requires_positive_oos_evidence(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        register_candidate(
            conn,
            "weak",
            {"market_types": ["15m"]},
            {"trades": 19, "pnl_usdc": 2, "roi": 0.1, "windows": 1, "profitable_windows": 1},
            evidence_sha256="b" * 64,
        )
        decision = activate_candidate(conn, "weak", settings)

    assert decision.status == "candidate"
    assert "OOS gate" in decision.reason


def test_activation_requires_evidence_checksum(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        register_candidate(
            conn,
            "unverified",
            {"market_types": ["15m"]},
            {"trades": 25, "pnl_usdc": 2, "roi": 0.1, "windows": 1, "profitable_windows": 1},
        )
        decision = activate_candidate(conn, "unverified", settings)

    assert decision.status == "candidate"
    assert "checksum" in decision.reason


def test_policy_validates_after_forward_gates(settings):
    settings = settings.model_copy(update={"policy_min_forward_trades": 20, "policy_max_drawdown_pct": 0.15})
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        _candidate(conn)
        _settled(conn, "v-test", 20, 16, win_pnl=0.5, loss_pnl=-0.5)
        decision = evaluate_policy(conn, "v-test", settings)

    assert decision.status == "validated"
    assert decision.metrics["pnl_usdc"] > 0
    assert decision.metrics["profit_factor"] >= 1.1


def test_policy_stops_when_drawdown_exceeds_limit(settings):
    settings = settings.model_copy(update={"policy_min_forward_trades": 200, "policy_max_drawdown_pct": 0.02})
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        _candidate(conn)
        _settled(conn, "v-test", 3, 0)
        decision = evaluate_policy(conn, "v-test", settings)

    assert decision.status == "stopped"
    assert decision.metrics["max_drawdown_pct"] > 0.02


def test_policy_metrics_calculate_peak_to_trough_drawdown():
    rows = [
        {"status": "WON", "size_usdc": 1, "realized_pnl_usdc": 5, "break_even": 0.6},
        {"status": "LOST", "size_usdc": 1, "realized_pnl_usdc": -3, "break_even": 0.6},
        {"status": "LOST", "size_usdc": 1, "realized_pnl_usdc": -2, "break_even": 0.6},
    ]
    metrics = policy_metrics(rows, bankroll_usdc=100)

    assert metrics["max_drawdown_usdc"] == 5
    assert metrics["max_drawdown_pct"] == 0.05
    assert metrics["win_rate_ci95_low"] < metrics["win_rate"] < metrics["win_rate_ci95_high"]


def test_validated_policy_remains_the_effective_active_policy(settings):
    settings = settings.model_copy(update={"policy_min_forward_trades": 20})
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        _candidate(conn)
        activate_candidate(conn, "v-test", settings)
        _settled(conn, "v-test", 20, 16, win_pnl=0.5, loss_pnl=-0.5)

        decision = evaluate_and_transition(conn, "v-test", settings)
        active = active_policy(conn)
        effective = apply_active_policy(settings, conn)

    assert decision.status == "validated"
    assert active["version"] == "v-test"
    assert active["is_active"] is True
    assert effective.policy_version == "v-test"


def test_stopped_policy_is_deactivated(settings):
    settings = settings.model_copy(update={"policy_max_drawdown_pct": 0.01})
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        _candidate(conn)
        activate_candidate(conn, "v-test", settings)
        _settled(conn, "v-test", 3, 0)

        decision = evaluate_and_transition(conn, "v-test", settings)
        active = active_policy(conn)

    assert decision.status == "stopped"
    assert active is None


def test_activating_candidate_supersedes_any_validated_active_policy(settings):
    settings = settings.model_copy(update={"policy_min_forward_trades": 20})
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        _candidate(conn, "v-one")
        activate_candidate(conn, "v-one", settings)
        _settled(conn, "v-one", 20, 16, win_pnl=0.5, loss_pnl=-0.5)
        evaluate_and_transition(conn, "v-one", settings)
        _candidate(conn, "v-two")

        activate_candidate(conn, "v-two", settings)
        rows = conn.execute(
            "SELECT version, status, is_active FROM policy_versions ORDER BY version"
        ).fetchall()

    assert [(row["version"], row["is_active"]) for row in rows] == [("v-one", 0), ("v-two", 1)]
    assert rows[0]["status"] == "validated"


def test_maker_experiment_hard_stop_restores_previous_policy_and_cancels_orders(settings):
    settings = settings.model_copy(update={"paper_bankroll_usdc": 10.0})
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        previous = ensure_baseline_policy(conn, settings)
        register_candidate(
            conn,
            "v4-maker",
            {
                "paper_order_style": "maker",
                "paper_experiment_enabled": True,
                "paper_max_trade_size_usdc": 0.25,
            },
            {"trades": 8, "pnl_usdc": 2.0, "roi": 0.25, "windows": 5, "profitable_windows": 4},
            evidence_sha256="c" * 64,
        )
        activated = activate_paper_experiment(conn, "v4-maker", settings)
        now = "2026-07-21T12:00:00+00:00"
        conn.execute(
            """
            INSERT INTO positions (
              market_id, token_id, size_usdc, avg_price, status, realized_pnl_usdc,
              policy_version, break_even_probability, settled_at, updated_at
            ) VALUES ('loss', 'token', 0.25, 0.60, 'LOST', -0.25, 'v4-maker', 0.60, ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO orders (
              order_id, market_id, token_id, side, status, price, size_usdc,
              execution_style, policy_version, created_at
            ) VALUES ('pending', 'next', 'token', 'BUY', 'OPEN', 0.59, 0.25, 'maker', 'v4-maker', ?)
            """,
            (now,),
        )
        conn.commit()

        decision = evaluate_and_transition(conn, "v4-maker", settings)
        active = active_policy(conn)
        pending = conn.execute("SELECT status FROM orders WHERE order_id='pending'").fetchone()

    assert activated.status == "paper_active"
    assert decision.status == "stopped"
    assert active["version"] == previous["version"]
    assert pending["status"] == "CANCELED"


def test_maker_experiment_applies_bid_offset_and_margin_gates(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        register_candidate(
            conn,
            "v5-margin",
            {
                "paper_order_style": "maker",
                "paper_maker_bid_offset_cents": 1.0,
                "paper_experiment_enabled": True,
                "paper_experiment_min_profit_factor": 1.25,
                "paper_experiment_min_fill_rate": 0.60,
                "min_edge_cents": 2.5,
                "min_net_edge_cents": 2.5,
                "min_net_edge_15m_cents": 2.5,
            },
            {"trades": 10, "pnl_usdc": 0.64, "roi": 0.25, "windows": 6, "profitable_windows": 4},
            evidence_sha256="e" * 64,
        )
        activate_paper_experiment(conn, "v5-margin", settings)
        effective = apply_active_policy(settings, conn)

    assert effective.paper_maker_bid_offset_cents == 1.0
    assert effective.paper_experiment_min_profit_factor == 1.25
    assert effective.paper_experiment_min_fill_rate == 0.60
    assert effective.minimum_net_edge_cents_for("15m") == 2.5


def test_rollback_of_inactive_candidate_does_not_restore_stale_predecessor(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        baseline = ensure_baseline_policy(conn, settings)
        _candidate(conn, "inactive")

        restored = rollback_paper_experiment(conn, "inactive")
        candidate = conn.execute("SELECT status FROM policy_versions WHERE version='inactive'").fetchone()

    assert restored == baseline["version"]
    assert candidate["status"] == "candidate"


def test_successor_waits_until_predecessor_has_twenty_fills_or_stops(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        register_candidate(
            conn,
            "v4-maker",
            {"paper_order_style": "maker", "paper_experiment_enabled": True},
            {"trades": 8, "pnl_usdc": 1, "roi": 0.1, "windows": 2, "profitable_windows": 2},
            evidence_sha256="d" * 64,
        )
        activate_paper_experiment(conn, "v4-maker", settings)
        _settled(conn, "v4-maker", 19, 14, win_pnl=0.15, loss_pnl=-0.25)

        ready, reason = predecessor_experiment_ready(conn, "v4-maker")
        conn.execute(
            """
            INSERT INTO positions (
              market_id, token_id, size_usdc, avg_price, status,
              realized_pnl_usdc, policy_version, settled_at, updated_at
            ) VALUES ('maturity-fill', 'token', 0.25, 0.65, 'WON', 0.13, 'v4-maker', ?, ?)
            """,
            ("2026-01-02T00:00:00+00:00", "2026-01-02T00:00:00+00:00"),
        )
        conn.commit()
        mature_ready, mature_reason = predecessor_experiment_ready(conn, "v4-maker")
        conn.execute("UPDATE policy_versions SET status='stopped', is_active=0 WHERE version='v4-maker'")
        conn.commit()
        stopped_ready, stopped_reason = predecessor_experiment_ready(conn, "v4-maker")

    assert ready is False
    assert "19/20" in reason
    assert mature_ready is True
    assert "20/20" in mature_reason
    assert stopped_ready is True
    assert "stopped" in stopped_reason
