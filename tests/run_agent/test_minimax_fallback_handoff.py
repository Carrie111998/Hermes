"""Regression: MiniMax → fallback-provider history handoff (H-14).

Sibling of H-10. H-10 makes Hermes **preserve** MiniMax's interleaved
thinking so M3 keeps quality across tool rounds. H-14 guards the *opposite*
edge of the same fix: when the agent falls back from MiniMax to a stricter
provider (``zai``/glm-5.2, or any generic third-party Anthropic proxy), the
MiniMax reasoning semantics must **not** leak onto the new provider —
otherwise the replayed history is rejected with HTTP 400.

Two failure directions this locks in:

1. **Pad-detection must FLIP on fallback.** ``_needs_thinking_reasoning_pad()``
   is True for MiniMax but must return False for glm-5.2/zai. If a detector
   were too broad — or the ``(provider, model, base_url)`` cache stuck the
   MiniMax verdict onto the fallback — ``copy_reasoning_content_for_api``
   would force ``reasoning_content`` onto glm's tool-call replay → HTTP 400
   ``reasoning_content is required`` on a provider that never asked for it.

2. **History conversion must SANITIZE for a generic third-party.** A message
   history that still carries MiniMax-synthesised *unsigned* thinking, once
   handed to a non-MiniMax ``/anthropic`` endpoint, must be stripped clean by
   the third-party catch-all — the stricter upstream can't validate those
   blocks. (Contrast: on MiniMax's own ``/anthropic`` route H-10 keeps them.)

These tests are marked ``h10_regression`` on purpose: they fail the moment
the MiniMax detection over-applies to the fallback provider, which is exactly
the H-10 fix's blast radius.

Refs: H-14 (hermes-v2 plan, 2026-07-20); sibling of H-10.
"""

from __future__ import annotations

import pytest

# [hermes-v2] H-61: fallback handoff is part of the H-10 Core-Patch regression
# surface (guards against over-broad MiniMax detection). Reusing the registered
# h10/h61 markers auto-joins this file to the ``pytest -m h61_regression`` lane.
pytestmark = [pytest.mark.h10_regression, pytest.mark.h61_regression]

from run_agent import AIAgent


def _make_agent(provider: str = "", model: str = "", base_url: str = "") -> AIAgent:
    """Bare AIAgent carrying just the routing identity the detectors read
    (mirrors tests/run_agent/test_minimax_tool_reasoning.py)."""
    agent = object.__new__(AIAgent)
    agent.provider = provider
    agent.model = model
    agent.base_url = base_url
    agent.verbose_logging = False
    agent.reasoning_callback = None
    agent.stream_delta_callback = None
    agent._stream_callback = None
    return agent


def _switch(agent: AIAgent, provider: str, model: str, base_url: str = "") -> None:
    """Simulate ``_try_activate_fallback()`` swapping the routing identity in
    place — WITHOUT touching ``_thinking_pad_cache``. The whole point is to
    prove the cache self-invalidates on the changed (provider, model, base_url)
    key rather than serving the stale MiniMax verdict."""
    agent.provider = provider
    agent.model = model
    agent.base_url = base_url


# --- Direction 1: the fallback provider must NOT pad ------------------------

class TestFallbackProviderDoesNotPad:
    """glm-5.2 / zai (and generic strict providers) never enforce the
    MiniMax/DeepSeek reasoning_content echo-back."""

    @pytest.mark.parametrize(
        ("provider", "model", "base_url"),
        [
            ("zai", "glm-5.2", "https://api.z.ai/api/anthropic"),
            ("zai", "glm-5.2", "https://api.z.ai/api/paas/v4"),
            ("zai", "glm-4.6", ""),
            ("openrouter", "z-ai/glm-5.2", "https://openrouter.ai/api/v1"),
            ("", "glm-5.2", ""),  # bare model, no provider slug
        ],
    )
    def test_glm_fallback_never_pads(self, provider, model, base_url) -> None:
        agent = _make_agent(provider=provider, model=model, base_url=base_url)
        assert agent._needs_thinking_reasoning_pad() is False, (
            f"Fallback target {provider!r}/{model!r} must not enforce "
            "reasoning_content echo — MiniMax's pad semantics would 400 here."
        )

    def test_glm_matches_no_reasoning_detector(self) -> None:
        """None of the four sub-detectors may fire for glm-5.2 — a single
        false-positive would silently re-enable the pad via the OR-chain."""
        agent = _make_agent(provider="zai", model="glm-5.2",
                            base_url="https://api.z.ai/api/anthropic")
        assert agent._needs_minimax_tool_reasoning() is False
        assert agent._needs_deepseek_tool_reasoning() is False
        assert agent._needs_kimi_tool_reasoning() is False
        assert agent._needs_mimo_tool_reasoning() is False


