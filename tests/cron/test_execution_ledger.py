"""Durable cron execution-ledger behavior."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def _point_ledger(monkeypatch, tmp_path):
    import cron.executions as executions

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    monkeypatch.setattr(executions, "_last_retention_error_by_ledger", {})
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


def test_execution_ledger_follows_the_current_profile_home(monkeypatch, tmp_path):
    import cron.executions as executions

    current_home = {"path": tmp_path / "default"}
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", None)
    monkeypatch.setattr(executions, "get_hermes_home", lambda: current_home["path"])

    default_row = executions.create_execution("default-job", source="builtin")
    current_home["path"] = tmp_path / "worker"
    worker_row = executions.create_execution("worker-job", source="builtin")

    assert executions.list_executions() == [worker_row]
    current_home["path"] = tmp_path / "default"
    assert executions.list_executions() == [default_row]
    assert (tmp_path / "default" / "cron" / "executions.db").is_file()
    assert (tmp_path / "worker" / "cron" / "executions.db").is_file()


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


def _write_retention_config(raw_value):
    """Write a real config.yaml into the isolated HERMES_HOME."""
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        f"cron:\n  max_terminal_executions: {raw_value}\n", encoding="utf-8"
    )


def test_resolve_max_terminal_executions_accepts_valid_values():
    from cron.executions import (
        DEFAULT_MAX_TERMINAL_EXECUTIONS,
        resolve_max_terminal_executions,
    )

    cases = [
        (None, DEFAULT_MAX_TERMINAL_EXECUTIONS),  # unset → shipped default
        (1, 1),
        (5000, 5000),
        ((1 << 63) - 1, (1 << 63) - 1),
        (str((1 << 63) - 1), (1 << 63) - 1),
        ("25", 25),
        ("  10  ", 10),
        (250.0, 250),
    ]
    for raw, expected in cases:
        assert resolve_max_terminal_executions(raw) == expected, raw


def test_resolve_max_terminal_executions_rejects_invalid_values():
    import pytest

    from cron.executions import resolve_max_terminal_executions

    invalid = [
        True, False,           # booleans are not caps
        0, -1, -1000,          # zero/negative would wipe or corrupt history
        0.0, -3.0, 1.5,        # fractional / non-positive floats
        float("inf"), float("nan"),
        "", "   ",             # empty strings
        "0", "-1", "+5",       # non-positive / signed strings
        "1.5", "abc", "1000x", "1_000",
        1 << 63, str(1 << 63),
        [1000], {"cap": 1000},
    ]
    for raw in invalid:
        with pytest.raises(ValueError):
            resolve_max_terminal_executions(raw)

    with pytest.raises(ValueError):
        resolve_max_terminal_executions(None, default=1 << 63)


def test_default_config_ships_module_default_cap():
    """The shipped config default and the module default must agree, and the
    shipped value must pass its own validation."""
    from cron.executions import (
        DEFAULT_MAX_TERMINAL_EXECUTIONS,
        resolve_max_terminal_executions,
    )
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    cron_config = DEFAULT_CONFIG["cron"]
    assert isinstance(cron_config, dict)
    configured = cron_config["max_terminal_executions"]
    assert configured == DEFAULT_MAX_TERMINAL_EXECUTIONS
    assert resolve_max_terminal_executions(configured) == DEFAULT_MAX_TERMINAL_EXECUTIONS


def test_default_cap_applies_without_user_config(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)

    assert (
        executions.current_max_terminal_executions()
        == executions.DEFAULT_MAX_TERMINAL_EXECUTIONS
    )


def test_config_cap_applies_end_to_end(monkeypatch, tmp_path):
    """Real config.yaml in an isolated HERMES_HOME drives the prune cap."""
    executions = _point_ledger(monkeypatch, tmp_path)
    _write_retention_config(2)

    assert executions.current_max_terminal_executions() == 2

    inflight = executions.create_execution("live", source="builtin")
    executions.mark_execution_running(inflight["id"])
    for index in range(6):
        row = executions.create_execution(f"done-{index}", source="builtin")
        executions.finish_execution(row["id"], success=True)

    records = executions.list_executions(limit=100)
    assert len([row for row in records if row["status"] == "completed"]) == 2
    assert executions.latest_execution("live")["status"] == "running"


def test_configured_cap_and_ledger_follow_context_local_profile(
    monkeypatch, tmp_path
):
    """Each in-process profile keeps its own cap and execution history."""
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", None)
    profiles = [(tmp_path / "profile-two", 2), (tmp_path / "profile-four", 4)]

    for profile_home, cap in profiles:
        token = set_hermes_home_override(profile_home)
        try:
            _write_retention_config(cap)
            for index in range(6):
                row = executions.create_execution(
                    f"{profile_home.name}-{index}", source="builtin"
                )
                executions.finish_execution(row["id"], success=True)
        finally:
            reset_hermes_home_override(token)

    for profile_home, cap in profiles:
        token = set_hermes_home_override(profile_home)
        try:
            records = executions.list_executions(limit=100)
            assert len(records) == cap
            assert all(row["job_id"].startswith(profile_home.name) for row in records)
        finally:
            reset_hermes_home_override(token)


def test_monkeypatched_module_constant_wins_over_config(monkeypatch, tmp_path):
    """The existing test injection point must beat a configured value."""
    executions = _point_ledger(monkeypatch, tmp_path)
    _write_retention_config(500)
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 3)

    assert executions.current_max_terminal_executions() == 3

    for index in range(8):
        row = executions.create_execution(f"done-{index}", source="builtin")
        executions.finish_execution(row["id"], success=True)
    records = executions.list_executions(limit=100)
    assert len([row for row in records if row["status"] == "completed"]) == 3


def test_explicit_default_sized_module_override_wins_over_config(monkeypatch, tmp_path):
    """An explicit override equal to the shipped default is still an override."""
    executions = _point_ledger(monkeypatch, tmp_path)
    _write_retention_config(7)
    monkeypatch.setattr(
        executions,
        "MAX_TERMINAL_EXECUTIONS",
        executions.DEFAULT_MAX_TERMINAL_EXECUTIONS,
    )

    assert (
        executions.current_max_terminal_executions()
        == executions.DEFAULT_MAX_TERMINAL_EXECUTIONS
    )


def test_env_var_does_not_override_configured_cap(monkeypatch, tmp_path):
    """No HERMES_* env override exists for this setting — config.yaml wins."""
    executions = _point_ledger(monkeypatch, tmp_path)
    _write_retention_config(7)
    monkeypatch.setenv("HERMES_CRON_MAX_EXECUTIONS", "99999")
    monkeypatch.setenv("HERMES_CRON_MAX_TERMINAL_EXECUTIONS", "99999")

    assert executions.current_max_terminal_executions() == 7


def test_env_var_does_not_override_default_cap(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_CRON_MAX_EXECUTIONS", "5")
    monkeypatch.setenv("HERMES_CRON_MAX_TERMINAL_EXECUTIONS", "5")

    assert (
        executions.current_max_terminal_executions()
        == executions.DEFAULT_MAX_TERMINAL_EXECUTIONS
    )


def test_invalid_configured_cap_fails_closed_without_deleting(
    monkeypatch, tmp_path, caplog
):
    """An invalid cap must skip pruning entirely — never coerce and delete."""
    import logging

    import pytest

    executions = _point_ledger(monkeypatch, tmp_path)
    _write_retention_config(0)

    with pytest.raises(ValueError):
        executions.current_max_terminal_executions()

    inflight = executions.create_execution("live", source="builtin")
    executions.mark_execution_running(inflight["id"])
    with caplog.at_level(logging.ERROR, logger="cron.executions"):
        for index in range(5):
            row = executions.create_execution(f"done-{index}", source="builtin")
            executions.finish_execution(row["id"], success=True)

    records = executions.list_executions(limit=100)
    assert len([row for row in records if row["status"] == "completed"]) == 5
    assert executions.latest_execution("live")["status"] == "running"
    errors = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR
        and "max_terminal_executions" in record.message
    ]
    assert len(errors) == 1


def test_oversized_configured_cap_fails_closed_without_rolling_back_terminal_state(
    monkeypatch, tmp_path, caplog
):
    """A non-bindable SQLite offset must not roll back a completed execution."""
    import logging

    import pytest

    executions = _point_ledger(monkeypatch, tmp_path)
    _write_retention_config(1 << 63)

    with pytest.raises(ValueError):
        executions.current_max_terminal_executions()

    claimed = executions.create_execution("oversized-cap", source="builtin")
    with caplog.at_level(logging.ERROR, logger="cron.executions"):
        completed = executions.finish_execution(claimed["id"], success=True)

    assert completed is not None
    assert completed["status"] == "completed"
    assert executions.latest_execution("oversized-cap")["status"] == "completed"
    assert sum(
        record.levelno == logging.ERROR
        and "max_terminal_executions" in record.message
        for record in caplog.records
    ) == 1


def test_retention_error_is_reported_again_after_a_valid_resolution(
    monkeypatch, tmp_path, caplog
):
    """A repaired setting ends the suppressed-error streak for its ledger."""
    import logging

    executions = _point_ledger(monkeypatch, tmp_path)
    resolved = iter([ValueError("invalid cap"), 3, ValueError("invalid cap")])

    def _resolve_next():
        result = next(resolved)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(executions, "current_max_terminal_executions", _resolve_next)

    with caplog.at_level(logging.ERROR, logger="cron.executions"):
        for index in range(3):
            row = executions.create_execution(f"streak-{index}", source="builtin")
            executions.finish_execution(row["id"], success=True)

    assert sum(record.levelno == logging.ERROR for record in caplog.records) == 2


def test_non_numeric_configured_cap_fails_closed_without_deleting(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    _write_retention_config("not-a-number")

    for index in range(4):
        row = executions.create_execution(f"done-{index}", source="builtin")
        executions.finish_execution(row["id"], success=True)

    records = executions.list_executions(limit=100)
    assert len([row for row in records if row["status"] == "completed"]) == 4


def test_recover_prune_honors_configured_cap(monkeypatch, tmp_path):
    """The restart-recovery prune path resolves the same configured cap."""
    executions = _point_ledger(monkeypatch, tmp_path)
    _write_retention_config(2)

    for index in range(5):
        executions.create_execution(f"dead-{index}", source="builtin")
    monkeypatch.setattr(executions, "_PROCESS_ID", "another-process")
    monkeypatch.setattr(executions, "_owner_is_live", lambda pid, started_at: False)

    assert executions.recover_interrupted_executions() == 5

    records = executions.list_executions(limit=100)
    assert len(records) == 2
    assert all(row["status"] == "unknown" for row in records)


def test_corrupt_store_fails_closed_without_overwrite(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    executions.EXECUTIONS_FILE.parent.mkdir(parents=True)
    executions.EXECUTIONS_FILE.write_bytes(b"not a sqlite database")

    with __import__("pytest").raises(sqlite3.DatabaseError):
        executions.create_execution("new", source="builtin")
    assert executions.EXECUTIONS_FILE.read_bytes() == b"not a sqlite database"


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
    monkeypatch.setattr(scheduler, "claim_job_for_fire", lambda _job_id: True)
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
    run_execution_ids = []
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

    def fake_run_job(job, *, defer_agent_teardown=None, execution_id=None, **_kw):
        run_execution_ids.append(execution_id)
        return True, "output", "response", None

    monkeypatch.setattr(scheduler, "run_job", fake_run_job)
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_args: None)
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *_args, **_kwargs: None)

    assert scheduler.run_one_job({"id": "job-3", "execution_id": "exec-3"}) is True
    assert run_execution_ids == ["exec-3"]
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
    monkeypatch.setattr("cron.jobs.record_ticker_heartbeat", lambda **_kwargs: events.append("heartbeat"))

    provider.InProcessCronScheduler().start(stop, interval=1)

    assert events[:2] == ["recover", "heartbeat"]


def test_external_provider_start_recovers_interrupted_records(monkeypatch):
    from plugins.cron_providers.chronos import ChronosCronScheduler

    provider = ChronosCronScheduler()
    provider._client = type("Client", (), {"arm": lambda self, **kwargs: None})()
    events = []
    monkeypatch.setattr(
        "cron.executions.recover_interrupted_executions",
        lambda: events.append("recover") or 0,
    )
    monkeypatch.setattr(provider, "reconcile", lambda: events.append("reconcile"))

    provider.start(__import__("threading").Event())

    assert events == ["recover", "reconcile"]


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
