"""Seam tests for the R4-C5 RowID/Role extraction (hermes_state_rowid.py).

The window bytes (hermes_state.py lines 6709-6767 at pin aaf9688519) moved
byte-verbatim into ``SessionRowIdMixin``; ``hermes_state.SessionDB`` now
inherits it. These tests pin the seam: every moved name must resolve
``is``-identical through the class, and the RowID/Role behavior must work
through the SessionDB re-export path.
"""

import pytest

import hermes_state
import hermes_state_rowid
from hermes_state import SessionDB
from hermes_state_rowid import SessionRowIdMixin

#: Moved names: (class attribute, name on the leaf mixin)
MOVED_NAMES = [
    "latest_message_row_id",
    "latest_user_message_row_id",
    "get_message_role",
]


@pytest.fixture()
def db(tmp_path):
    d = SessionDB(db_path=tmp_path / "state.db")
    d.create_session("sess-rowid", source="cli")
    yield d
    d.close()


def test_leaf_is_mixin_class():
    """The leaf module exports exactly the mixin class."""
    assert SessionRowIdMixin.__name__ == "SessionRowIdMixin"


def test_moved_names_identity_through_class():
    """Every moved method is `is`-identical through SessionDB (seam identity).

    The mixin is a real base of SessionDB, so attribute lookup walks the
    MRO to the same function object the leaf module defines.
    """
    for name in MOVED_NAMES:
        leaf_attr = getattr(SessionRowIdMixin, name)
        class_attr = getattr(SessionDB, name)
        assert class_attr is leaf_attr, f"{name}: SessionDB.{name} is not SessionRowIdMixin.{name}"
        # and it resolves through hermes_state the same way
        assert getattr(hermes_state.SessionDB, name) is leaf_attr


def test_leaf_module_reexports_are_identical():
    """Module attribute on hermes_state (module-level lookup) matches leaf."""
    for name in MOVED_NAMES:
        assert getattr(hermes_state_rowid.SessionRowIdMixin, name) is getattr(
            hermes_state.SessionDB, name
        )


def test_mixin_declared_as_base():
    """SessionRowIdMixin is a direct base of SessionDB (MRO sanity)."""
    assert SessionRowIdMixin in SessionDB.__mro__
    assert SessionDB.__mro__.index(SessionRowIdMixin) > 0


def test_rowid_assignment_and_role_roundtrip(db):
    """Row ids are durable and monotonic; roles round-trip via get_message_role."""
    db.append_messages_batch(
        "sess-rowid",
        [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "second question"},
        ],
    )

    # latest user message is the third row
    last_user = db.latest_user_message_row_id("sess-rowid")
    assert last_user is not None
    assert last_user > 0

    # latest message (default role=user) == latest user message
    assert db.latest_message_row_id("sess-rowid") == last_user

    # latest assistant message is the second row
    last_assistant = db.latest_message_row_id("sess-rowid", role="assistant")
    assert last_assistant is not None
    assert last_assistant < last_user

    # roles round-trip
    assert db.get_message_role("sess-rowid", last_user) == "user"
    assert db.get_message_role("sess-rowid", last_assistant) == "assistant"

    # offset steps back: second-to-latest user message is the first row
    assert db.latest_message_row_id("sess-rowid", offset=1) == last_user - 2

    # unknown row / empty session guards
    assert db.get_message_role("sess-rowid", 999999) is None
    assert db.latest_message_row_id("") is None
    assert db.latest_message_row_id("sess-rowid", role="system") is None
    assert db.latest_message_row_id("sess-rowid", offset=-1) is None


def test_rowid_skips_empty_content_when_require_text(db):
    """require_text (default) skips rows with no plain-text content."""
    db.append_messages_batch(
        "sess-rowid",
        [
            {"role": "user", "content": "visible question"},
            {"role": "assistant", "content": "", "tool_calls": [{"name": "terminal", "arguments": "{}"}]},
        ],
    )
    # default: latest user message is the visible one
    assert db.get_message_role("sess-rowid", db.latest_user_message_row_id("sess-rowid")) == "user"
    # require_text (default) skips the empty assistant row -> None
    assert db.latest_message_row_id("sess-rowid", role="assistant") is None
    # require_text=False resolves to the empty assistant row (id 2)
    latest_any = db.latest_message_row_id("sess-rowid", role="assistant", require_text=False)
    assert latest_any is not None
    assert db.get_message_role("sess-rowid", latest_any) == "assistant"
