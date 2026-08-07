"""Temperature transmission tests for the ChatCompletionsTransport.

Covers the profile path and the legacy flag path:
  * user temperature reaches api_kwargs on both paths
  * priority: omit_temperature > fixed_temperature > user temperature
  * backward compatibility: no temperature params -> no ``temperature`` key
"""

import pytest
from typing import Any
from unittest.mock import patch

from run_agent import AIAgent

API_KEY = "test-key-1234567890"
MSGS = [{"role": "user", "content": "Hi"}]


def _legacy_agent(model="claude-opus-4-6-thinking", base_url="http://proxy.example/v1", **kwargs):
    """Agent on the legacy flag path (provider without a registered profile)."""
    defaults: dict[str, Any] = dict(
        api_key=API_KEY,
        provider="no-such-provider-xyz",
        model=model,
        base_url=base_url,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    defaults.update(kwargs)
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("hermes_cli.config.load_config", return_value={"model": {}}),
    ):
        return AIAgent(**defaults)


class TestLegacyPathTemperature:
    """Legacy flag path (unknown provider): temperature transmission."""

    def test_user_temperature_reaches_kwargs(self):
        agent = _legacy_agent(temperature=0.7)
        kwargs = agent._build_api_kwargs(MSGS)
        assert kwargs["temperature"] == 0.7

    def test_no_temperature_no_key(self):
        agent = _legacy_agent()
        kwargs = agent._build_api_kwargs(MSGS)
        assert "temperature" not in kwargs

    def test_omit_temperature_wins_over_user(self):
        # Kimi manages temperature server-side -> _fixed_temperature_for_model
        # returns OMIT_TEMPERATURE -> no temperature key even though the
        # caller passed a value.
        agent = _legacy_agent(model="kimi-k2-turbo-preview", temperature=0.8)
        kwargs = agent._build_api_kwargs(MSGS)
        assert "temperature" not in kwargs

    def test_fixed_temperature_wins_over_user(self):
        # Trinity Large Thinking has a fixed 0.5 contract -> fixed wins.
        agent = _legacy_agent(model="trinity-large-thinking", temperature=0.8)
        kwargs = agent._build_api_kwargs(MSGS)
        assert kwargs["temperature"] == 0.5

    def test_fixed_temperature_applies_when_user_unset(self):
        agent = _legacy_agent(model="trinity-large-thinking")
        kwargs = agent._build_api_kwargs(MSGS)
        assert kwargs["temperature"] == 0.5


class TestProfilePathTemperature:
    """Provider-profile path: fixed_temperature priority from the profile."""

    def test_user_temperature_reaches_kwargs(self):
        # "deepseek" is a registered provider profile with fixed_temperature=None.
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch("hermes_cli.config.load_config", return_value={"model": {}}),
        ):
            agent = AIAgent(
                api_key=API_KEY,
                provider="deepseek",
                model="deepseek/deepseek-chat",
                base_url="https://openrouter.ai/api/v1",
                temperature=0.6,
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
        kwargs = agent._build_api_kwargs(MSGS)
        assert kwargs["temperature"] == 0.6

    def test_no_temperature_no_key(self):
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch("hermes_cli.config.load_config", return_value={"model": {}}),
        ):
            agent = AIAgent(
                api_key=API_KEY,
                provider="deepseek",
                model="deepseek/deepseek-chat",
                base_url="https://openrouter.ai/api/v1",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
        kwargs = agent._build_api_kwargs(MSGS)
        assert "temperature" not in kwargs

    def test_profile_omit_temperature_wins_over_user(self):
        # kimi-coding profile declares fixed_temperature=OMIT_TEMPERATURE.
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch("hermes_cli.config.load_config", return_value={"model": {}}),
        ):
            agent = AIAgent(
                api_key=API_KEY,
                provider="kimi-coding",
                model="kimi-k2-turbo-preview",
                base_url="https://api.kimi.com/v1",
                temperature=0.8,
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
        kwargs = agent._build_api_kwargs(MSGS)
        assert "temperature" not in kwargs

    def test_profile_fixed_temperature_wins_over_user(self):
        # A profile with a numeric fixed_temperature must override the caller.
        from providers.base import ProviderProfile
        from providers import get_provider_profile

        fixed_profile = ProviderProfile(
            name="fixed-test",
            base_url="http://proxy.example/v1",
            fixed_temperature=0.5,
        )
        real_get = get_provider_profile
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch("hermes_cli.config.load_config", return_value={"model": {}}),
            patch("providers.get_provider_profile", side_effect=lambda name: (
                fixed_profile if name == "no-such-provider-xyz" else real_get(name)
            )),
        ):
            agent = AIAgent(
                api_key=API_KEY,
                provider="no-such-provider-xyz",
                model="some-model",
                base_url="http://proxy.example/v1",
                temperature=0.8,
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            kwargs = agent._build_api_kwargs(MSGS)
        assert kwargs["temperature"] == 0.5
