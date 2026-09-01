"""Behavior contracts for the extracted per-iteration request projection."""

from __future__ import annotations

import copy
from types import SimpleNamespace

from agent.turn_request import PreparedTurnRequest, build_turn_request


def _agent():
    agent = SimpleNamespace(
        session_id="session-1",
        model="model-1",
        provider="openai",
        client=None,
        api_mode="chat_completions",
        ephemeral_system_prompt="ephemeral",
        prefill_messages=[
            {"role": "assistant", "content": [{"type": "text", "text": "prefill"}]}
        ],
        context_compressor=None,
        tools=[{"type": "function", "function": {"name": "read_file"}}],
        _use_prompt_caching=False,
    )
    agent._sanitize_tool_call_arguments = lambda *args, **kwargs: 0
    agent._copy_reasoning_content_for_api = lambda source, target: None
    agent._should_sanitize_tool_calls = lambda: False
    agent._sanitize_api_messages = lambda messages: messages
    agent._drop_thinking_only_and_merge_users = (
        lambda messages, **kwargs: messages
    )
    return agent


def test_projection_is_api_only_and_keeps_durable_history_unchanged():
    agent = _agent()
    history = [
        {
            "role": "user",
            "content": "clean transcript",
            "api_content": "exact wire bytes",
            "display_kind": "event",
            "display_metadata": {"source": "test"},
            "_row_id": 42,
        }
    ]
    snapshot = copy.deepcopy(history)

    prepared = build_turn_request(
        agent,
        history,
        current_turn_user_idx=0,
        active_system_prompt="stable system",
        ext_prefetch_cache="",
        plugin_user_context="",
        original_user_message="clean transcript",
        moa_config=None,
    )

    assert isinstance(prepared, PreparedTurnRequest)
    assert history == snapshot
    assert prepared.messages == snapshot
    assert prepared.tools_for_api is agent.tools
    assert prepared.api_messages[0] == {
        "role": "system",
        "content": "stable system\n\nephemeral",
    }
    assert prepared.api_messages[1]["content"][0]["text"] == "prefill"
    assert prepared.api_messages[2] == {
        "role": "user",
        "content": "exact wire bytes",
    }

    prepared.api_messages[1]["content"][0]["text"] = "changed"
    assert agent.prefill_messages[0]["content"][0]["text"] == "prefill"


def test_current_turn_context_is_composed_only_on_the_wire_copy():
    agent = _agent()
    agent.prefill_messages = []
    agent.ephemeral_system_prompt = ""
    history = [{"role": "user", "content": "question"}]

    prepared = build_turn_request(
        agent,
        history,
        current_turn_user_idx=0,
        active_system_prompt="",
        ext_prefetch_cache="memory",
        plugin_user_context="plugin context",
        original_user_message="question",
        moa_config=None,
    )

    assert history == [{"role": "user", "content": "question"}]
    wire_content = prepared.api_messages[0]["content"]
    assert wire_content.startswith("question\n\n")
    assert "memory" in wire_content
    assert wire_content.endswith("plugin context")
