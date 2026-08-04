"""Trajectory JSONL must not be dropped inside a git work tree.

``save_trajectory`` is called from ``finalize_turn`` with a *relative* default
filename, so it resolves against whatever CWD the agent was launched in —
routinely a source checkout. That left a full verbatim transcript (message
text, tool results, tool-call arguments) next to the user's source as an
untracked file, one ``git add -A`` away from being published. See #77472
(cluster R-DUMP).

The contract these assert:

* a relative write whose target is inside a git work tree is *relocated* under
  ``<HERMES_HOME>/trajectories/<work-tree>/``, byte-for-byte intact —
  trajectories are training data, so the fix must never drop or truncate one;
* the ``<work-tree>`` component is per-repository, so two checkouts keep two
  datasets instead of merging irreversibly into one file;
* nothing is left behind in the repo;
* a CWD outside any git work tree is untouched;
* CWD placement stays reachable, via an absolute path or the
  ``agent.trajectory_allow_git_cwd`` config opt-out;
* when the destination cannot be established safely the write is *skipped*
  rather than falling back into the checkout, and never raises;
* the user is told — on the terminal, not only in ``errors.log``.

These exercise the real ``save_trajectory`` → ``open()`` path with real file
I/O against a temp HERMES_HOME and a real ``git init`` repo. Nothing on the
write path is mocked.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

import agent.trajectory as trajectory
from agent.trajectory import resolve_trajectory_path, save_trajectory

# A transcript with content that must survive the relocation verbatim: the
# whole point of a trajectory is full fidelity for training.
SAMPLE = [
    {"from": "human", "value": "deploy with token ghp_EXAMPLENOTREAL"},
    {"from": "gpt", "value": "<think>reasoning kept</think>done — 日本語 & \"quotes\""},
]


@pytest.fixture(autouse=True)
def _reset_warn_dedupe():
    """The redirect warning dedupes per process; keep tests independent."""
    trajectory._REDIRECT_WARNED.clear()
    yield
    trajectory._REDIRECT_WARNED.clear()


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a temp dir with no config.yaml (default config)."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _git_repo(root: Path) -> Path:
    """Create a real git work tree at *root* with one committed file."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
         "commit", "-qm", "init"],
        cwd=root, check=True,
    )
    return root


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _tracked_and_untracked(root: Path) -> str:
    """Everything git would offer to commit — the actual exposure."""
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=root,
        capture_output=True, text=True, check=True,
    ).stdout


def _would_stage(root: Path) -> str:
    """What ``git add -A`` would actually stage — exposure, not mere presence.

    A file physically inside a work tree but git-ignored is not committable,
    so ``rglob`` presence alone overstates the risk. This is the real test.
    """
    return subprocess.run(
        ["git", "add", "-An", "--", "."], cwd=root,
        capture_output=True, text=True,
    ).stdout


def _landed(hermes_home: Path, name: str = "trajectory_samples.jsonl") -> Path:
    """Find the single relocated dataset, without hardcoding the work-tree key.

    The key is ``<basename>-<sha256[:8]>``; asserting the literal digest would
    be a change-detector. What matters is that exactly one dataset exists and
    it sits under ``trajectories/``.
    """
    base = hermes_home / "trajectories"
    hits = sorted(p for p in base.rglob(name) if p.is_file())
    assert len(hits) == 1, f"expected exactly one {name} under {base}, got {hits}"
    return hits[0]


def _break_the_destination(hermes_home: Path) -> None:
    """Make ``trajectories/`` impossible to create, deterministically.

    A plain ``chmod`` is not enough on a cold config cache: ``_allow_git_cwd()``
    → ``load_config()`` → ``ensure_hermes_home()`` re-creates the directory
    skeleton and *repairs* the mode to 0700, so the write then succeeds.
    (Worth knowing on its own — a merely read-only home often self-heals.)
    Putting a regular *file* where ``trajectories/`` must be makes ``mkdir``
    fail with ENOTDIR no matter the ordering, which is the same failure class as
    a read-only or full disk and needs no permission games.
    """
    traj = hermes_home / "trajectories"
    if traj.is_dir():
        import shutil

        shutil.rmtree(traj)
    traj.write_text("not a directory\n", encoding="utf-8")


def _staged_jsonl(root: Path) -> list[str]:
    """Trajectory files git would offer to commit, ignoring test fixtures.

    Some fixtures (symlinks, scratch dirs) legitimately show in ``git status``;
    the exposure under test is specifically a transcript.
    """
    return [
        line for line in _tracked_and_untracked(root).splitlines()
        if ".jsonl" in line or ".json" in line
    ]


def test_relative_write_in_git_repo_lands_in_hermes_home(tmp_path, monkeypatch, hermes_home):
    """The default filename is relocated out of the checkout, intact."""
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo / "src")

    save_trajectory(SAMPLE, "kimi-k2", completed=True)

    landed = _landed(hermes_home)
    assert landed.exists(), "trajectory was not written to HERMES_HOME"

    # Nothing left in the repo, by any name.
    assert list(repo.rglob("*.jsonl")) == []
    assert _tracked_and_untracked(repo) == "", "trajectory is committable from the repo"

    # Full fidelity: the relocation must not drop or truncate the transcript.
    entries = _jsonl(landed)
    assert len(entries) == 1
    assert entries[0]["conversations"] == SAMPLE
    assert entries[0]["model"] == "kimi-k2"
    assert entries[0]["completed"] is True


