import pytest

from plugins.memory.obsidian_duo.security import (
    assert_safe_to_persist,
    redact_secrets,
    scan_for_secrets,
)


@pytest.mark.parametrize(
    "text",
    [
        "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
        "token=ghp_1234567890abcdefghijklmnop",
        "GITHUB_TOKEN=ghp_1234567890abcdefghijklmnop",
        "API_KEY=sk-proj-1234567890abcdefghijklmnop",
        "client_secret=Zx9Qv7Lm2Nw4Rt6Yp8Ks3Hd5Fg7Jk9Lm",
    ],
)
def test_secret_patterns_are_blocked(text):
    result = scan_for_secrets(text)

    assert result.matches
    with pytest.raises(ValueError, match="secret credentials detected"):
        assert_safe_to_persist(text)
    assert redact_secrets(text) != text


def test_non_secret_identifiers_are_allowed():
    text = "uuid=550e8400-e29b-41d4-a716-446655440000 sha=0123456789abcdef0123456789abcdef01234567"

    assert not scan_for_secrets(text).matches
    assert_safe_to_persist(text)


def test_rejection_never_logs_raw_secret(caplog):
    secret = "GITHUB_TOKEN=ghp_1234567890abcdefghijklmnop"

    with pytest.raises(ValueError):
        assert_safe_to_persist(secret)

    assert all(secret not in record.getMessage() for record in caplog.records)
