from __future__ import annotations

from datetime import UTC, datetime

import pytest

import bot.live_loop as live_loop
from bot.btc.price_feed import CoinbaseBtcFeed
from bot.execution.live_broker import LiveBroker
from bot.monitoring.alerts import send_alert
from bot.polymarket.clob import ClobClient
from bot.polymarket.gamma import GammaClient
from bot.polymarket.models import (
    BookLevel,
    BtcMarketState,
    MarketType,
    OrderBook,
    OrderRecord,
    OrderStatus,
    Signal,
    SignalAction,
)
from bot.storage.db import connect, init_db
from bot.strategy.momentum_book_imbalance import MomentumBookImbalanceStrategy


@pytest.mark.asyncio
async def test_run_live_loop_blocked_when_disabled(settings):
    settings.enable_live_trading = False
    with pytest.raises(RuntimeError):
        await live_loop.run_live_loop(settings, max_cycles=1)


class _FakeGamma:
    def __init__(self, settings, market):
        self.market = market

    async def discover_btc_updown(self):
        return {MarketType.FIVE_MINUTE: [self.market], MarketType.FIFTEEN_MINUTE: []}

    async def close(self):
        return None


class _FakeClob:
    async def get_order_book(self, token_id, market_id=""):
        return OrderBook(market_id=market_id, token_id=token_id, bids=[BookLevel(price=0.49, size=100)], asks=[BookLevel(price=0.51, size=100)])

    async def close(self):
        return None


async def _fake_poll_once(self):
    return BtcMarketState(current_price=101, market_open_price=100, price_timestamp=datetime.now(UTC), momentum_15s=0.01, momentum_60s=0.02)


@pytest.mark.asyncio
async def test_live_loop_places_then_cancels_open_order(monkeypatch, settings, market):
    settings.enable_live_trading = True
    settings.enable_experimental_strategy = True
    settings.live_loop_interval_seconds = 0

    canceled: list[str] = []

    async def _fake_preflight(_settings):
        return None

    async def _fake_place(self, request, context, signal):
        return OrderRecord(order_id="live-1", request=request, status=OrderStatus.OPEN)

    async def _fake_cancel(self, order_id):
        canceled.append(order_id)

    def _buy_signal(self, context):
        return Signal(action=SignalAction.BUY_UP, confidence=0.9, max_price=0.52, size_usdc=1.0, reason="test buy")

    monkeypatch.setattr(live_loop, "_preflight", _fake_preflight)
    monkeypatch.setattr(GammaClient, "__init__", lambda self, settings: setattr(self, "_fake", _FakeGamma(settings, market)))
    monkeypatch.setattr(GammaClient, "discover_btc_updown", lambda self: self._fake.discover_btc_updown())
    monkeypatch.setattr(GammaClient, "close", lambda self: self._fake.close())
    monkeypatch.setattr(ClobClient, "__init__", lambda self, settings: None)
    monkeypatch.setattr(ClobClient, "get_order_book", _FakeClob().get_order_book)
    monkeypatch.setattr(ClobClient, "close", _FakeClob().close)
    monkeypatch.setattr(CoinbaseBtcFeed, "poll_once", _fake_poll_once)
    monkeypatch.setattr(MomentumBookImbalanceStrategy, "evaluate", _buy_signal)
    monkeypatch.setattr(LiveBroker, "place_limit_order", _fake_place)
    monkeypatch.setattr(LiveBroker, "cancel_order", _fake_cancel)

    await live_loop.run_live_loop(settings, max_cycles=1)

    assert canceled == ["live-1"]  # open order canceled on shutdown
    with connect(settings.sqlite_path) as conn:
        init_db(settings.sqlite_path)
        row = conn.execute("SELECT status FROM orders WHERE order_id = 'live-1'").fetchone()
    assert row is not None


def test_send_alert_without_webhook_does_not_call_http(monkeypatch, settings):
    called = False

    def _post(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("bot.monitoring.alerts.httpx.post", _post)
    settings.alert_webhook_url = ""
    send_alert(settings, "test_message", foo="bar")
    assert called is False


def test_send_alert_with_webhook_posts(monkeypatch, settings):
    payloads: list[dict] = []

    def _post(url, json=None, timeout=None):
        payloads.append({"url": url, "json": json})

    monkeypatch.setattr("bot.monitoring.alerts.httpx.post", _post)
    settings.alert_webhook_url = "https://example.test/hook"
    send_alert(settings, "hello", n=1)
    assert len(payloads) == 1
    assert payloads[0]["url"] == "https://example.test/hook"
