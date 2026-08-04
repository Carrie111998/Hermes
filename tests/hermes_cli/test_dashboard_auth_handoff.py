"""Handoff ticket mint + gated middleware consume (QR phone-path server core).

Covers the one-time handoff bootstrap, persistent linked-device credential,
default-deny API gate, session/profile binding and exact consume path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth import ws_tickets
from hermes_cli.dashboard_auth.base import Session, TokenPrincipal
from hermes_cli.dashboard_auth import linked_devices
from hermes_cli.dashboard_auth.cookies import LINKED_DEVICE_COOKIE, SESSION_RT_COOKIE
from hermes_cli.dashboard_auth.ws_tickets import _reset_for_tests
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


def _linked_secret(client) -> str:
    for name, value in client.cookies.items():
        if name.endswith(LINKED_DEVICE_COOKIE):
            return value
    raise AssertionError("missing linked-device cookie")


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


def _fragment_consume(
    ticket: str,
    *,
    origin: str = "https://fly-app.fly.dev",
    extra_headers: dict[str, str] | None = None,
) -> tuple[TestClient, object]:
    phone = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    headers = {
        "Origin": origin,
        "Sec-Fetch-Site": "same-origin",
        "X-Hermes-Handoff": "1",
    }
    headers.update(extra_headers or {})
    response = phone.post(
        "/api/auth/handoff-consume",
        json={"ticket": ticket},
        headers=headers,
    )
    return phone, response


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
# Fragment bootstrap transport
# ---------------------------------------------------------------------------


def test_handoff_bootstrap_is_public_and_hardened(gated_app):
    phone = TestClient(web_server.app, base_url="https://fly-app.fly.dev")

    response = phone.get("/handoff#ticket=hnd_never-sent-to-server")

    assert response.status_code == 200
    assert "hnd_never-sent-to-server" not in response.text
    assert "/api/auth/handoff-consume" in response.text
    assert "history.replaceState" in response.text
    assert response.headers["referrer-policy"] == "no-referrer"
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp

    lookalike = phone.get("/handoff-extra", follow_redirects=False)
    assert lookalike.status_code == 302
    assert "/login" in lookalike.headers["location"]


def test_fragment_bootstrap_preserves_proxy_prefix(gated_app):
    phone = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    page = phone.get(
        "/handoff",
        headers={"X-Forwarded-Prefix": "/hermes"},
    )

    assert page.status_code == 200
    assert 'fetch("/hermes/api/auth/handoff-consume"' in page.text

    _complete_stub_login(gated_app)
    ticket = _mint(gated_app, "prefixed-fragment")
    _phone, response = _fragment_consume(
        ticket,
        extra_headers={"X-Forwarded-Prefix": "/hermes"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "location": "/hermes/chat?resume=prefixed-fragment",
    }
    assert "Path=/hermes" in response.headers["set-cookie"]


def test_fragment_consume_sets_scoped_cookie_and_uses_bound_target(gated_app):
    _complete_stub_login(gated_app)
    ticket = _mint(gated_app, "fragment-session")

    phone, response = _fragment_consume(ticket)

    assert response.status_code == 200, response.text
    assert response.json() == {"location": "/chat?resume=fragment-session"}
    assert response.headers["cache-control"] == "no-store"
    bare = _bare_cookie_names(phone)
    assert LINKED_DEVICE_COOKIE in bare
    assert SESSION_RT_COOKIE not in bare

    me = phone.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["scopes"] == ["resume"]
    assert me.json()["bound_session_id"] == "fragment-session"


def test_fragment_consume_is_single_use(gated_app):
    _complete_stub_login(gated_app)
    ticket = _mint(gated_app, "single-use-fragment")

    _phone, first = _fragment_consume(ticket)
    _replay_phone, replay = _fragment_consume(ticket)

    assert first.status_code == 200
    assert replay.status_code == 401
    assert replay.json() == {"detail": "Invalid or expired handoff"}


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://attacker.example"},
        {"Sec-Fetch-Site": "cross-site"},
        {"X-Hermes-Handoff": "0"},
    ],
)
def test_rejected_fragment_request_does_not_burn_ticket(gated_app, headers):
    _complete_stub_login(gated_app)
    ticket = _mint(gated_app, "origin-guard")

    phone = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    rejected_headers = {
        "Origin": "https://fly-app.fly.dev",
        "Sec-Fetch-Site": "same-origin",
        "X-Hermes-Handoff": "1",
        **headers,
    }
    rejected = phone.post(
        "/api/auth/handoff-consume",
        json={"ticket": ticket},
        headers=rejected_headers,
    )
    _valid_phone, valid = _fragment_consume(ticket)

    assert rejected.status_code == 403
    assert valid.status_code == 200


def test_fragment_consume_requires_json_without_burning_ticket(gated_app):
    _complete_stub_login(gated_app)
    ticket = _mint(gated_app, "content-type-guard")
    phone = TestClient(web_server.app, base_url="https://fly-app.fly.dev")

    rejected = phone.post(
        "/api/auth/handoff-consume",
        content=f'{{"ticket":"{ticket}"}}',
        headers={
            "Content-Type": "text/plain",
            "Origin": "https://fly-app.fly.dev",
            "Sec-Fetch-Site": "same-origin",
            "X-Hermes-Handoff": "1",
        },
    )
    _valid_phone, valid = _fragment_consume(ticket)

    assert rejected.status_code == 403
    assert valid.status_code == 200


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
    assert LINKED_DEVICE_COOKIE in bare, f"expected AT cookie, got {bare}"
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
    assert LINKED_DEVICE_COOKIE not in _bare_cookie_names(attacker)


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
    assert LINKED_DEVICE_COOKIE not in _bare_cookie_names(phone)


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
    assert LINKED_DEVICE_COOKIE not in _bare_cookie_names(phone)


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
        if bare == LINKED_DEVICE_COOKIE:
            at = value
            break
    assert at, "missing linked device cookie"
    session = linked_devices.authenticate(at)
    assert session is not None
    assert session["session_id"] == "chat-scope"
    assert session["id"]


def test_linked_device_inactivity_ttl_is_90_days():
    assert linked_devices.DEVICE_COOKIE_TTL_SECONDS == 90 * 24 * 60 * 60


def test_linked_cookie_is_secure_http_only_lax_and_renews(gated_app):
    _complete_stub_login(gated_app)
    ticket = _mint(gated_app, "cookie-contract")

    phone, response = _fragment_consume(ticket)

    set_cookie = response.headers["set-cookie"]
    assert f"__Host-{LINKED_DEVICE_COOKIE}=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Secure" in set_cookie
    assert f"Max-Age={linked_devices.DEVICE_COOKIE_TTL_SECONDS}" in set_cookie

    renewed = phone.get("/api/auth/me")
    assert renewed.status_code == 200
    assert f"__Host-{LINKED_DEVICE_COOKIE}=" in renewed.headers["set-cookie"]


def test_linked_device_expiry_is_sliding_and_clears_invalid_cookie(
    gated_app, monkeypatch
):
    clock = {"now": 1_000_000}
    monkeypatch.setattr(linked_devices, "_now", lambda: clock["now"])
    _complete_stub_login(gated_app)
    phone = _phone_consume(_mint(gated_app, "sliding-device"))

    clock["now"] += linked_devices.DEVICE_COOKIE_TTL_SECONDS - 1
    assert phone.get("/api/auth/me").status_code == 200
    clock["now"] += linked_devices.DEVICE_COOKIE_TTL_SECONDS - 1
    assert phone.get("/api/auth/me").status_code == 200

    clock["now"] += linked_devices.DEVICE_COOKIE_TTL_SECONDS + 1
    expired = phone.get("/api/auth/me", follow_redirects=False)
    assert expired.status_code == 401
    cleared = expired.headers.get_list("set-cookie")
    host_values = [
        value for value in cleared if f"__Host-{LINKED_DEVICE_COOKIE}" in value
    ]
    assert host_values, cleared
    host_clear = host_values[0]
    assert "Max-Age=0" in host_clear
    assert "Secure" in host_clear
    assert not any(name.endswith(LINKED_DEVICE_COOKIE) for name in phone.cookies.keys())


def test_same_browser_repair_rotates_one_device_record(gated_app):
    _complete_stub_login(gated_app)
    phone = _phone_consume(_mint(gated_app, "first-bound-session"))
    old_secret = _linked_secret(phone)
    old_record = linked_devices.authenticate(old_secret)
    assert old_record is not None

    _phone, response = _fragment_consume(
        _mint(gated_app, "second-bound-session"),
        extra_headers={"Cookie": f"__Host-{LINKED_DEVICE_COOKIE}={old_secret}"},
    )
    new_secret = response.cookies.get(f"__Host-{LINKED_DEVICE_COOKIE}")
    assert new_secret and new_secret != old_secret
    new_record = linked_devices.authenticate(new_secret)

    assert linked_devices.authenticate(old_secret) is None
    assert new_record is not None
    assert new_record["id"] == old_record["id"]
    assert new_record["session_id"] == "second-bound-session"
    assert len(linked_devices.list_devices()) == 1


def test_full_session_manages_devices_but_linked_session_cannot(gated_app):
    _complete_stub_login(gated_app)
    phone = _phone_consume(_mint(gated_app, "managed-device"))
    record = linked_devices.authenticate(_linked_secret(phone))
    assert record is not None

    assert phone.get("/api/auth/linked-devices").status_code == 403
    assert phone.delete(f"/api/auth/linked-devices/{record['id']}").status_code == 403

    listed = gated_app.get("/api/auth/linked-devices")
    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "no-store"
    assert listed.json()["devices"] == [
        {
            "id": record["id"],
            "label": record["label"],
            "created_at": record["created_at"],
            "last_seen_at": record["last_seen_at"],
        }
    ]
    revoked = gated_app.delete(f"/api/auth/linked-devices/{record['id']}")
    assert revoked.status_code == 200
    assert revoked.headers["cache-control"] == "no-store"
    assert phone.get("/api/auth/me", follow_redirects=False).status_code == 401


def test_loopback_desktop_can_manage_linked_devices(gated_app):
    previous = web_server.app.state.auth_required
    previous_host = web_server.app.state.bound_host
    web_server.app.state.auth_required = False
    web_server.app.state.bound_host = "127.0.0.1"
    try:
        desktop = TestClient(web_server.app, base_url="http://127.0.0.1")
        desktop.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
        response = desktop.get("/api/auth/linked-devices")
        assert response.status_code == 200, response.text
    finally:
        web_server.app.state.auth_required = previous
        web_server.app.state.bound_host = previous_host


def test_oauth_session_takes_precedence_over_linked_cookie(gated_app):
    _complete_stub_login(gated_app)
    phone = _phone_consume(_mint(gated_app, "precedence-device"))
    assert phone.get("/api/auth/me").json()["scopes"] == ["resume"]

    _complete_stub_login(phone)
    full = phone.get("/api/auth/me")
    assert full.status_code == 200
    assert full.json()["provider"] == "stub"
    assert full.json()["scopes"] == []
    assert phone.get("/api/auth/linked-devices").status_code == 200


def test_linked_device_silently_redirects_to_its_bound_chat(gated_app):
    phone = _resume_phone(gated_app, "canonical-device-chat")

    root = phone.get("/", follow_redirects=False)
    hostile = phone.get(
        "/chat?resume=another-session&profile=evil",
        follow_redirects=False,
    )

    assert root.status_code == 302
    assert root.headers["location"] == "/chat?resume=canonical-device-chat"
    assert hostile.status_code == 302
    assert hostile.headers["location"] == "/chat?resume=canonical-device-chat"


@pytest.mark.parametrize("path", ["/settings", "/plugins", "/credentials", "/terminal"])
def test_linked_device_cannot_open_non_chat_spa_routes(gated_app, path):
    phone = _resume_phone(gated_app, "chat-only-device")
    response = phone.get(path, follow_redirects=False)
    assert response.status_code == 403, (path, response.status_code, response.text)


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
    ticket_response = r.json()
    ticket = ticket_response["ticket"]
    event_channel = ticket_response["event_channel"]
    assert web_server._VALID_CHANNEL_RE.fullmatch(event_channel)
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
    assert info.get("event_channel") == event_channel

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
        ticket_response = r2.json()
        t = ticket_response["ticket"]
        query = {"ticket": t}
        if path == "/api/events":
            query["channel"] = ticket_response["event_channel"]
        ws = _FakeWS(path, query)
        reason, cred = web_server._ws_auth_reason(ws)
        assert reason is None, (path, reason, cred)
        assert cred == "ticket"
        # Bound ticket metadata stashed for PTY handler bind enforcement.
        assert getattr(ws.state, "ws_ticket_info", None) is not None
        assert ws.state.ws_ticket_info.get("bound_session_id") == "ws-bound"
        assert ws.state.ws_ticket_info.get("allowed_endpoints") is not None
        assert (
            getattr(ws.state, "ws_ticket_event_channel")
            == ticket_response["event_channel"]
        )

    # Unbound admin sockets still denied.
    for path in ("/api/ws", "/api/console"):
        r2 = phone.post("/api/auth/ws-ticket")
        assert r2.status_code == 200, r2.text
        t = r2.json()["ticket"]
        ws = _FakeWS(path, {"ticket": t})
        reason, cred = web_server._ws_auth_reason(ws)
        assert reason == "ticket_endpoint_denied", (path, reason, cred)
        assert cred == "ticket"


def test_resume_ws_ticket_binds_events_and_pty_sidecar_to_its_channel(
    gated_app, monkeypatch
):
    """A resume ticket cannot select another session's event bridge channel."""
    import asyncio

    phone_a = _resume_phone(gated_app, "event-session-a")
    phone_b = _resume_phone(gated_app, "event-session-b")

    def mint(phone):
        response = phone.post("/api/auth/ws-ticket")
        assert response.status_code == 200, response.text
        return response.json()

    channel_a = mint(phone_a)["event_channel"]
    channel_b = mint(phone_b)["event_channel"]
    assert channel_a != channel_b

    class _FakeWS:
        def __init__(self, path, query):
            self.query_params = query
            self.headers = {}
            self.client = type("C", (), {"host": "1.2.3.4"})()
            self.app = web_server.app
            self.url = type("U", (), {"path": path})()
            self.state = type("S", (), {})()
            self.closed = []

        async def accept(self):
            return None

        async def close(self, **kwargs):
            self.closed.append(kwargs)

        async def send_text(self, _text):
            return None

    matching = mint(phone_a)
    matching_ws = _FakeWS(
        "/api/events",
        {"ticket": matching["ticket"], "channel": channel_a},
    )
    reason, credential = web_server._ws_auth_reason(matching_ws)  # type: ignore[arg-type]
    assert reason is None
    assert credential == "ticket"
    assert getattr(matching_ws.state, "ws_ticket_event_channel") == channel_a

    foreign = mint(phone_a)
    foreign_ws = _FakeWS(
        "/api/events",
        {"ticket": foreign["ticket"], "channel": channel_b},
    )
    reason, credential = web_server._ws_auth_reason(foreign_ws)  # type: ignore[arg-type]
    assert reason == "ticket_event_channel_denied"
    assert credential == "ticket"
    assert not hasattr(foreign_ws.state, "ws_ticket_event_channel")

    for invalid_channel in ("", "invalid channel"):
        invalid = mint(phone_a)
        invalid_ws = _FakeWS(
            "/api/events",
            {"ticket": invalid["ticket"], "channel": invalid_channel},
        )
        reason, credential = web_server._ws_auth_reason(invalid_ws)  # type: ignore[arg-type]
        assert reason == "ticket_event_channel_denied"
        assert credential == "ticket"

    hostile_pty = mint(phone_a)
    pty_ws = _FakeWS(
        "/api/pty",
        {"ticket": hostile_pty["ticket"], "channel": channel_b},
    )
    captured = {}

    async def fake_resolve_chat_argv_async(**kwargs):
        captured["sidecar_url"] = kwargs["sidecar_url"]
        raise web_server.HTTPException(
            status_code=400, detail="stop after sidecar capture"
        )

    monkeypatch.setattr(web_server, "_ws_host_origin_reason", lambda _ws: None)
    monkeypatch.setattr(web_server, "_ws_client_reason", lambda _ws: None)
    monkeypatch.setattr(
        web_server, "_resolve_chat_argv_async", fake_resolve_chat_argv_async
    )
    asyncio.run(web_server.pty_ws(pty_ws))  # type: ignore[arg-type]

    assert f"channel={channel_a}" in captured["sidecar_url"]
    assert f"channel={channel_b}" not in captured["sidecar_url"]


