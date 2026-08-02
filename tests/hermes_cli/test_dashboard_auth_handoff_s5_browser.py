"""S5 exposure-prep proofs that stay on localhost.

1. TTL / revocation contract (constants + runtime expiry + logout clears cookie)
2. Real browser execution of fragment bootstrap + gated ``buildWsUrl``

No public tunnel and no external origin. The browser proof still verifies that
the handoff capability never reaches the HTTP URL.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth import linked_devices
from hermes_cli.dashboard_auth import ws_tickets
from hermes_cli.dashboard_auth.cookies import LINKED_DEVICE_COOKIE
from hermes_cli.dashboard_auth.ws_tickets import (
    HANDOFF_TTL_SECONDS,
    _reset_for_tests,
)
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


def _wait_ready(base: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base}/api/auth/status", timeout=1.0)
            if r.status_code in (200, 401, 403):
                return
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.05)
    raise RuntimeError(f"server not ready at {base}: {last}")


@pytest.fixture
def runtime_server(tmp_path, monkeypatch):
    clear_providers()
    register_provider(StubAuthProvider())
    _reset_for_tests()

    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)

    # NOTE: the SPA build is deliberately NOT required here. `web_dist` is a
    # gitignored build artifact and CI's Python slices never run `npm run build`,
    # so asserting on it in the fixture failed both tests in CI even though only
    # the browser test needs a shell to load. web_server guards its own SPA mount
    # (`not WEB_DIST.exists()`), so the server boots fine without it. The browser
    # test skips on a missing SPA itself, next to its agent-browser skip.

    port = _free_port()
    host = "127.0.0.1"
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
    thread = threading.Thread(target=server.run, name="s5-handoff-browser", daemon=True)
    thread.start()
    base = f"http://{host}:{port}"
    try:
        _wait_ready(base)
        yield {"base": base, "host": host, "port": port}
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
    r2 = client.get(r1.headers["location"], follow_redirects=False)
    assert r2.status_code in (302, 200), r2.text


def _consume_fragment(client: httpx.Client, base: str, ticket: str):
    return client.post(
        "/api/auth/handoff-consume",
        json={"ticket": ticket},
        headers={
            "Origin": base,
            "Sec-Fetch-Site": "same-origin",
            "X-Hermes-Handoff": "1",
        },
    )


def test_s5_ttl_and_revocation_contract(runtime_server, monkeypatch):
    """One-time QR plus persistent linked device, including logout revoke."""
    assert HANDOFF_TTL_SECONDS == 120
    assert linked_devices.DEVICE_COOKIE_TTL_SECONDS == 90 * 24 * 60 * 60

    base = runtime_server["base"]
    sid = "s5-ttl-session"
    _seed_session(sid)

    with httpx.Client(base_url=base, timeout=10.0, follow_redirects=False) as desk:
        _desk_login(desk)
        r = desk.post(
            "/api/auth/handoff-ticket",
            json={"session_id": sid, "profile": "default"},
        )
        assert r.status_code == 200, r.text
        ticket = r.json()["ticket"]
        assert r.json()["ttl_seconds"] == 120

    # Expire the handoff ticket via process clock double.
    clock = {"now": time.time()}
    monkeypatch.setattr(ws_tickets.time, "time", lambda: clock["now"])
    clock["now"] += HANDOFF_TTL_SECONDS + 1

    phone = httpx.Client(base_url=base, timeout=10.0, follow_redirects=False)
    try:
        expired = _consume_fragment(phone, base, ticket)
        # Expired: must not establish a resume session cookie.
        assert not any(LINKED_DEVICE_COOKIE in k for k in phone.cookies.keys()), (
            f"expired handoff must not link a device: {list(phone.cookies.keys())}"
        )
        assert expired.status_code == 401
    finally:
        phone.close()

    # Fresh mint + consume, then logout revokes cookie path.
    with httpx.Client(base_url=base, timeout=10.0, follow_redirects=False) as desk:
        _desk_login(desk)
        ticket2 = desk.post(
            "/api/auth/handoff-ticket",
            json={"session_id": sid, "profile": "default"},
        ).json()["ticket"]

    phone2 = httpx.Client(base_url=base, timeout=10.0, follow_redirects=False)
    try:
        c = _consume_fragment(phone2, base, ticket2)
        assert c.status_code == 200, c.text
        assert c.json()["location"] == f"/chat?resume={sid}&profile=default"
        assert any(LINKED_DEVICE_COOKIE in k for k in phone2.cookies.keys())
        me = phone2.get("/api/auth/me")
        assert me.status_code == 200, me.text
        lo = phone2.post("/auth/logout")
        assert lo.status_code in (200, 204, 302), lo.text
        me2 = phone2.get("/api/auth/me")
        assert me2.status_code in (401, 403), me2.text
    finally:
        phone2.close()


def test_s5_browser_build_ws_url_after_handoff(runtime_server, tmp_path):
    """Real Chromium via agent-browser: handoff consume + gated buildWsUrl path."""
    # This test, unlike the TTL contract above, genuinely needs the built shell:
    # it asserts on the gated SPA's injected globals. `web_dist` is gitignored and
    # CI does not build it, so skip rather than fail there.
    if not (web_server.WEB_DIST / "index.html").is_file():
        pytest.skip(
            f"SPA not built at {web_server.WEB_DIST}; run: cd web && npm run build"
        )

    base = runtime_server["base"]
    sid = "s5-browser-session"
    _seed_session(sid)

    with httpx.Client(base_url=base, timeout=10.0, follow_redirects=False) as desk:
        _desk_login(desk)
        ticket = desk.post(
            "/api/auth/handoff-ticket",
            json={"session_id": sid, "profile": "default"},
        ).json()["ticket"]

    which = subprocess.run(["which", "agent-browser"], capture_output=True, text=True)
    if which.returncode != 0:
        pytest.skip("agent-browser not installed")

    # Browser is the first consumer of the handoff URL (Set-Cookie on 302).
    handoff_url = f"{base}/handoff#ticket={ticket}"

    # Keep JS free of nested quotes so agent-browser eval shell-wrapping is safe.
    # Mirrors web/src/lib/api.ts buildWsUrl gated branch:
    # POST /api/auth/ws-ticket → ticket + event_channel overwrite on /api/pty.
    js_path = tmp_path / "s5_build_ws_url.js"
    js_path.write_text(
        """
