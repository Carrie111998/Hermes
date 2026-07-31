"""Temperature transmission tests for the Bedrock Converse path.

Covers:
  * temperature reaches inferenceConfig in converse() kwargs
  * transport passthrough (BedrockTransport.build_kwargs)
  * backward compatibility: no temperature -> no key
"""

import pytest

from agent.transports import get_transport


class TestBedrockTemperatureTransmission:

    def _transport(self):
        import agent.transports.bedrock  # noqa: F401
        transport = get_transport("bedrock_converse")
        assert transport is not None
        return transport

    def test_temperature_reaches_inference_config(self):
        kwargs = self._transport().build_kwargs(
            model="anthropic.claude-3-5-sonnet",
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=4096,
            temperature=0.6,
        )
        assert kwargs["inferenceConfig"]["temperature"] == 0.6

    def test_no_temperature_no_key(self):
        kwargs = self._transport().build_kwargs(
            model="anthropic.claude-3-5-sonnet",
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=4096,
        )
        assert "temperature" not in kwargs["inferenceConfig"]

    def test_zero_temperature_is_preserved(self):
        kwargs = self._transport().build_kwargs(
            model="anthropic.claude-3-5-sonnet",
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=4096,
            temperature=0.0,
        )
        assert kwargs["inferenceConfig"]["temperature"] == 0.0

    def test_agent_level_bedrock_temperature(self):
        """End-to-end: agent.temperature lands in Bedrock inferenceConfig."""
        from unittest.mock import patch

        from run_agent import AIAgent

        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch("hermes_cli.config.load_config", return_value={"model": {}}),
        ):
            agent = AIAgent(
                api_key="test-key-1234567890",
                provider="bedrock",
                model="anthropic.claude-3-5-sonnet",
                api_mode="bedrock_converse",
                temperature=0.55,
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
        kwargs = agent._build_api_kwargs([{"role": "user", "content": "Hi"}])
        assert kwargs["inferenceConfig"]["temperature"] == 0.55
