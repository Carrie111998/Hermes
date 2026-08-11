"""Managed MCP overlays resolve inside the selected shared-serve profile."""

from contextlib import contextmanager
import os
from types import SimpleNamespace

import yaml


def _write_yaml(path, value):
    path.write_text(yaml.safe_dump(value), encoding="utf-8")


def _clear_config_caches():
    from hermes_cli import managed_scope
    from hermes_cli import config

    config._LOAD_CONFIG_CACHE.clear()
    config._RAW_CONFIG_CACHE.clear()
    managed_scope.invalidate_managed_cache()


@contextmanager
def _profile_scope(home):
    from agent.secret_scope import (
        build_profile_secret_scope,
        reset_secret_scope,
        set_secret_scope,
    )
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home_token = set_hermes_home_override(home)
    secret_token = set_secret_scope(build_profile_secret_scope(home))
    try:
        yield
    finally:
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


def _setup_scopes(tmp_path, monkeypatch):
    base = tmp_path / "base"
    profiles = tmp_path / "profiles"
    managed = tmp_path / "managed"
    jane = profiles / "jane"
    louis = profiles / "louis"
    jane_managed = managed / "jane"
    louis_managed = managed / "louis"
    for path in (base, jane, louis, jane_managed, louis_managed):
        path.mkdir(parents=True)

    _write_yaml(
        base / "config.yaml",
        {"mcp_servers": {"base-only": {"url": "https://base.example/mcp"}}},
    )
    _write_yaml(
        jane / "config.yaml",
        {
            "display": {"skin": "jane"},
            "mcp_servers": {
                "jane-local-secret": {
                    "url": "https://jane.local/mcp",
                    "headers": {
                        "Authorization": "Bearer ${PROFILE_MCP_TOKEN}",
                    },
                },
            },
        },
    )
    _write_yaml(
        louis / "config.yaml",
        {
            "mcp_servers": {
                "louis-local": {
                    "url": "https://louis.local/mcp",
                    "headers": {
                        "Authorization": "Bearer ${PROFILE_MCP_TOKEN}",
                    },
                },
            },
        },
    )
    _write_yaml(
        jane_managed / "config.yaml",
        {
            "mcp_servers": {
                "gbrain": {
                    "url": "${GBRAIN_URL}",
                    "headers": {"Authorization": "Bearer ${GBRAIN_TOKEN}"},
                }
            }
        },
    )
    (jane_managed / ".env").write_text(
        "GBRAIN_URL=https://jane.gbrain.example/mcp\n"
        "GBRAIN_TOKEN=jane-managed-token\n",
        encoding="utf-8",
    )
    (jane / ".env").write_text(
        "PROFILE_MCP_TOKEN=jane-profile-token\n",
        encoding="utf-8",
    )
    (louis / ".env").write_text(
        "PROFILE_MCP_TOKEN=louis-profile-token\n",
        encoding="utf-8",
    )
    _write_yaml(louis_managed / "config.yaml", {"display": {"skin": "managed"}})

    monkeypatch.setenv("HERMES_HOME", str(base))
    monkeypatch.setenv("EVAOS_HERMES_MANAGED_PROFILE_ROOT", str(managed))
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    monkeypatch.setenv("GBRAIN_URL", "https://process.example/mcp")
    monkeypatch.setenv("GBRAIN_TOKEN", "process-token")
    monkeypatch.setenv("PROFILE_MCP_TOKEN", "launch-profile-token")
    _clear_config_caches()
    return base, jane, louis


def test_profile_managed_mcp_is_discoverable_with_isolated_env(
    tmp_path, monkeypatch
):
    from hermes_cli import mcp_startup
    from tools.mcp_tool import _load_mcp_config

    _, jane, _ = _setup_scopes(tmp_path, monkeypatch)
    with _profile_scope(jane):
        assert mcp_startup._has_configured_mcp_servers() is True
        servers = _load_mcp_config()

    assert set(servers) == {"gbrain", "jane-local-secret"}
    assert servers["gbrain"]["url"] == "https://jane.gbrain.example/mcp"
    assert servers["gbrain"]["headers"]["Authorization"] == (
        "Bearer jane-managed-token"
    )
    assert servers["jane-local-secret"]["headers"]["Authorization"] == (
        "Bearer jane-profile-token"
    )
    assert os.environ["GBRAIN_URL"] == "https://process.example/mcp"
    assert os.environ["GBRAIN_TOKEN"] == "process-token"


