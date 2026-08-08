"""Regression coverage for incomplete-task boundary handoffs (#80772).

Structured evidence only: remaining todos + whether ``clarify`` ran this
turn. Freeform English (or any language) summaries are not a valid pause.
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


def _clarify_assistant():
    return {
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
    assert is_valid_terminal_for_remaining_work(todo_store=store) is True
    assert build_boundary_handoff_nudge(todo_store=store) is None


def test_partial_progress_summary_is_invalid_when_todos_remain():
    store = _store(("scheduler scripts write", "pending"))
    assert is_valid_terminal_for_remaining_work(todo_store=store, messages=[]) is False


def test_english_handoff_prose_alone_is_not_valid_terminal():
    """Prose handoffs are ignored — language-agnostic structured gate."""
    store = _store(("choose storage backend", "pending"))
    handoff = (
        "Completed the API outline. Remaining work is choosing the storage "
        "backend — that needs your approval before I continue. No external "
        "state changed. Should I use Postgres or SQLite?"
    )
    assert (
        build_boundary_handoff_nudge(
            todo_store=store,
            messages=[{"role": "assistant", "content": handoff}],
            attempts=0,
        )
        is not None
    )


def test_non_english_summary_also_nudges():
    store = _store(("scheduler scripts write", "pending"))
    nudge = build_boundary_handoff_nudge(
        todo_store=store,
        messages=[{"role": "assistant", "content": "技能侧门已完成，调度脚本待定。"}],
        attempts=0,
    )
    assert nudge is not None
    assert "clarify" in nudge
    assert "scheduler scripts write" in nudge


def test_planning_partial_summary_nudges():
    store = _store(
        ("outline API shape", "completed"),
        ("choose storage backend", "pending"),
        ("write rollout plan", "pending"),
    )
    nudge = build_boundary_handoff_nudge(
        todo_store=store,
        messages=[{"role": "assistant", "content": "Outlined the API. Next steps later."}],
        attempts=0,
    )
    assert nudge is not None
    assert "Outstanding items" in nudge
    assert "choose storage backend" in nudge
    assert "`clarify`" in nudge


def test_clarify_in_same_turn_skips_nudge_even_after_text_reply():
    store = _store(("remaining step", "pending"))
    messages = [
        {"role": "user", "content": "implement the plan"},
        _clarify_assistant(),
        {"role": "tool", "name": "clarify", "content": '{"user_response":"wait"}'},
        {"role": "assistant", "content": "Paused pending your decision."},
    ]
    assert turn_called_clarify(messages) is True
    assert is_valid_terminal_for_remaining_work(todo_store=store, messages=messages) is True
    assert build_boundary_handoff_nudge(todo_store=store, messages=messages) is None


def test_synthetic_nudge_does_not_reset_clarify_window():
    store = _store(("remaining step", "pending"))
    messages = [
        {"role": "user", "content": "implement the plan"},
        _clarify_assistant(),
        {
            "role": "user",
            "content": "[System: nudge]",
            "_boundary_handoff_synthetic": True,
        },
        {"role": "assistant", "content": "Still waiting."},
    ]
    assert turn_called_clarify(messages) is True
    assert build_boundary_handoff_nudge(todo_store=store, messages=messages) is None


def test_nudge_budget_disable_and_clarify_unavailable():
    store = _store(("remaining step", "pending"))
    assert (
        build_boundary_handoff_nudge(todo_store=store, attempts=2) is None
    )
    assert (
        build_boundary_handoff_nudge(todo_store=store, attempts=0, enabled=False)
        is None
    )
    assert (
        build_boundary_handoff_nudge(
            todo_store=store,
            attempts=0,
            clarify_available=False,
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
    # Guard only fires when clarify is in the live toolset.
    instance.valid_tool_names = {"clarify", "todo"}
    instance._todo_store = _store(
        ("skill-local gate", "completed"),
        ("scheduler scripts write", "pending"),
    )
    return instance


def test_loop_rejects_ambiguous_stop_then_accepts_when_todos_settled(agent):
    partial = (
        "I finished the skill-local gate and verified tests. "
        "Scheduler integration is still pending."
    )
    done = "All outstanding todos are complete."

    def model_call(_api_kwargs):
        if not hasattr(model_call, "n"):
            model_call.n = 0
        model_call.n += 1
        if model_call.n == 1:
            return _response(partial)
        # After the nudge, settle todos then stop — structured completion.
        agent._todo_store = _store(
            ("skill-local gate", "completed"),
            ("scheduler scripts write", "completed"),
        )
        return _response(done)

    agent._interruptible_api_call = model_call

    with (
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("implement the dual-repo plan")

    assert result["final_response"] == done
    assert agent._boundary_handoff_nudges == 1
    assert not any(
        isinstance(message, dict) and message.get("_boundary_handoff_synthetic")
        for message in result["messages"]
    )


def test_loop_english_handoff_without_clarify_still_nudges(agent):
    handoff = (
        "Completed and verified the skill-local gate. Remaining work is the "
        "scheduler scripts write at a workspace boundary. No external state "
        "changed. Do you want me to continue with that write?"
    )
    settled = "Marked remaining todos complete after your go-ahead path."

    def model_call(_api_kwargs):
        if not hasattr(model_call, "n"):
            model_call.n = 0
        model_call.n += 1
        if model_call.n == 1:
            return _response(handoff)
        agent._todo_store = _store(
            ("skill-local gate", "completed"),
            ("scheduler scripts write", "completed"),
        )
        return _response(settled)

    agent._interruptible_api_call = model_call

    with (
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("implement the dual-repo plan")

    assert result["final_response"] == settled
    assert agent._boundary_handoff_nudges == 1


def test_loop_skips_guard_when_clarify_not_in_toolset(agent):
    agent.valid_tool_names = {"todo"}
    handoff = "Stopped at the workspace boundary without calling clarify."
    agent._interruptible_api_call = lambda _kwargs: _response(handoff)

    with (
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("implement the dual-repo plan")

    assert result["final_response"] == handoff
    assert getattr(agent, "_boundary_handoff_nudges", 0) == 0
