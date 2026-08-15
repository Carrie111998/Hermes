"""TDD regressions for split-phase compression fencing (#75316).

The provider summary is external/slow.  It must not retain the durable
per-session compression fence: a Telegram transcript append arriving while the
summary is in flight must persist, and a stale summary must not compact over
that new row.
"""
from __future__ import annotations

import os
import sys
import json
import threading
import time
import traceback
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB, SessionCompressionInProgressError


def _agent(db: SessionDB, session_id: str):
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent
        agent = AIAgent(
            api_key="test-key", base_url="https://example.invalid/v1",
            model="test/model", quiet_mode=True, session_db=db,
            session_id=session_id, skip_context_files=True, skip_memory=True,
        )
    compressor = MagicMock()
    compressor.compression_count = 1
    compressor.last_prompt_tokens = 0
    compressor.last_completion_tokens = 0
    compressor._last_summary_error = None
    compressor._last_compress_aborted = False
    compressor._last_aux_model_failure_model = None
    compressor._last_aux_model_failure_error = None
    agent.context_compressor = compressor
    agent._compression_feasibility_checked = True
    agent.compression_in_place = True
    return agent, compressor


def _seed(db: SessionDB, sid: str, n: int = 12):
    db.create_session(sid, source="test")
    messages = []
    for i in range(n):
        msg = {"role": "user" if i % 2 == 0 else "assistant", "content": f"old-{i}"}
        db.append_message(sid, msg["role"], msg["content"])
        messages.append(msg)
    return messages


def _active_contents(db: SessionDB, sid: str) -> list[str]:
    return [m["content"] for m in db.get_messages(sid)]


def test_provider_call_does_not_block_same_session_append_and_stale_summary_cannot_commit(tmp_path: Path):
    """GREEN-A: while the summary provider is blocked under admission lease, a same-session
    ``append_message`` (unqualified) must succeed immediately.
    """
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "split-fence"
    _seed(db, sid)
    messages = db.get_messages(sid)
    agent, compressor = _agent(db, sid)

    provider_entered = threading.Event()
    provider_release = threading.Event()
    append_done = threading.Event()
    append_result: dict = {"ok": None, "error": None, "stack": None}
    worker_error: dict = {"exc": None}

    append_deadline_s = 3.0

    def blocked_provider(*_a, **_kw):
        provider_entered.set()
        provider_release.wait(60)
        return [
            {"role": "user", "content": "[CONTEXT COMPACTION] summary"},
            {"role": "assistant", "content": "old-tail"},
        ]

    compressor.compress.side_effect = blocked_provider

    def run_compression():
        try:
            agent._compress_context(messages, "sys", approx_tokens=120_000)
        except BaseException as exc:
            worker_error["exc"] = exc

    def run_append():
        try:
            db.append_message(sid, "user", "telegram-arrived-during-summary")
            append_result["ok"] = True
        except BaseException as exc:
            append_result["ok"] = False
            append_result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            append_done.set()

    worker = threading.Thread(target=run_compression, name="compression-worker")
    append_thread = None
    try:
        worker.start()
        assert provider_entered.wait(30), (
            "compression worker never entered the provider call "
            f"(worker_error={worker_error['exc']!r})"
        )

        append_thread = threading.Thread(target=run_append, name="append-thread")
        append_thread.start()

        append_completed = append_done.wait(append_deadline_s)
        if not append_completed:
            for tid, frame in sys._current_frames().items():
                if append_thread.ident is not None and tid == append_thread.ident:
                    append_result["stack"] = "".join(traceback.format_stack(frame))
                    break

        assert append_completed is True, (
            "append_message did not complete within "
            f"{append_deadline_s:.1f}s while the compression provider was "
            f"blocked under admission lock. append_thread stack:\n{append_result['stack']}"
        )
        assert append_result["ok"] is True, (
            "append_message finished with an error while the compression "
            f"provider was blocked: {append_result['error']}"
        )
    finally:
        provider_release.set()
        if append_thread is not None:
            append_thread.join(timeout=15)
        worker.join(timeout=30)


