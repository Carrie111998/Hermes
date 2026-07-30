"""Blocking a running card must terminate its exact worker, never orphan it.

``block_task`` used to be SQL-only: it nulled ``worker_pid`` while the worker
process kept running, so the live process became untraceable (every reclaim
path filters on ``worker_pid IS NOT NULL``) and kept consuming/writing until
it exited on its own. A controller block must reach the worker process —
through the same host-guarded termination helper the reclaim paths use — and
must never touch the worktree contents (WIP stays intact).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, *, pid: int) -> str:
    tid = kb.create_task(conn, title="job", assignee="worker")
    kb.claim_task(conn, tid)
    kb._set_worker_pid(conn, tid, pid)
    return tid


def _wait_exit(proc: subprocess.Popen, timeout: float = 8.0) -> bool:
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def test_block_running_task_terminates_host_local_worker(kanban_home):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        conn = kb.connect()
        try:
            tid = _running_task(conn, pid=proc.pid)
            assert kb.block_task(conn, tid, reason="controller stop", kind="needs_input")

            assert _wait_exit(proc), (
                "blocking a running card must terminate the exact live worker; "
                "leaving it running is a guaranteed orphan"
            )

            task = kb.get_task(conn, tid)
            assert task.status in {"blocked", "triage"}
            assert task.worker_pid is None

            events = kb.list_events(conn, tid)
            blocked = next(e for e in events if e.kind == "blocked")
            termination = blocked.payload.get("termination")
            assert termination, "blocked event must trace the termination outcome"
            assert termination["prev_pid"] == proc.pid
            assert termination["terminated"] is True
        finally:
            conn.close()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_block_does_not_signal_foreign_host_claims(kanban_home):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        conn = kb.connect()
        try:
            tid = _running_task(conn, pid=proc.pid)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET claim_lock = ? WHERE id = ?",
                    ("otherhost:99999", tid),
                )
            assert kb.block_task(conn, tid, reason="controller stop", kind="needs_input")

            # The claim was recorded by another host: never signal a local PID
            # that merely coincides. The worker must still be alive.
            time.sleep(0.5)
            assert proc.poll() is None, (
                "a foreign-host claim must never trigger a local kill "
                "(PID collision would murder an unrelated process)"
            )
            events = kb.list_events(conn, tid)
            blocked = next(e for e in events if e.kind == "blocked")
            termination = blocked.payload.get("termination")
            assert termination is not None
            assert termination["host_local"] is False
            assert termination["termination_attempted"] is False
        finally:
            conn.close()
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_block_dependency_kind_also_terminates_worker(kanban_home):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        conn = kb.connect()
        try:
            tid = _running_task(conn, pid=proc.pid)
            assert kb.block_task(conn, tid, reason="waiting on HER-41", kind="dependency")
            assert _wait_exit(proc), (
                "dependency blocks route differently but must equally "
                "terminate the live worker"
            )
            assert kb.get_task(conn, tid).worker_pid is None
        finally:
            conn.close()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_self_block_never_signals_the_calling_worker(kanban_home):
    """A worker blocking its own card (kanban_block) must not be killed mid-call.

    It still has its final report to write; it exits on its own right after.
    """
    conn = kb.connect()
    try:
        tid = _running_task(conn, pid=os.getpid())
        assert kb.block_task(conn, tid, reason="self", kind="needs_input")
        events = kb.list_events(conn, tid)
        blocked = next(e for e in events if e.kind == "blocked")
        assert "termination" not in blocked.payload
    finally:
        conn.close()


def test_block_stale_run_id_does_not_kill_newer_run_worker(kanban_home):
    """A block aimed at a stale run must not signal the newer run's worker."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        conn = kb.connect()
        try:
            tid = _running_task(conn, pid=proc.pid)
            task = kb.get_task(conn, tid)
            stale_run = int(task.current_run_id) - 1
            assert not kb.block_task(
                conn, tid, reason="stale", kind="needs_input",
                expected_run_id=stale_run,
            )
            time.sleep(0.3)
            assert proc.poll() is None, (
                "a stale-run block must leave the live newer-run worker alone"
            )
        finally:
            conn.close()
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_block_preserves_worktree_wip(kanban_home, tmp_path):
    """Termination must not touch files the worker wrote (WIP preserved)."""
    wip = tmp_path / "workspace" / "wip.txt"
    wip.parent.mkdir(parents=True)
    wip.write_text("half-finished work\n")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        conn = kb.connect()
        try:
            tid = _running_task(conn, pid=proc.pid)
            assert kb.block_task(conn, tid, reason="stop", kind="needs_input")
            assert wip.read_text() == "half-finished work\n"
        finally:
            conn.close()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
