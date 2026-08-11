"""Regression test: oneshot mode must hand the fallback chain to AIAgent.

AIAgent derives ``_fallback_chain`` exclusively from its ``fallback_model``
argument.  ``_run_agent`` used to omit it, so ``hermes -z`` (and every daemon
answering through it) ran with an empty chain and could never fail over, no
matter what ``fallback_providers`` held in the config.
"""

from unittest.mock import MagicMock, patch

import pytest


CHAIN = [
    {
        "provider": "openrouter",
        "model": "google/gemma-4-26b-a4b-it:free",
        "base_url": "https://openrouter.ai/api/v1",
    }
]


def _run_and_capture(config):
    """Invoke _run_agent with all heavy collaborators stubbed out.

    Returns the kwargs AIAgent was constructed with.
    """
    from hermes_cli import oneshot

    fake_agent = MagicMock()
    fake_agent.chat.return_value = "ok"
    agent_cls = MagicMock(return_value=fake_agent)

    with (
        patch("hermes_cli.config.load_config", return_value=config),
        patch("run_agent.AIAgent", agent_cls),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value={
                "api_key": "test-key-1234567890",
                "base_url": "https://api.groq.com/openai/v1",
                "provider": "groq",
                "api_mode": None,
                "credential_pool": None,
            },
        ),
        patch("hermes_cli.tools_config._get_platform_tools", return_value=set()),
        patch.object(oneshot, "_create_session_db_for_oneshot", return_value=None),
    ):
        oneshot._run_agent("hello", model="llama-3.3-70b-versatile")

    assert agent_cls.call_count == 1
    return agent_cls.call_args.kwargs


def test_fallback_providers_reach_the_agent():
    kwargs = _run_and_capture(
        {
            "model": {"default": "llama-3.3-70b-versatile", "provider": "groq"},
            "fallback_providers": CHAIN,
        }
    )
    assert kwargs.get("fallback_model") == CHAIN


def test_legacy_fallback_model_key_still_honoured():
    legacy = {"provider": "openrouter", "model": "google/gemma-4-26b-a4b-it:free"}
    kwargs = _run_and_capture(
        {
            "model": {"default": "llama-3.3-70b-versatile", "provider": "groq"},
            "fallback_model": legacy,
        }
    )
    assert kwargs.get("fallback_model") == legacy


def test_absent_config_yields_none_not_missing_kwarg():
    kwargs = _run_and_capture(
        {"model": {"default": "llama-3.3-70b-versatile", "provider": "groq"}}
    )
    assert kwargs.get("fallback_model", "MISSING") is None
