"""Live multi-surface session subscribers.

A live ``session.resume`` or ``prompt.submit`` from a second WebSocket must
not replace the session's controlling transport. Session-scoped events fan
out to the controller plus identity-deduplicated viewers; disconnect (and
``detach_sessions_for_transport``) remove only the departing subscriber.
Session-less global events stay on ``_live_transports``.
"""

from __future__ import annotations

import threading
import types

import pytest

from tui_gateway import server
from tui_gateway.transport import bind_transport, reset_transport


class _RecordingTransport:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    def write(self, obj: dict) -> bool:
        self.frames.append(obj)
        return True

    def close(self) -> None:
        return None


def _session(**extra):
    return {
        "agent": types.SimpleNamespace(),
        "session_key": extra.pop("session_key", "stored-live"),
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "cols": 80,
        "created_at": 1.0,
        **extra,
    }


def _resume(session_id: str, transport, **params) -> dict:
    token = bind_transport(transport)
    try:
        return server.handle_request(
            {
                "id": "resume-1",
                "method": "session.resume",
                "params": {"session_id": session_id, "omit_messages": True, **params},
            }
        )
    finally:
        reset_transport(token)


def _submit(session_id: str, transport, text: str = "hello", **params) -> dict:
    token = bind_transport(transport)
    try:
        return server.handle_request(
            {
                "id": "submit-1",
                "method": "prompt.submit",
                "params": {"session_id": session_id, "text": text, **params},
            }
        )
    finally:
        reset_transport(token)


def _stub_idle_prompt_submit(monkeypatch) -> None:
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda _s: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda _s: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *_a, **_k: None)


def _join_run_thread(session) -> None:
    thread = session.get("_run_thread")
    if thread is not None:
        thread.join(timeout=2)


@pytest.fixture(autouse=True)
def _isolate_live_state():
    saved_sessions = dict(server._sessions)
    saved_transports = set(server._live_transports)
    server._sessions.clear()
    server._live_transports.clear()
    try:
        yield
    finally:
        server._sessions.clear()
        server._sessions.update(saved_sessions)
        server._live_transports.clear()
        server._live_transports.update(saved_transports)


def test_live_resume_from_second_transport_does_not_steal_controller():
    """Cyllene (B) resuming Desktop's live session must not take the writer seat."""
    controller = _RecordingTransport()
    observer = _RecordingTransport()
    session = _session(transport=controller)
    server._sessions["live-sid"] = session

    server._live_session_payload(
        "live-sid", session, transport=observer, omit_messages=True
    )

    assert session["transport"] is controller
    assert observer in session["viewers"]
    # The original controller is not auto-inserted into viewers; event
    # fan-out still includes session["transport"] by identity.


def test_session_resume_rpc_attaches_observer_without_replacing_controller(monkeypatch):
    controller = _RecordingTransport()
    observer = _RecordingTransport()
    session = _session(transport=controller, session_key="stored-rpc")
    server._sessions["rpc-sid"] = session

    monkeypatch.setattr(
        server,
        "_get_db",
        lambda: types.SimpleNamespace(
            get_session=lambda target: {"id": target, "cwd": "/tmp", "message_count": 0},
            resolve_resume_session_id=lambda target: target,
        ),
    )

    response = _resume("stored-rpc", observer)

    assert response is not None
    assert "error" not in response, response
    assert response["result"]["session_id"] == "rpc-sid"
    assert session["transport"] is controller
    assert observer in session["viewers"]


def test_unpersisted_lazy_resume_does_not_steal_live_controller(monkeypatch):
    controller = _RecordingTransport()
    observer = _RecordingTransport()
    session = _session(
        transport=controller,
        session_key="stored-lazy",
        profile_home=None,
    )
    server._sessions["lazy-sid"] = session
    monkeypatch.setattr(
        server,
        "_get_db",
        lambda: types.SimpleNamespace(
            get_session=lambda _t: None,
            get_session_by_title=lambda _t: None,
        ),
    )

    response = _resume("stored-lazy", observer)

    assert response is not None
    assert "error" not in response, response
    assert response["result"]["session_id"] == "lazy-sid"
    assert session["transport"] is controller
    assert observer in session["viewers"]


