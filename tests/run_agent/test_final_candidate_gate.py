"""Behavior tests for the optional Host-owned final-candidate seam."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.agent_runtime_helpers import convert_to_trajectory_format
from agent.final_candidate_gate import evaluate_final_candidate
from run_agent import AIAgent


def _response(content: str) -> SimpleNamespace:
    message = SimpleNamespace(
        role="assistant",
        content=content,
        tool_calls=None,
        reasoning=None,
        reasoning_content=None,
        reasoning_details=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="test/model",
        usage=None,
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


def _run(agent: AIAgent, directives: list[dict], responses: list[SimpleNamespace]):
    candidate_calls = []
    persisted = []

    def required(name, **kwargs):
        if name == "provider_request_gate":
            return {"action": "ALLOW", "allow_streaming": False}
        if name == "assistant_final_candidate_gate":
            candidate_calls.append(kwargs)
            return directives.pop(0)
        return None

    agent.client.chat.completions.create.side_effect = responses
    with (
        patch("hermes_cli.lifecycle.invoke_required_hook", side_effect=required),
        patch(
            "hermes_cli.lifecycle.has_hook",
            side_effect=lambda name: name == "assistant_final_candidate_gate",
        ),
        patch.object(agent, "_persist_session", side_effect=lambda rows, _: persisted.append(deepcopy(rows))),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.object(agent, "_emit_interim_assistant_message") as emit,
    ):
        result = agent.run_conversation("research this", task_id="task-1")
    return result, candidate_calls, persisted, emit


def test_continue_is_private_and_second_request_is_role_valid(agent):
    digest = "a" * 64
    result, calls, persisted, emit = _run(
        agent,
        [
            {
                "action": "CONTINUE",
                "context": "Only complete the missing market scan.",
                "state_revision": 7,
                "pending_sha256": digest,
            },
            {"action": "ALLOW"},
        ],
        [_response("unverified draft"), _response("verified answer")],
    )

    assert result["completed"] is True
    assert result["final_response"] == "verified answer"
    assert [row["response_text"] for row in calls] == [
        "unverified draft",
        "verified answer",
    ]
    second_request = agent.client.chat.completions.create.call_args_list[1].kwargs
    assert second_request.get("stream") is not True
    assert [
        row["role"] for row in second_request["messages"][-2:]
    ] == ["assistant", "user"]
    assert any(row.get("content") == "unverified draft" for row in second_request["messages"])
    assert any(
        row.get("content") == "Only complete the missing market scan."
        for row in second_request["messages"]
    )
    assert emit.call_count == 0
    assert persisted
    durable = persisted[-1]
    assert not any(row.get("content") == "unverified draft" for row in durable)
    assert not any(row.get("_final_candidate_synthetic") for row in durable)
    assert durable[-1]["role"] == "assistant"
    assert durable[-1]["content"] == "verified answer"


def test_allow_may_canonicalize_the_only_visible_and_durable_body(agent):
    result, _, persisted, emit = _run(
        agent,
        [{"action": "ALLOW", "content": "authorized body"}],
        [_response("model draft")],
    )

    assert result["final_response"] == "authorized body"
    assert emit.call_count == 0
    assert persisted[-1][-1]["content"] == "authorized body"
    assert all(row.get("content") != "model draft" for row in persisted[-1])


def test_replace_is_the_only_visible_and_durable_body(agent):
    result, _, persisted, emit = _run(
        agent,
        [{
            "action": "REPLACE",
            "content": "当前缺少必要证据，不能给出正式结论。",
            "reason_code": "NO_PROGRESS",
        }],
        [_response("unsupported draft")],
    )

    assert result["final_response"] == "当前缺少必要证据，不能给出正式结论。"
    assert emit.call_count == 0
    assert persisted[-1][-1]["content"] == "当前缺少必要证据，不能给出正式结论。"
    assert all(row.get("content") != "unsupported draft" for row in persisted[-1])


def test_candidate_owner_without_non_streaming_request_gate_fails_pre_provider(agent):
    def required(name, **_kwargs):
        if name == "provider_request_gate":
            return {"action": "ALLOW"}
        return None

    with (
        patch("hermes_cli.lifecycle.invoke_required_hook", side_effect=required),
        patch(
            "hermes_cli.lifecycle.has_hook",
            side_effect=lambda name: name == "assistant_final_candidate_gate",
        ),
        pytest.raises(RuntimeError, match="requires allow_streaming=false"),
    ):
        agent.run_conversation("research this", task_id="task-1")

    agent.client.chat.completions.create.assert_not_called()


def test_continue_without_remaining_budget_is_rejected():
    with (
        patch(
            "hermes_cli.lifecycle.invoke_required_hook",
            return_value={
                "action": "CONTINUE",
                "context": "do more",
                "state_revision": 1,
                "pending_sha256": "b" * 64,
            },
        ),
        pytest.raises(RuntimeError, match="without budget"),
    ):
        evaluate_final_candidate(
            response_text="draft",
            session_id="s",
            task_id="t",
            turn_id="turn",
            model="m",
            platform="p",
            finish_reason="stop",
            iteration=1,
            max_iterations=1,
            remaining_iterations=0,
        )


def test_unchanged_continue_is_rejected_instead_of_spending_more_tokens(agent):
    directive = {
        "action": "CONTINUE",
        "context": "Only complete the missing market scan.",
        "state_revision": 7,
        "pending_sha256": "c" * 64,
    }

    result, calls, persisted, emit = _run(
        agent,
        [directive, directive],
        [_response("first draft"), _response("unchanged second draft")],
    )

    assert len(calls) == 2
    assert agent.client.chat.completions.create.call_count == 2
    assert "repeated an unchanged CONTINUE" in result["final_response"]
    assert persisted
    assert not any(
        row.get("content") in {"first draft", "unchanged second draft"}
        for row in persisted[-1]
    )
    assert emit.call_count == 0


def test_candidate_scaffolding_is_absent_from_trajectory(agent):
    rows = [
        {"role": "user", "content": "research this"},
        {
            "role": "assistant",
            "content": "private draft",
            "_final_candidate_synthetic": True,
        },
        {
            "role": "user",
            "content": "private nudge",
            "_final_candidate_synthetic": True,
        },
        {"role": "assistant", "content": "public answer"},
    ]

    trajectory = convert_to_trajectory_format(agent, rows, "research this", True)

    values = [str(row.get("value", "")) for row in trajectory]
    assert all("private draft" not in value for value in values)
    assert all("private nudge" not in value for value in values)
    assert any("public answer" in value for value in values)
