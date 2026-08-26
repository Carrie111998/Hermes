"""Regression coverage for the post-restore agent health proof (#94264)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import update_cmd


def _git(
    repo: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=check,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Hermes test")
    source = repo / "agent_entry.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    return repo


def _stash_invalid_tracked_file(repo: Path) -> tuple[str, Path]:
    source = repo / "agent_entry.py"
    source.write_text("<<<<<<< Updated upstream\nVALUE = 2\n", encoding="utf-8")
    stash_ref = update_cmd._stash_local_changes_if_needed(["git"], repo)
    assert stash_ref
    return stash_ref, source


def _stub_import_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep these repository-state tests independent of optional dependencies."""
    monkeypatch.setattr(update_cmd, "_UPDATE_CRITICAL_MODULES", ())


def test_clean_stash_apply_with_invalid_python_is_rejected_and_parked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    stash_ref, source = _stash_invalid_tracked_file(repo)
    _stub_import_probe(monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        update_cmd._restore_stashed_changes(["git"], repo, stash_ref)

    assert exc_info.value.code == 1
    assert source.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert _git(repo, "status", "--porcelain").stdout == ""
    assert _git(repo, "stash", "list").stdout.strip()
    output = capsys.readouterr().out
    assert "health proof" in output
    assert "gateway was not allowed to restart" in output
    assert f"git stash apply {stash_ref}" in output


def test_valid_python_restore_still_drops_the_stash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    source = repo / "agent_entry.py"
    source.write_text("VALUE = 2  # local customization\n", encoding="utf-8")
    stash_ref = update_cmd._stash_local_changes_if_needed(["git"], repo)
    assert stash_ref
    _stub_import_probe(monkeypatch)

    assert update_cmd._restore_stashed_changes(["git"], repo, stash_ref) is True
    assert source.read_text(encoding="utf-8") == "VALUE = 2  # local customization\n"
    assert _git(repo, "stash", "list").stdout == ""


def test_new_import_time_failure_is_rejected_and_parked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    source = repo / "consumer.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add import probe module")
    source.write_text(
        "raise RuntimeError('restored local failure')\n", encoding="utf-8"
    )
    stash_ref = update_cmd._stash_local_changes_if_needed(["git"], repo)
    assert stash_ref
    monkeypatch.setattr(update_cmd, "_UPDATE_CRITICAL_MODULES", ("consumer",))

    with pytest.raises(SystemExit) as exc_info:
        update_cmd._restore_stashed_changes(["git"], repo, stash_ref)

    assert exc_info.value.code == 1
    assert source.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert _git(repo, "status", "--porcelain").stdout == ""
    assert _git(repo, "stash", "list").stdout.strip()


def test_unavailable_import_probe_cannot_drop_new_runtime_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    source = repo / "consumer.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add import probe module")
    source.write_text(
        "raise RuntimeError('restored local failure')\n", encoding="utf-8"
    )
    stash_ref = update_cmd._stash_local_changes_if_needed(["git"], repo)
    assert stash_ref
    monkeypatch.setattr(update_cmd, "_UPDATE_CRITICAL_MODULES", ("consumer",))
    real_run = update_cmd.subprocess.run

    def fail_import_probe(cmd, *args, **kwargs):
        if cmd and cmd[0] != "git" and len(cmd) >= 2 and cmd[1] == "-c":
            raise OSError("simulated interpreter launch failure")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(update_cmd.subprocess, "run", fail_import_probe)

    with pytest.raises(SystemExit) as exc_info:
        update_cmd._restore_stashed_changes(["git"], repo, stash_ref)

    assert exc_info.value.code == 1
    assert source.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert _git(repo, "status", "--porcelain").stdout == ""
    assert _git(repo, "stash", "list").stdout.strip()


def test_preexisting_import_time_failure_does_not_block_valid_restore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    consumer = repo / "consumer.py"
    consumer.write_text(
        "raise RuntimeError('missing local config')\n", encoding="utf-8"
    )
    local_file = repo / "local.txt"
    local_file.write_text("original\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add preexisting import failure")
    local_file.write_text("restored\n", encoding="utf-8")
    stash_ref = update_cmd._stash_local_changes_if_needed(["git"], repo)
    assert stash_ref
    monkeypatch.setattr(update_cmd, "_UPDATE_CRITICAL_MODULES", ("consumer",))

    assert update_cmd._restore_stashed_changes(["git"], repo, stash_ref) is True
    assert local_file.read_text(encoding="utf-8") == "restored\n"
    assert _git(repo, "stash", "list").stdout == ""


