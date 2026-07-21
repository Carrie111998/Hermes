"""Runtime coverage for delegated-child non-convergence guardrails."""

from contextlib import nullcontext
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.delegation_context import delegated_child_context
from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list[dict]:
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


def _progress_config(*, enabled: bool = True, warn_after: int = 2, halt_after: int = 3) -> dict:
    return {
        "delegation": {
            "progress_tracker": {
                "enabled": enabled,
                "warn_after": warn_after,
                "halt_after": halt_after,
            }
        }
    }


def _make_agent(
    *tool_names: str,
    config: dict | None = None,
    delegated: bool = False,
    max_iterations: int = 10,
) -> AIAgent:
    context = delegated_child_context() if delegated else nullcontext()
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs(*tool_names)),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value=config or {}),
        patch("hermes_cli.config.load_config_readonly", return_value=config or {}),
        patch("run_agent.OpenAI"),
        context,
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            max_iterations=max_iterations,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


def _tool_call(name: str, call_id: str, arguments: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments or {})),
    )


def _response(
    *,
    content: str = "",
    tool_calls: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    finish_reason = "tool_calls" if tool_calls else "stop"
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        model="test/model",
        usage=None,
    )


def _run(agent: AIAgent, prompt: str = "work autonomously") -> dict:
    agent._disable_streaming = True
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation(prompt)


def test_enabled_delegation_tracker_is_not_installed_on_parent_agent():
    agent = _make_agent("web_search", config=_progress_config(), delegated=False)

    assert agent._progress_tracker is None


def test_enabled_delegation_tracker_is_installed_on_child_agent():
    agent = _make_agent("web_search", config=_progress_config(), delegated=True)

    assert agent._progress_tracker is not None
    assert agent._progress_tracker.enabled is True


def test_disabled_delegation_tracker_is_not_installed_on_child_agent():
    agent = _make_agent(
        "web_search",
        config=_progress_config(enabled=False),
        delegated=True,
    )

    assert agent._progress_tracker is None


def test_non_mutating_rounds_warn_then_take_controlled_halt_path():
    agent = _make_agent(
        "web_search",
        config=_progress_config(warn_after=2, halt_after=3),
        delegated=True,
    )
    agent.client.chat.completions.create.side_effect = [
        *[
            _response(tool_calls=[_tool_call("web_search", f"call-{index}")])
            for index in range(1, 7)
        ],
        _response(content="late completion"),
    ]

    with patch("run_agent.handle_function_call", return_value=json.dumps({"results": []})) as dispatch:
        result = _run(agent)

    assert dispatch.call_count == 3
    assert result["turn_exit_reason"] == "guardrail_halt"
    assert "non-convergence" in result["final_response"]
    tool_contents = [
        message["content"]
        for message in result["messages"]
        if message.get("role") == "tool"
    ]
    assert any("PROGRESS TRACKER" in content for content in tool_contents)


def test_warning_is_present_when_tool_result_is_first_persisted():
    agent = _make_agent(
        "web_search",
        config=_progress_config(warn_after=1, halt_after=3),
        delegated=True,
    )
    agent.client.chat.completions.create.side_effect = [
        _response(tool_calls=[_tool_call("web_search", "search-1")]),
        _response(content="done"),
    ]
    persisted_snapshots: list[list[dict]] = []

    def capture_flush(messages, *_args, **_kwargs):
        persisted_snapshots.append(deepcopy(messages))
        return True

    agent._flush_messages_to_session_db = capture_flush
    with patch("run_agent.handle_function_call", return_value=json.dumps({"results": []})):
        result = _run(agent)

    assert result["final_response"] == "done"
    persisted_tool_contents = [
        message["content"]
        for snapshot in persisted_snapshots
        for message in snapshot
        if message.get("role") == "tool"
    ]
    assert any("PROGRESS TRACKER" in content for content in persisted_tool_contents)


