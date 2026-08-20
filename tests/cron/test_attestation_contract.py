"""Behavioral coverage for Phase A cron attestation contracts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path


def test_legacy_execution_rows_migrate_and_keep_terminal_evidence(
    monkeypatch, tmp_path
):
    import cron.executions as executions

    db_path = tmp_path / "cron" / "executions.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE executions (
             id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL,
             process_id TEXT NOT NULL, pid INTEGER NOT NULL,
             process_started_at INTEGER, status TEXT NOT NULL,
             claimed_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
             error TEXT)"""
    )
    conn.execute(
        "INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "old",
            "legacy-job",
            "builtin",
            "old-process",
            1,
            None,
            "completed",
            "2026-08-20T00:00:00+00:00",
            None,
            "2026-08-20T00:01:00+00:00",
            None,
        ),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", db_path)

    migrated = executions.latest_execution("legacy-job")
    assert migrated["id"] == "old"
    assert migrated["invocation_kind"] == "UNKNOWN"
    assert migrated["delivery_status"] == "NOT_ATTEMPTED"

    output = tmp_path / "founder-card.md"
    output.write_text("card\n", encoding="utf-8")
    row = executions.create_execution(
        "attested-job",
        source="builtin",
        invocation_kind="SCHEDULED_ON_TIME",
        intended_fire_at="2026-08-20T12:00:00+00:00",
    )
    finished = executions.finish_execution(
        row["id"],
        success=True,
        delivery_status="PROVIDER_ACCEPTED",
        delivery_target=json.dumps({"platform": "telegram", "chat_id": "42"}),
        delivery_target_class="telegram",
        delivery_content_sha256=hashlib.sha256(b"hello").hexdigest(),
        delivery_attempted_at="2026-08-20T12:00:01+00:00",
        delivery_completed_at="2026-08-20T12:00:02+00:00",
        delivery_receipt_id="receipt-1",
        output_path=str(output),
        output_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
        founder_card_path=str(output),
        founder_card_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
    )
    assert finished["delivery_status"] == "PROVIDER_ACCEPTED"
    assert finished["delivery_consumption_status"] == "UNKNOWN"
    assert finished["delivery_receipt_id"] == "receipt-1"
    assert finished["output_path"] == str(output)
    assert finished["output_sha256"] == finished["founder_card_sha256"]


