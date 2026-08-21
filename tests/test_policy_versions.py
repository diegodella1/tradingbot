from __future__ import annotations

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
    stop_active_policy,
)
from bot.storage.db import connect, init_db


def _oos_evidence(*, trades: int = 30, pnl_usdc: float = 1.5, roi: float = 0.06) -> dict:
    return {
        "method": "chronological_market_split_80_20_full_gates",
        "test_markets": 30,
        "trades": trades,
        "pnl_usdc": pnl_usdc,
        "roi": roi,
        "profit_factor": 1.5,
        "max_drawdown_pct": 0.02,
        "trades_per_day": 4.0,
        "windows": 3,
        "profitable_windows": 2,
        "model_validation": {
            "test_log_loss": 0.50,
            "market_log_loss": 0.60,
            "test_brier_score": 0.18,
            "market_brier_score": 0.22,
            "beats_market": True,
        },
    }


def _candidate(conn, version: str = "v-test") -> None:
    register_candidate(
        conn,
        version,
        {"market_types": ["15m"], "enable_5m_scout": False, "min_entry_price_15m": 0.65},
        _oos_evidence(),
        evidence_sha256="a" * 64,
        model_sha256="f" * 64,
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
    assert updated.policy_model_sha256 == "f" * 64
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


def test_candidate_rejects_unsafe_numeric_config(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        with pytest.raises(ValueError, match="paper_trade_size_usdc"):
            register_candidate(
                conn,
                "unsafe-size",
                {"paper_trade_size_usdc": -1},
                _oos_evidence(),
                evidence_sha256="a" * 64,
                model_sha256="f" * 64,
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
    assert "OOS trades below 30" in decision.reason


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


def test_activation_waits_for_active_predecessor_to_mature(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        _candidate(conn, "predecessor")
        activate_candidate(conn, "predecessor", settings)
        _settled(conn, "predecessor", 49, 38, win_pnl=0.5, loss_pnl=-0.5)
        _candidate(conn, "successor")

        decision = activate_candidate(conn, "successor", settings)
        active = active_policy(conn)

    assert decision.status == "candidate"
    assert "49/50" in decision.reason
    assert active["version"] == "predecessor"


def test_maker_experiment_hard_stop_enters_no_trade_and_cancels_orders(settings):
    settings = settings.model_copy(update={"paper_bankroll_usdc": 10.0})
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        ensure_baseline_policy(conn, settings)
        register_candidate(
            conn,
            "v4-maker",
            {
                "paper_order_style": "maker",
                "paper_experiment_enabled": True,
                "paper_max_trade_size_usdc": 0.25,
            },
            _oos_evidence(pnl_usdc=2.0, roi=0.25),
            evidence_sha256="c" * 64,
            model_sha256="f" * 64,
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
    assert active is None
    assert pending["status"] == "CANCELED"


def test_manual_policy_stop_enters_no_trade_without_restoring_predecessor(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        _candidate(conn)
        activate_candidate(conn, "v-test", settings)

        stop_active_policy(conn, "v-test", "replace bound model")
        effective = apply_active_policy(settings, conn)
        active = active_policy(conn)

    assert effective.policy_mode == "observe"
    assert active is None


def test_losing_standard_policy_stops_at_interim_gate(settings):
    settings = settings.model_copy(
        update={
            "policy_interim_min_trades": 30,
            "policy_interim_stop_loss_usdc": 3.0,
            "policy_interim_max_drawdown_pct": 0.03,
            "policy_min_forward_trades": 200,
        }
    )
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        _candidate(conn)
        activate_candidate(conn, "v-test", settings)
        _settled(conn, "v-test", 30, 18, win_pnl=0.45, loss_pnl=-0.75)

        decision = evaluate_and_transition(conn, "v-test", settings)
        effective = apply_active_policy(settings, conn)

    assert decision.status == "stopped"
    assert "interim" in decision.reason
    assert effective.policy_mode == "observe"
    assert effective.enable_experimental_strategy is False


def test_empty_policy_registry_keeps_environment_strategy_for_bootstrap(settings):
    init_db(settings.sqlite_path)
    settings.enable_experimental_strategy = True
    with connect(settings.sqlite_path) as conn:
        effective = apply_active_policy(settings, conn)

    assert effective.policy_mode == "unmanaged"
    assert effective.enable_experimental_strategy is True


def test_fresh_baseline_starts_as_observer_candidate(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        baseline = ensure_baseline_policy(conn, settings)
        registered = conn.execute(
            "SELECT status, is_active, activated_at FROM policy_versions WHERE version = ?",
            (settings.policy_version,),
        ).fetchone()
        effective = apply_active_policy(settings, conn)

    assert baseline is None
    assert (registered["status"], registered["is_active"], registered["activated_at"]) == (
        "candidate",
        0,
        None,
    )
    assert effective.policy_mode == "observe"
    assert effective.enable_experimental_strategy is False


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
            _oos_evidence(pnl_usdc=0.64, roi=0.25),
            evidence_sha256="e" * 64,
            model_sha256="f" * 64,
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
        ensure_baseline_policy(conn, settings)
        _candidate(conn, "inactive")

        restored = rollback_paper_experiment(conn, "inactive")
        candidate = conn.execute("SELECT status FROM policy_versions WHERE version='inactive'").fetchone()

    assert restored is None
    assert candidate["status"] == "candidate"


def test_activation_requires_exact_configured_approved_model(settings):
    settings = settings.model_copy(update={"require_approved_probability_model": True})
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        _candidate(conn)
        decision = activate_candidate(conn, "v-test", settings)

    assert decision.status == "candidate"
    assert "configured probability model is missing or invalid" in decision.reason


def test_successor_waits_until_predecessor_has_fifty_fills_or_stops(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        register_candidate(
            conn,
            "v4-maker",
            {"paper_order_style": "maker", "paper_experiment_enabled": True},
            _oos_evidence(pnl_usdc=1, roi=0.1),
            evidence_sha256="d" * 64,
            model_sha256="f" * 64,
        )
        activate_paper_experiment(conn, "v4-maker", settings)
        _settled(conn, "v4-maker", 49, 37, win_pnl=0.15, loss_pnl=-0.25)

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
    assert "49/50" in reason
    assert mature_ready is True
    assert "50/50" in mature_reason
    assert stopped_ready is True
    assert "stopped" in stopped_reason
