"""Tests for notify_on_complete background process feature.

Covers:
  - ProcessSession.notify_on_complete field
  - ProcessRegistry.completion_queue population on _move_to_finished()
  - Checkpoint persistence of notify_on_complete
  - Terminal tool schema includes notify_on_complete
  - Terminal tool handler passes notify_on_complete through
"""

import json
import os
import time
import pytest
from unittest.mock import MagicMock, patch

from tools.process_registry import (
    ProcessRegistry,
    ProcessSession,
)


@pytest.fixture()
def registry():
    """Create a fresh ProcessRegistry."""
    return ProcessRegistry()


def _make_session(
    sid="proc_test_notify",
    command="echo hello",
    task_id="t1",
    exited=False,
    exit_code=None,
    output="",
    notify_on_complete=False,
) -> ProcessSession:
    s = ProcessSession(
        id=sid,
        command=command,
        task_id=task_id,
        started_at=time.time(),
        exited=exited,
        exit_code=exit_code,
        output_buffer=output,
        notify_on_complete=notify_on_complete,
    )
    return s


# =========================================================================
# ProcessSession field
# =========================================================================

class TestProcessSessionField:
    def test_default_false(self):
        s = ProcessSession(id="proc_1", command="echo hi")
        assert s.notify_on_complete is False

    def test_set_true(self):
        s = ProcessSession(id="proc_1", command="echo hi", notify_on_complete=True)
        assert s.notify_on_complete is True


# =========================================================================
# Completion queue
# =========================================================================

class TestCompletionQueue:
    def test_queue_exists(self, registry):
        assert hasattr(registry, "completion_queue")
        assert registry.completion_queue.empty()


    def test_move_to_finished_idempotent_no_duplicate(self, registry):
        """Calling _move_to_finished twice must NOT enqueue two notifications.

        Regression test: kill_process() and the reader thread can both call
        _move_to_finished() for the same session, producing duplicate
        [SYSTEM: Background process ...] messages.
        """
        s = _make_session(notify_on_complete=True, output="done", exit_code=-15)
        s.exited = True
        s.exit_code = -15
        registry._running[s.id] = s
        with patch.object(registry, "_write_checkpoint"):
            registry._move_to_finished(s)  # first call — should enqueue
            s.exit_code = 143  # reader thread updates exit code
            registry._move_to_finished(s)  # second call — should be no-op

        assert registry.completion_queue.qsize() == 1
        completion = registry.completion_queue.get_nowait()
        assert completion["exit_code"] == -15  # from the first (kill) call


    def test_output_truncated_to_2000(self, registry):
        """Long output is truncated to last 2000 chars."""
        long_output = "x" * 5000
        s = _make_session(
            notify_on_complete=True,
            output=long_output,
        )
        s.exited = True
        s.exit_code = 0
        registry._running[s.id] = s
        with patch.object(registry, "_write_checkpoint"):
            registry._move_to_finished(s)

        completion = registry.completion_queue.get_nowait()
        assert len(completion["output"]) == 2000

    def test_multiple_completions_queued(self, registry):
        """Multiple notify processes all push to the same queue."""
        for i in range(3):
            s = _make_session(
                sid=f"proc_{i}",
                notify_on_complete=True,
                output=f"output_{i}",
            )
            s.exited = True
            s.exit_code = 0
            registry._running[s.id] = s
            with patch.object(registry, "_write_checkpoint"):
                registry._move_to_finished(s)

        completions = []
        while not registry.completion_queue.empty():
            completions.append(registry.completion_queue.get_nowait())
        assert len(completions) == 3
        ids = {c["session_id"] for c in completions}
        assert ids == {"proc_0", "proc_1", "proc_2"}

    def test_arming_after_ultrafast_exit_enqueues_exactly_once(self, registry):
        session = _make_session(notify_on_complete=False, output="done")
        session.exited = True
        session.exit_code = 0
        registry._finished[session.id] = session

        registry.arm_completion_notification(session)
        registry.arm_completion_notification(session)

        assert registry.completion_queue.qsize() == 1
        completion = registry.completion_queue.get_nowait()
        assert completion["session_id"] == session.id
        assert completion["exit_code"] == 0

    def test_ultrafast_local_processes_never_lose_completion(self, registry, tmp_path):
        with patch.object(registry, "_write_checkpoint"):
            sessions = []
            for _ in range(50):
                session = registry.spawn_local("true", cwd=str(tmp_path))
                registry.arm_completion_notification(session)
                sessions.append(session)

            assert all(session._completion_event.wait(5) for session in sessions)

        completions = []
        while not registry.completion_queue.empty():
            completions.append(registry.completion_queue.get_nowait())
        assert len(completions) == len(sessions)
        assert {event["session_id"] for event in completions} == {
            session.id for session in sessions
        }


