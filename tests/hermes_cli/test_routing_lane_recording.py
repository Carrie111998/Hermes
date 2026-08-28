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
import os

from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    (tmp_path / "home").mkdir()
    # This process IS the `coder` profile, which is what a dispatcher-spawned
    # worker actually is: `task_runs.profile` is the assignee, and the worker
    # runs as `hermes -p <assignee>`. Tests that need a DIFFERENT draining
    # profile override this to prove cross-profile routing.
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name", lambda: "coder")
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


def _seed_usage(tmp_path, rows):
    """Write `session_model_usage` rows in the real shipped column order."""
    import sqlite3

    conn = sqlite3.connect(tmp_path / "home" / "state.db")
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS session_model_usage (
                 session_id TEXT, model TEXT, billing_provider TEXT,
                 billing_base_url TEXT, billing_mode TEXT, task TEXT,
                 api_call_count INTEGER, input_tokens INTEGER,
                 output_tokens INTEGER, cache_read_tokens INTEGER,
                 cache_write_tokens INTEGER, reasoning_tokens INTEGER,
                 estimated_cost_usd REAL, actual_cost_usd REAL,
                 cost_status TEXT, cost_source TEXT,
                 first_seen INTEGER, last_seen INTEGER)"""
        )
        for r in rows:
            (sid, model, prov, base, mode, calls, inp, out, cr, cw, reas,
             est, act, status, source) = r
            conn.execute(
                "INSERT INTO session_model_usage VALUES "
                "(?,?,?,?,?,'',?,?,?,?,?,?,?,?,?,?,1,2)",
                (sid, model, prov, base, mode, calls, inp, out, cr, cw, reas,
                 est, act, status, source),
            )
        conn.commit()
    finally:
        conn.close()


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
        # The record was STAGED inside the run's transaction; projecting it is
        # a separate step that can only happen after that commit.
        assert kb.project_routing_outbox(conn) == 1
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
        assert kb.project_routing_outbox(conn) == 0
    finally:
        conn.close()
    assert _routing_lines(tmp_path) == [], (
        "a task with no lane is the unchanged default path"
    )


def test_the_usage_join_is_read_not_recomputed(board, tmp_path):
    """When state.db carries usage for the session, it is joined verbatim."""
    import sqlite3

    from hermes_cli import routing_audit

    _seed_usage(tmp_path, [
        ("sess-j", "m1", "p1", "https://a", "subscription",
         3, 10, 20, 5, 7, 1, 0.0, 0.0, "included", "provider"),
        ("sess-j", "m2", "p2", "https://b", "api",
         1, 1, 2, 0, 0, 0, 0.25, 0.30, "estimated", "pricing-table"),
    ])
    usage = routing_audit.usage_for_session("sess-j")
    assert usage["input_tokens"] == 11
    assert usage["output_tokens"] == 22
    assert usage["cache_write_tokens"] == 7
    assert usage["api_call_count"] == 4
    # Actual and estimated stay SEPARATE — they are different claims.
    assert usage["estimated_cost_usd"] == 0.25
    assert usage["actual_cost_usd"] == 0.30
    assert usage["cost_status"] == "mixed", "two statuses must not collapse"
    assert usage["cost_sources"] == ["pricing-table", "provider"]
    assert {r["model"] for r in usage["routes"]} == {"m1", "m2"}
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


# ---------------------------------------------------------------------------
# Second-round corrections
# ---------------------------------------------------------------------------

def _two_claimed_runs():
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="A", assignee="coder")
        b = kb.create_task(conn, title="B", assignee="coder")
        kb.claim_task(conn, a)
        kb.claim_task(conn, b)
        ra = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?",
                          (a,)).fetchone()["current_run_id"]
        rb = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?",
                          (b,)).fetchone()["current_run_id"]
    finally:
        conn.close()
    return a, ra, b, rb


def test_a_worker_cannot_stamp_a_sibling_run(board, monkeypatch):
    """Env vars are not proof of a relationship; the database is."""
    from tools import kanban_tools as kt

    a, _ra, _b, rb = _two_claimed_runs()
    kt._session_bound_run_ids.clear()
    monkeypatch.setenv("HERMES_KANBAN_TASK", a)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(rb))     # B's run
    monkeypatch.setenv("HERMES_SESSION_ID", "sess-attacker")

    assert kt.bind_worker_session_from_env() is False
    conn = kb.connect()
    try:
        rows = {r["id"]: r["session_id"] for r in conn.execute(
            "SELECT id, session_id FROM task_runs")}
    finally:
        conn.close()
    assert all(v is None for v in rows.values()), rows


def test_a_run_that_no_longer_belongs_to_the_task_is_refused(board):
    a, ra, _b, _rb = _two_claimed_runs()
    conn = kb.connect()
    try:
        # The run is superseded: the task points somewhere else now.
        conn.execute("UPDATE tasks SET current_run_id = NULL WHERE id = ?", (a,))
        conn.commit()
        assert kb.record_run_session_id(conn, ra, "sess", task_id=a) is False
        stored = conn.execute("SELECT session_id FROM task_runs WHERE id = ?",
                              (ra,)).fetchone()["session_id"]
    finally:
        conn.close()
    assert stored is None


def test_an_ended_run_is_refused(board):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder")
        kb.claim_task(conn, tid)
        run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?",
                              (tid,)).fetchone()["current_run_id"]
        kb.complete_task(conn, tid, summary="done")
        assert kb.record_run_session_id(conn, run_id, "late") is False
    finally:
        conn.close()


def test_the_session_key_is_set_once(board):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder")
        kb.claim_task(conn, tid)
        run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?",
                              (tid,)).fetchone()["current_run_id"]
        assert kb.record_run_session_id(conn, run_id, "session-one") is True
        # An identical retry is idempotent...
        assert kb.record_run_session_id(conn, run_id, "session-one") is True
        # ...but a DIFFERENT identity is refused without mutation.
        assert kb.record_run_session_id(conn, run_id, "session-two") is False
        stored = conn.execute("SELECT session_id FROM task_runs WHERE id = ?",
                              (run_id,)).fetchone()["session_id"]
    finally:
        conn.close()
    assert stored == "session-one"


def test_competing_binders_produce_exactly_one_winner(board):
    """Two processes race for the same run; one identity must win."""
    import threading

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder")
        kb.claim_task(conn, tid)
        run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?",
                              (tid,)).fetchone()["current_run_id"]
    finally:
        conn.close()

    results: dict = {}
    gate = threading.Barrier(4)

    def binder(name):
        own = kb.connect()
        try:
            gate.wait(timeout=10)
            results[name] = kb.record_run_session_id(own, run_id, name)
        finally:
            own.close()

    threads = [threading.Thread(target=binder, args=(f"sess-{i}",))
               for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    conn = kb.connect()
    try:
        stored = conn.execute("SELECT session_id FROM task_runs WHERE id = ?",
                              (run_id,)).fetchone()["session_id"]
    finally:
        conn.close()
    winners = [n for n, ok in results.items() if ok]
    assert winners == [stored], f"exactly one identity must win: {results}"


# --- the outbox -------------------------------------------------------------

def _staged(run_id=None):
    conn = kb.connect()
    try:
        if run_id is None:
            return [dict(r) for r in conn.execute(
                "SELECT run_id, projected_at FROM routing_outbox ORDER BY id")]
        return conn.execute(
            "SELECT projected_at FROM routing_outbox WHERE run_id = ?",
            (run_id,)).fetchone()
    finally:
        conn.close()


def _routed_claimed_task():
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder")
        kb.set_routing_lane(conn, tid, "coding_routine")
        kb.claim_task(conn, tid)
        run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?",
                              (tid,)).fetchone()["current_run_id"]
    finally:
        conn.close()
    return tid, run_id


def test_a_rolled_back_run_leaves_no_terminal_record(board, tmp_path, monkeypatch):
    """The record is staged in the run's transaction, so it shares its fate."""
    tid, _run_id = _routed_claimed_task()
    before = len(_routing_lines(tmp_path))

    real_append = kb._append_event

    def boom(conn, task_id, kind, payload=None, run_id=None):
        if kind == "completed":
            raise RuntimeError("injected failure after _end_run")
        return real_append(conn, task_id, kind, payload, run_id=run_id)

    monkeypatch.setattr(kb, "_append_event", boom)
    conn = kb.connect()
    try:
        with pytest.raises(RuntimeError):
            kb.complete_task(conn, tid, summary="x")
        monkeypatch.undo()
        row = conn.execute("SELECT status, current_run_id FROM tasks WHERE id = ?",
                           (tid,)).fetchone()
        # Nothing staged, so nothing can ever be projected.
        assert kb.project_routing_outbox(conn) == 0
    finally:
        conn.close()

    assert row["status"] == "running", "the run really did roll back"
    assert row["current_run_id"] is not None
    terminal = [ln for ln in _routing_lines(tmp_path)[before:] if ln.get("run_ref")]
    assert terminal == [], "the log claimed an outcome that never committed"


