"""Startup isolation contracts for lifecycle-only Kanban workers."""

from __future__ import annotations

from argparse import Namespace
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest


def _lifecycle_env(
    monkeypatch: pytest.MonkeyPatch,
    scope: str = "lifecycle-only",
) -> None:
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_lifecycle_startup")
    monkeypatch.setenv("HERMES_KANBAN_WORKER_SCOPE", scope)


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


def test_profile_dotenv_cannot_override_dispatcher_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from gateway.session_context import _VAR_MAP
    from hermes_cli import env_loader

    home = tmp_path / ".hermes"
    home.mkdir()
    foreign_values = [
        "HERMES_KANBAN_WORKER_SCOPE=",
        "HERMES_KANBAN_TASK=foreign-task",
        "HERMES_KANBAN_RUN_ID=foreign-run",
        "HERMES_KANBAN_WORKSPACE=/foreign/workspace",
        "HERMES_KANBAN_BOARD=foreign-board",
        "HERMES_HOME=/foreign/profile",
        "HERMES_TENANT=foreign-tenant",
        "HERMES_TUI=1",
        "HERMES_ACCEPT_HOOKS=1",
    ]
    foreign_values.extend(f"{key}=foreign-route" for key in sorted(_VAR_MAP))
    (home / ".env").write_text(
        "\n".join(foreign_values) + "\n",
        encoding="utf-8",
    )
    dispatcher_identity = {
        "HERMES_KANBAN_WORKER_SCOPE": "lifecycle-only",
        "HERMES_KANBAN_TASK": "owned-task",
        "HERMES_KANBAN_RUN_ID": "owned-run",
        "HERMES_KANBAN_WORKSPACE": "/owned/workspace",
        "HERMES_KANBAN_BOARD": "owned-board",
        "HERMES_HOME": "/owned/profile",
        "HERMES_TENANT": "owned-tenant",
        "HERMES_SESSION_SOURCE": "kanban",
    }
    for key, value in dispatcher_identity.items():
        monkeypatch.setenv(key, value)
    dispatcher_absences = (
        set(_VAR_MAP) - {"HERMES_SESSION_SOURCE"}
    ) | {"HERMES_TUI", "HERMES_ACCEPT_HOOKS"}
    for key in dispatcher_absences:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(env_loader, "_apply_external_secret_sources", lambda _home: None)
    monkeypatch.setattr(env_loader, "_apply_managed_env", lambda: None)

    loaded = env_loader.load_hermes_dotenv(hermes_home=home)

    assert loaded == [home / ".env"]
    assert {
        key: os.environ.get(key) for key in dispatcher_identity
    } == dispatcher_identity
    assert dispatcher_absences.isdisjoint(os.environ)


def test_pinned_worker_authority_covers_gateway_routing() -> None:
    from gateway.session_context import _VAR_MAP
    from hermes_cli.kanban_worker_scope import PINNED_WORKER_ENV_KEYS

    assert set(_VAR_MAP) <= set(PINNED_WORKER_ENV_KEYS)


@pytest.mark.parametrize("failing_stage", ["dotenv", "managed", "config"])
def test_dispatcher_authority_is_restored_when_startup_stage_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failing_stage: str,
) -> None:
    from hermes_cli import env_loader
    from hermes_cli.kanban_worker_scope import PINNED_WORKER_ENV_KEYS

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / ".env").write_text("ORDINARY_SETTING=loaded\n", encoding="utf-8")

    for index, key in enumerate(PINNED_WORKER_ENV_KEYS):
        if key in {"HERMES_TUI", "HERMES_ACCEPT_HOOKS"}:
            monkeypatch.delenv(key, raising=False)
        elif key == "HERMES_KANBAN_WORKER_SCOPE":
            monkeypatch.setenv(key, "lifecycle-only")
        elif key == "HERMES_KANBAN_TASK":
            monkeypatch.setenv(key, "owned-task")
        else:
            monkeypatch.setenv(key, f"owned-{index}")
    expected = {key: os.environ.get(key) for key in PINNED_WORKER_ENV_KEYS}

    def mutate_and_raise(*_args, **_kwargs) -> None:
        for key in PINNED_WORKER_ENV_KEYS:
            os.environ[key] = "foreign"
        raise RuntimeError(f"{failing_stage} bridge failed")

    monkeypatch.setattr(env_loader, "_apply_managed_env", lambda: None)
    monkeypatch.setattr(env_loader, "_reapply_terminal_config_bridge", lambda _home: None)
    if failing_stage == "dotenv":
        monkeypatch.setattr(env_loader, "_load_dotenv_with_fallback", mutate_and_raise)
    elif failing_stage == "managed":
        monkeypatch.setattr(env_loader, "_apply_managed_env", mutate_and_raise)
    else:
        monkeypatch.setattr(env_loader, "_reapply_terminal_config_bridge", mutate_and_raise)

    with pytest.raises(RuntimeError, match=f"{failing_stage} bridge failed"):
        env_loader.load_hermes_dotenv(hermes_home=home)

    assert {
        key: os.environ.get(key) for key in PINNED_WORKER_ENV_KEYS
    } == expected


