"""Seam tests for the R3-C9 Title cluster extraction (hermes_state_title.py).

The window bytes (hermes_state.py lines 5291-5463 at pin 01a1037d1e / blob
bf9a24d31d35b29cf1eebedf20c5686ea6d64933) moved byte-verbatim into
``SessionTitleMixin``; ``hermes_state.SessionDB`` now inherits it. These
tests pin the seam: (a) identity — every moved name resolves ``is``-identical
through the class, (b) MRO — the mixin is a direct base (appended last, per
R3-CONSENSUS §5), (c) behavioral round-trip through the SessionDB path using
the repo's db-fixture pattern (tests/test_hermes_state.py).
"""

import time

import pytest

import hermes_state
import hermes_state_title
from hermes_state import SessionDB
from hermes_state_title import SessionTitleMixin

#: Moved names: (class attribute, expected kind)
MOVED_NAMES = [
    "MAX_TITLE_LENGTH",
    "sanitize_title",
    "_is_compression_ancestor",
    "_set_session_title",
    "set_session_title",
    "set_auto_title_if_empty",
    "get_session_title",
]


@pytest.fixture()
def db(tmp_path):
    """Create a SessionDB with a temp database file (repo fixture pattern)."""
    db_path = tmp_path / "test_state.db"
    session_db = SessionDB(db_path=db_path)
    session_db.create_session("sess-title", source="cli")
    yield session_db
    session_db.close()


# (a) identity ---------------------------------------------------------------


def test_moved_names_identity_through_class():
    """Every moved name is `is`-identical through SessionDB (seam identity)."""
    for name in MOVED_NAMES:
        leaf_attr = getattr(SessionTitleMixin, name)
        class_attr = getattr(SessionDB, name)
        assert class_attr is leaf_attr, f"{name}: SessionDB.{name} is not SessionTitleMixin.{name}"
        assert getattr(hermes_state.SessionDB, name) is leaf_attr


def test_leaf_module_host_proxy_resolves_max_title_length():
    """The leaf's lazy host proxy resolves the real class attr at call time."""
    assert hermes_state_title.SessionDB.MAX_TITLE_LENGTH is SessionDB.MAX_TITLE_LENGTH


# (b) MRO --------------------------------------------------------------------


def test_mixin_is_direct_base_appended_last():
    """SessionTitleMixin is a direct base of SessionDB, appended last (contract)."""
    assert SessionDB.__mro__ == (
        SessionDB,
        hermes_state.SessionSearchMixin,
        hermes_state.SessionSchemaMixin,
        hermes_state.SessionPortabilityMixin,
        SessionTitleMixin,
        object,
    )


# (c) behavioral round-trip ---------------------------------------------------


def test_sanitize_title_behavior():
    """Sanitization: control chars stripped, whitespace collapsed, None passthrough."""
    s = SessionDB.sanitize_title
    assert s(None) is None
    assert s("") is None
    assert s("   \t\n  ") is None
    assert s("  Hello   World  ") == "Hello World"
    assert s("a\x00b\x1fc") == "abc"
    assert s("zero\u200bwidth") == "zerowidth"
    assert s("rtl\u202erun") == "rtlrun"
    with pytest.raises(ValueError, match="too long"):
        s("x" * 101)


def test_set_get_title_roundtrip(db):
    """Set/get round-trip through the moved methods."""
    assert db.get_session_title("sess-title") is None
    assert db.set_session_title("sess-title", "  My   Title  ") is True
    assert db.get_session_title("sess-title") == "My Title"
    # empty/whitespace-only clears
    assert db.set_session_title("sess-title", "   ") is True
    assert db.get_session_title("sess-title") is None
    # unknown session
    assert db.set_session_title("nope", "t") is False
    assert db.get_session_title("nope") is None


def test_set_title_unique_conflict_raises(db):
    """A title in use by another, non-ancestor session raises ValueError."""
    db.create_session("other", source="cli")
    db.set_session_title("sess-title", "taken")
    with pytest.raises(ValueError, match="already in use"):
        db.set_session_title("other", "taken")


def test_set_auto_title_if_empty_only_when_null(db):
    """Auto-title only fills NULL titles; manual title is never overwritten."""
    assert db.set_auto_title_if_empty("sess-title", "auto") is True
    assert db.get_session_title("sess-title") == "auto"
    # manual rename wins
    db.set_session_title("sess-title", "manual")
    assert db.set_auto_title_if_empty("sess-title", "second-auto") is False
    assert db.get_session_title("sess-title") == "manual"


def test_compression_ancestor_title_transfer(db):
    """Renaming a continuation back to its base title transfers the title off
    the ended, hidden ancestor (lineage-aware uniqueness)."""

    t0 = time.time() - 3600
    db.create_session("root", "cli")
    db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0, "root"))
    db._conn.execute(
        "UPDATE sessions SET ended_at=?, end_reason='compression' WHERE id=?",
        (t0 + 100, "root"),
    )
    db.create_session("tip", "cli", parent_session_id="root")
    db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0 + 200, "tip"))
    db._conn.commit()

    db.set_session_title("root", "fingerprint-scanner")
    db.set_session_title("tip", "fingerprint-scanner #2")
    # rename tip back to base name — must succeed via ancestor transfer
    assert db.set_session_title("tip", "fingerprint-scanner") is True
    assert db.get_session_title("tip") == "fingerprint-scanner"
    assert db.get_session_title("root") is None
