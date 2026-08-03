"""Busy TUI prompts are routed or bounded-queued without silent loss.

The backend applies ``busy_input_mode`` while keeping classification outside
the RPC reader/history lock. SMART input never interrupts; complete envelopes
are steered only into the admitted generation or queued FIFO with receipts.
"""

import json
import logging
import multiprocessing
import os
import threading
import time
import types
from pathlib import Path

import pytest

import tools.async_delegation as ad

from tui_gateway import server
from tui_gateway.transport import Transport


class _AckingSteerAgent:
    """Concrete double for the generation-bound terminal steer protocol."""

    def __init__(self, *, messages=None, activity=None, steer_effect=None):
        self.messages = list(messages or [])
        self.activity = dict(activity or {})
        self.steer_effect = steer_effect
        self.steered: list[str] = []
        self.run_generation = 37
        self.on_consumed = None
        self.on_unconsumed = None
        self.on_uncertain = None

    def get_activity_summary(self):
        return dict(self.activity)

    def get_steer_generation(self):
        return self.run_generation

    def supports_steer_consumption_ack(self):
        return True

    def steer(
        self,
        text,
        *,
        run_generation=None,
        on_consumed=None,
        on_unconsumed=None,
        on_uncertain=None,
    ):
        assert run_generation == self.run_generation
        self.steered.append(text)
        self.on_consumed = on_consumed
        self.on_unconsumed = on_unconsumed
        self.on_uncertain = on_uncertain
        if self.steer_effect is not None:
            return self.steer_effect(text)
        return True

    def interrupt(self, *_args, **_kwargs):
        raise AssertionError("must not interrupt")


class _RecordingTransport(Transport):
    def __init__(self):
        self.writes: list[dict] = []

    def write(self, obj: dict) -> bool:
        self.writes.append(obj)
        return True

    def close(self) -> None:
        return None


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


# ── _enqueue_prompt ────────────────────────────────────────────────────────

def test_enqueue_pins_complete_envelope_and_transport():
    session = _session()
    receipt = server._enqueue_prompt(session, "hello", "ws-1")

    assert receipt["accepted"] is True
    assert receipt["depth"] == 1
    assert session["queued_prompt"]["text"] == "hello"
    assert session["queued_prompt"]["images"] == []
    assert session["queued_prompt"]["transport"] == "ws-1"
    assert session["queued_prompt"]["metadata"]["arrival_ordinal"] == 1


def test_enqueue_preserves_order_after_an_image_turn():
    session = _session()
    assert server._enqueue_prompt(session, "B", "ws-1")["accepted"]
    assert server._enqueue_prompt(
        session,
        "C",
        "ws-1",
        envelope={"text": "C", "images": ["/tmp/c.png"], "transport": "ws-1"},
    )["accepted"]
    assert server._enqueue_prompt(session, "D", "ws-1")["accepted"]

    queued = [session["queued_prompt"], *session["queued_prompt_overflow"]]
    assert [item["text"] for item in queued] == ["B", "C", "D"]
    assert [item["images"] for item in queued] == [[], ["/tmp/c.png"], []]




def test_enqueue_preserves_second_arrival_as_separate_fifo_turn():
    session = _session()
    first = server._enqueue_prompt(session, "first", "ws-1")
    second = server._enqueue_prompt(session, "second", "ws-2")

    assert first["accepted"] is True
    assert second["accepted"] is True
    assert second["depth"] == 2
    assert session["queued_prompt"]["text"] == "first"
    assert session["queued_prompt"]["transport"] == "ws-1"
    assert [item["text"] for item in session["queued_prompt_overflow"]] == ["second"]
    assert session["queued_prompt_overflow"][0]["transport"] == "ws-2"


def test_enqueue_rejects_item_33_explicitly_without_merging_or_dropping():
    session = _session()

    receipts = [
        server._enqueue_prompt(session, f"turn-{index}", f"ws-{index}")
        for index in range(33)
    ]

    assert all(receipt["accepted"] for receipt in receipts[:32])
    assert receipts[32]["accepted"] is False
    assert receipts[32]["reason"] == "max_items"
    assert receipts[32]["depth"] == 32
    queued = [session["queued_prompt"], *session["queued_prompt_overflow"]]
    assert [item["text"] for item in queued] == [f"turn-{index}" for index in range(32)]
    assert all("turn-32" not in str(item["text"]) for item in queued)


def test_enqueue_enforces_configured_serialized_byte_cap(monkeypatch):
    monkeypatch.setattr(server.time, "time", lambda: 100.0)
    session = _session()
    first = {
        "text": "x" * 64,
        "images": ["/tmp/one.png"],
        "transport": "ws-1",
        "metadata": {
            "arrival_ordinal": 1,
            "received_at": 100.0,
            "receipt_id": "byte-receipt-1",
            "request_id": "byte-1",
            "session_key": "session-key",
        },
    }
    second = {
        **first,
        "transport": "ws-2",
        "metadata": {
            **first["metadata"],
            "arrival_ordinal": 2,
            "attachment_owner": "batch:byte-2",
            "receipt_id": "byte-receipt-2",
            "request_id": "byte-2",
        },
    }
    cap = max(server._prompt_envelope_size(first), server._prompt_envelope_size(second))
    monkeypatch.setattr(
        server,
        "_load_tui_busy_queue_config",
        lambda: {"max_bytes": cap, "ttl_seconds": 900.0},
    )

    accepted = server._enqueue_prompt(session, first["text"], "ws-1", envelope=first)
    rejected = server._enqueue_prompt(session, second["text"], "ws-2", envelope=second)

    assert accepted["accepted"] is True
    assert accepted["queued_bytes"] <= cap
    assert rejected["accepted"] is False
    assert rejected["reason"] == "max_bytes"
    assert rejected["depth"] == 1
    assert session["queued_prompt"]["metadata"]["request_id"] == "byte-1"
    # Rejected media remains on its explicit reservation; it never returns to
    # global staging where an unrelated prompt could inherit it.
    assert session["attached_images"] == []
    assert session["attached_images_by_owner"] == {
        "batch:byte-2": ["/tmp/one.png"]
    }





def test_finalize_clears_all_queued_and_active_envelope_references(monkeypatch):
    monkeypatch.setattr(server, "_release_active_session_slot", lambda _session: None)
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_get_db", lambda: None)
    session = _session(source="test")
    assert server._enqueue_prompt(session, "first", "ws-1")["accepted"]
    assert server._enqueue_prompt(session, "second", "ws-2")["accepted"]
    session["active_prompt_envelope"] = {
        "text": "active",
        "images": ["/tmp/active.png"],
        "transport": "ws-active",
        "metadata": {"arrival_ordinal": 99, "received_at": 100.0},
    }

    server._finalize_session(session, end_reason="test")

    assert session["_finalized"] is True
    assert session.get("queued_prompt") is None
    assert session.get("queued_prompt_overflow") is None
    assert session.get("active_prompt_envelope") is None
    assert session["queued_prompt_bytes"] == 0

    recovered = _session(source="test")
    with server._prompt_queue_lock(recovered):
        restored = server._queued_prompt_items_locked(recovered)
    assert [item["text"] for item in restored] == ["first", "second"]


def test_busy_overflow_rejects_truthfully_without_interrupting(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "interrupt")
    interrupt_calls = []
    session = _session(
        agent=types.SimpleNamespace(interrupt=lambda: interrupt_calls.append(True)),
        running=True,
    )
    for index in range(32):
        assert server._enqueue_prompt(session, f"queued-{index}", None)["accepted"]

    resp = server._handle_busy_submit("overflow", "sid", session, "rejected", "ws-new")

    assert resp["result"]["status"] == "queue_rejected"
    assert resp["result"]["accepted"] is False
    assert resp["result"]["reason"] == "max_items"
    assert resp["result"]["depth"] == 32
    assert interrupt_calls == []
    queued = [session["queued_prompt"], *session["queued_prompt_overflow"]]
    assert [item["text"] for item in queued] == [f"queued-{index}" for index in range(32)]


