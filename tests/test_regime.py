from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.monitoring.regime import regime_snapshot
from bot.storage.db import connect, init_db


def _insert(conn, i: int, status: str, pnl: float, avg_price: float = 0.6, fee: float = 0.01) -> None:
    settled = (datetime.now(UTC) - timedelta(minutes=i)).isoformat()
    conn.execute(
        """
        INSERT INTO positions (
          market_id, token_id, size_usdc, avg_price, shares, fee_usdc,
          status, realized_pnl_usdc, settlement_outcome, settled_at, updated_at
        )
        VALUES (?, ?, 1.0, ?, ?, ?, ?, ?, 't', ?, ?)
        """,
        (f"m{i}", f"t{i}", avg_price, 1.0 / avg_price, fee, status, pnl, settled, settled),
    )


def test_snapshot_without_trades_is_healthy(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        snapshot = regime_snapshot(conn)
    assert snapshot["healthy"] is True
    assert snapshot["evaluated"] is False
    assert snapshot["trades"] == 0


def test_snapshot_below_min_trades_is_not_evaluated(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        for i in range(5):
            _insert(conn, i, "LOST", -1.0)
        conn.commit()
        snapshot = regime_snapshot(conn, window_trades=50, min_trades=30)
    assert snapshot["healthy"] is True
    assert snapshot["evaluated"] is False


def test_snapshot_flags_wr_below_breakeven(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        # Entries at 0.6 need ~61% WR with 1% fees; give it 50%.
        for i in range(30):
            _insert(conn, i, "WON" if i % 2 == 0 else "LOST", 0.65 if i % 2 == 0 else -1.01)
        conn.commit()
        snapshot = regime_snapshot(conn, window_trades=50, min_trades=30)
    assert snapshot["evaluated"] is True
    assert snapshot["healthy"] is False
    assert snapshot["win_rate"] == 0.5
    assert snapshot["breakeven_win_rate"] > 0.6


def test_snapshot_healthy_when_wr_beats_breakeven(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        for i in range(30):
            _insert(conn, i, "WON" if i % 10 else "LOST", 0.65 if i % 10 else -1.01)
        conn.commit()
        snapshot = regime_snapshot(conn, window_trades=50, min_trades=30)
    assert snapshot["evaluated"] is True
    assert snapshot["healthy"] is True


def test_risk_manager_blocks_when_regime_blocked(settings, context):
    from bot.execution.risk_manager import RiskManager
    from bot.polymarket.models import Signal, SignalAction

    risk = RiskManager(settings)
    risk.state.regime_blocked = True
    signal = Signal(action=SignalAction.BUY_UP, confidence=0.9, max_price=0.6, size_usdc=1.0, reason="test")
    decision = risk.validate(signal, context)
    assert decision.approved is False
    assert "regime stop" in decision.reason
