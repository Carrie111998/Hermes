"""Regression test: MiniMax thinking mode requires reasoning_content echo.

MiniMax reasoning mode (M3, M2.7, …) rejects assistant tool-call replays
that omit ``reasoning_content`` with HTTP 400 — same shape as DeepSeek V4
thinking mode. Without the ``_needs_minimax_tool_reasoning()`` detection
hook in :meth:`AIAgent._needs_thinking_reasoning_pad`, Hermes would pop
the field during history replay and the next turn would fail.

Fix has three pillars (mirror :mod:`test_deepseek_reasoning_content_echo`):

1. ``_needs_minimax_tool_reasoning()`` — new helper that detects all three
   MiniMax signals (provider slug, model-name substring, base_url host).
2. ``_needs_thinking_reasoning_pad()`` — OR's the new helper in.
3. :func:`agent.anthropic_adapter._manage_thinking_signatures` — gains a
   MiniMax-Anthropic branch that preserves unsigned thinking blocks
   (separate regression covered in
   ``tests/agent/test_minimax_anthropic_thinking.py``).

Refs: H-10 (hermes-v2 plan, 2026-07-20).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

# [hermes-v2] H-61: regression marker for Core-Patch verification.
# ``h10_regression`` keeps the file reachable from the standard
# hermes-v2 regression marker set (the four marker files collected by
# ``scripts/run_tests.sh`` use ``-m h10_regression``) so a downstream
# CI lane that only filters by ``h10_regression`` still picks up the
# MiniMax thinking-mode pad regression alongside its H-10 siblings.
pytestmark = [
    pytest.mark.h10_regression,
    pytest.mark.h61_regression,
]

from run_agent import AIAgent


def _make_agent(provider: str = "", model: str = "", base_url: str = "") -> AIAgent:
    agent = object.__new__(AIAgent)
    agent.provider = provider
    agent.model = model
    agent.base_url = base_url
    agent.verbose_logging = False
    agent.reasoning_callback = None
    agent.stream_delta_callback = None
    agent._stream_callback = None
    return agent


class TestNeedsMiniMaxToolReasoning:
    """_needs_minimax_tool_reasoning() recognises all three detection signals."""

    @pytest.mark.parametrize(
        "provider",
        ["minimax", "minimax-cn", "minimax-oauth"],
    )
    def test_provider_slugs(self, provider: str) -> None:
        agent = _make_agent(provider=provider, model="MiniMax-M3")
        assert agent._needs_minimax_tool_reasoning() is True

    def test_provider_case_insensitive(self) -> None:
        agent = _make_agent(provider="MiniMax", model="")
        assert agent._needs_minimax_tool_reasoning() is True

    @pytest.mark.parametrize(
        "model",
        [
            "MiniMax-M3",
            "MiniMax-M2.7",
            "minimax-m3",
            "minimax/MiniMax-M2.7-highspeed",
            "openrouter-suffix/minimax-m2.5-free",
        ],
    )
    def test_model_substring(self, model: str) -> None:
        agent = _make_agent(provider="custom", model=model)
        assert agent._needs_minimax_tool_reasoning() is True

    @pytest.mark.parametrize(
        ("base_url", "expected"),
        [
            ("https://api.minimax.io/v1", True),
            ("https://api.minimax.io/anthropic", True),
            ("https://api.minimaxi.com/v1", True),
            ("https://api.minimaxi.com/anthropic", True),
            ("https://api.minimax.io/", True),
            ("http://127.0.0.1:11434/v1", False),
        ],
    )
    def test_base_url_host(self, base_url: str, expected: bool) -> None:
        agent = _make_agent(provider="custom", model="", base_url=base_url)
        assert agent._needs_minimax_tool_reasoning() is expected

    def test_non_minimax_provider(self) -> None:
        agent = _make_agent(
            provider="openrouter",
            model="anthropic/claude-sonnet-4.6",
            base_url="https://openrouter.ai/api/v1",
        )
        assert agent._needs_minimax_tool_reasoning() is False

    def test_empty_everything(self) -> None:
        agent = _make_agent()
        assert agent._needs_minimax_tool_reasoning() is False

    def test_does_not_match_glm_or_deepseek(self) -> None:
        """Sanity: the helper must not false-positive on similar-looking providers."""
        agent = _make_agent(provider="zai", model="glm-5.2")
        assert agent._needs_minimax_tool_reasoning() is False
        agent = _make_agent(provider="deepseek", model="deepseek-v4-flash")
        assert agent._needs_minimax_tool_reasoning() is False


class TestNeedsThinkingReasoningPadIncludesMiniMax:
    """The OR-chain in _needs_thinking_reasoning_pad includes MiniMax."""

    def test_minimax_triggers_pad(self) -> None:
        agent = _make_agent(provider="minimax", model="MiniMax-M3")
        assert agent._needs_thinking_reasoning_pad() is True

    def test_minimax_cn_triggers_pad(self) -> None:
        agent = _make_agent(provider="minimax-cn", model="MiniMax-M3")
        assert agent._needs_thinking_reasoning_pad() is True

    def test_minimax_host_triggers_pad(self) -> None:
        agent = _make_agent(
            provider="custom",
            model="some-alias",
            base_url="https://api.minimax.io/v1",
        )
        assert agent._needs_thinking_reasoning_pad() is True

    def test_openrouter_anthropic_does_not_trigger_pad(self) -> None:
        """Regression guard: Anthropic on OpenRouter speaks standard protocol,
        not MiniMax thinking mode. Must not be padded."""
        agent = _make_agent(
            provider="openrouter",
            model="anthropic/claude-sonnet-4.6",
            base_url="https://openrouter.ai/api/v1",
        )
        assert agent._needs_thinking_reasoning_pad() is False
