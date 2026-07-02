from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

import httpx

from bot.config import Settings
from bot.polymarket.models import MarketType, OutcomeSide, OutcomeToken, UpDownMarket


BTC_TERMS = ("bitcoin", "btc")
UP_DOWN_TERMS = ("up", "down")
RECURRING_SLUG_RE = re.compile(r"^btc-updown-(5m|15m)-(\d{9,})$")
INTERVAL_SECONDS = {
    MarketType.FIVE_MINUTE: 5 * 60,
    MarketType.FIFTEEN_MINUTE: 15 * 60,
}


class MarketDiscoveryError(RuntimeError):
    pass


def rejection_reason(item: dict[str, Any], wanted_types: Iterable[str]) -> tuple[str | None, str] | None:
    market = convert_gamma_market(item)
    if not market:
        return None
    if market.market_type.value not in set(wanted_types):
        return None
    if not market.active:
        return market.market_type.value, "not accepting orders or orderbook disabled"
    if market.closed:
        return market.market_type.value, "closed or expired"
    if market.resolved:
        return market.market_type.value, "resolved"
    if not market.mapping_verified:
        return market.market_type.value, "ambiguous UP/DOWN token mapping"
    return None


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_jsonish_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def detect_market_type(text: str) -> MarketType | None:
    normalized = text.lower().replace("-", " ")
    if re.search(r"\b5\s*(minute|min|m)\b", normalized) or re.search(r"\b5m\b", normalized):
        return MarketType.FIVE_MINUTE
    if re.search(r"\b15\s*(minute|min|m)\b", normalized) or re.search(r"\b15m\b", normalized):
        return MarketType.FIFTEEN_MINUTE
    if "btc up or down 5m" in normalized or "btc updown 5m" in normalized or "btc up down 5m" in normalized:
        return MarketType.FIVE_MINUTE
    if "btc up or down 15m" in normalized or "btc updown 15m" in normalized or "btc up down 15m" in normalized:
        return MarketType.FIFTEEN_MINUTE
    return None


def recurring_slug_window(slug: str) -> tuple[MarketType, datetime, datetime] | None:
    match = RECURRING_SLUG_RE.match(slug)
    if not match:
        return None
    market_type = MarketType.FIVE_MINUTE if match.group(1) == "5m" else MarketType.FIFTEEN_MINUTE
    start_time = datetime.fromtimestamp(int(match.group(2)), UTC)
    return market_type, start_time, start_time + timedelta(seconds=INTERVAL_SECONDS[market_type])


def recurring_slug_candidates(market_type: MarketType, now: datetime | None = None) -> list[str]:
    current = now or datetime.now(UTC)
    interval = INTERVAL_SECONDS[market_type]
    aligned = int(current.timestamp()) // interval * interval
    suffix = "5m" if market_type == MarketType.FIVE_MINUTE else "15m"
    return [f"btc-updown-{suffix}-{aligned + (offset * interval)}" for offset in (-1, 0, 1, 2)]


def looks_like_btc_updown(item: dict[str, Any]) -> bool:
    text = " ".join(str(item.get(key, "")) for key in ("question", "title", "slug", "ticker", "description", "seriesSlug")).lower()
    has_btc = any(term in text for term in BTC_TERMS)
    has_updown = all(term in text for term in UP_DOWN_TERMS) or "updown" in text or "up-or-down" in text
    return has_btc and has_updown


def _normalize_outcome_label(label: str) -> OutcomeSide | None:
    normalized = re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()
    if normalized in {"up", "yes", "higher", "above"} or "up" in normalized.split():
        return OutcomeSide.UP
    if normalized in {"down", "no", "lower", "below"} or "down" in normalized.split():
        return OutcomeSide.DOWN
    return None


def extract_tokens(item: dict[str, Any]) -> tuple[dict[OutcomeSide, OutcomeToken], bool]:
    outcomes = parse_jsonish_list(item.get("outcomes"))
    token_ids = (
        parse_jsonish_list(item.get("clobTokenIds"))
        or parse_jsonish_list(item.get("clob_token_ids"))
        or parse_jsonish_list(item.get("tokens"))
    )

    tokens: dict[OutcomeSide, OutcomeToken] = {}

    if token_ids and token_ids and isinstance(token_ids[0], dict):
        for token in token_ids:
            side = _normalize_outcome_label(str(token.get("outcome") or token.get("name") or ""))
            token_id = str(token.get("token_id") or token.get("tokenId") or token.get("id") or "")
            if side and token_id:
                tokens[side] = OutcomeToken(side=side, token_id=token_id, label=str(token.get("outcome") or side))
    else:
        if len(outcomes) != len(token_ids):
            return {}, False
        for label, token_id in zip(outcomes, token_ids, strict=False):
            side = _normalize_outcome_label(str(label))
            if side and token_id:
                tokens[side] = OutcomeToken(side=side, token_id=str(token_id), label=str(label))

    verified = set(tokens.keys()) == {OutcomeSide.UP, OutcomeSide.DOWN}
    if verified and tokens[OutcomeSide.UP].token_id == tokens[OutcomeSide.DOWN].token_id:
        verified = False
    return tokens, verified


