"""HER-110: ``hermes update`` must never destroy committed local history.

The updater used to fall back to ``git reset --hard origin/<branch>`` when
``git merge --ff-only origin/<branch>`` failed. On a checkout carrying
local-only commits (a committed local overlay), that fallback silently erased
them — dirty-file autostash only protects uncommitted changes.

The guard under test classifies the local history against the freshly
fetched ``origin/<branch>`` BEFORE any stash/checkout/merge:

- ``FAST_FORWARD_SAFE``   — local tip is an ancestor of ``origin/<branch>``
  (equal or strictly behind): the normal ff-only update proceeds.
- ``LOCAL_ONLY_OR_DIVERGED`` — local tip is NOT an ancestor: refuse,
  preserve HEAD/branch/worktree, exit non-zero.
- ``UNKNOWN`` — unresolvable refs, detached HEAD, or git errors: refuse
  fail-closed.

The separate post-pull syntax-guard rollback to the captured pre-pull SHA is
a *preserving* operation (it restores the exact local pre-update state) and
must keep working.

These tests drive the real ``_cmd_update_impl`` against real throwaway git
repositories; only the heavyweight non-git machinery (backups, gateway
pausing, dependency reinstall) is stubbed out. A subprocess spy records every
git invocation so the tests can prove no destructive mutation ran before the
guard's decision.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

from hermes_cli import update_cmd  # noqa: E402
import hermes_cli.main as hermes_main  # noqa: E402


# ── git helpers ──────────────────────────────────────────────────────────────

GIT_TEST_ENV = {
    "GIT_AUTHOR_NAME": "her110-test",
    "GIT_AUTHOR_EMAIL": "her110@example.invalid",
    "GIT_COMMITTER_NAME": "her110-test",
    "GIT_COMMITTER_EMAIL": "her110@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
}


def _git(cwd: Path, *argv: str) -> str:
    result = subprocess.run(
        ["git", *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, **GIT_TEST_ENV},
    )
    assert result.returncode == 0, (
        f"git {' '.join(argv)} failed in {cwd}:\n{result.stdout}\n{result.stderr}"
    )
    return result.stdout.strip()


def _head_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def _commit_file(repo: Path, relpath: str, content: str, message: str) -> str:
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(repo, "add", relpath)
    _git(repo, "commit", "-m", message)
    return _head_sha(repo)


def _init_update_repos(tmp_path: Path) -> tuple[Path, Path]:
    """Create an ``origin`` repo plus a ``local`` clone of it, on ``main``.

    ``cli.py`` is one of the updater's critical-path syntax-guard files, so
    the syntax-rollback scenario can break it in a remote commit.
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    (origin / "base.txt").write_text("v1\n", encoding="utf-8")
    (origin / "cli.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "c1: base")
    local = tmp_path / "local"
    _git(tmp_path, "clone", str(origin), str(local))
    return origin, local


# ── harness around _cmd_update_impl ──────────────────────────────────────────


class _GuardPassed(Exception):
    """Sentinel raised from a patched ``_invalidate_update_cache``.

    That call sits immediately after the guarded git phase on BOTH the
    already-up-to-date path and the successful-pull path, and before the
    heavyweight dependency/skill/gateway phases. Raising here (a) stops the
    test run at the right boundary and (b) guarantees the real cache
    invalidation never touches anything outside the test sandbox.
    """


def _run_update_impl(monkeypatch, repo: Path, *, branch: str = "main"):
    """Run the real ``_cmd_update_impl`` against *repo*; return (outcome, git calls)."""
    git_calls: list[list[str]] = []
    real_run = subprocess.run

    def spy(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)) and cmd and "git" in str(cmd[0]):
            git_calls.append([str(part) for part in cmd])
        return real_run(cmd, *args, **kwargs)

    # Point the updater at the throwaway clone.
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", repo)
    # Stub the non-git machinery that would touch the host system.
    monkeypatch.setattr(hermes_main, "_run_pre_update_backup", lambda args: None)
    monkeypatch.setattr(hermes_main, "_pause_windows_gateways_for_update", lambda: None)
    monkeypatch.setattr(
        hermes_main, "_resume_windows_gateways_after_update", lambda state: None
    )
    monkeypatch.setattr(
        hermes_main, "_sync_with_upstream_if_needed", lambda *a, **k: None
    )
    monkeypatch.setattr(update_cmd, "_discard_lockfile_churn", lambda *a, **k: None)
    monkeypatch.setattr(update_cmd, "_normalize_managed_eol", lambda *a, **k: None)

    import hermes_cli.config as hermes_config

    monkeypatch.setattr(hermes_config, "load_config", lambda: {})

    def _stop(*a, **k):
        raise _GuardPassed()

    monkeypatch.setattr(update_cmd, "_invalidate_update_cache", _stop)
    monkeypatch.setattr(subprocess, "run", spy)

    args = SimpleNamespace(branch=branch, yes=True, check=False)
    outcome = {"exit_code": None, "passed_guard": False}
    try:
        update_cmd._cmd_update_impl(args, gateway_mode=False)
    except _GuardPassed:
        outcome["passed_guard"] = True
    except SystemExit as exc:
        outcome["exit_code"] = exc.code
    return outcome, git_calls


