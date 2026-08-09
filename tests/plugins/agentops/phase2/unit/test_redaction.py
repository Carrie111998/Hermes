from datetime import datetime, timezone

import pytest

from plugins.agentops.control.observer_models import RawSignal
from plugins.agentops.control.redaction import RedactionError, contains_secret, redact_signal, verify_redacted_signal


def _raw(payload):
    return RawSignal(
        target_id="hermes:profile:default:gateway",
        collector="test.collector",
        signal_type="signal.test",
        observed_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
        payload=payload,
    )


def test_redaction_removes_token_cookie_and_sensitive_field_names_without_removing_user_text():
    signal = redact_signal(
        _raw(
            {
                "api_token": "sk-test-canary-secret-123456",
                "cookie": "session=example-cookie-value",
                "message": "Molly's user-provided content remains useful.",
            }
        )
    )

    assert signal.payload["message"] == "Molly's user-provided content remains useful."
    assert "api_token" not in signal.payload
    assert "cookie" not in signal.payload
    assert not contains_secret(signal.to_dict())
    verify_redacted_signal(signal)


def test_redaction_gate_does_not_allow_an_unredacted_signal():
    signal = redact_signal(_raw({"message": "ok"}))
    object.__setattr__(signal, "payload", {"token": "sk-test-canary-secret-123456"})

    with pytest.raises(RedactionError):
        verify_redacted_signal(signal)
