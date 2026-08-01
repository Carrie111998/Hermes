"""A restored session keeps the user's durable "keep" flag.

``set_session_pinned``'s contract: *"pinned is a durable 'keep' flag: pinned
sessions are exempt from the sessions.auto_archive stale sweep."* It is a user
decision, not live runtime state — the same class as ``archived``, which
``import_sessions`` already restores.

``import_sessions`` wrote every other durable column but not ``pinned``, so a
restored backup came back unpinned and the stale sweep — the very thing the
flag exists to opt out of — then archived exactly the sessions the user marked
keep.
"""

from __future__ import annotations

import pytest

from hermes_state import SessionDB

SESSION_ID = "20260801_120000_abc123"


def _seed(db_path, *, pinned: bool, archived: bool = False) -> dict:
    db = SessionDB(db_path=db_path)
    db.create_session(SESSION_ID, source="cli")
    db.append_message(SESSION_ID, "user", "keep this one")
    db.append_message(SESSION_ID, "assistant", "ok")
    if pinned:
        db.set_session_pinned(SESSION_ID, True)
    if archived:
        db.set_session_archived(SESSION_ID, True)
    return db.export_session(SESSION_ID)


@pytest.fixture
def restore(tmp_path):
    """Export from one DB and import into a fresh one; return the restored row."""

    def _restore(*, pinned: bool, archived: bool = False):
        payload = _seed(tmp_path / "source.db", pinned=pinned, archived=archived)
        assert payload is not None
        target = SessionDB(db_path=tmp_path / "target.db")
        result = target.import_sessions([payload])
        assert result["ok"] is True and result["imported"] == 1
        return target, target.get_session(SESSION_ID), payload

    return _restore


def test_pinned_survives_the_export_import_roundtrip(restore):
    """The regression: the keep-flag must not be dropped on restore."""
    _, restored, payload = restore(pinned=True)

    assert payload["pinned"], "sanity: the export payload carries the flag"
    assert restored["pinned"]


def test_unpinned_session_stays_unpinned(restore):
    """Restoring must not pin everything either."""
    _, restored, _ = restore(pinned=False)

    assert not restored["pinned"]


def test_archived_still_survives(restore):
    """Guard the sibling durable flag this import already handled."""
    _, restored, _ = restore(pinned=False, archived=True)

    assert restored["archived"]


def test_restored_pin_still_exempts_the_session_from_the_stale_sweep(restore):
    """The consequence: the sweep the flag opts out of must honour it."""
    target, restored, _ = restore(pinned=True)
    assert restored["pinned"]

    swept = target.archive_stale_sessions(idle_days=0, exclude_pinned=True)

    assert swept == 0
    assert not target.get_session(SESSION_ID)["archived"]


def test_unpinned_restored_session_is_still_sweepable(restore):
    """The exemption must be the pin, not the restore itself."""
    target, _, _ = restore(pinned=False)

    swept = target.archive_stale_sessions(idle_days=0, exclude_pinned=True)

    assert swept == 1
    assert target.get_session(SESSION_ID)["archived"]
