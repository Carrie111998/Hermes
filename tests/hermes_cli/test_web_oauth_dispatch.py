"""Regression tests for the OAuth dispatcher in hermes_cli.web_server.

Bug history (2026-05-09): the `_OAUTH_PROVIDER_CATALOG` had two entries
flagged ``flow: "pkce"`` — anthropic and minimax-oauth — and the
dispatcher ``start_oauth_login`` hardcoded ``_start_anthropic_pkce()``
for any pkce-flagged provider. So clicking "Login" next to MiniMax in
the dashboard's Keys tab silently launched the Anthropic/Claude OAuth
flow.

The fix:
  1. Catalog entry for minimax-oauth changed from ``flow: "pkce"`` to
     ``flow: "device_code"`` (the actual UX is verification URI + user
     code + background poll, with PKCE as a security extension).
  2. New MiniMax branch added to ``_start_device_code_flow``.
  3. Dispatcher tightened: pkce branch now requires
     ``provider_id == "anthropic"``, so any future PKCE provider added
     without an explicit branch gets a clean ``400 Unsupported flow``
     instead of silently launching Anthropic OAuth.

These tests pin the corrected behavior.
"""
import asyncio
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from hermes_cli.web_server import _SESSION_TOKEN, app

client = TestClient(app)
HEADERS = {"X-Hermes-Session-Token": _SESSION_TOKEN}


def _make_profile_home(tmp_path, monkeypatch, profile="coder"):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    profile_home = tmp_path / "profiles" / profile
    profile_home.mkdir(parents=True)
    return profile_home


def _fake_nous_device_data():
    return {
        "device_code": "device-code",
        "user_code": "NOUS-1234",
        "verification_uri": "https://portal.nousresearch.com/device",
        "verification_uri_complete": (
            "https://portal.nousresearch.com/device?user_code=NOUS-1234"
        ),
        "expires_in": 600,
        "interval": 5,
    }


def _invoke_scope_refusal():
    request = httpx.Request("POST", "https://portal.nousresearch.com/oauth/device/code")
    response = httpx.Response(
        400,
        json={
            "error": "invalid_scope",
            "error_description": "unsupported scope inference:invoke",
        },
        request=request,
    )
    return httpx.HTTPStatusError("invalid scope", request=request, response=response)


