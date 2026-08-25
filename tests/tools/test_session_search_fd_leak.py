"""Regression: session_search must close profile_db on every return path.

The session_search_tool opens a read-only SessionDB when called with a
profile parameter via _resolve_profile_db(), but the function had no
try/finally to guarantee it was closed before returning on any path
(discovery, scroll, read, browse). This leaked a state.db connection + its
WAL/SHM file descriptors on every call with profile set, accumulating on
a busy gateway (~10/day). The fix wraps the entire function logic in
try/finally so profile_db.close() runs before every return.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from tools import session_search_tool


class _TrackingConnection:
    """Delegates to a real sqlite3.Connection while recording close() calls."""

    def __init__(self, real, closed_ids):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_closed_ids", closed_ids)

    def close(self):
        self._closed_ids.append(id(self._real))
        self._real.close()

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._real, name, value)


def _track_sessiondb_closes(monkeypatch):
    """Intercept SessionDB.close() to track calls."""
    closed_ids = []
    real_close = None

    def tracking_close(self):
        closed_ids.append(id(self))
        if real_close:
            real_close(self)

    from hermes_state import SessionDB

    # Store the original close method
    real_close = SessionDB.close
    monkeypatch.setattr(SessionDB, "close", tracking_close)

    return closed_ids


def test_session_search_closes_profile_db_on_discovery(monkeypatch, tmp_path):
    """Calling session_search with profile param must close profile_db before returning."""
    closed_ids = _track_sessiondb_closes(monkeypatch)

    # Mock _resolve_profile_db to track which instances were created
    opened_ids = []
    original_resolve = session_search_tool._resolve_profile_db

    def tracking_resolve(profile):
        from hermes_state import SessionDB

        # Create a temporary state.db for the mock profile
        mock_dir = tmp_path / f"profile_{profile}"
        mock_dir.mkdir(exist_ok=True)
        db_path = mock_dir / "state.db"

        # Create a minimal state.db so SessionDB() doesn't fail
        temp_conn = sqlite3.connect(str(db_path))
        temp_conn.close()

        db = SessionDB(db_path=db_path, read_only=True)
        opened_ids.append(id(db))
        return db

    monkeypatch.setattr(session_search_tool, "_resolve_profile_db", tracking_resolve)

    # Call session_search with a profile (discovery shape: just query)
    # This should return early but still close profile_db
    try:
        result = session_search_tool.session_search(
            query="test", profile="mock_profile", limit=1
        )
        # Parse result to ensure it succeeded
        import json

        parsed = json.loads(result)
        assert parsed.get("success") is False  # No actual sessions, but call completed
    except Exception:
        # Expected: no real sessions, but the test is about fd cleanup
        pass

    # Verify: every opened SessionDB was closed
    assert opened_ids, "Expected at least one SessionDB to be opened"
    for opened_id in opened_ids:
        assert (
            opened_id in closed_ids
        ), f"SessionDB {opened_id} was opened but never closed"


def test_session_search_closes_profile_db_on_read(monkeypatch, tmp_path):
    """Reading a session with profile param must close profile_db."""
    closed_ids = _track_sessiondb_closes(monkeypatch)

    opened_ids = []
    original_resolve = session_search_tool._resolve_profile_db

    def tracking_resolve(profile):
        from hermes_state import SessionDB

        mock_dir = tmp_path / f"profile_{profile}"
        mock_dir.mkdir(exist_ok=True)
        db_path = mock_dir / "state.db"

        temp_conn = sqlite3.connect(str(db_path))
        temp_conn.close()

        db = SessionDB(db_path=db_path, read_only=True)
        opened_ids.append(id(db))
        return db

    monkeypatch.setattr(session_search_tool, "_resolve_profile_db", tracking_resolve)

    # Call with session_id (read shape) — should still close profile_db
    try:
        result = session_search_tool.session_search(
            session_id="fake_session_id", profile="mock_profile"
        )
        import json

        parsed = json.loads(result)
        # Will be success=false because session doesn't exist, but cleanup should run
    except Exception:
        pass

    # Verify all opened DBs were closed
    assert opened_ids, "Expected at least one SessionDB to be opened"
    for opened_id in opened_ids:
        assert opened_id in closed_ids, f"SessionDB {opened_id} was never closed (read path)"


def test_session_search_closes_profile_db_on_browse(monkeypatch, tmp_path):
    """Browsing with profile param (no query, no session_id) must close profile_db."""
    closed_ids = _track_sessiondb_closes(monkeypatch)

    opened_ids = []

    def tracking_resolve(profile):
        from hermes_state import SessionDB

        mock_dir = tmp_path / f"profile_{profile}"
        mock_dir.mkdir(exist_ok=True)
        db_path = mock_dir / "state.db"

        temp_conn = sqlite3.connect(str(db_path))
        temp_conn.close()

        db = SessionDB(db_path=db_path, read_only=True)
        opened_ids.append(id(db))
        return db

    monkeypatch.setattr(session_search_tool, "_resolve_profile_db", tracking_resolve)

    # Call with profile but no query/session_id (browse shape)
    try:
        result = session_search_tool.session_search(profile="mock_profile", limit=5)
        import json

        parsed = json.loads(result)
    except Exception:
        pass

    # Verify all opened DBs were closed
    assert opened_ids, "Expected at least one SessionDB to be opened"
    for opened_id in opened_ids:
        assert (
            opened_id in closed_ids
        ), f"SessionDB {opened_id} was never closed (browse path)"


def test_session_search_closes_profile_db_even_on_exception(monkeypatch, tmp_path):
    """profile_db must be closed even if an exception occurs during processing."""
    closed_ids = _track_sessiondb_closes(monkeypatch)

    opened_ids = []

    def tracking_resolve(profile):
        from hermes_state import SessionDB

        mock_dir = tmp_path / f"profile_{profile}"
        mock_dir.mkdir(exist_ok=True)
        db_path = mock_dir / "state.db"

        temp_conn = sqlite3.connect(str(db_path))
        temp_conn.close()

        db = SessionDB(db_path=db_path, read_only=True)
        opened_ids.append(id(db))
        return db

    monkeypatch.setattr(session_search_tool, "_resolve_profile_db", tracking_resolve)

    # Mock one of the helper functions to raise an exception
    original_discover = session_search_tool._discover

    def failing_discover(*args, **kwargs):
        raise RuntimeError("Simulated failure in _discover")

    monkeypatch.setattr(session_search_tool, "_discover", failing_discover)

    # Call with query (discovery shape that will fail)
    # The exception should not prevent profile_db.close() from running
    try:
        result = session_search_tool.session_search(query="test", profile="mock_profile")
    except RuntimeError:
        pass  # Expected

    # Verify: every opened SessionDB was still closed despite the exception
    assert opened_ids, "Expected at least one SessionDB to be opened"
    for opened_id in opened_ids:
        assert (
            opened_id in closed_ids
        ), f"SessionDB {opened_id} was not closed after exception (exception safety)"
