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
