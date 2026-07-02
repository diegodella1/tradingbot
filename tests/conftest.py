from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.config import Settings
from bot.polymarket.models import BookLevel, BtcMarketState, MarketContext, MarketType, OrderBook, OutcomeSide, OutcomeToken, UpDownMarket


@pytest.fixture()
def settings(tmp_path):
    return Settings(sqlite_path=tmp_path / "bot.sqlite3", kill_switch_file=tmp_path / "KILL_SWITCH")


@pytest.fixture()
def market():
    return UpDownMarket(
        market_id="m1",
        event_id="e1",
        question="Bitcoin Up or Down - 5 minute",
        slug="bitcoin-up-or-down-5m",
        market_type=MarketType.FIVE_MINUTE,
        start_time=datetime.now(UTC) - timedelta(minutes=1),
        end_time=datetime.now(UTC) + timedelta(minutes=3),
        active=True,
        closed=False,
        resolved=False,
        liquidity=1000,
        volume=5000,
        mapping_verified=True,
        tokens={
            OutcomeSide.UP: OutcomeToken(side=OutcomeSide.UP, token_id="up-token", label="Up"),
            OutcomeSide.DOWN: OutcomeToken(side=OutcomeSide.DOWN, token_id="down-token", label="Down"),
        },
    )


@pytest.fixture()
def book():
    return OrderBook(
        market_id="m1",
        token_id="up-token",
        bids=[BookLevel(price=0.49, size=100)],
        asks=[BookLevel(price=0.51, size=100)],
        websocket_connected=True,
    )


@pytest.fixture()
def context(market, book):
    return MarketContext(
        market=market,
        up_book=book,
        down_book=OrderBook(
            market_id="m1",
            token_id="down-token",
            bids=[BookLevel(price=0.48, size=100)],
            asks=[BookLevel(price=0.52, size=100)],
            websocket_connected=True,
        ),
        btc=BtcMarketState(
            current_price=101,
            market_open_price=100,
            price_timestamp=datetime.now(UTC),
            momentum_15s=0.001,
            momentum_60s=0.002,
        ),
    )

