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


def test_decompose_missing_parents_is_parallel(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[{"title": "parallel task"}],
            author="alice",
        )

    assert child_ids is not None
    with kb.connect() as conn:
        child = kb.get_task(conn, child_ids[0])
    assert child is not None
    assert child.status == "ready"


@pytest.mark.parametrize(
    ("parents", "child_index", "child_count"),
    [
        ([-1], 1, 2),
        ([2], 1, 2),
        ([True], 2, 3),
        ([False], 1, 2),
        ([0, True], 2, 3),
    ],
    ids=["negative", "out-of-range", "true", "false", "mixed"],
)
def test_decompose_rejects_invalid_parent_indices_atomically(
    kanban_home, parents, child_index, child_count
):
    with kb.connect() as conn:
        tid = _create_triage(conn)

    children = [
        {"title": f"child {idx}", "parents": []}
        for idx in range(child_count)
    ]
    children[child_index]["parents"] = parents

    with kb.connect() as conn:
        with pytest.raises(ValueError, match="parents"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orch",
                children=children,
                author="alice",
            )

    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
        task_count = conn.execute("SELECT COUNT(*) AS count FROM tasks").fetchone()["count"]
        link_count = conn.execute("SELECT COUNT(*) AS count FROM task_links").fetchone()["count"]
    assert root is not None
    assert root.status == "triage"
    assert task_count == 1
    assert link_count == 0


@pytest.mark.parametrize("parents", [True, False, None], ids=["true", "false", "null"])
def test_decompose_rejects_present_non_list_parents(kanban_home, parents):
    with kb.connect() as conn:
        tid = _create_triage(conn)

    with kb.connect() as conn:
        with pytest.raises(ValueError, match="parents must be a list"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orch",
                children=[
                    {"title": "parallel"},
                    {"title": "invalid", "parents": parents},
                ],
                author="alice",
            )




