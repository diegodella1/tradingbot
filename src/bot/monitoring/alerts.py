from __future__ import annotations

from contextlib import suppress

import httpx
import structlog

from bot.config import Settings

log = structlog.get_logger()


def alert(message: str, **fields: object) -> None:
    """Structured local alert (stdout)."""
    log.warning(message, **fields)


def send_alert(settings: Settings, message: str, **fields: object) -> None:
    """Local structured alert plus an optional external webhook (Telegram/Discord/Slack).

    The webhook is best-effort: failures never interrupt trading. When
    ALERT_WEBHOOK_URL is unset (default) this behaves exactly like `alert`.
    """
    alert(message, **fields)
    url = settings.alert_webhook_url
    if not url:
        return
    payload = {"content": message, "text": message, "fields": fields}
    with suppress(Exception):
        httpx.post(url, json=payload, timeout=5.0)
