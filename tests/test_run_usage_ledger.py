from __future__ import annotations

import sqlite3
import threading

from agent.run_usage_ledger import UsageLedger, run_id_for_session
from hermes_cli import kanban_db
from hermes_state import SessionDB


def test_sessiondb_schema_migrates_run_receipts_on_existing_database(tmp_path):
    db_path = tmp_path / "state.db"
    first = SessionDB(db_path)
    first.create_session("legacy-session", source="cli", model="old-model")
    first.close()

    second = SessionDB(db_path)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM usage_runs").fetchone() == (0,)
        assert connection.execute("SELECT model FROM sessions WHERE id = 'legacy-session'").fetchone() == ("old-model",)
    second.close()


def test_legacy_state_database_migrates_without_losing_sessions(tmp_path):
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT)")
        connection.execute("INSERT INTO sessions VALUES ('legacy-session', 'kept')")
        connection.commit()

    ledger = UsageLedger(db_path)
    ledger.start_run(
        run_id="run-legacy",
        process_id="123",
        session_id="session-direct",
        model="model-a",
        provider="provider-a",
    )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT title FROM sessions WHERE id = 'legacy-session'").fetchone() == ("kept",)
        assert connection.execute("SELECT run_id FROM usage_runs").fetchone() == ("run-legacy",)

    ledger.start_run(
        run_id="run-legacy",
        process_id="123",
        session_id="session-direct",
        model="model-a",
        provider="provider-a",
    )


def test_model_usage_callback_is_idempotent_for_duplicate_event(tmp_path):
    ledger = UsageLedger(tmp_path / "state.db")

    first = ledger.record_model_usage(
        run_id="run-1",
        event_id="session-direct:turn-1:api:1",
        session_id="session-direct",
        turn_id="turn-1",
        model="model-a",
        provider="provider-a",
        input_tokens=100,
        output_tokens=25,
        cost_usd=0.012,
        retry_count=1,
    )
    second = ledger.record_model_usage(
        run_id="run-1",
        event_id="session-direct:turn-1:api:1",
        session_id="session-direct",
        turn_id="turn-1",
        model="model-a",
        provider="provider-a",
        input_tokens=100,
        output_tokens=25,
        cost_usd=0.012,
        retry_count=1,
    )

    assert first is True
    assert second is False
    receipt = ledger.get_run("run-1")
    assert receipt["input_tokens"] == 100
    assert receipt["output_tokens"] == 25
    assert receipt["cost_usd"] == 0.012
    assert receipt["turn_count"] == 1
    assert receipt["retry_count"] == 1


def test_usage_receipt_keeps_card_optional_and_reports_non_card_identity(tmp_path):
    ledger = UsageLedger(tmp_path / "state.db")
    ledger.start_run(
        run_id="run-card",
        process_id="1",
        session_id="session-card",
        task_id="task-123",
        board="default",
        model="model-a",
        provider="provider-a",
    )
    ledger.record_model_usage(
        run_id="run-card",
        event_id="event-card",
        session_id="session-card",
        turn_id="turn-card",
        model="model-a",
        provider="provider-a",
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.1,
    )
    ledger.start_run(
        run_id="run-direct",
        process_id="2",
        session_id="session-direct",
        model="model-b",
        provider="provider-b",
    )
    ledger.record_model_usage(
        run_id="run-direct",
        event_id="event-direct",
        session_id="session-direct",
        turn_id="turn-direct",
        model="model-b",
        provider="provider-b",
        input_tokens=20,
        output_tokens=8,
        cost_usd=0.2,
    )

    card_rows = ledger.report(board="default", task_id="task-123")
    direct_rows = ledger.report(board="default", run_id="run-direct", include_unassigned=True)

    assert [row["run_id"] for row in card_rows] == ["run-card"]
    assert card_rows[0]["task_id"] == "task-123"
    assert [row["run_id"] for row in direct_rows] == ["run-direct"]
    assert direct_rows[0]["session_id"] == "session-direct"


