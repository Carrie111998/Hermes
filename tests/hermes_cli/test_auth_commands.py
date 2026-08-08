"""Tests for auth subcommands backed by the credential pool."""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
import yaml


def _write_auth_store(tmp_path, payload: dict) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "auth.json").write_text(json.dumps(payload, indent=2))


def _jwt_with_email(email: str) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"email": email}).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.signature"


def _codex_pool_only_store(*, exhausted: bool = False) -> dict:
    entry = {
        "id": "codex-1",
        "label": "codex@example.com",
        "auth_type": "oauth",
        "priority": 0,
        "source": "manual:device_code",
        "access_token": _jwt_with_email("codex@example.com"),
        "refresh_token": "refresh-token",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "last_refresh": "2026-06-15T10:00:00Z",
    }
    if exhausted:
        entry.update(
            {
                "last_status": "exhausted",
                "last_status_at": time.time(),
                "last_error_code": 429,
                "last_error_reason": "usage_limit_reached",
                "last_error_message": "The usage limit has been reached",
                "last_error_reset_at": time.time() + 3600,
            }
        )
    return {
        "version": 1,
        "active_provider": "openai-codex",
        "providers": {},
        "credential_pool": {"openai-codex": [entry]},
    }


@pytest.fixture(autouse=True)
def _clear_provider_env(monkeypatch):
    for key in (
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)


def test_auth_add_api_key_persists_manual_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _write_auth_store(tmp_path, {"version": 1, "providers": {}})

    from hermes_cli.auth_commands import auth_add_command

    class _Args:
        provider = "openrouter"
        auth_type = "api-key"
        api_key = "sk-or-manual"
        label = "personal"

    auth_add_command(_Args())

    payload = json.loads((tmp_path / "hermes" / "auth.json").read_text())
    entries = payload["credential_pool"]["openrouter"]
    entry = next(item for item in entries if item["source"] == "manual")
    assert entry["label"] == "personal"
    assert entry["auth_type"] == "api_key"
    assert entry["source"] == "manual"
    assert entry["access_token"] == "sk-or-manual"


def test_auth_add_nous_oauth_persists_pool_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, {"version": 1, "providers": {}})
    token = _jwt_with_email("nous@example.com")
    monkeypatch.setattr(
        "hermes_cli.auth._nous_device_code_login",
        lambda **kwargs: {
            "portal_base_url": "https://portal.example.com",
            "inference_base_url": "https://inference.example.com/v1",
            "client_id": "hermes-cli",
            "scope": "inference:invoke",
            "token_type": "Bearer",
            "access_token": token,
            "refresh_token": "refresh-token",
            "obtained_at": "2026-03-23T10:00:00+00:00",
            "expires_at": "2026-03-23T11:00:00+00:00",
            "expires_in": 3600,
            "agent_key": token,
            "agent_key_id": None,
            "agent_key_expires_at": "2026-03-23T10:30:00+00:00",
            "agent_key_expires_in": 1800,
            "agent_key_reused": False,
            "agent_key_obtained_at": "2026-03-23T10:00:10+00:00",
            "tls": {"insecure": False, "ca_bundle": None},
        },
    )

    from hermes_cli.auth_commands import auth_add_command

    class _Args:
        provider = "nous"
        auth_type = "oauth"
        api_key = None
        label = None
        portal_url = None
        inference_url = None
        client_id = None
        scope = None
        no_browser = False
        timeout = None
        insecure = False
        ca_bundle = None

    auth_add_command(_Args())

    payload = json.loads((tmp_path / "hermes" / "auth.json").read_text())

    # Pool has exactly one canonical `device_code` entry — not a duplicate
    # pair of `manual:device_code` + `device_code` (the latter would be
    # materialised by _seed_from_singletons on every load_pool).
    entries = payload["credential_pool"]["nous"]
    device_code_entries = [
        item for item in entries if item["source"] == "device_code"
    ]
    assert len(device_code_entries) == 1, entries
    assert not any(item["source"] == "manual:device_code" for item in entries)
    entry = device_code_entries[0]
    assert entry["source"] == "device_code"
    assert entry["agent_key"] == token
    assert entry["portal_base_url"] == "https://portal.example.com"

    # `hermes auth add nous` must also populate providers.nous so the
    # 401-recovery path (resolve_nous_runtime_credentials) can refresh an
    # invoke JWT when the token expires. If this mirror is missing, recovery
    # raises "Hermes is not logged into Nous Portal" and the agent dies.
    singleton = payload["providers"]["nous"]
    assert singleton["access_token"] == token
    assert singleton["refresh_token"] == "refresh-token"
    assert singleton["agent_key"] == token
    assert singleton["portal_base_url"] == "https://portal.example.com"
    assert singleton["inference_base_url"] == "https://inference.example.com/v1"


