"""Focused tests for the API server remote-attach endpoints."""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB


@pytest.fixture
def session_db(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    db = SessionDB(hermes_home / "state.db")
    try:
        yield db
    finally:
        close = getattr(db, "close", None)
        if callable(close):
            close()


@pytest.fixture
def inline_to_thread(monkeypatch):
    # This file tests handler behavior. The API module's off-event-loop
    # SessionDB contract is covered independently in test_api_server.py.
    async def _inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr("gateway.platforms.api_server.asyncio.to_thread", _inline)


def _remote_handler(adapter: APIServerAdapter):
    handlers = [
        handler
        for method, path, handler in adapter._http_route_table()
        if method == "GET" and path == "/api/remote/sessions"
    ]
    assert len(handlers) == 1
    return handlers[0]


def _route_handler(adapter: APIServerAdapter, method: str, path: str):
    handlers = [
        handler
        for route_method, route_path, handler in adapter._http_route_table()
        if route_method == method and route_path == path
    ]
    assert len(handlers) == 1
    return handlers[0]


class _RecordingStreamResponse:
    def __init__(self, disconnect_after: int):
        self.disconnect_after = disconnect_after
        self.frames: list[bytes] = []
        self.prepared = asyncio.Event()
        self.headers: dict[str, str] = {}

    async def prepare(self, request):
        del request
        self.prepared.set()

    async def write(self, payload: bytes):
        self.frames.append(payload)
        if len(self.frames) >= self.disconnect_after:
            raise ConnectionResetError("test subscriber disconnected")

    @property
    def events(self) -> list[dict]:
        events = []
        for frame in self.frames:
            data_lines = [
                line[6:]
                for line in frame.decode().splitlines()
                if line.startswith("data: ")
            ]
            if data_lines:
                events.append(json.loads("\n".join(data_lines)))
        return events


def _apply_headers(stream: "_RecordingStreamResponse", kwargs: dict) -> "_RecordingStreamResponse":
    """Mirror headers passed to web.StreamResponse(...) onto the recording stream."""
    headers = kwargs.get("headers") or {}
    for key, value in headers.items():
        stream.headers[str(key)] = str(value)
    assert kwargs.get("status", 200) == 200
    return stream


def _request(method: str, path: str, session_id: str, *, authenticated=True):
    headers = _remote_headers() if authenticated else {}
    return make_mocked_request(
        method,
        path.format(session_id=session_id),
        headers=headers,
        match_info={"session_id": session_id},
    )


def _remote_headers() -> dict[str, str]:
    return {"Authorization": "Bearer remote-test-key"}


async def _generate_pairing_code(adapter: APIServerAdapter) -> dict:
    handler = _route_handler(adapter, "POST", "/api/remote/pair/code")
    response = await handler(
        make_mocked_request(
            "POST", "/api/remote/pair/code", headers=_remote_headers()
        )
    )
    assert response.status == 200
    return json.loads(response.text)


async def _redeem_pairing_code(adapter: APIServerAdapter, code: str):
    handler = _route_handler(adapter, "POST", "/api/remote/pair")
    with patch.object(
        adapter,
        "_read_json_body",
        return_value=({"code": code}, None),
    ):
        return await handler(
            make_mocked_request(
                "POST", "/api/remote/pair", headers=_remote_headers()
            )
        )


@pytest.mark.asyncio
async def test_remote_pair_redeems_code_without_api_key():
    """Client-side redemption must NOT require API_SERVER_KEY — the pairing
    code is the only credential a remote client possesses."""
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "remote-test-key"})
    )
    generated = await _generate_pairing_code(adapter)

    handler = _route_handler(adapter, "POST", "/api/remote/pair")
    with patch.object(
        adapter,
        "_read_json_body",
        return_value=({"code": generated["code"]}, None),
    ):
        # No Authorization header at all
        response = await handler(make_mocked_request("POST", "/api/remote/pair"))

    assert response.status == 200
    payload = json.loads(response.text)
    assert payload["token"]
    assert payload["expires_at"]
    assert payload["ttl_hours"] == 24


def test_remote_pairing_routes_are_registered():
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    routes = {(method, path) for method, path, _ in adapter._http_route_table()}

    assert ("POST", "/api/remote/pair/code") in routes
    assert ("POST", "/api/remote/pair") in routes


