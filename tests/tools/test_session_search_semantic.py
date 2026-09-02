"""Tests for the semantic enhancement of session_search (optional engine).

The embedding engine (session_embedder) is optional: if it is missing or
raises, the tool must degrade cleanly to FTS5-only results while still
honouring the ``semantic=True`` flag in the payload.
"""
import json
import time

import pytest

from hermes_state import SessionDB
from tools.session_search_tool import SESSION_SEARCH_SCHEMA, session_search


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _seed_sessions(db):
    now = int(time.time())
    db.create_session("s_oldest", source="cli")
    db._conn.execute("UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
                     (now - 30000, "Building the Modpack", "s_oldest"))
    db.append_message("s_oldest", role="user", content="Let's build a Minecraft modpack")
    db.append_message("s_oldest", role="assistant", content="Great. Let me scaffold the modpack repo.")
    db.append_message("s_oldest", role="user", content="Use NeoForge 1.21.1")
    db.append_message("s_oldest", role="assistant", content="Tier-0 mods installed; smoke test passes.")
    db._conn.commit()


class TestSemanticEnhancement:
    def test_semantic_param_declared_in_schema(self):
        params = SESSION_SEARCH_SCHEMA["parameters"]["properties"]
        assert "semantic" in params
        assert params["semantic"]["type"] == "boolean"

    def test_payload_carries_semantic_flag(self, db):
        _seed_sessions(db)
        result = json.loads(session_search(db=db, query="modpack", semantic=True))
        assert result["mode"] == "discover"
        assert result["semantic"] is True

    def test_semantic_false_by_default(self, db):
        _seed_sessions(db)
        result = json.loads(session_search(db=db, query="modpack"))
        assert result["semantic"] is False

    def test_degrades_cleanly_without_embedder(self, db, monkeypatch):
        """Without the embedding engine the tool must not crash; FTS5
        results are returned and no semantic_score fields appear."""
        _seed_sessions(db)

        def _none():
            return None
        monkeypatch.setattr("tools.session_search_tool._get_semantic_index", _none)
        result = json.loads(session_search(db=db, query="modpack", semantic=True))
        assert result["mode"] == "discover"
        assert all("semantic_score" not in r for r in result["results"])
        assert result["semantic"] is True