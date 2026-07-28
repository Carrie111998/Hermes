"""Discovery contract for the opt-in research protocol plugin."""

import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from hermes_cli.plugins import PluginManager


PLUGIN_KEY = "research-protocol"


def _configure_plugins(hermes_home, plugins):
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": plugins}),
        encoding="utf-8",
    )


def _discover(tmp_path, monkeypatch, plugins=None):
    hermes_home = tmp_path / "hermes-home"
    if plugins is not None:
        _configure_plugins(hermes_home, plugins)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("HERMES_BUNDLED_PLUGINS", raising=False)
    manager = PluginManager()
    manager.discover_and_load()
    return manager._plugins[PLUGIN_KEY]


def _assert_no_runtime_registrations(loaded):
    assert loaded.tools_registered == []
    assert loaded.hooks_registered == []
    assert loaded.middleware_registered == []
    assert loaded.commands_registered == []


def test_research_protocol_is_discovered_but_not_enabled_by_default(
    tmp_path, monkeypatch
):
    """The bundled plugin is visible to discovery but remains opt-in."""
    loaded = _discover(tmp_path, monkeypatch)

    assert loaded.manifest.name == PLUGIN_KEY
    assert loaded.manifest.key == PLUGIN_KEY
    assert loaded.manifest.kind == "standalone"
    assert loaded.manifest.version == "0.2.0"
    assert set(loaded.manifest.provides_tools) == {
        "plan_context_read",
        "plan_artifact_write",
        "plan_approval_request",
    }
    assert loaded.enabled is False
    assert loaded.module is None
    assert "not enabled in config" in (loaded.error or "")
    _assert_no_runtime_registrations(loaded)


def test_research_protocol_explicit_enable_registers_exact_pr2_surface(
    tmp_path, monkeypatch
):
    """The exact public key opts into only the three bounded planner tools."""
    loaded = _discover(tmp_path, monkeypatch, {"enabled": [PLUGIN_KEY]})

    assert loaded.enabled is True
    assert loaded.error is None
    assert set(loaded.tools_registered) == {
        "plan_context_read",
        "plan_artifact_write",
        "plan_approval_request",
    }
    assert loaded.hooks_registered == []
    assert loaded.middleware_registered == []
    assert loaded.commands_registered == []


def test_enabled_discovery_without_fcntl_registers_unavailable_tools(tmp_path):
    """Windows-like discovery must load the plugin and fail closed at runtime."""
    hermes_home = tmp_path / "hermes-home"
    _configure_plugins(
        hermes_home,
        {
            "enabled": [PLUGIN_KEY],
            "entries": {
                PLUGIN_KEY: {
                    "artifact_root": str(tmp_path / "artifacts"),
                }
            },
        },
    )
    script = """
import builtins
import importlib

original_import = builtins.__import__
original_import_module = importlib.import_module


def import_without_fcntl(name, *args, **kwargs):
    if name == "fcntl":
        raise ModuleNotFoundError("No module named 'fcntl'")
    return original_import(name, *args, **kwargs)


def import_module_without_fcntl(name, *args, **kwargs):
    if name == "fcntl":
        raise ModuleNotFoundError("No module named 'fcntl'")
    return original_import_module(name, *args, **kwargs)


builtins.__import__ = import_without_fcntl
importlib.import_module = import_module_without_fcntl

from hermes_cli.plugins import PluginManager
from tools.registry import registry

manager = PluginManager()
manager.discover_and_load()
loaded = manager._plugins["research-protocol"]
assert loaded.enabled is True
assert loaded.error is None
assert set(loaded.tools_registered) == {
    "plan_context_read",
    "plan_artifact_write",
    "plan_approval_request",
}
assert all(
    registry.get_entry(name) is not None
    and registry.get_entry(name).check_fn is not None
    and not registry.get_entry(name).check_fn()
    for name in loaded.tools_registered
)
"""
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    env.pop("HERMES_BUNDLED_PLUGINS", None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[3],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("enabled", [None, "", {}, [], PLUGIN_KEY])
def test_research_protocol_malformed_or_empty_enabled_fails_closed(
    tmp_path, monkeypatch, enabled
):
    """Only an explicit list containing the exact plugin key enables code."""
    loaded = _discover(tmp_path, monkeypatch, {"enabled": enabled})

    assert loaded.enabled is False
    assert loaded.module is None
    assert "not enabled in config" in (loaded.error or "")
    _assert_no_runtime_registrations(loaded)


def test_research_protocol_disabled_list_has_priority(tmp_path, monkeypatch):
    """An explicit deny wins even if the same plugin is allowlisted."""
    loaded = _discover(
        tmp_path,
        monkeypatch,
        {"enabled": [PLUGIN_KEY], "disabled": [PLUGIN_KEY]},
    )

    assert loaded.enabled is False
    assert loaded.module is None
    assert "disabled via config" in (loaded.error or "")
    _assert_no_runtime_registrations(loaded)


def test_pr2_register_entry_point_only_touches_tool_registration(
    tmp_path,
    monkeypatch,
):
    """PR2 must not gain hooks, middleware, commands, LLM, or other surfaces."""
    from plugins.research_protocol import register

    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("plugins: {}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    calls = []

    class ToolOnlyContext:
        def register_tool(self, **kwargs):
            calls.append(kwargs)

        def __getattr__(self, name):
            pytest.fail(f"register() accessed plugin context surface {name!r}")

    assert register(ToolOnlyContext()) is None
    assert {call["name"] for call in calls} == {
        "plan_context_read",
        "plan_artifact_write",
        "plan_approval_request",
    }
    assert {call["toolset"] for call in calls} == {"planner"}
    assert all(call["is_async"] is True for call in calls)
    assert all(call["check_fn"]() is False for call in calls)


def test_register_reads_real_plugin_entry_config_without_opening_database(
    tmp_path,
    monkeypatch,
):
    from plugins.research_protocol import register
    from plugins.research_protocol.runtime import (
        READER_DATABASE_URL_ENV,
        WRITER_DATABASE_URL_ENV,
    )

    artifact_root = tmp_path / "artifacts"
    hermes_home = tmp_path / "hermes-home"
    _configure_plugins(
        hermes_home,
        {
            "enabled": [PLUGIN_KEY],
            "entries": {
                PLUGIN_KEY: {
                    "artifact_root": str(artifact_root),
                }
            },
        },
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv(
        READER_DATABASE_URL_ENV,
        "postgresql://reader@localhost/research-reader",
    )
    monkeypatch.setenv(
        WRITER_DATABASE_URL_ENV,
        "postgresql://writer@localhost/research-writer",
    )
    calls = []

    class ToolOnlyContext:
        def register_tool(self, **kwargs):
            calls.append(kwargs)

        def __getattr__(self, name):
            pytest.fail(f"register() accessed plugin context surface {name!r}")

    register(ToolOnlyContext())

    assert artifact_root.is_dir()
    assert {call["name"] for call in calls} == {
        "plan_context_read",
        "plan_artifact_write",
        "plan_approval_request",
    }
    assert all(call["check_fn"]() is True for call in calls)
