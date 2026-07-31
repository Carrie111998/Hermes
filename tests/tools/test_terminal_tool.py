"""Regression tests for sudo detection and sudo password handling."""

import json

import tools.terminal_tool as terminal_tool


def setup_function():
    terminal_tool._reset_cached_sudo_passwords()


def teardown_function():
    terminal_tool._reset_cached_sudo_passwords()


def test_searching_for_sudo_does_not_trigger_rewrite(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    command = "rg --line-number --no-heading --with-filename 'sudo' . | head -n 20"
    transformed, sudo_stdin = terminal_tool._transform_sudo_command(command)

    assert transformed == command
    assert sudo_stdin is None


def test_terminal_schema_advertises_persistent_env_state():
    description = terminal_tool.TERMINAL_TOOL_DESCRIPTION

    assert "exported environment variables persist between calls" in description
    assert "activate a virtualenv" in description
    assert "do not re-source the same environment before every command" in description


def test_printf_literal_sudo_does_not_trigger_rewrite(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    command = "printf '%s\\n' sudo"
    transformed, sudo_stdin = terminal_tool._transform_sudo_command(command)

    assert transformed == command
    assert sudo_stdin is None


def test_non_command_argument_named_sudo_does_not_trigger_rewrite(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    command = "grep -n sudo README.md"
    transformed, sudo_stdin = terminal_tool._transform_sudo_command(command)

    assert transformed == command
    assert sudo_stdin is None


def test_actual_sudo_command_uses_configured_password(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "testpass")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    transformed, sudo_stdin = terminal_tool._transform_sudo_command("sudo apt install -y ripgrep")

    assert transformed == "sudo -S -p '' apt install -y ripgrep"
    assert sudo_stdin == "testpass\n"


def test_explicit_empty_sudo_password_tries_empty_without_prompt(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "")
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")

    def _fail_prompt(*_args, **_kwargs):
        raise AssertionError("interactive sudo prompt should not run for explicit empty password")

    monkeypatch.setattr(terminal_tool, "_prompt_for_sudo_password", _fail_prompt)

    transformed, sudo_stdin = terminal_tool._transform_sudo_command("sudo true")

    assert transformed == "sudo -S -p '' true"
    assert sudo_stdin == "\n"


def test_validate_workdir_blocks_shell_metacharacters_in_windows_paths():
    assert terminal_tool._validate_workdir(r"C:\Users\Alice\project; rm -rf /")
    assert terminal_tool._validate_workdir(r"C:\Users\Alice\project$(whoami)")
    assert terminal_tool._validate_workdir("C:\\Users\\Alice\\project\nwhoami")


def test_count_real_sudo_invocations_ignores_mentions(monkeypatch):
    assert terminal_tool._count_real_sudo_invocations("grep sudo README.md") == 0
    assert terminal_tool._count_real_sudo_invocations("sudo a; sudo b") == 2


class TestExecutionWriteScope:
    def _config(self, scope, cwd):
        return {
            "env_type": "local",
            "execution_write_scope": scope,
            "cwd": str(cwd),
            "timeout": 30,
        }

    def test_workspace_local_rejects_foreground_before_environment_creation(self, monkeypatch, tmp_path):
        spawned = []
        monkeypatch.setattr(
            terminal_tool,
            "_get_env_config",
            lambda: self._config("workspace", tmp_path),
        )
        monkeypatch.setattr(terminal_tool, "_create_environment", lambda *a, **k: spawned.append(True))

        result = json.loads(
            terminal_tool.terminal_tool(
                "printf blocked",
                task_id="terminal-foreground",
                force=True,
            )
        )

        assert result["error_code"] == "unsupported_execution_backend"
        assert not spawned

    def test_workspace_local_rejects_background_and_pty_before_spawn(self, monkeypatch, tmp_path):
        spawned = []
        monkeypatch.setattr(
            terminal_tool,
            "_get_env_config",
            lambda: self._config("workspace", tmp_path),
        )
        monkeypatch.setattr(terminal_tool, "_create_environment", lambda *a, **k: spawned.append(True))

        for kwargs in ({"background": True}, {"pty": True}):
            result = json.loads(
                terminal_tool.terminal_tool(
                    "printf blocked",
                    task_id=f"terminal-{len(spawned)}",
                    force=True,
                    **kwargs,
                )
            )
            assert result["error_code"] == "unsupported_execution_backend"
        assert not spawned

    def test_legacy_execution_write_scope_preserves_current_terminal_behavior(
        self, monkeypatch, tmp_path
    ):
        class FakeEnvironment:
            cwd = str(tmp_path)

            def execute(self, command, **kwargs):
                return {"output": "legacy", "returncode": 0}

        monkeypatch.setattr(
            terminal_tool,
            "_get_env_config",
            lambda: self._config("legacy", tmp_path),
        )
        monkeypatch.setattr(terminal_tool, "_active_environments", {})
        monkeypatch.setattr(terminal_tool, "_last_activity", {})
        monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
        monkeypatch.setattr(terminal_tool, "_create_environment", lambda *a, **k: FakeEnvironment())
        monkeypatch.setattr(terminal_tool, "_check_all_guards", lambda *a, **k: {"approved": True})

        result = json.loads(
            terminal_tool.terminal_tool(
                "printf legacy",
                task_id="terminal-legacy",
                force=True,
            )
        )

        assert result["output"] == "legacy"
        assert result["exit_code"] == 0
        assert terminal_tool._active_environments["default"] is not None

    def test_legacy_cleanup_preserves_shared_default_environment(
        self, monkeypatch
    ):
        class FakeEnvironment:
            def __init__(self):
                self.cleanup_calls = []

            def cleanup(self, *, force_remove=False):
                self.cleanup_calls.append(force_remove)

        shared = FakeEnvironment()
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": shared})
        monkeypatch.setattr(terminal_tool, "_last_activity", {"default": 1.0})

        terminal_tool.cleanup_vm("gateway-session-A", force_remove=True)

        assert shared.cleanup_calls == []
        assert terminal_tool._active_environments["default"] is shared

        terminal_tool.cleanup_vm("default", force_remove=True)
        assert shared.cleanup_calls == [True]
        assert "default" not in terminal_tool._active_environments

    def test_get_active_env_legacy_fast_path_skips_policy_resolution(
        self, monkeypatch
    ):
        live_environment = object()
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": live_environment})
        monkeypatch.setattr(
            terminal_tool,
            "_get_env_config",
            lambda: (_ for _ in ()).throw(AssertionError("legacy lookup loaded config")),
        )
        monkeypatch.setattr(
            terminal_tool,
            "_resolve_execution_policy_for_task",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("legacy lookup resolved policy")
            ),
        )

        assert terminal_tool.get_active_env("gateway-session-A") is live_environment

    def test_workspace_cleanup_resolves_raw_session_to_bounded_identity(
        self, monkeypatch, tmp_path
    ):
        from tools.environments.execution_policy import (
            clear_execution_workspace,
            lookup_policy_environment_key,
            resolve_execution_write_policy,
        )
        import tools.file_tools as file_tools
        import tools.process_registry as process_registry_module

        class FakeEnvironment:
            def __init__(self):
                self.cleanup_calls = []

            def cleanup(self, *, force_remove=False):
                self.cleanup_calls.append(force_remove)

        class FakeRegistry:
            def __init__(self):
                self.killed = []

            def kill_all(self, *, task_id=None):
                self.killed.append(task_id)
                return 1

        raw_session = "raw-session-cleanup"
        clear_execution_workspace(raw_session)
        policy = resolve_execution_write_policy(
            "workspace",
            session_id=raw_session,
            workspace_root=str(tmp_path),
            backend="docker",
        )
        bounded_key = terminal_tool._resolve_environment_key(raw_session, policy)
        env = FakeEnvironment()
        registry = FakeRegistry()
        monkeypatch.setattr(terminal_tool, "_active_environments", {bounded_key: env})
        monkeypatch.setattr(terminal_tool, "_last_activity", {bounded_key: 1.0})
        monkeypatch.setattr(process_registry_module, "process_registry", registry)
        with file_tools._file_ops_lock:
            file_tools._file_ops_cache[bounded_key] = object()

        try:
            terminal_tool.cleanup_vm(raw_session, force_remove=True)
            assert env.cleanup_calls == [True]
            assert registry.killed == [bounded_key]
            assert bounded_key not in terminal_tool._active_environments
            assert bounded_key not in file_tools._file_ops_cache
            assert lookup_policy_environment_key(raw_session) is None
        finally:
            with file_tools._file_ops_lock:
                file_tools._file_ops_cache.pop(bounded_key, None)
            clear_execution_workspace(raw_session)
