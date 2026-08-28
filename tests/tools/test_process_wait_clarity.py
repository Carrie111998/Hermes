"""Tests for truthful process wait checkpoints and terminal states."""

import sys
import time
from typing import Any, cast

import pytest

from tools.process_registry import ProcessRegistry, ProcessSession


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    return ProcessRegistry()


def _spawn_sleeper(registry, notify=False):
    command = f'"{sys.executable}" -c "import time; time.sleep(30)"'
    session = registry.spawn_local(command, cwd="/tmp", task_id="t-waitclar")
    session.notify_on_complete = notify
    return session.id


class TestWaitTimeoutClarity:
    def test_wait_window_expiry_is_a_running_checkpoint(self, registry):
        sid = _spawn_sleeper(registry)
        try:
            r = registry.wait(sid, timeout=1)
            assert r["status"] == "running"
            assert r["wait_window_expired"] is True
            assert r["process_running"] is True
            assert "not an error" in r["timeout_note"]
            assert "Uptime" in r["timeout_note"]
        finally:
            registry.kill_process(sid)

    def test_wait_timeout_suggests_notify_when_unset(self, registry):
        sid = _spawn_sleeper(registry, notify=False)
        try:
            r = registry.wait(sid, timeout=1)
            assert "notify_on_complete=true" in r["timeout_note"]
        finally:
            registry.kill_process(sid)

    def test_wait_timeout_defers_to_notify_when_set(self, registry):
        sid = _spawn_sleeper(registry, notify=True)
        try:
            r = registry.wait(sid, timeout=1)
            assert "you will be notified on exit" in r["timeout_note"]
        finally:
            registry.kill_process(sid)

    def test_clamped_wait_keeps_clamp_note_and_running_semantics(
        self, registry, monkeypatch
    ):
        monkeypatch.setenv("TERMINAL_TIMEOUT", "1")
        sid = _spawn_sleeper(registry)
        try:
            r = registry.wait(sid, timeout=600)
            assert r["status"] == "running"
            assert r["wait_window_expired"] is True
            assert "clamped" in r["timeout_note"]
            assert "not an error" in r["timeout_note"]
            assert r["process_running"] is True
        finally:
            registry.kill_process(sid)

    def test_exited_process_unaffected(self, registry):
        session = registry.spawn_local("true", cwd="/tmp", task_id="t-waitclar")
        r = registry.wait(session.id, timeout=10)
        assert r["status"] == "exited"
        assert "process_running" not in r
        assert "wait_window_expired" not in r

    @pytest.mark.parametrize(
        ("exit_code", "completion_reason", "termination_source"),
        [
            (0, "exited", ""),
            (7, "exited", ""),
            (124, "exited", ""),
            (-15, "killed", "process.kill"),
        ],
    )
    def test_terminal_states_remain_machine_detectable(
        self, registry, exit_code, completion_reason, termination_source
    ):
        session = ProcessSession(
            id=f"proc_terminal_{exit_code}",
            command="controlled child",
            started_at=time.time(),
            exited=True,
            exit_code=exit_code,
            completion_reason=completion_reason,
            termination_source=termination_source,
        )
        session._completion_event.set()
        registry._finished[session.id] = session

        r = registry.wait(session.id, timeout=1)

        assert r["status"] == "exited"
        assert r["exit_code"] == exit_code
        assert r["completion_reason"] == completion_reason
        assert r["termination_source"] == termination_source
        assert "wait_window_expired" not in r

    def test_unknown_session_remains_distinct(self, registry):
        r = registry.wait("proc_unknown", timeout=1)
        assert r["status"] == "not_found"
        assert "error" in r
        assert "wait_window_expired" not in r

    def test_interrupt_at_wait_deadline_remains_distinct(self, registry, monkeypatch):
        session = ProcessSession(
            id="proc_deadline_interrupt",
            command="controlled child",
            started_at=time.time(),
        )
        registry._running[session.id] = session
        interrupted = False

        class InterruptAtDeadline:
            def wait(self, timeout=None):
                nonlocal interrupted
                time.sleep(timeout or 0)
                interrupted = True
                return False

            def set(self):
                pass

        monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: interrupted)
        session._completion_event = cast(Any, InterruptAtDeadline())
        r = registry.wait(session.id, timeout=1)

        assert r["status"] == "interrupted"
        assert "wait_window_expired" not in r

    def test_exit_at_wait_deadline_never_returns_a_live_checkpoint(self, registry):
        session = ProcessSession(
            id="proc_deadline_exit",
            command="controlled child",
            started_at=time.time(),
        )
        registry._running[session.id] = session

        class ExitAtDeadline:
            def wait(self, timeout=None):
                time.sleep(timeout or 0)
                session.exited = True
                session.exit_code = 0
                session.completion_reason = "exited"
                return True

            def set(self):
                pass

        session._completion_event = cast(Any, ExitAtDeadline())
        r = registry.wait(session.id, timeout=1)

        assert r["status"] == "exited"
        assert r["exit_code"] == 0
        assert "wait_window_expired" not in r
