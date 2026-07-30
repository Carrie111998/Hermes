"""A multiplexed gateway must not revive a sibling profile's session (#74285).

``SessionDB.find_latest_gateway_session_for_peer`` falls back to matching only
the peer tuple ``(source, user_id, chat_id, chat_type, thread_id)`` when the
exact ``session_key`` is missing. For a Telegram DM that tuple is
byte-identical across profiles — ``chat_id == user_id`` and ``thread_id IS
NULL`` for every bot — so the fallback can hand back a row owned by a sibling
profile.

``SessionStore._recovered_row_allowed_for_active_profile`` exists to stop
exactly that, but it returned early whenever ``multiplex_profiles`` was
enabled: precisely the configuration in which several profiles each own a bot
token and can serve the same allowlisted user. The sibling profile was then
actually executed — its persona, tools, credentials and filesystem scope — so
this is a privilege boundary, not a session-list display detail.

The requested key already carries the routed profile, so the multiplexed case
has the information it needs to compare profiles rather than wave the row
through.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional
from unittest.mock import MagicMock, patch

from gateway.config import GatewayConfig, Platform, SessionResetPolicy
from gateway.session import SessionSource, SessionStore


def _db() -> MagicMock:
    """SessionDB mock: no routing state, recovery finds nothing by default."""
    db = MagicMock()
    db.get_session.return_value = None
    db.find_latest_gateway_session_for_peer.return_value = None
    db.reopen_session.return_value = None
    db.create_session.return_value = None
    # Mirror the real get_compression_tip identity for uncompressed sessions;
    # a bare Mock would be assigned as the session_id by the routing heal.
    db.get_compression_tip.side_effect = lambda sid: sid
    return db


def _store(tmp_path, db_mock: MagicMock, *, multiplex: bool) -> SessionStore:
    """Build a SessionStore with a mock SessionDB, bypassing disk load."""
    config = GatewayConfig(
        default_reset_policy=SessionResetPolicy(mode="none"),
        multiplex_profiles=multiplex,
    )
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=tmp_path, config=config)
    store._db = db_mock
    store._loaded = True
    return store


def _dm_source(profile: Optional[str]) -> SessionSource:
    """A Telegram DM from one user — same peer tuple whichever bot received it."""
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="8494508720",
        chat_type="dm",
        user_id="8494508720",
        profile=profile,
    )


def _row_owned_by(store: SessionStore, profile: Optional[str], session_id: str) -> dict:
    """A durable row the peer fallback would return, owned by ``profile``."""
    return {
        "id": session_id,
        "session_key": store._generate_session_key(_dm_source(profile)),
        "started_at": (datetime.now() - timedelta(hours=2)).timestamp(),
    }


class TestMultiplexedPeerFallbackProfileBoundary:
    """Multiplexing on: the routed profile in the requested key is authoritative."""

    def test_sibling_profile_row_is_rejected(self, tmp_path):
        """A row owned by ``admin`` must not be adopted for ``restricted``."""
        store = _store(tmp_path, _db(), multiplex=True)
        allowed = store._recovered_row_allowed_for_active_profile(
            requested_session_key=store._generate_session_key(_dm_source("restricted")),
            recovered=_row_owned_by(store, "admin", "sid_admin"),
        )
        assert allowed is False

    def test_same_profile_row_is_allowed(self, tmp_path):
        """Recovery within one profile keeps working."""
        store = _store(tmp_path, _db(), multiplex=True)
        allowed = store._recovered_row_allowed_for_active_profile(
            requested_session_key=store._generate_session_key(_dm_source("restricted")),
            recovered=_row_owned_by(store, "restricted", "sid_restricted"),
        )
        assert allowed is True

    def test_row_without_profile_namespace_is_allowed(self, tmp_path):
        """Rows whose key carries no parseable profile stay recoverable."""
        store = _store(tmp_path, _db(), multiplex=True)
        allowed = store._recovered_row_allowed_for_active_profile(
            requested_session_key=store._generate_session_key(_dm_source("restricted")),
            recovered={"id": "sid_legacy", "session_key": "legacy-unnamespaced-key"},
        )
        assert allowed is True

    def test_recovery_does_not_reuse_sibling_profile_session_id(self, tmp_path):
        """End to end: a DM routed to one profile never lands on another's session."""
        db = _db()
        store = _store(tmp_path, db, multiplex=True)
        db.find_latest_gateway_session_for_peer.return_value = _row_owned_by(
            store, "admin", "sid_admin"
        )

        entry = store.get_or_create_session(_dm_source("restricted"))

        assert entry.session_id != "sid_admin"
        assert store._generate_session_key(
            _dm_source("restricted")
        ) != store._generate_session_key(_dm_source("admin"))


class TestNonMultiplexedGuardUnchanged:
    """Multiplexing off: keep comparing against the process-wide active profile."""

    def test_other_profile_row_still_rejected(self, tmp_path):
        store = _store(tmp_path, _db(), multiplex=False)
        with patch.object(SessionStore, "_active_profile_name", staticmethod(lambda: "default")):
            allowed = store._recovered_row_allowed_for_active_profile(
                requested_session_key="agent:main:telegram:dm:8494508720",
                recovered={
                    "id": "sid_other",
                    "session_key": "agent:coder:telegram:dm:8494508720",
                },
            )
        assert allowed is False

    def test_active_profile_row_allowed(self, tmp_path):
        store = _store(tmp_path, _db(), multiplex=False)
        with patch.object(SessionStore, "_active_profile_name", staticmethod(lambda: "coder")):
            allowed = store._recovered_row_allowed_for_active_profile(
                requested_session_key="agent:main:telegram:dm:8494508720",
                recovered={
                    "id": "sid_coder",
                    "session_key": "agent:coder:telegram:dm:8494508720",
                },
            )
        assert allowed is True
