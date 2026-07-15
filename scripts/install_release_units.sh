#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

sudo install -d -m 0755 /etc/systemd/system/tradingbot-paper.service.d
sudo install -d -m 0755 /etc/systemd/system/tradingbot-frontend.service.d
sudo install -m 0644 \
  "$ROOT_DIR/deploy/systemd/tradingbot-paper-release.conf" \
  /etc/systemd/system/tradingbot-paper.service.d/release.conf
sudo install -m 0644 \
  "$ROOT_DIR/deploy/systemd/tradingbot-frontend-release.conf" \
  /etc/systemd/system/tradingbot-frontend.service.d/release.conf
sudo systemctl daemon-reload

echo "release_unit_dropins_installed=true"
