"""Focused boundaries for verified managed short-task startup."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch


def _managed_env(monkeypatch, tmp_path, *, lane="implementation"):
    snapshot = {
        "schema": 2,
        "enabled": lane == "implementation",
        "soft_iteration_limit": 4,
        "max_handoffs": 3,
        "max_iterations": 90,
        "failure_limit": 2,
        "validation_error": None,
    }
    if lane == "review":
        snapshot["inactive_reason"] = (
            "goal/review workers are outside Phase-1 handoff scope"
        )
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_managed")
    monkeypatch.setenv("HERMES_KANBAN_MANAGED_LANE", lane)
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_MODE", "1" if lane == "review" else "0")
    monkeypatch.setenv("HERMES_KANBAN_MANAGED_BOOTSTRAP", "1")
    monkeypatch.setenv("HERMES_KANBAN_MANAGED_BOOTSTRAP_VERIFIED", "1")
    monkeypatch.delenv("HERMES_KANBAN_MANAGED_BOOTSTRAP_ERROR", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    monkeypatch.setenv(
        "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY",
        json.dumps(snapshot),
    )


def test_verified_managed_plugin_entrypoints_are_inert(monkeypatch, tmp_path):
    from hermes_cli import plugins

    _managed_env(monkeypatch, tmp_path)
    calls = []
    manager = SimpleNamespace(
        discover_and_load=lambda **kwargs: calls.append(("discover", kwargs)),
        invoke_hook=lambda *args, **kwargs: calls.append(("hook", args, kwargs)) or ["x"],
        invoke_middleware=lambda *args, **kwargs: calls.append(("middleware", args, kwargs)) or ["x"],
        has_hook=lambda name: calls.append(("has_hook", name)) or True,
        has_middleware=lambda name: calls.append(("has_middleware", name)) or True,
        _context_engine=object(),
        _plugin_commands={"x": {"handler": object()}},
        _aux_tasks={"x": {"key": "x"}},
        _plugin_tool_names={"x"},
        _plugins={},
    )
    monkeypatch.setattr(plugins, "_plugin_manager", manager)

    plugins.discover_plugins()
    assert plugins.invoke_hook("on_session_start") == []
    assert plugins.invoke_middleware("tool_execution") == []
    assert plugins.has_hook("on_session_start") is False
    assert plugins.has_middleware("tool_execution") is False
    assert plugins.get_plugin_context_engine() is None
    assert plugins.get_plugin_command_handler("x") is None
    assert plugins.get_plugin_commands() == {}
    assert plugins.get_plugin_auxiliary_tasks() == []
    assert plugins.get_plugin_toolsets() == []
    assert calls == []

    # A lane-shaped environment without the CLI's verified attestation must
    # not change ordinary plugin behavior.
    monkeypatch.delenv("HERMES_KANBAN_MANAGED_BOOTSTRAP_VERIFIED")
    assert plugins.invoke_hook("on_session_start") == ["x"]
    assert calls and calls[-1][0] == "hook"


def test_managed_tool_schema_skips_tool_search_config_and_assembly(
    monkeypatch, tmp_path
):
    import model_tools

    _managed_env(monkeypatch, tmp_path)
    calls = []
    fake_tool_search = SimpleNamespace(
        load_config=lambda: calls.append("load") or SimpleNamespace(enabled="off"),
        assemble_tool_defs=lambda *args, **kwargs: calls.append("assemble"),
    )
    monkeypatch.setitem(sys.modules, "tools.tool_search", fake_tool_search)
    model_tools._clear_tool_defs_cache()

    definitions = model_tools.get_tool_definitions(
        enabled_toolsets=["file", "kanban"], quiet_mode=True
    )
    names = {item["function"]["name"] for item in definitions}
    assert names
    assert "tool_search" not in names
    assert calls == []

    monkeypatch.delenv("HERMES_KANBAN_TASK")
    monkeypatch.delenv("HERMES_KANBAN_MANAGED_LANE")
    monkeypatch.delenv("HERMES_KANBAN_MANAGED_BOOTSTRAP")
    monkeypatch.delenv("HERMES_KANBAN_MANAGED_BOOTSTRAP_VERIFIED")
    model_tools._clear_tool_defs_cache()
    model_tools.get_tool_definitions(
        enabled_toolsets=["file", "kanban"], quiet_mode=True
    )
    assert calls == ["load"]


def test_managed_oneshot_cleanup_does_not_import_unused_capabilities(
    monkeypatch,
):
    from hermes_cli import main

    imported = []
    real_import = __import__

    def recording_import(name, *args, **kwargs):
        imported.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(main, "_oneshot_cleanup_done", False)
    monkeypatch.setattr(main, "_MANAGED_SHORT_TASK_CLI_ACTIVE", True)
    monkeypatch.setattr(main, "_MANAGED_SHORT_TASK_BOOTSTRAP_VALID", True)
    with patch("builtins.__import__", side_effect=recording_import):
        main._cleanup_oneshot_runtime()

    assert "tools.terminal_tool" not in imported
    assert "tools.async_delegation" not in imported
    assert "tools.browser_tool" not in imported
    assert "tools.mcp_tool" not in imported
    assert "agent.auxiliary_client" not in imported


def test_real_managed_cli_import_loads_only_file_and_kanban_tools(
    tmp_path,
):
    """Exercise Python's real importer in a fresh isolated process."""
    repo_root = Path(__file__).resolve().parents[2]
    snapshot = {
        "schema": 2,
        "enabled": True,
        "soft_iteration_limit": 4,
        "max_handoffs": 3,
        "max_iterations": 90,
        "failure_limit": 2,
        "validation_error": None,
    }
    env = os.environ.copy()
    env.update(
        {
            "HERMES_KANBAN_TASK": "t_import_boundary",
            "HERMES_KANBAN_MANAGED_LANE": "implementation",
            "HERMES_KANBAN_REVIEW_MODE": "0",
            "HERMES_KANBAN_MANAGED_BOOTSTRAP": "1",
            "HERMES_KANBAN_MANAGED_BOOTSTRAP_VERIFIED": "1",
            "HERMES_KANBAN_WORKSPACE": str(tmp_path),
            "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY": json.dumps(snapshot),
            "PYTHONPYCACHEPREFIX": "/private/tmp/hermes-short-task-pycache",
        }
    )
    env.pop("HERMES_KANBAN_MANAGED_BOOTSTRAP_ERROR", None)
    script = (
        "import json,sys; "
        f"sys.path.insert(0, {str(repo_root)!r}); "
        "import hermes_cli.main; import cli; import run_agent; "
        "print(json.dumps(sorted(n for n in sys.modules if "
        "n.startswith('tools.') or n == 'model_tools')))"
    )

    completed = subprocess.run(
        [sys.executable, "-B", "-I", "-s", "-E", "-c", script],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    imported = set(json.loads(completed.stdout.strip().splitlines()[-1]))

    assert "model_tools" in imported
    assert "tools.managed_file_tools" in imported
    assert "tools.kanban_tools" in imported
    assert "tools.file_tools" not in imported
    assert "tools.file_operations" not in imported
    assert "tools.terminal_tool" not in imported
    assert "tools.browser_tool" not in imported
    assert "tools.code_execution_tool" not in imported
    assert "tools.cronjob_tool" not in imported
    assert "tools.delegate_tool" not in imported
    assert "tools.mcp_tool" not in imported


def test_fresh_managed_agent_uses_inert_runtime_without_auxiliary_import_or_probe(
    tmp_path,
):
    """Construct the production agent while import and network canaries are armed."""
    repo_root = Path(__file__).resolve().parents[2]
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    snapshot = {
        "schema": 2,
        "enabled": True,
        "soft_iteration_limit": 4,
        "max_handoffs": 3,
        "max_iterations": 90,
        "failure_limit": 2,
        "validation_error": None,
    }
    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(hermes_home),
            "HERMES_KANBAN_TASK": "t_runtime_boundary",
            "HERMES_KANBAN_MANAGED_LANE": "implementation",
            "HERMES_KANBAN_REVIEW_MODE": "0",
            "HERMES_KANBAN_MANAGED_BOOTSTRAP": "1",
            "HERMES_KANBAN_MANAGED_BOOTSTRAP_VERIFIED": "1",
            "HERMES_KANBAN_WORKSPACE": str(tmp_path),
            "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY": json.dumps(snapshot),
            "PYTHONPYCACHEPREFIX": "/private/tmp/hermes-short-task-pycache",
        }
    )
    env.pop("HERMES_KANBAN_MANAGED_BOOTSTRAP_ERROR", None)
    script = f"""
import importlib.abc, json, socket, sys, threading
sys.path.insert(0, {str(repo_root)!r})
blocked_imports = {{
    'agent.auxiliary_client',
    'agent.context_compressor',
    'agent.context_engine',
    'tools.checkpoint_manager',
}}
class ImportCanary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked_imports:
            raise AssertionError('forbidden managed import: ' + fullname)
        return None
sys.meta_path.insert(0, ImportCanary())
real_socket = socket.socket
network_calls = []
class CanarySocket(real_socket):
    def connect(self, address):
        network_calls.append(repr(address))
        raise AssertionError('managed startup attempted network access')
socket.socket = CanarySocket
real_thread_start = threading.Thread.start
thread_starts = []
def blocked_thread_start(self):
    thread_starts.append(getattr(self, 'name', ''))
    return real_thread_start(self)
threading.Thread.start = blocked_thread_start
from run_agent import AIAgent
agent = AIAgent(
    api_key='offline',
    base_url='https://example.invalid/v1',
    provider='custom:offline',
    model='offline-model',
    max_iterations=2,
    enabled_toolsets=['file', 'kanban'],
    quiet_mode=True,
    skip_context_files=True,
    skip_memory=True,
    platform='cli',
)
evidence = {{
    'checkpoint': type(agent._checkpoint_mgr).__name__,
    'context': type(agent.context_compressor).__name__,
    'compression': agent.compression_enabled,
    'network_calls': network_calls,
    'thread_starts': thread_starts,
    'tools': sorted(agent.valid_tool_names),
    'forbidden_loaded': sorted(blocked_imports.intersection(sys.modules)),
}}
agent.close()
print(json.dumps(evidence))
"""

    completed = subprocess.run(
        [sys.executable, "-B", "-I", "-s", "-E", "-c", script],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    evidence = json.loads(completed.stdout.strip().splitlines()[-1])

    assert evidence["checkpoint"] == "NullCheckpointManager"
    assert evidence["context"] == "ManagedShortTaskContext"
    assert evidence["compression"] is False
    assert evidence["network_calls"] == []
    assert evidence["thread_starts"] == []
    assert evidence["forbidden_loaded"] == []
    assert set(evidence["tools"]) == {
        "kanban_block",
        "kanban_comment",
        "kanban_complete",
        "kanban_heartbeat",
        "kanban_show",
        "patch",
        "read_file",
        "search_files",
        "write_file",
    }


def test_managed_openrouter_agent_does_not_start_metadata_prewarm(
    monkeypatch, tmp_path
):
    _managed_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir()

    from run_agent import AIAgent

    with (
        patch("agent.agent_init.threading.Thread") as thread_cls,
        patch("hermes_logging.setup_logging"),
    ):
        agent = AIAgent(
            api_key="offline",
            base_url="https://openrouter.ai/api/v1",
            provider="openrouter",
            model="openai/gpt-test",
            max_iterations=2,
            enabled_toolsets=[],
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            platform="cli",
        )
    assert not any(
        call.kwargs.get("name") == "openrouter-prewarm"
        for call in thread_cls.call_args_list
    )
    agent.close()
