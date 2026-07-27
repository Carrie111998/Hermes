from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

from hermes_cli import kanban_db as kb


NOTIFY_KINDS = [
    "completed",
    "blocked",
    "scheduled",
    "gave_up",
    "crashed",
    "timed_out",
    "block_loop_detected",
    "archived",
    "unblocked",
]


def _completed_subscription() -> str:
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="durable notify", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="chat-1",
            notifier_profile="default",
        )
        kb.complete_task(conn, task_id, summary="done")
        return task_id
    finally:
        conn.close()


def _claim(conn, task_id: str, owner: str, *, now: int):
    return kb.claim_notify_delivery_event(
        conn,
        task_id=task_id,
        platform="telegram",
        chat_id="chat-1",
        notifier_profile="default",
        delivery_owner=owner,
        lease_seconds=30,
        kinds=NOTIFY_KINDS,
        now=now,
    )


def _claim_prepare_and_die(task_id: str, mode: str, sink_path: str) -> None:
    """Child-process crash fixture; os._exit preserves no Python cleanup path."""
    conn = kb.connect()
    lock_path = kb._notify_delivery_lock_path(
        conn,
        task_id=task_id,
        platform="telegram",
        chat_id="chat-1",
    )
    handle = kb._acquire_notify_delivery_lock(lock_path, timeout_seconds=0.0)
    assert handle is not None
    claim = _claim(conn, task_id, "gateway-child", now=100)
    assert claim is not None
    [chunk] = kb.prepare_notify_delivery_chunks(
        conn,
        claim=claim,
        chunks=["process-boundary delivery"],
        now=100,
    )
    if mode == "after-send":
        assert kb.mark_notify_delivery_chunk_attempting(
            conn,
            delivery_key=chunk["delivery_key"],
            delivery_owner="gateway-child",
            now=101,
        )
        Path(sink_path).write_text(chunk["delivery_key"], encoding="utf-8")
    # Deliberately leak the connection + advisory-lock handle. The operating
    # system, not a finally block, must release ownership after process death.
    os._exit(0)


def _spawn_crashing_sender(task_id: str, mode: str, sink_path: Path) -> None:
    process = multiprocessing.get_context("spawn").Process(
        target=_claim_prepare_and_die,
        args=(task_id, mode, str(sink_path)),
    )
    process.start()
    process.join(timeout=15)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)
        raise AssertionError("crashing sender child did not terminate")
    assert process.exitcode == 0


def _take_over_expired_claim(conn, task_id: str):
    return kb.claim_notify_delivery_event_guarded(
        conn,
        task_id=task_id,
        platform="telegram",
        chat_id="chat-1",
        notifier_profile="default",
        delivery_owner="gateway-parent",
        lease_seconds=30,
        kinds=NOTIFY_KINDS,
        now=130,
    )


def _idempotent_sink_send(sink_path: Path, delivery_key: str) -> None:
    existing = (
        sink_path.read_text(encoding="utf-8").splitlines()
        if sink_path.exists()
        else []
    )
    if delivery_key not in existing:
        sink_path.write_text("\n".join((*existing, delivery_key)), encoding="utf-8")


def test_delivery_claim_is_durable_and_reclaimable_after_lease_expiry(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "claim.db"))
    kb.init_db()
    task_id = _completed_subscription()

    first = kb.connect()
    try:
        claim = _claim(first, task_id, "gateway-a", now=100)
        assert claim is not None
        assert claim["event"].kind == "completed"
        assert claim["delivery_owner"] == "gateway-a"
        [sub] = kb.list_notify_subs(first, task_id)
        assert sub["last_event_id"] == claim["old_cursor"]
        assert sub["notify_inflight_event_id"] == claim["event"].id
    finally:
        first.close()

    second = kb.connect()
    try:
        assert _claim(second, task_id, "gateway-b", now=129) is None
        takeover = _claim(second, task_id, "gateway-b", now=130)
        assert takeover is not None
        assert takeover["event"].id == claim["event"].id
        assert takeover["delivery_owner"] == "gateway-b"
    finally:
        second.close()


