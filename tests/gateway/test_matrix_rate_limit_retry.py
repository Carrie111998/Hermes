"""Tests for Matrix 429 (M_LIMIT_EXCEEDED) send backoff.

mautrix 0.21 raises ``MLimitExceeded`` for HTTP 429 but neither retries it
(its HTTPAPI only retries 502/503/504) nor exposes ``retry_after_ms`` as an
attribute, so the adapter recognises the rate-limit itself and honours a
backoff. These tests cover the detection/parse/clamp helper.
"""

from plugins.platforms.matrix.adapter import (
    _MATRIX_DEFAULT_RETRY_AFTER_SECONDS,
    _MATRIX_MAX_RETRY_AFTER_SECONDS,
    _matrix_retry_after_seconds,
)


class _FakeMatrixError(Exception):
    """Stand-in for mautrix ``MatrixStandardRequestError`` (http_status only)."""

    def __init__(self, http_status=None, errcode=None, message=""):
        super().__init__(message)
        if http_status is not None:
            self.http_status = http_status
        if errcode is not None:
            self.errcode = errcode
        self._message = message

    def __str__(self):
        return self._message


class TestMatrixRetryAfterSeconds:
    def test_non_rate_limit_returns_none(self):
        # A generic send failure (e.g. timeout) must not be treated as a 429.
        assert _matrix_retry_after_seconds(TimeoutError("boom")) is None
        assert _matrix_retry_after_seconds(_FakeMatrixError(http_status=500)) is None

    def test_detects_429_by_http_status(self):
        exc = _FakeMatrixError(http_status=429)
        # No retry_after_ms available -> default backoff.
        assert _matrix_retry_after_seconds(exc) == _MATRIX_DEFAULT_RETRY_AFTER_SECONDS

    def test_detects_429_by_errcode_text(self):
        exc = _FakeMatrixError(errcode="M_LIMIT_EXCEEDED")
        assert _matrix_retry_after_seconds(exc) == _MATRIX_DEFAULT_RETRY_AFTER_SECONDS

    def test_detects_429_by_message_text(self):
        exc = _FakeMatrixError(message="M_LIMIT_EXCEEDED: Too Many Requests")
        assert _matrix_retry_after_seconds(exc) == _MATRIX_DEFAULT_RETRY_AFTER_SECONDS

    def test_parses_retry_after_ms_from_attribute(self):
        exc = _FakeMatrixError(http_status=429)
        exc.retry_after_ms = 3500
        assert _matrix_retry_after_seconds(exc) == 3.5

    def test_parses_retry_after_ms_from_message(self):
        exc = _FakeMatrixError(
            http_status=429,
            message='{"errcode":"M_LIMIT_EXCEEDED","retry_after_ms":5000}',
        )
        assert _matrix_retry_after_seconds(exc) == 5.0

    def test_clamps_absurd_retry_after_to_max(self):
        exc = _FakeMatrixError(http_status=429)
        exc.retry_after_ms = 10_000_000  # 10000s -> clamp
        assert _matrix_retry_after_seconds(exc) == _MATRIX_MAX_RETRY_AFTER_SECONDS

    def test_clamps_tiny_retry_after_to_floor(self):
        exc = _FakeMatrixError(http_status=429)
        exc.retry_after_ms = 1  # 0.001s -> floor of 0.5s
        assert _matrix_retry_after_seconds(exc) == 0.5
