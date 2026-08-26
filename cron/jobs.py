"""
Cron job storage and management.

Jobs are stored in ~/.hermes/cron/jobs.json
Output is saved to ~/.hermes/cron/output/{job_id}/{timestamp}.md
"""

import contextlib
import copy
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import logging
import shutil
import tempfile
import threading
import time
import os
import re
import uuid

# Cross-process advisory file locking for jobs.json critical sections.
# fcntl is Unix-only; on Windows fall back to msvcrt. Either may be absent,
# in which case _jobs_lock() degrades to in-process locking only (the old
# behaviour) rather than failing.
try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows
    msvcrt = None
from datetime import datetime, timedelta
from pathlib import Path
from hermes_constants import get_hermes_home
from typing import Optional, Dict, List, Any, Set, Tuple, Union

logger = logging.getLogger(__name__)

from hermes_time import now as _hermes_now
from jobflow_dispatch.quarantine_control import default_control_store
from utils import atomic_replace

try:
    from croniter import croniter
    HAS_CRONITER = True
except ImportError:
    HAS_CRONITER = False

# =============================================================================
# Configuration
# =============================================================================

# Cron is per-profile by design (issue #4707). Each profile owns its own cron
# store under its own HERMES_HOME, and a profile-scoped gateway runs that
# profile's jobs under that same HERMES_HOME — so a job authored in profile
# `coder` lives in `~/.hermes/profiles/coder/cron/jobs.json` and executes with
# `coder`'s `.env`, `config.yaml`, and skills. We deliberately anchor on
# `get_hermes_home()` (the active profile home), NOT `get_default_hermes_root()`
# (the shared root). Anchoring at the root would funnel every profile's jobs
# into one shared `jobs.json` and run them under whatever HERMES_HOME the
# ticker process happens to have — leaking config/credentials/skills across
# profiles (the security boundary #4707 was filed for). Do NOT change this to
# the default root: that re-breaks per-profile isolation.
#
# FORK (2026-07-14, root-fix 3b91389a1): resolution is DYNAMIC (call-time),
# not import-pinned. cron.jobs is imported once per process — frequently
# before a profile switch or a test's hermetic HERMES_HOME lands — so freezing
# the paths at import made writes land in whatever home was active at import
# (historically the real ~/.hermes/cron, polluting the live store during test
# collection). The `_get_*` helpers below re-resolve on every call, mirroring
# `_get_hermes_home()` / `_get_lock_paths()` in cron/scheduler.py. All internal
# reads/writes MUST go through `_current_cron_store()` (which layers the
# upstream 0.19.0 per-context `use_cron_store()` override ON TOP of these
# helpers), never the module-level snapshots defined further down.
#
# Test/override hook: `_hermes_home` mirrors cron/scheduler.py's slot — leave
# it None to resolve the active HERMES_HOME dynamically at call time.
_hermes_home: Optional[Path] = None


def _get_hermes_home() -> Path:
    """Resolve Hermes home at call time (per-profile, #4707).

    Honors the `_hermes_home` override when set (test hook / future profile
    scoping), else the active `get_hermes_home()` — which itself layers the
    context-local override, `HERMES_HOME`, and the platform default. Mirrors
    `cron/scheduler.py::_get_hermes_home`.
    """
    return _hermes_home or get_hermes_home()


def _get_hermes_dir() -> Path:
    """Resolved Hermes home dir (``.resolve()``d, matching legacy HERMES_DIR)."""
    return _get_hermes_home().resolve()


def _get_cron_dir() -> Path:
    """Active profile's ``cron`` directory."""
    return _get_hermes_dir() / "cron"


def _get_jobs_file() -> Path:
    """Active profile's ``cron/jobs.json`` store."""
    return _get_cron_dir() / "jobs.json"


def _get_output_dir() -> Path:
    """Active profile's ``cron/output`` directory."""
    return _get_cron_dir() / "output"


def _get_ticker_heartbeat_file() -> Path:
    """Heartbeat file the in-process ticker touches on every loop iteration.

    The gateway process and the (separate) ``hermes cron status`` process share
    it so status can tell whether the ticker THREAD is alive, not just whether
    the gateway PROCESS exists — a ticker that dies silently inside a live
    gateway would otherwise report healthy (#32612, #32895).
    """
    return _current_cron_store().cron_dir / "ticker_heartbeat"


def _get_ticker_success_file() -> Path:
    """Last tick that completed WITHOUT raising.

    Distinguishing this from the plain heartbeat lets status detect a ticker
    that is alive but failing every tick.
    """
    return _current_cron_store().cron_dir / "ticker_last_success"


# Backward-compatible module-level path SNAPSHOTS, resolved once at import for
# callers that still read `cron.jobs.CRON_DIR` / `JOBS_FILE` / `OUTPUT_DIR` /
# `HERMES_DIR` / `TICKER_*` as attributes, and for the long-standing test
# monkeypatch pattern (`monkeypatch.setattr("cron.jobs.CRON_DIR", ...)` — see
# `_current_cron_store()` step 2, which honors a deliberately re-pointed
# surface). Internal code does NOT read these directly — it goes through
# `_current_cron_store()` / the dynamic `_get_*` helpers above — so a stale
# snapshot (e.g. pinned to whatever home existed at import) can no longer
# misroute a write into the wrong store. Prefer the helpers in new code.
HERMES_DIR = _get_hermes_dir()
CRON_DIR = _get_cron_dir()
JOBS_FILE = _get_jobs_file()
TICKER_HEARTBEAT_FILE = _get_cron_dir() / "ticker_heartbeat"
TICKER_SUCCESS_FILE = _get_cron_dir() / "ticker_last_success"
# Default ticker loop interval (seconds). The single source of truth shared by
# the in-process ticker (cron/scheduler_provider.py) and the staleness
# threshold in `hermes cron status` (hermes_cli/cron.py), so the two never
# drift apart.
TICKER_INTERVAL_SECONDS = 60

# In-process lock protecting load_jobs→modify→save_jobs cycles.
# Required when tick() runs jobs in parallel threads — without this,
# concurrent mark_job_run / advance_next_run calls can clobber each other.
_jobs_file_lock = threading.RLock()
_jobs_lock_state = threading.local()

# Upper bound on waiting for the cross-process .jobs.lock flock (#60703).
# Every cron function in the process funnels through _jobs_lock(), and the
# flock is taken while holding the process-wide RLock — so an unbounded wait
# on a lock held by a wedged sibling process silently freezes the ticker
# heartbeat and every job forever.  30s is orders of magnitude above any
# legitimate critical section (field updates only) while keeping the ticker's
# worst-case stall well under one status-alarm threshold.
_JOBS_LOCK_TIMEOUT_SECONDS = 30.0
OUTPUT_DIR = _get_output_dir()
ONESHOT_GRACE_SECONDS = 120

# How many completed pause/resume cycles a job record keeps in
# ``paused_history``. Bounded so a job that is paused and resumed daily cannot
# grow jobs.json without limit; oldest entries are dropped first.
PAUSED_HISTORY_LIMIT = 10


@dataclass(frozen=True)
class _CronStorePaths:
    cron_dir: Path
    jobs_file: Path
    output_dir: Path


_cron_store_override: ContextVar[Optional[_CronStorePaths]] = ContextVar(
    "cron_store_override",
    default=None,
)


# Import-time snapshot of the compatibility constants, so deliberate
# re-pointing of the module surface (monkeypatched CRON_DIR/JOBS_FILE/
# OUTPUT_DIR — the documented escape hatch existing tests/embedders use)
# is distinguishable from the constants merely being stale.
_IMPORT_STORE = _CronStorePaths(CRON_DIR, JOBS_FILE, OUTPUT_DIR)
# Import-time home marker (fork). A repointed store surface only counts as
# the DELIBERATE escape hatch when HERMES_DIR itself is untouched; if
# HERMES_DIR was also swapped, that is the stale-import-snapshot shape the
# fork root-fix exists to neutralize (memory
# `cron_jobs_paths_import_pinned_real_store_pollution`) — dynamic resolution
# from the active HERMES_HOME must win instead.
_IMPORT_HERMES_DIR = HERMES_DIR


def _current_cron_store() -> _CronStorePaths:
    """Return paths pinned to this execution context's profile.

    Precedence, most explicit first:

    1. an active use_cron_store() override (ContextVar);
    2. deliberately re-pointed module constants — if CRON_DIR/JOBS_FILE/
       OUTPUT_DIR no longer match their import-time values, someone chose
       the documented process-wide compatibility surface; honor it;
    3. the ACTIVE profile home, resolved FRESH ON EVERY CALL via the fork's
       dynamic `_get_*` helpers (`_hermes_home` slot, then get_hermes_home():
       context-local override, then the HERMES_HOME env var) — so a test
       or embedder that re-points HERMES_HOME after this module was
       imported reads/writes ITS OWN store, not whatever jobs.json the
       import happened to freeze (the filed incident on both sides: fixtures
       that patched the env too late silently rewrote the user's real
       ~/.hermes/cron/jobs.json — fork root-fix 3b91389a1, memory
       `cron_jobs_paths_import_pinned_real_store_pollution`).

    The fork's dynamic resolution is the OUTER default rule: with no explicit
    override active (production), the store is always the active profile
    home's `cron/jobs.json` — per-context isolation (steps 1-2) can only be
    entered deliberately and cannot silently redirect the real store.
    """
    override = _cron_store_override.get()
    if override is not None:
        return override
    live_constants = _CronStorePaths(CRON_DIR, JOBS_FILE, OUTPUT_DIR)
    if live_constants != _IMPORT_STORE and HERMES_DIR == _IMPORT_HERMES_DIR:
        # Deliberately re-pointed store surface (documented escape hatch):
        # the home marker is untouched, so this is not a stale snapshot.
        # When HERMES_DIR was ALSO swapped (stale-import-snapshot shape),
        # fall through — the fork's dynamic resolution below must win.
        return live_constants
    home = _get_hermes_dir()
    if home == HERMES_DIR:
        # Home unchanged since import — the common production path; return
        # the import-time snapshot objects unchanged (identity preserved).
        return live_constants
    cron_dir = home / "cron"
    return _CronStorePaths(cron_dir, cron_dir / "jobs.json", cron_dir / "output")


@contextlib.contextmanager
def use_cron_store(home: Union[str, Path]):
    """Route cron storage to ``home`` without mutating process globals."""
    cron_dir = Path(home).expanduser().resolve() / "cron"
    token = _cron_store_override.set(
        _CronStorePaths(
            cron_dir=cron_dir,
            jobs_file=cron_dir / "jobs.json",
            output_dir=cron_dir / "output",
        )
    )
    try:
        yield
    finally:
        _cron_store_override.reset(token)


def get_cron_output_dir() -> Path:
    """Return the output directory for the active cron store context."""
    return _current_cron_store().output_dir


# Fallback stale-recovery window for a one-shot's running-claim (#59229) when
# the cron inactivity timeout is disabled (HERMES_CRON_TIMEOUT=0 → unlimited),
# in which case no finite run bound exists to derive from. Also acts as the
# floor for the derived value so a very short configured timeout can't make the
# claim expire mid-run.
ONESHOT_RUN_CLAIM_TTL_SECONDS = 1800

# The derived TTL is the cron inactivity timeout times this headroom multiplier.
# A healthy run clears its claim via mark_job_run() long before the TTL; the
# TTL only recovers a claim left by a tick that DIED mid-run. HERMES_CRON_TIMEOUT
# is an *inactivity* limit, not a wall-clock cap — a job that keeps producing
# output legitimately runs past it — so the multiplier gives comfortable
# headroom over any healthy run before we treat a claim as stale.
_ONESHOT_RUN_CLAIM_TTL_HEADROOM = 3

_DEFAULT_CRON_INACTIVITY_TIMEOUT = 600.0


def _oneshot_run_claim_ttl_seconds() -> float:
    """Resolve the one-shot running-claim stale-recovery TTL.

    Derived from ``HERMES_CRON_TIMEOUT`` (the cron inactivity timeout the
    scheduler enforces on each run) so the safety valve tracks how long a run
    is actually allowed to go quiet, instead of a magic constant:

    - unset / invalid → default 600s inactivity limit → TTL = 1800s
    - ``0`` (unlimited runs) → no finite bound to derive from → fall back to
      ``ONESHOT_RUN_CLAIM_TTL_SECONDS``
    - positive N → ``max(N * headroom, ONESHOT_RUN_CLAIM_TTL_SECONDS)`` so a
      tiny configured timeout can never expire a claim mid-run.
    """
    raw = os.getenv("HERMES_CRON_TIMEOUT", "").strip()
    timeout = _DEFAULT_CRON_INACTIVITY_TIMEOUT
    if raw:
        try:
            timeout = float(raw)
        except (ValueError, TypeError):
            timeout = _DEFAULT_CRON_INACTIVITY_TIMEOUT
    if timeout <= 0:
        # Unlimited runs — cannot bound; use the fixed fallback floor.
        return float(ONESHOT_RUN_CLAIM_TTL_SECONDS)
    return max(
        timeout * _ONESHOT_RUN_CLAIM_TTL_HEADROOM,
        float(ONESHOT_RUN_CLAIM_TTL_SECONDS),
    )


def _job_running_in_this_process(job_id: str) -> bool:
    """Return True when the scheduler in THIS process is still running ``job_id``.

    Direct liveness signal for stale-entry recovery (#62002): the run_claim
    TTL alone cannot distinguish "the claiming tick died" from "the run is
    alive but slow" — a run stalled on network I/O (or a laptop that slept
    mid-run) legitimately outlives the TTL. The in-process ticker and the run
    share this process, so the scheduler's running set settles the common
    single-gateway case without any claim-age guesswork.

    Imported lazily: the scheduler imports this module at load, so a
    module-level import here would be circular.
    """
    try:
        from cron.scheduler import get_running_job_ids
        return job_id in get_running_job_ids()
    except Exception:
        logger.warning(
            "Cron running-set liveness check failed for job %r; keeping the "
            "entry to avoid deleting a possibly live one-shot run",
            job_id,
            exc_info=True,
        )
        return True


def _job_has_live_execution(job_id: str) -> bool:
    """True only when the execution ledger PROVES another run of ``job_id`` is alive.

    Layer 2 of the fire-claim admission gate. The claim TTL alone cannot tell a
    run that is legitimately slow from a tick that died mid-run: on 2026-08-24 a
    second fire of ``jobflow-matcher`` was admitted 1810.63s into a run whose
    ledger row was still non-terminal, purely because 1810 > 300. This is the
    cross-process, PID-recycle-safe signal that answers the question the clock
    cannot — the same ledger ``recover_interrupted_executions`` already trusts.

    Deliberately NOT ``cron.scheduler``'s ``_in_flight`` registry: that is a
    plain dict under a ``threading.Lock``, so it is blind to a sibling process,
    and the duplicate it failed to catch was two separate ``hermes cron run``
    invocations.

    Never raises, and never blocks on doubt: any failure — and any owner whose
    liveness cannot be established — answers False (admit). Degrading to the
    old clock-only behaviour is acceptable; wedging a job forever is not, and a
    raise on this path would cost the gateway its scheduler.

    Imported lazily for the same reason as ``_job_running_in_this_process``:
    ``cron.scheduler`` imports this module at load, so a module-level cron
    import here risks a cycle.
    """
    try:
        from cron.executions import live_execution_for_job

        live = live_execution_for_job(job_id)
        if live is None:
            return False
        # Name the blocker. A bare False here is indistinguishable from the
        # paused/disabled/missing refusals at the call site, and this is the
        # one refusal an operator will want to explain after the fact. Logged
        # rather than emitted: an event-bus write is a transaction against
        # another file and this runs under the cross-process _jobs_lock, the
        # same reason _emit_recovery_fire_triggers defers its emits.
        logger.warning(
            "Cron job %r: refusing a duplicate fire — execution %s is still "
            "running under live pid %s (claimed %s)",
            job_id, live.get("id"), live.get("pid"), live.get("claimed_at"),
        )
        return True
    except Exception:
        logger.warning(
            "Cron execution-ledger liveness check failed for job %r; admitting "
            "the fire on the claim TTL alone",
            job_id,
            exc_info=True,
        )
        return False


