#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_ROOT="${DEPLOY_ROOT:-/home/diego/.local/share/tradingbot}"
REQUIRE_PUBLISHED="${REQUIRE_PUBLISHED:-true}"
INSTALL_RELEASE_UNITS="${INSTALL_RELEASE_UNITS:-true}"
BUILD_ONLY="${BUILD_ONLY:-false}"
ACTIVATE_MAKER_EXPERIMENT="${ACTIVATE_MAKER_EXPERIMENT:-false}"
MAKER_EXPERIMENT_VERSION="${MAKER_EXPERIMENT_VERSION:-btc-updown-v4-maker-experiment}"
SHA="${1:-$(git -C "$ROOT_DIR" rev-parse HEAD)}"
SHA="$(git -C "$ROOT_DIR" rev-parse "$SHA^{commit}")"
RELEASE_DIR="$DEPLOY_ROOT/releases/$SHA"
CURRENT_LINK="$DEPLOY_ROOT/current"
CURRENT_ENV_LINK="$DEPLOY_ROOT/current.env"
RELEASE_ENV="$DEPLOY_ROOT/release-env/$SHA.env"
NEXT_LINK="$DEPLOY_ROOT/.current.next"
NEXT_ENV_LINK="$DEPLOY_ROOT/.current-env.next"
BUILD_DIR=""
VERIFY_DIR=""

cleanup() {
  if [ -n "$BUILD_DIR" ] && [ -d "$BUILD_DIR" ]; then
    rm -rf -- "$BUILD_DIR"
  fi
  if [ -n "$VERIFY_DIR" ] && [ -d "$VERIFY_DIR" ]; then
    rm -rf -- "$VERIFY_DIR"
  fi
}
trap cleanup EXIT

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

uv lock --project "$ROOT_DIR" --check
"$ROOT_DIR/.venv/bin/python" -m pytest -q

verify_release_source() {
  local target="$1"
  VERIFY_DIR="$(mktemp -d "$DEPLOY_ROOT/.verify-$SHA.XXXXXX")"
  git -C "$ROOT_DIR" archive --format=tar "$SHA" | tar -xf - -C "$VERIFY_DIR"
  if ! diff -qr --exclude=.venv --exclude=.release.json "$VERIFY_DIR" "$target" >/dev/null; then
    echo "ERROR release_source_mismatch=true release=$target"
    diff -qr --exclude=.venv --exclude=.release.json "$VERIFY_DIR" "$target" || true
    return 1
  fi
  rm -rf -- "$VERIFY_DIR"
  VERIFY_DIR=""
}

verify_release_read_only() {
  local target="$1"
  local writable_path
  writable_path="$(find "$target" ! -type l -perm /222 -print -quit)"
  if [ -n "$writable_path" ]; then
    echo "ERROR release_is_writable=true release=$target path=$writable_path"
    return 1
  fi
}

mkdir -p "$DEPLOY_ROOT/releases" "$DEPLOY_ROOT/release-env"
if [ -d "$RELEASE_DIR" ]; then
  if [ ! -f "$RELEASE_DIR/.release.json" ] || ! grep -Fq "\"commit\":\"$SHA\"" "$RELEASE_DIR/.release.json"; then
    echo "ERROR release_manifest_invalid=true release=$RELEASE_DIR"
    exit 1
  fi
  verify_release_read_only "$RELEASE_DIR"
  verify_release_source "$RELEASE_DIR"
else
  BUILD_DIR="$(mktemp -d "$DEPLOY_ROOT/releases/.build-$SHA.XXXXXX")"
  git -C "$ROOT_DIR" archive --format=tar "$SHA" | tar -xf - -C "$BUILD_DIR"
  UV_PROJECT_ENVIRONMENT="$BUILD_DIR/.venv" uv sync \
    --project "$BUILD_DIR" \
    --python "$ROOT_DIR/.venv/bin/python" \
    --frozen \
    --no-dev \
    --extra dev \
    --no-install-project
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$BUILD_DIR/src" "$BUILD_DIR/.venv/bin/python" -c \
    'import bot, httpx, pydantic, structlog, typer, websockets'
  (
    cd "$BUILD_DIR"
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$BUILD_DIR/src" \
      "$BUILD_DIR/.venv/bin/python" -m pytest -q -p no:cacheprovider
  )
  SOURCE_ARCHIVE_SHA256="$(git -C "$ROOT_DIR" archive --format=tar "$SHA" | sha256sum | awk '{print $1}')"
  UV_LOCK_SHA256="$(sha256sum "$BUILD_DIR/uv.lock" | awk '{print $1}')"
  printf '{"commit":"%s","source_archive_sha256":"%s","uv_lock_sha256":"%s"}\n' \
    "$SHA" "$SOURCE_ARCHIVE_SHA256" "$UV_LOCK_SHA256" > "$BUILD_DIR/.release.json"
  chmod -R a-w "$BUILD_DIR"
  mv "$BUILD_DIR" "$RELEASE_DIR"
  BUILD_DIR=""
  verify_release_read_only "$RELEASE_DIR"
  verify_release_source "$RELEASE_DIR"
fi

RUNTIME_ENV_TMP="$DEPLOY_ROOT/release-env/.${SHA}.env.$$"
printf '%s\n' \
  "DEPLOY_COMMIT=$SHA" \
  "SQLITE_PATH=$ROOT_DIR/bot.sqlite3" \
  "PROBABILITY_MODEL_PATH=$ROOT_DIR/probability_model.json" \
  "KILL_SWITCH_FILE=$ROOT_DIR/KILL_SWITCH" > "$RUNTIME_ENV_TMP"
