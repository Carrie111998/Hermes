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


class CodeDriftMonitor:
    """Probes checkout-vs-main drift and emits CODE_DRIFT on the edge.

    Call check() from the gateway subscriber poll loop (any cadence — it
    self-gates to one git probe per ``check_interval_seconds``). Sampler,
    wall clock, and state path are injectable so the edge core is fully
    testable without git, sleeps, or live ~/.hermes I/O.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        repo_path: Optional[Path] = None,
        sampler: Optional[Callable[[], Optional[DriftSample]]] = None,
        clock: Optional[Callable[[], float]] = None,
        state_path: Optional[Path] = None,
        check_interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
        re_alert_cooldown_seconds: float = DEFAULT_RE_ALERT_COOLDOWN_SECONDS,
    ):
        self.bus = bus
        self._repo_path = Path(repo_path) if repo_path else None
        self._sampler = sampler or (lambda: sample_code_drift(self._repo_path))
        # WALL clock, not monotonic: last_emit is persisted across restarts.
        self._clock = clock or time.time
        self._state_path = Path(state_path) if state_path else code_drift_state_path()
        self.check_interval_seconds = check_interval_seconds
        self.re_alert_cooldown_seconds = re_alert_cooldown_seconds

        self._last_check: Optional[float] = None
        state = load_state(self._state_path, {})
        self._alerting: bool = bool(state.get("alerting"))
        last_emit = state.get("last_emit_wall")
        self._last_emit: Optional[float] = (
            float(last_emit) if isinstance(last_emit, (int, float)) else None
        )
        last_shape = state.get("last_shape")
        self._last_shape: Optional[List] = (
            list(last_shape) if isinstance(last_shape, list) else None
        )

    def check(self) -> Optional[str]:
        """Probe if the interval elapsed; emit if an edge fired.

        Swallows sampler failures — a git hiccup must never crash the
        gateway poll loop, and must not fabricate drift or recovery.
        """
        now = self._clock()
        if (self._last_check is not None
                and now - self._last_check < self.check_interval_seconds):
            return None
        self._last_check = now
        try:
            sample = self._sampler()
        except Exception:
            logger.exception("CodeDriftMonitor: sampler raised")
            return None
        if sample is None:
            return None
        return self.evaluate(sample, now)

    def evaluate(self, sample: DriftSample, now: float) -> Optional[str]:
        """Pure edge core given (sample, wall-clock now) + persisted state."""
        if sample.state == "in_sync":
            if not self._alerting:
                return None
            # Falling edge: the episode alerted, so close the loop.
            self._alerting = False
            self._last_shape = None
            self._last_emit = None  # next rising edge fires immediately
            self._save()
            return self._emit_resolved(sample)

        shape = sample.shape
        rising_edge = not self._alerting
        shape_changed = self._last_shape is not None and shape != self._last_shape
        cooldown_elapsed = (
            self._last_emit is None
            or (now - self._last_emit) >= self.re_alert_cooldown_seconds
        )
        self._alerting = True
        if not (rising_edge or shape_changed or cooldown_elapsed):
            return None

        self._last_emit = now
        self._last_shape = shape
        self._save()
        return self._emit_drift(sample)

    def _save(self) -> None:
        try:
            save_state(self._state_path, {
                "alerting": self._alerting,
                "last_emit_wall": self._last_emit,
                "last_shape": self._last_shape,
            })
        except Exception:  # pragma: no cover - defensive
            logger.exception("CodeDriftMonitor: state persist failed")

    def _repo_str(self) -> str:
        return str(self._repo_path or _agent_src_root())

    def _emit_drift(self, sample: DriftSample) -> str:
        logger.warning(
            "Code drift: checkout %s main (behind %d / ahead %d, dirty=%s) "
            "— HEAD %s vs main %s",
            sample.state, sample.behind_count, sample.ahead_count,
            sample.dirty, sample.head[:9], sample.main[:9],
        )
        return self.bus.emit(
            event_type=EventType.CODE_DRIFT,
            source="system",
            payload={
                "status": "drifting",
                "state": sample.state,
                "head": sample.head[:9],
                "main": sample.main[:9],
                "behind_count": sample.behind_count,
                "ahead_count": sample.ahead_count,
                "dirty": sample.dirty,
                "missed_subjects": list(sample.missed_subjects),
                "repo": self._repo_str(),
            },
            tags=["code", "drift", sample.state],
        )

    def _emit_resolved(self, sample: DriftSample) -> str:
        logger.info("Code drift resolved: checkout back in sync @ %s",
                    sample.main[:9])
        return self.bus.emit(
            event_type=EventType.CODE_DRIFT,
            source="system",
            payload={
                "status": "resolved",
                "head": sample.head[:9],
                "main": sample.main[:9],
                "repo": self._repo_str(),
            },
            tags=["code", "drift", "resolved"],
        )
