from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from bot.config import get_settings
from bot.learning.policy import generate_learning_report
from bot.polymarket.gamma import convert_gamma_market, markets_from_event
from bot.storage.db import force_settle_pending_positions, init_db
from bot.storage.repositories import Repository


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
TABLES = (
    "markets",
    "market_snapshots",
    "btc_ticks",
    "signals",
    "orders",
    "fills",
    "positions",
    "pnl",
    "risk_events",
    "health_events",
    "rag_documents",
    "learning_notes",
    "learning_recommendations",
    "discovery_rejections",
    "strategy_decisions",
)


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FRONTEND), **kwargs)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/") and not self._authorized():
            return
        if path == "/api/status":
            self._json(status_payload())
            return
        if path == "/api/analytics":
            self._json(analytics_payload())
            return
        if path == "/api/strategies":
            self._json(strategies_payload())
            return
        if path == "/api/learning":
            self._json(learning_payload())
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/") and not self._authorized():
            return
        if path == "/api/settlements/force":
            self._json(force_settlements_payload())
            return
        self.send_response(404)
        self.end_headers()

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_HEAD(self) -> None:
        if urlparse(self.path).path in {"/api/status", "/api/analytics", "/api/strategies", "/api/learning"}:
            if not self._authorized():
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            return
        super().do_HEAD()

    def _authorized(self) -> bool:
        """Bearer-token gate for /api/*. Fails closed when DASHBOARD_TOKEN is unset."""
        expected = get_settings().dashboard_token
        if not expected:
            self._deny(403, "dashboard token not configured; set DASHBOARD_TOKEN")
            return False
        header = self.headers.get("Authorization", "")
        provided = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
        if not provided:
            provided = self.headers.get("X-Dashboard-Token", "").strip()
        if not provided:
            query = parse_qs(urlparse(self.path).query)
            provided = (query.get("token") or [""])[0].strip()
        if provided == expected:
            return True
        self._deny(401, "invalid or missing dashboard token")
        return False

    def _deny(self, code: int, message: str) -> None:
        body = json.dumps({"error": message}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


def status_payload() -> dict:
    settings = get_settings()
    init_db(settings.sqlite_path)
    counts = {table: 0 for table in TABLES}

    with sqlite3.connect(settings.sqlite_path) as conn:
        conn.row_factory = sqlite3.Row
        init_db(settings.sqlite_path)
        for table in TABLES:
            counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        latest_state = _state(conn, "paper_loop")
        activity = _recent_operations(conn)
        latest_btc = _latest_btc(conn, latest_state)
        btc_candles_1m = _btc_candles_1m(conn)
        markets = _state_markets(latest_state) or _latest_markets(conn)
        rejections = _latest_rejections(conn)
        performance = _performance(conn, settings)
        execution = _execution_stats(conn)
        learning = _latest_learning(conn)
        last_risk = _last_row(conn, "risk_events")
        decisions = _latest_decisions(conn)
        last_health = {name: _last_health(conn, name) for name in ("paper_loop", "btc_feed", "market_discovery", "market_feed")}

    safety = _safety_gates(settings, counts, last_health)

    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "sqlite_path": str(settings.sqlite_path),
        "live_trading_enabled": settings.enable_live_trading,
        "mode": "paper",
        "config": {
            "paper_bankroll_usdc": settings.paper_bankroll_usdc,
            "paper_trade_size_usdc": settings.paper_trade_size_usdc,
            "paper_enable_fees": settings.paper_enable_fees,
            "paper_taker_fee_rate": settings.paper_taker_fee_rate,
            "min_edge_cents": settings.min_edge_cents,
            "max_spread_cents": settings.max_spread_cents,
            "min_orderbook_liquidity_usdc": settings.min_orderbook_liquidity_usdc,
            "kelly_fraction_multiplier": settings.kelly_fraction_multiplier,
        },
        "paper_state": latest_state,
        "btc": latest_btc,
        "btc_candles_1m": btc_candles_1m,
        "markets": markets,
        "discovery_rejections": rejections,
        "performance": performance,
        "execution": execution,
        "last_risk": dict(last_risk) if last_risk else None,
        "health": last_health,
        "safety": safety,
        "counts": counts,
        "activity": activity[:12],
        "learning": learning,
        "decisions": decisions,
    }


def force_settlements_payload() -> dict:
    settings = get_settings()
    init_db(settings.sqlite_path)
    refresh = _refresh_pending_market_results(settings)
    settlement = force_settle_pending_positions(settings.sqlite_path)
    payload = status_payload()
    pending_details = _pending_settlement_details(settings.sqlite_path)
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "mode": "paper",
        "refreshed_markets": refresh["refreshed_markets"],
        "refresh_errors": refresh["errors"],
        "settlement": settlement,
        "pending_details": pending_details,
        "performance": payload["performance"],
    }