def test_fixed_grace_and_builtin_claim_kinds(monkeypatch, tmp_path):
    import cron.jobs as jobs
    from cron.context import (
        OPERATOR_TRIGGERED,
        RECOVERY_CATCHUP,
        SCHEDULED_ON_TIME,
        _BUILTIN_SCHEDULER_ADMISSION,
        classify_scheduled_fire,
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(jobs, "_hermes_now", lambda: now)
    intended = (now - timedelta(seconds=300)).isoformat()
    assert classify_scheduled_fire(intended, now=now) == SCHEDULED_ON_TIME
    assert (
        classify_scheduled_fire((now - timedelta(seconds=301)).isoformat(), now=now)
        == RECOVERY_CATCHUP
    )

    on_time = jobs.create_job("on time", "every 5m", name="on-time")
    stored = jobs.load_jobs()
    stored[0]["next_run_at"] = intended
    jobs.save_jobs(stored)
    claimed = jobs.claim_job_for_fire(
        on_time["id"],
        invocation_kind=SCHEDULED_ON_TIME,
        intended_fire_at=intended,
        _scheduler_admission=_BUILTIN_SCHEDULER_ADMISSION,
        return_job=True,
    )
    assert claimed["fire_claim"]["invocation_kind"] == SCHEDULED_ON_TIME

    operator = jobs.create_job("operator", "every 5m", name="operator")
    forced = jobs.claim_job_for_fire(
        operator["id"],
        force=True,
        invocation_kind=SCHEDULED_ON_TIME,
        intended_fire_at=intended,
        return_job=True,
    )
    assert forced["fire_claim"]["invocation_kind"] == OPERATOR_TRIGGERED

    generic = jobs.create_job("generic", "every 5m", name="generic")
    generic_claim = jobs.claim_job_for_fire(
        generic["id"],
        invocation_kind="PROVIDER_SCHEDULED",
        intended_fire_at=intended,
        return_job=True,
    )
    assert generic_claim["fire_claim"]["invocation_kind"] == "UNKNOWN"
    assert generic_claim["fire_claim"]["intended_fire_at"] is None


def test_provider_binds_scheduled_and_force_provenance(monkeypatch, tmp_path):
    import cron.jobs as jobs
    from cron.context import OPERATOR_TRIGGERED, PROVIDER_SCHEDULED
    from cron.executions import list_executions
    from cron.scheduler_provider import (
        InProcessCronScheduler,
        _AUTHENTICATED_PROVIDER_ADMISSION,
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fixed_now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(jobs, "_hermes_now", lambda: fixed_now)
    monkeypatch.setattr(
        "cron.executions.EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )
    scheduled = jobs.create_job("provider", "every 5m", name="provider")
    stored = jobs.load_jobs()
    stored[0]["next_run_at"] = (fixed_now - timedelta(seconds=300)).isoformat()
    jobs.save_jobs(stored)
    claimed = InProcessCronScheduler().claim_fire(
        scheduled["id"],
        intended_fire_at=(fixed_now - timedelta(seconds=300)).isoformat(),
        _provider_admission=_AUTHENTICATED_PROVIDER_ADMISSION,
    )
    assert claimed["fire_claim"]["invocation_kind"] == PROVIDER_SCHEDULED
    assert (
        list_executions(job_id=scheduled["id"])[0]["invocation_kind"]
        == PROVIDER_SCHEDULED
    )

    forced_job = jobs.create_job("force", "every 5m", name="force")
    forced = InProcessCronScheduler().claim_fire(forced_job["id"], force=True)
    assert forced["fire_claim"]["invocation_kind"] == OPERATOR_TRIGGERED

    laundered_job = jobs.create_job("launder", "every 5m", name="launder")
    laundered = InProcessCronScheduler().claim_fire(
        laundered_job["id"],
        invocation_kind=PROVIDER_SCHEDULED,
        intended_fire_at=fixed_now.isoformat(),
    )
    assert laundered["fire_claim"]["invocation_kind"] == OPERATOR_TRIGGERED


def test_provider_binds_time_from_atomic_claim_after_schedule_mutation(
    monkeypatch, tmp_path
):
    import cron.executions as executions
    import cron.jobs as jobs
    from cron.context import RECOVERY_CATCHUP, _AUTHENTICATED_PROVIDER_ADMISSION
    from cron.scheduler_provider import InProcessCronScheduler

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )
    fixed_now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(jobs, "_hermes_now", lambda: fixed_now)
    job = jobs.create_job("atomic", "every 5m", name="atomic")
    real_claim = jobs.claim_job_for_fire
    mutated_intended = (fixed_now - timedelta(seconds=301)).isoformat()

    def mutate_then_claim(job_id, **kwargs):
        records = jobs.load_jobs()
        records[0]["next_run_at"] = mutated_intended
        jobs.save_jobs(records)
        return real_claim(job_id, **kwargs)

    mutate_then_claim.__module__ = "cron.jobs"
    monkeypatch.setattr(jobs, "claim_job_for_fire", mutate_then_claim)
    claimed = InProcessCronScheduler().claim_fire(
        job["id"],
        intended_fire_at=fixed_now.isoformat(),
        _provider_admission=_AUTHENTICATED_PROVIDER_ADMISSION,
    )
    assert claimed["fire_claim"]["intended_fire_at"] == mutated_intended
    assert claimed["fire_claim"]["invocation_kind"] == RECOVERY_CATCHUP


def test_execution_claim_binding_is_single_assignment(monkeypatch, tmp_path):
    from cron.context import OPERATOR_TRIGGERED, PROVIDER_SCHEDULED
    from cron.executions import bind_execution_claim, create_execution

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    row = create_execution("single-bind", source="builtin")
    first = bind_execution_claim(
        row["id"],
        invocation_kind=PROVIDER_SCHEDULED,
        intended_fire_at="2026-08-20T12:00:00+00:00",
        claim_owner="owner-a",
    )
    assert first["claim_owner"] == "owner-a"
    assert bind_execution_claim(
        row["id"],
        invocation_kind=OPERATOR_TRIGGERED,
        intended_fire_at=None,
        claim_owner="owner-b",
    ) is None


def test_founder_card_binds_exact_dispatched_bytes_and_delivery_statuses(
    monkeypatch, tmp_path
):
    import cron.executions as executions
    import cron.scheduler as scheduler
    from cron.context import set_delivery_detail

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )
    monkeypatch.setattr(
        scheduler,
        "_resolve_delivery_targets",
        lambda job: [] if job["id"] in {"suppressed", "missing"} else [
            {"platform": "telegram", "chat_id": "42", "thread_id": None}
        ],
    )
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda *_args, **_kwargs: (True, "FULL OUTPUT DOCUMENT", "Final response", None),
    )
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *_args, **_kwargs: True)

    def save_output(job_id, _output):
        path = tmp_path / f"{job_id}-full.md"
        path.write_text("FULL OUTPUT DOCUMENT", encoding="utf-8")
        return path

    monkeypatch.setattr(scheduler, "save_job_output", save_output)

    def deliver(job, _content, **_kwargs):
        if job["id"] == "missing":
            return "no delivery target resolved"
        if job["id"] == "suppressed":
            return None
        set_delivery_detail("provider_attempted", True)
        set_delivery_detail("provider_attempted_at", "2026-08-20T12:00:01+00:00")
        set_delivery_detail("dispatched_content", "TRANSPORT CARD\n")
        if job["id"] == "exception":
            set_delivery_detail("transport_exception", True)
            return "socket closed after send"
        if job["id"] == "rejected":
            return "transport rejected"
        set_delivery_detail("provider_accepted", True)
        set_delivery_detail("provider_receipt_id", "receipt-42")
        return None

    monkeypatch.setattr(scheduler, "_deliver_result", deliver)

    jobs = {
        job_id: {"id": job_id, "name": job_id, "deliver": "telegram"}
        for job_id in ("accepted", "exception", "rejected")
    }
    jobs["suppressed"] = {"id": "suppressed", "name": "suppressed", "deliver": "local"}
    jobs["missing"] = {"id": "missing", "name": "missing", "deliver": "telegram"}
    for job in jobs.values():
        assert scheduler.run_one_job(job) is True

    accepted = executions.latest_execution("accepted")
    assert accepted["delivery_status"] == "PROVIDER_ACCEPTED"
    assert accepted["delivery_consumption_status"] == "UNKNOWN"
    assert accepted["delivery_receipt_id"] == "receipt-42"
    card_bytes = Path(accepted["founder_card_path"]).read_bytes()
    assert card_bytes == b"TRANSPORT CARD\n"
    assert accepted["founder_card_sha256"] == hashlib.sha256(card_bytes).hexdigest()
    assert accepted["delivery_content_sha256"] == accepted["founder_card_sha256"]
    assert Path(accepted["output_path"]).read_text(encoding="utf-8") != card_bytes.decode()
    assert accepted["output_sha256"] != accepted["founder_card_sha256"]

    assert executions.latest_execution("exception")["delivery_status"] == "UNKNOWN"
    assert executions.latest_execution("rejected")["delivery_status"] == "FAILED"
    suppressed = executions.latest_execution("suppressed")
    assert suppressed["delivery_status"] == "SUPPRESSED"
    assert suppressed["founder_card_path"] is None
    missing = executions.latest_execution("missing")
    assert missing["delivery_status"] == "NOT_CONFIGURED"
    assert missing["delivery_attempted_at"] is None
    assert missing["founder_card_path"] is None


