"""Tests for agent.retry_utils jittered backoff."""

import threading

import pytest

import agent.retry_utils as retry_utils
from types import SimpleNamespace

from agent.retry_utils import (
    adaptive_rate_limit_backoff,
    is_zai_coding_overload_error,
    jittered_backoff,
    provider_retry_after_seconds,
)


def test_backoff_is_exponential():
    """Base delay should double each attempt (before jitter)."""
    for attempt in (1, 2, 3, 4):
        delays = [jittered_backoff(attempt, base_delay=5.0, max_delay=120.0, jitter_ratio=0.0) for _ in range(100)]
        expected = min(5.0 * (2 ** (attempt - 1)), 120.0)
        mean = sum(delays) / len(delays)
        assert abs(mean - expected) < 0.01, f"attempt {attempt}: expected {expected}, got {mean}"


def test_backoff_respects_max_delay():
    """Even with high attempt numbers, delay should not exceed max_delay."""
    for attempt in (10, 20, 100):
        delay = jittered_backoff(attempt, base_delay=5.0, max_delay=60.0, jitter_ratio=0.0)
        assert delay <= 60.0, f"attempt {attempt}: delay {delay} exceeds max 60s"




def test_backoff_attempt_1_is_base():
    """First attempt delay should equal base_delay (with no jitter)."""
    delay = jittered_backoff(1, base_delay=3.0, max_delay=120.0, jitter_ratio=0.0)
    assert delay == 3.0








