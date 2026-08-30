"""The ACP text bridge is what makes Hermes' own tools reachable on an ACP provider.

ACP has no OpenAI ``tools``/``tool_calls`` channel, so ``memory``,
``skill_manage``, ``todo`` and friends only work if the schemas travel into the
prompt as text and the calls are parsed back out of the response text. These
tests pin both halves plus the streaming shape, and check that the in-tree
consumer (``agent/copilot_acp_client.py``) still produces the same prompt it did
when it owned a private copy of this code.
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent.acp_tool_bridge import (  # noqa: E402
    StreamChunks,
    LiveToolCallTextFilter,
    completion_to_stream_chunks,
    extract_acp_usage,
    extract_tool_calls_from_text,
    format_acp_tool_progress_line,
    hermes_tool_call_from_acp,
    parse_acp_tool_update,
    render_tool_bridge_sections,
    tool_specs_from_openai_tools,
)

_TOOLS = [
    {"type": "function", "function": {"name": "memory", "description": "d1", "parameters": {"a": 1}}},
    {"type": "function", "function": {"name": "read_file", "description": "d2", "parameters": {}}},
]


# ── prompt side ──────────────────────────────────────────────────────────────


def test_specs_are_flattened_and_malformed_entries_skipped():
    specs = tool_specs_from_openai_tools(
        [*_TOOLS, "junk", None, {"function": None}, {"function": {"name": "  "}}]
    )
    assert [s["name"] for s in specs] == ["memory", "read_file"]
    assert specs[0] == {"name": "memory", "description": "d1", "parameters": {"a": 1}}


def test_allowlist_forwards_only_the_named_tools():
    """An agent-as-provider runs its own read/edit tools; re-offering them would
    make Hermes re-run finished work, so those clients forward an allowlist."""
    specs = tool_specs_from_openai_tools(_TOOLS, allowlist=["memory"])
    assert [s["name"] for s in specs] == ["memory"]
    # No allowlist at all means "forward everything" — not "forward nothing".
    assert len(tool_specs_from_openai_tools(_TOOLS)) == 2
    assert tool_specs_from_openai_tools(_TOOLS, allowlist=[]) == []


def test_rendered_sections_carry_the_contract_and_the_schemas():
    sections = render_tool_bridge_sections(_TOOLS, {"type": "function"})
    assert len(sections) == 2
    assert "<tool_call>" in sections[0]
    payload = json.loads(sections[0].split("\n", 1)[1])
    assert [s["name"] for s in payload] == ["memory", "read_file"]
    assert sections[1].startswith("Tool choice hint:")


def test_no_tools_and_no_choice_render_nothing():
    """Callers splice the result unconditionally, so it must be safe to be empty."""
    assert render_tool_bridge_sections(None) == []
    assert render_tool_bridge_sections([]) == []
    assert render_tool_bridge_sections([{"function": {}}]) == []


# ── response side ────────────────────────────────────────────────────────────


def test_tool_call_block_is_parsed_and_stripped_from_the_text():
    calls, cleaned = extract_tool_calls_from_text(
        'Sure.\n<tool_call>{"id": "c1", "type": "function", "function": '
        '{"name": "memory", "arguments": "{\\"action\\": \\"add\\"}"}}</tool_call>\nDone.'
    )
    assert [c.function.name for c in calls] == ["memory"]
    assert calls[0].id == "c1"
    assert json.loads(calls[0].function.arguments) == {"action": "add"}
    # The user must not see the raw JSON.
    assert "<tool_call>" not in cleaned
    assert cleaned == "Sure.\nDone."


def test_multiple_blocks_are_all_parsed():
    text = "".join(
        f'<tool_call>{{"id": "c{i}", "type": "function", '
        f'"function": {{"name": "todo", "arguments": "{{}}"}}}}</tool_call>'
        for i in range(3)
    )
    calls, cleaned = extract_tool_calls_from_text(text)
    assert [c.id for c in calls] == ["c0", "c1", "c2"]
    assert cleaned == ""


def test_non_string_arguments_are_json_encoded_and_missing_ids_synthesised():
    calls, _ = extract_tool_calls_from_text(
        '<tool_call>{"type": "function", "function": '
        '{"name": "todo", "arguments": {"op": "list"}}}</tool_call>'
    )
    assert calls[0].function.arguments == '{"op": "list"}'
    assert calls[0].id == "acp_call_1"


def test_bare_json_is_a_fallback_only_when_no_block_matched():
    bare = '{"id": "c9", "type": "function", "function": {"name": "memory", "arguments": "{}"}}'
    calls, cleaned = extract_tool_calls_from_text(f"before {bare} after")
    assert [c.id for c in calls] == ["c9"]
    assert cleaned == "before\nafter"

    # With a real block present the bare-JSON scan must not double-count.
    both = f'<tool_call>{bare}</tool_call> and {bare}'
    calls, _ = extract_tool_calls_from_text(both)
    assert len(calls) == 1


def test_malformed_and_empty_input_never_raises():
    assert extract_tool_calls_from_text("") == ([], "")
    assert extract_tool_calls_from_text(None) == ([], "")
    calls, cleaned = extract_tool_calls_from_text("<tool_call>{not json}</tool_call>plain")
    assert calls == []
    assert cleaned == "plain"
    # Well-formed JSON that isn't a tool call is ignored, text preserved.
    calls, cleaned = extract_tool_calls_from_text('<tool_call>{"function": 5}</tool_call>hi')
    assert calls == []
    assert cleaned == "hi"


# ── streaming shape ──────────────────────────────────────────────────────────


def _completion(**extras):
    message = SimpleNamespace(
        content="hello",
        tool_calls=[
            SimpleNamespace(
                id="c1", type="function",
                function=SimpleNamespace(name="memory", arguments="{}"),
            )
        ],
        reasoning=None,
        reasoning_content=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
        usage=SimpleNamespace(total_tokens=3),
        model="acp",
        **extras,
    )


def test_stream_chunks_carry_the_delta_then_the_usage():
    chunks = completion_to_stream_chunks(_completion())
    assert len(chunks) == 2
    delta = chunks[0].choices[0].delta
    assert delta.content == "hello"
    assert delta.tool_calls[0].function.name == "memory"
    assert delta.tool_calls[0].index == 0
    assert chunks[0].choices[0].finish_reason == "tool_calls"
    # Usage arrives on its own trailing chunk, as OpenAI does it.
    assert chunks[0].usage is None
    assert chunks[1].usage.total_tokens == 3
    assert chunks[1].choices == []


def test_response_level_extras_survive_the_stream_conversion():
    """Hermes reads provider extras off the returned object; a plain list would
    drop them and silently disable the projection on stream=True."""
    chunks = completion_to_stream_chunks(
        _completion(hermes_projected_messages=[{"role": "tool", "content": "x"}])
    )
    assert isinstance(chunks, StreamChunks)
    assert isinstance(chunks, list)
    assert chunks.hermes_projected_messages == [{"role": "tool", "content": "x"}]


def test_a_text_only_completion_streams_without_tool_call_deltas():
    completion = _completion()
    completion.choices[0].message.tool_calls = []
    completion.choices[0].finish_reason = "stop"
    chunks = completion_to_stream_chunks(completion)
    assert chunks[0].choices[0].delta.tool_calls is None


def test_live_filter_hides_xml_tool_calls_and_drops_post_call_ramble():
    """Codex never paints function-call JSON as assistant text. ACP must not either."""
    block = (
        '<tool_call>{"id": "c1", "type": "function", "function": '
        '{"name": "terminal", "arguments": "{\\"command\\": \\"pwd\\"}"}}</tool_call>'
    )
    filt = LiveToolCallTextFilter()
    assert filt.push("I'll check.\n<tool_") == "I'll check.\n"
    assert filt.push(block[len("<tool_") :] + "\nTools are not wired up.") == ""
    leftover, calls = filt.flush()
    assert leftover == ""
    assert [c.function.name for c in calls] == ["terminal"]
    assert json.loads(calls[0].function.arguments)["command"] == "pwd"


# ── the in-tree consumer still speaks the same wire ──────────────────────────


def test_copilot_prompt_still_carries_the_contract_and_the_tools():
    """copilot-acp lost its private copy of the bridge; its prompt must not
    change shape."""
    from agent.copilot_acp_client import _format_messages_as_prompt

    prompt = _format_messages_as_prompt(
        [{"role": "user", "content": "hi"}], model="gpt-5", tools=_TOOLS,
    )
    assert "<tool_call>{...}</tool_call>" in prompt
    assert '"name": "memory"' in prompt
    assert '"name": "read_file"' in prompt  # copilot forwards everything
    assert "Hermes requested model hint: gpt-5" in prompt
    assert "hi" in prompt


def test_copilot_prompt_omits_the_tool_section_when_there_are_no_tools():
    from agent.copilot_acp_client import _format_messages_as_prompt

    prompt = _format_messages_as_prompt([{"role": "user", "content": "hi"}])
    assert "Available tools" not in prompt
    assert "Tool choice hint" not in prompt


# ── live ACP progress helpers ────────────────────────────────────────────────


def test_parse_acp_execute_without_command_is_not_a_null_terminal_call():
    """Incomplete Kiro execute (title only, command=null) must not become a Hermes call."""
    parsed = parse_acp_tool_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-stub",
            "kind": "execute",
            "status": "in_progress",
            "title": "preparing",
            "rawInput": {"command": None},
        }
    )
    assert parsed is not None
    assert parsed["name"] == "terminal"
    assert "command" not in (parsed.get("args") or {})
    assert hermes_tool_call_from_acp(parsed) is None
    assert hermes_tool_call_from_acp(
        {"id": "tc-stub", "name": "terminal", "args": {"command": None}, "status": "in_progress"}
    ) is None
    assert hermes_tool_call_from_acp(
        {"id": "tc-stub", "name": "terminal", "args": {}, "status": "in_progress"}
    ) is None


def test_parse_acp_search_kind_with_shell_command_is_terminal():
    """kind=search / grep title must not steal a real shell command into search_files."""
    parsed = parse_acp_tool_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-ps",
            "kind": "search",
            "title": "grep",
            "status": "in_progress",
            "rawInput": {"command": "ps aux | grep foo"},
        }
    )
    assert parsed is not None
    assert parsed["name"] == "terminal"
    assert parsed["args"]["command"] == "ps aux | grep foo"
    assert "pattern" not in parsed["args"]
    call = hermes_tool_call_from_acp(parsed)
    assert call is not None
    assert call.function.name == "terminal"
    assert json.loads(call.function.arguments)["command"] == "ps aux | grep foo"


def test_parse_acp_grep_title_with_shell_command_is_terminal():
    parsed = parse_acp_tool_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-ps-title",
            "title": "grep",
            "status": "in_progress",
            "rawInput": {"command": "ps aux | grep foo"},
        }
    )
    assert parsed is not None
    assert parsed["name"] == "terminal"
    assert parsed["args"]["command"] == "ps aux | grep foo"


def test_parse_acp_search_pipeline_in_pattern_is_terminal():
    parsed = parse_acp_tool_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-ps-pat",
            "kind": "search",
            "title": "Running: ps aux | grep foo",
            "rawInput": {"pattern": "ps aux | grep foo"},
        }
    )
    assert parsed is not None
    assert parsed["name"] == "terminal"
    assert parsed["args"]["command"] == "ps aux | grep foo"
    assert "pattern" not in parsed["args"]


def test_parse_acp_search_kind_with_file_pattern_is_search_files():
    parsed = parse_acp_tool_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-sf",
            "kind": "search",
            "title": "grep",
            "rawInput": {"pattern": "TODO"},
        }
    )
    assert parsed is not None
    assert parsed["name"] == "search_files"
    assert parsed["args"]["pattern"] == "TODO"
    call = hermes_tool_call_from_acp(parsed)
    assert call is not None
    assert call.function.name == "search_files"


def test_parse_acp_search_regex_alternation_is_not_a_shell_pipeline():
    parsed = parse_acp_tool_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-alt",
            "kind": "search",
            "rawInput": {"pattern": "TODO|FIXME"},
        }
    )
    assert parsed is not None
    assert parsed["name"] == "search_files"
    assert parsed["args"]["pattern"] == "TODO|FIXME"


def test_parse_acp_search_title_only_is_not_ready():
    parsed = parse_acp_tool_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-grep-stub",
            "kind": "search",
            "title": "grep",
            "rawInput": {},
        }
    )
    assert parsed is not None
    assert parsed["name"] == "search_files"
    assert "pattern" not in (parsed.get("args") or {})


def test_parse_acp_execute_maps_to_terminal():
    parsed = parse_acp_tool_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-1",
            "title": "ls -la",
            "kind": "execute",
            "status": "in_progress",
            "rawInput": {"command": "ls -la"},
        }
    )
    assert parsed is not None
    assert parsed["name"] == "terminal"
    assert parsed["args"]["command"] == "ls -la"
    assert "💻 $" in format_acp_tool_progress_line(parsed)
    call = hermes_tool_call_from_acp(parsed)
    assert call is not None
    assert call.function.name == "terminal"
    assert json.loads(call.function.arguments)["command"] == "ls -la"
    done = dict(parsed)
    done["status"] = "completed"
    assert hermes_tool_call_from_acp(done) is None


def test_parse_acp_write_reads_kiro_file_text():
    parsed = parse_acp_tool_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-ft",
            "kind": "write",
            "locations": [{"path": "/tmp/out.txt"}],
            "rawInput": {"file_text": "hello kiro"},
        }
    )
    assert parsed is not None
    assert parsed["name"] == "write_file"
    assert parsed["args"]["content"] == "hello kiro"


def test_parse_acp_str_replace_command_is_patch_not_shell():
    parsed = parse_acp_tool_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-sr",
            "kind": "write",
            "locations": [{"path": "/tmp/out.txt"}],
            "rawInput": {
                "command": "str_replace",
                "old_str": "a",
                "new_str": "b",
            },
        }
    )
    assert parsed is not None
    assert parsed["name"] == "patch"
    assert parsed["args"]["old_string"] == "a"
    assert parsed["args"]["new_string"] == "b"
    assert "command" not in parsed["args"]


def test_parse_acp_write_maps_to_write_file_with_content():
    parsed = parse_acp_tool_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-w",
            "kind": "write",
            "locations": [{"path": "/tmp/out.txt"}],
            "rawInput": {"content": "hello"},
        }
    )
    assert parsed is not None
    assert parsed["name"] == "write_file"
    assert parsed["args"]["path"] == "/tmp/out.txt"
    assert parsed["args"]["content"] == "hello"


def test_parse_acp_delete_is_not_a_shell_command():
    assert parse_acp_tool_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-d",
            "kind": "delete",
            "title": "secret.env",
            "locations": [{"path": "/tmp/secret.env"}],
        }
    ) is None


def test_parse_acp_read_maps_to_read_file():
    parsed = parse_acp_tool_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-2",
            "kind": "read",
            "locations": [{"path": "/tmp/foo.py"}],
        }
    )
    assert parsed is not None
    assert parsed["name"] == "read_file"
    assert parsed["args"]["path"] == "/tmp/foo.py"
    assert "📖 read" in format_acp_tool_progress_line(parsed)


def test_extract_acp_usage_ignores_zeros_and_keeps_real_counts():
    assert extract_acp_usage({}) is None
    assert extract_acp_usage({"usage": {"promptTokens": 0, "outputTokens": 0}}) is None
    usage = extract_acp_usage({"usage": {"inputTokens": 1200, "outputTokens": 40}})
    assert usage is not None
    assert usage.prompt_tokens == 1200
    assert usage.completion_tokens == 40
    assert usage.total_tokens == 1240


def test_parse_acp_qmd_and_memory_names_map_to_hermes_tools():
    qmd = parse_acp_tool_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-q",
            "toolName": "mcp__qmd__query",
            "status": "in_progress",
            "rawInput": {"query": "int1"},
        }
    )
    assert qmd is not None
    assert qmd["name"] == "mcp__qmd__query"
    mem = parse_acp_tool_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-m",
            "toolName": "memory",
            "status": "in_progress",
            "rawInput": {"action": "add", "target": "user", "content": "x"},
        }
    )
    assert mem is not None
    assert mem["name"] == "memory"


def test_extract_acp_usage_reads_native_used_and_nested_update():
    native = extract_acp_usage(
        {"sessionUpdate": "usage_update", "used": 25000, "size": 1_000_000}
    )
    assert native is not None
    assert native.prompt_tokens == 25000
    assert native.total_tokens == 25000
    wrapped = extract_acp_usage(
        {
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "usage_update",
                "used": 1800,
                "size": 200_000,
            },
        }
    )
    assert wrapped is not None
    assert wrapped.prompt_tokens == 1800


def test_parse_acp_think_and_switch_mode_are_not_hermes_tools():
    assert parse_acp_tool_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-think",
            "kind": "think",
            "title": "planning",
            "rawInput": {"text": "hmm"},
        }
    ) is None
    assert parse_acp_tool_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-mode",
            "kind": "switch_mode",
            "title": "plan",
        }
    ) is None


def test_parse_acp_fetch_maps_to_web_extract():
    parsed = parse_acp_tool_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-f",
            "kind": "fetch",
            "title": "https://example.com",
            "rawInput": {"url": "https://example.com"},
        }
    )
    assert parsed is not None
    assert parsed["name"] == "web_extract"
    assert parsed["args"]["url"] == "https://example.com"


def test_parse_acp_tool_update_without_session_update_key():
    parsed = parse_acp_tool_update(
        {
            "toolCallId": "tc-flat",
            "kind": "execute",
            "title": "echo hi",
            "rawInput": {"command": "echo hi"},
        }
    )
    assert parsed is not None
    assert parsed["name"] == "terminal"
    assert "echo hi" in parsed["args"]["command"]


def test_coerce_session_update_accepts_params_level_payload():
    from agent.acp_stdio_transport import coerce_session_update

    updates = coerce_session_update(
        {
            "sessionId": "s1",
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-1",
            "kind": "read",
            "locations": [{"path": "/tmp/a.py"}],
        }
    )
    assert len(updates) == 1
    assert updates[0]["sessionUpdate"] == "tool_call"
