"""Desktop/TUI shows a human gate event and never starts an agent turn (G11).

The Desktop app, the TUI, and the dashboard chat all deliver kanban
notifications through ``tui_gateway/server.py``'s per-session poller. That
poller has two halves: ``_emit("status.update", ...)`` puts a line in front of
the human, and ``_kanban_pending`` → ``_run_prompt_submit()`` makes the agent
take a turn.

A human plan/deploy gate must reach the first and never the second.
"""

import threading
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_gate_events as ge
from tui_gateway import server as tui

SESSION_KEY = "tui-gate-session"
FAKE_SECRET = "glpat-NOTAREALKEY-ABCDEFGHIJKLMNOP"
FAKE_PATH = "/Users/example/private/config"
HOSTILE = f"{FAKE_SECRET} {FAKE_PATH} \x1b[31mred\x1b[0m\nline" + "L" * 400

GATE_PAYLOADS = {
    "plan_awaiting_approval": {"project_id": "proj-1", "revision": 3},
    "plan_approved": {"operator": "op", "project_id": "proj-1",
                      "revision": 3, "landing_status": "ready"},
    "plan_rejected": {"operator": "op", "project_id": "proj-1", "revision": 3},
    "gate_release_refused": {"via": "unblock_task", "gate_state": "plan"},
}


def _session():
    return {"session_key": SESSION_KEY, "history_lock": threading.RLock(),
            "running": False}


def _subscribed_task(title="gate root"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title=title, assignee="pm")
        kb.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=SESSION_KEY)
        return tid
    finally:
        conn.close()


def _emit_event(tid, kind, payload):
    conn = kb.connect()
    try:
        kb._append_event(conn, tid, kind=kind, payload=payload)
    finally:
        conn.close()


def _run_poller_once(monkeypatch, session, *, deadline=6.0):
    """Drive the real poller thread through one kanban poll."""
    displayed, submitted = [], []

    def fake_emit(event, sid, payload=None):
        if event == "status.update" and isinstance(payload, dict):
            displayed.append(payload.get("text") or "")

    def fake_submit(rid, sid, sess, text, **kwargs):
        submitted.append(text)
        with sess["history_lock"]:
            sess["running"] = False

    monkeypatch.setattr(tui, "_emit", fake_emit)
    monkeypatch.setattr(tui, "_run_prompt_submit", fake_submit)
    monkeypatch.setattr(tui, "_maybe_fire_tui_loop_tick", lambda *a, **k: None)

    stop = threading.Event()
    thread = threading.Thread(
        target=tui._notification_poller_loop,
        args=(stop, "sid-1", session), daemon=True,
    )
    thread.start()
    try:
        end = time.monotonic() + deadline
        # One kanban poll happens immediately; give the thread a moment to make
        # both decisions (display, and submit-or-not) before stopping it.
        while time.monotonic() < end and not displayed:
            time.sleep(0.05)
        time.sleep(0.4)
    finally:
        stop.set()
        thread.join(timeout=5)
    return displayed, submitted


# ---------------------------------------------------------------------------
# Vocabulary is shared, not restated
# ---------------------------------------------------------------------------

def test_the_tui_surface_uses_the_canonical_gate_vocabulary():
    assert tui._KANBAN_GATE_KINDS is ge.PLAN_GATE_NOTIFY_KINDS
    for kind in ge.PLAN_GATE_NOTIFY_KINDS:
        assert kind in tui._KANBAN_NOTIFY_KINDS
        assert kind in tui._KANBAN_PASSIVE_KINDS


def test_actionable_kinds_are_not_passive():
    for kind in ("completed", "blocked", "gave_up", "crashed", "timed_out"):
        assert kind not in tui._KANBAN_PASSIVE_KINDS


# ---------------------------------------------------------------------------
# Collection: displayed, but never agent-eligible
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", sorted(GATE_PAYLOADS))
def test_a_gate_event_is_collected_for_display_only(kind):
    tid = _subscribed_task()
    _emit_event(tid, kind, GATE_PAYLOADS[kind])
    display, agent = tui._collect_kanban_notifications(_session())
    assert len(display) == 1, f"{kind} not displayed"
    assert tid in display[0]
    assert agent == [], f"{kind} became agent-eligible"


