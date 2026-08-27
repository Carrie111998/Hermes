"""Regression tests for the consecutive clarify-timeout breaker (#96050).

When a session's clarifies keep timing out, each NEW prompt still arms the
text intercept — the user's imperative follow-ups ("stop", "restart") are
consumed as clarify answers and never reach the agent as real turns, and the
model re-clarifies in a loop (live report: ~4 hours of 10-minute
clarify→timeout cycles). After two consecutive timeouts the gateway stops
posting new prompts for the session and tells the agent to answer as text;
any successfully answered clarify resets the streak.
"""

from unittest.mock import MagicMock

import pytest

from gateway import run as gateway_run


@pytest.fixture(autouse=True)
def _clean_streaks():
    gateway_run._CLARIFY_TIMEOUT_STREAKS.clear()
    yield
    gateway_run._CLARIFY_TIMEOUT_STREAKS.clear()


def _timed_out_wait(clarify_mod):
    """Drive one clarify through the sent-disposition → timeout path."""
    fut = MagicMock()
    result = MagicMock()
    result.success = True
    fut.result.return_value = result
    clarify_mod.get_clarify_timeout.return_value = 600.0
    clarify_mod.wait_for_response.return_value = None  # timeout
    return gateway_run._clarify_send_then_wait(
        fut, clarify_id="c1", session_key="sk", clarify_mod=clarify_mod
    )


class TestClarifyTimeoutBreaker:
    def test_two_consecutive_timeouts_open_the_breaker(self):
        clarify_mod = MagicMock()
        for _ in range(2):
            out = _timed_out_wait(clarify_mod)
            assert "did not respond" in out

        assert gateway_run._clarify_breaker_open("sk") is True
        assert gateway_run._clarify_breaker_open("other-session") is False

    def test_answered_clarify_resets_the_streak(self):
        clarify_mod = MagicMock()
        _timed_out_wait(clarify_mod)
        assert gateway_run._clarify_breaker_open("sk") is False

        fut = MagicMock()
        result = MagicMock()
        result.success = True
        fut.result.return_value = result
        clarify_mod.wait_for_response.return_value = "the user's answer"
        out = gateway_run._clarify_send_then_wait(
            fut, clarify_id="c2", session_key="sk", clarify_mod=clarify_mod
        )
        assert out == "the user's answer"
        assert gateway_run._clarify_breaker_open("sk") is False

        # One timeout alone no longer opens it.
        _timed_out_wait(clarify_mod)
        assert gateway_run._clarify_breaker_open("sk") is False

    def test_empty_session_key_never_opens(self):
        for _ in range(5):
            gateway_run._record_clarify_timeout("")
        assert gateway_run._clarify_breaker_open("") is False
