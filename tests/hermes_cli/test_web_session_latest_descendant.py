"""Dashboard session-resume lineage regression tests."""

from __future__ import annotations

import pytest

from hermes_cli.web_server import _session_latest_descendant
from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    try:
        yield session_db
    finally:
        session_db.close()


@pytest.mark.parametrize(
    ("child_id", "source", "model_config"),
    [
        ("branch", "webui", {"_branched_from": "parent"}),
        ("delegate", "subagent", {"_delegate_from": "parent"}),
        ("reset", "webui", {"_reset_from": "parent"}),
        ("tool", "tool", {}),
    ],
)
def test_latest_descendant_does_not_follow_non_continuation_children(
    db: SessionDB,
    child_id: str,
    source: str,
    model_config: dict,
) -> None:
    db.create_session("parent", source="webui")
    db.create_session(
        child_id,
        source=source,
        parent_session_id="parent",
        model_config=model_config,
    )

    assert _session_latest_descendant("parent", db) == ("parent", ["parent"])


def test_latest_descendant_still_follows_model_child_with_inherited_marker(
    db: SessionDB,
) -> None:
    db.create_session("branch", source="webui")
    db.create_session(
        "model-child",
        source="webui",
        parent_session_id="branch",
        model_config={"_branched_from": "original-parent"},
    )

    assert _session_latest_descendant("branch", db) == (
        "model-child",
        ["branch", "model-child"],
    )
