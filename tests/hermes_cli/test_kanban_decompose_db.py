"""Tests for kb.decompose_triage_task — the DB-layer atomic fan-out
from the triage column. LLM-free by design.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _create_triage(conn, title="rough idea", body=None, assignee=None, tenant=None):
    return kb.create_task(
        conn,
        title=title,
        body=body,
        assignee=assignee,
        tenant=tenant,
        triage=True,
    )


def test_decompose_creates_children_and_promotes_root(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn, title="ship a feature")
        assert kb.get_task(conn, tid).status == "triage"

    children = [
        {"title": "research", "body": "look at prior art", "assignee": "researcher", "parents": []},
        {"title": "build it", "body": "write code", "assignee": "engineer", "parents": [0]},
    ]
    with kb.connect() as conn:
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=children,
            author="decomposer",
        )
    assert child_ids is not None
    assert len(child_ids) == 2

    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, child_ids[0])
        c1 = kb.get_task(conn, child_ids[1])

    # Root flipped to todo with orchestrator assignee, gated by children.
    assert root.status == "todo"
    assert root.assignee == "orchestrator"
    # First child has no internal parents → ready on recompute_ready.
    assert c0.status == "ready"
    assert c0.assignee == "researcher"
    # Second child has parents=[0] → stays in todo until c0 completes.
    assert c1.status == "todo"
    assert c1.assignee == "engineer"


@pytest.mark.parametrize(
    "parents_value",
    [None, "", 0, "0", ["0"], [True], [0.0], [-1], [1]],
)
def test_decompose_rejects_supplied_invalid_parents_atomically(
    kanban_home, parents_value,
):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        root_before = kb.get_task(conn, tid)
        with pytest.raises(ValueError):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orch",
                children=[{"title": "x", "parents": parents_value}],
                author="me",
            )

        root_after = kb.get_task(conn, tid)
        child_count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE id != ?", (tid,)
        ).fetchone()[0]
        link_count = conn.execute(
            "SELECT COUNT(*) FROM task_links WHERE parent_id = ? OR child_id = ?",
            (tid, tid),
        ).fetchone()[0]
        rejection_events = [
            event for event in kb.list_events(conn, tid)
            if event.kind == "decompose_rejected"
        ]

    assert root_after == root_before
    assert child_count == 0
    assert link_count == 0
    assert len(rejection_events) == 1
    assert rejection_events[0].payload["rejection_class"] == "invalid_dependency_graph"
    assert "raw" not in str(rejection_events[0].payload).lower()


def test_decompose_rejection_events_are_durable_across_valid_retry(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)

        for invalid_value in (None, ""):
            with pytest.raises(ValueError):
                kb.decompose_triage_task(
                    conn,
                    tid,
                    root_assignee="orch",
                    children=[{"title": "x", "parents": invalid_value}],
                    author="me",
                )

        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[{"title": "valid after retry"}],
            author="me",
        )
        events = kb.list_events(conn, tid)

    assert child_ids and len(child_ids) == 1
    assert [event.kind for event in events].count("decompose_rejected") == 2
    assert [event.kind for event in events].count("decomposed") == 1


def test_decompose_records_audit_comment_and_event(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[{"title": "task A", "assignee": "researcher"}],
            author="alice",
        )
    assert child_ids is not None

    with kb.connect() as conn:
        comments = kb.list_comments(conn, tid)
        events = kb.list_events(conn, tid)

    assert any("Decomposed into" in (c.body or "") for c in comments)
    assert any(ev.kind == "decomposed" for ev in events)




