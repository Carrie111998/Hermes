from unittest.mock import MagicMock

import pytest


def test_minimax_oauth_resolves_anthropic_auxiliary_client(monkeypatch):
    from agent import auxiliary_client

    real_client = MagicMock()
    build_client = MagicMock(return_value=real_client)
    monkeypatch.setattr(
        "agent.anthropic_adapter.build_anthropic_client",
        build_client,
    )

    client, model = auxiliary_client.resolve_provider_client(
        "minimax-oauth",
        "minimax-m3",
        explicit_base_url="https://api.minimax.io/anthropic",
        explicit_api_key="oauth-access-token",
        api_mode="anthropic_messages",
    )

    assert client is not None
    assert isinstance(client, auxiliary_client.AnthropicAuxiliaryClient)
    assert model == "minimax-m3"
    build_client.assert_called_once_with(
        "oauth-access-token",
        "https://api.minimax.io/anthropic",
    )
    # Bearer transport does not imply native Anthropic OAuth.  MiniMax must not
    # receive Claude Code identity prompts, tool transforms, or OAuth betas.
    assert client.chat.completions._is_oauth is False


def test_minimax_oauth_resolver_used_when_explicit_credentials_absent(monkeypatch):
    from agent import auxiliary_client

    monkeypatch.setattr(
        "hermes_cli.auth.resolve_minimax_oauth_runtime_credentials",
        lambda: {
            "provider": "minimax-oauth",
            "api_key": "stored-oauth-token",
            "base_url": "https://api.minimax.io/anthropic",
            "source": "oauth",
        },
    )
    build_client = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(
        "agent.anthropic_adapter.build_anthropic_client",
        build_client,
    )

    client, model = auxiliary_client.resolve_provider_client(
        "minimax-oauth",
        "minimax-m3",
    )

    assert client is not None
    assert model == "minimax-m3"
    build_client.assert_called_once_with(
        "stored-oauth-token",
        "https://api.minimax.io/anthropic",
    )


def test_minimax_oauth_credential_resolution_failure_returns_none(monkeypatch):
    from agent import auxiliary_client

    def _raise():
        raise RuntimeError("not logged in")

    monkeypatch.setattr(
        "hermes_cli.auth.resolve_minimax_oauth_runtime_credentials",
        _raise,
    )

    client, model = auxiliary_client.resolve_provider_client("minimax-oauth")

    assert client is None
    assert model is None


@pytest.mark.parametrize(
    "creds",
    [
        {"provider": "minimax-oauth", "api_key": "", "base_url": "https://api.minimax.io/anthropic"},
        {"provider": "minimax-oauth", "api_key": "token", "base_url": ""},
        {"provider": "minimax-oauth", "api_key": "token"},
    ],
)
def test_minimax_oauth_empty_resolver_credentials_returns_none(monkeypatch, creds):
    from agent import auxiliary_client

    monkeypatch.setattr(
        "hermes_cli.auth.resolve_minimax_oauth_runtime_credentials",
        lambda: creds,
    )

    client, model = auxiliary_client.resolve_provider_client("minimax-oauth")

    assert client is None
    assert model is None


def test_minimax_oauth_partial_explicit_with_unavailable_resolver_returns_none(monkeypatch):
    from agent import auxiliary_client

    def _raise():
        raise RuntimeError("not logged in")

    monkeypatch.setattr(
        "hermes_cli.auth.resolve_minimax_oauth_runtime_credentials",
        _raise,
    )

    client, model = auxiliary_client.resolve_provider_client(
        "minimax-oauth",
        explicit_api_key="oauth-access-token",
        explicit_base_url="",
    )

    assert client is None
    assert model is None
