"""Provider-profile transport resolution regression tests."""
from __future__ import annotations

from types import SimpleNamespace


def test_determine_api_mode_uses_user_provider_profile(monkeypatch):
    """Out-of-tree provider profiles must control their own wire protocol."""
    import providers
    from hermes_cli.providers import determine_api_mode

    monkeypatch.setattr(
        providers,
        "get_provider_profile",
        lambda name: SimpleNamespace(api_mode="anthropic_messages")
        if name == "third-party-anthropic"
        else None,
    )

    assert determine_api_mode("third-party-anthropic") == "anthropic_messages"


def test_agent_init_uses_user_provider_profile_defaults(monkeypatch):
    """Provider-only construction must resolve endpoint before client selection."""
    import providers
    from run_agent import AIAgent

    profile = SimpleNamespace(
        base_url="https://provider.example/anthropic",
        api_mode="anthropic_messages",
    )
    monkeypatch.setattr(
        providers,
        "get_provider_profile",
        lambda name: profile if name == "third-party-anthropic" else None,
    )

    agent = AIAgent(
        provider="third-party-anthropic",
        model="example-model",
        api_key="test-token-not-a-secret",
        quiet_mode=True,
        enabled_toolsets=[],
        skip_context_files=True,
        skip_memory=True,
    )

    assert agent.base_url == profile.base_url
    assert getattr(agent, "api_mode") == profile.api_mode
    assert getattr(agent, "_anthropic_client") is not None
    assert getattr(agent, "client") is None


def test_explicit_host_keeps_mandatory_transport_precedence(monkeypatch):
    """Profile defaults must not override a caller's protocol-mandated host."""
    import providers
    from run_agent import AIAgent

    profile = SimpleNamespace(
        base_url="https://provider.example/anthropic",
        api_mode="anthropic_messages",
    )
    monkeypatch.setattr(
        providers,
        "get_provider_profile",
        lambda name: profile if name == "third-party-anthropic" else None,
    )

    agent = AIAgent(
        provider="third-party-anthropic",
        base_url="https://api.openai.com/v1",
        model="example-model",
        api_key="test-token-not-a-secret",
        quiet_mode=True,
        enabled_toolsets=[],
        skip_context_files=True,
        skip_memory=True,
    )

    assert agent.base_url == "https://api.openai.com/v1"
    assert getattr(agent, "api_mode") == "codex_responses"