def test_busy_smart_overflow_ack_is_rejected_not_queued(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "smart")
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (_smart_decision("dependent"), "turn-33"),
    )
    session = _session(
        agent=types.SimpleNamespace(
            messages=[],
            get_activity_summary=lambda: {},
            interrupt=lambda: (_ for _ in ()).throw(AssertionError("SMART must not interrupt")),
        ),
        running=True,
        turn_generation=1,
        inflight_turn={"generation": 1, "user": "active", "streaming": True},
    )
    for index in range(32):
        assert server._enqueue_prompt(session, f"turn-{index}", None)["accepted"]

    resp = server._handle_busy_submit("smart-overflow", "sid", session, "turn-33", None)

    assert resp["result"]["status"] == "smart_rejected"
    assert resp["result"]["accepted"] is False
    assert resp["result"]["reason"] == "max_items"
    assert "não foi aceita" in resp["result"]["ack"].lower()
    queued = [session["queued_prompt"], *session["queued_prompt_overflow"]]
    assert [item["text"] for item in queued] == [f"turn-{index}" for index in range(32)]


def test_busy_smart_media_envelopes_stay_with_their_own_fifo_turn(monkeypatch):
    """Two media submissions cannot steer or leak an image into the other turn."""

    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "smart")
    classifier_calls = []
    steer_calls = []

    def forbidden_classifier(**kwargs):
        classifier_calls.append(kwargs)
        raise AssertionError("media must not be classified")

    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        forbidden_classifier,
    )
    agent = types.SimpleNamespace(
        steer=lambda text: steer_calls.append(text) or True,
        messages=[],
        get_activity_summary=lambda: {},
    )
    session = _session(
        agent=agent,
        running=True,
        turn_generation=1,
        inflight_turn={"generation": 1, "user": "active", "streaming": True},
        attached_images=["/tmp/first.png"],
    )
    fired = []

    def capture_dispatch(rid, _sid, captured_session, text, **kwargs):
        assert kwargs["queued_prompt_generation"] == 0
        envelope = captured_session.pop("active_prompt_envelope")
        fired.append(
            (
                rid,
                text,
                list(envelope["images"]),
                envelope["metadata"]["request_id"],
            )
        )

    monkeypatch.setattr(server, "_run_prompt_submit", capture_dispatch)
    server._sessions["smart-media"] = session
    try:
        first = server.handle_request(
            {
                "id": "media-1",
                "method": "prompt.submit",
                "params": {"session_id": "smart-media", "text": "first turn"},
            }
        )
        assert first["result"]["status"] == "smart_queued"
        assert session["attached_images"] == []

        with session["history_lock"]:
            session["attached_images"].append("/tmp/second.png")
        second = server.handle_request(
            {
                "id": "media-2",
                "method": "prompt.submit",
                "params": {"session_id": "smart-media", "text": "second turn"},
            }
        )
        assert second["result"]["status"] == "smart_queued"
        assert session["attached_images"] == []

        with session["history_lock"]:
            session["running"] = False
        assert server._drain_queued_prompt("drain-1", "smart-media", session) is True
        assert server._complete_queued_prompt_claim(session) is True
        with session["history_lock"]:
            session["running"] = False
        assert server._drain_queued_prompt("drain-2", "smart-media", session) is True
    finally:
        server._sessions.pop("smart-media", None)

    assert classifier_calls == []
    assert steer_calls == []
    assert fired == [
        ("drain-1", "first turn", ["/tmp/first.png"], "media-1"),
        ("drain-2", "second turn", ["/tmp/second.png"], "media-2"),
    ]


def test_busy_smart_compute_host_queues_without_local_steer_or_classifier(monkeypatch):
    """Without a remote-steer protocol, an isolated live turn fails closed."""

    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "smart")
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {
            "dashboard": {"turn_isolation": True},
            "orchestration": {"smart": {}},
        },
    )
    classifier_calls = []
    steer_calls = []
    interrupt_calls = []

    def forbidden_classifier(**kwargs):
        classifier_calls.append(kwargs)
        return _smart_decision("related"), "remote update"

    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        forbidden_classifier,
    )
    proxy = types.SimpleNamespace(
        steer=lambda text: steer_calls.append(text) or True,
        interrupt=lambda: interrupt_calls.append(True),
        messages=[],
        get_activity_summary=lambda: {},
    )
    session = _session(
        agent=proxy,
        running=True,
        _compute_host_active=True,
        turn_generation=7,
        inflight_turn={"generation": 7, "user": "remote turn", "streaming": True},
    )

    resp = server._handle_busy_submit(
        "remote-queued",
        "remote-sid",
        session,
        "remote update",
        "ws-remote",
    )

    assert resp["result"]["status"] == "smart_queued"
    assert resp["result"]["route"] == "ambiguous"
    assert "fila" in resp["result"]["ack"].lower()
    assert session["queued_prompt"]["text"] == "remote update"
    assert classifier_calls == []
    assert steer_calls == []
    assert interrupt_calls == []


def test_busy_smart_turn_end_during_classification_drains_immediately(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "smart")
    fired = []
    agent = types.SimpleNamespace(
        steer=lambda _text: (_ for _ in ()).throw(AssertionError("stale run must not steer")),
        messages=[],
        get_activity_summary=lambda: {},
    )
    session = _session(
        agent=agent,
        running=True,
        turn_generation=4,
        inflight_turn={"generation": 4, "user": "run N", "streaming": True},
    )

    def finish_run(**_kwargs):
        with session["history_lock"]:
            session["running"] = False
            session["inflight_turn"] = None
        return _smart_decision("related"), "run after N"

    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        finish_run,
    )
    monkeypatch.setattr(
        server,
        "_run_prompt_submit",
        lambda _rid, _sid, _session, text, **_kwargs: fired.append(text),
    )

    resp = server._handle_busy_submit(
        "r-drain",
        "sid",
        session,
        "run after N",
        "ws-1",
    )

    assert resp["result"]["status"] == "smart_started"
    assert fired == ["run after N"]
    assert session.get("queued_prompt") is None
    assert session["running"] is True


def test_busy_smart_classifier_result_cannot_steer_reused_agent_next_generation(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "smart")
    steered = []
    agent = types.SimpleNamespace(
        steer=lambda text: steered.append(text) or True,
        messages=[],
        get_activity_summary=lambda: {},
    )
    session = _session(
        agent=agent,
        running=True,
        turn_generation=1,
        inflight_turn={
            "generation": 1,
            "user": "run N",
            "streaming": True,
        },
    )

    def replace_run_during_classification(**_kwargs):
        with session["history_lock"]:
            session["running"] = True
            session["turn_generation"] = 2
            session["inflight_turn"] = {
                "generation": 2,
                "user": "run N+1",
                "streaming": True,
            }
        return _smart_decision("related"), "belongs to run N"

    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        replace_run_during_classification,
    )

    resp = server._handle_busy_submit(
        "r-generation",
        "sid",
        session,
        "belongs to run N",
        "ws-1",
    )

    assert resp["result"]["status"] == "smart_queued"
    assert session["queued_prompt"]["text"] == "belongs to run N"
    assert steered == []


def test_busy_smart_generation_is_captured_before_waiting_for_route_lock(monkeypatch):
    """Admission for run N cannot be retargeted while waiting behind a classifier."""

    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "smart")
    steered = []
    agent = types.SimpleNamespace(
        steer=lambda text: steered.append(text) or True,
        messages=[],
        get_activity_summary=lambda: {},
    )
    route_lock = threading.Lock()
    route_lock.acquire()
    session = _session(
        agent=agent,
        running=True,
        turn_generation=10,
        inflight_turn={"generation": 10, "user": "run N", "streaming": True},
        smart_route_lock=route_lock,
    )
    captured = threading.Event()
    original_capture = server._capture_prompt_envelope_locked

    def capture_then_signal(*args, **kwargs):
        envelope = original_capture(*args, **kwargs)
        captured.set()
        return envelope

    monkeypatch.setattr(server, "_capture_prompt_envelope_locked", capture_then_signal)
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (_smart_decision("related"), "for run N"),
    )
    server._sessions["admission-generation"] = session
    responses = []

    submit_thread = threading.Thread(
        target=lambda: responses.append(
            server.handle_request(
                {
                    "id": "admission-N",
                    "method": "prompt.submit",
                    "params": {
                        "session_id": "admission-generation",
                        "text": "for run N",
                    },
                }
            )
        )
    )
    submit_thread.start()
    try:
        assert captured.wait(timeout=2)
        with session["history_lock"]:
            session["turn_generation"] = 11
            session["inflight_turn"] = {
                "generation": 11,
                "user": "run N+1",
                "streaming": True,
            }
        route_lock.release()
        submit_thread.join(timeout=2)
    finally:
        if route_lock.locked():
            route_lock.release()
        submit_thread.join(timeout=2)
        server._sessions.pop("admission-generation", None)

    assert not submit_thread.is_alive()
    assert responses[0]["result"]["status"] == "smart_queued"
    assert session["queued_prompt"]["text"] == "for run N"
    assert steered == []


