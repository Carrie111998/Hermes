"""Kanban <-> Projects integration: project-linked tasks get a deterministic
worktree path + branch instead of the random ``wt/<task-id>`` fallback."""

from __future__ import annotations

import contextlib
import os
import subprocess

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb


@pytest.fixture
def kanban_conn(tmp_path):
    c = kb.connect(db_path=tmp_path / "kanban.db")
    try:
        yield c
    finally:
        c.close()


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _make_repo(repo):
    repo.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q", "--initial-branch=main", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(
        repo,
        "-c",
        "user.name=Hermes Test",
        "-c",
        "user.email=hermes@example.invalid",
        "commit",
        "-q",
        "-m",
        "fixture",
    )
    return repo


def _make_project(repo, name="Web App"):
    with pdb.connect_closing() as pc:
        pid = pdb.create_project(pc, name=name, folders=[str(repo)])
        project = pdb.get_project(pc, pid)
        assert project is not None
        return project


def _sqlite_atomic_state(conn, root_id):
    return {
        "tasks": [tuple(row) for row in conn.execute("SELECT * FROM tasks ORDER BY id")],
        "task_events": [
            tuple(row) for row in conn.execute("SELECT * FROM task_events ORDER BY id")
        ],
        "task_links": [
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM task_links ORDER BY parent_id, child_id"
            )
        ],
        "task_comments": [
            tuple(row)
            for row in conn.execute("SELECT * FROM task_comments ORDER BY id")
        ],
        "root": tuple(
            conn.execute(
                "SELECT status, workspace_kind, workspace_path, branch_name, "
                "project_id FROM tasks WHERE id = ?",
                (root_id,),
            ).fetchone()
        ),
    }


def _repo_effect_state(*repos):
    return {
        str(repo): {
            "status": _git(repo, "status", "--porcelain=v1", "--untracked-files=all"),
            "refs": _git(repo, "for-each-ref", "--format=%(refname) %(objectname)"),
            "worktrees": _git(repo, "worktree", "list", "--porcelain"),
            "worktree_root_exists": (repo / ".worktrees").exists(),
        }
        for repo in repos
    }


@pytest.mark.parametrize("explicit_companions", [False, True])
def test_project_linked_task_gets_deterministic_worktree_and_branch(
    kanban_conn, tmp_path, monkeypatch, explicit_companions
):
    mode = "explicit" if explicit_companions else "derived"
    project_repo = _make_repo(tmp_path / f"webapp-{mode}")
    proj = _make_project(project_repo)
    task_id = f"t_project_route_{mode}"
    monkeypatch.setattr(kb, "_new_task_id", lambda: task_id)
    expected_path = project_repo / ".worktrees" / task_id
    expected_branch = pdb.branch_name_for(proj, task_id, title="Add login")
    tid = kb.create_task(
        kanban_conn,
        title="Add login",
        project_id=proj.slug,
        workspace_kind="worktree" if explicit_companions else "scratch",
        workspace_path=str(expected_path) if explicit_companions else None,
        branch_name=expected_branch if explicit_companions else None,
    )
    task = kb.get_task(kanban_conn, tid)

    assert task is not None
    assert tid == task_id
    assert task.project_id == proj.id
    assert task.workspace_kind == "worktree"
    # Worktree dir anchored under the project's primary repo, keyed on task id.
    assert task.workspace_path == str(expected_path)
    # Deterministic branch: <slug>/<task-id>-<title-slug>. NOT a random wt/...
    assert task.branch_name == expected_branch
    assert not task.branch_name.startswith("wt/")
    workspace = kb.resolve_workspace(task)
    assert workspace == expected_path.resolve()
    assert kb._is_linked_worktree_checkout(workspace)
    assert kb._git_toplevel(workspace) == workspace
    assert kb._git_common_dir(workspace) == kb._git_common_dir(project_repo)
    assert kb._git_current_branch(workspace) == task.branch_name


