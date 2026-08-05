"""Deterministic local guard for cross-provider fallback context."""

from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


def test_sensitive_fallback_guard_is_disabled_by_default():
    from hermes_cli.config import DEFAULT_CONFIG

    guard = DEFAULT_CONFIG["security"]["sensitive_fallback_guard"]
    assert guard["enabled"] is False
    assert guard["mode"] == "block"


@pytest.mark.parametrize(
    ("messages", "markers", "prefixes", "expected"),
    [
        (
            [{"role": "user", "content": "ordinary", "metadata": {"sensitive": True}}],
            [],
            [],
            ("explicit_marker", "message_metadata"),
        ),
        (
            [{"role": "system", "content": "INTERNAL_ONLY instructions"}],
            ["INTERNAL_ONLY"],
            [],
            ("explicit_marker", "configured_marker"),
        ),
        (
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path":"/vault/private/client.txt"}',
                            }
                        }
                    ],
                }
            ],
            [],
            ["/vault/private"],
            ("sensitive_path", "configured_prefix"),
        ),
        (
            [{"role": "tool", "content": "Authorization: Bearer sk-test-1234567890abcdef"}],
            [],
            [],
            ("secret_material", "redaction_match"),
        ),
        (
            [
                {
                    "role": "tool",
                    "content": "-----BEGIN PRIVATE KEY-----\nabc123\n-----END PRIVATE KEY-----",
                }
            ],
            [],
            [],
            ("secret_material", "redaction_match"),
        ),
        (
            [{"role": "user", "content": "주민번호 900101-1234567"}],
            [],
            [],
            ("pii", "korean_resident_number"),
        ),
        (
            [{"role": "user", "content": "연락처는 010-1234-5678입니다"}],
            [],
            [],
            ("pii", "phone_number"),
        ),
        (
            [{"role": "user", "content": "Contact client@example.com"}],
            [],
            [],
            ("pii", "email_address"),
        ),
        (
            [{"role": "user", "content": "고객 계약 비용과 납기를 검토해 주세요"}],
            [],
            [],
            ("client_context", "contract_contact_deadline_cost"),
        ),
    ],
)
def test_local_classifier_returns_only_category_and_reason_codes(
    messages, markers, prefixes, expected
):
    from agent.redact import classify_sensitive_fallback_context

    assert classify_sensitive_fallback_context(
        messages,
        explicit_markers=markers,
        sensitive_path_prefixes=prefixes,
    ) == expected


def test_local_classifier_allows_non_sensitive_probe_prompt():
    from agent.redact import classify_sensitive_fallback_context

    assert classify_sensitive_fallback_context(
        [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Reply with exactly FALLBACK_PROBE_OK."},
        ]
    ) is None


def test_static_system_customer_policy_does_not_block_every_turn():
    from agent.redact import classify_sensitive_fallback_context

    assert classify_sensitive_fallback_context(
        [
            {
                "role": "system",
                "content": (
                    "고객 계약, 연락처, 납기, 비용을 다룰 때는 확인한다. "
                    "Do not disclose client contract or deadline data."
                ),
            },
            {"role": "user", "content": "일반 로컬 테스트를 실행해 주세요."},
        ]
    ) is None


@pytest.fixture()
def fallback_agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            model="primary-model",
            provider="openrouter",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock(name="primary_client")
    agent._fallback_chain = [
        {"provider": "deepseek", "model": "deepseek-chat"}
    ]
    agent._fallback_index = 0
    agent._fallback_activated = False
    agent._unavailable_fallback_keys = set()
    agent._sensitive_fallback_guard = {
        "enabled": True,
        "mode": "block",
        "explicit_markers": [],
        "sensitive_path_prefixes": [],
    }
    agent._current_fallback_context_messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Run a safe local fallback probe."},
    ]
    agent._buffer_status = MagicMock()
    return agent


def _fallback_client(provider="deepseek"):
    client = MagicMock(name=f"{provider}_client")
    client.base_url = (
        "https://openrouter.ai/api/v1"
        if provider == "openrouter"
        else "https://api.deepseek.com/v1"
    )
    client.api_key = "fallback-key-1234567890"
    client._custom_headers = None
    client.default_headers = None
    return client


def _activate_with_resolver(agent, resolver):
    with (
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            side_effect=resolver,
        ) as mock_resolver,
        patch("agent.credential_pool.load_pool", return_value=None),
        patch("agent.model_metadata.get_model_context_length", return_value=128000),
    ):
        result = agent._try_activate_fallback()
    return result, mock_resolver


