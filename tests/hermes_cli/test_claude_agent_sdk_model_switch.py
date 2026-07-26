"""Focused /model resolution tests for the clientless Claude Agent SDK runtime."""

from unittest.mock import patch

import pytest

from hermes_cli.model_switch import switch_model


@pytest.mark.parametrize(
    "provider_name",
    (
        "claude-agent-sdk",
        "claude-sdk",
        "claude-code-sdk",
        "claude_agent_sdk",
    ),
)
def test_explicit_provider_resolves_clientless_sdk_runtime(provider_name):
    accepted = {
        "accepted": True,
        "persist": True,
        "recognized": True,
        "message": None,
    }

    with patch("hermes_cli.models.validate_requested_model", return_value=accepted):
        result = switch_model(
            raw_input="claude-opus-5",
            current_provider="openai-codex",
            current_model="gpt-5.3-codex",
            current_base_url="",
            current_api_key="",
            explicit_provider=provider_name,
        )

    assert result.success is True
    assert result.new_model == "claude-opus-5"
    assert result.target_provider == "claude-agent-sdk"
    assert result.base_url == ""
    assert result.api_mode == "claude_agent_sdk"
    assert result.api_key == "claude-subscription-oauth"
