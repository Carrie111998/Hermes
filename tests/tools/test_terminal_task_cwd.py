"""Regression tests for task/session cwd propagation in terminal_tool."""

import json
from types import SimpleNamespace

import pytest

import tools.terminal_tool as terminal_tool


@pytest.fixture(autouse=True)
def _isolate_cwd_registries(monkeypatch):
    """Keep cwd values and their authority-scope tags isolated together."""
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    monkeypatch.setattr(terminal_tool, "_session_cwd_authority_scopes", {})


def _minimal_terminal_config(cwd="/default"):
    return {
        "env_type": "local",
        "cwd": cwd,
        "timeout": 60,
        "lifetime_seconds": 3600,
    }


def test_foreground_command_uses_registered_task_cwd_for_existing_environment(monkeypatch):
    """ACP can update task cwd after the local env exists; foreground must honor it."""
    calls = []

    class FakeEnv:
        env = {}

        def execute(self, command, **kwargs):
            calls.append((command, kwargs))
            return {"output": "ok", "returncode": 0}

    task_id = "acp-session-1"
    monkeypatch.setattr(terminal_tool, "_active_environments", {task_id: FakeEnv()})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {task_id: {"cwd": "/workspace/acp"}})
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: _minimal_terminal_config())
    monkeypatch.setattr(
        terminal_tool,
        "_check_all_guards",
        lambda command, env_type, **kwargs: {"approved": True},
    )

    result = json.loads(terminal_tool.terminal_tool(command="pwd", task_id=task_id))

    assert result["exit_code"] == 0
    assert calls == [("pwd", {"timeout": 60, "cwd": "/workspace/acp", "bounded_capture": True})]


def test_explicit_workdir_still_wins_over_registered_task_cwd(monkeypatch):
    calls = []

    class FakeEnv:
        env = {}

        def execute(self, command, **kwargs):
            calls.append(kwargs)
            return {"output": "ok", "returncode": 0}

    task_id = "acp-session-1"
    monkeypatch.setattr(terminal_tool, "_active_environments", {task_id: FakeEnv()})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {task_id: {"cwd": "/workspace/acp"}})
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: _minimal_terminal_config())
    monkeypatch.setattr(
        terminal_tool,
        "_check_all_guards",
        lambda command, env_type, **kwargs: {"approved": True},
    )

    result = json.loads(
        terminal_tool.terminal_tool(
            command="pwd",
            task_id=task_id,
            workdir="/explicit/workdir",
        )
    )

    assert result["exit_code"] == 0
    assert calls == [{"timeout": 60, "cwd": "/explicit/workdir", "bounded_capture": True}]


def test_background_command_prefers_recorded_session_cwd_over_init_time_cwd(monkeypatch):
    """Background process launches must also use the recorded session cwd."""

    class FakeEnv:
        env = {}
        cwd = "/workspace/live"

    class FakeRegistry:
        def __init__(self):
            self.calls = []
            self.pending_watchers = []

        def spawn_local(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(id="proc_test", pid=1234)

    import tools.process_registry as process_registry_mod

    registry = FakeRegistry()
    task_id = "session-live-cwd-bg"
    monkeypatch.setattr(terminal_tool, "_active_environments", {task_id: FakeEnv()})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {task_id: {"cwd": "/workspace/init"}})
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: _minimal_terminal_config(cwd="/workspace/init"))
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool, "_resolve_container_task_id", lambda value: value or "default")
    monkeypatch.setattr(
        terminal_tool,
        "_check_all_guards",
        lambda command, env_type, **kwargs: {"approved": True},
    )
    monkeypatch.setattr(process_registry_mod, "process_registry", registry)
    terminal_tool.record_session_cwd(task_id, "/workspace/live")

    result = json.loads(
        terminal_tool.terminal_tool(
            command="sleep 1",
            task_id=task_id,
            background=True,
        )
    )

    assert result["exit_code"] == 0
    # session_key falls back to the raw task_id when no gateway contextvar is set
    # (it doesn't propagate to tool-worker threads), so process.kill / stop can
    # still find and terminate this background process.
    assert registry.calls == [{
        "command": "sleep 1",
        "cwd": "/workspace/live",
        "task_id": task_id,
        "session_key": task_id,
        "env_vars": {},
        "use_pty": False,
    }]


def test_safe_getcwd_falls_back_to_home_when_no_terminal_cwd(monkeypatch):
    def _boom():
        raise FileNotFoundError()

    monkeypatch.setattr(terminal_tool.os, "getcwd", _boom)
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    monkeypatch.setattr(terminal_tool.os.path, "expanduser", lambda p: "/home/me")
    assert terminal_tool._safe_getcwd() == "/home/me"


