"""Read-only dependency contract checks for the Kanban projection."""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path
from typing import Any


PROJECTION_DEPENDENCY_CONTRACT = {
    "name": "hermes-kanban-outcome-first",
    "version": 1,
    "required_api": {
        "connect": (),
        "create_task": ("initial_status", "idempotency_key"),
        "get_task": (),
        "write_txn": (),
        "publish_task_output": ("summary", "metadata", "with_reason"),
        "complete_task": ("result", "summary", "metadata", "with_reason"),
        "list_attachments": (),
        "store_attachment_bytes": ("uploaded_by", "board", "max_bytes"),
    },
    "required_task_columns": (
        "result",
        "current_step",
        "progress_percent",
        "latest_log",
        "files_changed",
        "progress_updated_at",
        "block_kind",
    ),
    "required_statuses": ("working", "output_ready"),
}


def probe_projection_dependency(
    api: Any,
    *,
    kanban_db_path: Path | None = None,
) -> dict[str, Any]:
    """Verify the outcome-first prerequisite without mutating Kanban state."""

    missing_api: list[str] = []
    incompatible_api: list[str] = []
    for name, required_parameters in PROJECTION_DEPENDENCY_CONTRACT["required_api"].items():
        function = getattr(api, name, None)
        if not callable(function):
            missing_api.append(name)
            continue
        try:
            parameters = inspect.signature(function).parameters
        except (TypeError, ValueError):
            incompatible_api.append(f"{name}:signature-unavailable")
            continue
        missing_parameters = sorted(set(required_parameters) - set(parameters))
        if missing_parameters:
            incompatible_api.append(f"{name}:missing-{','.join(missing_parameters)}")

    valid_statuses = set(
        getattr(api, "VALID_STATUSES", getattr(api, "VALID_TASK_STATUSES", ()))
    )
    missing_statuses = sorted(
        set(PROJECTION_DEPENDENCY_CONTRACT["required_statuses"]) - valid_statuses
    )
    missing_columns: list[str] = []
    database_error: str | None = None
    if kanban_db_path is not None:
        path = Path(kanban_db_path)
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            connection.row_factory = sqlite3.Row
            try:
                columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
                }
            finally:
                connection.close()
            missing_columns = sorted(
                set(PROJECTION_DEPENDENCY_CONTRACT["required_task_columns"]) - columns
            )
        except (OSError, sqlite3.Error) as exc:
            database_error = f"{type(exc).__name__}: {exc}"[:500]

    blockers = bool(
        missing_api
        or incompatible_api
        or missing_statuses
        or missing_columns
        or database_error
    )
    return {
        "contract": PROJECTION_DEPENDENCY_CONTRACT["name"],
        "contract_version": PROJECTION_DEPENDENCY_CONTRACT["version"],
        "ready": not blockers,
        "missing_api": sorted(missing_api),
        "incompatible_api": sorted(incompatible_api),
        "missing_statuses": missing_statuses,
        "missing_task_columns": missing_columns,
        "database_error": database_error,
    }
