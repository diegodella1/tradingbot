from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.storage.db import connect, init_db, maybe_prune_old_data, prune_old_data


def _seed(conn, created_at: str) -> None:
    conn.execute(
        "INSERT INTO market_snapshots (market_id, token_id, best_bid, best_ask, spread, liquidity, imbalance, created_at) VALUES ('m', 't', 0.5, 0.51, 0.01, 100, 0, ?)",
        (created_at,),
    )
    conn.execute("INSERT INTO btc_ticks (price, created_at) VALUES (100.0, ?)", (created_at,))


def test_prune_deletes_only_rows_older_than_retention(settings):
    init_db(settings.sqlite_path)
    now = datetime.now(UTC)
    with connect(settings.sqlite_path) as conn:
        _seed(conn, (now - timedelta(days=10)).isoformat())
        _seed(conn, (now - timedelta(days=1)).isoformat())
        conn.commit()

        result = prune_old_data(conn, retention_days=7)

        assert result["market_snapshots_deleted"] == 1
        assert result["btc_ticks_deleted"] == 1
        assert conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM btc_ticks").fetchone()[0] == 1


def test_maybe_prune_runs_at_most_once_per_interval(settings):
    init_db(settings.sqlite_path)
    now = datetime.now(UTC)
    with connect(settings.sqlite_path) as conn:
        _seed(conn, (now - timedelta(days=10)).isoformat())
        conn.commit()

        first = maybe_prune_old_data(conn, retention_days=7)
        assert first is not None
        assert first["market_snapshots_deleted"] == 1

        _seed(conn, (now - timedelta(days=10)).isoformat())
        conn.commit()
        second = maybe_prune_old_data(conn, retention_days=7)
        assert second is None  # throttled by the daily marker
        assert conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0] == 1
