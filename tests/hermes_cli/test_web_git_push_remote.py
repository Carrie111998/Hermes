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


# --- diff base ---------------------------------------------------------------
# `_branch_base` is the Python twin of the desktop's `branchBase`: it picks the
# ref the review diff is computed against. Hardcoding `origin/main` is wrong in a
# fork, where `origin` is the upstream project and the merge-base sits thousands
# of commits back. The fixture puts the FORK trunk AHEAD of origin's, which is the
# only shape that discriminates -- with upstream ahead, merge-base returns the
# shared tip either way and the test passes against the buggy code.


def _forked_repo(tmp_path):
    upstream = _bare(tmp_path, "upstream.git")
    fork = _bare(tmp_path, "fork.git")
    repo = tmp_path / "work"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "hermes-test@example.com")
    _git(repo, "config", "user.name", "Hermes Test")
    _git(repo, "checkout", "-q", "-B", "main")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "first")
    _git(repo, "remote", "add", "origin", str(upstream))
    _git(repo, "remote", "add", "fork", str(fork))
    _git(repo, "push", "-q", "origin", "main")
    at_origin = _git(repo, "rev-parse", "HEAD")

    (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "fork-only commit")
    _git(repo, "push", "-q", "fork", "main")
    at_fork = _git(repo, "rev-parse", "HEAD")

    _git(repo, "fetch", "-q", "origin")
    _git(repo, "fetch", "-q", "fork")
    _git(repo, "checkout", "-q", "-b", "feature/x")
    (repo / "tracked.txt").write_text("feature\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "feature work")
    return at_fork, at_origin, repo


def test_branch_base_prefers_the_trunk_configured_upstream(tmp_path):
    at_fork, at_origin, repo = _forked_repo(tmp_path)
    _git(repo, "config", "branch.main.remote", "fork")
    _git(repo, "config", "branch.main.merge", "refs/heads/main")

    base = web_git._branch_base(str(repo))

    assert base == at_fork, "diff base should follow the fork trunk"
    assert base != at_origin, "diff base fell back to the upstream project"


def test_branch_base_still_falls_back_to_origin_without_a_trunk_upstream(tmp_path):
    _at_fork, at_origin, repo = _forked_repo(tmp_path)

    assert web_git._branch_base(str(repo)) == at_origin


# --- longer resolution chain --------------------------------------------------
# Ported from a sibling session that fixed the same defect independently. Its
# chain consults more explicit user config before giving up, and validates
# configured values against the real remote list -- git happily stores a URL in
# `branch.<b>.remote`, and passing that through as if it were a remote NAME is
# how a "resolved" remote silently becomes an unintended push target.


def test_push_honours_branch_remote_when_push_remote_is_unset(tmp_path):
    upstream = _bare(tmp_path, "upstream.git")
    fork = _bare(tmp_path, "fork.git")
    repo = _work_repo(tmp_path)
    _git(repo, "remote", "add", "origin", str(upstream))
    _git(repo, "remote", "add", "fork", str(fork))
    _git(repo, "config", "branch.feature/x.remote", "fork")

    web_git.review_push(str(repo))

    assert _has_branch(fork, "feature/x")
    assert not _has_branch(upstream, "feature/x")


def test_push_falls_back_to_the_trunk_branch_remote(tmp_path):
    """agent-src's real shape: the feature branch has no config, but main does."""
    upstream = _bare(tmp_path, "upstream.git")
    fork = _bare(tmp_path, "fork.git")
    repo = _work_repo(tmp_path)
    _git(repo, "remote", "add", "origin", str(upstream))
    _git(repo, "remote", "add", "fork", str(fork))
    _git(repo, "config", "branch.main.remote", "fork")

    web_git.review_push(str(repo))

    assert _has_branch(fork, "feature/x")
    assert not _has_branch(upstream, "feature/x")


def test_push_ignores_a_configured_value_that_is_not_a_remote_name(tmp_path):
    """A URL in the config must not be passed through as if it were a remote."""
    upstream = _bare(tmp_path, "upstream.git")
    fork = _bare(tmp_path, "fork.git")
    repo = _work_repo(tmp_path)
    _git(repo, "remote", "add", "origin", str(upstream))
    _git(repo, "remote", "add", "fork", str(fork))
    _git(repo, "config", "branch.feature/x.pushRemote", str(upstream))

    with pytest.raises(RuntimeError):
        web_git.review_push(str(repo))

    assert not _has_branch(upstream, "feature/x"), "pushed to a raw URL from config"
    assert not _has_branch(fork, "feature/x")


def test_branch_base_uses_the_resolved_remote_when_the_trunk_has_no_upstream(tmp_path):
    """branch.main.remote alone (no .merge) still beats the origin/* fallback."""
    at_fork, at_origin, repo = _forked_repo(tmp_path)
    _git(repo, "config", "branch.main.remote", "fork")

    base = web_git._branch_base(str(repo))

    assert base == at_fork, "diff base ignored the configured trunk remote"
    assert base != at_origin
