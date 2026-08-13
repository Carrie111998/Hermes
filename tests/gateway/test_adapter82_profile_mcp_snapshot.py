"""Regression coverage for adapter#82 profile-overlay MCP discovery."""

import inspect
from types import SimpleNamespace

import yaml


def _write_yaml(path, value):
    path.write_text(yaml.safe_dump(value), encoding="utf-8")


def test_profile_overlay_discovery_precedes_gateway_agent_snapshot(
    tmp_path, monkeypatch
):
    import gateway.run as gateway_run
    from agent import secret_scope
    from hermes_cli import config, managed_scope
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )
    from tools import mcp_tool

    base = tmp_path / "base"
    profile = tmp_path / "profiles" / "jane"
    managed = tmp_path / "managed" / "jane"
    for path in (base, profile, managed):
        path.mkdir(parents=True)
    _write_yaml(base / "config.yaml", {"mcp_servers": {"gbrain": {}}})
    _write_yaml(profile / "config.yaml", {"mcp_servers": {"gbrain": {}}})
    _write_yaml(
        managed / "config.yaml",
        {"mcp_servers": {"gbrain": {"command": "gbrain-server"}}},
    )
    monkeypatch.setenv("HERMES_HOME", str(base))
    monkeypatch.setenv(
        "EVAOS_HERMES_MANAGED_PROFILE_ROOT", str(tmp_path / "managed")
    )
    config._LOAD_CONFIG_CACHE.clear()
    config._RAW_CONFIG_CACHE.clear()
    managed_scope.invalidate_managed_cache()

    discovered = {"gbrain": {}}
    seen_homes = []

    def fake_discover():
        from hermes_constants import get_hermes_home

        seen_homes.append(str(get_hermes_home().resolve()))
        discovered.update(mcp_tool._load_mcp_config())
        return ["mcp__gbrain__search"]

    monkeypatch.setattr(mcp_tool, "discover_mcp_tools", fake_discover)
    previous_multiplex = secret_scope.is_multiplex_active()
    secret_scope.set_multiplex_active(True)
    token = set_hermes_home_override(str(profile))
    try:
        gateway_run._prepare_mcp_registry_for_gateway_agent()
        agent_snapshot = dict(discovered["gbrain"])
    finally:
        reset_hermes_home_override(token)
        secret_scope.set_multiplex_active(previous_multiplex)

    assert seen_homes == [str(profile.resolve())]
    assert agent_snapshot["command"] == "gbrain-server"
    source = inspect.getsource(gateway_run.TurnRunner.run_sync)
    assert source.index("_prepare_mcp_registry_for_gateway_agent()") < source.index(
        "agent = ctx.AIAgent("
    )


def test_profile_discovery_refreshes_or_discards_stale_empty_tasks(
    tmp_path, monkeypatch
):
    from agent import secret_scope
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )
    from tools import mcp_tool

    previous_multiplex = secret_scope.is_multiplex_active()
    secret_scope.set_multiplex_active(True)
    current_home = tmp_path / "profiles" / "jane"
    shadow_home = tmp_path / "base"
    current_home.mkdir(parents=True)
    shadow_home.mkdir()
    token = set_hermes_home_override(str(current_home))
    saved_servers = dict(mcp_tool._servers)
    try:
        current_stale = mcp_tool.MCPServerTask("gbrain")
        current_stale._config = {}
        current_stale._error = ValueError("gbrain has no 'command' in config")
        shadow_stale = SimpleNamespace(
            name="gbrain",
            state_key=(str(shadow_home.resolve()), "gbrain"),
            session=None,
            _config={},
            _error=ValueError("gbrain has no 'command' in config"),
            shutdown=lambda: None,
        )
        with mcp_tool._lock:
            mcp_tool._servers.clear()
            mcp_tool._servers[current_stale.state_key] = current_stale
            mcp_tool._servers[shadow_stale.state_key] = shadow_stale

        connected = SimpleNamespace(
            name="gbrain",
            state_key=current_stale.state_key,
        )
        replaced = mcp_tool._pop_stale_empty_server_task(
            current_stale.state_key
        )
        assert replaced is current_stale
        with mcp_tool._lock:
            mcp_tool._servers[current_stale.state_key] = connected
        discarded = mcp_tool._discard_shadowed_empty_server_tasks(connected)
        assert discarded == [shadow_stale]
        assert shadow_stale.state_key not in mcp_tool._servers
    finally:
        with mcp_tool._lock:
            mcp_tool._servers.clear()
            mcp_tool._servers.update(saved_servers)
        reset_hermes_home_override(token)
        secret_scope.set_multiplex_active(previous_multiplex)
