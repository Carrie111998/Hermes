from types import SimpleNamespace

import gateway.run as gateway_run
from gateway.run import GatewayRunner
from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin


def _shared_config():
    return {
        "routing": {
            "enabled": True,
            "default_profile": "balanced",
            "allow_escalation": True,
        }
    }


def _gateway_runner():
    runner = object.__new__(GatewayRunner)
    runner._service_tier = None
    runner._reasoning_config = {"enabled": True, "effort": "medium"}
    return runner


def _cli_stub():
    return SimpleNamespace(
        model="gpt-5.6-sol",
        api_key="***",
        base_url="https://openrouter.ai/api/v1",
        provider="openrouter",
        requested_provider="openrouter",
        api_mode="chat_completions",
        acp_command=None,
        acp_args=[],
        _credential_pool=None,
        service_tier=None,
        reasoning_config={"enabled": True, "effort": "medium"},
    )


def test_gateway_and_cli_match_for_auto_classification(monkeypatch):
    cfg = _shared_config()
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: cfg)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: cfg)

    runner = _gateway_runner()
    gw_route = GatewayRunner._resolve_turn_agent_config(
        runner,
        "build a YouTube video packet",
        "gpt-5.6-sol",
        {
            "api_key": "***",
            "base_url": "https://openrouter.ai/api/v1",
            "provider": "openrouter",
            "requested_provider": "openrouter",
            "api_mode": "chat_completions",
            "command": None,
            "args": [],
            "credential_pool": None,
        },
    )

    cli = _cli_stub()
    cli_route = CLIAgentSetupMixin._resolve_turn_agent_config(
        cli,
        "build a YouTube video packet",
    )

    assert gw_route["model"] == cli_route["model"] == "gpt-5.6-terra"
    assert gw_route["reasoning_config"] == cli_route["reasoning_config"] == {
        "enabled": True,
        "effort": "high",
    }
    assert gw_route["route_profile"] == cli_route["route_profile"] == "creative"


def test_gateway_and_cli_match_for_explicit_override(monkeypatch):
    cfg = _shared_config()
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: cfg)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: cfg)

    message = "[[route: luna xhigh]] build our launch strategy"

    runner = _gateway_runner()
    gw_route = GatewayRunner._resolve_turn_agent_config(
        runner,
        message,
        "gpt-5.6-sol",
        {
            "api_key": "***",
            "base_url": "https://openrouter.ai/api/v1",
            "provider": "openrouter",
            "requested_provider": "openrouter",
            "api_mode": "chat_completions",
            "command": None,
            "args": [],
            "credential_pool": None,
        },
    )

    cli = _cli_stub()
    cli_route = CLIAgentSetupMixin._resolve_turn_agent_config(cli, message)

    assert gw_route["model"] == cli_route["model"] == "gpt-5.6-luna"
    assert gw_route["reasoning_config"] == cli_route["reasoning_config"] == {
        "enabled": True,
        "effort": "xhigh",
    }
    assert gw_route["clean_message"] == cli_route["clean_message"] == "build our launch strategy"
