"""Tests for the %SystemDrive% existence sweep.

The sweep replaces scripts/systemdrive_watcher.py (retired 2026-08-17). The
watcher's premise was that both the writer's ancestry and the artifact were
perishable, so detection had to be sub-millisecond. Neither holds on this box:
Security-4688 retains process ancestry with command lines for ~2 days, and the
junk trees persist on disk. What actually went wrong on 2026-08-14 was that
nobody LOOKED inside that retention window -- a failure a manually-armed
instrument cannot fix and a cheap always-on check can.

So the property under test is not latency. It is: does a sweep over every
checkout root notice a tree, and does it say plainly whether the sighting is
still attributable?
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from scripts.systemdrive_sweep import (
    JUNK_NAME,
    RETENTION_HOURS,
    append_log,
    checkout_roots,
    find_junk,
    format_report,
    is_attributable,
    log_path,
    main,
)


@pytest.fixture(autouse=True)
def _never_touch_the_real_log(tmp_path, monkeypatch):
    """Redirect the DEFAULT log path for every test in this module.

    Caught by actually running the registered scheduled task: three real
    sightings had been appended to ~/.hermes/logs/systemdrive-sweep.jsonl, all
    of them pytest fixture dirs under pytest-of-diego. `main()` falls back to
    log_path() when no --log is given, and two tests called it that way.

    That is worse than ordinary test pollution. This log is FORENSIC -- its
    whole job is to tell a future reader "a junk tree really appeared here".
    Test fixtures in it are fabricated evidence, and they are indistinguishable
    from the real thing at 3am six months from now.

    Autouse rather than per-test --no-log on purpose: a test that forgets the
    flag must not be able to reach the real path. Tests that assert ON the log
    still pass an explicit --log and are unaffected by this redirect.
    """
    monkeypatch.setattr(
        "scripts.systemdrive_sweep.log_path",
        lambda: tmp_path / "redirected" / "systemdrive-sweep.jsonl",
    )


def _make_checkout(tmp_path: Path, worktrees: tuple[str, ...] = ()) -> Path:
    """A fake shared checkout, optionally with .claude/worktrees siblings."""
    root = tmp_path / "agent-src"
    (root / "scripts").mkdir(parents=True)
    for name in worktrees:
        (root / ".claude" / "worktrees" / name / "scripts").mkdir(parents=True)
    return root


def _plant(root: Path) -> Path:
    """Create a junk tree exactly as the real writer does."""
    junk = root / JUNK_NAME / "ProgramData" / "Microsoft" / "Windows" / "Caches"
    junk.mkdir(parents=True)
    return root / JUNK_NAME


# --- root enumeration ----------------------------------------------------


def test_checkout_roots_covers_the_repo_root_and_every_worktree(tmp_path):
    root = _make_checkout(tmp_path, worktrees=("alpha-1", "beta-2"))

    roots = checkout_roots(root)

    assert root in roots, roots
    assert root / ".claude" / "worktrees" / "alpha-1" in roots, roots
    assert root / ".claude" / "worktrees" / "beta-2" in roots, roots
    assert len(roots) == 3, roots


def _shared_checkout() -> Path | None:
    """The SHARED checkout, resolved from git rather than from __file__.

    This test file usually runs from a worktree, and a worktree has no nested
    ``.claude/worktrees`` of its own -- so anchoring on ``__file__`` makes the
    guard below skip itself exactly where it runs most often. ``--git-common-dir``
    points at the shared ``.git`` from inside any worktree, and its parent is
    the shared checkout, which is the tree that actually has worktrees.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return Path(out.stdout.strip()).resolve().parent


def test_checkout_roots_finds_the_REAL_worktrees_on_this_box():
    """Anti-vacuous guard.

    Every "0 junk trees found" result is only as good as the root list it
    swept. If checkout_roots() silently returned just [repo_root] against the
    live checkout -- a wrong glob, a renamed worktree dir -- the sweep would
    report a permanent clean negative and this suite would still be green.
    That is the fabricated-clean-negative failure the retired watcher existed
    to avoid, and it outlives the retirement.

    Written the naive way first (anchor on ``__file__``, guard on ``is_dir()``)
    and caught by a deliberate probe that broke the worktree glob: this test
    stayed GREEN while two others went red, because from a worktree the guard
    simply skipped. A conditional anti-vacuous check is only as good as the
    condition -- if the setup it needs is missing, it must SKIP LOUDLY, never
    pass quietly.
    """
    shared = _shared_checkout()
    if shared is None:
        pytest.skip("not a git checkout -- cannot locate the shared worktree root")
    worktrees = shared / ".claude" / "worktrees"
    if not worktrees.is_dir():
        pytest.skip(f"no worktrees present at {worktrees} to prove the glob against")

    live = [p for p in worktrees.iterdir() if p.is_dir()]
    if not live:
        pytest.skip(f"{worktrees} exists but holds no worktree dirs")

    roots = checkout_roots(shared)

    assert shared in roots, roots
    assert len(roots) == len(live) + 1, (
        f"checkout_roots() returned {len(roots)} roots for a checkout with "
        f"{len(live)} live worktrees -- the glob has drifted and every sweep "
        "result is vacuous"
    )


