"""Gate notifications must be SAFE and TRUTHFUL on every delivery path.

Commit 8 proved gate events never wake an agent. This proves the other two
halves of the same contract:

* every event-derived value that leaves Hermes is redacted, path-safe,
  control-character-free and bounded — not just free-text ``reason``;
* a subscription's cursor never records a delivery that did not happen.

Synthetic markers only. No real credential appears in this file.
"""

import asyncio
import json
import time
import unicodedata

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


@pytest.mark.parametrize("mode", ["notify", "notify+wake"])
def test_non_push_gate_batch_is_recorded_not_consumed_and_not_retried(
    tmp_path, monkeypatch, mode
):
    """A non-push adapter has no passive channel and a gate never wakes.

    Attempting the send anyway (the first correction) was still lossy: an
    ApiServerAdapter refuses by design, so twelve doomed attempts trip
    MAX_SEND_FAILURES and DELETE a live subscription. The truthful behaviour is
    to record the non-delivery on the task and leave the subscription alone.
    """
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / f"np-{mode}.db"))
    kb.init_db()
    tid = _subscribed_task(mode)
    _emit(tid, "plan_awaiting_approval", {"project_id": "p", "revision": 1})
    adapter = NonPushAdapter()
    _tick(monkeypatch, adapter)
    assert adapter.handled == [], "a gate event must never wake"
    assert adapter.sent == [], "no send may be attempted on a dead channel"
    rows = _undeliverable_rows(tid)
    assert len(rows) == 1, "the non-delivery must be on the task"
    assert rows[0]["event_kind"] == "plan_awaiting_approval"
    assert _subs(tid), "the subscription must survive"


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


# ---------------------------------------------------------------------------
# Non-push delivery is durable, not lossy (second-round review)
# ---------------------------------------------------------------------------

def _events(tid):
    conn = kb.connect()
    try:
        return [(r["kind"], r["payload"]) for r in conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
            (tid,)).fetchall()]
    finally:
        conn.close()


def _undeliverable_rows(tid):
    return [json.loads(p) for k, p in _events(tid)
            if k == "gate_notification_undeliverable"]


def _subs(tid):
    conn = kb.connect()
    try:
        return kb.list_notify_subs(conn, task_id=tid)
    finally:
        conn.close()


def test_the_undeliverable_audit_kind_is_never_claimed_or_woken():
    """It must not be in TERMINAL_KINDS, or the notifier would recurse."""
    assert "gate_notification_undeliverable" not in kw.TERMINAL_KINDS
    assert "gate_notification_undeliverable" not in kw.WAKE_KINDS


@pytest.mark.parametrize("gate_first", [True, False])
def test_non_push_mixed_batch_records_the_gate_instead_of_consuming_it(
    tmp_path, monkeypatch, gate_first
):
    """The actionable self-post is NOT delivery for the gate half."""
    monkeypatch.setenv("HERMES_KANBAN_DB",
                       str(tmp_path / f"np-mixed-{gate_first}.db"))
    kb.init_db()
    tid = _subscribed_task("notify+wake")

    import gateway.wake as wake_mod
    posted = []

    async def _record(adapter, *, text, session_id="", source=None):
        posted.append(text)

    monkeypatch.setattr(wake_mod, "deliver_wake", _record)

    gate = ("plan_awaiting_approval", {"project_id": "p", "revision": 1})
    done = ("completed", {"summary": "work finished"})
    for kind, payload in ([gate, done] if gate_first else [done, gate]):
        _emit(tid, kind, payload)

    adapter = NonPushAdapter()
    _tick(monkeypatch, adapter)

    assert len(posted) == 1, "the actionable event is still delivered"
    assert "cannot cross this gate" not in posted[0].lower()
    assert "awaiting a human plan decision" not in posted[0].lower()
    assert adapter.sent == [], "no doomed send may be attempted"

    rows = _undeliverable_rows(tid)
    assert len(rows) == 1, "the gate half must be recorded, not consumed"
    assert rows[0]["event_kind"] == "plan_awaiting_approval"
    assert rows[0]["delivery_mode"] == "notify+wake"
    assert "no passive channel" in rows[0]["reason"]


