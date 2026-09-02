import asyncio
import sqlite3
from pathlib import Path


from gateway.config import Platform
from gateway.kanban_watchers import (
    _acquire_singleton_lock,
    _release_singleton_lock,
)
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)


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
    # Most tests model the default gateway after its dispatcher acquired the
    # singleton lock. Tests for startup or non-owner gateways clear this.
    runner._kanban_dispatcher_lock_handle = object()
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


def test_kanban_notifier_replays_telegram_dm_topic_delivery_metadata(tmp_path, monkeypatch):
    db_path = tmp_path / "dm-topic-metadata.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="dm topic task",
            assignee="worker",
            session_id="agent:main:telegram:dm:chat-1",
        )
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            thread_id="20197",
            delivery_mode="notify+wake",
            delivery_metadata={
                "chat_type": "dm",
                "direct_messages_topic_id": "20197",
                "telegram_dm_topic_reply_fallback": True,
                "telegram_reply_to_message_id": "462",
                "thread_id": "20197",
            },
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["metadata"] == {
        "chat_type": "dm",
        "direct_messages_topic_id": "20197",
        "telegram_dm_topic_reply_fallback": True,
        "telegram_reply_to_message_id": "462",
        "thread_id": "20197",
    }
    assert len(adapter.handled) == 1
    assert adapter.handled[0].source.chat_type == "dm"
    assert adapter.handled[0].source.thread_id == "20197"


def test_active_named_profile_subscription_is_delivered(tmp_path, monkeypatch):
    """A sub stamped with the gateway's own named profile uses self.adapters.

    Regression for #71340: on a standalone (non-multiplex) gateway running a
    named profile, _authorization_adapter() used to treat the active name as a
    multiplex secondary, find no _profile_adapters entry, fail closed, and
    rewind the claim forever — silent zero-delivery.
    """
    db_path = tmp_path / "actionable-block.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    reason = "AGE-39 — https://linear.example/AGE-39 — publishing verified."
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="approval", assignee="publisher")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            notifier_profile="main",
        )
        kb.block_task(conn, tid, reason=reason, kind="needs_input")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "main"

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    message = adapter.sent[0]["text"]
    assert tid in message
    assert "blocked" in message


def test_non_dispatch_gateway_claims_only_its_profile_subscriptions(
    tmp_path, monkeypatch,
):
    """A profile gateway delivers its events while another gateway dispatches."""
    db_path = tmp_path / "cross-profile-notifier.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        foreign_tid = kb.create_task(
            conn, title="default-owned", assignee="worker",
        )
        kb.add_notify_sub(
            conn,
            task_id=foreign_tid,
            platform="telegram",
            chat_id="default-chat",
            notifier_profile="default",
        )
        kb.complete_task(conn, foreign_tid, summary="default done")

        owned_tid = kb.create_task(
            conn, title="writer-owned", assignee="worker",
        )
        kb.add_notify_sub(
            conn,
            task_id=owned_tid,
            platform="telegram",
            chat_id="writer-chat",
            notifier_profile="writer",
        )
        kb.complete_task(conn, owned_tid, summary="writer done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "writer"
    runner._kanban_dispatcher_lock_handle = None

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [delivery["chat_id"] for delivery in adapter.sent] == ["writer-chat"]
    assert owned_tid in adapter.sent[0]["text"]
    assert len(_unseen_terminal_events_for(foreign_tid, "default-chat")) == 1


def test_legacy_subscription_requires_confirmed_dispatcher_lock_owner(
    tmp_path, monkeypatch,
):
    """Startup and lock-losing gateways cannot claim legacy notifications."""
    db_path = tmp_path / "legacy-lock-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="legacy", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="legacy-chat",
        )
        kb.complete_task(conn, task_id, summary="legacy done")
    finally:
        conn.close()

    startup_adapter = RecordingAdapter()
    startup_runner = _make_runner(startup_adapter)
    startup_runner._kanban_dispatcher_lock_handle = None
    asyncio.run(_run_one_notifier_tick(monkeypatch, startup_runner))
    assert startup_adapter.sent == []
    assert len(_unseen_terminal_events_for(task_id, "legacy-chat")) == 1

    lock_path = tmp_path / ".dispatcher.lock"
    winner_handle, winner_state = _acquire_singleton_lock(lock_path)
    loser_handle, loser_state = _acquire_singleton_lock(lock_path)
    try:
        assert winner_state == "held"
        assert loser_state == "contended"

        loser_adapter = RecordingAdapter()
        loser_runner = _make_runner(loser_adapter)
        loser_runner._kanban_dispatcher_lock_handle = loser_handle
        asyncio.run(_run_one_notifier_tick(monkeypatch, loser_runner))
        assert loser_adapter.sent == []
        assert len(_unseen_terminal_events_for(task_id, "legacy-chat")) == 1

        winner_adapter = RecordingAdapter()
        winner_runner = _make_runner(winner_adapter)
        winner_runner._kanban_dispatcher_lock_handle = winner_handle
        asyncio.run(_run_one_notifier_tick(monkeypatch, winner_runner))
        assert [item["chat_id"] for item in winner_adapter.sent] == ["legacy-chat"]
        assert task_id in winner_adapter.sent[0]["text"]
    finally:
        _release_singleton_lock(loser_handle)
        _release_singleton_lock(winner_handle)


