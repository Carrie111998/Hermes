"""Layer A — end-to-end agent-loop pinning (behavior-equivalence oracle).

Each scenario scripts the model turns, drives ``AIAgent.run_conversation`` via
the shared :class:`ScriptedClient` seam, and pins a full fingerprint
(``final_response``, ``completed``, ``partial``, ``api_calls``, the normalized
message sequence, and the per-call create() kwargs summary) against a golden.

These tests MUST pass against the CURRENT run_agent.py monolith — that is what
makes them a valid oracle for the upcoming ``agent/*`` refactor.  Goldens are
written on first run (sorted-key JSON) and asserted thereafter; re-pin an
intentional change with ``HERMES_CHAR_REGEN=1``.

Scenarios that are infeasible to wire cheaply against the current code
(provider seams that bypass the OpenAI-client symbol) are kept as ``xfail``
with a precise reason rather than deleted — their absence is itself a
documented finding.
"""

from __future__ import annotations

import json
from contextlib import ExitStack

import pytest

from tests.run_agent.characterization._harness import (
    make_agent,
    run_scenario,
    result_fingerprint,
    golden_compare,
)


pytestmark = pytest.mark.characterization


# ---------------------------------------------------------------------------
# Scenario 1 — No-tool plain response
# ---------------------------------------------------------------------------


def test_no_tool_plain_response():
    with ExitStack() as stack:
        agent = make_agent(
            script=[{"content": "The answer is 42.", "finish_reason": "stop"}],
            stack=stack,
        )
        result = run_scenario(agent, "what is the answer")

    # Behavioral asserts (in addition to the golden) so the contract is
    # legible even if the golden is regenerated.
    assert result["final_response"] == "The answer is 42."
    assert result["completed"] is True
    assert result["api_calls"] == 1

    fp = result_fingerprint(result, agent._characterization_client)
    golden_compare(fp, "01_no_tool_plain")


# ---------------------------------------------------------------------------
# Scenario 2 — Single tool-call turn, then text
# ---------------------------------------------------------------------------


def test_single_tool_call_then_text():
    with ExitStack() as stack:
        agent = make_agent(
            script=[
                {
                    "content": None,
                    "finish_reason": "tool_calls",
                    "tool_calls": [
                        {"id": "call_1", "name": "read_file",
                         "arguments": {"path": "README.md"}}
                    ],
                },
                {"content": "I read the file.", "finish_reason": "stop"},
            ],
            stack=stack,
            tool_names=["read_file"],
            tool_result=json.dumps({"ok": True, "content": "hello"}),
        )
        result = run_scenario(agent, "read the readme")

    assert result["final_response"] == "I read the file."
    assert result["completed"] is True
    assert result["api_calls"] == 2

    # Message ordering: assistant(tool_calls) -> tool result -> assistant text.
    roles = [m.get("role") for m in result["messages"]]
    assert "assistant" in roles and "tool" in roles
    # The tool result must come AFTER the assistant tool-call message.
    first_assistant_with_tc = next(
        i for i, m in enumerate(result["messages"])
        if m.get("role") == "assistant" and m.get("tool_calls")
    )
    first_tool = next(
        i for i, m in enumerate(result["messages"]) if m.get("role") == "tool"
    )
    assert first_assistant_with_tc < first_tool

    fp = result_fingerprint(result, agent._characterization_client)
    golden_compare(fp, "02_single_tool_then_text")


# ---------------------------------------------------------------------------
# Scenario 3a — Multi-tool single turn, PARALLEL-eligible (two distinct reads)
# ---------------------------------------------------------------------------


