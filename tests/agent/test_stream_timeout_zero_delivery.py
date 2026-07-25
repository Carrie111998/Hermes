"""Regression tests for zero-delivery stream timeout retry amplification."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest

from run_agent import AIAgent


_TIMEOUT_MARKER = "hermes_zero_delivery_stream_timeout"


def _make_agent() -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://example.com/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.api_mode = "chat_completions"
    agent._interrupt_requested = False
    return agent


def _timeout_cases() -> list[BaseException]:
    request = httpx.Request("POST", "https://example.com/v1/chat/completions")
    return [
        openai.APITimeoutError(request=request),
        httpx.ReadTimeout("read timeout", request=request),
        httpx.ConnectTimeout("connect timeout", request=request),
        httpx.PoolTimeout("pool timeout", request=request),
    ]


@pytest.mark.parametrize("timeout", _timeout_cases(), ids=lambda exc: type(exc).__name__)
def test_zero_delivery_timeout_is_marked_at_stream_retry_boundary(
    timeout: BaseException,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("HERMES_STREAM_RETRIES", "2")
    agent = _make_agent()
    request_client = MagicMock()
    request_client.chat.completions.create.side_effect = timeout
    agent._create_request_openai_client = MagicMock(return_value=request_client)

    with pytest.raises(type(timeout)) as caught:
        agent._interruptible_streaming_api_call({"model": "test/model", "messages": []})

    assert getattr(caught.value, _TIMEOUT_MARKER, False) is True
    assert request_client.chat.completions.create.call_count == 1


def test_post_delta_timeout_is_not_marked(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")
    agent = _make_agent()
    timeout = httpx.ReadTimeout(
        "read timeout",
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )

    def stream():
        yield SimpleNamespace(
            choices=[SimpleNamespace(
                index=0,
                delta=SimpleNamespace(
                    content="partial",
                    tool_calls=None,
                    reasoning_content=None,
                    reasoning=None,
                ),
                finish_reason=None,
            )],
            model="test/model",
            usage=None,
        )
        raise timeout

    request_client = MagicMock()
    request_client.chat.completions.create.return_value = stream()
    agent._create_request_openai_client = MagicMock(return_value=request_client)

    response = agent._interruptible_streaming_api_call(
        {"model": "test/model", "messages": []}
    )

    assert response.choices[0].finish_reason == "length"
    assert getattr(timeout, _TIMEOUT_MARKER, False) is False


def _marked_read_timeout() -> httpx.ReadTimeout:
    timeout = httpx.ReadTimeout(
        "read timeout",
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )
    setattr(timeout, _TIMEOUT_MARKER, True)
    return timeout


def _run_non_streaming_turn(agent: AIAgent) -> dict:
    agent._disable_streaming = True
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("agent.conversation_loop.jittered_backoff", return_value=0),
        patch("agent.conversation_loop.time.sleep"),
    ):
        return agent.run_conversation("hello")


def test_zero_delivery_timeout_uses_one_primary_recovery_then_fallback():
    agent = _make_agent()
    agent._api_max_retries = 3
    agent.client = MagicMock()
    agent.client.chat.completions.create.side_effect = (
        lambda **_kwargs: (_ for _ in ()).throw(_marked_read_timeout())
    )
    agent._try_recover_primary_transport = MagicMock(return_value=True)
    agent._has_pending_fallback = MagicMock(return_value=False)
    agent._try_activate_fallback = MagicMock(return_value=False)

    result = _run_non_streaming_turn(agent)

    assert result["failed"] is True
    assert agent.client.chat.completions.create.call_count == 2
    agent._try_recover_primary_transport.assert_called_once()
    agent._try_activate_fallback.assert_called_once()


def test_zero_delivery_timeout_attempts_configured_fallback_without_outer_retries():
    agent = _make_agent()
    agent._api_max_retries = 3
    agent.client = MagicMock()
    agent.client.chat.completions.create.side_effect = (
        lambda **_kwargs: (_ for _ in ()).throw(_marked_read_timeout())
    )
    agent._try_recover_primary_transport = MagicMock(return_value=False)
    agent._has_pending_fallback = MagicMock(return_value=True)
    agent._try_activate_fallback = MagicMock(return_value=False)

    result = _run_non_streaming_turn(agent)

    assert agent.client.chat.completions.create.call_count == 1
    agent._try_activate_fallback.assert_called_once()
    assert "timed out before delivering any response" in result["final_response"]
    assert "payload may be too large" in result["final_response"]


def test_successful_fallback_completes_without_timeout_diagnostic():
    agent = _make_agent()
    agent._api_max_retries = 3
    agent.client = MagicMock()
    fallback_response = SimpleNamespace(
        choices=[SimpleNamespace(
            index=0,
            message=SimpleNamespace(
                role="assistant",
                content="fallback worked",
                tool_calls=None,
                reasoning_content=None,
            ),
            finish_reason="stop",
        )],
        model="fallback/model",
        usage=None,
    )
    agent.client.chat.completions.create.side_effect = [
        _marked_read_timeout(),
        fallback_response,
    ]
    agent._try_recover_primary_transport = MagicMock(return_value=False)
    agent._has_pending_fallback = MagicMock(return_value=True)

    def activate_fallback() -> bool:
        agent._fallback_activated = True
        agent.provider = "fallback-provider"
        agent.model = "fallback/model"
        return True

    agent._try_activate_fallback = MagicMock(side_effect=activate_fallback)

    result = _run_non_streaming_turn(agent)

    assert result["completed"] is True
    assert result["final_response"] == "fallback worked"
    assert agent.client.chat.completions.create.call_count == 2
    agent._try_activate_fallback.assert_called_once()