def test_non_push_gate_only_never_exhausts_the_failure_counter(
    tmp_path, monkeypatch
):
    """A structurally unsupported send must not delete a live subscription."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "np-exhaust.db"))
    kb.init_db()
    tid = _subscribed_task("notify+wake")
    _emit(tid, "plan_awaiting_approval", {"project_id": "p", "revision": 1})

    # ONE persistent runner: the failure counter lives on it across ticks.
    runner = _runner(NonPushAdapter())
    attempts = 0
    for _ in range(14):
        runner._running = True
        runner.adapters = {Platform.TELEGRAM: NonPushAdapter()}
        asyncio.run(_one_tick(monkeypatch, runner))
        attempts += len(runner.adapters[Platform.TELEGRAM].sent)

    assert attempts == 0, "no send may be attempted on a channel that cannot send"
    assert _subs(tid), "the subscription must survive"
    assert len(_undeliverable_rows(tid)) == 1, "recorded once, not every tick"


def test_non_push_gate_only_keeps_working_for_later_actionable_events(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "np-later.db"))
    kb.init_db()
    tid = _subscribed_task("notify+wake")
    _emit(tid, "plan_rejected", {"operator": "op", "project_id": "p",
                                 "revision": 1})
    adapter = NonPushAdapter()
    _tick(monkeypatch, adapter)
    assert len(_undeliverable_rows(tid)) == 1

    import gateway.wake as wake_mod
    posted = []

    async def _record(adapter, *, text, session_id="", source=None):
        posted.append(text)

    monkeypatch.setattr(wake_mod, "deliver_wake", _record)
    _emit(tid, "completed", {"summary": "done"})
    _tick(monkeypatch, NonPushAdapter())
    assert len(posted) == 1, "the subscription still delivers actionable work"


def test_wake_only_gate_batch_is_recorded_as_undeliverable(tmp_path, monkeypatch):
    """The documented wake-only choice is now auditable, not invisible."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "wakeonly-audit.db"))
    kb.init_db()
    tid = _subscribed_task("wake")
    _emit(tid, "plan_awaiting_approval", {"project_id": "p", "revision": 1})
    adapter = PushAdapter()
    _tick(monkeypatch, adapter)
    assert adapter.sent == []
    assert adapter.handled == []
    rows = _undeliverable_rows(tid)
    assert len(rows) == 1
    assert rows[0]["delivery_mode"] == "wake"
    assert "wake-only" in rows[0]["reason"]


# ---------------------------------------------------------------------------
# Path / Unicode / surrogate attacks (second-round review)
# ---------------------------------------------------------------------------

PATH_ATTACKS = [
    "/Users/example/My Secret/config.yaml",
    "file:///Users/example/private/config.yaml",
    "/Volumes/PrivateDrive/company/secrets.txt",
    "/opt/homebrew/etc/private.conf",
    "/root/.ssh/id_rsa",
    "/Applications/Hermes.app/Contents/Resources/x",
    "/Library/Keychains/login.keychain-db",
    "/srv/data/customer/export.csv",
    r"C:\Users\example\secret.txt",
]

LEAK_FRAGMENTS = (
    "Secret/config", "private/config", "PrivateDrive", "homebrew/etc",
    ".ssh/id_rsa", "Hermes.app", "Keychains", "customer/export",
    "example\\secret",
)

UNICODE_ATTACKS = [
    "operator\u202eevil",          # RIGHT-TO-LEFT OVERRIDE
    "op\u202areordered\u202c",     # LRE / PDF
    "op\u2066hidden\u2069x",       # isolates
    "ad\u200bmin",                 # zero width space
    "a\u200d\u200cb",              # ZWJ / ZWNJ
    "x\ufeffy",                    # BOM
    "x\ud800y",                    # lone surrogate
]


@pytest.mark.parametrize("value", PATH_ATTACKS)
def test_local_paths_are_redacted_without_leaking_the_tail(value):
    out = ge.safe_display_value(value, limit=200)
    for fragment in LEAK_FRAGMENTS:
        assert fragment not in out, f"{value!r} leaked {fragment!r} as {out!r}"


@pytest.mark.parametrize("value", UNICODE_ATTACKS)
def test_unicode_format_controls_and_surrogates_are_removed(value):
    out = ge.safe_display_value(value, limit=200)
    for ch in out:
        assert unicodedata.category(ch) not in ("Cf", "Cs"), repr(out)
    out.encode("utf-8")  # must never raise inside an adapter's encoder


def test_ordinary_prose_after_a_path_survives():
    out = ge.safe_display_value(
        "/Users/me/x and then the plan was approved", limit=200)
    assert "and then the plan was approved" in out
    assert "/Users/me/x" not in out


