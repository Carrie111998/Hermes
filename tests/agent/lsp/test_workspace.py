"""Tests for workspace + project-root resolution."""
from __future__ import annotations

import os
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




def test_find_git_worktree_finds_dotgit(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    assert find_git_worktree(str(sub)) == str(repo)








def test_nearest_root_finds_first_marker(tmp_path: Path):
    root = tmp_path / "p"
    deep = root / "src" / "pkg"
    deep.mkdir(parents=True)
    (root / "pyproject.toml").write_text("")
    found = nearest_root(str(deep / "mod.py"), ["pyproject.toml"])
    assert found == str(root)






def test_resolve_workspace_for_file_uses_cwd_first(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    file_path = repo / "x.py"
    file_path.write_text("")
    # cwd is inside the repo
    monkeypatch.chdir(str(repo))
    root, gated = resolve_workspace_for_file(str(file_path))
    assert root == str(repo)
    assert gated is True






def test_normalize_path_expands_tilde(monkeypatch):
    monkeypatch.setenv("HOME", "/home/user")
    p = normalize_path("~/x.py")
    assert p == os.path.abspath("/home/user/x.py")


def test_resolve_workspace_for_file_deleted_cwd_does_not_raise(tmp_path: Path, monkeypatch):
    """Process CWD deleted mid-run (workspace-wipe family, t_886b35f5) must
    not crash workspace resolution: os.getcwd() raises ENOENT from a removed
    directory, which previously turned LSP-gated writes into spurious
    ``[Errno 2] No such file or directory`` tool errors even though the
    write itself succeeded.  Resolution falls back to the file's own
    directory as the cwd anchor."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    file_path = repo / "x.py"
    file_path.write_text("")

    def _boom_getcwd():
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(os, "getcwd", _boom_getcwd)
    # Absolute file path: the file's own dir anchors the walk.
    root, gated = resolve_workspace_for_file(str(file_path))
    assert root == str(repo)
    assert gated is True


def test_resolve_workspace_for_file_deleted_cwd_outside_repo(tmp_path: Path, monkeypatch):
    """Same deleted-CWD condition but the file lives outside any git
    worktree — must return (None, False), not raise."""
    file_path = tmp_path / "plain.py"
    file_path.write_text("")

    def _boom_getcwd():
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(os, "getcwd", _boom_getcwd)
    root, gated = resolve_workspace_for_file(str(file_path))
    assert root is None
    assert gated is False