def test_revoked_device_closes_an_already_authenticated_ws(gated_app):
    import asyncio

    phone = _resume_phone(gated_app, "revoked-open-ws")
    response = phone.post("/api/auth/ws-ticket")
    assert response.status_code == 200

    class _FakeWS:
        def __init__(self):
            self.query_params = {"ticket": response.json()["ticket"]}
            self.headers = {}
            self.client = type("C", (), {"host": "1.2.3.4"})()
            self.app = web_server.app
            self.url = type("U", (), {"path": "/api/pty"})()
            self.state = type("S", (), {})()
            self.closed = []

        async def close(self, **kwargs):
            self.closed.append(kwargs)

    ws = _FakeWS()
    reason, _credential = web_server._ws_auth_reason(ws)  # type: ignore[arg-type]
    assert reason is None
    assert asyncio.run(web_server._linked_ws_device_allowed(ws)) is True

    device_id = ws.state.ws_ticket_info["device_id"]
    assert device_id
    assert linked_devices.revoke(device_id)
    assert asyncio.run(web_server._linked_ws_device_allowed(ws)) is False
    assert ws.closed == [{"code": 4401, "reason": "linked device revoked"}]


def test_full_dashboard_ws_ticket_keeps_client_event_channel_unbound(gated_app):
    """Full dashboard WS tickets retain the existing client-channel contract."""
    _complete_stub_login(gated_app)
    response = gated_app.post("/api/auth/ws-ticket")
    assert response.status_code == 200, response.text
    ticket_response = response.json()
    assert "event_channel" not in ticket_response

    class _FakeWS:
        query_params = {
            "ticket": ticket_response["ticket"],
            "channel": "dashboard-client",
        }
        headers = {}
        client = type("C", (), {"host": "1.2.3.4"})()
        app = web_server.app
        url = type("U", (), {"path": "/api/events"})()
        state = type("S", (), {})()

    ws = _FakeWS()
    reason, credential = web_server._ws_auth_reason(ws)  # type: ignore[arg-type]
    assert reason is None
    assert credential == "ticket"
    assert not hasattr(ws.state, "ws_ticket_event_channel")


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
    assert LINKED_DEVICE_COOKIE in _bare_cookie_names(phone)


