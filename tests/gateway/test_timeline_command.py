"""Tests for the /timeline slash command (gateway/slash_commands.py,
hermes_cli/commands.py's CommandDef, and gateway/run.py's dispatch wiring).

Harness mirrors tests/gateway/test_unknown_command.py's _make_runner(): a
GatewayRunner built via object.__new__ with the minimal attribute surface
_handle_message/_handle_timeline_command actually touch, so the tests
exercise the real handler code, not a stand-in.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key
from tools import session_timeline as st


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM, user_id="u1", chat_id="c1",
        user_name="tester", chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_runner(session_id: str = "sess-timeline-test"):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.hooks = None

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id=session_id,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.has_any_sessions.return_value = True
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._is_user_authorized = lambda _source: True
    return runner


class TestTimelineCommandRegistration:
    def test_timeline_is_a_known_command(self):
        from hermes_cli.commands import resolve_command

        cmd = resolve_command("timeline")
        assert cmd is not None
        assert cmd.name == "timeline"

    def test_timeline_command_not_flagged_as_unknown(self):
        """A real built-in like /timeline must not hit the unknown-command guard."""
        import asyncio

        runner = _make_runner()
        runner._running_agents[build_session_key(_make_source())] = MagicMock()

        result = asyncio.run(runner._handle_message(_make_event("/timeline")))

        assert result is not None
        assert "Unknown command" not in result

    def test_status_command_registry_untouched(self):
        """The pre-existing /status entry must be exactly what it was —
        USER DECIDED not to touch /status while adding /timeline."""
        from hermes_cli.commands import resolve_command

        cmd = resolve_command("status")
        assert cmd is not None
        assert cmd.description == "Show session, model, token, and context info"
        assert cmd.busy_policy == "dispatch"


class TestTimelineCommandHandler:
    @pytest.mark.asyncio
    async def test_empty_timeline_message(self):
        runner = _make_runner("sess-timeline-empty")
        result = await runner._handle_timeline_command(_make_event("/timeline"))
        assert "No tool-call timeline recorded" in result

    @pytest.mark.asyncio
    async def test_renders_recorded_steps_including_running_one(self):
        sid = "sess-timeline-render"
        runner = _make_runner(sid)

        st.record_start(sid, "call-0", "read_file", {"path": "foo.py"})
        st.record_end(sid, "call-0", status="succeeded", duration=0.3)
        st.record_start(sid, "call-1", "terminal", {"command": "still going"})

        result = await runner._handle_timeline_command(_make_event("/timeline"))

        assert "2 step(s), in progress" in result
        assert "read_file" in result
        assert "foo.py" in result
        assert "terminal" in result
        assert "0.30s" in result

    @pytest.mark.asyncio
    async def test_renders_failed_and_blocked_status_icons(self):
        sid = "sess-timeline-icons"
        runner = _make_runner(sid)

        st.record_start(sid, "call-fail", "terminal", {"command": "false"})
        st.record_end(sid, "call-fail", status="failed", duration=0.1)
        st.record_start(sid, "call-blk", "write_file", {"path": "x"})
        st.record_end(sid, "call-blk", status="blocked", duration=0.0)

        result = await runner._handle_timeline_command(_make_event("/timeline"))

        assert "✗" in result  # failed
        assert "⛔" in result  # blocked
        assert "idle" in result  # nothing left running

    @pytest.mark.asyncio
    async def test_does_not_leak_secret_in_reply(self):
        sid = "sess-timeline-secret"
        runner = _make_runner(sid)
        bearer = "sk-ant-api03-" + "Z" * 24
        st.record_start(sid, "call-1", "terminal", {"command": f'curl -H "Authorization: Bearer {bearer}"'})
        st.record_end(sid, "call-1", status="succeeded", duration=0.1)

        result = await runner._handle_timeline_command(_make_event("/timeline"))

        assert bearer not in result
        assert "curl" in result
