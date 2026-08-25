"""Storage invariants: bounded history, no duplicate answers, safe outbox."""

from __future__ import annotations

import time

import pytest

from kakao_legal_bot.app.db import pseudonymise


def test_history_is_trimmed_to_the_configured_window(db):
    for index in range(30):
        db.add_message("room-1", "user", f"메시지 {index}", keep_last=10)
    history = db.recent_messages("room-1", limit=50)
    assert len(history) == 10
    assert history[0].text == "메시지 20"
    assert history[-1].text == "메시지 29"


def test_recent_messages_come_back_oldest_first(db):
    db.add_message("room-1", "user", "첫번째")
    db.add_message("room-1", "bot", "두번째")
    assert [m.text for m in db.recent_messages("room-1")] == ["첫번째", "두번째"]


def test_rooms_upsert_without_clobbering_known_values(db):
    db.upsert_room("room-1", "상담방", "direct")
    db.upsert_room("room-1", "", "")
    room = db.get_room("room-1")
    assert room["room_name"] == "상담방"
    assert room["kind"] == "direct"


def test_room_flags_round_trip(db):
    db.upsert_room("room-1")
    db.set_room_flag("room-1", "muted", 1)
    assert db.get_room("room-1")["muted"] == 1


def test_mark_seen_is_true_once(db):
    assert db.mark_seen("log:1") is True
    assert db.mark_seen("log:1") is False
    assert db.mark_seen("log:2") is True


def test_consultation_is_reused_until_closed(db):
    db.upsert_room("room-1")
    first = db.get_or_create_consultation("room-1", "홍길동")
    second = db.get_or_create_consultation("room-1", "홍길동")
    assert first["id"] == second["id"]

    db.update_consultation(int(first["id"]), status="closed")
    third = db.get_or_create_consultation("room-1", "홍길동")
    assert third["id"] != first["id"]


def test_draft_lifecycle(db):
    draft_id = db.create_draft("room-1", "내용증명", "보증금 반환 청구", "본문")
    assert db.get_draft(draft_id).status == "pending_review"

    db.update_draft(draft_id, status="approved", lawyer_note="기한 수정함")
    draft = db.get_draft(draft_id)
    assert draft.status == "approved"
    assert draft.lawyer_note == "기한 수정함"
    assert [d.id for d in db.list_drafts("approved")] == [draft_id]


def test_outbox_claim_then_ack(db):
    first = db.enqueue_outbox("room-1", "메시지 1")
    db.enqueue_outbox("room-1", "메시지 2")
    assert db.outbox_depth() == 2

    claimed = db.claim_outbox(limit=10)
    assert [row["id"] for row in claimed] == [first, first + 1]
    assert db.outbox_depth() == 0  # claimed rows are not handed out twice

    db.ack_outbox([first], ok=True)
    db.ack_outbox([first + 1], ok=False, error="iris down")
    assert db.outbox_depth() == 1  # the failed one is queued again


def test_stale_claims_are_requeued(db):
    db.enqueue_outbox("room-1", "메시지")
    db.claim_outbox()
    assert db.outbox_depth() == 0
    assert db.requeue_stale_outbox(older_than_s=-1) == 1
    assert db.outbox_depth() == 1


def test_answer_log_and_daily_count(db):
    db.log_answer("room-1", "질문", "답변", citations=["민법 제618조"], latency_ms=42)
    assert db.count_answers_since("room-1", time.time() - 60) == 1
    assert db.count_answers_since("room-2", time.time() - 60) == 0


def test_cache_expires(db):
    db.cache_put("key", "body")
    assert db.cache_get("key", ttl_s=60) == "body"

    db._exec("UPDATE http_cache SET created_at = ?", (time.time() - 7200,))
    assert db.cache_get("key", ttl_s=3600) is None
    # ttl_s <= 0 means "never expire" — used to serve a cached law lookup
    # when the upstream API is down.
    assert db.cache_get("key", ttl_s=0) == "body"


def test_retention_purge(db):
    db.add_message("room-1", "user", "오래된 메시지")
    db._exec("UPDATE messages SET created_at = ?", (time.time() - 100 * 86400,))
    db.add_message("room-1", "user", "새 메시지")

    assert db.purge_old_messages(older_than_days=90) == 1
    assert [m.text for m in db.recent_messages("room-1")] == ["새 메시지"]
    # 0 disables retention entirely rather than deleting everything.
    assert db.purge_old_messages(older_than_days=0) == 0


def test_an_existing_v1_database_gains_the_new_column(tmp_path):
    """CREATE TABLE IF NOT EXISTS is a no-op — a deployed bot needs ALTER."""
    import sqlite3

    from kakao_legal_bot.app.db import Database

    path = tmp_path / "old.sqlite3"
    legacy = sqlite3.connect(str(path))
    legacy.execute(
        """
        CREATE TABLE rooms (
            room_id TEXT PRIMARY KEY, room_name TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'unknown', consult_id INTEGER,
            lawyer_takeover INTEGER NOT NULL DEFAULT 0, muted INTEGER NOT NULL DEFAULT 0,
            intro_sent INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        )
        """
    )
    legacy.execute(
        "INSERT INTO rooms(room_id, created_at, updated_at) VALUES('old-room', 1, 1)"
    )
    legacy.commit()
    legacy.close()

    upgraded = Database(path)
    try:
        room = upgraded.get_room("old-room")
        assert room is not None
        assert room["first_alerts_done"] == 0  # existing rooms default to "not yet"
        upgraded.set_room_flag("old-room", "first_alerts_done", 1)
        assert upgraded.get_room("old-room")["first_alerts_done"] == 1
    finally:
        upgraded.close()


def test_unknown_room_flags_are_rejected(db):
    db.upsert_room("room-1")
    with pytest.raises(ValueError, match="not a room flag"):
        db.set_room_flag("room-1", "room_name; DROP TABLE rooms", 1)


def test_pseudonymise_is_stable_and_salted():
    assert pseudonymise("uid-1", "salt") == pseudonymise("uid-1", "salt")
    assert pseudonymise("uid-1", "salt") != pseudonymise("uid-1", "other-salt")
    assert pseudonymise("uid-1", "salt") != "uid-1"
    assert pseudonymise("", "salt") == ""