def test_a_committed_record_survives_a_log_outage_and_projects_once(
    board, tmp_path, monkeypatch
):
    from hermes_cli import routing_audit

    tid, run_id = _routed_claimed_task()
    conn = kb.connect()
    try:
        kb.record_run_session_id(conn, run_id, "sess-1")

        # The log is unavailable while the run ends.
        real_path = routing_audit.resolve_profile_log_owner

        def boom(*a, **k):
            raise OSError("log is gone")

        monkeypatch.setattr(routing_audit, "resolve_profile_log_owner", boom)
        kb.complete_task(conn, tid, summary="done")
        assert kb.project_routing_outbox(conn) == 0, "nothing could be written"
        assert _staged(run_id)["projected_at"] is None, "and nothing was claimed"

        # The log comes back. Restore only this attribute — monkeypatch.undo()
        # would also revert the board fixture's HERMES_KANBAN_DB.
        monkeypatch.setattr(routing_audit, "resolve_profile_log_owner", real_path)
        assert kb.project_routing_outbox(conn) == 1
        assert _staged(run_id)["projected_at"] is not None
        # Draining again is a no-op: exactly once.
        assert kb.project_routing_outbox(conn) == 0
    finally:
        conn.close()

    terminal = [ln for ln in _routing_lines(tmp_path) if ln.get("run_ref")]
    assert len(terminal) == 1, terminal
    assert terminal[0]["run_ref"] == f"task_run:{run_id}"
    assert terminal[0]["record_id"], "a record id makes a duplicate detectable"


def test_concurrent_projectors_write_each_record_once(board, tmp_path):
    import threading

    for _ in range(3):
        tid, _run = _routed_claimed_task()
        conn = kb.connect()
        try:
            kb.complete_task(conn, tid, summary="done")
        finally:
            conn.close()

    counts: list = []
    gate = threading.Barrier(4)

    def projector():
        own = kb.connect()
        try:
            gate.wait(timeout=10)
            counts.append(kb.project_routing_outbox(own))
        finally:
            own.close()

    threads = [threading.Thread(target=projector) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    terminal = [ln for ln in _routing_lines(tmp_path) if ln.get("run_ref")]
    refs = [ln["run_ref"] for ln in terminal]
    assert len(refs) == len(set(refs)) == 3, f"one line per run: {refs}"
    # `set_routing_lane` drains opportunistically, so some records may already
    # have been projected before the race began. What must hold is that no
    # racing projector duplicated one — checked by the uniqueness above — and
    # that nothing is left unprojected.
    assert all(r["projected_at"] is not None for r in _staged()), _staged()
    assert counts, "every projector returned a count"


def test_a_retried_terminal_path_stages_once(board):
    tid, run_id = _routed_claimed_task()
    conn = kb.connect()
    try:
        kb.complete_task(conn, tid, summary="done")
        # A second terminal call for the same run must not stage a duplicate.
        kb._stage_terminal_routing_record(conn, tid, run_id, "completed")
        conn.commit()
        staged = [r for r in _staged() if r["run_id"] == run_id]
    finally:
        conn.close()
    assert len(staged) == 1


# --- accounting -------------------------------------------------------------

def test_the_terminal_record_carries_the_recorded_accounting(board, tmp_path):
    _seed_usage(tmp_path, [
        ("sess-acct", "gpt-5.6-sol", "openai-codex", "https://a", "subscription",
         3, 100, 200, 50, 70, 10, 0.0, 0.0, "included", "provider"),
    ])
    tid, run_id = _routed_claimed_task()
    conn = kb.connect()
    try:
        kb.record_run_session_id(conn, run_id, "sess-acct")
        kb.complete_task(conn, tid, summary="done")
        assert kb.project_routing_outbox(conn) == 1
    finally:
        conn.close()
    rec = [ln for ln in _routing_lines(tmp_path) if ln.get("run_ref")][-1]
    assert rec["input_tokens"] == 100 and rec["cache_write_tokens"] == 70
    assert rec["estimated_cost_usd"] == 0.0
    assert rec["actual_cost_usd"] == 0.0
    assert rec["cost_status"] == "included", "the stored status, not 'joined'"
    assert rec["cost_sources"] == ["provider"]
    assert rec["routes"][0]["model"] == "gpt-5.6-sol"
    assert rec["routes"][0]["billing_provider"] == "openai-codex"


def test_costs_are_never_blended_or_relabelled(board, tmp_path):
    _seed_usage(tmp_path, [
        ("s", "m", "p", "u", "api", 1, 1, 1, 0, 0, 0, 1.50, 0.00,
         "estimated", "pricing-table"),
        ("s", "m2", "p", "u", "api", 1, 1, 1, 0, 0, 0, 0.00, 2.25,
         "actual", "provider"),
    ])
    from hermes_cli import routing_audit

    usage = routing_audit.usage_for_session("s")
    assert usage["estimated_cost_usd"] == 1.50
    assert usage["actual_cost_usd"] == 2.25
    assert usage["cost_status"] == "mixed"


def test_unavailable_accounting_says_so(board, tmp_path):
    tid, run_id = _routed_claimed_task()
    conn = kb.connect()
    try:
        kb.record_run_session_id(conn, run_id, "sess-nothing")
        kb.complete_task(conn, tid, summary="done")
        kb.project_routing_outbox(conn)
    finally:
        conn.close()
    rec = [ln for ln in _routing_lines(tmp_path) if ln.get("run_ref")][-1]
    assert rec["cost_status"] == "unavailable"
    assert "estimated_cost_usd" not in rec, "absent, not invented as zero"


# --- lane validation and redaction safety -----------------------------------

@pytest.mark.parametrize("lanes", [{}, [], "malformed", None])
def test_configured_routing_with_no_usable_lanes_refuses_everything(
    board, monkeypatch, lanes
):
    configured = sorted(lanes) if isinstance(lanes, (dict, list)) else []
    monkeypatch.setattr(
        "hermes_cli.kanban._configured_routing_lanes",
        lambda: (configured, True))
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="coder")
    finally:
        conn.close()
    code, _out, err = _run_cli(["kanban", "routing-lane", tid, "invented"])
    assert code == 2, "an empty configured set means no lane is valid"
    assert "not a configured lane" in err
    assert _lane_of(tid) is None


