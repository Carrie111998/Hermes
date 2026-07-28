"""Credential-safe verbose initialization banners."""

import pytest

from agent.agent_init import _format_auth_credential_status


@pytest.mark.parametrize(
    ("credential", "expected"),
    [
        ("x", "🔑 Authentication credential: present"),
        ("short-value", "🔑 Authentication credential: present"),
        ("long-enough-to-have-been-previewed", "🔑 Authentication credential: present"),
        (None, "⚠️  Authentication credential: missing or not required"),
        ("", "⚠️  Authentication credential: missing or not required"),
        ("dummy-key", "⚠️  Authentication credential: missing or not required"),
        ("no-key-required", "⚠️  Authentication credential: missing or not required"),
    ],
)
def test_formats_only_credential_presence(credential, expected):
    assert _format_auth_credential_status(credential) == expected


def test_formats_managed_identity_as_static_source():
    assert _format_auth_credential_status(
        object(), uses_entra_id=True
    ) == "🔑 Authentication credential source: Microsoft Entra ID"
