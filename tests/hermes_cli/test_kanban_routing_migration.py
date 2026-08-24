"""Tests for the atomic routing metadata schema migration."""

from __future__ import annotations

import sqlite3
import time

from hermes_cli import kanban_db as kb


def _column(conn: sqlite3.Connection, table: str, name: str) -> sqlite3.Row:
    """Return one column description from SQLite's table-info pragma."""
    return next(row for row in conn.execute(f"PRAGMA table_info({table})") if row["name"] == name)


def test_routing_metadata_migration_is_typed_atomic_and_idempotent(tmp_path):
    """Publish the routing schema only after preserving its original run cutoff."""
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)

    with kb.connect_closing(db_path) as conn:
        metadata = {
            row["key"]: row["value"]
            for row in conn.execute("SELECT key, value FROM kanban_metadata")
        }
        assert metadata == {"migration_cutoff_id": "0", "routing_schema_version": "1"}

        table_columns = {
            row["name"]: row
            for row in conn.execute("PRAGMA table_info(kanban_metadata)")
        }
        assert table_columns["key"]["type"] == "TEXT"
        assert table_columns["key"]["pk"] == 1
        assert table_columns["value"]["type"] == "TEXT"
        assert table_columns["value"]["notnull"] == 1
        assert table_columns["updated_at"]["type"] == "INTEGER"
        assert table_columns["updated_at"]["notnull"] == 1
        assert _column(conn, "tasks", "routing_role")["type"] == "TEXT"
        assert _column(conn, "tasks", "routing_role")["notnull"] == 0
        assert _column(conn, "task_runs", "routing_source")["type"] == "TEXT"
        assert _column(conn, "task_runs", "routing_source")["notnull"] == 0

        created_at = int(time.time())
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, ?, ?)",
            ("post-migration", "post-migration", "pending", created_at),
        )
        cursor = conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at) VALUES (?, ?, ?)",
            ("post-migration", "running", created_at),
        )
        post_migration_id = cursor.lastrowid
        conn.commit()

    kb.init_db(db_path)

    with kb.connect_closing(db_path) as conn:
        metadata = {
            row["key"]: row["value"]
            for row in conn.execute("SELECT key, value FROM kanban_metadata")
        }
        assert metadata == {"migration_cutoff_id": "0", "routing_schema_version": "1"}
        assert post_migration_id > int(metadata["migration_cutoff_id"])
        row = conn.execute(
            "SELECT routing_source FROM task_runs WHERE id = ?", (post_migration_id,)
        ).fetchone()
        assert row["routing_source"] is None