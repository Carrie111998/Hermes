#!/usr/bin/env python3
"""Service-account (M2M) credential provider for MCP HTTP servers.

Exchanges a long-lived service-account password for a short-lived Bearer
access token, injects ``Authorization: Bearer <access_token>`` into MCP
requests, and renews automatically.  This is distinct from the
browser-based PKCE flow (``auth: oauth``) — no user interaction is
required.

Grant strategy is **explicit**, never inferred from which fields happen to
be present.  ``service_account.grant_type`` selects it and is required.

Supported strategies
--------------------
``authentik_app_password``
    Authentik's service-account extension.  Posts ``grant_type=
    client_credentials`` together with a resource-owner ``username`` /
    ``password`` pair.  Note this is *not* the RFC 6749 §4.4.2
    client-credentials request, which carries no username/password — it is
    a provider extension that happens to reuse the same wire grant name.
    Providers whose M2M flow is plain client authentication (Keycloak
    service accounts, Auth0 M2M) are **not** supported by this strategy;
    adding a standards-conforming ``client_credentials`` strategy is a
    separate, additive change.

Configuration in config.yaml::

    mcp_servers:
      toolhive:
        url: https://mcp.example/mcp
        auth: service_account
        service_account:
          grant_type: authentik_app_password         # required, explicit
          token_url: https://idp.example/application/o/toolhive/token/
          client_id: toolhive
          username: zug
          password_env: AUTHENTIK_ZUG_APP_PASSWORD   # env-var name, not value
          scope: "openid profile groups toolhive-audience"
          client_secret_env: OPTIONAL_CLIENT_SECRET  # optional

Secret values (password, client_secret) are **never** stored in
config.yaml. Only the environment-variable *names* appear there; the
values are read at runtime via ``agent.secret_scope.get_secret`` which
honours the active profile's isolated secret scope under multiplexing and
falls back to ``os.environ`` in single-profile mode.

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
- The token endpoint must be ``https://``.  This is enforced both when the
  config is validated and again immediately before every token request, so
  a credential-bearing form can never be sent in the clear.
- Token-endpoint redirects are **not** followed.  A 307/308 is
  method-preserving, so an authorization server (or a compromised or
  misconfigured one) could otherwise redirect the POST — password and
  client secret included — to an origin the config never authorised.  The
  config proves exactly one secret sink; runtime does not widen it.  A 3xx
  from the token endpoint is surfaced as an error.
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

from agent.secret_scope import get_secret as _get_scoped_secret

logger = logging.getLogger(__name__)

# ── How many seconds before nominal expiry to proactively renew the token.
_PROACTIVE_RENEW_BUFFER_SECONDS = 60

# ── Env-var name validation — same rule as shell identifier.
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# ── Grant strategies.  The config value is a Hermes-level discriminator, not
#    the OAuth wire ``grant_type`` — see GRANT_WIRE_TYPES below.
GRANT_AUTHENTIK_APP_PASSWORD = "authentik_app_password"

#: Config-level grant strategies this provider implements.  Adding a
#: standards-conforming ``client_credentials`` strategy is additive: extend
#: this set, GRANT_WIRE_TYPES, and _build_exchange_form.
SUPPORTED_GRANT_TYPES: frozenset[str] = frozenset({GRANT_AUTHENTIK_APP_PASSWORD})

#: Config strategy → the ``grant_type`` value actually sent on the wire.
#: Authentik's service-account extension reuses the ``client_credentials``
#: wire name while adding a resource-owner username/password pair, so the
#: two names deliberately differ here.
GRANT_WIRE_TYPES: dict[str, str] = {
    GRANT_AUTHENTIK_APP_PASSWORD: "client_credentials",
}

#: Fields required per grant strategy, on top of the common ones.
_GRANT_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    GRANT_AUTHENTIK_APP_PASSWORD: ("username", "password_env"),
}

#: Required regardless of strategy.
_COMMON_REQUIRED_FIELDS: tuple[str, ...] = ("token_url", "client_id")


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

    # Grant strategy is explicit — never inferred from field presence.
    grant_type = cfg.get("grant_type")
    if not grant_type:
        errors.append(
            f"MCP server '{name}': service_account.grant_type is required. "
            f"Supported: {', '.join(sorted(SUPPORTED_GRANT_TYPES))}"
        )
    elif str(grant_type) not in SUPPORTED_GRANT_TYPES:
        errors.append(
            f"MCP server '{name}': service_account.grant_type "
            f"'{grant_type}' is not supported. "
            f"Supported: {', '.join(sorted(SUPPORTED_GRANT_TYPES))}"
        )

    required = _COMMON_REQUIRED_FIELDS + _GRANT_REQUIRED_FIELDS.get(
        str(grant_type), ()
    )
    for field in required:
        if not cfg.get(field):
            errors.append(f"MCP server '{name}': service_account.{field} is required")

    token_url = cfg.get("token_url", "")
    if token_url and not str(token_url).startswith("https://"):
        errors.append(
            f"MCP server '{name}': service_account.token_url must be an "
            "https:// URL — the token request carries the service-account "
            "password and must not be sent over plaintext HTTP"
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
    """Fetch the service-account password from the active profile secret scope.

    Reads the env-var named in ``password_env`` via ``agent.secret_scope.get_secret``
    so the active profile's isolated scope is honoured under multiplexing. Falls
    back to ``os.environ`` in single-profile mode (when no secret scope is
    installed and multiplexing is inactive).

    Raises ``ValueError`` with a non-secret message if the secret is missing or
    empty. In multiplex mode with no scope installed, ``get_secret`` raises
    ``UnscopedSecretError`` (a ``RuntimeError`` subclass) before this function
    constructs its own error — that propagates as-is to the caller.
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
    value = _get_scoped_secret(str(env_name)) or ""
    if not value:
        raise ValueError(
            f"MCP service-account '{server_name}': environment variable "
            f"'{env_name}' is not set or is empty. "
            f"Set it in the profile's $HERMES_HOME/.env before connecting."
        )
    return value