def test_failed_trajectory_filename_also_relocated(tmp_path, monkeypatch, hermes_home):
    """The ``completed=False`` filename is the same class of leak."""
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)

    save_trajectory(SAMPLE, "kimi-k2", completed=False)

    landed = _landed(hermes_home, "failed_trajectories.jsonl")
    assert landed.exists()
    assert _jsonl(landed)[0]["completed"] is False
    assert _tracked_and_untracked(repo) == ""


def test_appends_accumulate_at_the_redirected_path(tmp_path, monkeypatch, hermes_home):
    """Repeated turns append to one relocated file, not one file per turn."""
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo / "src")

    save_trajectory(SAMPLE, "m1", completed=True)
    save_trajectory(SAMPLE, "m2", completed=True)

    entries = _jsonl(_landed(hermes_home))
    assert [e["model"] for e in entries] == ["m1", "m2"]


def test_non_git_cwd_is_left_alone(tmp_path, monkeypatch, hermes_home):
    """Outside a work tree there is nothing to protect — don't move the file.

    This is the common datagen shape (a scratch dir / a datagen box) and the
    guard must not disturb it.
    """
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    assert not (scratch / ".git").exists()
    monkeypatch.chdir(scratch)

    save_trajectory(SAMPLE, "kimi-k2", completed=True)

    assert (scratch / "trajectory_samples.jsonl").exists()
    assert not (hermes_home / "trajectories").exists()


def test_git_dir_as_file_still_redirects(tmp_path, monkeypatch, hermes_home):
    """A linked worktree / submodule carries ``.git`` as a *file*.

    A transcript is just as committable there, so an ``is_dir()`` check would
    miss a real leak.
    """
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: /elsewhere/.git/worktrees/linked\n", encoding="utf-8")
    monkeypatch.chdir(linked)

    save_trajectory(SAMPLE, "kimi-k2", completed=True)

    assert _landed(hermes_home).exists()
    assert list(linked.glob("*.jsonl")) == []


def test_subdirectory_deep_in_repo_redirects(tmp_path, monkeypatch, hermes_home):
    """The walk finds the root from any depth, not just the repo root."""
    repo = _git_repo(tmp_path / "repo")
    deep = repo / "src" / "a" / "b" / "c"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)

    save_trajectory(SAMPLE, "kimi-k2", completed=True)

    assert _landed(hermes_home).exists()
    assert _tracked_and_untracked(repo) == ""


def test_absolute_path_inside_repo_is_honoured(tmp_path, monkeypatch, hermes_home):
    """An explicit absolute path is a deliberate choice, not an ambient default.

    This is the per-call escape hatch: a caller who really wants the file in a
    checkout can still put it there.
    """
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    explicit = repo / "src" / "wanted.jsonl"

    save_trajectory(SAMPLE, "kimi-k2", completed=True, filename=str(explicit))

    assert explicit.exists(), "an absolute path must not be redirected"
    assert _jsonl(explicit)[0]["conversations"] == SAMPLE
    assert not (hermes_home / "trajectories").exists()


def test_config_opt_out_restores_cwd_write(tmp_path, monkeypatch, hermes_home):
    """``agent.trajectory_allow_git_cwd: true`` restores the old behaviour.

    Written as a real config.yaml under the temp HERMES_HOME and read through
    the real loader, so this covers the config plumbing too.
    """
    (hermes_home / "config.yaml").write_text(
        "agent:\n  trajectory_allow_git_cwd: true\n", encoding="utf-8"
    )
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo / "src")

    save_trajectory(SAMPLE, "kimi-k2", completed=True)

    assert (repo / "src" / "trajectory_samples.jsonl").exists()
    assert not (hermes_home / "trajectories").exists()


def test_config_default_is_redirect(tmp_path, monkeypatch, hermes_home):
    """An explicit ``false`` and an absent key must behave the same."""
    (hermes_home / "config.yaml").write_text(
        "agent:\n  trajectory_allow_git_cwd: false\n", encoding="utf-8"
    )
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)

    save_trajectory(SAMPLE, "kimi-k2", completed=True)

    assert _landed(hermes_home).exists()
    assert _tracked_and_untracked(repo) == ""


def test_documented_default_matches_the_shipped_default():
    """``DEFAULT_CONFIG`` is what the read path resolves through.

    ``load_config()`` deep-merges ``DEFAULT_CONFIG`` at read time, so the key
    must be declared there for the documented default to be the effective one
    — that deep-merge is the whole reason to declare it. (It is *not* so
    `hermes update` lists it: ``get_missing_config_fields()`` calls
    ``load_config()``, which merges ``DEFAULT_CONFIG`` first, so a declared key
    is never reported missing.) Asserts the contract between the two, not a
    snapshot of the value.
    """
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["agent"]["trajectory_allow_git_cwd"] is False
    assert trajectory._allow_git_cwd() is False


def test_relative_subdir_is_preserved(tmp_path, monkeypatch, hermes_home):
    """A relative subdir is kept inside the per-work-tree directory."""
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)

    save_trajectory(SAMPLE, "m", completed=True, filename="out/run1.jsonl")

    landed = _landed(hermes_home, "run1.jsonl")
    assert landed.parent.name == "out", f"subdir not preserved: {landed}"
    assert landed.parent.parent.parent == hermes_home / "trajectories"
    assert _tracked_and_untracked(repo) == ""


