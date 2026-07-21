from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from datetime import UTC, datetime, timedelta


SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
  market_id TEXT PRIMARY KEY,
  event_id TEXT,
  question TEXT NOT NULL,
  slug TEXT NOT NULL,
  market_type TEXT NOT NULL,
  start_time TEXT,
  end_time TEXT,
  liquidity REAL,
  volume REAL,
  mapping_verified INTEGER NOT NULL,
  raw_json TEXT
);
CREATE TABLE IF NOT EXISTS market_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_id TEXT NOT NULL,
  token_id TEXT NOT NULL,
  best_bid REAL,
  best_ask REAL,
  spread REAL,
  liquidity REAL,
  imbalance REAL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS btc_ticks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  price REAL NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS market_open_prices (
  market_id TEXT PRIMARY KEY,
  price REAL NOT NULL,
  source TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_id TEXT NOT NULL,
  action TEXT NOT NULL,
  confidence REAL NOT NULL,
  max_price REAL NOT NULL,
  size_usdc REAL NOT NULL,
  reason TEXT,
  policy_version TEXT,
  metadata_json TEXT,
  config_snapshot_json TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
  order_id TEXT PRIMARY KEY,
  market_id TEXT NOT NULL,
  token_id TEXT NOT NULL,
  side TEXT NOT NULL,
  status TEXT NOT NULL,
  price REAL NOT NULL,
  size_usdc REAL NOT NULL,
  filled_size_usdc REAL,
  avg_fill_price REAL,
  reason TEXT,
  policy_version TEXT,
  metadata_json TEXT,
  config_snapshot_json TEXT,
  execution_style TEXT NOT NULL DEFAULT 'taker',
  expires_at TEXT,
  updated_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id TEXT NOT NULL,
  market_id TEXT NOT NULL,
  token_id TEXT NOT NULL,
  side TEXT NOT NULL,
  price REAL NOT NULL,
  size_usdc REAL NOT NULL,
  fee_usdc REAL DEFAULT 0,
  pnl_usdc REAL NOT NULL,
  policy_version TEXT,
  metadata_json TEXT,
  config_snapshot_json TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_id TEXT NOT NULL,
  token_id TEXT NOT NULL,
  size_usdc REAL NOT NULL,
  avg_price REAL NOT NULL,
  shares REAL DEFAULT 0,
  fee_usdc REAL DEFAULT 0,
  status TEXT DEFAULT 'OPEN',
  realized_pnl_usdc REAL DEFAULT 0,
  policy_version TEXT,
  estimated_probability REAL,
  break_even_probability REAL,
  net_edge_cents REAL,
  metadata_json TEXT,
  config_snapshot_json TEXT,
  settlement_outcome TEXT,
  settled_at TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pnl (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_id TEXT,
  realized_usdc REAL NOT NULL,
  unrealized_usdc REAL NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_id TEXT,
  approved INTEGER NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS health_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  status TEXT NOT NULL,
  detail TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_state (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rag_documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_path TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  tags TEXT,
  mtime REAL NOT NULL,
  indexed_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS rag_documents_fts USING fts5(
  title,
  content,
  tags,
  content='rag_documents',
  content_rowid='id',
  tokenize='porter unicode61 remove_diacritics 2',
  prefix='2,3'
);
CREATE TRIGGER IF NOT EXISTS rag_documents_ai AFTER INSERT ON rag_documents BEGIN
  INSERT INTO rag_documents_fts(rowid, title, content, tags)
  VALUES (new.id, new.title, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS rag_documents_ad AFTER DELETE ON rag_documents BEGIN
  INSERT INTO rag_documents_fts(rag_documents_fts, rowid, title, content, tags)
  VALUES ('delete', old.id, old.title, old.content, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS rag_documents_au AFTER UPDATE ON rag_documents BEGIN
  INSERT INTO rag_documents_fts(rag_documents_fts, rowid, title, content, tags)
  VALUES ('delete', old.id, old.title, old.content, old.tags);
  INSERT INTO rag_documents_fts(rowid, title, content, tags)
  VALUES (new.id, new.title, new.content, new.tags);
END;
CREATE TABLE IF NOT EXISTS learning_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  note TEXT NOT NULL,
  tags TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS learning_recommendations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL,
  scope TEXT NOT NULL,
  metric TEXT NOT NULL,
  recommendation TEXT NOT NULL,
  rationale TEXT NOT NULL,
  confidence REAL NOT NULL,
  sample_size INTEGER NOT NULL,
  suggested_config_json TEXT
);
CREATE TABLE IF NOT EXISTS policy_versions (
  version TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK(status IN ('candidate', 'paper_active', 'validated', 'rejected', 'stopped')),
  is_active INTEGER NOT NULL DEFAULT 0 CHECK(is_active IN (0, 1)),
  config_json TEXT NOT NULL,
  config_sha256 TEXT,
  oos_metrics_json TEXT,
  evidence_sha256 TEXT,
  model_sha256 TEXT,
  rejection_reason TEXT,
  created_at TEXT NOT NULL,
  activated_at TEXT,
  evaluated_at TEXT
);
CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discovery_rejections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_type TEXT,
  question TEXT,
  slug TEXT,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discovery_rejection_rollups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_type TEXT NOT NULL DEFAULT '',
  question TEXT NOT NULL DEFAULT '',
  slug TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL,
  bucket_start TEXT NOT NULL,
  occurrences INTEGER NOT NULL DEFAULT 1,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  UNIQUE(market_type, slug, reason, bucket_start)
);
CREATE TABLE IF NOT EXISTS strategy_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_id TEXT NOT NULL,
  market_type TEXT,
  action TEXT NOT NULL,
  estimated_probability REAL,
  market_price REAL,
  edge REAL,
  ev_usdc REAL,
  kelly_fraction REAL,
  recommended_size_usdc REAL,
  confidence REAL NOT NULL,
  reason TEXT,
  metadata_json TEXT,
  policy_version TEXT,
  config_snapshot_json TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_snapshots_market_created ON market_snapshots(market_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_market_snapshots_created ON market_snapshots(created_at);
CREATE INDEX IF NOT EXISTS idx_btc_ticks_created ON btc_ticks(created_at);
CREATE INDEX IF NOT EXISTS idx_signals_market_created ON signals(market_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_fills_created ON fills(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_risk_events_created ON risk_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_health_events_name_created ON health_events(name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_learning_notes_created ON learning_notes(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_discovery_rejections_created ON discovery_rejections(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_decisions_created ON strategy_decisions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_learning_recommendations_created ON learning_recommendations(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_policy_versions_status ON policy_versions(status);
CREATE INDEX IF NOT EXISTS idx_discovery_rejection_rollups_last_seen
ON discovery_rejection_rollups(last_seen_at DESC);
"""

MIGRATIONS = (
    (
        "20260714_analytics_snapshot_lookup",
        "analytics snapshot lookup index",
        """
        CREATE INDEX IF NOT EXISTS idx_market_snapshots_market_token_created
        ON market_snapshots(market_id, token_id, created_at DESC, best_bid)
        """,
    ),
    (
        "20260714_analytics_decision_core",
        "analytics decision core covering index",
        """
        CREATE INDEX IF NOT EXISTS idx_strategy_decisions_analytics_core
        ON strategy_decisions(market_type, action, edge, confidence, kelly_fraction)
        """,
    ),
    (
        "20260714_analytics_decision_reason",
        "analytics decision reason index",
        """
        CREATE INDEX IF NOT EXISTS idx_strategy_decisions_reason
        ON strategy_decisions(reason)
        """,
    ),
    (
        "20260714_analytics_decision_hour",
        "analytics decision hour covering index",
        """
        CREATE INDEX IF NOT EXISTS idx_strategy_decisions_hour_edge
        ON strategy_decisions(substr(created_at, 12, 2), edge)
        """,
    ),
    (
        "20260714_analytics_rejection_count",
        "analytics rejection count covering index",
        """
        CREATE INDEX IF NOT EXISTS idx_discovery_rejections_count
        ON discovery_rejections(id)
        """,
    ),
    (
        "20260715_policy_single_active",
        "enforce one active policy",
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_policy_versions_single_active
        ON policy_versions(is_active) WHERE is_active = 1
        """,
    ),
)


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(path: Path) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        _ensure_column(conn, "positions", "shares", "REAL DEFAULT 0")
        _ensure_column(conn, "positions", "fee_usdc", "REAL DEFAULT 0")
        _ensure_column(conn, "positions", "status", "TEXT DEFAULT 'OPEN'")
        _ensure_column(conn, "positions", "realized_pnl_usdc", "REAL DEFAULT 0")
        _ensure_policy_columns(conn)
        _ensure_order_lifecycle_columns(conn)
        _run_migrations(conn)
        _ensure_column(conn, "positions", "settlement_outcome", "TEXT")
        _ensure_column(conn, "positions", "settled_at", "TEXT")
        _ensure_column(conn, "fills", "fee_usdc", "REAL DEFAULT 0")
        _backfill_open_positions(conn)
        _expire_unknown_positions(conn)
        _settle_binary_positions(conn)


def refresh_settlements(conn: sqlite3.Connection, retention_days: int | None = None) -> None:
    """Backfill/expire/settle positions on an existing connection.

    Lightweight per-cycle alternative to init_db: it skips re-running the full
    schema DDL and reconnecting, but keeps settlement (and therefore realized
    PnL / loss-streak risk state) up to date while the paper loop runs.

    When `retention_days` is set, old snapshots/ticks are also pruned at most
    once per day (bulk data grows ~228 MB/day on the production Pi otherwise).
    """
    _backfill_open_positions(conn)
    _expire_unknown_positions(conn)
    _settle_binary_positions(conn)
    conn.commit()
    if retention_days is not None:
        maybe_prune_old_data(conn, retention_days)


def prune_old_data(conn: sqlite3.Connection, retention_days: int = 7) -> dict[str, int | str]:
    """Delete bulk time-series rows older than the retention window.

    Only `market_snapshots` and `btc_ticks` are pruned; decisions, fills and
    positions are lightweight and stay forever (they feed calibration).
    """
    cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
    snapshots = conn.execute("DELETE FROM market_snapshots WHERE created_at < ?", (cutoff,)).rowcount
    ticks = conn.execute("DELETE FROM btc_ticks WHERE created_at < ?", (cutoff,)).rowcount
    conn.commit()
    return {"market_snapshots_deleted": int(snapshots), "btc_ticks_deleted": int(ticks), "cutoff": cutoff}


def maybe_prune_old_data(conn: sqlite3.Connection, retention_days: int, min_interval_hours: float = 24.0) -> dict | None:
    """Run prune_old_data at most once per `min_interval_hours`, tracked in paper_state."""
    now = datetime.now(UTC)
    row = conn.execute("SELECT value_json FROM paper_state WHERE key = 'last_prune'").fetchone()
    if row:
        try:
            last = datetime.fromisoformat(json.loads(row["value_json"]).get("ran_at", ""))
            if (now - last).total_seconds() < min_interval_hours * 3600:
                return None
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    result = prune_old_data(conn, retention_days)
    conn.execute(
        "INSERT OR REPLACE INTO paper_state (key, value_json, updated_at) VALUES ('last_prune', ?, ?)",
        (json.dumps({"ran_at": now.isoformat(), **result}), now.isoformat()),
    )
    conn.commit()
    return result


def force_settle_pending_positions(path: Path) -> dict[str, int]:
    started_at = datetime.now(UTC).isoformat()
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        _ensure_column(conn, "positions", "shares", "REAL DEFAULT 0")
        _ensure_column(conn, "positions", "fee_usdc", "REAL DEFAULT 0")
        _ensure_column(conn, "positions", "status", "TEXT DEFAULT 'OPEN'")
        _ensure_column(conn, "positions", "realized_pnl_usdc", "REAL DEFAULT 0")
        _ensure_policy_columns(conn)
        _ensure_order_lifecycle_columns(conn)
        _run_migrations(conn)
        _ensure_column(conn, "positions", "settlement_outcome", "TEXT")
        _ensure_column(conn, "positions", "settled_at", "TEXT")
        _ensure_column(conn, "fills", "fee_usdc", "REAL DEFAULT 0")
        before_pending = _count_pending_positions(conn)
        before_settled = _count_settled_positions(conn)
        _backfill_open_positions(conn)
        _expire_unknown_positions(conn)
        _settle_binary_positions(conn)
        after_pending = _count_pending_positions(conn)
        after_settled = _count_settled_positions(conn)
        touched = conn.execute("SELECT COUNT(*) FROM positions WHERE settled_at >= ?", (started_at,)).fetchone()[0]
    return {
        "pending_before": int(before_pending),
        "pending_after": int(after_pending),
        "settled_before": int(before_settled),
        "settled_after": int(after_settled),
        "settled_now": int(touched),
    }


def _count_pending_positions(conn: sqlite3.Connection) -> int:
    now = datetime.now(UTC).isoformat()
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM positions p
            LEFT JOIN markets m ON m.market_id = p.market_id
            WHERE p.status = 'EXPIRED_UNKNOWN'
               OR (p.status = 'OPEN' AND m.end_time IS NOT NULL AND m.end_time <= ?)
            """,
            (now,),
        ).fetchone()[0]
    )


def _count_settled_positions(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM positions WHERE status IN ('WON', 'LOST')").fetchone()[0])


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _run_migrations(conn: sqlite3.Connection) -> None:
    for version, name, sql in MIGRATIONS:
        applied = conn.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (version,)).fetchone()
        if applied:
            continue
        conn.execute(sql)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (version, name, datetime.now(UTC).isoformat()),
        )


def _ensure_policy_columns(conn: sqlite3.Connection) -> None:
    for table in ("signals", "orders", "fills", "strategy_decisions"):
        _ensure_column(conn, table, "policy_version", "TEXT")
        _ensure_column(conn, table, "metadata_json", "TEXT")
        _ensure_column(conn, table, "config_snapshot_json", "TEXT")
    _ensure_column(conn, "positions", "policy_version", "TEXT")
    _ensure_column(conn, "positions", "estimated_probability", "REAL")
    _ensure_column(conn, "positions", "break_even_probability", "REAL")
    _ensure_column(conn, "positions", "net_edge_cents", "REAL")
    _ensure_column(conn, "positions", "metadata_json", "TEXT")
    _ensure_column(conn, "positions", "config_snapshot_json", "TEXT")
    _ensure_column(conn, "policy_versions", "is_active", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "policy_versions", "config_sha256", "TEXT")
    _ensure_column(conn, "policy_versions", "evidence_sha256", "TEXT")
    _ensure_column(conn, "policy_versions", "model_sha256", "TEXT")


def _ensure_order_lifecycle_columns(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "orders", "execution_style", "TEXT NOT NULL DEFAULT 'taker'")
    _ensure_column(conn, "orders", "expires_at", "TEXT")
    _ensure_column(conn, "orders", "updated_at", "TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_open_execution_expiry "
        "ON orders(status, execution_style, expires_at)"
    )


def _backfill_open_positions(conn: sqlite3.Connection) -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO positions (
            market_id, token_id, size_usdc, avg_price, shares, fee_usdc,
            status, realized_pnl_usdc, policy_version, estimated_probability,
            break_even_probability, net_edge_cents, metadata_json,
            config_snapshot_json, updated_at
        )
        SELECT f.market_id,
               f.token_id,
               SUM(f.size_usdc) AS size_usdc,
               SUM(f.size_usdc) / NULLIF(SUM(f.size_usdc / NULLIF(f.price, 0)), 0) AS avg_price,
               SUM(f.size_usdc / NULLIF(f.price, 0)) AS shares,
               COALESCE(SUM(f.fee_usdc), 0) AS fee_usdc,
               CASE WHEN m.end_time IS NOT NULL AND m.end_time <= ? THEN 'EXPIRED_UNKNOWN' ELSE 'OPEN' END,
               0,
               MAX(f.policy_version),
               AVG(json_extract(f.metadata_json, '$.estimated_probability')),
               AVG(json_extract(f.metadata_json, '$.break_even_probability_after_fees')),
               AVG(json_extract(f.metadata_json, '$.net_edge_cents')),
               MAX(f.metadata_json),
               MAX(f.config_snapshot_json),
               MAX(f.created_at)
        FROM fills f
        LEFT JOIN markets m ON m.market_id = f.market_id
        WHERE NOT EXISTS (
            SELECT 1 FROM positions p
            WHERE p.market_id = f.market_id AND p.token_id = f.token_id
          )
        GROUP BY f.market_id, f.token_id
        """,
        (now,),
    )


def _expire_unknown_positions(conn: sqlite3.Connection) -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        UPDATE positions
        SET status = 'EXPIRED_UNKNOWN',
            updated_at = ?
        WHERE status = 'OPEN'
          AND EXISTS (
            SELECT 1 FROM markets m
            WHERE m.market_id = positions.market_id
              AND m.end_time IS NOT NULL
              AND m.end_time <= ?
          )
        """,
        (now, now),
    )


def _settle_binary_positions(conn: sqlite3.Connection) -> None:
    now = datetime.now(UTC).isoformat()
    rows = conn.execute(
        """
        SELECT p.id, p.token_id, p.size_usdc, p.shares, p.fee_usdc, m.raw_json
        FROM positions p
        JOIN markets m ON m.market_id = p.market_id
        WHERE p.status = 'EXPIRED_UNKNOWN'
        """
    ).fetchall()
    for row in rows:
        winner = _verified_winner_token_id(row["raw_json"])
        if winner is None:
            continue
        won = str(row["token_id"]) == winner
        cost = float(row["size_usdc"] or 0)
        shares = float(row["shares"] or 0)
        fee = float(row["fee_usdc"] or 0)
        realized = (shares - cost - fee) if won else -(cost + fee)
        conn.execute(
            """
            UPDATE positions
            SET status = ?,
                realized_pnl_usdc = ?,
                settlement_outcome = ?,
                settled_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            ("WON" if won else "LOST", realized, winner, now, now, row["id"]),
        )


def _verified_winner_token_id(raw_json: str | None) -> str | None:
    if not raw_json:
        return None
    try:
        raw = json.loads(raw_json)
        token_ids = _jsonish_list(raw.get("clobTokenIds") or raw.get("clob_token_ids"))
        prices = [float(item) for item in _jsonish_list(raw.get("outcomePrices"))]
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if len(token_ids) != len(prices) or len(prices) < 2:
        return None
    max_price = max(prices)
    min_price = min(prices)
    if max_price < 0.99 or min_price > 0.01:
        return None
    if prices.count(max_price) != 1:
        return None
    return str(token_ids[prices.index(max_price)])


def _jsonish_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        return decoded if isinstance(decoded, list) else []
    return []
