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
from typing import Any, Dict, Optional

from events.bus import EventBus
from events.schema import EventType

logger = logging.getLogger(__name__)

PAUSED = "paused"
RESUMED = "resumed"

_EVENT_TYPE_BY_ACTION = {
    PAUSED: EventType.CRON_PAUSED,
    RESUMED: EventType.CRON_RESUMED,
}


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

    payload: Dict[str, Any] = {
        "job_id": job_id,
        "job_name": job_name,
        "caller": caller,
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
            "caller=%s",
            job_id, action, caller,
        )
        return None
