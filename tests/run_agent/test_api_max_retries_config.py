"""Tests for the AIAgent and config api_max_retries surfaces.

Closes #11616 — make the hardcoded ``max_retries = 3`` in the agent's API
retry loop user-configurable so fallback-provider setups can fail over
faster on flaky primaries instead of burning ~3x180s on the same stall.
"""
from copy import deepcopy
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


_UNSET = object()


def _make_agent(*, config_api_max_retries=_UNSET, api_max_retries=_UNSET, cfg=None):
    """Build an isolated AIAgent without provider or filesystem access."""
    cfg = cfg if cfg is not None else {"agent": {}}
    if config_api_max_retries is not _UNSET:
        cfg["agent"]["api_max_retries"] = config_api_max_retries

    kwargs = {}
    if api_max_retries is not _UNSET:
        kwargs["api_max_retries"] = api_max_retries

    with patch("run_agent.OpenAI"), \
         patch("hermes_cli.config.load_config", return_value=cfg), \
         patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        return AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            **kwargs,
        )


def _run_retryable_failure(api_max_retries):
    agent = _make_agent(api_max_retries=api_max_retries)
    agent.client = MagicMock()
    error = Exception("upstream unavailable")
    error.status_code = 500
    agent.client.chat.completions.create.side_effect = error
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False

    with patch.object(agent, "_persist_session"), \
         patch.object(agent, "_save_trajectory"), \
         patch.object(agent, "_cleanup_task_resources"), \
         patch.object(agent, "_try_recover_primary_transport", return_value=False), \
         patch("agent.conversation_loop.jittered_backoff", return_value=0.0):
        result = agent.run_conversation("hello")

    assert result["failed"] is True
    return agent.client.chat.completions.create.call_count


def test_default_api_max_retries_is_three():
    """No config override → legacy default of 3 retries preserved."""
    agent = _make_agent()
    assert agent._api_max_retries == 3


def test_api_max_retries_honors_config_override():
    """Setting agent.api_max_retries in config propagates to the agent."""
    agent = _make_agent(config_api_max_retries=1)
    assert agent._api_max_retries == 1

    agent2 = _make_agent(config_api_max_retries=5)
    assert agent2._api_max_retries == 5


def test_explicit_none_preserves_config_override():
    agent = _make_agent(config_api_max_retries=4, api_max_retries=None)
    assert agent._api_max_retries == 4


def test_explicit_api_max_retries_overrides_config_without_mutation():
    cfg = {"agent": {"api_max_retries": 5}}
    original = deepcopy(cfg)

    agent = _make_agent(api_max_retries=2, cfg=cfg)

    assert agent._api_max_retries == 2
    assert cfg == original


def test_public_constructor_forwards_api_max_retries_unchanged():
    with patch("agent.agent_init.init_agent") as init_agent:
        AIAgent(api_max_retries=2)

    assert init_agent.call_args.kwargs["api_max_retries"] == 2


@pytest.mark.parametrize(
    ("value", "error_type"),
    [
        (True, TypeError),
        (False, TypeError),
        (1.5, TypeError),
        ("2", TypeError),
        (0, ValueError),
        (-1, ValueError),
    ],
)
def test_invalid_explicit_api_max_retries_fails_before_provider_construction(
    value, error_type
):
    with patch("run_agent.OpenAI") as openai:
        with pytest.raises(error_type, match="api_max_retries"):
            AIAgent(api_max_retries=value)

    openai.assert_not_called()


def test_explicit_one_is_one_total_provider_attempt():
    assert _run_retryable_failure(1) == 1


def test_explicit_two_is_two_total_provider_attempts():
    assert _run_retryable_failure(2) == 2


def test_non_retryable_failure_remains_single_attempt():
    agent = _make_agent(api_max_retries=2)
    agent.client = MagicMock()
    error = Exception("invalid request")
    error.status_code = 400
    agent.client.chat.completions.create.side_effect = error
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False

    with patch.object(agent, "_persist_session"), \
         patch.object(agent, "_save_trajectory"), \
         patch.object(agent, "_cleanup_task_resources"):
        result = agent.run_conversation("hello")

    assert result["failed"] is True
    assert agent.client.chat.completions.create.call_count == 1


def test_retry_override_is_isolated_between_instances_and_non_persistent():
    cfg = {"agent": {"api_max_retries": 7}}
    original = deepcopy(cfg)

    agent_one = _make_agent(api_max_retries=1, cfg=cfg)
    agent_two = _make_agent(api_max_retries=2, cfg=cfg)

    assert agent_one._api_max_retries == 1
    assert agent_two._api_max_retries == 2
    assert cfg == original


