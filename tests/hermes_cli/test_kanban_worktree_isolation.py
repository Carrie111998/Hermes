"""Per-task worktree isolation for decompose siblings.

Decompose children used to inherit the root's literal ``workspace_path``,
so every sibling of a worktree-kind root pointed at the SAME checkout —
and ``_resolve_worktree_workspace``'s existing-checkout shortcut reused it
on whatever branch was there, letting sibling workers run concurrently in
one directory on one branch (cross-task provenance corruption, no lock).

Two-part fix under test:
- unlinked worktree children leave ``workspace_path`` unset so dispatch gives
  each child its own board-anchored ``<repo>/.worktrees/<child-id>``;
  project-linked children instead persist that unique path under the declared
  project's repo, so an unrelated board default can never capture them;
- ``_resolve_worktree_workspace`` falls back to a fresh per-task worktree
  when the requested path is occupied by another task's branch (heals
  pre-existing rows that still carry a shared path).
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [
            "git", "-C", str(cwd),
            "-c", "user.name=Test User",
            "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        check=True, capture_output=True, text=True,
    )


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True, capture_output=True, text=True,
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


def _add_worktree(repo: Path, target: Path, branch: str) -> Path:
    _git(repo, "worktree", "add", str(target), "-b", branch, "HEAD")
    return target


def test_decompose_worktree_children_get_own_workspace(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="build the feature", triage=True)
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', "
            "workspace_path='/repo/.worktrees/root' WHERE id = ?",
            (root,),
        )
        conn.commit()

        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {"title": "spec it", "assignee": "alice", "parents": []},
                {"title": "implement it", "assignee": "bob", "parents": [0]},
            ],
            author="decomposer",
        )
        assert child_ids is not None and len(child_ids) == 2

        for cid in child_ids:
            row = conn.execute(
                "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
                (cid,),
            ).fetchone()
            assert row["workspace_kind"] == "worktree"
            # Each child resolves its own <repo>/.worktrees/<child-id> at
            # dispatch; the root's literal path must never be shared.
            assert row["workspace_path"] is None


def test_decompose_project_child_routes_to_project_not_board_default(
    kanban_home, tmp_path
):
    project_repo = _make_repo(tmp_path, "project-a")
    board_repo = _make_repo(tmp_path, "board-default-b")

    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Project A",
            primary_path=str(project_repo),
        )
        project = pdb.get_project(project_conn, project_id)
    assert project is not None

    kb.write_board_metadata(
        "default",
        default_workdir=str(board_repo),
        project_id="",
    )
    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(
            conn,
            title="Project A root",
            triage=True,
            project_id=project_id,
            board="default",
        )
        child_ids = kb.decompose_triage_task(
            conn,
            root_id,
            root_assignee="orchestrator",
            children=[{"title": "implement child", "assignee": "worker"}],
            author="decomposer",
            auto_promote=False,
        )
        assert child_ids is not None and len(child_ids) == 1
        child = kb.get_task(conn, child_ids[0])

    assert child is not None
    expected_workspace = project_repo / ".worktrees" / child.id
    assert child.project_id == project_id
    assert child.workspace_kind == "worktree"
    assert child.workspace_path == str(expected_workspace)
    assert child.branch_name == f"{project.slug}/{child.id}-implement-child"

    resolved, branch = kb._resolve_worktree_workspace(child, board="default")
    common_dir = Path(
        subprocess.run(
            [
                "git",
                "-C",
                str(resolved),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    ).resolve()

    assert resolved == expected_workspace.resolve()
    assert branch == child.branch_name
    assert common_dir == (project_repo / ".git").resolve()
    assert common_dir != (board_repo / ".git").resolve()


def test_decompose_project_child_rejects_wrong_canonical_repo(
    kanban_home, tmp_path
):
    project_repo = _make_repo(tmp_path, "project-a")
    wrong_repo = _make_repo(tmp_path, "board-default-b")

    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Project A",
            primary_path=str(project_repo),
        )

    kb.write_board_metadata(
        "default",
        default_workdir=str(wrong_repo),
        project_id="",
    )
    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(
            conn,
            title="Project A root",
            triage=True,
            project_id=project_id,
            board="default",
        )
        wrong_root_path = wrong_repo / ".worktrees" / root_id
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(wrong_root_path), root_id),
        )
        conn.commit()

        before = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            "links": conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0],
            "comments": conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0],
        }

        with pytest.raises(
            ValueError,
            match="does not match authoritative project repository",
        ):
            kb.decompose_triage_task(
                conn,
                root_id,
                root_assignee="orchestrator",
                children=[{"title": "must not dispatch", "assignee": "worker"}],
                author="decomposer",
            )

        root = kb.get_task(conn, root_id)
        after = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            "links": conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0],
            "comments": conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0],
        }

    assert after == before
    assert root is not None
    assert root.status == "triage"
    assert root.project_id == project_id
    assert root.workspace_path == str(wrong_root_path)
    assert not (wrong_repo / ".worktrees").exists()


def test_decompose_project_child_rejects_counterfeit_canonical_git_repo(
    kanban_home, tmp_path
):
    project_repo = _make_repo(tmp_path, "project-a")
    wrong_repo = _make_repo(tmp_path, "repo-b")

    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Project A",
            primary_path=str(project_repo),
        )

    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(
            conn,
            title="Project A root",
            triage=True,
            project_id=project_id,
            board="default",
        )
        root = kb.get_task(conn, root_id)
        assert root is not None
        assert root.workspace_path is not None
        assert root.branch_name is not None
        canonical_path = Path(root.workspace_path)
        assert not canonical_path.exists()

        subprocess.run(
            [
                "git",
                "-C",
                str(wrong_repo),
                "worktree",
                "add",
                "-b",
                root.branch_name,
                str(canonical_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert kb._git_common_dir(canonical_path) == (wrong_repo / ".git").resolve()

        before = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            "links": conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0],
        }
        with pytest.raises(
            ValueError,
            match="effective Git repository does not match authoritative project",
        ):
            kb.decompose_triage_task(
                conn,
                root_id,
                root_assignee="orchestrator",
                children=[{"title": "must not dispatch", "assignee": "worker"}],
                author="decomposer",
            )
        after = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            "links": conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0],
        }
        root_after = kb.get_task(conn, root_id)

    assert after == before
    assert root_after is not None and root_after.status == "triage"


@pytest.mark.parametrize("git_entry_kind", ["symlink", "copy"])
def test_decompose_project_rejects_counterfeit_gitfile_registration_atomically(
    kanban_home, tmp_path, git_entry_kind
):
    project_repo = _make_repo(tmp_path, f"decompose-gitfile-{git_entry_kind}")
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name=f"Decompose Gitfile {git_entry_kind}",
            primary_path=str(project_repo),
        )

    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(
            conn,
            title=f"counterfeit root {git_entry_kind}",
            triage=True,
            project_id=project_id,
            board="default",
        )
        root = kb.get_task(conn, root_id)
        assert root is not None and root.workspace_path and root.branch_name
        registered = _add_worktree(
            project_repo,
            tmp_path / f"decompose-registered-{git_entry_kind}",
            root.branch_name,
        )
        source = Path(root.workspace_path)
        source.mkdir(parents=True)
        (source / "UNTRUSTED").write_text("counterfeit\n")
        if git_entry_kind == "symlink":
            (source / ".git").symlink_to(registered / ".git")
        else:
            (source / ".git").write_bytes((registered / ".git").read_bytes())

        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("tasks", "task_events", "task_links", "task_comments")
        }
        with pytest.raises(
            ValueError,
            match="effective Git repository does not match authoritative project",
        ):
            kb.decompose_triage_task(
                conn,
                root_id,
                root_assignee="orchestrator",
                children=[{"title": "must not write", "assignee": "worker"}],
                author="decomposer",
            )
        after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
        root_after = kb.get_task(conn, root_id)

    assert after == before
    assert root_after is not None and root_after.status == "triage"


def test_decompose_project_child_rejects_actual_wrong_git_branch(
    kanban_home, tmp_path
):
    project_repo = _make_repo(tmp_path, "project-a-actual-branch")
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Project A Branch",
            primary_path=str(project_repo),
        )

    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(
            conn,
            title="Project A branch root",
            triage=True,
            project_id=project_id,
            board="default",
        )
        root = kb.get_task(conn, root_id)
        assert root is not None
        assert root.workspace_path is not None
        assert root.branch_name is not None
        canonical_path = Path(root.workspace_path)
        subprocess.run(
            [
                "git",
                "-C",
                str(project_repo),
                "worktree",
                "add",
                "-b",
                "counterfeit-actual-branch",
                str(canonical_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert kb._git_common_dir(canonical_path) == (project_repo / ".git").resolve()
        assert kb._git_current_branch(canonical_path) == "counterfeit-actual-branch"
        assert root.branch_name != "counterfeit-actual-branch"

        before = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            "links": conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0],
            "comments": conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0],
        }
        with pytest.raises(
            ValueError,
            match="actual Git branch does not match authoritative project identity",
        ):
            kb.decompose_triage_task(
                conn,
                root_id,
                root_assignee="orchestrator",
                children=[{"title": "must not dispatch", "assignee": "worker"}],
                author="decomposer",
            )
        after = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            "links": conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0],
            "comments": conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0],
        }
        root_after = kb.get_task(conn, root_id)

    assert after == before
    assert root_after is not None and root_after.status == "triage"


def test_decompose_project_rejects_symlinked_root_checkout(kanban_home, tmp_path):
    project_repo = _make_repo(tmp_path, "project-root-symlink")
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Project Root Symlink",
            primary_path=str(project_repo),
        )

    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(
            conn,
            title="Symlinked project root",
            triage=True,
            project_id=project_id,
            board="default",
        )
        root = kb.get_task(conn, root_id)
        assert root is not None
        assert root.workspace_path is not None
        assert root.branch_name is not None
        target = Path(root.workspace_path)
        actual_checkout = tmp_path / "actual-root-checkout"
        _add_worktree(project_repo, actual_checkout, root.branch_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(actual_checkout, target_is_directory=True)
        before = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            "links": conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0],
            "comments": conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0],
        }

        with pytest.raises(ValueError, match="canonical project workspace route"):
            kb.decompose_triage_task(
                conn,
                root_id,
                root_assignee="orchestrator",
                children=[{"title": "must not materialize", "assignee": "worker"}],
                author="decomposer",
            )

        after = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            "links": conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0],
            "comments": conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0],
        }
        root_after = kb.get_task(conn, root_id)

    assert after == before
    assert root_after is not None and root_after.status == "triage"


def test_decompose_project_rejects_symlinked_worktree_parent(kanban_home, tmp_path):
    project_repo = _make_repo(tmp_path, "project-root-parent-symlink")
    redirected_root = project_repo / "alternate-worktrees"
    redirected_root.mkdir()
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Project Root Parent Symlink",
            primary_path=str(project_repo),
        )

    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(
            conn,
            title="Symlinked worktree parent",
            triage=True,
            project_id=project_id,
            board="default",
        )
        root = kb.get_task(conn, root_id)
        assert root is not None and root.workspace_path is not None
        Path(root.workspace_path).parent.symlink_to(
            redirected_root,
            target_is_directory=True,
        )

        with pytest.raises(ValueError, match="canonical project workspace route"):
            kb.decompose_triage_task(
                conn,
                root_id,
                root_assignee="orchestrator",
                children=[{"title": "must not materialize", "assignee": "worker"}],
                author="decomposer",
            )

        root_after = kb.get_task(conn, root_id)

    assert root_after is not None and root_after.status == "triage"
    assert not any(redirected_root.iterdir())


def test_decompose_project_child_rejects_non_worktree_project_route(
    kanban_home, tmp_path
):
    project_repo = _make_repo(tmp_path, "project-non-worktree")
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Project Non Worktree",
            primary_path=str(project_repo),
        )

    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(conn, title="Project malformed kind", triage=True)
        conn.execute(
            """UPDATE tasks
               SET project_id = ?, workspace_kind = 'scratch',
                   workspace_path = NULL, branch_name = NULL
               WHERE id = ?""",
            (project_id, root_id),
        )
        conn.commit()
        before = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            "links": conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0],
            "comments": conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0],
        }
        with pytest.raises(
            ValueError,
            match="must use an authoritative project worktree",
        ):
            kb.decompose_triage_task(
                conn,
                root_id,
                root_assignee="orchestrator",
                children=[{"title": "must not dispatch", "assignee": "worker"}],
                author="decomposer",
            )
        after = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            "links": conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0],
            "comments": conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0],
        }
        root_after = kb.get_task(conn, root_id)

    assert after == before
    assert root_after is not None and root_after.status == "triage"
    assert not (project_repo / ".worktrees").exists()


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("wrong_leaf", "no canonical project workspace route"),
        ("wrong_branch", "branch does not match authoritative project identity"),
        ("missing_project", "no authoritative project repository"),
        ("project_slug_id", "no authoritative project repository"),
    ],
)
def test_decompose_project_child_rejects_corrupt_project_identity(
    kanban_home, tmp_path, corruption, message
):
    project_repo = _make_repo(tmp_path, "project-a")
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Project A",
            primary_path=str(project_repo),
        )
        project = pdb.get_project(project_conn, project_id)
    assert project is not None

    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(
            conn,
            title="Project A root",
            triage=True,
            project_id=project_id,
            board="default",
        )
        if corruption == "wrong_leaf":
            conn.execute(
                "UPDATE tasks SET workspace_path = ? WHERE id = ?",
                (str(project_repo / ".worktrees" / "different-task"), root_id),
            )
            conn.commit()
        elif corruption == "wrong_branch":
            conn.execute(
                "UPDATE tasks SET branch_name = ? WHERE id = ?",
                (f"wrong-project/{root_id}-root", root_id),
            )
            conn.commit()
        elif corruption == "missing_project":
            with pdb.connect_closing() as project_conn:
                project_conn.execute(
                    "DELETE FROM project_folders WHERE project_id = ?",
                    (project_id,),
                )
                project_conn.execute(
                    "DELETE FROM projects WHERE id = ?",
                    (project_id,),
                )
                project_conn.commit()
        else:
            conn.execute(
                "UPDATE tasks SET project_id = ? WHERE id = ?",
                (project.slug, root_id),
            )
            conn.commit()

        before = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            "links": conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0],
        }
        with pytest.raises(ValueError, match=message):
            kb.decompose_triage_task(
                conn,
                root_id,
                root_assignee="orchestrator",
                children=[{"title": "must not dispatch", "assignee": "worker"}],
                author="decomposer",
            )
        root = kb.get_task(conn, root_id)
        after = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            "links": conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0],
        }

    assert after == before
    assert root is not None and root.status == "triage"


@pytest.mark.parametrize("override_kind", ["worktree_path", "directory"])
def test_decompose_project_child_rejects_workspace_override(
    kanban_home, tmp_path, override_kind
):
    project_repo = _make_repo(tmp_path, "project-a")
    wrong_repo = _make_repo(tmp_path, "wrong-repo")

    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Project A",
            primary_path=str(project_repo),
        )

    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(
            conn,
            title="Project A root",
            triage=True,
            project_id=project_id,
            board="default",
        )
        child = {"title": "must not dispatch", "assignee": "worker"}
        if override_kind == "worktree_path":
            child["workspace_path"] = str(wrong_repo)
        else:
            child["workspace_kind"] = "dir"
            child["workspace_path"] = str(wrong_repo)

        before = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            "links": conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0],
        }
        with pytest.raises(
            ValueError,
            match="cannot override authoritative project workspace",
        ):
            kb.decompose_triage_task(
                conn,
                root_id,
                root_assignee="orchestrator",
                children=[child],
                author="decomposer",
            )
        root = kb.get_task(conn, root_id)
        after = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            "links": conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0],
        }

    assert after == before
    assert root is not None and root.status == "triage"
    assert not (wrong_repo / ".worktrees").exists()


def test_decompose_project_child_without_canonical_route_fails_closed(
    kanban_home, tmp_path
):
    board_repo = _make_repo(tmp_path, "board-default")
    kb.write_board_metadata("default", default_workdir=str(board_repo))

    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(conn, title="legacy root", triage=True)
        conn.execute(
            "UPDATE tasks SET project_id = ?, workspace_kind = 'worktree', "
            "workspace_path = NULL, branch_name = ? WHERE id = ?",
            ("p_legacy", f"legacy/{root_id}-root", root_id),
        )
        conn.commit()

        with pytest.raises(ValueError, match="no canonical project workspace route"):
            kb.decompose_triage_task(
                conn,
                root_id,
                root_assignee="orchestrator",
                children=[{"title": "must not dispatch", "assignee": "worker"}],
                author="decomposer",
            )

        root = kb.get_task(conn, root_id)
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    assert root is not None and root.status == "triage"
    assert task_count == 1
    assert not (board_repo / ".worktrees").exists()


def test_decompose_revalidates_project_route_inside_write_transaction(
    kanban_home, tmp_path, monkeypatch
):
    project_repo = _make_repo(tmp_path, "project-a")
    wrong_repo = _make_repo(tmp_path, "wrong-repo")
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Project A",
            primary_path=str(project_repo),
        )

    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(
            conn,
            title="Project A root",
            triage=True,
            project_id=project_id,
            board="default",
        )
        wrong_root_path = wrong_repo / ".worktrees" / root_id
        before = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            "links": conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0],
        }
        real_write_txn = kb.write_txn
        injected = False

        @contextmanager
        def inject_route_edit(db_conn):
            nonlocal injected
            if not injected:
                db_conn.execute(
                    "UPDATE tasks SET workspace_path = ? WHERE id = ?",
                    (str(wrong_root_path), root_id),
                )
                db_conn.commit()
                injected = True
            with real_write_txn(db_conn):
                yield

        monkeypatch.setattr(kb, "write_txn", inject_route_edit)
        with pytest.raises(ValueError, match="routing changed during decomposition"):
            kb.decompose_triage_task(
                conn,
                root_id,
                root_assignee="orchestrator",
                children=[{"title": "must not dispatch", "assignee": "worker"}],
                author="decomposer",
            )

        root = kb.get_task(conn, root_id)
        after = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            "links": conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0],
        }

    assert injected is True
    assert after == before
    assert root is not None and root.status == "triage"
    assert root.workspace_path == str(wrong_root_path)
    assert not (wrong_repo / ".worktrees").exists()


@pytest.mark.parametrize("replacement_kind", ["non_git", "new_git"])
def test_decompose_project_revalidates_git_identity_after_kanban_lock(
    kanban_home, tmp_path, monkeypatch, replacement_kind
):
    project_repo = _make_repo(tmp_path, f"project-git-race-{replacement_kind}")
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name=f"Project Git Race {replacement_kind}",
            primary_path=str(project_repo),
        )

    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(
            conn,
            title="Project Git authority race root",
            triage=True,
            project_id=project_id,
            board="default",
        )
        before = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            "links": conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0],
        }
        real_write_txn = kb.write_txn
        injected = False

        @contextmanager
        def replace_git_after_project_authority(db_conn):
            nonlocal injected
            if not injected:
                project_repo.rename(tmp_path / f"former-decompose-{replacement_kind}")
                if replacement_kind == "new_git":
                    _make_repo(tmp_path, project_repo.name)
                else:
                    project_repo.mkdir()
                    (project_repo / "ordinary.txt").write_text(
                        "not git\n", encoding="utf-8"
                    )
                injected = True
            with real_write_txn(db_conn):
                yield

        monkeypatch.setattr(kb, "write_txn", replace_git_after_project_authority)
        with pytest.raises(ValueError, match="routing changed during decomposition"):
            kb.decompose_triage_task(
                conn,
                root_id,
                root_assignee="orchestrator",
                children=[{"title": "must not dispatch", "assignee": "worker"}],
                author="decomposer",
            )
        after = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            "links": conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0],
        }
        root_after = kb.get_task(conn, root_id)

    assert injected is True
    assert after == before
    assert root_after is not None and root_after.status == "triage"
    assert not (project_repo / ".worktrees").exists()


def test_decompose_project_revalidates_actual_branch_inside_transaction(
    kanban_home, tmp_path, monkeypatch
):
    project_repo = _make_repo(tmp_path, "project-branch-race")
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Project Branch Race",
            primary_path=str(project_repo),
        )

    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(
            conn,
            title="Project branch race root",
            triage=True,
            project_id=project_id,
            board="default",
        )
        root = kb.get_task(conn, root_id)
        assert root is not None
        assert root.workspace_path is not None
        assert root.branch_name is not None
        root_path = _add_worktree(
            project_repo,
            Path(root.workspace_path),
            root.branch_name,
        )
        before = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            "links": conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0],
            "comments": conn.execute(
                "SELECT COUNT(*) FROM task_comments"
            ).fetchone()[0],
        }
        real_write_txn = kb.write_txn
        injected = False

        @contextmanager
        def inject_branch_switch(db_conn):
            nonlocal injected
            if not injected:
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(root_path),
                        "checkout",
                        "-b",
                        "counterfeit-race-branch",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                injected = True
            with real_write_txn(db_conn):
                yield

        monkeypatch.setattr(kb, "write_txn", inject_branch_switch)
        with pytest.raises(ValueError, match="routing changed during decomposition"):
            kb.decompose_triage_task(
                conn,
                root_id,
                root_assignee="orchestrator",
                children=[{"title": "must not dispatch", "assignee": "worker"}],
                author="decomposer",
            )
        after = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            "links": conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0],
            "comments": conn.execute(
                "SELECT COUNT(*) FROM task_comments"
            ).fetchone()[0],
        }
        root_after = kb.get_task(conn, root_id)

    assert injected is True
    assert after == before
    assert root_after is not None and root_after.status == "triage"


def test_decompose_project_rejects_path_appearance_inside_transaction(
    kanban_home, tmp_path, monkeypatch
):
    project_repo = _make_repo(tmp_path, "project-path-race")
    wrong_repo = _make_repo(tmp_path, "wrong-path-race")
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Project Path Race",
            primary_path=str(project_repo),
        )

    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(
            conn,
            title="Project path race root",
            triage=True,
            project_id=project_id,
            board="default",
        )
        root = kb.get_task(conn, root_id)
        assert root is not None
        assert root.workspace_path is not None
        assert root.branch_name is not None
        root_path = Path(root.workspace_path)
        root_branch = root.branch_name
        assert not root_path.exists()
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        real_write_txn = kb.write_txn
        injected = False

        @contextmanager
        def inject_path_appearance(db_conn):
            nonlocal injected
            if not injected:
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(wrong_repo),
                        "worktree",
                        "add",
                        "-b",
                        root_branch,
                        str(root_path),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                injected = True
            with real_write_txn(db_conn):
                yield

        monkeypatch.setattr(kb, "write_txn", inject_path_appearance)
        with pytest.raises(ValueError, match="routing changed during decomposition"):
            kb.decompose_triage_task(
                conn,
                root_id,
                root_assignee="orchestrator",
                children=[{"title": "must not dispatch", "assignee": "worker"}],
                author="decomposer",
            )
        after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        root_after = kb.get_task(conn, root_id)

    assert injected is True
    assert after == before
    assert root_after is not None and root_after.status == "triage"


def test_decompose_project_rejects_parent_symlink_inside_transaction(
    kanban_home, tmp_path, monkeypatch
):
    project_repo = _make_repo(tmp_path, "project-parent-symlink-race")
    redirected_root = project_repo / "alternate-worktrees"
    redirected_root.mkdir()
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Project Parent Symlink Race",
            primary_path=str(project_repo),
        )

    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(
            conn,
            title="Project parent symlink race root",
            triage=True,
            project_id=project_id,
            board="default",
        )
        root = kb.get_task(conn, root_id)
        assert root is not None and root.workspace_path is not None
        worktrees_root = Path(root.workspace_path).parent
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        real_write_txn = kb.write_txn
        injected = False

        @contextmanager
        def inject_parent_symlink(db_conn):
            nonlocal injected
            if not injected:
                worktrees_root.symlink_to(redirected_root, target_is_directory=True)
                injected = True
            with real_write_txn(db_conn):
                yield

        monkeypatch.setattr(kb, "write_txn", inject_parent_symlink)
        with pytest.raises(ValueError, match="routing changed during decomposition"):
            kb.decompose_triage_task(
                conn,
                root_id,
                root_assignee="orchestrator",
                children=[{"title": "must not dispatch", "assignee": "worker"}],
                author="decomposer",
            )
        after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        root_after = kb.get_task(conn, root_id)

    assert injected is True
    assert after == before
    assert root_after is not None and root_after.status == "triage"


def test_decompose_project_rejects_dangling_target_symlink_inside_transaction(
    kanban_home, tmp_path, monkeypatch
):
    project_repo = _make_repo(tmp_path, "project-target-symlink-race")
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Project Target Symlink Race",
            primary_path=str(project_repo),
        )

    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(
            conn,
            title="Project target symlink race root",
            triage=True,
            project_id=project_id,
            board="default",
        )
        root = kb.get_task(conn, root_id)
        assert root is not None and root.workspace_path is not None
        root_path = Path(root.workspace_path)
        root_path.parent.mkdir(parents=True)
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        real_write_txn = kb.write_txn
        injected = False

        @contextmanager
        def inject_target_symlink(db_conn):
            nonlocal injected
            if not injected:
                root_path.symlink_to(
                    tmp_path / "nonexistent-checkout",
                    target_is_directory=True,
                )
                injected = True
            with real_write_txn(db_conn):
                yield

        monkeypatch.setattr(kb, "write_txn", inject_target_symlink)
        with pytest.raises(ValueError, match="routing changed during decomposition"):
            kb.decompose_triage_task(
                conn,
                root_id,
                root_assignee="orchestrator",
                children=[{"title": "must not dispatch", "assignee": "worker"}],
                author="decomposer",
            )
        after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        root_after = kb.get_task(conn, root_id)

    assert injected is True
    assert after == before
    assert root_after is not None and root_after.status == "triage"


def test_decompose_project_rejects_primary_path_change_inside_transaction(
    kanban_home, tmp_path, monkeypatch
):
    project_repo = _make_repo(tmp_path, "project-primary-race")
    wrong_repo = _make_repo(tmp_path, "wrong-primary-race")
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Project Primary Race",
            primary_path=str(project_repo),
        )

    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(
            conn,
            title="Project primary race root",
            triage=True,
            project_id=project_id,
            board="default",
        )
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        real_locked_authority = kb._locked_project_authority
        injected = False

        @contextmanager
        def inject_primary_path_change(*args, **kwargs):
            nonlocal injected
            if not injected:
                with pdb.connect_closing() as project_conn:
                    project_conn.execute(
                        "UPDATE projects SET primary_path = ? WHERE id = ?",
                        (str(wrong_repo), project_id),
                    )
                    project_conn.commit()
                injected = True
            with real_locked_authority(*args, **kwargs) as authority:
                yield authority

        monkeypatch.setattr(
            kb, "_locked_project_authority", inject_primary_path_change
        )
        with pytest.raises(ValueError, match="routing changed during decomposition"):
            kb.decompose_triage_task(
                conn,
                root_id,
                root_assignee="orchestrator",
                children=[{"title": "must not dispatch", "assignee": "worker"}],
                author="decomposer",
            )
        after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        root_after = kb.get_task(conn, root_id)

    assert injected is True
    assert after == before
    assert root_after is not None and root_after.status == "triage"


def test_decompose_project_rejects_primary_symlink_inside_transaction(
    kanban_home, tmp_path, monkeypatch
):
    project_repo = _make_repo(tmp_path, "project-primary-symlink-race")
    alias_repo = tmp_path / "project-primary-symlink-alias"
    alias_repo.symlink_to(project_repo, target_is_directory=True)
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Project Primary Symlink Race",
            primary_path=str(project_repo),
        )

    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(
            conn,
            title="Project primary symlink race root",
            triage=True,
            project_id=project_id,
            board="default",
        )
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("tasks", "task_events", "task_links", "task_comments")
        }
        real_locked_authority = kb._locked_project_authority
        injected = False

        @contextmanager
        def inject_primary_symlink(*args, **kwargs):
            nonlocal injected
            if not injected:
                with pdb.connect_closing() as project_conn:
                    project_conn.execute(
                        "UPDATE projects SET primary_path = ? WHERE id = ?",
                        (str(alias_repo), project_id),
                    )
                    project_conn.commit()
                injected = True
            with real_locked_authority(*args, **kwargs) as authority:
                yield authority

        monkeypatch.setattr(kb, "_locked_project_authority", inject_primary_symlink)
        with pytest.raises(ValueError, match="routing changed during decomposition"):
            kb.decompose_triage_task(
                conn,
                root_id,
                root_assignee="orchestrator",
                children=[{"title": "must not write", "assignee": "worker"}],
                author="decomposer",
            )
        after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
        root_after = kb.get_task(conn, root_id)

    assert injected is True
    assert after == before
    assert root_after is not None and root_after.status == "triage"


def test_decompose_project_rejects_gitfile_symlink_inside_transaction(
    kanban_home, tmp_path, monkeypatch
):
    project_repo = _make_repo(tmp_path, "project-gitfile-symlink-race")
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Project Gitfile Symlink Race",
            primary_path=str(project_repo),
        )

    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(
            conn,
            title="Project gitfile symlink race root",
            triage=True,
            project_id=project_id,
            board="default",
        )
        root = kb.get_task(conn, root_id)
        assert root is not None and root.workspace_path and root.branch_name
        source = Path(root.workspace_path)
        _add_worktree(project_repo, source, root.branch_name)
        gitfile = source / ".git"
        backup = source / ".git.original"
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("tasks", "task_events", "task_links", "task_comments")
        }
        real_write_txn = kb.write_txn
        injected = False

        @contextmanager
        def inject_gitfile_symlink(db_conn):
            nonlocal injected
            if not injected:
                gitfile.rename(backup)
                gitfile.symlink_to(backup)
                injected = True
            with real_write_txn(db_conn):
                yield

        monkeypatch.setattr(kb, "write_txn", inject_gitfile_symlink)
        try:
            with pytest.raises(
                ValueError, match="routing changed during decomposition"
            ):
                kb.decompose_triage_task(
                    conn,
                    root_id,
                    root_assignee="orchestrator",
                    children=[{"title": "must not write", "assignee": "worker"}],
                    author="decomposer",
                )
        finally:
            if gitfile.is_symlink():
                gitfile.unlink()
            if backup.exists():
                backup.rename(gitfile)
        after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
        root_after = kb.get_task(conn, root_id)

    assert injected is True
    assert after == before
    assert root_after is not None and root_after.status == "triage"


def test_project_child_resolver_rejects_symlinked_authoritative_repository(
    kanban_home, tmp_path
):
    project_repo = _make_repo(tmp_path, "project-dispatch-authority")
    alias_repo = tmp_path / "project-dispatch-authority-alias"
    alias_repo.symlink_to(project_repo, target_is_directory=True)
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Project Dispatch Authority",
            primary_path=str(project_repo),
        )
    with kb.connect_closing(board="default") as conn:
        task_id = kb.create_task(
            conn,
            title="dispatch authority alias",
            triage=True,
            project_id=project_id,
            board="default",
        )
        task = kb.get_task(conn, task_id)
    assert task is not None and task.workspace_path is not None
    with pdb.connect_closing() as project_conn:
        project_conn.execute(
            "UPDATE projects SET primary_path = ? WHERE id = ?",
            (str(alias_repo), project_id),
        )
        project_conn.commit()

    with pytest.raises(ValueError, match="symlinked project repository"):
        kb._resolve_worktree_workspace(task, board="default")
    assert not Path(task.workspace_path).exists()


def test_project_child_resolver_rejects_occupied_canonical_wrong_branch(
    kanban_home, tmp_path
):
    project_repo = _make_repo(tmp_path, "project-child-occupied")
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Project Child Occupied",
            primary_path=str(project_repo),
        )

    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(
            conn,
            title="Project child occupied root",
            triage=True,
            project_id=project_id,
            board="default",
        )
        child_ids = kb.decompose_triage_task(
            conn,
            root_id,
            root_assignee="orchestrator",
            children=[{"title": "child exact target", "assignee": "worker"}],
            author="decomposer",
            auto_promote=False,
        )
        assert child_ids is not None and len(child_ids) == 1
        child = kb.get_task(conn, child_ids[0])
        assert child is not None
        assert child.workspace_path is not None
        assert child.branch_name is not None

    target = Path(child.workspace_path)
    subprocess.run(
        [
            "git",
            "-C",
            str(project_repo),
            "worktree",
            "add",
            "-b",
            "wrong-child-branch",
            str(target),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert kb._git_current_branch(target) == "wrong-child-branch"
    with pytest.raises(ValueError, match="occupied by branch"):
        kb._resolve_worktree_workspace(child, board="default")


def test_project_subdirectory_child_never_falls_back_from_wrong_branch(
    kanban_home, tmp_path
):
    repository = _make_repo(tmp_path, "project-subdirectory")
    project_repo = repository / "packages" / "project-a"
    project_repo.mkdir(parents=True)
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Project Subdirectory",
            primary_path=str(project_repo),
        )

    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(
            conn,
            title="Project subdirectory root",
            triage=True,
            project_id=project_id,
            board="default",
        )
        child_ids = kb.decompose_triage_task(
            conn,
            root_id,
            root_assignee="orchestrator",
            children=[{"title": "subdirectory child", "assignee": "worker"}],
            author="decomposer",
            auto_promote=False,
        )
        assert child_ids is not None and len(child_ids) == 1
        child = kb.get_task(conn, child_ids[0])
        assert child is not None
        assert child.workspace_path is not None
        assert child.branch_name is not None

    target = Path(child.workspace_path)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "worktree",
            "add",
            "-b",
            "wrong-subdirectory-branch",
            str(target),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    unsafe_fallback = repository / ".worktrees" / child.id
    with pytest.raises(ValueError, match="occupied by branch"):
        kb._resolve_worktree_workspace(child, board="default")
    assert not unsafe_fallback.exists()


def test_project_child_rejects_symlinked_worktree_parent(kanban_home, tmp_path):
    project_repo = _make_repo(tmp_path, "project-parent-symlink")
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Project Parent Symlink",
            primary_path=str(project_repo),
        )

    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(
            conn,
            title="Project parent symlink root",
            triage=True,
            project_id=project_id,
            board="default",
        )
        child_ids = kb.decompose_triage_task(
            conn,
            root_id,
            root_assignee="orchestrator",
            children=[{"title": "symlink parent child", "assignee": "worker"}],
            author="decomposer",
            auto_promote=False,
        )
        assert child_ids is not None and len(child_ids) == 1
        child = kb.get_task(conn, child_ids[0])
        assert child is not None
        assert child.workspace_path is not None

    worktrees_root = project_repo / ".worktrees"
    redirected_root = project_repo / "alternate-worktrees"
    redirected_root.mkdir()
    worktrees_root.symlink_to(redirected_root, target_is_directory=True)
    redirected_target = redirected_root / child.id

    with pytest.raises(ValueError, match="canonical project worktree path"):
        kb._resolve_worktree_workspace(child, board="default")
    assert not redirected_target.exists()


@pytest.mark.parametrize("git_entry_kind", ["symlink", "copy"])
def test_project_child_resolver_rejects_counterfeit_gitfile_registration(
    kanban_home, tmp_path, git_entry_kind
):
    project_repo = _make_repo(tmp_path, f"project-counterfeit-{git_entry_kind}")
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name=f"Counterfeit {git_entry_kind}",
            primary_path=str(project_repo),
        )
    with kb.connect_closing(board="default") as conn:
        task_id = kb.create_task(
            conn,
            title=f"counterfeit {git_entry_kind}",
            project_id=project_id,
            board="default",
        )
        task = kb.get_task(conn, task_id)
    assert task is not None and task.workspace_path and task.branch_name

    registered = _add_worktree(
        project_repo,
        tmp_path / f"registered-project-{git_entry_kind}",
        task.branch_name,
    )
    target = Path(task.workspace_path)
    target.mkdir(parents=True)
    (target / "UNTRUSTED").write_text("counterfeit\n")
    if git_entry_kind == "symlink":
        (target / ".git").symlink_to(registered / ".git")
    else:
        (target / ".git").write_bytes((registered / ".git").read_bytes())

    with pytest.raises(ValueError, match="occupied"):
        kb._resolve_worktree_workspace(task, board="default")


def test_project_child_rejects_symlinked_target_checkout(kanban_home, tmp_path):
    project_repo = _make_repo(tmp_path, "project-target-symlink")
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Project Target Symlink",
            primary_path=str(project_repo),
        )

    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(
            conn,
            title="Project target symlink root",
            triage=True,
            project_id=project_id,
            board="default",
        )
        child_ids = kb.decompose_triage_task(
            conn,
            root_id,
            root_assignee="orchestrator",
            children=[{"title": "symlink target child", "assignee": "worker"}],
            author="decomposer",
            auto_promote=False,
        )
        assert child_ids is not None and len(child_ids) == 1
        child = kb.get_task(conn, child_ids[0])
        assert child is not None
        assert child.workspace_path is not None
        assert child.branch_name is not None

    actual_checkout = tmp_path / "actual-child-checkout"
    _add_worktree(project_repo, actual_checkout, child.branch_name)
    target = Path(child.workspace_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(actual_checkout, target_is_directory=True)

    with pytest.raises(ValueError, match="canonical project worktree path"):
        kb._resolve_worktree_workspace(child, board="default")
    assert actual_checkout.exists()


def test_project_child_resolver_rejects_wrong_repo_with_expected_branch(
    kanban_home, tmp_path
):
    project_repo = _make_repo(tmp_path, "project-child-a")
    wrong_repo = _make_repo(tmp_path, "project-child-b")
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Project Child A",
            primary_path=str(project_repo),
        )

    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(
            conn,
            title="Project child A root",
            triage=True,
            project_id=project_id,
            board="default",
        )
        child_ids = kb.decompose_triage_task(
            conn,
            root_id,
            root_assignee="orchestrator",
            children=[{"title": "child wrong repo target", "assignee": "worker"}],
            author="decomposer",
            auto_promote=False,
        )
        assert child_ids is not None and len(child_ids) == 1
        child = kb.get_task(conn, child_ids[0])
        assert child is not None
        assert child.workspace_path is not None
        assert child.branch_name is not None

    target = Path(child.workspace_path)
    subprocess.run(
        [
            "git",
            "-C",
            str(wrong_repo),
            "worktree",
            "add",
            "-b",
            child.branch_name,
            str(target),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert kb._git_common_dir(target) == (wrong_repo / ".git").resolve()
    with pytest.raises(
        ValueError,
        match="effective Git repository does not match authoritative project",
    ):
        kb._resolve_worktree_workspace(child, board="default")


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("wrong_path", "no canonical project worktree path"),
        ("wrong_branch", "branch does not match authoritative project identity"),
        ("project_slug_id", "no authoritative project route"),
        ("missing_project", "no authoritative project route"),
    ],
)
def test_project_child_resolver_rejects_corrupt_persisted_route(
    kanban_home, tmp_path, corruption, message
):
    project_repo = _make_repo(tmp_path, f"dispatch-{corruption}")
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name=f"Dispatch {corruption}",
            primary_path=str(project_repo),
        )
        project = pdb.get_project(project_conn, project_id)
    assert project is not None

    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(
            conn,
            title=f"dispatch {corruption} root",
            triage=True,
            project_id=project_id,
            board="default",
        )
        child_ids = kb.decompose_triage_task(
            conn,
            root_id,
            root_assignee="orchestrator",
            children=[{"title": "child persisted route", "assignee": "worker"}],
            author="decomposer",
            auto_promote=False,
        )
        assert child_ids is not None and len(child_ids) == 1
        child_id = child_ids[0]
        original_child = kb.get_task(conn, child_id)
        assert original_child is not None
        assert original_child.workspace_path is not None
        canonical_target = Path(original_child.workspace_path)

        if corruption == "wrong_path":
            conn.execute(
                "UPDATE tasks SET workspace_path = ? WHERE id = ?",
                (str(project_repo / ".worktrees" / "different-task"), child_id),
            )
        elif corruption == "wrong_branch":
            conn.execute(
                "UPDATE tasks SET branch_name = ? WHERE id = ?",
                (f"wrong-project/{child_id}-child", child_id),
            )
        elif corruption == "project_slug_id":
            conn.execute(
                "UPDATE tasks SET project_id = ? WHERE id = ?",
                (project.slug, child_id),
            )
        conn.commit()
        child = kb.get_task(conn, child_id)
        assert child is not None

    if corruption == "missing_project":
        with pdb.connect_closing() as project_conn:
            project_conn.execute(
                "DELETE FROM project_folders WHERE project_id = ?",
                (project_id,),
            )
            project_conn.execute(
                "DELETE FROM projects WHERE id = ?",
                (project_id,),
            )
            project_conn.commit()

    with pytest.raises(ValueError, match=message):
        kb._resolve_worktree_workspace(child, board="default")
    assert not canonical_target.exists()


def test_project_task_resolver_never_falls_back_to_board_default(
    kanban_home, tmp_path
):
    project_repo = _make_repo(tmp_path, "project-no-path")
    board_repo = _make_repo(tmp_path, "board-no-path")
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Project No Path",
            primary_path=str(project_repo),
        )
        project = pdb.get_project(project_conn, project_id)
    assert project is not None
    kb.write_board_metadata("default", default_workdir=str(board_repo), project_id="")

    with kb.connect_closing(board="default") as conn:
        task_id = kb.create_task(conn, title="legacy project no path", triage=True)
        conn.execute(
            """UPDATE tasks
               SET project_id = ?, workspace_kind = 'worktree',
                   workspace_path = NULL, branch_name = ?
               WHERE id = ?""",
            (project_id, f"{project.slug}/{task_id}-legacy", task_id),
        )
        conn.commit()
        task = kb.get_task(conn, task_id)
        assert task is not None

    with pytest.raises(ValueError, match="no canonical project worktree path"):
        kb._resolve_worktree_workspace(task, board="default")
    assert not (board_repo / ".worktrees").exists()


def test_project_task_general_resolver_rejects_corrupt_scratch_route(
    kanban_home, tmp_path
):
    project_repo = _make_repo(tmp_path, "project-corrupt-scratch")
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Project Corrupt Scratch",
            primary_path=str(project_repo),
        )

    with kb.connect_closing(board="default") as conn:
        task_id = kb.create_task(
            conn,
            title="corrupt scratch route",
            triage=True,
            project_id=project_id,
            board="default",
        )
        conn.execute(
            """UPDATE tasks
               SET workspace_kind = 'scratch', workspace_path = NULL,
                   branch_name = NULL
               WHERE id = ?""",
            (task_id,),
        )
        conn.commit()
        task = kb.get_task(conn, task_id)
        assert task is not None

    with pytest.raises(
        ValueError,
        match="must use an authoritative project worktree",
    ):
        kb.resolve_workspace(task, board="default")
    assert not (kb.workspaces_root(board="default") / task_id).exists()


def test_path_symlink_component_lookup_error_fails_closed(
    tmp_path, monkeypatch
):
    protected = tmp_path / "protected"
    target = protected / ".worktrees" / "task-id"
    real_lstat = Path.lstat

    def deny_one_component(path):
        if path == protected:
            raise PermissionError("ancestry unreadable")
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", deny_one_component)
    assert kb._path_has_symlink_component(target) is True


@pytest.mark.parametrize("git_entry_kind", ["symlink", "copy"])
def test_ensure_git_worktree_rejects_counterfeit_gitfile_registration(
    tmp_path, git_entry_kind
):
    repo = _make_repo(tmp_path, f"ensure-counterfeit-{git_entry_kind}")
    branch_name = f"expected-{git_entry_kind}"
    registered = _add_worktree(
        repo,
        tmp_path / f"registered-{git_entry_kind}",
        branch_name,
    )
    target = repo / ".worktrees" / f"counterfeit-{git_entry_kind}"
    target.mkdir(parents=True)
    payload = target / "UNTRUSTED"
    payload.write_text("counterfeit\n")
    if git_entry_kind == "symlink":
        (target / ".git").symlink_to(registered / ".git")
    else:
        (target / ".git").write_bytes((registered / ".git").read_bytes())

    with pytest.raises(RuntimeError, match="occupied"):
        kb._ensure_git_worktree(repo, target, branch_name)
    assert payload.read_text() == "counterfeit\n"


def test_ensure_git_worktree_rejects_registration_with_nonexact_toplevel(
    tmp_path, monkeypatch
):
    repo = _make_repo(tmp_path, "ensure-nonexact-toplevel")
    branch_name = "expected-nonexact-toplevel"
    target = _add_worktree(
        repo,
        tmp_path / "registered-nonexact-toplevel",
        branch_name,
    )
    real_toplevel = kb._git_toplevel

    def report_parent(path):
        if path == target:
            return repo.resolve(strict=False)
        return real_toplevel(path)

    monkeypatch.setattr(kb, "_git_toplevel", report_parent)
    with pytest.raises(RuntimeError, match="occupied"):
        kb._ensure_git_worktree(repo, target, branch_name)


def test_ensure_git_worktree_rejects_symlinked_parent(tmp_path):
    repo = _make_repo(tmp_path, "ensure-symlink-parent")
    redirected_root = repo / "alternate-worktrees"
    redirected_root.mkdir()
    worktrees_root = repo / ".worktrees"
    worktrees_root.symlink_to(redirected_root, target_is_directory=True)
    target = worktrees_root / "task-id"

    with pytest.raises(RuntimeError, match="symlink alias"):
        kb._ensure_git_worktree(repo, target, "wt/task-id")
    assert not (redirected_root / "task-id").exists()


def test_ensure_git_worktree_rejects_wrong_repository(tmp_path):
    repo = _make_repo(tmp_path, "ensure-right-repo")
    wrong_repo = _make_repo(tmp_path, "ensure-wrong-repo")
    target = _add_worktree(
        wrong_repo,
        tmp_path / "wrong-repository-target",
        "expected-branch",
    )

    with pytest.raises(RuntimeError, match="occupied by branch"):
        kb._ensure_git_worktree(repo, target, "expected-branch")


def test_ensure_git_worktree_rejects_wrong_branch(tmp_path):
    repo = _make_repo(tmp_path, "ensure-wrong-branch")
    target = _add_worktree(repo, tmp_path / "wrong-branch-target", "wrong-branch")

    with pytest.raises(RuntimeError, match="occupied by branch"):
        kb._ensure_git_worktree(repo, target, "expected-branch")


def test_ensure_git_worktree_rejects_primary_checkout(tmp_path):
    repo = _make_repo(tmp_path, "ensure-primary-checkout")

    with pytest.raises(RuntimeError, match="occupied by branch"):
        kb._ensure_git_worktree(repo, repo, "main")


def test_ensure_git_worktree_rejects_nested_worktree_directory(tmp_path):
    repo = _make_repo(tmp_path, "ensure-nested-directory")
    worktree = _add_worktree(repo, tmp_path / "linked-worktree", "expected-branch")
    nested = worktree / "nested"
    nested.mkdir()

    with pytest.raises(RuntimeError, match="occupied by branch"):
        kb._ensure_git_worktree(repo, nested, "expected-branch")


def test_create_task_rejects_missing_project_authority_atomically(
    kanban_home,
):
    with kb.connect_closing(board="default") as conn:
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("tasks", "task_events", "task_links", "task_comments")
        }
        with pytest.raises(ValueError, match="no authoritative project"):
            kb.create_task(
                conn,
                title="must remain project linked",
                project_id="p_missing_authority",
                tenant="tenant-a",
                board="default",
            )
        after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
    assert after == before


def test_create_task_rejects_symlinked_project_authority_atomically(
    kanban_home, tmp_path, monkeypatch
):
    real_repo = _make_repo(tmp_path, "create-real-authority")
    alias_repo = tmp_path / "create-authority-alias"
    alias_repo.symlink_to(real_repo, target_is_directory=True)
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Symlinked Create Authority",
            primary_path=str(alias_repo),
        )

    # Isolate the creation-preflight lexical guard from the later locked
    # authority recheck.  If the preflight guard is removed, normalize the
    # registry before the lock is acquired so the later guard cannot mask the
    # missing phase; the mutant must then persist and fail with DID NOT RAISE.
    real_locked_authority = kb._locked_project_authority

    @contextmanager
    def normalize_authority_before_lock(*args, **kwargs):
        with pdb.connect_closing() as project_conn:
            with pdb.write_txn(project_conn):
                project_conn.execute(
                    "UPDATE projects SET primary_path = ? WHERE id = ?",
                    (str(real_repo), project_id),
                )
        with real_locked_authority(*args, **kwargs) as current_project:
            yield current_project

    monkeypatch.setattr(
        kb,
        "_locked_project_authority",
        normalize_authority_before_lock,
    )
    with kb.connect_closing(board="default") as conn:
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("tasks", "task_events", "task_links", "task_comments")
        }
        with pytest.raises(ValueError, match="symlinked project repository"):
            kb.create_task(
                conn,
                title="must not persist alias authority",
                project_id=project_id,
                tenant="tenant-a",
                board="default",
            )
        after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
    assert after == before


def test_create_task_rejects_source_row_without_project_authority_atomically(
    kanban_home, tmp_path
):
    with kb.connect_closing(board="default") as conn:
        source_id = kb.create_task(conn, title="untrusted source", triage=True)
        fake_project_id = "p_untrusted_source"
        fake_repo = tmp_path / "untrusted-repo"
        conn.execute(
            """UPDATE tasks
               SET project_id = ?, workspace_kind = 'worktree',
                   workspace_path = ?, branch_name = ?
               WHERE id = ?""",
            (
                fake_project_id,
                str(fake_repo / ".worktrees" / source_id),
                f"fake-project/{source_id}-source",
                source_id,
            ),
        )
        conn.commit()
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("tasks", "task_events", "task_links", "task_comments")
        }
        with pytest.raises(ValueError, match="no authoritative project"):
            kb.create_task(
                conn,
                title="must not inherit self-attested route",
                project_id=fake_project_id,
                project_source_task_id=source_id,
                tenant="tenant-a",
                board="default",
            )
        after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
    assert after == before


def test_unlinked_worktree_never_reuses_matching_branch_nested_directory(
    kanban_home, tmp_path, monkeypatch
):
    repository = _make_repo(tmp_path, "unlinked-nested-toplevel-repo")
    outer = _add_worktree(
        repository,
        tmp_path / "unlinked-outer-worktree",
        "matching-branch",
    )
    nested = outer / "nested"
    nested.mkdir()
    with kb.connect_closing(board="default") as conn:
        task_id = kb.create_task(
            conn,
            title="unlinked nested non-toplevel",
            workspace_kind="worktree",
            workspace_path=str(nested),
            branch_name="matching-branch",
            board="default",
        )
        task = kb.get_task(conn, task_id)
    assert task is not None

    def require_safe_fallback(repo_root, target, branch_name):
        assert target.resolve(strict=False) != nested.resolve(strict=False)
        raise RuntimeError("safe fallback required")

    monkeypatch.setattr(kb, "_ensure_git_worktree", require_safe_fallback)
    with pytest.raises(RuntimeError, match="safe fallback required"):
        kb._resolve_worktree_workspace(task, board="default")


def test_project_child_rejects_matching_branch_nested_non_toplevel(
    kanban_home, tmp_path
):
    repository = _make_repo(tmp_path, "nested-toplevel-repo")
    outer = _add_worktree(
        repository,
        tmp_path / "outer-linked-worktree",
        "nested-project-primary",
    )
    project_repo = outer / "packages" / "project-a"
    project_repo.mkdir(parents=True)
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Nested Toplevel Project",
            primary_path=str(project_repo),
        )
    with kb.connect_closing(board="default") as conn:
        task_id = kb.create_task(
            conn,
            title="nested non-toplevel",
            project_id=project_id,
            triage=True,
            board="default",
        )
        task = kb.get_task(conn, task_id)
    assert task is not None and task.workspace_path and task.branch_name
    _git(outer, "checkout", "-b", task.branch_name)
    target = Path(task.workspace_path)
    target.mkdir(parents=True)
    assert kb._git_toplevel(target) != target.resolve(strict=False)

    with pytest.raises(ValueError, match="exact linked worktree checkout"):
        kb._resolve_worktree_workspace(task, board="default")


def test_project_child_rejects_symlinked_repository_ancestor(
    kanban_home, tmp_path
):
    project_repo = _make_repo(tmp_path, "dispatch-symlink-ancestor")
    alias = tmp_path / "dispatch-repo-alias"
    alias.symlink_to(project_repo, target_is_directory=True)
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Dispatch Symlink Ancestor",
            primary_path=str(project_repo),
        )
    with kb.connect_closing(board="default") as conn:
        task_id = kb.create_task(
            conn,
            title="dispatch alias",
            project_id=project_id,
            triage=True,
            board="default",
        )
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(alias / ".worktrees" / task_id), task_id),
        )
        conn.commit()
        task = kb.get_task(conn, task_id)
    assert task is not None

    with pytest.raises(ValueError, match="canonical project worktree path"):
        kb._resolve_worktree_workspace(task, board="default")
    assert not (project_repo / ".worktrees" / task_id).exists()


def test_decompose_project_rejects_symlinked_repository_ancestor_atomically(
    kanban_home, tmp_path
):
    project_repo = _make_repo(tmp_path, "decompose-symlink-ancestor")
    alias = tmp_path / "decompose-repo-alias"
    alias.symlink_to(project_repo, target_is_directory=True)
    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn,
            name="Decompose Symlink Ancestor",
            primary_path=str(project_repo),
        )
    with kb.connect_closing(board="default") as conn:
        root_id = kb.create_task(
            conn,
            title="decompose alias root",
            project_id=project_id,
            triage=True,
            board="default",
        )
        root = kb.get_task(conn, root_id)
        assert root is not None and root.workspace_path and root.branch_name
        _add_worktree(project_repo, Path(root.workspace_path), root.branch_name)
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(alias / ".worktrees" / root_id), root_id),
        )
        conn.commit()
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("tasks", "task_events", "task_links", "task_comments")
        }
        with pytest.raises(ValueError, match="canonical project workspace route"):
            kb.decompose_triage_task(
                conn,
                root_id,
                root_assignee="orchestrator",
                children=[{"title": "must not write", "assignee": "worker"}],
                author="decomposer",
                auto_promote=False,
            )
        after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
        root_after = kb.get_task(conn, root_id)
    assert after == before
    assert root_after is not None and root_after.status == "triage"


def test_resolve_worktree_falls_back_when_path_occupied(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)
    occupied = _add_worktree(repo, repo / ".worktrees" / "sibling", "wt/sibling")

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="second sibling",
            workspace_kind="worktree",
            workspace_path=str(occupied),  # inherited shared/stale path
        )
        task = kb.get_task(conn, tid)

    workspace, branch = kb._resolve_worktree_workspace(task)
    assert workspace == (repo / ".worktrees" / tid).resolve()
    assert branch == f"wt/{tid}"
    # The sibling's checkout is untouched, still on its own branch.
    assert (occupied / "README.md").exists()
    head = subprocess.run(
        ["git", "-C", str(occupied), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == "wt/sibling"