def test_project_linked_task_rejects_non_git_authority_atomically(
    kanban_conn, tmp_path, monkeypatch
):
    project_repo = tmp_path / "project-not-git"
    project_repo.mkdir()
    sentinel = project_repo / "preserve.txt"
    sentinel.write_text("preserve me\n", encoding="utf-8")
    proj = _make_project(project_repo, name="Not Git")
    root_id = kb.create_task(kanban_conn, title="existing root")
    kb.add_comment(kanban_conn, root_id, "reviewer", "preserve me")
    task_id = "t_non_git_authority"
    task_id_calls = 0
    write_txn_calls = 0
    real_write_txn = kb.write_txn

    def unexpected_task_id_generation():
        nonlocal task_id_calls
        task_id_calls += 1
        return task_id

    @contextlib.contextmanager
    def unexpected_kanban_write_txn(db_conn):
        nonlocal write_txn_calls
        write_txn_calls += 1
        with real_write_txn(db_conn):
            yield db_conn

    monkeypatch.setattr(kb, "_new_task_id", unexpected_task_id_generation)
    monkeypatch.setattr(kb, "write_txn", unexpected_kanban_write_txn)
    before_db = _sqlite_atomic_state(kanban_conn, root_id)
    before_files = tuple(
        sorted(str(path.relative_to(project_repo)) for path in project_repo.rglob("*"))
    )

    with pytest.raises(ValueError, match="not a Git repository"):
        kb.create_task(
            kanban_conn,
            title="must not persist",
            project_id=proj.id,
            parents=(root_id,),
        )

    assert _sqlite_atomic_state(kanban_conn, root_id) == before_db
    assert tuple(
        sorted(str(path.relative_to(project_repo)) for path in project_repo.rglob("*"))
    ) == before_files
    assert sentinel.read_text(encoding="utf-8") == "preserve me\n"
    assert not (project_repo / ".worktrees" / task_id).exists()
    assert task_id_calls == 0
    assert write_txn_calls == 0


@pytest.mark.parametrize("replacement_kind", ["non_git", "new_git"])
def test_project_linked_creation_revalidates_git_identity_after_kanban_lock(
    kanban_conn, tmp_path, monkeypatch, replacement_kind
):
    project_repo = _make_repo(tmp_path / f"project-race-{replacement_kind}")
    proj = _make_project(project_repo, name=f"Race {replacement_kind}")
    root_id = kb.create_task(kanban_conn, title="existing parent")
    kb.add_comment(kanban_conn, root_id, "reviewer", "preserve")
    before_db = _sqlite_atomic_state(kanban_conn, root_id)

    task_id = f"t_git_authority_race_{replacement_kind}"
    monkeypatch.setattr(kb, "_new_task_id", lambda: task_id)
    real_write_txn = kb.write_txn
    injected = False

    @contextlib.contextmanager
    def replace_git_after_project_authority(db_conn):
        nonlocal injected
        if not injected:
            project_repo.rename(tmp_path / f"former-{replacement_kind}")
            if replacement_kind == "new_git":
                _make_repo(project_repo)
            else:
                project_repo.mkdir()
                (project_repo / "ordinary.txt").write_text(
                    "not git\n", encoding="utf-8"
                )
            injected = True
        with real_write_txn(db_conn):
            yield db_conn

    monkeypatch.setattr(kb, "write_txn", replace_git_after_project_authority)

    with pytest.raises(
        ValueError,
        match="project-linked task authority changed during creation; retry",
    ):
        kb.create_task(
            kanban_conn,
            title="must reject atomically",
            project_id=proj.id,
            parents=(root_id,),
        )

    assert injected is True
    assert _sqlite_atomic_state(kanban_conn, root_id) == before_db
    assert not (project_repo / ".worktrees" / task_id).exists()


@pytest.mark.parametrize("companion", ["branch", "wrong_path", "dir"])
@pytest.mark.parametrize("identity_source", ["explicit", "board"])
def test_project_linked_task_rejects_noncanonical_companion_atomically(
    kanban_conn, tmp_path, monkeypatch, companion, identity_source
):
    suffix = f"{identity_source}-{companion}"
    project_repo = _make_repo(tmp_path / f"project-{suffix}")
    wrong_repo = _make_repo(tmp_path / f"wrong-{suffix}")
    proj = _make_project(project_repo, name=f"Project {suffix}")
    root_id = kb.create_task(kanban_conn, title="existing root")
    kb.add_comment(kanban_conn, root_id, "reviewer", "preserve me")
    if identity_source == "board":
        kb.write_board_metadata("default", project_id=proj.id)

    task_id = "t_route_contract"
    monkeypatch.setattr(kb, "_new_task_id", lambda: task_id)
    workspace_kind = "worktree"
    workspace_path = None
    branch_name = None
    if companion == "branch":
        branch_name = "feature/custom"
    elif companion == "wrong_path":
        workspace_path = str(wrong_repo / ".worktrees" / task_id)
    else:
        workspace_kind = "dir"
        workspace_path = str(wrong_repo / "task-dir")

    before_db = _sqlite_atomic_state(kanban_conn, root_id)
    before_fs = _repo_effect_state(project_repo, wrong_repo)

    with pytest.raises(ValueError, match="project-linked task"):
        kb.create_task(
            kanban_conn,
            title="must not persist",
            project_id=proj.slug if identity_source == "explicit" else None,
            parents=(root_id,),
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
            branch_name=branch_name,
        )

    assert _sqlite_atomic_state(kanban_conn, root_id) == before_db
    assert _repo_effect_state(project_repo, wrong_repo) == before_fs
    assert not (project_repo / ".worktrees" / task_id).exists()
    assert not (wrong_repo / ".worktrees" / task_id).exists()
    assert not (wrong_repo / "task-dir").exists()


