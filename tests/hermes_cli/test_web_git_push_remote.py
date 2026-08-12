"""The review push must never guess ``origin`` as its remote.

In a fork checkout ``origin`` is frequently the UPSTREAM project rather than the
user's own repo (``~/.hermes/agent-src`` is exactly that: ``origin`` is
NousResearch/hermes-agent, the fork is ``daragao3``). A push that hardcodes
``origin`` therefore publishes private work to a public upstream and pins the
branch's tracking there permanently.

These tests use real git against local bare repositories, so each one proves
*which* remote received the branch rather than which argv was assembled.
"""

from __future__ import annotations

import subprocess

import pytest

from hermes_cli import web_git


def _git(cwd, *args):
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout.strip()


def _bare(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    _git(path, "init", "-q", "--bare")
    return path


def _work_repo(tmp_path, name="work"):
    path = tmp_path / name
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "hermes-test@example.com")
    _git(path, "config", "user.name", "Hermes Test")
    _git(path, "checkout", "-q", "-b", "feature/x")
    (path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-qm", "initial")
    return path


def _has_branch(bare, branch):
    out = subprocess.run(
        ["git", "branch", "--list", branch],
        cwd=str(bare),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return branch in out.stdout


def test_push_refuses_to_guess_when_several_remotes_and_no_push_default(tmp_path):
    """An untracked branch in a fork must not silently publish to ``origin``."""
    upstream = _bare(tmp_path, "upstream.git")
    fork = _bare(tmp_path, "fork.git")
    repo = _work_repo(tmp_path)
    # Mirrors agent-src: `origin` is the upstream project, the fork is separate.
    _git(repo, "remote", "add", "origin", str(upstream))
    _git(repo, "remote", "add", "fork", str(fork))

    with pytest.raises(RuntimeError) as excinfo:
        web_git.review_push(str(repo))

    assert "origin" in str(excinfo.value) or "remote" in str(excinfo.value)
    assert not _has_branch(upstream, "feature/x"), "published to the upstream remote"
    assert not _has_branch(fork, "feature/x")


def test_push_uses_the_sole_remote_even_when_it_is_not_named_origin(tmp_path):
    """One remote is unambiguous — no guessing required, whatever it is called."""
    fork = _bare(tmp_path, "fork.git")
    repo = _work_repo(tmp_path)
    _git(repo, "remote", "add", "daragao3", str(fork))

    web_git.review_push(str(repo))

    assert _has_branch(fork, "feature/x")


def test_push_honours_branch_push_remote_over_origin(tmp_path):
    upstream = _bare(tmp_path, "upstream.git")
    fork = _bare(tmp_path, "fork.git")
    repo = _work_repo(tmp_path)
    _git(repo, "remote", "add", "origin", str(upstream))
    _git(repo, "remote", "add", "fork", str(fork))
    _git(repo, "config", "branch.feature/x.pushRemote", "fork")

    web_git.review_push(str(repo))

    assert _has_branch(fork, "feature/x")
    assert not _has_branch(upstream, "feature/x")


def test_push_honours_remote_push_default_over_origin(tmp_path):
    upstream = _bare(tmp_path, "upstream.git")
    fork = _bare(tmp_path, "fork.git")
    repo = _work_repo(tmp_path)
    _git(repo, "remote", "add", "origin", str(upstream))
    _git(repo, "remote", "add", "fork", str(fork))
    _git(repo, "config", "remote.pushDefault", "fork")

    web_git.review_push(str(repo))

    assert _has_branch(fork, "feature/x")
    assert not _has_branch(upstream, "feature/x")


def test_push_with_an_upstream_still_pushes_to_that_upstream(tmp_path):
    """The existing tracking path must keep working untouched."""
    fork = _bare(tmp_path, "fork.git")
    repo = _work_repo(tmp_path)
    _git(repo, "remote", "add", "fork", str(fork))
    _git(repo, "push", "-q", "-u", "fork", "feature/x")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "second")

    web_git.review_push(str(repo))

    assert _git(fork, "rev-parse", "feature/x") == _git(repo, "rev-parse", "HEAD")
