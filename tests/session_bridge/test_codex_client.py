from __future__ import annotations

from collections.abc import Callable
import threading
from typing import Any

import pytest

from agent.transports.codex_app_server import (
    CodexAppServerError,
    CodexRequestCancelled,
)
from session_bridge.codex_client import RecoveringCodexAppServerClient


class _FakeClient:
    def __init__(self, responses: dict[str, list[object]]) -> None:
        self.responses = responses
        self.initialize_calls: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any], float]] = []
        self.closed = False
        self._initialized = False

    def initialize(self, **kwargs: Any) -> dict[str, Any]:
        self.initialize_calls.append(kwargs)
        self._initialized = True
        return {"userAgent": "fake"}

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        self.calls.append((method, params or {}, timeout))
        value = self.responses[method].pop(0)
        if isinstance(value, BaseException):
            raise value
        assert isinstance(value, dict)
        return value

    def close(self) -> None:
        self.closed = True


def _factory(
    clients: list[_FakeClient],
) -> tuple[Callable[[], _FakeClient], list[_FakeClient]]:
    created: list[_FakeClient] = []

    def create() -> _FakeClient:
        client = clients[len(created)]
        created.append(client)
        return client

    return create, created


def test_read_request_recycles_dead_client_and_retries_once() -> None:
    first = _FakeClient({"thread/list": [TimeoutError("dead transport")]})
    second = _FakeClient({"thread/list": [{"data": [{"id": "fresh"}]}]})
    factory, created = _factory([first, second])
    client = RecoveringCodexAppServerClient(factory)

    client.initialize(capabilities={"experimentalApi": True})
    result = client.request("thread/list", {"archived": False}, timeout=5.0)

    assert result == {"data": [{"id": "fresh"}]}
    assert created == [first, second]
    assert first.closed is True
    assert second.initialize_calls == [
        {
            "capabilities": {"experimentalApi": True},
            "timeout": pytest.approx(5.0),
        }
    ]
    assert second.calls == [("thread/list", {"archived": False}, pytest.approx(5.0))]


def test_lock_queue_wait_does_not_consume_transport_request_budget() -> None:
    clock = {"now": 100.0}
    current = _FakeClient({"thread/list": [{"data": []}]})
    replacement = _FakeClient({"thread/list": [{"data": []}]})
    factory, created = _factory([current, replacement])
    client = RecoveringCodexAppServerClient(
        factory,
        monotonic=lambda: clock["now"],
    )
    result: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def request() -> None:
        try:
            result.append(client.request("thread/list", {}, timeout=30.0))
        except BaseException as exc:
            errors.append(exc)

    with client._lock:
        worker = threading.Thread(target=request)
        worker.start()
        clock["now"] += 31.0
    worker.join(timeout=1.0)

    assert worker.is_alive() is False
    assert errors == []
    assert result == [{"data": []}]
    assert created == [current]
    assert current.calls == [("thread/list", {}, 30.0)]
    assert current.closed is False


def test_read_replay_uses_only_the_remaining_logical_timeout() -> None:
    clock = {"now": 100.0}

    class AdvancingClient(_FakeClient):
        def request(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            timeout: float = 30.0,
        ) -> dict[str, Any]:
            if method == "thread/list":
                clock["now"] += 3.0
            return super().request(method, params, timeout)

    first = AdvancingClient({"thread/list": [TimeoutError("dead transport")]})
    second = AdvancingClient({"thread/list": [{"data": []}]})
    factory, _created = _factory([first, second])
    client = RecoveringCodexAppServerClient(
        factory,
        monotonic=lambda: clock["now"],
    )

    client.initialize(capabilities={"experimentalApi": True})
    assert client.request("thread/list", {}, timeout=10.0) == {"data": []}

    assert first.calls == [("thread/list", {}, 10.0)]
    assert second.calls == [("thread/list", {}, 7.0)]


