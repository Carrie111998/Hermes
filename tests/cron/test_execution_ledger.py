"""Durable cron execution-ledger behavior."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _point_ledger(monkeypatch, tmp_path):
    import cron.executions as executions

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    return executions


def test_execution_transitions_are_durable(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)

    claimed = executions.create_execution("job-1", source="builtin")
    assert claimed["status"] == "claimed"
    assert claimed["claimed_at"]
    assert claimed["started_at"] is None
    assert claimed["finished_at"] is None

    running = executions.mark_execution_running(claimed["id"])
    assert running["status"] == "running"
    assert running["started_at"]

    completed = executions.finish_execution(claimed["id"], success=True)
    assert completed["status"] == "completed"
    assert completed["finished_at"]
    assert completed["error"] is None

    persisted = executions.list_executions(job_id="job-1")
    assert persisted == [completed]


def test_terminal_execution_cannot_be_rewritten(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("immutable", source="builtin")
    executions.mark_execution_running(record["id"])
    executions.finish_execution(record["id"], success=True)

    assert executions.finish_execution(
        record["id"], success=False, error="late writer"
    ) is None
    assert executions.latest_execution("immutable")["status"] == "completed"


def test_retention_bounds_terminal_history_but_preserves_inflight(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 3)
    inflight = executions.create_execution("live", source="builtin")
    executions.mark_execution_running(inflight["id"])
    for index in range(8):
        row = executions.create_execution(f"done-{index}", source="builtin")
        executions.finish_execution(row["id"], success=True)

    records = executions.list_executions(limit=100)
    assert len([row for row in records if row["status"] == "completed"]) == 3
    assert executions.latest_execution("live")["status"] == "running"


def test_retention_preserves_terminal_execution_with_pending_delivery(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 1)
    pending = executions.create_execution("pending-delivery", source="builtin")
    executions.finish_execution(pending["id"], success=True)
    executions.enqueue_delivery(
        pending["id"],
        job={"id": "pending-delivery"},
        content="result",
        targets=[{"platform": "telegram", "chat_id": "1"}],
    )
    for index in range(4):
        row = executions.create_execution(f"newer-{index}", source="builtin")
        executions.finish_execution(row["id"], success=True)

    assert executions.latest_execution("pending-delivery")["id"] == pending["id"]
    assert executions.get_delivery(pending["id"])["status"] == "pending"


def test_execution_ledger_path_follows_active_profile(monkeypatch, tmp_path):
    import cron.executions as executions

    profile = tmp_path / "profiles" / "ops"
    monkeypatch.setattr(executions, "get_hermes_home", lambda: profile)

    assert executions._current_executions_file() == profile / "cron" / "executions.db"


def test_corrupt_store_fails_closed_without_overwrite(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    executions.EXECUTIONS_FILE.parent.mkdir(parents=True)
    executions.EXECUTIONS_FILE.write_bytes(b"not a sqlite database")

    with __import__("pytest").raises(sqlite3.DatabaseError):
        executions.create_execution("new", source="builtin")
    assert executions.EXECUTIONS_FILE.read_bytes() == b"not a sqlite database"


def test_delivery_schema_additively_migrates_pre_hardening_database(
    monkeypatch, tmp_path
):
    executions = _point_ledger(monkeypatch, tmp_path)
    executions.EXECUTIONS_FILE.parent.mkdir(parents=True)
    conn = sqlite3.connect(executions.EXECUTIONS_FILE)
    conn.execute(
        """CREATE TABLE deliveries (
             execution_id TEXT PRIMARY KEY,
             job_id TEXT NOT NULL,
             job_json TEXT,
             content TEXT,
             targets_json TEXT NOT NULL,
             status TEXT NOT NULL,
             attempt_count INTEGER NOT NULL DEFAULT 0,
             next_attempt_at TEXT,
             last_error TEXT,
             created_at TEXT NOT NULL,
             updated_at TEXT NOT NULL
           )"""
    )
    conn.execute(
        """INSERT INTO deliveries
           (execution_id, job_id, job_json, content, targets_json, status,
            attempt_count, next_attempt_at, created_at, updated_at)
           VALUES ('legacy', 'job', '{"id":"job"}', 'result', '[]',
                   'retry_wait', 1, '2099-01-01T00:00:00+00:00', 'now', 'now')"""
    )
    conn.commit()
    conn.close()

    migrated = executions.get_delivery("legacy")

    assert migrated["status"] == "retry_wait"
    assert migrated["permanent_error"] is None
    assert migrated["terminal_reason"] is None
    assert migrated["claim_token"] is None


def test_cron_runs_cli_prints_execution_history(monkeypatch, tmp_path, capsys):
    executions = _point_ledger(monkeypatch, tmp_path)
    row = executions.create_execution("cli-job", source="builtin")
    executions.finish_execution(row["id"], success=False, error="boom")
    from hermes_cli.cron import cron_runs

    cron_runs("cli-job", limit=10)

    output = capsys.readouterr().out
    assert row["id"] in output
    assert "failed" in output
    assert "boom" in output


def test_quick_backup_includes_execution_ledger():
    from hermes_cli.backup import _QUICK_STATE_FILES

    assert "cron/executions.db" in _QUICK_STATE_FILES


def test_failed_execution_keeps_error(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)

    record = executions.create_execution("job-2", source="external")
    failed = executions.finish_execution(record["id"], success=False, error="provider exploded")

    assert failed["status"] == "failed"
    assert failed["error"] == "provider exploded"


def test_recovery_does_not_mark_live_process_execution_unknown(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("still-live", source="builtin")
    executions.mark_execution_running(record["id"])

    assert executions.recover_interrupted_executions() == 0
    assert executions.latest_execution("still-live")["status"] == "running"


def test_restart_marks_interrupted_execution_unknown_without_requeue(tmp_path):
    """Real temp-HERMES_HOME subprocess restart: in-flight is audit-only unknown."""
    home = tmp_path / "home"
    repo = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(repo)

    create = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cron.executions import create_execution, mark_execution_running; "
            "r=create_execution('restart-job', source='builtin'); "
            "mark_execution_running(r['id']); print(r['id'])",
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    execution_id = create.stdout.strip()

    recover = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from cron.executions import recover_interrupted_executions, list_executions; "
            "print(recover_interrupted_executions()); "
            "print(json.dumps(list_executions(job_id='restart-job'))) ",
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    lines = recover.stdout.strip().splitlines()
    assert lines[0] == "1"
    records = json.loads(lines[1])
    assert len(records) == 1
    assert records[0]["id"] == execution_id
    assert records[0]["status"] == "unknown"
    assert records[0]["finished_at"]
    assert "restart" in records[0]["error"].lower()
    # Recovery only classifies the old attempt. It must not manufacture a new
    # claimed record (which would imply an automatic retry).
    assert [r["status"] for r in records] == ["unknown"]


def test_delivery_retry_keeps_only_failed_targets(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    clock = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(executions, "_hermes_now", lambda: clock)
    execution = executions.create_execution("fanout", source="builtin")
    telegram = {"platform": "telegram", "chat_id": "1", "thread_id": None}
    discord = {"platform": "discord", "chat_id": "2", "thread_id": "3"}

    queued = executions.enqueue_delivery(
        execution["id"],
        job={"id": "fanout", "name": "Fan out", "deliver": "all"},
        content="exact agent result",
        targets=[telegram, discord],
    )
    assert queued["status"] == "pending"
    assert queued["targets"] == [telegram, discord]

    claimed = executions.claim_delivery(execution["id"])
    assert claimed["status"] == "delivering"
    assert claimed["attempt_count"] == 1

    waiting = executions.finish_delivery_attempt(
        execution["id"],
        claim_token=claimed["claim_token"],
        failed_targets=[discord],
        error="discord unavailable",
    )
    assert waiting["status"] == "retry_wait"
    assert waiting["targets"] == [discord]
    assert waiting["next_attempt_at"] == "2026-08-05T12:01:00+00:00"
    assert executions.list_due_deliveries() == []

    monkeypatch.setattr(
        executions,
        "_hermes_now",
        lambda: datetime(2026, 8, 5, 12, 1, tzinfo=timezone.utc),
    )
    assert [row["execution_id"] for row in executions.list_due_deliveries()] == [
        execution["id"]
    ]

    attempts = executions.list_delivery_attempts(execution["id"])
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "retry_wait"
    assert attempts[0]["targets"] == [telegram, discord]


def test_completed_delivery_discards_sensitive_payload(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    execution = executions.create_execution("private", source="builtin")
    executions.enqueue_delivery(
        execution["id"],
        job={"id": "private", "origin": {"user_id": "secret-user"}},
        content="private response",
        targets=[{"platform": "telegram", "chat_id": "1"}],
    )
    claimed = executions.claim_delivery(execution["id"])

    completed = executions.finish_delivery_attempt(
        execution["id"], claim_token=claimed["claim_token"], failed_targets=[]
    )

    assert completed["status"] == "delivered"
    assert completed["content"] is None
    assert completed["job"] is None
    assert completed["targets"] == []


def test_delivery_attempts_exhaust_with_bounded_backoff(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    target = {"platform": "telegram", "chat_id": "1"}
    current = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(executions, "_hermes_now", lambda: current)
    execution = executions.create_execution("bounded", source="builtin")
    executions.enqueue_delivery(
        execution["id"], job={"id": "bounded"}, content="payload", targets=[target]
    )

    expected_delays = [60, 120, 600]
    for delay in expected_delays:
        claimed = executions.claim_delivery(execution["id"])
        record = executions.finish_delivery_attempt(
            execution["id"],
            claim_token=claimed["claim_token"],
            failed_targets=[target],
            error="offline",
        )
        assert record["status"] == "retry_wait"
        current = datetime.fromisoformat(record["next_attempt_at"])

    claimed = executions.claim_delivery(execution["id"])
    exhausted = executions.finish_delivery_attempt(
        execution["id"],
        claim_token=claimed["claim_token"],
        failed_targets=[target],
        error="still offline",
    )
    assert exhausted["status"] == "exhausted"
    assert exhausted["attempt_count"] == 4
    assert exhausted["content"] is None
    assert exhausted["job"] is None
    assert len(executions.list_delivery_attempts(execution["id"])) == 4


def test_recovery_marks_inflight_delivery_unknown_without_retry(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    execution = executions.create_execution("ambiguous-delivery", source="builtin")
    executions.enqueue_delivery(
        execution["id"],
        job={"id": "ambiguous-delivery"},
        content="may already have been sent",
        targets=[{"platform": "telegram", "chat_id": "1"}],
    )
    executions.claim_delivery(execution["id"])
    monkeypatch.setattr(executions, "_PROCESS_ID", "replacement-process")
    monkeypatch.setattr(executions, "_owner_is_live", lambda *_args: False)

    assert executions.recover_interrupted_deliveries() == 1
    recovered = executions.get_delivery(execution["id"])
    assert recovered["status"] == "unknown"
    assert recovered["content"] is None
    assert executions.list_due_deliveries() == []
    assert executions.list_delivery_attempts(execution["id"])[0]["outcome"] == "unknown"


def test_generic_submit_failure_finishes_attempt_and_releases_guard(monkeypatch):
    import cron.scheduler as scheduler

    class BrokenPool:
        def submit(self, _callable):
            raise ValueError("executor rejected")

    finished = []
    monkeypatch.setattr(
        scheduler, "create_execution",
        lambda *_args, **_kwargs: {"id": "exec-submit-fail"},
    )
    monkeypatch.setattr(
        scheduler, "finish_execution",
        lambda execution_id, **kwargs: finished.append((execution_id, kwargs)),
    )
    monkeypatch.setattr(scheduler, "get_due_jobs", lambda: [{"id": "submit-fail"}])
    monkeypatch.setattr(scheduler, "advance_next_runs", lambda _ids: 0)
    monkeypatch.setattr(scheduler, "_get_parallel_pool", lambda _workers: BrokenPool())

    assert scheduler.tick(verbose=False, sync=False) == 0
    assert finished == [
        ("exec-submit-fail", {
            "success": False,
            "error": "Executor dispatch failed: executor rejected",
        })
    ]
    assert "submit-fail" not in scheduler.get_running_job_ids()


def test_run_one_job_records_running_then_terminal(monkeypatch):
    import cron.scheduler as scheduler

    events = []
    monkeypatch.setattr(
        scheduler,
        "mark_execution_running",
        lambda execution_id: events.append(("running", execution_id)),
        raising=False,
    )
    monkeypatch.setattr(
        scheduler,
        "finish_execution",
        lambda execution_id, **kwargs: events.append(("finish", execution_id, kwargs)),
        raising=False,
    )
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda job, *, defer_agent_teardown=None: (True, "output", "response", None),
    )
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_args: None)
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *_args, **_kwargs: None)

    assert scheduler.run_one_job({"id": "job-3", "execution_id": "exec-3"}) is True
    assert events[0] == ("running", "exec-3")
    assert events[-1][0:2] == ("finish", "exec-3")
    assert events[-1][2]["success"] is True


def test_provider_start_recovers_interrupted_records_before_tick(monkeypatch):
    import cron.scheduler_provider as provider

    events = []
    stop = __import__("threading").Event()
    stop.set()
    monkeypatch.setattr(
        "cron.executions.recover_interrupted_executions",
        lambda: events.append("recover") or 0,
        raising=False,
    )
    monkeypatch.setattr(
        "cron.executions.recover_interrupted_deliveries",
        lambda: events.append("recover-delivery") or 0,
        raising=False,
    )
    monkeypatch.setattr(
        "cron.scheduler.retry_due_deliveries",
        lambda **_kwargs: events.append("retry-delivery") or 0,
        raising=False,
    )
    monkeypatch.setattr("cron.jobs.record_ticker_heartbeat", lambda **_kwargs: events.append("heartbeat"))

    provider.InProcessCronScheduler().start(stop, interval=1)

    assert events[:3] == ["recover", "recover-delivery", "heartbeat"]


def test_external_provider_start_recovers_interrupted_records(monkeypatch):
    from plugins.cron_providers.chronos import ChronosCronScheduler

    provider = ChronosCronScheduler()
    provider._client = type("Client", (), {"arm": lambda self, **kwargs: None})()
    events = []
    monkeypatch.setattr(
        "cron.executions.recover_interrupted_executions",
        lambda: events.append("recover") or 0,
    )
    monkeypatch.setattr(
        "cron.executions.recover_interrupted_deliveries",
        lambda: events.append("recover-delivery") or 0,
    )
    monkeypatch.setattr(
        "cron.scheduler.retry_due_deliveries",
        lambda **_kwargs: events.append("retry-delivery") or 0,
    )
    monkeypatch.setattr(provider, "reconcile", lambda: events.append("reconcile"))

    provider.start(__import__("threading").Event())

    assert events == ["recover", "recover-delivery", "retry-delivery", "reconcile"]


class _TrackingConnection:
    """Delegates to a real sqlite3.Connection while recording close() calls.

    sqlite3.Connection is a static C type: it has no per-instance __dict__
    and its class methods can't be monkeypatched, so open/close tracking is
    done via a delegating wrapper returned in place of the real connection.
    """

    def __init__(self, real, closed_ids):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_closed_ids", closed_ids)

    def close(self):
        self._closed_ids.append(id(self._real))
        self._real.close()

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __setattr__(self, name, value):
        setattr(self._real, name, value)


def _count_open_connections(executions, monkeypatch):
    """Wrap sqlite3.connect to track open/close balance for the ledger module."""
    opened_ids = []
    closed_ids = []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened_ids.append(id(conn))
        return _TrackingConnection(conn, closed_ids)

    monkeypatch.setattr(executions.sqlite3, "connect", tracking_connect)
    return opened_ids, closed_ids


def test_ledger_operations_close_every_connection(monkeypatch, tmp_path):
    """Regression for #69567: every ledger call must close its connection
    deterministically instead of relying on garbage collection."""
    executions = _point_ledger(monkeypatch, tmp_path)
    opened, closed = _count_open_connections(executions, monkeypatch)

    record = executions.create_execution("leak-check", source="builtin")
    executions.mark_execution_running(record["id"])
    executions.finish_execution(record["id"], success=True)
    executions.list_executions(job_id="leak-check")
    executions.latest_executions(["leak-check"])
    executions.recover_interrupted_executions()

    assert len(opened) == 6
    assert len(closed) == 6
    assert set(opened) == set(closed)


def test_early_return_still_closes_connection(monkeypatch, tmp_path):
    """mark_execution_running returns None mid-block on a bad transition;
    the connection must still be closed rather than leaked."""
    executions = _point_ledger(monkeypatch, tmp_path)
    opened, closed = _count_open_connections(executions, monkeypatch)

    assert executions.mark_execution_running("does-not-exist") is None

    assert len(opened) == 1
    assert len(closed) == 1


def test_exception_during_operation_still_closes_connection(monkeypatch, tmp_path):
    """A failing statement inside the transaction must roll back and close,
    not leak the connection."""
    executions = _point_ledger(monkeypatch, tmp_path)
    opened, closed = _count_open_connections(executions, monkeypatch)

    with __import__("pytest").raises(sqlite3.IntegrityError):
        with executions._transaction() as conn:
            conn.execute(
                "INSERT INTO executions (id, job_id, source, process_id, pid, "
                "status, claimed_at) VALUES ('x', 'x', 'x', 'x', 1, 'bogus-status', 'now')"
            )

    assert len(opened) == 1
    assert len(closed) == 1


def test_schema_init_failure_still_closes_connection(monkeypatch, tmp_path):
    """If PRAGMA/DDL setup in _connect() fails after sqlite3.connect()
    succeeds, the partially-initialized connection must still be closed."""
    executions = _point_ledger(monkeypatch, tmp_path)
    opened_ids = []
    closed_ids = []
    real_connect = sqlite3.connect

    class _FailingSchemaConnection(_TrackingConnection):
        def execute(self, sql, *args, **kwargs):
            if "CREATE TABLE" in sql:
                raise sqlite3.OperationalError("simulated schema init failure")
            return self._real.execute(sql, *args, **kwargs)

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened_ids.append(id(conn))
        return _FailingSchemaConnection(conn, closed_ids)

    monkeypatch.setattr(executions.sqlite3, "connect", tracking_connect)

    with __import__("pytest").raises(sqlite3.OperationalError):
        executions.create_execution("init-fail", source="builtin")

    assert len(opened_ids) == 1
    assert len(closed_ids) == 1


def test_job_listing_exposes_latest_execution(monkeypatch, tmp_path):
    import cron.jobs as jobs

    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    executions = _point_ledger(monkeypatch, tmp_path)

    job = jobs.create_job(prompt="audit me", schedule="every 1h", name="audit")
    record = executions.create_execution(job["id"], source="builtin")
    executions.mark_execution_running(record["id"])

    listed = jobs.list_jobs(include_disabled=True)
    assert listed[0]["latest_execution"]["id"] == record["id"]
    assert listed[0]["latest_execution"]["status"] == "running"
