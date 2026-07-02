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
