"""Legacy delegated sessions must migrate off human-facing sources."""

from __future__ import annotations

import json

from hermes_state import SCHEMA_VERSION, SessionDB


def test_v26_delegation_sources_migrate_without_touching_human_sessions(tmp_path):
    db_path = tmp_path / "legacy-delegation-sources.db"
    db = SessionDB(db_path=db_path)
    try:
        db.create_session("human", source="discord")
        db.create_session("delegate-parent", source="discord")
        db.create_session(
            "direct-delegate",
            source="discord",
            parent_session_id="delegate-parent",
            model_config={"_delegate_from": "delegate-parent", "keep": "direct"},
        )
        db.end_session("direct-delegate", "compression")
        db.create_session(
            "delegate-continuation",
            source="discord",
            parent_session_id="direct-delegate",
            model_config={"_delegate_from": "delegate-parent", "keep": "continuation"},
        )
        db.create_session(
            "branch",
            source="discord",
            parent_session_id="human",
            model_config={"_branched_from": "human"},
        )
        db.create_session(
            "already-subagent",
            source="subagent",
            parent_session_id="delegate-parent",
            model_config={"_delegate_from": "delegate-parent"},
        )
        db.create_session(
            "delegated-branch-marker",
            source="discord",
            parent_session_id="delegate-parent",
            model_config={
                "_delegate_from": "delegate-parent",
                "_branched_from": "delegate-parent",
            },
        )
        db.create_session("malformed-config", source="discord")
        db._conn.execute(
            "UPDATE sessions SET model_config = '{not-json' "
            "WHERE id = 'malformed-config'"
        )
        db.create_session("null-config", source="discord")
        db.create_session(
            "tool-child",
            source="tool",
            parent_session_id="delegate-parent",
            model_config={"_delegate_from": "delegate-parent"},
        )
        db._conn.execute("UPDATE schema_version SET version = 26")
        db._conn.commit()
    finally:
        db.close()

    migrated = SessionDB(db_path=db_path)
    try:
        sources = {
            row["id"]: row["source"]
            for row in migrated._conn.execute(
                "SELECT id, source FROM sessions ORDER BY id"
            ).fetchall()
        }
        assert sources == {
            "already-subagent": "subagent",
            "branch": "discord",
            "delegate-continuation": "subagent",
            "delegate-parent": "discord",
            "delegated-branch-marker": "subagent",
            "direct-delegate": "subagent",
            "human": "discord",
            "malformed-config": "discord",
            "null-config": "discord",
            "tool-child": "tool",
        }
        for session_id, expected_keep in (
            ("direct-delegate", "direct"),
            ("delegate-continuation", "continuation"),
        ):
            row = migrated.get_session(session_id)
            assert row is not None
            assert json.loads(row["model_config"])["keep"] == expected_keep
        assert migrated._conn.execute(
            "SELECT version FROM schema_version LIMIT 1"
        ).fetchone()[0] == SCHEMA_VERSION == 27
    finally:
        migrated.close()