def test_success_arriving_after_logical_deadline_recycles_without_replay() -> None:
    clock = {"now": 100.0}

    class LateClient(_FakeClient):
        def request(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            timeout: float = 30.0,
        ) -> dict[str, Any]:
            clock["now"] += timeout + 0.1
            return super().request(method, params, timeout)

    first = LateClient({"thread/list": [{"data": [{"id": "late"}]}]})
    second = _FakeClient({"thread/list": [{"data": [{"id": "fresh"}]}]})
    factory, created = _factory([first, second])
    client = RecoveringCodexAppServerClient(
        factory,
        monotonic=lambda: clock["now"],
    )
    client.initialize(capabilities={"experimentalApi": True})

    with pytest.raises(TimeoutError, match="request deadline exhausted"):
        client.request("thread/list", {}, timeout=5.0)

    assert created == [first, second]
    assert first.calls == [("thread/list", {}, 5.0)]
    assert first.closed is True
    assert second.calls == []

    assert client.request("thread/list", {}) == {"data": [{"id": "fresh"}]}
    assert second.initialize_calls == [
        {
            "capabilities": {"experimentalApi": True},
            "timeout": pytest.approx(30.0),
        }
    ]
    assert second.calls == [("thread/list", {}, pytest.approx(30.0))]


def test_late_success_preserves_timeout_when_replacement_factory_fails() -> None:
    clock = {"now": 100.0}

    class LateClient(_FakeClient):
        def request(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            timeout: float = 30.0,
        ) -> dict[str, Any]:
            clock["now"] += timeout + 0.1
            return super().request(method, params, timeout)

    first = LateClient({"thread/list": [{"data": [{"id": "late"}]}]})
    recovered = _FakeClient({"thread/list": [{"data": [{"id": "fresh"}]}]})
    factory_calls = 0

    def factory() -> _FakeClient:
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 1:
            return first
        if factory_calls == 2:
            raise OSError("replacement factory failed")
        return recovered

    client = RecoveringCodexAppServerClient(
        factory,
        monotonic=lambda: clock["now"],
    )
    client.initialize(capabilities={"experimentalApi": True})

    with pytest.raises(TimeoutError, match="request deadline exhausted"):
        client.request("thread/list", {}, timeout=5.0)

    assert first.closed is True
    assert first.calls == [("thread/list", {}, 5.0)]
    assert factory_calls == 2

    assert client.request("thread/list", {}) == {"data": [{"id": "fresh"}]}
    assert factory_calls == 3
    assert recovered.initialize_calls == [
        {
            "capabilities": {"experimentalApi": True},
            "timeout": pytest.approx(30.0),
        }
    ]


def test_late_success_preserves_timeout_when_replacement_close_fails() -> None:
    clock = {"now": 100.0}

    class LateCloseFailingClient(_FakeClient):
        def request(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            timeout: float = 30.0,
        ) -> dict[str, Any]:
            clock["now"] += timeout + 0.1
            return super().request(method, params, timeout)

        def close(self) -> None:
            self.closed = True
            raise OSError("close failed")

    first = LateCloseFailingClient({
        "thread/list": [{"data": [{"id": "late"}]}],
    })
    recovered = _FakeClient({"thread/list": [{"data": [{"id": "fresh"}]}]})
    factory, created = _factory([first, recovered])
    client = RecoveringCodexAppServerClient(
        factory,
        monotonic=lambda: clock["now"],
    )
    client.initialize(capabilities={"experimentalApi": True})

    with pytest.raises(TimeoutError, match="request deadline exhausted"):
        client.request("thread/list", {}, timeout=5.0)

    assert created == [first, recovered]
    assert first.closed is True
    assert client.request("thread/list", {}) == {"data": [{"id": "fresh"}]}


def test_read_replay_is_skipped_when_logical_timeout_is_exhausted() -> None:
    clock = {"now": 100.0}

    class ExhaustingClient(_FakeClient):
        def request(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            timeout: float = 30.0,
        ) -> dict[str, Any]:
            clock["now"] += timeout
            return super().request(method, params, timeout)

    first = ExhaustingClient({"thread/list": [TimeoutError("dead transport")]})
    second = _FakeClient({"thread/list": [{"data": []}]})
    factory, created = _factory([first, second])
    client = RecoveringCodexAppServerClient(
        factory,
        monotonic=lambda: clock["now"],
    )
    client.initialize(capabilities={"experimentalApi": True})

    with pytest.raises(TimeoutError, match="request deadline exhausted"):
        client.request("thread/list", {}, timeout=5.0)

    assert created == [first, second]
    assert second.calls == []


