"""Failover must be tried before the compression dead-ends give up.

``run_conversation`` has five terminal paths for "payload too large / context
overflow and compression cannot help any further". Every *other* terminal error
class (rate limit, billing, transport, auth) escalates to the fallback chain
before returning -- these five did not, so a configured provider with a larger
context window or request-size limit was never tried for a 413.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent
import agent.conversation_loop as conversation_loop


CHAIN = [
    {
        "provider": "openrouter",
        "model": "google/gemma-4-26b-a4b-it:free",
        "base_url": "https://openrouter.ai/api/v1",
    }
]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Compression retries sleep for rate-limit smoothing; tests assert behaviour."""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)


def _make_agent(fallback_model=None):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
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
    agent.compression_enabled = True
    agent.save_trajectories = False
    return agent


def _413():
    err = Exception("Request entity too large")
    err.status_code = 413
    return err


def _ok(text="Hello"):
    msg = SimpleNamespace(
        content=text, tool_calls=None, reasoning_content=None, reasoning=None
    )
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="stop")], model="fb/model"
    )
    resp.usage = None
    return resp


class TestFailoverBeforeCompressionDeadEnd:
    def test_uncompressible_payload_switches_provider(self):
        """A 413 that cannot be compressed should move to the next provider."""
        agent = _make_agent(fallback_model=CHAIN)
        agent.client.chat.completions.create.side_effect = [_413(), _ok("Hello")]

        def _activate(*_a, **_k):
            agent.provider = "openrouter"
            agent.model = "google/gemma-4-26b-a4b-it:free"
            agent._fallback_index = 1
            return True

        with (
            patch.object(agent, "_compress_context") as compress,
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch.object(
                agent, "_try_activate_fallback", side_effect=_activate
            ) as activate,
        ):
            # Same message count back -> nothing left to compress.
            compress.return_value = (
                [{"role": "user", "content": "hello"}],
                "same prompt",
            )
            result = agent.run_conversation("hello")

        assert activate.called, "fallback chain was never consulted"
        assert result.get("final_response") == "Hello"
        assert result.get("completed") is True
        assert not result.get("compression_exhausted")

    def test_without_a_chain_the_terminal_result_is_unchanged(self):
        """No fallback configured -> the existing partial-failure result stands."""
        agent = _make_agent(fallback_model=None)
        agent.client.chat.completions.create.side_effect = [_413()]

        with (
            patch.object(agent, "_compress_context") as compress,
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            compress.return_value = (
                [{"role": "user", "content": "hello"}],
                "same prompt",
            )
            result = agent.run_conversation("hello")

        assert result.get("completed") is False
        assert result.get("partial") is True
        assert result.get("compression_exhausted") is True
        assert "413" in result.get("error", "")
        # The terminal result still carries a user-facing string.
        assert result.get("final_response")
