"""No-destroy preflight for ``hermes update``.

A fetch may refresh remote refs, but local committed or uncommitted source work
must stop the update before lockfile cleanup, stash, checkout, pull, or reset.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import main as cli_main


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _checkout_with_remote(tmp_path: Path, *, single_branch: bool = False) -> Path:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    checkout = tmp_path / "checkout"

    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "init", "-b", "main", str(seed))
    (seed / "tracked.txt").write_text("upstream\n")
    (seed / "package-lock.json").write_text('{"lock":"upstream"}\n')
    _git(seed, "add", "tracked.txt", "package-lock.json")
    _git(seed, "commit", "-m", "initial")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")
    clone_args = ["clone", "--branch", "main"]
    if single_branch:
        clone_args.append("--single-branch")
    _git(tmp_path, *clone_args, str(remote), str(checkout))
    return checkout


def _update_args(*, yes: bool = False, gateway: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        backup=False,
        branch=None,
        force=False,
        force_venv=False,
        gateway=gateway,
        yes=yes,
    )


@pytest.mark.parametrize(
    ("yes", "gateway"),
    [(False, False), (True, False), (False, True)],
    ids=["interactive", "yes", "gateway"],
)
def test_uncommitted_work_stops_before_cleanup_stash_checkout_pull_or_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    yes: bool,
    gateway: bool,
) -> None:
    checkout = _checkout_with_remote(tmp_path)
    tracked = checkout / "tracked.txt"
    tracked.write_text("local edit\n")
    lockfile = checkout / "package-lock.json"
    lockfile.write_text('{"lock":"local edit"}\n')
    untracked = checkout / "notes.txt"
    untracked.write_text("keep me\n")

    monkeypatch.setattr(cli_main, "PROJECT_ROOT", checkout)
    monkeypatch.setattr(cli_main, "_is_windows", lambda: False)
    monkeypatch.setattr(cli_main, "_run_pre_update_backup", lambda _args: None)
    monkeypatch.setattr(cli_main, "_pause_windows_gateways_for_update", lambda: None)

    with (
        pytest.raises(SystemExit) as excinfo,
    ):
        cli_main._cmd_update_impl(
            _update_args(yes=yes, gateway=gateway), gateway_mode=gateway
        )

    assert excinfo.value.code == 2
    assert tracked.read_text() == "local edit\n"
    assert lockfile.read_text() == '{"lock":"local edit"}\n'
    assert untracked.read_text() == "keep me\n"
    assert _git(checkout, "status", "--porcelain").stdout
    output = capsys.readouterr().out
    assert "local work" in output.lower()
    assert "update stopped" in output.lower()


@pytest.mark.parametrize(
    ("yes", "gateway"),
    [(False, False), (True, False), (False, True)],
    ids=["interactive", "yes", "gateway"],
)
def test_committed_work_stops_before_cleanup_stash_checkout_pull_or_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    yes: bool,
    gateway: bool,
) -> None:
    checkout = _checkout_with_remote(tmp_path)
    tracked = checkout / "tracked.txt"
    tracked.write_text("local commit\n")
    _git(checkout, "add", "tracked.txt")
    _git(checkout, "commit", "-m", "local work")
    local_head = _git(checkout, "rev-parse", "HEAD").stdout.strip()

    monkeypatch.setattr(cli_main, "PROJECT_ROOT", checkout)
    monkeypatch.setattr(cli_main, "_is_windows", lambda: False)
    monkeypatch.setattr(cli_main, "_run_pre_update_backup", lambda _args: None)
    monkeypatch.setattr(cli_main, "_pause_windows_gateways_for_update", lambda: None)

    with (
        pytest.raises(SystemExit) as excinfo,
    ):
        cli_main._cmd_update_impl(
            _update_args(yes=yes, gateway=gateway), gateway_mode=gateway
        )

    assert excinfo.value.code == 2
    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == local_head
    assert tracked.read_text() == "local commit\n"
    output = capsys.readouterr().out
    assert "local work" in output.lower()
    assert "update stopped" in output.lower()


def test_pushed_commit_on_current_branch_is_not_treated_as_local(
    tmp_path: Path,
) -> None:
    checkout = _checkout_with_remote(tmp_path)
    _git(checkout, "checkout", "-b", "feature")
    (checkout / "feature.txt").write_text("published\n")
    _git(checkout, "add", "feature.txt")
    _git(checkout, "commit", "-m", "published feature")
    _git(checkout, "push", "-u", "origin", "feature")

    current_branch = cli_main._guard_update_local_work(["git"], checkout, "main")

    assert current_branch == "feature"


def test_pushed_branch_is_refreshed_in_a_single_branch_clone(tmp_path: Path) -> None:
    checkout = _checkout_with_remote(tmp_path, single_branch=True)
    _git(checkout, "checkout", "-b", "feature")
    (checkout / "feature.txt").write_text("published\n")
    _git(checkout, "add", "feature.txt")
    _git(checkout, "commit", "-m", "published feature")
    _git(checkout, "push", "-u", "origin", "feature")

    missing_ref = subprocess.run(
        ["git", "rev-parse", "--verify", "origin/feature"],
        cwd=checkout,
        capture_output=True,
        text=True,
    )
    assert missing_ref.returncode != 0

    current_branch = cli_main._guard_update_local_work(["git"], checkout, "main")

    assert current_branch == "feature"
    assert _git(checkout, "rev-parse", "origin/feature").stdout.strip() == _git(
        checkout, "rev-parse", "HEAD"
    ).stdout.strip()


def test_local_commit_on_target_branch_blocks_before_checkout(tmp_path: Path) -> None:
    checkout = _checkout_with_remote(tmp_path)
    _git(checkout, "checkout", "-b", "feature")
    _git(checkout, "push", "-u", "origin", "feature")
    (checkout / "feature.txt").write_text("local target commit\n")
    _git(checkout, "add", "feature.txt")
    _git(checkout, "commit", "-m", "local target work")
    target_head = _git(checkout, "rev-parse", "HEAD").stdout.strip()
    _git(checkout, "checkout", "main")

    with pytest.raises(SystemExit) as excinfo:
        cli_main._guard_update_local_work(["git"], checkout, "feature")

    assert excinfo.value.code == 2
    assert _git(checkout, "branch", "--show-current").stdout.strip() == "main"
    assert _git(checkout, "rev-parse", "feature").stdout.strip() == target_head


def test_updater_recovery_markers_do_not_block_clean_checkout(tmp_path: Path) -> None:
    checkout = _checkout_with_remote(tmp_path)
    (checkout / ".update-incomplete").write_text("started=1\n")
    (checkout / ".lazy-refresh-incomplete").write_text("started=1\n")

    current_branch = cli_main._guard_update_local_work(["git"], checkout, "main")

    assert current_branch == "main"


def test_status_probe_failure_stops_before_any_followup_git_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(cmd)
        if cmd[1:3] == ["status", "--porcelain"]:
            return SimpleNamespace(returncode=128, stdout="", stderr="broken repo")
        raise AssertionError(f"unexpected command after failed status probe: {cmd}")

    monkeypatch.setattr(cli_main.subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as excinfo:
        cli_main._guard_update_local_work(["git"], tmp_path, "main")

    assert excinfo.value.code == 2
    assert len(calls) == 1
    assert calls[0][1:3] == ["status", "--porcelain"]
    assert "could not verify local work" in capsys.readouterr().out.lower()


def test_guard_refreshes_current_branch_before_trusting_remote_ref(tmp_path: Path) -> None:
    checkout = _checkout_with_remote(tmp_path)
    _git(checkout, "checkout", "-b", "feature")
    (checkout / "feature.txt").write_text("published\n")
    _git(checkout, "add", "feature.txt")
    _git(checkout, "commit", "-m", "published feature")
    _git(checkout, "push", "-u", "origin", "feature")
    stale_remote_head = _git(checkout, "rev-parse", "origin/feature").stdout.strip()

    replacement = tmp_path / "replacement"
    _git(tmp_path, "clone", "--branch", "feature", str(tmp_path / "remote.git"), str(replacement))
    _git(replacement, "checkout", "--orphan", "replacement")
    _git(replacement, "rm", "-rf", ".")
    (replacement / "replacement.txt").write_text("rewritten\n")
    _git(replacement, "add", "replacement.txt")
    _git(replacement, "commit", "-m", "rewrite feature")
    _git(replacement, "push", "--force", "origin", "HEAD:feature")

    assert _git(checkout, "rev-parse", "origin/feature").stdout.strip() == stale_remote_head
    with pytest.raises(SystemExit) as excinfo:
        cli_main._guard_update_local_work(["git"], checkout, "main")

    assert excinfo.value.code == 2
    assert _git(checkout, "rev-parse", "origin/feature").stdout.strip() != stale_remote_head


def test_work_created_after_first_preflight_stops_before_cleanup_stash_or_pull(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout_with_remote(tmp_path)
    seed = tmp_path / "seed"
    (seed / "tracked.txt").write_text("upstream update\n")
    _git(seed, "add", "tracked.txt")
    _git(seed, "commit", "-m", "upstream update")
    _git(seed, "push", "origin", "main")

    lockfile = checkout / "package-lock.json"
    real_guard = cli_main._guard_update_local_work
    guard_calls = 0

    def guard_then_create_work(git_cmd, repo_root, branch):
        nonlocal guard_calls
        guard_calls += 1
        current_branch = real_guard(git_cmd, repo_root, branch)
        if guard_calls == 1:
            lockfile.write_text('{"lock":"late local edit"}\n')
        return current_branch

    monkeypatch.setattr(cli_main, "PROJECT_ROOT", checkout)
    monkeypatch.setattr(cli_main, "_is_windows", lambda: False)
    monkeypatch.setattr(cli_main, "_run_pre_update_backup", lambda _args: None)
    monkeypatch.setattr(cli_main, "_pause_windows_gateways_for_update", lambda: None)
    monkeypatch.setattr(cli_main, "_guard_update_local_work", guard_then_create_work)

    with (
        pytest.raises(SystemExit) as excinfo,
    ):
        cli_main._cmd_update_impl(_update_args(), gateway_mode=False)

    assert excinfo.value.code == 2
    assert guard_calls >= 2
    assert lockfile.read_text() == '{"lock":"late local edit"}\n'
    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() != _git(
        checkout, "rev-parse", "origin/main"
    ).stdout.strip()


def test_noop_update_restores_original_detached_head(
    tmp_path: Path, monkeypatch
) -> None:
    checkout = _checkout_with_remote(tmp_path)
    original_head = _git(checkout, "rev-parse", "HEAD").stdout.strip()
    _git(checkout, "checkout", "--detach", original_head)

    monkeypatch.setattr(cli_main, "PROJECT_ROOT", checkout)
    monkeypatch.setattr(cli_main.sys, "platform", "linux")
    monkeypatch.setattr(cli_main, "_is_windows", lambda: False)
    monkeypatch.setattr(cli_main, "_is_fork", lambda origin_url: False)
    monkeypatch.setattr(cli_main, "_run_pre_update_backup", lambda args: None)
    monkeypatch.setattr(cli_main, "_pause_windows_gateways_for_update", lambda: None)
    monkeypatch.setattr(cli_main, "_resume_windows_gateways_after_update", lambda *a: None)
    monkeypatch.setattr(cli_main, "_invalidate_update_cache", lambda: None)
    monkeypatch.setattr(cli_main, "_venv_core_imports_healthy", lambda: (True, ""))
    monkeypatch.setattr(
        "hermes_cli.managed_uv.update_managed_uv", lambda **kwargs: None
    )
    monkeypatch.setattr("hermes_cli.managed_uv.ensure_uv", lambda **kwargs: None)

    cli_main._cmd_update_impl(_update_args(), gateway_mode=False)

    assert _git(checkout, "branch", "--show-current").stdout.strip() == ""
    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == original_head


def test_syntax_rollback_uses_reset_keep_on_a_clean_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout_with_remote(tmp_path)
    old_head = _git(checkout, "rev-parse", "HEAD").stdout.strip()
    seed = tmp_path / "seed"
    (seed / "tracked.txt").write_text("upstream update\n")
    _git(seed, "add", "tracked.txt")
    _git(seed, "commit", "-m", "upstream update")
    _git(seed, "push", "origin", "main")

    monkeypatch.setattr(cli_main, "PROJECT_ROOT", checkout)
    monkeypatch.setattr(cli_main, "_is_windows", lambda: False)
    monkeypatch.setattr(cli_main, "_run_pre_update_backup", lambda _args: None)
    monkeypatch.setattr(cli_main, "_pause_windows_gateways_for_update", lambda: None)
    monkeypatch.setattr(
        cli_main,
        "_validate_critical_files_syntax",
        lambda _root: (False, "hermes_cli/main.py", "bad syntax"),
    )
    real_run = subprocess.run
    calls: list[list[str]] = []

    def recording_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(cli_main.subprocess, "run", recording_run)

    with pytest.raises(SystemExit) as excinfo:
        cli_main._cmd_update_impl(_update_args(), gateway_mode=False)

    assert excinfo.value.code == 1
    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == old_head
    assert any(command[1:3] == ["reset", "--keep"] for command in calls)
    assert not any("--hard" in command for command in calls)


def test_syntax_rollback_refuses_to_reset_late_local_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout_with_remote(tmp_path)
    seed = tmp_path / "seed"
    (seed / "tracked.txt").write_text("upstream update\n")
    _git(seed, "add", "tracked.txt")
    _git(seed, "commit", "-m", "upstream update")
    _git(seed, "push", "origin", "main")
    remote_head = _git(seed, "rev-parse", "HEAD").stdout.strip()
    lockfile = checkout / "package-lock.json"

    def fail_syntax_after_local_edit(_root):
        lockfile.write_text('{"lock":"late local edit"}\n')
        return False, "hermes_cli/main.py", "bad syntax"

    monkeypatch.setattr(cli_main, "PROJECT_ROOT", checkout)
    monkeypatch.setattr(cli_main, "_is_windows", lambda: False)
    monkeypatch.setattr(cli_main, "_run_pre_update_backup", lambda _args: None)
    monkeypatch.setattr(cli_main, "_pause_windows_gateways_for_update", lambda: None)
    monkeypatch.setattr(cli_main, "_validate_critical_files_syntax", fail_syntax_after_local_edit)
    real_run = subprocess.run
    calls: list[list[str]] = []

    def recording_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(cli_main.subprocess, "run", recording_run)

    with pytest.raises(SystemExit) as excinfo:
        cli_main._cmd_update_impl(_update_args(), gateway_mode=False)

    assert excinfo.value.code == 2
    assert lockfile.read_text() == '{"lock":"late local edit"}\n'
    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == remote_head
    assert not any("reset" in command for command in calls)