# ---------------------------------------------------------------------------
# F-03 / M2: consume placement — exact GET /chat only
# ---------------------------------------------------------------------------


def test_api_config_with_handoff_does_not_set_cookies(gated_app):
    _complete_stub_login(gated_app)
    ticket = _mint(gated_app, "place-api")

    phone = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    phone.get(f"/api/config?handoff={ticket}", follow_redirects=False)
    # Must not mint cookies; ticket still live for /chat.
    assert LINKED_DEVICE_COOKIE not in _bare_cookie_names(phone)
    r2 = phone.get(f"/chat?handoff={ticket}", follow_redirects=False)
    assert r2.status_code == 302
    assert LINKED_DEVICE_COOKIE in _bare_cookie_names(phone)


def test_post_with_handoff_does_not_set_cookies(gated_app):
    _complete_stub_login(gated_app)
    ticket = _mint(gated_app, "place-post")

    phone = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    phone.post(f"/chat?handoff={ticket}", follow_redirects=False)
    assert LINKED_DEVICE_COOKIE not in _bare_cookie_names(phone)
    r2 = phone.get(f"/chat?handoff={ticket}", follow_redirects=False)
    assert r2.status_code == 302
    assert LINKED_DEVICE_COOKIE in _bare_cookie_names(phone)


