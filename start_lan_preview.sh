#!/bin/zsh
set -euo pipefail

# LAN preview deliberately keeps APP_ENV=production so non-cookie behavior stays
# production-like. LAN_PREVIEW_HTTP is the narrow opt-in that permits the session
# cookie to work on an explicit plain-HTTP preview address.
: "${SESSION_SECRET:?SESSION_SECRET must come from the process environment}"
: "${FAMILY_AUTH_HTPASSWD_PATH:?FAMILY_AUTH_HTPASSWD_PATH is required}"
: "${FAMILY_MENU_DB_PATH:?FAMILY_MENU_DB_PATH is required}"
: "${H5_BASE_URL:?H5_BASE_URL is required when APP_ENV=production}"
: "${OWNER_AUTH_USERNAME:?OWNER_AUTH_USERNAME is required}"

if [[ ! -r "$FAMILY_AUTH_HTPASSWD_PATH" ]]; then
  print -u2 "FAMILY_AUTH_HTPASSWD_PATH is not readable"
  exit 1
fi
if [[ ! -f "$FAMILY_MENU_DB_PATH" ]]; then
  print -u2 "FAMILY_MENU_DB_PATH does not exist"
  exit 1
fi

export APP_ENV=production
export LOCAL_PREVIEW_UI=true
export LAN_PREVIEW_HTTP=true
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-18765}"
export PUSH_ENABLED=false
export PUSH_ON_CONFIRM=false
export PUSH_SCHEDULE_ENABLED=false

script_dir="${0:A:h}"
cd "$script_dir"
exec "${PYTHON_BIN:-python3}" app.py
