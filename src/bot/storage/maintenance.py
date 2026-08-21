from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from bot.storage.db import init_db, prune_old_data


DEDUPLICATION_RULES = {
    "signals": ("action = 'HOLD'", "market_id, action, COALESCE(reason, ''), COALESCE(policy_version, ''), substr(created_at, 1, 16)"),
    "strategy_decisions": ("action = 'HOLD'", "market_id, action, COALESCE(reason, ''), COALESCE(policy_version, ''), substr(created_at, 1, 16)"),
    "risk_events": ("approved = 0", "COALESCE(market_id, ''), reason, substr(created_at, 1, 16)"),
    "health_events": ("1 = 1", "name, status, COALESCE(detail, ''), substr(created_at, 1, 16)"),
    "learning_notes": ("1 = 1", "note, COALESCE(tags, ''), substr(created_at, 1, 16)"),
}


def compact_database(source: Path, output: Path, backup_directory: Path, retention_days: int = 7) -> dict:
    source = source.resolve()
    output = output.resolve()
    backup_directory = backup_directory.resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    if output.exists():
        raise FileExistsError(output)

    backup_directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_directory / f"{source.stem}-{timestamp}.sqlite3"

    source_uri = f"{source.as_uri()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_connection:
        integrity = source_connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"source integrity check failed: {integrity}")
        _backup(source_connection, backup_path)
        _backup(source_connection, output)

    init_db(output)
    with sqlite3.connect(output) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        before = _telemetry_counts(connection)
        with connection:
            _roll_up_legacy_rejections(connection)
            for table, (predicate, partition) in DEDUPLICATION_RULES.items():
                _deduplicate(connection, table, predicate, partition)
        pruned = prune_old_data(connection, retention_days)
        connection.execute("ANALYZE")
        connection.execute("PRAGMA optimize")
        connection.execute("VACUUM")
        after = _telemetry_counts(connection)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"compacted integrity check failed: {integrity}")

    output.chmod(0o600)
    backup_path.chmod(0o600)
    return {
        "source": source,
        "output": output,
        "backup_path": backup_path,
        "backup_sha256": _sha256(backup_path),
        "output_sha256": _sha256(output),
        "integrity_check": integrity,
        "before": before,
        "after": after,
        "pruned": pruned,
    }


def _backup(source: sqlite3.Connection, destination_path: Path) -> None:
    with sqlite3.connect(destination_path) as destination:
        source.backup(destination, pages=4096, sleep=0.05)


def _roll_up_legacy_rejections(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO discovery_rejection_rollups (
            market_type, question, slug, reason, bucket_start,
            occurrences, first_seen_at, last_seen_at
        )
        SELECT COALESCE(market_type, ''), MAX(COALESCE(question, '')), COALESCE(slug, ''), reason,
               substr(created_at, 1, 13) || ':00:00+00:00', COUNT(*), MIN(created_at), MAX(created_at)
        FROM discovery_rejections
        GROUP BY COALESCE(market_type, ''), COALESCE(slug, ''), reason, substr(created_at, 1, 13)
        ON CONFLICT(market_type, slug, reason, bucket_start) DO UPDATE SET
            occurrences = discovery_rejection_rollups.occurrences + excluded.occurrences,
            first_seen_at = MIN(discovery_rejection_rollups.first_seen_at, excluded.first_seen_at),
            last_seen_at = MAX(discovery_rejection_rollups.last_seen_at, excluded.last_seen_at)
        """
    )
    connection.execute("DELETE FROM discovery_rejections")


def _deduplicate(
    connection: sqlite3.Connection,
    table: str,
    predicate: str,
    partition: str,
) -> None:
    if table not in DEDUPLICATION_RULES:
        raise ValueError(f"unsupported telemetry table: {table}")
    connection.execute(
        f"""
        DELETE FROM {table}
        WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY {partition} ORDER BY id DESC
                ) AS duplicate_rank
                FROM {table}
                WHERE {predicate}
            )
            WHERE duplicate_rank > 1
        )
        """
    )


def _telemetry_counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = tuple(DEDUPLICATION_RULES) + ("discovery_rejections", "discovery_rejection_rollups")
    return {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in tables
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
