"""Tests for kb.decompose_triage_task — the DB-layer atomic fan-out
from the triage column. LLM-free by design.
"""

from __future__ import annotations

from pathlib import Path
import subprocess

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


def test_decompose_returns_none_when_task_missing(kanban_home):
    with kb.connect() as conn:
        result = kb.decompose_triage_task(
            conn,
            "nonexistent",
            root_assignee="orch",
            children=[{"title": "x"}],
            author="me",
        )
    assert result is None


def test_decompose_returns_none_when_task_not_in_triage(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="already a real task")  # not triage
        result = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[{"title": "x"}],
            author="me",
        )
    assert result is None


def test_decompose_empty_children_returns_none(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        result = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[],
            author="me",
        )
    assert result is None


def test_decompose_rejects_self_parent(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        with pytest.raises(ValueError, match="cannot list itself"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orch",
                children=[{"title": "x", "parents": [0]}],
                author="me",
            )


def test_decompose_rejects_out_of_range_parent(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        with pytest.raises(ValueError, match="not a valid index"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orch",
                children=[{"title": "x", "parents": [5]}],
                author="me",
            )


def test_decompose_rejects_cyclic_parents(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        with pytest.raises(ValueError, match="cyclic dependency"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orch",
                children=[
                    {"title": "A", "parents": [1]},
                    {"title": "B", "parents": [0]},
                ],
                author="me",
            )


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


def test_decompose_children_inherit_dir_workspace(kanban_home):
    """Fan-out children inherit the root's dir workspace, not scratch."""
    proj = "/home/teknium/myproject"
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="codegen root", assignee="worker",
            workspace_kind="dir", workspace_path=proj, triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=[{"title": "part A"}, {"title": "part B", "parents": [0]}],
            author="decomposer",
        )
    assert child_ids and len(child_ids) == 2
    with kb.connect() as conn:
        for cid in child_ids:
            t = kb.get_task(conn, cid)
            assert t.workspace_kind == "dir"
            assert t.workspace_path == proj


def test_decompose_children_stay_scratch_when_root_scratch(kanban_home):
    """No regression: a scratch root still fans out into scratch children."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="scratch root", assignee="worker",
            workspace_kind="scratch", triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=[{"title": "s1"}], author="decomposer",
        )
    with kb.connect() as conn:
        t = kb.get_task(conn, child_ids[0])
    assert t.workspace_kind == "scratch"
    assert t.workspace_path is None


def test_decompose_falls_back_to_scratch_when_worktree_has_no_anchor(kanban_home):
    """An unanchored worktree root must not fan out unspawnable children."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="unanchored worktree root",
            assignee="worker",
            workspace_kind="worktree",
            workspace_path=None,
            triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=[{"title": "part A"}, {"title": "part B", "parents": [0]}],
            author="decomposer",
        )

    assert child_ids and len(child_ids) == 2
    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
        children = [kb.get_task(conn, child_id) for child_id in child_ids]

    assert root is not None
    assert all(child is not None for child in children)
    assert root.workspace_kind == "scratch"
    assert root.workspace_path is None
    assert all(child is not None and child.workspace_kind == "scratch" for child in children)
    assert all(child is not None and child.workspace_path is None for child in children)


def test_decompose_falls_back_to_scratch_for_unanchored_child_override(kanban_home):
    """A child worktree override is validated independently of its root."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="scratch root",
            assignee="worker",
            workspace_kind="scratch",
            triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=[{"title": "worktree child", "workspace_kind": "worktree"}],
            author="decomposer",
        )

    assert child_ids and len(child_ids) == 1
    with kb.connect() as conn:
        child = kb.get_task(conn, child_ids[0])
    assert child is not None
    assert child.workspace_kind == "scratch"
    assert child.workspace_path is None


def test_decompose_falls_back_to_scratch_for_invalid_child_worktree_path(
    kanban_home, tmp_path
):
    """A non-repository child path must not survive as a worktree workspace."""
    invalid_anchor = tmp_path / "not-a-repository"
    invalid_anchor.mkdir()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="root", workspace_kind="scratch", triage=True)
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=[
                {
                    "title": "invalid worktree child",
                    "workspace_kind": "worktree",
                    "workspace_path": str(invalid_anchor),
                }
            ],
            author="decomposer",
        )

    assert child_ids and len(child_ids) == 1
    with kb.connect() as conn:
        child = kb.get_task(conn, child_ids[0])
    assert child is not None
    assert child.workspace_kind == "scratch"
    assert child.workspace_path is None


def test_decompose_falls_back_for_relative_child_worktree_path(kanban_home):
    """Relative paths must not resolve against the dispatcher's incidental CWD."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="root", workspace_kind="scratch", triage=True)
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=[
                {
                    "title": "relative override",
                    "workspace_kind": "worktree",
                    "workspace_path": "relative-worktree",
                }
            ],
            author="decomposer",
        )

    assert child_ids and len(child_ids) == 1
    with kb.connect() as conn:
        child = kb.get_task(conn, child_ids[0])
    assert child is not None
    assert child.workspace_kind == "scratch"
    assert child.workspace_path is None


def test_decompose_preserves_worktrees_with_resolvable_board_default(
    kanban_home, tmp_path
):
    """A real board default keeps root and child worktrees independently resolvable."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"], check=True
    )
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)

    kb.create_board("anchored-worktrees", default_workdir=str(repo))
    with kb.connect(board="anchored-worktrees") as conn:
        tid = kb.create_task(conn, title="root", workspace_kind="worktree", triage=True)
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=[{"title": "child"}],
            author="decomposer",
        )
        assert child_ids and len(child_ids) == 1
        root = kb.get_task(conn, tid)
        child = kb.get_task(conn, child_ids[0])

    assert root is not None
    assert child is not None
    assert root.workspace_kind == "worktree"
    assert child.workspace_kind == "worktree"
    root_workspace = kb.resolve_workspace(root, board="anchored-worktrees")
    child_workspace = kb.resolve_workspace(child, board="anchored-worktrees")
    assert root_workspace.is_dir()
    assert child_workspace.is_dir()
    assert root_workspace != child_workspace


def test_decompose_per_child_workspace_override(kanban_home):
    """An explicit per-child workspace beats inheritance."""
    proj = "/home/teknium/myproject"
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="root", assignee="worker",
            workspace_kind="dir", workspace_path=proj, triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=[
                {"title": "override", "workspace_kind": "dir",
                 "workspace_path": "/other/repo"},
                {"title": "inherit"},
            ],
            author="decomposer",
        )
    with kb.connect() as conn:
        over = kb.get_task(conn, child_ids[0])
        inh = kb.get_task(conn, child_ids[1])
    assert over.workspace_path == "/other/repo"
    assert inh.workspace_path == proj


def test_task_runs_has_task_sequence_index_for_bounded_streak_queries(kanban_home):
    with kb.connect() as conn:
        columns = [
            row[2]
            for row in conn.execute("PRAGMA index_info(idx_runs_task_sequence)")
        ]
    assert columns == ["task_id", "id"]