def test_minimax_login_does_not_launch_anthropic_flow():
    """Click 'Login' on MiniMax → MUST NOT return claude.ai auth_url."""
    fake_user_code_resp = {
        "user_code": "ABCD-1234",
        "verification_uri": "https://api.minimax.io/oauth/verify",
        # `expired_in` < 1e12 so the heuristic treats it as seconds.
        "expired_in": 600,
        "interval": 2000,
        "state": "stub-state",
    }
    with patch(
        "hermes_cli.auth._minimax_request_user_code",
        return_value=fake_user_code_resp,
    ), patch(
        "hermes_cli.auth._minimax_pkce_pair",
        return_value=("verifier-stub", "challenge-stub", "stub-state"),
    ), patch(
        "hermes_cli.web_server._minimax_poller",
        return_value=None,
    ):
        resp = client.post(
            "/api/providers/oauth/minimax-oauth/start",
            headers=HEADERS,
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # The bug used to return Anthropic's auth_url — make sure the response
    # references neither the auth_url field nor anything Claude-related.
    assert "auth_url" not in body
    assert "claude.ai" not in str(body).lower()

    # And the response IS the device-code shape pointing at MiniMax.
    assert body["flow"] == "device_code"
    assert "minimax" in body["verification_url"].lower()
    assert body["user_code"] == "ABCD-1234"
    assert body["expires_in"] == 600




def test_oauth_provider_status_uses_profile_query(tmp_path, monkeypatch):
    from hermes_cli import web_server as ws
    from hermes_constants import get_hermes_home

    profile_home = _make_profile_home(tmp_path, monkeypatch)
    observed_homes = []

    def fake_status():
        observed_homes.append(get_hermes_home())
        return {"logged_in": False, "source": None}

    fake_catalog = ({
        "id": "fake-oauth",
        "name": "Fake OAuth",
        "flow": "pkce",
        "cli_command": "hermes auth add fake-oauth",
        "docs_url": "https://example.com",
        "status_fn": fake_status,
    },)
    monkeypatch.setattr(ws, "_OAUTH_PROVIDER_CATALOG", fake_catalog)

    resp = client.get("/api/providers/oauth?profile=coder", headers=HEADERS)

    assert resp.status_code == 200, resp.text
    assert observed_homes == [profile_home]


def test_oauth_start_stores_profile_for_background_completion(tmp_path, monkeypatch):
    from hermes_cli import web_server as ws

    _make_profile_home(tmp_path, monkeypatch)
    fake_user_code_resp = {
        "user_code": "ABCD-1234",
        "verification_uri": "https://api.minimax.io/oauth/verify",
        "expired_in": 600,
        "interval": 2000,
        "state": "stub-state",
    }
    with patch(
        "hermes_cli.auth._minimax_request_user_code",
        return_value=fake_user_code_resp,
    ), patch(
        "hermes_cli.auth._minimax_pkce_pair",
        return_value=("verifier-stub", "challenge-stub", "stub-state"),
    ), patch(
        "hermes_cli.web_server._minimax_poller",
        return_value=None,
    ):
        resp = client.post(
            "/api/providers/oauth/minimax-oauth/start?profile=coder",
            headers=HEADERS,
        )

    assert resp.status_code == 200, resp.text
    session_id = resp.json()["session_id"]
    try:
        assert ws._oauth_sessions[session_id]["profile"] == "coder"
    finally:
        ws._oauth_sessions.pop(session_id, None)


def test_oauth_poll_and_cancel_reject_cross_profile_session_access(monkeypatch):
    from hermes_cli import web_server as ws

    monkeypatch.setattr(ws, "_oauth_profile_name", lambda profile: profile)
    session_id, _ = ws._new_oauth_session(
        "openai-codex", "device_code", profile="coder"
    )
    try:
        wrong_poll = client.get(
            f"/api/providers/oauth/openai-codex/poll/{session_id}?profile=other",
            headers=HEADERS,
        )
        wrong_cancel = client.delete(
            f"/api/providers/oauth/sessions/{session_id}?profile=other",
            headers=HEADERS,
        )
        omitted_poll = client.get(
            f"/api/providers/oauth/openai-codex/poll/{session_id}",
            headers=HEADERS,
        )
        omitted_cancel = client.delete(
            f"/api/providers/oauth/sessions/{session_id}",
            headers=HEADERS,
        )

        assert wrong_poll.status_code == 409
        assert wrong_cancel.status_code == 409
        assert omitted_poll.status_code == 409
        assert omitted_cancel.status_code == 409
        assert session_id in ws._oauth_sessions

        right_poll = client.get(
            f"/api/providers/oauth/openai-codex/poll/{session_id}?profile=coder",
            headers=HEADERS,
        )
        right_cancel = client.delete(
            f"/api/providers/oauth/sessions/{session_id}?profile=coder",
            headers=HEADERS,
        )

        assert right_poll.status_code == 200
        assert right_cancel.status_code == 200
        assert session_id not in ws._oauth_sessions
    finally:
        ws._oauth_sessions.pop(session_id, None)


def test_oauth_session_profile_preserves_explicit_default_owner(monkeypatch):
    from hermes_cli import web_server as ws

    monkeypatch.setattr(ws, "_oauth_profile_name", lambda profile: profile)
    session_id, _ = ws._new_oauth_session("anthropic", "pkce", profile=None)
    try:
        assert ws._oauth_session_profile(session_id, "coder") is None
    finally:
        ws._oauth_sessions.pop(session_id, None)


def test_pkce_submit_rejects_omitted_profile_for_named_session(monkeypatch):
    from hermes_cli import web_server as ws

    monkeypatch.setattr(ws, "_oauth_profile_name", lambda profile: profile)
    session_id, _ = ws._new_oauth_session("anthropic", "pkce", profile="coder")
    try:
        with pytest.raises(ws.HTTPException) as exc_info:
            ws._submit_anthropic_pkce(session_id, "code#state", profile=None)

        assert exc_info.value.status_code == 409
    finally:
        ws._oauth_sessions.pop(session_id, None)


def test_codex_dashboard_worker_credential_only_preserves_active_provider(
    tmp_path, monkeypatch
):
    from hermes_cli import web_server as ws
    from hermes_cli.auth import get_active_provider
    from hermes_cli.runtime_provider import resolve_runtime_provider

    access_token = "h.eyJleH...OTl9.s"

    class _Resp:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, **kwargs):
            if url.endswith("/deviceauth/usercode"):
                return _Resp(200, {
                    "device_auth_id": "device-auth-id",
                    "interval": 3,
                    "user_code": "CODEX-1234",
                })
            if url.endswith("/deviceauth/token"):
                return _Resp(200, {
                    "authorization_code": "authorization-code",
                    "code_verifier": "code-verifier",
                })
            return _Resp(200, {
                "access_token": access_token,
                "refresh_token": "codex-refresh",
            })

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text(
        json.dumps(
            {
                "active_provider": "anthropic",
                "providers": {"anthropic": {"api_key": "anthropic-key"}},
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("model:\n  provider: anthropic\n", encoding="utf-8")
    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr(ws.time, "sleep", lambda _: None)

    sid, sess = ws._new_oauth_session("openai-codex", "device_code")
    sess["activate_provider"] = False
    try:
        ws._codex_full_login_worker(sid)

        assert ws._oauth_sessions[sid]["status"] == "approved"
        assert get_active_provider() == "anthropic"
        assert config_path.read_text(encoding="utf-8") == "model:\n  provider: anthropic\n"

        runtime = resolve_runtime_provider(requested="openai-codex")
        assert runtime["provider"] == "openai-codex"
        assert runtime["api_key"] == access_token
        assert runtime["api_mode"] == "codex_responses"
    finally:
        ws._oauth_sessions.pop(sid, None)


def test_codex_dashboard_worker_does_not_save_after_session_cancelled(monkeypatch):
    from hermes_cli import auth as auth_mod
    from hermes_cli import web_server as ws

    session = {}

    class _Resp:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, **kwargs):
            if url.endswith("/deviceauth/usercode"):
                return _Resp(
                    200,
                    {
                        "device_auth_id": "device-auth-id",
                        "interval": 3,
                        "user_code": "CODEX-1234",
                    },
                )
            if url.endswith("/deviceauth/token"):
                return _Resp(
                    200,
                    {
                        "authorization_code": "authorization-code",
                        "code_verifier": "code-verifier",
                    },
                )
            with ws._oauth_sessions_lock:
                ws._oauth_sessions.pop(session["id"], None)
            return _Resp(
                200,
                {
                    "access_token": "codex-access",
                    "refresh_token": "codex-refresh",
                },
            )

    save_tokens = Mock()
    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr(ws.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        auth_mod, "_save_codex_device_login_tokens", save_tokens
    )

    sid, _ = ws._new_oauth_session("openai-codex", "device_code")
    session["id"] = sid
    ws._codex_full_login_worker(sid)

    assert sid not in ws._oauth_sessions
    save_tokens.assert_not_called()


def test_codex_dashboard_worker_persists_inside_session_profile(tmp_path, monkeypatch):
    from hermes_cli import auth as auth_mod
    from hermes_cli import web_server as ws
    from hermes_constants import get_hermes_home

    profile_home = _make_profile_home(tmp_path, monkeypatch)

    class _Resp:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, **kwargs):
            if url.endswith("/deviceauth/usercode"):
                return _Resp(200, {
                    "device_auth_id": "device-auth-id",
                    "interval": 3,
                    "user_code": "CODEX-1234",
                })
            if url.endswith("/deviceauth/token"):
                return _Resp(200, {
                    "authorization_code": "authorization-code",
                    "code_verifier": "code-verifier",
                })
            return _Resp(200, {
                "access_token": "codex-access",
                "refresh_token": "codex-refresh",
            })

    saved_homes = []
    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr(ws.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        auth_mod,
        "_save_codex_device_login_tokens",
        lambda tokens, *args, **kwargs: saved_homes.append(get_hermes_home()),
    )

    sid, _ = ws._new_oauth_session(
        "openai-codex",
        "device_code",
        profile="coder",
    )
    try:
        ws._codex_full_login_worker(sid)

        assert ws._oauth_sessions[sid]["status"] == "approved"
        assert saved_homes == [profile_home]
    finally:
        ws._oauth_sessions.pop(sid, None)


def test_codex_dashboard_start_forwards_credential_only_intent(monkeypatch):
    from hermes_cli import web_server as ws

    captured = {}

    async def fake_start(provider_id, profile=None, *, activate_provider=True):
        captured.update(
            provider_id=provider_id,
            profile=profile,
            activate_provider=activate_provider,
        )
        return {"session_id": "sid", "flow": "device_code"}

    monkeypatch.setattr(ws, "_start_device_code_flow", fake_start)
    resp = client.post(
        "/api/providers/oauth/openai-codex/start?activate_provider=false",
        headers=HEADERS,
    )

    assert resp.status_code == 200
    assert captured == {
        "provider_id": "openai-codex",
        "profile": None,
        "activate_provider": False,
    }


def test_codex_dashboard_start_waits_through_shared_retry_window(monkeypatch):
    from hermes_cli import web_server as ws

    class _Thread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return None

    clock = iter((0.0, 11.0, 11.0))

    async def fake_sleep(_seconds):
        with ws._oauth_sessions_lock:
            session = next(
                value
                for value in ws._oauth_sessions.values()
                if value.get("provider") == "openai-codex"
            )
            session.update(
                user_code="LATE-429",
                verification_url="https://auth.openai.com/codex/device",
                expires_in=900,
                interval=5,
            )

    monkeypatch.setattr(ws.threading, "Thread", _Thread)
    monkeypatch.setattr(
        ws,
        "time",
        SimpleNamespace(time=time.time, monotonic=lambda: next(clock)),
    )
    monkeypatch.setattr(ws.asyncio, "sleep", fake_sleep)

    result = asyncio.run(ws._start_device_code_flow("openai-codex"))
    try:
        assert result["user_code"] == "LATE-429"
    finally:
        ws._oauth_sessions.pop(result["session_id"], None)


def test_codex_dashboard_start_rewords_device_authorization_error(monkeypatch):
    from hermes_cli import web_server as ws

    before_sessions = set(ws._oauth_sessions)

    class _Resp:
        status_code = 400
        text = "Enable device code authorization"

        def json(self):
            return {
                "error": {
                    "message": "Enable device code authorization",
                    "code": "device_authorization_not_enabled",
                }
            }

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, **kwargs):
            assert url.endswith("/deviceauth/usercode")
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)

    try:
        resp = client.post(
            "/api/providers/oauth/openai-codex/start",
            headers=HEADERS,
        )

        assert resp.status_code == 500
        detail = resp.json()["detail"]
        assert "OpenAI rejected the device-code login request" in detail
        assert "Enable device-code authorization in OpenAI" in detail
        assert "click Login again" in detail
        assert "hermes auth" not in detail
    finally:
        for sid in set(ws._oauth_sessions) - before_sessions:
            ws._oauth_sessions.pop(sid, None)


