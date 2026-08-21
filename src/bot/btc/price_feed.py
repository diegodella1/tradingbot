from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import websockets

from bot.btc.candles import RollingTicks
from bot.config import Settings
from bot.polymarket.models import BtcMarketState


def exception_detail(exc: BaseException) -> str:
    message = str(exc).strip() or "no message"
    return f"{type(exc).__name__}: {message}"


class CoinbaseBtcFeed:
    ws_endpoint = "wss://ws-feed.exchange.coinbase.com"
    rest_endpoint = "https://api.exchange.coinbase.com/products/{symbol}/ticker"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.ticks = RollingTicks()
        self.market_open_price: float | None = None
        self.connected = False
        self._stop = asyncio.Event()
        self._rest_client: httpx.AsyncClient | None = None
        self.reconnect_count = 0
        self.last_error: str | None = None

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
        if self._rest_client is None:
            self._rest_client = httpx.AsyncClient(timeout=5.0)
        response = await self._rest_client.get(self.rest_endpoint.format(symbol=self.settings.btc_symbol))
        response.raise_for_status()
        price = float(response.json()["price"])
        self.ticks.add(price, datetime.now(UTC))
        if self.market_open_price is None:
            self.market_open_price = price
        return self.state

    async def stop(self) -> None:
        self._stop.set()
        if self._rest_client is not None:
            await self._rest_client.aclose()
            self._rest_client = None

    async def run(self) -> None:
        subscribe = {
            "type": "subscribe",
            "product_ids": [self.settings.btc_symbol],
            "channels": ["ticker"],
        }
        backoff_seconds = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.ws_endpoint, ping_interval=10, ping_timeout=10) as ws:
                    self.connected = True
                    self.last_error = None
                    backoff_seconds = 1.0
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
                    if not self._stop.is_set():
                        raise ConnectionError("Coinbase websocket stream closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.reconnect_count += 1
                self.last_error = exception_detail(exc)
                try:
                    await self.poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception as fallback_exc:
                    self.last_error = f"websocket={self.last_error}; rest={exception_detail(fallback_exc)}"
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff_seconds)
                except TimeoutError:
                    pass
                backoff_seconds = min(backoff_seconds * 2, self.settings.feed_reconnect_max_seconds)
            finally:
                self.connected = False
