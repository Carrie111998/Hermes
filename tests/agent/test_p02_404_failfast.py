"""P0.2 regression guard — 404/NOT_FOUND must fail fast (0 retries).

IC-001 root cause: a retired model (Gemini "no longer available to new
users") returned HTTP 404 with a bare message naming nothing. The old
generic-404 branch classified it as ``FailoverReason.unknown,
retryable=True`` — so the retry loop hammered the same dead model 3x, and
with conversation fallback enabled each retry also spun up a paid
fallback call. ~419 NOT_FOUND events in one day.

P0.2 contract:
  * 404 / NOT_FOUND  -> fail immediately (retryable=False, 0 retries)
  * 400 Bad Request  -> fail immediately (retryable=False)
  * 401 / 403        -> fail immediately (retryable=False, already correct)
  * 429              -> keep retry policy (retryable=True)
  * 5xx (503/529)    -> keep exponential backoff (retryable=True)
  * 402              -> unchanged behavior (retryable=False, billing)

The retry loop in conversation_loop.py only retries when
``classified.retryable`` is True. So asserting ``retryable=False`` on 404
is the exact fail-fast guarantee.
"""

from __future__ import annotations

from agent.error_classifier import (
    FailoverReason,
    classify_api_error,
)
from agent.gemini_native_adapter import GeminiAPIError


class MockAPIError(Exception):
    """Mirrors OpenAI SDK APIStatusError shape used elsewhere in the suite."""

    def __init__(self, message, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body or {}


class TestP02_404FailFast:
    def test_404_retired_model_is_non_retryable(self):
        """Exact IC-001 shape: Gemini 404 'no longer available', no signal."""
        err = GeminiAPIError(
            "This model models/gemini-2.5-flash is no longer available to "
            "new users. Please update to a newer model.",
            status_code=404,
            code="gemini_http_404",
        )
        c = classify_api_error(err, provider="gemini", model="gemini-2.5-flash")
        assert c.status_code == 404
        assert c.retryable is False, "404/NOT_FOUND must fail fast (0 retries) — IC-001"
        # Falls back to a different model/provider rather than retrying dead model.
        assert c.should_fallback is True

    def test_404_generic_unknown_path_is_non_retryable(self):
        """Any bare 404 (wrong endpoint, retired model) must not retry."""
        err = MockAPIError("404 page not found", status_code=404)
        c = classify_api_error(err, provider="openai", model="some-model")
        assert c.status_code == 404
        assert c.retryable is False

    def test_404_model_not_found_signal_is_non_retryable(self):
        """404 with explicit 'model not found' signal stays fail-fast + fallback."""
        err = MockAPIError("The model does not exist", status_code=404)
        c = classify_api_error(err, provider="openrouter", model="x")
        assert c.retryable is False
        assert c.reason == FailoverReason.model_not_found


class TestP02_400FailFast:
    def test_400_generic_bad_request_is_non_retryable(self):
        err = MockAPIError("Bad Request", status_code=400)
        c = classify_api_error(err, provider="openai", model="gpt-4o")
        assert c.status_code == 400
        assert c.retryable is False, "400 Bad Request must fail fast (0 retries)"
        assert c.reason == FailoverReason.format_error


class TestP02_429KeepsRetry:
    def test_429_rate_limit_remains_retryable(self):
        err = MockAPIError("Rate limit exceeded", status_code=429)
        c = classify_api_error(err, provider="openai", model="gpt-4o")
        assert c.status_code == 429
        assert c.retryable is True, "429 must keep retry policy"
        assert c.reason == FailoverReason.rate_limit


class TestP02_5xxKeepsBackoff:
    def test_503_overloaded_remains_retryable(self):
        err = MockAPIError("Service Unavailable", status_code=503)
        c = classify_api_error(err, provider="openai", model="gpt-4o")
        assert c.status_code == 503
        assert c.retryable is True, "503 must keep exponential backoff"
        assert c.reason == FailoverReason.overloaded

    def test_500_server_error_remains_retryable(self):
        err = MockAPIError("Internal Server Error", status_code=500)
        c = classify_api_error(err, provider="openai", model="gpt-4o")
        assert c.status_code == 500
        assert c.retryable is True, "5xx must keep retry/backoff"


class TestP02_AuthFailFast:
    def test_401_is_non_retryable(self):
        err = MockAPIError("Unauthorized", status_code=401)
        c = classify_api_error(err, provider="openai", model="gpt-4o")
        assert c.status_code == 401
        assert c.retryable is False

    def test_403_is_non_retryable(self):
        err = MockAPIError("Forbidden", status_code=403)
        c = classify_api_error(err, provider="openai", model="gpt-4o")
        assert c.status_code == 403
        assert c.retryable is False
