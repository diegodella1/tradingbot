from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime

from bot.btc.chainlink_feed import ChainlinkBtcFeed
from bot.btc.price_feed import CoinbaseBtcFeed
from bot.config import Settings
from bot.polymarket.models import OrderBook
from bot.polymarket.websocket import MarketWebSocket


class BookCache:
    """In-memory latest order books keyed by token id, updated by the WS handler."""

    def __init__(self) -> None:
        self._books: dict[str, OrderBook] = {}

    async def update(self, book: OrderBook) -> None:
        self._books[book.token_id] = book

    def get(self, token_id: str) -> OrderBook | None:
        return self._books.get(token_id)

    def is_fresh(self, token_id: str, max_age_seconds: float) -> bool:
        book = self._books.get(token_id)
        if book is None:
            return False
        return (datetime.now(UTC) - book.timestamp).total_seconds() <= max_age_seconds


class RealtimeMarketData:
    """Manage streaming BTC price and CLOB order books, replacing per-cycle REST polls.

    Subscriptions follow the recurring markets: when the active token set changes,
    the CLOB websocket is torn down and re-subscribed. All socket loops already live
    in ``CoinbaseBtcFeed`` and ``MarketWebSocket``; this class only orchestrates them
    and exposes freshness + connection state for the risk gate.
    """

    def __init__(self, settings: Settings, btc_feed: CoinbaseBtcFeed):
        self.settings = settings
        self.btc_feed = btc_feed
        self.chainlink = ChainlinkBtcFeed()  # resolution-source oracle stream (Price to Beat)
        self.cache = BookCache()
        # Feed-degradation tracking (used by the loops to alert on WS -> REST fallback).
        self.rest_fallback_active = False
        self.ever_streamed = False
        self._btc_task: asyncio.Task | None = None
        self._chainlink_task: asyncio.Task | None = None
        self._market_ws: MarketWebSocket | None = None
        self._market_task: asyncio.Task | None = None
        self._token_ids: tuple[str, ...] = ()

    async def start(self) -> None:
        if self._btc_task is None:
            self._btc_task = asyncio.create_task(self.btc_feed.run())
        if self._chainlink_task is None:
            self._chainlink_task = asyncio.create_task(self.chainlink.run())

    async def ensure_subscription(self, token_ids: list[str]) -> None:
        wanted = tuple(sorted({token for token in token_ids if token}))
        if wanted == self._token_ids and self._market_task is not None:
            return
        await self._stop_market_ws()
        if not wanted:
            return
        self._token_ids = wanted
        self._market_ws = MarketWebSocket(list(wanted), self.cache.update)
        self._market_task = asyncio.create_task(self._market_ws.run())

    @property
    def connected(self) -> bool:
        return self.btc_connected and self.market_connected

    @property
    def btc_connected(self) -> bool:
        return bool(self.btc_feed.connected)

    @property
    def market_connected(self) -> bool:
        return bool(self._market_ws is not None and self._market_ws.connected)

    def get_book(self, token_id: str) -> OrderBook | None:
        if self.cache.is_fresh(token_id, self.settings.websocket_book_max_age_seconds):
            return self.cache.get(token_id)
        return None

    async def _stop_market_ws(self) -> None:
        if self._market_ws is not None:
            with suppress(Exception):
                await self._market_ws.stop()
        if self._market_task is not None:
            self._market_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._market_task
        self._market_ws = None
        self._market_task = None
        self._token_ids = ()

    async def stop(self) -> None:
        await self._stop_market_ws()
        with suppress(Exception):
            await self.btc_feed.stop()
        if self._btc_task is not None:
            self._btc_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._btc_task
        self._btc_task = None
        with suppress(Exception):
            await self.chainlink.stop()
        if self._chainlink_task is not None:
            self._chainlink_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._chainlink_task
        self._chainlink_task = None
