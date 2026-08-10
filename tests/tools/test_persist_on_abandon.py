"""Behavioral contract for abandoned-turn background-process persistence."""

import json
import shlex
import sys
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from tools.process_registry import ProcessRegistry, ProcessSession


def _session(session_id: str, *, persist_on_abandon: bool = False) -> ProcessSession:
    return ProcessSession(
        id=session_id,
        command="sleep 60",
        task_id="session-a",
        environment_task_id="default",
        session_key="gateway-session-a",
        started_at=time.time(),
        persist_on_abandon=persist_on_abandon,
    )


def test_abandoned_reap_honors_opt_in_without_weakening_broad_cleanup(
    monkeypatch,
):
    registry = ProcessRegistry()
    ordinary = _session("proc_ordinary")
    explicit = _session("proc_explicit", persist_on_abandon=True)
    broad = _session("proc_broad", persist_on_abandon=True)
    registry._running = {
        ordinary.id: ordinary,
        explicit.id: explicit,
        broad.id: broad,
    }

    calls = []

    def fake_kill(
        session_id,
        *,
        source="process.kill",
        consume_output=True,
    ):
        calls.append(
            (
                session_id,
                {"source": source, "consume_output": consume_output},
            )
        )
        registry._running[session_id].exited = True
        return {"status": "killed"}

    monkeypatch.setattr(registry, "kill_process", fake_kill)

    assert registry.kill_started_since(
        "session-a",
        frozenset(),
        source="gateway_turn_timeout",
    ) == 1
    assert calls == [
        (
            ordinary.id,
            {
                "source": "gateway_turn_timeout",
                "consume_output": True,
            },
        )
    ]

    assert registry.kill_process(explicit.id)["status"] == "killed"
    assert calls[-1] == (
        explicit.id,
        {"source": "process.kill", "consume_output": True},
    )

    calls.clear()
    assert registry.kill_all("session-a") == 1
    assert calls == [
        (
            broad.id,
            {
                "source": "kill_all",
                "consume_output": False,
            },
        )
    ]


def test_process_list_surfaces_abandoned_turn_opt_in():
    registry = ProcessRegistry()
    ordinary = _session("proc_ordinary")
    persistent = _session("proc_persistent", persist_on_abandon=True)
    registry._running = {
        ordinary.id: ordinary,
        persistent.id: persistent,
    }

    listed = {
        entry["session_id"]: entry
        for entry in registry.list_sessions(task_id="session-a")
    }

    assert "persist_on_abandon" not in listed[ordinary.id]
    assert listed[persistent.id]["persist_on_abandon"] is True


def test_shared_environment_key_stays_live_for_session_owned_process(
    monkeypatch,
):
    import tools.process_registry as process_registry_module
    import tools.terminal_tool as terminal_tool_module

    registry = ProcessRegistry()
    process = _session("proc_shared_environment")
    registry._running[process.id] = process
    environment = SimpleNamespace(cleanup=MagicMock())

    monkeypatch.setattr(process_registry_module, "process_registry", registry)
    monkeypatch.setattr(
        terminal_tool_module,
        "_active_environments",
        {"default": environment},
    )
    monkeypatch.setattr(terminal_tool_module, "_last_activity", {"default": 0.0})
    monkeypatch.setattr(terminal_tool_module.time, "time", lambda: 100.0)

    assert registry.has_active_processes("default") is True
    terminal_tool_module._cleanup_inactive_envs(lifetime_seconds=10)

    assert terminal_tool_module._active_environments["default"] is environment
    assert terminal_tool_module._last_activity["default"] == 100.0
    environment.cleanup.assert_not_called()


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.mark.linux_only
def test_local_opted_in_process_survives_reap_but_explicit_kill_still_works(
    tmp_path,
    monkeypatch,
):
    registry = ProcessRegistry()
    monkeypatch.setattr(registry, "_write_checkpoint", lambda: None)
    command = (
        f"{shlex.quote(sys.executable)} -c "
        f"{shlex.quote('import time; time.sleep(60)')}"
    )
    ordinary = registry.spawn_local(
        command,
        cwd=str(tmp_path),
        task_id="session-a",
        environment_task_id="default",
    )
    persistent = None
    try:
        persistent = registry.spawn_local(
            command,
            cwd=str(tmp_path),
            task_id="session-a",
            environment_task_id="default",
            persist_on_abandon=True,
        )

        assert registry.kill_started_since(
            "session-a",
            frozenset(),
            source="gateway_turn_timeout",
        ) == 1
        ordinary_process = ordinary.process
        persistent_process = persistent.process
        assert ordinary_process is not None
        assert persistent_process is not None
        assert _wait_until(lambda: ordinary_process.poll() is not None)
        assert persistent_process.poll() is None

        result = registry.kill_process(persistent.id)
        assert result["status"] == "killed"
        assert _wait_until(lambda: persistent_process.poll() is not None)
    finally:
        registry.kill_all()


