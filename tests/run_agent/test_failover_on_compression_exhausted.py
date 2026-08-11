"""Failover must be attempted before giving up on an uncompressible payload.

Two defects made a 413 look like a crash instead of a provider switch:

* the compression dead-ends returned straight out of ``run_conversation``,
  bypassing the failover guard that handles every other error class;
* ``chat()`` subscripted ``result["final_response"]``, a key those failure
  results never carry, so the caller saw ``KeyError: final_response``
  instead of the real cause sitting in ``result["error"]``.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent
import run_agent


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(run_agent, "jittered_backoff", lambda *a, **k: 0.0)


def _tool_defs():
    return [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "web_search tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


def _make_agent(fallback_model=None):
    with (
        patch("run_agent.get_tool_definitions", return_value=_tool_defs()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://api.groq.com/openai/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


def _413():
    err = Exception("Request entity too large")
    err.status_code = 413
    return err


def _ok_response(text="Hello"):
    msg = SimpleNamespace(content=text, tool_calls=None, reasoning_content=None, reasoning=None)
    resp = SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], model="fb/model")
    resp.usage = None
    return resp


CHAIN = [
    {
        "provider": "openrouter",
        "model": "google/gemma-4-26b-a4b-it:free",
        "base_url": "https://openrouter.ai/api/v1",
    }
]


class TestFailoverOnCompressionExhausted:

    def test_uncompressible_payload_switches_provider_and_answers(self):
        """A 413 that cannot be compressed should move to the next provider."""
        agent = _make_agent(fallback_model=CHAIN)
        agent.client.chat.completions.create.side_effect = [_413(), _ok_response("Hello")]

        def _activate():
            agent.provider = "openrouter"
            agent.model = "google/gemma-4-26b-a4b-it:free"
            agent._fallback_index = 1
            return True

        with (
            patch.object(agent, "_compress_context") as compress,
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch.object(agent, "_try_activate_fallback", side_effect=_activate) as activate,
        ):
            # Same message count back -> nothing left to compress.
            compress.return_value = ([{"role": "user", "content": "hello"}], "same prompt")
            result = agent.run_conversation("hello")

        assert activate.called, "failover was never attempted"
        assert result.get("final_response") == "Hello"
        assert result["completed"] is True

    def test_gives_up_only_after_the_chain_is_exhausted(self):
        """With no fallback left, the original partial-failure result stands."""
        agent = _make_agent(fallback_model=None)
        agent.client.chat.completions.create.side_effect = [_413()]

        with (
            patch.object(agent, "_compress_context") as compress,
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            compress.return_value = ([{"role": "user", "content": "hello"}], "same prompt")
            result = agent.run_conversation("hello")

        assert result["completed"] is False
        assert result.get("partial") is True
        assert "413" in result["error"]


class TestChatSurfacesTheRealError:

    def test_failure_without_final_response_reports_the_cause(self):
        agent = _make_agent()
        failure = {
            "messages": [],
            "completed": False,
            "partial": True,
            "failed": True,
            "compression_exhausted": True,
            "error": "Request payload too large (413). Cannot compress further.",
        }
        with patch.object(agent, "run_conversation", return_value=failure):
            with pytest.raises(RuntimeError, match="Cannot compress further"):
                agent.chat("hello")

    def test_successful_result_is_returned_unchanged(self):
        agent = _make_agent()
        with patch.object(agent, "run_conversation", return_value={"final_response": "Hi"}):
            assert agent.chat("hello") == "Hi"
