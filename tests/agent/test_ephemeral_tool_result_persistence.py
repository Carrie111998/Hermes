import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.agent_runtime_helpers import convert_to_trajectory_format, dump_api_request_debug
from agent.tool_dispatch_helpers import (
    _durable_message_copy,
    _has_ephemeral_sensitive_context,
    _seal_ephemeral_tool_results,
    _trajectory_normalize_msg,
)
from agent.tool_executor import _observer_safe_tool_result
from gateway.session_context import (
    clear_session_vars,
    consume_slack_history_authorization,
    set_session_vars,
)
from run_agent import AIAgent


SECRET = "private Slack message that must not persist"
PLACEHOLDER = "[Ephemeral Slack context was used for this turn and was not retained.]"


def _ephemeral_message():
    return {
        "role": "tool",
        "name": "slack_history",
        "tool_name": "slack_history",
        "tool_call_id": "call-1",
        "content": SECRET,
        "api_content": SECRET,
        "_persist_content_override": PLACEHOLDER,
    }


def _agent(tmp_path):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("run_agent._hermes_home", tmp_path),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://example.invalid/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "system"
    return agent


def test_durable_copy_replaces_content_without_mutating_live_message():
    live = _ephemeral_message()

    durable = _durable_message_copy(live)

    assert durable["content"] == PLACEHOLDER
    assert "api_content" not in durable
    assert "_persist_content_override" not in durable
    assert live["content"] == SECRET


def test_sqlite_flush_receives_placeholder_only(tmp_path):
    agent = _agent(tmp_path)
    agent._session_db = MagicMock()
    agent._session_db_created = True
    agent.session_id = "session-1"
    agent._last_flushed_db_idx = 0
    agent._flushed_db_message_ids = set()
    live = _ephemeral_message()

    assert agent._flush_messages_to_session_db([live], []) is True

    rows = agent._session_db.append_messages_batch.call_args.kwargs["messages"]
    serialized = json.dumps(rows)
    assert PLACEHOLDER in serialized
    assert SECRET not in serialized
    assert live["content"] == SECRET


def test_optional_json_snapshot_receives_placeholder_only(tmp_path):
    agent = _agent(tmp_path)
    agent._session_json_enabled = True
    agent.logs_dir = tmp_path
    agent.session_id = "session-1"
    live = _ephemeral_message()

    agent._save_session_log([live])

    payload = (tmp_path / "session_session-1.json").read_text()
    assert PLACEHOLDER in payload
    assert SECRET not in payload


def test_trajectory_normalization_receives_placeholder_only():
    durable = _trajectory_normalize_msg(_ephemeral_message())
    assert durable["content"] == PLACEHOLDER
    assert SECRET not in json.dumps(durable)


def test_trajectory_conversion_receives_placeholder_only():
    agent = SimpleNamespace(_format_tools_for_system_message=lambda: "")
    trajectory = convert_to_trajectory_format(
        agent,
        [
            {"role": "user", "content": "read"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "slack_history",
                            "arguments": "{}",
                        },
                    }
                ],
            },
            _ephemeral_message(),
        ],
        "read",
        True,
    )
    serialized = json.dumps(trajectory)
    assert PLACEHOLDER in serialized
    assert SECRET not in serialized


def test_request_dump_is_disabled_after_sensitive_context_is_consumed(tmp_path):
    tokens = set_session_vars(
        platform="slack",
        chat_id="C12345678",
        scope_id="T12345678",
        slack_history_authorized=True,
    )
    try:
        assert consume_slack_history_authorization() is True
        agent = SimpleNamespace(logs_dir=tmp_path)
        result = dump_api_request_debug(
            agent,
            {"messages": [{"role": "tool", "content": SECRET}]},
            reason="test",
        )
    finally:
        clear_session_vars(tokens)

    assert result is None
    assert list(tmp_path.glob("request_dump_*.json")) == []


def test_hidden_reasoning_is_not_durable_after_sensitive_context_read():
    tokens = set_session_vars(
        platform="slack",
        chat_id="C12345678",
        scope_id="T12345678",
        slack_history_authorized=True,
    )
    try:
        assert consume_slack_history_authorization() is True
        live = {
            "role": "assistant",
            "content": "user-visible summary",
            "reasoning": SECRET,
            "reasoning_content": SECRET,
            "reasoning_details": [{"text": SECRET}],
            "codex_reasoning_items": [{"text": SECRET}],
        }
        durable = _durable_message_copy(live)
    finally:
        clear_session_vars(tokens)

    assert durable["content"] == "user-visible summary"
    assert "reasoning" not in durable
    assert SECRET not in json.dumps(durable)
    assert live["reasoning"] == SECRET