def test_non_local_spawn_records_abandoned_turn_opt_in(monkeypatch):
    registry = ProcessRegistry()
    fake_thread = MagicMock()

    class FakeEnvironment:
        def get_temp_dir(self):
            return "/tmp"

        def execute(self, command, **kwargs):
            return {"output": "4242\n", "returncode": 0}

    monkeypatch.setattr("tools.process_registry.threading.Thread", lambda **_kw: fake_thread)
    monkeypatch.setattr(registry, "_write_checkpoint", lambda: None)

    session = registry.spawn_via_env(
        FakeEnvironment(),
        "sleep 60",
        task_id="session-a",
        environment_task_id="default",
        persist_on_abandon=True,
    )

    assert session.persist_on_abandon is True
    assert session.environment_task_id == "default"
    assert registry.get(session.id) is session
    fake_thread.start.assert_called_once_with()


def test_checkpoint_round_trip_and_old_checkpoint_default(monkeypatch, tmp_path):
    import tools.process_registry as process_registry_module

    checkpoint = tmp_path / "processes.json"
    monkeypatch.setattr(process_registry_module, "CHECKPOINT_PATH", checkpoint)

    original = ProcessRegistry()
    persistent = _session("proc_checkpoint", persist_on_abandon=True)
    persistent.pid = 4242
    persistent.host_start_time = 123
    original._running[persistent.id] = persistent
    original._write_checkpoint()

    encoded = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert encoded[0]["persist_on_abandon"] is True
    assert encoded[0]["environment_task_id"] == "default"

    recovered = ProcessRegistry()
    monkeypatch.setattr(recovered, "_host_pid_is_ours", lambda *_args: True)
    assert recovered.recover_from_checkpoint() == 1
    recovered_session = recovered.get(persistent.id)
    assert recovered_session is not None
    assert recovered_session.persist_on_abandon is True
    assert recovered_session.environment_task_id == "default"

    encoded[0].pop("persist_on_abandon")
    encoded[0].pop("environment_task_id")
    checkpoint.write_text(json.dumps(encoded), encoding="utf-8")
    old_checkpoint = ProcessRegistry()
    monkeypatch.setattr(old_checkpoint, "_host_pid_is_ours", lambda *_args: True)
    assert old_checkpoint.recover_from_checkpoint() == 1
    old_session = old_checkpoint.get(persistent.id)
    assert old_session is not None
    assert old_session.persist_on_abandon is False
    assert old_session.environment_task_id == old_session.task_id == "session-a"


def test_foreground_opt_in_fails_before_environment_creation(monkeypatch):
    import tools.terminal_tool as terminal_tool_module

    monkeypatch.setattr(
        terminal_tool_module,
        "_get_env_config",
        lambda: (_ for _ in ()).throw(
            AssertionError("invalid opt-in must fail before environment creation")
        ),
    )

    result = json.loads(
        terminal_tool_module.terminal_tool(
            command="echo should-not-run",
            persist_on_abandon=True,
        )
    )

    assert result["status"] == "error"
    assert "persist_on_abandon=true requires background=true" in result["error"]


