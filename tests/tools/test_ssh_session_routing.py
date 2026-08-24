"""Routing contracts for task-scoped SSH gateway bindings."""

from __future__ import annotations


def _ssh_overrides(host: str = "build.example") -> dict:
    return {
        "env_type": "ssh",
        "ssh_alias": "build-box",
        "ssh_host": host,
        "ssh_user": "runner",
        "ssh_port": 2222,
        "ssh_key": "/keys/id_ed25519",
        "ssh_persistent": True,
        "cwd": "/srv/project",
    }


def test_effective_terminal_config_uses_task_ssh_binding(monkeypatch):
    from tools import terminal_tool as terminal

    monkeypatch.setenv("TERMINAL_ENV", "local")
    task_id = "ssh-config-session"
    terminal.register_task_env_overrides(task_id, _ssh_overrides())
    try:
        config = terminal.get_effective_env_config(task_id)
    finally:
        terminal.clear_task_env_overrides(task_id)

    assert config["env_type"] == "ssh"
    assert config["ssh_host"] == "build.example"
    assert config["ssh_user"] == "runner"
    assert config["ssh_port"] == 2222
    assert config["ssh_key"] == "/keys/id_ed25519"
    assert config["cwd"] == "/srv/project"


def test_backend_change_evicts_environment_but_identical_binding_does_not():
    from tools import terminal_tool as terminal

    task_id = "ssh-eviction-session"

    class FakeEnvironment:
        cleaned = False

        def cleanup(self):
            self.cleaned = True

    stable = FakeEnvironment()
    terminal.register_task_env_overrides(task_id, _ssh_overrides())
    with terminal._env_lock:
        terminal._active_environments[task_id] = stable
    try:
        terminal.register_task_env_overrides(task_id, _ssh_overrides())
        with terminal._env_lock:
            assert terminal._active_environments.get(task_id) is stable
        assert stable.cleaned is False

        terminal.register_task_env_overrides(
            task_id,
            _ssh_overrides(host="replacement.example"),
        )
        with terminal._env_lock:
            assert task_id not in terminal._active_environments
        assert stable.cleaned is True
    finally:
        terminal.clear_task_env_overrides(task_id)
        with terminal._env_lock:
            terminal._active_environments.pop(task_id, None)
            terminal._last_activity.pop(task_id, None)


def test_file_tools_create_ssh_environment_from_task_binding(monkeypatch):
    from tools import file_tools
    from tools import terminal_tool as terminal

    captured: dict = {}
    task_id = "ssh-file-session"
    terminal.register_task_env_overrides(task_id, _ssh_overrides())

    class FakeEnvironment:
        cwd = "/srv/project"

    class FakeFileOperations:
        def __init__(self, env):
            self.env = env

    def fake_create_environment(**kwargs):
        captured.update(kwargs)
        return FakeEnvironment()

    monkeypatch.setattr(terminal, "_create_environment", fake_create_environment)
    monkeypatch.setattr(file_tools, "ShellFileOperations", FakeFileOperations)
    try:
        operations = file_tools._get_file_ops(task_id)
    finally:
        terminal.clear_task_env_overrides(task_id)
        file_tools.clear_file_ops_cache(task_id)
        with terminal._env_lock:
            terminal._active_environments.pop(task_id, None)
            terminal._last_activity.pop(task_id, None)

    assert isinstance(operations.env, FakeEnvironment)
    assert captured["env_type"] == "ssh"
    assert captured["cwd"] == "/srv/project"
    assert captured["ssh_config"] == {
        "host": "build.example",
        "user": "runner",
        "port": 2222,
        "key": "/keys/id_ed25519",
        "persistent": True,
        "binding_error": "",
    }


def test_file_paths_are_never_resolved_on_host_for_ssh(monkeypatch):
    from tools import file_tools

    monkeypatch.setattr(file_tools, "_is_ssh_backend_task", lambda task_id: True)
    monkeypatch.setattr(
        file_tools,
        "_authoritative_workspace_root",
        lambda task_id: "/srv/project",
    )

    lock_key, shell_path, display_path = file_tools._file_tool_paths_for_task(
        "src/app.py",
        "ssh-file-session",
    )

    assert lock_key == "/srv/project/src/app.py"
    assert shell_path == "src/app.py"
    assert display_path == "/srv/project/src/app.py"

    monkeypatch.setattr(
        file_tools,
        "_authoritative_workspace_root",
        lambda task_id: "~/project",
    )
    lock_key, shell_path, display_path = file_tools._file_tool_paths_for_task(
        "src/app.py",
        "ssh-file-session",
    )

    assert lock_key == "~/project/src/app.py"
    assert shell_path == "src/app.py"
    assert display_path == "~/project/src/app.py"


def test_execute_code_dispatches_to_task_ssh_backend(monkeypatch):
    from tools import code_execution_tool as code_execution
    from tools import terminal_tool as terminal

    task_id = "ssh-code-session"
    calls = []
    terminal.register_task_env_overrides(task_id, _ssh_overrides())

    def fake_execute_remote(code, remote_task_id, enabled_tools):
        calls.append((code, remote_task_id, enabled_tools))
        return '{"status":"success","remote":true}'

    monkeypatch.setattr(code_execution, "_execute_remote", fake_execute_remote)
    monkeypatch.setattr(
        "tools.approval.check_execute_code_guard",
        lambda code, env_type, **kwargs: {"approved": True},
    )
    try:
        result = code_execution.execute_code(
            "print('hello')",
            task_id=task_id,
            enabled_tools=["terminal"],
        )
    finally:
        terminal.clear_task_env_overrides(task_id)

    assert calls == [("print('hello')", task_id, ["terminal"])]
    assert '"remote":true' in result