# =========================================================================
# Checkpoint persistence
# =========================================================================

class TestCheckpointNotify:
    def test_checkpoint_includes_notify(self, registry, tmp_path):
        with patch("tools.process_registry.CHECKPOINT_PATH", tmp_path / "procs.json"):
            s = _make_session(notify_on_complete=True)
            registry._running[s.id] = s
            registry._write_checkpoint()

            data = json.loads((tmp_path / "procs.json").read_text())
            assert len(data) == 1
            assert data[0]["notify_on_complete"] is True


    def test_recover_defaults_false(self, registry, tmp_path):
        """Old checkpoint entries without the field default to False."""
        checkpoint = tmp_path / "procs.json"
        checkpoint.write_text(json.dumps([{
            "session_id": "proc_live",
            "command": "sleep 999",
            "pid": os.getpid(),
            "task_id": "t1",
        }]))
        with patch("tools.process_registry.CHECKPOINT_PATH", checkpoint):
            recovered = registry.recover_from_checkpoint()
            assert recovered == 1
            s = registry.get("proc_live")
            assert s.notify_on_complete is False


# =========================================================================
# Terminal tool schema
# =========================================================================

class TestTerminalSchema:
    def test_schema_has_notify_on_complete(self):
        from tools.terminal_tool import TERMINAL_SCHEMA
        props = TERMINAL_SCHEMA["parameters"]["properties"]
        assert "notify_on_complete" in props
        assert props["notify_on_complete"]["type"] == "boolean"
        assert props["notify_on_complete"]["default"] is False

    def test_handler_passes_notify(self):
        """_handle_terminal passes notify_on_complete to terminal_tool."""
        from tools.terminal_tool import _handle_terminal
        with patch("tools.terminal_tool.terminal_tool", return_value='{"ok":true}') as mock_tt:
            _handle_terminal(
                {"command": "echo hi", "background": True, "notify_on_complete": True},
                task_id="t1",
            )
            _, kwargs = mock_tt.call_args
            assert kwargs["notify_on_complete"] is True


# =========================================================================
# Code execution blocked params
# =========================================================================

class TestCodeExecutionBlocked:
    def test_notify_on_complete_blocked_in_sandbox(self):
        from tools.code_execution_tool import _TERMINAL_BLOCKED_PARAMS
        assert "notify_on_complete" in _TERMINAL_BLOCKED_PARAMS

    def test_ci_wait_timeout_blocked_in_sandbox(self):
        from tools.code_execution_tool import _TERMINAL_BLOCKED_PARAMS
        assert "ci_wait_timeout" in _TERMINAL_BLOCKED_PARAMS


# =========================================================================
# Completion consumed suppression
# =========================================================================