# --- Direction 2: the pad verdict must flip when routing switches -----------

class TestFallbackFlipsPad:
    """The (provider, model, base_url)-keyed pad cache must not carry a stale
    MiniMax=True verdict across a fallback switch."""

    def test_minimax_to_glm_flips_true_to_false(self) -> None:
        agent = _make_agent(provider="minimax", model="MiniMax-M3",
                            base_url="https://api.minimax.io/v1")
        assert agent._needs_thinking_reasoning_pad() is True  # primes cache = True
        _switch(agent, "zai", "glm-5.2", "https://api.z.ai/api/anthropic")
        assert agent._needs_thinking_reasoning_pad() is False, (
            "After fallback minimax→glm-5.2 the pad must flip to False even "
            "though the previous call cached True on the same agent instance."
        )

    def test_glm_back_to_minimax_flips_false_to_true(self) -> None:
        agent = _make_agent(provider="zai", model="glm-5.2",
                            base_url="https://api.z.ai/api/anthropic")
        assert agent._needs_thinking_reasoning_pad() is False  # primes cache = False
        _switch(agent, "minimax", "MiniMax-M3", "https://api.minimax.io/v1")
        assert agent._needs_thinking_reasoning_pad() is True, (
            "Recovering minimax after a glm fallback must re-enable the pad; "
            "a stale False verdict would drop reasoning_content and 400."
        )

    def test_host_only_minimax_flips_on_base_url_change(self) -> None:
        """Detection is host-driven for aliased models: changing only the
        base_url away from MiniMax must flip the verdict."""
        agent = _make_agent(provider="custom", model="house-alias",
                            base_url="https://api.minimax.io/v1")
        assert agent._needs_thinking_reasoning_pad() is True
        _switch(agent, "custom", "house-alias", "https://openrouter.ai/api/v1")
        assert agent._needs_thinking_reasoning_pad() is False


# --- Direction 3: history conversion sanitizes for the stricter provider ----

class TestFallbackHistorySanitized:
    """MiniMax-synthesised unsigned thinking must be stripped when the
    conversation is replayed to a non-MiniMax /anthropic endpoint."""

    @staticmethod
    def _minimax_style_history():
        # Assistant is deliberately NOT the last message so even the
        # direct-Anthropic path would strip it — isolates the third-party rule.
        return [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "reasoning_content": "minimax planning the tool call",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "skill_view", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
            {"role": "user", "content": "continue"},
        ]

    @staticmethod
    def _assistant_thinking(base_url):
        from agent.anthropic_adapter import convert_messages_to_anthropic
        _system, converted = convert_messages_to_anthropic(
            TestFallbackHistorySanitized._minimax_style_history(),
            base_url=base_url,
        )
        assistant = next(m for m in converted if m["role"] == "assistant")
        return [
            b for b in assistant["content"]
            if isinstance(b, dict) and b.get("type") == "thinking"
        ]

    @pytest.mark.parametrize(
        "fallback_base_url",
        [
            "https://api.z.ai/api/anthropic",        # zai/glm-5.2 fallback
            "https://openrouter.ai/anthropic",       # generic aggregator
            "https://house-proxy.example.com/anthropic",  # private strict proxy
        ],
    )
    def test_thinking_stripped_for_fallback_endpoint(self, fallback_base_url) -> None:
        blocks = self._assistant_thinking(fallback_base_url)
        assert blocks == [], (
            f"MiniMax thinking must be stripped when handed to {fallback_base_url} "
            "— the stricter upstream cannot validate MiniMax blocks and would 400."
        )

    def test_minimax_endpoint_still_preserves_thinking(self) -> None:
        """Paired contrast: on MiniMax's own /anthropic route H-10 keeps the
        block. This asserts the fallback strip is endpoint-specific, not a
        blanket teardown that would also break the healthy MiniMax path."""
        blocks = self._assistant_thinking("https://api.minimax.io/anthropic")
        assert len(blocks) == 1
        assert blocks[0]["thinking"] == "minimax planning the tool call"
        assert "signature" not in blocks[0]
