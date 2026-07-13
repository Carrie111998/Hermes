#!/usr/bin/env bash
# bootstrap_local.sh — build the hermes-mtu Studio-local pilot HERMES_HOME from this deploy dir.
#
# Usage:
#   bootstrap_local.sh <telegram_bot_token_file> <allowed_user_ids_csv>
#
# - Copies config.yaml / mtu_constitution.yaml / SOUL.md into $HERMES_HOME (~/.hermes-mtu).
# - Writes $HERMES_HOME/.env (chmod 600) with the Telegram bot token, the allowlist, and the
#   OpenAI key sourced from ~/.marshal/secrets.env. NO secret value is hardcoded here.
# - Idempotent: safe to re-run (overwrites config/SOUL/.env from source-of-truth).
#
# The runtime executes with:  HERMES_HOME=~/.hermes-mtu <hermes-venv>/python <hermes>/hermes gateway run
set -euo pipefail

TOKEN_FILE="${1:-$HOME/.hermes-mtu-token.tmp}"
ALLOWED_USERS="${2:-}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes-mtu}"
DEP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS="$HOME/.marshal/secrets.env"

[ -f "$TOKEN_FILE" ] || { echo "ERROR: telegram token file not found: $TOKEN_FILE" >&2; exit 1; }
[ -f "$SECRETS" ]    || { echo "ERROR: secrets file not found: $SECRETS" >&2; exit 1; }
OPENAI_KEY="$(grep -E '^(export )?OPENAI_API_KEY=' "$SECRETS" | head -1 | sed -E 's/^(export )?OPENAI_API_KEY=//; s/^"//; s/"$//')"
[ -n "$OPENAI_KEY" ] || { echo "ERROR: OPENAI_API_KEY not in $SECRETS" >&2; exit 1; }
TG_TOKEN="$(tr -d ' \n' < "$TOKEN_FILE")"

mkdir -p "$HERMES_HOME"
cp "$DEP/config.yaml"            "$HERMES_HOME/config.yaml"
cp "$DEP/mtu_constitution.yaml" "$HERMES_HOME/mtu_constitution.yaml"
cp "$DEP/SOUL.md"               "$HERMES_HOME/SOUL.md"

umask 077
cat > "$HERMES_HOME/.env" <<ENV
# hermes-mtu pilot secrets — NOT committed. Regenerate via bootstrap_local.sh.
TELEGRAM_BOT_TOKEN=$TG_TOKEN
TELEGRAM_ALLOWED_USERS=$ALLOWED_USERS
OPENAI_API_KEY=$OPENAI_KEY
ENV
chmod 600 "$HERMES_HOME/.env"

echo "OK: HERMES_HOME=$HERMES_HOME bootstrapped."
echo "  config.yaml, mtu_constitution.yaml, SOUL.md installed."
echo "  .env written (chmod 600): TELEGRAM_BOT_TOKEN=${TG_TOKEN:0:9}..., ALLOWED_USERS=$ALLOWED_USERS, OPENAI_API_KEY set."
