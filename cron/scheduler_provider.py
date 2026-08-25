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

import inspect
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

# Cap for the exponential tick backoff applied while consecutive ticks fail
# with fd exhaustion (EMFILE/ENFILE, #87644).  Base is the tick interval
# (60s by default); each consecutive EMFILE failure doubles the wait, capped
# here so a still-alive-but-exhausted gateway never sleeps longer than this
# between recovery attempts.
_EMFILE_BACKOFF_MAX_SECONDS = 15 * 60  # 15 minutes


def _backoff_wait_seconds(interval: float, consecutive_failures: int) -> float:
    """Exponential tick backoff shared by both ticker loops (#87644).

    Returns the plain ``interval`` while healthy; doubles per consecutive
    fd-exhaustion failure, capped at ``_EMFILE_BACKOFF_MAX_SECONDS``.
    """
    if consecutive_failures <= 0:
        return interval
    return min(
        interval * (2 ** (consecutive_failures - 1)),
        _EMFILE_BACKOFF_MAX_SECONDS,
    )


def _note_tick_failure(exc: BaseException, consecutive_failures: int) -> int:
    """Classify one failed tick and return the updated failure counter.

    Shared by both ticker loops (#87644): on fd exhaustion, attempt
    reclamation (gc.collect + raise the soft nofile limit) so the NEXT tick
    can succeed, and bump the counter so ``_backoff_wait_seconds`` backs off
    exponentially while the process has no chance of making progress.  Any
    other failure resets the counter — backoff is reserved for the
    self-inflicted EMFILE storm, not transient errors.
    """
    from cron.scheduler import _is_fd_exhaustion, _reclaim_fds_best_effort

    if _is_fd_exhaustion(exc):
        _reclaim_fds_best_effort()
        return consecutive_failures + 1
    return 0


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

    @property
    def supports_force_fire(self) -> bool:
        """Whether ``fire_due`` accepts the additive ``force`` keyword.

        Signature detection keeps providers written before ``force`` was added
        source-compatible. Providers accepting ``**kwargs`` are compatible.
        """
        return provider_supports_force_fire(self)

    def fire_due(
        self,
        job_id: str,
        *,
        adapters: Any = None,
        loop: Any = None,
        force: bool = False,
    ) -> bool:
        """Run a single job NOW via the shared orchestrator. Called by the
        inbound fire webhook when an external scheduler signals a job is due.

        The default claims the job with a store-level compare-and-set
        (multi-machine at-most-once), then runs it via the shared
        ``run_one_job`` body. Built-in never calls this (it has its own tick
        loop); an external provider routes its inbound fire here.

        Returns True if THIS caller claimed and processed the attempt, even if
        the job itself failed. Returns False only if the claim was lost
        (another machine/retry won it) or the job no longer exists.
        """
        claimed_job = self.claim_fire(job_id, force=force)
        if claimed_job is None:
            return False
        return self.fire_claimed(claimed_job, adapters=adapters, loop=loop)

    def claim_fire(self, job_id: str, *, force: bool = False) -> dict | None:
        """Durably claim one fire and create its audit attempt before dispatch.

        Webhook transports call this synchronously before acknowledging the
        external scheduler, then pass the exact owner-bearing snapshot to
        ``fire_claimed`` in tracked background work.
        """
        from cron.executions import create_execution, finish_execution
        from cron.jobs import claim_job_for_fire

        execution = create_execution(job_id, source=self.name)
        claim_kwargs = {"return_job": True}
        if force:
            claim_kwargs["force"] = True
        try:
            claimed_job = claim_job_for_fire(job_id, **claim_kwargs)
        except BaseException as exc:
            finish_execution(
                execution["id"],
                success=False,
                error=f"Fire claim failed before dispatch: {type(exc).__name__}: {exc}",
            )
            raise
        if not isinstance(claimed_job, dict):
            finish_execution(
                execution["id"],
                success=False,
                error="Fire claim was not acquired",
            )
            return None
        claimed_job["execution_id"] = execution["id"]
        return claimed_job

    def fire_claimed(
        self,
        claimed_job: dict,
        *,
        adapters: Any = None,
        loop: Any = None,
        cancel_event: Any = None,
    ) -> bool:
        """Run an exact snapshot returned by ``claim_fire``.

        ``cancel_event``: optional transport-owned ``threading.Event`` (or
        compatible) that lets the caller stop this execution cooperatively
        — e.g. the dashboard lifespan drain signalling pending webhook
        fires before the event loop shuts down.
        """
        from cron.scheduler import run_one_job

        run_one_job(
            claimed_job,
            adapters=adapters,
            loop=loop,
            cancel_event=cancel_event,
        )
        return True

    def reconcile(self) -> None:
        """Converge the external registry toward jobs.json (the desired state):
        arm missing one-shots, cancel orphaned ones, re-arm changed times.
        Built-in: no-op."""
        return None


