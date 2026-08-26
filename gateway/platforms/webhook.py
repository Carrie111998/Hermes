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
  - handoff_to: trusted destination platform for exact-session handoff
  - deliver_only: if true, skip the agent — the rendered prompt IS the
    message that gets delivered.  Use for external push notifications
    (Supabase, monitoring alerts, inter-agent pings) where zero LLM cost
    and sub-second delivery matter more than agent reasoning.

Security:
  - HMAC secret is required per route (validated at startup)
  - Rate limiting per route (fixed-window, configurable)
  - Idempotency prevents duplicate agent runs on webhook retries; handoff
    routes use a durable state.db claim that survives gateway restarts
  - Body size limits checked before reading payload
  - Generic HMAC supports a V2 signature (X-Webhook-Signature-V2) that
    binds a timestamp into the signed data for replay protection; the
    legacy body-only V1 (X-Webhook-Signature) is deprecated but still
    accepted with a warning, since it has no replay protection
  - Set secret to "INSECURE_NO_AUTH" to skip validation (testing only)
"""

import asyncio
import base64
import binascii
import hashlib
import hmac
import inspect
import json
import logging
import re
import subprocess
import sys
import time
import uuid
from collections import deque
from typing import Any, Deque, Dict, List, Optional

try:
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
)
from gateway.platforms.webhook_filters import (
    DEFAULT_SCRIPT_TIMEOUT_SECONDS,
    WebhookRouteProcessor,
)
from gateway.response_filters import is_autonomous_silence_response

logger = logging.getLogger(__name__)


def _is_webhook_silence_response(content: Any) -> bool:
    """Whether an agent response means "deliberately say nothing".

    Webhook routes are autonomous background lanes: a subscription prompt tells
    the agent to answer with ``[SILENT]`` when a tick produced nothing worth a
    human's attention (a duplicate inbound, a stand-down because a sibling lane
    already replied, a routine close).  Nobody is waiting on the other end, so
    there is no reader for whom a "nothing happened" message is useful.

    The reason this is the loose autonomous rule rather than the live gateway's
    is what the two lanes optimise for.  In an interactive chat, swallowing a
    real answer because it happens to open with a marker is much worse than
    showing a stray marker, so ``is_intentional_silence_response`` demands the
    response be EXACTLY a marker.  A webhook run has the opposite payoff: the
    cost of a leaked non-story is a pointless notification on every tick, and
    models reliably add a sentence explaining why they stayed quiet — which
    under the strict rule flips the whole thing back to "deliver".  That is not
    a hypothetical: it is why a Helper support lane kept messaging its owner to
    report that it had nothing to report.

    So use the shared autonomous-lane matcher (also used by cron), which treats
    a marker on its own first or last line as silence while still delivering
    prose that merely mentions one mid-sentence.  Sharing the function keeps
    the two autonomous lanes from drifting apart, and keeps the interactive
    path untouched.
    """
    return is_autonomous_silence_response(content)

# Sentinel returned by _resolve_request_profile when a /p/<profile>/ prefix
# names a profile this gateway does not serve (→ 404). Distinct from None
# (no prefix / multiplexing off → handle as the default profile).
_PROFILE_REJECTED = object()

_BUILTIN_DELIVER_PLATFORMS = {
    "telegram", "discord", "slack", "signal", "sms", "whatsapp",
    "matrix", "mattermost", "homeassistant", "email", "dingtalk",
    "feishu", "wecom", "wecom_callback", "weixin", "bluebubbles",
    "qqbot", "yuanbao",
}

# Default bind host. ``None`` tells aiohttp/asyncio's ``create_server`` to bind
# BOTH address families (IPv4 + IPv6) — the portable dual-stack default.
#
# Why not "0.0.0.0" (the old default) or "::"?
#   - "0.0.0.0" binds IPv4 ONLY. On IPv6-only private networks — notably Fly.io
#     6PN, where an agent's ``<app>.internal`` name resolves to an ``fdaa:…``
#     IPv6 address — an IPv4-only listener is unreachable. That is exactly why
#     hosted-agent webhook routes were publicly unreachable: the edge router
#     reverse-proxies to ``<app>.internal:8644`` over 6PN (IPv6) but the adapter
#     was listening on 0.0.0.0 (v4 only) → connection refused.
#   - "::" is NOT a safe fix: on hosts where the kernel sets IPV6_V6ONLY=1
#     (verified on Fly machines), binding "::" yields an IPv6-ONLY socket, which
#     then breaks the IPv4 loopback health check (``curl 127.0.0.1:8644/health``)
#     and the AF_INET port-conflict probe in connect().
#   - ``None`` asks the event loop to create a listening socket per resolved
#     family, so both 127.0.0.1 (v4) and the 6PN fdaa (v6) are served regardless
#     of the bindv6only sysctl. Users can still pin a specific host via
#     ``platforms.webhook.extra.host``.
DEFAULT_HOST = None
DEFAULT_PORT = 8644
_INSECURE_NO_AUTH = "INSECURE_NO_AUTH"
_DYNAMIC_ROUTES_FILENAME = "webhook_subscriptions.json"
_RATE_WINDOW_SECONDS = 60.0
_SUPPORTED_HANDOFF_TARGETS = frozenset({"discord"})
_HANDOFF_DELIVERY_STATE_PREFIX = "webhook_handoff_delivery:"
_EVENT_HANDOFF_TARGET_KEY = "_webhook_handoff_to"
_EVENT_HANDOFF_MARKER_KEY = "_webhook_handoff_delivery"
_EVENT_HANDOFF_REQUESTED_KEY = "_webhook_handoff_requested"
_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY = "_webhook_handoff_ownership_conflict"
_EVENT_HANDOFF_COMPLETION_ATTEMPTED_KEY = (
    "_webhook_handoff_completion_attempted"
)
_EVENT_HANDOFF_ADMISSION_FUTURE_ATTR = (
    "_webhook_handoff_admission_future"
)
_EVENT_HANDOFF_ADMISSION_OWNER_ATTR = "_webhook_handoff_admission_owner"


class _HandoffDeliveryTargetConflict(RuntimeError):
    """A provider delivery ID is already owned by another target."""

    def __init__(self, state: Dict[str, Any], expected_target: str):
        self.state = state
        self.stored_target = str(state.get("platform") or "")
        super().__init__(
            "durable webhook delivery belongs to target "
            f"{self.stored_target!r}, not {expected_target!r}"
        )


class _HandoffRequestOwnershipConflict(RuntimeError):
    """The session's same-target handoff belongs to an interactive producer."""


# Hostnames/IP literals that only serve connections originating on the same
# machine. Anything else is treated as a public bind for safety-rail purposes.
_LOOPBACK_HOSTS = frozenset({
    "127.0.0.1",
    "localhost",
    "::1",
    "ip6-localhost",
    "ip6-loopback",
})


def _is_loopback_host(host: Optional[str]) -> bool:
    """True when `host` binds only to the local machine.

    Covers IPv4 loopback, the standard `localhost` alias, IPv6 loopback in
    both bracketed and bare form, and the common Debian-style aliases. Any
    falsy value (empty string, None) is conservatively treated as non-loopback
    because an unset host usually means the platform-default public bind.
    """
    if not host:
        return False
    return host.strip().lower() in _LOOPBACK_HOSTS


def _hmac_str_equal(provided: str, expected: str) -> bool:
    """Timing-safe equality for two ``str`` values, tolerant of non-ASCII input.

    ``hmac.compare_digest`` raises ``TypeError`` when given a ``str`` that
    contains non-ASCII characters. The ``provided`` value here is an
    attacker-controlled signature/token header on a public, unauthenticated
    webhook endpoint, so a single non-ASCII byte would otherwise raise out of
    the request handler and return a 500 instead of rejecting the request.
    Comparing as UTF-8 bytes keeps the constant-time guarantee while making a
    hostile header fail closed with a clean rejection.
    """
    return hmac.compare_digest(provided.encode(), expected.encode())


def check_webhook_requirements() -> bool:
    """Check if webhook adapter dependencies are available."""
    return AIOHTTP_AVAILABLE


