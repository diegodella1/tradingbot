from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bot.btc.price_feed import CoinbaseBtcFeed
from bot.execution.order_manager import OrderManager
from bot.execution.paper_broker import PaperBroker
from bot.execution.risk_manager import RiskManager
from bot.polymarket.clob import ClobClient
from bot.polymarket.gamma import GammaClient
from bot.polymarket.models import BookLevel, BtcMarketState, MarketType, OrderBook, OutcomeSide, Signal, SignalAction
from bot.main import run_paper_loop
from bot.storage.db import connect, init_db
from bot.strategy.calibration import FEATURE_NAMES, ProbabilityModel
from bot.strategy.momentum_book_imbalance import MomentumBookImbalanceStrategy


def _low_prob_model(path) -> None:
    n_extras = len(FEATURE_NAMES) - 1
    ProbabilityModel(weights=[0.0] * n_extras, bias=-10.0, means=[0.0] * n_extras, stds=[1.0] * n_extras).save(path)


class _FakeGamma:
    def __init__(self, settings, market):
        self.market = market

    async def discover_btc_updown(self):
        return {MarketType.FIVE_MINUTE: [self.market], MarketType.FIFTEEN_MINUTE: []}

    async def close(self):
        return None


class _FakeClob:
    async def get_order_book(self, token_id, market_id=""):
        return OrderBook(
            market_id=market_id,
            token_id=token_id,
            bids=[BookLevel(price=0.49, size=100)],
            asks=[BookLevel(price=0.51, size=100)],
        )

    async def close(self):
        return None


async def _fake_poll_once(self):
    return BtcMarketState(current_price=101, market_open_price=100, price_timestamp=datetime.now(UTC), momentum_15s=0.01, momentum_60s=0.02)


def test_evaluate_exit_holds_when_hold_to_resolution(settings, context):
    settings.hold_to_resolution = True
    settings.enable_exit_signals = True
    signal = MomentumBookImbalanceStrategy(settings).evaluate_exit(context, OutcomeSide.UP, 0.5)
    assert signal.action == SignalAction.HOLD


def test_evaluate_exit_emits_exit_on_low_probability(tmp_path, settings, context):
    model_path = tmp_path / "model.json"
    _low_prob_model(model_path)
    settings.probability_model_path = model_path
    settings.hold_to_resolution = False
    settings.enable_exit_signals = True

    signal = MomentumBookImbalanceStrategy(settings).evaluate_exit(context, OutcomeSide.UP, 0.5)

    assert signal.action == SignalAction.EXIT
    assert signal.metadata["estimated_probability"] < settings.exit_min_probability


def test_execute_exit_signal_closes_position(settings, context):
    broker = PaperBroker(settings)
    manager = OrderManager(RiskManager(settings), broker)
    exit_signal = Signal(action=SignalAction.EXIT, max_price=0.0, reason="test exit")
    position = {"side": OutcomeSide.UP.value, "token_id": "up-token", "shares": 2.0, "cost_usdc": 1.0, "fee_usdc": 0.0}

    order, realized = manager.execute_exit_signal(exit_signal, context, position)

    assert order is not None
    assert order.filled_size_usdc > 0
    # Bought 2 shares for 1.0 (avg 0.50); selling at bid 0.49 minus 1c slippage minus
    # fees nets a small loss, proving realized PnL is computed on close.
    assert round(realized, 2) == -0.07


@pytest.mark.asyncio
async def test_paper_loop_exits_open_position(tmp_path, monkeypatch, settings, market):
    model_path = tmp_path / "model.json"
    _low_prob_model(model_path)
    settings.probability_model_path = model_path
    settings.enable_experimental_strategy = True
    settings.enable_exit_signals = True
    settings.hold_to_resolution = False
    settings.paper_loop_interval_seconds = 0

    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        conn.execute("INSERT OR REPLACE INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (market.market_id, market.event_id, market.question, market.slug, market.market_type.value,
                      market.start_time.isoformat(), market.end_time.isoformat(), 100, 10, 1, "{}"))
        conn.execute(
            """
            INSERT INTO positions (market_id, token_id, size_usdc, avg_price, shares, fee_usdc, status, realized_pnl_usdc, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'OPEN', 0, ?)
            """,
            (market.market_id, "up-token", 1.0, 0.5, 2.0, 0.0, datetime.now(UTC).isoformat()),
        )
        conn.commit()

    monkeypatch.setattr(GammaClient, "__init__", lambda self, settings: setattr(self, "_fake", _FakeGamma(settings, market)))
    monkeypatch.setattr(GammaClient, "discover_btc_updown", lambda self: self._fake.discover_btc_updown())
    monkeypatch.setattr(GammaClient, "close", lambda self: self._fake.close())
    monkeypatch.setattr(ClobClient, "__init__", lambda self, settings: None)
    monkeypatch.setattr(ClobClient, "get_order_book", _FakeClob().get_order_book)
    monkeypatch.setattr(ClobClient, "close", _FakeClob().close)
    monkeypatch.setattr(CoinbaseBtcFeed, "poll_once", _fake_poll_once)

    await run_paper_loop(settings, max_cycles=1)

    with connect(settings.sqlite_path) as conn:
        status = conn.execute("SELECT status FROM positions WHERE market_id = ?", (market.market_id,)).fetchone()["status"]
        sells = conn.execute("SELECT COUNT(*) FROM fills WHERE side = 'SELL'").fetchone()[0]

    assert status == "CLOSED"
    assert sells == 1