def test_controller_and_observer_receive_the_same_session_event_once():
    controller = _RecordingTransport()
    observer = _RecordingTransport()
    session = _session(transport=controller)
    server._sessions["fanout-sid"] = session
    server._live_session_payload(
        "fanout-sid", session, transport=controller, omit_messages=True
    )
    server._live_session_payload(
        "fanout-sid", session, transport=observer, omit_messages=True
    )

    server._emit("message.delta", "fanout-sid", {"text": "hi"})

    assert len(controller.frames) == 1
    assert len(observer.frames) == 1
    assert controller.frames[0] is observer.frames[0]
    params = controller.frames[0]["params"]
    assert params["type"] == "message.delta"
    assert params["session_id"] == "fanout-sid"
    assert params["payload"] == {"text": "hi"}
    assert params.get("seq")


def test_disconnect_observer_leaves_controller_and_runtime():
    reap_calls = []
    controller = _RecordingTransport()
    observer = _RecordingTransport()
    session = _session(transport=controller)
    session["viewers"] = {controller: 1.0, observer: 2.0}
    server._sessions["obs-sid"] = session

    original_reap = server._schedule_ws_orphan_reap

    def _track_reap(sid, **kwargs):
        reap_calls.append(sid)
        return original_reap(sid, **kwargs)

    server._schedule_ws_orphan_reap = _track_reap  # type: ignore[method-assign]
    try:
        reaped, detached = server._close_sessions_for_transport(observer)
    finally:
        server._schedule_ws_orphan_reap = original_reap  # type: ignore[method-assign]

    assert reaped == 0 and detached == 0
    assert reap_calls == []
    assert "obs-sid" in server._sessions
    assert session["transport"] is controller
    assert observer not in session["viewers"]
    assert controller in session["viewers"]

    server._emit("message.complete", "obs-sid", {"text": "done"})
    assert len(controller.frames) == 1
    assert observer.frames == []


def test_re_resume_after_observer_disconnect_is_idempotent():
    controller = _RecordingTransport()
    observer = _RecordingTransport()
    session = _session(transport=controller)
    server._sessions["idem-sid"] = session

    server._live_session_payload(
        "idem-sid", session, transport=observer, omit_messages=True
    )
    server._live_session_payload(
        "idem-sid", session, transport=observer, omit_messages=True
    )
    assert session["transport"] is controller
    assert list(session["viewers"]).count(observer) == 1

    server._close_sessions_for_transport(observer)
    assert observer not in session["viewers"]
    assert session["transport"] is controller

    server._live_session_payload(
        "idem-sid", session, transport=observer, omit_messages=True
    )
    assert session["transport"] is controller
    assert observer in session["viewers"]
    assert list(session["viewers"]).count(observer) == 1

    server._emit("session.status", "idem-sid", {"status": "running"})
    assert len(controller.frames) == 1
    assert len(observer.frames) == 1
    assert controller.frames[0] is observer.frames[0]


def test_detached_reconnect_still_claims_controller():
    incoming = _RecordingTransport()
    session = _session(transport=server._detached_ws_transport)
    server._live_session_payload(
        "rejoin-sid", session, transport=incoming, omit_messages=True
    )

    assert session["transport"] is incoming
    assert incoming in session["viewers"]


def test_session_events_do_not_leak_to_unrelated_live_transports():
    controller = _RecordingTransport()
    observer = _RecordingTransport()
    bystander = _RecordingTransport()
    session = _session(transport=controller, viewers={controller: 1.0, observer: 2.0})
    server._sessions["scoped-sid"] = session
    server.register_live_transport(controller)
    server.register_live_transport(observer)
    server.register_live_transport(bystander)

    server._emit("message.delta", "scoped-sid", {"text": "secret"})

    assert len(controller.frames) == 1
    assert len(observer.frames) == 1
    assert bystander.frames == []


def test_global_event_broadcast_still_reaches_every_live_transport():
    a = _RecordingTransport()
    b = _RecordingTransport()
    server.register_live_transport(a)
    server.register_live_transport(b)

    server._broadcast_global_event("skin.changed", {"name": "mono"})

    assert len(a.frames) == 1
    assert len(b.frames) == 1
    assert a.frames[0]["params"]["type"] == "skin.changed"
    assert a.frames[0]["params"]["session_id"] == ""
    assert b.frames[0]["params"]["payload"] == {"name": "mono"}


def test_observer_is_not_browser_control_owner():
    """Browser-control ownership stays ``session["transport"] is transport``."""
    controller = _RecordingTransport()
    observer = _RecordingTransport()
    session = _session(transport=controller)
    server._sessions["bc-sid"] = session
    server._live_session_payload(
        "bc-sid", session, transport=observer, omit_messages=True
    )

    assert session["transport"] is controller
    assert session.get("transport") is not observer


