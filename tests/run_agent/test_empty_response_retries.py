"""Tests for configurable empty-response retries and jittered backoff.

See: NousResearch/hermes-agent#74916
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pytest

from run_agent import AIAgent


def _mock_response(content=None, finish_reason="stop"):
    msg = SimpleNamespace(
        content=content,
        tool_calls=None,
        reasoning=None,
        reasoning_content=None,
        reasoning_details=None,
    )
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test-model", usage=None)


def _make_agent(empty_response_retries=None):
    """Helper to create an AIAgent with a specific agent.empty_response_retries."""
    with patch("hermes_cli.config.load_config") as mock_load:
        cfg = {"agent": {}}
        if empty_response_retries is not None:
            cfg["agent"]["empty_response_retries"] = empty_response_retries
        mock_load.return_value = cfg
        agent = AIAgent(
            model="test-model",
            provider="custom",
            api_key="test-key",
            base_url="http://127.0.0.1:1234/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    return agent


class TestEmptyResponseRetriesConfig:
    """Test agent.empty_response_retries configuration surface."""

    def test_default_empty_response_retries_is_five(self):
        agent = _make_agent()
        assert agent._empty_response_retries == 5

    def test_empty_response_retries_honors_config_override(self):
        agent = _make_agent(empty_response_retries=3)
        assert agent._empty_response_retries == 3

        agent_zero = _make_agent(empty_response_retries=0)
        assert agent_zero._empty_response_retries == 0

    def test_empty_response_retries_handles_invalid_value(self):
        agent = _make_agent(empty_response_retries="invalid")
        assert agent._empty_response_retries == 5

    def test_empty_response_retries_zero_skips_retries(self):
        agent = _make_agent(empty_response_retries=0)
        empty_resp = _mock_response(content=None)

        agent.client = MagicMock()
        agent.client.chat.completions.create.return_value = empty_resp

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("time.sleep"),
        ):
            result = agent.run_conversation("test prompt")

        assert result["completed"] is True
        assert result["final_response"] == "(empty)"
        # 0 retries means 1 single API call attempt, no retries
        assert result["api_calls"] == 1

    def test_empty_response_retries_uses_jittered_backoff(self):
        agent = _make_agent(empty_response_retries=2)
        empty_resp = _mock_response(content=None)

        agent.client = MagicMock()
        agent.client.chat.completions.create.return_value = empty_resp

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("agent.conversation_loop.jittered_backoff", return_value=0.0) as mock_backoff,
            patch("time.sleep"),
        ):
            result = agent.run_conversation("test prompt")

        assert result["completed"] is True
        assert result["final_response"] == "(empty)"
        assert result["api_calls"] == 3  # 1 original + 2 retries
        assert mock_backoff.call_count == 2
        mock_backoff.assert_called_with(2, base_delay=3.0, max_delay=15.0)

    def test_empty_response_retries_preserves_redirect_during_backoff(self):
        agent = _make_agent(empty_response_retries=3)
        empty_resp = _mock_response(content=None)
        good_resp = _mock_response(content="Recovered after steer!")

        agent.client = MagicMock()
        agent.client.chat.completions.create.side_effect = [empty_resp, good_resp]

        # Simulate user injecting a steer redirect during backoff sleep
        def mock_sleep(seconds):
            agent.steer("User correction during retry backoff")
            agent._interrupt_requested = True

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("agent.conversation_loop.jittered_backoff", return_value=0.0),
            patch("time.sleep", side_effect=mock_sleep),
        ):
            result = agent.run_conversation("test prompt")

        assert result["completed"] is True
        assert result["final_response"] == "Recovered after steer!"
