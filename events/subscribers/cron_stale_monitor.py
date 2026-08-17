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

It ALSO watches the ticker heartbeat itself (``_check_ticker_stale``).  The
job-level check above is blind to a dead *scheduler*: it only ever alerts on a
job that STARTED and never finished, so when the ticker thread itself dies no
CRON_STARTED is ever emitted, ``_open_jobs`` stays empty and nothing fires.
That is precisely how the 2026-08-11 outage ran 5h08m — the gateway process
alive and this subscriber polling beside it — with zero of 69 cron jobs
running.  The ticker heartbeat file is the one signal that survives that
failure, because nothing refreshes it once the thread is gone.
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
        # A gateway shutdown kills its in-flight crons. Without this the
        # monitor cannot tell those from a wedge: it keeps them in
        # ``_open_jobs`` and fires a generic HIGH cron_stale ~20 minutes
        # later, a false "stuck job" alarm for a run the gateway itself
        # stopped. See ``_resolve_gateway_stopped``.
        EventType.GATEWAY_STOPPED,
    ]

    # Default threshold raised from 600 → 1200 after the 2026-04-19 flood
    # revealed that 10 minutes is tight for ML-training / skill-evolution
    # jobs that routinely run 15–30 minutes.  Per-job overrides live in
    # ~/.hermes/notifications/cron_stale_thresholds.json and are injected
    # via the constructor by gateway_integration.
    STALE_THRESHOLD_SECONDS: int = 1200

    # How stale the ticker heartbeat may get before we call the scheduler dead.
    # The in-process ticker beats once per loop iteration at interval=60s, so
    # 300s is five consecutive missed beats — comfortably past a slow tick under
    # memory pressure, far short of the 5h08m outage this exists to catch.
    TICKER_STALE_THRESHOLD_SECONDS: int = 300

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
        # cron_started event_id -> job_id. GATEWAY_STOPPED identifies the runs
        # it killed by *correlation id* (the cron_started event_id, matching
        # the prior_cron_started_event_id convention), while _open_jobs is
        # keyed by job_id — this bridges the two. Bounded by the number of
        # open runs: an entry is dropped on the terminal event, on a fresh
        # start of the same job, and on shutdown resolution.
        self._started_event_ids: Dict[str, str] = {}
        # One alert per ticker outage; cleared when the heartbeat goes fresh
        # again so a SECOND outage still alerts.
        self._ticker_alerted: bool = False
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

    def _forget_started_ids_for(self, job_id: str) -> None:
        """Drop every correlation id pointing at ``job_id`` (small dict)."""
        for eid in [k for k, v in self._started_event_ids.items() if v == job_id]:
            self._started_event_ids.pop(eid, None)

    def _resolve_gateway_stopped(self, event: Event) -> None:
        """Close the runs this shutdown killed, attributed as such.

        ``gateway/run.py`` stamps GATEWAY_STOPPED with the cron_started
        event_ids of everything still in flight (``cron/inflight.py``). Each
        one that we are still tracking is reported ONCE, immediately, with
        ``scope="gateway_stopped"`` — mirroring the ``scope="ticker"`` idiom in
        ``_check_ticker_stale`` — and removed from ``_open_jobs`` so the
        generic HIGH-priority wedge alert never fires for it.

        Priority is NORMAL, not HIGH: a run stopped by a deliberate restart is
        explained, so it belongs in the record rather than in the paging tier.
        """
        raw = event.payload.get("inflight_cron_correlation_ids") or []
        if not isinstance(raw, (list, tuple)):
            return
        exit_reason = event.payload.get("exit_reason")
        now = datetime.now(timezone.utc)

        for correlation_id in raw:
            job_id = self._started_event_ids.pop(correlation_id, None)
            if job_id is None:
                continue
            entry = self._open_jobs.pop(job_id, None)
            self._alerted.discard(job_id)
            if entry is None:
                continue
            started_at, job_name = entry
            try:
                age = (now - started_at).total_seconds()
            except (TypeError, ValueError):
                age = 0.0
            try:
                self.bus.emit(
                    event_type=EventType.CRON_STALE,
                    source="cron-stale-monitor",
                    payload={
                        "job_id": job_id,
                        "job_name": job_name,
                        "scope": "gateway_stopped",
                        "exit_reason": exit_reason,
                        "age_seconds": int(age),
                        "gateway_stopped_event_id": event.event_id,
                        "cron_started_event_id": correlation_id,
                    },
                    priority=Priority.NORMAL,
                )
                logger.info(
                    "CronStaleMonitor: %s (%s) was cut short by gateway "
                    "shutdown (%s) after %ds",
                    job_name, job_id, exit_reason, int(age),
                )
            except Exception:
                logger.exception(
                    "CronStaleMonitor: failed to emit gateway_stopped "
                    "cron_stale for %s", job_id,
                )

    def handle(self, event: Event) -> None:
        # BEFORE the job_id guard: GATEWAY_STOPPED carries no job_id, so
        # handling it after that guard would silently do nothing.
        if event.event_type == EventType.GATEWAY_STOPPED:
            self._resolve_gateway_stopped(event)
            return

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
            # A fresh run supersedes any earlier correlation id for this job.
            self._forget_started_ids_for(job_id)
            self._started_event_ids[event.event_id] = job_id
        elif event.event_type in (EventType.CRON_COMPLETED, EventType.CRON_FAILED):
            self._open_jobs.pop(job_id, None)
            self._alerted.discard(job_id)
            # It finished on its own, so a later shutdown did not kill it.
            self._forget_started_ids_for(job_id)

    def poll(self) -> int:
        count = super().poll()
        self._check_stale()
        self._check_ticker_stale()
        return count

    def _check_ticker_stale(self) -> None:
        """Alert when the cron ticker's heartbeat stops advancing.

        ``_check_stale`` above can only see a job that STARTED and never
        finished. If the ticker THREAD dies, no job ever starts, so it stays
        silent forever — that is how the 2026-08-11 outage ran 5h08m unnoticed
        while this subscriber polled happily beside it.

        ``record_ticker_heartbeat()`` is written once per tick-loop iteration,
        so its age is the one signal that survives a dead ticker: nothing
        refreshes the file and it ages without bound. Read it here, where cron
        health is already being judged.
        """
        try:
            # Imported lazily: cron.jobs is a heavy module and the events
            # package must stay cheap to import.
            import cron.jobs

            age = cron.jobs.get_ticker_heartbeat_age()
        except Exception:
            logger.exception("CronStaleMonitor: could not read ticker heartbeat")
            return

        # None = "cannot determine" (older build, never ran, torn read) — the
        # documented contract of get_ticker_heartbeat_age(). Not evidence of
        # death; alerting here would fire on every fresh install.
        if age is None:
            return

        if age <= self.TICKER_STALE_THRESHOLD_SECONDS:
            self._ticker_alerted = False
            return

        if self._ticker_alerted:
            return

        try:
            self.bus.emit(
                event_type=EventType.CRON_STALE,
                source="cron-stale-monitor",
                payload={
                    # No real job is stuck — the scheduler itself is gone. The
                    # sentinel keeps consumers that format job identity working
                    # while `scope` lets them tell the two apart.
                    "job_id": "__ticker__",
                    "job_name": "cron-ticker",
                    "scope": "ticker",
                    "age_seconds": int(age),
                    "threshold_seconds": self.TICKER_STALE_THRESHOLD_SECONDS,
                },
                priority=Priority.CRITICAL,
            )
            self._ticker_alerted = True
            logger.error(
                "CronStaleMonitor: cron ticker heartbeat is %ds old (threshold "
                "%ds) — the scheduler thread is not running; NO cron job can "
                "fire until the gateway is restarted",
                int(age), self.TICKER_STALE_THRESHOLD_SECONDS,
            )
        except Exception:
            logger.exception("CronStaleMonitor: failed to emit ticker cron_stale")

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
