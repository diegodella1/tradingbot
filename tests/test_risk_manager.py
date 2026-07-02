from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.execution.risk_manager import RiskManager, RiskState
from bot.polymarket.models import OutcomeSide, Signal, SignalAction


def buy_signal():
    return Signal(action=SignalAction.BUY_UP, confidence=0.9, max_price=0.52, size_usdc=1, reason="test")


def test_risk_manager_approves_safe_trade(settings, context):
    decision = RiskManager(settings).validate(buy_signal(), context)
    assert decision.approved is True


def test_kill_switch_blocks_execution(settings, context):
    settings.kill_switch_file.write_text("stop")
    decision = RiskManager(settings).validate(buy_signal(), context)
    assert decision.approved is False
    assert "kill switch" in decision.reason


def test_geoblock_blocked_response_blocks_live_trading(settings, context):
    context.geoblocked = True
    decision = RiskManager(settings).validate(buy_signal(), context)
    assert decision.approved is False
    assert "geoblock" in decision.reason


def test_stale_btc_feed_blocks_trading(settings, context):
    context.btc.price_timestamp = datetime.now(UTC) - timedelta(seconds=10)
    decision = RiskManager(settings).validate(buy_signal(), context)
    assert decision.approved is False
    assert "stale" in decision.reason


def test_wide_spread_blocks_trading(settings, context):
    context.up_book.asks[0].price = 0.60
    decision = RiskManager(settings).validate(buy_signal(), context)
    assert decision.approved is False
    assert "spread" in decision.reason


def test_ambiguous_market_blocks_trading(settings, context):
    context.market.mapping_verified = False
    decision = RiskManager(settings).validate(buy_signal(), context)
    assert decision.approved is False
    assert "ambiguous" in decision.reason


def test_future_market_blocks_trading(settings, context):
    context.market.start_time = datetime.now(UTC) + timedelta(minutes=1)
    context.market.end_time = datetime.now(UTC) + timedelta(minutes=6)
    decision = RiskManager(settings).validate(buy_signal(), context)
    assert decision.approved is False
    assert "not started" in decision.reason


def test_existing_market_position_blocks_duplicate_entry(settings, context):
    risk = RiskManager(settings, RiskState(market_exposure={context.market.market_id: 1.0}))
    decision = risk.validate(buy_signal(), context)
    assert decision.approved is False
    assert "already" in decision.reason


def test_daily_loss_blocks_trading(settings, context):
    risk = RiskManager(settings, RiskState(daily_pnl_usdc=-settings.max_daily_loss_usdc))
    decision = risk.validate(buy_signal(), context)
    assert decision.approved is False
    assert "daily loss" in decision.reason


def test_token_exposure_limit_blocks_trading(settings, context):
    token_id = context.market.tokens[OutcomeSide.UP].token_id
    risk = RiskManager(settings, RiskState(token_exposure={token_id: settings.max_token_position_usdc}))
    decision = risk.validate(buy_signal(), context)
    assert decision.approved is False
    assert "token exposure" in decision.reason


def test_consecutive_losses_block_trading(settings, context):
    risk = RiskManager(settings, RiskState(consecutive_losses=settings.max_consecutive_losses))
    decision = risk.validate(buy_signal(), context)
    assert decision.approved is False
    assert "consecutive loss" in decision.reason


@pytest.mark.asyncio
async def test_live_broker_rejects_when_disabled(settings, context):
    from bot.execution.live_broker import LiveBroker
    from bot.polymarket.models import OrderRequest, OrderSide

    broker = LiveBroker(settings, RiskManager(settings))
    order = await broker.place_limit_order(
        OrderRequest(market_id="m1", token_id="up-token", side=OrderSide.BUY, price=0.52, size_usdc=1),
        context,
        buy_signal(),
    )

    assert order.status.value == "REJECTED"
