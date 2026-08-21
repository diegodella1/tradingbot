from __future__ import annotations

import json

from bot.backtest import build_training_rows
from bot.storage.db import connect, init_db


def _market_raw(up_price: str, down_price: str) -> str:
    return json.dumps(
        {
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
        (market_id, None, "Bitcoin Up or Down - 5 minute", "btc-updown-5m-1750000000", "5m", None, None, 100, 10, 1, _market_raw(up_price, down_price)),
    )


def _insert_snapshot(conn, market_id: str, token_id: str, ask: float, created_at: str) -> None:
    conn.execute(
        "INSERT INTO market_snapshots (market_id, token_id, best_bid, best_ask, spread, liquidity, imbalance, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (market_id, token_id, ask - 0.02, ask, 0.02, 100, 0.1, created_at),
    )


def _insert_hold_decision(conn, market_id: str, created_at: str) -> None:
    metadata = {"features": {"momentum_15s": 0.001, "momentum_60s": 0.002, "change_since_open": 10.0, "realized_volatility": 0.0001, "book_imbalance": 0.1, "seconds_to_close": 400}}
    conn.execute(
        """
        INSERT INTO strategy_decisions (
          market_id, market_type, action, estimated_probability, market_price, edge,
          ev_usdc, kelly_fraction, recommended_size_usdc, confidence, reason, metadata_json, created_at
        )
        VALUES (?, '5m', 'HOLD', NULL, NULL, NULL, 0, 0, 0, 0, 'test', ?, ?)
        """,
        (market_id, json.dumps(metadata), created_at),
    )


def test_build_training_rows_covers_both_sides_of_holds(settings):
    init_db(settings.sqlite_path)
    ts = "2026-01-01T00:00:00+00:00"
    with connect(settings.sqlite_path) as conn:
        _insert_market(conn, "m1", "1", "0")  # UP wins
        _insert_snapshot(conn, "m1", "up-token", 0.55, ts)
        _insert_snapshot(conn, "m1", "down-token", 0.47, ts)
        _insert_hold_decision(conn, "m1", ts)
        conn.commit()
        rows = build_training_rows(conn)

    assert len(rows) == 2  # one per side, even though the decision was HOLD
    by_side = {row.side: row for row in rows}
    assert by_side["UP"].label == 1
    assert by_side["DOWN"].label == 0
    assert by_side["UP"].ask == 0.55
    assert by_side["UP"].seconds_to_close == 400


def test_build_training_rows_skips_unresolved_markets(settings):
    init_db(settings.sqlite_path)
    ts = "2026-01-01T00:00:00+00:00"
    with connect(settings.sqlite_path) as conn:
        _insert_market(conn, "m1", "0.5", "0.5")  # not resolved
        _insert_snapshot(conn, "m1", "up-token", 0.55, ts)
        _insert_hold_decision(conn, "m1", ts)
        conn.commit()
        rows = build_training_rows(conn)

    assert rows == []


def test_build_training_rows_respects_snapshot_gap(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        _insert_market(conn, "m1", "1", "0")
        # Snapshot 10 minutes away from the decision: outside the 30s gap.
        _insert_snapshot(conn, "m1", "up-token", 0.55, "2026-01-01T00:10:00+00:00")
        _insert_hold_decision(conn, "m1", "2026-01-01T00:00:00+00:00")
        conn.commit()
        rows = build_training_rows(conn)

    assert rows == []


def test_build_training_rows_never_uses_future_snapshot(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        _insert_market(conn, "m1", "1", "0")
        _insert_snapshot(conn, "m1", "up-token", 0.55, "2026-01-01T00:00:01+00:00")
        _insert_snapshot(conn, "m1", "down-token", 0.47, "2026-01-01T00:00:01+00:00")
        _insert_hold_decision(conn, "m1", "2026-01-01T00:00:00+00:00")
        conn.commit()

        rows = build_training_rows(conn)

    assert rows == []


def test_build_training_rows_deduplicates_each_market_side_time_bucket(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        _insert_market(conn, "m1", "1", "0")
        for second, remaining in ((0, 410), (2, 408), (20, 390)):
            ts = f"2026-01-01T00:00:{second:02d}+00:00"
            _insert_snapshot(conn, "m1", "up-token", 0.55, ts)
            _insert_snapshot(conn, "m1", "down-token", 0.47, ts)
            metadata = {
                "features": {
                    "momentum_15s": 0.001,
                    "momentum_60s": 0.002,
                    "change_since_open": 10.0,
                    "realized_volatility": 0.0001,
                    "book_imbalance": 0.1,
                    "seconds_to_close": remaining,
                }
            }
            conn.execute(
                "INSERT INTO strategy_decisions (market_id, market_type, action, confidence, reason, metadata_json, created_at) VALUES (?, '5m', 'HOLD', 0, 'test', ?, ?)",
                ("m1", json.dumps(metadata), ts),
            )
        conn.commit()

        rows = build_training_rows(conn, sample_bucket_seconds=15)

    assert len(rows) == 4  # two sides across two 15-second buckets