@pytest.mark.asyncio
async def test_remote_pair_code_generation_returns_single_use_ten_minute_code():
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "remote-test-key"})
    )

    payload = await _generate_pairing_code(adapter)

    assert len(payload["code"]) == 6
    assert payload["code"].isalnum()
    assert payload["code"] == payload["code"].upper()
    assert payload["ttl_minutes"] == 10
    assert datetime.fromisoformat(payload["expires_at"]).tzinfo is not None


@pytest.mark.asyncio
async def test_remote_pair_code_redemption_returns_24_hour_attach_token():
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "remote-test-key"})
    )
    generated = await _generate_pairing_code(adapter)

    response = await _redeem_pairing_code(adapter, generated["code"])
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["token"]
    assert payload["ttl_hours"] == 24
    assert datetime.fromisoformat(payload["expires_at"]).tzinfo is not None

    reused = await _redeem_pairing_code(adapter, generated["code"])
    assert reused.status == 401
    assert set(json.loads(reused.text)) == {"error"}


@pytest.mark.asyncio
async def test_remote_pair_rejects_wrong_code_with_openai_error():
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "remote-test-key"})
    )

    response = await _redeem_pairing_code(adapter, "WRONG1")
    payload = json.loads(response.text)

    assert response.status == 401
    assert set(payload) == {"error"}
    assert payload["error"]["code"] == "invalid_pairing_code"


@pytest.mark.asyncio
async def test_remote_pair_rejects_expired_code():
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "remote-test-key"})
    )
    generated = await _generate_pairing_code(adapter)
    expired_at = datetime.fromisoformat(generated["expires_at"]).timestamp() + 1

    with patch("gateway.platforms.api_server.time.time", return_value=expired_at):
        response = await _redeem_pairing_code(adapter, generated["code"])
    payload = json.loads(response.text)

    assert response.status == 401
    assert set(payload) == {"error"}
    assert payload["error"]["code"] == "invalid_pairing_code"


@pytest.mark.asyncio
async def test_remote_attach_token_is_scoped_to_remote_endpoints(
    session_db, inline_to_thread
):
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "remote-test-key"})
    )
    adapter._session_db = session_db
    generated = await _generate_pairing_code(adapter)
    paired = await _redeem_pairing_code(adapter, generated["code"])
    token = json.loads(paired.text)["token"]
    attach_headers = {"Authorization": f"Bearer {token}"}

    remote = await _remote_handler(adapter)(
        make_mocked_request(
            "GET", "/api/remote/sessions", headers=attach_headers
        )
    )
    ordinary = await _route_handler(adapter, "GET", "/api/sessions")(
        make_mocked_request("GET", "/api/sessions", headers=attach_headers)
    )
    static_key = await _remote_handler(adapter)(
        make_mocked_request(
            "GET", "/api/remote/sessions", headers=_remote_headers()
        )
    )

    assert remote.status == 200
    assert ordinary.status == 401
    assert static_key.status == 200


@pytest.mark.asyncio
async def test_remote_pairing_code_is_invalidated_after_five_failed_attempts():
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "remote-test-key"})
    )
    generated = await _generate_pairing_code(adapter)

    for attempt in range(5):
        response = await _redeem_pairing_code(adapter, f"BAD{attempt:03d}")
        assert response.status == 401

    invalidated = await _redeem_pairing_code(adapter, generated["code"])
    assert invalidated.status == 401


@pytest.mark.asyncio
async def test_remote_pair_code_endpoint_requires_static_api_key():
    """Code GENERATION (host-side) requires API_SERVER_KEY; code REDEMPTION
    (client-side) does not — the code itself is the credential."""
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "remote-test-key"})
    )

    # Host-side generation: requires the static key
    code_response = await _route_handler(
        adapter, "POST", "/api/remote/pair/code"
    )(make_mocked_request("POST", "/api/remote/pair/code"))
    assert code_response.status == 401

    # Client-side redemption: NO key required; an unknown code is a 401 but
    # for a different reason (invalid code), and a valid code succeeds.
    with patch.object(
        adapter,
        "_read_json_body",
        return_value=({"code": "ABCDEF"}, None),
    ):
        pair_response = await _route_handler(adapter, "POST", "/api/remote/pair")(
            make_mocked_request("POST", "/api/remote/pair")
        )
    # No key, unknown code -> 401 (invalid code path, not auth path)
    assert pair_response.status == 401