def test_busy_smart_slow_classifier_does_not_hold_history_lock_or_dispatcher(monkeypatch):
    """SMART classification cannot block turn finalization or control RPC ingress."""

    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "smart")
    classifier_started = threading.Event()
    release_classifier = threading.Event()
    response_written = threading.Event()

    class _Transport:
        def write(self, _message):
            response_written.set()
            return True

    agent = types.SimpleNamespace(
        steer=lambda _text: True,
        messages=[],
        get_activity_summary=lambda: {},
    )
    session = _session(
        agent=agent,
        running=True,
        inflight_turn={"user": "Fix gateway", "streaming": True},
    )

    def slow_classifier(**_kwargs):
        classifier_started.set()
        assert release_classifier.wait(timeout=2)
        return _smart_decision("related"), "late update"

    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        slow_classifier,
    )
    server._sessions["smart-lock"] = session
    dispatch_returned = threading.Event()

    def dispatch_submit():
        server.dispatch(
            {
                "id": "r-lock",
                "method": "prompt.submit",
                "params": {"session_id": "smart-lock", "text": "late update"},
            },
            _Transport(),
        )
        dispatch_returned.set()

    submit_thread = threading.Thread(target=dispatch_submit)
    submit_thread.start()
    try:
        assert classifier_started.wait(timeout=2)
        assert dispatch_returned.wait(timeout=0.5), "prompt.submit blocked the RPC reader"
        assert session["history_lock"].acquire(timeout=0.5), (
            "slow SMART classification held history_lock"
        )
        session["history_lock"].release()
    finally:
        release_classifier.set()
        submit_thread.join(timeout=2)
        response_written.wait(timeout=2)
        server._sessions.pop("smart-lock", None)


def test_compute_host_frame_uses_envelope_images_not_unbound_session_images():
    """An isolated turn receives the exact media captured with its prompt."""

    session = _session(attached_images=["/tmp/next-unbound.png"])
    envelope = {
        "text": "queued with image",
        "images": ["/tmp/owned.png"],
        "transport": "ws-owned",
        "metadata": {"arrival_ordinal": 1, "received_at": 1.0},
    }

    frame = server._compute_host_turn_frame(
        "compute-rid",
        "compute-sid",
        session,
        "wrong fallback text",
        envelope=envelope,
    )

    assert frame["text"] == "queued with image"
    assert frame["attached_images"] == ["/tmp/owned.png"]
    assert session["attached_images"] == ["/tmp/next-unbound.png"]


def test_compute_host_submit_with_envelope_preserves_later_attached_images(monkeypatch):
    class Supervisor:
        def submit_turn(self, _frame, *, on_complete):
            self.on_complete = on_complete

    supervisor = Supervisor()
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda _cfg: supervisor)
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    envelope = {
        "text": "inspect first",
        "images": ["/tmp/first.png"],
        "transport": "ws-first",
        "metadata": {"arrival_ordinal": 1, "received_at": time.time()},
    }
    session = _session(
        attached_images=["/tmp/later.png"],
        active_prompt_envelope=envelope,
    )

    response = server._submit_prompt_to_compute_host(
        "compute-envelope",
        "sid",
        session,
        envelope["text"],
        envelope=envelope,
    )

    assert response["result"]["status"] == "streaming"
    assert session["attached_images"] == ["/tmp/later.png"]
    assert session.get("active_prompt_envelope") is None


# ── _handle_busy_submit (policy) ───────────────────────────────────────────

def test_busy_interrupt_mode_redirects_active_turn(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "interrupt")
    seen = []
    agent = types.SimpleNamespace(
        _supports_active_turn_redirect=True,
        redirect=lambda text: seen.append(text) or True,
        interrupt=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("redirect must not hard-interrupt")
        ),
    )
    session = _session(agent=agent, running=True)
    session["inflight_turn"] = {"user": "original request", "assistant": "partial reply"}

    resp = server._handle_busy_submit("r1", "sid", session, "redirect", "ws-1")

    assert resp["result"]["status"] == "redirected"
    assert seen == ["redirect"]
    # Appended, not overwritten: the original prompt must stay recoverable.
    assert session["inflight_turn"]["user"] == "original request"
    assert session["inflight_turn"]["corrections"] == ["redirect"]
    assert session.get("queued_prompt") is None








def test_busy_interrupt_mode_ignores_completed_background_delegation(monkeypatch):
    """A terminal delegation must not suppress normal busy-turn interruption."""
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "interrupt")
    calls = {"interrupt": 0}
    agent = types.SimpleNamespace(
        interrupt=lambda *a, **k: calls.__setitem__("interrupt", calls["interrupt"] + 1)
    )
    session = _session(agent=agent, running=True)

    with ad._records_lock:
        ad._records["deleg_completed"] = {
            "delegation_id": "deleg_completed",
            "status": "completed",
            "session_key": "session-key",
            "origin_ui_session_id": "sid",
        }

    try:
        resp = server._handle_busy_submit("r1", "sid", session, "continue", "ws-1")
    finally:
        with ad._records_lock:
            ad._records.clear()

    assert resp["result"]["status"] == "queued"
    assert calls["interrupt"] == 1
    assert session["queued_prompt"]["text"] == "continue"




def test_busy_steer_mode_injects_when_accepted(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "steer")
    agent = _AckingSteerAgent()
    session = _session(
        agent=agent,
        running=True,
        turn_generation=7,
        inflight_turn={"generation": 7, "user": "active", "streaming": True},
    )

    resp = server._handle_busy_submit("r1", "sid", session, "nudge", "ws-1")

    assert resp["result"]["status"] == "steered"
    assert resp["result"]["accepted"] is True
    assert agent.steered == ["nudge"]
    assert callable(agent.on_consumed)
    assert callable(agent.on_unconsumed)
    assert callable(agent.on_uncertain)
    assert session["_smart_steer_claim"]["text"] == "nudge"
    assert session.get("queued_prompt") is None
    agent.on_consumed()






def test_busy_helper_retries_when_turn_finished(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "interrupt")
    session = _session(running=False)

    assert server._handle_busy_submit("r1", "sid", session, "run now", "ws-1") is None
    assert session.get("queued_prompt") is None






def test_busy_interrupt_mode_queues_multimodal_payload_instead_of_redirect(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "interrupt")
    seen = []
    rich = [
        {"type": "text", "text": "caption"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
    ]
    agent = types.SimpleNamespace(
        _supports_active_turn_redirect=True,
        redirect=lambda text: seen.append(text) or True,
        interrupt=lambda *a, **k: None,
    )
    session = _session(agent=agent, running=True)

    resp = server._handle_busy_submit("r1", "sid", session, rich, "ws-1")

    assert resp["result"]["status"] == "queued"
    assert seen == []
    assert session["queued_prompt"]["text"] == rich


def test_busy_submit_claims_attached_image_for_queued_turn(monkeypatch):
    """A pasted image belongs to its submitted prompt, not ambient session state."""
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "interrupt")
    redirected = []
    interrupted = threading.Event()
    agent = types.SimpleNamespace(
        _supports_active_turn_redirect=True,
        redirect=lambda text: redirected.append(text) or True,
        interrupt=interrupted.set,
    )
    session = _session(agent=agent, running=True, attached_images=["/tmp/b.png"])
    server._sessions["sid"] = session
    try:
        response = server._methods["prompt.submit"](
            "r1", {"session_id": "sid", "text": "is this B?"}
        )
    finally:
        server._sessions.pop("sid", None)

    assert response["result"]["status"] == "queued"
    assert redirected == []
    assert not interrupted.wait(0.1)
    assert session["attached_images"] == []
    queued = session["queued_prompt"]
    assert queued["text"] == "is this B?"
    assert queued["images"] == ["/tmp/b.png"]
    assert queued["transport"] is None
    assert queued["metadata"]["request_id"] == "r1"