def test_terminal_schema_and_handler_expose_abandoned_turn_opt_in(monkeypatch):
    import tools.terminal_tool as terminal_tool_module

    schema = cast(dict[str, Any], terminal_tool_module.TERMINAL_SCHEMA)
    parameters = cast(dict[str, Any], schema["parameters"])
    properties = cast(dict[str, dict[str, Any]], parameters["properties"])
    prop = properties["persist_on_abandon"]
    assert prop["type"] == "boolean"
    assert prop["default"] is False

    captured = {}
    monkeypatch.setattr(
        terminal_tool_module,
        "terminal_tool",
        lambda **kwargs: captured.update(kwargs) or "{}",
    )
    terminal_tool_module._handle_terminal(
        {
            "command": "sleep 60",
            "background": True,
            "persist_on_abandon": True,
        },
        task_id="session-a",
    )

    assert captured["persist_on_abandon"] is True


def test_execute_code_keeps_background_persistence_out_of_foreground_stub():
    from tools.code_execution_tool import _TERMINAL_BLOCKED_PARAMS

    assert "persist_on_abandon" in _TERMINAL_BLOCKED_PARAMS


def _configure_background_terminal(
    monkeypatch,
    tmp_path,
    *,
    env_type,
    registry,
    environment,
):
    import tools.process_registry as process_registry_module
    import tools.terminal_tool as terminal_tool_module

    config = {
        "env_type": env_type,
        "cwd": str(tmp_path),
        "timeout": 60,
        "lifetime_seconds": 3600,
    }
    monkeypatch.setattr(process_registry_module, "process_registry", registry)
    monkeypatch.setattr(
        terminal_tool_module,
        "_active_environments",
        {"default": environment},
    )
    monkeypatch.setattr(terminal_tool_module, "_last_activity", {})
    monkeypatch.setattr(terminal_tool_module, "_task_env_overrides", {})
    monkeypatch.setattr(terminal_tool_module, "_container_aliases", {})
    monkeypatch.setattr(terminal_tool_module, "_get_env_config", lambda: config)
    monkeypatch.setattr(terminal_tool_module, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(
        terminal_tool_module,
        "_check_all_guards",
        lambda *_args, **_kwargs: {"approved": True},
    )
    monkeypatch.setattr(
        "tools.approval.get_current_session_key",
        lambda default="": "agent:main:telegram:dm:race",
    )
    monkeypatch.setenv("TERMINAL_ENV", env_type)
    return terminal_tool_module


@pytest.mark.parametrize("env_type", ["local", "ssh"], ids=("local", "non-local"))
def test_background_spawn_is_skipped_when_tool_thread_is_already_interrupted(
    monkeypatch,
    tmp_path,
    env_type,
):
    from tools.interrupt import is_interrupted, set_interrupt

    registry = SimpleNamespace(
        pending_watchers=[],
        spawn_local=MagicMock(side_effect=AssertionError("local spawn must not run")),
        spawn_via_env=MagicMock(side_effect=AssertionError("remote spawn must not run")),
    )
    environment = SimpleNamespace(env={}, cwd=str(tmp_path))
    terminal_tool_module = _configure_background_terminal(
        monkeypatch,
        tmp_path,
        env_type=env_type,
        registry=registry,
        environment=environment,
    )

    set_interrupt(True)
    try:
        result = json.loads(
            terminal_tool_module.terminal_tool(
                command="sleep 60",
                background=True,
                task_id=f"session-{env_type}",
                persist_on_abandon=True,
            )
        )
        assert is_interrupted() is True
    finally:
        set_interrupt(False)

    assert result["exit_code"] == 130, result
    assert result["output"] == "[Command interrupted]"
    assert "session_id" not in result
    registry.spawn_local.assert_not_called()
    registry.spawn_via_env.assert_not_called()


@pytest.mark.parametrize(
    ("env_type", "spawn_method", "process_id", "pid"),
    [
        ("local", "spawn_local", "proc_local_race", 1234),
        ("ssh", "spawn_via_env", "proc_remote_race", 4242),
    ],
    ids=("local", "non-local"),
)
def test_background_spawn_racing_interrupt_is_killed_and_consumed(
    monkeypatch,
    tmp_path,
    env_type,
    spawn_method,
    process_id,
    pid,
):
    from tools.interrupt import is_interrupted, set_interrupt

    registry = ProcessRegistry()
    monkeypatch.setattr(registry, "_write_checkpoint", lambda: None)
    host_kills = []
    remote_commands = []

    class FakeEnvironment:
        env = {}
        cwd = str(tmp_path)

        def execute(self, command, **kwargs):
            remote_commands.append((command, kwargs))
            return {"output": "", "returncode": 0}

    environment = FakeEnvironment()
    created = {}

    def fake_spawn(**kwargs):
        session = ProcessSession(
            id=process_id,
            command=kwargs["command"],
            task_id=kwargs["task_id"],
            environment_task_id=kwargs["environment_task_id"],
            session_key=kwargs["session_key"],
            pid=pid,
            persist_on_abandon=kwargs["persist_on_abandon"],
        )
        if env_type == "local":
            session.process = cast(Any, SimpleNamespace(pid=pid))
        else:
            session.env_ref = kwargs["env"]
            session.pid_scope = "sandbox"
        registry._running[session.id] = session
        created["session"] = session
        # Deterministic race barrier: interruption lands after registration but
        # before terminal_tool's post-spawn check.
        set_interrupt(True)
        return session

    monkeypatch.setattr(registry, spawn_method, fake_spawn)
    monkeypatch.setattr(
        registry,
        "_terminate_host_pid",
        lambda process_pid, start_time: host_kills.append((process_pid, start_time)),
    )
    terminal_tool_module = _configure_background_terminal(
        monkeypatch,
        tmp_path,
        env_type=env_type,
        registry=registry,
        environment=environment,
    )

    set_interrupt(False)
    try:
        result = json.loads(
            terminal_tool_module.terminal_tool(
                command="sleep 60",
                background=True,
                notify_on_complete=True,
                task_id=f"session-{env_type}",
                persist_on_abandon=True,
            )
        )
        assert is_interrupted() is True
    finally:
        set_interrupt(False)

    session = created["session"]
    assert result["exit_code"] == 130
    assert result["output"] == "[Command interrupted]"
    assert "session_id" not in result
    assert session.exited is True
    assert session.completion_reason == "killed"
    assert session.termination_source == "terminal_interrupt"
    assert registry.is_completion_consumed(session.id) is True
    assert registry.pending_watchers == []
    assert registry.completion_queue.empty()
    if env_type == "local":
        assert host_kills == [(pid, None)]
        assert remote_commands == []
    else:
        assert host_kills == []
        assert remote_commands == [(f"kill {pid} 2>/dev/null", {"timeout": 5})]


@pytest.mark.parametrize(
    ("env_type", "spawn_method", "process_id", "pid"),
    [
        ("local", "spawn_local", "proc_local", 1234),
        ("ssh", "spawn_via_env", "proc_remote", 4242),
    ],
    ids=("local", "non-local"),
)
@pytest.mark.parametrize(
    "persist_on_abandon",
    [False, True],
    ids=("ordinary", "abandon-exempt"),
)
def test_terminal_background_spawn_receives_opt_in(
    monkeypatch,
    tmp_path,
    env_type,
    spawn_method,
    process_id,
    pid,
    persist_on_abandon,
):
    import tools.process_registry as process_registry_module
    import tools.terminal_tool as terminal_tool_module

    task_id = f"session-{env_type}"
    captured = {}

    class FakeRegistry:
        pending_watchers = []

    fake_registry = FakeRegistry()

    def fake_spawn(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id=process_id, pid=pid)

    setattr(fake_registry, spawn_method, fake_spawn)

    config = {
        "env_type": env_type,
        "cwd": str(tmp_path),
        "timeout": 60,
        "lifetime_seconds": 3600,
    }
    environment = SimpleNamespace(env={}, cwd=str(tmp_path))
    monkeypatch.setattr(
        terminal_tool_module,
        "_active_environments",
        {"default": environment},
    )
    monkeypatch.setattr(terminal_tool_module, "_last_activity", {})
    monkeypatch.setattr(terminal_tool_module, "_task_env_overrides", {})
    monkeypatch.setattr(terminal_tool_module, "_get_env_config", lambda: config)
    monkeypatch.setattr(terminal_tool_module, "_start_cleanup_thread", lambda: None)
    monkeypatch.setenv("TERMINAL_ENV", env_type)
    monkeypatch.setattr(
        terminal_tool_module,
        "_check_all_guards",
        lambda *_args, **_kwargs: {"approved": True},
    )
    monkeypatch.setattr(
        process_registry_module,
        "process_registry",
        fake_registry,
    )

    result = json.loads(
        terminal_tool_module.terminal_tool(
            command="sleep 60",
            background=True,
            task_id=task_id,
            persist_on_abandon=persist_on_abandon,
        )
    )

    assert captured["persist_on_abandon"] is persist_on_abandon
    assert captured["task_id"] == task_id
    assert captured["environment_task_id"] == "default"
    assert terminal_tool_module._resolve_container_task_id(task_id) == "default"
    assert result.get("persist_on_abandon", False) is persist_on_abandon
    if env_type == "local":
        assert captured["env_vars"] == {}
    else:
        assert captured["env"] is environment


def test_delegate_background_processes_use_parent_cleanup_owner(
    monkeypatch,
    tmp_path,
):
    import tools.process_registry as process_registry_module
    import tools.terminal_tool as terminal_tool_module

    registry = ProcessRegistry()
    next_id = iter(("proc_delegate_ordinary", "proc_delegate_persistent"))

    def fake_spawn_local(**kwargs):
        session = ProcessSession(
            id=next(next_id),
            command=kwargs["command"],
            task_id=kwargs["task_id"],
            environment_task_id=kwargs["environment_task_id"],
            session_key=kwargs["session_key"],
            persist_on_abandon=kwargs["persist_on_abandon"],
        )
        registry._running[session.id] = session
        return session

    kill_calls = []

    def fake_kill(session_id, **kwargs):
        kill_calls.append((session_id, kwargs))
        registry._running[session_id].exited = True
        return {"status": "killed"}

    monkeypatch.setattr(registry, "spawn_local", fake_spawn_local)
    monkeypatch.setattr(registry, "kill_process", fake_kill)

    config = {
        "env_type": "local",
        "cwd": str(tmp_path),
        "timeout": 60,
        "lifetime_seconds": 3600,
    }
    environment = SimpleNamespace(env={}, cwd=str(tmp_path))
    monkeypatch.setattr(process_registry_module, "process_registry", registry)
    monkeypatch.setattr(terminal_tool_module, "_active_environments", {"default": environment})
    monkeypatch.setattr(terminal_tool_module, "_last_activity", {})
    monkeypatch.setattr(terminal_tool_module, "_task_env_overrides", {})
    monkeypatch.setattr(terminal_tool_module, "_container_aliases", {})
    monkeypatch.setattr(terminal_tool_module, "_get_env_config", lambda: config)
    monkeypatch.setattr(terminal_tool_module, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(
        "tools.approval.get_current_session_key",
        lambda default="": "agent:main:telegram:dm:parent",
    )
    monkeypatch.setattr(
        terminal_tool_module,
        "_check_all_guards",
        lambda *_args, **_kwargs: {"approved": True},
    )
    monkeypatch.setenv("TERMINAL_ENV", "local")
    terminal_tool_module.register_container_alias("sa-child", "parent-session")

    ordinary_result = json.loads(
        terminal_tool_module.terminal_tool(
            command="sleep 60",
            background=True,
            task_id="sa-child",
        )
    )
    persistent_result = json.loads(
        terminal_tool_module.terminal_tool(
            command="sleep 60",
            background=True,
            task_id="sa-child",
            persist_on_abandon=True,
        )
    )

    ordinary = registry.get(ordinary_result["session_id"])
    persistent = registry.get(persistent_result["session_id"])
    assert ordinary is not None and persistent is not None
    assert ordinary.task_id == persistent.task_id == "parent-session"
    assert ordinary.environment_task_id == persistent.environment_task_id == "default"
    assert (
        ordinary.session_key
        == persistent.session_key
        == "agent:main:telegram:dm:parent"
    )
    assert registry.has_active_processes("default") is True

    assert registry.kill_started_since(
        "parent-session",
        frozenset(),
        source="gateway_turn_interrupt",
    ) == 1
    assert ordinary.exited is True
    assert persistent.exited is False
    assert kill_calls == [
        (
            ordinary.id,
            {"source": "gateway_turn_interrupt", "consume_output": True},
        )
    ]

    kill_calls.clear()
    assert registry.kill_all("parent-session") == 1
    assert persistent.exited is True
    assert kill_calls == [
        (
            persistent.id,
            {"source": "kill_all", "consume_output": False},
        )
    ]
