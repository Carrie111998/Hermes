"""MCP OAuth identity resolution for shared vs per-user authorization.

Hermes historically stores one OAuth token set per (profile, MCP server).
Multi-user gateways need a stronger boundary: the authorization used for an
MCP call must belong to the human who initiated the request, not whoever
first completed ``hermes mcp login``.

Config (``mcp.oauth.identity_mode`` in config.yaml):

- ``shared`` (default) — existing behaviour; tokens at
  ``$HERMES_HOME/mcp-tokens/<server>.json``.
- ``per_user`` — tokens at
  ``$HERMES_HOME/mcp-tokens/by-user/<user_key>/<server>.json``.
  The user key is derived from the gateway session
  (``HERMES_SESSION_PLATFORM`` + ``HERMES_SESSION_USER_ID``). When no
  authenticated user identity is available, MCP OAuth calls fail closed
  instead of silently reusing another user's credentials.

CLI operators can target a specific user key with
``hermes mcp login <server> --user <key>`` (sets a force override for the
duration of that login).
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

# Explicit override for CLI ``--user`` / tests. Takes precedence over session.
_FORCE_USER_KEY: ContextVar[Optional[str]] = ContextVar(
    "mcp_oauth_force_user_key", default=None
)

_IDENTITY_MODE_SHARED = "shared"
_IDENTITY_MODE_PER_USER = "per_user"
_VALID_MODES = frozenset({_IDENTITY_MODE_SHARED, _IDENTITY_MODE_PER_USER})


class MissingMcpOAuthIdentityError(RuntimeError):
    """Raised when per-user OAuth mode requires a user identity and none is set."""


def _safe_user_key(raw: str) -> str:
    """Sanitize a user key for filesystem / registry use."""
    cleaned = re.sub(r"[^\w\-.:@]", "_", (raw or "").strip()).strip("._")[:160]
    return cleaned or "anonymous"


def get_oauth_identity_mode() -> str:
    """Return ``shared`` or ``per_user`` from config (default ``shared``)."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        mcp = cfg.get("mcp") or {}
        oauth = mcp.get("oauth") if isinstance(mcp, dict) else {}
        if not isinstance(oauth, dict):
            return _IDENTITY_MODE_SHARED
        mode = str(oauth.get("identity_mode") or _IDENTITY_MODE_SHARED).strip().lower()
        if mode in _VALID_MODES:
            return mode
        logger.warning(
            "Unknown mcp.oauth.identity_mode %r — falling back to shared", mode
        )
    except Exception as exc:  # pragma: no cover — config load failure
        logger.debug("mcp.oauth.identity_mode lookup failed: %s", exc)
    return _IDENTITY_MODE_SHARED


def is_per_user_oauth_identity() -> bool:
    return get_oauth_identity_mode() == _IDENTITY_MODE_PER_USER


@contextmanager
def force_oauth_user_key(user_key: str) -> Iterator[None]:
    """Force a specific OAuth user key for the current context (CLI/tests)."""
    token = _FORCE_USER_KEY.set(_safe_user_key(user_key) if user_key else "")
    try:
        yield
    finally:
        _FORCE_USER_KEY.reset(token)


def current_oauth_user_key(*, require: bool = False) -> str:
    """Resolve the OAuth user key for the current request.

    Returns ``\"\"`` in shared mode (or when an empty force override is set).
    In ``per_user`` mode:

    - Prefer ``force_oauth_user_key`` override.
    - Else derive from gateway session platform + user_id.
    - If neither is available and ``require`` is True, raise
      :class:`MissingMcpOAuthIdentityError`.
    - If ``require`` is False, return ``\"\"`` (caller decides fail-closed).
    """
    forced = _FORCE_USER_KEY.get()
    if forced is not None:
        # Explicit empty force means "shared path for this call" (tests).
        return forced

    if not is_per_user_oauth_identity():
        return ""

    platform = ""
    user_id = ""
    try:
        from gateway.session_context import get_session_env

        platform = (get_session_env("HERMES_SESSION_PLATFORM", "") or "").strip()
        user_id = (get_session_env("HERMES_SESSION_USER_ID", "") or "").strip()
    except Exception as exc:  # pragma: no cover
        logger.debug("session identity lookup failed: %s", exc)

    if user_id:
        raw = f"{platform}:{user_id}" if platform else user_id
        return _safe_user_key(raw)

    if require:
        raise MissingMcpOAuthIdentityError(
            "mcp.oauth.identity_mode is 'per_user' but no authenticated user "
            "identity is available for this request. Hermes will not reuse "
            "another user's MCP OAuth credentials. Bind a gateway session "
            "user (or pass --user to hermes mcp login)."
        )
    return ""


def oauth_connection_registry_key(server_name: str, user_key: str = "") -> str:
    """Build the ``_servers`` dict key for an OAuth-aware connection.

    Shared / empty user_key keeps the historical bare ``server_name`` key so
    existing single-user deployments are unchanged.
    """
    if not user_key:
        return server_name
    return f"{server_name}@@{user_key}"


# Model-supplied tool args must never select which OAuth credentials to use.
# Identity comes only from session ContextVars / force_oauth_user_key.
_CREDENTIAL_SELECTOR_ARG_KEYS = frozenset(
    {
        "user_key",
        "user_id",
        "hermes_user",
        "oauth_user",
        "oauth_user_key",
        "mcp_user",
        "mcp_user_key",
    }
)


def strip_credential_selector_args(args: dict | None) -> dict:
    """Return a copy of *args* without credential-selector keys.

    MCP tool/resource/prompt handlers must call this before using
    arguments so a model cannot request ``user_key=telegram:OTHER``.
    Nested ``arguments`` dicts (e.g. prompts/get) are scrubbed too.
    """
    if not args:
        return {}
    cleaned: dict = {}
    for key, value in args.items():
        if key in _CREDENTIAL_SELECTOR_ARG_KEYS:
            continue
        if key == "arguments" and isinstance(value, dict):
            cleaned[key] = {
                nested_key: nested_value
                for nested_key, nested_value in value.items()
                if nested_key not in _CREDENTIAL_SELECTOR_ARG_KEYS
            }
        else:
            cleaned[key] = value
    return cleaned