_OWNED_EXTERNAL_PROVIDERS: dict[str, "OwnedExternalCronScheduler"] = {}
_OWNED_EXTERNAL_PROVIDERS_LOCK = threading.Lock()


def _owned_provider_key(profile_home: Path | str) -> str:
    return str(Path(profile_home).expanduser().resolve(strict=False))


class OwnedExternalCronScheduler(CronScheduler):
    """Ownership-fenced proxy for a single-profile external provider.

    External providers may return from ``start`` after one reconciliation. The
    proxy keeps their scheduler thread alive to renew the profile lease and
    fences every later registry/fire hook through the same token.
    """

    def __init__(
        self,
        provider: CronScheduler,
        *,
        profile_home: Path | str,
        profile: str,
        runtime_id: str,
        owner_kind: str,
        poll_interval: float = 30.0,
    ) -> None:
        from cron.scheduler_ownership import SchedulerOwnershipLease

        self._provider = provider
        self._profile_home = Path(profile_home).expanduser().resolve(strict=False)
        self._profile = profile
        self._poll_interval = max(0.01, float(poll_interval))
        self._lease = SchedulerOwnershipLease(
            profile_home=self._profile_home,
            profile=profile,
            runtime_id=runtime_id,
            owner_kind=owner_kind,
        )
        self._eager_stop = threading.Event()
        self._state_lock = threading.Lock()
        self._provider_started = False
        self._provider_thread: threading.Thread | None = None
        self._provider_stop_event: threading.Event | None = None

    @property
    def name(self) -> str:
        return self._provider.name

    def is_available(self) -> bool:
        return self._provider.is_available()

    @property
    def supports_force_fire(self) -> bool:
        return self._provider.supports_force_fire

    def _start_provider_if_needed(
        self,
        *,
        adapters: Any,
        loop: Any,
        interval: int,
        kwargs: dict[str, Any],
    ) -> None:
        with self._state_lock:
            if self._provider_started:
                return
            provider_stop = threading.Event()

            def _run_provider() -> None:
                try:
                    self._provider.start(
                        provider_stop,
                        adapters=adapters,
                        loop=loop,
                        interval=interval,
                        **kwargs,
                    )
                except BaseException as exc:
                    import logging

                    logging.getLogger("cron.scheduler_provider").error(
                        "External cron provider '%s' start failed: %s",
                        self.name,
                        exc,
                        exc_info=True,
                    )

            provider_thread = threading.Thread(
                target=_run_provider,
                daemon=True,
                name=f"cron-provider-{self.name}",
            )
            self._provider_started = True
            self._provider_stop_event = provider_stop
            self._provider_thread = provider_thread
            provider_thread.start()

    def _stop_provider_if_started(self) -> None:
        with self._state_lock:
            if not self._provider_started:
                return
            self._provider_started = False
            provider_stop = self._provider_stop_event
            provider_thread = self._provider_thread
            self._provider_stop_event = None
            self._provider_thread = None
        if provider_stop is not None:
            provider_stop.set()
        try:
            self._provider.stop()
        except Exception:
            import logging

            logging.getLogger("cron.scheduler_provider").debug(
                "External cron provider stop failed", exc_info=True
            )
        if provider_thread is not None and provider_thread is not threading.current_thread():
            provider_thread.join(timeout=2.0)

    def start(
        self,
        stop_event: threading.Event,
        *,
        adapters: Any = None,
        loop: Any = None,
        interval: int = 60,
        **kwargs: Any,
    ) -> None:
        try:
            while not stop_event.is_set() and not self._eager_stop.is_set():
                if not self._lease.claim():
                    self._stop_provider_if_started()
                    stop_event.wait(self._poll_interval)
                    continue
                with self._lease.dispatch_guard() as allowed:
                    if allowed:
                        # Submit provider activation while ownership is fenced,
                        # but never hold the mutex for the provider's lifetime.
                        self._start_provider_if_needed(
                            adapters=adapters,
                            loop=loop,
                            interval=interval,
                            kwargs=kwargs,
                        )
                stop_event.wait(self._poll_interval)
        finally:
            self._stop_provider_if_started()
            self._lease.release()
            key = _owned_provider_key(self._profile_home)
            with _OWNED_EXTERNAL_PROVIDERS_LOCK:
                if _OWNED_EXTERNAL_PROVIDERS.get(key) is self:
                    _OWNED_EXTERNAL_PROVIDERS.pop(key, None)

    def stop(self) -> None:
        self._eager_stop.set()
        self._stop_provider_if_started()

    def on_jobs_changed(self) -> None:
        with self._lease.dispatch_guard() as allowed:
            if allowed:
                self._provider.on_jobs_changed()

    def register_job(self, job: dict[str, Any]) -> None:
        with self._lease.dispatch_guard() as allowed:
            if not allowed:
                raise RuntimeError(
                    f"Cron scheduler registration refused: profile {self._profile} "
                    "is owned by another runtime"
                )
            self._provider.register_job(job)

    def recover_interrupted(self) -> int:
        with self._lease.dispatch_guard() as allowed:
            return self._provider.recover_interrupted() if allowed else 0

    def reconcile(self) -> None:
        with self._lease.dispatch_guard() as allowed:
            if allowed:
                self._provider.reconcile()

    def claim_fire(self, job_id: str, *, force: bool = False) -> dict | None:
        with self._lease.dispatch_guard() as allowed:
            if not allowed:
                return None
            return self._provider.claim_fire(job_id, force=force)

    def fire_claimed(
        self,
        claimed_job: dict,
        *,
        adapters: Any = None,
        loop: Any = None,
        cancel_event: Any = None,
    ) -> bool:
        # A durable claim already belongs to this process. Finish it even if a
        # takeover occurs between admission and worker execution.
        kwargs = {"adapters": adapters, "loop": loop}
        if provider_supports_fire_cancel(self._provider):
            kwargs["cancel_event"] = cancel_event
        return self._provider.fire_claimed(claimed_job, **kwargs)

    def fire_due(
        self,
        job_id: str,
        *,
        adapters: Any = None,
        loop: Any = None,
        force: bool = False,
    ) -> bool:
        with self._lease.dispatch_guard() as allowed:
            if not allowed:
                return False
            if self._provider.supports_force_fire:
                return self._provider.fire_due(
                    job_id,
                    adapters=adapters,
                    loop=loop,
                    force=force,
                )
            return self._provider.fire_due(job_id, adapters=adapters, loop=loop)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)


