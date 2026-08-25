"""Regression tests for the gated-WS spawn-ticket bootstrap (#93981).

A backend that declares a non-loopback ``dashboard.public_url`` engages the
gated WS auth branch even when it binds loopback — exactly how the Desktop
spawns local chat backends for edge-accessible profiles (Tailscale, LAN,
tunnels). Gated mode rejects the legacy ``?token=`` upgrade unconditionally
(a pinned security invariant, tested in
``tests/hermes_cli/test_dashboard_auth_ws_auth.py``), so the Desktop's
readiness probe must instead mint a single-use ticket through
``POST /api/auth/spawn-ticket`` — token-guarded (X-Hermes-Session-Token),
public-listed to bypass the cookie gate.

The contract pinned here:

  * The pinned invariant stands: gated mode rejects legacy ``?token=``,
    even from a loopback peer holding the in-process token value.
  * ``/api/auth/spawn-ticket`` requires the session token; without it → 401.
    A non-string ``_SESSION_TOKEN`` (misconfigured env injection) and a
    case-variant ``bearer`` scheme are also rejected cleanly here: the
    handler 401s instead of raising (no 500s from the auth path) while
    still accepting any RFC 9110 scheme casing.
  * With the token, it mints a ticket that ``_ws_auth_ok`` accepts exactly
    once (single-use).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server


@pytest.fixture
def gated_client():
    """web_server.app in gated mode, like the ws-auth suite's gated_app."""
    from hermes_cli.dashboard_auth.ws_tickets import _reset_for_tests

    _reset_for_tests()
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    client = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    yield client
    _reset_for_tests()
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required


class _FakeWS:
    def __init__(self, query_params, client_host):
        self.query_params = query_params
        self.client = type("C", (), {"host": client_host})() if client_host is not None else None
        self.url = SimpleNamespace(path="/api/ws")
        self.headers = {}


@pytest.mark.usefixtures("gated_client")
def test_gated_mode_still_rejects_legacy_token():
    """The pinned invariant: ?token= is dead in gated mode, loopback or not."""
    token = web_server._SESSION_TOKEN
    ws = _FakeWS({"token": token}, "127.0.0.1")

    reason, credential = web_server._ws_auth_reason(ws)

    assert reason == "no_credential"


@pytest.mark.usefixtures("gated_client")
def test_spawn_ticket_requires_session_token(gated_client):
    resp = gated_client.post("/api/auth/spawn-ticket")

    assert resp.status_code == 401


@pytest.mark.usefixtures("gated_client")
def test_spawn_ticket_mints_usable_single_use_ticket(gated_client):
    ok_header = {"X-Hermes-Session-Token": web_server._SESSION_TOKEN}

    first = gated_client.post("/api/auth/spawn-ticket", headers=ok_header)
    assert first.status_code == 200
    ticket = first.json()["ticket"]
    assert ticket

    # The ticket authenticates one WS upgrade...
    ws = _FakeWS({"ticket": ticket}, "127.0.0.1")
    reason, credential = web_server._ws_auth_reason(ws)
    assert reason is None
    assert credential == "ticket"

    # ...and only one: single-use.
    reason2, _ = web_server._ws_auth_reason(_FakeWS({"ticket": ticket}, "127.0.0.1"))
    assert reason2 == "ticket_invalid"


@pytest.mark.usefixtures("gated_client")
def test_wrong_spawn_token_rejected(gated_client):
    resp = gated_client.post(
        "/api/auth/spawn-ticket",
        headers={"X-Hermes-Session-Token": "not-the-token"},
    )

    assert resp.status_code == 401


@pytest.mark.usefixtures("gated_client")
def test_spawn_ticket_accepts_case_variant_bearer_scheme(gated_client):
    """RFC 9110: the auth-scheme token is case-insensitive."""
    resp = gated_client.post(
        "/api/auth/spawn-ticket",
        headers={"Authorization": f"bearer {web_server._SESSION_TOKEN}"},
    )

    assert resp.status_code == 200


@pytest.mark.usefixtures("gated_client")
def test_non_string_session_token_401s_instead_of_raising(gated_client, monkeypatch):
    """A misconfigured env injection must not turn the auth check into a 500."""
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", None)

    resp = gated_client.post("/api/auth/spawn-ticket")

    assert resp.status_code == 401
