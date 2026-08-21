"""Client-message correlation across gateway acceptance and queued turns."""

import threading
import types

from tui_gateway import server


def _session(agent=None, **extra):
    return {
        "agent": agent if agent is not None else types.SimpleNamespace(),
        "session_key": "session-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "transport": None,
        "attached_images": [],
        **extra,
    }


def test_prompt_submit_busy_queue_preserves_client_message_id(monkeypatch):
    session = _session(running=True)
    monkeypatch.setattr(server, "_voice_mode_enabled", lambda: False)
    monkeypatch.setattr(server, "_sess_nowait", lambda _params, _rid: (session, None))
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_args: None)
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "queue")

    response = server._methods["prompt.submit"](
        "rid",
        {
            "session_id": "sid",
            "text": "?",
            "client_message_id": "client-queued",
        },
    )

    assert response["result"] == {"status": "queued"}
    assert session["queued_prompt"] == {
        "text": "?",
        "transport": None,
        "client_message_id": "client-queued",
    }


def test_correlated_queue_entries_do_not_merge_or_drop_identical_text():
    session = _session()
    server._start_inflight_turn(session, "?", "client-active")

    server._enqueue_prompt(session, "?", None, client_message_id="client-next")
    server._enqueue_prompt(session, "later", None, client_message_id="client-later")

    assert session["queued_prompt"] == {
        "text": "?",
        "transport": None,
        "client_message_id": "client-next",
    }
    assert session["queued_prompts"] == [
        {
            "text": "later",
            "transport": None,
            "client_message_id": "client-later",
        }
    ]


def test_drain_forwards_client_message_id_to_started_turn(monkeypatch):
    captured = {}
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda _session: False)
    monkeypatch.setattr(
        server,
        "_run_prompt_submit",
        lambda rid, sid, session, text, **kwargs: captured.update(
            rid=rid,
            sid=sid,
            text=text,
            client_message_id=kwargs.get("client_message_id"),
        ),
    )
    session = _session(
        queued_prompt={
            "text": "?",
            "transport": None,
            "client_message_id": "client-next",
        }
    )

    assert server._drain_queued_prompt("rid", "sid", session) is True
    assert captured == {
        "rid": "rid",
        "sid": "sid",
        "text": "?",
        "client_message_id": "client-next",
    }


def test_resume_snapshots_expose_client_message_id():
    session = _session(
        queued_prompt={
            "text": "next",
            "transport": None,
            "client_message_id": "client-next",
        }
    )
    server._start_inflight_turn(session, "active", "client-active")
    server._record_inflight_correction(session, "redirect")

    assert server._inflight_snapshot(session) == {
        "assistant": "",
        "streaming": True,
        "user": "active",
        "client_message_id": "client-active",
        "corrections": ["redirect"],
        "correction_offsets": [0],
    }
    assert server._queued_prompt_snapshot(session) == {
        "user": "next",
        "client_message_id": "client-next",
    }


def test_session_redirect_lease_wait_queues_client_message_id(monkeypatch):
    agent = types.SimpleNamespace(
        _supports_active_turn_redirect=True,
        _waiting_for_session_turn_lease=True,
        redirect=lambda _text: False,
    )
    session = _session(agent=agent, running=True)
    monkeypatch.setattr(server, "_sess_nowait", lambda _params, _rid: (session, None))
    monkeypatch.setattr(server, "_interrupt_busy_session", lambda *_args: None)

    response = server._methods["session.redirect"](
        "rid",
        {
            "session_id": "sid",
            "text": "?",
            "client_message_id": "client-redirect",
        },
    )

    assert response["result"] == {
        "status": "queued",
        "text": "?",
    }
    assert session["queued_prompt"]["text"] == "?"
    assert session["queued_prompt"]["client_message_id"] == "client-redirect"
    assert session["queued_prompt"]["transport"] is server._stdio_transport


def test_session_redirect_during_tool_batch_reports_deferred_delivery(monkeypatch):
    redirected = []
    agent = types.SimpleNamespace(
        _supports_active_turn_redirect=True,
        _waiting_for_session_turn_lease=False,
        _executing_tools=True,
        redirect=lambda text: redirected.append(text) or True,
    )
    session = _session(agent=agent, running=True)
    server._start_inflight_turn(session, "original", "client-active")
    monkeypatch.setattr(server, "_sess_nowait", lambda _params, _rid: (session, None))

    response = server._methods["session.redirect"](
        "rid",
        {
            "session_id": "sid",
            "text": "?",
            "client_message_id": "client-correction",
        },
    )

    assert response["result"] == {
        "status": "queued",
        "text": "?",
        "delivery": "tool_boundary",
    }
    assert redirected == ["?"]
    assert session["inflight_turn"]["corrections"] == ["?"]
    # Tool-boundary steer belongs to the active turn, not the next-turn FIFO;
    # the client keeps its own correlated optimistic bubble until progress.
    assert session.get("queued_prompt") is None


def test_compute_host_turn_frame_carries_client_message_id():
    session = _session()

    frame = server._compute_host_turn_frame(
        "rid",
        "sid",
        session,
        "?",
        client_message_id="client-isolated",
    )

    assert frame["client_message_id"] == "client-isolated"
