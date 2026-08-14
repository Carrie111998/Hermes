"""Regression tests for #82001 — flush adopts the live compression continuation.

When context compression closes a session while a client still writes against
the old id, ``_flush_messages_to_session_db_unlocked`` used to swallow
``CompressionSessionClosedError`` as a generic write failure: the turn died
with ``session_persistence_failed`` and the user was told "this is often a
full disk" even though the DB was healthy.

The fix adopts the unique live continuation (``find_live_compression_child``)
at most once per flush and replays the failed batch against it.  0 or >1
children still fail closed, but are now classified as a ``compression`` cause
so the turn-completion explanation is honest.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


def _make_agent(session_db, session_id="parent-session"):
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=session_db,
            session_id=session_id,
            skip_context_files=True,
            skip_memory=True,
        )
    return agent


def _close_by_compression(db, parent_id, child_ids=()):
    """Mark *parent_id* compression-closed and publish continuation rows."""
    with db._lock:
        db._conn.execute(
            "UPDATE sessions SET ended_at = CURRENT_TIMESTAMP, "
            "end_reason = 'compression' WHERE id = ?",
            (parent_id,),
        )
        db._conn.commit()
    for child_id in child_ids:
        db.create_session(session_id=child_id, source="test")
        with db._lock:
            db._conn.execute(
                "UPDATE sessions SET parent_session_id = ? WHERE id = ?",
                (parent_id, child_id),
            )
            db._conn.commit()


@pytest.fixture()
def session_db(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    yield db
    db.close()


class TestFlushCompressionAdoption:
    def test_flush_adopts_unique_live_child(self, session_db):
        """Closed parent + exactly one live child → rows land on the child."""
        agent = _make_agent(session_db)
        agent._ensure_db_session()
        _close_by_compression(session_db, "parent-session", ["child-session"])

        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        result = agent._flush_messages_to_session_db(msgs)

        assert result is True
        assert agent.session_id == "child-session"
        child_rows = session_db.get_messages("child-session")
        assert [m["content"] for m in child_rows] == ["hello", "hi there"]
        assert session_db.get_messages("parent-session") == []

    def test_flush_fails_closed_with_no_child(self, session_db):
        """Closed parent + no continuation → still fails, but cause is honest."""
        agent = _make_agent(session_db)
        agent._ensure_db_session()
        _close_by_compression(session_db, "parent-session", [])

        result = agent._flush_messages_to_session_db(
            [{"role": "user", "content": "hello"}]
        )

        assert result is False
        assert agent.session_id == "parent-session"
        assert agent._last_persistence_error_cause == "compression"

    def test_flush_fails_closed_with_ambiguous_children(self, session_db):
        """Closed parent + two live children → ambiguous, fail closed."""
        agent = _make_agent(session_db)
        agent._ensure_db_session()
        _close_by_compression(
            session_db, "parent-session", ["child-a", "child-b"]
        )

        result = agent._flush_messages_to_session_db(
            [{"role": "user", "content": "hello"}]
        )

        assert result is False
        assert agent.session_id == "parent-session"
        assert agent._last_persistence_error_cause == "compression"
        assert session_db.get_messages("child-a") == []
        assert session_db.get_messages("child-b") == []

    def test_adoption_probe_is_bounded_to_one(self, session_db):
        """A second Closed error (child closed mid-replay) fails closed —
        the adoption budget is one probe per flush, no recursion."""
        agent = _make_agent(session_db)
        agent._ensure_db_session()
        _close_by_compression(session_db, "parent-session", ["child-session"])
        # Close the child too, with its own continuation — the replay against
        # the child must NOT trigger a second adoption hop.
        _close_by_compression(session_db, "child-session", ["grandchild"])

        result = agent._flush_messages_to_session_db(
            [{"role": "user", "content": "hello"}]
        )

        assert result is False
        assert agent._last_persistence_error_cause == "compression"
        assert session_db.get_messages("grandchild") == []

    def test_next_flush_writes_directly_to_adopted_child(self, session_db):
        """After adoption, subsequent flushes target the child with no retry."""
        agent = _make_agent(session_db)
        agent._ensure_db_session()
        _close_by_compression(session_db, "parent-session", ["child-session"])

        first = [{"role": "user", "content": "hello"}]
        assert agent._flush_messages_to_session_db(first) is True
        assert agent.session_id == "child-session"

        second = first + [{"role": "assistant", "content": "reply"}]
        assert agent._flush_messages_to_session_db(second) is True
        contents = [m["content"] for m in session_db.get_messages("child-session")]
        assert contents == ["hello", "reply"]

    def test_normal_flush_unaffected(self, session_db):
        """A live session flush takes the plain path — no probe, no rebind."""
        agent = _make_agent(session_db)
        agent._ensure_db_session()

        result = agent._flush_messages_to_session_db(
            [{"role": "user", "content": "hello"}]
        )

        assert result is True
        assert agent.session_id == "parent-session"


class TestPersistenceCauseClassification:
    def test_closed_error_classifies_as_compression(self):
        from hermes_state import (
            CompressionSessionClosedError,
            classify_persistence_error,
        )

        exc = CompressionSessionClosedError("some-session")
        assert classify_persistence_error(exc) == "compression"
        # String form (RPC-wrapped) must classify identically.
        assert classify_persistence_error(str(exc)) == "compression"
