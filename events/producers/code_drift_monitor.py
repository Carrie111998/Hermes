"""CodeDriftMonitor — emits CODE_DRIFT when a deployed checkout drifts from trunk.

Two repos on this box are "production by working tree":

1. ~/.hermes/agent-src (trunk `main`) — the gateway's editable install
   imports the WORKING TREE of this shared checkout, which is deliberately
   kept on a detached HEAD so worktree agents can land commits onto the
   `main` ref via `git branch -f`. A commit landed on main therefore does
   NOT run until the checkout is fast-forwarded and the gateway restarted —
   on 2026-07-20/21 three restart cycles ran stale code while every session
   believed the fix was live because "main tip moved".

2. ~/.hermes itself (trunk `master`) — cron script-slot jobs and Windows
   Scheduled Tasks resolve ABSOLUTE paths under ~/.hermes/scripts/,
   ~/.hermes/profiles/*/scripts/ and ~/.hermes/ops/. Landing a commit on
   master does not deploy it there; only the working tree runs. On
   2026-07-28, 62 commits sat undeployed for three days behind a clean
   `git status` because the live checkout had been pointed off master.

Per-repo trunk ref names are NOT optional decoration. Until 2026-07-28 this
monitor hardcoded `refs/heads/main` and watched agent-src alone. ~/.hermes
has no `main` branch, so simply adding the path without its trunk name
would make every probe fail the ref lookup and — under the old code —
return None, i.e. report "nothing to evaluate" forever. That is why a repo
whose CONFIGURED trunk ref cannot be resolved is now a loud
state="misconfigured" event rather than a silent skip: a monitor that is a
no-op by construction is worse than no monitor, because it is believed.

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
from events.paths import code_drift_state_path, hermes_repo_root as _paths_root
from events.schema import EventType
from events.state import load_state, save_state

logger = logging.getLogger(__name__)

# One git probe per 15 min; a sustained episode re-pings every 6 h.
DEFAULT_CHECK_INTERVAL_SECONDS = 900.0
DEFAULT_RE_ALERT_COOLDOWN_SECONDS = 6 * 3600.0
MISSED_SUBJECTS_CAP = 5

_AGENT_SRC_DEFAULT = Path.home() / ".hermes" / "agent-src"

DEFAULT_TRUNK_REF = "refs/heads/main"


def _agent_src_root() -> Path:
    return Path(os.getenv("HERMES_AGENT_SRC") or _AGENT_SRC_DEFAULT)


def _hermes_root() -> Path:
    """The ~/.hermes parent repo root.

    Deliberately the CANONICAL root (events.paths._root -> hermes_constants
    .get_default_hermes_root), never the profile-scoped get_hermes_home():
    the repo lives at ~/.hermes, not ~/.hermes/profiles/<name>. Under pytest
    (HERMES_HOME -> a tmpdir outside ~/.hermes) this resolves to that
    tmpdir, which has no .git, so the probe skips and the suite stays
    hermetic.
    """
    return _paths_root()


# The EXECUTED surface of the ~/.hermes working tree: cron script-slot jobs
# and Windows Scheduled Tasks resolve absolute paths under these dirs, so a
# difference here means something stale is genuinely RUNNING. Drift confined
# to docs/, artifacts/, backups/ etc. is inert and must not page anyone —
# ~/.hermes carries far more non-executed churn than agent-src.
#
# The `:(glob)` magic prefix and the trailing `/**` are BOTH required.
# Measured against the live repo 2026-07-28:
#   'profiles/*/scripts'            -> 0 files   (trailing literal never aligns)
#   'profiles/*/scripts/**'         -> 302 files (default wildmatch lets * cross /)
#   ':(glob)profiles/*/scripts/**'  -> 51 files  (correct: one dir level)
# The first form fails SILENTLY — a gate that matches nothing reports "no
# executed change" forever, i.e. the same class of fail-silent bug this
# monitor exists to catch.
HERMES_EXECUTED_DIRS: Tuple[str, ...] = (
    ":(glob)scripts/**",
    ":(glob)ops/**",
    ":(glob)profiles/*/scripts/**",
)


@dataclass(frozen=True)
class WatchedRepo:
    """A checkout that IS production, plus the trunk ref it must track.

    trunk_ref is a fully-qualified ref (``refs/heads/<name>``) and is
    per-repo on purpose: agent-src lands on `main`, ~/.hermes lands on
    `master` and has no `main` at all.

    executed_dirs, when non-empty, narrows ALERTING (never detection) to
    drift that actually touches executed code. agent-src leaves it empty:
    the editable install imports the whole package, so every file is
    executed surface.
    """

    name: str
    path: Path
    trunk_ref: str = DEFAULT_TRUNK_REF
    executed_dirs: Tuple[str, ...] = ()

    @property
    def trunk_name(self) -> str:
        """Bare branch name for operator-facing text ('main' / 'master')."""
        return self.trunk_ref.rsplit("/", 1)[-1]


def watched_repos() -> List[WatchedRepo]:
    """The repos whose WORKING TREE is deployed code, newest concern last.

    Each entry carries its own trunk ref name. Adding a repo here without
    its correct trunk name yields a permanently "misconfigured" alert, not
    a silent clean bill of health — see the module docstring.
    """
    return [
        WatchedRepo("agent-src", _agent_src_root(), "refs/heads/main"),
        WatchedRepo("hermes", _hermes_root(), "refs/heads/master",
                    executed_dirs=HERMES_EXECUTED_DIRS),
    ]


@dataclass(frozen=True)
class DriftSample:
    """Point-in-time relationship of a checkout's HEAD to its trunk ref."""

    state: str  # "in_sync" | "behind" | "ahead" | "diverged" | "misconfigured"
    head: str
    trunk: str
    behind_count: int = 0
    ahead_count: int = 0
    dirty: bool = False
    missed_subjects: Tuple[str, ...] = ()
    repo_name: str = "agent-src"
    trunk_ref: str = DEFAULT_TRUNK_REF
    detail: str = ""
    executed_gated: bool = False
    executed_changed: bool = False
    executed_files: Tuple[str, ...] = ()

    @property
    def shape(self) -> List:
        """The identity of a drift episode: a change here re-alerts
        immediately (list, not tuple, so it round-trips through JSON)."""
        return [self.state, self.behind_count, self.ahead_count]

    @property
    def alerts(self) -> bool:
        """Whether this sample should page the operator.

        Detection and alerting are deliberately separate. On a gated repo
        (~/.hermes) the checkout can legitimately sit behind trunk on docs,
        artifacts or backups without a single executed byte being stale —
        alerting on that would train the operator to ignore the channel,
        which is how the 2026-07-28 incident stayed invisible for 3 days.
        The drift is still SAMPLED and logged; it just does not page.

        "misconfigured" is never gated: if the trunk ref does not resolve we
        cannot compute an executed diff at all, so silence there would
        recreate the exact fail-silent hole this rewrite closes.
        """
        if self.state == "in_sync":
            return False
        if self.state == "misconfigured":
            return True
        if self.executed_gated and not self.executed_changed:
            return False
        return True


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


