"""Deterministic no-agent watchdog for Herbie active owner tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

from .ledger import (
    LedgerPaths,
    append_event,
    default_paths,
    fingerprint,
    format_time,
    get_active_task,
    load_dedupe,
    load_outbox,
    load_store,
    minutes_between,
    save_dedupe,
    save_outbox,
    save_store,
    supervisor_lock,
    utc_now,
    validate_task,
)
from .transport import OwnerTransport, transport_from_name

OWNER_NOTIFICATION_DELIVERED = "delivered"
OWNER_NOTIFICATION_ATTEMPTED = "attempted_pending_transport"
OWNER_NOTIFICATION_FAILED = "failed_retryable"


@dataclass
class WatchdogDecision:
    stdout: str = ""
    exit_code: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)
    task_saved: bool = False
    dedupe_saved: bool = False
    outbox_saved: bool = False
    delivered: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def _incident_seen(dedupe: dict[str, Any], key: str) -> bool:
    return key in dedupe.setdefault("incidents", {})


def _mark_incident(dedupe: dict[str, Any], key: str, now: datetime, kind: str) -> None:
    dedupe.setdefault("incidents", {})[key] = {"kind": kind, "first_recorded_at": format_time(now)}


def _task_name(task: dict[str, Any]) -> str:
    return f"{task.get('task_id', 'UNKNOWN')} — {task.get('title', 'Untitled task')}"


def _notify_message(kind: str, task: dict[str, Any], now: datetime, *, active_task: dict[str, Any] | None = None, test_label: str | None = None) -> str:
    task_name = _task_name(task)
    prefix = f"{test_label}\n" if test_label else ""
    if kind == "ack_overdue":
        return prefix + (
            f"HERBIE TASK ACKNOWLEDGEMENT OVERDUE — {task_name}\n"
            f"- State: RECEIVED\n"
            f"- Created: {task.get('created_at')}\n"
            f"- Required action: Herbie must acknowledge, run preflight, or declare BLOCKED."
        )
    if kind == "waiting_owner":
        return prefix + (
            f"HERBIE TASK WAITING OWNER — {task_name}\n"
            f"- Decision needed: {task.get('next_step') or 'owner decision required'}\n"
            f"- Current step: {task.get('current_step')}\n"
            f"- Context: {task.get('blocker_detail') or task.get('percent_or_stage_complete') or 'none recorded'}"
        )
    if kind == "queued":
        active = _task_name(active_task) if active_task else "none recorded"
        return prefix + (
            f"HERBIE TASK QUEUED — {task_name}\n"
            f"- Currently active task: {active}\n"
            f"- Reason queued: {task.get('current_step') or 'another consequential task is active'}\n"
            f"- Owner action required: authorize promotion/parallelism after active task closeout."
        )
    if kind == "heartbeat":
        return prefix + (
            f"HERBIE TASK STATUS — {task_name}\n"
            f"- State: {task.get('status')}\n"
            f"- Current milestone: {task.get('percent_or_stage_complete')}\n"
            f"- Completed: last progress at {task.get('last_progress_at')}\n"
            f"- Working on: {task.get('current_step')}\n"
            f"- Blocker: {task.get('blocker_detail') or 'none known'}\n"
            f"- Last checkpoint: {task.get('checkpoint_commit') or task.get('checkpoint_artifact_path') or 'not recorded'}\n"
            f"- Next expected milestone: {task.get('next_step')}"
        )
    if kind == "blocked":
        return prefix + (
            f"HERBIE TASK BLOCKED — {task_name}\n"
            f"- Failure stage: {task.get('current_step')}\n"
            f"- Exact blocker: {task.get('blocker_detail')}\n"
            f"- Work preserved: {task.get('checkpoint_artifact_path') or task.get('checkpoint_commit') or 'not recorded'}\n"
            f"- Side effects: task-supervisor ledger only; no external business side effects recorded\n"
            f"- Owner decision/action required: {task.get('next_step') or 'review blocker'}"
        )
    if kind == "recovery":
        return prefix + (
            f"HERBIE TASK RESUMED — {task_name}\n"
            f"- State: ACTIVE\n"
            f"- Current milestone: {task.get('percent_or_stage_complete')}\n"
            f"- Working on: {task.get('current_step')}\n"
            f"- Next expected milestone: {task.get('next_step')}"
        )
    if kind == "ready":
        return prefix + (
            f"HERBIE TASK READY FOR REVIEW — {task_name}\n"
            f"- Result: {task.get('percent_or_stage_complete')}\n"
            f"- Evidence: {task.get('completion_artifact') or task.get('checkpoint_artifact_path') or 'see repository PR/checks'}\n"
            f"- Commit/artifact: {task.get('checkpoint_commit') or 'not recorded'}\n"
            f"- Remaining limitations: {task.get('blocker_detail') or 'none recorded'}\n"
            f"- Unauthorized next steps remain stopped."
        )
    if kind == "complete":
        return prefix + (
            f"HERBIE TASK COMPLETE — {task_name}\n"
            f"- Result: {task.get('percent_or_stage_complete')}\n"
            f"- Evidence: {task.get('completion_artifact') or task.get('checkpoint_artifact_path') or 'see repository PR/checks'}\n"
            f"- Commit/artifact: {task.get('checkpoint_commit') or 'not recorded'}\n"
            f"- Unauthorized next steps remain stopped."
        )
    if kind == "aborted":
        return prefix + (
            f"HERBIE TASK ABORTED — {task_name}\n"
            f"- Final state: ABORTED\n"
            f"- Reason: {task.get('blocker_detail') or task.get('current_step') or 'not recorded'}\n"
            f"- Preserved artifact: {task.get('checkpoint_artifact_path') or task.get('checkpoint_commit') or 'not recorded'}"
        )
    if kind == "stale_owner":
        elapsed = minutes_between(now, task.get("last_progress_at"))
        elapsed_text = "unknown" if elapsed is None else f"{elapsed:.0f} minutes"
        return prefix + (
            f"HERBIE TASK STALE — OWNER ATTENTION\n"
            f"- Task: {task_name}\n"
            f"- State: {task.get('status')}\n"
            f"- Last known step: {task.get('current_step')}\n"
            f"- Last progress: {task.get('last_progress_at')} ({elapsed_text} ago)\n"
            f"- Last checkpoint: {task.get('checkpoint_commit') or task.get('checkpoint_artifact_path') or 'not recorded'}\n"
            f"- Required action: Herbie must update state, continue, or declare BLOCKED."
        )
    return ""


def _outbox_record(outbox: dict[str, Any], key: str, kind: str, task: dict[str, Any], message: str, now: datetime) -> dict[str, Any]:
    records = outbox.setdefault("notifications", {})
    record = records.get(key)
    if record is None:
        record = {
            "incident_key": key,
            "kind": kind,
            "task_id": task.get("task_id"),
            "status": "pending",
            "message": message,
            "created_at": format_time(now),
            "attempts": 0,
            "last_attempt_at": None,
            "delivered_at": None,
            "last_error": None,
            "transport": None,
            "provider_id": None,
        }
        records[key] = record
    else:
        record["message"] = message
    return record


def _notify_once(
    *,
    kind: str,
    key: str,
    task: dict[str, Any],
    store: dict[str, Any],
    outbox: dict[str, Any],
    dedupe: dict[str, Any],
    now: datetime,
    decision: WatchdogDecision,
    transport: OwnerTransport,
    test_label: str | None = None,
) -> bool:
    active_task = get_active_task(store)
    message = _notify_message(kind, task, now, active_task=active_task, test_label=test_label)
    if not message:
        return False
    record = _outbox_record(outbox, key, kind, task, message, now)
    if record.get("status") == "delivered":
        return False

    record["status"] = "attempted"
    record["attempts"] = int(record.get("attempts") or 0) + 1
    record["last_attempt_at"] = format_time(now)
    task["last_owner_notification_attempt_at"] = format_time(now)
    task["owner_notification_state"] = OWNER_NOTIFICATION_ATTEMPTED
    result = transport.send(message)
    record["transport"] = result.transport
    record["provider_id"] = result.provider_id
    if result.transport == "stdout-confirmed":
        if decision.stdout:
            decision.stdout += "\n\n"
        decision.stdout += message
    if result.success:
        record["status"] = "delivered"
        record["delivered_at"] = format_time(now)
        record["last_error"] = None
        task["owner_notification_state"] = OWNER_NOTIFICATION_DELIVERED
        task["last_owner_update_at"] = format_time(now)
        task["last_owner_notification_delivered_at"] = format_time(now)
        _mark_incident(dedupe, key, now, kind)
        decision.delivered.append(key)
        decision.events.append({"at": format_time(now), "task_id": task.get("task_id"), "event": "owner_notification_delivered", "kind": kind, "incident_key": key, "transport": result.transport, "provider_id": result.provider_id})
        return True

    record["status"] = "failed_retryable"
    record["last_error"] = result.detail
    task["owner_notification_state"] = OWNER_NOTIFICATION_FAILED
    decision.exit_code = 1
    decision.failed.append(key)
    decision.events.append({"at": format_time(now), "task_id": task.get("task_id"), "event": "owner_notification_failed", "kind": kind, "incident_key": key, "transport": result.transport, "detail": result.detail})
    return False


def _open_blocker(dedupe: dict[str, Any], task: dict[str, Any], key: str, now: datetime) -> None:
    blockers = dedupe.setdefault("blockers", {})
    blockers.setdefault(key, {"task_id": task.get("task_id"), "fingerprint": key.rsplit(":", 1)[-1], "status": "open", "opened_at": format_time(now), "delivered": False, "resolved_at": None, "recovery_delivered_at": None})


def _unrecovered_blockers(dedupe: dict[str, Any], task_id: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for key, rec in dedupe.setdefault("blockers", {}).items():
        if rec.get("task_id") == task_id and rec.get("status") == "open" and not rec.get("recovery_delivered_at"):
            out.append((key, rec))
    return out


def _evaluate_task(task: dict[str, Any], store: dict[str, Any], dedupe: dict[str, Any], outbox: dict[str, Any], now: datetime, decision: WatchdogDecision, transport: OwnerTransport, test_label: str | None) -> None:
    errors = validate_task(task)
    if errors:
        task["status"] = "BLOCKED"
        task["blocker_type"] = "SPECIFICATION_AUTHORITY" if any("spec" in e for e in errors) else "STATE_VALIDATION"
        task["blocker_detail"] = "; ".join(errors)
        task["last_progress_at"] = format_time(now)
        task["owner_notification_state"] = "pending"

    status = task.get("status")
    task_id = task["task_id"]

    if status == "RECEIVED":
        age = minutes_between(now, task.get("created_at"))
        if age is not None and age > 5:
            _notify_once(kind="ack_overdue", key=f"{task_id}:ACK_OVERDUE", task=task, store=store, outbox=outbox, dedupe=dedupe, now=now, decision=decision, transport=transport, test_label=test_label)

    elif status == "PREFLIGHT":
        age = minutes_between(now, task.get("last_progress_at"))
        if age is not None and age >= 30:
            task["status"] = "BLOCKED"
            task["blocker_type"] = "PREFLIGHT_STALE"
            task["blocker_detail"] = "preflight did not transition to ACTIVE, BLOCKED, or WAITING_OWNER within 30 minutes"
            task["last_progress_at"] = format_time(now)
            key = f"{task_id}:BLOCKED:{fingerprint(task.get('blocker_detail'))}"
            _open_blocker(dedupe, task, key, now)
            if _notify_once(kind="blocked", key=key, task=task, store=store, outbox=outbox, dedupe=dedupe, now=now, decision=decision, transport=transport, test_label=test_label):
                dedupe["blockers"][key]["delivered"] = True

    elif status == "WAITING_OWNER":
        _notify_once(kind="waiting_owner", key=f"{task_id}:WAITING_OWNER:{fingerprint(task.get('current_step'), task.get('next_step'))}", task=task, store=store, outbox=outbox, dedupe=dedupe, now=now, decision=decision, transport=transport, test_label=test_label)

    elif status == "QUEUED":
        _notify_once(kind="queued", key=f"{task_id}:QUEUED", task=task, store=store, outbox=outbox, dedupe=dedupe, now=now, decision=decision, transport=transport, test_label=test_label)

    elif status == "ACTIVE":
        for blocker_key, blocker in _unrecovered_blockers(dedupe, task_id):
            recovery_key = f"{task_id}:RECOVERY:{blocker_key.rsplit(':', 1)[-1]}"
            if _notify_once(kind="recovery", key=recovery_key, task=task, store=store, outbox=outbox, dedupe=dedupe, now=now, decision=decision, transport=transport, test_label=test_label):
                blocker["status"] = "resolved"
                blocker["resolved_at"] = format_time(now)
                blocker["recovery_delivered_at"] = format_time(now)

        stale_minutes = minutes_between(now, task.get("last_progress_at"))
        nudge_key = f"{task_id}:INTERNAL_NUDGE:{fingerprint(task.get('last_progress_at'))}"
        if stale_minutes is not None and stale_minutes >= 30 and not _incident_seen(dedupe, nudge_key):
            _mark_incident(dedupe, nudge_key, now, "internal_nudge_recorded")
            task.setdefault("internal_nudge_state", {})["last_nudge_recorded_at"] = format_time(now)
            task.setdefault("internal_nudge_state", {})["auto_resume_status"] = "NOT_CONFIGURED"
            task.setdefault("internal_nudge_state", {})["message"] = "TASK SUPERVISOR: update task state, checkpoint progress, and continue or declare BLOCKED."
            decision.events.append({"at": format_time(now), "task_id": task_id, "event": "internal_nudge_recorded", "auto_resume_status": "NOT_CONFIGURED"})

        owner_minutes = minutes_between(now, task.get("last_owner_update_at"))
        if owner_minutes is None or owner_minutes >= 45:
            _notify_once(kind="heartbeat", key=f"{task_id}:HEARTBEAT:{fingerprint(task.get('last_owner_update_at'), task.get('current_step'))}", task=task, store=store, outbox=outbox, dedupe=dedupe, now=now, decision=decision, transport=transport, test_label=test_label)

        if stale_minutes is not None and stale_minutes >= 60:
            _notify_once(kind="stale_owner", key=f"{task_id}:CRITICAL_STALE:{fingerprint(task.get('last_progress_at'))}", task=task, store=store, outbox=outbox, dedupe=dedupe, now=now, decision=decision, transport=transport, test_label=test_label)

    elif status == "BLOCKED":
        key = f"{task_id}:BLOCKED:{fingerprint(task.get('blocker_detail'))}"
        _open_blocker(dedupe, task, key, now)
        if _notify_once(kind="blocked", key=key, task=task, store=store, outbox=outbox, dedupe=dedupe, now=now, decision=decision, transport=transport, test_label=test_label):
            dedupe["blockers"][key]["delivered"] = True

    elif status == "READY_FOR_INDEPENDENT_REVIEW":
        _notify_once(kind="ready", key=f"{task_id}:READY:{fingerprint(task.get('completion_artifact'), task.get('checkpoint_commit'))}", task=task, store=store, outbox=outbox, dedupe=dedupe, now=now, decision=decision, transport=transport, test_label=test_label)

    elif status == "COMPLETE":
        if _notify_once(kind="complete", key=f"{task_id}:COMPLETE:{fingerprint(task.get('completion_artifact'), task.get('checkpoint_commit'))}", task=task, store=store, outbox=outbox, dedupe=dedupe, now=now, decision=decision, transport=transport, test_label=test_label):
            task["closed_at"] = task.get("closed_at") or format_time(now)

    elif status == "ABORTED":
        if _notify_once(kind="aborted", key=f"{task_id}:ABORTED:{fingerprint(task.get('blocker_detail'))}", task=task, store=store, outbox=outbox, dedupe=dedupe, now=now, decision=decision, transport=transport, test_label=test_label):
            task["closed_at"] = task.get("closed_at") or format_time(now)


def run_watchdog(
    *,
    base_dir: Path | None = None,
    now: datetime | None = None,
    transport: OwnerTransport | None = None,
    transport_name: str | None = None,
    owner_target: str | None = None,
    test_label: str | None = None,
    lock_timeout_seconds: float = 5.0,
) -> WatchdogDecision:
    """Evaluate task store and send transport-confirmed owner notifications."""

    paths: LedgerPaths = default_paths(base_dir)
    now = (now or utc_now()).astimezone(UTC).replace(microsecond=0)
    decision = WatchdogDecision()
    transport = transport or transport_from_name(transport_name, target=owner_target)

    try:
        with supervisor_lock(paths, timeout_seconds=lock_timeout_seconds):
            store = load_store(paths)
            if not store.get("tasks"):
                return decision
            dedupe = load_dedupe(paths)
            outbox = load_outbox(paths)

            active = get_active_task(store)
            if active is not None:
                _evaluate_task(active, store, dedupe, outbox, now, decision, transport, test_label)

            # Queued tasks are consequential too: supervise their queue notice,
            # but do not promote automatically.
            for queued_id in list(store.get("queue", [])):
                task = store.get("tasks", {}).get(queued_id)
                if task is not None:
                    _evaluate_task(task, store, dedupe, outbox, now, decision, transport, test_label)

            save_store(paths, store)
            save_dedupe(paths, dedupe)
            save_outbox(paths, outbox)
            decision.task_saved = True
            decision.dedupe_saved = True
            decision.outbox_saved = True
            for event in decision.events:
                append_event(paths, event)
            return decision
    except TimeoutError as exc:
        decision.exit_code = 1
        decision.stdout = f"HERBIE TASK SUPERVISOR LOCK BUSY\n- Error: {exc}"
        return decision
    except Exception as exc:  # malformed JSON/state fails closed
        decision.exit_code = 1
        decision.stdout = f"HERBIE TASK SUPERVISOR WATCHDOG FAILURE\n- Error: {exc}"
        return decision


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the deterministic Herbie active task watchdog")
    parser.add_argument("--base-dir", type=Path, default=None, help="Task supervisor ledger directory")
    parser.add_argument("--now", default=None, help="Override current UTC time for tests, ISO-8601")
    parser.add_argument("--transport", default=None, help="Owner transport: send-message, stdout-confirmed, null-fail")
    parser.add_argument("--owner-target", default=None, help="send_message target, e.g. telegram")
    parser.add_argument("--test-label", default=None, help="Prefix all owner messages with a test label")
    parser.add_argument("--lock-timeout", type=float, default=5.0)
    args = parser.parse_args(argv)
    now = None
    if args.now:
        raw = args.now
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        now = datetime.fromisoformat(raw)
    decision = run_watchdog(base_dir=args.base_dir, now=now, transport_name=args.transport, owner_target=args.owner_target, test_label=args.test_label, lock_timeout_seconds=args.lock_timeout)
    if decision.stdout:
        print(decision.stdout)
    return decision.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
