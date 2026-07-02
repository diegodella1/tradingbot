from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.btc.price_feed import CoinbaseBtcFeed
from bot.polymarket.models import BookLevel, OrderBook
from bot.polymarket.realtime import BookCache, RealtimeMarketData


def _book(token_id: str, ts: datetime) -> OrderBook:
    return OrderBook(
        market_id="m1",
        token_id=token_id,
        bids=[BookLevel(price=0.49, size=100)],
        asks=[BookLevel(price=0.51, size=100)],
        timestamp=ts,
    )


@pytest.mark.asyncio
async def test_book_cache_update_and_freshness():
    cache = BookCache()
    await cache.update(_book("up-token", datetime.now(UTC)))
    assert cache.get("up-token") is not None
    assert cache.is_fresh("up-token", 5.0) is True


@pytest.mark.asyncio
async def test_book_cache_stale_book_not_fresh():
    cache = BookCache()
    await cache.update(_book("up-token", datetime.now(UTC) - timedelta(seconds=30)))
    assert cache.is_fresh("up-token", 5.0) is False


def test_get_book_returns_none_when_stale(settings):
    feed = CoinbaseBtcFeed(settings)
    realtime = RealtimeMarketData(settings, feed)
    realtime.cache._books["up-token"] = _book("up-token", datetime.now(UTC) - timedelta(seconds=30))
    assert realtime.get_book("up-token") is None


def test_connected_false_before_market_ws_started(settings):
    feed = CoinbaseBtcFeed(settings)
    feed.connected = True
    realtime = RealtimeMarketData(settings, feed)
    assert realtime.connected is False  # no market websocket yet


@pytest.mark.asyncio
async def test_ensure_subscription_tracks_token_set(settings, monkeypatch):
    started: list[list[str]] = []

    class _StubWS:
        def __init__(self, token_ids, on_book):
            self.token_ids = token_ids
            self.connected = True
            started.append(list(token_ids))

        async def run(self):
            return None

        async def stop(self):
            return None

    monkeypatch.setattr("bot.polymarket.realtime.MarketWebSocket", _StubWS)
    feed = CoinbaseBtcFeed(settings)
    realtime = RealtimeMarketData(settings, feed)

    await realtime.ensure_subscription(["up-token", "down-token"])
    await realtime.ensure_subscription(["down-token", "up-token"])  # same set, no restart
    assert len(started) == 1

    await realtime.ensure_subscription(["new-token"])  # changed set -> restart
    assert len(started) == 2
    await realtime.stop()