def _jobs_lock_file() -> Path:
    """Return the advisory lock path for the current cron directory."""
    return _current_cron_store().cron_dir / ".jobs.lock"


@contextlib.contextmanager
def _jobs_lock():
    """Serialize a load_jobs→modify→save_jobs critical section.

    Combines the in-process threading lock (cheap mutual exclusion between
    the gateway's parallel tick threads) with a cross-process advisory file
    lock on ``<cron dir>/.jobs.lock`` (mutual exclusion between the gateway process
    and standalone ``hermes`` CLI invocations, which previously shared no lock
    at all — a `cron pause` could be silently clobbered by a concurrent
    gateway write, leaving a "paused" job still firing).

    The flock is blocking, but every critical section that uses it is short
    (field updates only — no agent execution), so contention resolves in
    milliseconds. If neither fcntl nor msvcrt is available the manager still
    provides in-process locking, matching the historical behaviour.

    Nested calls in the same thread reuse the held lock so legacy callers that
    invoke save_jobs() inside a broader mutation section don't deadlock or try
    to reacquire the advisory file lock.
    """
    depth = getattr(_jobs_lock_state, "depth", 0)
    if depth:
        _jobs_lock_state.depth = depth + 1
        try:
            yield
        finally:
            _jobs_lock_state.depth -= 1
        return

    with _jobs_file_lock:
        _jobs_lock_state.depth = 1
        lock_fd = None
        try:
            try:
                ensure_dirs()
                lock_fd = open(_jobs_lock_file(), "a+", encoding="utf-8")
                lock_fd.seek(0)
                if fcntl is not None:
                    # Bounded acquisition (#60703): a plain blocking
                    # fcntl.flock(LOCK_EX) here has NO timeout, and it is
                    # taken while holding the process-wide _jobs_file_lock
                    # RLock above.  If another process wedges while holding
                    # .jobs.lock (e.g. an old gateway draining through a
                    # restart), a single blocked acquirer freezes EVERY cron
                    # function in this process — including the ticker's
                    # get_due_jobs() — silently and forever: the heartbeat
                    # file stops updating and all jobs stop firing with no
                    # error logged.  Poll LOCK_NB against a deadline instead;
                    # on timeout, log loudly and fall through to the same
                    # in-process-only degraded mode used when locking is
                    # unavailable.  A briefly-torn cross-process write is
                    # strictly better than a permanently dead scheduler.
                    _deadline = time.monotonic() + _JOBS_LOCK_TIMEOUT_SECONDS
                    while True:
                        try:
                            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except (OSError, IOError):
                            if time.monotonic() >= _deadline:
                                logger.error(
                                    "Timed out after %.0fs waiting for the cron "
                                    "jobs lock (%s) — another process is holding "
                                    "it. Proceeding with in-process locking only "
                                    "so the scheduler stays alive (#60703).",
                                    _JOBS_LOCK_TIMEOUT_SECONDS,
                                    _jobs_lock_file(),
                                )
                                try:
                                    lock_fd.close()
                                except OSError:
                                    pass
                                lock_fd = None
                                break
                            time.sleep(0.1)
                elif msvcrt is not None:
                    getattr(msvcrt, "locking")(lock_fd.fileno(), getattr(msvcrt, "LK_LOCK"), 1)
            except (OSError, IOError) as e:
                # Never let a locking failure take down cron writes — fall back to
                # in-process-only protection (still held via _jobs_file_lock).
                logger.warning("jobs.json cross-process lock unavailable (%s); "
                               "proceeding with in-process lock only", e)
            try:
                yield
            finally:
                if lock_fd is not None:
                    try:
                        if fcntl is not None:
                            fcntl.flock(lock_fd, fcntl.LOCK_UN)
                        elif msvcrt is not None:
                            getattr(msvcrt, "locking")(lock_fd.fileno(), getattr(msvcrt, "LK_UNLCK"), 1)
                    except (OSError, IOError):
                        pass
                    finally:
                        lock_fd.close()
        finally:
            _jobs_lock_state.depth = 0

# Fields on a cron job that must never change after creation. ``id`` is used
# as a filesystem path component under ``OUTPUT_DIR``; allowing it to be
# updated lets an unsafe value (``../escape``, absolute path, nested) leak
# into output writes/deletes.
_IMMUTABLE_JOB_FIELDS = frozenset({"id"})


def _job_output_dir(job_id: str) -> Path:
    """Resolve a job's output directory, rejecting any path-escape attempt.

    Job IDs are filesystem path components under ``OUTPUT_DIR``. A legacy or
    crafted ID containing ``..``, absolute paths, or nested separators would
    allow output writes/deletes to escape the cron output sandbox. Reject
    anything that isn't a single safe path component.
    """
    text = str(job_id or "").strip()
    if not text or text in {".", ".."} or "/" in text or "\\" in text:
        raise ValueError(f"Invalid cron job id for output path: {job_id!r}")
    if Path(text).is_absolute() or Path(text).drive:
        raise ValueError(f"Invalid cron job id for output path: {job_id!r}")
    return _current_cron_store().output_dir / text


def _normalize_skill_list(skill: Optional[str] = None, skills: Optional[Any] = None) -> List[str]:
    """Normalize legacy/single-skill and multi-skill inputs into a unique ordered list."""
    if skills is None:
        raw_items = [skill] if skill else []
    elif isinstance(skills, str):
        raw_items = [skills]
    else:
        raw_items = list(skills)

    normalized: List[str] = []
    for item in raw_items:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _apply_skill_fields(job: Dict[str, Any]) -> Dict[str, Any]:
    """Return a job dict with canonical `skills` and legacy `skill` fields aligned."""
    normalized = dict(job)
    skills = _normalize_skill_list(normalized.get("skill"), normalized.get("skills"))
    normalized["skills"] = skills
    normalized["skill"] = skills[0] if skills else None
    return normalized


def _coerce_job_text(value: Any, fallback: str = "") -> str:
    """Coerce legacy/hand-edited nullable cron fields to strings for readers."""
    if value is None:
        return fallback
    return str(value)


def _schedule_display_for_job(job: Dict[str, Any]) -> str:
    display = _coerce_job_text(job.get("schedule_display")).strip()
    if display:
        return display

    schedule = job.get("schedule")
    if isinstance(schedule, dict):
        for key in ("display", "value", "expr", "run_at"):
            text = _coerce_job_text(schedule.get(key)).strip()
            if text:
                return text
    elif schedule is not None:
        return str(schedule)

    return "?"


def _normalize_job_record(job: Dict[str, Any]) -> Dict[str, Any]:
    """Return a read-safe cron job shape for UI/API/tool/scheduler consumers.

    Older or hand-edited jobs can have nullable fields like ``prompt``,
    ``name``, or ``schedule_display``.  Keep storage untouched on read, but
    ensure consumers never crash while formatting or running those records.
    """
    normalized = _apply_skill_fields(job)
    job_id = _coerce_job_text(normalized.get("id"), "unknown")
    prompt = _coerce_job_text(normalized.get("prompt"))
    normalized["id"] = job_id
    normalized["prompt"] = prompt

    name = _coerce_job_text(normalized.get("name")).strip()
    if not name:
        script = _coerce_job_text(normalized.get("script")).strip()
        label_source = (
            prompt
            or (normalized["skills"][0] if normalized.get("skills") else "")
            or script
            or job_id
            or "cron job"
        )
        name = label_source[:50].strip() or "cron job"
    normalized["name"] = name
    normalized["schedule_display"] = _schedule_display_for_job(normalized)

    state = _coerce_job_text(normalized.get("state")).strip()
    if not state:
        state = "scheduled" if normalized.get("enabled", True) else "paused"
    normalized["state"] = state

    profile = _coerce_job_text(normalized.get("profile")).strip()
    normalized["profile"] = profile or None

    return normalized


def _secure_dir(path: Path):
    """Set directory to owner-only access (0700). No-op on Windows."""
    try:
        os.chmod(path, 0o700)
    except (OSError, NotImplementedError):
        pass  # Windows or other platforms where chmod is not supported


def _secure_file(path: Path):
    """Set file to owner-only read/write (0600). No-op on Windows."""
    try:
        if path.exists():
            os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


def ensure_dirs():
    """Ensure cron directories exist with secure permissions."""
    store = _current_cron_store()
    store.cron_dir.mkdir(parents=True, exist_ok=True)
    store.output_dir.mkdir(parents=True, exist_ok=True)
    _secure_dir(store.cron_dir)
    _secure_dir(store.output_dir)


# =============================================================================
# Schedule Parsing
# =============================================================================

def parse_duration(s: str) -> int:
    """
    Parse duration string into minutes.
    
    Examples:
        "30m" → 30
        "2h" → 120
        "1d" → 1440
    """
    s = s.strip().lower()
    match = re.match(r'^(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$', s)
    if not match:
        raise ValueError(f"Invalid duration: '{s}'. Use format like '30m', '2h', or '1d'")
    
    value = int(match.group(1))
    unit = match.group(2)[0]  # First char: m, h, or d
    
    multipliers = {'m': 1, 'h': 60, 'd': 1440}
    return value * multipliers[unit]


def parse_schedule(schedule: str) -> Dict[str, Any]:
    """
    Parse schedule string into structured format.
    
    Returns dict with:
        - kind: "once" | "interval" | "cron"
        - For "once": "run_at" (ISO timestamp)
        - For "interval": "minutes" (int)
        - For "cron": "expr" (cron expression)
    
    Examples:
        "30m"              → once in 30 minutes
        "2h"               → once in 2 hours
        "every 30m"        → recurring every 30 minutes
        "every 2h"         → recurring every 2 hours
        "0 9 * * *"        → cron expression
        "2026-02-03T14:00" → once at timestamp
    """
    schedule = schedule.strip()
    original = schedule
    schedule_lower = schedule.lower()
    
    # "every X" pattern → recurring interval
    if schedule_lower.startswith("every "):
        duration_str = schedule[6:].strip()
        minutes = parse_duration(duration_str)
        return {
            "kind": "interval",
            "minutes": minutes,
            "display": f"every {minutes}m"
        }
    
    # Check for cron expression (5 or 6 space-separated fields)
    # Cron fields: minute hour day month weekday [year]
    parts = schedule.split()
    if len(parts) >= 5 and all(
        re.match(r'^[\d\*\-,/]+$', p) for p in parts[:5]
    ):
        if not HAS_CRONITER:
            raise ValueError("Cron expressions require 'croniter' package. Install with: pip install croniter")
        # Validate cron expression
        try:
            croniter(schedule)
        except Exception as e:
            raise ValueError(f"Invalid cron expression '{schedule}': {e}")
        return {
            "kind": "cron",
            "expr": schedule,
            "display": schedule
        }
    
    # ISO timestamp (contains T or looks like date)
    if 'T' in schedule or re.match(r'^\d{4}-\d{2}-\d{2}', schedule):
        try:
            # Parse and validate
            dt = datetime.fromisoformat(schedule.replace('Z', '+00:00'))
            # Make naive timestamps timezone-aware at parse time so the stored
            # value doesn't depend on the system timezone matching at check time.
            #
            # Anchor to the CONFIGURED Hermes timezone, not the server's local
            # timezone. The due-check (`get_due_jobs`) compares `next_run_at`
            # against `hermes_time.now()`, which uses the configured zone. If a
            # naive "20:07" were interpreted as server-local (e.g. UTC) while
            # now() runs in Asia/Kolkata, the stored instant would land hours
            # off from the user's wall-clock intent — far enough that one-shots
            # never become due and recurring jobs fire at the wrong time. Using
            # the configured zone makes "20:07" mean 20:07 on the same clock the
            # scheduler checks against (#51021).
            if dt.tzinfo is None:
                hermes_tz = _hermes_now().tzinfo
                dt = dt.replace(tzinfo=hermes_tz)
            return {
                "kind": "once",
                "run_at": dt.isoformat(),
                "display": f"once at {dt.strftime('%Y-%m-%d %H:%M')}"
            }
        except ValueError as e:
            raise ValueError(f"Invalid timestamp '{schedule}': {e}")
    
    # Duration like "30m", "2h", "1d" → one-shot from now
    try:
        minutes = parse_duration(schedule)
        run_at = _hermes_now() + timedelta(minutes=minutes)
        return {
            "kind": "once",
            "run_at": run_at.isoformat(),
            "display": f"once in {original}"
        }
    except ValueError:
        pass
    
    raise ValueError(
        f"Invalid schedule '{original}'. Use:\n"
        f"  - Duration: '30m', '2h', '1d' (one-shot)\n"
        f"  - Interval: 'every 30m', 'every 2h' (recurring)\n"
        f"  - Cron: '0 9 * * *' (cron expression)\n"
        f"  - Timestamp: '2026-02-03T14:00:00' (one-shot at time)"
    )


def _ensure_aware(dt: datetime) -> datetime:
    """Return a timezone-aware datetime in Hermes configured timezone.

    Backward compatibility:
    - Older stored timestamps may be naive.
    - Naive values are interpreted as *system-local wall time* (the timezone
      `datetime.now()` used when they were created), then converted to the
      configured Hermes timezone.

    This preserves relative ordering for legacy naive timestamps across
    timezone changes and avoids false not-due results.
    """
    target_tz = _hermes_now().tzinfo
    if dt.tzinfo is None:
        local_tz = datetime.now().astimezone().tzinfo
        return dt.replace(tzinfo=local_tz).astimezone(target_tz)
    return dt.astimezone(target_tz)


def _timezone_offset_mismatch(stored: datetime, current: datetime) -> bool:
    """Return True when a stored aware timestamp uses a different UTC offset.

    Naive stored timestamps return False: they carry no offset to compare, and
    are normalized by ``_ensure_aware`` instead — they intentionally never take
    the offset-repair path.
    """
    if stored.tzinfo is None or current.tzinfo is None:
        return False
    return stored.utcoffset() != current.utcoffset()


def _stored_wall_clock_is_future(stored: datetime, current: datetime) -> bool:
    """Return True when the stored local wall-clock time has not arrived yet.

    Cron schedules express local wall-clock intent. If Hermes/system local time
    changes after next_run_at was persisted, an old offset can make a future
    wall-clock run look due at the converted absolute time (for example
    21:00+10 becomes 13:00+02). Comparing naive wall-clock values lets us
    distinguish that migration case from a genuinely missed run whose scheduled
    wall time has already passed.
    """
    return stored.replace(tzinfo=None) > current.replace(tzinfo=None)


def _recoverable_oneshot_run_at(
    schedule: Dict[str, Any],
    now: datetime,
    *,
    last_run_at: Optional[str] = None,
) -> Optional[str]:
    """Return a one-shot run time if it is still eligible to fire.

    One-shot jobs get a small grace window so jobs created a few seconds after
    their requested minute still run on the next tick. Once a one-shot has
    already run, it is never eligible again.
    """
    if not isinstance(schedule, dict) or schedule.get("kind") != "once":
        return None
    if last_run_at:
        return None

    run_at = schedule.get("run_at")
    if not run_at:
        return None

    try:
        run_at_dt = _ensure_aware(datetime.fromisoformat(run_at))
    except Exception:
        return None
    if run_at_dt >= now - timedelta(seconds=ONESHOT_GRACE_SECONDS):
        return run_at
    return None


