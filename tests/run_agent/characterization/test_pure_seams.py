"""Layer B — pure-function golden/pinning tests.

Fast, table-driven characterization of the pure helpers that survive the
upcoming refactor as the extracted modules' contract.  No agent construction:
these call module-level functions / static methods / transport methods
directly and pin their exact outputs for representative inputs.

When the refactor moves these into ``agent/*`` modules, the *behavior* pinned
here must be identical.  If a function moves, update the import — never relax
an assertion.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import run_agent
from run_agent import AIAgent, IterationBudget
from agent.transports.types import NormalizedResponse, ToolCall


pytestmark = pytest.mark.characterization


# ===========================================================================
# _repair_tool_call_arguments
# ===========================================================================


class TestRepairToolCallArguments:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("", "{}"),                                   # empty -> {}
            ("   ", "{}"),                                # whitespace -> {}
            ("None", "{}"),                               # python None literal
            ('{"a": 1}', '{"a": 1}'),                     # already valid -> unchanged
            ('{"a": 1,}', '{"a": 1}'),                    # trailing comma stripped
            ('{"a": 1', '{"a": 1}'),                      # unclosed object closed
            # Single-pass closer-append produces '{"a": [1, 2}]' (curly then
            # bracket) which is invalid JSON; the excess-trimmer can't recover
            # it, so the real behavior falls through to '{}'.  PIN that.
            ('{"a": [1, 2', "{}"),
            # Array already closed -> appending one '}' yields valid JSON.
            ('{"a": [1, 2]', '{"a": [1, 2]}'),
            ('{"a": 1}}}', '{"a": 1}'),                   # excess closers trimmed
            ("not json at all", "{}"),                    # unrepairable -> {}
        ],
    )
    def test_repairs(self, raw, expected):
        assert run_agent._repair_tool_call_arguments(raw, "tool") == expected


# ===========================================================================
# _should_parallelize_tool_batch / _paths_overlap
# ===========================================================================


def _tc(name: str, arguments):
    if isinstance(arguments, (dict, list)):
        arguments = json.dumps(arguments)
    return SimpleNamespace(
        id=f"id_{name}",
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class TestShouldParallelizeToolBatch:
    def test_single_call_never_parallel(self):
        assert run_agent._should_parallelize_tool_batch([_tc("read_file", {"path": "a"})]) is False

    def test_two_readonly_web_search_parallel(self):
        batch = [_tc("web_search", {"q": "a"}), _tc("web_search", {"q": "b"})]
        assert run_agent._should_parallelize_tool_batch(batch) is True

    def test_two_reads_distinct_paths_parallel(self):
        batch = [_tc("read_file", {"path": "/tmp/a.txt"}), _tc("read_file", {"path": "/tmp/b.txt"})]
        assert run_agent._should_parallelize_tool_batch(batch) is True

    def test_two_reads_overlapping_paths_sequential(self):
        batch = [_tc("read_file", {"path": "/tmp/dir"}), _tc("read_file", {"path": "/tmp/dir/inner.txt"})]
        assert run_agent._should_parallelize_tool_batch(batch) is False

    def test_never_parallel_tool_forces_sequential(self):
        batch = [_tc("clarify", {"question": "?"}), _tc("web_search", {"q": "a"})]
        assert run_agent._should_parallelize_tool_batch(batch) is False

    def test_unknown_tool_in_batch_sequential(self):
        batch = [_tc("web_search", {"q": "a"}), _tc("terminal", {"cmd": "ls"})]
        assert run_agent._should_parallelize_tool_batch(batch) is False

    def test_unparseable_args_sequential(self):
        batch = [_tc("read_file", "{not json"), _tc("read_file", {"path": "/tmp/b"})]
        assert run_agent._should_parallelize_tool_batch(batch) is False


class TestPathsOverlap:
    def test_identical(self):
        assert run_agent._paths_overlap(Path("/a/b"), Path("/a/b")) is True

    def test_parent_child(self):
        assert run_agent._paths_overlap(Path("/a"), Path("/a/b/c")) is True

    def test_siblings_no_overlap(self):
        assert run_agent._paths_overlap(Path("/a/b"), Path("/a/c")) is False

    def test_disjoint_roots(self):
        assert run_agent._paths_overlap(Path("/x/y"), Path("/p/q")) is False


# ===========================================================================
# _deduplicate_tool_calls
# ===========================================================================


class TestDeduplicateToolCalls:
    def test_removes_exact_duplicate(self):
        batch = [_tc("read_file", {"path": "a"}), _tc("read_file", {"path": "a"})]
        out = AIAgent._deduplicate_tool_calls(batch)
        assert len(out) == 1
        assert out[0].function.name == "read_file"

    def test_keeps_distinct_arguments(self):
        batch = [_tc("read_file", {"path": "a"}), _tc("read_file", {"path": "b"})]
        out = AIAgent._deduplicate_tool_calls(batch)
        assert len(out) == 2

    def test_no_dupes_returns_same_object(self):
        batch = [_tc("read_file", {"path": "a"}), _tc("web_search", {"q": "x"})]
        out = AIAgent._deduplicate_tool_calls(batch)
        assert out is batch  # identity preserved when nothing removed

    def test_keeps_first_occurrence_order(self):
        batch = [
            _tc("a", {"x": 1}),
            _tc("b", {"y": 2}),
            _tc("a", {"x": 1}),  # dup of first
        ]
        out = AIAgent._deduplicate_tool_calls(batch)
        assert [tc.function.name for tc in out] == ["a", "b"]


# ===========================================================================
# _cap_delegate_task_calls
# ===========================================================================


class TestCapDelegateTaskCalls:
    def test_non_delegate_calls_pass_through(self):
        batch = [_tc("read_file", {"path": "a"}), _tc("web_search", {"q": "x"})]
        out = AIAgent._cap_delegate_task_calls(batch)
        assert out is batch  # no delegate -> untouched

    def test_excess_delegates_truncated_preserving_others(self, monkeypatch):
        # Pin the cap deterministically regardless of config/env.
        monkeypatch.setattr(
            "tools.delegate_tool._get_max_concurrent_children", lambda: 2
        )
        batch = [
            _tc("delegate_task", {"goal": "1"}),
            _tc("read_file", {"path": "keep"}),
            _tc("delegate_task", {"goal": "2"}),
            _tc("delegate_task", {"goal": "3"}),  # excess
        ]
        out = AIAgent._cap_delegate_task_calls(batch)
        names = [tc.function.name for tc in out]
        assert names.count("delegate_task") == 2
        assert "read_file" in names
        # Non-delegate ordering preserved.
        assert names == ["delegate_task", "read_file", "delegate_task"]

    def test_under_cap_untouched(self, monkeypatch):
        monkeypatch.setattr(
            "tools.delegate_tool._get_max_concurrent_children", lambda: 5
        )
        batch = [_tc("delegate_task", {"goal": "1"}), _tc("delegate_task", {"goal": "2"})]
        out = AIAgent._cap_delegate_task_calls(batch)
        assert out is batch


# ===========================================================================
# _sanitize_api_messages  (static; orphan tool-call/result repair)
# ===========================================================================


class TestSanitizeApiMessages:
    def test_drops_invalid_role(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "bogus", "content": "drop me"},
            {"role": "assistant", "content": "ok"},
        ]
        out = AIAgent._sanitize_api_messages(msgs)
        assert [m["role"] for m in out] == ["user", "assistant"]

    def test_drops_orphaned_tool_result(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "ghost", "content": "no parent"},
        ]
        out = AIAgent._sanitize_api_messages(msgs)
        assert all(m.get("role") != "tool" for m in out)

    def test_injects_stub_for_missing_result(self):
        msgs = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "type": "function",
                                "function": {"name": "read_file", "arguments": "{}"}}],
            },
        ]
        out = AIAgent._sanitize_api_messages(msgs)
        tool_msgs = [m for m in out if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "call_1"
        assert "unavailable" in tool_msgs[0]["content"].lower()

    def test_matched_pair_preserved(self):
        msgs = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "type": "function",
                                "function": {"name": "read_file", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ]
        out = AIAgent._sanitize_api_messages(msgs)
        assert out == msgs  # nothing dropped or injected


# ===========================================================================
# _get_tool_call_id_static
# ===========================================================================


class TestGetToolCallIdStatic:
    def test_dict_form(self):
        assert AIAgent._get_tool_call_id_static({"id": "abc"}) == "abc"

    def test_object_form(self):
        assert AIAgent._get_tool_call_id_static(SimpleNamespace(id="xyz")) == "xyz"

    def test_missing_id_returns_empty(self):
        assert AIAgent._get_tool_call_id_static({}) == ""
        assert AIAgent._get_tool_call_id_static(SimpleNamespace()) == ""


# ===========================================================================
# IterationBudget
# ===========================================================================


class TestIterationBudget:
    def test_consume_until_exhausted(self):
        b = IterationBudget(2)
        assert b.remaining == 2
        assert b.consume() is True
        assert b.consume() is True
        assert b.consume() is False  # exhausted
        assert b.remaining == 0
        assert b.used == 2

    def test_refund_restores_one(self):
        b = IterationBudget(1)
        assert b.consume() is True
        assert b.remaining == 0
        b.refund()
        assert b.remaining == 1
        assert b.used == 0

    def test_refund_floor_at_zero(self):
        b = IterationBudget(3)
        b.refund()  # nothing used yet
        assert b.used == 0
        assert b.remaining == 3


# ===========================================================================
# ChatCompletionsTransport.normalize_response
# ===========================================================================


def _cc_response(content, finish_reason, tool_calls=None, reasoning=None,
                 reasoning_content=None, usage=None):
    msg = SimpleNamespace(
        content=content,
        reasoning=reasoning,
        reasoning_content=reasoning_content,
        tool_calls=tool_calls,
    )
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    resp = SimpleNamespace(choices=[choice])
    resp.usage = SimpleNamespace(**usage) if usage else None
    return resp


class TestChatCompletionsNormalize:
    def _transport(self):
        from agent.transports.chat_completions import ChatCompletionsTransport
        return ChatCompletionsTransport()

    def test_plain_text(self):
        nr = self._transport().normalize_response(
            _cc_response("hello world", "stop")
        )
        assert nr.content == "hello world"
        assert nr.tool_calls is None
        assert nr.finish_reason == "stop"
        assert nr.reasoning is None

    def test_single_tool_call(self):
        tc = SimpleNamespace(
            id="call_1", type="function",
            function=SimpleNamespace(name="read_file", arguments='{"path": "README.md"}'),
        )
        nr = self._transport().normalize_response(
            _cc_response(None, "tool_calls", tool_calls=[tc])
        )
        assert nr.content is None
        assert nr.finish_reason == "tool_calls"
        assert len(nr.tool_calls) == 1
        only = nr.tool_calls[0]
        assert only.id == "call_1"
        assert only.name == "read_file"
        assert only.arguments == '{"path": "README.md"}'
        # Backward-compat accessors used across the loop.
        assert only.function.name == "read_file"
        assert only.type == "function"

    def test_finish_reason_none_defaults_to_stop(self):
        nr = self._transport().normalize_response(_cc_response("x", None))
        assert nr.finish_reason == "stop"

    def test_usage_and_reasoning_preserved(self):
        nr = self._transport().normalize_response(
            _cc_response(
                "answer", "stop",
                reasoning="because",
                reasoning_content="deepseek-style",
                usage={"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15},
            )
        )
        assert nr.reasoning == "because"
        assert nr.usage.prompt_tokens == 11
        assert nr.usage.completion_tokens == 4
        assert nr.usage.total_tokens == 15
        # reasoning_content lands in provider_data and is exposed via property.
        assert nr.reasoning_content == "deepseek-style"

    def test_validate_response(self):
        t = self._transport()
        assert t.validate_response(_cc_response("x", "stop")) is True
        assert t.validate_response(None) is False
        assert t.validate_response(SimpleNamespace(choices=[])) is False


# ===========================================================================
# AnthropicTransport.normalize_response + map_finish_reason
# ===========================================================================


class TestAnthropicNormalize:
    def _transport(self):
        from agent.transports.anthropic import AnthropicTransport
        return AnthropicTransport()

    def test_text_and_tool_use_blocks(self):
        resp = SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="Sure."),
                SimpleNamespace(type="tool_use", id="toolu_1", name="read_file",
                                input={"path": "x"}),
            ],
            stop_reason="tool_use",
        )
        nr = self._transport().normalize_response(resp)
        assert nr.content == "Sure."
        assert nr.finish_reason == "tool_calls"
        assert len(nr.tool_calls) == 1
        assert nr.tool_calls[0].id == "toolu_1"
        assert nr.tool_calls[0].name == "read_file"
        assert json.loads(nr.tool_calls[0].arguments) == {"path": "x"}

    def test_text_only_end_turn(self):
        resp = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Done.")],
            stop_reason="end_turn",
        )
        nr = self._transport().normalize_response(resp)
        assert nr.content == "Done."
        assert nr.finish_reason == "stop"
        assert nr.tool_calls is None

    @pytest.mark.parametrize(
        "raw,mapped",
        [
            ("end_turn", "stop"),
            ("tool_use", "tool_calls"),
            ("max_tokens", "length"),
            ("stop_sequence", "stop"),
            ("refusal", "content_filter"),
            ("model_context_window_exceeded", "length"),
            ("totally_unknown", "stop"),
        ],
    )
    def test_map_finish_reason(self, raw, mapped):
        assert self._transport().map_finish_reason(raw) == mapped


# ===========================================================================
# ResponsesApiTransport (Codex) — normalize_response + map_finish_reason
# ===========================================================================


class TestCodexNormalize:
    def _transport(self):
        from agent.transports.codex import ResponsesApiTransport
        return ResponsesApiTransport()

    def test_normalize_message_and_function_call(self):
        # Responses API output: a message item + a function_call item.
        resp = SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="message",
                    role="assistant",
                    content=[SimpleNamespace(type="output_text", text="Working on it.")],
                ),
                SimpleNamespace(
                    type="function_call",
                    id="fc_1",
                    call_id="call_abc",
                    name="read_file",
                    arguments='{"path": "x"}',
                ),
            ],
            status="completed",
        )
        nr = self._transport().normalize_response(resp)
        assert "Working on it." in (nr.content or "")
        assert nr.tool_calls is not None and len(nr.tool_calls) == 1
        tc = nr.tool_calls[0]
        assert tc.name == "read_file"
        assert json.loads(tc.arguments) == {"path": "x"}
        # Codex call_id lands in provider_data and is exposed via property.
        assert tc.call_id == "call_abc"

    @pytest.mark.parametrize(
        "raw,mapped",
        [
            ("completed", "stop"),
            ("incomplete", "length"),
            ("failed", "stop"),
            ("cancelled", "stop"),
            ("???", "stop"),
        ],
    )
    def test_map_finish_reason(self, raw, mapped):
        assert self._transport().map_finish_reason(raw) == mapped


# ===========================================================================
# BedrockTransport.normalize_response + map_finish_reason
# ===========================================================================


class TestBedrockNormalize:
    def _transport(self):
        from agent.transports.bedrock import BedrockTransport
        return BedrockTransport()

    def test_normalize_already_normalized_namespace(self):
        # The dispatch-site shape: OpenAI-compatible SimpleNamespace with .choices.
        tc = SimpleNamespace(
            id="tc1", type="function",
            function=SimpleNamespace(name="read_file", arguments='{"path": "x"}'),
        )
        msg = SimpleNamespace(content="hi", tool_calls=[tc], reasoning=None)
        choice = SimpleNamespace(message=msg, finish_reason="tool_calls")
        resp = SimpleNamespace(
            choices=[choice],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5),
        )
        nr = self._transport().normalize_response(resp)
        assert nr.content == "hi"
        assert nr.finish_reason == "tool_calls"
        assert nr.tool_calls[0].name == "read_file"
        assert nr.usage.total_tokens == 5

    @pytest.mark.parametrize(
        "raw,mapped",
        [
            ("end_turn", "stop"),
            ("tool_use", "tool_calls"),
            ("max_tokens", "length"),
        ],
    )
    def test_map_finish_reason(self, raw, mapped):
        assert self._transport().map_finish_reason(raw) == mapped


# ===========================================================================
# _build_assistant_message  (instance method; minimal agent via __new__)
# ===========================================================================


class TestBuildAssistantMessage:
    """``_build_assistant_message`` is an instance method but only touches a
    handful of attributes.  We build a bare instance via ``__new__`` and set
    just those, avoiding the full (heavy) constructor.  This pins the
    normalized assistant-message dict shape that the refactor must preserve.
    """

    def _bare_agent(self):
        agent = AIAgent.__new__(AIAgent)
        agent.verbose_logging = False
        agent.reasoning_callback = None
        agent.stream_delta_callback = None
        agent._stream_callback = None
        return agent

    def test_plain_text_message(self):
        agent = self._bare_agent()
        nr = NormalizedResponse(
            content="Hello.", tool_calls=None, finish_reason="stop",
        )
        msg = agent._build_assistant_message(nr, "stop")
        assert msg["role"] == "assistant"
        assert msg["content"] == "Hello."
        assert msg["finish_reason"] == "stop"
        assert "tool_calls" not in msg

    def test_tool_call_message_shape(self):
        agent = self._bare_agent()
        tc = ToolCall(id="call_1", name="read_file", arguments='{"path": "x"}')
        nr = NormalizedResponse(
            content=None, tool_calls=[tc], finish_reason="tool_calls",
        )
        msg = agent._build_assistant_message(nr, "tool_calls")
        assert msg["role"] == "assistant"
        assert msg["tool_calls"][0]["id"] == "call_1"
        assert msg["tool_calls"][0]["type"] == "function"
        assert msg["tool_calls"][0]["function"] == {
            "name": "read_file",
            "arguments": '{"path": "x"}',
        }

    def test_strips_inline_think_block_from_content(self):
        agent = self._bare_agent()
        nr = NormalizedResponse(
            content="<think>secret reasoning</think>Visible answer.",
            tool_calls=None,
            finish_reason="stop",
        )
        msg = agent._build_assistant_message(nr, "stop")
        assert "secret reasoning" not in msg["content"]
        assert msg["content"] == "Visible answer."
        # Reasoning was extracted into the reasoning field.
        assert msg["reasoning"] == "secret reasoning"
