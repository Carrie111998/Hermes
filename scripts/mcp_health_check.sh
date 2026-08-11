#!/usr/bin/env bash
# T0138: detect a silently-degraded MCP server connection (e.g. klib) and
# alert once the failure has persisted across consecutive checks.
#
# Why log-based, not an in-process query: tools.mcp_tool.get_mcp_status()
# reads module-level state (_servers) that only exists inside the actual
# running gateway process. A fresh `python3` subprocess spawned by this
# script has an empty _servers dict regardless of the real gateway's
# connection state -- there is no existing IPC/HTTP/CLI surface to query
# the live process out-of-band (verified: `hermes gateway status --deep`
# only tails journalctl, no admin socket exists). So this watches for the
# same log signature that revealed the 2026-08-07 23:51:03 incident
# instead: a "keepalive failed ... degraded" line with no subsequent
# successful registration/reconnect line after it.
set -euo pipefail

export TZ=Asia/Taipei
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
ENV_FILE="$HERMES_HOME/.env"
STATE_DIR="$HERMES_HOME"
LAST_CHECK_FILE="$STATE_DIR/mcp_health_last_check"
STATE_FILE="$STATE_DIR/mcp_health_state"          # "healthy" or "degraded"
FAIL_COUNT_FILE="$STATE_DIR/mcp_health_fail_count"
ALERT_SENT_FILE="$STATE_DIR/mcp_health_alert_sent"

FAIL_THRESHOLD=2
SERVICE_NAME="hermes-gateway.service"
BOOTSTRAP_LOOKBACK_MIN=15

mkdir -p "$STATE_DIR"

# --- prevent overlapping runs: if a previous invocation is still inside
# journalctl/curl when the next 5-minute timer fires, skip rather than
# race on the state files below (flock released automatically on exit). ---
exec 200>"$STATE_DIR/mcp_health.lock"
if ! flock -n 200; then
  echo "mcp_health_check: another instance is already running, skipping" >&2
  exit 0
fi

# --- read persisted state (defaults on first run) ---
prev_state="healthy"
[[ -f "$STATE_FILE" ]] && prev_state=$(cat "$STATE_FILE")
fail_count=0
[[ -f "$FAIL_COUNT_FILE" ]] && fail_count=$(cat "$FAIL_COUNT_FILE")
alert_sent=0
[[ -f "$ALERT_SENT_FILE" ]] && alert_sent=$(cat "$ALERT_SENT_FILE")

if [[ -f "$LAST_CHECK_FILE" ]]; then
  since_arg="--since=@$(cat "$LAST_CHECK_FILE")"
else
  since_arg="--since=-${BOOTSTRAP_LOOKBACK_MIN} minutes"
fi

now_epoch=$(date +%s)

# --- scan only NEW log lines since the last check (persisted cursor, not
# a fixed lookback window -- a fixed window would miss a failure that
# started before the window and never recovered, which is exactly what
# happened for 11+ hours in the real incident). ---
new_logs=$(journalctl --user -u "$SERVICE_NAME" --no-pager "$since_arg" 2>/dev/null \
  | grep -iE "mcp server 'klib'.*(keepalive failed|registered [0-9]+ tool)" || true)

echo "$now_epoch" > "$LAST_CHECK_FILE"

current_state="$prev_state"
if [[ -n "$new_logs" ]]; then
  # The last matching line in this window wins: a later success line
  # after a degraded line means it already recovered.
  last_line=$(echo "$new_logs" | tail -1)
  if [[ "$last_line" == *"registered"*"tool"* ]]; then
    current_state="healthy"
  elif [[ "$last_line" == *"keepalive failed"* ]]; then
    current_state="degraded"
  fi
fi
# If no new lines at all, current_state stays as prev_state -- an
# already-degraded connection that produces no further log output is
# still degraded, it does not silently heal by going quiet.

echo "$current_state" > "$STATE_FILE"

if [[ "$current_state" == "degraded" ]]; then
  fail_count=$((fail_count + 1))
else
  fail_count=0
  alert_sent=0
fi
echo "$fail_count" > "$FAIL_COUNT_FILE"
echo "$alert_sent" > "$ALERT_SENT_FILE"

if [[ "$fail_count" -ge "$FAIL_THRESHOLD" && "$alert_sent" -eq 0 ]]; then
  echo "⚠️ MCP health guard: '$SERVICE_NAME' MCP server 'klib' has been degraded for $fail_count consecutive check(s)." >&2

  if [[ -f "$ENV_FILE" ]]; then
    bot_token=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | tail -1 | cut -d= -f2-)
    home_channel=$(grep -E '^TELEGRAM_HOME_CHANNEL=' "$ENV_FILE" | tail -1 | cut -d= -f2-)
  fi

  if [[ -n "${bot_token:-}" && -n "${home_channel:-}" ]]; then
    alert_text="MCP health guard: klib server has been degraded for ${fail_count} consecutive checks (last transition: $(date '+%Y-%m-%d %H:%M:%S %Z')). Run: systemctl --user restart ${SERVICE_NAME}"
    # Direct Telegram Bot API call -- deliberately not the in-process
    # adapter.send() (an async instance method on a live bot session this
    # standalone script has no access to). Best-effort: alert failures
    # are logged but must not make the health check itself exit non-zero
    # (a delivery hiccup is not the same signal as an MCP failure).
    if ! curl -sf -m 10 -X POST \
      "https://api.telegram.org/bot${bot_token}/sendMessage" \
      -d "chat_id=${home_channel}" \
      --data-urlencode "text=${alert_text}" \
      >/dev/null 2>&1; then
      echo "⚠️ MCP health guard: Telegram alert delivery failed" >&2
    else
      echo 1 > "$ALERT_SENT_FILE"
    fi
  else
    echo "⚠️ MCP health guard: TELEGRAM_BOT_TOKEN/TELEGRAM_HOME_CHANNEL not found in $ENV_FILE, cannot alert" >&2
  fi
fi

exit 0