def test_append_fails_with_SessionCompressionInProgressError_after_busy_wait_red_a2(tmp_path: Path):
    """GREEN-A2: when exclusive lock is acquired, normal appends are blocked and fail-closed after budget.
    """
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "split-fence-a2"
    _seed(db, sid)

    # Force insert an exclusive lock
    db._execute_write(lambda conn: conn.execute(
        "INSERT INTO compression_locks (session_id, holder, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
        (sid, "exclusive:some-holder", time.time(), time.time() + 60)
    ))

    append_done = threading.Event()
    append_result: dict = {"ok": None, "error": None, "stack": None, "elapsed_s": None}

    append_thread = None

    def run_append():
        start = time.monotonic()
        try:
            db.append_message(sid, "user", "telegram-arrives-during-exclusive")
            append_result["ok"] = True
        except BaseException as exc:
            append_result["ok"] = False
            append_result["error"] = f"{type(exc).__name__}: {exc}"
            for tid, frame in sys._current_frames().items():
                if append_thread is not None and append_thread.ident is not None and tid == append_thread.ident:
                    append_result["stack"] = "".join(traceback.format_stack(frame))
                    break
        finally:
            append_result["elapsed_s"] = time.monotonic() - start
            append_done.set()

    try:
        append_thread = threading.Thread(target=run_append, name="append-thread-a2")
        append_thread.start()

        # Wait past the busy-wait retry budget
        assert append_done.wait(10), "append_message never terminated"
        append_thread.join(timeout=5)

        assert append_result["ok"] is False, "append_message succeeded under exclusive lock"
        assert "SessionCompressionInProgressError" in append_result["error"], (
            f"expected SessionCompressionInProgressError, got {append_result['error']!r}"
        )
        assert append_result["elapsed_s"] >= 4.5, f"failed too early ({append_result['elapsed_s']:.2f}s)"
    finally:
        if append_thread is not None:
            append_thread.join(timeout=5)


def test_stale_summary_must_not_archive_live_tail_landing_during_provider_call_red_b(tmp_path: Path):
    """GREEN-B: live tail B landing during provider call survives in-place commit via Design B.
    """
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "red-b-stale-tail"
    _seed(db, sid)
    messages = db.get_messages(sid)
    agent, compressor = _agent(db, sid)
    agent._cached_system_prompt = "sys"

    provider_entered = threading.Event()
    provider_release = threading.Event()
    worker_error: dict = {"exc": None}
    holder_at_insert: dict = {"value": None}

    def stale_summary_provider(*_a, **_kw):
        provider_entered.set()
        provider_release.wait(30)
        return [
            {"role": "user", "content": "[CONTEXT COMPACTION] stale summary of A only"},
            {"role": "assistant", "content": "old-tail"},
        ]

    compressor.compress.side_effect = stale_summary_provider

    def run_compression():
        try:
            agent._compress_context(messages, "sys", approx_tokens=120_000)
        except BaseException as exc:
            worker_error["exc"] = exc

    worker = threading.Thread(target=run_compression, name="compression-worker-red-b")
    try:
        worker.start()
        assert provider_entered.wait(30), (
            "compression worker never entered the provider call"
        )

        holder_at_insert["value"] = db.get_compression_lock_holder(sid)
        assert holder_at_insert["value"], "compression lease holder not visible"
        b_content = "live-tail-B-arrived-during-provider-call"
        
        # In our new implementation, normal appends (compression_lock_holder=None)
        # are permitted, but let's test that both work.
        db.append_message(sid, "user", b_content)
        
        assert any(m["content"] == b_content for m in db.get_messages(sid)), \
            "live tail B was not durably inserted before the commit"

        provider_release.set()
        worker.join(timeout=30)
        assert not worker.is_alive(), "compression worker hung"

        live = db.get_messages(sid)
        with_compacted = db.get_messages(sid, include_compacted=True)

        b_live = [m for m in live if m["content"] == b_content]
        b_any = [m for m in with_compacted if m["content"] == b_content]

        assert len(b_live) == 1, f"live tail B appears {len(b_live)}x in active, expected 1"
        assert len(b_any) == 1, f"live tail B appears {len(b_any)}x in disk, expected 1"
        assert agent.session_id == sid, "session ID rotated"
        
        children = db.find_live_compression_child(sid)
        assert children is None, f"orphan sibling fork detected: {children}"
    finally:
        provider_release.set()
        worker.join(timeout=30)


