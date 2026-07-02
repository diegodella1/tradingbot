from __future__ import annotations

from bot.execution.paper_broker import PaperBroker, polymarket_taker_fee_usdc
from bot.polymarket.models import OrderRequest, OrderSide, OrderStatus


def test_paper_broker_simulates_partial_fill(settings, book):
    settings.paper_fill_ratio = 0.5
    broker = PaperBroker(settings)
    order = broker.place_limit_order(
        OrderRequest(market_id="m1", token_id="up-token", side=OrderSide.BUY, price=0.53, size_usdc=100),
        book,
    )

    assert order.status == OrderStatus.PARTIALLY_FILLED
    assert order.filled_size_usdc == 25.5
    assert order.avg_fill_price == 0.52
    assert len(broker.fills) == 1


def test_polymarket_crypto_taker_fee_formula_rounds_to_5_decimals():
    assert polymarket_taker_fee_usdc(shares=100, price=0.5, fee_rate=0.07) == 1.75
    assert polymarket_taker_fee_usdc(shares=100, price=0.3, fee_rate=0.07) == 1.47


def test_paper_broker_records_fee(settings, book):
    broker = PaperBroker(settings)
    order = broker.place_limit_order(
        OrderRequest(market_id="m1", token_id="up-token", side=OrderSide.BUY, price=0.53, size_usdc=1),
        book,
    )

    assert order.status == OrderStatus.FILLED
    assert broker.fills[0].fee_usdc > 0
