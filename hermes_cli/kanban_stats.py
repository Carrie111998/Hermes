"""Profile and routing statistics for the Kanban board.

The report deliberately keeps task-level and attempt-level measurements
separate.  Task rows describe assignment and lifecycle; task_runs rows
represent execution attempts.  Historical rows are never rewritten, so a
reassignment is visible as the current assignment plus the profile that
actually claimed each attempt.
"""

from __future__ import annotations

import statistics
import time
import json
from collections import Counter, defaultdict
from typing import Any, Optional

from hermes_cli import kanban_db as kb


_TERMINAL_FAILURES = {"crashed", "timed_out", "failed", "spawn_failed", "gave_up"}
_EVENT_BUCKETS = {
    "reclaimed": "reclaimed",
    "protocol_violation": "protocol_violating",
    "stale": "stale",
    "recomputed_ready": "dependency_releases",
    "dependency_released": "dependency_releases",
}


def _percentile(values: list[float], percentile: float) -> Optional[float]:
    if not values:
        return None
    return float(statistics.quantiles(values, n=100, method="inclusive")[int(percentile) - 1]) if len(values) > 1 else float(values[0])


def _summary(values: list[float]) -> dict[str, Optional[float]]:
    return {
        "average_seconds": round(statistics.fmean(values), 3) if values else None,
        "median_seconds": round(float(statistics.median(values)), 3) if values else None,
        "p95_seconds": round(_percentile(values, 95) or 0, 3) if values else None,
    }


def _task_type(row: Any) -> str:
    return str(row["workflow_template_id"] or row["workspace_kind"] or "untyped")


