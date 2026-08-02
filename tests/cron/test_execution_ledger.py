"""Durable cron execution-ledger behavior."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

FIRE_AT = "2026-08-01T09:10:00+00:00"


def _create(executions, job_id, *, source="builtin"):
    return executions.create_execution(job_id, source=source, scheduled_for=FIRE_AT)


def _point_ledger(monkeypatch, tmp_path):
    import cron.executions as executions

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    return executions


def test_execution_transitions_are_durable(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)

    claimed = _create(executions, "job-1")
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


def test_execution_persists_nominal_scheduled_time(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    claimed = executions.create_execution(
        "scheduled-job",
        source="builtin",
        scheduled_for="2026-08-01T09:10:00+00:00",
    )

    assert claimed["scheduled_for"] == "2026-08-01T09:10:00+00:00"
    assert executions.get_execution(claimed["id"])["scheduled_for"] == claimed["scheduled_for"]


@pytest.mark.parametrize("scheduled_for", [
    None,
    "",
    "not-a-time",
    "2026-08-01T09:10:00",
    "2026-08-01T09:10:00Z",
    "2026-08-01T04:10:00-05:00",
])
def test_producer_execution_rejects_missing_or_noncanonical_nominal_time(
    monkeypatch, tmp_path, scheduled_for,
):
    executions = _point_ledger(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="canonical UTC scheduled_for"):
        executions.create_execution(
            "scheduled-job", source="builtin", scheduled_for=scheduled_for,
        )


def test_producer_execution_source_is_exactly_allowlisted(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    fire_at = "2026-08-01T09:10:00+00:00"

    builtin = executions.create_execution(
        "builtin-job", source="builtin", scheduled_for=fire_at,
    )
    chronos = executions.create_execution(
        "chronos-job", source="chronos", scheduled_for=fire_at,
    )
    with pytest.raises(ValueError, match="scheduler source"):
        executions.create_execution(
            "foreign-job", source="external", scheduled_for=fire_at,
        )

    assert builtin["source"] == "builtin"
    assert chronos["source"] == "chronos"


def test_delivery_is_a_distinct_execution_bound_to_completed_producer_bytes(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    artifact = tmp_path / "ana.png"
    artifact.write_bytes(b"exact ana bytes")
    digest = f"sha256:{hashlib.sha256(artifact.read_bytes()).hexdigest()}"

    producer = executions.create_execution(
        "ana-live", source="builtin", scheduled_for="2026-08-01T09:10:00+00:00",
    )
    executions.finish_execution(producer["id"], success=True)
    delivery = executions.create_delivery_execution(
        producer_execution_id=producer["id"],
        artifact_path=str(artifact),
        artifact_sha256=digest,
        delivery_targets=[{
            "platform": "telegram", "chat_id": "-100123", "thread_id": "55",
        }],
    )

    assert delivery["id"] != producer["id"]
    assert delivery["kind"] == "delivery"
    assert delivery["parent_execution_id"] == producer["id"]
    assert delivery["artifact_path"] != str(artifact.resolve())
    assert Path(delivery["artifact_path"]).read_bytes() == artifact.read_bytes()
    assert delivery["artifact_sha256"] == digest
    assert delivery["artifact_size_bytes"] == len(b"exact ana bytes")
    assert delivery["delivery_targets"] is None
    assert json.loads(delivery["authorized_delivery_targets"]) == [{
        "chat_id": "-100123", "platform": "telegram", "thread_id": "55",
    }]


def test_delivery_execution_owns_immutable_artifact_bytes(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    source = tmp_path / "mutable.png"
    source.write_bytes(b"claimed bytes")
    producer = _create(executions, "ana-live")
    executions.finish_execution(producer["id"], success=True)

    delivery = executions.create_delivery_execution(
        producer_execution_id=producer["id"],
        artifact_path=str(source),
        artifact_sha256=f"sha256:{hashlib.sha256(source.read_bytes()).hexdigest()}",
        delivery_targets=[{"platform": "telegram", "chat_id": "-100123"}],
    )
    owned = Path(delivery["artifact_path"])
    source.write_bytes(b"mutated after claim")

    assert owned != source.resolve()
    assert owned.read_bytes() == b"claimed bytes"
    assert delivery["artifact_sha256"] == (
        f"sha256:{hashlib.sha256(owned.read_bytes()).hexdigest()}"
    )


def test_dispatch_content_uses_claimed_bytes_after_source_mutation(monkeypatch, tmp_path):
    import cron.scheduler as scheduler
    from gateway.platforms.base import BasePlatformAdapter

    executions = _point_ledger(monkeypatch, tmp_path)
    source = tmp_path / "mutable.png"
    source.write_bytes(b"bytes at claim")
    producer = _create(executions, "ana-live")
    executions.finish_execution(producer["id"], success=True)
    delivery = executions.create_delivery_execution(
        producer_execution_id=producer["id"], artifact_path=str(source),
        artifact_sha256=f"sha256:{hashlib.sha256(source.read_bytes()).hexdigest()}",
        delivery_targets=[{"platform": "telegram", "chat_id": "-100123"}],
    )
    source.write_bytes(b"bytes after claim")

    bound = scheduler._bind_delivery_content_to_execution_artifact(
        f"MEDIA:{source}",
        source_artifact_path=str(source),
        delivery_execution=delivery,
    )
    media, _text = BasePlatformAdapter.extract_media(bound)

    assert Path(media[0][0]).read_bytes() == b"bytes at claim"
    assert Path(media[0][0]) == Path(delivery["artifact_path"])


def test_delivery_execution_rejects_unfinished_parent_and_changed_bytes(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    artifact = tmp_path / "ana.png"
    artifact.write_bytes(b"first")
    digest = f"sha256:{hashlib.sha256(artifact.read_bytes()).hexdigest()}"
    producer = _create(executions, "ana-live")

    with pytest.raises(ValueError, match="terminal producer"):
        executions.create_delivery_execution(
            producer_execution_id=producer["id"],
            artifact_path=str(artifact),
            artifact_sha256=digest,
            delivery_targets=[{"platform": "telegram", "chat_id": "-100123"}],
        )
    executions.finish_execution(producer["id"], success=True)
    artifact.write_bytes(b"changed")
    with pytest.raises(ValueError, match="digest"):
        executions.create_delivery_execution(
            producer_execution_id=producer["id"],
            artifact_path=str(artifact),
            artifact_sha256=digest,
            delivery_targets=[{"platform": "telegram", "chat_id": "-100123"}],
        )


def test_ambiguous_delivery_terminalizes_unknown_without_fabricating_delivery(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    artifact = tmp_path / "ana.png"
    artifact.write_bytes(b"ana")
    digest = f"sha256:{hashlib.sha256(artifact.read_bytes()).hexdigest()}"
    producer = _create(executions, "ana-live")
    executions.finish_execution(producer["id"], success=True)
    delivery = executions.create_delivery_execution(
        producer_execution_id=producer["id"],
        artifact_path=str(artifact),
        artifact_sha256=digest,
        delivery_targets=[{"platform": "telegram", "chat_id": "-100123"}],
    )
    executions.mark_execution_running(delivery["id"])

    ambiguous = executions.mark_execution_ambiguous(
        delivery["id"], error="confirmation timeout after dispatch",
        delivery_receipts=[{
            "requested_target": {
                "platform": "telegram", "chat_id": "-100123", "thread_id": None,
            },
            "actual_target": {
                "platform": "telegram", "chat_id": "-100123", "thread_id": None,
            },
            "status": "ambiguous", "transport": "live",
            "error": "confirmation timeout after dispatch",
            "provider_receipt_id": None,
        }],
    )

    assert ambiguous is not None
    assert ambiguous["status"] == "unknown"
    assert ambiguous["delivery_state"] == "ambiguous"
    assert ambiguous["delivery_status"] is None
    assert ambiguous["delivered_at"] is None


def test_all_delivery_terminalizers_reject_foreign_or_malformed_receipts(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    artifact = tmp_path / "ana.png"
    artifact.write_bytes(b"ana")
    producer = _create(executions, "ana-live")
    executions.finish_execution(producer["id"], success=True)

    def claim_delivery():
        claimed = executions.create_delivery_execution(
            producer_execution_id=producer["id"],
            artifact_path=str(artifact),
            artifact_sha256=f"sha256:{hashlib.sha256(artifact.read_bytes()).hexdigest()}",
            delivery_targets=[{"platform": "telegram", "chat_id": "-100123"}],
        )
        executions.mark_execution_running(claimed["id"])
        return claimed

    foreign = {
        "requested_target": {
            "platform": "telegram", "chat_id": "foreign", "thread_id": None,
        },
        "actual_target": {
            "platform": "telegram", "chat_id": "foreign", "thread_id": None,
        },
        "status": "failed", "transport": "live", "error": "rejected",
        "provider_receipt_id": None,
    }
    with pytest.raises(ValueError, match="authorized targets"):
        executions.finish_execution(
            claim_delivery()["id"], success=False, error="delivery failed",
            delivery_status="failed", delivery_error="delivery failed",
            delivery_receipts=[foreign],
        )

    malformed = {**foreign, "requested_target": {"platform": "telegram"}}
    with pytest.raises(ValueError, match="receipt"):
        executions.mark_execution_ambiguous(
            claim_delivery()["id"], error="unknown after dispatch",
            delivery_receipts=[malformed],
        )


def test_mixed_receipts_keep_authority_actual_routes_and_evidence_separate(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    artifact = tmp_path / "brief.txt"
    artifact.write_bytes(b"brief")
    producer = _create(executions, "brief")
    executions.finish_execution(producer["id"], success=True)
    requested = [
        {"platform": "telegram", "chat_id": "-100123", "thread_id": None},
        {"platform": "discord", "chat_id": "456", "thread_id": None},
    ]
    claimed = executions.create_delivery_execution(
        producer_execution_id=producer["id"], artifact_path=str(artifact),
        artifact_sha256=f"sha256:{hashlib.sha256(artifact.read_bytes()).hexdigest()}",
        delivery_targets=requested,
    )
    executions.mark_execution_running(claimed["id"])
    actual = {"platform": "telegram", "chat_id": "-100123", "thread_id": "new-topic"}
    receipts = [
        {
            "requested_target": requested[0], "actual_target": actual,
            "status": "delivered", "transport": "live", "error": None,
            "provider_receipt_id": "message-1",
        },
        {
            "requested_target": requested[1], "actual_target": requested[1],
            "status": "failed", "transport": "standalone", "error": "rejected",
            "provider_receipt_id": None,
        },
    ]
    failed = executions.finish_execution(
        claimed["id"], success=False, error="partial delivery",
        delivery_status="failed", delivery_error="partial delivery",
        delivery_targets=[actual], delivery_receipts=receipts,
    )

    assert json.loads(failed["authorized_delivery_targets"]) == requested
    assert json.loads(failed["delivery_targets"]) == [actual]
    assert json.loads(failed["delivery_receipts"]) == receipts

    receipts[1]["actual_target"] = {
        "platform": "discord", "chat_id": "foreign", "thread_id": None,
    }
    second = executions.create_delivery_execution(
        producer_execution_id=producer["id"], artifact_path=str(artifact),
        artifact_sha256=f"sha256:{hashlib.sha256(artifact.read_bytes()).hexdigest()}",
        delivery_targets=requested,
    )
    with pytest.raises(ValueError, match="actual target"):
        executions.finish_execution(
            second["id"], success=False, error="partial delivery",
            delivery_status="failed", delivery_error="partial delivery",
            delivery_targets=[actual], delivery_receipts=receipts,
        )


def test_execution_terminal_row_persists_confirmed_delivery_receipt(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    artifact = tmp_path / "delivered.txt"
    artifact.write_bytes(b"delivered")
    producer = _create(executions, "delivered-job")
    executions.finish_execution(producer["id"], success=True)
    claimed = executions.create_delivery_execution(
        producer_execution_id=producer["id"],
        artifact_path=str(artifact),
        artifact_sha256=f"sha256:{hashlib.sha256(artifact.read_bytes()).hexdigest()}",
        delivery_targets=[{
            "platform": "telegram", "chat_id": "-100123", "thread_id": 17,
        }],
    )
    executions.mark_execution_running(claimed["id"])
    receipt = {
        "requested_target": {
            "platform": "telegram", "chat_id": "-100123", "thread_id": "17",
        },
        "actual_target": {
            "platform": "telegram", "chat_id": "-100123", "thread_id": "17",
        },
        "status": "delivered", "transport": "live", "error": None,
        "provider_receipt_id": "message-1",
    }
    completed = executions.finish_execution(
        claimed["id"],
        success=True,
        delivery_status="delivered",
        output_file="/tmp/cron-output.md",
        delivery_targets=[receipt["actual_target"]],
        delivery_receipts=[receipt],
    )
    assert completed is not None
    assert completed["delivery_status"] == "delivered"
    assert completed["delivery_state"] == "delivered"
    assert completed["delivery_error"] is None
    assert completed["delivered_at"]
    assert completed["output_file"] == "/tmp/cron-output.md"
    assert json.loads(completed["delivery_targets"]) == [receipt["actual_target"]]
    assert json.loads(completed["delivery_receipts"]) == [receipt]

    failed_artifact = tmp_path / "failed.txt"
    failed_artifact.write_bytes(b"failed")
    other_producer = _create(executions, "failed-delivery")
    executions.finish_execution(other_producer["id"], success=True)
    other = executions.create_delivery_execution(
        producer_execution_id=other_producer["id"],
        artifact_path=str(failed_artifact),
        artifact_sha256=f"sha256:{hashlib.sha256(failed_artifact.read_bytes()).hexdigest()}",
        delivery_targets=[{"platform": "telegram", "chat_id": "-100123"}],
    )
    failed_receipt = {
        "requested_target": {
            "platform": "telegram", "chat_id": "-100123", "thread_id": None,
        },
        "actual_target": {
            "platform": "telegram", "chat_id": "-100123", "thread_id": None,
        },
        "status": "failed", "transport": "live", "error": "adapter timeout",
        "provider_receipt_id": None,
    }
    failed = executions.finish_execution(
        other["id"],
        success=False,
        error="adapter timeout",
        delivery_status="failed",
        delivery_error="adapter timeout",
        delivery_receipts=[failed_receipt],
    )
    assert failed["status"] == "failed"
    assert failed["delivery_status"] == "failed"
    assert failed["delivery_error"] == "adapter timeout"
    assert failed["delivered_at"] is None

    missing_target = _create(executions, "missing-target")
    with pytest.raises(ValueError, match="delivery evidence requires a delivery execution"):
        executions.finish_execution(
            missing_target["id"], success=True, delivery_status="delivered",
        )


def test_delivery_terminal_evidence_requires_dispatch_and_success_coherence(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    requested = {"platform": "telegram", "chat_id": "-100123", "thread_id": None}

    def claim_delivery(label):
        artifact = tmp_path / f"{label}.txt"
        artifact.write_bytes(label.encode())
        producer = _create(executions, f"producer-{label}")
        executions.finish_execution(producer["id"], success=True)
        delivery = executions.create_delivery_execution(
            producer_execution_id=producer["id"],
            artifact_path=str(artifact),
            artifact_sha256=f"sha256:{hashlib.sha256(artifact.read_bytes()).hexdigest()}",
            delivery_targets=[requested],
        )
        executions.mark_execution_running(delivery["id"])
        return delivery

    delivered_without_transport = {
        "requested_target": requested,
        "actual_target": requested,
        "status": "delivered",
        "transport": "none",
        "error": None,
        "provider_receipt_id": None,
    }
    with pytest.raises(ValueError, match="delivered receipt requires a dispatched transport"):
        delivery = claim_delivery("no-transport")
        executions.finish_execution(
            delivery["id"], success=True, delivery_status="delivered",
            delivery_targets=[requested], delivery_receipts=[delivered_without_transport],
        )

    delivered = {**delivered_without_transport, "transport": "live"}
    with pytest.raises(ValueError, match="success must agree with delivered delivery status"):
        delivery = claim_delivery("false-delivered")
        executions.finish_execution(
            delivery["id"], success=False, error="contradiction",
            delivery_status="delivered", delivery_targets=[requested],
            delivery_receipts=[delivered],
        )

    failed = {
        "requested_target": requested,
        "actual_target": requested,
        "status": "failed",
        "transport": "none",
        "error": "failed before dispatch",
        "provider_receipt_id": None,
    }
    with pytest.raises(ValueError, match="success must agree with failed delivery status"):
        delivery = claim_delivery("true-failed")
        executions.finish_execution(
            delivery["id"], success=True, delivery_status="failed",
            delivery_error="failed before dispatch", delivery_receipts=[failed],
        )


def test_terminal_execution_cannot_be_rewritten(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = _create(executions, "immutable")
    executions.mark_execution_running(record["id"])
    executions.finish_execution(record["id"], success=True)

    assert executions.finish_execution(
        record["id"], success=False, error="late writer"
    ) is None
    assert executions.latest_execution("immutable")["status"] == "completed"


def test_retention_bounds_terminal_history_but_preserves_inflight(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 3)
    inflight = _create(executions, "live")
    executions.mark_execution_running(inflight["id"])
    for index in range(8):
        row = _create(executions, f"done-{index}")
        executions.finish_execution(row["id"], success=True)

    records = executions.list_executions(limit=100)
    assert len([row for row in records if row["status"] == "completed"]) == 3
    assert executions.latest_execution("live")["status"] == "running"


def test_corrupt_store_fails_closed_without_overwrite(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    executions.EXECUTIONS_FILE.parent.mkdir(parents=True)
    executions.EXECUTIONS_FILE.write_bytes(b"not a sqlite database")

    with __import__("pytest").raises(sqlite3.DatabaseError):
        _create(executions, "new")
    assert executions.EXECUTIONS_FILE.read_bytes() == b"not a sqlite database"


def test_execution_history_is_paginated(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    ids = []
    for _index in range(5):
        row = _create(executions, "paged")
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
    row = _create(executions, "cli-job")
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

    record = _create(executions, "job-2", source="chronos")
    failed = executions.finish_execution(record["id"], success=False, error="provider exploded")

    assert failed["status"] == "failed"
    assert failed["error"] == "provider exploded"


def test_recovery_does_not_mark_live_process_execution_unknown(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = _create(executions, "still-live")
    executions.mark_execution_running(record["id"])

    assert executions.recover_interrupted_executions() == 0
    assert executions.latest_execution("still-live")["status"] == "running"


def test_recovery_does_not_mark_other_live_owner_unknown(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = _create(executions, "other-live")
    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        conn.execute(
            "UPDATE executions SET process_id=?, pid=? WHERE id=?",
            ("another-import", os.getpid(), record["id"]),
        )

    assert executions.recover_interrupted_executions() == 0
    assert executions.latest_execution("other-live")["status"] == "claimed"


def test_recovery_rejects_recycled_pid(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = _create(executions, "recycled")
    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        conn.execute(
            "UPDATE executions SET process_id=?, process_started_at=? WHERE id=?",
            ("old-import", -1, record["id"]),
        )

    assert executions.recover_interrupted_executions() == 1
    assert executions.latest_execution("recycled")["status"] == "unknown"


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
            "r=create_execution('restart-job', source='builtin', "
            "scheduled_for='2026-08-01T09:10:00+00:00'); "
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
        "get_execution",
        lambda execution_id: {
            "id": execution_id, "job_id": "job-3", "source": "builtin",
            "scheduled_for": FIRE_AT,
        },
    )
    monkeypatch.setattr(
        scheduler,
        "mark_execution_running",
        lambda execution_id: events.append(("running", execution_id)) or {"id": execution_id},
        raising=False,
    )
    monkeypatch.setattr(
        scheduler,
        "finish_execution",
        lambda execution_id, **kwargs: events.append(("finish", execution_id, kwargs)) or {"id": execution_id},
        raising=False,
    )
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda job, *, defer_agent_teardown=None, **_kw: (True, "output", "response", None),
    )
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_args: None)
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *_args, **_kwargs: None)

    assert scheduler.run_one_job({"id": "job-3", "execution_id": "exec-3"}) is True
    assert events[0] == ("running", "exec-3")
    assert events[-1][0:2] == ("finish", "exec-3")
    assert events[-1][2]["success"] is True


def test_run_one_job_rejects_conflicting_durable_nominal_before_context_or_work(monkeypatch):
    import cron.scheduler as scheduler

    touched = []
    monkeypatch.setattr(
        scheduler,
        "get_execution",
        lambda execution_id: {
            "id": execution_id,
            "job_id": "job-conflict",
            "source": "chronos",
            "scheduled_for": "2026-08-01T09:10:00+00:00",
        },
    )
    monkeypatch.setattr(
        scheduler,
        "_install_cron_execution_context",
        lambda _job: touched.append("context"),
    )
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda *_args, **_kwargs: touched.append("work"),
    )
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(
        scheduler, "mark_execution_running", lambda execution_id: {"id": execution_id},
    )
    monkeypatch.setattr(
        scheduler, "finish_execution", lambda execution_id, **_kwargs: {"id": execution_id},
    )

    with pytest.raises(ValueError, match="conflicts with durable scheduled_for"):
        scheduler.run_one_job({
            "id": "job-conflict",
            "execution_id": "execution-conflict",
            "scheduled_for": "2026-08-01T09:11:00+00:00",
        })
    assert touched == []



def test_run_one_job_records_delivery_on_a_distinct_artifact_bound_execution(monkeypatch):
    import cron.scheduler as scheduler

    finished = []
    monkeypatch.setattr(
        scheduler,
        "get_execution",
        lambda execution_id: {
            "id": execution_id,
            "job_id": "job-delivered",
            "source": "builtin",
            "scheduled_for": FIRE_AT,
        },
    )
    monkeypatch.setattr(
        scheduler, "mark_execution_running", lambda execution_id: {"id": execution_id},
    )
    monkeypatch.setattr(
        scheduler,
        "finish_execution",
        lambda execution_id, **kwargs: finished.append((execution_id, kwargs)) or {"id": execution_id},
    )
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda job, *, defer_agent_teardown=None: (True, "output", "response", None),
    )
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_args: "/tmp/exact-output.md")
    concrete_targets = [{
        "platform": "telegram", "chat_id": "-100123", "thread_id": "17",
    }]
    receipt = {
        "requested_target": concrete_targets[0], "actual_target": concrete_targets[0],
        "status": "delivered", "transport": "live", "error": None,
        "provider_receipt_id": "message-1",
    }
    resolutions = []
    deliveries = []
    monkeypatch.setattr(
        scheduler, "_resolve_delivery_targets",
        lambda _job: resolutions.append(_job["id"]) or concrete_targets,
    )
    monkeypatch.setattr(
        scheduler, "_deliver_result",
        lambda *_args, **kwargs: (
            deliveries.append(kwargs["targets"]), kwargs["receipts"].append(receipt),
            scheduler.DeliveryOutcome(scheduler.DeliveryState.DELIVERED, (receipt,)),
        )[-1],
    )
    monkeypatch.setattr(
        scheduler, "_materialize_delivery_artifact",
        lambda *_args: ("/tmp/exact-image.png", f"sha256:{'a' * 64}"),
    )
    created = []
    monkeypatch.setattr(
        scheduler,
        "create_delivery_execution",
        lambda **kwargs: created.append(kwargs) or {"id": "delivery-exec"},
    )
    monkeypatch.setattr(
        scheduler,
        "_bind_delivery_content_to_execution_artifact",
        lambda content, **_kwargs: content,
    )
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *_args, **_kwargs: None)

    assert scheduler.run_one_job({
        "id": "job-delivered",
        "execution_id": "exec-delivered",
        "deliver": "origin",
    }) is True
    assert finished == [("exec-delivered", {
        "success": True,
        "error": None,
        "output_file": "/tmp/exact-output.md",
    }), ("delivery-exec", {
        "success": True,
        "error": None,
        "delivery_status": "delivered",
        "delivery_error": None,
        "delivery_targets": concrete_targets,
        "delivery_receipts": [receipt],
    })]
    assert created == [{
        "producer_execution_id": "exec-delivered",
        "artifact_path": "/tmp/exact-image.png",
        "artifact_sha256": f"sha256:{'a' * 64}",
        "delivery_targets": concrete_targets,
    }]
    assert resolutions == ["job-delivered"]
    assert deliveries == [concrete_targets]


def test_confirmed_delivery_survives_job_summary_write_failure(monkeypatch):
    import cron.scheduler as scheduler

    finished = []
    monkeypatch.setattr(
        scheduler,
        "get_execution",
        lambda execution_id: {
            "id": execution_id,
            "job_id": "job-delivered",
            "source": "builtin",
            "scheduled_for": "2026-08-01T09:10:00+00:00",
        },
    )
    monkeypatch.setattr(
        scheduler, "mark_execution_running", lambda execution_id: {"id": execution_id},
    )
    monkeypatch.setattr(
        scheduler,
        "finish_execution",
        lambda execution_id, **kwargs: finished.append((execution_id, kwargs)) or {"id": execution_id},
    )
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda job, *, defer_agent_teardown=None: (True, "output", "response", None),
    )
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_args: "/tmp/exact-output.md")
    monkeypatch.setattr(
        scheduler,
        "_resolve_delivery_targets",
        lambda _job: [{"platform": "telegram", "chat_id": "-100123", "thread_id": None}],
    )
    def confirmed_delivery(*_args, **kwargs):
        target = kwargs["targets"][0]
        kwargs["receipts"].append({
            "requested_target": target, "actual_target": target,
            "status": "delivered", "transport": "live", "error": None,
            "provider_receipt_id": "message-2",
        })
        return scheduler.DeliveryOutcome(
            scheduler.DeliveryState.DELIVERED, tuple(kwargs["receipts"]),
        )

    monkeypatch.setattr(scheduler, "_deliver_result", confirmed_delivery)
    monkeypatch.setattr(
        scheduler, "_materialize_delivery_artifact",
        lambda *_args: ("/tmp/exact-image.png", f"sha256:{'b' * 64}"),
    )
    monkeypatch.setattr(
        scheduler, "create_delivery_execution", lambda **_kwargs: {"id": "delivery-exec"},
    )
    monkeypatch.setattr(
        scheduler,
        "_bind_delivery_content_to_execution_artifact",
        lambda content, **_kwargs: content,
    )
    monkeypatch.setattr(
        scheduler,
        "mark_job_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("jobs store unavailable")),
    )

    assert scheduler.run_one_job({
        "id": "job-delivered",
        "execution_id": "exec-delivered",
        "deliver": "origin",
    }) is False
    assert finished[0][1]["success"] is True
    assert finished[1][0] == "delivery-exec"
    assert finished[1][1]["delivery_status"] == "delivered"


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

    record = executions.create_execution(
        "leak-check", source="builtin", scheduled_for=FIRE_AT,
    )
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
        executions.create_execution(
            "init-fail", source="builtin", scheduled_for=FIRE_AT,
        )

    assert len(opened_ids) == 1
    assert len(closed_ids) == 1


def test_job_listing_exposes_latest_execution(monkeypatch, tmp_path):
    import cron.jobs as jobs

    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    executions = _point_ledger(monkeypatch, tmp_path)

    job = jobs.create_job(prompt="audit me", schedule="every 1h", name="audit")
    record = _create(executions, job["id"])
    executions.mark_execution_running(record["id"])

    listed = jobs.list_jobs(include_disabled=True)
    assert listed[0]["latest_execution"]["id"] == record["id"]
    assert listed[0]["latest_execution"]["status"] == "running"
