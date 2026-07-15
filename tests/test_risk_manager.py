from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.execution.risk_manager import RiskManager, RiskState
from bot.execution.order_manager import OrderManager
from bot.execution.paper_broker import PaperBroker
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


def test_hourly_trade_limit_blocks_trading(settings, context):
    settings.max_trades_per_hour = 4
    risk = RiskManager(settings, RiskState(trades_last_hour=4))
    decision = risk.validate(buy_signal(), context)
    assert decision.approved is False
    assert "hourly trade limit" in decision.reason


def test_break_even_guard_blocks_financially_bad_signal(settings, context):
    signal = Signal(
        action=SignalAction.BUY_UP,
        confidence=0.9,
        max_price=0.72,
        size_usdc=1,
        reason="test",
        metadata={
            "estimated_probability": 0.73,
            "break_even_probability_after_fees": 0.70,
            "min_net_edge_cents": 5.0,
        },
    )
    decision = RiskManager(settings).validate(signal, context)
    assert decision.approved is False
    assert "break-even" in decision.reason


def test_recent_5m_losses_pause_scout(settings, context):
    settings.disable_5m_after_recent_loss_usdc = 2
    risk = RiskManager(settings, RiskState(recent_5m_settled_count=3, recent_5m_pnl_usdc=-2.1))
    decision = risk.validate(buy_signal(), context)
    assert decision.approved is False
    assert "5m scout paused" in decision.reason


def test_recent_drawdown_reduces_size(settings, context):
    settings.drawdown_lookback_trades = 10
    settings.drawdown_pause_loss_usdc = 3
    settings.drawdown_size_multiplier = 0.5
    risk = RiskManager(
        settings,
        RiskState(
            recent_settled_count=10,
            recent_pnl_usdc=-3.5,
            last_settled_at=datetime.now(UTC),
        ),
    )
    decision = risk.validate(buy_signal(), context)
    assert decision.approved is True
    assert "drawdown size reduction" in decision.reason
    assert decision.size_multiplier == 0.5


def test_order_manager_applies_drawdown_size_multiplier(settings, context):
    settings.drawdown_lookback_trades = 10
    settings.drawdown_pause_loss_usdc = 3
    settings.drawdown_size_multiplier = 0.5
    risk = RiskManager(
        settings,
        RiskState(
            recent_settled_count=10,
            recent_pnl_usdc=-3.5,
            last_settled_at=datetime.now(UTC),
        ),
    )
    broker = PaperBroker(settings)
    signal = Signal(action=SignalAction.BUY_UP, confidence=0.9, max_price=0.52, size_usdc=2, reason="test")
    order, decision = OrderManager(risk, broker).execute_paper_signal(signal, context)

    assert decision.approved is True
    assert order is not None
    assert order.request.size_usdc == 1


def test_old_consecutive_losses_do_not_permanently_block(settings, context):
    risk = RiskManager(
        settings,
        RiskState(
            consecutive_losses=settings.max_consecutive_losses,
            last_loss_at=datetime.now(UTC) - timedelta(seconds=settings.cooldown_after_loss_seconds + 1),
        ),
    )
    decision = risk.validate(buy_signal(), context)
    assert decision.approved is True


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
