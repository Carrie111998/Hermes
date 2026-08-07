"""Tests for the /version slash command."""

import re
from pathlib import Path
from unittest.mock import patch

from cli import HermesCLI
from hermes_cli import __release_date__, __version__
from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command
from hermes_cli.slash_exec import CommandContext, execute_command
from hermes_cli.version_info import format_version_command_label
from ops.muncho.release.metadata import load_release_bundle


ROOT = Path(__file__).parents[2]


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
    muncho_version = load_release_bundle(ROOT).metadata.version

    assert result.startswith(f"Muncho v{muncho_version}\n")
    assert f"Hermes upstream v{__version__}" in result
    assert re.search(r"Release SHA: [0-9a-f]{40} \(short [0-9a-f]{8}\)", result)


def test_missing_muncho_metadata_preserves_clean_upstream_hermes_reply(tmp_path):
    with patch(
        "hermes_cli.banner.format_banner_version_label",
        return_value=f"Hermes Agent v{__version__} ({__release_date__})",
    ):
        result = format_version_command_label(release_root=tmp_path)

    assert result == f"Hermes Agent v{__version__} ({__release_date__})"


def test_cli_version_command_prints_shared_identity(capsys):
    from hermes_cli.main import _print_version_info

    _print_version_info(check_updates=False)
    output = capsys.readouterr().out
    assert f"Muncho v{load_release_bundle(ROOT).metadata.version}" in output
    assert f"Hermes upstream v{__version__}" in output
    assert "Install directory:" in output