def test_cancellation_is_not_recovered_or_replayed() -> None:
    stop = threading.Event()
    stop.set()
    first = _FakeClient({"thread/list": [RuntimeError("must not request")]})
    factory, created = _factory([first])
    client = RecoveringCodexAppServerClient(factory, cancel_event=stop)

    with pytest.raises(CodexRequestCancelled, match="request cancelled"):
        client.request("thread/list", {})

    assert created == [first]
    assert first.calls == []
    assert first.closed is False


def test_per_request_cancellation_is_forwarded_without_recovery_or_replay() -> None:
    stop = threading.Event()

    class CancellingClient(_FakeClient):
        def request(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            timeout: float = 30.0,
            *,
            cancel_event: threading.Event | None = None,
        ) -> dict[str, Any]:
            self.calls.append((method, params or {}, timeout))
            assert cancel_event is stop
            stop.set()
            raise CodexRequestCancelled("codex app-server request cancelled")

    first = CancellingClient({"thread/list": []})
    factory, created = _factory([first])
    client = RecoveringCodexAppServerClient(factory)

    with pytest.raises(CodexRequestCancelled, match="request cancelled"):
        client.request("thread/list", {}, cancel_event=stop)

    assert created == [first]
    assert len(first.calls) == 1
    assert first.closed is False


