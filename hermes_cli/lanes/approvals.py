"""Durable owner-approval queue operations."""

from __future__ import annotations

import json
import secrets
import string
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_cli.lanes import schema
from hermes_cli.lanes.contracts import (
    ApprovalRequest,
    ApprovalStatus,
    LaneDraft,
    LaneTask,
)
from hermes_cli.lanes.errors import ApprovalExpired, ApprovalNotGranted
from hermes_cli.sqlite_util import retrying_write_txn

_ALPHANUMERIC = string.ascii_letters + string.digits


def _now(value: datetime | None = None) -> datetime:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def generate_token() -> str:
    """Return a cryptographically secure 12-character approval token."""
    return "".join(secrets.choice(_ALPHANUMERIC) for _ in range(12))


def enqueue(
    *,
    task: LaneTask,
    draft: LaneDraft,
    channel: str,
    timeout_hours: int,
    db_path: str | Path | None = None,
    now: datetime | None = None,
) -> ApprovalRequest:
    if task.id is None:
        raise ValueError("approval requires a persisted lane task")
    if channel not in {"telegram", "dashboard"}:
        raise ValueError(f"unsupported approval channel: {channel}")
    created = _now(now)
    expires = created + timedelta(hours=int(timeout_hours))
    schema.ensure_migrated(db_path)
    conn = schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            while True:
                token = generate_token()
                exists = conn.execute(
                    "SELECT 1 FROM lane_approval_queue "
                    "WHERE approval_token=?",
                    (token,),
                ).fetchone()
                if exists is None:
                    break
            conn.execute(
                """INSERT INTO lane_approval_queue(
                     lane_id,lane_task_id,approval_token,channel,draft_json,
                     created_at,expires_at,status)
                   VALUES(?,?,?,?,?,?,?,'pending')""",
                (
                    task.lane_id,
                    int(task.id),
                    token,
                    channel,
                    json.dumps(
                        {
                            "content": draft.content,
                            "metadata": draft.metadata,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    _iso(created),
                    _iso(expires),
                ),
            )
            conn.execute(
                "UPDATE lane_task SET status='awaiting_approval' WHERE id=?",
                (int(task.id),),
            )
    finally:
        conn.close()
    return ApprovalRequest(
        token=token,
        lane_task_id=int(task.id),
        status="pending",
        expires_at=_iso(expires),
    )


def expire_sweep(
    *,
    db_path: str | Path | None = None,
    now: datetime | None = None,
) -> int:
    schema.ensure_migrated(db_path)
    conn = schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            cursor = conn.execute(
                """UPDATE lane_approval_queue
                      SET status='expired'
                    WHERE status='pending' AND expires_at <= ?""",
                (_iso(_now(now)),),
            )
            return int(cursor.rowcount)
    finally:
        conn.close()


def check(
    token: str,
    *,
    db_path: str | Path | None = None,
    now: datetime | None = None,
) -> ApprovalStatus:
    expire_sweep(db_path=db_path, now=now)
    conn = schema.connect(db_path)
    try:
        row = conn.execute(
            """SELECT approval_token,status,expires_at,grant_note,reject_reason
                 FROM lane_approval_queue WHERE approval_token=?""",
            (token,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ApprovalNotGranted(f"unknown approval token: {token}")
    return ApprovalStatus(
        token=str(row["approval_token"]),
        status=str(row["status"]),
        expires_at=str(row["expires_at"]),
        grant_note=row["grant_note"],
        reject_reason=row["reject_reason"],
    )


def grant(
    token: str,
    *,
    note: str | None = None,
    db_path: str | Path | None = None,
    now: datetime | None = None,
) -> ApprovalStatus:
    current = check(token, db_path=db_path, now=now)
    if current.status == "expired":
        raise ApprovalExpired(f"approval token expired: {token}")
    if current.status != "pending":
        raise ApprovalNotGranted(
            f"approval token is not pending: {token} ({current.status})"
        )
    conn = schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            cursor = conn.execute(
                """UPDATE lane_approval_queue
                      SET status='granted',grant_ts=?,grant_note=?
                    WHERE approval_token=? AND status='pending'""",
                (_iso(_now(now)), note, token),
            )
            if cursor.rowcount != 1:
                raise ApprovalNotGranted(
                    f"approval token could not be granted: {token}"
                )
    finally:
        conn.close()
    return check(token, db_path=db_path, now=now)


def reject(
    token: str,
    *,
    reason: str,
    db_path: str | Path | None = None,
    now: datetime | None = None,
) -> ApprovalStatus:
    if not str(reason).strip():
        raise ValueError("reject reason is required")
    current = check(token, db_path=db_path, now=now)
    if current.status == "expired":
        raise ApprovalExpired(f"approval token expired: {token}")
    if current.status != "pending":
        raise ApprovalNotGranted(
            f"approval token is not pending: {token} ({current.status})"
        )
    conn = schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            cursor = conn.execute(
                """UPDATE lane_approval_queue
                      SET status='rejected',reject_reason=?
                    WHERE approval_token=? AND status='pending'""",
                (str(reason).strip(), token),
            )
            if cursor.rowcount != 1:
                raise ApprovalNotGranted(
                    f"approval token could not be rejected: {token}"
                )
    finally:
        conn.close()
    return check(token, db_path=db_path, now=now)


def list_pending(
    *,
    lane_id: str | None = None,
    db_path: str | Path | None = None,
    now: datetime | None = None,
) -> list[dict]:
    expire_sweep(db_path=db_path, now=now)
    conn = schema.connect(db_path)
    try:
        params: tuple[object, ...] = ()
        lane_clause = ""
        if lane_id:
            lane_clause = " AND lane_id=?"
            params = (str(lane_id),)
        rows = conn.execute(
            f"""SELECT id,lane_id,lane_task_id,approval_token,channel,
                       created_at,expires_at,status
                  FROM lane_approval_queue
                 WHERE status='pending'{lane_clause}
                 ORDER BY id""",
            params,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


__all__ = [
    "check",
    "enqueue",
    "expire_sweep",
    "generate_token",
    "grant",
    "list_pending",
    "reject",
]
