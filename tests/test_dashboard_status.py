from __future__ import annotations

from datetime import UTC, datetime
import json

from bot import web
from bot.storage.db import force_settle_pending_positions, init_db
from bot.web import _btc_candles_1m, _execution_stats, analytics_payload, force_settlements_payload, health_payload, learning_payload, status_payload, strategies_payload


def test_status_payload_has_dashboard_contract(settings, monkeypatch):
    monkeypatch.chdir(settings.sqlite_path.parent)
    init_db(settings.sqlite_path)

    payload = status_payload()

    assert payload["mode"] == "paper"
    assert payload["policy_mode"] == "unmanaged"
    assert "btc" in payload
    assert "markets" in payload
    assert "performance" in payload
    assert "execution" in payload
    assert "signal_candidates" in payload["execution"]
    assert payload["execution"]["window"] == "24h"
    assert payload["execution"]["target_entries_per_day"] == {"min": 2, "max": 6}
    assert "feed_health" in payload["execution"]
    assert "rag_documents" in payload["counts"]
    assert any(item["name"] == "live trading" and item["ok"] for item in payload["safety"])


def test_health_payload_exposes_policy_and_feed_runtime(settings, monkeypatch):
    monkeypatch.chdir(settings.sqlite_path.parent)
    init_db(settings.sqlite_path)
    now = datetime.now(UTC).isoformat()
    state = {
        "status": "ok",
        "policy_mode": "observe",
        "btc": {
            "age_seconds": 2.5,
            "feed_task_alive": True,
            "feed_reconnects": 4,
            "last_feed_error": "ConnectionError: reset",
        },
    }
    with __import__("sqlite3").connect(settings.sqlite_path) as conn:
        conn.execute(
            "INSERT INTO paper_state (key, value_json, updated_at) VALUES (?, ?, ?)",
            ("paper_loop", json.dumps(state), now),
        )

    payload = health_payload()

    assert payload["schema_version"] == 3
    assert payload["policy_mode"] == "unmanaged"
    assert payload["feed_task_alive"] is True
    assert payload["btc_age_seconds"] == 2.5
    assert payload["feed_reconnects"] == 4
    assert payload["last_feed_error"] == "ConnectionError: reset"


def test_gate_failure_share_uses_only_decisions_with_gate_telemetry(settings):
    init_db(settings.sqlite_path)
    now = datetime.now(UTC).isoformat()
    with __import__("sqlite3").connect(settings.sqlite_path) as raw_conn:
        raw_conn.execute(
            """
            INSERT INTO strategy_decisions
              (market_id, action, confidence, reason, metadata_json, created_at)
            VALUES
              ('legacy', 'HOLD', 0, 'legacy', '{}', ?),
              ('new', 'HOLD', 0, 'new', ?, ?)
            """,
            (now, json.dumps({"failed_gates": ["feed.stale_btc"]}), now),
        )
        raw_conn.row_factory = __import__("sqlite3").Row
        stats = _execution_stats(raw_conn)

    assert stats["decisions"] == 2
    assert stats["gate_telemetry_decisions"] == 1
    assert stats["top_gate_failures"][0] == {"gate": "feed.stale_btc", "count": 1, "share": 1.0}


def test_status_payload_uses_paper_state_btc_fallback(settings, monkeypatch):
    monkeypatch.chdir(settings.sqlite_path.parent)
    init_db(settings.sqlite_path)
    now = datetime.now(UTC).isoformat()
    with __import__("sqlite3").connect(settings.sqlite_path) as conn:
        conn.execute(
            "INSERT INTO paper_state (key, value_json, updated_at) VALUES (?, ?, ?)",
            (
                "paper_loop",
                json.dumps({"status": "no_market", "btc": {"current_price": 60000.0, "price_timestamp": now}}),
                now,
            ),
        )

    payload = status_payload()

    assert payload["btc"]["price"] == 60000.0
    assert payload["btc"]["fresh"] is True
    assert "btc_candles_1m" in payload


