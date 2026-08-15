from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys
import time

from task_supervisor.ledger import LedgerPaths, create_task_entry, format_time, load_store
from task_supervisor.transport import NullFailTransport
from task_supervisor.watchdog import run_watchdog


def t(hour: int, minute: int) -> datetime:
    return datetime(2026, 8, 15, hour, minute, tzinfo=UTC)


def write_task(base: Path, **overrides):
    base.mkdir(parents=True, exist_ok=True)
    task = {
        "task_id": "HERBIE-20260815-1200-test-task",
        "title": "Synthetic task",
        "owner": "Steve",
        "spec_filename": "spec.md",
        "spec_path": "/tmp/spec.md",
        "spec_version": "test",
        "spec_sha256": "a" * 64,
        "status": "ACTIVE",
        "created_at": format_time(t(12, 0)),
        "started_at": format_time(t(12, 0)),
        "last_progress_at": format_time(t(12, 0)),
        "last_owner_update_at": format_time(t(12, 0)),
        "next_required_owner_update_at": format_time(t(12, 45)),
        "current_step": "testing",
        "next_step": "next",
        "percent_or_stage_complete": "fixture",
        "blocker_type": None,
        "blocker_detail": None,
        "checkpoint_commit": "abc123",
        "checkpoint_artifact_path": None,
        "tool_budget_risk": "normal",
        "owner_notification_state": "delivered",
        "internal_nudge_state": {},
        "completion_artifact": None,
        "closed_at": None,
    }
    task.update(overrides)
    (base / "active_task.json").write_text(json.dumps(task, indent=2) + "\n")
    return task


def load_task(base: Path):
    return load_store(LedgerPaths(base, base / "active_task.json", base / "events.jsonl", base / "dedupe_state.json"))["tasks"]["HERBIE-20260815-1200-test-task"]


def load_outbox(base: Path):
    return json.loads((base / "notification_outbox.json").read_text())


def load_dedupe(base: Path):
    return json.loads((base / "dedupe_state.json").read_text())


def test_no_task_watchdog_silent(tmp_path):
    decision = run_watchdog(base_dir=tmp_path, now=t(12, 0))
    assert decision.exit_code == 0
    assert decision.stdout == ""


def test_fresh_active_task_silent(tmp_path):
    write_task(tmp_path)
    decision = run_watchdog(base_dir=tmp_path, now=t(12, 10))
    assert decision.exit_code == 0
    assert decision.stdout == ""


def test_active_no_progress_30m_records_one_internal_nudge(tmp_path):
    write_task(tmp_path)
    decision = run_watchdog(base_dir=tmp_path, now=t(12, 30))
    assert decision.stdout == ""
    task = load_task(tmp_path)
    assert task["internal_nudge_state"]["last_nudge_recorded_at"] == "2026-08-15T12:30:00Z"
    assert task["internal_nudge_state"]["auto_resume_status"] == "NOT_CONFIGURED"

    second = run_watchdog(base_dir=tmp_path, now=t(12, 35))
    assert second.stdout == ""
    dedupe = load_dedupe(tmp_path)
    assert sum(1 for v in dedupe["incidents"].values() if v["kind"] == "internal_nudge_recorded") == 1


def test_active_no_owner_update_45m_emits_heartbeat_after_delivery(tmp_path):
    write_task(tmp_path, last_progress_at=format_time(t(12, 40)))
    decision = run_watchdog(base_dir=tmp_path, now=t(12, 45))
    assert "HERBIE TASK STATUS" in decision.stdout
    task = load_task(tmp_path)
    assert task["owner_notification_state"] == "delivered"
    assert task["last_owner_update_at"] == "2026-08-15T12:45:00Z"
    assert task["last_owner_notification_delivered_at"] == "2026-08-15T12:45:00Z"


def test_received_acknowledgement_overdue_after_5m(tmp_path):
    write_task(tmp_path, status="RECEIVED", created_at=format_time(t(12, 0)), last_owner_update_at=None, owner_notification_state="pending_task_accepted_preflight")
    assert run_watchdog(base_dir=tmp_path, now=t(12, 5)).stdout == ""
    decision = run_watchdog(base_dir=tmp_path, now=t(12, 6))
    assert "HERBIE TASK ACKNOWLEDGEMENT OVERDUE" in decision.stdout