@pytest.mark.parametrize("payload", [
    "cycle", "deep", "throwing", "unserializable",
])
def test_hostile_values_never_raise_out_of_the_audit(board, tmp_path, payload):
    from hermes_cli import routing_audit

    if payload == "cycle":
        value: object = []
        value.append(value)              # type: ignore[attr-defined]
    elif payload == "deep":
        value = current = []
        for _ in range(200):
            nxt: list = []
            current.append(nxt)          # type: ignore[attr-defined]
            current = nxt
    elif payload == "throwing":
        class Boom:
            def __str__(self):
                raise RuntimeError("glpat-NOTAREALKEY-ABCDEFGHIJKLMNOP")
        value = Boom()
    else:
        value = object()

    ok = routing_audit.record_routing_decision(task_id="t", lane="x", bad=value)
    assert isinstance(ok, bool), "it must return, not raise"
    raw = (tmp_path / "home" / "logs" / "routing.jsonl")
    if raw.exists():
        assert "NOTAREALKEY" not in raw.read_text()


def test_mapping_keys_are_redacted_too(board, tmp_path):
    from hermes_cli import routing_audit

    secret = "glpat-NOTAREALKEY-ABCDEFGHIJKLMNOP"
    routing_audit.record_routing_decision(
        task_id="t", lane="x", meta={secret: "value"})
    raw = (tmp_path / "home" / "logs" / "routing.jsonl").read_text()
    assert "NOTAREALKEY" not in raw


# ---------------------------------------------------------------------------
# Third-round: the outbox must be drained by real production paths
# ---------------------------------------------------------------------------

def _pending():
    conn = kb.connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) n FROM routing_outbox "
            " WHERE projected_at IS NULL AND quarantined_at IS NULL"
        ).fetchone()["n"]
    finally:
        conn.close()


def _quarantined():
    conn = kb.connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT run_id, error, projected_at FROM routing_outbox "
            " WHERE quarantined_at IS NOT NULL")]
    finally:
        conn.close()


def _terminal(tmp_path, home=None):
    root = home or (tmp_path / "home")
    path = root / "logs" / "routing.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines()
            if x.strip() and json.loads(x).get("run_ref")]


def _routed_task(assignee="coder"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee=assignee)
        kb.set_routing_lane(conn, tid, "coding_routine")
        kb.claim_task(conn, tid)
        run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?",
                              (tid,)).fetchone()["current_run_id"]
    finally:
        conn.close()
    return tid, run_id


def test_the_cli_production_path_projects_the_record(board, tmp_path):
    """No helper is called by hand: `hermes kanban complete` is the surface."""
    tid, run_id = _routed_task()
    before = len(_terminal(tmp_path))
    code, _out, _err = _run_cli(["kanban", "complete", tid, "--summary", "done"])
    assert code == 0
    assert _pending() == 0
    after = _terminal(tmp_path)
    assert len(after) == before + 1
    assert after[-1]["run_ref"] == f"task_run:{run_id}"


def test_the_worker_tool_production_path_projects_the_record(
    board, tmp_path, monkeypatch
):
    import tools.kanban_tools as kt

    tid, run_id = _routed_task()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setattr(kt, "_is_dispatcher_owned_worker", lambda: True)
    before = len(_terminal(tmp_path))

    out = kt._handle_complete({"task_id": tid, "summary": "done"})
    assert '"ok":true' in out.replace(" ", "")
    assert _pending() == 0
    assert len(_terminal(tmp_path)) == before + 1


