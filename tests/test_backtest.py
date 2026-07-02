from __future__ import annotations

import json

from bot.backtest import load_samples, run_backtest, summarize
from bot.storage.db import connect, init_db


def _market_raw(market_id: str, up_price: str, down_price: str) -> str:
    return json.dumps(
        {
            "conditionId": market_id,
            "question": "Bitcoin Up or Down - 5 minute",
            "slug": "btc-updown-5m-1750000000",
            "outcomes": json.dumps(["Up", "Down"]),
            "clobTokenIds": json.dumps(["up-token", "down-token"]),
            "outcomePrices": json.dumps([up_price, down_price]),
            "active": False,
            "closed": True,
            "resolved": True,
        }
    )


def _insert_market(conn, market_id: str, up_price: str, down_price: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (market_id, None, "Bitcoin Up or Down - 5 minute", "btc-updown-5m-1750000000", "5m", None, None, 100, 10, 1, _market_raw(market_id, up_price, down_price)),
    )


def _insert_decision(conn, market_id: str, action: str, prob: float, price: float, created_at: str) -> None:
    conn.execute(
        """
        INSERT INTO strategy_decisions (
          market_id, market_type, action, estimated_probability, market_price, edge,
          ev_usdc, kelly_fraction, recommended_size_usdc, confidence, reason, metadata_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            market_id,
            "5m",
            action,
            prob,
            price,
            prob - price,
            0.0,
            0.1,
            1.0,
            0.8,
            "test",
            json.dumps({"net_edge": 0.03, "features": {"momentum_15s": 0.001}}),
            created_at,
        ),
    )


def test_load_samples_resolves_outcomes(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        _insert_market(conn, "m1", "1", "0")  # UP wins
        _insert_market(conn, "m2", "1", "0")  # UP wins
        _insert_market(conn, "m3", "0.5", "0.5")  # unresolved
        _insert_decision(conn, "m1", "BUY_UP", 0.6, 0.5, "2026-01-01T00:00:00+00:00")
        _insert_decision(conn, "m2", "BUY_DOWN", 0.6, 0.5, "2026-01-01T00:01:00+00:00")
        _insert_decision(conn, "m3", "BUY_UP", 0.6, 0.5, "2026-01-01T00:02:00+00:00")
        conn.commit()
        samples = load_samples(conn)

    by_market = {sample.market_id: sample for sample in samples}
    assert by_market["m1"].won is True
    assert by_market["m2"].won is False
    assert by_market["m3"].won is None


def test_summarize_computes_win_rate_and_pnl(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        _insert_market(conn, "m1", "1", "0")
        _insert_market(conn, "m2", "1", "0")
        _insert_decision(conn, "m1", "BUY_UP", 0.6, 0.5, "2026-01-01T00:00:00+00:00")
        _insert_decision(conn, "m2", "BUY_DOWN", 0.6, 0.5, "2026-01-01T00:01:00+00:00")
        conn.commit()
        samples = load_samples(conn)
        summary = summarize(samples, settings)

    assert summary["all"]["resolved"] == 2
    assert summary["all"]["wins"] == 1
    assert summary["all"]["win_rate"] == 0.5
    # win at 0.5 pays ~+0.965, loss costs ~-1.035 => small negative net.
    assert summary["all"]["pnl_usdc"] < 0


def test_run_backtest_calibration_buckets(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        _insert_market(conn, "m1", "1", "0")
        _insert_market(conn, "m2", "1", "0")
        _insert_decision(conn, "m1", "BUY_UP", 0.65, 0.5, "2026-01-01T00:00:00+00:00")
        _insert_decision(conn, "m2", "BUY_DOWN", 0.65, 0.5, "2026-01-01T00:01:00+00:00")
        conn.commit()
        report = run_backtest(conn, settings)

    populated = [bucket for bucket in report["calibration"] if bucket["count"] > 0]
    assert len(populated) == 1
    assert populated[0]["count"] == 2
    assert abs(populated[0]["predicted"] - 0.65) < 1e-9
    assert populated[0]["actual"] == 0.5
