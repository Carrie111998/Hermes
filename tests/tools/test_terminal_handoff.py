"""Tests for automatic foreground-to-background handoff."""

import json

from tools import terminal_tool as terminal_module
from tools.process_registry import process_registry


def test_long_foreground_command_is_handed_to_background(monkeypatch):
    """A command past handoff_timeout keeps running and returns a process id."""
    monkeypatch.setenv("TERMINAL_HANDOFF_TIMEOUT", "1")
    monkeypatch.setenv("TERMINAL_TIMEOUT", "30")

    result = json.loads(
        terminal_module.terminal_tool(
            "sleep 4",
            timeout=30,
            force=True,
        )
    )

    assert result["status"] == "running"
    assert result["auto_handoff"] is True
    assert result["session_id"].startswith("proc_")

    try:
        assert process_registry.poll(result["session_id"])["status"] == "running"
    finally:
        process_registry.kill_process(result["session_id"])


def test_handoff_persists_session_cwd_after_completion(monkeypatch, tmp_path):
    """An adopted command updates the durable cwd after it exits."""
    monkeypatch.setenv("TERMINAL_HANDOFF_TIMEOUT", "1")
    monkeypatch.setenv("TERMINAL_TIMEOUT", "30")
    task_id = "handoff-cwd-test"
    from tools.terminal_tool import clear_session_cwd, get_session_cwd
    clear_session_cwd(task_id)

    result = json.loads(
        terminal_module.terminal_tool(
            f"cd {tmp_path}; sleep 2",
            timeout=30,
            task_id=task_id,
            force=True,
        )
    )
    assert result["status"] == "running", result

    try:
        waited = process_registry.wait(result["session_id"], timeout=5)
        assert waited["status"] == "exited", waited
        assert get_session_cwd(task_id) == str(tmp_path)
    finally:
        clear_session_cwd(task_id)


def test_handoff_failure_after_start_is_not_retried(monkeypatch):
    """A post-start handoff failure must not execute the command a second time."""
    monkeypatch.setattr(
        terminal_module,
        "_auto_handoff_foreground",
        lambda **_: {
            "output": "",
            "returncode": -1,
            "error": "handoff failed after start",
            "handoff_failed": True,
        },
    )
    execute_calls = []
    from tools.environments.local import LocalEnvironment
    monkeypatch.setattr(
        LocalEnvironment,
        "execute",
        lambda self, command, **kwargs: execute_calls.append(command),
    )

    result = json.loads(
        terminal_module.terminal_tool(
            "touch /tmp/should-not-run-twice",
            timeout=30,
            task_id="handoff-failure-test",
            force=True,
        )
    )

    assert execute_calls == []
    assert result["exit_code"] == -1
    assert result["error"] == "handoff failed after start"
