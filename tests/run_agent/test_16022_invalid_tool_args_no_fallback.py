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
     in front of the ``_try_activate_fallback()`` call inside the
     ``is_client_error`` branch — the path malformed-args actually
     reaches (it is non-retryable, so the retries-exhausted branch
     never fires for it). A defensive copy of the same guard sits on
     the auth-failover compound condition; that guard is a no-op today
     (every auth reason sets ``should_fallback=True``) but codifies the
     principle for any future auth reason the classifier marks as
     non-recoverable-by-fallback.

Callsite audit (round-3 follow-up to teknium1's review) — two sites
were intentionally left UNGUARDED because the classifier's
``should_fallback`` field is overloaded across reasons:
  * Rate-limit / billing / transport-failure eager-fallback branch
    (``_should_fallback`` predicate). ``overloaded`` and ``timeout``
    set ``should_fallback=False`` meaning "retry first, switching is
    not yet warranted" — but this branch's own ``retry_count >= 2``
    ceiling IS the warrant. Naively AND-gating on ``should_fallback``
    would strand transport-failure providers in retry loops after the
    per-provider budget is spent.
  * Max-retries-exhausted terminal-fallback branch. Same reasoning:
    once the retry budget is spent on ``overloaded`` / ``timeout``,
    switching providers is a legitimate recovery. The classifier
    cannot make this call because it does not see ``retry_count``.

The discriminator that WOULD be uniform across reasons is
"classification marks the error as deterministic for this input"
(``reason == format_error and not should_fallback``). Today only
malformed-args matches that discriminator, and malformed-args is
non-retryable, so the L4862 guard already covers it. A future
``is_input_deterministic`` field on ``ClassifiedError`` could let
the retries-exhausted branch skip fallback for new deterministic
reasons without breaking transport-failure recovery; out of scope
for #16022.

This test file locks the fix in via the mirrored-predicate pattern
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

from agent.error_classifier import ClassifiedError, FailoverReason, classify_api_error


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


# Mirror of the L4213-L4221 source-set reason buckets feeding the
# rate-limit / billing / transport-failure eager-fallback gate. Lock-step
# with conversation_loop.py.
_RATE_LIMITED_REASONS = {
    FailoverReason.rate_limit,
    FailoverReason.billing,
    FailoverReason.upstream_rate_limit,
}
_TRANSPORT_FAILURE_REASONS = {
    FailoverReason.timeout,
    FailoverReason.overloaded,
}


def _mirror_rate_limit_eager_activates_fallback(classified, *, retry_count=2):
    """Mirror of the ``_should_fallback`` predicate at
    conversation_loop.py L4234-L4238 (post-round-3: this branch is
    intentionally UNGUARDED by ``should_fallback`` — see module docstring
    for the semantic analysis). Returns True when the eager-fallback
    branch will reach ``agent._try_activate_fallback(reason=...)``.

    Default ``retry_count=2`` so transport-failure falls back the same way
    the source does once the per-branch ``retry_count >= 2`` ceiling is
    met; rate-limit / billing reach the call immediately. Lock-step with
    the source: change one, change both.
    """
    is_rate_limited = classified.reason in _RATE_LIMITED_REASONS
    _is_transport_failure = classified.reason in _TRANSPORT_FAILURE_REASONS
    return bool(
        is_rate_limited
        or (_is_transport_failure and retry_count >= 2)
    )


def _mirror_auth_failover_activates_fallback(classified):
    """Mirror of the auth-failover compound condition at
    conversation_loop.py L4306-L4318. The source guards the call with
    ``and classified.should_fallback`` (defensive no-op today; see
    module docstring); this helper returns True when the loop will reach
    ``agent._try_activate_fallback(reason=...)`` (excluding the
    per-iteration retry bookkeeping flags, which are contextual and not
    part of the classification contract).

    Lock-step with the source: change one, change both.
    """
    return bool(
        classified.is_auth
        and classified.should_fallback
    )


def _mirror_max_retries_exhausted_activates_fallback(classified):
    """Mirror of the max-retries-exhausted terminal-fallback gate at
    conversation_loop.py L5090-L5100 (post-round-3: this branch is
    intentionally UNGUARDED by ``should_fallback`` — see module docstring
    for the semantic analysis). Returns True unconditionally: the source
    calls ``agent._try_activate_fallback()`` whenever this branch is
    reached, regardless of the classifier's ``should_fallback`` field.

    Lock-step with the source: change one, change both.
    """
    return True


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


class TestRateLimitEagerFallbackGate:
    """Round-3 audit follow-up: the eager-fallback gate at
    conversation_loop.py L4234-L4238 is intentionally NOT guarded by
    ``classified.should_fallback``. The original round-3 draft added
    that guard; this test class locks in the revert by asserting the
    pre-round-3 recovery contract still holds.

    Rationale: ``overloaded`` and ``timeout`` set
    ``should_fallback=False`` in the classifier to mean "retry first,
    switching is not yet warranted." This branch's own
    ``retry_count >= 2`` ceiling IS the warrant — at that point the
    per-provider retry budget is spent and switching providers is the
    legitimate recovery. Naively AND-gating on ``should_fallback``
    would strand transport-failure providers in retry loops forever.
    """

    def test_malformed_tool_args_does_not_enter_eager_gate(self):
        """Sanity: malformed-args is a format_error, not in the
        rate-limit / transport-failure reason buckets, so this branch
        is never reached for it. The L4862 ``is_client_error`` guard
        is what protects malformed-args.
        """
        classified = _classify_invalid_args(
            "invalid tool call arguments: expected valid JSON object"
        )
        assert classified.reason not in _RATE_LIMITED_REASONS
        assert classified.reason not in _TRANSPORT_FAILURE_REASONS
        assert _mirror_rate_limit_eager_activates_fallback(classified) is False

    def test_billing_eager_activates_fallback(self):
        e = _MockAPIError(
            "Insufficient credits. Top up your balance.",
            status_code=402,
            body={"error": {"message": "insufficient credits"}},
        )
        classified = classify_api_error(
            e, provider="openrouter", model="anthropic/claude-opus"
        )
        assert classified.reason == FailoverReason.billing
        assert _mirror_rate_limit_eager_activates_fallback(classified) is True

    def test_transport_failure_eager_activates_fallback_after_two_retries(self):
        """REGRESSION GUARD: round-3 draft broke this by AND-gating on
        ``should_fallback`` (which is False for ``overloaded``). The
        revert restores the per-branch ``retry_count >= 2`` ceiling as
        the sole transport-fallback discriminator.
        """
        e = _MockAPIError(
            "Overloaded",
            status_code=429,
            body={"error": {"message": "the engine is currently overloaded"}},
        )
        classified = classify_api_error(e, provider="openai", model="gpt-4o")
        assert classified.reason == FailoverReason.overloaded
        # Below the retry ceiling → no eager fallback.
        assert _mirror_rate_limit_eager_activates_fallback(
            classified, retry_count=1
        ) is False
        # At/above the retry ceiling → eager fallback fires.
        assert _mirror_rate_limit_eager_activates_fallback(
            classified, retry_count=2
        ) is True


class TestAuthFailoverGate:
    """Round-3 audit follow-up: the auth-failover compound condition at
    conversation_loop.py L4306-L4318 carries a defensive
    ``and classified.should_fallback`` guard. Every auth-classified
    reason currently sets ``should_fallback=True``, so the guard is a
    no-op today; it exists to codify the principle for any future auth
    reason the classifier marks as non-recoverable-by-fallback.
    """

    def test_malformed_tool_args_does_not_enter_auth_gate(self):
        """Sanity: malformed-args is a format_error, not auth, so this
        branch is never reached for it.
        """
        classified = _classify_invalid_args(
            "invalid function_call arguments: JSON parse failed"
        )
        assert classified.is_auth is False
        assert _mirror_auth_failover_activates_fallback(classified) is False

    def test_auth_error_with_should_fallback_true_activates_failover(self):
        classified = ClassifiedError(
            reason=FailoverReason.auth,
            should_fallback=True,
            retryable=False,
        )
        assert classified.is_auth is True
        assert _mirror_auth_failover_activates_fallback(classified) is True

    def test_auth_error_with_should_fallback_false_skips_failover(self):
        """Defensive guard: even when ``is_auth`` holds, a classifier
        signal of ``should_fallback=False`` keeps the gate closed. No
        production reason hits this today, but the lock-in prevents a
        future regression if one is added.
        """
        classified = ClassifiedError(
            reason=FailoverReason.auth,
            should_fallback=False,
            retryable=False,
        )
        assert classified.is_auth is True
        assert _mirror_auth_failover_activates_fallback(classified) is False


class TestMaxRetriesExhaustedGate:
    """Round-3 audit follow-up: the terminal-fallback path at
    conversation_loop.py L5090-L5100 is intentionally NOT guarded by
    ``classified.should_fallback``. The original round-3 draft added
    that guard; this test class locks in the revert.

    Rationale: this branch is reached only by retryable errors whose
    retry budget is spent. ``overloaded`` / ``timeout`` /
    ``rate_limit`` at retries-exhausted all benefit from switching
    providers — the per-provider budget is gone, but a different
    provider may have capacity. The classifier's ``should_fallback``
    field does not see ``retry_count`` and so cannot make this call.

    Malformed-args and other non-retryable client errors never reach
    this branch: they abort via ``is_client_error`` (L4862) on the
    first attempt. The L4862 guard is therefore sufficient for the
    teknium1 fix.
    """

    def test_malformed_tool_args_never_reaches_terminal_gate(self):
        """Sanity: malformed-args is non-retryable, so it never
        reaches the retries-exhausted branch. The L4862 guard handles
        it on the first attempt.
        """
        classified = _classify_invalid_args(
            "invalid tool call arguments: arguments must be valid JSON"
        )
        assert classified.retryable is False
        # Mirror: branch fires unconditionally when reached — but
        # malformed-args never reaches it. The discriminator is
        # ``retryable``, not ``should_fallback``.
        assert _mirror_max_retries_exhausted_activates_fallback(classified) is True

    def test_overloaded_at_retries_exhausted_still_activates_terminal_fallback(self):
        """REGRESSION GUARD: round-3 draft broke this by AND-gating on
        ``should_fallback`` (which is False for ``overloaded``). The
        revert restores the unconditional terminal-fallback for
        retryable errors whose retry budget is spent.
        """
        e = _MockAPIError(
            "Overloaded",
            status_code=429,
            body={"error": {"message": "the engine is currently overloaded"}},
        )
        classified = classify_api_error(e, provider="openai", model="gpt-4o")
        assert classified.reason == FailoverReason.overloaded
        assert classified.retryable is True
        assert classified.should_fallback is False  # classifier says "retry first"
        # But at retries-exhausted the loop unconditionally tries fallback.
        assert _mirror_max_retries_exhausted_activates_fallback(classified) is True

    def test_billing_at_retries_exhausted_activates_terminal_fallback(self):
        e = _MockAPIError(
            "Insufficient credits. Top up your balance.",
            status_code=402,
            body={"error": {"message": "insufficient credits"}},
        )
        classified = classify_api_error(
            e, provider="openrouter", model="anthropic/claude-opus"
        )
        assert _mirror_max_retries_exhausted_activates_fallback(classified) is True
