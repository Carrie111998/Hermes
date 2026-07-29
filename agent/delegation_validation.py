"""Import-safe delegate_task argument normalization and validation."""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional


def recover_tasks_from_json_string(
    tasks: Any,
) -> tuple[Optional[list[dict[str, Any]]], Optional[str]]:
    """Recover a model-emitted JSON string containing a task batch."""
    if not isinstance(tasks, str):
        return None, None
    raw = tasks.strip()
    if not raw:
        return None, "Provide either 'goal' (single task) or 'tasks' (batch)."
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, (
            "tasks must be a JSON array of task objects; received a string "
            f"that could not be parsed as JSON ({exc.msg})."
        )
    if not isinstance(parsed, list):
        return None, (
            f"tasks must be a JSON array of task objects; parsed "
            f"{type(parsed).__name__} instead."
        )
    return parsed, None


def build_delegate_task_list(
    *,
    goal: Any,
    context: Any,
    role: Any,
    tasks: Any,
) -> tuple[Optional[list[dict[str, Any]]], Optional[str]]:
    """Select single/batch mode and validate every task's goal."""
    if tasks and isinstance(tasks, list):
        task_list = tasks
    elif goal and isinstance(goal, str) and goal.strip():
        task_list = [{"goal": goal, "context": context, "role": role}]
    else:
        return None, "Provide either 'goal' (single task) or 'tasks' (batch)."

    if not task_list:
        return None, "No tasks provided."

    for i, task in enumerate(task_list):
        if not isinstance(task, Mapping):
            return None, f"Task {i} must be an object, got {type(task).__name__}."
        task_goal = task.get("goal")
        if not isinstance(task_goal, str) or not task_goal.strip():
            return None, f"Task {i} is missing a 'goal'."

    return task_list, None
