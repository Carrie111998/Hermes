#!/usr/bin/env bash
# Shared helpers for the hermes-sync-fork restart-notification scripts
# (sync-fork.sh and sync-fork-restart-async.sh). Sourced, never executed
# directly.

# Atomically write the restart-state marker as JSON: temp file in the same
# directory + fsync + os.replace (matches foundation_cron_common.py's
# atomic_write_text convention -- rename on the same filesystem is atomic,
# so a reader (sync-fork-restart-check.py) can never observe a
# half-written file).
#
# Args: state scheduled_at before_head after_head commit_count [reason]
write_marker() {
  local state="$1" scheduled_at="$2" before_head="$3" after_head="$4" commit_count="$5" reason="${6:-}"
  python3 - "$MARKER" "$state" "$scheduled_at" "$before_head" "$after_head" "$commit_count" "$reason" <<'PYEOF'
import json
import os
import sys
import tempfile

marker, state, scheduled_at, before_head, after_head, commit_count, reason = sys.argv[1:8]

data = {
    "state": state,
    "scheduled_at": scheduled_at,
    "before_head": before_head,
    "after_head": after_head,
    "commit_count": int(commit_count),
}
if reason:
    data["reason"] = reason

directory = os.path.dirname(marker) or "."
os.makedirs(directory, exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=directory, prefix=".sync_fork_restart_state.json.")
try:
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, marker)
finally:
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
PYEOF
}