class TestCompletionConsumed:
    """Test that wait/log consume completion notifications while poll stays read-only."""

    def test_wait_marks_completion_consumed(self, registry):
        """wait() returning exited status marks session as consumed."""
        s = _make_session(sid="proc_wait", notify_on_complete=True, output="done")
        s.exited = True
        s.exit_code = 0
        registry._running[s.id] = s
        with patch.object(registry, "_write_checkpoint"):
            registry._move_to_finished(s)

        # Notification is in the queue
        assert not registry.completion_queue.empty()
        assert not registry.is_completion_consumed("proc_wait")

        # Agent calls wait() — gets the result directly
        result = registry.wait("proc_wait", timeout=1)
        assert result["status"] == "exited"

        # Now the completion is marked as consumed
        assert registry.is_completion_consumed("proc_wait")


    def test_poll_observed_does_not_suppress_gateway_watcher(self, registry):
        """The gateway/tui watcher gate (is_completion_consumed) must stay False
        after a read-only poll, so the autonomous delivery turn still fires
        even though the CLI drain was deduped (#10156)."""
        s = _make_session(sid="proc_gw", notify_on_complete=True, output="done")
        s.exited = True
        s.exit_code = 0
        registry._finished[s.id] = s

        registry.poll("proc_gw")
        # CLI-side dedup signal present...
        assert "proc_gw" in registry._poll_observed
        # ...but the gateway watcher gate is untouched, so it still delivers.
        assert not registry.is_completion_consumed("proc_gw")

    def test_running_poll_does_not_mark_poll_observed(self, registry):
        """poll() on a still-running process must not record _poll_observed."""
        s = _make_session(sid="proc_run2", notify_on_complete=True, output="partial")
        registry._running[s.id] = s

        registry.poll("proc_run2")
        assert "proc_run2" not in registry._poll_observed

    def test_wait_and_log_still_skip_cli_drain(self, registry):
        """wait()/read_log() consume the output, so the CLI drain skips their
        completions via _completion_consumed (the original #8228 contract)."""
        for sid, action in (("proc_w", "wait"), ("proc_l", "log")):
            s = _make_session(sid=sid, notify_on_complete=True, output="done")
            s.exited = True
            s.exit_code = 0
            registry._running[s.id] = s
            with patch.object(registry, "_write_checkpoint"):
                registry._move_to_finished(s)
            if action == "wait":
                registry.wait(sid, timeout=1)
            else:
                registry.read_log(sid)
            assert registry.is_completion_consumed(sid)
        assert registry.drain_notifications() == []


# ---------------------------------------------------------------------------
# Silent-background-process hint
#
# background=True without notify_on_complete=True OR watch_patterns runs
# the process silently — the agent has no way to learn it finished short
# of calling process(action="poll") explicitly. The tool result must
# include a "hint" field that nudges the agent toward
# notify_on_complete=True for bounded tasks. May 2026 PR #31231 incident:
# bg CI poller exited green, agent never noticed, user had to surface it.
# ---------------------------------------------------------------------------


def _silent_bg_base_config(tmp_path):
    return {
        "env_type": "local",
        "docker_image": "",
        "singularity_image": "",
        "modal_image": "",
        "daytona_image": "",
        "cwd": str(tmp_path),
        "timeout": 30,
    }


def _silent_bg_harness(monkeypatch, tmp_path):
    """Common test fixture: patch enough of terminal_tool to spawn a fake
    background process and capture the JSON result the agent sees."""
    import tools.terminal_tool as terminal_tool_module
    from tools import process_registry as process_registry_module
    from types import SimpleNamespace

    config = _silent_bg_base_config(tmp_path)
    dummy_env = SimpleNamespace(env={})

    def fake_spawn_local(**kwargs):
        return ProcessSession(
            id="proc_silent_test",
            command=kwargs["command"],
            pid=4242,
        )

    monkeypatch.setattr(terminal_tool_module, "_get_env_config", lambda: config)
    monkeypatch.setattr(terminal_tool_module, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool_module, "_check_all_guards", lambda *_args, **_kwargs: {"approved": True})
    monkeypatch.setattr(process_registry_module.process_registry, "spawn_local", fake_spawn_local)
    monkeypatch.setitem(terminal_tool_module._active_environments, "default", dummy_env)
    monkeypatch.setitem(terminal_tool_module._last_activity, "default", 0.0)
    return terminal_tool_module


