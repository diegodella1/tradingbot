from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.storage.db import connect, init_db
from bot.storage.repositories import Repository


def _insert_settled(conn, market_id: str, token_id: str, status: str, pnl: float, settled_at: str) -> None:
    conn.execute(
        """
        INSERT INTO positions (
          market_id, token_id, size_usdc, avg_price, shares, fee_usdc,
          status, realized_pnl_usdc, settlement_outcome, settled_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (market_id, token_id, 1.0, 0.5, 2.0, 0.0, status, pnl, token_id, settled_at, settled_at),
    )


def _insert_market(conn, market_id: str, market_type: str) -> None:
    conn.execute(
        """
        INSERT INTO markets (
          market_id, event_id, question, slug, market_type, start_time,
          end_time, liquidity, volume, mapping_verified, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            market_id,
            f"event-{market_id}",
            f"Bitcoin Up or Down - {market_type}",
            f"btc-updown-{market_type}-{market_id}",
            market_type,
            None,
            None,
            1000,
            1000,
            1,
            "{}",
        ),
    )


def test_hydrate_populates_daily_pnl_streak_and_last_loss(settings):
    init_db(settings.sqlite_path)
    now = datetime.now(UTC)
    with connect(settings.sqlite_path) as conn:
        _insert_settled(conn, "m1", "t1", "WON", 0.8, (now - timedelta(minutes=30)).isoformat())
        _insert_settled(conn, "m2", "t2", "LOST", -1.0, (now - timedelta(minutes=20)).isoformat())
        _insert_settled(conn, "m3", "t3", "LOST", -1.0, (now - timedelta(minutes=10)).isoformat())
        conn.commit()
        state = Repository(conn).hydrate_risk_state()

    assert round(state.daily_pnl_usdc, 2) == -1.2
    assert state.consecutive_losses == 2
    assert state.last_loss_at is not None
    assert state.last_loss_at.tzinfo is not None


def test_hydrate_streak_resets_after_a_win(settings):
    init_db(settings.sqlite_path)
    now = datetime.now(UTC)
    with connect(settings.sqlite_path) as conn:
        _insert_settled(conn, "m1", "t1", "LOST", -1.0, (now - timedelta(minutes=30)).isoformat())
        _insert_settled(conn, "m2", "t2", "WON", 0.9, (now - timedelta(minutes=5)).isoformat())
        conn.commit()
        state = Repository(conn).hydrate_risk_state()

    assert state.consecutive_losses == 0
    assert state.last_loss_at is None

def test_hydrate_streak_ignores_losses_outside_window(settings):
    """Old losing streaks must expire; otherwise the bot deadlocks forever."""
    init_db(settings.sqlite_path)
    now = datetime.now(UTC)
    with connect(settings.sqlite_path) as conn:
        _insert_settled(conn, "m1", "t1", "LOST", -1.0, (now - timedelta(hours=5)).isoformat())
        _insert_settled(conn, "m2", "t2", "LOST", -1.0, (now - timedelta(hours=4)).isoformat())
        _insert_settled(conn, "m3", "t3", "LOST", -1.0, (now - timedelta(hours=3)).isoformat())
        conn.commit()
        state = Repository(conn).hydrate_risk_state(loss_streak_window_minutes=120)

    assert state.consecutive_losses == 0
    assert state.last_loss_at is None


def test_hydrate_streak_counts_losses_inside_window(settings):
    init_db(settings.sqlite_path)
    now = datetime.now(UTC)
    with connect(settings.sqlite_path) as conn:
        _insert_settled(conn, "m1", "t1", "LOST", -1.0, (now - timedelta(hours=5)).isoformat())
        _insert_settled(conn, "m2", "t2", "LOST", -1.0, (now - timedelta(minutes=30)).isoformat())
        _insert_settled(conn, "m3", "t3", "LOST", -1.0, (now - timedelta(minutes=10)).isoformat())
        conn.commit()
        state = Repository(conn).hydrate_risk_state(loss_streak_window_minutes=120)

    assert state.consecutive_losses == 2
    assert state.last_loss_at is not None


def test_hydrate_populates_frequency_and_5m_loss_state(settings):
    init_db(settings.sqlite_path)
    now = datetime.now(UTC)
    with connect(settings.sqlite_path) as conn:
        _insert_market(conn, "m5a", "5m")
        _insert_market(conn, "m5b", "5m")
        _insert_market(conn, "m15", "15m")
        for idx in range(3):
            created_at = (now - timedelta(minutes=idx * 10)).isoformat()
            conn.execute(
                """
                INSERT INTO orders (
                  order_id, market_id, token_id, side, status, price, size_usdc,
                  filled_size_usdc, avg_fill_price, reason, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (f"o{idx}", "m15", "t15", "BUY", "FILLED", 0.6, 1.0, 1.0, 0.6, "test", created_at),
            )
        _insert_settled(conn, "m5a", "t5a", "LOST", -1.0, (now - timedelta(minutes=5)).isoformat())
        _insert_settled(conn, "m5b", "t5b", "LOST", -1.2, (now - timedelta(minutes=15)).isoformat())
        _insert_settled(conn, "m15", "t15", "WON", 0.7, (now - timedelta(minutes=20)).isoformat())
        conn.commit()
        state = Repository(conn).hydrate_risk_state()

    assert state.trades_last_hour == 3
    assert state.recent_5m_settled_count == 2
    assert round(state.recent_5m_pnl_usdc, 2) == -2.2
    assert state.recent_settled_count == 3
    assert round(state.recent_pnl_usdc, 2) == -1.5
    assert state.last_settled_at is not None
