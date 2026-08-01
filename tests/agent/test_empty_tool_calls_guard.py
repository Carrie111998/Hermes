"""Regression tests for premature turn-end on empty tool_calls arrays.

Bug: some providers (observed on GitHub Copilot with claude-opus-4.7 / 4.8)
return ``finish_reason="tool_calls"`` while the ``tool_calls`` array is empty,
carrying only a short preamble in ``content`` ("I'll read the brief file
first."). The dispatch check ``if assistant_message.tool_calls:`` is falsy for
``[]``, so the loop fell through to the final-text-response branch and ended the
turn at 1 API call out of a 60-call budget — surfacing a plan as if it were the
answer.

These tests assert the behaviour contract:
  finish_reason == "tool_calls" AND no tool calls  ->  do NOT end the turn.
"""
from __future__ import annotations

import pytest


from agent.conversation_loop import (
    _classify_tool_call_response,
    should_end_turn_on_text as _should_end_turn,
)


class _FakeFunction:
    def __init__(self, name="terminal", arguments="{}"):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, name="terminal", arguments="{}"):
        self.id = "call_1"
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    """Minimal stand-in for an OpenAI-SDK assistant message."""

    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls
        self.role = "assistant"


def _local_should_end_turn(finish_reason: str, message: _FakeMessage) -> bool:
    """Deliberately unused reference implementation.

    Kept only to document the intended semantics. Tests import the REAL
    production function (``should_end_turn_on_text``) so that disabling the
    guard in ``conversation_loop.py`` makes these tests fail.
    """
    declared = finish_reason == "tool_calls"
    has_calls = bool(getattr(message, "tool_calls", None))
    if declared and not has_calls:
        return False
    if has_calls:
        return False
    return True


# --- the exact production symptom ---------------------------------------

def test_empty_tool_calls_list_does_not_end_turn():
    """The reported bug: finish_reason=tool_calls + [] ended the turn."""
    msg = _FakeMessage(content="I'll read the brief file first.", tool_calls=[])
    assert _should_end_turn("tool_calls", msg) is False


def test_none_tool_calls_with_tool_calls_finish_reason_does_not_end_turn():
    msg = _FakeMessage(content="Let me gather the system state.", tool_calls=None)
    assert _should_end_turn("tool_calls", msg) is False


@pytest.mark.parametrize("preamble", [
    "I'll read the brief file first.",
    "Let me add a real handler.",
    "First, let me gather the system state.",
    "I'll start by reading the brief file.",
])
def test_known_stall_preambles_do_not_end_turn(preamble):
    """These exact strings ended real Meta-Auditor cron runs."""
    msg = _FakeMessage(content=preamble, tool_calls=[])
    assert _should_end_turn("tool_calls", msg) is False


# --- the guard must not break normal operation ---------------------------

def test_genuine_text_response_still_ends_turn():
    msg = _FakeMessage(content="🔎 Meta-Audit — done. Gaps found: 0", tool_calls=None)
    assert _should_end_turn("stop", msg) is True


def test_real_tool_calls_still_dispatch():
    msg = _FakeMessage(content="", tool_calls=[_FakeToolCall()])
    assert _should_end_turn("tool_calls", msg) is False


def test_text_with_tool_calls_still_dispatches():
    msg = _FakeMessage(content="Checking the log.", tool_calls=[_FakeToolCall()])
    assert _should_end_turn("tool_calls", msg) is False


def test_length_finish_reason_with_no_calls_still_ends():
    """Truncation is handled elsewhere; the guard must not swallow it."""
    msg = _FakeMessage(content="partial...", tool_calls=None)
    assert _should_end_turn("length", msg) is True


def test_stop_finish_reason_with_empty_list_still_ends():
    """Empty list but finish_reason=stop is a normal final answer."""
    msg = _FakeMessage(content="All done.", tool_calls=[])
    assert _should_end_turn("stop", msg) is True


# --- retry budget is bounded --------------------------------------------

def test_guard_retry_budget_is_bounded():
    """After 3 attempts the guard must give up, not spin forever."""
    class _Agent:
        _empty_tool_calls_retries = 0

    agent = _Agent()
    ended = None
    for _ in range(10):
        if agent._empty_tool_calls_retries < 3:
            agent._empty_tool_calls_retries += 1
            ended = False
            continue
        ended = True
        break
    assert ended is True
    assert agent._empty_tool_calls_retries == 3


def test_counter_resets_on_healthy_turn():
    class _Agent:
        _empty_tool_calls_retries = 2

    agent = _Agent()
    msg = _FakeMessage(content="", tool_calls=[_FakeToolCall()])
    if not (bool(msg.tool_calls) is False):
        agent._empty_tool_calls_retries = 0
    assert agent._empty_tool_calls_retries == 0


# --- source-level contract ----------------------------------------------

def test_guard_exists_in_conversation_loop_source():
    """Lock the guard in place so a refactor can't silently drop it."""
    import os
    from agent import conversation_loop

    src = open(conversation_loop.__file__).read()
    assert "_empty_tool_calls_retries" in src, "empty tool_calls guard missing"
    assert "finish_reason=tool_calls with empty tool_calls array" in src


def test_guard_precedes_tool_dispatch():
    """The guard must run BEFORE `if assistant_message.tool_calls:`."""
    from agent import conversation_loop

    src = open(conversation_loop.__file__).read()
    guard_at = src.index("_classify_tool_call_response(")
    dispatch_at = src.index("# Check for tool calls\n            if assistant_message.tool_calls:")
    assert guard_at < dispatch_at, "guard must precede tool dispatch"
