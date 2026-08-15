"""Deterministic no-agent watchdog for Herbie active owner tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

from .ledger import (
    ACTIVE_MONITORED_STATES,
    LedgerPaths,
    append_event,
    default_paths,
    fingerprint,
    format_time,
    load_dedupe,
    load_task,
    minutes_between,
    save_dedupe,
    save_task,
    utc_now,
    validate_task,
)

OWNER_NOTIFICATION_DELIVERED = "delivered"
OWNER_NOTIFICATION_EMITTED = "emitted_pending_transport"


@dataclass
class WatchdogDecision:
    stdout: str = ""
    exit_code: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)
    task_saved: bool = False
    dedupe_saved: bool = False


def _incident_seen(dedupe: dict[str, Any], key: str) -> bool:
    return key in dedupe.setdefault("incidents", {})


def _mark_incident(dedupe: dict[str, Any], key: str, now: datetime, kind: str) -> None:
    dedupe.setdefault("incidents", {})[key] = {"kind": kind, "first_emitted_at": format_time(now)}


def _notify_message(kind: str, task: dict[str, Any], now: datetime) -> str:
    task_name = f"{task.get('task_id', 'UNKNOWN')} — {task.get('title', 'Untitled task')}"
    if kind == "heartbeat":
        return (
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
        return (
            f"HERBIE TASK BLOCKED — {task_name}\n"
            f"- Failure stage: {task.get('current_step')}\n"
            f"- Exact blocker: {task.get('blocker_detail')}\n"
            f"- Work preserved: {task.get('checkpoint_artifact_path') or task.get('checkpoint_commit') or 'not recorded'}\n"
            f"- Side effects: task ledger only; no customer/prospect side effects recorded\n"
            f"- Owner decision/action required: {task.get('next_step') or 'review blocker'}"
        )
    if kind == "recovery":
        return (
            f"HERBIE TASK RESUMED — {task_name}\n"
            f"- State: ACTIVE\n"
            f"- Current milestone: {task.get('percent_or_stage_complete')}\n"
            f"- Working on: {task.get('current_step')}\n"
            f"- Next expected milestone: {task.get('next_step')}"
        )
    if kind == "ready":
        return (
            f"HERBIE TASK READY FOR REVIEW — {task_name}\n"
            f"- Result: {task.get('percent_or_stage_complete')}\n"
            f"- Evidence: {task.get('completion_artifact') or task.get('checkpoint_artifact_path') or 'see repository PR/checks'}\n"
            f"- Commit/artifact: {task.get('checkpoint_commit') or 'not recorded'}\n"
            f"- Remaining limitations: {task.get('blocker_detail') or 'none recorded'}\n"
            f"- Unauthorized next steps remain stopped."
        )
    if kind == "complete":
        return (
            f"HERBIE TASK COMPLETE — {task_name}\n"
            f"- Result: {task.get('percent_or_stage_complete')}\n"
            f"- Evidence: {task.get('completion_artifact') or task.get('checkpoint_artifact_path') or 'see repository PR/checks'}\n"
            f"- Commit/artifact: {task.get('checkpoint_commit') or 'not recorded'}\n"
            f"- Unauthorized next steps remain stopped."
        )
    if kind == "stale_owner":
        elapsed = minutes_between(now, task.get("last_progress_at"))
        elapsed_text = "unknown" if elapsed is None else f"{elapsed:.0f} minutes"
        return (
            f"HERBIE TASK STALE — OWNER ATTENTION\n"
            f"- Task: {task_name}\n"
            f"- State: {task.get('status')}\n"
            f"- Last known step: {task.get('current_step')}\n"
            f"- Last progress: {task.get('last_progress_at')} ({elapsed_text} ago)\n"
            f"- Last checkpoint: {task.get('checkpoint_commit') or task.get('checkpoint_artifact_path') or 'not recorded'}\n"
            f"- Required action: Herbie must update state, continue, or declare BLOCKED."
        )
    return ""


def _emit_once(
    *,
    kind: str,
    key: str,
    task: dict[str, Any],
    dedupe: dict[str, Any],
    now: datetime,
    decision: WatchdogDecision,
) -> None:
    if _incident_seen(dedupe, key):
        return
    message = _notify_message(kind, task, now)
    if not message:
        return
    _mark_incident(dedupe, key, now, kind)
    if decision.stdout:
        decision.stdout += "\n\n"
    decision.stdout += message
    decision.events.append(
        {
            "at": format_time(now),
            "task_id": task.get("task_id"),
            "event": "owner_notification_emitted",
            "kind": kind,
            "incident_key": key,
        }
    )


def run_watchdog(
    *,
    base_dir: Path | None = None,
    now: datetime | None = None,
    internal_nudge_command_available: bool = False,
) -> WatchdogDecision:
    """Evaluate the active task ledger and return deterministic output.

    The default transport is stdout so a script-only Hermes cron can deliver the
    notification. Because the cron runner does not call back into this script
    after delivery, this code records notification emission separately from
    transport-confirmed delivery.
    """

    paths: LedgerPaths = default_paths(base_dir)
    now = (now or utc_now()).astimezone(UTC).replace(microsecond=0)
    decision = WatchdogDecision()

    try:
        task = load_task(paths)
        if task is None:
            return decision

        status = task.get("status")
        if status not in ACTIVE_MONITORED_STATES and status not in {"PREFLIGHT"}:
            return decision

        errors = validate_task(task)
        dedupe = load_dedupe(paths)

        if errors:
            task["status"] = "BLOCKED"
            task["blocker_type"] = "SPECIFICATION_AUTHORITY" if any("spec" in e for e in errors) else "STATE_VALIDATION"
            task["blocker_detail"] = "; ".join(errors)
            task["last_progress_at"] = format_time(now)
            task["owner_notification_state"] = "pending"
            save_task(paths, task)
            decision.task_saved = True
            key = f"{task.get('task_id', 'UNKNOWN')}:BLOCKED:{fingerprint(task.get('blocker_detail'))}"
            _emit_once(kind="blocked", key=key, task=task, dedupe=dedupe, now=now, decision=decision)
            save_dedupe(paths, dedupe)
            decision.dedupe_saved = True
            for event in decision.events:
                append_event(paths, event)
            return decision

        if status == "PREFLIGHT":
            key = f"{task['task_id']}:PREFLIGHT_STALE"
            age = minutes_between(now, task.get("last_progress_at"))
            if age is not None and age >= 30:
                task["status"] = "BLOCKED"
                task["blocker_type"] = "PREFLIGHT_STALE"
                task["blocker_detail"] = "preflight did not transition to ACTIVE, BLOCKED, or WAITING_OWNER within 30 minutes"
                task["last_progress_at"] = format_time(now)
                task["owner_notification_state"] = "pending"
                save_task(paths, task)
                decision.task_saved = True
                _emit_once(kind="blocked", key=key, task=task, dedupe=dedupe, now=now, decision=decision)
                save_dedupe(paths, dedupe)
                decision.dedupe_saved = True

        elif status == "ACTIVE":
            blocker_key_prefix = f"{task['task_id']}:BLOCKED:"
            previous_blocked_keys = [k for k in dedupe.get("incidents", {}) if k.startswith(blocker_key_prefix)]
            if previous_blocked_keys:
                key = f"{task['task_id']}:RECOVERY:{fingerprint(task.get('last_progress_at'), task.get('current_step'))}"
                _emit_once(kind="recovery", key=key, task=task, dedupe=dedupe, now=now, decision=decision)

            stale_minutes = minutes_between(now, task.get("last_progress_at"))
            nudge_key = f"{task['task_id']}:INTERNAL_NUDGE:{fingerprint(task.get('last_progress_at'))}"
            if stale_minutes is not None and stale_minutes >= 30 and not _incident_seen(dedupe, nudge_key):
                _mark_incident(dedupe, nudge_key, now, "internal_nudge")
                task.setdefault("internal_nudge_state", {})["last_nudge_at"] = format_time(now)
                task.setdefault("internal_nudge_state", {})["auto_resume_available"] = internal_nudge_command_available
                task.setdefault("internal_nudge_state", {})["message"] = (
                    "TASK SUPERVISOR: update task state, checkpoint progress, and continue or declare BLOCKED."
                )
                save_task(paths, task)
                decision.task_saved = True
                decision.events.append(
                    {
                        "at": format_time(now),
                        "task_id": task.get("task_id"),
                        "event": "internal_nudge_recorded",
                        "available": internal_nudge_command_available,
                    }
                )

            owner_minutes = minutes_between(now, task.get("last_owner_update_at"))
            if owner_minutes is None or owner_minutes >= 45:
                key = f"{task['task_id']}:HEARTBEAT:{fingerprint(task.get('last_owner_update_at'), task.get('current_step'))}"
                _emit_once(kind="heartbeat", key=key, task=task, dedupe=dedupe, now=now, decision=decision)
                if decision.stdout:
                    task["last_owner_update_at"] = format_time(now)
                    task["owner_notification_state"] = OWNER_NOTIFICATION_EMITTED
                    save_task(paths, task)
                    decision.task_saved = True

            if stale_minutes is not None and stale_minutes >= 60:
                key = f"{task['task_id']}:CRITICAL_STALE:{fingerprint(task.get('last_progress_at'))}"
                _emit_once(kind="stale_owner", key=key, task=task, dedupe=dedupe, now=now, decision=decision)

        elif status == "BLOCKED":
            notification_state = task.get("owner_notification_state")
            key = f"{task['task_id']}:BLOCKED:{fingerprint(task.get('blocker_detail'))}"
            if notification_state != OWNER_NOTIFICATION_DELIVERED:
                _emit_once(kind="blocked", key=key, task=task, dedupe=dedupe, now=now, decision=decision)
                if decision.stdout:
                    task["owner_notification_state"] = OWNER_NOTIFICATION_EMITTED
                    save_task(paths, task)
                    decision.task_saved = True

        elif status == "READY_FOR_INDEPENDENT_REVIEW":
            key = f"{task['task_id']}:READY:{fingerprint(task.get('completion_artifact'), task.get('checkpoint_commit'))}"
            if task.get("owner_notification_state") != OWNER_NOTIFICATION_DELIVERED:
                _emit_once(kind="ready", key=key, task=task, dedupe=dedupe, now=now, decision=decision)
                if decision.stdout:
                    task["owner_notification_state"] = OWNER_NOTIFICATION_EMITTED
                    save_task(paths, task)
                    decision.task_saved = True

        elif status == "COMPLETE":
            key = f"{task['task_id']}:COMPLETE:{fingerprint(task.get('completion_artifact'), task.get('checkpoint_commit'))}"
            if task.get("owner_notification_state") != OWNER_NOTIFICATION_DELIVERED:
                _emit_once(kind="complete", key=key, task=task, dedupe=dedupe, now=now, decision=decision)
                if decision.stdout:
                    task["owner_notification_state"] = OWNER_NOTIFICATION_EMITTED
                    task["closed_at"] = task.get("closed_at") or format_time(now)
                    save_task(paths, task)
                    decision.task_saved = True

        save_dedupe(paths, dedupe)
        decision.dedupe_saved = True
        for event in decision.events:
            append_event(paths, event)
        return decision
    except Exception as exc:  # pragma: no cover - exercised through CLI smoke
        decision.exit_code = 1
        decision.stdout = f"HERBIE TASK SUPERVISOR WATCHDOG FAILURE\n- Error: {exc}"
        return decision


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the deterministic Herbie active task watchdog")
    parser.add_argument("--base-dir", type=Path, default=None, help="Task supervisor ledger directory")
    parser.add_argument("--now", default=None, help="Override current UTC time for tests, ISO-8601")
    parser.add_argument(
        "--internal-nudge-command-available",
        action="store_true",
        help="Record internal nudges as command-capable when an approved resume path is configured",
    )
    args = parser.parse_args(argv)
    now = None
    if args.now:
        raw = args.now
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        now = datetime.fromisoformat(raw)
    decision = run_watchdog(
        base_dir=args.base_dir,
        now=now,
        internal_nudge_command_available=args.internal_nudge_command_available,
    )
    if decision.stdout:
        print(decision.stdout)
    return decision.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
