"""Tests for per-user MCP OAuth identity (#78174)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip(
    "mcp.client.auth.oauth2",
    reason="MCP SDK 1.26.0+ required for OAuth support",
)


def _write_token(path: Path, access_token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": 3600,
            }
        )
    )


def test_token_storage_isolates_users(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools.mcp_oauth import HermesTokenStorage

    shared = HermesTokenStorage("linear")
    user_a = HermesTokenStorage("linear", user_key="telegram:111")
    user_b = HermesTokenStorage("linear", user_key="telegram:222")

    assert "by-user" not in str(shared._tokens_path())
    assert "by-user" in str(user_a._tokens_path())
    assert "by-user" in str(user_b._tokens_path())
    assert user_a._tokens_path() != user_b._tokens_path()
    assert user_a._tokens_path() != shared._tokens_path()

    _write_token(shared._tokens_path(), "SHARED")
    _write_token(user_a._tokens_path(), "TOKEN_A")
    _write_token(user_b._tokens_path(), "TOKEN_B")

    assert shared.has_cached_tokens()
    assert user_a.has_cached_tokens()
    assert user_b.has_cached_tokens()
    assert json.loads(user_a._tokens_path().read_text())["access_token"] == "TOKEN_A"
    assert json.loads(user_b._tokens_path().read_text())["access_token"] == "TOKEN_B"
    assert json.loads(shared._tokens_path().read_text())["access_token"] == "SHARED"


def test_manager_isolates_providers_by_user_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mock_stdin = MagicMock()
    mock_stdin.isatty.return_value = True
    monkeypatch.setattr("tools.mcp_oauth.sys.stdin", mock_stdin)

    from tools.mcp_oauth import HermesTokenStorage
    from tools.mcp_oauth_manager import MCPOAuthManager, reset_manager_for_tests

    reset_manager_for_tests()
    for key, token in (("telegram:111", "TOKEN_A"), ("telegram:222", "TOKEN_B")):
        storage = HermesTokenStorage("srv", user_key=key)
        _write_token(storage._tokens_path(), token)

    mgr = MCPOAuthManager()
    provider_a = mgr.get_or_build_provider(
        "srv", "https://example.com/mcp", None, user_key="telegram:111",
    )
    provider_b = mgr.get_or_build_provider(
        "srv", "https://example.com/mcp", None, user_key="telegram:222",
    )
    assert provider_a is not None and provider_b is not None
    assert provider_a is not provider_b

    asyncio.run(provider_a._initialize())
    asyncio.run(provider_b._initialize())
    assert provider_a.context.current_tokens.access_token == "TOKEN_A"
    assert provider_b.context.current_tokens.access_token == "TOKEN_B"


def test_current_oauth_user_key_fail_closed(monkeypatch):
    from tools import mcp_oauth_identity as identity

    monkeypatch.setattr(identity, "get_oauth_identity_mode", lambda: "per_user")
    # No force override, no session user.
    monkeypatch.setattr(
        identity,
        "_FORCE_USER_KEY",
        identity._FORCE_USER_KEY,  # keep real ContextVar
    )
    # Ensure no leftover force
    token = identity._FORCE_USER_KEY.set(None)
    try:
        with pytest.raises(identity.MissingMcpOAuthIdentityError):
            identity.current_oauth_user_key(require=True)
        assert identity.current_oauth_user_key(require=False) == ""
    finally:
        identity._FORCE_USER_KEY.reset(token)

    with identity.force_oauth_user_key("discord:99"):
        assert identity.current_oauth_user_key(require=True) == "discord:99"


def test_oauth_connection_registry_key():
    from tools.mcp_oauth_identity import oauth_connection_registry_key

    assert oauth_connection_registry_key("linear", "") == "linear"
    assert oauth_connection_registry_key("linear", "telegram:1") == "linear@@telegram:1"


def test_shared_mode_ignores_session_user(monkeypatch):
    from tools import mcp_oauth_identity as identity

    monkeypatch.setattr(identity, "get_oauth_identity_mode", lambda: "shared")
    monkeypatch.setattr(
        "gateway.session_context.get_session_env",
        lambda name, default="": {"HERMES_SESSION_USER_ID": "999", "HERMES_SESSION_PLATFORM": "telegram"}.get(name, default),
    )
    assert identity.current_oauth_user_key(require=True) == ""
