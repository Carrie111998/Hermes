"""Structured delivery suppression is a successful terminal tool outcome."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.delivery_outcome import (
    DELIVERY_SUPPRESSION_TOKEN,
    get_delivery_outcome,
)
from gateway.response_filters import should_suppress_delivery
from run_agent import AIAgent


def _tool_definitions(*names: str) -> list[dict]:
    if not names:
        names = ("todo",)
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _tool_response(*, action: str, reason: str) -> SimpleNamespace:
    call = SimpleNamespace(
        id="delivery-call",
        type="function",
        function=SimpleNamespace(
            name="todo",
            arguments=json.dumps(
                {
                    "delivery_outcome": {
                        "action": action,
                        "reason": reason,
                    }
                }
            ),
        ),
    )
    message = SimpleNamespace(content="", tool_calls=[call])
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=message,
                finish_reason="tool_calls",
            )
        ],
        model="test/model",
        usage=None,
    )


def _text_response(content: str) -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="test/model",
        usage=None,
    )


def _plain_tool_response(name: str) -> SimpleNamespace:
    call = SimpleNamespace(
        id=f"{name}-call",
        type="function",
        function=SimpleNamespace(name=name, arguments="{}"),
    )
    message = SimpleNamespace(content="", tool_calls=[call])
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=message,
                finish_reason="tool_calls",
            )
        ],
        model="test/model",
        usage=None,
    )


def _mixed_suppress_response(sibling_name: str) -> SimpleNamespace:
    sibling = SimpleNamespace(
        id=f"{sibling_name}-call",
        type="function",
        function=SimpleNamespace(name=sibling_name, arguments="{}"),
    )
    suppress = _tool_response(
        action="suppress",
        reason="No user-facing update is needed.",
    ).choices[0].message.tool_calls[0]
    message = SimpleNamespace(content="", tool_calls=[sibling, suppress])
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=message,
                finish_reason="tool_calls",
            )
        ],
        model="test/model",
        usage=None,
    )


@pytest.fixture()
def todo_agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=_tool_definitions()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent._disable_streaming = True
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.valid_tool_names = {"todo"}
    agent.tools = _tool_definitions()
    return agent


def _run(agent: AIAgent, prompt: str = "Check whether an update is needed"):
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        return agent.run_conversation(prompt)


def test_suppress_receipt_terminates_without_second_provider_call(todo_agent):
    reason = "No new operational facts are available."
    todo_agent.max_iterations = 1
    todo_agent.client.chat.completions.create.return_value = _tool_response(
        action="suppress",
        reason=reason,
    )

    result = _run(todo_agent)

    assert result["final_response"] == DELIVERY_SUPPRESSION_TOKEN
    assert result["failed"] is False
    assert result["completed"] is True
    assert result["api_calls"] == 1
    assert (
        result["turn_exit_reason"]
        == "text_response(structured_delivery_suppression)"
    )
    assert result["delivery_outcome"] == {
        "action": "suppress",
        "reason": reason,
        "turn_id": result["turn_id"],
    }
    todo_agent.client.chat.completions.create.assert_called_once()

    current_turn = []
    for message in reversed(result["messages"]):
        current_turn.append(message)
        if message.get("role") == "user":
            break
    current_turn.reverse()
    assert [message.get("role") for message in current_turn] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert current_turn[-1]["content"] == DELIVERY_SUPPRESSION_TOKEN
    assert all(
        not message.get("_empty_recovery_synthetic")
        for message in current_turn
    )
    receipt = json.loads(current_turn[-2]["content"])
    assert receipt["delivery_outcome"] == {
        "recorded": True,
        "action": "suppress",
        "turn_id": result["turn_id"],
    }


def test_deliver_receipt_still_requires_model_final_response(todo_agent):
    todo_agent.client.chat.completions.create.side_effect = [
        _tool_response(
            action="deliver",
            reason="A material update is available.",
        ),
        _text_response("Material update."),
    ]

    result = _run(todo_agent)

    assert result["final_response"] == "Material update."
    assert result["failed"] is False
    assert result["completed"] is True
    assert result["api_calls"] == 2
    assert result["delivery_outcome"]["action"] == "deliver"
    assert todo_agent.client.chat.completions.create.call_count == 2


def test_rejected_suppress_directive_fails_open_to_visible_response(todo_agent):
    todo_agent.client.chat.completions.create.side_effect = [
        _tool_response(action="suppress", reason=""),
        _text_response("The suppression directive was invalid."),
    ]

    result = _run(todo_agent)

    assert result["final_response"] == "The suppression directive was invalid."
    assert result["failed"] is False
    assert result["completed"] is True
    assert result["api_calls"] == 2
    assert result["delivery_outcome"] is None
    assert todo_agent.client.chat.completions.create.call_count == 2


def test_suppress_with_failing_sibling_returns_results_to_model(todo_agent):
    todo_agent.valid_tool_names = {"todo", "web_search"}
    todo_agent.tools = _tool_definitions("todo", "web_search")
    todo_agent.client.chat.completions.create.side_effect = [
        _mixed_suppress_response("web_search"),
        _text_response("The sibling tool failed; this needs attention."),
    ]

    with patch(
        "run_agent.handle_function_call",
        return_value=json.dumps(
            {"success": False, "error": "search failed"}
        ),
    ):
        result = _run(todo_agent)

    assert (
        result["final_response"]
        == "The sibling tool failed; this needs attention."
    )
    assert result["api_calls"] == 2
    assert todo_agent.client.chat.completions.create.call_count == 2
    assert result["delivery_outcome"] is None
    assert should_suppress_delivery(result) is False
    second_request = (
        todo_agent.client.chat.completions.create.call_args_list[1].kwargs
    )
    tool_results = [
        message
        for message in second_request["messages"]
        if message.get("role") == "tool"
    ]
    assert any("search failed" in message["content"] for message in tool_results)
    todo_receipt = next(
        json.loads(message["content"])
        for message in tool_results
        if message.get("tool_call_id") == "delivery-call"
    )
    assert todo_receipt["delivery_outcome"]["recorded"] is False
    assert "isolated todo call" in todo_receipt["error"]
    assert (
        "observe sibling tool results"
        in todo_receipt["delivery_outcome"]["error"]
    )


def test_suppress_with_invalid_named_sibling_returns_results_to_model(
    todo_agent,
):
    todo_agent.client.chat.completions.create.side_effect = [
        _mixed_suppress_response("hallucinated_tool"),
        _text_response("The sibling tool name was invalid."),
    ]

    result = _run(todo_agent)

    assert result["final_response"] == "The sibling tool name was invalid."
    assert result["api_calls"] == 2
    assert todo_agent.client.chat.completions.create.call_count == 2
    assert result["delivery_outcome"] is None
    assert should_suppress_delivery(result) is False


def test_mixed_suppress_is_rejected_before_process_exiting_sibling(
    todo_agent,
):
    todo_agent.valid_tool_names = {"todo", "web_search"}
    todo_agent.tools = _tool_definitions("todo", "web_search")
    todo_agent.client.chat.completions.create.return_value = (
        _mixed_suppress_response("web_search")
    )

    def exit_during_sibling_execution(
        _assistant_message,
        messages,
        _task_id,
        _api_call_count,
    ):
        todo_receipt = next(
            json.loads(message["content"])
            for message in messages
            if (
                message.get("role") == "tool"
                and message.get("tool_call_id") == "delivery-call"
            )
        )
        assert todo_receipt["delivery_outcome"]["recorded"] is False
        assert get_delivery_outcome(
            todo_agent,
            todo_agent._current_turn_id,
        ) is None
        raise SystemExit(75)

    with (
        patch.object(
            todo_agent,
            "_execute_tool_calls",
            side_effect=exit_during_sibling_execution,
        ),
        pytest.raises(SystemExit, match="75"),
    ):
        _run(todo_agent)

    todo_agent.client.chat.completions.create.assert_called_once()


def test_suppress_is_not_deferred_by_code_like_filename(todo_agent):
    def model_call(**_kwargs):
        todo_agent._turn_file_mutation_paths.add(
            "src/test_delivery_suppression.py"
        )
        return _tool_response(
            action="suppress",
            reason="No user-facing update is needed.",
        )

    todo_agent.client.chat.completions.create.side_effect = model_call

    with patch(
        "agent.verification_stop.build_verify_on_stop_nudge",
        side_effect=AssertionError("semantic completion gate must not run"),
    ):
        result = _run(todo_agent)

    assert result["final_response"] == DELIVERY_SUPPRESSION_TOKEN
    assert result["failed"] is False
    assert result["completed"] is True
    assert result["api_calls"] == 1
    todo_agent.client.chat.completions.create.assert_called_once()
