"""Crash, concurrency, and terminal-state contracts for durable cron delivery."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path


def _point_ledger(monkeypatch, tmp_path):
    import cron.executions as executions

    monkeypatch.setattr(
        executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )
    return executions


def _queued(executions, job_id="job", *, target=None):
    execution = executions.create_execution(job_id, source="builtin")
    target = target or {"platform": "telegram", "chat_id": "1", "thread_id": None}
    executions.enqueue_delivery(
        execution["id"],
        job={"id": job_id, "name": job_id},
        content="persisted result",
        targets=[target],
    )
    return execution, target


def test_result_persisted_before_send_survives_process_restart(tmp_path):
    """Crash after enqueue/before claim leaves a safely retryable pending row."""
    home = tmp_path / "home"
    repo = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env.update({"HERMES_HOME": str(home), "PYTHONPATH": str(repo)})
    create = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cron.executions import create_execution, enqueue_delivery; "
            "e=create_execution('restart-pending', source='builtin'); "
            "enqueue_delivery(e['id'], job={'id':'restart-pending'}, content='result', "
            "targets=[{'platform':'telegram','chat_id':'1'}]); print(e['id'])",
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    execution_id = create.stdout.strip()

    inspect = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from cron.executions import list_due_deliveries; "
            "print(json.dumps(list_due_deliveries()))",
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    due = json.loads(inspect.stdout)
    assert [row["execution_id"] for row in due] == [execution_id]
    assert due[0]["content"] == "result"


def test_two_processes_cannot_claim_same_delivery(tmp_path):
    home = tmp_path / "home"
    gate = tmp_path / "start"
    repo = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env.update({"HERMES_HOME": str(home), "PYTHONPATH": str(repo)})
    create = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cron.executions import create_execution, enqueue_delivery; "
            "e=create_execution('contended', source='builtin'); "
            "enqueue_delivery(e['id'], job={'id':'contended'}, content='result', "
            "targets=[{'platform':'telegram','chat_id':'1'}]); print(e['id'])",
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    execution_id = create.stdout.strip()
    claimant = (
        "import json,time; from pathlib import Path; "
        "from cron.executions import claim_delivery; "
        f"gate=Path({str(gate)!r}); "
        "\nwhile not gate.exists(): time.sleep(0.005)\n"
        f"row=claim_delivery({execution_id!r}); "
        "print(json.dumps({'claimed': row is not None, "
        "'token': row.get('claim_token') if row else None}))"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", claimant],
            cwd=repo,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    gate.touch()
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr
        results.append(json.loads(stdout))

    assert sum(result["claimed"] for result in results) == 1
    assert len({result["token"] for result in results if result["claimed"]}) == 1


def test_live_delivery_owner_is_not_recovered(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    execution, _ = _queued(executions, "live-owner")
    claimed = executions.claim_delivery(execution["id"])

    assert claimed["claim_token"]
    assert executions.recover_interrupted_deliveries() == 0
    assert executions.get_delivery(execution["id"])["status"] == "delivering"


def test_live_owner_without_start_time_is_not_declared_dead(monkeypatch, tmp_path):
    """A live PID with unavailable identity metadata is not proof of death."""
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(executions, "_process_start_time", lambda _pid: None)
    execution, _ = _queued(executions, "live-owner-no-start")
    executions.claim_delivery(execution["id"])
    monkeypatch.setattr(executions, "_PROCESS_ID", "other-reconciler")
    other_pid = os.getpid() + 1
    monkeypatch.setattr(executions.os, "getpid", lambda: other_pid)
    monkeypatch.setattr("gateway.status._pid_exists", lambda _pid: True)

    assert executions.recover_interrupted_deliveries() == 0
    assert executions.get_delivery(execution["id"])["status"] == "delivering"


def test_crash_during_delivery_becomes_unknown_and_never_due(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    execution, _ = _queued(executions, "crash-during-send")
    executions.claim_delivery(execution["id"])
    monkeypatch.setattr(executions, "_PROCESS_ID", "replacement-process")
    monkeypatch.setattr(executions, "_owner_is_live", lambda *_args: False)

    assert executions.recover_interrupted_deliveries() == 1
    recovered = executions.get_delivery(execution["id"])
    assert recovered["status"] == "unknown"
    assert recovered["content"] is None
    assert executions.list_due_deliveries() == []


def test_crash_after_send_before_completion_is_fail_closed(monkeypatch, tmp_path):
    """The ledger cannot distinguish this from an in-flight send: no duplicate."""
    executions = _point_ledger(monkeypatch, tmp_path)
    execution, _ = _queued(executions, "sent-not-acked")
    executions.claim_delivery(execution["id"])
    monkeypatch.setattr(executions, "_PROCESS_ID", "replacement-process")
    monkeypatch.setattr(executions, "_owner_is_live", lambda *_args: False)

    executions.recover_interrupted_deliveries()

    assert executions.get_delivery(execution["id"])["status"] == "unknown"
    assert executions.claim_delivery(execution["id"]) is None


def test_retry_backoff_is_durable_and_matches_sixty_second_tick(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    clock = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(executions, "_hermes_now", lambda: clock)
    execution, target = _queued(executions, "backoff")
    claimed = executions.claim_delivery(execution["id"])

    waiting = executions.finish_delivery_attempt(
        execution["id"],
        claim_token=claimed["claim_token"],
        failed_targets=[target],
        error="temporary outage",
    )

    assert waiting["status"] == "retry_wait"
    assert waiting["next_attempt_at"] == "2026-08-05T12:01:00+00:00"
    assert executions.get_delivery(execution["id"])["status"] == "retry_wait"
    assert executions.list_due_deliveries() == []


def test_permanent_failure_is_not_retried_and_scrubs_payload(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    execution, target = _queued(executions, "auth-failure")
    claimed = executions.claim_delivery(execution["id"])

    failed = executions.finish_delivery_attempt(
        execution["id"],
        claim_token=claimed["claim_token"],
        failed_targets=[],
        error="401 unauthorized",
        permanent_error="telegram: 401 unauthorized",
    )

    assert failed["status"] == "exhausted"
    assert failed["terminal_reason"] == "permanent_failure"
    assert failed["attempt_count"] == 1
    assert failed["content"] is None
    assert failed["job"] is None
    assert failed["targets"] == []
    assert executions.list_due_deliveries() == []
    attempts = executions.list_delivery_attempts(execution["id"])
    assert attempts[0]["targets"] == [target]
    assert "401" in attempts[0]["error"]


def test_cancel_pending_delivery_is_terminal_and_scrubbed(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    execution, _ = _queued(executions, "cancelled")

    cancelled = executions.cancel_delivery(
        execution["id"], reason="job was deleted before retry"
    )

    assert cancelled["status"] == "exhausted"
    assert cancelled["terminal_reason"] == "cancelled"
    assert cancelled["content"] is None
    assert cancelled["job"] is None
    assert executions.list_due_deliveries() == []


def test_deleted_job_is_cancelled_even_during_future_backoff(monkeypatch, tmp_path):
    import cron.scheduler as scheduler

    executions = _point_ledger(monkeypatch, tmp_path)
    clock = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(executions, "_hermes_now", lambda: clock)
    execution, target = _queued(executions, "deleted-in-backoff")
    claimed = executions.claim_delivery(execution["id"])
    executions.finish_delivery_attempt(
        execution["id"],
        claim_token=claimed["claim_token"],
        failed_targets=[target],
        error="temporary outage",
    )
    monkeypatch.setattr(scheduler, "get_job", lambda _job_id: None)

    assert scheduler.cancel_invalid_deliveries() == 1
    cancelled = executions.get_delivery(execution["id"])
    assert cancelled["status"] == "exhausted"
    assert cancelled["terminal_reason"] == "cancelled"
    assert cancelled["content"] is None


def test_profile_delivery_ledgers_are_isolated(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    homes = [tmp_path / "alpha", tmp_path / "beta"]
    for index, home in enumerate(homes):
        env = os.environ.copy()
        env.update({"HERMES_HOME": str(home), "PYTHONPATH": str(repo)})
        subprocess.run(
            [
                sys.executable,
                "-c",
                "from cron.executions import create_execution, enqueue_delivery; "
                f"e=create_execution('job-{index}', source='builtin'); "
                f"enqueue_delivery(e['id'], job={{'id':'job-{index}'}}, content='result', "
                "targets=[{'platform':'telegram','chat_id':'1'}])",
            ],
            cwd=repo,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )

    for index, home in enumerate(homes):
        env = os.environ.copy()
        env.update({"HERMES_HOME": str(home), "PYTHONPATH": str(repo)})
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json; from cron.executions import list_due_deliveries; "
                "print(json.dumps([r['job_id'] for r in list_due_deliveries()]))",
            ],
            cwd=repo,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        assert json.loads(result.stdout) == [f"job-{index}"]


def test_builtin_and_chronos_reconciliation_send_once(monkeypatch, tmp_path):
    """Both provider wake paths may race, but the SQLite claim has one winner."""
    import cron.executions as executions
    import cron.scheduler as scheduler

    monkeypatch.setattr(
        executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )
    execution, target = _queued(executions, "provider-race")
    monkeypatch.setattr(
        scheduler, "get_job", lambda _job_id: {"state": "completed"}, raising=False
    )
    sends = []
    sends_lock = threading.Lock()

    def deliver(*_args, **_kwargs):
        with sends_lock:
            sends.append(_kwargs["targets_override"])
        return scheduler.DeliveryReport(None, [target], [])

    monkeypatch.setattr(scheduler, "_deliver_result", deliver)
    barrier = threading.Barrier(3)
    results = []

    def reconcile():
        barrier.wait()
        results.append(scheduler.retry_due_deliveries())

    threads = [threading.Thread(target=reconcile) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(results) == [0, 1]
    assert sends == [[target]]
    assert executions.get_delivery(execution["id"])["status"] == "delivered"
