"""Tests for the /version slash command."""

import re
from unittest.mock import patch

from cli import HermesCLI
from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command
from hermes_cli.slash_exec import CommandContext, execute_command
from hermes_cli.version_info import format_version_command_label


def test_version_command_is_registered():
    cmd = resolve_command("version")
    assert cmd is not None
    assert cmd.name == "version"
    assert cmd.category == "Info"
    assert resolve_command("v") is cmd


def test_version_is_gateway_known():
    assert "version" in GATEWAY_KNOWN_COMMANDS
    assert "v" in GATEWAY_KNOWN_COMMANDS


def test_process_command_version_prints_version_info():
    cli_obj = HermesCLI.__new__(HermesCLI)

    with patch("hermes_cli.main._print_version_info") as mock_print:
        assert cli_obj.process_command("/version") is True

    mock_print.assert_called_once_with(check_updates=True)


def test_cli_version_executor_reports_muncho_hermes_and_exact_sha():
    result = execute_command("version", CommandContext(surface="cli")).text

    assert result.startswith("Muncho v2.3.2\n")
    assert "Hermes upstream v0.20.0" in result
    assert re.search(r"Release SHA: [0-9a-f]{40} \(short [0-9a-f]{8}\)", result)


def test_missing_muncho_metadata_preserves_clean_upstream_hermes_reply(tmp_path):
    with patch(
        "hermes_cli.banner.format_banner_version_label",
        return_value="Hermes Agent v0.20.0 (2026.8.3)",
    ):
        result = format_version_command_label(release_root=tmp_path)

    assert result == "Hermes Agent v0.20.0 (2026.8.3)"


def test_cli_version_command_prints_shared_identity(capsys):
    from hermes_cli.main import _print_version_info

    _print_version_info(check_updates=False)
    output = capsys.readouterr().out
    assert "Muncho v2.3.2" in output
    assert "Hermes upstream v0.20.0" in output
    assert "Install directory:" in output
