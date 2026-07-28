"""Bounded stage successors for explicitly opted-in successful tasks."""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

logger = logging.getLogger("gateway.run")

DEFAULT_STAGE_SUCCESSORS_ENABLED = True
DEFAULT_STAGE_SUCCESSOR_MAX_CHAIN = 4
MAX_SUMMARY_CHARS = 4_000
_SUCCESS_STATUSES = {"succeeded", "accepted"}

# Parents whose public bridge creation failed already in this process. This
# prevents an unservable opted-in task from flooding every watcher tick.
_reported_skips: set[str] = set()


def resolve_stage_successor_settings(cfg: Any) -> dict:
    """Extract ``worker_bridge.stage_successors`` with bounded defaults."""
    worker_bridge = cfg.get("worker_bridge", {}) if isinstance(cfg, dict) else {}
    raw = (
        worker_bridge.get("stage_successors", {})
        if isinstance(worker_bridge, dict)
        else {}
    )
    if not isinstance(raw, dict):
        raw = {}
    try:
        max_chain = max(
            0,
            int(raw.get("max_chain", DEFAULT_STAGE_SUCCESSOR_MAX_CHAIN)),
        )
    except (TypeError, ValueError):
        max_chain = DEFAULT_STAGE_SUCCESSOR_MAX_CHAIN
    return {
        "enabled": raw.get("enabled", DEFAULT_STAGE_SUCCESSORS_ENABLED) is not False,
        "max_chain": max_chain,
    }


def _integer(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _task_summary(task: dict) -> str:
    result = task.get("result") or {}
    if not isinstance(result, dict):
        return ""
    return str(result.get("summary") or "").strip()[:MAX_SUMMARY_CHARS]


def _render_objective(template: str, parent_task_id: str, summary: str) -> str:
    return (
        template.replace("{parent_task_id}", parent_task_id)
        .replace("{summary}", summary)
        .strip()
    )


def create_stage_successors(bridge: Any, settings: dict) -> int:
    """Create one idempotent authored successor per opted-in successful task."""
    if not settings.get("enabled"):
        return 0
    max_chain = _integer(settings.get("max_chain"))
    tasks = bridge.list_tasks(limit=10_000)
    continued_stages = {
        str((task.get("spec") or {}).get("metadata", {}).get("source_stage"))
        for task in tasks
        if isinstance(task.get("spec"), dict)
        and isinstance((task.get("spec") or {}).get("metadata"), dict)
        and task["spec"]["metadata"].get("source_stage")
    }

    created = 0
    for task in tasks:
        if str(task.get("status") or "").lower() not in _SUCCESS_STATUSES:
            continue
        task_id = str(task.get("task_id") or "").strip()
        spec = task.get("spec") or {}
        if not task_id or not isinstance(spec, dict):
            continue
        metadata = spec.get("metadata") or {}
        if not isinstance(metadata, dict):
            continue
        on_success = metadata.get("on_success")
        if not isinstance(on_success, dict) or on_success.get("enabled") is not True:
            continue
        next_stage = on_success.get("next")
        if not isinstance(next_stage, dict):
            continue
        worker = str(next_stage.get("worker") or "").strip()
        template = str(next_stage.get("objective_template") or "").strip()
        if not worker or not template:
            continue
        chain_depth = _integer(metadata.get("successor_chain_depth"))
        if chain_depth >= max_chain or task_id in continued_stages:
            continue

        next_metadata = next_stage.get("metadata") or {}
        if not isinstance(next_metadata, dict):
            logger.info(
                "worker-bridge stage successor skipped for %s: "
                "next.metadata must be an object",
                task_id,
            )
            continue
        successor_metadata = deepcopy(next_metadata)
        successor_metadata.setdefault("continuation_kind", "stage")
        successor_metadata.update(
            {
                "source_stage": task_id,
                "successor_chain_depth": chain_depth + 1,
            }
        )
        workspace = next_stage.get("workspace")
        if workspace is None:
            workspace = spec.get("workspace")
        successor_spec = deepcopy(spec)
        successor_spec.update(
            {
                "task_id": None,
                "idempotency_key": f"stage-successor:{task_id}",
                "parent_task_id": task_id,
                "worker": worker,
                "objective": _render_objective(
                    template,
                    task_id,
                    _task_summary(task),
                ),
                "workspace": deepcopy(workspace),
                "priority": _integer(next_stage.get("priority"), 50),
                "metadata": successor_metadata,
            }
        )
        try:
            bridge.create_task(successor_spec)
        except Exception as exc:
            if task_id not in _reported_skips:
                _reported_skips.add(task_id)
                logger.warning(
                    "worker-bridge stage successor skipped for %s: %s",
                    task_id,
                    exc,
                )
            continue
        _reported_skips.discard(task_id)
        continued_stages.add(task_id)
        created += 1
        logger.info(
            "worker-bridge stage successor created for %s at chain depth %d",
            task_id,
            chain_depth + 1,
        )
    return created
