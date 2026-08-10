"""Canonical schema and migration metadata for the ARCH-001 state store.

This package is intentionally separate from :mod:`hermes_state`.  The latter
stores conversation history; this module defines the durable infrastructure
state shared by future approval, recovery, compression, and watchdog code.
"""

from __future__ import annotations

from hashlib import sha256

SCHEMA_VERSION = 1
MIN_COMPATIBLE_SCHEMA_VERSION = 1

STATE_TABLES = (
    "session_state",
    "task_state",
    "approval_state",
    "compression_state",
)

TABLE_BUSINESS_KEY = {
    "session_state": "session_id",
    "task_state": "task_id",
    "approval_state": "approval_id",
    "compression_state": "session_id",
}

SCHEMA_DDL = """
CREATE TABLE schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version INTEGER NOT NULL,
    min_compatible_schema_version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE runtime_state_migrations (
    version INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    checksum_sha256 TEXT NOT NULL CHECK (length(checksum_sha256) = 64),
    applied_at TEXT NOT NULL
);

CREATE TABLE session_state (
    profile_name TEXT NOT NULL,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workspace TEXT NOT NULL,
    target_host TEXT,
    deployment_target TEXT,
    status TEXT NOT NULL CHECK (status IN
        ('active', 'completed', 'failed', 'degraded', 'cancelled')),
    owner TEXT,
    owner_version INTEGER NOT NULL DEFAULT 0 CHECK (owner_version >= 0),
    schema_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (profile_name, session_id)
);

CREATE TABLE task_state (
    profile_name TEXT NOT NULL,
    task_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    branch TEXT,
    worktree TEXT,
    target_host TEXT,
    deployment_target TEXT,
    status TEXT NOT NULL CHECK (status IN
        ('pending', 'running', 'succeeded', 'failed', 'blocked',
         'cancelled', 'degraded')),
    owner TEXT,
    owner_version INTEGER NOT NULL DEFAULT 0 CHECK (owner_version >= 0),
    schema_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (profile_name, task_id),
    UNIQUE (profile_name, task_id, session_id),
    FOREIGN KEY (profile_name, session_id)
        REFERENCES session_state(profile_name, session_id)
        ON DELETE RESTRICT
);

CREATE TABLE approval_state (
    profile_name TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    session_id TEXT,
    task_id TEXT,
    approval_status TEXT NOT NULL CHECK (approval_status IN
        ('pending', 'approved', 'denied', 'expired', 'reset_pending')),
    breaker_status TEXT NOT NULL CHECK (breaker_status IN
        ('closed', 'open', 'reset_pending')),
    owner TEXT,
    owner_version INTEGER NOT NULL DEFAULT 0 CHECK (owner_version >= 0),
    schema_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (profile_name, approval_id),
    CHECK (session_id IS NOT NULL OR task_id IS NOT NULL),
    FOREIGN KEY (profile_name, session_id)
        REFERENCES session_state(profile_name, session_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (profile_name, task_id)
        REFERENCES task_state(profile_name, task_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (profile_name, task_id, session_id)
        REFERENCES task_state(profile_name, task_id, session_id)
        ON DELETE RESTRICT
);

CREATE TABLE compression_state (
    profile_name TEXT NOT NULL,
    session_id TEXT NOT NULL,
    task_id TEXT,
    compression_status TEXT NOT NULL CHECK (compression_status IN
        ('idle', 'running', 'succeeded', 'failed', 'degraded', 'disabled')),
    owner TEXT,
    owner_version INTEGER NOT NULL DEFAULT 0 CHECK (owner_version >= 0),
    schema_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (profile_name, session_id),
    FOREIGN KEY (profile_name, session_id)
        REFERENCES session_state(profile_name, session_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (profile_name, task_id)
        REFERENCES task_state(profile_name, task_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (profile_name, task_id, session_id)
        REFERENCES task_state(profile_name, task_id, session_id)
        ON DELETE RESTRICT
);

CREATE INDEX task_state_session_idx
    ON task_state(profile_name, session_id);
CREATE INDEX approval_state_session_idx
    ON approval_state(profile_name, session_id);
CREATE INDEX approval_state_task_idx
    ON approval_state(profile_name, task_id);
CREATE INDEX compression_state_task_idx
    ON compression_state(profile_name, task_id);
CREATE INDEX session_state_schema_version_idx
    ON session_state(schema_version);
CREATE INDEX task_state_schema_version_idx
    ON task_state(schema_version);
CREATE INDEX approval_state_schema_version_idx
    ON approval_state(schema_version);
CREATE INDEX compression_state_schema_version_idx
    ON compression_state(schema_version);
""".strip()

MIGRATION_1_SEED_SQL = """
INSERT INTO schema_version
    (id, schema_version, min_compatible_schema_version, updated_at)
VALUES (1, 1, 1, '<timestamp>');

INSERT INTO runtime_state_migrations
    (version, description, checksum_sha256, applied_at)
VALUES (1, 'Initial runtime state schema', '<sha256_hex>', '<timestamp>');
""".strip()


def normalize_migration_body(body: str) -> str:
    """Normalize migration text before hashing, independent of OS line endings."""

    return body.replace("\r\n", "\n").replace("\r", "\n")


MIGRATION_1_BODY = f"{SCHEMA_DDL}\n\n{MIGRATION_1_SEED_SQL}"
MIGRATION_1_CHECKSUM = sha256(
    normalize_migration_body(MIGRATION_1_BODY).encode("utf-8")
).hexdigest()

MIGRATION_METADATA = (
    {
        "version": 1,
        "description": "Initial runtime state schema",
        "checksum_sha256": MIGRATION_1_CHECKSUM,
    },
)

# Compatibility alias for downstream code and tests that use the ticket's name.
SCHEMA_SQL = SCHEMA_DDL
