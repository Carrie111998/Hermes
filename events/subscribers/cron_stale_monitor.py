"""CronStaleMonitor subscriber (SR-106) — detects cron jobs that never finish.

Watches CRON_STARTED / CRON_COMPLETED / CRON_FAILED and emits a CRON_STALE
event when a started job has no matching completion after STALE_THRESHOLD_SECONDS.
At most one CRON_STALE is emitted per stuck run; a subsequent CRON_COMPLETED
or CRON_FAILED for the same job_id clears the state so a *new* run that also
goes stale will alert again.

Why this exists: scheduler.run_job() invokes agents synchronously in-process,
so there is no subprocess heartbeat we can read.  If the job thread wedges or
the gateway dies mid-run, CRON_STARTED is recorded but the matching terminal
event never arrives.  This subscriber closes that observability gap.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from events.bus import EventBus
from events.schema import Event, EventType, Priority
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)


class CronStaleMonitor(BaseSubscriber):
    subscriber_id = "cron-stale-monitor"
    poll_interval_seconds = 60
    event_types: Optional[List[EventType]] = [
        EventType.CRON_STARTED,
        EventType.CRON_COMPLETED,
        EventType.CRON_FAILED,
    ]

    STALE_THRESHOLD_SECONDS: int = 600

    def __init__(self, bus: EventBus):
        super().__init__(bus)
        self._open_jobs: Dict[str, datetime] = {}
        self._alerted: Set[str] = set()

    def handle(self, event: Event) -> None:
        job_id = event.payload.get("job_id")
        if not job_id:
            return

        if event.event_type == EventType.CRON_STARTED:
            try:
                started_at = datetime.fromisoformat(event.timestamp)
            except ValueError:
                logger.warning(
                    "CronStaleMonitor: unparseable timestamp %r on %s",
                    event.timestamp, event.event_id,
                )
                return
            self._open_jobs[job_id] = started_at
            self._alerted.discard(job_id)
        elif event.event_type in (EventType.CRON_COMPLETED, EventType.CRON_FAILED):
            self._open_jobs.pop(job_id, None)
            self._alerted.discard(job_id)

    def poll(self) -> int:
        count = super().poll()
        self._check_stale()
        return count

    def _check_stale(self) -> None:
        now = datetime.now(timezone.utc)
        for job_id, started_at in list(self._open_jobs.items()):
            if job_id in self._alerted:
                continue
            age = (now - started_at).total_seconds()
            if age < self.STALE_THRESHOLD_SECONDS:
                continue
            try:
                self.bus.emit(
                    event_type=EventType.CRON_STALE,
                    source="cron-stale-monitor",
                    payload={
                        "job_id": job_id,
                        "age_seconds": int(age),
                        "threshold_seconds": self.STALE_THRESHOLD_SECONDS,
                    },
                    priority=Priority.HIGH,
                )
                self._alerted.add(job_id)
            except Exception:
                logger.exception(
                    "CronStaleMonitor: failed to emit cron_stale for %s", job_id,
                )
