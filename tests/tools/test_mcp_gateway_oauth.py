"""Tests for gateway-surfaced MCP OAuth consent URLs (#78169 / #78174)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def gateway_notify(monkeypatch):
    from tools import approval

    cbs = {}
    monkeypatch.setattr(approval, "_gateway_notify_cbs", cbs)
    return cbs


def _start_flow(session_key: str, *, gateway_notify):
    from tools.mcp_gateway_oauth import GatewayOAuthFlow

    notify = MagicMock()
    gateway_notify[session_key] = notify
    flow = GatewayOAuthFlow(server_name="linear", session_key=session_key)
    return flow, notify


def test_parse_and_deliver_oauth_paste(gateway_notify):
    from tools.mcp_gateway_oauth import gateway_oauth_flow, try_deliver_oauth_paste

    flow, notify = _start_flow("telegram:1", gateway_notify=gateway_notify)
    with gateway_oauth_flow(flow):
        asyncio.run(
            flow.publish_authorization_url(
                "https://auth.example/authorize?state=abc123&code_challenge=x"
            )
        )
        assert flow.authorization_url is not None
        notify.assert_called_once()
        assert notify.call_args[0][0]["kind"] == "mcp_oauth_consent"
        assert try_deliver_oauth_paste(
            "telegram:1",
            "http://127.0.0.1:9/callback?code=tok&state=abc123",
        )
        assert flow._callback == ("tok", "abc123")


def test_oauth_paste_rejects_wrong_state(gateway_notify):
    from tools.mcp_gateway_oauth import gateway_oauth_flow, try_deliver_oauth_paste

    flow, _notify = _start_flow("telegram:1", gateway_notify=gateway_notify)
    with gateway_oauth_flow(flow):
        asyncio.run(
            flow.publish_authorization_url(
                "https://auth.example/authorize?state=abc123"
            )
        )
        assert not try_deliver_oauth_paste(
            "telegram:1",
            "http://127.0.0.1:9/callback?code=tok&state=WRONG",
        )
        assert not flow._callback_ready.is_set()


def test_oauth_paste_ignores_unrelated_text(gateway_notify):
    from tools.mcp_gateway_oauth import gateway_oauth_flow, try_deliver_oauth_paste

    flow, _notify = _start_flow("telegram:1", gateway_notify=gateway_notify)
    with gateway_oauth_flow(flow):
        asyncio.run(
            flow.publish_authorization_url(
                "https://auth.example/authorize?state=abc123"
            )
        )
        assert not try_deliver_oauth_paste("telegram:1", "hello there")


def test_redirect_handler_publishes_to_gateway_flow(monkeypatch, gateway_notify):
    from tools import mcp_oauth
    from tools.mcp_gateway_oauth import GatewayOAuthFlow, gateway_oauth_flow

    gateway_notify["slack:U1"] = MagicMock()
    monkeypatch.setattr(mcp_oauth, "_raise_if_non_interactive", lambda *_a, **_k: None)

    flow = GatewayOAuthFlow(server_name="srv", session_key="slack:U1")
    handler = mcp_oauth._make_redirect_handler(1234)
    with gateway_oauth_flow(flow):
        asyncio.run(handler("https://auth.example/authorize?state=s"))

    assert flow.authorization_url == "https://auth.example/authorize?state=s"
    gateway_notify["slack:U1"].assert_called_once()


def test_is_interactive_when_gateway_flow_bound(gateway_notify):
    from tools.mcp_oauth import _is_interactive
    from tools.mcp_gateway_oauth import GatewayOAuthFlow, gateway_oauth_flow

    gateway_notify["discord:9"] = MagicMock()
    flow = GatewayOAuthFlow(server_name="srv", session_key="discord:9")
    with gateway_oauth_flow(flow):
        assert _is_interactive() is True


def test_gateway_oauth_available_requires_notify(monkeypatch):
    from tools import mcp_gateway_oauth as mod
    from tools import approval

    monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: True)
    monkeypatch.setattr(approval, "get_current_session_key", lambda: "telegram:42")
    with approval._lock:
        approval._gateway_notify_cbs.clear()
    assert mod.gateway_oauth_available() is False

    approval.register_gateway_notify("telegram:42", MagicMock())
    try:
        assert mod.gateway_oauth_available() is True
    finally:
        approval.unregister_gateway_notify("telegram:42")