def test_background_without_notify_emits_silent_process_hint(monkeypatch, tmp_path):
    """The footgun case (May 2026 PR #31231): bg=True alone runs silently
    and the agent has no signal it finished. Tool must nudge."""
    tt = _silent_bg_harness(monkeypatch, tmp_path)
    try:
        result = json.loads(
            tt.terminal_tool(
                command="while true; do gh pr checks 999; sleep 30; done",
                background=True,
            )
        )
    finally:
        tt._active_environments.pop("default", None)
        tt._last_activity.pop("default", None)

    assert result["session_id"] == "proc_silent_test"
    hint = result.get("hint", "")
    assert hint, "Silent background process must include a hint field"
    assert "notify_on_complete" in hint, (
        "Hint must name the corrective flag so the agent can self-correct"
    )
    assert "silent" in hint.lower() or "no way to learn" in hint.lower(), (
        "Hint must explain the failure mode, not just suggest the fix"
    )


def test_background_with_notify_does_not_emit_hint(monkeypatch, tmp_path):
    """The correct shape — bg+notify together — must not nag."""
    tt = _silent_bg_harness(monkeypatch, tmp_path)
    try:
        result = json.loads(
            tt.terminal_tool(
                command="pytest tests/",
                background=True,
                notify_on_complete=True,
            )
        )
    finally:
        tt._active_environments.pop("default", None)
        tt._last_activity.pop("default", None)

    assert "hint" not in result, (
        f"Correct usage must not emit a hint, got: {result.get('hint')!r}"
    )
    assert result.get("notify_on_complete") is True


def test_foreground_command_does_not_emit_hint(monkeypatch, tmp_path):
    """Hint only applies to background processes — foreground returns its
    result synchronously and the agent always sees the outcome."""
    tt = _silent_bg_harness(monkeypatch, tmp_path)

    # Foreground path doesn't go through spawn_local. Patch the local-env
    # exec method to short-circuit to a clean exit so the test doesn't
    # actually shell out.
    from types import SimpleNamespace
    dummy_env = SimpleNamespace(
        env={},
        execute=lambda *a, **kw: {"output": "done", "exit_code": 0, "error": None},
    )
    monkeypatch.setitem(tt._active_environments, "default", dummy_env)

    try:
        result = json.loads(
            tt.terminal_tool(
                command="echo hello",
                background=False,
            )
        )
    finally:
        tt._active_environments.pop("default", None)
        tt._last_activity.pop("default", None)

    assert "hint" not in result, (
        f"Foreground commands must not emit the background-silence hint, got: {result.get('hint')!r}"
    )


# ---------------------------------------------------------------------------
# Homebrewed-CI-watcher hint
#
# Background processes whose command looks like a hand-rolled CI poller
# (`gh pr view` / `gh pr checks` combined with jq/awk on stdout) get an
# additional hint pointing at the canonical green-ci-policy snippet. The
# homebrew shape has burned us repeatedly (May 2026 PRs #31329, #31448,
# #31695, #31709, #31745, #32264, #33131) with stdout buffering, jq null
# keys, conclusion-vs-status confusion, and TTY-only banner grepping —
# none of which the canonical snippets suffer from. Fire on every detection;
# false positives are cheap (~one read).
# ---------------------------------------------------------------------------


def test_non_ci_background_command_does_not_emit_homebrew_hint(monkeypatch, tmp_path):
    """A long-running task that happens to use awk for unrelated reasons
    must not be mistaken for a CI poller — the gating signal is the
    combination of `gh pr ...` AND a stdout parser."""
    tt = _silent_bg_harness(monkeypatch, tmp_path)
    try:
        result = json.loads(
            tt.terminal_tool(
                command="cat /var/log/syslog | awk '/error/ {print}' > /tmp/errs.log",
                background=True,
                notify_on_complete=True,
            )
        )
    finally:
        tt._active_environments.pop("default", None)
        tt._last_activity.pop("default", None)

    assert "hint" not in result, (
        f"Non-CI command using awk must not be flagged as homebrew CI poller, got: {result.get('hint')!r}"
    )


