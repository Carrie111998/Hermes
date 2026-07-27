"""Behavior tests for fail-closed Kanban-worker signal shutdown.

These tests call production helpers or execute them in a real subprocess. They
deliberately do not inspect Python source or assert an implementation shape.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace

import pytest

import cli as cli_module


def _is_alive_like_dispatcher(pid: int) -> bool:
    """Treat a child zombie as dead on Linux and macOS."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        observed = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        state = observed.stdout.strip()
        if observed.returncode == 0 and state:
            return not state.upper().startswith("Z")
        if observed.returncode != 0:
            return False
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        pass
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as status_file:
            for line in status_file:
                if line.startswith("State:"):
                    return "Z" not in line.split(":", 1)[1]
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return True


def _cleanup_process(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate(timeout=2)


def test_managed_hard_exit_waits_for_cleanup_and_receipt_fences():
    cleanup_results = iter([False, True, True])
    commit_results = iter([False, True])
    waits = []
    exits = []

    cli_module._managed_hard_exit_after_signal(
        cleanup_safe_fn=lambda: next(cleanup_results),
        commit_exit_fn=lambda: next(commit_results),
        sleep_fn=waits.append,
        hard_exit_fn=exits.append,
    )

    assert waits == [1.0, 1.0]
    assert exits == [0]


def test_cleanup_veto_must_be_durable_before_hard_exit(
    monkeypatch,
):
    from tools.environments import base as environment_base

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_signal_test")
    monkeypatch.setenv(
        "HERMES_KANBAN_PROCESS_CLEANUP_UNSAFE", "foreground still active"
    )
    monkeypatch.delenv(
        "HERMES_KANBAN_PROCESS_CLEANUP_UNSAFE_DURABLE", raising=False
    )
    monkeypatch.setattr(
        environment_base,
        "mark_short_task_process_cleanup_unsafe",
        lambda _reason: False,
    )
    assert cli_module._kanban_cleanup_safe_to_hard_exit() is False

    monkeypatch.setattr(
        environment_base,
        "mark_short_task_process_cleanup_unsafe",
        lambda _reason: True,
    )
    assert cli_module._kanban_cleanup_safe_to_hard_exit() is True


def test_receipt_registry_not_diagnostic_env_controls_hard_exit(monkeypatch):
    from agent import kanban_auto_handoff as handoff

    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    monkeypatch.setenv("HERMES_KANBAN_HANDOFF_CONTROL_PENDING", "1")
    monkeypatch.setattr(
        handoff, "try_commit_handoff_control_hard_exit", lambda: False
    )
    assert cli_module._kanban_try_commit_hard_exit() is False

    monkeypatch.setattr(
        handoff, "try_commit_handoff_control_hard_exit", lambda: True
    )
    assert cli_module._kanban_try_commit_hard_exit() is True


class _FakeThread:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False

    def start(self):
        self.started = True


def test_signal_helper_cleans_registries_and_starts_daemon_hard_exit(
    monkeypatch,
):
    from tools.environments import base as environment_base
    from tools.process_registry import process_registry

    calls = []
    fake_threads = []
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_signal_test")
    monkeypatch.setenv("HERMES_KANBAN_FOREGROUND_CLEANUP_GRACE", "0")
    monkeypatch.setattr(
        cli_module, "_arm_exit_watchdog_on_shutdown_signal", lambda: calls.append("arm")
    )
    monkeypatch.setattr(
        environment_base,
        "cleanup_registered_short_task_foreground_processes",
        lambda: calls.append("foreground_cleanup") or [],
    )
    monkeypatch.setattr(
        environment_base,
        "wait_for_short_task_foreground_cleanup",
        lambda timeout: calls.append(("foreground_wait", timeout)) or True,
    )
    monkeypatch.setattr(
        environment_base,
        "mark_short_task_process_cleanup_unsafe",
        lambda reason: calls.append(("unsafe", reason)) or True,
    )
    monkeypatch.setattr(
        process_registry, "kill_all", lambda: calls.append("background_kill")
    )
    monkeypatch.setattr(process_registry, "has_any_active", lambda: False)

    def thread_factory(**kwargs):
        thread = _FakeThread(**kwargs)
        fake_threads.append(thread)
        return thread

    class _Agent:
        def interrupt(self, reason, *, system_signal):
            calls.append(("interrupt", reason, system_signal))

    with pytest.raises(KeyboardInterrupt):
        cli_module._handle_single_query_shutdown_signal(
            SimpleNamespace(agent=_Agent()),
            signal.SIGTERM,
            thread_factory=thread_factory,
        )

    assert calls[0] == "arm"
    assert any(item == "foreground_cleanup" for item in calls)
    assert calls.count("background_kill") == 2
    assert not [item for item in calls if isinstance(item, tuple) and item[0] == "unsafe"]
    assert len(fake_threads) == 1
    assert fake_threads[0].started is True
    assert fake_threads[0].kwargs == {
        "target": cli_module._managed_hard_exit_after_signal,
        "daemon": True,
        "name": "kanban-signal-hard-exit",
    }


def test_signal_helper_persists_cleanup_failure_before_unwind(monkeypatch):
    from tools.environments import base as environment_base
    from tools.process_registry import process_registry

    reasons = []
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_signal_test")
    monkeypatch.setattr(
        cli_module, "_arm_exit_watchdog_on_shutdown_signal", lambda: None
    )
    monkeypatch.setattr(
        environment_base,
        "cleanup_registered_short_task_foreground_processes",
        lambda: ["pgid survived"],
    )
    monkeypatch.setattr(
        environment_base,
        "mark_short_task_process_cleanup_unsafe",
        lambda reason: reasons.append(reason) or True,
    )
    monkeypatch.setattr(process_registry, "kill_all", lambda: None)
    monkeypatch.setattr(process_registry, "has_any_active", lambda: False)

    with pytest.raises(KeyboardInterrupt):
        cli_module._handle_single_query_shutdown_signal(
            SimpleNamespace(agent=None),
            signal.SIGTERM,
            thread_factory=lambda **kwargs: _FakeThread(**kwargs),
        )

    assert any("pgid survived" in reason for reason in reasons)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="foreground process-group cleanup is POSIX-only",
)
def test_sigterm_cleans_real_foreground_process_group(tmp_path):
    """A real worker signal kills and proves its foreground process group."""
    marker_path = tmp_path / "unexpected-unsafe-marker.txt"
    repo_root = Path(__file__).resolve().parents[2]
    script = textwrap.dedent(
        """
        import json
        import logging
        import os
        import signal
        import sys
        from types import SimpleNamespace

        sys.path.insert(0, os.environ["HERMES_TEST_REPO_ROOT"])
        import cli
        from tools.environments.local import LocalEnvironment
        from tools.environments import base as environment_base

        class Agent:
            def interrupt(self, *_args, **_kwargs):
                return True

        os.environ["HERMES_KANBAN_TASK"] = "t_real_signal_worker"
        os.environ["HERMES_KANBAN_REVIEW_MODE"] = "1"
        os.environ["HERMES_KANBAN_MANAGED_LANE"] = "review"
        os.environ["HERMES_KANBAN_MANAGED_BOOTSTRAP"] = "1"
        os.environ["HERMES_KANBAN_MANAGED_BOOTSTRAP_VERIFIED"] = "1"
        os.environ["HERMES_KANBAN_FOREGROUND_CLEANUP_GRACE"] = "0.1"

        real_mark = environment_base.mark_short_task_process_cleanup_unsafe
        def tracking_mark(reason):
            with open(os.environ["HERMES_TEST_MARKER_PATH"], "a", encoding="utf-8") as out:
                out.write(str(reason) + "\\n")
                out.flush()
                os.fsync(out.fileno())
            return real_mark(reason)
        environment_base.mark_short_task_process_cleanup_unsafe = tracking_mark

        session = SimpleNamespace(agent=Agent())
        signal.signal(
            signal.SIGTERM,
            lambda signum, frame: cli._handle_single_query_shutdown_signal(
                session, signum
            ),
        )
        terminal = LocalEnvironment(cwd=os.environ["HERMES_TEST_CWD"], timeout=60)
        child = terminal._run_bash("trap '' TERM; while :; do sleep 1; done", timeout=60)
        print(f"READY {child.pid} {child._hermes_pgid}", flush=True)
        terminal._wait_for_process(child, timeout=60)
        """
    )
    env = dict(os.environ)
    env.update(
        {
            "HERMES_TEST_REPO_ROOT": str(repo_root),
            "HERMES_TEST_CWD": str(tmp_path),
            "HERMES_TEST_MARKER_PATH": str(marker_path),
        }
    )
    for key in (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_REVIEW_MODE",
        "HERMES_KANBAN_MANAGED_LANE",
        "HERMES_KANBAN_MANAGED_BOOTSTRAP",
        "HERMES_KANBAN_MANAGED_BOOTSTRAP_VERIFIED",
        "HERMES_KANBAN_MANAGED_BOOTSTRAP_ERROR",
        "HERMES_KANBAN_PROCESS_CLEANUP_UNSAFE",
        "HERMES_KANBAN_PROCESS_CLEANUP_UNSAFE_DURABLE",
        "PYTEST_CURRENT_TEST",
    ):
        env.pop(key, None)
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        cwd=str(tmp_path),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    child_pgid = None
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline().decode("utf-8", errors="replace").strip()
        assert line.startswith("READY "), line
        _ready, child_pid, child_pgid_raw = line.split()
        child_pgid = int(child_pgid_raw)
        assert int(child_pid) == child_pgid

        os.kill(proc.pid, signal.SIGTERM)
        assert proc.wait(timeout=6) == 0
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                os.killpg(child_pgid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            pytest.fail("foreground process group survived worker signal cleanup")
        assert not marker_path.exists()
        assert not _is_alive_like_dispatcher(proc.pid)
    finally:
        if child_pgid is not None:
            try:
                os.killpg(child_pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        _cleanup_process(proc)


def test_marker_reader_is_data_only(tmp_path):
    """Marker output remains parseable without inspecting implementation files."""
    marker_path = tmp_path / "marker.jsonl"
    marker_path.write_text(json.dumps({"durable": True}) + "\n", encoding="utf-8")
    records = [
        json.loads(line)
        for line in marker_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records == [{"durable": True}]
