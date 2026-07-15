#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATABASE="${SQLITE_PATH:-$ROOT_DIR/bot.sqlite3}"
OUTPUT="$DATABASE.compacted"
BACKUP_DIR="${BACKUP_DIR:-$ROOT_DIR/backups/maintenance}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
PREVIOUS="$DATABASE.precompact.$STAMP"
FAILED="$DATABASE.failed.$STAMP"

if [ -e "$OUTPUT" ]; then
  echo "ERROR compact_output_exists=$OUTPUT"
  exit 1
fi

sudo systemctl stop tradingbot-paper.service tradingbot-frontend.service

rollback() {
  sudo systemctl stop tradingbot-paper.service tradingbot-frontend.service || true
  if [ -e "$DATABASE" ] && [ -e "$PREVIOUS" ]; then
    mv "$DATABASE" "$FAILED"
    mv "$PREVIOUS" "$DATABASE"
  fi
  sudo systemctl start tradingbot-paper.service tradingbot-frontend.service || true
  echo "maintenance_rollback=true restored=$DATABASE failed_copy=$FAILED"
}
trap rollback ERR

"$ROOT_DIR/.venv/bin/python" -c \
  'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())' \
  "$DATABASE"
PYTHONPATH="$ROOT_DIR/src" "$ROOT_DIR/.venv/bin/python" \
  "$ROOT_DIR/scripts/compact_database.py" "$DATABASE" "$OUTPUT" --backup-directory "$BACKUP_DIR"

mv "$DATABASE" "$PREVIOUS"
rm -f "$DATABASE-wal" "$DATABASE-shm"
mv "$OUTPUT" "$DATABASE"
sudo systemctl start tradingbot-paper.service tradingbot-frontend.service

for _ in $(seq 1 30); do
  HEALTH="$(curl -fsS --max-time 3 http://127.0.0.1:8888/api/healthz 2>/dev/null || true)"
  if [ -n "$HEALTH" ] && \
    "$ROOT_DIR/.venv/bin/python" -c \
      'import json,sys; raise SystemExit(0 if json.loads(sys.argv[1]).get("ok") else 1)' \
      "$HEALTH" 2>/dev/null; then
    trap - ERR
    rm -f "$PREVIOUS"
    echo "maintenance_compaction_ok=true backup_directory=$BACKUP_DIR database=$DATABASE"
    exit 0
  fi
  sleep 1
done

echo "ERROR maintenance_health_timeout=true"
false
