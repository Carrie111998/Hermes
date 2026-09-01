"""Regression tests for ``_recover_same_branch_divergence``.

Live incident (2026-08-23): a checkout on ``main`` (the update target
itself, not a parked custom branch) had one local commit not yet pushed
anywhere. ``origin/main`` had also moved forward, so
``git merge --ff-only origin/main`` failed with "not possible to
fast-forward". The old code treated *any* such failure while on the
target branch as proof of an upstream force-push/rebase and unconditionally
ran ``git reset --hard origin/main`` — silently discarding the local
commit with no tag, no stash, nothing. It was only recoverable via
``git reflog`` because the object happened not to be garbage collected yet.

The fix (``_recover_same_branch_divergence``): before resetting, count
commits on HEAD that ``origin/<branch>`` doesn't have. If there are any,
merge instead (tagging HEAD first as a recovery anchor) and stop cleanly
on conflict rather than ever discarding them.

Note on the reset --hard branch (count == 0): for a full, non-shallow
clone this is provably unreachable — ``merge --ff-only`` succeeding is
logically equivalent to ``rev-list origin/<branch>..HEAD --count`` being
0, so if ff-only already failed (the precondition for calling this
function at all), the count is always >= 1 and the merge path is always
taken. It's kept as a defensive fallback for shallow-clone states, where
this file elsewhere notes the commit count can be unreliable
(``apply_is_shallow`` / "Shallow checkout, exact count unrecoverable").
Not covered here since it isn't constructible with plain git commands
against a full clone.

These tests run against REAL git repositories (init, commit, clone) — not
mocked subprocess.run — matching test_update_parked_branch_guard.py.
"""

import subprocess

import pytest

from hermes_cli import update_cmd


GIT = ["git"]


def _git(cwd, *args, check=True):
    return subprocess.run(
        GIT + list(args),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


def _head_sha(cwd):
    return _git(cwd, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture()
def origin_and_clone(tmp_path):
    """A real origin repo + local clone, both starting on ``main`` at the
    same commit."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "test@example.com")
    _git(origin, "config", "user.name", "Test")
    (origin / "a.txt").write_text("one\n")
    _git(origin, "add", "a.txt")
    _git(origin, "commit", "-qm", "c1")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test")
    return origin, clone


def test_local_only_commit_is_merged_not_reset(origin_and_clone):
    """The exact incident shape: clone has a local commit directly on
    ``main`` that was never pushed; origin/main also advanced. The local
    commit must survive."""
    origin, clone = origin_and_clone

    # Local, unpushed commit directly on main.
    (clone / "local.txt").write_text("local fix\n")
    _git(clone, "add", "local.txt")
    _git(clone, "commit", "-qm", "local fix, not yet pushed")
    local_sha = _head_sha(clone)

    # Origin advances independently.
    (origin / "b.txt").write_text("upstream\n")
    _git(origin, "add", "b.txt")
    _git(origin, "commit", "-qm", "upstream advance")

    _git(clone, "fetch", "-q", "origin", "main")
    assert _git(clone, "merge", "--ff-only", "origin/main", check=False).returncode != 0

    ok = update_cmd._recover_same_branch_divergence(GIT, clone, "main")

    assert ok is True
    # The local commit's content must still be reachable from HEAD.
    assert local_sha in _git(clone, "log", "--format=%H").stdout.splitlines()
    assert (clone / "local.txt").exists()
    assert (clone / "b.txt").exists()  # upstream's commit also merged in


def test_merge_conflict_stops_cleanly_without_reset(origin_and_clone):
    """Local-only commit conflicts with upstream's change to the same
    line: must abort the merge and report failure — never fall back to
    reset --hard, which would still destroy the local commit."""
    origin, clone = origin_and_clone

    (clone / "a.txt").write_text("local change\n")
    _git(clone, "commit", "-aqm", "local change")
    local_sha = _head_sha(clone)

    (origin / "a.txt").write_text("conflicting upstream change\n")
    _git(origin, "commit", "-aqm", "upstream change")

    _git(clone, "fetch", "-q", "origin", "main")
    assert _git(clone, "merge", "--ff-only", "origin/main", check=False).returncode != 0

    ok = update_cmd._recover_same_branch_divergence(GIT, clone, "main")

    assert ok is False
    # Local commit must still be exactly where it was — no reset occurred.
    assert _head_sha(clone) == local_sha
    # No merge left half-applied.
    status = _git(clone, "status", "--porcelain").stdout
    assert status == ""


def test_local_only_commit_tags_a_recovery_anchor(origin_and_clone):
    """A pre-update-<timestamp> tag is created before the merge attempt,
    so the pre-merge state stays recoverable even in edge cases the merge
    itself doesn't handle cleanly."""
    origin, clone = origin_and_clone

    (clone / "local.txt").write_text("local fix\n")
    _git(clone, "add", "local.txt")
    _git(clone, "commit", "-qm", "local fix")

    (origin / "b.txt").write_text("upstream\n")
    _git(origin, "add", "b.txt")
    _git(origin, "commit", "-qm", "upstream advance")

    _git(clone, "fetch", "-q", "origin", "main")

    before_tags = set(_git(clone, "tag", "-l").stdout.split())
    update_cmd._recover_same_branch_divergence(GIT, clone, "main")
    after_tags = set(_git(clone, "tag", "-l").stdout.split())

    new_tags = after_tags - before_tags
    assert any(t.startswith("pre-update-") for t in new_tags)
