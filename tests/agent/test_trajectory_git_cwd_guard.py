"""Trajectory JSONL must not be dropped inside a git work tree.

``save_trajectory`` is called from ``finalize_turn`` with a *relative* default
filename, so it resolves against whatever CWD the agent was launched in —
routinely a source checkout. That left a full verbatim transcript (message
text, tool results, tool-call arguments) next to the user's source as an
untracked file, one ``git add -A`` away from being published. See #77472
(cluster R-DUMP).

The contract these assert:

* a relative write whose target is inside a git work tree is *relocated* under
  ``<HERMES_HOME>/trajectories/``, byte-for-byte intact — trajectories are
  training data, so the fix must never drop or truncate one;
* nothing is left behind in the repo;
* a CWD outside any git work tree is untouched;
* CWD placement stays reachable, via an absolute path or the
  ``agent.trajectory_allow_git_cwd`` config opt-out.

These exercise the real ``save_trajectory`` → ``open()`` path with real file
I/O against a temp HERMES_HOME and a real ``git init`` repo. Nothing on the
write path is mocked.
"""

from __future__ import annotations

import json
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


def test_relative_write_in_git_repo_lands_in_hermes_home(tmp_path, monkeypatch, hermes_home):
    """The default filename is relocated out of the checkout, intact."""
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo / "src")

    save_trajectory(SAMPLE, "kimi-k2", completed=True)

    landed = hermes_home / "trajectories" / "trajectory_samples.jsonl"
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

    landed = hermes_home / "trajectories" / "failed_trajectories.jsonl"
    assert landed.exists()
    assert _jsonl(landed)[0]["completed"] is False
    assert _tracked_and_untracked(repo) == ""


def test_appends_accumulate_at_the_redirected_path(tmp_path, monkeypatch, hermes_home):
    """Repeated turns append to one relocated file, not one file per turn."""
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo / "src")

    save_trajectory(SAMPLE, "m1", completed=True)
    save_trajectory(SAMPLE, "m2", completed=True)

    entries = _jsonl(hermes_home / "trajectories" / "trajectory_samples.jsonl")
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

    assert (hermes_home / "trajectories" / "trajectory_samples.jsonl").exists()
    assert list(linked.glob("*.jsonl")) == []


def test_subdirectory_deep_in_repo_redirects(tmp_path, monkeypatch, hermes_home):
    """The walk finds the root from any depth, not just the repo root."""
    repo = _git_repo(tmp_path / "repo")
    deep = repo / "src" / "a" / "b" / "c"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)

    save_trajectory(SAMPLE, "kimi-k2", completed=True)

    assert (hermes_home / "trajectories" / "trajectory_samples.jsonl").exists()
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

    assert (hermes_home / "trajectories" / "trajectory_samples.jsonl").exists()
    assert _tracked_and_untracked(repo) == ""


def test_documented_default_matches_the_shipped_default():
    """``DEFAULT_CONFIG`` is what the read path resolves through.

    ``load_config()`` deep-merges ``DEFAULT_CONFIG`` at read time, so the key
    must be declared there for the documented default to be the effective one
    (and for `hermes update` to list it as a new option). Asserts the contract
    between the two, not a snapshot of the value.
    """
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["agent"]["trajectory_allow_git_cwd"] is False
    assert trajectory._allow_git_cwd() is False


def test_relative_subdir_is_preserved(tmp_path, monkeypatch, hermes_home):
    """A relative subdir is kept, so two callers' basenames can't collide."""
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)

    save_trajectory(SAMPLE, "m", completed=True, filename="out/run1.jsonl")

    assert (hermes_home / "trajectories" / "out" / "run1.jsonl").exists()
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
    landed = base / "up.jsonl"
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
    assert str(hermes_home / "trajectories" / "trajectory_samples.jsonl") in msg
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
    needs a live model), plus a wiring check that the CLI path calls it.
    """
    import inspect

    import run_agent

    repo = _git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)

    resolved = Path(run_agent.resolve_trajectory_path("sample_deadbeef.json"))
    assert resolved.parent == hermes_home / "trajectories"

    src = inspect.getsource(run_agent.main)
    assert "resolve_trajectory_path(f\"sample_" in src, \
        "--save_sample must route its filename through resolve_trajectory_path"