def test_codex_dashboard_worker_stops_polling_after_cancel(tmp_path, monkeypatch):
    """A real DELETE mid-poll must stop the worker before it exchanges/saves tokens.

    Regression for IA-01: cancelling only popped the session dict; the
    background worker kept polling/exchanging/saving regardless, and once
    the session was gone `_oauth_session_profile()` fell back to the
    caller's current profile scope instead of the one the login started
    in. The fix marks the dict `cancelled` before popping, and the worker
    checks that flag before every remaining step.

    Exercises the actual `DELETE /api/providers/oauth/sessions/{id}`
    endpoint (rather than mutating the session dict directly) so the
    endpoint/worker race and the real removal from `_oauth_sessions` are
    both under test.
    """
    from hermes_cli import auth as auth_mod
    from hermes_cli import web_server as ws

    class _Resp:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, **kwargs):
            if url.endswith("/deviceauth/usercode"):
                return _Resp(200, {
                    "device_auth_id": "device-auth-id",
                    "interval": 3,
                    "user_code": "CODEX-1234",
                })
            raise AssertionError(
                f"worker must stop before calling {url} once cancelled"
            )

    saved = []
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr(
        auth_mod,
        "_save_codex_tokens",
        lambda tokens, last_refresh=None, **kwargs: saved.append(tokens),
    )

    sid, _ = ws._new_oauth_session("openai-codex", "device_code", profile="coder")

    def fake_sleep(_interval):
        # Simulate a real concurrent DELETE /api/providers/oauth/sessions/{sid}
        # firing while the worker is asleep between polls.
        resp = client.delete(f"/api/providers/oauth/sessions/{sid}?profile=coder", headers=HEADERS)
        assert resp.status_code == 200, resp.text

    monkeypatch.setattr(ws.time, "sleep", fake_sleep)

    try:
        ws._codex_full_login_worker(sid)

        assert saved == []
        assert sid not in ws._oauth_sessions
    finally:
        ws._oauth_sessions.pop(sid, None)


