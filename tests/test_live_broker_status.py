from __future__ import annotations

from bot.execution.live_broker import LiveBroker
from bot.polymarket.models import OrderStatus


def test_map_status_matched_is_filled():
    assert LiveBroker._map_status({"status": "matched"}) == OrderStatus.FILLED


def test_map_status_full_size_is_filled():
    assert LiveBroker._map_status({"status": "live", "size_matched": 10, "original_size": 10}) == OrderStatus.FILLED


def test_map_status_partial_fill():
    assert LiveBroker._map_status({"status": "live", "size_matched": 4, "original_size": 10}) == OrderStatus.PARTIALLY_FILLED


def test_map_status_open():
    assert LiveBroker._map_status({"status": "live"}) == OrderStatus.OPEN


def test_map_status_canceled():
    assert LiveBroker._map_status({"status": "canceled"}) == OrderStatus.CANCELED


def test_map_status_unknown_returns_none():
    assert LiveBroker._map_status("not-a-dict") is None
