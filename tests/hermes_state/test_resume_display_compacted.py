"""Resume display projection must surface compaction-archived rows (#95906).

``get_resume_conversations`` used to fetch its lineage with ``active = 1``
only, so after in-place compaction the resume's ``display_history`` silently
ended at the compaction boundary — earlier turns were unreachable in the UI
even though ``get_messages(include_compacted=True)`` (the REST/backfill read)
served them. The two display surfaces disagreed; this pins the fixed contract:

  * ``display_history`` includes ``active=0, compacted=1`` rows (deduped
    cross-generation with the same helper ``get_messages`` uses), while
  * ``model_history`` stays active-only (the AI context keeps the compressed
    summary + protected tail), and
  * soft-deleted Undo/Rewind rows (``active=0, compacted=0``) stay excluded.
"""

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _seed_compacted(db, sid="s1"):
    """4 archived (compacted) turns + summary + 1 live turn."""
    db.create_session(sid, source="cli")
    db.append_messages_batch(
        sid,
        [
            {"role": "user", "content": "old q1"},
            {"role": "assistant", "content": "old a1"},
            {"role": "user", "content": "old q2"},
            {"role": "assistant", "content": "old a2"},
        ],
    )
    db.archive_and_compact(
        sid,
        [
            {"role": "assistant", "content": "summary of old turns"},
            {"role": "user", "content": "live q1"},
            {"role": "assistant", "content": "live a1"},
        ],
    )


class TestResumeDisplayCompacted:
    def test_display_includes_compacted_rows_model_stays_active_only(self, db):
        _seed_compacted(db)
        model_history, display_history = db.get_resume_conversations("s1")
        display_contents = [m["content"] for m in display_history]
        # The archived turns are durable display history: they must be in the
        # resume's display projection instead of ending at the summary.
        for archived in ("old q1", "old a1", "old q2", "old a2"):
            assert archived in display_contents
        # The model-fed projection is unchanged: compressed summary + live
        # tail only — the model must never see the archived rows again.
        model_contents = [m["content"] for m in model_history]
        for archived in ("old q1", "old a1", "old q2", "old a2"):
            assert archived not in model_contents
        assert "summary of old turns" in model_contents
        assert "live q1" in model_contents

    def test_display_agrees_with_include_compacted_rest_read(self, db):
        """Resume display and the paged REST display read must agree."""
        _seed_compacted(db)
        _, display_history = db.get_resume_conversations("s1")
        rest_rows = db.get_messages("s1", include_compacted=True)
        assert [m["content"] for m in display_history] == [
            m["content"] for m in rest_rows
        ]

    def test_display_dedupes_cross_generation_copies(self, db):
        """A compaction epoch's verbatim tail copy must appear exactly once."""
        db.create_session("s1", source="cli")
        db.append_messages_batch(
            "s1",
            [
                {"role": "user", "content": "carry q"},
                {"role": "assistant", "content": "carry a"},
            ],
        )
        live_ids = [m["id"] for m in db.get_messages("s1")]

        # Simulate one compaction epoch: duplicate the rows verbatim (same
        # content AND timestamp — the real copy-protected-tail behaviour) as
        # active=0, compacted=1.
        def _copy_tail(conn):
            placeholders = ",".join("?" * len(live_ids))
            conn.execute(
                f"""
                INSERT INTO messages
                    (session_id, role, content, tool_call_id, tool_calls,
                     tool_name, timestamp, active, compacted)
                SELECT session_id, role, content, tool_call_id, tool_calls,
                       tool_name, timestamp, 0, 1
                FROM messages
                WHERE session_id = ? AND id IN ({placeholders})
                """,
                ["s1", *live_ids],
            )

        db._execute_write(_copy_tail)
        _, display_history = db.get_resume_conversations("s1")
        contents = [m["content"] for m in display_history]
        assert contents.count("carry q") == 1
        assert contents.count("carry a") == 1
        # Dedupe winner is the live row (same semantics as get_messages).
        assert [m.get("_row_id") for m in display_history] == live_ids

    def test_soft_deleted_rows_stay_excluded_from_display(self, db):
        _seed_compacted(db)
        live = db.get_messages("s1")
        user_msg = next(m for m in reversed(live) if m["role"] == "user")
        db.rewind_to_message("s1", user_msg["id"])
        _, display_history = db.get_resume_conversations("s1")
        # rewind_to_message soft-deletes the user row and everything after it
        # (active=0, compacted=0) — audit rows, not display history.
        assert "live q1" not in [m["content"] for m in display_history]
        # The compacted rows remain reachable.
        assert "old q1" in [m["content"] for m in display_history]