@pytest.mark.parametrize("kind", sorted(GATE_PAYLOADS))
def test_hostile_gate_payloads_are_sanitized_for_the_ui(kind):
    tid = _subscribed_task()
    payload = {k: HOSTILE for k in GATE_PAYLOADS[kind]}
    payload["reason"] = HOSTILE
    _emit_event(tid, kind, payload)
    display, agent = tui._collect_kanban_notifications(_session())
    assert len(display) == 1
    text = display[0]
    assert FAKE_SECRET not in text and "NOTAREALKEY" not in text
    assert FAKE_PATH not in text
    assert "\x1b" not in text
    assert "\n" not in text and "\t" not in text
    assert "L" * 300 not in text
    assert len(text) < 600
    assert agent == []


def test_an_actionable_event_stays_agent_eligible():
    tid = _subscribed_task()
    conn = kb.connect()
    try:
        kb.complete_task(conn, tid, summary="shipped")
    finally:
        conn.close()
    display, agent = tui._collect_kanban_notifications(_session())
    assert len(display) == 1
    assert agent == display


def test_a_mixed_batch_displays_both_but_only_one_is_agent_eligible():
    tid = _subscribed_task()
    _emit_event(tid, "plan_approved", GATE_PAYLOADS["plan_approved"])
    conn = kb.connect()
    try:
        kb.complete_task(conn, tid, summary="shipped")
    finally:
        conn.close()
    display, agent = tui._collect_kanban_notifications(_session())
    assert len(display) == 2
    assert len(agent) == 1
    assert "shipped" in agent[0]
    assert all("cannot cross this gate" not in a for a in agent)


# ---------------------------------------------------------------------------
# The real poller thread
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", sorted(GATE_PAYLOADS))
def test_the_poller_displays_a_gate_event_and_never_submits_a_turn(
    monkeypatch, kind
):
    tid = _subscribed_task()
    _emit_event(tid, kind, GATE_PAYLOADS[kind])
    session = _session()
    displayed, submitted = _run_poller_once(monkeypatch, session)
    assert any(tid in d for d in displayed), f"{kind} not shown to the human"
    assert submitted == [], f"{kind} started an agent turn"
    assert not session.get("_kanban_pending")


def test_the_poller_still_submits_an_actionable_event(monkeypatch):
    tid = _subscribed_task()
    conn = kb.connect()
    try:
        kb.complete_task(conn, tid, summary="shipped")
    finally:
        conn.close()
    session = _session()
    displayed, submitted = _run_poller_once(monkeypatch, session)
    assert any(tid in d for d in displayed)
    assert len(submitted) == 1
    assert "shipped" in submitted[0]


def test_the_poller_on_a_mixed_batch_submits_only_the_actionable_text(monkeypatch):
    tid = _subscribed_task()
    _emit_event(tid, "plan_awaiting_approval",
                GATE_PAYLOADS["plan_awaiting_approval"])
    conn = kb.connect()
    try:
        kb.complete_task(conn, tid, summary="shipped")
    finally:
        conn.close()
    session = _session()
    displayed, submitted = _run_poller_once(monkeypatch, session)
    assert len(displayed) == 2
    assert len(submitted) == 1
    assert "shipped" in submitted[0]
    assert "cannot cross this gate" not in submitted[0]


def test_a_gate_event_is_still_auditable_after_the_poller_consumed_it():
    tid = _subscribed_task()
    _emit_event(tid, "plan_rejected", GATE_PAYLOADS["plan_rejected"])
    tui._collect_kanban_notifications(_session())
    conn = kb.connect()
    try:
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ?", (tid,)).fetchall()]
    finally:
        conn.close()
    assert "plan_rejected" in kinds


def test_repeated_gate_events_never_accumulate_an_agent_turn(monkeypatch):
    tid = _subscribed_task()
    session = _session()
    for _ in range(4):
        _emit_event(tid, "plan_awaiting_approval",
                    GATE_PAYLOADS["plan_awaiting_approval"])
        display, agent = tui._collect_kanban_notifications(session)
        assert len(display) == 1
        assert agent == []
    assert not session.get("_kanban_pending")
