"""BUSY-safe API for reserving and auditing external side effects."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from hermes_cli.side_effects.config import get_policy
from hermes_cli.side_effects.schema import connect, ensure_migrated
from hermes_cli.sqlite_util import retrying_write_txn


_TERMINAL_GC_STATUSES = ("done", "failed", "abandoned")
_ACTIVE_STATUSES = ("pending", "in_flight")


@dataclass(frozen=True)
class ReserveResult:
    already_done: dict[str, Any] | None = None
    already_in_flight: dict[str, Any] | None = None
    reserved_id: int | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _compute_idempotency_key(
    *,
    task_id: str | None,
    action_type: str,
    payload_hash: str,
    allow_duplicate: bool,
    caller_idempotency_key: str | None,
) -> str:
    if caller_idempotency_key:
        return caller_idempotency_key
    if action_type == "telegram.send":
        hour_bucket = _now().strftime("%Y-%m-%dT%HZ")
        base = (
            f"{task_id or 'notask'}|{action_type}|{payload_hash}|{hour_bucket}"
        )
    else:
        base = f"{task_id or 'notask'}|{action_type}|{payload_hash}"
    if allow_duplicate:
        base = f"{base}|dup:{secrets.token_hex(4)}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def _seconds_since(iso_ts: str) -> float:
    then = datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    return (_now() - then).total_seconds()


def _archive_key(idempotency_key: str, row_id: int, attempt: int) -> str:
    """Free the logical key while retaining an immutable attempt row.

    The requested schema makes idempotency_key unique while also requiring a
    new row for each retry. Terminal/stale attempts therefore receive a
    deterministic archival key immediately before the next attempt reuses the
    caller-visible logical key.
    """
    value = f"{idempotency_key}|archived:{row_id}:{attempt}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _run_write(
    db_path: str | Path | None,
    operation: Callable[[sqlite3.Connection], Any],
) -> Any:
    """Run one operation through the CS-01a BUSY-safe transaction helper."""
    ensure_migrated(db_path)
    conn = connect(db_path)
    try:
        with retrying_write_txn(conn):
            return operation(conn)
    finally:
        conn.close()


def reserve(
    *,
    task_id: str | None,
    lane: str,
    action_type: str,
    payload: dict[str, Any],
    allow_duplicate: bool = False,
    idempotency_key: str | None = None,
    db_path: str | Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> ReserveResult:
    """Reserve an action before dispatch, returning any prior live result.

    Supplying ``conn`` lets an authoritative caller include the reservation in
    its existing transaction. The caller owns that transaction and connection.
    """
    policy = get_policy(action_type)
    if allow_duplicate and not policy.allow_legit_duplicates:
        raise ValueError(
            f"action_type {action_type!r} does not allow legitimate duplicates"
        )
    payload_hash = _canonical_payload_hash(payload)
    logical_key = _compute_idempotency_key(
        task_id=task_id,
        action_type=action_type,
        payload_hash=payload_hash,
        allow_duplicate=allow_duplicate,
        caller_idempotency_key=idempotency_key,
    )
    now = _now_iso()

    def _txn(conn: sqlite3.Connection) -> tuple[str, dict[str, Any] | int]:
        existing = conn.execute(
            """
            SELECT *
              FROM side_effects
             WHERE task_id IS ?
               AND action_type = ?
               AND idempotency_key = ?
            """,
            (task_id, action_type, logical_key),
        ).fetchone()
        next_attempt = 1
        if existing is not None:
            row = dict(existing)
            status = str(row["status"])
            if status == "done":
                return "done", row
            if status in _ACTIVE_STATUSES:
                if _seconds_since(str(row["updated_at"])) <= policy.stale_seconds:
                    return "in_flight", row
                status = "stale"
                conn.execute(
                    """
                    UPDATE side_effects
                       SET status = 'stale',
                           updated_at = ?
                     WHERE id = ?
                    """,
                    (now, row["id"]),
                )
            next_attempt = int(row["attempt_number"]) + 1
            conn.execute(
                """
                UPDATE side_effects
                   SET idempotency_key = ?
                 WHERE id = ?
                """,
                (
                    _archive_key(
                        logical_key,
                        int(row["id"]),
                        int(row["attempt_number"]),
                    ),
                    row["id"],
                ),
            )

        cursor = conn.execute(
            """
            INSERT INTO side_effects (
                ts, updated_at, task_id, lane, action_type, payload_hash,
                idempotency_key, status, attempt_number, allow_duplicate,
                vendor
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                now,
                now,
                task_id,
                lane,
                action_type,
                payload_hash,
                logical_key,
                next_attempt,
                int(allow_duplicate),
                policy.vendor,
            ),
        )
        return "reserved", int(cursor.lastrowid)

    outcome, value = (
        _txn(conn) if conn is not None else _run_write(db_path, _txn)
    )
    if outcome == "done":
        return ReserveResult(already_done=value)  # type: ignore[arg-type]
    if outcome == "in_flight":
        return ReserveResult(already_in_flight=value)  # type: ignore[arg-type]
    return ReserveResult(reserved_id=int(value))


