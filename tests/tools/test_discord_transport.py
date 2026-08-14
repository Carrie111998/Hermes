"""Tests for the Discord route-aware rate-limit contract (feature R1)."""

import pytest

from tools.discord_api.transport import (
    RateLimitInfo,
    RouteBucket,
    TransportError,
    is_retriable,
    parse_rate_limit_response,
)


class TestParseRateLimitResponse:
    def test_parses_valid_429_body(self):
        info = parse_rate_limit_response(
            429,
            {
                "retry_after": 2.5,
                "message": "You are being rate limited.",
                "global": True,
                "code": 20029,
            },
        )
        assert isinstance(info, RateLimitInfo)
        assert info.status == 429
        assert info.retry_after == 2.5
        assert info.message == "You are being rate limited."
        assert info.global_ is True
        assert info.code == 20029

    def test_accepts_integer_retry_after(self):
        info = parse_rate_limit_response(
            429,
            {"retry_after": 7, "message": "slow down", "global": False, "code": 20029},
        )
        assert info.retry_after == 7.0

    @pytest.mark.parametrize(
        "body",
        [
            None,
            "not a dict",
            [1, 2, 3],
            {"retry_after": 1.0, "message": "m", "global": False},  # missing code
            {"retry_after": 1.0, "message": "m", "code": 1},  # missing global
            {"retry_after": 1.0, "global": False, "code": 1},  # missing message
            {"message": "m", "global": False, "code": 1},  # missing retry_after
            {"retry_after": "abc", "message": "m", "global": False, "code": 1},  # non-numeric
            {"retry_after": -1.0, "message": "m", "global": False, "code": 1},  # negative
            {"retry_after": 1.0, "message": 123, "global": False, "code": 1},  # wrong type
            {"retry_after": 1.0, "message": "m", "global": "yes", "code": 1},  # wrong type
            {"retry_after": 1.0, "message": "m", "global": False, "code": "nope"},  # wrong type
            {"retry_after": 1.0, "message": "m", "global": False, "code": True},  # bool != int
        ],
    )
    def test_malformed_body_raises_transport_error(self, body):
        with pytest.raises(TransportError):
            parse_rate_limit_response(429, body)

    def test_transport_error_is_value_error(self):
        with pytest.raises(ValueError):
            parse_rate_limit_response(429, None)


class TestIsRetriable:
    def test_429_within_window_is_retriable(self):
        assert is_retriable(429, "POST", 5.0) is True
        assert is_retriable(429, "GET", 60) is True
        assert is_retriable(429, "POST", "3.5") is True

    def test_429_outside_window_is_not_retriable(self):
        assert is_retriable(429, "GET", 60.5) is False
        assert is_retriable(429, "GET", 61) is False

    def test_429_without_retry_after_is_not_retriable(self):
        assert is_retriable(429, "GET", None) is False

    @pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
    def test_5xx_idempotent_methods_are_retriable(self, method):
        assert is_retriable(500, method, None) is True
        assert is_retriable(503, method.lower(), None) is True

    @pytest.mark.parametrize("method", ["POST", "PATCH"])
    def test_5xx_non_idempotent_methods_are_not_retriable(self, method):
        assert is_retriable(500, method, None) is False
        assert is_retriable(502, method, None) is False

    def test_5xx_ignores_retry_after(self):
        assert is_retriable(500, "GET", 999) is True

    @pytest.mark.parametrize("status", [400, 401, 403])
    @pytest.mark.parametrize("method", ["GET", "POST"])
    def test_client_errors_never_retriable(self, status, method):
        assert is_retriable(status, method, 5.0) is False

    def test_other_status_codes_not_retriable(self):
        assert is_retriable(200, "GET", None) is False
        assert is_retriable(404, "GET", None) is False


class _FakeClock:
    """Deterministic stand-in for time.monotonic."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestRouteBucket:
    def test_fresh_bucket_is_available(self):
        assert RouteBucket("channels/123/messages").available() is True

    def test_cooldown_makes_route_unavailable(self):
        clock = _FakeClock(now=1000.0)
        bucket = RouteBucket(route="channels/123/messages", clock=clock)
        bucket.apply_rate_limit(5.0)
        assert bucket.cooldown_until == 1005.0
        assert bucket.available() is False

    def test_route_becomes_available_after_cooldown_elapses(self):
        clock = _FakeClock(now=1000.0)
        bucket = RouteBucket(clock=clock)
        bucket.apply_rate_limit(5.0)
        clock.advance(4.9)
        assert bucket.available() is False
        clock.advance(0.2)
        assert bucket.available() is True

    def test_reset_clears_cooldown(self):
        clock = _FakeClock(now=1000.0)
        bucket = RouteBucket(clock=clock)
        bucket.apply_rate_limit(30.0)
        assert bucket.available() is False
        bucket.reset()
        assert bucket.available() is True
        assert bucket.cooldown_until == 0.0

    def test_new_rate_limit_overwrites_previous(self):
        clock = _FakeClock(now=1000.0)
        bucket = RouteBucket(clock=clock)
        bucket.apply_rate_limit(1.0)
        clock.advance(2.0)
        bucket.apply_rate_limit(10.0)
        assert bucket.cooldown_until == 1012.0
