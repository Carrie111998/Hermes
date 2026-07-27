"""Per-profile Kanban notifier ownership and adapter routing regressions."""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.kanban_watchers import (
    _acquire_singleton_lock,
    _notifier_profile_lock_path,
    _release_singleton_lock,
    _subscription_owner_profile,
)
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


async def _run_one_tick(monkeypatch, runner: GatewayRunner) -> None:
    """Run one notifier poll after bypassing its startup delay."""
    sleeps = 0

    async def _sleep(_delay):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 2:
            runner._running = False

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _completed_subscription(profile: str) -> str:
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title=f"{profile} task", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id=f"{profile}-chat",
            notifier_profile=profile,
        )
        kb.complete_task(conn, task_id, summary="done")
        return task_id
    finally:
        conn.close()


def _runner(profile: str, *, primary=None, secondary=None) -> GatewayRunner:
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner._active_profile_name = lambda: profile
    runner._kanban_notifier_profile = profile
    runner._kanban_sub_fail_counts = {}
    runner.adapters = {Platform.TELEGRAM: primary} if primary is not None else {}
    runner._profile_adapters = secondary or {}
    return runner


def test_notifier_lock_is_exclusive_per_profile_but_not_across_profiles(
    tmp_path, monkeypatch
):
    """Duplicate same-profile gateways coordinate without starving another profile."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))

    default_path = _notifier_profile_lock_path("default")
    writer_path = _notifier_profile_lock_path("writer")
    assert default_path != writer_path

    default_handle, default_state = _acquire_singleton_lock(default_path)
    duplicate_handle, duplicate_state = _acquire_singleton_lock(default_path)
    writer_handle, writer_state = _acquire_singleton_lock(writer_path)
    try:
        assert default_state == "held"
        assert duplicate_state == "contended"
        assert duplicate_handle is None
        assert writer_state == "held"
    finally:
        _release_singleton_lock(default_handle)
        _release_singleton_lock(writer_handle)


def test_reregistering_subscription_transfers_notifier_ownership(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "transfer.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="transfer owner", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="same-chat",
            notifier_profile="profile-a",
        )
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="same-chat",
            notifier_profile="profile-b",
        )
        subs = kb.list_notify_subs(conn, task_id=task_id)
    finally:
        conn.close()

    assert len(subs) == 1
    assert subs[0]["notifier_profile"] == "profile-b"


def test_legacy_blank_subscription_owner_maps_to_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "legacy.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="legacy owner", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="legacy-chat",
        )
        sub = kb.list_notify_subs(conn, task_id=task_id)[0]
    finally:
        conn.close()

    assert not sub["notifier_profile"]
    assert _subscription_owner_profile(sub) == "default"


def test_lock_loser_does_not_open_or_enumerate_board_dbs(tmp_path, monkeypatch):
    """A duplicate gateway waits for ownership without touching board SQLite."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))

    held, state = _acquire_singleton_lock(_notifier_profile_lock_path("default"))
    assert state == "held"
    adapter = MagicMock()
    runner = _runner("default", primary=adapter)
    try:
        with (
            patch.object(kb, "list_boards") as list_boards,
            patch.object(kb, "connect") as connect,
        ):
            asyncio.run(_run_one_tick(monkeypatch, runner))
        list_boards.assert_not_called()
        connect.assert_not_called()
    finally:
        _release_singleton_lock(held)


