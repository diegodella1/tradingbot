from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from bot.execution.risk_manager import RiskDecision, RiskState
from bot.polymarket.models import FillRecord, OrderBook, OrderRecord, OrderRequest, OrderSide, OrderStatus, Signal, UpDownMarket


def now_text() -> str:
    return datetime.now(UTC).isoformat()


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class Repository:
    def __init__(self, conn: sqlite3.Connection, raw_sample_seconds: int = 0):
        self.conn = conn
        self.raw_sample_seconds = max(0, raw_sample_seconds)
        self._last_raw_sample: dict[str, datetime] = {}

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
        metadata = signal.metadata or {}
        if signal.action.value == "HOLD" and self._recent_duplicate(
            "signals",
            "market_id = ? AND action = ? AND COALESCE(reason, '') = ?",
            (market_id, signal.action.value, signal.reason or ""),
        ):
            return
        self.conn.execute(
            """
            INSERT INTO signals (
              market_id, action, confidence, max_price, size_usdc, reason,
              policy_version, metadata_json, config_snapshot_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market_id,
                signal.action.value,
                signal.confidence,
                signal.max_price,
                signal.size_usdc,
                signal.reason,
                metadata.get("policy_version"),
                json.dumps(metadata),
                json.dumps(metadata.get("config_snapshot")) if metadata.get("config_snapshot") is not None else None,
                now_text(),
            ),
        )
        self.conn.commit()

    def save_strategy_decision(self, market: UpDownMarket, signal: Signal) -> None:
        metadata = signal.metadata or {}
        if signal.action.value == "HOLD" and self._recent_duplicate(
            "strategy_decisions",
            "market_id = ? AND action = ? AND COALESCE(reason, '') = ?",
            (market.market_id, signal.action.value, signal.reason or ""),
        ):
            return
        self.conn.execute(
            """
            INSERT INTO strategy_decisions (
              market_id, market_type, action, estimated_probability, market_price, edge,
              ev_usdc, kelly_fraction, recommended_size_usdc, confidence, reason,
              metadata_json, policy_version, config_snapshot_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                metadata.get("policy_version"),
                json.dumps(metadata.get("config_snapshot")) if metadata.get("config_snapshot") is not None else None,
                now_text(),
            ),
        )
        self.conn.commit()

    def save_snapshot(self, book: OrderBook) -> None:
        if not self._raw_sample_due(f"book:{book.token_id}"):
            return
        self.conn.execute(
            """
            INSERT INTO market_snapshots (market_id, token_id, best_bid, best_ask, spread, liquidity, imbalance, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (book.market_id, book.token_id, book.best_bid, book.best_ask, book.spread, book.top_liquidity_usdc, book.imbalance, now_text()),
        )
        self.conn.commit()

    def save_btc_tick(self, price: float) -> None:
        if not self._raw_sample_due("btc"):
            return
        self.conn.execute("INSERT INTO btc_ticks (price, created_at) VALUES (?, ?)", (price, now_text()))
        self.conn.commit()

    def _raw_sample_due(self, key: str) -> bool:
        if self.raw_sample_seconds <= 0:
            return True
        now = datetime.now(UTC)
        previous = self._last_raw_sample.get(key)
        if previous is not None and (now - previous).total_seconds() < self.raw_sample_seconds:
            return False
        self._last_raw_sample[key] = now
        return True

    def get_market_open_price(self, market_id: str) -> float | None:
        row = self.conn.execute("SELECT price FROM market_open_prices WHERE market_id = ?", (market_id,)).fetchone()
        return float(row["price"]) if row else None

    def save_market_open_price(self, market_id: str, price: float, source: str = "tick") -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO market_open_prices (market_id, price, source, created_at) VALUES (?, ?, ?, ?)",
            (market_id, price, source, now_text()),
        )
        self.conn.commit()

    def nearest_btc_tick(self, target_iso: str) -> float | None:
        """Return the BTC tick price closest in time to `target_iso` (before or after)."""
        after = self.conn.execute(
            "SELECT price, created_at FROM btc_ticks WHERE created_at >= ? ORDER BY created_at ASC LIMIT 1",
            (target_iso,),
        ).fetchone()
        before = self.conn.execute(
            "SELECT price, created_at FROM btc_ticks WHERE created_at < ? ORDER BY created_at DESC LIMIT 1",
            (target_iso,),
        ).fetchone()
        if after and not before:
            return float(after["price"])
        if before and not after:
            return float(before["price"])
        if not before and not after:
            return None
        target = _parse_utc(target_iso)
        after_gap = abs((_parse_utc(after["created_at"]) - target).total_seconds())
        before_gap = abs((_parse_utc(before["created_at"]) - target).total_seconds())
        return float(after["price"]) if after_gap <= before_gap else float(before["price"])

    def save_risk_event(self, market_id: str | None, decision: RiskDecision) -> None:
        if not decision.approved and self._recent_duplicate(
            "risk_events",
            "COALESCE(market_id, '') = ? AND approved = 0 AND reason = ?",
            (market_id or "", decision.reason),
        ):
            return
        self.conn.execute(
            "INSERT INTO risk_events (market_id, approved, reason, created_at) VALUES (?, ?, ?, ?)",
            (market_id, int(decision.approved), decision.reason, now_text()),
        )
        self.conn.commit()

    def save_health_event(self, name: str, status: str, detail: str = "") -> None:
        if self._recent_duplicate(
            "health_events",
            "name = ? AND status = ? AND COALESCE(detail, '') = ?",
            (name, status, detail),
        ):
            return
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

    def save_order(self, order: OrderRecord, *, commit: bool = True) -> None:
        metadata = order.request.metadata or {}
        self.conn.execute(
            """
            INSERT OR REPLACE INTO orders (
              order_id, market_id, token_id, side, status, price, size_usdc,
              filled_size_usdc, avg_fill_price, reason, policy_version,
              metadata_json, config_snapshot_json, execution_style, expires_at,
              updated_at, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
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
                metadata.get("policy_version"),
                json.dumps(metadata),
                json.dumps(metadata.get("config_snapshot")) if metadata.get("config_snapshot") is not None else None,
                order.execution_style,
                order.expires_at.isoformat() if order.expires_at else None,
                order.updated_at.isoformat(),
                order.created_at.isoformat(),
            ),
        )
        if commit:
            self.conn.commit()

    def open_maker_orders(self) -> list[OrderRecord]:
        rows = self.conn.execute(
            """
            SELECT * FROM orders
            WHERE execution_style = 'maker' AND status IN ('OPEN', 'PARTIALLY_FILLED')
            ORDER BY created_at
            """
        ).fetchall()
        orders: list[OrderRecord] = []
        for row in rows:
            metadata = json.loads(row["metadata_json"] or "{}")
            orders.append(
                OrderRecord(
                    order_id=row["order_id"],
                    request=OrderRequest(
                        market_id=row["market_id"],
                        token_id=row["token_id"],
                        side=OrderSide(row["side"]),
                        price=float(row["price"]),
                        size_usdc=float(row["size_usdc"]),
                        reason=row["reason"] or "",
                        metadata=metadata,
                    ),
                    status=OrderStatus(row["status"]),
                    filled_size_usdc=float(row["filled_size_usdc"] or 0),
                    avg_fill_price=float(row["avg_fill_price"]) if row["avg_fill_price"] is not None else None,
                    execution_style="maker",
                    expires_at=_parse_utc(row["expires_at"]),
                    created_at=_parse_utc(row["created_at"]) or datetime.now(UTC),
                    updated_at=_parse_utc(row["updated_at"]) or _parse_utc(row["created_at"]) or datetime.now(UTC),
                )
            )
        return orders

    def cancel_open_maker_orders(self, reason: str) -> int:
        now = now_text()
        result = self.conn.execute(
            """
            UPDATE orders
            SET status = 'CANCELED', reason = ?, updated_at = ?
            WHERE execution_style = 'maker' AND status IN ('OPEN', 'PARTIALLY_FILLED')
            """,
            (reason, now),
        )
        self.conn.commit()
        return int(result.rowcount)

    def save_fill(self, fill: FillRecord, *, commit: bool = True) -> None:
        metadata = fill.metadata or {}
        self.conn.execute(
            """
            INSERT INTO fills (
              order_id, market_id, token_id, side, price, size_usdc, fee_usdc,
              pnl_usdc, policy_version, metadata_json, config_snapshot_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fill.order_id,
                fill.market_id,
                fill.token_id,
                fill.side.value,
                fill.price,
                fill.size_usdc,
                fill.fee_usdc,
                fill.pnl_usdc,
                metadata.get("policy_version"),
                json.dumps(metadata),
                json.dumps(metadata.get("config_snapshot")) if metadata.get("config_snapshot") is not None else None,
                fill.created_at.isoformat(),
            ),
        )
        self.upsert_position(fill, commit=commit)

    def save_order_and_fill(self, order: OrderRecord, fill: FillRecord) -> None:
        """Persist lifecycle transition, fill and position as one transaction."""
        with self.conn:
            self.save_order(order, commit=False)
            self.save_fill(fill, commit=False)

    def upsert_position(self, fill: FillRecord, *, commit: bool = True) -> None:
        metadata = fill.metadata or {}
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
                """
                UPDATE positions
                SET size_usdc = ?, shares = ?, fee_usdc = ?, avg_price = ?,
                    policy_version = COALESCE(?, policy_version),
                    estimated_probability = COALESCE(?, estimated_probability),
                    break_even_probability = COALESCE(?, break_even_probability),
                    net_edge_cents = COALESCE(?, net_edge_cents),
                    metadata_json = COALESCE(?, metadata_json),
                    config_snapshot_json = COALESCE(?, config_snapshot_json),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    new_size,
                    new_shares,
                    new_fee,
                    avg_price,
                    metadata.get("policy_version"),
                    metadata.get("estimated_probability"),
                    metadata.get("break_even_probability_after_fees"),
                    metadata.get("net_edge_cents"),
                    json.dumps(metadata) if metadata else None,
                    json.dumps(metadata.get("config_snapshot")) if metadata.get("config_snapshot") is not None else None,
                    now_text(),
                    existing["id"],
                ),
            )
        else:
            self.conn.execute(
                """
                INSERT INTO positions (
                  market_id, token_id, size_usdc, avg_price, shares, fee_usdc,
                  status, realized_pnl_usdc, policy_version, estimated_probability,
                  break_even_probability, net_edge_cents, metadata_json,
                  config_snapshot_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'OPEN', 0, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill.market_id,
                    fill.token_id,
                    fill.size_usdc,
                    fill.price,
                    shares,
                    fill.fee_usdc,
                    metadata.get("policy_version"),
                    metadata.get("estimated_probability"),
                    metadata.get("break_even_probability_after_fees"),
                    metadata.get("net_edge_cents"),
                    json.dumps(metadata) if metadata else None,
                    json.dumps(metadata.get("config_snapshot")) if metadata.get("config_snapshot") is not None else None,
                    now_text(),
                ),
            )
        if commit:
            self.conn.commit()

    def hydrate_risk_state(self, loss_streak_window_minutes: int = 120) -> RiskState:
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
        pending = self.conn.execute(
            """
            SELECT market_id, token_id, SUM(size_usdc - COALESCE(filled_size_usdc, 0)) AS exposure
            FROM orders
            WHERE execution_style = 'maker'
              AND status IN ('OPEN', 'PARTIALLY_FILLED')
              AND (expires_at IS NULL OR expires_at > ?)
            GROUP BY market_id, token_id
            """,
            (now_text(),),
        ).fetchall()
        for row in pending:
            exposure = max(0.0, float(row["exposure"] or 0))
            if exposure <= 0:
                continue
            state.market_exposure[row["market_id"]] = state.market_exposure.get(row["market_id"], 0.0) + exposure
            state.token_exposure[row["token_id"]] = state.token_exposure.get(row["token_id"], 0.0) + exposure
        self._apply_frequency_risk_state(state)
        self._apply_pnl_risk_state(state, loss_streak_window_minutes)
        return state

    def _apply_frequency_risk_state(self, state: RiskState) -> None:
        now = datetime.now(UTC)
        hour_ago = (now - timedelta(hours=1)).isoformat()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        state.trades_last_hour = self._filled_trade_count_since(hour_ago)
        state.trades_today = self._filled_trade_count_since(day_start)

        recent_5m = self.conn.execute(
            """
            SELECT p.realized_pnl_usdc
            FROM positions p
            JOIN markets m ON m.market_id = p.market_id
            WHERE m.market_type = '5m'
              AND p.status IN ('WON', 'LOST', 'CLOSED')
              AND p.settled_at IS NOT NULL
            ORDER BY p.settled_at DESC
            LIMIT 10
            """
        ).fetchall()
        state.recent_5m_settled_count = len(recent_5m)
        state.recent_5m_pnl_usdc = sum(float(row["realized_pnl_usdc"] or 0) for row in recent_5m)

        recent = self.conn.execute(
            """
            SELECT realized_pnl_usdc, settled_at
            FROM positions
            WHERE status IN ('WON', 'LOST', 'CLOSED')
              AND settled_at IS NOT NULL
            ORDER BY settled_at DESC
            LIMIT ?
            """,
            (10,),
        ).fetchall()
        state.recent_settled_count = len(recent)
        state.recent_pnl_usdc = sum(float(row["realized_pnl_usdc"] or 0) for row in recent)
        state.last_settled_at = _parse_utc(recent[0]["settled_at"]) if recent else None

    def _filled_trade_count_since(self, cutoff: str) -> int:
        row = self.conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM orders
               WHERE COALESCE(execution_style, 'taker') != 'maker'
                 AND status IN ('FILLED', 'PARTIALLY_FILLED') AND created_at >= ?)
              +
              (SELECT COUNT(DISTINCT f.order_id) FROM fills f
               JOIN orders o ON o.order_id = f.order_id
               WHERE o.execution_style = 'maker' AND f.side = 'BUY' AND f.created_at >= ?)
            """,
            (cutoff, cutoff),
        ).fetchone()
        return int(row[0] or 0)

    def _apply_pnl_risk_state(self, state: RiskState, loss_streak_window_minutes: int = 120) -> None:
        """Populate loss-tracking fields so daily-loss/streak/cooldown gates work at runtime.

        Covers settled binaries (WON/LOST) and exit-closed positions (CLOSED); a loss
        is any settled position with negative realized PnL. The streak only counts
        losses settled within `loss_streak_window_minutes`; without the window an old
        losing streak would block trading forever (no new trades means nothing ever
        resets the counter).
        """
        day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        daily = self.conn.execute(
            """
            SELECT COALESCE(SUM(realized_pnl_usdc), 0)
            FROM positions
            WHERE status IN ('WON', 'LOST', 'CLOSED') AND settled_at IS NOT NULL AND settled_at >= ?
            """,
            (day_start,),
        ).fetchone()
        state.daily_pnl_usdc = float(daily[0] or 0)
        losses = self.conn.execute(
            """
            SELECT COUNT(*) FROM positions
            WHERE status IN ('WON', 'LOST', 'CLOSED')
              AND settled_at IS NOT NULL AND settled_at >= ?
              AND realized_pnl_usdc < 0
            """,
            (day_start,),
        ).fetchone()
        state.losses_today = int(losses[0] or 0)

        window_start = (datetime.now(UTC) - timedelta(minutes=loss_streak_window_minutes)).isoformat()
        recent = self.conn.execute(
            """
            SELECT realized_pnl_usdc, settled_at
            FROM positions
            WHERE status IN ('WON', 'LOST', 'CLOSED') AND settled_at IS NOT NULL AND settled_at >= ?
            ORDER BY settled_at DESC
            LIMIT 50
            """,
            (window_start,),
        ).fetchall()
        streak = 0
        for row in recent:
            if float(row["realized_pnl_usdc"] or 0) < 0:
                streak += 1
            else:
                break
        state.consecutive_losses = streak
        state.last_loss_at = _parse_utc(recent[0]["settled_at"]) if recent and float(recent[0]["realized_pnl_usdc"] or 0) < 0 else None

    def get_open_position(self, market_id: str) -> dict | None:
        row = self.conn.execute(
            """
            SELECT id, token_id, shares, size_usdc, fee_usdc, avg_price
            FROM positions
            WHERE market_id = ? AND status = 'OPEN'
            ORDER BY id DESC LIMIT 1
            """,
            (market_id,),
        ).fetchone()
        return dict(row) if row else None

    def save_exit_fill(self, fill: FillRecord, realized_pnl_usdc: float) -> None:
        """Persist a closing SELL fill without mutating position size (see close_position)."""
        metadata = fill.metadata or {}
        self.conn.execute(
            """
            INSERT INTO fills (
              order_id, market_id, token_id, side, price, size_usdc, fee_usdc,
              pnl_usdc, policy_version, metadata_json, config_snapshot_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fill.order_id,
                fill.market_id,
                fill.token_id,
                fill.side.value,
                fill.price,
                fill.size_usdc,
                fill.fee_usdc,
                realized_pnl_usdc,
                metadata.get("policy_version"),
                json.dumps(metadata),
                json.dumps(metadata.get("config_snapshot")) if metadata.get("config_snapshot") is not None else None,
                fill.created_at.isoformat(),
            ),
        )
        self.conn.commit()

    def close_position(self, position_id: int, realized_pnl_usdc: float) -> None:
        now = now_text()
        self.conn.execute(
            "UPDATE positions SET status = 'CLOSED', realized_pnl_usdc = ?, settled_at = ?, updated_at = ? WHERE id = ?",
            (realized_pnl_usdc, now, now, position_id),
        )
        self.conn.commit()

    def save_learning_note(self, note: str, tags: str = "") -> None:
        if self._recent_duplicate(
            "learning_notes",
            "note = ? AND COALESCE(tags, '') = ?",
            (note, tags),
        ):
            return
        self.conn.execute(
            "INSERT INTO learning_notes (note, tags, created_at) VALUES (?, ?, ?)",
            (note, tags, now_text()),
        )
        self.conn.commit()

    def save_discovery_rejection(self, market_type: str | None, question: str, slug: str, reason: str) -> None:
        now = datetime.now(UTC)
        bucket_start = now.replace(minute=0, second=0, microsecond=0).isoformat()
        now_iso = now.isoformat()
        self.conn.execute(
            """
            INSERT INTO discovery_rejection_rollups (
                market_type, question, slug, reason, bucket_start,
                occurrences, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(market_type, slug, reason, bucket_start) DO UPDATE SET
                question = excluded.question,
                occurrences = discovery_rejection_rollups.occurrences + 1,
                last_seen_at = excluded.last_seen_at
            """,
            (market_type or "", question, slug, reason, bucket_start, now_iso, now_iso),
        )
        self.conn.commit()

    def _recent_duplicate(
        self,
        table: str,
        predicate: str,
        parameters: tuple,
        window_seconds: int = 60,
    ) -> bool:
        allowed_tables = {
            "signals",
            "strategy_decisions",
            "risk_events",
            "health_events",
            "learning_notes",
        }
        if table not in allowed_tables:
            raise ValueError(f"unsupported telemetry table: {table}")
        cutoff = (datetime.now(UTC) - timedelta(seconds=window_seconds)).isoformat()
        row = self.conn.execute(
            f"SELECT 1 FROM {table} WHERE {predicate} AND created_at >= ? LIMIT 1",
            (*parameters, cutoff),
        ).fetchone()
        return row is not None