def test_dispatcher_identity_is_restored_before_later_startup_bridges(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from hermes_cli import env_loader

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / ".env").write_text(
        "HERMES_KANBAN_WORKER_SCOPE=\nHERMES_KANBAN_TASK=foreign-task\n",
        encoding="utf-8",
    )
    _lifecycle_env(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", "/owned/profile")
    monkeypatch.setenv("HERMES_TENANT", "owned-tenant")
    monkeypatch.delenv("HERMES_TUI", raising=False)
    monkeypatch.delenv("HERMES_ACCEPT_HOOKS", raising=False)
    observations: list[tuple[str | None, ...]] = []

    def observe() -> tuple[str | None, ...]:
        return tuple(
            os.environ.get(key)
            for key in (
                "HERMES_KANBAN_WORKER_SCOPE",
                "HERMES_KANBAN_TASK",
                "HERMES_HOME",
                "HERMES_TENANT",
                "HERMES_TUI",
                "HERMES_ACCEPT_HOOKS",
            )
        )

    def managed_bridge() -> None:
        observations.append(observe())
        os.environ["HERMES_KANBAN_TASK"] = "managed-task"
        os.environ["HERMES_HOME"] = "/managed/profile"
        os.environ["HERMES_TENANT"] = "managed-tenant"
        os.environ["HERMES_TUI"] = "1"
        os.environ["HERMES_ACCEPT_HOOKS"] = "1"

    def terminal_bridge(_home: Path) -> None:
        observations.append(observe())
        os.environ["HERMES_KANBAN_TASK"] = "terminal-task"
        os.environ["HERMES_HOME"] = "/terminal/profile"
        os.environ["HERMES_TENANT"] = "terminal-tenant"
        os.environ["HERMES_TUI"] = "1"
        os.environ["HERMES_ACCEPT_HOOKS"] = "1"

    monkeypatch.setattr(env_loader, "_apply_managed_env", managed_bridge)
    monkeypatch.setattr(env_loader, "_reapply_terminal_config_bridge", terminal_bridge)
    monkeypatch.setattr(
        env_loader,
        "_apply_external_secret_sources",
        lambda _home: pytest.fail("scoped worker must skip external secret sources"),
    )

    env_loader.load_hermes_dotenv(hermes_home=home)

    assert observations == [
        (
            "lifecycle-only",
            "t_lifecycle_startup",
            "/owned/profile",
            "owned-tenant",
            None,
            None,
        ),
        (
            "lifecycle-only",
            "t_lifecycle_startup",
            "/owned/profile",
            "owned-tenant",
            None,
            None,
        ),
    ]
    assert observe() == (
        "lifecycle-only",
        "t_lifecycle_startup",
        "/owned/profile",
        "owned-tenant",
        None,
        None,
    )


@pytest.mark.parametrize("scope", ["lifecycle-only", "future-worker-scope"])
def test_scoped_worker_skips_external_secret_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    scope: str,
) -> None:
    from hermes_cli import env_loader

    home = tmp_path / ".hermes"
    home.mkdir()
    _lifecycle_env(monkeypatch, scope)
    calls: list[Path] = []
    monkeypatch.setattr(
        env_loader,
        "_apply_external_secret_sources",
        lambda resolved_home: calls.append(resolved_home),
    )
    monkeypatch.setattr(env_loader, "_apply_managed_env", lambda: None)

    env_loader.load_hermes_dotenv(hermes_home=home)

    assert calls == []


