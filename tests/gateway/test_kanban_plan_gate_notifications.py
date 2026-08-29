"""G11 — plan-gate events notify humans and never wake agents.

A human gate exists to stop an agent. Waking an agent because a gate was
reached, crossed, or refused would drive it straight back at the boundary it is
forbidden to cross. These events must therefore be *visible* (claimed,
delivered, auditable) and *inert* (never a wake, never a dispatch, never a
promotion) — no matter how many times they are delivered or replayed.
"""

import asyncio

import pytest

from gateway import kanban_watchers as kw
from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb

# The agent-actionable set as it stood before G11. Pinned so a future edit that
# quietly adds a human-gate kind here has to change this line too.
AGENT_ACTIONABLE = {
    "completed", "gave_up", "crashed", "timed_out", "blocked",
    "review_requested", "changes_requested", "block_loop_detected",
}

GATE_PAYLOADS = {
    "plan_awaiting_approval": {"project_id": "proj-1", "revision": 3,
                               "reason": "needs a human plan decision"},
    "plan_approved": {"operator": "operator@example", "surface": "gateway",
                      "project_id": "proj-1", "revision": 3,
                      "landing_status": "ready", "reason": None},
    "plan_rejected": {"operator": "operator@example", "surface": "gateway",
                      "project_id": "proj-1", "revision": 3,
                      "landing_status": "triage", "reason": "scope too broad"},
    "gate_release_refused": {"via": "unblock_task", "gate_state": "plan"},
}


class RecordingAdapter:
    """`sent` == what a human sees. `handled` == an agent taking a turn."""

    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text,
                          "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)


def _runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


async def _run_one_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _tick(monkeypatch, adapter):
    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))


def _task_with_sub(delivery_mode="notify+wake"):
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="gated project root", assignee="pm",
            session_id="agent:main:telegram:thread:chat-1:topic-7",
        )
        kb.add_notify_sub(
            conn, task_id=task_id, platform="telegram", chat_id="chat-1",
            thread_id="topic-7", chat_type="thread",
            delivery_mode=delivery_mode,
            delivery_metadata={"thread_id": "topic-7", "chat_type": "thread"},
        )
        return task_id
    finally:
        conn.close()


def _emit(task_id, kind, payload=None, times=1):
    conn = kb.connect()
    try:
        for _ in range(times):
            kb._append_event(conn, task_id, kind=kind,
                             payload=payload if payload is not None
                             else GATE_PAYLOADS[kind])
    finally:
        conn.close()


def _task_row(task_id):
    conn = kb.connect()
    try:
        return dict(conn.execute(
            "SELECT status, gate_state, current_run_id FROM tasks WHERE id = ?",
            (task_id,)).fetchone())
    finally:
        conn.close()


def _runs(task_id):
    conn = kb.connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, status FROM task_runs WHERE task_id = ?",
            (task_id,)).fetchall()]
    finally:
        conn.close()


def _event_kinds(task_id):
    conn = kb.connect()
    try:
        return [r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
            (task_id,)).fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Structural invariant
# ---------------------------------------------------------------------------

def test_plan_gate_kinds_are_excluded_from_wake_kinds():
    assert set(kw.PLAN_GATE_NOTIFY_KINDS).isdisjoint(kw.WAKE_KINDS)


def test_every_plan_gate_kind_is_claimed_so_a_human_sees_it():
    for kind in kw.PLAN_GATE_NOTIFY_KINDS:
        assert kind in kw.TERMINAL_KINDS, kind


def test_the_agent_actionable_wake_set_is_unchanged():
    assert set(kw.WAKE_KINDS) == AGENT_ACTIONABLE


def test_wake_kinds_is_derived_not_hand_maintained():
    """Adding a gate kind must remove it from waking, automatically."""
    assert set(kw.WAKE_KINDS) == (
        set(kw.TERMINAL_KINDS) - set(kw.NEVER_WAKE_KINDS)
    )


def test_the_four_gate_kinds_are_the_ones_kanban_db_emits():
    assert set(kw.PLAN_GATE_NOTIFY_KINDS) == {
        "plan_awaiting_approval", "plan_approved",
        "plan_rejected", "gate_release_refused",
    }


# ---------------------------------------------------------------------------
# Behavioral: visible to humans, inert for agents
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", sorted(GATE_PAYLOADS))
def test_gate_event_notifies_a_human_without_waking_an_agent(
    tmp_path, monkeypatch, kind
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / f"{kind}.db"))
    kb.init_db()
    task_id = _task_with_sub("notify+wake")
    _emit(task_id, kind)
    adapter = RecordingAdapter()

    _tick(monkeypatch, adapter)

    assert len(adapter.sent) == 1, f"{kind} produced no human notification"
    text = adapter.sent[0]["text"]
    assert task_id in text
    assert adapter.sent[0]["metadata"]["thread_id"] == "topic-7"
    assert adapter.handled == [], f"{kind} woke an agent"

    # The operator can act on it: the message names what happened and where.
    expected = {
        "plan_awaiting_approval": ("awaiting a human plan decision",
                                   "proj-1 r3", "cannot cross this gate"),
        "plan_approved": ("approved by", "operator@example", "ready"),
        "plan_rejected": ("rejected by", "operator@example", "triage"),
        "gate_release_refused": ("refused an attempt", "plan", "unblock_task"),
    }[kind]
    for fragment in expected:
        assert fragment in text, f"{kind}: {fragment!r} missing from {text!r}"
    # Attestation secrets are never user-facing.
    assert "nonce" not in text.lower()