def test_authoritative_context_cwd_overrides_stale_session_record(monkeypatch):
    import agent.runtime_cwd as runtime_cwd

    monkeypatch.setattr(
        terminal_tool, "_session_cwd", {"default": "/workspace/stale-persona"}
    )
    tokens = runtime_cwd.set_authoritative_session_cwd("/workspace/cron-job")
    try:
        assert terminal_tool._resolve_command_cwd(
            workdir=None,
            default_cwd="/workspace/config",
            session_key="default",
        ) == "/workspace/cron-job"
    finally:
        runtime_cwd.reset_authoritative_session_cwd(tokens)


def test_authoritative_context_accepts_live_cwd_recorded_in_same_scope():
    import agent.runtime_cwd as runtime_cwd
    import tools.file_tools as file_tools
    from tools.code_execution_tool import _resolve_child_cwd

    task_id = "cron-live-cwd"
    tokens = runtime_cwd.set_authoritative_session_cwd("/workspace/cron-job")
    try:
        terminal_tool.record_session_cwd(task_id, "/workspace/cron-job/packages/api")
        assert terminal_tool._resolve_command_cwd(
            workdir=None,
            default_cwd="/workspace/config",
            session_key=task_id,
        ) == "/workspace/cron-job/packages/api"
        assert file_tools._authoritative_workspace_root(task_id) == (
            "/workspace/cron-job/packages/api"
        )
        assert _resolve_child_cwd("project", "/staging", task_id) == (
            "/workspace/cron-job/packages/api"
        )
    finally:
        terminal_tool.clear_session_cwd(task_id)
        runtime_cwd.reset_authoritative_session_cwd(tokens)


def test_authoritative_host_cwd_maps_to_workspace_for_docker_dispatch(monkeypatch):
    import agent.runtime_cwd as runtime_cwd

    calls = []

    class FakeEnv:
        env = {}
        cwd = "/workspace"

        def execute(self, command, **kwargs):
            calls.append((command, kwargs))
            return {"output": "ok", "returncode": 0, "cwd": self.cwd}

    config = _minimal_terminal_config(cwd="/workspace")
    config.update(
        {
            "env_type": "docker",
            "docker_image": "python:3.12",
            "host_cwd": "/mnt/project",
            "docker_mount_cwd_to_workspace": True,
            "docker_volumes": [],
        }
    )
    monkeypatch.setattr(terminal_tool, "_active_environments", {"default": FakeEnv()})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: config)
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(
        terminal_tool,
        "_check_all_guards",
        lambda command, env_type, **kwargs: {"approved": True},
    )

    tokens = runtime_cwd.set_authoritative_session_cwd("/mnt/project")
    try:
        result = json.loads(terminal_tool.terminal_tool(command="pwd"))
    finally:
        runtime_cwd.reset_authoritative_session_cwd(tokens)

    assert result["exit_code"] == 0
    assert calls == [
        ("pwd", {"timeout": 60, "cwd": "/workspace", "bounded_capture": True})
    ]


def test_authoritative_cwd_preserves_backend_native_semantics():
    import agent.runtime_cwd as runtime_cwd

    tokens = runtime_cwd.set_authoritative_session_cwd("/mnt/project")
    try:
        assert terminal_tool._resolve_command_cwd(
            workdir=None,
            default_cwd="/root",
            config={"env_type": "docker", "docker_mount_cwd_to_workspace": False},
        ) == "/root"
        assert terminal_tool._resolve_command_cwd(
            workdir=None,
            default_cwd="~",
            config={"env_type": "ssh"},
        ) == "/mnt/project"
    finally:
        runtime_cwd.reset_authoritative_session_cwd(tokens)


def test_authoritative_record_uses_command_result_not_shared_env_cwd(monkeypatch):
    import agent.runtime_cwd as runtime_cwd

    class FakeEnv:
        env = {}
        cwd = "/cron/root"

        def execute(self, command, **kwargs):
            self.cwd = "/foreign/session"
            return {"output": "ok", "returncode": 0, "cwd": "/cron/root/subdir"}

    monkeypatch.setattr(terminal_tool, "_active_environments", {"default": FakeEnv()})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: _minimal_terminal_config(cwd="/cron/root"),
    )
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(
        terminal_tool,
        "_check_all_guards",
        lambda command, env_type, **kwargs: {"approved": True},
    )

    tokens = runtime_cwd.set_authoritative_session_cwd("/cron/root")
    try:
        result = json.loads(terminal_tool.terminal_tool(command="cd subdir"))
        assert result["exit_code"] == 0
        assert terminal_tool.get_authoritative_session_cwd(None) == (
            "/cron/root/subdir"
        )
    finally:
        terminal_tool.clear_session_cwd("default")
        runtime_cwd.reset_authoritative_session_cwd(tokens)
