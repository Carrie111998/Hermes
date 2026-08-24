"""Regression test for ``_remote_rewrote_history`` in ``hermes update``.

Live incident (2026-08-23): a reviewed, tested host-hygiene fix was committed
directly onto ``main`` in a live checkout (no fork/PR access). ``origin/main``
later advanced normally (new unrelated commits, no rewrite). The next
``hermes update`` ran ``merge --ff-only origin/main``, which failed (local
HEAD carried a commit origin didn't have), fell into the "same branch as the
update target" branch, and — because the old code assumed any ff-only
failure on the tracked branch meant an upstream force-push — ran
``reset --hard origin/main`` and silently destroyed the local commit.

The fix distinguishes the two cases by checking whether the origin tip we
knew about BEFORE the update's fetch is still an ancestor of the freshly
fetched tip: if yes, origin only moved forward (this incident); if no,
history was genuinely rewritten (the original, correct case for resetting).

These tests exercise ``_remote_rewrote_history`` against real git
repositories, not mocked subprocess.run.
"""

import subprocess

import pytest

from hermes_cli.update_cmd import _remote_rewrote_history

GIT = ["git"]


def _git(cwd, *args, check=True):
    return subprocess.run(
        GIT + list(args),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


def _rev_parse(cwd, ref):
    return _git(cwd, "rev-parse", ref).stdout.strip()


@pytest.fixture()
def origin_and_clone(tmp_path):
    """A real origin repo + local clone, both starting on ``main`` at c1."""
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


def test_local_only_commit_plus_normal_advance_is_not_a_rewrite(origin_and_clone):
    """The 2026-08-23 incident scenario: local commits directly on main,
    origin advances normally (no rewrite) — must be detected as safe-to-merge."""
    origin, clone = origin_and_clone

    # Capture the tip the clone already knows about, before anything moves.
    pre_fetch_tip = _rev_parse(clone, "origin/main")

    # A host-ops-style local-only commit lands directly on the clone's main.
    (clone / "local_fix.txt").write_text("host hygiene fix\n")
    _git(clone, "add", "local_fix.txt")
    _git(clone, "commit", "-qm", "local-only deploy commit")

    # Origin advances normally — a genuine fast-forward, nothing rewritten.
    (origin / "b.txt").write_text("two\n")
    _git(origin, "add", "b.txt")
    _git(origin, "commit", "-qm", "c2 (unrelated upstream work)")

    _git(clone, "fetch", "-q", "origin", "main")

    assert _remote_rewrote_history(GIT, clone, pre_fetch_tip, "main") is False


def test_genuine_force_push_is_detected_as_a_rewrite(origin_and_clone):
    """A true force-push (origin's old tip is no longer reachable) must
    still be treated as a rewrite so the original reset behavior applies."""
    origin, clone = origin_and_clone

    pre_fetch_tip = _rev_parse(clone, "origin/main")

    # Clone also makes an unrelated local commit (irrelevant to the outcome
    # here — the point under test is origin's history, not local's).
    (clone / "local.txt").write_text("local\n")
    _git(clone, "add", "local.txt")
    _git(clone, "commit", "-qm", "local commit")

    # Origin rewrites history: amend c1 into a different c1', discarding the
    # original tip entirely rather than building on top of it.
    _git(origin, "commit", "--amend", "-qm", "c1 (rewritten)")

    _git(clone, "fetch", "-q", "origin", "main")

    assert _remote_rewrote_history(GIT, clone, pre_fetch_tip, "main") is True


def test_no_prior_tip_defaults_to_rewrite_for_safety(origin_and_clone):
    """No prior origin tip to compare against (e.g. first-ever fetch) —
    must default to the original, safe-by-default reset behavior rather
    than guess that it's safe to merge."""
    origin, clone = origin_and_clone
    assert _remote_rewrote_history(GIT, clone, None, "main") is True
