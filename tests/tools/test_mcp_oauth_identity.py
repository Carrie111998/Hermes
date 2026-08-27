"""Behavior tests for requester-scoped MCP OAuth identity (#78174)."""

from __future__ import annotations

import hashlib
import json

import pytest

from gateway.session_context import (
    get_bound_session_principal,
    reset_session_vars,
    set_session_vars,
)
from tools.mcp_oauth_identity import (
    EMPTY_SCOPE_SENTINEL,
    SHARED_SCOPE,
    InvalidMcpOAuthIdentityModeError,
    McpOAuthPrincipal,
    McpOAuthScope,
    MissingRequesterIdentity,
    configured_identity_mode,
    connection_registry_token,
    is_registry_key_for_server,
    parse_identity_mode,
    principal_from_bound_fields,
    registry_key_prefix,
    resolve_mcp_oauth_scope,
    schema_cache_entry_key,
    server_uses_oauth,
)


def _principal(platform="slack", scope_id="T1", user_id="U1") -> McpOAuthPrincipal:
    return principal_from_bound_fields(platform, scope_id, user_id)


class TestParseIdentityMode:
    def test_shared_and_per_user_accepted(self):
        assert parse_identity_mode("shared") == "shared"
        assert parse_identity_mode("per_user") == "per_user"

    def test_absent_defaults_to_shared(self):
        assert parse_identity_mode(None, explicit=False) == "shared"

    def test_typo_is_rejected_not_downgraded(self):
        with pytest.raises(InvalidMcpOAuthIdentityModeError, match="per-user"):
            parse_identity_mode("per-user")
        with pytest.raises(InvalidMcpOAuthIdentityModeError):
            parse_identity_mode("")
        with pytest.raises(InvalidMcpOAuthIdentityModeError):
            parse_identity_mode("PER_USER")

    def test_config_absent_key_is_shared(self):
        assert configured_identity_mode({}) == "shared"
        assert configured_identity_mode({"mcp": {}}) == "shared"
        assert configured_identity_mode({"mcp": {"oauth": {}}}) == "shared"

    def test_config_explicit_invalid_raises(self):
        with pytest.raises(InvalidMcpOAuthIdentityModeError):
            configured_identity_mode(
                {"mcp": {"oauth": {"identity_mode": "per-user"}}}
            )


class TestCanonicalPrincipal:
    def test_deterministic_digest(self):
        a = _principal()
        b = _principal()
        assert a.canonical_json() == b.canonical_json()
        assert a.persistence_key() == b.persistence_key()
        expected = (
            "u-v1-"
            + hashlib.sha256(a.canonical_json().encode("utf-8")).hexdigest()
        )
        assert a.persistence_key() == expected
        assert json.loads(a.canonical_json()) == ["v1", "slack", "T1", "U1"]

    def test_platform_scope_and_user_are_distinct(self):
        keys = {
            _principal(platform="slack").persistence_key(),
            _principal(platform="discord").persistence_key(),
            _principal(scope_id="T2").persistence_key(),
            _principal(user_id="U2").persistence_key(),
        }
        assert len(keys) == 4

    def test_empty_scope_canonicalizes_to_sentinel(self):
        p = _principal(scope_id="")
        assert p.scope_id == EMPTY_SCOPE_SENTINEL
        assert p.persistence_key() == _principal(scope_id="~").persistence_key()

    def test_path_hostile_ids_are_not_path_components(self):
        p = _principal(user_id="../etc/passwd", scope_id="T/../../x")
        key = p.persistence_key()
        assert "/" not in key
        assert ".." not in key
        assert key.startswith("u-v1-")
        assert len(key) == len("u-v1-") + 64

    def test_long_ids_do_not_lengthen_the_key(self):
        p = _principal(user_id="U" * 5000, scope_id="T" * 5000)
        assert len(p.persistence_key()) == len("u-v1-") + 64

    def test_missing_platform_or_user_rejected(self):
        with pytest.raises(MissingRequesterIdentity):
            principal_from_bound_fields("", "T1", "U1")
        with pytest.raises(MissingRequesterIdentity):
            principal_from_bound_fields("slack", "T1", "")


class TestBoundPrincipalGetter:
    def test_unset_context_is_not_bound(self, monkeypatch):
        reset_session_vars()
        monkeypatch.setenv("HERMES_SESSION_PLATFORM", "slack")
        monkeypatch.setenv("HERMES_SESSION_USER_ID", "UENV")
        monkeypatch.setenv("HERMES_SESSION_SCOPE_ID", "TENV")
        assert get_bound_session_principal() is None

    def test_bound_values_are_returned(self):
        reset_session_vars()
        set_session_vars(platform="slack", scope_id="T9", user_id="U9")
        bound = get_bound_session_principal()
        assert bound is not None
        assert bound.platform == "slack"
        assert bound.scope_id == "T9"
        assert bound.user_id == "U9"
        reset_session_vars()

    def test_empty_bound_user_is_not_a_principal(self):
        reset_session_vars()
        set_session_vars(platform="slack", scope_id="T9", user_id="")
        assert get_bound_session_principal() is None
        reset_session_vars()

    def test_telegram_empty_scope_is_still_bound(self):
        reset_session_vars()
        set_session_vars(platform="telegram", scope_id="", user_id="12345")
        bound = get_bound_session_principal()
        assert bound is not None
        assert bound.scope_id == ""
        assert bound.user_id == "12345"
        reset_session_vars()


