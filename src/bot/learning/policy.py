from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from bot.config import Settings
from bot.knowledge.rag import search


MIN_TOTAL_SETTLEMENTS = 20
MIN_SEGMENT_SETTLEMENTS = 10


@dataclass(frozen=True)
class LearningRecommendation:
    status: str
    scope: str
    metric: str
    recommendation: str
    rationale: str
    confidence: float
    sample_size: int
    suggested_config: dict

    def model_dump(self) -> dict:
        data = asdict(self)
        data["suggested_config_json"] = json.dumps(self.suggested_config, sort_keys=True) if self.suggested_config else None
        return data


def generate_learning_report(conn: sqlite3.Connection, settings: Settings) -> dict:
    conn.row_factory = sqlite3.Row
    settled = _settled_positions(conn)
    decisions = _decision_summary(conn)
    total = _summary(settled)
    timeframe = [_summary([row for row in settled if row["market_type"] == market_type], market_type) for market_type in ("5m", "15m")]
    price_buckets = [_price_bucket_summary(settled, low, high) for low, high in ((0.0, 0.25), (0.25, 0.50), (0.50, 0.65), (0.65, 1.01))]
    side = [_summary([row for row in settled if row["side"] == outcome], outcome) for outcome in ("UP", "DOWN", "UNKNOWN")]
    duplicates = _duplicate_market_entries(conn)
    risk_state = _risk_state_summary(conn)
    recommendations = _recommendations(total, timeframe, price_buckets, side, duplicates, decisions, settings)
    if risk_state["stale_block_count"] > 0 and risk_state["open_positions"] == 0:
        recommendations.insert(
            0,
            LearningRecommendation(
                status="ready_to_apply",
                scope="risk:state_sync",
                metric="risk_state_stale",
                recommendation="Sync risk exposure from SQLite before each paper cycle.",
                rationale=f"{risk_state['stale_block_count']} recent blocks reported one open position while SQLite has no open positions.",
                confidence=0.9,
                sample_size=risk_state["stale_block_count"],
                suggested_config={},
            ),
        )
    references = _reference_snippets(conn)
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "mode": "recommend_only",
        "enabled": settings.enable_learning_recommendations,
        "minimums": {
            "total_settlements": MIN_TOTAL_SETTLEMENTS,
            "segment_settlements": MIN_SEGMENT_SETTLEMENTS,
        },
        "summary": total,
        "timeframe": timeframe,
        "price_buckets": price_buckets,
        "side": [item for item in side if item["sample_size"] > 0],
        "duplicates": duplicates,
        "risk_state": risk_state,
        "decision_summary": decisions,
        "recommendations": [item.model_dump() for item in recommendations] if settings.enable_learning_recommendations else [],
        "references": references,
    }


def persist_learning_recommendations(conn: sqlite3.Connection, recommendations: list[dict]) -> int:
    created_at = datetime.now(UTC).isoformat()
    inserted = 0
    for item in recommendations:
        conn.execute(
            """
            INSERT INTO learning_recommendations (
              created_at, status, scope, metric, recommendation, rationale,
              confidence, sample_size, suggested_config_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                item["status"],
                item["scope"],
                item["metric"],
                item["recommendation"],
                item["rationale"],
                float(item["confidence"]),
                int(item["sample_size"]),
                item.get("suggested_config_json"),
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def _settled_positions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT p.market_id, p.token_id, p.status, p.size_usdc, p.avg_price, p.shares,
               COALESCE(p.fee_usdc, 0) AS fee_usdc, COALESCE(p.realized_pnl_usdc, 0) AS realized_pnl_usdc,
               m.market_type, m.question, m.raw_json
        FROM positions p
        LEFT JOIN markets m ON m.market_id = p.market_id
        WHERE p.status IN ('WON', 'LOST')
        ORDER BY p.updated_at DESC
        """
    ).fetchall()
    return [_position_dict(row) for row in rows]