class FailingAdapter:
    """Adapter whose send() always raises, simulating a transient send error."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        raise RuntimeError("simulated send failure")


class ReportedFailureAdapter:
    """Adapter that REPORTS failure via SendResult(success=False) instead of
    raising — the exact contract the Telegram adapter uses for 'Not connected'
    and degraded-send paths."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        from gateway.platforms.base import SendResult
        return SendResult(success=False, error="Not connected")


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


def test_notifier_subscription_survives_done_reopen_until_archive(
    tmp_path, monkeypatch,
):
    """Done is reversible; archive alone ends notification ownership."""
    db_path = tmp_path / "done-reopen-archive.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="review continuation",
            assignee="worker",
            session_id="origin-session",
        )
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="origin-chat",
            thread_id="origin-thread",
            user_id="origin-user",
            chat_type="group",
            notifier_profile="reviewer",
            delivery_mode="notify+wake",
        )
        assert kb.complete_task(conn, tid, summary="first completion")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "reviewer"
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1
    assert adapter.sent[0]["chat_id"] == "origin-chat"
    assert adapter.sent[0]["metadata"]["thread_id"] == "origin-thread"
    assert adapter.handled[0].source.thread_id == "origin-thread"
    assert adapter.handled[0].source.profile == "reviewer"

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1, "completion must retain the origin subscription"
        first_cursor = subs[0]["last_event_id"]
    finally:
        conn.close()

    # A quiet tick proves the completed event cannot replay after its cursor
    # was advanced, even though the subscription now remains present.
    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "reviewer"
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1

    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
            kb._append_event(conn, tid, "status", {"status": "ready"})
        assert kb.complete_task(conn, tid, summary="corrected completion")
    finally:
        conn.close()

    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "reviewer"
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # The reopen status and second completion each deliver once, while only
    # completion wakes the exact original session/thread.
    assert len(adapter.sent) == 3
    assert len(adapter.handled) == 2
    assert all(item["chat_id"] == "origin-chat" for item in adapter.sent)
    assert adapter.handled[-1].source.thread_id == "origin-thread"
    assert adapter.handled[-1].source.profile == "reviewer"

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1
        assert subs[0]["last_event_id"] > first_cursor
        assert kb.archive_task(conn, tid)
    finally:
        conn.close()

    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "reviewer"
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Archive itself is intentionally silent, but consumes its event and
    # removes the subscription so no later historical event can replay.
    assert len(adapter.sent) == 3
    assert len(adapter.handled) == 2
    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn, tid) == []
    finally:
        conn.close()


def test_notifier_wakeup_uses_subscription_chat_type(tmp_path, monkeypatch):
    db_path = tmp_path / "chat-type-wakeup.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="dm requester",
            assignee="worker",
            session_id="origin-session",
        )
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-dm",
            chat_type="dm",
            delivery_mode="notify+wake",
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1
    assert adapter.handled[0].source.chat_type == "dm"

    # The wake must resume the creator's real DM session key — the whole bug
    # was that a hardcoded chat_type="group" made build_session_key() produce
    # a group-scoped key (a NEW session) instead of the ":dm:<chat_id>" shape
    # the original conversation runs under (#56580 / #68874).
    from gateway.session import build_session_key

    wake_key = build_session_key(adapter.handled[0].source)
    assert wake_key == "agent:main:telegram:dm:chat-dm"
    assert ":group:" not in wake_key


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