def test_cron_context_reaches_child_and_resets_without_cross_thread_leak(monkeypatch):
    from cron.context import SCHEDULED_ON_TIME, cron_execution_context
    from tools.environments.local import build_subprocess_env

    monkeypatch.setenv("HERMES_CRON_EXECUTION_ID", "caller-value")
    monkeypatch.setenv("HERMES_CRON_INVOCATION_KIND", "caller-value")

    def probe(execution_id):
        with cron_execution_context(execution_id, SCHEDULED_ON_TIME):
            env = build_subprocess_env(scrub_secrets=False)
            child = subprocess.check_output(
                [
                    sys.executable,
                    "-c",
                    "import os; print(os.getenv('HERMES_CRON_EXECUTION_ID')); print(os.getenv('HERMES_CRON_INVOCATION_KIND'))",
                ],
                env=env,
                text=True,
            ).splitlines()
            return env["HERMES_CRON_EXECUTION_ID"], child

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(probe, ("exec-a", "exec-b")))
    assert {item[0] for item in results} == {"exec-a", "exec-b"}
    assert all(item[1] == [item[0], SCHEDULED_ON_TIME] for item in results)

    after = build_subprocess_env(scrub_secrets=False)
    assert "HERMES_CRON_EXECUTION_ID" not in after
    assert "HERMES_CRON_INVOCATION_KIND" not in after


