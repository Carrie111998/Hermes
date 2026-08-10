import pytest

from hermes_cli.runtime_provider import resolve_runtime_provider
from providers import get_provider_profile


def test_claude_cli_provider_profile_is_external_process_and_text_only():
    profile = get_provider_profile("claude-cli")
    assert profile is not None
    assert profile.api_mode == "claude_cli"
    assert profile.auth_type == "external_process"
    assert profile.base_url == ""
    assert profile.env_vars == ()
    assert profile.supports_vision is False
    assert profile.fetch_models() == ["claude-opus-5"]


def test_explicit_runtime_resolution_requires_exact_pair(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    resolved = resolve_runtime_provider(
        requested="claude-cli", target_model="claude-opus-5"
    )
    assert resolved == {
        "provider": "claude-cli",
        "api_mode": "claude_cli",
        "base_url": "",
        "api_key": "",
        "source": "external-process",
        "credential_contract": "external_process",
        "requested_provider": "claude-cli",
    }

    with pytest.raises(ValueError, match="exact provider/model pair"):
        resolve_runtime_provider(requested="claude-cli", target_model="claude-sonnet-5")
    with pytest.raises(ValueError, match="api_key"):
        resolve_runtime_provider(
            requested="claude-cli",
            target_model="claude-opus-5",
            explicit_api_key="not-allowed",
        )
    with pytest.raises(ValueError, match="base_url"):
        resolve_runtime_provider(
            requested="claude-cli",
            target_model="claude-opus-5",
            explicit_base_url="https://not-allowed.invalid",
        )


def test_configured_provider_is_explicit_but_auto_remains_unchanged(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.runtime_provider._get_model_config",
        lambda: {"provider": "claude-cli", "default": "claude-opus-5"},
    )
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    resolved = resolve_runtime_provider(requested="auto")
    assert resolved["provider"] == "claude-cli"
    assert resolved["api_mode"] == "claude_cli"

    monkeypatch.setattr(
        "hermes_cli.runtime_provider._get_model_config",
        lambda: {
            "provider": "claude-cli",
            "default": "claude-opus-5",
            "base_url": "https://not-allowed.invalid",
        },
    )
    with pytest.raises(ValueError, match="base_url"):
        resolve_runtime_provider(requested="auto")
