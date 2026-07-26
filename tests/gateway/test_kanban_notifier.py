import asyncio
from pathlib import Path


from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


class DisconnectedAdapters(dict):
    """Expose a platform during collection, then simulate disconnect on get()."""

    def get(self, key, default=None):
        return None


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    return runner


def _create_completed_subscription(summary="done once"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify once", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_dedupes_board_slugs_pointing_to_same_db(tmp_path, monkeypatch):
    db_path = tmp_path / "shared-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    kb.write_board_metadata("alias-a", name="Alias A")
    kb.write_board_metadata("alias-b", name="Alias B")

    tid = _create_completed_subscription()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "Kanban" in adapter.sent[0]["text"]
    assert tid in adapter.sent[0]["text"]


def test_kanban_notifier_claim_prevents_second_watcher_send(tmp_path, monkeypatch):
    db_path = tmp_path / "single-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_completed_subscription()

    adapter1 = RecordingAdapter()
    adapter2 = RecordingAdapter()

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter1)))
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter2)))

    assert len(adapter1.sent) == 1
    assert adapter2.sent == []


def test_kanban_notifier_rewinds_claim_if_adapter_disconnects(tmp_path, monkeypatch):
    db_path = tmp_path / "adapter-disconnect.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = DisconnectedAdapters({Platform.TELEGRAM: RecordingAdapter()})
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_kanban_db_path_is_test_isolated_from_real_home():
    hermes_home = Path(kb.kanban_home())
    production_db = Path.home() / ".hermes" / "kanban.db"
    assert kb.kanban_db_path().resolve() != production_db.resolve()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
    finally:
        conn.close()

    assert kb.kanban_db_path().resolve().is_relative_to(hermes_home.resolve())
    assert kb.kanban_db_path().resolve() != production_db.resolve()


