"""Regression tests for #97794: FTS5-index-only failures must fail open.

A corrupt FTS5 shadow table makes MATCH queries raise the malformed /
"fts5: corrupt structure record" class while the canonical messages table
is intact. Before this fix:

* ``classify_persistence_error`` bucketed those errors as whole-file
  ``corrupt``, killing the turn and recommending ``.recover`` / backup
  restore on a healthy database;
* ``search_messages`` re-raised when the one-shot in-place rebuild was
  refused, so ``session_search`` returned "Search failed: file is not a
  database" instead of degrading to LIKE.

These tests pin the fail-open contract: index-scoped classification, LIKE
degradation on the search path, and the ``db_path`` annotation that lets
the classifier verify scope.
"""

import sqlite3

import pytest

from hermes_state import SessionDB, classify_persistence_error


@pytest.fixture
def db(tmp_path):
    d = SessionDB(db_path=tmp_path / "state.db")
    yield d
    try:
        d.close()
    except Exception:
        pass


def _seed(db, session_id="s1"):
    db.create_session(session_id=session_id, source="cli")
    db.append_message(session_id, role="user", content="hello world 0")
    db.append_message(session_id, role="assistant", content="reply about pizza 0")
    db.append_message(session_id, role="user", content="hello world 1")
    db.append_message(session_id, role="assistant", content="reply about pizza 1")


def _corrupt_fts_shadow(db_path):
    """Overwrite the messages_fts shadow b-tree blocks so MATCH queries
    raise the FTS5 corruption class (read-path corruption)."""
    raw = sqlite3.connect(str(db_path), isolation_level=None)
    raw.execute("UPDATE messages_fts_data SET block = X'BADC0FFEE0DDF00D'")
    raw.close()


def _corrupt_trigram_shadow(db_path):
    """Overwrite the messages_fts_trigram shadow b-tree blocks."""
    raw = sqlite3.connect(str(db_path), isolation_level=None)
    raw.execute("UPDATE messages_fts_trigram_data SET block = X'BADC0FFEE0DDF00D'")
    raw.close()


def test_search_falls_back_to_like_when_rebuild_refused(tmp_path):
    """A corrupt FTS index with no rebuild available must degrade to LIKE
    over the canonical messages table, not raise (#97794)."""
    db_path = tmp_path / "state.db"
    d = SessionDB(db_path=db_path)
    try:
        _seed(d)
    finally:
        d.close()
    _corrupt_fts_shadow(db_path)

    d = SessionDB(db_path=db_path)
    try:
        # Refuse the one-shot rebuild (already attempted) so the fallback
        # path is what answers.
        d._fts_runtime_rebuild_attempted = True
        hits = d.search_messages("pizza", limit=5)
        assert len(hits) == 2
        snippets = {h["snippet"] for h in hits}
        assert any("pizza 0" in s for s in snippets)
        assert any("pizza 1" in s for s in snippets)
    finally:
        d.close()


def test_search_self_heals_when_rebuild_available(tmp_path):
    """With the one-shot rebuild available, search still self-heals in
    place (existing #66296 behavior must not regress)."""
    db_path = tmp_path / "state.db"
    d = SessionDB(db_path=db_path)
    try:
        _seed(d)
    finally:
        d.close()
    _corrupt_fts_shadow(db_path)

    d = SessionDB(db_path=db_path)
    try:
        hits = d.search_messages("pizza", limit=5)
        assert len(hits) == 2
    finally:
        d.close()


def test_append_message_annotates_db_path_on_error(tmp_path):
    """The transcript-write boundary must attach the resolved db path to
    raised sqlite3 errors so classify_persistence_error can verify whether
    corruption markers are FTS-scoped (#97794)."""
    from unittest.mock import patch as _patch

    db_path = tmp_path / "state.db"
    d = SessionDB(db_path=db_path)
    try:
        _seed(d)
    finally:
        d.close()
    _corrupt_fts_shadow(db_path)

    d = SessionDB(db_path=db_path)
    try:
        # Refuse the rebuild AND the fail-open detach so the raw error
        # propagates with the db_path annotation attached.
        d._fts_runtime_rebuild_attempted = True
        with _patch.object(d, "_enter_fts_fail_open", return_value=False):
            with pytest.raises(sqlite3.DatabaseError) as exc_info:
                d.append_message("s1", role="user", content="boom")
        assert getattr(exc_info.value, "db_path", None) == str(db_path)
        # And the classifier now reads it as index-scoped.
        assert classify_persistence_error(exc_info.value) == "fts_index"
    finally:
        d.close()


def test_trigram_search_returns_none_on_corruption(tmp_path):
    """_run_trigram_search must return None (caller falls back) instead of
    raising when the substring-capable index is corrupt (#97794)."""
    db_path = tmp_path / "state.db"
    d = SessionDB(db_path=db_path)
    try:
        _seed(d)
    finally:
        d.close()
    _corrupt_trigram_shadow(db_path)

    d = SessionDB(db_path=db_path)
    try:
        d._fts_runtime_rebuild_attempted = True
        result = d._run_trigram_search(
            "pizza",
            order_by_sql="ORDER BY rank",
            include_inactive=False,
            limit=5,
            offset=0,
        )
        assert result is None
    finally:
        d.close()