def test_btc_ticks_aggregate_into_1m_ohlc(settings, monkeypatch):
    monkeypatch.chdir(settings.sqlite_path.parent)
    init_db(settings.sqlite_path)
    with __import__("sqlite3").connect(settings.sqlite_path) as conn:
        conn.row_factory = __import__("sqlite3").Row
        conn.execute("INSERT INTO btc_ticks (price, created_at) VALUES (?, ?)", (100.0, "2026-07-01T12:00:01+00:00"))
        conn.execute("INSERT INTO btc_ticks (price, created_at) VALUES (?, ?)", (105.0, "2026-07-01T12:00:20+00:00"))
        conn.execute("INSERT INTO btc_ticks (price, created_at) VALUES (?, ?)", (99.0, "2026-07-01T12:00:40+00:00"))
        conn.execute("INSERT INTO btc_ticks (price, created_at) VALUES (?, ?)", (102.0, "2026-07-01T12:01:02+00:00"))

        candles = _btc_candles_1m(conn)

    assert candles[0] == {
        "minute": "2026-07-01T12:00:00+00:00",
        "open": 100.0,
        "high": 105.0,
        "low": 99.0,
        "close": 99.0,
    }
    assert candles[1]["minute"] == "2026-07-01T12:01:00+00:00"
    assert candles[1]["open"] == 102.0


def test_status_payload_prefers_live_paper_state_market(settings, monkeypatch):
    monkeypatch.chdir(settings.sqlite_path.parent)
    init_db(settings.sqlite_path)
    now = datetime.now(UTC).isoformat()
    with __import__("sqlite3").connect(settings.sqlite_path) as conn:
        conn.execute(
            "INSERT INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("old-5m", None, "old expired 5m", "old", "5m", None, "2026-01-01T00:00:00+00:00", 0, 0, 1, "{}"),
        )
        conn.execute(
            "INSERT INTO paper_state (key, value_json, updated_at) VALUES (?, ?, ?)",
            (
                "paper_loop",
                json.dumps(
                    {
                        "status": "ok",
                        "markets": [
                            {
                                "type": "5m",
                                "status": "ok",
                                "market_id": "live-5m",
                                "question": "live current 5m",
                                "seconds_to_close": 120,
                                "up_bid": 0.4,
                                "up_ask": 0.41,
                                "down_bid": 0.59,
                                "down_ask": 0.6,
                            }
                        ],
                    }
                ),
                now,
            ),
        )

    payload = status_payload()

    assert payload["markets"][0]["market_id"] == "live-5m"
    assert payload["markets"][0]["up_bid"] == 0.4


def test_status_payload_calculates_unrealized_paper_pnl(settings, monkeypatch):
    monkeypatch.chdir(settings.sqlite_path.parent)
    init_db(settings.sqlite_path)
    now = datetime.now(UTC).isoformat()
    raw = json.dumps({"outcomes": '["Up", "Down"]', "clobTokenIds": '["up-token", "down-token"]'})
    with __import__("sqlite3").connect(settings.sqlite_path) as conn:
        conn.execute(
            "INSERT INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("m1", None, "Bitcoin Up or Down - 5 minute", "btc-updown-5m-1", "5m", None, None, 100, 10, 1, raw),
        )
        conn.execute(
            "INSERT INTO fills (order_id, market_id, token_id, side, price, size_usdc, pnl_usdc, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("o1", "m1", "up-token", "BUY", 0.5, 1.0, 0.0, now),
        )
        conn.execute(
            "INSERT INTO market_snapshots (market_id, token_id, best_bid, best_ask, spread, liquidity, imbalance, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("m1", "up-token", 0.6, 0.61, 0.01, 100, 0, now),
        )

    payload = status_payload()

    assert round(payload["performance"]["unrealized_pnl_usdc"], 2) == 0.2
    assert payload["performance"]["positions"][0]["side"] == "UP"


