"""Tests for the LdapAuthProvider plugin (LDAP bind auth, signed sessions).

Loads the plugin module directly (bundled backend plugin, not a package on
the import path) and exercises construction validation, the stateless
session-token lifecycle, the two bind modes (via ldap3 MOCK_SYNC), group
restriction, refresh directory checks, and the register(ctx) entry point.
"""

from __future__ import annotations

import secrets
import time
from unittest.mock import MagicMock

import pytest

import plugins.dashboard_auth.ldap as ldap_plugin
from hermes_cli.dashboard_auth import (
    InvalidCredentialsError,
    ProviderError,
    RefreshExpiredError,
    assert_protocol_compliance,
)

SECRET = secrets.token_bytes(32)


def make_provider(**overrides):
    kwargs = dict(
        server_url="ldaps://ldap.example.com",
        secret=SECRET,
        user_dn_template="uid={username},ou=people,dc=example,dc=com",
    )
    kwargs.update(overrides)
    return ldap_plugin.LdapAuthProvider(**kwargs)


class TestProtocolAndConstruction:
    def test_protocol_compliance(self):
        assert_protocol_compliance(ldap_plugin.LdapAuthProvider)

    def test_supports_password_flag(self):
        p = make_provider()
        assert p.supports_password is True
        assert p.name == "ldap"

    def test_oauth_methods_are_stubs(self):
        p = make_provider()
        with pytest.raises(NotImplementedError):
            p.start_login(redirect_uri="http://x/cb")
        with pytest.raises(NotImplementedError):
            p.complete_login(code="c", state="s", code_verifier="v",
                             redirect_uri="http://x/cb")

    def test_rejects_short_secret(self):
        with pytest.raises(ValueError, match="secret"):
            make_provider(secret=b"short")

    def test_rejects_bad_scheme(self):
        with pytest.raises(ValueError, match="server_url"):
            make_provider(server_url="http://ldap.example.com")

    def test_rejects_plain_ldap_without_tls(self):
        with pytest.raises(ValueError, match="allow_insecure"):
            make_provider(server_url="ldap://ldap.example.com")

    def test_plain_ldap_allowed_with_start_tls(self):
        make_provider(server_url="ldap://ldap.example.com", start_tls=True)

    def test_plain_ldap_allowed_with_allow_insecure(self):
        make_provider(server_url="ldap://ldap.example.com",
                      allow_insecure=True)

    def test_rejects_both_bind_modes(self):
        with pytest.raises(ValueError, match="mutually exclusive"):
            make_provider(
                user_dn_template="uid={username},dc=example,dc=com",
                user_search_base="ou=people,dc=example,dc=com",
            )

    def test_rejects_no_bind_mode(self):
        with pytest.raises(ValueError, match="bind mode"):
            make_provider(user_dn_template="")

    def test_rejects_template_without_placeholder(self):
        with pytest.raises(ValueError, match="{username}"):
            make_provider(user_dn_template="uid=admin,dc=example,dc=com")


class TestSessionTokens:
    def test_mint_verify_roundtrip(self):
        p = make_provider()
        s = p._mint_session("alice", "uid=alice,ou=people,dc=example,dc=com",
                            {"email": "alice@example.com", "display": "Alice"})
        assert s.provider == "ldap"
        assert s.user_id == "alice"
        assert s.email == "alice@example.com"
        assert s.display_name == "Alice"
        got = p.verify_session(access_token=s.access_token)
        assert got is not None
        assert got.user_id == "alice"
        assert got.email == "alice@example.com"

    def test_verify_rejects_tampered_token(self):
        p = make_provider()
        s = p._mint_session("alice", "uid=alice,dc=example,dc=com", {})
        assert p.verify_session(access_token=s.access_token[:-4] + "AAAA") is None

    def test_verify_rejects_wrong_secret(self):
        p1 = make_provider()
        p2 = make_provider(secret=secrets.token_bytes(32))
        s = p1._mint_session("alice", "uid=alice,dc=example,dc=com", {})
        assert p2.verify_session(access_token=s.access_token) is None

    def test_verify_rejects_refresh_token_as_access(self):
        p = make_provider()
        s = p._mint_session("alice", "uid=alice,dc=example,dc=com", {})
        assert p.verify_session(access_token=s.refresh_token) is None

    def test_verify_rejects_expired(self):
        p = make_provider(session_ttl_seconds=60)
        payload = {"sub": "alice", "dn": "x", "em": "", "nm": "alice",
                   "kind": "access", "exp": int(time.time()) - 1}
        token = ldap_plugin._sign(payload, SECRET)
        assert p.verify_session(access_token=token) is None

    def test_refresh_token_only_in_direct_mode(self):
        # Direct mode has no service credentials → refresh is token-only.
        p = make_provider()
        s = p._mint_session("alice", "uid=alice,dc=example,dc=com",
                            {"email": "a@x.com", "display": "Alice"})
        s2 = p.refresh_session(refresh_token=s.refresh_token)
        assert s2.user_id == "alice"
        assert s2.email == "a@x.com"
        assert p.verify_session(access_token=s2.access_token) is not None

    def test_refresh_rejects_garbage(self):
        p = make_provider()
        with pytest.raises(RefreshExpiredError):
            p.refresh_session(refresh_token="not-a-token")
        with pytest.raises(RefreshExpiredError):
            p.refresh_session(refresh_token="")

    def test_revoke_never_raises(self):
        p = make_provider()
        assert p.revoke_session(refresh_token="anything") is None
