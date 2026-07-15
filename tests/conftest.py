from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.config import Settings
from bot.polymarket.models import BookLevel, BtcMarketState, MarketContext, MarketType, OrderBook, OutcomeSide, OutcomeToken, UpDownMarket


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        sqlite_path=tmp_path / "bot.sqlite3",
        kill_switch_file=tmp_path / "KILL_SWITCH",
        # Keep tests hermetic: never pick up a real trained model from the repo root.
        probability_model_path=tmp_path / "probability_model.json",
        enable_experimental_strategy=False,
        min_seconds_to_close=45,
        min_seconds_to_close_5m=45,
        min_seconds_to_close_15m=45,
        min_entry_price=0.10,
        min_entry_price_15m=0.10,
        max_entry_price=0.90,
        min_estimated_probability=0.60,
        min_probability_15m=0.60,
        min_probability_5m=0.60,
        min_net_edge_cents=1,
        min_net_edge_15m_cents=1,
        min_net_edge_5m_cents=1,
        min_confidence=0.10,
        min_confidence_5m=0.10,
        min_book_imbalance=0.05,
        min_book_imbalance_5m=0.05,
        enable_5m_scout=True,
        danger_zone_min_probability=0.60,
        danger_zone_min_net_edge_cents=1,
        high_price_min_probability=0.60,
        high_price_min_net_edge_cents=1,
        size_tier_base_usdc=0.75,
        size_tier_good_usdc=1.0,
        size_tier_strong_usdc=1.5,
        size_tier_max_usdc=2.0,
        min_kelly_size_usdc=0.01,
        max_trades_per_hour=100,
        disable_5m_after_recent_loss_usdc=999,
        drawdown_pause_loss_usdc=999,
    )


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
