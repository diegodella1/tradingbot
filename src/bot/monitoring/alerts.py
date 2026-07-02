from __future__ import annotations

import structlog


log = structlog.get_logger()


def alert(message: str, **fields: object) -> None:
    log.warning(message, **fields)