def test_kanban_notifier_isolates_per_subscription_failure(tmp_path, monkeypatch):
    """One bad subscription must not block delivery for all others.

    Regression for #59269: when claim_unseen_events_for_sub raises for one
    subscription, the entire notifier tick used to abort — silently blocking
    delivery for every other subscription.
    """
    db_path = tmp_path / "isolation.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    # Create two tasks with subscriptions and complete both. The BAD task is
    # created first: list_notify_subs() has no ORDER BY, so SQLite's natural
    # scan returns insertion order — the failing subscription must be
    # processed BEFORE the good one or this test passes even without the
    # per-subscription isolation (the good delivery happens before the tick
    # aborts). A deterministic-order shim below removes the reliance on the
    # scan order entirely.
    conn = kb.connect()
    try:
        tid_bad = kb.create_task(conn, title="bad task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid_bad, platform="telegram", chat_id="chat-bad")
        kb.complete_task(conn, tid_bad, summary="done")

        tid_good = kb.create_task(conn, title="good task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid_good, platform="telegram", chat_id="chat-good")
        kb.complete_task(conn, tid_good, summary="done")
    finally:
        conn.close()

    original_claim = kb.claim_unseen_events_for_sub

    def selective_claim(conn, task_id, **kwargs):
        if task_id == tid_bad:
            raise RuntimeError("simulated DB corruption for bad task")
        return original_claim(conn, task_id=task_id, **kwargs)

    monkeypatch.setattr(kb, "claim_unseen_events_for_sub", selective_claim)

    # Force the failing subscription to be iterated FIRST regardless of the
    # unordered SELECT's scan order.
    original_list = kb.list_notify_subs

    def bad_first(conn, task_id=None, **kwargs):
        subs = original_list(conn, task_id, **kwargs)
        return sorted(subs, key=lambda s: 0 if s["task_id"] == tid_bad else 1)

    monkeypatch.setattr(kb, "list_notify_subs", bad_first)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # The good task must still be delivered despite the bad task failing.
    assert len(adapter.sent) == 1
    assert tid_good in adapter.sent[0]["text"]


def test_notifier_delivers_block_loop_detected_triage_ping(tmp_path, monkeypatch):
    """A `block_loop_detected` event must reach the subscriber as a triage ping.

    Regression for the silent-triage gap (PR #62712): kanban_db routes a task
    to `triage` after BLOCK_RECURRENCE_LIMIT re-blocks for the same cause and
    emits ONLY a `block_loop_detected` event — no `blocked`/`status` event.
    Before `block_loop_detected` joined TERMINAL_KINDS with its own message
    branch, that one transition (the whole point of which is to force human
    attention) produced zero notification and the task stalled in triage
    silently.
    """
    db_path = tmp_path / "block-loop.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="loops forever", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb._append_event(
            conn, tid, "block_loop_detected",
            {"reason": "needs credentials", "kind": "needs_input",
             "recurrences": 2, "limit": kb.BLOCK_RECURRENCE_LIMIT},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, "block_loop_detected must produce a notification"
    text = adapter.sent[0]["text"]
    assert "TRIAGE" in text
    assert tid in text
    assert "needs credentials" in text
    # Cursor advanced: the event is claimed and not re-delivered.
    conn = kb.connect()
    try:
        _, remaining = kb.unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            kinds=["block_loop_detected"],
        )
    finally:
        conn.close()
    assert remaining == []


# ---------------------------------------------------------------------------
# Handoffs that hand a decision back to the origin must wake it, not only ping
# it: `review_requested` (implementation done, waiting for a reviewer) and
# `block_loop_detected` (routed to triage) are terminal kinds just like
# `blocked`.
# ---------------------------------------------------------------------------


def _wake_text(adapter):
    """Text of the single synthetic wake turn injected into the adapter."""
    assert len(adapter.handled) == 1, (
        f"expected exactly one wake turn, got {len(adapter.handled)}"
    )
    return getattr(adapter.handled[0], "text", "") or ""


def _review_handoff_task(
    *,
    delivery_mode="notify+wake",
    summary="PR ready: https://example.invalid/pr/7\nfull details below",
):
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="implement the thing",
            assignee="worker",
            session_id="agent:main:telegram:dm:chat-1",
        )
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            chat_type="dm",
            delivery_mode=delivery_mode,
        )
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.request_review(
            conn, tid, summary=summary, expected_run_id=run_id,
        ) is True
        return tid
    finally:
        conn.close()


