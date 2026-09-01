"""Shared notification coalescing and wording for Kanban surfaces."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def _payload(event: Any) -> dict[str, Any]:
    value = getattr(event, "payload", None)
    return value if isinstance(value, dict) else {}


def _is_superseded_timeout(event: Any, successor: Any) -> bool:
    """Return whether ``successor`` terminally supersedes ``event``.

    Runtime enforcement writes the retry-shaped ``timed_out`` event and the
    breaker-shaped ``gave_up`` event as adjacent rows for the same task.  Exact
    row adjacency is the primary correlation key.  Run and payload fields are
    additional guards when both events carry them; older timeout breaker rows
    intentionally have no ``gave_up.run_id``.
    """
    if getattr(event, "kind", None) != "timed_out":
        return False
    if getattr(successor, "kind", None) != "gave_up":
        return False
    if _payload(successor).get("trigger_outcome") != "timed_out":
        return False

    event_id = getattr(event, "id", None)
    successor_id = getattr(successor, "id", None)
    if not isinstance(event_id, int) or not isinstance(successor_id, int):
        return False
    if successor_id != event_id + 1:
        return False

    task_id = getattr(event, "task_id", None)
    successor_task_id = getattr(successor, "task_id", None)
    if task_id is not None and successor_task_id is not None:
        if task_id != successor_task_id:
            return False

    run_id = getattr(event, "run_id", None)
    successor_run_id = getattr(successor, "run_id", None)
    if run_id is not None and successor_run_id is not None:
        if run_id != successor_run_id:
            return False

    event_payload = _payload(event)
    successor_payload = _payload(successor)
    for key in ("pid", "retry_status"):
        if key in event_payload and key in successor_payload:
            if event_payload[key] != successor_payload[key]:
                return False
    return True


def coalesce_notification_events(events: Iterable[Any]) -> list[Any]:
    """Drop only timeout alerts superseded by an adjacent terminal breaker."""
    ordered = list(events)
    return [
        event
        for index, event in enumerate(ordered)
        if not (
            index + 1 < len(ordered)
            and _is_superseded_timeout(event, ordered[index + 1])
        )
    ]


def format_gave_up_notification(
    board_tag: str,
    assignee_tag: str,
    task_id: str,
    payload: dict[str, Any] | None,
) -> str:
    """Render a truthful circuit-breaker notification."""
    data = payload if isinstance(payload, dict) else {}
    trigger = str(data.get("trigger_outcome") or "")
    failures = data.get("failures")

    if trigger == "timed_out":
        attempts = f"{failures} attempts" if isinstance(failures, int) else "repeated attempts"
        detail = f"timed out after {attempts} and is blocked"
        return f"✖ {board_tag}{assignee_tag}Kanban {task_id} {detail}"

    if trigger == "spawn_failed":
        count = f"{failures} " if isinstance(failures, int) else "repeated "
        detail = f"gave up after {count}spawn failures and is blocked"
        error = str(data.get("error") or "").strip()
        suffix = f"\n{error[:200]}" if error else ""
        return f"✖ {board_tag}{assignee_tag}Kanban {task_id} {detail}{suffix}"

    error = str(data.get("error") or "").strip()
    detail = error[:200] if error else "failure limit reached"
    return (
        f"✖ {board_tag}{assignee_tag}Kanban {task_id} gave up\n"
        f"{detail}"
    )