def test_dotdot_inside_the_tree_is_contained(tmp_path, monkeypatch, hermes_home):
    """A ``..`` path still inside the work tree is redirected, and flattened.

    Flattening matters: joining the raw ``..`` onto the trajectories dir would
    let the write climb back out of it.
    """
    repo = _git_repo(tmp_path / "repo")
    nested = repo / "src" / "a"
    nested.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(nested)

    # ../up.jsonl -> repo/src/up.jsonl: still in the tree, so still a leak.
    save_trajectory(SAMPLE, "m", completed=True, filename="../up.jsonl")

    base = (hermes_home / "trajectories").resolve()
    landed = _landed(hermes_home, "up.jsonl")
    assert landed.exists(), "redirected write vanished"
    assert base in landed.resolve().parents, "write escaped the trajectories dir"
    assert list(repo.rglob("*.jsonl")) == []
    assert _tracked_and_untracked(repo) == ""


def test_dotdot_resolving_outside_the_tree_is_left_alone(tmp_path, monkeypatch, hermes_home):
    """A relative path that lands outside any work tree needs no protection.

    The guard keys off the *resolved* target, not merely the CWD, so pointing
    out of the checkout is honoured rather than second-guessed.
    """
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo / "src")

    # ../../outside.jsonl -> tmp_path/outside.jsonl, which is not in a repo.
    save_trajectory(SAMPLE, "m", completed=True, filename="../../outside.jsonl")

    assert (tmp_path / "outside.jsonl").exists()
    assert not (hermes_home / "trajectories").exists()


def test_warning_names_the_destination_and_dedupes(tmp_path, monkeypatch, hermes_home, caplog):
    """The user must be able to find their training data, without log spam."""
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)

    with caplog.at_level("WARNING", logger="agent.trajectory"):
        save_trajectory(SAMPLE, "m", completed=True)
        save_trajectory(SAMPLE, "m", completed=True)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, "redirect warning should fire once per destination"
    msg = warnings[0].getMessage()
    assert str(_landed(hermes_home)) in msg
    assert "trajectory_allow_git_cwd" in msg, "warning must name the opt-out"


def test_resolver_is_pure_for_absolute_and_non_git(tmp_path, monkeypatch, hermes_home):
    """``resolve_trajectory_path`` returns its input when there's nothing to do."""
    scratch = tmp_path / "plain"
    scratch.mkdir()
    monkeypatch.chdir(scratch)
    assert resolve_trajectory_path("t.jsonl") == "t.jsonl"

    abs_path = str(tmp_path / "anywhere.jsonl")
    assert resolve_trajectory_path(abs_path) == abs_path


def test_run_agent_save_sample_uses_the_same_guard(tmp_path, monkeypatch, hermes_home):
    """``run_agent.py --save_sample`` writes the same content class to the CWD.

    It builds its payload from ``_convert_to_trajectory_format`` exactly like
    ``save_trajectory`` does, under a relative ``sample_<uuid>.json``, so it is
    the same leak and must resolve through the same helper. Asserted on the
    resolver against a real repo rather than by driving the fire CLI (which
    needs a live model).
    """
    import run_agent

    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)

    resolved = Path(run_agent.resolve_trajectory_path("sample_deadbeef.json"))
    assert resolved.name == "sample_deadbeef.json"
    assert resolved.parent.parent == hermes_home / "trajectories", \
        f"--save_sample was not relocated under trajectories/: {resolved}"
    assert list(repo.rglob("*.json")) == []


# ── Fail closed, never into the checkout (review item 1) ────────────────────
#
# The pre-fix resolver returned the *original* filename from its broad
# ``except``, so any error on the way to deciding "is this inside a repo?"
# wrote the full transcript into the repo and said so only at ``debug``.
# Measured before the fix: an unreadable ancestor left ``?? locked/`` and a
# read-only HERMES_HOME left ``?? trajectory_samples.jsonl``. For a change
# whose entire purpose is keeping transcripts out of checkouts, the failure
# mode has to be "refuse and say so loudly", not "silently do the unsafe
# thing". Skipping is chosen over a fallback location because a fallback in
# /tmp is data the OS deletes and no pipeline reads — a silent loss dressed up
# as a save. What must never happen either way: raising, since this runs during
# turn finalization.


def test_unreadable_ancestor_does_not_write_into_the_repo(tmp_path, monkeypatch, hermes_home):
    """EACCES while deciding containment must not fall back to the CWD.

    ``Path.exists()`` swallows ENOENT/ENOTDIR but *not* EACCES, and with an
    unreadable ancestor ``os.getcwd()`` raises EACCES too — while a *relative*
    ``open()`` still succeeds, because it resolves against the process's CWD
    file descriptor and needs no path traversal. So "cannot determine" here
    genuinely coexists with "the write would still land in the repo".
    """
    repo = _git_repo(tmp_path / "repo")
    locked = repo / "locked"
    child = locked / "child"
    child.mkdir(parents=True)
    monkeypatch.chdir(child)

    os.chmod(locked, 0o000)
    try:
        # Must not raise: a trajectory save is a side effect of finalize_turn.
        save_trajectory(SAMPLE, "m", completed=True)
    finally:
        os.chmod(locked, 0o755)

    assert list(repo.rglob("*.jsonl")) == [], "transcript written inside the repo"
    assert _tracked_and_untracked(repo) == "", "transcript is committable from the repo"


