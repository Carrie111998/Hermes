"""Regression coverage for provider-specific init failures (#94785)."""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from agent.credential_pool import CredentialPool, PooledCredential


def test_exhausted_openrouter_pool_surfaces_billing_error_without_raw_details() -> None:
    from agent.agent_init import init_agent
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent._base_url = ""
    agent._base_url_lower = ""
    agent._base_url_hostname = ""
    pool = CredentialPool(
        "openrouter",
        [
            PooledCredential(
                provider="openrouter",
                id="or-billing",
                label="desktop override",
                auth_type="api_key",
                priority=0,
                source="manual",
                access_token="sk-secret-must-not-leak",
                last_status="exhausted",
                last_status_at=time.time(),
                last_error_code=402,
                last_error_reason="billing",
                last_error_message="account owner secret@example.com can only afford 4282 tokens",
            )
        ],
    )

    with (
        patch("agent.auxiliary_client.resolve_provider_client", return_value=(None, None)),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("hermes_cli.config.load_config", return_value={}),
        patch("hermes_cli.config.get_compatible_custom_providers", return_value=[]),
        patch("agent.iteration_budget.IterationBudget"),
        patch("hermes_cli.config.cfg_get", return_value=None),
        pytest.raises(RuntimeError) as exc_info,
    ):
        init_agent(
            agent,
            provider="openrouter",
            requested_provider="openrouter",
            model="z-ai/glm-5.3",
            credential_pool=pool,
            skip_context_files=True,
            skip_memory=True,
            quiet_mode=True,
        )

    message = str(exc_info.value)
    assert "openrouter" in message.lower()
    assert "billing" in message.lower() or "credit" in message.lower()
    assert "402" in message
    assert "No LLM provider configured" not in message
    assert "sk-secret-must-not-leak" not in message
    assert "secret@example.com" not in message
