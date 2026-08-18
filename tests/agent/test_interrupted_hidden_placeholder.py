"""
Tests for empty display_kind=hidden assistant placeholder handling (#88955).

Ensures that in Bot Mode group chats (and active turn redirects), when a member
turn is interrupted without visible text, the empty hidden assistant placeholder
is safely substituted with [response interrupted] on the wire copy without
repeatedly triggering pre-call sanitizer warning spam on every subsequent turn.
"""

import logging
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
import pytest

from agent.conversation_loop import _apply_active_turn_redirect
from agent.agent_runtime_helpers import (
    repair_empty_non_final_messages,
    _INTERRUPTED_PLACEHOLDER,
)
from run_agent import AIAgent


def _mock_response(content="Hello", finish_reason="stop", tool_calls=None):
    msg = SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        reasoning=None,
        reasoning_content=None,
        reasoning_details=None,
    )
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(
        choices=[choice],
        model="test/model",
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10, total_tokens=20),
    )


@pytest.fixture
def agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
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


class TestInterruptedHiddenPlaceholder:
    def test_apply_active_turn_redirect_creates_hidden_placeholder_when_not_visible(self, agent):
        """When interrupted with no visible text, active turn redirect creates
        an empty display_kind=hidden assistant placeholder."""
        agent._current_streamed_assistant_text = ""
        messages = [{"role": "user", "content": "Member turn prompt"}]

        _apply_active_turn_redirect(agent, messages, "User interruption")

        assert len(messages) == 3
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"] == ""
        assert messages[1]["display_kind"] == "hidden"
        assert messages[2]["role"] == "user"
        assert messages[2]["content"] == "User interruption"

    def test_repair_empty_non_final_messages_heals_hidden_assistant_without_warning(self, caplog):
        """repair_empty_non_final_messages substitutes [response interrupted] on
        display_kind=hidden assistant messages without logging healing warnings (#88955)."""
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "", "display_kind": "hidden"},
            {"role": "user", "content": "world"},
            {"role": "assistant", "content": "response"},
        ]

        with caplog.at_level(logging.WARNING):
            result = repair_empty_non_final_messages(messages)

        assert len(result) == 4
        assert result[1]["content"] == _INTERRUPTED_PLACEHOLDER
        assert "Pre-call sanitizer: healed" not in caplog.text

    def test_repair_empty_non_final_messages_still_heals_unintended_empty_messages(self, caplog):
        """Non-hidden empty messages (true corruption) are healed with
        _INTERRUPTED_PLACEHOLDER AND log a warning."""
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": ""},  # No display_kind=hidden
            {"role": "user", "content": "world"},
            {"role": "assistant", "content": "response"},
        ]

        with caplog.at_level(logging.WARNING):
            result = repair_empty_non_final_messages(messages)

        assert len(result) == 4
        assert result[1]["content"] == _INTERRUPTED_PLACEHOLDER
        assert "Pre-call sanitizer: healed" in caplog.text

    def test_group_chat_interruption_does_not_retrigger_sanitizer_on_subsequent_turns(self, agent, caplog):
        """Regression test for #88955:
        In a multi-turn conversation with an interrupted turn, subsequent turns
        must not log pre-call sanitizer warnings.
        """
        history = [
            {"role": "user", "content": "Member turn 1"},
            {"role": "assistant", "content": "", "display_kind": "hidden"},
            {
                "role": "user",
                "content": "Interruption",
                "api_content": "[Context from the interrupted assistant response]\nInterruption",
            },
        ]

        requests = []
        def fake_api_call(api_kwargs):
            requests.append(api_kwargs)
            return _mock_response(content="Final response", finish_reason="stop")

        with (
            patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            caplog.at_level(logging.WARNING),
        ):
            result = agent.run_conversation("Member turn 2", conversation_history=history)

        assert result["completed"] is True
        assert len(requests) == 1
        replayed = requests[0]["messages"]

        # Ensure non-final assistant message has valid placeholder content on wire
        for m in replayed[:-1]:
            if m.get("role") == "assistant":
                assert m.get("content") != "" or m.get("tool_calls")

        # Ensure no recurring sanitizer warning was emitted
        assert "Pre-call sanitizer: healed" not in caplog.text

    def test_interrupted_turn_with_visible_text_preserved(self, agent):
        """When an interrupted turn had visible text on screen, it is preserved."""
        agent._current_streamed_assistant_text = "I am looking up the data..."
        messages = [{"role": "user", "content": "start"}]

        _apply_active_turn_redirect(agent, messages, "Wait, stop that.")

        assert len(messages) == 3
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"] == "I am looking up the data..."
        assert "display_kind" not in messages[1]
        assert messages[2]["role"] == "user"
        assert messages[2]["content"] == "Wait, stop that."