def test_eacces_during_the_upward_walk_is_not_swallowed(tmp_path, monkeypatch, hermes_home):
    """The walk's own ``.exists()`` must not swallow EACCES.

    Distinct from the case above, where ``os.getcwd()`` fails first and the walk
    is never reached. Here the CWD is readable and only the *target's* parent is
    locked, so the guard genuinely depends on the loop raising: swallowing the
    error would walk past the unreadable directory, find no ``.git``, conclude
    "not in a repo", and return the in-repo filename.
    """
    repo = _git_repo(tmp_path / "repo")
    locked = repo / "locked"
    (locked / "child").mkdir(parents=True)
    monkeypatch.chdir(repo)

    os.chmod(locked, 0o000)
    try:
        assert resolve_trajectory_path("locked/child/t.jsonl") is None, \
            "an unreadable ancestor must not be read as 'not in a repo'"
        save_trajectory(SAMPLE, "m", completed=True, filename="locked/child/t.jsonl")
    finally:
        os.chmod(locked, 0o755)

    assert list(repo.rglob("*.jsonl")) == [], "transcript written inside the repo"
    assert _staged_jsonl(repo) == []


def test_unwritable_hermes_home_does_not_write_into_the_repo(tmp_path, monkeypatch, hermes_home):
    """The plausible case: a read-only or full HERMES_HOME.

    ``mkdir`` of ``trajectories/`` raises, and the pre-fix code answered that by
    writing the transcript into the checkout instead.
    """
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)

    _break_the_destination(hermes_home)
    save_trajectory(SAMPLE, "m", completed=True)

    assert list(repo.rglob("*.jsonl")) == [], "transcript written inside the repo"
    assert _tracked_and_untracked(repo) == ""


def test_read_only_hermes_home_does_not_write_into_the_repo(tmp_path, monkeypatch, hermes_home):
    """The same, driven by a real permission denial rather than ENOTDIR.

    The config cache is warmed first so ``ensure_hermes_home()`` cannot repair
    the mode mid-flight — this is the ordering the reviewer measured, where the
    pre-fix code left ``?? trajectory_samples.jsonl`` in the repo.
    """
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)

    trajectory._allow_git_cwd()  # warm the config cache / ensure_hermes_home
    os.chmod(hermes_home, 0o500)  # r-x: cannot create trajectories/
    try:
        save_trajectory(SAMPLE, "m", completed=True)
    finally:
        os.chmod(hermes_home, 0o755)

    assert list(repo.rglob("*.jsonl")) == [], "transcript written inside the repo"
    assert _tracked_and_untracked(repo) == ""


def test_symlink_loop_does_not_write_into_the_repo(tmp_path, monkeypatch, hermes_home):
    """``Path.resolve()`` raises ``RuntimeError`` on a loop, not ``OSError``.

    The pre-fix inner handler caught only ``OSError``, so the loop escaped into
    the outer handler and returned the in-repo filename.

    The assertion is on the resolver's *decision*, not just on the absence of a
    file: here ELOOP happens to block the ``open()`` too, so "no file appeared"
    is true even unfixed. What was broken is that the guard answered "write it
    to the checkout" — the same wrong answer it gives in the EACCES case, where
    the write does land. Pin the decision and the bug class is covered.
    """
    repo = _git_repo(tmp_path / "repo")
    (repo / "a").symlink_to("b")
    (repo / "b").symlink_to("a")
    monkeypatch.chdir(repo)

    assert resolve_trajectory_path("a/t.jsonl") is None, \
        "an unresolvable path must be refused, not answered with the in-repo filename"

    trajectory._REDIRECT_WARNED.clear()
    save_trajectory(SAMPLE, "m", completed=True, filename="a/t.jsonl")

    assert list(repo.rglob("*.jsonl")) == []
    # The loop symlinks are the fixture's own; no transcript may be committable.
    assert _staged_jsonl(repo) == []


def test_skipped_save_is_reported_not_raised(tmp_path, monkeypatch, hermes_home, caplog):
    """A refusal must be loud, and must not propagate out of the save.

    ``finalize_turn`` guards ``_save_trajectory`` precisely because it is a
    fallible side effect; turning a skip into an exception would trade a leak
    for a broken turn.
    """
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    _break_the_destination(hermes_home)

    with caplog.at_level("WARNING", logger="agent.trajectory"):
        save_trajectory(SAMPLE, "m", completed=True)  # must not raise

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings, "a skipped trajectory save must be reported at WARNING"
    assert any("NOT saved" in r.getMessage() for r in warnings), \
        f"the notice must say the trajectory was not saved: {[r.getMessage() for r in warnings]}"


def test_opt_out_still_wins_when_containment_is_undetermined(tmp_path, monkeypatch, hermes_home):
    """``trajectory_allow_git_cwd: true`` means "my CWD, my choice" — always.

    Failing closed must not override an explicit opt-out, or the escape hatch
    would evaporate in exactly the broken-environment cases where a datagen run
    needs it.
    """
    (hermes_home / "config.yaml").write_text(
        "agent:\n  trajectory_allow_git_cwd: true\n", encoding="utf-8"
    )
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    (repo / "a").symlink_to("b")
    (repo / "b").symlink_to("a")

    assert resolve_trajectory_path("a/t.jsonl") == "a/t.jsonl"


