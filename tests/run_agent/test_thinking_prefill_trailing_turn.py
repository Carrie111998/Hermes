"""Regression test for the thinking-only prefill reaching the wire.

A thinking-only response (reasoning tokens, no visible text) makes the loop
append an internal assistant turn plus a user-role continuation so the model
finishes with visible output. On providers that don't echo reasoning back, the API copy has its
reasoning fields stripped before ``_drop_thinking_only_and_merge_users`` runs,
so the drop pass used to see a bare ``{"role": "assistant", "content": ""}``
and let it through. Gemini rejects that with

    400 INVALID_ARGUMENT: Requests ending with a model turn are not supported.

classified as non-retryable, so the turn aborts outright.

Unlike the unit tests in ``test_thinking_only_sanitizer.py``, this drives
``run_conversation`` and asserts on the payload actually handed to the client,
so it exercises the API-copy build that decides whether ``_thinking_prefill``
survives as far as the drop pass.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def loop_agent():
    """AIAgent with a mocked OpenAI client, mirroring the fixture in
    ``test_dropped_tool_call_recovery.py``."""
    from run_agent import AIAgent
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.client = MagicMock()
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.tool_delay = 0
        agent.compression_enabled = False
        agent.save_trajectories = False
        return agent


def _thinking_only_response():
    """Reasoning tokens, no visible text — what triggers the prefill retry."""
    from tests.run_agent.test_run_agent import _mock_response
    return _mock_response(
        content="",
        finish_reason="stop",
        reasoning="Let me work through the request step by step.",
    )


def _final_response(text="Here is the answer."):
    """An ordinary text turn that ends the loop."""
    from tests.run_agent.test_run_agent import _mock_response
    return _mock_response(content=text, finish_reason="stop")


def _sent_messages(create_mock, call_index):
    call = create_mock.call_args_list[call_index]
    return call.kwargs.get("messages") or call.args[0].get("messages")


class TestThinkingPrefillTrailingTurn:

    def test_request_after_prefills_does_not_end_on_assistant(self, loop_agent):
        # Two thinking-only responses queue two prefill stubs, then the model
        # finally produces text. The third request is the one that used to go
        # out ending on a model turn.
        loop_agent.client.chat.completions.create.side_effect = [
            _thinking_only_response(),
            _thinking_only_response(),
            _final_response(),
        ]

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            loop_agent.run_conversation("do the thing")

        create = loop_agent.client.chat.completions.create
        assert create.call_count >= 3, (
            "Two thinking-only responses should each trigger a prefill retry."
        )

        final_request = _sent_messages(create, 2)
        assert final_request[-1]["role"] != "assistant", (
            "Request ends on a model turn, which Gemini rejects with a "
            "non-retryable 400. The thinking-only prefill stubs must be "
            f"dropped before send. Got roles: {[m['role'] for m in final_request]}"
        )

    def test_prefill_retry_explicitly_requests_visible_output(self, loop_agent):
        """Dropping the thinking stub must not make the retry byte-identical."""
        requests = []

        def respond(**kwargs):
            requests.append(kwargs["messages"])
            if "Produce your final answer as plain text now" in str(
                kwargs["messages"][-1].get("content", "")
            ):
                return _final_response()
            return _thinking_only_response()

        loop_agent.client.chat.completions.create.side_effect = respond

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation("do the thing")

        assert loop_agent.client.chat.completions.create.call_count == 2
        assert requests[0] != requests[1]
        sent = requests[1]
        empty_assistants = [
            m for m in sent
            if m.get("role") == "assistant" and not (m.get("content") or "").strip()
        ]
        assert not empty_assistants, (
            f"Empty assistant stub(s) reached the wire: {empty_assistants}"
        )
        assert "Produce your final answer as plain text now" in sent[-1]["content"]
        assert result["final_response"] == "Here is the answer."
        assert all(
            "Produce your final answer as plain text now"
            not in str(message.get("content", ""))
            for message in result["messages"]
        )

    def test_internal_marker_never_reaches_the_wire(self, loop_agent):
        """``_thinking_prefill`` survives the API-copy build on purpose, but the
        transport must still keep it off the wire."""
        loop_agent.client.chat.completions.create.side_effect = [
            _thinking_only_response(),
            _final_response(),
        ]

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            loop_agent.run_conversation("do the thing")

        sent = _sent_messages(loop_agent.client.chat.completions.create, 1)
        leaked = [m for m in sent if any(str(k).startswith("_") for k in m)]
        assert not leaked, f"Internal scaffolding keys reached the wire: {leaked}"
