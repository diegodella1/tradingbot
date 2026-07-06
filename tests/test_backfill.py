from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

from bot.polymarket.backfill import backfill_outcomes, unverified_closed_markets
from bot.storage.db import connect, init_db


def _insert_market(conn, market_id: str, slug: str, raw: dict, end_offset_minutes: int = -10) -> None:
    end_time = (datetime.now(UTC) + timedelta(minutes=end_offset_minutes)).isoformat()
    conn.execute(
        "INSERT INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (market_id, None, "Bitcoin Up or Down - 5 minute", slug, "5m", None, end_time, 100, 10, 1, json.dumps(raw)),
    )


_UNRESOLVED_RAW = {
    "outcomes": '["Up", "Down"]',
    "clobTokenIds": '["up-token", "down-token"]',
    "outcomePrices": '["0.45", "0.55"]',
}
_RESOLVED_RAW = {
    "outcomes": '["Up", "Down"]',
    "clobTokenIds": '["up-token", "down-token"]',
    "outcomePrices": '["0.995", "0.005"]',
}


class _FakeGamma:
    def __init__(self, event: dict | None):
        self.event = event
        self.calls: list[str] = []

    async def event_by_slug(self, slug: str) -> dict | None:
        self.calls.append(slug)
        return self.event


def _resolved_event(slug: str) -> dict:
    return {
        "id": "e1",
        "slug": slug,
        "title": "Bitcoin Up or Down - 5 minute",
        "markets": [
            {
                "conditionId": "m1",
                "question": "Bitcoin Up or Down - 5 minute",
                "slug": slug,
                **_RESOLVED_RAW,
                "closed": True,
                "endDate": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
            }
        ],
    }


def test_unverified_closed_markets_filters_resolved(settings):
    init_db(settings.sqlite_path)
    with connect(settings.sqlite_path) as conn:
        _insert_market(conn, "m1", "btc-updown-5m-1", _UNRESOLVED_RAW)
        _insert_market(conn, "m2", "btc-updown-5m-2", _RESOLVED_RAW)
        _insert_market(conn, "m3", "btc-updown-5m-3", _UNRESOLVED_RAW, end_offset_minutes=30)  # still open
        conn.commit()
        pending = unverified_closed_markets(conn)

    assert [item["market_id"] for item in pending] == ["m1"]


def test_backfill_outcomes_updates_raw_json(settings):
    init_db(settings.sqlite_path)
    slug = "btc-updown-5m-1"
    gamma = _FakeGamma(_resolved_event(slug))
    with connect(settings.sqlite_path) as conn:
        _insert_market(conn, "m1", slug, _UNRESOLVED_RAW)
        conn.commit()

        result = asyncio.run(backfill_outcomes(gamma, conn))

        raw = json.loads(conn.execute("SELECT raw_json FROM markets WHERE market_id = 'm1'").fetchone()["raw_json"])
    assert gamma.calls == [slug]
    assert result["refreshed"] == 1
    assert result["verified"] == 1
    assert json.loads(raw["outcomePrices"]) == ["0.995", "0.005"]


def test_backfill_outcomes_reports_missing_event(settings):
    init_db(settings.sqlite_path)
    gamma = _FakeGamma(None)
    with connect(settings.sqlite_path) as conn:
        _insert_market(conn, "m1", "btc-updown-5m-1", _UNRESOLVED_RAW)
        conn.commit()

        result = asyncio.run(backfill_outcomes(gamma, conn))

    assert result["refreshed"] == 0
    assert result["errors"] == ["btc-updown-5m-1: no gamma event"]