def test_a_lone_surrogate_cannot_even_be_stored(tmp_path, monkeypatch):
    """Defence in depth: the DB layer rejects it, and so does the formatter."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "surrogate.db"))
    kb.init_db()
    tid = _subscribed_task("notify")
    with pytest.raises(UnicodeEncodeError):
        _emit(tid, "plan_approved", {"operator": "x\ud800y"})
    # The formatter still neutralises it for any non-DB caller.
    out = ge.safe_display_value("x\ud800y")
    assert all(unicodedata.category(c) not in ("Cf", "Cs") for c in out)
    out.encode("utf-8")


@pytest.mark.parametrize(
    "value", PATH_ATTACKS + [v for v in UNICODE_ATTACKS if v.isprintable()
                             or "\ud800" not in v])
def test_attacks_are_neutralised_through_the_real_notifier(
    tmp_path, monkeypatch, value
):
    monkeypatch.setenv("HERMES_KANBAN_DB",
                       str(tmp_path / f"atk-{abs(hash(value))}.db"))
    kb.init_db()
    tid = _subscribed_task("notify")
    _emit(tid, "plan_approved", {"operator": value, "project_id": value,
                                 "revision": 1, "landing_status": value,
                                 "reason": value})
    adapter = PushAdapter()
    _tick(monkeypatch, adapter)
    assert len(adapter.sent) == 1
    text = adapter.sent[0]["text"]
    for fragment in LEAK_FRAGMENTS:
        assert fragment not in text
    for ch in text:
        assert unicodedata.category(ch) not in ("Cf", "Cs")
    text.encode("utf-8")


@pytest.mark.parametrize("value", PATH_ATTACKS + UNICODE_ATTACKS)
def test_attacks_are_neutralised_through_the_shared_renderer(value):
    out = ge.render_gate_event(
        "gate_release_refused", {"via": value, "gate_state": value,
                                 "reason": value},
        task_id="t_1", board_slug=value, assignee=value,
    )
    for fragment in LEAK_FRAGMENTS:
        assert fragment not in out
    for ch in out:
        assert unicodedata.category(ch) not in ("Cf", "Cs")
    out.encode("utf-8")


# ---------------------------------------------------------------------------
# The disposition is durable and idempotent (third-round review)
# ---------------------------------------------------------------------------

def _break_audit_writes(monkeypatch):
    """Fail ONLY the disposition write; every other event still records."""
    real_append = kb._append_event

    def _boom(conn, task_id, kind, payload=None, run_id=None):
        if kind == ge.GATE_UNDELIVERABLE_KIND:
            raise RuntimeError("simulated storage failure")
        return real_append(conn, task_id, kind, payload, run_id=run_id)

    monkeypatch.setattr(kb, "_append_event", _boom)


@pytest.mark.parametrize("mode", ["notify+wake", "wake"])
def test_an_audit_write_failure_does_not_consume_the_gate_event(
    tmp_path, monkeypatch, mode
):
    """Fail closed: no row written means the claim is given back."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / f"audit-fail-{mode}.db"))
    kb.init_db()
    tid = _subscribed_task(mode)
    _emit(tid, "plan_awaiting_approval", {"project_id": "p", "revision": 1})

    _break_audit_writes(monkeypatch)
    adapter = NonPushAdapter() if mode == "notify+wake" else PushAdapter()
    _tick(monkeypatch, adapter)

    assert _undeliverable_rows(tid) == [], "the write was supposed to fail"
    assert _unseen(tid, GATE_KINDS), "the event must NOT have been consumed"
    assert _subs(tid), "a failing database is not a dead chat"
    assert adapter.handled == []

    # With storage healthy again the next tick records it and moves on.
    monkeypatch.undo()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / f"audit-fail-{mode}.db"))
    _tick(monkeypatch, NonPushAdapter() if mode == "notify+wake" else PushAdapter())
    assert len(_undeliverable_rows(tid)) == 1
    assert _unseen(tid, GATE_KINDS) == []


def test_a_repeatedly_failing_audit_write_never_unsubscribes(tmp_path, monkeypatch):
    """A broken board database must not be treated as a dead chat."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "audit-fail-many.db"))
    kb.init_db()
    tid = _subscribed_task("notify+wake")
    _emit(tid, "plan_awaiting_approval", {"project_id": "p", "revision": 1})
    _break_audit_writes(monkeypatch)
    runner = _runner(NonPushAdapter())
    for _ in range(15):
        runner._running = True
        runner.adapters = {Platform.TELEGRAM: NonPushAdapter()}
        asyncio.run(_one_tick(monkeypatch, runner))
    assert _subs(tid), "the subscription must survive a failing database"
    assert _unseen(tid, GATE_KINDS), "and the event must still be unconsumed"


def test_a_rewound_mixed_batch_does_not_duplicate_the_disposition(
    tmp_path, monkeypatch
):
    """A later self-post failure rewinds the whole batch — the row must not
    be appended again when the gate event is reprocessed."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "rewind-dup.db"))
    kb.init_db()
    tid = _subscribed_task("notify+wake")
    _emit(tid, "plan_awaiting_approval", {"project_id": "p", "revision": 1})
    _emit(tid, "completed", {"summary": "done"})

    import gateway.wake as wake_mod
    attempts = []

    async def _failing(adapter, *, text, session_id="", source=None):
        attempts.append(text)
        raise RuntimeError("self-post failed")

    monkeypatch.setattr(wake_mod, "deliver_wake", _failing)

    runner = _runner(NonPushAdapter())
    for _ in range(3):
        runner._running = True
        runner.adapters = {Platform.TELEGRAM: NonPushAdapter()}
        asyncio.run(_one_tick(monkeypatch, runner))

    assert len(attempts) == 3, "the actionable half really did retry"
    assert len(_undeliverable_rows(tid)) == 1, "one row per source event"


