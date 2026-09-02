"""complete_task / block_task must reap the worker they stop tracking (#90073).

Both transitions NULL ``worker_pid`` in the same UPDATE that moves a task
out of ``running``. Without a reap the child kept running unsupervised and
the board had no pid left to signal, so ``reclaim`` / ``block`` / ``complete``
could not stop it afterwards.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


FOREIGN_PID = 999_999


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    yield home


@pytest.fixture
def dead_after_sigterm(monkeypatch):
    """Pretend the signalled worker exits at once so the poll loop is fast."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)


def _running_task(conn, *, pid: int) -> str:
    tid = kb.create_task(conn, title="long job", assignee="worker")
    kb.claim_task(conn, tid)
    kb._set_worker_pid(conn, tid, pid)
    return tid


def _event(conn, tid, kind):
    return next(e for e in kb.list_events(conn, tid) if e.kind == kind)


def test_complete_terminates_a_worker_it_did_not_spawn(
    kanban_home, dead_after_sigterm
):
    killed = []
    conn = kb.connect()
    try:
        tid = _running_task(conn, pid=FOREIGN_PID)

        assert kb.complete_task(
            conn, tid, result="done", signal_fn=lambda pid, sig: killed.append((pid, sig)),
        ) is True

        assert killed == [(FOREIGN_PID, signal.SIGTERM)]
        task = kb.get_task(conn, tid)
        assert task.status == "done"
        assert task.worker_pid is None
        reap = _event(conn, tid, "completed").payload["worker_reap"]
        assert reap["prev_pid"] == FOREIGN_PID
        assert reap["terminated"] is True
    finally:
        conn.close()


def test_complete_does_not_signal_the_worker_reporting_itself(kanban_home):
    """The worker calls kanban_complete from inside worker_pid's process."""
    killed = []
    conn = kb.connect()
    try:
        tid = _running_task(conn, pid=os.getpid())

        assert kb.complete_task(
            conn, tid, result="done", signal_fn=lambda pid, sig: killed.append((pid, sig)),
        ) is True

        assert killed == []
        assert kb.get_task(conn, tid).status == "done"
        reap = _event(conn, tid, "completed").payload["worker_reap"]
        assert reap == {"prev_pid": os.getpid(), "self_reported": True}
    finally:
        conn.close()


def test_complete_of_an_unclaimed_task_signals_nothing(kanban_home):
    killed = []
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="manual card", assignee="worker")

        assert kb.complete_task(
            conn, tid, result="done", signal_fn=lambda pid, sig: killed.append((pid, sig)),
        ) is True

        assert killed == []
        assert "worker_reap" not in _event(conn, tid, "completed").payload
    finally:
        conn.close()


def test_block_terminates_a_worker_it_did_not_spawn(
    kanban_home, dead_after_sigterm
):
    killed = []
    conn = kb.connect()
    try:
        tid = _running_task(conn, pid=FOREIGN_PID)

        assert kb.block_task(
            conn, tid, reason="needs a key", kind="needs_input",
            signal_fn=lambda pid, sig: killed.append((pid, sig)),
        ) is True

        assert killed == [(FOREIGN_PID, signal.SIGTERM)]
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.worker_pid is None
        reap = _event(conn, tid, "blocked").payload["worker_reap"]
        assert reap["prev_pid"] == FOREIGN_PID
        assert reap["terminated"] is True
    finally:
        conn.close()


def test_dependency_block_terminates_the_worker_too(
    kanban_home, dead_after_sigterm
):
    killed = []
    conn = kb.connect()
    try:
        tid = _running_task(conn, pid=FOREIGN_PID)

        assert kb.block_task(
            conn, tid, reason="waiting on parent", kind="dependency",
            signal_fn=lambda pid, sig: killed.append((pid, sig)),
        ) is True

        assert killed == [(FOREIGN_PID, signal.SIGTERM)]
        assert kb.get_task(conn, tid).worker_pid is None
        assert _event(conn, tid, "dependency_wait").payload["worker_reap"]["prev_pid"] == FOREIGN_PID
    finally:
        conn.close()
