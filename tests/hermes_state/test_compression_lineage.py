"""Contracts for the read-only compression lineage projection."""

import time

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _make_lineage(db: SessionDB):
    base = int(time.time()) - 10_000
    db.create_session("root", source="desktop")
    db.append_message("root", "user", "before compression")
    db.end_session("root", "compression")
    db.create_session("mid", source="desktop", parent_session_id="root")
    db.append_message("mid", "assistant", "middle segment")
    db.end_session("mid", "compression")
    db.create_session("tip", source="desktop", parent_session_id="mid")
    db.append_message("tip", "user", "current segment")

    # These share parent pointers but are not compression continuations.
    db.create_session("branch", source="desktop", parent_session_id="root")
    db.create_session("delegate", source="desktop", parent_session_id="mid")
    db.create_session("tool", source="tool", parent_session_id="mid")
    db.create_session("stale", source="desktop", parent_session_id="mid")
    db.end_session("stale", "ws_orphan_reap")
    db._conn.execute(
        "UPDATE sessions SET model_config = ? WHERE id = 'branch'",
        ('{"_branched_from":"root"}',),
    )
    db._conn.execute(
        "UPDATE sessions SET model_config = ? WHERE id = 'delegate'",
        ('{"_delegate_from":"mid"}',),
    )
    for offset, session_id in enumerate((
        "root",
        "mid",
        "tip",
        "branch",
        "delegate",
        "tool",
        "stale",
    )):
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (base + offset, session_id),
        )
    db._conn.commit()


def test_compression_lineage_is_root_to_tip_and_excludes_non_compression_children(db):
    _make_lineage(db)

    lineage = db.get_compression_lineage_metadata("tip")

    assert lineage["root_session_id"] == "root"
    assert lineage["tip_session_id"] == "tip"
    assert [segment["id"] for segment in lineage["segments"]] == ["root", "mid", "tip"]
    assert [segment["index"] for segment in lineage["segments"]] == [1, 2, 3]
    assert lineage["segments"][-1]["is_tip"] is True
    assert lineage["segments"][1]["parent_session_id"] == "root"
    assert lineage["segments"][0]["message_count"] == 1


def test_compression_lineage_from_an_excluded_sibling_is_its_own_single_segment(db):
    _make_lineage(db)

    lineage = db.get_compression_lineage_metadata("branch")

    assert lineage["root_session_id"] == "branch"
    assert [segment["id"] for segment in lineage["segments"]] == ["branch"]

    stale = db.get_compression_lineage_metadata("stale")
    assert stale["root_session_id"] == "stale"
    assert [segment["id"] for segment in stale["segments"]] == ["stale"]


@pytest.mark.parametrize("marker", ["_branched_from", "_delegate_from"])
def test_compression_continuation_keeps_inherited_non_continuation_marker(db, marker):
    db.create_session("origin", source="desktop")
    db.end_session("origin", "compression")
    db.create_session("fork", source="desktop", parent_session_id="origin")
    db._conn.execute(
        "UPDATE sessions SET model_config = ? WHERE id = 'fork'",
        (f'{{"{marker}":"origin"}}',),
    )
    db.end_session("fork", "compression")
    db.create_session("fork-tip", source="desktop", parent_session_id="fork")
    db._conn.execute(
        "UPDATE sessions SET model_config = ? WHERE id = 'fork-tip'",
        (f'{{"{marker}":"origin"}}',),
    )
    db._conn.commit()

    assert db.get_compression_tip("fork") == "fork-tip"
    assert db.get_compression_lineage("fork-tip") == ["fork", "fork-tip"]


def test_compression_lineage_excludes_an_only_stale_orphan_sibling(db):
    db.create_session("root", source="desktop")
    db.end_session("root", "compression")
    db.create_session("stale", source="desktop", parent_session_id="root")
    db.end_session("stale", "ws_orphan_reap")

    assert db.get_compression_tip("root") == "root"
    assert db.get_compression_lineage("root") == ["root"]
    assert db.get_compression_lineage("stale") == ["stale"]


def test_compression_tip_fails_closed_when_continuation_is_ambiguous(db):
    db.create_session("root", source="desktop")
    db.end_session("root", "compression")
    db.create_session("child-a", source="desktop", parent_session_id="root")
    db.create_session("child-b", source="desktop", parent_session_id="root")

    lineage = db.get_compression_lineage_metadata("root")

    assert db.get_compression_tip("root") == "root"
    assert [segment["id"] for segment in lineage["segments"]] == ["root"]
    assert lineage["integrity"]["ok"] is False
    assert lineage["integrity"]["reason"] == "ambiguous_continuation"
    assert set(lineage["integrity"]["candidate_ids"]) == {"child-a", "child-b"}


def test_compression_tip_fails_closed_on_unknown_closed_child(db):
    db.create_session("root", source="desktop")
    db.end_session("root", "compression")
    db.create_session("closed", source="desktop", parent_session_id="root")
    db.end_session("closed", "session_reset")

    lineage = db.get_compression_lineage_metadata("root")

    assert db.get_compression_tip("root") == "root"
    assert [segment["id"] for segment in lineage["segments"]] == ["root"]
    assert lineage["integrity"]["ok"] is False
    assert lineage["integrity"]["reason"] == "invalid_closed_child"
    assert lineage["integrity"]["candidate_ids"] == ["closed"]