def test_waiting_owner_emits_decision_request_once(tmp_path):
    write_task(tmp_path, status="WAITING_OWNER", current_step="Need approval", next_step="Choose A or B", last_owner_update_at=None, owner_notification_state="pending")
    first = run_watchdog(base_dir=tmp_path, now=t(12, 1))
    second = run_watchdog(base_dir=tmp_path, now=t(12, 2))
    assert "HERBIE TASK WAITING OWNER" in first.stdout
    assert second.stdout == ""


def test_aborted_emits_one_closeout(tmp_path):
    write_task(tmp_path, status="ABORTED", blocker_detail="scope withdrawn", owner_notification_state="pending")
    first = run_watchdog(base_dir=tmp_path, now=t(12, 1))
    second = run_watchdog(base_dir=tmp_path, now=t(12, 2))
    assert "HERBIE TASK ABORTED" in first.stdout
    assert second.stdout == ""


def test_blocked_unnotified_alerts_and_dedupes_after_delivery(tmp_path):
    write_task(tmp_path, status="BLOCKED", blocker_type="AUTH", blocker_detail="HTTP 401 Unauthorized", owner_notification_state="pending")
    first = run_watchdog(base_dir=tmp_path, now=t(12, 5))
    assert "HERBIE TASK BLOCKED" in first.stdout
    assert "HTTP 401 Unauthorized" in first.stdout
    second = run_watchdog(base_dir=tmp_path, now=t(12, 20))
    assert second.stdout == ""


def test_transport_failure_remains_pending_and_retries_later(tmp_path):
    write_task(tmp_path, status="BLOCKED", blocker_type="AUTH", blocker_detail="HTTP 401 Unauthorized", owner_notification_state="pending")
    first = run_watchdog(base_dir=tmp_path, now=t(12, 5), transport=NullFailTransport())
    assert first.exit_code == 1
    assert first.stdout == ""
    task = load_task(tmp_path)
    assert task["owner_notification_state"] == "failed_retryable"
    assert task["last_owner_update_at"] == "2026-08-15T12:00:00Z"
    outbox = load_outbox(tmp_path)
    rec = next(iter(outbox["notifications"].values()))
    assert rec["status"] == "failed_retryable"
    assert rec["attempts"] == 1

    second = run_watchdog(base_dir=tmp_path, now=t(12, 6))
    assert "HERBIE TASK BLOCKED" in second.stdout
    rec = next(iter(load_outbox(tmp_path)["notifications"].values()))
    assert rec["status"] == "delivered"
    assert rec["attempts"] == 2


def test_heartbeat_clock_not_updated_on_transport_failure(tmp_path):
    write_task(tmp_path, last_progress_at=format_time(t(12, 40)))
    decision = run_watchdog(base_dir=tmp_path, now=t(12, 45), transport=NullFailTransport())
    assert decision.exit_code == 1
    task = load_task(tmp_path)
    assert task["last_owner_update_at"] == "2026-08-15T12:00:00Z"
    assert task["last_owner_notification_attempt_at"] == "2026-08-15T12:45:00Z"
    assert task.get("last_owner_notification_delivered_at") is None


def test_blocked_to_active_transition_emits_one_recovery_per_episode(tmp_path):
    write_task(tmp_path, status="BLOCKED", blocker_type="AUTH", blocker_detail="HTTP 401 Unauthorized", owner_notification_state="pending")
    assert "HERBIE TASK BLOCKED" in run_watchdog(base_dir=tmp_path, now=t(12, 5)).stdout
    store = load_store(LedgerPaths(tmp_path, tmp_path / "active_task.json", tmp_path / "events.jsonl", tmp_path / "dedupe_state.json"))
    task = store["tasks"]["HERBIE-20260815-1200-test-task"]
    task.update({"status": "ACTIVE", "blocker_type": None, "blocker_detail": None, "last_progress_at": format_time(t(12, 10)), "last_owner_update_at": format_time(t(12, 10)), "owner_notification_state": "delivered"})
    (tmp_path / "tasks.json").write_text(json.dumps(store, indent=2) + "\n")
    first = run_watchdog(base_dir=tmp_path, now=t(12, 11))
    assert "HERBIE TASK RESUMED" in first.stdout
    task["last_progress_at"] = format_time(t(12, 12))
    task["current_step"] = "ordinary progress"
    (tmp_path / "tasks.json").write_text(json.dumps(store, indent=2) + "\n")
    second = run_watchdog(base_dir=tmp_path, now=t(12, 13))
    assert "HERBIE TASK RESUMED" not in second.stdout

    task.update({"status": "BLOCKED", "blocker_detail": "new blocker", "blocker_type": "OTHER", "owner_notification_state": "pending"})
    (tmp_path / "tasks.json").write_text(json.dumps(store, indent=2) + "\n")
    assert "HERBIE TASK BLOCKED" in run_watchdog(base_dir=tmp_path, now=t(12, 14)).stdout
    task.update({"status": "ACTIVE", "blocker_detail": None, "blocker_type": None, "last_progress_at": format_time(t(12, 15))})
    (tmp_path / "tasks.json").write_text(json.dumps(store, indent=2) + "\n")
    assert "HERBIE TASK RESUMED" in run_watchdog(base_dir=tmp_path, now=t(12, 16)).stdout


