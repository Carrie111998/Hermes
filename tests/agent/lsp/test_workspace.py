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
from agent.lsp.servers import _root_python


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


def test_resolve_workspace_does_not_escape_current_worktree(
    tmp_path: Path, monkeypatch
):
    current = tmp_path / "current"
    other = tmp_path / "other"
    (current / ".git").mkdir(parents=True)
    (other / ".git").mkdir(parents=True)
    file_path = other / "outside.py"
    file_path.write_text("")

    monkeypatch.chdir(str(current))

    assert resolve_workspace_for_file(str(file_path)) == (None, False)


def test_python_root_does_not_escape_worktree_ceiling(tmp_path: Path):
    parent = tmp_path / "parent"
    workspace = parent / "workspace"
    source = workspace / "src" / "module.py"
    source.parent.mkdir(parents=True)
    (parent / "pyproject.toml").write_text("")
    (workspace / ".git").mkdir()
    source.write_text("")

    assert _root_python(str(source), str(workspace)) == str(workspace)






def test_normalize_path_expands_tilde(monkeypatch):
    monkeypatch.setenv("HOME", "/home/user")
    p = normalize_path("~/x.py")
    assert p == os.path.abspath("/home/user/x.py")