def test_a_dispatcher_tick_drains_what_a_previous_process_left(
    board, tmp_path, monkeypatch
):
    """A restart's first tick recovers committed records."""
    from hermes_cli import routing_audit

    tid, _run_id = _routed_task()
    real = routing_audit.resolve_profile_log_owner
    monkeypatch.setattr(routing_audit, "resolve_profile_log_owner",
                        lambda p: (None, routing_audit.OWNER_MISSING))
    conn = kb.connect()
    try:
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()
    # Unresolvable → quarantined, not projected. Restore and prove the tick
    # recovers a genuinely pending one instead.
    monkeypatch.setattr(routing_audit, "resolve_profile_log_owner", real)

    tid2, run2 = _routed_task()
    boom = {"n": 0}

    def flaky(profile):
        boom["n"] += 1
        if boom["n"] == 1:
            raise OSError("log gone")
        return real(profile)

    monkeypatch.setattr(routing_audit, "resolve_profile_log_owner", flaky)
    conn = kb.connect()
    try:
        kb.complete_task(conn, tid2, summary="done")
        assert kb.project_routing_outbox(conn) == 0
    finally:
        conn.close()
    assert _pending() == 1, "the record survived the outage"

    monkeypatch.setattr(routing_audit, "resolve_profile_log_owner", real)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _n: True)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None)
    finally:
        conn.close()
    assert _pending() == 0
    assert any(t["run_ref"] == f"task_run:{run2}" for t in _terminal(tmp_path))


def test_ordinary_activity_after_recovery_drains_exactly_once(
    board, tmp_path, monkeypatch
):
    from hermes_cli import routing_audit

    tid, run_id = _routed_task()
    real = routing_audit.resolve_profile_log_owner
    monkeypatch.setattr(routing_audit, "resolve_profile_log_owner",
                        lambda p: (_ for _ in ()).throw(OSError("gone")))
    conn = kb.connect()
    try:
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()
    assert _pending() == 1
    assert _terminal(tmp_path) == []

    monkeypatch.setattr(routing_audit, "resolve_profile_log_owner", real)
    for _ in range(3):                      # ordinary CLI activity, repeatedly
        _run_cli(["kanban", "stats"])
    records = [t for t in _terminal(tmp_path)
               if t["run_ref"] == f"task_run:{run_id}"]
    assert len(records) == 1, f"exactly once: {records}"
    assert _pending() == 0


# --- the record goes to its OWNER's log ------------------------------------

def test_a_record_is_written_to_its_owning_profiles_log(board, tmp_path, monkeypatch):
    """A default/PM process draining the shared board must not divert it."""
    from hermes_cli import routing_audit

    coder_home = tmp_path / "profiles" / "coder"
    (coder_home / "logs").mkdir(parents=True)
    monkeypatch.setattr(
        "hermes_cli.profiles._get_profiles_root", lambda: tmp_path / "profiles")
    monkeypatch.setattr(
        "hermes_cli.profiles._get_default_hermes_home", lambda: tmp_path / "home")
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name", lambda: "default")

    tid, run_id = _routed_task(assignee="coder")
    conn = kb.connect()
    try:
        kb.complete_task(conn, tid, summary="done")
        assert kb.project_routing_outbox(conn) == 1
    finally:
        conn.close()

    owner = _terminal(tmp_path, home=coder_home)
    drainer = _terminal(tmp_path)
    assert [t["run_ref"] for t in owner] == [f"task_run:{run_id}"]
    assert drainer == [], "the draining profile's log must stay untouched"
    assert routing_audit.resolve_profile_log_path("coder") == (
        coder_home / "logs" / "routing.jsonl")


def test_an_unresolvable_owner_is_quarantined_not_misrouted(board, tmp_path):
    from hermes_cli import routing_audit

    tid, run_id = _routed_task(assignee="ghost-profile")
    conn = kb.connect()
    try:
        kb.complete_task(conn, tid, summary="done")
        assert kb.project_routing_outbox(conn) == 0
    finally:
        conn.close()
    assert _terminal(tmp_path) == [], "never guess a destination"
    q = _quarantined()
    assert len(q) == 1 and q[0]["run_id"] == run_id
    assert q[0]["error"] == routing_audit.OWNER_MISSING
    assert q[0]["projected_at"] is None, "quarantine is not success"


def test_a_payload_cannot_choose_its_own_destination(board, tmp_path):
    """`_log_path` is internal; a stored payload must not smuggle one in."""
    from hermes_cli import routing_audit

    elsewhere = tmp_path / "elsewhere.jsonl"
    conn = kb.connect()
    try:
        conn.execute(
            "INSERT INTO routing_outbox (run_id, profile, payload, created_at) "
            "VALUES (?, ?, ?, 1)",
            (4242, None, json.dumps(
                {"task_id": "t", "lane": "x", "run_ref": "task_run:4242",
                 "_log_path": str(elsewhere)})),
        )
        conn.commit()
        kb.project_routing_outbox(conn)
    finally:
        conn.close()
    assert not elsewhere.exists(), "a payload redirected the write"
    assert len(_terminal(tmp_path)) == 1


# --- malformed evidence -----------------------------------------------------

def test_a_malformed_payload_is_quarantined_not_faked(board, tmp_path):
    conn = kb.connect()
    try:
        conn.execute(
            "INSERT INTO routing_outbox (run_id, profile, payload, created_at) "
            "VALUES (999, 'coder', '{not json', 1)")
        conn.commit()
        assert kb.project_routing_outbox(conn) == 0
    finally:
        conn.close()
    assert _terminal(tmp_path) == [], "no synthetic record may be written"
    q = _quarantined()
    assert len(q) == 1 and q[0]["run_id"] == 999
    assert q[0]["projected_at"] is None
    assert "unreadable" in q[0]["error"]


def test_a_malformed_row_does_not_block_later_valid_records(board, tmp_path):
    """Ordering policy: a quarantined row is skipped, not a barrier."""
    conn = kb.connect()
    try:
        conn.execute(
            "INSERT INTO routing_outbox (run_id, profile, payload, created_at) "
            "VALUES (999, 'coder', '{not json', 1)")
        conn.commit()
    finally:
        conn.close()

    tid, run_id = _routed_task()
    conn = kb.connect()
    try:
        kb.complete_task(conn, tid, summary="done")
        # The malformed row sorts FIRST by id and must not stop the valid one.
        assert kb.project_routing_outbox(conn) == 1
    finally:
        conn.close()
    assert [t["run_ref"] for t in _terminal(tmp_path)] == [f"task_run:{run_id}"]
    assert len(_quarantined()) == 1
    assert _pending() == 0, "neither row is left pending"


def test_a_payload_that_is_not_an_object_is_quarantined(board, tmp_path):
    conn = kb.connect()
    try:
        conn.execute(
            "INSERT INTO routing_outbox (run_id, profile, payload, created_at) "
            "VALUES (998, 'coder', '[1,2,3]', 1)")
        conn.commit()
        assert kb.project_routing_outbox(conn) == 0
    finally:
        conn.close()
    assert _terminal(tmp_path) == []
    assert len(_quarantined()) == 1