def test_successful_write_result_resets_progress_and_allows_completion():
    agent = _make_agent(
        "write_file",
        config=_progress_config(warn_after=2, halt_after=3),
        delegated=True,
    )
    agent.client.chat.completions.create.side_effect = [
        *[
            _response(
                tool_calls=[
                    _tool_call(
                        "write_file",
                        f"write-{index}",
                        {"path": f"/tmp/out-{index}.txt", "content": "done"},
                    )
                ]
            )
            for index in range(1, 4)
        ],
        _response(content="completed"),
    ]
    landed = json.dumps({"bytes_written": 4, "resolved_path": "/tmp/out.txt"})

    with (
        patch("run_agent.handle_function_call", return_value=landed) as dispatch,
        patch.object(
            agent,
            "_append_guardrail_observation",
            side_effect=lambda _name, _args, value, **_kwargs: (
                f"{value}\n\n[existing guardrail guidance]"
            ),
        ),
    ):
        result = _run(agent)

    assert dispatch.call_count == 3
    assert result["turn_exit_reason"].startswith("text_response")
    assert result["final_response"] == "completed"
    assert agent._progress_tracker.iterations_since_progress == 0


def test_failed_write_results_do_not_reset_progress():
    agent = _make_agent(
        "write_file",
        config=_progress_config(warn_after=2, halt_after=3),
        delegated=True,
    )
    agent.client.chat.completions.create.side_effect = [
        _response(
            tool_calls=[
                _tool_call(
                    "write_file",
                    f"failed-{index}",
                    {"path": "/tmp/out.txt", "content": "done"},
                )
            ]
        )
        for index in range(1, 5)
    ]

    with patch(
        "run_agent.handle_function_call",
        return_value=json.dumps({"error": "permission denied"}),
    ) as dispatch:
        result = _run(agent)

    assert dispatch.call_count == 3
    assert result["turn_exit_reason"] == "guardrail_halt"


def test_concurrent_batch_counts_as_one_iteration():
    agent = _make_agent(
        "web_search",
        config=_progress_config(warn_after=3, halt_after=5),
        delegated=True,
    )
    agent.client.chat.completions.create.side_effect = [
        _response(
            tool_calls=[
                _tool_call("web_search", "search-1", {"query": "one"}),
                _tool_call("web_search", "search-2", {"query": "two"}),
            ]
        ),
        _response(content="done"),
    ]

    with patch("run_agent.handle_function_call", return_value=json.dumps({"results": []})):
        result = _run(agent)

    assert result["final_response"] == "done"
    assert agent._progress_tracker.iterations_since_progress == 1


def test_segmented_batch_uses_successful_write_result_as_progress():
    agent = _make_agent(
        "web_search",
        "write_file",
        config=_progress_config(warn_after=2, halt_after=3),
        delegated=True,
    )
    agent.client.chat.completions.create.side_effect = [
        _response(
            tool_calls=[
                _tool_call("web_search", "before", {"query": "one"}),
                _tool_call(
                    "write_file",
                    "write",
                    {"path": "/tmp/out.txt", "content": "done"},
                ),
                _tool_call("web_search", "after", {"query": "two"}),
            ]
        ),
        _response(content="done"),
    ]

    def dispatch(name, _args, _task_id, **_kwargs):
        if name == "write_file":
            return json.dumps({"bytes_written": 4, "resolved_path": "/tmp/out.txt"})
        return json.dumps({"results": []})

    with patch("run_agent.handle_function_call", side_effect=dispatch):
        result = _run(agent)

    assert result["final_response"] == "done"
    assert agent._progress_tracker.iterations_since_progress == 0


def test_blocked_write_does_not_count_as_progress():
    agent = _make_agent(
        "write_file",
        config=_progress_config(warn_after=3, halt_after=5),
        delegated=True,
    )
    agent.client.chat.completions.create.side_effect = [
        _response(
            tool_calls=[
                _tool_call(
                    "write_file",
                    "blocked",
                    {"path": "/tmp/out.txt", "content": "done"},
                )
            ]
        ),
        _response(content="done"),
    ]

    with (
        patch("hermes_cli.plugins.resolve_pre_tool_block", return_value="blocked by policy"),
        patch("run_agent.handle_function_call") as dispatch,
    ):
        result = _run(agent)

    dispatch.assert_not_called()
    assert result["final_response"] == "done"
    assert agent._progress_tracker.iterations_since_progress == 1