def test_auth_add_nous_oauth_honors_custom_label(tmp_path, monkeypatch):
    """`hermes auth add nous --type oauth --label <name>` must preserve the
    custom label end-to-end — it was silently dropped in the first cut of the
    persist_nous_credentials helper because `--label` wasn't threaded through.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, {"version": 1, "providers": {}})
    token = _jwt_with_email("nous@example.com")
    monkeypatch.setattr(
        "hermes_cli.auth._nous_device_code_login",
        lambda **kwargs: {
            "portal_base_url": "https://portal.example.com",
            "inference_base_url": "https://inference.example.com/v1",
            "client_id": "hermes-cli",
            "scope": "inference:invoke",
            "token_type": "Bearer",
            "access_token": token,
            "refresh_token": "refresh-token",
            "obtained_at": "2026-03-23T10:00:00+00:00",
            "expires_at": "2026-03-23T11:00:00+00:00",
            "expires_in": 3600,
            "agent_key": token,
            "agent_key_id": None,
            "agent_key_expires_at": "2026-03-23T10:30:00+00:00",
            "agent_key_expires_in": 1800,
            "agent_key_reused": False,
            "agent_key_obtained_at": "2026-03-23T10:00:10+00:00",
            "tls": {"insecure": False, "ca_bundle": None},
        },
    )

    from hermes_cli.auth_commands import auth_add_command

    class _Args:
        provider = "nous"
        auth_type = "oauth"
        api_key = None
        label = "my-nous"
        portal_url = None
        inference_url = None
        client_id = None
        scope = None
        no_browser = False
        timeout = None
        insecure = False
        ca_bundle = None

    auth_add_command(_Args())

    payload = json.loads((tmp_path / "hermes" / "auth.json").read_text())

    # Custom label reaches the pool entry …
    pool_entry = payload["credential_pool"]["nous"][0]
    assert pool_entry["source"] == "device_code"
    assert pool_entry["label"] == "my-nous"

    # … and survives in providers.nous so a subsequent load_pool() re-seeds
    # it without reverting to the auto-derived fingerprint.
    assert payload["providers"]["nous"]["label"] == "my-nous"


def test_auth_add_codex_oauth_keeps_distinct_pool_accounts(tmp_path, monkeypatch):
    """Two ``hermes auth add openai-codex`` runs for different ChatGPT
    accounts must produce two independent pool entries with distinct tokens.

    Regression for #39236: the add path used to route through the singleton
    ``_save_codex_tokens`` save, so the second login overwrote the first
    account's singleton-mirrored ``device_code`` entry instead of adding a
    second independent one. ``hermes auth list`` showed two labels sharing
    one token pair, and rotation silently always used the latest account.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, {"version": 1, "providers": {}})
    first_token = _jwt_with_email("first-codex@example.com")
    second_token = _jwt_with_email("second-codex@example.com")
    logins = iter(
        [
            {
                "tokens": {
                    "access_token": first_token,
                    "refresh_token": "first-refresh-token",
                },
                "base_url": "https://chatgpt.com/backend-api/codex",
                "last_refresh": "2026-03-23T10:00:00Z",
            },
            {
                "tokens": {
                    "access_token": second_token,
                    "refresh_token": "second-refresh-token",
                },
                "base_url": "https://chatgpt.com/backend-api/codex",
                "last_refresh": "2026-03-23T10:05:00Z",
            },
        ]
    )
    monkeypatch.setattr("hermes_cli.auth._codex_device_code_login", lambda: next(logins))

    from hermes_cli.auth_commands import auth_add_command
    from agent.credential_pool import load_pool

    class _Args:
        provider = "openai-codex"
        auth_type = "oauth"
        api_key = None
        label = None

    auth_add_command(_Args())
    auth_add_command(_Args())

    pool = load_pool("openai-codex")
    entries = pool.entries()

    assert [entry.source for entry in entries] == [
        "manual:device_code",
        "manual:device_code",
    ]
    assert [entry.label for entry in entries] == [
        "first-codex@example.com",
        "second-codex@example.com",
    ]
    assert [entry.access_token for entry in entries] == [first_token, second_token]
    assert [entry.refresh_token for entry in entries] == [
        "first-refresh-token",
        "second-refresh-token",
    ]

    payload = json.loads((tmp_path / "hermes" / "auth.json").read_text())
    # No singleton block — the add path is now pool-only.
    assert "openai-codex" not in payload.get("providers", {})
    # First add activated the provider; second add left it as-is.
    assert payload["active_provider"] == "openai-codex"


def test_codex_auth_status_reports_pool_only_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _codex_pool_only_store())

    from hermes_cli.auth import get_codex_auth_status

    status = get_codex_auth_status()

    assert status["logged_in"] is True
    assert status["source"] == "pool:codex@example.com"


def test_codex_runtime_pool_only_rate_limit_is_not_missing_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _codex_pool_only_store(exhausted=True))

    from hermes_cli.auth import AuthError, CODEX_RATE_LIMITED_CODE, resolve_codex_runtime_credentials

    with pytest.raises(AuthError) as exc_info:
        resolve_codex_runtime_credentials()

    assert exc_info.value.code == CODEX_RATE_LIMITED_CODE
    assert exc_info.value.relogin_required is False


