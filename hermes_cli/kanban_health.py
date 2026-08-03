"""Kanban board health — queue-drain alert for gated workers.

Monitors the "silent fleet drain" failure mode from the 2026-08-03 incident:
Todo/Blocked work exists on the board, but no runnable workers remain
because every profile that owns pending work is gated — either its session
store is quarantined/unhealthy (the pre-dispatch store health probe from the
DB-hardening work) or the two-failure circuit breaker has tripped on all of
its pending tasks.

The check is intentionally board-level (not per-task like
:mod:`kanban_diagnostics`): the operator signal that was missing during the
incident is "the queue is non-empty AND nobody can run it". A plain "stuck"
warning only fires while ``ready`` work exists; after the breaker trips every
task goes ``blocked`` and the board looks idle even though it is drained.

Design:

* :func:`check_queue_drain` is a pure, read-only function over a kanban
  connection. It computes the pending-work counts, the set of profiles that
  own pending work, which of those profiles could still run a worker, and
  the gate reasons (quarantine / circuit breaker) when none can.
* Quarantine state is consumed through a small provider seam so this module
  never blocks on the sibling pre-dispatch health-probe work landing. A
  default provider reads profile-quarantine events from the board's own
  ``task_events`` stream; task 1 of the hardening epic can additionally
  register a richer provider via :func:`register_quarantine_provider`.
* :func:`format_queue_drain_alert` renders the high-signal alert line that
  callers (gateway dispatcher watcher, ``hermes kanban health``, the legacy
  ``--force`` daemon) emit when the alert fires.

The check is read-only: it never writes to the board. Quarantine marking is
owned by the pre-dispatch probe; this module only reads it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from hermes_cli import kanban_db as kb

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Quarantine provider seam (task 1 of the hardening epic)
# ---------------------------------------------------------------------------

#: Event kinds that mark a profile's store as quarantined/unhealthy. Task 1
#: (pre-dispatch store health probe + quarantine gate) emits these onto the
#: gated task's event stream with a payload carrying ``profile`` and the
#: exact evidence (db path, sqlite error, FTS index name, message row info).
#: This module reads them so the queue-drain alert fires with zero wiring
#: once the probe lands.
QUARANTINE_EVENT_KINDS = (
    "profile_quarantined",
    "profile_store_unhealthy",
)

#: Event kind that clears a profile's quarantine (e.g. an operator repaired
#: the store and ran an un-quarantine). Supported so the default provider
#: can track quarantine → healthy transitions from the event stream alone.
UNQUARANTINE_EVENT_KINDS = (
    "profile_unquarantined",
    "profile_store_healthy",
)


@dataclass
class QuarantineState:
    """Evidence that one profile's session store is quarantined/unhealthy.

    Mirrors the payload contract the pre-dispatch health probe (task 1)
    emits on ``profile_quarantined`` / ``profile_store_unhealthy`` events.
    ``reason`` is a short canonical code (e.g. ``store_unhealthy``,
    ``fts_malformed``); ``detail`` is human-readable; ``db_path`` and
    ``error`` are the exact evidence.
    """

    profile: str
    reason: str = "store_unhealthy"
    detail: str = ""
    db_path: str = ""
    error: str = ""


#: Registry of quarantine providers. Each is a callable
#: ``(conn, profile) -> Optional[QuarantineState]``. The default provider
#: (event-stream scan) is always consulted first; registered providers are
#: consulted in registration order and short-circuit on the first hit.
_QUARANTINE_PROVIDERS: list[Callable[..., Optional[QuarantineState]]] = []


def register_quarantine_provider(
    provider: Callable[..., Optional[QuarantineState]],
) -> None:
    """Register a quarantine-state provider.

    The provider receives the kanban connection and a profile name and
    returns :class:`QuarantineState` when the profile's store is
    quarantined/unhealthy, else ``None``. The pre-dispatch health probe
    (hardening task 1) should call this at import time so the queue-drain
    alert reflects its live quarantine gate; until then the default
    event-stream provider still picks up ``profile_quarantined`` events.
    """
    if provider is not None and provider not in _QUARANTINE_PROVIDERS:
        _QUARANTINE_PROVIDERS.append(provider)


def _parse_event_payload(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _scan_event_stream_quarantine(
    conn: "Any",
    profile: str,
) -> Optional[QuarantineState]:
    """Default provider: read quarantine state from the board's event stream.

    Scans ``task_events`` for quarantine / un-quarantine events mentioning
    ``profile`` and returns the latest state. This is the "reuse quarantine
    state/event streams from task 1 if available" seam — the probe's event
    stream is the board-native record, so no cross-module coupling is
    required for the alert to see it.
    """
    try:
        rows = conn.execute(
            "SELECT kind, payload, created_at FROM task_events "
            "WHERE kind IN (?, ?, ?, ?) ORDER BY id ASC",
            (
                QUARANTINE_EVENT_KINDS[0],
                QUARANTINE_EVENT_KINDS[1],
                UNQUARANTINE_EVENT_KINDS[0],
                UNQUARANTINE_EVENT_KINDS[1],
            ),
        ).fetchall()
    except Exception:
        # Board schema without task_events (or a transient read failure)
        # simply means no quarantine evidence available.
        return None

    quarantined: Optional[QuarantineState] = None
    for row in rows:
        payload = _parse_event_payload(row["payload"])
        event_profile = payload.get("profile") or payload.get("profile_name")
        if event_profile != profile:
            continue
        if row["kind"] in QUARANTINE_EVENT_KINDS:
            quarantined = QuarantineState(
                profile=profile,
                reason=str(payload.get("reason") or "store_unhealthy"),
                detail=str(payload.get("detail") or ""),
                db_path=str(payload.get("db_path") or ""),
                error=str(payload.get("error") or ""),
            )
        else:
            # An un-quarantine event after a quarantine event clears it.
            quarantined = None
    return quarantined


def get_quarantine_state(
    conn: "Any",
    profile: str,
) -> Optional[QuarantineState]:
    """Return quarantine evidence for ``profile``, or None when healthy.

    Consulted providers, in order: registered providers (task 1 seam) then
    the default event-stream scanner. First hit wins.
    """
    for provider in _QUARANTINE_PROVIDERS:
        try:
            state = provider(conn, profile)
        except Exception:
            logger.debug(
                "kanban health: quarantine provider %r failed for %r",
                getattr(provider, "__name__", provider), profile, exc_info=True,
            )
            continue
        if state is not None:
            return state
    try:
        return _scan_event_stream_quarantine(conn, profile)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Queue-drain check
# ---------------------------------------------------------------------------

#: Statuses that count as "pending work" for the runnable-worker calculus.
#: A ready or review task means the dispatcher *will* try to spawn a worker
#: for its assignee; todo/blocked tasks are the backlog that cannot be
#: processed while every owner is gated.
_PENDING_STATUSES = ("todo", "blocked", "ready", "review")

#: Statuses that count toward the alert's headline "Todo/Blocked" figure.
_BACKLOG_STATUSES = ("todo", "blocked")


@dataclass
class QueueDrainReport:
    """Result of a board-level queue-drain inspection."""

    board: str = "default"
    todo_blocked_count: int = 0
    pending_profiles: list = field(default_factory=list)
    runnable_profiles: list = field(default_factory=list)
    quarantined_profiles: list = field(default_factory=list)
    breaker_tripped_profiles: list = field(default_factory=list)
    breaker_tripped_tasks: int = 0
    #: Canonical gate reasons present: subset of {"quarantine",
    #: "circuit_breaker"}.
    reasons: list = field(default_factory=list)

    @property
    def should_alert(self) -> bool:
        """True when backlog exists and no runnable worker remains because
        of a known gate (quarantine and/or circuit breaker)."""
        return (
            self.todo_blocked_count > 0
            and not self.runnable_profiles
            and bool(self.reasons)
        )

    def to_dict(self) -> dict:
        return {
            "board": self.board,
            "todo_blocked_count": self.todo_blocked_count,
            "pending_profiles": list(self.pending_profiles),
            "runnable_profiles": list(self.runnable_profiles),
            "quarantined_profiles": list(self.quarantined_profiles),
            "breaker_tripped_profiles": list(self.breaker_tripped_profiles),
            "breaker_tripped_tasks": self.breaker_tripped_tasks,
            "reasons": list(self.reasons),
            "should_alert": self.should_alert,
        }


def _effective_failure_limit(row: "Any", fallback: int) -> int:
    """Per-task breaker threshold: ``max_retries`` override else fallback."""
    try:
        if "max_retries" in row.keys() and row["max_retries"] is not None:
            return int(row["max_retries"])
    except Exception:
        pass
    return int(fallback)


def _profile_exists(name: str) -> bool:
    """Real-profile check with the same fallback as ``has_spawnable_ready``."""
    try:
        from hermes_cli.profiles import profile_exists  # local import: avoids cycle
        return bool(profile_exists(name))
    except Exception:
        # Can't introspect — assume the profile exists (legacy behavior).
        return True


def check_queue_drain(
    conn: "Any",
    *,
    board: str = "default",
    failure_limit: Optional[int] = None,
    profile_exists_fn: Optional[Callable[[str], bool]] = None,
    quarantine_provider: Optional[Callable[..., Optional[QuarantineState]]] = None,
) -> QueueDrainReport:
    """Inspect the board and decide whether a queue-drain alert should fire.

    Read-only. Computes:

    * ``todo_blocked_count`` — tasks in ``todo`` or ``blocked`` status.
    * Pending profiles — distinct assignees of any pending task
      (``todo`` / ``blocked`` / ``ready`` / ``review``).
    * Runnable profiles — pending profiles that map to a real Hermes
      profile, are not quarantined, and are not fully circuit-breaker gated.
    * Gate reasons — ``quarantine`` when any pending profile is
      quarantined; ``circuit_breaker`` when at least one pending task has
      tripped the two-failure breaker (``consecutive_failures`` at/over the
      effective limit).

    The alert fires when all of the following hold (see
    :attr:`QueueDrainReport.should_alert`):

    1. at least one Todo or Blocked task exists,
    2. no runnable worker remains,
    3. the reason no worker remains is quarantine and/or circuit breaker.

    ``failure_limit`` mirrors the dispatcher's ``kanban.failure_limit``
    (default ``DEFAULT_FAILURE_LIMIT``). ``profile_exists_fn`` and
    ``quarantine_provider`` are injectable for tests; the defaults match the
    dispatcher's own profile/health semantics.
    """
    if failure_limit is None:
        failure_limit = kb.DEFAULT_FAILURE_LIMIT
    failure_limit = int(failure_limit)  # type: ignore[arg-type]
    profile_exists = profile_exists_fn or _profile_exists

    report = QueueDrainReport(board=board)
    try:
        rows = conn.execute(
            "SELECT id, assignee, status, consecutive_failures, max_retries "
            "FROM tasks WHERE status IN (?, ?, ?, ?) "
            "ORDER BY id ASC",
            tuple(_PENDING_STATUSES),
        ).fetchall()
    except Exception:
        # Board unreadable — the dispatcher's own corrupt-board handling
        # owns that signal; do not fire a queue-drain alert on top of it.
        return report

    pending_rows = [r for r in rows if r["assignee"]]
    report.todo_blocked_count = sum(
        1 for r in rows if r["status"] in _BACKLOG_STATUSES
    )

    # Group pending tasks by assignee, tracking the breaker state per task.
    by_profile: dict[str, list] = {}
    for r in pending_rows:
        by_profile.setdefault(r["assignee"], []).append(r)

    breaker_tripped_tasks = 0
    breaker_tripped_profiles: list[str] = []
    for profile, task_rows in sorted(by_profile.items()):
        # A profile is breaker-gated when EVERY pending task it owns has
        # tripped the circuit breaker. A single non-tripped pending task
        # (e.g. a fresh ready card, or a todo waiting on parents) means the
        # dispatcher can still make progress for this profile.
        tripped = [
            r for r in task_rows
            if int(r["consecutive_failures"] or 0)
            >= _effective_failure_limit(r, failure_limit)
        ]
        breaker_tripped_tasks += len(tripped)
        if tripped and len(tripped) == len(task_rows):
            breaker_tripped_profiles.append(profile)

    # Quarantine evidence per profile.
    quarantined_profiles: list[str] = []
    for profile in sorted(by_profile):
        provider = quarantine_provider or (lambda c, p: get_quarantine_state(c, p))
        try:
            state = provider(conn, profile)
        except Exception:
            state = None
        if state is not None:
            quarantined_profiles.append(profile)

    # Runnable: real profile, not quarantined, not fully breaker-gated.
    runnable_profiles: list[str] = []
    for profile in sorted(by_profile):
        if profile in quarantined_profiles:
            continue
        if profile in breaker_tripped_profiles:
            continue
        if not profile_exists(profile):
            # Control-plane lane (e.g. orion-cc) — never auto-spawned.
            continue
        runnable_profiles.append(profile)

    report.pending_profiles = sorted(by_profile)
    report.runnable_profiles = runnable_profiles
    report.quarantined_profiles = quarantined_profiles
    report.breaker_tripped_profiles = breaker_tripped_profiles
    report.breaker_tripped_tasks = breaker_tripped_tasks
    if quarantined_profiles:
        report.reasons.append("quarantine")
    if breaker_tripped_profiles:
        report.reasons.append("circuit_breaker")
    return report


# ---------------------------------------------------------------------------
# Alert emission
# ---------------------------------------------------------------------------

def format_queue_drain_alert(report: QueueDrainReport) -> str:
    """Render the high-signal, one-line queue-drain alert.

    Includes the three required figures: number of Todo/Blocked tasks,
    number of quarantined profiles, and circuit-breaker state.
    """
    parts = [
        f"KANBAN QUEUE DRAIN [{report.board}]",
        f"{report.todo_blocked_count} todo/blocked task(s) pending",
        "0 runnable workers",
    ]
    if report.quarantined_profiles:
        parts.append(
            f"{len(report.quarantined_profiles)} quarantined profile(s) "
            f"({', '.join(report.quarantined_profiles)})"
        )
    if report.breaker_tripped_profiles:
        parts.append(
            f"circuit breaker tripped on {len(report.breaker_tripped_profiles)} "
            f"profile(s)/{report.breaker_tripped_tasks} task(s) "
            f"({', '.join(report.breaker_tripped_profiles)})"
        )
    return "; ".join(parts) + "."


def emit_queue_drain_alert(
    report: QueueDrainReport,
    *,
    log: Optional[logging.Logger] = None,
) -> str:
    """Log a queue-drain alert at ERROR and return the formatted line.

    Callers rate-limit how often they invoke this (the dispatcher watcher
    uses a 5-minute cooldown per board) so a persistent drain produces a
    bounded stream of alerts rather than one per tick.
    """
    line = format_queue_drain_alert(report)
    (log or logger).error("%s", line)
    return line
