from __future__ import annotations

import tempfile
from pathlib import Path

from hermes_cli.kanban_store.database import connect
from hermes_cli.kanban_store.schema import migrate

_CONNECTIONS: dict[str, list[object]] = {}


def database(root: str | Path):
    path = Path(root) / "kanban.db"
    conn = connect(path)
    conn.executescript(
        """
        CREATE TABLE tasks(
            id TEXT PRIMARY KEY,
            title TEXT,
            status TEXT NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            current_run_id INTEGER,
            claim_expires INTEGER,
            last_heartbeat_at INTEGER,
            worker_pid INTEGER
        );
        CREATE TABLE task_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            profile TEXT,
            status TEXT NOT NULL,
            claim_lock TEXT,
            claim_expires INTEGER,
            last_heartbeat_at INTEGER,
            started_at INTEGER,
            ended_at INTEGER,
            outcome TEXT,
            summary TEXT,
            metadata TEXT
        );
        CREATE TABLE task_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            run_id INTEGER,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE task_attachments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            content_type TEXT,
            size INTEGER NOT NULL,
            uploaded_by TEXT,
            created_at INTEGER NOT NULL
        );
        """
    )
    migrate(conn)
    _CONNECTIONS.setdefault(str(Path(root).resolve()), []).append(conn)
    return conn


def add_task(conn, task_id: str = "task-1", status: str = "ready") -> None:
    conn.execute(
        "INSERT INTO tasks(id, title, status) VALUES (?, ?, ?)",
        (task_id, task_id, status),
    )


class TempRoot:
    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name)
        return self.path

    def __exit__(self, *args):
        for conn in _CONNECTIONS.pop(str(self.path.resolve()), []):
            conn.close()
        self._tmp.cleanup()