def test_finalizer_seal_removes_live_payload_and_marks_sensitive_reasoning():
    messages = [
        {"role": "user", "content": "read above"},
        _ephemeral_message(),
        {"role": "assistant", "content": "summary", "reasoning": SECRET},
        {"role": "user", "content": "next turn"},
        {"role": "assistant", "content": "unrelated", "reasoning": "keep"},
    ]

    assert _seal_ephemeral_tool_results(messages) == 1

    assert messages[1]["content"] == PLACEHOLDER
    assert SECRET not in json.dumps(messages[1])
    assert _has_ephemeral_sensitive_context(messages) is False
    assert SECRET not in json.dumps(messages[2])
    assert _durable_message_copy(messages[4])["reasoning"] == "keep"


def test_observer_surface_gets_placeholder_not_private_history():
    assert _observer_safe_tool_result("slack_history", SECRET) == PLACEHOLDER
    assert _observer_safe_tool_result("read_file", "ordinary") == "ordinary"


def test_conversation_wrapper_seals_early_return_messages(monkeypatch):
    from agent import conversation_loop

    messages = [_ephemeral_message()]
    agent = SimpleNamespace(_session_messages=messages)
    monkeypatch.setattr(
        conversation_loop,
        "_run_conversation_impl",
        lambda *_args, **_kwargs: {"messages": messages, "completed": False},
    )

    result = conversation_loop.run_conversation(agent, "read above")

    assert result["messages"][0]["content"] == PLACEHOLDER
    assert SECRET not in json.dumps(result)


def test_conversation_wrapper_seals_cached_messages_when_impl_raises(monkeypatch):
    from agent import conversation_loop

    messages = [_ephemeral_message()]
    agent = SimpleNamespace(_session_messages=messages)

    def fail(*_args, **_kwargs):
        raise RuntimeError("provider failed")

    monkeypatch.setattr(conversation_loop, "_run_conversation_impl", fail)

    try:
        conversation_loop.run_conversation(agent, "read above")
    except RuntimeError as exc:
        assert str(exc) == "provider failed"
    else:
        raise AssertionError("expected provider failure")

    assert messages[0]["content"] == PLACEHOLDER
    assert SECRET not in json.dumps(messages)


def test_session_end_memory_and_context_engine_receive_durable_history(tmp_path):
    agent = _agent(tmp_path)
    agent._memory_manager = MagicMock()
    agent.context_compressor = MagicMock()
    messages = [_ephemeral_message()]

    agent.shutdown_memory_provider(messages)

    memory_messages = agent._memory_manager.on_session_end.call_args.args[0]
    compressor_messages = agent.context_compressor.on_session_end.call_args.args[1]
    assert SECRET not in json.dumps(memory_messages)
    assert SECRET not in json.dumps(compressor_messages)
    assert messages[0]["content"] == SECRET


def test_request_hook_payload_omits_sensitive_provider_messages(tmp_path):
    agent = _agent(tmp_path)
    tokens = set_session_vars(
        platform="slack",
        chat_id="C12345678",
        scope_id="T12345678",
        slack_history_authorized=True,
    )
    try:
        assert consume_slack_history_authorization() is True
        payload = agent._api_request_payload_for_hook(
            {"messages": [{"role": "tool", "content": SECRET}]}
        )
    finally:
        clear_session_vars(tokens)

    assert payload["body"]["messages"] == []
    assert payload["body"]["ephemeral_context_omitted"] is True
    assert SECRET not in json.dumps(payload)


def test_api_error_observability_hook_is_suppressed_for_sensitive_turn(
    tmp_path, monkeypatch
):
    agent = _agent(tmp_path)
    invoke = MagicMock()
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda _name: True)
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", invoke)
    tokens = set_session_vars(
        platform="slack",
        chat_id="C12345678",
        scope_id="T12345678",
        slack_history_authorized=True,
    )
    try:
        assert consume_slack_history_authorization() is True
        agent._invoke_api_request_error_hook(
            task_id="task-1",
            turn_id="turn-1",
            api_request_id="request-1",
            api_call_count=2,
            api_start_time=0.0,
            api_kwargs={"messages": [{"role": "tool", "content": SECRET}]},
            error_type="test",
            error_message="test",
        )
    finally:
        clear_session_vars(tokens)

    invoke.assert_not_called()


def test_context_compression_skips_history_with_sensitive_markers():
    from agent.conversation_compression import compress_context

    compressor = MagicMock()
    agent = SimpleNamespace(
        context_compressor=compressor,
        _cached_system_prompt="system",
    )
    messages = [_ephemeral_message()]

    returned, prompt = compress_context(agent, messages, "system")

    assert returned is messages
    assert prompt == "system"
    compressor.compress.assert_not_called()


