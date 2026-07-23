"""Handoff ticket mint + gated middleware consume (QR phone-path server core).

Covers the security invariants for Approach D (single-use handoff ticket →
cookie session), without the public-SPA / tunnel surface (slice 2).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth import ws_tickets
from hermes_cli.dashboard_auth.base import Session, TokenPrincipal
from hermes_cli.dashboard_auth.cookies import SESSION_AT_COOKIE, SESSION_RT_COOKIE
from hermes_cli.dashboard_auth.ws_tickets import _reset_for_tests
from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider


@pytest.fixture
def gated_app():
    clear_providers()
    register_provider(StubAuthProvider())
    _reset_for_tests()
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    client = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    yield client
    clear_providers()
    _reset_for_tests()
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required


def _complete_stub_login(client) -> None:
    r1 = client.get("/auth/login?provider=stub", follow_redirects=False)
    assert r1.status_code == 302
    state = r1.headers["location"].split("state=")[1]
    r2 = client.get(
        f"/auth/callback?code=stub_code&state={state}",
        follow_redirects=False,
    )
    assert r2.status_code == 302


def _set_cookie_names(response) -> set[str]:
    """Names present in Set-Cookie headers (prefix-stripped bare names)."""
    names = set()
    # Starlette TestClient exposes set-cookie via response.cookies (jar)
    # and headers. Use both for resilience.
    for k in response.cookies.keys():
        bare = k
        for pfx in ("__Host-", "__Secure-"):
            if bare.startswith(pfx):
                bare = bare[len(pfx) :]
        names.add(bare)
    return names


# ---------------------------------------------------------------------------
# Mint endpoint auth
# ---------------------------------------------------------------------------


def test_mint_handoff_requires_auth(gated_app):
    r = gated_app.post(
        "/api/auth/handoff-ticket",
        json={"session_id": "sess-abc", "profile": "default"},
    )
    assert r.status_code == 401, f"unauth mint must be rejected, got {r.status_code}: {r.text}"


def test_mint_handoff_succeeds_when_authenticated(gated_app):
    _complete_stub_login(gated_app)
    r = gated_app.post(
        "/api/auth/handoff-ticket",
        json={"session_id": "sess-abc", "profile": "default"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ticket"].startswith(ws_tickets.HANDOFF_TICKET_PREFIX)
    assert body["ttl_seconds"] == ws_tickets.HANDOFF_TTL_SECONDS == 120
    assert body["session_id"] == "sess-abc"
    assert body["profile"] == "default"


def test_mint_handoff_requires_session_id(gated_app):
    _complete_stub_login(gated_app)
    r = gated_app.post(
        "/api/auth/handoff-ticket",
        json={"session_id": "  ", "profile": ""},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Consume path: cookie set + 302 strip
# ---------------------------------------------------------------------------


def test_consume_valid_handoff_sets_cookie_and_302_strips_param(gated_app):
    _complete_stub_login(gated_app)
    mint = gated_app.post(
        "/api/auth/handoff-ticket",
        json={"session_id": "chat-42", "profile": "work"},
    )
    assert mint.status_code == 200
    ticket = mint.json()["ticket"]

    # Fresh client = no cookies (phone scan).
    phone = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    r = phone.get(
        f"/chat?resume=chat-42&profile=work&handoff={ticket}",
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    loc = r.headers.get("location", "")
    assert "handoff=" not in loc, f"handoff must be stripped from redirect: {loc}"
    assert "resume=chat-42" in loc or "resume=chat-42" in loc.replace("%3D", "=")
    # Cookie jar should now hold a session AT.
    cookie_names = set(phone.cookies.keys())
    bare = {
        n[len("__Host-") :] if n.startswith("__Host-") else
        n[len("__Secure-") :] if n.startswith("__Secure-") else n
        for n in cookie_names
    }
    assert SESSION_AT_COOKIE in bare, f"expected AT cookie, got {cookie_names}"
    # No refresh token for handoff sessions.
    assert SESSION_RT_COOKIE not in bare, (
        f"handoff must not set refresh cookie, got {cookie_names}"
    )

    # Follow-up request with the cookie authenticates (no handoff param).
    me = phone.get("/api/auth/me")
    assert me.status_code == 200, me.text
    data = me.json()
    assert data["user_id"]
    scopes = data.get("scopes") or []
    assert "resume" in scopes
    assert "*" not in scopes
    assert "superuser" not in scopes
    assert "API_SERVER_KEY" not in scopes


def test_replay_consumed_handoff_fails_closed(gated_app):
    _complete_stub_login(gated_app)
    ticket = gated_app.post(
        "/api/auth/handoff-ticket",
        json={"session_id": "chat-1"},
    ).json()["ticket"]

    phone = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    first = phone.get(f"/chat?handoff={ticket}", follow_redirects=False)
    assert first.status_code == 302

    # New client (or same after clearing) replaying the ticket.
    attacker = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    second = attacker.get(f"/chat?handoff={ticket}", follow_redirects=False)
    # Normal unauth flow: 302 to login / auto-SSO (HTML), NOT a 500 / ticket leak.
    assert second.status_code == 302, second.text
    loc = second.headers.get("location", "")
    assert "/login" in loc or "/auth/login" in loc, loc
    # No session cookie granted.
    bare = {
        n[len("__Host-") :] if n.startswith("__Host-") else
        n[len("__Secure-") :] if n.startswith("__Secure-") else n
        for n in attacker.cookies.keys()
    }
    assert SESSION_AT_COOKIE not in bare


def test_expired_handoff_fails_closed(gated_app, monkeypatch):
    _complete_stub_login(gated_app)
    clock = {"now": 2_000_000.0}
    monkeypatch.setattr(ws_tickets.time, "time", lambda: clock["now"])

    ticket = gated_app.post(
        "/api/auth/handoff-ticket",
        json={"session_id": "chat-exp"},
    ).json()["ticket"]

    clock["now"] += ws_tickets.HANDOFF_TTL_SECONDS + 5

    phone = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    r = phone.get(f"/chat?handoff={ticket}", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers.get("location", "")
    assert "/login" in loc or "/auth/login" in loc, loc
    bare = {
        n[len("__Host-") :] if n.startswith("__Host-") else
        n[len("__Secure-") :] if n.startswith("__Secure-") else n
        for n in phone.cookies.keys()
    }
    assert SESSION_AT_COOKIE not in bare


# ---------------------------------------------------------------------------
# Cross-use: handoff vs WS ticket namespaces
# ---------------------------------------------------------------------------


def test_handoff_ticket_rejected_on_ws_auth_path(gated_app):
    """A handoff ticket must not authenticate a WS ``?ticket=`` upgrade."""
    _complete_stub_login(gated_app)
    ticket = gated_app.post(
        "/api/auth/handoff-ticket",
        json={"session_id": "chat-ws"},
    ).json()["ticket"]

    # Drive the same helper the WS endpoints use.
    class _FakeWS:
        def __init__(self, params):
            self.query_params = params
            self.headers = {}
            self.client = type("C", (), {"host": "1.2.3.4"})()
            self.app = web_server.app
            self.url = type("U", (), {"path": "/api/ws"})()

        async def close(self, code=1008):
            self.closed = code

    # Ensure gated mode is active for _ws_auth_reason.
    assert getattr(web_server.app.state, "auth_required", False) is True
    reason, cred = web_server._ws_auth_reason(_FakeWS({"ticket": ticket}))
    assert reason == "ticket_invalid"
    assert cred == "ticket"


def test_ws_ticket_rejected_on_handoff_path(gated_app):
    _complete_stub_login(gated_app)
    ws_ticket = gated_app.post("/api/auth/ws-ticket").json()["ticket"]

    phone = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    r = phone.get(f"/chat?handoff={ws_ticket}", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers.get("location", "")
    assert "/login" in loc or "/auth/login" in loc, loc
    bare = {
        n[len("__Host-") :] if n.startswith("__Host-") else
        n[len("__Secure-") :] if n.startswith("__Secure-") else n
        for n in phone.cookies.keys()
    }
    assert SESSION_AT_COOKIE not in bare


# ---------------------------------------------------------------------------
# Scope: handoff-minted session is NOT superuser
# ---------------------------------------------------------------------------


def test_handoff_minted_session_is_not_superuser(gated_app):
    _complete_stub_login(gated_app)
    ticket = gated_app.post(
        "/api/auth/handoff-ticket",
        json={"session_id": "chat-scope"},
    ).json()["ticket"]

    phone = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    phone.get(f"/?handoff={ticket}", follow_redirects=False)

    me = phone.get("/api/auth/me")
    assert me.status_code == 200, me.text
    data = me.json()
    scopes = set(data.get("scopes") or [])
    assert scopes == {"resume"}
    assert "*" not in scopes
    assert "superuser" not in scopes
    assert "API_SERVER_KEY" not in scopes

    # Cookie path yields a Session, never a TokenPrincipal with wildcard scope.
    # Prove via the access token the cookie carries.
    at = None
    for name, value in phone.cookies.items():
        bare = name
        for pfx in ("__Host-", "__Secure-"):
            if bare.startswith(pfx):
                bare = bare[len(pfx) :]
        if bare == SESSION_AT_COOKIE:
            at = value
            break
    assert at, "missing handoff access token cookie"
    session = ws_tickets.verify_handoff_session_token(at)
    assert isinstance(session, Session)
    assert not isinstance(session, TokenPrincipal)
    assert session.scopes == ("resume",)
    assert session.refresh_token == ""
    forbidden = {"*", "superuser", "API_SERVER_KEY"}
    assert not forbidden.intersection(session.scopes)
