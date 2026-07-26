"""A prompt that lands mid-turn is redirected or queued, never dropped.

Before this, ``prompt.submit`` on a running session returned ``session busy``,
forcing clients into a deadline-bounded busy-retry. When turn teardown outlived
the deadline — e.g. a slow, non-interruptible tool (``web_search``) still
running when the user hit stop — the resubmitted message was silently dropped
("it just doesn't listen"). The gateway now applies the ``busy_input_mode``
policy: redirect the live turn by default, with the legacy interrupt + queue
path retained as a compatibility fallback.
"""

import threading
import time
import types

import tools.async_delegation as ad
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
            "request_id": "byte-1",
            "session_key": "session-key",
        },
    }
    second = {
        **first,
        "transport": "ws-2",
        "metadata": {**first["metadata"], "arrival_ordinal": 2, "request_id": "byte-2"},
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
    # Rejected media remains staged for an explicit retry instead of disappearing.
    assert session["attached_images"] == ["/tmp/one.png"]


def test_drain_discards_expired_envelopes_before_dispatch(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(server.time, "time", lambda: clock[0])
    monkeypatch.setattr(
        server,
        "_load_tui_busy_queue_config",
        lambda: {"max_bytes": 1024 * 1024, "ttl_seconds": 10.0},
    )
    monkeypatch.setattr(
        server,
        "_run_prompt_submit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("expired prompt must not dispatch")
        ),
    )
    session = _session()
    assert server._enqueue_prompt(session, "stale", "ws-old")["accepted"] is True

    clock[0] = 111.0

    assert server._drain_queued_prompt("drain-expired", "sid", session) is False
    assert session.get("queued_prompt") is None
    assert session.get("queued_prompt_overflow") is None
    assert session["queued_prompt_bytes"] == 0
    assert session["running"] is False


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

    def capture_dispatch(rid, _sid, captured_session, text, **_kwargs):
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
    agent = types.SimpleNamespace(steer=lambda text: True, interrupt=lambda *a, **k: None)
    session = _session(agent=agent, running=True)

    resp = server._handle_busy_submit("r1", "sid", session, "nudge", "ws-1")

    assert resp["result"]["status"] == "steered"
    assert session.get("queued_prompt") is None






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
    steered = []
    interrupted = []
    agent = types.SimpleNamespace(
        steer=lambda text: steered.append(text) or True,
        interrupt=lambda *a, **k: interrupted.append(True),
        messages=[{"role": "user", "content": "Fix gateway"}],
        get_activity_summary=lambda: {"current_tool": "terminal"},
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
    assert steered == ["add tests"]
    assert interrupted == []
    assert session.get("queued_prompt") is None

def test_busy_smart_independent_injects_parallel_directive(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "smart")
    steered = []
    agent = types.SimpleNamespace(
        steer=lambda text: steered.append(text) or True,
        interrupt=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not interrupt")),
        messages=[],
        get_activity_summary=lambda: {},
    )
    session = _session(agent=agent, running=True, inflight_turn={"user": "Build API"})
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (_smart_decision("independent"), "research market"),
    )

    resp = server._handle_busy_submit("r1", "sid", session, "research market", "ws-1")

    assert resp["result"]["status"] == "smart_parallel"
    assert "SMART ORCHESTRATOR" in steered[0]
    assert "delegate_task" in steered[0]
    assert "research market" in steered[0]
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