# ---------------------------------------------------------------------------
# Bounded CI wait promotion
# ---------------------------------------------------------------------------


def test_terminal_schema_exposes_bounded_ci_wait():
    from tools.terminal_tool import TERMINAL_SCHEMA

    prop = TERMINAL_SCHEMA["parameters"]["properties"]["ci_wait_timeout"]
    assert prop["type"] == "integer"
    assert prop["minimum"] == 30
    assert prop["maximum"] == 3600


@pytest.mark.parametrize(
    "command",
    [
        "gh pr checks --watch",
        "gh pr checks 123 | cat",
        "gh pr checks 123 && echo unsafe",
        "gh issue checks 123",
    ],
)
def test_ci_wait_rejects_unrecognized_command_shapes(command):
    from tools.terminal_tool import _parse_gh_pr_checks

    assert _parse_gh_pr_checks(command) is None


def test_ci_wait_builds_checks_and_sha_drift_guard():
    from tools.terminal_tool import _build_bounded_ci_wait_command, _parse_gh_pr_checks

    parsed = _parse_gh_pr_checks("gh pr checks 123 --required --repo owner/repo")
    assert parsed is not None

    wrapper = _build_bounded_ci_wait_command(parsed, timeout=300)

    assert "gh pr checks 123 --required --repo owner/repo" in wrapper
    assert "gh pr view 123 --repo owner/repo --json headRefOid" in wrapper
    assert "CI_WAIT_SUCCESS" in wrapper
    assert "CI_WAIT_FAILURE" in wrapper
    assert "CI_WAIT_TIMEOUT" in wrapper
    assert "CI_WAIT_SHA_DRIFT" in wrapper


def test_pending_ci_check_promotes_to_notifying_background_wait(monkeypatch, tmp_path):
    tt = _silent_bg_harness(monkeypatch, tmp_path)
    from tools import process_registry as process_registry_module
    from types import SimpleNamespace

    spawned = {}

    class PendingEnv:
        env = {}
        cwd = str(tmp_path)

        def execute(self, command, **kwargs):
            if command.startswith("gh pr view"):
                return {"returncode": 0, "output": f"{1:040d}"}
            assert command == "gh pr checks 123 --required"
            return {"returncode": 8, "output": "build\tpending"}

    def fake_spawn_local(**kwargs):
        spawned.update(kwargs)
        return ProcessSession(
            id="proc_ci_wait",
            command=kwargs["command"],
            pid=4242,
        )

    monkeypatch.setitem(tt._active_environments, "default", PendingEnv())
    monkeypatch.setattr(process_registry_module.process_registry, "spawn_local", fake_spawn_local)
    monkeypatch.setattr("gateway.session_context.async_delivery_supported", lambda: True)
    try:
        result = json.loads(
            tt.terminal_tool(
                command="gh pr checks 123 --required",
                ci_wait_timeout=300,
            )
        )
    finally:
        tt._active_environments.pop("default", None)
        tt._last_activity.pop("default", None)

    assert result["exit_code"] == 8
    assert result["ci_wait_status"] == "pending"
    assert result["ci_wait_session_id"] == "proc_ci_wait"
    assert result["notify_on_complete"] is True
    assert "CI_WAIT_SHA_DRIFT" in spawned["command"]
    assert f"ci_wait_expected_sha={1:040d}" in spawned["command"]


