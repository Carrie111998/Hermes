import pytest
import sqlite3
import json
import time

from hermes_state import SessionDB, remediate_qa_failure
from hermes_state_common import activate_openspec_enforcement, rollback_openspec_enforcement

def test_activate_openspec_enforcement():
    conn = sqlite3.connect(':memory:')
    conn.execute("CREATE TABLE openspec_enforcement_meta (key TEXT PRIMARY KEY, value TEXT)")
    
    with pytest.raises(PermissionError):
        activate_openspec_enforcement(conn, "unauthorized_user")
        
    activate_openspec_enforcement(conn, "commander")
    
    cursor = conn.execute("SELECT value FROM openspec_enforcement_meta WHERE key = 'enforcement_status'")
    assert cursor.fetchone()[0] == 'active'

def test_rollback_openspec_enforcement():
    conn = sqlite3.connect(':memory:')
    
    with pytest.raises(PermissionError):
        rollback_openspec_enforcement(conn, "unauthorized_user")
        
    conn.execute("CREATE TABLE openspec_registry (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE task_run_identity_events (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE openspec_enforcement_meta (key TEXT PRIMARY KEY, value TEXT)")
    
    conn.execute("INSERT INTO openspec_registry (id) VALUES ('1')")
    
    with pytest.raises(RuntimeError, match="openspec_registry contains 1 rows"):
        rollback_openspec_enforcement(conn, "commander")
        
    conn.execute("DELETE FROM openspec_registry")
    conn.execute("INSERT INTO task_run_identity_events (id) VALUES ('1')")
    
    with pytest.raises(RuntimeError, match="task_run_identity_events contains 1 rows"):
        rollback_openspec_enforcement(conn, "commander")
        
    conn.execute("DELETE FROM task_run_identity_events")
    
    rollback_openspec_enforcement(conn, "commander")
    
    cursor = conn.execute("SELECT count(name) FROM sqlite_master WHERE type='table' AND name IN ('openspec_registry', 'task_run_identity_events', 'openspec_enforcement_meta')")
    assert cursor.fetchone()[0] == 0

def test_remediate_qa_failure():
    conn = sqlite3.connect(':memory:')
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT)")
    conn.execute("CREATE TABLE task_events (id TEXT PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT, created_at REAL)")
    
    conn.execute("INSERT INTO tasks (id, status) VALUES ('t1', 'done')")
    
    with pytest.raises(ValueError, match="No review_requested event found"):
        remediate_qa_failure(conn, 't1')
        
    conn.execute("INSERT INTO task_events (id, task_id, kind, payload, created_at) VALUES ('e1', 't1', 'review_requested', '{\"provenance_run_id\": \"run1\", \"provenance_event_id\": \"evt1\"}', 100)")
    
    remediate_qa_failure(conn, 't1')
    
    cursor = conn.execute("SELECT status FROM tasks WHERE id = 't1'")
    assert cursor.fetchone()[0] == 'todo'
    
    cursor = conn.execute("SELECT payload FROM task_events WHERE task_id = 't1' AND kind = 'qa_remediation_initiated'")
    row = cursor.fetchone()
    assert row is not None
    payload = json.loads(row[0])
    assert payload["provenance_run_id"] == "run1"
    assert payload["provenance_event_id"] == "evt1"

def test_enforce_openspec_transaction(tmp_path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    
    # Setup test DB
    with db._lock:
        db._conn.execute("CREATE TABLE IF NOT EXISTS openspec_enforcement_meta (key TEXT PRIMARY KEY, value TEXT)")
        db._conn.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, status TEXT, openspec_contract_hash TEXT)")
        
        # Insert test data
        db._conn.execute("INSERT INTO openspec_enforcement_meta (key, value) VALUES ('enforcement_status', 'active')")
        db._conn.execute("INSERT INTO tasks (id, status, openspec_contract_hash) VALUES ('t_123', 'todo', 'hash123')")
        db._conn.commit()
    
    # Test valid transition
    assert db.enforce_openspec_transaction('t_123', 'done', 'hash123') is True
    
    # Verify status changed
    with db._lock:
        row = db._conn.execute("SELECT status FROM tasks WHERE id = 't_123'").fetchone()
        assert row[0] == 'done'
    
    # Test hash mismatch
    with pytest.raises(ValueError, match="OpenSpec contract hash mismatch"):
        db.enforce_openspec_transaction('t_123', 'done', 'wrong_hash')
        
    # Test concurrency/missing row
    with pytest.raises(ValueError, match="Task missing_task not found"):
        db.enforce_openspec_transaction('missing_task', 'done', 'hash123')
