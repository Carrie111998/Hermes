"""CronEventEmitter -- emits lifecycle events from the cron execution pipeline.

Hooks into the cron scheduler's tick()/run_job() cycle to emit:
  - cron_started: before job execution
  - cron_completed: after successful execution
  - cron_failed: after failed execution
  - cron_failed_consecutive: when consecutive failures reach threshold
"""

import logging
from typing import Optional

from events.bus import EventBus
from events.schema import EventType, Priority

logger = logging.getLogger(__name__)

CONSECUTIVE_FAILURE_THRESHOLD = 3


class CronEventEmitter:
    """Emits cron lifecycle events into the EventBus."""

    def __init__(self, bus: EventBus):
        self.bus = bus

    def on_job_started(
        self,
        job_id: str,
        job_name: str,
        schedule: str,
    ) -> str:
        """Emit cron_started event before job execution."""
        return self.bus.emit(
            event_type=EventType.CRON_STARTED,
            source=job_name,
            payload={
                "job_id": job_id,
                "job_name": job_name,
                "schedule": schedule,
            },
        )

    def on_job_completed(
        self,
        job_id: str,
        job_name: str,
        success: bool,
        duration: float,
        output_summary: Optional[str] = None,
        error: Optional[str] = None,
        consecutive_errors: int = 0,
    ) -> str:
        """Emit cron_completed or cron_failed event after job execution.

        If consecutive_errors >= CONSECUTIVE_FAILURE_THRESHOLD, also emits
        cron_failed_consecutive as a separate critical event.
        """
        if success:
            event_id = self.bus.emit(
                event_type=EventType.CRON_COMPLETED,
                source=job_name,
                payload={
                    "job_id": job_id,
                    "job_name": job_name,
                    "duration": duration,
                    "output_summary": output_summary or "",
                },
            )
        else:
            event_id = self.bus.emit(
                event_type=EventType.CRON_FAILED,
                source=job_name,
                payload={
                    "job_id": job_id,
                    "job_name": job_name,
                    "duration": duration,
                    "error": error or "Unknown error",
                    "consecutive_errors": consecutive_errors,
                },
            )

            if consecutive_errors >= CONSECUTIVE_FAILURE_THRESHOLD:
                self.bus.emit(
                    event_type=EventType.CRON_FAILED_CONSECUTIVE,
                    source=job_name,
                    payload={
                        "job_id": job_id,
                        "job_name": job_name,
                        "consecutive_errors": consecutive_errors,
                        "error": error or "Unknown error",
                    },
                )

        return event_id
