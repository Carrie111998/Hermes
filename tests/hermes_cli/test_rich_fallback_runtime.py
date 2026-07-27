"""Contract tests for rich, route-specific fallback runtime resolution."""

from unittest.mock import patch

import pytest


def test_rich_fallback_runtime_uses_central_resolver_and_route_overrides():
    from hermes_cli.fallback_config import resolve_fallback_runtime

    entry = {
        "provider": "custom:local",
        "model": "large-local",
        "base_url": "http://localhost:1234/v1",
        "transport": "chat_completions",
        "context_length": 65_536,
        "max_output_tokens": 16_384,
        "extra_body": {
            "temperature": 1.0,
            "top_p": 0.95,
            "top_k": 20,
            "presence_penalty": 1.5,
        },
        "request_timeout_seconds": 600,
        "model_transition_policy": "sequential",
    }
    named_runtime = {
        "provider": "custom",
        "requested_provider": "custom:local",
        "base_url": "http://localhost:1234/v1",
        "api_key": "local-key",
        "api_mode": "chat_completions",
        "context_length": 32_768,
        "max_output_tokens": 4_096,
        "request_overrides": {"extra_body": {"named_default": True}},
    }

    with patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        return_value=named_runtime,
    ) as resolve:
        runtime = resolve_fallback_runtime(entry)

    resolve.assert_called_once_with(
        requested="custom:local",
        explicit_api_key=None,
        explicit_base_url="http://localhost:1234/v1",
        target_model="large-local",
    )
    assert runtime["model"] == "large-local"
    assert runtime["api_mode"] == "chat_completions"
    assert runtime["context_length"] == 65_536
    assert runtime["max_output_tokens"] == 16_384
    assert runtime["request_timeout_seconds"] == 600.0
    assert runtime["model_transition_policy"] == "sequential"
    assert runtime["request_overrides"] == {
        "extra_body": {
            "named_default": True,
            "temperature": 1.0,
            "top_p": 0.95,
            "top_k": 20,
            "presence_penalty": 1.5,
        }
    }


def test_fallback_normalization_filters_unknown_route_keys():
    from hermes_cli.fallback_config import get_fallback_chain

    chain = get_fallback_chain(
        {
            "fallback_providers": [
                {
                    "provider": "lmstudio",
                    "model": "local-model",
                    "base_url": "http://localhost:1234/v1/",
                    "context_length": 65_536,
                    "max_tokens": 16_384,
                    "extra_body": {"temperature": 0.8},
                    "extra_headers": {"X-Route": 7, "X-Drop": None},
                    "arbitrary_payload": {"unsafe": True},
                    "request_overrides": {"stream": True},
                }
            ]
        }
    )

    assert chain == [
        {
            "provider": "lmstudio",
            "model": "local-model",
            "base_url": "http://localhost:1234/v1",
            "context_length": 65_536,
            "max_tokens": 16_384,
            "extra_body": {"temperature": 0.8},
            "extra_headers": {"X-Route": "7"},
        }
    ]


def test_fallback_api_mode_uses_canonical_runtime_allowlist():
    from hermes_cli.fallback_config import normalize_fallback_entry
    from hermes_cli.runtime_provider import _VALID_API_MODES

    for mode in _VALID_API_MODES:
        normalized = normalize_fallback_entry(
            {"provider": "custom", "model": "model", "api_mode": mode.upper()}
        )
        assert normalized is not None
        assert normalized["api_mode"] == mode

    normalized = normalize_fallback_entry(
        {"provider": "custom", "model": "model", "api_mode": "future_stale_mode"}
    )
    assert normalized is not None
    assert "api_mode" not in normalized


def test_same_endpoint_distinct_models_remain_ordered_and_nondeduplicated():
    from hermes_cli.fallback_config import get_fallback_chain

    first = {
        "provider": "lmstudio",
        "model": "large-local",
        "base_url": "http://localhost:1234/v1",
    }
    second = {
        "provider": "lmstudio",
        "model": "small-local",
        "base_url": "http://localhost:1234/v1",
    }
    assert get_fallback_chain({"fallback_providers": [first, second]}) == [first, second]


def test_legacy_simple_fallback_merge_semantics_are_unchanged():
    from hermes_cli.fallback_config import get_fallback_chain

    modern = {"provider": "openrouter", "model": "modern"}
    duplicate_legacy = {"provider": "openrouter", "model": "modern"}
    distinct_legacy = {"provider": "openai", "model": "legacy"}
    assert get_fallback_chain(
        {
            "fallback_providers": [modern],
            "fallback_model": [duplicate_legacy, distinct_legacy],
        }
    ) == [modern, distinct_legacy]


def test_fallback_runtime_does_not_invent_provider_specific_or_anthropic_routing():
    from hermes_cli.fallback_config import resolve_fallback_runtime

    resolved = {
        "provider": "lmstudio",
        "requested_provider": "lmstudio",
        "base_url": "http://localhost:1234/v1",
        "api_key": "local",
        "api_mode": "chat_completions",
    }
    with patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        return_value=resolved,
    ):
        runtime = resolve_fallback_runtime(
            {"provider": "lmstudio", "model": "local-model"}
        )

    assert runtime["provider"] == "lmstudio"
    assert runtime["api_mode"] == "chat_completions"
    assert "anthropic" not in repr(runtime).lower()


def test_fallback_runtime_does_not_swallow_disabled_provider_error():
    from hermes_cli.fallback_config import resolve_fallback_runtime

    with patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        side_effect=ValueError(
            "provider 'lmstudio' is disabled in config "
            "(providers.lmstudio.enabled: false)"
        ),
    ):
        with pytest.raises(ValueError, match="is disabled in config"):
            resolve_fallback_runtime(
                {"provider": "lmstudio", "model": "local-model"}
            )


def test_fallback_runtime_alias_cannot_bypass_disabled_canonical_provider():
    from hermes_cli.fallback_config import resolve_fallback_runtime

    config = {"providers": {"kimi-coding": {"enabled": False}}}
    with patch("hermes_cli.config.load_config", return_value=config):
        with pytest.raises(ValueError, match="providers.kimi-coding.enabled"):
            resolve_fallback_runtime(
                {
                    "provider": "kimi",
                    "model": "kimi-k2.5",
                    "api_key": "fixture-key",
                }
            )
