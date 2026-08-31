"""CronScheduler provider interface (Axis B — the trigger).

⚠️ EXPERIMENTAL — this interface is validated by exactly ONE consumer (the
built-in) until an external provider (Chronos, Phase 4) shakes it out. Until
then the module path, method signatures, and start() kwargs MAY change without
a deprecation cycle. Once a second provider validates the shape it becomes
stable. Any growth MUST be additive (new optional method with a default), never
a changed signature on start() or a new abstractmethod.

A CronScheduler decides *when* a due job fires. It does NOT decide what firing
means: execution + delivery stay in cron.scheduler.run_job / _deliver_result,
shared by all providers. Providers must never reimplement agent construction or
delivery.

The built-in InProcessCronScheduler runs the historical 60s daemon-thread
ticker. Alternative providers (e.g. Chronos, a NAS-mediated managed-cron
provider for scale-to-zero deployments) live under plugins/cron_providers/<name>/ and are
selected via the `cron.provider` config key (empty = built-in).
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from typing import Any

from jobflow_dispatch.quarantine_control import (
    default_control_store,
    retain_dispatch_admission,
)


class CronScheduler(ABC):
    """Axis-B trigger provider. Decides WHEN a due cron job fires.

    Required surface is intentionally minimal: ``name`` + ``start``. ``stop``
    and ``is_available`` carry safe defaults. The three Phase-4 hooks
    (``on_jobs_changed`` / ``fire_due`` / ``reconcile``) are added later as
    NON-abstract methods so the built-in keeps satisfying the ABC without
    overriding them — see ``test_abc_growth_stays_additive``.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier, e.g. 'builtin', 'chronos'."""

    def is_available(self) -> bool:
        """Whether this provider can run in the current environment.

        MUST NOT make network calls. The built-in is always available; an
        external provider checks for configured endpoint/credentials. When a
        named provider returns False, the resolver falls back to the built-in.
        """
        return True

    @abstractmethod
    def start(
        self,
        stop_event: threading.Event,
        *,
        adapters: Any = None,
        loop: Any = None,
        interval: int = 60,
    ) -> None:
        """Begin firing due jobs.

        For the built-in this BLOCKS in the 60s loop until stop_event is set
        (it is run inside a daemon thread by the caller, exactly as today).
        An external provider may register a schedule/webhook and return
        immediately; in that case it must still honor stop_event for teardown.
        """

    def stop(self) -> None:
        """Optional eager teardown hook. Default no-op; setting the stop_event
        is the primary stop signal. Override for providers holding external
        resources (queue consumers, HTTP servers)."""
        return None

    # --- Optional hooks for external providers (added Phase 4). --------------
    # All default-safe so the built-in inherits working behavior without
    # overriding. Keep these NON-abstract — see test_abc_growth_stays_additive.

    def on_jobs_changed(self) -> None:
        """Called after a successful store mutation (create/update/remove/
        pause/resume). External providers reconcile their registry here (e.g.
        Chronos re-provisions/cancels the affected one-shot via NAS).
        Built-in: no-op (it re-reads jobs.json on every tick)."""
        return None

    def recover_interrupted(self) -> int:
        """Run profile-local attempt recovery for every provider lifecycle.

        Also carries each recovered verdict onto the job record. jobs.json's
        ``last_run_at`` is written only by ``mark_job_run()`` at the end of a
        run, so an owner that dies mid-run leaves the job reporting its
        previous clean completion — with ``next_run_at`` already advanced, that
        reads as "never ran" indefinitely for anything less frequent than the
        restart cadence. The ledger knows better; propagate it.
        """
        import logging

        from cron.executions import recover_interrupted_execution_records
        from cron.jobs import mark_job_interrupted

        logger = logging.getLogger("cron.scheduler_provider")
        records = recover_interrupted_execution_records()
        for record in records:
            # started_at is when the side effects began; a claimed-but-
            # never-started attempt only has claimed_at.
            ran_at = record.get("started_at") or record["claimed_at"]
            try:
                mark_job_interrupted(
                    record["job_id"],
                    ran_at=ran_at,
                    error=record.get("error"),
                )
            except Exception as e:
                # One unwritable job record must not abandon the rest of the
                # pass — the ledger is already correct either way.
                logger.warning(
                    "Could not stamp interrupted run onto job %s: %s",
                    record.get("job_id"), e,
                )
            # Independent of the stamp above: an unwritable jobs.json must not
            # also cost the notification layer its only record of the kill.
            self._emit_interrupted_cron_stale(record, ran_at=ran_at)
        return len(records)

    @staticmethod
    def _shutdown_attribution_exists(bus: Any, job_id: str, ran_at: str) -> bool:
        """Has the bus-query reconstruction already reported this run?

        ``CronStaleMonitor.startup()`` (main ``517cc56c97``) rebuilds shutdown
        attributions from durable bus rows and emits ``scope='gateway_stopped'``
        for every run a GATEWAY_STOPPED named. It covers the common force-kill,
        because the in-flight snapshot is taken early in ``_stop_impl_body`` —
        before the drain — so the event is usually out before the kill lands.
        This ledger path is for the deaths that leave no GATEWAY_STOPPED at all:
        a crash, an OOM, a power-off, or a kill preceding the emit.

        The window floor is THIS run's start, so an attribution belonging to an
        earlier run of the same job cannot swallow this one's report.

        **``ran_at`` must be converted to UTC first.** The ledger stamps local
        wall-clock with a local offset (``cron/executions.py`` via
        ``hermes_time.now()`` -> ``datetime.now().astimezone()``, ``-04:00``
        here) while every bus row is ``datetime.now(timezone.utc).isoformat()``
        (``+00:00``), and ``query(since=...)`` compares them as STRINGS. Passing
        the raw value widens the window by the offset — four hours on this box.
        """
        from datetime import datetime, timezone

        from events.schema import EventType

        try:
            # A naive stamp (offset lost somewhere upstream) is interpreted as
            # local by astimezone(), which is what it was.
            since = datetime.fromisoformat(ran_at).astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError):
            # Cannot bound the window, so cannot prove this is a duplicate.
            # Report it: a duplicate is noisy, a miss loses the only record
            # that this run was killed at all.
            return False

        for event in bus.query(event_type=EventType.CRON_STALE, since=since):
            payload = event.payload
            if (payload.get("scope") == "gateway_stopped"
                    and payload.get("job_id") == job_id):
                return True
        return False

    @classmethod
    def _emit_interrupted_cron_stale(cls, record: dict[str, Any], *, ran_at: str) -> None:
        """Announce one recovered run on the event bus. Never raises.

        CronStaleMonitor attributes a killed cron only when the dying gateway
        both emits GATEWAY_STOPPED and lives long enough for
        ``events/gateway_integration.py:shutdown()`` to flush the reports it
        staged. On Windows neither is guaranteed: ``hermes gateway stop`` hands
        the gateway ``_windows_stop_drain_timeout()`` — clamped to 30s at
        ``hermes_cli/gateway_windows.py:1593`` — and then force-kills the PID,
        while the flush runs in ``start_gateway()``'s teardown tail, well past
        the drain. A cron mid-LLM-call routinely needs more than 30s, so the
        kill lands first and the staged report dies with the process. The
        2026-08-12 census recorded 3 of 6 shutdowns emitting no GATEWAY_STOPPED
        at all, and 2026-08-17 lost three staged runs exactly this way.

        This is the successor's backstop and the only path that survives that
        kill, because it runs in the NEXT process off durable state. It needs no
        deduplication of its own: ``recover_interrupted_execution_records()``
        flips each row under ``WHERE id=? AND status IN ('claimed','running')``
        and returns only rows whose UPDATE changed something, so a run is
        recovered — and therefore announced — exactly once.

        ``age_seconds`` is deliberately absent. Its sibling on the
        ``gateway_stopped`` attribution means "how far into the run did the
        shutdown land", measured against the shutdown event. Nothing here can
        reconstruct that: the only clock available is now-minus-``ran_at``,
        which silently includes however long the box was down. An omitted field
        is honest; a redefined one is not. ``ran_at`` is carried instead.
        """
        import logging

        logger = logging.getLogger("cron.scheduler_provider")
        try:
            from events.gateway_integration import get_bus
            from events.schema import EventType, Priority

            bus = get_bus()
            if bus is None:
                # Recovery also runs from `hermes cron` CLI paths and from
                # Chronos, where no gateway EventBus exists. The ledger and
                # jobs.json are already correct; silence is the right answer.
                return

            job_id = record["job_id"]
            if cls._shutdown_attribution_exists(bus, job_id, ran_at):
                # Already reported by CronStaleMonitor.startup()'s bus-query
                # reconstruction (main 517cc56c97), which the gateway runs
                # BEFORE the cron ticker — runner.start() at gateway/run.py:23733
                # vs cron_thread.start() at :23818. Emitting again would
                # deterministically double-report every force-kill that still
                # managed to emit GATEWAY_STOPPED.
                return

            try:
                from cron.jobs import get_job

                job = get_job(job_id)
            except Exception:
                job = None
            job_name = (job or {}).get("name") or job_id

            bus.emit(
                event_type=EventType.CRON_STALE,
                source="cron-recovery",
                payload={
                    "job_id": job_id,
                    "job_name": job_name,
                    # Distinct from 'gateway_stopped' (a shutdown that reported
                    # its own kills) and from the generic wedge alert. All this
                    # claims is that the owner died without a terminal state —
                    # whether a stop, a crash, or an OOM did it is unknown, and
                    # so is whether the side effects ran.
                    "scope": "owner_exited",
                    "execution_id": record.get("id"),
                    "ran_at": ran_at,
                    "error": record.get("error"),
                },
                # An interrupted run explained by a restart is not a wedge
                # emergency — matches the gateway_stopped attribution, which is
                # deliberately quieter than the HIGH generic alert.
                priority=Priority.NORMAL,
            )
            logger.warning(
                "Cron '%s' (%s) was interrupted at %s and recovered by this "
                "process — reported to the event bus as scope=owner_exited",
                job_name, job_id, ran_at,
            )
        except Exception:
            # This runs on the ticker startup path, where a raised exception
            # costs the gateway its entire scheduler (the 2026-08-11 5h08m
            # outage). Recovery is already durable in the ledger before the
            # emit is attempted, so losing the announcement is the cheap half.
            logger.warning(
                "Could not announce interrupted run for job %s on the event bus",
                record.get("job_id"), exc_info=True,
            )

    def fire_due(self, job_id: str, *, adapters: Any = None, loop: Any = None) -> bool:
        """Run a single job NOW via the shared orchestrator. Called by the
        inbound fire webhook when an external scheduler signals a job is due.

        The default claims the job with a store-level compare-and-set
        (multi-machine at-most-once), then runs it via the shared
        ``run_one_job`` body. Built-in never calls this (it has its own tick
        loop); an external provider routes its inbound fire here.

        Returns True if THIS caller claimed and ran the job, False if the claim
        was lost (another machine/retry won it) or the job no longer exists.
        """
        from cron.jobs import claim_job_for_fire, get_job
        from cron.executions import create_execution
        from cron.scheduler import run_one_job

        # One retained section spans the store-level fire claim through the
        # durable running execution row. run_one_job releases this exact admission
        # at that handoff rather than holding it through the model run -- but the
        # release below stays armed regardless, so a call that fails to BIND
        # (signature skew between a partially-deployed scheduler and this call
        # site) cannot leave the section held by nobody. Both releases are the
        # same idempotent one; see RetainedDispatchAdmission.
        admission = retain_dispatch_admission(
            default_control_store(), boundary="external-provider-fire"
        )
        try:
            if not claim_job_for_fire(job_id):
                return False  # another machine already claimed this fire
            job = get_job(job_id)
            if job is None:
                return False  # job removed (e.g. repeat-N exhausted) between arm and fire
            job["execution_id"] = create_execution(job_id, source=self.name)["id"]
            return run_one_job(
                job,
                adapters=adapters,
                loop=loop,
                _dispatch_admission=admission,
            )
        finally:
            admission.release()

    def reconcile(self) -> None:
        """Converge the external registry toward jobs.json (the desired state):
        arm missing one-shots, cancel orphaned ones, re-arm changed times.
        Built-in: no-op."""
        return None


def resolve_cron_scheduler() -> "CronScheduler":
    """Return the active cron scheduler provider.

    Reads ``cron.provider`` from config. Empty/absent → built-in. A named
    provider that is missing, fails to load, or reports ``is_available() ==
    False`` falls back to the built-in with a warning — cron must never be left
    without a trigger.
    """
    import logging

    logger = logging.getLogger("cron.scheduler_provider")

    name = ""
    try:
        from hermes_cli.config import cfg_get, load_config
        name = (cfg_get(load_config(), "cron", "provider", default="") or "").strip()
    except Exception:
        pass

    if not name or name in ("builtin", "in-process", "inprocess"):
        return InProcessCronScheduler()

    try:
        from plugins.cron_providers import load_cron_scheduler
        provider = load_cron_scheduler(name)
        if provider is None:
            logger.warning("cron.provider '%s' not found; using built-in ticker", name)
            return InProcessCronScheduler()
        if not provider.is_available():
            logger.warning("cron.provider '%s' not available; using built-in ticker", name)
            return InProcessCronScheduler()
        logger.info("Using cron scheduler provider: %s", provider.name)
        return provider
    except Exception as e:
        logger.warning(
            "Failed to load cron.provider '%s' (%s); using built-in ticker", name, e
        )
        return InProcessCronScheduler()


class InProcessCronScheduler(CronScheduler):
    """Default provider: the historical in-process 60s ticker.

    ``start()`` blocks in the tick loop until ``stop_event`` is set, identical
    to the pre-refactor ``_start_cron_ticker`` core loop. The caller runs it in
    a daemon thread. ``can_dispatch`` is an optional synchronous gate supplied
    by GatewayRunner during external drain; skipped ticks leave due jobs intact
    for the next allowed tick.
    """

    @property
    def name(self) -> str:
        return "builtin"

    def start(self, stop_event, *, adapters=None, loop=None, interval=60, can_dispatch=None):
        import logging
        from cron.scheduler import tick as cron_tick
        from cron.jobs import (
            get_ticker_authority_counts,
            record_ticker_heartbeat,
            record_ticker_state,
        )

        logger = logging.getLogger("cron.scheduler_provider")
        logger.info("In-process cron scheduler started (interval=%ds)", interval)
        # Startup-phase recovery must NEVER be able to kill this thread. It runs
        # BEFORE the tick loop, so the loop's own `except BaseException` guard
        # does not cover it — on 2026-08-11 a transiently half-applied checkout
        # made this raise ImportError, the daemon thread died before its first
        # tick, and the gateway ran for 5h08m with NO scheduler while logging a
        # clean "In-process cron scheduler started" line one statement earlier.
        # Zero of 69 jobs fired. A failed recovery pass only costs interrupted
        # runs their `unknown` stamp; losing the ticker stops cron entirely, so
        # degrade to the former and always enter the loop.
        try:
            recovered = self.recover_interrupted()
        except BaseException as e:
            logger.error(
                "Cron startup recovery failed — continuing WITHOUT it so the "
                "ticker still runs (interrupted runs keep their stale status): %s",
                e, exc_info=True,
            )
            recovered = 0
        if recovered:
            logger.warning(
                "Marked %d interrupted cron execution(s) unknown after restart",
                recovered,
            )
        # Heartbeat once before the first sleep so `hermes cron status` sees a
        # live ticker immediately after startup, not only after the first tick.
        record_ticker_heartbeat()
        ticker_started_at = time.time()
        last_success_at = None
        record_ticker_state(
            "idle",
            ticker_started_at_epoch=ticker_started_at,
            last_success_at_epoch=last_success_at,
            **get_ticker_authority_counts(),
        )
        resume_gap = 0.0
        while not stop_event.is_set():
            # Publish the preceding wait's oversized gap BEFORE cron_tick enters
            # catch-up. Windows monotonic time advances through Modern Standby,
            # while some platforms' monotonic clocks do not, so use the larger
            # wall/monotonic wait and subtract the one expected interval. Timing
            # only stop_event.wait excludes a slow cron_tick from suspend time.
            counts = get_ticker_authority_counts()
            record_ticker_state(
                "dispatching",
                resume_gap_seconds=resume_gap,
                ticker_started_at_epoch=ticker_started_at,
                last_success_at_epoch=last_success_at,
                **counts,
            )
            ok = False
            try:
                if can_dispatch is not None and not can_dispatch():
                    logger.debug("Cron dispatch paused while gateway drains existing work")
                else:
                    cron_tick(
                        verbose=False,
                        adapters=adapters,
                        loop=loop,
                        sync=False,
                        can_dispatch=can_dispatch,
                    )
                ok = True
            except BaseException as e:
                # Catch BaseException (not just Exception) so a SystemExit from
                # a misbehaving provider SDK / agent retry path does not kill
                # the ticker thread silently (#32612). KeyboardInterrupt is
                # intentionally caught here too — gateway shutdown is driven by
                # stop_event (set by the main thread's signal handler), not by
                # an exception in this daemon thread, so swallowing it and
                # re-checking stop_event keeps shutdown clean.
                logger.error("Cron tick error: %s", e, exc_info=True)
            # Record liveness every iteration; bump the success marker only on a
            # clean tick, so status can tell "alive but failing every tick" from
            # "actually firing jobs" (#32612, #32895).
            record_ticker_heartbeat(success=ok)
            if ok:
                last_success_at = time.time()
            record_ticker_state(
                "completed" if ok else "failed",
                resume_gap_seconds=resume_gap,
                ticker_started_at_epoch=ticker_started_at,
                last_success_at_epoch=last_success_at,
                **get_ticker_authority_counts(),
            )
            wait_wall = time.time()
            wait_monotonic = time.monotonic()
            stop_event.wait(interval)
            wall_wait = max(0.0, time.time() - wait_wall)
            monotonic_wait = max(0.0, time.monotonic() - wait_monotonic)
            resume_gap = max(0.0, max(wall_wait, monotonic_wait) - interval)