def test_ready_for_review_emits_owner_notification(tmp_path):
    write_task(tmp_path, status="READY_FOR_INDEPENDENT_REVIEW", completion_artifact="https://github.example/pr/1", owner_notification_state="pending")
    decision = run_watchdog(base_dir=tmp_path, now=t(12, 1))
    assert "HERBIE TASK READY FOR REVIEW" in decision.stdout


def test_complete_notifies_records_closed_and_dedupes(tmp_path):
    write_task(tmp_path, status="COMPLETE", completion_artifact="artifact.zip", owner_notification_state="pending")
    first = run_watchdog(base_dir=tmp_path, now=t(12, 1))
    assert "HERBIE TASK COMPLETE" in first.stdout
    assert load_task(tmp_path)["closed_at"] == "2026-08-15T12:01:00Z"
    assert run_watchdog(base_dir=tmp_path, now=t(12, 2)).stdout == ""


def test_active_task_disappearing_progress_gets_critical_stale_alert(tmp_path):
    write_task(tmp_path)
    decision = run_watchdog(base_dir=tmp_path, now=t(13, 0))
    assert "HERBIE TASK STALE — OWNER ATTENTION" in decision.stdout


def test_preflight_cannot_remain_stale(tmp_path):
    write_task(tmp_path, status="PREFLIGHT", last_progress_at=format_time(t(12, 0)), owner_notification_state="pending")
    decision = run_watchdog(base_dir=tmp_path, now=t(12, 31))
    assert "HERBIE TASK BLOCKED" in decision.stdout
    task = load_task(tmp_path)
    assert task["status"] == "BLOCKED"
    assert task["blocker_type"] == "PREFLIGHT_STALE"


def test_missing_spec_provenance_blocks_execution(tmp_path):
    write_task(tmp_path, spec_sha256="", owner_notification_state="pending")
    decision = run_watchdog(base_dir=tmp_path, now=t(12, 1))
    assert "HERBIE TASK BLOCKED" in decision.stdout
    assert load_task(tmp_path)["blocker_type"] == "SPECIFICATION_AUTHORITY"


def test_tool_budget_stop_state_notifies(tmp_path):
    write_task(tmp_path, status="BLOCKED", blocker_type="TOOL_BUDGET", blocker_detail="tool budget low; checkpoint created", checkpoint_artifact_path="/tmp/checkpoint.patch", owner_notification_state="pending")
    decision = run_watchdog(base_dir=tmp_path, now=t(12, 1))
    assert "tool budget low" in decision.stdout
    assert "/tmp/checkpoint.patch" in decision.stdout


def test_queued_task_does_not_replace_active_and_notice_delivered_once(tmp_path):
    active = write_task(tmp_path, status="ACTIVE", current_step="first task running")
    paths = LedgerPaths(tmp_path, tmp_path / "active_task.json", tmp_path / "events.jsonl", tmp_path / "dedupe_state.json")
    queued = create_task_entry(paths, task_id="HERBIE-20260815-1201-second-task", title="Second task", owner="Steve", spec_filename="second.md", spec_path="/tmp/second.md", spec_version="test", spec_sha256="b" * 64, now=t(12, 1))
    store = load_store(paths)
    assert store["active_task_id"] == active["task_id"]
    assert store["tasks"][active["task_id"]]["current_step"] == "first task running"
    assert queued["status"] == "QUEUED"
    decision = run_watchdog(base_dir=tmp_path, now=t(12, 2))
    assert "HERBIE TASK QUEUED" in decision.stdout
    assert "Second task" in decision.stdout
    assert run_watchdog(base_dir=tmp_path, now=t(12, 3)).stdout == ""
    store = load_store(paths)
    assert "HERBIE-20260815-1201-second-task" in store["queue"]


