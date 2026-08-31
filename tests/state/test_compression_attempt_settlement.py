"""RC1: compression_attempt durable settlement — real SessionDB integration."""

import pytest
from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    sdb = SessionDB(db_path=tmp_path / "state.db")
    try:
        yield sdb
    finally:
        sdb.close()


def _new_family(db: SessionDB, source="tui") -> str:
    sid = f"20260831_{source}_init"
    db.create_session(sid, source=source, session_key=sid)
    return sid


def test_family_stable_across_two_rotations(db):
    p = _new_family(db, "tui")
    fam = db.get_session(p)["session_key"]
    c1 = "child_a"
    db.publish_compression_child(
        parent_session_id=p,
        child_session_id=c1,
        source="tui",
        messages=[{"role": "user", "content": "h"}],
        watermark=None,
        require_compression_lease=False,
    )
    assert db.get_session(c1)["session_key"] == fam
    c2 = "child_b"
    db.publish_compression_child(
        parent_session_id=c1,
        child_session_id=c2,
        source="tui",
        messages=[{"role": "user", "content": "h2"}],
        watermark=None,
        require_compression_lease=False,
    )
    assert db.get_session(c2)["session_key"] == fam


def test_attempt_stores_family_not_gateway_shadow(db):
    parent = _new_family(db, "tui")
    fam = db.get_session(parent)["session_key"]
    attempt_id = "att_family"
    db.create_compression_attempt(
        attempt_id=attempt_id,
        session_key=fam,
        parent_session_id=parent,
        input_history_version=99,
        input_watermark=3,
        holder=attempt_id,
    )
    row = db.get_compression_attempt(attempt_id)
    assert row["session_key"] == fam


def test_input_watermark_max_not_len(db):
    parent = _new_family(db, "tui")
    db.append_message(parent, "user", "a")
    db.append_message(parent, "assistant", "b")
    wm = db.get_active_message_watermark(parent)
    assert wm >= 1
    db.append_message(parent, "user", "concurrent")
    wm2 = db.get_active_message_watermark(parent)
    assert wm2 == wm + 1


def test_foreign_tail_cloned_using_watermark_ceiling(db):
    parent = _new_family(db, "tui")
    for i in range(3):
        db.append_message(parent, "user", str(i))
    wm_start = db.get_active_message_watermark(parent)
    for _ in range(2):
        db.append_message(parent, "user", "foreign")
    ceiling = db.get_active_message_watermark(parent)
    child = "child_tail"
    holder = "att_tail"
    db.create_compression_attempt(
        attempt_id=holder,
        session_key=db.get_session(parent)["session_key"],
        parent_session_id=parent,
        input_history_version=0,
        input_watermark=wm_start,
        holder=holder,
    )
    db.transition_compression_attempt_pending_to_running(holder)
    db.try_acquire_compression_lock(parent, holder=holder, ttl_seconds=60)
    db.publish_compression_child(
        parent_session_id=parent,
        child_session_id=child,
        source="tui",
        messages=[{"role": "user", "content": "summary"}],
        watermark=wm_start,
        watermark_ceiling=ceiling,
        attempt_id=holder,
        compression_lock_holder=holder,
    )
    child_msgs = db.get_messages_as_conversation(child, include_ancestors=False)
    assert len(child_msgs) == 3


def test_normal_completion_commits_atomically(db):
    parent = _new_family(db, "tui")
    holder = "att_commit"
    db.create_compression_attempt(
        attempt_id=holder,
        session_key=db.get_session(parent)["session_key"],
        parent_session_id=parent,
        input_history_version=0,
        input_watermark=0,
        holder=holder,
    )
    db.transition_compression_attempt_pending_to_running(holder)
    db.try_acquire_compression_lock(parent, holder=holder, ttl_seconds=60)
    db.publish_compression_child(
        parent_session_id=parent,
        child_session_id="child_commit",
        source="tui",
        messages=[{"role": "user", "content": "h"}],
        watermark=None,
        attempt_id=holder,
        compression_lock_holder=holder,
    )
    row = db.get_compression_attempt(holder)
    assert row["state"] == "committed"
    assert row["child_session_key"] == "child_commit"


def test_duplicate_settlement_exactly_once(db):
    parent = _new_family(db, "tui")
    holder = "att_dup"
    db.create_compression_attempt(
        attempt_id=holder,
        session_key=db.get_session(parent)["session_key"],
        parent_session_id=parent,
        input_history_version=0,
        input_watermark=0,
        holder=holder,
    )
    db.transition_compression_attempt_pending_to_running(holder)
    db.try_acquire_compression_lock(parent, holder=holder, ttl_seconds=60)
    db.publish_compression_child(
        parent_session_id=parent,
        child_session_id="child_dup",
        source="tui",
        messages=[{"role": "user", "content": "h"}],
        watermark=None,
        attempt_id=holder,
        compression_lock_holder=holder,
    )
    with pytest.raises(Exception):
        db.publish_compression_child(
            parent_session_id=parent,
            child_session_id="child_dup2",
            source="tui",
            messages=[{"role": "user", "content": "h2"}],
            watermark=None,
            attempt_id=holder,
            compression_lock_holder=holder,
        )


def test_stale_after_second_rotation(db):
    parent = _new_family(db, "tui")
    a1 = "att_stale1"
    db.create_compression_attempt(
        attempt_id=a1,
        session_key=db.get_session(parent)["session_key"],
        parent_session_id=parent,
        input_history_version=0,
        input_watermark=0,
        holder=a1,
    )
    db.transition_compression_attempt_pending_to_running(a1)
    db.try_acquire_compression_lock(parent, holder=a1, ttl_seconds=60)
    db.publish_compression_child(
        parent_session_id=parent,
        child_session_id="child_stale1",
        source="tui",
        messages=[{"role": "user", "content": "h"}],
        watermark=None,
        attempt_id=a1,
        compression_lock_holder=a1,
    )
    a2 = "att_stale2"
    db.create_compression_attempt(
        attempt_id=a2,
        session_key=db.get_session("child_stale1")["session_key"],
        parent_session_id="child_stale1",
        input_history_version=0,
        input_watermark=0,
        holder=a2,
    )
    db.transition_compression_attempt_pending_to_running(a2)
    db.try_acquire_compression_lock("child_stale1", holder=a2, ttl_seconds=60)
    db.publish_compression_child(
        parent_session_id="child_stale1",
        child_session_id="child_stale2",
        source="tui",
        messages=[{"role": "user", "content": "h2"}],
        watermark=None,
        attempt_id=a2,
        compression_lock_holder=a2,
    )
    assert db._is_compression_attempt_stale(a1) is True
    assert db._is_compression_attempt_stale(a2) is False