def test_opt_out_is_unaffected_by_a_broken_destination(tmp_path, monkeypatch, hermes_home):
    """Failing closed must never strand a user who opted into CWD writes.

    With the opt-out on, HERMES_HOME is not on the path at all, so a broken one
    is irrelevant — datagen keeps working.
    """
    (hermes_home / "config.yaml").write_text(
        "agent:\n  trajectory_allow_git_cwd: true\n", encoding="utf-8"
    )
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    _break_the_destination(hermes_home)

    save_trajectory(SAMPLE, "m", completed=True)

    assert (repo / "trajectory_samples.jsonl").exists(), \
        "the opt-out stopped working when HERMES_HOME was broken"


# ── The destination must not be committable either (review item 2) ──────────
#
# ``HERMES_HOME`` is a documented knob and ``git init ~`` (yadm / dotfiles) is
# a real pattern, so the redirect target can itself sit inside a work tree.
# Measured before the fix: ``git add -A`` would stage
# ``.hermes/trajectories/trajectory_samples.jsonl`` — the warning's promise was
# simply false. Refusing to write there would break trajectory saving for every
# dotfiles user, so instead the destination is made *uncommittable* with a
# self-ignoring .gitignore, and the notice tells the truth either way.


def test_destination_inside_a_work_tree_is_not_committable(tmp_path, monkeypatch):
    """HERMES_HOME inside the project: relocated, and unstageable there.

    Presence inside the tree is not the exposure — ``git add -A`` staging it is.
    """
    repo = _git_repo(tmp_path / "myproject")
    home = repo / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (repo / "src" / "deep").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(repo / "src" / "deep")

    save_trajectory(SAMPLE, "m", completed=True)

    landed = _landed(home)
    assert landed.exists(), "trajectory vanished"
    assert _jsonl(landed)[0]["conversations"] == SAMPLE, "content must survive"
    assert "trajectory_samples.jsonl" not in _would_stage(repo), \
        "git add -A would stage the transcript from the destination"
    assert ".gitignore" not in _would_stage(repo), \
        "the protective .gitignore must not itself become a tracked file"


def test_home_as_work_tree_destination_is_not_committable(tmp_path, monkeypatch):
    """``git init ~`` (dotfiles): the default HERMES_HOME is in a work tree.

    The transcript moved out of the *project*, which is the fix's point, but it
    must not become committable in ``$HOME`` as a side effect.
    """
    fake_home = tmp_path / "home" / "dev"
    _git_repo(fake_home)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HOME", str(fake_home))

    project = _git_repo(tmp_path / "work" / "project")
    monkeypatch.chdir(project)

    save_trajectory(SAMPLE, "m", completed=True)

    assert _tracked_and_untracked(project) == "", "leaked into the project"
    assert "trajectory_samples.jsonl" not in _would_stage(fake_home), \
        "git add -A in $HOME would stage the transcript"


def test_notice_admits_when_the_destination_is_also_in_a_checkout(tmp_path, monkeypatch, caplog):
    """The warning must not promise more than it delivers.

    "so a full transcript is not left in your checkout" is misleading when the
    destination is in one too; the user needs to know, and needs the remedy.
    """
    repo = _git_repo(tmp_path / "proj")
    home = repo / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.chdir(repo)

    with caplog.at_level("WARNING", logger="agent.trajectory"):
        save_trajectory(SAMPLE, "m", completed=True)

    msg = " ".join(r.getMessage() for r in caplog.records)
    assert str(repo) in msg, "the notice must name the checkout the destination is in"
    assert "HERMES_HOME" in msg, "the notice must name the remedy"


def test_no_gitignore_side_effect_claim_when_destination_is_clean(tmp_path, monkeypatch, hermes_home, caplog):
    """The normal install must not be told about a problem it doesn't have."""
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)

    with caplog.at_level("WARNING", logger="agent.trajectory"):
        save_trajectory(SAMPLE, "m", completed=True)

    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "itself inside the checkout" not in msg, \
        f"spurious destination caveat on a clean HERMES_HOME: {msg}"


# ── The notice has to reach a terminal (review item 3) ──────────────────────
#
# ``hermes_logging.setup_logging`` installs no stderr StreamHandler unless
# --verbose, so ``logger.warning`` alone reached ``errors.log`` and nothing
# else. Measured before the fix: zero lines on the terminal, one in errors.log.


def test_redirect_notice_reaches_stderr_once(tmp_path, monkeypatch, hermes_home, capsys):
    """One line on the terminal, naming the destination, exactly once.

    Once per process, not per write: a datagen run saves every turn, and the
    dedupe is a contract of its own (see
    ``test_warning_names_the_destination_and_dedupes``).
    """
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)

    save_trajectory(SAMPLE, "m", completed=True)
    save_trajectory(SAMPLE, "m", completed=True)
    save_trajectory(SAMPLE, "m", completed=True)

    err = capsys.readouterr().err
    landed = _landed(hermes_home)
    assert str(landed) in err, f"destination never reached the terminal: {err!r}"
    assert err.count("Trajectory would have been written") == 1, \
        f"notice should appear once per process, got:\n{err}"
    # stderr, not stdout: piped trajectory data must stay clean.
    assert "Trajectory would have been written" not in capsys.readouterr().out


def test_status_line_destination_is_where_the_write_lands(tmp_path, monkeypatch, hermes_home):
    """``AIAgent(save_trajectories=True)`` is the path datagen uses.

    It printed "📝 Trajectory saving enabled" with no destination, so a
    redirected file was findable only by hunting through ``errors.log``. The
    contract: what the startup line reports is where the bytes actually go.
    Asserted on the helper the status block calls, because ``init_agent()``
    needs credentials and a live provider to construct.
    """
    from agent.trajectory import describe_trajectory_destination

    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)

    described = describe_trajectory_destination()
    save_trajectory(SAMPLE, "m", completed=True)
    landed = _landed(hermes_home)

    assert Path(described) == landed.parent, \
        f"status line says {described!r} but the write landed at {landed}"
    assert str(hermes_home) in described, "the reported destination is not under HERMES_HOME"


