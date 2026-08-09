"""Regression coverage for strictly read-only Kanban dispatch planning."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def kanban_db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "profiles" / "default").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS.discard(str(kb.kanban_db_path(board="default").resolve()))
    kb.init_db()
    return kb


def _snapshot(conn):
    def rows(sql):
        return [tuple(row) for row in conn.execute(sql)]

    return {
        "tasks": rows(
            "SELECT id, status, assignee, claim_lock, claim_expires, current_run_id, "
            "workspace_path, worker_pid FROM tasks ORDER BY id"
        ),
        "events": rows("SELECT task_id, kind, payload FROM task_events ORDER BY id"),
        "runs": rows(
            "SELECT task_id, status, outcome, claim_lock, worker_pid FROM task_runs ORDER BY id"
        ),
    }


def test_dry_run_is_read_only_and_plans_each_dispatchable_column(kanban_db, monkeypatch):
    kb = kanban_db
    with kb.connect_closing() as conn:
        ready_id = kb.create_task(conn, title="ready", assignee="default")
        review_id = kb.create_task(
            conn, title="review", assignee="default", initial_status="review"
        )
        blocked_id = kb.create_task(
            conn, title="blocked", assignee="default", initial_status="blocked"
        )
        triage_id = kb.create_task(
            conn, title="triage", assignee="default", initial_status="triage"
        )
        todo_id = kb.create_task(conn, title="todo", assignee="default")
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (todo_id,))
        statuses = {
            row["id"]: row["status"]
            for row in conn.execute("SELECT id, status FROM tasks")
        }
        assert statuses[blocked_id] == "blocked"
        assert statuses[triage_id] == "triage"
        assert statuses[todo_id] == "todo"
        before = _snapshot(conn)

        spawn_calls = []

        def spawn(*args, **kwargs):
            spawn_calls.append(args)
            return 123

        monkeypatch.setattr(kb, "reap_worker_zombies", lambda: (_ for _ in ()).throw(
            AssertionError("dry-run must not reap workers")
        ))
        monkeypatch.setattr(kb, "_dispatch_tick_lock", lambda *args: (_ for _ in ()).throw(
            AssertionError("dry-run must not acquire the dispatch lock")
        ))

        result = kb.dispatch_once(conn, dry_run=True, spawn_fn=spawn)
        after = _snapshot(conn)

    assert spawn_calls == []
    assert after == before
    assert result.promoted >= 1
    planned = {task_id for task_id, _assignee, _workspace in result.spawned}
    assert ready_id in planned
    assert review_id in planned
    # A dependency-free blocked card is prospective recovery work, but the
    # source card remains blocked because the plan ran on an in-memory copy.
    assert blocked_id in planned
    assert triage_id not in planned
    assert todo_id in planned


def test_normal_dispatch_still_promotes_claims_and_spawns(kanban_db):
    kb = kanban_db
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="ready", assignee="default")
        calls = []

        def spawn(task, workspace, **kwargs):
            calls.append((task.id, workspace))
            return 321

        result = kb.dispatch_once(conn, dry_run=False, spawn_fn=spawn)
        row = conn.execute(
            "SELECT status, claim_lock, current_run_id, worker_pid FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()

    assert result.spawned and result.spawned[0][0] == task_id
    assert calls and calls[0][0] == task_id
    assert row["status"] == "running"
    assert row["claim_lock"]
    assert row["current_run_id"] is not None
    assert row["worker_pid"] == 321
