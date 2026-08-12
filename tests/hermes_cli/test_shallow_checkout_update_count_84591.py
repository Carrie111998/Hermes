"""Regression for #84591 — a --depth 1 check-fetch permanently poisons
merge-base on shallow installer checkouts, stalling the update indicator
at a placeholder "1 commit behind" forever.

Uses REAL git repositories (not mocked subprocess calls): the bug is in
git's own shallow-fetch semantics (whether --depth 1 marks an unconditional
new shallow boundary vs. a plain fetch correctly extending an existing one
incrementally), which a mock cannot meaningfully exercise.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, check=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com",
             "PATH": "/usr/bin:/bin"},
    )


def _commit(repo: Path, message: str) -> None:
    (repo / "file.txt").write_text(message)
    _run(["git", "add", "file.txt"], cwd=repo)
    _run(["git", "commit", "-q", "-m", message], cwd=repo)


@pytest.fixture
def shallow_checkout(tmp_path):
    """A real shallow clone of a real 'remote' repo, matching the
    installer's `git clone --depth 1`. Returns (clone_dir, remote_dir)."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _run(["git", "init", "-q", "-b", "main"], cwd=remote)
    for i in range(1, 21):
        _commit(remote, f"commit {i}")

    clone = tmp_path / "shallow_clone"
    _run(
        ["git", "clone", "--depth", "1", f"file://{remote}", str(clone)],
        cwd=tmp_path,
    )
    return clone, remote


def test_shallow_clone_fixture_is_actually_shallow(shallow_checkout):
    """Sanity check on the test fixture itself."""
    clone, _ = shallow_checkout
    result = subprocess.run(
        ["git", "rev-parse", "--is-shallow-repository"],
        cwd=str(clone), capture_output=True, text=True,
    )
    assert result.stdout.strip() == "true"


def test_plain_fetch_after_shallow_clone_preserves_exact_count(shallow_checkout):
    """The core empirical claim behind this fix: a PLAIN (no --depth)
    branch-scoped fetch on an already-shallow clone correctly extends the
    shallow boundary forward, without dragging in full history, and
    merge-base/rev-list stay exact. This is what banner.py/update_cmd.py's
    fetch now does instead of --depth 1."""
    clone, remote = shallow_checkout
    for i in range(21, 24):
        _commit(remote, f"commit {i}")

    branch_result = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=str(clone), capture_output=True, text=True,
    )
    branch = branch_result.stdout.strip()

    fetch = subprocess.run(
        ["git", "fetch", "origin", branch],
        cwd=str(clone), capture_output=True, text=True,
    )
    assert fetch.returncode == 0

    merge_base = subprocess.run(
        ["git", "merge-base", "HEAD", f"origin/{branch}"],
        cwd=str(clone), capture_output=True, text=True,
    )
    assert merge_base.returncode == 0, (
        "merge-base must succeed after a plain fetch on a shallow clone"
    )

    count = subprocess.run(
        ["git", "rev-list", "--count", f"HEAD..origin/{branch}"],
        cwd=str(clone), capture_output=True, text=True,
    )
    assert count.returncode == 0
    assert count.stdout.strip() == "3"


def test_depth_1_fetch_poisons_merge_base_permanently(shallow_checkout):
    """Confirms the reported bug's exact mechanism, as a negative control:
    a --depth 1 fetch on an already-shallow clone marks a NEW shallow
    boundary and breaks merge-base -- and does not self-heal on a later
    plain fetch, since the next tip connects to the now-orphaned boundary.
    This is the behavior the fix removes from the check paths."""
    clone, remote = shallow_checkout
    for i in range(21, 23):
        _commit(remote, f"commit {i}")

    branch_result = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=str(clone), capture_output=True, text=True,
    )
    branch = branch_result.stdout.strip()

    subprocess.run(
        ["git", "fetch", "--depth", "1", "origin", branch],
        cwd=str(clone), capture_output=True, text=True,
    )
    merge_base = subprocess.run(
        ["git", "merge-base", "HEAD", f"origin/{branch}"],
        cwd=str(clone), capture_output=True, text=True,
    )
    assert merge_base.returncode != 0, (
        "sanity: --depth 1 must reproduce the merge-base failure "
        "this fix works around"
    )

    # And it does NOT self-heal on a later plain fetch, even with more
    # commits landing -- matching the reporter's "stuck forever" claim.
    _commit(remote, "commit 23")
    subprocess.run(
        ["git", "fetch", "origin", branch],
        cwd=str(clone), capture_output=True, text=True,
    )
    merge_base_2 = subprocess.run(
        ["git", "merge-base", "HEAD", f"origin/{branch}"],
        cwd=str(clone), capture_output=True, text=True,
    )
    assert merge_base_2.returncode != 0, (
        "the poisoned boundary must not self-heal from a plain fetch alone"
    )


def test_check_via_local_git_reports_exact_count_on_shallow_checkout(
    shallow_checkout, monkeypatch
):
    """End-to-end: banner.py's _check_via_local_git() against a real
    shallow checkout must report the EXACT commit count, not the
    presence-only placeholder, once its fetch no longer uses --depth 1."""
    clone, remote = shallow_checkout
    for i in range(21, 25):
        _commit(remote, f"commit {i}")

    import hermes_cli.banner as banner_mod

    monkeypatch.setattr(banner_mod, "_is_official_ssh_remote", lambda url: False)

    result = banner_mod._check_via_local_git(clone)
    assert result == 4, (
        f"expected the exact behind-count (4), got {result!r} -- if this "
        f"is banner_mod.UPDATE_AVAILABLE_NO_COUNT (-1), the placeholder "
        f"path fired instead of the exact count"
    )