def convert_gamma_market(item: dict[str, Any]) -> UpDownMarket | None:
    slug = str(item.get("slug") or "")
    slug_window = recurring_slug_window(slug)
    text = " ".join(str(item.get(key, "")) for key in ("question", "title", "slug", "ticker", "seriesSlug"))
    market_type = slug_window[0] if slug_window else detect_market_type(text)
    if market_type is None or not looks_like_btc_updown(item):
        return None

    tokens, mapping_verified = extract_tokens(item)
    closed = bool(item.get("closed") or item.get("archived") or item.get("resolved"))
    accepting_orders = item.get("acceptingOrders")
    active = bool(item.get("active")) and (accepting_orders is not False) and (item.get("enableOrderBook") is not False)
    slug_start = slug_window[1] if slug_window else None
    slug_end = slug_window[2] if slug_window else None
    start_time = slug_start or parse_time(item.get("eventStartTime") or item.get("startTime") or item.get("startDate") or item.get("startDateIso") or item.get("start_time"))
    end_time = parse_time(item.get("endDate") or item.get("endDateIso") or item.get("end_time")) or slug_end
    if end_time and end_time <= datetime.now(UTC):
        closed = True

    return UpDownMarket(
        market_id=str(item.get("conditionId") or item.get("id") or item.get("marketId") or ""),
        event_id=str(item.get("eventId") or item.get("event_id") or "") or None,
        question=str(item.get("question") or item.get("title") or ""),
        slug=slug,
        market_type=market_type,
        start_time=start_time,
        end_time=end_time,
        active=active,
        closed=closed,
        resolved=bool(item.get("resolved") or item.get("umaResolutionStatus") == "resolved"),
        liquidity=float(item.get("liquidityNum") or item.get("liquidity") or 0),
        volume=float(item.get("volumeNum") or item.get("volume") or 0),
        tokens=tokens,
        mapping_verified=mapping_verified,
        raw=item,
    )


class GammaClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(base_url=settings.gamma_host, timeout=10.0)
        self.last_raw_markets: list[dict[str, Any]] = []

    async def close(self) -> None:
        await self.client.aclose()

    async def list_markets(self, search: str = "bitcoin up down", limit: int = 250) -> list[dict[str, Any]]:
        response = await self.client.get(
            "/markets",
            params={"limit": limit, "active": "true", "closed": "false", "q": search},
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            data = payload.get("data") or payload.get("markets") or []
            return data if isinstance(data, list) else []
        return []

    async def list_events(self, limit: int = 250) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for offset in range(0, 1000, limit):
            response = await self.client.get(
                "/events",
                params={"limit": limit, "offset": offset, "active": "true", "closed": "false"},
            )
            response.raise_for_status()
            payload = response.json()
            batch = payload if isinstance(payload, list) else payload.get("data", []) if isinstance(payload, dict) else []
            if not batch:
                break
            events.extend(batch)
        return events

    async def event_by_slug(self, slug: str) -> dict[str, Any] | None:
        response = await self.client.get("/events", params={"slug": slug})
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            return payload[0] if payload else None
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list):
                return data[0] if data else None
            return payload
        return None

    async def recurring_slug_markets(self) -> list[dict[str, Any]]:
        raw_markets: list[dict[str, Any]] = []
        wanted = {MarketType(item) for item in self.settings.market_types}
        seen_slugs: set[str] = set()
        for market_type in wanted:
            for slug in recurring_slug_candidates(market_type):
                if slug in seen_slugs:
                    continue
                seen_slugs.add(slug)
                event = await self.event_by_slug(slug)
                if not event:
                    continue
                raw_markets.extend(markets_from_event(event, source="slug_probe"))
        return raw_markets

    async def discover_btc_updown(self) -> dict[MarketType, list[UpDownMarket]]:
        raw_markets = await self.recurring_slug_markets()
        raw_markets.extend(await self.list_markets())
        for event in await self.list_events():
            if looks_like_btc_updown(event):
                raw_markets.extend(markets_from_event(event, source="event_scan"))
        self.last_raw_markets = raw_markets
        return discover_from_payload(raw_markets, self.settings.market_types)


def markets_from_event(event: dict[str, Any], source: str = "event") -> list[dict[str, Any]]:
    markets = []
    series = event.get("series") or [{}]
    series_slug = event.get("seriesSlug") or (series[0].get("slug") if series and isinstance(series[0], dict) else None)
    for market in event.get("markets") or []:
        enriched = {**market}
        enriched.setdefault("eventId", event.get("id"))
        enriched.setdefault("title", event.get("title"))
        enriched.setdefault("slug", event.get("slug"))
        enriched.setdefault("ticker", event.get("ticker"))
        enriched.setdefault("seriesSlug", series_slug)
        enriched.setdefault("endDate", event.get("endDate") or event.get("endDateIso"))
        enriched.setdefault("eventStartTime", event.get("eventStartTime"))
        enriched.setdefault("startDate", event.get("startDate"))
        enriched.setdefault("active", event.get("active"))
        enriched.setdefault("closed", event.get("closed"))
        enriched.setdefault("archived", event.get("archived"))
        enriched.setdefault("enableOrderBook", event.get("enableOrderBook"))
        enriched["discoverySource"] = source
        markets.append(enriched)
    return markets


def discover_from_payload(raw_markets: Iterable[dict[str, Any]], wanted_types: Iterable[str]) -> dict[MarketType, list[UpDownMarket]]:
    wanted = {MarketType(item) for item in wanted_types}
    grouped: dict[MarketType, list[UpDownMarket]] = {market_type: [] for market_type in wanted}
    for item in raw_markets:
        market = convert_gamma_market(item)
        if not market or market.market_type not in wanted:
            continue
        if market.active and not market.closed and not market.resolved and market.mapping_verified:
            grouped[market.market_type].append(market)

    for markets in grouped.values():
        markets.sort(key=lambda m: (m.end_time or datetime.max.replace(tzinfo=UTC), -m.liquidity))
    return grouped