def test_normal_worker_runs_external_secret_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from hermes_cli import env_loader

    home = tmp_path / ".hermes"
    home.mkdir()
    _clear_lifecycle_env(monkeypatch)
    calls: list[Path] = []
    monkeypatch.setattr(
        env_loader,
        "_apply_external_secret_sources",
        lambda resolved_home: calls.append(resolved_home),
    )
    monkeypatch.setattr(env_loader, "_apply_managed_env", lambda: None)

    env_loader.load_hermes_dotenv(hermes_home=home)

    assert calls == [home]


def test_normal_dispatcher_worker_keeps_authority_and_runs_secret_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from hermes_cli import env_loader

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / ".env").write_text(
        "HERMES_KANBAN_TASK=foreign-task\n"
        "HERMES_KANBAN_BOARD=foreign-board\n"
        "HERMES_SESSION_SOURCE=telegram\n"
        "HERMES_SESSION_CHAT_ID=foreign-chat\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_KANBAN_TASK", "owned-task")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "owned-board")
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")
    monkeypatch.delenv("HERMES_KANBAN_WORKER_SCOPE", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    calls: list[Path] = []
    monkeypatch.setattr(
        env_loader,
        "_apply_external_secret_sources",
        lambda resolved_home: calls.append(resolved_home),
    )
    monkeypatch.setattr(env_loader, "_apply_managed_env", lambda: None)

    env_loader.load_hermes_dotenv(hermes_home=home)

    assert os.environ["HERMES_KANBAN_TASK"] == "owned-task"
    assert os.environ["HERMES_KANBAN_BOARD"] == "owned-board"
    assert os.environ["HERMES_SESSION_SOURCE"] == "kanban"
    assert "HERMES_SESSION_CHAT_ID" not in os.environ
    assert calls == [home]


@pytest.mark.parametrize("scope", ["lifecycle-only", "future-worker-scope"])
def test_scoped_worker_does_not_execute_user_memory_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    scope: str,
) -> None:
    _lifecycle_env(monkeypatch, scope)
    marker = tmp_path / "memory-provider-executed"
    plugins_dir = tmp_path / "plugins"
    provider_dir = plugins_dir / "adversarial_probe"
    provider_dir.mkdir(parents=True)
    (provider_dir / "__init__.py").write_text(
        "\n".join(
            [
                "from pathlib import Path",
                f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')",
                "class MemoryProvider:",
                "    pass",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    from plugins import memory as memory_plugins

    monkeypatch.setattr(memory_plugins, "_get_user_plugins_dir", lambda: plugins_dir)
    module_name = "_hermes_user_memory.adversarial_probe"
    sys.modules.pop(module_name, None)
    try:
        assert memory_plugins.load_memory_provider("adversarial_probe") is None
    finally:
        sys.modules.pop(module_name, None)

    assert not marker.exists()


@pytest.mark.parametrize("scope", ["lifecycle-only", "future-worker-scope"])
def test_scoped_worker_does_not_execute_memory_provider_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    scope: str,
) -> None:
    _lifecycle_env(monkeypatch, scope)
    marker = tmp_path / "memory-provider-cli-executed"
    plugins_dir = tmp_path / "plugins"
    provider_dir = plugins_dir / "adversarial_cli"
    provider_dir.mkdir(parents=True)
    (provider_dir / "__init__.py").write_text(
        "# MemoryProvider plugin probe\n",
        encoding="utf-8",
    )
    (provider_dir / "cli.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
        "def register_cli(_parser):\n"
        "    return None\n",
        encoding="utf-8",
    )

    from plugins import memory as memory_plugins

    monkeypatch.setattr(memory_plugins, "_get_user_plugins_dir", lambda: plugins_dir)
    monkeypatch.setattr(
        memory_plugins,
        "_get_active_memory_provider",
        lambda: "adversarial_cli",
    )
    module_name = "_hermes_user_memory.adversarial_cli.cli"
    sys.modules.pop(module_name, None)
    try:
        assert memory_plugins.discover_plugin_cli_commands() == []
    finally:
        sys.modules.pop(module_name, None)
        sys.modules.pop("_hermes_user_memory.adversarial_cli", None)

    assert not marker.exists()


@pytest.mark.parametrize("scope", ["lifecycle-only", "future-worker-scope"])
def test_scoped_worker_does_not_execute_user_model_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    scope: str,
) -> None:
    _lifecycle_env(monkeypatch, scope)
    marker = tmp_path / "model-provider-executed"
    plugins_dir = tmp_path / "model-providers"
    provider_dir = plugins_dir / "adversarial_probe"
    provider_dir.mkdir(parents=True)
    (provider_dir / "__init__.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )

    import pkgutil
    import providers

    monkeypatch.setattr(providers, "_discovered", False)
    monkeypatch.setattr(providers, "_REGISTRY", {})
    monkeypatch.setattr(providers, "_ALIASES", {})
    monkeypatch.setattr(providers, "_PROVIDER_LIST_CACHE", None)
    monkeypatch.setattr(providers, "_BUNDLED_PLUGINS_DIR", tmp_path / "bundled")
    monkeypatch.setattr(providers, "_user_plugins_dir", lambda: plugins_dir)
    monkeypatch.setattr(pkgutil, "iter_modules", lambda *_args, **_kwargs: [])
    module_name = "_hermes_user_provider_adversarial_probe"
    sys.modules.pop(module_name, None)
    try:
        assert providers.list_providers() == []
    finally:
        sys.modules.pop(module_name, None)

    assert not marker.exists()


@pytest.mark.parametrize("scope", ["lifecycle-only", "future-worker-scope"])
def test_scoped_worker_does_not_import_legacy_provider_extensions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    scope: str,
) -> None:
    _lifecycle_env(monkeypatch, scope)

    import pkgutil
    import providers

    monkeypatch.setattr(providers, "_discovered", False)
    monkeypatch.setattr(providers, "_REGISTRY", {})
    monkeypatch.setattr(providers, "_ALIASES", {})
    monkeypatch.setattr(providers, "_PROVIDER_LIST_CACHE", None)
    monkeypatch.setattr(providers, "_BUNDLED_PLUGINS_DIR", tmp_path / "bundled")
    monkeypatch.setattr(providers, "_user_plugins_dir", lambda: None)
    monkeypatch.setattr(
        pkgutil,
        "iter_modules",
        lambda *_args, **_kwargs: [(None, "adversarial_legacy", False)],
    )
    imported: list[str] = []
    monkeypatch.setattr(
        providers.importlib,
        "import_module",
        lambda name: imported.append(name),
    )

    assert providers.list_providers() == []
    assert imported == []


@pytest.mark.parametrize(
    ("scope", "should_execute"),
    [
        ("lifecycle-only", False),
        ("future-worker-scope", False),
        (None, True),
    ],
)
def test_legacy_provider_execution_boundary_in_fresh_process(
    tmp_path: Path,
    scope: str | None,
    should_execute: bool,
) -> None:
    marker = tmp_path / "legacy-provider-executed"
    legacy_dir = tmp_path / "providers"
    legacy_dir.mkdir()
    (legacy_dir / "adversarial_legacy.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    code = """
import os
from pathlib import Path
import providers
providers.__path__ = [os.environ["LEGACY_PROVIDER_DIR"]]
providers._BUNDLED_PLUGINS_DIR = Path(os.environ["EMPTY_BUNDLED_DIR"])
providers._REGISTRY.clear()
providers._ALIASES.clear()
providers._PROVIDER_LIST_CACHE = None
providers._discovered = False
providers.list_providers()
print("MARKER=" + str(Path(os.environ["LEGACY_MARKER"]).exists()))
"""
    env = os.environ.copy()
    env.update(
        {
            "LEGACY_PROVIDER_DIR": str(legacy_dir),
            "EMPTY_BUNDLED_DIR": str(tmp_path / "bundled"),
            "LEGACY_MARKER": str(marker),
        }
    )
    env.pop("HERMES_KANBAN_TASK", None)
    env.pop("HERMES_KANBAN_WORKER_SCOPE", None)
    if scope is not None:
        env["HERMES_KANBAN_TASK"] = "t_legacy_probe"
        env["HERMES_KANBAN_WORKER_SCOPE"] = scope

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )

    assert f"MARKER={should_execute}" in result.stdout


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