def test_status_payload_exposes_paper_wallet_after_fee(settings, monkeypatch):
    monkeypatch.chdir(settings.sqlite_path.parent)
    monkeypatch.setenv("PAPER_BANKROLL_USDC", "100")
    init_db(settings.sqlite_path)
    now = datetime.now(UTC).isoformat()
    raw = json.dumps({"outcomes": '["Up", "Down"]', "clobTokenIds": '["up-token", "down-token"]'})
    with __import__("sqlite3").connect(settings.sqlite_path) as conn:
        conn.execute(
            "INSERT INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("m1", None, "Bitcoin Up or Down - 5 minute", "btc-updown-5m-1", "5m", None, None, 100, 10, 1, raw),
        )
        conn.execute(
            "INSERT INTO fills (order_id, market_id, token_id, side, price, size_usdc, fee_usdc, pnl_usdc, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("o1", "m1", "up-token", "BUY", 0.5, 1.0, 0.035, 0.0, now),
        )
        conn.execute(
            "INSERT INTO market_snapshots (market_id, token_id, best_bid, best_ask, spread, liquidity, imbalance, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("m1", "up-token", 0.5, 0.51, 0.01, 100, 0, now),
        )

    payload = status_payload()

    wallet = payload["performance"]["paper_wallet"]
    assert wallet["initial_cash_usdc"] == 100
    assert round(wallet["available_cash_usdc"], 3) == 98.965
    assert round(wallet["fees_paid_usdc"], 3) == 0.035
    assert round(payload["performance"]["positions"][0]["unrealized_pnl_usdc"], 3) == -0.035


def test_status_payload_marks_expired_paper_positions_pending_settlement(settings, monkeypatch):
    monkeypatch.chdir(settings.sqlite_path.parent)
    init_db(settings.sqlite_path)
    now = datetime.now(UTC).isoformat()
    raw = json.dumps({"outcomes": '["Up", "Down"]', "clobTokenIds": '["up-token", "down-token"]'})
    with __import__("sqlite3").connect(settings.sqlite_path) as conn:
        conn.execute(
            "INSERT INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("expired", None, "Bitcoin Up or Down - 5 minute", "btc-updown-5m-old", "5m", None, "2026-01-01T00:00:00+00:00", 100, 10, 1, raw),
        )
        conn.execute(
            """
            INSERT INTO positions (market_id, token_id, size_usdc, avg_price, shares, status, realized_pnl_usdc, updated_at)
            VALUES (?, ?, ?, ?, ?, 'OPEN', 0, ?)
            """,
            ("expired", "up-token", 1.0, 0.5, 2.0, now),
        )

    payload = status_payload()

    assert payload["performance"]["open_positions_count"] == 0
    assert payload["performance"]["pending_settlement_count"] == 1
    assert payload["performance"]["positions"][0]["status"] == "EXPIRED_UNKNOWN"
    assert payload["performance"]["positions"][0]["current_value_usdc"] is None


def test_status_payload_settles_binary_winning_paper_position(settings, monkeypatch):
    monkeypatch.chdir(settings.sqlite_path.parent)
    init_db(settings.sqlite_path)
    raw = json.dumps(
        {
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["up-token", "down-token"]',
            "outcomePrices": '["0.995", "0.005"]',
        }
    )
    with __import__("sqlite3").connect(settings.sqlite_path) as conn:
        conn.execute(
            "INSERT INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("won-market", None, "Bitcoin Up or Down - 5 minute", "btc-updown-5m-won", "5m", None, "2026-01-01T00:00:00+00:00", 100, 10, 1, raw),
        )
        conn.execute(
            "INSERT INTO fills (order_id, market_id, token_id, side, price, size_usdc, pnl_usdc, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("o1", "won-market", "up-token", "BUY", 0.5, 1.0, 0.0, "2026-01-01T00:00:00+00:00"),
        )
    init_db(settings.sqlite_path)

    payload = status_payload()

    assert payload["performance"]["settled_trades"] == 1
    assert payload["performance"]["wins"] == 1
    assert payload["performance"]["win_rate"] == 1.0
    assert payload["performance"]["positions"][0]["status"] == "WON"
    assert round(payload["performance"]["realized_pnl_usdc"], 2) == 1.0


def test_status_payload_settles_binary_losing_paper_position(settings, monkeypatch):
    monkeypatch.chdir(settings.sqlite_path.parent)
    init_db(settings.sqlite_path)
    raw = json.dumps(
        {
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["up-token", "down-token"]',
            "outcomePrices": '["0.005", "0.995"]',
        }
    )
    with __import__("sqlite3").connect(settings.sqlite_path) as conn:
        conn.execute(
            "INSERT INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("lost-market", None, "Bitcoin Up or Down - 5 minute", "btc-updown-5m-lost", "5m", None, "2026-01-01T00:00:00+00:00", 100, 10, 1, raw),
        )
        conn.execute(
            "INSERT INTO fills (order_id, market_id, token_id, side, price, size_usdc, pnl_usdc, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("o1", "lost-market", "up-token", "BUY", 0.5, 1.0, 0.0, "2026-01-01T00:00:00+00:00"),
        )
    init_db(settings.sqlite_path)

    payload = status_payload()

    assert payload["performance"]["settled_trades"] == 1
    assert payload["performance"]["losses"] == 1
    assert payload["performance"]["win_rate"] == 0.0
    assert payload["performance"]["positions"][0]["status"] == "LOST"
    assert round(payload["performance"]["realized_pnl_usdc"], 2) == -1.0


