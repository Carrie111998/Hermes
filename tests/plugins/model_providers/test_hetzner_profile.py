"""Contract tests for the Hetzner AI Inference provider profile."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def hetzner_profile():
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("hetzner")
    assert profile is not None, "hetzner provider profile must be registered"
    return profile


def test_hetzner_profile_contract(hetzner_profile):
    profile = hetzner_profile
    assert profile.name == "hetzner"
    assert profile.auth_type == "api_key"
    assert profile.api_mode == "chat_completions"
    assert profile.base_url == "https://inference.hetzner.com/api/v1"
    assert profile.env_vars == ("HETZNER_API_KEY", "HETZNER_BASE_URL")
    assert profile.supports_vision is True
    assert profile.default_aux_model == "Qwen/Qwen3.6-35B-A3B-FP8"
    assert profile.fallback_models == ("Qwen/Qwen3.6-35B-A3B-FP8",)


@pytest.mark.parametrize("alias", ["hetzner-ai", "hetzner-inference"])
def test_hetzner_aliases_resolve(hetzner_profile, alias):
    import providers

    assert providers.get_provider_profile(alias) is hetzner_profile


def test_hetzner_is_auto_registered_for_auth(hetzner_profile):
    from hermes_cli.auth import PROVIDER_REGISTRY

    config = PROVIDER_REGISTRY["hetzner"]
    assert config.name == "Hetzner AI Inference"
    assert config.inference_base_url == "https://inference.hetzner.com/api/v1"
    assert config.api_key_env_vars == ("HETZNER_API_KEY",)
    assert config.base_url_env_var == "HETZNER_BASE_URL"


def test_hetzner_credentials_and_alias_resolve(monkeypatch, hetzner_profile):
    from hermes_cli.auth import (
        resolve_api_key_provider_credentials,
        resolve_provider,
    )

    monkeypatch.setenv("HETZNER_API_KEY", "test-key")
    monkeypatch.delenv("HETZNER_BASE_URL", raising=False)

    assert resolve_provider("hetzner-ai") == "hetzner"
    assert resolve_api_key_provider_credentials("hetzner") == {
        "provider": "hetzner",
        "api_key": "test-key",
        "base_url": "https://inference.hetzner.com/api/v1",
        "source": "HETZNER_API_KEY",
    }


def test_hetzner_model_picker_has_offline_fallback(monkeypatch, hetzner_profile):
    from hermes_cli import models

    monkeypatch.delenv("HETZNER_API_KEY", raising=False)
    monkeypatch.delenv("HETZNER_BASE_URL", raising=False)

    assert models.provider_model_ids("hetzner") == [
        "Qwen/Qwen3.6-35B-A3B-FP8"
    ]


def test_hetzner_fetch_models_uses_bearer_and_standard_endpoint(hetzner_profile):
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = json.dumps(
        {"data": [{"id": "Qwen/Qwen3.6-35B-A3B-FP8"}]}
    ).encode()

    with patch(
        "hermes_cli.urllib_security.open_credentialed_url",
        return_value=response,
    ) as opener:
        models = hetzner_profile.fetch_models(api_key="secret-token")

    assert models == ["Qwen/Qwen3.6-35B-A3B-FP8"]
    request = opener.call_args.args[0]
    assert request.full_url == "https://inference.hetzner.com/api/v1/models"
    assert request.get_header("Authorization") == "Bearer secret-token"


@pytest.mark.parametrize(
    ("reasoning_config", "expected"),
    [
        ({"enabled": True, "effort": "medium"}, True),
        ({"enabled": False, "effort": "none"}, False),
    ],
)
def test_hetzner_maps_reasoning_to_chat_template_kwargs(
    hetzner_profile, reasoning_config, expected
):
    extra_body, top_level = hetzner_profile.build_api_kwargs_extras(
        reasoning_config=reasoning_config,
        model="Qwen/Qwen3.6-35B-A3B-FP8",
    )

    assert extra_body == {
        "chat_template_kwargs": {"enable_thinking": expected}
    }
    assert top_level == {}


def test_hetzner_omits_thinking_when_unconfigured(hetzner_profile):
    assert hetzner_profile.build_api_kwargs_extras(reasoning_config=None) == ({}, {})


def test_hetzner_thinking_reaches_chat_completion_request(hetzner_profile):
    from agent.transports.chat_completions import ChatCompletionsTransport

    kwargs = ChatCompletionsTransport().build_kwargs(
        model="Qwen/Qwen3.6-35B-A3B-FP8",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        provider_profile=hetzner_profile,
        reasoning_config={"enabled": False, "effort": "none"},
        request_overrides=None,
    )

    assert kwargs["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }
