"""Config contract for fallback-entry failure policies."""
import pytest

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


def test_explicit_continue_preserves_existing_continuation_behavior():
    entry = {
        "provider": "custom",
        "model": "local",
        "failure_policy": "continue",
    }
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


@pytest.mark.parametrize(
    "invalid_value",
    ["unknown", "triage_and_notfiy", "", "   ", None, 42, False, []],
)
def test_present_malformed_failure_policy_is_invalid_and_cannot_continue(invalid_value):
    entry = {
        "provider": "custom",
        "model": "local",
        "failure_policy": invalid_value,
    }
    assert fallback_failure_policy(entry) == "invalid"
    assert fallback_entry_allows_continuation(entry) is False
