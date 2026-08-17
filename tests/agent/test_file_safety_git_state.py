"""Tests for the git-managed state write guard in file_safety.

Regression for https://github.com/NousResearch/hermes-agent/issues/78793 —
``write_file`` / ``patch`` (and the other mutating file tools) could silently
rewrite git-managed state inside a normal repository's ``.git`` directory
(``HEAD``, ``index``, ``refs/``, ``objects/``, ``logs/``, ``packed-refs``,
``ORIG_HEAD``, ...). A single misdirected write to ``.git/HEAD`` replaces the
branch identity and turns a healthy checkout into an apparently empty one —
the same silent-corruption shape as the #78565 worktree-pointer guard, but
inside the git directory itself.

These tests verify that git-managed control paths are write-denied while the
user-owned paths inside a git dir (``config``, ``hooks/*``,
``info/exclude``, ``description``) remain writable, and that normal
non-git files are untouched.
"""

from __future__ import annotations

import os

import pytest

from agent import file_safety as fs


@pytest.fixture()
def git_repo(tmp_path):
    """A minimal normal git-style repository layout under tmp_path."""
    repo = tmp_path / "repo"
    git = repo / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "objects").mkdir(parents=True)
    (git / "logs").mkdir(parents=True)
    (git / "hooks").mkdir(parents=True)
    (git / "info").mkdir(parents=True)
    (repo / "src").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (repo / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    return repo


# --- git-managed control paths must be denied ------------------------------

@pytest.mark.parametrize("rel", [
    ".git/HEAD",
    ".git/index",
    ".git/packed-refs",
    ".git/ORIG_HEAD",
    ".git/FETCH_HEAD",
    ".git/MERGE_HEAD",
    ".git/CHERRY_PICK_HEAD",
    ".git/REBASE_HEAD",
    ".git/COMMIT_EDITMSG",
    ".git/shallow",
    ".git/refs/heads/x",
    ".git/refs/tags/v1",
    ".git/objects/ab/cd1234",
    ".git/logs/HEAD",
    ".git/info/refs",
    ".git/info/alternates",
])
def test_git_managed_state_is_write_denied(git_repo, rel):
    target = git_repo / rel
    assert fs.is_write_denied(str(target)), f"{rel} should be write-denied"


def test_git_dir_itself_is_write_denied(git_repo):
    assert fs.is_write_denied(str(git_repo / ".git"))


def test_git_managed_error_message(git_repo):
    err = fs.get_write_denied_error(str(git_repo / ".git" / "HEAD"))
    assert err is not None
    assert "denied" in err


# --- user-owned paths inside .git must stay writable -----------------------

@pytest.mark.parametrize("rel", [
    ".git/config",
    ".git/description",
    ".git/hooks/pre-commit",
    ".git/hooks/post-commit",
    ".git/info/exclude",
])
def test_user_owned_git_paths_stay_writable(git_repo, rel):
    target = git_repo / rel
    assert not fs.is_write_denied(str(target)), f"{rel} should remain writable"


# --- non-git files and unrelated .git dirs -------------------------------

def test_normal_file_not_blocked(git_repo):
    assert not fs.is_write_denied(str(git_repo / "src" / "main.py"))


def test_parent_dir_not_blocked(git_repo):
    assert not fs.is_write_denied(str(git_repo))


def test_non_git_dotfile_named_gitfile_not_blocked(git_repo):
    # A regular file literally named ".git" (a worktree pointer, not a dir)
    # is the #78565 case handled elsewhere; here we ensure a plain sibling
    # directory named "notgit" is not caught.
    sibling = git_repo / "notgit" / "HEAD"
    sibling.parent.mkdir(parents=True, exist_ok=True)
    assert not fs.is_write_denied(str(sibling))


def test_dotgit_in_unrelated_subpath_not_blocked(git_repo):
    # `.git` as an intermediate dir inside a data folder (not a real git repo)
    # is still refused because git reserves that name, but a normal nested
    # dir that merely contains "git" in the name must be untouched.
    normal = git_repo / "git-config-example" / "HEAD"
    normal.parent.mkdir(parents=True, exist_ok=True)
    assert not fs.is_write_denied(str(normal))


def test_path_with_trailing_sep_is_git_dir(git_repo):
    # Ensure the guard also fires for `<repo>/.git/` with a trailing sep.
    assert fs.is_write_denied(str(git_repo / ".git" / ""))
