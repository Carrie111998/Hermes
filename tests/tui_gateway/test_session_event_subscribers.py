"""Ordered read-only subscribers for one authoritative TUI gateway runtime."""

from __future__ import annotations

import copy
import gc
import io
import threading
import weakref

import pytest

from tui_gateway import server
from tui_gateway.transport import StdioTransport, TeeTransport, Transport


class RecordingTransport:
    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.closed = False

    def write(self, obj: dict) -> bool:
        if self.closed:
            return False
        self.frames.append(copy.deepcopy(obj))
        return True

    def close(self) -> None:
        self.closed = True

    @property
    def events(self) -> list[dict]:
        return [frame for frame in self.frames if frame.get("method") == "event"]


@pytest.fixture(autouse=True)
def clean_sessions():
    server._sessions.clear()
    yield
    server._sessions.clear()


def _session(primary: Transport, *, key: str = "stored-1") -> dict:
    record = {
        "agent": None,
        "created_at": 1.0,
        "history": [],
        "history_lock": threading.Lock(),
        "last_active": 1.0,
        "running": False,
        "session_key": key,
        "transport": primary,
    }
    server._sessions["runtime-1"] = record
    return record


def _subscribe(transport: RecordingTransport, params: dict | None = None) -> dict:
    response = server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": "subscribe",
            "method": "session.subscribe",
            "params": {"session_id": "stored-1", **(params or {})},
        },
        transport=transport,
    )
    assert response is not None and "result" in response, response
    return response["result"]


def _event_ids(transport: RecordingTransport) -> list[int]:
    return [frame["params"]["event_id"] for frame in transport.events]


def test_desktop_and_spectator_receive_identical_ordered_turn_events():
    desktop = RecordingTransport()
    spectator = RecordingTransport()
    second_spectator = RecordingTransport()
    session = _session(desktop)

    subscribed = _subscribe(spectator)
    _subscribe(second_spectator)
    assert session["transport"] is desktop
    assert subscribed["replayed"] == 0

    expected_types = [
        "message.start",
        "message.delta",
        "tool.start",
        "tool.complete",
        "message.delta",
        "message.complete",
    ]
    for event_type in expected_types:
        server._emit(event_type, "runtime-1", {"type_seen": event_type})

    assert [frame["params"]["type"] for frame in desktop.events] == expected_types
    assert spectator.events == desktop.events
    assert second_spectator.events == desktop.events
    assert _event_ids(desktop) == list(range(1, len(expected_types) + 1))
    generations = {frame["params"]["runtime_generation"] for frame in desktop.events}
    assert generations == {subscribed["runtime_generation"]}
    assert session["transport"] is desktop


def test_reconnect_cursor_replays_only_missed_events_then_continues_live():
    desktop = RecordingTransport()
    first_spectator = RecordingTransport()
    session = _session(desktop)
    initial = _subscribe(first_spectator)

    server._emit("message.delta", "runtime-1", {"text": "one"})
    server._emit("tool.start", "runtime-1", {"name": "terminal"})
    assert server._unsubscribe_session_events(session, first_spectator) is True
    server._emit("tool.complete", "runtime-1", {"name": "terminal"})
    server._emit("message.delta", "runtime-1", {"text": "two"})

    reconnected = RecordingTransport()
    resumed = _subscribe(
        reconnected,
        {
            "cursor": {
                "runtime_generation": initial["runtime_generation"],
                "event_id": 2,
            }
        },
    )
    server._emit("message.complete", "runtime-1", {"text": "onetwo"})

    assert resumed["replay_gap"] is False
    assert resumed["replayed"] == 2
    assert _event_ids(reconnected) == [3, 4, 5]
    assert len(set(_event_ids(reconnected))) == 3
    assert reconnected.events == desktop.events[2:]


def test_bounded_replay_reports_old_cursor_and_generation_gaps(monkeypatch):
    monkeypatch.setattr(server, "_SESSION_EVENT_REPLAY_LIMIT", 3)
    desktop = RecordingTransport()
    _session(desktop)
    for index in range(1, 6):
        server._emit("message.delta", "runtime-1", {"text": str(index)})

    generation = desktop.events[-1]["params"]["runtime_generation"]
    stale = RecordingTransport()
    stale_result = _subscribe(
        stale,
        {"cursor": {"runtime_generation": generation, "event_id": 1}},
    )
    assert stale_result == {
        "session_id": "runtime-1",
        "session_key": "stored-1",
        "runtime_generation": generation,
        "oldest_event_id": 3,
        "latest_event_id": 5,
        "replayed": 0,
        "replay_gap": True,
        "gap_reason": "replay_window_exceeded",
        "subscribed": False,
        "already_subscribed": False,
        "cursor": {"runtime_generation": generation, "event_id": 5},
    }
    assert _event_ids(stale) == []

    old_runtime = RecordingTransport()
    generation_result = _subscribe(
        old_runtime,
        {"cursor": {"runtime_generation": "previous-runtime", "event_id": 5}},
    )
    assert generation_result["replay_gap"] is True
    assert generation_result["gap_reason"] == "runtime_generation_changed"
    assert generation_result["subscribed"] is False
    assert _event_ids(old_runtime) == []

    unscoped_cursor = RecordingTransport()
    unscoped_result = _subscribe(unscoped_cursor, {"after_event_id": 5})
    assert unscoped_result["replay_gap"] is True
    assert unscoped_result["gap_reason"] == "runtime_generation_required"
    assert unscoped_result["subscribed"] is False
    assert _event_ids(unscoped_cursor) == []

    server._emit("message.complete", "runtime-1", {"text": "six"})
    assert stale.events == old_runtime.events == unscoped_cursor.events == []


