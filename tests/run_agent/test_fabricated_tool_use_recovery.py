"""Regression tests for fabricated tool-use recovery.

Under real conditions (observed live: a search backend that reports itself
available but is unreachable, with no keyless fallback), a genuinely
completed turn (finish_reason="stop", normal token usage, no error) can
return text that either leaks a raw tool-call-shaped JSON fragment as
prose, or fully fabricates a plausible-looking tool result — both with an
empty tool_calls array. Before this fix, neither the existing
dropped-tool-call recovery (gated on finish_reason="tool_calls") nor the
Codex-specific Harmony-leak recovery (gated on that one transport and
finish_reason="incomplete") ever saw this: a finish_reason="stop" text
finish never entered either guard.

The fix keys on the turn's own content (a JSON tool-call shape, or
affirmative self-claiming language) independent of provider and of the
specific value of finish_reason -- other than "tool_calls", which is the
dropped-tool-call check's own territory and is deliberately excluded here
so the two checks' retry budgets can't compound (see
test_tool_calls_finish_reason_does_not_compound_both_budgets below). It
re-prompts, bounded to 3 consecutive stalls, with the budget resetting
after any successful tool round — the same recovery discipline already
proven for the dropped-tool-call case. A genuine clean answer, an honest
disclaimer, or a real tool call's own summary text are all unaffected.
See docs/rfcs/2026-08-fabricated-tool-use-detection.md.
"""

from __future__ import annotations

from unittest.mock import patch

# `loop_agent` is defined in tests/run_agent/conftest.py (shared across this
# directory) rather than redefined here.

FAKE_GOLD_SEARCH_TEXT = (
    "I'll perform a quick web search. Here's the result from my "
    "search:\n1 | Gold Price Today - December X, 20XX\n"
    "   url: https://example.com/gold\n"
    "The current gold price is USD $... per troy ounce. This search "
    "returned results from the authoritative historical charting "
    "service I trusted to give factual up-to-date information."
)

RAW_JSON_LEAK_TEXT = (
    '{"arguments": {"query": "local LLM inference"}, "name": "web_search"}'
)