def test_sensitive_cross_provider_fallback_blocks_before_resolver(fallback_agent):
    fallback_agent._current_fallback_context_messages[1]["content"] = (
        "Contact client@example.com"
    )
    primary_client = fallback_agent.client

    result, resolver = _activate_with_resolver(
        fallback_agent,
        lambda *args, **kwargs: (_fallback_client(), None),
    )

    assert result is False
    resolver.assert_not_called()
    assert fallback_agent.provider == "openrouter"
    assert fallback_agent.model == "primary-model"
    assert fallback_agent.client is primary_client
    assert ("deepseek", "deepseek-chat", "") not in (
        fallback_agent._unavailable_fallback_keys
    )
    fallback_agent._buffer_status.assert_called_with(
        "🔒 Fallback blocked because sensitive context was detected."
    )


def test_sensitive_block_does_not_disable_fallback_for_later_safe_turn(fallback_agent):
    fallback_agent._current_fallback_context_messages[1]["content"] = (
        "Contact client@example.com"
    )
    blocked, first_resolver = _activate_with_resolver(
        fallback_agent,
        lambda *args, **kwargs: (_fallback_client(), None),
    )
    assert blocked is False
    first_resolver.assert_not_called()

    fallback_agent._fallback_index = 0
    fallback_agent._current_fallback_context_messages[1]["content"] = (
        "Run a safe local fallback probe."
    )
    activated, second_resolver = _activate_with_resolver(
        fallback_agent,
        lambda *args, **kwargs: (_fallback_client(), None),
    )

    assert activated is True
    second_resolver.assert_called_once()
    assert fallback_agent.provider == "deepseek"


def test_non_sensitive_cross_provider_fallback_still_calls_resolver(fallback_agent):
    from agent.redact import classify_sensitive_fallback_context

    with patch(
        "agent.redact.classify_sensitive_fallback_context",
        wraps=classify_sensitive_fallback_context,
    ) as classifier:
        result, resolver = _activate_with_resolver(
            fallback_agent,
            lambda *args, **kwargs: (_fallback_client(), None),
        )

    assert result is True
    classifier.assert_called_once()
    resolver.assert_called_once()
    assert fallback_agent.provider == "deepseek"


def test_same_provider_fallback_bypasses_cross_provider_guard(fallback_agent):
    fallback_agent._fallback_chain = [
        {
            "provider": "openrouter",
            "model": "different-model",
            "base_url": "https://openrouter.ai/api/v1/",
        }
    ]
    fallback_agent._current_fallback_context_messages[1]["content"] = (
        "Contact client@example.com"
    )

    with patch(
        "agent.redact.classify_sensitive_fallback_context"
    ) as classifier:
        result, resolver = _activate_with_resolver(
            fallback_agent,
            lambda *args, **kwargs: (_fallback_client("openrouter"), None),
        )

    assert result is True
    classifier.assert_not_called()
    resolver.assert_called_once()
    assert fallback_agent.provider == "openrouter"
    assert fallback_agent.model == "different-model"


def test_same_provider_different_endpoint_is_blocked_for_sensitive_context(
    fallback_agent,
):
    fallback_agent._fallback_chain = [
        {
            "provider": "openrouter",
            "model": "different-model",
            "base_url": "https://private-proxy.example/v1",
        }
    ]
    fallback_agent._current_fallback_context_messages[1]["content"] = (
        "Contact client@example.com"
    )

    result, resolver = _activate_with_resolver(
        fallback_agent,
        lambda *args, **kwargs: (_fallback_client("openrouter"), None),
    )

    assert result is False
    resolver.assert_not_called()
    assert fallback_agent.provider == "openrouter"
    assert fallback_agent.base_url == "https://openrouter.ai/api/v1"


def test_later_tool_output_can_make_current_turn_sensitive(fallback_agent):
    fallback_agent._current_fallback_context_messages.extend(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": "Customer email: client@example.com",
            },
        ]
    )

    result, resolver = _activate_with_resolver(
        fallback_agent,
        lambda *args, **kwargs: (_fallback_client(), None),
    )

    assert result is False
    resolver.assert_not_called()


def test_classifier_error_fails_closed_before_resolver(fallback_agent):
    with patch(
        "agent.redact.classify_sensitive_fallback_context",
        side_effect=RuntimeError("classifier broke"),
    ):
        result, resolver = _activate_with_resolver(
            fallback_agent,
            lambda *args, **kwargs: (_fallback_client(), None),
        )

    assert result is False
    resolver.assert_not_called()
    assert fallback_agent.provider == "openrouter"


def test_disabled_guard_preserves_cross_provider_fallback(fallback_agent):
    fallback_agent._sensitive_fallback_guard["enabled"] = False
    fallback_agent._current_fallback_context_messages[1]["content"] = (
        "Contact client@example.com"
    )

    with patch(
        "agent.redact.classify_sensitive_fallback_context"
    ) as classifier:
        result, resolver = _activate_with_resolver(
            fallback_agent,
            lambda *args, **kwargs: (_fallback_client(), None),
        )

    assert result is True
    classifier.assert_not_called()
    resolver.assert_called_once()
    assert fallback_agent.provider == "deepseek"