def test_status_line_names_the_cwd_when_nothing_is_redirected(tmp_path, monkeypatch, hermes_home):
    """Outside a checkout the line must name the real CWD file, not a bare name.

    "trajectory_samples.jsonl" alone is what the user already couldn't find.
    """
    from agent.trajectory import describe_trajectory_destination

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.chdir(scratch)

    described = describe_trajectory_destination()

    assert Path(described).is_absolute(), f"not an absolute location: {described!r}"
    assert Path(described) == scratch / "trajectory_samples.jsonl"


def test_status_line_admits_an_unavailable_destination(tmp_path, monkeypatch, hermes_home):
    """A skipped destination must be described, not reported as a real path."""
    from agent.trajectory import describe_trajectory_destination

    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    _break_the_destination(hermes_home)

    assert "unavailable" in describe_trajectory_destination()


# ── An existing pipeline must not go quietly stale (review item 4) ──────────
#
# The highest-severity finding: a pre-existing ./trajectory_samples.jsonl keeps
# its plausible content and simply stops growing, so a pipeline reads a frozen
# dataset forever. Worse than an empty one, which someone notices.
#
# The tension: the most discoverable place for a pointer is next to the stale
# file — inside the checkout — but creating a file there is the very thing this
# PR exists to stop, and the stale file is the user's data so it must not be
# moved or rewritten. Resolved by keeping the signal entirely out-of-band: a
# distinct, once-per-process notice on the terminal *and* in errors.log, naming
# both paths and all three remedies, plus the docs. Nothing in the checkout is
# created, moved, or modified.


def test_pre_existing_in_repo_dataset_is_reported(tmp_path, monkeypatch, hermes_home, capsys, caplog):
    """Upgrading with a dataset already in the checkout must be impossible to miss."""
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    stale = repo / "trajectory_samples.jsonl"
    stale.write_text(
        json.dumps({"conversations": SAMPLE, "model": "old", "completed": True}) + "\n",
        encoding="utf-8",
    )
    before = stale.read_bytes()

    with caplog.at_level("WARNING", logger="agent.trajectory"):
        save_trajectory(SAMPLE, "new-model", completed=True)

    err = capsys.readouterr().err
    logged = " ".join(r.getMessage() for r in caplog.records)
    for where in (err, logged):
        assert str(stale) in where, f"the stale dataset path was not named: {where!r}"
        assert "NOT receive new entries" in where, \
            f"the divergence was not stated plainly: {where!r}"

    # The user's data is untouched — not moved, not appended to, not rewritten.
    assert stale.read_bytes() == before, "the user's existing dataset was modified"

    # And nothing new was created in their checkout, which is the PR's point.
    assert _tracked_and_untracked(repo) == "?? trajectory_samples.jsonl\n", \
        f"the guard added something to the checkout: {_tracked_and_untracked(repo)!r}"

    # New entries went to the relocated dataset.
    assert [e["model"] for e in _jsonl(_landed(hermes_home))] == ["new-model"]


def test_stale_notice_is_silent_when_there_is_no_prior_dataset(tmp_path, monkeypatch, hermes_home, capsys):
    """A fresh install has no divergence, so it must not be warned about one."""
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)

    save_trajectory(SAMPLE, "m", completed=True)

    err = capsys.readouterr().err
    assert "NOT receive new entries" not in err, f"spurious staleness notice: {err!r}"


def test_empty_pre_existing_file_is_not_reported_as_a_dataset(tmp_path, monkeypatch, hermes_home, capsys):
    """A zero-byte leftover is not a dataset anyone is reading."""
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    (repo / "trajectory_samples.jsonl").write_text("", encoding="utf-8")

    save_trajectory(SAMPLE, "m", completed=True)

    assert "NOT receive new entries" not in capsys.readouterr().err


# ── Two repos keep two datasets (review item 5) ─────────────────────────────
#
# The pre-fix path was CWD-relative, so projA/ and projB/ each had their own
# dataset. A single flat file merges them irreversibly: the entry schema has no
# cwd/repo/session field, so afterwards the only discriminator is ``model``,
# often identical. Measured before the fix: one file, two entries, no way back.
#
# Chosen fix: key the *path* per work tree, not add a provenance field to the
# entry. The JSONL is a documented format consumed by training pipelines, so
# widening the schema has a much larger blast radius — and it would write repo
# paths into the dataset itself, which is a privacy regression in a PR about
# not leaking local context. Path keying also restores exactly what users had
# (one dataset per repo) and follows ``agent.moa_trace``'s per-session naming.


