import pytest
import sqlite3
import json

from hermes_state import SessionDB
from hermes_state_schema import run_openspec_migration

def test_openspec_immutability_triggers(tmp_path):
    """
    Test that the BEFORE UPDATE and BEFORE DELETE triggers prevent mutations
    on openspec_registry and task_run_identity_events as required by Task 1.1/1.2.
    """
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    
    # Initialize required tables before migration
    with db._lock:
        db._conn.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, status TEXT)")
        db._conn.execute("CREATE TABLE IF NOT EXISTS task_runs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id))")
        db._conn.execute("CREATE TABLE IF NOT EXISTS task_events (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id))")
        run_openspec_migration(db._conn)
        db._conn.commit()

    with db._lock:
        db._conn.execute("INSERT INTO tasks (id, status) VALUES (?, ?)", ('task_1', 'running'))
        db._conn.execute("INSERT INTO task_runs (id, task_id) VALUES (?, ?)", ('run_1', 'task_1'))
        
        # Insert test data into openspec_registry
        db._conn.execute(
            "INSERT INTO openspec_registry (id, openspec_contract, status, created_at) VALUES (?, ?, ?, ?)",
            ('reg_123', 'spec123', 'active', 1.0)
        )
        
        # Insert test data into task_run_identity_events
        db._conn.execute(
            "INSERT INTO task_run_identity_events (id, run_id, task_id, identity_snapshot, event_type, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ('evt_123', 'run_1', 'task_1', 'snapshot123', 'start', 1.0)
        )
        db._conn.commit()

    with db._lock:
        # Test UPDATE on openspec_registry
        with pytest.raises(sqlite3.IntegrityError, match="SQLITE_CONSTRAINT_TRIGGER"):
            db._conn.execute("UPDATE openspec_registry SET openspec_contract = 'hash456' WHERE id = 'reg_123'")
            
        # Test DELETE on openspec_registry
        with pytest.raises(sqlite3.IntegrityError, match="SQLITE_CONSTRAINT_TRIGGER"):
            db._conn.execute("DELETE FROM openspec_registry WHERE id = 'reg_123'")
            
        # Test UPDATE on task_run_identity_events
        with pytest.raises(sqlite3.IntegrityError, match="SQLITE_CONSTRAINT_TRIGGER"):
            db._conn.execute("UPDATE task_run_identity_events SET event_type = 'stop' WHERE id = 'evt_123'")
            
        # Test DELETE on task_run_identity_events
        with pytest.raises(sqlite3.IntegrityError, match="SQLITE_CONSTRAINT_TRIGGER"):
            db._conn.execute("DELETE FROM task_run_identity_events WHERE id = 'evt_123'")
