"""Recovery tests for kanban workers that never record a PID."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db(board="default")
    with kb.connect(board="default") as connection:
        yield connection


def test_detect_never_started_requeues_aged_pidless_claim_as_spawn_failure(conn):
    task_id = kb.create_task(conn, title="ghost worker", assignee="worker")
    claimed = kb.claim_task(conn, task_id, claimer=kb._dispatcher_claimer_id())
    assert claimed is not None

    old_started = int(time.time()) - 120
    conn.execute(
        "UPDATE task_runs SET started_at = ? WHERE id = ?",
        (old_started, claimed.current_run_id),
    )
    conn.commit()

    recovered = kb.detect_never_started(
        conn,
        grace_seconds=60,
        failure_limit=2,
        board="default",
    )

    assert recovered == [task_id]
    task = kb.get_task(conn, task_id)
    assert task.status == "ready"
    assert task.claim_lock is None
    assert task.worker_pid is None
    assert task.consecutive_failures == 1

    run = kb.latest_run(conn, task_id)
    assert run.status == "spawn_failed"
    assert run.outcome == "spawn_failed"
    assert run.ended_at is not None

    events = kb.list_events(conn, task_id)
    failure = next(event for event in events if event.kind == "spawn_failed")
    assert failure.payload["reason"] == "worker_never_started"
    assert failure.payload["grace_seconds"] == 60


def test_dispatch_tick_surfaces_never_started_recovery(conn, monkeypatch):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    task_id = kb.create_task(conn, title="dispatch ghost", assignee="worker")
    first = kb.dispatch_once(
        conn,
        spawn_fn=lambda *_args, **_kwargs: None,
        board="default",
    )
    assert [task_id for task_id, _assignee, _workspace in first.spawned] == [
        task_id
    ]
    claimed = kb.get_task(conn, task_id)
    assert claimed is not None
    assert claimed.claim_lock == kb._dispatcher_claimer_id()

    old_started = int(time.time()) - 120
    conn.execute(
        "UPDATE task_runs SET started_at = ? WHERE id = ?",
        (old_started, claimed.current_run_id),
    )
    conn.commit()
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "30")

    result = kb.dispatch_once(
        conn,
        max_spawn=0,
        failure_limit=2,
        board="default",
    )

    assert result.never_started == [task_id]
    assert kb.get_task(conn, task_id).status == "ready"


def test_stale_log_from_previous_attempt_does_not_hide_never_started_worker(conn):
    task_id = kb.create_task(conn, title="retry ghost", assignee="worker")
    log_path = kb.worker_logs_dir(board="default") / f"{task_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("prior attempt\n", encoding="utf-8")
    prior_mtime = int(time.time()) - 3600
    os.utime(log_path, (prior_mtime, prior_mtime))

    claimed = kb.claim_task(conn, task_id, claimer=kb._dispatcher_claimer_id())
    assert claimed is not None
    active_started = int(time.time()) - 120
    conn.execute(
        "UPDATE task_runs SET started_at = ? WHERE id = ?",
        (active_started, claimed.current_run_id),
    )
    conn.commit()

    recovered = kb.detect_never_started(
        conn,
        grace_seconds=60,
        failure_limit=2,
        board="default",
    )

    assert recovered == [task_id]


def test_current_attempt_log_preserves_pidless_claim(conn):
    task_id = kb.create_task(conn, title="worker may be alive", assignee="worker")
    claimed = kb.claim_task(conn, task_id, claimer=kb._dispatcher_claimer_id())
    assert claimed is not None
    active_started = int(time.time()) - 120
    conn.execute(
        "UPDATE task_runs SET started_at = ? WHERE id = ?",
        (active_started, claimed.current_run_id),
    )
    conn.commit()

    log_path = kb.worker_logs_dir(board="default") / f"{task_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("active attempt\n", encoding="utf-8")

    assert kb.detect_never_started(
        conn,
        grace_seconds=60,
        board="default",
    ) == []
    assert kb.get_task(conn, task_id).status == "running"


def test_never_started_recovery_ignores_other_hosts_claim(conn):
    task_id = kb.create_task(conn, title="remote claim", assignee="worker")
    claimed = kb.claim_task(conn, task_id, claimer="another-host:1:dispatch")
    assert claimed is not None
    old_started = int(time.time()) - 120
    conn.execute(
        "UPDATE task_runs SET started_at = ? WHERE id = ?",
        (old_started, claimed.current_run_id),
    )
    conn.commit()

    assert kb.detect_never_started(
        conn,
        grace_seconds=60,
        board="default",
    ) == []
    assert kb.get_task(conn, task_id).status == "running"


def test_never_started_recovery_preserves_direct_manual_claim(conn):
    task_id = kb.create_task(conn, title="terminal lane", assignee="worker")
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    old_started = int(time.time()) - 120
    conn.execute(
        "UPDATE task_runs SET started_at = ? WHERE id = ?",
        (old_started, claimed.current_run_id),
    )
    conn.commit()

    assert kb.detect_never_started(
        conn,
        grace_seconds=60,
        board="default",
    ) == []
    task = kb.get_task(conn, task_id)
    assert task.status == "running"
    assert task.consecutive_failures == 0


def test_never_started_recovery_trips_existing_failure_limit(conn):
    task_id = kb.create_task(conn, title="repeated spawn failure", assignee="worker")
    claimed = kb.claim_task(conn, task_id, claimer=kb._dispatcher_claimer_id())
    assert claimed is not None
    old_started = int(time.time()) - 120
    conn.execute(
        "UPDATE tasks SET consecutive_failures = 1 WHERE id = ?",
        (task_id,),
    )
    conn.execute(
        "UPDATE task_runs SET started_at = ? WHERE id = ?",
        (old_started, claimed.current_run_id),
    )
    conn.commit()

    assert kb.detect_never_started(
        conn,
        grace_seconds=60,
        failure_limit=2,
        board="default",
    ) == [task_id]

    task = kb.get_task(conn, task_id)
    assert task.status == "blocked"
    assert task.consecutive_failures == 2
    assert getattr(kb.detect_never_started, "_last_auto_blocked") == [task_id]
