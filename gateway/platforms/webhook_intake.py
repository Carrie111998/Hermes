"""Intake responsibilities for the webhook adapter."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import threading
import time
from typing import Any, Callable, Dict, Mapping, Optional

try:
    from aiohttp import web
except ImportError:
    web = None  # type: ignore[assignment]

from gateway.platforms.base import (
    MessageType,
)
from gateway.platforms.webhook_contract import (
    WebhookContractError,
    WebhookEnvelope,
    WebhookPayloadContractError,
    WebhookRouteConfig,
    WebhookRouteScopeError,
)
from gateway.platforms.webhook_filters import (
    WebhookPreparedScript,
    WebhookScriptDisposition,
    read_bounded_regular_file_snapshot,
)
from gateway.platforms.webhook_ledger import (
    AdmitDisposition,
    AdmitSaturationReason,
    OperationState,
    WebhookLedgerError,
    WebhookLedgerCapacityError,
    WebhookLedgerTransitionError,
)

from gateway.platforms.webhook_common import (
    WebhookMessageEvent,
    _DYNAMIC_ROUTES_CONTENT_RECHECK_SECONDS,
    _DYNAMIC_ROUTES_FILENAME,
    _INSECURE_NO_AUTH,
    _MAX_DURABLE_AUTHORITY_SNAPSHOT_BYTES,
    _MAX_DURABLE_EVENT_SNAPSHOT_BYTES,
    _MAX_DYNAMIC_ROUTES_FILE_BYTES,
    _MAX_RENDERED_PROMPT_BYTES,
    _PROFILE_REJECTED,
    _RATE_WINDOW_SECONDS,
    _canonical_snapshot_size,
    _is_loopback_host,
    _plain_json_snapshot,
    _reject_duplicate_json_keys,
    _reject_nonfinite_json,
    _route_worker_slots,
    _snapshot_route_config,
)

logger = logging.getLogger(__name__)


class WebhookIntakeMixin:
    async def _handle_health(self, request: "web.Request") -> "web.Response":
        """Report readiness from the same authority that gates webhook intake."""

        del request
        accepting_webhooks = self._intake_is_authoritative("default")
        try:
            # Serialize durable probes and keep SQLite lock/disk latency off
            # the public aiohttp event loop.
            async with self._health_capacity_lock:
                global_capacity_available = await asyncio.to_thread(
                    self._operation_ledger.has_global_admission_capacity
                )
        except WebhookLedgerError:
            global_capacity_available = False
            logger.exception("[webhook] Durable capacity health check failed")
        self._global_ledger_saturated = not global_capacity_available
        if not accepting_webhooks or not global_capacity_available:
            error = (
                "Durable webhook evidence capacity is exhausted"
                if accepting_webhooks and not global_capacity_available
                else "Webhook intake is not active"
            )
            return web.json_response(
                {
                    "status": "degraded",
                    "platform": "webhook",
                    "accepting_webhooks": False,
                    "error": error,
                },
                status=503,
                headers=({} if not global_capacity_available else {"Retry-After": "5"}),
            )
        return web.json_response({
            "status": "ok",
            "platform": "webhook",
            "accepting_webhooks": True,
        })

    def _withdraw_changed_dynamic_authorities(
        self,
        candidate_dynamic: Mapping[str, dict],
    ) -> None:
        """Keep only candidate routes with the exact already-bound authority.

        A rejected policy update must not leave the prior, broader route live.
        New conflicting routes are also excluded. Unchanged routes remain
        available, so JSON ordering cannot choose which shared-key scope wins.
        """

        unchanged: dict[str, dict] = {}
        for route_name, route in candidate_dynamic.items():
            if route_name not in self._dynamic_routes:
                continue
            try:
                _, _, authorities, _, _, _, _ = (
                    self._route_authentication_authority_snapshot({
                        route_name: route,
                    })
                )
            except (WebhookContractError, WebhookLedgerError):
                continue
            if authorities.get(route_name) == (
                self._authenticated_route_authorities.get(route_name)
            ):
                unchanged[route_name] = route
        self._dynamic_routes = unchanged
        self._routes = {**unchanged, **self._static_routes}
        self._prune_rate_limit_buckets()

    def _reload_dynamic_routes(self) -> None:
        """Atomically publish a content-versioned dynamic route snapshot."""

        from hermes_constants import get_hermes_home

        hermes_home = get_hermes_home()
        subs_path = hermes_home / _DYNAMIC_ROUTES_FILENAME
        if not subs_path.exists():
            if self._dynamic_routes_file_present or self._dynamic_routes:
                # File removal is itself authoritative revocation. Publish it
                # before touching unrelated static authorities so a static
                # policy mismatch or storage fault cannot keep deleted dynamic
                # keys live.
                self._dynamic_routes = {}
                self._routes = dict(self._static_routes)
                self._prune_rate_limit_buckets()
                try:
                    static_routes = dict(self._static_routes)
                    self._bind_route_authentication_authorities(static_routes)
                except (WebhookContractError, WebhookLedgerError) as exc:
                    logger.error(
                        "[webhook] Dynamic routes removed, but static route "
                        "authority refresh failed: %s",
                        exc,
                    )
                self._dynamic_routes_content_sha256 = None
                self._dynamic_routes_file_identity = None
                self._dynamic_routes_rejected_content_sha256 = None
                self._dynamic_routes_rejected_file_identity = None
                self._dynamic_routes_transient_file_identity = None
                self._dynamic_routes_retry_after = 0.0
                self._dynamic_routes_integrity_recheck_identity = None
                self._dynamic_routes_integrity_recheck_after = 0.0
                self._dynamic_routes_file_present = False
                logger.debug(
                    "[webhook] Dynamic subscriptions file removed; cleared "
                    "dynamic routes"
                )
            else:
                self._dynamic_routes_content_sha256 = None
                self._dynamic_routes_file_identity = None
                self._dynamic_routes_rejected_content_sha256 = None
                self._dynamic_routes_rejected_file_identity = None
                self._dynamic_routes_transient_file_identity = None
                self._dynamic_routes_retry_after = 0.0
                self._dynamic_routes_integrity_recheck_identity = None
                self._dynamic_routes_integrity_recheck_after = 0.0
                self._dynamic_routes_file_present = False
            return

        observed_digest: Optional[str] = None
        observed_identity: Optional[tuple[int, ...]] = None
        # Once existence has been observed, any inability to read the file is
        # authority loss, not permission to retain the previous dynamic keys.
        # Initialize the fail-closed candidate before stat/read so a removal,
        # replacement, or permission race at either syscall withdraws them.
        candidate_dynamic: Optional[dict[str, dict]] = {}
        try:
            path_stat = subs_path.stat()
            path_identity = (
                int(path_stat.st_dev),
                int(path_stat.st_ino),
                int(path_stat.st_size),
                int(path_stat.st_mtime_ns),
                int(path_stat.st_ctime_ns),
            )
            now = time.monotonic()
            # Stat identity changes are loaded immediately. For unchanged
            # identity, periodically reopen and hash because neither Windows
            # creation time nor POSIX mtime/ctime reliably exposes every
            # same-size mmap write. Publish the gate before the bounded read so
            # request floods cannot amplify a 4 MiB integrity check.
            if (
                path_identity == self._dynamic_routes_integrity_recheck_identity
                and now < self._dynamic_routes_integrity_recheck_after
            ):
                return
            self._dynamic_routes_integrity_recheck_identity = path_identity
            self._dynamic_routes_integrity_recheck_after = (
                now + _DYNAMIC_ROUTES_CONTENT_RECHECK_SECONDS
            )
            if (
                path_identity == self._dynamic_routes_transient_file_identity
                and now < self._dynamic_routes_retry_after
            ):
                return
            snapshot = read_bounded_regular_file_snapshot(
                subs_path,
                max_bytes=_MAX_DYNAMIC_ROUTES_FILE_BYTES,
            )
            stat_result = snapshot.stat_result
            observed_identity = (
                int(stat_result.st_dev),
                int(stat_result.st_ino),
                int(stat_result.st_size),
                int(stat_result.st_mtime_ns),
                int(stat_result.st_ctime_ns),
            )
            self._dynamic_routes_integrity_recheck_identity = observed_identity
            if (
                observed_identity == self._dynamic_routes_transient_file_identity
                and now < self._dynamic_routes_retry_after
            ):
                return
            raw_config = snapshot.content
            observed_digest = hashlib.sha256(raw_config).hexdigest()
            if observed_digest == self._dynamic_routes_content_sha256:
                self._dynamic_routes_file_identity = observed_identity
                self._dynamic_routes_transient_file_identity = None
                self._dynamic_routes_retry_after = 0.0
                return
            if observed_digest == self._dynamic_routes_rejected_content_sha256:
                self._dynamic_routes_rejected_file_identity = observed_identity
                return
            try:
                data = json.loads(
                    raw_config.decode("utf-8"),
                    object_pairs_hook=_reject_duplicate_json_keys,
                    parse_constant=_reject_nonfinite_json,
                )
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                ValueError,
                RecursionError,
            ) as exc:
                raise WebhookContractError(
                    "dynamic subscriptions file is not canonical JSON"
                ) from exc
            if not isinstance(data, dict):
                raise WebhookContractError(
                    "dynamic subscriptions root must be an object"
                )
            # Merge: static routes take precedence over dynamic ones.
            # Reject any dynamic route whose effective secret is empty —
            # an empty secret would cause _handle_webhook to skip HMAC
            # validation entirely, letting unauthenticated callers in.
            new_dynamic: Dict[str, dict] = {}
            for k, v in data.items():
                if k in self._static_routes:
                    continue
                if not isinstance(v, dict):
                    logger.warning(
                        "[webhook] Dynamic route '%s' skipped: route must be an object",
                        k,
                    )
                    continue
                candidate = dict(v)
                if not candidate.get("provider") and not candidate.get(
                    "signature_mode"
                ):
                    description = str(candidate.get("description") or "")
                    if description.startswith("Agent-created subscription:"):
                        # Historical CLI subscriptions were emitted with the
                        # GitHub-style body HMAC but omitted the provider. This
                        # content-derived migration runs once at config load;
                        # request headers never participate.
                        candidate["provider"] = "github"
                        logger.warning(
                            "[webhook] Dynamic route '%s' migrated in memory to "
                            "provider=github; re-save it with a provider field",
                            k,
                        )
                    else:
                        logger.warning(
                            "[webhook] Dynamic route '%s' skipped: explicit "
                            "provider or signature_mode is required",
                            k,
                        )
                        continue
                request_profile = candidate.get("profile", "default")
                try:
                    WebhookRouteConfig.bind(
                        k,
                        candidate,
                        headers={},
                        request_profile=request_profile,
                    )
                except WebhookContractError as exc:
                    logger.warning("[webhook] Dynamic route '%s' skipped: %s", k, exc)
                    continue
                effective_secret = candidate.get("secret", self._global_secret)
                if not isinstance(effective_secret, str) or not effective_secret:
                    logger.warning(
                        "[webhook] Dynamic route '%s' skipped: 'secret' is "
                        "missing or empty. Set a valid HMAC secret, or use "
                        "'%s' to explicitly disable auth (testing only).",
                        k,
                        _INSECURE_NO_AUTH,
                    )
                    continue
                if effective_secret == _INSECURE_NO_AUTH and not _is_loopback_host(
                    self._host
                ):
                    logger.warning(
                        "[webhook] Dynamic route '%s' skipped: INSECURE_NO_AUTH "
                        "is only allowed on loopback hosts. Current host: '%s'.",
                        k,
                        self._host,
                    )
                    continue
                new_dynamic[k] = candidate

            candidate_routes = {**new_dynamic, **self._static_routes}
            candidate_dynamic = new_dynamic
            self._bind_route_authentication_authorities(candidate_routes)
            self._dynamic_routes = new_dynamic
            self._routes = candidate_routes
            self._dynamic_routes_content_sha256 = observed_digest
            self._dynamic_routes_file_identity = observed_identity
            self._dynamic_routes_rejected_content_sha256 = None
            self._dynamic_routes_rejected_file_identity = None
            self._dynamic_routes_transient_file_identity = None
            self._dynamic_routes_retry_after = 0.0
            self._dynamic_routes_file_present = True
            logger.info(
                "[webhook] Reloaded %d dynamic route(s): %s",
                len(self._dynamic_routes),
                ", ".join(self._dynamic_routes.keys()) or "(none)",
            )
        except (
            WebhookContractError,
            WebhookLedgerCapacityError,
            WebhookLedgerTransitionError,
        ) as exc:
            # Keep the last complete, durably bound snapshot. A candidate that
            # cannot be validated must never be partially published according
            # to JSON order.
            if observed_identity is not None:
                self._dynamic_routes_rejected_file_identity = observed_identity
                self._dynamic_routes_rejected_content_sha256 = observed_digest
                self._dynamic_routes_file_present = True
            if candidate_dynamic is not None:
                self._withdraw_changed_dynamic_authorities(candidate_dynamic)
            logger.error("[webhook] Failed to reload dynamic routes: %s", exc)
        except Exception as exc:
            # Store or filesystem failures are retried after a short bounded
            # delay. They must not mark unapplied configuration as current,
            # otherwise a one-off SQLite fault leaves a revoked key live.
            if observed_identity is not None:
                self._dynamic_routes_transient_file_identity = observed_identity
                self._dynamic_routes_retry_after = time.monotonic() + 1.0
                self._dynamic_routes_file_present = True
            if candidate_dynamic is not None:
                # Availability of the global binding store cannot override a
                # locally observed revocation. Preserve only routes whose old
                # immutable authority exactly matches the candidate and retry
                # publication later.
                self._withdraw_changed_dynamic_authorities(candidate_dynamic)
            logger.error(
                "[webhook] Transient dynamic route reload failure; will retry: %s",
                exc,
            )

    def _resolve_request_profile(
        self,
        request: "web.Request",
        route_config: Optional[Mapping[str, Any]] = None,
    ):
        """Resolve + validate the /p/<profile>/ URL prefix on a webhook request.

        Returns:
          - ``None`` when no profile prefix is present.
          - the profile name (str) when multiplexing serves it, or when a
            single-profile self prefix matches an explicitly bound route.
            For an ordinary default-bound route, that same self prefix returns
            ``None`` so the canonical ``default`` stamp retains its authority.
          - ``_PROFILE_REJECTED`` when a prefix is present but the profile is
            unknown/unconfigured, or names a profile this single-profile
            gateway does not serve (handler returns 404).
        """
        profile = (request.match_info.get("profile") or "").strip()
        if not profile:
            return None
        runner = self.gateway_runner
        cfg = getattr(runner, "config", None)
        if getattr(cfg, "multiplex_profiles", False) is not True:
            # Prefix supplied but multiplexing is off. Only a self-referential
            # prefix (naming this gateway's own profile) may fall through to
            # the bare route; anything else fails closed — silently ignoring
            # the prefix served the gateway owner's routes/config under
            # another profile's URL (#91583 defect 2).
            try:
                from hermes_cli.profiles import profile_matches_home

                if profile_matches_home(profile):
                    configured_profile = (
                        route_config.get("profile")
                        if isinstance(route_config, Mapping)
                        and "profile" in route_config
                        else None
                    )
                    if (
                        isinstance(configured_profile, str)
                        and configured_profile.strip() == profile
                    ):
                        return profile
                    return None
            except Exception:
                pass
            return _PROFILE_REJECTED
        try:
            from hermes_cli.profiles import profiles_to_serve

            served = {
                name
                for name, _ in profiles_to_serve(
                    multiplex=True,
                    profile_allowlist=getattr(cfg, "multiplex_profile_allowlist", None),
                )
            }
        except Exception:
            return _PROFILE_REJECTED
        if profile not in served:
            return _PROFILE_REJECTED
        return profile

    @staticmethod
    def _route_allows_profile(
        route_config: dict,
        request_profile: Optional[str],
    ) -> bool:
        """Compatibility facade over the canonical route-binding authority."""

        try:
            WebhookRouteConfig.bind(
                "profile-check",
                route_config,
                headers={},
                request_profile=request_profile,
            )
        except WebhookContractError:
            return False
        return True

    async def _handle_webhook(self, request: "web.Request") -> "web.Response":
        """Authenticate, durably admit, snapshot, and dispatch one webhook."""

        if not self._accepting_webhooks:
            return web.json_response(
                {"status": "unavailable", "error": "Webhook intake is draining"},
                status=503,
                headers={"Retry-After": "5"},
            )

        self._reload_dynamic_routes()
        route_name = request.match_info.get("route_name", "")
        live_route_config = self._routes.get(route_name)
        if not isinstance(live_route_config, dict):
            return web.json_response(
                {"error": f"Unknown route: {route_name}"}, status=404
            )
        if live_route_config.get(
            "secret", self._global_secret
        ) == _INSECURE_NO_AUTH and not _is_loopback_host(self._host):
            return web.json_response(
                {"error": "Local auth bypass is not allowed on this host"},
                status=403,
            )
        profile = self._resolve_request_profile(request, live_route_config)
        if profile is _PROFILE_REJECTED:
            return web.json_response(
                {"error": "Unknown or unconfigured profile"}, status=404
            )
        try:
            preliminary_route = WebhookRouteConfig.bind(
                route_name,
                live_route_config,
                headers=request.headers,
                request_profile=profile,
            )
        except WebhookRouteScopeError:
            return web.json_response(
                {"error": f"Unknown route: {route_name}"}, status=404
            )
        except WebhookContractError:
            return web.json_response(
                {
                    "status": "failed",
                    "error": "Webhook route is misconfigured",
                },
                status=500,
            )
        if not self._intake_is_authoritative(preliminary_route.profile):
            return web.json_response(
                {"status": "unavailable", "error": "Webhook intake is not active"},
                status=503,
                headers={"Retry-After": "5"},
            )
        if not preliminary_route.enabled:
            return web.json_response(
                {"error": f"Route disabled: {route_name}"}, status=403
            )
        try:
            if self._authenticated_route_snapshot is None:
                # Compatibility for direct handler tests/embedders. A real
                # listener publishes every bundle before it opens intake.
                self._bind_route_authentication_authorities(self._routes)
            route_bundle = self._authenticated_route_bundles.get(route_name)
            if route_bundle is None or (
                _snapshot_route_config(live_route_config) != route_bundle.route_config
            ):
                raise WebhookContractError(
                    "webhook route has no exact published authority bundle"
                )
        except (WebhookContractError, WebhookLedgerError) as exc:
            logger.error("[webhook] Invalid route %s: %s", route_name, exc)
            target_unavailable = "not an outbound webhook target" in str(exc)
            return web.json_response(
                {
                    "status": "unavailable" if target_unavailable else "failed",
                    "error": (
                        "Webhook target or grant is unavailable"
                        if target_unavailable
                        else "Webhook route is misconfigured"
                    ),
                },
                status=503 if target_unavailable else 500,
            )
        route_config = route_bundle.route_config
        try:
            bound_route = WebhookRouteConfig.bind(
                route_name,
                route_config,
                headers=request.headers,
                request_profile=profile,
            )
        except WebhookRouteScopeError:
            logger.warning(
                "[webhook] Route %s is not authorized for profile %r",
                route_name,
                profile or "default",
            )
            return web.json_response(
                {"error": f"Unknown route: {route_name}"}, status=404
            )
        except WebhookContractError as exc:
            logger.error("[webhook] Invalid route %s: %s", route_name, exc)
            return web.json_response(
                {"error": "Webhook route is misconfigured"}, status=500
            )
        content_length = request.content_length or 0
        if content_length > self._max_body_bytes:
            return web.json_response({"error": "Payload too large"}, status=413)
        content_encoding = request.headers.get("Content-Encoding", "").strip().lower()
        if content_encoding not in {"", "identity"}:
            return web.json_response(
                {"error": "Unsupported Content-Encoding"}, status=415
            )
        try:
            raw_body = await request.read()
        except web.HTTPRequestEntityTooLarge:
            return web.json_response({"error": "Payload too large"}, status=413)
        except Exception as exc:
            logger.error("[webhook] Failed to read body: %s", exc)
            return web.json_response({"error": "Bad request"}, status=400)
        if len(raw_body) > self._max_body_bytes:
            return web.json_response({"error": "Payload too large"}, status=413)
        if not self._route_bundle_is_current(route_name, route_bundle):
            return web.json_response(
                {
                    "status": "unavailable",
                    "error": "Webhook route authority changed during request",
                },
                status=503,
            )

        secret = route_bundle.secret
        if not isinstance(secret, str) or not secret:
            logger.error("[webhook] Route %s has no HMAC secret", route_name)
            return web.json_response(
                {"error": "Webhook route is missing an HMAC secret"}, status=403
            )
        if secret == _INSECURE_NO_AUTH and not _is_loopback_host(self._host):
            return web.json_response(
                {"error": "Local auth bypass is not allowed on this host"},
                status=403,
            )
        if not self._route_owns_unique_authenticated_secret(
            route_name,
            secret,
            bound_route.signature_mode,
            route_bundle,
        ):
            logger.error(
                "[webhook] Route %s does not own a unique authentication secret",
                route_name,
            )
            return web.json_response(
                {
                    "status": "failed",
                    "error": "Webhook route authentication is misconfigured",
                },
                status=500,
            )
        if secret == _INSECURE_NO_AUTH:
            verification_receipt = self._issue_local_bypass_receipt(
                request, raw_body, bound_route
            )
        else:
            verification_receipt = self._verify_signature_receipt(
                request, raw_body, secret, bound_route
            )
            if verification_receipt is None:
                logger.warning("[webhook] Invalid signature for route %s", route_name)
                return web.json_response({"error": "Invalid signature"}, status=401)

        media_type = (
            request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        )
        if media_type != "application/json" and not media_type.endswith("+json"):
            return web.json_response({"error": "Unsupported Content-Type"}, status=415)
        try:
            parsed_payload = json.loads(
                raw_body,
                parse_constant=_reject_nonfinite_json,
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            ValueError,
            RecursionError,
        ):
            return web.json_response({"error": "Cannot parse JSON body"}, status=400)
        if not isinstance(parsed_payload, dict):
            return web.json_response(
                {"error": "JSON body must be an object"}, status=400
            )
        try:
            envelope = WebhookEnvelope.from_receipt(
                verification_receipt,
                raw_body=raw_body,
                media_type=media_type,
                authority_profile=str(route_bundle.authority[0]),
            )
        except WebhookPayloadContractError as exc:
            logger.warning(
                "[webhook] Authenticated payload rejected for route %s: %s",
                route_name,
                exc,
            )
            return web.json_response(
                {"error": "Invalid authenticated webhook payload"}, status=400
            )
        except WebhookContractError as exc:
            logger.warning(
                "[webhook] Authenticated metadata rejected for route %s: %s",
                route_name,
                exc,
            )
            return web.json_response(
                {"error": "Invalid authenticated webhook metadata"}, status=401
            )

        # Only authenticated identity material may enter the execution carrier.
        # Provider IDs observed beside a body-only MAC remain diagnostics and
        # must not become the event message ID or any downstream reply anchor.
        delivery_id = envelope.delivery_id
        # Recheck at the last synchronous seam before admission. Registry
        # removal and disconnect run on this event loop, so no retirement can
        # interleave between this check and the non-awaiting SQLite claim.
        if not self._intake_is_authoritative(bound_route.profile):
            return web.json_response(
                {"status": "unavailable", "error": "Webhook intake is draining"},
                status=503,
                headers={"Retry-After": "5"},
            )
        try:
            admission = self._operation_ledger.admit(envelope)
        except WebhookLedgerError:
            logger.exception("[webhook] Durable admission failed for %s", route_name)
            self._fence_intake_for_durable_transition_failure(
                "durable admission failure"
            )
            return web.json_response(
                {"status": "unavailable", "error": "Durable admission failed"},
                status=503,
                headers={"Retry-After": "5"},
            )
        if admission.disposition is AdmitDisposition.CONFLICT:
            return web.json_response(
                {
                    "status": "conflict",
                    "delivery_id": delivery_id,
                    "error": "Replay identity was reused with a different body",
                },
                status=409,
            )
        if admission.disposition is AdmitDisposition.INDETERMINATE:
            return web.json_response(
                {
                    "status": "indeterminate",
                    "delivery_id": delivery_id,
                    "error": "Previous outcome requires reconciliation",
                },
                status=409,
            )
        if admission.disposition is AdmitDisposition.SATURATED:
            global_saturation = admission.saturation not in {
                AdmitSaturationReason.SCOPE_RECORD_LIMIT,
                AdmitSaturationReason.SCOPE_STORAGE_LIMIT,
            }
            if global_saturation:
                self._global_ledger_saturated = True
            capacity_scope = "global" if global_saturation else "route scope"
            return web.json_response(
                {
                    "status": "unavailable",
                    "error": (
                        f"Durable webhook evidence capacity exhausted for "
                        f"{capacity_scope}"
                    ),
                },
                status=503,
            )
        if admission.disposition is AdmitDisposition.DUPLICATE:
            return web.json_response(
                {"status": "duplicate", "delivery_id": delivery_id}, status=200
            )
        if admission.disposition is AdmitDisposition.ACTIVE:
            active = admission.authority
            if (
                active is not None
                and active.state is OperationState.DELIVERY_READY
                and active.owner_instance == self._operation_ledger.instance_id
            ):
                resumed = await self._invoke_staged_target(active)
                target_kind = (
                    str(active.target_snapshot.get("kind"))
                    if isinstance(active.target_snapshot, Mapping)
                    else None
                )
                return self._target_http_response(
                    resumed,
                    route=active.route,
                    delivery_id=delivery_id,
                    target_kind=target_kind,
                )
            return web.json_response(
                {"status": "in_progress", "delivery_id": delivery_id}, status=202
            )

        authority = admission.authority
        if admission.disposition is not AdmitDisposition.ACCEPTED or authority is None:
            logger.error("[webhook] Ledger returned an invalid admission result")
            return web.json_response(
                {"status": "unavailable", "error": "Webhook admission failed"},
                status=503,
            )
        self._global_ledger_saturated = False
        script_started = False

        def release_before_effect() -> bool:
            try:
                released = self._operation_ledger.release_pre_effect(authority)
                if not released:
                    logger.warning(
                        "[webhook] Claim was not releasable for %s",
                        authority.operation_id,
                    )
                    self._fence_intake_for_durable_transition_failure(
                        "pre-effect release authority loss"
                    )
                return released
            except BaseException as exc:
                logger.exception(
                    "[webhook] Could not release operation %s",
                    authority.operation_id,
                )
                self._fence_intake_for_durable_transition_failure(
                    "pre-effect release failure"
                )
                if not isinstance(exc, Exception):
                    raise
                return False

        # Resource-backed authority validation is deliberately paid only by a
        # newly admitted identity. Durable duplicates/conflicts/active retries
        # above are indexed ledger results and cannot amplify filesystem,
        # profile, skill, or adapter-resolution work.
        if not self._route_bundle_is_current(route_name, route_bundle):
            release_before_effect()
            return web.json_response(
                {
                    "status": "unavailable",
                    "error": "Webhook route authority changed during request",
                },
                status=503,
            )

        # Charge each fresh authenticated identity before any resource-backed
        # proof. Durable duplicates never reach this path, while unique IDs
        # over quota stay an indexed release + 429.
        if not self._record_rate_limit_hit(
            route_name,
            time.time(),
            profile=envelope.authority_profile,
        ):
            if not release_before_effect():
                return web.json_response(
                    {
                        "status": "unavailable",
                        "error": "Rate-limited claim could not be released",
                    },
                    status=503,
                )
            return web.json_response(
                {"error": "Rate limit exceeded"},
                status=429,
                headers={"Retry-After": str(int(_RATE_WINDOW_SECONDS))},
            )

        if not self._authentication_authority_proof_slots.acquire(blocking=False):
            release_before_effect()
            return web.json_response(
                {
                    "status": "unavailable",
                    "error": "Webhook authentication authority is busy",
                },
                status=503,
                headers={"Retry-After": "1"},
            )

        slot_detached = False

        async def run_authority_worker(
            worker: Callable[..., Any],
            *args: Any,
        ) -> Any:
            nonlocal slot_detached
            task = asyncio.create_task(asyncio.to_thread(worker, *args))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                slot_detached = True

                def release_when_worker_exits(completed: asyncio.Task) -> None:
                    try:
                        completed.exception()
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        pass
                    self._authentication_authority_proof_slots.release()

                task.add_done_callback(release_when_worker_exits)
                raise

        try:
            durable_bindings = self._route_bundle_authentication_bindings(route_bundle)
            authority_error: Optional[Exception] = None
            durable_authority_matches = True
            if durable_bindings:
                try:
                    durable_authority_matches = await run_authority_worker(
                        self._authentication_authority_ledger.authentication_keys_match,
                        durable_bindings,
                    )
                except asyncio.CancelledError:
                    release_before_effect()
                    raise
                except Exception as exc:
                    authority_error = exc
                    durable_authority_matches = False
                except BaseException:
                    release_before_effect()
                    raise
            bundle_still_current = self._route_bundle_is_current(
                route_name,
                route_bundle,
            )
            if not durable_authority_matches or not bundle_still_current:
                release_before_effect()
                withdrew = False
                if not durable_authority_matches and bundle_still_current:
                    withdrew = self._withdraw_live_route(route_name, route_bundle)
                if authority_error is not None:
                    logger.error(
                        "[webhook] Durable authentication authority proof failed "
                        "for %s: %s",
                        route_name,
                        authority_error,
                    )
                return web.json_response(
                    {
                        "status": "unavailable",
                        "error": (
                            "Webhook authentication authority was withdrawn"
                            if withdrew
                            else "Webhook route authority changed during request"
                        ),
                    },
                    status=503,
                )

            try:
                live_authority_matches = await run_authority_worker(
                    self._live_route_authority_matches,
                    route_name,
                    route_bundle,
                )
            except asyncio.CancelledError:
                release_before_effect()
                raise
            except Exception as exc:
                release_before_effect()
                logger.error(
                    "[webhook] Live route authority proof failed for %s: %s",
                    route_name,
                    exc,
                )
                return web.json_response(
                    {
                        "status": "unavailable",
                        "error": "Webhook route authority proof failed",
                    },
                    status=503,
                )
            except BaseException:
                release_before_effect()
                raise
            bundle_still_current = self._route_bundle_is_current(
                route_name,
                route_bundle,
            )
            if not live_authority_matches or not bundle_still_current:
                release_before_effect()
                withdrew = False
                if not live_authority_matches and bundle_still_current:
                    withdrew = self._withdraw_live_route(route_name, route_bundle)
                if withdrew:
                    logger.error(
                        "[webhook] Withdrew route %s after its effective authority "
                        "changed; rotate the route secret before re-enabling it",
                        route_name,
                    )
                return web.json_response(
                    {
                        "status": "unavailable",
                        "error": (
                            "Webhook route authority changed and was withdrawn"
                            if withdrew
                            else "Webhook route authority changed during request"
                        ),
                    },
                    status=503,
                )
        finally:
            if not slot_detached:
                self._authentication_authority_proof_slots.release()

        payload = envelope.mutable_payload()
        event_type = envelope.event_type
        source = self._source_for_envelope(envelope)

        def settle_without_effect(reason: str) -> bool:
            try:
                settled = self._operation_ledger.settle_no_effect(authority, reason)
            except BaseException as exc:
                logger.exception(
                    "[webhook] Could not settle operation %s", authority.operation_id
                )
                self._fence_intake_for_durable_transition_failure(
                    "no-effect settlement failure"
                )
                if not isinstance(exc, Exception):
                    raise
                return False
            if not settled:
                self._fence_intake_for_durable_transition_failure(
                    "no-effect settlement authority loss"
                )
            return settled

        if bound_route.events and event_type not in bound_route.events:
            if not settle_without_effect("event not selected by route"):
                return web.json_response(
                    {"status": "unavailable", "error": "Settlement failed"},
                    status=503,
                )
            return web.json_response({"status": "ignored", "event": event_type})

        def evaluate_route_filters() -> bool:
            with self._profile_runtime_context(source):
                filters_match = self._route_processor.route_filters_match(
                    route_bundle.filter_route_config,
                    payload,
                    event_type,
                    verification_receipt.verified_headers,
                )
            return filters_match

        filter_worker_slots = _route_worker_slots
        if not filter_worker_slots.acquire(blocking=False):
            release_before_effect()
            return web.json_response(
                {
                    "status": "unavailable",
                    "error": "Webhook route workers are busy",
                },
                status=503,
                headers={"Retry-After": "1"},
            )
        try:
            filters_match = await self._run_retained_route_worker(
                evaluate_route_filters,
                slot_gate=filter_worker_slots,
            )
        except asyncio.CancelledError:
            release_before_effect()
            raise
        except Exception:
            release_before_effect()
            logger.exception("[webhook] Route filter evaluation failed")
            return web.json_response(
                {"status": "failed", "error": "Webhook route filter failed"},
                status=500,
            )
        except BaseException:
            release_before_effect()
            raise
        if not self._route_bundle_is_current(route_name, route_bundle):
            release_before_effect()
            return web.json_response(
                {
                    "status": "unavailable",
                    "error": "Webhook route authority changed during request",
                },
                status=503,
            )
        if not filters_match:
            if not settle_without_effect("route filter did not match"):
                return web.json_response(
                    {"status": "unavailable", "error": "Settlement failed"},
                    status=503,
                )
            return web.json_response({
                "status": "ignored",
                "reason": "filter",
                "route": route_name,
            })

        deliver_only = route_config.get("deliver_only", False)
        if not isinstance(deliver_only, bool):
            release_before_effect()
            return web.json_response(
                {"error": "Webhook route has invalid deliver_only configuration"},
                status=500,
            )
        try:
            prepared_target = route_bundle.prepared_target
            admitted_toolsets = (
                [] if deliver_only else list(route_bundle.effective_toolsets)
            )
        except WebhookContractError as exc:
            release_before_effect()
            logger.error("[webhook] Target/grant preflight failed: %s", exc)
            return web.json_response(
                {
                    "status": "unavailable",
                    "error": "Webhook target or grant is unavailable",
                },
                status=503,
                headers={"Retry-After": "5"},
            )
        except Exception:
            release_before_effect()
            logger.exception("[webhook] Target/grant preflight failed")
            return web.json_response(
                {"status": "unavailable", "error": "Webhook preflight failed"},
                status=503,
                headers={"Retry-After": "5"},
            )

        prepared_script: Optional[WebhookPreparedScript] = None
        if route_config.get("script"):
            prepared_script = route_bundle.prepared_script
            if prepared_script is None:
                release_before_effect()
                logger.error(
                    "[webhook] Frozen script authority is unavailable for %s",
                    route_name,
                )
                return web.json_response(
                    {"status": "failed", "error": "Webhook route script failed"},
                    status=500,
                )
            script_worker_slots = _route_worker_slots
            if not script_worker_slots.acquire(blocking=False):
                release_before_effect()
                return web.json_response(
                    {
                        "status": "unavailable",
                        "error": "Webhook route workers are busy",
                    },
                    status=503,
                    headers={"Retry-After": "1"},
                )
            try:
                current_profile_generation = await asyncio.to_thread(
                    self._current_profile_authority_generation,
                    envelope.authority_profile,
                    route_name=route_name,
                )
            except asyncio.CancelledError:
                script_worker_slots.release()
                release_before_effect()
                raise
            except Exception as exc:
                script_worker_slots.release()
                release_before_effect()
                self._withdraw_live_route(route_name, route_bundle)
                logger.error(
                    "[webhook] Script profile authority is unavailable for %s: %s",
                    route_name,
                    exc,
                )
                return web.json_response(
                    {
                        "status": "unavailable",
                        "error": "Webhook script profile authority is unavailable",
                    },
                    status=503,
                )
            if not secrets.compare_digest(
                route_bundle.profile_generation,
                current_profile_generation,
            ):
                script_worker_slots.release()
                release_before_effect()
                self._withdraw_live_route(route_name, route_bundle)
                return web.json_response(
                    {
                        "status": "unavailable",
                        "error": "Webhook script profile authority changed",
                    },
                    status=503,
                )
            try:
                script_started = self._operation_ledger.mark_script_started(authority)
            except BaseException as exc:
                script_worker_slots.release()
                logger.exception("[webhook] Script start fence failed")
                self._fence_intake_for_durable_transition_failure(
                    "script-start transition failure"
                )
                if not isinstance(exc, Exception):
                    raise
                return web.json_response(
                    {"status": "unavailable", "error": "Script authority failed"},
                    status=503,
                )
            if not script_started:
                script_worker_slots.release()
                self._fence_intake_for_durable_transition_failure(
                    "script-start authority loss"
                )
                return web.json_response(
                    {"status": "unavailable", "error": "Script authority was lost"},
                    status=503,
                )

            script_cancellation = threading.Event()

            def execute_route_script():
                with self._profile_runtime_context(source):
                    return self._route_processor.run_prepared_script(
                        prepared_script,
                        payload,
                        cancellation_event=script_cancellation,
                    )

            try:
                script_result = await self._run_retained_route_worker(
                    execute_route_script,
                    cancellation_event=script_cancellation,
                    slot_gate=script_worker_slots,
                )
            except asyncio.CancelledError:
                self._mark_indeterminate_or_fence(
                    authority,
                    "webhook route script was cancelled after start",
                    context="script cancellation",
                )
                raise
            except BaseException as exc:
                self._mark_indeterminate_or_fence(
                    authority,
                    exc,
                    context="unexpected script processing failure",
                )
                if not isinstance(exc, Exception):
                    raise
                logger.exception("[webhook] Route script processing failed")
                return web.json_response(
                    {
                        "status": "indeterminate",
                        "error": "Webhook route script requires reconciliation",
                    },
                    status=500,
                )
            if not self._route_bundle_is_current(route_name, route_bundle):
                self._mark_indeterminate_or_fence(
                    authority,
                    "webhook route authority changed after script start",
                    context="post-script authority change",
                )
                return web.json_response(
                    {
                        "status": "indeterminate",
                        "error": "Webhook route script requires reconciliation",
                    },
                    status=500,
                )
            if script_result.disposition is WebhookScriptDisposition.IGNORED:
                if not settle_without_effect("route script suppressed delivery"):
                    return web.json_response(
                        {"status": "unavailable", "error": "Settlement failed"},
                        status=503,
                    )
                return web.json_response({
                    "status": "ignored",
                    "reason": "script",
                    "route": route_name,
                })
            if script_result.disposition is not WebhookScriptDisposition.CONTINUE:
                self._mark_indeterminate_or_fence(
                    authority,
                    script_result.error or "route script outcome is unknown",
                    context="unknown script disposition",
                )
                return web.json_response(
                    {
                        "status": "indeterminate",
                        "error": "Webhook route script requires reconciliation",
                    },
                    status=500,
                )
            payload = script_result.payload or payload

        try:
            prompt = self._render_prompt(
                route_config.get("prompt", ""), payload, event_type, route_name
            )
            if route_bundle.prepared_skill is not None:
                prompt = route_bundle.prepared_skill.render(prompt)
            rendered_extra = self._render_delivery_extra(
                route_config.get("deliver_extra", {}), payload
            )
            with self._profile_runtime_context(source):
                target_snapshot = self._materialize_target(
                    prepared_target, rendered_extra
                )
        except BaseException as exc:
            if script_started:
                self._mark_indeterminate_or_fence(
                    authority,
                    exc,
                    context="post-script materialization failure",
                )
            else:
                release_before_effect()
            if not isinstance(exc, Exception):
                raise
            logger.error(
                "[webhook] Prompt/target materialization failed for %s: %s",
                route_name,
                exc,
            )
            return web.json_response(
                {"error": "Webhook route has invalid execution configuration"},
                status=500,
            )

        event_snapshot = {
            "v": 1,
            "mode": "direct" if deliver_only else "agent",
            "text": prompt,
            "payload": payload,
            "message_id": delivery_id,
            "source": source.to_dict(),
        }
        grant_snapshot = {
            "v": 1,
            "toolsets": admitted_toolsets,
            "profile_generation": route_bundle.profile_generation,
        }
        try:
            prompt_size = len(prompt.encode("utf-8"))
            event_snapshot_size = _canonical_snapshot_size(event_snapshot)
            target_snapshot_size = _canonical_snapshot_size(target_snapshot)
            grant_snapshot_size = _canonical_snapshot_size(grant_snapshot)
        except BaseException as exc:
            if script_started:
                self._mark_indeterminate_or_fence(
                    authority,
                    exc,
                    context="invalid post-script carrier",
                )
                if not isinstance(exc, Exception):
                    raise
                return web.json_response(
                    {
                        "status": "indeterminate",
                        "error": "Script produced an invalid durable carrier",
                    },
                    status=500,
                )
            release_before_effect()
            if not isinstance(exc, Exception):
                raise
            return web.json_response(
                {"error": "Webhook execution carrier is invalid"}, status=500
            )
        carrier_too_large = (
            prompt_size > _MAX_RENDERED_PROMPT_BYTES
            or event_snapshot_size > _MAX_DURABLE_EVENT_SNAPSHOT_BYTES
            or target_snapshot_size > _MAX_DURABLE_AUTHORITY_SNAPSHOT_BYTES
            or grant_snapshot_size > _MAX_DURABLE_AUTHORITY_SNAPSHOT_BYTES
        )
        if carrier_too_large:
            if script_started:
                self._mark_indeterminate_or_fence(
                    authority,
                    "script output exceeded durable webhook carrier limits",
                    context="oversized post-script carrier",
                )
                return web.json_response(
                    {
                        "status": "indeterminate",
                        "error": "Script output exceeded durable carrier limits",
                    },
                    status=500,
                )
            release_before_effect()
            return web.json_response(
                {"error": "Payload expands beyond durable webhook limits"},
                status=413,
            )
        try:
            prepared = self._operation_ledger.prepare(
                authority,
                event_snapshot=event_snapshot,
                target_snapshot=target_snapshot,
                grant_snapshot=grant_snapshot,
            )
        except BaseException as exc:
            if script_started:
                self._mark_indeterminate_or_fence(
                    authority,
                    exc,
                    context="post-script durable prepare failure",
                )
            else:
                release_before_effect()
            if not isinstance(exc, Exception):
                raise
            logger.exception("[webhook] Durable prepare failed")
            return web.json_response(
                {"status": "unavailable", "error": "Webhook prepare failed"},
                status=503,
                headers={"Retry-After": "5"},
            )

        if deliver_only:
            try:
                generation_is_current = await asyncio.to_thread(
                    self._recovery_profile_generation_is_current,
                    prepared,
                )
            except asyncio.CancelledError:
                self._mark_indeterminate_or_fence(
                    prepared,
                    "direct profile generation check was cancelled",
                    context="direct generation-check cancellation",
                )
                raise
            except BaseException as exc:
                self._mark_indeterminate_or_fence(
                    prepared,
                    exc,
                    context="direct generation-check failure",
                )
                if not isinstance(exc, Exception):
                    raise
                return web.json_response(
                    {
                        "status": "indeterminate",
                        "error": "Webhook profile authority could not be checked",
                        "delivery_id": delivery_id,
                    },
                    status=503,
                )
            if not generation_is_current:
                self._mark_indeterminate_or_fence(
                    prepared,
                    "webhook profile incarnation changed before direct execution",
                    context="direct profile generation mismatch",
                )
                return web.json_response(
                    {
                        "status": "indeterminate",
                        "error": "Webhook profile authority changed",
                        "delivery_id": delivery_id,
                    },
                    status=409,
                )
            try:
                entered_running = self._operation_ledger.mark_running(prepared)
            except BaseException as exc:
                logger.exception("[webhook] Direct running gate failed")
                self._fence_intake_for_durable_transition_failure(
                    "direct running-gate transition failure"
                )
                if not isinstance(exc, Exception):
                    raise
                return web.json_response(
                    {
                        "status": "unavailable",
                        "error": "Webhook running authority failed",
                        "delivery_id": delivery_id,
                    },
                    status=503,
                )
            if not entered_running:
                self._fence_intake_for_durable_transition_failure(
                    "direct running-gate authority loss"
                )
                return web.json_response(
                    {
                        "status": "unavailable",
                        "error": "Webhook running authority was lost",
                        "delivery_id": delivery_id,
                    },
                    status=503,
                )
            try:
                staged = self._stage_exact_delivery(
                    prepared, prompt, {"v": 1, "kind": "direct"}
                )
                result = await self._invoke_staged_target(staged)
            except asyncio.CancelledError:
                self._mark_indeterminate_or_fence(
                    prepared,
                    "direct delivery was cancelled after execution started",
                    context="direct-delivery cancellation",
                )
                raise
            except BaseException as exc:
                self._mark_indeterminate_or_fence(
                    prepared,
                    exc,
                    context="direct-delivery failure",
                )
                if not isinstance(exc, Exception):
                    raise
                logger.exception("[webhook] Direct delivery failed")
                return web.json_response(
                    {
                        "status": "indeterminate",
                        "error": "Direct delivery failed",
                        "delivery_id": delivery_id,
                    },
                    status=502,
                )
            return self._target_http_response(
                result,
                route=route_name,
                delivery_id=delivery_id,
                target_kind=target_snapshot["kind"],
            )

        try:
            event = WebhookMessageEvent(
                text=prompt,
                message_type=MessageType.TEXT,
                source=source,
                raw_message=_plain_json_snapshot(payload),
                message_id=delivery_id,
                webhook_authority=prepared,
                webhook_envelope=envelope,
                allow_gateway_control=False,
            )
            logger.info(
                "[webhook] admitted event=%s route=%s operation=%s",
                event_type,
                route_name,
                prepared.operation_id,
            )
            task = asyncio.create_task(self.handle_message(event))
        except BaseException as exc:
            self._mark_indeterminate_or_fence(
                prepared,
                exc,
                context="admission dispatch failure",
            )
            if not isinstance(exc, Exception):
                raise
            logger.exception("[webhook] Could not dispatch admitted operation")
            return web.json_response(
                {"error": "Webhook dispatch failed", "delivery_id": delivery_id},
                status=503,
            )
        self._background_tasks.add(task)

        def admission_task_done(done: "asyncio.Task") -> None:
            self._background_tasks.discard(done)
            failed = done.cancelled()
            if not failed:
                try:
                    failed = done.exception() is not None
                except BaseException:
                    failed = True
            if failed:
                self._mark_indeterminate_or_fence(
                    prepared,
                    "webhook admission task failed",
                    context="admission task failure",
                )

        task.add_done_callback(admission_task_done)
        return web.json_response(
            {
                "status": "accepted",
                "route": route_name,
                "event": event_type,
                "delivery_id": delivery_id,
                "deduplication": envelope.replay_identity.kind.value,
            },
            status=202,
        )