def learning_payload() -> dict:
    settings = get_settings()
    init_db(settings.sqlite_path)
    with sqlite3.connect(settings.sqlite_path) as conn:
        conn.row_factory = sqlite3.Row
        return generate_learning_report(conn, settings)


def _refresh_pending_market_results(settings) -> dict:
    errors: list[str] = []
    refreshed = 0
    pending = _pending_market_refs(settings.sqlite_path)
    if not pending:
        return {"refreshed_markets": 0, "errors": []}

    with sqlite3.connect(settings.sqlite_path) as conn:
        conn.row_factory = sqlite3.Row
        repo = Repository(conn)
        with httpx.Client(base_url=settings.gamma_host, timeout=8.0) as client:
            for ref in pending:
                slug = ref.get("slug")
                if not slug:
                    continue
                try:
                    response = client.get("/events", params={"slug": slug})
                    response.raise_for_status()
                    payload = response.json()
                    event = _first_event(payload)
                    if not event:
                        errors.append(f"{slug}: no gamma event")
                        continue
                    raw_markets = markets_from_event(event, source="settlement_force")
                    matched = False
                    for raw in raw_markets:
                        market = convert_gamma_market(raw)
                        if not market:
                            continue
                        if market.market_id == ref.get("market_id") or market.slug == slug:
                            repo.save_market(market)
                            refreshed += 1
                            matched = True
                    if not matched:
                        errors.append(f"{slug}: no matching market")
                except Exception as exc:
                    errors.append(f"{slug}: {exc}")
    return {"refreshed_markets": refreshed, "errors": errors[:8]}


def _first_event(payload):
    if isinstance(payload, list):
        return payload[0] if payload else None
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return data[0] if data else None
        return payload
    return None


def _pending_market_refs(path: Path) -> list[dict]:
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT DISTINCT m.market_id, m.slug
            FROM positions p
            JOIN markets m ON m.market_id = p.market_id
            WHERE p.status = 'EXPIRED_UNKNOWN'
               OR (p.status = 'OPEN' AND m.end_time IS NOT NULL AND m.end_time <= ?)
            """,
            (now,),
        ).fetchall()
    return [dict(row) for row in rows]


def _pending_settlement_details(path: Path) -> list[dict]:
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT p.id, p.market_id, p.token_id, p.status, p.size_usdc, p.avg_price,
                   p.shares, p.fee_usdc, p.updated_at, m.question, m.slug, m.market_type,
                   m.end_time, m.raw_json
            FROM positions p
            JOIN markets m ON m.market_id = p.market_id
            WHERE p.status = 'EXPIRED_UNKNOWN'
               OR (p.status = 'OPEN' AND m.end_time IS NOT NULL AND m.end_time <= ?)
            ORDER BY p.updated_at DESC
            LIMIT 12
            """,
            (now,),
        ).fetchall()
    return [_pending_detail_from_row(row) for row in rows]


def _pending_detail_from_row(row: sqlite3.Row) -> dict:
    token_ids: list[str] = []
    outcomes: list[str] = []
    prices: list[float] = []
    try:
        raw = json.loads(row["raw_json"] or "{}")
        token_ids = [str(item) for item in _jsonish_list(raw.get("clobTokenIds") or raw.get("clob_token_ids"))]
        outcomes = [str(item) for item in _jsonish_list(raw.get("outcomes"))]
        prices = [float(item) for item in _jsonish_list(raw.get("outcomePrices"))]
    except (TypeError, ValueError, json.JSONDecodeError):
        pass

    held_token_id = str(row["token_id"])
    held_index = token_ids.index(held_token_id) if held_token_id in token_ids else None
    held_token_price = prices[held_index] if held_index is not None and held_index < len(prices) else None
    held_outcome = outcomes[held_index] if held_index is not None and held_index < len(outcomes) else None
    reason = _pending_settlement_reason(prices, token_ids)
    return {
        "market_id": row["market_id"],
        "market_type": row["market_type"],
        "question": row["question"],
        "slug": row["slug"],
        "status": row["status"],
        "end_time": row["end_time"],
        "held_token_id": held_token_id,
        "held_outcome": held_outcome,
        "held_token_price": held_token_price,
        "outcome_prices": prices,
        "outcomes": outcomes,
        "cost_usdc": float(row["size_usdc"] or 0),
        "shares": float(row["shares"] or 0),
        "fee_usdc": float(row["fee_usdc"] or 0),
        "updated_at": row["updated_at"],
        "reason": reason,
    }


def _pending_settlement_reason(prices: list[float], token_ids: list[str]) -> str:
    if len(token_ids) < 2:
        return "missing token mapping; waiting for a verifiable Polymarket result"
    if len(prices) != len(token_ids):
        return "missing outcome prices; waiting for Gamma result refresh"
    max_price = max(prices)
    min_price = min(prices)
    if max_price < 0.99 or min_price > 0.01:
        return f"outcome prices not final yet: min={min_price:.3f}, max={max_price:.3f}; waiting for Polymarket resolution"
    if prices.count(max_price) != 1:
        return "ambiguous winner price; refusing to settle automatically"
    return "pending DB settlement retry"