def test_two_repos_keep_separate_datasets(tmp_path, monkeypatch, hermes_home):
    """The merge has to be impossible, not merely documented."""
    repo_a = _git_repo(tmp_path / "project-alpha")
    repo_b = _git_repo(tmp_path / "project-beta")

    monkeypatch.chdir(repo_a)
    save_trajectory([{"from": "human", "value": "ALPHA"}], "m", completed=True)
    monkeypatch.chdir(repo_b)
    save_trajectory([{"from": "human", "value": "BETA"}], "m", completed=True)

    found = sorted((hermes_home / "trajectories").rglob("trajectory_samples.jsonl"))
    assert len(found) == 2, f"two repos must not share one dataset: {found}"

    by_content = {_jsonl(p)[0]["conversations"][0]["value"]: p for p in found}
    assert set(by_content) == {"ALPHA", "BETA"}
    assert len(_jsonl(by_content["ALPHA"])) == 1, "alpha's dataset picked up beta's entry"
    assert len(_jsonl(by_content["BETA"])) == 1, "beta's dataset picked up alpha's entry"


def test_same_basename_repos_do_not_collide(tmp_path, monkeypatch, hermes_home):
    """``~/a/proj`` and ``~/b/proj`` share a basename but are different repos.

    A basename-only key would silently merge them, which is the bug again.
    """
    left = _git_repo(tmp_path / "a" / "proj")
    right = _git_repo(tmp_path / "b" / "proj")

    monkeypatch.chdir(left)
    save_trajectory([{"from": "human", "value": "LEFT"}], "m", completed=True)
    monkeypatch.chdir(right)
    save_trajectory([{"from": "human", "value": "RIGHT"}], "m", completed=True)

    found = sorted((hermes_home / "trajectories").rglob("trajectory_samples.jsonl"))
    assert len(found) == 2, f"same-basename repos collided: {found}"


def test_same_relative_subdir_in_two_repos_does_not_collide(tmp_path, monkeypatch, hermes_home):
    """The claim the old comment made, now actually true.

    Pre-fix, ``out/run.jsonl`` from two repos landed in one
    ``trajectories/out/run.jsonl`` — measured, merged.
    """
    repo_a = _git_repo(tmp_path / "proj-a")
    repo_b = _git_repo(tmp_path / "proj-b")

    monkeypatch.chdir(repo_a)
    save_trajectory([{"from": "human", "value": "A"}], "m", completed=True, filename="out/run.jsonl")
    monkeypatch.chdir(repo_b)
    save_trajectory([{"from": "human", "value": "B"}], "m", completed=True, filename="out/run.jsonl")

    found = sorted((hermes_home / "trajectories").rglob("out/run.jsonl"))
    assert len(found) == 2, f"same subdir in two repos still collides: {found}"
    assert {_jsonl(p)[0]["conversations"][0]["value"] for p in found} == {"A", "B"}


def test_one_repo_keeps_one_dataset_across_subdirectories(tmp_path, monkeypatch, hermes_home):
    """Per *work tree*, not per CWD: one project is one dataset.

    Runs from ``repo/`` and ``repo/src/deep/`` are the same project and must
    accumulate together, or the guard would fragment a dataset instead.
    """
    repo = _git_repo(tmp_path / "repo")
    deep = repo / "src" / "deep"
    deep.mkdir(parents=True, exist_ok=True)

    monkeypatch.chdir(repo)
    save_trajectory(SAMPLE, "from-root", completed=True)
    monkeypatch.chdir(deep)
    save_trajectory(SAMPLE, "from-deep", completed=True)

    landed = _landed(hermes_home)
    assert [e["model"] for e in _jsonl(landed)] == ["from-root", "from-deep"]


def test_work_tree_key_is_stable_and_filesystem_safe(tmp_path):
    """Same repo → same directory every process; no path separators leak in.

    Stability is what makes appends accumulate across runs; a key derived from
    anything per-process would scatter one project across many datasets.
    """
    repo = tmp_path / "My Repo (v2)"
    repo.mkdir()
    first = trajectory._work_tree_key(repo)
    assert first == trajectory._work_tree_key(repo), "key is not stable"
    assert trajectory._work_tree_key(tmp_path / "other") != first
    assert "/" not in first and "\\" not in first and " " not in first
    assert first.strip(". ") == first, "key must not start/end with dot or space (Windows)"


def test_entry_schema_is_unchanged(tmp_path, monkeypatch, hermes_home):
    """No provenance field was added to the documented format.

    Keying the path was chosen precisely to avoid widening a schema that
    training pipelines consume — and to keep repo paths out of the dataset.
    """
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)

    save_trajectory(SAMPLE, "m", completed=True)

    entry = _jsonl(_landed(hermes_home))[0]
    assert set(entry) == {"conversations", "timestamp", "model", "completed"}
    assert str(repo) not in json.dumps(entry), "a repo path leaked into the dataset"


# ── Item 7 nits ─────────────────────────────────────────────────────────────


def test_notice_does_not_claim_a_work_tree_that_may_not_exist(tmp_path, monkeypatch, hermes_home, caplog):
    """A stray ``.git`` redirects (safe direction) — but isn't a work tree.

    Naming "the git work tree at X" for a path that git would not recognise is
    a lie in exactly the case the user has to reason about.
    """
    fake = tmp_path / "not-really-a-repo"
    fake.mkdir()
    (fake / ".git").write_text("this is not a gitdir pointer\n", encoding="utf-8")
    monkeypatch.chdir(fake)

    with caplog.at_level("WARNING", logger="agent.trajectory"):
        save_trajectory(SAMPLE, "m", completed=True)

    assert _landed(hermes_home).exists(), "a stray .git should still redirect"
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "git work tree at" not in msg, f"notice claims a work tree that may not exist: {msg}"
    assert ".git found at" in msg, f"notice should state what was actually observed: {msg}"


