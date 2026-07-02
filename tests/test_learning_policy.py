from __future__ import annotations

import json
from datetime import UTC, datetime

from bot.learning.policy import generate_learning_report, persist_learning_recommendations
from bot.storage.db import connect, init_db
from bot.web import learning_payload


def _market(conn, market_id: str, market_type: str = "5m") -> None:
    raw = json.dumps({"outcomes": '["Up", "Down"]', "clobTokenIds": '["up-token", "down-token"]'})
    conn.execute(
        "INSERT INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (market_id, None, f"Bitcoin Up or Down {market_type}", f"btc-updown-{market_type}-{market_id}", market_type, None, "2026-01-01T00:00:00+00:00", 100, 10, 1, raw),
    )


def _position(conn, market_id: str, status: str, price: float, pnl: float, token_id: str = "up-token", size: float = 1.0) -> None:
    conn.execute(
        """
        INSERT INTO positions (market_id, token_id, size_usdc, avg_price, shares, fee_usdc, status, realized_pnl_usdc, updated_at)
        VALUES (?, ?, ?, ?, ?, 0.03, ?, ?, '2026-01-01T00:00:00+00:00')
        """,
        (market_id, token_id, size, price, size / price, status, pnl),
    )


def test_learning_policy_observes_when_sample_is_small(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        _market(conn, "m1")
        _position(conn, "m1", "LOST", 0.2, -1.03)
        report = generate_learning_report(conn, settings)

    assert report["mode"] == "recommend_only"
    assert report["summary"]["sample_size"] == 1
    assert report["recommendations"][0]["status"] == "observe"
    assert "minimum" in report["recommendations"][0]["rationale"]


def test_learning_policy_flags_losing_price_buckets_and_duplicate_markets(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        for index in range(20):
            market_id = f"m{index}"
            market_type = "5m" if index < 12 else "15m"
            _market(conn, market_id, market_type)
            if index < 10:
                _position(conn, market_id, "LOST", 0.2, -1.03)
            else:
                _position(conn, market_id, "WON", 0.55, 0.78)
        _position(conn, "m0", "LOST", 0.2, -1.03, token_id="down-token")

        report = generate_learning_report(conn, settings)

    scopes = {item["scope"] for item in report["recommendations"]}
    assert "price_bucket:0.00-0.25" in scopes
    assert "risk:market_exposure" in scopes
    assert all("suggested_config_json" in item for item in report["recommendations"])


def test_learning_recommendations_can_be_persisted(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        _market(conn, "m1")
        _position(conn, "m1", "LOST", 0.2, -1.03)
        report = generate_learning_report(conn, settings)
        inserted = persist_learning_recommendations(conn, report["recommendations"])
        count = conn.execute("SELECT COUNT(*) FROM learning_recommendations").fetchone()[0]

    assert inserted == len(report["recommendations"])
    assert count == inserted


def test_learning_policy_flags_stale_risk_state(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        conn.execute(
            "INSERT INTO risk_events (market_id, approved, reason, created_at) VALUES (?, 0, ?, ?)",
            ("m1", "one open position limit hit", datetime.now(UTC).isoformat()),
        )
        report = generate_learning_report(conn, settings)

    assert report["risk_state"]["stale_block_count"] == 1
    assert report["recommendations"][0]["metric"] == "risk_state_stale"


def test_learning_payload_does_not_leak_secrets(settings, monkeypatch):
    monkeypatch.chdir(settings.sqlite_path.parent)
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "should-not-leak")
    init_db(settings.sqlite_path)

    payload = learning_payload()
    serialized = json.dumps(payload)

    assert payload["mode"] == "recommend_only"
    assert "should-not-leak" not in serialized