def test_a_file_named_like_a_worktree_is_not_swept_as_a_root(tmp_path):
    root = _make_checkout(tmp_path)
    (root / ".claude" / "worktrees").mkdir(parents=True, exist_ok=True)
    (root / ".claude" / "worktrees" / "not-a-dir").write_text("x", encoding="utf-8")

    roots = checkout_roots(root)

    assert roots == [root], roots


# --- detection -----------------------------------------------------------


def test_clean_roots_produce_no_sightings(tmp_path):
    root = _make_checkout(tmp_path, worktrees=("alpha-1",))

    assert find_junk(checkout_roots(root)) == []


def test_a_planted_tree_is_found_in_the_repo_root(tmp_path):
    root = _make_checkout(tmp_path)
    planted = _plant(root)

    sightings = find_junk(checkout_roots(root))

    assert len(sightings) == 1, sightings
    assert sightings[0].path == planted


def test_a_planted_tree_is_found_INSIDE_A_WORKTREE(tmp_path):
    """The sightings that mattered were in worktrees, not the shared checkout.

    A sweep that only checked the repo root would have missed the 2026-08-15
    (objective-bose), 2026-08-16 (determined-raman) and 2026-08-17 (the
    watcher's own) trees -- i.e. every sighting but one.
    """
    root = _make_checkout(tmp_path, worktrees=("alpha-1", "beta-2"))
    planted = _plant(root / ".claude" / "worktrees" / "beta-2")

    sightings = find_junk(checkout_roots(root))

    assert [s.path for s in sightings] == [planted], sightings


def test_the_sweep_does_not_recurse_into_a_root(tmp_path):
    """Cost guard: one stat per root, not a tree walk.

    A nested %SystemDrive% is not the artifact this hunts -- the writer builds
    it in its CWD, which is always a checkout root. Recursing would turn a
    ~30-root stat into a walk of the entire multi-worktree tree, which is the
    kind of cost that gets an always-on check disabled.
    """
    root = _make_checkout(tmp_path)
    (root / "scripts" / "deep").mkdir(parents=True)
    _plant(root / "scripts" / "deep")

    assert find_junk(checkout_roots(root)) == []


# --- attribution window --------------------------------------------------


def test_a_fresh_sighting_is_attributable():
    now = 1_000_000.0
    assert is_attributable(now - 3600.0, now, RETENTION_HOURS) is True


def test_a_sighting_older_than_retention_is_not_attributable():
    """The 2026-08-14 case: found late, unattributable forever.

    This is the one the sweep exists to prevent, so the report must not imply
    a 4688 query will work when it cannot.
    """
    now = 1_000_000.0
    stale = now - (RETENTION_HOURS + 1.0) * 3600.0
    assert is_attributable(stale, now, RETENTION_HOURS) is False


def test_the_retention_boundary_is_treated_as_expired():
    now = 1_000_000.0
    exactly = now - RETENTION_HOURS * 3600.0
    assert is_attributable(exactly, now, RETENTION_HOURS) is False


# --- reporting -----------------------------------------------------------


def test_report_of_a_fresh_sighting_names_the_4688_query(tmp_path):
    root = _make_checkout(tmp_path)
    _plant(root)
    sightings = find_junk(checkout_roots(root))

    report = format_report(sightings, now=time.time())

    assert "4688" in report, report
    assert "Get-WinEvent" in report, report
    assert str(root) in report, report


def test_report_of_a_stale_sighting_says_it_is_unattributable(tmp_path):
    root = _make_checkout(tmp_path)
    _plant(root)
    sightings = find_junk(checkout_roots(root))
    much_later = time.time() + (RETENTION_HOURS + 24.0) * 3600.0

    report = format_report(sightings, now=much_later)

    assert "UNATTRIBUTABLE" in report, report
    assert "Get-WinEvent" not in report, (
        "a 4688 query is useless past retention -- offering it invites a "
        f"session to burn a ~90s query proving nothing:\n{report}"
    )


def test_clean_report_states_how_many_roots_were_swept(tmp_path):
    """A clean negative must show its work.

    "no junk found" is indistinguishable from "swept nothing" unless the
    count is in the message.

    The count is DERIVED from a real sweep, not passed as a literal: a
    hardcoded 3 here would keep passing even if checkout_roots() stopped
    finding the worktrees, which is the same vacuous-pass shape that the
    anti-vacuous guard above was written the wrong way for first.
    """
    root = _make_checkout(tmp_path, worktrees=("alpha-1", "beta-2"))
    roots = checkout_roots(root)

    report = format_report([], now=time.time(), roots_swept=len(roots))

    assert "3" in report, f"expected 3 roots (repo + 2 worktrees), got {roots}: {report}"


# --- exit contract -------------------------------------------------------


def test_main_exits_zero_on_a_clean_checkout(tmp_path, capsys):
    root = _make_checkout(tmp_path, worktrees=("alpha-1",))

    assert main([str(root)]) == 0