def _resolve_client_secret(cfg: dict) -> Optional[str]:
    """Return the optional client secret from the profile secret scope, or None."""
    env_name = cfg.get("client_secret_env", "")
    if not env_name:
        return None
    return _get_scoped_secret(str(env_name)) or None


# ---------------------------------------------------------------------------
# Token cache (disk)
# ---------------------------------------------------------------------------


def _get_sa_token_path(
    server_name: str, hermes_home: Optional[str | Path] = None
) -> Path:
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

    The ``https://`` requirement is re-checked here rather than trusted from
    validation time: this is the last point before a credential-bearing body
    leaves the process, and the caller may have been handed a config that
    never passed through :func:`validate_service_account_config`.

    The caller must supply a client with redirects disabled; a 3xx from the
    token endpoint therefore falls through to the non-2xx branch and is
    reported as an error rather than replaying the form at a new origin.
    """
    if not str(token_url).startswith("https://"):
        raise ValueError(
            f"MCP service-account '{server_name}': refusing to send "
            "credentials to a non-https:// token endpoint"
        )

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

    if 300 <= resp.status_code < 400:
        # Redirects are deliberately not followed: replaying a
        # password-bearing POST at a Location the config never authorised is
        # credential egress to an unproven sink.  Never log the Location.
        raise ValueError(
            f"MCP service-account '{server_name}': token endpoint returned a "
            f"redirect (HTTP {resp.status_code}); redirects are not followed "
            "because the request carries credentials. Point token_url at the "
            "authorization server's final https:// token endpoint."
        )

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
                self._server_name,
                exc,
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

    def _build_exchange_form(
        self, password: str, client_secret: Optional[str]
    ) -> dict:
        """Build the token-request form for this server's grant strategy.

        Dispatch is on the explicit ``grant_type`` discriminator, so a new
        strategy adds a branch here rather than changing meaning for existing
        configs.
        """
        cfg = self._cfg
        grant = str(cfg.get("grant_type", ""))
        if grant not in SUPPORTED_GRANT_TYPES:
            raise ValueError(
                f"MCP service-account '{self._server_name}': unsupported "
                f"service_account.grant_type '{grant}'. "
                f"Supported: {', '.join(sorted(SUPPORTED_GRANT_TYPES))}"
            )

        form: dict = {
            "grant_type": GRANT_WIRE_TYPES[grant],
            "client_id": cfg["client_id"],
        }
        if grant == GRANT_AUTHENTIK_APP_PASSWORD:
            # Authentik's service-account extension: client_credentials on the
            # wire plus a resource-owner username/password pair.
            form["username"] = cfg["username"]
            form["password"] = password
        if cfg.get("scope"):
            form["scope"] = cfg["scope"]
        if client_secret:
            form["client_secret"] = client_secret
        return form

    async def _exchange_service_account(self, http_client: Any) -> _CachedToken:
        """Exchange the service-account credential for an access token."""
        cfg = self._cfg
        password = _resolve_password(cfg, self._server_name)
        client_secret = _resolve_client_secret(cfg)

        form = self._build_exchange_form(password, client_secret)

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

            _httpx_mod = getattr(_transport, "httpx2", None) or getattr(
                _transport, "httpx", None
            )
        except ImportError:
            _httpx_mod = None
        if _httpx_mod is None:
            try:
                import httpx2 as _httpx_mod  # type: ignore[no-redef]
            except ImportError:
                import httpx as _httpx_mod  # type: ignore[no-redef]

        # follow_redirects=False is a security requirement, not a default:
        # 307/308 preserve the method and body, so following one would replay
        # the service-account password at whatever origin the token endpoint
        # names.  _post_token_request turns any 3xx into an error.
        async with _httpx_mod.AsyncClient(follow_redirects=False) as token_client:
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