def test_force_settle_pending_positions_returns_counts(settings, monkeypatch):
    monkeypatch.chdir(settings.sqlite_path.parent)
    init_db(settings.sqlite_path)
    raw = json.dumps(
        {
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["up-token", "down-token"]',
            "outcomePrices": '["0.995", "0.005"]',
        }
    )
    with __import__("sqlite3").connect(settings.sqlite_path) as conn:
        conn.execute(
            "INSERT INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("force-market", None, "Bitcoin Up or Down - 5 minute", "btc-updown-5m-force", "5m", None, "2026-01-01T00:00:00+00:00", 100, 10, 1, raw),
        )
        conn.execute(
            """
            INSERT INTO positions (market_id, token_id, size_usdc, avg_price, shares, status, realized_pnl_usdc, updated_at)
            VALUES (?, ?, ?, ?, ?, 'EXPIRED_UNKNOWN', 0, ?)
            """,
            ("force-market", "up-token", 1.0, 0.5, 2.0, "2026-01-01T00:00:00+00:00"),
        )

    result = force_settle_pending_positions(settings.sqlite_path)

    assert result["pending_before"] == 1
    assert result["pending_after"] == 0
    assert result["settled_now"] == 1


def test_force_settlements_payload_keeps_unverified_pending(settings, monkeypatch):
    monkeypatch.chdir(settings.sqlite_path.parent)
    monkeypatch.setenv("GAMMA_HOST", "http://127.0.0.1:9")
    init_db(settings.sqlite_path)
    raw = json.dumps(
        {
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["up-token", "down-token"]',
            "outcomePrices": '["0.305", "0.695"]',
        }
    )
    with __import__("sqlite3").connect(settings.sqlite_path) as conn:
        conn.execute(
            "INSERT INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("pending-market", None, "Bitcoin Up or Down - 5 minute", "btc-updown-5m-pending", "5m", None, "2026-01-01T00:00:00+00:00", 100, 10, 1, raw),
        )
        conn.execute(
            """
            INSERT INTO positions (market_id, token_id, size_usdc, avg_price, shares, status, realized_pnl_usdc, updated_at)
            VALUES (?, ?, ?, ?, ?, 'EXPIRED_UNKNOWN', 0, ?)
            """,
            ("pending-market", "up-token", 1.0, 0.5, 2.0, "2026-01-01T00:00:00+00:00"),
        )

    payload = force_settlements_payload()

    assert payload["settlement"]["pending_after"] == 1
    assert payload["settlement"]["settled_now"] == 0
    assert payload["refresh_errors"]
    assert payload["pending_details"][0]["held_token_price"] == 0.305
    assert payload["pending_details"][0]["outcome_prices"] == [0.305, 0.695]
    assert "not final yet" in payload["pending_details"][0]["reason"]


def test_strategies_payload_exposes_safe_runtime_contract(settings, monkeypatch):
    monkeypatch.chdir(settings.sqlite_path.parent)
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "should-not-leak")
    monkeypatch.setenv("POLYMARKET_CLOB_API_KEY", "should-not-leak")
    monkeypatch.setenv("POLYMARKET_CLOB_SECRET", "should-not-leak")
    monkeypatch.setenv("POLYMARKET_CLOB_PASSPHRASE", "should-not-leak")
    init_db(settings.sqlite_path)

    payload = strategies_payload()
    serialized = json.dumps(payload).lower()

    assert payload["mode"] == "paper"
    assert "config" in payload
    assert "runtime" in payload
    assert "markets" in payload
    assert "decisions" in payload
    assert "safety" in payload
    assert payload["config"]["paper_trade_size_usdc"] == 1.0
    assert payload["config"]["max_trades_per_market"] == 1
    assert "min_profit_if_win_usdc" in payload["config"]
    assert "min_net_edge_cents" in payload["config"]
    assert "private_key" not in serialized
    assert "clob_secret" not in serialized
    assert "passphrase" not in serialized
    assert "should-not-leak" not in serialized