def test_codex_worker_final_save_is_atomic_with_cancel_delete(tmp_path, monkeypatch):
    """The final cancellation check and the token save must be one atomic
    section under `_oauth_sessions_lock`.

    Regression: checking `cancelled` and calling `_save_codex_tokens()` used
    to be two separate steps with no lock held across them, so a DELETE
    landing in that gap flipped the flag too late for the worker to see it
    and the tokens were saved anyway. This drives a real DELETE from another
    thread exactly while the worker holds the lock for its check+save, and
    asserts DELETE stays blocked for the whole critical section instead of
    slipping in between the check and the save.
    """
    import threading

    from hermes_cli import auth as auth_mod
    from hermes_cli import web_server as ws

    class _Resp:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, **kwargs):
            if url.endswith("/deviceauth/usercode"):
                return _Resp(200, {
                    "device_auth_id": "device-auth-id",
                    "interval": 0,
                    "user_code": "CODEX-1234",
                })
            return _Resp(200, {
                "authorization_code": "auth-code",
                "code_verifier": "verifier",
            })

    class _TokenClient(_Client):
        def post(self, url, **kwargs):
            return _Resp(200, {"access_token": "at", "refresh_token": "rt"})

    clients = iter([_Client, _Client, _TokenClient])
    _make_profile_home(tmp_path, monkeypatch, profile="coder")
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: next(clients)(*a, **k))

    saved = []
    delete_threads = []
    delete_started = threading.Event()
    delete_finished = threading.Event()

    def fake_save(tokens, last_refresh=None, **kwargs):
        # We are inside the worker's critical section right now (holding
        # _oauth_sessions_lock). Fire a real DELETE from another thread and
        # prove it cannot complete until this section releases the lock.
        # Do NOT join the DELETE thread here: it is blocked on the very
        # lock this section holds, so joining here would deadlock.
        delete_thread = threading.Thread(target=_fire_delete, daemon=True)
        delete_threads.append(delete_thread)
        delete_thread.start()
        delete_started.wait(timeout=2)
        still_blocked = not delete_finished.wait(timeout=0.2)
        saved.append((tokens, still_blocked))

    def _fire_delete():
        delete_started.set()
        client.delete(f"/api/providers/oauth/sessions/{sid}?profile=coder", headers=HEADERS)
        delete_finished.set()

    monkeypatch.setattr(auth_mod, "_save_codex_tokens", fake_save)
    monkeypatch.setattr(ws.time, "sleep", lambda *_a, **_k: None)

    sid, _ = ws._new_oauth_session("openai-codex", "device_code", profile="coder")

    ws._codex_full_login_worker(sid)

    # The lock is released now (worker returned), so the DELETE thread can
    # finally complete.
    delete_threads[0].join(timeout=2)

    assert len(saved) == 1
    tokens, delete_was_still_blocked_during_save = saved[0]
    assert tokens == {"access_token": "at", "refresh_token": "rt"}
    assert delete_was_still_blocked_during_save, (
        "DELETE must block until the worker's check+save critical section "
        "finishes, not slip in between the check and the save"
    )
    # DELETE arrived after the point of no return (save already committed),
    # so this is the legitimate too-late-to-cancel outcome: token saved,
    # session subsequently removed by the now-unblocked DELETE.
    assert sid not in ws._oauth_sessions


