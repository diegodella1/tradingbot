from __future__ import annotations

from dataclasses import dataclass

from bot.config import Settings


@dataclass(frozen=True)
class HealthItem:
    name: str
    ok: bool
    detail: str


def local_health(settings: Settings) -> list[HealthItem]:
    return [
        HealthItem("live_trading", not settings.enable_live_trading or settings.live_auth_ready, "disabled" if not settings.enable_live_trading else "auth ready"),
        HealthItem("kill_switch", not settings.kill_switch_file.exists(), str(settings.kill_switch_file)),
        HealthItem("database", settings.sqlite_path.parent.exists(), str(settings.sqlite_path)),
    ]

