#!/usr/bin/env bash
# hermes-agent-autodeploy.sh — pull the tracked branch and restart the gateway
# only when the checkout actually moved. Idempotent and quiet: if the local
# checkout is already at the remote tip, it exits 0 without touching the
# running gateway or emitting an alert.
#
# Why a systemd/launchd path and NOT a cronjob: the gateway's cron
# lifecycle_guard deliberately blocks jobs that start/stop/restart the gateway
# they run under, so a "pull + restart" step cannot live in `hermes cron`.
# This script is driven by hermes-agent-autodeploy.timer instead.
#
# Everything is configured through environment variables (see the .service
# unit's EnvironmentFile), so the same script serves a system-level deploy
# (User=hermes, /home/hermes) and a rootless user-level deploy.
#
#   HERMES_REPO_DIR       Path to the hermes-agent git checkout.   (required)
#   HERMES_BRANCH         Branch to track.                         (default: main)
#   HERMES_RESTART_UNIT   systemd unit to restart on change.       (default: hermes-gateway.service)
#   HERMES_SYSTEMCTL_ARGS Extra systemctl args, e.g. "--user".     (default: empty = system scope)
#   HERMES_VENV_SYNC      Command to run when deps changed,        (default: empty = skip)
#                         e.g. "uv sync --frozen" or
#                         ".venv/bin/pip install -e .". Run with CWD = repo.
#   SLACK_WEBHOOK_URL     Incoming-webhook URL for alerts.         (optional)
#   HERMES_ALERT_LABEL    Host/name shown in alerts.               (default: hostname)
#
# Exit codes: 0 = up to date or deployed cleanly; 1 = a problem an operator
# should see (dirty tree, non-fast-forward divergence, restart failure).

set -euo pipefail

REPO_DIR="${HERMES_REPO_DIR:?set HERMES_REPO_DIR to the hermes-agent checkout}"
BRANCH="${HERMES_BRANCH:-main}"
RESTART_UNIT="${HERMES_RESTART_UNIT:-hermes-gateway.service}"
SYSTEMCTL_ARGS="${HERMES_SYSTEMCTL_ARGS:-}"
VENV_SYNC="${HERMES_VENV_SYNC:-}"
ALERT_LABEL="${HERMES_ALERT_LABEL:-$(hostname)}"

log() { printf '%s hermes-autodeploy: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# Post one line to Slack when a webhook is configured; always mirror to the
# journal. Never let an alert failure abort the deploy result.
alert() {
  local msg="$1"
  log "$msg"
  if [ -n "${SLACK_WEBHOOK_URL:-}" ]; then
    curl -fsS --max-time 10 -X POST -H 'Content-Type: application/json' \
      --data "$(printf '{"text":%s}' "$(json_escape "[$ALERT_LABEL] $msg")")" \
      "$SLACK_WEBHOOK_URL" >/dev/null 2>&1 || log "slack alert failed (non-fatal)"
  fi
}

json_escape() { printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'; }

fail() { alert "❌ autodeploy failed: $*"; exit 1; }

cd "$REPO_DIR" || fail "repo dir $REPO_DIR not found"

# Serialize against overlapping timer fires (a slow fetch must not race a
# second run). Non-blocking: if another run holds the lock, exit quietly.
exec 9>"${TMPDIR:-/tmp}/hermes-agent-autodeploy.lock"
if ! flock -n 9; then
  log "another autodeploy run holds the lock; skipping this tick"
  exit 0
fi

# Refuse to touch a tree with modified TRACKED files — a local edit means
# someone is mid-change, and a fast-forward could silently discard or conflict
# with it. Untracked files are deliberately ignored: a live gateway writes
# runtime state inside the checkout (e.g. cron/executions.db), which would
# otherwise trip this guard on every host, forever. Untracked files never
# block a fast-forward unless an incoming commit would overwrite one, and git
# errors on that case on its own.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  fail "tracked files at $REPO_DIR are modified; refusing to auto-pull (commit/stash by hand)"
fi

git fetch --quiet origin "$BRANCH" || fail "git fetch origin $BRANCH failed"

LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/$BRANCH")"

# Already current: the common case. Exit silently — no restart, no alert.
if [ "$LOCAL" = "$REMOTE" ]; then
  log "already at ${REMOTE:0:9}; nothing to do"
  exit 0
fi

# Only fast-forward. A diverged local history (someone committed locally, or a
# force-push upstream) is an operator decision, never an automatic one.
if ! git merge-base --is-ancestor "$LOCAL" "$REMOTE"; then
  fail "local ${LOCAL:0:9} is not an ancestor of origin/$BRANCH ${REMOTE:0:9} (diverged/force-push); manual intervention needed"
fi

# Detect whether Python dependencies changed across the fast-forward, so the
# venv sync only runs when it must.
DEPS_CHANGED=""
if ! git diff --quiet "$LOCAL" "$REMOTE" -- uv.lock pyproject.toml requirements.txt 2>/dev/null; then
  DEPS_CHANGED=1
fi

git merge --ff-only "origin/$BRANCH" --quiet || fail "fast-forward merge to ${REMOTE:0:9} failed"

if [ -n "$DEPS_CHANGED" ] && [ -n "$VENV_SYNC" ]; then
  log "dependencies changed; running venv sync: $VENV_SYNC"
  # shellcheck disable=SC2086
  ( eval $VENV_SYNC ) || fail "venv sync failed after ff to ${REMOTE:0:9} (gateway NOT restarted)"
fi

# shellcheck disable=SC2086
if ! systemctl $SYSTEMCTL_ARGS restart "$RESTART_UNIT"; then
  fail "restart of $RESTART_UNIT failed after ff to ${REMOTE:0:9}"
fi

alert "✅ autodeploy: ${LOCAL:0:9} → ${REMOTE:0:9} on $BRANCH, restarted $RESTART_UNIT${DEPS_CHANGED:+ (deps synced)}"
