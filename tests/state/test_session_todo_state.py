"""Durable, revisioned todo snapshots follow session lineage safely."""

from hermes_state import SessionDB


def _todo(item_id: str, status: str = "pending") -> list[dict[str, str]]:
    return [{"id": item_id, "content": f"Task {item_id}", "status": status}]


def test_snapshot_survives_database_reopen(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("session", source="test")
    db.save_session_todo_state("session", _todo("one"), 1)
    db.close()

    reopened = SessionDB(db_path=path)
    try:
        state = reopened.get_session_todo_state("session")
        assert state["todos"] == _todo("one")
        assert state["revision"] == 1
        assert state["owner_session_id"] == "session"
    finally:
        reopened.close()


def test_stale_revision_is_rejected(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("session", source="test")
        db.save_session_todo_state("session", _todo("current"), 3)

        result = db.save_session_todo_state("session", _todo("stale"), 2)

        assert result["revision"] == 3
        assert result["todos"] == _todo("current")
        assert db.get_session_todo_state("session")["todos"] == _todo("current")
    finally:
        db.close()


def test_child_inherits_then_diverges_copy_on_write(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("parent", source="test")
        db.create_session("child-a", source="test", parent_session_id="parent")
        db.create_session("child-b", source="test", parent_session_id="parent")
        db.save_session_todo_state("parent", _todo("parent"), 4)

        inherited = db.get_session_todo_state("child-a")
        assert inherited["todos"] == _todo("parent")
        assert inherited["owner_session_id"] == "parent"

        db.save_session_todo_state("child-a", _todo("branch"), 5)

        assert db.get_session_todo_state("child-a")["todos"] == _todo("branch")
        assert db.get_session_todo_state("parent")["todos"] == _todo("parent")
        assert db.get_session_todo_state("child-b")["todos"] == _todo("parent")
    finally:
        db.close()


def test_profiles_with_same_session_id_are_isolated(tmp_path):
    first = SessionDB(db_path=tmp_path / "first.db")
    second = SessionDB(db_path=tmp_path / "second.db")
    try:
        first.create_session("same", source="test")
        second.create_session("same", source="test")
        first.save_session_todo_state("same", _todo("first"), 1)
        second.save_session_todo_state("same", _todo("second"), 1)

        assert first.get_session_todo_state("same")["todos"] == _todo("first")
        assert second.get_session_todo_state("same")["todos"] == _todo("second")
    finally:
        first.close()
        second.close()
