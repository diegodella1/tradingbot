#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
LOG_DIR="${LEARNING_LOG_DIR:-$ROOT_DIR/logs/learning}"
MODEL_ACTIVE="${MODEL_ACTIVE:-$ROOT_DIR/probability_model.json}"
MODEL_CANDIDATE="${MODEL_CANDIDATE:-$ROOT_DIR/probability_model.candidate.json}"
MODEL_REJECTED="${MODEL_REJECTED:-$ROOT_DIR/probability_model.rejected.json}"
PROMOTE_MODEL="${PROMOTE_MODEL:-false}"
RESTART_PAPER_ON_PROMOTE="${RESTART_PAPER_ON_PROMOTE:-true}"

mkdir -p "$LOG_DIR"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_FILE="$LOG_DIR/learning-cycle-$RUN_ID.log"

exec > >(tee -a "$LOG_FILE") 2>&1

echo "learning_cycle_start=$RUN_ID"
echo "root=$ROOT_DIR"
echo "python=$PYTHON_BIN"
echo "promote_model=$PROMOTE_MODEL"
echo "restart_paper_on_promote=$RESTART_PAPER_ON_PROMOTE"

if [ ! -x "$PYTHON_BIN" ]; then
  echo "ERROR python_not_found=$PYTHON_BIN"
  exit 1
fi

echo
echo "== backtest buckets =="
"$PYTHON_BIN" -m bot.cli backtest --buckets

echo
echo "== learning report =="
"$PYTHON_BIN" -m bot.cli learning-report

echo
echo "== calibrate candidate =="
rm -f "$MODEL_CANDIDATE"
CALIBRATE_OUTPUT="$("$PYTHON_BIN" -m bot.cli calibrate --output "$MODEL_CANDIDATE" 2>&1)"
printf '%s\n' "$CALIBRATE_OUTPUT"

if printf '%s\n' "$CALIBRATE_OUTPUT" | grep -qi "NO supera"; then
  if [ -f "$MODEL_CANDIDATE" ]; then
    mv "$MODEL_CANDIDATE" "$MODEL_REJECTED"
  fi
  echo "model_decision=rejected reason=does_not_beat_market_baseline"
  echo "active_model_unchanged=true"
  exit 0
fi

if [ ! -f "$MODEL_CANDIDATE" ]; then
  echo "model_decision=rejected reason=candidate_missing"
  exit 1
fi

PYTHONPATH="$ROOT_DIR/src" "$PYTHON_BIN" -c \
  'import sys; from bot.strategy.calibration import ProbabilityModel; model=ProbabilityModel.load(sys.argv[1]); raise SystemExit(0 if model and model.is_trade_approved() else 1)' \
  "$MODEL_CANDIDATE" || {
    mv "$MODEL_CANDIDATE" "$MODEL_REJECTED"
    echo "model_decision=rejected reason=model_contract_not_approved"
    exit 0
  }

if [ "$PROMOTE_MODEL" = "true" ]; then
  CANDIDATE_SHA256="$(sha256sum "$MODEL_CANDIDATE" | awk '{print $1}')"
  PROBABILITY_MODEL_PATH="$MODEL_ACTIVE" "$PYTHON_BIN" -m bot.cli \
    model-promote --candidate-model "$MODEL_CANDIDATE"
  echo "model_decision=promoted active_model=$MODEL_ACTIVE sha256=$CANDIDATE_SHA256"
  if [ "$RESTART_PAPER_ON_PROMOTE" = "true" ]; then
    /bin/bash "$ROOT_DIR/scripts/restart_paper.sh"
    echo "restart_required=false"
  else
    echo "restart_required=true"
  fi
else
  echo "model_decision=candidate_ready candidate=$MODEL_CANDIDATE"
  echo "active_model_unchanged=true"
fi

echo "learning_cycle_done=$(date -u +%Y%m%dT%H%M%SZ)"
