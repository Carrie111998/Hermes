"""Regression test: Mistral rejects a user turn straight after a tool result.

Mistral answers HTTP 400 ``Unexpected role 'user' after role 'tool'`` when a
``user`` message directly follows a ``tool`` result. The shape is produced
whenever the user redirects before the model gets its continuation turn, and
``_repair_message_sequence`` deliberately preserves it in the stored history
(the pattern is valid, and rewinding it would drop the user's message), so the
adjacency reaches the wire on every subsequent request and the session stays
stuck until it scrolls out of context.

The repair is wire-only and provider-gated: the per-call copy gets a minimal
assistant turn inserted between the tool result and the user turn, and only
when the *active* endpoint is one that rejects the adjacency.

Refs #20154.
"""

from __future__ import annotations

import pytest

from agent.message_sanitization import TOOL_TO_USER_BRIDGE_CONTENT
from run_agent import AIAgent


def _make_agent(provider: str = "", model: str = "", base_url: str = "") -> AIAgent:
    agent = object.__new__(AIAgent)
    agent.provider = provider
    agent.model = model
    agent.base_url = base_url
    return agent


def _history_with_tool_then_user() -> list[dict]:
    """The reported shape: tool result, then the user redirecting."""
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "find the config"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "search_files", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "found 3 files"},
        {"role": "user", "content": "actually, check the other dir"},
    ]


def _tool_then_user_indexes(messages: list[dict]) -> list[int]:
    return [
        idx for idx in range(len(messages) - 1)
        if messages[idx].get("role") == "tool"
        and messages[idx + 1].get("role") == "user"
    ]


def test_mistral_wire_copy_gets_an_assistant_turn_between_tool_and_user():
    agent = _make_agent(
        provider="nvidia",
        model="mistralai/mistral-small-4-119b-2603",
        base_url="https://integrate.api.nvidia.com/v1",
    )
    api_messages = _history_with_tool_then_user()
    assert _tool_then_user_indexes(api_messages) == [3]

    bridged = agent._bridge_tool_to_user_for_provider(api_messages)

    assert bridged == 1
    assert _tool_then_user_indexes(api_messages) == []
    assert [m["role"] for m in api_messages] == [
        "system", "user", "assistant", "tool", "assistant", "user",
    ]
    assert api_messages[4] == {
        "role": "assistant", "content": TOOL_TO_USER_BRIDGE_CONTENT,
    }
    # The user's redirect survives — bridging inserts, never rewinds.
    assert api_messages[5]["content"] == "actually, check the other dir"


def test_lenient_provider_wire_copy_is_untouched():
    agent = _make_agent(
        provider="openai", model="gpt-5", base_url="https://api.openai.com/v1",
    )
    api_messages = _history_with_tool_then_user()
    before = [dict(m) for m in api_messages]

    assert agent._bridge_tool_to_user_for_provider(api_messages) == 0
    assert api_messages == before


def test_bridging_is_idempotent_across_retry_attempts():
    agent = _make_agent(provider="mistral", model="mistral-large",
                        base_url="https://api.mistral.ai/v1")
    api_messages = _history_with_tool_then_user()

    assert agent._bridge_tool_to_user_for_provider(api_messages) == 1
    after_first = [dict(m) for m in api_messages]

    assert agent._bridge_tool_to_user_for_provider(api_messages) == 0
    assert api_messages == after_first


def test_every_adjacency_in_a_long_history_is_bridged():
    agent = _make_agent(provider="mistral", model="mistral-large",
                        base_url="https://api.mistral.ai/v1")
    api_messages = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "r1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "t2", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t2", "content": "r2"},
        {"role": "user", "content": "q3"},
    ]

    assert agent._bridge_tool_to_user_for_provider(api_messages) == 2
    assert _tool_then_user_indexes(api_messages) == []


def test_tool_followed_by_assistant_is_left_alone():
    """The normal completed turn must not grow a bridge."""
    agent = _make_agent(provider="mistral", model="mistral-large",
                        base_url="https://api.mistral.ai/v1")
    api_messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "r"},
        {"role": "assistant", "content": "here you go"},
        {"role": "user", "content": "thanks"},
    ]
    before = [dict(m) for m in api_messages]

    assert agent._bridge_tool_to_user_for_provider(api_messages) == 0
    assert api_messages == before


def test_stored_history_keeps_the_adjacency():
    """Only the wire copy is repaired — the persisted shape is still valid."""
    agent = _make_agent(provider="mistral", model="mistral-large",
                        base_url="https://api.mistral.ai/v1")
    history = _history_with_tool_then_user()
    api_messages = [dict(m) for m in history]

    agent._bridge_tool_to_user_for_provider(api_messages)

    assert _tool_then_user_indexes(history) == [3]
    assert len(history) == 5


def test_pre_send_pipeline_yields_a_mistral_legal_payload():
    """The full pre-send chain must leave no tool->user on the wire.

    ``sanitize_api_messages`` alone deliberately preserves the adjacency (it
    is legal everywhere else, and rewinding it would drop the user's turn) —
    that is exactly why the provider-gated bridge runs after it.
    """
    from agent.agent_runtime_helpers import sanitize_api_messages

    agent = _make_agent(provider="mistral", model="mistral-large",
                        base_url="https://api.mistral.ai/v1")

    sanitized = sanitize_api_messages(_history_with_tool_then_user())
    assert _tool_then_user_indexes(sanitized) == [3]

    agent._bridge_tool_to_user_for_provider(sanitized)
    assert _tool_then_user_indexes(sanitized) == []


@pytest.mark.parametrize("provider,model,base_url", [
    ("mistral", "", ""),
    ("Mistral", "", ""),
    ("custom", "", "https://api.mistral.ai/v1"),
    ("nvidia", "mistralai/mistral-small-4-119b-2603", "https://integrate.api.nvidia.com/v1"),
    ("openrouter", "mistralai/mixtral-8x22b-instruct", "https://openrouter.ai/api/v1"),
    ("custom", "Devstral-Small-2603", "http://localhost:8000/v1"),
    ("custom", "magistral-medium", "http://localhost:8000/v1"),
])
def test_strict_endpoints_are_recognized(provider, model, base_url):
    agent = _make_agent(provider=provider, model=model, base_url=base_url)
    assert agent._bridge_tool_to_user_for_provider(
        _history_with_tool_then_user()) == 1


@pytest.mark.parametrize("provider,model,base_url", [
    ("openai", "gpt-5", "https://api.openai.com/v1"),
    ("anthropic", "claude-opus-5", "https://api.anthropic.com"),
    ("deepseek", "deepseek-v4", "https://api.deepseek.com"),
    ("openrouter", "qwen/qwen3-max", "https://openrouter.ai/api/v1"),
    ("", "", ""),
])
def test_lenient_endpoints_are_not_recognized(provider, model, base_url):
    agent = _make_agent(provider=provider, model=model, base_url=base_url)
    assert agent._bridge_tool_to_user_for_provider(
        _history_with_tool_then_user()) == 0
