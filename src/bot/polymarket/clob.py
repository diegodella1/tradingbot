from __future__ import annotations

from typing import Any

import httpx

from bot.config import Settings
from bot.polymarket.models import BookLevel, OrderBook


class ClobClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(base_url=settings.polymarket_host, timeout=10.0)

    async def close(self) -> None:
        await self.client.aclose()

    async def get_order_book(self, token_id: str, market_id: str = "") -> OrderBook:
        response = await self.client.get("/book", params={"token_id": token_id})
        response.raise_for_status()
        return order_book_from_payload(response.json(), token_id=token_id, market_id=market_id)

    async def get_server_time(self) -> int:
        response = await self.client.get("/time")
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            return int(payload.get("serverTime") or payload.get("time") or payload.get("timestamp"))
        return int(payload)


def _levels(payload: list[dict[str, Any]]) -> list[BookLevel]:
    levels = [BookLevel(price=float(item["price"]), size=float(item["size"])) for item in payload if float(item.get("size", 0)) > 0]
    return sorted(levels, key=lambda level: level.price)


def order_book_from_payload(payload: dict[str, Any], token_id: str, market_id: str = "") -> OrderBook:
    bids = _levels(payload.get("bids") or payload.get("buys") or [])
    asks = _levels(payload.get("asks") or payload.get("sells") or [])
    bids.sort(key=lambda level: level.price, reverse=True)
    asks.sort(key=lambda level: level.price)
    return OrderBook(
        market_id=str(payload.get("market") or payload.get("market_id") or market_id),
        token_id=str(payload.get("asset_id") or payload.get("token_id") or token_id),
        bids=bids,
        asks=asks,
        last_trade_price=float(payload["last_trade_price"]) if payload.get("last_trade_price") else None,
    )

