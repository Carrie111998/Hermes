"""Tests for gateway /version command."""

import asyncio
from pathlib import Path

from hermes_cli import __version__
from hermes_cli.version_info import format_version_command_label
from ops.muncho.release.metadata import load_release_bundle


ROOT = Path(__file__).parents[2]


def test_gateway_version_command_returns_release_line():
    from gateway.run import GatewayRunner

    result = asyncio.run(GatewayRunner._handle_version_command(None, None))  # type: ignore[arg-type]
    assert result == format_version_command_label()
    assert result.startswith(f"Muncho v{load_release_bundle(ROOT).metadata.version}\n")
    assert f"Hermes upstream v{__version__}" in result
    assert "Release SHA:" in result