def test_auth_add_xai_oauth_sets_active_provider(tmp_path, monkeypatch):
    """hermes auth add xai-oauth must set active_provider and write a pool entry.

    Regression history:
    - Early path called ``pool.add_entry()`` without ``active_provider``, so
      the setup wizard reported "No inference provider configured".
    - Intermediate path fixed that by routing through ``_save_xai_oauth_tokens``
      (singleton), which set active_provider but collapsed multi-account adds.
    - Current path mirrors openai-codex: pool-only ``manual:device_code`` entry
      plus ``mark_provider_active_if_unset`` on first add.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, {"version": 1, "providers": {}})
    access_token = "xai-test-access-token"
    monkeypatch.setattr(
        "hermes_cli.auth._xai_oauth_device_code_login",
        lambda **kwargs: {
            "tokens": {
                "access_token": access_token,
                "refresh_token": "xai-refresh-token",
                "id_token": "",
                "token_type": "Bearer",
            },
            "discovery": {"token_endpoint": "https://auth.x.ai/token"},
            "redirect_uri": "",
            "base_url": "https://api.x.ai/v1",
            "last_refresh": "2026-06-02T10:00:00Z",
            "source": "oauth-device-code",
        },
    )

    from hermes_cli.auth_commands import auth_add_command

    class _Args:
        provider = "xai-oauth"
        auth_type = "oauth"
        api_key = None
        label = None
        timeout = None
        no_browser = False

    auth_add_command(_Args())

    payload = json.loads((tmp_path / "hermes" / "auth.json").read_text())
    # active_provider must be set — the core of the original regression
    assert payload["active_provider"] == "xai-oauth"
    # Pool-only multi-account path: no providers.xai-oauth singleton write
    assert "xai-oauth" not in payload.get("providers", {})
    entries = payload["credential_pool"]["xai-oauth"]
    entry = next(item for item in entries if item["source"] == "manual:device_code")
    assert entry["access_token"] == access_token
    assert entry["refresh_token"] == "xai-refresh-token"
    assert entry["base_url"] == "https://api.x.ai/v1"


def test_auth_add_xai_oauth_keeps_distinct_pool_accounts(tmp_path, monkeypatch):
    """Two ``hermes auth add xai-oauth`` runs must produce independent pool entries.

    Regression for the same collapse class as #39236 / #42316 for Codex: the
    add path used to route through the singleton ``_save_xai_oauth_tokens``
    save, so the second login overwrote the first account's singleton-mirrored
    ``device_code`` entry instead of adding a second independent one.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, {"version": 1, "providers": {}})
    first_token = "xai-access-token-account-a"
    second_token = "xai-access-token-account-b"
    logins = iter(
        [
            {
                "tokens": {
                    "access_token": first_token,
                    "refresh_token": "first-xai-refresh",
                    "id_token": "",
                    "token_type": "Bearer",
                },
                "discovery": {"token_endpoint": "https://auth.x.ai/token"},
                "redirect_uri": "",
                "base_url": "https://api.x.ai/v1",
                "last_refresh": "2026-07-10T10:00:00Z",
                "source": "oauth-device-code",
            },
            {
                "tokens": {
                    "access_token": second_token,
                    "refresh_token": "second-xai-refresh",
                    "id_token": "",
                    "token_type": "Bearer",
                },
                "discovery": {"token_endpoint": "https://auth.x.ai/token"},
                "redirect_uri": "",
                "base_url": "https://api.x.ai/v1",
                "last_refresh": "2026-07-10T10:05:00Z",
                "source": "oauth-device-code",
            },
        ]
    )
    monkeypatch.setattr(
        "hermes_cli.auth._xai_oauth_device_code_login",
        lambda **kwargs: next(logins),
    )

    from hermes_cli.auth_commands import auth_add_command
    from agent.credential_pool import load_pool

    class _Args:
        provider = "xai-oauth"
        auth_type = "oauth"
        api_key = None
        label = None
        timeout = None
        no_browser = False

    # Distinct labels so order is unambiguous even without JWT email claims.
    class _ArgsA(_Args):
        label = "xai-heavy"

    class _ArgsB(_Args):
        label = "xai-premium"

    auth_add_command(_ArgsA())
    auth_add_command(_ArgsB())

    pool = load_pool("xai-oauth")
    entries = pool.entries()

    assert [entry.source for entry in entries] == [
        "manual:device_code",
        "manual:device_code",
    ]
    assert [entry.label for entry in entries] == ["xai-heavy", "xai-premium"]
    assert [entry.access_token for entry in entries] == [first_token, second_token]
    assert [entry.refresh_token for entry in entries] == [
        "first-xai-refresh",
        "second-xai-refresh",
    ]

    payload = json.loads((tmp_path / "hermes" / "auth.json").read_text())
    # No singleton block — the add path is now pool-only.
    assert "xai-oauth" not in payload.get("providers", {})
    # First add activated the provider; second add left it as-is.
    assert payload["active_provider"] == "xai-oauth"


def test_auth_remove_reindexes_priorities(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    # Prevent pool auto-seeding from host env vars and file-backed sources
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(
        "agent.credential_pool._seed_from_singletons",
        lambda provider, entries: (False, set()),
    )
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "anthropic": [
                    {
                        "id": "cred-1",
                        "label": "primary",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "sk-ant-api-primary",
                    },
                    {
                        "id": "cred-2",
                        "label": "secondary",
                        "auth_type": "api_key",
                        "priority": 1,
                        "source": "manual",
                        "access_token": "sk-ant-api-secondary",
                    },
                ]
            },
        },
    )

    from hermes_cli.auth_commands import auth_remove_command

    class _Args:
        provider = "anthropic"
        target = "1"

    auth_remove_command(_Args())

    payload = json.loads((tmp_path / "hermes" / "auth.json").read_text())
    entries = payload["credential_pool"]["anthropic"]
    assert len(entries) == 1
    assert entries[0]["label"] == "secondary"
    assert entries[0]["priority"] == 0


def test_clear_provider_auth_removes_provider_pool_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "active_provider": "anthropic",
            "providers": {
                "anthropic": {"access_token": "legacy-token"},
            },
            "credential_pool": {
                "anthropic": [
                    {
                        "id": "cred-1",
                        "label": "primary",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "manual:hermes_pkce",
                        "access_token": "pool-token",
                    }
                ],
                "openrouter": [
                    {
                        "id": "cred-2",
                        "label": "other-provider",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "sk-or-test",
                    }
                ],
            },
        },
    )

    from hermes_cli.auth import clear_provider_auth

    assert clear_provider_auth("anthropic") is True

    payload = json.loads((tmp_path / "hermes" / "auth.json").read_text())
    assert payload["active_provider"] is None
    assert "anthropic" not in payload.get("providers", {})
    assert "anthropic" not in payload.get("credential_pool", {})
    assert "openrouter" in payload.get("credential_pool", {})