def _compute_grace_seconds(schedule: dict) -> int:
    """Compute how late a job can be and still catch up instead of fast-forwarding.

    Uses half the schedule period, clamped between 120 seconds and 2 hours.
    This ensures daily jobs can catch up if missed by up to 2 hours,
    while frequent jobs (every 5-10 min) still fast-forward quickly.
    """
    MIN_GRACE = 120
    MAX_GRACE = 7200  # 2 hours

    kind = schedule.get("kind")

    if kind == "interval":
        period_seconds = schedule.get("minutes", 1) * 60
        grace = period_seconds // 2
        return max(MIN_GRACE, min(grace, MAX_GRACE))

    if kind == "cron" and HAS_CRONITER:
        expr = schedule.get("expr")
        if expr:
            try:
                now = _hermes_now()
                cron = croniter(expr, now)
                first = cron.get_next(datetime)
                second = cron.get_next(datetime)
                period_seconds = int((second - first).total_seconds())
                grace = period_seconds // 2
                return max(MIN_GRACE, min(grace, MAX_GRACE))
            except Exception as exc:
                logger.warning(
                    "Failed to compute grace for cron expr=%r: %s",
                    expr,
                    exc,
                )

    return MIN_GRACE


def _compute_period_seconds(schedule: dict) -> Optional[int]:
    """Compute the schedule's period in seconds.

    Mirrors the period extraction inside _compute_grace_seconds but returns
    the raw period (not period/2) and does not clamp. Used by
    get_due_and_skipped_jobs() to decide fire-once-on-recovery eligibility:
    a daily cron has period=86400, weekly has 604800, etc.

    For irregular crons (e.g. "0 8,13,18 * * *" with 5h/5h/14h intervals),
    returns the SMALLEST interval — this matches the conservative semantics
    used by get_due_and_skipped_jobs() (a job is "daily-or-shorter" iff its
    fastest cadence is <= 24h).

    Returns None for kind="once" (no period) or invalid schedules.
    """
    kind = schedule.get("kind")

    if kind == "interval":
        minutes = schedule.get("minutes", 1)
        return int(minutes) * 60

    if kind == "cron" and HAS_CRONITER:
        try:
            now = _hermes_now()
            cron = croniter(schedule["expr"], now)
            # Sample enough successive fires to cover all distinct intervals
            # in a typical recurrence pattern (24 fires covers a daily-pattern
            # cron; weekly+ patterns will surface their period in the first
            # interval). Take the minimum to make the result independent of
            # wall-clock time-of-day.
            fires = [cron.get_next(datetime) for _ in range(25)]
            intervals = [
                int((fires[i + 1] - fires[i]).total_seconds())
                for i in range(len(fires) - 1)
            ]
            return min(intervals) if intervals else None
        except Exception as exc:
            logger.warning(
                "Failed to compute period for cron expr=%r: %s",
                schedule.get("expr"),
                exc,
            )
            return None

    return None


def compute_next_run(schedule: Dict[str, Any], last_run_at: Optional[str] = None) -> Optional[str]:
    """
    Compute the next run time for a schedule.

    Returns ISO timestamp string, or None if no more runs.
    """
    now = _hermes_now()

    if not isinstance(schedule, dict):
        return None
    kind = schedule.get("kind")
    if kind is None:
        return None

    if kind == "once":
        return _recoverable_oneshot_run_at(schedule, now, last_run_at=last_run_at)

    elif kind == "interval":
        minutes = schedule.get("minutes")
        if minutes is None:
            return None
        if last_run_at:
            try:
                last = _ensure_aware(datetime.fromisoformat(last_run_at))
                next_run = last + timedelta(minutes=minutes)
            except Exception:
                next_run = now + timedelta(minutes=minutes)
        else:
            # First run is now + interval
            next_run = now + timedelta(minutes=minutes)
        return next_run.isoformat()

    elif kind == "cron":
        expr = schedule.get("expr")
        if not expr:
            return None
        if not HAS_CRONITER:
            logger.warning(
                "Cannot compute next run for cron schedule %r: 'croniter' is "
                "not installed. croniter is a core dependency as of v0.9.x; "
                "reinstall hermes-agent or run 'pip install croniter' in your "
                "runtime env.",
                expr,
            )
            return None
        # Use last_run_at as the croniter base when available, consistent
        # with interval jobs.  This ensures that after a crash/restart,
        # the next run is anchored to the actual last execution time
        # rather than to an arbitrary restart time.
        base_time = now
        if last_run_at:
            try:
                base_time = _ensure_aware(datetime.fromisoformat(last_run_at))
            except Exception:
                base_time = now
        cron = croniter(expr, base_time)
        next_run = cron.get_next(datetime)
        return next_run.isoformat()

    return None


# =============================================================================
# Ticker heartbeat (liveness signal for `hermes cron status`)
# =============================================================================

def _atomic_write_epoch(path: Path) -> None:
    """Atomically write the current epoch time to ``path``.

    Uses the same tmpfile + ``atomic_replace`` pattern as ``save_jobs`` so a
    concurrent reader in another process (``hermes cron status``) never sees a
    torn/truncated file. Best-effort: failures are swallowed by callers.
    """
    ensure_dirs()
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".hb_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def record_ticker_heartbeat(success: bool = False) -> None:
    """Record a ticker liveness signal, and optionally a successful-tick signal.

    The ticker calls this once per loop iteration. ``success=True`` additionally
    bumps the *last successful tick* marker. We track two distinct signals so
    `hermes cron status` can tell a thread that is merely *alive and looping*
    (heartbeat fresh, success stale) from one that is actually *firing jobs*
    (both fresh) — a ticker stuck failing every tick would otherwise keep the
    plain heartbeat fresh and falsely report healthy (#32612, #32895).

    Best-effort: a write failure must never disrupt the tick loop.
    """
    try:
        _atomic_write_epoch(_get_ticker_heartbeat_file())
    except Exception:
        pass
    if success:
        try:
            _atomic_write_epoch(_get_ticker_success_file())
        except Exception:
            pass


def _epoch_file_age(path: Path) -> Optional[float]:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return max(0.0, time.time() - float(raw))
    except Exception:
        return None


def get_ticker_heartbeat_age() -> Optional[float]:
    """Seconds since the ticker loop last iterated, or None if unknown.

    None = heartbeat file missing/unreadable (older build, never ran, or a
    torn read). Callers treat None as "cannot determine", not "dead".
    """
    return _epoch_file_age(_get_ticker_heartbeat_file())


def get_ticker_success_age() -> Optional[float]:
    """Seconds since the ticker last completed a tick WITHOUT raising, or None."""
    return _epoch_file_age(_get_ticker_success_file())


# =============================================================================
# Job CRUD Operations
# =============================================================================