def test_review_requested_wakes_the_origin_session(tmp_path, monkeypatch):
    """A review handoff wakes the origin and carries the worker's summary."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "review-wake.db"))
    kb.init_db()
    tid = _review_handoff_task()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, "the passive review ping is unchanged"
    assert "ready for review" in adapter.sent[0]["text"]

    wake = _wake_text(adapter)
    assert tid in wake
    assert "PR ready: https://example.invalid/pr/7" in wake, (
        "the worker's handoff must ride the wake turn like it does for "
        "`completed`, otherwise the woken reviewer has to re-read the board"
    )


def test_block_loop_detected_wakes_the_origin_session(tmp_path, monkeypatch):
    """A triage escalation wakes the origin so a decision gets made."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "triage-wake.db"))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="loops forever",
            assignee="worker",
            session_id="agent:main:telegram:dm:chat-1",
        )
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            chat_type="dm",
            delivery_mode="notify+wake",
        )
        kb._append_event(
            conn, tid, "block_loop_detected",
            {"reason": "needs credentials", "kind": "needs_input",
             "recurrences": 2, "limit": kb.BLOCK_RECURRENCE_LIMIT},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert tid in _wake_text(adapter)


def test_review_requested_does_not_wake_a_notify_only_subscription(
    tmp_path, monkeypatch,
):
    """delivery_mode still decides whether a wake-worthy kind wakes at all."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "review-notify.db"))
    kb.init_db()
    _review_handoff_task(delivery_mode="notify")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.handled == [], (
        "notify-only subscriptions must not be woken by a review handoff"
    )


def test_kanban_subscription_metadata_keeps_profile_route_anchors():
    """Auto-subscribe persists enough source identity to replay exact routing."""
    from types import SimpleNamespace

    runner = GatewayRunner.__new__(GatewayRunner)
    source = SimpleNamespace(
        platform=Platform.DISCORD,
        chat_id="thread-7",
        thread_id="thread-7",
        chat_type="thread",
        message_id="message-9",
        scope_id="guild-3",
        guild_id="guild-3",
        parent_chat_id="engineering-chat",
    )

    metadata = runner._thread_metadata_for_source(source)

    assert metadata == {
        "thread_id": "thread-7",
        "scope_id": "guild-3",
        "guild_id": "guild-3",
        "parent_chat_id": "engineering-chat",
    }


def test_gateway_kanban_create_subscription_uses_routed_source_profile(
    tmp_path, monkeypatch,
):
    """Slash-created subscriptions belong to the routed conversation profile."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="slash routed", assignee="peer")
    finally:
        conn.close()

    from types import SimpleNamespace
    from gateway.run import GatewayRunner
    import hermes_cli.kanban as kanban_cli

    monkeypatch.setattr(
        kanban_cli,
        "run_slash",
        lambda _text: f"Created {task_id}  (ready, assignee=peer)",
    )
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._kanban_notifier_profile = "default"
    runner._active_profile_name = lambda: "default"
    source = SimpleNamespace(
        platform=Platform.DISCORD,
        chat_id="engineering-chat",
        chat_type="channel",
        thread_id=None,
        user_id="user-1",
        user_id_alt=None,
        guild_id="engineering-guild",
        scope_id="engineering-guild",
        parent_chat_id=None,
        profile="yuki",
        message_id="message-1",
    )
    event = SimpleNamespace(text="/kanban create slash routed", source=source)

    output = asyncio.run(
        runner._handle_kanban_command(event)  # type: ignore[arg-type]
    )

    assert "Subscribed" in output or "subscribed" in output
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, task_id=task_id)
    finally:
        conn.close()
    assert len(subs) == 1
    assert subs[0]["notifier_profile"] == "yuki"
    assert subs[0]["delivery_metadata"]["guild_id"] == "engineering-guild"