def test_logout_resets_codex_config_when_auth_state_already_cleared(tmp_path, monkeypatch, capsys):
    """`hermes logout --provider openai-codex` must still clear model.provider.

    Users can end up with auth.json already cleared but config.yaml still set to
    openai-codex.  Previously logout reported no auth state and left the agent
    pinned to the Codex provider.
    """
    hermes_home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_auth_store(tmp_path, {"version": 1, "providers": {}, "credential_pool": {}})
    (hermes_home / "config.yaml").write_text(
        "model:\n"
        "  default: gpt-5.3-codex\n"
        "  provider: openai-codex\n"
        "  base_url: https://chatgpt.com/backend-api/codex\n"
    )

    from types import SimpleNamespace
    from hermes_cli.auth import logout_command

    logout_command(SimpleNamespace(provider="openai-codex"))

    out = capsys.readouterr().out
    assert "Logged out of OpenAI Codex." in out
    config_text = (hermes_home / "config.yaml").read_text()
    assert "provider: auto" in config_text
    assert "base_url: https://openrouter.ai/api/v1" in config_text




def test_unsuppress_credential_source_clears_marker(tmp_path, monkeypatch):
    """unsuppress_credential_source() removes a previously-set marker."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, {"version": 1})

    from hermes_cli.auth import suppress_credential_source, unsuppress_credential_source, is_source_suppressed

    suppress_credential_source("openai-codex", "device_code")
    assert is_source_suppressed("openai-codex", "device_code") is True

    cleared = unsuppress_credential_source("openai-codex", "device_code")
    assert cleared is True
    assert is_source_suppressed("openai-codex", "device_code") is False

    payload = json.loads((tmp_path / "hermes" / "auth.json").read_text())
    # Empty suppressed_sources dict should be cleaned up entirely
    assert "suppressed_sources" not in payload


def test_unsuppress_credential_source_preserves_other_markers(tmp_path, monkeypatch):
    """Clearing one marker must not affect unrelated markers."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, {"version": 1})

    from hermes_cli.auth import (
        suppress_credential_source,
        unsuppress_credential_source,
        is_source_suppressed,
    )

    suppress_credential_source("openai-codex", "device_code")
    suppress_credential_source("anthropic", "claude_code")

    assert unsuppress_credential_source("openai-codex", "device_code") is True
    assert is_source_suppressed("anthropic", "claude_code") is True




# =============================================================================
# Unified credential-source stickiness — every source Hermes reads from has a
# registered RemovalStep in agent.credential_sources, and every seeding path
# gates on is_source_suppressed.  Below: one test per source proving remove
# sticks across a fresh load_pool() call.
# =============================================================================


def test_seed_from_singletons_respects_hermes_pkce_suppression(tmp_path, monkeypatch):
    """anthropic hermes_pkce must not re-seed from ~/.hermes/.anthropic_oauth.json when suppressed."""
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    import yaml
    (hermes_home / "config.yaml").write_text(yaml.dump({"model": {"provider": "anthropic", "model": "claude"}}))
    (hermes_home / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {},
        "suppressed_sources": {"anthropic": ["hermes_pkce"]},
    }))

    # Stub the readers so only hermes_pkce is "available"; claude_code returns None
    import agent.anthropic_adapter as aa
    monkeypatch.setattr(aa, "read_hermes_oauth_credentials", lambda: {
        "accessToken": "tok", "refreshToken": "r", "expiresAt": 9999999999000,
    })
    monkeypatch.setattr(aa, "read_claude_code_credentials", lambda: None)

    from agent.credential_pool import _seed_from_singletons
    entries = []
    changed, active = _seed_from_singletons("anthropic", entries)
    # hermes_pkce suppressed, claude_code returns None → nothing should be seeded
    assert entries == []
    assert "hermes_pkce" not in active




def test_credential_sources_registry_has_expected_steps():
    """Sanity check — the registry contains the expected RemovalSteps.

    Adding a new credential source is routine, so this is a structural
    invariant check (every step has a description, every step is unique,
    core steps are present) rather than a frozen snapshot. Frozen
    snapshots of catalog-like data violate the AGENTS.md "don't write
    change-detector tests" rule — they break every time someone adds a
    provider.
    """
    from agent.credential_sources import _REGISTRY

    descriptions = [step.description for step in _REGISTRY]
    # No empty descriptions, no duplicates.
    assert all(d for d in descriptions), "Every removal step must have a description"
    assert len(descriptions) == len(set(descriptions)), (
        f"Registry has duplicate step descriptions: {descriptions}"
    )
    # Core steps must be present — these are the ones the rest of the code
    # assumes exist. When deliberately dropping one, update this list.
    required = {
        "gh auth token / COPILOT_GITHUB_TOKEN / GH_TOKEN",
        "Any env-seeded credential (XAI_API_KEY, DEEPSEEK_API_KEY, etc.)",
        "~/.claude/.credentials.json",
        "~/.hermes/.anthropic_oauth.json",
        "auth.json providers.nous",
        "auth.json providers.openai-codex + ~/.codex/auth.json",
        "auth.json providers.minimax-oauth",
        "~/.qwen/oauth_creds.json",
        "Custom provider config.yaml api_key field",
    }
    missing = required - set(descriptions)
    assert not missing, f"Registry missing required steps: {missing}"


