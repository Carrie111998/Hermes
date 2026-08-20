#!/usr/bin/env bash
set -uo pipefail
# Deliberately no `set -e` / ERR trap here: this process runs fully
# detached from sync-fork.sh (its parent already exited and delivered its
# notification by the time this runs). Its own failures must be captured
# into the marker file so hermes-sync-fork-restart-check can alert on
# them — an uncaught crash into /tmp/hermes-sync-fork-restart-async.log
# that nobody reads promptly would just reintroduce the same kind of
# silent gap this whole change exists to close.
#
# Invoked by sync-fork.sh as:
#   sync-fork-restart-async.sh <scheduled_at> <before_head> <after_head> <commit_count> <deps_changed>
# fully detached (setsid, fds redirected, disowned) so it outlives the
# parent script's process.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./sync-fork-common.sh
source "$SCRIPT_DIR/sync-fork-common.sh"

scheduled_at="$1"
before_head="$2"
after_head="$3"
commit_count="$4"
deps_changed="$5"

REPO_DIR="/home/jiddy/.hermes/hermes-agent"
HERMES_HOME="/home/jiddy/.hermes"
UV_BIN="/home/jiddy/.local/bin/uv"
GATEWAY_UNIT="hermes-gateway.service"
MARKER="$HERMES_HOME/cron/sync_fork_restart_state.json"

mark_failed() {
  write_marker "failed" "$scheduled_at" "$before_head" "$after_head" "$commit_count" "$1"
  exit 1
}

# Give sync-fork.sh's own exit and the scheduler's stdout capture +
# Telegram delivery a few seconds' head start before anything here
# touches the gateway. The delivery itself doesn't depend on this sleep
# (it only depends on the parent process having exited, which already
# happened), but restarting the gateway concurrently with the scheduler
# still finishing up work from the parent's exit is avoidable risk for
# free.
sleep 5

if [[ "$deps_changed" == "true" ]]; then
  cd "$REPO_DIR" 2>/dev/null || mark_failed "cannot cd into $REPO_DIR for dependency reinstall"
  if ! "$UV_BIN" pip install -e ".[all]" --python "$REPO_DIR/venv/bin/python" >/tmp/hermes-sync-fork-deps-reinstall.log 2>&1; then
    mark_failed "dependency reinstall failed after pulling $commit_count commit(s) (before=$before_head after=$after_head) — see /tmp/hermes-sync-fork-deps-reinstall.log. Live checkout is on the new commits but the running gateway is still on old code; restart was not attempted, needs manual intervention"
  fi
fi

# Restart so the running gateway actually loads the new code — anything
# imported into its process (as opposed to skill scripts, which are
# subprocess-invoked fresh every call and don't need this) stays stale in
# memory until this happens.
#
# Going through systemctl directly (rather than `hermes gateway restart`,
# which refuses to run under the gateway's own _HERMES_GATEWAY=1
# environment by design — see sync-fork.sh's original comment on this) is
# genuinely external to the gateway process, so it doesn't need to touch
# that self-restart-loop guard.
if ! systemctl --user restart "$GATEWAY_UNIT" >/tmp/hermes-sync-fork-restart-systemctl.log 2>&1; then
  mark_failed "gateway restart command failed after pulling $commit_count commit(s) — see /tmp/hermes-sync-fork-restart-systemctl.log. Live checkout is updated on disk but the running process may still be on old code."
fi

# Give the new process a moment to actually come up before checking.
sleep 5

status_output="$(systemctl --user status "$GATEWAY_UNIT" 2>&1)" || mark_failed "gateway status command itself failed after restart — cannot confirm health. Raw output: $status_output"

grep -q "active (running)" <<< "$status_output" \
  || mark_failed "gateway did not report 'active (running)' after restart. Status output: $status_output"

# Check the NEW process's own log for anything that looks like a startup
# failure the status check alone wouldn't catch. Scoped to this exact
# restart's systemd invocation ID, not a plain time window — a
# time-window scan also catches the dying OLD process's error tail during
# the handoff (confirmed live 2026-08-15: an in-flight conversation on the
# old process threw a burst of connection/model errors in the same second
# the new process started, and a naive `--since "-2 minutes"` scan
# misattributed all of it to the new process, failing a genuinely healthy
# restart). The invocation ID is unique per service start, so this only
# ever sees output from the process that's actually running now.
invocation_id="$(systemctl --user show "$GATEWAY_UNIT" --property=InvocationID --value)"
[[ -n "$invocation_id" ]] || mark_failed "could not read the new gateway process's systemd invocation ID — cannot scope the post-restart log check"

recent_errors="$(journalctl --user -u "$GATEWAY_UNIT" "_SYSTEMD_INVOCATION_ID=$invocation_id" --no-pager 2>/dev/null | grep -iE 'traceback|error' || true)"
if [[ -n "$recent_errors" ]]; then
  mark_failed "gateway restarted and reports healthy, but errors appeared in its own log: $recent_errors"
fi

write_marker "healthy" "$scheduled_at" "$before_head" "$after_head" "$commit_count"
exit 0
