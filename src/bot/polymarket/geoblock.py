from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from bot.config import Settings


@dataclass(frozen=True)
class GeoblockStatus:
    blocked: bool
    reason: str
    raw: dict[str, Any] | None = None


class GeoblockClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def check(self) -> GeoblockStatus:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(self.settings.geoblock_url)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            return GeoblockStatus(blocked=True, reason=f"geoblock check failed: {exc}", raw=None)

        blocked = bool(payload.get("blocked") or payload.get("restricted") or payload.get("isBlocked"))
        return GeoblockStatus(blocked=blocked, reason=str(payload.get("reason") or "ok"), raw=payload)