@pytest.mark.parametrize(
    ("kill_status", "expected_wait_status"),
    [
        ("error", "cleanup_failed"),
        ("not_found", "cleanup_failed"),
        ("killed", "not_started"),
        ("already_exited", "not_started"),
    ],
)
def test_pending_ci_reports_verified_waiter_cleanup(
    monkeypatch, tmp_path, kill_status, expected_wait_status
):
    """Claim cleanup only for registry statuses that prove the waiter stopped."""
    tt = _silent_bg_harness(monkeypatch, tmp_path)
    from tools import process_registry as process_registry_module

    class PendingEnv:
        env = {}
        cwd = str(tmp_path)

        def execute(self, command, **kwargs):
            if command.startswith("gh pr view"):
                return {"returncode": 0, "output": f"{1:040d}"}
            assert command == "gh pr checks 123"
            return {"returncode": 8, "output": "build\tpending"}

    def fake_spawn_local(**kwargs):
        return ProcessSession(
            id="proc_cleanup_failed",
            command=kwargs["command"],
            pid=4242,
        )

    monkeypatch.setitem(tt._active_environments, "default", PendingEnv())
    monkeypatch.setattr(process_registry_module.process_registry, "spawn_local", fake_spawn_local)
    monkeypatch.setattr(
        process_registry_module.process_registry,
        "kill_process",
        lambda _session_id: {"status": kill_status, "error": "synthetic refusal"},
    )
    monkeypatch.setattr("gateway.session_context.async_delivery_supported", lambda: False)
    try:
        result = json.loads(
            tt.terminal_tool(
                command="gh pr checks 123",
                ci_wait_timeout=300,
            )
        )
    finally:
        tt._active_environments.pop("default", None)
        tt._last_activity.pop("default", None)

    assert result["status"] == "error"
    assert result["ci_wait_status"] == expected_wait_status
    if expected_wait_status == "cleanup_failed":
        assert result["ci_wait_session_id"] == "proc_cleanup_failed"
        assert "could not be verified" in result["error"]
        assert "no CI waiter was left running" not in result["error"]
    else:
        assert "ci_wait_session_id" not in result
        assert "no CI waiter was left running" in result["error"]


def test_promoted_ci_wait_enqueues_completion_wakeup(monkeypatch, tmp_path):
    """Prove the promoted waiter reaches the existing async completion rail."""
    tt = _silent_bg_harness(monkeypatch, tmp_path)
    from tools import process_registry as process_registry_module
    from tools.process_registry import ProcessRegistry

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gh = fake_bin / "gh"
    gh.write_text(
        """#!/bin/bash
if [ "$1 $2" = "pr view" ]; then
  printf '%040d\n' 1
  exit 0
fi
printf 'build\tpass\n'
exit 0
"""
    )
    gh.chmod(0o755)
    fake_shell = fake_bin / "shell"
    fake_shell.write_text('#!/bin/bash\nexec /bin/bash -c "$2"\n')
    fake_shell.chmod(0o755)

    class PendingEnv:
        cwd = str(tmp_path)
        env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}

        def execute(self, command, **kwargs):
            if command.startswith("gh pr view"):
                return {"returncode": 0, "output": f"{1:040d}"}
            assert command == "gh pr checks 123"
            return {"returncode": 8, "output": "build\tpending"}

    fresh_registry = ProcessRegistry()
    monkeypatch.setattr(process_registry_module, "process_registry", fresh_registry)
    monkeypatch.setattr(process_registry_module, "_find_shell", lambda: str(fake_shell))
    monkeypatch.setitem(tt._active_environments, "default", PendingEnv())
    monkeypatch.setattr("gateway.session_context.async_delivery_supported", lambda: True)
    try:
        result = json.loads(
            tt.terminal_tool(
                command="gh pr checks 123",
                ci_wait_timeout=300,
                task_id="ci-wakeup-test",
            )
        )
        process_id = result["ci_wait_session_id"]
        deadline = time.time() + 5
        while time.time() < deadline:
            session = fresh_registry.get(process_id)
            if (
                session is not None
                and session.exited
                and not fresh_registry.completion_queue.empty()
            ):
                break
            time.sleep(0.01)
        else:
            pytest.fail("promoted CI waiter did not exit")

        notifications = fresh_registry.drain_notifications(
            skip_poll_observed=False,
        )
    finally:
        fresh_registry.kill_all()
        tt._active_environments.pop("default", None)
        tt._last_activity.pop("default", None)

    assert len(notifications) == 1
    event, synthetic_message = notifications[0]
    assert event["session_id"] == process_id
    assert event["exit_code"] == 0
    assert "CI_WAIT_SUCCESS" in synthetic_message


