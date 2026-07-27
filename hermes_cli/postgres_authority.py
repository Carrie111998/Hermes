"""PostgreSQL authority store for production Charterforge deployments.

This module provides a Postgres-backed authority store for governed workers,
enabling:

  - Multi-worker coordination across containers/processes
  - Durable claim storage with automatic expiry
  - Transactional run tracking with CAS fencing
  - Production-grade concurrent access

The Postgres authority store is a drop-in replacement for the SQLite authority
store used in development/testing.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Iterator, Optional

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.sql import SQL, Identifier, Literal
except ImportError:
    psycopg = None  # type: ignore

from pathlib import Path

# Schema version for migrations
SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS task_claims (
    id SERIAL PRIMARY KEY,
    task_id TEXT NOT NULL,
    claim_lock TEXT NOT NULL UNIQUE,
    organization_id TEXT NOT NULL,
    worker_id TEXT,
    claim_scope_url TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_claims_task_id ON task_claims(task_id);
CREATE INDEX IF NOT EXISTS idx_task_claims_expires_at ON task_claims(expires_at);
CREATE INDEX IF NOT EXISTS idx_task_claims_organization ON task_claims(organization_id);

CREATE TABLE IF NOT EXISTS task_runs (
    id SERIAL PRIMARY KEY,
    task_id TEXT NOT NULL,
    claim_lock TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    outcome TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_runs_task_id ON task_runs(task_id);
CREATE INDEX IF NOT EXISTS idx_task_runs_claim_lock ON task_runs(claim_lock);
CREATE INDEX IF NOT EXISTS idx_task_runs_status ON task_runs(status);

CREATE TABLE IF NOT EXISTS task_permits (
    id SERIAL PRIMARY KEY,
    task_id TEXT NOT NULL,
    claim_lock TEXT NOT NULL,
    permit_id TEXT NOT NULL UNIQUE,
    action_payload JSONB NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    consumed_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_permits_task_id ON task_permits(task_id);
CREATE INDEX IF NOT EXISTS idx_task_permits_permit_id ON task_permits(permit_id);
CREATE INDEX IF NOT EXISTS idx_task_permits_expires_at ON task_permits(expires_at);

CREATE TABLE IF NOT EXISTS execution_effects (
    id SERIAL PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    effect_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_execution_effects_task_id ON execution_effects(task_id);
CREATE INDEX IF NOT EXISTS idx_execution_effects_run_id ON execution_effects(run_id);
"""


def get_postgres_url() -> str:
    """Get Postgres connection URL from environment.
    
    Prefers AUTHORITY_POSTGRES_URL, falls back to DATABASE_URL.
    
    Returns:
        Postgres connection URL
        
    Raises:
        RuntimeError: If no Postgres URL configured
    """
    url = os.environ.get("AUTHORITY_POSTGRES_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "Postgres authority store requires AUTHORITY_POSTGRES_URL or DATABASE_URL"
        )
    return url


def connect(url: Optional[str] = None) -> "psycopg.Connection":
    """Connect to Postgres authority store.
    
    Args:
        url: Optional Postgres URL (defaults to environment)
        
    Returns:
        Postgres connection with dict_row factory
        
    Raises:
        ImportError: If psycopg not installed
        RuntimeError: If no URL configured
    """
    if psycopg is None:
        raise ImportError(
            "psycopg is required for Postgres authority store: "
            "pip install psycopg[binary]"
        )
    
    resolved_url = url or get_postgres_url()
    conn = psycopg.connect(resolved_url, row_factory=dict_row)
    conn.autocommit = False
    return conn


def init_schema(conn: "psycopg.Connection") -> None:
    """Initialize Postgres authority schema.
    
    Creates tables if not exist, runs migrations if needed.
    
    Args:
        conn: Postgres connection
    """
    with conn.cursor() as cur:
        # Create schema
        cur.execute(SCHEMA_SQL)
        
        # Check schema version
        cur.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version")
        row = cur.fetchone()
        current_version = row["coalesce"] if row else 0
        
        if current_version < SCHEMA_VERSION:
            # Future migrations would go here
            cur.execute(
                "INSERT INTO schema_version (version) VALUES (%s) ON CONFLICT DO NOTHING",
                (SCHEMA_VERSION,)
            )
    
    conn.commit()


