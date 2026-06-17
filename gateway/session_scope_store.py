"""Sidecar session ownership store for API-server multi-user ACL."""

from __future__ import annotations

import time
import uuid
from typing import Any, Iterable, Mapping

from gateway.session_acl import (
    has_principal_scope,
    scope_fields,
    scope_matches_record,
)


_CREATE_SCOPE_TABLE = """
CREATE TABLE IF NOT EXISTS api_session_scopes (
    session_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)
"""

_CREATE_SCOPE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_api_session_scopes_scope
ON api_session_scopes (
    tenant_id,
    workspace_id,
    project_id,
    user_id,
    updated_at DESC
)
"""

_CREATE_SANDBOX_LEASE_TABLE = """
CREATE TABLE IF NOT EXISTS api_sandbox_leases (
    session_id TEXT PRIMARY KEY,
    sandbox_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)
"""

_CREATE_SANDBOX_LEASE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_api_sandbox_leases_scope
ON api_sandbox_leases (
    tenant_id,
    workspace_id,
    project_id,
    user_id,
    updated_at DESC
)
"""


def ensure_scope_schema(db: Any) -> None:
    """Create the API session scope sidecar table on the SessionDB connection."""
    if db is None or getattr(db, "read_only", False):
        return
    with db._lock:
        db._conn.execute(_CREATE_SCOPE_TABLE)
        db._conn.execute(_CREATE_SCOPE_INDEX)
        db._conn.commit()


def ensure_sandbox_lease_schema(db: Any) -> None:
    """Create the API sandbox lease sidecar table on the SessionDB connection."""
    if db is None or getattr(db, "read_only", False):
        return
    with db._lock:
        db._conn.execute(_CREATE_SANDBOX_LEASE_TABLE)
        db._conn.execute(_CREATE_SANDBOX_LEASE_INDEX)
        db._conn.commit()


def bind_session_scope(
    db: Any,
    session_id: str | None,
    scope: Mapping[str, Any] | None,
) -> None:
    """Bind a session id to the request principal scope."""
    if db is None or not session_id or not has_principal_scope(scope):
        return
    ensure_scope_schema(db)
    fields = scope_fields(scope)
    now = time.time()
    with db._lock:
        db._conn.execute(
            """
            INSERT INTO api_session_scopes (
                session_id, tenant_id, workspace_id, project_id, user_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                tenant_id = excluded.tenant_id,
                workspace_id = excluded.workspace_id,
                project_id = excluded.project_id,
                user_id = excluded.user_id,
                updated_at = excluded.updated_at
            """,
            (
                session_id,
                fields["tenant_id"],
                fields["workspace_id"],
                fields["project_id"],
                fields["user_id"],
                now,
                now,
            ),
        )
        db._conn.commit()


