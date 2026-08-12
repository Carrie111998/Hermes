"""Regression test for #84371: stale reasoning fields must not inflate
preflight estimates when ``exclude_stale_thinking=True``.

The tail-budget walk in ``context_compressor.py`` charges stale thinking
only on the newest assistant turn (#73624).  Preflight used to charge it
on every message, so a reasoning-heavy session could have a preflight
estimate 10x the actual wire size and trigger a compaction dead-loop.
"""
from agent.model_metadata import (
    estimate_messages_tokens_rough,
    estimate_request_tokens_rough,
)


_REASONING = "This is a very long chain of thought. " * 200


class TestStaleThinkingExclusion:
    def test_exclude_stale_thinking_strips_reasoning_fields(self):
        msg = {
            "role": "assistant",
            "content": "Hello",
            "reasoning": _REASONING,
            "reasoning_content": _REASONING,
        }
        with_stale = estimate_messages_tokens_rough([msg])
        without_stale = estimate_messages_tokens_rough(
            [msg], exclude_stale_thinking=True
        )
        assert without_stale < with_stale, (
            "exclude_stale_thinking should reduce the estimate when the "
            "message carries stale reasoning content"
        )

    def test_exclude_stale_thinking_is_opt_in(self):
        msg = {
            "role": "assistant",
            "content": "Hello",
            "reasoning": _REASONING,
        }
        # Default must stay charged (backward compat).
        default_estimate = estimate_messages_tokens_rough([msg])
        assert default_estimate > 0

    def test_request_estimate_forwards_exclude_stale_thinking(self):
        messages = [
            {
                "role": "assistant",
                "content": "Hi",
                "reasoning": _REASONING,
                "reasoning_content": _REASONING,
            }
        ]
        with_stale = estimate_request_tokens_rough(messages)
        without_stale = estimate_request_tokens_rough(
            messages, exclude_stale_thinking=True
        )
        assert without_stale < with_stale
