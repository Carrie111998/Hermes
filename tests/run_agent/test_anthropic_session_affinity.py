"""Conversation affinity for Anthropic-compatible proxy clients."""

import hashlib
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def test_agent_reuses_one_affinity_across_anthropic_client_rebuilds():
    proxy_url = "https://proxy.example.com/anthropic"
    with (
        patch("agent.anthropic_adapter._anthropic_sdk") as mock_sdk,
        patch(
            "hermes_cli.config.get_custom_provider_session_affinity",
            return_value=True,
        ),
        patch("agent.agent_runtime_helpers.time.sleep"),
    ):
        agent = AIAgent(
            api_key="proxy-key",
            base_url=proxy_url,
            provider="custom-proxy",
            api_mode="anthropic_messages",
            model="claude-test",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        initial_headers = mock_sdk.Anthropic.call_args.kwargs[
            "default_headers"
        ]

        agent._create_request_anthropic_client(reason="test")
        request_headers = mock_sdk.Anthropic.call_args.kwargs[
            "default_headers"
        ]

        agent._rebuild_anthropic_client()
        rebuilt_headers = mock_sdk.Anthropic.call_args.kwargs[
            "default_headers"
        ]

        entry = MagicMock()
        entry.runtime_api_key = "rotated-key"
        entry.runtime_base_url = proxy_url
        entry.provider = "custom-proxy"
        agent._swap_credential(entry)
        credential_headers = mock_sdk.Anthropic.call_args.kwargs[
            "default_headers"
        ]

        class ReadTimeout(Exception):
            pass

        agent._fallback_activated = False
        assert agent._try_recover_primary_transport(
            ReadTimeout("stale"),
            retry_count=1,
            max_retries=1,
        ) is True
        recovery_headers = mock_sdk.Anthropic.call_args.kwargs[
            "default_headers"
        ]

        agent._fallback_activated = True
        agent.provider = "openrouter"
        agent.base_url = "https://openrouter.ai/api/v1"
        agent.api_mode = "chat_completions"
        assert agent._restore_primary_runtime() is True
        restore_headers = mock_sdk.Anthropic.call_args.kwargs[
            "default_headers"
        ]

        agent.context_compressor = None
        with patch("agent.credential_pool.load_pool", return_value=None):
            agent.switch_model(
                "claude-next",
                "custom-proxy",
                api_key="switch-key",
                base_url=proxy_url,
                api_mode="anthropic_messages",
            )
        switch_headers = mock_sdk.Anthropic.call_args.kwargs[
            "default_headers"
        ]

        agent.provider = "openrouter"
        agent.requested_provider = "openrouter"
        agent.model = "openai/gpt-4o"
        agent.base_url = "https://openrouter.ai/api/v1"
        agent.api_mode = "chat_completions"
        agent._fallback_chain = [
            {
                "provider": "custom-proxy",
                "model": "claude-fallback",
                "base_url": proxy_url,
                "api_key": "fallback-key",
            },
        ]
        agent._fallback_index = 0
        agent._fallback_activated = False
        fallback_client = MagicMock()
        fallback_client.api_key = "fallback-key"
        fallback_client.base_url = proxy_url
        with (
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(fallback_client, "claude-fallback"),
            ),
            patch("agent.credential_pool.load_pool", return_value=None),
        ):
            assert agent._try_activate_fallback() is True
        fallback_headers = mock_sdk.Anthropic.call_args.kwargs[
            "default_headers"
        ]

    expected = hashlib.sha256(
        agent.session_id.encode("utf-8"),
    ).hexdigest()
    for headers in (
        initial_headers,
        request_headers,
        rebuilt_headers,
        credential_headers,
        recovery_headers,
        restore_headers,
        switch_headers,
        fallback_headers,
    ):
        assert headers["x-session-affinity"] == expected
