from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli.runtime.command_runner import LocalRunner, SSHRunner


@patch("hermes_cli.runtime.command_runner.subprocess.run")
def test_local_runner_preserves_command_and_cwd(mock_run):
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = "ok\n"
    mock_run.return_value.stderr = ""

    result = LocalRunner().run(
        ("git", "status", "--short"),
        cwd=Path("/srv/project"),
    )

    mock_run.assert_called_once_with(
        ("git", "status", "--short"),
        cwd=Path("/srv/project"),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.ok is True
    assert result.command == ("git", "status", "--short")
    assert result.stdout == "ok\n"


@patch("hermes_cli.runtime.command_runner.subprocess.run")
def test_ssh_runner_quotes_cwd_and_command(mock_run):
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = "main\n"
    mock_run.return_value.stderr = ""

    result = SSHRunner("handwerkeros", "qm").run(
        ("git", "branch", "--show-current"),
        cwd=Path("/home/qm/project with space"),
    )

    mock_run.assert_called_once_with(
        (
            "ssh",
            "-o",
            "BatchMode=yes",
            "qm@handwerkeros",
            "cd '/home/qm/project with space' && git branch --show-current",
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.ok is True
    assert result.stdout == "main\n"


@patch("hermes_cli.runtime.command_runner.subprocess.run")
def test_ssh_runner_uses_host_alias_without_user(mock_run):
    mock_run.return_value.returncode = 7
    mock_run.return_value.stdout = ""
    mock_run.return_value.stderr = "failed"

    result = SSHRunner("handwerkeros").run(("false",))

    assert mock_run.call_args.args[0][3] == "handwerkeros"
    assert result.ok is False
    assert result.returncode == 7
    assert result.stderr == "failed"


def test_ssh_runner_rejects_empty_host():
    with pytest.raises(ValueError, match="host must not be empty"):
        SSHRunner("   ")
