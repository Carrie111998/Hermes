"""Regression tests for Hermes-managed Anthropic PKCE rotation."""

from __future__ import annotations

import json

from agent.credential_pool import (
    AUTH_TYPE_OAUTH,
    STATUS_EXHAUSTED,
    CredentialPool,
    PooledCredential,
    _upsert_entry,
)


def _pkce_entry(**overrides):
    values = {
        "provider": "anthropic",
        "id": "pkce-1",
        "label": "Hermes PKCE",
        "auth_type": AUTH_TYPE_OAUTH,
        "priority": 0,
        "source": "hermes_pkce",
        "access_token": "fresh-access",
        "refresh_token": "fresh-refresh",
        "expires_at_ms": 2_000,
    }
    values.update(overrides)
    return PooledCredential(**values)


def test_singleton_seed_cannot_regress_rotated_pkce_token():
    entry = _pkce_entry(
        last_status=STATUS_EXHAUSTED,
        last_status_at=10.0,
        last_error_code=401,
        last_error_reason="token_revoked",
    )
    entries = [entry]

    changed = _upsert_entry(
        entries,
        "anthropic",
        "hermes_pkce",
        {
            "source": "hermes_pkce",
            "auth_type": AUTH_TYPE_OAUTH,
            "access_token": "stale-access",
            "refresh_token": "stale-refresh",
            "expires_at_ms": 2_000,
        },
    )

    assert changed is False
    assert entries == [entry]


def test_newer_singleton_token_replaces_exhausted_pkce_entry():
    entries = [
        _pkce_entry(
            expires_at_ms=2_000,
            last_status=STATUS_EXHAUSTED,
            last_error_code=401,
        )
    ]

    changed = _upsert_entry(
        entries,
        "anthropic",
        "hermes_pkce",
        {
            "source": "hermes_pkce",
            "auth_type": AUTH_TYPE_OAUTH,
            "access_token": "new-login-access",
            "refresh_token": "new-login-refresh",
            "expires_at_ms": 4_000,
        },
    )

    assert changed is True
    assert entries[0].refresh_token == "new-login-refresh"
    assert entries[0].expires_at_ms == 4_000
    assert entries[0].last_status is None
    assert entries[0].last_error_code is None


def test_pkce_refresh_persists_rotated_singleton(tmp_path, monkeypatch):
    oauth_file = tmp_path / ".anthropic_oauth.json"
    entry = _pkce_entry(expires_at_ms=1_000)
    pool = CredentialPool("anthropic", [entry])

    monkeypatch.setattr(
        "agent.anthropic_adapter.refresh_anthropic_oauth_pure",
        lambda refresh_token, *, use_json: {
            "access_token": "rotated-access",
            "refresh_token": "rotated-refresh",
            "expires_at_ms": 3_000,
        },
    )
    monkeypatch.setattr(
        "agent.anthropic_adapter._get_hermes_oauth_file", lambda: oauth_file
    )
    monkeypatch.setattr(pool, "_persist", lambda **_kwargs: None)

    updated = pool._refresh_entry_impl(entry, force=True)

    assert updated is not None
    assert updated.refresh_token == "rotated-refresh"
    assert json.loads(oauth_file.read_text(encoding="utf-8")) == {
        "accessToken": "rotated-access",
        "refreshToken": "rotated-refresh",
        "expiresAt": 3_000,
    }


def test_singleton_seed_collapses_legacy_dashboard_mirror(monkeypatch):
    from agent.credential_pool import _seed_from_singletons

    legacy = _pkce_entry(
        id="dashboard",
        source="manual:dashboard_pkce",
        access_token="rotated-access",
        refresh_token="rotated-refresh",
        expires_at_ms=3_000,
    )
    entries = [legacy]
    written = []

    monkeypatch.setattr(
        "hermes_cli.auth.is_provider_explicitly_configured", lambda _provider: True
    )
    monkeypatch.setattr(
        "agent.anthropic_adapter.read_hermes_oauth_credentials",
        lambda: {
            "accessToken": "stale-access",
            "refreshToken": "stale-refresh",
            "expiresAt": 2_000,
        },
    )
    monkeypatch.setattr(
        "agent.anthropic_adapter.read_claude_code_credentials", lambda: None
    )
    monkeypatch.setattr(
        "agent.anthropic_adapter._write_hermes_oauth_credentials",
        lambda access, refresh, expires: written.append((access, refresh, expires)),
    )

    changed, active_sources = _seed_from_singletons("anthropic", entries)

    assert changed is True
    assert active_sources == {"hermes_pkce"}
    assert len(entries) == 1
    assert entries[0].source == "hermes_pkce"
    assert entries[0].access_token == "rotated-access"
    assert entries[0].refresh_token == "rotated-refresh"
    assert written == [("rotated-access", "rotated-refresh", 3_000)]
