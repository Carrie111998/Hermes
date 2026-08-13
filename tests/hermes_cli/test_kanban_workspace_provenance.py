"""Workspace provenance event contracts for Kanban tasks."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _event(conn, task_id: str, kind: str):
    return [event for event in kb.list_events(conn, task_id) if event.kind == kind]


def test_created_event_preserves_requested_and_normalized_workspace(kanban_home: Path) -> None:
    requested_path = kanban_home / "repo"
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="explicit workspace",
            workspace_kind="dir",
            workspace_path=str(requested_path),
            requested_workspace=f"dir:{requested_path}",
        )

        created = _event(conn, task_id, "created")

    assert len(created) == 1
    assert created[0].payload["requested_workspace"] == f"dir:{requested_path}"
    assert created[0].payload["workspace_kind"] == "dir"
    assert created[0].payload["workspace_path"] == str(requested_path)


def test_created_event_records_null_when_workspace_was_omitted(kanban_home: Path) -> None:
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="implicit workspace")

        created = _event(conn, task_id, "created")

    assert len(created) == 1
    assert "requested_workspace" in created[0].payload
    assert created[0].payload["requested_workspace"] is None
    assert created[0].payload["workspace_kind"] == "scratch"
    assert created[0].payload["workspace_path"] is None


def test_unchanged_workspace_resolution_emits_no_event(kanban_home: Path) -> None:
    path = kanban_home / "workspace"
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="already resolved", workspace_kind="dir", workspace_path=str(path)
        )

        changed = kb.set_workspace_path(conn, task_id, path, branch_name=None)

        assert changed is False
        assert _event(conn, task_id, "workspace_resolved") == []


def test_changed_workspace_resolution_updates_row_and_emits_payload_atomically(
    kanban_home: Path,
) -> None:
    previous = kanban_home / "requested"
    resolved = kanban_home / "repo" / ".worktrees" / "task"
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="resolved elsewhere",
            workspace_kind="worktree",
            workspace_path=str(previous),
            branch_name="old/branch",
        )
        created_before = _event(conn, task_id, "created")[0]

        changed = kb.set_workspace_path(
            conn, task_id, resolved, branch_name="wt/resolved"
        )

        task = kb.get_task(conn, task_id)
        events = _event(conn, task_id, "workspace_resolved")
        created_after = _event(conn, task_id, "created")[0]

    assert changed is True
    assert task.workspace_path == str(resolved)
    assert task.branch_name == "wt/resolved"
    assert len(events) == 1
    assert events[0].payload == {
        "previous_path": str(previous),
        "resolved_path": str(resolved),
        "branch_name": "wt/resolved",
    }
    assert created_after.id == created_before.id
    assert created_after.payload == created_before.payload


def test_legacy_claim_time_healing_is_visible_without_rewriting_created_event(
    kanban_home: Path,
) -> None:
    previous = kanban_home / "legacy-parent-worktree"
    resolved = kanban_home / "repo" / ".worktrees" / "legacy-child"
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="legacy child",
            workspace_kind="worktree",
            workspace_path=str(previous),
        )
        legacy_payload = {"workspace_kind": "worktree", "workspace_path": str(previous)}
        conn.execute(
            "UPDATE task_events SET payload = ? WHERE task_id = ? AND kind = 'created'",
            (json.dumps(legacy_payload), task_id),
        )
        conn.commit()

        kb.set_workspace_path(conn, task_id, resolved, branch_name="wt/legacy-child")

        created = _event(conn, task_id, "created")
        resolved_events = _event(conn, task_id, "workspace_resolved")

    assert len(created) == 1
    assert created[0].payload == legacy_payload
    assert len(resolved_events) == 1
    assert resolved_events[0].payload == {
        "previous_path": str(previous),
        "resolved_path": str(resolved),
        "branch_name": "wt/legacy-child",
    }


def test_repeated_resolution_does_not_duplicate_event(kanban_home: Path) -> None:
    first = kanban_home / "first"
    resolved = kanban_home / "resolved"
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="repeat claim", workspace_kind="worktree", workspace_path=str(first)
        )

        assert kb.set_workspace_path(conn, task_id, resolved, branch_name="wt/repeat") is True
        assert kb.set_workspace_path(conn, task_id, resolved, branch_name="wt/repeat") is False

        assert len(_event(conn, task_id, "workspace_resolved")) == 1


def test_resolution_event_failure_rolls_back_task_row(kanban_home: Path) -> None:
    previous = kanban_home / "previous"
    resolved = kanban_home / "resolved"
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="atomic resolution", workspace_kind="dir", workspace_path=str(previous)
        )
        conn.execute(
            """
            CREATE TRIGGER reject_workspace_resolution
            BEFORE INSERT ON task_events
            WHEN NEW.kind = 'workspace_resolved'
            BEGIN
                SELECT RAISE(ABORT, 'reject resolution event');
            END
            """
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError, match="reject resolution event"):
            kb.set_workspace_path(conn, task_id, resolved)

        task = kb.get_task(conn, task_id)

    assert task is not None
    assert task.workspace_path == str(previous)
