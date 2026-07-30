"""PID-only must never be presented as a productive worker.

2026-07-30: the cockpit showed Code A as ``running`` from ``tasks.status`` +
``worker_pid IS NOT NULL`` alone, while the worker had produced no admitted
tool call, no diff and no commit. ``assess_worker_activity`` is the single
assessment every user-facing surface must go through: it distinguishes
``spawned`` (a PID exists) from ``productive`` (process alive AND heartbeat
fresh), so a spawned-but-idle or dead worker can never read as "working".
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


def _running_task(conn, *, pid) -> str:
    tid = kb.create_task(conn, title="job", assignee="worker")
    kb.claim_task(conn, tid)
    if pid is not None:
        kb._set_worker_pid(conn, tid, pid)
    return tid


def test_dead_pid_is_not_productive(kanban_home):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    conn = kb.connect()
    try:
        tid = _running_task(conn, pid=proc.pid)
        activity = kb.assess_worker_activity(conn, tid)
        assert activity["spawned"] is True
        assert activity["alive"] is False
        assert activity["productive"] is False
    finally:
        conn.close()


def test_live_pid_without_heartbeat_is_not_productive(kanban_home):
    """THE regression: a live PID with no heartbeat proof must not read as working."""
    conn = kb.connect()
    try:
        tid = _running_task(conn, pid=os.getpid())
        activity = kb.assess_worker_activity(conn, tid)
        assert activity["spawned"] is True
        assert activity["alive"] is True
        assert activity["heartbeat_fresh"] is False
        assert activity["productive"] is False
    finally:
        conn.close()


def test_live_pid_with_fresh_heartbeat_is_productive(kanban_home):
    conn = kb.connect()
    try:
        tid = _running_task(conn, pid=os.getpid())
        task = kb.get_task(conn, tid)
        assert kb.heartbeat_worker(conn, tid, expected_run_id=task.current_run_id)
        activity = kb.assess_worker_activity(conn, tid)
        assert activity["alive"] is True
        assert activity["heartbeat_fresh"] is True
        assert activity["productive"] is True
    finally:
        conn.close()


def test_stale_heartbeat_is_not_productive(kanban_home):
    conn = kb.connect()
    try:
        tid = _running_task(conn, pid=os.getpid())
        stale = int(time.time()) - (kb.PRODUCTIVE_HEARTBEAT_MAX_AGE_SECONDS + 60)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
                (stale, tid),
            )
        activity = kb.assess_worker_activity(conn, tid)
        assert activity["alive"] is True
        assert activity["heartbeat_fresh"] is False
        assert activity["productive"] is False
    finally:
        conn.close()


def test_unspawned_task_reports_not_spawned(kanban_home):
    conn = kb.connect()
    try:
        tid = _running_task(conn, pid=None)
        activity = kb.assess_worker_activity(conn, tid)
        assert activity["spawned"] is False
        assert activity["alive"] is False
        assert activity["productive"] is False
    finally:
        conn.close()
