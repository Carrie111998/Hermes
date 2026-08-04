"""Config contract for fallback-entry failure policies."""
from hermes_cli.fallback_config import (
    FALLBACK_FAILURE_POLICY_CONTINUE,
    FALLBACK_FAILURE_POLICY_TRIAGE_AND_NOTIFY,
    fallback_entry_allows_continuation,
    fallback_failure_policy,
)


def test_absent_failure_policy_defaults_to_existing_continuation_behavior():
    entry = {"provider": "custom", "model": "local"}
    assert fallback_failure_policy(entry) == FALLBACK_FAILURE_POLICY_CONTINUE
    assert fallback_entry_allows_continuation(entry) is True


def test_triage_and_notify_is_a_supported_per_fallback_config_policy():
    entry = {
        "provider": "custom",
        "model": "local",
        "failure_policy": "triage_and_notify",
    }
    assert fallback_failure_policy(entry) == FALLBACK_FAILURE_POLICY_TRIAGE_AND_NOTIFY
    assert fallback_entry_allows_continuation(entry) is False


def test_unknown_failure_policy_fails_open_to_backwards_compatible_continuation():
    entry = {"provider": "custom", "model": "local", "failure_policy": "unknown"}
    assert fallback_failure_policy(entry) == FALLBACK_FAILURE_POLICY_CONTINUE
    assert fallback_entry_allows_continuation(entry) is True
