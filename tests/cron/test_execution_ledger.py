"""Durable cron execution-ledger behavior."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


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


def test_corrupt_store_fails_closed_without_overwrite(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    executions.EXECUTIONS_FILE.parent.mkdir(parents=True)
    executions.EXECUTIONS_FILE.write_bytes(b"not a sqlite database")

    with __import__("pytest").raises(sqlite3.DatabaseError):
        executions.create_execution("new", source="builtin")
    assert executions.EXECUTIONS_FILE.read_bytes() == b"not a sqlite database"


def test_execution_history_is_paginated(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    ids = []
    for _index in range(5):
        row = executions.create_execution("paged", source="builtin")
        executions.finish_execution(row["id"], success=True)
        ids.append(row["id"])

    first = executions.list_executions(job_id="paged", limit=2)
    second = executions.list_executions(
        job_id="paged", limit=2, before_claimed_at=first[-1]["claimed_at"]
    )
    assert [row["id"] for row in first] == list(reversed(ids))[:2]
    assert set(row["id"] for row in first).isdisjoint(row["id"] for row in second)


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


def test_recovery_does_not_mark_other_live_owner_unknown(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("other-live", source="builtin")
    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        conn.execute(
            "UPDATE executions SET process_id=?, pid=? WHERE id=?",
            ("another-import", os.getpid(), record["id"]),
        )

    assert executions.recover_interrupted_executions() == 0
    assert executions.latest_execution("other-live")["status"] == "claimed"


def test_recovery_rejects_recycled_pid(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("recycled", source="builtin")
    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        conn.execute(
            "UPDATE executions SET process_id=?, process_started_at=? WHERE id=?",
            ("old-import", -1, record["id"]),
        )

    assert executions.recover_interrupted_executions() == 1
    assert executions.latest_execution("recycled")["status"] == "unknown"


@pytest.mark.timeout(240)
def test_restart_marks_interrupted_execution_unknown_without_requeue(tmp_path):
    """Real temp-HERMES_HOME subprocess restart: in-flight is audit-only unknown.

    Explicitly budgeted well above the suite-wide per-test cap. This test is
    the only one in the file that spawns interpreters, and it spawns two — a
    real restart needs the owner process to actually die before a *different*
    process recovers its row, which is the whole point of the assertion. Each
    of those pays a full cold ``import cron.executions``; on this box the pair
    costs ~26s alone and the nightly gate runs 24 workers on top of that.

    Measured 2026-08-11: standalone the file is green in 72.7s with this test
    at 26.2s, but in the gate's parallel lane it crossed the 60s cap and
    pytest-timeout's thread method killed the process, so all 24 tests reported
    as "no tests ran". The cost is real and inherent, not a wedge — so raise
    the bound for this test rather than weaken what it proves.
    """
    home = tmp_path / "home"
    repo = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    # PREPEND, never replace. Assigning str(repo) outright drops whatever the
    # parent inherited -- which is fatal under an interpreter whose third-party
    # deps reach it only through PYTHONPATH rather than through a venv's own
    # site-packages. That is exactly the nightly gate's interpreter since
    # 2026-08-15, when the cron script slot flipped from Store CPython 3.11 to
    # the uv-managed CPython 3.12: the child lost
    # agent-src/.venv/Lib/site-packages and died on `import yaml` inside
    # utils.py, three nights running, while the file passed standalone under the
    # venv python. The repo path still comes first, so it keeps winning.
    inherited = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo), *(p for p in inherited if p != str(repo))]
    )

    def _run_child(label: str, code: str) -> subprocess.CompletedProcess:
        """Run a helper interpreter, keeping its output attached to failures.

        Deliberately not ``check=True``: CalledProcessError renders as
        "Command ... returned non-zero exit status N" and does NOT print the
        child's stderr, so a child that dies under load reports a bare exit
        code and the run that produced it is gone. This test only fails
        intermittently (once in the 2026-08-12 nightly gate at 12 workers, not
        reproducible on demand), so the one run that catches it must carry
        enough to name the cause.
        """
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=repo,
            env=env,
            text=True,
            capture_output=True,
        )
        assert proc.returncode == 0, (
            f"{label} child exited {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
        return proc

    create = _run_child(
        "create",
        "from cron.executions import create_execution, mark_execution_running; "
        "r=create_execution('restart-job', source='builtin'); "
        "mark_execution_running(r['id']); print(r['id'])",
    )
    # One line only. Anything else on stdout (a warning, a log line routed to
    # stdout) would silently become the "execution id" and misattribute the
    # later id comparison to the wrong defect.
    create_lines = create.stdout.strip().splitlines()
    assert len(create_lines) == 1, (
        "create child wrote more than the execution id to stdout\n"
        f"--- stdout ---\n{create.stdout}\n--- stderr ---\n{create.stderr}"
    )
    execution_id = create_lines[0]

    recover = _run_child(
        "recover",
        "import json; from cron.executions import recover_interrupted_executions, list_executions; "
        "print(recover_interrupted_executions()); "
        "print(json.dumps(list_executions(job_id='restart-job'))) ",
    )
    lines = recover.stdout.strip().splitlines()
    # A 0 here means the recovering process decided the owner was still alive.
    # The owner is definitively dead (subprocess.run waited for it), and a
    # recycled PID is rejected by the centisecond-resolution
    # process_started_at comparison -- so 0 means _owner_is_live() took its
    # fail-safe "could not probe liveness" branch, which now logs to the
    # child's stderr. Print both streams so that branch names itself.
    assert lines[0] == "1", (
        f"expected exactly 1 recovered execution, got {lines[0]!r}. "
        "A 0 means _owner_is_live() could not prove the owner dead - check "
        "stderr for its fail-safe warning.\n"
        f"--- stdout ---\n{recover.stdout}\n--- stderr ---\n{recover.stderr}"
    )
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
    # Merge (0.16.0 catch-up): the fork's tick() consumes
    # get_due_and_skipped_jobs() (it emits skipped-job events), so patch
    # that — patching get_due_jobs (pure-upstream tick) would leave tick
    # reading the real empty list.
    monkeypatch.setattr(
        scheduler, "get_due_and_skipped_jobs",
        lambda: ([{"id": "submit-fail"}], []),
    )
    monkeypatch.setattr(scheduler, "advance_next_run", lambda _job_id: None)
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
        "cron.executions.recover_interrupted_execution_records",
        lambda: events.append("recover") or [],
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
        "cron.executions.recover_interrupted_execution_records",
        lambda: events.append("recover") or [],
    )
    monkeypatch.setattr(provider, "reconcile", lambda: events.append("reconcile"))

    provider.start(__import__("threading").Event())

    assert events == ["recover", "reconcile"]


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


# =========================================================================
# Orphaned write-back backfill
# =========================================================================
# jobs.json's ``last_run_at`` is written ONLY by mark_job_run() at the end of a
# run, in the owning process, while advance_next_run() moves ``next_run_at``
# BEFORE the run. When the owner dies in between, next_run_at advanced but
# last_run_at did not, so the job silently reports its previous clean
# completion forever. Observed 2026-07-27: financier-snapshot-pm produced its
# snapshot artifact at 16:12 but jobs.json still claimed a 2026-07-24 run
# because gateway pid 6668 died before mark_job_run(). The ledger knew
# (status='unknown') but nothing carried that back to jobs.json.


def _point_jobs(monkeypatch, tmp_path):
    import cron.jobs as jobs

    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    return jobs


def _orphan(executions, job_id):
    """Create an attempt whose owner process is provably gone."""
    record = executions.create_execution(job_id, source="builtin")
    executions.mark_execution_running(record["id"])
    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        conn.execute(
            "UPDATE executions SET process_id=?, process_started_at=? WHERE id=?",
            ("dead-owner", -1, record["id"]),
        )
    # Re-read: the create_execution() row predates the running transition, so
    # its started_at is still None.
    return executions.latest_execution(job_id)


def test_recovery_returns_records_and_count_stays_int(monkeypatch, tmp_path):
    """The records variant exposes the rows; the legacy name still returns a count."""
    executions = _point_ledger(monkeypatch, tmp_path)
    record = _orphan(executions, "records-api")

    recovered = executions.recover_interrupted_execution_records()

    assert [row["id"] for row in recovered] == [record["id"]]
    assert recovered[0]["status"] == "unknown"
    assert recovered[0]["job_id"] == "records-api"
    # Idempotent: the row is terminal now, so a second pass recovers nothing.
    assert executions.recover_interrupted_executions() == 0


def test_mark_job_interrupted_stamps_unknown_without_advancing_schedule(
    monkeypatch, tmp_path
):
    """An orphaned run lands last_run_at/last_status but must not move the schedule."""
    jobs = _point_jobs(monkeypatch, tmp_path)
    job = jobs.create_job(prompt="snapshot", schedule="every 1h", name="snap")
    jobs.advance_next_run(job["id"])  # pre-run advance, as the scheduler does
    armed_next = jobs.get_job(job["id"])["next_run_at"]

    assert jobs.mark_job_interrupted(
        job["id"], ran_at="2026-07-27T16:10:45.551353-04:00", error="owner exited"
    ) is True

    stamped = jobs.get_job(job["id"])
    assert stamped["last_run_at"] == "2026-07-27T16:10:45.551353-04:00"
    assert stamped["last_status"] == "unknown"
    assert stamped["last_error"] == "owner exited"
    # next_run_at was already advanced pre-run — re-advancing would skip a fire.
    assert stamped["next_run_at"] == armed_next
    # An unknown outcome is not a known completion and not a known error.
    assert stamped["repeat"]["completed"] == 0
    assert stamped.get("consecutive_errors", 0) == 0


def test_mark_job_interrupted_never_regresses_a_newer_run(monkeypatch, tmp_path):
    """A late recovery pass must not overwrite a newer clean completion."""
    jobs = _point_jobs(monkeypatch, tmp_path)
    job = jobs.create_job(prompt="snapshot", schedule="every 1h", name="snap")
    jobs.mark_job_run(job["id"], success=True)
    fresh = jobs.get_job(job["id"])["last_run_at"]

    assert jobs.mark_job_interrupted(
        job["id"], ran_at="2026-07-24T16:14:54.214437-04:00", error="stale orphan"
    ) is False

    unchanged = jobs.get_job(job["id"])
    assert unchanged["last_run_at"] == fresh
    assert unchanged["last_status"] == "ok"


def test_mark_job_interrupted_ignores_unknown_job(monkeypatch, tmp_path):
    jobs = _point_jobs(monkeypatch, tmp_path)
    jobs.save_jobs([])

    assert jobs.mark_job_interrupted("ghost", ran_at="2026-07-27T16:10:45-04:00") is False


def test_restart_backfills_jobs_json_for_orphaned_run(monkeypatch, tmp_path):
    """The reported bug: a dead owner must not leave jobs.json reporting a stale run.

    financier-snapshot-pm ran and produced its artifact, but its gateway died
    before mark_job_run(). jobs.json kept reporting the previous clean run days
    later. Recovery must carry the ledger's verdict back onto the job record.
    """
    from cron.scheduler_provider import InProcessCronScheduler

    jobs = _point_jobs(monkeypatch, tmp_path)
    executions = _point_ledger(monkeypatch, tmp_path)

    job = jobs.create_job(prompt="", schedule="every 1d", name="financier-snapshot-pm")
    jobs.mark_job_run(job["id"], success=True)
    stale_run = jobs.get_job(job["id"])["last_run_at"]

    record = _orphan(executions, job["id"])

    assert InProcessCronScheduler().recover_interrupted() == 1

    backfilled = jobs.get_job(job["id"])
    assert backfilled["last_run_at"] != stale_run
    assert backfilled["last_run_at"] == record["started_at"]
    assert backfilled["last_status"] == "unknown"
    assert "owner exited" in backfilled["last_error"]


def test_recovery_backfill_survives_a_missing_job_record(monkeypatch, tmp_path):
    """A ledger row for a since-deleted job must not abort the recovery pass."""
    from cron.scheduler_provider import InProcessCronScheduler

    jobs = _point_jobs(monkeypatch, tmp_path)
    executions = _point_ledger(monkeypatch, tmp_path)

    job = jobs.create_job(prompt="", schedule="every 1d", name="survivor")
    _orphan(executions, "deleted-job")
    _orphan(executions, job["id"])

    assert InProcessCronScheduler().recover_interrupted() == 2
    assert jobs.get_job(job["id"])["last_status"] == "unknown"


def test_execution_retention_holds_multiple_days_of_history():
    """1000 rows was ~24h on a busy profile — too short to date a daily job."""
    from cron.executions import MAX_TERMINAL_EXECUTIONS

    assert MAX_TERMINAL_EXECUTIONS >= 10000


def test_deadline_verdict_can_be_amended_to_late_success(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    row = executions.create_execution("late-ok", source="builtin")
    executions.mark_execution_running(row["id"])
    abandon = "soft deadline exceeded: worker abandoned"
    executions.finish_execution(row["id"], success=False, error=abandon)

    amended = executions.amend_execution_after_abandon(
        row["id"], abandon_error=abandon, success=True
    )

    assert amended["status"] == "completed"
    assert amended["error"] is None
    assert executions.latest_execution("late-ok")["status"] == "completed"


def test_deadline_verdict_can_expose_the_late_real_failure(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    row = executions.create_execution("late-fail", source="builtin")
    abandon = "soft deadline exceeded: worker abandoned"
    executions.finish_execution(row["id"], success=False, error=abandon)

    amended = executions.amend_execution_after_abandon(
        row["id"],
        abandon_error=abandon,
        success=False,
        error="hard wall-clock timeout: last activity terminal",
    )

    assert amended["status"] == "failed"
    assert amended["error"] == "hard wall-clock timeout: last activity terminal"


def test_deadline_amendment_never_rewrites_an_unrelated_failure(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    row = executions.create_execution("owned", source="builtin")
    executions.finish_execution(row["id"], success=False, error="successor failure")

    assert executions.amend_execution_after_abandon(
        row["id"],
        abandon_error="soft deadline exceeded",
        success=True,
    ) is None
    assert executions.latest_execution("owned")["error"] == "successor failure"
