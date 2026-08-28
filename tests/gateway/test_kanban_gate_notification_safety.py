"""Gate notifications must be SAFE and TRUTHFUL on every delivery path.

Commit 8 proved gate events never wake an agent. This proves the other two
halves of the same contract:

* every event-derived value that leaves Hermes is redacted, path-safe,
  control-character-free and bounded — not just free-text ``reason``;
* a subscription's cursor never records a delivery that did not happen.

Synthetic markers only. No real credential appears in this file.
"""

import asyncio

import pytest

from gateway import kanban_watchers as kw
from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_gate_events as ge

# Shaped like a GitLab PAT, but not one. Followed by a local path and a long
# tail so one marker exercises redaction, path-stripping and bounding at once.
FAKE_SECRET = "glpat-NOTAREALKEY-ABCDEFGHIJKLMNOP"
FAKE_PATH = "/Users/example/private/config"
HOSTILE = f"{FAKE_SECRET} {FAKE_PATH} \x1b[31mred\x1b[0m\nsecond\tline " + "L" * 400

GATE_KINDS = list(ge.PLAN_GATE_NOTIFY_KINDS)


class PushAdapter:
    supports_async_delivery = True

    def __init__(self, fail_send=False):
        self.sent = []
        self.handled = []
        self.fail_send = fail_send

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text})
        if self.fail_send:
            return SendResult(success=False)
        return SendResult(success=True)

    async def handle_message(self, event):
        self.handled.append(event)


class NonPushAdapter(PushAdapter):
    """Shaped like ApiServerAdapter: send() can never succeed."""

    supports_async_delivery = False

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text})
        return SendResult(success=False)


def _runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


async def _one_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _tick(monkeypatch, adapter):
    asyncio.run(_one_tick(monkeypatch, _runner(adapter)))


def _subscribed_task(mode="notify+wake", *, chat_id="chat-1", title="gate root"):
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title=title, assignee="pm",
            session_id="agent:main:telegram:thread:chat-1:topic-7",
        )
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id=chat_id,
            thread_id="topic-7", chat_type="thread", delivery_mode=mode,
            delivery_metadata={"thread_id": "topic-7", "chat_type": "thread"},
        )
        return tid
    finally:
        conn.close()


def _emit(tid, kind, payload):
    conn = kb.connect()
    try:
        kb._append_event(conn, tid, kind=kind, payload=payload)
    finally:
        conn.close()


def _unseen(tid, kinds):
    conn = kb.connect()
    try:
        _cursor, events = kb.unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            thread_id="topic-7", kinds=list(kinds),
        )
        return events
    finally:
        conn.close()


def _assert_safe(text):
    assert FAKE_SECRET not in text, "credential-shaped marker delivered"
    assert "NOTAREALKEY" not in text, "credential body delivered"
    assert FAKE_PATH not in text, "local path delivered"
    assert "\x1b" not in text, "ANSI escape delivered"
    assert "\n" not in text and "\t" not in text, "control whitespace delivered"
    # The 400-char tail must be cut. Individual fields keep their own
    # field-appropriate bounds (64 for identifiers, 160 for a reason), so a
    # legitimate run shorter than that may survive — the whole message may not.
    assert "L" * 300 not in text, "unbounded value delivered"
    assert len(text) < 600, f"message not bounded: {len(text)}"


# ---------------------------------------------------------------------------
# Every gate field is sanitized — through the real notifier
# ---------------------------------------------------------------------------

HOSTILE_FIELD_CASES = [
    ("plan_awaiting_approval", "project_id"),
    ("plan_awaiting_approval", "revision"),
    ("plan_awaiting_approval", "reason"),
    ("plan_approved", "operator"),
    ("plan_approved", "landing_status"),
    ("plan_approved", "project_id"),
    ("plan_rejected", "operator"),
    ("plan_rejected", "reason"),
    ("gate_release_refused", "via"),
    ("gate_release_refused", "gate_state"),
    ("gate_release_refused", "reason"),
]


