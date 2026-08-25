"""Focused validation for the profile statistics report."""

from __future__ import annotations

import json
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_stats


def _home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_empty_report_has_no_profiles_or_attempts(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kb.connect_closing() as conn:
        report = kanban_stats.build_report(conn, now=100)
    assert report["task_count"] == 0
    assert report["attempt_count"] == 0
    assert report["profiles"] == []
    assert report["notes"]["attempts_are_task_runs"] is True


def test_report_distinguishes_tasks_from_retried_attempts_and_block_kinds(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="retry me", assignee="alice", tenant="acme")
        archived_id = kb.create_task(conn, title="old", assignee="alice", tenant="acme")
        conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (archived_id,))
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome) "
            "VALUES (?, 'alice', 'done', 110, 130, 'crashed'), (?, 'alice', 'done', 140, 170, 'completed')",
            (task_id, task_id),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'blocked', ?, 120)",
            (task_id, json.dumps({"kind": "needs_input"})),
        )
        conn.commit()
        report = kanban_stats.build_report(conn, tenant="acme", now=200)
        row = next(item for item in report["profiles"] if item["name"] == "alice")
    assert report["task_count"] == 1
    assert report["attempt_count"] == 2
    assert row["assigned_tasks"] == 1
    assert row["attempts"] == 2
    assert row["retried_tasks"] == 1
    assert row["completed_tasks"] == 1
    assert row["failed_attempts"] == 1
    assert row["blocker_classes"] == {"needs_input": 1}


def test_archived_and_profile_filters_are_explicit(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kb.connect_closing() as conn:
        live = kb.create_task(conn, title="live", assignee="alice")
        old = kb.create_task(conn, title="old", assignee="bob")
        conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (old,))
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome) "
            "VALUES (?, 'alice', 'done', 100, 110, 'completed'), (?, 'bob', 'done', 100, 110, 'completed')",
            (live, old),
        )
        conn.commit()
        filtered = kanban_stats.build_report(conn, profile="alice")
        archived = kanban_stats.build_report(conn, include_archived=True)
    assert filtered["task_count"] == 1
    assert filtered["attempt_count"] == 1
    assert archived["task_count"] == 2
    assert archived["attempt_count"] == 2
