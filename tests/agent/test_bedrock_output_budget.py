"""Output-budget resolution for the bedrock_converse api_mode.

Regression coverage for the truncation class where long Claude-on-Bedrock
generations died with "Response truncated due to output length limit":
the Converse path sent a flat ``max_tokens=agent.max_tokens or 4096`` and
never consumed the ``_ephemeral_max_output_tokens`` boost that the
truncation-recovery loop sets, so all 4 continuation retries re-ran at the
same budget and recovery was mathematically impossible.
"""

import types

from agent.chat_completion_helpers import (
    _BEDROCK_FALLBACK_MAX_OUTPUT_TOKENS,
    _BEDROCK_UNKNOWN_MODEL_MAX_OUTPUT_TOKENS,
    _resolve_bedrock_max_output_tokens,
)


def _agent(model, max_tokens=None):
    return types.SimpleNamespace(model=model, max_tokens=max_tokens)


def test_claude_model_resolves_its_real_output_ceiling():
    """No explicit pin: fall back to the model's ceiling, not a flat 4096."""
    assert _resolve_bedrock_max_output_tokens(
        _agent("global.anthropic.claude-opus-4-7")
    ) == 128_000
    assert _resolve_bedrock_max_output_tokens(
        _agent("global.anthropic.claude-sonnet-4-5")
    ) == 64_000


def test_explicit_max_tokens_is_honoured_within_the_ceiling():
    assert _resolve_bedrock_max_output_tokens(
        _agent("global.anthropic.claude-opus-4-7", max_tokens=32_000)
    ) == 32_000


def test_explicit_max_tokens_is_clamped_to_the_model_ceiling():
    """A flat pin must not exceed what Bedrock accepts (hard ValidationException)."""
    assert _resolve_bedrock_max_output_tokens(
        _agent("global.anthropic.claude-sonnet-4-5", max_tokens=128_000)
    ) == 64_000


def test_ephemeral_boost_takes_priority_over_explicit_max_tokens():
    """The truncation-recovery boost must actually raise the budget."""
    assert _resolve_bedrock_max_output_tokens(
        _agent("global.anthropic.claude-opus-4-7", max_tokens=8_192),
        ephemeral_out=16_384,
    ) == 16_384


def test_ephemeral_boost_is_clamped_to_the_model_ceiling():
    """A doubled retry budget must not exceed the provider limit."""
    assert _resolve_bedrock_max_output_tokens(
        _agent("global.anthropic.claude-opus-4-7", max_tokens=128_000),
        ephemeral_out=256_000,
    ) == 128_000


def test_unknown_model_without_pin_uses_conservative_fallback():
    """Non-Claude Bedrock models (Nova/Llama/DeepSeek) have no ceiling table entry."""
    assert _resolve_bedrock_max_output_tokens(
        _agent("us.amazon.nova-pro-v1:0")
    ) == _BEDROCK_FALLBACK_MAX_OUTPUT_TOKENS


def test_unknown_model_clamps_a_stale_claude_sized_pin():
    """A pin left over from a Claude model must degrade, not hard-fail the turn."""
    assert _resolve_bedrock_max_output_tokens(
        _agent("us.amazon.nova-pro-v1:0", max_tokens=128_000)
    ) == _BEDROCK_UNKNOWN_MODEL_MAX_OUTPUT_TOKENS
    assert _resolve_bedrock_max_output_tokens(
        _agent("meta.llama3-70b-instruct-v1:0", max_tokens=128_000),
        ephemeral_out=256_000,
    ) == _BEDROCK_UNKNOWN_MODEL_MAX_OUTPUT_TOKENS


def test_invalid_max_tokens_values_are_ignored():
    """Bools/non-ints/non-positives must not become the budget."""
    for bad in (True, False, 0, -1, "128000", None, 3.5):
        assert _resolve_bedrock_max_output_tokens(
            _agent("global.anthropic.claude-opus-4-7", max_tokens=bad)
        ) == 128_000


def test_build_api_kwargs_consumes_the_ephemeral_boost():
    """The boost must be cleared after use so it can't leak into later turns."""
    agent = _agent("global.anthropic.claude-opus-4-7", max_tokens=8_192)
    agent._ephemeral_max_output_tokens = 16_384

    ephemeral = getattr(agent, "_ephemeral_max_output_tokens", None)
    if ephemeral is not None:
        agent._ephemeral_max_output_tokens = None
    budget = _resolve_bedrock_max_output_tokens(agent, ephemeral_out=ephemeral)

    assert budget == 16_384
    assert agent._ephemeral_max_output_tokens is None
