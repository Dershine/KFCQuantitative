#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash /opt/kfcquant/app/deploy/deploy_server.sh [40-character SHA]" >&2
  exit 1
fi

APP_DIR=/opt/kfcquant/app
TARGET_SHA="${1:-}"

if [[ $# -gt 1 ]]; then
  echo "usage: sudo bash $APP_DIR/deploy/deploy_server.sh [40-character SHA]" >&2
  exit 64
fi

if [[ -z "$TARGET_SHA" ]]; then
  runuser -u kfcops -- git -C "$APP_DIR" fetch --prune origin main
  TARGET_SHA="$(runuser -u kfcops -- git -C "$APP_DIR" rev-parse origin/main)"
fi

if [[ ! "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Target must be a 40-character lowercase Git SHA" >&2
  exit 64
fi

set -a
# shellcheck disable=SC1091
source /etc/kfcquant/research.env
# shellcheck disable=SC1091
source /etc/kfcquant/ops.env
if [[ -f "$APP_DIR/.release.env" ]]; then
  # shellcheck disable=SC1091
  source "$APP_DIR/.release.env"
fi
set +a

exec runuser -u kfcops --preserve-environment -- "$APP_DIR/.venv/bin/kfcops" deploy "$TARGET_SHA"
