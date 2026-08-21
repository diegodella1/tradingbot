from __future__ import annotations

from bot.learning.evolution import build_evolution_payload
from bot.learning.versions import (
    activate_candidate,
    activate_paper_experiment,
    auto_promote_best_candidate,
    evaluate_and_transition,
    register_candidate,
)
from bot.storage.db import connect, init_db
from bot import web


def _evidence(*, pnl_usdc: float = 2.0, roi: float = 0.08) -> dict:
    return {
        "method": "chronological_market_split_80_20_full_gates",
        "test_markets": 30,
        "trades": 50,
        "pnl_usdc": pnl_usdc,
        "roi": roi,
        "profit_factor": 1.5,
        "max_drawdown_pct": 0.02,
        "trades_per_day": 4.0,
        "windows": 5,
        "profitable_windows": 4,
        "model_validation": {
            "test_log_loss": 0.50,
            "market_log_loss": 0.60,
            "test_brier_score": 0.18,
            "market_brier_score": 0.22,
            "beats_market": True,
        },
    }


def _activate(conn, settings, version: str = "v-evolution") -> None:
    register_candidate(
        conn,
        version,
        {"market_types": ["15m"], "min_entry_price_15m": 0.55},
        _evidence(),
        evidence_sha256="e" * 64,
        model_sha256="f" * 64,
    )
    assert activate_candidate(conn, version, settings).status == "paper_active"


def _position(conn, version: str, index: int, status: str, pnl: float, price: float = 0.60) -> None:
    at = f"2026-08-01T00:{index:02d}:00+00:00"
    conn.execute(
        """
        INSERT INTO positions (
          market_id, token_id, size_usdc, avg_price, shares, fee_usdc, status,
          realized_pnl_usdc, policy_version, break_even_probability, settled_at, updated_at
        ) VALUES (?, ?, 0.25, ?, 0.4167, 0, ?, ?, ?, ?, ?, ?)
        """,
        (f"m-{version}-{index}", f"t-{index}", price, status, pnl, version, price, at, at),
    )


def test_evolution_series_is_chronological_and_has_no_future_leakage(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        _activate(conn, settings)
        _position(conn, "v-evolution", 1, "WON", 0.1667)
        _position(conn, "v-evolution", 2, "LOST", -0.25)
        conn.commit()

        payload = build_evolution_payload(conn, settings.paper_bankroll_usdc)

    first, second = payload["series"]
    assert first["trade_number_policy"] == 1
    assert first["win_rate"] == 1.0
    assert round(first["cumulative_pnl_usdc"], 4) == 0.1667
    assert second["trade_number_policy"] == 2
    assert second["win_rate"] == 0.5
    assert round(second["cumulative_pnl_usdc"], 4) == -0.0833
    assert payload["target"]["reference_win_rate"] == 0.68
    assert payload["current"]["sample_state"] == "early"


def test_policy_evolution_records_activation_and_exact_ten_trade_checkpoint(settings):
    settings = settings.model_copy(update={"policy_min_forward_trades": 200})
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        _activate(conn, settings)
        for index in range(10):
            status = "WON" if index < 7 else "LOST"
            _position(conn, "v-evolution", index, status, 0.1667 if status == "WON" else -0.25)
        conn.commit()

        evaluate_and_transition(conn, "v-evolution", settings)
        events = conn.execute(
            "SELECT event_type, source, metrics_json FROM policy_evolution_events ORDER BY id"
        ).fetchall()

    assert any(row["event_type"] == "registered" and row["source"] == "recorded" for row in events)
    assert any(row["event_type"] == "activated" and row["source"] == "recorded" for row in events)
    checkpoint = next(row for row in events if row["event_type"] == "checkpoint")
    assert '"trades": 10' in checkpoint["metrics_json"]
    assert '"win_rate": 0.7' in checkpoint["metrics_json"]


def test_evolution_api_contract_is_read_only(settings, monkeypatch):
    monkeypatch.chdir(settings.sqlite_path.parent)
    init_db(settings.sqlite_path)
    web._reset_dashboard_runtime_state()
    with connect(settings.sqlite_path) as conn:
        _activate(conn, settings)

    def unexpected_init(_path):
        raise AssertionError("GET evolution must not initialize or mutate the database")

    monkeypatch.setattr(web, "init_db", unexpected_init)
    payload = web.evolution_payload()

    assert payload["schema_version"] == web.API_SCHEMA_VERSION
    assert payload["mode"] == "paper"
    assert payload["current"]["policy_version"] == "v-evolution"
    assert payload["series"] == []
    assert payload["milestones"]


def test_policy_backfill_migration_is_idempotent(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO policy_versions (
              version, status, is_active, config_json, created_at, activated_at, evaluated_at, rejection_reason
            ) VALUES ('legacy-v4', 'stopped', 0, '{}', '2026-01-01', '2026-01-02', '2026-01-03', 'failed gates')
            """
        )
        conn.execute("DELETE FROM schema_migrations WHERE version='20260804_policy_evolution_backfill'")
        conn.commit()

    init_db(settings.sqlite_path)
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        events = conn.execute(
            "SELECT event_type, source FROM policy_evolution_events WHERE policy_version='legacy-v4'"
        ).fetchall()

    assert {(row["event_type"], row["source"]) for row in events} == {
        ("registered", "reconstructed"),
        ("activated", "reconstructed"),
        ("stopped", "reconstructed"),
    }


def test_experiment_cannot_be_auto_superseded_before_fifty_settlements(settings):
    settings = settings.model_copy(update={"paper_auto_promote": True})
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        register_candidate(
            conn,
            "v5-champion",
            {"paper_order_style": "maker", "paper_experiment_enabled": True},
            _evidence(pnl_usdc=1, roi=0.1),
            evidence_sha256="a" * 64,
            model_sha256="f" * 64,
        )
        assert activate_paper_experiment(conn, "v5-champion", settings).status == "paper_active"
        register_candidate(
            conn,
            "v6-candidate",
            {"market_types": ["15m"]},
            _evidence(roi=0.1),
            evidence_sha256="b" * 64,
            model_sha256="f" * 64,
        )

        decision = auto_promote_best_candidate(conn, settings)
        active = conn.execute("SELECT version FROM policy_versions WHERE is_active=1").fetchone()[0]

    assert decision is None
    assert active == "v5-champion"