def test_compression_lineage_metadata_resolves_root_and_tip_beyond_100_segments(db):
    segment_ids = [f"segment-{index:03d}" for index in range(102)]
    for index, segment_id in enumerate(segment_ids):
        db.create_session(
            segment_id,
            source="desktop",
            parent_session_id=segment_ids[index - 1] if index else None,
        )
        if index < len(segment_ids) - 1:
            db.end_session(segment_id, "compression")

    lineage = db.get_compression_lineage_metadata(segment_ids[-1])

    assert lineage["root_session_id"] == segment_ids[0]
    assert lineage["tip_session_id"] == segment_ids[-1]
    assert [segment["id"] for segment in lineage["segments"]] == segment_ids
    assert lineage["segments"][-1]["is_tip"] is True


def test_compression_lineage_tolerates_historical_malformed_model_config(db):
    db.create_session("root", source="desktop")
    db.end_session("root", "compression")
    db.create_session("tip", source="desktop", parent_session_id="root")
    db._conn.execute("UPDATE sessions SET model_config = '{malformed' WHERE id = 'tip'")
    db._conn.commit()

    assert db.get_compression_lineage("tip") == ["root", "tip"]


def test_compression_cycle_is_bounded_and_fails_closed_to_requested_session(db):
    db.create_session("cycle-a", source="desktop")
    db.create_session("cycle-b", source="desktop", parent_session_id="cycle-a")
    db.end_session("cycle-a", "compression")
    db.end_session("cycle-b", "compression")
    db._conn.execute(
        "UPDATE sessions SET parent_session_id = 'cycle-b', ended_at = 100 WHERE id = 'cycle-a'"
    )
    db._conn.execute("UPDATE sessions SET ended_at = 100 WHERE id = 'cycle-b'")
    db._conn.commit()

    assert db.get_compression_tip("cycle-a") == "cycle-a"
    assert db.resolve_resume_session_id("cycle-a") == "cycle-a"

    lineage = db.get_compression_lineage_metadata("cycle-a")
    assert lineage["root_session_id"] == "cycle-a"
    assert lineage["tip_session_id"] == "cycle-a"
    assert [segment["id"] for segment in lineage["segments"]] == ["cycle-a"]
    assert lineage["integrity"]["ok"] is False
    assert lineage["integrity"]["reason"] == "cycle"

    progress_calls = 0

    def abort_unbounded_query():
        nonlocal progress_calls
        progress_calls += 1
        return int(progress_calls > 10_000)

    db._conn.set_progress_handler(abort_unbounded_query, 100)
    try:
        listed = db.list_sessions_rich(
            include_children=True,
            limit=20,
            order_by_last_active=True,
        )
    finally:
        db._conn.set_progress_handler(None, 0)

    assert isinstance(listed, list)
    assert progress_calls <= 10_000


def test_session_list_projects_foreign_branch_marker_continuation_once(db):
    marker = "_branched_from"
    db.create_session("origin", source="desktop")
    db.end_session("origin", "compression")
    db.create_session("fork", source="desktop", parent_session_id="origin")
    db._conn.execute(
        "UPDATE sessions SET model_config = ? WHERE id = 'fork'",
        (f'{{"{marker}":"origin"}}',),
    )
    db.end_session("fork", "compression")
    db.create_session("fork-tip", source="desktop", parent_session_id="fork")
    db._conn.execute(
        "UPDATE sessions SET model_config = ? WHERE id = 'fork-tip'",
        (f'{{"{marker}":"origin"}}',),
    )
    db._conn.commit()

    listed = db.list_sessions_rich(limit=20, order_by_last_active=True)
    ids = [session["id"] for session in listed]

    assert ids.count("fork-tip") == 1
    assert "fork" not in ids


def test_session_list_tolerates_malformed_branch_model_config(db):
    db.create_session("parent", source="desktop")
    db.end_session("parent", "branched")
    db.create_session("legacy-branch", source="desktop", parent_session_id="parent")
    db._conn.execute(
        "UPDATE sessions SET model_config = '{malformed' WHERE id = 'legacy-branch'"
    )
    db._conn.commit()

    listed = db.list_sessions_rich(limit=20, order_by_last_active=True)

    assert "legacy-branch" in {session["id"] for session in listed}


@pytest.mark.parametrize("order_by_last_active", [False, True])
def test_session_preview_skips_hidden_compaction_checkpoint(db, order_by_last_active):
    db.create_session("branch", source="desktop")
    db.append_messages_batch(
        "branch",
        [
            {
                "content": "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns.",
                "display_kind": "hidden",
                "role": "user",
                "timestamp": 1,
            },
            {
                "content": "visible branch prompt",
                "role": "user",
                "timestamp": 2,
            },
        ],
    )

    listed = db.list_sessions_rich(
        limit=10,
        order_by_last_active=order_by_last_active,
    )
    row = next(session for session in listed if session["id"] == "branch")

    assert row["preview"] == "visible branch prompt"
    assert db.get_session_rich_row("branch")["preview"] == "visible branch prompt"
