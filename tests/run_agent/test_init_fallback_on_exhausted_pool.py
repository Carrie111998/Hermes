"""Regression test for #17929: AIAgent.__init__ should try fallback_model
when primary provider credentials are exhausted."""
import pytest
from unittest.mock import patch, MagicMock
from run_agent import AIAgent


def _make_tool_defs():
    return [{"type": "function", "function": {"name": "web_search",
             "description": "search", "parameters": {"type": "object", "properties": {}}}}]


def _mock_client(api_key="fb-key-1234567890", base_url="https://fb.example.com/v1"):
    c = MagicMock()
    c.api_key = api_key
    c.base_url = base_url
    c._default_headers = None
    return c


def test_init_tries_fallback_when_primary_returns_none():
    """When resolve_provider_client returns None for primary but succeeds for
    a fallback entry, __init__ should NOT raise RuntimeError."""
    fb = _mock_client()

    def fake_resolve(provider, model=None, raw_codex=False,
                     explicit_base_url=None, explicit_api_key=None):
        if provider == "tencent-token-plan":
            return fb, "kimi2.5"
        return None, None  # primary exhausted

    with patch("agent.auxiliary_client.resolve_provider_client", side_effect=fake_resolve), \
         patch("run_agent.get_tool_definitions", return_value=_make_tool_defs()), \
         patch("run_agent.check_toolset_requirements", return_value={}), \
         patch("run_agent.OpenAI", return_value=MagicMock()):

        agent = AIAgent(
            provider="alibaba-coding-plan",
            model="qwen3.6-plus",
            api_key=None,
            base_url=None,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=[{"provider": "tencent-token-plan", "model": "kimi2.5"}],
        )
        assert agent.provider == "tencent-token-plan"
        assert agent.model == "kimi2.5"
        assert agent._fallback_activated is True


def test_init_named_custom_fallback_separates_transport_identity_and_pool():
    """Init-time fallback must not store a named custom provider as transport."""
    fallback_client = _mock_client(
        api_key="provider-b-key",
        base_url="https://gateway.example/v1",
    )
    fallback_pool = MagicMock(name="provider_b_pool")
    fallback_pool.provider = "custom:provider-b"
    fallback_pool.has_credentials.return_value = True

    def fake_resolve(provider, model=None, raw_codex=False,
                     explicit_base_url=None, explicit_api_key=None):
        if provider == "provider-b":
            return fallback_client, "fallback-model"
        return None, None

    with (
        patch("agent.auxiliary_client.resolve_provider_client", side_effect=fake_resolve),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value={
                "provider": "custom",
                "requested_provider": "provider-b",
                "api_mode": "chat_completions",
                "credential_pool": fallback_pool,
            },
        ) as resolve_runtime,
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            provider="primary-unavailable",
            model="primary-model",
            api_key=None,
            base_url=None,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=[{"provider": "provider-b", "model": "fallback-model"}],
        )

    resolve_runtime.assert_called_once_with(
        requested="provider-b",
        target_model="fallback-model",
        explicit_base_url=None,
        explicit_api_key=None,
    )
    assert agent.provider == "custom"
    assert agent.requested_provider == "custom:provider-b"
    assert agent._credential_pool is fallback_pool
    assert agent._primary_runtime["provider"] == "custom"
    assert agent._primary_runtime["requested_provider"] == "custom:provider-b"


def test_init_named_custom_fallback_uses_resolved_api_mode():
    """Init-time fallback must retain the resolver-selected wire protocol."""
    fallback_client = _mock_client(
        api_key="provider-b-key",
        base_url="https://gateway.example/v1",
    )

    def fake_resolve(provider, model=None, raw_codex=False,
                     explicit_base_url=None, explicit_api_key=None):
        if provider == "provider-b":
            return fallback_client, "fallback-model"
        return None, None

    with (
        patch("agent.auxiliary_client.resolve_provider_client", side_effect=fake_resolve),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value={
                "provider": "custom",
                "requested_provider": "provider-b",
                "api_mode": "codex_responses",
            },
        ),
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            provider="primary-unavailable",
            model="primary-model",
            api_key=None,
            base_url=None,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=[{"provider": "provider-b", "model": "fallback-model"}],
        )

    assert agent.api_mode == "codex_responses"
    assert agent._primary_runtime["api_mode"] == "codex_responses"


