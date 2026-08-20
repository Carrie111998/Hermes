"""Tests for Anthropic prompt caching breakpoint bounds, slice safety, and tool accounting."""

from unittest.mock import patch
from agent.prompt_caching import (
    apply_anthropic_cache_control,
    build_prompt_cache_plan,
    _count_cache_markers,
    _enforce_max_cache_budget,
    _can_carry_marker,
)


class TestPromptCachingSliceOverflow:
    def test_apply_anthropic_cache_control_when_breakpoints_exhausted(self):
        """When breakpoints_used >= 4, remaining is <= 0.
        non_sys[-0:] must NOT mark all non-system messages in history.
        """
        messages = [
            {"role": "system", "content": "You are Hermes Agent."},
            {"role": "user", "content": "Hello 1"},
            {"role": "assistant", "content": "Hi 1"},
            {"role": "user", "content": "Hello 2"},
            {"role": "assistant", "content": "Hi 2"},
            {"role": "user", "content": "Hello 3"},
            {"role": "assistant", "content": "Hi 3"},
        ]

        with patch("agent.prompt_caching._apply_system_cache_markers", return_value=4):
            result = apply_anthropic_cache_control(messages)

            non_sys_marked = [
                m for m in result if m.get("role") != "system" and "cache_control" in str(m)
            ]
            assert len(non_sys_marked) == 0
            assert _count_cache_markers(result, []) <= 4

    def test_apply_anthropic_cache_control_when_sys_used_exceeds_budget(self):
        """When sys_used > 4, defense-in-depth clamp ensures total <= 4."""
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "User question"},
            {"role": "assistant", "content": "Assistant answer"},
        ]
        with patch("agent.prompt_caching._apply_system_cache_markers", return_value=5):
            result = apply_anthropic_cache_control(messages)
            assert _count_cache_markers(result, []) <= 4

    def test_apply_anthropic_cache_control_empty_messages(self):
        """Empty messages list is a safe no-op."""
        assert apply_anthropic_cache_control([]) == []

    def test_apply_anthropic_cache_control_normal_budget_permutations(self):
        """Under sys_used in (0, 1, 2, 3), total markers exactly hit target <= 4."""
        for sys_used in (0, 1, 2, 3):
            messages = [
                {"role": "system", "content": "System prompt"},
                {"role": "user", "content": "U1"},
                {"role": "assistant", "content": "A1"},
                {"role": "user", "content": "U2"},
                {"role": "assistant", "content": "A2"},
                {"role": "user", "content": "U3"},
                {"role": "assistant", "content": "A3"},
            ]

            def fake_apply_sys(msg, marker, prefix, **kwargs):
                if sys_used == 1:
                    msg["cache_control"] = marker
                elif sys_used == 2:
                    msg["content"] = [
                        {"type": "text", "text": "p", "cache_control": marker},
                        {"type": "text", "text": "s", "cache_control": marker},
                    ]
                elif sys_used == 3:
                    msg["content"] = [
                        {"type": "text", "text": "p1", "cache_control": marker},
                        {"type": "text", "text": "p2", "cache_control": marker},
                        {"type": "text", "text": "p3", "cache_control": marker},
                    ]
                return sys_used

            with patch("agent.prompt_caching._apply_system_cache_markers", side_effect=fake_apply_sys):
                result = apply_anthropic_cache_control(messages)
                expected_non_sys = min(len(messages) - 1, 4 - sys_used)
                non_sys_marked = [
                    m for m in result if m.get("role") != "system" and "cache_control" in str(m)
                ]
                assert len(non_sys_marked) == expected_non_sys
                assert _count_cache_markers(result, []) == sys_used + expected_non_sys

    def test_build_prompt_cache_plan_dynamic_tool_accounting(self):
        """build_prompt_cache_plan dynamically counts tools and never exceeds 4 markers."""
        tools = [
            {"type": "function", "function": {"name": "tool_a"}},
            {"type": "function", "function": {"name": "tool_b"}},
            {"type": "function", "function": {"name": "tool_c"}},
        ]
        messages = [
            {"role": "system", "content": "PREFIX_STATIC System prompt"},
            {"role": "user", "content": "Run tool"},
            {"role": "assistant", "content": "Calling", "tool_calls": [{"name": "tool_a"}]},
            {"role": "tool", "content": "output", "tool_name": "tool_a"},
            {"role": "assistant", "content": "Done!"},
        ]

        plan = build_prompt_cache_plan(
            messages,
            tools,
            native_anthropic=True,
            direct_native_tool_cache=True,
            static_system_prefix="PREFIX_STATIC",
        )

        assert plan.marker_count <= 4
        # Exactly 1 tool marker on the last tool
        assert "cache_control" in plan.tools[-1]
        assert "cache_control" not in plan.tools[0]
        assert "cache_control" not in plan.tools[1]

    def test_build_prompt_cache_plan_direct_tool_cache_with_no_tools(self):
        """When direct_native_tool_cache=True but tools is empty, falls back safely."""
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Question"},
            {"role": "assistant", "content": "Answer"},
        ]
        plan = build_prompt_cache_plan(
            messages,
            [],
            native_anthropic=True,
            direct_native_tool_cache=True,
        )
        assert plan.marker_count <= 4
        assert len(plan.tools) == 0

    def test_enforce_max_cache_budget_clamps_overflow(self):
        """_enforce_max_cache_budget strips overflow markers from oldest non-system messages."""
        messages = [
            {"role": "system", "content": [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]},
            {"role": "user", "content": [{"type": "text", "text": "u1", "cache_control": {"type": "ephemeral"}}]},
            {"role": "assistant", "content": [{"type": "text", "text": "a1", "cache_control": {"type": "ephemeral"}}]},
            {"role": "user", "content": [{"type": "text", "text": "u2", "cache_control": {"type": "ephemeral"}}]},
            {"role": "assistant", "content": [{"type": "text", "text": "a2", "cache_control": {"type": "ephemeral"}}]},
        ]
        tools = [{"type": "function", "cache_control": {"type": "ephemeral"}}]
        # Total is 1 sys + 4 msgs + 1 tool = 6 markers.
        assert _count_cache_markers(messages, tools) == 6

        _enforce_max_cache_budget(messages, tools, max_budget=4)
        assert _count_cache_markers(messages, tools) == 4
        # u1 and a1 (oldest non-system) were stripped; u2, a2, sys, tool survived
        assert "cache_control" not in messages[1]["content"][0]
        assert "cache_control" not in messages[2]["content"][0]
        assert "cache_control" in messages[3]["content"][0]
        assert "cache_control" in messages[4]["content"][0]
        assert "cache_control" in messages[0]["content"][0]
        assert "cache_control" in tools[0]

    def test_can_carry_marker_envelope_vs_native(self):
        """_can_carry_marker properly filters empty turns on non-native layouts."""
        empty_assistant = {"role": "assistant", "content": None}
        assert _can_carry_marker(empty_assistant, native_anthropic=False) is False
        assert _can_carry_marker(empty_assistant, native_anthropic=True) is True

        normal_user = {"role": "user", "content": "Hello"}
        assert _can_carry_marker(normal_user, native_anthropic=False) is True
        assert _can_carry_marker(normal_user, native_anthropic=True) is True
