"""Tests for agent.transports.acp_approval — core ACP approval bridge.

Tests verify:

1. CLI callback used as the underlying channel (passthrough for non-execute).
2. Gateway context with notify → request_tool_approval bridge.
3. Gateway context without notify → fail-closed.
4. Neither CLI nor gateway → fail-closed.
5. Import failure → fail-closed.
6. Gateway bridge escalates non-execute kinds; execute → command guards.
7. Fail-closed always returns "deny".
8. Dynamic approval-bypass wrapper (yolo / mode:off honored on every path,
   outermost — wins over the command guards).
9. Execute routing: kind="execute" with a command goes through
   check_all_command_guards (approved → "once", denied → "deny", guard
   failure → channel); other kinds and empty commands go to the channel.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from agent.transports.acp_approval import (
    make_acp_approval_callback,
    _make_fail_closed_callback,
    _wrap_with_bypass_check,
    _wrap_with_execute_command_guards,
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
        """CLI callback is wrapped but non-execute kinds still delegate to it.

        With bypass off (the fixture default) and a non-execute kind, calling
        the returned callback forwards to the CLI callback verbatim.
        (kind="execute" with a command is intercepted by the command guards —
        see TestExecuteCommandGuards.)
        """
        fake_cli_cb = MagicMock(return_value="once")

        with patch(
            "tools.terminal_tool._get_approval_callback",
            return_value=fake_cli_cb,
        ):
            result = make_acp_approval_callback()

        # Wrapped (bypass check + execute guards), not returned raw…
        assert result is not fake_cli_cb
        # …but delegates to the CLI callback for non-execute kinds.
        assert result("cmd", "desc", kind="read") == "once"
        fake_cli_cb.assert_called_once_with(
            "cmd", "desc", allow_permanent=False, kind="read"
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

    def test_gateway_escalates_non_execute_kinds(self):
        """Non-execute kinds go through request_tool_approval; execute is
        decided by the native command guards, not the gateway channel."""
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

        with patch(
            "tools.approval.check_all_command_guards",
            return_value={"approved": True, "message": None},
        ) as guards:
            # Non-execute kinds escalate to request_tool_approval.
            assert cb("read_file", "desc", kind="read") == "once"
            assert cb("write_file", "desc", kind="write") == "once"
            assert cb("mystery", "desc", kind="frobnicate") == "once"
            assert req_fn.call_count == 3
            # Execute is decided by the command guards instead.
            assert cb("ls -la", "desc", kind="execute") == "once"
            guards.assert_called_once()
            assert req_fn.call_count == 3

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

        # Non-execute kind: nothing can answer, so fail-closed deny.
        assert result("tool", "desc", kind="read") == "deny"
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
        assert result("any", "desc", kind="read") == "deny"

    def test_fail_closed_always_deny(self):
        cb = _make_fail_closed_callback("test reason")
        assert cb("a", "b", kind="read") == "deny"
        assert cb("c", "d", allow_permanent=True, kind="execute") == "deny"
        assert cb("", "", kind="") == "deny"

    def test_fail_closed_for_all_kinds(self):
        """Fail-closed returns deny regardless of kind.

        The execute case is pinned to a denied guard result so the assertion
        is deterministic (an unpatched guard would auto-approve in a
        non-interactive test environment).
        """
        with patch(
            "tools.terminal_tool._get_approval_callback",
            return_value=None,
        ), patch(
            "tools.approval._is_gateway_approval_context",
            return_value=False,
        ):
            cb = make_acp_approval_callback()

        with patch(
            "tools.approval.check_all_command_guards",
            return_value={"approved": False, "message": "BLOCKED"},
        ):
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
        # (the check is dynamic, evaluated per invocation).  The bypass wrapper
        # is outermost, so the execute command guards never run.
        with patch(
            "tools.approval.is_approval_bypass_active",
            return_value=True,
        ), patch(
            "tools.approval.check_all_command_guards",
        ) as guards:
            assert cb("rm -rf /", "dangerous", kind="execute") == "once"
        guards.assert_not_called()

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
        """With bypass off, a guard-denied execute still denies end-to-end."""
        with patch(
            "tools.terminal_tool._get_approval_callback",
            return_value=None,
        ), patch(
            "tools.approval._is_gateway_approval_context",
            return_value=False,
        ), patch(
            "tools.approval.is_approval_bypass_active",
            return_value=False,
        ), patch(
            "tools.approval.check_all_command_guards",
            return_value={"approved": False, "message": "BLOCKED"},
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
        ), patch(
            "tools.approval.check_all_command_guards",
            return_value={"approved": False, "message": "BLOCKED"},
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


# --------------------------------------------------------------------------- #
# Test 6: Execute routing — kind="execute" → native command guards
# --------------------------------------------------------------------------- #


class TestExecuteCommandGuards:
    """kind="execute" with a command is decided by check_all_command_guards;
    other kinds, empty commands, and guard failures fall through to the
    underlying channel.

    The wrapper is tested directly (precise inner/guard call assertions) and
    end-to-end through the factory.
    """

    # --- direct wrapper behavior --- #

    def test_execute_approved_returns_once_without_inner(self):
        inner = MagicMock(return_value="deny")
        cb = _wrap_with_execute_command_guards(inner)

        with patch(
            "tools.approval.check_all_command_guards",
            return_value={"approved": True, "message": None},
        ) as guards:
            assert cb("ls -la", "list files", kind="execute") == "once"

        # Guard received the raw command and the optional CLI callback kwarg.
        (command, _env_type), kwargs = guards.call_args
        assert command == "ls -la"
        assert "approval_callback" in kwargs
        inner.assert_not_called()

    def test_execute_denied_returns_deny_without_inner(self):
        inner = MagicMock(return_value="once")
        cb = _wrap_with_execute_command_guards(inner)

        with patch(
            "tools.approval.check_all_command_guards",
            return_value={"approved": False, "message": "BLOCKED"},
        ):
            assert cb("rm -rf /", "wipe", kind="execute") == "deny"

        inner.assert_not_called()

    def test_execute_kind_match_is_case_insensitive(self):
        inner = MagicMock(return_value="deny")
        cb = _wrap_with_execute_command_guards(inner)

        with patch(
            "tools.approval.check_all_command_guards",
            return_value={"approved": True, "message": None},
        ):
            assert cb("ls", "", kind="Execute") == "once"
            assert cb("ls", "", kind="EXECUTE") == "once"

        inner.assert_not_called()

    def test_non_execute_kinds_skip_guards_and_call_inner(self):
        inner = MagicMock(return_value="session")
        cb = _wrap_with_execute_command_guards(inner)

        with patch("tools.approval.check_all_command_guards") as guards:
            for kind in ("read", "edit", "write", "delete", "frobnicate", ""):
                assert cb("cmd", "desc", kind=kind) == "session"

        guards.assert_not_called()
        assert inner.call_count == 6

    def test_execute_empty_command_falls_through_to_inner(self):
        """No command text → nothing to guard → the channel decides."""
        inner = MagicMock(return_value="always")
        cb = _wrap_with_execute_command_guards(inner)

        with patch("tools.approval.check_all_command_guards") as guards:
            assert cb("", "desc", kind="execute") == "always"
            assert cb("   ", "desc", kind="execute") == "always"

        guards.assert_not_called()
        assert inner.call_count == 2

    def test_execute_guard_exception_falls_through_to_inner(self):
        """Unexpected guard failure → channel decides (fail-safe fall-through)."""
        inner = MagicMock(return_value="deny")
        cb = _wrap_with_execute_command_guards(inner)

        with patch(
            "tools.approval.check_all_command_guards",
            side_effect=RuntimeError("boom"),
        ):
            assert cb("ls", "desc", kind="execute") == "deny"

        inner.assert_called_once()

    # --- end-to-end through the factory --- #

    def test_execute_uses_guards_not_fail_closed_channel(self):
        """An approved guard result wins over the fail-closed channel."""
        with patch(
            "tools.terminal_tool._get_approval_callback",
            return_value=None,
        ), patch(
            "tools.approval._is_gateway_approval_context",
            return_value=False,
        ), patch(
            "tools.approval.check_all_command_guards",
            return_value={"approved": True, "message": None},
        ) as guards:
            cb = make_acp_approval_callback()
            assert cb("git status", "show status", kind="execute") == "once"
            # A non-execute kind on the same callback still fails closed.
            assert cb("read_file", "desc", kind="read") == "deny"

        guards.assert_called_once()

    def test_execute_guards_run_before_cli_callback(self):
        """A safe execute command auto-approves without the CLI callback."""
        fake_cli_cb = MagicMock(return_value="deny")

        with patch(
            "tools.terminal_tool._get_approval_callback",
            return_value=fake_cli_cb,
        ), patch(
            "tools.approval.check_all_command_guards",
            return_value={"approved": True, "message": None},
        ):
            cb = make_acp_approval_callback()
            assert cb("ls -la", "list", kind="execute") == "once"

        fake_cli_cb.assert_not_called()

    def test_bypass_wins_over_guards_end_to_end(self):
        """yolo / mode=off short-circuits before the guards ever run."""
        with patch(
            "tools.terminal_tool._get_approval_callback",
            return_value=None,
        ), patch(
            "tools.approval._is_gateway_approval_context",
            return_value=False,
        ), patch(
            "tools.approval.check_all_command_guards",
        ) as guards, patch(
            "tools.approval.is_approval_bypass_active",
            return_value=True,
        ):
            cb = make_acp_approval_callback()
            assert cb("rm -rf /", "dangerous", kind="execute") == "once"

        guards.assert_not_called()

