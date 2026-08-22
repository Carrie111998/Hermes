"""Behavior coverage for live Kanban worker observability."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _load_dashboard_plugin():
    plugin_file = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "kanban"
        / "dashboard"
        / "plugin_api.py"
    )
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_observability_test",
        plugin_file,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_progress_round_trips_to_task_run_and_dashboard(kanban_home):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="observable", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        run_id = claimed.current_run_id

        assert kb.update_worker_progress(
            conn,
            task_id,
            current_step="Running tests",
            progress_percent=37,
            latest_log="12 tests collected",
            files_changed=["src/api.py", "src/api.py", "tests/test_api.py"],
            expected_run_id=run_id,
            touch_heartbeat=True,
        )

        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id)
        assert task is not None and run is not None
        assert task.current_step == run.current_step == "Running tests"
        assert task.progress_percent == run.progress_percent == 37
        assert task.latest_log == run.latest_log == "12 tests collected"
        assert task.files_changed == run.files_changed == [
            "src/api.py",
            "tests/test_api.py",
        ]
        assert task.last_heartbeat_at == run.last_heartbeat_at

        plugin = _load_dashboard_plugin()
        rendered = plugin._task_dict(task, stalled_after_seconds=300)
        assert rendered["current_step"] == "Running tests"
        assert rendered["progress_percent"] == 37
        assert rendered["worker_state"] == "waiting_tool"
        assert rendered["is_stalled"] is False


def test_output_ready_closes_run_with_terminal_progress(kanban_home):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="deliverable", assignee="worker")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        run_id = claimed.current_run_id
        assert kb.update_worker_progress(
            conn,
            task_id,
            current_step="Writing patch",
            progress_percent=72,
            expected_run_id=run_id,
        )

        assert kb.publish_task_output(
            conn,
            task_id,
            summary="Patch and verification delivered",
            expected_run_id=run_id,
        )
        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id)
        assert task is not None and run is not None
        assert task.status == "output_ready"
        assert task.current_step == run.current_step == "Output delivered"
        assert task.progress_percent == run.progress_percent == 100
        assert run.latest_log == "Patch and verification delivered"
        assert kb.task_observability(task)["worker_state"] == "awaiting_review"


def test_stalled_is_a_warning_based_on_heartbeat_not_total_runtime(kanban_home):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="long but alive", assignee="worker")
        task = kb.claim_task(conn, task_id)
        assert task is not None
        assert kb.heartbeat_worker(
            conn,
            task_id,
            current_step="Waiting for model",
            expected_run_id=task.current_run_id,
        )
        task = kb.get_task(conn, task_id)
        assert task is not None and task.last_heartbeat_at is not None

        fresh = kb.task_observability(
            task,
            now=task.last_heartbeat_at + 299,
            stalled_after_seconds=300,
        )
        stalled = kb.task_observability(
            task,
            now=task.last_heartbeat_at + 301,
            stalled_after_seconds=300,
        )
        assert fresh["is_stalled"] is False
        assert stalled["is_stalled"] is True
        assert stalled["worker_state"] == "stalled"


def test_reclaimed_worker_cannot_overwrite_successor_progress(
    kanban_home,
    monkeypatch,
):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="retry", assignee="worker")
        first = kb.claim_task(conn, task_id)
        assert first is not None and first.current_run_id is not None
        kb._set_worker_pid(conn, task_id, 98765)
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        assert kb.detect_crashed_workers(conn) == [task_id]

        second = kb.claim_task(conn, task_id)
        assert second is not None and second.current_run_id != first.current_run_id
        assert not kb.update_worker_progress(
            conn,
            task_id,
            current_step="stale callback",
            progress_percent=99,
            expected_run_id=first.current_run_id,
        )
        assert kb.update_worker_progress(
            conn,
            task_id,
            current_step="new worker",
            progress_percent=10,
            expected_run_id=second.current_run_id,
        )
        landed = kb.get_task(conn, task_id)
        assert landed is not None
        assert landed.current_step == "new worker"
        assert landed.progress_percent == 10


def test_legacy_task_and_run_tables_gain_observability_columns(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    db_path = home / "legacy.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT,
                assignee TEXT, status TEXT NOT NULL, priority INTEGER DEFAULT 0,
                created_by TEXT, created_at INTEGER NOT NULL, started_at INTEGER,
                completed_at INTEGER, workspace_kind TEXT NOT NULL DEFAULT 'scratch',
                workspace_path TEXT, claim_lock TEXT, claim_expires INTEGER
            );
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                profile TEXT, step_key TEXT, status TEXT NOT NULL,
                claim_lock TEXT, claim_expires INTEGER, worker_pid INTEGER,
                max_runtime_seconds INTEGER, last_heartbeat_at INTEGER,
                started_at INTEGER NOT NULL, ended_at INTEGER, outcome TEXT,
                summary TEXT, metadata TEXT, error TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    kb.init_db(db_path=db_path)
    with sqlite3.connect(db_path) as migrated:
        task_columns = {row[1] for row in migrated.execute("PRAGMA table_info(tasks)")}
        run_columns = {row[1] for row in migrated.execute("PRAGMA table_info(task_runs)")}
    expected = {
        "current_step",
        "progress_percent",
        "latest_log",
        "files_changed",
        "progress_updated_at",
    }
    assert expected <= task_columns
    assert expected <= run_columns