def test_credential_sources_find_step_copilot_before_generic_env(tmp_path, monkeypatch):
    """copilot env:GH_TOKEN must dispatch to the copilot step, not the
    generic env-var step.  The copilot step handles the duplicate-source
    problem (same token seeded as both gh_cli and env:<VAR>); the generic
    env step would only suppress one of the variants.
    """
    from agent.credential_sources import find_removal_step

    step = find_removal_step("copilot", "env:GH_TOKEN")
    assert step is not None
    assert "copilot" in step.description.lower() or "gh" in step.description.lower()

    # Generic step still matches any other provider's env var
    step = find_removal_step("xai", "env:XAI_API_KEY")
    assert step is not None
    assert "env-seeded" in step.description.lower()


def test_auth_remove_copilot_suppresses_all_variants(tmp_path, monkeypatch):
    """Removing any copilot source must suppress gh_cli + all env:* variants
    so the duplicate-seed paths don't resurrect the credential.
    """
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    # The copilot pool entry is no longer persisted directly in auth.json —
    # `(copilot, gh_cli)` is borrowed and stripped by
    # sanitize_borrowed_credential_payload (PR #31416, May 2026). Tokens are
    # hydrated at runtime via resolve_copilot_token(). Mock that path so the
    # pool has an entry to remove.
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {"copilot": []},
        },
    )

    from types import SimpleNamespace
    from hermes_cli.auth import is_source_suppressed
    from hermes_cli.auth_commands import auth_remove_command

    with patch(
        "hermes_cli.copilot_auth.resolve_copilot_token",
        return_value=("ghp_fake", "gh"),
    ), patch(
        "hermes_cli.copilot_auth.get_copilot_api_token",
        return_value=("ghu_fake_api", None),
    ):
        auth_remove_command(SimpleNamespace(provider="copilot", target="1"))

    assert is_source_suppressed("copilot", "gh_cli")
    assert is_source_suppressed("copilot", "env:COPILOT_GITHUB_TOKEN")
    assert is_source_suppressed("copilot", "env:GH_TOKEN")
    assert is_source_suppressed("copilot", "env:GITHUB_TOKEN")


# ──────────────────────────────────────────────────────────────────────────
# `hermes auth usage <provider>` — standalone live account-usage fetch
# ──────────────────────────────────────────────────────────────────────────
#
# These tests cover the no-agent CLI entry point that the REPL ``/usage``
# slash used to own. The fetch helpers (``fetch_account_usage``,
# ``render_account_usage_lines``) live in ``agent.account_usage`` and are
# already covered there; here we only verify the command-shape behavior:
#   * rejects an empty / unsupported provider cleanly,
#   * fails fast when the provider has no usable credential,
#   * prints the rendered snapshot for a happy-path fetch,
#   * exits non-zero (and never with a stacktrace) on a slow fetch timeout.

import concurrent.futures
from types import SimpleNamespace

from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow


def test_auth_usage_rejects_missing_provider(capsys):
    from hermes_cli.auth_commands import auth_usage_command

    with pytest.raises(SystemExit) as exc_info:
        auth_usage_command(SimpleNamespace(provider=""))
    # argparse already rejects ``hermes auth usage`` (no provider) at the
    # parser level; this guard is the "empty string" fallback for direct
    # programmatic callers. ``SystemExit(str)`` stores the message on
    # ``.code`` — exit code is implicitly 1 when the message is a string.
    assert "Provider is required" in str(exc_info.value)
    assert exc_info.value.code != 0  # non-zero → scriptable from cron/loops


def test_auth_usage_rejects_unsupported_provider(capsys):
    from hermes_cli.auth_commands import auth_usage_command

    with pytest.raises(SystemExit) as exc_info:
        auth_usage_command(SimpleNamespace(provider="spotify"))
    msg = str(exc_info.value)
    assert "not supported" in msg
    # The error must list the SUPPORTED set so a typo self-corrects on retry
    # rather than the user having to read the docs to find the right id.
    assert "openai-codex" in msg
    assert "anthropic" in msg
    assert "openrouter" in msg


def test_auth_usage_requires_logged_in_credential(monkeypatch, capsys):
    from hermes_cli.auth_commands import auth_usage_command

    # ``get_auth_status`` is the same probe ``hermes auth status`` uses; patch
    # it to simulate "provider is supported but not logged in" so the command
    # fails fast with the auth-add hint instead of timing out on the API.
    monkeypatch.setattr(
        "hermes_cli.auth.get_auth_status",
        lambda provider: {"logged_in": False, "error": "no creds"},
    )
    with pytest.raises(SystemExit) as exc_info:
        auth_usage_command(SimpleNamespace(provider="openai-codex"))
    assert "not logged in" in str(exc_info.value)
    assert "hermes auth add openai-codex" in str(exc_info.value)