class FailingAdapter:
    """Adapter whose send() always raises, simulating a transient send error."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        raise RuntimeError("simulated send failure")


def test_kanban_notifier_rewinds_claim_on_send_exception(tmp_path, monkeypatch):
    """A raising adapter rewinds the claim so the next tick can retry.

    This is the second rewind path (distinct from the adapter-disconnect path
    in test_kanban_notifier_rewinds_claim_if_adapter_disconnects). Here the
    adapter is connected and the send call actually fires; the claim must
    still rewind so the event isn't lost when send() raises mid-tick.
    """
    db_path = tmp_path / "send-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    adapter = FailingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Send was attempted (so we exercised the failure path, not just the
    # disconnect path) and the claim was rewound — the unseen-events query
    # still returns the event for retry on the next tick.
    assert adapter.attempts >= 1, "send should have been attempted at least once"
    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_notifier_redelivers_same_kind_on_dispatch_cycle(tmp_path, monkeypatch):
    """A retry cycle (crashed → reclaimed → crashed) notifies the user twice.

    Before #21398 the notifier auto-unsubscribed on any terminal event kind
    (gave_up / crashed / timed_out), so the second crash in a respawn cycle
    silently dropped — the subscription was already gone. This test pins the
    new contract: subscription survives non-final terminal events; the
    cursor handles dedup.

    Two crashes ten seconds apart on the same task — both should land on
    the adapter.
    """
    db_path = tmp_path / "redeliver-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cycle test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # First crash — fired by the dispatcher when the worker PID dies.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # First crash delivered.
    assert len(adapter.sent) == 1
    assert "crashed" in adapter.sent[0]["text"].lower()

    # Subscription survives — the cursor advanced past event #1, but the
    # row is still there.
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1, (
            "Subscription must survive a crashed event so a respawn-cycle "
            "second crash also notifies the user (issue #21398)."
        )

        # Second crash — same task, same dispatcher (or a respawn). Append
        # another event to simulate the dispatcher firing crashed a second
        # time during retry.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    # New tick: the second event has a fresh id past the cursor advance,
    # so it gets claimed and delivered.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2, (
        f"Second crashed event should also notify; got {len(adapter.sent)} "
        f"deliveries (texts: {[d['text'] for d in adapter.sent]})"
    )
    assert "crashed" in adapter.sent[1]["text"].lower()


def test_notifier_owning_profile_adapter_no_default_fallback(tmp_path, monkeypatch):
    """A subscription owned by a secondary profile whose profile-adapter
    registry entry EXISTS but lacks this platform must NOT fall back to the
    default profile's same-platform adapter — the notifier must route through
    the shared ``_authorization_adapter`` chokepoint, which forbids that
    fallback (gateway/authz_mixin.py). Delivering via the default profile's bot
    is the exact cross-profile mis-delivery this whole change exists to fix
    (`[230002] Bot can NOT be out of the chat`).

    Mutation check: reverting kanban_watchers.py's adapter selection to the old
    inline ``if adapter is None: adapter = self.adapters.get(plat)`` fallback
    makes this test FAIL (the default adapter receives the delivery).
    """
    db_path = tmp_path / "profile-no-fallback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="owned by beta", assignee="worker")
        # Subscription is owned by profile "beta".
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-beta",
            notifier_profile="beta",
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    default_adapter = RecordingAdapter()
    other_adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    # Default profile has a telegram adapter …
    runner.adapters = {Platform.TELEGRAM: default_adapter}
    # … and profile "beta" HAS a non-empty registry entry (so it passes the
    # notifier's upstream skip-filter, which only skips owning profiles with NO
    # adapter at all), but that entry does NOT contain a telegram adapter — beta
    # connected a different platform (discord). The telegram sub owned by beta
    # must therefore resolve to NO adapter, not silently borrow the default
    # profile's telegram bot.
    runner._profile_adapters = {"beta": {Platform.DISCORD: other_adapter}}
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # The default profile's adapter must never receive beta's notification.
    assert default_adapter.sent == [], (
        "Owning-profile subscription must not fall back to the default "
        f"profile's adapter; got {default_adapter.sent!r}"
    )
    assert other_adapter.sent == [], (
        f"beta's discord adapter must not receive a telegram sub; got {other_adapter.sent!r}"
    )
    # The claim is rewound (adapter resolved to None → treated as disconnected),
    # so the event is still unseen and will deliver once beta's adapter connects.
    assert [ev.kind for ev in _unseen_terminal_events_for(tid, "chat-beta")] == ["completed"]


def _unseen_terminal_events_for(tid, chat_id):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id=chat_id,
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# RCJ durability: SendResult false + crash replay semantics (C2a-C3c)
# ---------------------------------------------------------------------------


class FalseSendResultAdapter:
    """Adapter whose send() returns SendResult(success=False) without raising.

    This is the C2a/C2b scenario: the adapter is connected and responds, but
    reports a genuine transient failure (rate-limit, downstream 5xx).  The
    notifier must rewind the claim so the event is retried — advancing the
    cursor would permanently lose the notification.
    """

    def __init__(self):
        self.sent = []
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})
        from gateway.platforms.base import SendResult

        return SendResult(success=False, error="simulated transient failure")


def test_kanban_notifier_rewinds_when_send_result_false(tmp_path, monkeypatch):
    """C2a/C2b — a SendResult(success=False) must rewind the cursor.

    Before the fix, a False SendResult (distinct from an exception) was
    silently ignored and the cursor advanced, permanently losing the
    notification.  This test pins the contract that the notifier treats a
    False SendResult identically to a raised exception: rewind + retry.
    """
    db_path = tmp_path / "false-send-result.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    adapter = FalseSendResultAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Send was attempted (the adapter was connected and responded).
    assert adapter.attempts >= 1, "send should have been attempted"

    # The cursor must NOT have advanced — the event is still unseen.
    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"], (
        "SendResult(success=False) must rewind the claim so the event "
        "is retried on the next tick, not permanently lost."
    )


class CrashAfterClaimAdapter:
    """Adapter that simulates a crash after the claim but before send completes.

    For C3a/C3b: the first call raises a transient error (the closest
    in-process equivalent of a process crash mid-send that the notifier
    can catch and rewind); the key invariant is that the cursor advance
    inside ``claim_unseen_events_for_sub`` is reversed by the rewind path
    so the next tick sees the same event.
    """

    def __init__(self, crash_on_first=True):
        self.crash_on_first = crash_on_first
        self.sent = []
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        if self.crash_on_first and self.attempts == 1:
            raise RuntimeError("simulated transient failure before send completed")
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


def test_kanban_notifier_claim_before_send_crash_replays_unadvanced_cursor(
    tmp_path, monkeypatch,
):
    """C3a/C3b — crash after claim but before confirmed send must replay.

    The claim step (claim_unseen_events_for_sub) advances the cursor
    atomically, but the notifier rewinds on send failure.  After a crash
    that prevents the cursor from being confirmed, the next tick must see
    the same event and retry delivery.
    """
    db_path = tmp_path / "crash-before-send.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    # First tick: crash during send.
    crash_adapter = CrashAfterClaimAdapter(crash_on_first=True)
    crash_runner = _make_runner(crash_adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, crash_runner))

    # The crash prevented confirmation. The event must still be unseen.
    assert crash_adapter.attempts == 1, "first tick should have attempted send"
    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"], (
        "After a crash before confirmed send, the event must still be "
        "unseen for replay on the next tick."
    )

    # Second tick: a fresh runner picks up the same event and delivers it.
    ok_adapter = RecordingAdapter()
    ok_runner = _make_runner(ok_adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, ok_runner))

    assert len(ok_adapter.sent) == 1, (
        "Second tick should replay the unconfirmed event and deliver it."
    )
    assert "done" in ok_adapter.sent[0]["text"].lower()


class PartialBatchCrashAdapter:
    """Adapter that crashes after the first event in a multi-event batch.

    For C3c: when a claim returns multiple events, a failure after the first
    send (but before all sends complete) must leave the unconfirmed tail
    available for replay.
    """

    def __init__(self):
        self.sent = []
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        if self.attempts == 1:
            self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})
            return  # first event succeeds
        raise RuntimeError("simulated failure before second send")


def test_kanban_notifier_partial_batch_crash_replays_unconfirmed_tail(
    tmp_path, monkeypatch,
):
    """C3c — partial batch crash must replay the unconfirmed tail.

    Two events are claimed atomically. The first send succeeds but the
    second crashes. Because the notifier breaks on the first send failure
    (the SystemExit on attempt 2) and rewinds the entire claim, both
    events must remain available for replay on the next tick.
    """
    db_path = tmp_path / "partial-batch-crash.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    # Create a task with two terminal events so the claim returns a batch.
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="partial batch test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb._append_event(conn, tid, kind="crashed")
        kb._append_event(conn, tid, kind="completed")
    finally:
        conn.close()

    # First tick: crash on second send.
    crash_adapter = PartialBatchCrashAdapter()
    crash_runner = _make_runner(crash_adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, crash_runner))

    # The batch was claimed but crashed mid-way; the rewind means at least
    # the unconfirmed events remain unseen. Because the notifier breaks on
    # the first failure and rewinds the whole claim, all events replay.
    remaining = _unseen_terminal_events(tid)
    assert len(remaining) >= 1, (
        "After a partial-batch crash, at least the unconfirmed tail must "
        "remain unseen for replay."
    )