def test_adapter_loss_releases_profile_for_bounded_failover(tmp_path, monkeypatch):
    """A disconnected owner yields; one peer acquires and keeps exclusivity."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    first = _runner("default", primary=MagicMock())
    second = _runner("default", primary=MagicMock())
    first_locks: dict[str, object] = {}
    second_locks: dict[str, object] = {}
    try:
        first_serviceable = first._kanban_notifier_adapters_by_profile()
        first._sync_kanban_notifier_locks(first_locks, first_serviceable)
        second._sync_kanban_notifier_locks(
            second_locks,
            second._kanban_notifier_adapters_by_profile(),
        )
        assert set(first_locks) == {"default"}
        assert second_locks == {}

        first.adapters.clear()
        first._sync_kanban_notifier_locks(
            first_locks,
            first._kanban_notifier_adapters_by_profile(),
        )
        assert first_locks == {}

        second._sync_kanban_notifier_locks(
            second_locks,
            second._kanban_notifier_adapters_by_profile(),
        )
        assert set(second_locks) == {"default"}

        first.adapters[Platform.TELEGRAM] = MagicMock()
        first._sync_kanban_notifier_locks(
            first_locks,
            first._kanban_notifier_adapters_by_profile(),
        )
        assert first_locks == {}
    finally:
        for handle in (*first_locks.values(), *second_locks.values()):
            _release_singleton_lock(handle)


def test_disconnected_transport_does_not_hold_profile_lock(tmp_path, monkeypatch):
    """A reconnect loop without a usable transport must yield notifier ownership."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    disconnected = MagicMock()
    disconnected._running = True
    disconnected.is_connected = False
    runner = _runner("default", primary=disconnected)
    locks: dict[str, object] = {}

    runner._sync_kanban_notifier_locks(
        locks,
        runner._kanban_notifier_adapters_by_profile(),
    )

    assert locks == {}


def test_transferred_owner_blocks_stale_cursor_mutation(tmp_path, monkeypatch):
    """A prior transport owner cannot rewind or delete the new owner's row."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "transfer-race.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="transfer race", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="same-chat",
            notifier_profile="profile-a",
        )
        kb.complete_task(conn, task_id, summary="done")
        old_cursor, claimed_cursor, events = kb.claim_unseen_events_for_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="same-chat",
            notifier_profile="profile-a",
            kinds=["completed"],
            limit=1,
        )
        assert [event.kind for event in events] == ["completed"]

        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="same-chat",
            notifier_profile="profile-b",
        )

        assert kb.rewind_notify_cursor(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="same-chat",
            notifier_profile="profile-a",
            claimed_cursor=claimed_cursor,
            old_cursor=old_cursor,
        ) is False
        assert kb.remove_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="same-chat",
            notifier_profile="profile-a",
        ) is False
        [sub] = kb.list_notify_subs(conn, task_id=task_id)
        assert sub["notifier_profile"] == "profile-b"
        assert sub["last_event_id"] == old_cursor
        _, new_cursor, replay = kb.claim_unseen_events_for_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="same-chat",
            notifier_profile="profile-b",
            kinds=["completed"],
            limit=1,
        )
        assert new_cursor == claimed_cursor
        assert [event.kind for event in replay] == ["completed"]
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="same-chat",
            notifier_profile="profile-b",
        )
        [same_owner] = kb.list_notify_subs(conn, task_id=task_id)
        assert same_owner["last_event_id"] == claimed_cursor
    finally:
        conn.close()


def test_inflight_transfer_routes_event_only_to_new_owner(tmp_path, monkeypatch):
    """A transfer between claim and send replays through the replacement bot."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    kb.init_db()
    task_id = _completed_subscription("profile-a")

    old_adapter = MagicMock()
    old_adapter.send = AsyncMock(return_value=SendResult(success=True))
    old_runner = _runner("profile-a", primary=old_adapter)
    original_acquire = old_runner._kanban_acquire_delivery_lock
    transferred = False

    def transfer_then_acquire(sub, board):
        nonlocal transferred
        if not transferred:
            transferred = True
            conn = kb.connect(board=board)
            try:
                kb.add_notify_sub(
                    conn,
                    task_id=task_id,
                    platform="telegram",
                    chat_id="profile-a-chat",
                    notifier_profile="profile-b",
                )
            finally:
                conn.close()
        return original_acquire(sub, board)

    old_runner._kanban_acquire_delivery_lock = transfer_then_acquire
    asyncio.run(_run_one_tick(monkeypatch, old_runner))
    old_adapter.send.assert_not_called()

    new_adapter = MagicMock()
    new_adapter.send = AsyncMock(return_value=SendResult(success=True))
    new_runner = _runner("profile-b", primary=new_adapter)
    asyncio.run(_run_one_tick(monkeypatch, new_runner))

    new_adapter.send.assert_called_once()
    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn, task_id=task_id) == []
    finally:
        conn.close()


