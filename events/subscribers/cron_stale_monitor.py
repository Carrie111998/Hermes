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

Three checks, on three clocks.  ``_check_stale`` and ``_check_ticker_stale``
run per poll, on this process's own state.  The third runs ONCE, in
``startup()``: it rebuilds the shutdown attributions a force-killed
predecessor never got to write, by querying the bus rather than by handling
events.  That one needs no in-memory state at all, which is exactly why it
works where ``handle()`` cannot.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

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

    # How far back a fresh gateway looks for shutdowns it must still attribute
    # (see startup()). Bounded on purpose: event_bus.db is hundreds of MB and
    # cron events dominate it, so an unbounded sweep would scan the whole bus
    # on every boot. 24h covers an overnight outage and every ordinary restart
    # gap; anything older is reported as excluded rather than dropped silently.
    ATTRIBUTION_HORIZON_SECONDS: int = 86_400

    # How far past the horizon to look when counting what the horizon excluded.
    # Also bounded — the point is to tell the operator "N shutdowns were too old
    # to examine", not to enumerate the entire history of the box.
    HORIZON_REPORT_WINDOW_SECONDS: int = 7 * 86_400

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
        # Shutdown attributions staged by _resolve_gateway_stopped and emitted
        # by shutdown(). Bounded by the number of runs one shutdown reports,
        # and drained on flush. See _resolve_gateway_stopped for why the report
        # cannot be made at the moment the shutdown event is seen.
        self._pending_shutdown: List[Dict[str, Any]] = []
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

    def _drop_pending_shutdown_for(self, job_id: str) -> None:
        """Cancel a staged attribution because the run reached a terminal event.

        The gateway takes its in-flight snapshot EARLY in teardown, so a run
        that is merely *unfinished* at that instant can still land while the
        gateway drains. Once it does, it was not killed by the shutdown.
        """
        self._pending_shutdown = [
            p for p in self._pending_shutdown if p["job_id"] != job_id
        ]

    def _resolve_gateway_stopped(self, event: Event) -> None:
        """STAGE the runs this shutdown may have killed; do not report yet.

        ``gateway/run.py`` stamps GATEWAY_STOPPED with the cron_started
        event_ids of everything still in flight (``cron/inflight.py``), and it
        takes that snapshot EARLY in ``_stop_impl_body`` — before the gateway
        drains its work. It has to stay there: a teardown force-killed past
        ``_TASKKILL_TIMEOUT_S`` would otherwise emit no GATEWAY_STOPPED at all.

        So "in flight when the stop began" is a weaker claim than "killed by
        the stop", and reporting on sight gets it wrong for any run that
        finishes while the gateway tears down. Production, 2026-08-17:
        jobflow-researcher was reported killed at 05:31:30 and emitted
        cron_completed at 05:32:05. The report is therefore staged here and
        flushed in :meth:`shutdown` — the last moment before the process
        exits, and the first at which the answer is final.

        The SUPPRESSION is not deferred, only the report: the entry leaves
        ``_open_jobs`` now, so the generic HIGH wedge alert cannot fire for it
        in the polls between here and the flush.
        """
        raw = event.payload.get("inflight_cron_correlation_ids") or []
        if not isinstance(raw, (list, tuple)):
            return
        exit_reason = event.payload.get("exit_reason")
        try:
            stopped_at = datetime.fromisoformat(event.timestamp)
        except (TypeError, ValueError):
            # A corrupt stamp is no reason to report nothing; the poll clock
            # only inflates the age, which is the pre-fix behaviour.
            logger.warning(
                "CronStaleMonitor: unparseable timestamp %r on %s; measuring "
                "the shutdown age from the poll clock",
                event.timestamp, event.event_id,
            )
            stopped_at = datetime.now(timezone.utc)

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
                age = max(0.0, (stopped_at - started_at).total_seconds())
            except (TypeError, ValueError):
                age = 0.0
            self._pending_shutdown.append({
                "job_id": job_id,
                "job_name": job_name,
                # From the GATEWAY_STOPPED's OWN stamp — not the flush clock
                # (2a4ece2c07) and not this poll's clock either. age_seconds
                # means "how far into the run did the shutdown land", so both
                # of those redefine it: the flush adds however long teardown
                # took, and staging adds up to a whole poll_interval_seconds
                # (60). Production, 2026-08-19: a 14s-old run was reported at
                # 68, against a 1200s wedge threshold. Matches
                # _age_at_shutdown, the successor-side path, clamp included.
                "age_seconds": int(age),
                "exit_reason": exit_reason,
                "gateway_stopped_event_id": event.event_id,
                "cron_started_event_id": correlation_id,
            })

    def _flush_pending_shutdown(self) -> None:
        """Report the staged runs that never reached a terminal event.

        Drains the queue first, so a second call (or a failed emit) cannot
        double-report.
        """
        pending, self._pending_shutdown = self._pending_shutdown, []
        if not pending:
            return
        for record in pending:
            job_id = record["job_id"]
            job_name = record["job_name"]
            exit_reason = record["exit_reason"]
            # Computed from the GATEWAY_STOPPED's own stamp, not now — see
            # the staging site. The flush may be minutes later.
            age = record["age_seconds"]
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
                        "gateway_stopped_event_id": record["gateway_stopped_event_id"],
                        "cron_started_event_id": record["cron_started_event_id"],
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

    # ------------------------------------------------------------------
    # Successor-side reconstruction
    # ------------------------------------------------------------------

    def startup(self) -> None:
        """Rebuild shutdown attributions the previous gateway never recorded.

        ``_flush_pending_shutdown`` only runs on a GRACEFUL teardown. A gateway
        force-killed past ``gateway/status.py``'s ``_TASKKILL_TIMEOUT_S``, or
        cut down by the shutdown watchdog's ``exit_code=1``, reaches neither
        ``_drain_subscribers_for_shutdown()`` nor ``shutdown_all()`` — the
        staged reports die with the process and nothing is recorded for runs
        that genuinely WERE killed. The 2026-08-12 census found six shutdowns
        started that day and three completed, so this is about half of them.

        The successor cannot recover them from ``handle()``: it learns
        correlation ids only from CRON_STARTED events as it sees them, and the
        cursor seed is INSERT OR IGNORE, so a restart never replays the rows
        that built the map (production 2026-08-17 04:12:03Z — every id missed
        an empty map and nothing was emitted).

        Every input is durable in the bus, so this is a query. Evaluated after
        the fact it is strictly BETTER than the in-process path, not a
        fallback: the answer is already final, so it needs no deferral and has
        no race. The in-process staging is kept as the fast path because it
        covers the one case this cannot — a graceful stop that no successor
        ever follows.

        Never raises: ``startup_all()`` runs inline in gateway boot.
        """
        try:
            self._reconstruct_shutdown_attributions()
        except Exception:
            logger.exception(
                "CronStaleMonitor: shutdown-attribution reconstruction failed",
            )

    def _already_attributed(self, since: str) -> Set[Tuple[str, str]]:
        """``(gateway_stopped_event_id, cron_started_event_id)`` pairs on the bus.

        The dedupe key is entirely inside the CRON_STALE payload already, so
        "did somebody report this?" is a bus query rather than new state — which
        is also what makes this pass idempotent without a watermark. On a
        graceful shutdown the predecessor's flush wrote the row before it died;
        this is what stops the successor reporting it a second time.
        """
        pairs: Set[Tuple[str, str]] = set()
        for event in self.bus.query(event_type=EventType.CRON_STALE, since=since):
            payload = event.payload
            if payload.get("scope") != "gateway_stopped":
                continue
            stopped_id = payload.get("gateway_stopped_event_id")
            started_id = payload.get("cron_started_event_id")
            if stopped_id and started_id:
                pairs.add((stopped_id, started_id))
        return pairs

    @staticmethod
    def _age_at_shutdown(started_ts: str, stopped_ts: str) -> int:
        """Seconds into the run when the shutdown landed.

        NOT measured against now: reconstruction happens an arbitrary time
        later — the box may have been off for hours — so `now` would report the
        DOWNTIME instead of the run. cf. 2a4ece2c07, which fixed exactly this
        for the in-process path.
        """
        try:
            started = datetime.fromisoformat(started_ts)
            stopped = datetime.fromisoformat(stopped_ts)
        except (TypeError, ValueError):
            return 0
        return max(0, int((stopped - started).total_seconds()))

    def _reconstruct_shutdown_attributions(self) -> None:
        now = datetime.now(timezone.utc)
        horizon = (now - timedelta(seconds=self.ATTRIBUTION_HORIZON_SECONDS)).isoformat()
        report_floor = (
            now - timedelta(seconds=self.HORIZON_REPORT_WINDOW_SECONDS)
        ).isoformat()

        # One query for both the work set and the exclusion report: gateway
        # shutdowns are rare (28 rows all-time on the live bus), so a week of
        # them is cheaper than a second round trip.
        recent = self.bus.query(
            event_type=EventType.GATEWAY_STOPPED, since=report_floor,
        )
        in_horizon = [e for e in recent if e.timestamp >= horizon]
        excluded = len(recent) - len(in_horizon)
        if excluded:
            logger.info(
                "CronStaleMonitor: %d gateway shutdown(s) in the last %dd are "
                "older than the %ds attribution horizon and were not examined",
                excluded,
                self.HORIZON_REPORT_WINDOW_SECONDS // 86_400,
                self.ATTRIBUTION_HORIZON_SECONDS,
            )
        if not in_horizon:
            return

        reported = self._already_attributed(horizon)
        # Snapshot the head BEFORE reading: a cron_started landing mid-pass is
        # then simply outside every window rather than half-visible.
        head = self.bus.head_rowid()
        emitted = already = landed = unresolved = 0

        for stopped in in_horizon:
            raw = stopped.payload.get("inflight_cron_correlation_ids") or []
            if not isinstance(raw, (list, tuple)):
                logger.warning(
                    "CronStaleMonitor: %s carries a non-list "
                    "inflight_cron_correlation_ids (%s) — skipped",
                    stopped.event_id, type(raw).__name__,
                )
                continue
            exit_reason = stopped.payload.get("exit_reason")

            for correlation_id in raw:
                if (stopped.event_id, correlation_id) in reported:
                    already += 1
                    continue
                resolved = self.bus.event_with_rowid(correlation_id)
                if resolved is None:
                    # Retention evicted the row, or the id came from another
                    # bus. Nothing to attribute it to.
                    unresolved += 1
                    continue
                started_rowid, started = resolved
                job_id = started.payload.get("job_id")
                if started.event_type != EventType.CRON_STARTED or not job_id:
                    unresolved += 1
                    continue

                # The FIRST of these three after the run began decides it: a
                # terminal event means the run landed (the snapshot is taken
                # early in teardown, so a listed run can still finish), while a
                # newer CRON_STARTED means a later boot re-ran the job — that
                # run's completion belongs to IT, not to this one.
                outcome = self.bus.first_event_for_job(
                    job_id,
                    [EventType.CRON_STARTED, EventType.CRON_COMPLETED,
                     EventType.CRON_FAILED],
                    after_rowid=started_rowid,
                    through_rowid=head,
                )
                if outcome is not None and outcome.event_type in (
                    EventType.CRON_COMPLETED, EventType.CRON_FAILED,
                ):
                    landed += 1
                    continue

                job_name = (
                    started.payload.get("job_name") or started.source or job_id
                )
                age = self._age_at_shutdown(started.timestamp, stopped.timestamp)
                try:
                    self.bus.emit(
                        event_type=EventType.CRON_STALE,
                        source="cron-stale-monitor",
                        payload={
                            "job_id": job_id,
                            "job_name": job_name,
                            "scope": "gateway_stopped",
                            "exit_reason": exit_reason,
                            "age_seconds": age,
                            "gateway_stopped_event_id": stopped.event_id,
                            "cron_started_event_id": correlation_id,
                        },
                        priority=Priority.NORMAL,
                    )
                except Exception:
                    logger.exception(
                        "CronStaleMonitor: failed to emit reconstructed "
                        "gateway_stopped cron_stale for %s", job_id,
                    )
                    continue
                emitted += 1
                logger.info(
                    "CronStaleMonitor: %s (%s) was cut short by a gateway "
                    "shutdown (%s) after %ds — reconstructed from the bus, "
                    "the previous gateway never recorded it",
                    job_name, job_id, exit_reason, age,
                )

        log = logger.info if emitted else logger.debug
        log(
            "CronStaleMonitor: examined %d gateway shutdown(s) within the "
            "%ds horizon — %d reconstructed, %d already reported, %d had "
            "landed, %d unresolvable",
            len(in_horizon), self.ATTRIBUTION_HORIZON_SECONDS,
            emitted, already, landed, unresolved,
        )

    def shutdown(self) -> None:
        """Flush staged shutdown attributions — the last moment they are true.

        ``events/gateway_integration.py:shutdown()`` drains subscribers first
        and closes the bus afterwards, so by the time this runs every terminal
        event the teardown produced has been handled and the bus is still open.
        """
        self._flush_pending_shutdown()

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
            self._drop_pending_shutdown_for(job_id)
            self._started_event_ids[event.event_id] = job_id
        elif event.event_type in (EventType.CRON_COMPLETED, EventType.CRON_FAILED):
            self._open_jobs.pop(job_id, None)
            self._alerted.discard(job_id)
            # It finished on its own, so a later shutdown did not kill it.
            self._forget_started_ids_for(job_id)
            # ...and neither did an EARLIER one whose report is still staged:
            # the gateway's snapshot is taken before it drains, so this run was
            # in flight then and landed anyway.
            self._drop_pending_shutdown_for(job_id)

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
