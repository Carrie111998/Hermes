"""Durable, machine-owned timing state for the todo tool.

The model owns todo identifiers, text, and statuses. Hermes owns all timestamps.
Timing is serialized into the existing todo tool result so a fresh AIAgent can
restore it from canonical paired tool history without a second persistence
surface.
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Dict, List, Optional


TODO_TIMING_SCHEMA_VERSION = 1
_ACTIVE_STATUSES = {"pending", "in_progress"}
_TERMINAL_STATUSES = {"completed", "cancelled"}
_MAX_SAFE_CYCLE_ID = (1 << 53) - 1
_MAX_FUTURE_SKEW_SECONDS = 300.0
_TIMING_EPSILON_SECONDS = 1e-6


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


class TodoTimingState:
    """Track task-active time and task-cycle wall time across todo writes."""

    def __init__(self, clock: Optional[Callable[[], float]] = None) -> None:
        self._clock = clock or time.time
        self._cycle_id = 0
        self._cycle_known = False
        self._cycle_started_at: Optional[float] = None
        self._cycle_completed_at: Optional[float] = None
        self._items: Dict[str, Dict[str, Any]] = {}
        self._source = "live_runtime"

    def has_state(self) -> bool:
        """Return whether timing has durable cycle or item state to preserve."""
        return self._cycle_id > 0 or bool(self._items)

    def apply(
        self,
        previous: List[Dict[str, str]],
        current: List[Dict[str, str]],
    ) -> None:
        """Apply one validated todo-list transition at the current clock time."""
        now = float(self._clock())
        previous_by_id = {item["id"]: item for item in previous}
        previous_terminal = bool(previous) and all(
            item["status"] in _TERMINAL_STATUSES for item in previous
        )
        current_active = any(item["status"] in _ACTIVE_STATUSES for item in current)
        rotate = previous_terminal and current_active

        started_cycle = False
        if rotate:
            self._start_cycle(current, now)
            started_cycle = True
        elif not previous and current:
            self._start_cycle(current, now)
            started_cycle = True

        updated: Dict[str, Dict[str, Any]] = {}
        for item in current:
            task_id = item["id"]
            previous_item = previous_by_id.get(task_id)
            timing = self._items.get(task_id)
            carried_terminal = (
                started_cycle
                and previous_item is not None
                and previous_item["status"] in _TERMINAL_STATUSES
                and item["status"] in _TERMINAL_STATUSES
                and timing is not None
            )
            if started_cycle and not carried_terminal:
                timing = None
            if (
                timing is not None
                and timing.get("cycle_id") != self._cycle_id
                and item["status"] in _ACTIVE_STATUSES
            ):
                timing = None
            if timing is None:
                timing = self._new_item(item["status"], now, self._cycle_id)
            elif previous_item is not None:
                self._transition(
                    timing,
                    previous_item["status"],
                    item["status"],
                    now,
                )
            updated[task_id] = timing
        self._items = updated

        if not current:
            if previous and self._cycle_completed_at is None:
                self._cycle_completed_at = now
            return
        if all(item["status"] in _TERMINAL_STATUSES for item in current):
            if self._cycle_completed_at is None:
                self._cycle_completed_at = now
        else:
            self._cycle_completed_at = None

    def hydrate(self, todos: List[Dict[str, str]], payload: Any) -> None:
        """Restore a validated timing envelope or mark legacy state unknown."""
        self._reset_unknown(todos)
        if not isinstance(payload, dict) or payload.get("schema_version") != TODO_TIMING_SCHEMA_VERSION:
            return

        cycle = payload.get("cycle")
        item_payloads = payload.get("items")
        if not isinstance(cycle, dict) or not isinstance(item_payloads, dict):
            return

        now = float(self._clock())
        cycle_id = cycle.get("id")
        cycle_known = cycle.get("known") is True
        cycle_started_at = _number(cycle.get("started_at"))
        cycle_completed_at = _number(cycle.get("finished_at"))
        if (
            not isinstance(cycle_id, int)
            or isinstance(cycle_id, bool)
            or cycle_id < 1
            or cycle_id > _MAX_SAFE_CYCLE_ID
        ):
            return
        if cycle_started_at is not None and cycle_started_at > now + _MAX_FUTURE_SKEW_SECONDS:
            return
        if cycle_completed_at is not None and cycle_completed_at > now + _MAX_FUTURE_SKEW_SECONDS:
            return
        if cycle_known and cycle_started_at is None:
            return
        if (
            cycle_known
            and cycle_started_at is not None
            and cycle_completed_at is not None
            and cycle_completed_at < cycle_started_at
        ):
            return
        if cycle_completed_at is not None and any(
            item["status"] in _ACTIVE_STATUSES for item in todos
        ):
            return
        if (
            cycle_known
            and todos
            and all(item["status"] in _TERMINAL_STATUSES for item in todos)
            and cycle_completed_at is None
        ):
            return

        restored: Dict[str, Dict[str, Any]] = {}
        for item in todos:
            task_id = item["id"]
            raw = item_payloads.get(task_id)
            timing = self._hydrate_item(
                item["status"], raw, now=now, current_cycle_id=cycle_id
            )
            if not cycle_known:
                timing = self._unknown_item(timing.get("cycle_id"))
            restored[task_id] = timing

        self._cycle_id = cycle_id
        self._cycle_known = cycle_known
        self._cycle_started_at = cycle_started_at if cycle_known else None
        self._cycle_completed_at = cycle_completed_at if cycle_known else None
        self._items = restored
        self._source = "paired_history"

    def snapshot(self, todos: List[Dict[str, str]]) -> Dict[str, Any]:
        """Return a JSON-safe timing envelope for tool results and hooks."""
        now = float(self._clock())
        items: Dict[str, Dict[str, Any]] = {}
        for item in todos:
            timing = self._items.get(item["id"])
            if timing is None or not timing.get("known"):
                cycle_id = timing.get("cycle_id") if timing is not None else None
                items[item["id"]] = self._unknown_snapshot_item(cycle_id)
                continue
            active_seconds = float(timing["active_seconds"])
            active_since = timing.get("active_since")
            if active_since is not None:
                active_seconds += max(0.0, now - float(active_since))
            items[item["id"]] = {
                "known": True,
                "cycle_id": timing.get("cycle_id"),
                "created_at": timing.get("created_at"),
                "started_at": timing.get("started_at"),
                "finished_at": timing.get("completed_at"),
                "accumulated_active_seconds": max(
                    0.0,
                    float(timing["active_seconds"]),
                ),
                "active_seconds": max(0, round(active_seconds)),
                "active_since": active_since,
            }

        cycle_elapsed: Optional[int] = None
        if self._cycle_known and self._cycle_started_at is not None:
            cycle_end = self._cycle_completed_at if self._cycle_completed_at is not None else now
            cycle_elapsed = max(0, round(cycle_end - self._cycle_started_at))
        return {
            "schema_version": TODO_TIMING_SCHEMA_VERSION,
            "source": self._source,
            "observed_at": now,
            "cycle": {
                "id": self._cycle_id if self._cycle_id > 0 else None,
                "known": self._cycle_known,
                "started_at": self._cycle_started_at if self._cycle_known else None,
                "finished_at": self._cycle_completed_at if self._cycle_known else None,
                "elapsed_seconds": cycle_elapsed,
            },
            "items": items,
        }

    def _start_cycle(self, todos: List[Dict[str, str]], now: float) -> None:
        self._cycle_id += 1
        if any(item["status"] in _ACTIVE_STATUSES for item in todos):
            self._cycle_known = True
            self._cycle_started_at = now
            self._cycle_completed_at = None
        else:
            self._cycle_known = False
            self._cycle_started_at = None
            self._cycle_completed_at = None

    def _reset_unknown(self, todos: List[Dict[str, str]]) -> None:
        self._cycle_id = 0
        self._cycle_known = False
        self._cycle_started_at = None
        self._cycle_completed_at = None
        self._items = {item["id"]: self._unknown_item() for item in todos}
        self._source = "paired_history"

    @staticmethod
    def _unknown_item(cycle_id: int | None = None) -> Dict[str, Any]:
        return {
            "known": False,
            "cycle_id": cycle_id,
            "created_at": None,
            "started_at": None,
            "completed_at": None,
            "active_since": None,
            "accumulated_active_seconds": None,
            "active_seconds": None,
        }

    @classmethod
    def _unknown_snapshot_item(cls, cycle_id: int | None = None) -> Dict[str, Any]:
        item = cls._unknown_item(cycle_id)
        item["finished_at"] = item.pop("completed_at")
        return item

    @classmethod
    def _new_item(cls, status: str, now: float, cycle_id: int) -> Dict[str, Any]:
        if cycle_id < 1:
            return cls._unknown_item()
        if status in _TERMINAL_STATUSES:
            return cls._unknown_item(cycle_id)
        started_at = now if status == "in_progress" else None
        return {
            "known": True,
            "cycle_id": cycle_id,
            "created_at": now,
            "started_at": started_at,
            "completed_at": None,
            "active_since": started_at,
            "active_seconds": 0.0,
        }

    @staticmethod
    def _transition(
        timing: Dict[str, Any],
        previous_status: str,
        current_status: str,
        now: float,
    ) -> None:
        if not timing.get("known"):
            return
        active_since = timing.get("active_since")
        if previous_status == "in_progress" and current_status != "in_progress":
            if active_since is not None:
                timing["active_seconds"] = float(timing["active_seconds"]) + max(
                    0.0, now - float(active_since)
                )
            timing["active_since"] = None
        if current_status == "in_progress" and previous_status != "in_progress":
            if timing.get("started_at") is None:
                timing["started_at"] = now
            timing["active_since"] = now
        if current_status in _TERMINAL_STATUSES and previous_status not in _TERMINAL_STATUSES:
            timing["completed_at"] = now
        elif current_status not in _TERMINAL_STATUSES:
            timing["completed_at"] = None

    @classmethod
    def _hydrate_item(
        cls,
        status: str,
        raw: Any,
        *,
        now: float,
        current_cycle_id: int,
    ) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            return cls._unknown_item()
        cycle_id = raw.get("cycle_id")
        if (
            isinstance(cycle_id, bool)
            or not isinstance(cycle_id, int)
            or cycle_id < 1
            or cycle_id > current_cycle_id
        ):
            cycle_id = None
        if raw.get("known") is not True:
            return cls._unknown_item(cycle_id)
        if cycle_id is None:
            return cls._unknown_item()
        if status in _ACTIVE_STATUSES and cycle_id != current_cycle_id:
            return cls._unknown_item(cycle_id)
        created_at = _number(raw.get("created_at"))
        started_at = _number(raw.get("started_at"))
        completed_at = _number(raw.get("finished_at"))
        active_since = _number(raw.get("active_since"))
        accumulated = _number(raw.get("accumulated_active_seconds"))
        if created_at is None or accumulated is None:
            return cls._unknown_item(cycle_id)
        if any(
            value is not None and value > now + _MAX_FUTURE_SKEW_SECONDS
            for value in (created_at, started_at, completed_at, active_since)
        ):
            return cls._unknown_item(cycle_id)
        if started_at is not None and started_at < created_at:
            return cls._unknown_item(cycle_id)
        if completed_at is not None and completed_at < (started_at or created_at):
            return cls._unknown_item(cycle_id)
        if status in _TERMINAL_STATUSES and completed_at is None:
            return cls._unknown_item(cycle_id)
        if status not in _TERMINAL_STATUSES and completed_at is not None:
            return cls._unknown_item(cycle_id)
        if status == "in_progress" and (
            started_at is None
            or active_since is None
            or active_since < started_at
        ):
            return cls._unknown_item(cycle_id)
        if status != "in_progress" and active_since is not None:
            return cls._unknown_item(cycle_id)
        active_end = completed_at if completed_at is not None else now
        if accumulated > 0 and started_at is None:
            return cls._unknown_item(cycle_id)
        if (
            started_at is not None
            and accumulated
            > max(0.0, active_end - started_at) + _TIMING_EPSILON_SECONDS
        ):
            return cls._unknown_item(cycle_id)
        if accumulated > max(0.0, active_end - created_at) + _TIMING_EPSILON_SECONDS:
            return cls._unknown_item(cycle_id)
        total_active = accumulated
        if active_since is not None:
            total_active += max(0.0, now - active_since)
        if total_active > max(0.0, now - created_at) + _TIMING_EPSILON_SECONDS:
            return cls._unknown_item(cycle_id)
        if status != "in_progress":
            active_since = None
        return {
            "known": True,
            "cycle_id": cycle_id,
            "created_at": created_at,
            "started_at": started_at,
            "completed_at": completed_at,
            "active_since": active_since,
            "active_seconds": accumulated,
        }
