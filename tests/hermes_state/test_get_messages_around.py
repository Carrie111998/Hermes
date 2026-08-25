"""Tests for SessionDB.get_messages_around (anchored-window primitive).

Used by session_search both for the discovery shape (FTS5 match as anchor)
and the scroll shape (user-supplied anchor). Returns a window of messages
around the anchor plus before/after counts so callers can detect session
boundaries.
"""
import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _seed(db, sid="s1", n=10):
    """Create session with n alternating user/assistant messages, return ids ascending."""
    db.create_session(sid, source="cli")
    ids = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        # append_message returns the new id
        mid = db.append_message(sid, role=role, content=f"msg {i}")
        ids.append(mid)
    return ids


class TestBasicWindow:
    def test_returns_window_around_anchor(self, db):
        ids = _seed(db, n=10)
        anchor = ids[5]
        view = db.get_messages_around("s1", anchor, window=2)
        # Expected: 2 before + anchor + 2 after = 5 messages
        msgs = view["window"]
        assert len(msgs) == 5
        assert [m["id"] for m in msgs] == [ids[3], ids[4], ids[5], ids[6], ids[7]]
        assert view["messages_before"] == 2
        assert view["messages_after"] == 2

    def test_window_zero_returns_only_anchor(self, db):
        ids = _seed(db, n=5)
        view = db.get_messages_around("s1", ids[2], window=0)
        assert len(view["window"]) == 1
        assert view["window"][0]["id"] == ids[2]
        assert view["messages_before"] == 0
        assert view["messages_after"] == 0



class TestBoundaryDetection:
    """messages_before / messages_after tell the agent it's at start/end."""

    def test_at_session_start_messages_before_is_short(self, db):
        ids = _seed(db, n=10)
        # Anchor on first message; ask for window=5
        view = db.get_messages_around("s1", ids[0], window=5)
        assert view["messages_before"] == 0  # nothing before the first msg
        assert view["messages_after"] == 5
        # window contains anchor + 5 after = 6 messages
        assert len(view["window"]) == 6






class TestScrollPattern:
    """The forward/backward scroll loop the agent will run."""

    def test_scroll_forward_re_anchored_on_last_id(self, db):
        ids = _seed(db, n=20)
        anchor = ids[5]
        v1 = db.get_messages_around("s1", anchor, window=3)
        last_id = v1["window"][-1]["id"]
        v2 = db.get_messages_around("s1", last_id, window=3)
        # Boundary id (last_id) appears in both windows (in v2 it's the anchor)
        assert last_id in [m["id"] for m in v1["window"]]
        assert last_id in [m["id"] for m in v2["window"]]
        # v2's window extends beyond v1
        assert max(m["id"] for m in v2["window"]) > max(m["id"] for m in v1["window"])



class TestContentHydration:
    def test_content_is_decoded(self, db):
        ids = _seed(db, n=3)
        view = db.get_messages_around("s1", ids[1], window=1)
        for m in view["window"]:
            assert isinstance(m.get("content"), str)
            assert m["content"].startswith("msg ")

    def test_tool_calls_deserialized(self, db):
        db.create_session("s1", source="cli")
        # Message with tool_calls (pass list — append_message JSON-encodes it)
        tc_payload = [{"id": "t1", "function": {"name": "x", "arguments": "{}"}}]
        db.append_message("s1", role="assistant", content="", tool_calls=tc_payload)
        mid = db.append_message("s1", role="tool", content="result", tool_name="x")

        view = db.get_messages_around("s1", mid, window=2)
        # Find the assistant message with tool_calls
        asst = [m for m in view["window"] if m.get("role") == "assistant"]
        assert asst, "expected an assistant message"
        # tool_calls should be a list after hydration, not a string
        assert isinstance(asst[0].get("tool_calls"), list)


class TestRewoundRowsHidden:
    """Rewound rows (active=0, compacted=0) must not surface through the
    window — search already hides them as hits, and the window otherwise
    leaked exactly the content the user's rewind removed. Compaction-archived
    rows (compacted=1) stay visible, mirroring search_messages (#38763)."""

    def _rewind(self, db, mid):
        db._conn.execute(
            "UPDATE messages SET active = 0, compacted = 0 WHERE id = ?", (mid,)
        )
        db._conn.commit()

    def test_rewound_neighbor_is_excluded_from_window(self, db):
        ids = _seed(db, n=6)
        self._rewind(db, ids[3])
        view = db.get_messages_around("s1", ids[2], window=2)
        got = [m["id"] for m in view["window"]]
        assert ids[3] not in got
        # Window still fills from remaining live rows.
        assert ids[2] in got

    def test_rewound_anchor_itself_is_still_returned(self, db):
        ids = _seed(db, n=4)
        self._rewind(db, ids[1])
        view = db.get_messages_around("s1", ids[1], window=1)
        assert ids[1] in [m["id"] for m in view["window"]]

    def test_compaction_archived_neighbor_stays_visible(self, db):
        ids = _seed(db, n=4)
        db._conn.execute(
            "UPDATE messages SET active = 0, compacted = 1 WHERE id = ?", (ids[1],)
        )
        db._conn.commit()
        view = db.get_messages_around("s1", ids[2], window=2)
        assert ids[1] in [m["id"] for m in view["window"]]

    def test_rewound_rows_excluded_from_bookends(self, db):
        ids = _seed(db, n=10)
        self._rewind(db, ids[0])
        self._rewind(db, ids[9])
        view = db.get_anchored_view("s1", ids[5], window=1, bookend=2)
        bookend_ids = [m["id"] for m in view["bookend_start"] + view["bookend_end"]]
        assert ids[0] not in bookend_ids
        assert ids[9] not in bookend_ids