def get_session_scope(db: Any, session_id: str | None) -> dict[str, str] | None:
    """Return persisted owner scope for a session id, if present."""
    if db is None or not session_id:
        return None
    ensure_scope_schema(db)
    with db._lock:
        row = db._conn.execute(
            """
            SELECT tenant_id, workspace_id, project_id, user_id
            FROM api_session_scopes
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def can_access_session(
    db: Any,
    session_id: str | None,
    scope: Mapping[str, Any] | None,
) -> bool:
    """Return whether the current request scope can access a session."""
    if not has_principal_scope(scope):
        return True
    return scope_matches_record(scope, get_session_scope(db, session_id))


def filter_sessions_for_scope(
    db: Any,
    sessions: Iterable[Mapping[str, Any]],
    scope: Mapping[str, Any] | None,
) -> list[Mapping[str, Any]]:
    """Filter SessionDB rows to sessions owned by the request scope."""
    if not has_principal_scope(scope):
        return list(sessions)
    return [
        session
        for session in sessions
        if can_access_session(db, str(session.get("id") or ""), scope)
    ]


def inherit_or_bind_session_scope(
    db: Any,
    session_id: str | None,
    *,
    scope: Mapping[str, Any] | None = None,
    parent_session_id: str | None = None,
) -> None:
    """Bind explicit scope, otherwise inherit from a parent sidecar row."""
    if db is None or not session_id:
        return
    if has_principal_scope(scope):
        bind_session_scope(db, session_id, scope)
        return
    parent_scope = get_session_scope(db, parent_session_id)
    if parent_scope:
        bind_session_scope(db, session_id, parent_scope)


def issue_or_refresh_sandbox_lease(
    db: Any,
    session_id: str | None,
    scope: Mapping[str, Any] | None,
    *,
    ttl_seconds: int = 8 * 60 * 60,
) -> dict[str, Any] | None:
    """Create or refresh the sandbox lease for an API session.

    This P0 sidecar records the lease envelope that gets bound to the agent
    turn. A future sandbox service can replace this table without changing the
    agent-facing ``SandboxLease`` contract.
    """
    if db is None or not session_id or not has_principal_scope(scope):
        return None
    ensure_sandbox_lease_schema(db)
    fields = scope_fields(scope)
    now = time.time()
    explicit_sandbox_id = str(scope.get("sandbox_id") or "").strip()
    status = str(scope.get("sandbox_status") or "active").strip() or "active"
    expires_at = _coerce_expires_at(scope.get("sandbox_expires_at"), default=now + ttl_seconds)

    with db._lock:
        existing = db._conn.execute(
            """
            SELECT session_id, sandbox_id, tenant_id, workspace_id, project_id,
                   user_id, status, expires_at, created_at, updated_at
            FROM api_sandbox_leases
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        existing_dict = dict(existing) if existing else None
        if explicit_sandbox_id:
            sandbox_id = explicit_sandbox_id
        elif _lease_matches_scope(existing_dict, fields) and _lease_is_active(existing_dict, now):
            sandbox_id = str(existing_dict.get("sandbox_id") or "")
        else:
            sandbox_id = f"sbx_{uuid.uuid4().hex}"
        created_at = float(existing_dict.get("created_at") or now) if existing_dict else now
        db._conn.execute(
            """
            INSERT INTO api_sandbox_leases (
                session_id, sandbox_id, tenant_id, workspace_id, project_id,
                user_id, status, expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                sandbox_id = excluded.sandbox_id,
                tenant_id = excluded.tenant_id,
                workspace_id = excluded.workspace_id,
                project_id = excluded.project_id,
                user_id = excluded.user_id,
                status = excluded.status,
                expires_at = excluded.expires_at,
                updated_at = excluded.updated_at
            """,
            (
                session_id,
                sandbox_id,
                fields["tenant_id"],
                fields["workspace_id"],
                fields["project_id"],
                fields["user_id"],
                status,
                expires_at,
                created_at,
                now,
            ),
        )
        db._conn.commit()
    return {
        "session_id": session_id,
        "sandbox_id": sandbox_id,
        "tenant_id": fields["tenant_id"],
        "workspace_id": fields["workspace_id"],
        "project_id": fields["project_id"],
        "user_id": fields["user_id"],
        "status": status,
        "expires_at": expires_at,
        "created_at": created_at,
        "updated_at": now,
    }


def get_sandbox_lease(db: Any, session_id: str | None) -> dict[str, Any] | None:
    """Return the persisted sandbox lease for a session id, if present."""
    if db is None or not session_id:
        return None
    ensure_sandbox_lease_schema(db)
    with db._lock:
        row = db._conn.execute(
            """
            SELECT session_id, sandbox_id, tenant_id, workspace_id, project_id,
                   user_id, status, expires_at, created_at, updated_at
            FROM api_sandbox_leases
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def _coerce_expires_at(value: Any, *, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _lease_matches_scope(record: Mapping[str, Any] | None, fields: Mapping[str, str]) -> bool:
    if not record:
        return False
    return all(
        str(record.get(key) or "") == str(fields.get(key) or "")
        for key in ("tenant_id", "workspace_id", "project_id", "user_id")
    )


def _lease_is_active(record: Mapping[str, Any] | None, now: float) -> bool:
    if not record:
        return False
    if str(record.get("status") or "") != "active":
        return False
    try:
        return float(record.get("expires_at") or 0) > now
    except (TypeError, ValueError):
        return False