def test_busy_image_prompts_keep_b_and_c_attachments_in_submission_order(monkeypatch):
    """A later paste must not replace the image already claimed by B."""
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "interrupt")
    dispatched = []

    def capture(rid, sid, captured_session, text, **kwargs):
        envelope = captured_session.pop("active_prompt_envelope")
        dispatched.append((rid, sid, text, list(envelope["images"]), kwargs))
        server._complete_queued_prompt_claim(
            captured_session,
            expected=envelope,
        )

    monkeypatch.setattr(server, "_run_prompt_submit", capture)
    agent = types.SimpleNamespace(
        _supports_active_turn_redirect=True,
        redirect=lambda _text: (_ for _ in ()).throw(AssertionError("images must queue")),
        interrupt=lambda: None,
    )
    session = _session(agent=agent, running=True, attached_images=["/tmp/b.png"])
    server._sessions["sid"] = session
    try:
        server._methods["prompt.submit"]("b", {"session_id": "sid", "text": "B"})
        with session["history_lock"]:
            session["attached_images"] = ["/tmp/c.png"]
        server._methods["prompt.submit"]("c", {"session_id": "sid", "text": "C"})

        queued = [session["queued_prompt"], *session["queued_prompt_overflow"]]
        assert [item["images"] for item in queued] == [["/tmp/b.png"], ["/tmp/c.png"]]

        session["running"] = False
        assert server._drain_queued_prompt("drain-b", "sid", session) is True
        session["running"] = False
        assert server._drain_queued_prompt("drain-c", "sid", session) is True
    finally:
        server._sessions.pop("sid", None)

    assert [(rid, sid, text, images) for rid, sid, text, images, _ in dispatched] == [
        ("drain-b", "sid", "B", ["/tmp/b.png"]),
        ("drain-c", "sid", "C", ["/tmp/c.png"]),
    ]
    assert all(item[4] == {"queued_prompt_generation": 0} for item in dispatched)


def _smart_decision(route):
    from hermes_cli.smart_orchestrator import SmartRouteDecision

    return SmartRouteDecision(
        route=route,
        confidence=0.95,
        reason=f"reason-{route}",
        source="classifier",
    )

def test_busy_smart_related_steers_without_interrupt(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "smart")
    agent = _AckingSteerAgent(
        messages=[{"role": "user", "content": "Fix gateway"}],
        activity={"current_tool": "terminal"},
    )
    session = _session(
        agent=agent,
        running=True,
        inflight_turn={"user": "Fix gateway", "assistant": "", "streaming": True},
    )
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (_smart_decision("related"), "add tests"),
    )

    resp = server._handle_busy_submit("r1", "sid", session, "add tests", "ws-1")

    assert resp["result"]["status"] == "smart_related"
    assert resp["result"]["route"] == "related"
    assert "continua" in resp["result"]["ack"].lower()
    assert agent.steered == ["add tests"]
    assert session.get("queued_prompt") is None



def test_busy_smart_dependent_and_classifier_failure_queue_without_interrupt(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "smart")
    interrupted = []
    agent = types.SimpleNamespace(
        steer=lambda text: (_ for _ in ()).throw(AssertionError("must not steer")),
        interrupt=lambda *a, **k: interrupted.append(True),
        messages=[],
        get_activity_summary=lambda: {},
    )

    for classifier in (
        lambda **_kwargs: (_smart_decision("dependent"), "deploy later"),
        lambda **_kwargs: (_ for _ in ()).throw(TimeoutError("provider down")),
    ):
        session = _session(
            agent=agent,
            running=True,
            inflight_turn={"user": "Build artifact"},
        )
        monkeypatch.setattr(
            "hermes_cli.smart_orchestrator.classify_smart_message",
            classifier,
        )
        resp = server._handle_busy_submit("r1", "sid", session, "deploy later", "ws-1")
        assert resp["result"]["status"] == "smart_queued"
        assert session["queued_prompt"]["text"] == "deploy later"

    assert interrupted == []

def test_busy_smart_rechecks_live_turn_after_classifier_race(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "smart")
    steered = []
    interrupted = []
    agent = types.SimpleNamespace(
        steer=lambda text: steered.append(text) or True,
        interrupt=lambda *a, **k: interrupted.append(True),
        messages=[],
        get_activity_summary=lambda: {},
    )
    session = _session(agent=agent, running=True, inflight_turn={"user": "Fix gateway"})
    fired = []
    monkeypatch.setattr(
        server,
        "_run_prompt_submit",
        lambda _rid, _sid, _session, queued_text, **_kwargs: fired.append(queued_text),
    )

    def finish_turn_during_classification(**_kwargs):
        session["running"] = False
        return _smart_decision("related"), "late update"

    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        finish_turn_during_classification,
    )

    resp = server._handle_busy_submit("r1", "sid", session, "late update", "ws-1")

    assert resp["result"]["status"] == "smart_started"
    assert session.get("queued_prompt") is None
    assert fired == ["late update"]
    assert steered == []
    assert interrupted == []


# ── _drain_queued_prompt ───────────────────────────────────────────────────

def test_drain_fires_queued_prompt_and_claims_running(monkeypatch):
    fired = {}
    monkeypatch.setattr(
        server, "_run_prompt_submit",
        lambda rid, sid, session, text, **kwargs: fired.update(rid=rid, sid=sid, text=text),
    )
    session = _session(queued_prompt={"text": "go", "transport": "ws-9"})

    assert server._drain_queued_prompt("r1", "sid", session) is True
    assert fired == {"rid": "r1", "sid": "sid", "text": "go"}
    assert session["running"] is True
    assert session["queued_prompt"] is None
    assert session["transport"] == "ws-9"


def test_drain_compute_host_forwards_queued_envelope_images(monkeypatch):
    captured = {}
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda _session: True)
    monkeypatch.setattr(
        server,
        "_submit_prompt_to_compute_host",
        lambda rid, sid, session, text, **kwargs: captured.update(
            rid=rid,
            sid=sid,
            text=text,
            images=list(kwargs["envelope"]["images"]),
            generation=kwargs.get("queued_prompt_generation"),
        )
        or {"result": {"status": "started"}},
    )
    session = _session(
        queued_prompt={
            "text": "inspect",
            "images": ["/tmp/b.png"],
            "transport": "ws-9",
            "metadata": {"arrival_ordinal": 1, "received_at": time.time()},
        }
    )

    assert server._drain_queued_prompt("r1", "sid", session) is True
    assert captured == {
        "rid": "r1",
        "sid": "sid",
        "text": "inspect",
        "images": ["/tmp/b.png"],
        "generation": 0,
    }






