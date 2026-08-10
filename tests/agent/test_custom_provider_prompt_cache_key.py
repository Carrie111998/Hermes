"""End-to-end config plumbing for custom-provider prompt cache keys."""

from agent.agent_init import (
    _custom_provider_supports_prompt_cache_key_for_agent,
)
from agent.transports.chat_completions import ChatCompletionsTransport
from hermes_cli.config import get_compatible_custom_providers
from providers import get_provider_profile


def test_custom_provider_capability_reaches_chat_completions_transport():
    config = {
        "providers": {
            "local-gateway": {
                "api": "https://gateway.example.com/v1",
                "transport": "chat_completions",
                "models": {"deepseek-chat": {}},
                "supports_prompt_cache_key": True,
            }
        }
    }
    entries = get_compatible_custom_providers(config)
    capability = _custom_provider_supports_prompt_cache_key_for_agent(
        provider="custom:local-gateway",
        model="deepseek-chat",
        base_url="https://gateway.example.com/v1/",
        custom_providers=entries,
    )

    kwargs = ChatCompletionsTransport().build_kwargs(
        model="deepseek-chat",
        messages=[{"role": "system", "content": "You are stable."}],
        tools=[{"type": "function", "function": {"name": "lookup"}}],
        session_id="session-local-gateway",
        provider_profile=get_provider_profile("custom"),
        supports_prompt_cache_key=capability,
    )

    assert capability is True
    assert kwargs["prompt_cache_key"].startswith("pck_")


def test_custom_provider_capability_stays_scoped_to_matching_model():
    entries = [
        {
            "provider_key": "local-gateway",
            "name": "Local Gateway",
            "base_url": "https://gateway.example.com/v1",
            "models": {"deepseek-chat": {}},
            "supports_prompt_cache_key": True,
        }
    ]

    assert not _custom_provider_supports_prompt_cache_key_for_agent(
        provider="custom:local-gateway",
        model="strict-model",
        base_url="https://gateway.example.com/v1",
        custom_providers=entries,
    )