def test_removed_target_blocks_terminal_without_local_fallback(monkeypatch):
    from tools import terminal_tool as terminal

    monkeypatch.setenv("TERMINAL_ENV", "local")
    task_id = "removed-ssh-target"
    terminal.register_task_env_overrides(
        task_id,
        {
            "env_type": "ssh",
            "ssh_alias": "retired-box",
            "ssh_host": "",
            "ssh_user": "",
            "ssh_binding_error": (
                "SSH target `retired-box` is no longer configured"
            ),
        },
    )
    try:
        result = terminal.terminal_tool("pwd", task_id=task_id)
    finally:
        terminal.clear_task_env_overrides(task_id)

    assert "no longer configured" in result
    assert '"status": "error"' in result
    assert '"cwd"' not in result


def test_execute_code_environment_uses_task_ssh_connection(monkeypatch):
    from tools import code_execution_tool as code_execution
    from tools import terminal_tool as terminal

    captured: dict = {}
    task_id = "ssh-code-environment"
    terminal.register_task_env_overrides(task_id, _ssh_overrides())

    class FakeEnvironment:
        pass

    def fake_create_environment(**kwargs):
        captured.update(kwargs)
        return FakeEnvironment()

    monkeypatch.setattr(terminal, "_create_environment", fake_create_environment)
    try:
        environment, env_type = code_execution._get_or_create_env(task_id)
    finally:
        terminal.clear_task_env_overrides(task_id)
        with terminal._env_lock:
            terminal._active_environments.pop(task_id, None)
            terminal._last_activity.pop(task_id, None)

    assert isinstance(environment, FakeEnvironment)
    assert env_type == "ssh"
    assert captured["env_type"] == "ssh"
    assert captured["cwd"] == "/srv/project"
    assert captured["ssh_config"]["host"] == "build.example"
    assert captured["ssh_config"]["user"] == "runner"


def test_execute_code_restores_bound_remote_cwd(monkeypatch):
    from tools import code_execution_tool as code_execution

    class FakeEnvironment:
        def __init__(self):
            self.cwd = "/srv/project"

        def get_temp_dir(self):
            return "/tmp"

        def execute(self, command, cwd="", timeout=None, **kwargs):
            effective_cwd = cwd or self.cwd
            if command.startswith("cd /tmp/hermes_exec_"):
                self.cwd = command.split(" && ", 1)[0].removeprefix("cd ")
                return {"output": "script output\n", "returncode": 0}
            self.cwd = effective_cwd
            if "command -v python3" in command:
                return {"output": "OK\n", "returncode": 0}
            return {"output": "", "returncode": 0}

    environment = FakeEnvironment()
    monkeypatch.setattr(
        code_execution,
        "_get_or_create_env",
        lambda task_id: (environment, "ssh"),
    )
    monkeypatch.setattr(
        code_execution,
        "_load_config",
        lambda: {"timeout": 30, "max_tool_calls": 5},
    )

    result = code_execution._execute_remote(
        "print('hello')",
        task_id="ssh-code-session",
        enabled_tools=[],
    )

    assert '"status": "success"' in result
    assert environment.cwd == "/srv/project"


def test_prompt_environment_hints_use_task_backend(monkeypatch):
    from agent import prompt_builder
    from tools import terminal_tool as terminal

    task_id = "ssh-prompt-session"
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("HERMES_SESSION_ID", task_id)
    monkeypatch.setattr(
        prompt_builder,
        "_probe_remote_backend",
        lambda backend: "  User: runner\n  Working directory: /srv/project",
    )
    terminal.register_task_env_overrides(task_id, _ssh_overrides())
    try:
        hints = prompt_builder.build_environment_hints()
    finally:
        terminal.clear_task_env_overrides(task_id)

    assert "Terminal backend: ssh" in hints
    assert "all operate inside this ssh environment" in hints
    assert "Current working directory:" not in hints


def test_local_runtime_preparation_preserves_unrelated_session_cwd(
    monkeypatch,
    tmp_path,
):
    from gateway.ssh_mode import GatewaySshModeMixin
    from tools import terminal_tool as terminal

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    task_id = "local-session"
    terminal.record_session_cwd(task_id, "/workspace/local")

    GatewaySshModeMixin()._prepare_ssh_runtime(
        session_key="unbound-session",
        task_id=task_id,
    )

    assert terminal.get_session_cwd(task_id) == "/workspace/local"
    terminal.clear_session_cwd(task_id)


def test_repeated_runtime_preparation_preserves_live_remote_cwd(
    monkeypatch,
    tmp_path,
):
    from gateway import ssh_bindings
    from gateway.ssh_bindings import set_ssh_binding
    from gateway.ssh_mode import GatewaySshModeMixin
    from gateway.ssh_targets import SshTarget
    from tools import terminal_tool as terminal

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    target = SshTarget(
        alias="build",
        host="build.example.invalid",
        user="builder",
        cwd="/srv/repo",
    )
    monkeypatch.setattr(
        ssh_bindings,
        "load_ssh_targets",
        lambda: [target],
    )
    session_key = "bound-session"
    task_id = "bound-task"
    set_ssh_binding(session_key, alias=target.alias)

    runner = GatewaySshModeMixin()
    try:
        runner._prepare_ssh_runtime(
            session_key=session_key,
            task_id=task_id,
        )
        terminal.record_session_cwd(task_id, "/srv/repo/subdir")

        runner._prepare_ssh_runtime(
            session_key=session_key,
            task_id=task_id,
        )

        assert terminal.get_session_cwd(task_id) == "/srv/repo/subdir"
    finally:
        terminal.clear_task_env_overrides(task_id)
