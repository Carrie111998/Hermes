"""Regression guard for #16022: provider-side malformed tool-call /
function-call argument validation errors must classify as
``FailoverReason.format_error`` with ``should_fallback=False`` so the
conversation-loop fallback gate skips ``_try_activate_fallback()``.

Before this fix the classifier signalled ``should_fallback=True`` for
malformed-argument 400s, but the loop's fallback gate at
``agent/conversation_loop.py`` did not consult that field — it only
checked ``is_client_error`` (derived from ``retryable`` /
``should_compress`` / ``reason``). The result was that a malformed set
of tool-call arguments produced by the primary model was retried against
every configured fallback provider, each of which re-ran the same model
and reproduced the same arguments and the same 400. The fallback cascade
reported in #12770 therefore stayed intact for this case even after the
classifier was taught to recognise it.

The fix has two halves:
  1. ``error_classifier.py`` sets ``should_fallback=False`` on the
     malformed-argument branch (the same semantic already used by
     ``provider_policy_blocked`` and ``invalid_encrypted_content``).
  2. ``conversation_loop.py`` adds ``classified.should_fallback and``
     in front of both the ``_has_pending_fallback()`` status announcement
     and the ``_try_activate_fallback()`` call inside the
     ``is_client_error`` branch.

This test file locks in both halves via the mirrored-predicate pattern
established by ``test_18028_content_policy_blocked.py`` and
``test_31273_402_not_retried.py`` — driving the real conversation_loop
end-to-end would require a prohibitive amount of agent wiring, but the
predicate mirror is kept in lock-step with the source so a regression
in either half surfaces here.

Coverage of the ``function_call arguments`` wording variant (#58233) is
included because that was the original gap that surfaced the bug.
"""
from __future__ import annotations

import pytest

from agent.error_classifier import FailoverReason, classify_api_error


