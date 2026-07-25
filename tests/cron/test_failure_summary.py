"""Behavioral coverage for cron delivery failure classification."""

import pytest

from cron.scheduler import _summarize_cron_failure_for_delivery


@pytest.mark.parametrize(
    "output",
    [
        "PASS auth gate returns 401\nFAIL broken file link",
        "PASS quota endpoint returns 429\nFAIL broken file link",
        "PASS timeout handler test\nFAIL broken file link",
    ],
)
def test_no_agent_failure_does_not_use_provider_classifiers(output):
    message = _summarize_cron_failure_for_delivery(
        {"name": "verify", "no_agent": True}, output
    )

    assert "provider" not in message.lower()
    assert "broken file link" in message


def test_agent_failure_still_classifies_provider_authentication():
    message = _summarize_cron_failure_for_delivery(
        {"name": "agent job", "no_agent": False}, "HTTP 401 unauthorized"
    )

    assert "provider authentication error" in message
