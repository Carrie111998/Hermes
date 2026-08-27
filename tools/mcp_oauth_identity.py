"""Requester-scoped MCP OAuth identity types and fail-closed resolution.

See docs/rfc/requester-scoped-mcp-oauth.md and issue #78174.

This module is the single source of truth for:

- ``shared`` vs ``per_user`` identity mode
- the immutable OAuth principal/scope
- opaque persistence keys
- live-connection registry tokens

Credential identity is derived only from trusted bound session context.
Tool arguments and model output must never reach these APIs as selectors.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal, Optional

IDENTITY_MODE_SHARED = "shared"
IDENTITY_MODE_PER_USER = "per_user"
VALID_IDENTITY_MODES = frozenset({IDENTITY_MODE_SHARED, IDENTITY_MODE_PER_USER})

PRINCIPAL_VERSION = "v1"
EMPTY_SCOPE_SENTINEL = "~"
PERSISTENCE_KEY_PREFIX = f"u-{PRINCIPAL_VERSION}-"
REGISTRY_SEPARATOR = "\x1f"


class InvalidMcpOAuthIdentityModeError(ValueError):
    """Raised when ``mcp.oauth.identity_mode`` is present but not a valid value."""


class MissingRequesterIdentity(RuntimeError):
    """Raised in ``per_user`` mode when no trusted bound principal exists."""


class McpOAuthScopeMismatch(RuntimeError):
    """Raised when a connection's captured scope differs from the request scope."""


@dataclass(frozen=True, slots=True)
class McpOAuthPrincipal:
    """Immutable requester identity for MCP OAuth isolation."""

    version: Literal["v1"]
    platform: str
    scope_id: str
    user_id: str

    def canonical_json(self) -> str:
        return json.dumps(
            [self.version, self.platform, self.scope_id, self.user_id],
            separators=(",", ":"),
            ensure_ascii=True,
        )

    def persistence_key(self) -> str:
        digest = hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
        return f"{PERSISTENCE_KEY_PREFIX}{digest}"


@dataclass(frozen=True, slots=True)
class McpOAuthScope:
    """Authorization isolation mode plus optional principal."""

    mode: Literal["shared", "per_user"]
    principal: Optional[McpOAuthPrincipal] = None

    def persistence_key(self) -> str:
        if self.mode == IDENTITY_MODE_SHARED or self.principal is None:
            return IDENTITY_MODE_SHARED
        return self.principal.persistence_key()

    def log_fingerprint(self) -> str:
        """Opaque identifier safe for operational logs."""
        key = self.persistence_key()
        if key == IDENTITY_MODE_SHARED:
            return IDENTITY_MODE_SHARED
        return key[:18]


SHARED_SCOPE = McpOAuthScope(mode="shared", principal=None)


@dataclass(frozen=True, slots=True)
class McpConnectionKey:
    server_name: str
    oauth_scope: McpOAuthScope

    def registry_token(self) -> str:
        return connection_registry_token(self.server_name, self.oauth_scope)


def parse_identity_mode(value: Any, *, explicit: bool = True) -> str:
    """Parse ``mcp.oauth.identity_mode``.

    Absent/None when ``explicit`` is False defaults to ``shared``.
    Any other value, including typos such as ``per-user``, is an error.
    """
    if value is None and not explicit:
        return IDENTITY_MODE_SHARED
    if isinstance(value, str) and value in VALID_IDENTITY_MODES:
        return value
    raise InvalidMcpOAuthIdentityModeError(
        "mcp.oauth.identity_mode must be 'shared' or 'per_user', "
        f"got {value!r}. A typo must not silently fall back to shared mode."
    )