class _BlockingSendAdapter:
    def __init__(self):
        self.send_started = threading.Event()
        self.release_send = threading.Event()
        self.messages: list[str] = []

    async def send(self, _chat_id, text, metadata=None):
        self.messages.append(text)
        self.send_started.set()
        released = await asyncio.to_thread(self.release_send.wait, 5)
        assert released, "test timed out waiting to release the in-flight send"
        return SendResult(success=True)


def test_owner_transfer_waits_for_inflight_send_without_replay(tmp_path, monkeypatch):
    """A transfer after owner validation cannot race or replay a delivered event."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="post-check transfer", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="shared-chat",
            notifier_profile="profile-a",
        )
        assert kb.block_task(conn, task_id, reason="waiting for input")
    finally:
        conn.close()

    old_adapter = _BlockingSendAdapter()
    old_runner = _runner("profile-a", primary=old_adapter)
    transfer_finished = threading.Event()

    def transfer_owner() -> None:
        transfer_conn = kb.connect()
        try:
            kb.add_notify_sub(
                transfer_conn,
                task_id=task_id,
                platform="telegram",
                chat_id="shared-chat",
                notifier_profile="profile-b",
            )
        finally:
            transfer_conn.close()
            transfer_finished.set()

    async def run_delivery_and_transfer() -> None:
        watcher = asyncio.create_task(_run_one_tick(monkeypatch, old_runner))
        assert await asyncio.to_thread(old_adapter.send_started.wait, 5)
        transfer = asyncio.create_task(asyncio.to_thread(transfer_owner))
        await asyncio.to_thread(time.sleep, 0.1)
        assert not transfer_finished.is_set(), (
            "ownership transfer must wait until the in-flight send is acknowledged"
        )
        old_adapter.release_send.set()
        await asyncio.wait_for(asyncio.gather(watcher, transfer), timeout=10)

    asyncio.run(run_delivery_and_transfer())
    assert len(old_adapter.messages) == 1

    new_adapter = MagicMock()
    new_adapter.send = AsyncMock(return_value=SendResult(success=True))
    new_runner = _runner("profile-b", primary=new_adapter)
    asyncio.run(_run_one_tick(monkeypatch, new_runner))

    new_adapter.send.assert_not_called()
    conn = kb.connect()
    try:
        [sub] = kb.list_notify_subs(conn, task_id=task_id)
        assert sub["notifier_profile"] == "profile-b"
    finally:
        conn.close()


def test_named_standalone_profile_delivers_via_its_primary_adapter(
    tmp_path, monkeypatch
):
    """A named standalone gateway resolves its own stamped sub to self.adapters."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    kb.init_db()
    task_id = _completed_subscription("writer")
    adapter = MagicMock()
    adapter.send = AsyncMock(return_value=SendResult(success=True))
    runner = _runner("writer", primary=adapter)

    asyncio.run(_run_one_tick(monkeypatch, runner))

    adapter.send.assert_called_once()
    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn, task_id) == []
    finally:
        conn.close()


