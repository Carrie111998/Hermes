"""Duplicate resolution governance tests.

These tests validate that the kanban system provides first-class duplicate
resolution primitives: duplicate_of, intentional_sibling_of, and distinct.

The lifecycle contract is:
* Two materially identical tasks can be resolved as duplicates
* Related but distinct tasks can be marked as intentional siblings
* Disputed duplicates can be explicitly passed with reason
* Each duplicate resolution is first-class board data, not prose
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _events(conn, tid, kind=None):
    """Fetch events for a task, optionally filtered by kind."""
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
        (tid,),
    ).fetchall()
    out = [
        (r["kind"], json.loads(r["payload"]) if r["payload"] else None)
        for r in rows
    ]
    if kind is not None:
        out = [e for e in out if e[0] == kind]
    return out


def test_resolve_duplicate_creates_first_class_event(kanban_home: Path) -> None:
    """Resolving a duplicate creates a persistent task event with kind and metadata."""
    with kb.connect() as conn:
        # Create two materially identical tasks
        task_id = kb.create_task(conn, title="analyze sales report", assignee="analyst")
        dup_task_id = kb.create_task(
            conn, title="analyze sales report", assignee="analyst"
        )

        # Resolve the second as a duplicate of the first
        kb.resolve_duplicate(
            conn,
            task_id=dup_task_id,
            kind="duplicate_of",
            other_task_id=task_id,
            reason="exact duplicate, analysis already in progress",
        )

        # Verify event was persisted
        events = _events(conn, dup_task_id, kind="duplicate_resolved")
        assert len(events) == 1
        kind, payload = events[0]
        assert kind == "duplicate_resolved"
        assert payload["resolution_kind"] == "duplicate_of"
        assert payload["other_task_id"] == task_id
        assert payload["reason"] == "exact duplicate, analysis already in progress"


def test_resolve_sibling_creates_intentional_sibling_event(kanban_home: Path) -> None:
    """Marking tasks as intentional siblings creates a persistent event."""
    with kb.connect() as conn:
        # Create two related but distinct tasks
        task_id = kb.create_task(conn, title="implement feature A", assignee="dev")
        sibling_id = kb.create_task(
            conn, title="implement feature A variant for special case", assignee="dev"
        )

        # Mark as intentional siblings
        kb.resolve_duplicate(
            conn,
            task_id=sibling_id,
            kind="intentional_sibling_of",
            other_task_id=task_id,
            reason="parallel implementation for edge case, not a duplicate",
        )

        # Verify event
        events = _events(conn, sibling_id, kind="duplicate_resolved")
        assert len(events) == 1
        kind, payload = events[0]
        assert kind == "duplicate_resolved"
        assert payload["resolution_kind"] == "intentional_sibling_of"
        assert payload["other_task_id"] == task_id


def test_resolve_distinct_marks_explicit_pass(kanban_home: Path) -> None:
    """Marking tasks as distinct despite similar names creates a persistent event."""
    with kb.connect() as conn:
        # Create two tasks with similar names but different scopes
        task_id = kb.create_task(conn, title="analyze Q3 sales", assignee="analyst")
        other_id = kb.create_task(conn, title="analyze Q4 sales", assignee="analyst")

        # Mark as explicitly distinct
        kb.resolve_duplicate(
            conn,
            task_id=other_id,
            kind="distinct",
            other_task_id=task_id,
            reason="different quarters, separate analyses required",
        )

        # Verify event
        events = _events(conn, other_id, kind="duplicate_resolved")
        assert len(events) == 1
        kind, payload = events[0]
        assert kind == "duplicate_resolved"
        assert payload["resolution_kind"] == "distinct"
        assert payload["reason"] == "different quarters, separate analyses required"


def test_resolve_duplicate_invalid_kind_rejected(kanban_home: Path) -> None:
    """resolve_duplicate with invalid kind is rejected."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="task", assignee="dev")
        other_id = kb.create_task(conn, title="task", assignee="dev")

        # Attempt invalid kind
        with pytest.raises(ValueError, match="resolution_kind"):
            kb.resolve_duplicate(
                conn,
                task_id=other_id,
                kind="invalid_kind",
                other_task_id=task_id,
                reason="test",
            )
