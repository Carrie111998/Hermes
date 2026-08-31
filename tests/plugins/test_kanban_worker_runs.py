"""Tests for kanban worker/runs read endpoints.

Covers:
  GET /workers/active
  GET /runs/{run_id}
  GET /runs/{run_id}/inspect
  POST /runs/{run_id}/terminate
"""

from __future__ import annotations

import importlib.util
import secrets
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _load_plugin_router():
    """Dynamically load plugins/kanban/dashboard/plugin_api.py and return its router."""
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"

    mod_name = "hermes_dashboard_plugin_kanban_worker_runs_test"
    # Re-use a cached module if already loaded to avoid duplicate-router issues.
    if mod_name in sys.modules:
        return sys.modules[mod_name].router

    spec = importlib.util.spec_from_file_location(mod_name, plugin_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def _insert_run(conn, task_id, *, worker_pid=None, ended_at=None):
    """Insert a task_runs row directly (bypassing claim machinery) and return run_id."""
    lock = secrets.token_hex(8)
    future = int(time.time()) + 3600
    cur = conn.execute(
        "INSERT INTO task_runs "
        "(task_id, status, claim_lock, claim_expires, worker_pid, started_at, ended_at) "
        "VALUES (?, 'running', ?, ?, ?, ?, ?)",
        (task_id, lock, future, worker_pid, int(time.time()), ended_at),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# GET /workers/active
# ---------------------------------------------------------------------------

def test_workers_active_empty_board(client):
    """Board with no running tasks returns an empty workers list."""
    r = client.get("/api/plugins/kanban/workers/active")
    assert r.status_code == 200
    body = r.json()
    assert body["workers"] == []
    assert body["count"] == 0
    assert "checked_at" in body


# ---------------------------------------------------------------------------
# GET /runs/{run_id}
# ---------------------------------------------------------------------------

def test_get_run_404_unknown_id(client):
    """Non-existent run_id returns 404."""
    r = client.get("/api/plugins/kanban/runs/999999")
    assert r.status_code == 404
    assert "999999" in r.json()["detail"]


# ---------------------------------------------------------------------------
# GET /runs/{run_id}/inspect
# ---------------------------------------------------------------------------

def test_inspect_run_404(client):
    """Non-existent run_id returns 404."""
    r = client.get("/api/plugins/kanban/runs/888888/inspect")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /runs/{run_id}/terminate
# ---------------------------------------------------------------------------

def _setup_running_task_with_run(conn, *, title, assignee, worker_pid):
    """Create a task in 'running' state with a matching open task_runs row.

    Mirrors what dispatcher_claim does: stamps tasks.status='running',
    tasks.claim_lock, tasks.worker_pid; inserts task_runs row with the
    same claim_lock so reclaim_task's preconditions are satisfied.
    """
    task_id = kb.create_task(conn, title=title, assignee=assignee)
    lock = secrets.token_hex(8)
    future = int(time.time()) + 3600
    conn.execute(
        "UPDATE tasks SET status='running', claim_lock=?, "
        "claim_expires=?, worker_pid=? WHERE id=?",
        (lock, future, worker_pid, task_id),
    )
    cur = conn.execute(
        "INSERT INTO task_runs "
        "(task_id, status, claim_lock, claim_expires, worker_pid, started_at) "
        "VALUES (?, 'running', ?, ?, ?, ?)",
        (task_id, lock, future, worker_pid, int(time.time())),
    )
    run_id = int(cur.lastrowid)
    conn.execute(
        "UPDATE tasks SET current_run_id=? WHERE id=?",
        (run_id, task_id),
    )
    conn.commit()
    return task_id, run_id


def test_terminate_run_404_unknown_id(client):
    """POST to unknown run_id returns 404."""
    r = client.post(
        "/api/plugins/kanban/runs/777777/terminate",
        json={"reason": "test"},
    )
    assert r.status_code == 404
    assert "777777" in r.json()["detail"]


def test_terminate_stale_run_handle_does_not_reclaim_successor(
    client, monkeypatch,
):
    """A run endpoint handle remains bound to its snapshotted run and claim."""
    with kb.connect_closing() as conn:
        task_id, old_run_id = _setup_running_task_with_run(
            conn,
            title="stale termination handle",
            assignee="default",
            worker_pid=None,
        )

    original_reclaim = kb.reclaim_task
    raced = False
    successor_state = {}

    def replace_with_successor(conn, requested_task_id, **kwargs):
        nonlocal raced
        assert requested_task_id == task_id
        assert not raced
        raced = True
        now = int(time.time())
        successor_lock = secrets.token_hex(8)
        future = now + 3600
        conn.execute(
            "UPDATE task_runs SET ended_at=?, status='ended', outcome='done' "
            "WHERE id=?",
            (now, old_run_id),
        )
        cur = conn.execute(
            "INSERT INTO task_runs "
            "(task_id, status, claim_lock, claim_expires, worker_pid, started_at) "
            "VALUES (?, 'running', ?, ?, NULL, ?)",
            (task_id, successor_lock, future, now),
        )
        successor_run_id = int(cur.lastrowid)
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=NULL, current_run_id=?, started_at=? WHERE id=?",
            (successor_lock, future, successor_run_id, now, task_id),
        )
        conn.commit()
        successor_state["run_id"] = successor_run_id
        successor_state["claim_lock"] = successor_lock
        return original_reclaim(conn, requested_task_id, **kwargs)

    monkeypatch.setattr(kb, "reclaim_task", replace_with_successor)
    response = client.post(
        f"/api/plugins/kanban/runs/{old_run_id}/terminate",
        json={"reason": "stale dashboard request"},
    )

    assert raced
    assert response.status_code == 409
    with kb.connect_closing() as conn:
        task = conn.execute(
            "SELECT status, claim_lock, current_run_id FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        successor = kb.get_run(conn, successor_state["run_id"])
    assert task["status"] == "running"
    assert task["claim_lock"] == successor_state["claim_lock"]
    assert int(task["current_run_id"]) == successor_state["run_id"]
    assert successor is not None
    assert successor.ended_at is None


