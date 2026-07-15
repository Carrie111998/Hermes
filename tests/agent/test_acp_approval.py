"""Tests for agent.transports.acp_approval — generic core ACP approval bridge.

The core bridge is **generic** — no kind classification.  Tests verify:

1. CLI callback returned directly (passthrough).
2. Gateway context with notify → request_tool_approval bridge.
3. Gateway context without notify → fail-closed.
4. Neither CLI nor gateway → fail-closed.
5. Import failure → fail-closed.
6. Gateway bridge escalates all kinds identically.
7. Fail-closed always returns "deny".

Kind-aware routing tests (read/execute/write matrix) live in the plugin's
test suite (claude-code-acp/tests/test_approval.py).
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from agent.transports.acp_approval import (
    make_acp_approval_callback,
    _make_fail_closed_callback,
)


# --------------------------------------------------------------------------- #
# Test 1: CLI callback — returned directly (passthrough, no kind wrapping)
# --------------------------------------------------------------------------- #


class TestCLICallbackPassthrough:
    """When a CLI thread-local callback is registered, it is returned directly."""

    def test_cli_callback_returned_directly(self):
        """CLI callback IS returned directly — no kind-aware wrapping."""
        fake_cli_cb = MagicMock(return_value="once")

        with patch(
            "tools.terminal_tool._get_approval_callback",
            return_value=fake_cli_cb,
        ):
            result = make_acp_approval_callback()

        assert result is fake_cli_cb

    def test_cli_callback_none_falls_through(self):
        """When CLI returns None, the bridge checks gateway/fail-closed."""
        with patch(
            "tools.terminal_tool._get_approval_callback",
            return_value=None,
        ), patch(
            "tools.approval._is_gateway_approval_context",
            return_value=False,
        ):
            result = make_acp_approval_callback()

        assert result is not None
        assert result("test", "desc", kind="read") == "deny"


# --------------------------------------------------------------------------- #
# Test 2: Gateway context — all kinds go through request_tool_approval
# --------------------------------------------------------------------------- #


def _setup_gateway_env(*, approved=True, request_exc=None):
    """Set up mocks for a gateway session with a notify callback."""
    mock_approval_mod = MagicMock()
    mock_approval_mod._is_gateway_approval_context.return_value = True
    mock_approval_mod.get_current_session_key.return_value = "test-session"
    mock_approval_mod._gateway_notify_cbs = {"test-session": MagicMock()}
    mock_approval_mod._lock = threading.Lock()

    if request_exc:
        mock_request = MagicMock(side_effect=request_exc)
    else:
        mock_request = MagicMock(
            return_value={"approved": approved, "message": None}
        )
    mock_approval_mod.request_tool_approval = mock_request

    return mock_approval_mod, mock_request


class TestGatewayContextIntegration:
    """Integration: make_acp_approval_callback in a gateway context."""

    def test_gateway_callback_escalates_all_kinds(self):
        """All kinds go through request_tool_approval — no classification."""
        mod, req_fn = _setup_gateway_env(approved=True)

        with patch("tools.terminal_tool._get_approval_callback", return_value=None):
            import sys
            original = sys.modules.get("tools.approval")
            sys.modules["tools.approval"] = mod
            try:
                cb = make_acp_approval_callback()
            finally:
                if original is not None:
                    sys.modules["tools.approval"] = original
                else:
                    del sys.modules["tools.approval"]

        # All kinds go to request_tool_approval
        assert cb("read_file", "desc", kind="read") == "once"
        assert cb("ls -la", "desc", kind="execute") == "once"
        assert cb("write_file", "desc", kind="write") == "once"
        assert cb("mystery", "desc", kind="frobnicate") == "once"
        assert req_fn.call_count == 4

    def test_gateway_approved_returns_once(self):
        mod, req_fn = _setup_gateway_env(approved=True)

        with patch("tools.terminal_tool._get_approval_callback", return_value=None):
            import sys
            original = sys.modules.get("tools.approval")
            sys.modules["tools.approval"] = mod
            try:
                cb = make_acp_approval_callback()
            finally:
                if original is not None:
                    sys.modules["tools.approval"] = original
                else:
                    del sys.modules["tools.approval"]

        assert cb("tool", "desc", kind="read") == "once"

    def test_gateway_denied_returns_deny(self):
        mod, req_fn = _setup_gateway_env(approved=False)

        with patch("tools.terminal_tool._get_approval_callback", return_value=None):
            import sys
            original = sys.modules.get("tools.approval")
            sys.modules["tools.approval"] = mod
            try:
                cb = make_acp_approval_callback()
            finally:
                if original is not None:
                    sys.modules["tools.approval"] = original
                else:
                    del sys.modules["tools.approval"]

        assert cb("tool", "desc", kind="read") == "deny"

    def test_gateway_request_exception_returns_deny(self):
        mod, req_fn = _setup_gateway_env(request_exc=RuntimeError("boom"))

        with patch("tools.terminal_tool._get_approval_callback", return_value=None):
            import sys
            original = sys.modules.get("tools.approval")
            sys.modules["tools.approval"] = mod
            try:
                cb = make_acp_approval_callback()
            finally:
                if original is not None:
                    sys.modules["tools.approval"] = original
                else:
                    del sys.modules["tools.approval"]

        assert cb("tool", "desc", kind="read") == "deny"

    def test_gateway_no_notify_fail_closed(self):
        """Gateway but no notify callback → fail-closed."""
        mod = MagicMock()
        mod._is_gateway_approval_context.return_value = True
        mod.get_current_session_key.return_value = "sess-no-notify"
        mod._gateway_notify_cbs = {}
        mod._lock = threading.Lock()
        mod.request_tool_approval = MagicMock()

        with patch("tools.terminal_tool._get_approval_callback", return_value=None):
            import sys
            original = sys.modules.get("tools.approval")
            sys.modules["tools.approval"] = mod
            try:
                result = make_acp_approval_callback()
            finally:
                if original is not None:
                    sys.modules["tools.approval"] = original
                else:
                    del sys.modules["tools.approval"]

        assert result("tool", "desc", kind="execute") == "deny"
        mod.request_tool_approval.assert_not_called()


# --------------------------------------------------------------------------- #
# Test 3: Fail-closed
# --------------------------------------------------------------------------- #


class TestFailClosed:
    """Neither CLI nor gateway → fail-closed callback."""

    def test_non_cli_non_gateway_fail_closed(self):
        with patch(
            "tools.terminal_tool._get_approval_callback",
            return_value=None,
        ), patch(
            "tools.approval._is_gateway_approval_context",
            return_value=False,
        ):
            result = make_acp_approval_callback()

        assert result is not None
        assert result("any", "desc", kind="execute") == "deny"

    def test_fail_closed_always_deny(self):
        cb = _make_fail_closed_callback("test reason")
        assert cb("a", "b", kind="read") == "deny"
        assert cb("c", "d", allow_permanent=True, kind="execute") == "deny"
        assert cb("", "", kind="") == "deny"

    def test_fail_closed_for_all_kinds(self):
        """Fail-closed returns deny regardless of kind."""
        with patch(
            "tools.terminal_tool._get_approval_callback",
            return_value=None,
        ), patch(
            "tools.approval._is_gateway_approval_context",
            return_value=False,
        ):
            cb = make_acp_approval_callback()

        assert cb("tool", "desc", kind="read") == "deny"
        assert cb("tool", "desc", kind="execute") == "deny"
        assert cb("tool", "desc", kind="write") == "deny"
        assert cb("tool", "desc", kind="frobnicate") == "deny"


# --------------------------------------------------------------------------- #
# Test 4: Import failure → fail-closed
# --------------------------------------------------------------------------- #


class TestImportFailure:
    """When tools.approval import fails, return fail-closed."""

    def test_import_failure_returns_fail_closed(self):
        """Simulate tools.approval not being importable."""
        import sys

        # Save original
        original_approval = sys.modules.get("tools.approval")
        original_terminal = sys.modules.get("tools.terminal_tool")

        # Remove both modules so the lazy import fails
        sys.modules.pop("tools.approval", None)
        sys.modules.pop("tools.terminal_tool", None)

        # Insert a blocker that raises on import
        import importlib

        class _Blocker:
            def find_module(self, name, path=None):
                if name == "tools.approval":
                    return self
                return None

            def load_module(self, name):
                raise ImportError("blocked for test")

        blocker = _Blocker()
        sys.meta_path.insert(0, blocker)

        try:
            cb = make_acp_approval_callback()
            assert cb is not None
            assert cb("tool", "desc", kind="read") == "deny"
        finally:
            sys.meta_path.remove(blocker)
            if original_approval is not None:
                sys.modules["tools.approval"] = original_approval
            if original_terminal is not None:
                sys.modules["tools.terminal_tool"] = original_terminal