def test_auth_usage_renders_snapshot_on_success(monkeypatch, capsys):
    from hermes_cli.auth_commands import auth_usage_command

    monkeypatch.setattr(
        "hermes_cli.auth.get_auth_status",
        lambda provider: {"logged_in": True},
    )

    # Build a deterministic snapshot so the assertion is value-driven, not
    # snapshot-of-a-snapshot. ``available`` must be True for the command to
    # proceed past the unavailable-guard and reach the render branch.
    snapshot = AccountUsageSnapshot(
        provider="openai-codex",
        source="usage_api",
        fetched_at=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        plan="Pro",
        windows=(
            AccountUsageWindow(
                label="Session",
                used_percent=42.0,
                reset_at=datetime(2026, 1, 1, 5, 0, tzinfo=timezone.utc),
            ),
        ),
        details=("You have 2 resets banked - use /usage reset to activate",),
    )

    # The command goes through a ThreadPoolExecutor + ``fetch_account_usage``;
    # short-circuit the executor by replacing ``submit`` so the snapshot is
    # returned synchronously without touching the network.
    class _ImmediateFuture:
        def __init__(self, value):
            self._value = value

        def result(self, timeout=None):
            return self._value

    class _ImmediatePool:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def submit(self, fn, *args, **kwargs):
            return _ImmediateFuture(fn(*args, **kwargs))

    monkeypatch.setattr(
        "concurrent.futures.ThreadPoolExecutor", _ImmediatePool
    )
    # Patch at the import path the lazy import resolves to.
    import agent.account_usage as _account_usage

    monkeypatch.setattr(_account_usage, "fetch_account_usage", lambda *a, **kw: snapshot)

    auth_usage_command(SimpleNamespace(provider="openai-codex"))

    out = capsys.readouterr().out
    # The renderer (``render_account_usage_lines``) is the source of truth;
    # assert on its surface, not the literal byte sequence, so a renderer
    # tweak doesn't silently break this test.
    assert "Account limits" in out
    assert "Session" in out
    assert "openai-codex" in out
    assert "Pro" in out
    assert "resets banked" in out


def test_auth_usage_times_out_cleanly(monkeypatch, capsys):
    from hermes_cli.auth_commands import (
        _ACCOUNT_USAGE_FETCH_TIMEOUT_SECONDS,
        auth_usage_command,
    )

    monkeypatch.setattr(
        "hermes_cli.auth.get_auth_status",
        lambda provider: {"logged_in": True},
    )

    class _HangingFuture:
        def result(self, timeout=None):
            # Mirror concurrent.futures' exact behavior on a hard timeout so
            # the command's own except-branch (not a generic Exception
            # fallback) handles it. Using TimeoutError makes that explicit.
            raise concurrent.futures.TimeoutError()

    class _HangingPool:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def submit(self, fn, *args, **kwargs):
            return _HangingFuture()

    monkeypatch.setattr(
        "concurrent.futures.ThreadPoolExecutor", _HangingPool
    )

    # No pool entries are seeded; the command falls back to a single
    # ``default`` synthetic row whose fetch hangs. The single-entry path
    # surfaces the timeout inline (no stacktrace) and then exits non-zero
    # because no entry rendered.
    with pytest.raises(SystemExit) as exc_info:
        auth_usage_command(SimpleNamespace(provider="openai-codex"))
    msg = str(exc_info.value)
    out = capsys.readouterr().out
    # The timeout must surface a short actionable line, not a stacktrace,
    # and the inline message must include the budget so the user can
    # correlate it with the known REPL ``/usage`` 10s cap.
    assert "timed out" in out
    assert f"{_ACCOUNT_USAGE_FETCH_TIMEOUT_SECONDS:.0f}s" in out
    assert "no account returned usable usage" in msg
    assert "Traceback" not in capsys.readouterr().err


def test_auth_usage_reports_unavailable_snapshot(monkeypatch, capsys):
    from hermes_cli.auth_commands import auth_usage_command

    monkeypatch.setattr(
        "hermes_cli.auth.get_auth_status",
        lambda provider: {"logged_in": True},
    )

    # ``fetch_account_usage`` returns a snapshot with ``unavailable_reason``
    # when the endpoint is reachable but the payload is in a shape we can't
    # render (e.g. a future Codex payload change). The command must surface
    # that reason inline (per entry) instead of as the final SystemExit
    # summary, and exit non-zero because nothing rendered.
    snapshot = AccountUsageSnapshot(
        provider="openai-codex",
        source="usage_api",
        fetched_at=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        unavailable_reason="unexpected response shape",
    )

    class _ImmediatePool:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def submit(self, fn, *args, **kwargs):
            class _Fut:
                def result(self, timeout=None):
                    return fn(*args, **kwargs)

            return _Fut()

    monkeypatch.setattr(
        "concurrent.futures.ThreadPoolExecutor", _ImmediatePool
    )
    import agent.account_usage as _account_usage

    monkeypatch.setattr(
        _account_usage, "fetch_account_usage", lambda *a, **kw: snapshot
    )

    with pytest.raises(SystemExit) as exc_info:
        auth_usage_command(SimpleNamespace(provider="openai-codex"))
    out = capsys.readouterr().out
    # Inline per-entry message carries the provider reason; the SystemExit
    # summary only fires because nothing rendered.
    assert "unexpected response shape" in out
    assert "no account returned usable usage" in str(exc_info.value)


# ──────────────────────────────────────────────────────────────────────────
# `--all` / `--account` — multi-pool-entry iteration for `auth usage`
# ──────────────────────────────────────────────────────────────────────────
#
# The default (no flag) path renders only the resolver-selected account and
# prints a hint when the pool has more than one entry. ``--all`` iterates
# every entry and renders each separately; ``--account LABEL`` filters to a
# single entry by stored label. Per-entry failures are surfaced inline and
# do not abort the loop; only "no entry rendered at all" exits non-zero.