def test_analytics_payload_exposes_factual_outcomes(settings, monkeypatch):
    monkeypatch.chdir(settings.sqlite_path.parent)
    monkeypatch.setenv("PAPER_BANKROLL_USDC", "100")
    init_db(settings.sqlite_path)
    raw = json.dumps(
        {
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["up-token", "down-token"]',
            "outcomePrices": '["0.995", "0.005"]',
        }
    )
    with __import__("sqlite3").connect(settings.sqlite_path) as conn:
        conn.execute(
            "INSERT INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("won-market", None, "Bitcoin Up or Down - 5 minute", "btc-updown-5m-won", "5m", None, "2026-01-01T00:00:00+00:00", 100, 10, 1, raw),
        )
        conn.execute(
            "INSERT INTO fills (order_id, market_id, token_id, side, price, size_usdc, fee_usdc, pnl_usdc, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("o1", "won-market", "up-token", "BUY", 0.5, 1.0, 0.035, 0.0, "2026-01-01T00:00:00+00:00"),
        )
    init_db(settings.sqlite_path)

    payload = analytics_payload()

    assert payload["outcomes"][0]["status"] == "WON"
    assert payload["outcomes"][0]["fee_usdc"] == 0.035
    assert "paper_wallet" in payload["kpis"]


def test_analytics_payload_loads_positions_once(settings, monkeypatch):
    monkeypatch.chdir(settings.sqlite_path.parent)
    init_db(settings.sqlite_path)
    web._reset_dashboard_runtime_state()
    calls = 0
    original = web._paper_positions

    def counted(conn):
        nonlocal calls
        calls += 1
        return original(conn)

    monkeypatch.setattr(web, "_paper_positions", counted)

    web.analytics_payload()

    assert calls == 1


def test_snapshot_lookup_migration_uses_covering_index(settings):
    init_db(settings.sqlite_path)
    with __import__("sqlite3").connect(settings.sqlite_path) as conn:
        migration = conn.execute(
            "SELECT name FROM schema_migrations WHERE version = ?",
            ("20260714_analytics_snapshot_lookup",),
        ).fetchone()
        plan = conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT best_bid FROM market_snapshots
            WHERE market_id = ? AND token_id = ? AND best_bid IS NOT NULL
            ORDER BY created_at DESC LIMIT 1
            """,
            ("market", "token"),
        ).fetchall()

    assert migration == ("analytics snapshot lookup index",)
    assert any("idx_market_snapshots_market_token_created" in row[3] for row in plan)


def test_analytics_aggregate_migrations_create_covering_indexes(settings):
    init_db(settings.sqlite_path)
    with __import__("sqlite3").connect(settings.sqlite_path) as conn:
        migrations = {
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_migrations WHERE version LIKE ?",
                ("20260714_analytics_%",),
            ).fetchall()
        }
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(strategy_decisions)").fetchall()}
        indexes.update(row[1] for row in conn.execute("PRAGMA index_list(discovery_rejections)").fetchall())

    assert {
        "20260714_analytics_decision_core",
        "20260714_analytics_decision_reason",
        "20260714_analytics_decision_hour",
        "20260714_analytics_rejection_count",
    } <= migrations
    assert {
        "idx_strategy_decisions_analytics_core",
        "idx_strategy_decisions_reason",
        "idx_strategy_decisions_hour_edge",
        "idx_discovery_rejections_count",
    } <= indexes


def test_dashboard_get_payloads_are_read_only(settings, monkeypatch):
    monkeypatch.chdir(settings.sqlite_path.parent)
    init_db(settings.sqlite_path)
    web._reset_dashboard_runtime_state()

    def unexpected_init(_path):
        raise AssertionError("GET payload must not initialize or mutate the database")

    monkeypatch.setattr(web, "init_db", unexpected_init)

    assert status_payload()["mode"] == "paper"
    assert analytics_payload()["kpis"]["decision_count"] == 0
    assert strategies_payload()["mode"] == "paper"
    assert "summary" in learning_payload()
    assert web.evolution_payload()["mode"] == "paper"
