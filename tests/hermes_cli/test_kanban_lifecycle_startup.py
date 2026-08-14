"""Startup isolation contracts for lifecycle-only Kanban workers."""

from __future__ import annotations

from argparse import Namespace
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest


def _lifecycle_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_lifecycle_startup")
    monkeypatch.setenv("HERMES_KANBAN_WORKER_SCOPE", "lifecycle-only")


def _clear_lifecycle_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKER_SCOPE", raising=False)


def _agent_args(command: str = "chat") -> Namespace:
    return Namespace(
        accept_hooks=True,
        command=command,
        cron_command=None,
        gateway_command=None,
        mcp_action="serve" if command == "mcp" else None,
        safe_mode=False,
        tui=False,
        yolo=False,
    )


def _install_startup_spies(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"plugin": 0, "mcp_background": 0, "mcp_inline": 0, "hook": 0}

    plugins = types.ModuleType("hermes_cli.plugins")
    plugins.discover_plugins = lambda: calls.__setitem__(
        "plugin", calls["plugin"] + 1
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)

    mcp_startup = types.ModuleType("hermes_cli.mcp_startup")
    mcp_startup.start_background_mcp_discovery = lambda **_kwargs: calls.__setitem__(
        "mcp_background", calls["mcp_background"] + 1
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.mcp_startup", mcp_startup)

    mcp_tool = types.ModuleType("tools.mcp_tool")
    mcp_tool.discover_mcp_tools = lambda: calls.__setitem__(
        "mcp_inline", calls["mcp_inline"] + 1
    )
    monkeypatch.setitem(sys.modules, "tools.mcp_tool", mcp_tool)

    config = types.ModuleType("hermes_cli.config")
    config.load_config = lambda: {
        "hooks": {"on_session_start": [{"command": "adversarial-hook"}]},
        "mcp_servers": {"adversarial": {"url": "https://invalid.test/mcp"}},
    }
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)

    shell_hooks = types.ModuleType("agent.shell_hooks")
    shell_hooks.register_from_config = lambda *_args, **_kwargs: calls.__setitem__(
        "hook", calls["hook"] + 1
    )
    monkeypatch.setitem(sys.modules, "agent.shell_hooks", shell_hooks)
    return calls


