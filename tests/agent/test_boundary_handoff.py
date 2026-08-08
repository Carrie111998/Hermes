"""Regression coverage for incomplete-task boundary handoffs (#80772).

Covers both implementation and planning shapes: remaining todos + a vague
progress summary is an invalid terminal state; a self-contained handoff or
clear completion claim is allowed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.boundary_handoff import (
    build_boundary_handoff_nudge,
    has_remaining_work,
    is_valid_terminal_for_remaining_work,
    remaining_todo_items,
    response_has_blocker_or_clarification,
    response_has_completion_claim,
    turn_called_clarify,
)
from tools.todo_tool import TodoStore


def _store(*items):
    store = TodoStore()
    store.write(
        [
            {"id": str(i), "content": content, "status": status}
            for i, (content, status) in enumerate(items, start=1)
        ]
    )
    return store


# ── Policy unit tests ────────────────────────────────────────────────


def test_remaining_work_from_active_todos_only():
    store = _store(
        ("done piece", "completed"),
        ("still open", "pending"),
        ("cancelled piece", "cancelled"),
        ("active now", "in_progress"),
    )
    remaining = remaining_todo_items(store)
    assert has_remaining_work(store) is True
    assert [item["content"] for item in remaining] == ["still open", "active now"]


def test_no_remaining_work_when_todos_settled():
    store = _store(("a", "completed"), ("b", "cancelled"))
    assert has_remaining_work(store) is False
    assert build_boundary_handoff_nudge(todo_store=store, final_response="summary") is None


def test_partial_progress_summary_is_invalid_terminal():
    text = (
        "I finished the skill-local gate and verified the unit tests. "
        "The scheduler integration is next."
    )
    assert response_has_completion_claim(text) is False
    assert response_has_blocker_or_clarification(text) is False
    assert is_valid_terminal_for_remaining_work(text) is False


def test_implementation_boundary_handoff_is_valid_terminal():
    text = (
        "Completed and verified the skill-local gate. Remaining work is the "
        "scripts-tree scheduler write outside this workspace boundary. No live "
        "or external state changed. Should I proceed with that write?"
    )
    assert response_has_blocker_or_clarification(text) is True
    assert is_valid_terminal_for_remaining_work(text) is True


def test_clear_completion_claim_is_valid_terminal():
    text = "The implementation is complete. All requested work is done."
    assert response_has_completion_claim(text) is True
    assert is_valid_terminal_for_remaining_work(text) is True


def test_planning_partial_summary_nudges():
    store = _store(
        ("outline API shape", "completed"),
        ("choose storage backend", "pending"),
        ("write rollout plan", "pending"),
    )
    partial = (
        "I outlined the API shape. Next I will choose a storage backend and "
        "draft the rollout plan."
    )
    nudge = build_boundary_handoff_nudge(
        todo_store=store,
        final_response=partial,
        attempts=0,
    )
    assert nudge is not None
    assert "Outstanding items" in nudge
    assert "choose storage backend" in nudge
    assert "decision or authorization" in nudge


def test_planning_valid_pause_does_not_nudge():
    store = _store(
        ("outline API shape", "completed"),
        ("choose storage backend", "pending"),
    )
    handoff = (
        "Completed the API outline. Remaining work is choosing the storage "
        "backend — that needs your approval before I continue. No external "
        "state changed. Should I use Postgres or SQLite?"
    )
    assert (
        build_boundary_handoff_nudge(
            todo_store=store,
            final_response=handoff,
            attempts=0,
        )
        is None
    )


def test_implementation_partial_summary_nudges():
    store = _store(
        ("skill-local gate", "completed"),
        ("scheduler scripts write", "pending"),
    )
    partial = (
        "Implemented and tested the skill-local gate. Stopping here for now."
    )
    nudge = build_boundary_handoff_nudge(
        todo_store=store,
        final_response=partial,
        attempts=0,
    )
    assert nudge is not None
    assert "scheduler scripts write" in nudge
    assert "self-contained" in nudge.lower() or "actionable" in nudge.lower()


def test_clarify_tool_already_asked_skips_nudge():
    store = _store(("remaining step", "pending"))
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "clarify", "arguments": "{}"},
                }
            ],
        }
    ]
    assert turn_called_clarify(messages) is True
    assert (
        build_boundary_handoff_nudge(
            todo_store=store,
            final_response="Waiting.",
            messages=messages,
        )
        is None
    )


def test_nudge_budget_and_disable_gates():
    store = _store(("remaining step", "pending"))
    partial = "Finished the first half."
    assert (
        build_boundary_handoff_nudge(
            todo_store=store,
            final_response=partial,
            attempts=2,
        )
        is None
    )
    assert (
        build_boundary_handoff_nudge(
            todo_store=store,
            final_response=partial,
            attempts=0,
            enabled=False,
        )
        is None
    )


# ── Conversation-loop integration ────────────────────────────────────


def _response(content: str):
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="test/model",
        usage=None,
    )


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")
    from run_agent import AIAgent

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        instance = AIAgent(
            session_id="boundary-handoff-test",
            api_key="test-key",
            base_url="https://example.invalid/v1",
            provider="openai-compat",
            model="test/model",
            max_iterations=3,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    instance._cached_system_prompt = "stable test prompt"
    instance._session_db = None
    instance._session_json_enabled = False
    instance.save_trajectories = False
    instance.compression_enabled = False
    instance._task_completion_guidance = True
    instance._cleanup_task_resources = lambda *_a, **_kw: None
    instance._save_trajectory = lambda *_a, **_kw: None
    instance._todo_store = _store(
        ("skill-local gate", "completed"),
        ("scheduler scripts write", "pending"),
    )
    return instance


def test_loop_rejects_ambiguous_stop_then_accepts_handoff(agent):
    partial = (
        "I finished the skill-local gate and verified tests. "
        "Scheduler integration is still pending."
    )
    handoff = (
        "Completed and verified the skill-local gate. Remaining work is the "
        "scheduler scripts write outside this repository boundary. No live or "
        "external state changed. Should I proceed with that write?"
    )
    replies = [_response(partial), _response(handoff)]

    def model_call(_api_kwargs):
        return replies.pop(0)

    agent._interruptible_api_call = model_call

    with (
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("implement the dual-repo plan")

    assert result["final_response"] == handoff
    assert agent._boundary_handoff_nudges == 1
    # Synthetic nudge stripped from returned history; assistant handoff remains.
    roles = [message["role"] for message in result["messages"]]
    assert roles[0] == "user"
    assert "assistant" in roles
    assert not any(
        isinstance(message, dict) and message.get("_boundary_handoff_synthetic")
        for message in result["messages"]
    )


def test_loop_accepts_valid_boundary_pause_without_nudge(agent):
    handoff = (
        "Completed and verified the skill-local gate. Remaining work is the "
        "scheduler scripts write at a workspace boundary. No external state "
        "changed. Do you want me to continue with that write?"
    )
    agent._interruptible_api_call = lambda _kwargs: _response(handoff)

    with (
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("implement the dual-repo plan")

    assert result["final_response"] == handoff
    assert getattr(agent, "_boundary_handoff_nudges", 0) == 0
