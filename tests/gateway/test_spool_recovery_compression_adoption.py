"""Sibling coverage for the #82001 bug class outside the agent flush path.

Two more writers could target a compression-closed session id:

* startup spool recovery (``gateway/shutdown_flush.recover_pending_to_db``)
  replayed rows to the recorded id; a closed id made every startup raise and
  retry the same file forever;
* the shared adoption helper used by both paths must fail closed on 0/>1
  children.
"""

import json
import time

import pytest


@pytest.fixture()
def session_db(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    yield db
    db.close()


def _close_by_compression(db, parent_id, child_ids=()):
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


class TestSpoolRecoveryAdoption:
    def _spool_cap_drop(self, flush_dir, sid, content):
        from gateway.shutdown_flush import TRANSCRIPT_CAP_DROP_REASON

        payload = {
            "session_key": sid,
            "reason": TRANSCRIPT_CAP_DROP_REASON,
            "ts": int(time.time()),
            "seq": 1,
            "data": {
                "session_id": sid,
                "message": {"role": "user", "content": content},
            },
        }
        path = flush_dir / f"pending-test-{content}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_startup_recovery_adopts_closed_session(
        self, session_db, tmp_path, monkeypatch
    ):
        import gateway.shutdown_flush as sf

        flush_dir = tmp_path / "pending_messages"
        flush_dir.mkdir()
        monkeypatch.setattr(sf, "_get_flush_dir", lambda: flush_dir)

        session_db.create_session(session_id="old-sid", source="test")
        spool_path = self._spool_cap_drop(flush_dir, "old-sid", "spooled-row")
        _close_by_compression(session_db, "old-sid", ["live-child"])

        recovered = sf.recover_pending_to_db(session_db=session_db)

        assert recovered == 1
        assert not spool_path.exists()
        assert [m["content"] for m in session_db.get_messages("live-child")] == [
            "spooled-row"
        ]
        assert session_db.get_messages("old-sid") == []

    def test_startup_recovery_preserves_file_when_ambiguous(
        self, session_db, tmp_path, monkeypatch
    ):
        import gateway.shutdown_flush as sf

        flush_dir = tmp_path / "pending_messages"
        flush_dir.mkdir()
        monkeypatch.setattr(sf, "_get_flush_dir", lambda: flush_dir)

        session_db.create_session(session_id="old-sid", source="test")
        spool_path = self._spool_cap_drop(flush_dir, "old-sid", "kept-row")
        _close_by_compression(session_db, "old-sid", ["child-a", "child-b"])

        recovered = sf.recover_pending_to_db(session_db=session_db)

        assert recovered == 0
        assert spool_path.exists()  # preserved for the next attempt
        assert session_db.get_messages("child-a") == []
        assert session_db.get_messages("child-b") == []


class TestAdoptionHelper:
    def test_helper_appends_directly_when_live(self, session_db):
        from gateway.shutdown_flush import _append_with_compression_adoption

        session_db.create_session(session_id="live-sid", source="test")
        _append_with_compression_adoption(
            session_db, session_id="live-sid", role="user", content="direct"
        )
        assert [m["content"] for m in session_db.get_messages("live-sid")] == [
            "direct"
        ]

    def test_helper_reraises_without_finder(self, session_db):
        from gateway.shutdown_flush import _append_with_compression_adoption
        from hermes_state import CompressionSessionClosedError

        session_db.create_session(session_id="old-sid", source="test")
        _close_by_compression(session_db, "old-sid", ["live-child"])

        class _NoFinderDB:
            def append_message(self, **kwargs):
                return session_db.append_message(**kwargs)

        with pytest.raises(CompressionSessionClosedError):
            _append_with_compression_adoption(
                _NoFinderDB(), session_id="old-sid", role="user", content="x"
            )
