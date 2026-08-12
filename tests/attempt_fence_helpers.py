import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb


def logical_board_snapshot(conn):
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {
        table: sorted(
            (tuple(row) for row in conn.execute(f'SELECT * FROM "{table}"')),
            key=repr,
        )
        for table in tables
    }


def process_tuple(task):
    return (
        task.current_run_id,
        task.claim_lock,
        task.worker_pid,
        task.worker_pgid,
        task.worker_identity,
        task.worker_fence,
    )


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "attempt-fence")
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    conn = kb.connect()
    conn.close()
    return home


def create_bound_attempt(conn, *, leader_identity, status="running"):
    task_id = kb.create_task(conn, title="fenced", assignee="dor-coo")
    claimed = kb.claim_task(conn, task_id, claimer="fixture:claim")
    assert claimed is not None
    assert claimed.current_run_id is not None
    raw_fence = json.dumps(
        {
            "run_id": claimed.current_run_id,
            "claim_lock": claimed.claim_lock,
            "host": kb._host_id(),
            "leader_pid": leader_identity.pid,
            "worker_pgid": leader_identity.pgid,
            "worker_identity": leader_identity.token,
            "reason": "running",
            "created_at": int(time.time()),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    conn.execute(
        "UPDATE tasks SET status=?, worker_pid=?, worker_pgid=?, "
        "worker_identity=?, worker_fence=? WHERE id=?",
        (
            status,
            leader_identity.pid,
            leader_identity.pgid,
            leader_identity.token,
            raw_fence,
            task_id,
        ),
    )
    conn.execute(
        "UPDATE task_runs SET worker_pid=?, worker_pgid=?, worker_identity=?, "
        "worker_fence=? WHERE id=?",
        (
            leader_identity.pid,
            leader_identity.pgid,
            leader_identity.token,
            raw_fence,
            claimed.current_run_id,
        ),
    )
    conn.commit()
    return task_id, claimed, raw_fence


@pytest.fixture
def registered_current_process(isolated_home):
    identity = kb._darwin_process_identity(os.getpgid(0))
    assert identity is not None and identity.pid == os.getpgid(0)
    conn = kb.connect()
    task_id, claimed, raw_fence = create_bound_attempt(
        conn,
        leader_identity=identity,
    )
    fixture = SimpleNamespace(
        conn=conn,
        task_id=task_id,
        claimed=claimed,
        raw_fence=raw_fence,
        board_path=Path(conn.execute("PRAGMA database_list").fetchone()["file"]),
    )
    try:
        yield fixture
    finally:
        conn.close()