def test_cancel_oauth_session_marks_dict_cancelled_before_popping():
    """The DELETE endpoint must flag the session dict before removing it.

    A background worker holds its own reference to the same dict object;
    it can only observe cancellation if the flag is set on that shared
    object prior to (or instead of) removal from the global session map.
    """
    from hermes_cli import web_server as ws

    session_id = "cancel-flag-test"
    ws._oauth_sessions[session_id] = {
        "session_id": session_id,
        "provider": "openai-codex",
        "flow": "device_code",
        "profile": "coder",
        "created_at": time.time(),
        "status": "pending",
        "error_message": None,
    }
    worker_ref = ws._oauth_sessions[session_id]

    resp = client.delete(
        f"/api/providers/oauth/sessions/{session_id}?profile=coder",
        headers=HEADERS,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "session_id": session_id}
    assert session_id not in ws._oauth_sessions
    assert worker_ref["cancelled"] is True


def test_nous_dashboard_poller_preserves_effective_scope_when_token_omits_scope(monkeypatch):
    from hermes_cli import auth as auth_mod
    from hermes_cli import web_server as ws

    session_id = "nous-effective-scope-test"
    ws._oauth_sessions[session_id] = {
        "session_id": session_id,
        "provider": "nous",
        "flow": "device_code",
        "created_at": time.time(),
        "status": "pending",
        "error_message": None,
        "portal_base_url": "https://portal.nousresearch.com",
        "client_id": "hermes-cli",
        "device_code": "device-code",
        "interval": 5,
        "expires_at": time.time() + 600,
        "scope": auth_mod.DEFAULT_NOUS_SCOPE,
    }
    captured_state = {}

    def fake_refresh_nous_oauth_from_state(state, **kwargs):
        captured_state.update(state)
        return {**state, "agent_key": "jwt-agent-key"}

    monkeypatch.setattr(
        auth_mod,
        "_poll_for_token",
        lambda **kwargs: {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
            "token_type": "Bearer",
        },
    )
    monkeypatch.setattr(
        auth_mod,
        "refresh_nous_oauth_from_state",
        fake_refresh_nous_oauth_from_state,
    )
    monkeypatch.setattr(auth_mod, "persist_nous_credentials", lambda state: None)

    try:
        ws._nous_poller(session_id)
        assert captured_state["scope"] == auth_mod.DEFAULT_NOUS_SCOPE
        assert ws._oauth_sessions[session_id]["status"] == "approved"
    finally:
        ws._oauth_sessions.pop(session_id, None)