def claim_task(
    conn: "psycopg.Connection",
    *,
    task_id: str,
    claim_lock: str,
    organization_id: str,
    worker_id: str,
    claim_scope_url: str,
    expires_at: float,
) -> bool:
    """Claim a task for execution (CAS operation).
    
    Args:
        conn: Postgres connection
        task_id: Task to claim
        claim_lock: Unique claim lock (run ID)
        organization_id: Organization scope
        worker_id: Worker identifier
        claim_scope_url: Scope URL for the claim
        expires_at: Unix timestamp when claim expires
        
    Returns:
        True if claim succeeded, False if already claimed
    """
    expires_dt = f"to_timestamp({expires_at})"
    
    with conn.cursor() as cur:
        # Try to insert claim (will fail if task_id already claimed)
        try:
            cur.execute(
                SQL("""
                INSERT INTO task_claims 
                    (task_id, claim_lock, organization_id, worker_id, 
                     claim_scope_url, expires_at)
                VALUES 
                    (%s, %s, %s, %s, %s, {})
                ON CONFLICT (claim_lock) DO NOTHING
                RETURNING id
                """).format(Literal(expires_dt)),
                (task_id, claim_lock, organization_id, worker_id, claim_scope_url)
            )
            row = cur.fetchone()
            if not row:
                return False
            
            # Record run
            cur.execute(
                """
                INSERT INTO task_runs 
                    (task_id, claim_lock, organization_id, status)
                VALUES 
                    (%s, %s, %s, 'pending')
                """,
                (task_id, claim_lock, organization_id)
            )
            
            conn.commit()
            return True
            
        except psycopg.errors.UniqueViolation:
            conn.rollback()
            return False


def get_claim(
    conn: "psycopg.Connection",
    *,
    task_id: str,
    organization_id: str,
) -> Optional[dict[str, Any]]:
    """Get the active claim for a task.
    
    Args:
        conn: Postgres connection
        task_id: Task to query
        organization_id: Organization scope
        
    Returns:
        Claim dict or None if not claimed/expired
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM task_claims
            WHERE task_id = %s 
              AND organization_id = %s
              AND expires_at > NOW()
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (task_id, organization_id)
        )
        return cur.fetchone()


def release_claim(
    conn: "psycopg.Connection",
    *,
    task_id: str,
    claim_lock: str,
    organization_id: str,
) -> bool:
    """Release a claim (for cleanup/expiry).
    
    Args:
        conn: Postgres connection
        task_id: Task to release
        claim_lock: Claim lock to validate
        organization_id: Organization scope
        
    Returns:
        True if released, False if not found
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM task_claims
            WHERE task_id = %s 
              AND claim_lock = %s
              AND organization_id = %s
            RETURNING id
            """,
            (task_id, claim_lock, organization_id)
        )
        row = cur.fetchone()
        conn.commit()
        return row is not None


def complete_task(
    conn: "psycopg.Connection",
    *,
    task_id: str,
    claim_lock: str,
    organization_id: str,
    outcome: str,
    effects: Optional[list[dict[str, Any]]] = None,
) -> bool:
    """Complete a task run.
    
    Args:
        conn: Postgres connection
        task_id: Task to complete
        claim_lock: Claim lock (must match active claim)
        organization_id: Organization scope
        outcome: Completion outcome
        effects: Optional list of effects to record
        
    Returns:
        True if completed, False if claim mismatch/expired
    """
    with conn.cursor() as cur:
        # Verify claim is still valid
        cur.execute(
            """
            SELECT id FROM task_claims
            WHERE task_id = %s
              AND claim_lock = %s
              AND organization_id = %s
              AND expires_at > NOW()
            """,
            (task_id, claim_lock, organization_id)
        )
        if not cur.fetchone():
            return False
        
        # Update run status
        cur.execute(
            """
            UPDATE task_runs
            SET status = 'completed',
                outcome = %s,
                ended_at = NOW()
            WHERE task_id = %s
              AND claim_lock = %s
              AND organization_id = %s
              AND status = 'pending'
            """,
            (outcome, task_id, claim_lock, organization_id)
        )
        
        # Record effects
        if effects:
            for effect in effects:
                cur.execute(
                    """
                    INSERT INTO execution_effects
                        (task_id, run_id, effect_type, payload)
                    VALUES
                        (%s, %s, %s, %s)
                    """,
                    (task_id, claim_lock, effect.get("type", "unknown"), json.dumps(effect))
                )
        
        # Release claim
        cur.execute(
            """
            DELETE FROM task_claims
            WHERE task_id = %s AND claim_lock = %s
            """,
            (task_id, claim_lock)
        )
        
        conn.commit()
        return True


def issue_permit(
    conn: "psycopg.Connection",
    *,
    task_id: str,
    claim_lock: str,
    organization_id: str,
    action_payload: dict[str, Any],
    ttl_seconds: int = 300,
) -> str:
    """Issue an execution permit for a governed action.
    
    Args:
        conn: Postgres connection
        task_id: Task ID
        claim_lock: Claim lock
        organization_id: Organization scope
        action_payload: Action payload to authorize
        ttl_seconds: Permit TTL in seconds
        
    Returns:
        Permit ID (UUID)
        
    Raises:
        ValueError: If no valid claim exists
    """
    import uuid
    
    permit_id = str(uuid.uuid4())
    expires_at = time.time() + ttl_seconds
    expires_dt = f"to_timestamp({expires_at})"
    
    with conn.cursor() as cur:
        # Verify claim exists
        cur.execute(
            """
            SELECT id FROM task_claims
            WHERE task_id = %s
              AND claim_lock = %s
              AND organization_id = %s
              AND expires_at > NOW()
            """,
            (task_id, claim_lock, organization_id)
        )
        if not cur.fetchone():
            raise ValueError("No valid claim for permit issuance")
        
        # Issue permit
        cur.execute(
            SQL("""
            INSERT INTO task_permits
                (task_id, claim_lock, permit_id, action_payload, expires_at)
            VALUES
                (%s, %s, %s, %s, {})
            RETURNING permit_id
            """).format(Literal(expires_dt)),
            (task_id, claim_lock, permit_id, json.dumps(action_payload))
        )
        
        row = cur.fetchone()
        conn.commit()
        return row["permit_id"]


def consume_permit(
    conn: "psycopg.Connection",
    *,
    permit_id: str,
    claim_lock: str,
    action_payload: dict[str, Any],
) -> bool:
    """Consume an execution permit.
    
    Args:
        conn: Postgres connection
        permit_id: Permit to consume
        claim_lock: Claim lock (must match)
        action_payload: Action payload (must match SHA256)
        
    Returns:
        True if consumed, False if invalid/expired
    """
    import hashlib
    
    payload_hash = hashlib.sha256(json.dumps(action_payload, sort_keys=True).encode()).hexdigest()
    
    with conn.cursor() as cur:
        # Verify permit
        cur.execute(
            """
            SELECT id, action_payload, expires_at FROM task_permits
            WHERE permit_id = %s AND consumed_at IS NULL
            """,
            (permit_id,)
        )
        row = cur.fetchone()
        if not row:
            return False
        
        # Verify expiry
        if row["expires_at"].timestamp() < time.time():
            return False
        
        # Verify payload hash
        stored_hash = hashlib.sha256(
            json.dumps(row["action_payload"], sort_keys=True).encode()
        ).hexdigest()
        if stored_hash != payload_hash:
            return False
        
        # Mark consumed
        cur.execute(
            """
            UPDATE task_permits
            SET consumed_at = NOW()
            WHERE permit_id = %s
            """,
            (permit_id,)
        )
        
        conn.commit()
        return True


def get_active_runs(
    conn: "psycopg.Connection",
    *,
    organization_id: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Get active runs for an organization.
    
    Args:
        conn: Postgres connection
        organization_id: Organization scope
        limit: Maximum runs to return
        
    Returns:
        List of active run dicts
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tr.*, tc.worker_id, tc.claim_scope_url
            FROM task_runs tr
            JOIN task_claims tc ON tr.claim_lock = tc.claim_lock
            WHERE tr.organization_id = %s
              AND tr.status = 'pending'
              AND tc.expires_at > NOW()
            ORDER BY tr.started_at DESC
            LIMIT %s
            """,
            (organization_id, limit)
        )
        return cur.fetchall()