def test_process_death_before_send_releases_lock_and_delivers_once(
    tmp_path, monkeypatch,
):
    """A hard process exit after claim but before send cannot lose the event."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "death-before-send.db"))
    kb.init_db()
    task_id = _completed_subscription()
    sink_path = tmp_path / "before-send-sink.txt"

    _spawn_crashing_sender(task_id, "before-send", sink_path)
    assert not sink_path.exists()

    conn = kb.connect()
    try:
        takeover = _take_over_expired_claim(conn, task_id)
        assert takeover is not None
        [chunk] = kb.pending_notify_delivery_chunks(conn, claim=takeover)
        assert chunk["attempts"] == 0
        _idempotent_sink_send(sink_path, chunk["delivery_key"])
        assert kb.mark_notify_delivery_chunk_attempting(
            conn,
            delivery_key=chunk["delivery_key"],
            delivery_owner="gateway-parent",
            now=131,
        )
        assert kb.ack_notify_delivery_chunk(
            conn,
            delivery_key=chunk["delivery_key"],
            delivery_owner="gateway-parent",
            now=132,
        )
        assert kb.ack_notify_delivery_event(
            conn,
            claim=takeover,
            delivery_owner="gateway-parent",
            now=133,
        )
    finally:
        conn.close()

    assert sink_path.read_text(encoding="utf-8").splitlines() == [
        chunk["delivery_key"]
    ]


def test_process_death_after_send_before_ack_reuses_stable_delivery_identity(
    tmp_path, monkeypatch,
):
    """Takeover retries an indeterminate send with the same idempotency key."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "death-before-ack.db"))
    kb.init_db()
    task_id = _completed_subscription()
    sink_path = tmp_path / "before-ack-sink.txt"

    _spawn_crashing_sender(task_id, "after-send", sink_path)
    [sent_key] = sink_path.read_text(encoding="utf-8").splitlines()

    conn = kb.connect()
    try:
        takeover = _take_over_expired_claim(conn, task_id)
        assert takeover is not None
        [chunk] = kb.pending_notify_delivery_chunks(conn, claim=takeover)
        assert chunk["delivery_key"] == sent_key
        assert chunk["attempts"] == 1
        # This fake transport models a provider accepting Idempotency-Key: the
        # takeover attempt is safe because the durable identity is unchanged.
        _idempotent_sink_send(sink_path, chunk["delivery_key"])
        assert kb.mark_notify_delivery_chunk_attempting(
            conn,
            delivery_key=chunk["delivery_key"],
            delivery_owner="gateway-parent",
            now=131,
        )
        assert kb.ack_notify_delivery_chunk(
            conn,
            delivery_key=chunk["delivery_key"],
            delivery_owner="gateway-parent",
            now=132,
        )
        assert kb.ack_notify_delivery_event(
            conn,
            claim=takeover,
            delivery_owner="gateway-parent",
            now=133,
        )
    finally:
        conn.close()

    assert sink_path.read_text(encoding="utf-8").splitlines() == [sent_key]


def test_guarded_takeover_waits_for_inflight_sender_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "guarded-claim.db"))
    kb.init_db()
    task_id = _completed_subscription()
    conn = kb.connect()
    try:
        first = _claim(conn, task_id, "gateway-a", now=100)
        lock_path = kb._notify_delivery_lock_path(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="chat-1",
        )
        handle = kb._acquire_notify_delivery_lock(
            lock_path,
            timeout_seconds=0.0,
        )
        try:
            assert kb.claim_notify_delivery_event_guarded(
                conn,
                task_id=task_id,
                platform="telegram",
                chat_id="chat-1",
                notifier_profile="default",
                delivery_owner="gateway-b",
                lease_seconds=30,
                kinds=NOTIFY_KINDS,
                now=200,
            ) is None
        finally:
            kb._release_notify_delivery_lock(handle)

        takeover = kb.claim_notify_delivery_event_guarded(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="chat-1",
            notifier_profile="default",
            delivery_owner="gateway-b",
            lease_seconds=30,
            kinds=NOTIFY_KINDS,
            now=200,
        )
        assert takeover is not None
        assert takeover["event"].id == first["event"].id
    finally:
        conn.close()


