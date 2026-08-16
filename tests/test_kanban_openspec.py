import pytest
import sqlite3
import json
import threading

from hermes_state import SessionDB
from hermes_state_schema import run_openspec_migration

def test_openspec_registry_negative_cases(tmp_path):
    """
    Simulate failures and unauthorized operations on openspec_registry.
    """
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    
    with db._lock:
        db._conn.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, status TEXT)")
        db._conn.execute("CREATE TABLE IF NOT EXISTS task_runs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id))")
        db._conn.execute("CREATE TABLE IF NOT EXISTS task_events (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id))")
        run_openspec_migration(db._conn)
        db._conn.commit()

    with db._lock:
        db._conn.execute(
            "INSERT INTO openspec_registry (id, openspec_contract, status, created_at) VALUES (?, ?, ?, ?)",
            ('reg_123', 'spec123', 'active', 1.0)
        )
        db._conn.commit()
    
    # Simulate a failed update (rowcount = 0)
    with db._lock:
        cursor = db._conn.execute("UPDATE openspec_registry SET status = 'archived' WHERE id = 'reg_999'")
        assert cursor.rowcount == 0  # Should fail normally since ID doesn't exist, but triggers shouldn't block it initially if it doesn't match

        # Let's ensure the trigger BLOCKS updates even if we try to circumvent it. 
        # But wait, BEFORE UPDATE trigger fires before the update is applied. 
        # For a non-existent row, the trigger might not fire, or it fires and rowcount is 0. 
        # But for an existing row, it raises IntegrityError.
        with pytest.raises(sqlite3.IntegrityError, match="SQLITE_CONSTRAINT_TRIGGER"):
            db._conn.execute("UPDATE openspec_registry SET status = 'archived' WHERE id = 'reg_123'")

def test_task_run_identity_events_negative_cases(tmp_path):
    """
    Simulate failures and unauthorized operations on task_run_identity_events.
    """
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    
    with db._lock:
        db._conn.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, status TEXT)")
        db._conn.execute("CREATE TABLE IF NOT EXISTS task_runs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id))")
        db._conn.execute("CREATE TABLE IF NOT EXISTS task_events (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id))")
        run_openspec_migration(db._conn)
        db._conn.commit()

    with db._lock:
        db._conn.execute("INSERT INTO tasks (id, status) VALUES (?, ?)", ('task_1', 'running'))
        db._conn.execute("INSERT INTO task_runs (id, task_id) VALUES (?, ?)", ('run_1', 'task_1'))
        db._conn.execute(
            "INSERT INTO task_run_identity_events (id, run_id, task_id, identity_snapshot, event_type, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ('evt_123', 'run_1', 'task_1', 'snapshot123', 'start', 1.0)
        )
        db._conn.commit()

    # Simulate a failed update (rowcount = 0)
    with db._lock:
        cursor = db._conn.execute("UPDATE task_run_identity_events SET event_type = 'stop' WHERE id = 'evt_999'")
        assert cursor.rowcount == 0

        # Existing row
        with pytest.raises(sqlite3.IntegrityError, match="SQLITE_CONSTRAINT_TRIGGER"):
            db._conn.execute("UPDATE task_run_identity_events SET event_type = 'stop' WHERE id = 'evt_123'")


def test_concurrent_inserts_race_condition(tmp_path):
    """
    Simulate race conditions with concurrent inserts to ensure database integrity.
    """
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    
    with db._lock:
        db._conn.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, status TEXT)")
        db._conn.execute("CREATE TABLE IF NOT EXISTS task_runs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id))")
        db._conn.execute("CREATE TABLE IF NOT EXISTS task_events (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id))")
        run_openspec_migration(db._conn)
        db._conn.commit()
        
    exceptions = []
    
    def insert_worker(worker_id):
        try:
            # Each worker uses its own connection to properly simulate concurrent DB access
            # that might hit locks. SessionDB is meant to be instantiated or we can use the same 
            # with lock, but let's test SQLite's underlying concurrency handling or the lock's protection.
            local_db = SessionDB(db_path)
            with local_db._lock:
                local_db._conn.execute(
                    "INSERT INTO openspec_registry (id, openspec_contract, status, created_at) VALUES (?, ?, ?, ?)",
                    (f'reg_worker_{worker_id}', f'spec_{worker_id}', 'active', 1.0)
                )
                local_db._conn.commit()
        except Exception as e:
            exceptions.append(e)

    threads = []
    for i in range(10):
        t = threading.Thread(target=insert_worker, args=(i,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    assert len(exceptions) == 0, f"Exceptions occurred during concurrent inserts: {exceptions}"
    
    with db._lock:
        cursor = db._conn.execute("SELECT COUNT(*) FROM openspec_registry")
        count = cursor.fetchone()[0]
        assert count == 10