def _make_routed_discord_runner(
    adapter,
    *,
    route_profile="yuki",
    route_enabled=True,
    served_profiles=("default", "yuki"),
):
    from gateway.config import GatewayConfig
    from gateway.profile_routing import parse_profile_routes

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: adapter}
    runner._profile_adapters = {}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = None
    setattr(runner, "_kanban_served_profiles", frozenset(served_profiles))
    runner._active_profile_name = lambda: "default"
    runner.config = GatewayConfig(
        multiplex_profiles=True,
        profile_routes=parse_profile_routes(
            [
                {
                    "name": "engineering-discord",
                    "platform": "discord",
                    "guild_id": "engineering-guild",
                    "chat_id": "engineering-chat",
                    "profile": route_profile,
                    "enabled": route_enabled,
                }
            ]
        ),
    )
    return runner


def _create_routed_discord_completion(
    *,
    board,
    notifier_profile: str | None = "yuki",
    chat_id="engineering-chat",
    chat_type="group",
    thread_id=None,
    guild_id="engineering-guild",
    include_guild_metadata=True,
):
    conn = kb.connect(board=board)
    try:
        tid = kb.create_task(
            conn,
            title="routed engineering task",
            assignee="worker",
            session_id=f"agent:yuki:discord:group:{chat_id}:creator",
        )
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="discord",
            chat_id=chat_id,
            chat_type=chat_type,
            thread_id=thread_id,
            user_id="creator",
            notifier_profile=notifier_profile,
            delivery_mode="notify+wake",
            delivery_metadata=(
                {"guild_id": guild_id} if include_guild_metadata else None
            ),
        )
        kb.complete_task(conn, tid, summary="route-aware completion")
        return tid
    finally:
        conn.close()


def _unseen_routed_event_count(task_id, chat_id="engineering-chat"):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=task_id,
            platform="discord",
            chat_id=chat_id,
            kinds=["completed"],
        )
        return len(events)
    finally:
        conn.close()


def test_routed_profile_uses_primary_discord_adapter_on_secondary_board_once(
    tmp_path, monkeypatch,
):
    """An exact profile route owns the primary transport for notify and wake."""
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.create_board("engineering", name="Engineering")
    tid = _create_routed_discord_completion(board="engineering")

    adapter = RecordingAdapter()
    runner = _make_routed_discord_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["chat_id"] == "engineering-chat"
    assert tid in adapter.sent[0]["text"]
    assert len(adapter.handled) == 1
    wake_source = adapter.handled[0].source
    assert wake_source.profile == "yuki"
    assert wake_source.guild_id == "engineering-guild"
    assert runner._adapter_for_source(wake_source) is adapter

    from gateway.session import build_session_key

    assert build_session_key(wake_source, profile=wake_source.profile) == (
        "agent:yuki:discord:group:engineering-chat:creator"
    )

    # The engineering-board cursor is the dedup boundary: a second tick neither
    # re-sends the visible completion nor wakes the creator again.
    runner = _make_routed_discord_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1


def test_disabled_profile_route_is_not_a_primary_transport_target(
    tmp_path, monkeypatch,
):
    """An explicitly disabled parsed route remains fail-closed."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    task_id = _create_routed_discord_completion(board=None)

    adapter = RecordingAdapter()
    runner = _make_routed_discord_runner(adapter, route_enabled=False)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
    assert adapter.handled == []
    assert _unseen_routed_event_count(task_id) == 1


def test_routed_profile_primary_adapter_denies_wrong_or_unmatched_route(
    tmp_path, monkeypatch,
):
    """A primary credential is never a general secondary-profile fallback."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "route-denied.db"))
    kb.init_db()
    wrong_tid = _create_routed_discord_completion(
        board=None,
        notifier_profile="other-profile",
    )
    unmatched_tid = _create_routed_discord_completion(
        board=None,
        notifier_profile="yuki",
        chat_id="unrouted-chat",
    )

    adapter = RecordingAdapter()
    runner = _make_routed_discord_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
    assert adapter.handled == []
    conn = kb.connect()
    try:
        for task_id, chat_id in (
            (wrong_tid, "engineering-chat"),
            (unmatched_tid, "unrouted-chat"),
        ):
            subs = kb.list_notify_subs(conn, task_id)
            assert len(subs) == 1
            _, events = kb.unseen_events_for_sub(
                conn,
                task_id=task_id,
                platform="discord",
                chat_id=chat_id,
                kinds=["completed"],
            )
            assert len(events) == 1, "denied delivery must remain retryable"
    finally:
        conn.close()


