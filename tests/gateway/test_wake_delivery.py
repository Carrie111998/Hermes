"""Tests for gateway/wake.py — background wake delivery.

Two strategies:
* push-capable adapters keep the synthetic MessageEvent / handle_message path;
* the stateless API server (supports_async_delivery=False) self-POSTs
  /v1/chat/completions with the RAW session id in X-Hermes-Session-Id, so the
  wake turn resumes the REAL session instead of a parallel invisible one
  keyed by build_session_key().
"""

import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import threading

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.wake import deliver_wake, adapter_supports_push


class PushAdapter:
    """Default adapter shape — no supports_async_delivery attribute."""

    def __init__(self):
        self.handled = []

    async def handle_message(self, event):
        self.handled.append(event)


class ApiServerLikeAdapter:
    supports_async_delivery = False

    def __init__(self, host="0.0.0.0", port=0, key="test-key", model="hermes"):
        self._host = host
        self._port = port
        self._api_key = key
        self._model_name = model

    async def handle_message(self, event):  # pragma: no cover — must NOT be hit
        raise AssertionError("non-push adapter must not receive handle_message wakes")


def _source():
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_type="group",
    )


def test_adapter_supports_push_default_true():
    assert adapter_supports_push(PushAdapter()) is True
    assert adapter_supports_push(ApiServerLikeAdapter()) is False


def test_deliver_wake_push_adapter_uses_handle_message():
    adapter = PushAdapter()
    asyncio.run(deliver_wake(adapter, text="wake up", source=_source()))
    assert len(adapter.handled) == 1
    evt = adapter.handled[0]
    assert evt.text == "wake up"
    assert evt.internal is True
    assert evt.source.chat_id == "chat-1"


def test_deliver_wake_push_adapter_requires_source():
    with pytest.raises(ValueError):
        asyncio.run(deliver_wake(PushAdapter(), text="x", session_id="sid"))


def test_deliver_wake_non_push_requires_session_id():
    with pytest.raises(ValueError):
        asyncio.run(deliver_wake(ApiServerLikeAdapter(), text="x", source=_source()))


def test_deliver_wake_non_push_requires_api_key():
    """Session continuation is 403-gated on API_SERVER_KEY — a missing key
    must fail loudly instead of running the wake in a fresh session."""
    adapter = ApiServerLikeAdapter(key="")
    with pytest.raises(RuntimeError, match="API_SERVER_KEY"):
        asyncio.run(deliver_wake(adapter, text="x", session_id="raw-sid"))


@contextmanager
def _serve(handler):
    """Serve test JSON responses using only the Python standard library."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            status, payload = handler(self.path, self.headers, body)
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format, *args):  # pragma: no cover - test server noise
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_deliver_wake_non_push_self_posts_raw_session_id(monkeypatch):
    """The self-post carries the RAW session id header + bearer auth and a
    single user message with stream=false — the exact entry point real
    gateway turns use."""
    seen = {}

    def handler(path, headers, body):
        seen["path"] = path
        seen["session_id"] = headers.get("X-Hermes-Session-Id")
        seen["auth"] = headers.get("Authorization")
        seen["body"] = body
        return 200, {"choices": [{"message": {"content": "ok"}}]}

    # A base+dev install has httpx but deliberately omits optional messaging
    # dependencies such as aiohttp. The API-server wake path must still work.
    monkeypatch.setitem(sys.modules, "aiohttp", None)
    with _serve(handler) as port:
        adapter = ApiServerLikeAdapter(host="0.0.0.0", port=port, key="sekrit")
        asyncio.run(
            deliver_wake(adapter, text="task done — wake", session_id="raw-sid-42")
        )
    assert seen["path"] == "/v1/chat/completions"
    assert seen["session_id"] == "raw-sid-42"
    assert seen["auth"] == "Bearer sekrit"
    assert seen["body"]["stream"] is False
    assert seen["body"]["messages"] == [
        {"role": "user", "content": "task done — wake"}
    ]


def test_deliver_wake_loopback_post_ignores_environment_proxy(monkeypatch):
    """Authenticated in-process wake requests must never leave loopback."""
    import gateway.wake as wake_mod

    monkeypatch.setattr(wake_mod, "_RETRY_DELAYS_SECONDS", (0.01,))
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    def handler(path, headers, body):
        return 200, {"choices": []}

    with _serve(handler) as port:
        adapter = ApiServerLikeAdapter(port=port)
        asyncio.run(deliver_wake(adapter, text="safe", session_id="sid"))


def test_deliver_wake_routes_profile_and_uses_stable_idempotency_key():
    seen = {}

    def handler(path, headers, body):
        seen["path"] = path
        seen["delivery_key"] = headers.get("Idempotency-Key")
        return 200, {"choices": []}

    with _serve(handler) as port:
        adapter = ApiServerLikeAdapter(port=port)
        asyncio.run(
            deliver_wake(
                adapter,
                text="review required",
                session_id="raw-profile-session",
                profile="reviewer",
                delivery_key="kanban-event-42-chunk-0",
            )
        )
    assert seen == {
        "path": "/p/reviewer/v1/chat/completions",
        "delivery_key": "kanban-event-42-chunk-0",
    }


def test_deliver_wake_retries_429_then_succeeds(monkeypatch):
    """HTTP 429 (max_concurrent_runs cap) is transient — retried with backoff."""
    import gateway.wake as wake_mod

    monkeypatch.setattr(wake_mod, "_RETRY_DELAYS_SECONDS", (0.01, 0.01, 0.01))
    calls = {"n": 0}

    def handler(path, headers, body):
        calls["n"] += 1
        if calls["n"] == 1:
            return 429, {"error": "busy"}
        return 200, {"choices": []}

    with _serve(handler) as port:
        adapter = ApiServerLikeAdapter(port=port)
        asyncio.run(deliver_wake(adapter, text="x", session_id="sid"))
    assert calls["n"] == 2


def test_deliver_wake_raises_on_permanent_http_error(monkeypatch):
    """Auth/validation errors (403/400) are permanent — raise immediately so
    the caller can rewind instead of treating the event as delivered."""
    calls = {"n": 0}

    def handler(path, headers, body):
        calls["n"] += 1
        return 403, {"error": "forbidden"}

    with _serve(handler) as port:
        adapter = ApiServerLikeAdapter(port=port)
        with pytest.raises(RuntimeError, match="HTTP 403"):
            asyncio.run(deliver_wake(adapter, text="x", session_id="sid"))
    assert calls["n"] == 1


def test_deliver_wake_raises_after_exhausted_retries(monkeypatch):
    """Connection failures raise after bounded retries — never silent."""
    import gateway.wake as wake_mod

    monkeypatch.setattr(wake_mod, "_RETRY_DELAYS_SECONDS", (0.01,))
    # Nothing is listening on this port.
    adapter = ApiServerLikeAdapter(host="127.0.0.1", port=1, key="k")
    with pytest.raises(RuntimeError, match="gave up"):
        asyncio.run(deliver_wake(adapter, text="x", session_id="sid"))