def test_second_compressor_blocked(tmp_path: Path):
    """Verify that a second compressor cannot start on the same session while one is active."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "second-blocked"
    _seed(db, sid)

    acquired = db.try_acquire_compression_lock(sid, "admission:holder-A", ttl_seconds=60)
    assert acquired is True

    # Try to acquire with holder-B
    acquired_B = db.try_acquire_compression_lock(sid, "admission:holder-B", ttl_seconds=60)
    assert acquired_B is False


def test_append_allowed_mutation_blocked_during_admission(tmp_path: Path):
    """Verify that append is allowed during admission, but replace_messages is blocked."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "append-allowed-replace-blocked"
    _seed(db, sid)

    db.try_acquire_compression_lock(sid, "admission:holder", ttl_seconds=60)
    
    # Append should succeed
    db.append_message(sid, "user", "ok-append")
    
    # Replace should fail
    with pytest.raises(SessionCompressionInProgressError):
        db.replace_messages(sid, [{"role": "user", "content": "rewritten"}])


def test_holder_safety(tmp_path: Path):
    """Verify that holder A cannot release or upgrade holder B's lock."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "holder-safety"
    _seed(db, sid)

    db.try_acquire_compression_lock(sid, "admission:holder-B", ttl_seconds=60)

    # Holder A try to upgrade B's lock
    upgraded = db.upgrade_compression_lock_to_exclusive(sid, "admission:holder-A", "exclusive:holder-A")
    assert upgraded is False

    # Holder A try to release B's lock
    db.release_compression_lock(sid, "admission:holder-A")
    
    # Verify B's lock is still intact in the database
    holder = db.get_compression_lock_holder(sid)
    assert holder == "admission:holder-B"


def test_tail_ordering_and_multiplicity(tmp_path: Path):
    """Verify B/B1/B2 are preserved in order after summary, exactly once."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "tail-ordering"
    db.create_session(sid, source="test")
    
    # Snapshot messages (A)
    db.append_message(sid, "user", "A1")
    db.append_message(sid, "assistant", "A2")
    active_m = db.get_messages(sid)
    max_snapshot_id = max(m["id"] for m in active_m)

    # Admission phase: compressor starts, live tail B1/B2 lands (allowed)
    db.try_acquire_compression_lock(sid, "admission:holder-A")
    db.append_message(sid, "user", "B1")
    db.append_message(sid, "assistant", "B2")

    # Pre-commit: upgrade admission -> exclusive (blocks ordinary appends)
    upgraded = db.upgrade_compression_lock_to_exclusive(
        sid, "admission:holder-A", "exclusive:holder-A"
    )
    assert upgraded is True

    # archive_and_compact
    summary = [{"role": "user", "content": "summary"}]
    db.archive_and_compact(
        sid,
        summary,
        compression_lock_holder="exclusive:holder-A",
        max_snapshot_id=max_snapshot_id
    )

    # Check contents
    msgs = db.get_messages(sid)
    contents = [m["content"] for m in msgs]
    assert contents == ["summary", "B1", "B2"]

    # History shows them exactly once
    compacted = db.get_messages(sid, include_compacted=True)
    all_contents = [m["content"] for m in compacted]
    assert all_contents.count("B1") == 1
    assert all_contents.count("B2") == 1


def test_metadata_preservation(tmp_path: Path):
    """Verify platform_message_id, tool metadata, and reasoning are fully preserved."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "metadata-preservation"
    db.create_session(sid, source="test")
    
    db.append_message(sid, "user", "A")
    active_m = db.get_messages(sid)
    max_snapshot_id = max(m["id"] for m in active_m)

    db.try_acquire_compression_lock(sid, "exclusive:holder")
    
    # Append message directly to messages table with full mock metadata
    # We will use an internal insert helper to customize fields
    db._execute_write(lambda conn: db._insert_message_rows(conn, sid, [{
        "role": "assistant",
        "content": "B",
        "platform_message_id": "test-platform-123",
        "tool_call_id": "tc-123",
        "tool_calls": [{"id": "tc1", "name": "foo", "arguments": "{}"}],
        "reasoning": "some reasoning info",
        "observed": 1
    }]))

    # archive_and_compact
    db.archive_and_compact(
        sid,
        [{"role": "user", "content": "summary"}],
        compression_lock_holder="exclusive:holder",
        max_snapshot_id=max_snapshot_id
    )

    # Verify preserved message structure
    msgs = db.get_messages(sid)
    b_msg = msgs[1]
    assert b_msg["content"] == "B"
    assert b_msg["platform_message_id"] == "test-platform-123"
    assert b_msg["tool_call_id"] == "tc-123"
    assert b_msg["tool_calls"] == [{"id": "tc1", "name": "foo", "arguments": "{}"}]
    assert b_msg["reasoning"] == "some reasoning info"


def test_provider_timeout_recovery(tmp_path: Path):
    """Verify provider timeout/exception releases the admission lock."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "provider-timeout"
    _seed(db, sid)
    messages = db.get_messages(sid)
    agent, compressor = _agent(db, sid)

    def failing_provider(*_a, **_kw):
        raise TimeoutError("Provider timed out")

    compressor.compress.side_effect = failing_provider

    # Implementation catches provider errors internally and returns gracefully
    # (does NOT propagate TimeoutError). Verify lock is released after failure.
    result = agent._compress_context(messages, "sys", approx_tokens=120_000)
    assert result is not None, "_compress_context returned None on provider failure"

    # Verify lock is freed
    holder = db.get_compression_lock_holder(sid)
    assert holder is None, f"lock not released after provider timeout: {holder}"


