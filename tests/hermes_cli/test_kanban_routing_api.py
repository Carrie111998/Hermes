"""Public run-routing API tests."""

import pytest

from hermes_cli import kanban_db as kb


def test_get_snapshot_and_atomic_override_modes(tmp_path):
    """Expose run snapshots and atomically switch explicit routing modes."""
    db = tmp_path / "kanban.db"
    kb.init_db(db)
    with kb.connect_closing(db) as conn:
        task_id = kb.create_task(conn, title="api", assignee="coder")
        kb.set_routing_override(conn, task_id, role="executor")
        row = conn.execute(
            "SELECT routing_role,model_override,provider_override FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        assert tuple(row) == ("executor", None, None)

        kb.set_routing_override(conn, task_id, model="m", provider="p")
        row = conn.execute(
            "SELECT routing_role,model_override,provider_override FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        assert tuple(row) == (None, "m", "p")

        run_id = conn.execute(
            "INSERT INTO task_runs (task_id,routing_role,routing_model,routing_provider,"
            "routing_source,started_at,status) VALUES (?,?,?,?,?,?,?)",
            (task_id, "executor", "m", "p", "task_role", 1, "completed"),
        ).lastrowid
        snapshot = kb.get_routing_snapshot(conn, run_id, board="unit-board")
        assert snapshot.run_id == run_id
        assert snapshot.task_id == task_id
        assert snapshot.board == "unit-board"
        assert snapshot.routing_source == "task_role"


def test_override_validation_and_snapshot_not_found(tmp_path):
    """Reject ambiguous override modes and unknown board-local runs."""
    db = tmp_path / "kanban.db"
    kb.init_db(db)
    with kb.connect_closing(db) as conn:
        task_id = kb.create_task(conn, title="guards")
        with pytest.raises(ValueError):
            kb.set_routing_override(conn, task_id)
        with pytest.raises(ValueError):
            kb.set_routing_override(conn, task_id, provider="p")
        with pytest.raises(ValueError):
            kb.set_routing_override(conn, task_id, role="r", model="m")
        with pytest.raises(KeyError):
            kb.get_routing_snapshot(conn, 999, board="unit-board")


def test_board_default_role_round_trip_preserves_other_metadata(tmp_path, monkeypatch):
    """Board role updates preserve unrelated board metadata fields."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.write_board_metadata("routing-board", name="Routing", description="keep")

    result = kb.write_board_metadata("routing-board", default_role="executor")

    assert result["default_role"] == "executor"
    assert result["name"] == "Routing"
    assert result["description"] == "keep"
    assert kb.read_board_metadata("routing-board")["default_role"] == "executor"
