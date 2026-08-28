"""A drift rebuild must not silently delete columns.

`_rebuild_drifted_tables()` recreates a table from `_REBUILD_SPECS` when its
column *types* drifted from SCHEMA_SQL. The copy is `old ∩ new`, so any column
present in SCHEMA_SQL but missing from the rebuild spec is **dropped, with its
data**, on exactly the boards that need repairing most.

The module comment claims `test_rebuilt_schema_matches_fresh` guards this. That
test did not exist, and in its absence `task_runs.observed_cwd` — the commit-7
confinement audit field — and `task_runs.session_id` were both droppable.

This is the general guard: every rebuild spec must produce the same columns as
a fresh database, for every table, forever.
"""
from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_db as kb


def _fresh_columns(tmp_path, monkeypatch, table):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "fresh.db"))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        return [(r["name"], r["type"]) for r in
                conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


@pytest.mark.parametrize("table", sorted(kb._REBUILD_SPECS))
def test_rebuilt_schema_matches_fresh(tmp_path, monkeypatch, table):
    """The canonical rebuild SQL must match what a fresh board produces."""
    fresh = _fresh_columns(tmp_path, monkeypatch, table)

    scratch = sqlite3.connect(":memory:")
    scratch.row_factory = sqlite3.Row
    create_sql, _indexes = kb._REBUILD_SPECS[table]
    scratch.execute(create_sql)
    rebuilt = [(r["name"], r["type"]) for r in
               scratch.execute(f"PRAGMA table_info({table})")]
    scratch.close()

    fresh_names = [n for n, _t in fresh]
    rebuilt_names = [n for n, _t in rebuilt]
    missing = [n for n in fresh_names if n not in rebuilt_names]
    extra = [n for n in rebuilt_names if n not in fresh_names]
    assert not missing, (
        f"{table}: the rebuild spec would DROP {missing} — a drifted board "
        f"loses those columns and their data"
    )
    assert not extra, f"{table}: rebuild spec has columns a fresh board lacks: {extra}"


def test_a_drifted_task_runs_keeps_its_audit_and_join_columns(tmp_path, monkeypatch):
    """The reproduction: a TEXT-primary-key task_runs carrying real values."""
    db = tmp_path / "drifted.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    raw = sqlite3.connect(db)
    raw.executescript(
        """
        DROP TABLE task_runs;
        CREATE TABLE task_runs (
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL, profile TEXT,
          step_key TEXT, status TEXT NOT NULL, claim_lock TEXT,
          claim_expires INTEGER, worker_pid INTEGER,
          max_runtime_seconds INTEGER, last_heartbeat_at INTEGER,
          started_at INTEGER NOT NULL, ended_at INTEGER, outcome TEXT,
          summary TEXT, metadata TEXT, error TEXT, observed_cwd TEXT,
          session_id TEXT);
        INSERT INTO task_runs
          (id, task_id, profile, status, started_at, observed_cwd, session_id)
        VALUES ('1', 't_x', 'coder', 'running', 1, '/verified', 'sess-1');
        """
    )
    raw.commit()
    raw.close()

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_runs)")}
        row = conn.execute(
            "SELECT observed_cwd, session_id, profile FROM task_runs"
        ).fetchone()
    finally:
        conn.close()

    assert "observed_cwd" in cols, "commit-7 confinement evidence was dropped"
    assert "session_id" in cols, "the cost join key was dropped"
    assert row["observed_cwd"] == "/verified", "the value must survive the rebuild"
    assert row["session_id"] == "sess-1"
    assert row["profile"] == "coder"


def test_the_rebuild_still_repairs_the_drifted_id(tmp_path, monkeypatch):
    """The repair itself must keep working: TEXT ids become INTEGER."""
    db = tmp_path / "drift-id.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    raw = sqlite3.connect(db)
    raw.executescript(
        """
        DROP TABLE task_runs;
        CREATE TABLE task_runs (
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL, profile TEXT,
          step_key TEXT, status TEXT NOT NULL, claim_lock TEXT,
          claim_expires INTEGER, worker_pid INTEGER,
          max_runtime_seconds INTEGER, last_heartbeat_at INTEGER,
          started_at INTEGER NOT NULL, ended_at INTEGER, outcome TEXT,
          summary TEXT, metadata TEXT, error TEXT, observed_cwd TEXT,
          session_id TEXT);
        INSERT INTO task_runs (id, task_id, status, started_at)
        VALUES ('not-an-int', 't_x', 'running', 1);
        """
    )
    raw.commit()
    raw.close()
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        types = {r["name"]: r["type"]
                 for r in conn.execute("PRAGMA table_info(task_runs)")}
        rid = conn.execute("SELECT id FROM task_runs").fetchone()["id"]
    finally:
        conn.close()
    assert types["id"] == "INTEGER"
    assert isinstance(rid, int)
