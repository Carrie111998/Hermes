from unittest.mock import MagicMock

from cli import HermesCLI
from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command


def test_speculative_command_is_registered_for_cli_and_gateway():
    command = resolve_command("speculative")

    assert command is not None
    assert command.category == "Configuration"
    assert command.args_hint == "[on|off]"
    assert command.name in GATEWAY_KNOWN_COMMANDS


def test_cli_process_command_dispatches_speculative():
    cli = HermesCLI.__new__(HermesCLI)
    cli._pending_resume_sessions = None
    cli._handle_speculative_command = MagicMock()

    assert HermesCLI.process_command(cli, "/speculative on") is True
    cli._handle_speculative_command.assert_called_once_with("/speculative on")
