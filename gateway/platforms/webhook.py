"""Generic webhook platform adapter.

Runs an aiohttp HTTP server that receives webhook POSTs from external
services (GitHub, GitLab, JIRA, Stripe, etc.), validates HMAC signatures,
transforms payloads into agent prompts, and routes responses back to the
source or to another configured platform.

Configuration lives in config.yaml under platforms.webhook.extra.routes.
Each route defines:
  - events: which event types to accept (header-based filtering)
  - secret: HMAC secret for signature validation (REQUIRED)
  - prompt: template string formatted with the webhook payload
  - skills: optional list of skills to load for the agent
  - deliver: where to send the response (github_comment, telegram, etc.)
  - deliver_extra: additional delivery config (repo, pr_number, chat_id)
  - deliver_only: if true, skip the agent — the rendered prompt IS the
    message that gets delivered.  Use for external push notifications
    (Supabase, monitoring alerts, inter-agent pings) where zero LLM cost
    and sub-second delivery matter more than agent reasoning.

Security:
  - HMAC secret is required per route (validated at startup)
  - Rate limiting per route (fixed-window, configurable)
  - A durable replay ledger prevents duplicate agent runs across retries/restarts
  - Body size limits checked before reading payload
  - Generic HMAC supports a V2 signature (X-Webhook-Signature-V2) that
    binds a timestamp into the signed data for cryptographic freshness.
    Legacy body-only V1 (X-Webhook-Signature) has no signed nonce/time;
    the durable ledger permanently fences an identical body, which also
    means legitimate identical V1 payloads collapse. Migrate to V2.
  - Set secret to "INSECURE_NO_AUTH" to skip validation (testing only)
"""

import asyncio
import json
import logging
import os
import re
import sys
import threading
from collections import deque
from types import MappingProxyType
from typing import Any, Callable, Deque, Dict, Mapping, Optional

try:
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
)
from gateway.platforms.webhook_auth import WebhookAuthMixin, _hmac_str_equal
from gateway.platforms.webhook_contract import (
    WebhookContractError,
    WebhookRouteConfig,
)
from gateway.platforms.webhook_filters import (
    DEFAULT_SCRIPT_TIMEOUT_SECONDS,
    WebhookPreparedScript,
    WebhookRouteProcessor,
)
from gateway.platforms.webhook_ledger import (
    MINIMUM_MAX_STORAGE_BYTES,
    OperationAuthority,
    RecoveryCursor,
    WebhookLedgerConfigurationError,
    WebhookLedgerError,
    WebhookLedgerCapacityError,
    WebhookLedgerTransitionError,
    WebhookOperationLedger,
)

logger = logging.getLogger(__name__)

__all__ = [
    "WebhookAdapter",
    "WebhookConfigurationError",
    "WebhookMessageEvent",
    "_hmac_str_equal",
    "check_webhook_requirements",
]


def check_webhook_requirements() -> bool:
    """Check if webhook adapter dependencies are available."""
    return AIOHTTP_AVAILABLE


from gateway.platforms.webhook_common import (
    AuthenticatedRouteAuthority,
    DEFAULT_HOST,
    DEFAULT_PORT,
    WebhookConfigurationError,
    WebhookMessageEvent,
    WebhookTargetDeliveryDisposition,
    WebhookTargetDeliveryResult,  # noqa: F401 - compatibility re-export
    _DYNAMIC_ROUTES_FILENAME,  # noqa: F401 - compatibility re-export
    _IDEMPOTENCY_DEFAULT_MAX_ENTRIES,
    _IDEMPOTENCY_DEFAULT_MAX_STORAGE_BYTES,
    _IDEMPOTENCY_MAX_ENTRIES_LIMIT,
    _IDEMPOTENCY_MAX_STORAGE_BYTES_LIMIT,
    _INSECURE_NO_AUTH,
    _MAX_BODY_BYTES_LIMIT,
    _MAX_CONCURRENT_AUTHORITY_PROOFS,
    _MAX_RATE_LIMIT_PER_MINUTE,
    _MAX_SCRIPT_TIMEOUT_SECONDS,
    _PROFILE_AUTHORITY_INCARNATION_FILENAME,  # noqa: F401 - compatibility re-export
    _PROFILE_REJECTED,  # noqa: F401 - compatibility re-export
    _PROMPT_TOKEN_RE,
    _RATE_WINDOW_SECONDS,
    _RAW_PAYLOAD_DEFAULT_CAP_BYTES,
    _RAW_PAYLOAD_MAX_CAP_BYTES,
    _RAW_PAYLOAD_MIN_CAP_BYTES,
    _bounded_positive_int,
    _clear_quarantined_retirement_owner,
    _is_loopback_host,
    _profile_incarnation_token,  # noqa: F401 - compatibility re-export
    _quarantine_failed_retirement,
    _quarantined_retirement_owners,
    _retirement_quarantine_key,
    _route_worker_slots,
    _strict_bounded_int,
)

