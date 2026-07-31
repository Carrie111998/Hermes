"""Regression coverage for the inline stale watchdog (#75222)."""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.chat_completion_helpers import direct_api_call, interruptible_api_call


def _make_agent(*, stale_timeout=0.03):
    agent = SimpleNamespace(
        platform="subagent",
        api_mode="chat_completions",
        provider="openrouter",
        model="test/model",
        _interrupt_requested=False,
        _consecutive_stale_streams=0,
        _active_request_abort=None,
        _touch_activity=MagicMock(),
        _buffer_status=MagicMock(),
        _abort_request_openai_client=MagicMock(),
        _close_request_openai_client=MagicMock(),
        _compute_non_stream_stale_timeout=MagicMock(return_value=stale_timeout),
    )
    return agent


def test_delegated_inline_call_stale_abort_preserves_transport_error_then_recovers():
    """The stale policy applies without moving delegated dispatch off-thread."""
    agent = _make_agent()
    request_released = threading.Event()
    request_thread_ids = []
    caller_thread_id = threading.get_ident()

    stale_client = MagicMock()

    def _stale_request(**_kwargs):
        request_thread_ids.append(threading.get_ident())
        assert request_released.wait(timeout=2)
        raise RuntimeError("socket closed by watchdog")

    stale_client.chat.completions.create.side_effect = _stale_request
    healthy_client = MagicMock()
    healthy_response = SimpleNamespace(id="healthy")
    healthy_client.chat.completions.create.return_value = healthy_response
    agent._create_request_openai_client = MagicMock(
        side_effect=[stale_client, healthy_client]
    )

    def _abort(client, *, reason):
        assert client is stale_client
        assert reason == "stale_call_kill"
        request_released.set()

    agent._abort_request_openai_client.side_effect = _abort

    with pytest.raises(RuntimeError, match="socket closed by watchdog"):
        interruptible_api_call(agent, {"model": "test/model", "messages": []})

    assert request_thread_ids == [caller_thread_id]
    assert agent._consecutive_stale_streams == 1
    agent._close_request_openai_client.assert_called_once_with(
        stale_client, reason="request_error_cleanup"
    )

    agent._compute_non_stream_stale_timeout.return_value = 1.0
    response = interruptible_api_call(
        agent, {"model": "test/model", "messages": []}
    )

    assert response is healthy_response
    assert agent._consecutive_stale_streams == 0
    assert agent._close_request_openai_client.call_args_list[-1].kwargs == {
        "reason": "request_complete"
    }