def test_parallel_task_can_be_received_when_owner_authorizes_parallelism(tmp_path):
    write_task(tmp_path, status="ACTIVE", current_step="first task running")
    paths = LedgerPaths(tmp_path, tmp_path / "active_task.json", tmp_path / "events.jsonl", tmp_path / "dedupe_state.json")
    task = create_task_entry(paths, task_id="HERBIE-20260815-1201-second-task", title="Second task", owner="Steve", spec_filename="second.md", spec_path="/tmp/second.md", spec_version="test", spec_sha256="b" * 64, now=t(12, 1), parallel_authorized=True)
    assert task["status"] == "RECEIVED"
    assert load_store(paths)["active_task_id"] == task["task_id"]


def test_malformed_ledger_fails_closed(tmp_path):
    (tmp_path / "tasks.json").write_text("{not-json")
    decision = run_watchdog(base_dir=tmp_path, now=t(12, 1))
    assert decision.exit_code == 1
    assert "WATCHDOG FAILURE" in decision.stdout


def test_lock_contention_is_safe(tmp_path):
    paths = LedgerPaths(tmp_path, tmp_path / "active_task.json", tmp_path / "events.jsonl", tmp_path / "dedupe_state.json")
    write_task(tmp_path, status="BLOCKED", blocker_type="AUTH", blocker_detail="HTTP 401", owner_notification_state="pending")
    from task_supervisor.ledger import supervisor_lock
    with supervisor_lock(paths):
        decision = run_watchdog(base_dir=tmp_path, now=t(12, 1), lock_timeout_seconds=0.05)
    assert decision.exit_code == 1
    assert "LOCK BUSY" in decision.stdout


def test_concurrent_two_processes_produce_one_notification_and_valid_events(tmp_path):
    write_task(tmp_path, status="BLOCKED", blocker_type="AUTH", blocker_detail="HTTP 401", owner_notification_state="pending")
    cmd = [sys.executable, "scripts/herbie_task_supervisor_watchdog.py", "--base-dir", str(tmp_path), "--now", "2026-08-15T12:01:00Z", "--transport", "stdout-confirmed"]
    p1 = subprocess.Popen(cmd, cwd=Path(__file__).resolve().parents[2], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    p2 = subprocess.Popen(cmd, cwd=Path(__file__).resolve().parents[2], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out1, err1 = p1.communicate(timeout=10)
    out2, err2 = p2.communicate(timeout=10)
    assert p1.returncode == 0, err1
    assert p2.returncode == 0, err2
    assert sum("HERBIE TASK BLOCKED" in out for out in [out1, out2]) == 1
    for line in (tmp_path / "events.jsonl").read_text().splitlines():
        json.loads(line)


def test_task_registration_entrypoint_preserves_active_and_queues(tmp_path):
    write_task(tmp_path, status="ACTIVE", current_step="first task running")
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n")
    cmd = [sys.executable, "scripts/herbie_task_supervisor_task.py", "start", "--base-dir", str(tmp_path), "--task-id", "HERBIE-20260815-1201-second-task", "--title", "Second task", "--spec-path", str(spec), "--spec-version", "test"]
    res = subprocess.run(cmd, cwd=Path(__file__).resolve().parents[2], text=True, capture_output=True, timeout=10)
    assert res.returncode == 0, res.stderr
    assert "status=QUEUED" in res.stdout
    store = load_store(LedgerPaths(tmp_path, tmp_path / "active_task.json", tmp_path / "events.jsonl", tmp_path / "dedupe_state.json"))
    assert store["active_task_id"] == "HERBIE-20260815-1200-test-task"
    assert "HERBIE-20260815-1201-second-task" in store["queue"]


def test_disabled_manifest_pins_runtime_and_no_agent():
    manifest = json.loads((Path(__file__).resolve().parents[2] / "cron-manifests/herbie_active_task_supervisor.disabled.json").read_text())
    assert manifest["no_agent"] is True
    assert manifest["schedule"] == "*/15 * * * *"
    assert manifest["enabled"] is False
    assert manifest["workdir"]
    assert "--transport send-message" in manifest["script"]
    assert manifest["deliver"] == "local"


def test_supervisor_has_no_customer_or_prospect_capability():
    root = Path(__file__).resolve().parents[2]
    text = "\n".join((root / rel).read_text() for rel in ["task_supervisor/watchdog.py", "task_supervisor/transport.py", "scripts/herbie_task_supervisor_task.py"])
    forbidden = ["race_director", "audit_requests", "supabase", "private_mockup_sent", "prospect"]
    assert not any(token in text for token in forbidden)
