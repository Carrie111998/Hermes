import json

import pytest
from unittest.mock import AsyncMock

from hermes_cli import web_server


class FakeBridge:
    def __init__(self):
        self.alive = True

    def read(self, timeout):
        return b""        # idle forever

    def write(self, data):
        pass

    def resize(self, cols, rows):
        pass

    def close(self):
        self.alive = False


def test_terminate_pty_endpoint_closes_attach_token_sessions(monkeypatch):
    from starlette.testclient import TestClient

    terminate = AsyncMock(return_value=2)
    monkeypatch.setattr(web_server.PTY_REGISTRY, "terminate_attach_token", terminate)

    with TestClient(web_server.app) as client:
        response = client.post(
            "/api/pty/terminate",
            json={"attach_token": "07" * 16},
            headers={web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN},
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "terminated": 2}
    terminate.assert_awaited_once_with("07" * 16)


def test_terminate_pty_endpoint_rejects_invalid_attach_token():
    from starlette.testclient import TestClient

    with TestClient(web_server.app) as client:
        response = client.post(
            "/api/pty/terminate",
            json={"attach_token": "not-a-token"},
            headers={web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN},
        )

    assert response.status_code == 422


@pytest.fixture
def pty_keepalive_harness(monkeypatch):
    spawned = []

    def fake_spawn(argv, cwd=None, env=None):
        b = FakeBridge()
        spawned.append(argv)
        return b

    monkeypatch.setattr(web_server.PtyBridge, "spawn", staticmethod(fake_spawn))
    monkeypatch.setattr(web_server, "_ws_auth_reason", lambda ws: (None, "test"))
    monkeypatch.setattr(web_server, "_ws_host_origin_reason", lambda ws: None)
    monkeypatch.setattr(web_server, "_ws_client_reason", lambda ws: None)

    async def fake_argv(**kw):
        resume = "child" if kw.get("resume") == "parent" else kw.get("resume")
        env = {"HERMES_TUI_RESUME": resume} if resume else {}
        return (["x", resume or "fresh"], "/tmp", env)

    monkeypatch.setattr(web_server, "_resolve_chat_argv_async", fake_argv)

    try:
        yield spawned
    finally:
        web_server.PTY_REGISTRY._sessions.clear()


@pytest.mark.asyncio
async def test_attach_token_reuses_same_session(pty_keepalive_harness):
    """Two connects with the same ?attach= token hit one spawned bridge."""
    from starlette.testclient import TestClient

    client = TestClient(web_server.app)
    with client.websocket_connect("/api/pty?attach=TOK1") as ws1:
        ws1.send_bytes(b"hi")
    with client.websocket_connect("/api/pty?attach=TOK1") as ws2:
        ws2.send_bytes(b"again")
    assert len(pty_keepalive_harness) == 1                # reattached, did not respawn


@pytest.mark.asyncio
async def test_attach_token_reuses_same_resume(pty_keepalive_harness):
    from starlette.testclient import TestClient

    client = TestClient(web_server.app)
    with client.websocket_connect("/api/pty?attach=TOK1&resume=same") as ws1:
        ws1.send_bytes(b"hi")
    with client.websocket_connect("/api/pty?attach=TOK1&resume=same") as ws2:
        ws2.send_bytes(b"again")
    assert pty_keepalive_harness == [["x", "same"]]




@pytest.mark.asyncio
async def test_attach_token_reuses_canonical_resume(pty_keepalive_harness):
    from starlette.testclient import TestClient

    client = TestClient(web_server.app)
    with client.websocket_connect("/api/pty?attach=TOK1&resume=parent") as ws1:
        ws1.send_bytes(b"hi")
    with client.websocket_connect("/api/pty?attach=TOK1&resume=child") as ws2:
        ws2.send_bytes(b"again")
    assert pty_keepalive_harness == [["x", "child"]]




@pytest.mark.asyncio
async def test_attach_token_reuses_default_chat_after_active_session_fallback(
    pty_keepalive_harness, tmp_path, monkeypatch
):
    from starlette.testclient import TestClient

    active_session_file = tmp_path / "active-session.json"
    monkeypatch.setattr(
        web_server,
        "_active_session_file_for_channel",
        lambda app, channel: active_session_file,
    )

    client = TestClient(web_server.app)
    with client.websocket_connect("/api/pty?attach=TOK1&channel=CHAT") as ws1:
        ws1.send_bytes(b"hi")

    active_session_file.write_text(json.dumps({"session_id": "existing"}))

    with client.websocket_connect("/api/pty?attach=TOK1&channel=CHAT") as ws2:
        ws2.send_bytes(b"again")

    assert pty_keepalive_harness == [["x", "fresh"]]
