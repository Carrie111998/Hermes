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


def test_consent_failure_notify_timeout(gateway_notify):
    from tools.mcp_gateway_oauth import (
        GatewayOAuthFlow,
        gateway_oauth_flow,
        notify_gateway_consent_result,
    )

    flow, notify = _start_flow("telegram:1", gateway_notify=gateway_notify)
    with gateway_oauth_flow(flow):
        notify_gateway_consent_result(
            session_key="telegram:1",
            server_name="linear",
            outcome="timeout",
        )
    kinds = [c.args[0]["kind"] for c in notify.call_args_list]
    assert "mcp_oauth_consent_result" in kinds
    result_payload = next(
        c.args[0] for c in notify.call_args_list
        if c.args[0]["kind"] == "mcp_oauth_consent_result"
    )
    assert "timed out" in result_payload["description"].lower()
    assert "linear" in result_payload["description"]


def test_consent_failure_notify_cancelled_and_state(gateway_notify):
    from tools.mcp_gateway_oauth import (
        classify_oauth_consent_failure,
        notify_gateway_consent_result,
    )

    assert classify_oauth_consent_failure(TimeoutError("x")) == "timeout"
    assert classify_oauth_consent_failure(RuntimeError("user_skipped")) == "cancelled"
    assert classify_oauth_consent_failure(ValueError("state mismatch")) == "state_mismatch"

    notify = MagicMock()
    gateway_notify["telegram:9"] = notify
    notify_gateway_consent_result(
        session_key="telegram:9",
        server_name="srv",
        outcome="cancelled",
    )
    assert "cancelled" in notify.call_args[0][0]["description"].lower()


def test_oauth_paste_wrong_session_rejected(gateway_notify):
    """Paste on session B with A's state must not complete A's flow."""
    from tools.mcp_gateway_oauth import gateway_oauth_flow, try_deliver_oauth_paste

    flow_a, notify_a = _start_flow("telegram:111", gateway_notify=gateway_notify)
    flow_b, _notify_b = _start_flow("telegram:222", gateway_notify=gateway_notify)
    with gateway_oauth_flow(flow_a):
        asyncio.run(
            flow_a.publish_authorization_url(
                "https://auth.example/authorize?state=stateA"
            )
        )
        # Session B has its own flow with different state
        with gateway_oauth_flow(flow_b):
            asyncio.run(
                flow_b.publish_authorization_url(
                    "https://auth.example/authorize?state=stateB"
                )
            )
            assert not try_deliver_oauth_paste(
                "telegram:222",
                "http://127.0.0.1:9/callback?code=tok&state=stateA",
            )
            assert not flow_a._callback_ready.is_set()
            assert not flow_b._callback_ready.is_set()
        # Completing on A still works
        assert try_deliver_oauth_paste(
            "telegram:111",
            "http://127.0.0.1:9/callback?code=tok&state=stateA",
        )
        assert flow_a._callback == ("tok", "stateA")
    # State-mismatch on B should have notified
    assert any(
        c.args[0].get("kind") == "mcp_oauth_consent_result"
        and c.args[0].get("outcome") == "state_mismatch"
        for c in gateway_notify["telegram:222"].call_args_list
    )


def test_start_gateway_reauth_publishes_url(monkeypatch, gateway_notify):
    from tools import approval
    from tools.mcp_gateway_oauth import (
        GatewayOAuthFlow,
        get_gateway_oauth_flow,
        start_gateway_reauth_and_wait_for_url,
    )

    session_key = "telegram:42"
    notify = MagicMock()
    gateway_notify[session_key] = notify
    monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: True)
    monkeypatch.setattr(approval, "get_current_session_key", lambda: session_key)
    monkeypatch.setattr(
        "tools.mcp_oauth_identity.current_oauth_user_key",
        lambda require=False: "telegram:42",
    )

    def _reconnect():
        flow = get_gateway_oauth_flow()
        assert isinstance(flow, GatewayOAuthFlow)
        asyncio.run(
            flow.publish_authorization_url(
                "https://auth.example/authorize?state=reauth1"
            )
        )
        # Simulate user paste completing the wait inside reconnect.
        flow.deliver_callback(code="c", state="reauth1")

    url, user_key = start_gateway_reauth_and_wait_for_url(
        "linear", reconnect_fn=_reconnect, wait_url_timeout=5.0,
    )
    assert url == "https://auth.example/authorize?state=reauth1"
    assert user_key == "telegram:42"
    assert notify.call_args_list
    assert notify.call_args_list[0].args[0]["kind"] == "mcp_oauth_consent"
