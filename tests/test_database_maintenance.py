from datetime import UTC, datetime

from bot.storage.db import connect, init_db
from bot.storage.maintenance import compact_database


def test_compaction_preserves_source_and_actionable_records(settings, tmp_path):
    init_db(settings.sqlite_path)
    created_at = datetime.now(UTC).replace(second=10, microsecond=0).isoformat()
    with connect(settings.sqlite_path) as conn:
        for _ in range(2):
            conn.execute(
                "INSERT INTO discovery_rejections (market_type, question, slug, reason, created_at) VALUES ('5m', 'q', 's', 'r', ?)",
                (created_at,),
            )
            conn.execute(
                "INSERT INTO signals (market_id, action, confidence, max_price, size_usdc, reason, created_at) VALUES ('m', 'HOLD', 0, 0, 0, 'wait', ?)",
                (created_at,),
            )
            conn.execute(
                "INSERT INTO signals (market_id, action, confidence, max_price, size_usdc, reason, created_at) VALUES ('m', 'BUY_UP', 0.9, 0.7, 1, 'trade', ?)",
                (created_at,),
            )
        conn.commit()

    output = tmp_path / "compacted.sqlite3"
    result = compact_database(settings.sqlite_path, output, tmp_path / "backups")

    with connect(settings.sqlite_path) as source:
        assert source.execute("SELECT COUNT(*) FROM discovery_rejections").fetchone()[0] == 2
        assert source.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 4
    with connect(output) as compacted:
        assert compacted.execute("SELECT COUNT(*) FROM discovery_rejections").fetchone()[0] == 0
        assert compacted.execute("SELECT occurrences FROM discovery_rejection_rollups").fetchone()[0] == 2
        assert compacted.execute("SELECT COUNT(*) FROM signals WHERE action = 'HOLD'").fetchone()[0] == 1
        assert compacted.execute("SELECT COUNT(*) FROM signals WHERE action = 'BUY_UP'").fetchone()[0] == 2
        assert compacted.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    assert result["integrity_check"] == "ok"
    assert result["backup_path"].exists()
    assert len(result["backup_sha256"]) == 64