def test_a_log_outage_never_fails_the_completed_task(board, tmp_path, monkeypatch):
    from hermes_cli import routing_audit

    monkeypatch.setattr(routing_audit, "resolve_profile_log_owner",
                        lambda p: (_ for _ in ()).throw(OSError("gone")))
    tid, _run_id = _routed_task()
    conn = kb.connect()
    try:
        assert kb.complete_task(conn, tid, summary="done") is True
        row = conn.execute("SELECT status FROM tasks WHERE id = ?",
                           (tid,)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "done", "the task completed despite the log outage"


def test_the_record_id_stays_stable_for_duplicate_detection(board, tmp_path):
    tid, run_id = _routed_task()
    conn = kb.connect()
    try:
        kb.complete_task(conn, tid, summary="done")
        row_id = conn.execute(
            "SELECT id FROM routing_outbox WHERE run_id = ?", (run_id,)
        ).fetchone()["id"]
        kb.project_routing_outbox(conn)
    finally:
        conn.close()
    rec = _terminal(tmp_path)[-1]
    assert rec["record_id"] == row_id, (
        "the stable id is what makes the at-least-once crash window detectable"
    )


# ---------------------------------------------------------------------------
# Fourth round: owner containment, and a bounded pending query
# ---------------------------------------------------------------------------

@pytest.fixture
def profiles_root(tmp_path, monkeypatch):
    """A realistic root: `<root>/profiles/<name>`, with the worker as `coder`."""
    root = tmp_path / "root"
    profiles = root / "profiles"
    (profiles / "coder").mkdir(parents=True)
    (profiles / "reviewer").mkdir()
    monkeypatch.setattr("hermes_cli.profiles._get_profiles_root", lambda: profiles)
    monkeypatch.setattr("hermes_cli.profiles._get_default_hermes_home", lambda: root)
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name", lambda: "coder")
    monkeypatch.setenv("HERMES_HOME", str(profiles / "coder"))
    return SimpleNamespace(root=root, profiles=profiles, outside=tmp_path / "escape")


ESCAPES = [
    "../../escape", "..", ".", "../escape", "../../../etc",
    "coder/../../escape", "/etc/passwd", "C:\\Windows",
    "coder\\..\\escape", "%2e%2e/escape", "..%2F..%2Fescape",
    "coder/subdir", "a/b", "\\\\server\\share",
]


@pytest.mark.parametrize("name", ESCAPES)
def test_a_traversing_owner_never_resolves(profiles_root, name):
    from hermes_cli import routing_audit

    assert routing_audit.resolve_profile_log_path(name) is None, name


@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
def test_a_blank_owner_falls_back_to_this_process(profiles_root, name):
    from hermes_cli import routing_audit

    resolved = routing_audit.resolve_profile_log_path(name)
    assert resolved == profiles_root.profiles / "coder" / "logs" / "routing.jsonl"


@pytest.mark.parametrize("name", ["hermes", "test", "tmp", "root", "sudo"])
def test_reserved_owner_names_are_refused(profiles_root, name):
    from hermes_cli import routing_audit

    (profiles_root.profiles / name).mkdir(exist_ok=True)
    assert routing_audit.resolve_profile_log_path(name) is None


def test_a_legitimate_named_owner_resolves(profiles_root):
    from hermes_cli import routing_audit

    assert routing_audit.resolve_profile_log_path("reviewer") == (
        profiles_root.profiles / "reviewer" / "logs" / "routing.jsonl")


def test_the_default_owner_is_the_root_home_not_a_child(profiles_root):
    from hermes_cli import routing_audit

    resolved = routing_audit.resolve_profile_log_path("default")
    assert resolved == profiles_root.root / "logs" / "routing.jsonl"
    assert profiles_root.profiles not in resolved.parents


@pytest.mark.parametrize("given,canon", [
    ("Reviewer", "reviewer"), ("REVIEWER", "reviewer"),
    ("  reviewer  ", "reviewer"), ("Default", "default"),
])
def test_owner_names_are_canonicalised_before_resolution(profiles_root, given, canon):
    from hermes_cli import routing_audit

    resolved = routing_audit.resolve_profile_log_path(given)
    assert resolved is not None
    assert resolved.parent.parent.name == canon or canon == "default"


def test_a_missing_owner_directory_is_unresolvable(profiles_root):
    from hermes_cli import routing_audit

    assert routing_audit.resolve_profile_log_path("never-created") is None


def test_a_symlink_inside_the_root_is_allowed(profiles_root):
    from hermes_cli import routing_audit

    real = profiles_root.profiles / "real-target"
    real.mkdir()
    link = profiles_root.profiles / "linked"
    link.symlink_to(real, target_is_directory=True)
    resolved = routing_audit.resolve_profile_log_path("linked")
    assert resolved == real.resolve() / "logs" / "routing.jsonl"


def test_a_symlink_outside_the_root_fails_closed(profiles_root):
    from hermes_cli import routing_audit

    outside = profiles_root.outside
    outside.mkdir(parents=True)
    link = profiles_root.profiles / "escaped"
    link.symlink_to(outside, target_is_directory=True)
    assert routing_audit.resolve_profile_log_path("escaped") is None


def test_a_traversing_owner_is_quarantined_and_writes_nothing(
    profiles_root, monkeypatch, tmp_path
):
    """The end-to-end reproduction: nothing outside the root, nothing marked."""
    from hermes_cli import routing_audit

    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "esc.db"))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    outside = profiles_root.outside
    outside.mkdir(parents=True, exist_ok=True)

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="../../escape")
        kb.set_routing_lane(conn, tid, "coding_routine")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="done")
        assert kb.project_routing_outbox(conn) == 0
        row = conn.execute(
            "SELECT projected_at, quarantined_at, error FROM routing_outbox "
            "ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()

    assert not (outside / "logs" / "routing.jsonl").exists()
    assert row["projected_at"] is None
    assert row["quarantined_at"] is not None
    assert row["error"] == routing_audit.OWNER_INVALID


def test_the_quarantine_reason_is_bounded_and_printable(profiles_root, tmp_path,
                                                        monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "bounded.db"))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    hostile = "../" * 200 + "\x00\x1b[31m" + "A" * 500
    conn = kb.connect()
    try:
        conn.execute(
            "INSERT INTO routing_outbox (run_id, profile, payload, created_at) "
            "VALUES (1, ?, '{}', 1)", (hostile,))
        conn.commit()
        kb.project_routing_outbox(conn)
        err = conn.execute(
            "SELECT error FROM routing_outbox WHERE run_id = 1").fetchone()["error"]
    finally:
        conn.close()
    assert len(err) < 120, err
    assert "\x00" not in err and "\x1b" not in err
    assert "/logs/routing.jsonl" not in err, "no resolved local path"


