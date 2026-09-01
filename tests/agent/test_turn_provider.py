"""Behavior contracts for the extracted one-request provider boundary."""

from __future__ import annotations

import inspect
import threading
from types import SimpleNamespace

import pytest

from agent.turn_provider import ProviderCallContext, execute_provider_call


class _Agent:
    def __init__(self, *, stream_consumers=True, redirect=False):
        self.tools = [{"type": "function", "function": {"name": "read"}}]
        self._force_ascii_payload = False
        self.api_mode = "chat_completions"
        self._empty_content_retries = 0
        self._is_user_initiated_turn = False
        self.provider = "openai"
        self.base_url = "https://example.test/v1"
        self.model = "test-model"
        self.session_id = "session-1"
        self.platform = "cli"
        self.max_tokens = 256
        self.client = object()
        self.is_subagent = False
        self._fallback_index = 0
        self._stream_consumers = stream_consumers
        self._model_request_active = threading.Event()
        self._pending_redirect_lock = threading.RLock()
        self._pending_redirect = redirect
        self.streaming_calls = []
        self.non_stream_calls = []

    def _build_api_kwargs(self, messages, *, tools_for_api=None):
        return {
            "model": self.model,
            "messages": messages,
            "tools": self.tools if tools_for_api is None else tools_for_api,
        }

    def _is_copilot_url(self):
        return False

    def _is_codex_backend(self):
        return False

    def _is_openrouter_url(self):
        return False

    def _has_stream_consumers(self):
        return self._stream_consumers

    def _has_pending_redirect(self):
        return self._pending_redirect

    def _interruptible_streaming_api_call(self, kwargs, *, on_first_delta):
        self.streaming_calls.append(kwargs)
        on_first_delta()
        return SimpleNamespace(model="stream-result")

    def _interruptible_api_call(self, kwargs):
        self.non_stream_calls.append(kwargs)
        return SimpleNamespace(model="non-stream-result")

    def _api_request_payload_for_hook(self, kwargs):
        return {"body": kwargs}


def _context(**overrides):
    values = {
        "task_id": "task-1",
        "turn_id": "turn-1",
        "api_request_id": "turn-1:api:1",
        "original_user_message": "question",
        "conversation_messages": [{"role": "user", "content": "question"}],
        "api_call_count": 1,
        "retry_count": 0,
        "approx_input_tokens": 12,
        "request_char_count": 48,
        "started_at": 123.0,
    }
    values.update(overrides)
    return ProviderCallContext(**values)


def test_streaming_executes_once_and_clears_the_redirect_fence():
    agent = _Agent()
    first_deltas = []

    result = execute_provider_call(
        agent,
        [{"role": "user", "content": "question"}],
        tools_for_api=agent.tools,
        moa_prepared_request=None,
        context=_context(),
        on_first_delta=lambda: first_deltas.append("seen"),
    )

    assert result.response.model == "stream-result"
    assert len(agent.streaming_calls) == 1
    assert not agent.non_stream_calls
    assert first_deltas == ["seen"]
    assert agent._model_request_active.is_set() is False
    assert result.redirect_crossed_response is False


def test_host_gate_uses_one_non_streaming_relay_call(monkeypatch):
    from agent import relay_llm

    agent = _Agent()
    agent._host_streaming_allowed = False
    observed = []

    def relay(request, callback, **kwargs):
        observed.append((request, callback, kwargs))
        return callback(request)

    monkeypatch.setattr(relay_llm, "execute", relay)
    result = execute_provider_call(
        agent,
        [{"role": "user", "content": "question"}],
        tools_for_api=agent.tools,
        moa_prepared_request=None,
        context=_context(retry_count=2),
        on_first_delta=lambda: pytest.fail("non-streaming call emitted a delta"),
    )

    assert result.response.model == "non-stream-result"
    assert len(observed) == len(agent.non_stream_calls) == 1
    assert observed[0][2]["metadata"]["retry_count"] == 2
    assert not agent.streaming_calls


def test_request_middleware_and_hook_observe_the_same_outbound_request(monkeypatch):
    import hermes_cli.lifecycle as lifecycle
    import hermes_cli.middleware as middleware

    agent = _Agent()
    events = []

    def apply(request, **kwargs):
        rewritten = {**request, "middleware": "applied"}
        return SimpleNamespace(
            payload=rewritten,
            original_payload=request,
            trace=[{"source": "test"}],
        )

    def execute(request, next_call, **kwargs):
        events.append(("execute", request, kwargs))
        return next_call(request)

    monkeypatch.setattr(middleware, "apply_llm_request_middleware", apply)
    monkeypatch.setattr(middleware, "run_llm_execution_middleware", execute)
    monkeypatch.setattr(lifecycle, "has_hook", lambda name: name == "pre_api_request")
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda name, **kwargs: events.append((name, kwargs)))

    result = execute_provider_call(
        agent,
        [{"role": "system", "content": "system"}, {"role": "user", "content": "q"}],
        tools_for_api=agent.tools,
        moa_prepared_request=None,
        context=_context(),
        on_first_delta=lambda: None,
    )

    hook = next(event for event in events if event[0] == "pre_api_request")[1]
    execution = next(event for event in events if event[0] == "execute")
    assert hook["request"]["body"]["middleware"] == "applied"
    assert hook["system_prompt"] == "system"
    assert execution[1]["middleware"] == result.api_kwargs["middleware"] == "applied"
    assert execution[2]["middleware_trace"] == [{"source": "test"}]


def test_crossed_redirect_is_reported_after_request_active_flag_is_cleared():
    agent = _Agent(redirect=True)

    result = execute_provider_call(
        agent,
        [{"role": "user", "content": "question"}],
        tools_for_api=agent.tools,
        moa_prepared_request=None,
        context=_context(),
        on_first_delta=lambda: None,
    )

    assert result.redirect_crossed_response is True
    assert agent._model_request_active.is_set() is False


def test_exception_propagates_to_the_existing_retry_owner():
    agent = _Agent()

    def boom(*_args, **_kwargs):
        raise RuntimeError("provider failed")

    agent._interruptible_streaming_api_call = boom
    with pytest.raises(RuntimeError, match="provider failed"):
        execute_provider_call(
            agent,
            [{"role": "user", "content": "question"}],
            tools_for_api=agent.tools,
            moa_prepared_request=None,
            context=_context(),
            on_first_delta=lambda: None,
        )
    assert agent._model_request_active.is_set() is False


def test_conversation_loop_delegates_single_call_execution_to_turn_provider():
    from agent import conversation_loop

    source = inspect.getsource(conversation_loop.run_conversation)
    assert "execute_provider_call(" in source
    assert "run_llm_execution_middleware(" not in source
