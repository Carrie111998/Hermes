#!/usr/bin/env bash
# presence_notify.sh — Budgeted ambient alerts on Android Termux (max N/day).
# Usage: presence_notify.sh "message"
set -u

HOME_DIR="${HOME:-.}"
BUDGET_FILE="$HOME_DIR/.hermes/presence_budget.json"
MAX_PER_DAY=3
MSG="${1:-Hermes notification}"

today=$(date +%Y-%m-%d)
count=$(python3 -c "
import json, os
p = '$BUDGET_FILE'
try:
    d = json.load(open(p))
    print(d.get('count', 0) if d.get('day') == '$today' else 0)
except Exception:
    print(0)
" 2>/dev/null || echo 0)

if [ "$count" -ge "$MAX_PER_DAY" ]; then
    mkdir -p "$HOME_DIR/.hermes/logs"
    echo "$(date '+%F %T') presence: BUDGET EXHAUSTED ($count/$MAX_PER_DAY) — skipped: $MSG" >> "$HOME_DIR/.hermes/logs/presence.log"
    exit 0
fi

# Locate termux_presence.py in ~/.hermes/scripts or skill directory
SCRIPT_PATH="$HOME_DIR/.hermes/scripts/termux_presence.py"
if [ ! -f "$SCRIPT_PATH" ]; then
    SCRIPT_PATH="$(dirname "$0")/termux_presence.py"
fi

if python3 "$SCRIPT_PATH" "$MSG"; then
    python3 -c "
import json, os
p = '$BUDGET_FILE'
os.makedirs(os.path.dirname(p), exist_ok=True)
json.dump({'day': '$today', 'count': $count + 1}, open(p, 'w'))
" 2>/dev/null || true
    echo "$(date '+%F %T') presence: sent ($((count+1))/$MAX_PER_DAY): $MSG" >> "$HOME_DIR/.hermes/logs/presence.log"
fi