def test_a_coder_record_is_not_cross_written_by_a_default_drainer(
    profiles_root, tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "cross.db"))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="reviewer")
        kb.set_routing_lane(conn, tid, "review_only")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="done")
        # A `default` process performs the recovery.
        monkeypatch.setattr(
            "hermes_cli.profiles.get_active_profile_name", lambda: "default")
        monkeypatch.setenv("HERMES_HOME", str(profiles_root.root))
        assert kb.project_routing_outbox(conn) == 1
    finally:
        conn.close()

    owner_log = profiles_root.profiles / "reviewer" / "logs" / "routing.jsonl"
    drainer_log = profiles_root.root / "logs" / "routing.jsonl"
    assert owner_log.exists(), "the owner's log must receive it"
    assert not drainer_log.exists(), "the drainer's log must stay untouched"


# --- the pending query stays bounded ---------------------------------------

PENDING_QUERY = (
    "SELECT id, run_id, profile, payload FROM routing_outbox "
    " WHERE projected_at IS NULL AND quarantined_at IS NULL "
    " ORDER BY id LIMIT 50"
)


def _plan(conn):
    return " | ".join(r["detail"] for r in
                      conn.execute("EXPLAIN QUERY PLAN " + PENDING_QUERY))


def test_a_fresh_board_has_the_pending_index(board):
    conn = kb.connect()
    try:
        names = {r["name"] for r in conn.execute("PRAGMA index_list(routing_outbox)")}
        plan = _plan(conn)
    finally:
        conn.close()
    assert "idx_routing_outbox_pending" in names
    assert "idx_routing_outbox_pending" in plan, plan


def test_a_legacy_board_gains_the_pending_index_on_open(board, tmp_path):
    """A board created before the columns existed must still get the index."""
    import sqlite3

    legacy = tmp_path / "legacy-outbox.db"
    os.environ["HERMES_KANBAN_DB"] = str(legacy)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    raw = sqlite3.connect(legacy)
    raw.executescript(
        "DROP INDEX IF EXISTS idx_routing_outbox_pending;"
        "ALTER TABLE routing_outbox DROP COLUMN quarantined_at;"
        "ALTER TABLE routing_outbox DROP COLUMN error;"
    )
    raw.commit()
    cols = {r[1] for r in raw.execute("PRAGMA table_info(routing_outbox)")}
    assert "quarantined_at" not in cols, "the fixture must really be legacy"
    raw.close()

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(routing_outbox)")}
        names = {r["name"] for r in conn.execute("PRAGMA index_list(routing_outbox)")}
        plan = _plan(conn)
    finally:
        conn.close()
    assert {"quarantined_at", "error"} <= cols
    assert "idx_routing_outbox_pending" in names
    assert "idx_routing_outbox_pending" in plan, plan


def test_history_is_preserved_and_the_tick_stays_bounded(board):
    """The index is partial, so lifetime history costs the tick nothing."""
    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            for i in range(5000):
                conn.execute(
                    "INSERT INTO routing_outbox "
                    "(run_id, profile, payload, created_at, projected_at) "
                    "VALUES (?, 'coder', '{}', 1, 1)", (i,))
            conn.execute(
                "INSERT INTO routing_outbox "
                "(run_id, profile, payload, created_at, quarantined_at) "
                "VALUES (90001, 'coder', '{}', 1, 1)")
            conn.execute(
                "INSERT INTO routing_outbox "
                "(run_id, profile, payload, created_at) "
                "VALUES (90002, 'coder', '{}', 1)")
        assert "idx_routing_outbox_pending" in _plan(conn)
        pending = conn.execute(PENDING_QUERY).fetchall()
        total = conn.execute("SELECT COUNT(*) c FROM routing_outbox").fetchone()["c"]
    finally:
        conn.close()
    assert [r["run_id"] for r in pending] == [90002], "only the pending row"
    assert total == 5002, "projected and quarantined history is preserved"


# ---------------------------------------------------------------------------
# Fifth round: a rejected owner never reaches a diagnostic sink
# ---------------------------------------------------------------------------

# Synthetic markers only — shaped like the real families, valid in none of them.
CREDENTIAL_SHAPED = [
    ("gitlab",      "glpat-NOTAREALKEY-ABCDEFGHIJKLMNOP"),
    ("stripe",      "sk_live_NOTAREAL0000ABCDEFGHIJKLMNOPQRSTUV"),
    ("openrouter",  "sk-or-v1-notarealnotarealnotarealnotarealnotareal00"),
    ("github",      "ghp_NOTAREAL0000ABCDEFGHIJKLMNOPQRSTUV"),
    ("github-pat",  "github_pat_NOTAREAL0000ABCDEFGHIJKLMNOPQRSTUV"),
    ("huggingface", "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"),
    ("anthropic",   "sk-ant-api03-notarealnotarealnotarealnotarealnotareal"),
    ("aws",         "AKIANOTAREAL00000000"),
    ("google",      "AIzaNotARealKeyNotARealKeyNotARealKey00"),
    ("slack",       "xoxb-000000000000-000000000000-notarealnotarealnotar"),
    ("generic",     "api_key_NOTAREAL0000ABCDEFGHIJKLMNOPQRSTUVWX"),
]

PATH_SHAPED = [
    ("traversal",     "../../Users/Rick/Secret Project"),
    ("absolute-unix", "/Users/Rick/Secret Project/.env"),
    ("absolute-etc",  "/etc/hermes/credentials.json"),
    ("windows",       "C:\\Users\\Rick\\Secret Project\\.env"),
    ("unc",           "\\\\fileserver\\Finance$\\payroll"),
    ("home",          "~/Documents/Client Names/acme-corp"),
]

CONTROL_SHAPED = [
    ("newline",     "coder\nCRITICAL zzforgedzz NOTAREAL"),
    ("crlf",        "coder\r\nFAKE"),
    ("ansi",        "coder\x1b[31mRED\x1b[0m"),
    ("bidi",        "coder\u202egnidaolyap\u202c"),
    ("zero-width",  "co\u200bder\u200d"),
    ("tab-null",    "coder\t\x00trailing"),
    ("oversized",   "z" * 20000),
    ("oversized-credential", "glpat-" + "N" * 9000),
]