@pytest.mark.asyncio
async def test_remote_sessions_returns_host_profile_and_open_sessions(
    session_db, inline_to_thread, monkeypatch
):
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "remote-test-key"})
    )
    adapter._session_db = session_db

    active_id = session_db.create_session("active-session", "api_server")
    session_db.set_session_title(active_id, "Active work")
    session_db.replace_messages(
        active_id, [{"role": "user", "content": "keep working"}]
    )
    idle_id = session_db.create_session("idle-session", "cli")
    session_db.set_session_title(idle_id, "Waiting for input")
    ended_id = session_db.create_session("ended-session", "cli")
    session_db.end_session(ended_id, "user_exit")

    adapter._shutdown_interruptible_agents[1] = SimpleNamespace(session_id=active_id)
    monkeypatch.setattr("socket.gethostname", lambda: "test-host")
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name", lambda: "default"
    )

    request = make_mocked_request(
        "GET",
        "/api/remote/sessions",
        headers={"Authorization": "Bearer remote-test-key"},
    )
    response = await _remote_handler(adapter)(request)
    assert response.status == 200
    payload = json.loads(response.text)

    assert payload["hostname"] == "test-host"
    assert payload["profile"] == "default"
    assert [session["id"] for session in payload["sessions"]] == [
        idle_id,
        active_id,
    ]
    assert ended_id not in {session["id"] for session in payload["sessions"]}

    sessions = {session["id"]: session for session in payload["sessions"]}
    assert sessions[active_id]["title"] == "Active work"
    assert sessions[active_id]["status"] == "active"
    assert sessions[idle_id]["status"] == "idle"
    assert set(sessions[active_id]) == {"id", "title", "status", "updated_at"}
    assert datetime.fromisoformat(sessions[active_id]["updated_at"]).tzinfo is not None


@pytest.mark.asyncio
async def test_remote_sessions_requires_api_server_key(session_db, inline_to_thread):
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "remote-test-key"})
    )
    adapter._session_db = session_db

    handler = _remote_handler(adapter)
    missing = await handler(make_mocked_request("GET", "/api/remote/sessions"))
    invalid = await handler(
        make_mocked_request(
            "GET",
            "/api/remote/sessions",
            headers={"Authorization": "Bearer wrong-key"},
        )
    )
    valid = await handler(
        make_mocked_request(
            "GET",
            "/api/remote/sessions",
            headers={"Authorization": "Bearer remote-test-key"},
        )
    )

    assert missing.status == 401
    assert invalid.status == 401
    assert valid.status == 200


def test_remote_session_attach_routes_are_registered():
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    routes = {(method, path) for method, path, _ in adapter._http_route_table()}

    assert ("GET", "/api/remote/sessions/{session_id}/events") in routes
    assert ("POST", "/api/remote/sessions/{session_id}/chat") in routes


@pytest.mark.asyncio
async def test_remote_session_events_initial_status_can_be_active(
    session_db, inline_to_thread
):
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "remote-test-key"})
    )
    adapter._session_db = session_db
    session_id = session_db.create_session("remote-active", "cli")
    adapter._shutdown_interruptible_agents[1] = SimpleNamespace(session_id=session_id)
    stream = _RecordingStreamResponse(disconnect_after=1)
    handler = _route_handler(
        adapter, "GET", "/api/remote/sessions/{session_id}/events"
    )

    with patch(
        "gateway.platforms.api_server.web.StreamResponse", return_value=stream
    ):
        await handler(
            _request("GET", "/api/remote/sessions/{session_id}/events", session_id)
        )

    assert stream.events == [
        {
            "event": "session.status",
            "session_id": session_id,
            "status": "active",
        }
    ]
    assert session_id not in adapter._remote_session_subscribers


