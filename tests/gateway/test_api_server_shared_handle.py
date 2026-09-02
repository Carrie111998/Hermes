"""Regression tests for #100896: the API-server profile cache must acquire
SessionDB handles through the shared per-path registry, not raw
constructions.

The reporter's production gateway logged "5 live SessionDB handles on
state.db in this process; each holds its own writer connection" seven
minutes before corruption incident #4 — and the handle-count warning
machinery (hermes_state.py:573) exists precisely to surface redundant
writers before they become incidents. The api_server's multiplex cache was
a redundant-writer source: every profile served constructed a RAW
``SessionDB(db_path=...)`` holding its own writer connection on the same
WAL file, alongside every other long-lived surface in the same process.

The registry's own routing rule (hermes_state.py:4353-4358) says long-lived
in-process callers share ONE writer connection per resolved path via
``get_shared_session_db()`` — the api_server was the one long-lived
surface not following it.

Contracts pinned here:
- the cache acquires through the registry, keyed per resolved profile path
- two profiles get two distinct shared instances (no cross-profile bleed)
- teardown RELEASES (refcount drop), never closes the underlying handle —
  a sibling surface may still hold the same instance
- close-after-close is a no-op (idempotent shutdown)
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _adapter():
    """Minimal APIServer-shaped object with the cache attributes."""
    from gateway.platforms.api_server import APIServerAdapter

    adapter = APIServerAdapter.__new__(APIServerAdapter)
    adapter._session_db = None
    adapter._session_dbs = {}
    adapter._session_db_cache_lock = __import__("threading").Lock()
    adapter._session_db_cache_closed = False
    return adapter


class TestRegistryBackedProfileCache:
    def test_cache_acquires_via_registry_not_raw_construction(self, tmp_path):
        """The #100896 core contract: no raw SessionDB() writer handles."""
        from hermes_state import SessionDB

        adapter = _adapter()
        with patch("hermes_state.get_shared_session_db") as mock_shared:
            sentinel = MagicMock(spec=SessionDB)
            mock_shared.return_value = sentinel
            db = adapter._open_and_cache_session_db(tmp_path)

        mock_shared.assert_called_once_with(tmp_path / "state.db")
        assert db is sentinel

    def test_same_profile_reuses_the_same_instance(self, tmp_path):
        """Second request for the same home must not acquire again."""
        adapter = _adapter()
        with patch("hermes_state.get_shared_session_db") as mock_shared:
            first = MagicMock()
            mock_shared.return_value = first
            a = adapter._open_and_cache_session_db(tmp_path)
            b = adapter._open_and_cache_session_db(tmp_path)

        assert a is b is first
        assert mock_shared.call_count == 1, "one acquire per profile, ever"

    def test_two_profiles_two_distinct_shared_instances(self, tmp_path):
        """Multiplex: each profile's state.db is a different registry key —
        but they are REGISTRY instances, not private writers."""
        adapter = _adapter()
        home_a, home_b = tmp_path / "a", tmp_path / "b"
        with patch("hermes_state.get_shared_session_db") as mock_shared:
            db_a, db_b = MagicMock(), MagicMock()
            mock_shared.side_effect = [db_a, db_b]
            first = adapter._open_and_cache_session_db(home_a)
            second = adapter._open_and_cache_session_db(home_b)

        assert first is db_a
        assert second is db_b
        assert mock_shared.call_count == 2

    def test_teardown_releases_never_closes(self, tmp_path):
        """Registry handles are refcount-dropped; a sibling surface holding
        the same instance keeps its writer alive. Closing the underlying
        handle outright is the multi-writer churn this conversion removes."""
        adapter = _adapter()
        shared = MagicMock()
        with patch("hermes_state.get_shared_session_db", return_value=shared):
            adapter._open_and_cache_session_db(tmp_path)

        with patch("hermes_state.release_shared_session_db") as mock_release:
            adapter._close_cached_session_dbs()

        mock_release.assert_called_once_with(shared)
        shared.close.assert_not_called(), "release, not close — siblings exist"

    def test_teardown_idempotent(self, tmp_path):
        """Close-after-close must not double-release or raise."""
        adapter = _adapter()
        with patch("hermes_state.get_shared_session_db"):
            adapter._open_and_cache_session_db(tmp_path)
        adapter._close_cached_session_dbs()
        with patch("hermes_state.release_shared_session_db") as mock_release:
            adapter._close_cached_session_dbs()
        mock_release.assert_not_called(), "second close is a no-op"

    def test_cache_closed_refuses_new_acquires(self, tmp_path):
        """After teardown, new requests get None, not a fresh writer."""
        adapter = _adapter()
        adapter._close_cached_session_dbs()
        with patch("hermes_state.get_shared_session_db") as mock_shared:
            db = adapter._open_and_cache_session_db(tmp_path)
        assert db is None
        mock_shared.assert_not_called()

    def test_explicit_override_db_is_never_released(self, tmp_path):
        """The explicit test/manual-override handle is caller-owned: teardown
        must skip it (previous behavior preserved)."""
        adapter = _adapter()
        adapter._session_db = override = MagicMock()
        with patch("hermes_state.get_shared_session_db", return_value=MagicMock()):
            adapter._open_and_cache_session_db(tmp_path)
        with patch("hermes_state.release_shared_session_db") as mock_release:
            adapter._close_cached_session_dbs()
        mock_release.assert_called_once()  # only the cached one
        override.close.assert_not_called()

    def test_release_failure_does_not_raise(self, tmp_path):
        """A failed release logs and moves on — shutdown must complete."""
        adapter = _adapter()
        with patch("hermes_state.get_shared_session_db"):
            adapter._open_and_cache_session_db(tmp_path)
        with patch("hermes_state.release_shared_session_db",
                   side_effect=RuntimeError("boom")):
            adapter._close_cached_session_dbs()  # must not raise
