"""Behavioral coverage for custom-provider ``extra_headers`` route identity."""

from types import MethodType, SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _shared_provider_config():
    return {
        "providers": {
            "first": {
                "name": "First",
                "base_url": "https://shared.example.com/v1",
                "extra_headers": {"X-Route": "first"},
            },
            "selected": {
                "name": "Selected",
                "base_url": "https://shared.example.com/v1",
                "extra_headers": {"X-Route": "selected"},
            },
        }
    }


def test_client_rebuild_selects_headers_for_active_provider():
    agent = SimpleNamespace(
        api_mode="chat_completions",
        provider="custom:selected",
        _client_kwargs={},
    )
    agent._apply_user_default_headers = MagicMock()

    with patch(
        "hermes_cli.config.load_config",
        return_value=_shared_provider_config(),
    ):
        AIAgent._apply_client_headers_for_base_url(
            agent,
            "https://shared.example.com/v1",
            apply_user_headers=False,
        )

    assert agent._client_kwargs["default_headers"] == {"X-Route": "selected"}


def test_credential_rotation_keeps_active_provider_identity():
    agent = SimpleNamespace(
        api_mode="chat_completions",
        provider="custom:selected",
        model="shared-model",
        api_key="old",
        base_url="https://shared.example.com/v1",
        _client_kwargs={
            "api_key": "old",
            "base_url": "https://shared.example.com/v1",
        },
        _replace_primary_openai_client=MagicMock(),
    )
    agent._apply_client_headers_for_base_url = MethodType(
        AIAgent._apply_client_headers_for_base_url,
        agent,
    )
    agent._apply_user_default_headers = MagicMock()
    entry = SimpleNamespace(
        runtime_api_key="new",
        access_token="",
        runtime_base_url="https://shared.example.com/v1",
        base_url="https://shared.example.com/v1",
    )
    config = _shared_provider_config()

    with (
        patch("hermes_cli.config.load_config_readonly", return_value=config),
        patch("hermes_cli.config.load_config", return_value=config),
    ):
        AIAgent._swap_credential(agent, entry)

    assert agent._client_kwargs["default_headers"] == {"X-Route": "selected"}
