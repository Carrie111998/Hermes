"""Requester-scoped MCP OAuth isolation (#78174).

Exercises persistence, manager, live-registry, schema-cache, and CLI
fail-closed behavior against a real temp HERMES_HOME. Bound identity
comes from ``set_session_vars`` — never from tool arguments.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from gateway.session_context import reset_session_vars, set_session_vars
from tools.mcp_oauth_identity import (
    SHARED_SCOPE,
    connection_registry_token,
    principal_from_bound_fields,
    resolve_mcp_oauth_scope,
)


def _alice():
    return principal_from_bound_fields("slack", "T1", "U-alice")


def _bob():
    return principal_from_bound_fields("slack", "T1", "U-bob")


def _alice_scope():
    return resolve_mcp_oauth_scope(identity_mode="per_user", principal=_alice())


def _bob_scope():
    return resolve_mcp_oauth_scope(identity_mode="per_user", principal=_bob())


def _seed_cached_tokens(
    home: Path, server_name: str, oauth_scope, access_token: str = "TOK"
) -> None:
    from tools.mcp_oauth import HermesTokenStorage

    storage = HermesTokenStorage(
        server_name, hermes_home=home, oauth_scope=oauth_scope
    )
    path = storage._tokens_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": 3600,
            }
        ),
        encoding="utf-8",
    )


def _write_per_user_config(home: Path) -> None:
    (home / "config.yaml").write_text(
        "mcp:\n  oauth:\n    identity_mode: per_user\n",
        encoding="utf-8",
    )
    from hermes_cli.config import load_config

    loaded = load_config()
    assert loaded.get("mcp", {}).get("oauth", {}).get("identity_mode") == "per_user"


def _bind(platform: str, scope_id: str, user_id: str) -> None:
    reset_session_vars()
    set_session_vars(platform=platform, scope_id=scope_id, user_id=user_id)


@pytest.fixture(autouse=True)
def _reset_bound_principal():
    reset_session_vars()
    yield
    reset_session_vars()


class TestPerUserTokenLayout:
    def test_alice_and_bob_get_distinct_paths(self, tmp_path):
        from tools.mcp_oauth import HermesTokenStorage

        alice = HermesTokenStorage(
            "github", hermes_home=tmp_path, oauth_scope=_alice_scope()
        )
        bob = HermesTokenStorage(
            "github", hermes_home=tmp_path, oauth_scope=_bob_scope()
        )
        assert alice._tokens_path() != bob._tokens_path()
        assert alice._tokens_path().parent != bob._tokens_path().parent
        assert "by-user" in str(alice._tokens_path())
        assert alice._tokens_path().name == "github.json"
        assert bob._tokens_path().name == "github.json"
        assert _alice().persistence_key() in str(alice._tokens_path())
        assert _bob().persistence_key() not in str(alice._tokens_path())

    def test_shared_layout_is_unchanged(self, tmp_path):
        from tools.mcp_oauth import HermesTokenStorage

        shared = HermesTokenStorage(
            "github", hermes_home=tmp_path, oauth_scope=SHARED_SCOPE
        )
        assert shared._tokens_path() == tmp_path / "mcp-tokens" / "github.json"
        assert "by-user" not in str(shared._tokens_path())

    def test_shared_token_is_not_assigned_to_a_requester(self, tmp_path):
        from tools.mcp_oauth import HermesTokenStorage

        shared = HermesTokenStorage(
            "github", hermes_home=tmp_path, oauth_scope=SHARED_SCOPE
        )
        shared._tokens_path().parent.mkdir(parents=True, exist_ok=True)
        shared._tokens_path().write_text(
            '{"access_token":"SHARED","token_type":"Bearer"}', encoding="utf-8"
        )
        alice = HermesTokenStorage(
            "github", hermes_home=tmp_path, oauth_scope=_alice_scope()
        )
        assert alice._tokens_path() != shared._tokens_path()
        assert not alice._tokens_path().exists()
        assert not alice.has_cached_tokens()

    def test_ids_never_appear_as_path_components(self, tmp_path):
        from tools.mcp_oauth import HermesTokenStorage

        hostile = principal_from_bound_fields(
            "slack", "../etc", "U/../../passwd"
        )
        scope = resolve_mcp_oauth_scope(identity_mode="per_user", principal=hostile)
        storage = HermesTokenStorage(
            "github", hermes_home=tmp_path, oauth_scope=scope
        )
        path = str(storage._tokens_path())
        assert "../etc" not in path
        assert "passwd" not in path
        assert "U/" not in path


class TestPerUserRemove:
    def test_alice_logout_does_not_wipe_bob(self, tmp_path):
        from tools.mcp_oauth import HermesTokenStorage, remove_oauth_tokens

        alice = HermesTokenStorage(
            "github", hermes_home=tmp_path, oauth_scope=_alice_scope()
        )
        bob = HermesTokenStorage(
            "github", hermes_home=tmp_path, oauth_scope=_bob_scope()
        )
        for storage, token in ((alice, "ALICE"), (bob, "BOB")):
            storage._tokens_path().parent.mkdir(parents=True, exist_ok=True)
            storage._tokens_path().write_text(
                json.dumps({"access_token": token, "token_type": "Bearer"}),
                encoding="utf-8",
            )
        remove_oauth_tokens(
            "github", hermes_home=tmp_path, oauth_scope=_alice_scope()
        )
        assert not alice._tokens_path().exists()
        assert bob._tokens_path().exists()
        assert "BOB" in bob._tokens_path().read_text(encoding="utf-8")

    def test_all_identities_removes_shared_and_by_user(self, tmp_path):
        from tools.mcp_oauth import HermesTokenStorage, remove_oauth_tokens

        shared = HermesTokenStorage(
            "github", hermes_home=tmp_path, oauth_scope=SHARED_SCOPE
        )
        alice = HermesTokenStorage(
            "github", hermes_home=tmp_path, oauth_scope=_alice_scope()
        )
        bob = HermesTokenStorage(
            "github", hermes_home=tmp_path, oauth_scope=_bob_scope()
        )
        for storage in (shared, alice, bob):
            storage._tokens_path().parent.mkdir(parents=True, exist_ok=True)
            storage._tokens_path().write_text("{}", encoding="utf-8")
        remove_oauth_tokens("github", hermes_home=tmp_path, all_identities=True)
        assert not shared._tokens_path().exists()
        assert not alice._tokens_path().exists()
        assert not bob._tokens_path().exists()


class TestPerUserManager:
    def test_alice_and_bob_get_distinct_providers(self, tmp_path, monkeypatch):
        pytest.importorskip("mcp.client.auth.oauth2")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from tools.mcp_oauth_manager import MCPOAuthManager

        _seed_cached_tokens(tmp_path, "github", _alice_scope(), "ALICE")
        _seed_cached_tokens(tmp_path, "github", _bob_scope(), "BOB")
        manager = MCPOAuthManager()
        alice = manager.get_or_build_provider(
            "github",
            "https://mcp.example/mcp",
            {},
            oauth_scope=_alice_scope(),
            hermes_home=tmp_path,
        )
        bob = manager.get_or_build_provider(
            "github",
            "https://mcp.example/mcp",
            {},
            oauth_scope=_bob_scope(),
            hermes_home=tmp_path,
        )
        assert alice is not None and bob is not None
        assert alice is not bob
        assert manager._key(
            "github", tmp_path, oauth_scope=_alice_scope()
        ) != manager._key("github", tmp_path, oauth_scope=_bob_scope())

    def test_key_without_scope_stays_shared(self, tmp_path):
        from tools.mcp_oauth_manager import MCPOAuthManager

        manager = MCPOAuthManager()
        assert manager._key("github", tmp_path) == manager._key(
            "github", tmp_path, oauth_scope=SHARED_SCOPE
        )
        assert manager._key("github", tmp_path) != manager._key(
            "github", tmp_path, oauth_scope=_alice_scope()
        )

    def test_alice_remove_does_not_evict_bob(self, tmp_path, monkeypatch):
        pytest.importorskip("mcp.client.auth.oauth2")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from tools.mcp_oauth_manager import MCPOAuthManager

        _seed_cached_tokens(tmp_path, "github", _alice_scope(), "ALICE")
        _seed_cached_tokens(tmp_path, "github", _bob_scope(), "BOB")
        manager = MCPOAuthManager()
        alice = manager.get_or_build_provider(
            "github",
            "https://mcp.example/mcp",
            {},
            oauth_scope=_alice_scope(),
            hermes_home=tmp_path,
        )
        bob = manager.get_or_build_provider(
            "github",
            "https://mcp.example/mcp",
            {},
            oauth_scope=_bob_scope(),
            hermes_home=tmp_path,
        )
        manager.remove(
            "github", hermes_home=tmp_path, oauth_scope=_alice_scope()
        )
        still_bob = manager.get_or_build_provider(
            "github",
            "https://mcp.example/mcp",
            {},
            oauth_scope=_bob_scope(),
            hermes_home=tmp_path,
        )
        assert still_bob is bob
        _seed_cached_tokens(tmp_path, "github", _alice_scope(), "ALICE2")
        rebuilt_alice = manager.get_or_build_provider(
            "github",
            "https://mcp.example/mcp",
            {},
            oauth_scope=_alice_scope(),
            hermes_home=tmp_path,
        )
        assert rebuilt_alice is not alice


class TestPerUserSchemaCache:
    def test_private_cache_is_requester_scoped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from tools.mcp_schema_cache import get_cached_entry, write_cache_entry

        write_cache_entry(
            "github",
            "fp-alice",
            tools=[{"name": "alice_only", "description": "", "inputSchema": {}}],
            cache_scope="private",
            oauth_scope=_alice_scope(),
        )
        assert get_cached_entry(
            "github",
            "fp-alice",
            cache_scope="private",
            oauth_scope=_alice_scope(),
        ) is not None
        assert get_cached_entry(
            "github",
            "fp-alice",
            cache_scope="private",
            oauth_scope=_bob_scope(),
        ) is None
        assert get_cached_entry("github", "fp-alice") is None

    def test_public_cache_may_stay_unscoped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from tools.mcp_schema_cache import get_cached_entry, write_cache_entry

        write_cache_entry(
            "github",
            "fp-public",
            tools=[{"name": "shared_tool", "description": "", "inputSchema": {}}],
            cache_scope="public",
            oauth_scope=_alice_scope(),
        )
        assert get_cached_entry(
            "github",
            "fp-public",
            cache_scope="public",
            oauth_scope=_bob_scope(),
        ) is not None
        assert get_cached_entry(
            "github", "fp-public", cache_scope="public"
        ) is not None


class TestPerUserRuntimeRegistry:
    def test_non_oauth_stays_on_bare_name_in_per_user(self):
        from hermes_constants import get_hermes_home
        import tools.mcp_tool as mcp

        _write_per_user_config(get_hermes_home())
        server = type(
            "Srv",
            (),
            {
                "name": "filesystem",
                "session": object(),
                "_oauth_scope": SHARED_SCOPE,
                "_registry_key": "filesystem",
                "_is_recycled_stdio": lambda self: False,
            },
        )()
        mcp._servers["filesystem"] = server
        mcp._oauth_protected_servers.discard("filesystem")
        try:
            _bind("slack", "T1", "U-alice")
            assert mcp._get_connected_server_for_call("filesystem") is server
        finally:
            mcp._servers.pop("filesystem", None)

    def test_exact_lookup_does_not_cross_principals(self, tmp_path, monkeypatch):
        from hermes_constants import get_hermes_home
        import tools.mcp_tool as mcp

        home = get_hermes_home()
        _write_per_user_config(home)
        mcp._oauth_protected_servers.add("github")
        alice_key = connection_registry_token("github", _alice_scope())
        bob_key = connection_registry_token("github", _bob_scope())
        alice_server = type("Srv", (), {"name": "github", "session": object(), "_oauth_scope": _alice_scope(), "_registry_key": alice_key, "_is_recycled_stdio": lambda self: False})()
        bob_server = type("Srv", (), {"name": "github", "session": object(), "_oauth_scope": _bob_scope(), "_registry_key": bob_key, "_is_recycled_stdio": lambda self: False})()
        mcp._servers[alice_key] = alice_server
        mcp._servers[bob_key] = bob_server
        try:
            _bind("slack", "T1", "U-alice")
            assert mcp._get_connected_server_for_call("github") is alice_server
            _bind("slack", "T1", "U-bob")
            assert mcp._get_connected_server_for_call("github") is bob_server
            assert mcp._servers.get("github") is None
        finally:
            mcp._servers.pop(alice_key, None)
            mcp._servers.pop(bob_key, None)
            mcp._oauth_protected_servers.discard("github")

    def test_missing_identity_does_not_pick_any_connection(self, tmp_path, monkeypatch):
        from hermes_constants import get_hermes_home
        import tools.mcp_tool as mcp

        home = get_hermes_home()
        _write_per_user_config(home)
        mcp._oauth_protected_servers.add("github")
        alice_key = connection_registry_token("github", _alice_scope())
        alice_server = type("Srv", (), {"name": "github", "session": object(), "_oauth_scope": _alice_scope(), "_registry_key": alice_key, "_is_recycled_stdio": lambda self: False})()
        mcp._servers[alice_key] = alice_server
        try:
            reset_session_vars()
            assert mcp._get_connected_server_for_call("github") is None
            err = mcp._oauth_call_target("github")[1]
            assert err is not None
            assert "per_user" in err
        finally:
            mcp._servers.pop(alice_key, None)
            mcp._oauth_protected_servers.discard("github")

    def test_lazy_config_is_not_popped_in_per_user(self, tmp_path):
        from hermes_constants import get_hermes_home
        import tools.mcp_tool as mcp

        home = get_hermes_home()
        _write_per_user_config(home)
        mcp._lazy_server_configs["github"] = {"auth": "oauth", "url": "https://mcp.example"}
        mcp._lazy_server_fingerprints["github"] = "fp"
        mcp._lazy_server_tool_names["github"] = ["github_search"]
        try:
            fp, names = mcp._maybe_pop_lazy("github")
            assert fp == "fp"
            assert names == ["github_search"]
            assert "github" in mcp._lazy_server_configs
            assert mcp._lazy_server_tool_names["github"] == ["github_search"]
        finally:
            mcp._lazy_server_configs.pop("github", None)
            mcp._lazy_server_fingerprints.pop("github", None)
            mcp._lazy_server_tool_names.pop("github", None)

    def test_circuit_breaker_is_isolated(self):
        import tools.mcp_tool as mcp

        alice_key = connection_registry_token("github", _alice_scope())
        bob_key = connection_registry_token("github", _bob_scope())
        mcp._server_error_counts.pop(alice_key, None)
        mcp._server_error_counts.pop(bob_key, None)
        mcp._server_breaker_opened_at.pop(alice_key, None)
        mcp._server_breaker_opened_at.pop(bob_key, None)
        try:
            for _ in range(mcp._CIRCUIT_BREAKER_THRESHOLD):
                mcp._bump_server_error(alice_key)
            assert mcp._server_error_counts[alice_key] >= mcp._CIRCUIT_BREAKER_THRESHOLD
            assert mcp._server_error_counts.get(bob_key, 0) == 0
            mcp._reset_server_error(alice_key)
            assert mcp._server_error_counts[alice_key] == 0
        finally:
            mcp._server_error_counts.pop(alice_key, None)
            mcp._server_error_counts.pop(bob_key, None)
            mcp._server_breaker_opened_at.pop(alice_key, None)
            mcp._server_breaker_opened_at.pop(bob_key, None)

    def test_reconnect_is_exact_key_only(self):
        import tools.mcp_tool as mcp

        alice_key = connection_registry_token("github", _alice_scope())
        bob_key = connection_registry_token("github", _bob_scope())

        class _Srv:
            def __init__(self, key):
                self.name = "github"
                self._registry_key = key
                self.signaled = False
                self._reconnect_event = type("E", (), {"set": lambda inner: None})()

        alice_server = _Srv(alice_key)
        bob_server = _Srv(bob_key)
        mcp._servers[alice_key] = alice_server
        mcp._servers[bob_key] = bob_server
        mcp._oauth_protected_servers.add("github")
        from hermes_constants import get_hermes_home

        _write_per_user_config(get_hermes_home())
        try:
            _bind("slack", "T1", "U-alice")
            signaled = []

            def _signal(server):
                signaled.append(server)
                return True

            original = mcp._signal_reconnect
            mcp._signal_reconnect = _signal
            try:
                assert mcp.reconnect_mcp_server("github") is True
                assert signaled == [alice_server]
            finally:
                mcp._signal_reconnect = original
        finally:
            mcp._servers.pop(alice_key, None)
            mcp._servers.pop(bob_key, None)
            mcp._oauth_protected_servers.discard("github")


class TestPerUserCliGuard:
    def test_cli_blocks_without_bound_principal(self):
        from hermes_constants import get_hermes_home
        from hermes_cli.mcp_config import _per_user_oauth_cli_block

        _write_per_user_config(get_hermes_home())
        reset_session_vars()
        msg = _per_user_oauth_cli_block()
        assert msg is not None
        assert "per_user" in msg

    def test_cli_allows_bound_gateway_principal(self):
        from hermes_constants import get_hermes_home
        from hermes_cli.mcp_config import _per_user_oauth_cli_block

        _write_per_user_config(get_hermes_home())
        _bind("slack", "T1", "U-alice")
        assert _per_user_oauth_cli_block() is None

    def test_shared_mode_does_not_block_cli(self):
        from hermes_cli.mcp_config import _per_user_oauth_cli_block

        reset_session_vars()
        # Default config is shared.
        assert _per_user_oauth_cli_block() is None


def _github_oauth_cfg():
    return {"auth": "oauth", "url": "https://mcp.example/mcp"}


def _cached_search_tools():
    return [{"name": "search", "description": "s", "inputSchema": {"type": "object"}}]


def _cleanup_github_runtime(mcp) -> None:
    from tools.mcp_oauth_identity import (
        connection_registry_token,
        is_registry_key_for_server,
    )
    from tools.mcp_tool import mcp_prefixed_tool_name
    from tools.registry import registry

    for name in list(mcp._lazy_server_tool_names.get("github") or []):
        registry.deregister(name)
    registry.deregister(mcp_prefixed_tool_name("github", "search"))
    for key in list(mcp._servers):
        if is_registry_key_for_server(key, "github"):
            mcp._servers.pop(key, None)
    mcp._lazy_server_configs.pop("github", None)
    mcp._lazy_server_fingerprints.pop("github", None)
    mcp._lazy_server_tool_names.pop("github", None)
    mcp._oauth_protected_servers.discard("github")
    mcp._server_connecting.discard("github")
    alice_key = connection_registry_token("github", _alice_scope())
    bob_key = connection_registry_token("github", _bob_scope())
    mcp._server_connecting.discard(alice_key)
    mcp._server_connecting.discard(bob_key)


class TestScopedCachePublicationOnRegister:
    def test_unbound_startup_publishes_requester_scoped_cache(self):
        from hermes_constants import get_hermes_home
        from tools.mcp_schema_cache import config_fingerprint, write_cache_entry
        from tools.mcp_tool import mcp_prefixed_tool_name
        import tools.mcp_tool as mcp

        home = get_hermes_home()
        _write_per_user_config(home)
        cfg = _github_oauth_cfg()
        write_cache_entry(
            "github",
            config_fingerprint(cfg),
            tools=_cached_search_tools(),
            oauth_scope=_alice_scope(),
        )
        reset_session_vars()
        try:
            with patch.object(mcp, "_ensure_mcp_sdk", return_value=True), patch(
                "tools.mcp_tool._run_on_mcp_loop"
            ) as mock_run:
                names = mcp.register_mcp_servers({"github": cfg})
            mock_run.assert_not_called()
            tool_name = mcp_prefixed_tool_name("github", "search")
            assert tool_name in (mcp._lazy_server_tool_names.get("github") or [])
            assert tool_name in names
        finally:
            _cleanup_github_runtime(mcp)

    def test_bound_register_loads_scoped_cache_after_empty_stash(self):
        from hermes_constants import get_hermes_home
        from tools.mcp_schema_cache import config_fingerprint, write_cache_entry
        from tools.mcp_tool import mcp_prefixed_tool_name
        import tools.mcp_tool as mcp

        home = get_hermes_home()
        _write_per_user_config(home)
        cfg = _github_oauth_cfg()
        reset_session_vars()
        try:
            with patch.object(mcp, "_ensure_mcp_sdk", return_value=True), patch(
                "tools.mcp_tool._run_on_mcp_loop"
            ) as mock_run:
                mcp.register_mcp_servers({"github": cfg})
            mock_run.assert_not_called()
            assert "github" in mcp._lazy_server_configs
            assert not mcp._lazy_server_tool_names.get("github")

            write_cache_entry(
                "github",
                config_fingerprint(cfg),
                tools=_cached_search_tools(),
                oauth_scope=_alice_scope(),
            )
            _bind("slack", "T1", "U-alice")
            with patch.object(mcp, "_ensure_mcp_sdk", return_value=True), patch(
                "tools.mcp_tool._run_on_mcp_loop"
            ) as mock_run:
                names = mcp.register_mcp_servers({"github": cfg})
            mock_run.assert_not_called()
            tool_name = mcp_prefixed_tool_name("github", "search")
            assert tool_name in (mcp._lazy_server_tool_names.get("github") or [])
            assert tool_name in names
        finally:
            _cleanup_github_runtime(mcp)


class TestDeregisterKeepsSiblingTools:
    def test_sibling_connection_keeps_shared_tool_name(self):
        import tools.mcp_tool as mcp
        from tools.registry import registry
        from tools.mcp_tool import mcp_prefixed_tool_name

        tool_name = mcp_prefixed_tool_name("github", "search")
        alice_key = connection_registry_token("github", _alice_scope())
        bob_key = connection_registry_token("github", _bob_scope())
        schema = {
            "name": tool_name,
            "description": "search",
            "parameters": {"type": "object", "properties": {}},
        }
        registry.register(
            name=tool_name,
            toolset="mcp-github",
            schema=schema,
            handler=lambda args, **kw: "{}",
        )
        alice = mcp.MCPServerTask("github")
        alice._registered_tool_names = [tool_name]
        alice._registry_key = alice_key
        bob = mcp.MCPServerTask("github")
        bob._registered_tool_names = [tool_name]
        bob._registry_key = bob_key
        mcp._servers[alice_key] = alice
        mcp._servers[bob_key] = bob
        try:
            alice._deregister_tools()
            assert registry.get_toolset_for_tool(tool_name) == "mcp-github"
            assert alice._registered_tool_names == []
            assert bob._registered_tool_names == [tool_name]
        finally:
            mcp._servers.pop(alice_key, None)
            mcp._servers.pop(bob_key, None)
            registry.deregister(tool_name)

    def test_cache_backed_names_survive_last_connection(self):
        import tools.mcp_tool as mcp
        from tools.registry import registry
        from tools.mcp_tool import mcp_prefixed_tool_name

        tool_name = mcp_prefixed_tool_name("github", "search")
        alice_key = connection_registry_token("github", _alice_scope())
        schema = {
            "name": tool_name,
            "description": "search",
            "parameters": {"type": "object", "properties": {}},
        }
        registry.register(
            name=tool_name,
            toolset="mcp-github",
            schema=schema,
            handler=lambda args, **kw: "{}",
        )
        alice = mcp.MCPServerTask("github")
        alice._registered_tool_names = [tool_name]
        alice._registry_key = alice_key
        mcp._servers[alice_key] = alice
        mcp._lazy_server_tool_names["github"] = [tool_name]
        try:
            alice._deregister_tools()
            assert registry.get_toolset_for_tool(tool_name) == "mcp-github"
        finally:
            mcp._servers.pop(alice_key, None)
            mcp._lazy_server_tool_names.pop("github", None)
            registry.deregister(tool_name)

    def test_last_connection_without_cache_deregisters(self):
        import tools.mcp_tool as mcp
        from tools.registry import registry
        from tools.mcp_tool import mcp_prefixed_tool_name

        tool_name = mcp_prefixed_tool_name("github", "search")
        alice_key = connection_registry_token("github", _alice_scope())
        schema = {
            "name": tool_name,
            "description": "search",
            "parameters": {"type": "object", "properties": {}},
        }
        registry.register(
            name=tool_name,
            toolset="mcp-github",
            schema=schema,
            handler=lambda args, **kw: "{}",
        )
        alice = mcp.MCPServerTask("github")
        alice._registered_tool_names = [tool_name]
        alice._registry_key = alice_key
        mcp._servers[alice_key] = alice
        mcp._lazy_server_tool_names.pop("github", None)
        try:
            alice._deregister_tools()
            assert registry.get_toolset_for_tool(tool_name) is None
        finally:
            mcp._servers.pop(alice_key, None)
            registry.deregister(tool_name)


class TestOAuthClassificationReload:
    def test_auth_change_discards_oauth_flag_and_refreshes_lazy_template(self):
        import tools.mcp_tool as mcp
        from tools.mcp_oauth_identity import server_uses_oauth

        mcp._oauth_protected_servers.add("github")
        mcp._lazy_server_configs["github"] = _github_oauth_cfg()
        mcp._lazy_server_tool_names["github"] = ["mcp__github__search"]
        header_cfg = {"url": "https://mcp.example/mcp", "headers": {"Authorization": "Bearer x"}}
        try:
            with patch.object(mcp, "_ensure_mcp_sdk", return_value=True), patch(
                "tools.mcp_tool._run_on_mcp_loop"
            ) as mock_run:
                mcp.register_mcp_servers({"github": header_cfg})
            mock_run.assert_not_called()
            assert "github" not in mcp._oauth_protected_servers
            assert not server_uses_oauth(mcp._lazy_server_configs["github"])
            assert not mcp._mcp_server_uses_oauth("github")
        finally:
            _cleanup_github_runtime(mcp)

    def test_partial_register_does_not_clear_other_oauth_servers(self):
        import tools.mcp_tool as mcp

        mcp._oauth_protected_servers.add("github")
        mcp._oauth_protected_servers.add("other")
        mcp._lazy_server_configs["filesystem"] = {"command": "npx", "args": []}
        mcp._lazy_server_tool_names["filesystem"] = ["mcp__filesystem__ls"]
        try:
            with patch.object(mcp, "_ensure_mcp_sdk", return_value=True):
                mcp.register_mcp_servers(
                    {"filesystem": {"command": "npx", "args": []}}
                )
            assert "github" in mcp._oauth_protected_servers
            assert "other" in mcp._oauth_protected_servers
        finally:
            mcp._oauth_protected_servers.discard("github")
            mcp._oauth_protected_servers.discard("other")
            mcp._lazy_server_configs.pop("filesystem", None)
            mcp._lazy_server_tool_names.pop("filesystem", None)

    def test_shutdown_clears_oauth_protected_servers(self):
        import tools.mcp_tool as mcp

        mcp._oauth_protected_servers.add("github")
        try:
            mcp.shutdown_mcp_servers()
            assert not mcp._oauth_protected_servers
        finally:
            mcp._oauth_protected_servers.discard("github")