class _ThrowingTransport:
    def write(self, obj: dict) -> bool:
        raise RuntimeError("wedged observer")

    def close(self) -> None:
        return None


class _FalseTransport:
    def write(self, obj: dict) -> bool:
        return False

    def close(self) -> None:
        return None


def _event(sid: str, text: str = "hi") -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "message.delta",
            "session_id": sid,
            "payload": {"text": text},
        },
    }


def test_throwing_controller_does_not_starve_observers_or_fail_producer():
    """A raising controller.write must not drop later viewers or the emit."""
    boom = _ThrowingTransport()
    observer = _RecordingTransport()
    later = _RecordingTransport()
    session = _session(
        transport=boom,
        viewers={boom: 1.0, observer: 2.0, later: 3.0},
    )
    server._sessions["iso-ctrl-sid"] = session

    ok = server.write_json(_event("iso-ctrl-sid"))

    assert ok is True
    assert len(observer.frames) == 1
    assert len(later.frames) == 1
    assert observer.frames[0] is later.frames[0]
    assert observer.frames[0]["params"]["payload"] == {"text": "hi"}


def test_throwing_observer_does_not_starve_later_targets():
    controller = _RecordingTransport()
    boom = _ThrowingTransport()
    later = _RecordingTransport()
    session = _session(
        transport=controller,
        viewers={controller: 1.0, boom: 2.0, later: 3.0},
    )
    server._sessions["iso-obs-sid"] = session

    ok = server.write_json(_event("iso-obs-sid"))

    assert ok is True
    assert len(controller.frames) == 1
    assert len(later.frames) == 1
    assert controller.frames[0] is later.frames[0]


def test_write_json_false_when_every_session_target_fails():
    session = _session(
        transport=_ThrowingTransport(),
        viewers={_FalseTransport(): 1.0, _ThrowingTransport(): 2.0},
    )
    server._sessions["iso-all-sid"] = session

    ok = server.write_json(_event("iso-all-sid"))

    assert ok is False


def test_emit_does_not_deadlock_when_caller_holds_history_lock():
    """Snapshot must not acquire history_lock; emitters may already hold it."""
    controller = _RecordingTransport()
    observer = _RecordingTransport()
    session = _session(transport=controller, viewers={observer: 1.0})
    server._sessions["held-sid"] = session

    done = threading.Event()

    def _emit_under_lock():
        with session["history_lock"]:
            server._emit("message.delta", "held-sid", {"text": "held"})
        done.set()

    thread = threading.Thread(target=_emit_under_lock)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive(), "write_json deadlocked on history_lock"
    assert done.is_set()
    assert len(controller.frames) == 1
    assert len(observer.frames) == 1


def test_session_event_targets_survives_concurrent_viewer_mutation():
    """Subscribe/disconnect mutate viewers without a lock shared with emit.

    The snapshot copies under the GIL so a racing pop/setitem cannot raise
    RuntimeError or deadlock an emitter that already holds history_lock.
    """
    controller = _RecordingTransport()
    extras = [_RecordingTransport() for _ in range(8)]
    viewers = {controller: 1.0}
    session = _session(transport=controller, viewers=viewers)
    stop = threading.Event()
    errors: list[BaseException] = []

    def _mutate():
        i = 0
        while not stop.is_set():
            extra = extras[i % len(extras)]
            viewers[extra] = float(i)
            if i % 2:
                viewers.pop(extra, None)
            i += 1

    def _snapshot():
        try:
            for _ in range(4000):
                targets = server._session_event_targets(session)
                assert isinstance(targets, list)
                assert controller in targets
        except BaseException as exc:
            errors.append(exc)

    mutator = threading.Thread(target=_mutate)
    reader = threading.Thread(target=_snapshot)
    mutator.start()
    reader.start()
    reader.join(timeout=5)
    stop.set()
    mutator.join(timeout=5)
    assert not reader.is_alive()
    assert not mutator.is_alive()
    assert errors == []


def test_prompt_submit_from_observer_does_not_steal_controller(monkeypatch):
    """Cyllene submitting into Desktop's live session must not take the writer seat."""
    controller = _RecordingTransport()
    observer = _RecordingTransport()
    session = _session(transport=controller, active_session_lease=object())
    server._sessions["prompt-sid"] = session
    _stub_idle_prompt_submit(monkeypatch)

    response = _submit("prompt-sid", observer)

    try:
        assert response is not None
        assert "error" not in response, response
        assert session["transport"] is controller
        assert observer in session["viewers"]
        # Browser-control ownership stays session["transport"] is controller.
        assert session.get("transport") is not observer

        server._emit("message.delta", "prompt-sid", {"text": "hi"})
        assert len(controller.frames) == 1
        assert len(observer.frames) == 1
        assert controller.frames[0] is observer.frames[0]
    finally:
        _join_run_thread(session)