def test_run_job_requires_owner_bearing_claim_agreement(monkeypatch, tmp_path):
    import cron.executions as executions
    import cron.jobs as jobs
    import cron.scheduler as scheduler
    from tools.environments.local import build_subprocess_env

    observed = []

    def fake_run_job(job, **_kwargs):
        observed.append(build_subprocess_env(scrub_secrets=False))
        return True, "", "", None

    monkeypatch.setattr(scheduler, "_run_job", fake_run_job)
    scheduler.run_job(
        {"id": "crafted", "execution_id": "forged", "invocation_kind": "PROVIDER_SCHEDULED"}
    )
    assert "HERMES_CRON_EXECUTION_ID" not in observed[-1]

    scheduler.run_job(
        {
            "id": "mismatch",
            "execution_id": "forged",
            "invocation_kind": "PROVIDER_SCHEDULED",
            "fire_claim": {
                "execution_id": "real",
                "invocation_kind": "PROVIDER_SCHEDULED",
                "by": "owner",
            },
        }
    )
    assert "HERMES_CRON_EXECUTION_ID" not in observed[-1]

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )
    fixed_now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(jobs, "_hermes_now", lambda: fixed_now)
    valid_job = jobs.create_job("valid", "every 5m", name="valid")
    stored_jobs = jobs.load_jobs()
    stored_jobs[0]["next_run_at"] = fixed_now.isoformat()
    jobs.save_jobs(stored_jobs)
    from cron.scheduler_provider import (
        InProcessCronScheduler,
        _AUTHENTICATED_PROVIDER_ADMISSION,
    )

    claimed = InProcessCronScheduler().claim_fire(
        valid_job["id"],
        _provider_admission=_AUTHENTICATED_PROVIDER_ADMISSION,
    )
    assert claimed is not None
    scheduler.run_job(claimed)
    assert observed[-1]["HERMES_CRON_EXECUTION_ID"] == claimed["execution_id"]
    assert observed[-1]["HERMES_CRON_INVOCATION_KIND"] == claimed["fire_claim"]["invocation_kind"]


