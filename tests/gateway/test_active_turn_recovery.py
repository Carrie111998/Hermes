"""Regression tests for exact durable active-turn restart recovery.

A long-running gateway turn can outlive the legacy 120-second
``updated_at`` crash heuristic.  These tests require an exact persisted
marker, compare-and-swap cleanup, and promotion into the existing
``resume_pending`` recovery path after an unclean exit.
"""

import hashlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource, SessionStore, build_session_key
from hermes_state import _WEBHOOK_HANDOFF_CLAIM_LOCK_PROTOCOL


ACTIVE_TURN_MAX_AGE_SECONDS = 60 * 60


def _make_source(chat_id: str = "active-turn-chat") -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id=chat_id,
        user_id="user-1",
        chat_type="channel",
        thread_id="thread-1",
    )


def _make_store(tmp_path) -> SessionStore:
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    # Exercise the legacy JSON fallback deterministically.  ``_save_entry``
    # must still persist correctly when state.db is unavailable.
    store._db = None
    return store


def _make_db_store(tmp_path) -> SessionStore:
    from hermes_state import SessionDB

    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir, config=GatewayConfig())
    if store._db is not None:
        store._db.close()
    store._db = SessionDB(db_path=tmp_path / "state.db")
    return store


def _close_store_db(store: SessionStore) -> None:
    db = store._db
    assert db is not None
    db.close()


def _entry_for(store: SessionStore, source: SessionSource) -> SessionEntry:
    key = store._generate_session_key(source)
    with store._lock:
        store._ensure_loaded_locked()
        return store._entries[key]


def _durable_entry_for(store: SessionStore, session_key: str) -> SessionEntry:
    db = store._db
    assert db is not None
    entry_json = db.load_gateway_routing_entries(
        scope=store._routing_scope()
    )[session_key]
    return SessionEntry.from_dict(json.loads(entry_json))