def _seed_pool(monkeypatch, entries):
    """Patch ``_load_auth_store`` so it returns the given provider entries."""
    store = {"credential_pool": {"openai-codex": list(entries)}}
    monkeypatch.setattr(
        "hermes_cli.auth_commands._list_provider_account_credentials",
        lambda provider: [
            {
                "label": e.get("label", ""),
                "source": e.get("source", "manual:device_code"),
                "access_token": e.get("access_token", "tok-" + e.get("label", "")),
                "account_id": e.get("account_id", ""),
            }
            for e in entries
            if e.get("access_token")
        ],
    )
    return store


def test_auth_usage_all_renders_every_pool_entry(monkeypatch, capsys):
    from hermes_cli.auth_commands import auth_usage_command

    monkeypatch.setattr(
        "hermes_cli.auth.get_auth_status",
        lambda provider: {"logged_in": True},
    )
    _seed_pool(
        monkeypatch,
        [
            {"label": "codex-current", "access_token": "tok-1"},
            {"label": "openai-codex-oauth-2", "access_token": "tok-2"},
        ],
    )

    # Stub the per-account fetch to return a happy-path snapshot for every
    # entry; the loop should render both, each under its own header.
    snapshot = AccountUsageSnapshot(
        provider="openai-codex",
        source="usage_api",
        fetched_at=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        plan="Plus",
        windows=(
            AccountUsageWindow(
                label="Session",
                used_percent=100.0,
                reset_at=datetime(2026, 1, 1, 5, 0, tzinfo=timezone.utc),
            ),
        ),
        details=(),
    )

    class _ImmediatePool:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def submit(self, fn, *args, **kwargs):
            class _Fut:
                def result(self, timeout=None):
                    return fn(*args, **kwargs)

            return _Fut()

    monkeypatch.setattr(
        "concurrent.futures.ThreadPoolExecutor", _ImmediatePool
    )

    # Stub the per-account fetch (resolved lazily inside
    # ``_fetch_account_usage_with_timeout``) so the test does not hit the
    # network. The snapshot must be available, or the command will exit
    # non-zero and the assertions below will fire spuriously.
    import agent.account_usage as _account_usage

    monkeypatch.setattr(
        _account_usage, "fetch_account_usage", lambda *a, **kw: snapshot
    )

    auth_usage_command(
        SimpleNamespace(provider="openai-codex", all_accounts=True, account="")
    )

    out = capsys.readouterr().out
    # Both labels appear, each preceded by its own header banner, and the
    # renderer output is shared (so ``Session`` is rendered twice).
    assert "=== openai-codex: codex-current ===" in out
    assert "=== openai-codex: openai-codex-oauth-2 ===" in out
    assert out.count("Session") == 2
    # No final SystemExit — both rendered cleanly.
    assert "no account returned usable usage" not in out


def test_auth_usage_account_filters_to_label(monkeypatch, capsys):
    from hermes_cli.auth_commands import auth_usage_command

    monkeypatch.setattr(
        "hermes_cli.auth.get_auth_status",
        lambda provider: {"logged_in": True},
    )
    _seed_pool(
        monkeypatch,
        [
            {"label": "codex-current", "access_token": "tok-1"},
            {"label": "openai-codex-oauth-2", "access_token": "tok-2"},
        ],
    )

    snapshot = AccountUsageSnapshot(
        provider="openai-codex",
        source="usage_api",
        fetched_at=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        plan="Plus",
        windows=(
            AccountUsageWindow(
                label="Session",
                used_percent=50.0,
                reset_at=datetime(2026, 1, 1, 5, 0, tzinfo=timezone.utc),
            ),
        ),
        details=(),
    )

    class _ImmediatePool:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def submit(self, fn, *args, **kwargs):
            class _Fut:
                def result(self, timeout=None):
                    return fn(*args, **kwargs)

            return _Fut()

    monkeypatch.setattr(
        "concurrent.futures.ThreadPoolExecutor", _ImmediatePool
    )

    import agent.account_usage as _account_usage

    monkeypatch.setattr(
        _account_usage, "fetch_account_usage", lambda *a, **kw: snapshot
    )

    auth_usage_command(
        SimpleNamespace(
            provider="openai-codex", all_accounts=False, account="codex-current"
        )
    )

    out = capsys.readouterr().out
    # The renderer output reflects the requested entry's snapshot (the
    # only window is the one we built above). The other label must NOT
    # appear anywhere — including the renderer, since each entry has its
    # own snapshot and we filtered to one.
    assert "Account limits" in out
    assert "Session" in out
    assert "50%" in out
    # With a single-entry filter, no per-account header banner is emitted
    # (consistent with the default one-shot path).
    assert "===" not in out