def _position_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["side"] = _side_for_token(str(row["token_id"]), row["raw_json"])
    return data


def _side_for_token(token_id: str, raw_json: str | None) -> str:
    if not raw_json:
        return "UNKNOWN"
    try:
        raw = json.loads(raw_json)
        outcomes = _jsonish_list(raw.get("outcomes"))
        token_ids = _jsonish_list(raw.get("clobTokenIds") or raw.get("clob_token_ids"))
    except (TypeError, json.JSONDecodeError):
        return "UNKNOWN"
    for outcome, candidate in zip(outcomes, token_ids, strict=False):
        if str(candidate) != token_id:
            continue
        label = str(outcome).upper()
        if label in {"UP", "YES"}:
            return "UP"
        if label in {"DOWN", "NO"}:
            return "DOWN"
        return label
    return "UNKNOWN"


def _jsonish_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        return decoded if isinstance(decoded, list) else []
    return []


def _summary(rows: list[dict], label: str = "all") -> dict:
    sample_size = len(rows)
    volume = sum(float(row["size_usdc"] or 0) for row in rows)
    pnl = sum(float(row["realized_pnl_usdc"] or 0) for row in rows)
    fees = sum(float(row["fee_usdc"] or 0) for row in rows)
    wins = sum(1 for row in rows if row["status"] == "WON")
    losses = sum(1 for row in rows if row["status"] == "LOST")
    avg_price = sum(float(row["avg_price"] or 0) for row in rows) / sample_size if sample_size else None
    gross_profit = sum(max(0.0, float(row["realized_pnl_usdc"] or 0)) for row in rows)
    gross_loss = abs(sum(min(0.0, float(row["realized_pnl_usdc"] or 0)) for row in rows))
    return {
        "label": label,
        "sample_size": sample_size,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / sample_size if sample_size else None,
        "volume_usdc": volume,
        "pnl_usdc": pnl,
        "roi": pnl / volume if volume else None,
        "fees_usdc": fees,
        "avg_entry_price": avg_price,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
    }


def _price_bucket_summary(rows: list[sqlite3.Row], low: float, high: float) -> dict:
    bucket = [row for row in rows if low <= float(row["avg_price"] or 0) < high]
    data = _summary(bucket, f"{low:.2f}-{high:.2f}")
    data["low"] = low
    data["high"] = high
    return data


def _duplicate_market_entries(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """
        SELECT market_id, COUNT(*) AS positions, COALESCE(SUM(size_usdc), 0) AS volume_usdc
        FROM positions
        WHERE status IN ('OPEN', 'EXPIRED_UNKNOWN', 'WON', 'LOST')
        GROUP BY market_id
        HAVING COUNT(*) > 1 OR SUM(size_usdc) > 1.00001
        """
    ).fetchall()
    return {
        "markets": len(rows),
        "excess_positions": sum(max(0, int(row["positions"]) - 1) for row in rows),
        "volume_usdc": sum(float(row["volume_usdc"] or 0) for row in rows),
    }