def _jsonish_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        return decoded if isinstance(decoded, list) else []
    return []


def analytics_payload() -> dict:
    settings = get_settings()
    init_db(settings.sqlite_path)
    with sqlite3.connect(settings.sqlite_path) as conn:
        conn.row_factory = sqlite3.Row
        performance = _performance(conn, settings)
        decisions = _latest_decisions(conn)
        total_volume = performance["paper_volume_usdc"]
        settled_volume = performance["settled_volume_usdc"]
        total_pnl = performance["realized_pnl_usdc"]
        roi = (total_pnl / settled_volume) if settled_volume > 0 else 0.0
        edge_stats = conn.execute(
            """
            SELECT COUNT(*) AS decisions,
                   AVG(edge) AS avg_edge,
                   AVG(confidence) AS avg_confidence,
                   AVG(kelly_fraction) AS avg_kelly,
                   SUM(CASE WHEN action != 'HOLD' THEN 1 ELSE 0 END) AS entries
            FROM strategy_decisions
            """
        ).fetchone()
        reasons = [
            dict(row)
            for row in conn.execute(
                """
                SELECT reason, COUNT(*) AS count
                FROM strategy_decisions
                GROUP BY reason
                ORDER BY count DESC
                LIMIT 8
                """
            ).fetchall()
        ]
        timeframe = _timeframe_stats(conn)
        hourly = _hourly_distribution(conn)
        recent_positions = _analytics_positions(conn)

    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "kpis": {
            "paper_roi": roi,
            "paper_pnl_usdc": total_pnl,
            "paper_wallet": performance["paper_wallet"],
            "paper_volume_usdc": total_volume,
            "settled_volume_usdc": settled_volume,
            "settled_trades": performance["settled_trades"],
            "wins": performance["wins"],
            "losses": performance["losses"],
            "win_rate": performance["win_rate"],
            "profit_factor": performance["profit_factor"],
            "pending_settlement": performance["pending_settlement_count"],
            "total_orders": performance["total_orders"],
            "total_fills": performance["total_fills"],
            "open_positions": len([p for p in performance["positions"] if p.get("status") == "OPEN"]),
            "avg_edge": float(edge_stats["avg_edge"] or 0),
            "avg_confidence": float(edge_stats["avg_confidence"] or 0),
            "avg_kelly": float(edge_stats["avg_kelly"] or 0),
            "decision_count": int(edge_stats["decisions"] or 0),
            "entry_count": int(edge_stats["entries"] or 0),
        },
        "timeframe": timeframe,
        "hourly": hourly,
        "reasons": reasons,
        "decisions": decisions,
        "positions": recent_positions,
        "outcomes": _position_outcomes(conn),
    }


def strategies_payload() -> dict:
    settings = get_settings()
    init_db(settings.sqlite_path)
    counts = {table: 0 for table in TABLES}

    with sqlite3.connect(settings.sqlite_path) as conn:
        conn.row_factory = sqlite3.Row
        for table in TABLES:
            counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        latest_state = _state(conn, "paper_loop")
        markets = _state_markets(latest_state) or _latest_markets(conn)
        decisions = _latest_decisions(conn)
        rejections = _latest_rejections(conn)
        performance = _performance(conn, settings)
        timeframe = _timeframe_stats(conn)
        learning = generate_learning_report(conn, settings)
        execution = _execution_stats(conn)
        last_health = {name: _last_health(conn, name) for name in ("paper_loop", "btc_feed", "market_discovery", "market_feed")}

    strategy_name = latest_state.get("strategy")
    if not strategy_name:
        strategy_name = "momentum_book_imbalance" if settings.enable_experimental_strategy else "no_trade"

    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "mode": "paper",
        "live_trading_enabled": settings.enable_live_trading,
        "strategy": {
            "name": strategy_name,
            "experimental_enabled": settings.enable_experimental_strategy,
            "hold_to_resolution": settings.hold_to_resolution,
            "paper_only": not settings.enable_live_trading,
        },
        "config": {
            "enable_experimental_strategy": settings.enable_experimental_strategy,
            "min_edge_cents": settings.min_edge_cents,
            "min_confidence": settings.min_confidence,
            "min_entry_price": settings.min_entry_price,
            "max_entry_price": settings.max_entry_price,
            "min_profit_if_win_usdc": settings.min_profit_if_win_usdc,
            "min_net_edge_cents": settings.min_net_edge_cents,
            "min_book_imbalance": settings.min_book_imbalance,
            "kelly_fraction_multiplier": settings.kelly_fraction_multiplier,
            "min_kelly_size_usdc": settings.min_kelly_size_usdc,
            "max_position_usdc": settings.max_position_usdc,
            "max_market_position_usdc": settings.max_market_position_usdc,
            "max_open_markets": settings.max_open_markets,
            "max_trades_per_market": settings.max_trades_per_market,
            "max_spread_cents": settings.max_spread_cents,
            "min_orderbook_liquidity_usdc": settings.min_orderbook_liquidity_usdc,
            "min_seconds_to_close": settings.min_seconds_to_close,
            "paper_loop_interval_seconds": settings.paper_loop_interval_seconds,
            "paper_bankroll_usdc": settings.paper_bankroll_usdc,
            "paper_trade_size_usdc": settings.paper_trade_size_usdc,
            "paper_enable_fees": settings.paper_enable_fees,
            "paper_taker_fee_rate": settings.paper_taker_fee_rate,
        },
        "runtime": {
            "status": latest_state.get("status", "pending"),
            "updated_at": latest_state.get("updated_at"),
            "last_error": latest_state.get("error"),
            "market_count": len(markets),
            "open_positions": len([item for item in performance["positions"] if item.get("status") == "OPEN"]),
        },
        "markets": markets,
        "decisions": decisions,
        "rejections": rejections[:8],
        "safety": _safety_gates(settings, counts, last_health),
        "timeframe": timeframe,
        "learning": learning,
        "performance": performance,
        "execution": execution,
        "counts": counts,
        "health": last_health,
    }


