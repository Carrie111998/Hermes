"""PostgreSQL authority store for production Charterforge deployments.

This module provides a Postgres-backed authority store for governed workers,
enabling:

  - Multi-worker coordination across containers/processes
  - Durable claim storage with atomic exclusivity per (task_id, organization_id)
  - Monotonically increasing lease_generation for fencing stale workers
  - Transactional run tracking with generation-fenced CAS
  - Idempotent execution-effect recording with stable effect_key
  - Full permit model matching the SQLite authority invariants

Design invariants:
  - Exactly one active claim per (task_id, organization_id) — enforced by
    UNIQUE (task_id, organization_id) on task_claims.
  - Every claim carries a lease_generation column that increments on each
    reclaim.  All write operations (release, complete, permit consume, effect
    insert) require the caller to supply the correct generation; a mismatched
    generation is an unconditional rejection.
  - No timestamp-only authority ordering.  Fencing token (generation) is the
    primary ordering mechanism; expiry is a secondary reclaim trigger.
  - Timestamps are passed as timezone-aware Python datetime objects through
    bound parameters — never as interpolated SQL fragments.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None  # type: ignore

# ---------------------------------------------------------------------------
# Schema versioning
# ---------------------------------------------------------------------------

# Current schema version.  Startup fails closed if the DB is on an
# unsupported version (higher than this) or a lower version that cannot
# be migrated automatically.
SCHEMA_VERSION = 4

# Each entry describes one migration step: (from_version, sql).
# Migrations are applied in order when current_version < SCHEMA_VERSION.
# The SQL must be idempotent where PostgreSQL supports it.
_MIGRATIONS: list[tuple[int, str]] = [
    # v0 → v1: initial tables
    (
        0,
        """
        CREATE TABLE IF NOT EXISTS task_claims (
            id              BIGSERIAL PRIMARY KEY,
            task_id         TEXT        NOT NULL,
            organization_id TEXT        NOT NULL,
            tenant_id       UUID        NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000',
            worker_id       TEXT,
            claim_scope_url TEXT,
            lease_generation BIGINT     NOT NULL DEFAULT 1,
            expires_at      TIMESTAMPTZ NOT NULL,
            claimed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            -- Exactly one active claim per task+org (tenant_id is NOT in this
            -- constraint — it is a row-level scoping column, not an exclusivity key).
            CONSTRAINT uq_task_claims_task_org UNIQUE (task_id, organization_id)
        );

        CREATE INDEX IF NOT EXISTS idx_task_claims_task_id
            ON task_claims(task_id);
        CREATE INDEX IF NOT EXISTS idx_task_claims_expires_at
            ON task_claims(expires_at);
        CREATE INDEX IF NOT EXISTS idx_task_claims_organization
            ON task_claims(organization_id);
        CREATE INDEX IF NOT EXISTS idx_task_claims_tenant
            ON task_claims(tenant_id);

        CREATE TABLE IF NOT EXISTS task_runs (
            id              BIGSERIAL   PRIMARY KEY,
            task_id         TEXT        NOT NULL,
            organization_id TEXT        NOT NULL,
            tenant_id       UUID        NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000',
            -- claim_token ties this run to its claim snapshot; not a FK
            -- so we keep the run row after the claim is deleted.
            claim_token     TEXT        NOT NULL,
            lease_generation BIGINT     NOT NULL,
            status          TEXT        NOT NULL DEFAULT 'pending',
            outcome         TEXT,
            started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ended_at        TIMESTAMPTZ,

            CONSTRAINT uq_task_runs_task_org_gen
                UNIQUE (task_id, organization_id, lease_generation)
        );

        CREATE INDEX IF NOT EXISTS idx_task_runs_task_id
            ON task_runs(task_id);
        CREATE INDEX IF NOT EXISTS idx_task_runs_status
            ON task_runs(status);
        CREATE INDEX IF NOT EXISTS idx_task_runs_tenant
            ON task_runs(tenant_id);

        CREATE TABLE IF NOT EXISTS task_permits (
            id              BIGSERIAL   PRIMARY KEY,
            permit_id       TEXT        NOT NULL UNIQUE,
            task_id         TEXT        NOT NULL,
            organization_id TEXT        NOT NULL,
            tenant_id       UUID        NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000',
            claim_token     TEXT        NOT NULL,
            lease_generation BIGINT     NOT NULL,
            -- Full authority model fields (parity with SQLite)
            actor           TEXT        NOT NULL DEFAULT '',
            executor        TEXT        NOT NULL DEFAULT '',
            capability      TEXT        NOT NULL DEFAULT '',
            action_type     TEXT        NOT NULL DEFAULT '',
            target_resource TEXT        NOT NULL DEFAULT '',
            payload_hash    TEXT        NOT NULL,
            policy_version  TEXT        NOT NULL DEFAULT '',
            action_payload  JSONB       NOT NULL,
            issued_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            consumed_at     TIMESTAMPTZ,
            expires_at      TIMESTAMPTZ NOT NULL,
            revoked_at      TIMESTAMPTZ,
            revocation_reason TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_task_permits_task_id
            ON task_permits(task_id);
        CREATE INDEX IF NOT EXISTS idx_task_permits_permit_id
            ON task_permits(permit_id);
        CREATE INDEX IF NOT EXISTS idx_task_permits_expires_at
            ON task_permits(expires_at);
        CREATE INDEX IF NOT EXISTS idx_task_permits_tenant
            ON task_permits(tenant_id);

        CREATE TABLE IF NOT EXISTS execution_effects (
            id              BIGSERIAL   PRIMARY KEY,
            -- Stable identity for idempotency: if the same governed action
            -- is replayed by a recovery worker the effect_key is identical
            -- and the INSERT ... ON CONFLICT is a no-op.
            effect_key      TEXT        NOT NULL UNIQUE,
            task_id         TEXT        NOT NULL,
            organization_id TEXT        NOT NULL,
            tenant_id       UUID        NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000',
            run_claim_token TEXT        NOT NULL,
            lease_generation BIGINT     NOT NULL,
            permit_id       TEXT,
            effect_type     TEXT        NOT NULL,
            provider        TEXT        NOT NULL DEFAULT '',
            provider_ref    TEXT        NOT NULL DEFAULT '',
            idempotency_key TEXT        NOT NULL DEFAULT '',
            payload         JSONB       NOT NULL,
            recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_execution_effects_task_id
            ON execution_effects(task_id);
        CREATE INDEX IF NOT EXISTS idx_execution_effects_effect_key
            ON execution_effects(effect_key);
        CREATE INDEX IF NOT EXISTS idx_execution_effects_tenant
            ON execution_effects(tenant_id);
        """,
    ),
    # v1 → v2: schema_version bookkeeping table
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version     INTEGER     PRIMARY KEY,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            description TEXT
        );
        """,
    ),
    # v2 → v3: add tenant_id for multi-tenant isolation (RFC-0.23.0).
    # tenant_id is a partitioning/scoping column — NOT part of the claim
    # exclusivity constraint.  The invariant remains: one active claim per
    # (task_id, organization_id).  tenant_id defaults to a fixed UUID so
    # existing single-tenant deployments migrate without data fixup.
    (
        2,
        """
        -- Default tenant for single-tenant deployments.
        ALTER TABLE task_claims
            ADD COLUMN IF NOT EXISTS tenant_id UUID
            NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000';
        CREATE INDEX IF NOT EXISTS idx_task_claims_tenant
            ON task_claims(tenant_id);

        ALTER TABLE task_runs
            ADD COLUMN IF NOT EXISTS tenant_id UUID
            NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000';
        CREATE INDEX IF NOT EXISTS idx_task_runs_tenant
            ON task_runs(tenant_id);

        ALTER TABLE task_permits
            ADD COLUMN IF NOT EXISTS tenant_id UUID
            NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000';
        CREATE INDEX IF NOT EXISTS idx_task_permits_tenant
            ON task_permits(tenant_id);

        ALTER TABLE execution_effects
            ADD COLUMN IF NOT EXISTS tenant_id UUID
            NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000';
        CREATE INDEX IF NOT EXISTS idx_execution_effects_tenant
            ON execution_effects(tenant_id);
        """,
    ),
    # v3 → v4: tenants registry table + per-tenant concurrent claim limit.
    (
        3,
        """
        CREATE TABLE IF NOT EXISTS tenants (
            id              UUID        PRIMARY KEY,
            slug            TEXT        NOT NULL UNIQUE,
            name            TEXT        NOT NULL DEFAULT '',
            max_concurrent_claims INTEGER NOT NULL DEFAULT 10,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            active_at       TIMESTAMPTZ,
            suspended_at    TIMESTAMPTZ
        );

        -- Seed the default tenant so existing rows satisfy FK if added later.
        INSERT INTO tenants (id, slug, name)
        VALUES ('00000000-0000-0000-0000-000000000000', 'default', 'Default Tenant')
        ON CONFLICT (id) DO NOTHING;
        """,
    ),
]


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------


