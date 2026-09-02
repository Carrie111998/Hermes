"""Behavior contract for durable lineage metadata on ``session.list``."""

from __future__ import annotations

import json

import pytest

from hermes_state import SessionDB
import tui_gateway.methods_session  # noqa: F401  (registers RPC methods)
import tui_gateway.server as srv


@pytest.fixture
def db(tmp_path, monkeypatch):
    database = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(srv, "_get_db", lambda: database)
    try:
        yield database
    finally:
        database.close()


def _seed(
    db: SessionDB,
    sid: str,
    *,
    parent: str | None = None,
    branch_from: str | None = None,
    source: str = "desktop",
    started_at: float = 1.0,
) -> None:
    model_config = {"_branched_from": branch_from} if branch_from is not None else None
    db.create_session(
        sid,
        source=source,
        model_config=model_config,
        parent_session_id=parent,
    )
    db._conn.execute(
        "UPDATE sessions SET title = ?, started_at = ?, last_activity_at = ?, "
        "message_count = 1 WHERE id = ?",
        (sid, started_at, started_at, sid),
    )
    db._conn.commit()


def _compress(db: SessionDB, parent: str, child: str, *, started_at: float) -> None:
    parent_row = db.get_session(parent)
    inherited = (
        json.loads(parent_row["model_config"])
        if parent_row.get("model_config")
        else None
    )
    db._conn.execute(
        "UPDATE sessions SET ended_at = ?, end_reason = 'compression' WHERE id = ?",
        (started_at - 0.5, parent),
    )
    _seed(db, child, parent=parent, started_at=started_at)
    if inherited:
        db._conn.execute(
            "UPDATE sessions SET model_config = ? WHERE id = ?",
            (json.dumps(inherited), child),
        )
        db._conn.commit()


def _list(**params) -> dict:
    envelope = srv._methods["session.list"](1, params)
    assert "error" not in envelope, envelope
    return envelope["result"]


def _by_id(result: dict) -> dict[str, dict]:
    return {row["id"]: row for row in result["sessions"]}


def test_plain_branch_adds_compatible_optional_lineage_fields(db):
    _seed(db, "parent", started_at=1)
    _seed(db, "branch", parent="parent", branch_from="parent", started_at=2)

    result = _list()
    rows = _by_id(result)

    assert rows["branch"]["parent_session_id"] == "parent"
    assert rows["branch"]["branch_parent_root_id"] == "parent"
    assert rows["branch"]["last_active"] == 2
    assert set((
        "id",
        "title",
        "preview",
        "started_at",
        "message_count",
        "source",
    )) <= set(rows["branch"])
    assert "model_config" not in rows["branch"]
    assert "_branched_from" not in rows["branch"]
    assert result["total"] == 2
    assert result["has_more"] is False


def test_legacy_branch_uses_the_same_listable_fallback(db):
    _seed(db, "legacy-parent", started_at=1)
    db._conn.execute(
        "UPDATE sessions SET ended_at = 1.5, end_reason = 'branched' "
        "WHERE id = 'legacy-parent'"
    )
    db._conn.commit()
    _seed(db, "legacy-child", parent="legacy-parent", started_at=2)

    row = _by_id(_list())["legacy-child"]

    assert row["parent_session_id"] == "legacy-parent"
    assert row["branch_parent_root_id"] == "legacy-parent"


def test_exact_title_lookup_keeps_root_tip_shape_and_adds_lineage(db):
    _seed(db, "parent-root", started_at=1)
    _compress(db, "parent-root", "parent-tip", started_at=2)
    _seed(
        db,
        "named-branch",
        parent="parent-tip",
        branch_from="parent-tip",
        started_at=3,
    )

    result = _list(title="named-branch")
    row = result["sessions"][0]

    assert row["id"] == "named-branch"
    assert row["resolved_id"] == "named-branch"
    assert row["parent_session_id"] == "parent-tip"
    assert row["branch_parent_root_id"] == "parent-root"
    assert row["last_active"] == 3
    assert result["total"] == 1
    assert result["has_more"] is False


def test_branch_parent_normalizes_through_later_parent_compression(db):
    _seed(db, "parent-root", started_at=1)
    _compress(db, "parent-root", "parent-tip-1", started_at=2)
    _seed(
        db,
        "branch",
        parent="parent-tip-1",
        branch_from="parent-tip-1",
        started_at=3,
    )
    _compress(db, "parent-tip-1", "parent-tip-2", started_at=4)

    rows = _by_id(_list())

    assert rows["parent-tip-2"]["_lineage_root_id"] == "parent-root"
    assert rows["branch"]["parent_session_id"] == "parent-tip-1"
    assert rows["branch"]["branch_parent_root_id"] == "parent-root"