class _MockAPIError(Exception):
    """Minimal stand-in for the SDK's APIStatusError used by classify_api_error."""

    def __init__(self, message, *, status_code, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body if body is not None else {}


def _classify_invalid_args(message_body):
    e = _MockAPIError(
        "Bad Request",
        status_code=400,
        body={"error": {"message": message_body}},
    )
    return classify_api_error(
        e,
        provider="openrouter",
        approx_tokens=180000,
        context_length=200000,
        num_messages=300,
    )


def _mirror_is_client_error(classified):
    """Exact shape of conversation_loop.py's is_client_error predicate.

    Kept in lock-step with the source. If you change one, change both.
    """
    return (
        not classified.retryable
        and not classified.should_compress
        and classified.reason not in {
            FailoverReason.rate_limit,
            FailoverReason.overloaded,
            FailoverReason.context_overflow,
            FailoverReason.payload_too_large,
            FailoverReason.long_context_tier,
            FailoverReason.thinking_signature,
        }
    )


def _mirror_loop_activates_fallback(classified):
    """Mirror of the new fallback-gate in conversation_loop.py's
    is_client_error branch. Returns True when the loop will call
    ``agent._try_activate_fallback()``, False when it skips straight
    to the abort path.

    Lock-step with the source: change one, change both.
    """
    return _mirror_is_client_error(classified) and bool(classified.should_fallback)


class TestMalformedToolArgsClassification:
    """classify_api_error output for malformed-argument 400s — both
    `tool_call` and `function_call` spellings, both bare and OpenRouter
    `metadata.raw`-wrapped forms.
    """

    @pytest.mark.parametrize(
        "body_message",
        [
            "invalid tool call arguments: expected valid JSON object",
            "invalid tool_call arguments: expected valid JSON object",
            "invalid tool_calls arguments: expected valid JSON object",
            "tool call arguments are invalid: parse error",
            "tool_call arguments are invalid: parse error",
            "tool calls arguments are invalid: parse error",
            "invalid function call arguments: expected valid JSON object",
            "invalid function_call arguments: expected valid JSON object",
            "function call arguments are invalid: parse error",
            "function_call arguments are invalid: parse error",
        ],
    )
    def test_bare_body_classified_as_non_fallback_format_error(self, body_message):
        result = _classify_invalid_args(body_message)
        assert result.reason == FailoverReason.format_error
        assert result.retryable is False
        assert result.should_compress is False
        assert result.should_fallback is False

    def test_openrouter_wrapped_tool_call_wording(self):
        e = _MockAPIError(
            "Provider returned error",
            status_code=400,
            body={
                "error": {
                    "message": "Provider returned error",
                    "metadata": {
                        "raw": '{"error":{"message":"invalid tool call arguments: arguments must be valid JSON"}}'
                    },
                }
            },
        )
        result = classify_api_error(
            e, provider="openrouter", approx_tokens=180000, context_length=200000
        )
        assert result.reason == FailoverReason.format_error
        assert result.retryable is False
        assert result.should_compress is False
        assert result.should_fallback is False

    def test_openrouter_wrapped_function_call_wording(self):
        """#58233 wording variant — must match inside metadata.raw too."""
        e = _MockAPIError(
            "Provider returned error",
            status_code=400,
            body={
                "error": {
                    "message": "Provider returned error",
                    "metadata": {
                        "raw": '{"error":{"message":"function_call arguments are invalid: JSON parse failed"}}'
                    },
                }
            },
        )
        result = classify_api_error(
            e, provider="openrouter", approx_tokens=180000, context_length=200000
        )
        assert result.reason == FailoverReason.format_error
        assert result.retryable is False
        assert result.should_compress is False
        assert result.should_fallback is False


class TestLoopSkipsFallbackForMalformedToolArgs:
    """End-to-end-shape regression: mirror the conversation_loop fallback
    gate and confirm it does NOT activate fallback for malformed-argument
    400s — neither compression nor fallback fires, exactly as #16022's
    recovery contract requires.
    """

    def test_loop_does_not_activate_fallback_for_tool_call_wording(self):
        classified = _classify_invalid_args(
            "invalid tool call arguments: expected valid JSON object"
        )
        # Sanity: still a client error (so we reach the fallback gate).
        assert _mirror_is_client_error(classified)
        # Recovery contract: do NOT activate fallback, do NOT compress.
        assert _mirror_loop_activates_fallback(classified) is False
        assert classified.should_compress is False

    def test_loop_does_not_activate_fallback_for_function_call_wording(self):
        classified = _classify_invalid_args(
            "invalid function_call arguments: expected valid JSON object"
        )
        assert _mirror_is_client_error(classified)
        assert _mirror_loop_activates_fallback(classified) is False
        assert classified.should_compress is False

    def test_loop_does_not_activate_fallback_for_openrouter_wrapped_case(self):
        """The originally-reported shape: OpenRouter wraps the upstream
        provider error inside ``metadata.raw``. End-to-end recovery
        contract per teknium1's review: this neither compresses nor
        activates a configured fallback.
        """
        e = _MockAPIError(
            "Provider returned error",
            status_code=400,
            body={
                "error": {
                    "message": "Provider returned error",
                    "metadata": {
                        "raw": '{"error":{"message":"invalid tool call arguments: arguments must be valid JSON"}}'
                    },
                }
            },
        )
        classified = classify_api_error(
            e, provider="openrouter", approx_tokens=180000, context_length=200000
        )
        assert _mirror_is_client_error(classified)
        assert _mirror_loop_activates_fallback(classified) is False
        assert classified.should_compress is False


class TestFallbackStillFiresForOtherClientErrors:
    """Sanity: the new ``should_fallback`` guard must NOT suppress
    fallback for errors where the classifier explicitly asks for it.
    Billing / model_not_found / content_policy_blocked / generic
    format_error all stay on the fallback path.
    """

    def test_billing_still_activates_fallback(self):
        e = _MockAPIError(
            "Insufficient credits. Top up your balance.",
            status_code=402,
            body={"error": {"message": "insufficient credits"}},
        )
        classified = classify_api_error(e, provider="openrouter", model="anthropic/claude-opus")
        assert classified.reason == FailoverReason.billing
        assert _mirror_loop_activates_fallback(classified) is True

    def test_model_not_found_still_activates_fallback(self):
        e = _MockAPIError(
            "Not Found",
            status_code=404,
            body={"error": {"message": "model not found: gpt-9"}},
        )
        classified = classify_api_error(e, provider="openrouter", model="gpt-9")
        assert classified.reason == FailoverReason.model_not_found
        assert _mirror_loop_activates_fallback(classified) is True

    def test_generic_format_error_still_activates_fallback(self):
        """Generic 400 format_error (not malformed tool args) keeps the
        existing 'try a different provider' behaviour — fallback fires.
        """
        e = _MockAPIError(
            "Bad Request",
            status_code=400,
            body={"error": {"message": "messages: required field missing"}},
        )
        classified = classify_api_error(
            e,
            provider="openai",
            model="gpt-4o",
            approx_tokens=1000,
            context_length=200000,
            num_messages=2,
        )
        assert classified.reason == FailoverReason.format_error
        # Generic format_error is a recovery candidate — fallback fires.
        assert _mirror_loop_activates_fallback(classified) is True
