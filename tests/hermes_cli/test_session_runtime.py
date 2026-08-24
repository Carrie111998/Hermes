import json
from types import SimpleNamespace

from hermes_cli.session_runtime import copy_non_secret_session_runtime


def test_copy_agent_runtime_keeps_requested_and_resolved_provider_without_secrets():
    agent = SimpleNamespace(
        model="gpt-5.4",
        requested_provider="openai-codex",
        provider="openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        api_mode="codex_responses",
        responses_transport="websocket-cached",
        api_key="must-not-persist",
    )

    copied = copy_non_secret_session_runtime(
        agent,
        {"reasoning_config": {"effort": "high"}, "_branched_from": "parent"},
    )

    assert copied == {
        "model": "gpt-5.4",
        "requested_provider": "openai-codex",
        "provider": "openai-codex",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "api_mode": "codex_responses",
        "responses_transport": "websocket-cached",
        "reasoning_config": {"effort": "high"},
        "_branched_from": "parent",
    }


def test_copy_persisted_runtime_prefers_gateway_shape_and_keeps_lineage():
    row = {
        "model": "gpt-5.4",
        "billing_provider": "openrouter",
        "model_config": json.dumps(
            {
                "provider": "stale-provider",
                "responses_transport": "sse",
                "gateway_runtime": {
                    "requested_provider": "openai-codex",
                    "provider": "openai-codex",
                    "base_url": "https://chatgpt.com/backend-api/codex",
                    "api_mode": "codex_responses",
                    "responses_transport": "websocket-cached",
                },
            }
        ),
    }

    copied = copy_non_secret_session_runtime(row, {"_branched_from": "parent"})

    assert copied["model"] == "gpt-5.4"
    assert copied["requested_provider"] == "openai-codex"
    assert copied["provider"] == "openai-codex"
    assert copied["responses_transport"] == "websocket-cached"
    assert copied["_branched_from"] == "parent"


def test_clear_missing_removes_stale_route_fields_but_preserves_metadata():
    copied = copy_non_secret_session_runtime(
        SimpleNamespace(model="gpt-5.4", provider="openrouter"),
        {
            "base_url": "https://stale.invalid",
            "api_mode": "codex_responses",
            "responses_transport": "websocket-cached",
            "reasoning_config": {"effort": "low"},
        },
        clear_missing=True,
    )

    assert copied == {
        "model": "gpt-5.4",
        "provider": "openrouter",
        "reasoning_config": {"effort": "low"},
    }
