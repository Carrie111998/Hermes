"""Frozen per-lane defaults for CS-10a task-scope budgets."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class DefaultTaskCaps:
    green_captains: float = 0.75
    dayroute: float = 0.60
    tihna: float = 0.50
    ops: float = 0.25
    platform: float = 0.20
    reserve: float = 1.50
    escalation: float = 1.50
    default: float = 0.50


DEFAULT_TASK_CAPS = DefaultTaskCaps()


def default_task_cap_for_lane(lane: str | None) -> float:
    """Return the frozen cap for ``lane`` or the conservative default."""
    normalized = str(lane or "").strip().lower()
    return float(
        getattr(DEFAULT_TASK_CAPS, normalized, DEFAULT_TASK_CAPS.default)
    )


def validate_task_cap(value: float) -> float:
    """Normalize a caller-provided task cap and reject zero/invalid values."""
    try:
        cap = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("task_cap_aud must be numeric") from exc
    if not math.isfinite(cap) or cap <= 0:
        raise ValueError("task_cap_aud must be finite and greater than zero")
    return cap


__all__ = [
    "DEFAULT_TASK_CAPS",
    "DefaultTaskCaps",
    "default_task_cap_for_lane",
    "validate_task_cap",
]