def test_backoff_thread_safety():
    """Concurrent calls should generally produce different delays."""
    results = []
    barrier = threading.Barrier(8)

    def _call_backoff():
        barrier.wait()
        results.append(jittered_backoff(1, base_delay=10.0, max_delay=120.0, jitter_ratio=0.5))

    threads = [threading.Thread(target=_call_backoff) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(results) == 8
    unique = len(set(results))
    assert unique >= 6, f"Expected mostly unique delays, got {unique}/8 unique"


def test_backoff_uses_locked_tick_for_seed(monkeypatch):
    """Seed derivation should use per-call tick captured under lock."""
    import time

    monkeypatch.setattr(retry_utils, "_jitter_counter", 0)

    recorded_seeds = []

    class _RecordingRandom:
        def __init__(self, seed):
            recorded_seeds.append(seed)

        def uniform(self, a, b):
            return 0.0

    monkeypatch.setattr(retry_utils.random, "Random", _RecordingRandom)

    fixed_time_ns = 123456789

    def _time_ns_wait_for_two_ticks():
        deadline = time.time() + 2.0
        while retry_utils._jitter_counter < 2 and time.time() < deadline:
            time.sleep(0.001)
        return fixed_time_ns

    monkeypatch.setattr(retry_utils.time, "time_ns", _time_ns_wait_for_two_ticks)

    barrier = threading.Barrier(2)

    def _call():
        barrier.wait()
        jittered_backoff(1, base_delay=10.0, max_delay=120.0, jitter_ratio=0.5)

    threads = [threading.Thread(target=_call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(recorded_seeds) == 2
    assert len(set(recorded_seeds)) == 2, f"Expected unique seeds, got {recorded_seeds}"


def _zai_overload_error():
    return SimpleNamespace(
        status_code=429,
        body={
            "error": {
                "code": "1305",
                "message": "The service may be temporarily overloaded, please try again later",
            }
        },
    )










def test_zai_overload_retry_ceiling_exceeds_short_attempts():
    """Invariant: the ceiling must sit above the short-retry threshold, or the
    long-backoff tier is unreachable and the whole schedule is dead code
    (the original bug: default api_max_retries == short_attempts == 3)."""
    from agent.retry_utils import (
        zai_coding_overload_retry_ceiling,
        _ZAI_CODING_OVERLOAD_LONG_BACKOFF,
    )

    short_attempts = 3
    ceiling = zai_coding_overload_retry_ceiling(short_attempts)
    assert ceiling > short_attempts
    # Invariant (not a formula mirror): the loop's give-up check
    # (retry_count >= ceiling) runs *before* the attempt's backoff, so the
    # ceiling must leave headroom for every long-backoff entry to execute —
    # i.e. the largest attempt the loop still computes backoff for
    # (ceiling - 1) must reach the final long-tier index.
    last_attempt_with_backoff = ceiling - 1
    assert last_attempt_with_backoff - short_attempts >= len(_ZAI_CODING_OVERLOAD_LONG_BACKOFF)


def test_zai_overload_ceiling_makes_long_tier_reachable(monkeypatch):
    """End-to-end over the attempt range the retry loop actually walks: with the
    extended ceiling, at least one attempt reaches the long-backoff tier and the
    full 30/60/90/120s schedule is exercised."""
    monkeypatch.setattr(retry_utils, "jittered_backoff", lambda *a, **kw: kw["base_delay"])
    from agent.retry_utils import zai_coding_overload_retry_ceiling

    err = _zai_overload_error()
    ceiling = zai_coding_overload_retry_ceiling()

    long_waits = []
    # The loop computes backoff for attempts 1..ceiling-1 (it gives up at ceiling).
    for attempt in range(1, ceiling):
        _wait, policy = adaptive_rate_limit_backoff(
            attempt,
            base_url="https://api.z.ai/api/coding/paas/v4",
            model="glm-5.2",
            error=err,
            default_wait=1.0,
        )
        if policy == "zai_coding_overload_long":
            long_waits.append(_wait)

    assert long_waits, "long-backoff tier never reached within the retry ceiling"
    assert long_waits == [30.0, 60.0, 90.0, 120.0]


# ---------------------------------------------------------------------------
# parse_retry_after_seconds — shared Retry-After parser
# ---------------------------------------------------------------------------


class TestParseRetryAfterSeconds:
    def test_numeric_string(self):
        from agent.retry_utils import parse_retry_after_seconds
        assert parse_retry_after_seconds("120") == 120.0
        assert parse_retry_after_seconds(" 4.5 ") == 4.5

    def test_numeric_value(self):
        from agent.retry_utils import parse_retry_after_seconds
        assert parse_retry_after_seconds(45) == 45.0
        assert parse_retry_after_seconds(3.25) == 3.25


    def test_http_date(self):
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime
        from agent.retry_utils import parse_retry_after_seconds

        future = datetime.now(timezone.utc) + timedelta(seconds=90)
        seconds = parse_retry_after_seconds(format_datetime(future, usegmt=True))
        assert seconds is not None and 80 <= seconds <= 91

        past = datetime.now(timezone.utc) - timedelta(seconds=90)
        assert parse_retry_after_seconds(format_datetime(past, usegmt=True)) == 0.0



    def test_headers_get_raises(self):
        from agent.retry_utils import parse_retry_after_seconds

        class Explosive:
            def get(self, _key):
                raise RuntimeError("boom")

        assert parse_retry_after_seconds(Explosive()) is None


def test_provider_retry_after_seconds_reads_gemini_retry_message():
    error = SimpleNamespace(
        status_code=429,
        body={"error": {"message": "Resource exhausted. Please retry in 36.3s."}},
        response=SimpleNamespace(headers={}),
    )
    assert provider_retry_after_seconds(error) == pytest.approx(36.3)


def test_provider_retry_hint_uses_response_status_and_text_shape():
    response = SimpleNamespace(
        status_code=429,
        headers={},
        text='{"error":{"message":"Please retry in 58.7s."}}',
    )
    error = SimpleNamespace(status_code=None, body=None, response=response)
    assert provider_retry_after_seconds(error) == pytest.approx(58.7)


def test_provider_retry_after_seconds_prefers_retry_after_header_for_503():
    error = SimpleNamespace(
        status_code=503,
        body={"error": {"message": "UNAVAILABLE"}},
        response=SimpleNamespace(headers={"Retry-After": "12.5"}),
    )
    assert provider_retry_after_seconds(error) == pytest.approx(12.5)


# ---------------------------------------------------------------------------
# is_daily_quota_exhaustion — distinguishing a per-DAY cap from a per-minute
# rate limit so the fallback-suppressing retry-after hint doesn't strand a
# session on a provider that cannot recover within the retry window.
# ---------------------------------------------------------------------------


class TestIsDailyQuotaExhaustion:
    def test_gemini_free_tier_daily_cap_detected(self):
        """Real body shape captured from a Gemini free-tier RESOURCE_EXHAUSTED
        429 — includes a short retry hint that would otherwise suppress
        fallback even though the daily cap won't clear in that window."""
        from agent.retry_utils import is_daily_quota_exhaustion

        error = SimpleNamespace(
            status_code=429,
            body={
                "error": {
                    "code": 429,
                    "status": "RESOURCE_EXHAUSTED",
                    "message": (
                        "You exceeded your current quota, please check your plan "
                        "and billing details. * Quota exceeded for metric: "
                        "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
                        "limit: 500, model: gemini-3.5-flash-lite. Please retry in 59.468589331s."
                    ),
                }
            },
            response=SimpleNamespace(headers={}),
        )
        assert is_daily_quota_exhaustion(error) is True

    def test_per_minute_rate_limit_not_flagged_as_daily(self):
        """A generic per-minute 429 (no per-day quota metric in the body)
        should NOT be classified as daily-quota exhaustion — the short
        retry-after hint is trustworthy there and fallback should stay
        gated on it as before."""
        from agent.retry_utils import is_daily_quota_exhaustion

        error = SimpleNamespace(
            status_code=429,
            body={"error": {"message": "Resource exhausted. Please retry in 6.5s."}},
            response=SimpleNamespace(headers={}),
        )
        assert is_daily_quota_exhaustion(error) is False

    def test_non_quota_error_not_flagged(self):
        from agent.retry_utils import is_daily_quota_exhaustion

        error = SimpleNamespace(
            status_code=500,
            body={"error": {"message": "internal error"}},
            response=SimpleNamespace(headers={}),
        )
        assert is_daily_quota_exhaustion(error) is False

    def test_string_error_text_detected(self):
        """Callers also probe the flattened lowercased error message string
        directly (conversation_loop.py passes both the exception and
        error_msg through this check)."""
        from agent.retry_utils import is_daily_quota_exhaustion

        msg = (
            "gemini http 429 (resource_exhausted): quota exceeded for metric: "
            "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
            "limit: 500, model: gemini-3.1-flash-lite. please retry in 12.0s."
        )
        assert is_daily_quota_exhaustion(msg) is True
