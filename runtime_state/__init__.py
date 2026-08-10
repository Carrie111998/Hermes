"""Standalone HERMES-ARCH-001 runtime-state infrastructure.

This package is not wired into ``cli.py``, ``gateway/``, or live services by
ARCH-001. It is intentionally separate from ``hermes_state.py``'s
conversation-history database.
"""

from runtime_state.contract import (
    ClaimResult,
    CasResult,
    CreateResult,
    INVALID_PROFILE_REFERENCE,
    NOT_FOUND,
    OWNER_MISMATCH,
    OWNED_TABLES,
    STALE_VERSION,
    SUCCESS,
    UpdateResult,
    cas_claim_owner,
    cas_release_owner,
    cas_update_columns,
    create_approval_state,
    create_compression_state,
    create_session_state,
    create_task_state,
)
from runtime_state.migrations import (
    RuntimeStateDB,
    RuntimeStateMigrationError,
    RuntimeStatePreflightError,
    RuntimeStateSchemaError,
    utc_timestamp,
)
from runtime_state.retry_config import (
    DEFAULT_RETRY_CONFIG,
    MAX_BUSY_TIMEOUT_MS,
    MIN_BUSY_TIMEOUT_MS,
    RetryConfig,
    apply_busy_timeout,
    no_retry_hook,
)
from runtime_state.schema import (
    MIGRATION_1_BODY,
    MIGRATION_1_CHECKSUM,
    MIGRATION_1_SEED_SQL,
    MIGRATION_METADATA,
    MIN_COMPATIBLE_SCHEMA_VERSION,
    SCHEMA_DDL,
    SCHEMA_SQL,
    SCHEMA_VERSION,
    STATE_TABLES,
    normalize_migration_body,
)

__all__ = [
    "SCHEMA_VERSION",
    "MIN_COMPATIBLE_SCHEMA_VERSION",
    "SCHEMA_DDL",
    "SCHEMA_SQL",
    "STATE_TABLES",
    "MIGRATION_METADATA",
    "MIGRATION_1_BODY",
    "MIGRATION_1_SEED_SQL",
    "MIGRATION_1_CHECKSUM",
    "normalize_migration_body",
    "RuntimeStateDB",
    "RuntimeStateSchemaError",
    "RuntimeStatePreflightError",
    "RuntimeStateMigrationError",
    "RetryConfig",
    "DEFAULT_RETRY_CONFIG",
    "MIN_BUSY_TIMEOUT_MS",
    "MAX_BUSY_TIMEOUT_MS",
    "apply_busy_timeout",
    "no_retry_hook",
    "utc_timestamp",
    "OWNED_TABLES",
    "CasResult",
    "ClaimResult",
    "CreateResult",
    "UpdateResult",
    "SUCCESS",
    "STALE_VERSION",
    "OWNER_MISMATCH",
    "NOT_FOUND",
    "INVALID_PROFILE_REFERENCE",
    "cas_claim_owner",
    "cas_update_columns",
    "cas_release_owner",
    "create_session_state",
    "create_task_state",
    "create_approval_state",
    "create_compression_state",
]
