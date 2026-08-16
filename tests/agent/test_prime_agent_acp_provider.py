"""Regression tests for Prime Agent's local ACP launch configuration."""

from __future__ import annotations

from agent import acp_providers
from agent import prime_agent_acp_provider


def test_prime_agent_defaults_are_provider_specific():
    assert prime_agent_acp_provider.resolve_command() == "prime-agent"
    assert prime_agent_acp_provider.resolve_args() == ["--mode", "acp"]


def test_prime_agent_marker_selects_prime_provider():
    config = acp_providers.get_acp_provider_config("prime-agent")

    assert config.name == "prime-agent"
    assert config.marker_base_url == "acp://prime-agent"
    assert acp_providers.provider_from_base_url("acp://prime-agent/session") == "prime-agent"


def test_unknown_acp_provider_does_not_silently_select_copilot():
    import pytest

    with pytest.raises(ValueError, match="Unknown ACP provider"):
        acp_providers.get_acp_provider_config("not-a-provider")
