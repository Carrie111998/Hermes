"""CodeDriftMonitor — emits CODE_DRIFT when the deployed checkout drifts from main.

The gateway's editable install imports the WORKING TREE of the shared
checkout at ~/.hermes/agent-src, which is deliberately kept on a detached
HEAD so worktree agents can land commits onto the `main` ref via
`git branch -f`. A commit landed on main therefore does NOT run until the
checkout is fast-forwarded and the gateway restarted — on 2026-07-20/21
three restart cycles ran stale code while every session believed the fix
was live because "main tip moved".

Two local layers already surface this (laptop-monitor tray row,
events_doctor); this producer is the third: drift as an event-bus event so
it reaches Telegram when the operator is away from the machine.

Emission policy
---------------
Edge-triggered on the WALL clock (state is persisted across restarts):
fire on the rising edge of drift, fire immediately when the drift *shape*
(state, behind, ahead) changes, re-ping a sustained episode every 6 h, and
emit a single status="resolved" event on the falling edge — but only if
the episode actually alerted. Episode state lives in
~/.hermes/notifications/code_drift_state.json so the resolved ping
survives the common remediation path (FF, then restart the gateway).

Read-only git, bounded subprocess (15 s timeout). The monitor NEVER
fast-forwards — remediation is a deliberate operator action.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from events.bus import EventBus
from events.paths import code_drift_state_path
from events.schema import EventType
from events.state import load_state, save_state

logger = logging.getLogger(__name__)

# One git probe per 15 min; a sustained episode re-pings every 6 h.
DEFAULT_CHECK_INTERVAL_SECONDS = 900.0
DEFAULT_RE_ALERT_COOLDOWN_SECONDS = 6 * 3600.0
MISSED_SUBJECTS_CAP = 5

_AGENT_SRC_DEFAULT = Path.home() / ".hermes" / "agent-src"


def _agent_src_root() -> Path:
    return Path(os.getenv("HERMES_AGENT_SRC") or _AGENT_SRC_DEFAULT)


@dataclass(frozen=True)
class DriftSample:
    """Point-in-time relationship of the checkout's HEAD to refs/heads/main."""

    state: str  # "in_sync" | "behind" | "ahead" | "diverged"
    head: str
    main: str
    behind_count: int = 0
    ahead_count: int = 0
    dirty: bool = False
    missed_subjects: Tuple[str, ...] = ()

    @property
    def shape(self) -> List:
        """The identity of a drift episode: a change here re-alerts
        immediately (list, not tuple, so it round-trips through JSON)."""
        return [self.state, self.behind_count, self.ahead_count]


def _git(repo: Path, *args: str) -> Tuple[int, str]:
    """Run a read-only git command; returns (returncode, stdout)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=15,
        )
        return proc.returncode, proc.stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def sample_code_drift(repo: Optional[Path] = None) -> Optional[DriftSample]:
    """Read-only git probe of HEAD vs refs/heads/main.

    Returns None when there is nothing to evaluate (no checkout, refs
    unresolvable, git broken) — the caller treats None as a no-op so the
    poll loop never crashes and a transient git failure never fabricates
    a drift or a recovery.
    """
    repo = Path(repo) if repo is not None else _agent_src_root()
    # .git is a directory in a normal checkout and a file in a worktree.
    if not (repo / ".git").exists():
        return None

    rc_head, head = _git(repo, "rev-parse", "--verify", "HEAD")
    rc_main, main = _git(repo, "rev-parse", "--verify", "refs/heads/main")
    if rc_head != 0 or rc_main != 0:
        return None
    head, main = head.strip(), main.strip()
    dirty = bool(_git(repo, "status", "--porcelain")[1].strip())

    if head == main:
        return DriftSample(state="in_sync", head=head, main=main, dirty=dirty)

    def _count(rev_range: str) -> int:
        out = _git(repo, "rev-list", "--count", rev_range)[1].strip()
        try:
            return int(out)
        except ValueError:
            return 0

    head_behind = _git(repo, "merge-base", "--is-ancestor",
                       "HEAD", "refs/heads/main")[0] == 0
    head_ahead = _git(repo, "merge-base", "--is-ancestor",
                      "refs/heads/main", "HEAD")[0] == 0

    if head_behind:
        subjects = tuple(
            line.strip() for line in
            _git(repo, "log", "--format=%h %s", f"-{MISSED_SUBJECTS_CAP}",
                 "HEAD..refs/heads/main")[1].splitlines()
            if line.strip()
        )
        return DriftSample(
            state="behind", head=head, main=main,
            behind_count=_count("HEAD..refs/heads/main"),
            dirty=dirty, missed_subjects=subjects,
        )
    if head_ahead:
        return DriftSample(
            state="ahead", head=head, main=main,
            ahead_count=_count("refs/heads/main..HEAD"), dirty=dirty,
        )
    return DriftSample(
        state="diverged", head=head, main=main,
        behind_count=_count("HEAD..refs/heads/main"),
        ahead_count=_count("refs/heads/main..HEAD"), dirty=dirty,
    )


class CodeDriftMonitor:  # implemented in the next commit
    pass