def _safety_gates(settings, counts: dict[str, int], last_health: dict) -> list[dict]:
    return [
        {"name": "live trading", "ok": not settings.enable_live_trading, "detail": "disabled by default" if not settings.enable_live_trading else "enabled"},
        {"name": "kill switch", "ok": not settings.kill_switch_file.exists(), "detail": str(settings.kill_switch_file)},
        {"name": "credentials", "ok": not settings.enable_live_trading or settings.live_auth_ready, "detail": "not required for paper" if not settings.enable_live_trading else "required for live"},
        {"name": "database", "ok": settings.sqlite_path.exists(), "detail": str(settings.sqlite_path)},
        {"name": "paper loop", "ok": (last_health.get("paper_loop") or {}).get("status") in {"ok", "no_market"}, "detail": (last_health.get("paper_loop") or {}).get("detail", "pending")},
        {"name": "wallet readiness", "ok": False, "detail": "readiness command pending"},
        {"name": "rag index", "ok": counts["rag_documents"] > 0, "detail": f"{counts['rag_documents']} docs"},
    ]


def _state(conn: sqlite3.Connection, key: str) -> dict:
    row = conn.execute("SELECT value_json FROM paper_state WHERE key = ?", (key,)).fetchone()
    if not row:
        return {"status": "pending"}
    try:
        return json.loads(row["value_json"])
    except json.JSONDecodeError:
        return {"status": "invalid"}


def _latest_btc(conn: sqlite3.Connection, state: dict | None = None) -> dict:
    row = conn.execute("SELECT price, created_at FROM btc_ticks ORDER BY created_at DESC LIMIT 1").fetchone()
    if not row:
        state_btc = (state or {}).get("btc") or {}
        price = state_btc.get("current_price")
        created_at = state_btc.get("price_timestamp")
        if price is None or not created_at:
            return {"price": None, "created_at": None, "fresh": False}
        created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        return {"price": price, "created_at": created_at, "fresh": (datetime.now(UTC) - created).total_seconds() < 30}
    created = datetime.fromisoformat(row["created_at"])
    return {"price": row["price"], "created_at": row["created_at"], "fresh": (datetime.now(UTC) - created).total_seconds() < 30}


def _btc_candles_1m(conn: sqlite3.Connection, limit: int = 60) -> list[dict]:
    rows = conn.execute(
        """
        SELECT price, created_at
        FROM btc_ticks
        ORDER BY created_at DESC
        LIMIT 1000
        """
    ).fetchall()
    buckets: dict[str, list[tuple[str, float]]] = {}
    for row in reversed(rows):
        created_at = str(row["created_at"])
        try:
            parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        minute = parsed.replace(second=0, microsecond=0).isoformat()
        buckets.setdefault(minute, []).append((created_at, float(row["price"])))

    candles = []
    for minute in sorted(buckets.keys())[-limit:]:
        prices = [price for _, price in buckets[minute]]
        if not prices:
            continue
        candles.append(
            {
                "minute": minute,
                "open": prices[0],
                "high": max(prices),
                "low": min(prices),
                "close": prices[-1],
            }
        )
    return candles