def _git_tokens(cmd: list[str]) -> list[str]:
    """Strip the leading ``git`` (and any ``-c k=v`` pairs) from an argv."""
    toks = cmd[1:]
    while toks and toks[0] == "-c":
        toks = toks[2:]
    return toks


def _assert_no_local_mutation(git_calls: list[list[str]]) -> None:
    """No git command that can rewrite local commits/worktree may have run.

    ``fetch`` is allowed (it only refreshes remote-tracking refs); everything
    else must be read-only on a refused update.
    """
    forbidden = {"merge", "pull", "rebase", "stash", "checkout"}
    for cmd in git_calls:
        toks = _git_tokens(cmd)
        assert not (set(toks) & forbidden), f"mutating git command ran: {cmd}"
        if "reset" in toks:
            raise AssertionError(f"git reset ran on a refused update: {cmd}")


def _first_index(git_calls: list[list[str]], *needles: str) -> int:
    for i, cmd in enumerate(git_calls):
        toks = _git_tokens(cmd)
        if all(needle in toks for needle in needles):
            return i
    raise AssertionError(f"no git call matching {needles!r} in {git_calls!r}")


# ── unit tests: ancestry classification ──────────────────────────────────────


class TestClassifyUpdateAncestry:
    def test_local_behind_remote_is_fast_forward_safe(self, tmp_path):
        origin, local = _init_update_repos(tmp_path)
        _commit_file(origin, "base.txt", "v2\n", "c2: remote ahead")
        _git(local, "fetch", "origin", "main")
        classification, _ = update_cmd._classify_update_ancestry(
            ["git"], local, "main"
        )
        assert classification == update_cmd.UPDATE_ANCESTRY_FAST_FORWARD_SAFE

    def test_local_equal_remote_is_fast_forward_safe(self, tmp_path):
        origin, local = _init_update_repos(tmp_path)
        _git(local, "fetch", "origin", "main")
        classification, _ = update_cmd._classify_update_ancestry(
            ["git"], local, "main"
        )
        assert classification == update_cmd.UPDATE_ANCESTRY_FAST_FORWARD_SAFE

    def test_local_ahead_is_local_only(self, tmp_path):
        origin, local = _init_update_repos(tmp_path)
        _commit_file(local, "overlay.txt", "local overlay\n", "local-only commit")
        _git(local, "fetch", "origin", "main")
        classification, _ = update_cmd._classify_update_ancestry(
            ["git"], local, "main"
        )
        assert classification == update_cmd.UPDATE_ANCESTRY_LOCAL_ONLY_OR_DIVERGED

    def test_diverged_is_local_only_or_diverged(self, tmp_path):
        origin, local = _init_update_repos(tmp_path)
        _commit_file(origin, "base.txt", "v2\n", "remote side")
        _commit_file(local, "overlay.txt", "local overlay\n", "local side")
        _git(local, "fetch", "origin", "main")
        classification, _ = update_cmd._classify_update_ancestry(
            ["git"], local, "main"
        )
        assert classification == update_cmd.UPDATE_ANCESTRY_LOCAL_ONLY_OR_DIVERGED

    def test_missing_remote_ref_is_unknown(self, tmp_path):
        repo = tmp_path / "loner"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        (repo / "f.txt").write_text("x\n", encoding="utf-8")
        _git(repo, "add", "f.txt")
        _git(repo, "commit", "-m", "only local")
        classification, detail = update_cmd._classify_update_ancestry(
            ["git"], repo, "main"
        )
        assert classification == update_cmd.UPDATE_ANCESTRY_UNKNOWN
        assert detail  # must explain what could not be resolved

    def test_unresolvable_local_ref_is_unknown(self, tmp_path):
        origin, local = _init_update_repos(tmp_path)
        classification, _ = update_cmd._classify_update_ancestry(
            ["git"], local, "main", local_ref="refs/heads/no-such-branch"
        )
        assert classification == update_cmd.UPDATE_ANCESTRY_UNKNOWN

    def test_non_repo_cwd_is_unknown(self, tmp_path):
        not_a_repo = tmp_path / "empty"
        not_a_repo.mkdir()
        classification, _ = update_cmd._classify_update_ancestry(
            ["git"], not_a_repo, "main"
        )
        assert classification == update_cmd.UPDATE_ANCESTRY_UNKNOWN


