"""Unit tests for the fabricated-tool-use text classifier.

Pure-function tests, no agent/loop mocking -- see
tests/run_agent/test_fabricated_tool_use_recovery.py for the full
retry-loop integration tests.
"""

from __future__ import annotations

from agent.conversation_loop import _looks_like_fabricated_tool_use


class TestLooksLikeFabricatedToolUse:
    def test_flags_real_captured_json_leak(self):
        """Captured live, 2026-08-26: a raw tool-call JSON fragment leaked
        as prose with no real tool_calls behind it."""
        text = '{"arguments": {"query": "local LLM inference"}, "name": "web_search"}'
        assert _looks_like_fabricated_tool_use(text) is True

    def test_flags_real_captured_narrated_json_leak(self):
        text = (
            "Of course, here are my steps to find the current price of gold "
            "online:\n\n<EXPLANATION>\n"
            '{"arguments": {"query": "current price of gold"}, "name": "web_search"}'
        )
        assert _looks_like_fabricated_tool_use(text) is True

    def test_flags_real_captured_self_claim_fabrication(self):
        """Captured live, 2026-08-26: a fully fabricated fake search result,
        no JSON marker at all -- placeholder URLs/prices presented as fact."""
        text = (
            "I'll perform a quick web search. Here's the result from my "
            "search:\n1 | Gold Price Today - December X, 20XX\n"
            "   url: https://example.com/gold\n"
            "The current gold price is USD $... per troy ounce. This search "
            "returned results from the authoritative historical charting "
            "service I trusted to give factual up-to-date information."
        )
        assert _looks_like_fabricated_tool_use(text) is True

    def test_flags_json_shape_for_a_different_tool(self):
        """The JSON-shape signal is tool-agnostic -- not coupled to search."""
        text = (
            'I ran the command. Here is what happened: '
            '{"name": "terminal", "arguments": {"command": "ls"}}'
        )
        assert _looks_like_fabricated_tool_use(text) is True

    def test_does_not_flag_an_ordinary_clean_reply(self):
        assert _looks_like_fabricated_tool_use("Here is your answer: 4.") is False

    def test_does_not_flag_an_honest_disclaimer(self):
        text = (
            "I don't have a way to look that up right now, but based on "
            "general knowledge, gold has historically traded around "
            "$2000/oz."
        )
        assert _looks_like_fabricated_tool_use(text) is False

    def test_does_not_flag_a_proposal_to_check_later(self):
        assert _looks_like_fabricated_tool_use("Let me check that for you.") is False

    def test_returns_false_for_empty_or_none_text(self):
        assert _looks_like_fabricated_tool_use("") is False
        assert _looks_like_fabricated_tool_use(None) is False

    def test_flags_legitimate_schema_discussion_as_an_accepted_tradeoff(self):
        """KNOWN, ACCEPTED limitation: prose that legitimately quotes a
        tool-call-shaped JSON example (e.g. explaining an API schema) also
        matches. Pinned here deliberately so this boundary is visible and
        intentional, not a silent gap -- see the comment above
        _FABRICATED_TOOL_USE_HAS_NAME_KEY."""
        text = 'Your schema should look like {"name": "get_weather", "arguments": {"location": "string"}}'
        assert _looks_like_fabricated_tool_use(text) is True