def bind_external_scheduler_ownership(
    provider: CronScheduler,
    *,
    profile_home: Path | str,
    profile: str,
    runtime_id: str,
    owner_kind: str,
    poll_interval: float = 30.0,
) -> CronScheduler:
    """Bind one external provider lifecycle to a profile ownership lease."""
    if isinstance(provider, (InProcessCronScheduler, OwnedExternalCronScheduler)):
        return provider
    owned = OwnedExternalCronScheduler(
        provider,
        profile_home=profile_home,
        profile=profile,
        runtime_id=runtime_id,
        owner_kind=owner_kind,
        poll_interval=poll_interval,
    )
    with _OWNED_EXTERNAL_PROVIDERS_LOCK:
        _OWNED_EXTERNAL_PROVIDERS[_owned_provider_key(profile_home)] = owned
    return owned


def _active_owned_external_provider() -> CronScheduler | None:
    try:
        from hermes_constants import get_hermes_home

        key = _owned_provider_key(get_hermes_home())
    except Exception:
        return None
    with _OWNED_EXTERNAL_PROVIDERS_LOCK:
        return _OWNED_EXTERNAL_PROVIDERS.get(key)


def provider_supports_force_fire(provider: Any) -> bool:
    """Return whether a provider can safely receive ``fire_due(force=...)``."""
    try:
        parameters = inspect.signature(provider.fire_due).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or (
            parameter.name == "force"
            and parameter.kind
            in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        )
        for parameter in parameters
    )