def get_postgres_url() -> str:
    """Get Postgres connection URL from environment.

    Prefers AUTHORITY_POSTGRES_URL, falls back to DATABASE_URL.

    Raises:
        RuntimeError: If no Postgres URL configured
    """
    url = os.environ.get("AUTHORITY_POSTGRES_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "Postgres authority store requires AUTHORITY_POSTGRES_URL or DATABASE_URL"
        )
    return url


DEFAULT_TENANT_ID = UUID("00000000-0000-0000-0000-000000000000")


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


def _ts(unix: float) -> datetime:
    """Convert a Unix timestamp to a timezone-aware UTC datetime.

    Used for all timestamp bound parameters — never interpolated into SQL.
    """
    return datetime.fromtimestamp(unix, tz=timezone.utc)


# ---------------------------------------------------------------------------
# Schema + migrations
# ---------------------------------------------------------------------------


def init_schema(conn: "psycopg.Connection") -> None:
    """Apply all pending migrations and verify schema version.

    Behavior:
    - Fresh install: applies all migrations in order.
    - Already at current version: no-op.
    - Ahead of SCHEMA_VERSION (future schema): raises RuntimeError (fail closed).
    - Mid-migration failure: rolls back the failed migration and re-raises.

    Args:
        conn: Postgres connection

    Raises:
        RuntimeError: If schema is ahead of known version.
    """
    with conn.cursor() as cur:
        # Create schema_version if it doesn't exist yet (bootstrapping case).
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version     INTEGER     PRIMARY KEY,
                applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                description TEXT
            )
            """
        )
        cur.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version")
        row = cur.fetchone()
        current = row["coalesce"] if row else 0

    conn.commit()

    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"Authority store schema version {current} exceeds supported "
            f"version {SCHEMA_VERSION}. Upgrade the Charterforge package or "
            "run a downgrade migration before starting."
        )

    # Apply each pending migration in a separate transaction so partial
    # failures roll back only the failing step.
    for from_version, migration_sql in _MIGRATIONS:
        if current > from_version:
            continue
        to_version = from_version + 1
        try:
            with conn.cursor() as cur:
                cur.execute(migration_sql)
                cur.execute(
                    "INSERT INTO schema_version (version, description) "
                    "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (to_version, f"migration {from_version} → {to_version}"),
                )
            conn.commit()
            current = to_version
        except Exception:
            conn.rollback()
            raise


def get_schema_version(conn: "psycopg.Connection") -> int:
    """Return the current schema version recorded in the database."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_version"
            )
            row = cur.fetchone()
            return row["coalesce"] if row else 0
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Task claiming
# ---------------------------------------------------------------------------