def test_per_initialize_cancellation_is_forwarded_but_not_retained_for_replay() -> None:
    stop = threading.Event()

    class InitializingClient(_FakeClient):
        def initialize(
            self,
            *,
            cancel_event: threading.Event | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            expected = stop if self is first else None
            assert cancel_event is expected
            return super().initialize(**kwargs)

    first = InitializingClient({"thread/list": [TimeoutError("dead transport")]})
    second = InitializingClient({"thread/list": [{"data": []}]})
    factory, created = _factory([first, second])
    client = RecoveringCodexAppServerClient(factory)

    client.initialize(
        capabilities={"experimentalApi": True},
        cancel_event=stop,
    )
    assert client.request("thread/list", {}) == {"data": []}

    assert created == [first, second]
    assert first.initialize_calls == [{"capabilities": {"experimentalApi": True}}]
    assert second.initialize_calls == [
        {
            "capabilities": {"experimentalApi": True},
            "timeout": pytest.approx(30.0),
        }
    ]


def test_notification_delegates_to_current_client() -> None:
    first = _FakeClient({})
    first.take_notification = lambda timeout=0.0: {"timeout": timeout}
    factory, _created = _factory([first])
    client = RecoveringCodexAppServerClient(factory)

    assert client.take_notification(timeout=0.25) == {"timeout": 0.25}


@pytest.mark.parametrize("operation", ("initialize", "request"))
def test_closed_client_cannot_resurrect_transport(operation: str) -> None:
    first = _FakeClient({"thread/list": [{"data": []}]})
    replacement = _FakeClient({"thread/list": [{"data": []}]})
    factory, created = _factory([first, replacement])
    client = RecoveringCodexAppServerClient(factory)
    client.close()

    with pytest.raises(RuntimeError, match="client is closed"):
        if operation == "initialize":
            client.initialize(capabilities={"experimentalApi": True})
        else:
            client.request("thread/list", {})

    assert created == [first]
    assert first.closed is True
    assert replacement.initialize_calls == []
    assert replacement.calls == []


def test_per_request_cancellation_stops_before_replacement_factory() -> None:
    stop = threading.Event()

    class ClosingClient(_FakeClient):
        def close(self) -> None:
            super().close()
            stop.set()

    first = ClosingClient({"thread/list": [TimeoutError("dead transport")]})
    factory, created = _factory([first, _FakeClient({"thread/list": [{"data": []}]})])
    client = RecoveringCodexAppServerClient(factory)

    with pytest.raises(CodexRequestCancelled, match="request cancelled"):
        client.request("thread/list", {}, cancel_event=stop)

    assert created == [first]
    assert first.closed is True


def test_inflight_cancellation_is_not_recovered_or_replayed() -> None:
    stop = threading.Event()

    class CancellingClient(_FakeClient):
        def request(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            timeout: float = 30.0,
            *,
            cancel_event: threading.Event | None = None,
        ) -> dict[str, Any]:
            self.calls.append((method, params or {}, timeout))
            assert cancel_event is stop
            stop.set()
            raise CodexRequestCancelled("codex app-server request cancelled")

    first = CancellingClient({"thread/list": []})
    factory, created = _factory([first])
    client = RecoveringCodexAppServerClient(factory, cancel_event=stop)

    with pytest.raises(CodexRequestCancelled, match="request cancelled"):
        client.request("thread/list", {})

    assert created == [first]
    assert len(first.calls) == 1
    assert first.closed is False


def test_mutating_request_recycles_but_never_replays_unknown_outcome() -> None:
    first = _FakeClient({"thread/start": [TimeoutError("unknown outcome")]})
    second = _FakeClient({
        "thread/start": [{"thread": {"id": "duplicate"}}],
        "thread/list": [{"data": []}],
    })
    factory, created = _factory([first, second])
    client = RecoveringCodexAppServerClient(
        factory,
        monotonic=lambda: 100.0,
    )
    client.initialize(capabilities={"experimentalApi": True})

    with pytest.raises(TimeoutError, match="unknown outcome"):
        client.request("thread/start", {"cwd": "C:/work"}, timeout=5.0)

    assert created == [first, second]
    assert first.closed is True
    assert second.initialize_calls == []
    assert second.calls == []

    assert client.request("thread/list", {"archived": False}) == {"data": []}
    assert second.initialize_calls == [
        {
            "capabilities": {"experimentalApi": True},
            "timeout": pytest.approx(30.0),
        }
    ]


def test_exhausted_mutation_preserves_the_original_unknown_outcome() -> None:
    clock = {"now": 100.0}

    class ExhaustingClient(_FakeClient):
        def request(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            timeout: float = 30.0,
        ) -> dict[str, Any]:
            clock["now"] += timeout
            return super().request(method, params, timeout)

        def close(self, timeout: float = 3.0) -> None:
            self.closed = True

    first = ExhaustingClient({
        "thread/start": [TimeoutError("original unknown outcome")],
    })
    second = _FakeClient({"thread/list": [{"data": []}]})
    factory, created = _factory([first, second])
    client = RecoveringCodexAppServerClient(
        factory,
        monotonic=lambda: clock["now"],
    )
    client.initialize(capabilities={"experimentalApi": True})

    with pytest.raises(TimeoutError, match="original unknown outcome"):
        client.request("thread/start", {"cwd": "C:/work"}, timeout=5.0)

    assert created == [first, second]
    assert first.closed is True
    assert second.calls == []


def test_mutation_preserves_unknown_outcome_when_replacement_fails() -> None:
    first = _FakeClient({
        "thread/start": [TimeoutError("original unknown outcome")],
    })
    factory_calls = 0

    def factory() -> _FakeClient:
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 1:
            return first
        raise OSError("replacement factory failed")

    client = RecoveringCodexAppServerClient(factory)

    with pytest.raises(TimeoutError, match="original unknown outcome"):
        client.request("thread/start", {"cwd": "C:/work"}, timeout=5.0)

    assert first.closed is True
    assert factory_calls == 2


def test_protocol_failure_is_not_hidden_by_transport_recovery() -> None:
    first = _FakeClient(
        {
            "thread/list": [
                CodexAppServerError(code=-32602, message="invalid params")
            ]
        }
    )
    factory, created = _factory([first])
    client = RecoveringCodexAppServerClient(factory)
    client.initialize(capabilities={"experimentalApi": True})

    with pytest.raises(CodexAppServerError, match="invalid params"):
        client.request("thread/list", {"archived": False}, timeout=5.0)

    assert created == [first]
    assert first.closed is False