def build_report(
    conn,
    *,
    profile: Optional[str] = None,
    tenant: Optional[str] = None,
    status: Optional[str] = None,
    since: Optional[int] = None,
    until: Optional[int] = None,
    include_archived: bool = False,
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Build a bounded, secret-free report for one already-scoped board.

    ``since``/``until`` filter task creation and run start timestamps.  A
    profile's task assignment counts use ``tasks.assignee`` while execution
    counts use the immutable ``task_runs.profile`` attribution.  This avoids
    turning reassignment into false historical work.
    """
    now = int(time.time() if now is None else now)
    clauses: list[str] = []
    params: list[Any] = []
    if not include_archived:
        clauses.append("t.status != 'archived'")
    if tenant is not None:
        clauses.append("t.tenant = ?")
        params.append(tenant)
    if status is not None:
        clauses.append("t.status = ?")
        params.append(status)
    if profile is not None:
        clauses.append("(t.assignee = ? OR EXISTS (SELECT 1 FROM task_runs rp WHERE rp.task_id = t.id AND rp.profile = ?))")
        params.extend([profile, profile])
    if since is not None:
        clauses.append("t.created_at >= ?")
        params.append(int(since))
    if until is not None:
        clauses.append("t.created_at <= ?")
        params.append(int(until))
    where = " AND ".join(clauses) or "1=1"
    tasks = conn.execute(
        "SELECT t.id, t.assignee, t.status, t.tenant, t.created_at, "
        "t.started_at, t.completed_at, t.workspace_kind, t.workflow_template_id "
        f"FROM tasks t WHERE {where}", params,
    ).fetchall()
    task_ids = [row["id"] for row in tasks]
    runs: list[Any] = []
    events: list[Any] = []
    if task_ids:
        marks = ",".join("?" for _ in task_ids)
        run_params: list[Any] = list(task_ids)
        run_where = f"r.task_id IN ({marks})"
        if since is not None:
            run_where += " AND r.started_at >= ?"
            run_params.append(int(since))
        if until is not None:
            run_where += " AND r.started_at <= ?"
            run_params.append(int(until))
        if profile is not None:
            run_where += " AND r.profile = ?"
            run_params.append(profile)
        runs = conn.execute(f"SELECT r.* FROM task_runs r WHERE {run_where} ORDER BY r.started_at, r.id", run_params).fetchall()
        events = conn.execute(
            f"SELECT e.kind, e.task_id, e.payload FROM task_events e WHERE e.task_id IN ({marks})", task_ids
        ).fetchall()

    registry = {item["name"]: bool(item["on_disk"]) for item in kb.known_assignees(conn)}
    by_profile: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "assigned_tasks": 0, "task_status": Counter(), "attempts": 0,
        "running_attempts": 0, "completed_tasks": 0, "blocked_tasks": 0,
        "failed_attempts": 0, "reclaimed": 0, "retried_tasks": 0,
        "protocol_violating": 0, "stale": 0, "dependency_releases": 0,
        "durations": [], "queue_wait": [], "blocker_classes": Counter(),
        "task_types": Counter(), "_task_ids": set(), "_attempt_task_ids": set(),
    })
    for name, on_disk in registry.items():
        by_profile[name]["on_disk"] = on_disk
    if profile is not None and profile not in by_profile:
        by_profile[profile]["on_disk"] = profile in registry
    task_by_id = {row["id"]: row for row in tasks}
    for row in tasks:
        if profile is None or row["assignee"] == profile:
            if row["assignee"]:
                item = by_profile[row["assignee"]]
                item["assigned_tasks"] += 1
                item["task_status"][row["status"]] += 1
                item["task_types"][_task_type(row)] += 1
                item["_task_ids"].add(row["id"])
    for run in runs:
        name = run["profile"] or "unattributed"
        item = by_profile[name]
        item["attempts"] += 1
        item["_attempt_task_ids"].add(run["task_id"])
        if run["status"] == "running":
            item["running_attempts"] += 1
        if run["outcome"] == "completed":
            item.setdefault("_completed_task_ids", set()).add(run["task_id"])
        if run["outcome"] == "blocked":
            item["blocked_tasks"] += 1
        if run["outcome"] in _TERMINAL_FAILURES:
            item["failed_attempts"] += 1
        if run["ended_at"] is not None:
            item["durations"].append(max(0, int(run["ended_at"]) - int(run["started_at"])))
        task = task_by_id.get(run["task_id"])
        if task is not None:
            item["queue_wait"].append(max(0, int(run["started_at"]) - int(task["created_at"])))
        if run["outcome"] == "reclaimed":
            item["reclaimed"] += 1
    for event in events:
        bucket = _EVENT_BUCKETS.get(event["kind"])
        if not bucket:
            if event["kind"] == "blocked":
                task = task_by_id.get(event["task_id"])
                name = (task["assignee"] if task is not None else None) or "unattributed"
                blocker = "blocked"
                try:
                    payload = json.loads(event["payload"]) if event["payload"] else {}
                    blocker = str(payload.get("kind") or payload.get("block_kind") or blocker)
                except (TypeError, ValueError, AttributeError):
                    pass
                by_profile[name]["blocker_classes"][blocker] += 1
            continue
        # Attribute lifecycle events to the current assignee; run attribution
        # remains immutable above and is the source of attempt metrics.
        task = task_by_id.get(event["task_id"])
        name = (task["assignee"] if task is not None else None) or "unattributed"
        if profile is not None and name != profile:
            continue
        by_profile[name][bucket] += 1
    for item in by_profile.values():
        item["retried_tasks"] = sum(1 for task_id in item["_attempt_task_ids"] if sum(1 for r in runs if r["task_id"] == task_id) > 1)
        item["completed_tasks"] = len(item.get("_completed_task_ids", set()))

    profiles = []
    for name in sorted(by_profile):
        item = by_profile[name]
        observed_attempts = item["attempts"] > 0
        if name == "unattributed":
            telemetry_state = "unavailable"
        elif observed_attempts:
            telemetry_state = "observed_work"
        elif item["on_disk"]:
            telemetry_state = "observed_zero"
        else:
            telemetry_state = "unavailable"
        profiles.append({
            "name": name,
            "on_disk": bool(item["on_disk"]),
            "telemetry_state": telemetry_state,
            "assigned_tasks": item["assigned_tasks"],
            "task_status": dict(sorted(item["task_status"].items())),
            "attempts": item["attempts"],
            "running_attempts": item["running_attempts"],
            "completed_tasks": item["completed_tasks"],
            "blocked_tasks": item["blocked_tasks"],
            "failed_attempts": item["failed_attempts"],
            "reclaimed": item["reclaimed"],
            "retried_tasks": item["retried_tasks"],
            "protocol_violating": item["protocol_violating"],
            "stale": item["stale"],
            "dependency_releases": item["dependency_releases"],
            "duration": _summary(item["durations"]),
            "queue_wait": _summary(item["queue_wait"]),
            "blocker_classes": dict(sorted(item["blocker_classes"].items())),
            "task_types": dict(sorted(item["task_types"].items())),
        })
    by_status = Counter(row["status"] for row in tasks)
    by_assignee: dict[str, dict[str, int]] = defaultdict(dict)
    for row in tasks:
        if row["assignee"]:
            counts = by_assignee[row["assignee"]]
            counts[row["status"]] = counts.get(row["status"], 0) + 1
    ready_times = [int(row["created_at"]) for row in tasks if row["status"] == "ready"]
    return {
        "schema_version": 1,
        "board": kb.get_current_board(),
        "generated_at": now,
        "filters": {"profile": profile, "tenant": tenant, "status": status, "since": since, "until": until, "include_archived": include_archived},
        "task_count": len(tasks),
        "attempt_count": len(runs),
        "by_status": dict(sorted(by_status.items())),
        "by_assignee": {name: dict(sorted(counts.items())) for name, counts in sorted(by_assignee.items())},
        "oldest_ready_age_seconds": max(0, now - min(ready_times)) if ready_times else None,
        "profiles": profiles,
        "notes": {
            "attempts_are_task_runs": True,
            "task_counts_are_distinct_task_rows": True,
            "profile_attribution": "task_runs.profile for attempts; tasks.assignee for assignments and lifecycle events",
            "secret_safe": True,
        },
    }