def test_xai_oauth_listed_as_device_code_flow():
    """xAI Grok OAuth must surface in the catalog as a device-code flow."""
    resp = client.get("/api/providers/oauth", headers=HEADERS)
    assert resp.status_code == 200, resp.text
    providers = {p["id"]: p for p in resp.json()["providers"]}
    assert "xai-oauth" in providers
    assert providers["xai-oauth"]["flow"] == "device_code"
    assert "grok" in providers["xai-oauth"]["name"].lower()


def test_accounts_offers_every_oauth_provider_from_catalog():
    """PARITY CONTRACT: every accounts-tab provider in the unified catalog (the
    `hermes model` universe) must be offered by /api/providers/oauth. This keeps
    the desktop Accounts tab in lockstep with the CLI picker — no provider the
    CLI can sign into may be missing from the GUI.
    """
    from hermes_cli.provider_catalog import provider_catalog

    resp = client.get("/api/providers/oauth", headers=HEADERS)
    assert resp.status_code == 200, resp.text
    offered = {p["id"] for p in resp.json()["providers"]}
    for d in provider_catalog():
        if d.tab == "accounts":
            assert d.slug in offered, (
                f"{d.slug} is an accounts-tab provider in `hermes model` but is "
                f"missing from the desktop Accounts tab (/api/providers/oauth)"
            )




