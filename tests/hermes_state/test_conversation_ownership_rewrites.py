"""Destructive rewrites must not publish under a lost grant.

Retry, rewind, reset, compress, replacement and delete all rewrite history that
already exists. A plain lock is not enough for them: a writer that STALLED —
GC pause, a wedged network call, a starved refresher — can wake after its lease
expired and another surface took the conversation, and then happily replace or
delete rows the new owner has since written. That is the failure a fence
catches and a lock cannot.

So every rewrite runs its mutation inside the same transaction that validates
the caller's fence token. A stale token is refused *before* the mutation runs.

Callers that hold no grant remain compatible only while no durable owner is
live. Once any process owns the canonical root, even an unwired or direct
persistence caller is refused inside the same transaction as its attempted
mutation. This prevents a process-local grant lookup from becoming the sole
enforcement mechanism.
"""

import concurrent.futures
import time

import pytest

from hermes_state import SessionDB

from agent.session_ownership import (
    ConversationOwnershipConflict,
    StaleConversationOwnershipError,
    new_holder_id,
    own_conversation,
)


@pytest.fixture
def db(tmp_path):
    store = SessionDB(tmp_path / "state.db")
    store.create_session("root", source="cli")
    store.append_messages_batch(
        "root",
        [
            {"role": "user", "content": "one", "timestamp": 1.0},
            {"role": "assistant", "content": "two", "timestamp": 2.0},
        ],
    )
    yield store
    store.close()


def _stolen_grant_scope(db, session_id="root"):
    """A grant that is in scope but no longer the owner.

    Mirrors the real shape: our lease expired while we were stalled, another
    surface legitimately took the conversation, and now our thread wakes up
    still holding what it believes is a valid grant.
    """
    return own_conversation(
        db,
        session_id,
        surface="cli",
        ttl_seconds=0.2,
        # Long enough that the refresher cannot revive the lease mid-test —
        # a stalled holder is exactly one whose refresher did not tick.
        refresh_interval_seconds=30.0,
    )


def _steal(db, root="root"):
    time.sleep(0.3)
    return db.try_acquire_conversation_ownership(
        root, new_holder_id(surface="tui"), surface="tui", session_id=root,
    )


# ── each rewrite, under a lost grant ───────────────────────────────────────


def test_replace_messages_is_refused_under_a_lost_grant(db):
    """Compression / replacement rewrites the whole transcript."""
    with _stolen_grant_scope(db):
        _steal(db)
        with pytest.raises(StaleConversationOwnershipError):
            db.replace_messages(
                "root", [{"role": "user", "content": "clobbered", "timestamp": 9.0}]
            )
    assert [m["content"] for m in db.get_messages("root")] == ["one", "two"]


def test_rewind_is_refused_under_a_lost_grant(db):
    rows = db.get_messages("root")
    target = rows[0]["id"]
    with _stolen_grant_scope(db):
        _steal(db)
        with pytest.raises(StaleConversationOwnershipError):
            db.rewind_to_message("root", target)
    assert len(db.get_messages("root")) == 2


def test_reset_promotion_is_refused_under_a_lost_grant(db):
    with _stolen_grant_scope(db):
        _steal(db)
        with pytest.raises(StaleConversationOwnershipError):
            db.promote_to_session_reset("root")
    assert db.get_session("root")["ended_at"] is None


def test_turn_publication_is_refused_under_a_lost_grant(db):
    """The turn-end flush is a publication too — a stale turn must not land."""
    with _stolen_grant_scope(db):
        _steal(db)
        with pytest.raises(StaleConversationOwnershipError):
            db.append_messages_batch(
                "root", [{"role": "user", "content": "late", "timestamp": 9.0}]
            )
    assert len(db.get_messages("root")) == 2


def test_single_message_append_is_refused_under_a_lost_grant(db):
    with _stolen_grant_scope(db):
        _steal(db)
        with pytest.raises(StaleConversationOwnershipError):
            db.append_message("root", "user", "late")
    assert len(db.get_messages("root")) == 2


def test_session_delete_is_refused_under_a_lost_grant(db):
    with _stolen_grant_scope(db):
        _steal(db)
        with pytest.raises(StaleConversationOwnershipError):
            db.delete_session("root")
    assert db.get_session("root") is not None


def test_archive_and_compact_is_refused_under_a_lost_grant(db):
    with _stolen_grant_scope(db):
        _steal(db)
        with pytest.raises(StaleConversationOwnershipError):
            db.archive_and_compact(
                "root", [{"role": "user", "content": "summary", "timestamp": 9.0}]
            )
    assert [m["content"] for m in db.get_messages("root")] == ["one", "two"]


def test_delete_session_if_empty_is_refused_under_a_lost_grant(db):
    db.create_session("empty", source="cli")
    with _stolen_grant_scope(db, "empty"):
        _steal(db, "empty")
        with pytest.raises(StaleConversationOwnershipError):
            db.delete_session_if_empty("empty")
    assert db.get_session("empty") is not None


