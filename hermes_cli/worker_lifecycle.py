"""Typed terminal lifecycle evidence for dispatcher-supervised workers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional

SCHEMA_VERSION = 1
TRANSIENT_PROVIDER = "transient_provider"


def build_terminal_event(
    result: Any,
    *,
    session_id: Optional[str] = None,
    exit_code: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Build identity-bound terminal evidence when a worker contract is present."""
    path = os.environ.get("HERMES_WORKER_LIFECYCLE_EVENT_PATH", "").strip()
    task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    run_raw = os.environ.get("HERMES_KANBAN_RUN_ID", "").strip()
    attempt_raw = os.environ.get("HERMES_WORKER_LIFECYCLE_ATTEMPT", "").strip()
    expected_session = os.environ.get("HERMES_WORKER_SESSION_ID", "").strip()
    workspace_raw = os.environ.get("HERMES_KANBAN_WORKSPACE", "").strip()
    if not all((path, task_id, run_raw, attempt_raw, expected_session, workspace_raw)):
        return None
    try:
        run_id = int(run_raw)
        attempt = int(attempt_raw)
    except (TypeError, ValueError):
        return None
    actual_session = str(session_id or "").strip()
    if run_id <= 0 or attempt <= 0 or actual_session != expected_session:
        return None

    failed = bool(isinstance(result, Mapping) and result.get("failed"))
    failure_reason = (
        str(result.get("failure_reason") or "").strip() if failed else ""
    )
    if not failed:
        classification = "success"
    elif failure_reason == TRANSIENT_PROVIDER:
        classification = TRANSIENT_PROVIDER
    elif failure_reason == "rate_limit":
        classification = "rate_limited"
    elif failure_reason == "billing":
        classification = "billing"
    else:
        classification = "failed"
    actual_exit = int(exit_code if exit_code is not None else (1 if failed else 0))
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "terminal",
        "task_id": task_id,
        "run_id": run_id,
        "attempt": attempt,
        "session_id": actual_session,
        "worktree": str(Path(workspace_raw).resolve()),
        "owner_pid": os.getpid(),
        "exit_code": actual_exit,
        "failure_reason": failure_reason,
        "classification": classification,
    }


def emit_terminal_event(
    result: Any,
    *,
    session_id: Optional[str] = None,
    exit_code: Optional[int] = None,
) -> bool:
    """Atomically write a terminal lifecycle event for a supervised worker."""
    event = build_terminal_event(
        result, session_id=session_id, exit_code=exit_code
    )
    if event is None:
        return False
    target = Path(os.environ["HERMES_WORKER_LIFECYCLE_EVENT_PATH"])
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return True
