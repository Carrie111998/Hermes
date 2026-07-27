from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from agent.transports.codex_app_server import CodexAppServerError
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
        {"capabilities": {"experimentalApi": True}}
    ]
    assert second.calls == [("thread/list", {"archived": False}, 5.0)]


def test_mutating_request_recycles_but_never_replays_unknown_outcome() -> None:
    first = _FakeClient({"thread/start": [TimeoutError("unknown outcome")]})
    second = _FakeClient({
        "thread/start": [{"thread": {"id": "duplicate"}}],
        "thread/list": [{"data": []}],
    })
    factory, created = _factory([first, second])
    client = RecoveringCodexAppServerClient(factory)
    client.initialize(capabilities={"experimentalApi": True})

    with pytest.raises(TimeoutError, match="unknown outcome"):
        client.request("thread/start", {"cwd": "C:/work"}, timeout=5.0)

    assert created == [first, second]
    assert first.closed is True
    assert second.initialize_calls == []
    assert second.calls == []

    assert client.request("thread/list", {"archived": False}) == {"data": []}
    assert second.initialize_calls == [
        {"capabilities": {"experimentalApi": True}}
    ]


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