@pytest.mark.asyncio
async def test_remote_session_events_emit_status_messages_and_tool_calls(
    session_db, inline_to_thread
):
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "remote-test-key"})
    )
    adapter._session_db = session_db
    session_id = session_db.create_session("remote-events", "cli")

    async def _run_agent(**kwargs):
        kwargs["tool_start_callback"](
            "call_1", "terminal", {"command": "pwd"}
        )
        kwargs["tool_complete_callback"](
            "call_1", "terminal", {"command": "pwd"}, "/workspace"
        )
        return ({"final_response": "Turn complete", "session_id": session_id}, {})

    adapter._run_agent = AsyncMock(side_effect=_run_agent)

    stream = _RecordingStreamResponse(disconnect_after=5)
    events_handler = _route_handler(
        adapter, "GET", "/api/remote/sessions/{session_id}/events"
    )
    chat_handler = _route_handler(
        adapter, "POST", "/api/remote/sessions/{session_id}/chat"
    )
    with patch(
        "gateway.platforms.api_server.web.StreamResponse", return_value=stream
    ), patch.object(
        adapter,
        "_read_json_body",
        return_value=({"message": "Do the work"}, None),
    ):
        stream_task = asyncio.create_task(
            events_handler(
                _request(
                    "GET", "/api/remote/sessions/{session_id}/events", session_id
                )
            )
        )
        await asyncio.wait_for(stream.prepared.wait(), timeout=2)
        chat = await chat_handler(
            _request("POST", "/api/remote/sessions/{session_id}/chat", session_id)
        )
        await asyncio.wait_for(stream_task, timeout=2)

    payload = json.loads(chat.text)
    assert chat.status == 200
    assert payload["object"] == "hermes.session.chat.completion"
    assert payload["message"] == {
        "role": "assistant",
        "content": "Turn complete",
    }
    call = adapter._run_agent.await_args.kwargs
    assert call["user_message"] == "Do the work"
    assert callable(call["tool_start_callback"])
    assert callable(call["tool_complete_callback"])

    assert stream.events[0] == {
        "event": "session.status",
        "session_id": session_id,
        "status": "idle",
    }
    user_event, started, completed, assistant_event = stream.events[1:]
    assert user_event["message"] == {"role": "user", "content": "Do the work"}
    assert started["tool_call"] == {
        "id": "call_1",
        "name": "terminal",
        "phase": "started",
        "arguments": {"command": "pwd"},
    }
    assert completed["tool_call"]["phase"] == "completed"
    assert completed["tool_call"]["result"] == "/workspace"
    assert assistant_event["message"] == {
        "role": "assistant",
        "content": "Turn complete",
    }


@pytest.mark.asyncio
async def test_remote_session_events_broadcast_to_multiple_subscribers(
    session_db, inline_to_thread
):
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "remote-test-key"})
    )
    adapter._session_db = session_db
    session_id = session_db.create_session("remote-broadcast", "cli")
    adapter._run_agent = AsyncMock(
        return_value=({"final_response": "Shared answer", "session_id": session_id}, {})
    )

    first = _RecordingStreamResponse(disconnect_after=3)
    second = _RecordingStreamResponse(disconnect_after=3)
    events_handler = _route_handler(
        adapter, "GET", "/api/remote/sessions/{session_id}/events"
    )
    chat_handler = _route_handler(
        adapter, "POST", "/api/remote/sessions/{session_id}/chat"
    )
    with patch(
        "gateway.platforms.api_server.web.StreamResponse",
        side_effect=[first, second],
    ), patch.object(
        adapter,
        "_read_json_body",
        return_value=({"message": "Broadcast this"}, None),
    ):
        first_task = asyncio.create_task(
            events_handler(
                _request(
                    "GET", "/api/remote/sessions/{session_id}/events", session_id
                )
            )
        )
        second_task = asyncio.create_task(
            events_handler(
                _request(
                    "GET", "/api/remote/sessions/{session_id}/events", session_id
                )
            )
        )
        await asyncio.wait_for(first.prepared.wait(), timeout=2)
        await asyncio.wait_for(second.prepared.wait(), timeout=2)
        chat = await chat_handler(
            _request("POST", "/api/remote/sessions/{session_id}/chat", session_id)
        )
        assert chat.status == 200
        await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=2)

    assert first.events == second.events
    assert first.events[1]["message"]["content"] == "Broadcast this"
    assert first.events[2]["message"]["content"] == "Shared answer"
    assert session_id not in adapter._remote_session_subscribers


@pytest.mark.asyncio
@pytest.mark.parametrize("ended", [False, True], ids=["unknown", "ended"])
async def test_remote_session_events_return_404_for_non_open_session(
    session_db, inline_to_thread, ended
):
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "remote-test-key"})
    )
    adapter._session_db = session_db
    session_id = "missing-session"
    if ended:
        session_id = session_db.create_session("ended-remote", "cli")
        session_db.end_session(session_id, "user_exit")

    handler = _route_handler(
        adapter, "GET", "/api/remote/sessions/{session_id}/events"
    )
    response = await handler(
        _request("GET", "/api/remote/sessions/{session_id}/events", session_id)
    )
    payload = json.loads(response.text)

    assert response.status == 404
    assert set(payload) == {"error"}
    assert payload["error"]["code"] == "session_not_found"


