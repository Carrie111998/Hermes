from datetime import datetime
from pathlib import Path

import pytest

from gateway.platforms.api_server import ResponseStore
from gateway.session import GatewayConfig, SessionEntry, SessionStore

# hermes_state is imported lazily inside tests: a module-level import would
# load it at collection time, before the hermetic conftest fixture can re-pin
# DEFAULT_DB_PATH, breaking runtime-HERMES_HOME regression tests that run
# later in the same process (see tests/conftest.py step 3b).


def _session_db(db_path):
    from hermes_state import SessionDB

    return SessionDB(db_path=db_path)


def test_response_store_delete_for_sessions_removes_only_matching_rows(tmp_path):
    store = ResponseStore(db_path=str(tmp_path / "responses.db"))
    store.put("r1", {"session_id": "S1", "conversation_history": ["secret"]})
    store.put("r2", {"session_id": "S2", "conversation_history": ["keep"]})
    store.set_conversation("one", "r1")
    store.set_conversation("two", "r2")

    assert store.delete_for_sessions(["S1"]) == 1
    assert store.get("r1") is None
    assert store.get_conversation("one") is None
    assert store.get("r2") is not None
    assert store.get_conversation("two") == "r2"


def test_response_store_privacy_mode_refuses_memory_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr("sqlite3.connect", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("denied")))
    with pytest.raises(OSError, match="denied"):
        ResponseStore(db_path=str(tmp_path / "responses.db"), require_durable=True)


def test_session_store_remove_route_if_session_matches_is_compare_and_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    sessions_dir = tmp_path / "sessions"
    config = GatewayConfig()
    store = SessionStore(sessions_dir, config)
    store._loaded = True
    now = datetime.now()
    store._entries["route"] = SessionEntry(
        session_key="route", session_id="S1", created_at=now, updated_at=now
    )

    assert store.get_entry("route").session_id == "S1"
    assert store.get_entry("missing") is None
    assert not store.remove_route_if_session_matches("route", "OTHER")
    assert store._entries["route"].session_id == "S1"
    assert store.remove_route_if_session_matches("route", "S1")
    assert "route" not in store._entries


def test_route_delete_rolls_back_live_map_when_persistence_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    store._loaded = True
    now = datetime.now()
    store._entries["route"] = SessionEntry(
        session_key="route", session_id="S1", created_at=now, updated_at=now
    )

    def fail_save(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_persist_routing_data", fail_save)
    try:
        store.remove_route_if_session_matches("route", "S1")
    except OSError:
        pass
    else:
        raise AssertionError("expected persistence failure")

    assert store._entries["route"].session_id == "S1"


def test_delete_history_lineage_removes_branch_compression_and_delegate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    db = _session_db(tmp_path / "state.db")
    db.create_session("root", source="slack")
    db.end_session("root", "compression")
    db.create_session("compressed", source="slack", parent_session_id="root")
    db.create_session(
        "branch", source="slack", parent_session_id="root",
        model_config={"_branched_from": "root"},
    )
    db.create_session(
        "delegate", source="tool", parent_session_id="root",
        model_config={"_delegate_from": "root"},
    )
    db.create_session("unrelated", source="slack")

    targets = db.get_history_delete_targets("root")
    assert set(targets) == {"root", "compressed", "branch", "delegate"}
    assert db.delete_history_lineage("root", sessions_dir=tmp_path / "sessions") == (4, [])
    assert db.get_session("unrelated") is not None
    for sid in targets:
        assert db.get_session(sid) is None


def test_history_targets_exclude_plain_reset_but_include_nested_history_children(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    db = _session_db(tmp_path / "state.db")
    db.create_session("root", source="slack")
    db.create_session("reset", source="slack", parent_session_id="root")
    db.create_session(
        "branch", source="slack", parent_session_id="root",
        model_config={"_branched_from": "root"},
    )
    db.create_session(
        "delegate", source="tool", parent_session_id="branch",
        model_config={"_delegate_from": "branch"},
    )

    assert set(db.get_history_delete_targets("root")) == {
        "root", "branch", "delegate"
    }


def test_lineage_delete_reports_transcript_unlink_failure_and_keeps_rows(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    db = _session_db(tmp_path / "state.db")
    db.create_session("root", source="slack")
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    transcript = sessions_dir / "root.jsonl"
    transcript.write_text("secret")
    original_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if self == transcript:
            raise OSError("denied")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    count, failures = db.delete_history_lineage("root", sessions_dir=sessions_dir)

    assert count == 0
    assert db.get_session("root") is not None
    assert failures == ["session:root:transcript_unlink_failed"]


def test_retry_with_dead_route_session_converges(tmp_path, monkeypatch):
    """Route entry pointing at an already-deleted session id: empty targets,
    no failure, and the stale route is still removed — so a second !delete
    after a partial failure ends in silent success."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    db = _session_db(tmp_path / "state.db")
    db.create_session("root", source="slack")
    assert db.delete_history_lineage("root", sessions_dir=tmp_path / "sessions") == (1, [])

    # Retry against the now-missing session id converges without failures.
    assert db.get_history_delete_targets("root") == []
    assert db.delete_history_lineage("root", sessions_dir=tmp_path / "sessions") == (0, [])

    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    store._loaded = True
    now = datetime.now()
    store._entries["route"] = SessionEntry(
        session_key="route", session_id="root", created_at=now, updated_at=now
    )
    assert store.remove_route_if_session_matches("route", "root")
    assert "route" not in store._entries