def configured_identity_mode(config: Optional[dict] = None) -> str:
    """Read identity mode from a config dict or the loaded Hermes config."""
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly()
        except Exception:
            return IDENTITY_MODE_SHARED
    mcp = config.get("mcp") if isinstance(config, dict) else None
    if not isinstance(mcp, dict):
        return IDENTITY_MODE_SHARED
    oauth = mcp.get("oauth")
    if not isinstance(oauth, dict) or "identity_mode" not in oauth:
        return IDENTITY_MODE_SHARED
    return parse_identity_mode(oauth.get("identity_mode"), explicit=True)


def server_uses_oauth(config: Optional[dict]) -> bool:
    if not isinstance(config, dict):
        return False
    return str(config.get("auth") or "").lower().strip() == "oauth"


def principal_from_bound_fields(
    platform: str,
    scope_id: str,
    user_id: str,
) -> McpOAuthPrincipal:
    platform = (platform or "").strip()
    user_id = (user_id or "").strip()
    scope_id = (scope_id or "").strip() or EMPTY_SCOPE_SENTINEL
    if not platform or not user_id:
        raise MissingRequesterIdentity(
            "MCP OAuth requires an authenticated requester identity in "
            "per_user mode."
        )
    if "\x00" in platform or "\x00" in scope_id or "\x00" in user_id:
        raise MissingRequesterIdentity(
            "MCP OAuth requester identity contains invalid NUL bytes."
        )
    return McpOAuthPrincipal(
        version="v1",
        platform=platform,
        scope_id=scope_id,
        user_id=user_id,
    )


def principal_from_bound_session() -> Optional[McpOAuthPrincipal]:
    """Build a principal from the bound-only session getter. Never env fallback."""
    from gateway.session_context import get_bound_session_principal

    bound = get_bound_session_principal()
    if bound is None:
        return None
    try:
        return principal_from_bound_fields(
            bound.platform, bound.scope_id, bound.user_id
        )
    except MissingRequesterIdentity:
        return None


def resolve_mcp_oauth_scope(
    *,
    identity_mode: Optional[str] = None,
    uses_oauth: bool = True,
    principal: Optional[McpOAuthPrincipal] = None,
    config: Optional[dict] = None,
) -> McpOAuthScope:
    """Resolve the OAuth isolation scope for this request.

    Keyword-only on purpose: callers cannot pass MCP tool arguments such as
    ``user_id`` into this function.
    """
    if not uses_oauth:
        return SHARED_SCOPE

    mode = identity_mode
    if mode is None:
        mode = configured_identity_mode(config)
    else:
        mode = parse_identity_mode(mode, explicit=True)

    if mode == IDENTITY_MODE_SHARED:
        return SHARED_SCOPE

    resolved = principal
    if resolved is None:
        resolved = principal_from_bound_session()
    if resolved is None:
        raise MissingRequesterIdentity(
            "MCP OAuth requires an authenticated requester identity in "
            "per_user mode. Direct CLI, TUI, desktop, and cron paths "
            "without a bound gateway principal cannot use a shared "
            "credential as a fallback."
        )
    return McpOAuthScope(mode="per_user", principal=resolved)


def connection_registry_token(server_name: str, scope: McpOAuthScope) -> str:
    """Exact live-registry key. Shared mode preserves the bare server name."""
    if scope.mode == IDENTITY_MODE_SHARED:
        return server_name
    return f"{server_name}{REGISTRY_SEPARATOR}{scope.persistence_key()}"


def schema_cache_entry_key(
    server_name: str,
    scope: Optional[McpOAuthScope] = None,
    *,
    cache_scope: Optional[str] = None,
) -> str:
    """Disk/memory key for list/schema cache entries.

    Unscoped/shared lookups keep the historical server-name key so existing
    caches and tests keep working. In ``per_user``, private (and unknown)
    cache entries are principal-scoped. Explicit ``cacheScope=public`` may
    stay unscoped.
    """
    if scope is None or scope.mode == IDENTITY_MODE_SHARED:
        return server_name
    if (cache_scope or "").lower() == "public":
        return server_name
    return f"{server_name}{REGISTRY_SEPARATOR}{scope.persistence_key()}"
