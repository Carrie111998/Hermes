"""Regression coverage for #95712's per-profile Claude Code suppression.

Claude Code credentials are process-user-global, while ``auth.json`` and its
``suppressed_sources`` policy are profile-scoped.  The shared reader must
therefore gate the external stores before either the normal resolver or the
auxiliary refresh path can see their token.
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def suppressed_profile(tmp_path, monkeypatch):
    """Create a profile whose auth store suppresses the Claude Code source."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "work-profile"))
    from hermes_cli.auth import suppress_credential_source

    suppress_credential_source("anthropic", "claude_code")
    return tmp_path


def _forbid_global_credential_stores(monkeypatch):
    def _forbidden():
        raise AssertionError("suppressed profile must not read global Claude Code credentials")

    monkeypatch.setattr(
        "agent.anthropic_adapter._read_claude_code_credentials_from_keychain",
        _forbidden,
    )
    monkeypatch.setattr(
        "agent.anthropic_adapter._read_claude_code_credentials_from_file",
        _forbidden,
    )


def test_suppression_skips_both_global_claude_code_stores(suppressed_profile, monkeypatch):
    _forbid_global_credential_stores(monkeypatch)

    from agent.anthropic_adapter import read_claude_code_credentials

    assert read_claude_code_credentials() is None


def test_resolver_falls_back_to_profile_pool_when_claude_code_is_suppressed(
    suppressed_profile, monkeypatch
):
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _forbid_global_credential_stores(monkeypatch)
    monkeypatch.setattr(
        "agent.anthropic_adapter._resolve_anthropic_pool_token",
        lambda: "profile-pool-token",
    )

    from agent.anthropic_adapter import resolve_anthropic_token

    assert resolve_anthropic_token() == "profile-pool-token"


def test_auxiliary_refresh_does_not_bypass_suppression(
    suppressed_profile, monkeypatch
):
    _forbid_global_credential_stores(monkeypatch)
    monkeypatch.setattr(
        "agent.anthropic_adapter.resolve_anthropic_token",
        lambda: "profile-pool-token",
    )
    stale_client = MagicMock()
    monkeypatch.setattr(
        "agent.auxiliary_client._client_cache",
        {("anthropic", False, None, None, None): (stale_client, "model", None)},
    )

    from agent.auxiliary_client import _refresh_provider_credentials

    assert _refresh_provider_credentials("anthropic") is True
    stale_client.close.assert_called_once()


def test_unsuppressed_profile_keeps_claude_code_credentials_available(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "default-profile"))
    monkeypatch.setattr(
        "agent.anthropic_adapter._read_claude_code_credentials_from_keychain",
        lambda: None,
    )
    monkeypatch.setattr(
        "agent.anthropic_adapter._read_claude_code_credentials_from_file",
        lambda: {"accessToken": "global-claude-code-token"},
    )

    from agent.anthropic_adapter import read_claude_code_credentials

    assert read_claude_code_credentials() == {"accessToken": "global-claude-code-token"}
