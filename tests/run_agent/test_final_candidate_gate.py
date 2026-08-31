"""Behavior tests for the optional ALLOW/REPLACE final-candidate seam."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.final_candidate_gate import evaluate_final_candidate
from run_agent import AIAgent


def _response(content: str) -> SimpleNamespace:
    message = SimpleNamespace(
        role="assistant", content=content, tool_calls=None, reasoning=None,
        reasoning_content=None, reasoning_details=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="test/model", usage=None,
    )


@pytest.fixture()
def agent() -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        current = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    current.client = MagicMock()
    current._cached_system_prompt = "You are helpful."
    current._use_prompt_caching = False
    current.compression_enabled = False
    current.save_trajectories = False
    return current


def _run(agent: AIAgent, directive: dict):
    calls = []
    persisted = []

    def required(name, **kwargs):
        if name == "provider_request_gate":
            return {"action": "ALLOW"}
        if name == "assistant_final_candidate_gate":
            calls.append(kwargs)
            return directive
        return None

    agent.client.chat.completions.create.return_value = _response("model draft")
    with (
        patch("hermes_cli.lifecycle.invoke_required_hook", side_effect=required),
        patch.object(
            agent, "_persist_session",
            side_effect=lambda rows, _: persisted.append(deepcopy(rows)),
        ),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.object(agent, "_emit_interim_assistant_message") as emit,
    ):
        result = agent.run_conversation("research this", task_id="task-1")
    return result, calls, persisted, emit


def test_allow_may_canonicalize_the_only_visible_and_durable_body(agent):
    result, calls, persisted, emit = _run(
        agent, {"action": "ALLOW", "content": "authorized body"},
    )
    assert len(calls) == 1
    assert result["final_response"] == "authorized body"
    assert emit.call_count == 0
    assert persisted[-1][-1]["content"] == "authorized body"


def test_replace_is_the_only_visible_and_durable_body(agent):
    result, _, persisted, emit = _run(
        agent, {"action": "REPLACE", "content": "Evidence is incomplete."},
    )
    assert result["final_response"] == "Evidence is incomplete."
    assert emit.call_count == 0
    assert persisted[-1][-1]["content"] == "Evidence is incomplete."
    assert all(row.get("content") != "model draft" for row in persisted[-1])


def test_candidate_registration_does_not_require_a_provider_gate(agent):
    result, _, _, _ = _run(agent, {"action": "ALLOW"})
    assert result["final_response"] == "model draft"
    assert agent.client.chat.completions.create.call_count == 1


def test_continue_is_rejected_without_another_provider_call():
    with (
        patch(
            "hermes_cli.lifecycle.invoke_required_hook",
            return_value={"action": "CONTINUE", "context": "do more"},
        ),
        pytest.raises(RuntimeError, match="ALLOW or REPLACE"),
    ):
        evaluate_final_candidate(
            response_text="draft", session_id="s", task_id="t",
            turn_id="turn", model="m", platform="p", finish_reason="stop",
            iteration=1, max_iterations=2, remaining_iterations=1,
        )
