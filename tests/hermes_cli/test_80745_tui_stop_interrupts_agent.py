"""Regression test for #80745 — TUI ``/stop`` must interrupt the active agent turn.

The slash command handler ``CLICommandsMixin._handle_stop_command`` only
cleaned up background processes and async delegations. When the user typed
``/stop`` in the TUI while the agent was mid-turn, the agent kept firing
tool calls and the UI stayed in the busy/running state. Only Ctrl+C
actually halted the turn.

The fix routes the same ``self.agent.interrupt()`` seam Ctrl+C uses so the
in-flight tool loop unwinds and the TUI can return to the ready state.
"""

import io
from unittest.mock import MagicMock

from hermes_cli.cli_commands_mixin import CLICommandsMixin


class _Stub(CLICommandsMixin):
    """Bare mixin holder — ``_handle_stop_command`` only touches ``self.agent``."""

    def __init__(self, agent):
        self.agent = agent


class _Agent:
    def __init__(self):
        self.interrupt_calls = []

    def interrupt(self, message=None, *, hard_cancel=False):
        self.interrupt_calls.append((message, hard_cancel))


def _run(stub):
    buf = io.StringIO()
    import contextlib

    with contextlib.redirect_stdout(buf):
        stub._handle_stop_command()
    return buf.getvalue()


class TestStopInterruptsAgent:
    """``/stop`` must call ``self.agent.interrupt()`` so the in-flight turn
    unwinds. Background-process cleanup happens after the interrupt so the
    foreground turn aborts first (#80745)."""

    def test_stop_calls_agent_interrupt(self):
        agent = _Agent()
        stub = _Stub(agent)

        _run(stub)

        assert len(agent.interrupt_calls) == 1, (
            "/stop must interrupt the in-flight agent turn"
        )
        # No message payload — /stop is an unconditional stop, not a redirect.
        assert agent.interrupt_calls[0][0] is None
        # /stop is the canonical "hard cancel" — same flag Ctrl+C uses to
        # make compression honor the stop even with ordinary interrupts masked.
        assert agent.interrupt_calls[0][1] is True

    def test_stop_does_not_raise_when_no_agent(self):
        """A bare session without an agent must not crash; the handler is
        called from the interactive loop before the first turn sometimes."""
        stub = _Stub(agent=None)

        _run(stub)  # must not raise

    def test_stop_still_reports_background_process_cleanup(self):
        """The original /stop semantics — kill background processes — must be
        preserved so users keep their existing escape hatch."""
        from tools import process_registry as pr_mod

        # Force the registry to report one running background process so the
        # handler hits its post-interrupt cleanup branch.
        sentinel_proc = {"status": "running", "session_id": "bg-1"}
        original_list_sessions = pr_mod.process_registry.list_sessions
        original_kill_all = pr_mod.process_registry.kill_all

        pr_mod.process_registry.list_sessions = lambda: [sentinel_proc]
        pr_mod.process_registry.kill_all = lambda: 1

        try:
            agent = _Agent()
            stub = _Stub(agent)

            out = _run(stub)

            assert len(agent.interrupt_calls) == 1
            assert "Stopped" in out or "process" in out
        finally:
            pr_mod.process_registry.list_sessions = original_list_sessions
            pr_mod.process_registry.kill_all = original_kill_all

    def test_stop_calls_interrupt_before_killing_background(self):
        """The agent interrupt must fire *before* the background-process
        kill so a busy tool loop has a chance to observe the stop signal
        and unwind cleanly (#80745).
        """
        from tools import process_registry as pr_mod

        order: list[str] = []

        class _OrderingAgent:
            def interrupt(self, message=None, *, hard_cancel=False):
                order.append("interrupt")

        agent = _OrderingAgent()

        def _kill_all():
            order.append("kill_background")
            return 1

        original_list_sessions = pr_mod.process_registry.list_sessions
        original_kill_all = pr_mod.process_registry.kill_all

        pr_mod.process_registry.list_sessions = lambda: [
            {"status": "running", "session_id": "bg-1"}
        ]
        pr_mod.process_registry.kill_all = _kill_all

        try:
            stub = _Stub(agent)
            _run(stub)

            assert order == ["interrupt", "kill_background"], order
        finally:
            pr_mod.process_registry.list_sessions = original_list_sessions
            pr_mod.process_registry.kill_all = original_kill_all