def _bind_running_webhook_delivery(
    store: SessionStore,
    entry: SessionEntry,
    active_turn_token: str,
    delivery_id: str,
) -> tuple[str, str, str]:
    db = store._db
    assert db is not None
    marker = json.dumps(["default", "active-turn", delivery_id], separators=(",", ":"))
    state_key = f"test_webhook_handoff_delivery:{delivery_id}"
    admission_token = hashlib.sha256(
        f"webhook-admission\0{marker}".encode("utf-8")
    ).hexdigest()
    admission_owner = f"test-owner:{delivery_id}"
    accepted_state = json.dumps(
        {
            "marker": marker,
            "phase": "accepted",
            "platform": "discord",
            "session_id": None,
            "source_session_key": entry.session_key,
            "active_turn_token": None,
            "admission_token": admission_token,
            "lock_protocol": _WEBHOOK_HANDOFF_CLAIM_LOCK_PROTOCOL,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    running_state = json.dumps(
        {
            "marker": marker,
            "phase": "running",
            "platform": "discord",
            "session_id": entry.session_id,
            "source_session_key": entry.session_key,
            "active_turn_token": active_turn_token,
            "admission_token": admission_token,
            "lock_protocol": _WEBHOOK_HANDOFF_CLAIM_LOCK_PROTOCOL,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    assert db.set_meta_if_absent(state_key, accepted_state) is True
    assert db.try_acquire_webhook_delivery_admission_lock(
        state_key,
        admission_token,
        _WEBHOOK_HANDOFF_CLAIM_LOCK_PROTOCOL,
        admission_owner,
    ) is True
    assert db.bind_webhook_handoff_delivery_to_source_route(
        entry.session_id,
        entry.session_key,
        state_key,
        accepted_state,
        running_state,
        active_turn_token,
        admission_owner,
    ) is True
    return marker, state_key, running_state


def test_active_turn_fields_round_trip_and_legacy_payload_defaults(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)

    token = store.mark_turn_active(entry.session_key)
    assert token

    payload = _entry_for(store, source).to_dict()
    assert payload["active_turn_token"] == token
    assert payload["active_turn_started_at"] is not None

    restored = SessionEntry.from_dict(payload)
    assert restored.active_turn_token == token
    assert restored.active_turn_started_at is not None

    payload.pop("active_turn_token")
    payload.pop("active_turn_started_at")
    legacy = SessionEntry.from_dict(payload)
    assert legacy.active_turn_token is None
    assert legacy.active_turn_started_at is None

    payload["active_turn_token"] = {"invalid": "not-a-token"}
    payload["active_turn_started_at"] = datetime.now().isoformat()
    corrupt = SessionEntry.from_dict(payload)
    assert corrupt.active_turn_token is None
    assert corrupt.active_turn_started_at is None


def test_mark_refreshes_updated_at_for_legacy_upgrade_fallback(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    old_updated_at = datetime.now() - timedelta(hours=2)
    with store._lock:
        store._entries[entry.session_key].updated_at = old_updated_at

    token = store.mark_turn_active(entry.session_key)

    assert token is not None
    assert _entry_for(store, source).updated_at > old_updated_at


def test_mark_persists_consumed_reset_flags_before_crash_reload(tmp_path):
    store = _make_db_store(tmp_path)
    entry = store.get_or_create_session(_make_source())
    with store._lock:
        current = store._entries[entry.session_key]
        current.was_auto_reset = True
        current.is_fresh_reset = True
        current.auto_reset_reason = "idle"
        assert store._save_entry(
            entry.session_key,
            entry_data=current.to_dict(),
            lock_held=True,
        )
        current.was_auto_reset = False
        current.is_fresh_reset = False
        current.auto_reset_reason = None

    token = store.mark_turn_active(entry.session_key)

    assert token is not None
    durable = _durable_entry_for(store, entry.session_key)
    assert durable.active_turn_token == token
    assert durable.was_auto_reset is False
    assert durable.is_fresh_reset is False
    assert durable.auto_reset_reason is None
    _close_store_db(store)

    restarted = _make_db_store(tmp_path)
    try:
        recovered = restarted.lookup_by_session_key(entry.session_key)
        assert recovered is not None
        assert recovered.active_turn_token == token
        assert recovered.was_auto_reset is False
        assert recovered.is_fresh_reset is False
        assert recovered.auto_reset_reason is None
    finally:
        _close_store_db(restarted)


def test_active_turn_clear_is_compare_and_swap(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)

    first = store.mark_turn_active(entry.session_key)
    second = store.mark_turn_active(entry.session_key)
    assert first is not None
    assert second is not None
    assert first != second

    assert store.clear_turn_active(entry.session_key, first) is False
    assert _entry_for(store, source).active_turn_token == second

    assert store.clear_turn_active(entry.session_key, second) is True
    current = _entry_for(store, source)
    assert current.active_turn_token is None
    assert current.active_turn_started_at is None


def test_mark_and_clear_use_single_entry_persistence(tmp_path):
    store = _make_store(tmp_path)
    entry = store.get_or_create_session(_make_source())
    real_save_entry = store._save_entry
    store._save_entry = MagicMock(wraps=real_save_entry)

    token = store.mark_turn_active(entry.session_key)
    assert token is not None
    store._save_entry.assert_called_once_with(
        entry.session_key,
        entry_data=store._entries[entry.session_key].to_dict(),
        lock_held=True,
    )

    store._save_entry.reset_mock()
    assert store.clear_turn_active(entry.session_key, token) is True
    store._save_entry.assert_called_once_with(
        entry.session_key,
        entry_data=store._entries[entry.session_key].to_dict(),
        lock_held=True,
    )


def test_failed_mark_persistence_does_not_leak_marker_into_later_save(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    real_save_entry = store._save_entry
    store._save_entry = MagicMock(side_effect=OSError("disk unavailable"))

    with pytest.raises(OSError, match="disk unavailable"):
        store.mark_turn_active(entry.session_key)

    current = _entry_for(store, source)
    assert current.active_turn_token is None
    assert current.active_turn_started_at is None

    # A later unrelated save must not make the failed marker durable.
    store._save_entry = real_save_entry
    with store._lock:
        store._entries[entry.session_key].updated_at = datetime.now()
        store._save()

    reloaded = _make_store(tmp_path)
    assert reloaded.recover_interrupted_turns() == 0
    assert _entry_for(reloaded, source).active_turn_token is None


def test_failed_clear_persistence_keeps_token_retryable_and_durable_clear_wins(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    token = store.mark_turn_active(entry.session_key)
    assert token is not None

    real_save_entry = store._save_entry
    store._save_entry = MagicMock(side_effect=OSError("disk unavailable"))

    with pytest.raises(OSError, match="disk unavailable"):
        store.clear_turn_active(entry.session_key, token)

    current = _entry_for(store, source)
    assert current.active_turn_token == token
    assert current.active_turn_started_at is not None

    store._save_entry = real_save_entry
    assert store.clear_turn_active(entry.session_key, token) is True

    reloaded = _make_store(tmp_path)
    persisted = _entry_for(reloaded, source)
    assert persisted.active_turn_token is None
    assert persisted.active_turn_started_at is None


def test_state_db_failure_atomic_marker_round_trip(tmp_path):
    store = _make_db_store(tmp_path)
    source = _make_source("state-db-active-turn")
    entry = store.get_or_create_session(source)
    db = store._db
    assert db is not None
    real_replacer = db.replace_gateway_routing_active_turn_if_owned

    db.replace_gateway_routing_active_turn_if_owned = MagicMock(
        side_effect=OSError("state.db unavailable")
    )
    with pytest.raises(RuntimeError, match="state.db routing transition failed"):
        store.mark_turn_active(entry.session_key)
    assert _entry_for(store, source).active_turn_token is None

    db.replace_gateway_routing_active_turn_if_owned = real_replacer
    token = store.mark_turn_active(entry.session_key)
    assert token is not None

    db.replace_gateway_routing_active_turn_if_owned = MagicMock(
        side_effect=OSError("state.db unavailable")
    )
    with pytest.raises(RuntimeError, match="state.db routing transition failed"):
        store.clear_turn_active(entry.session_key, token)
    assert _entry_for(store, source).active_turn_token == token

    db.replace_gateway_routing_active_turn_if_owned = real_replacer
    assert store.clear_turn_active(entry.session_key, token) is True
    _close_store_db(store)

    reloaded = _make_db_store(tmp_path)
    assert reloaded.recover_interrupted_turns() == 0
    persisted = _entry_for(reloaded, source)
    assert persisted.active_turn_token is None
    assert persisted.active_turn_started_at is None
    _close_store_db(reloaded)


def test_state_db_commit_survives_legacy_mirror_failure(tmp_path):
    store = _make_db_store(tmp_path)
    source = _make_source("state-db-mirror-failure")
    entry = store.get_or_create_session(source)
    db = store._db
    assert db is not None
    db.save_gateway_routing_entry = MagicMock(
        side_effect=OSError("fast upsert unavailable")
    )
    store._save_sessions_json = MagicMock(
        side_effect=OSError("legacy mirror unavailable")
    )

    token = store.mark_turn_active(entry.session_key)
    assert token is not None
    _close_store_db(store)

    reloaded = _make_db_store(tmp_path)
    recovered = _entry_for(reloaded, source)
    assert recovered.active_turn_token == token
    _close_store_db(reloaded)


def test_bound_webhook_active_turn_survives_stale_fast_and_full_writers(tmp_path):
    live = _make_db_store(tmp_path)
    source = _make_source("bound-stale-writers")
    entry = live.get_or_create_session(source)

    # Both aliases load the same session before its active owner is published.
    stale_fast = _make_db_store(tmp_path)
    stale_full = _make_db_store(tmp_path)
    assert stale_fast.peek_session_id(entry.session_key) == entry.session_id
    assert stale_full.peek_session_id(entry.session_key) == entry.session_id

    token = live.mark_turn_active(entry.session_key)
    assert token is not None
    _bind_running_webhook_delivery(live, entry, token, "stale-writers")
    assert live.mark_resume_pending(entry.session_key, "restart_timeout") is True

    stale_fast.update_session(entry.session_key, last_prompt_tokens=77)
    assert stale_full.set_session_metadata(
        entry.session_key, "stale_full_writer", True
    ) is True

    durable = _durable_entry_for(live, entry.session_key)
    assert durable.session_id == entry.session_id
    assert durable.active_turn_token == token
    assert durable.active_turn_started_at is not None
    assert durable.resume_pending is True
    assert durable.resume_reason == "restart_timeout"
    assert durable.last_resume_marked_at is not None

    _close_store_db(stale_full)
    _close_store_db(stale_fast)
    _close_store_db(live)


def test_running_webhook_recovers_t1_rotates_t2_and_publishes_success(tmp_path):
    original = _make_db_store(tmp_path)
    source = _make_source("running-restart-cas")
    entry = original.get_or_create_session(source)
    t1 = original.mark_turn_active(entry.session_key)
    assert t1 is not None
    marker, state_key, running_state = _bind_running_webhook_delivery(
        original, entry, t1, "restart-cas"
    )

    # These stores represent other processes that loaded the still-running t1.
    stale_fast = _make_db_store(tmp_path)
    stale_full = _make_db_store(tmp_path)
    assert _entry_for(stale_fast, source).active_turn_token == t1
    assert _entry_for(stale_full, source).active_turn_token == t1
    _close_store_db(original)

    restarted = _make_db_store(tmp_path)
    assert restarted.recover_interrupted_turns() == 1
    recovered = _entry_for(restarted, source)
    assert recovered.active_turn_token is None
    assert recovered.resume_pending is True
    assert recovered.resume_reason == "restart_interrupted"

    # A t1 snapshot may neither erase the recovery proof nor resurrect t1.
    stale_fast.update_session(entry.session_key, last_prompt_tokens=88)
    assert stale_full.set_session_metadata(
        entry.session_key, "stale_after_recovery", True
    ) is True
    durable_recovered = _durable_entry_for(restarted, entry.session_key)
    assert durable_recovered.active_turn_token is None
    assert durable_recovered.resume_pending is True
    assert durable_recovered.resume_reason == "restart_interrupted"

    t2 = restarted.mark_turn_active(entry.session_key)
    assert t2 is not None
    assert t2 != t1
    assert _durable_entry_for(restarted, entry.session_key).active_turn_token == t2

    succeeded_state = json.dumps(
        {
            "marker": marker,
            "phase": "succeeded",
            "platform": "discord",
            "session_id": entry.session_id,
            "source_session_key": entry.session_key,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    db = restarted._db
    assert db is not None
    assert db.complete_webhook_handoff_delivery_once(
        entry.session_id,
        entry.session_key,
        state_key,
        running_state,
        succeeded_state,
        "discord",
        t2,
        entry.session_id,
    ) is True

    published = _durable_entry_for(restarted, entry.session_key)
    assert published.active_turn_token is None
    assert published.active_turn_started_at is None
    assert published.resume_pending is False
    assert published.resume_reason is None
    assert published.last_resume_marked_at is None
    assert db.get_meta(state_key) == succeeded_state
    assert db.get_handoff_state(entry.session_id) == {
        "state": "pending",
        "platform": "discord",
        "error": None,
    }
    assert db.is_webhook_handoff_request(entry.session_id, "discord") is True

    _close_store_db(stale_full)
    _close_store_db(stale_fast)
    _close_store_db(restarted)


def test_post_success_stale_writer_cannot_resurrect_active_turn(tmp_path):
    live = _make_db_store(tmp_path)
    source = _make_source("post-success-stale-writer")
    entry = live.get_or_create_session(source)
    token = live.mark_turn_active(entry.session_key)
    assert token is not None
    marker, state_key, running_state = _bind_running_webhook_delivery(
        live, entry, token, "post-success"
    )
    assert live.mark_resume_pending(entry.session_key, "restart_timeout") is True

    stale_fast = _make_db_store(tmp_path)
    stale_full = _make_db_store(tmp_path)
    stale_entry = _entry_for(stale_fast, source)
    assert stale_entry.active_turn_token == token
    assert stale_entry.resume_pending is True
    assert _entry_for(stale_full, source).active_turn_token == token

    succeeded_state = json.dumps(
        {
            "marker": marker,
            "phase": "succeeded",
            "platform": "discord",
            "session_id": entry.session_id,
            "source_session_key": entry.session_key,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    db = live._db
    assert db is not None
    assert db.complete_webhook_handoff_delivery_once(
        entry.session_id,
        entry.session_key,
        state_key,
        running_state,
        succeeded_state,
        "discord",
        token,
        entry.session_id,
    ) is True

    stale_fast.update_session(entry.session_key, last_prompt_tokens=99)
    assert stale_full.set_session_metadata(
        entry.session_key, "stale_after_success", True
    ) is True

    durable = _durable_entry_for(live, entry.session_key)
    assert durable.active_turn_token is None
    assert durable.active_turn_started_at is None
    assert durable.resume_pending is False
    assert durable.resume_reason is None
    assert durable.last_resume_marked_at is None
    assert db.get_meta(state_key) == succeeded_state
    assert db.is_webhook_handoff_request(entry.session_id, "discord") is True

    # The watcher consumes the same live SessionStore, whose source entry still
    # carried ``token`` when the DB-only success transaction cleared it.  Its
    # targeted move must publish the authoritative cleared fields at both the
    # destination row and the live destination entry.
    claim_token = "post-success-structural-move-owner"
    owner = {
        "token": claim_token,
        "pid": 12345,
        "process_start_time": 67890,
        "host": "test-host",
        "instantiation_epoch": "test-epoch",
        "routing_scope": live._routing_scope(),
        "source_session_key": entry.session_key,
        "active_session_key": entry.session_key,
    }
    assert db.claim_webhook_handoff(
        entry.session_id,
        json.dumps(owner),
    ) is True
    destination_source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="post-success-thread",
        user_id="system:handoff",
        user_name="Handoff",
        chat_type="thread",
        thread_id="post-success-thread",
    )
    destination_key = build_session_key(destination_source)
    moved = live.move_session_route(
        entry.session_key,
        destination_key,
        entry.session_id,
        destination_source,
        handoff_claim_token=claim_token,
    )
    assert moved is not None
    assert moved.active_turn_token is None
    assert moved.active_turn_started_at is None
    assert moved.resume_pending is False
    assert moved.resume_reason is None
    assert moved.last_resume_marked_at is None
    assert live.peek_session_id(entry.session_key) is None
    live_destination = live.lookup_by_session_id(entry.session_id)
    assert live_destination is not None
    assert live_destination.session_key == destination_key
    assert live_destination.active_turn_token is None
    moved_durable = _durable_entry_for(live, destination_key)
    assert moved_durable.active_turn_token is None
    assert moved_durable.active_turn_started_at is None
    assert moved_durable.resume_pending is False
    assert moved_durable.resume_reason is None
    assert moved_durable.last_resume_marked_at is None

    _close_store_db(stale_full)
    _close_store_db(stale_fast)
    _close_store_db(live)


def test_clean_discard_raises_when_durable_active_owner_changed(tmp_path):
    live = _make_db_store(tmp_path)
    source = _make_source("clean-discard-cas-rejection")
    entry = live.get_or_create_session(source)
    t1 = live.mark_turn_active(entry.session_key)
    assert t1 is not None

    stale = _make_db_store(tmp_path)
    assert _entry_for(stale, source).active_turn_token == t1

    t2 = live.mark_turn_active(entry.session_key)
    assert t2 is not None
    assert t2 != t1
    with pytest.raises(RuntimeError, match="active-turn marker changed"):
        stale.discard_active_turn_markers()

    assert _entry_for(stale, source).active_turn_token == t1
    assert _durable_entry_for(live, entry.session_key).active_turn_token == t2

    _close_store_db(stale)
    _close_store_db(live)


def test_exact_old_active_turn_recovers_even_when_updated_at_is_stale(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    token = store.mark_turn_active(entry.session_key)

    with store._lock:
        current = store._entries[entry.session_key]
        current.updated_at = datetime.now() - timedelta(hours=2)
        current.active_turn_started_at = datetime.now() - timedelta(minutes=10)
        store._save()

    # Prove the marker survives a fresh SessionStore and is not relying on the
    # in-memory object that wrote it.
    reloaded = _make_store(tmp_path)
    assert reloaded.recover_interrupted_turns(
        max_age_seconds=ACTIVE_TURN_MAX_AGE_SECONDS
    ) == 1

    recovered = _entry_for(reloaded, source)
    assert recovered.resume_pending is True
    assert recovered.resume_reason == "restart_interrupted"
    assert recovered.last_resume_marked_at is not None
    assert recovered.last_resume_marked_at > datetime.now() - timedelta(seconds=5)
    assert recovered.active_turn_token is None
    assert recovered.active_turn_started_at is None
    assert token


def test_suspended_active_turn_is_cleared_without_resume(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    store.mark_turn_active(entry.session_key)

    with store._lock:
        store._entries[entry.session_key].suspended = True

    assert store.recover_interrupted_turns() == 0
    recovered = _entry_for(store, source)
    assert recovered.suspended is True
    assert recovered.resume_pending is False
    assert recovered.active_turn_token is None
    assert recovered.active_turn_started_at is None


def test_existing_resume_reason_and_freshness_are_preserved(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    store.mark_turn_active(entry.session_key)
    original_mark = datetime.now() - timedelta(minutes=2)

    with store._lock:
        current = store._entries[entry.session_key]
        current.resume_pending = True
        current.resume_reason = "shutdown_timeout"
        current.last_resume_marked_at = original_mark

    assert store.recover_interrupted_turns() == 0
    recovered = _entry_for(store, source)
    assert recovered.resume_pending is True
    assert recovered.resume_reason == "shutdown_timeout"
    assert recovered.last_resume_marked_at == original_mark
    assert recovered.active_turn_token is None
    assert recovered.active_turn_started_at is None


def test_ancient_active_marker_is_cleared_without_auto_resume(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    store.mark_turn_active(entry.session_key)

    with store._lock:
        current = store._entries[entry.session_key]
        current.active_turn_started_at = datetime.now() - timedelta(hours=2)

    assert store.recover_interrupted_turns(
        max_age_seconds=ACTIVE_TURN_MAX_AGE_SECONDS
    ) == 0
    recovered = _entry_for(store, source)
    assert recovered.resume_pending is False
    assert recovered.active_turn_token is None
    assert recovered.active_turn_started_at is None


def test_clean_startup_discards_orphan_markers_without_resuming(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    store.mark_turn_active(entry.session_key)

    assert store.discard_active_turn_markers() == 1

    recovered = _entry_for(store, source)
    assert recovered.resume_pending is False
    assert recovered.active_turn_token is None
    assert recovered.active_turn_started_at is None


@pytest.mark.asyncio
async def test_clean_shutdown_marker_is_not_consumed_when_discard_fails(tmp_path):
    marker = tmp_path / ".clean_shutdown"
    marker.write_text("clean", encoding="utf-8")
    runner = object.__new__(GatewayRunner)
    async_store = MagicMock()
    async_store.discard_active_turn_markers = AsyncMock(
        side_effect=OSError("state store unavailable")
    )

    with patch.object(
        GatewayRunner,
        "async_session_store",
        new_callable=PropertyMock,
        return_value=async_store,
    ):
        with pytest.raises(OSError, match="state store unavailable"):
            await runner._consume_clean_shutdown_marker(marker)

    assert marker.exists()


@pytest.mark.asyncio
async def test_clean_shutdown_marker_is_unlinked_after_durable_discard(tmp_path):
    marker = tmp_path / ".clean_shutdown"
    marker.write_text("clean", encoding="utf-8")
    runner = object.__new__(GatewayRunner)
    async_store = MagicMock()
    async_store.discard_active_turn_markers = AsyncMock(return_value=2)

    with patch.object(
        GatewayRunner,
        "async_session_store",
        new_callable=PropertyMock,
        return_value=async_store,
    ):
        discarded = await runner._consume_clean_shutdown_marker(marker)

    assert discarded == 2
    assert not marker.exists()


@pytest.mark.asyncio
async def test_runner_active_turn_carrier_clears_the_exact_resolved_key():
    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    mark_active = AsyncMock(return_value="token-1")
    clear_active = AsyncMock(return_value=True)
    setattr(
        runner,
        "_async_session_store",
        SimpleNamespace(
            _store=runner.session_store,
            mark_turn_active=mark_active,
            clear_turn_active=clear_active,
        ),
    )
    event = SimpleNamespace()

    await runner._mark_durable_active_turn(
        cast(Any, event), "resolved-session-key"
    )

    assert event._gateway_active_turn_session_key == "resolved-session-key"
    assert event._gateway_active_turn_token == "token-1"

    await runner._clear_durable_active_turn(cast(Any, event))

    clear_active.assert_awaited_once_with("resolved-session-key", "token-1")
    assert not hasattr(event, "_gateway_active_turn_session_key")
    assert not hasattr(event, "_gateway_active_turn_token")


@pytest.mark.asyncio
async def test_runner_active_turn_carrier_marks_failed_admission():
    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    mark_active = AsyncMock(return_value=None)
    setattr(
        runner,
        "_async_session_store",
        SimpleNamespace(
            _store=runner.session_store,
            mark_turn_active=mark_active,
        ),
    )
    event = MessageEvent(
        text="alert",
        source=SessionSource(
            platform=Platform.WEBHOOK,
            chat_id="webhook:alerts:delivery-1",
        ),
    )

    admitted = await runner._mark_durable_active_turn(
        event, "resolved-session-key"
    )

    assert admitted is False
    assert event.active_turn_admission_failed is True


@pytest.mark.asyncio
async def test_runner_active_turn_clear_is_best_effort():
    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    clear_active = AsyncMock(
        side_effect=[OSError("disk unavailable"), True]
    )
    setattr(
        runner,
        "_async_session_store",
        SimpleNamespace(
            _store=runner.session_store,
            clear_turn_active=clear_active,
        ),
    )
    event = SimpleNamespace(
        _gateway_active_turn_session_key="resolved-session-key",
        _gateway_active_turn_token="token-1",
    )

    await runner._clear_durable_active_turn(cast(Any, event))

    assert clear_active.await_count == 2
    assert not hasattr(event, "_gateway_active_turn_session_key")
    assert not hasattr(event, "_gateway_active_turn_token")


@pytest.mark.asyncio
async def test_runner_active_turn_clear_stops_after_bounded_retries():
    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    clear_active = AsyncMock(side_effect=OSError("disk unavailable"))
    setattr(
        runner,
        "_async_session_store",
        SimpleNamespace(
            _store=runner.session_store,
            clear_turn_active=clear_active,
        ),
    )
    event = SimpleNamespace(
        _gateway_active_turn_session_key="resolved-session-key",
        _gateway_active_turn_token="token-1",
    )

    assert await runner._clear_durable_active_turn(cast(Any, event)) is False

    assert clear_active.await_count == 3
    assert not hasattr(event, "_gateway_active_turn_session_key")
    assert not hasattr(event, "_gateway_active_turn_token")


@pytest.mark.asyncio
async def test_unclean_recovery_promotes_exact_markers_before_legacy_fallback(
    monkeypatch,
):
    runner = object.__new__(GatewayRunner)
    calls: list[str] = []

    monkeypatch.delenv("HERMES_AGENT_TIMEOUT", raising=False)

    async def _recover(*, max_age_seconds):
        assert max_age_seconds == ACTIVE_TURN_MAX_AGE_SECONDS
        calls.append("exact")
        return 1

    async def _fallback(*, max_age_seconds):
        assert max_age_seconds == 120
        calls.append("fallback")
        return 2

    runner.session_store = MagicMock()
    setattr(
        runner,
        "_async_session_store",
        SimpleNamespace(
            _store=runner.session_store,
            recover_interrupted_turns=_recover,
            suspend_recently_active=_fallback,
        ),
    )

    assert await runner._recover_unclean_sessions() == (1, 2)
    assert calls == ["exact", "fallback"]


def test_mark_and_clear_turn_active_use_fast_save_bookkeeping(tmp_path):
    """Per-turn marker transitions must not act as full-index writers.

    Advancing ``_persisted_routing_generation`` after a single-row CAS would
    suppress a concurrent in-flight full snapshot of *other* keys, and the
    sessions.json mirror rewrite (twice per turn) is exactly the cost the
    ``_save_entry`` fast path exists to avoid.  The committed row rides the
    fast-persisted overlay instead, so a delayed full writer folds it in.
    """
    store = _make_db_store(tmp_path)
    try:
        source = _make_source()
        entry = store.get_or_create_session(source)
        key = entry.session_key

        sessions_file = store.sessions_dir / "sessions.json"
        mirror_before = (
            sessions_file.read_text() if sessions_file.exists() else None
        )
        persisted_before = getattr(store, "_persisted_routing_generation", 0)

        token = store.mark_turn_active(key)
        assert token

        assert (
            getattr(store, "_persisted_routing_generation", 0)
            == persisted_before
        )
        fast = getattr(store, "_fast_persisted_entries", {})
        assert key in fast
        revision, entry_json = fast[key]
        assert revision > persisted_before
        assert json.loads(entry_json)["active_turn_token"] == token

        # The owner CAS is still the durable commit point.
        durable = store._db.load_gateway_routing_entries(
            scope=store._routing_scope()
        )[key]
        assert json.loads(durable)["active_turn_token"] == token

        mirror_after = (
            sessions_file.read_text() if sessions_file.exists() else None
        )
        assert mirror_after == mirror_before

        assert store.clear_turn_active(key, token) is True
        assert (
            getattr(store, "_persisted_routing_generation", 0)
            == persisted_before
        )
        durable = store._db.load_gateway_routing_entries(
            scope=store._routing_scope()
        )[key]
        assert json.loads(durable)["active_turn_token"] is None
    finally:
        _close_store_db(store)