@pytest.mark.parametrize("command", ["chat", "mcp"])
def test_prepare_agent_startup_lifecycle_worker_calls_no_extension_surface(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    from hermes_cli import main as main_mod

    _lifecycle_env(monkeypatch)
    calls = _install_startup_spies(monkeypatch)

    main_mod._prepare_agent_startup(_agent_args(command))

    assert calls == {
        "plugin": 0,
        "mcp_background": 0,
        "mcp_inline": 0,
        "hook": 0,
    }


def test_prepare_agent_startup_normal_worker_preserves_extension_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import main as main_mod

    _clear_lifecycle_env(monkeypatch)
    calls = _install_startup_spies(monkeypatch)

    main_mod._prepare_agent_startup(_agent_args("chat"))
    assert calls == {
        "plugin": 1,
        "mcp_background": 1,
        "mcp_inline": 0,
        "hook": 1,
    }

    main_mod._prepare_agent_startup(_agent_args("mcp"))
    assert calls == {
        "plugin": 2,
        "mcp_background": 1,
        "mcp_inline": 1,
        "hook": 2,
    }


def test_deferred_cli_startup_lifecycle_worker_starts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cli as cli_mod

    _lifecycle_env(monkeypatch)
    monkeypatch.setenv("HERMES_DEFER_AGENT_STARTUP", "1")
    monkeypatch.setattr(cli_mod, "_deferred_agent_startup_done", False)
    calls = _install_startup_spies(monkeypatch)

    cli_mod._prepare_deferred_agent_startup()

    assert cli_mod._deferred_agent_startup_done is True
    assert calls == {
        "plugin": 0,
        "mcp_background": 0,
        "mcp_inline": 0,
        "hook": 0,
    }


def test_model_tools_import_skips_plugin_discovery_only_for_lifecycle_worker(
    tmp_path: Path,
) -> None:
    code = """
import hermes_cli.plugins as plugins
calls = []
plugins.discover_plugins = lambda *args, **kwargs: calls.append('plugin')
import model_tools
print('PLUGIN_DISCOVERY_CALLS=' + str(len(calls)))
"""
    base_env = os.environ.copy()
    base_env["HERMES_HOME"] = str(tmp_path / ".hermes")
    base_env["HERMES_QUIET"] = "1"
    lifecycle_env = dict(base_env)
    lifecycle_env.update(
        {
            "HERMES_KANBAN_TASK": "t_lifecycle_startup",
            "HERMES_KANBAN_WORKER_SCOPE": "lifecycle-only",
        }
    )

    scoped = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        env=lifecycle_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert "PLUGIN_DISCOVERY_CALLS=0" in scoped.stdout

    normal_env = dict(base_env)
    normal_env.pop("HERMES_KANBAN_TASK", None)
    normal_env.pop("HERMES_KANBAN_WORKER_SCOPE", None)
    normal = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        env=normal_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert "PLUGIN_DISCOVERY_CALLS=1" in normal.stdout


def test_lifecycle_worker_blocks_lazy_plugin_mcp_and_hook_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _lifecycle_env(monkeypatch)

    from agent import shell_hooks
    from hermes_cli import mcp_startup
    from hermes_cli.plugins import PluginManager
    from tools import mcp_tool

    plugin_calls: list[str] = []
    manager = PluginManager()
    monkeypatch.setattr(
        manager,
        "_discover_and_load_inner",
        lambda: plugin_calls.append("discovered"),
    )
    manager.discover_and_load(force=True)
    assert plugin_calls == []
    assert manager._discovered is True

    monkeypatch.setattr(mcp_startup, "_mcp_discovery_started", False)
    monkeypatch.setattr(mcp_startup, "_mcp_discovery_thread", None)
    monkeypatch.setattr(
        mcp_startup,
        "_has_configured_mcp_servers",
        lambda: pytest.fail("lifecycle worker must not probe MCP configuration"),
    )
    mcp_startup.start_background_mcp_discovery(
        logger=types.SimpleNamespace(debug=lambda *_args, **_kwargs: None),
        thread_name="lifecycle-worker-mcp",
    )
    assert mcp_startup._mcp_discovery_started is False
    assert mcp_startup._mcp_discovery_thread is None

    monkeypatch.setattr(
        mcp_tool,
        "_load_mcp_config",
        lambda: pytest.fail("lifecycle worker must not read MCP config"),
    )
    assert mcp_tool.discover_mcp_tools() == []
    assert mcp_tool.register_mcp_servers(
        {"adversarial": {"url": "https://invalid.test/mcp"}}
    ) == []

    hook_calls: list[str] = []
    monkeypatch.setattr(
        shell_hooks,
        "_resolve_effective_accept",
        lambda *_args, **_kwargs: hook_calls.append("resolved") or True,
    )
    assert shell_hooks.register_from_config(
        {"hooks": {"on_session_start": [{"command": "adversarial-hook"}]}},
        accept_hooks=True,
    ) == []
    assert hook_calls == []


def test_lazy_extension_guards_preserve_normal_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_lifecycle_env(monkeypatch)

    from agent import shell_hooks
    from hermes_cli.plugins import PluginManager
    from tools import mcp_tool

    plugin_calls: list[str] = []
    manager = PluginManager()
    monkeypatch.setattr(
        manager,
        "_discover_and_load_inner",
        lambda: plugin_calls.append("discovered"),
    )
    manager.discover_and_load()
    assert plugin_calls == ["discovered"]

    mcp_config_calls: list[str] = []
    monkeypatch.setattr(mcp_tool, "_MCP_AVAILABLE", True)
    monkeypatch.setattr(
        mcp_tool,
        "_load_mcp_config",
        lambda: mcp_config_calls.append("loaded") or {},
    )
    assert mcp_tool.discover_mcp_tools() == []
    assert mcp_config_calls == ["loaded"]

    hook_calls: list[str] = []
    monkeypatch.setattr(
        shell_hooks,
        "_resolve_effective_accept",
        lambda *_args, **_kwargs: hook_calls.append("resolved") or False,
    )
    assert shell_hooks.register_from_config({}, accept_hooks=False) == []
    assert hook_calls == ["resolved"]
