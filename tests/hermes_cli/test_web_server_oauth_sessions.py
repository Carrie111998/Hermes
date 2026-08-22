import json
import threading
from typing import Any, cast

import pytest
from fastapi import HTTPException

from hermes_cli import web_server


def _session(session_id: str, profile: str | None = "work") -> dict:
    return {
        "session_id": session_id,
        "provider": "anthropic",
        "flow": "pkce",
        "profile": profile,
        "created_at": 0.0,
        "status": "pending",
        "error_message": None,
        "state": "state",
        "verifier": "verifier",
    }


def test_anthropic_submit_refuses_profile_mismatch(monkeypatch):
    session_id = "profile-bound"
    with web_server._oauth_sessions_lock:
        web_server._oauth_sessions[session_id] = _session(session_id, "work")
    try:
        with pytest.raises(HTTPException) as exc:
            web_server._submit_anthropic_pkce(session_id, "code", "other")
        assert exc.value.status_code == 404
    finally:
        with web_server._oauth_sessions_lock:
            web_server._oauth_sessions.pop(session_id, None)


@pytest.mark.asyncio
async def test_rest_cancel_does_not_claim_success_after_approval(monkeypatch):
    session_id = "already-approved"
    session = _session(session_id, "work")
    session["status"] = "approved"
    with web_server._oauth_sessions_lock:
        web_server._oauth_sessions[session_id] = session
    monkeypatch.setattr(web_server, "_require_token", lambda request: None)
    try:
        result = await web_server.cancel_oauth_session(
            session_id, request=cast(Any, None), profile="work"
        )
        assert result == {"ok": False, "session_id": session_id, "status": "approved"}
        with web_server._oauth_sessions_lock:
            assert web_server._oauth_sessions.get(session_id) is session
    finally:
        with web_server._oauth_sessions_lock:
            web_server._oauth_sessions.pop(session_id, None)


@pytest.mark.asyncio
async def test_rest_cancel_rejects_profile_mismatch(monkeypatch):
    session_id = "profile-mismatch"
    session = _session(session_id, "work")
    with web_server._oauth_sessions_lock:
        web_server._oauth_sessions[session_id] = session
    monkeypatch.setattr(web_server, "_require_token", lambda request: None)
    try:
        result = await web_server.cancel_oauth_session(
            session_id, request=cast(Any, None), profile="other"
        )
        assert result == {"ok": False, "message": "session not found"}
        assert session.get("cancelled") is not True
    finally:
        with web_server._oauth_sessions_lock:
            web_server._oauth_sessions.pop(session_id, None)



def test_gc_marks_pending_session_cancelled_before_removal():
    session_id = "gc-pending"
    session = _session(session_id, "work")
    session["created_at"] = 0.0
    with web_server._oauth_sessions_lock:
        web_server._oauth_sessions[session_id] = session

    web_server._gc_oauth_sessions()

    assert session["cancelled"] is True
    assert session["status"] == "expired"
    with web_server._oauth_sessions_lock:
        assert session_id not in web_server._oauth_sessions



def test_anthropic_cancel_before_final_save_prevents_persistence(monkeypatch):
    session_id = "cancel-race"
    exchange_started = threading.Event()
    release_exchange = threading.Event()
    saved = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {"access_token": "secret-access", "refresh_token": "secret-refresh", "expires_in": 3600}
            ).encode()

    def urlopen(_request, timeout=0):
        assert timeout == 20
        exchange_started.set()
        assert release_exchange.wait(2)
        return Response()

    monkeypatch.setattr(web_server, "_ANTHROPIC_OAUTH_TOKEN_URLS", ("https://auth.example/token",))
    monkeypatch.setattr(web_server.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(
        web_server,
        "_save_anthropic_oauth_creds",
        lambda *args: saved.append(args),
    )

    with web_server._oauth_sessions_lock:
        web_server._oauth_sessions[session_id] = _session(session_id, "work")

    result = {}

    def submit():
        result.update(web_server._submit_anthropic_pkce(session_id, "code#state", "work"))

    worker = threading.Thread(target=submit)
    worker.start()
    assert exchange_started.wait(2)
    with web_server._oauth_sessions_lock:
        session = web_server._oauth_sessions[session_id]
        session["cancelled"] = True
        session["status"] = "cancelled"
        web_server._oauth_sessions.pop(session_id, None)
    release_exchange.set()
    worker.join(2)

    assert not worker.is_alive()
    assert result["status"] == "cancelled"
    assert saved == []