def test_restore_rewound_is_refused_under_a_lost_grant(db):
    first_id = db.get_messages("root")[0]["id"]
    db.rewind_to_message("root", first_id)
    with _stolen_grant_scope(db):
        _steal(db)
        with pytest.raises(StaleConversationOwnershipError):
            db.restore_rewound("root", first_id)
    assert db.get_messages("root") == []


def test_ancestor_delete_is_refused_while_its_lineage_is_owned(db):
    db.create_session("child", source="compression", parent_session_id="root")
    with own_conversation(db, "child", surface="cli"):
        with pytest.raises(ConversationOwnershipConflict):
            db.delete_session("root")
    assert db.get_conversation_root("child") == "root"


@pytest.mark.parametrize("operation", ["bulk", "empty", "prune"])
def test_bulk_cleanup_skips_owned_lineages_and_deletes_unrelated(db, operation):
    """Maintenance preserves its count contract while ownership stays intact."""
    db.create_session("owned", source="cli")
    db.create_session("other", source="cli")
    now = time.time()
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET ended_at = ?, started_at = ? WHERE id IN (?, ?)",
            (now, now - 10 * 86400, "owned", "other"),
        )
    )

    with own_conversation(db, "owned", surface="cli"):
        if operation == "bulk":
            deleted = db.delete_sessions(["owned", "other"])
        elif operation == "empty":
            deleted = db.delete_empty_sessions()
        else:
            deleted = db.prune_sessions(older_than_days=1)

    assert deleted == 1
    assert db.get_session("owned") is not None
    assert db.get_session("other") is None


# ── the same operations under a LIVE grant still work ──────────────────────


def test_rewrites_run_normally_under_a_live_grant(db):
    with own_conversation(db, "root", surface="cli") as grant:
        assert grant is not None
        db.append_messages_batch(
            "root", [{"role": "user", "content": "three", "timestamp": 3.0}]
        )
        assert len(db.get_messages("root")) == 3
        db.replace_messages(
            "root", [{"role": "user", "content": "only", "timestamp": 4.0}]
        )
        assert [m["content"] for m in db.get_messages("root")] == ["only"]
        assert db.promote_to_session_reset("root") is True


# ── callers without a local grant still respect a foreign owner ────────────


def test_no_grant_and_no_durable_owner_leaves_behaviour_unchanged(db):
    """Legacy direct mutations remain compatible when no authority is held."""
    db.append_messages_batch(
        "root", [{"role": "user", "content": "three", "timestamp": 3.0}]
    )
    assert len(db.get_messages("root")) == 3
    db.replace_messages("root", [{"role": "user", "content": "x", "timestamp": 4.0}])
    assert [m["content"] for m in db.get_messages("root")] == ["x"]
    assert db.promote_to_session_reset("root") is True


def test_foreign_unowned_append_is_refused_while_an_owner_is_live(db):
    """A process-local grant cannot be the only enforcement mechanism."""
    def _foreign_append():
        foreign = SessionDB(db.db_path)
        try:
            foreign.append_message("root", "user", "foreign")
        finally:
            foreign.close()

    with own_conversation(db, "root", surface="cli"):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_foreign_append)
            with pytest.raises(ConversationOwnershipConflict):
                future.result()
    assert [m["content"] for m in db.get_messages("root")] == ["one", "two"]


def test_foreign_unowned_rewind_is_refused_while_an_owner_is_live(db):
    """Direct TUI/API lifecycle rewrites must consult durable authority."""
    target = db.get_messages("root")[0]["id"]

    def _foreign_rewind():
        foreign = SessionDB(db.db_path)
        try:
            foreign.rewind_to_message("root", target)
        finally:
            foreign.close()

    with own_conversation(db, "root", surface="cli"):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_foreign_rewind)
            with pytest.raises(ConversationOwnershipConflict):
                future.result()
    assert [m["content"] for m in db.get_messages("root")] == ["one", "two"]


def test_delegate_segment_can_publish_while_its_parent_turn_is_owned(db):
    """Concurrent subagents write their own segment without borrowing root authority."""
    db.create_session("delegate", source="delegate", parent_session_id="root")

    def _delegate_append():
        delegate_db = SessionDB(db.db_path)
        try:
            delegate_db.append_message("delegate", "assistant", "delegate-write")
        finally:
            delegate_db.close()

    with own_conversation(db, "root", surface="cli"):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(_delegate_append).result()
    assert [m["content"] for m in db.get_messages("delegate")] == ["delegate-write"]


def test_unowned_compression_child_cannot_bypass_the_parent_owner(db):
    """A root-owned compression child remains covered by root authority."""
    db.create_session("compressed", source="compression", parent_session_id="root")

    def _compressed_append():
        child_db = SessionDB(db.db_path)
        try:
            child_db.append_message("compressed", "assistant", "foreign")
        finally:
            child_db.close()

    with own_conversation(db, "root", surface="cli"):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_compressed_append)
            with pytest.raises(ConversationOwnershipConflict):
                future.result()
    assert db.get_messages("compressed") == []


