#!/usr/bin/env bash
set -euo pipefail

UNIT="${PAPER_SYSTEMD_UNIT:-tradingbot-paper.service}"
SYSTEMCTL=(sudo "${SYSTEMCTL_BIN:-systemctl}")

old_pid="$("${SYSTEMCTL[@]}" show "$UNIT" --property MainPID --value)"
"${SYSTEMCTL[@]}" restart "$UNIT"
"${SYSTEMCTL[@]}" is-active --quiet "$UNIT"
new_pid="$("${SYSTEMCTL[@]}" show "$UNIT" --property MainPID --value)"

if [ -z "$new_pid" ] || [ "$new_pid" = "0" ]; then
  echo "ERROR paper_restart_missing_pid=true unit=$UNIT"
  exit 1
fi

if [ "$new_pid" = "$old_pid" ]; then
  echo "ERROR paper_restart_pid_unchanged=true pid=$new_pid unit=$UNIT"
  exit 1
fi

echo "paper_restarted_unit=$UNIT old_pid=$old_pid new_pid=$new_pid"