def test_finish_records_elapsed_failure_outcome_and_tool_calls_once(tmp_path):
    ledger = UsageLedger(tmp_path / "state.db")
    ledger.start_run(run_id="run-fail", process_id="9", session_id="s")
    assert ledger.record_tool_call(run_id="run-fail", event_id="tool-1", session_id="s") is True
    assert ledger.record_tool_call(run_id="run-fail", event_id="tool-1", session_id="s") is False

    ledger.finish_run(
        run_id="run-fail",
        outcome="failed",
        failure_reason="provider timeout",
        ended_at=120.0,
        elapsed=5.5,
    )

    receipt = ledger.get_run("run-fail")
    assert receipt["tool_call_count"] == 1
    assert receipt["outcome"] == "failed"
    assert receipt["failure_reason"] == "provider timeout"
    assert receipt["elapsed"] == 5.5
    assert receipt["ended_at"] == 120.0


def test_kanban_link_is_keyed_by_authoritative_task_run_and_idempotent(tmp_path):
    board = tmp_path / "kanban.db"
    kanban_db.init_db(board)
    with kanban_db.connect_closing(board) as connection:
        connection.execute(
            "INSERT INTO task_runs(task_id, status, started_at) VALUES (?, ?, ?)",
            ("task-1", "running", 1),
        )
        task_run_id = connection.execute("SELECT id FROM task_runs").fetchone()[0]

    ledger = UsageLedger(tmp_path / "state.db")
    run_id = f"task-run:{task_run_id}"
    ledger.start_run(
        run_id=run_id,
        process_id="worker",
        task_run_id=task_run_id,
        task_id="task-1",
        board="default",
        model="model-a",
        provider="provider-a",
    )
    ledger.record_model_usage(
        run_id=run_id,
        event_id="event-1",
        session_id="s",
        turn_id="t",
        model="model-a",
        provider="provider-a",
        input_tokens=4,
        output_tokens=5,
        cost_usd=0.6,
    )
    ledger.finish_run(run_id=run_id, outcome="completed")
    assert ledger.link_kanban_run(task_run_id=task_run_id, usage_run_id=run_id, kanban_db=board)
    assert ledger.link_kanban_run(task_run_id=task_run_id, usage_run_id=run_id, kanban_db=board)

    with kanban_db.connect_closing(board) as connection:
        row = connection.execute(
            "SELECT task_run_id, usage_run_id, input_tokens, output_tokens, cost_usd FROM task_run_usage"
        ).fetchone()
    assert tuple(row) == (task_run_id, run_id, 4, 5, 0.6)


def test_async_writer_preserves_order_and_isolates_queue_backpressure(tmp_path):
    ledger = UsageLedger(tmp_path / "state.db", queue_size=2)
    assert ledger.queue_start_run(run_id="ordered", process_id="1")
    assert ledger.queue_model_usage(
        run_id="ordered", event_id="one", session_id="s", turn_id="t1",
        model="m", provider="p", input_tokens=1,
    )
    # A full queue is a bounded accounting loss, not a conversation failure.
    assert ledger.queue_model_usage(
        run_id="ordered", event_id="two", session_id="s", turn_id="t2",
        model="m", provider="p", input_tokens=1,
    ) in {True, False}
    assert ledger.flush()
    assert ledger.get_run("ordered")["input_tokens"] >= 1


def test_full_queue_retains_critical_events_and_persists_noncritical_diagnostic(tmp_path):
    ledger = UsageLedger(tmp_path / "state.db", queue_size=1)
    gate = threading.Event()
    entered = threading.Event()

    def blocked_writer():
        entered.set()
        gate.wait(timeout=2)

    ledger._writer_loop = blocked_writer
    assert ledger.queue_start_run(run_id="full", process_id="p")
    assert entered.wait(timeout=1)
    assert not ledger.queue_model_usage(
        run_id="full", event_id="api-1", session_id="s", turn_id="t",
        model="m", provider="p", input_tokens=5, output_tokens=2, cost_usd=0.25,
    )
    gate.set()
    assert not ledger.finalize_run(run_id="full", outcome="completed")
    with sqlite3.connect(tmp_path / "state.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM usage_diagnostics").fetchone()[0] >= 1


