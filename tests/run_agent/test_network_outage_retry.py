"""Focused tests for persistent provider-network outage retries."""

from types import SimpleNamespace

import pytest

from agent.error_classifier import classify_api_error
from agent.network_outage_retry import (
    NetworkOutageRetryPolicy,
    is_provider_network_outage,
    outage_retry_wait_seconds,
)


class APIConnectionError(Exception):
    """OpenAI-SDK-shaped status-less transport failure for tests."""


class HTTPError(Exception):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code
        self.response = SimpleNamespace(status_code=status_code)


def _classified(error: Exception):
    return classify_api_error(
        error,
        provider="openai-codex",
        model="gpt-5.6",
        approx_tokens=10_000,
        context_length=272_000,
        num_messages=10,
    )


@pytest.mark.parametrize(
    "error",
    [
        APIConnectionError("Connection error."),
        TimeoutError(
            "Codex stream produced no bytes within 600s (TTFB threshold: 600s)"
        ),
        TimeoutError(
            "Codex stream produced no SSE events for 300s after first byte"
        ),
        TimeoutError(
            "Non-streaming API call timed out after 900s with no response"
        ),
    ],
)
def test_statusless_network_and_provider_stall_errors_enter_outage_mode(error):
    assert is_provider_network_outage(error, _classified(error)) is True


@pytest.mark.parametrize(
    "error",
    [
        HTTPError("Service temporarily unavailable", 503),
        HTTPError("rate limited", 429),
        HTTPError("unauthorized", 401),
        RuntimeError("certificate verify failed"),
        RuntimeError(
            "Provider has been unresponsive (no response received) for 5 "
            "consecutive stale attempts; aborting this call"
        ),
    ],
)
def test_non_network_failures_do_not_enter_outage_mode(error):
    assert is_provider_network_outage(error, _classified(error)) is False


def test_policy_defaults_disabled_and_clamps_bad_values():
    policy = NetworkOutageRetryPolicy.from_config(
        {
            "enabled": "yes",
            "interval_seconds": 0,
            "max_wait_seconds": -10,
        }
    )
    assert policy.enabled is True
    assert policy.interval_seconds == 1.0
    assert policy.max_wait_seconds == 0.0


def test_policy_accepts_legacy_sleep_seconds_alias():
    policy = NetworkOutageRetryPolicy.from_config(
        {"enabled": True, "sleep_seconds": 17}
    )
    assert policy.interval_seconds == 17.0


def test_unlimited_policy_retries_at_fixed_interval():
    policy = NetworkOutageRetryPolicy(
        enabled=True,
        interval_seconds=300,
        max_wait_seconds=0,
    )
    assert outage_retry_wait_seconds(policy, elapsed_seconds=86_400) == 300


def test_finite_policy_stops_after_budget_and_truncates_last_wait():
    policy = NetworkOutageRetryPolicy(
        enabled=True,
        interval_seconds=300,
        max_wait_seconds=650,
    )
    assert outage_retry_wait_seconds(policy, elapsed_seconds=0) == 300
    assert outage_retry_wait_seconds(policy, elapsed_seconds=600) == 50
    assert outage_retry_wait_seconds(policy, elapsed_seconds=650) is None
