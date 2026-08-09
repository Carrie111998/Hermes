"""Anthropic OAuth billing-shaped 400 vs. real subscription exhaustion (#82154).

Anthropic's subscription endpoint answers a *content* rejection with its
billing sentence ("You're out of extra usage. Add more at
claude.ai/settings/usage and keep going.") on an HTTP 400 — byte-for-byte what
a genuinely depleted account gets. Message matching cannot separate the two,
so Hermes asks the account: the OAuth usage endpoint reports the very quota the
message claims is gone.

These tests pin both halves: the pure decision table over a usage payload, and
the one-directional reclassification that only ever *clears* a false billing
verdict.
"""

import pytest

from agent import account_usage
from agent.conversation_loop import _reclassify_verified_anthropic_content_filter
from agent.error_classifier import ClassifiedError, FailoverReason


@pytest.fixture(autouse=True)
def _clear_capacity_cache():
    account_usage._capacity_cache.clear()
    yield
    account_usage._capacity_cache.clear()


def _window(utilization):
    return {"utilization": utilization, "resets_at": "2026-08-09T12:00:00Z"}


def _billing_400(**overrides):
    kwargs = {
        "reason": FailoverReason.billing,
        "status_code": 400,
        "provider": "anthropic",
        "model": "claude-opus-5",
        "message": "You're out of extra usage. Add more at claude.ai/settings/usage and keep going.",
        "retryable": False,
        "should_rotate_credential": True,
        "should_fallback": True,
    }
    kwargs.update(overrides)
    return ClassifiedError(**kwargs)


class _Agent:
    def __init__(self, provider="anthropic"):
        self.provider = provider


# ── Decision table over a usage payload ─────────────────────────────────

def test_extra_usage_credits_remaining_means_capacity_available():
    """Money left in the overage bucket — "out of extra usage" cannot be literal."""
    payload = {
        "five_hour": _window(1.0),  # plan window fully spent
        "extra_usage": {"is_enabled": True, "used_credits": 3.5, "monthly_limit": 25.0},
    }
    assert account_usage._capacity_exhausted_from_usage(payload) is False


def test_extra_usage_credits_spent_means_exhausted():
    payload = {
        "five_hour": _window(0.2),
        "extra_usage": {"is_enabled": True, "used_credits": 25.0, "monthly_limit": 25.0},
    }
    assert account_usage._capacity_exhausted_from_usage(payload) is True


def test_plan_windows_below_cap_mean_capacity_available():
    payload = {"five_hour": _window(0.42), "seven_day": _window(0.10)}
    assert account_usage._capacity_exhausted_from_usage(payload) is False


def test_any_saturated_plan_window_means_exhausted():
    payload = {"five_hour": _window(0.42), "seven_day_opus": _window(1.0)}
    assert account_usage._capacity_exhausted_from_usage(payload) is True


def test_percentage_encoded_utilization_is_not_rescaled():
    """Values above 1 are already percentages — 45 is 45%, not 4500%."""
    payload = {"five_hour": _window(45)}
    assert account_usage._capacity_exhausted_from_usage(payload) is False


def test_payload_without_usage_fields_is_unknown():
    assert account_usage._capacity_exhausted_from_usage({}) is None
    assert account_usage._capacity_exhausted_from_usage({"five_hour": {}}) is None


def test_extra_usage_without_numbers_falls_through_to_windows():
    payload = {
        "extra_usage": {"is_enabled": True},
        "seven_day": _window(1.0),
    }
    assert account_usage._capacity_exhausted_from_usage(payload) is True


# ── Probe gating, caching, and failure handling ─────────────────────────

def test_api_key_credentials_are_never_probed(monkeypatch):
    """An sk-ant-api… key never meets the subscription classifier."""
    calls = []
    monkeypatch.setattr(account_usage, "resolve_anthropic_token", lambda: "sk-ant-api03-xxx")
    monkeypatch.setattr(account_usage, "_is_oauth_token", lambda _t: False)
    monkeypatch.setattr(
        account_usage, "_fetch_anthropic_usage_payload",
        lambda *a, **k: calls.append(1) or {},
    )

    assert account_usage.anthropic_subscription_capacity_exhausted() is None
    assert calls == []


