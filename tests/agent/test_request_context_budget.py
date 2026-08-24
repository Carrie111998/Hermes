"""Tests for request-level context budgeting and token-based history selection."""

from types import SimpleNamespace

from agent.conversation_loop import _apply_request_context_budget_window
from agent.request_context_budget import (
    RequestContextBudget,
    select_request_context_window,
    select_token_budgeted_history_tail,
)


def _message(role: str, content: str, **extra):
    return {"role": role, "content": content, **extra}


class TestRequestContextBudget:
    def test_reserves_system_tools_and_output_before_history(self):
        budget = RequestContextBudget(
            context_window_tokens=1000,
            reserved_output_tokens=250,
            system_prompt_tokens=200,
            tool_schema_tokens=150,
            confidence="rough",
        )

        assert budget.history_budget_tokens == 400
        assert budget.safe_input_budget_tokens == 750
        assert budget.fixed_input_tokens == 350

    def test_never_allocates_negative_history_budget_for_large_fixed_prompt(self):
        budget = RequestContextBudget(
            context_window_tokens=1000,
            reserved_output_tokens=300,
            system_prompt_tokens=800,
            tool_schema_tokens=200,
            confidence="rough",
        )

        assert budget.safe_input_budget_tokens == 700
        assert budget.history_budget_tokens == 0

    def test_rejects_unknown_confidence(self):
        try:
            RequestContextBudget(
                context_window_tokens=1000,
                reserved_output_tokens=1,
                system_prompt_tokens=1,
                tool_schema_tokens=1,
                confidence="guessed",
            )
        except ValueError as exc:
            assert "confidence" in str(exc)
        else:
            raise AssertionError("unknown confidence must be rejected")

    def test_rejects_non_integer_history_budget_input(self):
        messages = [_message("user", "latest request")]

        try:
            select_token_budgeted_history_tail(messages, history_budget_tokens="10")
        except ValueError as exc:
            assert "history_budget_tokens" in str(exc)
        else:
            raise AssertionError("non-integer budget must be rejected")


class TestTokenBudgetedHistoryTail:
    def test_drops_old_messages_before_latest_user_turn_when_budget_is_exhausted(self):
        messages = [
            _message("user", "old user " + "a" * 200),
            _message("assistant", "old assistant " + "b" * 200),
            _message("user", "latest user request"),
            _message("assistant", "latest answer"),
        ]

        selected = select_token_budgeted_history_tail(messages, history_budget_tokens=20)

        assert selected.messages == messages[2:]
        assert selected.omitted_message_count == 2
        assert selected.pinned_tokens > selected.history_budget_tokens

    def test_keeps_active_tool_chain_with_its_latest_user_turn(self):
        messages = [
            _message("user", "old request " + "x" * 200),
            _message("assistant", "old reply " + "y" * 200),
            _message("user", "deploy now"),
            _message(
                "assistant",
                "",
                tool_calls=[{"id": "call-1", "function": {"name": "terminal", "arguments": "{}"}}],
            ),
            _message("tool", "build output", tool_call_id="call-1"),
        ]

        selected = select_token_budgeted_history_tail(messages, history_budget_tokens=5)

        assert selected.messages == messages[2:]
        assert selected.messages[-2]["tool_calls"][0]["id"] == "call-1"
        assert selected.messages[-1]["tool_call_id"] == "call-1"

    def test_adds_only_complete_older_tool_group_when_it_fits(self):
        messages = [
            _message("user", "older request"),
            _message(
                "assistant",
                "",
                tool_calls=[{"id": "call-1", "function": {"name": "read_file", "arguments": "{}"}}],
            ),
            _message("tool", "small result", tool_call_id="call-1"),
            _message("assistant", "older conclusion"),
            _message("user", "latest request"),
        ]

        selected = select_token_budgeted_history_tail(messages, history_budget_tokens=80)

        assert selected.messages == messages
        assert selected.omitted_message_count == 0

    def test_does_not_orphan_prior_assistant_response_from_its_user_turn(self):
        messages = [
            _message("user", "older request " + "a" * 100),
            _message("assistant", "older conclusion"),
            _message("user", "latest request"),
        ]

        selected = select_token_budgeted_history_tail(messages, history_budget_tokens=25)

        assert selected.messages == messages[-1:]
        assert selected.omitted_message_count == 2

    def test_excludes_oversized_old_tool_result_without_mutating_source(self):
        huge_tool = _message("tool", "z" * 10_000, tool_call_id="call-1")
        messages = [
            _message("user", "old request"),
            _message(
                "assistant",
                "",
                tool_calls=[{"id": "call-1", "function": {"name": "terminal", "arguments": "{}"}}],
            ),
            huge_tool,
            _message("user", "latest request"),
        ]

        selected = select_token_budgeted_history_tail(messages, history_budget_tokens=20)

        assert selected.messages == messages[-1:]
        assert selected.omitted_message_count == 3
        assert huge_tool["content"] == "z" * 10_000


class TestRequestContextWindow:
    def test_preserves_fixed_system_prefix_while_clipping_history(self):
        request_messages = [
            _message("system", "dynamic system prompt " + "s" * 100),
            _message("user", "old request " + "a" * 1_000),
            _message("assistant", "old reply " + "b" * 1_000),
            _message("user", "latest request"),
        ]
        budget = RequestContextBudget(
            context_window_tokens=100,
            reserved_output_tokens=20,
            system_prompt_tokens=30,
            tool_schema_tokens=20,
            confidence="rough",
        )

        selected = select_request_context_window(
            request_messages,
            fixed_prefix_count=1,
            budget=budget,
        )

        assert selected.messages[0] == request_messages[0]
        assert selected.messages[1:] == request_messages[-1:]
        assert selected.omitted_message_count == 2


class TestConversationLoopBudgetIntegration:
    def test_uses_active_system_and_tool_schema_before_selecting_history(self):
        agent = SimpleNamespace(
            context_compressor=SimpleNamespace(context_length=100),
            max_tokens=20,
            tools=[{"type": "function", "function": {"name": "tool", "parameters": {}}}],
        )
        request_messages = [
            _message("system", "dynamic system prompt " + "s" * 100),
            _message("user", "old request " + "a" * 1_000),
            _message("assistant", "old response " + "b" * 1_000),
            _message("user", "latest request"),
        ]

        selected = _apply_request_context_budget_window(
            agent,
            request_messages,
            fixed_prefix_count=1,
        )

        assert selected[0] == request_messages[0]
        assert selected[-1] == request_messages[-1]
        assert len(selected) == 2
        assert agent._last_request_context_budget.system_prompt_tokens > 0
        assert agent._last_request_context_budget.tool_schema_tokens > 0
        assert agent._last_request_context_budget.reserved_output_tokens == 20