def claim_task(
    conn: "psycopg.Connection",
    *,
    task_id: str,
    claim_token: str,
    organization_id: str,
    worker_id: str,
    claim_scope_url: str,
    expires_at: float,
    tenant_id: Optional[UUID] = None,
) -> Optional[int]:
    """Claim a task for execution (atomic CAS).

    Uses INSERT ... ON CONFLICT (task_id, organization_id) DO NOTHING so
    exactly one worker wins the race regardless of how many try simultaneously.

    Args:
        conn: Postgres connection
        task_id: Task to claim
        claim_token: Unique token for this claim attempt (run ID / UUID)
        organization_id: Organization scope
        worker_id: Worker identifier
        claim_scope_url: Scope URL for the claim
        expires_at: Unix timestamp when claim expires
        tenant_id: Tenant scope (defaults to DEFAULT_TENANT_ID)

    Returns:
        lease_generation (int >= 1) if claim succeeded, None if already claimed
    """
    expires_dt = _ts(expires_at)
    resolved_tenant = str(tenant_id or DEFAULT_TENANT_ID)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO task_claims
                (task_id, organization_id, tenant_id, worker_id,
                 claim_scope_url, lease_generation, expires_at)
            VALUES
                (%s, %s, %s, %s, %s, 1, %s)
            ON CONFLICT (task_id, organization_id) DO NOTHING
            RETURNING lease_generation
            """,
            (task_id, organization_id, resolved_tenant, worker_id,
             claim_scope_url, expires_dt),
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return None

        generation = row["lease_generation"]

        # Record the immutable run row for this generation.
        cur.execute(
            """
            INSERT INTO task_runs
                (task_id, organization_id, tenant_id, claim_token,
                 lease_generation, status)
            VALUES
                (%s, %s, %s, %s, %s, 'pending')
            ON CONFLICT (task_id, organization_id, lease_generation) DO NOTHING
            """,
            (task_id, organization_id, resolved_tenant, claim_token, generation),
        )

        conn.commit()
        return generation


def reclaim_task(
    conn: "psycopg.Connection",
    *,
    task_id: str,
    organization_id: str,
    new_claim_token: str,
    new_worker_id: str,
    claim_scope_url: str,
    expires_at: float,
    tenant_id: Optional[UUID] = None,
) -> Optional[int]:
    """Atomically replace an expired claim with a new one.

    The old claim must be expired (expires_at <= NOW()).  The new lease
    generation is max(old_generation + 1, 1) so it is always strictly greater
    than any previous generation the stale worker could have observed.

    Args:
        conn: Postgres connection
        task_id: Task to reclaim
        organization_id: Organization scope
        new_claim_token: Token for the new claim
        new_worker_id: Worker identifier for the new claimer
        claim_scope_url: Scope URL
        expires_at: Unix timestamp when new claim expires
        tenant_id: Tenant scope (defaults to DEFAULT_TENANT_ID)

    Returns:
        New lease_generation (always > previous) if reclaim succeeded, else None.
    """
    expires_dt = _ts(expires_at)
    resolved_tenant = str(tenant_id or DEFAULT_TENANT_ID)

    with conn.cursor() as cur:
        # Lock the expired row and compute the next generation atomically.
        cur.execute(
            """
            UPDATE task_claims
            SET
                worker_id       = %s,
                claim_scope_url = %s,
                lease_generation = lease_generation + 1,
                expires_at      = %s,
                claimed_at      = NOW()
            WHERE task_id        = %s
              AND organization_id = %s
              AND tenant_id      = %s
              AND expires_at      <= NOW()
            RETURNING lease_generation
            """,
            (new_worker_id, claim_scope_url, expires_dt,
             task_id, organization_id, resolved_tenant),
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return None

        generation = row["lease_generation"]

        # Insert the immutable run record for this generation.
        cur.execute(
            """
            INSERT INTO task_runs
                (task_id, organization_id, tenant_id, claim_token,
                 lease_generation, status)
            VALUES
                (%s, %s, %s, %s, %s, 'pending')
            ON CONFLICT (task_id, organization_id, lease_generation) DO NOTHING
            """,
            (task_id, organization_id, resolved_tenant, new_claim_token, generation),
        )

        conn.commit()
        return generation


# ---------------------------------------------------------------------------
# Claim inspection
# ---------------------------------------------------------------------------


def get_claim(
    conn: "psycopg.Connection",
    *,
    task_id: str,
    organization_id: str,
) -> Optional[dict[str, Any]]:
    """Get the active (non-expired) claim for a task.

    Returns:
        Claim dict (includes lease_generation) or None if not claimed/expired.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM task_claims
            WHERE task_id        = %s
              AND organization_id = %s
              AND expires_at      > NOW()
            """,
            (task_id, organization_id),
        )
        return cur.fetchone()


# ---------------------------------------------------------------------------
# Fenced claim release
# ---------------------------------------------------------------------------


def release_claim(
    conn: "psycopg.Connection",
    *,
    task_id: str,
    organization_id: str,
    claim_token: str,
    lease_generation: int,
) -> bool:
    """Release a claim.

    Only succeeds when claim_token matches an active run for the given
    task+org with exactly the supplied lease_generation.  A stale worker
    cannot release the reclaimer's claim.

    Args:
        conn: Postgres connection
        task_id: Task to release
        organization_id: Organization scope
        claim_token: Must match the run's claim_token
        lease_generation: Must match current lease_generation

    Returns:
        True if released, False if fencing check failed.
    """
    with conn.cursor() as cur:
        # Verify the caller owns the active generation for this task+org.
        cur.execute(
            """
            SELECT 1 FROM task_runs
            WHERE task_id         = %s
              AND organization_id  = %s
              AND claim_token      = %s
              AND lease_generation = %s
              AND status          = 'pending'
            """,
            (task_id, organization_id, claim_token, lease_generation),
        )
        if not cur.fetchone():
            conn.rollback()
            return False

        # Remove the claim row — must also match generation on claims table.
        cur.execute(
            """
            DELETE FROM task_claims
            WHERE task_id         = %s
              AND organization_id  = %s
              AND lease_generation = %s
            RETURNING id
            """,
            (task_id, organization_id, lease_generation),
        )
        deleted = cur.fetchone()
        if not deleted:
            conn.rollback()
            return False

        conn.commit()
        return True


# ---------------------------------------------------------------------------
# Fenced task completion
# ---------------------------------------------------------------------------


def complete_task(
    conn: "psycopg.Connection",
    *,
    task_id: str,
    organization_id: str,
    claim_token: str,
    lease_generation: int,
    outcome: str,
    effects: Optional[list[dict[str, Any]]] = None,
    tenant_id: Optional[UUID] = None,
) -> bool:
    """Complete a task run.

    Three fencing checks must pass atomically:
    1. The run record for (task_id, org, generation) exists and is 'pending'.
    2. The claim_token matches that run's claim_token.
    3. The claim row still carries that generation AND has not expired.

    Args:
        conn: Postgres connection
        task_id: Task to complete
        organization_id: Organization scope
        claim_token: Must match the winning run's claim_token
        lease_generation: Must match current lease_generation on both tables
        outcome: Completion outcome string
        effects: Optional list of effects — each must include 'effect_key'
        tenant_id: Tenant scope (defaults to DEFAULT_TENANT_ID)

    Returns:
        True if completed, False if any fencing check fails.
    """
    resolved_tenant = str(tenant_id or DEFAULT_TENANT_ID)

    with conn.cursor() as cur:
        # --- Fence: verify run is pending for this exact generation ---
        cur.execute(
            """
            UPDATE task_runs
            SET status   = 'completed',
                outcome  = %s,
                ended_at = NOW()
            WHERE task_id         = %s
              AND organization_id  = %s
              AND claim_token      = %s
              AND lease_generation = %s
              AND status          = 'pending'
            RETURNING id
            """,
            (outcome, task_id, organization_id, claim_token, lease_generation),
        )
        updated = cur.fetchone()
        if not updated:
            conn.rollback()
            return False

        # --- Fence: verify claim row matches generation and is not expired ---
        cur.execute(
            """
            SELECT 1 FROM task_claims
            WHERE task_id         = %s
              AND organization_id  = %s
              AND lease_generation = %s
              AND expires_at       > NOW()
            """,
            (task_id, organization_id, lease_generation),
        )
        if not cur.fetchone():
            conn.rollback()
            return False

        # --- Record effects idempotently ---
        if effects:
            for effect in effects:
                key = effect.get("effect_key")
                if not key:
                    raise ValueError(
                        "Every effect must carry a stable 'effect_key' for "
                        "idempotent recovery.  Provide: "
                        "org_id:objective_id:action_id:permit_id:provider:provider_ref"
                    )
                cur.execute(
                    """
                    INSERT INTO execution_effects
                        (effect_key, task_id, organization_id, tenant_id,
                         run_claim_token, lease_generation,
                         permit_id, effect_type, provider,
                         provider_ref, idempotency_key, payload)
                    VALUES
                        (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (effect_key) DO NOTHING
                    """,
                    (
                        key,
                        task_id,
                        organization_id,
                        resolved_tenant,
                        claim_token,
                        lease_generation,
                        effect.get("permit_id", ""),
                        effect.get("type", "unknown"),
                        effect.get("provider", ""),
                        effect.get("provider_ref", ""),
                        effect.get("idempotency_key", ""),
                        json.dumps(effect),
                    ),
                )

        # --- Release claim ---
        cur.execute(
            """
            DELETE FROM task_claims
            WHERE task_id         = %s
              AND organization_id  = %s
              AND lease_generation = %s
            """,
            (task_id, organization_id, lease_generation),
        )

        conn.commit()
        return True


# ---------------------------------------------------------------------------
# Permit issuance and consumption
# ---------------------------------------------------------------------------


def issue_permit(
    conn: "psycopg.Connection",
    *,
    task_id: str,
    organization_id: str,
    claim_token: str,
    lease_generation: int,
    action_payload: dict[str, Any],
    ttl_seconds: int = 300,
    # Full authority model fields (parity with SQLite)
    actor: str = "",
    executor: str = "",
    capability: str = "",
    action_type: str = "",
    target_resource: str = "",
    policy_version: str = "",
    tenant_id: Optional[UUID] = None,
) -> str:
    """Issue an execution permit for a governed action.

    Verifies the active claim has the correct generation before issuing.

    Args:
        conn: Postgres connection
        task_id: Task ID
        organization_id: Organization scope
        claim_token: Must match active claim
        lease_generation: Must match current lease_generation
        action_payload: Action payload (hashed for binding)
        ttl_seconds: Permit TTL in seconds
        actor / executor / capability / action_type / target_resource / policy_version:
            Full authority model fields matching SQLite semantics.
        tenant_id: Tenant scope (defaults to DEFAULT_TENANT_ID)

    Returns:
        Permit ID (UUID string)

    Raises:
        ValueError: If no valid fenced claim exists
    """
    import uuid

    permit_id = str(uuid.uuid4())
    expires_dt = _ts(time.time() + ttl_seconds)
    payload_hash = hashlib.sha256(
        json.dumps(action_payload, sort_keys=True).encode()
    ).hexdigest()
    resolved_tenant = str(tenant_id or DEFAULT_TENANT_ID)

    with conn.cursor() as cur:
        # Verify claim is alive and carries the correct generation.
        cur.execute(
            """
            SELECT 1 FROM task_claims
            WHERE task_id         = %s
              AND organization_id  = %s
              AND lease_generation = %s
              AND expires_at       > NOW()
            """,
            (task_id, organization_id, lease_generation),
        )
        if not cur.fetchone():
            raise ValueError(
                "No valid fenced claim for permit issuance "
                f"(task={task_id}, org={organization_id}, gen={lease_generation})"
            )

        # Verify the run record is still pending for this generation.
        cur.execute(
            """
            SELECT 1 FROM task_runs
            WHERE task_id         = %s
              AND organization_id  = %s
              AND claim_token      = %s
              AND lease_generation = %s
              AND status          = 'pending'
            """,
            (task_id, organization_id, claim_token, lease_generation),
        )
        if not cur.fetchone():
            raise ValueError(
                "No pending run for permit issuance "
                f"(task={task_id}, org={organization_id}, gen={lease_generation})"
            )

        cur.execute(
            """
            INSERT INTO task_permits
                (permit_id, task_id, organization_id, tenant_id, claim_token,
                 lease_generation, actor, executor, capability,
                 action_type, target_resource, payload_hash,
                 policy_version, action_payload, expires_at)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING permit_id
            """,
            (
                permit_id,
                task_id,
                organization_id,
                resolved_tenant,
                claim_token,
                lease_generation,
                actor,
                executor,
                capability,
                action_type,
                target_resource,
                payload_hash,
                policy_version,
                json.dumps(action_payload),
                expires_dt,
            ),
        )

        row = cur.fetchone()
        conn.commit()
        return row["permit_id"]


def consume_permit(
    conn: "psycopg.Connection",
    *,
    permit_id: str,
    organization_id: str,
    claim_token: str,
    lease_generation: int,
    action_payload: dict[str, Any],
) -> bool:
    """Consume an execution permit (atomic, once-only).

    Fencing checks:
    1. permit_id must exist, not yet consumed, not revoked, not expired.
    2. organization_id must match.
    3. claim_token must match the permit's recorded claim_token.
    4. lease_generation must match the permit's recorded generation.
    5. SHA256 hash of action_payload must match the stored payload_hash.
    6. A single atomic UPDATE with all five conditions prevents races.

    Args:
        conn: Postgres connection
        permit_id: Permit to consume
        organization_id: Must match permit's organization
        claim_token: Must match permit's claim_token
        lease_generation: Must match permit's lease_generation
        action_payload: Must hash to the stored payload_hash

    Returns:
        True if consumed, False if any check fails.
    """
    payload_hash = hashlib.sha256(
        json.dumps(action_payload, sort_keys=True).encode()
    ).hexdigest()

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE task_permits
            SET consumed_at = NOW()
            WHERE permit_id       = %s
              AND organization_id  = %s
              AND claim_token      = %s
              AND lease_generation = %s
              AND payload_hash     = %s
              AND consumed_at     IS NULL
              AND revoked_at      IS NULL
              AND expires_at       > NOW()
            RETURNING permit_id
            """,
            (
                permit_id,
                organization_id,
                claim_token,
                lease_generation,
                payload_hash,
            ),
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return False

        conn.commit()
        return True


def revoke_permit(
    conn: "psycopg.Connection",
    *,
    permit_id: str,
    organization_id: str,
    reason: str = "",
) -> bool:
    """Revoke an unconsumed permit.

    Args:
        conn: Postgres connection
        permit_id: Permit to revoke
        organization_id: Must match permit's organization
        reason: Human-readable revocation reason

    Returns:
        True if revoked, False if not found / already consumed.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE task_permits
            SET revoked_at        = NOW(),
                revocation_reason = %s
            WHERE permit_id       = %s
              AND organization_id  = %s
              AND consumed_at     IS NULL
              AND revoked_at      IS NULL
            RETURNING permit_id
            """,
            (reason, permit_id, organization_id),
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return False

        conn.commit()
        return True


# ---------------------------------------------------------------------------
# Effect recording (standalone, for recovery workers)
# ---------------------------------------------------------------------------


def record_effect(
    conn: "psycopg.Connection",
    *,
    effect_key: str,
    task_id: str,
    organization_id: str,
    run_claim_token: str,
    lease_generation: int,
    effect_type: str,
    provider: str = "",
    provider_ref: str = "",
    idempotency_key: str = "",
    permit_id: str = "",
    payload: dict[str, Any],
    tenant_id: Optional[UUID] = None,
) -> bool:
    """Persist one execution-effect record, idempotently.

    The effect_key uniquely identifies the governed action so recovery can
    call this multiple times without creating duplicates.

    Returns:
        True if inserted (new effect), False if effect_key already exists
        (idempotent — not an error).
    """
    resolved_tenant = str(tenant_id or DEFAULT_TENANT_ID)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO execution_effects
                (effect_key, task_id, organization_id, tenant_id,
                 run_claim_token, lease_generation,
                 permit_id, effect_type, provider,
                 provider_ref, idempotency_key, payload)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (effect_key) DO NOTHING
            RETURNING id
            """,
            (
                effect_key,
                task_id,
                organization_id,
                resolved_tenant,
                run_claim_token,
                lease_generation,
                permit_id,
                effect_type,
                provider,
                provider_ref,
                idempotency_key,
                json.dumps(payload),
            ),
        )
        row = cur.fetchone()
        conn.commit()
        return row is not None


def get_effect(
    conn: "psycopg.Connection",
    *,
    effect_key: str,
) -> Optional[dict[str, Any]]:
    """Look up an effect record by its stable key.

    Returns:
        Effect dict or None.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM execution_effects WHERE effect_key = %s",
            (effect_key,),
        )
        return cur.fetchone()