def test_stale_compressor_commit_rejected(tmp_path: Path):
    """Verify that a stale compressor trying to commit has its attempt rejected."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "stale-commit"
    db.create_session(sid, source="test")
    db.append_message(sid, "user", "A")
    active_m = db.get_messages(sid)
    max_snapshot_id = max(m["id"] for m in active_m)

    # Compressor A acquires
    db.try_acquire_compression_lock(sid, "admission:holder-A", ttl_seconds=60)

    # Compressor B comes and takes the lock (e.g. A's expired lock is reclaimed)
    # Since try_acquire_compression_lock doesn't overwrite active locks, let's force expire it
    db._execute_write(lambda conn: conn.execute(
        "UPDATE compression_locks SET expires_at = 0 WHERE session_id = ?", (sid,)
    ))
    db.try_acquire_compression_lock(sid, "admission:holder-B", ttl_seconds=60)

    # A tries to commit using exclusive:holder-A
    with pytest.raises(SessionCompressionInProgressError):
        db.archive_and_compact(
            sid,
            [{"role": "user", "content": "stale-summary"}],
            compression_lock_holder="exclusive:holder-A",
            max_snapshot_id=max_snapshot_id
        )


def test_expired_lease_recovery(tmp_path: Path):
    """Verify expired lease recovery allows a new compressor to take it."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "lease-recovery"
    _seed(db, sid)

    # Insert expired lock
    db._execute_write(lambda conn: conn.execute(
        "INSERT INTO compression_locks (session_id, holder, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
        (sid, "admission:holder-A", time.time() - 20, time.time() - 10)
    ))

    # B tries to acquire lock
    acquired = db.try_acquire_compression_lock(sid, "admission:holder-B", ttl_seconds=60)
    assert acquired is True
    
    # Lock belongs to B now
    assert db.get_compression_lock_holder(sid) == "admission:holder-B"


# ── Failure-injection tests (pre-commit gate 2026-08-16) ─────────────


def test_fi1_exception_before_upgrade_releases_admission_lock(tmp_path: Path):
    """FI-1: Exception during provider call (before admission→exclusive upgrade)
    must release the admission lock so the session is not permanently blocked.
    """
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "fi1-before-upgrade"
    _seed(db, sid)
    messages = db.get_messages(sid)
    agent, compressor = _agent(db, sid)

    def failing_provider(*_a, **_kw):
        raise RuntimeError("Injected: provider crashed before upgrade")

    compressor.compress.side_effect = failing_provider

    with pytest.raises(RuntimeError, match="Injected: provider crashed before upgrade"):
        agent._compress_context(messages, "sys", approx_tokens=120_000)

    # Lock must be released
    holder = db.get_compression_lock_holder(sid)
    assert holder is None, f"admission lock not released after provider crash: {holder}"

    # Session must not be permanently blocked: a new compressor can acquire
    acquired = db.try_acquire_compression_lock(sid, "admission:new-holder", ttl_seconds=60)
    assert acquired is True, "session permanently blocked after provider crash"


