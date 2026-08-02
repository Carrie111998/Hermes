"""S4 private runtime proof: real uvicorn on loopback, not only TestClient.

Proves the Approach D path against a **running** gated dashboard:

1. Desk (full session) mints a single-use handoff ticket
2. Phone client consumes ``GET /chat?resume=…&handoff=…`` over real TCP
3. Lands on resume-scoped cookie session (handoff stripped from redirect)
4. Resume scope denies env/config/foreign session + unscoped WS destinations
5. Resume scope allows bound session detail + mint of bound WS tickets

Localhost only. No tunnel, public origin, phone hardware, or secret dump.
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import uvicorn
import websockets
from websockets.exceptions import ConnectionClosedError, InvalidStatus

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth import ws_tickets
from hermes_cli.dashboard_auth.cookies import LINKED_DEVICE_COOKIE
from hermes_cli.dashboard_auth.ws_tickets import _reset_for_tests
from hermes_state import SessionDB
from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _seed_session(session_id: str) -> None:
    home = Path(os.environ["HERMES_HOME"])
    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session(session_id, source="cli")
    finally:
        db.close()


def _wait_ready(base: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            # Unauth status should answer without needing a session when public.
            r = httpx.get(f"{base}/api/auth/status", timeout=1.0)
            if r.status_code in (200, 401, 403):
                return
        except Exception as exc:  # noqa: BLE001 — readiness probe
            last = exc
            time.sleep(0.05)
    raise RuntimeError(f"server not ready at {base}: {last}")


@pytest.fixture
def runtime_server(tmp_path, monkeypatch):
    """Real uvicorn bind on 127.0.0.1 with forced gated auth + stub IdP."""
    clear_providers()
    register_provider(StubAuthProvider())
    _reset_for_tests()

    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)

    port = _free_port()
    host = "127.0.0.1"
    # Force gated mode on loopback so handoff middleware engages.
    web_server.app.state.auth_required = True
    web_server.app.state.bound_host = host
    web_server.app.state.bound_port = port

    config = uvicorn.Config(
        web_server.app,
        host=host,
        port=port,
        log_level="warning",
        proxy_headers=False,
        ws_ping_interval=None,
        ws_ping_timeout=None,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="s4-handoff-runtime", daemon=True)
    thread.start()

    base = f"http://{host}:{port}"
    try:
        _wait_ready(base)
        yield {
            "base": base,
            "host": host,
            "port": port,
            "home": hermes_home,
        }
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        clear_providers()
        _reset_for_tests()
        web_server.app.state.bound_host = prev_host
        web_server.app.state.bound_port = prev_port
        web_server.app.state.auth_required = prev_required


def _desk_login(client: httpx.Client) -> None:
    r1 = client.get("/auth/login", params={"provider": "stub"}, follow_redirects=False)
    assert r1.status_code == 302, r1.text
    loc = r1.headers["location"]
    # Stub bounces to callback with code+state on same host.
    r2 = client.get(loc, follow_redirects=False)
    # Callback may 302 to / or /login-complete; cookies must be set.
    assert r2.status_code in (302, 200), r2.text
    assert client.cookies.get("hermes_session_at") or any(
        k.endswith("hermes_session_at") for k in client.cookies.keys()
    ), f"desk login did not set session cookie: {list(client.cookies.keys())}"


def _mint_ticket(
    client: httpx.Client, session_id: str, profile: str = "default"
) -> dict:
    r = client.post(
        "/api/auth/handoff-ticket",
        json={"session_id": session_id, "profile": profile},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ticket"].startswith(ws_tickets.HANDOFF_TICKET_PREFIX)
    assert body["session_id"] == session_id
    return body


def _phone_consume(
    base: str, ticket: str, session_id: str, profile: str = "default"
) -> httpx.Client:
    phone = httpx.Client(base_url=base, timeout=10.0, follow_redirects=False)
    r = phone.get(
        "/chat",
        params={"resume": session_id, "profile": profile, "handoff": ticket},
    )
    assert r.status_code == 302, f"consume expected 302, got {r.status_code}: {r.text}"
    loc = r.headers.get("location", "")
    assert "handoff=" not in loc.lower(), f"handoff must be stripped: {loc}"
    parsed = urlparse(loc)
    qs = parse_qs(parsed.query)
    assert qs.get("resume", [None])[0] == session_id
    # Land: follow redirect with phone cookies (resume session).
    land = phone.get(loc if loc.startswith("http") else loc, follow_redirects=True)
    # SPA may be unbuilt (no dist); still must not authz-deny the shell path.
    assert land.status_code not in (401, 403), land.text
    return phone


async def _ws_close_code(url: str) -> int | None:
    try:
        async with websockets.connect(url, open_timeout=5, close_timeout=5) as ws:
            await asyncio.wait_for(ws.recv(), timeout=2)
            return None
    except ConnectionClosedError as exc:
        return int(exc.code)
    except InvalidStatus as exc:
        # Handshake rejected before accept (e.g. 403/401 HTTP).
        return int(getattr(exc.response, "status_code", 0) or 0)
    except Exception:  # noqa: BLE001
        return -1


def test_s4_private_runtime_handoff_e2e(runtime_server):
    base = runtime_server["base"]
    host = runtime_server["host"]
    port = runtime_server["port"]
    session_a = "runtime-session-a"
    session_b = "runtime-session-b"
    profile = "default"

    _seed_session(session_a)
    _seed_session(session_b)

    # --- Desk mint (full dashboard session) ---
    with httpx.Client(base_url=base, timeout=10.0, follow_redirects=False) as desk:
        _desk_login(desk)
        mint = _mint_ticket(desk, session_a, profile=profile)
        ticket = mint["ticket"]
        # QR URL shape (desktop builder requires https for public_url;
        # runtime proof uses loopback HTTP origin with the same path/query).
        qr_path = f"/chat?resume={session_a}&profile={profile}&handoff={ticket}"
        assert ticket.startswith("hnd_")

        # Desk must still be full-scope (can mint, can read config).
        cfg = desk.get("/api/config")
        assert cfg.status_code not in (401, 403), cfg.text

    # --- Phone consume + land ---
    phone = _phone_consume(base, ticket, session_a, profile=profile)
    try:
        me = phone.get("/api/auth/me")
        assert me.status_code == 200, me.text
        me_body = me.json()
        # Resume identity markers when present.
        scopes = me_body.get("scopes") or me_body.get("session", {}).get("scopes")
        if scopes is not None:
            assert "resume" in scopes or scopes == ["resume"] or scopes == ("resume",)

        # --- Resume scope denials (running server) ---
        denials = {
            "POST /api/env/reveal": phone.post(
                "/api/env/reveal", json={"key": "OPENAI_API_KEY"}
            ),
            "PUT /api/env": phone.put(
                "/api/env",
                json={"key": "OPENAI_API_KEY", "value": "sk-evil-runtime"},
            ),
            "GET /api/config": phone.get("/api/config"),
            "GET /api/sessions (list)": phone.get("/api/sessions"),
            f"GET /api/sessions/{session_b} (foreign)": phone.get(
                f"/api/sessions/{session_b}"
            ),
        }
        for label, resp in denials.items():
            assert resp.status_code in (401, 403), (
                f"{label} must be denied for resume scope, got "
                f"{resp.status_code}: {resp.text[:300]}"
            )

        # Bound session must not be authz-denied.
        bound = phone.get(f"/api/sessions/{session_a}")
        assert bound.status_code not in (401, 403), bound.text

        # --- WS ticket + unscoped destination denials ---
        wt = phone.post("/api/auth/ws-ticket")
        assert wt.status_code == 200, wt.text
        wt_body = wt.json()
        ws_ticket = wt_body["ticket"]
        event_channel = wt_body.get("event_channel")
        assert event_channel, "resume WS ticket must bind event_channel"
        assert "/api/ws" not in (wt_body.get("allowed_endpoints") or [])

        # Store-side allowlist (process-local; readable in same process as server).
        with ws_tickets._lock:
            assert ws_ticket in ws_tickets._tickets
            _exp, info = ws_tickets._tickets[ws_ticket]
        allowed = set(info.get("allowed_endpoints") or [])
        assert "/api/pty" in allowed
        assert "/api/events" in allowed
        assert "/api/ws" not in allowed
        assert "/api/console" not in allowed
        assert info.get("bound_session_id") == session_a

        # Actual WebSocket upgrades against the running server.
        forbidden_paths = (
            f"/api/ws?ticket={ws_ticket}",
            f"/api/console?ticket={ws_ticket}",
            f"/api/pub?ticket={ws_ticket}&channel={event_channel}",
        )
        for path in forbidden_paths:
            # Mint a fresh single-use ticket per attempt.
            wt2 = phone.post("/api/auth/ws-ticket")
            assert wt2.status_code == 200, wt2.text
            t2 = wt2.json()["ticket"]
            chan = wt2.json().get("event_channel") or event_channel
            if path.startswith("/api/pub"):
                url = f"ws://{host}:{port}/api/pub?ticket={t2}&channel={chan}"
            elif path.startswith("/api/console"):
                url = f"ws://{host}:{port}/api/console?ticket={t2}"
            else:
                url = f"ws://{host}:{port}/api/ws?ticket={t2}"
            code = asyncio.run(_ws_close_code(url))
            assert code in (4401, 403, 401, 1008), (
                f"unscoped WS {path!r} must fail closed, got close/status {code}"
            )

        # Replay of the original handoff ticket must fail closed.
        replay = httpx.Client(base_url=base, timeout=10.0, follow_redirects=False)
        try:
            r = replay.get(
                "/chat",
                params={
                    "resume": session_a,
                    "profile": profile,
                    "handoff": ticket,
                },
            )
            # No cookies set; should not grant a session (302 with cookies would be bad).
            if r.status_code == 302:
                assert not any(LINKED_DEVICE_COOKIE in k for k in r.cookies.keys()), (
                    "replay must not mint a linked-device cookie"
                )
            else:
                assert r.status_code in (200, 401, 403)
                assert not any(LINKED_DEVICE_COOKIE in k for k in replay.cookies.keys())
        finally:
            replay.close()
    finally:
        phone.close()


def test_s4_runtime_unauth_mint_rejected(runtime_server):
    base = runtime_server["base"]
    with httpx.Client(base_url=base, timeout=10.0) as client:
        r = client.post(
            "/api/auth/handoff-ticket",
            json={"session_id": "nope", "profile": "default"},
        )
        assert r.status_code == 401, r.text
