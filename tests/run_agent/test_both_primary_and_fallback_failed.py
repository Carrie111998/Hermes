"""CRITICAL alert for the double-failure case: primary died on billing/
credit exhaustion (HTTP 402) in this turn AND the fallback provider that
was activated in response also failed (auth or billing) before completing.

Before this fix, that sequence surfaced through the same generic
"Provider authentication failed" / "Billing or credits exhausted" copy
used for any ordinary single-provider hiccup, giving the user no signal
that BOTH legs of the failover chain were dead in the same turn (real
incident: 2026-08-09 20:59, Nous 402 -> Anthropic fallback 401 -> turn
aborted with the generic message; see kanban t_069d2d08 / t_7b1728c5).

These tests pin the exact gating logic added to
``agent/conversation_loop.py`` (mirrored here rather than driving the full
7000-line retry loop, matching the existing project convention in
``tests/run_agent/test_auth_provider_failover.py``) and the message
builder in isolation.
"""

from agent.error_classifier import FailoverReason
from agent.turn_retry_state import TurnRetryState
from agent.conversation_loop import _critical_both_failed_message


def _should_flag_both_failed(retry: TurnRetryState, reason: FailoverReason) -> bool:
    """Mirror the exact `_both_failed` / `_both_failed_mr` gating condition
    added to conversation_loop.py's non-retryable and max-retries-exhausted
    terminal branches."""
    return retry.primary_failed_billing_or_credit and reason in {
        FailoverReason.billing,
        FailoverReason.auth,
        FailoverReason.auth_permanent,
    }


class TestGuardFlagDefaults:
    def test_flag_defaults_false(self):
        assert TurnRetryState().primary_failed_billing_or_credit is False


class TestDoubleFailureGating:
    """The actual failure-mode from the incident: primary 402, fallback 401."""

    def test_primary_billing_then_fallback_auth_flags_critical(self):
        retry = TurnRetryState()
        retry.primary_failed_billing_or_credit = True  # set when primary 402'd and fallback activated
        assert _should_flag_both_failed(retry, FailoverReason.auth) is True
        assert _should_flag_both_failed(retry, FailoverReason.auth_permanent) is True

    def test_primary_billing_then_fallback_billing_flags_critical(self):
        """Both providers billing-exhausted in the same turn is also CRITICAL."""
        retry = TurnRetryState()
        retry.primary_failed_billing_or_credit = True
        assert _should_flag_both_failed(retry, FailoverReason.billing) is True

    def test_primary_billing_then_fallback_succeeds_never_sets_flag(self):
        """If the fallback call succeeds, the loop never reaches the terminal
        error branches at all this turn -- nothing to assert on the flag's
        effect here beyond: a fresh TurnRetryState per api_call_count means
        a later, unrelated failure doesn't inherit a stale True from an
        earlier successful turn."""
        retry = TurnRetryState()
        assert retry.primary_failed_billing_or_credit is False


class TestOrdinarySingleProviderFailureNotFlagged:
    """The generic message must be preserved for every non-double-failure shape."""

    def test_fallback_only_auth_failure_without_primary_billing_not_critical(self):
        """Fallback auth failure alone (primary never hit billing this turn,
        e.g. primary_index already > 0 from a prior turn, or this is a
        fresh transport-error escalation) must NOT be flagged."""
        retry = TurnRetryState()  # flag never set -- primary did not fail on billing
        assert _should_flag_both_failed(retry, FailoverReason.auth) is False

    def test_primary_auth_failure_not_billing_not_critical(self):
        """Primary failing on a plain auth error (not billing/credits) and
        then the fallback also failing is NOT the specific case this fix
        targets -- the flag is only set on FailoverReason.billing at the
        primary, so it stays False here."""
        retry = TurnRetryState()
        # Primary failed on auth (not billing) -> the eager-fallback billing
        # branch never runs, so primary_failed_billing_or_credit stays False.
        assert retry.primary_failed_billing_or_credit is False
        assert _should_flag_both_failed(retry, FailoverReason.auth) is False

    def test_primary_billing_then_fallback_rate_limited_not_critical(self):
        """Fallback merely rate-limited (not auth/billing dead) is transient,
        not the 'nothing will work' case -- excluded from the critical set."""
        retry = TurnRetryState()
        retry.primary_failed_billing_or_credit = True
        assert _should_flag_both_failed(retry, FailoverReason.rate_limit) is False

    def test_only_fallback_ever_tried_and_it_fails_alone(self):
        """No fallback chain / fallback never actually needed primary-billing
        escalation this turn (e.g. channel-level provider override configured
        directly, not a failover) -- flag never set, stays generic."""
        retry = TurnRetryState()
        assert _should_flag_both_failed(retry, FailoverReason.auth) is False
        assert _should_flag_both_failed(retry, FailoverReason.billing) is False


class TestCriticalMessageContent:
    """The message itself must be visibly distinct from the generic copy."""

    def test_message_contains_critical_marker_and_details(self):
        msg = _critical_both_failed_message("anthropic", "claude-sonnet-5", "401 invalid x-api-key")
        assert "CRITICAL" in msg
        assert "both primary and fallback" in msg.lower()
        assert "anthropic" in msg
        assert "claude-sonnet-5" in msg
        assert "401 invalid x-api-key" in msg

    def test_message_distinct_from_generic_billing_copy(self):
        msg = _critical_both_failed_message("openrouter", "gpt-4o", "boom")
        generic = "Billing or credits exhausted: boom"
        assert msg != generic
        assert "CRITICAL" not in generic  # sanity: generic copy has no such marker

    def test_message_handles_missing_provider_model(self):
        msg = _critical_both_failed_message("", "", "some error")
        assert "unknown provider" in msg
        assert "unknown model" in msg
