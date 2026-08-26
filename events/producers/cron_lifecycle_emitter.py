"""Cron lifecycle emitter — one event per pause/resume state transition.

The sibling of :mod:`events.producers.cron_trigger_emitter`. That one records
a fire the schedule did not ask for; this one records the schedule being
switched off and back on.

Called by ``cron.jobs.pause_job`` / ``resume_job`` (and by ``trigger_job``,
which implicitly un-pauses) AFTER the job record has been written. Defensive
for the same reason as the trigger emitter: an unhealthy event bus must never
be able to break a pause. The state mutation is already durable by the time
this runs, so a swallowed failure is a missing audit record, never a job left
in a half-changed state.
"""

import logging
import os
import sys
from typing import Any, Dict, Optional, Tuple

from events.bus import EventBus
from events.schema import EventType

logger = logging.getLogger(__name__)

PAUSED = "paused"
RESUMED = "resumed"

_EVENT_TYPE_BY_ACTION = {
    PAUSED: EventType.CRON_PAUSED,
    RESUMED: EventType.CRON_RESUMED,
}

#: ``caller_source`` values. THREADED means a call site passed an explicit
#: caller; DERIVED means nobody did and the emitter reconstructed one.
THREADED = "threaded"
DERIVED = "derived"

#: Env var an out-of-repo script can export to attribute itself without being
#: edited. A fallback only — it never overrides a threaded caller.
CALLER_ENV_VAR = "HERMES_CALLER"

#: Last resort. Named rather than null so the *reason* attribution is missing
#: is legible: no caller threaded, no env set, no usable ``sys.argv[0]``.
UNKNOWN_SCRIPT = "script:unknown"


def resolve_caller(caller: Optional[str]) -> Tuple[str, str]:
    """Return ``(caller, caller_source)``, never a null or blank caller.

    ``caller`` is the single field the whole CRON_PAUSED/CRON_RESUMED feature
    exists to provide, and until 2026-08-26 it could be null: ``pause_job`` /
    ``resume_job`` / ``trigger_job`` all warn on an anonymous call but do not
    refuse, so any code importing ``cron.jobs`` and calling ``resume_job(jid)``
    bare wrote a well-formed, audit-logged, *unattributed* event. That is not
    hypothetical — three Gate-2 containment-hold jobs were released that way at
    01:17 EDT and landed on the bus as ``caller=None`` while every sanctioned
    resume beside them read ``hermes_cli:cron_resume``. The event's ``source``
    field carries the JOB NAME, not the actor, so it cannot substitute.

    Resolution order, first non-blank wins:

    1. the threaded ``caller`` argument       -> ``THREADED``
    2. ``$HERMES_CALLER``                     -> ``DERIVED``
    3. ``script:<basename(sys.argv[0])>``     -> ``DERIVED``
    4. :data:`UNKNOWN_SCRIPT`                 -> ``DERIVED``

    Two properties are load-bearing:

    ``caller_source`` travels with the value. A derived caller is real evidence
    (for the incident above it reads ``script:gate2_resume_barrier_set.py``,
    which names the actor) but it is weaker than a threaded one, and a reader
    who cannot tell them apart is worse off than one staring at an obvious
    null. Never label a derivation ``THREADED``.

    The env var is a fallback, NOT an override. If ``$HERMES_CALLER`` could
    rewrite a caller a call site threaded correctly, ambient process state
    could forge attribution — strictly worse than the null it replaces.

    Deliberately NOT a required-keyword refusal. That design was tried on
    2026-08-25 for ``pause_jobs_cas``/``restore_jobs_cas`` on the premise that
    they had no landed consumer; the premise was false, the tracked tracker
    quarantine adapter called both without a caller, and it raised TypeError
    unnoticed for a day. A signature break lands on exactly the out-of-repo
    scripts that are the hole, at runtime, mid-operation. The anonymous-call
    warnings in ``cron.jobs`` stay: deriving a value makes the record usable,
    it does not make threading a caller optional in new code.
    """
    if isinstance(caller, str) and caller.strip():
        return caller, THREADED

    env = os.environ.get(CALLER_ENV_VAR)
    if isinstance(env, str) and env.strip():
        return env.strip(), DERIVED

    argv = getattr(sys, "argv", None) or []
    argv0 = argv[0] if argv else ""
    basename = os.path.basename(str(argv0).strip().rstrip("/\\")).strip()
    if basename:
        return f"script:{basename}", DERIVED

    return UNKNOWN_SCRIPT, DERIVED


def emit_cron_lifecycle(
    bus: EventBus,
    *,
    action: str,
    job_id: str,
    job_name: str,
    caller: Optional[str],
    reason: Optional[str],
    paused_at: Optional[str],
    previous_state: Optional[str],
    new_state: Optional[str],
    next_run_at: Optional[str] = None,
) -> Optional[str]:
    """Emit one CRON_PAUSED/CRON_RESUMED event for a lifecycle transition.

    ``reason`` is the PAUSE's why in both directions — on resume it carries the
    reason being retired (the value ``_unpause_updates`` archives into
    ``paused_history``), so a pause/resume span reads the same from either end.

    ``previous_state`` is the job's ``state`` as observed BEFORE the write. It
    is what separates a genuine transition from a repeat pause of an already-
    paused job, which is the distinction the 2026-08-24/25 churn investigation
    needed and could not get from the job record alone.

    Returns the event_id on success, None on failure (logged but swallowed).
    """
    event_type = _EVENT_TYPE_BY_ACTION.get(action)
    if event_type is None:
        # A caller-side typo must not become a silently-dropped audit record.
        logger.error(
            "cron_lifecycle_emitter: unknown action=%r for job_id=%s "
            "(expected one of %s)",
            action, job_id, sorted(_EVENT_TYPE_BY_ACTION),
        )
        return None

    # Resolved here rather than in cron.jobs so the invariant holds at the last
    # choke point before the bus: every one of the five lifecycle paths, and
    # any future one that calls this emitter directly, gets it for free.
    resolved_caller, caller_source = resolve_caller(caller)

    payload: Dict[str, Any] = {
        "job_id": job_id,
        "job_name": job_name,
        "caller": resolved_caller,
        "caller_source": caller_source,
        "action": action,
        "reason": reason,
        "paused_at": paused_at,
        "previous_state": previous_state,
        "new_state": new_state,
    }
    if action == RESUMED:
        payload["next_run_at"] = next_run_at

    try:
        return bus.emit(
            event_type=event_type,
            source=job_name,
            payload=payload,
            job_id=job_id,
        )
    except Exception:
        logger.exception(
            "cron_lifecycle_emitter: emit failed for job_id=%s action=%s "
            "caller=%s (%s)",
            job_id, action, resolved_caller, caller_source,
        )
        return None
