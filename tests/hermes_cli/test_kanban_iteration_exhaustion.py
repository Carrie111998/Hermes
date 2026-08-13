from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    return db_path


def test_iteration_exhaustion_terminalizes_exact_run_and_preserves_workspace(
    kanban_db: Path, tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    evidence = workspace / "evidence.txt"
    evidence.write_text("preserve me", encoding="utf-8")

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="bounded implementation",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        claimed = kb.claim_task(conn, task_id, claimer="worker:one")
        assert claimed is not None
        run_id = claimed.current_run_id
        assert run_id is not None

        closed_run_id = kb._record_iteration_exhaustion(
            conn,
            task_id,
            expected_run_id=run_id,
            budget_used=60,
            budget_max=60,
        )
        assert closed_run_id == run_id

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.block_kind == "iteration_exhausted"
        assert task.current_run_id is None
        assert task.worker_pid is None
        assert task.claim_lock is None
        assert task.workspace_path == str(workspace)
        assert evidence.read_text(encoding="utf-8") == "preserve me"
        assert task.max_retries == 0

        run = conn.execute(
            "SELECT status, outcome, metadata FROM task_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        assert run["status"] == "iteration_exhausted"
        assert run["outcome"] == "iteration_exhausted"
        metadata = json.loads(run["metadata"])
        assert metadata == {
            "budget_used": 60,
            "budget_max": 60,
            "checkpoint_required": True,
            "workspace_preserved": True,
            "workspace_path": str(workspace),
            "retryable": False,
            "resume_policy": "never",
        }

        events = conn.execute(
            "SELECT run_id, payload FROM task_events "
            "WHERE task_id = ? AND kind = 'iteration_exhausted'",
            (task_id,),
        ).fetchall()
        assert len(events) == 1
        assert events[0]["run_id"] == run_id
        payload = json.loads(events[0]["payload"])
        assert payload["terminal_run_id"] == run_id
        assert payload["budget_used"] == 60
        assert payload["budget_max"] == 60
        assert payload["checkpoint_required"] is True
        assert payload["workspace_preserved"] is True
        assert payload["workspace_path"] == str(workspace)
        assert payload["retryable"] is False
        assert payload["resume_policy"] == "never"

        assert kb.claim_task(conn, task_id, claimer="worker:retry") is None
        assert kb.recompute_ready(conn) == 0
        assert kb.unblock_task(conn, task_id) is False
        assert kb.get_task(conn, task_id).status == "blocked"
        assert kb.goal_run_status(conn, task_id, expected_run_id=run_id) == "blocked"

        # Same run finalization is idempotent: no duplicate event or mutation.
        assert kb._record_iteration_exhaustion(
            conn,
            task_id,
            expected_run_id=run_id,
            budget_used=60,
            budget_max=60,
        ) == run_id
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events "
            "WHERE task_id = ? AND kind = 'iteration_exhausted'",
            (task_id,),
        ).fetchone()[0] == 1


def test_iteration_exhaustion_cannot_terminalize_a_successor_run(
    kanban_db: Path,
) -> None:
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="revision guard", assignee="worker")
        first = kb.claim_task(conn, task_id, claimer="worker:first")
        assert first is not None and first.current_run_id is not None
        stale_run_id = first.current_run_id

        with kb.write_txn(conn):
            kb._end_run(conn, task_id, outcome="released", status="released")
            conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL WHERE id = ?",
                (task_id,),
            )
        successor = kb.claim_task(conn, task_id, claimer="worker:successor")
        assert successor is not None
        successor_run_id = successor.current_run_id
        assert successor_run_id is not None and successor_run_id != stale_run_id

        assert kb._record_iteration_exhaustion(
            conn,
            task_id,
            expected_run_id=stale_run_id,
            budget_used=60,
            budget_max=60,
        ) is None
        current = kb.get_task(conn, task_id)
        assert current is not None
        assert current.status == "running"
        assert current.current_run_id == successor_run_id
        assert current.claim_lock == "worker:successor"


def test_wall_clock_timeout_remains_retryable(kanban_db: Path, monkeypatch) -> None:
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="wall clock timeout",
            assignee="worker",
            max_runtime_seconds=1,
        )
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        kb._set_worker_pid(conn, task_id, os.getpid())
        old_started = int(time.time()) - 30
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET started_at = ? WHERE id = ?",
                (old_started, claimed.current_run_id),
            )

        assert kb.enforce_max_runtime(conn, signal_fn=lambda *_args: None) == [task_id]
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"
        assert task.block_kind is None
        assert task.max_retries is None
        assert kb.claim_task(conn, task_id, claimer="worker:retry") is not None
        timeout = next(e for e in kb.list_events(conn, task_id) if e.kind == "timed_out")
        assert timeout.payload["retry_status"] == "ready"
