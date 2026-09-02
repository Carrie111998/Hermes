from __future__ import annotations

from hermes_cli import kanban_db as kb


def test_end_run_rejects_stale_expected_run_without_touching_successor(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="exact run CAS", assignee="default")
        claimed = kb.claim_task(conn, task_id, claimer="host:first")
        assert claimed is not None
        first_run_id = claimed.current_run_id
        assert first_run_id is not None

        with kb.write_txn(conn):
            now = 1_700_000_000
            successor = conn.execute(
                """
                INSERT INTO task_runs (
                    task_id, profile, status, claim_lock, claim_expires, started_at
                ) VALUES (?, ?, 'running', ?, ?, ?)
                """,
                (task_id, "default", "successor-claim", now + 60, now),
            )
            assert successor.lastrowid is not None
            successor_run_id = int(successor.lastrowid)
            conn.execute(
                """
                UPDATE tasks
                   SET current_run_id = ?, claim_lock = ?, claim_expires = ?
                 WHERE id = ?
                """,
                (successor_run_id, "successor-claim", now + 60, task_id),
            )

        assert (
            kb._end_run(
                conn,
                task_id,
                outcome="reclaimed",
                expected_run_id=first_run_id,
            )
            is None
        )

        task_row = conn.execute(
            "SELECT current_run_id, claim_lock FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        successor_row = conn.execute(
            "SELECT ended_at, status, claim_lock FROM task_runs WHERE id = ?",
            (successor_run_id,),
        ).fetchone()
        assert task_row["current_run_id"] == successor_run_id
        assert task_row["claim_lock"] == "successor-claim"
        assert successor_row["ended_at"] is None
        assert successor_row["status"] == "running"
        assert successor_row["claim_lock"] == "successor-claim"
    finally:
        conn.close()


def test_end_run_closes_exact_current_run_and_clears_its_pointer(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="exact close", assignee="default")
        claimed = kb.claim_task(conn, task_id, claimer="host:exact")
        assert claimed is not None and claimed.current_run_id is not None

        closed = kb._end_run(
            conn,
            task_id,
            outcome="completed",
            expected_run_id=claimed.current_run_id,
        )

        assert closed == claimed.current_run_id
        assert kb._current_run_id(conn, task_id) is None
        run = conn.execute(
            "SELECT ended_at, outcome, claim_lock FROM task_runs WHERE id = ?",
            (claimed.current_run_id,),
        ).fetchone()
        assert run["ended_at"] is not None
        assert run["outcome"] == "completed"
        assert run["claim_lock"] is None
    finally:
        conn.close()