def _decision_summary(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT COUNT(*) AS decisions,
               SUM(CASE WHEN action != 'HOLD' THEN 1 ELSE 0 END) AS entries,
               AVG(edge) AS avg_edge,
               AVG(confidence) AS avg_confidence,
               AVG(kelly_fraction) AS avg_kelly
        FROM strategy_decisions
        """
    ).fetchone()
    reasons = [
        dict(item)
        for item in conn.execute(
            """
            SELECT reason, COUNT(*) AS count
            FROM strategy_decisions
            GROUP BY reason
            ORDER BY count DESC
            LIMIT 8
            """
        ).fetchall()
    ]
    return {
        "decisions": int(row["decisions"] or 0),
        "entries": int(row["entries"] or 0),
        "avg_edge": float(row["avg_edge"] or 0),
        "avg_confidence": float(row["avg_confidence"] or 0),
        "avg_kelly": float(row["avg_kelly"] or 0),
        "top_reasons": reasons,
    }


def _risk_state_summary(conn: sqlite3.Connection) -> dict:
    open_positions = conn.execute(
        """
        SELECT COUNT(*)
        FROM positions p
        LEFT JOIN markets m ON m.market_id = p.market_id
        WHERE p.status = 'OPEN'
          AND (m.end_time IS NULL OR m.end_time > ?)
        """,
        (datetime.now(UTC).isoformat(),),
    ).fetchone()[0]
    stale_blocks = conn.execute(
        """
        SELECT COUNT(*)
        FROM risk_events
        WHERE reason = 'one open position limit hit'
          AND created_at >= ?
        """,
        ((datetime.now(UTC) - timedelta(hours=12)).isoformat(),),
    ).fetchone()[0]
    return {"open_positions": int(open_positions or 0), "stale_block_count": int(stale_blocks or 0)}


def _recommendations(
    total: dict,
    timeframe: list[dict],
    price_buckets: list[dict],
    side: list[dict],
    duplicates: dict,
    decisions: dict,
    settings: Settings,
) -> list[LearningRecommendation]:
    recs: list[LearningRecommendation] = []
    total_n = int(total["sample_size"])
    if total_n < MIN_TOTAL_SETTLEMENTS:
        return [
            LearningRecommendation(
                status="observe",
                scope="global",
                metric="sample_size",
                recommendation="Keep collecting paper settlements before changing strategy thresholds.",
                rationale=f"Only {total_n} settled positions are available; minimum for global recommendations is {MIN_TOTAL_SETTLEMENTS}.",
                confidence=0.25,
                sample_size=total_n,
                suggested_config={},
            )
        ]

    if float(total["pnl_usdc"] or 0) < 0:
        recs.append(
            LearningRecommendation(
                status="candidate",
                scope="global",
                metric="net_pnl",
                recommendation="Do not increase trade size; tighten entry quality before any sizing change.",
                rationale=f"Net settled PnL is {total['pnl_usdc']:.2f} USDC across {total_n} settlements.",
                confidence=_confidence(total_n),
                sample_size=total_n,
                suggested_config={"paper_trade_size_usdc": settings.paper_trade_size_usdc},
            )
        )

    cheap = next((item for item in price_buckets if item["low"] == 0.0 and item["high"] == 0.25), None)
    if cheap and cheap["sample_size"] >= 3 and float(cheap["pnl_usdc"] or 0) < 0:
        recs.append(
            LearningRecommendation(
                status="candidate" if cheap["sample_size"] < MIN_SEGMENT_SETTLEMENTS else "ready_to_apply",
                scope="price_bucket:0.00-0.25",
                metric="bucket_roi",
                recommendation="Avoid sub-0.25 lottery entries unless a future rule proves much stronger edge.",
                rationale=f"Bucket <0.25 has ROI {cheap['roi']:.1%} over {cheap['sample_size']} settlements.",
                confidence=_confidence(cheap["sample_size"]),
                sample_size=cheap["sample_size"],
                suggested_config={"min_entry_price": max(settings.min_entry_price, 0.25)},
            )
        )

    mid = next((item for item in price_buckets if item["low"] == 0.50 and item["high"] == 0.65), None)
    if mid and mid["sample_size"] >= 5 and float(mid["pnl_usdc"] or 0) < 0:
        recs.append(
            LearningRecommendation(
                status="candidate" if mid["sample_size"] < MIN_SEGMENT_SETTLEMENTS else "ready_to_apply",
                scope="price_bucket:0.50-0.65",
                metric="payout_adjusted_ev",
                recommendation="Raise net edge requirement for 0.50-0.65 entries.",
                rationale=f"Mid-price entries produced {mid['pnl_usdc']:.2f} USDC PnL; winners in this band need a high hit rate after fees.",
                confidence=_confidence(mid["sample_size"]),
                sample_size=mid["sample_size"],
                suggested_config={"min_net_edge_cents": max(settings.min_net_edge_cents, settings.min_net_edge_cents + 2)},
            )
        )

    by_type = {item["label"]: item for item in timeframe}
    five = by_type.get("5m")
    fifteen = by_type.get("15m")
    if five and fifteen and five["sample_size"] >= MIN_SEGMENT_SETTLEMENTS and fifteen["sample_size"] >= MIN_SEGMENT_SETTLEMENTS:
        five_roi = float(five["roi"] or 0)
        fifteen_roi = float(fifteen["roi"] or 0)
        if five_roi < 0 and fifteen_roi > five_roi:
            recs.append(
                LearningRecommendation(
                    status="candidate",
                    scope="timeframe:5m",
                    metric="relative_roi",
                    recommendation="Tighten 5m entries or temporarily reduce 5m trade frequency relative to 15m.",
                    rationale=f"5m ROI {five_roi:.1%} underperformed 15m ROI {fifteen_roi:.1%}.",
                    confidence=min(_confidence(five["sample_size"]), _confidence(fifteen["sample_size"])),
                    sample_size=min(five["sample_size"], fifteen["sample_size"]),
                    suggested_config={"min_seconds_to_close": settings.min_seconds_to_close, "max_trades_per_market": 1},
                )
            )

    if duplicates["markets"] > 0:
        recs.append(
            LearningRecommendation(
                status="ready_to_apply",
                scope="risk:market_exposure",
                metric="duplicate_market_entries",
                recommendation="Limit paper entries to one position per market.",
                rationale=f"{duplicates['markets']} markets had duplicate or larger-than-target exposure.",
                confidence=0.85,
                sample_size=duplicates["markets"],
                suggested_config={"max_trades_per_market": 1, "max_token_position_usdc": settings.paper_trade_size_usdc},
            )
        )

    if decisions["entries"] > 100 and float(total["pnl_usdc"] or 0) < 0:
        recs.append(
            LearningRecommendation(
                status="candidate",
                scope="frequency",
                metric="entry_count_vs_pnl",
                recommendation="Reduce trade frequency until settled PnL turns positive.",
                rationale=f"{decisions['entries']} entry decisions exist while net settled PnL remains negative.",
                confidence=0.7,
                sample_size=total_n,
                suggested_config={"min_confidence": min(0.95, settings.min_confidence + 0.05)},
            )
        )

    return recs or [
        LearningRecommendation(
            status="observe",
            scope="global",
            metric="no_clear_action",
            recommendation="Keep current parameters; no deterministic threshold change is supported yet.",
            rationale="No segment has enough negative evidence to justify a conservative recommendation.",
            confidence=0.5,
            sample_size=total_n,
            suggested_config={},
        )
    ]


def _confidence(sample_size: int) -> float:
    if sample_size >= 50:
        return 0.9
    if sample_size >= 20:
        return 0.75
    if sample_size >= 10:
        return 0.6
    return 0.4


def _reference_snippets(conn: sqlite3.Connection) -> list[dict]:
    refs: list[dict] = []
    try:
        for result in search(conn, "risk payout fees 5m 15m paper trading", limit=4):
            refs.append({"source_path": result.source_path, "title": result.title, "snippet": result.snippet})
    except sqlite3.OperationalError:
        pass
    notes = conn.execute(
        """
        SELECT note, tags, created_at
        FROM learning_notes
        WHERE tags LIKE '%learning%' OR tags LIKE '%risk%' OR note LIKE '%fee%' OR note LIKE '%payout%'
        ORDER BY created_at DESC
        LIMIT 4
        """
    ).fetchall()
    refs.extend({"source_path": "learning_notes", "title": row["tags"] or "learning", "snippet": row["note"][:240]} for row in notes)
    return refs[:6]
