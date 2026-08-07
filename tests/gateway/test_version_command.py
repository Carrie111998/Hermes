"""Tests for gateway /version command."""

import asyncio

from hermes_cli.version_info import format_version_command_label


def test_gateway_version_command_returns_release_line():
    from gateway.run import GatewayRunner

    result = asyncio.run(GatewayRunner._handle_version_command(None, None))  # type: ignore[arg-type]
    assert result == format_version_command_label()
    assert result.startswith("Muncho v2.3.2\n")
    assert "Hermes upstream v0.20.0" in result
    assert "Release SHA:" in result