def test_late_timer_callback_cannot_abort_reused_client():
    """A callback from a completed request is inert during the next request."""

    class CapturedTimer:
        instances = []

        def __init__(self, interval, function):
            self.interval = interval
            self.function = function
            self.daemon = False
            self.cancelled = False
            self.instances.append(self)

        def start(self):
            return None

        def cancel(self):
            self.cancelled = True

    agent = _make_agent(stale_timeout=1.0)
    reused_client = MagicMock()
    second_started = threading.Event()
    release_second = threading.Event()
    calls = {"count": 0}

    def _request(**_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return SimpleNamespace(id="first")
        second_started.set()
        assert release_second.wait(timeout=2)
        return SimpleNamespace(id="second")

    reused_client.chat.completions.create.side_effect = _request
    agent._create_request_openai_client = MagicMock(return_value=reused_client)

    with patch(
        "agent.chat_completion_helpers.threading.Timer", CapturedTimer
    ):
        first = direct_api_call(agent, {"model": "test/model", "messages": []})
        result = {}
        worker = threading.Thread(
            target=lambda: result.setdefault(
                "response",
                direct_api_call(agent, {"model": "test/model", "messages": []}),
            )
        )
        worker.start()
        assert second_started.wait(timeout=1)

        CapturedTimer.instances[0].function()
        agent._abort_request_openai_client.assert_not_called()

        release_second.set()
        worker.join(timeout=2)

    assert not worker.is_alive()
    assert first.id == "first"
    assert result["response"].id == "second"
    assert CapturedTimer.instances[0].cancelled is True


def test_timer_firing_before_client_registration_aborts_late_client():
    """A stale client factory must not dispatch after its watchdog was spent."""

    class ImmediateTimer:
        def __init__(self, _interval, function):
            self.function = function
            self.daemon = False

        def start(self):
            self.function()

        def cancel(self):
            return None

    agent = _make_agent(stale_timeout=0.0)
    client = MagicMock()
    client.chat.completions.create.return_value = SimpleNamespace(id="too-late")
    agent._create_request_openai_client = MagicMock(return_value=client)

    with patch("agent.chat_completion_helpers.threading.Timer", ImmediateTimer):
        with pytest.raises(TimeoutError, match="timed out"):
            direct_api_call(agent, {"model": "test/model", "messages": []})

    agent._abort_request_openai_client.assert_called_once_with(
        client, reason="stale_call_kill"
    )
    agent._close_request_openai_client.assert_called_once_with(
        client, reason="request_error_cleanup"
    )
    client.chat.completions.create.assert_not_called()
    assert agent._consecutive_stale_streams == 1


def test_stale_abort_is_reenforced_across_registration_to_dispatch_race():
    """A pre-socket abort is retried after dispatch starts opening the socket."""

    class CapturedTimer:
        instance = None

        def __init__(self, _interval, function):
            self.function = function
            self.daemon = False
            CapturedTimer.instance = self

        def start(self):
            return None

        def cancel(self):
            return None

    before_create = threading.Event()
    allow_create_lookup = threading.Event()
    request_released = threading.Event()
    abort_count = {"value": 0}

    def _request(**_kwargs):
        assert request_released.wait(timeout=2)
        raise RuntimeError("socket closed by reenforced watchdog")

    class Completions:
        @property
        def create(self):
            # _make_client has returned, but the SDK request has not started:
            # model a timer firing during the final attribute lookup.
            before_create.set()
            assert allow_create_lookup.wait(timeout=2)
            return _request

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    agent = _make_agent(stale_timeout=1.0)
    agent._create_request_openai_client = MagicMock(return_value=client)

    def _abort(_client, *, reason):
        assert reason == "stale_call_kill"
        abort_count["value"] += 1
        if abort_count["value"] >= 2:
            request_released.set()

    agent._abort_request_openai_client.side_effect = _abort
    result = {}

    with patch("agent.chat_completion_helpers.threading.Timer", CapturedTimer):
        worker = threading.Thread(
            target=lambda: result.setdefault(
                "exception",
                pytest.raises(
                    RuntimeError,
                    direct_api_call,
                    agent,
                    {"model": "test/model", "messages": []},
                ).value,
            )
        )
        worker.start()
        assert before_create.wait(timeout=1)

        CapturedTimer.instance.function()
        allow_create_lookup.set()
        worker.join(timeout=2)

    assert not worker.is_alive()
    assert "reenforced watchdog" in str(result["exception"])
    assert abort_count["value"] >= 2
    assert agent._consecutive_stale_streams == 1


def test_interrupt_winning_before_timer_does_not_count_as_stale():
    """A cancelled request must not poison the provider stale circuit breaker."""

    class CapturedTimer:
        instance = None

        def __init__(self, _interval, function):
            self.function = function
            self.daemon = False
            self.cancelled = False
            CapturedTimer.instance = self

        def start(self):
            return None

        def cancel(self):
            self.cancelled = True

    agent = _make_agent(stale_timeout=1.0)
    request_started = threading.Event()
    release_request = threading.Event()
    client = MagicMock()

    def _request(**_kwargs):
        request_started.set()
        assert release_request.wait(timeout=2)
        raise RuntimeError("socket closed by interrupt")

    client.chat.completions.create.side_effect = _request
    agent._create_request_openai_client = MagicMock(return_value=client)
    result = {}

    with patch("agent.chat_completion_helpers.threading.Timer", CapturedTimer):
        worker = threading.Thread(
            target=lambda: result.setdefault(
                "exception",
                pytest.raises(
                    InterruptedError,
                    direct_api_call,
                    agent,
                    {"model": "test/model", "messages": []},
                ).value,
            )
        )
        worker.start()
        assert request_started.wait(timeout=1)

        agent._interrupt_requested = True
        agent._active_request_abort("interrupt_abort")
        CapturedTimer.instance.function()
        release_request.set()
        worker.join(timeout=2)

    assert not worker.is_alive()
    assert isinstance(result["exception"], InterruptedError)
    agent._abort_request_openai_client.assert_called_once_with(
        client, reason="interrupt_abort"
    )
    agent._buffer_status.assert_not_called()
    assert agent._consecutive_stale_streams == 0


def test_infinite_stale_timeout_disables_inline_timer():
    agent = _make_agent(stale_timeout=float("inf"))
    client = MagicMock()
    response = SimpleNamespace(id="local")
    client.chat.completions.create.return_value = response
    agent._create_request_openai_client = MagicMock(return_value=client)

    with patch(
        "agent.chat_completion_helpers.threading.Timer",
        side_effect=AssertionError("timer must stay disabled"),
    ):
        assert direct_api_call(agent, {"model": "test/model", "messages": []}) is response