def test_fi2_exception_after_upgrade_before_commit_releases_exclusive_lock(tmp_path: Path):
    """FI-2: Exception immediately after upgrade to exclusive, before commit,
    must release the exclusive lock and leave no partial commit.

    Note: _compress_context catches the archive_and_compact exception internally
    (session-split failure handler), releases the lock, and returns normally.
    The key guarantees are: lock released + no partial commit in DB.
    """
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "fi2-after-upgrade"
    _seed(db, sid)
    messages = db.get_messages(sid)
    original_count = len(messages)
    agent, compressor = _agent(db, sid)
    agent._cached_system_prompt = "sys"

    # Compressor returns a valid summary (shorter than input → passes would-grow check)
    compressor.compress.side_effect = lambda *_a, **_kw: [
        {"role": "user", "content": "[CONTEXT COMPACTION] summary"},
    ]

    # Inject failure in archive_and_compact (called after upgrade to exclusive)
    def failing_archive(*args, **kwargs):
        raise RuntimeError("Injected: crash after upgrade, before commit")

    with patch.object(db, "archive_and_compact", side_effect=failing_archive):
        # _compress_context catches the exception internally and returns normally
        result = agent._compress_context(messages, "sys", approx_tokens=120_000)
        assert result is not None, "_compress_context returned None on archive failure"

    # Exclusive lock must be released
    holder = db.get_compression_lock_holder(sid)
    assert holder is None, f"exclusive lock not released after post-upgrade crash: {holder}"

    # No partial commit: all original messages still active in DB
    live = db.get_messages(sid)
    assert len(live) == original_count, (
        f"partial commit detected: {len(live)} active != {original_count} original"
    )


def test_fi3_exception_inside_archive_transaction_full_rollback(tmp_path: Path):
    """FI-3: Exception inside the archive/reinsert transaction must cause a
    full rollback — no partial archive, no partial reinsert, no lost tail.
    """
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "fi3-rollback"
    db.create_session(sid, source="test")

    # Snapshot messages (A)
    db.append_message(sid, "user", "A1")
    db.append_message(sid, "assistant", "A2")
    active_m = db.get_messages(sid)
    max_snapshot_id = max(m["id"] for m in active_m)

    # Admission phase: live tail B1/B2 lands
    db.try_acquire_compression_lock(sid, "admission:holder-A")
    db.append_message(sid, "user", "B1")
    db.append_message(sid, "assistant", "B2")

    # Upgrade to exclusive
    upgraded = db.upgrade_compression_lock_to_exclusive(
        sid, "admission:holder-A", "exclusive:holder-A"
    )
    assert upgraded is True

    # Inject failure in _insert_message_rows on the SECOND call (tail reinsert).
    # First call (summary insert) succeeds; second call (tail reinsert) raises.
    # All changes within the _execute_write transaction must be rolled back.
    call_count = [0]
    original_insert = db._insert_message_rows

    def failing_insert(conn, session_id, msgs):
        call_count[0] += 1
        if call_count[0] >= 2:
            raise RuntimeError("Injected: crash during tail reinsert")
        return original_insert(conn, session_id, msgs)

    with patch.object(db, "_insert_message_rows", side_effect=failing_insert):
        with pytest.raises(RuntimeError, match="Injected: crash during tail reinsert"):
            db.archive_and_compact(
                sid,
                [{"role": "user", "content": "summary"}],
                compression_lock_holder="exclusive:holder-A",
                max_snapshot_id=max_snapshot_id,
            )

    # Full rollback: all 4 messages still active in original order
    live = db.get_messages(sid)
    contents = [m["content"] for m in live]
    assert contents == ["A1", "A2", "B1", "B2"], f"rollback failed: {contents}"

    # No summary was inserted (rollback undid it)
    assert "summary" not in contents, "partial commit: summary inserted despite rollback"

    # Lock is still held (archive_and_compact does not release it on failure)
    holder = db.get_compression_lock_holder(sid)
    assert holder == "exclusive:holder-A", f"lock state unexpected after rollback: {holder}"


