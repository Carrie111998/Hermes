from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _tool_defs(*names):
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _agent(recorder=None):
    with (
        patch("run_agent.get_tool_definitions", return_value=_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            provider="openrouter",
            model="requested/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            activity_recorder=recorder,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    return agent


def _tool_call(call_id="c1"):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name="web_search", arguments="{}"),
    )


def _response(model="served/model", content="done", finish_reason="stop", tool_calls=None):
    message = SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        reasoning_content=None,
        reasoning=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        model=model,
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=20,
            input_tokens=10,
            output_tokens=20,
            cache_read_tokens=80,
            cache_write_tokens=5,
            reasoning_tokens=7,
            request_count=1,
            raw_usage={"authorization": "must-not-forward"},
        ),
    )


def test_constructor_preserves_optional_recorder():
    recorder = MagicMock()
    assert _agent(recorder).activity_recorder is recorder
    assert _agent().activity_recorder is None


def test_success_records_canonical_usage_without_post_hook_listener():
    recorder = MagicMock()
    agent = _agent(recorder)
    agent.client.chat.completions.create.return_value = _response()
    with (
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("hello")
    assert result["final_response"] == "done"
    recorder.record_response.assert_called_once()
    call = recorder.record_response.call_args.kwargs
    assert call["provider"] == "openrouter"
    assert call["model"] == "served/model"
    assert set(call["usage"]) >= {
        "input_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "output_tokens",
        "reasoning_tokens",
        "request_count",
    }
    assert "raw_usage" not in call["usage"]
    assert all(value != "must-not-forward" for value in call["usage"].values())


def test_response_model_switches_are_attributed_separately():
    """A mid-conversation served-model switch must produce two distinct routes.

    This drives the real ``run_conversation`` loop across two API responses so
    the conversation-loop wiring itself is covered, not just the helper.
    """
    recorder = MagicMock()
    agent = _agent(recorder)
    agent.client.chat.completions.create.side_effect = [
        _response("first/model", content="", finish_reason="tool_calls", tool_calls=[_tool_call()]),
        _response("second/model"),
    ]
    with (
        patch("run_agent.handle_function_call", return_value="search result"),
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("search something")
    assert result["final_response"] == "done"
    assert result["api_calls"] == 2
    assert [call.kwargs["model"] for call in recorder.record_response.call_args_list] == [
        "first/model",
        "second/model",
    ]


def test_recorder_failure_does_not_change_conversation_result():
    recorder = MagicMock()
    recorder.record_response.side_effect = RuntimeError("sensitive response body")
    agent = _agent(recorder)
    agent.client.chat.completions.create.return_value = _response()
    with (
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("hello")
    assert result["final_response"] == "done"