def test_cancelled_write_result_does_not_count_as_progress():
    agent = _make_agent(
        "write_file",
        config=_progress_config(),
        delegated=True,
    )
    agent._turn_failed_file_mutations = {}
    agent._turn_file_mutation_paths = set()
    agent._progress_iteration_made_progress = False

    agent._record_file_mutation_result(
        "write_file",
        {"path": "/tmp/out.txt", "content": "done"},
        json.dumps({"error": "cancelled"}),
        True,
    )

    assert agent._progress_iteration_made_progress is False


def test_user_visible_text_with_tool_calls_counts_as_progress():
    agent = _make_agent(
        "web_search",
        config=_progress_config(warn_after=2, halt_after=3),
        delegated=True,
    )
    agent.client.chat.completions.create.side_effect = [
        *[
            _response(
                content=f"Useful finding {index}",
                tool_calls=[_tool_call("web_search", f"search-{index}")],
            )
            for index in range(1, 4)
        ],
        _response(content="done"),
    ]

    with patch("run_agent.handle_function_call", return_value=json.dumps({"results": []})):
        result = _run(agent)

    assert result["final_response"] == "done"
    assert agent._progress_tracker.iterations_since_progress == 0


def test_structured_interim_text_counts_as_progress():
    agent = _make_agent(
        "web_search",
        config=_progress_config(warn_after=2, halt_after=3),
        delegated=True,
    )
    agent.client.chat.completions.create.side_effect = [
        *[
            _response(tool_calls=[_tool_call("web_search", f"search-{index}")])
            for index in range(1, 4)
        ],
        _response(content="done"),
    ]

    agent.interim_assistant_callback = MagicMock()

    def visible_text(message):
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            return ""
        return f"structured commentary {tool_calls[0]['id']}"

    with (
        patch("run_agent.handle_function_call", return_value=json.dumps({"results": []})),
        patch.object(agent, "_interim_assistant_visible_text", side_effect=visible_text),
    ):
        result = _run(agent)

    assert result["final_response"] == "done"
    assert agent._progress_tracker.iterations_since_progress == 0


def test_withheld_top_level_text_does_not_count_as_visible_progress():
    agent = _make_agent(
        "web_search",
        config=_progress_config(warn_after=2, halt_after=3),
        delegated=True,
    )
    agent.client.chat.completions.create.side_effect = [
        *[
            _response(
                content=f"withheld answer {index}",
                tool_calls=[_tool_call("web_search", f"search-{index}")],
            )
            for index in range(1, 4)
        ],
        _response(content="unreachable"),
    ]

    with (
        patch("run_agent.handle_function_call", return_value=json.dumps({"results": []})),
        patch.object(agent, "_interim_assistant_visible_text", return_value=""),
    ):
        result = _run(agent)

    assert result["turn_exit_reason"] == "guardrail_halt"
    assert agent.client.chat.completions.create.call_count == 3


def test_repeated_already_delivered_text_does_not_evade_halt():
    agent = _make_agent(
        "web_search",
        config=_progress_config(warn_after=2, halt_after=3),
        delegated=True,
    )
    agent.interim_assistant_callback = MagicMock()
    agent.client.chat.completions.create.side_effect = [
        *[
            _response(
                content="same update",
                tool_calls=[_tool_call("web_search", f"search-{index}")],
            )
            for index in range(1, 6)
        ],
        _response(content="unreachable"),
    ]

    with patch("run_agent.handle_function_call", return_value=json.dumps({"results": []})):
        result = _run(agent)

    assert result["turn_exit_reason"] == "guardrail_halt"
    assert agent.client.chat.completions.create.call_count == 4
    agent.interim_assistant_callback.assert_called_once()


def test_new_user_turn_resets_stale_progress_state():
    agent = _make_agent(
        "web_search",
        config=_progress_config(warn_after=2, halt_after=3),
        delegated=True,
    )
    agent._progress_tracker.finish_iteration(made_progress=False)
    agent._progress_tracker.finish_iteration(made_progress=False)
    assert agent._progress_tracker.iterations_since_progress == 2
    agent.client.chat.completions.create.return_value = _response(content="done")

    result = _run(agent, "new request")

    assert result["final_response"] == "done"
    assert agent._progress_tracker.iterations_since_progress == 0
