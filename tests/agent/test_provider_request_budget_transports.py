from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.provider_request_budget import ProviderRequestBudgetExceeded


def _make_agent(limit=1):
    from run_agent import AIAgent

    config = {"agent": {"max_provider_requests_per_turn": limit}}
    with (
        patch("run_agent.OpenAI"),
        patch("hermes_cli.config.load_config", return_value=config),
        patch("hermes_cli.config.load_config_readonly", return_value=config),
    ):
        return AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )


def _chat_response(text="done"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text, tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=None,
    )


def test_nonstreaming_dispatch_reserves_one_request_and_reports_it():
    agent = _make_agent()
    request_client = MagicMock()
    request_client.chat.completions.create.return_value = _chat_response()

    with patch.object(
        agent, "_create_request_openai_client", return_value=request_client
    ):
        result = agent.run_conversation("hello")

    assert result["final_response"] == "done"
    assert result["provider_requests"] == 1
    assert result["provider_request_limit"] == 1
    assert result["provider_requests_remaining"] == 0
    assert result["provider_request_budget_exhausted"] is False
    assert agent.provider_request_budget.used == 1
    request_client.chat.completions.create.assert_called_once()


def test_budget_exhaustion_does_not_retry_or_activate_fallback():
    agent = _make_agent()
    calls = 0

    def capped_call(_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            agent.provider_request_budget.reserve(reason="first request")
        agent.provider_request_budget.reserve(reason="blocked request")

    agent._interruptible_api_call = capped_call
    agent._api_max_retries = 3
    agent._try_activate_fallback = MagicMock(return_value=False)

    with patch("agent.conversation_loop.jittered_backoff", return_value=0):
        result = agent.run_conversation("hello")

    assert "provider request budget exhausted" in result["final_response"]
    assert result["provider_requests"] == 1
    assert result["provider_request_budget_exhausted"] is True
    assert result["failure_reason"] == "provider_request_budget_exhausted"
    assert calls == 1
    agent._try_activate_fallback.assert_not_called()


def test_stream_retry_is_blocked_before_a_second_transport(monkeypatch):
    import httpx

    agent = _make_agent()
    request_client = MagicMock()
    request_client.chat.completions.create.side_effect = httpx.RemoteProtocolError(
        "peer closed connection"
    )
    monkeypatch.setenv("HERMES_STREAM_RETRIES", "2")

    with (
        patch.object(
            agent, "_create_request_openai_client", return_value=request_client
        ),
        pytest.raises(ProviderRequestBudgetExceeded),
    ):
        agent._interruptible_streaming_api_call(
            {
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
            }
        )

    assert agent.provider_request_budget.used == 1
    request_client.chat.completions.create.assert_called_once()


def test_bedrock_stream_fallback_is_blocked_before_second_transport():
    agent = _make_agent()
    agent.api_mode = "bedrock_converse"
    agent.provider = "bedrock"
    agent.model = "anthropic.claude-test"
    client = MagicMock()
    client.converse_stream.side_effect = RuntimeError("stream denied")

    with (
        patch(
            "agent.bedrock_adapter._get_bedrock_runtime_client",
            return_value=client,
        ),
        patch(
            "agent.bedrock_adapter.is_streaming_access_denied_error",
            return_value=True,
        ),
        pytest.raises(ProviderRequestBudgetExceeded),
    ):
        agent._interruptible_streaming_api_call(
            {
                "modelId": agent.model,
                "messages": [{"role": "user", "content": [{"text": "hi"}]}],
                "__bedrock_region__": "us-east-1",
            }
        )

    assert agent.provider_request_budget.used == 1
    client.converse_stream.assert_called_once()
    client.converse.assert_not_called()


def test_codex_retry_is_blocked_before_a_second_transport():
    import httpx

    agent = _make_agent()
    agent.api_mode = "codex_responses"
    client = MagicMock()
    client.responses.create.side_effect = httpx.RemoteProtocolError(
        "peer closed connection"
    )

    def execute_relay_wrapper(request, callback, **_kwargs):
        return callback(request)

    with (
        patch(
            "agent.relay_llm.stream",
            side_effect=execute_relay_wrapper,
        ) as relay_execute,
        pytest.raises(ProviderRequestBudgetExceeded),
    ):
        agent._run_codex_stream({}, client=client)

    assert agent.provider_request_budget.used == 1
    client.responses.create.assert_called_once()
    assert relay_execute.call_count == 2


def test_anthropic_stream_to_create_fallback_counts_each_transport():
    from agent.anthropic_adapter import create_anthropic_message
    from agent.provider_request_budget import capture_provider_request_reservation

    agent = _make_agent()
    agent.api_mode = "anthropic_messages"
    agent.provider = "anthropic"
    client = MagicMock()
    client.messages.stream.side_effect = RuntimeError("stream unavailable")
    reserve = capture_provider_request_reservation(agent)

    with (
        patch(
            "agent.anthropic_adapter._is_stream_unavailable_error",
            return_value=True,
        ),
        pytest.raises(ProviderRequestBudgetExceeded),
    ):
        create_anthropic_message(
            client,
            {"model": "test", "messages": []},
            before_request=reserve,
        )

    assert agent.provider_request_budget.used == 1
    client.messages.stream.assert_called_once()
    client.messages.create.assert_not_called()


def test_max_iteration_summary_is_blocked_before_a_second_request():
    agent = _make_agent()
    agent.provider_request_budget.reserve(reason="initial request")
    agent.client.chat.completions.create.reset_mock()
    agent.client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="unexpected"))]
    )

    with pytest.raises(ProviderRequestBudgetExceeded):
        agent._handle_max_iterations(
            [{"role": "user", "content": "do work"}], 1
        )

    assert agent.provider_request_budget.used == 1
    agent.client.chat.completions.create.assert_not_called()


