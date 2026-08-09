from types import SimpleNamespace
from unittest.mock import patch

import cli as cli_mod


def _partial_cli(*, max_turns=90, explicit=None):
    cli = cli_mod.HermesCLI.__new__(cli_mod.HermesCLI)
    cli.max_turns = max_turns
    setattr(cli, "_max_turns_cli_override", explicit)
    cli.agent = SimpleNamespace(max_iterations=max_turns)
    return cli


def test_refresh_updates_reused_agent_from_current_config(tmp_path, monkeypatch):
    cli = _partial_cli()
    (tmp_path / "config.yaml").write_text("agent:\n  max_turns: 200\n")
    monkeypatch.setattr(cli_mod, "_hermes_home", tmp_path)

    cli._refresh_max_turns_from_config()

    assert cli.max_turns == 200
    assert cli.agent is not None
    assert cli.agent.max_iterations == 200


def test_refresh_preserves_explicit_cli_override(tmp_path, monkeypatch):
    cli = _partial_cli(max_turns=25, explicit=25)
    (tmp_path / "config.yaml").write_text("agent:\n  max_turns: 200\n")
    monkeypatch.setattr(cli_mod, "_hermes_home", tmp_path)

    cli._refresh_max_turns_from_config()

    assert cli.max_turns == 25
    assert cli.agent is not None
    assert cli.agent.max_iterations == 25


def test_chat_refreshes_max_turns_before_agent_initialization():
    cli = _partial_cli()
    setattr(cli, "_secret_capture_callback", lambda *_args, **_kwargs: {})
    cli._last_turn_interrupted = False
    setattr(cli, "_active_agent_route_signature", "route")
    route = {
        "signature": "route",
        "model": None,
        "runtime": None,
        "request_overrides": None,
    }

    with (
        patch.object(cli, "_ensure_runtime_credentials", return_value=True),
        patch.object(cli, "_resolve_turn_agent_config", return_value=route),
        patch.object(cli, "_refresh_max_turns_from_config") as refresh,
        patch.object(cli, "_init_agent", return_value=False),
    ):
        cli.chat("hello")

    refresh.assert_called_once_with()