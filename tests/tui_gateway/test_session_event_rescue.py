"""Session-event rescue in ``write_json``: a dead bound transport must not
silently swallow a session's events (#98503).

``clarify.request`` (and the other blocking bridges) is emitted from the agent
worker thread via ``_emit`` → ``write_json``, which routed strictly through
``_sessions[sid]["transport"]``. When that transport is the post-disconnect
drop sentinel — or a socket that went ``_closed`` before a rebind — the frame
was lost with no error, and the clarify tool blocked until timeout with no
client ever seeing the card. These tests pin the viewer-fallback rescue.
"""

import importlib
import json
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

_original_stdout = sys.stdout


@pytest.fixture(autouse=True)
def _restore_stdout():
    yield
    sys.stdout = _original_stdout


class _RecordingTransport:
    """Minimal Transport stand-in that records frames written to it."""

    def __init__(self) -> None:
        self.frames: list[dict] = []
        self._closed = False

    def write(self, obj: dict) -> bool:
        if self._closed:
            return False
        self.frames.append(obj)
        return True

    def close(self) -> None:
        self._closed = True


@pytest.fixture()
def server():
    with patch.dict(
        "sys.modules",
        {
            "hermes_constants": MagicMock(
                get_hermes_home=MagicMock(return_value="/tmp/hermes_test")
            ),
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
            "hermes_state": MagicMock(),
        },
    ):
        mod = importlib.import_module("tui_gateway.server")
    yield mod
    for sid in list(mod._sessions):
        mod._sessions.pop(sid, None)
    mod._live_transports.clear()


def _event_frame(event: str, sid: str, payload: dict | None = None) -> dict:
    params: dict = {"type": event, "session_id": sid}
    if payload is not None:
        params["payload"] = payload
    return {"jsonrpc": "2.0", "method": "event", "params": params}


def _install_session(server, sid: str, transport, viewers: dict | None = None):
    server._sessions[sid] = {
        "session_key": sid,
        "history": [],
        "history_lock": threading.Lock(),
        "transport": transport,
        "viewers": dict(viewers or {}),
    }


def test_dead_bound_transport_falls_back_to_newest_live_viewer(server):
    """The disconnect path parks the session on the drop sentinel; a live
    pop-out viewer of the same session must still receive the frame."""
    sid = "rescue-live-viewer"
    dead = _RecordingTransport()
    dead._closed = True
    older_viewer = _RecordingTransport()
    newest_viewer = _RecordingTransport()
    _install_session(
        server,
        sid,
        server._detached_ws_transport,
        viewers={dead: 1.0, older_viewer: 2.0, newest_viewer: 3.0},
    )

    frame = _event_frame("clarify.request", sid, {"question": "ship it?"})
    assert server.write_json(frame) is True

    assert newest_viewer.frames == [frame]
    assert older_viewer.frames == []
    assert dead.frames == []
    # The rescued frame keeps the single seq stamp from the original write.
    assert newest_viewer.frames[0]["params"]["seq"] == frame["params"]["seq"]


def test_drop_of_blocking_request_warns_when_no_viewer_takes_it(server, caplog):
    """With no live viewer the frame is still lost — but a blocking
    ``*.request`` drop must be visible in the logs instead of silent."""
    sid = "rescue-no-viewer"
    closed = _RecordingTransport()
    closed._closed = True
    _install_session(server, sid, closed, viewers={closed: 1.0})

    with caplog.at_level("WARNING", logger="tui_gateway.server"):
        ok = server.write_json(_event_frame("clarify.request", sid))

    assert ok is False
    assert any(
        "clarify.request" in rec.message and "dropped" in rec.message
        for rec in caplog.records
    )


def test_non_request_drop_stays_quiet(server, caplog):
    """High-frequency progress frames must not spam warnings when a detached
    session has no viewer; the replay ring still carries them for a
    reconnecting client."""
    sid = "rescue-quiet"
    _install_session(server, sid, server._detached_ws_transport)

    with caplog.at_level("WARNING", logger="tui_gateway.server"):
        ok = server.write_json(_event_frame("tool.progress", sid))

    assert ok is False
    assert not [rec for rec in caplog.records if rec.levelname == "WARNING"]


def test_live_bound_transport_skips_viewer_fallback(server):
    """A healthy bound transport stays the single delivery path — viewers of
    the same session must not receive a duplicate frame."""
    sid = "rescue-healthy"
    bound = _RecordingTransport()
    viewer = _RecordingTransport()
    _install_session(server, sid, bound, viewers={viewer: 1.0})

    frame = _event_frame("tool.complete", sid)
    assert server.write_json(frame) is True

    assert bound.frames == [frame]
    assert viewer.frames == []
