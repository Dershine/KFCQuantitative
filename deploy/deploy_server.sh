#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash /opt/kfcquant/current/deploy/deploy_server.sh [40-character SHA] [--approve-irreversible-migration]" >&2
  exit 1
fi

REPOSITORY_DIR=/opt/kfcquant/repository
CURRENT_RELEASE=/opt/kfcquant/current
TARGET_SHA=""
APPROVE_IRREVERSIBLE=false

for argument in "$@"; do
  case "$argument" in
    --approve-irreversible-migration) APPROVE_IRREVERSIBLE=true ;;
    *)
      if [[ -n "$TARGET_SHA" ]]; then
        echo "usage: sudo bash $CURRENT_RELEASE/deploy/deploy_server.sh [40-character SHA] [--approve-irreversible-migration]" >&2
        exit 64
      fi
      TARGET_SHA="$argument"
      ;;
  esac
done

if [[ -z "$TARGET_SHA" ]]; then
  runuser -u kfcops -- git -C "$REPOSITORY_DIR" fetch --prune origin main
  TARGET_SHA="$(runuser -u kfcops -- git -C "$REPOSITORY_DIR" rev-parse origin/main)"
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
if [[ -f "$CURRENT_RELEASE/.release.env" ]]; then
  # shellcheck disable=SC1091
  source "$CURRENT_RELEASE/.release.env"
fi
set +a
cd "$CURRENT_RELEASE"
export HOME=/var/lib/kfcops

if [[ "$APPROVE_IRREVERSIBLE" == true ]]; then
  exec runuser -u kfcops --preserve-environment -- "$CURRENT_RELEASE/.venv/bin/kfcops" deploy \
    "$TARGET_SHA" --approve-irreversible-migration
fi
exec runuser -u kfcops --preserve-environment -- "$CURRENT_RELEASE/.venv/bin/kfcops" deploy "$TARGET_SHA"
