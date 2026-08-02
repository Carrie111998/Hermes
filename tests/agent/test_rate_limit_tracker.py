"""Tests for agent.rate_limit_tracker — header parsing and formatting."""
import json
import time
from datetime import datetime, timezone

import pytest
from agent.rate_limit_tracker import (
    RateLimitBucket,
    RateLimitState,
    parse_rate_limit_headers,
    format_rate_limit_display,
    format_rate_limit_compact,
    _fmt_count,
    _fmt_seconds,
    _bar,
)


# ── Sample headers from Nous inference API ──────────────────────────────

NOUS_HEADERS = {
    "x-ratelimit-limit-requests": "800",
    "x-ratelimit-limit-requests-1h": "33600",
    "x-ratelimit-limit-tokens": "8000000",
    "x-ratelimit-limit-tokens-1h": "336000000",
    "x-ratelimit-remaining-requests": "795",
    "x-ratelimit-remaining-requests-1h": "33590",
    "x-ratelimit-remaining-tokens": "7999500",
    "x-ratelimit-remaining-tokens-1h": "335999000",
    "x-ratelimit-reset-requests": "45.5",
    "x-ratelimit-reset-requests-1h": "3500.0",
    "x-ratelimit-reset-tokens": "42.3",
    "x-ratelimit-reset-tokens-1h": "3490.0",
}


class TestParseHeaders:
    def test_basic_parsing(self):
        state = parse_rate_limit_headers(NOUS_HEADERS, provider="nous")
        assert state is not None
        assert state.provider == "nous"
        assert state.has_data

        assert state.requests_min.limit == 800
        assert state.requests_min.remaining == 795
        assert state.requests_min.reset_seconds == 45.5

        assert state.requests_hour.limit == 33600
        assert state.requests_hour.remaining == 33590

        assert state.tokens_min.limit == 8000000
        assert state.tokens_min.remaining == 7999500

        assert state.tokens_hour.limit == 336000000
        assert state.tokens_hour.remaining == 335999000
        assert state.tokens_hour.reset_seconds == 3490.0

    def test_no_headers(self):
        state = parse_rate_limit_headers({})
        assert state is None





class TestBucket:
    def test_snapshot_serialization_is_json_safe(self, monkeypatch):
        captured_at = 1_700_000_000.0
        monkeypatch.setattr(time, "time", lambda: captured_at + 15)
        state = RateLimitState(
            requests_min=RateLimitBucket(limit=100, remaining=75, reset_seconds=60, captured_at=captured_at),
            requests_hour=RateLimitBucket(limit=1000, remaining=900, reset_seconds=3600, captured_at=captured_at),
            captured_at=captured_at,
            provider="xai-oauth",
        )

        from agent.rate_limit_tracker import rate_limit_state_to_dict

        payload = rate_limit_state_to_dict(state)

        assert payload is not None
        assert set(payload) == {"provider", "captured_at", "age_seconds", "available", "buckets"}
        assert payload["provider"] == "xai-oauth"
        assert payload["captured_at"] == datetime.fromtimestamp(captured_at, tz=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        assert payload["age_seconds"] == pytest.approx(15)
        assert payload["available"] is True
        assert set(payload["buckets"]) == {"requests_min", "requests_hour", "tokens_min", "tokens_hour"}
        request_bucket = payload["buckets"]["requests_min"]
        assert set(request_bucket) == {"limit", "remaining", "used", "used_percent", "reset_at"}
        assert request_bucket == {
            "limit": 100,
            "remaining": 75,
            "used": 25,
            "used_percent": 25.0,
            "reset_at": datetime.fromtimestamp(captured_at + 60, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        json.dumps(payload, allow_nan=False)

    def test_snapshot_serialization_handles_none_and_non_finite_values(self):
        from agent.rate_limit_tracker import rate_limit_state_to_dict

        assert rate_limit_state_to_dict(None) is None
        state = RateLimitState(
            requests_min=RateLimitBucket(limit=100, remaining=20, reset_seconds=float("inf"), captured_at=1.0),
            captured_at=1.0,
            provider="xai",
        )
        payload = rate_limit_state_to_dict(state)
        assert payload["buckets"]["requests_min"]["reset_at"] is None
        json.dumps(payload, allow_nan=False)

    def test_usage_pct(self):
        b = RateLimitBucket(limit=100, remaining=20, reset_seconds=30.0, captured_at=time.time())
        assert b.usage_pct == pytest.approx(80.0)


    def test_remaining_seconds_now(self):
        now = time.time()
        b = RateLimitBucket(limit=800, remaining=795, reset_seconds=60.0, captured_at=now - 10)
        # ~50 seconds should remain
        assert 49 <= b.remaining_seconds_now <= 51



class TestFormatting:



    def test_fmt_seconds_short(self):
        assert _fmt_seconds(45) == "45s"
        assert _fmt_seconds(0) == "0s"



    def test_bar(self):
        bar = _bar(50.0, width=10)
        assert bar == "[█████░░░░░]"
        assert _bar(0.0, width=10) == "[░░░░░░░░░░]"
        assert _bar(100.0, width=10) == "[██████████]"




    def test_format_compact(self):
        state = parse_rate_limit_headers(NOUS_HEADERS, provider="nous")
        result = format_rate_limit_compact(state)
        assert "RPM:" in result
        assert "RPH:" in result
        assert "TPM:" in result
        assert "TPH:" in result
        assert "resets" in result



class TestAgentIntegration:
    """Test that AIAgent captures rate limit state correctly."""

    def test_capture_rate_limits_from_headers(self):
        """Simulate the header capture path without a real API call."""
        # Use a mock httpx-like response
        class MockResponse:
            headers = NOUS_HEADERS

        # Import AIAgent minimally

        # Test the parsing directly
        state = parse_rate_limit_headers(MockResponse.headers, provider="nous")
        assert state is not None
        assert state.requests_min.limit == 800
        assert state.tokens_hour.limit == 336000000

    def test_capture_rate_limits_none_response(self):
        """_capture_rate_limits should handle None gracefully."""
        from agent.rate_limit_tracker import parse_rate_limit_headers
        # None should not crash
        result = parse_rate_limit_headers({})
        assert result is None
