from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Awaitable

import websockets

from bot.polymarket.clob import order_book_from_payload
from bot.polymarket.models import OrderBook


MarketBookHandler = Callable[[OrderBook], Awaitable[None]]


class MarketWebSocket:
    endpoint = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    def __init__(self, token_ids: list[str], on_book: MarketBookHandler):
        self.token_ids = token_ids
        self.on_book = on_book
        self.connected = False
        self._stop = asyncio.Event()

    async def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.endpoint, ping_interval=10, ping_timeout=10) as ws:
                    self.connected = True
                    await ws.send(json.dumps({"assets_ids": self.token_ids, "type": "market", "custom_feature_enabled": True}))
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        await self._handle_message(raw)
            except Exception:
                self.connected = False
                await asyncio.sleep(2)

    async def _handle_message(self, raw: str) -> None:
        payload = json.loads(raw)
        messages = payload if isinstance(payload, list) else [payload]
        for message in messages:
            if message.get("event_type") == "book":
                book = order_book_from_payload(message, token_id=str(message.get("asset_id", "")))
                book.websocket_connected = self.connected
                await self.on_book(book)