def test_drain_releases_running_on_dispatch_failure(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("dispatch failed")
    monkeypatch.setattr(server, "_run_prompt_submit", _boom)
    session = _session(queued_prompt={"text": "go", "transport": None})

    assert server._drain_queued_prompt("r1", "sid", session) is True
    # Failure must not leave the session wedged as running.
    assert session["running"] is False


def test_drain_does_not_dispatch_a_prompt_cancelled_after_claim(monkeypatch):
    session = _session(queued_prompt={"text": "B", "transport": None})
    monkeypatch.setattr(
        server,
        "_session_uses_compute_host",
        lambda _session: session.__setitem__("_queued_prompt_generation", 1) or False,
    )
    monkeypatch.setattr(
        server,
        "_run_prompt_submit",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not dispatch")),
    )

    assert server._drain_queued_prompt("r1", "sid", session) is True
    assert session["running"] is False


def test_drain_does_not_clear_stop_after_its_final_generation_check(monkeypatch):
    class _Agent:
        clear_calls = 0

        def clear_interrupt(self):
            self.clear_calls += 1

    agent = _Agent()
    session = _session(agent=agent, queued_prompt={"text": "B", "transport": None})
    original_run = server._run_prompt_submit

    def stop_before_run(*args, **kwargs):
        session["_queued_prompt_generation"] = 1
        return original_run(*args, **kwargs)

    monkeypatch.setattr(server, "_session_uses_compute_host", lambda _session: False)
    monkeypatch.setattr(server, "_run_prompt_submit", stop_before_run)

    assert server._drain_queued_prompt("r1", "sid", session) is True
    assert agent.clear_calls == 0
    assert session["running"] is False
    assert session["queued_prompt"]["text"] == "B"


def test_drain_requeues_failed_prompt_ahead_of_later_envelope(monkeypatch):
    calls = []

    def _run(_rid, _sid, _session, text, **_kwargs):
        calls.append(text)
        raise RuntimeError("dispatch failed")

    monkeypatch.setattr(server, "_run_prompt_submit", _run)
    now = time.time()
    session = _session(
        queued_prompt={
            "text": "broken",
            "images": [],
            "transport": None,
            "metadata": {"arrival_ordinal": 1, "received_at": now},
        },
        queued_prompt_overflow=[{
            "text": "next",
            "images": ["/tmp/next.png"],
            "transport": None,
            "metadata": {"arrival_ordinal": 2, "received_at": now},
        }],
    )

    assert server._drain_queued_prompt("r1", "sid", session) is True
    assert calls == ["broken"]
    queued = [session["queued_prompt"], *session["queued_prompt_overflow"]]
    assert [item["text"] for item in queued] == ["broken", "next"]
    assert session["running"] is False


def _ipc_competing_prompt_queue_writer(
    home: str,
    session_key: str,
    loaded,
    started,
    finished,
    results,
) -> None:
    """Attempt a write through a second process/session representation."""

    try:
        server._hermes_home = Path(home)
        if not loaded.wait(timeout=5):
            raise TimeoutError("stale writer did not load queue state")
        session = _session(session_key=session_key)
        started.set()
        receipt = server._enqueue_prompt(session, "from-b", "transport-b")
        if not receipt["accepted"]:
            raise AssertionError(f"competing admission rejected: {receipt}")
        results.put(("b", "ok"))
    except BaseException as exc:  # pragma: no cover - child-process diagnostics
        results.put(("b", f"{type(exc).__name__}: {exc}"))
    finally:
        finished.set()


def _ipc_stale_prompt_queue_writer(
    home: str,
    session_key: str,
    loaded,
    allow_store,
    results,
) -> None:
    """Hold a stale RMW candidate open at the interprocess lock boundary."""

    try:
        server._hermes_home = Path(home)
        session = _session(session_key=session_key)
        with server._prompt_queue_lock(session):
            items = server._queued_prompt_items_locked(session)
            loaded.set()
            if not allow_store.wait(timeout=5):
                raise TimeoutError("parent did not release stale queue writer")
            item = server._normalize_prompt_envelope(
                session,
                "from-a",
                "transport-a",
                None,
            )
            server._store_queued_prompt_items_locked(session, [*items, item])
        results.put(("a", "ok"))
    except BaseException as exc:  # pragma: no cover - child-process diagnostics
        results.put(("a", f"{type(exc).__name__}: {exc}"))


@pytest.fixture(autouse=True)
def _isolate_durable_prompt_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_hermes_home", tmp_path)


def test_accepted_queue_survives_process_state_recreation_with_media(tmp_path):
    first_session = _session(session_key="durable-session")
    envelope = {
        "text": "process after restart",
        "images": ["/tmp/image-a.png"],
        "transport": "old-ws",
        "metadata": {
            "arrival_ordinal": 1,
            "received_at": 100.0,
            "request_id": "req-1",
            "session_key": "durable-session",
        },
    }
    receipt = server._enqueue_prompt(
        first_session,
        envelope["text"],
        envelope["transport"],
        envelope=envelope,
    )
    state_path = server._prompt_queue_state_path(first_session)

    assert receipt["accepted"] is True
    assert state_path is not None and state_path.exists()
    assert state_path.stat().st_mode & 0o777 == 0o600

    restored_session = _session(session_key="durable-session", transport="new-ws")
    with server._prompt_queue_lock(restored_session):
        items = server._queued_prompt_items_locked(restored_session)

    assert [item["text"] for item in items] == ["process after restart"]
    assert items[0]["images"] == ["/tmp/image-a.png"]
    assert items[0]["transport"] == "new-ws"

    with server._prompt_queue_lock(restored_session):
        server._store_queued_prompt_items_locked(restored_session, [])
    assert state_path.exists()
    tombstone = json.loads(state_path.read_text(encoding="utf-8"))
    assert tombstone["items"] == []
    assert tombstone["claim"] is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink contract")
def test_prompt_queue_state_rejects_symlinked_private_ancestor(tmp_path):
    profile_home = tmp_path / "profile"
    profile_home.mkdir(mode=0o700)
    outside_cache = tmp_path / "outside-cache"
    outside_cache.mkdir(mode=0o700)
    try:
        (profile_home / "cache").symlink_to(outside_cache, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    session = _session(
        session_key="symlinked-cache-ancestor",
        profile_home=str(profile_home),
    )

    with pytest.raises(OSError, match="unsafe private artifact directory"):
        server._prompt_queue_state_path(session)
    assert list(outside_cache.iterdir()) == []


def test_prompt_queue_restore_rejects_symlinked_ledger(tmp_path):
    session_key = "symlinked-ledger"
    original = _session(session_key=session_key)
    assert server._enqueue_prompt(original, "private prompt", "old-ws")["accepted"]
    state_path = server._prompt_queue_state_path(original)
    assert state_path is not None
    outside = tmp_path / "outside-ledger.json"
    state_path.replace(outside)
    try:
        state_path.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    outside_bytes = outside.read_bytes()

    restored = _session(session_key=session_key, transport="new-ws")
    with server._prompt_queue_lock(restored):
        items = server._queued_prompt_items_locked(restored)
    receipt = server._enqueue_prompt(restored, "must not overwrite", "new-ws")

    assert items == []
    assert restored.get("_prompt_queue_restore_error") is True
    assert receipt["accepted"] is False
    assert receipt["reason"] == "recovery_error"
    assert state_path.is_symlink()
    assert outside.read_bytes() == outside_bytes


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link contract")
def test_prompt_queue_restore_rejects_hardlinked_ledger(tmp_path):
    session_key = "hardlinked-ledger"
    original = _session(session_key=session_key)
    assert server._enqueue_prompt(original, "private prompt", "old-ws")["accepted"]
    state_path = server._prompt_queue_state_path(original)
    assert state_path is not None
    outside = tmp_path / "outside-hardlink.json"
    os.link(state_path, outside)
    outside_bytes = outside.read_bytes()

    restored = _session(session_key=session_key, transport="new-ws")
    with server._prompt_queue_lock(restored):
        items = server._queued_prompt_items_locked(restored)
    receipt = server._enqueue_prompt(restored, "must not overwrite", "new-ws")

    assert items == []
    assert restored.get("_prompt_queue_restore_error") is True
    assert receipt["accepted"] is False
    assert receipt["reason"] == "recovery_error"
    assert state_path.stat().st_nlink == 2
    assert outside.read_bytes() == outside_bytes


@pytest.mark.skipif(os.name == "nt", reason="POSIX private-mode contract")
def test_prompt_queue_restore_repairs_nonprivate_ledger_mode_before_reading():
    session_key = "public-mode-ledger"
    original = _session(session_key=session_key)
    assert server._enqueue_prompt(original, "private prompt", "old-ws")["accepted"]
    state_path = server._prompt_queue_state_path(original)
    assert state_path is not None
    os.chmod(state_path, 0o644)

    restored = _session(session_key=session_key, transport="new-ws")
    with server._prompt_queue_lock(restored):
        items = server._queued_prompt_items_locked(restored)

    assert [item["text"] for item in items] == ["private prompt"]
    assert state_path.stat().st_mode & 0o777 == 0o600
    assert restored.get("_prompt_queue_restore_error") is not True


def test_authoritative_reload_prevents_revision_aba_across_session_mirrors():
    """A stale representation cannot resurrect work after empty→ready reuse."""

    session_key = "revision-aba"
    stale = _session(session_key=session_key)
    assert server._enqueue_prompt(stale, "obsolete", "old-ws")["accepted"]

    resetter = _session(session_key=session_key)
    with server._prompt_queue_lock(resetter):
        assert [
            item["text"] for item in server._queued_prompt_items_locked(resetter)
        ] == ["obsolete"]
        server._store_queued_prompt_items_locked(resetter, [])

    fresh = _session(session_key=session_key)
    assert server._enqueue_prompt(fresh, "fresh", "new-ws")["accepted"]
    assert server._enqueue_prompt(stale, "after-refresh", "stale-ws")["accepted"]

    restored = _session(session_key=session_key, transport="restored")
    with server._prompt_queue_lock(restored):
        items = server._queued_prompt_items_locked(restored)
    assert [item["text"] for item in items] == ["fresh", "after-refresh"]


def test_busy_smart_independent_does_not_claim_worker_before_receipt(
    monkeypatch,
    caplog,
):
    caplog.set_level(logging.INFO, logger="tui_gateway.server")
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "smart")
    agent = _AckingSteerAgent()
    session = _session(agent=agent, running=True, inflight_turn={"user": "Build API"})
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (_smart_decision("independent"), "research market"),
    )

    resp = server._handle_busy_submit("r1", "sid", session, "research market", "ws-1")

    assert resp["result"]["status"] == "smart_related"
    assert resp["result"]["route"] == "related"
    assert "incorporada" in resp["result"]["ack"].lower()
    assert "SMART ORCHESTRATOR" in agent.steered[0]
    assert "delegate_task" in agent.steered[0]
    assert "research market" in agent.steered[0]
    assert session.get("queued_prompt") is None
    telemetry = "\n".join(record.getMessage() for record in caplog.records)
    assert "smart_route surface=tui" in telemetry
    assert "accepted=True" in telemetry
    assert "interrupt=false" in telemetry
    for private_value in (
        "research market",
        "session-key",
        "mission=",
        "confidence",
        "0.950",
        "reason-independent",
    ):
        assert private_value not in telemetry