def test_main_exits_one_when_a_tree_is_found(tmp_path, capsys):
    root = _make_checkout(tmp_path)
    _plant(root)

    assert main([str(root)]) == 1
    assert JUNK_NAME in capsys.readouterr().out


def test_main_spawns_no_child_process(tmp_path, monkeypatch):
    """The retired watcher's own joke, structurally foreclosed.

    On 2026-08-17 the watcher wrote a %SystemDrive% tree itself: it was run
    under `env -i` with the MSIX python and its cwd was a checkout root, so it
    reproduced the exact condition it existed to detect. The preconditions are
    conjunctive and all three involve a CHILD PROCESS -- so a sweep that never
    spawns one cannot be its own suspect, regardless of how it is invoked.
    """
    import subprocess

    root = _make_checkout(tmp_path)
    _plant(root)

    def _forbidden(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("the sweep must not spawn a child process")

    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr(subprocess, "run", _forbidden)

    assert main([str(root)]) == 1


# --- durable record ------------------------------------------------------
#
# Under a scheduled task the sweep runs with pythonw and no console, so stdout
# goes nowhere. Without a durable record an unattended sweep would notice a
# tree and then lose the sighting -- reproducing, by a different route, the
# exact "nobody looked in time" failure that retiring the watcher was meant to
# fix. The log IS the delivery mechanism, not a nicety.


def test_a_sighting_is_appended_to_the_log(tmp_path):
    root = _make_checkout(tmp_path)
    _plant(root)
    log = tmp_path / "logs" / "sweep.jsonl"

    assert main([str(root), "--log", str(log)]) == 1

    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1, lines
    record = json.loads(lines[0])
    assert record["path"] == str(root / JUNK_NAME)
    assert record["root"] == str(root)


def test_the_log_record_carries_the_attributability_verdict(tmp_path):
    """The one field that decides what a reader does next."""
    root = _make_checkout(tmp_path)
    _plant(root)
    log = tmp_path / "sweep.jsonl"

    main([str(root), "--log", str(log)])

    record = json.loads(log.read_text(encoding="utf-8").strip())
    assert record["attributable"] is True
    assert "mtime" in record and "at" in record


def test_a_clean_sweep_writes_no_log_line(tmp_path):
    """A daily no-op must not grow the log.

    A line per clean run would bury the one line that matters under a year of
    "nothing here", which is how a durable record stops being read.
    """
    root = _make_checkout(tmp_path, worktrees=("alpha-1",))
    log = tmp_path / "sweep.jsonl"

    assert main([str(root), "--log", str(log)]) == 0
    assert not log.exists()


def test_appending_is_additive_across_runs(tmp_path):
    root = _make_checkout(tmp_path)
    _plant(root)
    log = tmp_path / "sweep.jsonl"

    main([str(root), "--log", str(log)])
    main([str(root), "--log", str(log)])

    assert len(log.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_an_unwritable_log_does_not_fail_the_sweep(tmp_path, capsys):
    """A diagnostic must never take down the thing it is diagnosing.

    Same rule the retired watcher's write_record() followed. The exit code
    must still report the sighting even when the record cannot be persisted,
    and the failure has to be visible rather than swallowed.
    """
    root = _make_checkout(tmp_path)
    _plant(root)
    # A path whose PARENT is a regular file: mkdir(parents=True) cannot
    # succeed, so this exercises the real OSError path rather than a mock.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")

    assert main([str(root), "--log", str(blocker / "sub" / "sweep.jsonl")]) == 1
    assert "could not write" in capsys.readouterr().err.lower()


def test_log_path_defaults_under_the_hermes_logs_dir():
    """Matches the sibling guards on this box (~/.hermes/logs/*.jsonl)."""
    p = log_path()
    assert p.parent.name == "logs"
    assert p.parent.parent.name == ".hermes"
    assert p.name.endswith(".jsonl")


def test_append_log_is_a_no_op_for_an_empty_sighting_list(tmp_path):
    log = tmp_path / "sweep.jsonl"
    append_log(log, [], now=time.time())
    assert not log.exists()


def test_main_without_an_explicit_log_uses_the_default_path(tmp_path):
    """Pins the fallback that leaked into the real log.

    main() resolves log_path() when --log is absent. The autouse fixture above
    redirects that, so this asserts the FALLBACK IS TAKEN (rather than silently
    writing nowhere) while proving it lands on the redirected path and not on
    ~/.hermes/logs/. Without this, someone could "fix" a future pollution bug by
    deleting the fallback and no test would notice.
    """
    root = _make_checkout(tmp_path)
    _plant(root)

    assert main([str(root)]) == 1

    redirected = tmp_path / "redirected" / "systemdrive-sweep.jsonl"
    assert redirected.exists(), "main() did not use log_path() as its fallback"
    assert json.loads(redirected.read_text(encoding="utf-8").strip())["path"] == str(
        root / JUNK_NAME
    )


def test_no_log_suppresses_the_record_entirely(tmp_path):
    root = _make_checkout(tmp_path)
    _plant(root)

    assert main([str(root), "--no-log"]) == 1

    assert not (tmp_path / "redirected" / "systemdrive-sweep.jsonl").exists()
