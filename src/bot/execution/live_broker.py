from __future__ import annotations

from bot.config import Settings
from bot.execution.risk_manager import RiskDecision, RiskManager
from bot.polymarket.geoblock import GeoblockClient
from bot.polymarket.models import MarketContext, OrderRecord, OrderRequest, OrderSide, OrderStatus, Signal


class LiveTradingDisabled(RuntimeError):
    pass


class LiveBroker:
    def __init__(self, settings: Settings, risk_manager: RiskManager):
        self.settings = settings
        self.risk_manager = risk_manager
        self.client = None

    async def startup_check(self, context: MarketContext | None = None) -> RiskDecision:
        if not self.settings.enable_live_trading:
            return RiskDecision(False, "ENABLE_LIVE_TRADING is false")
        if not self.settings.live_auth_ready:
            return RiskDecision(False, "live auth credentials incomplete")
        geoblock = await GeoblockClient(self.settings).check()
        if geoblock.blocked:
            return RiskDecision(False, geoblock.reason)
        return RiskDecision(True, "startup approved")

    def _client(self):
        if not self.settings.enable_live_trading:
            raise LiveTradingDisabled("live trading disabled")
        try:
            from py_clob_client.client import ClobClient  # type: ignore
            from py_clob_client.clob_types import ApiCreds  # type: ignore
        except Exception as exc:
            raise LiveTradingDisabled(f"py-clob-client-v2 unavailable: {exc}") from exc

        if self.client is None:
            creds = ApiCreds(
                api_key=self.settings.polymarket_clob_api_key,
                api_secret=self.settings.polymarket_clob_secret,
                api_passphrase=self.settings.polymarket_clob_passphrase,
            )
            self.client = ClobClient(
                self.settings.polymarket_host,
                key=self.settings.polymarket_private_key,
                chain_id=self.settings.polymarket_chain_id,
                creds=creds,
            )
        return self.client

    async def place_limit_order(self, request: OrderRequest, context: MarketContext, signal: Signal) -> OrderRecord:
        startup = await self.startup_check()
        if not startup.approved:
            return OrderRecord(order_id="live-rejected", request=request, status=OrderStatus.REJECTED)
        decision = self.risk_manager.validate(signal, context)
        if not decision.approved:
            return OrderRecord(order_id="live-rejected", request=request, status=OrderStatus.REJECTED)

        client = self._client()
        tick_size = self._call_first(client, ("get_tick_size", "getTickSize"), request.token_id) or "0.01"
        neg_risk = bool(self._call_first(client, ("get_neg_risk", "getNegRisk"), request.token_id) or False)
        side = self._sdk_side(request.side)
        order_type = self._sdk_order_type("GTC")
        share_size = request.size_usdc / request.price if request.side == OrderSide.BUY else request.size_usdc

        order_args = self._order_args(token_id=request.token_id, price=request.price, size=share_size, side=side)
        post_order = getattr(client, "create_and_post_order", None) or getattr(client, "createAndPostOrder", None)
        if post_order is None:
            raise LiveTradingDisabled("SDK client has no create-and-post order method")
        response = post_order(order_args, {"tickSize": str(tick_size), "negRisk": neg_risk}, order_type)
        order_id = str(response.get("orderID") or response.get("order_id") or response.get("id") or "")
        status_text = str(response.get("status") or "").lower()
        status = OrderStatus.FILLED if status_text == "matched" else OrderStatus.OPEN if status_text in {"live", "delayed", "unmatched"} else OrderStatus.REJECTED
        return OrderRecord(order_id=order_id or "live-unknown", request=request, status=status)

    @staticmethod
    def _sdk_side(side: OrderSide):
        try:
            from py_clob_client.clob_types import Side  # type: ignore

            return Side.BUY if side == OrderSide.BUY else Side.SELL
        except Exception:
            return "BUY" if side == OrderSide.BUY else "SELL"

    @staticmethod
    def _sdk_order_type(name: str):
        try:
            from py_clob_client.clob_types import OrderType  # type: ignore

            return getattr(OrderType, name)
        except Exception:
            return name

    @staticmethod
    def _order_args(token_id: str, price: float, size: float, side):
        try:
            from py_clob_client.clob_types import OrderArgs  # type: ignore

            return OrderArgs(token_id=token_id, price=price, size=size, side=side)
        except Exception:
            return {"tokenID": token_id, "token_id": token_id, "price": price, "size": size, "side": side}

    @staticmethod
    def _call_first(client, names: tuple[str, ...], *args):
        for name in names:
            method = getattr(client, name, None)
            if method:
                return method(*args)
        return None

    async def cancel_order(self, order_id: str) -> None:
        if not self.settings.enable_live_trading:
            return
        client = self._client()
        method = getattr(client, "cancel", None) or getattr(client, "cancel_order", None) or getattr(client, "cancelOrder", None)
        if method:
            method(order_id)

    async def cancel_all(self) -> None:
        if not self.settings.enable_live_trading:
            return
        client = self._client()
        method = getattr(client, "cancel_all", None) or getattr(client, "cancelAll", None)
        if method:
            method()
