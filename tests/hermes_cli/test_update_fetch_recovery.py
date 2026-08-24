from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch


def test_update_check_fetch_is_bounded_and_sweeps_timeout_artifacts(tmp_path: Path) -> None:
    from hermes_cli import update_cmd

    with (
        patch.object(
            update_cmd.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["git", "fetch"], timeout=30),
        ) as run,
        patch("hermes_cli.gitlock.clear_stale_git_artifacts") as clear,
    ):
        result = update_cmd._run_update_check_fetch(
            ["git"], [], "origin", "main", tmp_path
        )

    assert result.returncode != 0
    assert "timed out" in result.stderr.lower()
    assert run.call_args.kwargs["timeout"] == update_cmd.UPDATE_CHECK_FETCH_TIMEOUT_SECONDS
    clear.assert_called_once_with(tmp_path, temp_pack_min_age_seconds=0)


def test_update_check_fetch_sweeps_artifacts_after_git_failure(tmp_path: Path) -> None:
    from hermes_cli import update_cmd

    failed = subprocess.CompletedProcess(
        ["git", "fetch"], 128, stdout="", stderr="fatal: transfer aborted"
    )
    with (
        patch.object(update_cmd.subprocess, "run", return_value=failed),
        patch("hermes_cli.gitlock.clear_stale_git_artifacts") as clear,
    ):
        result = update_cmd._run_update_check_fetch(
            ["git"], ["--depth", "1"], "origin", "main", tmp_path
        )

    assert result is failed
    clear.assert_called_once_with(tmp_path, temp_pack_min_age_seconds=0)


def test_update_check_fetch_keeps_success_path_cleanup_free(tmp_path: Path) -> None:
    from hermes_cli import update_cmd

    succeeded = subprocess.CompletedProcess(
        ["git", "fetch"], 0, stdout="", stderr=""
    )
    with (
        patch.object(update_cmd.subprocess, "run", return_value=succeeded),
        patch("hermes_cli.gitlock.clear_stale_git_artifacts") as clear,
    ):
        result = update_cmd._run_update_check_fetch(
            ["git"], [], "origin", "main", tmp_path
        )

    assert result is succeeded
    clear.assert_not_called()