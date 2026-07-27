"""Behavior coverage for the shared ``pre_response`` continuation gate."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


def _response(content):
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
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        instance = AIAgent(
            session_id="pre-response-test",
            api_key="test-key",
            base_url="https://example.invalid/v1",
            provider="openai-compat",
            model="test/model",
            max_iterations=3,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            platform="desktop",
        )
    instance._cached_system_prompt = "stable test prompt"
    instance._session_db = None
    instance._session_json_enabled = False
    instance.save_trajectories = False
    instance.compression_enabled = False
    instance._cleanup_task_resources = lambda *_a, **_kw: None
    instance._save_trajectory = lambda *_a, **_kw: None
    return instance


def test_non_coding_candidate_is_revised_before_delivery(agent):
    answers = iter([_response("rejected draft"), _response("corrected answer")])
    requests = []

    def model_call(api_kwargs):
        requests.append(api_kwargs["messages"])
        return next(answers)

    agent._interruptible_api_call = model_call
    emitted = []
    agent.interim_assistant_callback = lambda text, **kwargs: emitted.append(text)

    with (
        patch(
            "hermes_cli.plugins.has_hook",
            side_effect=lambda name: name == "pre_response",
        ),
        patch(
            "hermes_cli.plugins.get_pre_response_continue_message",
            side_effect=["Invoke the style skill and rewrite.", None],
        ) as gate,
        patch(
            "agent.response_hooks.max_response_continuations",
            return_value=1,
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("Explain this simply.")

    assert result["final_response"] == "corrected answer"
    assert result["completed"] is True
    assert emitted == []
    first_gate = gate.call_args_list[0].kwargs
    assert first_gate == {
        "response_text": "rejected draft",
        "user_message": "Explain this simply.",
        "session_id": "pre-response-test",
        "task_id": first_gate["task_id"],
        "turn_id": first_gate["turn_id"],
        "platform": "desktop",
        "model": "test/model",
        "attempt": 0,
    }
    assert first_gate["task_id"]
    assert first_gate["turn_id"]
    assert gate.call_args_list[1].kwargs["response_text"] == "corrected answer"
    assert gate.call_args_list[1].kwargs["attempt"] == 1

    # The revision request preserves assistant→user alternation for the next
    # provider call, but the rejected pair never reaches the returned history.
    assert [message["role"] for message in requests[1][-2:]] == [
        "assistant",
        "user",
    ]
    assert requests[1][-2]["content"] == "rejected draft"
    assert requests[1][-1]["content"] == "Invoke the style skill and rewrite."
    assert [message["content"] for message in result["messages"]] == [
        "Explain this simply.",
        "corrected answer",
    ]
    assert all(
        not message.get("_pre_response_synthetic")
        for message in result["messages"]
    )


def test_second_rejection_stops_at_bound_without_delivering_candidate(agent):
    answers = iter([_response("first rejected"), _response("second rejected")])
    agent._interruptible_api_call = lambda _kwargs: next(answers)

    with (
        patch(
            "hermes_cli.plugins.has_hook",
            side_effect=lambda name: name == "pre_response",
        ),
        patch(
            "hermes_cli.plugins.get_pre_response_continue_message",
            return_value="rewrite again",
        ) as gate,
        patch(
            "agent.response_hooks.max_response_continuations",
            return_value=1,
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("Answer in the required style.")

    assert gate.call_count == 2
    assert [call.kwargs["attempt"] for call in gate.call_args_list] == [0, 1]
    assert "response hook rejected the answer" in result["final_response"].lower()
    assert "stopped retrying" in result["final_response"].lower()
    assert "first rejected" not in result["final_response"]
    assert "second rejected" not in result["final_response"]
    assert all(
        message.get("content") not in {"first rejected", "second rejected", "rewrite again"}
        for message in result["messages"]
    )


def test_allow_or_noop_finishes_without_extra_call(agent):
    agent._interruptible_api_call = MagicMock(return_value=_response("allowed answer"))

    with (
        patch(
            "hermes_cli.plugins.has_hook",
            side_effect=lambda name: name == "pre_response",
        ),
        patch(
            "hermes_cli.plugins.get_pre_response_continue_message",
            return_value=None,
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("Give me the answer.")

    assert result["final_response"] == "allowed answer"
    assert agent._interruptible_api_call.call_count == 1


def test_hook_exception_fails_open(agent):
    agent._interruptible_api_call = MagicMock(return_value=_response("keep answer"))

    with (
        patch(
            "hermes_cli.plugins.has_hook",
            side_effect=lambda name: name == "pre_response",
        ),
        patch(
            "hermes_cli.plugins.get_pre_response_continue_message",
            side_effect=RuntimeError("hook failed"),
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("Give me the answer.")

    assert result["final_response"] == "keep answer"
    assert agent._interruptible_api_call.call_count == 1


def test_budget_fallback_does_not_reuse_rejected_candidate(agent):
    agent.max_iterations = 1
    agent.iteration_budget.max_total = 1
    agent._interruptible_api_call = MagicMock(return_value=_response("rejected draft"))
    agent._handle_max_iterations = MagicMock(return_value="budget fallback answer")

    with (
        patch(
            "hermes_cli.plugins.has_hook",
            side_effect=lambda name: name == "pre_response",
        ),
        patch(
            "hermes_cli.plugins.get_pre_response_continue_message",
            return_value="rewrite the draft",
        ),
        patch(
            "agent.response_hooks.max_response_continuations",
            return_value=1,
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("Give me the answer.")

    assert result["final_response"].startswith("budget fallback answer")
    assert result["turn_exit_reason"] == "max_iterations_reached(1/1)"
    assert agent._handle_max_iterations.call_count == 1
    assert all(
        message.get("content") not in {"rejected draft", "rewrite the draft"}
        for message in result["messages"]
    )


def test_transform_hook_still_runs_after_pre_response_accepts(agent):
    agent._interruptible_api_call = MagicMock(return_value=_response("accepted answer"))

    def invoke(hook_name, **kwargs):
        if hook_name == "transform_llm_output":
            return ["transformed answer"]
        return []

    with (
        patch(
            "hermes_cli.plugins.has_hook",
            side_effect=lambda name: name == "pre_response",
        ),
        patch(
            "hermes_cli.plugins.get_pre_response_continue_message",
            return_value=None,
        ),
        patch("hermes_cli.plugins.invoke_hook", side_effect=invoke),
    ):
        result = agent.run_conversation("Give me the answer.")

    assert result["final_response"] == "transformed answer"
    assert result["response_transformed"] is True


def test_pre_verify_finishes_before_pre_response_inspects_candidate(agent):
    answers = iter([_response("unverified answer"), _response("verified answer")])

    def model_call(_api_kwargs):
        agent._turn_file_mutation_paths = {"changed.py"}
        return next(answers)

    agent._interruptible_api_call = model_call

    with (
        patch(
            "hermes_cli.plugins.has_hook",
            side_effect=lambda name: name in {"pre_verify", "pre_response"},
        ),
        patch(
            "hermes_cli.plugins.get_pre_verify_continue_message",
            side_effect=["run checks", None],
        ),
        patch(
            "hermes_cli.plugins.get_pre_response_continue_message",
            return_value=None,
        ) as response_gate,
        patch("agent.verify_hooks.max_verify_nudges", return_value=3),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("Edit changed.py.")

    assert result["final_response"] == "verified answer"
    assert [call.kwargs["response_text"] for call in response_gate.call_args_list] == [
        "verified answer"
    ]


def test_active_pre_response_gate_buffers_provider_text_stream(agent):
    streamed = []
    agent.stream_delta_callback = streamed.append
    agent._interruptible_streaming_api_call = MagicMock(
        return_value=_response("candidate")
    )
    agent._interruptible_api_call = MagicMock(return_value=_response("candidate"))

    with (
        patch(
            "hermes_cli.plugins.has_hook",
            side_effect=lambda name: name == "pre_response",
        ),
        patch(
            "hermes_cli.plugins.get_pre_response_continue_message",
            return_value=None,
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("Give me the answer.")

    assert result["final_response"] == "candidate"
    assert streamed == []
    agent._interruptible_streaming_api_call.assert_not_called()
    agent._interruptible_api_call.assert_called_once()


def test_response_rejection_does_not_reuse_stale_verification_fallback(agent):
    agent.max_iterations = 2
    agent.iteration_budget.max_total = 2
    answers = iter([_response("unverified draft"), _response("rejected final")])

    def model_call(_api_kwargs):
        agent._turn_file_mutation_paths = {"changed.py"}
        return next(answers)

    agent._interruptible_api_call = model_call
    agent._handle_max_iterations = MagicMock(return_value="safe budget fallback")

    with (
        patch(
            "hermes_cli.plugins.has_hook",
            side_effect=lambda name: name in {"pre_verify", "pre_response"},
        ),
        patch(
            "hermes_cli.plugins.get_pre_verify_continue_message",
            side_effect=["run checks", None],
        ),
        patch(
            "hermes_cli.plugins.get_pre_response_continue_message",
            return_value="rewrite the final answer",
        ),
        patch("agent.verify_hooks.max_verify_nudges", return_value=3),
        patch(
            "agent.response_hooks.max_response_continuations",
            return_value=1,
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("Edit changed.py.")

    assert result["final_response"].startswith("safe budget fallback")
    assert "unverified draft" not in result["final_response"]
    assert "rejected final" not in result["final_response"]
    agent._handle_max_iterations.assert_called_once()


def test_budget_summary_request_is_ephemeral_after_response_rejection(agent):
    agent.client.chat.completions.create.return_value = _response("safe summary")
    messages = [
        {"role": "user", "content": "original prompt"},
        {
            "role": "assistant",
            "content": "rejected draft",
            "_pre_response_synthetic": True,
        },
        {
            "role": "user",
            "content": "rewrite it",
            "_pre_response_synthetic": True,
        },
    ]

    result = agent._handle_max_iterations(messages, 1)

    assert result == "safe summary"
    assert messages[-2]["role"] == "user"
    assert messages[-2]["_pre_response_synthetic"] is True
    assert messages[-1] == {"role": "assistant", "content": "safe summary"}
