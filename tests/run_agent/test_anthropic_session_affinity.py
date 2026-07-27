"""Conversation affinity for Anthropic-compatible proxy clients."""

import hashlib
from unittest.mock import patch

from run_agent import AIAgent


def test_agent_reuses_one_affinity_across_anthropic_client_rebuilds():
    with (
        patch("agent.anthropic_adapter._anthropic_sdk") as mock_sdk,
        patch(
            "hermes_cli.config.get_custom_provider_session_affinity",
            return_value=True,
        ),
    ):
        agent = AIAgent(
            api_key="proxy-key",
            base_url="https://proxy.example.com/anthropic",
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

    expected = hashlib.sha256(
        agent.session_id.encode("utf-8"),
    ).hexdigest()
    assert initial_headers["x-session-affinity"] == expected
    assert request_headers["x-session-affinity"] == expected
    assert rebuilt_headers["x-session-affinity"] == expected