# ── integration: _cmd_update_impl guard behavior ─────────────────────────────


class TestUpdateGuardIntegration:
    def test_clean_behind_fast_forwards(self, monkeypatch, tmp_path, capsys):
        origin, local = _init_update_repos(tmp_path)
        remote_tip = _commit_file(origin, "base.txt", "v2\n", "c2: remote ahead")

        outcome, git_calls = _run_update_impl(monkeypatch, local)

        out = capsys.readouterr().out
        assert outcome["passed_guard"], f"update did not reach the normal path:\n{out}"
        assert outcome["exit_code"] is None
        assert _head_sha(local) == remote_tip, "fast-forward should have advanced HEAD"
        assert "Update refused" not in out
        # The guard's ancestry probe must decide BEFORE the merge mutates HEAD.
        idx_guard = _first_index(git_calls, "merge-base", "--is-ancestor")
        idx_merge = _first_index(git_calls, "merge", "--ff-only")
        assert idx_guard < idx_merge

    def test_clean_equal_takes_up_to_date_path(self, monkeypatch, tmp_path, capsys):
        origin, local = _init_update_repos(tmp_path)
        before = _head_sha(local)

        outcome, _git_calls = _run_update_impl(monkeypatch, local)

        out = capsys.readouterr().out
        assert outcome["passed_guard"], f"update did not reach the normal path:\n{out}"
        assert _head_sha(local) == before
        assert "Update refused" not in out

    def test_local_ahead_refuses_without_mutation(self, monkeypatch, tmp_path, capsys):
        origin, local = _init_update_repos(tmp_path)
        local_tip = _commit_file(
            local, "overlay.txt", "local overlay\n", "local-only commit"
        )
        branch_before = _git(local, "rev-parse", "--abbrev-ref", "HEAD")

        outcome, git_calls = _run_update_impl(monkeypatch, local)

        out = capsys.readouterr().out
        assert outcome["exit_code"] == 1, f"expected refusal, got:\n{out}"
        assert not outcome["passed_guard"]
        assert _head_sha(local) == local_tip, "local commits must be preserved"
        assert _git(local, "rev-parse", "--abbrev-ref", "HEAD") == branch_before
        assert "local commits" in out and "preserved" in out
        assert "reset --hard" not in out  # no destructive recovery advice
        _assert_no_local_mutation(git_calls)

    def test_diverged_refuses_without_destructive_reset(
        self, monkeypatch, tmp_path, capsys
    ):
        origin, local = _init_update_repos(tmp_path)
        _commit_file(origin, "base.txt", "v2\n", "remote side")
        local_tip = _commit_file(
            local, "overlay.txt", "local overlay\n", "local side"
        )

        outcome, git_calls = _run_update_impl(monkeypatch, local)

        out = capsys.readouterr().out
        assert outcome["exit_code"] == 1, f"expected refusal, got:\n{out}"
        assert _head_sha(local) == local_tip, (
            "diverged local history must be preserved (this is the destructive "
            "reset the guard exists to prevent)"
        )
        assert "local commits" in out and "preserved" in out
        _assert_no_local_mutation(git_calls)

    def test_detached_head_fails_closed(self, monkeypatch, tmp_path, capsys):
        origin, local = _init_update_repos(tmp_path)
        _git(local, "checkout", "--detach")
        before = _head_sha(local)

        outcome, git_calls = _run_update_impl(monkeypatch, local)

        out = capsys.readouterr().out
        assert outcome["exit_code"] == 1, f"expected fail-closed refusal, got:\n{out}"
        assert _head_sha(local) == before
        assert "Update refused" in out
        _assert_no_local_mutation(git_calls)

    def test_dirty_behind_still_fast_forwards_with_stash(
        self, monkeypatch, tmp_path, capsys
    ):
        """Uncommitted changes on a fast-forward-safe checkout keep working:
        the guard must not regress the autostash flow."""
        origin, local = _init_update_repos(tmp_path)
        remote_tip = _commit_file(origin, "base.txt", "v2\n", "c2: remote ahead")
        (local / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")

        outcome, git_calls = _run_update_impl(monkeypatch, local)

        out = capsys.readouterr().out
        assert outcome["passed_guard"], f"update did not reach the normal path:\n{out}"
        assert _head_sha(local) == remote_tip
        # Guard decision must come before the disruptive stash creation.
        idx_guard = _first_index(git_calls, "merge-base", "--is-ancestor")
        idx_stash = _first_index(git_calls, "stash", "push")
        assert idx_guard < idx_stash
        # The dirty file is preserved: either still in tree or in the stash.
        stash_list = _git(local, "stash", "list")
        assert (local / "scratch.txt").exists() or stash_list

    def test_syntax_rollback_to_pre_pull_sha_still_runs(
        self, monkeypatch, tmp_path, capsys
    ):
        """The post-pull syntax guard's rollback restores the exact pre-pull
        state; it is a preserving reset and must NOT be blocked."""
        origin, local = _init_update_repos(tmp_path)
        pre_pull = _head_sha(local)
        _commit_file(origin, "cli.py", "def broken(:\n", "remote breaks cli.py")

        outcome, git_calls = _run_update_impl(monkeypatch, local)

        out = capsys.readouterr().out
        assert outcome["exit_code"] == 1, f"expected syntax-guard abort, got:\n{out}"
        assert "syntax error" in out
        assert _head_sha(local) == pre_pull, "rollback must restore the pre-pull SHA"
        # The rollback ran as reset --hard <pre_pull_sha> — never to origin/*.
        assert any(
            "reset" in _git_tokens(cmd) and pre_pull in _git_tokens(cmd)
            for cmd in git_calls
        ), f"pre-pull rollback did not run: {git_calls!r}"
        for cmd in git_calls:
            toks = _git_tokens(cmd)
            if "reset" in toks:
                assert not any(tok.startswith("origin/") for tok in toks), (
                    f"destructive reset to origin ran: {cmd}"
                )


# ── static proofs ────────────────────────────────────────────────────────────


def test_no_reset_hard_to_origin_remains_in_update_path():
    """The ``hermes update`` code path must contain no reset-to-origin at all —
    neither as an executed command nor as recovery advice printed to users."""
    for relpath in ("hermes_cli/update_cmd.py", "hermes_cli/main.py"):
        src = (REPO_ROOT / relpath).read_text(encoding="utf-8")
        assert '"--hard", f"origin/' not in src, relpath
        assert "reset --hard origin/" not in src, relpath


def test_no_force_escape_hatch_in_guard():
    """No config key or CLI flag may re-enable the destructive reset."""
    src = (REPO_ROOT / "hermes_cli" / "update_cmd.py").read_text(encoding="utf-8")
    guard_start = src.index("_classify_update_ancestry")
    guarded = src[guard_start:]
    for needle in ("allow_destructive", "force_reset", "destructive_reset"):
        assert needle not in guarded


def test_gateway_update_command_spawns_protected_cli_path():
    """Gateway ``/update`` shells out to ``hermes update --gateway``, which
    runs the same ``_cmd_update_impl`` (and thus the same guard)."""
    src = (REPO_ROOT / "gateway" / "slash_commands.py").read_text(encoding="utf-8")
    assert '"update", "--gateway"' in src or "update --gateway" in src