def test_gateway_restarts_cannot_grow_the_audit_log(tmp_path, monkeypatch):
    """A fresh runner resets the in-memory counter; the row must not repeat."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "restart-dup.db"))
    kb.init_db()
    tid = _subscribed_task("notify+wake")
    _emit(tid, "plan_awaiting_approval", {"project_id": "p", "revision": 1})
    _emit(tid, "completed", {"summary": "done"})

    import gateway.wake as wake_mod

    async def _failing(adapter, *, text, session_id="", source=None):
        raise RuntimeError("self-post failed")

    monkeypatch.setattr(wake_mod, "deliver_wake", _failing)

    for _ in range(8):                      # eight separate "gateway restarts"
        _tick(monkeypatch, NonPushAdapter())

    assert len(_undeliverable_rows(tid)) == 1
    rows = _undeliverable_rows(tid)
    assert rows[0]["event_kind"] == "plan_awaiting_approval"
    assert "source_event_id" in rows[0] and "subscription" in rows[0]
    # The digest keys the row without publishing a new addressable identifier.
    assert "chat-1" not in json.dumps(rows[0])


def test_two_subscriptions_each_get_their_own_disposition(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "two-subs.db"))
    kb.init_db()
    tid = _subscribed_task("notify+wake", chat_id="chat-1")
    conn = kb.connect()
    try:
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-2",
            thread_id="topic-7", chat_type="thread",
            delivery_mode="notify+wake",
        )
    finally:
        conn.close()
    _emit(tid, "plan_awaiting_approval", {"project_id": "p", "revision": 1})
    _tick(monkeypatch, NonPushAdapter())
    rows = _undeliverable_rows(tid)
    assert len(rows) == 2, "one per subscription"
    assert len({r["subscription"] for r in rows}) == 2


# ---------------------------------------------------------------------------
# Multi-word and Windows paths (third-round review)
# ---------------------------------------------------------------------------

MULTIWORD_PATHS = [
    "/Users/rick/My Very Secret/config.yaml",
    "file:///Users/rick/My Very Secret/config.yaml",
    r"C:\Users\Rick Swindell\Secret Folder\key.txt",
    "/Volumes/My Big Drive/Client Files/keys.txt",
    "/home/rick/Two Word Dir/Another One/creds.json",
    r"D:\Program Files\Hermes Data\token.txt",
]

MULTIWORD_LEAKS = (
    "Very Secret", "Secret Folder", "Client Files", "Another One",
    "Hermes Data", "config.yaml", "key.txt", "keys.txt", "creds.json",
    "token.txt", "Swindell",
)


@pytest.mark.parametrize("value", MULTIWORD_PATHS)
def test_multiword_and_windows_paths_are_fully_redacted(value):
    out = ge.safe_display_value(value, limit=200)
    for fragment in MULTIWORD_LEAKS:
        assert fragment not in out, f"{value!r} leaked {fragment!r} as {out!r}"


@pytest.mark.parametrize("value", MULTIWORD_PATHS)
def test_multiword_paths_are_redacted_through_the_real_notifier(
    tmp_path, monkeypatch, value
):
    monkeypatch.setenv("HERMES_KANBAN_DB",
                       str(tmp_path / f"mw-{abs(hash(value))}.db"))
    kb.init_db()
    tid = _subscribed_task("notify")
    _emit(tid, "plan_rejected", {"operator": value, "project_id": value,
                                 "revision": 1, "reason": value})
    adapter = PushAdapter()
    _tick(monkeypatch, adapter)
    assert len(adapter.sent) == 1
    for fragment in MULTIWORD_LEAKS:
        assert fragment not in adapter.sent[0]["text"]


def test_prose_after_a_path_still_survives_redaction():
    out = ge.safe_display_value(
        "/Users/me/x and then the plan was approved by the operator", limit=200)
    assert "and then the plan was approved by the operator" in out
    assert "/Users/me/x" not in out


@pytest.mark.parametrize("value", [
    " " * 20000 + "/Users/x",
    "/Users/" + "a " * 5000,
    "C:\\" + "a " * 5000,
])
def test_pathological_inputs_stay_bounded(value):
    start = time.monotonic()
    out = ge.safe_display_value(value, limit=160)
    assert time.monotonic() - start < 2.0
    assert len(out) <= 160
