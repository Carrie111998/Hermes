"""Tests for SessionStore.rewind_session — the gateway /undo [N] primitive.

The gateway /undo backs up N user turns by soft-deleting the truncated rows
in state.db (active=0, kept for audit, hidden from re-prompts/search) via
SessionDB.rewind_to_message, rather than the old hard rewrite_transcript.
load_transcript returns only the active view. See issue #21910.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_state import SessionDB
from gateway.config import GatewayConfig
from gateway.session import SessionStore


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db = SessionDB(db_path=tmp_path / "state.db")
    s = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    s._db = db  # use the same DB instance the fixture seeds
    return s


def _seed(store, sid, source="telegram", turns=3):
    store._db.create_session(sid, source=source)
    for i in range(1, turns + 1):
        store._db.append_message(sid, "user", f"q{i}")
        store._db.append_message(sid, "assistant", f"a{i}")
    return sid


def test_rewind_default_one_turn(store):
    sid = _seed(store, "gw-1")
    res = store.rewind_session(sid)
    assert res["turns_undone"] == 1
    assert res["target_text"] == "q3"
    assert res["rewound_count"] == 2  # q3 + a3
    active = store.load_transcript(sid)
    assert [m["role"] for m in active] == ["user", "assistant", "user", "assistant"]


def test_rewind_n_turns(store):
    sid = _seed(store, "gw-2")
    res = store.rewind_session(sid, 2)
    assert res["turns_undone"] == 2
    assert res["target_text"] == "q2"
    assert res["rewound_count"] == 4  # q2,a2,q3,a3
    assert len(store.load_transcript(sid)) == 2  # q1,a1


def test_rewind_soft_deletes_rows_for_audit(store):
    sid = _seed(store, "gw-3")
    store.rewind_session(sid, 1)
    all_rows = store._db.get_messages(sid, include_inactive=True)
    assert len(all_rows) == 6  # nothing hard-deleted
    assert sum(1 for r in all_rows if r["active"] == 1) == 4
    assert store._db.get_session(sid)["rewind_count"] == 1


def test_rewind_clamps_to_oldest_turn(store):
    sid = _seed(store, "gw-4", turns=2)
    res = store.rewind_session(sid, 99)
    assert res["target_text"] == "q1"
    assert len(store.load_transcript(sid)) == 0


def test_rewind_empty_session_returns_none(store):
    store._db.create_session("gw-5", source="discord")
    assert store.rewind_session("gw-5") is None


def test_rewind_clamps_negative_count_to_one(store):
    sid = _seed(store, "gw-6")
    res = store.rewind_session(sid, -5)
    assert res["turns_undone"] == 1
    assert res["target_text"] == "q3"


def test_rewind_returns_platform_message_ids_for_gateway_cleanup(store):
    sid = "gw-platform-ids"
    store._db.create_session(sid, source="telegram")
    store._db.append_message(sid, "user", "q1", platform_message_id="101")
    store._db.append_message(sid, "assistant", "a1", platform_message_id="102")
    store._db.append_message(sid, "user", "q2", platform_message_id="201")
    store._db.append_message(
        sid,
        "assistant",
        "a2",
        platform_message_id='["202", "203"]',
    )

    res = store.rewind_session(sid, 1)

    assert res["rewound_count"] == 2
    assert res["rewound_messages"] == [
        {"id": 3, "role": "user", "platform_message_id": "201"},
        {"id": 4, "role": "assistant", "platform_message_id": '["202", "203"]'},
    ]


def test_set_latest_assistant_platform_message_ids_uses_scalar_or_json(store):
    sid = "gw-delivery-ids"
    store._db.create_session(sid, source="telegram")
    store._db.append_message(sid, "user", "q1")
    store._db.append_message(sid, "assistant", "a1")

    assert store._db.set_latest_assistant_platform_message_ids(sid, ["301"])
    rows = store._db.get_messages(sid)
    assert rows[-1]["platform_message_id"] == "301"

    assert store._db.set_latest_assistant_platform_message_ids(
        sid,
        ["301", "302", "302", None, "__no_edit__"],
    )
    rows = store._db.get_messages(sid)
    assert rows[-1]["platform_message_id"] == '["301", "302"]'


def test_rewind_pins_raw_active_ids_when_projection_hides_review_harness(store):
    sid = _seed(store, "gw-review-harness", turns=2)
    store._db.append_message(
        sid,
        "user",
        "Review the conversation above and update the skill library safely",
    )
    store._db.append_message(sid, "assistant", "curator-only reply")

    # Legacy background-review rows are intentionally absent from replay, but
    # they remain physical active rows that the rewind CAS must pin.
    assert [message["content"] for message in store.load_transcript(sid)] == [
        "q1",
        "a1",
        "q2",
        "a2",
    ]

    result = store.rewind_session(sid)

    assert result is not None
    assert result["target_text"] == "q2"
    assert result["rewound_count"] == 4
    assert [message["content"] for message in store.load_transcript(sid)] == [
        "q1",
        "a1",
    ]


def test_rewind_fails_closed_when_transcript_changes_after_snapshot(
    store, monkeypatch
):
    sid = _seed(store, "gw-cas", turns=2)
    sibling = SessionDB(db_path=store._db.db_path)
    original_rewind = store._db.rewind_to_message

    def _append_then_rewind(*args, **kwargs):
        sibling.append_message(sid, "assistant", "concurrent tail")
        return original_rewind(*args, **kwargs)

    monkeypatch.setattr(store._db, "rewind_to_message", _append_then_rewind)

    assert store.rewind_session(sid) is None

    rows = store._db._conn.execute(
        "SELECT content, active FROM messages "
        "WHERE session_id = ? ORDER BY id",
        (sid,),
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("q1", 1),
        ("a1", 1),
        ("q2", 1),
        ("a2", 1),
        ("concurrent tail", 1),
    ]
    sibling.close()


def test_rewind_fails_closed_when_new_turn_lands_after_id_snapshot(
    store, monkeypatch
):
    sid = _seed(store, "gw-snapshot-order", turns=2)
    sibling = SessionDB(db_path=store._db.db_path)
    original_load = store._db.get_messages_as_conversation

    def _load_then_append(*args, **kwargs):
        snapshot = original_load(*args, **kwargs)
        sibling.append_message(sid, "user", "q3-from-other-process")
        sibling.append_message(sid, "assistant", "a3-from-other-process")
        return snapshot

    monkeypatch.setattr(store._db, "get_messages_as_conversation", _load_then_append)

    assert store.rewind_session(sid) is None

    rows = store._db._conn.execute(
        "SELECT content, active FROM messages "
        "WHERE session_id = ? ORDER BY id",
        (sid,),
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("q1", 1),
        ("a1", 1),
        ("q2", 1),
        ("a2", 1),
        ("q3-from-other-process", 1),
        ("a3-from-other-process", 1),
    ]
    sibling.close()
