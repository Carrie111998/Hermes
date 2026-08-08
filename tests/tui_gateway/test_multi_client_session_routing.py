"""Per-session stream routing fans out to every registered transport.

Regression coverage for #81286 — two frontends (Web Dashboard + Desktop)
attached to the same backend session must both receive live stream events,
not just whichever one most recently submitted. The previous implementation
overwrote ``session["transport"]`` on every ``prompt.submit``, silently
freezing the other frontend until it submitted again.
"""

from __future__ import annotations

import json
from typing import List


class _RecordingTransport:
    """Minimal Transport stand-in that records the frames written to it."""

    def __init__(self) -> None:
        self.frames: List[dict] = []

    def write(self, obj: dict) -> bool:
        self.frames.append(obj)
        return True

    def close(self) -> None:
        pass


def test_session_event_fans_out_to_multiple_frontends(monkeypatch):
    """write_json routes a session-scoped event to every transport registered
    on that session's fan-out set, not just the most recent submitter."""
    from tui_gateway import server

    sid = "session-shared-by-web-and-desktop"
    a = _RecordingTransport()
    b = _RecordingTransport()
    server._sessions[sid] = {
        "transport": a,
        "transports": {a, b},
    }
    try:
        frame = server._event_frame("message.delta", sid, {"text": "hello"})
        delivered = server.write_json(frame)
        assert delivered is True
        assert a.frames == [frame]
        assert b.frames == [frame]
    finally:
        server._sessions.pop(sid, None)


def test_session_event_falls_back_to_legacy_transport(monkeypatch):
    """If a session has not been migrated to the ``transports`` set yet and
    only carries the legacy single-transport field, write_json must still
    deliver to that transport so existing readers keep working."""
    from tui_gateway import server

    sid = "legacy-session-no-set"
    a = _RecordingTransport()
    server._sessions[sid] = {"transport": a}
    try:
        frame = server._event_frame("message.delta", sid, {"text": "hi"})
        assert server.write_json(frame) is True
        assert a.frames == [frame]
    finally:
        server._sessions.pop(sid, None)


def test_unregister_removes_transport_from_session_fanout(monkeypatch):
    """ws.disconnect calls unregister_live_transport; the helper must drop
    the closing transport from every per-session fan-out set so a dead
    socket is never written to via the fan-out path and the other peer
    keeps receiving."""
    from tui_gateway import server

    sid = "session-with-disconnecting-peer"
    b = _RecordingTransport()
    server._sessions[sid] = {
        # ``transport`` is intentionally NOT set here — the legacy fallback
        # is exercised by a separate test. We want this test to isolate the
        # fan-out set behaviour.
        "transports": set(),
    }
    a = _RecordingTransport()
    server._sessions[sid]["transports"] = {a, b}
    try:
        # Peer A disconnects.
        server.unregister_live_transport(a)

        assert server._sessions[sid]["transports"] == {b}
        assert a not in server._live_transports

        frame = server._event_frame("message.complete", sid, {"text": "done"})
        server.write_json(frame)
        assert a.frames == []
        assert b.frames == [frame]
    finally:
        server._sessions.pop(sid, None)


def test_unregister_is_safe_with_legacy_sessions(monkeypatch):
    """unregister_live_transport must not blow up on sessions that never
    had a transports set (only the legacy single-transport field)."""
    from tui_gateway import server

    sid = "legacy-only"
    a = _RecordingTransport()
    server._sessions[sid] = {"transport": a}
    try:
        # Should be a silent no-op for the session dict.
        server.unregister_live_transport(a)
        assert server._sessions[sid] == {"transport": a}
    finally:
        server._sessions.pop(sid, None)


def test_session_event_with_no_transport_falls_through(monkeypatch):
    """A session that has been reaped (no transport at all) must not crash
    write_json; it falls through to the contextvar/stdio path."""
    from tui_gateway import server

    sid = "session-with-no-transport"
    server._sessions[sid] = {}
    try:
        # No transports — write_json should return False (no delivery) but
        # not raise.
        frame = server._event_frame("message.complete", sid, {"text": "x"})
        # The stdio transport is the module-level fallback; we only assert
        # the function returns a bool without touching a missing transport.
        result = server.write_json(frame)
        assert isinstance(result, bool)
    finally:
        server._sessions.pop(sid, None)