def test_profile_without_managed_mcp_sees_only_profile_config(
    tmp_path, monkeypatch
):
    from hermes_cli import mcp_startup
    from tools.mcp_tool import _load_mcp_config

    _, _, louis = _setup_scopes(tmp_path, monkeypatch)
    with _profile_scope(louis):
        assert mcp_startup._has_configured_mcp_servers() is True
        servers = _load_mcp_config()

    assert set(servers) == {"louis-local"}
    assert servers["louis-local"]["headers"]["Authorization"] == (
        "Bearer louis-profile-token"
    )


def test_base_scope_mcp_config_is_unchanged(tmp_path, monkeypatch):
    from hermes_cli import mcp_startup
    from tools.mcp_tool import _load_mcp_config

    _setup_scopes(tmp_path, monkeypatch)
    assert mcp_startup._has_configured_mcp_servers() is True
    assert set(_load_mcp_config()) == {"base-only"}


def test_multiplex_sessions_discover_managed_mcp_per_profile(
    tmp_path, monkeypatch
):
    """Each session home gets one isolated lazy discovery slot."""
    from agent.secret_scope import (
        current_secret_scope,
        is_multiplex_active,
        set_multiplex_active,
    )
    from hermes_cli import mcp_startup
    from hermes_constants import get_hermes_home
    from tools import mcp_tool
    from tools import registry as registry_mod
    from tools.registry import ToolRegistry
    from tui_gateway import entry

    _, jane, louis = _setup_scopes(tmp_path, monkeypatch)
    empty = tmp_path / "profiles" / "empty"
    empty.mkdir()
    _write_yaml(empty / "config.yaml", {"display": {"skin": "empty"}})
    fresh_registry = ToolRegistry()
    monkeypatch.setattr(registry_mod, "registry", fresh_registry)

    previous_multiplex = is_multiplex_active()
    previous_enabled = entry._mcp_discovery_enabled
    seen = []
    seen_secret_scopes = []
    added_state_keys = []

    def _fake_discover():
        home = str(get_hermes_home().resolve())
        seen.append(home)
        scope = current_secret_scope()
        seen_secret_scopes.append(
            (home, (scope or {}).get("PROFILE_MCP_TOKEN"))
        )
        for server_name in mcp_tool._load_mcp_config():
            tool_name = f"mcp__{server_name}__whoami"
            fresh_registry.register(
                name=tool_name,
                toolset=f"mcp-{server_name}",
                schema={
                    "name": tool_name,
                    "description": "profile identity probe",
                    "parameters": {"type": "object", "properties": {}},
                },
                handler=lambda _args: "ok",
                registration_home_override=home,
            )
            state_key = mcp_tool._server_state_key(server_name, home)
            with mcp_tool._lock:
                mcp_tool._servers[state_key] = SimpleNamespace(
                    session=object(),
                    _registered_tool_names=[tool_name],
                    _sampling=None,
                )
            added_state_keys.append(state_key)

    monkeypatch.setattr(
        mcp_startup,
        "_discover_mcp_tools_without_interactive_oauth",
        _fake_discover,
    )
    monkeypatch.setattr(entry, "_mcp_discovery_enabled", False)
    set_multiplex_active(True)
    mcp_startup._mcp_discovery_started_scopes.clear()
    mcp_startup._mcp_discovery_threads.clear()
    # Model the Dorman topology: base MCP state was published before
    # multiplex activation. It must not count as Jane's discovery state.
    base_state_key = "base-pre-multiplex"
    with mcp_tool._lock:
        mcp_tool._servers[base_state_key] = SimpleNamespace(
            session=object(),
            _registered_tool_names=["mcp__base__probe"],
            _sampling=None,
        )
    added_state_keys.append(base_state_key)

    try:
        with _profile_scope(jane):
            entry.ensure_mcp_discovery_started()
            assert mcp_startup.join_mcp_discovery(timeout=2)
            assert fresh_registry.get_entry("mcp__gbrain__whoami") is not None
            assert fresh_registry.get_entry("mcp__louis-local__whoami") is None

        with _profile_scope(louis):
            assert fresh_registry.get_entry("mcp__gbrain__whoami") is None
            entry.ensure_mcp_discovery_started()
            assert mcp_startup.join_mcp_discovery(timeout=2)
            assert fresh_registry.get_entry("mcp__gbrain__whoami") is None
            assert (
                fresh_registry.get_entry("mcp__louis-local__whoami") is not None
            )

        # A profile with no MCP config never inherits Jane's tools and does
        # not create an empty retrying discovery slot.
        with _profile_scope(empty):
            entry.ensure_mcp_discovery_started()
            mcp_startup.start_background_mcp_discovery(
                logger=SimpleNamespace(
                    debug=lambda *_args, **_kwargs: None,
                    warning=lambda *_args, **_kwargs: None,
                ),
                thread_name="test-empty-profile-mcp",
            )
            assert mcp_startup.join_mcp_discovery(timeout=2)
            assert fresh_registry.get_entry("mcp__gbrain__whoami") is None
            assert str(empty.resolve()) not in (
                mcp_startup._mcp_discovery_started_scopes
            )

        # Opening another session for Jane reuses her completed scope instead
        # of rescanning every profile or starting another discovery thread.
        with _profile_scope(jane):
            entry.ensure_mcp_discovery_started()
            assert mcp_startup.join_mcp_discovery(timeout=2)
            assert fresh_registry.get_entry("mcp__gbrain__whoami") is not None

        assert seen == [str(jane.resolve()), str(louis.resolve())]
        assert seen_secret_scopes == [
            (str(jane.resolve()), "jane-profile-token"),
            (str(louis.resolve()), "louis-profile-token"),
        ]
        assert set(mcp_startup._mcp_discovery_started_scopes) == {
            str(jane.resolve()),
            str(louis.resolve()),
        }
    finally:
        for state_key in added_state_keys:
            with mcp_tool._lock:
                mcp_tool._servers.pop(state_key, None)
        mcp_startup._mcp_discovery_threads.clear()
        mcp_startup._mcp_discovery_started_scopes.clear()
        entry._mcp_discovery_enabled = previous_enabled
        set_multiplex_active(previous_multiplex)


