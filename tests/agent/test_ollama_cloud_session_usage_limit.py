"""Tests for Ollama Cloud "session usage limit" credential pool handling.

Covers three fixes:
1. ``_extract_retry_delay_seconds`` recognising "session usage limit" and
   returning a 30-minute TTL (gated on status_code == 429) instead of
   falling through to the 1-hour default.
2. ``recover_with_credential_pool`` treating "session usage limit" as a
   usage-limit condition so the pool rotates on the first 429 instead of
   retrying the same credential 3 times.
3. ``classify_api_error`` classifying the 429 as ``billing`` so the pool
   rotates immediately via the billing recovery branch.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from agent.credential_pool import (
    PooledCredential,
    _extract_retry_delay_seconds,
    _exhausted_until,
    _normalize_error_context,
)
from agent.error_classifier import classify_api_error, FailoverReason


# ---------------------------------------------------------------------------
# Helpers (match conventions from test_credential_pool_interrupt.py)
# ---------------------------------------------------------------------------

def _make_entry(idx, **overrides):
    defaults = dict(
        provider="ollama-cloud",
        id=f"cred-{idx}",
        label=f"Credential {idx}",
        auth_type="api_key",
        priority=idx,
        source="manual",
        access_token=f"key-{idx}",
    )
    defaults.update(overrides)
    return PooledCredential(**defaults)


def _make_pool(entries):
    pool = MagicMock()
    pool.entries.return_value = entries
    pool.current.return_value = entries[0]
    # Must be set explicitly — MagicMock.provider returns a truthy
    # child mock, which would trigger the provider-mismatch guard.
    pool.provider = ""
    return pool


# ---------------------------------------------------------------------------
# _extract_retry_delay_seconds
# ---------------------------------------------------------------------------

def test_session_usage_limit_returns_30_minutes():
    msg = "you (user) have reached your session usage limit, upgrade for higher limits"
    assert _extract_retry_delay_seconds(msg, status_code=429) == 30 * 60


def test_session_usage_limit_case_insensitive():
    msg = "Session Usage Limit reached"
    assert _extract_retry_delay_seconds(msg, status_code=429) == 30 * 60


def test_session_usage_limit_does_not_shadow_explicit_reset_time():
    """If the message also contains an explicit 'resets in Nmin', that wins."""
    msg = "session usage limit. Resets in 5min"
    assert _extract_retry_delay_seconds(msg, status_code=429) == 5 * 60


def test_session_usage_limit_does_not_shadow_hr_min_format():
    """If the message also contains 'resets in Nhr Mmin', that wins."""
    msg = "session usage limit. Resets in 2hr 15min"
    assert _extract_retry_delay_seconds(msg, status_code=429) == 2 * 3600 + 15 * 60


def test_session_usage_limit_non_429_returns_none():
    """The 30-min TTL must NOT fire on non-429 errors (sweeper gate on #68832)."""
    msg = "you (user) have reached your session usage limit"
    assert _extract_retry_delay_seconds(msg, status_code=403) is None
    assert _extract_retry_delay_seconds(msg, status_code=None) is None


def test_non_session_usage_limit_returns_none():
    assert _extract_retry_delay_seconds("rate limited, try again later", status_code=429) is None
    assert _extract_retry_delay_seconds("", status_code=429) is None


# ---------------------------------------------------------------------------
# Persisted cooldown: _normalize_error_context → _exhausted_until
# ---------------------------------------------------------------------------

def test_persisted_cooldown_is_30min_for_session_usage_limit_429():
    """End-to-end: the normalized reset_at produces a ~30min cooldown."""
    ctx = _normalize_error_context(
        {"message": "you (user) have reached your session usage limit"},
        status_code=429,
    )
    assert "reset_at" in ctx
    now = time.time()
    delta = ctx["reset_at"] - now
    assert 29 * 60 < delta < 31 * 60


def test_persisted_cooldown_non_429_uses_default_ttl():
    """Without status_code, no custom reset_at is set (falls back to default TTL)."""
    ctx = _normalize_error_context(
        {"message": "you (user) have reached your session usage limit"},
        status_code=None,
    )
    assert "reset_at" not in ctx


def test_exhausted_until_uses_persisted_reset_at():
    """A pool entry with a persisted reset_at should honor it."""
    now = time.time()
    reset = now + 30 * 60
    entry = _make_entry(0, last_status="exhausted", last_status_at=now)
    entry.last_error_reset_at = reset
    result = _exhausted_until(entry)
    assert result is not None
    assert abs(result - reset) < 2


# ---------------------------------------------------------------------------
# error_classifier: 429 "session usage limit" → billing
# ---------------------------------------------------------------------------

class _FakeError(Exception):
    status_code = 429

    def __init__(self, msg):
        super().__init__(msg)


_OLLAMA_429_MSG = (
    "Error code: 429 - {'error': {'message': "
    "'you (seppe) have reached your session usage limit, "
    "upgrade for higher limits: https://ollama.com/upgrade"
    " or add extra usage: https://ollama.com/settings"
    " (ref: 64445a74-57b5-4de8-93ee-965b7b10ef47)', "
    "'type': 'api_error', 'param': None, 'code': None}}"
)


def test_classify_429_session_usage_limit_as_billing():
    """The 429 must classify as billing (rotate immediately, non-retryable)."""
    err = _FakeError(_OLLAMA_429_MSG)
    c = classify_api_error(err, provider="ollama-cloud", model="glm-5.2")
    assert c.reason == FailoverReason.billing
    assert c.should_rotate_credential is True
    assert c.retryable is False


def test_classify_429_plain_rate_limit_still_rate_limit():
    """A normal 429 without 'session usage limit' stays rate_limit."""
    err = _FakeError("Error code: 429 - Too many requests")
    c = classify_api_error(err, provider="openrouter", model="claude-sonnet-4")
    assert c.reason == FailoverReason.rate_limit


def test_classify_429_overloaded_still_overloaded():
    """Z.AI-style overload 429 still classifies as overloaded."""
    err = _FakeError("Error code: 429 - The server is overloaded")
    c = classify_api_error(err, provider="zai", model="glm-5.2")
    assert c.reason == FailoverReason.overloaded


# ---------------------------------------------------------------------------
# recover_with_credential_pool — usage_limit_reached detection
# ---------------------------------------------------------------------------

def test_session_usage_limit_triggers_pool_rotation_on_first_429():
    """On the first 429 with 'session usage limit', the pool should rotate
    immediately instead of waiting for has_retried_429=True."""
    entries = [_make_entry(0), _make_entry(1)]
    pool = _make_pool(entries)
    pool.mark_exhausted_and_rotate.return_value = entries[1]

    from run_agent import AIAgent
    with patch("run_agent.get_tool_definitions", return_value=[]), \
         patch("run_agent.check_toolset_requirements", return_value={}), \
         patch("run_agent.OpenAI"):
        agent = MagicMock(spec=AIAgent)
        agent._credential_pool = pool
        agent._swap_credential = MagicMock()

        error_context = {
            "reason": "",
            "message": "you (user) have reached your session usage limit, "
                       "upgrade for higher limits",
        }

        recovered, retried = AIAgent._recover_with_credential_pool(
            agent,
            status_code=429,
            has_retried_429=False,
            classified_reason=None,
            error_context=error_context,
        )

    assert recovered is True
    assert retried is False
    pool.mark_exhausted_and_rotate.assert_called_once()
    agent._swap_credential.assert_called_once_with(entries[1])


def test_plain_rate_limit_without_usage_limit_waits_for_retry():
    """A normal 429 without 'session usage limit' should NOT rotate on the
    first 429 — it should retry first (has_retried_429=False → retried=True)."""
    entries = [_make_entry(0), _make_entry(1)]
    pool = _make_pool(entries)

    from run_agent import AIAgent
    with patch("run_agent.get_tool_definitions", return_value=[]), \
         patch("run_agent.check_toolset_requirements", return_value={}), \
         patch("run_agent.OpenAI"):
        agent = MagicMock(spec=AIAgent)
        agent._credential_pool = pool

        error_context = {
            "reason": "rate_limit",
            "message": "Too many requests, try again later",
        }

        recovered, retried = AIAgent._recover_with_credential_pool(
            agent,
            status_code=429,
            has_retried_429=False,
            classified_reason=None,
            error_context=error_context,
        )

    assert recovered is False
    assert retried is True
    pool.mark_exhausted_and_rotate.assert_not_called()
