"""Seam tests for the R3 Title cluster extraction (hermes_state_title.py).

The window bytes (hermes_state.py lines 5291-5463 at pin ea0d54db1d) moved
byte-verbatim into ``SessionTitleMixin``; ``hermes_state.SessionDB`` now
inherits it. These tests pin the seam: every moved name must resolve
``is``-identical through the class, and title behavior (sanitize, set/get
round-trip, compression-ancestor transfer) must work through the SessionDB
re-export path exactly as before the extraction.
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
    d = SessionDB(db_path=tmp_path / "state.db")
    d.create_session("sess-title", source="cli")
    yield d
    d.close()


def test_leaf_is_mixin_class():
    """The leaf module exports exactly the mixin class."""
    assert SessionTitleMixin.__name__ == "SessionTitleMixin"


def test_moved_names_identity_through_class():
    """Every moved name is `is`-identical through SessionDB (seam identity)."""
    for name in MOVED_NAMES:
        leaf_attr = getattr(SessionTitleMixin, name)
        class_attr = getattr(SessionDB, name)
        assert class_attr is leaf_attr, f"{name}: SessionDB.{name} is not SessionTitleMixin.{name}"
        # and it resolves through hermes_state the same way
        assert getattr(hermes_state.SessionDB, name) is leaf_attr


def test_leaf_module_reexports_are_identical():
    """Module attribute on hermes_state (module-level lookup) matches leaf."""
    for name in MOVED_NAMES:
        assert getattr(SessionTitleMixin, name) is getattr(
            hermes_state.SessionDB, name
        )


def test_mixin_declared_as_base():
    """SessionTitleMixin is a direct base of SessionDB (MRO sanity)."""
    assert SessionTitleMixin in SessionDB.__mro__
    assert SessionDB.__mro__.index(SessionTitleMixin) > 0


def test_max_title_length_constant():
    """The moved constant is the same object through both paths."""
    assert SessionDB.MAX_TITLE_LENGTH == 100
    assert hermes_state_title.SessionTitleMixin.MAX_TITLE_LENGTH == 100


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
    with pytest.raises(ValueError):
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
    with pytest.raises(ValueError):
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

    def _make_chain(t0):
        db.create_session("root", "cli")
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0, "root"))
        db._conn.execute(
            "UPDATE sessions SET ended_at=?, end_reason='compression' WHERE id=?",
            (t0 + 100, "root"),
        )
        db.create_session("tip", "cli", parent_session_id="root")
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0 + 200, "tip"))
        db._conn.commit()

    _make_chain(time.time() - 3600)
    db.set_session_title("root", "fingerprint-scanner")
    db.set_session_title("tip", "fingerprint-scanner #2")
    # rename tip back to base name — must succeed via ancestor transfer
    assert db.set_session_title("tip", "fingerprint-scanner") is True
    assert db.get_session_title("tip") == "fingerprint-scanner"
    assert db.get_session_title("root") is None


def test_is_compression_ancestor_logic(db):
    """Direct probe of the moved ancestor-walk (via _set_session_title path)."""
    t0 = time.time() - 3600
    db.create_session("root2", "cli")
    db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0, "root2"))
    db._conn.execute(
        "UPDATE sessions SET ended_at=?, end_reason='compression' WHERE id=?",
        (t0 + 100, "root2"),
    )
    db.create_session("tip2", "cli", parent_session_id="root2")
    db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0 + 200, "tip2"))
    db._conn.commit()

    # a parent that ended WITHOUT compression is not a compression ancestor,
    # even when it has a child (delegate subagent / branch child)
    db.create_session("done_parent", "cli")
    db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0, "done_parent"))
    db._conn.execute(
        "UPDATE sessions SET ended_at=?, end_reason='done' WHERE id=?",
        (t0 + 100, "done_parent"),
    )
    db.create_session("plain_child", "cli", parent_session_id="done_parent")
    db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0 + 200, "plain_child"))
    db._conn.commit()
    assert (
        db._is_compression_ancestor(
            db._conn, ancestor_id="done_parent", descendant_id="plain_child"
        )
        is False
    )
    assert (
        db._is_compression_ancestor(
            db._conn, ancestor_id="root2", descendant_id="tip2"
        )
        is True
    )
    # self / empty guards
    assert db._is_compression_ancestor(db._conn, ancestor_id="tip2", descendant_id="tip2") is False
    assert db._is_compression_ancestor(db._conn, ancestor_id="", descendant_id="tip2") is False


def test_host_proxy_resolves_max_title_length():
    """The lazy host proxy resolves SessionDB.MAX_TITLE_LENGTH at call time."""
    assert hermes_state_title.SessionDB.MAX_TITLE_LENGTH == 100
    # and the proxy's attribute IS the host constant
    assert hermes_state_title.SessionDB.MAX_TITLE_LENGTH is SessionDB.MAX_TITLE_LENGTH