def test_root_with_handoff_does_not_set_cookies(gated_app):
    _complete_stub_login(gated_app)
    ticket = _mint(gated_app, "place-root")

    phone = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    phone.get(f"/?handoff={ticket}", follow_redirects=False)
    assert LINKED_DEVICE_COOKIE not in _bare_cookie_names(phone)


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
    assert LINKED_DEVICE_COOKIE not in _bare_cookie_names(phone), hostile_path

    # Ticket remains valid for exact /chat once.
    r2 = phone.get(f"/chat?handoff={ticket}", follow_redirects=False)
    assert r2.status_code == 302, (hostile_path, r2.status_code, r2.text)
    assert LINKED_DEVICE_COOKIE in _bare_cookie_names(phone)


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
        assert LINKED_DEVICE_COOKIE not in _bare_cookie_names(phone2), path

    r_ok = phone.get(f"/chat?handoff={ticket}", follow_redirects=False)
    assert r_ok.status_code == 302
    assert LINKED_DEVICE_COOKIE in _bare_cookie_names(phone)


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
    assert not is_handoff_consume_request(_req("/chat", raw=b"/chat", method="POST"))
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
    assert LINKED_DEVICE_COOKIE not in _bare_cookie_names(phone)

    forced = ForceDecodedChatStripRaw(web_server.app)
    phone_alias = TestClient(forced, base_url="https://fly-app.fly.dev")
    phone_alias.get(f"/%63hat?handoff={ticket}", follow_redirects=False)
    assert LINKED_DEVICE_COOKIE not in _bare_cookie_names(phone_alias)

    # Ticket still valid once for exact /chat with present raw_path.
    phone_ok = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    r_ok = phone_ok.get(f"/chat?handoff={ticket}", follow_redirects=False)
    assert r_ok.status_code == 302, r_ok.text
    assert LINKED_DEVICE_COOKIE in _bare_cookie_names(phone_ok)

    # Replay denied.
    phone_replay = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    phone_replay.get(f"/chat?handoff={ticket}", follow_redirects=False)
    assert LINKED_DEVICE_COOKIE not in _bare_cookie_names(phone_replay)


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
    assert LINKED_DEVICE_COOKIE in _bare_cookie_names(phone)

    # Single-use: replay with same prefix must not mint again.
    phone2 = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    phone2.get(
        f"/chat?handoff={ticket}",
        headers={"X-Forwarded-Prefix": "/hermes"},
        follow_redirects=False,
    )
    assert LINKED_DEVICE_COOKIE not in _bare_cookie_names(phone2)


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
    assert LINKED_DEVICE_COOKIE not in _bare_cookie_names(phone)

    r2 = phone.get(f"/chat?handoff={ticket}", follow_redirects=False)
    assert r2.status_code == 302
    assert LINKED_DEVICE_COOKIE in _bare_cookie_names(phone)


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
    assert LINKED_DEVICE_COOKIE not in _bare_cookie_names(phone)

    r2 = phone.get(f"/chat?handoff={ticket}", follow_redirects=False)
    assert r2.status_code == 302
    assert LINKED_DEVICE_COOKIE in _bare_cookie_names(phone)


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


def test_consume_handoff_ticket_rejects_non_exact_scopes(gated_app):
    """M3: ticket store payload with admin/mixed scopes fails closed."""
    ticket = ws_tickets.mint_handoff_ticket(
        session_id="s-scope",
        user_id="u1",
        provider="stub",
    )
    with ws_tickets._handoff_db() as db:
        row = db.execute(
            "SELECT payload_json FROM handoff_tickets WHERE ticket_hash=?",
            (ws_tickets._handoff_hash(ticket),),
        ).fetchone()
        assert row is not None
        evil = json.loads(row[0])
        evil["scopes"] = ["resume", "admin"]
        db.execute(
            "UPDATE handoff_tickets SET payload_json=? WHERE ticket_hash=?",
            (json.dumps(evil), ws_tickets._handoff_hash(ticket)),
        )
    with pytest.raises(ws_tickets.TicketInvalid, match="forbidden handoff scope"):
        ws_tickets.consume_handoff_ticket(ticket)
