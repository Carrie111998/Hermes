"""Tests for Anthropic prompt caching breakpoint bounds and negative slice guard."""

from unittest.mock import patch
from agent.prompt_caching import (
    apply_anthropic_cache_control,
    _count_cache_markers,
)


def test_apply_anthropic_cache_control_when_breakpoints_exhausted():
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

    # Mock _apply_system_cache_markers to simulate 4 breakpoints consumed on system/scaffold
    with patch("agent.prompt_caching._apply_system_cache_markers", return_value=4):
        result = apply_anthropic_cache_control(messages)

        # None of the non-system messages should receive cache_control
        non_sys_marked = [
            m for m in result if m.get("role") != "system" and "cache_control" in str(m)
        ]
        assert len(non_sys_marked) == 0
        assert _count_cache_markers(result, []) <= 4


def test_apply_anthropic_cache_control_normal_budget():
    """Under normal circumstances with 1 system breakpoint, exactly 3 non-sys messages are marked."""
    messages = [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "U1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "U2"},
        {"role": "assistant", "content": "A2"},
    ]

    result = apply_anthropic_cache_control(messages)
    assert _count_cache_markers(result, []) == 4
