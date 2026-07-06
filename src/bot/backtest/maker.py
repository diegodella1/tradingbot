from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class MakerComparison:
    trades: int
    maker_fills: int
    maker_pnl_usdc: float
    taker_pnl_usdc: float
    maker_fees_usdc: float
    taker_fees_usdc: float

    @property
    def fill_rate(self) -> float | None:
        return self.maker_fills / self.trades if self.trades else None


def _parse(created_at: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        return None


def _entry_bid(conn: sqlite3.Connection, market_id: str, token_id: str, at_iso: str) -> float | None:
    row = conn.execute(
        """
        SELECT best_bid FROM market_snapshots
        WHERE market_id = ? AND token_id = ? AND created_at <= ? AND best_bid IS NOT NULL
        ORDER BY created_at DESC LIMIT 1
        """,
        (market_id, token_id, at_iso),
    ).fetchone()
    return float(row["best_bid"]) if row else None


def _maker_filled(conn: sqlite3.Connection, market_id: str, token_id: str, bid: float, start: datetime, window_seconds: float) -> bool:
    """Conservative fill proxy: the ask must trade down to (or through) our bid.

    Snapshots arrive every ~10s, so a fleeting touch can be missed — this
    underestimates fills rather than inventing them.
    """
    end_iso = (start + timedelta(seconds=window_seconds)).isoformat()
    row = conn.execute(
        """
        SELECT 1 FROM market_snapshots
        WHERE market_id = ? AND token_id = ? AND created_at > ? AND created_at <= ?
          AND best_ask IS NOT NULL AND best_ask <= ?
        LIMIT 1
        """,
        (market_id, token_id, start.isoformat(), end_iso, bid),
    ).fetchone()
    return row is not None


def maker_vs_taker(conn: sqlite3.Connection, fill_window_seconds: float = 60.0) -> MakerComparison:
    """Replay settled paper entries as maker orders posted at the best bid.

    Taker leg uses the actual recorded PnL/fees. The maker leg pays zero fee
    (Polymarket crypto fees are taker-only) but risks not filling: unfilled
    entries contribute zero PnL. The 20% maker rebate is NOT included, so the
    reported maker PnL is a lower bound.
    """
    rows = conn.execute(
        """
        SELECT p.market_id, p.token_id, p.size_usdc, p.fee_usdc, p.status, p.realized_pnl_usdc,
               f.created_at AS entry_at
        FROM positions p
        JOIN fills f
          ON f.market_id = p.market_id AND f.token_id = p.token_id AND f.side = 'BUY'
        WHERE p.status IN ('WON', 'LOST')
        GROUP BY p.id
        """
    ).fetchall()

    trades = maker_fills = 0
    maker_pnl = taker_pnl = taker_fees = 0.0
    for row in rows:
        entry_at = _parse(row["entry_at"])
        if entry_at is None:
            continue
        bid = _entry_bid(conn, row["market_id"], row["token_id"], row["entry_at"])
        if bid is None or bid <= 0:
            continue
        trades += 1
        taker_pnl += float(row["realized_pnl_usdc"] or 0)
        taker_fees += float(row["fee_usdc"] or 0)
        if not _maker_filled(conn, row["market_id"], row["token_id"], bid, entry_at, fill_window_seconds):
            continue
        maker_fills += 1
        size = float(row["size_usdc"] or 0)
        shares = size / bid
        maker_pnl += (shares - size) if row["status"] == "WON" else -size

    return MakerComparison(
        trades=trades,
        maker_fills=maker_fills,
        maker_pnl_usdc=maker_pnl,
        taker_pnl_usdc=taker_pnl,
        maker_fees_usdc=0.0,
        taker_fees_usdc=taker_fees,
    )
