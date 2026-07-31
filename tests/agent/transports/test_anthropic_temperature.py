"""Temperature transmission tests for the Anthropic Messages path.

Covers:
  * temperature reaches messages.create() kwargs
  * manual thinking forces temperature=1 (older models, overrides caller)
  * adaptive thinking (4.6+) preserves the caller's temperature
  * 4.7+ models strip temperature entirely (400 contract)
  * transport passthrough (AnthropicTransport.build_kwargs)
  * backward compatibility: no temperature -> no key
"""

import pytest

from agent.anthropic_adapter import build_anthropic_kwargs
from agent.transports import get_transport


def _kwargs(model, temperature=None, reasoning_config=None, **extra):
    return build_anthropic_kwargs(
        model=model,
        messages=[{"role": "user", "content": "Hi"}],
        tools=None,
        max_tokens=4096,
        reasoning_config=reasoning_config,
        temperature=temperature,
        **extra,
    )


class TestAnthropicTemperatureTransmission:

    def test_temperature_reaches_kwargs(self):
        kwargs = _kwargs("claude-sonnet-4-5", temperature=0.8)
        assert kwargs["temperature"] == 0.8

    def test_no_temperature_no_key(self):
        kwargs = _kwargs("claude-sonnet-4-5")
        assert "temperature" not in kwargs

    def test_zero_temperature_is_preserved(self):
        kwargs = _kwargs("claude-sonnet-4-5", temperature=0.0)
        assert kwargs["temperature"] == 0.0

    def test_manual_thinking_forces_temperature_one(self):
        # Non-adaptive models (pre-4.6) force temperature=1 when thinking is
        # enabled — overriding the caller's value.
        kwargs = _kwargs(
            "claude-sonnet-4-5",
            temperature=0.8,
            reasoning_config={"enabled": True, "effort": "medium"},
        )
        assert kwargs["temperature"] == 1

    def test_adaptive_thinking_preserves_temperature(self):
        # 4.6+ adaptive thinking does not force temperature=1.
        kwargs = _kwargs(
            "claude-opus-4-6",
            temperature=0.8,
            reasoning_config={"enabled": True, "effort": "medium"},
        )
        assert kwargs["temperature"] == 0.8
        assert kwargs["thinking"]["type"] == "adaptive"

    def test_thinking_disabled_preserves_temperature(self):
        kwargs = _kwargs(
            "claude-sonnet-4-5",
            temperature=0.8,
            reasoning_config={"enabled": False},
        )
        assert kwargs["temperature"] == 0.8
        assert "thinking" not in kwargs

    def test_4_7_strips_temperature(self):
        # Opus 4.7+ rejects sampling params with HTTP 400 — strip entirely.
        kwargs = _kwargs("claude-opus-4.7", temperature=0.8)
        assert "temperature" not in kwargs

    def test_4_7_strips_temperature_even_with_thinking(self):
        kwargs = _kwargs(
            "claude-opus-4.7",
            temperature=0.8,
            reasoning_config={"enabled": True, "effort": "high"},
        )
        assert "temperature" not in kwargs


class TestAnthropicTransportPassthrough:

    def _transport(self):
        import agent.transports.anthropic  # noqa: F401
        transport = get_transport("anthropic_messages")
        assert transport is not None
        return transport

    def test_transport_forwards_temperature(self):
        kwargs = self._transport().build_kwargs(
            model="claude-sonnet-4-5",
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=4096,
            temperature=0.7,
        )
        assert kwargs["temperature"] == 0.7

    def test_transport_omits_temperature_when_unset(self):
        kwargs = self._transport().build_kwargs(
            model="claude-sonnet-4-5",
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=4096,
        )
        assert "temperature" not in kwargs
