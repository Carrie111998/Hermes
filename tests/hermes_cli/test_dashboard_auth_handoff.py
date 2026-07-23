"""Handoff ticket mint + gated middleware consume (QR phone-path server core).

Covers Approach D security invariants + Oscar F-01..F-04 scoped authz:
single-use handoff ticket → resume-scoped cookie session, default-deny API
gate, session/profile bind, consume only on GET /chat, shortened session TTL.
Without the public-SPA / tunnel surface (slice 2).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth import ws_tickets
from hermes_cli.dashboard_auth.base import Session, TokenPrincipal
from hermes_cli.dashboard_auth.cookies import SESSION_AT_COOKIE, SESSION_RT_COOKIE
from hermes_cli.dashboard_auth.ws_tickets import (
    HANDOFF_SESSION_TTL_SECONDS,
    _reset_for_tests,
)
from hermes_state import SessionDB
from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider


@pytest.fixture
def gated_app(tmp_path, monkeypatch):
    clear_providers()
    register_provider(StubAuthProvider())
    _reset_for_tests()
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
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


def _seed_session(session_id: str) -> str:
    home = Path(os.environ["HERMES_HOME"])
    db = SessionDB(db_path=home / "state.db")
    try:
        return db.create_session(session_id, source="cli")
    finally:
        db.close()


def _complete_stub_login(client) -> None:
    r1 = client.get("/auth/login?provider=stub", follow_redirects=False)
    assert r1.status_code == 302
    state = r1.headers["location"].split("state=")[1]
    r2 = client.get(
        f"/auth/callback?code=stub_code&state={state}",
        follow_redirects=False,
    )
    assert r2.status_code == 302


def _bare_cookie_names(client_or_response) -> set[str]:
    names = set()
    cookies = getattr(client_or_response, "cookies", None) or {}
    for k in cookies.keys():
        bare = k
        for pfx in ("__Host-", "__Secure-"):
            if bare.startswith(pfx):
                bare = bare[len(pfx) :]
        names.add(bare)
    return names


def _mint(client, session_id: str, profile: str = "") -> str:
    _seed_session(session_id)
    body = {"session_id": session_id}
    if profile:
        body["profile"] = profile
    r = client.post("/api/auth/handoff-ticket", json=body)
    assert r.status_code == 200, r.text
    return r.json()["ticket"]


def _phone_consume(ticket: str, *, extra_qs: str = "") -> TestClient:
    phone = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    qs = f"handoff={ticket}"
    if extra_qs:
        qs = f"{qs}&{extra_qs}"
    r = phone.get(f"/chat?{qs}", follow_redirects=False)
    assert r.status_code == 302, r.text
    return phone


# ---------------------------------------------------------------------------
# Mint endpoint auth
# ---------------------------------------------------------------------------


def test_mint_handoff_requires_auth(gated_app):
    r = gated_app.post(
        "/api/auth/handoff-ticket",
        json={"session_id": "sess-abc", "profile": "default"},
    )
    assert r.status_code == 401, (
        f"unauth mint must be rejected, got {r.status_code}: {r.text}"
    )


def test_mint_handoff_succeeds_when_authenticated(gated_app):
    _complete_stub_login(gated_app)
    _seed_session("sess-abc")
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


def test_mint_handoff_rejects_unknown_session(gated_app):
    _complete_stub_login(gated_app)
    r = gated_app.post(
        "/api/auth/handoff-ticket",
        json={"session_id": "does-not-exist"},
    )
    assert r.status_code == 404, r.text


def test_mint_handoff_rejects_unknown_profile(gated_app):
    _complete_stub_login(gated_app)
    _seed_session("sess-p")
    r = gated_app.post(
        "/api/auth/handoff-ticket",
        json={"session_id": "sess-p", "profile": "nope-profile-xyz"},
    )
    assert r.status_code in (400, 404), r.text


# ---------------------------------------------------------------------------
# Consume path: cookie set + 302 strip (F-03 GET /chat)
# ---------------------------------------------------------------------------


def test_consume_valid_handoff_sets_cookie_and_302_strips_param(gated_app):
    _complete_stub_login(gated_app)
    ticket = _mint(gated_app, "chat-42", profile="default")

    # Fresh client = no cookies (phone scan).
    phone = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    r = phone.get(
        f"/chat?resume=chat-42&profile=default&handoff={ticket}",
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    loc = r.headers.get("location", "")
    assert "handoff=" not in loc, f"handoff must be stripped from redirect: {loc}"
    assert "resume=chat-42" in loc
    assert "profile=default" in loc
    bare = _bare_cookie_names(phone)
    assert SESSION_AT_COOKIE in bare, f"expected AT cookie, got {bare}"
    # No refresh token for handoff sessions.
    assert SESSION_RT_COOKIE not in bare, (
        f"handoff must not set refresh cookie, got {bare}"
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
    assert data.get("bound_session_id") == "chat-42"


def test_replay_consumed_handoff_fails_closed(gated_app):
    _complete_stub_login(gated_app)
    ticket = _mint(gated_app, "chat-1")

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
    assert SESSION_AT_COOKIE not in _bare_cookie_names(attacker)


def test_expired_handoff_fails_closed(gated_app, monkeypatch):
    _complete_stub_login(gated_app)
    clock = {"now": 2_000_000.0}
    monkeypatch.setattr(ws_tickets.time, "time", lambda: clock["now"])

    ticket = _mint(gated_app, "chat-exp")

    clock["now"] += ws_tickets.HANDOFF_TTL_SECONDS + 5

    phone = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    r = phone.get(f"/chat?handoff={ticket}", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers.get("location", "")
    assert "/login" in loc or "/auth/login" in loc, loc
    assert SESSION_AT_COOKIE not in _bare_cookie_names(phone)


# ---------------------------------------------------------------------------
# Cross-use: handoff vs WS ticket namespaces
# ---------------------------------------------------------------------------


def test_handoff_ticket_rejected_on_ws_auth_path(gated_app):
    """A handoff ticket must not authenticate a WS ``?ticket=`` upgrade."""
    _complete_stub_login(gated_app)
    ticket = _mint(gated_app, "chat-ws")

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
    assert SESSION_AT_COOKIE not in _bare_cookie_names(phone)


# ---------------------------------------------------------------------------
# Scope: handoff-minted session is NOT superuser
# ---------------------------------------------------------------------------


def test_handoff_minted_session_is_not_superuser(gated_app):
    _complete_stub_login(gated_app)
    ticket = _mint(gated_app, "chat-scope")

    phone = _phone_consume(ticket)

    me = phone.get("/api/auth/me")
    assert me.status_code == 200, me.text
    data = me.json()
    scopes = set(data.get("scopes") or [])
    assert scopes == {"resume"}
    assert "*" not in scopes
    assert "superuser" not in scopes
    assert "API_SERVER_KEY" not in scopes

    # Cookie path yields a Session, never a TokenPrincipal with wildcard scope.
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
    assert session.bound_session_id == "chat-scope"
    forbidden = {"*", "superuser", "API_SERVER_KEY"}
    assert not forbidden.intersection(session.scopes)


def test_handoff_session_ttl_is_short():
    """F-04: session TTL is 30–60 minutes (45m)."""
    assert 30 * 60 <= HANDOFF_SESSION_TTL_SECONDS <= 60 * 60
    assert HANDOFF_SESSION_TTL_SECONDS == 45 * 60


# ---------------------------------------------------------------------------
# F-01: resume cookie default-deny (Oscar probes)
# ---------------------------------------------------------------------------


def _resume_phone(gated_app, session_id: str = "resume-bound") -> TestClient:
    _complete_stub_login(gated_app)
    ticket = _mint(gated_app, session_id)
    return _phone_consume(ticket)


def test_resume_cookie_denies_env_reveal(gated_app):
    """M4: real sink is POST /api/env/reveal, not GET /api/env/{key}/reveal."""
    phone = _resume_phone(gated_app)
    r = phone.post("/api/env/reveal", json={"key": "OPENAI_API_KEY"})
    assert r.status_code in (401, 403), r.text


def test_resume_cookie_denies_env_put(gated_app):
    """M4: real sink is PUT /api/env, not PUT /api/env/{key}."""
    phone = _resume_phone(gated_app)
    r = phone.put(
        "/api/env",
        json={"key": "OPENAI_API_KEY", "value": "sk-evil"},
    )
    assert r.status_code in (401, 403), r.text


def test_resume_cookie_denies_config_get(gated_app):
    phone = _resume_phone(gated_app)
    r = phone.get("/api/config")
    assert r.status_code in (401, 403), r.text


def test_resume_cookie_denies_sessions_list(gated_app):
    phone = _resume_phone(gated_app)
    r = phone.get("/api/sessions")
    assert r.status_code in (401, 403), r.text


def test_resume_cookie_denies_unbound_session_detail(gated_app):
    phone = _resume_phone(gated_app, "bound-only")
    _seed_session("other-session")
    r = phone.get("/api/sessions/other-session")
    assert r.status_code in (401, 403), r.text


def test_resume_cookie_allows_bound_session_detail(gated_app):
    phone = _resume_phone(gated_app, "bound-only")
    r = phone.get("/api/sessions/bound-only")
    # Must not be authz-denied; route may 200 or 404 depending on profile wiring.
    assert r.status_code not in (401, 403), r.text


def test_resume_cookie_ws_ticket_allows_bound_pty_denies_unbound_ws(gated_app):
    """Slice 2: resume WS tickets open bound /api/pty (+ events); not /api/ws.

    Ticket bind metadata is recorded; destination allow-list is non-empty for
    chat-needed endpoints only. Unbound admin sockets stay denied.
    """
    from hermes_cli.dashboard_auth.scopes import RESUME_WS_ENDPOINTS

    assert "/api/pty" in RESUME_WS_ENDPOINTS
    assert "/api/events" in RESUME_WS_ENDPOINTS
    assert "/api/ws" not in RESUME_WS_ENDPOINTS
    assert "/api/console" not in RESUME_WS_ENDPOINTS

    phone = _resume_phone(gated_app, "ws-bound")
    r = phone.post("/api/auth/ws-ticket")
    assert r.status_code == 200, r.text
    ticket = r.json()["ticket"]
    with ws_tickets._lock:
        assert ticket in ws_tickets._tickets
        _exp, info = ws_tickets._tickets[ticket]
    assert info.get("scopes") == ["resume"]
    allowed = info.get("allowed_endpoints")
    assert allowed is not None
    assert "/api/pty" in allowed
    assert "/api/events" in allowed
    assert "/api/ws" not in allowed
    assert "/api/console" not in allowed
    assert info.get("bound_session_id") == "ws-bound"

    class _FakeWS:
        def __init__(self, path, query=None):
            self.query_params = query or {"ticket": ticket}
            self.headers = {}
            self.client = type("C", (), {"host": "1.2.3.4"})()
            self.app = web_server.app
            self.url = type("U", (), {"path": path})()
            self.state = type("S", (), {})()

    # Allowed destinations: consume succeeds with ticket credential.
    for path in ("/api/pty", "/api/events"):
        r2 = phone.post("/api/auth/ws-ticket")
        assert r2.status_code == 200, r2.text
        t = r2.json()["ticket"]
        ws = _FakeWS(path, {"ticket": t})
        reason, cred = web_server._ws_auth_reason(ws)
        assert reason is None, (path, reason, cred)
        assert cred == "ticket"
        # Bound ticket metadata stashed for PTY handler bind enforcement.
        assert getattr(ws.state, "ws_ticket_info", None) is not None
        assert ws.state.ws_ticket_info.get("bound_session_id") == "ws-bound"
        assert ws.state.ws_ticket_info.get("allowed_endpoints") is not None

    # Unbound admin sockets still denied.
    for path in ("/api/ws", "/api/console"):
        r2 = phone.post("/api/auth/ws-ticket")
        assert r2.status_code == 200, r2.text
        t = r2.json()["ticket"]
        ws = _FakeWS(path, {"ticket": t})
        reason, cred = web_server._ws_auth_reason(ws)
        assert reason == "ticket_endpoint_denied", (path, reason, cred)
        assert cred == "ticket"


def test_bound_pty_ticket_ignores_client_resume_and_profile(gated_app):
    """Hostile phone path: client resume/profile/fresh cannot pivot bind.

    Auth accepts /api/pty for a resume-scoped ticket; bind metadata on the
    ticket wins over query params. (Full PTY accept is not driven here — the
    starlette TestClient WS path is flaky; we assert the bind stash the
    handler reads.)
    """
    _complete_stub_login(gated_app)
    ticket = _mint(gated_app, "real-session", profile="default")
    phone = _phone_consume(ticket)
    r = phone.post("/api/auth/ws-ticket")
    assert r.status_code == 200, r.text
    t = r.json()["ticket"]
    with ws_tickets._lock:
        _exp, info = ws_tickets._tickets[t]
    assert info.get("bound_session_id") == "real-session"
    # default profile is canonicalised; empty or "default" both ok for bind.
    assert (info.get("bound_profile") or "") in ("", "default")

    class _FakeWS:
        def __init__(self):
            self.query_params = {
                "ticket": t,
                "resume": "attacker-session",
                "profile": "evil",
                "fresh": "1",
            }
            self.headers = {}
            self.client = type("C", (), {"host": "1.2.3.4"})()
            self.app = web_server.app
            self.url = type("U", (), {"path": "/api/pty"})()
            self.state = type("S", (), {})()

    ws = _FakeWS()
    reason, cred = web_server._ws_auth_reason(ws)
    assert reason is None, (reason, cred)
    assert cred == "ticket"
    ti = ws.state.ws_ticket_info
    assert ti.get("bound_session_id") == "real-session"
    assert (ti.get("bound_profile") or "") in ("", "default")

    # Handler helper, not a copied test implementation: ticket bind must
    # override hostile query params and disable a new-session request.
    assert web_server._pty_resume_params(ws) == ("real-session", "default", False)


def test_resume_cookie_cannot_mint_handoff(gated_app):
    phone = _resume_phone(gated_app, "no-remint")
    r = phone.post(
        "/api/auth/handoff-ticket",
        json={"session_id": "no-remint"},
    )
    assert r.status_code in (401, 403), r.text


# ---------------------------------------------------------------------------
# F-02: ticket-bound redirect wins over client query
# ---------------------------------------------------------------------------


def test_consume_ticket_wins_over_resume_query_mismatch(gated_app):
    _complete_stub_login(gated_app)
    ticket = _mint(gated_app, "real-session", profile="default")

    phone = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    r = phone.get(
        f"/chat?handoff={ticket}&resume=attacker-session&profile=evil",
        follow_redirects=False,
    )
    assert r.status_code == 302
    loc = r.headers.get("location", "")
    assert "resume=real-session" in loc
    assert "attacker-session" not in loc
    assert "profile=default" in loc
    assert "evil" not in loc
    assert SESSION_AT_COOKIE in _bare_cookie_names(phone)


# ---------------------------------------------------------------------------
# F-03 / M2: consume placement — exact GET /chat only
# ---------------------------------------------------------------------------


def test_api_config_with_handoff_does_not_set_cookies(gated_app):
    _complete_stub_login(gated_app)
    ticket = _mint(gated_app, "place-api")

    phone = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    phone.get(f"/api/config?handoff={ticket}", follow_redirects=False)
    # Must not mint cookies; ticket still live for /chat.
    assert SESSION_AT_COOKIE not in _bare_cookie_names(phone)
    r2 = phone.get(f"/chat?handoff={ticket}", follow_redirects=False)
    assert r2.status_code == 302
    assert SESSION_AT_COOKIE in _bare_cookie_names(phone)


def test_post_with_handoff_does_not_set_cookies(gated_app):
    _complete_stub_login(gated_app)
    ticket = _mint(gated_app, "place-post")

    phone = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    phone.post(f"/chat?handoff={ticket}", follow_redirects=False)
    assert SESSION_AT_COOKIE not in _bare_cookie_names(phone)
    r2 = phone.get(f"/chat?handoff={ticket}", follow_redirects=False)
    assert r2.status_code == 302
    assert SESSION_AT_COOKIE in _bare_cookie_names(phone)


def test_root_with_handoff_does_not_set_cookies(gated_app):
    _complete_stub_login(gated_app)
    ticket = _mint(gated_app, "place-root")

    phone = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    phone.get(f"/?handoff={ticket}", follow_redirects=False)
    assert SESSION_AT_COOKIE not in _bare_cookie_names(phone)


@pytest.mark.parametrize(
    "hostile_path",
    [
        "/chat/",
        "/chat//",
        "/nested/chat",
        "/api/config/chat",
        "/api/chat",
        "//chat",
        "/CHAT",
        "/chat/extra",
    ],
)
def test_hostile_path_does_not_consume_handoff(gated_app, hostile_path):
    """M2: non-canonical paths must not mint cookies or burn the ticket."""
    _complete_stub_login(gated_app)
    ticket = _mint(gated_app, "place-hostile")

    phone = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    phone.get(f"{hostile_path}?handoff={ticket}", follow_redirects=False)
    assert SESSION_AT_COOKIE not in _bare_cookie_names(phone), hostile_path

    # Ticket remains valid for exact /chat once.
    r2 = phone.get(f"/chat?handoff={ticket}", follow_redirects=False)
    assert r2.status_code == 302, (hostile_path, r2.status_code, r2.text)
    assert SESSION_AT_COOKIE in _bare_cookie_names(phone)


def _handoff_scope_request(
    path: str,
    *,
    raw: object | None = ...,
    omit_raw: bool = False,
    headers: list | None = None,
    method: str = "GET",
):
    """Build a Starlette Request with optional ASGI raw_path control."""
    from starlette.requests import Request

    scope: dict = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "query_string": b"",
        "headers": headers or [],
        "client": ("1.2.3.4", 1234),
        "server": ("fly-app.fly.dev", 443),
    }
    if not omit_raw:
        if raw is ...:
            scope["raw_path"] = path.encode("ascii")
        else:
            scope["raw_path"] = raw
    return Request(scope)


def test_encoded_path_variants_do_not_consume_handoff(gated_app):
    """M2: encoded separators / letters must not satisfy exact /chat."""
    from hermes_cli.dashboard_auth.scopes import is_handoff_consume_request

    _req = _handoff_scope_request

    # Decoded lookalikes that must fail when raw_path is non-canonical.
    assert not is_handoff_consume_request(_req("/chat", raw=b"/%63hat"))
    assert not is_handoff_consume_request(_req("/chat", raw=b"/chat%2F"))
    assert not is_handoff_consume_request(
        _req("/api/config/chat", raw=b"/api%2Fconfig%2Fchat")
    )
    assert not is_handoff_consume_request(_req("/chat/", raw=b"/chat/"))
    assert not is_handoff_consume_request(_req("/nested/chat", raw=b"/nested/chat"))
    # Canonical exact path is accepted (bytes and bytearray).
    assert is_handoff_consume_request(_req("/chat", raw=b"/chat"))
    assert is_handoff_consume_request(_req("/chat", raw=bytearray(b"/chat")))
    # F-01: client X-Forwarded-Prefix must not redefine consume path.
    assert is_handoff_consume_request(
        _req(
            "/chat",
            raw=b"/chat",
            headers=[(b"x-forwarded-prefix", b"/hermes")],
        )
    )
    assert not is_handoff_consume_request(
        _req(
            "/nested/chat",
            raw=b"/nested/chat",
            headers=[(b"x-forwarded-prefix", b"/nested")],
        )
    )
    assert not is_handoff_consume_request(
        _req(
            "/hermes/chat",
            raw=b"/hermes/chat",
            headers=[(b"x-forwarded-prefix", b"/hermes")],
        )
    )

    _complete_stub_login(gated_app)
    ticket = _mint(gated_app, "place-encoded")
    phone = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    # Live client: percent-encoded path segments that ASGI may decode.
    for path in ("/%63hat", "/chat%2F", "/api%2Fconfig%2Fchat"):
        phone2 = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
        phone2.get(f"{path}?handoff={ticket}", follow_redirects=False)
        assert SESSION_AT_COOKIE not in _bare_cookie_names(phone2), path

    r_ok = phone.get(f"/chat?handoff={ticket}", follow_redirects=False)
    assert r_ok.status_code == 302
    assert SESSION_AT_COOKIE in _bare_cookie_names(phone)


def test_missing_or_non_byte_raw_path_does_not_authorise_consume():
    """F-02 slice 1.4: absent/None/str/wrong-type raw_path fail closed."""
    from hermes_cli.dashboard_auth.scopes import is_handoff_consume_request

    _req = _handoff_scope_request

    assert not is_handoff_consume_request(_req("/chat", omit_raw=True))
    assert not is_handoff_consume_request(_req("/chat", raw=None))
    assert not is_handoff_consume_request(_req("/chat", raw="/chat"))
    assert not is_handoff_consume_request(_req("/chat", raw=memoryview(b"/chat")))
    assert not is_handoff_consume_request(_req("/chat", raw=123))
    assert not is_handoff_consume_request(_req("/chat", raw=b"/%63hat"))
    # Non-GET still rejected even with canonical raw.
    assert not is_handoff_consume_request(
        _req("/chat", raw=b"/chat", method="POST")
    )
    assert is_handoff_consume_request(_req("/chat", raw=b"/chat"))


def test_missing_raw_path_live_middleware_does_not_consume(gated_app):
    """F-02 slice 1.4: ASGI wrapper stripping raw_path must not burn ticket.

    Oscar proof shape: decoded path /chat (incl. /%63hat decode) with raw_path
    removed must not set cookie; same ticket then consumes once on exact /chat.
    """
    from starlette.types import ASGIApp, Receive, Scope, Send

    class StripRawPath:
        def __init__(self, app: ASGIApp) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope.get("type") == "http":
                scope = dict(scope)
                scope.pop("raw_path", None)
            await self.app(scope, receive, send)

    class ForceDecodedChatStripRaw:
        """Simulate servers that decode /%63hat → path /chat without raw_path."""

        def __init__(self, app: ASGIApp) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope.get("type") == "http":
                scope = dict(scope)
                path = scope.get("path") or ""
                raw = scope.get("raw_path")
                if path in ("/%63hat",) or raw in (b"/%63hat", "/%63hat"):
                    scope["path"] = "/chat"
                scope.pop("raw_path", None)
            await self.app(scope, receive, send)

    _complete_stub_login(gated_app)
    ticket = _mint(gated_app, "missing-raw-live")

    stripped = StripRawPath(web_server.app)
    phone = TestClient(stripped, base_url="https://fly-app.fly.dev")
    phone.get(f"/chat?handoff={ticket}", follow_redirects=False)
    assert SESSION_AT_COOKIE not in _bare_cookie_names(phone)

    forced = ForceDecodedChatStripRaw(web_server.app)
    phone_alias = TestClient(forced, base_url="https://fly-app.fly.dev")
    phone_alias.get(f"/%63hat?handoff={ticket}", follow_redirects=False)
    assert SESSION_AT_COOKIE not in _bare_cookie_names(phone_alias)

    # Ticket still valid once for exact /chat with present raw_path.
    phone_ok = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    r_ok = phone_ok.get(f"/chat?handoff={ticket}", follow_redirects=False)
    assert r_ok.status_code == 302, r_ok.text
    assert SESSION_AT_COOKIE in _bare_cookie_names(phone_ok)

    # Replay denied.
    phone_replay = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    phone_replay.get(f"/chat?handoff={ticket}", follow_redirects=False)
    assert SESSION_AT_COOKIE not in _bare_cookie_names(phone_replay)


# ---------------------------------------------------------------------------
# F-01 / slice 1.3: X-Forwarded-Prefix is not consume authz
# ---------------------------------------------------------------------------


def test_bare_chat_with_forwarded_prefix_consumes_and_redirects(gated_app):
    """Legitimate proxy shape: ASGI path /chat + X-Forwarded-Prefix: /hermes."""
    _complete_stub_login(gated_app)
    ticket = _mint(gated_app, "pfx-hermes")

    phone = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    r = phone.get(
        f"/chat?handoff={ticket}",
        headers={"X-Forwarded-Prefix": "/hermes"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    loc = r.headers.get("location", "")
    assert loc.startswith("/hermes/chat?")
    assert "resume=pfx-hermes" in loc
    assert SESSION_AT_COOKIE in _bare_cookie_names(phone)

    # Single-use: replay with same prefix must not mint again.
    phone2 = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    phone2.get(
        f"/chat?handoff={ticket}",
        headers={"X-Forwarded-Prefix": "/hermes"},
        follow_redirects=False,
    )
    assert SESSION_AT_COOKIE not in _bare_cookie_names(phone2)


def test_nested_chat_with_matching_forwarded_prefix_does_not_consume(gated_app):
    """F-01: /nested/chat + X-Forwarded-Prefix: /nested must not consume."""
    _complete_stub_login(gated_app)
    ticket = _mint(gated_app, "pfx-nested")

    phone = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    phone.get(
        f"/nested/chat?handoff={ticket}",
        headers={"X-Forwarded-Prefix": "/nested"},
        follow_redirects=False,
    )
    assert SESSION_AT_COOKIE not in _bare_cookie_names(phone)

    r2 = phone.get(f"/chat?handoff={ticket}", follow_redirects=False)
    assert r2.status_code == 302
    assert SESSION_AT_COOKIE in _bare_cookie_names(phone)


def test_api_config_chat_with_matching_forwarded_prefix_does_not_consume(gated_app):
    """F-01: /api/config/chat + X-Forwarded-Prefix: /api/config must not consume."""
    _complete_stub_login(gated_app)
    ticket = _mint(gated_app, "pfx-api-config")

    phone = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    phone.get(
        f"/api/config/chat?handoff={ticket}",
        headers={"X-Forwarded-Prefix": "/api/config"},
        follow_redirects=False,
    )
    assert SESSION_AT_COOKIE not in _bare_cookie_names(phone)

    r2 = phone.get(f"/chat?handoff={ticket}", follow_redirects=False)
    assert r2.status_code == 302
    assert SESSION_AT_COOKIE in _bare_cookie_names(phone)


# ---------------------------------------------------------------------------
# M3: exact ('resume',) scopes only
# ---------------------------------------------------------------------------


def test_exact_handoff_scopes_reject_admin_and_extras():
    from hermes_cli.dashboard_auth.scopes import (
        EXACT_HANDOFF_SCOPES,
        exact_handoff_scopes_or_none,
        sanitize_handoff_scopes,
    )

    assert exact_handoff_scopes_or_none(None) == EXACT_HANDOFF_SCOPES
    assert exact_handoff_scopes_or_none(()) == EXACT_HANDOFF_SCOPES
    assert exact_handoff_scopes_or_none(("resume",)) == EXACT_HANDOFF_SCOPES
    assert exact_handoff_scopes_or_none(("resume", "resume")) == EXACT_HANDOFF_SCOPES
    assert exact_handoff_scopes_or_none(("admin",)) is None
    assert exact_handoff_scopes_or_none(("resume", "admin")) is None
    assert exact_handoff_scopes_or_none(("*",)) is None
    assert exact_handoff_scopes_or_none(("unknown",)) is None
    assert sanitize_handoff_scopes(("resume", "admin")) == ()
    assert sanitize_handoff_scopes(("admin",)) == ()


def test_verify_handoff_token_rejects_admin_scope(gated_app):
    """M3: forged admin/mixed scope payloads must not yield a Session."""
    payload_admin = {
        "sub": "u1",
        "email": "",
        "name": "",
        "org_id": "",
        "provider": "stub",
        "session_id": "s1",
        "profile": "default",
        "scopes": ["admin"],
        "kind": "handoff",
        "exp": 2_000_000_000,
    }
    payload_mixed = dict(payload_admin)
    payload_mixed["scopes"] = ["resume", "admin"]
    payload_ok = dict(payload_admin)
    payload_ok["scopes"] = ["resume"]
    payload_empty = dict(payload_admin)
    payload_empty["scopes"] = []

    tok_admin = ws_tickets._sign_handoff_session(payload_admin)
    tok_mixed = ws_tickets._sign_handoff_session(payload_mixed)
    tok_ok = ws_tickets._sign_handoff_session(payload_ok)
    tok_empty = ws_tickets._sign_handoff_session(payload_empty)

    assert ws_tickets.verify_handoff_session_token(tok_admin) is None
    assert ws_tickets.verify_handoff_session_token(tok_mixed) is None
    sess = ws_tickets.verify_handoff_session_token(tok_ok)
    assert sess is not None
    assert sess.scopes == ("resume",)
    sess_empty = ws_tickets.verify_handoff_session_token(tok_empty)
    assert sess_empty is not None
    assert sess_empty.scopes == ("resume",)


def test_consume_handoff_ticket_rejects_non_exact_scopes(gated_app):
    """M3: ticket store payload with admin/mixed scopes fails closed."""
    ticket = ws_tickets.mint_handoff_ticket(
        session_id="s-scope",
        user_id="u1",
        provider="stub",
    )
    with ws_tickets._lock:
        exp, info = ws_tickets._handoff_tickets[ticket]
        evil = dict(info)
        evil["scopes"] = ["resume", "admin"]
        ws_tickets._handoff_tickets[ticket] = (exp, evil)
    with pytest.raises(ws_tickets.TicketInvalid, match="forbidden handoff scope"):
        ws_tickets.consume_handoff_ticket(ticket)
