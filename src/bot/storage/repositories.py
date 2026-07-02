from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from bot.execution.risk_manager import RiskDecision, RiskState
from bot.polymarket.models import FillRecord, OrderBook, OrderRecord, Signal, UpDownMarket


def now_text() -> str:
    return datetime.now(UTC).isoformat()


class Repository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def save_market(self, market: UpDownMarket) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market.market_id,
                market.event_id,
                market.question,
                market.slug,
                market.market_type.value,
                market.start_time.isoformat() if market.start_time else None,
                market.end_time.isoformat() if market.end_time else None,
                market.liquidity,
                market.volume,
                int(market.mapping_verified),
                json.dumps(market.raw),
            ),
        )
        self.conn.commit()

    def save_signal(self, market_id: str, signal: Signal) -> None:
        self.conn.execute(
            "INSERT INTO signals (market_id, action, confidence, max_price, size_usdc, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (market_id, signal.action.value, signal.confidence, signal.max_price, signal.size_usdc, signal.reason, now_text()),
        )
        self.conn.commit()

    def save_strategy_decision(self, market: UpDownMarket, signal: Signal) -> None:
        metadata = signal.metadata or {}
        self.conn.execute(
            """
            INSERT INTO strategy_decisions (
              market_id, market_type, action, estimated_probability, market_price, edge,
              ev_usdc, kelly_fraction, recommended_size_usdc, confidence, reason,
              metadata_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market.market_id,
                market.market_type.value,
                signal.action.value,
                metadata.get("estimated_probability"),
                metadata.get("market_price"),
                metadata.get("edge"),
                metadata.get("ev_usdc_per_1"),
                metadata.get("kelly_fraction"),
                metadata.get("recommended_size_usdc"),
                signal.confidence,
                signal.reason,
                json.dumps(metadata),
                now_text(),
            ),
        )
        self.conn.commit()

    def save_snapshot(self, book: OrderBook) -> None:
        self.conn.execute(
            """
            INSERT INTO market_snapshots (market_id, token_id, best_bid, best_ask, spread, liquidity, imbalance, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (book.market_id, book.token_id, book.best_bid, book.best_ask, book.spread, book.top_liquidity_usdc, book.imbalance, now_text()),
        )
        self.conn.commit()

    def save_btc_tick(self, price: float) -> None:
        self.conn.execute("INSERT INTO btc_ticks (price, created_at) VALUES (?, ?)", (price, now_text()))
        self.conn.commit()

    def save_risk_event(self, market_id: str | None, decision: RiskDecision) -> None:
        self.conn.execute(
            "INSERT INTO risk_events (market_id, approved, reason, created_at) VALUES (?, ?, ?, ?)",
            (market_id, int(decision.approved), decision.reason, now_text()),
        )
        self.conn.commit()

    def save_health_event(self, name: str, status: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO health_events (name, status, detail, created_at) VALUES (?, ?, ?, ?)",
            (name, status, detail, now_text()),
        )
        self.conn.commit()

    def set_state(self, key: str, value: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO paper_state (key, value_json, updated_at) VALUES (?, ?, ?)",
            (key, json.dumps(value), now_text()),
        )
        self.conn.commit()

    def save_order(self, order: OrderRecord) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                order.order_id,
                order.request.market_id,
                order.request.token_id,
                order.request.side.value,
                order.status.value,
                order.request.price,
                order.request.size_usdc,
                order.filled_size_usdc,
                order.avg_fill_price,
                order.request.reason,
                order.created_at.isoformat(),
            ),
        )
        self.conn.commit()

    def save_fill(self, fill: FillRecord) -> None:
        self.conn.execute(
            "INSERT INTO fills (order_id, market_id, token_id, side, price, size_usdc, fee_usdc, pnl_usdc, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (fill.order_id, fill.market_id, fill.token_id, fill.side.value, fill.price, fill.size_usdc, fill.fee_usdc, fill.pnl_usdc, fill.created_at.isoformat()),
        )
        self.conn.commit()
        self.upsert_position(fill)

    def upsert_position(self, fill: FillRecord) -> None:
        shares = fill.size_usdc / fill.price if fill.price > 0 else 0.0
        existing = self.conn.execute(
            "SELECT id, size_usdc, shares, fee_usdc FROM positions WHERE market_id = ? AND token_id = ? AND status = 'OPEN' ORDER BY id DESC LIMIT 1",
            (fill.market_id, fill.token_id),
        ).fetchone()
        if existing:
            new_size = float(existing["size_usdc"]) + fill.size_usdc
            new_shares = float(existing["shares"] or 0) + shares
            new_fee = float(existing["fee_usdc"] or 0) + fill.fee_usdc
            avg_price = new_size / new_shares if new_shares > 0 else fill.price
            self.conn.execute(
                "UPDATE positions SET size_usdc = ?, shares = ?, fee_usdc = ?, avg_price = ?, updated_at = ? WHERE id = ?",
                (new_size, new_shares, new_fee, avg_price, now_text(), existing["id"]),
            )
        else:
            self.conn.execute(
                """
                INSERT INTO positions (market_id, token_id, size_usdc, avg_price, shares, fee_usdc, status, realized_pnl_usdc, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'OPEN', 0, ?)
                """,
                (fill.market_id, fill.token_id, fill.size_usdc, fill.price, shares, fill.fee_usdc, now_text()),
            )
        self.conn.commit()

    def hydrate_risk_state(self) -> RiskState:
        state = RiskState()
        rows = self.conn.execute(
            """
            SELECT p.market_id, p.token_id, SUM(p.size_usdc) AS exposure
            FROM positions p
            LEFT JOIN markets m ON m.market_id = p.market_id
            WHERE p.status = 'OPEN' AND (m.end_time IS NULL OR m.end_time > ?)
            GROUP BY p.market_id, p.token_id
            """,
            (now_text(),),
        ).fetchall()
        if not rows:
            rows = self.conn.execute(
                """
                SELECT f.market_id, f.token_id, SUM(f.size_usdc) AS exposure
                FROM fills f
                LEFT JOIN markets m ON m.market_id = f.market_id
                WHERE m.end_time IS NULL OR m.end_time > ?
                GROUP BY f.market_id, f.token_id
                """,
                (now_text(),),
            ).fetchall()
        for row in rows:
            exposure = float(row["exposure"] or 0)
            if exposure <= 0:
                continue
            state.market_exposure[row["market_id"]] = state.market_exposure.get(row["market_id"], 0.0) + exposure
            state.token_exposure[row["token_id"]] = state.token_exposure.get(row["token_id"], 0.0) + exposure
            state.trades_by_market[row["market_id"]] = state.trades_by_market.get(row["market_id"], 0) + 1
        return state

    def save_learning_note(self, note: str, tags: str = "") -> None:
        self.conn.execute(
            "INSERT INTO learning_notes (note, tags, created_at) VALUES (?, ?, ?)",
            (note, tags, now_text()),
        )
        self.conn.commit()

    def save_discovery_rejection(self, market_type: str | None, question: str, slug: str, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO discovery_rejections (market_type, question, slug, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (market_type, question, slug, reason, now_text()),
        )
        self.conn.commit()