def _state_markets(state: dict) -> list[dict]:
    markets = []
    for item in state.get("markets") or []:
        if item.get("status") != "ok":
            continue
        markets.append(
            {
                "market_id": item.get("market_id"),
                "type": item.get("type"),
                "question": item.get("question"),
                "slug": item.get("slug"),
                "end_time": _end_time_from_seconds(item.get("seconds_to_close")),
                "seconds_to_close": item.get("seconds_to_close"),
                "liquidity": item.get("liquidity"),
                "volume": item.get("volume"),
                "mapping_verified": True,
                "up_bid": item.get("up_bid"),
                "up_ask": item.get("up_ask"),
                "down_bid": item.get("down_bid"),
                "down_ask": item.get("down_ask"),
                "signal": item.get("signal"),
                "risk": item.get("risk"),
                "order": item.get("order"),
                "snapshots": [
                    {"side": "UP", "best_bid": item.get("up_bid"), "best_ask": item.get("up_ask")},
                    {"side": "DOWN", "best_bid": item.get("down_bid"), "best_ask": item.get("down_ask")},
                ],
            }
        )
    return markets


def _end_time_from_seconds(seconds: float | int | None) -> str | None:
    if seconds is None:
        return None
    return datetime.fromtimestamp(datetime.now(UTC).timestamp() + float(seconds), UTC).isoformat()