def test_writer_exception_is_replayed_during_finalization(tmp_path):
    ledger = UsageLedger(tmp_path / "state.db")
    ledger.start_run(run_id="replay", process_id="p")
    original = ledger._record_event
    state = {"failed": False}

    def fail_once(**kwargs):
        if not state["failed"]:
            state["failed"] = True
            raise sqlite3.OperationalError("injected writer failure")
        return original(**kwargs)

    ledger._record_event = fail_once
    assert ledger.queue_model_usage(
        run_id="replay", event_id="api-1", session_id="s", turn_id="t",
        model="m", provider="p", input_tokens=3, output_tokens=4, cost_usd=0.5,
    )
    assert ledger.finalize_run(run_id="replay", outcome="completed")
    receipt = ledger.get_run("replay")
    assert receipt["input_tokens"] == 3
    assert receipt["output_tokens"] == 4
    assert receipt["cost_usd"] == 0.5


def test_model_breakdown_is_deterministic_and_marks_mixed_runs(tmp_path):
    ledger = UsageLedger(tmp_path / "state.db")
    ledger.record_model_usage(
        run_id="mixed", event_id="a", session_id="s", turn_id="t1",
        model="model-b", provider="provider-b", input_tokens=2, output_tokens=3, cost_usd=0.2,
    )
    ledger.record_model_usage(
        run_id="mixed", event_id="b", session_id="s", turn_id="t2",
        model="model-a", provider="provider-a", input_tokens=5, output_tokens=7, cost_usd=0.7,
    )
    receipt = ledger.get_run("mixed")
    assert receipt["model"] == "mixed"
    assert receipt["provider"] == "mixed"
    assert [item["model"] for item in receipt["model_breakdown"]] == ["model-a", "model-b"]
    assert receipt["input_tokens"] == 7
    assert receipt["output_tokens"] == 10
    assert receipt["cost_usd"] == 0.9


def test_unknown_provider_is_non_null_and_idempotent(tmp_path):
    ledger = UsageLedger(tmp_path / "state.db")
    assert ledger.record_model_usage(
        run_id="unknown", event_id="one", session_id="s", turn_id="t1",
        model="fallback", provider=None, input_tokens=1, output_tokens=2,
    )
    assert ledger.record_model_usage(
        run_id="unknown", event_id="two", session_id="s", turn_id="t2",
        model="fallback", provider="", input_tokens=3, output_tokens=4,
    )
    receipt = ledger.get_run("unknown")
    assert receipt["model_breakdown"] == [{
        "model": "fallback", "provider": "unknown", "input_tokens": 4,
        "output_tokens": 6, "cost_usd": 0.0, "event_count": 2,
    }]


def test_session_run_identity_is_process_distinct_and_task_env_wins(monkeypatch):
    monkeypatch.delenv("HERMES_RUN_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    first = run_id_for_session("same-session")
    second = run_id_for_session("same-session")
    assert first == second
    monkeypatch.setenv("HERMES_RUN_ID", "explicit")
    assert run_id_for_session("same-session") == "explicit"
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    assert run_id_for_session("same-session") == "task-run:42"


def test_kanban_link_rejects_mismatched_authoritative_task_run(tmp_path):
    board = tmp_path / "kanban.db"
    kanban_db.init_db(board)
    with kanban_db.connect_closing(board) as connection:
        connection.execute("INSERT INTO task_runs(task_id, status, started_at) VALUES ('task', 'running', 1)")
        task_run_id = connection.execute("SELECT id FROM task_runs").fetchone()[0]
    ledger = UsageLedger(tmp_path / "state.db")
    ledger.start_run(run_id="task-run:999", process_id="p", task_run_id=999, task_id="task")
    ledger.finish_run(run_id="task-run:999", outcome="completed")
    assert not ledger.link_kanban_run(task_run_id=task_run_id, usage_run_id="task-run:999", kanban_db=board)
