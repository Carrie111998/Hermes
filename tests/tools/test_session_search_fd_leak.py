"""Regression tests for the session_search SessionDB fd leak (#83027).

``session_search`` opens two kinds of handle for itself: the default
``SessionDB()`` when the caller passes no ``db``, and the read-only profile DB
from ``_resolve_profile_db`` on a cross-profile read. Before the fix neither
was closed on any of the function's eight return paths.

Neither handle is reclaimed by GC either: once its token-writer thread starts,
a ``SessionDB`` pins itself through ``atexit.register(_drain_token_queue_at_exit)``
and only ``close()`` unregisters it (see the note in ``run_agent.py``). So each
call leaked ~2 fds (``state.db`` + its WAL sidecar) for the life of the process.
Measured on a launchd-managed gateway with the macOS default 256-fd soft limit:
122 of 283 fds were these handles after 3.5h, at which point cron scripts,
SQLite writes and DNS lookups all began failing with ``[Errno 24]`` while the
process still reported healthy.

These tests assert ownership rather than raw fd counts, so they are
deterministic and platform-independent: whoever opened a handle closes it, and
a caller-supplied handle is never touched.
"""
import time
from unittest.mock import MagicMock

import pytest

from hermes_state import SessionDB
from tools.session_search_tool import session_search


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _seed(db, session_id="s1"):
    """One session with a searchable message, so discovery has a hit."""
    db.create_session(session_id, source="cli")
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
        (int(time.time()) - 60, "Fd leak fixture", session_id),
    )
    db.append_message(session_id, role="user", content="searchable modpack content")


def _tracked(instance):
    """Wrap ``close`` so the test can assert on it without disabling it."""
    instance.close = MagicMock(wraps=instance.close)
    return instance


def test_lazily_opened_db_is_closed(tmp_path, monkeypatch):
    """No caller ``db`` → session_search opens one and must close it."""
    opened = []

    def _factory(*args, **kwargs):
        opened.append(_tracked(SessionDB(tmp_path / "state.db")))
        return opened[-1]

    monkeypatch.setattr("hermes_state.SessionDB", _factory)

    session_search(query="modpack", limit=1)

    assert len(opened) == 1, "expected exactly one self-opened SessionDB"
    opened[0].close.assert_called_once()


def test_caller_supplied_db_is_not_closed(db):
    """A caller-owned handle outlives the call — closing it would break the caller."""
    _seed(db)
    _tracked(db)

    session_search(query="modpack", limit=1, db=db)

    db.close.assert_not_called()


def test_profile_db_is_closed_and_caller_db_untouched(tmp_path, db, monkeypatch):
    """Cross-profile read swaps in a second handle — it must be closed too."""
    _seed(db)
    profile_db = _tracked(SessionDB(tmp_path / "other_state.db"))
    _seed(profile_db, "s_other")
    _tracked(db)

    monkeypatch.setattr(
        "tools.session_search_tool._resolve_profile_db", lambda profile: profile_db
    )

    session_search(query="modpack", limit=1, db=db, profile="other")

    profile_db.close.assert_called_once()
    db.close.assert_not_called()


def test_self_opened_db_is_closed_on_early_return(tmp_path, monkeypatch):
    """Every return path releases the handle, including the error shapes.

    ``_resolve_profile_db`` raising returns a tool_error long before the normal
    exit — the pre-fix code leaked the default handle on exactly these paths.
    """
    opened = []

    def _factory(*args, **kwargs):
        opened.append(_tracked(SessionDB(tmp_path / "state.db")))
        return opened[-1]

    def _boom(profile):
        raise ValueError(f"profile '{profile}' does not exist")

    monkeypatch.setattr("hermes_state.SessionDB", _factory)
    monkeypatch.setattr("tools.session_search_tool._resolve_profile_db", _boom)

    result = session_search(query="modpack", limit=1, profile="nope")

    assert "does not exist" in result
    assert len(opened) == 1
    opened[0].close.assert_called_once()
