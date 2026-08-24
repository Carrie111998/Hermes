"""Behavior tests for the generic leased-event and inbox substrates."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from gateway import durable_events as durable


NOW = 10_000.0
STREAM = "bot-relay-v2"
ROUTE = "courier:desktop-a"
INBOX = "bot-chat-v2"
LANE = "target-profile:analyst"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "private-state" / "state.db"


def _enqueue(
    db_path: Path,
    event_id: str,
    *,
    stream: str = STREAM,
    route: str = ROUTE,
    expires_at: float = NOW + 100,
    now: float = NOW,
    payload: dict | None = None,
) -> dict:
    return durable.enqueue(
        db_path,
        stream,
        event_id,
        payload or {"command": "status", "event_id": event_id},
        route,
        expires_at,
        now=now,
    )


def _claim_one(
    db_path: Path,
    *,
    event_id: str,
    owner: str = "desktop-a",
    stream: str = STREAM,
    route: str = ROUTE,
    now: float = NOW,
    lease_seconds: float = 10,
) -> dict:
    rows = durable.claim(
        db_path,
        stream,
        route,
        owner,
        limit=10,
        lease_seconds=lease_seconds,
        now=now,
    )
    matches = [row for row in rows if row["event_id"] == event_id]
    assert len(matches) == 1
    return matches[0]


def _payload_hash(value: bytes = b"payload") -> str:
    return hashlib.sha256(value).hexdigest()


def test_enqueue_is_immutable_and_adopts_original_deadline(db_path: Path) -> None:
    first = _enqueue(db_path, "event-1", expires_at=NOW + 20)
    assert first["idempotent"] is False
    assert first["state"] == "queued"
    assert first["attempts"] == first["generation"] == 0

    duplicate = durable.enqueue(
        db_path,
        STREAM,
        "event-1",
        {"event_id": "event-1", "command": "status"},
        ROUTE,
        NOW + 500,
        now=NOW + 1,
    )
    assert duplicate["idempotent"] is True
    assert duplicate["expires_at"] == NOW + 20
    assert duplicate["payload"] == first["payload"]

    # A duplicate remains readable after its proposed/original deadline.  The
    # first insert owns the deadline; retries cannot extend/reset it.
    late_duplicate = durable.enqueue(
        db_path,
        STREAM,
        "event-1",
        first["payload"],
        ROUTE,
        NOW + 20,
        now=NOW + 30,
    )
    assert late_duplicate["idempotent"] is True
    assert late_duplicate["expires_at"] == NOW + 20

    with pytest.raises(durable.EventConflict):
        durable.enqueue(
            db_path,
            STREAM,
            "event-1",
            {"command": "different"},
            ROUTE,
            NOW + 500,
            now=NOW + 1,
        )
    with pytest.raises(durable.EventConflict):
        durable.enqueue(
            db_path,
            STREAM,
            "event-1",
            first["payload"],
            "courier:desktop-b",
            NOW + 500,
            now=NOW + 1,
        )
    with pytest.raises(ValueError, match="future"):
        _enqueue(db_path, "already-dead", expires_at=NOW, now=NOW)


def test_claim_race_reclaim_and_stale_fencing(db_path: Path) -> None:
    current = time.time()
    durable.enqueue(
        db_path,
        STREAM,
        "race-event",
        {"command": "race"},
        ROUTE,
        current + 120,
        now=current,
    )
    workers = 8
    barrier = threading.Barrier(workers)

    def race(index: int) -> tuple[str, list[dict]]:
        owner = f"desktop-race-{index}"
        barrier.wait(timeout=10)
        return owner, durable.claim(
            db_path,
            STREAM,
            ROUTE,
            owner,
            limit=1,
            lease_seconds=10,
            now=current,
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(race, range(workers)))

    winners = [(owner, rows[0]) for owner, rows in results if rows]
    assert len(winners) == 1
    first_owner, first = winners[0]
    assert first["attempts"] == first["generation"] == 1

    reclaimed = durable.claim(
        db_path,
        STREAM,
        ROUTE,
        "desktop-reclaimer",
        limit=1,
        lease_seconds=20,
        now=current + 10,
    )
    assert len(reclaimed) == 1
    second = reclaimed[0]
    assert second["event_id"] == "race-event"
    assert second["attempts"] == second["generation"] == 2

    stale_calls = (
        lambda: durable.renew(
            db_path,
            STREAM,
            "race-event",
            first_owner,
            first["lease_token"],
            first["generation"],
            lease_seconds=10,
            now=current + 10,
        ),
        lambda: durable.ack(
            db_path,
            STREAM,
            "race-event",
            first_owner,
            first["lease_token"],
            first["generation"],
            {"status": "ok"},
            durable.json_digest({"status": "ok"}),
            now=current + 10,
        ),
        lambda: durable.nack(
            db_path,
            STREAM,
            "race-event",
            first_owner,
            first["lease_token"],
            first["generation"],
            "stale",
            True,
            retry_after_seconds=1,
            max_attempts=3,
            now=current + 10,
        ),
    )
    for stale_call in stale_calls:
        with pytest.raises(durable.LeaseMismatch) as error:
            stale_call()
        assert str(error.value) == "lease does not match"

    outcome = {"status": "delivered", "reply": "ready"}
    settled = durable.ack(
        db_path,
        STREAM,
        "race-event",
        "desktop-reclaimer",
        second["lease_token"],
        second["generation"],
        outcome,
        durable.json_digest(outcome),
        now=current + 11,
    )
    assert settled["state"] == "acked"
    assert settled["idempotent"] is False

    # Exact terminal ACK replay is accepted even after both clocks elapsed.
    replay = durable.ack(
        db_path,
        STREAM,
        "race-event",
        "desktop-reclaimer",
        second["lease_token"],
        second["generation"],
        outcome,
        durable.json_digest(outcome),
        now=current + 500,
    )
    assert replay["idempotent"] is True
    assert (
        durable.get_event(db_path, STREAM, "race-event", now=current + 12)["outcome"]
        == outcome
    )

    with pytest.raises(durable.LeaseMismatch):
        durable.ack(
            db_path,
            STREAM,
            "race-event",
            "desktop-reclaimer",
            second["lease_token"],
            second["generation"],
            {"status": "different"},
            durable.json_digest({"status": "different"}),
            now=current + 12,
        )

    # Only keyed hashes, never bearer/owner material, are persisted.
    conn = sqlite3.connect(db_path)
    try:
        stored = conn.execute(
            """SELECT lease_owner_hash, lease_token_hash
               FROM durable_events WHERE stream=? AND event_id=?""",
            (STREAM, "race-event"),
        ).fetchone()
    finally:
        conn.close()
    assert stored is not None
    assert all(len(value) == 64 for value in stored)
    assert first_owner not in stored
    assert first["lease_token"] not in stored
    assert "desktop-reclaimer" not in stored
    assert second["lease_token"] not in stored


def test_renew_and_nack_preserve_attempt_accounting(db_path: Path) -> None:
    _enqueue(db_path, "retry-event")
    first = _claim_one(db_path, event_id="retry-event", lease_seconds=10)
    queued = durable.nack(
        db_path,
        STREAM,
        "retry-event",
        "desktop-a",
        first["lease_token"],
        first["generation"],
        "temporary outage",
        True,
        retry_after_seconds=5,
        max_attempts=3,
        now=NOW + 1,
    )
    assert queued["state"] == "queued"
    assert queued["available_at"] == NOW + 6
    assert queued["attempts"] == queued["generation"] == 1
    assert (
        durable.claim(
            db_path,
            STREAM,
            ROUTE,
            "desktop-a",
            10,
            10,
            now=NOW + 5,
        )
        == []
    )

    second = _claim_one(
        db_path,
        event_id="retry-event",
        now=NOW + 6,
        lease_seconds=10,
    )
    assert second["attempts"] == second["generation"] == 2
    renewed = durable.renew(
        db_path,
        STREAM,
        "retry-event",
        "desktop-a",
        second["lease_token"],
        second["generation"],
        lease_seconds=20,
        now=NOW + 7,
    )
    assert renewed["attempts"] == renewed["generation"] == 2
    assert renewed["lease_expires_at"] == NOW + 27

    failed = durable.nack(
        db_path,
        STREAM,
        "retry-event",
        "desktop-a",
        second["lease_token"],
        second["generation"],
        "permanent rejection",
        False,
        retry_after_seconds=0,
        max_attempts=3,
        now=NOW + 8,
    )
    assert failed["state"] == "failed"
    assert failed["outcome"]["reason"] == "non_retryable"

    _enqueue(db_path, "poison-event")
    poison = _claim_one(db_path, event_id="poison-event")
    exhausted = durable.nack(
        db_path,
        STREAM,
        "poison-event",
        "desktop-a",
        poison["lease_token"],
        poison["generation"],
        "still broken",
        True,
        retry_after_seconds=1,
        max_attempts=1,
        now=NOW + 1,
    )
    assert exhausted["state"] == "failed"
    assert exhausted["outcome"]["reason"] == "max_attempts_exhausted"

    _enqueue(db_path, "deadline-event", expires_at=NOW + 5)
    deadline = _claim_one(
        db_path,
        event_id="deadline-event",
        lease_seconds=5,
    )
    expired = durable.nack(
        db_path,
        STREAM,
        "deadline-event",
        "desktop-a",
        deadline["lease_token"],
        deadline["generation"],
        "retry would miss deadline",
        True,
        retry_after_seconds=4,
        max_attempts=3,
        now=NOW + 1,
    )
    assert expired["state"] == "expired"
    assert expired["outcome"] == {
        "status": "expired",
        "reason": "event_deadline_exceeded",
    }


def test_get_event_terminalizes_deadline_for_waiter(db_path: Path) -> None:
    _enqueue(db_path, "waiter-event", expires_at=NOW + 2)
    waiting = durable.get_event(db_path, STREAM, "waiter-event", now=NOW + 1)
    assert waiting is not None and waiting["state"] == "queued"

    terminal = durable.get_event(db_path, STREAM, "waiter-event", now=NOW + 2)
    assert terminal is not None
    assert terminal["state"] == "expired"
    assert terminal["outcome"]["reason"] == "event_deadline_exceeded"


def test_cleanup_is_scoped_and_never_deletes_active_rows(db_path: Path) -> None:
    _enqueue(
        db_path,
        "expired-queued",
        route="queued-only-route",
        expires_at=NOW + 5,
    )
    _enqueue(db_path, "expired-leased", expires_at=NOW + 5)
    _claim_one(
        db_path,
        event_id="expired-leased",
        lease_seconds=5,
    )
    _enqueue(
        db_path,
        "future-active",
        route="future-route",
        expires_at=NOW + 100,
    )

    terminal_route = "terminal-route"
    _enqueue(
        db_path,
        "old-terminal",
        route=terminal_route,
        expires_at=NOW + 100,
    )
    old = _claim_one(
        db_path,
        event_id="old-terminal",
        route=terminal_route,
    )
    old_outcome = {"status": "ok"}
    durable.ack(
        db_path,
        STREAM,
        "old-terminal",
        "desktop-a",
        old["lease_token"],
        old["generation"],
        old_outcome,
        durable.json_digest(old_outcome),
        now=NOW + 2,
    )

    other_stream = "unrelated-consumer"
    _enqueue(db_path, "other-terminal", stream=other_stream)
    other = _claim_one(
        db_path,
        event_id="other-terminal",
        stream=other_stream,
    )
    durable.ack(
        db_path,
        other_stream,
        "other-terminal",
        "desktop-a",
        other["lease_token"],
        other["generation"],
        old_outcome,
        durable.json_digest(old_outcome),
        now=NOW + 2,
    )

    counts = durable.cleanup(
        db_path,
        stream=STREAM,
        retention_seconds=15,
        now=NOW + 20,
    )
    assert counts == {
        "events_expired": 2,
        "inbox_indeterminate": 0,
        "events_deleted": 1,
        "inbox_deleted": 0,
    }
    assert durable.get_event(db_path, STREAM, "old-terminal", now=NOW + 20) is None
    assert (
        durable.get_event(db_path, STREAM, "future-active", now=NOW + 20)["state"]
        == "queued"
    )
    for event_id in ("expired-queued", "expired-leased"):
        row = durable.get_event(db_path, STREAM, event_id, now=NOW + 20)
        assert row is not None and row["state"] == "expired"
        assert row["outcome"]["reason"] == "event_deadline_exceeded"
    assert (
        durable.get_event(db_path, other_stream, "other-terminal", now=NOW + 20)[
            "state"
        ]
        == "acked"
    )


def test_inbox_caches_terminal_result_and_serializes_lane(db_path: Path) -> None:
    identity = {"source": "desktop-a", "target": "analyst"}
    payload_hash = _payload_hash()
    first = durable.begin_inbox(
        db_path,
        INBOX,
        "inbox-event-1",
        identity,
        payload_hash,
        processing_seconds=10,
        lane=LANE,
        now=NOW,
    )
    assert first["action"] == "execute"
    assert first["execution_token"] == first["token"]

    duplicate = durable.begin_inbox(
        db_path,
        INBOX,
        "inbox-event-1",
        identity,
        payload_hash,
        processing_seconds=10,
        lane=LANE,
        now=NOW + 1,
    )
    assert duplicate["action"] == "processing"
    assert duplicate["retry_after_seconds"] == 9

    blocked = durable.begin_inbox(
        db_path,
        INBOX,
        "inbox-event-2",
        {"source": "desktop-b", "target": "analyst"},
        _payload_hash(b"second"),
        processing_seconds=10,
        lane=LANE,
        now=NOW + 1,
    )
    assert blocked["action"] == "processing"
    assert blocked["event_id"] == "inbox-event-2"
    assert "execution_token" not in blocked

    parallel_lane = durable.begin_inbox(
        db_path,
        INBOX,
        "other-lane",
        identity,
        _payload_hash(b"parallel"),
        processing_seconds=10,
        lane="target-profile:writer",
        now=NOW + 1,
    )
    assert parallel_lane["action"] == "execute"

    result = {"status": "ok", "reply": "completed"}
    finished = durable.finish_inbox(
        db_path,
        INBOX,
        "inbox-event-1",
        first["execution_token"],
        "succeeded",
        result,
        now=NOW + 2,
    )
    assert finished["status"] == "succeeded"
    assert finished["result"] == result
    assert finished["idempotent"] is False

    replay = durable.finish_inbox(
        db_path,
        INBOX,
        "inbox-event-1",
        first["execution_token"],
        "succeeded",
        result,
        now=NOW + 100,
    )
    assert replay["idempotent"] is True
    cached = durable.begin_inbox(
        db_path,
        INBOX,
        "inbox-event-1",
        identity,
        payload_hash,
        processing_seconds=10,
        lane=LANE,
        now=NOW + 3,
    )
    assert cached["action"] == "cached"
    assert cached["result"] == result

    # Once event 1 settles, the distinct event formerly blocked by its lane
    # can become the executor.
    second = durable.begin_inbox(
        db_path,
        INBOX,
        "inbox-event-2",
        {"source": "desktop-b", "target": "analyst"},
        _payload_hash(b"second"),
        processing_seconds=10,
        lane=LANE,
        now=NOW + 3,
    )
    assert second["action"] == "execute"

    with pytest.raises(durable.InboxConflict):
        durable.begin_inbox(
            db_path,
            INBOX,
            "inbox-event-1",
            {"source": "somebody-else"},
            payload_hash,
            processing_seconds=10,
            lane=LANE,
            now=NOW + 3,
        )
    with pytest.raises(durable.InboxMismatch) as error:
        durable.finish_inbox(
            db_path,
            INBOX,
            "inbox-event-1",
            "foreign-token",
            "failed",
            {"error": "no"},
            now=NOW + 3,
        )
    assert str(error.value) == "inbox receipt does not match"


def test_inbox_expiry_is_indeterminate_and_never_reexecutes(db_path: Path) -> None:
    identity = {"target": "analyst"}
    payload_hash = _payload_hash()
    started = durable.begin_inbox(
        db_path,
        INBOX,
        "ambiguous-event",
        identity,
        payload_hash,
        processing_seconds=2,
        lane=LANE,
        now=NOW,
    )

    with pytest.raises(durable.InboxMismatch):
        durable.finish_inbox(
            db_path,
            INBOX,
            "ambiguous-event",
            started["execution_token"],
            "succeeded",
            {"reply": "too late"},
            now=NOW + 2,
        )

    ambiguous = durable.begin_inbox(
        db_path,
        INBOX,
        "ambiguous-event",
        identity,
        payload_hash,
        processing_seconds=2,
        lane=LANE,
        now=NOW + 3,
    )
    assert ambiguous["action"] == "indeterminate"
    assert ambiguous["status"] == "indeterminate"
    assert ambiguous["result"]["reason"] == "processing_lease_expired"
    assert "may have produced side effects" in ambiguous["result"]["error"]

    # The expired receipt releases the lane for a different event while its
    # own identity remains permanently non-executable.
    successor = durable.begin_inbox(
        db_path,
        INBOX,
        "successor-event",
        {"target": "analyst", "source": "desktop-b"},
        _payload_hash(b"successor"),
        processing_seconds=10,
        lane=LANE,
        now=NOW + 3,
    )
    assert successor["action"] == "execute"

    honest_unknown = {"status": "indeterminate", "error": "child state unknown"}
    terminal = durable.finish_inbox(
        db_path,
        INBOX,
        "successor-event",
        successor["execution_token"],
        "indeterminate",
        honest_unknown,
        now=NOW + 4,
    )
    assert terminal["status"] == "indeterminate"
    assert terminal["result"] == honest_unknown


def test_distinct_inbox_events_have_one_lane_executor_under_race(
    db_path: Path,
) -> None:
    workers = 8
    barrier = threading.Barrier(workers)
    current = time.time()

    def race(index: int) -> dict:
        barrier.wait(timeout=10)
        return durable.begin_inbox(
            db_path,
            INBOX,
            f"lane-race-{index}",
            {"source": f"desktop-{index}", "target": "analyst"},
            _payload_hash(f"payload-{index}".encode()),
            processing_seconds=30,
            lane=LANE,
            now=current,
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(race, range(workers)))

    assert [row["action"] for row in results].count("execute") == 1
    assert [row["action"] for row in results].count("processing") == workers - 1
    conn = sqlite3.connect(db_path)
    try:
        processing_count = conn.execute(
            """SELECT COUNT(*) FROM durable_inbox_receipts
               WHERE inbox=? AND lane_key=? AND state='processing'""",
            (INBOX, LANE),
        ).fetchone()[0]
    finally:
        conn.close()
    assert processing_count == 1


def test_inbox_cleanup_is_scoped_and_retains_fresh_terminal(db_path: Path) -> None:
    receipt = durable.begin_inbox(
        db_path,
        INBOX,
        "cleanup-inbox",
        {"target": "analyst"},
        _payload_hash(),
        processing_seconds=2,
        lane=LANE,
        now=NOW,
    )
    assert receipt["action"] == "execute"

    counts = durable.cleanup(
        db_path,
        inbox=INBOX,
        terminal_before=NOW - 1,
        now=NOW + 2,
    )
    assert counts == {
        "events_expired": 0,
        "inbox_indeterminate": 1,
        "events_deleted": 0,
        "inbox_deleted": 0,
    }
    cached = durable.begin_inbox(
        db_path,
        INBOX,
        "cleanup-inbox",
        {"target": "analyst"},
        _payload_hash(),
        processing_seconds=2,
        lane=LANE,
        now=NOW + 3,
    )
    assert cached["action"] == "indeterminate"

    deleted = durable.cleanup(
        db_path,
        inbox=INBOX,
        retention_seconds=10,
        now=NOW + 20,
    )
    assert deleted["inbox_deleted"] == 1


def test_bounds_pragmas_permissions_and_symlink_rejection(db_path: Path) -> None:
    with pytest.raises(ValueError, match="exceeds"):
        _enqueue(
            db_path,
            "oversized",
            payload={"value": "x" * durable.MAX_EVENT_JSON_BYTES},
        )
    with pytest.raises(ValueError, match="finite"):
        _enqueue(db_path, "nan", payload={"value": float("nan")})

    _enqueue(db_path, "initialize")
    conn = durable._connect(db_path)
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == (
            durable.BUSY_TIMEOUT_MS
        )
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
    finally:
        conn.close()

    if os.name == "posix":
        assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
        symlink = db_path.parent / "linked-state.db"
        symlink.symlink_to(db_path)
        with pytest.raises(OSError, match="regular file"):
            _enqueue(symlink, "must-refuse-link")


def test_cleanup_requires_one_retention_contract(db_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        durable.cleanup(db_path, now=NOW)
    with pytest.raises(ValueError, match="exactly one"):
        durable.cleanup(
            db_path,
            terminal_before=NOW,
            retention_seconds=10,
            now=NOW,
        )
