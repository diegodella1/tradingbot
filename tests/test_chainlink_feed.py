from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from bot.btc.chainlink_feed import ChainlinkBtcFeed


def _frame(value: float, ts: datetime) -> str:
    return json.dumps(
        {
            "topic": "crypto_prices_chainlink",
            "type": "update",
            "payload": {"symbol": "btc/usd", "timestamp": ts.timestamp() * 1000, "value": value},
        }
    )


def test_handle_message_ingests_live_updates():
    feed = ChainlinkBtcFeed()
    now = datetime.now(UTC)
    feed.handle_message(_frame(100000.0, now))
    assert feed.current_price == 100000.0
    assert feed.is_fresh()


def test_handle_message_ingests_history_dump():
    feed = ChainlinkBtcFeed()
    now = datetime.now(UTC)
    dump = json.dumps(
        {
            "topic": "crypto_prices_chainlink",
            "type": "update",
            "payload": {
                "symbol": "btc/usd",
                "data": [
                    {"timestamp": (now - timedelta(seconds=20)).timestamp() * 1000, "value": 99000.0},
                    {"timestamp": (now - timedelta(seconds=10)).timestamp() * 1000, "value": 99500.0},
                ],
            },
        }
    )
    feed.handle_message(dump)
    assert feed.current_price == 99500.0
    assert len(feed.ticks.ticks) == 2


def test_handle_message_ignores_other_topics_and_symbols():
    feed = ChainlinkBtcFeed()
    now = datetime.now(UTC)
    feed.handle_message(json.dumps({"topic": "comments", "payload": {"value": 1}}))
    feed.handle_message(
        json.dumps({"topic": "crypto_prices_chainlink", "payload": {"symbol": "eth/usd", "timestamp": now.timestamp() * 1000, "value": 3500.0}})
    )
    feed.handle_message("PONG")
    assert feed.current_price is None


def test_first_tick_at_or_after_returns_price_to_beat():
    feed = ChainlinkBtcFeed()
    boundary = datetime.now(UTC) - timedelta(seconds=60)
    feed.handle_message(_frame(99000.0, boundary - timedelta(seconds=5)))
    feed.handle_message(_frame(99100.0, boundary + timedelta(seconds=1)))
    feed.handle_message(_frame(99300.0, boundary + timedelta(seconds=30)))
    assert feed.first_tick_at_or_after(boundary) == 99100.0


def test_first_tick_at_or_after_rejects_stale_first_tick():
    feed = ChainlinkBtcFeed()
    boundary = datetime.now(UTC) - timedelta(minutes=10)
    feed.handle_message(_frame(99100.0, boundary + timedelta(minutes=5)))
    assert feed.first_tick_at_or_after(boundary, max_delay_seconds=120) is None


def test_out_of_order_duplicate_ticks_are_dropped():
    feed = ChainlinkBtcFeed()
    now = datetime.now(UTC)
    feed.handle_message(_frame(100.0, now))
    feed.handle_message(_frame(99.0, now - timedelta(seconds=5)))  # overlap from history dump
    assert len(feed.ticks.ticks) == 1
    assert feed.current_price == 100.0