@pytest.mark.parametrize("kind,field", HOSTILE_FIELD_CASES)
def test_every_gate_field_is_sanitized_through_the_notifier(
    tmp_path, monkeypatch, kind, field
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / f"{kind}-{field}.db"))
    kb.init_db()
    tid = _subscribed_task()
    _emit(tid, kind, {field: HOSTILE})
    adapter = PushAdapter()
    _tick(monkeypatch, adapter)
    assert len(adapter.sent) == 1, f"{kind}.{field} produced no notification"
    _assert_safe(adapter.sent[0]["text"])
    assert adapter.handled == []


MALFORMED_PAYLOADS = [
    None, {}, {"project_id": None, "revision": None},
    {"project_id": ["a", "b"], "operator": {"k": "v"}},
    {"revision": {"nested": {"deep": 1}}, "via": 12345, "gate_state": 3.5},
    {"operator": True, "landing_status": []},
]


@pytest.mark.parametrize("kind", GATE_KINDS)
@pytest.mark.parametrize("payload", MALFORMED_PAYLOADS)
def test_malformed_and_legacy_payloads_never_crash_delivery(
    tmp_path, monkeypatch, kind, payload
):
    key = f"{kind}-{abs(hash(repr(payload)))}"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / f"{key}.db"))
    kb.init_db()
    tid = _subscribed_task()
    _emit(tid, kind, payload)
    adapter = PushAdapter()
    _tick(monkeypatch, adapter)
    assert len(adapter.sent) == 1
    assert tid in adapter.sent[0]["text"]
    assert adapter.handled == []


def test_a_value_whose_str_raises_is_reported_without_echoing_it(tmp_path):
    class Exploding:
        def __str__(self):
            raise RuntimeError(FAKE_SECRET)

    rendered = ge.safe_display_value(Exploding())
    assert rendered == "[unrenderable]"
    assert FAKE_SECRET not in rendered


def test_redaction_failure_fails_closed(monkeypatch):
    """If redaction is unavailable, nothing may leave."""
    import agent.redact as redact

    def boom(*a, **k):
        raise RuntimeError("redactor down")

    monkeypatch.setattr(redact, "redact_sensitive_text", boom)
    out = ge.safe_display_value(HOSTILE)
    assert out == "[redaction unavailable]"
    assert FAKE_SECRET not in out


# ---------------------------------------------------------------------------
# The real kanban_db emitters
# ---------------------------------------------------------------------------

def test_the_real_park_emitter_delivers_safely(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "park.db"))
    kb.init_db()
    tid = _subscribed_task()
    conn = kb.connect()
    try:
        assert kb.park_for_plan_approval(
            conn, tid, project_id=HOSTILE, revision=7, reason=HOSTILE) is True
    finally:
        conn.close()
    adapter = PushAdapter()
    _tick(monkeypatch, adapter)
    assert len(adapter.sent) == 1
    _assert_safe(adapter.sent[0]["text"])
    assert adapter.handled == []


def test_the_real_refusal_emitter_delivers_safely(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "refuse.db"))
    kb.init_db()
    tid = _subscribed_task()
    conn = kb.connect()
    try:
        kb.record_gate_release_refusal(conn, tid, via=HOSTILE, gate_state="plan")
    finally:
        conn.close()
    adapter = PushAdapter()
    _tick(monkeypatch, adapter)
    assert len(adapter.sent) == 1
    _assert_safe(adapter.sent[0]["text"])
    assert adapter.handled == []


# ---------------------------------------------------------------------------
# Truthful delivery: push and non-push
# ---------------------------------------------------------------------------

def test_push_adapter_notify_wake_delivers_the_gate_text_and_no_turn(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "push.db"))
    kb.init_db()
    tid = _subscribed_task("notify+wake")
    _emit(tid, "plan_awaiting_approval", {"project_id": "p", "revision": 1})
    adapter = PushAdapter()
    _tick(monkeypatch, adapter)
    assert len(adapter.sent) == 1
    assert adapter.handled == []
    assert _unseen(tid, GATE_KINDS) == [], "delivered event must be consumed"