class TestFabricatedToolUseRecovery:
    def test_json_leak_reprompts_instead_of_publishing(self, loop_agent):
        """A raw tool-call JSON leak with finish_reason=stop and empty
        tool_calls must re-prompt, not be treated as the final answer."""
        from tests.run_agent.test_run_agent import _mock_response

        loop_agent.client.chat.completions.create.side_effect = [
            _mock_response(content=RAW_JSON_LEAK_TEXT, finish_reason="stop"),
            _mock_response(content="Gold is trading around $2,650/oz today.", finish_reason="stop"),
        ]

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation("what's the price of gold")

        assert loop_agent.client.chat.completions.create.call_count == 2, (
            "A leaked tool-call JSON fragment must trigger a re-prompt, not "
            "be published as the final answer."
        )
        assert "$2,650" in result["final_response"]

    def test_self_claim_fabrication_reprompts_instead_of_publishing(self, loop_agent):
        """The real captured fake-search-result text (no JSON marker at all)
        must also trigger a re-prompt."""
        from tests.run_agent.test_run_agent import _mock_response

        loop_agent.client.chat.completions.create.side_effect = [
            _mock_response(content=FAKE_GOLD_SEARCH_TEXT, finish_reason="stop"),
            _mock_response(content="Gold is trading around $2,650/oz today.", finish_reason="stop"),
        ]

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation("what's the price of gold")

        assert loop_agent.client.chat.completions.create.call_count == 2
        assert "$2,650" in result["final_response"]

    def test_reprompt_asks_for_a_real_call_not_a_claim(self, loop_agent):
        from tests.run_agent.test_run_agent import _mock_response

        loop_agent.client.chat.completions.create.side_effect = [
            _mock_response(content=FAKE_GOLD_SEARCH_TEXT, finish_reason="stop"),
            _mock_response(content="Gold is trading around $2,650/oz today.", finish_reason="stop"),
        ]

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            loop_agent.run_conversation("what's the price of gold")

        second_call = loop_agent.client.chat.completions.create.call_args_list[1]
        msgs = second_call.kwargs.get("messages") or second_call.args[0].get("messages")
        last_user = next((m for m in reversed(msgs) if m.get("role") == "user"), None)
        assert last_user is not None
        assert "tool" in (last_user.get("content") or "").lower()

    def test_clean_stop_text_turn_is_unaffected(self, loop_agent):
        """A genuine finish_reason=stop text response must exit normally —
        the recovery path must not fire on ordinary final answers."""
        from tests.run_agent.test_run_agent import _mock_response

        loop_agent.client.chat.completions.create.side_effect = [
            _mock_response(content="Here is your answer.", finish_reason="stop"),
        ]

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation("hello")

        assert loop_agent.client.chat.completions.create.call_count == 1, (
            "An ordinary clean answer must not trigger a re-prompt."
        )
        assert "Here is your answer." in result["final_response"]

    def test_honest_disclaimer_is_unaffected(self, loop_agent):
        """An honest 'I can't look that up' admission is the OPPOSITE of
        this bug and must never be flagged."""
        from tests.run_agent.test_run_agent import _mock_response

        disclaimer = (
            "I don't have a way to look that up right now, but based on "
            "general knowledge, gold has historically traded around "
            "$2000/oz."
        )
        loop_agent.client.chat.completions.create.side_effect = [
            _mock_response(content=disclaimer, finish_reason="stop"),
        ]

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation("what's the price of gold")

        assert loop_agent.client.chat.completions.create.call_count == 1
        assert disclaimer in result["final_response"]

    def test_real_tool_call_with_similar_summary_text_is_unaffected(self, loop_agent):
        """A genuine tool call whose summary text happens to say 'the
        search returned' must not be flagged — a real tool_calls entry in
        an earlier round of the same turn always wins (the
        _landed_real_tool_call_this_turn guard)."""
        from tests.run_agent.test_run_agent import _mock_response, _mock_tool_call

        loop_agent.valid_tool_names.add("web_search")

        tool_turn = _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_mock_tool_call(name="web_search", arguments='{"query": "gold price"}', call_id="c1")],
        )
        summary = _mock_response(
            content="The search returned $2,650/oz as today's gold price.",
            finish_reason="stop",
        )
        loop_agent.client.chat.completions.create.side_effect = [tool_turn, summary]

        with (
            patch("run_agent.handle_function_call", return_value="gold: $2,650/oz"),
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation("what's the price of gold")

        assert loop_agent.client.chat.completions.create.call_count == 2, (
            "A real tool call followed by a summary must not trigger an "
            "extra re-prompt even though the summary text matches a "
            "fabrication pattern."
        )
        assert "$2,650" in result["final_response"]

    def test_landed_flag_does_not_leak_into_a_later_separate_turn(self, loop_agent):
        """_landed_real_tool_call_this_turn must reset at genuine turn end —
        otherwise one real tool call anywhere in a session would permanently
        disable this check for every later, unrelated turn."""
        from tests.run_agent.test_run_agent import _mock_response, _mock_tool_call

        loop_agent.valid_tool_names.add("web_search")

        # First turn: a real tool call lands and completes cleanly.
        first_turn_tool_call = _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_mock_tool_call(name="web_search", arguments='{"query": "weather"}', call_id="c1")],
        )
        first_turn_summary = _mock_response(content="It's sunny.", finish_reason="stop")

        # Second, separate turn: a fabrication with no real tool call at all.
        second_turn_fabrication = _mock_response(content=FAKE_GOLD_SEARCH_TEXT, finish_reason="stop")
        second_turn_recovered = _mock_response(
            content="Gold is trading around $2,650/oz today.", finish_reason="stop",
        )

        loop_agent.client.chat.completions.create.side_effect = [
            first_turn_tool_call, first_turn_summary,
            second_turn_fabrication, second_turn_recovered,
        ]

        with (
            patch("run_agent.handle_function_call", return_value="sunny, 72F"),
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            first_result = loop_agent.run_conversation("what's the weather")
            second_result = loop_agent.run_conversation("what's the price of gold")

        assert "sunny" in first_result["final_response"]
        # If the flag leaked from turn 1, this would be 1 (no re-prompt) —
        # 2 proves the fabrication was still caught in the second, separate turn.
        assert loop_agent.client.chat.completions.create.call_count == 4, (
            "The second turn's fabrication must still be caught — a real "
            "tool call in an EARLIER, separate turn must not permanently "
            "disable this check."
        )
        assert "$2,650" in second_result["final_response"]

    def test_persistent_fabrications_are_bounded(self, loop_agent):
        """If the model never stops fabricating, recovery must give up
        after a bounded number of consecutive stalls."""
        from tests.run_agent.test_run_agent import _mock_response

        loop_agent.client.chat.completions.create.side_effect = [
            _mock_response(content=FAKE_GOLD_SEARCH_TEXT, finish_reason="stop") for _ in range(9)
        ] + [_mock_response(content="done", finish_reason="stop")]

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation("what's the price of gold")

        assert loop_agent.client.chat.completions.create.call_count <= 4, (
            "Consecutive fabrications must be bounded (no infinite loop)."
        )
        assert result is not None

    def test_tool_calls_finish_reason_does_not_compound_both_budgets(self, loop_agent):
        """A finish_reason=tool_calls stall whose narration ALSO happens to
        match the fabrication pattern must be bounded by the dropped-toolcall
        check's own budget (3) alone -- not get 3 MORE retries from the
        fabrication check once that budget is exhausted."""
        from tests.run_agent.test_run_agent import _mock_response

        # Text that is BOTH a dropped-toolcall stall (finish_reason=tool_calls,
        # empty tool_calls) AND matches the fabrication pattern.
        overlapping_stall = _mock_response(
            content=RAW_JSON_LEAK_TEXT, finish_reason="tool_calls", tool_calls=None,
        )
        loop_agent.client.chat.completions.create.side_effect = [
            overlapping_stall for _ in range(9)
        ] + [_mock_response(content="done", finish_reason="stop")]

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            loop_agent.run_conversation("what's the price of gold")

        assert loop_agent.client.chat.completions.create.call_count <= 4, (
            "The dropped-toolcall and fabrication budgets must not compound: "
            "an overlap case is bounded by 3 total consecutive retries, not 6."
        )

    def test_nudge_pair_is_ephemeral_scaffolding(self, loop_agent):
        """The re-prompt pair must be flagged as ephemeral scaffolding so
        persistence never writes it to the durable transcript."""
        from run_agent import _is_ephemeral_scaffolding
        from tests.run_agent.test_run_agent import _mock_response

        loop_agent.client.chat.completions.create.side_effect = [
            _mock_response(content=RAW_JSON_LEAK_TEXT, finish_reason="stop"),
            _mock_response(content="Gold is trading around $2,650/oz today.", finish_reason="stop"),
        ]

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation("what's the price of gold")

        assert result["completed"] is True
        leftover = [
            m for m in result["messages"]
            if isinstance(m, dict) and m.get("_fabricated_tool_use_nudge")
        ]
        assert not leftover, (
            "The re-prompt pair must be stripped at finalization, not kept "
            "in the returned transcript."
        )
        assert _is_ephemeral_scaffolding(
            {"role": "user", "content": "nudge", "_fabricated_tool_use_nudge": True}
        ), (
            "_fabricated_tool_use_nudge messages must be classified as "
            "ephemeral scaffolding so they are never persisted."
        )
