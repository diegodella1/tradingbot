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

systemctl stop tradingbot-paper.service tradingbot-frontend.service

rollback() {
  systemctl stop tradingbot-paper.service tradingbot-frontend.service || true
  if [ -e "$DATABASE" ] && [ -e "$PREVIOUS" ]; then
    mv "$DATABASE" "$FAILED"
    mv "$PREVIOUS" "$DATABASE"
  fi
  systemctl start tradingbot-paper.service tradingbot-frontend.service || true
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
systemctl start tradingbot-paper.service tradingbot-frontend.service

for _ in $(seq 1 30); do
  if curl -fsS --max-time 3 http://127.0.0.1:8888/api/healthz | \
    "$ROOT_DIR/.venv/bin/python" -c 'import json,sys; p=json.load(sys.stdin); raise SystemExit(0 if p.get("ok") else 1)'; then
    trap - ERR
    echo "maintenance_compaction_ok=true previous=$PREVIOUS database=$DATABASE"
    exit 0
  fi
  sleep 1
done

echo "ERROR maintenance_health_timeout=true"
false