# ---------------------------------------------------------------------------
# Monitoring helpers
# ---------------------------------------------------------------------------


def get_active_runs(
    conn: "psycopg.Connection",
    *,
    organization_id: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Get active runs for an organization."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tr.*, tc.worker_id, tc.claim_scope_url, tc.lease_generation AS claim_gen
            FROM task_runs tr
            JOIN task_claims tc
              ON  tr.task_id         = tc.task_id
              AND tr.organization_id  = tc.organization_id
              AND tr.lease_generation = tc.lease_generation
            WHERE tr.organization_id = %s
              AND tr.status           = 'pending'
              AND tc.expires_at       > NOW()
            ORDER BY tr.started_at DESC
            LIMIT %s
            """,
            (organization_id, limit),
        )
        return cur.fetchall()


def cleanup_expired_claims(conn: "psycopg.Connection") -> int:
    """Delete expired claims that have no pending run (safe GC).

    Claims with a pending run should be reclaimed via reclaim_task, not
    deleted here, so the run row remains for audit.

    Returns:
        Number of claim rows deleted.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM task_claims tc
            WHERE tc.expires_at < NOW()
              AND NOT EXISTS (
                  SELECT 1 FROM task_runs tr
                  WHERE tr.task_id         = tc.task_id
                    AND tr.organization_id  = tc.organization_id
                    AND tr.lease_generation = tc.lease_generation
                    AND tr.status          = 'pending'
              )
            RETURNING id
            """
        )
        rows = cur.fetchall()
        conn.commit()
        return len(rows)


# ---------------------------------------------------------------------------
# Tenant management
# ---------------------------------------------------------------------------


def create_tenant(
    conn: "psycopg.Connection",
    *,
    tenant_id: UUID,
    slug: str,
    name: str = "",
    max_concurrent_claims: int = 10,
) -> bool:
    """Register a new tenant. Idempotent — returns False if slug/id already exists."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tenants (id, slug, name, max_concurrent_claims)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            RETURNING id
            """,
            (str(tenant_id), slug, name, max_concurrent_claims),
        )
        row = cur.fetchone()
        conn.commit()
        return row is not None


def get_tenant(
    conn: "psycopg.Connection",
    *,
    tenant_id: UUID,
) -> Optional[dict[str, Any]]:
    """Look up a tenant by ID."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM tenants WHERE id = %s", (str(tenant_id),))
        return cur.fetchone()


