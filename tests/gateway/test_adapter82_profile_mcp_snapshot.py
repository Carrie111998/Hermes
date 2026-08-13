"""Regression coverage for adapter#82 profile-overlay MCP discovery."""

import asyncio
from types import SimpleNamespace

import pytest


def test_profile_overlay_discovery_precedes_gateway_agent_snapshot(
    tmp_path, monkeypatch
):
    import gateway.run as gateway_run
    from agent import secret_scope
    from hermes_constants import get_hermes_home
    from tests.hermes_cli.test_managed_mcp_profile_scope import (
        _profile_scope,
        _setup_scopes,
    )
    from tools import mcp_tool

    _, profile, _ = _setup_scopes(tmp_path, monkeypatch)
    discovered = {"gbrain": {}}

    def discover():
        assert get_hermes_home().resolve() == profile.resolve()
        discovered.update(mcp_tool._load_mcp_config())
        return ["mcp__gbrain__search"]

    monkeypatch.setattr(mcp_tool, "discover_mcp_tools", discover)
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    with _profile_scope(profile):
        snapshot = gateway_run._create_gateway_agent(
            lambda **_kwargs: dict(discovered["gbrain"])
        )
        factory_calls = []

        def fail_discovery():
            raise RuntimeError("discovery failed")

        monkeypatch.setattr(mcp_tool, "discover_mcp_tools", fail_discovery)
        with pytest.raises(RuntimeError, match="discovery failed"):
            gateway_run._create_gateway_agent(
                lambda **_kwargs: factory_calls.append(True)
            )

    assert snapshot["url"] == "https://jane.gbrain.example/mcp"
    assert factory_calls == []


def test_profile_discovery_replaces_and_shuts_down_stale_empty_tasks(
    tmp_path, monkeypatch
):
    from agent import secret_scope
    from tests.hermes_cli.test_managed_mcp_profile_scope import _profile_scope
    from tools import mcp_tool

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    monkeypatch.setattr(mcp_tool, "_servers", {})
    monkeypatch.setattr(mcp_tool, "_server_connecting", set())
    shutdowns = []

    def stale_task(key, label):
        async def shutdown():
            shutdowns.append(label)

        return SimpleNamespace(
            name="gbrain",
            state_key=key,
            session=None,
            _config={},
            _error=ValueError("gbrain has no 'command' in config"),
            shutdown=shutdown,
        )

    current_home = tmp_path / "profiles" / "jane"
    current_home.mkdir(parents=True)
    shadow_key = (str((tmp_path / "base").resolve()), "gbrain")
    with _profile_scope(current_home):
        current_key = mcp_tool._server_state_key("gbrain")
        mcp_tool._servers.update(
            {
                current_key: stale_task(current_key, "current"),
                shadow_key: stale_task(shadow_key, "shadow"),
            }
        )
        connected = SimpleNamespace(
            name="gbrain",
            state_key=current_key,
            session=object(),
            _registered_tool_names=[],
        )

        async def connect(_name, config):
            assert config == {"command": "gbrain-server"}
            return connected

        monkeypatch.setattr(mcp_tool, "_MCP_AVAILABLE", True)
        monkeypatch.setattr(
            mcp_tool, "_filter_suspicious_mcp_servers", lambda servers: servers
        )
        monkeypatch.setattr(mcp_tool, "_connect_cooldown_active", lambda _name: False)
        monkeypatch.setattr(mcp_tool, "_ensure_mcp_loop", lambda: None)
        monkeypatch.setattr(
            mcp_tool,
            "_run_on_mcp_loop",
            lambda factory, **_kwargs: asyncio.run(factory()),
        )
        monkeypatch.setattr(mcp_tool, "_connect_server", connect)
        monkeypatch.setattr(mcp_tool, "_register_server_tools", lambda *_args: [])
        monkeypatch.setattr(mcp_tool, "_existing_tool_names", lambda: [])
        mcp_tool.register_mcp_servers(
            {"gbrain": {"command": "gbrain-server"}}
        )

    assert mcp_tool._servers[current_key] is connected
    assert shadow_key not in mcp_tool._servers
    assert shutdowns == ["current", "shadow"]
