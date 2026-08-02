"""Tests for GHSA-96vc-wcxf-jjff and GHSA-qg5c-hvr5-hjgr.

Two related ACP approval-flow issues:
- 96vc: ACP didn't set HERMES_EXEC_ASK, so `check_all_command_guards`
  took the non-interactive auto-approve path and never consulted the
  ACP-supplied callback.
- qg5c: `_approval_callback` was a module-global in terminal_tool;
  overlapping ACP sessions overwrote each other's callback slot.

Both fixed together by:
1. Setting HERMES_EXEC_ASK inside _run_agent (wraps the agent call).
2. Storing the callback in thread-local state so concurrent executor
   threads don't collide.
"""

import threading

import pytest


@pytest.fixture(autouse=True)
def _isolate_approval_state(monkeypatch):
    """Keep these security regression tests hermetic.

    These tests assert the manual interactive-callback path, independently of
    the developer's profile configuration.
    """
    import tools.approval as _approval

    monkeypatch.setattr(_approval, "_get_approval_mode", lambda: "manual")


class TestThreadLocalApprovalCallback:
    """GHSA-qg5c-hvr5-hjgr: set_approval_callback must be per-thread so
    concurrent ACP sessions don't stomp on each other's handlers."""

    def test_set_and_get_in_same_thread(self):
        from tools.terminal_tool import (
            set_approval_callback,
            _get_approval_callback,
        )

        cb1 = lambda cmd, desc: "once"  # noqa: E731
        set_approval_callback(cb1)
        assert _get_approval_callback() is cb1

    def test_callback_not_visible_in_different_thread(self):
        """Thread A's callback is NOT visible to Thread B."""
        from tools.terminal_tool import (
            set_approval_callback,
            _get_approval_callback,
        )

        cb_a = lambda cmd, desc: "thread_a"  # noqa: E731
        cb_b = lambda cmd, desc: "thread_b"  # noqa: E731

        seen_in_a = []
        seen_in_b = []

        def thread_a():
            set_approval_callback(cb_a)
            # Pause so thread B has time to set its own callback
            import time
            time.sleep(0.05)
            seen_in_a.append(_get_approval_callback())

        def thread_b():
            set_approval_callback(cb_b)
            import time
            time.sleep(0.05)
            seen_in_b.append(_get_approval_callback())

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start()
        tb.start()
        ta.join()
        tb.join()

        # Each thread must see ONLY its own callback — not the other's
        assert seen_in_a == [cb_a]
        assert seen_in_b == [cb_b]

    def test_main_thread_callback_not_leaked_to_worker(self):
        """A callback set in the main thread does NOT leak into a
        freshly-spawned worker thread."""
        from tools.terminal_tool import (
            set_approval_callback,
            _get_approval_callback,
        )

        cb_main = lambda cmd, desc: "main"  # noqa: E731
        set_approval_callback(cb_main)

        worker_saw = []

        def worker():
            worker_saw.append(_get_approval_callback())

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        # Worker thread has no callback set — TLS is empty for it
        assert worker_saw == [None]
        # Main thread still has its callback
        assert _get_approval_callback() is cb_main


    def test_sudo_password_cache_does_not_leak_across_threads(self):
        """Interactive sudo cache must not bleed into another executor thread."""
        from tools.terminal_tool import (
            _get_cached_sudo_password,
            _reset_cached_sudo_passwords,
            _set_cached_sudo_password,
        )

        _reset_cached_sudo_passwords()
        _set_cached_sudo_password("main-thread-password")

        worker_saw = []

        def worker():
            worker_saw.append(_get_cached_sudo_password())

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        assert worker_saw == [""]
        assert _get_cached_sudo_password() == "main-thread-password"



class TestAcpExecAskGate:
    """ACP's explicit callback is an exact owner-approval surface."""

    def test_interactive_env_var_routes_to_callback(self, monkeypatch):
        """An explicit callback is consulted without inspecting command prose."""
        # Clean env
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)

        from tools.approval import check_all_command_guards

        called_with = []

        def fake_cb(command, description, **kwargs):
            called_with.append((command, description))
            return "once"

        # An explicit callback is already a human surface; it must be used even
        # when the ambient CLI marker is absent.
        result = check_all_command_guards(
            "opaque exact bytes", "local", approval_callback=fake_cb,
        )
        assert result["approved"] is True
        assert len(called_with) == 1
        assert called_with[0][0] == "opaque exact bytes"

        # The interactive marker does not change the exact authority topology.
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        called_with.clear()
        result = check_all_command_guards(
            "opaque exact bytes", "local", approval_callback=fake_cb,
        )
        assert called_with
        assert result["approved"] is True
