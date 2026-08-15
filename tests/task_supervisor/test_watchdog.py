from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path

from task_supervisor.ledger import LedgerPaths, create_task_entry, format_time
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
    return json.loads((base / "active_task.json").read_text())


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
    assert task["internal_nudge_state"]["last_nudge_at"] == "2026-08-15T12:30:00Z"
    assert "TASK SUPERVISOR" in task["internal_nudge_state"]["message"]

    second = run_watchdog(base_dir=tmp_path, now=t(12, 35))
    assert second.stdout == ""
    dedupe = load_dedupe(tmp_path)
    assert sum(1 for v in dedupe["incidents"].values() if v["kind"] == "internal_nudge") == 1


def test_active_no_owner_update_45m_emits_heartbeat(tmp_path):
    write_task(tmp_path, last_progress_at=format_time(t(12, 40)))
    decision = run_watchdog(base_dir=tmp_path, now=t(12, 45))
    assert "HERBIE TASK STATUS" in decision.stdout
    task = load_task(tmp_path)
    assert task["owner_notification_state"] == "emitted_pending_transport"


def test_blocked_unnotified_alerts_and_dedupes(tmp_path):
    write_task(
        tmp_path,
        status="BLOCKED",
        blocker_type="AUTH",
        blocker_detail="HTTP 401 Unauthorized",
        owner_notification_state="pending",
    )
    first = run_watchdog(base_dir=tmp_path, now=t(12, 5))
    assert "HERBIE TASK BLOCKED" in first.stdout
    assert "HTTP 401 Unauthorized" in first.stdout
    second = run_watchdog(base_dir=tmp_path, now=t(12, 20))
    assert second.stdout == ""


def test_blocked_to_active_transition_emits_one_recovery(tmp_path):
    write_task(
        tmp_path,
        status="BLOCKED",
        blocker_type="AUTH",
        blocker_detail="HTTP 401 Unauthorized",
        owner_notification_state="pending",
    )
    assert "HERBIE TASK BLOCKED" in run_watchdog(base_dir=tmp_path, now=t(12, 5)).stdout
    task = load_task(tmp_path)
    task.update(
        {
            "status": "ACTIVE",
            "blocker_type": None,
            "blocker_detail": None,
            "last_progress_at": format_time(t(12, 10)),
            "last_owner_update_at": format_time(t(12, 10)),
            "owner_notification_state": "delivered",
        }
    )
    (tmp_path / "active_task.json").write_text(json.dumps(task, indent=2) + "\n")
    first = run_watchdog(base_dir=tmp_path, now=t(12, 11))
    assert "HERBIE TASK RESUMED" in first.stdout
    second = run_watchdog(base_dir=tmp_path, now=t(12, 12))
    assert second.stdout == ""


def test_ready_for_review_emits_owner_notification(tmp_path):
    write_task(
        tmp_path,
        status="READY_FOR_INDEPENDENT_REVIEW",
        completion_artifact="https://github.example/pr/1",
        owner_notification_state="pending",
    )
    decision = run_watchdog(base_dir=tmp_path, now=t(12, 1))
    assert "HERBIE TASK READY FOR REVIEW" in decision.stdout


def test_complete_notifies_records_closed_and_dedupes(tmp_path):
    write_task(
        tmp_path,
        status="COMPLETE",
        completion_artifact="artifact.zip",
        owner_notification_state="pending",
    )
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
    write_task(
        tmp_path,
        status="BLOCKED",
        blocker_type="TOOL_BUDGET",
        blocker_detail="tool budget low; checkpoint created",
        checkpoint_artifact_path="/tmp/checkpoint.patch",
        owner_notification_state="pending",
    )
    decision = run_watchdog(base_dir=tmp_path, now=t(12, 1))
    assert "tool budget low" in decision.stdout
    assert "/tmp/checkpoint.patch" in decision.stdout


def test_parallel_task_is_queued_state_and_silent_until_selected(tmp_path):
    write_task(tmp_path, status="ACTIVE", current_step="first task running")
    paths = LedgerPaths(tmp_path, tmp_path / "active_task.json", tmp_path / "events.jsonl", tmp_path / "dedupe_state.json")
    queued = create_task_entry(
        paths,
        task_id="HERBIE-20260815-1201-second-task",
        title="Second task",
        owner="Steve",
        spec_filename="second.md",
        spec_path="/tmp/second.md",
        spec_version="test",
        spec_sha256="b" * 64,
        now=t(12, 1),
    )
    assert queued["status"] == "QUEUED"
    assert queued["owner_notification_state"] == "pending_queued_notice"
    decision = run_watchdog(base_dir=tmp_path, now=t(12, 2))
    assert decision.stdout == ""


def test_parallel_task_can_be_received_when_owner_authorizes_parallelism(tmp_path):
    write_task(tmp_path, status="ACTIVE", current_step="first task running")
    paths = LedgerPaths(tmp_path, tmp_path / "active_task.json", tmp_path / "events.jsonl", tmp_path / "dedupe_state.json")
    task = create_task_entry(
        paths,
        task_id="HERBIE-20260815-1201-second-task",
        title="Second task",
        owner="Steve",
        spec_filename="second.md",
        spec_path="/tmp/second.md",
        spec_version="test",
        spec_sha256="b" * 64,
        now=t(12, 1),
        parallel_authorized=True,
    )
    assert task["status"] == "RECEIVED"


def test_watchdog_failure_exits_nonzero(monkeypatch, tmp_path):
    import task_supervisor.watchdog as watchdog

    def boom(_paths):
        raise RuntimeError("ledger unreadable")

    monkeypatch.setattr(watchdog, "load_task", boom)
    decision = watchdog.run_watchdog(base_dir=tmp_path, now=t(12, 1))
    assert decision.exit_code == 1
    assert "WATCHDOG FAILURE" in decision.stdout
