"""Bedrock Mantle Claude routing and credential-boundary regression tests."""

from unittest.mock import patch

import pytest

from hermes_cli import runtime_provider as rp


MANTLE_BASE = "https://bedrock-mantle.ap-northeast-1.api.aws/anthropic"


def test_runtime_uses_bedrock_key_for_mantle_anthropic(monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-key")
    monkeypatch.setenv("ANTHROPIC_TOKEN", "native-anthropic-token")
    monkeypatch.setattr(rp, "resolve_provider", lambda *a, **k: "anthropic")
    monkeypatch.setattr(
        rp,
        "_get_model_config",
        lambda: {
            "provider": "anthropic",
            "base_url": MANTLE_BASE,
            "api_mode": "anthropic_messages",
            "default": "anthropic.claude-opus-4-8",
        },
    )
    monkeypatch.setattr(rp, "load_pool", lambda *a, **k: None)

    resolved = rp.resolve_runtime_provider(requested="anthropic")

    assert resolved["provider"] == "anthropic"
    assert resolved["api_mode"] == "anthropic_messages"
    assert resolved["base_url"] == MANTLE_BASE
    assert resolved["api_key"] == "bedrock-key"


def test_runtime_mantle_does_not_fall_back_to_anthropic_credentials(monkeypatch):
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.setenv("ANTHROPIC_TOKEN", "native-anthropic-token")
    monkeypatch.setattr(rp, "resolve_provider", lambda *a, **k: "anthropic")
    monkeypatch.setattr(
        rp,
        "_get_model_config",
        lambda: {
            "provider": "anthropic",
            "base_url": MANTLE_BASE,
            "default": "anthropic.claude-opus-4-8",
        },
    )
    monkeypatch.setattr(rp, "load_pool", lambda *a, **k: None)

    with pytest.raises(rp.AuthError, match="AWS_BEARER_TOKEN_BEDROCK"):
        rp.resolve_runtime_provider(requested="anthropic")


def test_bedrock_api_key_flow_routes_claude_to_messages(monkeypatch):
    config = {}
    saved_config = {}
    saved_env = {}

    monkeypatch.setattr(
        "hermes_cli.config.get_env_value",
        lambda name: "bedrock-key" if name == "AWS_BEARER_TOKEN_BEDROCK" else "",
    )
    monkeypatch.setattr(
        "hermes_cli.config.save_env_value",
        lambda name, value: saved_env.__setitem__(name, value),
    )
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: config)
    monkeypatch.setattr(
        "hermes_cli.config.save_config",
        lambda value: saved_config.update(value),
    )
    monkeypatch.setattr(
        "hermes_cli.models.fetch_api_models",
        lambda key, base: [
            "anthropic.claude-opus-4-8",
            "openai.gpt-5.6",
        ],
    )
    monkeypatch.setattr(
        "hermes_cli.auth._prompt_model_selection",
        lambda *a, **k: "anthropic.claude-opus-4-8",
    )
    monkeypatch.setattr("hermes_cli.auth._save_model_choice", lambda model: None)
    monkeypatch.setattr("hermes_cli.auth.deactivate_provider", lambda: None)

    from hermes_cli.model_setup_flows import _model_flow_bedrock_api_key

    _model_flow_bedrock_api_key(config, "ap-northeast-1")

    model = saved_config["model"]
    assert model == {
        "provider": "anthropic",
        "base_url": MANTLE_BASE,
        "api_mode": "anthropic_messages",
        "key_env": "AWS_BEARER_TOKEN_BEDROCK",
    }
    assert "ANTHROPIC_API_KEY" not in saved_env
    assert "OPENAI_API_KEY" not in saved_env