def test_ci_wait_option_fails_closed_for_non_gh_command(monkeypatch, tmp_path):
    tt = _silent_bg_harness(monkeypatch, tmp_path)

    result = json.loads(
        tt.terminal_tool(command="pytest -q", ci_wait_timeout=300)
    )

    assert "requires an exact `gh pr checks" in result["error"]


@pytest.mark.parametrize(
    ("mode", "expected_rc", "marker", "expected_checks", "expected_views"),
    [
        ("success", 0, "CI_WAIT_SUCCESS", 2, 4),
        ("success-drift", 75, "CI_WAIT_SHA_DRIFT", 2, 4),
        ("failure", 1, "CI_WAIT_FAILURE", 1, 2),
        ("failure-drift", 75, "CI_WAIT_SHA_DRIFT", 1, 2),
        ("timeout", 124, "CI_WAIT_TIMEOUT", 1, 2),
        ("drift", 75, "CI_WAIT_SHA_DRIFT", 0, 1),
        ("post-pending-drift", 75, "CI_WAIT_SHA_DRIFT", 1, 2),
        ("in-loop-drift", 75, "CI_WAIT_SHA_DRIFT", 1, 3),
        ("lookup-pre", 70, "CI_WAIT_SHA_LOOKUP_FAILURE", 0, 1),
        ("lookup-post", 70, "CI_WAIT_SHA_LOOKUP_FAILURE", 1, 2),
    ],
)
def test_bounded_ci_wait_wrapper_resolves_explicitly(
    tmp_path, mode, expected_rc, marker, expected_checks, expected_views
):
    """Exercise the generated poller as a real shell process.

    Fake date/sleep binaries make backoff deterministic and instantaneous;
    the wrapper itself, its gh exit-code handling, and every terminal state are
    real rather than mocked.
    """
    import subprocess

    from tools.terminal_tool import _build_bounded_ci_wait_command, _parse_gh_pr_checks

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gh = fake_bin / "gh"
    gh.write_text(
        """#!/bin/bash
mode=$CI_WAIT_TEST_MODE
if [ "$1 $2" = "pr view" ]; then
  count=$(cat "$CI_WAIT_TEST_DIR/view_count" 2>/dev/null || printf 0)
  printf '%s' $((count + 1)) > "$CI_WAIT_TEST_DIR/view_count"
  [ "$mode" = lookup-pre ] && [ "$count" -ge 0 ] && exit 1
  [ "$mode" = lookup-post ] && [ "$count" -ge 1 ] && exit 1
  if { [ "$mode" = drift ] && [ "$count" -ge 0 ]; } ||
     { [ "$mode" = post-pending-drift ] && [ "$count" -ge 1 ]; } ||
     { [ "$mode" = in-loop-drift ] && [ "$count" -ge 2 ]; } ||
     { [ "$mode" = success-drift ] && [ "$count" -ge 3 ]; } ||
     { [ "$mode" = failure-drift ] && [ "$count" -ge 1 ]; }; then
    printf '%040d\n' 2
  else
    printf '%040d\n' 1
  fi
  exit 0
fi
count=$(cat "$CI_WAIT_TEST_DIR/check_count" 2>/dev/null || printf 0)
printf '%s' $((count + 1)) > "$CI_WAIT_TEST_DIR/check_count"
case "$mode" in
  success|success-drift) [ "$count" -eq 0 ] && exit 8; printf 'build\tpass\n'; exit 0 ;;
  failure|failure-drift) printf 'build\tfail\n'; exit 1 ;;
  timeout) exit 8 ;;
esac
exit 8
"""
    )
    gh.chmod(0o755)

    fake_date = fake_bin / "date"
    fake_date.write_text(
        """#!/bin/bash
count=$(cat "$CI_WAIT_TEST_DIR/date_count" 2>/dev/null || printf 0)
printf '%s' $((count + 1)) > "$CI_WAIT_TEST_DIR/date_count"
if [ "$CI_WAIT_TEST_MODE" = timeout ] && [ "$count" -ge 1 ]; then
  printf '101\n'
else
  printf '100\n'
fi
"""
    )
    fake_date.chmod(0o755)
    fake_sleep = fake_bin / "sleep"
    fake_sleep.write_text("#!/bin/bash\nexit 0\n")
    fake_sleep.chmod(0o755)

    parsed = _parse_gh_pr_checks("gh pr checks 123")
    assert parsed is not None
    wrapper = _build_bounded_ci_wait_command(
        parsed, timeout=1, expected_sha=f"{1:040d}"
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "CI_WAIT_TEST_MODE": mode,
            "CI_WAIT_TEST_DIR": str(tmp_path),
        }
    )

    completed = subprocess.run(
        ["/bin/bash", "-c", wrapper],
        text=True,
        capture_output=True,
        env=env,
        timeout=5,
        check=False,
    )

    assert completed.returncode == expected_rc, completed.stdout + completed.stderr
    assert marker in completed.stdout
    check_count = tmp_path / "check_count"
    actual_checks = int(check_count.read_text()) if check_count.exists() else 0
    assert actual_checks == expected_checks
    assert int((tmp_path / "view_count").read_text()) == expected_views