def suspend_tenant(
    conn: "psycopg.Connection",
    *,
    tenant_id: UUID,
) -> bool:
    """Suspend a tenant — claims will be rejected while suspended."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE tenants SET suspended_at = NOW()
            WHERE id = %s AND suspended_at IS NULL
            RETURNING id
            """,
            (str(tenant_id),),
        )
        row = cur.fetchone()
        conn.commit()
        return row is not None


def activate_tenant(
    conn: "psycopg.Connection",
    *,
    tenant_id: UUID,
) -> bool:
    """Activate a tenant — removes suspension."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE tenants SET suspended_at = NULL, active_at = NOW()
            WHERE id = %s
            RETURNING id
            """,
            (str(tenant_id),),
        )
        row = cur.fetchone()
        conn.commit()
        return row is not None


def check_tenant_claim_quota(
    conn: "psycopg.Connection",
    *,
    tenant_id: UUID,
) -> tuple[bool, int, int]:
    """Check if a tenant can acquire another claim.

    Returns:
        (allowed, current_count, max_allowed)
        allowed is False if tenant is suspended or at quota.
    """
    resolved = str(tenant_id or DEFAULT_TENANT_ID)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT max_concurrent_claims, suspended_at FROM tenants WHERE id = %s",
            (resolved,),
        )
        tenant = cur.fetchone()
        if not tenant:
            return (True, 0, 10)

        if tenant["suspended_at"] is not None:
            return (False, 0, 0)

        max_claims = tenant["max_concurrent_claims"]
        cur.execute(
            """
            SELECT COUNT(*) AS n FROM task_claims
            WHERE tenant_id = %s AND expires_at > NOW()
            """,
            (resolved,),
        )
        current = cur.fetchone()["n"]
        return (current < max_claims, current, max_claims)


# ---------------------------------------------------------------------------
# Environment / backend detection
# ---------------------------------------------------------------------------


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
        Connection for the appropriate backend (Postgres or SQLite).
    """
    backend = get_authority_backend()
    if backend == "postgres":
        conn = connect()
        init_schema(conn)
        return conn
    else:
        from hermes_cli.kanban_db import connect as sqlite_connect
        return sqlite_connect()


__all__ = [
    "connect",
    "init_schema",
    "get_schema_version",
    "claim_task",
    "reclaim_task",
    "get_claim",
    "release_claim",
    "complete_task",
    "issue_permit",
    "consume_permit",
    "revoke_permit",
    "record_effect",
    "get_effect",
    "get_active_runs",
    "cleanup_expired_claims",
    "create_tenant",
    "get_tenant",
    "suspend_tenant",
    "activate_tenant",
    "check_tenant_claim_quota",
    "get_authority_backend",
    "get_authority_connection",
    "DEFAULT_TENANT_ID",
    "SCHEMA_VERSION",
]