def test_probe_failure_is_unknown_and_not_cached(monkeypatch):
    """A transient failure must not lock in a verdict — the next attempt retries."""
    attempts = []

    def _boom(*_a, **_k):
        attempts.append(1)
        raise RuntimeError("connection reset")

    monkeypatch.setattr(account_usage, "resolve_anthropic_token", lambda: "sk-ant-oat01-xxx")
    monkeypatch.setattr(account_usage, "_is_oauth_token", lambda _t: True)
    monkeypatch.setattr(account_usage, "_fetch_anthropic_usage_payload", _boom)

    assert account_usage.anthropic_subscription_capacity_exhausted() is None
    assert account_usage.anthropic_subscription_capacity_exhausted() is None
    assert len(attempts) == 2
    assert account_usage._capacity_cache == {}


def test_verdict_is_cached_within_the_ttl(monkeypatch):
    payload = {"five_hour": _window(0.1)}
    fetches = []

    monkeypatch.setattr(account_usage, "resolve_anthropic_token", lambda: "sk-ant-oat01-xxx")
    monkeypatch.setattr(account_usage, "_is_oauth_token", lambda _t: True)
    monkeypatch.setattr(
        account_usage, "_fetch_anthropic_usage_payload",
        lambda *a, **k: fetches.append(1) or payload,
    )

    assert account_usage.anthropic_subscription_capacity_exhausted() is False
    assert account_usage.anthropic_subscription_capacity_exhausted() is False
    assert len(fetches) == 1


# ── Reclassification is one-directional ─────────────────────────────────

def _force_capacity(monkeypatch, verdict):
    monkeypatch.setattr(
        account_usage, "anthropic_subscription_capacity_exhausted", lambda: verdict
    )


def test_available_capacity_reclassifies_and_spares_the_credential(monkeypatch):
    _force_capacity(monkeypatch, False)
    original = _billing_400()

    refined = _reclassify_verified_anthropic_content_filter(_Agent(), original)

    assert refined.reason is FailoverReason.content_policy_blocked
    assert refined.should_rotate_credential is False
    assert refined.error_context["anthropic_oauth_content_filter"] is True
    # Non-retryable + fallback-eligible semantics are preserved.
    assert refined.retryable is False
    assert refined.should_fallback is True
    # The input verdict is left untouched for any other reader.
    assert original.reason is FailoverReason.billing
    assert original.should_rotate_credential is True


@pytest.mark.parametrize("verdict", [True, None])
def test_exhausted_or_unknown_capacity_keeps_the_billing_verdict(monkeypatch, verdict):
    _force_capacity(monkeypatch, verdict)

    refined = _reclassify_verified_anthropic_content_filter(_Agent(), _billing_400())

    assert refined.reason is FailoverReason.billing
    assert refined.should_rotate_credential is True


def test_probe_errors_keep_the_billing_verdict(monkeypatch):
    def _boom():
        raise RuntimeError("usage endpoint down")

    monkeypatch.setattr(account_usage, "anthropic_subscription_capacity_exhausted", _boom)

    refined = _reclassify_verified_anthropic_content_filter(_Agent(), _billing_400())

    assert refined.reason is FailoverReason.billing


def test_other_providers_are_never_probed(monkeypatch):
    calls = []
    monkeypatch.setattr(
        account_usage, "anthropic_subscription_capacity_exhausted",
        lambda: calls.append(1) or False,
    )

    refined = _reclassify_verified_anthropic_content_filter(
        _Agent(provider="openrouter"), _billing_400(provider="openrouter")
    )

    assert refined.reason is FailoverReason.billing
    assert calls == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"status_code": 402},  # a real payment-required wall, not the 400 shape
        {"reason": FailoverReason.rate_limit},
        {"reason": FailoverReason.auth},
    ],
)
def test_only_the_billing_400_shape_is_fact_checked(monkeypatch, overrides):
    calls = []
    monkeypatch.setattr(
        account_usage, "anthropic_subscription_capacity_exhausted",
        lambda: calls.append(1) or False,
    )

    original = _billing_400(**overrides)
    refined = _reclassify_verified_anthropic_content_filter(_Agent(), original)

    assert refined is original
    assert calls == []
