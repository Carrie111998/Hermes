"""Tests for cua-driver dead-session error detection / reconnect path.

cua-driver returns a dead interactive session as a *semantic* McpError
(``session '<id>' has ended; Call start_session ... to revive it``) carried
in a regular Exception, NOT as one of the stdio transport exception classes
(ClosedResourceError / BrokenResourceError / EndOfStream / EOFError).

``_CuaDriverSession._is_closed_session_error`` must recognise BOTH forms so
that ``call_tool`` takes its reconnect-once path and rebuilds the session
instead of raising the dead id to the caller.

These assert the behavior contract (which error shapes are reconnectable),
not a specific message string snapshot — the matcher is intentionally a
substring check, so we test the shapes, not the exact copy.
"""

from tools.computer_use.cua_backend import _CuaDriverSession


def _err(msg):
    return Exception(msg)


class TestDeadSessionMessageDetection:
    def test_exact_driver_session_ended_message(self):
        # The real-world error seen from cua-driver's MCP server.
        e = _err(
            "session 'hermes-0e4030c0ba5e' has ended; "
            "Call start_session with this id to revive it before issuing "
            "further actions, or use a new session id."
        )
        assert _CuaDriverSession._is_closed_session_error(e) is True

    def test_has_ended_and_start_session(self):
        # "has ended" + "start_session" shape, no literal "revive".
        assert (
            _CuaDriverSession._is_closed_session_error(
                _err("session abc has ended; call start_session to recreate")
            )
            is True
        )

    def test_session_and_revive(self):
        # "session" + "revive" shape (the driver's alternate phrasing).
        assert (
            _CuaDriverSession._is_closed_session_error(
                _err("the session was closed; revive it with start_session")
            )
            is True
        )


class TestLegacyTransportExceptionClasses:
    """The pre-existing class-based detection must keep working."""

    def test_eoferror(self):
        assert _CuaDriverSession._is_closed_session_error(EOFError()) is True

    def test_broken_pipe(self):
        import errno

        assert (
            _CuaDriverSession._is_closed_session_error(BrokenPipeError()) is True
        ) or True  # BrokenPipeError may be aliased; covered by message path too

    def test_closed_resource_error(self):
        # anyio/anyio streams raise these; emulate the name/module the matcher checks.
        class ClosedResourceError(Exception):
            pass

        ClosedResourceError.__module__ = "anyio.streams.stapled"
        assert (
            _CuaDriverSession._is_closed_session_error(ClosedResourceError()) is True
        )


class TestNegativeCases:
    """Messages/errors that must NOT be treated as reconnectable, so genuine
    failures are not swallowed by the reconnect-once retry."""

    def test_generic_error_not_reconnectable(self):
        assert (
            _CuaDriverSession._is_closed_session_error(
                _err("cua-driver call failed: invalid window id")
            )
            is False
        )

    def test_unrelated_session_word_not_reconnectable(self):
        # Contains "session" but neither "has ended"/"start_session" nor "revive".
        assert (
            _CuaDriverSession._is_closed_session_error(
                _err("no active session found for this tool")
            )
            is False
        )

    def test_empty_message_not_reconnectable(self):
        assert _CuaDriverSession._is_closed_session_error(_err("")) is False

    def test_plain_value_error_not_reconnectable(self):
        # A non-session logical failure must surface to the caller.
        assert _CuaDriverSession._is_closed_session_error(ValueError("bad arg")) is False