def _latest_markets(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM markets
        WHERE market_type IN ('5m', '15m')
          AND (end_time IS NULL OR end_time > ?)
        ORDER BY end_time ASC
        """,
        (datetime.now(UTC).isoformat(),),
    ).fetchall()
    markets = []
    for row in rows:
        snapshots = conn.execute(
            """
            SELECT token_id, best_bid, best_ask, spread, liquidity, imbalance, created_at
            FROM market_snapshots
            WHERE market_id = ?
            ORDER BY created_at DESC
            LIMIT 2
            """,
            (row["market_id"],),
        ).fetchall()
        markets.append(
            {
                "market_id": row["market_id"],
                "type": row["market_type"],
                "question": row["question"],
                "slug": row["slug"],
                "end_time": row["end_time"],
                "liquidity": row["liquidity"],
                "volume": row["volume"],
                "mapping_verified": bool(row["mapping_verified"]),
                "snapshots": [dict(item) for item in snapshots],
            }
        )
    latest_by_type = {}
    for market in markets:
        latest_by_type.setdefault(market["type"], market)
    return [latest_by_type[k] for k in ("5m", "15m") if k in latest_by_type]


def _performance(conn: sqlite3.Connection, settings) -> dict:
    orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    fills = conn.execute("SELECT COUNT(*), COALESCE(SUM(size_usdc), 0) FROM fills").fetchone()
    pnl = conn.execute("SELECT COALESCE(SUM(realized_usdc), 0), COALESCE(SUM(unrealized_usdc), 0) FROM pnl").fetchone()
    positions = _paper_positions(conn)
    open_positions = [item for item in positions if item.get("status") == "OPEN"]
    pending_settlement = [item for item in positions if item.get("status") == "EXPIRED_UNKNOWN"]
    settled_positions = [item for item in positions if item.get("status") in {"WON", "LOST"}]
    wins = [item for item in settled_positions if item.get("status") == "WON"]
    losses = [item for item in settled_positions if item.get("status") == "LOST"]
    realized = sum(item["realized_pnl_usdc"] for item in settled_positions) or float(pnl[0])
    gross_profit = sum(max(0.0, item["realized_pnl_usdc"]) for item in settled_positions)
    gross_loss = abs(sum(min(0.0, item["realized_pnl_usdc"]) for item in settled_positions))
    unrealized = sum(item["unrealized_pnl_usdc"] for item in open_positions)
    total_fees = sum(item.get("fee_usdc", 0.0) for item in positions)
    reserved_cash = sum(item["net_cost_usdc"] for item in open_positions + pending_settlement)
    available_cash = settings.paper_bankroll_usdc + realized - reserved_cash
    equity = available_cash + reserved_cash + unrealized
    wallet = {
        "initial_cash_usdc": settings.paper_bankroll_usdc,
        "available_cash_usdc": available_cash,
        "reserved_cash_usdc": reserved_cash,
        "equity_usdc": equity,
        "net_pnl_usdc": equity - settings.paper_bankroll_usdc,
        "realized_pnl_usdc": realized,
        "unrealized_pnl_usdc": unrealized,
        "fees_paid_usdc": total_fees,
        "fee_model": "polymarket_crypto_taker" if settings.paper_enable_fees else "disabled",
        "fee_rate": settings.paper_taker_fee_rate if settings.paper_enable_fees else 0.0,
    }
    return {
        "total_orders": int(orders),
        "total_fills": int(fills[0]),
        "paper_volume_usdc": float(fills[1]),
        "paper_wallet": wallet,
        "fees_paid_usdc": total_fees,
        "realized_pnl_usdc": realized,
        "unrealized_pnl_usdc": unrealized if open_positions else float(pnl[1]),
        "open_positions_count": len(open_positions),
        "pending_settlement_count": len(pending_settlement),
        "open_exposure_usdc": sum(item["net_cost_usdc"] for item in open_positions),
        "pending_settlement_usdc": sum(item["net_cost_usdc"] for item in pending_settlement),
        "settled_trades": len(settled_positions),
        "settled_volume_usdc": sum(item["cost_usdc"] for item in settled_positions),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(settled_positions)) if settled_positions else None,
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else None,
        "positions": positions,
    }


def _execution_stats(conn: sqlite3.Connection) -> dict:
    cutoff = (datetime.now(UTC) - timedelta(hours=12)).isoformat()
    decisions = conn.execute("SELECT COUNT(*) FROM strategy_decisions WHERE created_at >= ?", (cutoff,)).fetchone()[0]
    candidates = conn.execute("SELECT COUNT(*) FROM strategy_decisions WHERE created_at >= ? AND action != 'HOLD'", (cutoff,)).fetchone()[0]
    approved = conn.execute("SELECT COUNT(*) FROM risk_events WHERE created_at >= ? AND approved = 1", (cutoff,)).fetchone()[0]
    fills = conn.execute("SELECT COUNT(*) FROM fills WHERE created_at >= ?", (cutoff,)).fetchone()[0]
    latest_fill = conn.execute("SELECT created_at FROM fills ORDER BY created_at DESC LIMIT 1").fetchone()
    open_positions = conn.execute(
        """
        SELECT COUNT(*), COALESCE(SUM(size_usdc), 0)
        FROM positions p
        LEFT JOIN markets m ON m.market_id = p.market_id
        WHERE p.status = 'OPEN'
          AND (m.end_time IS NULL OR m.end_time > ?)
        """,
        (datetime.now(UTC).isoformat(),),
    ).fetchone()
    top_blocks = [
        dict(row)
        for row in conn.execute(
            """
            SELECT reason, COUNT(*) AS count
            FROM risk_events
            WHERE created_at >= ?
              AND approved = 0
            GROUP BY reason
            ORDER BY count DESC
            LIMIT 8
            """,
            (cutoff,),
        ).fetchall()
    ]
    stale_risk = any(row["reason"] == "one open position limit hit" for row in top_blocks) and int(open_positions[0] or 0) == 0
    return {
        "window": "12h",
        "decisions": int(decisions or 0),
        "signal_candidates": int(candidates or 0),
        "risk_approved": int(approved or 0),
        "paper_fills": int(fills or 0),
        "latest_fill_at": latest_fill["created_at"] if latest_fill else None,
        "open_positions": int(open_positions[0] or 0),
        "open_exposure_usdc": float(open_positions[1] or 0),
        "top_blocks": top_blocks,
        "stale_risk_warning": stale_risk,
    }


def _paper_positions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT p.market_id, p.token_id, p.size_usdc AS cost_usdc, p.shares, p.fee_usdc,
               p.avg_price, p.status, p.realized_pnl_usdc, p.settlement_outcome, p.settled_at, p.updated_at,
               m.question, m.market_type, m.end_time
        FROM positions p
        LEFT JOIN markets m ON m.market_id = p.market_id
        WHERE p.status IN ('OPEN', 'EXPIRED_UNKNOWN', 'WON', 'LOST')
        ORDER BY p.updated_at DESC
        """
    ).fetchall()
    if not rows:
        rows = conn.execute(
            """
            SELECT f.market_id, f.token_id, SUM(f.size_usdc) AS cost_usdc,
                   SUM(f.size_usdc / NULLIF(f.price, 0)) AS shares,
                   COALESCE(SUM(f.fee_usdc), 0) AS fee_usdc,
                   AVG(f.price) AS avg_price, 'OPEN' AS status, 0 AS realized_pnl_usdc,
                   NULL AS settlement_outcome, NULL AS settled_at,
                   MAX(f.created_at) AS updated_at, m.question, m.market_type, m.end_time
            FROM fills f
            LEFT JOIN markets m ON m.market_id = f.market_id
            GROUP BY f.market_id, f.token_id
            ORDER BY updated_at DESC
            """
        ).fetchall()
    labels = _token_labels(conn)
    positions = []
    for row in rows:
        bid = _latest_token_bid(conn, row["market_id"], row["token_id"])
        shares = float(row["shares"] or 0)
        cost = float(row["cost_usdc"] or 0)
        fee = float(row["fee_usdc"] or 0)
        status = _position_status(row["status"], row["end_time"])
        current_value = shares * bid if bid is not None and status == "OPEN" else None
        positions.append(
            {
                "market_id": row["market_id"],
                "token_id": row["token_id"],
                "side": labels.get(row["token_id"], "UNKNOWN"),
                "question": row["question"],
                "market_type": row["market_type"],
                "status": status,
                "cost_usdc": cost,
                "fee_usdc": fee,
                "net_cost_usdc": cost + fee,
                "shares": shares,
                "avg_price": float(row["avg_price"] or 0),
                "mark_price": bid,
                "current_value_usdc": current_value,
                "unrealized_pnl_usdc": (current_value - cost - fee) if current_value is not None else 0.0,
                "realized_pnl_usdc": float(row["realized_pnl_usdc"] or 0),
                "settlement_status": _settlement_status(status),
                "settlement_outcome": row["settlement_outcome"],
                "settled_at": row["settled_at"],
                "updated_at": row["updated_at"],
            }
        )
    return positions