def test_multiplex_gateway_routes_secondary_profile_to_secondary_adapter(
    tmp_path, monkeypatch
):
    """A multiplex owner sends a secondary-profile sub through that profile's adapter."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    kb.init_db()
    task_id = _completed_subscription("writer")
    primary = MagicMock()
    primary.send = AsyncMock(return_value=SendResult(success=True))
    secondary = MagicMock()
    secondary.send = AsyncMock(return_value=SendResult(success=True))
    runner = _runner(
        "default",
        primary=primary,
        secondary={"writer": {Platform.TELEGRAM: secondary}},
    )

    asyncio.run(_run_one_tick(monkeypatch, runner))

    primary.send.assert_not_called()
    secondary.send.assert_called_once()
    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn, task_id) == []
    finally:
        conn.close()


class _ChunkCapturingAdapter:
    MAX_MESSAGE_LENGTH = 120
    splits_long_messages = False
    truncate_message = staticmethod(BasePlatformAdapter.truncate_message)

    def __init__(self):
        self.messages: list[str] = []

    async def send(self, _chat_id, text, metadata=None):
        self.messages.append(text)
        return SendResult(success=True)


def _review_brief(task_id: str, marker: str) -> str:
    return "\n".join((
        f"ASK: approve the {marker.lower()} protected operation",
        "WHY GATED: " + ("human-floor evidence must remain complete; " * 6),
        "SCOPE: one reversible notifier-only operation",
        "EVIDENCE: dry-run output and exact command transcript",
        "RISKS: stale credentials may require a second review",
        "WINDOW: 30 minutes after approval",
        "ROLLBACK: restore the prior configuration snapshot",
        f"REPLY: APPROVE {task_id} to proceed or VETO {task_id} to cancel",
        f"APPROVE {task_id}",
        f"VETO {task_id}",
    ))


def test_blocked_and_scheduled_review_briefs_are_complete_and_chunk_safe(
    tmp_path,
    monkeypatch,
):
    """Human-floor notifications retain every review field beyond 160 chars."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    kb.init_db()
    conn = kb.connect()
    try:
        blocked = kb.create_task(conn, title="blocked brief", assignee="worker")
        scheduled = kb.create_task(conn, title="scheduled brief", assignee="worker")
        for task_id in (blocked, scheduled):
            kb.add_notify_sub(
                conn,
                task_id=task_id,
                platform="telegram",
                chat_id="review-chat",
                notifier_profile="default",
            )
        assert kb.block_task(
            conn,
            blocked,
            reason=_review_brief(blocked, "BLOCKED"),
            kind="needs_input",
        )
        assert kb.schedule_task(
            conn,
            scheduled,
            reason=_review_brief(scheduled, "SCHEDULED"),
        )
    finally:
        conn.close()

    adapter = _ChunkCapturingAdapter()
    runner = _runner("default", primary=adapter)
    asyncio.run(_run_one_tick(monkeypatch, runner))

    assert adapter.messages
    assert all(
        len(message) <= adapter.MAX_MESSAGE_LENGTH for message in adapter.messages
    )
    delivered = "\n".join(adapter.messages)
    for field in (
        "ASK:",
        "WHY GATED:",
        "SCOPE:",
        "EVIDENCE:",
        "RISKS:",
        "WINDOW:",
        "ROLLBACK:",
        "REPLY:",
        f"APPROVE {blocked}",
        f"VETO {blocked}",
        f"APPROVE {scheduled}",
        f"VETO {scheduled}",
    ):
        assert field in delivered


class _FailCompletedOnceAdapter:
    def __init__(self):
        self.messages: list[str] = []
        self.failed = False

    async def send(self, _chat_id, text, metadata=None):
        self.messages.append(text)
        if " done" in text and not self.failed:
            self.failed = True
            return SendResult(success=False, error="transient completion failure")
        return SendResult(success=True)


def test_partial_batch_retry_does_not_redeliver_earlier_success(tmp_path, monkeypatch):
    """A later failed event rewinds only itself, not prior delivered events."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="ordered events", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="ordered-chat",
            notifier_profile="default",
        )
        assert kb.schedule_task(conn, task_id, reason="WINDOW: tonight")
        assert kb.unblock_task(conn, task_id)
        assert kb.complete_task(conn, task_id, summary="finished")
    finally:
        conn.close()

    adapter = _FailCompletedOnceAdapter()
    runner = _runner("default", primary=adapter)
    # scheduled success → silent unblocked cursor → failed completion → retry
    for _ in range(4):
        runner._running = True
        asyncio.run(_run_one_tick(monkeypatch, runner))

    scheduled_messages = [m for m in adapter.messages if "scheduled" in m]
    completed_messages = [m for m in adapter.messages if " done" in m]
    assert len(scheduled_messages) == 1
    assert len(completed_messages) == 2, adapter.messages
    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn, task_id=task_id) == []
    finally:
        conn.close()
