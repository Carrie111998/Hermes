"""Focused tests for the API server remote-attach discovery endpoint."""

import json
from datetime import datetime
from types import SimpleNamespace

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