def test_primary_adapter_denies_ownerless_subscription_across_route(
    tmp_path, monkeypatch,
):
    """Legacy ownerless rows cannot bypass a destination's profile route."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    task_id = _create_routed_discord_completion(
        board=None,
        notifier_profile=None,
    )
    adapter = RecordingAdapter()
    runner = _make_routed_discord_runner(adapter)
    runner._kanban_dispatcher_lock_handle = object()

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
    assert adapter.handled == []
    assert _unseen_routed_event_count(task_id) == 1


def test_primary_adapter_denies_route_with_missing_guild_anchor(
    tmp_path, monkeypatch,
):
    """Legacy metadata cannot turn an ambiguous guild route into default ownership."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    task_id = _create_routed_discord_completion(
        board=None,
        notifier_profile="default",
        include_guild_metadata=False,
    )
    adapter = RecordingAdapter()
    runner = _make_routed_discord_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
    assert adapter.handled == []
    assert _unseen_routed_event_count(task_id) == 1


def test_primary_adapter_denies_thread_like_route_with_missing_parent_anchor(
    tmp_path, monkeypatch,
):
    """A legacy forum row cannot bypass its unknown parent-channel route."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    task_id = _create_routed_discord_completion(
        board=None,
        notifier_profile="default",
        chat_id="forum-post-1",
        chat_type="forum",
        thread_id=None,
    )
    adapter = RecordingAdapter()
    runner = _make_routed_discord_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
    assert adapter.handled == []
    assert _unseen_routed_event_count(task_id, chat_id="forum-post-1") == 1


def test_primary_adapter_denies_default_subscription_reassigned_by_route(
    tmp_path, monkeypatch,
):
    """A route to yuki prevents the default bot from delivering that destination."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    task_id = _create_routed_discord_completion(
        board=None,
        notifier_profile="default",
    )
    adapter = RecordingAdapter()
    runner = _make_routed_discord_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
    assert adapter.handled == []
    assert _unseen_routed_event_count(task_id) == 1


def test_routed_primary_fallback_denies_profile_with_secondary_registry(
    tmp_path, monkeypatch,
):
    """A partial secondary registry cannot silently fall back to the primary bot."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    task_id = _create_routed_discord_completion(board=None)
    adapter = RecordingAdapter()
    runner = _make_routed_discord_runner(adapter)
    setattr(
        runner,
        "_profile_adapters",
        {"yuki": {Platform.TELEGRAM: RecordingAdapter()}},
    )

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
    assert adapter.handled == []
    assert _unseen_routed_event_count(task_id) == 1


def test_routed_profile_primary_adapter_denies_unserved_route_target(
    tmp_path, monkeypatch,
):
    """A matching route cannot wake a profile this gateway does not serve."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "route-unserved.db"))
    kb.init_db()
    tid = _create_routed_discord_completion(board=None)

    adapter = RecordingAdapter()
    runner = _make_routed_discord_runner(
        adapter,
        served_profiles=("default",),
    )
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
    assert adapter.handled == []
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="discord",
            chat_id="engineering-chat",
            kinds=["completed"],
        )
    finally:
        conn.close()
    assert len(events) == 1


def test_true_secondary_adapter_still_owns_its_profile_subscription(
    tmp_path, monkeypatch,
):
    """A live secondary adapter wins; route-aware primary use is only fallback."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "secondary.db"))
    kb.init_db()
    tid = _create_routed_discord_completion(
        board=None,
        chat_id="secondary-only-chat",
    )

    primary_adapter = RecordingAdapter()
    secondary_adapter = RecordingAdapter()
    runner = _make_routed_discord_runner(primary_adapter)
    runner._profile_adapters = {  # type: ignore[assignment]
        "yuki": {Platform.DISCORD: secondary_adapter},
    }
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert primary_adapter.sent == []
    assert primary_adapter.handled == []
    assert len(secondary_adapter.sent) == 1
    assert tid in secondary_adapter.sent[0]["text"]
    assert len(secondary_adapter.handled) == 1