def test_non_push_notify_wake_gate_only_batch_is_not_recorded_as_delivered(
    tmp_path, monkeypatch
):
    """The regression: 0 sends + 0 wakes must not advance the cursor."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "nonpush-nw.db"))
    kb.init_db()
    tid = _subscribed_task("notify+wake")
    _emit(tid, "plan_awaiting_approval", {"project_id": "p", "revision": 1})
    adapter = NonPushAdapter()
    _tick(monkeypatch, adapter)
    assert adapter.handled == [], "a gate event must never wake"
    assert adapter.sent, "the passive send must be attempted, not skipped"
    assert _unseen(tid, GATE_KINDS), "undelivered event was consumed"


def test_non_push_notify_only_gate_batch_also_stays_unseen(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "nonpush-n.db"))
    kb.init_db()
    tid = _subscribed_task("notify")
    _emit(tid, "plan_rejected", {"operator": "op", "project_id": "p",
                                 "revision": 1})
    adapter = NonPushAdapter()
    _tick(monkeypatch, adapter)
    assert adapter.handled == []
    assert _unseen(tid, GATE_KINDS)


def test_non_push_notify_wake_still_skips_the_send_when_a_wake_will_happen(
    tmp_path, monkeypatch
):
    """The pre-existing optimisation must survive: an actionable batch on a
    non-push adapter is delivered by the wake self-post, not by a doomed send."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "nonpush-act.db"))
    kb.init_db()
    tid = _subscribed_task("notify+wake")
    _emit(tid, "completed", {"summary": "done"})

    # A non-push wake is an authenticated HTTP self-post, not handle_message.
    # Record the attempt instead of standing up an API server.
    import gateway.wake as wake_mod
    posted = []

    async def _record(adapter, *, text, session_id="", source=None):
        posted.append({"text": text, "session_id": session_id})

    monkeypatch.setattr(wake_mod, "deliver_wake", _record)

    adapter = NonPushAdapter()
    _tick(monkeypatch, adapter)
    assert adapter.sent == [], "doomed send should still be skipped"
    assert len(posted) == 1, "the wake self-post is the delivery"
    assert _unseen(tid, ["completed"]) == [], "a delivered wake consumes it"


def test_wake_only_gate_batch_is_documented_and_stable(tmp_path, monkeypatch):
    """`wake`-only subscribers asked for no passive text. A gate event produces
    no wake, so nothing is delivered on that subscription — by their own
    choice. The row stays in the global audit log; the cursor advances so a
    later actionable event is not wedged behind it."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "wakeonly.db"))
    kb.init_db()
    tid = _subscribed_task("wake")
    _emit(tid, "plan_awaiting_approval", {"project_id": "p", "revision": 1})
    adapter = PushAdapter()
    _tick(monkeypatch, adapter)
    assert adapter.sent == []
    assert adapter.handled == []
    conn = kb.connect()
    try:
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ?", (tid,)).fetchall()]
    finally:
        conn.close()
    assert "plan_awaiting_approval" in kinds, "audit row must survive"
    # A later actionable event still reaches the agent.
    _emit(tid, "completed", {"summary": "done"})
    adapter2 = PushAdapter()
    _tick(monkeypatch, adapter2)
    assert len(adapter2.handled) == 1


# ---------------------------------------------------------------------------
# Mixed batches, failure, replay, restart
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("gate_first", [True, False])
def test_mixed_batches_wake_once_in_either_order(tmp_path, monkeypatch, gate_first):
    monkeypatch.setenv("HERMES_KANBAN_DB",
                       str(tmp_path / f"mixed-{gate_first}.db"))
    kb.init_db()
    tid = _subscribed_task("notify+wake")
    gate = ("plan_approved", {"operator": "op", "project_id": "p",
                              "revision": 1, "landing_status": "ready"})
    done = ("completed", {"summary": "work finished"})
    for kind, payload in ([gate, done] if gate_first else [done, gate]):
        _emit(tid, kind, payload)
    adapter = PushAdapter()
    _tick(monkeypatch, adapter)
    assert len(adapter.sent) == 2
    assert len(adapter.handled) == 1
    wake_text = adapter.handled[0].text.lower()
    assert "completed" in wake_text
    assert "cannot cross this gate" not in wake_text
    assert "approved by" not in wake_text


def test_send_failure_rewinds_so_a_gate_notification_is_not_lost(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "rewind.db"))
    kb.init_db()
    tid = _subscribed_task("notify")
    _emit(tid, "plan_awaiting_approval", {"project_id": "p", "revision": 1})

    failing = PushAdapter(fail_send=True)
    _tick(monkeypatch, failing)
    assert failing.sent, "a send was attempted"
    assert _unseen(tid, GATE_KINDS), "failed delivery must stay unseen"

    # A gateway restart (fresh runner, fresh counters) still delivers it.
    healthy = PushAdapter()
    _tick(monkeypatch, healthy)
    assert len(healthy.sent) == 1
    assert healthy.handled == []
    assert _unseen(tid, GATE_KINDS) == []


def test_replay_after_delivery_does_not_resend_or_wake(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "replay.db"))
    kb.init_db()
    tid = _subscribed_task("notify+wake")
    _emit(tid, "plan_approved", {"operator": "op", "project_id": "p",
                                 "revision": 1, "landing_status": "ready"})
    first = PushAdapter()
    _tick(monkeypatch, first)
    assert len(first.sent) == 1
    for _ in range(3):
        again = PushAdapter()
        _tick(monkeypatch, again)
        assert again.sent == []
        assert again.handled == []


def test_an_inherited_child_subscription_keeps_its_own_destination(
    tmp_path, monkeypatch
):
    """A gate event must not re-route to the worker session behind the task."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "inherited.db"))
    kb.init_db()
    tid = _subscribed_task("notify+wake", chat_id="origin-chat")
    conn = kb.connect()
    try:
        conn.execute(
            "UPDATE tasks SET session_id = ? WHERE id = ?",
            ("agent:worker:telegram:chat:worker-chat:", tid),
        )
        conn.commit()
    finally:
        conn.close()
    _emit(tid, "plan_awaiting_approval", {"project_id": "p", "revision": 1})
    adapter = PushAdapter()
    _tick(monkeypatch, adapter)
    assert len(adapter.sent) == 1
    assert adapter.sent[0]["chat_id"] == "origin-chat"
    assert adapter.handled == []


