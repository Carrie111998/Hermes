"""Tests for session auto-reset notifications.

Verifies that:
- _should_reset() returns a reason string ("idle" or "daily") instead of bool
- SessionEntry captures auto_reset_reason
- SessionResetPolicy.notify controls whether notifications are sent
- notify_exclude_platforms skips notifications for excluded platforms
- resume_pending_expired auto-reset sets the correct reason and DB end_reason
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from gateway.config import (
    GatewayConfig,
    Platform,
    SessionResetPolicy,
)
from gateway.session import SessionEntry, SessionSource, SessionStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_source(platform=Platform.TELEGRAM, chat_id="123", user_id="u1"):
    return SessionSource(
        platform=platform,
        chat_id=chat_id,
        user_id=user_id,
    )


def _make_store(policy=None, tmp_path=None, has_active_processes_fn=None):
    config = GatewayConfig()
    if policy:
        config.default_reset_policy = policy
    store = SessionStore(
        sessions_dir=tmp_path or "/tmp/test-sessions",
        config=config,
        has_active_processes_fn=has_active_processes_fn,
    )
    return store


# ---------------------------------------------------------------------------
# _should_reset returns reason string
# ---------------------------------------------------------------------------

class TestShouldResetReason:

    def test_returns_idle_when_idle_expired(self, tmp_path):
        store = _make_store(
            SessionResetPolicy(mode="idle", idle_minutes=30),
            tmp_path,
        )
        entry = SessionEntry(
            session_key="test",
            session_id="s1",
            created_at=datetime.now() - timedelta(hours=2),
            updated_at=datetime.now() - timedelta(hours=1),  # 60min ago > 30min threshold
        )
        source = _make_source()
        assert store._should_reset(entry, source) == "idle"


    def test_returns_none_when_active_process_check_raises(self, tmp_path):
        def _raise(_session_key):
            raise RuntimeError("process registry unavailable")

        store = _make_store(
            SessionResetPolicy(mode="idle", idle_minutes=30),
            tmp_path,
            has_active_processes_fn=_raise,
        )
        entry = SessionEntry(
            session_key="test",
            session_id="s1",
            created_at=datetime.now() - timedelta(hours=2),
            updated_at=datetime.now() - timedelta(hours=1),
        )
        source = _make_source()

        assert store._should_reset(entry, source) is None


# ---------------------------------------------------------------------------
# SessionEntry captures reason
# ---------------------------------------------------------------------------

class TestSessionEntryReason:


    def test_reset_had_activity_true_when_tokens_used(self, tmp_path):
        """Expired session with tokens → reset_had_activity=True."""
        store = _make_store(
            SessionResetPolicy(mode="idle", idle_minutes=1),
            tmp_path,
        )
        source = _make_source()

        entry1 = store.get_or_create_session(source)
        # Simulate some conversation happened (last_prompt_tokens is the field
        # written on every turn; total_tokens is never persisted).
        entry1.last_prompt_tokens = 5000
        entry1.updated_at = datetime.now() - timedelta(minutes=5)
        store._save()

        entry2 = store.get_or_create_session(source)
        assert entry2.was_auto_reset is True
        assert entry2.reset_had_activity is True


# ---------------------------------------------------------------------------
# SessionResetPolicy notify config
# ---------------------------------------------------------------------------

class TestResetPolicyNotify:

    def test_notify_exclude_defaults(self):
        policy = SessionResetPolicy()
        assert "api_server" in policy.notify_exclude_platforms
        assert "webhook" in policy.notify_exclude_platforms


    def test_from_dict_with_custom_excludes(self):
        policy = SessionResetPolicy.from_dict({
            "notify_exclude_platforms": ["api_server", "webhook", "homeassistant"],
        })
        assert "homeassistant" in policy.notify_exclude_platforms


# ---------------------------------------------------------------------------
# SessionEntry to_dict / from_dict roundtrip for auto-reset fields
# ---------------------------------------------------------------------------

class TestSessionEntryAutoResetRoundtrip:
    def test_was_auto_reset_persists_across_roundtrip(self, tmp_path):
        """was_auto_reset=True survives to_dict() → from_dict() (gateway restart)."""
        store = _make_store(
            SessionResetPolicy(mode="idle", idle_minutes=1),
            tmp_path,
        )
        source = _make_source()

        entry = store.get_or_create_session(source)
        entry.updated_at = datetime.now() - timedelta(minutes=5)
        store._save()

        entry2 = store.get_or_create_session(source)
        assert entry2.was_auto_reset is True
        assert entry2.auto_reset_reason == "idle"
        assert entry2.session_id != entry.session_id

        # Simulate gateway restart: reload from disk
        store._loaded = False
        store._entries.clear()
        store._ensure_loaded()

        reloaded = store._entries.get(entry2.session_key)
        assert reloaded is not None
        assert reloaded.was_auto_reset is True
        assert reloaded.auto_reset_reason == "idle"


class TestConsumeResetMarkers:
    def test_consumption_is_durable_across_store_restart(self, tmp_path):
        config = GatewayConfig()
        config.default_reset_policy = SessionResetPolicy(
            mode="idle",
            idle_minutes=1,
        )
        sessions_dir = tmp_path / "sessions"
        db_path = tmp_path / "state.db"
        store = SessionStore(
            sessions_dir=sessions_dir,
            config=config,
            db_path=db_path,
        )
        source = _make_source()
        old_entry = store.get_or_create_session(source)
        old_entry.last_prompt_tokens = 1000
        old_entry.updated_at = datetime.now() - timedelta(minutes=5)
        store._save()

        reset_entry = store.get_or_create_session(source)
        claimed = store.consume_reset_markers(
            reset_entry.session_key,
            reset_entry.session_id,
            reset_entry.capability_epoch,
        )

        assert claimed.was_auto_reset is True
        assert claimed.auto_reset_reason == "idle"
        assert claimed.reset_had_activity is True

        restarted = SessionStore(
            sessions_dir=sessions_dir,
            config=config,
            db_path=db_path,
        )
        reloaded = restarted.get_or_create_session(source)
        assert reloaded.session_id == reset_entry.session_id
        assert reloaded.was_auto_reset is False
        assert reloaded.auto_reset_reason is None

        second_claim = restarted.consume_reset_markers(
            reloaded.session_key,
            reloaded.session_id,
            reloaded.capability_epoch,
        )
        assert second_claim.was_auto_reset is False
        assert second_claim.auto_reset_reason is None

    def test_concurrent_consumers_get_exactly_one_reset_claim(self, tmp_path):
        store = _make_store(tmp_path=tmp_path)
        source = _make_source()
        entry = store.get_or_create_session(source)
        entry.was_auto_reset = True
        entry.auto_reset_reason = "daily"
        entry.reset_had_activity = True
        store._save()

        def _claim():
            return store.consume_reset_markers(
                entry.session_key,
                entry.session_id,
                entry.capability_epoch,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: _claim(), range(2)))

        assert sum(claim.was_auto_reset for claim in claims) == 1
        assert sorted(claim.auto_reset_reason or "" for claim in claims) == [
            "",
            "daily",
        ]

    def test_fresh_reset_marker_is_durably_consumed(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        db_path = tmp_path / "state.db"
        config = GatewayConfig()
        store = SessionStore(
            sessions_dir=sessions_dir,
            config=config,
            db_path=db_path,
        )
        source = _make_source()
        original = store.get_or_create_session(source)
        reset_entry = store.reset_session(original.session_key)

        claimed = store.consume_reset_markers(
            reset_entry.session_key,
            reset_entry.session_id,
            reset_entry.capability_epoch,
        )

        assert claimed.was_fresh_reset is True
        assert claimed.fresh_reset_reason == "explicit_new"

        restarted = SessionStore(
            sessions_dir=sessions_dir,
            config=config,
            db_path=db_path,
        )
        reloaded = restarted.get_or_create_session(source)
        assert reloaded.session_id == reset_entry.session_id
        assert reloaded.is_fresh_reset is False
        assert reloaded.fresh_reset_reason is None

    def test_session_identity_mismatch_does_not_consume(self, tmp_path):
        store = _make_store(tmp_path=tmp_path)
        source = _make_source()
        entry = store.get_or_create_session(source)
        entry.was_auto_reset = True
        entry.auto_reset_reason = "idle"
        store._save()

        try:
            store.consume_reset_markers(
                entry.session_key,
                "stale-session-id",
                entry.capability_epoch,
            )
        except RuntimeError as exc:
            assert "session changed" in str(exc)
        else:
            raise AssertionError("stale handler must not consume reset markers")

        assert entry.was_auto_reset is True
        assert entry.auto_reset_reason == "idle"

    def test_rotated_capability_epoch_blocks_same_session_id_aba(self, tmp_path):
        store = _make_store(tmp_path=tmp_path)
        source = _make_source()
        entry = store.get_or_create_session(source)
        stale_epoch = entry.capability_epoch
        entry.was_auto_reset = True
        entry.auto_reset_reason = "daily"
        # Model an A -> B -> A route switch: transcript id is unchanged, but
        # the live capability generation is intentionally fresh.
        entry.capability_epoch = SessionEntry(
            session_key=entry.session_key,
            session_id=entry.session_id,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
        ).capability_epoch
        store._save()

        try:
            store.consume_reset_markers(
                entry.session_key,
                entry.session_id,
                stale_epoch,
            )
        except RuntimeError as exc:
            assert "session changed" in str(exc)
        else:
            raise AssertionError("stale capability epoch must not claim markers")

        assert entry.was_auto_reset is True
        assert entry.auto_reset_reason == "daily"

    def test_primary_write_failure_does_not_acknowledge_or_clear_claim(
        self,
        tmp_path,
    ):
        config = GatewayConfig()
        sessions_dir = tmp_path / "sessions"
        db_path = tmp_path / "state.db"
        store = SessionStore(
            sessions_dir=sessions_dir,
            config=config,
            db_path=db_path,
        )
        source = _make_source()
        entry = store.get_or_create_session(source)
        entry.was_auto_reset = True
        entry.auto_reset_reason = "idle"
        store._save()

        with patch.object(
            store._db,
            "replace_gateway_routing_entries",
            side_effect=RuntimeError("synthetic primary failure"),
        ):
            try:
                store.consume_reset_markers(
                    entry.session_key,
                    entry.session_id,
                    entry.capability_epoch,
                )
            except RuntimeError as exc:
                assert "primary save failed" in str(exc)
            else:
                raise AssertionError("failed primary claim must not be acknowledged")

        assert entry.was_auto_reset is True
        assert entry.auto_reset_reason == "idle"

        restarted = SessionStore(
            sessions_dir=sessions_dir,
            config=config,
            db_path=db_path,
        )
        reloaded = restarted.get_or_create_session(source)
        assert reloaded.was_auto_reset is True
        assert reloaded.auto_reset_reason == "idle"


# ---------------------------------------------------------------------------
# resume_pending_expired: auto_reset_reason and DB end_reason (#58933)
# ---------------------------------------------------------------------------

def _make_db_mock() -> MagicMock:
    """Return a SessionDB mock with safe defaults for all lookup methods."""
    db = MagicMock()
    db.get_session.return_value = None
    db.get_compression_tip.return_value = None  # avoids MagicMock leaking into session_id
    db.find_latest_gateway_session_for_peer.return_value = None
    db.reopen_session.return_value = None
    db.create_session.return_value = None
    return db


def _make_store_with_db(tmp_path, db_mock=None, policy=None) -> SessionStore:
    """Build a SessionStore with a mock SessionDB, bypassing disk load."""
    cfg_policy = policy or SessionResetPolicy(mode="none")
    config = GatewayConfig(default_reset_policy=cfg_policy)
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=tmp_path, config=config)
    store._db = db_mock if db_mock is not None else _make_db_mock()
    store._loaded = True
    return store


class TestResumePendingExpiredAutoReset:
    """resume_pending sessions past the freshness window should fire
    was_auto_reset=True with auto_reset_reason='resume_pending_expired' and
    persist that reason to state.db (#58933)."""

    def _seed_stale_resume_pending(self, store, source, freshness_seconds=3600):
        """Create a session, mark it resume_pending, then backdate the mark
        past the freshness window so get_or_create_session treats it as a
        zombie."""
        entry = store.get_or_create_session(source)
        store.mark_resume_pending(entry.session_key)
        with store._lock:
            entry = store._entries[entry.session_key]
            entry.last_resume_marked_at = (
                datetime.now() - timedelta(seconds=freshness_seconds + 60)
            )
            entry.updated_at = datetime.now()  # keep updated_at fresh
            store._save()
        return entry

    def test_stale_resume_pending_sets_auto_reset_reason(
        self, tmp_path, monkeypatch
    ):
        """Stale resume_pending triggers was_auto_reset=True with reason
        'resume_pending_expired', NOT 'idle'."""
        monkeypatch.setenv("HERMES_AUTO_CONTINUE_FRESHNESS", "3600")
        # The freshness gate requires an opted-in reset policy — mode "none"
        # disables it entirely (#61052). Use a huge idle window so only the
        # freshness gate (not the idle policy) can fire.
        store = _make_store_with_db(
            tmp_path,
            policy=SessionResetPolicy(mode="idle", idle_minutes=999999),
        )
        source = _make_source()

        old = self._seed_stale_resume_pending(store, source)

        new = store.get_or_create_session(source)

        assert new.session_id != old.session_id, "should have created a new session"
        assert new.was_auto_reset is True
        assert new.auto_reset_reason == "resume_pending_expired"


    def test_stale_resume_pending_db_end_reason_is_specific(
        self, tmp_path, monkeypatch
    ):
        """state.db must record end_reason='resume_pending_expired', NOT the
        generic 'session_reset', so the event is auditable (#58933 fix)."""
        monkeypatch.setenv("HERMES_AUTO_CONTINUE_FRESHNESS", "3600")
        db = _make_db_mock()
        store = _make_store_with_db(
            tmp_path, db,
            policy=SessionResetPolicy(mode="idle", idle_minutes=999999),
        )
        source = _make_source()

        old = self._seed_stale_resume_pending(store, source)
        store.get_or_create_session(source)

        # Auto-reset now writes through promote_to_session_reset so an
        # accidental agent_close end can't shadow the reset boundary.
        db.promote_to_session_reset.assert_called_once()
        ended_id, ended_reason = db.promote_to_session_reset.call_args.args
        assert ended_id == old.session_id
        assert ended_reason == "resume_pending_expired", (
            f"expected 'resume_pending_expired', got {ended_reason!r} — "
            "the DB end_reason must not be the generic 'session_reset'"
        )

    def test_idle_reset_db_end_reason_reflects_idle(
        self, tmp_path
    ):
        """Regular idle auto-reset persists 'idle' as end_reason so that all
        auto-reset paths are auditable (#58933 should not regress the common
        idle/daily path)."""
        db = _make_db_mock()
        store = _make_store_with_db(
            tmp_path, db, policy=SessionResetPolicy(mode="idle", idle_minutes=1)
        )
        source = _make_source()

        entry = store.get_or_create_session(source)
        # Age past idle threshold.
        with store._lock:
            entry.updated_at = datetime.now() - timedelta(minutes=5)
            store._save()

        store.get_or_create_session(source)

        db.promote_to_session_reset.assert_called_once()
        _, ended_reason = db.promote_to_session_reset.call_args.args
        assert ended_reason == "idle"