from gateway.platforms.webhook_delivery import WebhookDeliveryMixin
from gateway.platforms.webhook_intake import WebhookIntakeMixin
from gateway.platforms.webhook_recovery import WebhookRecoveryMixin
from gateway.platforms.webhook_route_authority import WebhookRouteAuthorityMixin


class WebhookAdapter(
    WebhookRecoveryMixin,
    WebhookIntakeMixin,
    WebhookDeliveryMixin,
    WebhookRouteAuthorityMixin,
    WebhookAuthMixin,
    BasePlatformAdapter,
):
    """Generic webhook receiver that triggers agent runs from HTTP POSTs."""

    owns_final_delivery_ledger: bool = True
    allows_automatic_session_resume: bool = False
    supports_async_delivery: bool = False

    # No human is present to answer a "session restored — what next?" prompt.
    # Generic session replay is disabled above; the durable operation ledger
    # decides whether an interrupted carrier is replayable or indeterminate.
    interactive_resume: bool = False

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.WEBHOOK)
        # ``host`` may be None (dual-stack default) or a user-pinned string.
        # A config value of empty string / null is normalised to None so it
        # also means "bind all families" rather than an invalid "" host.
        _cfg_host = config.extra.get("host", DEFAULT_HOST)
        if _cfg_host is not None and not isinstance(_cfg_host, str):
            raise WebhookConfigurationError("host must be a string or null")
        self._host: Optional[str] = _cfg_host or None
        self._port = _strict_bounded_int(
            config.extra.get("port", DEFAULT_PORT),
            label="port",
            minimum=0,
            maximum=65_535,
        )
        self._global_secret: str = config.extra.get("secret", "")
        configured_routes = config.extra.get("routes", {})
        if not isinstance(configured_routes, Mapping):
            raise WebhookConfigurationError("routes must be an object")
        self._static_routes: Dict[str, dict] = dict(configured_routes)
        self._dynamic_routes: Dict[str, dict] = {}
        self._dynamic_routes_content_sha256: Optional[str] = None
        self._dynamic_routes_file_identity: Optional[tuple[int, ...]] = None
        self._dynamic_routes_rejected_content_sha256: Optional[str] = None
        self._dynamic_routes_rejected_file_identity: Optional[tuple[int, ...]] = None
        self._dynamic_routes_transient_file_identity: Optional[tuple[int, ...]] = None
        self._dynamic_routes_retry_after = 0.0
        self._dynamic_routes_integrity_recheck_identity: Optional[tuple[int, ...]] = (
            None
        )
        self._dynamic_routes_integrity_recheck_after = 0.0
        self._dynamic_routes_file_present = False
        self._routes: Dict[str, dict] = dict(self._static_routes)
        self._authenticated_route_snapshot: Optional[tuple[Any, ...]] = None
        self._authenticated_route_authorities: Mapping[str, tuple[Any, ...]] = (
            MappingProxyType({})
        )
        self._authenticated_route_effective_toolsets: Mapping[str, tuple[str, ...]] = (
            MappingProxyType({})
        )
        self._authenticated_route_scripts: Mapping[
            str, Optional[WebhookPreparedScript]
        ] = MappingProxyType({})
        self._authenticated_route_profile_generations: Mapping[str, str] = (
            MappingProxyType({})
        )
        # This is the request-facing publication point. A request captures one
        # bundle object and never rejoins execution authority through mutable,
        # name-indexed caches after an await.
        self._authenticated_route_bundles: Mapping[str, AuthenticatedRouteAuthority] = (
            MappingProxyType({})
        )
        self._runner = None
        self._accepting_webhooks = True
        self._global_ledger_saturated = False
        self._health_capacity_lock = asyncio.Lock()
        self._authentication_authority_proof_slots = threading.BoundedSemaphore(
            _MAX_CONCURRENT_AUTHORITY_PROOFS
        )
        # Reference to gateway runner for cross-platform delivery (set externally)
        self.gateway_runner = None

        # Durable replay admission and exact delivery settlement. There is no
        # process-local authority cache or fallback delivery configuration.
        self._idempotency_max_entries: int = _bounded_positive_int(
            config.extra.get(
                "idempotency_max_entries", _IDEMPOTENCY_DEFAULT_MAX_ENTRIES
            ),
            default=_IDEMPOTENCY_DEFAULT_MAX_ENTRIES,
            maximum=_IDEMPOTENCY_MAX_ENTRIES_LIMIT,
        )
        self._idempotency_max_storage_bytes: int = _strict_bounded_int(
            config.extra.get(
                "idempotency_max_storage_bytes",
                _IDEMPOTENCY_DEFAULT_MAX_STORAGE_BYTES,
            ),
            label="idempotency_max_storage_bytes",
            minimum=MINIMUM_MAX_STORAGE_BYTES,
            maximum=_IDEMPOTENCY_MAX_STORAGE_BYTES_LIMIT,
            unit="bytes",
        )

        # Rate limiting is qualified by the selected profile and route.
        self._rate_counts: Dict[tuple[str, str], Deque[float]] = {}
        self._rate_limit = _strict_bounded_int(
            config.extra.get("rate_limit", 30),
            label="rate_limit",
            minimum=1,
            maximum=_MAX_RATE_LIMIT_PER_MINUTE,
            unit="requests per minute",
        )

        # Body size limit (auth-before-body pattern)
        self._max_body_bytes = _strict_bounded_int(
            config.extra.get("max_body_bytes", _MAX_BODY_BYTES_LIMIT),
            label="max_body_bytes",
            minimum=1,
            maximum=_MAX_BODY_BYTES_LIMIT,
            unit="bytes",
        )
        self._script_timeout_seconds = _strict_bounded_int(
            config.extra.get(
                "script_timeout_seconds",
                DEFAULT_SCRIPT_TIMEOUT_SECONDS,
            ),
            label="script_timeout_seconds",
            minimum=1,
            maximum=_MAX_SCRIPT_TIMEOUT_SECONDS,
            unit="seconds",
        )
        self._route_processor = WebhookRouteProcessor(
            script_timeout_seconds=self._script_timeout_seconds
        )
        # Replay proofs belong to the stable Hermes installation, never to the
        # profile or multiplex mode that happened to launch this process.  The
        # operation key already carries profile+route scope, and the ledger
        # enforces per-scope quotas.  Keeping one root DB prevents an exact
        # signed delivery from re-executing when operators switch named
        # profiles or toggle multiplexing.
        from hermes_constants import get_default_hermes_root

        operation_authority_path = get_default_hermes_root() / "state.db"
        self._operation_ledger = WebhookOperationLedger(
            operation_authority_path,
            max_records=self._idempotency_max_entries,
            max_storage_bytes=self._idempotency_max_storage_bytes,
        )
        # Credential ownership spans the same stable installation scope.
        # Sharing the object also shares its in-process lock for all writes to
        # the one physical schema.
        self._authentication_authority_ledger = self._operation_ledger
        self._recovery_tasks_by_operation: dict[str, asyncio.Task] = {}
        self._recovery_pump_lock = asyncio.Lock()
        self._recovery_cycle_active = False
        self._recovery_backlog_pending = False
        self._recovery_last_progress = False
        self._recovery_last_error = False
        self._recovery_restart_dead_scan = False
        self._dead_owner_recovery_cursor: Optional[RecoveryCursor] = None
        self._dead_owner_recovery_complete = False
        self._current_recovery_cursor: Optional[RecoveryCursor] = None
        self._current_recovery_complete = False
        self._recovery_current_scan_required = False
        self._dead_claim_handoff_in_progress = False
        self._recovery_authority_profiles: tuple[str, ...] = ()
        self._lifecycle_retiring = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _select_stable_operation_ledger(self) -> None:
        """Pin replay authority to the stable Hermes root before connect.

        This is normally a no-op because ``__init__`` already selects the root.
        It also protects embedders/tests that change the active profile between
        construction and connection: the final listener can never publish with
        a profile-local replay ledger.
        """

        from hermes_constants import get_default_hermes_root

        root_db_path = get_default_hermes_root() / "state.db"
        current_key = _retirement_quarantine_key(self._operation_ledger)
        root_key = os.path.normcase(str(root_db_path.resolve(strict=False)))
        if current_key == root_key:
            # Operation and authentication authority share both the physical
            # database and the in-process lock once the listener is root-owned.
            self._authentication_authority_ledger = self._operation_ledger
            return
        if self._runner is not None or any(
            not task.done() for task in self._recovery_tasks_by_operation.values()
        ):
            raise WebhookLedgerTransitionError(
                "multiplex webhook ledger cannot move after listener work started"
            )

        previous = self._operation_ledger
        replacement = WebhookOperationLedger(
            root_db_path,
            max_records=self._idempotency_max_entries,
            max_storage_bytes=self._idempotency_max_storage_bytes,
            terminal_retention_seconds=previous.terminal_retention_seconds,
            local_bypass_replay_retention_seconds=(
                previous.local_bypass_replay_retention_seconds
            ),
            instance_id=previous.instance_id,
        )
        self._operation_ledger = replacement
        self._authentication_authority_ledger = replacement
        logger.info(
            "[webhook] Listener replay authority pinned to %s",
            root_db_path,
        )

    def _fence_intake_for_durable_transition_failure(self, context: str) -> None:
        """Quarantine this exact owner and queue bounded asynchronous recovery.

        Returning a 503 is not enough when a failed transition may have left a
        PREPARING row owned by this still-live process: ordinary dead-owner
        recovery correctly refuses to steal it, so exact retries would remain
        ACTIVE forever.  Close intake, fence the whole exact adapter instance,
        and let the runner's bounded recovery loop safely reclaim replayable
        carriers.  A failed/ambiguous retirement is quarantined for the same
        exact-owner retry used by disconnect replacement.
        """

        self._accepting_webhooks = False
        self._recovery_backlog_pending = True
        self._recovery_restart_dead_scan = True
        marker_recorded = False
        try:
            marker_recorded = _quarantine_failed_retirement(self._operation_ledger)
        except Exception:
            logger.exception("[webhook] Could not quarantine owner after %s", context)
        if not marker_recorded:
            logger.critical(
                "[webhook] Durable transition quarantine is saturated; "
                "process restart is required"
            )
        logger.error(
            "[webhook] Intake fenced after %s; exact-owner recovery queued",
            context,
        )

        runner = self.gateway_runner
        update_status = getattr(runner, "_update_platform_runtime_status", None)
        if callable(update_status):
            update_status(
                Platform.WEBHOOK.value,
                platform_state="retrying",
                error_code="webhook_transition_failed",
                error_message="Durable webhook transition requires recovery",
            )
        schedule_retry = getattr(runner, "_schedule_webhook_recovery_retry", None)
        if callable(schedule_retry):
            schedule_retry(self)

    def _mark_indeterminate_or_fence(
        self,
        authority: OperationAuthority,
        reason: object,
        *,
        context: str,
    ) -> bool:
        """Persist one unknown outcome or fence the owner if that write fails."""

        try:
            preserved = self._operation_ledger.mark_indeterminate(
                authority,
                reason,
            )
        except BaseException as exc:
            logger.exception(
                "[webhook] Indeterminate reconciliation failed after %s", context
            )
            self._fence_intake_for_durable_transition_failure(context)
            if not isinstance(exc, Exception):
                raise
            return False
        if not preserved:
            logger.error("[webhook] Indeterminate authority was lost after %s", context)
            self._fence_intake_for_durable_transition_failure(context)
            return False
        return True

    def _reject_connect_configuration(self, detail: object) -> bool:
        """Publish one deterministic startup failure as non-retryable."""

        message = str(detail) or "Webhook configuration is invalid"
        self._accepting_webhooks = False
        self._set_fatal_error(
            "webhook_configuration_invalid",
            message,
            retryable=False,
        )
        logger.error("[webhook] Refusing invalid static configuration: %s", message)
        return False

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self._accepting_webhooks = False
        self._lifecycle_retiring = False
        try:
            self._select_stable_operation_ledger()
        except WebhookLedgerConfigurationError as exc:
            return self._reject_connect_configuration(exc)
        except WebhookLedgerError as exc:
            self._set_fatal_error(
                "webhook_storage_unavailable",
                "Durable webhook storage is unavailable",
                retryable=True,
            )
            logger.error("[webhook] Durable ledger selection failed: %s", exc)
            return False
        # Load agent-created subscriptions before validating
        self._reload_dynamic_routes()

        # Validate routes at startup — secret is required per route.
        for name, route in self._routes.items():
            request_profile = (
                route.get("profile", "default")
                if isinstance(route, dict)
                else "default"
            )
            try:
                bound_route = WebhookRouteConfig.bind(
                    name,
                    route,
                    headers={},
                    request_profile=request_profile,
                )
            except WebhookContractError as exc:
                return self._reject_connect_configuration(
                    f"Route '{name}' has invalid authority binding: {exc}"
                )
            secret = route.get("secret", self._global_secret)
            if not isinstance(secret, str) or not secret:
                return self._reject_connect_configuration(
                    f"Route '{name}' has no HMAC secret. "
                    f"Set 'secret' on the route or globally. "
                    f"For testing without auth, set secret to '{_INSECURE_NO_AUTH}'."
                )
            # Safety rail: refuse to start if INSECURE_NO_AUTH is combined with a
            # non-loopback bind. The escape hatch is for local testing only;
            # serving an unauthenticated route on a public interface is a
            # deployment-grade footgun we'd rather crash early than ship.
            if secret == _INSECURE_NO_AUTH and not _is_loopback_host(self._host):
                return self._reject_connect_configuration(
                    f"Route '{name}' uses INSECURE_NO_AUTH secret "
                    f"but is bound to non-loopback host '{self._host}'. "
                    f"INSECURE_NO_AUTH is for local testing only. "
                    f"Refusing to start to prevent accidental exposure."
                )
            # deliver_only routes bypass the agent — the POST body becomes a
            # direct push notification via the configured delivery target.
            # Validate up-front so misconfiguration surfaces at startup rather
            # than on the first webhook POST.
            if route.get("deliver_only"):
                deliver = route.get("deliver", "log")
                if not deliver or deliver == "log":
                    return self._reject_connect_configuration(
                        f"Route '{name}' has deliver_only=true but "
                        f"deliver is '{deliver}'. Direct delivery requires a "
                        f"real target (telegram, discord, slack, github_comment, etc.)."
                    )

        # Only after every startup rule passes, bind each credential to one
        # durable route/policy authority. Invalid routes must not consume a
        # permanent key binding when no listener could ever have opened.
        try:
            self._bind_route_authentication_authorities(self._routes)
        except (
            WebhookContractError,
            WebhookLedgerCapacityError,
            WebhookLedgerTransitionError,
        ) as exc:
            return self._reject_connect_configuration(
                f"Route authentication authority is invalid: {exc}"
            )
        except WebhookLedgerError as exc:
            self._set_fatal_error(
                "webhook_storage_unavailable",
                "Durable webhook storage is unavailable",
                retryable=True,
            )
            logger.error("[webhook] Authentication authority storage failed: %s", exc)
            return False

        # client_max_size makes aiohttp enforce the cap on every read path,
        # including Transfer-Encoding: chunked bodies that carry no
        # Content-Length and would otherwise bypass the header check below.
        app = web.Application(client_max_size=self._max_body_bytes)
        app.router.add_get("/health", self._handle_health)
        app.router.add_post("/webhooks/{route_name}", self._handle_webhook)
        # Multi-profile multiplexing: a /p/<profile>/webhooks/<route> prefix
        # routes the inbound event to that profile. Same handler; the profile is
        # captured from the path and stamped onto the SessionSource so the agent
        # turn resolves that profile's config/skills/credentials. Only honored
        # when gateway.multiplex_profiles is on (the handler validates).
        app.router.add_post("/p/{profile}/webhooks/{route_name}", self._handle_webhook)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        # Do not probe only one address family before binding. With the
        # dual-stack default, an IPv6-only listener can already own this port
        # while 127.0.0.1 still looks free.
        #
        # SO_REUSEADDR is platform-dependent:
        #   - macOS (BSD semantics): two wildcard/specific sockets with
        #     SO_REUSEADDR can silently split traffic while both servers
        #     report success — so disable it there.
        #   - Linux: SO_REUSEADDR only permits rebinding past TIME_WAIT
        #     (a second live listener needs SO_REUSEPORT, which we never
        #     set). Disabling it would make a quick gateway restart fail
        #     to bind for up to ~60s — so keep the default (enabled).
        site = web.TCPSite(
            self._runner,
            self._host,
            self._port,
            reuse_address=False if sys.platform == "darwin" else None,
        )
        try:
            await site.start()
        except OSError as exc:
            await self._runner.cleanup()
            self._runner = None
            logger.error(
                "[webhook] Could not bind %s:%d: %s. "
                "Set a different host or port in config.yaml under "
                "platforms.webhook.extra.",
                self._host or "all IPv4+IPv6 interfaces",
                self._port,
                exc,
            )
            return False
        # Runner-owned listeners stay closed until the runner has published
        # this exact adapter and claimed every recoverable durable operation.
        # Standalone use has no registry/recovery coordinator, so connect may
        # open its own gate immediately.
        standalone_intake = self.gateway_runner is None
        if standalone_intake:
            try:
                standalone_intake = not bool(
                    _quarantined_retirement_owners(self._operation_ledger)
                )
            except WebhookLedgerError:
                # A bounded-registry overflow is intentionally unrecoverable
                # in-process.  The listener may stay up for health diagnostics,
                # but it must not admit work under unresolved ownership.
                standalone_intake = False
                logger.exception(
                    "[webhook] Durable retirement quarantine blocks intake"
                )
        self._accepting_webhooks = standalone_intake
        self._mark_connected()

        route_names = ", ".join(self._routes.keys()) or "(none configured)"
        logger.info(
            "[webhook] Listening on %s:%d — routes: %s",
            self._host or "* (all interfaces, IPv4+IPv6)",
            self._port,
            route_names,
        )
        # Plugin-registered native handlers (ctx.register_platform_handler).
        self._wire_plugin_handlers(None)
        return True

    async def _run_ledger_mutation_barrier(
        self,
        operation: Callable[[], Any],
    ) -> Any:
        """Run one bounded SQLite mutation off-loop without late commits.

        ``asyncio.to_thread`` workers cannot be stopped after SQLite starts.
        If the awaiting lifecycle task is cancelled, wait for the bounded
        transaction to finish before propagating cancellation so disconnect
        can never retire an owner and then have an older recovery claim commit.
        """

        worker = asyncio.create_task(asyncio.to_thread(operation))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(worker)
            except BaseException:
                logger.exception(
                    "[webhook] Bounded ledger mutation failed during cancellation"
                )
            raise

    async def _run_retained_route_worker(
        self,
        operation: Callable[..., Any],
        *args: Any,
        cancellation_event: Optional[threading.Event] = None,
        slot_gate: Optional[Any] = None,
    ) -> Any:
        """Await an acquired route worker without freeing its slot early.

        Python executor work cannot be stopped when its awaiting request task is
        cancelled.  The caller acquires ``_route_worker_slots`` before any
        durable effect; cancellation detaches the await but keeps the process-wide
        slot until the real thread (and any bounded child process it owns) exits.
        """

        acquired_gate = slot_gate if slot_gate is not None else _route_worker_slots
        worker: Optional[asyncio.Task] = None
        detached = False
        try:
            worker = asyncio.create_task(asyncio.to_thread(operation, *args))
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            if worker is None:  # pragma: no cover - create_task is synchronous
                raise
            if cancellation_event is not None:
                # Route scripts own a subprocess and can terminate it
                # cooperatively. Do not let handler cancellation reach owner
                # retirement until the actual worker has acknowledged that
                # signal and exited.
                cancellation_event.set()
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        cancellation_event.set()
                        continue
                    except BaseException:
                        break
                try:
                    worker.exception()
                except BaseException:
                    pass
                raise
            detached = True

            def release_when_worker_exits(completed: asyncio.Task) -> None:
                try:
                    completed.exception()
                except BaseException:
                    # The request path has already reconciled its durable claim.
                    # This callback owns only resource-slot finalization.
                    pass
                try:
                    acquired_gate.release()
                except ValueError:
                    logger.critical(
                        "[webhook] Route worker slot accounting was corrupted"
                    )

            worker.add_done_callback(release_when_worker_exits)
            raise
        finally:
            if not detached:
                acquired_gate.release()

    async def disconnect(self) -> None:
        # Close the admission gate first, then let aiohttp drain every handler
        # that already crossed it. Only after no handler can create another row
        # may this instance retire/fence all of its durable operations.
        self._accepting_webhooks = False
        self._lifecycle_retiring = True
        marker_recorded = False
        try:
            marker_recorded = _quarantine_failed_retirement(self._operation_ledger)
        except Exception:
            logger.exception(
                "[webhook] Could not register owner retirement before disconnect"
            )
        try:
            if self._runner:
                await self._runner.cleanup()
                self._runner = None
        finally:
            try:
                async with self._recovery_pump_lock:
                    while True:
                        retired = await self._run_ledger_mutation_barrier(
                            self._operation_ledger.retire_instance
                        )
                        if not retired.has_more:
                            break
                        # Bound each transaction and give transport/shutdown
                        # coordination a chance to run between pages.  The
                        # exact marker remains installed across every yield.
                        await asyncio.sleep(0)
            except BaseException:
                if not marker_recorded:
                    try:
                        marker_recorded = _quarantine_failed_retirement(
                            self._operation_ledger
                        )
                    except Exception:
                        logger.exception(
                            "[webhook] Could not identify the failed retirement ledger"
                        )
                self._set_fatal_error(
                    "webhook_retirement_failed",
                    "Durable webhook ownership could not be fenced during disconnect",
                    retryable=True,
                )
                logger.exception(
                    "[webhook] Failed to fence durable operations during disconnect"
                )
                if not marker_recorded:
                    logger.critical(
                        "[webhook] Retirement quarantine capacity was exhausted; "
                        "webhook intake will remain fail-closed until process restart"
                    )
                # A caller coordinating replacement must observe that this was
                # not a clean disconnect.  Runner cleanup wrappers may choose to
                # swallow the error, but the exact owner quarantine remains for
                # the replacement's mandatory recovery pass.
                raise
            _clear_quarantined_retirement_owner(
                self._operation_ledger,
                self._operation_ledger.instance_id,
            )
            self._mark_disconnected()
            logger.info("[webhook] Disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Consume only a positively marked final response under durable authority."""

        try:
            authority = self._operation_ledger.lookup_session(chat_id)
        except WebhookLedgerError as exc:
            logger.error(
                "[webhook] Durable authority lookup failed for %s: %s", chat_id, exc
            )
            return SendResult(success=False, error="Webhook authority lookup failed")
        if authority is None:
            logger.error("[webhook] Missing durable delivery authority for %s", chat_id)
            return SendResult(
                success=False,
                error="Missing admitted webhook delivery authority",
            )

        is_final = (
            isinstance(metadata, Mapping)
            and metadata.get("notify") is True
            and metadata.get("_interim_send") is not True
        )
        if not is_final:
            logger.debug("[webhook] Suppressed non-final send for %s", chat_id)
            return SendResult(success=True)

        carrier = {"v": 1, "kind": "agent_final"}
        try:
            staged = self._stage_exact_delivery(authority, content, carrier)
        except WebhookLedgerError as exc:
            logger.error(
                "[webhook] Could not stage final response for %s: %s", chat_id, exc
            )
            return SendResult(
                success=False,
                error="Webhook final response conflicts with durable authority",
            )

        result = await self._invoke_staged_target(staged)
        return SendResult(
            success=result.success,
            message_id=result.message_id,
            error=result.error,
            retryable=(
                result.disposition is WebhookTargetDeliveryDisposition.PRE_EFFECT_FAILED
            ),
            raw_response={"webhook_settlement": result.disposition.value},
        )

    async def _send_with_retry(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Any = None,
        max_retries: int = 2,
        base_delay: float = 2.0,
    ) -> SendResult:
        """One exact final attempt; webhook settlement owns all retry decisions."""

        del max_retries, base_delay
        return await self.send(
            chat_id=chat_id,
            content=content,
            reply_to=reply_to,
            metadata=metadata,
        )

    def _record_rate_limit_hit(
        self,
        route_name: str,
        now: float,
        *,
        profile: Optional[str] = None,
    ) -> bool:
        """Return True when the profile/route remains inside its window."""

        key = (profile or "default", route_name)
        window = self._rate_counts.get(key)
        if not isinstance(window, deque):
            new_window: Deque[float] = deque(window or ())
            self._rate_counts[key] = new_window
            window = new_window
        cutoff = now - _RATE_WINDOW_SECONDS
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= self._rate_limit:
            return False
        window.append(now)
        return True

    def _prune_rate_limit_buckets(
        self,
        bundles: Optional[Mapping[str, AuthenticatedRouteAuthority]] = None,
    ) -> None:
        """Discard counters whose exact published route authority is gone."""

        if bundles is None:
            bundles = {
                route_name: bundle
                for route_name, bundle in self._authenticated_route_bundles.items()
                if route_name in self._routes
            }
        active_authorities = {
            (str(bundle.authority[0]), route_name)
            for route_name, bundle in bundles.items()
        }
        for key in tuple(self._rate_counts):
            if key not in active_authorities:
                self._rate_counts.pop(key, None)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "webhook"}

    def _render_prompt(
        self,
        template: str,
        payload: dict,
        event_type: str,
        route_name: str,
    ) -> str:
        """Render a prompt template with bounded, parseable raw envelopes.

        Supports dot-notation access into nested dicts:
        ``{pull_request.title}`` → ``payload["pull_request"]["title"]``

        ``{__raw__}`` retains the 4,000-byte default. ``{__raw__:N}``
        selects an explicit complete-envelope byte cap between 64 and
        1,000,000 bytes. The cap includes metadata and JSON escaping.
        """
        if not template:
            raw_envelope = self._render_raw_payload(
                payload, _RAW_PAYLOAD_DEFAULT_CAP_BYTES
            )
            return (
                f"Webhook event '{event_type}' on route "
                f"'{route_name}':\n\n```json\n{raw_envelope}\n```"
            )

        def _resolve(match: re.Match) -> str:
            token = match.group("token")
            if token.startswith("__raw__"):
                cap_text = match.group("raw_cap")
                if cap_text is None:
                    cap = _RAW_PAYLOAD_DEFAULT_CAP_BYTES
                else:
                    try:
                        cap = int(cap_text)
                    except (TypeError, ValueError) as exc:
                        raise ValueError(
                            f"invalid raw payload cap: {cap_text!r}"
                        ) from exc
                return self._render_raw_payload(payload, cap)
            if token == "event_type":
                return event_type
            value: Any = payload
            for part in token.split("."):
                if isinstance(value, dict):
                    value = value.get(part, f"{{{token}}}")
                else:
                    return f"{{{token}}}"
            if isinstance(value, (dict, list)):
                return json.dumps(value, indent=2)[:2000]
            return str(value)

        # Deliberately one pass: brace-shaped strings emitted from payloads do
        # not become a second template language.
        return _PROMPT_TOKEN_RE.sub(_resolve, template)

    def _render_raw_payload(
        self, payload: dict, cap: int = _RAW_PAYLOAD_DEFAULT_CAP_BYTES
    ) -> str:
        """Render valid JSON whose complete UTF-8 encoding fits ``cap``."""

        try:
            requested_cap = int(cap)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid raw payload cap: {cap!r}") from exc
        if not (
            _RAW_PAYLOAD_MIN_CAP_BYTES <= requested_cap <= _RAW_PAYLOAD_MAX_CAP_BYTES
        ):
            raise ValueError(
                "raw payload cap must be between "
                f"{_RAW_PAYLOAD_MIN_CAP_BYTES} and "
                f"{_RAW_PAYLOAD_MAX_CAP_BYTES} bytes"
            )

        serialized = json.dumps(payload, indent=2, ensure_ascii=False)
        serialized_bytes = serialized.encode("utf-8")
        original_bytes = len(serialized_bytes)

        def _envelope(payload_text: str, *, truncated: bool) -> str:
            return json.dumps(
                {
                    "payload": payload_text,
                    "truncated": truncated,
                    "original_bytes": original_bytes,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )

        full = _envelope(serialized, truncated=False)
        if len(full.encode("utf-8")) <= requested_cap:
            return full

        smallest = _envelope("", truncated=True)
        if len(smallest.encode("utf-8")) > requested_cap:
            raise ValueError("raw payload cap is too small for envelope metadata")

        low = 0
        high = min(original_bytes, requested_cap)
        best = smallest
        while low <= high:
            midpoint = (low + high) // 2
            bounded = serialized_bytes[:midpoint].decode("utf-8", errors="ignore")
            candidate = _envelope(bounded, truncated=True)
            if len(candidate.encode("utf-8")) <= requested_cap:
                best = candidate
                low = midpoint + 1
            else:
                high = midpoint - 1
        return best

    def _render_delivery_extra(self, extra: dict, payload: dict) -> dict:
        """Render delivery_extra template values with payload data."""
        rendered: Dict[str, Any] = {}
        for key, value in extra.items():
            if isinstance(value, str):
                rendered[key] = self._render_prompt(value, payload, "", "")
            else:
                rendered[key] = value
        return rendered