def test_auth_usage_account_unknown_label_exits_nonzero(monkeypatch, capsys):
    from hermes_cli.auth_commands import auth_usage_command

    monkeypatch.setattr(
        "hermes_cli.auth.get_auth_status",
        lambda provider: {"logged_in": True},
    )
    _seed_pool(
        monkeypatch,
        [
            {"label": "codex-current", "access_token": "tok-1"},
            {"label": "openai-codex-oauth-2", "access_token": "tok-2"},
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        auth_usage_command(
            SimpleNamespace(
                provider="openai-codex", all_accounts=False, account="ghost"
            )
        )
    msg = str(exc_info.value)
    # Must name the requested label and list the known set so the user can
    # self-correct on retry.
    assert "ghost" in msg
    assert "codex-current" in msg
    assert "openai-codex-oauth-2" in msg


def test_auth_usage_all_continues_after_per_entry_failure(monkeypatch, capsys):
    """One entry's fetch failure must not abort the rest of the loop."""
    from hermes_cli.auth_commands import auth_usage_command

    monkeypatch.setattr(
        "hermes_cli.auth.get_auth_status",
        lambda provider: {"logged_in": True},
    )
    _seed_pool(
        monkeypatch,
        [
            {"label": "codex-current", "access_token": "tok-1"},
            {"label": "openai-codex-oauth-2", "access_token": "tok-2"},
        ],
    )

    good_snapshot = AccountUsageSnapshot(
        provider="openai-codex",
        source="usage_api",
        fetched_at=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        plan="Plus",
        windows=(
            AccountUsageWindow(
                label="Session",
                used_percent=10.0,
                reset_at=datetime(2026, 1, 1, 5, 0, tzinfo=timezone.utc),
            ),
        ),
        details=(),
    )

    call_count = {"n": 0}

    def _flaky_fetch(*args, **kwargs):
        call_count["n"] += 1
        # First call hangs (timeout), second returns the good snapshot.
        if call_count["n"] == 1:
            raise concurrent.futures.TimeoutError()
        return good_snapshot

    class _FlakyFuture:
        def __init__(self, fn, *args, **kwargs):
            self._fn = fn
            self._args = args
            self._kwargs = kwargs

        def result(self, timeout=None):
            return self._fn(*self._args, **self._kwargs)

    class _FlakyPool:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def submit(self, fn, *args, **kwargs):
            # Wrap so the timeout the helper enforces kicks in — emulate
            # by raising TimeoutError on first call (handled above) and
            # returning the snapshot on the second.
            return _FlakyFuture(_flaky_fetch, fn, *args, **kwargs)

    monkeypatch.setattr(
        "concurrent.futures.ThreadPoolExecutor", _FlakyPool
    )

    auth_usage_command(
        SimpleNamespace(provider="openai-codex", all_accounts=True, account="")
    )

    out = capsys.readouterr().out
    # First entry's timeout surfaces inline; second entry still renders.
    assert "codex-current" in out
    assert "openai-codex-oauth-2" in out
    assert "timed out" in out
    assert "Account limits" in out
    # Both entries were attempted (not just the first).
    assert call_count["n"] == 2


def test_auth_usage_default_warns_when_multiple_pool_entries(monkeypatch, capsys):
    """The no-flag path must show the head entry AND a hint about ``--all``."""
    from hermes_cli.auth_commands import auth_usage_command

    monkeypatch.setattr(
        "hermes_cli.auth.get_auth_status",
        lambda provider: {"logged_in": True},
    )
    _seed_pool(
        monkeypatch,
        [
            {"label": "codex-current", "access_token": "tok-1"},
            {"label": "openai-codex-oauth-2", "access_token": "tok-2"},
        ],
    )

    snapshot = AccountUsageSnapshot(
        provider="openai-codex",
        source="usage_api",
        fetched_at=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        plan="Plus",
        windows=(
            AccountUsageWindow(
                label="Session",
                used_percent=100.0,
                reset_at=datetime(2026, 1, 1, 5, 0, tzinfo=timezone.utc),
            ),
        ),
        details=(),
    )

    class _ImmediatePool:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def submit(self, fn, *args, **kwargs):
            class _Fut:
                def result(self, timeout=None):
                    return fn(*args, **kwargs)

            return _Fut()

    monkeypatch.setattr(
        "concurrent.futures.ThreadPoolExecutor", _ImmediatePool
    )

    import agent.account_usage as _account_usage

    monkeypatch.setattr(
        _account_usage, "fetch_account_usage", lambda *a, **kw: snapshot
    )

    auth_usage_command(
        SimpleNamespace(provider="openai-codex", all_accounts=False, account="")
    )

    out = capsys.readouterr().out
    # Hint points the user at --all; the head entry's snapshot still renders.
    assert "2 pool entries" in out
    assert "--all" in out
    assert "codex-current" in out
    # The second entry never renders in the default path.
    assert "openai-codex-oauth-2" not in out


def test_auth_usage_lists_provider_account_credentials_skips_partial_rows():
    """Pool rows without an access_token are silently dropped."""
    from hermes_cli.auth_commands import _list_provider_account_credentials

    # Real ``_load_auth_store`` reads ~/.hermes/auth.json — that's not what
    # we want here. Patch it directly so the test is hermetic.
    import hermes_cli.auth_commands as ac

    ac.__dict__["_list_provider_account_credentials"]  # ensure imported
    fake_store = {
        "credential_pool": {
            "openai-codex": [
                {"label": "ok-1", "access_token": "tok-1"},
                {"label": "no-token", "access_token": ""},  # partial
                {"label": "ok-2", "access_token": "tok-2"},
                "not-a-dict",  # malformed
                {"label": "auto", "access_token": "tok-3"},
            ],
            "anthropic": [{"label": "anth", "access_token": "tok-a"}],
        }
    }
    import hermes_cli.auth as auth_mod

    original = auth_mod._load_auth_store
    auth_mod._load_auth_store = lambda: fake_store
    try:
        rows = _list_provider_account_credentials("openai-codex")
    finally:
        auth_mod._load_auth_store = original

    labels = [r["label"] for r in rows]
    # Partial / malformed rows are silently dropped; valid rows are kept
    # in original order so the user sees the pool's own head-to-tail view.
    assert labels == ["ok-1", "ok-2", "auto"]
    # And the access tokens were carried through untouched (the function
    # never redacts them, but it must not return empty / partial rows).
    assert all(r["access_token"] for r in rows)


