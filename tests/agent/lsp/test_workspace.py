"""Tests for workspace + project-root resolution."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agent.lsp.workspace import (
    clear_cache,
    find_git_worktree,
    is_inside_workspace,
    nearest_root,
    normalize_path,
    resolve_workspace_for_file,
)


@pytest.fixture(autouse=True)
def _clear():
    clear_cache()
    yield
    clear_cache()


def _init_git_repo(repo: Path) -> None:
    subprocess.run(
        ["git", "init", "--quiet", str(repo)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_find_git_worktree_returns_none_outside_repo(tmp_path: Path):
    sub = tmp_path / "sub"
    sub.mkdir()
    assert find_git_worktree(str(sub)) is None


def test_find_git_worktree_finds_dotgit(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    assert find_git_worktree(str(sub)) == str(repo)


def test_find_git_worktree_ignores_invalid_dotgit_marker(tmp_path: Path):
    """An arbitrary ``.git`` directory must not make its parent a workspace."""
    not_a_repo = tmp_path / "not-a-repo"
    (not_a_repo / ".git").mkdir(parents=True)
    src = not_a_repo / "src" / "orphan.py"
    src.parent.mkdir()
    src.write_text("")

    assert find_git_worktree(str(src)) is None


def test_find_git_worktree_handles_dotgit_file(tmp_path: Path):
    """A real linked worktree with a ``.git`` file is accepted."""
    source = tmp_path / "source"
    _init_git_repo(source)
    subprocess.run(
        [
            "git", "-C", str(source), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid", "commit", "--quiet",
            "--allow-empty", "-m", "init",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "-C", str(source), "worktree", "add", "--quiet", str(repo)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert (repo / ".git").is_file()
    assert find_git_worktree(str(repo)) == str(repo)


def test_is_inside_workspace_true_for_subpath(tmp_path: Path):
    root = tmp_path / "p"
    root.mkdir()
    sub = root / "x" / "y.py"
    sub.parent.mkdir(parents=True)
    sub.write_text("")
    assert is_inside_workspace(str(sub), str(root))


def test_is_inside_workspace_false_for_unrelated(tmp_path: Path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    f = b / "x.py"
    f.write_text("")
    assert not is_inside_workspace(str(f), str(a))


def test_nearest_root_finds_first_marker(tmp_path: Path):
    root = tmp_path / "p"
    deep = root / "src" / "pkg"
    deep.mkdir(parents=True)
    (root / "pyproject.toml").write_text("")
    found = nearest_root(str(deep / "mod.py"), ["pyproject.toml"])
    assert found == str(root)


def test_nearest_root_excludes_take_priority(tmp_path: Path):
    """If an exclude marker matches first, return None."""
    root = tmp_path / "p"
    sub = root / "deno-app"
    sub.mkdir(parents=True)
    (sub / "deno.json").write_text("{}")
    (root / "package.json").write_text("{}")  # would match if not for exclude
    found = nearest_root(
        str(sub / "main.ts"),
        ["package.json"],
        excludes=["deno.json"],
    )
    assert found is None


def test_nearest_root_returns_none_when_no_marker(tmp_path: Path):
    f = tmp_path / "x.py"
    f.write_text("")
    assert nearest_root(str(f), ["pyproject.toml"]) is None


def test_resolve_workspace_for_file_uses_cwd_first(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    file_path = repo / "x.py"
    file_path.write_text("")
    # cwd is inside the repo
    monkeypatch.chdir(str(repo))
    root, gated = resolve_workspace_for_file(str(file_path))
    assert root == str(repo)
    assert gated is True


def test_resolve_workspace_for_file_no_repo_returns_none(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(str(tmp_path))
    f = tmp_path / "x.py"
    f.write_text("")
    root, gated = resolve_workspace_for_file(str(f))
    assert root is None
    assert gated is False


def test_resolve_workspace_falls_back_to_file_location(tmp_path: Path, monkeypatch):
    """When cwd isn't a git repo but the file is inside one, we still
    discover the workspace from the file's path."""
    not_a_repo = tmp_path / "loose"
    not_a_repo.mkdir()
    monkeypatch.chdir(str(not_a_repo))

    repo = tmp_path / "actual-repo"
    _init_git_repo(repo)
    f = repo / "x.py"
    f.write_text("")

    root, gated = resolve_workspace_for_file(str(f))
    assert root == str(repo)
    assert gated is True


def test_normalize_path_expands_tilde(monkeypatch):
    monkeypatch.setenv("HOME", "/home/user")
    p = normalize_path("~/x.py")
    assert p == os.path.abspath("/home/user/x.py")
