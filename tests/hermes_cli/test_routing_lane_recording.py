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


# ---------------------------------------------------------------------------
# The supported surface, the session bind, and the run-linked record
# ---------------------------------------------------------------------------

def _run_cli(argv):
    import argparse
    import contextlib
    import io

    from hermes_cli.kanban import build_parser, kanban_command

    root = argparse.ArgumentParser(prog="hermes")
    sub = root.add_subparsers(dest="command")
    build_parser(sub)
    args = root.parse_args(argv)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = kanban_command(args)
    return code, out.getvalue(), err.getvalue()


def _lane_of(task_id):
    conn = kb.connect()
    try:
        return conn.execute(
            "SELECT routing_lane FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()["routing_lane"]
    finally:
        conn.close()


def test_the_cli_surface_records_a_lane(board, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.kanban._configured_routing_lanes",
        lambda: (["coding_routine", "review_only"], True))
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder")
    finally:
        conn.close()
    code, out, _err = _run_cli(
        ["kanban", "routing-lane", tid, "coding_routine", "--selected-by", "Rick"])
    assert code == 0
    assert "coding_routine" in out
    assert _lane_of(tid) == "coding_routine"


def test_an_unconfigured_lane_is_refused_with_the_real_list(board, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.kanban._configured_routing_lanes",
        lambda: (["coding_routine", "review_only"], True))
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder")
    finally:
        conn.close()
    code, _out, err = _run_cli(["kanban", "routing-lane", tid, "made_up"])
    assert code == 2
    assert "not a configured lane" in err
    assert "coding_routine" in err and "review_only" in err
    assert _lane_of(tid) is None, "a refused lane must not be recorded"


def test_with_no_routing_config_the_lane_lands_with_a_visible_warning(
    board, monkeypatch
):
    monkeypatch.setattr(
        "hermes_cli.kanban._configured_routing_lanes", lambda: ([], False))
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder")
    finally:
        conn.close()
    code, _out, err = _run_cli(["kanban", "routing-lane", tid, "coding_routine"])
    assert code == 0
    assert "no `routing:` section" in err
    assert _lane_of(tid) == "coding_routine"


def test_the_cli_can_clear_a_lane(board, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.kanban._configured_routing_lanes",
        lambda: (["coding_routine"], True))
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder")
    finally:
        conn.close()
    _run_cli(["kanban", "routing-lane", tid, "coding_routine"])
    code, out, _err = _run_cli(["kanban", "routing-lane", tid, "--clear"])
    assert code == 0 and "Cleared" in out
    assert _lane_of(tid) is None


def test_the_cli_refuses_an_unknown_task(board, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.kanban._configured_routing_lanes", lambda: (["x"], True))
    code, _out, err = _run_cli(["kanban", "routing-lane", "t_nope", "x"])
    assert code == 1 and "no such task" in err


def test_the_cli_never_changes_task_state(board, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.kanban._configured_routing_lanes",
        lambda: (["coding_routine"], True))
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder")
        before = dict(conn.execute(
            "SELECT status, assignee, gate_state FROM tasks WHERE id = ?",
            (tid,)).fetchone())
    finally:
        conn.close()
    _run_cli(["kanban", "routing-lane", tid, "coding_routine"])
    conn = kb.connect()
    try:
        after = dict(conn.execute(
            "SELECT status, assignee, gate_state FROM tasks WHERE id = ?",
            (tid,)).fetchone())
    finally:
        conn.close()
    assert after == before


def test_the_worker_bind_stamps_its_own_run_only(board, monkeypatch):
    from tools import kanban_tools as kt

    conn = kb.connect()
    try:
        mine = kb.create_task(conn, title="mine", assignee="coder")
        other = kb.create_task(conn, title="other", assignee="coder")
        kb.claim_task(conn, mine)
        kb.claim_task(conn, other)
        my_run = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (mine,)
        ).fetchone()["current_run_id"]
        other_run = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (other,)
        ).fetchone()["current_run_id"]
    finally:
        conn.close()

    kt._session_bound_run_ids.clear()
    monkeypatch.setenv("HERMES_KANBAN_TASK", mine)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(my_run))
    monkeypatch.setenv("HERMES_SESSION_ID", "sess-mine")
    assert kt.bind_worker_session_from_env() is True

    conn = kb.connect()
    try:
        rows = {r["id"]: r["session_id"] for r in conn.execute(
            "SELECT id, session_id FROM task_runs")}
    finally:
        conn.close()
    assert rows[my_run] == "sess-mine"
    assert rows[other_run] is None, "a sibling run must never be stamped"


