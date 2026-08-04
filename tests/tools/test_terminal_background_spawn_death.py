"""Regression tests: a background spawn that dies must not be reported as started.

``terminal(background=True)`` returned ``"Background process started"`` plus a
``session_id`` unconditionally — even when the process had already died during
spawn. On a non-local backend (SSH, Docker, Modal, Daytona) the wrapper can fail
to produce a PID (unwritable redirect target, broken login shell, transport
error), and ``ProcessRegistry.spawn_via_env`` marks the session
``completion_reason == "failed_start"`` and never registers it as running. The
agent was still handed a handle it could never poll — ``process(action="poll")``
answers ``not_found`` — so the real failure stayed invisible until someone read
the logs by hand.

Note the local ``Popen`` path cannot reach this state: it always obtains a PID,
and a bad command merely exits 127 *after* a successful spawn (a real, pollable
session). ``failed_start`` is specific to the ``env.execute`` backends, which is
why these tests drive a fake environment.

The assertions target the contract, not message strings:

1. spawn death => an error payload, and no ``session_id`` the caller could
   mistake for a pollable handle;
2. the surfaced output carries no unredacted credential from the command line;
3. a healthy spawn still returns the normal started payload — without this an
   over-broad guard that fired on every spawn would satisfy (1) and (2).
"""

import json

import pytest

import tools.terminal_tool as terminal_tool


SECRET = "sk-live-51HxXaMPLe0000000000000000000000000000000000000000"
COMMAND_WITH_SECRET = f"./deploy.sh --api-key={SECRET}"


class _BrokenSpawnEnvironment:
    """Non-local backend whose background wrapper never emits a PID."""

    env: dict = {}

    def execute(self, command, timeout=None, rewrite_compound_background=False, **kwargs):
        # Echo the command back the way a failing shell wrapper does, so the
        # credential on the command line reaches the captured output.
        return {
            "output": f"bash: cannot create log file for: {command}",
            "returncode": 1,
        }


class _HealthySpawnEnvironment:
    """Non-local backend whose wrapper prints a PID, i.e. a real launch."""

    env: dict = {}

    def execute(self, command, timeout=None, rewrite_compound_background=False, **kwargs):
        return {"output": "4242", "returncode": 0}


@pytest.fixture
def background_env(monkeypatch):
    """Route terminal_tool at a caller-supplied fake non-local environment."""

    def _install(environment, task_id="spawn-contract"):
        monkeypatch.setattr(
            terminal_tool,
            "_get_env_config",
            lambda: {
                "env_type": "ssh",
                "cwd": "/tmp",
                "timeout": 60,
                "lifetime_seconds": 3600,
            },
        )
        monkeypatch.setattr(
            terminal_tool,
            "_check_all_guards",
            lambda command, env_type, **kwargs: {"approved": True},
        )
        monkeypatch.setattr(
            terminal_tool, "_active_environments", {task_id: environment}
        )
        monkeypatch.setattr(terminal_tool, "_last_activity", {})
        return task_id

    return _install


class TestSpawnDeathIsReportedAsFailure:
    def test_dead_spawn_does_not_report_success(self, background_env):
        task_id = background_env(_BrokenSpawnEnvironment())

        result = json.loads(
            terminal_tool.terminal_tool(
                command="./run-server.sh", background=True, task_id=task_id
            )
        )

        assert result.get("error")
        assert result.get("exit_code") != 0

    def test_dead_spawn_hands_back_no_pollable_handle(self, background_env):
        """A session id the caller cannot poll is worse than no id at all."""
        from tools.process_registry import process_registry

        task_id = background_env(_BrokenSpawnEnvironment())

        result = json.loads(
            terminal_tool.terminal_tool(
                command="./run-server.sh", background=True, task_id=task_id
            )
        )

        session_id = result.get("session_id")
        if session_id is not None:
            # If an id is surfaced at all, it must actually resolve.
            assert process_registry.poll(session_id).get("status") != "not_found"


class TestSpawnFailureOutputIsRedacted:
    def test_credential_from_command_line_is_not_echoed(self, background_env):
        task_id = background_env(_BrokenSpawnEnvironment())

        raw = terminal_tool.terminal_tool(
            command=COMMAND_WITH_SECRET, background=True, task_id=task_id
        )

        assert SECRET not in raw


class TestHealthySpawnIsUnaffected:
    """Load-bearing: an over-broad guard firing on every spawn must fail here."""

    def test_healthy_spawn_still_returns_started_payload(self, background_env):
        task_id = background_env(_HealthySpawnEnvironment())

        result = json.loads(
            terminal_tool.terminal_tool(
                command="./run-server.sh", background=True, task_id=task_id
            )
        )

        assert result.get("error") is None
        assert result.get("exit_code") == 0
        assert result.get("session_id")

    def test_healthy_spawn_handle_is_pollable(self, background_env):
        from tools.process_registry import process_registry

        task_id = background_env(_HealthySpawnEnvironment())

        result = json.loads(
            terminal_tool.terminal_tool(
                command="./run-server.sh", background=True, task_id=task_id
            )
        )

        polled = process_registry.poll(result["session_id"])
        assert polled.get("status") != "not_found"
