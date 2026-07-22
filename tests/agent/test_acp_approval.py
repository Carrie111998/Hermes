"""Tests for agent.transports.acp_approval — generic core ACP approval bridge.

The core bridge is **generic** — no kind classification.  Tests verify:

1. CLI callback returned directly (passthrough).
2. Gateway context with notify → request_tool_approval bridge.
3. Gateway context without notify → fail-closed.
4. Neither CLI nor gateway → fail-closed.
5. Import failure → fail-closed.
6. Gateway bridge escalates all kinds identically.
7. Fail-closed always returns "deny".
8. Dynamic approval-bypass wrapper (yolo / mode:off honored on every path).

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
    _wrap_with_bypass_check,
)


@pytest.fixture(autouse=True)
def _bypass_off_by_default():
    """Isolate every test from ambient config by defaulting bypass to off.

    The bypass-aware wrapper consults the real ``is_approval_bypass_active()``
    at call time, which reads ``approvals.mode`` / yolo state from the caller's
    environment.  Without this fixture, a developer whose config sets
    ``approvals.mode: off`` (or ``HERMES_YOLO_MODE``) would see the gateway /
    fail-closed tests flip to ``"once"``.  Bypass-specific tests override this
    by patching ``is_approval_bypass_active`` again inside the test body.
    """
    with patch(
        "tools.approval.is_approval_bypass_active",
        return_value=False,
    ):
        yield


# --------------------------------------------------------------------------- #
# Test 1: CLI callback — returned directly (passthrough, no kind wrapping)
# --------------------------------------------------------------------------- #


class TestCLICallbackPassthrough:
    """When a CLI thread-local callback is registered, it is returned directly."""

    def test_cli_callback_wrapped_and_delegates(self):
        """CLI callback is wrapped in the bypass check but delegates to it.

        The generic bridge still does no kind-aware routing — the wrapper only
        adds the dynamic bypass check.  With bypass off (the fixture default),
        calling the returned callback forwards to the CLI callback verbatim.
        """
        fake_cli_cb = MagicMock(return_value="once")

        with patch(
            "tools.terminal_tool._get_approval_callback",
            return_value=fake_cli_cb,
        ):
            result = make_acp_approval_callback()

        # Wrapped (bypass check), not returned raw…
        assert result is not fake_cli_cb
        # …but delegates to the CLI callback when bypass is inactive.
        assert result("cmd", "desc", kind="execute") == "once"
        fake_cli_cb.assert_called_once_with(
            "cmd", "desc", allow_permanent=False, kind="execute"
        )

    def test_cli_callback_bypassed_when_active(self):
        """With bypass active, the CLI callback is skipped and 'once' returned."""
        fake_cli_cb = MagicMock(return_value="deny")

        with patch(
            "tools.terminal_tool._get_approval_callback",
            return_value=fake_cli_cb,
        ):
            result = make_acp_approval_callback()

        with patch(
            "tools.approval.is_approval_bypass_active",
            return_value=True,
        ):
            assert result("cmd", "desc", kind="execute") == "once"

        fake_cli_cb.assert_not_called()

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


# --------------------------------------------------------------------------- #
# Test 5: Dynamic approval-bypass wrapper (yolo / approvals.mode: off)
# --------------------------------------------------------------------------- #


class TestBypassWrapper:
    """The wrapper returned by make_acp_approval_callback() must honor the
    dynamic approval-bypass check on every invocation.

    Covers the required behaviors:
      (a) bypass active  -> "once", inner NOT called
      (b) bypass inactive -> inner called normally
      (c) bypass raises   -> inner called (fail-safe)

    Each behavior is verified both against ``_wrap_with_bypass_check`` directly
    (for precise inner-call assertions) and end-to-end through the factory's
    fail-closed path (the original bug: fail-closed paths never reached the
    yolo check in ``_run_approval_gate``).
    """

    # --- (a) bypass active -> "once" without calling inner --- #

    def test_bypass_active_returns_once_without_inner(self):
        inner = MagicMock(return_value="deny")
        cb = _wrap_with_bypass_check(inner)

        with patch(
            "tools.approval.is_approval_bypass_active",
            return_value=True,
        ):
            result = cb("rm -rf /", "dangerous", kind="execute")

        assert result == "once"
        inner.assert_not_called()

    def test_bypass_active_fail_closed_path_returns_once(self):
        """The previously fail-closed path now honors bypass end-to-end."""
        with patch(
            "tools.terminal_tool._get_approval_callback",
            return_value=None,
        ), patch(
            "tools.approval._is_gateway_approval_context",
            return_value=False,
        ):
            cb = make_acp_approval_callback()

        # Bypass toggled AFTER the callback was created — must still be honored
        # (the check is dynamic, evaluated per invocation).
        with patch(
            "tools.approval.is_approval_bypass_active",
            return_value=True,
        ):
            assert cb("rm -rf /", "dangerous", kind="execute") == "once"

    # --- (b) bypass inactive -> inner called normally --- #

    def test_bypass_inactive_calls_inner(self):
        inner = MagicMock(return_value="session")
        cb = _wrap_with_bypass_check(inner)

        with patch(
            "tools.approval.is_approval_bypass_active",
            return_value=False,
        ):
            result = cb("ls", "list", allow_permanent=True, kind="read")

        assert result == "session"
        inner.assert_called_once_with(
            "ls", "list", allow_permanent=True, kind="read"
        )

    def test_bypass_inactive_fail_closed_path_denies(self):
        """With bypass off, the fail-closed path still denies end-to-end."""
        with patch(
            "tools.terminal_tool._get_approval_callback",
            return_value=None,
        ), patch(
            "tools.approval._is_gateway_approval_context",
            return_value=False,
        ), patch(
            "tools.approval.is_approval_bypass_active",
            return_value=False,
        ):
            cb = make_acp_approval_callback()
            assert cb("rm -rf /", "dangerous", kind="execute") == "deny"

    # --- (c) bypass check raises -> inner called (fail-safe) --- #

    def test_bypass_check_raises_falls_through_to_inner(self):
        inner = MagicMock(return_value="always")
        cb = _wrap_with_bypass_check(inner)

        with patch(
            "tools.approval.is_approval_bypass_active",
            side_effect=RuntimeError("boom"),
        ):
            result = cb("cmd", "desc", kind="execute")

        assert result == "always"
        inner.assert_called_once()

    def test_bypass_check_raises_fail_closed_path_denies(self):
        """If the bypass check itself errors, fall through to fail-closed deny."""
        with patch(
            "tools.terminal_tool._get_approval_callback",
            return_value=None,
        ), patch(
            "tools.approval._is_gateway_approval_context",
            return_value=False,
        ), patch(
            "tools.approval.is_approval_bypass_active",
            side_effect=RuntimeError("boom"),
        ):
            cb = make_acp_approval_callback()
            assert cb("rm -rf /", "dangerous", kind="execute") == "deny"

    # --- dynamic re-evaluation across invocations --- #

    def test_bypass_is_dynamic_across_calls(self):
        """Same callback: deny while bypass off, 'once' once bypass flips on."""
        inner = MagicMock(return_value="deny")
        cb = _wrap_with_bypass_check(inner)

        with patch(
            "tools.approval.is_approval_bypass_active",
            return_value=False,
        ):
            assert cb("cmd", "desc", kind="execute") == "deny"
        assert inner.call_count == 1

        with patch(
            "tools.approval.is_approval_bypass_active",
            return_value=True,
        ):
            assert cb("cmd", "desc", kind="execute") == "once"
        # inner was NOT called a second time — bypass short-circuited it.
        assert inner.call_count == 1

