from unittest.mock import MagicMock, patch

import cli as cli_module
from agent.context_compressor import ContextCompressor
from agent.speculative_compression import SpeculativeCompressionSettings
from run_agent import AIAgent

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


def test_cli_process_command_reports_authoritative_runtime_state(monkeypatch):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.compression_enabled = True
    agent.speculative_compression_settings = SpeculativeCompressionSettings(enabled=False)
    agent.speculative_compression_enabled = False
    agent.context_compressor = agent.context_compressor or ContextCompressor(
        model="test/model",
        context_length=1_000,
        max_tokens=100,
        quiet_mode=True,
    )
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"compression": {"speculative": {"enabled": False}}})
    monkeypatch.setattr("agent.speculative_compression.get_default_manager", lambda: MagicMock())

    cli = HermesCLI.__new__(HermesCLI)
    cli._pending_resume_sessions = None
    cli.agent = agent
    output = []
    monkeypatch.setattr(cli_module, "_cprint", output.append)

    for command in ("/speculative on", "/speculative off", "/speculative status"):
        assert HermesCLI.process_command(cli, command) is True

    assert len(output) == 3
    for reply in output:
        assert "enabled=" in reply
        assert "eligible=" in reply
        assert "install_status=" in reply
        assert "runtime_override=" in reply