def test_oauth_catalog_marks_external_providers_not_disconnectable():
    """External CLI credentials are visible in Accounts but cannot be removed by Hermes."""
    resp = client.get("/api/providers/oauth", headers=HEADERS)
    assert resp.status_code == 200, resp.text
    providers = {p["id"]: p for p in resp.json()["providers"]}

    # Qwen: external and not auto-removable, and we don't know a clear command,
    # so it stays a manual hint with no runnable disconnect command.
    assert providers["qwen-oauth"]["flow"] == "external"
    assert providers["qwen-oauth"]["disconnectable"] is False
    assert "provider's CLI" in providers["qwen-oauth"]["disconnect_hint"]
    assert providers["qwen-oauth"]["disconnect_command"] is None

    # Claude Code: still not API-disconnectable, but we hand the GUI a runnable
    # command (clears the keychain entry / credentials file) so it can offer a
    # one-click "run in terminal" disconnect.
    assert providers["claude-code"]["flow"] == "external"
    assert providers["claude-code"]["disconnectable"] is False
    assert providers["claude-code"]["disconnect_hint"]
    cmd = providers["claude-code"]["disconnect_command"]
    assert cmd and ".claude/.credentials.json" in cmd


def test_external_oauth_disconnect_rejected_before_auth_mutation(monkeypatch):
    """DELETE must not pretend to remove credentials owned by another CLI."""
    from hermes_cli import auth as auth_mod

    def fail_clear_provider_auth(provider_id=None):
        raise AssertionError("external providers must not reach clear_provider_auth")

    monkeypatch.setattr(auth_mod, "clear_provider_auth", fail_clear_provider_auth)

    resp = client.delete("/api/providers/oauth/qwen-oauth", headers=HEADERS)
    assert resp.status_code == 400, resp.text
    assert "cannot be disconnected automatically" in resp.text
    assert "provider's CLI" in resp.text


