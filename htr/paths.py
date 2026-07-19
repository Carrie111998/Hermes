"""HTR workspace path contract under ``~/.hermes/runs/``."""

from __future__ import annotations

from pathlib import Path

from htr.ids import validate_id


def _validate_path_component(value: str, name: str) -> str:
    if not value or value in {".", ".."}:
        raise ValueError(f"invalid {name}: empty or reserved")
    if "/" in value or "\\" in value or ".." in value:
        raise ValueError(f"invalid {name}: path traversal not allowed")
    return value


def default_runs_root() -> Path:
    """Return the default HTR runs root (``HERMES_HOME/runs`` when available)."""
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "runs"
    except Exception:
        return Path.home() / ".hermes" / "runs"


def runs_root(base_dir: Path | None = None) -> Path:
    return Path(base_dir) if base_dir is not None else default_runs_root()


def run_root(run_id: str, base_dir: Path | None = None) -> Path:
    _validate_path_component(run_id, "run_id")
    if not validate_id(run_id, "run"):
        raise ValueError(f"invalid run_id format: {run_id!r}")
    return runs_root(base_dir) / run_id


def run_manifest_path(run_id: str, base_dir: Path | None = None) -> Path:
    return run_root(run_id, base_dir) / "run_manifest.json"


def task_events_path(run_id: str, base_dir: Path | None = None) -> Path:
    return run_root(run_id, base_dir) / "task_events.jsonl"


def approvals_path(run_id: str, base_dir: Path | None = None) -> Path:
    return run_root(run_id, base_dir) / "approvals.jsonl"


def reports_dir(run_id: str, base_dir: Path | None = None) -> Path:
    return run_root(run_id, base_dir) / "reports"


def tasks_dir(run_id: str, base_dir: Path | None = None) -> Path:
    return run_root(run_id, base_dir) / "tasks"


def task_dir(run_id: str, task_id: str, base_dir: Path | None = None) -> Path:
    _validate_path_component(task_id, "task_id")
    if not validate_id(task_id, "task"):
        raise ValueError(f"invalid task_id format: {task_id!r}")
    return tasks_dir(run_id, base_dir) / task_id


def task_card_path(run_id: str, task_id: str, base_dir: Path | None = None) -> Path:
    return task_dir(run_id, task_id, base_dir) / "task_card.json"


def task_status_path(run_id: str, task_id: str, base_dir: Path | None = None) -> Path:
    return task_dir(run_id, task_id, base_dir) / "task_status.json"


def attempts_dir(run_id: str, task_id: str, base_dir: Path | None = None) -> Path:
    return task_dir(run_id, task_id, base_dir) / "attempts"


def attempt_dir(
    run_id: str,
    task_id: str,
    attempt_id: str,
    base_dir: Path | None = None,
) -> Path:
    _validate_path_component(attempt_id, "attempt_id")
    if not validate_id(attempt_id, "attempt"):
        raise ValueError(f"invalid attempt_id format: {attempt_id!r}")
    return attempts_dir(run_id, task_id, base_dir) / attempt_id


def attempt_status_path(
    run_id: str,
    task_id: str,
    attempt_id: str,
    base_dir: Path | None = None,
) -> Path:
    return attempt_dir(run_id, task_id, attempt_id, base_dir) / "attempt_status.json"


def artifact_manifest_path(
    run_id: str,
    task_id: str,
    attempt_id: str,
    base_dir: Path | None = None,
) -> Path:
    return attempt_dir(run_id, task_id, attempt_id, base_dir) / "artifact_manifest.json"


def tool_calls_path(
    run_id: str,
    task_id: str,
    attempt_id: str,
    base_dir: Path | None = None,
) -> Path:
    return attempt_dir(run_id, task_id, attempt_id, base_dir) / "tool_calls.jsonl"


def input_dir(
    run_id: str,
    task_id: str,
    attempt_id: str,
    base_dir: Path | None = None,
) -> Path:
    return attempt_dir(run_id, task_id, attempt_id, base_dir) / "input"


def working_dir(
    run_id: str,
    task_id: str,
    attempt_id: str,
    base_dir: Path | None = None,
) -> Path:
    return attempt_dir(run_id, task_id, attempt_id, base_dir) / "working"


def output_dir(
    run_id: str,
    task_id: str,
    attempt_id: str,
    base_dir: Path | None = None,
) -> Path:
    return attempt_dir(run_id, task_id, attempt_id, base_dir) / "output"


def result_json_path(
    run_id: str,
    task_id: str,
    attempt_id: str,
    base_dir: Path | None = None,
) -> Path:
    return output_dir(run_id, task_id, attempt_id, base_dir) / "result.json"


def artifacts_dir(
    run_id: str,
    task_id: str,
    attempt_id: str,
    base_dir: Path | None = None,
) -> Path:
    return attempt_dir(run_id, task_id, attempt_id, base_dir) / "artifacts"


def logs_dir(
    run_id: str,
    task_id: str,
    attempt_id: str,
    base_dir: Path | None = None,
) -> Path:
    return attempt_dir(run_id, task_id, attempt_id, base_dir) / "logs"


def verification_dir(
    run_id: str,
    task_id: str,
    attempt_id: str,
    base_dir: Path | None = None,
) -> Path:
    return attempt_dir(run_id, task_id, attempt_id, base_dir) / "verification"


def heal_dir(
    run_id: str,
    task_id: str,
    attempt_id: str,
    base_dir: Path | None = None,
) -> Path:
    return attempt_dir(run_id, task_id, attempt_id, base_dir) / "heal"