def test_repeated_subscribe_on_same_transport_is_idempotent():
    desktop = RecordingTransport()
    spectator = RecordingTransport()
    _session(desktop)
    first = _subscribe(spectator)
    server._emit("message.delta", "runtime-1", {"text": "one"})

    repeated = _subscribe(
        spectator,
        {
            "cursor": {
                "runtime_generation": first["runtime_generation"],
                "event_id": 0,
            }
        },
    )
    server._emit("message.complete", "runtime-1", {"text": "done"})

    assert repeated["already_subscribed"] is True
    assert repeated["replayed"] == 0
    assert _event_ids(spectator) == [1, 2]


@pytest.mark.parametrize("invalid", ["1", 1.5, float("nan"), float("inf"), True, -1])
def test_cursor_rejects_non_integral_or_negative_values(invalid):
    desktop = RecordingTransport()
    spectator = RecordingTransport()
    _session(desktop)

    response = server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": "bad-cursor",
            "method": "session.subscribe",
            "params": {"session_id": "stored-1", "after_event_id": invalid},
        },
        transport=spectator,
    )

    assert response is not None and response["error"]["code"] == 4006
    assert spectator.events == []


def test_cursor_accepts_zero_and_integral_json_number():
    desktop = RecordingTransport()
    _session(desktop)
    generation = _subscribe(RecordingTransport())["runtime_generation"]

    for event_id in (0, 0.0):
        spectator = RecordingTransport()
        result = _subscribe(
            spectator,
            {"cursor": {"runtime_generation": generation, "event_id": event_id}},
        )
        assert result["subscribed"] is True


def test_delivery_locks_do_not_retain_unsubscribed_transport():
    desktop = RecordingTransport()
    spectator = RecordingTransport()
    session = _session(desktop)
    _subscribe(spectator)
    stream = session["_event_stream"]
    reference = weakref.ref(spectator)

    assert server._unsubscribe_session_events(session, spectator) is True
    del spectator
    gc.collect()

    assert reference() is None
    assert len(stream.delivery_locks) == 0


def test_delivery_locks_do_not_retain_rebound_primary(monkeypatch):
    original = RecordingTransport()
    resumed = RecordingTransport()
    session = _session(original)
    server._emit("message.delta", "runtime-1", {"text": "one"})
    stream = session["_event_stream"]
    reference = weakref.ref(original)
    monkeypatch.setattr(server, "_get_db", lambda: None)
    monkeypatch.setattr(server, "_fallback_session_info", lambda _session: {})

    server._live_session_payload(
        "runtime-1", session, touch=True, transport=resumed, omit_messages=True
    )
    del original
    gc.collect()

    assert reference() is None
    assert list(stream.delivery_locks) == []


def test_real_pty_tee_transport_supports_ordered_session_delivery():
    output = io.StringIO()
    stdio = StdioTransport(lambda: output, threading.Lock())
    sidecar = RecordingTransport()
    primary = TeeTransport(stdio, sidecar)
    _session(primary)

    assert weakref.ref(primary)() is primary
    server._emit("message.delta", "runtime-1", {"text": "one"})

    assert '"event_id": 1' in output.getvalue()
    assert _event_ids(sidecar) == [1]


def test_concurrent_emitters_still_deliver_monotonic_ids_to_every_observer():
    desktop = RecordingTransport()
    spectator = RecordingTransport()
    _session(desktop)
    _subscribe(spectator)
    barrier = threading.Barrier(9)

    def emit(index: int) -> None:
        barrier.wait(timeout=5)
        server._emit("message.delta", "runtime-1", {"text": str(index)})

    threads = [threading.Thread(target=emit, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert _event_ids(desktop) == list(range(1, 9))
    assert spectator.events == desktop.events


def test_unsubscribe_rpc_and_disconnect_cleanup_stop_delivery_without_closing_runtime():
    desktop = RecordingTransport()
    spectator = RecordingTransport()
    session = _session(desktop)
    _subscribe(spectator)

    response = server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": "unsubscribe",
            "method": "session.unsubscribe",
            "params": {"session_id": "stored-1"},
        },
        transport=spectator,
    )
    assert response == {
        "jsonrpc": "2.0",
        "id": "unsubscribe",
        "result": {"session_id": "runtime-1", "unsubscribed": True},
    }
    server._emit("message.complete", "runtime-1", {"text": "done"})
    assert spectator.events == []
    assert _event_ids(desktop) == [1]
    assert session["transport"] is desktop

    _subscribe(spectator)
    assert server._unsubscribe_transport_from_all_sessions(spectator) == 1
    assert server._unsubscribe_transport_from_all_sessions(spectator) == 0
    server._emit("session.info", "runtime-1", {})
    assert spectator.events == []
    assert _event_ids(desktop) == [1, 2]
    assert desktop.closed is False


def test_existing_interactive_transport_rebind_remains_backward_compatible(monkeypatch):
    original_desktop = RecordingTransport()
    resumed_desktop = RecordingTransport()
    spectator = RecordingTransport()
    session = _session(original_desktop)
    _subscribe(spectator)
    monkeypatch.setattr(server, "_get_db", lambda: None)
    monkeypatch.setattr(server, "_fallback_session_info", lambda _session: {})

    payload = server._live_session_payload(
        "runtime-1",
        session,
        touch=True,
        transport=resumed_desktop,
        omit_messages=True,
    )
    server._emit("message.complete", "runtime-1", {"text": "done"})

    assert payload["session_id"] == "runtime-1"
    assert session["transport"] is resumed_desktop
    assert original_desktop.events == []
    assert _event_ids(resumed_desktop) == [1]
    assert spectator.events == resumed_desktop.events