class WebhookAdapter(BasePlatformAdapter):
    """Generic webhook receiver that triggers agent runs from HTTP POSTs."""

    # A handoff delivery is acknowledged only after its exact source route has
    # a durable active-turn owner.  Running without that owner would let a
    # losing restart path mutate or finalize another run's delivery state.
    requires_durable_run_admission: bool = True

    # No human is present to answer a "session restored — what next?" prompt:
    # webhook runs are event-triggered.  The startup auto-resume turn must
    # instruct the model to FINISH the interrupted work instead of emitting an
    # interactive acknowledgement that abandons the task (#57056).
    interactive_resume: bool = False

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.WEBHOOK)
        # ``host`` may be None (dual-stack default) or a user-pinned string.
        # A config value of empty string / null is normalised to None so it
        # also means "bind all families" rather than an invalid "" host.
        _cfg_host = config.extra.get("host", DEFAULT_HOST)
        self._host: Optional[str] = _cfg_host or None
        self._port: int = int(config.extra.get("port", DEFAULT_PORT))
        self._global_secret: str = config.extra.get("secret", "")
        self._static_routes: Dict[str, dict] = config.extra.get("routes", {})
        self._dynamic_routes: Dict[str, dict] = {}
        self._dynamic_routes_mtime: float = 0.0
        self._routes: Dict[str, dict] = dict(self._static_routes)
        self._runner = None
        # Routes already warned about legacy V1 body-only signatures
        # (once-per-route so a busy sender doesn't spam the log).
        self._v1_signature_warned: set[str] = set()

        # Delivery info keyed by session chat_id.
        #
        # Read by every send() invocation for the chat_id (status messages
        # AND the final response).  Cleaned up via TTL on each POST so the
        # dict stays bounded — see _prune_delivery_info().  Do NOT pop on
        # send(), or interim status messages (e.g. fallback notifications,
        # context-pressure warnings) will consume the entry before the
        # final response arrives, causing the response to silently fall
        # back to the "log" deliver type.
        self._delivery_info: Dict[str, dict] = {}
        self._delivery_info_created: Dict[str, float] = {}
        self._delivery_info_order: Deque[tuple[float, str]] = deque()
        # Handoff suppression must last for the whole active run even if a
        # later POST ages its delivery-info snapshot out of the one-hour TTL.
        # Entries are removed by on_processing_complete.
        self._active_handoff_sessions: set[str] = set()

        # Reference to gateway runner for cross-platform delivery (set externally)
        self.gateway_runner = None

        # Idempotency: TTL cache of recently processed delivery IDs.
        # Prevents duplicate agent runs when webhook providers retry.
        self._seen_deliveries: Dict[str, float] = {}
        self._idempotency_ttl: int = 3600  # 1 hour
        self._seen_deliveries_next_prune_at: float = 0.0

        # Rate limiting: per-route timestamps in a fixed window.
        self._rate_counts: Dict[str, Deque[float]] = {}
        self._rate_limit: int = int(config.extra.get("rate_limit", 30))  # per minute

        # Body size limit (auth-before-body pattern)
        self._max_body_bytes: int = int(
            config.extra.get("max_body_bytes", 1_048_576)
        )  # 1MB
        self._script_timeout_seconds: int = int(
            config.extra.get(
                "script_timeout_seconds",
                DEFAULT_SCRIPT_TIMEOUT_SECONDS,
            )
        )
        self._route_processor = WebhookRouteProcessor(
            script_timeout_seconds=self._script_timeout_seconds
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_handoff_target(
        route_name: str, route: Dict[str, Any]
    ) -> Optional[str]:
        """Return a normalized trusted handoff target for one route.

        ``handoff_to`` is control-plane configuration.  It is deliberately
        read only from the route dict and never passed through webhook payload
        interpolation.  The first supported destination is Discord; widening
        this allowlist is an explicit platform-support change, not something a
        request body can opt into.
        """
        if "handoff_to" not in route:
            return None

        raw_target = route.get("handoff_to")
        if not isinstance(raw_target, str) or not raw_target.strip():
            raise ValueError(
                f"[webhook] Route '{route_name}' has invalid handoff_to; "
                "expected 'discord'."
            )
        target = raw_target.strip().lower()
        if target not in _SUPPORTED_HANDOFF_TARGETS:
            raise ValueError(
                f"[webhook] Route '{route_name}' has unsupported handoff_to "
                f"target '{raw_target}'. Supported targets: discord."
            )
        if route.get("deliver_only"):
            raise ValueError(
                f"[webhook] Route '{route_name}' cannot combine handoff_to "
                "with deliver_only=true."
            )
        configured_profile = route.get("profile", "default")
        if (
            not isinstance(configured_profile, str)
            or configured_profile.strip() != "default"
        ):
            raise ValueError(
                f"[webhook] Route '{route_name}' cannot use handoff_to from a "
                "named multiplex profile. Configure the route on the default "
                "profile."
            )
        return target

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        # Load agent-created subscriptions before validating
        self._reload_dynamic_routes()

        # Validate routes at startup — secret is required per route
        for name, route in self._routes.items():
            self._validate_handoff_target(name, route)
            secret = route.get("secret", self._global_secret)
            if not secret:
                raise ValueError(
                    f"[webhook] Route '{name}' has no HMAC secret. "
                    f"Set 'secret' on the route or globally. "
                    f"For testing without auth, set secret to '{_INSECURE_NO_AUTH}'."
                )

            # Safety rail: refuse to start if INSECURE_NO_AUTH is combined with a
            # non-loopback bind. The escape hatch is for local testing only;
            # serving an unauthenticated route on a public interface is a
            # deployment-grade footgun we'd rather crash early than ship.
            if secret == _INSECURE_NO_AUTH and not _is_loopback_host(self._host):
                raise ValueError(
                    f"[webhook] Route '{name}' uses INSECURE_NO_AUTH secret "
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
                    raise ValueError(
                        f"[webhook] Route '{name}' has deliver_only=true but "
                        f"deliver is '{deliver}'. Direct delivery requires a "
                        f"real target (telegram, discord, slack, github_comment, etc.)."
                    )

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
        app.router.add_post(
            "/p/{profile}/webhooks/{route_name}", self._handle_webhook
        )

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
        self._mark_connected()

        route_names = ", ".join(self._routes.keys()) or "(none configured)"
        logger.info(
            "[webhook] Listening on %s:%d — routes: %s",
            self._host or "* (all interfaces, IPv4+IPv6)",
            self._port,
            route_names,
        )
        return True

    async def disconnect(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._mark_disconnected()
        logger.info("[webhook] Disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Deliver the agent's response to the configured destination.

        chat_id is ``webhook:{route}:{delivery_id}``.  The delivery info
        stored during webhook receipt is read with ``.get()`` (not popped)
        so that interim status messages emitted before the final response
        — fallback-model notifications, context-pressure warnings, etc. —
        do not consume the entry and silently downgrade the final response
        to the ``log`` deliver type.  TTL cleanup happens on POST.
        """
        if _is_webhook_silence_response(content):
            logger.info(
                "[webhook] Response for %s is a silence marker — not delivering", chat_id
            )
            return SendResult(success=True)

        delivery = self._delivery_info.get(chat_id, {})
        if chat_id in self._active_handoff_sessions or delivery.get("handoff_to"):
            # Handoff owns delivery exclusively.  The durable watcher creates
            # the destination thread and delivers there after atomically
            # moving the exact session; status/final/error messages must not
            # also leak through the legacy parent-channel delivery path.
            logger.debug(
                "[webhook] Suppressing legacy delivery for handoff session %s",
                chat_id,
            )
            return SendResult(success=True)
        deliver_type = delivery.get("deliver", "log")

        if deliver_type == "log":
            logger.info("[webhook] Response for %s: %s", chat_id, content[:200])
            return SendResult(success=True)

        if deliver_type == "github_comment":
            return await self._deliver_github_comment(content, delivery)

        # Cross-platform delivery — any platform with a gateway adapter.
        # Check both built-in names and plugin-registered platforms.
        _is_known_platform = deliver_type in _BUILTIN_DELIVER_PLATFORMS
        if not _is_known_platform:
            try:
                from gateway.platform_registry import platform_registry
                _is_known_platform = platform_registry.is_registered(deliver_type)
            except Exception:
                pass
        if self.gateway_runner and _is_known_platform:
            return await self._deliver_cross_platform(
                deliver_type, content, delivery
            )

        logger.warning("[webhook] Unknown deliver type: %s", deliver_type)
        return SendResult(
            success=False, error=f"Unknown deliver type: {deliver_type}"
        )

    def _prune_delivery_info(self, now: float) -> None:
        """Drop delivery_info entries older than the idempotency TTL.

        Mirrors the cleanup pattern used for ``_seen_deliveries``.  Called
        on each POST so the dict size is bounded by ``rate_limit * TTL``
        even if many webhooks fire and never receive a final response.
        """
        if len(self._delivery_info_order) < len(self._delivery_info_created):
            self._delivery_info_order = deque(
                (created_at, key)
                for key, created_at in sorted(
                    self._delivery_info_created.items(), key=lambda item: item[1]
                )
            )
        cutoff = now - self._idempotency_ttl
        while self._delivery_info_order and self._delivery_info_order[0][0] < cutoff:
            created_at, key = self._delivery_info_order.popleft()
            if self._delivery_info_created.get(key) != created_at:
                continue
            self._delivery_info.pop(key, None)
            self._delivery_info_created.pop(key, None)

    def _prune_seen_deliveries(self, now: float) -> None:
        """Occasionally prune expired delivery IDs without scanning every POST."""
        if now < self._seen_deliveries_next_prune_at:
            return
        cutoff = now - self._idempotency_ttl
        stale = [k for k, t in self._seen_deliveries.items() if t < cutoff]
        for k in stale:
            self._seen_deliveries.pop(k, None)
        self._seen_deliveries_next_prune_at = now + min(60.0, max(1.0, self._idempotency_ttl / 10))

    def _record_rate_limit_hit(self, route_name: str, now: float) -> bool:
        """Return True if route is still within limit after recording this hit."""
        window = self._rate_counts.get(route_name)
        if not isinstance(window, deque):
            new_window: Deque[float] = deque(window or ())
            self._rate_counts[route_name] = new_window
            window = new_window
        cutoff = now - _RATE_WINDOW_SECONDS
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= self._rate_limit:
            return False
        window.append(now)
        return True

    def _record_delivery_id(self, delivery_id: str, now: float) -> bool:
        """Return True when this delivery should be processed."""
        seen_at = self._seen_deliveries.get(delivery_id)
        if seen_at is not None and now - seen_at < self._idempotency_ttl:
            return False
        if seen_at is not None:
            self._seen_deliveries.pop(delivery_id, None)
        self._seen_deliveries[delivery_id] = now
        if len(self._seen_deliveries) > max(self._rate_limit * 2, 128):
            self._prune_seen_deliveries(now)
        return True

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "webhook"}

    def toolsets_for_source(self, source) -> Optional[List[str]]:
        """Per-route toolset override.

        Webhook session chat_ids are ``webhook:{route}:{delivery_id}``.
        When the matching route config carries a ``toolsets`` list, that list
        replaces the platform-level ``platform_toolsets.webhook`` resolution
        for this run only. Routes without the key keep the platform default
        (the intentionally constrained webhook-safe toolset), so a single
        trusted route (e.g. a localhost monitoring push) can be granted
        ``terminal`` without widening every other webhook route.

        Set via ``platforms.webhook.extra.routes.<name>.toolsets`` in
        config.yaml or a ``toolsets`` key on a subscription in
        ``webhook_subscriptions.json`` (manual edit — deliberately NOT
        exposed through `hermes webhook subscribe`, so an agent-created
        subscription cannot self-grant elevated tools).
        """
        chat_id = str(getattr(source, "chat_id", "") or "")
        parts = chat_id.split(":", 2)
        if len(parts) < 2 or parts[0] != "webhook":
            return None
        route_config = self._routes.get(parts[1])
        if not isinstance(route_config, dict):
            return None
        toolsets = route_config.get("toolsets")
        if not isinstance(toolsets, list) or not toolsets:
            return None
        cleaned = [str(t).strip() for t in toolsets if str(t).strip()]
        return cleaned or None

    # ------------------------------------------------------------------
    # HTTP handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        """GET /health — simple health check."""
        return web.json_response({"status": "ok", "platform": "webhook"})

    def _reload_dynamic_routes(self) -> None:
        """Reload agent-created subscriptions from disk if the file changed."""
        from hermes_constants import get_hermes_home
        hermes_home = get_hermes_home()
        subs_path = hermes_home / _DYNAMIC_ROUTES_FILENAME
        if not subs_path.exists():
            if self._dynamic_routes:
                self._dynamic_routes = {}
                self._routes = dict(self._static_routes)
                logger.debug("[webhook] Dynamic subscriptions file removed, cleared dynamic routes")
            return
        try:
            mtime = subs_path.stat().st_mtime
            if mtime <= self._dynamic_routes_mtime:
                return  # No change
            data = json.loads(subs_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            # Merge: static routes take precedence over dynamic ones.
            # Reject any dynamic route whose effective secret is empty —
            # an empty secret would cause _handle_webhook to skip HMAC
            # validation entirely, letting unauthenticated callers in.
            new_dynamic: Dict[str, dict] = {}
            for k, v in data.items():
                if k in self._static_routes:
                    continue
                effective_secret = v.get("secret", self._global_secret)
                if not effective_secret:
                    logger.warning(
                        "[webhook] Dynamic route '%s' skipped: 'secret' is "
                        "missing or empty. Set a valid HMAC secret, or use "
                        "'%s' to explicitly disable auth (testing only).",
                        k,
                        _INSECURE_NO_AUTH,
                    )
                    continue
                if (
                    effective_secret == _INSECURE_NO_AUTH
                    and not _is_loopback_host(self._host)
                ):
                    logger.warning(
                        "[webhook] Dynamic route '%s' skipped: INSECURE_NO_AUTH "
                        "is only allowed on loopback hosts. Current host: '%s'.",
                        k,
                        self._host,
                    )
                    continue
                try:
                    self._validate_handoff_target(k, v)
                except ValueError as exc:
                    logger.warning("[webhook] Dynamic route '%s' skipped: %s", k, exc)
                    continue
                new_dynamic[k] = v
            self._dynamic_routes = new_dynamic
            self._routes = {**self._dynamic_routes, **self._static_routes}
            self._dynamic_routes_mtime = mtime
            logger.info(
                "[webhook] Reloaded %d dynamic route(s): %s",
                len(self._dynamic_routes),
                ", ".join(self._dynamic_routes.keys()) or "(none)",
            )
        except Exception as e:
            logger.error("[webhook] Failed to reload dynamic routes: %s", e)

    def _resolve_request_profile(self, request: "web.Request"):
        """Resolve + validate the /p/<profile>/ URL prefix on a webhook request.

        Returns:
          - ``None`` when no profile prefix is present, or when multiplexing
            is off and the prefix names this gateway's own profile (the
            request is handled as the serving profile).
          - the profile name (str) when present, multiplexing is on, and the
            profile is one this gateway serves.
          - ``_PROFILE_REJECTED`` when a prefix is present but the profile is
            unknown/unconfigured, or names a profile this single-profile
            gateway does not serve (handler returns 404).
        """
        profile = (request.match_info.get("profile") or "").strip()
        if not profile:
            return None
        runner = self.gateway_runner
        cfg = getattr(runner, "config", None)
        if not getattr(cfg, "multiplex_profiles", False):
            # Prefix supplied but multiplexing is off. Only a self-referential
            # prefix (naming this gateway's own profile) may fall through to
            # the bare route; anything else fails closed — silently ignoring
            # the prefix served the gateway owner's routes/config under
            # another profile's URL (#91583 defect 2).
            try:
                from hermes_cli.profiles import profile_matches_home

                if profile_matches_home(profile):
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
                    profile_allowlist=getattr(
                        cfg, "multiplex_profile_allowlist", None
                    ),
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
        """Return whether a route is bound to the URL-selected profile.

        Omitting ``profile`` keeps a route on the default profile. An explicit
        null, blank, or non-string value is malformed and fails closed.
        """
        if "profile" not in route_config:
            configured_profile = "default"
        else:
            configured_profile = route_config.get("profile")
        if not isinstance(configured_profile, str):
            return False
        configured_profile = configured_profile.strip()
        if not configured_profile:
            return False
        effective_profile = request_profile or "default"
        return configured_profile == effective_profile

    async def _handle_webhook(self, request: "web.Request") -> "web.Response":
        """POST /webhooks/{route_name} — receive and process a webhook event."""
        # Hot-reload dynamic subscriptions on each request (mtime-gated, cheap)
        self._reload_dynamic_routes()

        route_name = request.match_info.get("route_name", "")
        route_config = self._routes.get(route_name)

        # Multi-profile: resolve + validate the /p/<profile>/ prefix if present.
        profile = self._resolve_request_profile(request)
        if profile is _PROFILE_REJECTED:
            return web.json_response(
                {"error": "Unknown or unconfigured profile"}, status=404
            )

        if not route_config:
            return web.json_response(
                {"error": f"Unknown route: {route_name}"}, status=404
            )

        if not self._route_allows_profile(route_config, profile):
            effective_profile = profile or "default"
            logger.warning(
                "[webhook] Route %s is not authorized for profile %r",
                route_name,
                effective_profile,
            )
            # Match the unknown-route response so callers cannot use profile
            # mismatches to enumerate route bindings.
            return web.json_response(
                {"error": f"Unknown route: {route_name}"}, status=404
            )

        # Disabled routes are kept in the subscriptions file (so the dashboard
        # can re-enable them) but reject incoming events.  Default-enabled:
        # only an explicit ``enabled: false`` turns a route off, matching the
        # mcp_servers ``enabled`` semantics.
        if route_config.get("enabled", True) is False:
            return web.json_response(
                {"error": f"Route disabled: {route_name}"}, status=403
            )

        try:
            handoff_to = self._validate_handoff_target(route_name, route_config)
        except ValueError as exc:
            logger.error("[webhook] Invalid route configuration: %s", exc)
            return web.json_response(
                {"error": "Webhook route is misconfigured"}, status=500
            )

        # ── Auth-before-body ─────────────────────────────────────
        # Check Content-Length before reading the full payload.
        content_length = request.content_length or 0
        if content_length > self._max_body_bytes:
            return web.json_response(
                {"error": "Payload too large"}, status=413
            )

        # Read body (must be done before any validation)
        try:
            raw_body = await request.read()
        except web.HTTPRequestEntityTooLarge:
            # aiohttp's client_max_size tripped — chunked or lying
            # Content-Length. Same 413 as the header check above.
            return web.json_response(
                {"error": "Payload too large"}, status=413
            )
        except Exception as e:
            logger.error("[webhook] Failed to read body: %s", e)
            return web.json_response({"error": "Bad request"}, status=400)
        if len(raw_body) > self._max_body_bytes:
            # Defense in depth: enforce the cap on the actual bytes read even
            # if the server-level limit was bypassed or misconfigured.
            return web.json_response(
                {"error": "Payload too large"}, status=413
            )

        # Validate HMAC signature FIRST (skip only for the explicit local-test
        # INSECURE_NO_AUTH mode). Missing/empty secrets must fail closed here,
        # not only during connect(), so direct handler reuse cannot turn a
        # network webhook route into an unauthenticated agent-dispatch surface.
        secret = route_config.get("secret", self._global_secret)
        if not secret:
            logger.error(
                "[webhook] Route %s has no HMAC secret; refusing request",
                route_name,
            )
            return web.json_response(
                {"error": "Webhook route is missing an HMAC secret"},
                status=403,
            )
        if secret != _INSECURE_NO_AUTH:
            if not self._validate_signature(request, raw_body, secret):
                logger.warning(
                    "[webhook] Invalid signature for route %s", route_name
                )
                return web.json_response(
                    {"error": "Invalid signature"}, status=401
                )

        # ── Rate limiting (after auth) ───────────────────────────
        now = time.time()
        if not self._record_rate_limit_hit(route_name, now):
            return web.json_response(
                {"error": "Rate limit exceeded"}, status=429
            )

        # Parse payload
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            # Try form-encoded as fallback
            try:
                import urllib.parse

                payload = dict(
                    urllib.parse.parse_qsl(raw_body.decode("utf-8"))
                )
            except Exception:
                return web.json_response(
                    {"error": "Cannot parse body"}, status=400
                )

        # Check event type filter
        event_type = (
            request.headers.get("X-GitHub-Event", "")
            or request.headers.get("X-GitLab-Event", "")
            or payload.get("event_type", "")
            or payload.get("type", "")
            or "unknown"
        )
        allowed_events = route_config.get("events", [])
        if allowed_events and event_type not in allowed_events:
            logger.debug(
                "[webhook] Ignoring event %s for route %s (allowed: %s)",
                event_type,
                route_name,
                allowed_events,
            )
            return web.json_response(
                {"status": "ignored", "event": event_type}
            )

        if not self._route_processor.route_filters_match(
            route_config, payload, event_type, request.headers
        ):
            logger.info(
                "[webhook] filtered event=%s route=%s",
                event_type,
                route_name,
            )
            return web.json_response(
                {
                    "status": "ignored",
                    "reason": "filter",
                    "route": route_name,
                }
            )

        if route_config.get("script"):
            # run_route_script shells out (subprocess.run, up to its timeout);
            # run it in a worker thread so it can't block the gateway event loop.
            keep, transformed_payload = await asyncio.to_thread(
                self._route_processor.run_route_script,
                route_config.get("script"),
                payload,
            )
            if not keep:
                logger.info(
                    "[webhook] script ignored event=%s route=%s",
                    event_type,
                    route_name,
                )
                return web.json_response(
                    {
                        "status": "ignored",
                        "reason": "script",
                        "route": route_name,
                    }
                )
            payload = transformed_payload or payload

        # Format prompt from template
        prompt_template = route_config.get("prompt", "")
        prompt = self._render_prompt(
            prompt_template, payload, event_type, route_name
        )

        # Inject skill content if configured.
        # We call build_skill_invocation_message() directly rather than
        # using /skill-name slash commands — the gateway's command parser
        # would intercept those and break the flow.
        skills = route_config.get("skills", [])
        if skills:
            try:
                from agent.skill_commands import (
                    build_skill_invocation_message,
                    get_skill_commands,
                )

                skill_cmds = get_skill_commands()
                for skill_name in skills:
                    cmd_key = f"/{skill_name}"
                    if cmd_key in skill_cmds:
                        skill_content = build_skill_invocation_message(
                            cmd_key, user_instruction=prompt
                        )
                        if skill_content:
                            prompt = skill_content
                            break  # Load the first matching skill
                    else:
                        logger.warning(
                            "[webhook] Skill '%s' not found", skill_name
                        )
            except Exception as e:
                logger.warning("[webhook] Skill loading failed: %s", e)

        # Build a unique delivery ID
        delivery_id = request.headers.get(
            "X-GitHub-Delivery",
            request.headers.get(
                "svix-id",
                request.headers.get("X-Request-ID", str(int(time.time() * 1000))),
            ),
        )

        # The delivery's routing identity is deterministic and side-effect
        # free. Build it before claiming handoff idempotency so the claim can
        # name the exact source route, and a bound running turn can reconstruct
        # that identity after a process restart.
        session_chat_id = f"webhook:{route_name}:{delivery_id}"
        source = self.build_source(
            chat_id=session_chat_id,
            chat_name=f"webhook/{route_name}",
            chat_type="webhook",
            user_id=f"webhook:{route_name}",
            user_name=route_name,
        )
        if profile and isinstance(profile, str):
            source.profile = profile
        elif handoff_to and getattr(
            getattr(self.gateway_runner, "config", None),
            "multiplex_profiles",
            False,
        ):
            # Both the unprefixed webhook URL and /p/default authorize the
            # same default-profile route. Stamp that effective profile so
            # session keys and the durable delivery identity cannot diverge
            # based on which equivalent URL the provider retried.
            source.profile = "default"
        if handoff_to:
            # Persist the provider identity in SessionSource so an active-turn
            # restart can reconstruct this delivery without process-local
            # event metadata.
            source.message_id = str(delivery_id)

        # ── Idempotency ─────────────────────────────────────────
        now = time.time()
        handoff_marker = None
        if handoff_to:
            # Claim the delivery in state_meta before acknowledging or starting
            # the agent. Unlike live routing metadata, this tombstone survives
            # source removal, destination reset/pruning, and compression to a
            # child session. The INSERT-if-absent is the concurrency boundary:
            # only its winner may start an agent run for this provider ID.
            handoff_marker = self._handoff_delivery_marker(
                profile=profile,
                route_name=route_name,
                delivery_id=delivery_id,
            )
            source_session_key = self._webhook_source_session_key(source)
            delivery_state_key = self._handoff_delivery_state_key(handoff_marker)
            accepted_state = self._handoff_delivery_state_value(
                handoff_marker,
                handoff_to,
                session_id=None,
                source_session_key=source_session_key,
            )
            accepted_delivery_state = self._parse_handoff_delivery_state(
                accepted_state,
                marker=handoff_marker,
                handoff_to=handoff_to,
            )
            admission_owner_nonce = uuid.uuid4().hex
            admission_lock_owned = False
            admission_lock_attempted = False

            async def _release_admission_lock() -> None:
                nonlocal admission_lock_attempted, admission_lock_owned
                if not admission_lock_attempted:
                    return
                await self._release_handoff_admission_lock(
                    delivery_state_key,
                    accepted_delivery_state,
                    admission_owner_nonce,
                )
                admission_lock_attempted = False
                admission_lock_owned = False

            try:
                admission_lock_attempted = True
                lock_result = await self._try_acquire_handoff_admission_lock(
                    delivery_state_key,
                    accepted_delivery_state,
                    admission_owner_nonce,
                )
                if lock_result is None:
                    raise RuntimeError(
                        "durable webhook admission lock is unavailable"
                    )
                admission_lock_owned = lock_result is True
                claimed = False
                if admission_lock_owned:
                    claimed = await self._session_db_call(
                        "set_meta_if_absent",
                        delivery_state_key,
                        accepted_state,
                    )
            except asyncio.CancelledError:
                await _release_admission_lock()
                raise
            except Exception as exc:
                try:
                    await _release_admission_lock()
                except Exception:
                    logger.debug(
                        "[webhook] Failed to release admission fence for %s",
                        delivery_id,
                        exc_info=True,
                    )
                logger.error(
                    "[webhook] Durable handoff delivery claim failed for %s: %s",
                    delivery_id,
                    exc,
                )
                return web.json_response(
                    {"error": "Webhook handoff state unavailable"}, status=503
                )

            if not claimed:
                try:
                    stored_state = await self._session_db_call(
                        "get_meta", delivery_state_key
                    )
                    delivery_state = self._parse_handoff_delivery_state(
                        stored_state,
                        marker=handoff_marker,
                        handoff_to=handoff_to,
                    )
                    if delivery_state.get("phase") == "accepted":
                        if not admission_lock_owned:
                            logger.info(
                                "[webhook] Delivery %s is still entering its "
                                "durable agent run",
                                delivery_id,
                            )
                            return web.json_response(
                                {
                                    "error": "Webhook handoff admission in progress",
                                    "delivery_id": delivery_id,
                                },
                                status=503,
                            )
                        logger.warning(
                            "[webhook] Recovering crash-left accepted delivery %s",
                            delivery_id,
                        )
                        # This retry owns the crash-released admission fence.
                        # Rebuild the event from its authenticated provider
                        # payload and continue to the same accepted→running CAS.
                        delivery_state = accepted_delivery_state
                    else:
                        await _release_admission_lock()
                    bound_session_id = delivery_state.get("session_id")
                    if (
                        delivery_state.get("phase") == "succeeded"
                        and bound_session_id
                    ):
                        await self._recover_unrequested_handoff(
                            str(bound_session_id), handoff_to
                        )
                    if delivery_state.get("phase") == "accepted":
                        # The retry is now the sole admitted owner and must
                        # continue into event construction below.
                        pass
                    else:
                        logger.info(
                            "[webhook] Skipping durable duplicate delivery %s",
                            delivery_id,
                        )
                        return web.json_response(
                            {"status": "duplicate", "delivery_id": delivery_id},
                            status=200,
                        )
                except asyncio.CancelledError:
                    await _release_admission_lock()
                    raise
                except _HandoffDeliveryTargetConflict as exc:
                    await _release_admission_lock()
                    bound_session_id = exc.state.get("session_id")
                    if (
                        exc.state.get("phase") == "succeeded"
                        and bound_session_id
                        and exc.stored_target in _SUPPORTED_HANDOFF_TARGETS
                    ):
                        try:
                            await self._recover_unrequested_handoff(
                                str(bound_session_id), exc.stored_target
                            )
                        except _HandoffRequestOwnershipConflict as ownership_exc:
                            # The durable provider tombstone still makes this
                            # replay terminal. Preserve the interactive owner
                            # and report the route retarget below instead of
                            # turning every retry into a permanent 503 loop.
                            logger.warning(
                                "[webhook] Skipping original-target recovery "
                                "for %s: %s",
                                delivery_id,
                                ownership_exc,
                            )
                        except Exception as recovery_exc:
                            logger.error(
                                "[webhook] Durable original-target recovery "
                                "failed for %s: %s",
                                delivery_id,
                                recovery_exc,
                            )
                            return web.json_response(
                                {"error": "Webhook handoff state unavailable"},
                                status=503,
                            )
                    logger.warning(
                        "[webhook] Durable duplicate target conflict for %s: %s",
                        delivery_id,
                        exc,
                    )
                    return web.json_response(
                        {
                            "message": (
                                "Delivery ID already claimed for a different "
                                "handoff target"
                            ),
                            "status": "conflict",
                            "reason": "handoff_target_changed",
                            "delivery_id": delivery_id,
                        },
                        status=200,
                    )
                except _HandoffRequestOwnershipConflict as exc:
                    await _release_admission_lock()
                    # The provider delivery is already durably consumed, but
                    # the bound session's handoff belongs to an explicit
                    # CLI/TUI request.  Preserve that request and return a
                    # terminal exact-once response: retrying this provider ID
                    # must neither run the agent again nor steal ownership.
                    logger.warning(
                        "[webhook] Durable duplicate ownership conflict for %s: %s",
                        delivery_id,
                        exc,
                    )
                    return web.json_response(
                        {
                            "message": (
                                "Delivery ID is bound to a session with an "
                                "interactive handoff request"
                            ),
                            "status": "conflict",
                            "reason": "handoff_owned_by_interactive_request",
                            "delivery_id": delivery_id,
                        },
                        status=200,
                    )
                except Exception as exc:
                    await _release_admission_lock()
                    logger.error(
                        "[webhook] Durable duplicate recovery failed for %s: %s",
                        delivery_id,
                        exc,
                    )
                    return web.json_response(
                        {"error": "Webhook handoff state unavailable"}, status=503
                    )

            # A proxy gateway has no protocol by which the remote agent can
            # prove that this exact input is durable before we acknowledge the
            # provider.  Keep the accepted tombstone retryable and fail before
            # dispatching any remote agent request.  Legacy webhook routes and
            # already-terminal duplicates take their existing paths above.
            runner = self.gateway_runner
            proxy_url_fn = getattr(runner, "_get_proxy_url", None)
            if callable(proxy_url_fn) and proxy_url_fn():
                await _release_admission_lock()
                logger.error(
                    "[webhook] Durable handoff delivery %s cannot run through "
                    "gateway proxy mode",
                    delivery_id,
                )
                return web.json_response(
                    {
                        "error": (
                            "Webhook handoff requires a local durable agent run; "
                            "gateway proxy mode is unsupported"
                        ),
                        "delivery_id": delivery_id,
                    },
                    status=503,
                )
        else:
            # Legacy routes retain their process-local one-hour cache exactly.
            if not self._record_delivery_id(delivery_id, now):
                logger.info(
                    "[webhook] Skipping duplicate delivery %s", delivery_id
                )
                return web.json_response(
                    {"status": "duplicate", "delivery_id": delivery_id},
                    status=200,
                )

        # ── Direct delivery mode (deliver_only) ─────────────────
        # Skip the agent entirely — the rendered prompt IS the message we
        # deliver.  Use case: external services (Supabase, monitoring,
        # cron jobs, other agents) that need to push a plain notification
        # to a user's chat with zero LLM cost.  Reuses the same HMAC auth,
        # rate limiting, idempotency, and template rendering as agent mode.
        if route_config.get("deliver_only"):
            delivery = {
                "deliver": route_config.get("deliver", "log"),
                "deliver_extra": self._render_delivery_extra(
                    route_config.get("deliver_extra", {}), payload
                ),
                "payload": payload,
            }
            logger.info(
                "[webhook] direct-deliver event=%s route=%s target=%s msg_len=%d delivery=%s",
                event_type,
                route_name,
                delivery["deliver"],
                len(prompt),
                delivery_id,
            )
            try:
                result = await self._direct_deliver(prompt, delivery)
            except Exception:
                logger.exception(
                    "[webhook] direct-deliver failed route=%s delivery=%s",
                    route_name,
                    delivery_id,
                )
                return web.json_response(
                    {"status": "error", "error": "Delivery failed", "delivery_id": delivery_id},
                    status=502,
                )

            if result.success:
                return web.json_response(
                    {
                        "status": "delivered",
                        "route": route_name,
                        "target": delivery["deliver"],
                        "delivery_id": delivery_id,
                    },
                    status=200,
                )
            # Delivery attempted but target rejected it — surface as 502
            # with a generic error (don't leak adapter-level detail).
            logger.warning(
                "[webhook] direct-deliver target rejected route=%s target=%s error=%s",
                route_name,
                delivery["deliver"],
                result.error,
            )
            return web.json_response(
                {"status": "error", "error": "Delivery failed", "delivery_id": delivery_id},
                status=502,
            )

        # Store delivery info for send().  Read by every send() invocation
        # for this chat_id (interim status messages and the final response),
        # so we do NOT pop on send.  TTL-based cleanup keeps the dict bounded.
        deliver_config = {
            "deliver": route_config.get("deliver", "log"),
            "deliver_extra": self._render_delivery_extra(
                route_config.get("deliver_extra", {}), payload
            ),
        }
        if handoff_to:
            deliver_config["handoff_to"] = handoff_to
            deliver_config["handoff_marker"] = handoff_marker
            self._active_handoff_sessions.add(session_chat_id)
        self._delivery_info[session_chat_id] = deliver_config
        self._delivery_info_created[session_chat_id] = now
        self._delivery_info_order.append((now, session_chat_id))
        self._prune_delivery_info(now)

        # Build event from the routing source established before idempotency.
        event = MessageEvent(
            text=prompt,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=payload,
            message_id=delivery_id,
        )
        if handoff_to:
            event.metadata[_EVENT_HANDOFF_TARGET_KEY] = handoff_to
            event.metadata[_EVENT_HANDOFF_MARKER_KEY] = handoff_marker
            admission_future = asyncio.get_running_loop().create_future()
            setattr(
                event,
                _EVENT_HANDOFF_ADMISSION_FUTURE_ATTR,
                admission_future,
            )
            setattr(
                event,
                _EVENT_HANDOFF_ADMISSION_OWNER_ATTR,
                admission_owner_nonce,
            )

        logger.info(
            "[webhook] %s event=%s route=%s prompt_len=%d delivery=%s",
            request.method,
            event_type,
            route_name,
            len(prompt),
            delivery_id,
        )

        if handoff_to:
            # Do not acknowledge a durable handoff while it is only an
            # unbound ``accepted`` tombstone. ``handle_message`` schedules the
            # actual run and returns quickly; the runner's persisted-input hook
            # resolves this future only after the marked user row and the
            # accepted→running route binding both commit.
            # A hard crash before that point drops the HTTP connection and its
            # sidecar lock, allowing the provider retry to adopt the delivery.
            try:
                await self.handle_message(event)
                if not admission_future.done() and source_session_key not in getattr(
                    self, "_session_tasks", {}
                ):
                    raise RuntimeError(
                        "webhook handoff agent run was not scheduled"
                    )
                admitted = await asyncio.shield(admission_future)
                if admitted is not True:
                    raise RuntimeError(
                        "webhook handoff agent run was not durably admitted"
                    )
            except asyncio.CancelledError:
                # The background owner may still commit running after the HTTP
                # peer disconnects. It releases the lock/future itself.
                if (
                    not admission_future.done()
                    and source_session_key
                    not in getattr(self, "_session_tasks", {})
                ):
                    self._resolve_handoff_admission(event, False)
                    await _release_admission_lock()
                raise
            except Exception as exc:
                self._resolve_handoff_admission(event, False)
                try:
                    await _release_admission_lock()
                except Exception:
                    logger.debug(
                        "[webhook] Failed to release admission fence for %s",
                        delivery_id,
                        exc_info=True,
                    )
                logger.error(
                    "[webhook] Handoff agent admission failed for %s: %s",
                    delivery_id,
                    exc,
                )
                return web.json_response(
                    {"error": "Webhook handoff admission failed"},
                    status=503,
                )
        else:
            # Legacy webhook handling remains fire-and-forget.
            task = asyncio.create_task(self.handle_message(event))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        return web.json_response(
            {
                "status": "accepted",
                "route": route_name,
                "event": event_type,
                "delivery_id": delivery_id,
            },
            status=202,
        )

    async def on_processing_complete(
        self, event: "MessageEvent", outcome: Any
    ) -> None:
        """Close a one-shot route or durably hand its exact session onward.

        Legacy webhook deliveries are one-shot: the ``delivery_id`` is baked
        into the session key, so their rows are ended after the run.  A trusted
        ``handoff_to`` route instead writes its durable delivery marker and
        requests the existing handoff watcher, leaving the row open so that
        watcher can move the exact transcript to the destination thread.

        This hook is the one seam that runs at the TRUE end of the run:
        ``BasePlatformAdapter._process_message_background`` fires it after the
        message handler returns, on the success, failure, and cancellation
        paths alike — so error runs are reaped too.  (``handle_message`` is
        fire-and-forget; wrapping IT closes before the run even starts.)
        ``end_session()`` is first-reason-wins and no-ops on an already-ended
        row, so this never clobbers a ``compression``/``agent_close`` reason.
        """
        handoff_to = event.metadata.get(_EVENT_HANDOFF_TARGET_KEY)
        if not handoff_to:
            delivery = self._delivery_info.get(event.source.chat_id, {})
            handoff_to = delivery.get("handoff_to")

        # Gateway shutdown deliberately bounds adapter teardown, while this
        # hook may still be reconciling an offloaded end/request/finalizer.
        # Let the runner defer storage close until this exact owner task ends,
        # including the legacy one-shot end_session path below.
        track_quiescence = getattr(
            self.gateway_runner,
            "_track_session_storage_quiescence",
            None,
        )
        if callable(track_quiescence):
            track_quiescence()

        if not handoff_to and event.active_turn_admission_failed:
            # Startup recovery rebuilds a metadata-free event from the stored
            # SessionSource. If this turn loses the active-route CAS, the
            # adapter hook never gets a chance to restore handoff metadata.
            # A durable webhook source is the only webhook source that persists
            # its provider delivery ID, so preserve that owner while legacy
            # one-shot webhooks continue through their normal close path.
            durable_delivery_id = getattr(event.source, "message_id", None)
            durable_marker = event.metadata.get(_EVENT_HANDOFF_MARKER_KEY)
            if durable_marker or durable_delivery_id:
                try:
                    identity = await self._handoff_delivery_identity_for_event(
                        event
                    )
                except Exception:
                    logger.error(
                        "[webhook] Could not verify failed active-turn "
                        "admission; preserving the source route fail-closed",
                        exc_info=True,
                    )
                    return
                if identity is None:
                    logger.error(
                        "[webhook] Durable delivery identity is missing after "
                        "failed active-turn admission; preserving the source "
                        "route fail-closed"
                    )
                    return
                await self._release_event_admission_if_accepted(event)
                return

        if not handoff_to:
            await self._end_webhook_session(event, event.source.chat_id)
            return

        ownership_conflict = bool(
            event.metadata.get(_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY)
            or event.active_turn_admission_failed
        )
        # A losing recovery event shares the winner's chat identity.  It may
        # release only its own accepted admission fence; it must not remove the
        # winner's process-local delivery suppression before returning.
        if not ownership_conflict:
            # Base invokes this hook after every response/error send attempt,
            # so no more webhook-adapter delivery can occur for this run.
            self._active_handoff_sessions.discard(event.source.chat_id)
        await self._release_event_admission_if_accepted(event)

        agent_run_succeeded = event.agent_run_failed is False
        # Base derives ProcessingOutcome from text-delivery accounting. A
        # successful agent turn whose truthy response contains only media can
        # therefore arrive as FAILURE because attachment sends do not call
        # _record_delivery(). The runner's explicit False stamp is authoritative
        # for that narrow false-negative; an absent/true stamp remains a genuine
        # failure, and cancellation is never promoted to success.
        media_only_agent_success = (
            outcome is ProcessingOutcome.FAILURE
            and agent_run_succeeded
        )
        if (
            outcome is ProcessingOutcome.SUCCESS and agent_run_succeeded
        ) or media_only_agent_success:
            # AsyncSessionDB writes run off-loop and cannot be cancelled once
            # SQLite has started them. Keep this task alive through a caller
            # cancellation and reconcile its durable result before Base invokes
            # this hook a second time with CANCELLED. A persisted pending row
            # must remain restart-recoverable, never be reaped by that second
            # callback while its request commit is still in flight.
            await self._await_cancel_resistant(
                self._request_webhook_handoff(event, str(handoff_to))
            )
            return

        if event.metadata.get(_EVENT_HANDOFF_REQUESTED_KEY) or ownership_conflict:
            # Cancellation after a successful durable request is a shutdown
            # boundary, not a failed webhook run. A concurrent interactive
            # request is likewise owned by its user-facing path and must not be
            # destroyed by webhook cleanup. Leave either row untouched.
            return

        # A callback-required turn can fail after its user row commits but
        # before the accepted→running CAS returns (including a hard-cancelled
        # sync/async bridge).  Preserve that exact route so a provider retry
        # can reuse the delivery-marked row instead of duplicating or losing
        # the prompt.  With no committed input, normal failed-run cleanup below
        # removes the provisional session and the accepted tombstone remains
        # retryable against a fresh row.
        try:
            identity = await self._handoff_delivery_identity_for_event(event)
            if identity is not None:
                marker, _state_key, _raw_state, state = identity
                if state.get("phase") == "accepted" and await self._session_db_call(
                    "has_webhook_handoff_input",
                    marker,
                ):
                    logger.warning(
                        "[webhook] Preserving accepted delivery %s with a "
                        "durably persisted input for provider retry",
                        marker,
                    )
                    return
        except Exception:
            logger.error(
                "[webhook] Could not verify accepted input durability; "
                "preserving the source route fail-closed",
                exc_info=True,
            )
            return

        reason = (
            "webhook_handoff_cancelled"
            if outcome is ProcessingOutcome.CANCELLED
            else "webhook_handoff_failed"
        )
        await self._finalize_webhook_handoff(event, reason)

    @staticmethod
    def _handoff_delivery_marker(
        *, profile: Optional[str], route_name: str, delivery_id: str
    ) -> str:
        """Stable, JSON-safe identity for durable webhook retry detection."""
        effective_profile = str(profile or "default").strip() or "default"
        return json.dumps(
            [effective_profile, str(route_name), str(delivery_id)],
            separators=(",", ":"),
        )

    @staticmethod
    def _handoff_delivery_state_key(marker: str) -> str:
        digest = hashlib.sha256(marker.encode("utf-8")).hexdigest()
        return f"{_HANDOFF_DELIVERY_STATE_PREFIX}{digest}"

    def _webhook_source_session_key(self, source: Any) -> str:
        """Resolve the durable route key before a handoff run is admitted."""
        runner = self.gateway_runner
        key_fn = getattr(runner, "_session_key_for_source", None)
        if callable(key_fn):
            session_key = key_fn(source)
        else:
            from gateway.session import build_session_key

            config = getattr(runner, "config", None)
            session_key = build_session_key(
                source,
                group_sessions_per_user=getattr(
                    config, "group_sessions_per_user", True
                ),
                thread_sessions_per_user=getattr(
                    config, "thread_sessions_per_user", False
                ),
                profile=getattr(source, "profile", None),
            )
        if not isinstance(session_key, str) or not session_key:
            raise RuntimeError("webhook source route key is unavailable")
        return session_key

    @staticmethod
    def _handoff_delivery_state_value(
        marker: str,
        handoff_to: str,
        *,
        session_id: Optional[str],
        source_session_key: str,
        phase: Optional[str] = None,
        active_turn_token: Optional[str] = None,
    ) -> str:
        from hermes_state import _WEBHOOK_HANDOFF_CLAIM_LOCK_PROTOCOL

        if phase is None:
            phase = "accepted" if session_id is None else "succeeded"
        if phase not in {"accepted", "running", "succeeded"}:
            raise ValueError("invalid webhook handoff delivery phase")
        if (phase == "accepted") != (session_id is None):
            raise ValueError("webhook handoff delivery phase/session mismatch")
        has_active_turn_owner = (
            isinstance(active_turn_token, str) and bool(active_turn_token)
        )
        if (phase == "running") != has_active_turn_owner:
            raise ValueError(
                "webhook handoff running phase requires active-turn ownership"
            )
        return json.dumps(
            {
                "marker": marker,
                "phase": phase,
                "platform": handoff_to,
                "session_id": session_id,
                "source_session_key": source_session_key,
                "active_turn_token": active_turn_token,
                "admission_token": hashlib.sha256(
                    f"webhook-admission\0{marker}".encode("utf-8")
                ).hexdigest(),
                "lock_protocol": _WEBHOOK_HANDOFF_CLAIM_LOCK_PROTOCOL,
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _parse_handoff_delivery_state(
        raw_state: Any,
        *,
        marker: str,
        handoff_to: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            state = json.loads(raw_state) if isinstance(raw_state, str) else None
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("invalid durable webhook delivery state") from exc
        if not isinstance(state, dict):
            raise RuntimeError("durable webhook delivery state is missing")
        if state.get("marker") != marker:
            raise RuntimeError(
                "durable webhook delivery belongs to a different route"
            )
        phase = state.get("phase")
        if phase not in {"accepted", "running", "succeeded"}:
            raise RuntimeError(
                "durable webhook delivery has an invalid lifecycle phase"
            )
        stored_target = state.get("platform")
        if not isinstance(stored_target, str) or not stored_target:
            raise RuntimeError(
                "durable webhook delivery has an invalid handoff target"
            )
        session_id = state.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            raise RuntimeError("durable webhook delivery has an invalid session id")
        if (phase == "accepted") != (session_id is None):
            raise RuntimeError(
                "durable webhook delivery lifecycle is inconsistent"
            )
        active_turn_token = state.get("active_turn_token")
        if (phase == "running") != (
            isinstance(active_turn_token, str) and bool(active_turn_token)
        ):
            raise RuntimeError(
                "durable webhook delivery has invalid active-turn ownership"
            )
        source_session_key = state.get("source_session_key")
        if (
            not isinstance(source_session_key, str)
            or not source_session_key
        ):
            raise RuntimeError(
                "durable webhook delivery has an invalid source route key"
            )
        admission_token = state.get("admission_token")
        lock_protocol = state.get("lock_protocol")
        if (
            not isinstance(admission_token, str)
            or not admission_token
            or not isinstance(lock_protocol, str)
            or not lock_protocol
        ):
            raise RuntimeError(
                "durable webhook delivery has an invalid admission fence"
            )
        if handoff_to is not None and stored_target != handoff_to:
            raise _HandoffDeliveryTargetConflict(state, handoff_to)
        return state

    async def _try_acquire_handoff_admission_lock(
        self,
        state_key: str,
        state: Dict[str, Any],
        owner_nonce: str,
    ) -> Optional[bool]:
        """Acquire the crash-released fence for one pre-run delivery."""
        result = await self._session_db_call(
            "try_acquire_webhook_delivery_admission_lock",
            state_key,
            state["admission_token"],
            state["lock_protocol"],
            owner_nonce,
        )
        return result if result in {True, False, None} else None

    async def _release_handoff_admission_lock(
        self,
        state_key: str,
        state: Dict[str, Any],
        owner_nonce: str,
    ) -> None:
        await self._session_db_call(
            "release_webhook_delivery_admission_lock",
            state_key,
            state["admission_token"],
            owner_nonce,
        )

    @staticmethod
    def _resolve_handoff_admission(
        event: "MessageEvent",
        admitted: bool,
    ) -> None:
        future = getattr(event, _EVENT_HANDOFF_ADMISSION_FUTURE_ATTR, None)
        if isinstance(future, asyncio.Future) and not future.done():
            future.set_result(admitted)

    async def _release_event_admission_if_accepted(
        self,
        event: "MessageEvent",
    ) -> None:
        """Release a pre-run fence when processing ended before binding."""
        self._resolve_handoff_admission(event, False)
        try:
            identity = await self._handoff_delivery_identity_for_event(event)
            if identity is None:
                return
            _marker, state_key, _raw_state, state = identity
            if state.get("phase") == "accepted":
                owner_nonce = getattr(
                    event,
                    _EVENT_HANDOFF_ADMISSION_OWNER_ATTR,
                    "",
                )
                if owner_nonce:
                    await self._release_handoff_admission_lock(
                        state_key,
                        state,
                        owner_nonce,
                    )
        except Exception:
            logger.debug(
                "[webhook] Failed to release an accepted delivery fence",
                exc_info=True,
            )

    async def _handoff_delivery_identity_for_event(
        self, event: "MessageEvent"
    ) -> Optional[tuple[str, str, str, Dict[str, Any]]]:
        """Load the original durable delivery identity for a live/resumed run."""
        marker = event.metadata.get(_EVENT_HANDOFF_MARKER_KEY)
        if not marker:
            route_name = str(getattr(event.source, "user_name", "") or "")
            delivery_id = str(
                getattr(event.source, "message_id", "") or ""
            )
            if not route_name or not delivery_id:
                return None
            marker = self._handoff_delivery_marker(
                profile=getattr(event.source, "profile", None),
                route_name=route_name,
                delivery_id=delivery_id,
            )
        marker = str(marker)
        state_key = self._handoff_delivery_state_key(marker)
        raw_state = await self._session_db_call("get_meta", state_key)
        if raw_state is None:
            return None
        state = self._parse_handoff_delivery_state(
            raw_state,
            marker=marker,
        )
        return marker, state_key, str(raw_state), state

    async def on_agent_run_started(
        self,
        event: "MessageEvent",
        *,
        session_key: str,
        session_id: str,
    ) -> Optional[str]:
        """Validate run ownership and return an input-persistence identity.

        An accepted delivery deliberately remains accepted here.  The runner
        passes the returned marker into the agent's early transcript write and
        invokes :meth:`on_agent_input_persisted` only after that write commits.
        This keeps HTTP 202 behind both durable input and route ownership.
        """
        identity = await self._handoff_delivery_identity_for_event(event)
        if identity is None:
            if event.metadata.get(_EVENT_HANDOFF_TARGET_KEY):
                raise RuntimeError("webhook handoff delivery state is missing")
            return None

        marker, state_key, raw_state, state = identity
        handoff_to = str(state["platform"])
        if handoff_to not in _SUPPORTED_HANDOFF_TARGETS:
            raise RuntimeError(f"unsupported handoff target '{handoff_to}'")
        if state["source_session_key"] != session_key:
            event.metadata[_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY] = True
            raise RuntimeError(
                "durable webhook delivery belongs to a different source route"
            )
        active_turn_token = getattr(event, "_gateway_active_turn_token", None)
        if not isinstance(active_turn_token, str) or not active_turn_token:
            event.metadata[_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY] = True
            raise RuntimeError("webhook agent active-turn ownership is missing")

        # Restore all process-local suppression/identity state before the
        # agent can emit status or final delivery. This is required for a
        # startup-resumed running delivery whose original adapter process died.
        event.metadata[_EVENT_HANDOFF_TARGET_KEY] = handoff_to
        event.metadata[_EVENT_HANDOFF_MARKER_KEY] = marker
        self._active_handoff_sessions.add(event.source.chat_id)

        phase = state["phase"]
        if phase == "accepted":
            admission_owner_nonce = getattr(
                event,
                _EVENT_HANDOFF_ADMISSION_OWNER_ATTR,
                None,
            )
            if not isinstance(admission_owner_nonce, str) or not admission_owner_nonce:
                # Only the authenticated HTTP admission path receives this
                # process-local capability after it acquires the crash-released
                # sidecar lock.  Startup recovery must never manufacture one
                # and adopt a delivery whose provider request was not yet
                # durably admitted.
                event.metadata[_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY] = True
                self._resolve_handoff_admission(event, False)
                raise RuntimeError(
                    "accepted webhook delivery has no admission owner"
                )
            try:
                owns_admission = await self._session_db_call(
                    "ensure_webhook_delivery_admission_lock",
                    state_key,
                    state["admission_token"],
                    state["lock_protocol"],
                    admission_owner_nonce,
                )
            except asyncio.CancelledError:
                event.metadata[_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY] = True
                self._resolve_handoff_admission(event, False)
                await self._release_handoff_admission_lock(
                    state_key,
                    state,
                    admission_owner_nonce,
                )
                raise
            except Exception:
                event.metadata[_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY] = True
                self._resolve_handoff_admission(event, False)
                await self._release_handoff_admission_lock(
                    state_key,
                    state,
                    admission_owner_nonce,
                )
                raise
            if owns_admission is not True:
                event.metadata[_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY] = True
                self._resolve_handoff_admission(event, False)
                await self._release_handoff_admission_lock(
                    state_key,
                    state,
                    admission_owner_nonce,
                )
                raise RuntimeError(
                    "webhook delivery admission ownership changed"
                )
            return marker

        bound_session_id = str(state.get("session_id") or "")
        if bound_session_id != session_id:
            compression_tip = await self._session_db_call(
                "get_compression_tip", bound_session_id
            )
            if compression_tip != session_id:
                event.metadata[_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY] = True
                raise RuntimeError(
                    "durable webhook delivery is bound to a different session"
                )
        if phase == "running":
            if state["active_turn_token"] == active_turn_token:
                return None
            resumed_state = self._handoff_delivery_state_value(
                marker,
                handoff_to,
                session_id=session_id,
                source_session_key=session_key,
                phase="running",
                active_turn_token=active_turn_token,
            )
            resumed = await self._session_db_call(
                "resume_webhook_handoff_delivery_on_source_route",
                session_id,
                session_key,
                state_key,
                raw_state,
                resumed_state,
                active_turn_token,
                bound_session_id,
            )
            if resumed is not True:
                event.metadata[_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY] = True
                raise RuntimeError(
                    "webhook running delivery is owned by another agent turn"
                )
            return None

        if phase == "succeeded" and await self._owns_webhook_handoff_request(
            session_id, handoff_to
        ):
            event.metadata[_EVENT_HANDOFF_REQUESTED_KEY] = True
            raise RuntimeError(
                "webhook delivery already completed before agent restart"
            )
        event.metadata[_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY] = True
        raise RuntimeError("durable webhook delivery has invalid run ownership")

    async def on_agent_input_persisted(
        self,
        event: "MessageEvent",
        *,
        session_key: str,
        session_id: str,
    ) -> None:
        """Bind accepted delivery ownership after its user row commits."""
        identity = await self._handoff_delivery_identity_for_event(event)
        if identity is None:
            raise RuntimeError("webhook handoff delivery state is missing")
        marker, state_key, raw_state, state = identity
        if state["phase"] != "accepted":
            raise RuntimeError(
                "webhook delivery input admission is no longer accepted"
            )
        if state["source_session_key"] != session_key:
            event.metadata[_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY] = True
            raise RuntimeError(
                "durable webhook delivery belongs to a different source route"
            )
        handoff_to = str(state["platform"])
        if handoff_to not in _SUPPORTED_HANDOFF_TARGETS:
            raise RuntimeError(f"unsupported handoff target '{handoff_to}'")
        active_turn_token = getattr(event, "_gateway_active_turn_token", None)
        admission_owner_nonce = getattr(
            event,
            _EVENT_HANDOFF_ADMISSION_OWNER_ATTR,
            None,
        )
        if not isinstance(active_turn_token, str) or not active_turn_token:
            event.metadata[_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY] = True
            raise RuntimeError("webhook agent active-turn ownership is missing")
        if not isinstance(admission_owner_nonce, str) or not admission_owner_nonce:
            event.metadata[_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY] = True
            self._resolve_handoff_admission(event, False)
            raise RuntimeError("accepted webhook delivery has no admission owner")

        try:
            input_committed = await self._session_db_call(
                "has_webhook_handoff_input",
                marker,
            )
        except asyncio.CancelledError:
            self._resolve_handoff_admission(event, False)
            await self._release_handoff_admission_lock(
                state_key,
                state,
                admission_owner_nonce,
            )
            raise
        except Exception:
            # A storage error cannot be distinguished from a committed row.
            # Fence cleanup away from the possibly-live route and let the
            # authenticated provider retry reconcile it later.
            event.metadata[_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY] = True
            self._resolve_handoff_admission(event, False)
            await self._release_handoff_admission_lock(
                state_key,
                state,
                admission_owner_nonce,
            )
            raise
        if input_committed is not True:
            self._resolve_handoff_admission(event, False)
            await self._release_handoff_admission_lock(
                state_key,
                state,
                admission_owner_nonce,
            )
            raise RuntimeError(
                "durable webhook input row is not committed"
            )

        running_state = self._handoff_delivery_state_value(
            marker,
            handoff_to,
            session_id=session_id,
            source_session_key=session_key,
            phase="running",
            active_turn_token=active_turn_token,
        )

        async def _rollback_failed_callback() -> bool:
            """Keep a callback that cannot return out of ``running``."""
            try:
                rolled_back = await self._session_db_call(
                    "rollback_webhook_handoff_delivery_input_admission",
                    session_id,
                    session_key,
                    state_key,
                    running_state,
                    raw_state,
                    active_turn_token,
                )
            except Exception:
                logger.error(
                    "[webhook] Could not roll back failed input admission",
                    exc_info=True,
                )
                return False
            if rolled_back is not True:
                return False
            self._resolve_handoff_admission(event, False)
            try:
                await self._release_handoff_admission_lock(
                    state_key,
                    state,
                    admission_owner_nonce,
                )
            except Exception:
                # The delivery is accepted again. A live sidecar owner may
                # temporarily yield 503; process close or a successful retry
                # releases it without risking a false duplicate.
                logger.debug(
                    "[webhook] Failed to release rolled-back admission fence",
                    exc_info=True,
                )
            return True

        try:
            bound = await self._session_db_call(
                "bind_webhook_handoff_delivery_to_source_route",
                session_id,
                session_key,
                state_key,
                raw_state,
                running_state,
                active_turn_token,
                admission_owner_nonce,
            )
        except asyncio.CancelledError:
            if not await _rollback_failed_callback():
                event.metadata[_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY] = True
                self._resolve_handoff_admission(event, False)
            raise
        except Exception:
            if not await _rollback_failed_callback():
                event.metadata[_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY] = True
                self._resolve_handoff_admission(event, False)
            raise
        if bound is not True:
            if not await _rollback_failed_callback():
                event.metadata[_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY] = True
                self._resolve_handoff_admission(event, False)
            raise RuntimeError("webhook delivery could not enter the running phase")
        # No cancellation point may sit between the running CAS and callback
        # return. Otherwise an offloaded unlock can complete, surface
        # cancellation, and leave a brief unlocked-running window before a
        # rollback; a provider retry in that window sees a terminal duplicate
        # even though the primary call never started. Queue the unlock and
        # return synchronously. While it is pending, retries receive a
        # transient admission-in-progress response; process death releases the
        # OS lock through SessionDB.close().
        async def _release_bound_admission() -> None:
            try:
                await self._release_handoff_admission_lock(
                    state_key,
                    state,
                    admission_owner_nonce,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug(
                    "[webhook] Failed to release a bound admission fence",
                    exc_info=True,
                )

        release_task = asyncio.create_task(_release_bound_admission())
        self._background_tasks.add(release_task)
        release_task.add_done_callback(self._background_tasks.discard)
        self._resolve_handoff_admission(event, True)

    async def _webhook_completion_is_durable(
        self,
        *,
        marker: str,
        state_key: str,
        handoff_to: str,
        session_key: str,
        session_id: str,
    ) -> bool:
        raw_state = await self._session_db_call("get_meta", state_key)
        state = self._parse_handoff_delivery_state(
            raw_state,
            marker=marker,
            handoff_to=handoff_to,
        )
        exact_success = (
            state.get("phase") == "succeeded"
            and state.get("session_id") == session_id
            and state.get("source_session_key") == session_key
        )
        if exact_success and await self._owns_webhook_handoff_request(
            session_id, handoff_to
        ):
            return True

        handoff_state = await self._session_db_call(
            "get_handoff_state", session_id
        )
        if (
            isinstance(handoff_state, dict)
            and handoff_state.get("state")
            in {"pending", "running", "completed", "failed"}
        ):
            raise _HandoffRequestOwnershipConflict(
                "session handoff belongs to an interactive request"
            )
        return False

    @staticmethod
    def _preserve_webhook_active_turn_for_restart(event: "MessageEvent") -> None:
        """Keep an uncertain running delivery eligible for crash recovery."""
        try:
            delattr(event, "_gateway_active_turn_token")
        except AttributeError:
            pass

    async def on_agent_run_persisted(
        self,
        event: "MessageEvent",
        *,
        session_key: str,
        session_id: str,
    ) -> None:
        """Publish successful source completion and watcher work atomically."""
        if event.metadata.get(_EVENT_HANDOFF_REQUESTED_KEY):
            return
        event.metadata[_EVENT_HANDOFF_COMPLETION_ATTEMPTED_KEY] = True

        identity = await self._handoff_delivery_identity_for_event(event)
        if identity is None:
            if event.metadata.get(_EVENT_HANDOFF_TARGET_KEY):
                raise RuntimeError("webhook handoff delivery state is missing")
            return
        marker, state_key, raw_state, state = identity
        handoff_to = str(state["platform"])
        if handoff_to not in _SUPPORTED_HANDOFF_TARGETS:
            raise RuntimeError(f"unsupported handoff target '{handoff_to}'")
        if state["source_session_key"] != session_key:
            raise RuntimeError(
                "durable webhook delivery belongs to a different source route"
            )

        if state["phase"] == "succeeded":
            try:
                if await self._webhook_completion_is_durable(
                    marker=marker,
                    state_key=state_key,
                    handoff_to=handoff_to,
                    session_key=session_key,
                    session_id=session_id,
                ):
                    event.metadata[_EVENT_HANDOFF_REQUESTED_KEY] = True
                    return
            except _HandoffRequestOwnershipConflict:
                event.metadata[_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY] = True
                raise
            raise RuntimeError("webhook success publication is incomplete")
        if state["phase"] != "running":
            raise RuntimeError(
                "webhook delivery did not enter the running phase"
            )

        running_session_id = str(state["session_id"])
        active_turn_token = getattr(event, "_gateway_active_turn_token", None)
        if not isinstance(active_turn_token, str) or not active_turn_token:
            raise RuntimeError("webhook agent active-turn ownership is missing")
        succeeded_state = self._handoff_delivery_state_value(
            marker,
            handoff_to,
            session_id=session_id,
            source_session_key=session_key,
            phase="succeeded",
        )

        try:
            completed = await self._session_db_call(
                "complete_webhook_handoff_delivery_once",
                session_id,
                session_key,
                state_key,
                raw_state,
                succeeded_state,
                handoff_to,
                active_turn_token,
                running_session_id,
            )
        except asyncio.CancelledError:
            try:
                completion_is_durable = (
                    await self._webhook_completion_is_durable(
                        marker=marker,
                        state_key=state_key,
                        handoff_to=handoff_to,
                        session_key=session_key,
                        session_id=session_id,
                    )
                )
                if completion_is_durable:
                    event.metadata[_EVENT_HANDOFF_REQUESTED_KEY] = True
                else:
                    event.metadata[
                        _EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY
                    ] = True
                    self._preserve_webhook_active_turn_for_restart(event)
            except _HandoffRequestOwnershipConflict:
                event.metadata[_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY] = True
            except Exception:
                event.metadata[_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY] = True
                self._preserve_webhook_active_turn_for_restart(event)
            raise
        except _HandoffRequestOwnershipConflict:
            event.metadata[_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY] = True
            raise
        except Exception:
            try:
                if await self._webhook_completion_is_durable(
                    marker=marker,
                    state_key=state_key,
                    handoff_to=handoff_to,
                    session_key=session_key,
                    session_id=session_id,
                ):
                    event.metadata[_EVENT_HANDOFF_REQUESTED_KEY] = True
                    return
            except _HandoffRequestOwnershipConflict:
                event.metadata[_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY] = True
                raise
            except Exception:
                pass
            # An off-loop SQLite call can commit before its exception/result is
            # observable by this task. Preserve the route on ambiguity.
            event.metadata[_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY] = True
            self._preserve_webhook_active_turn_for_restart(event)
            raise

        if completed is not True:
            try:
                if await self._webhook_completion_is_durable(
                    marker=marker,
                    state_key=state_key,
                    handoff_to=handoff_to,
                    session_key=session_key,
                    session_id=session_id,
                ):
                    event.metadata[_EVENT_HANDOFF_REQUESTED_KEY] = True
                    return
            except _HandoffRequestOwnershipConflict:
                event.metadata[_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY] = True
                raise
            raise RuntimeError("webhook success publication was rejected")

        event.metadata[_EVENT_HANDOFF_REQUESTED_KEY] = True
        logger.info(
            "[webhook] Requested durable handoff for session %s to %s",
            session_id,
            handoff_to,
        )

    @staticmethod
    async def _await_cancel_resistant(awaitable: Any) -> Any:
        """Let an admitted off-loop DB operation settle before cancellation."""
        task = asyncio.ensure_future(awaitable)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while True:
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    if task.done():
                        break
                    continue
                except Exception:
                    break
                else:
                    break
            raise

    async def _session_store_call(
        self, method_name: str, *args: Any, **kwargs: Any
    ) -> Any:
        """Call a SessionStore method through the runner's async facade."""
        runner = self.gateway_runner
        if runner is None:
            raise RuntimeError("gateway runner is unavailable")

        facade = getattr(runner, "async_session_store", None)
        if facade is not None:
            method = getattr(facade, method_name)
            result = method(*args, **kwargs)
            if inspect.isawaitable(result):
                return await self._await_cancel_resistant(result)
            return result

        store = getattr(runner, "session_store", None)
        if store is None:
            raise RuntimeError("session store is unavailable")
        method = getattr(store, method_name)
        return await self._await_cancel_resistant(
            asyncio.to_thread(method, *args, **kwargs)
        )

    async def _resolve_webhook_session(
        self, event: "MessageEvent"
    ) -> tuple[str, str]:
        """Resolve the exact routing key and persisted ID for this delivery."""
        runner = self.gateway_runner
        if runner is None:
            raise RuntimeError("gateway runner is unavailable")
        key_fn = getattr(runner, "_session_key_for_source", None)
        if not callable(key_fn):
            raise RuntimeError("gateway session-key resolver is unavailable")
        session_key = key_fn(event.source)
        session_id = await self._session_store_call("peek_session_id", session_key)
        if not session_id:
            raise RuntimeError(
                f"no persisted session found for webhook route {session_key}"
            )
        return session_key, str(session_id)

    async def _session_db_call(
        self, method_name: str, *args: Any, **kwargs: Any
    ) -> Any:
        runner = self.gateway_runner
        if runner is None:
            raise RuntimeError("gateway runner is unavailable")
        session_db = getattr(runner, "_session_db", None)
        if session_db is None:
            raise RuntimeError("session database is unavailable")
        method = getattr(session_db, method_name)
        result = method(*args, **kwargs)
        if inspect.isawaitable(result):
            return await self._await_cancel_resistant(result)
        return result

    async def _owns_webhook_handoff_request(
        self,
        session_id: str,
        handoff_to: str,
    ) -> bool:
        return bool(
            await self._session_db_call(
                "is_webhook_handoff_request",
                session_id,
                handoff_to,
            )
        )

    async def _recover_unrequested_handoff(
        self, session_id: str, handoff_to: str
    ) -> None:
        """Close the marker-before-request crash gap for a provider retry."""
        if not session_id:
            raise RuntimeError("durable duplicate has no session id")

        session_row = await self._session_db_call("get_session", session_id)
        if not isinstance(session_row, dict) or session_row.get("ended_at") is not None:
            # The original delivery was finalized or later pruned. Its durable
            # tombstone still suppresses provider retries, but there is no live
            # session that should be handed off again.
            return
        source_session_key = session_row.get("session_key")

        terminal_or_inflight = {"pending", "running", "completed", "failed"}
        state = await self._session_db_call("get_handoff_state", session_id)
        state_value = state.get("state") if isinstance(state, dict) else None
        if state_value in terminal_or_inflight:
            if await self._owns_webhook_handoff_request(
                session_id,
                handoff_to,
            ):
                return
            raise _HandoffRequestOwnershipConflict(
                "marked session handoff belongs to an interactive request"
            )
        if state_value is not None:
            raise RuntimeError(f"unknown handoff state '{state_value}'")
        if not isinstance(source_session_key, str) or not source_session_key:
            raise RuntimeError("marked session has no durable source route key")

        if await self._session_db_call(
            "request_handoff_once",
            session_id,
            handoff_to,
            source_session_key=source_session_key,
        ):
            logger.info(
                "[webhook] Recovered pending handoff for marked session %s",
                session_id,
            )
            return

        # Another gateway may have won the NULL -> pending CAS between our
        # read and request.  Re-read before failing so that race is idempotent.
        state = await self._session_db_call("get_handoff_state", session_id)
        state_value = state.get("state") if isinstance(state, dict) else None
        if not (
            isinstance(state, dict)
            and state_value in terminal_or_inflight
            and state.get("platform") == handoff_to
            and await self._owns_webhook_handoff_request(
                session_id,
                handoff_to,
            )
        ):
            raise RuntimeError("marked session has no durable handoff request")

    async def _request_webhook_handoff(
        self, event: "MessageEvent", handoff_to: str
    ) -> None:
        """Compatibility fallback for runners without the persisted hook."""
        if event.metadata.get(_EVENT_HANDOFF_REQUESTED_KEY) or event.metadata.get(
            _EVENT_HANDOFF_COMPLETION_ATTEMPTED_KEY
        ):
            return

        session_key: Optional[str] = None
        session_id: Optional[str] = None
        try:
            if handoff_to not in _SUPPORTED_HANDOFF_TARGETS:
                raise RuntimeError(f"unsupported handoff target '{handoff_to}'")
            session_key, session_id = await self._resolve_webhook_session(event)
            input_marker = await self.on_agent_run_started(
                event,
                session_key=session_key,
                session_id=session_id,
            )
            if input_marker:
                await self.on_agent_input_persisted(
                    event,
                    session_key=session_key,
                    session_id=session_id,
                )
            await self.on_agent_run_persisted(
                event,
                session_key=session_key,
                session_id=session_id,
            )
        except _HandoffRequestOwnershipConflict as exc:
            event.metadata[_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY] = True
            logger.error(
                "[webhook] Handoff ownership conflict for %s: %s",
                event.source.chat_id,
                exc,
            )
        except Exception as exc:
            logger.error(
                "[webhook] Failed to request handoff for %s: %s",
                event.source.chat_id,
                exc,
            )
            if event.metadata.get(_EVENT_HANDOFF_OWNERSHIP_CONFLICT_KEY):
                return
            await self._finalize_webhook_handoff(
                event,
                "webhook_handoff_request_failed",
                session_key=session_key,
                session_id=session_id,
            )

    async def _finalize_webhook_handoff(
        self,
        event: "MessageEvent",
        reason: str,
        *,
        session_key: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> None:
        """Atomically unlink and end a failed handoff's exact session."""
        try:
            if not session_key or not session_id:
                session_key, session_id = await self._resolve_webhook_session(event)

            finalized = await self._session_store_call(
                "remove_session_route_and_end",
                session_key,
                session_id,
                reason,
            )
            if not finalized:
                raise RuntimeError(
                    f"could not atomically finalize webhook session {session_id}"
                )
            logger.warning(
                "[webhook] Finalized handoff session %s (%s)", session_id, reason
            )
        except Exception as exc:
            logger.error(
                "[webhook] Failed to finalize handoff session for %s: %s",
                event.source.chat_id,
                exc,
            )

    async def _end_webhook_session(
        self, event: "MessageEvent", session_chat_id: str
    ) -> None:
        """Mark the per-delivery webhook session ended in state.db.

        Resolves the persisted ``session_id`` from the gateway session store
        using the SAME source the run was keyed on (so profile multiplexing
        and key construction match exactly), then closes it via the existing
        ``SessionDB.end_session`` API — never a hand-written UPDATE.
        """
        runner = self.gateway_runner
        if runner is None:
            return
        session_db = getattr(runner, "_session_db", None)
        store = getattr(runner, "session_store", None)
        if session_db is None or store is None:
            return
        try:
            key_fn = getattr(runner, "_session_key_for_source", None)
            if key_fn is None:
                return
            session_key = key_fn(event.source)
            # Resolve the persisted session_id via the store's public,
            # lock-held accessor (peek_session_id) rather than reaching into
            # the private _entries dict without the store lock. Fall back to
            # the private path only for older stores / test doubles that
            # predate the accessor.
            peek = getattr(store, "peek_session_id", None)
            if callable(peek):
                session_id = peek(session_key)
            else:
                if hasattr(store, "_ensure_loaded"):
                    try:
                        store._ensure_loaded()
                    except Exception:
                        pass
                entries = getattr(store, "_entries", {}) or {}
                entry = entries.get(session_key)
                session_id = getattr(entry, "session_id", None) if entry else None
            if not session_id:
                logger.debug(
                    "[webhook] No session_id to close for %s (key=%s)",
                    session_chat_id,
                    session_key,
                )
                return
            # AsyncSessionDB forwards end_session via asyncio.to_thread; a
            # plain SessionDB exposes it synchronously.  Handle both.
            _end = session_db.end_session
            result = _end(session_id, "webhook_complete")
            if inspect.isawaitable(result):
                await self._await_cancel_resistant(result)
            logger.debug(
                "[webhook] Closed session %s for delivery %s",
                session_id,
                session_chat_id,
            )
        except Exception as e:
            logger.debug(
                "[webhook] Failed to close session for %s: %s",
                session_chat_id,
                e,
            )

    # ------------------------------------------------------------------
    # Signature validation
    # ------------------------------------------------------------------

    def _validate_signature(
        self, request: "web.Request", body: bytes, secret: str
    ) -> bool:
        """Validate webhook signature (GitHub, GitLab, Svix, Linear, generic HMAC-SHA256)."""
        def _header(name: str) -> str:
            return (
                request.headers.get(name, "")
                or request.headers.get(name.lower(), "")
                or request.headers.get(name.upper(), "")
            )

        # Svix / AgentMail:
        #   svix-id: msg_...
        #   svix-timestamp: unix seconds
        #   svix-signature: v1,<base64-hmac> [v1,<base64-hmac> ...]
        # Signed content is: "{id}.{timestamp}.{raw_body}".  Svix secrets
        # usually start with "whsec_" and the remainder is base64-encoded.
        svix_id = _header("svix-id")
        svix_timestamp = _header("svix-timestamp")
        svix_signature = _header("svix-signature")
        if svix_id or svix_timestamp or svix_signature:
            return self._validate_svix_signature(
                body=body,
                secret=secret,
                msg_id=svix_id,
                timestamp=svix_timestamp,
                signature_header=svix_signature,
            )

        # Linear: linear-signature = <hex HMAC-SHA256 of the raw body, keyed
        # by the webhook signing key>. Linear's documented scheme signs the
        # body only (no timestamp binding), so this mirrors it exactly;
        # without this branch every Linear delivery to a secret-configured
        # route was rejected as unrecognized (#87348).
        linear_sig = _header("linear-signature")
        if linear_sig:
            expected_linear = hmac.new(
                secret.encode(), body, hashlib.sha256
            ).hexdigest()
            return _hmac_str_equal(linear_sig, expected_linear)

        # GitHub: X-Hub-Signature-256 = sha256=<hex>
        gh_sig = request.headers.get("X-Hub-Signature-256", "")
        if gh_sig:
            expected = "sha256=" + hmac.new(
                secret.encode(), body, hashlib.sha256
            ).hexdigest()
            return _hmac_str_equal(gh_sig, expected)

        # GitLab: X-Gitlab-Token = <plain secret>
        gl_token = request.headers.get("X-Gitlab-Token", "")
        if gl_token:
            return _hmac_str_equal(gl_token, secret)

        # Generic V2: X-Webhook-Signature-V2 = <hex HMAC-SHA256 of "<timestamp>.<body>">
        #             X-Webhook-Timestamp = <unix seconds> (required for V2)
        # Checked independently of (and before) legacy V1 below — a sender
        # that only ever sends V2 headers must still validate here; nesting
        # this inside `if generic_sig:` would silently skip V2-only senders.
        #
        # The presence of X-Webhook-Signature-V2 alone selects V2 mode and
        # commits to it — it must NOT fall through to the V1 branch just
        # because the timestamp is missing/malformed/expired. A sender
        # migrating to V2 typically sends both V1 and V2 headers together
        # for compatibility; if incomplete V2 fell through to V1, an
        # attacker who captured one such mixed request could strip the
        # X-Webhook-Timestamp header from a replay and have it validate
        # against the still-present, still-unprotected V1 signature instead
        # — silently downgrading a V2-protected request back to the replay
        # hole V2 exists to close.
        v2_sig = request.headers.get("X-Webhook-Signature-V2", "")
        if v2_sig:
            v2_timestamp = request.headers.get("X-Webhook-Timestamp", "")
            if not v2_timestamp:
                logger.warning(
                    "[webhook] Route '%s' sent X-Webhook-Signature-V2 with "
                    "no X-Webhook-Timestamp — rejecting rather than "
                    "falling back to legacy V1",
                    request.match_info.get("route_name", ""),
                )
                return False
            try:
                ts = int(v2_timestamp)
            except (TypeError, ValueError):
                return False
            if abs(int(time.time()) - ts) > 300:
                logger.warning(
                    "[webhook] Route '%s' generic HMAC V2 timestamp outside replay window",
                    request.match_info.get("route_name", ""),
                )
                return False
            signed_content = v2_timestamp.encode() + b"." + body
            expected_v2 = hmac.new(
                secret.encode(), signed_content, hashlib.sha256
            ).hexdigest()
            return _hmac_str_equal(v2_sig, expected_v2)

        # Generic V1 (legacy): X-Webhook-Signature = <hex HMAC-SHA256 of body>
        # (deprecated — no replay protection, since the signature only
        # covers the body: a captured (body, signature) pair replays
        # indefinitely with no timestamp binding it to a specific delivery.)
        # Only reachable when X-Webhook-Signature-V2 was not sent at all —
        # see the guard above.
        generic_sig = request.headers.get("X-Webhook-Signature", "")
        if generic_sig:
            expected = hmac.new(
                secret.encode(), body, hashlib.sha256
            ).hexdigest()
            route_name = request.match_info.get("route_name", "")
            if route_name not in self._v1_signature_warned:
                self._v1_signature_warned.add(route_name)
                logger.warning(
                    "[webhook] Route '%s' uses legacy body-only HMAC (no "
                    "timestamp), which is vulnerable to replay attacks. Add "
                    "an 'X-Webhook-Timestamp' header and switch to "
                    "'X-Webhook-Signature-V2' (HMAC-SHA256 of "
                    "'<timestamp>.<body>').",
                    route_name,
                )
            return _hmac_str_equal(generic_sig, expected)

        # No recognised signature header but secret is configured → reject
        logger.debug(
            "[webhook] Secret configured but no signature header found"
        )
        return False

    def _validate_svix_signature(
        self,
        body: bytes,
        secret: str,
        msg_id: str,
        timestamp: str,
        signature_header: str,
        tolerance_seconds: int = 300,
    ) -> bool:
        """Validate Svix-compatible signatures used by AgentMail webhooks."""
        if not (msg_id and timestamp and signature_header and secret):
            return False

        try:
            ts = int(timestamp)
        except (TypeError, ValueError):
            return False
        if abs(int(time.time()) - ts) > tolerance_seconds:
            logger.warning("[webhook] Svix signature timestamp outside replay window")
            return False

        if secret.startswith("whsec_"):
            encoded_secret = secret.removeprefix("whsec_")
            try:
                key = base64.b64decode(encoded_secret, validate=True)
            except (binascii.Error, ValueError):
                logger.debug("[webhook] Invalid whsec_ Svix signing secret")
                return False
        else:
            # Be permissive for providers that document Svix-style headers but
            # hand out raw shared secrets rather than whsec_ base64 secrets.
            logger.debug("[webhook] Validating Svix-style signature with raw secret")
            key = secret.encode()

        signed_content = msg_id.encode() + b"." + timestamp.encode() + b"." + body
        expected = base64.b64encode(
            hmac.new(key, signed_content, hashlib.sha256).digest()
        ).decode()

        # Svix can send multiple signatures separated by spaces during secret
        # rotation. Each entry is formatted as "vN,<base64>".
        for part in signature_header.split():
            try:
                version, signature = part.split(",", 1)
            except ValueError:
                continue
            if version == "v1" and _hmac_str_equal(signature, expected):
                return True
        return False

    # ------------------------------------------------------------------
    # Prompt rendering
    # ------------------------------------------------------------------

    def _render_prompt(
        self,
        template: str,
        payload: dict,
        event_type: str,
        route_name: str,
    ) -> str:
        """Render a prompt template with the webhook payload.

        Supports dot-notation access into nested dicts:
        ``{pull_request.title}`` → ``payload["pull_request"]["title"]``

        Special token ``{__raw__}`` dumps the entire payload as indented
        JSON (truncated to 4000 chars).  Useful for monitoring alerts or
        any webhook where the agent needs to see the full payload.
        """
        if not template:
            truncated = json.dumps(payload, indent=2)[:4000]
            return (
                f"Webhook event '{event_type}' on route "
                f"'{route_name}':\n\n```json\n{truncated}\n```"
            )

        def _resolve(match: re.Match) -> str:
            key = match.group(1)
            # Special token: dump the entire payload as JSON
            if key == "__raw__":
                return json.dumps(payload, indent=2)[:4000]
            if key == "event_type":
                return event_type
            value: Any = payload
            for part in key.split("."):
                if isinstance(value, dict):
                    value = value.get(part, f"{{{key}}}")
                else:
                    return f"{{{key}}}"
            if isinstance(value, (dict, list)):
                return json.dumps(value, indent=2)[:2000]
            return str(value)

        return re.sub(r"\{([a-zA-Z0-9_.]+)\}", _resolve, template)

    def _render_delivery_extra(
        self, extra: dict, payload: dict
    ) -> dict:
        """Render delivery_extra template values with payload data."""
        rendered: Dict[str, Any] = {}
        for key, value in extra.items():
            if isinstance(value, str):
                rendered[key] = self._render_prompt(value, payload, "", "")
            else:
                rendered[key] = value
        return rendered

    # ------------------------------------------------------------------
    # Response delivery
    # ------------------------------------------------------------------

    async def _direct_deliver(
        self, content: str, delivery: dict
    ) -> SendResult:
        """Deliver *content* directly without invoking the agent.

        Used by ``deliver_only`` routes: the rendered template becomes the
        literal message body, and we dispatch to the same delivery helpers
        that the agent-mode ``send()`` flow uses.  All target types that
        work in agent mode work here — Telegram, Discord, Slack, GitHub
        PR comments, etc.
        """
        deliver_type = delivery.get("deliver", "log")

        if deliver_type == "log":
            # Shouldn't reach here — startup validation rejects deliver_only
            # with deliver=log — but guard defensively.
            logger.info("[webhook] direct-deliver log-only: %s", content[:200])
            return SendResult(success=True)

        if deliver_type == "github_comment":
            return await self._deliver_github_comment(content, delivery)

        # Fall through to the cross-platform dispatcher, which validates the
        # target name and routes via the gateway runner.
        return await self._deliver_cross_platform(
            deliver_type, content, delivery
        )

    async def _deliver_github_comment(
        self, content: str, delivery: dict
    ) -> SendResult:
        """Post agent response as a GitHub PR/issue comment via ``gh`` CLI."""
        extra = delivery.get("deliver_extra", {})
        repo = extra.get("repo", "")
        pr_number = extra.get("pr_number", "")

        if not repo or not pr_number:
            logger.error(
                "[webhook] github_comment delivery missing repo or pr_number"
            )
            return SendResult(
                success=False, error="Missing repo or pr_number"
            )

        # --- Input validation (prevent CLI argument injection) ---
        # pr_number must be a positive integer.
        try:
            pr_int = int(pr_number)
            if pr_int <= 0:
                raise ValueError("non-positive")
        except (ValueError, TypeError):
            logger.error(
                "[webhook] invalid pr_number: %r", pr_number
            )
            return SendResult(
                success=False, error="Invalid pr_number"
            )

        # repo must match owner/name (alphanumeric, hyphens, underscores, dots).
        if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo):
            logger.error("[webhook] invalid repo format: %r", repo)
            return SendResult(
                success=False, error="Invalid repo format"
            )

        try:
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "comment",
                    str(pr_int),
                    "--repo",
                    repo,
                    "--body",
                    content,
                ],
                capture_output=True,
                text=True, encoding='utf-8', errors='replace',
                timeout=30,
            )
            if result.returncode == 0:
                logger.info(
                    "[webhook] Posted comment on %s#%s", repo, pr_number
                )
                return SendResult(success=True)
            else:
                logger.error(
                    "[webhook] gh pr comment failed: %s", result.stderr
                )
                return SendResult(success=False, error=result.stderr)
        except FileNotFoundError:
            logger.error(
                "[webhook] 'gh' CLI not found — install GitHub CLI for "
                "github_comment delivery"
            )
            return SendResult(
                success=False, error="gh CLI not installed"
            )
        except Exception as e:
            logger.error("[webhook] github_comment delivery error: %s", e)
            return SendResult(success=False, error=str(e))

    async def _deliver_cross_platform(
        self, platform_name: str, content: str, delivery: dict
    ) -> SendResult:
        """Route response to another platform (telegram, discord, etc.)."""
        if not self.gateway_runner:
            return SendResult(
                success=False,
                error="No gateway runner for cross-platform delivery",
            )

        try:
            target_platform = Platform(platform_name)
        except ValueError:
            return SendResult(
                success=False, error=f"Unknown platform: {platform_name}"
            )

        # Default adapters first; multiplex may park Slack/etc. only on a
        # secondary profile (self._profile_adapters). Fall back so webhook
        # deliver:slack still works when default has slack disabled.
        adapter = self.gateway_runner.adapters.get(target_platform)
        if not adapter:
            for _prof, amap in (getattr(self.gateway_runner, "_profile_adapters", None) or {}).items():
                if not isinstance(amap, dict):
                    continue
                cand = amap.get(target_platform)
                if cand is not None:
                    adapter = cand
                    break
        if not adapter:
            return SendResult(
                success=False,
                error=f"Platform {platform_name} not connected",
            )

        # Use home channel if no specific chat_id in deliver_extra
        extra = delivery.get("deliver_extra", {})
        chat_id = extra.get("chat_id", "")
        if not chat_id:
            home = self.gateway_runner.config.get_home_channel(target_platform)
            if home:
                chat_id = home.chat_id
            else:
                return SendResult(
                    success=False,
                    error=f"No chat_id or home channel for {platform_name}",
                )

        # Pass thread_id from deliver_extra so Telegram forum topics work
        metadata = None
        thread_id = extra.get("message_thread_id") or extra.get("thread_id")
        if thread_id:
            metadata = {"thread_id": thread_id}

        return await adapter.send(chat_id, content, metadata=metadata)
