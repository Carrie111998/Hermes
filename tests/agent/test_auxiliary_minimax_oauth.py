"""Regression coverage for MiniMax OAuth in auxiliary/MoA calls."""

from unittest.mock import MagicMock, patch


def test_minimax_oauth_builds_anthropic_auxiliary_client_from_runtime_credentials():
    """MoA must use resolved MiniMax OAuth credentials, not require an env key."""
    from agent.auxiliary_client import AnthropicAuxiliaryClient, resolve_provider_client

    fake_client = MagicMock(name="minimax_anthropic_client")
    with patch(
        "agent.anthropic_adapter.build_anthropic_client",
        return_value=fake_client,
    ) as build_client:
        client, model = resolve_provider_client(
            "minimax-oauth",
            model="MiniMax-M3",
            explicit_base_url="https://api.minimax.io/anthropic",
            explicit_api_key="test-oauth-token",
            api_mode="anthropic_messages",
        )

    assert isinstance(client, AnthropicAuxiliaryClient)
    assert model == "MiniMax-M3"
    build_client.assert_called_once_with(
        "test-oauth-token", "https://api.minimax.io/anthropic"
    )
