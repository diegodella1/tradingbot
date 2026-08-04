from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from bot.execution.order_manager import paper_maker_bid_price


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


@dataclass
class MakerOrderReplay:
    attempts: int
    fills: int
    resolved_fills: int
    pnl_usdc: float
    recorded_pnl_usdc: float
    gross_profit_usdc: float
    gross_loss_usdc: float
    max_drawdown_usdc: float
    bankroll_usdc: float

    @property
    def fill_rate(self) -> float | None:
        return self.fills / self.attempts if self.attempts else None

    @property
    def profit_factor(self) -> float | None:
        if self.gross_loss_usdc > 0:
            return self.gross_profit_usdc / self.gross_loss_usdc
        return float("inf") if self.gross_profit_usdc > 0 else None

    @property
    def max_drawdown_pct(self) -> float:
        return self.max_drawdown_usdc / self.bankroll_usdc if self.bankroll_usdc > 0 else 0.0


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


def _maker_filled(
    conn: sqlite3.Connection,
    market_id: str,
    token_id: str,
    bid: float,
    start: datetime,
    window_seconds: float,
) -> bool:
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


def maker_vs_taker(
    conn: sqlite3.Connection,
    fill_window_seconds: float = 60.0,
    bid_offset_cents: float = 0.0,
) -> MakerComparison:
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
        bid = paper_maker_bid_price(bid, 1.0, bid_offset_cents)
        if bid is None:
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


def replay_maker_orders(
    conn: sqlite3.Connection,
    *,
    policy_version: str,
    fill_window_seconds: float = 60.0,
    bid_offset_cents: float = 0.0,
    min_net_edge_cents: float = 0.0,
    bankroll_usdc: float = 100.0,
) -> MakerOrderReplay:
    """Replay persisted maker attempts at a lower bid without inventing outcomes.

    Fill rate includes every observed ask touch. PnL includes only touched orders
    whose corresponding position has a verified WON/LOST settlement.
    """
    rows = conn.execute(
        """
        SELECT o.order_id, o.market_id, o.token_id, o.price, o.size_usdc, o.metadata_json,
               o.created_at, o.expires_at,
               p.id AS position_id, p.status AS settlement_status,
               p.realized_pnl_usdc AS recorded_pnl_usdc
        FROM orders o
        LEFT JOIN positions p
          ON p.market_id = o.market_id
         AND p.token_id = o.token_id
         AND p.policy_version = o.policy_version
         AND p.status IN ('WON', 'LOST')
        WHERE o.policy_version = ?
          AND o.execution_style = 'maker'
          AND o.side = 'BUY'
        ORDER BY o.created_at, o.order_id
        """,
        (policy_version,),
    ).fetchall()

    attempts = fills = resolved_fills = 0
    pnl = gross_profit = gross_loss = 0.0
    equity = peak = bankroll_usdc
    max_drawdown = 0.0
    recorded_pnl = 0.0
    recorded_positions: set[int] = set()

    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            metadata = {}
        if float(metadata.get("net_edge_cents") or 0) < min_net_edge_cents:
            continue
        attempts += 1
        start = _parse(row["created_at"])
        if start is None:
            continue
        bid = paper_maker_bid_price(float(row["price"]), 1.0, bid_offset_cents)
        if bid is None:
            continue
        window = fill_window_seconds
        expires_at = _parse(row["expires_at"]) if row["expires_at"] else None
        if expires_at is not None:
            window = max(0.0, min(window, (expires_at - start).total_seconds()))
        if window <= 0 or not _maker_filled(conn, row["market_id"], row["token_id"], bid, start, window):
            continue
        fills += 1
        if row["settlement_status"] not in {"WON", "LOST"}:
            continue

        resolved_fills += 1
        size = float(row["size_usdc"] or 0)
        trade_pnl = (size / bid - size) if row["settlement_status"] == "WON" else -size
        pnl += trade_pnl
        gross_profit += max(0.0, trade_pnl)
        gross_loss += abs(min(0.0, trade_pnl))
        equity += trade_pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

        position_id = int(row["position_id"])
        if position_id not in recorded_positions:
            recorded_positions.add(position_id)
            recorded_pnl += float(row["recorded_pnl_usdc"] or 0)

    return MakerOrderReplay(
        attempts=attempts,
        fills=fills,
        resolved_fills=resolved_fills,
        pnl_usdc=pnl,
        recorded_pnl_usdc=recorded_pnl,
        gross_profit_usdc=gross_profit,
        gross_loss_usdc=gross_loss,
        max_drawdown_usdc=max_drawdown,
        bankroll_usdc=bankroll_usdc,
    )