def test_the_bind_is_a_one_shot_and_retry_safe(board, monkeypatch):
    from tools import kanban_tools as kt

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder")
        kb.claim_task(conn, tid)
        run_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (tid,)
        ).fetchone()["current_run_id"]
    finally:
        conn.close()
    kt._session_bound_run_ids.clear()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

    # No session yet: it must NOT mark the run done, so the next tick retries.
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    assert kt.bind_worker_session_from_env() is False
    monkeypatch.setenv("HERMES_SESSION_ID", "sess-1")
    assert kt.bind_worker_session_from_env() is True
    assert kt.bind_worker_session_from_env() is False, "one shot per run"

    conn = kb.connect()
    try:
        stored = conn.execute(
            "SELECT session_id FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()["session_id"]
    finally:
        conn.close()
    assert stored == "sess-1"


def test_a_routed_run_writes_a_run_linked_terminal_record(board, tmp_path):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder")
        kb.set_routing_lane(conn, tid, "coding_routine", selected_by="Rick")
        kb.claim_task(conn, tid)
        run_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (tid,)
        ).fetchone()["current_run_id"]
        kb.record_run_session_id(conn, run_id, "sess-term")
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    lines = _routing_lines(tmp_path)
    terminal = [line for line in lines if line.get("run_ref")]
    assert terminal, f"no run-linked record was written: {lines}"
    rec = terminal[-1]
    assert rec["task_id"] == tid
    assert rec["run_ref"] == f"task_run:{run_id}"
    assert rec["session_id"] == "sess-term"
    assert rec["profile"] == "coder"
    assert rec["lane"] == "coding_routine"
    assert rec["selection"] == "manual"
    assert rec["outcome"] == "completed"
    assert rec["cost_status"] in ("joined", "unavailable")


def test_an_unrouted_run_writes_no_routing_record(board, tmp_path):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()
    assert _routing_lines(tmp_path) == [], (
        "a task with no lane is the unchanged default path"
    )


def test_the_usage_join_is_read_not_recomputed(board, tmp_path):
    """When state.db carries usage for the session, it is joined verbatim."""
    import sqlite3

    from hermes_cli import routing_audit

    state = tmp_path / "home" / "state.db"
    raw = sqlite3.connect(state)
    raw.executescript(
        """
        CREATE TABLE session_model_usage (
          session_id TEXT, task TEXT, model TEXT, billing_provider TEXT,
          input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
          cache_write_tokens INTEGER, reasoning_tokens INTEGER,
          api_call_count INTEGER, last_seen INTEGER);
        INSERT INTO session_model_usage VALUES
          ('sess-j','', 'm','p', 10, 20, 5, 0, 1, 3, 1),
          ('sess-j','', 'm','p',  1,  2, 0, 0, 0, 1, 2);
        """
    )
    raw.commit()
    raw.close()
    usage = routing_audit.usage_for_session("sess-j")
    assert usage == {
        "input_tokens": 11, "output_tokens": 22, "cache_read_tokens": 5,
        "reasoning_tokens": 1, "api_call_count": 4,
    }
    assert routing_audit.usage_for_session("sess-absent") is None
    assert routing_audit.usage_for_session("") is None


def test_a_credential_shaped_lane_never_reaches_the_log(board, tmp_path):
    secret = "glpat-NOTAREALKEY-ABCDEFGHIJKLMNOP"
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder")
        kb.set_routing_lane(conn, tid, secret, selected_by=secret)
    finally:
        conn.close()
    raw = (tmp_path / "home" / "logs" / "routing.jsonl").read_text()
    assert secret not in raw
    assert "NOTAREALKEY" not in raw


def test_credential_values_are_redacted_in_any_field(board, tmp_path):
    from hermes_cli import routing_audit

    secret = "glpat-NOTAREALKEY-ABCDEFGHIJKLMNOP"
    routing_audit.record_routing_decision(
        task_id="t_1", lane="coding_routine", note=secret,
        nested={"inner": secret}, listed=[secret],
    )
    raw = (tmp_path / "home" / "logs" / "routing.jsonl").read_text()
    assert "NOTAREALKEY" not in raw
    assert json.loads(raw.splitlines()[-1])["selection"] == "manual"


def test_selection_cannot_be_overridden_by_a_caller(board, tmp_path):
    from hermes_cli import routing_audit

    routing_audit.record_routing_decision(
        task_id="t_1", lane="x", selection="automatic")
    assert _routing_lines(tmp_path)[-1]["selection"] == "manual"
