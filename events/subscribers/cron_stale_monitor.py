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
from typing import Dict, List, Optional, Set, Tuple

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
        # Guard #1 (2026-04-30): cron_aborted on gateway shutdown is a
        # terminal event for the stale-monitor's purposes -- clears
        # _open_jobs / _alerted just like cron_completed / cron_failed.
        EventType.CRON_ABORTED,
    ]

    # Default threshold raised from 600 → 1200 after the 2026-04-19 flood
    # revealed that 10 minutes is tight for ML-training / skill-evolution
    # jobs that routinely run 15–30 minutes.  Per-job overrides live in
    # ~/.hermes/notifications/cron_stale_thresholds.json and are injected
    # via the constructor by gateway_integration.
    STALE_THRESHOLD_SECONDS: int = 1200

    def __init__(
        self,
        bus: EventBus,
        default_threshold_seconds: Optional[int] = None,
        per_job_thresholds: Optional[Dict[str, int]] = None,
    ):
        super().__init__(bus)
        # (started_at, job_name) — job_name is the key for override lookup
        self._open_jobs: Dict[str, Tuple[datetime, str]] = {}
        self._alerted: Set[str] = set()
        self._default_threshold = (
            default_threshold_seconds
            if default_threshold_seconds is not None
            else self.STALE_THRESHOLD_SECONDS
        )
        self._per_job_thresholds = dict(per_job_thresholds or {})

    def _threshold_for(self, job_name: str) -> int:
        if job_name and job_name in self._per_job_thresholds:
            return self._per_job_thresholds[job_name]
        return self._default_threshold

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
            job_name = event.payload.get("job_name") or event.source or job_id
            self._open_jobs[job_id] = (started_at, job_name)
            self._alerted.discard(job_id)
        elif event.event_type in (
            EventType.CRON_COMPLETED,
            EventType.CRON_FAILED,
            EventType.CRON_ABORTED,
        ):
            self._open_jobs.pop(job_id, None)
            self._alerted.discard(job_id)

    def poll(self) -> int:
        count = super().poll()
        self._check_stale()
        return count

    def _check_stale(self) -> None:
        now = datetime.now(timezone.utc)
        for job_id, (started_at, job_name) in list(self._open_jobs.items()):
            if job_id in self._alerted:
                continue
            age = (now - started_at).total_seconds()
            threshold = self._threshold_for(job_name)
            if age < threshold:
                continue
            try:
                self.bus.emit(
                    event_type=EventType.CRON_STALE,
                    source="cron-stale-monitor",
                    payload={
                        "job_id": job_id,
                        "job_name": job_name,
                        "age_seconds": int(age),
                        "threshold_seconds": threshold,
                    },
                    priority=Priority.HIGH,
                )
                self._alerted.add(job_id)
            except Exception:
                logger.exception(
                    "CronStaleMonitor: failed to emit cron_stale for %s", job_id,
                )
