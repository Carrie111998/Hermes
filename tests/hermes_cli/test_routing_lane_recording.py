"""Manual routing-lane recording, its audit line, and the cost-join key.

Commit 11. Three things, and deliberately only three:

* `tasks.routing_lane` — a lane is an explicit human or PM-agent choice
  recorded on the card. It is NEVER inferred from prompt text, and absent a
  lane the profile's own config decides exactly as it does today.
* `$HERMES_HOME/logs/routing.jsonl` — one line per decision, in the shape
  `dashboard_auth/audit.py` established: profile-aware path, redacting, and it
  never raises, because recording a decision must not be able to break the
  board.
* `task_runs.session_id` — the JOIN KEY to that profile's
  `state.db.session_model_usage`. Token and cost numbers are joined there,
  never recomputed here; this commit supplies the key, not the join.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    (tmp_path / "home").mkdir()
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return tmp_path


def _routing_lines(tmp_path):
    path = tmp_path / "home" / "logs" / "routing.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _events(task_id):
    conn = kb.connect()
    try:
        return [(r["kind"], r["payload"]) for r in conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
            (task_id,)).fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_the_new_columns_exist(board):
    conn = kb.connect()
    try:
        tasks = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        runs = {r[1] for r in conn.execute("PRAGMA table_info(task_runs)")}
    finally:
        conn.close()
    assert "routing_lane" in tasks
    assert "session_id" in runs


def test_a_legacy_board_gains_the_columns_on_open(board, tmp_path, monkeypatch):
    """Additive migration: an existing board must not need a rebuild.

    "Legacy" here is a real board with the two new columns removed, not a
    hand-rolled stub — a stub would exercise a migration path no shipped
    database has ever been in.
    """
    import sqlite3

    legacy = tmp_path / "legacy.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(legacy))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="pre-existing", assignee="coder")
    finally:
        conn.close()

    raw = sqlite3.connect(legacy)
    raw.execute("ALTER TABLE tasks DROP COLUMN routing_lane")
    raw.execute("ALTER TABLE task_runs DROP COLUMN session_id")
    raw.commit()
    cols = {r[1] for r in raw.execute("PRAGMA table_info(tasks)")}
    assert "routing_lane" not in cols, "the fixture must really be legacy"
    raw.close()

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tasks = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        runs = {r[1] for r in conn.execute("PRAGMA table_info(task_runs)")}
        survived = conn.execute(
            "SELECT title FROM tasks WHERE id = ?", (tid,)).fetchone()
    finally:
        conn.close()
    assert "routing_lane" in tasks
    assert "session_id" in runs
    assert survived["title"] == "pre-existing", "no rebuild, no data loss"


def test_both_columns_default_to_null(board):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder")
        row = conn.execute(
            "SELECT routing_lane FROM tasks WHERE id = ?", (tid,)).fetchone()
    finally:
        conn.close()
    assert row["routing_lane"] is None, "no lane is the unchanged default path"


# ---------------------------------------------------------------------------
# Manual lane recording
# ---------------------------------------------------------------------------

def test_setting_a_lane_records_it_and_emits_the_event(board, tmp_path):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder")
        assert kb.set_routing_lane(
            conn, tid, "coding_routine", selected_by="Rick") is True
        row = conn.execute(
            "SELECT routing_lane FROM tasks WHERE id = ?", (tid,)).fetchone()
    finally:
        conn.close()
    assert row["routing_lane"] == "coding_routine"
    kinds = [k for k, _p in _events(tid)]
    assert "routing_decided" in kinds
    payload = json.loads([p for k, p in _events(tid) if k == "routing_decided"][-1])
    assert payload["lane"] == "coding_routine"
    assert payload["selection"] == "manual"
    assert payload["selected_by"] == "Rick"


def test_the_decision_is_appended_to_routing_jsonl(board, tmp_path):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder")
        kb.set_routing_lane(conn, tid, "review_only", selected_by="Rick")
    finally:
        conn.close()
    lines = _routing_lines(tmp_path)
    assert len(lines) == 1
    line = lines[0]
    assert line["lane"] == "review_only"
    assert line["selection"] == "manual"
    assert line["task_id"] == tid
    assert line["ts"]
    assert line["event"] == "routing_decided"


def test_selection_is_always_manual(board, tmp_path):
    """M3b records decisions; it never infers them. No classifier exists."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder")
        kb.set_routing_lane(conn, tid, "coding_routine")
    finally:
        conn.close()
    assert _routing_lines(tmp_path)[0]["selection"] == "manual"


def test_changing_a_lane_records_a_second_decision(board, tmp_path):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder")
        kb.set_routing_lane(conn, tid, "coding_routine", selected_by="Rick")
        kb.set_routing_lane(conn, tid, "coding_high_risk", selected_by="Rick")
        row = conn.execute(
            "SELECT routing_lane FROM tasks WHERE id = ?", (tid,)).fetchone()
    finally:
        conn.close()
    assert row["routing_lane"] == "coding_high_risk"
    lines = _routing_lines(tmp_path)
    assert [line["lane"] for line in lines] == [
        "coding_routine", "coding_high_risk"]


