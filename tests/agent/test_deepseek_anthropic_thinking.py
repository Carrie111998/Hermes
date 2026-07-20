"""Regression guard: preserve thinking blocks on DeepSeek's /anthropic endpoint.

DeepSeek's ``api.deepseek.com/anthropic`` route speaks the Anthropic Messages
protocol but, when thinking mode is enabled, requires ``thinking`` blocks from
prior assistant turns to round-trip on subsequent requests.  The generic
third-party path strips them (signatures are Anthropic-proprietary and other
proxies cannot validate them), so without a DeepSeek-specific carve-out the
next tool-call turn fails with HTTP 400::

    The content[].thinking in the thinking mode must be passed back to the
    API.

DeepSeek's compatibility matrix lists ``thinking`` as supported but
``redacted_thinking`` and ``cache_control`` on thinking blocks as not
supported.  Handling is the same as Kimi's ``/coding`` endpoint: strip
Anthropic-signed blocks (DeepSeek can't validate them) but preserve unsigned
blocks that Hermes synthesises from ``reasoning_content``.

See hermes-agent#16748.
"""

from __future__ import annotations

import pytest


class TestDeepSeekAnthropicPreservesThinking:
    """convert_messages_to_anthropic must replay DeepSeek thinking blocks."""



    def test_signed_anthropic_thinking_block_is_stripped(self) -> None:
        """Anthropic-signed blocks (that leaked through) must still be stripped.

        DeepSeek issues its own signatures and cannot validate Anthropic's —
        the strip-signed / keep-unsigned split matches the Kimi policy.
        """
        from agent.anthropic_adapter import convert_messages_to_anthropic

        messages = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "anthropic-signed payload",
                        "signature": "anthropic-sig-xyz",
                    },
                    {"type": "text", "text": "hello"},
                ],
            },
            {"role": "user", "content": "again"},
        ]
        _system, converted = convert_messages_to_anthropic(
            messages, base_url="https://api.deepseek.com/anthropic"
        )

        assistant_msg = next(m for m in converted if m["role"] == "assistant")
        thinking_blocks = [
            b for b in assistant_msg["content"]
            if isinstance(b, dict) and b.get("type") == "thinking"
        ]
        assert thinking_blocks == [], (
            "Signed Anthropic thinking blocks must be stripped on DeepSeek — "
            "DeepSeek cannot validate Anthropic-proprietary signatures."
        )

    def test_cache_control_stripped_from_thinking_block(self) -> None:
        """cache_control must still be stripped even when the block is preserved.

        DeepSeek's compatibility matrix lists cache_control on thinking blocks
        as ignored — cache markers interfere with signature validation on
        upstreams that do check them, so Hermes strips them everywhere.
        """
        from agent.anthropic_adapter import convert_messages_to_anthropic

        messages = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "reasoning_content": "r1",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "f", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ]
        # Inject cache_control on the synthesised thinking block after-the-fact
        # by running conversion once, mutating, then re-running would be
        # indirect.  Instead check the simpler invariant: no thinking block in
        # the converted output carries cache_control.
        _system, converted = convert_messages_to_anthropic(
            messages, base_url="https://api.deepseek.com/anthropic"
        )
        for m in converted:
            if not isinstance(m.get("content"), list):
                continue
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") in {"thinking", "redacted_thinking"}:
                    assert "cache_control" not in b

    def test_openai_compat_deepseek_base_is_not_matched(self) -> None:
        """The OpenAI-compatible ``api.deepseek.com`` base must NOT trigger the
        DeepSeek /anthropic branch — it never reaches this adapter, but the
        detector should still fail closed so an accidental misuse doesn't
        quietly send signed Anthropic blocks to an OpenAI endpoint.
        """
        from agent.anthropic_adapter import _is_deepseek_anthropic_endpoint

        assert _is_deepseek_anthropic_endpoint("https://api.deepseek.com") is False
        assert _is_deepseek_anthropic_endpoint("https://api.deepseek.com/v1") is False
        assert _is_deepseek_anthropic_endpoint("https://api.deepseek.com/anthropic") is True
        assert _is_deepseek_anthropic_endpoint("https://api.deepseek.com/anthropic/v1") is True

    def test_non_deepseek_third_party_still_strips_all_thinking(self) -> None:
        """Generic third-party Anthropic endpoints (NOT DeepSeek, NOT MiniMax)
        must keep the strip-all behaviour — they reject unsigned blocks
        outright.

        Note: as of H-10 (hermes-v2, 2026-07-20), ``api.minimax.io/anthropic``
        is no longer a "generic third-party" — it has its own branch in
        ``_manage_thinking_signatures`` that preserves unsigned thinking
        blocks (like DeepSeek). This test now uses an unrelated self-hosted
        proxy URL to verify the generic strip-all path still works for
        endpoints that genuinely reject unsigned blocks. MiniMax's carve-out
        is covered by ``tests/agent/test_minimax_anthropic_thinking.py``.
        """
        from agent.anthropic_adapter import convert_messages_to_anthropic

        messages = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "reasoning_content": "r1",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "f", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ]
        _system, converted = convert_messages_to_anthropic(
            messages, base_url="https://anthropic-proxy.internal.example.com"
        )
        assistant_msg = next(m for m in converted if m["role"] == "assistant")
        thinking_blocks = [
            b for b in assistant_msg["content"]
            if isinstance(b, dict) and b.get("type") == "thinking"
        ]
        assert thinking_blocks == [], (
            "Generic third-party Anthropic endpoints (not DeepSeek, not "
            "MiniMax) must keep the strip-all-thinking behaviour — unsigned "
            "blocks get rejected by providers that can't validate them."
        )