def test_compressed_branch_keeps_original_conversation_parent(db):
    _seed(db, "parent", started_at=1)
    _seed(
        db,
        "branch-root",
        parent="parent",
        branch_from="parent",
        started_at=2,
    )
    _compress(db, "branch-root", "branch-tip", started_at=3)

    rows = _by_id(_list())

    assert "branch-root" not in rows
    assert rows["branch-tip"]["_lineage_root_id"] == "branch-root"
    assert rows["branch-tip"]["parent_session_id"] == "parent"
    assert rows["branch-tip"]["branch_parent_root_id"] == "parent"


def test_deleted_parent_leaves_branch_visible_as_an_orphan(db):
    _seed(db, "parent", started_at=1)
    _seed(db, "branch", parent="parent", branch_from="parent", started_at=2)
    assert db.delete_session("parent") is True

    rows = _by_id(_list())

    assert set(rows) == {"branch"}
    assert "parent_session_id" not in rows["branch"]
    assert "branch_parent_root_id" not in rows["branch"]


def test_cycle_and_depth_exhaustion_omit_only_normalized_parent(db):
    _seed(db, "cycle-a", started_at=1)
    _seed(db, "cycle-b", started_at=2)
    db._conn.execute(
        "UPDATE sessions SET parent_session_id = CASE id "
        "WHEN 'cycle-a' THEN 'cycle-b' ELSE 'cycle-a' END, "
        "ended_at = started_at + 0.1, end_reason = 'compression' "
        "WHERE id IN ('cycle-a', 'cycle-b')"
    )
    db._conn.commit()
    _seed(
        db,
        "cycle-branch",
        parent="cycle-a",
        branch_from="cycle-a",
        started_at=3,
    )

    _seed(db, "deep-root", started_at=10)
    previous = "deep-root"
    for index in range(1, 102):
        child = f"deep-{index}"
        _compress(db, previous, child, started_at=10 + index)
        previous = child
    _seed(
        db,
        "deep-branch",
        parent=previous,
        branch_from=previous,
        started_at=200,
    )

    rows = _by_id(_list())

    assert rows["cycle-branch"]["parent_session_id"] == "cycle-a"
    assert "branch_parent_root_id" not in rows["cycle-branch"]
    assert rows["deep-branch"]["parent_session_id"] == previous
    assert "branch_parent_root_id" not in rows["deep-branch"]


def test_pagination_carries_off_page_parent_and_truthful_bounds(db):
    _seed(db, "parent", started_at=1)
    _seed(db, "branch", parent="parent", branch_from="parent", started_at=2)

    first = _list(limit=1, offset=0)
    assert [row["id"] for row in first["sessions"]] == ["branch"]
    assert first["sessions"][0]["branch_parent_root_id"] == "parent"
    assert first["total"] == 2
    assert first["has_more"] is True

    second = _list(limit=1, offset=1)
    assert [row["id"] for row in second["sessions"]] == ["parent"]
    assert second["total"] == 2
    assert second["has_more"] is False


def test_total_uses_the_same_hidden_visibility_as_the_page(db):
    _seed(db, "visible", started_at=1)
    _seed(db, "hidden", started_at=2)
    assert db.set_session_hidden("hidden", True) is True

    default = _list()
    owning = _list(include_hidden=True)

    assert set(_by_id(default)) == {"visible"}
    assert default["total"] == 1
    assert set(_by_id(owning)) == {"visible", "hidden"}
    assert owning["total"] == 2


def test_limit_is_clamped_and_internal_sources_do_not_inflate_total(db):
    for index in range(205):
        _seed(db, f"row-{index:03d}", started_at=index + 1)
    _seed(db, "internal-tool", source="tool", started_at=999)
    _seed(db, "internal-tool-uppercase", source="TOOL", started_at=996)
    _seed(db, "internal-kanban", source="kanban", started_at=998)

    result = _list(limit=999)

    assert len(result["sessions"]) == 200
    assert result["total"] == 205
    assert result["has_more"] is True
    assert not {"internal-tool", "internal-tool-uppercase", "internal-kanban"} & set(
        _by_id(result)
    )


def test_requested_profile_remains_the_database_authority(tmp_path, monkeypatch):
    launch = SessionDB(tmp_path / "launch.db")
    work = SessionDB(tmp_path / "work.db")
    _seed(launch, "launch-only")
    _seed(work, "work-parent")
    _seed(
        work,
        "work-branch",
        parent="work-parent",
        branch_from="work-parent",
        started_at=2,
    )

    def db_for_profile(profile):
        return (work, False) if profile == "work" else (launch, False)

    monkeypatch.setattr(srv, "_db_for_profile", db_for_profile)
    try:
        result = _list(profile="work")
        assert set(_by_id(result)) == {"work-parent", "work-branch"}
        assert _by_id(result)["work-branch"]["branch_parent_root_id"] == "work-parent"
    finally:
        launch.close()
        work.close()