def test_clearing_a_lane_returns_to_the_default_path(board):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder")
        kb.set_routing_lane(conn, tid, "coding_routine")
        assert kb.set_routing_lane(conn, tid, None) is True
        row = conn.execute(
            "SELECT routing_lane FROM tasks WHERE id = ?", (tid,)).fetchone()
    finally:
        conn.close()
    assert row["routing_lane"] is None


@pytest.mark.parametrize("lane", ["", "   ", "\t"])
def test_a_blank_lane_is_not_a_lane(board, lane):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder")
        kb.set_routing_lane(conn, tid, lane)
        row = conn.execute(
            "SELECT routing_lane FROM tasks WHERE id = ?", (tid,)).fetchone()
    finally:
        conn.close()
    assert row["routing_lane"] is None


def test_an_unknown_task_is_refused(board):
    conn = kb.connect()
    try:
        assert kb.set_routing_lane(conn, "t_nope", "coding_routine") is False
    finally:
        conn.close()


def test_recording_a_lane_changes_nothing_else(board):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder")
        before = dict(conn.execute(
            "SELECT status, assignee, gate_state, current_run_id "
            "FROM tasks WHERE id = ?", (tid,)).fetchone())
        kb.set_routing_lane(conn, tid, "coding_routine")
        after = dict(conn.execute(
            "SELECT status, assignee, gate_state, current_run_id "
            "FROM tasks WHERE id = ?", (tid,)).fetchone())
        runs = conn.execute(
            "SELECT COUNT(*) c FROM task_runs WHERE task_id = ?", (tid,)
        ).fetchone()["c"]
    finally:
        conn.close()
    assert after == before, "a lane is a label, not a scheduling decision"
    assert runs == 0


# ---------------------------------------------------------------------------
# The audit line never breaks the board
# ---------------------------------------------------------------------------

def test_an_unwritable_log_does_not_break_the_lane_write(board, monkeypatch):
    from hermes_cli import routing_audit

    def boom(*a, **k):
        raise OSError("disk is gone")

    monkeypatch.setattr(routing_audit, "_resolve_log_path", boom)
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder")
        assert kb.set_routing_lane(conn, tid, "coding_routine") is True
        row = conn.execute(
            "SELECT routing_lane FROM tasks WHERE id = ?", (tid,)).fetchone()
    finally:
        conn.close()
    assert row["routing_lane"] == "coding_routine"


def test_token_like_fields_are_dropped_from_the_record(board, tmp_path):
    from hermes_cli import routing_audit

    routing_audit.record_routing_decision(
        task_id="t_1", lane="coding_routine",
        access_token="SHOULD-NOT-APPEAR", authorization="ALSO-NOT",
        api_key="NOR-THIS", model="gpt-5.6-sol",
    )
    line = _routing_lines(tmp_path)[0]
    assert "SHOULD-NOT-APPEAR" not in json.dumps(line)
    assert "ALSO-NOT" not in json.dumps(line)
    assert "NOR-THIS" not in json.dumps(line)
    assert line["model"] == "gpt-5.6-sol"


def test_the_record_lands_under_the_active_hermes_home(board, tmp_path):
    from hermes_cli import routing_audit

    routing_audit.record_routing_decision(task_id="t_1", lane="review_only")
    assert (tmp_path / "home" / "logs" / "routing.jsonl").exists()


# ---------------------------------------------------------------------------
# The cost-join key
# ---------------------------------------------------------------------------

def test_a_run_can_record_the_session_that_produced_it(board):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder")
        kb.claim_task(conn, tid)
        run_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (tid,)
        ).fetchone()["current_run_id"]
        assert run_id is not None
        assert kb.record_run_session_id(conn, run_id, "sess-abc") is True
        row = conn.execute(
            "SELECT session_id, profile FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["session_id"] == "sess-abc"
    assert row["profile"], "profile + session_id is the join key pair"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_a_blank_session_id_is_not_recorded(board, value):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder")
        kb.claim_task(conn, tid)
        run_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (tid,)
        ).fetchone()["current_run_id"]
        assert kb.record_run_session_id(conn, run_id, value) is False
        row = conn.execute(
            "SELECT session_id FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    finally:
        conn.close()
    assert row["session_id"] is None


def test_an_unknown_run_is_refused(board):
    conn = kb.connect()
    try:
        assert kb.record_run_session_id(conn, 9999, "sess") is False
    finally:
        conn.close()


def test_recording_a_session_id_changes_no_run_state(board):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder")
        kb.claim_task(conn, tid)
        run_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (tid,)
        ).fetchone()["current_run_id"]
        before = dict(conn.execute(
            "SELECT status, outcome, ended_at FROM task_runs WHERE id = ?",
            (run_id,)).fetchone())
        kb.record_run_session_id(conn, run_id, "sess-abc")
        after = dict(conn.execute(
            "SELECT status, outcome, ended_at FROM task_runs WHERE id = ?",
            (run_id,)).fetchone())
    finally:
        conn.close()
    assert after == before