def provider_supports_split_fire(provider: Any) -> bool:
    """Return whether a provider implements the two-phase fire contract.

    Ownership wrappers preserve the underlying provider's fire contract. A
    legacy custom ``fire_due`` must stay on its single-phase path.

    The webhook admission path uses ``claim_fire`` + ``fire_claimed`` so the
    202 response is backed by a durable, owner-fenced claim. A legacy
    third-party provider that overrides the documented single-phase
    ``fire_due`` hook (custom claim/re-arm/telemetry behavior) but inherits
    the base ``claim_fire`` must keep being driven through its own
    ``fire_due`` — silently routing around its override would drop that
    behavior. Providers that customize ``claim_fire`` itself are already
    split-aware and keep the two-phase path.
    """
    if isinstance(provider, OwnedExternalCronScheduler):
        return provider_supports_split_fire(provider._provider)
    cls = type(provider)
    fire_due_impl = getattr(cls, "fire_due", None)
    claim_fire_impl = getattr(cls, "claim_fire", None)
    fire_claimed_impl = getattr(cls, "fire_claimed", None)
    if claim_fire_impl is not None and claim_fire_impl is not CronScheduler.claim_fire:
        return True
    # Overriding the second phase is also proof of split-awareness (the
    # provider composes with the inherited claim path) — e.g. Chronos keeps
    # its re-arm logic in ``fire_claimed`` only.
    if fire_claimed_impl is not None and fire_claimed_impl is not CronScheduler.fire_claimed:
        return True
    if fire_due_impl is None or fire_due_impl is CronScheduler.fire_due:
        return True
    return False


def provider_supports_fire_cancel(provider: Any) -> bool:
    """Return whether ``fire_claimed`` accepts a ``cancel_event`` kwarg."""
    try:
        parameters = inspect.signature(provider.fire_claimed).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or (
            parameter.name == "cancel_event"
            and parameter.kind
            in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        )
        for parameter in parameters
    )


DEFAULT_MISFIRE_GRACE_MINUTES = 10


def _misfire_grace_minutes() -> float:
    """Resolve the misfire catch-up grace window from config.

    ``cron.misfire_grace_minutes`` (number, default
    ``DEFAULT_MISFIRE_GRACE_MINUTES``). A non-positive value disables the
    catch-up sweep entirely.
    """
    try:
        from hermes_cli.config import cfg_get, load_config

        return float(
            cfg_get(
                load_config(),
                "cron",
                "misfire_grace_minutes",
                default=DEFAULT_MISFIRE_GRACE_MINUTES,
            )
        )
    except Exception:
        return float(DEFAULT_MISFIRE_GRACE_MINUTES)


