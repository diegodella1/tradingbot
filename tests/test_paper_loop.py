from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bot.btc.price_feed import CoinbaseBtcFeed
from bot.execution.risk_manager import RiskManager, RiskState
from bot.main import _sync_risk_state, run_paper_loop
from bot.polymarket.clob import ClobClient
from bot.polymarket.gamma import GammaClient
from bot.polymarket.models import BookLevel, BtcMarketState, MarketType, OrderBook
from bot.storage.db import connect, init_db
from bot.storage.repositories import Repository


class FakeGamma:
    def __init__(self, settings, market):
        self.market = market

    async def discover_btc_updown(self):
        return {MarketType.FIVE_MINUTE: [self.market], MarketType.FIFTEEN_MINUTE: []}

    async def close(self):
        return None


class FakeClob:
    def __init__(self, settings):
        pass

    async def get_order_book(self, token_id, market_id=""):
        return OrderBook(
            market_id=market_id,
            token_id=token_id,
            bids=[BookLevel(price=0.49, size=100)],
            asks=[BookLevel(price=0.51, size=100)],
        )

    async def close(self):
        return None


async def fake_poll_once(self):
    return BtcMarketState(current_price=101, market_open_price=100, price_timestamp=datetime.now(UTC), momentum_15s=0.01, momentum_60s=0.02)


@pytest.mark.asyncio
async def test_paper_loop_uses_real_market_path_and_persists(monkeypatch, settings, market):
    settings.enable_experimental_strategy = False
    settings.paper_loop_interval_seconds = 0
    init_db(settings.sqlite_path)
    monkeypatch.setattr(GammaClient, "__init__", lambda self, settings: setattr(self, "_fake", FakeGamma(settings, market)))
    monkeypatch.setattr(GammaClient, "discover_btc_updown", lambda self: self._fake.discover_btc_updown())
    monkeypatch.setattr(GammaClient, "close", lambda self: self._fake.close())
    monkeypatch.setattr(ClobClient, "__init__", lambda self, settings: None)
    monkeypatch.setattr(ClobClient, "get_order_book", FakeClob(settings).get_order_book)
    monkeypatch.setattr(ClobClient, "close", FakeClob(settings).close)
    monkeypatch.setattr(CoinbaseBtcFeed, "poll_once", fake_poll_once)

    await run_paper_loop(settings, max_cycles=1)

    with connect(settings.sqlite_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM risk_events").fetchone()[0] == 1


def test_sync_risk_state_clears_expired_open_exposure(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        conn.execute(
            "INSERT INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("expired", None, "Bitcoin Up or Down - 5 minute", "btc-updown-5m-expired", "5m", None, "2026-01-01T00:00:00+00:00", 100, 10, 1, "{}"),
        )
        conn.execute(
            """
            INSERT INTO positions (market_id, token_id, size_usdc, avg_price, shares, status, realized_pnl_usdc, updated_at)
            VALUES (?, ?, ?, ?, ?, 'OPEN', 0, ?)
            """,
            ("expired", "up-token", 1.0, 0.5, 2.0, "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
        repo = Repository(conn)
        risk = RiskManager(settings, RiskState(market_exposure={"expired": 1.0}, token_exposure={"up-token": 1.0}))

        _sync_risk_state(settings, repo, risk)

        assert risk.state.market_exposure == {}
        assert conn.execute("SELECT status FROM positions WHERE market_id = ?", ("expired",)).fetchone()["status"] == "EXPIRED_UNKNOWN"
        assert conn.execute("SELECT COUNT(*) FROM health_events WHERE name = 'risk_state' AND status = 'synced'").fetchone()[0] == 1
