from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.config import Settings
from bot.polymarket.gamma import GammaClient, convert_gamma_market, discover_from_payload, markets_from_event, recurring_slug_candidates, recurring_slug_window
from bot.polymarket.models import MarketType, OutcomeSide


def test_market_discovery_filters_and_maps_tokens():
    payload = [
        {
            "id": "1",
            "conditionId": "condition-1",
            "eventId": "event-1",
            "question": "Bitcoin Up or Down - 5 minute",
            "slug": "bitcoin-up-or-down-5m",
            "active": True,
            "closed": False,
            "resolved": False,
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["up", "down"]',
        },
        {"id": "2", "question": "Ethereum Up or Down - 5 minute", "active": True},
    ]

    result = discover_from_payload(payload, ["5m"])

    assert len(result[MarketType.FIVE_MINUTE]) == 1
    market = result[MarketType.FIVE_MINUTE][0]
    assert market.tokens[OutcomeSide.UP].token_id == "up"
    assert market.tokens[OutcomeSide.DOWN].token_id == "down"
    assert market.mapping_verified is True


def test_ambiguous_outcome_mapping_is_not_tradeable():
    market = convert_gamma_market(
        {
            "id": "1",
            "question": "Bitcoin Up or Down - 15 minute",
            "slug": "bitcoin-up-or-down-15m",
            "active": True,
            "closed": False,
            "outcomes": '["Moon", "Crash"]',
            "clobTokenIds": '["a", "b"]',
        }
    )

    assert market is not None
    assert market.mapping_verified is False
    assert market.is_tradeable is False


def test_resolution_source_does_not_mark_market_resolved():
    market = convert_gamma_market(
        {
            "id": "1",
            "conditionId": "condition-1",
            "question": "Bitcoin Up or Down - 5 minute",
            "slug": "btc-updown-5m-123",
            "active": True,
            "closed": False,
            "resolutionSource": "https://data.chain.link/streams/btc-usd",
            "endDate": (datetime.now(UTC) + timedelta(minutes=4)).isoformat(),
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["up", "down"]',
            "acceptingOrders": True,
            "enableOrderBook": True,
        }
    )

    assert market is not None
    assert market.resolved is False
    assert market.is_tradeable is True


def test_expired_market_is_filtered_even_if_active():
    result = discover_from_payload(
        [
            {
                "id": "1",
                "conditionId": "condition-1",
                "question": "Bitcoin Up or Down - 5 minute",
                "slug": "btc-updown-5m-123",
                "active": True,
                "closed": False,
                "endDate": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
                "outcomes": '["Up", "Down"]',
                "clobTokenIds": '["up", "down"]',
                "acceptingOrders": True,
                "enableOrderBook": True,
            }
        ],
        ["5m"],
    )

    assert result[MarketType.FIVE_MINUTE] == []


def test_recurring_slug_window_uses_slug_timestamp_as_market_start():
    window = recurring_slug_window("btc-updown-5m-1782915900")

    assert window is not None
    market_type, start_time, end_time = window
    assert market_type == MarketType.FIVE_MINUTE
    assert start_time == datetime(2026, 7, 1, 14, 25, tzinfo=UTC)
    assert end_time == datetime(2026, 7, 1, 14, 30, tzinfo=UTC)


def test_recurring_slug_candidates_generate_current_and_next_windows():
    now = datetime(2026, 7, 1, 14, 28, 44, tzinfo=UTC)

    assert recurring_slug_candidates(MarketType.FIVE_MINUTE, now) == [
        "btc-updown-5m-1782915600",
        "btc-updown-5m-1782915900",
        "btc-updown-5m-1782916200",
        "btc-updown-5m-1782916500",
    ]
    assert recurring_slug_candidates(MarketType.FIFTEEN_MINUTE, now) == [
        "btc-updown-15m-1782914400",
        "btc-updown-15m-1782915300",
        "btc-updown-15m-1782916200",
        "btc-updown-15m-1782917100",
    ]


def test_gamma_event_slug_probe_payload_maps_nested_market_tokens():
    event = {
        "id": "651641",
        "ticker": "btc-updown-5m-1782915900",
        "slug": "btc-updown-5m-1782915900",
        "title": "Bitcoin Up or Down - July 1, 10:25AM-10:30AM ET",
        "startDate": "2026-06-30T14:32:41.898478Z",
        "endDate": "2026-07-01T14:30:00Z",
        "active": True,
        "closed": False,
        "archived": False,
        "enableOrderBook": True,
        "markets": [
            {
                "id": "2743853",
                "conditionId": "condition-5m",
                "question": "Bitcoin Up or Down - July 1, 10:25AM-10:30AM ET",
                "slug": "btc-updown-5m-1782915900",
                "active": True,
                "closed": False,
                "acceptingOrders": True,
                "enableOrderBook": True,
                "liquidity": "22498.8181",
                "volume": "5066.305776000003",
                "outcomes": '["Up", "Down"]',
                "clobTokenIds": '["up-token", "down-token"]',
            }
        ],
    }

    raw_markets = markets_from_event(event, source="slug_probe")
    market = convert_gamma_market(raw_markets[0])

    assert market is not None
    assert market.market_id == "condition-5m"
    assert market.event_id == "651641"
    assert market.start_time == datetime(2026, 7, 1, 14, 25, tzinfo=UTC)
    assert market.end_time == datetime(2026, 7, 1, 14, 30, tzinfo=UTC)
    assert market.tokens[OutcomeSide.UP].token_id == "up-token"
    assert market.tokens[OutcomeSide.DOWN].token_id == "down-token"
    assert market.mapping_verified is True


@pytest.mark.asyncio
async def test_gamma_discovery_skips_full_scan_when_slug_probe_finds_all_types(monkeypatch):
    settings = Settings(market_types=["5m"])
    client = GammaClient(settings)
    raw_market = {
        "conditionId": "condition-fast-path",
        "question": "Bitcoin Up or Down - 5 minute",
        "slug": "bitcoin-up-or-down-5m",
        "active": True,
        "closed": False,
        "resolved": False,
        "endDate": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "outcomes": '["Up", "Down"]',
        "clobTokenIds": '["up", "down"]',
    }

    async def slug_markets():
        return [raw_market]

    async def unexpected_full_scan(*args, **kwargs):
        raise AssertionError("full market scan should not run")

    monkeypatch.setattr(client, "recurring_slug_markets", slug_markets)
    monkeypatch.setattr(client, "list_markets", unexpected_full_scan)
    monkeypatch.setattr(client, "list_events", unexpected_full_scan)
    try:
        result = await client.discover_btc_updown()
    finally:
        await client.close()

    assert len(result[MarketType.FIVE_MINUTE]) == 1
    assert client.last_raw_markets == [raw_market]