def test_post_history_tool_call_is_blocked_before_dispatch(tmp_path, monkeypatch):
    from agent import relay_tools, tool_executor

    agent = _agent(tmp_path)
    dispatched = MagicMock(return_value="must not run")
    monkeypatch.setattr(
        "hermes_cli.middleware.apply_tool_request_middleware",
        lambda _name, args, **_kwargs: SimpleNamespace(payload=args, trace=[]),
    )
    monkeypatch.setattr(
        "hermes_cli.middleware.run_tool_execution_middleware",
        lambda _name, args, callback, **_kwargs: callback(args),
    )
    monkeypatch.setattr(
        "hermes_cli.plugins.resolve_pre_tool_block",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        relay_tools,
        "execute",
        lambda name, args, callback, **kwargs: (callback(args), args),
    )
    monkeypatch.setattr(
        tool_executor,
        "_emit_terminal_post_tool_call",
        lambda *_args, **_kwargs: None,
    )
    tokens = set_session_vars(
        platform="slack",
        chat_id="C12345678",
        scope_id="T12345678",
        slack_history_authorized=True,
    )
    try:
        assert consume_slack_history_authorization() is True
        outcome = tool_executor._run_agent_tool_execution_middleware(
            agent,
            function_name="terminal",
            function_args={"command": "touch forbidden"},
            effective_task_id="task-1",
            tool_call_id="call-2",
            execute=dispatched,
        )
    finally:
        clear_session_vars(tokens)

    assert outcome.blocked is True
    assert "No further tools" in json.loads(outcome.result)["error"]
    dispatched.assert_not_called()


def test_slack_history_bypasses_tool_middleware_and_relay(tmp_path, monkeypatch):
    from agent import relay_tools, tool_executor

    agent = _agent(tmp_path)
    monkeypatch.setattr(
        "hermes_cli.plugins.resolve_pre_tool_block",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(tool_executor, "_begin_tool_execution", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "hermes_cli.middleware.apply_tool_request_middleware",
        lambda *_a, **_k: pytest.fail("private history reached request middleware"),
    )
    monkeypatch.setattr(
        "hermes_cli.middleware.run_tool_execution_middleware",
        lambda *_a, **_k: pytest.fail("private history reached execution middleware"),
    )
    monkeypatch.setattr(
        relay_tools,
        "execute",
        lambda *_a, **_k: pytest.fail("private history reached Relay"),
    )

    outcome = tool_executor._run_agent_tool_execution_middleware(
        agent,
        function_name="slack_history",
        function_args={"limit": 8},
        effective_task_id="task-1",
        tool_call_id="call-1",
        execute=lambda args: SECRET,
    )

    assert outcome.result == SECRET
    assert outcome.dispatched is True
    assert outcome.blocked is False
    assert outcome.middleware_trace == []


def test_slack_history_is_blocked_for_multi_provider_moa(tmp_path, monkeypatch):
    from agent import tool_executor

    agent = _agent(tmp_path)
    agent.provider = "moa"
    monkeypatch.setattr(
        tool_executor,
        "_emit_terminal_post_tool_call",
        lambda *_args, **_kwargs: None,
    )
    dispatched = MagicMock(return_value=SECRET)

    outcome = tool_executor._run_agent_tool_execution_middleware(
        agent,
        function_name="slack_history",
        function_args={"limit": 8},
        effective_task_id="task-1",
        tool_call_id="call-1",
        execute=dispatched,
    )

    assert outcome.blocked is True
    assert "multi-provider MoA" in json.loads(outcome.result)["error"]
    dispatched.assert_not_called()


def test_slack_history_is_blocked_for_per_turn_moa(tmp_path, monkeypatch):
    from agent import tool_executor
    from gateway.session_context import reset_moa_turn_active, set_moa_turn_active

    agent = _agent(tmp_path)
    agent.provider = "openai"
    monkeypatch.setattr(
        tool_executor,
        "_emit_terminal_post_tool_call",
        lambda *_args, **_kwargs: None,
    )
    dispatched = MagicMock(return_value=SECRET)
    token = set_moa_turn_active(True)
    try:
        outcome = tool_executor._run_agent_tool_execution_middleware(
            agent,
            function_name="slack_history",
            function_args={"limit": 8},
            effective_task_id="task-1",
            tool_call_id="call-1",
            execute=dispatched,
        )
    finally:
        reset_moa_turn_active(token)

    assert outcome.blocked is True
    assert "multi-provider MoA" in json.loads(outcome.result)["error"]
    dispatched.assert_not_called()
