"""The single owner/CAS write contract for runtime-state rows."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Any, Mapping, Optional

from runtime_state.schema import TABLE_BUSINESS_KEY

SUCCESS = "Success"
STALE_VERSION = "StaleVersion"
OWNER_MISMATCH = "OwnerMismatch"
NOT_FOUND = "NotFound"
INVALID_PROFILE_REFERENCE = "InvalidProfileReference"

OWNED_TABLES = tuple(TABLE_BUSINESS_KEY)


@dataclass(frozen=True)
class CasResult:
    success: bool
    owner: Optional[str]
    owner_version: int
    error: Optional[str] = None
    schema_version: Optional[int] = None


# Backwards-compatible public names for callers that distinguish claim/update
# in type annotations while sharing one result shape.
ClaimResult = CasResult
UpdateResult = CasResult
CreateResult = CasResult


_ALLOWED_UPDATE_COLUMNS = {
    "session_state": {"status", "target_host", "deployment_target", "workspace", "user_id"},
    "task_state": {
        "status",
        "branch",
        "worktree",
        "target_host",
        "deployment_target",
    },
    "approval_state": {"approval_status", "breaker_status"},
    "compression_state": {"compression_status"},
}


def _table_and_key(table: str) -> str:
    if table not in TABLE_BUSINESS_KEY:
        raise ValueError(
            f"unknown runtime-state table {table!r}; expected one of "
            f"{sorted(TABLE_BUSINESS_KEY)}"
        )
    return TABLE_BUSINESS_KEY[table]


def _schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT schema_version FROM schema_version WHERE id = 1"
    ).fetchone()
    if row is None:
        raise RuntimeError("runtime_state schema_version singleton row is missing")
    return int(row[0])


def _current_owner_state(
    conn: sqlite3.Connection,
    table: str,
    profile_name: str,
    business_key_value: str,
) -> tuple[Optional[str], int, Optional[int]]:
    key_col = _table_and_key(table)
    row = conn.execute(
        f"SELECT owner, owner_version, schema_version FROM {table} "
        f"WHERE profile_name = ? AND {key_col} = ?",
        (profile_name, business_key_value),
    ).fetchone()
    if row is None:
        return None, 0, None
    return row[0], int(row[1]), int(row[2])


def _failure_result(
    conn: sqlite3.Connection,
    table: str,
    profile_name: str,
    business_key_value: str,
    expected_version: int,
    expected_owner: Optional[str] = None,
    attempted_owner: Optional[str] = None,
) -> CasResult:
    current_owner, current_version, row_schema = _current_owner_state(
        conn, table, profile_name, business_key_value
    )
    if row_schema is None:
        return CasResult(False, None, 0, NOT_FOUND, None)
    if attempted_owner is not None and current_owner not in (None, attempted_owner):
        error = OWNER_MISMATCH
    elif expected_owner is not None and current_owner != expected_owner:
        error = OWNER_MISMATCH
    else:
        error = STALE_VERSION
    return CasResult(False, current_owner, current_version, error, row_schema)


def _invalid_profile_result(conn: sqlite3.Connection) -> CasResult:
    """Return the typed result for a missing or cross-profile reference."""

    return CasResult(False, None, 0, INVALID_PROFILE_REFERENCE, _schema_version(conn))


def _existing_result(
    conn: sqlite3.Connection,
    table: str,
    profile_name: str,
    business_key_value: str,
) -> Optional[CasResult]:
    owner, owner_version, row_schema = _current_owner_state(
        conn, table, profile_name, business_key_value
    )
    if row_schema is None:
        return None
    return CasResult(True, owner, owner_version, SUCCESS, row_schema)


def _insert_result(
    conn: sqlite3.Connection,
    table: str,
    profile_name: str,
    business_key_value: str,
    statement: str,
    parameters: tuple[Any, ...],
) -> CreateResult:
    """Insert one row through the central runtime-state write boundary."""

    if not profile_name or not business_key_value:
        return _invalid_profile_result(conn)
    existing = _existing_result(conn, table, profile_name, business_key_value)
    if existing is not None:
        return existing
    schema_version = _schema_version(conn)
    try:
        conn.execute(statement, parameters)
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        existing = _existing_result(conn, table, profile_name, business_key_value)
        if existing is not None:
            return existing
        return _invalid_profile_result(conn)
    return CasResult(True, None, 0, SUCCESS, schema_version)


def _reference_exists(
    conn: sqlite3.Connection,
    table: str,
    profile_name: str,
    key_value: str,
) -> bool:
    key_col = _table_and_key(table)
    return conn.execute(
        f"SELECT 1 FROM {table} WHERE profile_name = ? AND {key_col} = ?",
        (profile_name, key_value),
    ).fetchone() is not None


def create_session_state(
    conn: sqlite3.Connection,
    profile_name: str,
    session_id: str,
    *,
    user_id: str,
    workspace: str,
    target_host: Optional[str] = None,
    deployment_target: Optional[str] = None,
    status: str = "active",
    now: Optional[str] = None,
) -> CreateResult:
    """Create or return a session row without bypassing the write boundary."""

    schema_version = _schema_version(conn)
    timestamp = now or _timestamp()
    return _insert_result(
        conn,
        "session_state",
        profile_name,
        session_id,
        "INSERT INTO session_state "
        "(profile_name, session_id, user_id, workspace, target_host, "
        "deployment_target, status, schema_version, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            profile_name,
            session_id,
            user_id or "unknown",
            workspace or "unknown",
            target_host,
            deployment_target,
            status,
            schema_version,
            timestamp,
            timestamp,
        ),
    )


def create_task_state(
    conn: sqlite3.Connection,
    profile_name: str,
    task_id: str,
    session_id: str,
    *,
    status: str = "pending",
    branch: Optional[str] = None,
    worktree: Optional[str] = None,
    target_host: Optional[str] = None,
    deployment_target: Optional[str] = None,
    now: Optional[str] = None,
) -> CreateResult:
    """Create or return a task row after validating its profile/session."""

    if not profile_name or not task_id or not session_id:
        return _invalid_profile_result(conn)
    if not _reference_exists(conn, "session_state", profile_name, session_id):
        return _invalid_profile_result(conn)
    schema_version = _schema_version(conn)
    timestamp = now or _timestamp()
    return _insert_result(
        conn,
        "task_state",
        profile_name,
        task_id,
        "INSERT INTO task_state "
        "(profile_name, task_id, session_id, branch, worktree, target_host, "
        "deployment_target, status, schema_version, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            profile_name,
            task_id,
            session_id,
            branch,
            worktree,
            target_host,
            deployment_target,
            status,
            schema_version,
            timestamp,
            timestamp,
        ),
    )


def create_approval_state(
    conn: sqlite3.Connection,
    profile_name: str,
    approval_id: str,
    *,
    session_id: Optional[str] = None,
    task_id: Optional[str] = None,
    approval_status: str = "pending",
    breaker_status: str = "closed",
    now: Optional[str] = None,
) -> CreateResult:
    """Create an approval row after validating same-profile references."""

    if not profile_name or not approval_id or (not session_id and not task_id):
        return _invalid_profile_result(conn)
    if session_id and not _reference_exists(conn, "session_state", profile_name, session_id):
        return _invalid_profile_result(conn)
    if task_id and not _reference_exists(conn, "task_state", profile_name, task_id):
        return _invalid_profile_result(conn)
    if session_id and task_id:
        row = conn.execute(
            "SELECT 1 FROM task_state WHERE profile_name = ? AND task_id = ? "
            "AND session_id = ?",
            (profile_name, task_id, session_id),
        ).fetchone()
        if row is None:
            return _invalid_profile_result(conn)
    schema_version = _schema_version(conn)
    timestamp = now or _timestamp()
    return _insert_result(
        conn,
        "approval_state",
        profile_name,
        approval_id,
        "INSERT INTO approval_state "
        "(profile_name, approval_id, session_id, task_id, approval_status, "
        "breaker_status, schema_version, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            profile_name,
            approval_id,
            session_id,
            task_id,
            approval_status,
            breaker_status,
            schema_version,
            timestamp,
            timestamp,
        ),
    )


def create_compression_state(
    conn: sqlite3.Connection,
    profile_name: str,
    session_id: str,
    *,
    task_id: Optional[str] = None,
    compression_status: str = "idle",
    now: Optional[str] = None,
) -> CreateResult:
    """Create the session-scoped compression row with profile validation."""

    if not profile_name or not session_id:
        return _invalid_profile_result(conn)
    if not _reference_exists(conn, "session_state", profile_name, session_id):
        return _invalid_profile_result(conn)
    if task_id:
        row = conn.execute(
            "SELECT 1 FROM task_state WHERE profile_name = ? AND task_id = ? "
            "AND session_id = ?",
            (profile_name, task_id, session_id),
        ).fetchone()
        if row is None:
            return _invalid_profile_result(conn)
    schema_version = _schema_version(conn)
    timestamp = now or _timestamp()
    return _insert_result(
        conn,
        "compression_state",
        profile_name,
        session_id,
        "INSERT INTO compression_state "
        "(profile_name, session_id, task_id, compression_status, schema_version, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            profile_name,
            session_id,
            task_id,
            compression_status,
            schema_version,
            timestamp,
            timestamp,
        ),
    )


def cas_claim_owner(
    conn: sqlite3.Connection,
    table: str,
    profile_name: str,
    business_key_value: str,
    new_owner: str,
    expected_version: int,
    *,
    now: Optional[str] = None,
) -> ClaimResult:
    """Claim an unowned row or renew a claim by the same owner.

    A row owned by another owner fails closed. The WHERE clause includes the
    composite profile/business key, expected owner version, and NULL-safe
    ownership condition so exactly one racing writer can advance the token.
    """

    key_col = _table_and_key(table)
    current_schema = _schema_version(conn)
    timestamp = now or _timestamp()
    cursor = conn.execute(
        f"UPDATE {table} SET owner = ?, owner_version = owner_version + 1, "
        f"schema_version = ?, updated_at = ? WHERE profile_name = ? "
        f"AND {key_col} = ? AND owner_version = ? "
        "AND (owner IS NULL OR owner IS ?)",
        (
            new_owner,
            current_schema,
            timestamp,
            profile_name,
            business_key_value,
            expected_version,
            new_owner,
        ),
    )
    if cursor.rowcount == 1:
        conn.commit()
        return CasResult(True, new_owner, expected_version + 1, SUCCESS, current_schema)
    conn.rollback()
    return _failure_result(
        conn,
        table,
        profile_name,
        business_key_value,
        expected_version,
        attempted_owner=new_owner,
    )


def cas_update_columns(
    conn: sqlite3.Connection,
    table: str,
    profile_name: str,
    business_key_value: str,
    owner: str,
    expected_version: int,
    columns: Mapping[str, Any],
    *,
    now: Optional[str] = None,
) -> UpdateResult:
    """Update allow-listed state columns through owner/version CAS."""

    if not columns:
        raise ValueError("columns must be non-empty")
    key_col = _table_and_key(table)
    allowed = _ALLOWED_UPDATE_COLUMNS[table]
    unknown = set(columns) - allowed
    if unknown:
        raise ValueError(
            f"columns {sorted(unknown)} are not writable on {table!r}; "
            f"allowed: {sorted(allowed)}"
        )
    current_schema = _schema_version(conn)
    timestamp = now or _timestamp()
    ordered_columns = sorted(columns)
    assignments = ", ".join(f"{column} = ?" for column in ordered_columns)
    params = [columns[column] for column in ordered_columns]
    params.extend(
        [current_schema, timestamp, profile_name, business_key_value, owner, expected_version]
    )
    cursor = conn.execute(
        f"UPDATE {table} SET {assignments}, schema_version = ?, updated_at = ?, "
        f"owner_version = owner_version + 1 WHERE profile_name = ? "
        f"AND {key_col} = ? AND owner IS ? AND owner_version = ?",
        params,
    )
    if cursor.rowcount == 1:
        conn.commit()
        return CasResult(True, owner, expected_version + 1, SUCCESS, current_schema)
    conn.rollback()
    return _failure_result(
        conn,
        table,
        profile_name,
        business_key_value,
        expected_version,
        expected_owner=owner,
    )


def cas_release_owner(
    conn: sqlite3.Connection,
    table: str,
    profile_name: str,
    business_key_value: str,
    owner: str,
    expected_version: int,
    *,
    now: Optional[str] = None,
) -> UpdateResult:
    """Release ownership while advancing the CAS token and row version."""

    key_col = _table_and_key(table)
    current_schema = _schema_version(conn)
    timestamp = now or _timestamp()
    cursor = conn.execute(
        f"UPDATE {table} SET owner = NULL, owner_version = owner_version + 1, "
        f"schema_version = ?, updated_at = ? WHERE profile_name = ? "
        f"AND {key_col} = ? AND owner IS ? AND owner_version = ?",
        (current_schema, timestamp, profile_name, business_key_value, owner, expected_version),
    )
    if cursor.rowcount == 1:
        conn.commit()
        return CasResult(True, None, expected_version + 1, SUCCESS, current_schema)
    conn.rollback()
    return _failure_result(
        conn,
        table,
        profile_name,
        business_key_value,
        expected_version,
        expected_owner=owner,
    )


def _timestamp() -> str:
    from runtime_state.migrations import utc_timestamp

    return utc_timestamp()
