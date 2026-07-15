from __future__ import annotations

import json

from bot.polymarket.models import FillRecord, OrderRecord, OrderRequest, OrderSide, OrderStatus, Signal, SignalAction
from bot.storage.db import connect, init_db
from bot.storage.repositories import Repository


def test_repository_persists_policy_metadata(settings, market):
    init_db(settings.sqlite_path)
    metadata = {
        "policy_version": settings.policy_version,
        "config_snapshot": settings.strategy_config_snapshot(),
        "estimated_probability": 0.82,
        "break_even_probability_after_fees": 0.71,
        "net_edge_cents": 11.0,
    }
    with connect(settings.sqlite_path) as conn:
        repo = Repository(conn)
        repo.save_market(market)
        signal = Signal(action=SignalAction.BUY_UP, confidence=0.9, max_price=0.72, size_usdc=1, reason="test", metadata=metadata)
        repo.save_signal(market.market_id, signal)
        repo.save_strategy_decision(market, signal)
        request = OrderRequest(market_id=market.market_id, token_id="up-token", side=OrderSide.BUY, price=0.72, size_usdc=1, reason="test", metadata=metadata)
        repo.save_order(OrderRecord(order_id="o1", request=request, status=OrderStatus.FILLED, filled_size_usdc=1, avg_fill_price=0.70))
        repo.save_fill(FillRecord(order_id="o1", market_id=market.market_id, token_id="up-token", side=OrderSide.BUY, price=0.70, size_usdc=1, fee_usdc=0.01, metadata=metadata))

        row = conn.execute("SELECT policy_version, metadata_json FROM signals").fetchone()
        assert row["policy_version"] == settings.policy_version
        assert json.loads(row["metadata_json"])["estimated_probability"] == 0.82
        order = conn.execute("SELECT policy_version FROM orders").fetchone()
        assert order["policy_version"] == settings.policy_version
        position = conn.execute("SELECT policy_version, estimated_probability, break_even_probability, net_edge_cents FROM positions").fetchone()
        assert position["policy_version"] == settings.policy_version
        assert position["estimated_probability"] == 0.82
        assert position["break_even_probability"] == 0.71
        assert position["net_edge_cents"] == 11.0
