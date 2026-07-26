"""Unit tests for ``tui_gateway.git_probe.resolve`` root normalization.

The desktop "duplicate branch lanes" bug (a repo's main checkout showing both a
``main`` lane and a folder-named lane holding the same sessions) traces back to
``resolve`` returning a ``worktree_root`` and ``repo_root`` that are the same
location spelled differently, so ``project_tree`` cannot recognize the main
checkout. In production the mismatch is Windows separator style —
``git rev-parse --show-toplevel`` prints ``C:/Users/x/repo`` while
``common_repo_root`` runs the result through ``os.path.realpath`` and gets
``C:\\Users\\x\\repo``. ``resolve`` now normalizes both with
``os.path.normpath`` so the main-checkout comparison holds.

These tests exercise the normalization portably (``os.path.normpath`` is
``ntpath`` on Windows but ``posixpath`` on the Linux CI host, so backslash
conversion can't be asserted cross-platform); they use redundant path segments
that ``posixpath.normpath`` collapses to prove both roots are normalized and end
up equal for a main checkout.
"""
from __future__ import annotations

from tui_gateway import git_probe


def test_resolve_normalizes_both_roots(monkeypatch):
    # Same location, spelled with a redundant "." segment / trailing slash — the
    # kind of textual difference that left main checkouts misclassified.
    monkeypatch.setattr(git_probe, "repo_root", lambda _cwd: "/home/u/repo/./")
    monkeypatch.setattr(git_probe, "common_repo_root", lambda _cwd: "/home/u/repo")

    info = git_probe.resolve("/home/u/repo")

    assert info == {"repo_root": "/home/u/repo", "worktree_root": "/home/u/repo"}
    # The whole point: a main checkout now compares equal.
    assert info["worktree_root"] == info["repo_root"]


def test_resolve_preserves_distinct_worktree_root(monkeypatch):
    # A genuine linked worktree (distinct location) must stay distinct after
    # normalization, so it is still folded under its common root as non-main.
    monkeypatch.setattr(git_probe, "repo_root", lambda _cwd: "/home/u/repo/../repo-wt")
    monkeypatch.setattr(git_probe, "common_repo_root", lambda _cwd: "/home/u/repo")

    info = git_probe.resolve("/home/u/repo-wt")

    assert info == {"repo_root": "/home/u/repo", "worktree_root": "/home/u/repo-wt"}
    assert info["worktree_root"] != info["repo_root"]


def test_resolve_returns_none_when_not_a_repo(monkeypatch):
    monkeypatch.setattr(git_probe, "repo_root", lambda _cwd: "")

    assert git_probe.resolve("/tmp/not-a-repo") is None
