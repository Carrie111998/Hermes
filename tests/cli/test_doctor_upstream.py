"""Tests for ``hermes_cli.doctor_upstream`` (READONLY diagnostics).

Every test sets up a temporary Git repository using an explicit helper; the
fixture never mutates the real repository. Tests are grouped by the required
behavior categories:

  - parser exposes ``--upstream``;
  - ordinary ``hermes doctor`` remains unchanged;
  - normal upstream topology;
  - ahead state;
  - behind state;
  - diverged state;
  - no upstream remote;
  - detached HEAD;
  - git command failure;
  - malformed/unknown state;
  - read-only guarantee.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_cli.doctor_upstream as du
from hermes_cli.doctor_upstream import (
    AheadBehind,
    BranchHealth,
    BranchHealthReport,
    DivergenceInfo,
    GitCallError,
    GitCommandForbidden,
    MutualPaths,
    READONLY_GIT_SUBCOMMANDS,
    SCOPE_PASS_MAX_COMMITS,
    SCOPE_PASS_MAX_FILES,
    SCOPE_WARN_MAX_COMMITS,
    SCOPE_WARN_MAX_FILES,
    ScopeHealth,
    TrackingInfo,
    UpdateBehavior,
    UpdateBehaviorProfile,
    UpdateSafetyDecision,
    UpstreamReference,
    UpstreamHealthResult,
    UpdateSafetyReport,
    aggregate_exit_code,
    classify_branch_health,
    collect_branch_health,
    render_compact,
    render_text,
    run_upstream_health,
    serialize_json,
    update_safety_check,
)

# --------------------------------------------------------------------------- #
# Helpers: temporary Git repo construction (used only by the test fixture).
# --------------------------------------------------------------------------- #


def _git(args: list[str], cwd: Path, *, env: dict[str, str] | None = None) -> str:
    """Build temporary Git repositories for the fixture.

    The module under test never goes through here — it uses ``_run_git``,
    which restricts to the ``READONLY_GIT_SUBCOMMANDS`` allowlist at the
    module level.
    """
    base_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    if env:
        base_env.update(env)
    p = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
        env=base_env,
    )
    return p.stdout


@pytest.fixture
def gitrepo(tmp_path: Path):
    """Build a temporary Git repository with an ``origin/main`` upstream."""
    cwd = tmp_path / "fixture"
    cwd.mkdir()
    _git(["init", "-q", "--initial-branch=main"], cwd)
    _git(["config", "user.email", "test@test"], cwd)
    _git(["config", "user.name", "test"], cwd)
    _git(["config", "commit.gpgsign", "false"], cwd)

    (cwd / "README.md").write_text("main\n")
    _git(["add", "."], cwd)
    _git(["commit", "-q", "-m", "init main"], cwd)

    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(["init", "--bare", "-q"], bare)
    _git(["remote", "add", "origin", str(bare)], cwd)
    _git(["push", "-q", "origin", "main"], cwd)
    main_sha = _git(["rev-parse", "HEAD"], cwd).strip()

    _git(["branch", "--set-upstream-to=origin/main"], cwd)

    return SimpleNamespace(
        cwd=cwd,
        bare=bare,
        main_sha=main_sha,
        git=_git,
    )


def _build_minimal_gitrepo(tmp_path: Path):
    """Reusable factory equivalent to the ``gitrepo`` fixture."""
    cwd = tmp_path / "fixture"
    cwd.mkdir()
    _git(["init", "-q", "--initial-branch=main"], cwd)
    _git(["config", "user.email", "test@test"], cwd)
    _git(["config", "user.name", "test"], cwd)
    _git(["config", "commit.gpgsign", "false"], cwd)

    (cwd / "README.md").write_text("main\n")
    _git(["add", "."], cwd)
    _git(["commit", "-q", "-m", "init main"], cwd)

    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(["init", "--bare", "-q"], bare)
    _git(["remote", "add", "origin", str(bare)], cwd)
    _git(["push", "-q", "origin", "main"], cwd)
    main_sha = _git(["rev-parse", "HEAD"], cwd).strip()

    _git(["branch", "--set-upstream-to=origin/main"], cwd)

    return SimpleNamespace(
        cwd=cwd,
        bare=bare,
        main_sha=main_sha,
        git=_git,
    )


# =========================================================================== #
# 1. Parser exposes --upstream
# =========================================================================== #


class TestParser:
    def test_parser_exposes_upstream_flag(self):
        from hermes_cli.subcommands.doctor import build_doctor_parser

        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        build_doctor_parser(sub, cmd_doctor=lambda a: None)
        args = parser.parse_args(["doctor"])
        assert args.upstream is False
        args = parser.parse_args(["doctor", "--upstream"])
        assert args.upstream is True

    def test_parser_exposes_json_and_compact_flags(self):
        from hermes_cli.subcommands.doctor import build_doctor_parser

        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        build_doctor_parser(sub, cmd_doctor=lambda a: None)
        args = parser.parse_args(["doctor", "--upstream", "--json"])
        assert args.json is True
        args = parser.parse_args(["doctor", "--upstream", "--compact"])
        assert args.compact is True


# =========================================================================== #
# 2. Ordinary doctor remains unchanged
# =========================================================================== #


class TestOrdinaryDoctorUnchanged:
    def test_ack_fast_path_still_reached(self, monkeypatch, capsys):
        """The --ack fast path must be unaffected by adding --upstream."""
        from hermes_cli.doctor import run_doctor

        with pytest.raises(SystemExit) as exc:
            run_doctor(SimpleNamespace(
                fix=False, ack="NOPE-NOT-A-REAL-ID", upstream=False,
                json=False, compact=False))
        assert exc.value.code == 2
        out = capsys.readouterr().out
        assert "Unknown advisory ID" in out

    def test_plain_doctor_never_enters_upstream_branch(self, monkeypatch):
        from hermes_cli.doctor import run_doctor
        import hermes_cli.doctor_upstream as _du

        def _boom(*a, **k):
            raise AssertionError("--upstream path ran without --upstream")

        monkeypatch.setattr(_du, "run_upstream_health", _boom)
        # --ack is reached before the heavy doctor workflow; using an invalid
        # id exits 2 without ever entering the upstream branch.
        with pytest.raises(SystemExit) as exc:
            run_doctor(SimpleNamespace(
                fix=False, ack="NOPE", upstream=False, json=False, compact=False))
        assert exc.value.code == 2

    def test_upstream_incompatible_with_fix(self, monkeypatch, capsys):
        from hermes_cli.doctor import run_doctor

        with pytest.raises(SystemExit) as exc:
            run_doctor(SimpleNamespace(
                fix=True, ack=None, upstream=True, json=False, compact=False))
        assert exc.value.code == 2
        out = capsys.readouterr().out
        assert "--upstream is incompatible" in out

    def test_upstream_incompatible_with_ack(self, monkeypatch, capsys):
        from hermes_cli.doctor import run_doctor

        with pytest.raises(SystemExit) as exc:
            run_doctor(SimpleNamespace(
                fix=False, ack="SEC-1", upstream=True, json=False, compact=False))
        assert exc.value.code == 2
        out = capsys.readouterr().out
        assert "--upstream is incompatible" in out


# =========================================================================== #
# 3. Normal upstream topology (synchronized)
# =========================================================================== #


def test_normal_upstream_synchronized_passes(gitrepo) -> None:
    result = run_upstream_health(cwd=str(gitrepo.cwd))
    assert result.branch_health.health == BranchHealth.PASS
    assert result.update_safety.decision == UpdateSafetyDecision.UPDATE_SAFETY_PASS
    assert result.update_safety.requires_manual_confirmation is False
    assert result.exit_code == 0
    assert result.branch_health.upstream.resolved is True
    assert result.branch_health.upstream.ref == "origin/main"


# =========================================================================== #
# 4. Ahead state
# =========================================================================== #


def test_ahead_state_confirms(gitrepo) -> None:
    (gitrepo.cwd / "ahead.txt").write_text("ahead\n")
    gitrepo.git(["add", "."], gitrepo.cwd)
    gitrepo.git(["commit", "-q", "-m", "local ahead"], gitrepo.cwd)

    result = run_upstream_health(cwd=str(gitrepo.cwd))
    assert result.branch_health.ahead_behind.ahead == 1
    assert result.branch_health.ahead_behind.behind == 0
    # Ahead-only -> PASS w/ confirmation.
    assert result.update_safety.decision == UpdateSafetyDecision.UPDATE_SAFETY_PASS
    assert result.update_safety.requires_manual_confirmation is True


# =========================================================================== #
# 5. Behind state
# =========================================================================== #


def _make_upstream_work_clone(gitrepo, tmp_path: Path) -> Path:
    """Clone the bare upstream into a separate worktree to advance ``origin``.

    ``-b main`` is required: the bare repo's HEAD points at the unset
    ``master`` by default, so a bare clone has no checked-out branch.
    """
    work = tmp_path / "upstream_work"
    _git(["clone", "-q", "-b", "main", str(gitrepo.bare), str(work)], tmp_path)
    _git(["config", "user.email", "up@test"], work)
    _git(["config", "user.name", "up"], work)
    return work


def test_behind_state_passes_no_confirmation(gitrepo, tmp_path: Path) -> None:
    # Advance upstream via a separate clone, then refresh local's
    # remote-tracking ref so local is strictly behind.
    work = _make_upstream_work_clone(gitrepo, tmp_path)
    (work / "behind.txt").write_text("behind\n")
    _git(["add", "."], work)
    _git(["commit", "-q", "-m", "upstream moves"], work)
    _git(["push", "-q", "origin", "main"], work)

    # Refresh the remote-tracking ref without touching local's worktree.
    gitrepo.git(["fetch", "-q", "origin"], gitrepo.cwd)

    result = run_upstream_health(cwd=str(gitrepo.cwd))
    assert result.branch_health.ahead_behind.ahead == 0
    assert result.branch_health.ahead_behind.behind >= 1
    assert result.update_safety.decision == UpdateSafetyDecision.UPDATE_SAFETY_PASS
    assert result.update_safety.requires_manual_confirmation is False
    assert result.exit_code == 0


# =========================================================================== #
# 6. Diverged state
# =========================================================================== #


def test_diverged_state_blocks(gitrepo, tmp_path: Path) -> None:
    # Local advance.
    (gitrepo.cwd / "local.txt").write_text("local\n")
    gitrepo.git(["add", "."], gitrepo.cwd)
    gitrepo.git(["commit", "-q", "-m", "local divergent"], gitrepo.cwd)

    # Remote advance on an unrelated path -> true divergence.
    work = _make_upstream_work_clone(gitrepo, tmp_path)
    (work / "remote.txt").write_text("remote\n")
    _git(["add", "."], work)
    _git(["commit", "-q", "-m", "remote divergent"], work)
    _git(["push", "-q", "origin", "main"], work)

    # Refresh remote-tracking ref so both sides are visible locally.
    gitrepo.git(["fetch", "-q", "origin"], gitrepo.cwd)

    result = run_upstream_health(cwd=str(gitrepo.cwd))
    assert result.branch_health.ahead_behind.ahead >= 1
    assert result.branch_health.ahead_behind.behind >= 1
    # Diverged + reset fallback -> BLOCKED.
    assert result.update_safety.decision == UpdateSafetyDecision.UPDATE_SAFETY_BLOCKED
    assert result.exit_code == 2


# =========================================================================== #
# 7. No upstream remote
# =========================================================================== #


def test_no_upstream_remote_returns_error(tmp_path: Path) -> None:
    cwd = tmp_path / "noremote"
    cwd.mkdir()
    _git(["init", "-q", "--initial-branch=main"], cwd)
    _git(["config", "user.email", "test@test"], cwd)
    _git(["config", "user.name", "test"], cwd)
    (cwd / "f.txt").write_text("x\n")
    _git(["add", "."], cwd)
    _git(["commit", "-q", "-m", "init"], cwd)

    result = run_upstream_health(cwd=str(cwd))
    assert result.branch_health.upstream.resolved is False
    assert result.branch_health.health == BranchHealth.ERROR
    assert result.exit_code == 1


# =========================================================================== #
# 8. Detached HEAD
# =========================================================================== #


def test_detached_head_baseline(gitrepo) -> None:
    gitrepo.git(["checkout", "-q", "HEAD^{commit}"], gitrepo.cwd)
    result = run_upstream_health(cwd=str(gitrepo.cwd))
    assert result.branch_health.branch == "HEAD"
    assert result.branch_health.health == BranchHealth.PASS
    assert result.exit_code == 0


# =========================================================================== #
# 9. Git command failure
# =========================================================================== #


def test_git_command_failure_not_a_repo(tmp_path: Path) -> None:
    bare_dir = tmp_path / "norepo"
    bare_dir.mkdir()
    result = run_upstream_health(cwd=str(bare_dir))
    assert result.exit_code == 1
    assert result.branch_health.raw_error is not None
    assert result.branch_health.health == BranchHealth.ERROR


def test_run_git_surfaces_structured_failure(tmp_path: Path) -> None:
    with pytest.raises(GitCallError) as exc:
        du._run_git(("rev-parse", "definitely-not-a-real-ref-xyz-zzz"),
                    cwd=str(tmp_path))
    assert exc.value.returncode != 0
    assert "git" in str(exc.value.argv[0])


# =========================================================================== #
# 10. Malformed / unknown state
# =========================================================================== #


def test_ahead_behind_malformed_returns_zero(gitrepo, monkeypatch) -> None:
    monkeypatch.setattr(
        du, "_safe_stdout", lambda fn, *a, **k: ("garbage-not-two-numbers\n", None))
    ab = du._ahead_behind(cwd=str(gitrepo.cwd), upstream_ref="origin/main")
    assert ab.ahead == 0
    assert ab.behind == 0


def test_missing_upstream_returns_error_no_crash(gitrepo) -> None:
    bh = BranchHealthReport(
        branch="some-branch",
        head_sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        head_short="deadbee",
        repo_root=str(gitrepo.cwd),
        health=BranchHealth.ERROR,
        reasons=["UH1: upstream reference unresolved"],
        upstream=UpstreamReference(False, None, None, None,
                                   resolution_chain=[],
                                   error="upstream reference not found"),
        tracking=TrackingInfo(False, None, None, None, "none"),
        ahead_behind=AheadBehind(0, 0),
        divergence=DivergenceInfo(None, None, None, None, None, None),
        mutual=MutualPaths([], [], [], []),
        scope=ScopeHealth(0, 0, 0, 0),
        raw_error="upstream reference not found",
    )
    safety = update_safety_check(bh)
    assert safety.decision == UpdateSafetyDecision.UPDATE_SAFETY_PASS
    assert aggregate_exit_code(bh, safety) == 1


def test_unknown_git_subcommand_forbidden() -> None:
    with pytest.raises(GitCommandForbidden):
        du._run_git(("some-unknown-command", "x"))


# =========================================================================== #
# 11. Read-only guarantee
# =========================================================================== #


def test_allowlist_is_closed_mutating_commands_forbidden() -> None:
    for sub in ("fetch", "pull", "merge", "rebase", "checkout",
                "switch", "reset", "stash", "push", "clean",
                "update-ref", "submodule", "am", "cherry-pick"):
        with pytest.raises(GitCommandForbidden):
            du._run_git((sub, "anything"))
    for sub in ("rev-parse", "rev-list", "merge-base", "diff",
                "show", "symbolic-ref", "config", "remote", "status"):
        assert sub in READONLY_GIT_SUBCOMMANDS


def test_render_text_never_echoes_mutating_command(gitrepo) -> None:
    bh = collect_branch_health(cwd=str(gitrepo.cwd))
    safety = update_safety_check(bh)
    result = UpstreamHealthResult(bh, safety, aggregate_exit_code(bh, safety))
    text = render_text(result)
    for forbidden in ("git pull", "git fetch", "git reset", "git merge"):
        assert forbidden not in text


def test_upstream_diagnostic_is_read_only_end_to_end(gitrepo) -> None:
    """Snapshot the repo before/after; a diagnostic run must not mutate it."""
    cwd = gitrepo.cwd

    def snapshot():
        head = _git(["rev-parse", "HEAD"], cwd).strip()
        branch = _git(["symbolic-ref", "--short", "HEAD"], cwd).strip()
        remotes = _git(["remote"], cwd).strip()
        status = _git(["status", "--porcelain"], cwd).strip()
        config = _git(["config", "--local", "--list"], cwd).strip()
        refs = _git(["for-each-ref"], cwd).strip()
        return (head, branch, remotes, status, config, refs)

    before = snapshot()
    result = run_upstream_health(cwd=str(cwd))
    after = snapshot()

    assert result.branch_health.health in (BranchHealth.PASS,
                                           BranchHealth.WARN,
                                           BranchHealth.ERROR)
    assert before == after


# =========================================================================== #
# JSON / compact / text renderers
# =========================================================================== #


def test_json_is_pure_object(gitrepo) -> None:
    bh = collect_branch_health(cwd=str(gitrepo.cwd))
    safety = update_safety_check(bh)
    result = UpstreamHealthResult(bh, safety, aggregate_exit_code(bh, safety))
    parsed = json.loads(serialize_json(result))
    assert isinstance(parsed, dict)
    assert "branch_health" in parsed
    assert "update_safety" in parsed
    assert "exit_code" in parsed


def test_render_compact_is_single_line_stable(gitrepo) -> None:
    bh = collect_branch_health(cwd=str(gitrepo.cwd))
    safety = update_safety_check(bh)
    result = UpstreamHealthResult(bh, safety, aggregate_exit_code(bh, safety))
    line = render_compact(result)
    assert "\n" not in line
    assert "health=" in line
    assert "safety=" in line
    assert "ahead=" in line
    assert "behind=" in line
    assert "behavior=" in line


def test_aggregate_exit_code_table() -> None:
    bh_pass = BranchHealthReport(
        branch="main",
        head_sha="x" * 40,
        head_short="x",
        repo_root=".",
        health=BranchHealth.PASS,
        reasons=[],
        upstream=UpstreamReference(True, "origin/main", "origin", "main"),
        tracking=TrackingInfo(True, "origin/main", "origin",
                              "refs/heads/main", "explicit"),
        ahead_behind=AheadBehind(0, 0),
        divergence=DivergenceInfo(None, None, None, None, None, None),
        mutual=MutualPaths([], [], [], []),
        scope=ScopeHealth(0, 0, 0, 0),
    )
    bh_error = BranchHealthReport(**{**bh_pass.__dict__, "health": BranchHealth.ERROR})
    bh_warn = BranchHealthReport(**{**bh_pass.__dict__, "health": BranchHealth.WARN})
    pass_safety = UpdateSafetyReport(
        decision=UpdateSafetyDecision.UPDATE_SAFETY_PASS,
        requires_manual_confirmation=False,
        confirmation_reason=None,
        behavior_name=UpdateBehavior.PULL_FF_ONLY_PLUS_RESET_HARD_FALLBACK,
        behavior_profile=du.CURRENT_UPDATE_BEHAVIOR,
        reasoning=[],
    )
    blocked = UpdateSafetyReport(
        decision=UpdateSafetyDecision.UPDATE_SAFETY_BLOCKED,
        requires_manual_confirmation=False,
        confirmation_reason=None,
        behavior_name=UpdateBehavior.PULL_FF_ONLY_PLUS_RESET_HARD_FALLBACK,
        behavior_profile=du.CURRENT_UPDATE_BEHAVIOR,
        reasoning=[],
    )
    assert aggregate_exit_code(bh_pass, pass_safety) == 0
    assert aggregate_exit_code(bh_warn, pass_safety) == 0
    assert aggregate_exit_code(bh_warn, blocked) == 2
    assert aggregate_exit_code(bh_error, pass_safety) == 1
    assert aggregate_exit_code(bh_error, blocked) == 2


def test_scope_thresholds_frozen() -> None:
    assert SCOPE_PASS_MAX_COMMITS == 5
    assert SCOPE_PASS_MAX_FILES == 20
    assert SCOPE_WARN_MAX_COMMITS == 20
    assert SCOPE_WARN_MAX_FILES == 100