_CODES = {
    "invalid_profile_owner", "missing_profile_owner",
    "escaped_profile_owner", "unresolvable_profile_owner",
}


def _quarantine_diagnostics(owner, caplog, run_id=4242):
    """Insert *owner*, project it, and return (db_error, log_text)."""
    import logging

    conn = kb.connect()
    try:
        conn.execute(
            "INSERT INTO routing_outbox (run_id, profile, payload, created_at) "
            "VALUES (?, ?, '{}', 1)", (run_id, owner))
        conn.commit()
        with caplog.at_level(logging.DEBUG):
            caplog.clear()
            written = kb.project_routing_outbox(conn)
        row = conn.execute(
            "SELECT error, projected_at, quarantined_at FROM routing_outbox "
            " WHERE run_id = ?", (run_id,)).fetchone()
    finally:
        conn.close()
    assert written == 0, "a rejected owner is never projected"
    assert row["projected_at"] is None
    assert row["quarantined_at"] is not None
    return row["error"], "\n".join(r.getMessage() for r in caplog.records)


def _assert_sink_is_safe(owner, error, log):
    """Neither sink may carry the value, and both stay bounded and printable."""
    assert error in _CODES, f"reason code only, got {error!r}"
    haystack = f"{error}\n{log}"
    # No run of the rejected value survives anywhere in either sink.
    for n in range(0, max(1, len(owner) - 7)):
        chunk = owner[n:n + 8]
        if chunk.strip():
            assert chunk not in haystack, f"leaked {chunk!r}"
    for line in log.splitlines():
        assert len(line) < 200, "log lines stay bounded"
        assert all(ch == " " or ch.isprintable() for ch in line), repr(line)


@pytest.mark.parametrize("family,owner", CREDENTIAL_SHAPED)
def test_a_credential_shaped_owner_never_reaches_a_sink(board, caplog, family, owner):
    error, log = _quarantine_diagnostics(owner, caplog)
    _assert_sink_is_safe(owner, error, log)


@pytest.mark.parametrize("kind,owner", PATH_SHAPED)
def test_a_local_path_owner_never_reaches_a_sink(board, caplog, kind, owner):
    error, log = _quarantine_diagnostics(owner, caplog)
    _assert_sink_is_safe(owner, error, log)
    assert "Rick" not in f"{error}{log}" and "Secret" not in f"{error}{log}"


@pytest.mark.parametrize("kind,owner", CONTROL_SHAPED)
def test_a_control_character_owner_cannot_forge_a_log_line(board, caplog, kind, owner):
    error, log = _quarantine_diagnostics(owner, caplog)
    _assert_sink_is_safe(owner, error, log)
    assert "zzforgedzz" not in log, "a newline cannot inject a second line"
    assert len(error) < 64, "the stored reason stays small"


def test_a_lone_surrogate_owner_is_rejected_without_stringifying(board):
    """SQLite cannot store one, so the boundary is the resolver itself."""
    from hermes_cli import routing_audit

    path, reason = routing_audit.resolve_profile_log_owner("\ud800coder")
    assert path is None and reason in _CODES


def test_an_owner_whose_string_conversion_raises_is_rejected(board):
    from hermes_cli import routing_audit

    class Boom:
        def __str__(self):
            raise RuntimeError("never stringify untrusted input")

        __repr__ = __str__

    path, reason = routing_audit.resolve_profile_log_owner(Boom())
    assert path is None
    assert reason == routing_audit.OWNER_INVALID


def test_diagnostics_stay_safe_when_the_redactor_raises(board, caplog, monkeypatch):
    """Safety must not depend on the redactor: the value is simply never used."""
    import agent.redact

    monkeypatch.setattr(
        agent.redact, "redact_sensitive_text",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("redactor down")))
    owner = "glpat-NOTAREALKEY-ABCDEFGHIJKLMNOP"
    error, log = _quarantine_diagnostics(owner, caplog)
    _assert_sink_is_safe(owner, error, log)


def test_diagnostics_stay_safe_when_the_redactor_is_unavailable(
    board, caplog, monkeypatch
):
    import sys

    monkeypatch.setitem(sys.modules, "agent.redact", None)
    owner = "sk-ant-api03-notarealnotarealnotarealnotarealnotareal"
    error, log = _quarantine_diagnostics(owner, caplog)
    _assert_sink_is_safe(owner, error, log)


def test_the_rejected_value_is_still_recoverable_from_its_own_row(board, caplog):
    """Diagnostics drop the value; the record keeps it, which is where it belongs."""
    owner = "glpat-NOTAREALKEY-ABCDEFGHIJKLMNOP"
    _quarantine_diagnostics(owner, caplog, run_id=555)
    conn = kb.connect()
    try:
        row = conn.execute(
            "SELECT profile, error FROM routing_outbox WHERE run_id = 555").fetchone()
    finally:
        conn.close()
    assert row["profile"] == owner, "the row still identifies its own owner"
    assert row["error"] == "missing_profile_owner"


# --- the reason code is specific, not a single catch-all --------------------

def test_each_rejection_gets_its_own_reason_code(profiles_root):
    from hermes_cli import routing_audit as ra

    outside = profiles_root.outside
    outside.mkdir(parents=True, exist_ok=True)
    (profiles_root.profiles / "escaped").symlink_to(outside, target_is_directory=True)

    cases = {
        "../../escape": ra.OWNER_INVALID,       # rejected by validation
        "hermes": ra.OWNER_INVALID,             # reserved
        "never-created": ra.OWNER_MISSING,      # valid name, no directory
        "escaped": ra.OWNER_ESCAPED,            # symlink out of the root
    }
    for owner, expected in cases.items():
        path, reason = ra.resolve_profile_log_owner(owner)
        assert path is None, owner
        assert reason == expected, f"{owner}: {reason}"


def test_resolution_of_valid_owners_is_unchanged(profiles_root):
    from hermes_cli import routing_audit as ra

    assert ra.resolve_profile_log_owner("reviewer") == (
        profiles_root.profiles / "reviewer" / "logs" / "routing.jsonl", None)
    assert ra.resolve_profile_log_owner("REVIEWER ")[0] == (
        profiles_root.profiles / "reviewer" / "logs" / "routing.jsonl")
    assert ra.resolve_profile_log_owner("default") == (
        profiles_root.root / "logs" / "routing.jsonl", None)
    assert ra.resolve_profile_log_owner(None)[1] is None, "a legacy row still resolves"
    assert ra.resolve_profile_log_owner("  ")[1] is None


