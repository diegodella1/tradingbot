from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from datetime import UTC, datetime

import websockets

from bot.btc.candles import RollingTicks
from bot.polymarket.models import BtcMarketState


class ChainlinkBtcFeed:
    """Stream the Chainlink BTC/USD oracle price via Polymarket's Real-Time Data Socket.

    BTC up/down markets resolve against this exact stream (the market page's
    "Price to Beat" is its first tick at/after the window boundary), so it is
    the authoritative source for `market_open_price`. On connect the server
    dumps recent history for the filtered symbol, which lets a freshly started
    bot recover the boundary tick of a window already in progress.
    """

    endpoint = "wss://ws-live-data.polymarket.com"
    symbol = "btc/usd"
    ping_interval_seconds = 5.0

    def __init__(self) -> None:
        self.ticks = RollingTicks()
        self.connected = False
        self._stop = asyncio.Event()

    @property
    def current_price(self) -> float | None:
        return self.ticks.ticks[-1].price if self.ticks.ticks else None

    @property
    def last_tick_at(self) -> datetime | None:
        return self.ticks.ticks[-1].timestamp if self.ticks.ticks else None

    def is_fresh(self, max_age_seconds: float = 15.0) -> bool:
        last = self.last_tick_at
        return last is not None and (datetime.now(UTC) - last).total_seconds() <= max_age_seconds

    @property
    def state(self) -> BtcMarketState:
        latest = self.ticks.ticks[-1] if self.ticks.ticks else None
        return BtcMarketState(
            current_price=latest.price if latest else None,
            market_open_price=None,
            price_timestamp=latest.timestamp if latest else None,
            momentum_15s=self.ticks.momentum(15),
            momentum_60s=self.ticks.momentum(60),
            realized_volatility=self.ticks.realized_volatility(60),
        )

    def first_tick_at_or_after(self, boundary: datetime, max_delay_seconds: float = 120.0) -> float | None:
        """Return the Price to Beat for a window starting at `boundary`.

        Only ticks within `max_delay_seconds` of the boundary qualify: a tick
        minutes later is no longer the opening price.
        """
        for tick in self.ticks.ticks:
            if tick.timestamp >= boundary:
                if (tick.timestamp - boundary).total_seconds() <= max_delay_seconds:
                    return tick.price
                return None
        return None

    def handle_message(self, raw: str) -> None:
        """Ingest one RTDS frame; supports live updates and the initial history dump.

        The server labels the initial dump with the generic topic
        "crypto_prices" even for a chainlink subscription, so both topics are
        accepted; the slash-separated symbol ("btc/usd") is unique to the
        Chainlink source (Binance uses "btcusdt").
        """
        if not raw or raw == "PONG":
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        messages = payload if isinstance(payload, list) else [payload]
        for message in messages:
            if not isinstance(message, dict):
                continue
            if message.get("topic") not in ("crypto_prices_chainlink", "crypto_prices"):
                continue
            body = message.get("payload") or {}
            if str(body.get("symbol", "")).lower() != self.symbol:
                continue
            points = body.get("data") if isinstance(body.get("data"), list) else [body]
            for point in points:
                self._add_point(point)

    def _add_point(self, point: dict) -> None:
        try:
            price = float(point["value"])
            ts = datetime.fromtimestamp(float(point["timestamp"]) / 1000.0, UTC)
        except (KeyError, TypeError, ValueError):
            return
        last = self.last_tick_at
        if last is not None and ts <= last:
            return  # history dumps can overlap live updates
        self.ticks.add(price, ts)

    async def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        subscribe = {
            "action": "subscribe",
            "subscriptions": [
                {"topic": "crypto_prices_chainlink", "type": "*", "filters": json.dumps({"symbol": self.symbol})}
            ],
        }
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.endpoint, ping_interval=None) as ws:
                    self.connected = True
                    await ws.send(json.dumps(subscribe))
                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    try:
                        async for raw in ws:
                            if self._stop.is_set():
                                break
                            self.handle_message(raw if isinstance(raw, str) else raw.decode())
                    finally:
                        ping_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await ping_task
            except Exception:
                self.connected = False
                await asyncio.sleep(2)
        self.connected = False

    async def _ping_loop(self, ws) -> None:
        # RTDS drops clients that do not send an application-level PING every 5s.
        while True:
            await asyncio.sleep(self.ping_interval_seconds)
            await ws.send("PING")
