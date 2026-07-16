from __future__ import annotations

from types import SimpleNamespace
from datetime import UTC, datetime, timedelta

import pytest

from bot.btc.price_feed import CoinbaseBtcFeed
from bot.main import _btc_state_for_cycle, _track_feed_degradation
from bot.storage.db import connect, init_db
from bot.storage.repositories import Repository


def _realtime() -> SimpleNamespace:
    return SimpleNamespace(rest_fallback_active=False, ever_streamed=False, connected=False)


def _last_btc_feed_event(conn) -> dict | None:
    row = conn.execute(
        "SELECT status, detail FROM health_events WHERE name = 'btc_feed' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def test_warmup_rest_polling_does_not_alert(settings):
    init_db(settings.sqlite_path)
    realtime = _realtime()
    with connect(settings.sqlite_path) as conn:
        repo = Repository(conn)
        _track_feed_degradation(settings, repo, realtime, using_rest=True)
        assert realtime.rest_fallback_active is False
        assert _last_btc_feed_event(conn) is None


def test_degradation_after_streaming_alerts_once(settings):
    init_db(settings.sqlite_path)
    realtime = _realtime()
    with connect(settings.sqlite_path) as conn:
        repo = Repository(conn)
        _track_feed_degradation(settings, repo, realtime, using_rest=False)  # streamed
        _track_feed_degradation(settings, repo, realtime, using_rest=True)   # falls back
        assert realtime.rest_fallback_active is True
        event = _last_btc_feed_event(conn)
        assert event["status"] == "degraded"

        count_before = conn.execute("SELECT COUNT(*) FROM health_events WHERE name = 'btc_feed'").fetchone()[0]
        _track_feed_degradation(settings, repo, realtime, using_rest=True)   # still degraded, no re-alert
        count_after = conn.execute("SELECT COUNT(*) FROM health_events WHERE name = 'btc_feed'").fetchone()[0]
        assert count_after == count_before


def test_recovery_emits_recovered_event(settings):
    init_db(settings.sqlite_path)
    realtime = _realtime()
    with connect(settings.sqlite_path) as conn:
        repo = Repository(conn)
        _track_feed_degradation(settings, repo, realtime, using_rest=False)
        _track_feed_degradation(settings, repo, realtime, using_rest=True)
        _track_feed_degradation(settings, repo, realtime, using_rest=False)
        assert realtime.rest_fallback_active is False
        event = _last_btc_feed_event(conn)
        assert event["status"] == "recovered"


def test_rest_only_mode_is_ignored(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        repo = Repository(conn)
        _track_feed_degradation(settings, repo, None, using_rest=True)
        assert _last_btc_feed_event(conn) is None


@pytest.mark.asyncio
async def test_stale_stream_falls_back_to_rest(settings, monkeypatch):
    feed = CoinbaseBtcFeed(settings)
    feed.ticks.add(60_000, datetime.now(UTC) - timedelta(seconds=30))
    polled = False

    async def poll_once():
        nonlocal polled
        polled = True
        feed.ticks.add(60_100, datetime.now(UTC))
        return feed.state

    monkeypatch.setattr(feed, "poll_once", poll_once)
    state, source = await _btc_state_for_cycle(settings, feed, SimpleNamespace())

    assert polled is True
    assert source == "rest"
    assert state.current_price == 60_100


@pytest.mark.asyncio
async def test_fresh_stream_does_not_poll_rest(settings, monkeypatch):
    feed = CoinbaseBtcFeed(settings)
    feed.ticks.add(60_000, datetime.now(UTC))

    async def unexpected_poll():
        raise AssertionError("fresh websocket state should be used")

    monkeypatch.setattr(feed, "poll_once", unexpected_poll)
    state, source = await _btc_state_for_cycle(settings, feed, SimpleNamespace())

    assert source == "websocket"
    assert state.current_price == 60_000