def fire_overdue_jobs(
    provider: "CronScheduler",
    *,
    adapters: Any = None,
    loop: Any = None,
    now: Any = None,
) -> int:
    """Fire jobs whose scheduled time passed without an external fire arriving.

    The misfire catch-up half of the hosted fire path. External providers
    (Chronos) deliver scheduled fires over HTTP to this process's api_server
    adapter; when that hop is down at fire time (gateway restart window,
    api_server not bound, scheduler retry budget exhausted), the job's
    ``next_run_at`` stays parked in the past and — because external providers
    have no local tick loop — nothing ever runs it. The day is silently lost
    even though the gateway may be healthy again minutes later.

    Called from the gateway housekeeping loop. Deliberately:

    - **No-op for the built-in provider.** Its tick loop already picks up
      past-due jobs via ``get_due_jobs`` — local scheduling self-heals.
    - **Routes through the provider's own two-phase fire path** — a
      synchronous ``claim_fire`` (store CAS, so a late external retry
      landing concurrently is de-duplicated) and then ``fire_claimed`` in
      a daemon thread, mirroring the webhook admission pattern. The
      housekeeping loop that calls this must never block for the length
      of an agent run. Provider-specific re-arm logic (Chronos NAS
      one-shots) runs exactly as for a normal fire.
    - **Waits out a grace window** (``cron.misfire_grace_minutes``, default
      10, non-positive disables) so the external scheduler's own retry
      backoff gets first right to deliver — catch-up is the backstop, not
      a race.
    - **Operates on the process-global cron store only** — same profile
      scoping as the external provider's reconcile.

    Returns the number of jobs this sweep claimed and dispatched.
    """
    import logging
    import threading
    from datetime import datetime

    logger = logging.getLogger("cron.scheduler_provider")

    if isinstance(provider, InProcessCronScheduler):
        return 0

    grace_minutes = _misfire_grace_minutes()
    if grace_minutes <= 0:
        return 0

    from cron.jobs import _ensure_aware, _hermes_now, is_job_runnable, load_jobs

    if now is None:
        now = _hermes_now()

    fired = 0
    for job in load_jobs():
        if not is_job_runnable(job):
            continue
        next_run_at = job.get("next_run_at")
        if not next_run_at:
            continue
        try:
            due_dt = _ensure_aware(datetime.fromisoformat(next_run_at))
        except (ValueError, TypeError):
            continue
        overdue_seconds = (now - due_dt).total_seconds()
        if overdue_seconds < grace_minutes * 60:
            continue
        job_id = str(job.get("id") or "")
        # One-shot jobs share the module-wide policy: more than
        # ONESHOT_GRACE_SECONDS past their run time means "will never fire"
        # (create/update/resume/recovery and, since #89571, the due-scan all
        # enforce it). The misfire backstop must not resurrect them hours
        # late after downtime — that's #93526.
        schedule = job.get("schedule") or {}
        if str(schedule.get("kind") or "") == "once":
            from cron.jobs import ONESHOT_GRACE_SECONDS

            if overdue_seconds > ONESHOT_GRACE_SECONDS:
                logger.warning(
                    "Misfire catch-up: one-shot job %s (%s) was due %s "
                    "(%.0f min overdue) — outside the %ss one-shot grace "
                    "window, not firing.",
                    job_id,
                    job.get("name") or "unnamed",
                    next_run_at,
                    overdue_seconds / 60,
                    ONESHOT_GRACE_SECONDS,
                )
                continue
        logger.warning(
            "Misfire catch-up: job %s (%s) was due %s (%.0f min overdue) and "
            "no external fire arrived — firing locally.",
            job_id,
            job.get("name") or "unnamed",
            next_run_at,
            overdue_seconds / 60,
        )
        try:
            # Two-phase, webhook-style: claim synchronously (fast store
            # CAS — losing means an external retry beat us, which is
            # fine), then run the job off-thread so the caller's loop is
            # never blocked for the length of an agent run.
            claimed = provider.claim_fire(job_id)
            if claimed is None:
                continue
            threading.Thread(
                target=provider.fire_claimed,
                args=(claimed,),
                kwargs={"adapters": adapters, "loop": loop},
                daemon=True,
                name=f"cron-misfire-{job_id[:12]}",
            ).start()
            fired += 1
        except Exception as exc:
            logger.warning(
                "Misfire catch-up failed for job %s: %s: %s",
                job_id, type(exc).__name__, exc,
            )
    return fired


def resolve_cron_scheduler() -> "CronScheduler":
    """Return the active cron scheduler provider.

    Reads ``cron.provider`` from config. Empty/absent → built-in. A named
    provider that is missing, fails to load, or reports ``is_available() ==
    False`` falls back to the built-in with a warning — cron must never be left
    without a trigger.
    """
    import logging

    logger = logging.getLogger("cron.scheduler_provider")

    active_owned = _active_owned_external_provider()
    if active_owned is not None:
        return active_owned

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