@pytest.mark.parametrize("checks_rc", [0, 1, 8])
def test_foreground_ci_result_fails_closed_when_head_changes(
    monkeypatch, tmp_path, checks_rc
):
    """Success and failure are both invalid when the PR head moved mid-check."""
    tt = _silent_bg_harness(monkeypatch, tmp_path)

    class DriftingEnv:
        env = {}
        cwd = str(tmp_path)
        view_calls = 0
        checks_calls = 0

        def execute(self, command, **kwargs):
            if command.startswith("gh pr view"):
                self.view_calls += 1
                return {
                    "returncode": 0,
                    "output": f"{self.view_calls:040d}",
                }
            assert command == "gh pr checks 123"
            self.checks_calls += 1
            return {"returncode": checks_rc, "output": "checks result"}

    env = DriftingEnv()
    monkeypatch.setitem(tt._active_environments, "default", env)
    try:
        result = json.loads(
            tt.terminal_tool(command="gh pr checks 123", ci_wait_timeout=300)
        )
    finally:
        tt._active_environments.pop("default", None)
        tt._last_activity.pop("default", None)

    assert result["exit_code"] == 75
    assert "CI_WAIT_SHA_DRIFT" in result["output"]
    assert env.view_calls == 2
    assert env.checks_calls == 1


@pytest.mark.parametrize(("failure_at", "expected_checks"), [(1, 0), (2, 1)])
def test_foreground_ci_fails_closed_on_sha_lookup_error(
    monkeypatch, tmp_path, failure_at, expected_checks
):
    tt = _silent_bg_harness(monkeypatch, tmp_path)

    class LookupFailureEnv:
        env = {}
        cwd = str(tmp_path)
        view_calls = 0
        checks_calls = 0

        def execute(self, command, **kwargs):
            if command.startswith("gh pr view"):
                self.view_calls += 1
                if self.view_calls == failure_at:
                    return {"returncode": 1, "output": "lookup failed"}
                return {"returncode": 0, "output": f"{1:040d}"}
            assert command == "gh pr checks 123"
            self.checks_calls += 1
            return {"returncode": 0, "output": "build\tpass"}

    env = LookupFailureEnv()
    monkeypatch.setitem(tt._active_environments, "default", env)
    try:
        result = json.loads(
            tt.terminal_tool(command="gh pr checks 123", ci_wait_timeout=300)
        )
    finally:
        tt._active_environments.pop("default", None)
        tt._last_activity.pop("default", None)

    assert set(result) == {"error"}
    if failure_at == 1:
        assert "Unable to bind the CI wait" in result["error"]
        assert "no checks command or waiter was started" in result["error"]
    else:
        assert "Unable to revalidate the PR head" in result["error"]
    assert env.view_calls == failure_at
    assert env.checks_calls == expected_checks
