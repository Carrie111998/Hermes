"""Regression tests for deterministic provider worker request-limit errors."""
from __future__ import annotations

from agent.error_classifier import FailoverReason, classify_api_error


def test_worker_local_total_request_limit_is_not_backoff_retryable():
    classified = classify_api_error(
        Exception("ResourceExhausted: Worker local total request limit reached (173/48)"),
        provider="nvidia",
        model="deepseek-ai/deepseek-v4-flash",
    )

    assert classified.reason is FailoverReason.local_request_limit
    assert classified.retryable is False
    assert classified.should_fallback is True
    assert classified.should_rotate_credential is False


def test_other_resource_exhausted_errors_remain_rate_limit_retryable():
    classified = classify_api_error(
        Exception("ResourceExhausted: requests per minute exceeded"),
        provider="nvidia",
        model="deepseek-ai/deepseek-v4-flash",
    )

    assert classified.reason is FailoverReason.rate_limit
    assert classified.retryable is True
