from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def test_static_anthropic_api_key_is_not_replaced_by_oauth_autodiscovery():
    """A selected API-key route must stay on x-api-key authentication.

    Claude Code credentials may coexist on the machine.  The per-request OAuth
    refresh hook must not replace an explicit ``sk-ant-api`` credential with an
    auto-discovered subscription token.
    """
    agent = AIAgent.__new__(AIAgent)
    setattr(agent, "api_mode", "anthropic_messages")
    setattr(agent, "provider", "anthropic")
    setattr(agent, "model", "claude-opus-4-7")
    agent._anthropic_api_key = "sk-ant-api-test"
    agent._anthropic_base_url = "https://api.anthropic.com"
    agent._anthropic_client = MagicMock()
    agent._is_anthropic_oauth = False

    with patch(
        "agent.anthropic_adapter.resolve_anthropic_token",
        return_value="sk-ant-oat-autodiscovered",
    ) as resolve:
        refreshed = agent._try_refresh_anthropic_client_credentials()

    assert refreshed is False
    resolve.assert_not_called()
    agent._anthropic_client.close.assert_not_called()
    assert agent._anthropic_api_key == "sk-ant-api-test"
    assert agent._is_anthropic_oauth is False
