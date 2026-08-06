"""Read-only-Normalisierung alter Async-Delegationspayloads."""

from __future__ import annotations

from typing import Any

from tools.delegation_contracts import LaneResult, LaneTask


def _task_goal(payload: dict[str, Any], task_index: int) -> str:
    goals = payload.get("goals")
    if isinstance(goals, list):
        if task_index < 0 or task_index >= len(goals):
            return ""
        return str(goals[task_index] or "").strip()
    return str(payload.get("goal") or "").strip()


def upcast_async_task(
    payload: dict[str, Any], *, task_index: int = 0
) -> LaneTask | None:
    """Übersetzt einen bestehenden Async-Payload ohne ihn zu verändern."""

    delegation_id = str(payload.get("delegation_id") or "").strip()
    goal = _task_goal(payload, task_index)
    role = payload.get("role") or "leaf"
    if payload.get("is_batch"):
        task_id = f"{delegation_id}:task-{task_index}"
    else:
        task_id = delegation_id

    try:
        return LaneTask.model_validate({
            "task_id": task_id,
            "goal": goal,
            "role": role,
            "context": str(payload.get("context") or ""),
            "workdir": payload.get("workdir"),
            "parent_task_id": payload.get("parent_task_id"),
        })
    except Exception:
        return None


def upcast_async_result(
    payload: dict[str, Any], *, task_id: str, task_index: int = 0
) -> LaneResult:
    """Übersetzt ein Ergebnis fail-safe in einen prüfbaren LaneResult."""

    # Batch-Completion liefert Ergebnisse als Liste mit task_index.
    if isinstance(payload.get("results"), list):
        matching = [
            item for item in payload["results"]
            if isinstance(item, dict) and item.get("task_index") == task_index
        ]
        payload = matching[0] if matching else {}

    raw_status = payload.get("status")
    raw_summary = payload.get("summary")
    error = payload.get("error")
    if raw_status == "completed" and isinstance(raw_summary, str) and raw_summary.strip():
        status = "completed"
        summary = raw_summary.strip()
    elif raw_status == "failed":
        status = "failed"
        summary = str(raw_summary or "Delegation fehlgeschlagen").strip()
    else:
        status = "blocked"
        summary = "Delegationsergebnis konnte nicht verifiziert werden"

    result = {
        "task_id": task_id,
        "status": status,
        "summary": summary,
        "artifacts": payload.get("artifacts") or [],
        "verification": payload.get("verification") or [],
        "error": str(error).strip() if error else None,
    }
    return LaneResult.model_validate(result)