def test_codex_app_server_reserves_only_before_turn_start():
    from agent.provider_request_budget import capture_provider_request_reservation
    from agent.transports.codex_app_server_session import CodexAppServerSession

    agent = _make_agent()
    agent.provider_request_budget.reserve(reason="prior request")
    client = MagicMock()
    session = CodexAppServerSession()
    session._client = client
    session._thread_id = "thread-1"

    with pytest.raises(ProviderRequestBudgetExceeded):
        session.run_turn(
            "hello",
            before_request=capture_provider_request_reservation(agent),
        )

    client.request.assert_not_called()


def test_codex_app_server_compaction_reserves_before_compact_start():
    from agent.provider_request_budget import capture_provider_request_reservation
    from agent.transports.codex_app_server_session import CodexAppServerSession

    agent = _make_agent()
    agent.provider_request_budget.reserve(reason="prior request")
    client = MagicMock()
    session = CodexAppServerSession()
    session._client = client
    session._thread_id = "thread-1"

    with pytest.raises(ProviderRequestBudgetExceeded):
        session.compact_thread(
            before_request=capture_provider_request_reservation(agent),
        )

    client.request.assert_not_called()


def test_codex_native_compaction_consumes_final_slot_before_turn_start():
    from agent.provider_request_budget import capture_provider_request_reservation
    from agent.transports.codex_app_server import CodexAppServerError
    from agent.transports.codex_app_server_session import CodexAppServerSession

    agent = _make_agent()
    reserve_request = capture_provider_request_reservation(agent)
    client = MagicMock()
    client.request.side_effect = CodexAppServerError(-1, "synthetic compaction stop")
    session = CodexAppServerSession()
    session._client = client
    session._thread_id = "thread-1"

    session.compact_thread(before_request=reserve_request)

    assert agent.provider_request_budget.used == 1
    client.request.assert_called_once()
    client.reset_mock()
    client.request.side_effect = None

    with pytest.raises(ProviderRequestBudgetExceeded):
        session.run_turn("hello", before_request=reserve_request)

    client.request.assert_not_called()


def test_universal_turn_boundary_preserves_provider_budget_exit_reason():
    agent = _make_agent()
    agent._session_messages = [{"role": "assistant", "content": "prior turn"}]
    agent._save_trajectory = MagicMock()
    agent._cleanup_task_resources = MagicMock()
    agent._persist_session = MagicMock()

    def exhaust_budget(*_args, **_kwargs):
        agent.provider_request_budget.reserve(reason="idle compaction")
        agent.provider_request_budget.reserve(reason="preflight compaction")

    with patch(
        "agent.conversation_loop.run_conversation",
        side_effect=exhaust_budget,
    ):
        result = agent.run_conversation("hello")

    assert result["failed"] is True
    assert result["turn_exit_reason"] == "provider_request_budget_exhausted"
    assert result["failure_reason"] == "provider_request_budget_exhausted"
    assert result["provider_request_budget_exhausted"] is True
    assert result["provider_requests"] == 1
    assert result["api_calls"] == 0
    assert result["messages"] == [
        {"role": "assistant", "content": "prior turn"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": result["final_response"]},
    ]
    agent._save_trajectory.assert_called_once()
    agent._cleanup_task_resources.assert_called_once()
    agent._persist_session.assert_called_once()
    assert agent._persist_session.call_args.args[0] == result["messages"]