def test_git_env_vars_are_honoured(tmp_path, monkeypatch, hermes_home):
    """``GIT_DIR`` alone makes the CWD the work tree — verified with git itself.

    With only ``GIT_DIR`` set, ``git rev-parse --show-toplevel`` reports the CWD
    and ``git add`` stages a file there, so a wrapper or CI runner driving a
    repo this way from a ``.git``-less directory got no protection at all.
    """
    repo = _git_repo(tmp_path / "repo")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.chdir(scratch)

    # Sanity-check the premise against real git rather than assuming it.
    toplevel = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=scratch,
        env={**os.environ, "GIT_DIR": str(repo / ".git")},
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert Path(toplevel).resolve() == scratch.resolve()

    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    save_trajectory(SAMPLE, "m", completed=True)

    assert _landed(hermes_home).exists(), "GIT_DIR-driven work tree got no protection"
    assert list(scratch.glob("*.jsonl")) == []


def test_git_work_tree_env_var_is_honoured(tmp_path, monkeypatch, hermes_home):
    """``GIT_WORK_TREE`` names the work tree outright."""
    repo = _git_repo(tmp_path / "repo")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.chdir(scratch)
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(scratch))

    save_trajectory(SAMPLE, "m", completed=True)

    assert _landed(hermes_home).exists()
    assert list(scratch.glob("*.jsonl")) == []


def test_interior_dotdot_keeps_the_subdirectory(tmp_path, monkeypatch, hermes_home):
    """``a/../b/keep.jsonl`` must keep ``b/``, not silently drop it.

    ``".." not in path.parts`` flattened the whole path to its basename, so a
    caller's requested subdirectory vanished without a word.
    """
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)

    save_trajectory(SAMPLE, "m", completed=True, filename="a/../b/keep.jsonl")

    landed = _landed(hermes_home, "keep.jsonl")
    assert landed.parent.name == "b", f"interior '..' dropped the subdir: {landed}"


def test_escaping_dotdot_is_still_contained(tmp_path, monkeypatch, hermes_home):
    """Normalizing interior ``..`` must not become an escape hatch.

    Two distinct cases, both correct:

    * a ``..`` chain that resolves *outside* any work tree needs no protection
      and is honoured as-is (there is no checkout to leak into);
    * a ``..`` path that is still inside the tree is redirected, and must land
      under ``trajectories/`` rather than climbing back out of it.
    """
    repo = _git_repo(tmp_path / "repo")
    nested = repo / "src" / "a"
    nested.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(nested)

    # Escapes every repo -> left alone, by design (verified: git_root is None).
    outside = resolve_trajectory_path("../../../../../../tmp/pwned.jsonl")
    assert outside == "../../../../../../tmp/pwned.jsonl"

    # Still inside the tree -> redirected, and contained.
    base = (hermes_home / "trajectories").resolve()
    for candidate in ("../up.jsonl", "a/../../b/keep.jsonl", "./x/../y.jsonl"):
        trajectory._REDIRECT_WARNED.clear()
        resolved = resolve_trajectory_path(candidate)
        assert resolved is not None, f"{candidate} was skipped unexpectedly"
        assert base in Path(resolved).resolve().parents, \
            f"{candidate} escaped trajectories/: {resolved}"


# ── Docs must match shipped behaviour (review item 6) ───────────────────────


# The reference pages must carry the full contract (destination + opt-out); the
# guide pages must at least not send a datagen user to a path that stops
# receiving entries, and must point at the reference for the rest.
DOCS_REFERENCE = (
    "website/docs/developer-guide/trajectory-format.md",
    "website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/developer-guide/trajectory-format.md",
)
DOCS_GUIDE = (
    "website/docs/guides/python-library.md",
    "website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/guides/python-library.md",
)
DOCS = DOCS_REFERENCE + DOCS_GUIDE


@pytest.mark.parametrize("rel", DOCS)
def test_docs_name_the_redirect_destination(rel):
    """These are the snippets a datagen user copies.

    English *and* the zh-Hans mirrors: a page that still says the file is in the
    working directory sends the reader to a path that stops receiving entries.
    """
    repo_root = Path(__file__).resolve().parents[2]
    text = (repo_root / rel).read_text(encoding="utf-8")
    assert "<HERMES_HOME>/trajectories/" in text, f"{rel} does not name the destination"


@pytest.mark.parametrize("rel", DOCS_REFERENCE)
def test_reference_docs_name_the_opt_out(rel):
    """The reference page is where the escape hatch has to be discoverable."""
    repo_root = Path(__file__).resolve().parents[2]
    text = (repo_root / rel).read_text(encoding="utf-8")
    assert "trajectory_allow_git_cwd" in text, f"{rel} does not name the config key"


@pytest.mark.parametrize("rel", DOCS_REFERENCE)
def test_trajectory_format_doc_drops_the_plain_cwd_claim(rel):
    """"written to files in the current working directory" is now conditional.

    The unqualified claim was the line a datagen user trusted, and the upgrade
    path (a pre-existing dataset that stops growing) has to be written down —
    the terminal notice scrolls away, the docs don't.
    """
    repo_root = Path(__file__).resolve().parents[2]
    text = (repo_root / rel).read_text(encoding="utf-8")
    assert "Trajectories are written to files in the current working directory:" not in text
    assert "轨迹写入当前工作目录下的文件：" not in text
    assert ("Upgrading an existing pipeline" in text) or ("升级既有流水线" in text), \
        f"{rel} does not document the upgrade/staleness path"
