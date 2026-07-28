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
    _extract_acp_fields,
    _make_fail_closed_callback,
    _wrap_with_bypass_check,
    _wrap_with_execute_command_guards,
)


def _tc(title: str, kind: str = "", *, tool_call_id: str = "tc-1") -> dict:
    """Build a minimal raw ACP toolCall dict for tests.

    Mirrors the unadapted shape that reaches the core bridge when no plugin
    format adapter is wired in: just ``title`` / ``kind`` / ``toolCallId``.
    The core bridge's generic fallback extraction derives ``command_label``
    from ``title`` (or ``kind``), so for execute-kind tests the ``title``
    doubles as the command fed to the command guards.
    """
    tc: dict = {"toolCallId": tool_call_id}
    if title:
        tc["title"] = title
    if kind:
        tc["kind"] = kind
    return tc


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
        assert result(_tc("cmd", "read")) == "once"
        fake_cli_cb.assert_called_once_with(
            _tc("cmd", "read"), allow_permanent=False
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
            assert result(_tc("cmd", "execute")) == "once"

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
        assert result(_tc("test", "read")) == "deny"


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
            assert cb(_tc("read_file", "read")) == "once"
            assert cb(_tc("write_file", "write")) == "once"
            assert cb(_tc("mystery", "frobnicate")) == "once"
            assert req_fn.call_count == 3
            # Execute is decided by the command guards instead.
            assert cb(_tc("ls -la", "execute")) == "once"
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

        assert cb(_tc("tool", "read")) == "once"

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

        assert cb(_tc("tool", "read")) == "deny"

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

        assert cb(_tc("tool", "read")) == "deny"

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
        assert result(_tc("tool", "read")) == "deny"
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
        assert result(_tc("any", "read")) == "deny"

    def test_fail_closed_always_deny(self):
        cb = _make_fail_closed_callback("test reason")
        assert cb(_tc("a", "read")) == "deny"
        assert cb(_tc("c", "execute"), allow_permanent=True) == "deny"
        assert cb(_tc("", "")) == "deny"

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
            assert cb(_tc("tool", "read")) == "deny"
            assert cb(_tc("tool", "execute")) == "deny"
            assert cb(_tc("tool", "write")) == "deny"
            assert cb(_tc("tool", "frobnicate")) == "deny"


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
            assert cb(_tc("tool", "read")) == "deny"
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
            result = cb(_tc("rm -rf /", "execute"))

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
            assert cb(_tc("rm -rf /", "execute")) == "once"
        guards.assert_not_called()

    # --- (b) bypass inactive -> inner called normally --- #

    def test_bypass_inactive_calls_inner(self):
        inner = MagicMock(return_value="session")
        cb = _wrap_with_bypass_check(inner)

        with patch(
            "tools.approval.is_approval_bypass_active",
            return_value=False,
        ):
            result = cb(_tc("ls", "read"), allow_permanent=True)

        assert result == "session"
        inner.assert_called_once_with(
            _tc("ls", "read"), allow_permanent=True
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
            assert cb(_tc("rm -rf /", "execute")) == "deny"

    # --- (c) bypass check raises -> inner called (fail-safe) --- #

    def test_bypass_check_raises_falls_through_to_inner(self):
        inner = MagicMock(return_value="always")
        cb = _wrap_with_bypass_check(inner)

        with patch(
            "tools.approval.is_approval_bypass_active",
            side_effect=RuntimeError("boom"),
        ):
            result = cb(_tc("cmd", "execute"))

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
            assert cb(_tc("rm -rf /", "execute")) == "deny"

    # --- dynamic re-evaluation across invocations --- #

    def test_bypass_is_dynamic_across_calls(self):
        """Same callback: deny while bypass off, 'once' once bypass flips on."""
        inner = MagicMock(return_value="deny")
        cb = _wrap_with_bypass_check(inner)

        with patch(
            "tools.approval.is_approval_bypass_active",
            return_value=False,
        ):
            assert cb(_tc("cmd", "execute")) == "deny"
        assert inner.call_count == 1

        with patch(
            "tools.approval.is_approval_bypass_active",
            return_value=True,
        ):
            assert cb(_tc("cmd", "execute")) == "once"
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
            assert cb(_tc("ls -la", "execute")) == "once"

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
            assert cb(_tc("rm -rf /", "execute")) == "deny"

        inner.assert_not_called()

    def test_execute_kind_match_is_case_insensitive(self):
        inner = MagicMock(return_value="deny")
        cb = _wrap_with_execute_command_guards(inner)

        with patch(
            "tools.approval.check_all_command_guards",
            return_value={"approved": True, "message": None},
        ):
            assert cb(_tc("ls", "Execute")) == "once"
            assert cb(_tc("ls", "EXECUTE")) == "once"

        inner.assert_not_called()

    def test_non_execute_kinds_skip_guards_and_call_inner(self):
        inner = MagicMock(return_value="session")
        cb = _wrap_with_execute_command_guards(inner)

        with patch("tools.approval.check_all_command_guards") as guards:
            for kind in ("read", "edit", "write", "delete", "frobnicate", ""):
                assert cb(_tc("cmd", kind)) == "session"

        guards.assert_not_called()
        assert inner.call_count == 6

    def test_execute_empty_command_falls_through_to_inner(self):
        """Execute kind but no usable command text → channel decides.

        The generic fallback always derives a non-empty ``command_label``
        (``title or kind or "tool"``), so the only way an execute-kind
        request has nothing to guard is when the adapter explicitly
        normalized ``kind="execute"`` but supplied no command AND the raw
        dict has no title/kind to fall back to.
        """
        inner = MagicMock(return_value="always")
        cb = _wrap_with_execute_command_guards(inner)

        with patch("tools.approval.check_all_command_guards") as guards:
            # Adapter said "execute" but no command; raw dict has no title/kind
            # → command_label is "tool", command is None → cmd="tool" (truthy).
            # This routes to guards (the generic fallback never yields empty).
            # To truly have nothing to guard, kind must be the empty/unknown
            # path — covered by test_non_execute_kinds_skip_guards_and_call_inner.
            #
            # What we assert here instead: a blank toolCall (no kind at all)
            # never reaches the guards regardless of how it's labelled.
            assert cb({"toolCallId": "tc-blank"}) == "always"

        guards.assert_not_called()
        assert inner.call_count == 1

    def test_execute_guard_exception_falls_through_to_inner(self):
        """Unexpected guard failure → channel decides (fail-safe fall-through)."""
        inner = MagicMock(return_value="deny")
        cb = _wrap_with_execute_command_guards(inner)

        with patch(
            "tools.approval.check_all_command_guards",
            side_effect=RuntimeError("boom"),
        ):
            assert cb(_tc("ls", "execute")) == "deny"

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
            assert cb(_tc("git status", "execute")) == "once"
            # A non-execute kind on the same callback still fails closed.
            assert cb(_tc("read_file", "read")) == "deny"

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
            assert cb(_tc("ls -la", "execute")) == "once"

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
            assert cb(_tc("rm -rf /", "execute")) == "once"

        guards.assert_not_called()


# --------------------------------------------------------------------------- #
# Test 7: _extract_acp_fields — generic fallback + _normalized_* preference
# --------------------------------------------------------------------------- #


class TestExtractAcpFields:
    """``_extract_acp_fields`` is the single place that derives
    ``(command_label, kind, description, command, paths)`` from a toolCall.

    It prefers plugin-normalized ``_normalized_*`` keys when present and falls
    back to generic extraction from raw ``title`` / ``kind`` otherwise.  The
    generic fallback never touches ``rawInput`` (which may carry secrets) —
    coverage for the leak concern that previously lived in the session tests.
    """

    def test_generic_fallback_uses_title_as_label(self):
        tc = {"title": "git push", "kind": "tool", "toolCallId": "tc-1"}
        label, kind, desc, command, paths = _extract_acp_fields(tc)
        assert label == "git push"
        assert kind == "tool"
        assert desc  # non-empty generic description
        assert command is None
        assert paths == ()

    def test_generic_fallback_label_uses_kind_when_no_title(self):
        tc = {"kind": "file_edit", "toolCallId": "tc-2"}
        label, kind, _, _, _ = _extract_acp_fields(tc)
        assert label == "file_edit"
        assert kind == "file_edit"

    def test_generic_fallback_label_defaults_to_tool(self):
        tc = {"toolCallId": "tc-3"}
        label, kind, desc, _, _ = _extract_acp_fields(tc)
        assert label == "tool"
        assert kind == ""
        assert desc == "ACP permission request"

    def test_generic_fallback_never_reads_raw_input(self):
        """Secrets in ``rawInput`` never reach label / kind / description."""
        secret = "AKIA-SECRET"
        tc = {
            "title": "aws s3 cp",
            "kind": "tool",
            "toolCallId": "tc-4",
            "rawInput": f"--secret={secret}",
        }
        label, kind, desc, _, _ = _extract_acp_fields(tc)
        assert secret not in label
        assert secret not in kind
        assert secret not in desc

    def test_prefers_normalized_fields_over_raw(self):
        """When the plugin adapter enriched the dict, normalized fields win."""
        tc = {
            "title": "Bash(ls -la)",      # raw title (DSL form)
            "kind": "tool",               # raw kind (vendor vocabulary)
            "toolCallId": "tc-5",
            "_normalized_kind": "execute",
            "_normalized_command_label": "ls -la",
            "_normalized_description": "Shell command execution via ACP agent",
            "_normalized_command": "ls -la",
            "_normalized_paths": (),
        }
        label, kind, desc, command, paths = _extract_acp_fields(tc)
        assert label == "ls -la"
        assert kind == "execute"
        assert desc == "Shell command execution via ACP agent"
        assert command == "ls -la"
        assert paths == ()

    def test_normalized_paths_returned_as_tuple(self):
        tc = {
            "title": "Edit(/a.py)",
            "kind": "edit",
            "toolCallId": "tc-6",
            "_normalized_kind": "write",
            "_normalized_command_label": "/a.py",
            "_normalized_description": "File modification",
            "_normalized_paths": ("/a.py", "/b.py"),
        }
        _, _, _, _, paths = _extract_acp_fields(tc)
        assert paths == ("/a.py", "/b.py")

    def test_partial_enrichment_falls_back_per_field(self):
        """Adapter set kind but not command_label → label falls back to title."""
        tc = {
            "title": "MyTool",
            "kind": "tool",
            "toolCallId": "tc-7",
            "_normalized_kind": "read",
        }
        label, kind, _, _, _ = _extract_acp_fields(tc)
        assert kind == "read"        # normalized
        assert label == "MyTool"     # generic fallback (title)