def _transition(
    *,
    reserved_id: int,
    from_statuses: tuple[str, ...],
    assignments: dict[str, Any],
    db_path: str | Path | None,
    conn: sqlite3.Connection | None = None,
) -> None:
    assignments = dict(assignments)
    columns = ["status = ?", "updated_at = ?"]
    values: list[Any] = [assignments.pop("status"), _now_iso()]
    for column, value in assignments.items():
        columns.append(f"{column} = ?")
        values.append(value)
    placeholders = ", ".join("?" for _ in from_statuses)
    values.extend([int(reserved_id), *from_statuses])

    def _txn(conn: sqlite3.Connection) -> None:
        cursor = conn.execute(
            f"""
            UPDATE side_effects
               SET {", ".join(columns)}
             WHERE id = ?
               AND status IN ({placeholders})
            """,
            values,
        )
        if cursor.rowcount != 1:
            raise ValueError(
                f"side-effect row {reserved_id} is missing or cannot make "
                f"the requested transition"
            )

    if conn is not None:
        _txn(conn)
    else:
        _run_write(db_path, _txn)


def mark_in_flight(
    *,
    reserved_id: int,
    external_ref: str | None = None,
    db_path: str | Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Mark a reservation immediately before paid vendor dispatch."""
    assignments: dict[str, Any] = {"status": "in_flight"}
    if external_ref is not None:
        assignments["external_ref"] = external_ref
    _transition(
        reserved_id=reserved_id,
        from_statuses=("pending",),
        assignments=assignments,
        db_path=db_path,
        conn=conn,
    )


def confirm(
    *,
    reserved_id: int,
    external_ref: str | None,
    result_summary: str | None = None,
    db_path: str | Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Record vendor-confirmed success."""
    _transition(
        reserved_id=reserved_id,
        from_statuses=_ACTIVE_STATUSES,
        assignments={
            "status": "done",
            "external_ref": external_ref,
            "result_summary": result_summary,
        },
        db_path=db_path,
        conn=conn,
    )


def fail(
    *,
    reserved_id: int,
    error_class: str,
    error_message: str,
    db_path: str | Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Record vendor-confirmed failure without deleting the audit row."""
    _transition(
        reserved_id=reserved_id,
        from_statuses=_ACTIVE_STATUSES,
        assignments={
            "status": "failed",
            "error_class": error_class,
            "error_message": error_message,
        },
        db_path=db_path,
        conn=conn,
    )


def reconcile_external_ref(
    *,
    row: dict[str, Any],
    verify_fn: Callable[[str], str] | None = None,
    db_path: str | Path | None = None,
) -> str:
    """Query an injected vendor verifier before retrying a verifiable action."""
    policy = get_policy(str(row["action_type"]))
    external_ref = row.get("external_ref")
    if not policy.verifiable or verify_fn is None or external_ref is None:
        return "unknown"
    result = verify_fn(str(external_ref))
    if result == "done":
        confirm(
            reserved_id=int(row["id"]),
            external_ref=str(external_ref),
            result_summary="reconciled via external_ref verification",
            db_path=db_path,
        )
    elif result == "failed":
        fail(
            reserved_id=int(row["id"]),
            error_class="reconcile_verified_failed",
            error_message="vendor reported failure on reconcile",
            db_path=db_path,
        )
    elif result != "unknown":
        raise ValueError(f"verify_fn returned unsupported result: {result!r}")
    return result


def _now_iso_days_ago(days: int) -> str:
    if int(days) < 0:
        raise ValueError("older_than_days must be non-negative")
    return (_now() - timedelta(days=int(days))).strftime("%Y-%m-%dT%H:%M:%SZ")


def gc(
    *,
    older_than_days: int,
    dry_run: bool = False,
    db_path: str | Path | None = None,
) -> dict[str, int]:
    """Delete old terminal rows while preserving uncertain/audit states."""
    cutoff = _now_iso_days_ago(older_than_days)

    def _txn(conn: sqlite3.Connection) -> dict[str, int]:
        if dry_run:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                  FROM side_effects
                 WHERE status IN ('done', 'failed', 'abandoned')
                   AND ts < ?
                """,
                (cutoff,),
            ).fetchone()
            return {
                "would_delete": int(row["count"] if row is not None else 0),
                "deleted": 0,
            }
        cursor = conn.execute(
            """
            DELETE FROM side_effects
             WHERE status IN ('done', 'failed', 'abandoned')
               AND ts < ?
            """,
            (cutoff,),
        )
        return {"would_delete": 0, "deleted": int(cursor.rowcount)}

    return _run_write(db_path, _txn)


def list_rows(
    *,
    task_id: str | None = None,
    action_type: str | None = None,
    status: str | None = None,
    lane: str | None = None,
    limit: int = 50,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Read recent rows with optional exact-match filters."""
    ensure_migrated(db_path)
    clauses: list[str] = []
    values: list[Any] = []
    for column, value in (
        ("task_id", task_id),
        ("action_type", action_type),
        ("status", status),
        ("lane", lane),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            values.append(value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    values.append(max(1, int(limit)))
    conn = connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT * FROM side_effects {where} ORDER BY id DESC LIMIT ?",
            values,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_row(
    row_id: int,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    ensure_migrated(db_path)
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM side_effects WHERE id = ?",
            (int(row_id),),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def mark_abandoned(
    *,
    row_id: int,
    reason: str,
    operator: str | None = None,
    db_path: str | Path | None = None,
) -> None:
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise ValueError("reason is required")
    changed_by = operator or os.environ.get("USER", "unknown")
    _transition(
        reserved_id=row_id,
        from_statuses=("pending", "in_flight", "failed", "stale"),
        assignments={
            "status": "abandoned",
            "result_summary": (
                f"abandoned by {changed_by}: {normalized_reason}"
            ),
        },
        db_path=db_path,
    )


def mark_stale_scan(
    *,
    db_path: str | Path | None = None,
) -> int:
    """Mark every active row older than its action-specific stale window."""
    now = _now_iso()

    def _txn(conn: sqlite3.Connection) -> int:
        rows = conn.execute(
            """
            SELECT id, action_type, updated_at
              FROM side_effects
             WHERE status IN ('pending', 'in_flight')
            """
        ).fetchall()
        stale_ids = [
            int(row["id"])
            for row in rows
            if _seconds_since(str(row["updated_at"]))
            > get_policy(str(row["action_type"])).stale_seconds
        ]
        if not stale_ids:
            return 0
        placeholders = ", ".join("?" for _ in stale_ids)
        cursor = conn.execute(
            f"""
            UPDATE side_effects
               SET status = 'stale', updated_at = ?
             WHERE id IN ({placeholders})
               AND status IN ('pending', 'in_flight')
            """,
            [now, *stale_ids],
        )
        return int(cursor.rowcount)

    return _run_write(db_path, _txn)


__all__ = [
    "ReserveResult",
    "confirm",
    "fail",
    "gc",
    "get_row",
    "list_rows",
    "mark_abandoned",
    "mark_in_flight",
    "mark_stale_scan",
    "reconcile_external_ref",
    "reserve",
]