def test_multi_tool_single_turn_parallel():
    with ExitStack() as stack:
        agent = make_agent(
            script=[
                {
                    "content": None,
                    "finish_reason": "tool_calls",
                    "tool_calls": [
                        {"id": "call_a", "name": "read_file",
                         "arguments": {"path": "/tmp/a.txt"}},
                        {"id": "call_b", "name": "read_file",
                         "arguments": {"path": "/tmp/b.txt"}},
                    ],
                },
                {"content": "Read both files.", "finish_reason": "stop"},
            ],
            stack=stack,
            tool_names=["read_file"],
            tool_result=json.dumps({"ok": True}),
        )
        result = run_scenario(agent, "read both files")

    assert result["completed"] is True
    assert result["api_calls"] == 2
    # Both tool results present.
    tool_msgs = [m for m in result["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 2

    fp = result_fingerprint(result, agent._characterization_client)
    golden_compare(fp, "03a_multi_tool_parallel")


# ---------------------------------------------------------------------------
# Scenario 3b — Multi-tool single turn, SEQUENTIAL (overlapping paths)
# ---------------------------------------------------------------------------


def test_multi_tool_single_turn_overlapping_sequential():
    with ExitStack() as stack:
        agent = make_agent(
            script=[
                {
                    "content": None,
                    "finish_reason": "tool_calls",
                    "tool_calls": [
                        {"id": "call_a", "name": "read_file",
                         "arguments": {"path": "/tmp/dir"}},
                        {"id": "call_b", "name": "read_file",
                         "arguments": {"path": "/tmp/dir/inner.txt"}},
                    ],
                },
                {"content": "Read the dir then the file.", "finish_reason": "stop"},
            ],
            stack=stack,
            tool_names=["read_file"],
            tool_result=json.dumps({"ok": True}),
        )
        result = run_scenario(agent, "read overlapping paths")

    assert result["completed"] is True
    assert result["api_calls"] == 2
    tool_msgs = [m for m in result["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 2

    fp = result_fingerprint(result, agent._characterization_client)
    golden_compare(fp, "03b_multi_tool_overlapping_sequential")


# ---------------------------------------------------------------------------
# Scenario 4 — Multi-turn: tool -> tool -> text across 3 turns
# ---------------------------------------------------------------------------


def test_multi_turn_tool_tool_text():
    with ExitStack() as stack:
        agent = make_agent(
            script=[
                {
                    "content": None,
                    "finish_reason": "tool_calls",
                    "tool_calls": [
                        {"id": "call_1", "name": "read_file",
                         "arguments": {"path": "a"}}
                    ],
                },
                {
                    "content": None,
                    "finish_reason": "tool_calls",
                    "tool_calls": [
                        {"id": "call_2", "name": "read_file",
                         "arguments": {"path": "b"}}
                    ],
                },
                {"content": "Finished after two tools.", "finish_reason": "stop"},
            ],
            stack=stack,
            tool_names=["read_file"],
            tool_result=json.dumps({"ok": True}),
            max_iterations=4,
        )
        result = run_scenario(agent, "do two steps")

    assert result["final_response"] == "Finished after two tools."
    assert result["completed"] is True
    assert result["api_calls"] == 3
    # Two separate tool results accumulated.
    tool_msgs = [m for m in result["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 2

    fp = result_fingerprint(result, agent._characterization_client)
    golden_compare(fp, "04_multi_turn_tool_tool_text")


# ---------------------------------------------------------------------------
# Scenario 5 — Unknown/hallucinated tool name -> repair/invalid-tool path
# ---------------------------------------------------------------------------


def test_unknown_tool_name_repair_path():
    # The model emits a tool name not in valid_tool_names AND not fuzzy-matchable
    # (cutoff=0.7) to any real tool, so the invalid-tool branch fires: an error
    # tool-result is appended and the loop continues. The next turn returns text.
    with ExitStack() as stack:
        agent = make_agent(
            script=[
                {
                    "content": None,
                    "finish_reason": "tool_calls",
                    "tool_calls": [
                        {"id": "call_x", "name": "qzx_imaginary_9000",
                         "arguments": {}}
                    ],
                },
                {"content": "Recovered from the bad tool name.", "finish_reason": "stop"},
            ],
            stack=stack,
            tool_names=["read_file"],
        )
        result = run_scenario(agent, "call a fake tool")

    assert result["completed"] is True
    assert result["final_response"] == "Recovered from the bad tool name."
    assert result["api_calls"] == 2
    # An error tool-result mentioning the unavailable tool was injected.
    tool_msgs = [m for m in result["messages"] if m.get("role") == "tool"]
    assert any("does not exist" in (m.get("content") or "") for m in tool_msgs)

    fp = result_fingerprint(result, agent._characterization_client)
    golden_compare(fp, "05_unknown_tool_repair")


# ---------------------------------------------------------------------------
# Scenario 6 — Tool-error path (handle_function_call returns {"error": ...})
# ---------------------------------------------------------------------------


def test_tool_error_reaches_messages_loop_continues():
    with ExitStack() as stack:
        agent = make_agent(
            script=[
                {
                    "content": None,
                    "finish_reason": "tool_calls",
                    "tool_calls": [
                        {"id": "call_1", "name": "read_file",
                         "arguments": {"path": "missing"}}
                    ],
                },
                {"content": "Handled the tool error.", "finish_reason": "stop"},
            ],
            stack=stack,
            tool_names=["read_file"],
            # Tool returns a structured error; the loop appends it and continues.
            tool_result=json.dumps({"error": "file not found"}),
        )
        result = run_scenario(agent, "read a missing file")

    assert result["completed"] is True
    assert result["final_response"] == "Handled the tool error."
    assert result["api_calls"] == 2
    tool_msgs = [m for m in result["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "error" in (tool_msgs[0].get("content") or "").lower()

    fp = result_fingerprint(result, agent._characterization_client)
    golden_compare(fp, "06_tool_error_continues")


# ---------------------------------------------------------------------------
# Scenario 7 — Max-iterations stop -> summary path fires, completed=False
# ---------------------------------------------------------------------------


def test_max_iterations_summary_path():
    # max_iterations=2: the loop makes 2 tool-call API calls, exits the while,
    # then _handle_max_iterations makes ONE more (toolless) summary call.
    # completed = final_response is not None AND api_calls < max_iterations,
    # so api_calls==2 >= 2 => completed is False.
    with ExitStack() as stack:
        agent = make_agent(
            script=[
                {
                    "content": None,
                    "finish_reason": "tool_calls",
                    "tool_calls": [
                        {"id": "c1", "name": "read_file", "arguments": {"path": "a"}}
                    ],
                },
                {
                    "content": None,
                    "finish_reason": "tool_calls",
                    "tool_calls": [
                        {"id": "c2", "name": "read_file", "arguments": {"path": "b"}}
                    ],
                },
                # Summary call (tools stripped) — must be a plain text response.
                {"content": "Here is what I accomplished.", "finish_reason": "stop"},
            ],
            stack=stack,
            tool_names=["read_file"],
            tool_result=json.dumps({"ok": True}),
            max_iterations=2,
        )
        result = run_scenario(agent, "loop forever")

    assert result["completed"] is False
    assert result["api_calls"] == 2  # while-loop calls; summary call is separate
    assert result["final_response"] == "Here is what I accomplished."

    # The summary create() must have been issued with NO tools.
    calls = agent._characterization_client.calls
    assert len(calls) == 3, f"expected 3 create() calls, got {len(calls)}"
    assert calls[-1]["has_tools"] is False, "summary call must strip tools"

    fp = result_fingerprint(result, agent._characterization_client)
    golden_compare(fp, "07_max_iterations_summary")


# ---------------------------------------------------------------------------
# Scenario 8 — Empty-content response -> retry, then recover
# ---------------------------------------------------------------------------


def test_empty_content_then_retry_recovers():
    # finish_reason=stop with content=None and no tool calls, no reasoning, no
    # prior tool result, no fallback chain: the empty-response retry path fires
    # (_empty_content_retries < 3 -> continue). The retry turn returns text.
    with ExitStack() as stack:
        agent = make_agent(
            script=[
                {"content": None, "finish_reason": "stop", "tool_calls": []},
                {"content": "Recovered after an empty turn.", "finish_reason": "stop"},
            ],
            stack=stack,
            tool_names=["read_file"],
        )
        result = run_scenario(agent, "go")

    assert result["completed"] is True
    assert result["final_response"] == "Recovered after an empty turn."
    assert result["api_calls"] == 2

    fp = result_fingerprint(result, agent._characterization_client)
    golden_compare(fp, "08_empty_content_retry")


# ---------------------------------------------------------------------------
# Scenario 9 — api_mode=anthropic_messages single-tool (provider-branch parity)
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "Layer-A anthropic_messages routes through the native Anthropic SDK seam "
        "(_anthropic_messages_create), NOT the run_agent.OpenAI symbol the harness "
        "patches, so the loop blocks on a real client call. With no per-test "
        "timeout on Windows this HANGS rather than failing cleanly, which defeats "
        "xfail -- hence skip. chat_completions is the priority loop coverage; the "
        "Anthropic provider branch IS pinned at the unit level in "
        "test_pure_seams.py::TestAnthropicNormalize (normalize_response + "
        "map_finish_reason). Follow-up: add a second mock seam to characterize "
        "this loop branch end-to-end."
    ),
)
def test_anthropic_messages_single_tool():
    with ExitStack() as stack:
        agent = make_agent(
            script=[
                {
                    "content": None,
                    "finish_reason": "tool_calls",
                    "tool_calls": [
                        {"id": "call_1", "name": "read_file",
                         "arguments": {"path": "x"}}
                    ],
                },
                {"content": "done", "finish_reason": "stop"},
            ],
            stack=stack,
            tool_names=["read_file"],
            api_mode="anthropic_messages",
        )
        result = run_scenario(agent, "anthropic path")
    assert result["completed"] is True


# ---------------------------------------------------------------------------
# Scenario 10 — api_mode=codex_responses incomplete continuation
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "Layer-A codex_responses routes through _run_codex_stream (a streaming "
        "Responses-API path), not the chat.completions.create seam the harness "
        "scripts, so the loop blocks on a real call. With no per-test timeout on "
        "Windows this HANGS rather than failing cleanly, which defeats xfail -- "
        "hence skip. The Codex provider branch IS pinned at the unit level in "
        "test_pure_seams.py::TestCodexNormalize (normalize_response + "
        "map_finish_reason incl. incomplete->length). Follow-up: characterize this "
        "loop branch via the streaming seam."
    ),
)
def test_codex_responses_incomplete_continuation():
    with ExitStack() as stack:
        agent = make_agent(
            script=[
                {"content": "partial", "finish_reason": "incomplete"},
                {"content": "complete", "finish_reason": "stop"},
            ],
            stack=stack,
            tool_names=["read_file"],
            api_mode="codex_responses",
        )
        result = run_scenario(agent, "codex path")
    assert result["completed"] is True