chmod 0644 "$RUNTIME_ENV_TMP"
mv -f "$RUNTIME_ENV_TMP" "$RELEASE_ENV"

if [ "$BUILD_ONLY" = "true" ]; then
  echo "release_build_ok=true commit=$SHA release=$RELEASE_DIR"
  exit 0
fi

if [ "$INSTALL_RELEASE_UNITS" = "true" ]; then
  /bin/bash "$ROOT_DIR/scripts/install_release_units.sh"
fi

PREVIOUS_RELEASE=""
PREVIOUS_ENV=""
EXPERIMENT_ACTIVATED="false"
if [ -L "$CURRENT_LINK" ]; then
  PREVIOUS_RELEASE="$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)"
fi
if [ -L "$CURRENT_ENV_LINK" ]; then
  PREVIOUS_ENV="$(readlink -f "$CURRENT_ENV_LINK" 2>/dev/null || true)"
fi
if [ -n "$PREVIOUS_RELEASE" ] && [ -z "$PREVIOUS_ENV" ]; then
  PREVIOUS_SHA="$(basename "$PREVIOUS_RELEASE")"
  PREVIOUS_ENV="$DEPLOY_ROOT/release-env/$PREVIOUS_SHA.env"
  printf '%s\n' \
    "DEPLOY_COMMIT=$PREVIOUS_SHA" \
    "SQLITE_PATH=$ROOT_DIR/bot.sqlite3" \
    "PROBABILITY_MODEL_PATH=$ROOT_DIR/probability_model.json" \
    "KILL_SWITCH_FILE=$ROOT_DIR/KILL_SWITCH" > "$PREVIOUS_ENV"
  chmod 0644 "$PREVIOUS_ENV"
fi

rollback() {
  if [ "$EXPERIMENT_ACTIVATED" = "true" ]; then
    PYTHONPATH="$RELEASE_DIR/src" "$RELEASE_DIR/.venv/bin/python" -m bot.cli \
      policy-experiment-rollback --version "$MAKER_EXPERIMENT_VERSION" || true
  fi
  if [ -n "$PREVIOUS_RELEASE" ] && [ -d "$PREVIOUS_RELEASE" ]; then
    ln -sfn "$PREVIOUS_RELEASE" "$NEXT_LINK"
    mv -Tf "$NEXT_LINK" "$CURRENT_LINK"
    if [ -n "$PREVIOUS_ENV" ] && [ -f "$PREVIOUS_ENV" ]; then
      ln -sfn "$PREVIOUS_ENV" "$NEXT_ENV_LINK"
      mv -Tf "$NEXT_ENV_LINK" "$CURRENT_ENV_LINK"
    fi
    sudo systemctl restart tradingbot-paper.service tradingbot-frontend.service
    echo "deployment_rolled_back_to=$PREVIOUS_RELEASE"
  fi
}
trap rollback ERR

ln -sfn "$RELEASE_DIR" "$NEXT_LINK"
mv -Tf "$NEXT_LINK" "$CURRENT_LINK"
ln -sfn "$RELEASE_ENV" "$NEXT_ENV_LINK"
mv -Tf "$NEXT_ENV_LINK" "$CURRENT_ENV_LINK"

if [ "$ACTIVATE_MAKER_EXPERIMENT" = "true" ]; then
  case "$MAKER_EXPERIMENT_VERSION" in
    btc-updown-v4-maker-experiment)
      MAKER_ACTIVATION_SCRIPT="$RELEASE_DIR/scripts/activate_maker_experiment.py"
      ;;
    btc-updown-v5-margin-maker)
      MAKER_ACTIVATION_SCRIPT="$RELEASE_DIR/scripts/activate_margin_maker_experiment.py"
      ;;
    *)
      echo "ERROR unsupported_maker_experiment=$MAKER_EXPERIMENT_VERSION"
      false
      ;;
  esac
  PYTHONPATH="$RELEASE_DIR/src" "$RELEASE_DIR/.venv/bin/python" \
    "$MAKER_ACTIVATION_SCRIPT"
  EXPERIMENT_ACTIVATED="true"
fi

PAPER_RESTART_EPOCH="$(date +%s)"
sudo systemctl restart tradingbot-paper.service tradingbot-frontend.service

for _ in $(seq 1 60); do
  HEALTH="$(curl -fsS --max-time 3 http://127.0.0.1:8888/api/healthz 2>/dev/null || true)"
  if systemctl is-active --quiet tradingbot-paper.service tradingbot-frontend.service && \
    HEALTH="$HEALTH" EXPECTED_SHA="$SHA" PAPER_RESTART_EPOCH="$PAPER_RESTART_EPOCH" \
    "$RELEASE_DIR/.venv/bin/python" -c \
    'import json,os; from datetime import datetime; p=json.loads(os.environ["HEALTH"]); updated=p.get("paper_loop_updated_at"); epoch=datetime.fromisoformat(updated.replace("Z", "+00:00")).timestamp() if updated else 0; ok=p.get("ok") and p.get("deploy_commit")==os.environ["EXPECTED_SHA"] and epoch >= int(os.environ["PAPER_RESTART_EPOCH"]); raise SystemExit(0 if ok else 1)' \
    2>/dev/null; then
    trap - ERR
    echo "deployment_ok=true commit=$SHA release=$RELEASE_DIR"
    exit 0
  fi
  sleep 1
done

echo "ERROR deployment_health_timeout=true commit=$SHA"
false
