"""Tests for SessionStore._prune_stale_sessions_locked — crash self-healing.

When a gateway crashes (exit code 1) the graceful shutdown path is skipped and
sessions.json is left pointing at sessions already ended in state.db. On the
next startup _ensure_loaded_locked calls _prune_stale_sessions_locked to detect
and remove those stale routing entries before get_or_create_session() can reuse
them and silently route incoming messages into a closed session (#52804).
"""

import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from gateway.config import GatewayConfig, Platform, SessionResetPolicy
from gateway.session import SessionEntry, SessionSource, SessionStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entry(key: str, session_id: str) -> SessionEntry:
    now = datetime.now()
    return SessionEntry(
        session_key=key,
        session_id=session_id,
        created_at=now - timedelta(hours=2),
        updated_at=now - timedelta(hours=1),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )


def _make_entry_with_origin(key: str, session_id: str) -> SessionEntry:
    entry = _make_entry(key, session_id)
    entry.origin = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="5140768830",
        chat_type="dm",
        user_id="5140768830",
        user_name="João",
    )
    return entry


def _make_store_with_db(tmp_path, db_mock) -> SessionStore:
    """Build a SessionStore with a mock SessionDB, bypassing disk load."""
    config = GatewayConfig(default_reset_policy=SessionResetPolicy(mode="none"))
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=tmp_path, config=config)
    store._db = db_mock
    store._loaded = True
    return store


def _db_returning(rows: dict) -> MagicMock:
    """SessionDB mock where get_session maps session_id -> row dict."""
    db = MagicMock()
    db.get_session.side_effect = lambda sid: rows.get(sid)
    db.load_gateway_routing_entries.return_value = {}
    db.insert_gateway_routing_entry_if_absent.side_effect = (
        lambda _key, entry_json, *, scope="": entry_json
    )
    return db


# ---------------------------------------------------------------------------
# Core behaviour
# ---------------------------------------------------------------------------

class TestPruneStaleSessionsLocked:


    def test_prunes_multiple_stale_entries(self, tmp_path):
        db = _db_returning({
            "sid_a": {"end_reason": "agent_close", "id": "sid_a"},
            "sid_b": {"end_reason": "session_reset", "id": "sid_b"},
            "sid_c": {"end_reason": None, "id": "sid_c"},  # alive — keep
        })
        store = _make_store_with_db(tmp_path, db)
        store._entries["key_a"] = _make_entry("key_a", "sid_a")
        store._entries["key_b"] = _make_entry("key_b", "sid_b")
        store._entries["key_c"] = _make_entry("key_c", "sid_c")

        store._prune_stale_sessions_locked()

        assert "key_a" not in store._entries
        assert "key_b" not in store._entries
        assert "key_c" in store._entries


    def test_keeps_stale_entry_when_recovery_lookup_raises(self, tmp_path):
        """Indeterminate recovery must not delete the only routing handle.

        Startup pruning sees an ended parent and tries to repoint it to the
        latest live gateway child.  If that recovery query raises, deleting the
        sessions.json entry loses the routing key entirely; keeping it lets the
        runtime stale guard retry recovery on the next message.
        """
        key = "agent:main:telegram:dm:5140768830"
        db = _db_returning({"sid_parent": {"end_reason": "compression", "id": "sid_parent"}})
        db.find_latest_gateway_session_for_peer.side_effect = RuntimeError("db busy")
        store = _make_store_with_db(tmp_path, db)
        store._entries[key] = _make_entry_with_origin(key, "sid_parent")

        store._prune_stale_sessions_locked()

        assert key in store._entries
        assert store._entries[key].session_id == "sid_parent"

    def test_noop_when_db_is_none(self, tmp_path):
        config = GatewayConfig(default_reset_policy=SessionResetPolicy(mode="none"))
        with patch("gateway.session.SessionStore._ensure_loaded"):
            store = SessionStore(sessions_dir=tmp_path, config=config)
        store._db = None
        store._loaded = True
        store._entries["key"] = _make_entry("key", "sid_x")

        store._prune_stale_sessions_locked()  # must not raise

        assert "key" in store._entries


    def test_sessions_json_rewritten_after_pruning(self, tmp_path):
        db = _db_returning({"sid_stale": {"end_reason": "agent_close", "id": "sid_stale"}})
        store = _make_store_with_db(tmp_path, db)
        store._entries["stale_key"] = _make_entry("stale_key", "sid_stale")

        with patch.object(store, "_save") as mock_save:
            store._prune_stale_sessions_locked()
            mock_save.assert_called_once()


