"""Cron trigger emitter — writes one cron_triggered event per off-schedule fire.

Called by cron.jobs.trigger_job() after the job's next_run_at has been
written to NOW. Defensive: any bus failure is logged and swallowed so
that an unhealthy event bus never breaks the trigger path itself.
"""

import logging
from typing import Optional

from events.bus import EventBus
from events.schema import EventType

logger = logging.getLogger(__name__)


def emit_cron_triggered(
    bus: EventBus,
    *,
    job_id: str,
    job_name: str,
    caller: Optional[str],
    reason: Optional[str],
    previous_next_run_at: Optional[str],
    new_next_run_at: str,
) -> Optional[str]:
    """Emit one CRON_TRIGGERED event capturing the caller + state transition.

    Returns the event_id on success, None on failure (logged but swallowed).
    """
    try:
        return bus.emit(
            event_type=EventType.CRON_TRIGGERED,
            source=job_name,
            payload={
                "job_id": job_id,
                "job_name": job_name,
                "caller": caller,
                "reason": reason,
                "previous_next_run_at": previous_next_run_at,
                "new_next_run_at": new_next_run_at,
            },
            job_id=job_id,
        )
    except Exception:
        logger.exception(
            "cron_trigger_emitter: emit failed for job_id=%s caller=%s",
            job_id, caller,
        )
        return None
