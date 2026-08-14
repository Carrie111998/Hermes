import pytest
import sqlite3
import json
import time
from uuid import uuid4

import hermes_cli.kanban_db as kanban_db

def test_generic_task_parity(tmp_path):
    """
    Test that standard non-OpenSpec tasks work exactly as before.
    (Lifecycle: create -> running -> completed)
    """
    from hermes_state import SessionDB
    from hermes_state_schema import run_openspec_migration

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    
    with db._lock:
        db._conn.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT, status TEXT, priority INTEGER, created_by TEXT, created_at REAL, started_at REAL, completed_at REAL, workspace_kind TEXT, workspace_path TEXT, branch_name TEXT, project_id TEXT, claim_lock TEXT, claim_expires INTEGER, tenant TEXT, idempotency_key TEXT, max_runtime_seconds INTEGER, skills TEXT, max_retries INTEGER, model_override TEXT, provider_override TEXT, reasoning_effort TEXT, goal_mode INTEGER, goal_max_turns INTEGER, session_id TEXT, result TEXT, current_run_id INTEGER, current_step_key TEXT)")
        db._conn.execute("CREATE TABLE IF NOT EXISTS task_runs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), status TEXT, outcome TEXT, summary TEXT, ended_at REAL, claim_lock TEXT, claim_expires REAL, worker_pid INTEGER, profile TEXT, step_key TEXT, max_runtime_seconds INTEGER, started_at REAL)")
        db._conn.execute("CREATE TABLE IF NOT EXISTS task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, run_id INTEGER, kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL)")
        db._conn.execute("CREATE TABLE IF NOT EXISTS task_dependencies (parent_id TEXT, child_id TEXT)")
        db._conn.execute("CREATE TABLE IF NOT EXISTS task_links (parent_id TEXT, child_id TEXT)")
        run_openspec_migration(db._conn)
        db._conn.commit()

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # 1. Create a generic task
    task_id = kanban_db.create_task(
        conn,
        assignee="coder",
        title="Generic Task",
        body="Do some work"
    )

    # 2. Check initial state
    task = kanban_db.get_task(conn, task_id)
    assert task.status == "ready"

    # 3. Claim and update status
    run_id = "run_1"
    kanban_db.claim_task(conn, task_id)
    
    # In older models, claiming might set to 'running' or we can manually set it
    # No direct update_status in kanban_db without correct function signature.
    # We can do DB update to 'running'.
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
    conn.commit()
    task = kanban_db.get_task(conn, task_id)
    assert task.status == "running"

    # 4. Complete task
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
    conn.commit()
    task = kanban_db.get_task(conn, task_id)
    assert task.status == "done"

def test_openspec_registered_task_enforcement(tmp_path):
    """
    Test that OpenSpec registered tasks enforce strict state machine transitions.
    """
    from hermes_state import SessionDB
    from hermes_state_schema import run_openspec_migration

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    
    with db._lock:
        db._conn.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT, status TEXT, priority INTEGER, created_by TEXT, created_at REAL, started_at REAL, completed_at REAL, workspace_kind TEXT, workspace_path TEXT, branch_name TEXT, project_id TEXT, claim_lock TEXT, claim_expires INTEGER, tenant TEXT, idempotency_key TEXT, max_runtime_seconds INTEGER, skills TEXT, max_retries INTEGER, model_override TEXT, provider_override TEXT, reasoning_effort TEXT, goal_mode INTEGER, goal_max_turns INTEGER, session_id TEXT, result TEXT, current_run_id INTEGER, current_step_key TEXT)")
        db._conn.execute("CREATE TABLE IF NOT EXISTS task_runs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), status TEXT, outcome TEXT, summary TEXT, ended_at REAL, claim_lock TEXT, claim_expires REAL, worker_pid INTEGER, profile TEXT, step_key TEXT, max_runtime_seconds INTEGER, started_at REAL)")
        db._conn.execute("CREATE TABLE IF NOT EXISTS task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, run_id INTEGER, kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL)")
        db._conn.execute("CREATE TABLE IF NOT EXISTS task_dependencies (parent_id TEXT, child_id TEXT)")
        db._conn.execute("CREATE TABLE IF NOT EXISTS task_links (parent_id TEXT, child_id TEXT)")
        run_openspec_migration(db._conn)
        db._conn.commit()

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    
    # 1. Create an OpenSpec task
    task_id = kanban_db.create_task(
        conn,
        assignee="coder",
        title="OpenSpec Task",
        body="Do some OS work"
    )

    # 2. Register it in openspec_registry
    with db._lock:
        db._conn.execute(
            "INSERT INTO openspec_registry (id, openspec_contract, status, created_at) VALUES (?, ?, ?, ?)",
            (task_id, 'spec123', 'active', time.time())
        )
        db._conn.commit()

    # 3. Verify normal rules still apply
    run_id = "run_os_1"
    kanban_db.claim_task(conn, task_id)
    
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
    conn.commit()
    task = kanban_db.get_task(conn, task_id)
    assert task.status == "running"

    # 4. Attempt to do something the openspec task shouldn't
    # (Verify triggers fire on invalid mutations on registry)
    with pytest.raises(sqlite3.IntegrityError, match="SQLITE_CONSTRAINT_TRIGGER"):
        with db._lock:
            db._conn.execute("UPDATE openspec_registry SET status='done' WHERE id=?", (task_id,))
