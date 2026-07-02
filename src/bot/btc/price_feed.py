from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import websockets

from bot.btc.candles import RollingTicks
from bot.config import Settings
from bot.polymarket.models import BtcMarketState


class CoinbaseBtcFeed:
    ws_endpoint = "wss://ws-feed.exchange.coinbase.com"
    rest_endpoint = "https://api.exchange.coinbase.com/products/{symbol}/ticker"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.ticks = RollingTicks()
        self.market_open_price: float | None = None
        self.connected = False
        self._stop = asyncio.Event()

    def set_market_open_price(self, price: float | None = None) -> None:
        self.market_open_price = price if price is not None else self.current_price

    @property
    def current_price(self) -> float | None:
        return self.ticks.ticks[-1].price if self.ticks.ticks else None

    @property
    def state(self) -> BtcMarketState:
        latest = self.ticks.ticks[-1] if self.ticks.ticks else None
        return BtcMarketState(
            current_price=latest.price if latest else None,
            market_open_price=self.market_open_price,
            price_timestamp=latest.timestamp if latest else None,
            momentum_15s=self.ticks.momentum(15),
            momentum_60s=self.ticks.momentum(60),
            realized_volatility=self.ticks.realized_volatility(60),
        )

    async def poll_once(self) -> BtcMarketState:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(self.rest_endpoint.format(symbol=self.settings.btc_symbol))
            response.raise_for_status()
            price = float(response.json()["price"])
        self.ticks.add(price, datetime.now(UTC))
        if self.market_open_price is None:
            self.market_open_price = price
        return self.state

    async def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        subscribe = {
            "type": "subscribe",
            "product_ids": [self.settings.btc_symbol],
            "channels": ["ticker"],
        }
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.ws_endpoint, ping_interval=10, ping_timeout=10) as ws:
                    self.connected = True
                    await ws.send(json.dumps(subscribe))
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        payload = json.loads(raw)
                        if payload.get("type") == "ticker" and payload.get("price"):
                            price = float(payload["price"])
                            ts = datetime.fromisoformat(str(payload.get("time", "")).replace("Z", "+00:00")) if payload.get("time") else datetime.now(UTC)
                            self.ticks.add(price, ts)
                            if self.market_open_price is None:
                                self.market_open_price = price
            except Exception:
                self.connected = False
                await self.poll_once()
                await asyncio.sleep(2)