def test_a_gated_task_is_untouched_and_gets_no_run_row(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "gated.db"))
    kb.init_db()
    tid = _subscribed_task("notify+wake")
    conn = kb.connect()
    try:
        assert kb.park_for_plan_approval(
            conn, tid, project_id="proj", revision=1) is True
        before = dict(conn.execute(
            "SELECT status, gate_state, current_run_id FROM tasks WHERE id = ?",
            (tid,)).fetchone())
    finally:
        conn.close()
    adapter = PushAdapter()
    for _ in range(3):
        _tick(monkeypatch, adapter)
    conn = kb.connect()
    try:
        after = dict(conn.execute(
            "SELECT status, gate_state, current_run_id FROM tasks WHERE id = ?",
            (tid,)).fetchone())
        runs = conn.execute(
            "SELECT id FROM task_runs WHERE task_id = ?", (tid,)).fetchall()
    finally:
        conn.close()
    assert after == before
    assert runs == []
    assert adapter.handled == []


def test_the_eight_actionable_wake_kinds_are_unchanged(tmp_path, monkeypatch):
    """Each one still produces exactly one passive text and one agent turn."""
    for kind, payload in [
        ("completed", {"summary": "s"}), ("blocked", {"reason": "r"}),
        ("gave_up", {"error": "e"}), ("crashed", {}),
        ("timed_out", {"limit_seconds": 5}),
        ("review_requested", {"reviewer": "r"}),
        ("changes_requested", {"reason": "r"}),
        ("block_loop_detected", {"reason": "r", "recurrences": 3}),
    ]:
        monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / f"act-{kind}.db"))
        kb.init_db()
        tid = _subscribed_task("notify+wake")
        _emit(tid, kind, payload)
        adapter = PushAdapter()
        _tick(monkeypatch, adapter)
        assert len(adapter.sent) == 1, kind
        assert len(adapter.handled) == 1, kind
    assert set(kw.WAKE_KINDS) == {
        "completed", "blocked", "gave_up", "crashed", "timed_out",
        "review_requested", "changes_requested", "block_loop_detected",
    }
