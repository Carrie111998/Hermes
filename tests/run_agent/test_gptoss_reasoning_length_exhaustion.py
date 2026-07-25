"""Regression tests: GPT-OSS (Groq) out-of-band reasoning + finish_reason=length.

Bug: when a model exhausts its output-token budget entirely on hidden
reasoning before producing any visible text, conversation_loop's
finish_reason=="length" branch tries to detect "thinking budget exhausted"
via ``_has_think_tags`` — a regex over ``content`` looking for inline
``<think>`` tags. GPT-OSS on Groq (and DeepSeek/Moonshot-style providers)
signal reasoning out-of-band instead, via the transport's separate
``reasoning`` / ``reasoning_content`` field (see
``agent.transports.types.NormalizedResponse``) while ``content`` stays an
empty string. ``_has_think_tags`` was blind to that field, so
``_thinking_exhausted`` never fired, the response fell through to the
generic length-continuation retry path, and since ``content`` never
accumulated anything across retries, the turn silently returned
``final_response: None`` after exhausting continuation attempts — the
model's real (non-empty) API response became a blank message to the user.

The fix broadens the detection to ``_has_reasoning_signal`` (inline tags OR
non-empty ``reasoning``/``reasoning_content``) and, before giving up,
attempts the next fallback provider — never surfacing empty as success.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


# ── Local fixtures (mirror tests/run_agent/test_run_agent.py) ──────────────


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


def _mock_assistant_msg(
    content="Hello",
    tool_calls=None,
    reasoning=None,
    reasoning_content=None,
    reasoning_details=None,
):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    if reasoning is not None:
        msg.reasoning = reasoning
    if reasoning_content is not None:
        msg.reasoning_content = reasoning_content
    if reasoning_details is not None:
        msg.reasoning_details = reasoning_details
    return msg


def _mock_tool_call(name="web_search", arguments="{}", call_id=None):
    return SimpleNamespace(
        id=call_id or "call_test",
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _mock_response(
    content="Hello",
    finish_reason="stop",
    tool_calls=None,
    reasoning=None,
    reasoning_content=None,
    reasoning_details=None,
):
    msg = _mock_assistant_msg(
        content=content,
        tool_calls=tool_calls,
        reasoning=reasoning,
        reasoning_content=reasoning_content,
        reasoning_details=reasoning_details,
    )
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    resp = SimpleNamespace(choices=[choice], model="test/model", usage=None)
    return resp


@pytest.fixture()
def agent():
    """Minimal AIAgent with mocked OpenAI client and tool loading."""
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        return a


class TestGptOssReasoningLengthExhaustion:
    """Tests for run_conversation() with finish_reason='length' responses
    that carry out-of-band reasoning instead of inline <think> tags."""

    def _setup_agent(self, agent):
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.tool_delay = 0
        agent.compression_enabled = False
        agent.save_trajectories = False

    # ── Baseline: normal successful text responses (no regression) ──────

    def test_gpt_oss_20b_normal_text_response(self, agent):
        """Sanity: a normal (non-truncated) GPT-OSS 20B response is
        unaffected by the length-branch changes."""
        self._setup_agent(agent)
        agent.model = "openai/gpt-oss-20b"
        resp = _mock_response(
            content="The answer is 42.",
            finish_reason="stop",
            reasoning="short reasoning trace",
        )
        agent.client.chat.completions.create.return_value = resp
        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("what is the answer?")
        assert result["completed"] is True
        assert result["final_response"] == "The answer is 42."

    def test_gpt_oss_120b_normal_text_response(self, agent):
        """Same contract holds for the 120B variant."""
        self._setup_agent(agent)
        agent.model = "openai/gpt-oss-120b"
        resp = _mock_response(
            content="Paris is the capital of France.",
            finish_reason="stop",
            reasoning="short reasoning trace",
        )
        agent.client.chat.completions.create.return_value = resp
        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("what is the capital of France?")
        assert result["completed"] is True
        assert result["final_response"] == "Paris is the capital of France."

    # ── Tool calls take priority over thinking-exhaustion detection ─────

    def test_tool_call_response_not_treated_as_length_exhaustion(self, agent):
        """finish_reason='tool_calls' never enters the length branch at all —
        tool execution proceeds normally."""
        self._setup_agent(agent)
        agent.model = "openai/gpt-oss-20b"
        tc = _mock_tool_call(name="web_search", arguments="{}", call_id="c1")
        tool_resp = _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[tc],
            reasoning="deciding to call the search tool",
        )
        final_resp = _mock_response(content="Here are the results.", finish_reason="stop")
        agent.client.chat.completions.create.side_effect = [tool_resp, final_resp]
        with (
            patch("run_agent.handle_function_call", return_value="search result"),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("search something")
        assert result["completed"] is True
        assert result["final_response"] == "Here are the results."

    def test_length_truncation_mid_tool_call_not_treated_as_thinking_exhausted(self, agent):
        """finish_reason='length' WITH tool_calls present must not be
        misclassified as thinking-exhaustion (tool_calls always wins)."""
        self._setup_agent(agent)
        agent.model = "openai/gpt-oss-20b"
        truncated = _mock_response(
            content="",
            finish_reason="length",
            tool_calls=[_mock_tool_call(name="web_search", arguments='{"q": "pa')],
            reasoning="deciding to call the search tool",
        )
        agent.client.chat.completions.create.return_value = truncated
        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("search something")
        # Must NOT be the "Thinking Budget Exhausted" terminal message —
        # tool-call truncation handling (existing machinery) takes it instead.
        assert "Thinking Budget Exhausted" not in (result.get("final_response") or "")

    # ── Truly empty (no signal at all) is unaffected ─────────────────────

    def test_truly_empty_length_response_not_misclassified(self, agent):
        """No content, no reasoning, no tool_calls, finish_reason=length:
        _has_reasoning_signal must stay False — this is a normal truncation,
        not thinking-exhaustion, and keeps using the existing
        continuation-retry path."""
        self._setup_agent(agent)
        agent.model = "some-other-model"
        empty_len = _mock_response(content="", finish_reason="length")
        follow_up = _mock_response(content="Here is the real answer.", finish_reason="stop")
        agent.client.chat.completions.create.side_effect = [empty_len, follow_up]
        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("hello")
        assert "Thinking Budget Exhausted" not in (result.get("final_response") or "")

    # ── The actual bug: out-of-band reasoning + finish_reason=length ────

    def test_gpt_oss_reasoning_field_with_length_triggers_exhaustion_message(self, agent):
        """Core regression (#gptoss-empty): GPT-OSS shape — content='',
        reasoning populated via the out-of-band `reasoning` field,
        finish_reason='length'. No fallback chain configured, so the turn
        must surface the user-facing 'Thinking Budget Exhausted' message —
        never a blank/None final_response."""
        self._setup_agent(agent)
        agent.model = "openai/gpt-oss-20b"
        agent._fallback_chain = []
        exhausted = _mock_response(
            content="",
            finish_reason="length",
            reasoning="Let me think step by step about this problem in detail...",
        )
        agent.client.chat.completions.create.return_value = exhausted
        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("solve this hard problem")
        assert result["final_response"], "final_response must never be empty/None"
        assert result["final_response"] != "(empty)"
        assert "Thinking Budget Exhausted" in result["final_response"]

    def test_deepseek_style_reasoning_content_with_length_triggers_exhaustion_message(self, agent):
        """Same bug, DeepSeek/Moonshot-style field name (`reasoning_content`
        instead of `reasoning`) — both must be detected."""
        self._setup_agent(agent)
        agent.model = "deepseek-reasoner"
        agent._fallback_chain = []
        exhausted = _mock_response(
            content="",
            finish_reason="length",
            reasoning_content="Thinking through the steps required...",
        )
        agent.client.chat.completions.create.return_value = exhausted
        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("solve this hard problem")
        assert result["final_response"]
        assert result["final_response"] != "(empty)"
        assert "Thinking Budget Exhausted" in result["final_response"]

    # ── Fallback is attempted before giving up ──────────────────────────

    def test_reasoning_exhaustion_triggers_fallback_provider(self, agent):
        """With a fallback chain configured, the loop must try the next
        provider before surfacing the exhaustion message — and use its
        real answer when it succeeds."""
        self._setup_agent(agent)
        agent.model = "openai/gpt-oss-20b"
        agent._fallback_chain = [{"provider": "openrouter", "model": "anthropic/claude-sonnet-4"}]
        agent._fallback_index = 0
        agent._fallback_activated = False

        exhausted = _mock_response(
            content="",
            finish_reason="length",
            reasoning="Extensive internal deliberation consuming the whole budget...",
        )
        fallback_answer = _mock_response(content="Fallback provider's answer.", finish_reason="stop")
        agent.client.chat.completions.create.side_effect = [exhausted, fallback_answer]

        fallback_called = {"called": False}

        def _mock_fallback():
            fallback_called["called"] = True
            agent._fallback_index = 1
            agent._fallback_activated = True
            agent.model = "anthropic/claude-sonnet-4"
            agent.provider = "openrouter"
            return True

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch.object(agent, "_try_activate_fallback", side_effect=_mock_fallback),
        ):
            result = agent.run_conversation("solve this hard problem")

        assert fallback_called["called"], "Fallback must be attempted before giving up"
        assert result["final_response"] == "Fallback provider's answer."

    def test_reasoning_exhaustion_fallback_also_exhausted_returns_message_not_empty(self, agent):
        """If the fallback provider ALSO exhausts its budget on reasoning
        (and no further fallback is available), the terminal message must
        still be surfaced — never a blank response."""
        self._setup_agent(agent)
        agent.model = "openai/gpt-oss-20b"
        agent._fallback_chain = [{"provider": "openrouter", "model": "openai/gpt-oss-120b"}]
        agent._fallback_index = 0
        agent._fallback_activated = False

        exhausted = _mock_response(
            content="",
            finish_reason="length",
            reasoning="First provider's exhausted reasoning...",
        )
        fallback_exhausted = _mock_response(
            content="",
            finish_reason="length",
            reasoning="Fallback provider's exhausted reasoning too...",
        )
        agent.client.chat.completions.create.side_effect = [exhausted, fallback_exhausted]

        def _mock_fallback():
            if agent._fallback_index >= len(agent._fallback_chain):
                return False
            agent._fallback_index += 1
            agent._fallback_activated = True
            agent.model = "openai/gpt-oss-120b"
            agent.provider = "openrouter"
            return True

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch.object(agent, "_try_activate_fallback", side_effect=_mock_fallback),
        ):
            result = agent.run_conversation("solve this hard problem")

        assert result["final_response"]
        assert result["final_response"] != "(empty)"
        assert "Thinking Budget Exhausted" in result["final_response"]

    # ── Provider error handling is untouched ────────────────────────────

    def test_provider_error_is_not_swallowed_as_empty_success(self, agent):
        """A raw provider exception must go through the existing error-retry
        path, not be misreported as a successful empty response."""
        self._setup_agent(agent)
        agent.model = "openai/gpt-oss-20b"
        agent.max_retries = 1
        agent.client.chat.completions.create.side_effect = RuntimeError("upstream 500")
        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("hello")
        assert result["completed"] is False

    # ── DeepSeek non-regression (finish_reason=stop path, untouched) ────

    def test_deepseek_reasoning_content_stop_non_regression(self, agent):
        """DeepSeek-style stop+reasoning_content (the already-working
        thinking-prefill path, finish_reason='stop') must still work
        exactly as before — this change only touches the length branch."""
        self._setup_agent(agent)
        agent.model = "deepseek-reasoner"
        prefill_resp = _mock_response(
            content=None,
            finish_reason="stop",
            reasoning_content="structured reasoning answer",
        )
        content_resp = _mock_response(content="Here is the actual answer.", finish_reason="stop")
        agent.client.chat.completions.create.side_effect = [prefill_resp, content_resp]
        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("answer me")
        assert result["completed"] is True
        assert result["final_response"] == "Here is the actual answer."
        assert result["api_calls"] == 2
