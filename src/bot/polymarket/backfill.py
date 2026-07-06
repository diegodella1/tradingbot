from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from bot.polymarket.gamma import GammaClient, convert_gamma_market, markets_from_event
from bot.storage.db import _verified_winner_token_id
from bot.storage.repositories import Repository


def unverified_closed_markets(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Markets already past end_time whose raw_json still lacks a verified winner.

    Discovery snapshots raw_json BEFORE resolution, so without a refetch most
    closed markets never get final outcomePrices (evaluation bias: only markets
    that happened to be re-fetched count as resolved).
    """
    now = datetime.now(UTC).isoformat()
    rows = conn.execute(
        """
        SELECT market_id, slug, raw_json
        FROM markets
        WHERE end_time IS NOT NULL AND end_time <= ?
        ORDER BY end_time DESC
        LIMIT ?
        """,
        (now, limit * 4),
    ).fetchall()
    pending = [dict(row) for row in rows if _verified_winner_token_id(row["raw_json"]) is None]
    return pending[:limit]


async def backfill_outcomes(gamma: GammaClient, conn: sqlite3.Connection, limit: int = 50) -> dict:
    """Refetch closed markets from Gamma and persist their final raw_json."""
    repo = Repository(conn)
    pending = unverified_closed_markets(conn, limit)
    refreshed = 0
    verified = 0
    errors: list[str] = []
    for ref in pending:
        slug = ref.get("slug")
        if not slug:
            continue
        try:
            event = await gamma.event_by_slug(slug)
            if not event:
                errors.append(f"{slug}: no gamma event")
                continue
            for raw in markets_from_event(event, source="outcome_backfill"):
                market = convert_gamma_market(raw)
                if not market:
                    continue
                if market.market_id == ref.get("market_id") or market.slug == slug:
                    repo.save_market(market)
                    refreshed += 1
                    if market.raw and _verified_winner_token_id(json.dumps(market.raw)) is not None:
                        verified += 1
        except Exception as exc:
            errors.append(f"{slug}: {exc}")
    return {"pending": len(pending), "refreshed": refreshed, "verified": verified, "errors": errors[:8]}
