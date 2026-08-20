"""Behavioral coverage for Phase A cron attestation contracts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone


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
    claimed = jobs.claim_job_for_fire(
        on_time["id"],
        invocation_kind=SCHEDULED_ON_TIME,
        intended_fire_at=intended,
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


def test_provider_binds_scheduled_and_force_provenance(monkeypatch, tmp_path):
    import cron.jobs as jobs
    from cron.context import OPERATOR_TRIGGERED, PROVIDER_SCHEDULED
    from cron.executions import list_executions
    from cron.scheduler_provider import InProcessCronScheduler

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fixed_now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(jobs, "_hermes_now", lambda: fixed_now)
    monkeypatch.setattr(
        "cron.executions.EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )
    scheduled = jobs.create_job("provider", "every 5m", name="provider")
    claimed = InProcessCronScheduler().claim_fire(
        scheduled["id"],
        intended_fire_at=(fixed_now - timedelta(seconds=300)).isoformat(),
    )
    assert claimed["fire_claim"]["invocation_kind"] == PROVIDER_SCHEDULED
    assert (
        list_executions(job_id=scheduled["id"])[0]["invocation_kind"]
        == PROVIDER_SCHEDULED
    )

    forced_job = jobs.create_job("force", "every 5m", name="force")
    forced = InProcessCronScheduler().claim_fire(forced_job["id"], force=True)
    assert forced["fire_claim"]["invocation_kind"] == OPERATOR_TRIGGERED


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
