from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class MarketType(StrEnum):
    FIVE_MINUTE = "5m"
    FIFTEEN_MINUTE = "15m"


class OutcomeSide(StrEnum):
    UP = "UP"
    DOWN = "DOWN"


class SignalAction(StrEnum):
    BUY_UP = "BUY_UP"
    BUY_DOWN = "BUY_DOWN"
    EXIT = "EXIT"
    HOLD = "HOLD"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(StrEnum):
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


class OutcomeToken(BaseModel):
    side: OutcomeSide
    token_id: str
    label: str


class UpDownMarket(BaseModel):
    market_id: str
    event_id: str | None = None
    question: str
    slug: str
    market_type: MarketType
    start_time: datetime | None = None
    end_time: datetime | None = None
    active: bool = False
    closed: bool = True
    resolved: bool = True
    liquidity: float = 0.0
    volume: float = 0.0
    tokens: dict[OutcomeSide, OutcomeToken] = Field(default_factory=dict)
    mapping_verified: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_tradeable(self) -> bool:
        return self.active and not self.closed and not self.resolved and self.mapping_verified

    @property
    def seconds_to_close(self) -> float | None:
        if not self.end_time:
            return None
        return (self.end_time - datetime.now(UTC)).total_seconds()


class BookLevel(BaseModel):
    price: float
    size: float

    @property
    def notional(self) -> float:
        return self.price * self.size


class OrderBook(BaseModel):
    market_id: str
    token_id: str
    bids: list[BookLevel] = Field(default_factory=list)
    asks: list[BookLevel] = Field(default_factory=list)
    last_trade_price: float | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    websocket_connected: bool = False

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return max(0.0, self.best_ask - self.best_bid)

    @property
    def midpoint(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def top_liquidity_usdc(self) -> float:
        return sum(level.notional for level in self.bids[:3]) + sum(level.notional for level in self.asks[:3])

    @property
    def imbalance(self) -> float:
        bid_size = sum(level.size for level in self.bids[:5])
        ask_size = sum(level.size for level in self.asks[:5])
        total = bid_size + ask_size
        return 0.0 if total <= 0 else (bid_size - ask_size) / total


class BtcMarketState(BaseModel):
    current_price: float | None = None
    market_open_price: float | None = None
    price_timestamp: datetime | None = None
    momentum_15s: float = 0.0
    momentum_60s: float = 0.0
    realized_volatility: float = 0.0

    def is_fresh(self, max_age_seconds: float) -> bool:
        if self.price_timestamp is None:
            return False
        return (datetime.now(UTC) - self.price_timestamp).total_seconds() <= max_age_seconds

    @property
    def change_since_open(self) -> float:
        if self.current_price is None or self.market_open_price in (None, 0):
            return 0.0
        return self.current_price - self.market_open_price


class MarketContext(BaseModel):
    market: UpDownMarket
    up_book: OrderBook | None = None
    down_book: OrderBook | None = None
    btc: BtcMarketState = Field(default_factory=BtcMarketState)
    geoblocked: bool = False
    kill_switch_active: bool = False
    clock_skew_seconds: float = 0.0


class Signal(BaseModel):
    action: SignalAction = SignalAction.HOLD
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    max_price: float = Field(default=0.0, ge=0.0, le=1.0)
    size_usdc: float = Field(default=0.0, ge=0.0)
    reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class OrderRequest(BaseModel):
    market_id: str
    token_id: str
    side: OrderSide
    price: float = Field(ge=0.0, le=1.0)
    size_usdc: float = Field(gt=0.0)
    reason: str = ""


class OrderRecord(BaseModel):
    order_id: str
    request: OrderRequest
    status: OrderStatus
    filled_size_usdc: float = 0.0
    avg_fill_price: float | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class FillRecord(BaseModel):
    order_id: str
    market_id: str
    token_id: str
    side: OrderSide
    price: float
    size_usdc: float
    fee_usdc: float = 0.0
    pnl_usdc: float = 0.0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
