"""Guardrailed, audited lane-manifest mutations."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from hermes_constants import get_default_hermes_root
from hermes_cli.cost import caps as cost_caps
from hermes_cli.cost import config as cost_config
from hermes_cli.lanes.doctor import run_lane_doctor
from hermes_cli.lanes.manifest import (
    LaneManifestError,
    default_path,
    validate_manifest,
)
from hermes_cli.lanes.schema import connect as lane_connect
from hermes_cli.sqlite_util import retrying_write_txn


_AUDIT_DDL = (
    """
    CREATE TABLE IF NOT EXISTS lane_manifest_audit (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      lane_id TEXT NOT NULL,
      action TEXT NOT NULL CHECK (
        action IN ('enable','disable','enable_publish','disable_publish')
      ),
      previous_value INTEGER NOT NULL CHECK (previous_value IN (0,1)),
      new_value INTEGER NOT NULL CHECK (new_value IN (0,1)),
      actor TEXT NOT NULL,
      timestamp_utc TEXT NOT NULL,
      notes TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_lane_manifest_audit_lane_time
        ON lane_manifest_audit(lane_id, timestamp_utc)
    """,
)
_VALID_ACTIONS = frozenset(
    {"enable", "disable", "enable_publish", "disable_publish"}
)


@dataclass(frozen=True)
class LaneEnableResult:
    """Stable result shared by all four guarded mutations."""

    lane_id: str
    action: str
    success: bool
    exit_code: int
    previous_value: bool | None = None
    new_value: bool | None = None
    previous_enabled: bool | None = None
    new_enabled: bool | None = None
    backup_path: str | None = None
    audit_row_id: int | None = None
    audit_row_ids: tuple[int, ...] = ()
    message: str = ""
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
        )


def _utc_now(value: datetime | None = None) -> datetime:
    instant = value or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _paths(
    manifest_path: str | Path | None,
    db_path: str | Path | None,
) -> tuple[Path, Path]:
    root = get_default_hermes_root()
    return (
        Path(manifest_path or default_path()).expanduser(),
        Path(db_path or root / "kanban.db").expanduser(),
    )


def ensure_audit_migrated(
    db_path: str | Path | None = None,
) -> None:
    """Create the additive audit schema in one retrying transaction."""
    connection = lane_connect(db_path)
    try:
        with retrying_write_txn(connection):
            for statement in _AUDIT_DDL:
                connection.execute(statement)
    finally:
        connection.close()


def _read_only_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _read_raw_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LaneManifestError(
            f"cannot read lane manifest {path}: {exc}"
        ) from exc
    validate_manifest(raw)
    return raw


def _target_lane(
    raw: dict[str, Any],
    lane_id: str,
) -> dict[str, Any] | None:
    normalized = str(lane_id).strip().lower()
    for item in raw.get("lanes") or []:
        if str(item.get("lane_id") or "").strip().lower() == normalized:
            return item
    return None


def _refusal(
    lane_id: str,
    action: str,
    message: str,
    *,
    usage: bool = False,
) -> LaneEnableResult:
    return LaneEnableResult(
        lane_id=str(lane_id).strip().lower(),
        action=action,
        success=False,
        exit_code=2 if usage else 1,
        message=message,
        errors=(message,),
    )


def _guard_force(
    lane_id: str,
    action: str,
    force_flag: bool,
) -> LaneEnableResult | None:
    if force_flag:
        return None
    return _refusal(
        lane_id,
        action,
        "refused: --i-understand-this-is-live is required",
        usage=True,
    )


def _programme_state(db_path: Path) -> str:
    connection = _read_only_connection(db_path)
    try:
        row = connection.execute(
            "SELECT state FROM programme_state WHERE id=1"
        ).fetchone()
    finally:
        connection.close()
    return str(row["state"]) if row is not None else "UNKNOWN"


def _kill_switch_count(db_path: Path) -> int:
    connection = _read_only_connection(db_path)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='task_kill_switch'"
        ).fetchone()
        if exists is None:
            return 0
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM task_kill_switch"
            ).fetchone()[0]
        )
    finally:
        connection.close()


def _near_daily_cap(db_path: Path) -> tuple[bool, float, float]:
    connection = _read_only_connection(db_path)
    try:
        spend = cost_caps.daily_spend_aud_billable(conn=connection)
    finally:
        connection.close()
    cap = float(cost_config.GLOBAL_DAILY_CAP_AUD)
    return spend >= cap * 0.9, float(spend), cap


def _backup_path(
    manifest_path: Path,
    *,
    action: str,
    lane_id: str,
    now: datetime,
) -> Path:
    stamp = now.astimezone().strftime("%Y%m%d-%H%M%S")
    return manifest_path.with_name(
        f"{manifest_path.name}.pre-{action}-{lane_id}.{stamp}"
    )


def _atomic_write_manifest(
    path: Path,
    raw: dict[str, Any],
) -> None:
    """Write YAML through a same-directory fsynced temp file and rename."""
    validate_manifest(raw)
    encoded = yaml.safe_dump(raw, sort_keys=False)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _insert_audit_rows(
    *,
    db_path: Path,
    lane_id: str,
    changes: tuple[tuple[str, bool, bool], ...],
    notes: str | None,
    timestamp: datetime,
) -> tuple[int, ...]:
    ensure_audit_migrated(db_path)
    connection = lane_connect(db_path)
    try:
        with retrying_write_txn(connection):
            row_ids = []
            for action, previous, new in changes:
                if action not in _VALID_ACTIONS:
                    raise ValueError(f"invalid lane audit action: {action}")
                cursor = connection.execute(
                    """
                    INSERT INTO lane_manifest_audit (
                        lane_id, action, previous_value, new_value,
                        actor, timestamp_utc, notes
                    ) VALUES (?, ?, ?, ?, 'cli-operator', ?, ?)
                    """,
                    (
                        lane_id,
                        action,
                        int(previous),
                        int(new),
                        _iso(timestamp),
                        str(notes).strip() if notes else None,
                    ),
                )
                row_ids.append(int(cursor.lastrowid))
            return tuple(row_ids)
    finally:
        connection.close()


def _apply_changes(
    *,
    lane_id: str,
    action: str,
    raw: dict[str, Any],
    manifest_path: Path,
    db_path: Path,
    changes: tuple[tuple[str, str, bool, bool], ...],
    notes: str | None,
    now: datetime,
) -> LaneEnableResult:
    target = _target_lane(raw, lane_id)
    assert target is not None
    backup = _backup_path(
        manifest_path,
        action=action.replace("_", "-"),
        lane_id=lane_id,
        now=now,
    )
    shutil.copy2(manifest_path, backup)
    for _audit_action, key, _previous, new in changes:
        target[key] = bool(new)
    try:
        _atomic_write_manifest(manifest_path, raw)
        audit_ids = _insert_audit_rows(
            db_path=db_path,
            lane_id=lane_id,
            changes=tuple(
                (audit_action, previous, new)
                for audit_action, _key, previous, new in changes
            ),
            notes=notes,
            timestamp=now,
        )
    except Exception:
        if backup.exists():
            restored = _read_raw_manifest(backup)
            _atomic_write_manifest(manifest_path, restored)
        raise
    previous = changes[0][2]
    new = changes[0][3]
    enabled_change = next(
        (
            (before, after)
            for _audit_action, key, before, after in changes
            if key == "enabled"
        ),
        (None, None),
    )
    return LaneEnableResult(
        lane_id=lane_id,
        action=action,
        success=True,
        exit_code=0,
        previous_value=previous,
        new_value=new,
        previous_enabled=enabled_change[0],
        new_enabled=enabled_change[1],
        backup_path=str(backup),
        audit_row_id=audit_ids[0],
        audit_row_ids=audit_ids,
        message=f"{lane_id}: {action} complete",
    )


def enable_lane(
    lane_id: str,
    force_flag: bool,
    *,
    manifest_path: str | Path | None = None,
    db_path: str | Path | None = None,
    notes: str | None = None,
    doctor_runner: Callable[..., Any] = run_lane_doctor,
    now: datetime | None = None,
) -> LaneEnableResult:
    """Enable one healthy lane only after every live preflight passes."""
    action = "enable"
    normalized = str(lane_id).strip().lower()
    force_refusal = _guard_force(normalized, action, force_flag)
    if force_refusal:
        return force_refusal
    manifest, database = _paths(manifest_path, db_path)
    raw = _read_raw_manifest(manifest)
    target = _target_lane(raw, normalized)
    if target is None:
        return _refusal(normalized, action, f"unknown lane: {normalized}")
    state = _programme_state(database)
    if state != "RUNNING":
        return _refusal(
            normalized,
            action,
            f"programme is {state}; run hermes programme resume first",
        )
    doctor = doctor_runner(
        normalized,
        manifest_path=manifest,
        db_path=database,
    )
    doctor_value = (
        doctor.to_dict() if callable(getattr(doctor, "to_dict", None))
        else dict(doctor)
    )
    if not doctor_value.get("success"):
        return _refusal(
            normalized,
            action,
            "lane doctor failed: "
            + json.dumps(doctor_value.get("errors") or []),
        )
    if doctor_value.get("module_status") not in (None, "RESOLVABLE"):
        return _refusal(
            normalized,
            action,
            f"module_status={doctor_value.get('module_status')}",
        )
    if bool(target.get("enabled")):
        return _refusal(normalized, action, "lane is already enabled")
    killed = _kill_switch_count(database)
    if killed:
        return _refusal(
            normalized,
            action,
            f"kill switch is tripped ({killed} active rows)",
        )
    near_cap, spend, cap = _near_daily_cap(database)
    if near_cap:
        return _refusal(
            normalized,
            action,
            f"daily spend AUD {spend:.6f} is within 10% of cap AUD {cap:.2f}",
        )
    instant = _utc_now(now)
    return _apply_changes(
        lane_id=normalized,
        action=action,
        raw=raw,
        manifest_path=manifest,
        db_path=database,
        changes=(("enable", "enabled", False, True),),
        notes=notes,
        now=instant,
    )


def _active_disable_counts(
    db_path: Path,
    lane_id: str,
) -> tuple[int, int]:
    connection = _read_only_connection(db_path)
    try:
        tasks = int(
            connection.execute(
                "SELECT COUNT(*) FROM lane_task "
                "WHERE lane_id=? AND status IN ('claiming','claimed')",
                (lane_id,),
            ).fetchone()[0]
        )
        approvals = int(
            connection.execute(
                "SELECT COUNT(*) FROM lane_approval_queue "
                "WHERE lane_id=? AND status='pending'",
                (lane_id,),
            ).fetchone()[0]
        )
        return tasks, approvals
    finally:
        connection.close()


def disable_lane(
    lane_id: str,
    force_flag: bool,
    *,
    manifest_path: str | Path | None = None,
    db_path: str | Path | None = None,
    notes: str | None = None,
    now: datetime | None = None,
) -> LaneEnableResult:
    """Disable one idle lane and fail closed for publishing."""
    action = "disable"
    normalized = str(lane_id).strip().lower()
    force_refusal = _guard_force(normalized, action, force_flag)
    if force_refusal:
        return force_refusal
    manifest, database = _paths(manifest_path, db_path)
    raw = _read_raw_manifest(manifest)
    target = _target_lane(raw, normalized)
    if target is None:
        return _refusal(normalized, action, f"unknown lane: {normalized}")
    if not bool(target.get("enabled")):
        return _refusal(normalized, action, "lane is already disabled")
    active_tasks, active_approvals = _active_disable_counts(
        database,
        normalized,
    )
    if active_tasks:
        return _refusal(
            normalized,
            action,
            f"{active_tasks} claimed/claiming lane tasks remain; halt first",
        )
    if active_approvals:
        return _refusal(
            normalized,
            action,
            f"{active_approvals} active approval rows remain",
        )
    changes: list[tuple[str, str, bool, bool]] = [
        ("disable", "enabled", True, False)
    ]
    if bool(target.get("publish_enabled")):
        changes.append(
            ("disable_publish", "publish_enabled", True, False)
        )
    return _apply_changes(
        lane_id=normalized,
        action=action,
        raw=raw,
        manifest_path=manifest,
        db_path=database,
        changes=tuple(changes),
        notes=notes,
        now=_utc_now(now),
    )


def _has_successful_roundtrip(
    db_path: Path,
    lane_id: str,
    *,
    now: datetime,
) -> bool:
    connection = _read_only_connection(db_path)
    try:
        publish_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(lane_publish_log)"
            )
        }
        outcome_column = (
            "outcome" if "outcome" in publish_columns else "status"
        )
        published = connection.execute(
            f"""
            SELECT 1 FROM lane_publish_log
             WHERE lane_id=?
               AND {outcome_column}='success'
               AND LOWER(external_target) LIKE '%smoke%'
             LIMIT 1
            """,
            (lane_id,),
        ).fetchone()
        if published is not None:
            return True
        cutoff = _iso(now - timedelta(days=7))
        approval_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(lane_approval_queue)"
            )
        }
        approved_states = ("approved", "granted")
        timestamp_expr = (
            "COALESCE(grant_ts, created_at)"
            if "grant_ts" in approval_columns
            else "created_at"
        )
        return connection.execute(
            f"""
            SELECT 1 FROM lane_approval_queue
             WHERE lane_id=?
               AND status IN (?, ?)
               AND {timestamp_expr} >= ?
             LIMIT 1
            """,
            (lane_id, *approved_states, cutoff),
        ).fetchone() is not None
    finally:
        connection.close()


def enable_publish(
    lane_id: str,
    force_flag: bool,
    *,
    manifest_path: str | Path | None = None,
    db_path: str | Path | None = None,
    notes: str | None = None,
    now: datetime | None = None,
) -> LaneEnableResult:
    """Enable publishing only after a recent successful full round-trip."""
    action = "enable_publish"
    normalized = str(lane_id).strip().lower()
    force_refusal = _guard_force(normalized, action, force_flag)
    if force_refusal:
        return force_refusal
    manifest, database = _paths(manifest_path, db_path)
    raw = _read_raw_manifest(manifest)
    target = _target_lane(raw, normalized)
    if target is None:
        return _refusal(normalized, action, f"unknown lane: {normalized}")
    if not bool(target.get("enabled")):
        return _refusal(normalized, action, "lane is disabled")
    if bool(target.get("publish_enabled")):
        return _refusal(
            normalized,
            action,
            "publishing is already enabled",
        )
    instant = _utc_now(now)
    if not _has_successful_roundtrip(database, normalized, now=instant):
        return _refusal(
            normalized,
            action,
            "no successful smoke publish or approved round-trip in 7 days",
        )
    return _apply_changes(
        lane_id=normalized,
        action=action,
        raw=raw,
        manifest_path=manifest,
        db_path=database,
        changes=(
            ("enable_publish", "publish_enabled", False, True),
        ),
        notes=notes,
        now=instant,
    )


def disable_publish(
    lane_id: str,
    force_flag: bool,
    *,
    manifest_path: str | Path | None = None,
    db_path: str | Path | None = None,
    notes: str | None = None,
    now: datetime | None = None,
) -> LaneEnableResult:
    """Disable publishing independently of lane execution."""
    action = "disable_publish"
    normalized = str(lane_id).strip().lower()
    force_refusal = _guard_force(normalized, action, force_flag)
    if force_refusal:
        return force_refusal
    manifest, database = _paths(manifest_path, db_path)
    raw = _read_raw_manifest(manifest)
    target = _target_lane(raw, normalized)
    if target is None:
        return _refusal(normalized, action, f"unknown lane: {normalized}")
    if not bool(target.get("publish_enabled")):
        return _refusal(
            normalized,
            action,
            "publishing is already disabled",
        )
    return _apply_changes(
        lane_id=normalized,
        action=action,
        raw=raw,
        manifest_path=manifest,
        db_path=database,
        changes=(
            ("disable_publish", "publish_enabled", True, False),
        ),
        notes=notes,
        now=_utc_now(now),
    )


def list_audit(
    lane_id: str,
    *,
    limit: int = 50,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    normalized_limit = int(limit)
    if normalized_limit <= 0:
        raise ValueError("limit must be greater than zero")
    ensure_audit_migrated(db_path)
    connection = lane_connect(db_path)
    try:
        rows = connection.execute(
            """
            SELECT id, lane_id, action, previous_value, new_value,
                   actor, timestamp_utc, notes
              FROM lane_manifest_audit
             WHERE lane_id=?
             ORDER BY timestamp_utc DESC, id DESC
             LIMIT ?
            """,
            (str(lane_id).strip().lower(), normalized_limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def audit_summary(
    db_path: str | Path | None = None,
) -> str:
    database = Path(
        db_path or get_default_hermes_root() / "kanban.db"
    ).expanduser()
    connection = _read_only_connection(database)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='lane_manifest_audit'"
        ).fetchone()
        if exists is None:
            return "lane_manifest_audit: 0 rows"
        aggregate = connection.execute(
            """
            SELECT COUNT(*) AS rows,
                   COUNT(DISTINCT lane_id) AS lanes
              FROM lane_manifest_audit
            """
        ).fetchone()
        count = int(aggregate["rows"])
        if count == 0:
            return "lane_manifest_audit: 0 rows"
        recent = connection.execute(
            """
            SELECT lane_id, action, timestamp_utc
              FROM lane_manifest_audit
             ORDER BY timestamp_utc DESC, id DESC
             LIMIT 1
            """
        ).fetchone()
        return (
            f"lane_manifest_audit: {count} rows across "
            f"{int(aggregate['lanes'])} lanes; most recent: "
            f"{recent['lane_id']} {recent['action']} "
            f"{recent['timestamp_utc']}"
        )
    finally:
        connection.close()


__all__ = [
    "LaneEnableResult",
    "audit_summary",
    "disable_lane",
    "disable_publish",
    "enable_lane",
    "enable_publish",
    "ensure_audit_migrated",
    "list_audit",
]