def test_busy_smart_related_crash_window_preserves_uncertain_steer(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "smart")

    def crash_after_fence(_text):
        state_path = server._prompt_queue_state_path(session)
        assert state_path is not None
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        assert payload["steer_claim"]["text"] == "add tests"
        raise KeyboardInterrupt("synthetic crash")

    agent = _AckingSteerAgent(
        steer_effect=crash_after_fence,
        messages=[{"role": "user", "content": "Fix gateway"}],
    )
    session = _session(
        agent=agent,
        running=True,
        inflight_turn={"user": "Fix gateway", "assistant": "", "streaming": True},
    )
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (_smart_decision("related"), "add tests"),
    )

    with pytest.raises(KeyboardInterrupt, match="synthetic crash"):
        server._handle_busy_submit("r1", "sid", session, "add tests", "ws-1")

    restored = _session()
    restored["session_key"] = session["session_key"]
    with server._prompt_queue_lock(restored):
        assert server._queued_prompt_items_locked(restored) == []
    assert restored["_prompt_queue_restore_error"] is True
    assert restored["_smart_steer_uncertain_claim"]["text"] == "add tests"

    with server._prompt_queue_lock(restored):
        server._store_queued_prompt_items_locked(restored, [], steer_claim=None)


def test_busy_smart_related_keeps_durable_claim_until_consumption_ack(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "smart")

    class AckingAgent:
        messages = [{"role": "user", "content": "Fix gateway"}]

        def __init__(self):
            self.consumed = None
            self.unconsumed = None
            self.uncertain = None

        def get_activity_summary(self):
            return {"current_tool": "terminal"}

        def get_steer_generation(self):
            return 41

        def supports_steer_consumption_ack(self):
            return True

        def steer(
            self,
            _text,
            *,
            run_generation=None,
            on_consumed=None,
            on_unconsumed=None,
            on_uncertain=None,
        ):
            self.run_generation = run_generation
            self.consumed = on_consumed
            self.unconsumed = on_unconsumed
            self.uncertain = on_uncertain
            return True

    agent = AckingAgent()
    session = _session(
        agent=agent,
        running=True,
        turn_generation=7,
        inflight_turn={
            "generation": 7,
            "user": "Fix gateway",
            "assistant": "",
            "streaming": True,
        },
    )
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (_smart_decision("related"), "add tests"),
    )

    resp = server._handle_busy_submit("ack-rid", "sid", session, "add tests", "ws")

    assert resp["result"]["status"] == "smart_related"
    assert agent.run_generation == 41
    assert callable(agent.consumed)
    assert callable(agent.unconsumed)
    state_path = server._prompt_queue_state_path(session)
    assert state_path is not None
    transferring = json.loads(state_path.read_text(encoding="utf-8"))
    assert transferring["steer_claim"]["text"] == "add tests"

    agent.consumed()

    committed = json.loads(state_path.read_text(encoding="utf-8"))
    assert committed["steer_claim"] is None
    assert committed["items"] == []


def test_busy_smart_uncertain_callback_quarantines_claim_and_blocks_successor(
    monkeypatch,
):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "smart")
    agent = _AckingSteerAgent(
        messages=[{"role": "user", "content": "Fix gateway"}],
        activity={"current_tool": "terminal"},
    )
    session = _session(
        agent=agent,
        running=True,
        turn_generation=7,
        inflight_turn={
            "generation": 7,
            "user": "Fix gateway",
            "assistant": "",
            "streaming": True,
        },
    )
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (_smart_decision("related"), "add tests"),
    )

    response = server._handle_busy_submit(
        "uncertain-rid", "sid", session, "add tests", "ws"
    )
    assert response["result"]["status"] == "smart_related"
    assert callable(agent.on_uncertain)
    assert server._enqueue_prompt(session, "successor", "ws")["accepted"] is True

    agent.on_uncertain()

    assert session["_prompt_queue_restore_error"] is True
    assert session["_smart_steer_uncertain_claim"]["text"] == "add tests"
    session["running"] = False
    fired = []
    monkeypatch.setattr(
        server,
        "_run_prompt_submit",
        lambda *_args, **_kwargs: fired.append("started"),
    )
    assert server._drain_queued_prompt("next", "sid", session) is False
    assert fired == []
    assert session["queued_prompt"]["text"] == "successor"


def test_dispatch_claim_reserves_rollback_capacity_and_preserves_fifo(monkeypatch):
    session = _session()
    for index in range(server._TUI_BUSY_QUEUE_MAX_PENDING):
        assert server._enqueue_prompt(session, f"queued-{index}", "ws")["accepted"]

    late_receipts = []

    def fail_after_concurrent_arrival(*_args, **_kwargs):
        late_receipts.append(server._enqueue_prompt(session, "late", "late-ws"))
        raise RuntimeError("simulated dispatch admission failure")

    monkeypatch.setattr(server, "_run_prompt_submit", fail_after_concurrent_arrival)

    assert server._drain_queued_prompt("rid", "sid", session) is True

    assert late_receipts[0]["accepted"] is False
    with server._prompt_queue_lock(session):
        queued = server._queued_prompt_items_locked(session)
    assert len(queued) == server._TUI_BUSY_QUEUE_MAX_PENDING
    assert [item["text"] for item in queued] == [
        f"queued-{index}" for index in range(server._TUI_BUSY_QUEUE_MAX_PENDING)
    ]


def test_drain_async_launcher_keeps_claim_until_worker_terminal(monkeypatch):
    """Returning from the thread launcher is not a terminal disposition."""

    session = _session(session_key="async-launch-claim")
    launched: list[str] = []
    monkeypatch.setattr(
        server,
        "_run_prompt_submit",
        lambda _rid, _sid, _session, text, **_kwargs: launched.append(text),
    )
    assert server._enqueue_prompt(session, "accepted async", "ws")["accepted"]

    assert server._drain_queued_prompt("rid", "sid", session) is True
    assert launched == ["accepted async"]

    state_path = server._prompt_queue_state_path(session)
    assert state_path is not None and state_path.exists()
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["claim"]["text"] == "accepted async"
    assert payload["items"] == []

    recovered = _session(session_key="async-launch-claim", transport="new-ws")
    with server._prompt_queue_lock(recovered):
        assert server._queued_prompt_items_locked(recovered) == []
    assert recovered["_prompt_queue_uncertain_claim"]["text"] == "accepted async"
    assert recovered["_prompt_queue_restore_error"] is True


