"""Focused contracts for post-tool round advancement."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from agent import conversation_loop, turn_post_tools


class _Compressor:
    def __init__(self, tokens=0, compress=False):
        self.last_prompt_tokens = tokens
        self.compress = compress
        self.threshold_tokens = 100
        self.prune_tokens = None

    def should_compress(self, tokens):
        return self.compress

    def prune_tool_results_only(self, messages, *, current_tokens):
        self.prune_tokens = current_tokens
        return messages, 0


class _Agent:
    compression_enabled = True
    tools = []

    def __init__(self, compressor):
        self.context_compressor = compressor
        self.iteration_budget = SimpleNamespace(refund=lambda: setattr(self, "refunded", True))
        self.refunded = False
        self.session_messages = None
        self.touches = []

    def _safe_print(self, text):
        self.printed = text

    def _touch_activity(self, text):
        self.touches.append(text)

    def _warn_context_overflow_blocked(self, *args):
        self.warned = args


def _call(name):
    return SimpleNamespace(function=SimpleNamespace(name=name))


def _advance(agent, calls, **overrides):
    kwargs = dict(
        system_message={"role": "system", "content": "system"},
        active_system_prompt="system",
        user_message="question",
        task_id="task",
        api_call_count=3,
        compression_attempts=0,
        max_compression_attempts=2,
        final_response="",
        should_skip_handoff=lambda messages, user: False,
        handoff_final_response="handoff",
    )
    kwargs.update(overrides)
    return turn_post_tools.advance_after_tool_execution(agent, calls, [], [], **kwargs)


def test_execute_code_refund_and_next_round_activity_are_preserved():
    agent = _Agent(_Compressor())

    result = _advance(agent, [_call("execute_code")])

    assert agent.refunded is True
    assert agent._stream_needs_break is True
    assert agent._session_messages == []
    assert agent.touches == ["tool results posted, continuing iteration #3"]
    assert result.exit_reason is None


def test_prune_receives_the_actual_post_tool_token_count():
    compressor = _Compressor(tokens=321)
    agent = _Agent(compressor)

    _advance(agent, [_call("read_file")])

    assert compressor.prune_tokens == 321


def test_compaction_handoff_stops_before_next_round(monkeypatch):
    compressor = _Compressor(tokens=321, compress=True)
    agent = _Agent(compressor)
    compressed = [{"role": "user", "content": "reference"}]
    agent._compress_context = lambda *args, **kwargs: (compressed, "new-system")
    monkeypatch.setattr(turn_post_tools, "conversation_history_after_compression", lambda *args: ["history"])

    result = _advance(
        agent,
        [_call("read_file")],
        should_skip_handoff=lambda messages, user: True,
    )

    assert result.messages is compressed
    assert result.conversation_history == ["history"]
    assert result.active_system_prompt == "new-system"
    assert result.exit_reason == "compaction_handoff_not_actionable"
    assert result.final_response == "handoff"
    assert agent.touches == []


def test_conversation_loop_uses_post_tool_module_as_owner():
    source = inspect.getsource(conversation_loop.run_conversation)
    assert "post_tool = advance_after_tool_execution(" in source