def test_env_sourced_oauth_status_is_not_disconnectable(monkeypatch):
    """An env/.env-backed Anthropic API key is removed from Keys, not OAuth Accounts."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")

    resp = client.get("/api/providers/oauth", headers=HEADERS)
    assert resp.status_code == 200, resp.text
    providers = {p["id"]: p for p in resp.json()["providers"]}

    assert providers["anthropic"]["status"]["source"] == "env_var"
    assert providers["anthropic"]["disconnectable"] is False
    assert providers["anthropic"]["disconnect_hint"] == "Remove the API key from Settings → Keys instead."

    delete_resp = client.delete("/api/providers/oauth/anthropic", headers=HEADERS)
    assert delete_resp.status_code == 400, delete_resp.text
    assert "Settings" in delete_resp.text


def test_xai_dashboard_poller_seeds_single_entry_and_clears_suppression(tmp_path, monkeypatch):
    """The dashboard device-code poller must leave exactly ONE pool entry — the
    singleton-seeded ``device_code`` source — and must NOT create a parallel
    ``manual:dashboard_*`` entry.

    Dedupe: a parallel dashboard entry would share the singleton's single-use
    refresh token, and two entries racing the same rotation ->
    ``refresh_token_reused`` (on main, the dashboard login inserted exactly
    such a duplicate alongside the singleton seed). The poller writes the
    singleton only; the seed is the single source of truth.

    Suppression: an interactive dashboard login must also clear any
    ``device_code`` suppression left by a prior ``hermes auth remove
    xai-oauth``.
    """
    from hermes_cli import auth as auth_mod
    from hermes_cli import web_server as ws
    from agent.credential_pool import load_pool

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_XAI_BASE_URL", raising=False)
    monkeypatch.delenv("XAI_BASE_URL", raising=False)

    # Existing chat provider must not be overwritten by dashboard OAuth.
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "version": 1,
                "active_provider": "openrouter",
                "providers": {},
            }
        ),
        encoding="utf-8",
    )

    # Prior `hermes auth remove xai-oauth` left the source suppressed.
    auth_mod.suppress_credential_source("xai-oauth", "device_code")
    assert auth_mod.is_source_suppressed("xai-oauth", "device_code") is True

    monkeypatch.setattr(
        auth_mod,
        "_xai_oauth_discovery",
        lambda *a, **k: {"token_endpoint": "https://auth.x.ai/token"},
    )
    monkeypatch.setattr(
        auth_mod,
        "_xai_oauth_poll_device_token",
        lambda client, **kwargs: {
            "access_token": "xai-dashboard-access",
            "refresh_token": "rt-dashboard",
            "id_token": "",
            "expires_in": 3600,
            "token_type": "Bearer",
        },
    )

    session_id = "xai-dashboard-dedupe-test"
    ws._oauth_sessions[session_id] = {
        "session_id": session_id,
        "provider": "xai-oauth",
        "flow": "device_code",
        "created_at": time.time(),
        "status": "pending",
        "error_message": None,
        "device_code": "device-code",
        "interval": 5,
        "expires_at": time.time() + 600,
    }
    try:
        ws._xai_device_poller(session_id)
        assert ws._oauth_sessions[session_id]["status"] == "approved"
    finally:
        ws._oauth_sessions.pop(session_id, None)

    # The interactive dashboard login cleared the suppression marker.
    assert auth_mod.is_source_suppressed("xai-oauth", "device_code") is False

    after = json.loads(auth_path.read_text(encoding="utf-8"))
    assert after["active_provider"] == "openrouter"
    assert after["providers"]["xai-oauth"]["tokens"]["access_token"] == "xai-dashboard-access"

    # The credential pool has exactly one entry, seeded from the
    # singleton as ``device_code`` — no parallel ``manual:dashboard_*``
    # duplicate sharing the single-use refresh token.
    entries = load_pool("xai-oauth").entries()
    assert len(entries) == 1
    assert entries[0].source == "device_code"
    assert entries[0].refresh_token == "rt-dashboard"
    assert not any(
        getattr(e, "source", "").startswith("manual:dashboard") for e in entries
    )






def test_status_falls_through_to_generic_dispatcher_for_catalog_only_provider():
    """Accounts-tab providers with no hardcoded branch reflect REAL status.

    Providers appended to the Accounts tab from the unified provider_catalog()
    carry status_fn=None and may have no explicit branch in
    _resolve_provider_status. Before the fallthrough they rendered permanently
    logged-out; now they dispatch to hermes_cli.auth.get_auth_status (the
    canonical slug dispatcher) so membership AND status both auto-extend.
    """
    import hermes_cli.web_server as ws

    fake_status = {
        "logged_in": True,
        "provider": "some-future-oauth",
        "name": "Future OAuth Provider",
        "access_token": "sk-future-secret-token-xyz",
        "expires_at": "2026-12-01T00:00:00Z",
        "has_refresh_token": True,
    }
    with patch("hermes_cli.auth.get_auth_status", return_value=fake_status):
        out = ws._resolve_provider_status("some-future-oauth", None)

    assert out["logged_in"] is True
    assert out["source"] == "some-future-oauth"
    assert out["source_label"] == "Future OAuth Provider"
    # Token is previewed, never returned whole.
    assert out["token_preview"] and "sk-future-secret-token-xyz" not in out["token_preview"]
    assert out["expires_at"] == "2026-12-01T00:00:00Z"
    assert out["has_refresh_token"] is True




