#!/usr/bin/env python3
"""Check whether last night's async gateway restart actually completed.

sync-fork.sh (nightly, no_agent) pulls new commits into the live checkout
and then launches the gateway restart as a fully-detached background step
(sync-fork-restart-async.sh) so its own "I just synced N commits"
notification can be delivered before the restart has any chance to kill
the delivering process. Before launching that step it writes a
"scheduled" marker to ~/.hermes/cron/sync_fork_restart_state.json; the
detached step overwrites it with a final "healthy" or "failed" state on
completion.

This job runs a few minutes after hermes-sync-fork and reports on what
that marker says:

  - marker file doesn't exist -> nothing synced last night; silent
    (no_agent convention: empty stdout = no notification, nothing to see)
  - state == "scheduled" (never advanced) -> the detached restart step
    itself went missing -- alert, this is a real problem
  - state == "failed"    -> the detached step ran and hit an error --
    alert with the recorded reason
  - state == "healthy"   -> restart completed and gateway confirmed
    healthy -- silent, matches convention (JID doesn't need to be told
    the happy path happened)

The marker is archived (moved aside, never left in place) after being
read in any of the three "something existed" cases, so a stale result
from a previous night can never cause a false alert on a night nothing
happened.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from foundation_cron_common import local_now

MARKER_PATH = Path.home() / ".hermes" / "cron" / "sync_fork_restart_state.json"
ARCHIVE_DIR = Path.home() / ".hermes" / "cron" / "sync_fork_restart_state_archive"


def _archive(marker_path: Path, now: datetime) -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%dT%H-%M-%S")
    destination = ARCHIVE_DIR / f"{stamp}.json"
    # Same-filesystem rename: atomic, and never leaves the live marker
    # path pointing at a half-consumed file if something below raises.
    marker_path.replace(destination)


def check(*, marker_path: Path, now: datetime) -> list[str]:
    if not marker_path.is_file():
        return []

    problems: list[str] = []
    try:
        data = json.loads(marker_path.read_text(encoding="utf-8"))
    except Exception as exc:
        problems.append(f"could not read/parse {marker_path}: {type(exc).__name__}: {exc}")
        _archive(marker_path, now)
        return problems

    state = data.get("state")
    scheduled_at = data.get("scheduled_at", "unknown time")
    before_head = data.get("before_head", "?")
    after_head = data.get("after_head", "?")
    commit_count = data.get("commit_count", "?")
    commit_range = f"{before_head}..{after_head}"

    if state == "scheduled":
        problems.append(
            f"restart was scheduled at {scheduled_at} for commit range {commit_range} "
            f"({commit_count} commit(s)) but never completed — the async restart step "
            "may have failed to launch or been killed"
        )
    elif state == "failed":
        reason = data.get("reason", "no reason recorded")
        problems.append(
            f"async gateway restart FAILED for commit range {commit_range} "
            f"({commit_count} commit(s)), scheduled at {scheduled_at}: {reason}"
        )
    elif state == "healthy":
        pass
    else:
        problems.append(
            f"sync_fork_restart_state.json has an unrecognized state {state!r} "
            f"(scheduled_at={scheduled_at}, range={commit_range})"
        )

    _archive(marker_path, now)
    return problems


def main() -> int:
    now = local_now()
    try:
        problems = check(marker_path=MARKER_PATH, now=now)
    except Exception as exc:
        problems = [f"sync-fork-restart-check crashed: {type(exc).__name__}: {exc}"]
    if problems:
        print("🩺 Operations Alert — hermes-sync-fork restart check")
        for problem in problems:
            print(f"- {problem}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