(async function() {
  var authRequired = !!window.__HERMES_AUTH_REQUIRED__;
  var hasToken = !!(window.__HERMES_SESSION_TOKEN__);
  var meStatus = -1;
  var wsTicketStatus = -1;
  var eventChannel = null;
  var ticket = null;
  try {
    var meRes = await fetch('/api/auth/me', { credentials: 'include' });
    meStatus = meRes.status;
  } catch (e) { meStatus = -1; }
  try {
    var wtRes = await fetch('/api/auth/ws-ticket', {
      method: 'POST', credentials: 'include'
    });
    wsTicketStatus = wtRes.status;
    if (wtRes.status === 200) {
      var body = await wtRes.json();
      ticket = body.ticket || null;
      eventChannel = body.event_channel || null;
    }
  } catch (e) { wsTicketStatus = -1; }
  var built = null;
  if (ticket) {
    var u = new URL('/api/pty', window.location.origin);
    u.protocol = (window.location.protocol === 'https:') ? 'wss:' : 'ws:';
    u.searchParams.set('ticket', ticket);
    if (eventChannel) {
      u.searchParams.set('channel', eventChannel);
    }
    built = {
      href: u.toString(),
      hasTicket: u.searchParams.has('ticket'),
      channel: u.searchParams.get('channel'),
      serverChannel: eventChannel
    };
  }
  return {
    authRequired: authRequired,
    hasToken: hasToken,
    viewport: { width: window.innerWidth, height: window.innerHeight },
    meStatus: meStatus,
    wsTicketStatus: wsTicketStatus,
    eventChannel: eventChannel,
    built: built,
    href: window.location.href
  };
})()
""".strip(),
        encoding="utf-8",
    )

    session = f"s5-handoff-{os.getpid()}-{int(time.time())}"
    env = os.environ.copy()

    def _ab(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["agent-browser", "--session", session, *args],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

    try:
        r_device = _ab("set", "device", "iPhone 12")
        assert r_device.returncode == 0, r_device.stderr or r_device.stdout
        r_open = _ab("open", handoff_url, "--headless")
        assert r_open.returncode == 0, r_open.stderr or r_open.stdout
        r_wait = _ab("wait", "--load", "networkidle")
        assert r_wait.returncode == 0, r_wait.stderr or r_wait.stdout
        # Give SPA a moment to settle after redirect strip.
        time.sleep(0.5)
        r_url = _ab("get", "url", "--json")
        assert r_url.returncode == 0, r_url.stderr or r_url.stdout
        url_payload = json.loads(r_url.stdout)
        final_url = (
            url_payload.get("url")
            if isinstance(url_payload, dict)
            else str(url_payload)
        )
        if isinstance(url_payload, dict) and "data" in url_payload:
            final_url = url_payload["data"].get("url") or url_payload.get("url")
        assert "#ticket=" not in str(final_url).lower(), final_url
        assert "handoff=" not in str(final_url).lower(), final_url
        assert "resume=" in str(final_url), final_url

        # Eval file contents via stdin-safe one-liner: read file in Python, pass as arg.
        js = js_path.read_text(encoding="utf-8")
        r_eval = _ab("eval", js, "--json")
        assert r_eval.returncode == 0, (
            f"eval failed:\nstdout={r_eval.stdout[-2000:]}\nstderr={r_eval.stderr[-2000:]}"
        )
        payload = json.loads(r_eval.stdout)
        found = payload
        if isinstance(payload, dict):
            if "result" in payload and isinstance(payload["result"], dict):
                found = payload["result"]
            elif "data" in payload and isinstance(payload["data"], dict):
                found = payload["data"]
                if "result" in found and isinstance(found["result"], dict):
                    found = found["result"]
        assert isinstance(found, dict), found
        assert found.get("authRequired") is True, found
        assert found.get("hasToken") is False, found
        viewport = found.get("viewport") or {}
        assert 320 <= int(viewport.get("width") or 0) <= 430, viewport
        assert int(viewport.get("height") or 0) >= 600, viewport
        assert found.get("meStatus") == 200, found
        assert found.get("wsTicketStatus") == 200, found
        assert found.get("eventChannel"), found
        built = found.get("built") or {}
        assert built.get("hasTicket") is True, built
        assert built.get("channel") == found.get("eventChannel"), built
        assert str(built.get("href", "")).startswith("ws://"), built
        assert "handoff=" not in (found.get("href") or ""), found
        assert "#ticket=" not in (found.get("href") or ""), found
    finally:
        _ab("close")