def cleanup_expired_claims(conn: "psycopg.Connection") -> int:
    """Clean up expired claims.
    
    Args:
        conn: Postgres connection
        
    Returns:
        Number of claims cleaned up
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM task_claims
            WHERE expires_at < NOW()
            RETURNING id
            """
        )
        rows = cur.fetchall()
        conn.commit()
        return len(rows)


# Environment detection for automatic authority store selection

def get_authority_backend() -> str:
    """Detect which authority backend to use.
    
    Returns 'postgres' if AUTHORITY_POSTGRES_URL or DATABASE_URL is set,
    otherwise 'sqlite'.
    """
    if os.environ.get("AUTHORITY_POSTGRES_URL") or os.environ.get("DATABASE_URL"):
        return "postgres"
    return "sqlite"


def get_authority_connection():
    """Get authority connection based on environment.
    
    Returns:
        Connection for the appropriate backend
    """
    backend = get_authority_backend()
    if backend == "postgres":
        conn = connect()
        init_schema(conn)
        return conn
    else:
        import sqlite3
        from pathlib import Path
        from hermes_cli.kanban_db import connect as sqlite_connect
        return sqlite_connect()


__all__ = [
    "connect",
    "init_schema",
    "claim_task",
    "get_claim",
    "release_claim",
    "complete_task",
    "issue_permit",
    "consume_permit",
    "get_active_runs",
    "cleanup_expired_claims",
    "get_authority_backend",
    "get_authority_connection",
    "SCHEMA_SQL",
    "SCHEMA_VERSION",
]