# ---------------------------------------------------------------------------
# Sixth round: a malformed identifier cannot wedge the queue
# ---------------------------------------------------------------------------

DIAG_ACCEPTED = [
    ("positive", 1), ("large", 2 ** 63 - 1), ("zero", 0),
    ("negative", -7), ("most-negative", -(2 ** 63)),
]

DIAG_REJECTED = [
    ("true", True), ("false", False),
    ("float-integral", 1.0), ("float", 3.5), ("float-negative", -0.0),
    ("nan", float("nan")),
    ("inf", float("inf")), ("-inf", float("-inf")),
    ("text-numeric", "12"), ("text", "coder"),
    ("text-newline", "1\nCRITICAL zzforgedzz"),
    ("text-ansi", "1\x1b[31m"),
    ("text-percent", "%s %d %(x)s"),
    ("text-credential", "glpat-NOTAREALKEY-ABCDEFGHIJKLMNOP"),
    ("text-huge-number", "9" * 400),
    ("blob", b"\x00\x01binary"),
    ("null", None),
    ("int-too-large", 2 ** 63), ("int-too-small", -(2 ** 63) - 1),
    ("huge-int", 10 ** 400),
]


@pytest.mark.parametrize("kind,value", DIAG_ACCEPTED)
def test_a_real_identifier_is_shown_as_itself(kind, value):
    assert kb._diag_id(value) == value


@pytest.mark.parametrize("kind,value", DIAG_REJECTED)
def test_any_other_scalar_becomes_the_fixed_marker(kind, value):
    assert kb._diag_id(value) == "?", kind


def test_no_scalar_makes_the_identifier_helper_raise():
    """It is total: SQLite can return exactly these five Python types."""
    for value in (None, 1, 1.5, float("inf"), float("nan"), "x", b"y",
                  True, 10 ** 400, -(10 ** 400)):
        assert kb._diag_id(value) in ("?", value)


def test_an_int_subclass_is_not_trusted_to_render_itself():
    class Sneaky(int):
        def __str__(self):
            raise RuntimeError("formatting hook")

        __repr__ = __str__

    assert kb._diag_id(Sneaky(5)) == "?"


def _poisoned_board(caplog):
    """Row 1 carries run_id +inf; row 2 is an ordinary quarantinable row."""
    import logging

    conn = kb.connect()
    try:
        conn.execute(
            "INSERT INTO routing_outbox (id, run_id, profile, payload, created_at)"
            " VALUES (1, ?, 'missing-owner', '{\"a\": 1}', 1)", (float("inf"),))
        conn.execute(
            "INSERT INTO routing_outbox (id, run_id, profile, payload, created_at)"
            " VALUES (2, 2, 'also-missing', '{\"a\": 1}', 1)")
        conn.commit()
        stored = conn.execute(
            "SELECT typeof(run_id) t FROM routing_outbox WHERE id = 1").fetchone()["t"]
        assert stored == "real", "the fixture must really store a non-integer"
        with caplog.at_level(logging.DEBUG):
            caplog.clear()
            written = kb.project_routing_outbox(conn)
        rows = conn.execute(
            "SELECT id, error, quarantined_at, projected_at FROM routing_outbox "
            " ORDER BY id").fetchall()
        pending = conn.execute(
            "SELECT COUNT(*) c FROM routing_outbox "
            " WHERE projected_at IS NULL AND quarantined_at IS NULL").fetchone()["c"]
    finally:
        conn.close()
    return written, rows, pending, "\n".join(r.getMessage() for r in caplog.records)


def test_a_poisoned_identifier_does_not_wedge_the_queue(board, caplog, tmp_path):
    written, rows, pending, log = _poisoned_board(caplog)

    assert written == 0
    assert pending == 0, "one pass disposes of both rows"
    for row in rows:
        assert row["projected_at"] is None
        assert row["quarantined_at"] is not None
        assert row["error"] == "missing_profile_owner"
    assert "quarantined record 1 (run ?)" in log, log
    assert "quarantined record 2 (run 2)" in log, log
    assert _terminal(tmp_path) == [], "a rejected owner still writes no JSONL"


def test_a_poisoned_identifier_leaves_nothing_hostile_in_any_sink(board, caplog):
    _, rows, _, log = _poisoned_board(caplog)

    everything = log + "".join(str(r["error"]) for r in rows)
    for hostile in ("inf", "Infinity", "1e", "OverflowError"):
        assert hostile not in everything, hostile
    for line in log.splitlines():
        assert len(line) < 200 and all(ch == " " or ch.isprintable() for ch in line)


def test_a_broken_log_handler_cannot_undo_a_quarantine(board, caplog, monkeypatch):
    """The UPDATE has already run; a logging failure must not roll it back."""
    import logging

    class Exploding(logging.Handler):
        def emit(self, record):
            raise RuntimeError("handler is broken")

    lg = logging.getLogger("hermes_cli.kanban_db")
    handler = Exploding()
    lg.addHandler(handler)
    try:
        conn = kb.connect()
        try:
            conn.execute(
                "INSERT INTO routing_outbox (run_id, profile, payload, created_at)"
                " VALUES (9, 'missing-owner', '{}', 1)")
            conn.commit()
            assert kb.project_routing_outbox(conn) == 0
            row = conn.execute(
                "SELECT quarantined_at, error FROM routing_outbox "
                " WHERE run_id = 9").fetchone()
        finally:
            conn.close()
    finally:
        lg.removeHandler(handler)
    assert row["quarantined_at"] is not None, "the quarantine survived"
    assert row["error"] == "missing_profile_owner"


def test_a_malformed_payload_row_with_a_poisoned_id_also_disposes(board, caplog):
    """The other quarantine branch shares the same identifier boundary."""
    import logging

    conn = kb.connect()
    try:
        conn.execute(
            "INSERT INTO routing_outbox (id, run_id, profile, payload, created_at)"
            " VALUES (1, ?, 'coder', 'not json', 1)", (float("-inf"),))
        conn.commit()
        with caplog.at_level(logging.DEBUG):
            caplog.clear()
            kb.project_routing_outbox(conn)
        row = conn.execute(
            "SELECT quarantined_at, error FROM routing_outbox WHERE id = 1").fetchone()
    finally:
        conn.close()
    log = "\n".join(r.getMessage() for r in caplog.records)
    assert row["quarantined_at"] is not None
    assert "unreadable payload" in row["error"]
    assert "(run ?)" in log, log
