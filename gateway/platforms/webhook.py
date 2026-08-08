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
  - Idempotency cache prevents duplicate agent runs on webhook retries
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
import json
import logging
import re
import subprocess
import sys
import time
from collections import deque
from typing import Any, Deque, Dict, Optional

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
from tools.github_pr_evidence import (
    EvidenceScope,
    execution_evidence_complete_for,
    evidence_scope,
    review_evidence_complete_for,
)

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
        self._successful_github_reviews: set[str] = set()

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
        self._reconciliation_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        # Load agent-created subscriptions before validating
        self._reload_dynamic_routes()

        # Validate routes at startup — secret is required per route
        for name, route in self._routes.items():
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
        self._start_reconciliation_tasks()

        route_names = ", ".join(self._routes.keys()) or "(none configured)"
        logger.info(
            "[webhook] Listening on %s:%d — routes: %s",
            self._host or "* (all interfaces, IPv4+IPv6)",
            self._port,
            route_names,
        )
        return True

    async def disconnect(self) -> None:
        tasks = list(self._reconciliation_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reconciliation_tasks.clear()
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._mark_disconnected()
        logger.info("[webhook] Disconnected")

    def _start_reconciliation_tasks(self) -> None:
        """Start one immediate-and-periodic recovery loop per opted-in route."""
        if self._reconciliation_tasks:
            return
        for route_name, route_config in self._static_routes.items():
            if route_config.get("reconcile") is not True:
                continue
            raw_interval = route_config.get("reconcile_interval_seconds")
            if not isinstance(raw_interval, (int, float, str)):
                continue
            try:
                interval = float(raw_interval)
            except (TypeError, ValueError):
                continue
            if interval < 30 or interval > 86_400:
                continue
            if not isinstance(route_config.get("script"), str):
                continue
            task = asyncio.create_task(
                self._reconciliation_loop(route_name, route_config, interval)
            )
            self._reconciliation_tasks.add(task)
            task.add_done_callback(self._reconciliation_tasks.discard)

    async def _reconciliation_loop(
        self, route_name: str, route_config: dict, interval: float
    ) -> None:
        while True:
            try:
                await self._run_reconciliation_once(route_name, route_config)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "[webhook] Recovery scan failed for route '%s'", route_name
                )
            await asyncio.sleep(interval)

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

        delivery = self._delivery_info.get(chat_id)
        if delivery is None:
            if chat_id.startswith("webhook:"):
                logger.error(
                    "[webhook] Missing delivery authority for active session %s", chat_id
                )
                return SendResult(
                    success=False,
                    error="Webhook delivery authority is missing or expired",
                )
            delivery = {}
        deliver_type = delivery.get("deliver", "log")

        if deliver_type == "log":
            logger.info("[webhook] Response for %s: %s", chat_id, content[:200])
            return SendResult(success=True)

        if deliver_type == "github_comment":
            return await self._deliver_github_comment(content, delivery)

        if deliver_type == "github_review":
            result = await self._deliver_github_review(content, delivery)
            if result.success:
                self._successful_github_reviews.add(chat_id)
                delivery.pop("_github_review_failure_code", None)
            else:
                delivery["_github_review_failure_code"] = {
                    "GitHub PR review evidence is incomplete or out of scope": (
                        "review_evidence_incomplete"
                    ),
                    "GitHub PR execution evidence is incomplete or out of scope": (
                        "execution_evidence_incomplete"
                    ),
                    "PR state changed before publish": "live_tuple_changed",
                }.get(result.error, "publication_failed")
            return result

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
        # Route processing may legally take up to four hours. Preserve the
        # authority envelope through that bound plus settlement headroom.
        cutoff = now - max(self._idempotency_ttl, 5 * 60 * 60)
        while self._delivery_info_order and self._delivery_info_order[0][0] < cutoff:
            created_at, key = self._delivery_info_order.popleft()
            if self._delivery_info_created.get(key) != created_at:
                continue
            self._delivery_info.pop(key, None)
            self._delivery_info_created.pop(key, None)
            self._successful_github_reviews.discard(key)

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
          - ``None`` when no profile prefix is present, or multiplexing is off
            (the prefix is ignored, request handled as the default profile).
          - the profile name (str) when present, multiplexing is on, and the
            profile is one this gateway serves.
          - ``_PROFILE_REJECTED`` when a prefix is present but the profile is
            unknown/unconfigured (handler returns 404).
        """
        profile = (request.match_info.get("profile") or "").strip()
        if not profile:
            return None
        runner = self.gateway_runner
        cfg = getattr(runner, "config", None)
        if not getattr(cfg, "multiplex_profiles", False):
            # Prefix supplied but multiplexing is off — ignore it, behave as
            # the single-profile gateway (don't 404 a would-be valid route).
            return None
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
                **(
                    {"trusted_github_pr_environment": True}
                    if route_config.get("evidence") == "github_pr"
                    else {}
                ),
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

        payload, settlement_lease_token = self._extract_settlement_lease_token(
            route_name, payload
        )

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
        skill_loaded = not skills
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
                            skill_loaded = True
                            break  # Load the first matching skill
                    else:
                        logger.warning(
                            "[webhook] Skill '%s' not found", skill_name
                        )
            except Exception as e:
                logger.warning("[webhook] Skill loading failed: %s", e)
        if route_config.get("evidence") and not skill_loaded:
            await self._release_review_reservation(
                route_name, payload, settlement_lease_token
            )
            assert web is not None
            return web.json_response(
                {"status": "unavailable", "error": "Required skill unavailable"},
                status=503,
            )

        # Build a unique delivery ID
        delivery_id = request.headers.get(
            "X-GitHub-Delivery",
            request.headers.get(
                "svix-id",
                request.headers.get("X-Request-ID", str(int(time.time() * 1000))),
            ),
        )

        # ── Idempotency ─────────────────────────────────────────
        # Skip duplicate deliveries (webhook retries).
        now = time.time()
        if not self._record_delivery_id(delivery_id, now):
            logger.info(
                "[webhook] Skipping duplicate delivery %s", delivery_id
            )
            await self._release_review_reservation(
                route_name, payload, settlement_lease_token
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

        evidence = self._evidence_scope_for_route(route_name, payload)
        if route_config.get("evidence") and (
            evidence is None or settlement_lease_token is None
        ):
            logger.error(
                "[webhook] Refusing route '%s' without a valid evidence scope",
                route_name,
            )
            try:
                await self._release_review_reservation(
                    route_name, payload, settlement_lease_token
                )
            except Exception:
                logger.exception(
                    "[webhook] Failed to release reservation for invalid evidence scope"
                )
            self._seen_deliveries.pop(delivery_id, None)
            assert web is not None
            return web.json_response(
                {"status": "unavailable", "error": "Evidence scope unavailable"},
                status=503,
            )

        # Use delivery_id in session key so concurrent webhooks on the
        # same route get independent agent runs (not queued/interrupted).
        session_chat_id = f"webhook:{route_name}:{delivery_id}"

        # Store delivery and settlement authority for send()/completion. Read by every send() invocation
        # for this chat_id (interim status messages and the final response),
        # so we do NOT pop on send.  TTL-based cleanup keeps the dict bounded.
        deliver_config = {
            "deliver": route_config.get("deliver", "log"),
            "deliver_extra": self._render_delivery_extra(
                route_config.get("deliver_extra", {}), payload
            ),
        }
        if evidence is not None:
            deliver_config["_trusted_evidence_route"] = route_name
            deliver_config["_evidence_tuple"] = evidence.tuple_dict
            deliver_config["_settlement_lease_token"] = settlement_lease_token
        self._delivery_info[session_chat_id] = deliver_config
        self._delivery_info_created[session_chat_id] = now
        self._delivery_info_order.append((now, session_chat_id))
        self._prune_delivery_info(now)

        # Build source and event
        source = self.build_source(
            chat_id=session_chat_id,
            chat_name=f"webhook/{route_name}",
            chat_type="webhook",
            user_id=f"webhook:{route_name}",
            user_name=route_name,
        )
        if profile and isinstance(profile, str):
            source.profile = profile
        event = MessageEvent(
            text=prompt,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=payload,
            message_id=delivery_id,
        )

        if evidence is not None and not await self._mark_github_review_started(
            route_name,
            route_config,
            evidence.tuple_dict,
            settlement_lease_token,
            payload.get("pr_url"),
        ):
            await self._release_review_reservation(
                route_name, payload, settlement_lease_token
            )
            self._delivery_info.pop(session_chat_id, None)
            self._delivery_info_created.pop(session_chat_id, None)
            self._seen_deliveries.pop(delivery_id, None)
            assert web is not None
            return web.json_response(
                {"status": "unavailable", "error": "Review start unavailable"},
                status=503,
            )

        logger.info(
            "[webhook] %s event=%s route=%s prompt_len=%d delivery=%s",
            request.method,
            event_type,
            route_name,
            len(prompt),
            delivery_id,
        )

        # Non-blocking — return 202 Accepted immediately.  The per-delivery
        # session is closed by the ``on_processing_complete`` override below
        # once the agent run actually finishes (``handle_message`` itself is
        # fire-and-forget: it spawns ``_process_message_background`` and
        # returns before the run starts, so nothing can be closed here).
        if evidence is not None:
            # create_task copies the current ContextVar context. handle_message
            # then creates the long-running processing task inside that copied
            # context, while this request task resets immediately afterward.
            with evidence_scope(evidence):
                task = asyncio.create_task(self.handle_message(event))
        else:
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

    async def _run_reconciliation_once(
        self, route_name: str, route_config: dict
    ) -> int:
        """Ask one trusted static route script for bounded recovery events."""
        if self._static_routes.get(route_name) is not route_config:
            return 0
        script = route_config.get("script")
        if not isinstance(script, str) or not script:
            return 0
        ok, result = await asyncio.to_thread(
            self._route_processor.run_route_script,
            script,
            {"operation": "reconcile"},
            **(
                {"trusted_github_pr_environment": True}
                if route_config.get("evidence") == "github_pr"
                else {}
            ),
        )
        if not ok or not isinstance(result, dict):
            return 0
        events = result.get("events")
        if not isinstance(events, list) or len(events) > 100:
            return 0

        allowed_events = route_config.get("events", [])
        dispatched = 0
        for recovered in events:
            if not isinstance(recovered, dict):
                continue
            delivery_id = recovered.get("delivery_id")
            event_type = recovered.get("event_type")
            payload = recovered.get("payload")
            if (
                not isinstance(delivery_id, str)
                or not delivery_id
                or len(delivery_id) > 128
                or not isinstance(event_type, str)
                or event_type not in allowed_events
                or not isinstance(payload, dict)
            ):
                continue
            if await self._dispatch_recovered_event(
                route_name, route_config, recovered
            ):
                dispatched += 1
        return dispatched

    def _extract_settlement_lease_token(
        self, route_name: str, payload: Any
    ) -> tuple[Any, Optional[str]]:
        """Remove the opaque lease capability before model-visible dispatch."""
        static_route = self._static_routes.get(route_name)
        if (
            not isinstance(static_route, dict)
            or static_route.get("evidence") != "github_pr"
            or not isinstance(payload, dict)
        ):
            return payload, None
        sanitized = dict(payload)
        token = sanitized.pop("lease_token", None)
        if not isinstance(token, str) or re.fullmatch(
            r"[A-Za-z0-9_-]{32,128}", token
        ) is None:
            return sanitized, None
        return sanitized, token

    async def _release_review_reservation(
        self, route_name: str, payload: Any, lease_token: Optional[str]
    ) -> bool:
        """Release only through the same statically trusted route script."""
        static_route = self._static_routes.get(route_name)
        if (
            not isinstance(static_route, dict)
            or static_route.get("evidence") != "github_pr"
            or not isinstance(static_route.get("script"), str)
            or not isinstance(payload, dict)
            or lease_token is None
        ):
            return False
        settlement = {
            "operation": "release",
            "contract_version": payload.get("contract_version"),
            "repository": payload.get("repository"),
            "pr_number": str(payload.get("pr_number", "")),
            "base_sha": payload.get("expected_base_sha"),
            "head_sha": payload.get("expected_head_sha"),
            "lease_token": lease_token,
        }
        keep, _ = await asyncio.to_thread(
            self._route_processor.run_route_script,
            static_route["script"],
            settlement,
            trusted_github_pr_environment=True,
        )
        return keep

    async def _mark_github_review_started(
        self,
        route_name: str,
        route_config: dict,
        review_tuple: dict,
        lease_token: Optional[str],
        pr_url: Any,
    ) -> bool:
        """Record the active-review reply before the model run is dispatched."""
        if not route_config.get("buzz_thread_lifecycle"):
            return True
        if (
            self._static_routes.get(route_name) is not route_config
            or route_config.get("evidence") != "github_pr"
            or not isinstance(route_config.get("script"), str)
            or not isinstance(review_tuple, dict)
            or not isinstance(lease_token, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", lease_token) is None
            or not isinstance(pr_url, str)
        ):
            return False
        control = {
            "operation": "started",
            "contract_version": review_tuple.get("contract_version"),
            "repository": review_tuple.get("repository"),
            "pr_number": str(review_tuple.get("pr_number", "")),
            "base_sha": review_tuple.get("base_sha"),
            "head_sha": review_tuple.get("head_sha"),
            "lease_token": lease_token,
            "pr_url": pr_url,
        }
        try:
            keep, result = await asyncio.to_thread(
                self._route_processor.run_route_script,
                route_config["script"],
                control,
                trusted_github_pr_environment=True,
            )
        except Exception:
            logger.exception("[webhook] GitHub review start recording failed")
            return False
        return bool(
            keep
            and isinstance(result, dict)
            and result.get("settled") == "started"
        )

    async def _claim_review_publication(self, delivery: dict) -> bool:
        """Atomically fence the exact lease token before GitHub publication."""
        route_name = delivery.get("_trusted_evidence_route")
        lease_token = delivery.get("_settlement_lease_token")
        review_tuple = delivery.get("_evidence_tuple")
        static_route = (
            self._static_routes.get(route_name)
            if isinstance(route_name, str)
            else None
        )
        extra = delivery.get("deliver_extra")
        if (
            not isinstance(route_name, str)
            or not isinstance(static_route, dict)
            or static_route.get("evidence") != "github_pr"
            or not isinstance(static_route.get("script"), str)
            or not isinstance(lease_token, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", lease_token) is None
            or not isinstance(review_tuple, dict)
            or not isinstance(extra, dict)
            or str(extra.get("repo")) != str(review_tuple.get("repository"))
            or str(extra.get("pr_number")) != str(review_tuple.get("pr_number"))
            or str(extra.get("base_sha")) != str(review_tuple.get("base_sha"))
            or str(extra.get("head_sha")) != str(review_tuple.get("head_sha"))
        ):
            return False
        settlement = {
            "operation": "claim_publish",
            "contract_version": review_tuple.get("contract_version"),
            "repository": review_tuple.get("repository"),
            "pr_number": str(review_tuple.get("pr_number", "")),
            "base_sha": review_tuple.get("base_sha"),
            "head_sha": review_tuple.get("head_sha"),
            "lease_token": lease_token,
        }
        try:
            keep, result = await asyncio.to_thread(
                self._route_processor.run_route_script,
                static_route["script"],
                settlement,
                trusted_github_pr_environment=True,
            )
        except Exception:
            logger.exception("[webhook] GitHub review publication claim failed")
            return False
        return bool(
            keep
            and isinstance(result, dict)
            and result.get("settled") == "claim_publish"
        )

    async def _dispatch_recovered_event(
        self, route_name: str, route_config: dict, recovered: dict
    ) -> bool:
        """Dispatch a validated recovery event through the trusted route gate."""
        if self._static_routes.get(route_name) is not route_config:
            return False
        if route_config.get("enabled", True) is False or route_config.get(
            "deliver_only"
        ):
            return False
        payload = recovered["payload"]
        event_type = recovered["event_type"]
        delivery_id = recovered["delivery_id"]
        if not self._route_processor.route_filters_match(
            route_config, payload, event_type, {}
        ):
            return False
        keep, transformed = await asyncio.to_thread(
            self._route_processor.run_route_script,
            route_config.get("script"),
            payload,
            **(
                {"trusted_github_pr_environment": True}
                if route_config.get("evidence") == "github_pr"
                else {}
            ),
        )
        if not keep:
            return False
        payload = transformed or payload
        if not isinstance(payload, dict):
            return False

        payload, settlement_lease_token = self._extract_settlement_lease_token(
            route_name, payload
        )

        prompt = self._render_prompt(
            route_config.get("prompt", ""), payload, event_type, route_name
        )
        skills = route_config.get("skills", [])
        skill_loaded = not skills
        if skills:
            try:
                from agent.skill_commands import (
                    build_skill_invocation_message,
                    get_skill_commands,
                )

                skill_cmds = get_skill_commands()
                for skill_name in skills:
                    cmd_key = f"/{skill_name}"
                    if cmd_key not in skill_cmds:
                        continue
                    skill_content = build_skill_invocation_message(
                        cmd_key, user_instruction=prompt
                    )
                    if skill_content:
                        prompt = skill_content
                        skill_loaded = True
                        break
            except Exception:
                logger.exception(
                    "[webhook] Recovery skill loading failed for route '%s'",
                    route_name,
                )
                await self._release_review_reservation(
                    route_name, payload, settlement_lease_token
                )
                return False
        if route_config.get("evidence") and not skill_loaded:
            logger.error(
                "[webhook] Required recovery skill unavailable for route '%s'",
                route_name,
            )
            await self._release_review_reservation(
                route_name, payload, settlement_lease_token
            )
            return False

        now = time.time()
        evidence = self._evidence_scope_for_route(route_name, payload)
        if route_config.get("evidence") and (
            evidence is None or settlement_lease_token is None
        ):
            logger.error(
                "[webhook] Recovery evidence scope invalid for route '%s'",
                route_name,
            )
            await self._release_review_reservation(
                route_name, payload, settlement_lease_token
            )
            return False
        if not self._record_delivery_id(delivery_id, now):
            await self._release_review_reservation(
                route_name, payload, settlement_lease_token
            )
            return False

        if evidence is not None and not await self._mark_github_review_started(
            route_name,
            route_config,
            evidence.tuple_dict,
            settlement_lease_token,
            payload.get("pr_url"),
        ):
            await self._release_review_reservation(
                route_name, payload, settlement_lease_token
            )
            self._seen_deliveries.pop(delivery_id, None)
            return False

        # Keep the lease capability outside the model-visible recovered payload.
        session_chat_id = f"webhook:{route_name}:{delivery_id}"
        deliver_config = {
            "deliver": route_config.get("deliver", "log"),
            "deliver_extra": self._render_delivery_extra(
                route_config.get("deliver_extra", {}), payload
            ),
        }
        if evidence is not None:
            deliver_config["_trusted_evidence_route"] = route_name
            deliver_config["_evidence_tuple"] = evidence.tuple_dict
            deliver_config["_settlement_lease_token"] = settlement_lease_token
        self._delivery_info[session_chat_id] = deliver_config
        self._delivery_info_created[session_chat_id] = now
        self._delivery_info_order.append((now, session_chat_id))
        self._prune_delivery_info(now)

        source = self.build_source(
            chat_id=session_chat_id,
            chat_name=f"webhook/{route_name}",
            chat_type="webhook",
            user_id=f"webhook:{route_name}",
            user_name=route_name,
        )
        event = MessageEvent(
            text=prompt,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=payload,
            message_id=delivery_id,
        )
        if evidence is not None:
            with evidence_scope(evidence):
                task = asyncio.create_task(self.handle_message(event))
        else:
            task = asyncio.create_task(self.handle_message(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return True

    def _evidence_scope_for_route(
        self, route_name: str, payload: Any
    ) -> Optional[EvidenceScope]:
        """Build immutable evidence authority for an opted-in static route.

        Dynamic subscriptions are agent/user-created mutable state and must
        never grant this credential-bearing read interface. Only the startup
        static route map is consulted for the opt-in bit.
        """
        static_route = self._static_routes.get(route_name)
        if not isinstance(static_route, dict):
            return None
        if static_route.get("evidence") != "github_pr":
            return None
        if not isinstance(static_route.get("script"), str):
            return None
        delivery_extra = static_route.get("deliver_extra")
        if (
            not isinstance(delivery_extra, dict)
            or delivery_extra.get("contract_version") != "v2"
        ):
            return None
        if not isinstance(payload, dict):
            return None
        public_key_value = static_route.get("execution_attestation_public_key")
        baseline_gates = static_route.get("baseline_execution_gates")
        policy_version = static_route.get("execution_gate_policy_version")
        policy_sha256 = static_route.get("execution_gate_policy_sha256")
        if (
            not isinstance(public_key_value, str)
            or not isinstance(baseline_gates, list)
            or not isinstance(policy_version, str)
            or not isinstance(policy_sha256, str)
        ):
            return None
        try:
            public_key = base64.b64decode(public_key_value, validate=True)
            gate_ids = tuple(baseline_gates)
            if (
                len(public_key) != 32
                or not gate_ids
                or len(set(gate_ids)) != len(gate_ids)
                or any(
                    not isinstance(gate, str)
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", gate) is None
                    for gate in gate_ids
                )
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", policy_version)
                is None
                or re.fullmatch(r"[0-9a-f]{64}", policy_sha256) is None
            ):
                raise ValueError("invalid execution attestation contract")
            scope = EvidenceScope(
                contract_version="v2",
                repository=str(payload.get("repository", "")),
                pr_number=int(payload.get("pr_number", 0)),
                base_sha=str(payload.get("expected_base_sha", "")),
                head_sha=str(payload.get("expected_head_sha", "")),
                concise_review=static_route.get("review_evidence_mode") == "concise",
                execution_attestation_public_key=public_key,
                baseline_execution_gates=gate_ids,
                execution_gate_policy_version=policy_version,
                execution_gate_policy_sha256=policy_sha256,
            )
        except (binascii.Error, TypeError, ValueError):
            logger.error(
                "[webhook] static route %s produced an invalid evidence tuple",
                route_name,
            )
            return None

        script = static_route["script"]

        def load_signed_control_plane_result(
            operation: str, payload_key: str, signature_key: str
        ) -> tuple[bytes, str]:
            request = {
                "operation": operation,
                "contract_version": scope.contract_version,
                "repository": scope.repository,
                "pr_number": str(scope.pr_number),
                "base_sha": scope.base_sha,
                "head_sha": scope.head_sha,
            }
            script_kwargs = (
                {
                    "timeout_seconds": 4 * 60 * 60,
                    "trusted_github_pr_environment": True,
                }
                if operation == "execution_evidence"
                else {"trusted_github_pr_environment": True}
            )
            keep, result = self._route_processor.run_route_script(
                script, request, **script_kwargs
            )
            if not keep or not isinstance(result, dict):
                raise RuntimeError("Execution attestation control plane was unavailable")
            encoded_payload = result.get(payload_key)
            signature = result.get(signature_key)
            if not isinstance(encoded_payload, str) or not isinstance(signature, str):
                raise RuntimeError("Execution attestation control plane returned malformed data")
            try:
                attestation_payload = base64.b64decode(encoded_payload, validate=True)
                decoded_signature = base64.b64decode(signature, validate=True)
            except binascii.Error as exc:
                raise RuntimeError(
                    "Execution attestation control plane returned malformed data"
                ) from exc
            if len(attestation_payload) > 1_000_000 or len(decoded_signature) != 64:
                raise RuntimeError("Execution attestation control plane exceeded fixed limits")
            return attestation_payload, signature

        scope.gate_resolution_loader = lambda: load_signed_control_plane_result(
            "resolve_execution_gates",
            "gate_resolution_payload",
            "gate_resolution_signature",
        )
        scope.execution_attestation_loader = lambda: load_signed_control_plane_result(
            "execution_evidence",
            "attestation_payload",
            "attestation_signature",
        )
        return scope

    async def on_processing_complete(
        self, event: "MessageEvent", outcome: Any
    ) -> None:
        """Close the per-delivery webhook session once its run finishes.

        A webhook delivery is one-shot: the ``delivery_id`` is baked into the
        session key, so the session will never receive a second turn.  Mirror
        the cron completion path (``cron/scheduler.py`` →
        ``end_session(..., "cron_complete")``) by marking the session ended
        when the run completes.  Without this, webhook sessions keep
        ``ended_at`` NULL forever; ``SessionDB.prune_sessions`` only reaps
        rows with ``ended_at`` set, so unclosed webhook sessions accumulate
        unbounded and drive state.db bloat (the ghost-session leak).

        This hook is the one seam that runs at the TRUE end of the run:
        ``BasePlatformAdapter._process_message_background`` fires it after the
        message handler returns, on the success, failure, and cancellation
        paths alike — so error runs are reaped too.  (``handle_message`` is
        fire-and-forget; wrapping IT closes before the run even starts.)
        ``end_session()`` is first-reason-wins and no-ops on an already-ended
        row, so this never clobbers a ``compression``/``agent_close`` reason.
        """
        await self._settle_github_review(event.source.chat_id, outcome)
        await self._end_webhook_session(event, event.source.chat_id)

    async def _settle_github_review(
        self, session_chat_id: str, outcome: Any
    ) -> None:
        """Complete or release a route-script reservation for a formal review."""
        delivery = self._delivery_info.get(session_chat_id, {})
        if delivery.get("deliver") != "github_review":
            return
        route_name = delivery.get("_trusted_evidence_route")
        tuple_data = delivery.get("_evidence_tuple")
        lease_token = delivery.get("_settlement_lease_token")
        if not isinstance(route_name, str):
            return
        static_route = self._static_routes.get(route_name)
        if (
            not isinstance(tuple_data, dict)
            or not isinstance(lease_token, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", lease_token) is None
            or not isinstance(static_route, dict)
            or static_route.get("evidence") != "github_pr"
            or not isinstance(static_route.get("script"), str)
            or not isinstance(static_route.get("deliver_extra"), dict)
            or static_route["deliver_extra"].get("contract_version") != "v2"
        ):
            return
        try:
            trusted_tuple = EvidenceScope(**tuple_data)
        except (TypeError, ValueError):
            logger.error(
                "[webhook] refusing malformed github_review settlement authority for %s",
                session_chat_id,
            )
            return

        operation = (
            "complete"
            if (
                outcome == ProcessingOutcome.SUCCESS
                and session_chat_id in self._successful_github_reviews
            )
            else "release"
        )
        settlement_payload = {
            "operation": operation,
            "contract_version": trusted_tuple.contract_version,
            "repository": trusted_tuple.repository,
            "pr_number": str(trusted_tuple.pr_number),
            "base_sha": trusted_tuple.base_sha,
            "head_sha": trusted_tuple.head_sha,
            "lease_token": lease_token,
        }
        if operation == "release":
            settlement_payload["failure_code"] = delivery.get(
                "_github_review_failure_code", "processing_failed"
            )
        keep, _ = await asyncio.to_thread(
            self._route_processor.run_route_script,
            static_route["script"],
            settlement_payload,
            trusted_github_pr_environment=True,
        )
        if not keep:
            logger.error(
                "[webhook] github_review reservation %s failed for %s",
                operation,
                session_chat_id,
            )
        self._successful_github_reviews.discard(session_chat_id)

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
            if asyncio.iscoroutine(result):
                await result
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
        """Validate webhook signature (GitHub, GitLab, Svix, generic HMAC-SHA256)."""
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

        if deliver_type == "github_review":
            return await self._deliver_github_review(content, delivery)

        # Fall through to the cross-platform dispatcher, which validates the
        # target name and routes via the gateway runner.
        return await self._deliver_cross_platform(
            deliver_type, content, delivery
        )

    async def _deliver_github_review(
        self, content: str, delivery: dict
    ) -> SendResult:
        """Publish one non-approving formal PR review after immutable-state checks."""
        extra = delivery.get("deliver_extra", {})
        repo = extra.get("repo", "")
        pr_number = extra.get("pr_number", "")
        base_sha = extra.get("base_sha", "")
        base_ref = extra.get("base_ref", "")
        head_sha = extra.get("head_sha", "")
        publisher_login = extra.get("publisher_login", "")
        requested_reviewer = extra.get("requested_reviewer", "")

        try:
            pr_int = int(pr_number)
            if pr_int <= 0:
                raise ValueError("non-positive")
        except (ValueError, TypeError):
            return SendResult(success=False, error="Invalid pr_number")
        if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo):
            return SendResult(success=False, error="Invalid repo format")
        if not re.fullmatch(r"[0-9a-f]{40}", str(base_sha)):
            return SendResult(success=False, error="Invalid base_sha")
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,255}", str(base_ref)):
            return SendResult(success=False, error="Invalid base_ref")
        if not re.fullmatch(r"[0-9a-f]{40}", str(head_sha)):
            return SendResult(success=False, error="Invalid head_sha")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", str(publisher_login)):
            return SendResult(success=False, error="Invalid publisher_login")
        if requested_reviewer != publisher_login:
            return SendResult(success=False, error="Reviewer/publisher mismatch")
        if not review_evidence_complete_for(
            "v2", repo, pr_int, str(base_sha), str(head_sha)
        ):
            return SendResult(
                success=False,
                error="GitHub PR review evidence is incomplete or out of scope",
            )
        if not execution_evidence_complete_for(
            "v2", repo, pr_int, str(base_sha), str(head_sha)
        ):
            return SendResult(
                success=False,
                error="GitHub PR execution evidence is incomplete or out of scope",
            )
        review_marker = (
            "<!-- newtonsapple-pr-review:v2 "
            f"repo={repo} pr={pr_int} base={base_sha} head={head_sha} -->"
        )
        if review_marker not in content:
            return SendResult(
                success=False, error="Missing canonical review marker"
            )
        markers = re.findall(
            r"<!-- newtonsapple-pr-review:v2\b[^>]*-->", content
        )
        if markers != [review_marker]:
            return SendResult(
                success=False, error="Conflicting canonical review marker"
            )

        run_kwargs: Dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": 30,
        }
        try:
            actor_result = subprocess.run(["gh", "api", "user"], **run_kwargs)
            if actor_result.returncode != 0:
                return SendResult(success=False, error=actor_result.stderr)
            actor = json.loads(actor_result.stdout)
            if actor.get("login") != publisher_login:
                return SendResult(success=False, error="Publisher identity mismatch")

            pr_result = subprocess.run(
                ["gh", "api", f"repos/{repo}/pulls/{pr_int}"], **run_kwargs
            )
            if pr_result.returncode != 0:
                return SendResult(success=False, error=pr_result.stderr)
            live_pr = json.loads(pr_result.stdout)
            requested = {
                item.get("login")
                for item in live_pr.get("requested_reviewers", [])
                if isinstance(item, dict)
            }
            if (
                live_pr.get("state") != "open"
                or live_pr.get("draft") is not False
                or live_pr.get("base", {}).get("sha") != base_sha
                or live_pr.get("base", {}).get("ref") != base_ref
                or live_pr.get("head", {}).get("sha") != head_sha
                or requested_reviewer not in requested
            ):
                return SendResult(success=False, error="PR state changed before publish")

            existing_result = subprocess.run(
                [
                    "gh", "api", "--paginate", "--slurp",
                    f"repos/{repo}/pulls/{pr_int}/reviews",
                ],
                **run_kwargs,
            )
            if existing_result.returncode != 0:
                return SendResult(success=False, error=existing_result.stderr)
            pages = json.loads(existing_result.stdout)
            if not isinstance(pages, list):
                return SendResult(
                    success=False, error="Invalid GitHub marker response"
                )
            pending = list(pages)
            existing_reviews: list[dict[str, Any]] = []
            while pending:
                item = pending.pop()
                if isinstance(item, list):
                    pending.extend(item)
                elif isinstance(item, dict):
                    existing_reviews.append(item)
            for review in existing_reviews:
                review_id = review.get("id")
                author = review.get("user")
                body = review.get("body")
                if (
                    isinstance(review_id, int)
                    and not isinstance(review_id, bool)
                    and review_id > 0
                    and isinstance(author, dict)
                    and author.get("login") == publisher_login
                    and isinstance(body, str)
                    and review_marker in body
                    and review.get("state") == "COMMENTED"
                    and review.get("commit_id") == head_sha
                ):
                    logger.info(
                        "[webhook] COMMENT review already exists on %s#%s",
                        repo,
                        pr_int,
                    )
                    return SendResult(success=True)

            result = subprocess.run(
                [
                    "gh", "api", "--method", "POST",
                    f"repos/{repo}/pulls/{pr_int}/reviews",
                    "-f", f"body={content}",
                    "-f", "event=COMMENT",
                    "-f", f"commit_id={head_sha}",
                ],
                **run_kwargs,
            )
            if result.returncode == 0:
                accepted = json.loads(result.stdout)
                accepted_user = accepted.get("user")
                if (
                    isinstance(accepted.get("id"), int)
                    and accepted.get("id") > 0
                    and isinstance(accepted_user, dict)
                    and accepted_user.get("login") == publisher_login
                    and accepted.get("state") == "COMMENTED"
                    and accepted.get("commit_id") == head_sha
                ):
                    logger.info(
                        "[webhook] Posted COMMENT review on %s#%s", repo, pr_int
                    )
                    return SendResult(success=True)
                return SendResult(
                    success=False,
                    error="GitHub did not confirm the expected formal review",
                )
            return SendResult(success=False, error=result.stderr)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.error("[webhook] github_review delivery error: %s", exc)
            return SendResult(success=False, error=str(exc))
        except Exception as exc:
            logger.error("[webhook] github_review delivery error: %s", exc)
            return SendResult(success=False, error=str(exc))

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