def test_single_profile_discovery_keeps_legacy_process_slot(
    tmp_path, monkeypatch
):
    """Profile-keyed ownership stays dormant outside multiplex mode."""
    from agent.secret_scope import is_multiplex_active, set_multiplex_active
    from hermes_cli import mcp_startup

    _setup_scopes(tmp_path, monkeypatch)
    previous_multiplex = is_multiplex_active()
    seen = []

    monkeypatch.setattr(
        mcp_startup,
        "_discover_mcp_tools_without_interactive_oauth",
        lambda: seen.append("base"),
    )
    monkeypatch.setattr(
        "tools.mcp_tool.get_mcp_status",
        lambda: [{"connected": True}],
    )
    mcp_startup._mcp_discovery_started = False
    mcp_startup._mcp_discovery_thread = None
    set_multiplex_active(False)

    try:
        mcp_startup.start_background_mcp_discovery(
            logger=SimpleNamespace(
                debug=lambda *_args, **_kwargs: None,
                warning=lambda *_args, **_kwargs: None,
            ),
            thread_name="test-single-profile-mcp",
        )
        assert mcp_startup.join_mcp_discovery(timeout=2)
        mcp_startup.start_background_mcp_discovery(
            logger=SimpleNamespace(
                debug=lambda *_args, **_kwargs: None,
                warning=lambda *_args, **_kwargs: None,
            ),
            thread_name="test-single-profile-mcp",
        )

        assert seen == ["base"]
        assert not mcp_startup._mcp_discovery_started_scopes
    finally:
        mcp_startup._mcp_discovery_thread = None
        mcp_startup._mcp_discovery_started = False
        set_multiplex_active(previous_multiplex)
