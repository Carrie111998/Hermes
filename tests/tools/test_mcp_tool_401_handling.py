"""Tests for MCP tool-handler auth-failure detection.

When a tool call raises UnauthorizedError / OAuthNonInteractiveError /
httpx.HTTPStatusError(401), the handler should:
  1. Ask MCPOAuthManager.handle_401 if recovery is viable.
  2. If yes, trigger MCPServerTask._reconnect_event and retry once.
  3. If no, return a structured needs_reauth error so the model stops
     hallucinating manual refresh attempts.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


pytest.importorskip("mcp.client.auth.oauth2")


def test_is_auth_error_detects_oauth_flow_error():
    from tools.mcp_tool import _is_auth_error
    from mcp.client.auth import OAuthFlowError

    assert _is_auth_error(OAuthFlowError("expired")) is True


def test_call_tool_handler_returns_needs_reauth_on_unrecoverable_401(monkeypatch, tmp_path):
    """When session.call_tool raises 401 and handle_401 returns False,
    handler returns a structured needs_reauth error (not a generic failure)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.mcp_tool import (
        _make_tool_handler,
        _record_tool_approval_metadata,
    )
    from tools.mcp_oauth_manager import get_manager, reset_manager_for_tests
    from mcp.client.auth import OAuthFlowError

    reset_manager_for_tests()

    # Stub server
    server = MagicMock()
    server.name = "srv"
    session = MagicMock()

    async def _call_tool_raises(*a, **kw):
        raise OAuthFlowError("token expired")

    session.call_tool = _call_tool_raises
    server.session = session
    server._reconnect_event = MagicMock()
    server._ready = MagicMock()
    server._ready.is_set.return_value = True

    from tools import mcp_tool
    mcp_tool._servers["srv"] = server
    mcp_tool._server_error_counts.pop("srv", None)
    _record_tool_approval_metadata(
        "srv",
        [SimpleNamespace(name="tool1", annotations={"readOnlyHint": True})],
    )

    # Ensure the MCP loop exists (run_on_mcp_loop needs it)
    mcp_tool._ensure_mcp_loop()

    # Force handle_401 to return False (no recovery available)
    mgr = get_manager()

    async def _h401(name, token=None):
        return False

    monkeypatch.setattr(mgr, "handle_401", _h401)

    try:
        handler = _make_tool_handler("srv", "tool1", 10.0)
        result = handler({"arg": "v"})
        parsed = json.loads(result)
        assert parsed.get("needs_reauth") is True, f"expected needs_reauth, got: {parsed}"
        assert parsed.get("server") == "srv"
        assert "re-auth" in parsed.get("error", "").lower() or "reauth" in parsed.get("error", "").lower()
    finally:
        mcp_tool._servers.pop("srv", None)
        mcp_tool._server_error_counts.pop("srv", None)
        mcp_tool._tool_read_only_hints.pop("srv", None)


def test_call_tool_handler_non_auth_error_still_generic(monkeypatch, tmp_path):
    """Non-auth exceptions still surface via the generic error path, not needs_reauth."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools.mcp_tool import (
        _make_tool_handler,
        _record_tool_approval_metadata,
    )

    server = MagicMock()
    server.name = "srv"
    session = MagicMock()

    async def _raises(*a, **kw):
        raise RuntimeError("unrelated")

    session.call_tool = _raises
    server.session = session

    from tools import mcp_tool
    mcp_tool._servers["srv"] = server
    mcp_tool._server_error_counts.pop("srv", None)
    _record_tool_approval_metadata(
        "srv",
        [SimpleNamespace(name="tool1", annotations={"readOnlyHint": True})],
    )
    mcp_tool._ensure_mcp_loop()

    try:
        handler = _make_tool_handler("srv", "tool1", 10.0)
        result = handler({"arg": "v"})
        parsed = json.loads(result)
        assert "needs_reauth" not in parsed
        assert "MCP call failed" in parsed.get("error", "")
    finally:
        mcp_tool._servers.pop("srv", None)
        mcp_tool._server_error_counts.pop("srv", None)
        mcp_tool._tool_read_only_hints.pop("srv", None)


def test_managed_lease_second_401_is_terminal_without_oauth_recovery():
    from mcp.client.auth import OAuthFlowError
    from tools import mcp_tool

    server = MagicMock()
    server._auth_type = "evaos_lease"
    mcp_tool._servers["managed"] = server
    retry_call = MagicMock()
    try:
        result = mcp_tool._handle_auth_error_and_retry(
            "managed",
            OAuthFlowError("lease rejected"),
            retry_call,
            "tool call",
        )
        parsed = json.loads(result)

        assert parsed["needs_reauth"] is True
        assert "after one lease refresh" in parsed["error"]
        retry_call.assert_not_called()
    finally:
        mcp_tool._servers.pop("managed", None)
        mcp_tool._server_error_counts.pop("managed", None)