def sample_code_drift(
    repo: Optional[Path] = None,
    trunk_ref: str = DEFAULT_TRUNK_REF,
    *,
    repo_name: str = "agent-src",
    executed_dirs: Tuple[str, ...] = (),
) -> Optional[DriftSample]:
    """Read-only git probe of HEAD vs ``trunk_ref``.

    Returns None ONLY when the repo is genuinely absent from this box (no
    .git) — the caller treats None as a no-op so the poll loop never
    crashes and a missing checkout never fabricates a drift or a recovery.

    A repo that IS present but whose CONFIGURED trunk ref cannot be
    resolved returns state="misconfigured" instead, which flows through the
    normal edge machinery and alerts. That asymmetry is the whole point:
    before 2026-07-28 an unresolvable trunk ref returned None, so pointing
    this monitor at a repo whose trunk was not literally named `main`
    produced a permanently silent "nothing to evaluate" — a watcher that
    reports clean forever. Absent repo = skip; present repo we cannot
    evaluate = shout.
    """
    repo = Path(repo) if repo is not None else _agent_src_root()
    # .git is a directory in a normal checkout and a file in a worktree.
    if not (repo / ".git").exists():
        return None

    def _misconfigured(detail: str) -> DriftSample:
        logger.error("CodeDriftMonitor: %s in %s — cannot evaluate drift",
                     detail, repo)
        return DriftSample(
            state="misconfigured", head="", trunk="", detail=detail,
            repo_name=repo_name, trunk_ref=trunk_ref,
        )

    rc_head, head = _git(repo, "rev-parse", "--verify", "HEAD")
    rc_trunk, trunk = _git(repo, "rev-parse", "--verify", trunk_ref)
    if rc_trunk != 0:
        return _misconfigured(f"configured trunk ref {trunk_ref} does not exist")
    if rc_head != 0:
        return _misconfigured("HEAD does not resolve")
    head, trunk = head.strip(), trunk.strip()
    dirty = bool(_git(repo, "status", "--porcelain")[1].strip())

    common = dict(repo_name=repo_name, trunk_ref=trunk_ref)
    if head == trunk:
        return DriftSample(state="in_sync", head=head, trunk=trunk,
                           dirty=dirty, **common)

    if executed_dirs:
        # Tree-vs-tree diff, so this answers the same question for behind,
        # ahead and diverged alike: does the EXECUTED surface differ between
        # what is deployed and what is landed?
        rc_diff, diff_out = _git(repo, "diff", "--name-only", "HEAD",
                                 trunk_ref, "--", *executed_dirs)
        changed = tuple(ln.strip() for ln in diff_out.splitlines() if ln.strip())
        if rc_diff != 0:
            # Never let a failed gate silence a real drift — fail LOUD.
            logger.warning("CodeDriftMonitor: executed-dir diff failed in %s; "
                           "alerting as if executed code changed", repo)
            common.update(executed_gated=False, executed_changed=True)
        else:
            common.update(
                executed_gated=True,
                executed_changed=bool(changed),
                executed_files=changed[:MISSED_SUBJECTS_CAP],
            )

    def _count(rev_range: str) -> int:
        out = _git(repo, "rev-list", "--count", rev_range)[1].strip()
        try:
            return int(out)
        except ValueError:
            return 0

    head_behind = _git(repo, "merge-base", "--is-ancestor",
                       "HEAD", trunk_ref)[0] == 0
    head_ahead = _git(repo, "merge-base", "--is-ancestor",
                      trunk_ref, "HEAD")[0] == 0

    if head_behind:
        subjects = tuple(
            line.strip() for line in
            _git(repo, "log", "--format=%h %s", f"-{MISSED_SUBJECTS_CAP}",
                 f"HEAD..{trunk_ref}")[1].splitlines()
            if line.strip()
        )
        return DriftSample(
            state="behind", head=head, trunk=trunk,
            behind_count=_count(f"HEAD..{trunk_ref}"),
            dirty=dirty, missed_subjects=subjects, **common,
        )
    if head_ahead:
        return DriftSample(
            state="ahead", head=head, trunk=trunk,
            ahead_count=_count(f"{trunk_ref}..HEAD"), dirty=dirty, **common,
        )
    return DriftSample(
        state="diverged", head=head, trunk=trunk,
        behind_count=_count(f"HEAD..{trunk_ref}"),
        ahead_count=_count(f"{trunk_ref}..HEAD"), dirty=dirty, **common,
    )


