"""Claim-time routing snapshots are persisted immutably on task runs."""

from pathlib import Path
import sqlite3

import pytest

from hermes_cli import kanban_db as kb


SNAPSHOT = {
    "routing_role": "executor",
    "routing_model": "snapshot-model",
    "routing_provider": "snapshot-provider",
    "routing_contract": "snapshot-contract",
    "routing_reason": "snapshot-reason",
    "roster_digest": "snapshot-roster-digest",
    "routing_policy": "snapshot-policy",
    "ac_revision": "snapshot-ac-revision",
    "routing_source": "snapshot-source",
}
SNAPSHOT_COLUMNS = tuple(SNAPSHOT)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """Return an initialized, isolated Kanban connection."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    connection = kb.connect()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def deterministic_snapshot(monkeypatch):
    monkeypatch.setattr(
        kb,
        "_resolve_routing_snapshot",
        lambda *args, **kwargs: dict(SNAPSHOT),
    )


def _persisted_snapshot(conn: sqlite3.Connection, task_id: str) -> dict:
    columns = ", ".join(SNAPSHOT_COLUMNS)
    row = conn.execute(
        f"SELECT {columns} FROM task_runs WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    assert row is not None
    return {column: row[column] for column in SNAPSHOT_COLUMNS}


def test_claim_task_persists_complete_snapshot_without_changing_task_role(
    conn, deterministic_snapshot
):
    task_id = kb.create_task(conn, title="implementation", assignee="coder")
    conn.execute(
        "UPDATE tasks SET routing_role = ? WHERE id = ?",
        ("task-role", task_id),
    )
    conn.commit()

    assert kb.claim_task(conn, task_id) is not None

    assert _persisted_snapshot(conn, task_id) == SNAPSHOT
    task = conn.execute(
        "SELECT routing_role FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert task["routing_role"] == "task-role"


def test_claim_review_task_persists_complete_snapshot_without_changing_task_role(
    conn, deterministic_snapshot
):
    task_id = kb.create_task(conn, title="review", assignee="reviewer")
    conn.execute(
        "UPDATE tasks SET status = 'review', routing_role = ? WHERE id = ?",
        ("task-review-role", task_id),
    )
    conn.commit()

    assert kb.claim_review_task(conn, task_id) is not None

    assert _persisted_snapshot(conn, task_id) == SNAPSHOT
    task = conn.execute(
        "SELECT routing_role FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert task["routing_role"] == "task-review-role"


def test_review_capable_role_has_review_specific_source_and_reason(conn, monkeypatch):
    """Review claims distinguish accepted review roles from implementation claims."""
    monkeypatch.setattr(
        kb,
        "_load_roster",
        lambda: (
            {
                "roles": {
                    "reviewer": {
                        "model": "review-model",
                        "provider": "review-provider",
                        "review_capable": True,
                    }
                }
            },
            "review-digest",
        ),
    )
    task_id = kb.create_task(conn, title="review-ready", assignee="coder")
    conn.execute(
        "UPDATE tasks SET status='review', routing_role='reviewer' WHERE id=?",
        (task_id,),
    )

    claimed = kb.claim_review_task(conn, task_id)
    assert claimed is not None
    row = conn.execute(
        "SELECT routing_source,routing_reason FROM task_runs WHERE task_id=? "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()

    assert row["routing_source"] == "review_capable"
    assert row["routing_reason"] == (
        "review phase: role 'reviewer' already review-capable"
    )


def test_claim_rolls_back_task_transition_when_run_snapshot_insert_fails(
    conn, deterministic_snapshot
):
    task_id = kb.create_task(conn, title="atomic claim", assignee="coder")
    conn.commit()
    conn.execute(
        """
        CREATE TRIGGER fail_task_run_insert
        BEFORE INSERT ON task_runs
        BEGIN
            SELECT RAISE(ABORT, 'forced task_runs persistence failure');
        END
        """
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="forced task_runs persistence failure"):
        kb.claim_task(conn, task_id)

    task = conn.execute(
        "SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert dict(task) == {"status": "ready", "current_run_id": None}
    assert conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (task_id,)
    ).fetchone()[0] == 0
