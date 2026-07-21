from __future__ import annotations

import sqlite3


def regime_snapshot(
    conn: sqlite3.Connection,
    window_trades: int = 50,
    min_trades: int = 30,
    policy_version: str | None = None,
) -> dict:
    """Rolling win rate vs the real breakeven of the trades actually taken.

    For a binary position bought at price p with fee f per staked dollar, the
    breakeven win rate is w* = p * (1 + f): winning pays 1/p per dollar, so
    w*/p - (1 + f) = 0. If the rolling WR of the last `window_trades` settled
    trades drops below the average breakeven, the edge is gone (e.g. momentum
    dying in a mean-reversion regime) and the caller should alert/stop.
    """
    policy_clause = " AND policy_version = ?" if policy_version else ""
    parameters = (policy_version, window_trades) if policy_version else (window_trades,)
    rows = conn.execute(
        f"""
        SELECT status, avg_price, size_usdc, fee_usdc, realized_pnl_usdc
        FROM positions
        WHERE status IN ('WON', 'LOST', 'CLOSED') AND settled_at IS NOT NULL
        {policy_clause}
        ORDER BY settled_at DESC
        LIMIT ?
        """,
        parameters,
    ).fetchall()
    trades = len(rows)
    if trades == 0:
        return {"trades": 0, "win_rate": None, "breakeven_win_rate": None, "rolling_pnl_usdc": 0.0, "healthy": True, "evaluated": False}

    wins = 0
    breakeven_sum = 0.0
    rolling_pnl = 0.0
    for row in rows:
        pnl = float(row["realized_pnl_usdc"] or 0)
        rolling_pnl += pnl
        if row["status"] == "WON" or (row["status"] == "CLOSED" and pnl > 0):
            wins += 1
        price = float(row["avg_price"] or 0)
        size = float(row["size_usdc"] or 0)
        fee_rate = (float(row["fee_usdc"] or 0) / size) if size > 0 else 0.0
        breakeven_sum += min(1.0, price * (1 + fee_rate))

    win_rate = wins / trades
    breakeven = breakeven_sum / trades
    evaluated = trades >= min_trades
    return {
        "trades": trades,
        "win_rate": win_rate,
        "breakeven_win_rate": breakeven,
        "rolling_pnl_usdc": rolling_pnl,
        "healthy": (not evaluated) or win_rate >= breakeven,
        "evaluated": evaluated,
    }