def _settlement_status(status: str) -> str:
    if status == "EXPIRED_UNKNOWN":
        return "pending result verification"
    if status in {"WON", "LOST"}:
        return "settled"
    return "mark to market"


def _position_status(status: str | None, end_time: str | None) -> str:
    if status and status != "OPEN":
        return status
    if not end_time:
        return "OPEN"
    try:
        parsed = datetime.fromisoformat(str(end_time).replace("Z", "+00:00"))
    except ValueError:
        return "OPEN"
    return "EXPIRED_UNKNOWN" if parsed <= datetime.now(UTC) else "OPEN"


def _latest_token_bid(conn: sqlite3.Connection, market_id: str, token_id: str) -> float | None:
    row = conn.execute(
        """
        SELECT best_bid FROM market_snapshots
        WHERE market_id = ? AND token_id = ? AND best_bid IS NOT NULL
        ORDER BY created_at DESC LIMIT 1
        """,
        (market_id, token_id),
    ).fetchone()
    return float(row["best_bid"]) if row else None


def _token_labels(conn: sqlite3.Connection) -> dict[str, str]:
    labels: dict[str, str] = {}
    for row in conn.execute("SELECT raw_json FROM markets").fetchall():
        try:
            raw = json.loads(row["raw_json"] or "{}")
            outcomes = json.loads(raw.get("outcomes") or "[]")
            token_ids = json.loads(raw.get("clobTokenIds") or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        for outcome, token_id in zip(outcomes, token_ids, strict=False):
            labels[str(token_id)] = str(outcome).upper()
    return labels


def _recent_operations(conn: sqlite3.Connection) -> list[dict[str, str]]:
    rows = []
    rows.extend(_recent(conn, "signals", "action || ' confidence=' || confidence || ' reason=' || ifnull(reason, '')"))
    rows.extend(_recent(conn, "orders", "status || ' ' || side || ' $' || size_usdc || ' @ ' || price"))
    rows.extend(_recent(conn, "fills", "side || ' $' || size_usdc || ' @ ' || price"))
    rows.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return rows


def _recent(conn: sqlite3.Connection, table: str, expression: str) -> list[dict[str, str]]:
    if table not in {"signals", "orders", "fills"}:
        return []
    rows = conn.execute(f"SELECT created_at, {expression} AS summary FROM {table} ORDER BY created_at DESC LIMIT 5").fetchall()
    return [{"kind": table, "created_at": row["created_at"], "summary": row["summary"]} for row in rows]


def _latest_learning(conn: sqlite3.Connection) -> list[dict]:
    return [dict(row) for row in conn.execute("SELECT note, tags, created_at FROM learning_notes ORDER BY created_at DESC LIMIT 6").fetchall()]


def _latest_rejections(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT market_type, question, slug, reason, created_at FROM discovery_rejections ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
    ]


def _latest_decisions(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT market_id, market_type, action, estimated_probability, market_price, edge,
                   ev_usdc, kelly_fraction, recommended_size_usdc, confidence, reason, created_at
            FROM strategy_decisions
            ORDER BY created_at DESC
            LIMIT 12
            """
        ).fetchall()
    ]


def _timeframe_stats(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT COALESCE(d.market_type, m.market_type) AS market_type,
               COUNT(d.id) AS decisions,
               AVG(d.edge) AS avg_edge,
               AVG(d.confidence) AS avg_confidence,
               AVG(d.kelly_fraction) AS avg_kelly,
               SUM(CASE WHEN d.action != 'HOLD' THEN 1 ELSE 0 END) AS entries
        FROM strategy_decisions d
        LEFT JOIN markets m ON m.market_id = d.market_id
        WHERE COALESCE(d.market_type, m.market_type) IN ('5m', '15m')
        GROUP BY COALESCE(d.market_type, m.market_type)
        """
    ).fetchall()
    stats = {row["market_type"]: dict(row) for row in rows}
    fill_rows = conn.execute(
        """
        SELECT m.market_type, COUNT(f.id) AS fills, COALESCE(SUM(f.size_usdc), 0) AS volume
        FROM fills f
        LEFT JOIN markets m ON m.market_id = f.market_id
        WHERE m.market_type IN ('5m', '15m')
        GROUP BY m.market_type
        """
    ).fetchall()
    for row in fill_rows:
        stats.setdefault(row["market_type"], {"market_type": row["market_type"]})
        stats[row["market_type"]]["fills"] = row["fills"]
        stats[row["market_type"]]["volume"] = row["volume"]
    settled_rows = conn.execute(
        """
        SELECT m.market_type,
               COUNT(p.id) AS settled,
               SUM(CASE WHEN p.status = 'WON' THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN p.status = 'LOST' THEN 1 ELSE 0 END) AS losses,
               COALESCE(SUM(p.realized_pnl_usdc), 0) AS realized_pnl,
               COALESCE(SUM(p.size_usdc), 0) AS settled_volume
        FROM positions p
        LEFT JOIN markets m ON m.market_id = p.market_id
        WHERE m.market_type IN ('5m', '15m')
          AND p.status IN ('WON', 'LOST')
        GROUP BY m.market_type
        """
    ).fetchall()
    for row in settled_rows:
        stats.setdefault(row["market_type"], {"market_type": row["market_type"]})
        stats[row["market_type"]]["settled"] = row["settled"]
        stats[row["market_type"]]["wins"] = row["wins"]
        stats[row["market_type"]]["losses"] = row["losses"]
        stats[row["market_type"]]["realized_pnl"] = row["realized_pnl"]
        stats[row["market_type"]]["settled_volume"] = row["settled_volume"]
    return [
        {
            "market_type": market_type,
            "decisions": int((stats.get(market_type) or {}).get("decisions") or 0),
            "entries": int((stats.get(market_type) or {}).get("entries") or 0),
            "fills": int((stats.get(market_type) or {}).get("fills") or 0),
            "volume": float((stats.get(market_type) or {}).get("volume") or 0),
            "settled": int((stats.get(market_type) or {}).get("settled") or 0),
            "wins": int((stats.get(market_type) or {}).get("wins") or 0),
            "losses": int((stats.get(market_type) or {}).get("losses") or 0),
            "win_rate": (
                int((stats.get(market_type) or {}).get("wins") or 0) / int((stats.get(market_type) or {}).get("settled") or 1)
                if int((stats.get(market_type) or {}).get("settled") or 0) > 0
                else None
            ),
            "realized_pnl": float((stats.get(market_type) or {}).get("realized_pnl") or 0),
            "settled_volume": float((stats.get(market_type) or {}).get("settled_volume") or 0),
            "avg_edge": float((stats.get(market_type) or {}).get("avg_edge") or 0),
            "avg_confidence": float((stats.get(market_type) or {}).get("avg_confidence") or 0),
            "avg_kelly": float((stats.get(market_type) or {}).get("avg_kelly") or 0),
        }
        for market_type in ("5m", "15m")
    ]


def _hourly_distribution(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT substr(created_at, 12, 2) AS hour,
               COUNT(*) AS decisions,
               AVG(edge) AS avg_edge
        FROM strategy_decisions
        GROUP BY substr(created_at, 12, 2)
        ORDER BY hour
        """
    ).fetchall()
    by_hour = {int(row["hour"]): dict(row) for row in rows if row["hour"] is not None}
    return [
        {
            "hour": hour,
            "decisions": int((by_hour.get(hour) or {}).get("decisions") or 0),
            "avg_edge": float((by_hour.get(hour) or {}).get("avg_edge") or 0),
        }
        for hour in range(24)
    ]


def _position_outcomes(conn: sqlite3.Connection) -> list[dict]:
    positions = _paper_positions(conn)
    ranked = sorted(
        positions,
        key=lambda item: item.get("settled_at") or item.get("updated_at") or "",
        reverse=True,
    )
    return [
        {
            "market_type": item.get("market_type"),
            "question": item.get("question"),
            "side": item.get("side"),
            "status": item.get("status"),
            "cost_usdc": item.get("cost_usdc", 0.0),
            "fee_usdc": item.get("fee_usdc", 0.0),
            "realized_pnl_usdc": item.get("realized_pnl_usdc", 0.0),
            "unrealized_pnl_usdc": item.get("unrealized_pnl_usdc", 0.0),
            "updated_at": item.get("settled_at") or item.get("updated_at"),
        }
        for item in ranked[:32]
    ]


def _analytics_positions(conn: sqlite3.Connection) -> list[dict]:
    positions = _paper_positions(conn)
    if positions:
        return positions[:12]
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT f.created_at AS updated_at, m.market_type, m.question, f.side,
                   f.price AS avg_price, f.size_usdc AS cost_usdc, f.pnl_usdc AS realized_pnl_usdc,
                   'FILL_ONLY' AS status
            FROM fills f
            LEFT JOIN markets m ON m.market_id = f.market_id
            ORDER BY f.created_at DESC
            LIMIT 12
            """
        ).fetchall()
    ]


def _last_row(conn: sqlite3.Connection, table: str):
    if table != "risk_events":
        return None
    return conn.execute("SELECT * FROM risk_events ORDER BY created_at DESC LIMIT 1").fetchone()


def _last_health(conn: sqlite3.Connection, name: str) -> dict | None:
    row = conn.execute("SELECT status, detail, created_at FROM health_events WHERE name = ? ORDER BY created_at DESC LIMIT 1", (name,)).fetchone()
    return dict(row) if row else None


def main() -> None:
    host = "127.0.0.1"
    port = 8888
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Tradingbot frontend serving on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
