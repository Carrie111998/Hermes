"""A failed transcript read must not masquerade as an empty history (#100788).

Incident shape: a malformed ``state.db`` made every
``SessionStore.load_transcript`` raise; the except-block swallowed it and
returned ``[]``.  Restore then rebuilt the turn from "no history", so a
long-running chat silently restarted as a brand-new conversation and the
model happily answered as if nothing had ever been discussed.

Two guarantees under test:
  A. ``load_transcript`` raises ``TranscriptReadError`` on a read failure,
     while a genuinely empty session still returns ``[]``.
  B. The gateway restore path degrades loudly: history stays empty, and a
     per-turn ephemeral notice is queued telling the model the history
     exists but is unreadable.

Offline: SQLite on tmp_path only, no network.
"""

import sqlite3

import pytest

from gateway.config import GatewayConfig
from gateway.session import SessionStore, TranscriptReadError


@pytest.fixture
def store(tmp_path):
    return SessionStore(sessions_dir=tmp_path / "gw", config=GatewayConfig())


# --------------------------------------------------------------------------
# A. read failure != empty transcript
# --------------------------------------------------------------------------


class TestLoadTranscriptReadFailure:
    def test_read_failure_raises_instead_of_returning_empty(self, store, monkeypatch):
        db = store._db
        assert db is not None
        db.create_session("s1", "telegram", session_key="telegram:1")
        db.append_message("s1", "user", "the conversation we must not forget")

        boom = sqlite3.DatabaseError("database disk image is malformed")

        def _raise(*_args, **_kwargs):
            raise boom

        monkeypatch.setattr(db, "get_messages_as_conversation", _raise)

        with pytest.raises(TranscriptReadError) as excinfo:
            store.load_transcript("s1")

        assert excinfo.value.session_id == "s1"
        assert excinfo.value.__cause__ is boom

    def test_genuinely_empty_session_still_returns_empty_list(self, store):
        db = store._db
        assert db is not None
        db.create_session("s2", "telegram", session_key="telegram:2")

        assert store.load_transcript("s2") == []

    def test_no_db_still_returns_empty_list(self, store):
        # "No DB for this session" really is an empty transcript, not a
        # failure — that path must keep its [] contract.
        store._db = None
        assert store.load_transcript("nope") == []


# --------------------------------------------------------------------------
# B. restore path: empty history + a degraded-history notice
# --------------------------------------------------------------------------


class _FailingStore:
    async def load_transcript(self, session_id: str):
        raise TranscriptReadError(session_id) from sqlite3.DatabaseError("malformed")


class _WorkingStore:
    def __init__(self, history):
        self._history = history

    async def load_transcript(self, session_id: str):
        return list(self._history)


def _make_runner(async_store):
    """A stub carrying only the restore helpers under test."""
    from gateway.run import GatewayRunner

    class _Stub:
        _load_history_for_turn = GatewayRunner._load_history_for_turn
        _note_degraded_history = GatewayRunner._note_degraded_history
        _take_degraded_history_notice = GatewayRunner._take_degraded_history_notice

    stub = _Stub()
    stub._degraded_history_notices = {}
    stub.async_session_store = async_store
    return stub


class TestDegradedRestoreNotice:
    def test_notice_says_history_exists_and_is_unreadable(self):
        from gateway.run import build_degraded_history_notice

        notice = build_degraded_history_notice("sess-abc")
        lowered = notice.lower()

        assert "sess-abc" in notice
        # NOT a new conversation, and the model must not fill the gap.
        assert "not a new conversation" in lowered
        assert "guess" in lowered or "invent" in lowered
        # Operator-actionable: name the store that needs attention.
        assert "state.db" in lowered

    @pytest.mark.asyncio
    async def test_failed_read_yields_empty_history_plus_notice(self):
        runner = _make_runner(_FailingStore())

        history = await runner._load_history_for_turn("telegram:1", "sess-abc")

        assert history == []
        notice = runner._take_degraded_history_notice("telegram:1")
        assert notice, "a failed read must queue a degraded-history notice"
        assert "sess-abc" in notice
        assert "not a new conversation" in notice.lower()
        # One-shot: the notice belongs to the failed turn only.
        assert runner._take_degraded_history_notice("telegram:1") == ""

    @pytest.mark.asyncio
    async def test_failed_read_does_not_inject_rows_into_history(self):
        runner = _make_runner(_FailingStore())

        history = await runner._load_history_for_turn("telegram:1", "sess-abc")

        # The notice rides the ephemeral prompt, never the transcript: no
        # synthetic system/user row may appear in the replayed history.
        assert history == []
        assert not any(isinstance(m, dict) for m in history)

    @pytest.mark.asyncio
    async def test_successful_read_queues_no_notice(self):
        rows = [{"role": "user", "content": "hi"}]
        runner = _make_runner(_WorkingStore(rows))

        history = await runner._load_history_for_turn("telegram:1", "sess-abc")

        assert history == rows
        assert runner._take_degraded_history_notice("telegram:1") == ""
