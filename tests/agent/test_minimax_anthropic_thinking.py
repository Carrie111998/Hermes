"""Regression guard: preserve thinking blocks on MiniMax's /anthropic endpoint.

MiniMax's ``api.minimax.io/anthropic`` route (and ``api.minimaxi.com/anthropic``
for the China region) speaks the Anthropic Messages protocol but, when
thinking mode is enabled, requires ``thinking`` blocks from prior assistant
turns to round-trip on subsequent requests. Without a MiniMax-specific
carve-out, the request falls into the generic third-party path that
**strips all thinking blocks** (signatures are Anthropic-proprietary and
the generic path can't validate them) — destroying the interleaved-thinking
trace that MiniMax M3 needs for quality.

MiniMax compatibility: signed ``thinking`` blocks must be stripped (MiniMax
can't validate Anthropic signatures) but unsigned ``thinking`` blocks
synthesised from ``reasoning_content`` MUST be preserved on replayed
assistant tool-call messages — same policy as DeepSeek's ``/anthropic``
endpoint (test_deepseek_anthropic_thinking.py).

Refs: H-10 (hermes-v2 plan, 2026-07-20).
"""

from __future__ import annotations

import pytest

# [hermes-v2] H-61: regression marker for Core-Patch verification
pytestmark = [pytest.mark.h61_regression, pytest.mark.h10_regression]


class TestMiniMaxAnthropicPreservesThinking:
    """convert_messages_to_anthropic must replay MiniMax thinking blocks."""

    @pytest.mark.parametrize(
        "base_url",
        [
            "https://api.minimax.io/anthropic",
            "https://api.minimax.io/anthropic/",
            "https://api.minimax.io/anthropic/v1",
            "https://api.minimaxi.com/anthropic",
            "https://api.minimaxi.com/anthropic/",
            "https://API.MiniMax.io/anthropic",
        ],
    )
    def test_unsigned_thinking_block_survives_replay(self, base_url: str) -> None:
        """Unsigned thinking (synthesised from reasoning_content) must be
        preserved on MiniMax /anthropic. Without the H-10 branch, this
        assertion fails because the third-party catch-all strips everything.
        """
        from agent.anthropic_adapter import convert_messages_to_anthropic

        messages = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "reasoning_content": "planning the tool call",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "skill_view", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ]
        _system, converted = convert_messages_to_anthropic(
            messages, base_url=base_url
        )

        assistant_msg = next(m for m in converted if m["role"] == "assistant")
        thinking_blocks = [
            b for b in assistant_msg["content"]
            if isinstance(b, dict) and b.get("type") == "thinking"
        ]
        assert len(thinking_blocks) == 1, (
            f"MiniMax /anthropic ({base_url}) must preserve unsigned thinking "
            "blocks synthesised from reasoning_content — without the H-10 "
            "branch, the third-party catch-all strips them and the next turn "
            "fails with HTTP 400."
        )
        assert thinking_blocks[0]["thinking"] == "planning the tool call"
        # Synthesised block — never has a signature
        assert "signature" not in thinking_blocks[0]

    def test_signed_thinking_block_is_stripped(self) -> None:
        """Anthropic-signed thinking blocks must still be stripped — MiniMax
        cannot validate Anthropic-proprietary signatures."""
        from agent.anthropic_adapter import convert_messages_to_anthropic

        messages = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "old reasoning",
                        "signature": "anthropic-signed-bytes",
                    },
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "skill_view",
                        "input": {},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ]
        _system, converted = convert_messages_to_anthropic(
            messages, base_url="https://api.minimax.io/anthropic"
        )

        assistant_msg = next(m for m in converted if m["role"] == "assistant")
        thinking_blocks = [
            b for b in assistant_msg["content"]
            if isinstance(b, dict) and b.get("type") == "thinking"
        ]
        assert thinking_blocks == [], (
            "Signed thinking blocks must be stripped on MiniMax — the "
            "provider cannot validate Anthropic-proprietary signatures."
        )

    def test_minimax_non_anthropic_endpoint_still_third_party(self) -> None:
        """When MiniMax is reached via the OpenAI-compat route (no /anthropic
        path), the third-party catch-all still applies. This guards against
        an over-broad detection that would pull the wrong branch in."""
        from agent.anthropic_adapter import _is_minimax_anthropic_endpoint

        assert _is_minimax_anthropic_endpoint("https://api.minimax.io/v1") is False
        assert _is_minimax_anthropic_endpoint("https://api.minimax.io") is False
        assert _is_minimax_anthropic_endpoint("https://api.minimax.io/anthropic") is True
        assert _is_minimax_anthropic_endpoint("https://api.minimaxi.com/anthropic") is True
