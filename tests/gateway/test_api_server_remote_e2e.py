"""End-to-end coverage for remote session attachment over real HTTP/SSE."""

from __future__ import annotations

import asyncio
import json
import socket
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp import ClientSession, ClientTimeout

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB


_API_KEY = "remote-e2e-key-0123456789"


async def _read_sse_event(response, *, timeout: float = 2.0) -> dict:
    """Read one complete SSE data frame from an aiohttp client response."""
    data_lines: list[str] = []
    while True:
        raw_line = await asyncio.wait_for(response.content.readline(), timeout)
        if not raw_line:
            raise AssertionError("SSE connection closed before the next event")
        line = raw_line.decode("utf-8").rstrip("\r\n")
        if not line:
            if data_lines:
                return json.loads("\n".join(data_lines))
            continue
        if line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").lstrip())


async def _wait_for_user_message(
    db: SessionDB,
    session_id: str,
    content: str,
    *,
    timeout: float = 2.0,
) -> None:
    """Poll the real store until the submitted user message is durable."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        messages = await asyncio.to_thread(db.get_messages, session_id)
        if any(
            message.get("role") == "user" and message.get("content") == content
            for message in messages
        ):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"user message was not persisted: {content!r}")


async def _wait_for_sse_disconnect(
    adapter: APIServerAdapter,
    session_id: str,
    *,
    timeout: float = 2.0,
) -> None:
    """Wake the server writer until it observes the closed client socket."""
    deadline = asyncio.get_running_loop().time() + timeout
    while session_id in adapter._remote_session_subscribers:
        adapter._publish_remote_session_event(
            session_id,
            {
                "event": "session.status",
                "session_id": session_id,
                "status": "idle",
            },
        )
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("SSE subscriber did not disconnect")
        await asyncio.sleep(0.01)


@pytest_asyncio.fixture
async def remote_api_server(tmp_path, monkeypatch):
    """Boot the production API adapter on an ephemeral loopback TCP port."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    db = SessionDB(hermes_home / "state.db")
    active_id = db.create_session("remote-e2e-active", "api_server")
    db.set_session_title(active_id, "Active remote work")
    db.replace_messages(
        active_id,
        [
            {"role": "user", "content": "Seed question"},
            {"role": "assistant", "content": "Seed answer"},
        ],
    )
    idle_id = db.create_session("remote-e2e-idle", "cli")
    db.set_session_title(idle_id, "Idle remote work")

    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "key": _API_KEY,
            },
        )
    )
    adapter._session_db = db
    adapter._shutdown_interruptible_agents[1] = SimpleNamespace(
        session_id=active_id
    )

    async def _run_agent(**kwargs):
        """Exercise the chat handler without making an external model call."""
        stored = await asyncio.to_thread(db.get_messages, active_id)
        transcript = [
            {"role": message["role"], "content": message.get("content", "")}
            for message in stored
        ]
        transcript.extend(
            [
                {"role": "user", "content": kwargs["user_message"]},
                {"role": "assistant", "content": "Remote E2E reply"},
            ]
        )
        await asyncio.to_thread(db.replace_messages, active_id, transcript)
        return (
            {
                "final_response": "Remote E2E reply",
                "session_id": active_id,
            },
            {},
        )

    adapter._run_agent = _run_agent

    try:
        assert await adapter.connect()
        assert adapter._site is not None
        assert adapter._site._server is not None
        port = adapter._site._server.sockets[0].getsockname()[1]
        yield SimpleNamespace(
            adapter=adapter,
            base_url=f"http://127.0.0.1:{port}",
            db=db,
            active_id=active_id,
            idle_id=idle_id,
        )
    finally:
        await adapter.cancel_background_tasks()
        await adapter.disconnect()
        db.close()


@pytest.mark.asyncio
async def test_remote_attach_flow_over_real_http_and_sse(remote_api_server):
    server = remote_api_server
    timeout = ClientTimeout(total=5.0)
    static_headers = {"Authorization": f"Bearer {_API_KEY}"}

    async with ClientSession(base_url=server.base_url, timeout=timeout) as client:
        unauthenticated = await client.get("/api/remote/sessions")
        assert unauthenticated.status == 401

        generated = await client.post(
            "/api/remote/pair/code", headers=static_headers
        )
        assert generated.status == 200
        pairing = await generated.json()
        assert len(pairing["code"]) == 6
        assert pairing["code"].isalnum()

        wrong_code = await client.post(
            "/api/remote/pair", json={"code": "WRONG1"}
        )
        assert wrong_code.status == 401
        wrong_payload = await wrong_code.json()
        assert wrong_payload["error"]["code"] == "invalid_pairing_code"

        redeemed = await client.post(
            "/api/remote/pair", json={"code": pairing["code"]}
        )
        assert redeemed.status == 200
        token = (await redeemed.json())["token"]
        assert token
        remote_headers = {"Authorization": f"Bearer {token}"}

        listed = await client.get(
            "/api/remote/sessions", headers=remote_headers
        )
        assert listed.status == 200
        listing = await listed.json()
        assert listing["hostname"] == socket.gethostname()
        assert isinstance(listing["profile"], str) and listing["profile"]
        sessions = {session["id"]: session for session in listing["sessions"]}
        assert set(sessions) == {server.active_id, server.idle_id}
        assert sessions[server.active_id]["title"] == "Active remote work"
        assert sessions[server.active_id]["status"] == "active"
        assert sessions[server.idle_id]["title"] == "Idle remote work"
        assert sessions[server.idle_id]["status"] == "idle"

        events = await client.get(
            f"/api/remote/sessions/{server.active_id}/events",
            headers=remote_headers,
        )
        assert events.status == 200
        assert events.headers["Content-Type"].startswith("text/event-stream")
        initial = await _read_sse_event(events)
        assert initial == {
            "event": "session.status",
            "session_id": server.active_id,
            "status": "active",
        }

        published = {
            "event": "session.message",
            "session_id": server.active_id,
            "message": {"role": "assistant", "content": "Published live"},
        }
        server.adapter._publish_remote_session_event(server.active_id, published)
        assert await _read_sse_event(events) == published

        chat_message = "Continue over the remote connection"
        chatted = await client.post(
            f"/api/remote/sessions/{server.active_id}/chat",
            headers=remote_headers,
            json={"message": chat_message},
        )
        assert chatted.status == 200
        chat_payload = await chatted.json()
        assert chat_payload["session_id"] == server.active_id
        assert chat_payload["message"] == {
            "role": "assistant",
            "content": "Remote E2E reply",
        }
        await _wait_for_user_message(server.db, server.active_id, chat_message)

        events.close()
        await _wait_for_sse_disconnect(
            server.adapter,
            server.active_id,
        )