@pytest.mark.skipif(os.name == "nt", reason="symlink route checks require POSIX")
@pytest.mark.parametrize("alias_kind", ["parent", "leaf"])
def test_project_linked_task_rejects_symlinked_canonical_target_atomically(
    kanban_conn, tmp_path, monkeypatch, alias_kind
):
    project_repo = _make_repo(tmp_path / f"project-symlink-{alias_kind}")
    proj = _make_project(project_repo, name=f"Symlink {alias_kind}")
    task_id = "t_symlinked_route"
    external = tmp_path / f"external-{alias_kind}"
    external.mkdir()
    worktrees = project_repo / ".worktrees"
    if alias_kind == "parent":
        worktrees.symlink_to(external, target_is_directory=True)
    else:
        worktrees.mkdir()
        external_target = external / task_id
        external_target.mkdir()
        (worktrees / task_id).symlink_to(
            external_target,
            target_is_directory=True,
        )

    root_id = kb.create_task(kanban_conn, title="existing root")
    kb.add_comment(kanban_conn, root_id, "reviewer", "preserve me")
    monkeypatch.setattr(kb, "_new_task_id", lambda: task_id)
    before_db = _sqlite_atomic_state(kanban_conn, root_id)
    before_fs = _repo_effect_state(project_repo)
    before_external = tuple(
        sorted(str(path.relative_to(external)) for path in external.rglob("*"))
    )

    with pytest.raises(ValueError, match="symlinked authoritative workspace route"):
        kb.create_task(
            kanban_conn,
            title="must not persist",
            project_id=proj.id,
            parents=(root_id,),
        )

    assert _sqlite_atomic_state(kanban_conn, root_id) == before_db
    assert _repo_effect_state(project_repo) == before_fs
    assert tuple(
        sorted(str(path.relative_to(external)) for path in external.rglob("*"))
    ) == before_external


def test_project_linked_creation_revalidates_registry_before_write(
    kanban_conn, tmp_path, monkeypatch
):
    project_repo = _make_repo(tmp_path / "project-registry-a")
    moved_repo = _make_repo(tmp_path / "project-registry-b")
    proj = _make_project(project_repo, name="Registry Race")
    root_id = kb.create_task(kanban_conn, title="existing root")
    kb.add_comment(kanban_conn, root_id, "reviewer", "preserve me")
    before_db = _sqlite_atomic_state(kanban_conn, root_id)
    before_fs = _repo_effect_state(project_repo, moved_repo)

    real_get_project = pdb.get_project
    registry_changed = False

    def get_project_then_move_primary(project_conn, project_id):
        nonlocal registry_changed
        project = real_get_project(project_conn, project_id)
        if not registry_changed:
            with pdb.connect_closing() as mutation_conn:
                with pdb.write_txn(mutation_conn):
                    mutation_conn.execute(
                        "UPDATE projects SET primary_path = ? WHERE id = ?",
                        (str(moved_repo), proj.id),
                    )
            registry_changed = True
        return project

    monkeypatch.setattr(pdb, "get_project", get_project_then_move_primary)
    task_id = "t_registry_race"
    monkeypatch.setattr(kb, "_new_task_id", lambda: task_id)

    with pytest.raises(ValueError, match="authority changed during creation"):
        kb.create_task(
            kanban_conn,
            title="must not persist",
            project_id=proj.id,
            parents=(root_id,),
        )

    assert registry_changed is True
    assert _sqlite_atomic_state(kanban_conn, root_id) == before_db
    assert _repo_effect_state(project_repo, moved_repo) == before_fs
    assert not (project_repo / ".worktrees" / task_id).exists()
    assert not (moved_repo / ".worktrees" / task_id).exists()
    with pdb.connect_closing() as project_conn:
        current = real_get_project(project_conn, proj.id)
    assert current is not None
    assert current.primary_path == str(moved_repo)


def test_unlinked_task_unchanged(kanban_conn):
    tid = kb.create_task(kanban_conn, title="plain")
    task = kb.get_task(kanban_conn, tid)

    assert task.project_id is None
    assert task.workspace_kind == "scratch"
    # No branch is persisted — the worker still owns the wt/<id> fallback for
    # genuinely ad-hoc worktree tasks, but unlinked scratch tasks have none.
    assert task.branch_name is None