def test_drain_preserves_accepted_envelope_past_legacy_ttl_setting(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(server.time, "time", lambda: clock[0])
    # Legacy configs may still carry this key; accepted receipts must not expire.
    monkeypatch.setattr(
        server,
        "_load_tui_busy_queue_config",
        lambda: {"max_bytes": 1024 * 1024, "ttl_seconds": 10.0},
    )
    dispatched = []
    monkeypatch.setattr(
        server,
        "_run_prompt_submit",
        lambda *_args, **_kwargs: dispatched.append(_args[3]),
    )
    session = _session()
    assert server._enqueue_prompt(session, "accepted", "ws-old")["accepted"] is True

    clock[0] = 111.0

    assert server._drain_queued_prompt("drain-accepted", "sid", session) is True
    assert dispatched == ["accepted"]
    assert session.get("queued_prompt") is None
    assert session.get("queued_prompt_overflow") is None
    assert session["queued_prompt_bytes"] == 0
    assert session["running"] is True


def test_explicit_attachment_batch_never_consumes_transport_staging():
    session = _session()
    session["attached_images_by_owner"] = {
        "batch:composer-a": ["/tmp/a.png"],
        "transport:shared": ["/tmp/b.png"],
    }

    with session["history_lock"]:
        envelope = server._capture_prompt_envelope_locked(
            session,
            "submit-a",
            "matching A",
            None,
            attachment_owner="batch:composer-a",
            attachment_fallback_owner="transport:shared",
        )

    assert envelope["images"] == ["/tmp/a.png"]
    assert session["attached_images_by_owner"] == {
        "transport:shared": ["/tmp/b.png"]
    }


def test_failed_empty_state_unlink_cannot_resurrect_drained_prompt(
    monkeypatch,
):
    first_session = _session(session_key="drained-session")
    assert server._enqueue_prompt(first_session, "run once", "old-ws")["accepted"]
    state_path = server._prompt_queue_state_path(first_session)
    assert state_path is not None and state_path.exists()
    original_unlink = Path.unlink

    def fail_only_final_unlink(path, *args, **kwargs):
        if path == state_path:
            raise OSError("simulated unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_only_final_unlink)
    with server._prompt_queue_lock(first_session):
        server._store_queued_prompt_items_locked(first_session, [])

    recovered = _session(session_key="drained-session", transport="new-ws")
    with server._prompt_queue_lock(recovered):
        restored = server._queued_prompt_items_locked(recovered)

    assert restored == []


def test_initial_prompt_is_durable_and_requeued_when_agent_init_fails(monkeypatch):
    session = _session(agent=None, running=False)
    session["agent"] = None
    session["session_key"] = "initial-durable"
    server._sessions["initial-durable"] = session
    emitted = []

    class ImmediateThread:
        def __init__(self, *, target, daemon):
            self._target = target

        def start(self):
            self._target()

        def is_alive(self):
            return False

    monkeypatch.setattr(server.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(server, "_start_agent_build", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        server,
        "_wait_agent_for_prompt",
        lambda *_args, **_kwargs: {"error": {"message": "synthetic init failure"}},
    )
    monkeypatch.setattr(
        server,
        "_emit",
        lambda event, sid, payload=None: emitted.append((event, sid, payload)),
    )

    try:
        response = server.handle_request(
            {
                "id": "initial-1",
                "method": "prompt.submit",
                "params": {"session_id": "initial-durable", "text": "preserve me"},
            }
        )

        assert response["result"]["status"] == "streaming"
        assert session["running"] is False
        assert session["queued_prompt"]["text"] == "preserve me"
        state_path = server._prompt_queue_state_path(session)
        assert state_path is not None and state_path.exists()
        assert emitted[-1][0] == "error"
    finally:
        server._sessions.pop("initial-durable", None)
        with server._prompt_queue_lock(session):
            server._store_queued_prompt_items_locked(session, [])


def test_late_accepted_steer_is_restaged_ahead_of_dependent_fifo():
    session = _session(running=True)
    assert server._enqueue_prompt(session, "dependent-1", "ws-1")["accepted"]
    assert server._enqueue_prompt(session, "dependent-2", "ws-2")["accepted"]

    receipt = server._prioritize_tui_leftover_steer(session, "related-late")

    assert receipt["accepted"] is True
    assert session["queued_prompt"]["text"] == "related-late"
    assert session["queued_prompt"]["images"] == []
    assert session["queued_prompt"]["metadata"]["arrival_ordinal"] == 0
    assert [item["text"] for item in session["queued_prompt_overflow"]] == [
        "dependent-1",
        "dependent-2",
    ]


def test_oversized_persisted_queue_is_rejected_whole_without_truncation():
    session = _session(session_key="oversized-state")
    state_path = server._prompt_queue_state_path(session)
    assert state_path is not None
    state_path.parent.mkdir(parents=True, exist_ok=True)
    raw_items = [
        {
            "text": f"accepted-{index}",
            "images": [],
            "metadata": {
                "arrival_ordinal": index + 1,
                "session_key": "oversized-state",
            },
        }
        for index in range(server._TUI_BUSY_QUEUE_MAX_PENDING + 1)
    ]
    state_path.write_text(
        json.dumps({"version": 1, "items": raw_items}),
        encoding="utf-8",
    )

    with server._prompt_queue_lock(session):
        restored = server._queued_prompt_items_locked(session)

    assert restored == []
    assert session.get("_prompt_queue_restore_error") is True
    original_state = state_path.read_bytes()

    receipt = server._enqueue_prompt(session, "must not overwrite recovery", "ws")

    assert receipt["accepted"] is False
    assert receipt["reason"] == "recovery_error"
    assert state_path.read_bytes() == original_state


def test_persistence_failure_rejects_before_mutating_in_memory_queue(monkeypatch):
    session = _session()

    def fail_persist(*_args, **_kwargs):
        raise OSError("simulated durable admission failure")

    monkeypatch.setattr(server, "_persist_prompt_items_locked", fail_persist)

    receipt = server._enqueue_prompt(session, "must not be acknowledged", "ws")

    assert receipt["accepted"] is False
    assert receipt["reason"] == "persistence"
    assert session.get("queued_prompt") is None
    assert session.get("queued_prompt_overflow") is None


def test_process_crash_during_claim_preserves_uncertain_obligation(monkeypatch):
    session = _session(session_key="claim-crash")
    assert server._enqueue_prompt(session, "accepted once", "ws")["accepted"]

    def crash_process(*_args, **_kwargs):
        raise KeyboardInterrupt("simulated abrupt process loss")

    monkeypatch.setattr(server, "_run_prompt_submit", crash_process)

    with pytest.raises(KeyboardInterrupt):
        server._drain_queued_prompt("rid", "sid", session)

    recovered = _session(session_key="claim-crash", transport="new-ws")
    with server._prompt_queue_lock(recovered):
        queued = server._queued_prompt_items_locked(recovered)

    assert queued == []
    assert recovered.get("_prompt_queue_uncertain_claim") is not None
    assert recovered.get("_prompt_queue_restore_error") is True


@pytest.mark.skipif(os.name == "nt", reason="fcntl.flock is POSIX-only")
def test_prompt_queue_ipc_lock_serializes_authoritative_rmw_across_processes(
    tmp_path,
):
    """Two process-local session mirrors cannot publish stale queue candidates."""

    ctx = multiprocessing.get_context("spawn")
    loaded = ctx.Event()
    competing_started = ctx.Event()
    competing_finished = ctx.Event()
    allow_stale_store = ctx.Event()
    results = ctx.Queue()
    session_key = "ipc-authoritative-rmw"

    stale_writer = ctx.Process(
        target=_ipc_stale_prompt_queue_writer,
        args=(
            str(tmp_path),
            session_key,
            loaded,
            allow_stale_store,
            results,
        ),
    )
    competing_writer = ctx.Process(
        target=_ipc_competing_prompt_queue_writer,
        args=(
            str(tmp_path),
            session_key,
            loaded,
            competing_started,
            competing_finished,
            results,
        ),
    )
    stale_writer.start()
    competing_writer.start()
    try:
        assert loaded.wait(timeout=5), "stale writer never loaded queue state"
        assert competing_started.wait(timeout=5), "competing writer never attempted admission"
        was_serialized = not competing_finished.wait(timeout=0.5)
        allow_stale_store.set()
        stale_writer.join(timeout=10)
        competing_writer.join(timeout=10)
    finally:
        allow_stale_store.set()
        for process in (stale_writer, competing_writer):
            if process.is_alive():
                process.terminate()
            process.join(timeout=2)

    child_results = dict(results.get(timeout=2) for _ in range(2))
    assert child_results == {"a": "ok", "b": "ok"}
    assert stale_writer.exitcode == 0
    assert competing_writer.exitcode == 0
    assert was_serialized, "process-local keyed locks did not block the second writer"

    restored = _session(session_key=session_key, transport="restored")
    with server._prompt_queue_lock(restored):
        items = server._queued_prompt_items_locked(restored)
    assert [item["text"] for item in items] == ["from-a", "from-b"]


def test_prompt_queue_restore_logs_no_session_correlator(caplog):
    session_key = "customer-account-session-key"
    first = _session(session_key=session_key)
    assert server._enqueue_prompt(first, "durable private prompt", "ws")["accepted"]
    state_path = server._prompt_queue_state_path(first)
    assert state_path is not None

    restored = _session(session_key=session_key)
    with caplog.at_level(logging.INFO, logger="tui_gateway.server"):
        with server._prompt_queue_lock(restored):
            server._queued_prompt_items_locked(restored)

    log_text = caplog.text
    assert session_key not in log_text
    assert state_path.stem[:10] not in log_text
    assert "durable private prompt" not in log_text


def test_prompt_queue_state_path_uses_session_profile_home(tmp_path):
    profile_home = tmp_path / "profile-b"
    session = _session(session_key="tenant-b", profile_home=str(profile_home))

    path = server._prompt_queue_state_path(session)

    assert path is not None
    assert path.is_relative_to(profile_home)


def test_public_detect_drop_reservation_never_leaks_into_unrelated_submit(
    monkeypatch,
    tmp_path,
):
    """A detected image is consumed only by the submit carrying its token."""

    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "queue")
    monkeypatch.setattr(server._pool, "submit", lambda fn: fn())
    image = tmp_path / "private.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    client = _RecordingTransport()
    session = _session(running=True)
    server._sessions["media-owner"] = session

    try:
        detected = server.dispatch(
            {
                "id": "detect-a",
                "method": "input.detect_drop",
                "params": {"session_id": "media-owner", "text": str(image)},
            },
            client,
        )
        assert detected is not None
        reservation = detected["result"].get("attachment_batch_id")

        assert isinstance(reservation, str) and reservation

        assert server.dispatch(
            {
                "id": "submit-b",
                "method": "prompt.submit",
                "params": {"session_id": "media-owner", "text": "unrelated B"},
            },
            client,
        ) is None
        assert client.writes[-1]["result"]["accepted"] is True

        assert server.dispatch(
            {
                "id": "submit-a",
                "method": "prompt.submit",
                "params": {
                    "session_id": "media-owner",
                    "text": "matching A",
                    "attachment_batch_id": reservation,
                },
            },
            client,
        ) is None
        assert client.writes[-1]["result"]["accepted"] is True

        queued = [session["queued_prompt"], *session["queued_prompt_overflow"]]
        assert queued[0]["text"] == "unrelated B"
        assert queued[0]["images"] == []
        assert queued[1]["text"] == "matching A"
        assert len(queued[1]["images"]) == 1
        stored = Path(queued[1]["images"][0])
        assert stored != image
        assert stored.read_bytes() == image.read_bytes()
    finally:
        server._sessions.pop("media-owner", None)


def test_public_overflow_rejection_keeps_media_reserved_from_unrelated_submit(
    monkeypatch,
    tmp_path,
):
    """Rejected media stays on its reservation instead of transport staging."""

    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "queue")
    monkeypatch.setattr(server._pool, "submit", lambda fn: fn())
    monkeypatch.setattr(server, "_TUI_BUSY_QUEUE_MAX_PENDING", 1)
    image = tmp_path / "private.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    client = _RecordingTransport()
    session = _session(running=True)
    server._sessions["overflow-owner"] = session

    try:
        assert server._enqueue_prompt(session, "already queued", client)["accepted"]
        detected = server.dispatch(
            {
                "id": "detect-private",
                "method": "input.detect_drop",
                "params": {"session_id": "overflow-owner", "text": str(image)},
            },
            client,
        )
        assert detected is not None
        reservation = detected["result"]["attachment_batch_id"]

        assert server.dispatch(
            {
                "id": "reject-private",
                "method": "prompt.submit",
                "params": {
                    "session_id": "overflow-owner",
                    "text": "private A",
                    "attachment_batch_id": reservation,
                },
            },
            client,
        ) is None
        assert client.writes[-1]["result"]["status"] == "queue_rejected"
        assert client.writes[-1]["result"]["accepted"] is False

        with server._prompt_queue_lock(session):
            server._store_queued_prompt_items_locked(session, [])

        assert server.dispatch(
            {
                "id": "submit-unrelated",
                "method": "prompt.submit",
                "params": {
                    "session_id": "overflow-owner",
                    "text": "unrelated B",
                },
            },
            client,
        ) is None
        assert session["queued_prompt"]["images"] == []

        with server._prompt_queue_lock(session):
            server._store_queued_prompt_items_locked(session, [])

        assert server.dispatch(
            {
                "id": "retry-private",
                "method": "prompt.submit",
                "params": {
                    "session_id": "overflow-owner",
                    "text": "retry private A",
                    "attachment_batch_id": reservation,
                },
            },
            client,
        ) is None
        images = session["queued_prompt"]["images"]
        assert len(images) == 1
        assert Path(images[0]).read_bytes() == image.read_bytes()
    finally:
        server._sessions.pop("overflow-owner", None)


def test_stale_compute_host_callback_cannot_commit_or_drain_successor(monkeypatch):
    session_key = "stale-compute-host-callback"
    stale = _session(session_key=session_key, running=True)
    assert server._enqueue_prompt(stale, "old", "old-ws")["accepted"]
    with server._prompt_queue_lock(stale):
        old = server._queued_prompt_items_locked(stale)[0]
        server._store_queued_prompt_items_locked(stale, [], claim=old)

    resetter = _session(session_key=session_key)
    with server._prompt_queue_lock(resetter):
        server._queued_prompt_items_locked(resetter)
        server._store_queued_prompt_items_locked(resetter, [], claim=None)

    fresh = _session(session_key=session_key)
    assert server._enqueue_prompt(fresh, "fresh", "new-ws")["accepted"]
    with server._prompt_queue_lock(fresh):
        fresh_claim = server._queued_prompt_items_locked(fresh)[0]
        server._store_queued_prompt_items_locked(fresh, [], claim=fresh_claim)

    drained = []
    monkeypatch.setattr(server, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_session_info", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        server,
        "_drain_queued_prompt",
        lambda *_args, **_kwargs: drained.append(True),
    )

    server._on_compute_host_turn_done(
        "old-request",
        "sid",
        stale,
        {"type": "turn.end", "request_id": "old-request"},
        expected=old,
    )

    recovered = _session(session_key=session_key)
    with server._prompt_queue_lock(recovered):
        server._queued_prompt_items_locked(recovered)
        current = recovered.get("_queued_prompt_claim")
    assert isinstance(current, dict)
    assert current["text"] == "fresh"
    assert drained == []


def test_stale_terminal_callback_cannot_commit_a_newer_durable_claim():
    """Expected-claim CAS is checked after authoritative reload, not before it."""

    session_key = "stale-terminal-claim"
    stale = _session(session_key=session_key)
    assert server._enqueue_prompt(stale, "old", "old-ws")["accepted"]
    with server._prompt_queue_lock(stale):
        old = server._queued_prompt_items_locked(stale)[0]
        server._store_queued_prompt_items_locked(stale, [], claim=old)

    resetter = _session(session_key=session_key)
    with server._prompt_queue_lock(resetter):
        server._queued_prompt_items_locked(resetter)
        server._store_queued_prompt_items_locked(resetter, [], claim=None)

    fresh = _session(session_key=session_key)
    assert server._enqueue_prompt(fresh, "fresh", "new-ws")["accepted"]
    with server._prompt_queue_lock(fresh):
        fresh_claim = server._queued_prompt_items_locked(fresh)[0]
        server._store_queued_prompt_items_locked(fresh, [], claim=fresh_claim)

    assert server._complete_queued_prompt_claim(stale, expected=old) is False

    recovered = _session(session_key=session_key, transport="recovered-ws")
    with server._prompt_queue_lock(recovered):
        assert server._queued_prompt_items_locked(recovered) == []
    assert recovered["_prompt_queue_uncertain_claim"]["text"] == "fresh"


def test_tui_queued_prompts_are_fifo_and_never_replace_each_other(monkeypatch):
    fired = []
    monkeypatch.setattr(
        server,
        "_run_prompt_submit",
        lambda rid, sid, session, text, **_kwargs: fired.append(text),
    )
    session = _session(running=True)

    server._enqueue_prompt(session, "first", "ws-1")
    server._enqueue_prompt(session, "second", "ws-2")
    server._enqueue_prompt(session, "third", "ws-3")

    assert session["queued_prompt"]["text"] == "first"
    assert [item["text"] for item in session["queued_prompt_overflow"]] == [
        "second",
        "third",
    ]

    session["running"] = False
    assert server._drain_queued_prompt("r1", "sid", session) is True
    assert fired == ["first"]
    assert session["queued_prompt"]["text"] == "second"

    assert server._complete_queued_prompt_claim(session) is True
    session["running"] = False
    assert server._drain_queued_prompt("r2", "sid", session) is True
    assert fired == ["first", "second"]
    assert session["queued_prompt"]["text"] == "third"


def test_two_session_objects_cannot_dispatch_same_durable_item(monkeypatch):
    """Independent parent/child mirrors share one durable dispatch fence."""

    parent = _session(session_key="shared-dispatch")
    child = _session(session_key="shared-dispatch")
    dispatched: list[tuple[str, str]] = []

    def launch(_rid, _sid, session, text, **_kwargs):
        owner = "parent" if session is parent else "child"
        dispatched.append((owner, text))

    monkeypatch.setattr(server, "_run_prompt_submit", launch)
    assert server._enqueue_prompt(parent, "run exactly once", "ws")["accepted"]

    assert server._drain_queued_prompt("child-rid", "child-sid", child) is True
    assert server._drain_queued_prompt("parent-rid", "parent-sid", parent) is False
    assert dispatched == [("child", "run exactly once")]


def test_unknown_busy_input_mode_fails_closed_to_queue(monkeypatch):
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {"display": {"busy_input_mode": "bogus"}},
    )

    assert server._load_busy_input_mode() == "queue"
