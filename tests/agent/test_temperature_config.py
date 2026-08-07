"""Config-to-wire tests for the universal temperature support.

Covers:
  * model.temperature parsing from config.yaml (valid / invalid / boundary)
  * constructor arg > config precedence
  * _session_init_model_config recording
  * backward compatibility: no temperature config -> kwargs contain no
    ``temperature`` key on any of the 4 API paths
  * config-to-wire: model.temperature in config.yaml lands in API kwargs
"""

import pytest
from typing import Any
from unittest.mock import patch

from run_agent import AIAgent

API_KEY = "test-key-1234567890"


def _make_agent(load_config_value, **kwargs):
    """Build a minimal AIAgent with mocked tool loading and config."""
    defaults: dict[str, Any] = dict(
        api_key=API_KEY,
        provider="custom",
        model="claude-opus-4-6-thinking",
        base_url="http://proxy.example/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    defaults.update(kwargs)
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("hermes_cli.config.load_config_readonly", return_value=load_config_value),
    ):
        return AIAgent(**defaults)


class TestTemperatureConfigParsing:
    """model.temperature resolution: constructor > config.yaml > None."""

    @pytest.mark.parametrize("raw", [0.0, 0.5, 1.0, 1.5, 2.0, "0.5", 0])
    def test_valid_config_temperatures(self, raw):
        agent = _make_agent({"model": {"temperature": raw}})
        assert agent.temperature == float(raw)

    @pytest.mark.parametrize("raw", [-0.1, 2.1, 3.0, True, False, "hot", None, "abc"])
    def test_invalid_config_temperatures_fall_back_to_none(self, raw):
        agent = _make_agent({"model": {"temperature": raw}})
        assert agent.temperature is None

    def test_constructor_wins_over_config(self):
        agent = _make_agent(
            {"model": {"temperature": 0.4}}, temperature=0.9
        )
        assert agent.temperature == 0.9

    def test_constructor_temperature_survives_without_config(self):
        agent = _make_agent({"model": {}}, temperature=0.7)
        assert agent.temperature == 0.7

    def test_no_config_section_leaves_temperature_none(self):
        agent = _make_agent({})
        assert agent.temperature is None

    def test_session_init_model_config_records_temperature(self):
        agent = _make_agent({"model": {"temperature": 0.3}})
        assert agent._session_init_model_config["temperature"] == 0.3

    def test_session_init_model_config_records_constructor_value(self):
        agent = _make_agent({"model": {}}, temperature=1.2)
        assert agent._session_init_model_config["temperature"] == 1.2

    def test_session_init_model_config_none_when_unset(self):
        agent = _make_agent({})
        assert agent._session_init_model_config["temperature"] is None


class TestTemperatureBackwardCompatibility:
    """No temperature configured -> kwargs are byte-identical (no key)."""

    def test_legacy_path_kwargs_have_no_temperature_key(self):
        agent = _make_agent({})
        kwargs = agent._build_api_kwargs([{"role": "user", "content": "Hi"}])
        assert "temperature" not in kwargs

    def test_profile_path_kwargs_have_no_temperature_key(self):
        # "deepseek" is a registered provider profile -> profile path.
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch(
                "hermes_cli.config.load_config_readonly",
                return_value={"model": {"temperature": None}},
            ),
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
        kwargs = agent._build_api_kwargs([{"role": "user", "content": "Hi"}])
        assert "temperature" not in kwargs

    def test_anthropic_path_kwargs_have_no_temperature_key(self):
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch("hermes_cli.config.load_config_readonly", return_value={"model": {}}),
        ):
            agent = AIAgent(
                api_key=API_KEY,
                provider="anthropic",
                model="claude-sonnet-4-5",
                api_mode="anthropic_messages",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
        kwargs = agent._build_api_kwargs([{"role": "user", "content": "Hi"}])
        assert "temperature" not in kwargs

    def test_bedrock_path_kwargs_have_no_temperature_key(self):
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch("hermes_cli.config.load_config_readonly", return_value={"model": {}}),
        ):
            agent = AIAgent(
                api_key=API_KEY,
                provider="bedrock",
                model="anthropic.claude-3-5-sonnet",
                api_mode="bedrock_converse",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
        kwargs = agent._build_api_kwargs([{"role": "user", "content": "Hi"}])
        assert "temperature" not in kwargs["inferenceConfig"]


class TestTemperatureConfigToWire:
    """model.temperature from config.yaml reaches the API kwargs."""

    def test_legacy_path_config_to_wire(self):
        agent = _make_agent({"model": {"temperature": 0.7}})
        kwargs = agent._build_api_kwargs([{"role": "user", "content": "Hi"}])
        assert kwargs["temperature"] == 0.7

    def test_constructor_config_to_wire(self):
        agent = _make_agent({}, temperature=0.25)
        kwargs = agent._build_api_kwargs([{"role": "user", "content": "Hi"}])
        assert kwargs["temperature"] == 0.25

    def test_anthropic_path_config_to_wire(self):
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch(
                "hermes_cli.config.load_config_readonly",
                return_value={"model": {"temperature": 0.8}},
            ),
        ):
            agent = AIAgent(
                api_key=API_KEY,
                provider="anthropic",
                model="claude-sonnet-4-5",
                api_mode="anthropic_messages",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
        kwargs = agent._build_api_kwargs([{"role": "user", "content": "Hi"}])
        assert kwargs["temperature"] == 0.8

    def test_bedrock_path_config_to_wire(self):
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch(
                "hermes_cli.config.load_config_readonly",
                return_value={"model": {"temperature": 0.6}},
            ),
        ):
            agent = AIAgent(
                api_key=API_KEY,
                provider="bedrock",
                model="anthropic.claude-3-5-sonnet",
                api_mode="bedrock_converse",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
        kwargs = agent._build_api_kwargs([{"role": "user", "content": "Hi"}])
        assert kwargs["inferenceConfig"]["temperature"] == 0.6
