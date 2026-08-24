"""Regression coverage for OpenRouter per-key spending-cap guidance."""

from agent.conversation_loop import _key_limit_failure_result, _key_limit_message
from agent.error_classifier import ClassifiedError, FailoverReason


def test_key_limit_guidance_distinguishes_key_cap_from_account_balance():
    message = _key_limit_message(
        provider="openrouter",
        model="anthropic/claude-sonnet-4",
    )

    assert "selected API key" in message
    assert "does not mean the provider account is out of credits" in message
    assert "https://openrouter.ai/settings/keys" in message
    assert "Add credits" not in message


def test_key_limit_result_never_emits_a_billing_block():
    classified = ClassifiedError(
        reason=FailoverReason.key_limit,
        retryable=False,
        should_rotate_credential=True,
        should_fallback=True,
    )

    result = _key_limit_failure_result(
        classified=classified,
        summary="Key limit exceeded (daily limit)",
        messages=[],
        api_call_count=1,
        provider="openrouter",
        model="anthropic/claude-sonnet-4",
    )

    assert result["failure_reason"] == "key_limit"
    assert result["failure_retryable"] is False
    assert "billing_block" not in result
    assert "Add credits" not in result["final_response"]
    assert "selected API key" in result["final_response"]
