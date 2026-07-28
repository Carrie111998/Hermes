"""Bounded successor creation for terminal worker-task failures."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("gateway.run")

# Parents whose successor creation already failed this process, so a parent
# that can never produce a successor (e.g. its workspace repository was
# deleted) warns once instead of flooding the error log every watcher tick.
_reported_skips: set[str] = set()

DEFAULT_FAILURE_SUCCESSORS_ENABLED = True
DEFAULT_FAILURE_SUCCESSOR_MAX_CHAIN = 2

_ENVIRONMENTAL_TIMEOUT_MARKERS = (
    "silent worker",
    "is not recognized",
    "not recognized as an internal or external command",
    "cmd.exe was not found",
    "cmd.exe is missing",
    "missing cmd.exe",
)


def resolve_failure_successor_settings(cfg: Any) -> dict:
    """Extract ``worker_bridge.failure_successors`` with bounded defaults."""
    worker_bridge = cfg.get("worker_bridge", {}) if isinstance(cfg, dict) else {}
    raw = (
        worker_bridge.get("failure_successors", {})
        if isinstance(worker_bridge, dict)
        else {}
    )
    if not isinstance(raw, dict):
        raw = {}
    try:
        max_chain = max(
            0,
            int(raw.get("max_chain", DEFAULT_FAILURE_SUCCESSOR_MAX_CHAIN)),
        )
    except (TypeError, ValueError):
        max_chain = DEFAULT_FAILURE_SUCCESSOR_MAX_CHAIN
    return {
        "enabled": raw.get("enabled", DEFAULT_FAILURE_SUCCESSORS_ENABLED) is not False,
        "max_chain": max_chain,
    }


def _integer(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _failure_text(task: dict) -> str:
    result = task.get("result") or {}
    if not isinstance(result, dict):
        return ""
    return str(result.get("error") or result.get("summary") or "").strip()


def _is_environmental_timeout(task: dict) -> bool:
    if str(task.get("status") or "").lower() != "timed_out":
        return False
    failure = _failure_text(task).lower()
    return any(marker in failure for marker in _ENVIRONMENTAL_TIMEOUT_MARKERS)


def create_failure_successors(bridge: Any, settings: dict) -> int:
    """Create at most one idempotent successor per exhausted failed task.

    This is intentionally a pure bridge-policy helper: it does not import or
    call the gateway watcher. Watcher scheduling and re-entrancy belong to the
    watcher method that invokes this function.
    """
    if not settings.get("enabled"):
        return 0
    max_chain = _integer(settings.get("max_chain"))
    tasks = bridge.list_tasks(limit=10_000)
    successor_parents = {
        str((task.get("spec") or {}).get("parent_task_id"))
        for task in tasks
        if isinstance(task.get("spec"), dict)
        and isinstance((task.get("spec") or {}).get("metadata"), dict)
        and task["spec"]["metadata"].get("failure_successor") is True
        and task["spec"].get("parent_task_id")
    }

    created = 0
    for task in tasks:
        if str(task.get("status") or "").lower() not in {"failed", "timed_out"}:
            continue
        if _is_environmental_timeout(task):
            continue
        task_id = str(task.get("task_id") or "").strip()
        spec = task.get("spec") or {}
        if not task_id or not isinstance(spec, dict):
            continue
        metadata = spec.get("metadata") or {}
        runtime = task.get("runtime") or {}
        if not isinstance(metadata, dict) or not isinstance(runtime, dict):
            continue
        chain_depth = _integer(metadata.get("successor_chain_depth"))
        repair_budget = _integer(metadata.get("auto_repair"))
        repair_attempts = _integer(runtime.get("auto_repair_attempts"))
        if (
            chain_depth >= max_chain
            or repair_attempts < repair_budget
            or task_id in successor_parents
        ):
            continue

        successor_metadata = dict(metadata)
        successor_metadata.update(
            {
                "failure_successor": True,
                "successor_chain_depth": chain_depth + 1,
            }
        )
        failure = _failure_text(task)
        objective = str(spec.get("objective") or "").strip()
        if failure:
            objective = (
                f"{objective}\n\nPrevious task {task_id} failed. "
                f"Diagnose and fix the root cause:\n{failure}"
            ).strip()
        successor_spec = dict(spec)
        successor_spec.update(
            {
                "task_id": None,
                "idempotency_key": f"failure-successor:{task_id}",
                "parent_task_id": task_id,
                "objective": objective,
                "metadata": successor_metadata,
            }
        )
        try:
            bridge.create_task(successor_spec)
        except Exception as exc:
            # One unservable parent must not abort the pass for every other
            # failed task; record it once and move on.
            if task_id not in _reported_skips:
                _reported_skips.add(task_id)
                logger.warning(
                    "worker-bridge failure successor skipped for %s: %s",
                    task_id,
                    exc,
                )
            continue
        _reported_skips.discard(task_id)
        successor_parents.add(task_id)
        created += 1
        logger.info(
            "worker-bridge failure successor created for %s at chain depth %d",
            task_id,
            chain_depth + 1,
        )
    return created