def test_tick_binds_atomic_operator_claim_after_stale_snapshot(monkeypatch, tmp_path):
    """A trigger arriving after advance but before claim must win atomically."""
    import cron.executions as executions
    import cron.jobs as jobs
    import cron.scheduler as scheduler
    from cron.context import OPERATOR_TRIGGERED

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(jobs, "_hermes_now", lambda: now)
    due_at = (now - timedelta(seconds=30)).isoformat()
    created = jobs.create_job("operator-race", "every 5m", name="operator-race")
    stored = jobs.load_jobs()
    stored[0]["next_run_at"] = due_at
    jobs.save_jobs(stored)
    monkeypatch.setattr(scheduler, "_hermes_now", lambda: now)
    monkeypatch.setattr(
        scheduler,
        "_get_lock_paths",
        lambda: (tmp_path / "locks", tmp_path / "locks" / "tick.lock"),
    )
    monkeypatch.setattr(scheduler, "load_config", lambda: {})
    monkeypatch.setattr(scheduler, "try_register_running_job", lambda _job_id: True)
    monkeypatch.setattr(scheduler, "release_running_job", lambda _job_id: None)
    monkeypatch.setattr(scheduler, "_interpreter_shutting_down", lambda *_a: False)
    monkeypatch.setattr(executions, "recover_interrupted_executions", lambda: 0)

    real_claim = jobs.claim_job_for_fire

    def atomic_claim(job_id, **kwargs):
        # This is the concurrent operator write landing after the real
        # scheduler advance and immediately before the atomic claim lock.
        assert jobs.trigger_job(job_id) is not None
        return real_claim(job_id, **kwargs)

    # Match the production function identity check so this exercises the
    # capability-backed scheduler path, not a legacy test-double path.
    atomic_claim.__module__ = "cron.jobs"
    monkeypatch.setattr(scheduler, "claim_job_for_fire", atomic_claim)
    ran = []
    monkeypatch.setattr(
        scheduler,
        "run_one_job",
        lambda claimed, **_kwargs: ran.append(claimed) or True,
    )

    assert scheduler.tick(verbose=False, sync=True) == 1
    assert ran[0]["fire_claim"]["invocation_kind"] == OPERATOR_TRIGGERED

    persisted = executions.latest_execution(created["id"])
    assert persisted["invocation_kind"] == OPERATOR_TRIGGERED
    assert persisted["intended_fire_at"] is None
    stored = jobs.load_jobs()[0]
    assert stored["fire_claim"]["invocation_kind"] == OPERATOR_TRIGGERED
    assert "trigger_marker" not in stored


def test_tick_preserves_prior_due_instant_through_real_recurrence_advance(
    monkeypatch, tmp_path
):
    """A normal recurring tick attests the due instant before it advances."""
    import cron.executions as executions
    import cron.jobs as jobs
    import cron.scheduler as scheduler
    from cron.context import SCHEDULED_ON_TIME

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(jobs, "_hermes_now", lambda: now)
    monkeypatch.setattr(scheduler, "_hermes_now", lambda: now)
    monkeypatch.setattr(
        scheduler,
        "_get_lock_paths",
        lambda: (tmp_path / "locks", tmp_path / "locks" / "tick.lock"),
    )
    monkeypatch.setattr(scheduler, "load_config", lambda: {})
    monkeypatch.setattr(scheduler, "try_register_running_job", lambda _job_id: True)
    monkeypatch.setattr(scheduler, "release_running_job", lambda _job_id: None)
    monkeypatch.setattr(scheduler, "_interpreter_shutting_down", lambda *_a: False)
    monkeypatch.setattr(executions, "recover_interrupted_executions", lambda: 0)

    due_at = (now - timedelta(seconds=30)).isoformat()
    created = jobs.create_job("normal-fire", "every 5m", name="normal-fire")
    stored = jobs.load_jobs()
    stored[0]["next_run_at"] = due_at
    jobs.save_jobs(stored)

    ran = []
    monkeypatch.setattr(
        scheduler,
        "run_one_job",
        lambda claimed, **_kwargs: ran.append(claimed) or True,
    )

    assert scheduler.tick(verbose=False, sync=True) == 1
    assert ran[0]["fire_claim"]["invocation_kind"] == SCHEDULED_ON_TIME
    assert ran[0]["fire_claim"]["intended_fire_at"] == due_at

    persisted = executions.latest_execution(created["id"])
    assert persisted["invocation_kind"] == SCHEDULED_ON_TIME
    assert persisted["intended_fire_at"] == due_at
    stored = jobs.load_jobs()[0]
    assert stored["next_run_at"] != due_at