class CodeDriftMonitor:
    """Probes one watched repo's checkout-vs-trunk drift, emitting on the edge.

    Call check() from the gateway subscriber poll loop (any cadence — it
    self-gates to one git probe per ``check_interval_seconds``). Sampler,
    wall clock, and state path are injectable so the edge core is fully
    testable without git, sleeps, or live ~/.hermes I/O.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        repo: Optional[WatchedRepo] = None,
        repo_path: Optional[Path] = None,
        sampler: Optional[Callable[[], Optional[DriftSample]]] = None,
        clock: Optional[Callable[[], float]] = None,
        state_path: Optional[Path] = None,
        check_interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
        re_alert_cooldown_seconds: float = DEFAULT_RE_ALERT_COOLDOWN_SECONDS,
    ):
        self.bus = bus
        # One monitor instance per watched repo: each keeps its own episode
        # state file, so agent-src's cooldown/resolved edges never mask
        # ~/.hermes's and vice versa.
        self.repo = repo
        self._repo_path = Path(repo_path) if repo_path else (
            Path(repo.path) if repo else None
        )
        self._trunk_ref = repo.trunk_ref if repo else DEFAULT_TRUNK_REF
        self._repo_name = repo.name if repo else "agent-src"
        self._executed_dirs = repo.executed_dirs if repo else ()
        self._sampler = sampler or (
            lambda: sample_code_drift(self._repo_path, self._trunk_ref,
                                      repo_name=self._repo_name,
                                      executed_dirs=self._executed_dirs)
        )
        # WALL clock, not monotonic: last_emit is persisted across restarts.
        self._clock = clock or time.time
        self._state_path = (
            Path(state_path) if state_path
            else code_drift_state_path(self._repo_name)
        )
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

    @property
    def repo_name(self) -> str:
        """Identity of the watched repo ('agent-src' / 'hermes')."""
        return self._repo_name

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
        if not sample.alerts:
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

    def _identity(self, sample: DriftSample) -> dict:
        """Repo/trunk identity carried on every payload.

        `trunk` is the honest field name now that watched repos disagree on
        the branch (agent-src `main`, ~/.hermes `master`); `main` is kept as
        an alias so older consumers and stored events keep rendering.
        """
        return {
            "repo": self._repo_str(),
            "repo_name": sample.repo_name or self._repo_name,
            "trunk_ref": sample.trunk_ref or self._trunk_ref,
            "trunk_name": (sample.trunk_ref or self._trunk_ref).rsplit("/", 1)[-1],
            "trunk": sample.trunk[:9],
            "main": sample.trunk[:9],  # back-compat alias
        }

    def _emit_drift(self, sample: DriftSample) -> str:
        logger.warning(
            "Code drift [%s]: checkout %s %s (behind %d / ahead %d, dirty=%s) "
            "— HEAD %s vs trunk %s%s",
            self._repo_name, sample.state,
            (sample.trunk_ref or self._trunk_ref).rsplit("/", 1)[-1],
            sample.behind_count, sample.ahead_count,
            sample.dirty, sample.head[:9] or "?", sample.trunk[:9] or "?",
            f" — {sample.detail}" if sample.detail else "",
        )
        return self.bus.emit(
            event_type=EventType.CODE_DRIFT,
            source="system",
            payload={
                "status": "drifting",
                "state": sample.state,
                "head": sample.head[:9],
                "behind_count": sample.behind_count,
                "ahead_count": sample.ahead_count,
                "dirty": sample.dirty,
                "missed_subjects": list(sample.missed_subjects),
                "detail": sample.detail,
                "executed_gated": sample.executed_gated,
                "executed_files": list(sample.executed_files),
                **self._identity(sample),
            },
            tags=["code", "drift", sample.state],
        )

    def _emit_resolved(self, sample: DriftSample) -> str:
        # A gated repo can leave the alerting state without reaching
        # in_sync: the commits are still unmerged, they just no longer touch
        # executed code. Say which one happened rather than claiming a
        # sync that did not occur.
        inert = sample.state != "in_sync"
        logger.info(
            "Code drift resolved [%s]: %s @ %s", self._repo_name,
            "drift no longer touches executed code" if inert
            else "checkout back in sync", sample.trunk[:9],
        )
        return self.bus.emit(
            event_type=EventType.CODE_DRIFT,
            source="system",
            payload={
                "status": "resolved",
                "head": sample.head[:9],
                "state": sample.state,
                "inert": inert,
                **self._identity(sample),
            },
            tags=["code", "drift", "resolved"],
        )