def test_delegate_compression_child_can_publish_below_the_delegate_boundary(db):
    """A subagent remains independently writable after rotating its own segment."""
    db.create_session("delegate", source="delegate", parent_session_id="root")
    db.create_session(
        "delegate-compressed", source="compression", parent_session_id="delegate"
    )

    def _delegate_child_append():
        child_db = SessionDB(db.db_path)
        try:
            child_db.append_message("delegate-compressed", "assistant", "continued")
        finally:
            child_db.close()

    with own_conversation(db, "root", surface="cli"):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(_delegate_child_append).result()
    assert [m["content"] for m in db.get_messages("delegate-compressed")] == [
        "continued"
    ]


def test_delegate_sourced_root_does_not_gain_a_permanent_exemption(db):
    """The exception requires a real parent boundary, not only a source label."""
    db.create_session("orphan-delegate", source="delegate")

    def _foreign_append():
        foreign = SessionDB(db.db_path)
        try:
            foreign.append_message("orphan-delegate", "assistant", "foreign")
        finally:
            foreign.close()

    with own_conversation(db, "orphan-delegate", surface="cli"):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_foreign_append)
            with pytest.raises(ConversationOwnershipConflict):
                future.result()
    assert db.get_messages("orphan-delegate") == []


def test_malformed_delegate_cycle_fails_closed_instead_of_bypassing(db):
    """A delegate marker exempts only a lineage that reaches a real parent root."""
    db.create_session("cycle-a", source="delegate")
    db.create_session("cycle-b", source="compression", parent_session_id="cycle-a")
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET parent_session_id = ? WHERE id = ?",
            ("cycle-b", "cycle-a"),
        )
    )

    def _foreign_append():
        foreign = SessionDB(db.db_path)
        try:
            foreign.append_message("cycle-a", "assistant", "foreign")
        finally:
            foreign.close()

    with own_conversation(db, "cycle-a", surface="cli"):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_foreign_append)
            with pytest.raises(ConversationOwnershipConflict):
                future.result()
    assert db.get_messages("cycle-a") == []


def test_delegate_delete_if_empty_cannot_re_root_below_a_live_parent(db):
    """Delegate publication exemption must not cover lineage deletion."""
    db.create_session("empty-delegate", source="delegate", parent_session_id="root")

    def _foreign_delete():
        foreign = SessionDB(db.db_path)
        try:
            foreign.delete_session_if_empty("empty-delegate")
        finally:
            foreign.close()

    with own_conversation(db, "root", surface="cli"):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_foreign_delete)
            with pytest.raises(ConversationOwnershipConflict):
                future.result()
    assert db.get_session("empty-delegate") is not None


def test_delegate_reset_promotion_is_not_an_unfenced_publication(db):
    """The delegate exception is publication-only, not a generic mutation bypass."""
    db.create_session("delegate-reset", source="delegate", parent_session_id="root")

    def _foreign_reset():
        foreign = SessionDB(db.db_path)
        try:
            foreign.promote_to_session_reset("delegate-reset")
        finally:
            foreign.close()

    with own_conversation(db, "root", surface="cli"):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_foreign_reset)
            with pytest.raises(ConversationOwnershipConflict):
                future.result()
    assert db.get_session("delegate-reset")["end_reason"] is None


def test_delegate_replace_is_not_an_unfenced_publication(db):
    """Whole-transcript replacement is a rewrite, not delegate publication."""
    db.create_session("delegate-replace", source="delegate", parent_session_id="root")
    db.append_message("delegate-replace", "user", "original")

    def _foreign_replace():
        foreign = SessionDB(db.db_path)
        try:
            foreign.replace_messages(
                "delegate-replace", [{"role": "assistant", "content": "replaced"}]
            )
        finally:
            foreign.close()

    with own_conversation(db, "root", surface="cli"):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_foreign_replace)
            with pytest.raises(ConversationOwnershipConflict):
                future.result()
    assert [m["content"] for m in db.get_messages("delegate-replace")] == [
        "original"
    ]


# ── a rewrite on a session in the owned lineage is fenced too ──────────────


def test_a_compression_child_is_fenced_by_the_parent_grant(db):
    """Rotation moves the write target; ownership does not move with it.

    The grant covers the whole lineage, so a rewrite aimed at the child
    segment is fenced by the same token that covers the root.
    """
    db.create_session("child", source="compression", parent_session_id="root")
    with _stolen_grant_scope(db):
        _steal(db)
        with pytest.raises(StaleConversationOwnershipError):
            db.append_messages_batch(
                "child", [{"role": "user", "content": "late", "timestamp": 9.0}]
            )
    assert db.get_messages("child") == []