@pytest.mark.asyncio
async def test_remote_session_attach_endpoints_require_auth(
    session_db, inline_to_thread
):
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "remote-test-key"})
    )
    adapter._session_db = session_db
    session_id = session_db.create_session("remote-auth", "cli")

    events = await _route_handler(
        adapter, "GET", "/api/remote/sessions/{session_id}/events"
    )(
        _request(
            "GET",
            "/api/remote/sessions/{session_id}/events",
            session_id,
            authenticated=False,
        )
    )
    chat = await _route_handler(
        adapter, "POST", "/api/remote/sessions/{session_id}/chat"
    )(
        _request(
            "POST",
            "/api/remote/sessions/{session_id}/chat",
            session_id,
            authenticated=False,
        )
    )

    assert events.status == 401
    assert chat.status == 401


@pytest.mark.asyncio
async def test_remote_session_tool_events_are_redacted_before_broadcast(
    session_db, inline_to_thread
):
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "remote-test-key"})
    )
    adapter._session_db = session_db
    session_id = session_db.create_session("remote-redaction", "cli")
    secret = "sk-test-abcdefghijklmnopqrstuvwxyz1234567890"

    async def _run_agent(**kwargs):
        kwargs["tool_start_callback"](
            "call_secret", "terminal", {"api_key": secret}
        )
        kwargs["tool_complete_callback"](
            "call_secret", "terminal", {"api_key": secret}, f"token={secret}"
        )
        return ({"final_response": "Safe", "session_id": session_id}, {})

    adapter._run_agent = AsyncMock(side_effect=_run_agent)

    stream = _RecordingStreamResponse(disconnect_after=4)
    events_handler = _route_handler(
        adapter, "GET", "/api/remote/sessions/{session_id}/events"
    )
    chat_handler = _route_handler(
        adapter, "POST", "/api/remote/sessions/{session_id}/chat"
    )
    with patch(
        "gateway.platforms.api_server.web.StreamResponse", return_value=stream
    ), patch.object(
        adapter,
        "_read_json_body",
        return_value=({"message": "Use the key"}, None),
    ):
        stream_task = asyncio.create_task(
            events_handler(
                _request(
                    "GET", "/api/remote/sessions/{session_id}/events", session_id
                )
            )
        )
        await asyncio.wait_for(stream.prepared.wait(), timeout=2)
        chat = await chat_handler(
            _request("POST", "/api/remote/sessions/{session_id}/chat", session_id)
        )
        assert chat.status == 200
        await asyncio.wait_for(stream_task, timeout=2)

    tool_events = [
        event for event in stream.events if event["event"] == "session.tool_call"
    ]
    assert len(tool_events) == 2
    serialized = json.dumps(tool_events)
    assert secret not in serialized
    assert "redacted" in serialized or "..." in serialized


def test_remote_attach_preflight_allows_accept_header():
    """Browser clients (remote-attach desktop UI) must be able to preflight the
    SSE events stream: the fetch sends `Accept: text/event-stream`, which is not
    a CORS-safelisted value, so the preflight lists it in
    Access-Control-Request-Headers. Without Accept in the allowed headers the
    browser blocks the stream with "Failed to fetch"."""
    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "key": "remote-test-key",
                "cors_origins": "http://localhost:5174",
            },
        )
    )
    allowed = adapter._cors_headers_for_origin("http://localhost:5174")
    assert allowed is not None
    assert "Accept" in allowed.get("Access-Control-Allow-Headers", "")


@pytest.mark.asyncio
async def test_remote_session_events_stream_includes_cors_headers(session_db, inline_to_thread):
    """The SSE events stream must carry CORS headers on the response itself:
    the CORS middleware flushes headers only *after* the handler returns, which
    is too late for a StreamResponse that already called prepare(). Browser
    clients (desktop /remote UI) otherwise fail with 'Failed to fetch'."""
    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "key": "remote-test-key",
                "cors_origins": "http://localhost:5174",
            },
        )
    )
    adapter._session_db = session_db
    session_id = session_db.create_session("remote-cors", "cli")
    stream = _RecordingStreamResponse(disconnect_after=1)

    class _RequestWithOrigin:
        headers = {"Authorization": "Bearer remote-test-key", "Origin": "http://localhost:5174"}
        match_info = {"session_id": session_id}

    handler = _route_handler(adapter, "GET", "/api/remote/sessions/{session_id}/events")
    with patch(
        "gateway.platforms.api_server.web.StreamResponse",
        side_effect=lambda **kwargs: _apply_headers(stream, kwargs),
    ):
        await handler(_RequestWithOrigin())

    # The handler must have resolved CORS headers into the response headers
    # BEFORE prepare() flushed them.
    prepared_headers = stream.headers
    assert prepared_headers.get("Access-Control-Allow-Origin") == "http://localhost:5174"
