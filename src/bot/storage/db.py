from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from datetime import UTC, datetime


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
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_id TEXT NOT NULL,
  action TEXT NOT NULL,
  confidence REAL NOT NULL,
  max_price REAL NOT NULL,
  size_usdc REAL NOT NULL,
  reason TEXT,
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
CREATE TABLE IF NOT EXISTS discovery_rejections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_type TEXT,
  question TEXT,
  slug TEXT,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
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
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_snapshots_market_created ON market_snapshots(market_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_market_created ON signals(market_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_fills_created ON fills(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_health_events_name_created ON health_events(name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_discovery_rejections_created ON discovery_rejections(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_decisions_created ON strategy_decisions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_learning_recommendations_created ON learning_recommendations(created_at DESC);
"""


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
        _ensure_column(conn, "positions", "settlement_outcome", "TEXT")
        _ensure_column(conn, "positions", "settled_at", "TEXT")
        _ensure_column(conn, "fills", "fee_usdc", "REAL DEFAULT 0")
        _backfill_open_positions(conn)
        _expire_unknown_positions(conn)
        _settle_binary_positions(conn)


def force_settle_pending_positions(path: Path) -> dict[str, int]:
    started_at = datetime.now(UTC).isoformat()
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        _ensure_column(conn, "positions", "shares", "REAL DEFAULT 0")
        _ensure_column(conn, "positions", "fee_usdc", "REAL DEFAULT 0")
        _ensure_column(conn, "positions", "status", "TEXT DEFAULT 'OPEN'")
        _ensure_column(conn, "positions", "realized_pnl_usdc", "REAL DEFAULT 0")
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


def _backfill_open_positions(conn: sqlite3.Connection) -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO positions (market_id, token_id, size_usdc, avg_price, shares, fee_usdc, status, realized_pnl_usdc, updated_at)
        SELECT f.market_id,
               f.token_id,
               SUM(f.size_usdc) AS size_usdc,
               SUM(f.size_usdc) / NULLIF(SUM(f.size_usdc / NULLIF(f.price, 0)), 0) AS avg_price,
               SUM(f.size_usdc / NULLIF(f.price, 0)) AS shares,
               COALESCE(SUM(f.fee_usdc), 0) AS fee_usdc,
               CASE WHEN m.end_time IS NOT NULL AND m.end_time <= ? THEN 'EXPIRED_UNKNOWN' ELSE 'OPEN' END,
               0,
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