def test_init_fallback_does_not_adopt_empty_runtime_pool():
    """Init fallback must use the same credential-bearing pool standard as runtime fallback."""
    fallback_client = _mock_client()
    empty_pool = MagicMock(name="empty_fallback_pool")
    empty_pool.provider = "custom:provider-b"
    empty_pool.has_credentials.return_value = False

    def fake_resolve(provider, model=None, raw_codex=False,
                     explicit_base_url=None, explicit_api_key=None):
        if provider == "provider-b":
            return fallback_client, "fallback-model"
        return None, None

    with (
        patch("agent.auxiliary_client.resolve_provider_client", side_effect=fake_resolve),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value={
                "provider": "custom",
                "requested_provider": "custom:provider-b",
                "api_mode": "chat_completions",
                "credential_pool": empty_pool,
            },
        ),
        patch("agent.credential_pool.load_pool", return_value=None),
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            provider="primary-unavailable",
            model="primary-model",
            api_key=None,
            base_url=None,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=[{"provider": "provider-b", "model": "fallback-model"}],
        )

    assert agent._credential_pool is None


def test_init_named_custom_anthropic_fallback_builds_native_client():
    """Init fallback must dispatch a resolved Anthropic-wire route natively."""
    fallback_client = _mock_client(
        api_key="provider-b-key",
        base_url="https://gateway.example/anthropic",
    )
    native_client = MagicMock(name="native_anthropic_client")

    def fake_resolve(provider, model=None, raw_codex=False,
                     explicit_base_url=None, explicit_api_key=None):
        if provider == "provider-b":
            return fallback_client, "fallback-model"
        return None, None

    with (
        patch("agent.auxiliary_client.resolve_provider_client", side_effect=fake_resolve),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value={
                "provider": "custom",
                "requested_provider": "provider-b",
                "api_mode": "anthropic_messages",
            },
        ),
        patch(
            "agent.anthropic_adapter.build_anthropic_client",
            return_value=native_client,
        ) as build_anthropic,
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()) as openai,
    ):
        agent = AIAgent(
            provider="primary-unavailable",
            model="primary-model",
            api_key=None,
            base_url=None,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=[{"provider": "provider-b", "model": "fallback-model"}],
        )

    assert agent.api_mode == "anthropic_messages"
    assert agent._anthropic_client is native_client
    assert agent.client is None
    assert agent._primary_runtime["api_mode"] == "anthropic_messages"
    build_anthropic.assert_called_once_with(
        "provider-b-key", "https://gateway.example/anthropic", timeout=None,
    )
    openai.assert_not_called()


@pytest.mark.parametrize("api_mode", ["bedrock_converse", "codex_app_server"])
def test_init_named_custom_fallback_skips_unsupported_native_api_modes(api_mode):
    """Named custom fallback must not enter a native-only runtime it cannot build."""
    unsupported_client = _mock_client(
        api_key="unsupported-key",
        base_url="https://unsupported.example/v1",
    )
    supported_client = _mock_client(
        api_key="provider-b-key",
        base_url="https://gateway.example/v1",
    )

    def fake_resolve(provider, model=None, raw_codex=False,
                     explicit_base_url=None, explicit_api_key=None):
        if provider == "unsupported-provider":
            return unsupported_client, "unsupported-model"
        if provider == "provider-b":
            return supported_client, "fallback-model"
        return None, None

    def fake_runtime(*, requested=None, target_model=None,
                     explicit_base_url=None, explicit_api_key=None):
        if requested == "unsupported-provider":
            return {
                "provider": "custom",
                "requested_provider": requested,
                "api_mode": api_mode,
            }
        if requested == "provider-b":
            return {
                "provider": "custom",
                "requested_provider": requested,
                "api_mode": "chat_completions",
            }
        raise AssertionError(f"unexpected runtime request: {requested!r}")

    with (
        patch("agent.auxiliary_client.resolve_provider_client", side_effect=fake_resolve) as resolve_client,
        patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=fake_runtime),
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            provider="primary-unavailable",
            model="primary-model",
            api_key=None,
            base_url=None,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=[
                {"provider": "unsupported-provider", "model": "unsupported-model"},
                {"provider": "provider-b", "model": "fallback-model"},
            ],
        )

    assert agent.provider == "custom"
    assert agent.requested_provider == "custom:provider-b"
    assert agent.model == "fallback-model"
    assert all(call.args[0] != "unsupported-provider" for call in resolve_client.call_args_list)


def test_init_raises_when_no_fallback_configured():
    """When primary returns None and no fallback is set, should raise."""
    with patch("agent.auxiliary_client.resolve_provider_client", return_value=(None, None)), \
         patch("run_agent.get_tool_definitions", return_value=_make_tool_defs()), \
         patch("run_agent.check_toolset_requirements", return_value={}), \
         patch("run_agent.OpenAI", return_value=MagicMock()):

        with pytest.raises(RuntimeError, match="no API key was found"):
            AIAgent(
                provider="alibaba-coding-plan",
                model="qwen3.6-plus",
                api_key=None,
                base_url=None,
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                fallback_model=None,
            )
