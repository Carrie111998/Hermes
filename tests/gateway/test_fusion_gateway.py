import pytest

from gateway.run import GatewayRunner
from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS


class Event:
    text = "/fusion --help"


@pytest.mark.asyncio
async def test_gateway_fusion_handler_uses_shared_adapter(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)

    def fake_run(command, **kwargs):
        assert command == "/fusion --help"
        return "fusion help"

    monkeypatch.setattr("hermes_cli.fusion_command.run_fusion_command", fake_run)
    output = await runner._handle_fusion_command(Event())
    assert output == "fusion help"
    assert "fusion" in GATEWAY_KNOWN_COMMANDS