@pytest.mark.parametrize(
    "make_current",
    [
        lambda: None,
        lambda: server._detached_ws_transport,
        lambda: types.SimpleNamespace(_closed=True),
    ],
    ids=["vacant", "detached", "closed"],
)
def test_prompt_submit_claims_vacant_or_dead_controller(monkeypatch, make_current):
    incoming = _RecordingTransport()
    session = _session(transport=make_current(), active_session_lease=object())
    server._sessions["claim-sid"] = session
    _stub_idle_prompt_submit(monkeypatch)

    response = _submit("claim-sid", incoming)

    try:
        assert response is not None
        assert "error" not in response, response
        assert session["transport"] is incoming
        assert incoming in session["viewers"]
    finally:
        _join_run_thread(session)


def test_busy_observer_prompt_queues_without_stealing_controller(monkeypatch):
    controller = _RecordingTransport()
    observer = _RecordingTransport()
    interrupt_calls = []
    session = _session(
        transport=controller,
        running=True,
        active_session_lease=object(),
        agent=types.SimpleNamespace(interrupt=lambda: interrupt_calls.append(True)),
    )
    server._sessions["busy-sid"] = session
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "interrupt")
    monkeypatch.setattr(
        server,
        "_interrupt_busy_session",
        lambda sid, *_a, **_k: interrupt_calls.append(sid),
    )

    response = _submit("busy-sid", observer)

    assert response is not None
    assert "error" not in response, response
    assert response["result"]["status"] == "queued"
    assert session["transport"] is controller
    assert observer in session["viewers"]
    assert session["queued_prompt"]["text"] == "hello"
    assert session["queued_prompt"]["transport"] is observer
    assert interrupt_calls == ["busy-sid"]

    server._emit("message.delta", "busy-sid", {"text": "still-running"})
    assert len(controller.frames) == 1
    assert len(observer.frames) == 1
    assert controller.frames[0] is observer.frames[0]


def test_drain_queued_observer_prompt_does_not_steal_controller(monkeypatch):
    controller = _RecordingTransport()
    observer = _RecordingTransport()
    session = _session(
        transport=controller,
        viewers={observer: 1.0},
        queued_prompt={"text": "next", "transport": observer},
    )
    server._sessions["drain-sid"] = session
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_a, **_k: False)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *_a, **_k: None)

    assert server._drain_queued_prompt("r1", "drain-sid", session) is True
    assert session["transport"] is controller
    assert observer in session["viewers"]
    assert session["running"] is True


def test_detach_sessions_for_transport_drops_observer_without_reaping_controller():
    reap_calls = []
    controller = _RecordingTransport()
    observer = _RecordingTransport()
    session = _session(transport=controller)
    session["viewers"] = {controller: 1.0, observer: 2.0}
    server._sessions["plugin-obs-sid"] = session

    original_reap = server._schedule_ws_orphan_reap

    def _track_reap(sid, **kwargs):
        reap_calls.append(sid)
        return original_reap(sid, **kwargs)

    server._schedule_ws_orphan_reap = _track_reap  # type: ignore[method-assign]
    try:
        reaped, detached = server.detach_sessions_for_transport(observer)
    finally:
        server._schedule_ws_orphan_reap = original_reap  # type: ignore[method-assign]

    assert reaped == 0 and detached == 0
    assert reap_calls == []
    assert "plugin-obs-sid" in server._sessions
    assert session["transport"] is controller
    assert observer not in session["viewers"]
    assert controller in session["viewers"]

    server._emit("message.complete", "plugin-obs-sid", {"text": "done"})
    assert len(controller.frames) == 1
    assert observer.frames == []


def test_detach_sessions_for_transport_parks_last_controller(monkeypatch):
    reap_calls = []
    monkeypatch.setattr(
        server, "_schedule_ws_orphan_reap", lambda sid, **_k: reap_calls.append(sid)
    )
    only = _RecordingTransport()
    session = _session(transport=only, viewers={only: 1.0})
    server._sessions["plugin-solo-sid"] = session

    reaped, detached = server.detach_sessions_for_transport(only)

    assert reaped == 0 and detached == 1
    assert session["transport"] is server._detached_ws_transport
    assert reap_calls == ["plugin-solo-sid"]
    assert only not in (session.get("viewers") or {})
