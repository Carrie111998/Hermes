#!/usr/bin/env bash
set -euo pipefail

# Nightly sync: bring the live checkout (~/.hermes/hermes-agent, what
# hermes-gateway.service actually runs from) up to origin/main once JID
# has reviewed and merged a PR — never touches upstream (NousResearch),
# that's hermes-upstream-main-check's job entirely, this is downstream of
# it. No-agent, mechanical, no judgment calls: by the time a commit is on
# origin/main, JID already approved it via GitHub's own merge button.
#
# Silent when there's nothing to do (matches the no_agent cron convention
# in this system: empty stdout = no notification). NEVER silent on an
# actual sync or on any failure — a code change reaching the live running
# system, or a step of this job failing, are both things JID wants to
# know about, not things that should look identical to "nothing happened."
#
# Restart is ASYNC (added after a real notification gap was diagnosed):
# `systemctl --user restart` kills every process descended from this
# script's own process group when it tears down the gateway unit's
# cgroup, including this script's own shell if the restart happened
# synchronously inline. The cron scheduler's no-agent delivery path only
# builds/sends the Telegram message AFTER this script's process fully
# exits (see cron/scheduler.py's no_agent job handling) — so a synchronous
# restart killed the very process that was about to deliver the "I just
# synced N commits" notification, and it silently never went out. Every
# night there was something to sync, the one job whose entire purpose is
# telling JID code changed was the one job guaranteed not to tell him.
#
# Fix: do everything synchronous up through the pull, write a "scheduled"
# marker, print the summary, and exit — THAT stdout is what gets
# delivered, before anything touches the gateway. The restart itself (and
# dependency reinstall, if needed — slow enough to risk delaying the
# notification) is launched as a fully-detached background step
# (sync-fork-restart-async.sh) that outlives this process. It finalizes
# the marker to "healthy" or "failed". A companion cron job,
# hermes-sync-fork-restart-check, reads that marker a few minutes later
# and alerts only if the async step never finished or failed — silent
# otherwise, matching the no-agent convention.

REPO_DIR="/home/jiddy/.hermes/hermes-agent"
HERMES_HOME="/home/jiddy/.hermes"
UV_BIN="/home/jiddy/.local/bin/uv"
GATEWAY_UNIT="hermes-gateway.service"
MARKER="$HERMES_HOME/cron/sync_fork_restart_state.json"
RESTART_LOG="/tmp/hermes-sync-fork-restart-async.log"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./sync-fork-common.sh
source "$SCRIPT_DIR/sync-fork-common.sh"
RESTART_SCRIPT="$SCRIPT_DIR/sync-fork-restart-async.sh"

fail() {
  echo "hermes-sync-fork FAILED: $1"
  exit 1
}

trap 'fail "unexpected error at line $LINENO"' ERR

cd "$REPO_DIR" || fail "cannot cd into $REPO_DIR"

# Refuse to run if the live checkout isn't in the expected clean state —
# this should never happen (nothing should touch this checkout directly,
# per the standing isolated-clone-first rule), but if it ever does, that's
# exactly the kind of thing to fail loudly on rather than silently pull
# on top of.
[[ -z "$(git status --porcelain)" ]] || fail "live checkout has uncommitted changes — refusing to pull on top of unknown local state, needs manual review"

current_branch="$(git rev-parse --abbrev-ref HEAD)"
[[ "$current_branch" == "main" ]] || fail "live checkout is on branch '$current_branch', not main — refusing to pull, needs manual review"

git fetch origin --quiet || fail "git fetch origin failed"

before_head="$(git rev-parse HEAD)"
origin_head="$(git rev-parse origin/main)"

if [[ "$before_head" == "$origin_head" ]]; then
  # Nothing to do. Silence is correct here, not a gap.
  exit 0
fi

# Must be a clean fast-forward. If the live checkout has somehow diverged
# from origin/main (should be structurally impossible given nothing else
# writes to this checkout), that's a real problem — fail loudly rather
# than attempt any kind of merge/rebase unattended.
git merge-base --is-ancestor "$before_head" "$origin_head" \
  || fail "live checkout HEAD is not an ancestor of origin/main — this checkout has diverged and needs manual investigation, not an automated pull"

changed_files="$(git diff --name-only "$before_head" "$origin_head")"

git pull --ff-only origin main || fail "git pull --ff-only failed after passing the ancestor check — unexpected, needs manual review"

after_head="$(git rev-parse HEAD)"
[[ "$after_head" == "$origin_head" ]] || fail "pull completed but HEAD ($after_head) does not match origin/main ($origin_head)"

commit_count="$(git rev-list --count "$before_head..$after_head")"

# Only reinstall dependencies when something that affects them actually
# changed — most nights this sync is small (a doc/skill PR) and a full
# reinstall would be wasted work every single run. The actual reinstall
# runs in the detached async step (it can be slow); here we only decide
# whether it's needed, cheaply, from the diff we already have.
deps_changed=false
if grep -qE '^(pyproject\.toml|uv\.lock)$' <<< "$changed_files"; then
  deps_changed=true
fi

scheduled_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Write the "scheduled" marker BEFORE anything touches the gateway, so a
# reader always sees either "scheduled" (this step got at least this far)
# or a later "healthy"/"failed" from the async step — never nothing after
# a real sync happened.
write_marker "scheduled" "$scheduled_at" "$before_head" "$after_head" "$commit_count"

deps_note="not_needed"
[[ "$deps_changed" == true ]] && deps_note="scheduled"

echo "hermes-sync-fork: synced $commit_count commit(s) ($before_head -> $after_head), deps_reinstall=$deps_note, gateway restart scheduled (async) — hermes-sync-fork-restart-check will alert if it doesn't finish healthy"

# Launch the restart (and, if needed, the dependency reinstall) as a
# fully-detached background step that outlives this process. `setsid`
# gives it its own session (belt-and-suspenders: the cron scheduler
# already launches this script with start_new_session=True, but setsid
# here makes the detachment explicit and self-contained rather than
# relying on an invocation detail of the caller). stdin/stdout/stderr are
# all redirected away from this process's own pipes — the scheduler's
# subprocess.communicate() call blocks until BOTH pipes see EOF, which
# would otherwise mean waiting on this background step too, defeating the
# entire point. `disown` drops it from this shell's job table so bash
# doesn't try to track or wait on it either.
#
# Empirically verified on this VM (2026-08-20): a `setsid ... &
# disown`-launched child with fds fully redirected keeps running and
# completes its work after the parent script exits and the parent's
# process is gone.
nohup setsid bash "$RESTART_SCRIPT" "$scheduled_at" "$before_head" "$after_head" "$commit_count" "$deps_changed" \
  > "$RESTART_LOG" 2>&1 < /dev/null &
disown

exit 0