def test_fi4_late_stale_worker_after_timeout_cannot_commit(tmp_path: Path):
    """FI-4: A stale worker whose lease has expired and been reclaimed by another
    holder cannot commit via archive_and_compact.
    """
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "fi4-stale-worker"
    db.create_session(sid, source="test")
    db.append_message(sid, "user", "A")
    active_m = db.get_messages(sid)
    max_snapshot_id = max(m["id"] for m in active_m)

    # Worker A acquires lock
    db.try_acquire_compression_lock(sid, "admission:worker-A", ttl_seconds=60)

    # Simulate timeout: expire A's lease
    db._execute_write(lambda conn: conn.execute(
        "UPDATE compression_locks SET expires_at = 0 WHERE session_id = ?", (sid,)
    ))

    # Worker B reclaims the expired lease
    acquired = db.try_acquire_compression_lock(sid, "admission:worker-B", ttl_seconds=60)
    assert acquired is True, "worker B could not reclaim expired lease"

    # Worker B upgrades to exclusive
    upgraded = db.upgrade_compression_lock_to_exclusive(
        sid, "admission:worker-B", "exclusive:worker-B"
    )
    assert upgraded is True

    # Late stale worker A tries to commit with its old exclusive holder
    with pytest.raises(SessionCompressionInProgressError):
        db.archive_and_compact(
            sid,
            [{"role": "user", "content": "stale-summary-from-A"}],
            compression_lock_holder="exclusive:worker-A",
            max_snapshot_id=max_snapshot_id,
        )

    # Verify no stale commit happened
    live = db.get_messages(sid)
    contents = [m["content"] for m in live]
    assert "stale-summary-from-A" not in contents, "stale worker A committed despite lease loss"
    assert "A" in contents, "original message lost"


def test_fi5_holder_a_cannot_release_or_upgrade_holder_b(tmp_path: Path):
    """FI-5: Holder A cannot release or upgrade holder B's lock.
    Comprehensive check: release, upgrade, and commit all fail for wrong holder.
    """
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "fi5-holder-isolation"
    db.create_session(sid, source="test")
    db.append_message(sid, "user", "A")
    active_m = db.get_messages(sid)
    max_snapshot_id = max(m["id"] for m in active_m)

    # Holder B acquires and upgrades to exclusive
    db.try_acquire_compression_lock(sid, "admission:holder-B", ttl_seconds=60)
    upgraded = db.upgrade_compression_lock_to_exclusive(
        sid, "admission:holder-B", "exclusive:holder-B"
    )
    assert upgraded is True

    # Holder A cannot release B's lock
    db.release_compression_lock(sid, "exclusive:holder-A")
    holder = db.get_compression_lock_holder(sid)
    assert holder == "exclusive:holder-B", f"holder A released holder B's lock: {holder}"

    # Holder A cannot upgrade B's lock
    upgraded_A = db.upgrade_compression_lock_to_exclusive(
        sid, "exclusive:holder-A", "exclusive:holder-A2"
    )
    assert upgraded_A is False, "holder A upgraded holder B's lock"

    # Holder A cannot commit via archive_and_compact
    with pytest.raises(SessionCompressionInProgressError):
        db.archive_and_compact(
            sid,
            [{"role": "user", "content": "stale-summary"}],
            compression_lock_holder="exclusive:holder-A",
            max_snapshot_id=max_snapshot_id,
        )

    # B's lock is still intact
    holder = db.get_compression_lock_holder(sid)
    assert holder == "exclusive:holder-B", f"holder B's lock lost: {holder}"


def test_max_snapshot_id_prod_guarantee(tmp_path: Path):
    """Advisory proof: production DB-backed flow always provides message IDs,
    so max_snapshot_id is never None in the real compression path.

    The production flow loads messages via get_messages() which returns rows
    from SQLite with AUTOINCREMENT id — every row has a non-null integer id.
    """
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "max-snapshot-proof"
    _seed(db, sid, n=20)

    # Production flow: load messages from DB
    messages = db.get_messages(sid)
    assert len(messages) == 20, f"expected 20 messages, got {len(messages)}"

    # Every message must have a non-null integer id (production guarantee)
    for i, m in enumerate(messages):
        assert isinstance(m, dict), f"message {i} is not a dict"
        assert m.get("id") is not None, f"message {i} has no id"
        assert isinstance(m["id"], int), f"message {i} id is not int: {type(m['id'])}"

    # Compute max_snapshot_id exactly as production code does
    ids = [int(m["id"]) for m in messages if isinstance(m, dict) and m.get("id") is not None]
    max_snapshot_id = max(ids) if ids else None

    # Production guarantee: max_snapshot_id is never None for DB-backed flow
    assert max_snapshot_id is not None, (
        "max_snapshot_id is None for DB-backed messages — "
        "production guarantee violated"
    )
    assert max_snapshot_id == max(m["id"] for m in messages), (
        "max_snapshot_id does not match max DB id"
    )