# ---------------------------------------------------------------------------
# Integration: _ensure_loaded_locked calls _prune_stale_sessions_locked
# ---------------------------------------------------------------------------

class TestEnsureLoadedCallsPrune:
    def test_stale_entry_pruned_during_load(self, tmp_path):
        entry = _make_entry("dm_key", "sid_stale")
        (tmp_path / "sessions.json").write_text(
            json.dumps({"dm_key": entry.to_dict()}, indent=2), encoding="utf-8"
        )
        db = _db_returning({"sid_stale": {"end_reason": "agent_close", "id": "sid_stale"}})
        config = GatewayConfig(default_reset_policy=SessionResetPolicy(mode="none"))
        store = SessionStore(sessions_dir=tmp_path, config=config)
        store._db = db

        store._ensure_loaded()

        assert "dm_key" not in store._entries


class TestPruneSaveConcurrencySafety:
    """A prune-triggered whole-index save must not clobber a sibling writer's
    concurrent insert into the same routing scope (#9006 concurrency follow-up).
    """

    def test_prune_save_preserves_concurrent_foreign_row(self, tmp_path, monkeypatch):
        import hermes_state

        monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
        scope = str(tmp_path.resolve())

        live_key = "agent:main:telegram:dm:live"
        stale_key = "agent:main:telegram:dm:stale"
        foreign_key = "agent:main:telegram:dm:foreign"

        # Build the owned entries once; the DB is seeded from exactly these
        # bytes and the store's post-load baseline mirrors them, the way a real
        # _ensure_loaded captures both _entries and the raw CAS operand from the
        # same stored payload.
        live_entry = _make_entry(live_key, "sid_live")
        stale_entry = _make_entry(stale_key, "sid_stale")
        live_json = json.dumps(live_entry.to_dict())
        stale_json = json.dumps(stale_entry.to_dict())

        db = hermes_state.SessionDB()
        db.save_gateway_routing_entry(live_key, live_json, scope=scope)
        db.save_gateway_routing_entry(stale_key, stale_json, scope=scope)
        # sid_stale reads as ended so startup pruning removes it; every other
        # session_id is absent from the sessions table (get_session -> None,
        # kept).
        real_get = db.get_session
        db.get_session = lambda sid: (
            {"end_reason": "agent_close", "id": sid} if sid == "sid_stale" else real_get(sid)
        )

        store = _make_store_with_db(tmp_path, db)
        # Model the post-load snapshot: the store read and now owns both rows
        # at exactly these payloads (the persistence baseline).
        store._entries = {live_key: live_entry, stale_key: stale_entry}
        store._persisted_routing_payloads = {
            live_key: store._routing_payload_signature(live_entry.to_dict()),
            stale_key: store._routing_payload_signature(stale_entry.to_dict()),
        }
        store._persisted_routing_raw = {
            live_key: live_json,
            stale_key: stale_json,
        }

        # A sibling connection inserts a DIFFERENT row after the load snapshot.
        competitor = hermes_state.SessionDB()
        competitor.save_gateway_routing_entry(
            foreign_key,
            json.dumps(_make_entry(foreign_key, "sid_foreign").to_dict()),
            scope=scope,
        )

        # Pruning the stale key triggers the whole-index save immediately.
        store._prune_stale_sessions_locked()

        rows = db.load_gateway_routing_entries(scope=scope)
        assert set(rows) == {live_key, foreign_key}
        assert stale_key not in rows  # owned + removed -> deleted
        assert json.loads(rows[foreign_key])["session_id"] == "sid_foreign"
        competitor.close()
        db.close()

    def test_prune_save_preserves_already_loaded_sibling_update(
        self, tmp_path, monkeypatch
    ):
        """A row that was part of the load snapshot must not be re-upserted
        unchanged by a prune-triggered save: a sibling's concurrent update to
        that loaded row survives, while the pruned owned key is still deleted.
        """
        import hermes_state

        monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
        scope = str(tmp_path.resolve())

        stale_key = "agent:main:telegram:dm:stale"
        sibling_key = "agent:main:telegram:dm:sibling"

        # Build the owned entries once so the DB bytes and the store's raw CAS
        # baseline are byte-identical, the way a real load captures them.
        stale_entry = _make_entry(stale_key, "sid_stale")
        sibling_entry = _make_entry(sibling_key, "sid_sibling")
        stale_json = json.dumps(stale_entry.to_dict())
        sibling_json = json.dumps(sibling_entry.to_dict())

        db = hermes_state.SessionDB()
        db.save_gateway_routing_entry(stale_key, stale_json, scope=scope)
        db.save_gateway_routing_entry(sibling_key, sibling_json, scope=scope)
        real_get = db.get_session
        db.get_session = lambda sid: (
            {"end_reason": "agent_close", "id": sid}
            if sid == "sid_stale"
            else real_get(sid)
        )

        store = _make_store_with_db(tmp_path, db)
        # Model the post-load snapshot: BOTH rows were read and are owned at
        # exactly these payloads (baseline captured from the same objects).
        store._entries = {stale_key: stale_entry, sibling_key: sibling_entry}
        store._persisted_routing_payloads = {
            stale_key: store._routing_payload_signature(stale_entry.to_dict()),
            sibling_key: store._routing_payload_signature(sibling_entry.to_dict()),
        }
        store._persisted_routing_raw = {
            stale_key: stale_json,
            sibling_key: sibling_json,
        }

        # A sibling connection UPDATES the loaded sibling row after our snapshot.
        competitor = hermes_state.SessionDB()
        competitor.save_gateway_routing_entry(
            sibling_key,
            json.dumps(_make_entry(sibling_key, "sid_sibling2").to_dict()),
            scope=scope,
        )

        # Pruning the stale key triggers the whole-index save immediately.
        store._prune_stale_sessions_locked()

        rows = db.load_gateway_routing_entries(scope=scope)
        assert stale_key not in rows  # owned + removed -> deleted
        # The loaded sibling row was unchanged locally, so it must not have been
        # re-upserted over the sibling writer's newer value.
        assert json.loads(rows[sibling_key])["session_id"] == "sid_sibling2"
        competitor.close()
        db.close()

    def test_prune_save_preserves_concurrent_same_key_update(
        self, tmp_path, monkeypatch
    ):
        """Startup stale-prune of an owned key must CAS-guard its delete: if a
        sibling rewrote that SAME key after our load snapshot, the
        prune-triggered save must not erase the sibling's newer route. Our
        in-memory copy still points at the ended session, so the key still prunes
        locally — but the delete no longer matches what the table now holds.
        """
        import hermes_state

        monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
        scope = str(tmp_path.resolve())
        stale_key = "agent:main:telegram:dm:stale"

        stale_entry = _make_entry(stale_key, "sid_stale")
        stale_json = json.dumps(stale_entry.to_dict())

        db = hermes_state.SessionDB()
        db.save_gateway_routing_entry(stale_key, stale_json, scope=scope)
        real_get = db.get_session
        db.get_session = lambda sid: (
            {"end_reason": "agent_close", "id": sid}
            if sid == "sid_stale"
            else real_get(sid)
        )

        store = _make_store_with_db(tmp_path, db)
        store._entries = {stale_key: stale_entry}
        store._persisted_routing_payloads = {
            stale_key: store._routing_payload_signature(stale_entry.to_dict()),
        }
        store._persisted_routing_raw = {stale_key: stale_json}

        # A sibling rewrites the SAME key AFTER our snapshot with a live session.
        competitor = hermes_state.SessionDB()
        competitor.save_gateway_routing_entry(
            stale_key,
            json.dumps(_make_entry(stale_key, "sid_stale_live").to_dict()),
            scope=scope,
        )

        store._prune_stale_sessions_locked()

        rows = db.load_gateway_routing_entries(scope=scope)
        # Payload no longer ours -> stale delete is a no-op, sibling route lives.
        assert json.loads(rows[stale_key])["session_id"] == "sid_stale_live"
        competitor.close()
        db.close()

