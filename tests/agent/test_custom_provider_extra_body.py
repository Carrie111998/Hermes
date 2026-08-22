from types import SimpleNamespace

from agent.agent_init import _merge_custom_provider_extra_body




def test_custom_provider_extra_body_preserves_caller_override():
    agent = SimpleNamespace(
        provider="custom",
        model="google/gemma-4-31b-it",
        base_url="https://example.test/v1",
        request_overrides={
            "extra_body": {
                "reasoning_effort": "low",
                "caller_only": True,
            }
        },
    )

    _merge_custom_provider_extra_body(
        agent,
        [
            {
                "name": "gemma",
                "base_url": "https://example.test/v1",
                "model": "google/gemma-4-31b-it",
                "extra_body": {
                    "enable_thinking": True,
                    "reasoning_effort": "high",
                },
            }
        ],
    )

    assert agent.request_overrides["extra_body"] == {
        "enable_thinking": True,
        "reasoning_effort": "low",
        "caller_only": True,
    }




def test_named_custom_provider_extra_body_matches_provider_key():
    agent = SimpleNamespace(
        provider="custom:zai-coding-plan",
        model="glm-5.2",
        base_url="https://api.z.ai/api/coding/paas/v4",
        request_overrides={},
    )

    _merge_custom_provider_extra_body(
        agent,
        [
            {
                "provider_key": "other-provider",
                "name": "Other Provider",
                "base_url": "https://api.z.ai/api/coding/paas/v4",
                "model": "glm-5.2",
                "extra_body": {"enable_thinking": True},
            },
            {
                "provider_key": "zai-coding-plan",
                "name": "Z.AI Coding Plan",
                "base_url": "https://api.z.ai/api/coding/paas/v4/",
                "model": "glm-5.2",
                "extra_body": {"enable_thinking": False},
            },
        ],
    )

    assert agent.request_overrides == {"extra_body": {"enable_thinking": False}}


def test_custom_provider_parallel_tool_calls_preserves_false_and_caller_overrides():
    agent = SimpleNamespace(
        provider="custom:vendor",
        model="vendor-model",
        base_url="https://api.vendor.example.com/v1",
        request_overrides={
            "service_tier": "priority",
            "extra_body": {"caller_only": True},
        },
    )

    _merge_custom_provider_extra_body(
        agent,
        [
            {
                "provider_key": "vendor",
                "name": "Vendor",
                "base_url": "https://api.vendor.example.com/v1/",
                "model": "vendor-model",
                "parallel_tool_calls": False,
                "extra_body": {"provider_only": True},
            }
        ],
    )

    assert agent.request_overrides == {
        "service_tier": "priority",
        "parallel_tool_calls": False,
        "extra_body": {"provider_only": True, "caller_only": True},
    }


def test_custom_provider_parallel_tool_calls_merges_true_without_extra_body():
    agent = SimpleNamespace(
        provider="custom",
        model="vendor-model",
        base_url="https://api.vendor.example.com/v1",
        request_overrides={},
    )

    _merge_custom_provider_extra_body(
        agent,
        [
            {
                "name": "Vendor",
                "base_url": "https://api.vendor.example.com/v1",
                "model": "vendor-model",
                "parallel_tool_calls": True,
            }
        ],
    )

    assert agent.request_overrides == {"parallel_tool_calls": True}


def test_parallel_tool_calls_does_not_change_non_custom_provider():
    agent = SimpleNamespace(
        provider="openrouter",
        model="vendor-model",
        base_url="https://api.vendor.example.com/v1",
        request_overrides={"service_tier": "priority"},
    )

    _merge_custom_provider_extra_body(
        agent,
        [
            {
                "name": "Vendor",
                "base_url": "https://api.vendor.example.com/v1",
                "parallel_tool_calls": True,
            }
        ],
    )

    assert agent.request_overrides == {"service_tier": "priority"}


def test_parallel_tool_calls_does_not_change_non_matching_custom_endpoint():
    agent = SimpleNamespace(
        provider="custom",
        model="vendor-model",
        base_url="https://other.example.com/v1",
        request_overrides={"service_tier": "priority"},
    )

    _merge_custom_provider_extra_body(
        agent,
        [
            {
                "name": "Vendor",
                "base_url": "https://api.vendor.example.com/v1",
                "parallel_tool_calls": True,
            }
        ],
    )

    assert agent.request_overrides == {"service_tier": "priority"}