def test_chunk_acknowledgements_survive_process_boundary_and_retain_tail(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "chunks.db"))
    kb.init_db()
    task_id = _completed_subscription()

    first = kb.connect()
    try:
        claim = _claim(first, task_id, "gateway-a", now=100)
        chunks = kb.prepare_notify_delivery_chunks(
            first,
            claim=claim,
            chunks=["first", "second", "third"],
        )
        assert [chunk["chunk_index"] for chunk in chunks] == [0, 1, 2]
        assert len({chunk["delivery_key"] for chunk in chunks}) == 3
        assert kb.mark_notify_delivery_chunk_attempting(
            first,
            delivery_key=chunks[0]["delivery_key"],
            delivery_owner="gateway-a",
            now=101,
        )
        assert kb.ack_notify_delivery_chunk(
            first,
            delivery_key=chunks[0]["delivery_key"],
            delivery_owner="gateway-a",
            now=102,
        )
    finally:
        first.close()

    second = kb.connect()
    try:
        takeover = _claim(second, task_id, "gateway-b", now=130)
        pending = kb.pending_notify_delivery_chunks(second, claim=takeover)
        assert [chunk["content"] for chunk in pending] == ["second", "third"]
        assert [chunk["attempts"] for chunk in pending] == [0, 0]

        for chunk in pending:
            assert kb.mark_notify_delivery_chunk_attempting(
                second,
                delivery_key=chunk["delivery_key"],
                delivery_owner="gateway-b",
                now=131,
            )
            assert kb.ack_notify_delivery_chunk(
                second,
                delivery_key=chunk["delivery_key"],
                delivery_owner="gateway-b",
                now=132,
            )

        assert kb.ack_notify_delivery_event(
            second,
            claim=takeover,
            delivery_owner="gateway-b",
            now=133,
        )
        [sub] = kb.list_notify_subs(second, task_id)
        assert sub["last_event_id"] == takeover["event"].id
        assert sub["notify_inflight_event_id"] is None
    finally:
        second.close()


def test_event_ack_rejects_unacknowledged_chunks_and_stale_owner(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "ack-cas.db"))
    kb.init_db()
    task_id = _completed_subscription()

    conn = kb.connect()
    try:
        claim = _claim(conn, task_id, "gateway-a", now=100)
        [chunk] = kb.prepare_notify_delivery_chunks(
            conn,
            claim=claim,
            chunks=["only"],
        )
        assert not kb.ack_notify_delivery_event(
            conn,
            claim=claim,
            delivery_owner="gateway-a",
            now=101,
        )
        assert not kb.ack_notify_delivery_chunk(
            conn,
            delivery_key=chunk["delivery_key"],
            delivery_owner="gateway-b",
            now=102,
        )
        [sub] = kb.list_notify_subs(conn, task_id)
        assert sub["last_event_id"] == claim["old_cursor"]
    finally:
        conn.close()


def test_preparing_same_event_is_idempotent_and_keeps_original_chunks(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "idempotent.db"))
    kb.init_db()
    task_id = _completed_subscription()

    conn = kb.connect()
    try:
        claim = _claim(conn, task_id, "gateway-a", now=100)
        original = kb.prepare_notify_delivery_chunks(
            conn,
            claim=claim,
            chunks=["first", "second"],
        )
        repeated = kb.prepare_notify_delivery_chunks(
            conn,
            claim=claim,
            chunks=["changed formatter output"],
        )
        assert repeated == original
    finally:
        conn.close()


def test_profile_transfer_replays_full_event_with_new_delivery_keys(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "transfer-tail.db"))
    kb.init_db()
    task_id = _completed_subscription()
    conn = kb.connect()
    try:
        old_claim = _claim(conn, task_id, "old-process", now=100)
        old_chunks = kb.prepare_notify_delivery_chunks(
            conn,
            claim=old_claim,
            chunks=["first", "second"],
            now=100,
        )
        assert kb.mark_notify_delivery_chunk_attempting(
            conn,
            delivery_key=old_chunks[0]["delivery_key"],
            delivery_owner="old-process",
            now=101,
        )
        assert kb.ack_notify_delivery_chunk(
            conn,
            delivery_key=old_chunks[0]["delivery_key"],
            delivery_owner="old-process",
            now=102,
        )

        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="chat-1",
            notifier_profile="reviewer",
        )
        new_claim = kb.claim_notify_delivery_event(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="chat-1",
            notifier_profile="reviewer",
            delivery_owner="new-process",
            lease_seconds=30,
            kinds=NOTIFY_KINDS,
            now=103,
        )
        new_chunks = kb.prepare_notify_delivery_chunks(
            conn,
            claim=new_claim,
            chunks=["first", "second"],
            now=103,
        )

        assert [item["content"] for item in new_chunks] == ["first", "second"]
        assert all(item["state"] == "pending" for item in new_chunks)
        assert [item["delivery_key"] for item in new_chunks] != [
            item["delivery_key"] for item in old_chunks
        ]
    finally:
        conn.close()
