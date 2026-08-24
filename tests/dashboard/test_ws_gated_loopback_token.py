"""Regression tests for the gated-WS loopback-token escape hatch (#93981).

A backend that declares a non-loopback ``dashboard.public_url`` engages the
gated WS auth branch even when it binds loopback — exactly how the Desktop
spawns local chat backends for edge-accessible profiles (Tailscale, LAN,
tunnels). The Desktop's readiness probe authenticates with the legacy
``?token=<_SESSION_TOKEN>``, which gated mode rejected unconditionally, so
the probe failed, the desktop tore the backend down, and the profile could
not be opened as a chat.

The contract pinned here:

  * Gated mode + loopback peer + correct ``?token=`` → accepted
    (credential ``token-loopback``).
  * Gated mode + loopback peer + WRONG ``?token=`` → still rejected.
  * Gated mode + NON-loopback peer + correct ``?token=`` → still rejected
    (remote callers need a real ticket; posture unchanged).
  * Missing peer info fails closed.
"""

from __future__ import annotations

import hmac

import pytest

from hermes_cli import web_server


@pytest.fixture
def saved_ws_auth_state():
    saved = {
        "auth_required": getattr(web_server.app.state, "auth_required", None),
        "bound_host": getattr(web_server.app.state, "bound_host", None),
    }
    yield saved
    for key, value in saved.items():
        setattr(web_server.app.state, key, value)


class _FakeWS:
    """Minimal WebSocket stand-in carrying only what _ws_auth_reason reads."""

    def __init__(self, query_params, client_host):
        self.query_params = query_params
        self.client = type("C", (), {"host": client_host})() if client_host is not None else None
        self.url = "/api/ws"
        self.headers = {}


@pytest.mark.usefixtures("saved_ws_auth_state")
def test_gated_mode_accepts_loopback_token(monkeypatch):
    web_server.app.state.auth_required = True

    token = web_server._SESSION_TOKEN
    ws = _FakeWS({"token": token}, "127.0.0.1")

    reason, credential = web_server._ws_auth_reason(ws)

    assert reason is None
    assert credential == "token-loopback"


@pytest.mark.usefixtures("saved_ws_auth_state")
def test_gated_mode_rejects_wrong_token_from_loopback(monkeypatch):
    web_server.app.state.auth_required = True

    ws = _FakeWS({"token": "wrong-token"}, "127.0.0.1")

    reason, credential = web_server._ws_auth_reason(ws)

    # Falls through to the gated ticket logic and finds no credential —
    # a wrong token must never authenticate, from anywhere.
    assert reason == "no_credential"


@pytest.mark.usefixtures("saved_ws_auth_state")
def test_gated_mode_rejects_correct_token_from_remote_peer(monkeypatch):
    """The escape hatch is loopback-peer-only: a remote client presenting even
    the correct _SESSION_TOKEN must not authenticate (the token could have
    leaked; the public dashboard's posture must not regress)."""
    web_server.app.state.auth_required = True

    token = web_server._SESSION_TOKEN
    ws = _FakeWS({"token": token}, "100.98.104.92")

    reason, credential = web_server._ws_auth_reason(ws)

    assert reason == "no_credential"


@pytest.mark.usefixtures("saved_ws_auth_state")
def test_gated_mode_fails_closed_with_no_peer(monkeypatch):
    web_server.app.state.auth_required = True

    token = web_server._SESSION_TOKEN
    ws = _FakeWS({"token": token}, None)

    reason, credential = web_server._ws_auth_reason(ws)

    assert reason == "no_credential"


@pytest.mark.usefixtures("saved_ws_auth_state")
def test_loopback_mode_still_accepts_token(monkeypatch):
    """The pre-existing loopback-mode path is unchanged."""
    web_server.app.state.auth_required = False

    token = web_server._SESSION_TOKEN
    ws = _FakeWS({"token": token}, "127.0.0.1")

    reason, credential = web_server._ws_auth_reason(ws)

    assert reason is None
    assert credential == "token"
