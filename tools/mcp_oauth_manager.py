#!/usr/bin/env python3
"""Central manager for per-server MCP OAuth state.

One instance shared across the process. Holds per-server OAuth provider
instances and coordinates:

- **Cross-process token reload** via mtime-based disk watch. When an external
  process (e.g. a user cron job) refreshes tokens on disk, the next auth flow
  picks them up without requiring a process restart.
- **401 deduplication** via in-flight futures. When N concurrent tool calls
  all hit 401 with the same access_token, only one recovery attempt fires;
  the rest await the same result.
- **Reconnect signalling** for long-lived MCP sessions. The manager itself
  does not drive reconnection — the `MCPServerTask` in `mcp_tool.py` does —
  but the manager is the single source of truth that decides when reconnect
  is warranted.

Replaces what used to be scattered across eight call sites in `mcp_oauth.py`,
`mcp_tool.py`, and `hermes_cli/mcp_config.py`. This module is the ONLY place
that instantiates the MCP SDK's `OAuthClientProvider` — all other code paths
go through `get_manager()`.

Design reference:

- Claude Code's ``invalidateOAuthCacheIfDiskChanged``
  (``claude-code/src/utils/auth.ts:1320``, CC-1096 / GH#24317). Identical
  external-refresh staleness bug class.
- Codex's ``refresh_oauth_if_needed`` / ``persist_if_needed``
  (``codex-rs/rmcp-client/src/rmcp_client.rs:805``). We lean on the MCP SDK's
  lazy refresh rather than calling refresh before every op, because one
  ``stat()`` per tool call is cheaper than an ``await`` + potential refresh
  round-trip, and the SDK's in-memory expiry path is already correct.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _oauth_control_headers(configured_headers: Any, request_headers: Any) -> Any:
    """Merge safe client headers into an explicit SDK OAuth request."""
    import httpx

    merged = httpx.Headers({
        key: value
        for key, value in (configured_headers or {}).items()
        if key.lower() != "authorization"
    })
    # HTTPX Headers.update is case-insensitive and request values win, matching
    # AsyncClient.build_request while preserving SDK-generated Authorization.
    merged.update(request_headers)
    return merged


def _rebuild_oauth_control_request(request: Any, headers: Any) -> Any:
    """Rebuild an SDK request without changing its method, URL, body, or extensions."""
    import httpx

    return httpx.Request(
        request.method,
        request.url,
        content=request.content,
        headers=headers,
        extensions=dict(request.extensions),
    )


def _merge_oauth_control_request(client: Any, request: Any) -> Any:
    """Apply safe HTTPX client headers to one explicit OAuth request."""
    if not hasattr(request, "headers"):
        return request
    return _rebuild_oauth_control_request(
        request,
        _oauth_control_headers(getattr(client, "headers", {}), request.headers),
    )


def _StrictRedirectAsyncClient(*args: Any, **kwargs: Any) -> Any:
    """Build HTTPX client enforcing configured headers at redirect creation."""
    import types
    import httpx

    kwargs.pop("redirect_origin")
    configured_header_names = frozenset(kwargs.pop("configured_header_names"))
    client = httpx.AsyncClient(*args, **kwargs)
    original_builder = client._build_redirect_request

    def build_redirect_request(self: Any, request: Any, response: Any) -> Any:
        next_request = original_builder(request, response)
        target = next_request.url
        current = request.url
        if (target.scheme, target.host, target.port) != (
            current.scheme,
            current.host,
            current.port,
        ):
            next_request.headers.pop("authorization", None)
            for name in configured_header_names:
                next_request.headers.pop(name, None)
        return next_request

    client._build_redirect_request = types.MethodType(build_redirect_request, client)
    return client


class MCPAuthFlowProtocolError(RuntimeError):
    """Raised when the installed SDK cannot provide HTTPX auth-flow semantics."""


class MCPAuthFlowLifecycleError(RuntimeError):
    """Raised when a cached provider has unsafe loop or flow ownership."""


class MCPAuthConfigurationError(ValueError):
    """Raised when OAuth construction settings cannot be represented safely."""


class MCPAuthControlPlaneError(RuntimeError):
    """Raised when cold OAuth control-plane discovery cannot be trusted."""


def _oauth_config_fingerprint(oauth_config: Optional[dict]) -> str:
    """Fingerprint JSON OAuth settings; reject opaque values fail-closed."""
    if oauth_config is not None and not isinstance(oauth_config, dict):
        raise MCPAuthConfigurationError("OAuth configuration must be a JSON object")
    def _fingerprint_value(value: Any) -> Any:
        if callable(value):
            return {
                "__callable__": f"{getattr(value, '__module__', '')}."
                f"{getattr(value, '__qualname__', repr(value))}",
                "identity": id(value),
            }
        if isinstance(value, dict):
            return {str(k): _fingerprint_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_fingerprint_value(v) for v in value]
        return value

    try:
        encoded = json.dumps(
            _fingerprint_value(oauth_config or {}),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MCPAuthConfigurationError(
            "OAuth configuration contains unsupported non-JSON values"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _effective_provider_fingerprint(
    server_name: str,
    server_url: str,
    oauth_config: Optional[dict],
    transport_options: dict[str, Any],
) -> str:
    """Fingerprint the effective provider construction inputs.

    The manager must compare what the provider will actually receive, rather
    than the raw user fragment before provider-specific defaults and transport
    defaults are applied.  Keep this pure: callback-port reservation belongs
    to provider construction and must not happen while holding the cache lock.
    """
    from tools.mcp_oauth import apply_oauth_provider_defaults

    effective_oauth = dict(oauth_config or {})
    apply_oauth_provider_defaults(
        effective_oauth, server_name=server_name, server_url=server_url
    )
    effective_oauth.setdefault("client_name", "Hermes Agent")
    if not effective_oauth.get("token_endpoint_auth_method"):
        effective_oauth["token_endpoint_auth_method"] = (
            "client_secret_post"
            if effective_oauth.get("client_secret")
            else "none"
        )
    effective_oauth.setdefault("redirect_host", "127.0.0.1")
    effective_oauth.setdefault("timeout", 300.0)
    try:
        effective_redirect_port = int(effective_oauth.get("redirect_port", 0))
    except (TypeError, ValueError) as exc:
        raise MCPAuthConfigurationError(
            "OAuth redirect_port must be an integer"
        ) from exc
    effective_oauth["effective_redirect_uri"] = effective_oauth.get(
        "redirect_uri"
    ) or (
        f"http://{effective_oauth['redirect_host']}:{effective_redirect_port}"
        "/callback"
    )
    effective_transport = {
        "connect_timeout": 60.0,
        "read_timeout": 300.0,
        "ssl_verify": True,
        "client_cert": None,
        "follow_redirects": True,
        "headers": {},
        "request_hooks": [],
        "response_hooks": [],
        "strict_redirect_headers": False,
        **transport_options,
    }
    return _oauth_config_fingerprint(
        {"oauth": effective_oauth, "transport": effective_transport}
    )


def _same_endpoint(a: str, b: str) -> bool:
    """Return True if two URLs target the same endpoint (ignoring query/fragment).

    Compares scheme, host (case-insensitive), and path. Used to confirm a
    rejected response actually came from the OAuth token endpoint before we
    act on an ``invalid_client`` body.
    """
    from urllib.parse import urlsplit

    try:
        pa, pb = urlsplit(a), urlsplit(b)
    except ValueError:  # pragma: no cover — malformed URL
        return False
    return (
        pa.scheme == pb.scheme
        and pa.netloc.lower() == pb.netloc.lower()
        and pa.path.rstrip("/") == pb.path.rstrip("/")
    )


def _running_loop_or_none() -> Optional[asyncio.AbstractEventLoop]:
    """Return the running loop, or None when called from sync code.

    Providers are built from both the MCP event loop and synchronous CLI
    paths; the latter have no loop to record.
    """
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


# ---------------------------------------------------------------------------
# Per-server entry
# ---------------------------------------------------------------------------


@dataclass
class _ProviderEntry:
    """Per-server OAuth state tracked by the manager.

    Fields:
        server_url: The MCP server URL used to build the provider. Tracked
            so we can discard a cached provider if the URL changes.
        oauth_config: Optional dict from ``mcp_servers.<name>.oauth``.
        provider: The ``httpx.Auth``-compatible provider wrapping the MCP
            SDK. None until first use.
        last_mtime_ns: Last-seen ``st_mtime_ns`` of the on-disk tokens file.
            Zero if never read. Used by :meth:`MCPOAuthManager.invalidate_if_disk_changed`
            to detect external refreshes.
        lock: Serialises concurrent access to this entry's state. Bound to
            whichever asyncio loop first awaits it (the MCP event loop).
        loop: The event loop ``lock`` (and the SDK provider's own
            ``context.lock``) got bound to. Recorded so the entry can be
            discarded when that loop is gone — see
            :meth:`MCPOAuthManager._is_entry_loop_usable`.
        pending_401: In-flight 401-handler futures keyed by the failed
            access_token, for deduplicating thundering-herd 401s. Mirrors
            Claude Code's ``pending401Handlers`` map.
    """

    server_url: str
    oauth_config: Optional[dict]
    provider: Optional[Any] = None
    last_mtime_ns: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    loop: Optional[asyncio.AbstractEventLoop] = None
    oauth_config_fingerprint: str = ""
    requested_fingerprint: str = ""
    resolved_callback_fingerprint: str = ""
    transport_options: dict[str, Any] = field(default_factory=dict)
    pending_401: dict[str, "asyncio.Future[bool]"] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# HermesMCPOAuthProvider — OAuthClientProvider subclass with disk-watch
# ---------------------------------------------------------------------------


def _make_hermes_provider_class() -> Optional[type]:
    """Lazy-import the SDK base class and return our subclass.

    Wrapped in a function so this module imports cleanly even when the
    MCP SDK's OAuth module is unavailable (e.g. older mcp versions).
    """
    try:
        from mcp.client.auth.oauth2 import OAuthClientProvider
    except ImportError:  # pragma: no cover — SDK required in CI
        return None

    class HermesMCPOAuthProvider(OAuthClientProvider):
        """OAuthClientProvider with pre-flow disk-mtime reload.

        Before every ``async_auth_flow`` invocation, asks the manager to
        check whether the tokens file on disk has been modified externally.
        If so, the manager resets ``_initialized`` so the next flow
        re-reads from storage.

        This makes external-process refreshes (cron, another CLI instance)
        visible to the running MCP session without requiring a restart.

        Reference: Claude Code's ``invalidateOAuthCacheIfDiskChanged``
        (``src/utils/auth.ts:1320``, CC-1096 / GH#24317).
        """

        def __init__(
            self,
            *args: Any,
            server_name: str = "",
            preregistered: bool = False,
            **kwargs: Any,
        ):
            super().__init__(*args, **kwargs)
            self._hermes_server_name = server_name
            self._hermes_home = ""
            # When the client_id comes from config.yaml (pre-registered), an
            # invalid_client rejection means the *config* is wrong — deleting
            # client.json would just be re-seeded from config and re-running
            # registration can't help. Only auto-heal dynamically-registered
            # clients. See _maybe_flag_poisoned_client.
            self._hermes_preregistered = preregistered
            self._hermes_active_flows: dict[int, Any] = {}
            self._hermes_loop: Optional[asyncio.AbstractEventLoop] = None
            self._hermes_generation = 0

        async def _initialize(self) -> None:
            """Load stored tokens + client info AND seed token_expiry_time.

            Also eagerly fetches OAuth authorization-server metadata (PRM +
            ASM) when we have stored tokens but no cached metadata, so the
            SDK's ``_refresh_token`` can build the correct token_endpoint
            URL on the preemptive-refresh path. Without this, the SDK
            falls back to ``{mcp_server_url}/token`` (wrong for providers
            whose AS is a different origin — BetterStack's MCP lives at
            ``https://mcp.betterstack.com`` but its token endpoint is at
            ``https://betterstack.com/oauth/token``), the refresh 404s, and
            we drop through to full browser reauth.

            The SDK's base ``_initialize`` populates ``current_tokens`` but
            does NOT call ``update_token_expiry``, so ``token_expiry_time``
            stays ``None`` and ``is_token_valid()`` returns True for any
            loaded token regardless of actual age. After a process restart
            this ships stale Bearer tokens to the server; some providers
            return HTTP 401 (caught by the 401 handler), others return 200
            with an app-level auth error (invisible to the transport layer,
            e.g. BetterStack returning "No teams found. Please check your
            authentication.").

            Seeding ``token_expiry_time`` from the reloaded token fixes that:
            ``is_token_valid()`` correctly reports False for expired tokens,
            ``async_auth_flow`` takes the ``can_refresh_token()`` branch,
            and the SDK quietly refreshes before the first real request.

            Paired with :class:`HermesTokenStorage` persisting an absolute
            ``expires_at`` timestamp (``mcp_oauth.py:set_tokens``) so the
            remaining TTL we compute here reflects real wall-clock age.
            """
            await super()._initialize()
            tokens = self.context.current_tokens
            if tokens is not None and tokens.expires_in is not None:
                self.context.update_token_expiry(tokens)

            # Cold-load: restore OAuth server metadata from disk before any
            # refresh attempt. Without this, a restarted process with cached
            # tokens but no in-memory metadata would fall back to the SDK's
            # guessed ``{server_url}/token`` path (returns 404 on most real
            # providers) and require a full browser re-authorization.
            storage = self.context.storage
            from tools.mcp_oauth import HermesTokenStorage
            if (
                isinstance(storage, HermesTokenStorage)
                and self.context.oauth_metadata is None
            ):
                meta = storage.load_oauth_metadata()
                if meta is not None:
                    self.context.oauth_metadata = meta
                    logger.debug(
                        "MCP OAuth '%s': restored metadata from disk "
                        "(token_endpoint=%s)",
                        self._hermes_server_name,
                        meta.token_endpoint,
                    )

            # Pre-flight OAuth AS discovery so ``_refresh_token`` has a
            # correct ``token_endpoint`` before the first refresh attempt.
            # Only runs when we have tokens on cold-load but no cached
            # metadata — i.e. the exact scenario where the SDK's built-in
            # 401-branch discovery hasn't had a chance to run yet.
            if (
                tokens is not None
                and self.context.oauth_metadata is None
            ):
                try:
                    await self._prefetch_oauth_metadata()
                except Exception as exc:  # pragma: no cover — defensive
                    if getattr(self, "_hermes_control_plane_required", False):
                        raise MCPAuthControlPlaneError(
                            "cold OAuth control-plane discovery failed closed"
                        ) from exc
                    logger.debug(
                        "MCP OAuth '%s': legacy direct provider discovery unavailable: %s",
                        self._hermes_server_name,
                        exc,
                    )

        async def _handle_refresh_response(self, response: Any) -> bool:
            """Never leave a failed refresh bearer readable on disk."""
            invalidate = getattr(self.context.storage, "invalidate_tokens", None)

            def invalidate_durably() -> None:
                if not callable(invalidate):
                    return
                try:
                    invalidate()
                except BaseException as cleanup_exc:
                    # Durable cleanup is secondary to the SDK's refresh
                    # outcome.  The in-memory token has already been cleared;
                    # never replace a primary refresh exception with unlink or
                    # persistence diagnostics.
                    logger.warning(
                        "MCP OAuth '%s': failed to invalidate durable tokens "
                        "after refresh failure: %s",
                        self._hermes_server_name,
                        cleanup_exc,
                    )

            try:
                ok = await super()._handle_refresh_response(response)
            except BaseException:
                self.context.clear_tokens()
                invalidate_durably()
                raise
            if not ok:
                self.context.clear_tokens()
                invalidate_durably()
            return ok

        async def _prefetch_oauth_metadata(self) -> None:
            """Fetch PRM + ASM from the well-known endpoints, cache on context.

            Mirrors the SDK's 401-branch discovery (oauth2.py ~line 511-551)
            but runs synchronously before the first request instead of
            inside the httpx auth_flow generator. Uses the SDK's own URL
            builders and response handlers so we track whatever the SDK
            version we're pinned to expects.
            """
            import httpx  # local import: httpx is an MCP SDK dependency
            from mcp.client.auth.utils import (
                build_oauth_authorization_server_metadata_discovery_urls,
                build_protected_resource_metadata_discovery_urls,
                create_oauth_metadata_request,
                handle_auth_metadata_response,
                handle_protected_resource_response,
            )

            server_url = self.context.server_url
            options = getattr(self, "_hermes_transport_options", {})
            timeout = float(options.get("connect_timeout", 10.0))
            configured_headers = {
                key: value
                for key, value in (options.get("headers") or {}).items()
                if key.lower() != "authorization"
            }
            client_kwargs = {
                "timeout": httpx.Timeout(timeout, read=300.0),
                "follow_redirects": True,
                "verify": options.get("ssl_verify", True),
                "headers": configured_headers,
                "event_hooks": {
                    "request": list(options.get("request_hooks") or []),
                    "response": list(options.get("response_hooks") or []),
                },
            }
            if options.get("strict_redirect_headers"):
                from tools.mcp_oauth_manager import _StrictRedirectAsyncClient
                client_kwargs["redirect_origin"] = httpx.URL(server_url)
                client_kwargs["configured_header_names"] = {
                    key.lower() for key in configured_headers
                }
            if options.get("client_cert") is not None:
                client_kwargs["cert"] = options["client_cert"]
            if options.get("strict_redirect_headers"):
                client = _StrictRedirectAsyncClient(**client_kwargs)
            else:
                client = httpx.AsyncClient(**client_kwargs)
            async with client:
                # Step 1: PRM discovery to learn the authorization_server URL.
                prm = None
                for url in build_protected_resource_metadata_discovery_urls(
                    None, server_url
                ):
                    req = _merge_oauth_control_request(
                        client, create_oauth_metadata_request(url)
                    )
                    try:
                        resp = await client.send(req)
                    except httpx.HTTPError as exc:
                        logger.debug(
                            "MCP OAuth '%s': PRM discovery to %s failed: %s",
                            self._hermes_server_name, url, exc,
                        )
                        continue
                    prm = await handle_protected_resource_response(resp)
                    if prm:
                        await self._validate_resource_match(prm)
                        self.context.protected_resource_metadata = prm
                        if len(prm.authorization_servers) != 1:
                            raise MCPAuthControlPlaneError(
                                "protected-resource discovery has ambiguous authorization servers"
                            )
                        self.context.auth_server_url = str(prm.authorization_servers[0])
                        break

                if prm is None or self.context.auth_server_url is None:
                    raise MCPAuthControlPlaneError(
                        "protected-resource metadata did not resolve an authorization server"
                    )

                # Step 2: ASM discovery against the auth_server_url (or
                # server_url fallback for legacy providers).
                for url in build_oauth_authorization_server_metadata_discovery_urls(
                    self.context.auth_server_url, server_url
                ):
                    req = _merge_oauth_control_request(
                        client, create_oauth_metadata_request(url)
                    )
                    try:
                        resp = await client.send(req)
                    except httpx.HTTPError as exc:
                        logger.debug(
                            "MCP OAuth '%s': ASM discovery to %s failed: %s",
                            self._hermes_server_name, url, exc,
                        )
                        continue
                    ok, asm = await handle_auth_metadata_response(resp)
                    if not ok:
                        break
                    if asm:
                        self.context.oauth_metadata = asm
                        # Persist immediately so a subsequent cold-load can
                        # skip discovery entirely.
                        storage = self.context.storage
                        from tools.mcp_oauth import HermesTokenStorage
                        if isinstance(storage, HermesTokenStorage):
                            storage.save_oauth_metadata(asm)
                        logger.debug(
                            "MCP OAuth '%s': pre-flight ASM discovered "
                            "token_endpoint=%s",
                            self._hermes_server_name, asm.token_endpoint,
                        )
                        break
                if self.context.oauth_metadata is None:
                    raise MCPAuthControlPlaneError(
                        "authorization-server metadata was unavailable"
                    )

        def _persist_oauth_metadata_if_changed(self) -> None:
            """Persist discovered OAuth metadata for future process restarts.

            Called after the SDK's normal 401-branch auth flow completes so
            metadata discovered via the lazy path (not pre-flight) is also
            saved. No-op when nothing to persist or metadata hasn't changed.
            """
            meta = self.context.oauth_metadata
            if meta is None:
                return
            storage = self.context.storage
            from tools.mcp_oauth import HermesTokenStorage
            if not isinstance(storage, HermesTokenStorage):
                return
            existing = storage.load_oauth_metadata()
            if (
                existing is None
                or str(existing.token_endpoint) != str(meta.token_endpoint)
            ):
                storage.save_oauth_metadata(meta)

        async def _maybe_flag_poisoned_client(self, response: Any) -> None:
            """Detect a dead client registration and force re-registration.

            When the IdP rejects our ``client_id`` with ``invalid_client`` on
            the token endpoint (token exchange or refresh), the cached client
            registration is provably dead server-side. We delete ``client.json``
            (+ stale metadata) so the SDK's next ``async_auth_flow`` takes the
            ``if not client_info`` branch and re-runs RFC 7591 dynamic client
            registration. This addresses the recurring manual-reset ritual in
            GH#36767 for the auto-detectable subset (token-endpoint rejection);
            the browser-side "Redirect URI Mismatch" case has no HTTP signal
            and is handled by ``hermes mcp reauth``.

            Conservative by construction — acts ONLY when all hold:
              * status is 400/401,
              * the request hit the discovered ``token_endpoint`` (the only
                request carrying our ``client_id``), and
              * the body carries the ``invalid_client`` error code
                (word-boundary match, so RFC 7591's ``invalid_client_metadata``
                registration error does not trip it).
            Pre-registered (config-supplied) clients are never poisoned.
            Fully best-effort: any failure here is swallowed so a detection
            miss never breaks the live auth flow.

            Covers both the authorization-code token exchange and the
            preemptive refresh — but only when ``token_endpoint`` was
            discovered (``_initialize`` prefetches it on cold-load). If that
            discovery was skipped, the guard returns early and the user falls
            back to ``hermes mcp reauth``.
            """
            try:
                if self._hermes_preregistered:
                    return
                status = getattr(response, "status_code", None)
                if status not in (400, 401):
                    return
                meta = getattr(self.context, "oauth_metadata", None)
                token_endpoint = (
                    str(meta.token_endpoint)
                    if meta is not None and getattr(meta, "token_endpoint", None)
                    else None
                )
                req = getattr(response, "request", None)
                req_url = str(req.url) if req is not None else None
                if not token_endpoint or not req_url:
                    return
                if not _same_endpoint(req_url, token_endpoint):
                    return
                body = await response.aread()
                # Word-boundary match: matches `"error":"invalid_client"` but
                # not the RFC 7591 registration error `invalid_client_metadata`
                # (the trailing `_metadata` removes the right-hand boundary).
                if not re.search(rb"\binvalid_client\b", body.lower()):
                    return

                storage = self.context.storage
                from tools.mcp_oauth import HermesTokenStorage
                if isinstance(storage, HermesTokenStorage):
                    storage.poison_client_registration()
                # Drop the in-memory client so the SDK re-registers next flow.
                self.context.client_info = None
                self._initialized = False
            except Exception as exc:  # pragma: no cover — defensive, must not throw
                logger.debug(
                    "MCP OAuth '%s': invalid_client detection failed (non-fatal): %s",
                    self._hermes_server_name, exc,
                )

        @staticmethod
        def _is_loop_current(loop: Optional[asyncio.AbstractEventLoop]) -> bool:
            if loop is None or loop.is_closed() or not loop.is_running():
                return False
            try:
                return loop is asyncio.get_running_loop()
            except RuntimeError:
                return False

        async def _close_flow_on_owner(self, flow_id: int, flow: Any) -> None:
            try:
                await flow.aclose()
            except BaseException:
                self._hermes_cleanup_failed = True
                raise
            else:
                self._hermes_active_flows.pop(flow_id, None)

        async def _close_all_flows_on_owner(self, flows: list[tuple[int, Any]]) -> None:
            for flow_id, flow in flows:
                await self._close_flow_on_owner(flow_id, flow)

        async def async_auth_flow(self, request):  # type: ignore[override]
            # Pre-flow hook: ask the manager to refresh from disk if needed.
            # Any failure here is non-fatal — we just log and proceed with
            # whatever state the SDK already has.
            try:
                await get_manager().invalidate_if_disk_changed(
                    self._hermes_server_name,
                    hermes_home=self._hermes_home,
                )
            except Exception as exc:  # pragma: no cover — defensive
                logger.debug(
                    "MCP OAuth '%s': pre-flow disk-watch failed (non-fatal): %s",
                    self._hermes_server_name, exc,
                )

            # Manually bridge the bidirectional generator protocol. httpx's
            # auth_flow driver (httpx._client._send_handling_auth) calls
            # ``auth_flow.asend(response)`` to feed HTTP responses back into
            # the generator. A naive wrapper using ``async for item in inner:
            # yield item`` DISCARDS those .asend(response) values and resumes
            # the inner generator with None, so the SDK's
            # ``response = yield request`` branch in
            # mcp/client/auth/oauth2.py sees response=None and crashes at
            # ``if response.status_code == 401`` with AttributeError.
            #
            # The bridge below forwards each .asend() value into the inner
            # generator via inner.asend(incoming), preserving the bidirectional
            # contract. Regression from PR #11383 caught by
            # tests/tools/test_mcp_oauth_bidirectional.py.
            current_loop = asyncio.get_running_loop()
            owner_loop = getattr(self, "_hermes_loop", None)
            if owner_loop is not None and (
                owner_loop is not current_loop
                or owner_loop.is_closed()
                or not owner_loop.is_running()
            ):
                raise MCPAuthFlowLifecycleError(
                    "MCP OAuth provider is not owned by the current running event loop"
                )
            if owner_loop is None:
                self._hermes_loop = current_loop

            # Construct the SDK generator only after loop affinity is proven.
            # An async-generator object created on a rejected path otherwise
            # escapes the ownership registry and is left to GC for cleanup.
            inner = super().async_auth_flow(request)
            required = ("__anext__", "asend", "aclose")
            if any(not callable(getattr(inner, name, None)) for name in required):
                protocol_error = MCPAuthFlowProtocolError(
                    "MCP SDK OAuth auth flow must implement __anext__, asend, and aclose"
                )
                close = getattr(inner, "aclose", None)
                if callable(close):
                    try:
                        close_result = close()
                        if inspect.isawaitable(close_result):
                            await close_result
                    except BaseException as cleanup_exc:
                        logger.warning(
                            "MCP OAuth '%s': incompatible SDK flow cleanup failed: %s",
                            self._hermes_server_name,
                            cleanup_exc,
                        )
                        raise protocol_error from cleanup_exc
                raise protocol_error
            active_flows = getattr(self, "_hermes_active_flows", None)
            if active_flows is None:
                active_flows = self._hermes_active_flows = {}
            active_flows[id(inner)] = inner
            generation = getattr(self, "_hermes_generation", 0)
            self._hermes_generation = generation
            primary: BaseException | None = None

            async def _close_inner() -> None:
                flow_id = id(inner)
                if flow_id not in active_flows:
                    return
                try:
                    await inner.aclose()
                except BaseException:
                    self._hermes_cleanup_failed = True
                    raise
                else:
                    active_flows.pop(flow_id, None)

            def merge_outgoing(request):
                if not hasattr(request, "headers"):
                    return request
                options = getattr(self, "_hermes_transport_options", {})
                return _rebuild_oauth_control_request(
                    request,
                    _oauth_control_headers(options.get("headers"), request.headers),
                )

            try:
                outgoing = merge_outgoing(await inner.__anext__())
                while True:
                    incoming = yield outgoing
                    if generation != self._hermes_generation:
                        raise MCPAuthFlowLifecycleError(
                            "MCP OAuth auth flow was fenced by provider replacement"
                        )
                    # Sniff the response for a dead-client-registration signal
                    # before handing it back to the SDK (best-effort, GH#36767).
                    await self._maybe_flag_poisoned_client(incoming)
                    # MCP 1.28.1 treats every ``insufficient_scope`` 403 as a
                    # step-up, including malformed challenges with no scope.
                    # Do not let an ambiguous challenge trigger authorization;
                    # normalize it to a non-step-up error while preserving the
                    # response-driven generator protocol.
                    if incoming is not None and incoming.status_code == 403:
                        challenge = incoming.headers.get("www-authenticate", "")
                        error_match = re.search(
                            r'error\s*=\s*"([^"]*)"', challenge, re.I
                        )
                        scope_match = re.search(
                            r'scope\s*=\s*"([^"]*)"', challenge, re.I
                        )
                        if (
                            error_match
                            and error_match.group(1) == "insufficient_scope"
                            and (scope_match is None or not scope_match.group(1).strip())
                        ):
                            import httpx

                            headers = dict(incoming.headers)
                            headers["www-authenticate"] = 'Bearer error="invalid_token"'
                            incoming = httpx.Response(
                                403,
                                headers=headers,
                                request=incoming.request,
                            )
                    outgoing = merge_outgoing(await inner.asend(incoming))
            except StopAsyncIteration:
                # Persist any metadata the SDK discovered lazily during the
                # 401 branch so a subsequent cold-load skips discovery.
                try:
                    self._persist_oauth_metadata_if_changed()
                except BaseException as exc:
                    primary = exc
                    raise
                return
            except GeneratorExit:
                # Caller-driven outer aclose is cleanup control, not an
                # operation failure. A delegated close error must therefore be
                # surfaced by the finally block when no real primary exists.
                raise
            except BaseException as exc:
                primary = exc
                raise
            finally:
                # Close the inner SDK auth-flow generator so it can release its
                # SDK lock in the task that owns the HTTPX bridge. A cleanup
                # failure must never replace a transport/provider exception.
                try:
                    await _close_inner()
                except BaseException as cleanup_exc:
                    if primary is not None:
                        logger.warning(
                            "MCP OAuth '%s': inner auth-flow cleanup failed "
                            "while preserving primary exception: %s",
                            self._hermes_server_name,
                            cleanup_exc,
                        )
                    else:
                        raise

    return HermesMCPOAuthProvider


# Cached at import time. Tested and used by :class:`MCPOAuthManager`.
_HERMES_PROVIDER_CLS: Optional[type] = _make_hermes_provider_class()


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class MCPOAuthManager:
    """Single source of truth for per-server MCP OAuth state.

    Thread-safe: the ``_entries`` dict is guarded by ``_entries_lock`` for
    get-or-create semantics. Per-entry state is guarded by the entry's own
    ``asyncio.Lock`` (used from the MCP event loop thread).
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], _ProviderEntry] = {}
        self._entries_lock = threading.Lock()
        # Holds strong references to in-flight 401 handler tasks so the
        # event loop's weak-reference bookkeeping cannot GC them mid-run
        # and leave `await pending` waiters hanging forever.
        self._inflight_tasks: set[asyncio.Task] = set()

    # -- Provider construction / caching -------------------------------------

    def get_or_build_provider(
        self,
        server_name: str,
        server_url: str,
        oauth_config: Optional[dict],
        transport_options: Optional[dict] = None,
    ) -> Optional[Any]:
        """Return the provider for an endpoint/config and safe transport policy."""
        transport_options = dict(transport_options or {})
        fingerprint = _effective_provider_fingerprint(
            server_name, server_url, oauth_config, transport_options
        )
        key = self._key(server_name)
        with self._entries_lock:
            entry = self._entries.get(key)
            if entry is not None and (
                entry.server_url != server_url
                or (
                    entry.requested_fingerprint
                    or entry.oauth_config_fingerprint
                )
                != fingerprint
            ):
                if self._entry_has_active_flows(entry):
                    if not self._fence_and_schedule_close(entry):
                        raise MCPAuthFlowLifecycleError(
                            "cannot replace an MCP OAuth provider while its auth flow "
                            "owner cannot be safely scheduled"
                        )
                logger.info(
                    "MCP OAuth '%s': endpoint or construction policy changed; "
                    "fencing cached provider",
                    server_name,
                )
                entry = None

            if entry is not None and not self._is_entry_loop_usable(entry):
                if self._entry_has_active_flows(entry):
                    if not self._fence_and_schedule_close(entry):
                        raise MCPAuthFlowLifecycleError(
                            "cannot rebuild an MCP OAuth provider whose suspended auth "
                            "flow belongs to an unavailable event loop"
                        )
                logger.info(
                    "MCP OAuth '%s': cached provider loop is not current/usable; rebuilding",
                    server_name,
                )
                entry = None

            if entry is None:
                entry = _ProviderEntry(
                    server_url=server_url,
                    oauth_config=dict(oauth_config or {}),
                    oauth_config_fingerprint=fingerprint,
                    requested_fingerprint=fingerprint,
                    transport_options=transport_options,
                )
                self._entries[key] = entry

            if entry.provider is None:
                entry.provider = self._build_provider(server_name, entry)
                if entry.provider is not None:
                    entry.provider._hermes_home = key[0]
                    entry.loop = _running_loop_or_none()
                    if entry.loop is not None:
                        entry.provider._hermes_loop = entry.loop
                    resolved = getattr(entry.provider, "_hermes_resolved_port", None)
                    if resolved:
                        callback_identity = (
                            f"{entry.oauth_config.get('redirect_host', '127.0.0.1')}"
                            f":{int(resolved)}"
                        )
                        entry.resolved_callback_fingerprint = callback_identity
                        entry.oauth_config_fingerprint = _effective_provider_fingerprint(
                            server_name,
                            server_url,
                            entry.oauth_config,
                            {
                                **entry.transport_options,
                                "resolved_callback_identity": callback_identity,
                            },
                        )

            return entry.provider

    @staticmethod
    def _entry_has_active_flows(entry: _ProviderEntry) -> bool:
        """Return whether replacement would strand a delegated SDK generator."""
        provider = entry.provider
        return bool(provider and getattr(provider, "_hermes_active_flows", None))

    async def retry_active_flow_cleanup(
        self,
        server_name: str,
        *,
        hermes_home: str | Path | None = None,
    ) -> bool:
        """Retry a failed owner-side close and reap only after success."""
        with self._entries_lock:
            entry = self._entries.get(self._key(server_name, hermes_home))
        if entry is None or entry.provider is None:
            return True
        provider = entry.provider
        cleanup_task = getattr(provider, "_hermes_cleanup_task", None)
        if cleanup_task is not None and not cleanup_task.done():
            owner = entry.loop or getattr(provider, "_hermes_loop", None)
            if owner is None or owner.is_closed() or not owner.is_running():
                return False

            async def wait_for_owner_cleanup() -> bool:
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    raise
                except BaseException:
                    # The failed close is now terminal.  Retain poisoned
                    # ownership, clear only the completed task marker, and
                    # let the explicit retry below make the next close claim.
                    pass
                if getattr(provider, "_hermes_cleanup_task", None) is cleanup_task:
                    provider._hermes_cleanup_task = None
                return True

            current = asyncio.get_running_loop()
            if current is owner:
                await wait_for_owner_cleanup()
            else:
                future = asyncio.run_coroutine_threadsafe(
                    wait_for_owner_cleanup(), owner
                )
                await asyncio.wrap_future(future)
            flows = getattr(provider, "_hermes_active_flows", {})
            if not flows:
                provider._hermes_cleanup_failed = False
                return True

        flows = getattr(provider, "_hermes_active_flows", {})
        if not flows:
            provider._hermes_cleanup_failed = False
            return True
        owner = entry.loop or getattr(provider, "_hermes_loop", None)
        if owner is None or owner.is_closed() or not owner.is_running():
            return False

        async def close_on_owner() -> bool:
            for flow_id, flow in list(flows.items()):
                try:
                    await flow.aclose()
                except BaseException as exc:
                    provider._hermes_cleanup_failed = True
                    logger.warning(
                        "MCP OAuth '%s': explicit owner-side flow cleanup retry failed: %s",
                        server_name,
                        exc,
                    )
                    return False
                else:
                    if flows.get(flow_id) is flow:
                        flows.pop(flow_id, None)
            provider._hermes_cleanup_failed = bool(flows)
            return not flows

        current = asyncio.get_running_loop()
        if current is owner:
            return await close_on_owner()
        future = asyncio.run_coroutine_threadsafe(close_on_owner(), owner)
        return await asyncio.wrap_future(future)

    def _fence_and_schedule_close(self, entry: _ProviderEntry) -> bool:
        """Fence an entry and schedule all delegated closes on its owner loop."""
        provider = entry.provider
        if provider is None:
            return True
        owner = entry.loop or getattr(provider, "_hermes_loop", None)
        if owner is None or owner.is_closed() or not owner.is_running():
            return False
        flows = list(getattr(provider, "_hermes_active_flows", {}).items())
        if not flows:
            return True
        if getattr(provider, "_hermes_cleanup_failed", False):
            return False
        previous_task = getattr(provider, "_hermes_cleanup_task", None)
        if previous_task is not None and not previous_task.done():
            return False
        provider._hermes_generation = getattr(provider, "_hermes_generation", 0) + 1

        async def close_all() -> None:
            for flow_id, flow in flows:
                try:
                    await asyncio.wait_for(flow.aclose(), timeout=5.0)
                except BaseException:
                    provider._hermes_cleanup_failed = True
                    raise
                else:
                    active = getattr(provider, "_hermes_active_flows", {})
                    if active.get(flow_id) is flow:
                        active.pop(flow_id, None)
            provider._hermes_cleanup_failed = False

        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        close_coro = close_all()
        try:
            if current is owner:
                # A synchronous cache API cannot safely await a coroutine on
                # its own running loop. Schedule bounded cleanup, observe its
                # result, and fail closed without publishing a replacement.
                task = asyncio.create_task(close_coro)
                provider._hermes_cleanup_task = task

                def _observe(task: asyncio.Task) -> None:
                    try:
                        task.result()
                    except BaseException as exc:
                        provider._hermes_cleanup_failed = True
                        logger.warning(
                            "MCP OAuth '%s': owner-side flow cleanup failed: %s",
                            getattr(provider, "_hermes_server_name", ""),
                            exc,
                        )
                    else:
                        provider._hermes_cleanup_task = None

                task.add_done_callback(_observe)
                return False
            else:
                future = asyncio.run_coroutine_threadsafe(close_coro, owner)
                # A replacement requested from another live loop must not be
                # published before owner-side ``aclose`` has completed.
                future.result(timeout=5.0)
        except BaseException:
            close_coro.close()
            return False
        return True

    @staticmethod
    def _is_entry_loop_usable(entry: _ProviderEntry) -> bool:
        """True only when the provider owner is the current live event loop."""
        loop = entry.loop
        if loop is None and entry.provider is not None:
            loop = getattr(entry.provider, "_hermes_loop", None)
        if loop is None:
            return True
        if loop.is_closed() or not loop.is_running():
            return False
        try:
            return loop is asyncio.get_running_loop()
        except RuntimeError:
            return False

    @staticmethod
    def _key(
        server_name: str,
        hermes_home: str | Path | None = None,
    ) -> tuple[str, str]:
        from hermes_constants import get_hermes_home

        home = Path(hermes_home) if hermes_home is not None else get_hermes_home()
        return (str(home.expanduser().resolve(strict=False)), server_name)

    def _build_provider(
        self,
        server_name: str,
        entry: _ProviderEntry,
    ) -> Optional[Any]:
        """Build the underlying OAuth provider.

        Constructs :class:`HermesMCPOAuthProvider` directly using the helpers
        extracted from ``tools.mcp_oauth``. The subclass injects a pre-flow
        disk-watch hook so external token refreshes (cron, other CLI
        instances) are visible to running MCP sessions.

        Returns None if the MCP SDK's OAuth support is unavailable.
        """
        if _HERMES_PROVIDER_CLS is None:
            logger.warning(
                "MCP OAuth '%s': SDK auth module unavailable", server_name,
            )
            return None

        # Local imports avoid circular deps at module import time.
        from tools.mcp_oauth import (
            HermesTokenStorage,
            OAuthNonInteractiveError,
            _OAUTH_AVAILABLE,
            _build_client_metadata,
            _configure_callback_port,
            _is_interactive,
            _maybe_preregister_client,
            _make_callback_waiter,
            _make_redirect_handler,
        )

        if not _OAUTH_AVAILABLE:
            return None

        cfg = dict(entry.oauth_config or {})
        from tools.mcp_oauth import apply_oauth_provider_defaults

        apply_oauth_provider_defaults(
            cfg, server_name=server_name, server_url=entry.server_url
        )
        storage = HermesTokenStorage(server_name)

        from tools.mcp_dashboard_oauth import get_dashboard_oauth_flow

        if (
            get_dashboard_oauth_flow() is None
            and not _is_interactive()
            and not storage.has_cached_tokens()
            and not (
                callable(cfg.get("redirect_handler"))
                and callable(cfg.get("callback_handler"))
            )
        ):
            raise OAuthNonInteractiveError(
                "MCP OAuth for "
                f"'{server_name}': non-interactive environment and no "
                "cached tokens found. Run `hermes mcp login "
                f"{server_name}` interactively first to complete initial "
                "authorization."
            )

        _configure_callback_port(cfg, storage)
        client_metadata = _build_client_metadata(cfg)
        _maybe_preregister_client(storage, cfg, client_metadata)

        resolved_port = cfg.get("_resolved_port", 0)
        redirect_handler = cfg.get("redirect_handler")
        if not callable(redirect_handler):
            redirect_handler = _make_redirect_handler(
                resolved_port, redirect_uri=cfg.get("redirect_uri")
            )
        callback_handler = cfg.get("callback_handler")
        if not callable(callback_handler):
            callback_handler = _make_callback_waiter(
                resolved_port, timeout=float(cfg.get("timeout", 300))
            )

        provider = _HERMES_PROVIDER_CLS(
            server_name=server_name,
            preregistered=bool(cfg.get("client_id")),
            server_url=entry.server_url,
            client_metadata=client_metadata,
            storage=storage,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
            timeout=float(cfg.get("timeout", 300)),
            client_metadata_url=cfg.get("client_metadata_url"),
        )
        provider._hermes_transport_options = dict(entry.transport_options)
        provider._hermes_resolved_port = resolved_port
        provider._hermes_callback_identity = (
            cfg.get("redirect_uri")
            or f"{cfg.get('redirect_host', '127.0.0.1')}:{resolved_port}/callback"
        )
        provider._hermes_control_plane_required = bool(entry.transport_options)
        return provider

    def remove(
        self,
        server_name: str,
        *,
        hermes_home: str | Path | None = None,
    ) -> _ProviderEntry | None:
        """Evict the provider from cache AND delete tokens from disk.

        Called by ``hermes mcp remove <name>`` and (indirectly) by
        ``hermes mcp login <name>`` during forced re-auth.
        """
        with self._entries_lock:
            entry = self._entries.pop(self._key(server_name, hermes_home), None)

        from tools.mcp_oauth import remove_oauth_tokens
        remove_oauth_tokens(server_name, hermes_home=hermes_home)
        logger.info(
            "MCP OAuth '%s': evicted from cache and removed from disk",
            server_name,
        )
        return entry

    def restore_entry(
        self,
        server_name: str,
        entry: _ProviderEntry | None,
        *,
        hermes_home: str | Path | None = None,
    ) -> None:
        """Restore a provider entry removed for a failed reauthorization."""
        if entry is None:
            return
        with self._entries_lock:
            self._entries.setdefault(self._key(server_name, hermes_home), entry)

    def evict(
        self,
        server_name: str,
        *,
        hermes_home: str | Path | None = None,
    ) -> None:
        """Drop only the in-process provider, preserving persisted OAuth state."""
        with self._entries_lock:
            self._entries.pop(self._key(server_name, hermes_home), None)

    # -- Disk watch ----------------------------------------------------------

    async def invalidate_if_disk_changed(
        self,
        server_name: str,
        *,
        hermes_home: str | Path | None = None,
    ) -> bool:
        """If the tokens file on disk has a newer mtime than last-seen, force
        the MCP SDK provider to reload its in-memory state.

        Returns True if the cache was invalidated (mtime differed). This is
        the core fix for the external-refresh workflow: a cron job writes
        fresh tokens to disk, and on the next tool call the running MCP
        session picks them up without a restart.
        """
        from tools.mcp_oauth import _get_token_dir, _safe_filename

        entry = self._entries.get(self._key(server_name, hermes_home))
        if entry is None or entry.provider is None:
            return False

        async with entry.lock:
            tokens_path = _get_token_dir(hermes_home) / f"{_safe_filename(server_name)}.json"
            try:
                mtime_ns = tokens_path.stat().st_mtime_ns
            except (FileNotFoundError, OSError):
                return False

            if mtime_ns != entry.last_mtime_ns:
                old = entry.last_mtime_ns
                entry.last_mtime_ns = mtime_ns
                # Force the SDK's OAuthClientProvider to reload from storage
                # on its next auth flow. `_initialized` is private API but
                # stable across the MCP SDK versions we pin (>=1.26.0).
                if hasattr(entry.provider, "_initialized"):
                    entry.provider._initialized = False  # noqa: SLF001
                logger.info(
                    "MCP OAuth '%s': tokens file changed (mtime %d -> %d), "
                    "forcing reload",
                    server_name, old, mtime_ns,
                )
                return True
            return False

    # -- 401 handler (dedup'd) -----------------------------------------------

    async def handle_401(
        self,
        server_name: str,
        failed_access_token: Optional[str] = None,
    ) -> bool:
        """Handle a 401 from a tool call, deduplicated across concurrent callers.

        Returns:
            True  if a (possibly new) access token is now available — caller
                  should trigger a reconnect and retry the operation.
            False if no recovery path exists — caller should surface a
                  ``needs_reauth`` error to the model so it stops hallucinating
                  manual refresh attempts.

        Thundering-herd protection: if N concurrent tool calls hit 401 with
        the same ``failed_access_token``, only one recovery attempt fires.
        Others await the same future.
        """
        entry = self._entries.get(self._key(server_name))
        if entry is None or entry.provider is None:
            return False

        key = failed_access_token or "<unknown>"
        loop = asyncio.get_running_loop()

        async with entry.lock:
            pending = entry.pending_401.get(key)
            if pending is None:
                pending = loop.create_future()
                entry.pending_401[key] = pending

                async def _do_handle() -> None:
                    try:
                        # Step 1: Did disk change? Picks up external refresh.
                        disk_changed = await self.invalidate_if_disk_changed(
                            server_name
                        )
                        if disk_changed:
                            if not pending.done():
                                pending.set_result(True)
                            return

                        # Step 2: No disk change — if the SDK can refresh
                        # in-place, let the caller retry. The SDK's httpx.Auth
                        # flow will issue the refresh on the next request.
                        provider = entry.provider
                        ctx = getattr(provider, "context", None)
                        can_refresh = False
                        if ctx is not None:
                            can_refresh_fn = getattr(ctx, "can_refresh_token", None)
                            if callable(can_refresh_fn):
                                try:
                                    can_refresh = bool(can_refresh_fn())
                                except Exception:
                                    can_refresh = False
                        if not pending.done():
                            pending.set_result(can_refresh)
                    except Exception as exc:  # pragma: no cover — defensive
                        logger.warning(
                            "MCP OAuth '%s': 401 handler failed: %s",
                            server_name, exc,
                        )
                        if not pending.done():
                            pending.set_result(False)
                    finally:
                        entry.pending_401.pop(key, None)

                task = asyncio.create_task(_do_handle())
                self._inflight_tasks.add(task)
                task.add_done_callback(self._inflight_tasks.discard)

        try:
            return await pending
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "MCP OAuth '%s': awaiting 401 handler failed: %s",
                server_name, exc,
            )
            return False


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


_MANAGER: Optional[MCPOAuthManager] = None
_MANAGER_LOCK = threading.Lock()


def get_manager() -> MCPOAuthManager:
    """Return the process-wide :class:`MCPOAuthManager` singleton."""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = MCPOAuthManager()
        return _MANAGER


def reset_manager_for_tests() -> None:
    """Test-only helper: drop the singleton so fixtures start clean."""
    global _MANAGER
    with _MANAGER_LOCK:
        _MANAGER = None
