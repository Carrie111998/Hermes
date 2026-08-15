"""Persistent task store helpers for Herbie's active task supervisor.

Runtime state is private/profile-local under ``$HERMES_HOME/task-supervisor``.
The canonical V1 store is ``tasks.json`` so queued task registration never
replaces the active task. Legacy ``active_task.json`` is read as a compatibility
source and migrated into the task store by write paths.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterator

from hermes_constants import get_hermes_home

TASK_STATES = {
    "RECEIVED",
    "PREFLIGHT",
    "ACTIVE",
    "BLOCKED",
    "WAITING_OWNER",
    "READY_FOR_INDEPENDENT_REVIEW",
    "COMPLETE",
    "ABORTED",
    "QUEUED",
}

ACTIVE_MONITORED_STATES = {
    "RECEIVED",
    "PREFLIGHT",
    "ACTIVE",
    "BLOCKED",
    "WAITING_OWNER",
    "READY_FOR_INDEPENDENT_REVIEW",
    "COMPLETE",
    "ABORTED",
    "QUEUED",
}
TERMINAL_STATES = {"READY_FOR_INDEPENDENT_REVIEW", "COMPLETE", "ABORTED"}
QUEUE_BLOCKING_STATES = {"RECEIVED", "PREFLIGHT", "ACTIVE", "BLOCKED", "WAITING_OWNER"}

REQUIRED_TASK_FIELDS = [
    "task_id",
    "title",
    "owner",
    "spec_filename",
    "spec_path",
    "spec_version",
    "spec_sha256",
    "status",
    "created_at",
    "started_at",
    "last_progress_at",
    "last_owner_update_at",
    "next_required_owner_update_at",
    "current_step",
    "next_step",
    "percent_or_stage_complete",
    "blocker_type",
    "blocker_detail",
    "checkpoint_commit",
    "checkpoint_artifact_path",
    "tool_budget_risk",
    "owner_notification_state",
    "internal_nudge_state",
    "completion_artifact",
    "closed_at",
]


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def format_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fingerprint(*parts: Any) -> str:
    raw = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class LedgerPaths:
    base_dir: Path
    active_task: Path
    events: Path
    dedupe: Path
    tasks: Path | None = None
    outbox: Path | None = None
    lock: Path | None = None

    @property
    def tasks_path(self) -> Path:
        return self.tasks or (self.base_dir / "tasks.json")

    @property
    def outbox_path(self) -> Path:
        return self.outbox or (self.base_dir / "notification_outbox.json")

    @property
    def lock_path(self) -> Path:
        return self.lock or (self.base_dir / ".watchdog.lock")


def default_paths(base_dir: Path | None = None) -> LedgerPaths:
    base = base_dir or (get_hermes_home() / "task-supervisor")
    return LedgerPaths(
        base_dir=base,
        active_task=base / "active_task.json",
        events=base / "events.jsonl",
        dedupe=base / "dedupe_state.json",
        tasks=base / "tasks.json",
        outbox=base / "notification_outbox.json",
        lock=base / ".watchdog.lock",
    )


def ensure_paths(paths: LedgerPaths) -> None:
    paths.base_dir.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_write(path: Path, data: Any) -> None:
    ensure_paths(default_paths(path.parent))
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


@contextmanager
def supervisor_lock(paths: LedgerPaths, timeout_seconds: float = 5.0) -> Iterator[None]:
    """Advisory lock around complete read/decide/write watchdog transactions."""

    ensure_paths(paths)
    deadline = utc_now().timestamp() + timeout_seconds
    with paths.lock_path.open("a+") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if utc_now().timestamp() >= deadline:
                    raise TimeoutError(f"task supervisor lock busy: {paths.lock_path}")
                import time

                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def empty_store() -> dict[str, Any]:
    return {"active_task_id": None, "tasks": {}, "queue": []}


def load_store(paths: LedgerPaths) -> dict[str, Any]:
    """Load canonical task store, migrating legacy active_task.json if needed."""

    if paths.tasks_path.exists():
        store = _read_json(paths.tasks_path)
    elif paths.active_task.exists():
        task = _read_json(paths.active_task)
        store = empty_store()
        task_id = task.get("task_id")
        if task_id:
            store["tasks"][task_id] = task
            if task.get("status") == "QUEUED":
                store["queue"].append(task_id)
            else:
                store["active_task_id"] = task_id
    else:
        store = empty_store()
    if not isinstance(store, dict) or not isinstance(store.get("tasks"), dict) or not isinstance(store.get("queue"), list):
        raise ValueError("malformed tasks.json")
    active_id = store.get("active_task_id")
    if active_id is not None and active_id not in store["tasks"]:
        raise ValueError("active_task_id does not reference a stored task")
    return store


def save_store(paths: LedgerPaths, store: dict[str, Any]) -> None:
    ensure_paths(paths)
    _atomic_write(paths.tasks_path, store)
    active = get_active_task(store)
    if active is not None:
        _atomic_write(paths.active_task, active)


def get_active_task(store: dict[str, Any]) -> dict[str, Any] | None:
    active_id = store.get("active_task_id")
    if not active_id:
        return None
    return store.get("tasks", {}).get(active_id)


def load_task(paths: LedgerPaths) -> dict[str, Any] | None:
    return get_active_task(load_store(paths))


def save_task(paths: LedgerPaths, task: dict[str, Any]) -> None:
    store = load_store(paths)
    task_id = task["task_id"]
    store.setdefault("tasks", {})[task_id] = task
    if task.get("status") == "QUEUED":
        if task_id not in store.setdefault("queue", []):
            store["queue"].append(task_id)
    else:
        store["active_task_id"] = task_id
        if task_id in store.setdefault("queue", []):
            store["queue"].remove(task_id)
    save_store(paths, store)


def append_event(paths: LedgerPaths, event: dict[str, Any]) -> None:
    ensure_paths(paths)
    line = (json.dumps(event, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(paths.events, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def load_dedupe(paths: LedgerPaths) -> dict[str, Any]:
    if not paths.dedupe.exists():
        return {"incidents": {}, "blockers": {}}
    state = _read_json(paths.dedupe)
    state.setdefault("incidents", {})
    state.setdefault("blockers", {})
    return state


def save_dedupe(paths: LedgerPaths, state: dict[str, Any]) -> None:
    ensure_paths(paths)
    _atomic_write(paths.dedupe, state)


def load_outbox(paths: LedgerPaths) -> dict[str, Any]:
    if not paths.outbox_path.exists():
        return {"notifications": {}}
    outbox = _read_json(paths.outbox_path)
    if not isinstance(outbox, dict) or not isinstance(outbox.get("notifications"), dict):
        raise ValueError("malformed notification outbox")
    return outbox


def save_outbox(paths: LedgerPaths, outbox: dict[str, Any]) -> None:
    ensure_paths(paths)
    _atomic_write(paths.outbox_path, outbox)


def validate_task(task: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in REQUIRED_TASK_FIELDS:
        if field not in task:
            errors.append(f"missing required field: {field}")
    status = task.get("status")
    if status not in TASK_STATES:
        errors.append(f"invalid status: {status!r}")
    if not task.get("spec_filename") or not task.get("spec_path") or not task.get("spec_sha256"):
        errors.append("missing spec provenance")
    if status == "ACTIVE" and not task.get("last_progress_at"):
        errors.append("ACTIVE task missing last_progress_at")
    if status == "BLOCKED" and not task.get("blocker_detail"):
        errors.append("BLOCKED task missing blocker_detail")
    return errors


def minutes_between(now: datetime, then: str | None) -> float | None:
    parsed = parse_time(then)
    if parsed is None:
        return None
    return (now - parsed).total_seconds() / 60.0


def add_minutes(value: datetime, minutes: int) -> datetime:
    return value + timedelta(minutes=minutes)


def _new_task(*, task_id: str, title: str, owner: str, spec_filename: str, spec_path: str, spec_version: str, spec_sha256: str, now: datetime, status: str, queued_reason: str | None = None) -> dict[str, Any]:
    queued = status == "QUEUED"
    return {
        "task_id": task_id,
        "title": title,
        "owner": owner,
        "spec_filename": spec_filename,
        "spec_path": spec_path,
        "spec_version": spec_version,
        "spec_sha256": spec_sha256,
        "status": status,
        "created_at": format_time(now),
        "started_at": None if queued else format_time(now),
        "last_progress_at": None if queued else format_time(now),
        "last_owner_update_at": None,
        "last_owner_notification_attempt_at": None,
        "last_owner_notification_delivered_at": None,
        "next_required_owner_update_at": None if queued else format_time(add_minutes(now, 45)),
        "current_step": queued_reason or ("Queued behind existing owner task" if queued else "Task received"),
        "next_step": "Wait for owner authorization or active task closeout" if queued else "Run preflight",
        "percent_or_stage_complete": "queued" if queued else "received",
        "blocker_type": None,
        "blocker_detail": None,
        "checkpoint_commit": None,
        "checkpoint_artifact_path": None,
        "tool_budget_risk": "normal",
        "owner_notification_state": "pending_queued_notice" if queued else "pending_task_accepted_preflight",
        "internal_nudge_state": {},
        "completion_artifact": None,
        "closed_at": None,
    }


def create_task_entry(
    paths: LedgerPaths,
    *,
    task_id: str,
    title: str,
    owner: str,
    spec_filename: str,
    spec_path: str,
    spec_version: str,
    spec_sha256: str,
    now: datetime | None = None,
    parallel_authorized: bool = False,
) -> dict[str, Any]:
    """Create a RECEIVED task or safely queue it behind the active task."""

    ensure_paths(paths)
    now = (now or utc_now()).astimezone(UTC).replace(microsecond=0)
    with supervisor_lock(paths):
        store = load_store(paths)
        existing = get_active_task(store)
        existing_status = existing.get("status") if existing else None
        must_queue = bool(existing_status in QUEUE_BLOCKING_STATES and not parallel_authorized)
        task = _new_task(
            task_id=task_id,
            title=title,
            owner=owner,
            spec_filename=spec_filename,
            spec_path=spec_path,
            spec_version=spec_version,
            spec_sha256=spec_sha256,
            now=now,
            status="QUEUED" if must_queue else "RECEIVED",
            queued_reason=f"Queued behind active task {existing.get('task_id')}" if must_queue and existing else None,
        )
        store["tasks"][task_id] = task
        if must_queue:
            if task_id not in store["queue"]:
                store["queue"].append(task_id)
        else:
            store["active_task_id"] = task_id
        save_store(paths, store)
        append_event(paths, {"at": format_time(now), "task_id": task_id, "event": "state_transition", "from": None, "to": task["status"], "detail": "parallel owner task queued" if must_queue else "task received"})
        return task


def promote_next_queued_task(paths: LedgerPaths, *, now: datetime | None = None, owner_controlled: bool = True) -> dict[str, Any] | None:
    """Promote the next queued task only when explicitly invoked by owner flow."""

    now = (now or utc_now()).astimezone(UTC).replace(microsecond=0)
    with supervisor_lock(paths):
        store = load_store(paths)
        if owner_controlled and store.get("active_task_id"):
            return None
        if not store.get("queue"):
            return None
        task_id = store["queue"].pop(0)
        task = store["tasks"][task_id]
        task.update({"status": "RECEIVED", "started_at": format_time(now), "last_progress_at": format_time(now), "current_step": "Task promoted from queue", "next_step": "Run preflight", "percent_or_stage_complete": "received", "owner_notification_state": "pending_task_accepted_preflight"})
        store["active_task_id"] = task_id
        save_store(paths, store)
        append_event(paths, {"at": format_time(now), "task_id": task_id, "event": "state_transition", "from": "QUEUED", "to": "RECEIVED", "detail": "owner-controlled queue promotion"})
        return task
