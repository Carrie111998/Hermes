#!/usr/bin/env python3
"""Service-account (M2M) OAuth credential provider for MCP HTTP servers.

Exchanges a long-lived service-account password for a short-lived Bearer
access token via the OAuth 2.0 ``client_credentials`` grant, injects
``Authorization: Bearer <access_token>`` into MCP requests, and renews
automatically.  This is distinct from the browser-based PKCE flow
(``auth: oauth``) — no user interaction is required.

Configuration in config.yaml::

    mcp_servers:
      toolhive:
        url: https://mcp.example/mcp
        auth: service_account
        service_account:
          token_url: https://idp.example/application/o/toolhive/token/
          client_id: toolhive
          username: zug
          password_env: AUTHENTIK_ZUG_APP_PASSWORD   # env-var name, not value
          scope: "openid profile groups toolhive-audience"
          client_secret_env: OPTIONAL_CLIENT_SECRET  # optional

Secret values (password, client_secret) are **never** stored in
config.yaml. Only the environment-variable *names* appear there; the
values are read at runtime from the process environment (which is
populated from the active profile's ``.env`` file before any MCP
connection is made).

Token caching
-------------
Tokens are cached at ``$HERMES_HOME/mcp-tokens/<server>-sa.json`` with
file permissions 0o600 and atomic write (O_EXCL temp-then-rename). Two
Hermes profiles with the same server name get separate cache files
because the path is rooted at the profile's ``HERMES_HOME``.

The access token is cached; the service-account password is never written
to disk. If the server returns a ``refresh_token``, it is cached and used
on subsequent renewals, falling back to a fresh service-account exchange
if the refresh fails.

httpx compatibility
-------------------
``ServiceAccountAuth`` inherits from the ``Auth`` class exported by
whichever httpx distribution the installed MCP SDK uses (plain ``httpx``
for mcp < 2.0, ``httpx2`` for mcp >= 2.0).  The base class is resolved
once at module import time via :func:`_resolve_auth_base` and stored in
``_SA_AUTH_BASE``.  This makes the provider a valid ``isinstance(...,
httpx.Auth)`` object and therefore acceptable to ``AsyncClient(auth=...)``.

Security
--------
- TLS verification is always on; no way to disable it from config.
- Passwords, access tokens, Authorization header values, and token
  responses are never logged.  Errors are redacted before surfacing.
- ``password_env`` accepts only a legal environment-variable name.
- The password is fetched once per token exchange and not held in memory
  beyond the HTTP request coroutine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import stat
import time
import threading as _threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── How many seconds before nominal expiry to proactively renew the token.
_PROACTIVE_RENEW_BUFFER_SECONDS = 60

# ── Env-var name validation — same rule as shell identifier.
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ---------------------------------------------------------------------------
# httpx Auth base — resolved from the SDK's own httpx distribution
# ---------------------------------------------------------------------------

def _resolve_auth_base() -> type:
    """Return the ``Auth`` base class from the MCP SDK's httpx distribution.

    mcp >= 2.0 ships its transports against ``httpx2`` rather than the
    upstream ``httpx``.  ``AsyncClient(auth=...)`` uses ``isinstance(auth,
    Auth)`` from *its own* httpx module, so the provider must inherit from
    the same class.  We mirror what :func:`tools.mcp_tool.sdk_httpx` does
    (read the transport module's ``httpx2`` or ``httpx`` attribute) without
    importing all of ``mcp_tool`` to avoid a heavy circular-import at
    module load time.
    """
    try:
        from mcp.client import streamable_http as _transport
        _mod = getattr(_transport, "httpx2", None) or getattr(_transport, "httpx", None)
        if _mod is not None and hasattr(_mod, "Auth"):
            return _mod.Auth
    except ImportError:
        pass
    # Fallback: httpx2 then httpx
    try:
        import httpx2 as _h
        return _h.Auth
    except ImportError:
        import httpx as _h  # type: ignore[no-redef]
        return _h.Auth


_SA_AUTH_BASE: type = _resolve_auth_base()


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def validate_service_account_config(name: str, cfg: dict) -> list[str]:
    """Return a list of human-readable validation errors for a service_account block.

    ``name`` is the MCP server name (for error messages).  ``cfg`` is the
    value of the ``service_account:`` sub-key in the server config.
    """
    errors: list[str] = []
    if not isinstance(cfg, dict):
        return [f"MCP server '{name}': service_account must be a mapping"]

    required = ("token_url", "client_id", "username", "password_env")
    for field in required:
        if not cfg.get(field):
            errors.append(
                f"MCP server '{name}': service_account.{field} is required"
            )

    token_url = cfg.get("token_url", "")
    if token_url and not str(token_url).startswith(("https://", "http://")):
        errors.append(
            f"MCP server '{name}': service_account.token_url must be an HTTP(S) URL"
        )

    for env_field in ("password_env", "client_secret_env"):
        val = cfg.get(env_field)
        if val and not _ENV_VAR_NAME_RE.match(str(val)):
            errors.append(
                f"MCP server '{name}': service_account.{env_field} must be a "
                "valid environment-variable name (letters, digits, underscores)"
            )

    return errors


def _resolve_password(cfg: dict, server_name: str) -> str:
    """Fetch the service-account password from the configured source.

    Currently supports only ``password_env`` (an environment-variable name).
    The variable must be set in the process environment (populated from the
    active profile's ``.env`` before MCP connections are established).

    Raises ``ValueError`` with a non-secret message if the env var is missing
    or empty.
    """
    env_name = cfg.get("password_env", "")
    if not env_name:
        raise ValueError(
            f"MCP service-account '{server_name}': password_env is required"
        )
    if not _ENV_VAR_NAME_RE.match(str(env_name)):
        raise ValueError(
            f"MCP service-account '{server_name}': password_env "
            f"'{env_name}' is not a valid environment-variable name"
        )
    value = os.environ.get(str(env_name), "")
    if not value:
        raise ValueError(
            f"MCP service-account '{server_name}': environment variable "
            f"'{env_name}' is not set or is empty. "
            f"Set it in your shell or in $HERMES_HOME/.env before connecting."
        )
    return value


def _resolve_client_secret(cfg: dict) -> Optional[str]:
    """Return the optional client secret, or None if not configured."""
    env_name = cfg.get("client_secret_env", "")
    if not env_name:
        return None
    return os.environ.get(str(env_name)) or None


# ---------------------------------------------------------------------------
# Token cache (disk)
# ---------------------------------------------------------------------------

def _get_sa_token_path(server_name: str, hermes_home: Optional[str | Path] = None) -> Path:
    """Return the path to the service-account token cache file.

    Kept in the same ``mcp-tokens`` directory as OAuth tokens so directory
    permissions (0o700) are already correct.  The ``-sa`` suffix distinguishes
    this file from the OAuth ``.json`` file for the same server name.
    """
    from tools.mcp_oauth import _get_token_dir, _safe_filename
    return _get_token_dir(hermes_home) / f"{_safe_filename(server_name)}-sa.json"


def _read_token_cache(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_token_cache(path: Path, data: dict) -> None:
    """Atomically write token cache to *path* with mode 0o600."""
    from hermes_constants import secure_parent_dir
    path.parent.mkdir(parents=True, exist_ok=True)
    secure_parent_dir(path)
    tmp = path.with_suffix(f".tmp.{os.getpid()}.{secrets.token_hex(4)}")
    try:
        fd = os.open(
            str(tmp),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _delete_token_cache(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


class _CachedToken:
    """In-memory view of a cached service-account token."""

    def __init__(
        self,
        access_token: str,
        expires_at: float,
        refresh_token: Optional[str] = None,
    ):
        self.access_token = access_token
        self.expires_at = expires_at
        self.refresh_token = refresh_token

    def is_valid(self, buffer: float = _PROACTIVE_RENEW_BUFFER_SECONDS) -> bool:
        return time.time() < self.expires_at - buffer

    @classmethod
    def from_dict(cls, data: dict) -> "_CachedToken | None":
        at = data.get("access_token")
        ea = data.get("expires_at")
        if not at or not ea:
            return None
        try:
            return cls(
                access_token=str(at),
                expires_at=float(ea),
                refresh_token=data.get("refresh_token") or None,
            )
        except (TypeError, ValueError):
            return None

    def to_dict(self) -> dict:
        d: dict = {
            "access_token": self.access_token,
            "expires_at": self.expires_at,
        }
        if self.refresh_token:
            d["refresh_token"] = self.refresh_token
        return d


# ---------------------------------------------------------------------------
# HTTP token exchange
# ---------------------------------------------------------------------------


async def _post_token_request(
    http_client: Any,
    token_url: str,
    form: dict,
    server_name: str,
) -> dict:
    """POST form-encoded data to token_url and parse the JSON response.

    Never logs form values (which include the password).  Raises ``ValueError``
    with a redacted error message on any failure.
    """
    try:
        resp = await http_client.post(
            token_url,
            data=form,
            headers={"Accept": "application/json"},
        )
    except Exception as exc:
        # Redact the URL in case query-string values snuck in somehow.
        raise ValueError(
            f"MCP service-account '{server_name}': token endpoint request failed"
        ) from exc

    if not (200 <= resp.status_code < 300):
        # Never include the response body — it may echo back the error_description
        # which can include credential hints.
        raise ValueError(
            f"MCP service-account '{server_name}': token endpoint returned "
            f"HTTP {resp.status_code}"
        )

    try:
        body = resp.json()
    except Exception:
        raise ValueError(
            f"MCP service-account '{server_name}': token endpoint returned "
            "non-JSON response"
        )

    if not isinstance(body, dict) or "access_token" not in body:
        raise ValueError(
            f"MCP service-account '{server_name}': token response missing "
            "'access_token' field"
        )

    return body


def _parse_token_response(
    body: dict,
    server_name: str,
    *,
    now: Optional[float] = None,
) -> _CachedToken:
    """Parse a standard token response body into a ``_CachedToken``."""
    access_token = str(body.get("access_token", ""))
    if not access_token:
        raise ValueError(
            f"MCP service-account '{server_name}': empty access_token in response"
        )
    expires_in = body.get("expires_in")
    if expires_in is None:
        # Default to 1 hour when the server omits expires_in.
        expires_in = 3600
    try:
        expires_in = int(expires_in)
    except (TypeError, ValueError):
        expires_in = 3600
    t = now if now is not None else time.time()
    return _CachedToken(
        access_token=access_token,
        expires_at=t + expires_in,
        refresh_token=body.get("refresh_token") or None,
    )


# ---------------------------------------------------------------------------
# Per-server refresh deduplication
# ---------------------------------------------------------------------------

# Keyed by (hermes_home_str, server_name) → asyncio.Lock.
_refresh_locks: dict[tuple[str, str], asyncio.Lock] = {}
_refresh_locks_mu = _threading.Lock()


def _get_refresh_lock(server_name: str, hermes_home: str) -> asyncio.Lock:
    key = (hermes_home, server_name)
    with _refresh_locks_mu:
        lock = _refresh_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _refresh_locks[key] = lock
        return lock


def _clear_refresh_locks_for_tests() -> None:
    """Test-only: reset the global lock table."""
    with _refresh_locks_mu:
        _refresh_locks.clear()


# ---------------------------------------------------------------------------
# ServiceAccountAuth — inherits from the SDK's httpx.Auth
# ---------------------------------------------------------------------------


class ServiceAccountAuth(_SA_AUTH_BASE):  # type: ignore[valid-type,misc]
    """httpx.Auth subclass for service-account M2M token exchange.

    Inherits from the ``Auth`` class of whichever httpx distribution the
    installed MCP SDK uses (``httpx`` for mcp < 2.0, ``httpx2`` for mcp
    >= 2.0).  This satisfies ``AsyncClient(auth=...)``'s ``isinstance``
    check on both SDK generations.

    The provider:
    - Caches tokens to disk at ``$HERMES_HOME/mcp-tokens/<server>-sa.json``.
    - Proactively renews tokens before they expire (60s buffer).
    - On a 401, obtains a fresh token and retries the request once.
    - Deduplicates concurrent refresh attempts within the process.
    - Never logs passwords, tokens, or Authorization header values.
    """

    # Tell httpx we need to read both request and response bodies so the
    # base-class sync stub in auth_flow() is never called (we fully override
    # async_auth_flow).  Setting these to False is safe because our
    # async_auth_flow is a complete async generator that never delegates to
    # the sync auth_flow() path.
    requires_request_body = False
    requires_response_body = False

    def __init__(
        self,
        server_name: str,
        sa_config: dict,
        *,
        hermes_home: Optional[str | Path] = None,
    ):
        # httpx.Auth.__init__ takes no arguments, but call it for compat.
        super().__init__()
        self._server_name = server_name
        self._cfg = dict(sa_config)
        from hermes_constants import get_hermes_home
        self._hermes_home = str(
            Path(hermes_home).expanduser().resolve(strict=False)
            if hermes_home is not None
            else get_hermes_home()
        )
        self._cache_path = _get_sa_token_path(server_name, self._hermes_home)
        # In-memory token — avoids a disk read on every request.
        self._mem_token: Optional[_CachedToken] = None

    @property
    def _refresh_lock(self) -> asyncio.Lock:
        return _get_refresh_lock(self._server_name, self._hermes_home)

    # -- Token resolution ----------------------------------------------------

    def _load_from_disk(self) -> Optional[_CachedToken]:
        data = _read_token_cache(self._cache_path)
        if data is None:
            return None
        return _CachedToken.from_dict(data)

    def _save_to_disk(self, token: _CachedToken) -> None:
        try:
            _write_token_cache(self._cache_path, token.to_dict())
        except OSError as exc:
            logger.warning(
                "MCP service-account '%s': failed to write token cache: %s",
                self._server_name, exc,
            )

    def _get_cached_token(self) -> Optional[_CachedToken]:
        """Return a valid in-memory or disk-cached token, or None."""
        if self._mem_token is not None and self._mem_token.is_valid():
            return self._mem_token
        disk = self._load_from_disk()
        if disk is not None and disk.is_valid():
            self._mem_token = disk
            return disk
        return None

    async def _exchange_service_account(self, http_client: Any) -> _CachedToken:
        """Perform a client_credentials grant using the service-account password."""
        cfg = self._cfg
        password = _resolve_password(cfg, self._server_name)
        client_secret = _resolve_client_secret(cfg)

        form: dict = {
            "grant_type": "client_credentials",
            "client_id": cfg["client_id"],
            "username": cfg["username"],
            "password": password,
        }
        if cfg.get("scope"):
            form["scope"] = cfg["scope"]
        if client_secret:
            form["client_secret"] = client_secret

        body = await _post_token_request(
            http_client, cfg["token_url"], form, self._server_name
        )
        token = _parse_token_response(body, self._server_name)
        del password
        if client_secret:
            del client_secret
        return token

    async def _exchange_refresh_token(
        self,
        http_client: Any,
        refresh_token: str,
    ) -> Optional[_CachedToken]:
        """Try a refresh_token grant; return None on failure."""
        cfg = self._cfg
        client_secret = _resolve_client_secret(cfg)
        form: dict = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": cfg["client_id"],
        }
        if cfg.get("scope"):
            form["scope"] = cfg["scope"]
        if client_secret:
            form["client_secret"] = client_secret
        try:
            body = await _post_token_request(
                http_client, cfg["token_url"], form, self._server_name
            )
            return _parse_token_response(body, self._server_name)
        except ValueError:
            logger.debug(
                "MCP service-account '%s': refresh_token grant failed, "
                "falling back to service-account exchange",
                self._server_name,
            )
            return None

    async def _acquire_token(self, http_client: Any) -> _CachedToken:
        """Acquire a fresh token, using refresh_token if available.

        Protected by a per-server asyncio.Lock so concurrent requests only
        trigger one exchange.
        """
        async with self._refresh_lock:
            # Re-check under lock — another coroutine may have refreshed.
            cached = self._get_cached_token()
            if cached is not None:
                return cached

            # Try refresh_token first.
            existing = self._load_from_disk() or self._mem_token
            if existing and existing.refresh_token:
                new_token = await self._exchange_refresh_token(
                    http_client, existing.refresh_token
                )
                if new_token is not None:
                    self._mem_token = new_token
                    self._save_to_disk(new_token)
                    logger.debug(
                        "MCP service-account '%s': renewed via refresh_token",
                        self._server_name,
                    )
                    return new_token

            # Fall back to service-account exchange.
            token = await self._exchange_service_account(http_client)
            self._mem_token = token
            self._save_to_disk(token)
            logger.debug(
                "MCP service-account '%s': acquired new access token",
                self._server_name,
            )
            return token

    # -- httpx.Auth protocol -------------------------------------------------

    def auth_flow(self, request: Any):  # type: ignore[override]
        # httpx.Auth requires a sync auth_flow stub; the async path below
        # is used exclusively in our async MCP context.  This stub is never
        # called because async_auth_flow is overridden and httpx prefers it.
        raise NotImplementedError(  # pragma: no cover
            "ServiceAccountAuth requires an async context; "
            "use an AsyncClient, not a sync Client"
        )

    async def async_auth_flow(self, request: Any):  # type: ignore[override]
        """Inject Bearer token, handle one 401 retry.

        httpx drives this generator:
          1. ``__anext__()``       → we yield the request with Authorization header
          2. ``asend(response)``  → we inspect the response
             - 2xx/other: generator returns → httpx uses that response
             - 401:  invalidate cache, fetch fresh token, yield retry request
             - ``asend(response2)`` → generator returns
        """
        # Build a small dedicated client for token-endpoint requests.  It is
        # created fresh each auth-flow invocation so it lives only as long as
        # a single MCP request (including one possible 401 retry).  We resolve
        # the correct httpx module here (same logic as _resolve_auth_base) to
        # handle both mcp 1.x (httpx) and mcp 2.x (httpx2) at runtime.
        try:
            from mcp.client import streamable_http as _transport
            _httpx_mod = (
                getattr(_transport, "httpx2", None)
                or getattr(_transport, "httpx", None)
            )
        except ImportError:
            _httpx_mod = None
        if _httpx_mod is None:
            try:
                import httpx2 as _httpx_mod  # type: ignore[no-redef]
            except ImportError:
                import httpx as _httpx_mod  # type: ignore[no-redef]

        async with _httpx_mod.AsyncClient(follow_redirects=True) as token_client:
            token = await self._acquire_token(token_client)

            # Inject Authorization header without logging the value.
            request.headers["Authorization"] = f"Bearer {token.access_token}"
            response = yield request

            if response.status_code != 401:
                return

            # 401: invalidate cache and retry once.
            logger.debug(
                "MCP service-account '%s': received 401, refreshing token",
                self._server_name,
            )
            self._mem_token = None
            _delete_token_cache(self._cache_path)

            token = await self._acquire_token(token_client)
            request.headers["Authorization"] = f"Bearer {token.access_token}"
            yield request


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def build_service_account_auth(
    server_name: str,
    sa_config: dict,
    *,
    hermes_home: Optional[str | Path] = None,
) -> "ServiceAccountAuth":
    """Build and return a :class:`ServiceAccountAuth` for *server_name*.

    ``sa_config`` is the value of the ``service_account:`` sub-key in the
    MCP server config dict.  Call this once per server and cache the result;
    it manages its own token state.

    Raises ``ValueError`` if the config is missing required fields.
    """
    errors = validate_service_account_config(server_name, sa_config)
    if errors:
        raise ValueError("; ".join(errors))
    return ServiceAccountAuth(server_name, sa_config, hermes_home=hermes_home)


def remove_service_account_tokens(
    server_name: str,
    *,
    hermes_home: Optional[str | Path] = None,
) -> None:
    """Delete the on-disk service-account token cache for *server_name*."""
    path = _get_sa_token_path(server_name, hermes_home)
    _delete_token_cache(path)
    logger.info("MCP service-account '%s': removed token cache", server_name)
