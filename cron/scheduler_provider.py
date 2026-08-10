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
from abc import ABC, abstractmethod
from typing import Any


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

    def register_job(self, job: dict[str, Any]) -> None:
        """Register the first external trigger for one newly persisted job.

        The built-in provider reads the local store on every tick, so its
        default is a no-op. External providers override this when creating a
        job requires a remote registration before callers can honestly report
        that the job is scheduled.
        """
        return None

    def recover_interrupted(self) -> int:
        """Run profile-local attempt recovery for every provider lifecycle."""
        from cron.executions import recover_interrupted_executions

        return recover_interrupted_executions()

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
        from cron.jobs import (
            claim_job_for_fire_attempt,
            get_job,
            release_run_claim,
        )
        from cron.executions import create_execution
        from cron.scheduler import run_one_job

        attempt_owner = claim_job_for_fire_attempt(job_id)
        if attempt_owner is None:
            return False  # another machine already claimed this fire
        handed_off = False
        try:
            job = get_job(job_id)
            if job is None:
                return False  # removed between atomic claim and refetch
            run_claim = job.get("run_claim")
            if (
                not isinstance(run_claim, dict)
                or run_claim.get("by") != attempt_owner
            ):
                return False  # a successor replaced this exact attempt
            job["execution_id"] = create_execution(job_id, source=self.name)["id"]
            # From here run_one_job owns terminal bookkeeping/release. Set the
            # flag before calling it so even BaseException cannot make this
            # pre-handoff cleanup impersonate a completed worker.
            handed_off = True
            return run_one_job(job, adapters=adapters, loop=loop)
        except Exception:
            return False
        finally:
            if not handed_off:
                try:
                    release_run_claim(job_id, expected_owner=attempt_owner)
                except Exception:
                    # Exact-owner release is best effort. A later TTL retry is
                    # safer than allowing cleanup failure to escape the fire
                    # endpoint and trigger an unbounded upstream retry storm.
                    pass

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

    def start(
        self,
        stop_event,
        *,
        adapters=None,
        loop=None,
        interval=60,
        can_dispatch=None,
        profile_homes=None,
        gateway_owner_id=None,
        recover_when_dispatchable=False,
    ):
        import logging
        from cron.scheduler import tick as cron_tick
        from cron.jobs import (
            clear_gateway_ticker_lease,
            clear_ticker_error,
            record_gateway_ticker_lease,
            record_ticker_error,
            record_ticker_heartbeat,
        )

        logger = logging.getLogger("cron.scheduler_provider")
        logger.info("In-process cron scheduler started (interval=%ds)", interval)

        # ── Multiplex profiles ────────────────────────────────────────────
        # When profile_homes is set (multiplex_profiles on), tick EACH profile's
        # cron store on every tick cycle so secondary-profile jobs actually fire
        # instead of languishing in a store no ticker owns (#69377). Without this,
        # only the process-global HERMES_HOME (the default profile) is ticked.
        # Heartbeats and recovery are also scoped per profile so `hermes cron
        # status` reflects liveness for every profile independently.
        if profile_homes:
            self._start_multiplex(
                stop_event,
                profile_homes=profile_homes,
                adapters=adapters,
                loop=loop,
                interval=interval,
                can_dispatch=can_dispatch,
                gateway_owner_id=gateway_owner_id,
            )
            return

        # ── Single-profile (legacy) path ──────────────────────────────────
        recovery_pending = True

        def _recover_once():
            nonlocal recovery_pending
            recovered = self.recover_interrupted()
            recovery_pending = False
            if recovered:
                logger.warning(
                    "Marked %d interrupted cron execution(s) unknown after restart",
                    recovered,
                )

        def _publish_gateway_lease() -> bool:
            if gateway_owner_id is None:
                return True
            try:
                record_gateway_ticker_lease(gateway_owner_id)
                return True
            except Exception as exc:
                logger.error(
                    "Could not publish gateway cron ownership lease: %s; "
                    "skipping dispatch and retrying next cycle",
                    exc,
                    exc_info=True,
                )
                # An earlier successful refresh may still exist. Remove only
                # this unique owner's path so Desktop can take over promptly.
                try:
                    clear_gateway_ticker_lease(gateway_owner_id)
                except Exception:
                    logger.warning(
                        "Could not clear failed gateway cron ownership lease",
                        exc_info=True,
                    )
                try:
                    record_ticker_error(f"GatewayTickerLeaseError: {exc}")
                except Exception:
                    logger.warning(
                        "Could not record gateway cron ownership failure",
                        exc_info=True,
                    )
                return False

        try:
            if not recover_when_dispatchable:
                _recover_once()

            # Heartbeat once before the first sleep so `hermes cron status`
            # sees a live ticker immediately after startup, not only after the
            # first tick.
            record_ticker_heartbeat()
            while not stop_event.is_set():
                # This is the readiness boundary: publish only after provider
                # initialization/recovery completed. A transient fsync/write
                # failure skips this cycle and retries instead of killing the
                # ticker thread or dispatching without an ownership signal.
                if not _publish_gateway_lease():
                    try:
                        record_ticker_heartbeat(success=False)
                    except Exception:
                        logger.warning(
                            "Could not record failed gateway cron heartbeat",
                            exc_info=True,
                        )
                    stop_event.wait(interval)
                    continue

                ok = False
                try:
                    dispatch_allowed = can_dispatch is None or can_dispatch()
                    # Desktop uses deferred recovery during a Gateway handoff.
                    # Probe again immediately before reconciliation to close
                    # the gap between its outer standby check and provider
                    # startup as tightly as possible.
                    if (
                        dispatch_allowed
                        and recovery_pending
                        and recover_when_dispatchable
                        and can_dispatch is not None
                    ):
                        dispatch_allowed = can_dispatch()

                    if not dispatch_allowed:
                        logger.debug(
                            "Cron dispatch paused while another owner is active"
                        )
                    else:
                        if recovery_pending:
                            _recover_once()
                        # Ownership can change while recovery runs. Recheck
                        # before the due-job scan so the new owner wins without
                        # waiting for another full interval.
                        if can_dispatch is not None and not can_dispatch():
                            logger.debug(
                                "Cron dispatch paused after ownership changed"
                            )
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
                    # Catch BaseException (not just Exception) so a SystemExit
                    # from a misbehaving provider SDK / agent retry path does
                    # not kill the ticker thread silently (#32612).
                    logger.error("Cron tick error: %s", e, exc_info=True)
                    record_ticker_error(f"{type(e).__name__}: {e}")
                # Record liveness every iteration; bump success only on a clean
                # tick so status distinguishes alive-but-failing from firing.
                record_ticker_heartbeat(success=ok)
                if ok:
                    clear_ticker_error()
                stop_event.wait(interval)
        finally:
            if gateway_owner_id is not None:
                try:
                    clear_gateway_ticker_lease(gateway_owner_id)
                except Exception:
                    logger.warning(
                        "Could not clear gateway cron ownership lease",
                        exc_info=True,
                    )

    def _start_multiplex(
        self,
        stop_event,
        *,
        profile_homes,
        adapters=None,
        loop=None,
        interval=60,
        can_dispatch=None,
        gateway_owner_id=None,
    ):
        """Tick every served profile's cron store when multiplex_profiles is on.

        Each profile uses ``set_hermes_home_override()`` + ``use_cron_store()``
        to scope its tick, heartbeat, recovery, lock file, config/.env, and
        agent execution to that profile's home — mirroring how
        ``_profile_runtime_scope`` scopes the multiplexed inbound path and
        ``web_server.py`` scopes per-profile cron API calls.
        """
        import logging
        from cron.scheduler import tick as cron_tick
        from cron.jobs import (
            clear_gateway_ticker_lease,
            clear_ticker_error,
            record_gateway_ticker_lease,
            record_ticker_error,
            record_ticker_heartbeat,
            use_cron_store,
        )
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override

        logger = logging.getLogger("cron.scheduler_provider")
        logger.info(
            "Multiplex cron scheduler started for %d profile(s): %s",
            len(profile_homes),
            [p[0] if isinstance(p, tuple) else p for p in profile_homes],
        )

        homes = [entry[1] if isinstance(entry, tuple) else entry for entry in profile_homes]

        def _clear_profile_leases() -> None:
            if gateway_owner_id is None:
                return
            for home in homes:
                home_token = None
                try:
                    home_token = set_hermes_home_override(str(home))
                    with use_cron_store(home):
                        clear_gateway_ticker_lease(gateway_owner_id)
                except Exception:
                    logger.warning(
                        "Could not clear gateway cron ownership lease for %s",
                        home,
                        exc_info=True,
                    )
                finally:
                    if home_token is not None:
                        try:
                            reset_hermes_home_override(home_token)
                        except Exception:
                            logger.warning(
                                "Could not reset profile scope after lease cleanup for %s",
                                home,
                                exc_info=True,
                            )

        def _record_profile_lease_failure(home, lease_error: str | None) -> None:
            """Best-effort diagnostics that can never terminate multiplex."""
            home_token = None
            try:
                home_token = set_hermes_home_override(str(home))
                with use_cron_store(home):
                    record_ticker_heartbeat(success=False)
                    if lease_error:
                        record_ticker_error(lease_error)
            except Exception:
                logger.warning(
                    "Could not record gateway lease failure for profile %s",
                    home,
                    exc_info=True,
                )
            finally:
                if home_token is not None:
                    try:
                        reset_hermes_home_override(home_token)
                    except Exception:
                        logger.warning(
                            "Could not reset profile scope after lease diagnostics for %s",
                            home,
                            exc_info=True,
                        )

        def _publish_profile_leases() -> tuple[bool, str | None]:
            if gateway_owner_id is None:
                return True, None
            try:
                for home in homes:
                    home_token = set_hermes_home_override(str(home))
                    try:
                        with use_cron_store(home):
                            record_gateway_ticker_lease(gateway_owner_id)
                    finally:
                        reset_hermes_home_override(home_token)
                return True, None
            except Exception as exc:
                logger.error(
                    "Could not publish multiplex gateway cron ownership leases: %s; "
                    "skipping dispatch and retrying next cycle",
                    exc,
                    exc_info=True,
                )
                # Do not leave a partial profile set advertised as ready.
                _clear_profile_leases()
                return False, f"GatewayTickerLeaseError: {exc}"

        try:
            # Complete recovery/initialization for every served profile before
            # advertising any of them as Gateway-owned. If a later profile's
            # recovery stalls, already-initialized profiles keep their working
            # Desktop fallback instead of observing a false ready signal.
            for home in homes:
                home_token = set_hermes_home_override(str(home))
                try:
                    with use_cron_store(home):
                        recovered = self.recover_interrupted()
                        if recovered:
                            logger.warning(
                                "Marked %d interrupted cron execution(s) for profile at %s",
                                recovered,
                                home,
                            )
                        record_ticker_heartbeat()
                finally:
                    reset_hermes_home_override(home_token)

            while not stop_event.is_set():
                leases_ready, lease_error = _publish_profile_leases()
                if not leases_ready:
                    for home in homes:
                        _record_profile_lease_failure(home, lease_error)
                    stop_event.wait(interval)
                    continue

                ok = False
                try:
                    if can_dispatch is not None and not can_dispatch():
                        logger.debug(
                            "Cron dispatch paused while gateway drains existing work"
                        )
                    else:
                        for home in homes:
                            home_token = set_hermes_home_override(str(home))
                            try:
                                with use_cron_store(home):
                                    cron_tick(
                                        verbose=False,
                                        adapters=adapters,
                                        loop=loop,
                                        sync=False,
                                        can_dispatch=can_dispatch,
                                    )
                            finally:
                                reset_hermes_home_override(home_token)
                    ok = True
                except BaseException as e:
                    logger.error("Cron tick error: %s", e, exc_info=True)
                    _tick_error = f"{type(e).__name__}: {e}"
                else:
                    _tick_error = None
                # Record per-profile heartbeat after each tick cycle. The next
                # cycle refreshes all leases before any profile dispatches.
                for home in homes:
                    home_token = set_hermes_home_override(str(home))
                    try:
                        with use_cron_store(home):
                            record_ticker_heartbeat(success=ok)
                            if ok:
                                clear_ticker_error()
                            elif _tick_error:
                                record_ticker_error(_tick_error)
                    finally:
                        reset_hermes_home_override(home_token)
                stop_event.wait(interval)
        finally:
            _clear_profile_leases()