def load_jobs() -> List[Dict[str, Any]]:
    """Load all jobs from storage."""
    jobs_file = _current_cron_store().jobs_file
    ensure_dirs()
    if not jobs_file.exists():
        return []

    _strict_retry = False  # track whether we used the strict=False fallback

    try:
        # utf-8-sig: Windows Notepad / PowerShell 5.1 Set-Content -Encoding UTF8
        # write a leading BOM; json.load under plain utf-8 raises
        # JSONDecodeError("Unexpected UTF-8 BOM") and takes down cron.
        with open(jobs_file, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
    except json.JSONDecodeError:
        # Retry with strict=False to handle bare control chars in string values
        _strict_retry = True
        try:
            with open(jobs_file, 'r', encoding='utf-8-sig') as f:
                data = json.loads(f.read(), strict=False)
        except Exception as e:
            logger.error("Failed to auto-repair jobs.json: %s", e)
            raise RuntimeError(f"Cron database corrupted and unrepairable: {e}") from e
    except IOError as e:
        logger.error("IOError reading jobs.json: %s", e)
        raise RuntimeError(f"Failed to read cron database: {e}") from e

    # Validate the top-level JSON shape: accept a dict (expected) or a bare
    # list (auto-repair). Anything else (str/number/null) is corruption that
    # would otherwise raise an uncaught AttributeError on ``.get()`` and take
    # down the whole cron subsystem.
    if isinstance(data, dict):
        jobs = data.get("jobs", [])
        if _strict_retry and jobs:
            # Hit control-character corruption — rewrite with proper escaping.
            save_jobs(jobs)
            logger.warning("Auto-repaired jobs.json (had invalid control characters)")
        return jobs
    if isinstance(data, list):
        # Bare array — likely saved/edited outside save_jobs(). Wrap it back
        # into the expected {"jobs": [...]} structure.
        if data:
            save_jobs(data)
            logger.warning("Auto-repaired jobs.json (bare list wrapped as dict)")
        return data

    raise RuntimeError(
        f"Cron database corrupted: expected {{'jobs': [...]}}, got {type(data).__name__}"
    )


def _save_jobs_unlocked(jobs: List[Dict[str, Any]]):
    """Save all jobs to storage. Caller must hold _jobs_lock()."""
    jobs_file = _current_cron_store().jobs_file
    ensure_dirs()
    fd, tmp_path = tempfile.mkstemp(dir=str(jobs_file.parent), suffix='.tmp', prefix='.jobs_')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({"jobs": jobs, "updated_at": _hermes_now().isoformat()}, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp_path, jobs_file)
        _secure_file(jobs_file)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def save_jobs(jobs: List[Dict[str, Any]]):
    """Save all jobs to storage."""
    with _jobs_lock():
        _save_jobs_unlocked(jobs)


def _canonical_job_rows_digest(rows: List[Dict[str, Any]]) -> str:
    """Return the canonical SHA256 digest used by exact-row scheduler CAS."""
    try:
        raw = json.dumps(
            rows,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("scheduler rows must be finite canonical JSON") from exc
    return hashlib.sha256(raw).hexdigest()


def _validate_unique_job_names(names: Union[List[str], Tuple[str, ...]]) -> Tuple[str, ...]:
    if not isinstance(names, (list, tuple)):
        raise ValueError("job names must be a list or tuple")
    normalized = tuple(names)
    if any(not isinstance(name, str) or not name for name in normalized):
        raise ValueError("job names must be non-empty strings")
    if len(normalized) != len(set(normalized)):
        raise ValueError("job names must be unique")
    return normalized


def _exact_rows_by_name(
    all_jobs: List[Dict[str, Any]], names: Union[List[str], Tuple[str, ...]]
) -> List[Dict[str, Any]]:
    """Select exact durable rows, requiring one and only one row per name."""
    requested = _validate_unique_job_names(names)
    selected: List[Dict[str, Any]] = []
    for name in requested:
        matches = [row for row in all_jobs if row.get("name") == name]
        if len(matches) != 1:
            raise ValueError(
                f"scheduler job name {name!r} resolved to {len(matches)} rows; exactly one required"
            )
        selected.append(copy.deepcopy(matches[0]))
    return sorted(selected, key=lambda row: row["name"])


def snapshot_jobs_by_name(names: Tuple[str, ...]) -> List[Dict[str, Any]]:
    """Return exact full durable rows, sorted by name, one per unique name."""
    if not isinstance(names, tuple):
        raise ValueError("snapshot job names must be a tuple")
    return _exact_rows_by_name(load_jobs(), names)


def _require_dispatch_barrier(barrier: Any) -> Dict[str, Any]:
    """Require the exact retained production barrier capability, not a proof dict."""
    from jobflow_dispatch.quarantine_control import DispatchBarrier

    if not isinstance(barrier, DispatchBarrier):
        raise RuntimeError("exact retained DispatchBarrier capability is required")
    proof = barrier.assert_held()
    if (
        proof.get("schema_version") != 1
        or proof.get("complete") is not True
        or proof.get("barrier_token") != barrier.token
        or proof.get("coverage") != "due_row_capture_through_submission"
    ):
        raise RuntimeError("dispatch barrier proof is incomplete or invalid")
    return proof


def pause_jobs_cas(
    names: List[str],
    expected_digest: str,
    *,
    reason: str,
    dispatch_barrier: Any,
) -> Dict[str, Any]:
    """Atomically pause eligible exact rows under a retained dispatch barrier."""
    barrier_proof = _require_dispatch_barrier(dispatch_barrier)
    requested = _validate_unique_job_names(names)
    if not isinstance(names, list):
        raise ValueError("pause job names must be a list")
    if not isinstance(expected_digest, str) or len(expected_digest) != 64:
        raise ValueError("expected scheduler digest must be a SHA256 hex string")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("pause reason must be a non-empty string")

    with _jobs_lock():
        all_jobs = load_jobs()
        before = _exact_rows_by_name(all_jobs, requested)
        observed_digest = _canonical_job_rows_digest(before)
        if observed_digest != expected_digest:
            raise ValueError("scheduler prestate digest CAS mismatch")

        paused_at = _hermes_now().isoformat()
        changed_ids: List[str] = []
        selected_names = set(requested)
        for row in all_jobs:
            if row.get("name") not in selected_names:
                continue
            if row.get("enabled") is True or row.get("state") == "scheduled":
                row["enabled"] = False
                row["state"] = "paused"
                row["paused"] = True
                row["paused_at"] = paused_at
                row["paused_reason"] = reason
                changed_ids.append(str(row["id"]))

        after = _exact_rows_by_name(all_jobs, requested)
        _save_jobs_unlocked(all_jobs)
        durable = _exact_rows_by_name(load_jobs(), requested)
        if durable != after:
            raise RuntimeError("scheduler pause durable exact readback mismatch")

        return {
            "schema_version": 1,
            "complete": True,
            "source": "cron.jobs",
            "control_transaction_id": uuid.uuid4().hex,
            "dispatch_barrier": barrier_proof,
            "pause_reason": reason,
            "before_rows": before,
            "after_rows": after,
            "changed_job_ids": sorted(changed_ids),
            "digest_proof": {
                "algorithm": "sha256",
                "expected_before": expected_digest,
                "observed_before": observed_digest,
                "after": _canonical_job_rows_digest(after),
                "durable_readback": _canonical_job_rows_digest(durable),
            },
        }


def restore_jobs_cas(
    *,
    expected_paused_rows: List[Dict[str, Any]],
    target_rows: List[Dict[str, Any]],
    dependency_order: List[str],
    dispatch_barrier: Any,
) -> Dict[str, Any]:
    """Atomically restore exact rows under the retained dispatch barrier."""
    barrier_proof = _require_dispatch_barrier(dispatch_barrier)
    if not isinstance(expected_paused_rows, list) or not expected_paused_rows:
        raise ValueError("expected paused rows must be a non-empty list")
    if not isinstance(target_rows, list) or not target_rows:
        raise ValueError("restore target rows must be a non-empty list")
    if any(not isinstance(row, dict) for row in expected_paused_rows + target_rows):
        raise ValueError("scheduler CAS rows must be objects")

    expected = sorted(copy.deepcopy(expected_paused_rows), key=lambda row: row.get("name", ""))
    expected_names = _validate_unique_job_names([row.get("name") for row in expected])
    target = sorted(copy.deepcopy(target_rows), key=lambda row: row.get("name", ""))
    target_names = _validate_unique_job_names([row.get("name") for row in target])
    order = _validate_unique_job_names(dependency_order)
    if not isinstance(dependency_order, list):
        raise ValueError("dependency order must be a list")
    if set(order) != set(target_names):
        raise ValueError("dependency order must name every restore target exactly once")
    if "jobflow-matcher" in order and order[-1] != "jobflow-matcher":
        raise ValueError("jobflow-matcher must be restored last")

    expected_by_name = {row["name"]: row for row in expected}
    target_by_name = {row["name"]: row for row in target}
    for name, row in target_by_name.items():
        paused = expected_by_name.get(name)
        if paused is None or str(paused.get("id")) != str(row.get("id")):
            raise ValueError("restore target IDs/names do not match expected paused rows")
        allowed = {"enabled", "state", "paused", "paused_at", "paused_reason"}
        changed = {
            key for key in set(paused) | set(row)
            if paused.get(key) != row.get(key)
        }
        if changed - allowed:
            raise ValueError("restore target differs outside containment fields")

    with _jobs_lock():
        all_jobs = load_jobs()
        observed = _exact_rows_by_name(all_jobs, expected_names)
        if observed != expected:
            raise ValueError("scheduler current-row CAS mismatch")

        index_by_name = {row.get("name"): index for index, row in enumerate(all_jobs)}
        restored_ids: List[str] = []
        for name in order:
            replacement = copy.deepcopy(target_by_name[name])
            all_jobs[index_by_name[name]] = replacement
            restored_ids.append(str(replacement["id"]))

        _save_jobs_unlocked(all_jobs)
        durable_scope = _exact_rows_by_name(load_jobs(), expected_names)
        expected_scope = sorted(
            [
                copy.deepcopy(target_by_name.get(row["name"], row))
                for row in expected
            ],
            key=lambda row: row["name"],
        )
        if durable_scope != expected_scope:
            raise RuntimeError("scheduler restore durable exact readback mismatch")
        durable_targets = [
            copy.deepcopy(row) for row in durable_scope if row["name"] in set(target_names)
        ]

        return {
            "schema_version": 1,
            "complete": True,
            "source": "cron.jobs",
            "control_transaction_id": uuid.uuid4().hex,
            "dispatch_barrier": barrier_proof,
            "restored_job_ids": restored_ids,
            "before_rows": observed,
            "after_rows": durable_targets,
            "durable_rows": durable_scope,
            "digest_proof": {
                "algorithm": "sha256",
                "expected_paused": _canonical_job_rows_digest(expected),
                "observed_before": _canonical_job_rows_digest(observed),
                "target": _canonical_job_rows_digest(target),
                "durable_readback": _canonical_job_rows_digest(durable_scope),
            },
        }


def _normalize_workdir(workdir: Optional[str]) -> Optional[str]:
    """Normalize and validate a cron job workdir.

    Rules:
      - Empty / None → None (feature off, preserves old behaviour).
      - ``~`` is expanded.  Relative paths are rejected — cron jobs run detached
        from any shell cwd, so relative paths have no stable meaning.
      - The path must exist and be a directory at create/update time.  We do
        NOT re-check at run time (a user might briefly unmount the dir; the
        scheduler will just fall back to old behaviour with a logged warning).

    Returns the absolute path string, or None when disabled.
    Raises ValueError on invalid input.
    """
    if workdir is None:
        return None
    raw = str(workdir).strip()
    if not raw:
        return None
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        raise ValueError(
            f"Cron workdir must be an absolute path (got {raw!r}). "
            f"Cron jobs run detached from any shell cwd, so relative paths are ambiguous."
        )
    resolved = expanded.resolve()
    if not resolved.exists():
        raise ValueError(f"Cron workdir does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"Cron workdir is not a directory: {resolved}")
    return str(resolved)


def _resolve_default_model_snapshot() -> Optional[str]:
    """Resolve the global default model the same way the cron ticker does.

    Mirrors the unpinned-model resolution in ``cron/scheduler.py`` ``run_job``:
    read ``config.yaml`` ``model.default`` (or the ``model`` alias / bare string
    form), applying the managed-scope overlay and env expansion. Used by
    ``create_job`` to snapshot the default model for unpinned jobs so a later
    swap of the global default is detected at fire time (#44585).

    Returns the resolved model string, or ``None`` if config is missing/empty
    or resolution fails (fail-open — caller treats ``None`` as "no snapshot").
    """
    try:
        import yaml
        from hermes_cli.config import _expand_env_vars

        cfg_path = get_hermes_home() / "config.yaml"
        if not cfg_path.exists():
            return None
        with cfg_path.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        try:
            from hermes_cli import managed_scope
            cfg = managed_scope.apply_managed_overlay(cfg)
        except Exception:
            pass
        cfg = _expand_env_vars(cfg)
        model_cfg = cfg.get("model") or {}
        if isinstance(model_cfg, str):
            return model_cfg.strip() or None
        if isinstance(model_cfg, dict):
            default = model_cfg.get("default") or model_cfg.get("model")
            if isinstance(default, str):
                return default.strip() or None
        return None
    except Exception:
        return None


def _normalize_job_optional_text(value: Any, *, strip_trailing_slash: bool = False) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if strip_trailing_slash:
        text = text.rstrip("/")
    return text or None


def _normalize_profile(profile: Optional[str]) -> Optional[str]:
    """Normalize and validate an optional cron job profile name.

    Empty / None disables per-job profile selection. Otherwise the profile name
    is canonicalized with the same rules as ``hermes -p`` and must refer to an
    existing profile at create/update time. ``default`` is the built-in root
    profile and is always valid.
    """
    if profile is None:
        return None
    raw = str(profile).strip()
    if not raw:
        return None

    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env

    normalized = normalize_profile_name(raw)
    # resolve_profile_env validates the canonical name and checks that named
    # profiles exist. Store only the stable profile id, not the filesystem path,
    # so profile directories can move with the Hermes root.
    resolve_profile_env(normalized)
    return normalized


def _compute_provider_model_snapshots(
    *,
    provider: Any,
    model: Any,
    base_url: Any,
    no_agent: Any,
) -> Tuple[Optional[str], Optional[str]]:
    """Snapshot unpinned inference axes for the provider/model drift guard.

    Agent cron jobs with unpinned provider/model follow global config at fire
    time. Capture the current resolution for each unpinned axis so a later
    global switch fails closed instead of silently changing spend. Pinned axes
    and no-agent script jobs intentionally carry no snapshot.
    """
    normalized_provider = _normalize_job_optional_text(provider)
    normalized_model = _normalize_job_optional_text(model)
    normalized_base_url = _normalize_job_optional_text(
        base_url,
        strip_trailing_slash=True,
    )
    if bool(no_agent):
        return None, None

    provider_snapshot: Optional[str] = None
    model_snapshot: Optional[str] = None
    if normalized_provider is None:
        try:
            from hermes_cli.runtime_provider import resolve_runtime_provider

            runtime_kwargs = {"requested": None}
            if normalized_base_url:
                runtime_kwargs["explicit_base_url"] = normalized_base_url
            snap = resolve_runtime_provider(**runtime_kwargs)
            snap_provider = str(snap.get("provider") or "").strip().lower()
            provider_snapshot = snap_provider or None
        except Exception:
            provider_snapshot = None
    if normalized_model is None:
        try:
            model_snapshot = _resolve_default_model_snapshot() or None
        except Exception:
            model_snapshot = None
    return provider_snapshot, model_snapshot


def _normalized_inference_axes(job: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str], bool]:
    """Return the stored inference-routing fields in their semantic form."""
    return (
        _normalize_job_optional_text(job.get("provider")),
        _normalize_job_optional_text(job.get("model")),
        _normalize_job_optional_text(job.get("base_url"), strip_trailing_slash=True),
        bool(job.get("no_agent")),
    )


def create_job(
    prompt: Optional[str],
    schedule: str,
    name: Optional[str] = None,
    repeat: Optional[int] = None,
    deliver: Optional[str] = None,
    origin: Optional[Dict[str, Any]] = None,
    skill: Optional[str] = None,
    skills: Optional[List[str]] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    script: Optional[str] = None,
    context_from: Optional[Union[str, List[str]]] = None,
    enabled_toolsets: Optional[List[str]] = None,
    workdir: Optional[str] = None,
    profile: Optional[str] = None,
    no_agent: bool = False,
    attach_to_session: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Create a new cron job.

    Args:
        prompt: The prompt to run (must be self-contained, or a task instruction when skill is set).
                Ignored when ``no_agent=True`` except as an optional name hint.
        schedule: Schedule string (see parse_schedule)
        name: Optional friendly name
        repeat: How many times to run (None = forever, 1 = once)
        deliver: Where to deliver output ("origin", "local", "telegram", etc.)
        origin: Source info where job was created (for "origin" delivery)
        skill: Optional legacy single skill name to load before running the prompt
        skills: Optional ordered list of skills to load before running the prompt
        model: Optional per-job model override
        provider: Optional per-job provider override
        base_url: Optional per-job base URL override
        script: Optional path to a script whose stdout feeds the job. With
                ``no_agent=True`` the script IS the job — its stdout is
                delivered verbatim. Without ``no_agent``, its stdout is
                injected into the agent's prompt as context (data-collection /
                change-detection pattern). Paths resolve under
                ~/.hermes/scripts/; ``.sh`` / ``.bash`` files run via bash,
                anything else via Python.
        context_from: Optional job ID (or list of job IDs) whose most recent output
                      is injected into the prompt as context before each run.
                      Useful for chaining cron jobs: job A finds data, job B processes it.
        enabled_toolsets: Optional list of toolset names to restrict the agent to.
                          When set, only tools from these toolsets are loaded, reducing
                          token overhead. When omitted, all default tools are loaded.
                          Ignored when ``no_agent=True``.
        workdir: Optional absolute path.  When set, the job runs as if launched
                from that directory: AGENTS.md / CLAUDE.md / .cursorrules from
                that directory are injected into the system prompt, and the
                terminal/file/code_exec tools use it as their working directory
                (via TERMINAL_CWD).  When unset, the old behaviour is preserved
                (no context files injected, tools use the scheduler's cwd).
                With ``no_agent=True``, ``workdir`` is still applied as the
                script's cwd so relative paths inside the script behave
                predictably.
        profile: Optional Hermes profile name. When set, the job runs with
                that profile's HERMES_HOME so profile-specific config,
                credentials, scripts, skills, and memory paths resolve
                consistently. ``default`` selects the root profile; empty /
                None preserves the scheduler's existing behaviour.
        no_agent: When True, skip the agent entirely — run ``script`` on schedule
                and deliver its stdout directly. Empty stdout = silent (no
                delivery). Requires ``script`` to be set. Ideal for classic
                watchdogs and periodic alerts that don't need LLM reasoning.

    Returns:
        The created job dict
    """
    parsed_schedule = parse_schedule(schedule)

    # Normalize repeat: treat 0 or negative values as None (infinite)
    if repeat is not None and repeat <= 0:
        repeat = None

    # Auto-set repeat=1 for one-shot schedules if not specified
    if parsed_schedule["kind"] == "once" and repeat is None:
        repeat = 1

    # Default delivery to origin if available, otherwise local
    if deliver is None:
        deliver = "origin" if origin else "local"

    job_id = uuid.uuid4().hex[:12]
    now = _hermes_now().isoformat()

    normalized_skills = _normalize_skill_list(skill, skills)
    normalized_model = _normalize_job_optional_text(model)
    normalized_provider = _normalize_job_optional_text(provider)
    normalized_base_url = _normalize_job_optional_text(base_url, strip_trailing_slash=True)
    normalized_script = str(script).strip() if isinstance(script, str) else None
    normalized_script = normalized_script or None
    normalized_toolsets = [str(t).strip() for t in enabled_toolsets if str(t).strip()] if enabled_toolsets else None
    normalized_toolsets = normalized_toolsets or None
    normalized_workdir = _normalize_workdir(workdir)
    normalized_profile = _normalize_profile(profile)
    normalized_no_agent = bool(no_agent)
    normalized_attach = attach_to_session if isinstance(attach_to_session, bool) else None

    # no_agent jobs are meaningless without a script — the script IS the job.
    # Surface this as a clear ValueError at create time so bad configs never
    # reach the scheduler.
    if normalized_no_agent and not normalized_script:
        raise ValueError(
            "no_agent=True requires a script — with no agent and no script "
            "there is nothing for the job to run."
        )

    # Normalize context_from: accept str or list of str, store as list or None
    if isinstance(context_from, str):
        context_from = [context_from.strip()] if context_from.strip() else None
    elif isinstance(context_from, list):
        context_from = [str(j).strip() for j in context_from if str(j).strip()] or None
    else:
        context_from = None

    prompt_text = _coerce_job_text(prompt)

    # Reject cron jobs that schedule gateway-lifecycle commands. Prevents
    # agent-driven SIGTERM-respawn loops under launchd/systemd KeepAlive
    # (#30719). Enforced here (not only in the CLI layer) so the agent's
    # `cronjob` model tool — which calls create_job directly — is also
    # covered, not just `hermes cron create`.
    from cron.lifecycle_guard import check_gateway_lifecycle
    check_gateway_lifecycle(prompt_text, normalized_script)

    label_source = (prompt_text or (normalized_skills[0] if normalized_skills else None) or (normalized_script if normalized_no_agent else None)) or "cron job"

    provider_snapshot, model_snapshot = _compute_provider_model_snapshots(
        provider=normalized_provider,
        model=normalized_model,
        base_url=normalized_base_url,
        no_agent=normalized_no_agent,
    )

    next_run_at = compute_next_run(parsed_schedule)
    if parsed_schedule.get("kind") == "once" and next_run_at is None:
        run_at = parsed_schedule.get("run_at") or schedule
        logger.warning(
            "Rejecting one-shot cron job '%s': run_at %s is outside the %ss grace window",
            name or label_source[:50].strip(),
            run_at,
            ONESHOT_GRACE_SECONDS,
        )
        raise ValueError(
            f"Requested one-shot time {run_at} is more than "
            f"{ONESHOT_GRACE_SECONDS}s in the past and cannot be scheduled."
        )

    job = {
        "id": job_id,
        "name": name or label_source[:50].strip(),
        "prompt": prompt_text,
        "skills": normalized_skills,
        "skill": normalized_skills[0] if normalized_skills else None,
        "model": normalized_model,
        "provider": normalized_provider,
        # Provider/model resolution captured at creation for unpinned jobs
        # (#44585). None for pinned axes, no_agent jobs, resolution failures, and
        # any pre-existing job written before these fields existed (back-compat).
        "provider_snapshot": provider_snapshot,
        "model_snapshot": model_snapshot,
        "base_url": normalized_base_url,
        "script": normalized_script,
        "no_agent": normalized_no_agent,
        "context_from": context_from,
        "schedule": parsed_schedule,
        "schedule_display": parsed_schedule.get("display", schedule),
        "repeat": {
            "times": repeat,  # None = forever
            "completed": 0
        },
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
        "paused_reason": None,
        "created_at": now,
        "next_run_at": next_run_at,
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
        "last_delivery_error": None,
        # Delivery configuration
        "deliver": deliver,
        "origin": origin,  # Tracks where job was created for "origin" delivery
        "enabled_toolsets": normalized_toolsets,
        "workdir": normalized_workdir,
        "profile": normalized_profile,
    }
    # Only persist attach_to_session when explicitly set, so existing jobs and
    # the common case stay byte-identical (absent key => fall back to the
    # global cron.mirror_delivery config, default off).
    if normalized_attach is not None:
        job["attach_to_session"] = normalized_attach

    with _jobs_lock():
        jobs = load_jobs()
        jobs.append(job)
        save_jobs(jobs)

    return job


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Get a job by ID."""
    jobs = load_jobs()
    for job in jobs:
        if job["id"] == job_id:
            return _normalize_job_record(job)
    return None


class AmbiguousJobReference(LookupError):
    """Raised when a job name matches more than one job."""

    def __init__(self, ref: str, matches: List[Dict[str, Any]]):
        self.ref = ref
        self.matches = matches
        ids = ", ".join(m["id"] for m in matches)
        super().__init__(
            f"Job name '{ref}' is ambiguous — matches {len(matches)} jobs: {ids}. "
            f"Use the job ID instead."
        )


def resolve_job_ref(ref: str) -> Optional[Dict[str, Any]]:
    """Resolve a job reference (ID or name) to a job record.

    - Exact ID match wins (works even if a different job's name equals this ID).
    - Otherwise, case-insensitive name match.
    - If a name matches more than one job, raises AmbiguousJobReference so the
      caller can surface the matching IDs rather than silently picking one.
    """
    if not ref:
        return None
    jobs = load_jobs()
    for job in jobs:
        if job["id"] == ref:
            return _normalize_job_record(job)
    ref_lower = ref.lower()
    name_matches = [j for j in jobs if (j.get("name") or "").lower() == ref_lower]
    if not name_matches:
        return None
    if len(name_matches) > 1:
        raise AmbiguousJobReference(
            ref, [_normalize_job_record(j) for j in name_matches]
        )
    return _normalize_job_record(name_matches[0])


def list_jobs(include_disabled: bool = False) -> List[Dict[str, Any]]:
    """List all jobs, optionally including disabled ones."""
    jobs = [_normalize_job_record(j) for j in load_jobs()]
    if not include_disabled:
        jobs = [j for j in jobs if j.get("enabled", True)]
    try:
        from cron.executions import latest_executions

        latest = latest_executions([job.get("id", "") for job in jobs])
    except Exception:
        latest = {}
    for job in jobs:
        job["latest_execution"] = latest.get(job.get("id", ""))
    return jobs


def update_job(job_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Update a job by ID, refreshing derived schedule fields when needed."""
    # Block mutation of immutable fields. ``id`` in particular is a filesystem
    # path component under OUTPUT_DIR — letting an update change it leaks
    # path-escape values into output writes/deletes.
    bad_fields = _IMMUTABLE_JOB_FIELDS.intersection(updates or {})
    if bad_fields:
        raise ValueError(
            f"Cron job field(s) cannot be updated: {', '.join(sorted(bad_fields))}"
        )

    with _jobs_lock():
        jobs = load_jobs()
        for i, job in enumerate(jobs):
            if job["id"] != job_id:
                continue

            # Validate / normalize workdir if present in updates.  Empty string
            # or None both mean "clear the field" (restore old behaviour).
            if "workdir" in updates:
                _wd = updates["workdir"]
                if _wd in {None, "", False}:
                    updates["workdir"] = None
                else:
                    updates["workdir"] = _normalize_workdir(_wd)

            # Validate / normalize profile if present in updates.  Empty string
            # or None both mean "clear the field" (restore old behaviour).
            if "profile" in updates:
                _profile = updates["profile"]
                if _profile is None or _profile == "" or _profile is False:
                    updates["profile"] = None
                else:
                    updates["profile"] = _normalize_profile(_profile)

            previous_inference_axes = _normalized_inference_axes(job)
            updated = _apply_skill_fields({**job, **updates})
            schedule_changed = "schedule" in updates
            inference_fields_changed = bool(
                {"provider", "model", "base_url", "no_agent"}.intersection(updates)
            ) and _normalized_inference_axes(updated) != previous_inference_axes

            if "skills" in updates or "skill" in updates:
                normalized_skills = _normalize_skill_list(updated.get("skill"), updated.get("skills"))
                updated["skills"] = normalized_skills
                updated["skill"] = normalized_skills[0] if normalized_skills else None

            if schedule_changed:
                updated_schedule = updated["schedule"]
                # The API may pass schedule as a raw string (e.g. "every 10m")
                # instead of a pre-parsed dict.  Normalize it the same way
                # create_job() does so downstream code can call .get() safely.
                if isinstance(updated_schedule, str):
                    updated_schedule = parse_schedule(updated_schedule)
                    updated["schedule"] = updated_schedule
                updated["schedule_display"] = updates.get(
                    "schedule_display",
                    updated_schedule.get("display", updated.get("schedule_display")),
                )
                if updated.get("state") != "paused":
                    updated_next_run = compute_next_run(updated_schedule)
                    # Same guard as create_job: an UPDATE that sets a one-shot
                    # to a time >ONESHOT_GRACE_SECONDS in the past would store
                    # next_run_at=None with state="scheduled", re-creating the
                    # ghost job that never fires (#59395). Reject it here too so
                    # the bug can't re-enter through the update door.
                    if (
                        updated_next_run is None
                        and updated_schedule.get("kind") == "once"
                    ):
                        run_at = updated_schedule.get("run_at") or updated_schedule
                        logger.warning(
                            "Rejecting one-shot cron job update '%s': run_at %s "
                            "is outside the %ss grace window",
                            updated.get("name", job_id),
                            run_at,
                            ONESHOT_GRACE_SECONDS,
                        )
                        raise ValueError(
                            f"Requested one-shot time {run_at} is more than "
                            f"{ONESHOT_GRACE_SECONDS}s in the past and cannot be scheduled."
                        )
                    updated["next_run_at"] = updated_next_run

            if inference_fields_changed:
                provider_snapshot, model_snapshot = _compute_provider_model_snapshots(
                    provider=updated.get("provider"),
                    model=updated.get("model"),
                    base_url=updated.get("base_url"),
                    no_agent=updated.get("no_agent"),
                )
                updated["provider_snapshot"] = provider_snapshot
                updated["model_snapshot"] = model_snapshot

            if updated.get("enabled", True) and updated.get("state") != "paused" and not updated.get("next_run_at"):
                next_run = compute_next_run(updated["schedule"])
                if next_run is None and updated["schedule"].get("kind") == "once":
                    run_at = updated["schedule"].get("run_at", "unknown")
                    raise ValueError(
                        f"Requested one-shot time {run_at} is in the past "
                        f"(grace window: {ONESHOT_GRACE_SECONDS}s) and cannot be scheduled."
                    )
                updated["next_run_at"] = next_run

            jobs[i] = updated
            save_jobs(jobs)
            return _normalize_job_record(jobs[i])
    return None


def _unpause_updates(job: Dict[str, Any]) -> Dict[str, Any]:
    """Field updates that take a job OUT of the paused state, coherently.

    Clearing ``paused_at``/``paused_reason`` is deliberate — a running job must
    not keep advertising why it was once paused — but the WHY is not destroyed:
    the finished pause is archived to ``paused_history`` first, so an audit can
    still tell a routine pause from one that meant "this is broken".

    ``pause_jobs_cas`` (the scheduler's bulk pause) additionally writes a legacy
    ``paused: True`` flag that the scheduler itself ignores — every gate reads
    ``enabled``/``state``. Left behind it outlives the un-pause and reads as
    "still paused" to anyone auditing the record, so it is cleared in the same
    write. It is only touched when the key is already present, so records that
    never carried it stay byte-identical.

    Shared by ``resume_job`` and ``trigger_job``, the two paths that revive a
    paused job, so the field can never be coherent on one and lossy on the
    other.
    """
    updates: Dict[str, Any] = {"paused_at": None, "paused_reason": None}

    if "paused" in job:
        updates["paused"] = False

    if job.get("paused_at") or job.get("paused_reason"):
        history = [
            entry for entry in (job.get("paused_history") or [])
            if isinstance(entry, dict)
        ]
        history.append(
            {
                "paused_at": job.get("paused_at"),
                "paused_reason": job.get("paused_reason"),
                "resumed_at": _hermes_now().isoformat(),
            }
        )
        updates["paused_history"] = history[-PAUSED_HISTORY_LIMIT:]

    return updates


def pause_job(job_id: str, reason: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Pause a job without deleting it. Accepts a job ID or name.

    ``reason`` is the WHY, and it is worth passing: a paused job with no
    ``paused_reason`` is indistinguishable from one paused because it is
    broken, so the next reader has to guess whether resuming is safe.
    """
    job = resolve_job_ref(job_id)
    if not job:
        return None
    normalized_reason = (reason or "").strip() or None
    updates: Dict[str, Any] = {
        "enabled": False,
        "state": "paused",
        "paused_at": _hermes_now().isoformat(),
        "paused_reason": normalized_reason,
    }

    # Mirror of ``_unpause_updates``: keep the legacy ``paused`` bool in
    # lockstep with the lifecycle whenever the record carries it. Without this,
    # clearing the flag on resume just inverts the hazard — a job paused again
    # afterwards would sit at ``paused: False`` + ``enabled: False`` +
    # ``state: "paused"``, and an audit grepping for a false flag would read a
    # genuinely contained job as live. Same only-when-already-present rule, so
    # records that never carried the key stay byte-identical.
    if "paused" in job:
        updates["paused"] = True

    return update_job(job["id"], updates)


def resume_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Resume a paused job and compute the next future run from now. Accepts a job ID or name."""
    job = resolve_job_ref(job_id)
    if not job:
        return None

    next_run_at = compute_next_run(job["schedule"])
    if next_run_at is None and job["schedule"].get("kind") == "once":
        run_at = job["schedule"].get("run_at", "unknown")
        raise ValueError(
            f"Cannot resume: one-shot time {run_at} is in the past "
            f"(grace window: {ONESHOT_GRACE_SECONDS}s) and will never fire."
        )

    updates: Dict[str, Any] = {
        "enabled": True,
        "state": "scheduled",
        "next_run_at": next_run_at,
    }
    updates.update(_unpause_updates(job))
    return update_job(job["id"], updates)


def _get_event_bus():
    """Lazy-construct an EventBus instance for emit-side use.

    Kept as a module function so tests can monkeypatch the entire bus,
    and so an import failure (e.g. during early bootstrap) doesn't crash
    cron.jobs at module load.
    """
    from events.bus import EventBus
    return EventBus()


def emit_cron_triggered_safe(
    *,
    job_id: str,
    job_name: str,
    caller: Optional[str],
    reason: Optional[str],
    previous_next_run_at: Optional[str],
    new_next_run_at: Optional[str],
) -> None:
    """Best-effort CRON_TRIGGERED emit shared by every off-schedule trigger path.

    "Every" is load-bearing and enumerable. A cron job runs at an instant its
    schedule does not name for exactly three reasons, and all three emit here
    (a fourth apparent cause, plain LATENESS, is the scheduler observing an
    on-schedule job late — not a trigger, and deliberately not emitted):

    ==================  ==========================  ======================
    cause               call site                   reason=
    ==================  ==========================  ======================
    explicit trigger    ``trigger_job`` /           caller-supplied
                        ``request_run`` /
                        the cronjob tool
    event wake          ``cron.scheduler.``         ``"event_wake"``
                        ``_collect_woken_jobs``
    missed-run          ``_emit_recovery_fire_``    ``"missed_run_
    recovery            ``triggers`` (this module)  recovery"``
    ==================  ==========================  ======================

    Only the first MOVES the schedule; the other two record a fire that
    happens *in addition* to the regular cadence, and pass
    ``previous_next_run_at == new_next_run_at`` to say so in the payload.

    Until 2026-08-20 this docstring claimed the "shared by every path"
    property while the wake and recovery paths emitted nothing at all —
    which is what made two separate investigations conclude that off-schedule
    provenance was unobtainable. If a path is added, add it to the table or
    delete the claim.

    Both ``trigger_job`` (schedule-for-next-tick) and the cronjob tool's
    execute-now path route through here so the audit contract can't drift
    between them again (the 0.18.2 upstream merge dropped the emit from the
    run path when it stopped calling ``trigger_job``). Defensive on purpose:
    any import/bus failure is logged and swallowed — the trigger or run must
    never break because audit emission failed. The state mutation has already
    been persisted by the caller; the audit gap is a known degradation, not a
    correctness regression.
    """
    try:
        from events.producers.cron_trigger_emitter import emit_cron_triggered
        bus = _get_event_bus()
        emit_cron_triggered(
            bus,
            job_id=job_id,
            job_name=job_name,
            caller=caller,
            reason=reason,
            previous_next_run_at=previous_next_run_at,
            new_next_run_at=new_next_run_at,
        )
    except Exception:
        logger.exception(
            "cron_triggered emit failed for job_id=%s", job_id
        )


def trigger_job(
    job_id: str,
    caller: Optional[str] = None,
    reason: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Schedule a job only after entering the canonical dispatch boundary."""
    with default_control_store().dispatch_section(boundary="trigger-job"):
        return _trigger_job_admitted(job_id, caller=caller, reason=reason)


def _trigger_job_admitted(
    job_id: str,
    caller: Optional[str] = None,
    reason: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Schedule a job to run on the next scheduler tick.

    Sets ``next_run_at = NOW`` and emits a ``cron_triggered`` event capturing
    the caller (e.g. ``"hermes_cli:cron_run"``, ``"llm:cronjob_tool"``,
    ``"http_api:web_server"``) and an optional reason string. ``caller=None``
    is allowed for backward compatibility but logs a WARNING — every internal
    caller should pass an explicit caller string.
    """
    # v0.15.1 catch-up: resolve by ID or name (upstream resolve_job_ref) so
    # `cron run <name>` works; raises AmbiguousJobReference for an ambiguous
    # name (caller-facing, intentional). Fork's CRON_TRIGGERED traceability
    # below is preserved.
    job = resolve_job_ref(job_id)
    if not job:
        return None

    if caller is None:
        logger.warning(
            "trigger_job called anonymously (caller=None) for job_id=%s "
            "name=%s — postmortem attribution will be impossible. "
            "Pass an explicit caller string.",
            job_id, job.get("name"),
        )

    previous_next_run_at = job.get("next_run_at")

    updated = update_job(
        job["id"],
        {
            "enabled": True,
            "state": "scheduled",
            "next_run_at": _hermes_now().isoformat(),
            **_unpause_updates(job),
        },
    )

    if updated is not None:
        emit_cron_triggered_safe(
            job_id=job["id"],
            job_name=updated.get("name") or job.get("name") or job["id"],
            caller=caller,
            reason=reason,
            previous_next_run_at=previous_next_run_at,
            new_next_run_at=updated["next_run_at"],
        )

    return updated


def request_run(
    job_id: str,
    *,
    caller: str,
    reason: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Request an enabled job only inside the canonical dispatch boundary."""
    with default_control_store().dispatch_section(boundary="request-run"):
        return _request_run_admitted(job_id, caller=caller, reason=reason)


def _request_run_admitted(
    job_id: str,
    *,
    caller: str,
    reason: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Schedule an ALREADY-ENABLED job for the next tick. Never enables.

    The non-enabling counterpart to ``trigger_job``, for callers that are
    activating work on a worker's behalf rather than expressing an operator's
    "run this now" intent. ``trigger_job`` sets ``enabled: True``, so an
    automated caller that mis-resolves a job silently revives a worker an
    operator deliberately disabled — the hazard ``cron.wake_channel`` avoids
    in-process, and which this function makes available across processes.

    Returns ``None`` — writing nothing at all — when the job is unknown or not
    enabled. Fails closed on purpose: not activating is recoverable, reviving a
    disabled worker is not.

    Writes NO LIFECYCLE field — only ``next_run_at`` (``update_job``'s shared
    normalization may still materialize ``skills``/``skill`` on a legacy record
    that lacks those keys, which is why the claim above is scoped to lifecycle
    fields specifically). The due scan gates on ``enabled`` and never reads
    ``state`` (see ``get_due_and_skipped_jobs``), and ``pause_job`` always sets
    ``enabled: False`` alongside ``state: "paused"`` — so the single ``enabled``
    check above already covers paused jobs, and no lifecycle field needs
    touching. Keeping the write off lifecycle fields is what makes "this cannot
    change operator-visible state" assertable.

    ``caller`` is required, unlike ``trigger_job``'s warn-and-continue
    back-compat allowance: this is a new API, and an unattributable automated
    activation is impossible to reconstruct in a postmortem.
    """
    if not isinstance(caller, str) or not caller.strip():
        raise ValueError("caller must be a non-empty string")

    job = resolve_job_ref(job_id)
    if not job:
        return None

    # Intentional asymmetry: a legacy record with no "enabled" key is treated
    # as not-enabled HERE (fails closed), even though the due scan defaults
    # missing "enabled" to True (`job.get("enabled", True)`). A record like
    # that is runnable by the scheduler but permanently refused by this path.
    if not job.get("enabled"):
        logger.info(
            "request_run refused job_id=%s name=%s: not enabled — not reviving "
            "(caller=%s reason=%s)",
            job["id"], job.get("name"), caller, reason,
        )
        return None

    previous_next_run_at = job.get("next_run_at")

    updated = update_job(job["id"], {"next_run_at": _hermes_now().isoformat()})

    if updated is not None:
        emit_cron_triggered_safe(
            job_id=job["id"],
            job_name=updated.get("name") or job.get("name") or job["id"],
            caller=caller,
            reason=reason,
            previous_next_run_at=previous_next_run_at,
            new_next_run_at=updated["next_run_at"],
        )

    return updated


def remove_job(job_id: str) -> bool:
    """Remove a job by ID or name."""
    job = resolve_job_ref(job_id)
    if not job:
        return False
    canonical_id = job["id"]
    with _jobs_lock():
        jobs = load_jobs()
        original_len = len(jobs)
        jobs = [j for j in jobs if j["id"] != canonical_id]
        if len(jobs) < original_len:
            # Resolve the output dir BEFORE saving so a legacy unsafe ID (e.g.
            # left over from before the create-time guard) fails closed without
            # half-applying the removal.
            job_output_dir = _job_output_dir(canonical_id)
            save_jobs(jobs)
            # Clean up output directory to prevent orphaned dirs accumulating
            if job_output_dir.exists():
                shutil.rmtree(job_output_dir)
            return True
    return False


def mark_job_run(job_id: str, success: bool, error: Optional[str] = None,
                 delivery_error: Optional[str] = None) -> Optional[str]:
    """
    Mark a job as having been run.
    
    Updates last_run_at, last_status, increments completed count,
    computes next_run_at, and auto-deletes if repeat limit reached.

    ``delivery_error`` is tracked separately from the agent error — a job
    can succeed (agent produced output) but fail delivery (platform down).
    """
    with _jobs_lock():
        jobs = load_jobs()
        for i, job in enumerate(jobs):
            if job["id"] == job_id:
                now = _hermes_now().isoformat()
                job["last_run_at"] = now
                job["last_status"] = "ok" if success else "error"
                job["last_error"] = error if not success else None
                # Track consecutive errors for alerting
                if success:
                    job["consecutive_errors"] = 0
                else:
                    job["consecutive_errors"] = job.get("consecutive_errors", 0) + 1
                # Track delivery failures separately — cleared on successful delivery
                job["last_delivery_error"] = delivery_error
                # Clear any external-fire claim so a re-armed recurring job can
                # be claimed again on its next fire (Phase 4C CAS).
                job["fire_claim"] = None
                # Clear the one-shot running-claim (#59229): the run is over, so
                # a re-armed recurring job or a re-dispatched one-shot recovery
                # is claimable again. No-op if the job never carried a claim.
                if job.get("run_claim") is not None:
                    job["run_claim"] = None
                
                # Increment completed count.  Finite one-shot jobs are
                # pre-claimed by claim_dispatch() BEFORE the side effect runs
                # (issue #38758), which already incremented completed — do not
                # double-count them here.  Recurring jobs and direct callers
                # with no pre-run claim still get the legacy increment.
                if job.get("repeat"):
                    repeat = job["repeat"]
                    times = repeat.get("times")
                    completed = repeat.get("completed", 0)
                    kind = job.get("schedule", {}).get("kind")
                    preclaimed_oneshot = (
                        kind == "once"
                        and times is not None
                        and times > 0
                        and completed > 0
                    )
                    if not preclaimed_oneshot:
                        completed += 1
                        repeat["completed"] = completed

                    # Check if we've hit the repeat limit
                    if times is not None and times > 0 and completed >= times:
                        # Remove the job (limit reached)
                        jobs.pop(i)
                        save_jobs(jobs)
                        return now
                
                # Compute next run
                job["next_run_at"] = compute_next_run(job["schedule"], now)

                # If no next run, decide whether this is terminal completion
                # (one-shot) or a transient failure (recurring schedule couldn't
                # compute — e.g. 'croniter' missing from the runtime env).
                # Recurring jobs must NEVER be silently disabled: that turns a
                # missing runtime dep into "job completed" and the user's
                # schedule quietly goes off. See issue #16265.
                if job["next_run_at"] is None:
                    kind = job.get("schedule", {}).get("kind")
                    if kind in {"cron", "interval"}:
                        job["state"] = "error"
                        if not job.get("last_error"):
                            job["last_error"] = (
                                "Failed to compute next run for recurring "
                                "schedule (is the 'croniter' package "
                                "installed in the gateway's Python env?)"
                            )
                        logger.error(
                            "Job '%s' (%s) could not compute next_run_at; "
                            "leaving enabled and marking state=error so the "
                            "job is not silently disabled.",
                            job.get("name", job.get("id", "?")),
                            kind,
                        )
                    else:
                        job["enabled"] = False
                        job["state"] = "completed"
                elif job.get("state") != "paused":
                    job["state"] = "scheduled"

                save_jobs(jobs)
                return now

        logger.warning("mark_job_run: job_id %s not found, skipping save", job_id)
        return None


def amend_late_outcome_after_abandon(
    job_id: str,
    *,
    abandon_error: str,
    success: bool,
    error: Optional[str] = None,
    expected_last_run_at: str,
) -> bool:
    """Correct a run status after its deadline-abandoned worker finishes.

    The soft deadline marks a parallel-safe run failed and releases its
    slot WITHOUT killing the worker. This compare-and-swap preserves the
    worker's real terminal verdict, but only while the job still carries
    the exact deadline verdict being corrected. The timestamp matters: a
    successor can hit the byte-identical deadline message while the prior
    worker is still finishing.

    This intentionally does not call :func:`mark_job_run`: doing so would
    double-count ``repeat.completed``, recompute ``next_run_at`` from the
    late finish time, and clear claims that may belong to a successor.
    Only the status fields are amended.
    """
    with _jobs_lock():
        jobs = load_jobs()
        for job in jobs:
            if job["id"] != job_id:
                continue
            if job.get("last_status") != "error":
                return False
            if job.get("last_error") != abandon_error:
                return False
            if job.get("last_run_at") != expected_last_run_at:
                return False

            job["last_status"] = "ok" if success else "error"
            job["last_error"] = None if success else (error or "unknown failure")
            if success:
                job["consecutive_errors"] = 0
            save_jobs(jobs)
            return True

        logger.warning(
            "amend_late_outcome_after_abandon: job_id %s not found", job_id
        )
        return False


def _is_strictly_newer(candidate: str, existing: str) -> bool:
    """True if ``candidate`` is a later instant than ``existing``.

    Compared as instants, not strings: these timestamps carry a local UTC
    offset that shifts across a DST boundary (-04:00 → -05:00), so a lexical
    compare can rank a genuinely later run as older. Unparseable input falls
    back to the lexical order rather than blocking the stamp outright.
    """
    try:
        return (_ensure_aware(datetime.fromisoformat(candidate))
                > _ensure_aware(datetime.fromisoformat(existing)))
    except (TypeError, ValueError):
        return candidate > existing


def mark_job_interrupted(job_id: str, *, ran_at: str,
                         error: Optional[str] = None) -> bool:
    """Record that a job fired but its outcome was never durably written.

    ``mark_job_run()`` is the only writer of ``last_run_at``, and it runs at the
    END of a run inside the owning process, while ``advance_next_run()`` moves
    ``next_run_at`` BEFORE it. Kill the owner in between and the schedule has
    advanced but the job record still reports its previous clean completion —
    a daily job then reads as days-idle while its side effects demonstrably
    ran. The execution ledger already proves the attempt happened; this carries
    that verdict back so jobs.json under-reports nothing.

    Deliberately narrower than ``mark_job_run()``: ``next_run_at`` is left
    alone (already advanced pre-run — recomputing here would skip a fire),
    ``repeat.completed`` is not incremented (an unknown outcome is not a known
    completion, and bumping it can trip the one-shot repeat-limit deletion),
    and ``consecutive_errors`` is untouched (unknown is not a known failure, so
    it must not drive error alerting).

    Returns True if the record was stamped.
    """
    with _jobs_lock():
        jobs = load_jobs()
        for job in jobs:
            if job["id"] != job_id:
                continue
            # Never regress a newer outcome. A recovery pass can run after the
            # job has already completed cleanly again (long-lived ledger row,
            # delayed restart); the fresher truth wins.
            previous = job.get("last_run_at")
            if previous and not _is_strictly_newer(ran_at, previous):
                logger.info(
                    "mark_job_interrupted: job '%s' already reports a newer run "
                    "(%s >= %s) — leaving it alone",
                    job.get("name", job_id), previous, ran_at,
                )
                return False
            job["last_run_at"] = ran_at
            job["last_status"] = "unknown"
            job["last_error"] = error
            save_jobs(jobs)
            logger.warning(
                "Job '%s' fired at %s but its owner exited before recording an "
                "outcome — marked last_status=unknown",
                job.get("name", job_id), ran_at,
            )
            return True

    logger.info(
        "mark_job_interrupted: job_id %s not found (deleted since it ran)", job_id
    )
    return False


def claim_dispatch(job_id: str) -> bool:
    """Atomically claim a finite one-shot job dispatch BEFORE execution.

    Increments ``repeat.completed`` under the cross-process jobs lock and
    persists the claim immediately, so that if the tick dies mid-execution
    (gateway kill, OOM, segfault, hard-timeout) the dispatch is not lost.
    This converts finite one-shot jobs from *at-least-once* to *at-most-times*
    semantics — a job that self-destructs fires at most ``repeat.times`` times
    instead of infinitely (issue #38758).

    Returns ``True`` if the caller may proceed to run the job, ``False`` if the
    dispatch limit is already reached (in which case the stale job is removed).

    Only claims jobs with ``schedule.kind == "once"`` and ``repeat.times > 0``.
    Recurring jobs (they use ``advance_next_run``) and infinite-repeat / no-repeat
    jobs are left unchanged and always allowed to proceed.
    """
    with _jobs_lock():
        jobs = load_jobs()
        for i, job in enumerate(jobs):
            if job["id"] != job_id:
                continue
            if job.get("schedule", {}).get("kind") != "once":
                return True  # recurring jobs use advance_next_run(), not dispatch claims
            repeat = job.get("repeat")
            if not repeat:
                return True  # no repeat limit — always dispatch
            times = repeat.get("times")
            if times is None or times <= 0:
                return True  # infinite — always dispatch
            completed = repeat.get("completed", 0)
            if completed >= times:
                # Already dispatched the max number of times (e.g. a prior
                # tick claimed then died before mark_job_run could remove it).
                # Clean up so it stops appearing as due on every tick.
                jobs.pop(i)
                save_jobs(jobs)
                logger.info(
                    "Job '%s': dispatch limit reached (%d/%d) — removing",
                    job.get("name", job.get("id", "?")),
                    completed,
                    times,
                )
                return False
            # Claim this dispatch before the side effect runs.
            repeat["completed"] = completed + 1
            save_jobs(jobs)
            logger.debug(
                "Job '%s': claimed dispatch %d/%d",
                job.get("name", job.get("id", "?")),
                repeat["completed"],
                times,
            )
            return True

        logger.debug(
            "claim_dispatch: job_id %s not in store — proceeding without claim "
            "(handed-in job dict; nothing to persist a claim against)",
            job_id,
        )
        return True


def heartbeat_run_claim(job_id: str, *, expected_owner: str) -> bool:
    """Refresh a one-shot's ``run_claim`` timestamp while its run is alive.

    Called periodically from the scheduler's run monitor (#62002) so a
    legitimately long run keeps its claim fresh: an expired claim then really
    does mean "the claiming process died", and neither another process's tick
    nor this process's own next tick will re-dispatch or stale-remove the job
    while the run is in flight. mark_job_run() clears the claim on completion.

    ``expected_owner`` is the stable owner copied from the dispatched job. The
    compare-and-refresh prevents a stale runner that resumes after a long sleep
    from extending a claim another scheduler process has since taken over.

    Returns True if this owner's one-shot claim was refreshed; False when the
    job, claim, or ownership no longer matches.
    """
    with _jobs_lock():
        jobs = load_jobs()
        for job in jobs:
            if job.get("id") != job_id:
                continue
            if job.get("schedule", {}).get("kind") != "once":
                return False
            claim = job.get("run_claim")
            if not isinstance(claim, dict) or claim.get("by") != expected_owner:
                return False
            claim["at"] = _hermes_now().isoformat()
            save_jobs(jobs)
            return True
    return False


def advance_next_run(job_id: str) -> bool:
    """Preemptively advance next_run_at for a recurring job before execution.

    Call this BEFORE run_job() so that if the process crashes mid-execution,
    the job won't re-fire on the next gateway restart.  This converts the
    scheduler from at-least-once to at-most-once for recurring jobs — missing
    one run is far better than firing dozens of times in a crash loop.

    One-shot jobs are left unchanged so they can still retry on restart.

    Returns True if next_run_at was advanced, False otherwise.
    """
    with _jobs_lock():
        jobs = load_jobs()
        for job in jobs:
            if job["id"] == job_id:
                kind = job.get("schedule", {}).get("kind")
                if kind not in {"cron", "interval"}:
                    return False
                now = _hermes_now().isoformat()
                new_next = compute_next_run(job["schedule"], now)
                if new_next and new_next != job.get("next_run_at"):
                    job["next_run_at"] = new_next
                    save_jobs(jobs)
                    return True
                return False
        return False


def _machine_id() -> str:
    """Stable-ish identifier for claim attribution/debugging (NOT correctness).

    Uses ``HERMES_MACHINE_ID`` if set, else hostname + pid. The CAS correctness
    comes from the file lock + the fresh-claim check, not from this value.
    """
    explicit = os.getenv("HERMES_MACHINE_ID", "").strip()
    if explicit:
        return explicit
    try:
        import socket
        host = socket.gethostname()
    except Exception:
        host = "unknown"
    return f"{host}:{os.getpid()}"


def claim_job_for_fire(job_id: str, *, claim_ttl_seconds: int = 300) -> bool:
    """Atomically claim a job for a single external 'fire' (multi-machine
    at-most-once). Returns True iff THIS caller won the claim.

    Used by the external-provider fire path (``CronScheduler.fire_due``) when an
    external scheduler (Chronos) signals a job is due across N gateway replicas:
    exactly one wins. Single-machine deployments always win.

    Under the file lock: reject if the job is missing/disabled/paused. If a
    fresh claim (younger than ``claim_ttl_seconds``) already exists, lose.
    Otherwise stamp a ``fire_claim`` and, for recurring jobs, advance
    ``next_run_at`` (mirrors ``advance_next_run``'s at-most-once bump so a stale
    re-delivery for the old time can't re-fire). One-shots keep ``next_run_at``
    but the fresh ``fire_claim`` blocks a duplicate retry for the same fire.
    ``mark_job_run`` clears the claim on completion so a re-armed recurring job
    is claimable again next fire.

    Admission is layered, and the two layers answer different questions:

    1. A claim younger than ``claim_ttl_seconds`` loses outright. This is the
       cross-process CAS, and it is what closes the window between winning a
       claim and writing the durable execution row (both call sites claim
       first), so the TTL can never be zero.
    2. Past the TTL the clock says "go", but a run of this job may simply be
       long. ``_job_has_live_execution`` asks the execution ledger, which is
       cross-process and PID-recycle-safe, and the fire loses if another run is
       PROVABLY still alive.

    The stale-claim TTL means a machine that crashed after claiming but before
    completing doesn't wedge the job forever — after the TTL another fire can
    reclaim it. Layer 2 preserves that property rather than weakening it: it
    refuses only on positive proof of life, so a dead owner (or an owner whose
    liveness cannot be determined) still yields the job. Before layer 2 existed
    a run legitimately outliving the TTL was read as a crash and fired again —
    the 2026-08-24 ``jobflow-matcher`` duplicate, admitted 1810.63s in.
    """
    with _jobs_lock():
        jobs = load_jobs()
        for job in jobs:
            if job["id"] != job_id:
                continue
            if not job.get("enabled", True) or job.get("state") == "paused":
                return False
            now = _hermes_now()
            existing = job.get("fire_claim")
            if existing:
                try:
                    claimed_at = _ensure_aware(datetime.fromisoformat(existing["at"]))
                    # Bounded on BOTH sides (#60703): a claim stamped in the
                    # future (clock/TZ skew across a restart, or a corrupted
                    # timestamp) would otherwise have a negative age and stay
                    # "fresh" forever — the job becomes permanently unfireable
                    # and every manual `cron run` reports "already being
                    # fired". Treat future-dated claims as stale/overwritable.
                    _age = (now - claimed_at).total_seconds()
                    if 0 <= _age < claim_ttl_seconds:
                        return False  # someone holds a fresh claim
                except Exception:
                    pass  # malformed claim → overwrite

            # Layer 2 (2026-08-24): the claim is stale or absent, so the clock says
            # "go". Before overwriting it, ask the durable ledger whether a run
            # of this job is STILL ALIVE — a 20-40 minute run is otherwise
            # indistinguishable from a tick that died at minute 5.
            #
            # Order matters both ways. The fresh-claim check above must stay
            # FIRST for correctness: both call sites claim before
            # create_execution, so a winner holds a claim for a moment with no
            # ledger row yet, and layer 1 is what closes that window (which is
            # why the TTL can never go to zero). It must also stay first for
            # cost: measured warm against the real 10k-row ledger this check is
            # ~8.6ms median with nothing running and ~72ms when it does probe a
            # live owner, all of it under the cross-process _jobs_lock. That is
            # affordable only because this path is rare — manual and
            # external-provider fires; the built-in ticker never comes here —
            # and because a fresh claim short-circuits before reaching it.
            #
            # Known, accepted false-refusal window: _run_one_job_admitted calls
            # mark_job_run (clears the claim) immediately before
            # finish_execution (closes the row), so a fire landing between those
            # two statements is refused. It is one statement wide and costs a
            # missed run, not a duplicate. Swapping them would trade it for the
            # 2026-07-27 defect where a crash in between loses the jobs.json
            # write-back entirely.
            if _job_has_live_execution(job_id):
                return False

            job["fire_claim"] = {"at": now.isoformat(), "by": _machine_id()}
            kind = job.get("schedule", {}).get("kind")
            if kind in {"cron", "interval"}:
                nxt = compute_next_run(job["schedule"], now.isoformat())
                if nxt:
                    job["next_run_at"] = nxt
            save_jobs(jobs)
            return True
        return False


#: Transient key stamped on a due-job dict when the miss-recovery tree decided
#: to fire it once on catch-up. Consumed (and popped) by
#: ``_emit_recovery_fire_triggers`` after the jobs lock is released. It is only
#: ever set on the deepcopy the due scan builds, never on a ``raw_jobs``
#: element, so it cannot reach jobs.json.
_RECOVERY_FIRE_MARKER = "_missed_run_recovery"

#: The reason= string every miss-recovery catch-up fire carries in its
#: cron_triggered payload. Kept distinct from the wake path's "event_wake" so
#: the two stay separable in audit.jsonl by payload alone.
RECOVERY_FIRE_REASON = "missed_run_recovery"


def _emit_recovery_fire_triggers(due: List[Dict[str, Any]]) -> None:
    """Emit one cron_triggered per catch-up fire, with the jobs lock RELEASED.

    Why not emit at the decision site inside ``_get_due_jobs_locked``: that
    runs under ``_jobs_lock()``, a cross-process advisory file lock whose
    documented contract is that "every critical section that uses it is short
    (field updates only — no agent execution)". An event-bus emit is a SQLite
    transaction against a different file; putting one inside would make every
    standalone ``hermes cron`` invocation on the box wait behind it. The
    existing ``cron_skipped`` path already uses this shape — the decision is
    recorded as data under the lock and emitted by the caller afterwards.

    Marker-driven rather than list-driven on purpose: only jobs that actually
    survived the rest of the scan into ``due`` are here, so a catch-up
    decision later withdrawn (one-shot dispatch-limit removal, malformed-job
    ``continue``) cannot emit a fire that never happened.

    Never raises. The try/except is not redundant with
    ``emit_cron_triggered_safe``'s own — that one guards the emit's INTERNALS,
    this one guards the due scan against an emitter that is unavailable or
    refactored to raise. Losing an audit record must never cost a tick its
    jobs. Per-job rather than around the loop, so one bad record cannot
    silently drop the provenance of every job behind it.
    """
    for job in due:
        try:
            marker = job.pop(_RECOVERY_FIRE_MARKER, None)
            if not marker:
                continue
            emit_cron_triggered_safe(
                job_id=job.get("id") or "?",
                job_name=job.get("name") or job.get("id") or "?",
                caller="cron.miss_recovery",
                reason=RECOVERY_FIRE_REASON,
                # The catch-up deliberately leaves next_run_at alone —
                # scheduler.advance_next_run() moves it just before the run.
                # Equal values record "this fire did not move the schedule".
                previous_next_run_at=marker.get("missed_at"),
                new_next_run_at=marker.get("missed_at"),
            )
        except Exception:
            logger.exception(
                "recovery-fire cron_triggered emit failed for job_id=%s",
                job.get("id"),
            )


def get_due_and_skipped_jobs() -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return (due, skipped) lists.

    `due` — jobs to execute on this tick.
    `skipped` — jobs whose next_run_at was fast-forwarded past a missed
                fire window. Each entry is a dict with keys:
                  job_id, name, missed_at (iso str), missed_seconds (int),
                  schedule_kind (str), reason (str).

    For recurring jobs (cron/interval), if the scheduled time is stale (past the
    catch-up grace window, e.g. because the gateway was down OR because a
    long-running previous execution overran the interval), the accumulated
    missed runs are collapsed — ``next_run_at`` is fast-forwarded to the next
    future occurrence so a backlog does NOT burst-fire on restart. Whether the
    job then fires ONCE now (perpetual-defer fix #33315) or is skipped entirely
    is governed by the eligibility decision tree below: skip_only policy,
    weekly/unknown-period caps, and the 24h miss cap force a skip (recorded in
    ``skipped``); daily-or-shorter misses within 24h fire exactly once.

    Note: firing once on catch-up flows through ``mark_job_run``, so a job with
    a ``repeat.times`` limit consumes one of its runs on that catch-up fire.
    """
    with _jobs_lock():
        due, skipped = _get_due_jobs_locked()
    # Deliberately outside the lock — see _emit_recovery_fire_triggers.
    _emit_recovery_fire_triggers(due)
    return due, skipped


def _get_due_jobs_locked() -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Inner implementation of get_due_and_skipped_jobs(); must be called with _jobs_lock held."""
    now = _hermes_now()
    raw_jobs = load_jobs()
    needs_save = False

    # Repair id-less records BEFORE anything keys off ``job["id"]``. A direct
    # jobs.json edit that bypassed add_job() can leave a record without an "id"
    # (older writers used "job_id"). Every downstream site — the logging
    # helpers and the ``for rj in raw_jobs: if rj["id"] == job["id"]``
    # persistence loops — indexes job["id"] eagerly, so a single malformed
    # record raised KeyError mid-tick, aborting the whole scan before
    # save_jobs() ran. That froze the entire profile's scheduler in a
    # per-minute fast-forward loop (healthy jobs recomputed in memory, then
    # discarded when the exception unwound). Recover the id from the drifted
    # "job_id" key when present, else synthesize one, and persist.
    for rj in raw_jobs:
        if not rj.get("id"):
            rj["id"] = rj.pop("job_id", None) or uuid.uuid4().hex[:12]
            needs_save = True

    jobs = [_apply_skill_fields(j) for j in copy.deepcopy(raw_jobs)]
    due: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    # Normalize malformed "schedule" records (direct jobs.json edit, old writers,
    # corruption, etc.). "schedule" must be a dict; a null/string/etc. value
    # makes `schedule.get("kind")` or direct `schedule["kind"]` / ["expr"] /
    # ["minutes"] later raise and abort the entire scan *before* save_jobs().
    # Healthy jobs then lose their fast-forwarded next_run_at (exactly the
    # failure mode of the id-less job bug fixed above). Repair early at the
    # source so the rest of the tick can proceed and persist progress for
    # siblings.
    for j in jobs:
        if not isinstance(j.get("schedule"), dict):
            j["schedule"] = {}
            needs_save = True
    for rj in raw_jobs:
        if not isinstance(rj.get("schedule"), dict):
            rj["schedule"] = {}
            needs_save = True

    # Normalize malformed "next_run_at" records (direct jobs.json edit,
    # corruption, migration, or buggy writer). If present but not a valid
    # ISO string, datetime.fromisoformat(next_run) later raises and aborts
    # the entire scan *before* save_jobs(). Healthy siblings then lose any
    # fast-forwarded next_run_at (same class of bug as bad "id" or "schedule").
    # Strip the bad value so the existing "no next_run_at" recovery path
    # recomputes a sane value and persists it for this job.
    for j in jobs:
        nr = j.get("next_run_at")
        if nr is not None:
            if not isinstance(nr, str):
                j.pop("next_run_at", None)
                needs_save = True
            else:
                try:
                    datetime.fromisoformat(nr)
                except Exception:
                    j.pop("next_run_at", None)
                    needs_save = True
    for rj in raw_jobs:
        nr = rj.get("next_run_at")
        if nr is not None:
            if not isinstance(nr, str):
                rj.pop("next_run_at", None)
                needs_save = True
            else:
                try:
                    datetime.fromisoformat(nr)
                except Exception:
                    rj.pop("next_run_at", None)
                    needs_save = True

    # Same treatment for last_run_at (used as base in recovery / compute_next_run).
    for j in jobs:
        lr = j.get("last_run_at")
        if lr is not None and not isinstance(lr, str):
            j.pop("last_run_at", None)
            needs_save = True
        elif isinstance(lr, str):
            try:
                datetime.fromisoformat(lr)
            except Exception:
                j.pop("last_run_at", None)
                needs_save = True
    for rj in raw_jobs:
        lr = rj.get("last_run_at")
        if lr is not None and not isinstance(lr, str):
            rj.pop("last_run_at", None)
            needs_save = True
        elif isinstance(lr, str):
            try:
                datetime.fromisoformat(lr)
            except Exception:
                rj.pop("last_run_at", None)
                needs_save = True

    # Resolve the one-shot running-claim stale-recovery TTL once per scan
    # (derived from HERMES_CRON_TIMEOUT). See _oneshot_run_claim_ttl_seconds.
    _run_claim_ttl = _oneshot_run_claim_ttl_seconds()

    for job in jobs:
        # Per-job containment (structural guard): one malformed or
        # unexpected job record must never abort the whole scan. The id /
        # schedule / timestamp normalizations above repair the known shapes;
        # this guard catches every FUTURE variant, degrading to "skip this
        # job this tick" so healthy siblings still run and their recovered
        # state still reaches save_jobs() below.
        try:
            if not job.get("enabled", True):
                continue

            # Cross-process running-claim guard (#59229): if another scheduler
            # process already claimed this one-shot and its run is still in flight
            # (claim younger than the TTL), skip it — do NOT re-dispatch. The
            # claim is stamped just before we return the job as due (below) and
            # cleared by mark_job_run() on completion. A claim older than the TTL
            # is treated as stale (the claiming tick died mid-run) and allowed
            # through so the job is recovered rather than wedged forever.
            existing_claim = job.get("run_claim")
            if existing_claim and job.get("schedule", {}).get("kind") == "once":
                try:
                    claimed_at = _ensure_aware(
                        datetime.fromisoformat(existing_claim["at"])
                    )
                    # 0 <= age: a future-dated claim (clock/TZ skew across a
                    # restart) must be treated as stale, not eternally fresh,
                    # or the one-shot is skipped forever (#60703).
                    _age = (now - claimed_at).total_seconds()
                    if 0 <= _age < _run_claim_ttl:
                        continue  # a fresh claim is held by an in-flight run
                except (KeyError, ValueError, TypeError):
                    pass  # malformed claim → fall through and (re)claim

            next_run = job.get("next_run_at")
            if not next_run:
                schedule = job.get("schedule", {})
                kind = schedule.get("kind")

                # One-shot jobs use a small grace window via the dedicated helper.
                recovered_next = _recoverable_oneshot_run_at(
                    schedule,
                    now,
                    last_run_at=job.get("last_run_at"),
                )
                recovery_kind = "one-shot" if recovered_next else None

                # Recurring jobs reach here only when something — typically a
                # direct jobs.json edit that bypassed add_job() — left
                # next_run_at unset.  Without this branch, such jobs are
                # silently skipped forever; recompute next_run_at from the
                # schedule so they pick up at their next scheduled tick.
                if not recovered_next and kind in {"cron", "interval"}:
                    recovered_next = compute_next_run(schedule, now.isoformat())
                    if recovered_next:
                        recovery_kind = kind

                if not recovered_next:
                    continue

                job["next_run_at"] = recovered_next
                next_run = recovered_next
                logger.info(
                    "Job '%s' had no next_run_at; recovering %s run at %s",
                    job.get("name", job.get("id", "?")),
                    recovery_kind,
                    recovered_next,
                )
                for rj in raw_jobs:
                    if rj["id"] == job["id"]:
                        rj["next_run_at"] = recovered_next
                        needs_save = True
                        break

            raw_next_run_dt = datetime.fromisoformat(next_run)
            schedule = job.get("schedule", {})
            kind = schedule.get("kind")

            next_run_dt = _ensure_aware(raw_next_run_dt)
            # Migration repair: a cron job persists next_run_at as an absolute
            # instant, but the cron expr describes local wall-clock intent. If the
            # configured/system timezone changed after persistence, the stored
            # instant's offset no longer matches now's, and its converted time can
            # look due hours early (21:00+10 -> 13:00+02). When the stored *wall
            # clock* is still in the future, recompute from the schedule so we fire
            # at the intended local time instead of early-then-again.
            #
            # TRADE-OFF: this cannot distinguish a config/host TZ migration from a
            # legitimate DST offset change. A DST boundary that satisfies all four
            # conditions will recompute (and thus SKIP the pending occurrence, no
            # catch-up) rather than fire it. Accepted: in the pure-migration case
            # the recompute lands on the same wall-clock time later the same period,
            # and DST-boundary collisions with a still-future stored wall clock are
            # rare relative to the double-fire bug this prevents (#28934).
            if (
                kind == "cron"
                and next_run_dt <= now
                and _timezone_offset_mismatch(raw_next_run_dt, now)
                and _stored_wall_clock_is_future(raw_next_run_dt, now)
            ):
                new_next = compute_next_run(schedule, now.isoformat())
                if new_next:
                    logger.info(
                        "Job '%s' next_run_at offset changed (%s -> %s). "
                        "Recomputing cron run to preserve local wall-clock intent: %s",
                        job.get("name", job.get("id", "?")),
                        raw_next_run_dt.utcoffset(),
                        now.utcoffset(),
                        new_next,
                    )
                    for rj in raw_jobs:
                        if rj["id"] == job["id"]:
                            rj["next_run_at"] = new_next
                            needs_save = True
                            break
                    continue

            if next_run_dt <= now:

                # For recurring jobs, check if the scheduled time is stale
                # (gateway was down and missed the window). Fast-forward to
                # the next future occurrence instead of firing a stale run.
                missed_seconds = int((now - next_run_dt).total_seconds())
                if kind in ("cron", "interval"):
                    # ON-TIME GATE (2026-06-10). The sequential scheduler tick
                    # always observes a due job some seconds after its scheduled
                    # instant, so lateness within grace (period/2 clamped to
                    # [120s, 7200s]) is normal operation — NOT a missed window.
                    # Without this gate the miss-recovery tree below ran on every
                    # ordinary fire, which made weekly (default_period_cap) and
                    # skip_only crons permanently unable to fire (e.g.
                    # security-audit-weekly skipped at 12s/27s late on
                    # 2026-06-01/06-08). recovery_policy and the period caps
                    # govern miss RECOVERY only and apply past grace.
                    grace = _compute_grace_seconds(schedule)
                    if missed_seconds <= grace:
                        due.append(job)
                        continue

                    # Eligibility decision tree for missed recurring jobs
                    # (past grace — a genuinely missed window, e.g. gateway
                    # downtime across the fire instant).
                    period_seconds = _compute_period_seconds(schedule)
                    # recovery_policy is currently a flag-style string. The only recognized
                    # value is "skip_only"; anything else (None, missing, or other strings)
                    # falls through to the period/miss-size eligibility check. If this ever
                    # needs richer shape (e.g. {"mode": "skip_only", "max_miss_seconds": N}),
                    # add an explicit shape check here.
                    recovery_policy = job.get("recovery_policy")

                    if recovery_policy == "skip_only":
                        reason = "skip_only"
                        eligible_for_fire_once = False
                    elif period_seconds is None or period_seconds > 86400:
                        # Weekly or unknown-period cron — always skip
                        reason = "default_period_cap"
                        eligible_for_fire_once = False
                    elif missed_seconds > 86400:
                        # Daily cron missed for more than a full period
                        reason = "miss_exceeded_24h_cap"
                        eligible_for_fire_once = False
                    else:
                        # Daily-or-shorter, missed within 24h, no opt-out → fire once
                        reason = None
                        eligible_for_fire_once = True

                    if not eligible_for_fire_once:
                        # Skip + emit. Always advance next_run_at so we don't
                        # repeat this decision on the next tick.
                        new_next = compute_next_run(schedule, now.isoformat())
                        if new_next:
                            logger.info(
                                "Job '%s' missed at %s (by %ds, reason=%s) — skipping; "
                                "advanced next_run_at to %s",
                                job.get("name", job.get("id", "?")),
                                next_run,
                                missed_seconds,
                                reason,
                                new_next,
                            )
                            for rj in raw_jobs:
                                if rj["id"] == job["id"]:
                                    rj["next_run_at"] = new_next
                                    needs_save = True
                                    break
                            skipped.append({
                                "job_id": job["id"],
                                "name": job.get("name", job["id"]),
                                "missed_at": next_run,
                                "missed_seconds": missed_seconds,
                                "schedule_kind": kind,
                                "reason": reason,
                            })
                        continue  # Don't fall through to due.append

                    # Fire-once eligible (always past grace here — the on-time
                    # gate above handled within-grace fires silently). Log so the
                    # gateway-restart catch-up is visible. next_run_at is left
                    # alone so the scheduler's advance_next_run() (called before
                    # each run) handles advancement and prevents double-firing.
                    logger.info(
                        "Job '%s' missed at %s (by %ds) — firing once on recovery; "
                        "scheduler will advance next_run_at before run.",
                        job.get("name", job.get("id", "?")),
                        next_run,
                        missed_seconds,
                    )
                    # Record the catch-up for cron_triggered. Stamped, not
                    # emitted, because this runs under _jobs_lock(); the
                    # emit happens in get_due_and_skipped_jobs() once the
                    # lock is released. ``job`` is a deepcopy built at the
                    # top of this scan, so the key cannot reach jobs.json.
                    job[_RECOVERY_FIRE_MARKER] = {
                        "missed_at": next_run,
                        "missed_seconds": missed_seconds,
                    }

                # One-shot dispatch-limit guard (issue #38758): a finite one-shot
                # claimed via claim_dispatch() but whose tick died before
                # mark_job_run could remove it will have completed >= times while
                # still looking due (last_run_at was never written, so the
                # recovery helper re-armed it). Remove it instead of re-firing.
                if kind == "once":
                    repeat = job.get("repeat")
                    if repeat:
                        times = repeat.get("times")
                        completed = repeat.get("completed", 0)
                        if times is not None and times > 0 and completed >= times:
                            # A live run must never have its job record deleted
                            # underneath it (#62002): a run that outlives the
                            # run_claim TTL (stream stall, laptop asleep
                            # mid-run) satisfies the same completed >= times +
                            # expired-claim condition as a dead tick, but
                            # mark_job_run() still needs the record to land
                            # last_run_at / last_status / last_delivery_error.
                            # If this process is still running the job, it is
                            # slow, not stale — keep the entry and skip.
                            if _job_running_in_this_process(job.get("id", "")):
                                logger.info(
                                    "Job '%s': dispatch limit reached (%d/%d) "
                                    "but its run is still in flight in this "
                                    "process — keeping entry",
                                    job.get("name", job.get("id", "?")),
                                    completed,
                                    times,
                                )
                                continue
                            logger.info(
                                "Job '%s': one-shot dispatch limit reached (%d/%d) "
                                "— removing stale due entry",
                                job.get("name", job.get("id", "?")),
                                completed,
                                times,
                            )
                            for rj in raw_jobs:
                                if rj["id"] == job["id"]:
                                    raw_jobs.remove(rj)
                                    needs_save = True
                                    break
                            continue

                # Durably claim a one-shot for the DURATION of its run before
                # returning it as due, so a second scheduler process (gateway +
                # desktop both run in-process 60s tickers on one HERMES_HOME)
                # cannot re-dispatch it while the first run is still in flight
                # (#59229). A plain one-shot's due-state is not resolved until
                # mark_job_run() completes it minutes later, so advancing
                # next_run_at by a fixed window is not enough — a job that outlives
                # one tick (e.g. a 2.5-min research prompt) would simply re-fire on
                # the next tick after the window. Instead we stamp a run_claim under
                # the same lock get_due_jobs already holds; the other process reads
                # a fresh claim on its next tick and skips (handled at the top of
                # this loop). mark_job_run() clears the claim on completion. The TTL
                # is only a safety valve: a claiming tick that DIES mid-run leaves a
                # stale claim that expires after the resolved run-claim TTL
                # (_oneshot_run_claim_ttl_seconds, derived from HERMES_CRON_TIMEOUT),
                # so the job is re-dispatched rather than wedged forever.
                if kind == "once":
                    claim = {"at": now.isoformat(), "by": _machine_id()}
                    job["run_claim"] = claim
                    for rj in raw_jobs:
                        if rj["id"] == job["id"]:
                            rj["run_claim"] = claim
                            needs_save = True
                            break

                due.append(job)
        except Exception:
            logger.exception(
                "Skipping malformed cron job %r during due scan",
                job.get("name") or job.get("id") or "?",
            )
            continue

    if needs_save:
        save_jobs(raw_jobs)

    return due, skipped


def get_due_jobs() -> List[Dict[str, Any]]:
    """Backward-compatible wrapper — returns only the due list.

    Existing call sites (15+ tests + scheduler.tick()) continue to work
    unchanged. New call sites that need skipped events should use
    get_due_and_skipped_jobs() directly.
    """
    due, _ = get_due_and_skipped_jobs()
    return due


# Per-run cron output (`cron/output/<job>/<timestamp>.md`) is written once per
# execution. Unlike the quick-snapshot store (`hermes_cli.backup`, capped at 20)
# it had no retention, so a frequently-scheduled job on a long-running deploy
# accumulated one file per run forever and could fill the disk (#52383). Keep the
# most recent N files per job; a non-positive value disables pruning (opt-out).
_CRON_OUTPUT_DEFAULT_KEEP = 50


def _cron_output_keep() -> int:
    """Resolve the per-job output-file retention cap from config (``cron.output_retention``)."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        cron_cfg = cfg.get("cron", {}) if isinstance(cfg, dict) else {}
        return int(cron_cfg.get("output_retention", _CRON_OUTPUT_DEFAULT_KEEP))
    except Exception:
        return _CRON_OUTPUT_DEFAULT_KEEP


def _prune_job_output(job_output_dir: Path, keep: int) -> int:
    """Remove the oldest ``*.md`` run-output files beyond *keep*. Returns count deleted.

    Mirrors the quick-snapshot retention in ``hermes_cli.backup._prune_quick_snapshots``:
    output filenames are timestamp-based (``%Y-%m-%d_%H-%M-%S.md``) so a reverse
    lexical sort orders newest-first, and everything past *keep* is the tail to
    drop. A non-positive *keep* disables pruning. Pruning failures are swallowed
    so they can never break output saving.
    """
    if keep <= 0:
        return 0
    try:
        files = sorted(
            (f for f in job_output_dir.glob("*.md") if f.is_file()),
            key=lambda f: f.name,
            reverse=True,
        )
    except OSError:
        return 0
    deleted = 0
    for stale in files[keep:]:
        try:
            stale.unlink()
            deleted += 1
        except OSError as exc:
            logger.debug("Failed to prune cron output %s: %s", stale.name, exc)
    return deleted


def save_job_output(job_id: str, output: str):
    """Save job output to file."""
    ensure_dirs()
    job_output_dir = _job_output_dir(job_id)
    job_output_dir.mkdir(parents=True, exist_ok=True)
    _secure_dir(job_output_dir)

    timestamp = _hermes_now().strftime("%Y-%m-%d_%H-%M-%S")
    output_file = job_output_dir / f"{timestamp}.md"

    fd, tmp_path = tempfile.mkstemp(dir=str(job_output_dir), suffix='.tmp', prefix='.output_')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(output)
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp_path, output_file)
        _secure_file(output_file)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    # Bound per-job output growth so long-running deploys don't fill the disk (#52383).
    _prune_job_output(job_output_dir, _cron_output_keep())

    return output_file


# =============================================================================
# Skill reference rewriting (curator integration)
# =============================================================================

def referenced_skill_names() -> Set[str]:
    """Return the set of skill names referenced by ANY cron job.

    Includes paused and disabled jobs deliberately: a paused job never
    fires, so its skills never get a ``bump_use`` from the scheduler, yet
    resuming it must still find its skills present. The curator uses this
    set to protect referenced skills from inactivity archival — a skill a
    live job depends on is "in use" regardless of when it was last loaded.

    Best-effort: a corrupt/unreadable jobs store returns an empty set
    rather than raising, so a cron issue can never break the curator.
    """
    try:
        jobs = load_jobs()
    except Exception:
        logger.debug("referenced_skill_names: failed to load cron jobs", exc_info=True)
        return set()

    names: Set[str] = set()
    for job in jobs:
        if not isinstance(job, dict):
            continue
        for name in _normalize_skill_list(job.get("skill"), job.get("skills")):
            cleaned = str(name).strip().lstrip("/")
            if cleaned:
                names.add(cleaned)
    return names


def rewrite_skill_refs(
    consolidated: Optional[Dict[str, str]] = None,
    pruned: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Rewrite cron job skill references after a curator consolidation pass.

    When the curator consolidates a skill X into umbrella Y (or archives X
    as pruned), any cron job that lists ``X`` in its ``skills`` field will
    fail to load ``X`` at run time — the scheduler logs a warning and
    skips the skill, so the job runs without the instructions it was
    scheduled to follow. See cron/scheduler.py where ``skill_view`` is
    called per skill name.

    This function repairs cron jobs in-place:

    - A skill listed in ``consolidated`` is replaced with its umbrella
      target (the ``into`` value). If the umbrella is already in the
      job's skill list, the stale name is dropped without duplication.
    - A skill listed in ``pruned`` is dropped outright — there is no
      forwarding target.
    - Ordering and other skills in the list are preserved.
    - The legacy ``skill`` field is realigned via ``_apply_skill_fields``.

    Args:
        consolidated: mapping of ``old_skill_name -> umbrella_skill_name``.
        pruned: list of skill names that were archived with no forwarding
            target.

    Returns a report dict::

        {
            "rewrites": [
                {
                    "job_id": ...,
                    "job_name": ...,
                    "before": [...],
                    "after": [...],
                    "mapped": {"old": "new", ...},
                    "dropped": ["old", ...],
                },
                ...
            ],
            "jobs_updated": N,
            "jobs_scanned": M,
        }

    Best-effort: exceptions from loading/saving propagate to the caller so
    tests can assert behaviour; the curator invocation site wraps this
    call in a try/except so a failure here never breaks the curator.
    """
    consolidated = dict(consolidated or {})
    pruned_set = set(pruned or [])
    # A skill listed in both wins as "consolidated" — it has a target,
    # which is the more useful of the two outcomes.
    pruned_set -= set(consolidated.keys())

    if not consolidated and not pruned_set:
        return {"rewrites": [], "jobs_updated": 0, "jobs_scanned": 0}

    with _jobs_lock():
        jobs = load_jobs()
        rewrites: List[Dict[str, Any]] = []
        changed = False

        for job in jobs:
            skills_before = _normalize_skill_list(job.get("skill"), job.get("skills"))
            if not skills_before:
                continue

            mapped: Dict[str, str] = {}
            dropped: List[str] = []
            new_skills: List[str] = []

            for name in skills_before:
                if name in consolidated:
                    target = consolidated[name]
                    mapped[name] = target
                    if target and target not in new_skills:
                        new_skills.append(target)
                elif name in pruned_set:
                    dropped.append(name)
                elif name not in new_skills:
                    new_skills.append(name)

            if not mapped and not dropped:
                continue

            job["skills"] = new_skills
            job["skill"] = new_skills[0] if new_skills else None
            changed = True

            rewrites.append({
                "job_id": job.get("id"),
                "job_name": job.get("name") or job.get("id"),
                "before": list(skills_before),
                "after": list(new_skills),
                "mapped": mapped,
                "dropped": dropped,
            })

        if changed:
            save_jobs(jobs)
            logger.info(
                "Curator rewrote skill references in %d cron job(s)", len(rewrites)
            )

        return {
            "rewrites": rewrites,
            "jobs_updated": len(rewrites),
            "jobs_scanned": len(jobs),
        }