@pytest.mark.parametrize("kind", sorted(GATE_PAYLOADS))
def test_gate_event_remains_auditable_after_delivery(tmp_path, monkeypatch, kind):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / f"audit-{kind}.db"))
    kb.init_db()
    task_id = _task_with_sub("notify+wake")
    _emit(task_id, kind)
    _tick(monkeypatch, RecordingAdapter())
    assert kind in _event_kinds(task_id)


def test_repeated_gate_notifications_never_wake(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "repeat.db"))
    kb.init_db()
    task_id = _task_with_sub("notify+wake")
    adapter = RecordingAdapter()
    for _ in range(5):
        _emit(task_id, "plan_awaiting_approval")
        _tick(monkeypatch, adapter)
    assert len(adapter.sent) == 5
    assert adapter.handled == []


def test_wake_only_mode_still_sends_nothing_and_wakes_nothing(tmp_path, monkeypatch):
    """`wake`-only subscribers get no passive text — and no wake either."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "wakeonly.db"))
    kb.init_db()
    task_id = _task_with_sub("wake")
    _emit(task_id, "plan_awaiting_approval")
    adapter = RecordingAdapter()
    _tick(monkeypatch, adapter)
    assert adapter.handled == []


# ---------------------------------------------------------------------------
# Agent-actionable behavior is unchanged
# ---------------------------------------------------------------------------

def test_a_blocked_event_still_wakes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "legacy.db"))
    kb.init_db()
    task_id = _task_with_sub("notify+wake")
    _emit(task_id, "blocked", {"reason": "needs input"})
    adapter = RecordingAdapter()
    _tick(monkeypatch, adapter)
    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1


def test_a_mixed_batch_wakes_only_for_the_actionable_event(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "mixed.db"))
    kb.init_db()
    task_id = _task_with_sub("notify+wake")
    _emit(task_id, "plan_approved")
    _emit(task_id, "completed", {"summary": "work finished"})
    adapter = RecordingAdapter()
    _tick(monkeypatch, adapter)
    assert len(adapter.sent) == 2
    assert len(adapter.handled) == 1, "the gate event must not add a second wake"
    wake_text = adapter.handled[0].text.lower()
    assert "completed" in wake_text
    for gate_word in ("plan approved", "plan rejected", "awaiting a human",
                      "cannot cross this gate"):
        assert gate_word not in wake_text, gate_word


# ---------------------------------------------------------------------------
# A gated task cannot be advanced — by anything
# ---------------------------------------------------------------------------

def test_notifier_replay_restart_and_duplicates_cannot_advance_a_gated_task(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "gated.db"))
    kb.init_db()
    task_id = _task_with_sub("notify+wake")
    conn = kb.connect()
    try:
        assert kb.park_for_plan_approval(
            conn, task_id, project_id="proj-1", revision=1,
            reason="human decision required") is True
    finally:
        conn.close()
    before = _task_row(task_id)
    assert before["status"] == "scheduled" and before["gate_state"] == "plan"

    adapter = RecordingAdapter()
    for _ in range(3):                      # duplicate delivery + watcher replay
        _tick(monkeypatch, adapter)
    _tick(monkeypatch, adapter)             # a fresh runner == a gateway restart
    _emit(task_id, "gate_release_refused")  # a refused crossing attempt
    _tick(monkeypatch, adapter)

    assert adapter.handled == [], "a gated task woke an agent"
    assert _task_row(task_id) == before, "a gated task advanced"
    assert _runs(task_id) == [], "a gated task got a run row"


def test_the_dispatcher_never_spawns_a_gated_task(tmp_path, monkeypatch):
    """Cron / restart both reach the board through dispatch_once."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "dispatch.db"))
    kb.init_db()
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _n: True)
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="gated root", assignee="coder")
        assert kb.park_for_plan_approval(
            conn, task_id, project_id="proj-2", revision=1) is True
        before = _task_row(task_id)
        spawned = []
        for _ in range(3):                  # repeated cron ticks
            result = kb.dispatch_once(
                conn, spawn_fn=lambda *a, **k: spawned.append(a) or 4242)
            assert task_id not in [t for t, _a, _p in result.spawned]
        assert spawned == []
        assert _task_row(task_id) == before
        assert _runs(task_id) == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Operator-facing labels
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", sorted(GATE_PAYLOADS))
def test_every_gate_kind_has_an_operator_label(kind):
    from agent.i18n import t
    key = f"gateway.kanban.gate.{kind}"
    label = t(key)
    assert label and label != key, f"missing operator label for {kind}"


def test_the_release_path_refusal_payload_renders_and_stays_inert(
    tmp_path, monkeypatch
):
    """``_audit_gate_refusal`` carries ``via``+``reason`` but no ``gate_state``."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "refusal.db"))
    kb.init_db()
    task_id = _task_with_sub("notify+wake")
    _emit(task_id, "gate_release_refused",
          {"via": "release_plan_gate", "reason": "attestation already used"})
    adapter = RecordingAdapter()
    _tick(monkeypatch, adapter)
    assert len(adapter.sent) == 1
    text = adapter.sent[0]["text"]
    assert "release_plan_gate" in text
    assert "attestation already used" in text
    assert adapter.handled == []


def test_a_legacy_gate_event_with_an_empty_payload_is_inert_not_a_crash(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "legacy-payload.db"))
    kb.init_db()
    task_id = _task_with_sub("notify+wake")
    for kind in kw.PLAN_GATE_NOTIFY_KINDS:
        _emit(task_id, kind, {})
    adapter = RecordingAdapter()
    _tick(monkeypatch, adapter)
    assert len(adapter.sent) == len(kw.PLAN_GATE_NOTIFY_KINDS)
    assert adapter.handled == []