def test_invalid_untracked_python_is_part_of_the_restore_proof(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    source = repo / "new_agent_feature.py"
    source.write_text("<<<<<<< Updated upstream\n", encoding="utf-8")
    stash_ref = update_cmd._stash_local_changes_if_needed(["git"], repo)
    assert stash_ref
    _stub_import_probe(monkeypatch)

    with pytest.raises(SystemExit):
        update_cmd._restore_stashed_changes(["git"], repo, stash_ref)

    assert not source.exists()
    assert _git(repo, "status", "--porcelain").stdout == ""
    assert _git(repo, "stash", "list").stdout.strip()


def test_failed_restore_does_not_delete_concurrent_untracked_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    stash_ref, source = _stash_invalid_tracked_file(repo)
    _stub_import_probe(monkeypatch)
    real_run = update_cmd.subprocess.run
    concurrent = repo / "created-during-restore.py"

    def create_concurrent_file(cmd, *args, **kwargs):
        result = real_run(cmd, *args, **kwargs)
        if cmd[:3] == ["git", "stash", "apply"]:
            concurrent.write_text("user-owned = True\n", encoding="utf-8")
        return result

    monkeypatch.setattr(update_cmd.subprocess, "run", create_concurrent_file)

    with pytest.raises(SystemExit) as exc_info:
        update_cmd._restore_stashed_changes(["git"], repo, stash_ref)

    assert exc_info.value.code == 1
    assert source.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert concurrent.read_text(encoding="utf-8") == "user-owned = True\n"
    assert _git(repo, "stash", "list").stdout.strip()


def test_invalid_untracked_python_path_with_spaces_is_removed_safely(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    source = repo / "directory with spaces" / "untracked feature.py"
    source.parent.mkdir()
    source.write_text("<<<<<<< Updated upstream\n", encoding="utf-8")
    stash_ref = update_cmd._stash_local_changes_if_needed(["git"], repo)
    assert stash_ref
    _stub_import_probe(monkeypatch)

    with pytest.raises(SystemExit):
        update_cmd._restore_stashed_changes(["git"], repo, stash_ref)

    assert not source.exists()
    assert _git(repo, "status", "--porcelain").stdout == ""
    assert _git(repo, "stash", "list").stdout.strip()


def test_untracked_cleanup_probe_failure_preserves_ambiguous_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    source = repo / "restored.py"
    source.write_text("<<<<<<< Updated upstream\n", encoding="utf-8")
    stash_ref = update_cmd._stash_local_changes_if_needed(["git"], repo)
    assert stash_ref
    _stub_import_probe(monkeypatch)
    real_run = update_cmd.subprocess.run

    def fail_hash_object(cmd, *args, **kwargs):
        if len(cmd) >= 2 and cmd[1] == "hash-object":
            raise OSError("simulated hash-object launch failure")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(update_cmd.subprocess, "run", fail_hash_object)

    with pytest.raises(SystemExit) as exc_info:
        update_cmd._restore_stashed_changes(["git"], repo, stash_ref)

    assert exc_info.value.code == 1
    assert source.exists()
    assert _git(repo, "stash", "list").stdout.strip()


def test_gateway_success_marker_does_not_overwrite_terminal_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exit_code_path = tmp_path / ".update_exit_code"
    exit_code_path.write_text("1", encoding="utf-8")
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: tmp_path)

    update_cmd._write_gateway_update_exit_code(True, only_if_pending=True)

    assert exit_code_path.read_text(encoding="utf-8") == "1"


def test_invalid_python_path_with_spaces_is_compiled_from_nul_inventory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    source = repo / "directory with spaces" / "agent feature.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add spaced source path")
    source.write_text("<<<<<<< Updated upstream\n", encoding="utf-8")
    stash_ref = update_cmd._stash_local_changes_if_needed(["git"], repo)
    assert stash_ref
    _stub_import_probe(monkeypatch)

    with pytest.raises(SystemExit):
        update_cmd._restore_stashed_changes(["git"], repo, stash_ref)

    assert source.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert _git(repo, "status", "--porcelain").stdout == ""
    assert _git(repo, "stash", "list").stdout.strip()


def test_failed_git_path_inventory_cannot_become_a_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    stash_ref, source = _stash_invalid_tracked_file(repo)
    _stub_import_probe(monkeypatch)
    real_run = update_cmd.subprocess.run

    def fail_python_inventory(cmd, *args, **kwargs):
        if (
            cmd[:2] == ["git", "diff"]
            and "--name-only" in cmd
            and "-z" in cmd
            and "HEAD" in cmd
        ):
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="fatal: simulated inventory failure"
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(update_cmd.subprocess, "run", fail_python_inventory)

    with pytest.raises(SystemExit) as exc_info:
        update_cmd._restore_stashed_changes(["git"], repo, stash_ref)

    assert exc_info.value.code == 1
    assert source.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert _git(repo, "stash", "list").stdout.strip()


def test_unavailable_import_probe_cannot_drop_the_stash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    stash_ref, source = _stash_invalid_tracked_file(repo)
    _stub_import_probe(monkeypatch)
    real_run = update_cmd.subprocess.run

    def fail_import_probe(cmd, *args, **kwargs):
        if cmd and cmd[0] != "git" and len(cmd) >= 2 and cmd[1] == "-c":
            raise OSError("simulated interpreter launch failure")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(update_cmd.subprocess, "run", fail_import_probe)

    with pytest.raises(SystemExit) as exc_info:
        update_cmd._restore_stashed_changes(["git"], repo, stash_ref)

    assert exc_info.value.code == 1
    assert source.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert _git(repo, "stash", "list").stdout.strip()


def test_strict_import_probe_reports_syntax_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    broken = tmp_path / "broken.py"
    broken.write_text("def broken(:\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(update_cmd, "_UPDATE_CRITICAL_MODULES", ("broken",))

    ok, module, detail = update_cmd._validate_critical_modules_import(
        tmp_path, strict=True
    )

    assert ok is False
    assert module == "broken"
    assert detail and ("SyntaxError" in detail or "invalid syntax" in detail)


def test_gateway_restore_prompt_defaults_to_parking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prompts: list[tuple[str, str]] = []

    def gateway_input(prompt: str, default: str = "") -> str:
        prompts.append((prompt, default))
        return ""

    def unexpected_git(*_args, **_kwargs):
        raise AssertionError("declining the prompt must not apply the stash")

    monkeypatch.setattr(update_cmd.subprocess, "run", unexpected_git)
    result = update_cmd._restore_stashed_changes(
        ["git"], tmp_path, "stash@{0}", prompt_user=True, input_fn=gateway_input
    )

    assert result is False
    assert prompts == [("Restore local changes now? [y/N]", "n")]
