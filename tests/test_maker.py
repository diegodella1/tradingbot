from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.backtest.maker import maker_vs_taker
from bot.storage.db import connect, init_db


def _snapshot(conn, market_id: str, token_id: str, bid: float, ask: float, at: datetime) -> None:
    conn.execute(
        "INSERT INTO market_snapshots (market_id, token_id, best_bid, best_ask, spread, liquidity, imbalance, created_at) VALUES (?, ?, ?, ?, ?, 100, 0, ?)",
        (market_id, token_id, bid, ask, ask - bid, at.isoformat()),
    )


def _settled_trade(conn, market_id: str, token_id: str, status: str, price: float, pnl: float, fee: float, at: datetime) -> None:
    conn.execute(
        "INSERT INTO fills (order_id, market_id, token_id, side, price, size_usdc, fee_usdc, pnl_usdc, created_at) VALUES (?, ?, ?, 'BUY', ?, 1.0, ?, 0, ?)",
        (f"o-{market_id}", market_id, token_id, price, fee, at.isoformat()),
    )
    conn.execute(
        """
        INSERT INTO positions (market_id, token_id, size_usdc, avg_price, shares, fee_usdc, status, realized_pnl_usdc, settled_at, updated_at)
        VALUES (?, ?, 1.0, ?, ?, ?, ?, ?, ?, ?)
        """,
        (market_id, token_id, price, 1.0 / price, fee, status, pnl, at.isoformat(), at.isoformat()),
    )


def test_maker_wins_when_ask_trades_through_bid(settings):
    init_db(settings.sqlite_path)
    now = datetime.now(UTC)
    with connect(settings.sqlite_path) as conn:
        # Entry snapshot: bid 0.60 / ask 0.62; taker paid 0.62 with fee.
        _snapshot(conn, "m1", "t1", 0.60, 0.62, now - timedelta(seconds=5))
        _settled_trade(conn, "m1", "t1", "WON", 0.62, 0.58, 0.017, now)
        # 20s later the ask drops to 0.60 -> maker at 0.60 fills.
        _snapshot(conn, "m1", "t1", 0.58, 0.60, now + timedelta(seconds=20))
        conn.commit()

        result = maker_vs_taker(conn, fill_window_seconds=60)

    assert result.trades == 1
    assert result.maker_fills == 1
    # Maker: 1/0.60 - 1 = +0.667 vs taker +0.58; zero fees.
    assert round(result.maker_pnl_usdc, 3) == 0.667
    assert round(result.taker_pnl_usdc, 2) == 0.58
    assert result.taker_fees_usdc > 0


def test_maker_misses_fill_when_ask_never_drops(settings):
    init_db(settings.sqlite_path)
    now = datetime.now(UTC)
    with connect(settings.sqlite_path) as conn:
        _snapshot(conn, "m1", "t1", 0.60, 0.62, now - timedelta(seconds=5))
        _settled_trade(conn, "m1", "t1", "WON", 0.62, 0.58, 0.017, now)
        _snapshot(conn, "m1", "t1", 0.63, 0.65, now + timedelta(seconds=20))  # price ran away
        conn.commit()

        result = maker_vs_taker(conn, fill_window_seconds=60)

    assert result.trades == 1
    assert result.maker_fills == 0
    assert result.maker_pnl_usdc == 0.0  # missed the trade entirely


def test_maker_loses_full_stake_without_fee_on_lost_market(settings):
    init_db(settings.sqlite_path)
    now = datetime.now(UTC)
    with connect(settings.sqlite_path) as conn:
        _snapshot(conn, "m1", "t1", 0.60, 0.62, now - timedelta(seconds=5))
        _settled_trade(conn, "m1", "t1", "LOST", 0.62, -1.017, 0.017, now)
        _snapshot(conn, "m1", "t1", 0.55, 0.58, now + timedelta(seconds=20))
        conn.commit()

        result = maker_vs_taker(conn, fill_window_seconds=60)

    assert result.maker_fills == 1
    assert round(result.maker_pnl_usdc, 2) == -1.0  # no fee on the maker side
    assert round(result.taker_pnl_usdc, 3) == -1.017


def test_live_order_price_maker_joins_bid(settings, context):
    from bot.live_loop import _live_order_price
    from bot.polymarket.models import OutcomeSide, Signal, SignalAction

    signal = Signal(action=SignalAction.BUY_UP, confidence=0.9, max_price=0.52, size_usdc=1.0, reason="test")

    settings.live_order_style = "taker"
    assert _live_order_price(settings, context, OutcomeSide.UP, signal) == 0.52

    settings.live_order_style = "maker"
    assert _live_order_price(settings, context, OutcomeSide.UP, signal) == 0.49  # joins best bid

    context.up_book.bids.clear()
    assert _live_order_price(settings, context, OutcomeSide.UP, signal) is None
