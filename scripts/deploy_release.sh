#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_ROOT="${DEPLOY_ROOT:-/home/diego/.local/share/tradingbot}"
REQUIRE_PUBLISHED="${REQUIRE_PUBLISHED:-true}"
SHA="${1:-$(git -C "$ROOT_DIR" rev-parse HEAD)}"
SHA="$(git -C "$ROOT_DIR" rev-parse "$SHA^{commit}")"
RELEASE_DIR="$DEPLOY_ROOT/releases/$SHA"
CURRENT_LINK="$DEPLOY_ROOT/current"
NEXT_LINK="$DEPLOY_ROOT/.current.next"

if [ -n "$(git -C "$ROOT_DIR" status --porcelain)" ]; then
  echo "ERROR dirty_worktree=true"
  exit 1
fi

if [ "$REQUIRE_PUBLISHED" = "true" ]; then
  REMOTE_SHA="$(git -C "$ROOT_DIR" ls-remote origin refs/heads/main | awk '{print $1}')"
  if [ "$REMOTE_SHA" != "$SHA" ]; then
    echo "ERROR unpublished_commit=true local=$SHA remote=$REMOTE_SHA"
    exit 1
  fi
fi

"$ROOT_DIR/.venv/bin/python" -m pytest -q

mkdir -p "$DEPLOY_ROOT/releases"
if [ ! -d "$RELEASE_DIR" ]; then
  mkdir -p "$RELEASE_DIR"
  git -C "$ROOT_DIR" archive --format=tar "$SHA" | tar -xf - -C "$RELEASE_DIR"
fi
printf 'DEPLOY_COMMIT=%s\n' "$SHA" > "$RELEASE_DIR/deploy.env"

PREVIOUS_RELEASE="$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)"
ln -sfn "$RELEASE_DIR" "$NEXT_LINK"
mv -Tf "$NEXT_LINK" "$CURRENT_LINK"

rollback() {
  if [ -n "$PREVIOUS_RELEASE" ] && [ -d "$PREVIOUS_RELEASE" ]; then
    ln -sfn "$PREVIOUS_RELEASE" "$NEXT_LINK"
    mv -Tf "$NEXT_LINK" "$CURRENT_LINK"
    systemctl restart tradingbot-paper.service tradingbot-frontend.service
    echo "deployment_rolled_back_to=$PREVIOUS_RELEASE"
  fi
}
trap rollback ERR

systemctl restart tradingbot-paper.service tradingbot-frontend.service

for _ in $(seq 1 30); do
  HEALTH="$(curl -fsS --max-time 3 http://127.0.0.1:8888/api/healthz 2>/dev/null || true)"
  if HEALTH="$HEALTH" EXPECTED_SHA="$SHA" "$ROOT_DIR/.venv/bin/python" -c \
    'import json,os; p=json.loads(os.environ["HEALTH"]); raise SystemExit(0 if p.get("ok") and p.get("deploy_commit")==os.environ["EXPECTED_SHA"] else 1)' 2>/dev/null; then
    trap - ERR
    echo "deployment_ok=true commit=$SHA release=$RELEASE_DIR"
    exit 0
  fi
  sleep 1
done

echo "ERROR deployment_health_timeout=true commit=$SHA"
false