def scheduler_for_profile_mode(
    provider: "CronScheduler", *, multiplex_profiles: bool
) -> "CronScheduler":
    """Return a scheduler that can safely serve the gateway's profile mode.

    External providers currently own one unscoped remote registry/client and
    therefore cannot safely reconcile several profile stores from one process.
    Fail closed to the built-in multiplex ticker until the provider API carries
    explicit profile identity through lifecycle and webhook calls.
    """
    if not multiplex_profiles or isinstance(provider, InProcessCronScheduler):
        return provider

    import logging

    logging.getLogger("cron.scheduler_provider").warning(
        "cron.provider '%s' does not support multiplex_profiles; using built-in ticker",
        provider.name,
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
        profile_adapters=None,
        owner_kind=None,
        runtime_id=None,
    ):
        import logging
        from cron.scheduler import tick as cron_tick
        from cron.jobs import (
            clear_ticker_error,
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
                profile_adapters=profile_adapters,
                loop=loop,
                interval=interval,
                can_dispatch=can_dispatch,
                owner_kind=owner_kind,
                runtime_id=runtime_id,
            )
            return

        # ── Single-profile (legacy) path ──────────────────────────────────
        recovered = self.recover_interrupted()
        if recovered:
            logger.warning(
                "Marked %d interrupted cron execution(s) unknown after restart",
                recovered,
            )
        # Heartbeat once before the first sleep so `hermes cron status` sees a
        # live ticker immediately after startup, not only after the first tick.
        record_ticker_heartbeat()
        # Exponential backoff for consecutive tick failures — most importantly
        # fd exhaustion (EMFILE/ENFILE, #87644).  While FDs stay exhausted the
        # ticker must NOT hammer the store every 60s; once they free (leak
        # fixed, reclamation ran) the next tick succeeds and the backoff
        # resets, so the scheduler self-heals without a gateway restart.
        consecutive_failures = 0
        while not stop_event.is_set():
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
                # Persist the failure reason next to the heartbeat markers so
                # `hermes cron status`/`list` (separate processes) can show
                # WHY ticks fail, not just that the success marker is stale —
                # e.g. a root-rewritten jobs.json locking out the ticker's
                # uid went unnoticed for ~14h with the reason buried in the
                # gateway log (#68483).
                record_ticker_error(f"{type(e).__name__}: {e}")
                # EMFILE: reclaim fds + back off exponentially so the
                # exhausted process stops hammering the store while it has no
                # chance of making progress (#87644).
                consecutive_failures = _note_tick_failure(e, consecutive_failures)
            # Record liveness every iteration; bump the success marker only on a
            # clean tick, so status can tell "alive but failing every tick" from
            # "actually firing jobs" (#32612, #32895).
            record_ticker_heartbeat(success=ok)
            if ok:
                clear_ticker_error()
                consecutive_failures = 0
            stop_event.wait(_backoff_wait_seconds(interval, consecutive_failures))

    def _start_multiplex(
        self,
        stop_event,
        *,
        profile_homes,
        adapters=None,
        profile_adapters=None,
        loop=None,
        interval=60,
        can_dispatch=None,
        owner_kind=None,
        runtime_id=None,
    ):
        """Tick each profile only while this runtime owns its scheduler lease.

        The profile's home, cron store, secret scope, and adapter map are bound
        together before recovery, heartbeat, and dispatch. Ownership is checked
        atomically under the lease mutex through ``cron_tick`` submission, so a
        higher-priority dedicated gateway can take over from Desktop without a
        check-then-dispatch race.
        """
        import contextlib
        import logging

        from agent.secret_scope import (
            build_profile_secret_scope,
            reset_authoritative_secret_scope,
            reset_secret_scope,
            set_authoritative_secret_scope,
            set_secret_scope,
        )
        from cron.scheduler import tick as cron_tick
        from cron.jobs import (
            clear_ticker_error,
            record_ticker_error,
            record_ticker_heartbeat,
            use_cron_store,
        )
        from cron.scheduler_ownership import SchedulerOwnershipLease
        from hermes_cli.env_loader import hydrate_profile_secret_sources
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override

        logger = logging.getLogger("cron.scheduler_provider")
        entries = [
            (entry[0], entry[1]) if isinstance(entry, tuple) else (entry.name, entry)
            for entry in profile_homes
        ]
        logger.info(
            "Profile-aware cron scheduler started for %d candidate profile(s): %s",
            len(entries),
            [name for name, _home in entries],
        )

        leases = {}
        ownership_identity_requested = bool(owner_kind or runtime_id)
        ownership_identity_valid = not ownership_identity_requested
        if ownership_identity_requested:
            if not owner_kind or not runtime_id:
                logger.error(
                    "Cron scheduler ownership identity is incomplete; refusing all profile dispatch"
                )
            else:
                ownership_identity_valid = True
                leases = {
                    name: SchedulerOwnershipLease(
                        profile_home=home,
                        profile=name,
                        runtime_id=runtime_id,
                        owner_kind=owner_kind,
                    )
                    for name, home in entries
                }

        authoritative_secret_scope = not (
            owner_kind == "gateway-dedicated" and len(entries) == 1
        )

        @contextlib.contextmanager
        def _runtime_scope(home):
            home_token = set_hermes_home_override(str(home))
            hydrate_profile_secret_sources(home)
            secrets = build_profile_secret_scope(home)
            if authoritative_secret_scope:
                secret_tokens = set_authoritative_secret_scope(secrets)
            else:
                secret_token = set_secret_scope(secrets)
            try:
                with use_cron_store(home):
                    yield
            finally:
                if authoritative_secret_scope:
                    reset_authoritative_secret_scope(secret_tokens)
                else:
                    reset_secret_scope(secret_token)
                reset_hermes_home_override(home_token)

        def _profile_adapters(name):
            if profile_adapters is not None:
                return profile_adapters.get(name)
            if len(entries) == 1:
                return adapters
            # A shared adapter map in a multi-profile process can carry another
            # profile's delivery identity. Fail closed unless the caller supplies
            # an explicit per-profile map.
            return None

        def _claim(name):
            if not ownership_identity_valid:
                return False
            lease = leases.get(name)
            if lease is None:
                # Compatibility for direct/test callers that do not yet pass an
                # ownership identity. Production Desktop/gateway entry points do.
                return True
            return lease.claim()

        # Recovery + initial heartbeat only for profiles this runtime owns.
        active = []
        for name, home in entries:
            if not _claim(name):
                continue
            lease = leases.get(name)
            guard = lease.dispatch_guard() if lease is not None else contextlib.nullcontext(True)
            with guard as allowed:
                if not allowed:
                    continue
                with _runtime_scope(home):
                    recovered = self.recover_interrupted()
                    if recovered:
                        logger.warning(
                            "Marked %d interrupted cron execution(s) for profile %s unknown",
                            recovered,
                            name,
                        )
                    record_ticker_heartbeat()
                active.append(name)
        logger.info("Cron scheduler currently owns %d profile(s): %s", len(active), active)

        consecutive_failures = 0
        try:
            while not stop_event.is_set():
                cycle_failed = False
                owned_in_cycle = False
                for name, home in entries:
                    if stop_event.is_set():
                        break
                    if can_dispatch is not None and not can_dispatch():
                        logger.debug("Cron dispatch paused while gateway drains existing work")
                        break
                    if not _claim(name):
                        continue
                    # The ownership claim can wait behind another runtime's
                    # dispatch guard. Re-check shutdown after that wait so a
                    # stopped successor cannot dispatch one late fire after the
                    # prior owner releases during restart.
                    if stop_event.is_set():
                        break
                    lease = leases.get(name)
                    guard = (
                        lease.dispatch_guard()
                        if lease is not None
                        else contextlib.nullcontext(True)
                    )
                    with guard as allowed:
                        if not allowed:
                            continue
                        owned_in_cycle = True
                        ok = False
                        tick_error = None
                        with _runtime_scope(home):
                            try:
                                cron_tick(
                                    verbose=False,
                                    adapters=_profile_adapters(name),
                                    loop=loop,
                                    sync=False,
                                    can_dispatch=can_dispatch,
                                )
                                ok = True
                            except BaseException as exc:
                                cycle_failed = True
                                tick_error = f"{type(exc).__name__}: {exc}"
                                logger.error(
                                    "Cron tick error for profile %s: %s",
                                    name,
                                    exc,
                                    exc_info=True,
                                )
                                consecutive_failures = _note_tick_failure(
                                    exc, consecutive_failures
                                )
                            record_ticker_heartbeat(success=ok)
                            if ok:
                                clear_ticker_error()
                            elif tick_error:
                                record_ticker_error(tick_error)
                if not cycle_failed:
                    consecutive_failures = 0
                wait_seconds = _backoff_wait_seconds(interval, consecutive_failures)
                if leases and not owned_in_cycle:
                    # A dedicated gateway may be waiting behind Desktop's short
                    # dispatch guard. Retry standby ownership promptly instead
                    # of sleeping a full scheduler interval after lock timeout.
                    wait_seconds = min(wait_seconds, 1.0)
                stop_event.wait(wait_seconds)
        finally:
            for lease in leases.values():
                lease.release()