class TestResolveScope:
    def test_shared_ignores_bound_principal(self):
        reset_session_vars()
        set_session_vars(platform="slack", scope_id="T1", user_id="U1")
        scope = resolve_mcp_oauth_scope(identity_mode="shared")
        assert scope == SHARED_SCOPE
        reset_session_vars()

    def test_non_oauth_is_always_shared(self):
        reset_session_vars()
        set_session_vars(platform="slack", scope_id="T1", user_id="U1")
        scope = resolve_mcp_oauth_scope(identity_mode="per_user", uses_oauth=False)
        assert scope == SHARED_SCOPE
        reset_session_vars()

    def test_per_user_uses_bound_principal(self):
        reset_session_vars()
        set_session_vars(platform="slack", scope_id="T1", user_id="U1")
        scope = resolve_mcp_oauth_scope(identity_mode="per_user")
        assert scope.mode == "per_user"
        assert scope.principal is not None
        assert scope.principal.user_id == "U1"
        reset_session_vars()

    def test_per_user_without_identity_fails_closed(self):
        reset_session_vars()
        with pytest.raises(MissingRequesterIdentity, match="per_user"):
            resolve_mcp_oauth_scope(identity_mode="per_user")

    def test_resolver_rejects_tool_argument_kwargs(self):
        with pytest.raises(TypeError):
            resolve_mcp_oauth_scope(user_id="U-from-tool")  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            resolve_mcp_oauth_scope("per_user")  # type: ignore[misc]

    def test_explicit_principal_is_not_reread_from_context(self):
        reset_session_vars()
        set_session_vars(platform="slack", scope_id="T-bob", user_id="U-bob")
        alice = _principal(user_id="U-alice")
        scope = resolve_mcp_oauth_scope(
            identity_mode="per_user", principal=alice
        )
        assert scope.principal is not None
        assert scope.principal.user_id == "U-alice"
        reset_session_vars()


class TestRegistryAndCacheKeys:
    def test_shared_registry_token_is_bare_server_name(self):
        assert connection_registry_token("github", SHARED_SCOPE) == "github"

    def test_per_user_tokens_are_exact_and_distinct(self):
        alice = resolve_mcp_oauth_scope(
            identity_mode="per_user", principal=_principal(user_id="U-a")
        )
        bob = resolve_mcp_oauth_scope(
            identity_mode="per_user", principal=_principal(user_id="U-b")
        )
        a_key = connection_registry_token("github", alice)
        b_key = connection_registry_token("github", bob)
        assert a_key != b_key
        assert a_key != "github"
        assert "U-a" not in a_key
        assert "slack" not in a_key
        registry = {a_key: "alice-conn"}
        assert registry.get(b_key) is None
        assert registry.get("github") is None
        assert registry.get(a_key) == "alice-conn"

    def test_same_user_id_different_workspaces_differ(self):
        a = _principal(scope_id="T-a", user_id="U1")
        b = _principal(scope_id="T-b", user_id="U1")
        assert a.persistence_key() != b.persistence_key()

    def test_private_schema_cache_is_scoped(self):
        alice = resolve_mcp_oauth_scope(
            identity_mode="per_user", principal=_principal(user_id="U-a")
        )
        bob = resolve_mcp_oauth_scope(
            identity_mode="per_user", principal=_principal(user_id="U-b")
        )
        a_key = schema_cache_entry_key("github", alice)
        b_key = schema_cache_entry_key("github", bob)
        assert a_key != b_key
        assert schema_cache_entry_key("github", SHARED_SCOPE) == "github"
        assert schema_cache_entry_key("github", alice, cache_scope="public") == "github"

    def test_server_uses_oauth(self):
        assert server_uses_oauth({"auth": "oauth", "url": "https://x"})
        assert not server_uses_oauth({"command": "npx"})
        assert not server_uses_oauth({"auth": "header"})
        assert not server_uses_oauth(None)

    def test_per_user_scope_requires_principal(self):
        with pytest.raises(MissingRequesterIdentity):
            McpOAuthScope(mode="per_user", principal=None)

    def test_shared_scope_strips_principal(self):
        scope = McpOAuthScope(mode="shared", principal=_principal())
        assert scope.principal is None
        assert scope.persistence_key() == "shared"

    def test_registry_key_prefix_match(self):
        alice = resolve_mcp_oauth_scope(
            identity_mode="per_user", principal=_principal(user_id="U-a")
        )
        token = connection_registry_token("github", alice)
        assert token.startswith(registry_key_prefix("github"))
        assert is_registry_key_for_server("github", "github")
        assert is_registry_key_for_server(token, "github")
        assert not is_registry_key_for_server(token, "gitlab")
        assert not is_registry_key_for_server("github-extra", "github")
        assert schema_cache_entry_key("github", alice) == token
