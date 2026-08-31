from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, tzinfo
from decimal import Decimal
import errno
import hashlib
import hmac
import json
import math
import os
import random
import re
import secrets
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, TypeVar, cast

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from hermes_state import SCHEMA_VERSION, SessionDB
from hermes_constants import get_hermes_home

from .claude_visibility_codes import (
    CLAUDE_VISIBILITY_FATAL_CODES,
    CLAUDE_VISIBILITY_PUBLIC_RESULT_ERROR_CODES,
    CLAUDE_VISIBILITY_RETRY_CODES,
)
from .models import (
    BridgeMarkerPayload,
    ContextPack,
    MirrorJobState,
    OriginKind,
    ProjectedMessage,
    Provider,
    Relation,
    SessionLink,
    SessionProjection,
    SidebarHydrationState,
    SidebarJobState,
    UpsertResult,
    canonical_session_id,
    decode_bridge_marker,
    encode_bridge_marker,
)
from .sidebar_reconciliation import (
    SidebarReconciliationEvidence,
    SidebarReconciliationProofInput,
    sidebar_reconciliation_proof_digest,
)

if TYPE_CHECKING:
    from .claude_visibility import (
        ClaudeVisibilityCandidate,
        ClaudeVisibilityClaim,
        ClaudeVisibilityIdentity,
    )
    from .sidebar import SidebarCandidate
    from .worktree import WorktreeSnapshot


_EXTERNAL_PROVIDERS = (Provider.CLAUDE, Provider.CODEX)
_MESSAGE_KEY_QUERY_CHUNK = 400
_CONTINUATION_SNAPSHOT_STATE_PREFIX = "session-bridge:continuation:"
_MIRROR_ATTEMPT_STATE_PREFIX = "session-bridge:attempt:"
_MIRROR_AUTHORITY_STATE_PREFIX = "session-bridge:mirror-authority:"
_MIRROR_RATE_STATE_KEY = "session-bridge:mirror-rate"
_MIRROR_BREAKER_STATE_KEY = "session-bridge:mirror-breaker"
_MIRROR_BREAKER_RESERVATION_PREFIX = "session-bridge:breaker-reservation:"
_SIDEBAR_DELIVERY_STATE_PREFIX = "session-bridge:sidebar-delivery:"
_SIDEBAR_CREATE_RESERVATION_PREFIX = "session-bridge:sidebar-create:"
_SIDEBAR_CREATE_RESERVATION_CUTOVER_STATE_KEY = (
    "session-bridge:sidebar:create-reservation-cutover:v1"
)
_SIDEBAR_BROKER_HEARTBEAT_STATE_KEY = "session-bridge:sidebar:broker-heartbeat"
_SIDEBAR_PENDING_LANE_STATE_KEY = "session-bridge:sidebar:pending-lane:v1"
_SIDEBAR_RECOVERY_PROGRESS_STATE_KEY = (
    "session-bridge:sidebar:recovery-progress:v1"
)
_SIDEBAR_PLACEMENT_CANARY_STATE_KEY = (
    "session-bridge:sidebar:placement-canary:v1"
)
_SIDEBAR_PLACEMENT_CANARY_STATE_FIELDS = frozenset({
    "version",
    "status",
    "placement_generation",
    "verified_at",
    "canary_identity_digest",
})
_SIDEBAR_PLACEMENT_CANARY_DIGEST_DOMAIN = (
    b"session-bridge:sidebar:placement-canary:v1\0"
)
_SIDEBAR_FRESH_BURST = 3
_SIDEBAR_HYDRATION_LEASE_SECONDS = 300
_SIDEBAR_HYDRATION_LEASE_KEY = b"session-sidebar-hydration-lease-v1"
_SIDEBAR_HYDRATION_COMPLETION_KEY = b"session-sidebar-hydration-completion-v1"
_SIDEBAR_HYDRATION_MAX_ATTEMPTS = 5
_CLAUDE_VISIBILITY_CYCLE_STATE_KEY = "session-bridge:claude-visibility:cycle"
_CLAUDE_VISIBILITY_CYCLE_STATE_VERSION = 2
_CLAUDE_LINEAGE_RECONCILE_LIMIT_MAX = 100
_CLAUDE_LINEAGE_CURSOR_VERSION = 1
_CLAUDE_LINEAGE_CURSOR_OPERATION = "claude_visibility_lineage_reconcile"
_CLAUDE_LINEAGE_CURSOR_DOMAIN = b"session-bridge:claude-lineage-cursor:v1\0"
_CLAUDE_LINEAGE_CURSOR_UNSIGNED_FIELDS = frozenset({
    "version",
    "schema_version",
    "operation",
    "mode",
    "after_visible_at",
    "after_job_id",
    "high_water_visible_at",
    "high_water_job_id",
})
_CLAUDE_LINEAGE_CURSOR_FIELDS = frozenset({
    *_CLAUDE_LINEAGE_CURSOR_UNSIGNED_FIELDS,
    "signature",
})
_CLAUDE_LINEAGE_TARGET_MISSING = "claude_lineage_target_missing"
_CLAUDE_LINEAGE_TARGET_DUPLICATE = "claude_lineage_target_duplicate"
_CLAUDE_LINEAGE_TARGET_IDENTITY_MISMATCH = "claude_lineage_target_identity_mismatch"
_CLAUDE_LINEAGE_TARGET_PROVENANCE_MISMATCH = "claude_lineage_target_provenance_mismatch"
_CLAUDE_LINEAGE_MISSING_SOURCE = "claude_lineage_missing_source"
_CLAUDE_LINEAGE_SOURCE_IDENTITY_MISMATCH = "claude_lineage_source_identity_mismatch"
_CLAUDE_LINEAGE_SOURCE_PROVENANCE_MISMATCH = "claude_lineage_source_provenance_mismatch"
_CLAUDE_LINEAGE_INVALID_COMPLETION = "claude_lineage_invalid_completion"
_CLAUDE_LINEAGE_CONFLICT = "claude_lineage_conflict"
_PROFILE_SHADOW_SOURCE = "session_bridge_profile"
_EXTERNAL_ACTIVITY_KEY_PREFIX = "session-bridge:external-activity:"
# Repeated verbatim by idx_session_bridge_state_activity_ordered.  SQLite only
# substitutes the indexed value -- and only satisfies the ORDER BY from the
# index -- when the query's expression matches the index's exactly.
_SIDEBAR_ACTIVITY_EXPR = (
    "CASE WHEN json_valid(activity.value_json) "
    "THEN CAST(json_extract(activity.value_json, '$.last_active') AS REAL) END"
)
_WORKTREE_SNAPSHOT_STATE_PREFIX = "session-bridge:worktree:"
_WORKTREE_SNAPSHOT_FIELDS = frozenset({
    "version",
    "source_session_id",
    "cwd",
    "git_root",
    "branch",
    "head",
    "worktree_id",
})
_NATIVE_SESSION_SNAPSHOT_FIELDS = (
    "id",
    "source",
    "model",
    "title",
    "started_at",
    "ended_at",
    "end_reason",
    "message_count",
    "tool_call_count",
    "cwd",
    "git_branch",
    "git_repo_root",
    "rewind_count",
    "archived",
)
_NATIVE_MESSAGE_SNAPSHOT_FIELDS = (
    "id",
    "role",
    "content",
    "tool_call_id",
    "tool_calls",
    "tool_name",
    "timestamp",
    "finish_reason",
    "reasoning",
    "reasoning_details",
    "codex_reasoning_items",
    "reasoning_content",
    "codex_message_items",
    "active",
    "compacted",
)
_SIDEBAR_DELIVERY_STATE_FIELDS = frozenset({
    "version",
    "source_session_id",
    "provider",
    "bridge_id",
    "title",
    "cwd",
    "git_root",
    "git_branch",
    "git_head",
    "worktree_id",
    "eligible_at",
})
_SIDEBAR_CREATE_RESERVATION_V1_FIELDS = frozenset({
    "version",
    "job_id",
    "source_session_id",
    "bridge_id",
    "recovery_key",
    "reserved_at",
})
_SIDEBAR_CREATE_RESERVATION_FIELDS = frozenset({
    *_SIDEBAR_CREATE_RESERVATION_V1_FIELDS,
    "reconciliation_proof_digest",
    "reconciliation_generation",
})
_SIDEBAR_CREATE_RECOVERY_PREFIX = "hermes-session-bridge-create-v1:"
_STRUCTURED_CONTENT_HEX_PREFIX = "006A736F6E3A"
_PYTHON_STRIP_CHARACTERS = (
    "\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)
_CONTINUATION_SNAPSHOT_FIELDS = frozenset({
    "version",
    "pack_id",
    "source_session_id",
    "source_cursor",
    "source_hash",
    "target_session_id",
    "target_cursor",
    "target_hash",
})
SIDEBAR_RETRYABLE_ERRORS = frozenset({
    "codex_tool_unavailable",
    "desktop_offline",
    "bridge_temporarily_unavailable",
    "sqlite_busy",
    "rename_failed",
    "project_lookup_failed",
    "native_task_not_indexed",
    "broker_time_budget",
    "inbox_unavailable",
})
SIDEBAR_FATAL_ERRORS = frozenset({
    "native_create_ambiguous",
    "marker_conflict",
    "source_identity_mismatch",
    "codex_thread_conflict",
    "provider_mismatch",
    "source_cwd_missing",
    "permission_preflight_failed",
    "retry_budget_exhausted",
    "placement_mismatch",
})
HYDRATION_RETRYABLE_ERRORS = frozenset({
    "codex_tool_unavailable",
    "native_task_not_indexed",
    "hydration_send_ambiguous",
    "bridge_temporarily_unavailable",
    "broker_time_budget",
})
HYDRATION_FATAL_ERRORS = frozenset({
    "marker_conflict",
    "source_identity_mismatch",
    "codex_thread_conflict",
    "preview_digest_mismatch",
})
SIDEBAR_EXCLUSION_REASONS = frozenset({"source_cwd_missing"})
SIDEBAR_BOUND_RETRY_CONFIRMATION = "PRESERVE_EXACT_BOUND_TASK"
SIDEBAR_SOURCE_CWD_REPAIR_CONFIRMATION = (
    "PRESERVE_EXACT_BOUND_TASK_AFTER_SOURCE_CWD_REPAIR"
)
SIDEBAR_TERMINAL_RESOLUTION_CODE = "native_thread_unrecoverable"
SIDEBAR_TERMINAL_EVIDENCE_KIND = "codex_app_server_read_not_loaded_resume_no_rollout"
SIDEBAR_TERMINAL_EVIDENCE_VERSION = 1
SIDEBAR_PRECREATE_RESOLUTION_CODE = "precutover_create_unrecoverable"
SIDEBAR_PRECREATE_EVIDENCE_KIND = "codex_inventory_marker_and_recovery_zero_no_rollout"
SIDEBAR_PRECREATE_EVIDENCE_VERSION = 1
SIDEBAR_UNBOUND_RESOLUTION_CODE = "native_create_unrecoverable"
SIDEBAR_UNBOUND_EVIDENCE_KIND = (
    "codex_inventory_marker_and_recovery_zero_no_rollout"
)
SIDEBAR_UNBOUND_EVIDENCE_VERSION = 1
SIDEBAR_V2_ATTEMPT_ZERO_RESOLUTION_CODE = "v2_attempt_zero_create_unrecoverable"
SIDEBAR_V2_ATTEMPT_ZERO_EVIDENCE_KIND = (
    "codex_inventory_marker_and_recovery_zero_with_bound_absence_proof"
)
SIDEBAR_V2_ATTEMPT_ZERO_EVIDENCE_VERSION = 1


class SidebarNativeTaskNotIndexed(ValueError):
    """A verified native Codex task is not yet present in bridge inventory."""

    def __init__(self) -> None:
        super().__init__("native_task_not_indexed")


def sidebar_bound_retry_authority_matches(
    error_code: str,
    confirmation: str,
) -> bool:
    standard_errors = {
        "native_task_not_indexed",
        "codex_thread_conflict",
        "native_create_ambiguous",
        "marker_conflict",
        "bridge_temporarily_unavailable",
    }
    return (
        error_code in standard_errors
        and confirmation == SIDEBAR_BOUND_RETRY_CONFIRMATION
    ) or (
        error_code == "source_identity_mismatch"
        and confirmation == SIDEBAR_SOURCE_CWD_REPAIR_CONFIRMATION
    )


_SIDEBAR_TERMINAL_LEDGER_COLUMNS = (
    "job_id",
    "idempotency_key",
    "source_session_id",
    "bridge_id",
    "codex_thread_id",
    "failure_state",
    "failure_code",
    "failure_attempts",
    "failure_next_attempt_at",
    "failure_updated_at",
    "resolution_code",
    "evidence_kind",
    "evidence_version",
    "evidence_digest",
    "resolved_at",
)
_SIDEBAR_TERMINAL_LEDGER_SQL_REQUIREMENTS = (
    "job_id TEXT PRIMARY KEY REFERENCES session_sidebar_jobs(id) "
    "ON UPDATE RESTRICT ON DELETE RESTRICT",
    "idempotency_key TEXT NOT NULL UNIQUE",
    "source_session_id TEXT NOT NULL UNIQUE",
    "bridge_id TEXT NOT NULL UNIQUE",
    "codex_thread_id TEXT NOT NULL UNIQUE",
    "failure_state TEXT NOT NULL CHECK (failure_state = 'sidebar_failed')",
    "failure_code TEXT NOT NULL CHECK (failure_code = 'native_create_ambiguous')",
    "failure_attempts INTEGER NOT NULL CHECK (failure_attempts >= 0)",
    "failure_next_attempt_at REAL NOT NULL",
    "failure_updated_at REAL NOT NULL",
    "resolution_code TEXT NOT NULL CHECK ( resolution_code = "
    "'native_thread_unrecoverable' )",
    "evidence_kind TEXT NOT NULL CHECK ( evidence_kind = "
    "'codex_app_server_read_not_loaded_resume_no_rollout' )",
    "evidence_version INTEGER NOT NULL CHECK (evidence_version = 1)",
    "evidence_digest TEXT NOT NULL CHECK ( length(evidence_digest) = 64 "
    "AND evidence_digest NOT GLOB '*[^0-9a-f]*' )",
    "resolved_at REAL NOT NULL",
    "CHECK (resolved_at >= failure_updated_at)",
)
_SIDEBAR_PRECREATE_LEDGER_COLUMNS = (
    "job_id",
    "idempotency_key",
    "source_session_id",
    "bridge_id",
    "failure_state",
    "failure_code",
    "failure_attempts",
    "failure_next_attempt_at",
    "failure_updated_at",
    "cutover_applied_at",
    "reservation_reserved_at",
    "resolution_code",
    "evidence_kind",
    "evidence_version",
    "evidence_digest",
    "resolved_at",
)
_SIDEBAR_PRECREATE_LEDGER_SQL_REQUIREMENTS = (
    "job_id TEXT PRIMARY KEY REFERENCES session_sidebar_jobs(id) "
    "ON UPDATE RESTRICT ON DELETE RESTRICT",
    "idempotency_key TEXT NOT NULL UNIQUE",
    "source_session_id TEXT NOT NULL UNIQUE",
    "bridge_id TEXT NOT NULL UNIQUE",
    "failure_state TEXT NOT NULL CHECK (failure_state = 'sidebar_failed')",
    "failure_code TEXT NOT NULL CHECK (failure_code = 'native_create_ambiguous')",
    "failure_attempts INTEGER NOT NULL CHECK (failure_attempts = 0)",
    "failure_next_attempt_at REAL NOT NULL",
    "failure_updated_at REAL NOT NULL",
    "cutover_applied_at REAL NOT NULL",
    "reservation_reserved_at REAL NOT NULL",
    "resolution_code TEXT NOT NULL CHECK ( resolution_code = "
    "'precutover_create_unrecoverable' )",
    "evidence_kind TEXT NOT NULL CHECK ( evidence_kind = "
    "'codex_inventory_marker_and_recovery_zero_no_rollout' )",
    "evidence_version INTEGER NOT NULL CHECK (evidence_version = 1)",
    "evidence_digest TEXT NOT NULL CHECK ( length(evidence_digest) = 64 "
    "AND evidence_digest NOT GLOB '*[^0-9a-f]*' )",
    "resolved_at REAL NOT NULL",
    "CHECK (reservation_reserved_at = cutover_applied_at)",
    "CHECK (resolved_at >= failure_updated_at)",
)
_SIDEBAR_UNBOUND_LEDGER_COLUMNS = (
    "job_id",
    "idempotency_key",
    "source_session_id",
    "bridge_id",
    "failure_state",
    "failure_code",
    "failure_attempts",
    "failure_next_attempt_at",
    "failure_updated_at",
    "reservation_reserved_at",
    "resolution_code",
    "evidence_kind",
    "evidence_version",
    "evidence_digest",
    "resolved_at",
)
_SIDEBAR_UNBOUND_LEDGER_SQL_REQUIREMENTS = (
    "job_id TEXT PRIMARY KEY REFERENCES session_sidebar_jobs(id) "
    "ON UPDATE RESTRICT ON DELETE RESTRICT",
    "idempotency_key TEXT NOT NULL UNIQUE",
    "source_session_id TEXT NOT NULL UNIQUE",
    "bridge_id TEXT NOT NULL UNIQUE",
    "failure_state TEXT NOT NULL CHECK (failure_state = 'sidebar_failed')",
    "failure_code TEXT NOT NULL CHECK (failure_code = 'native_create_ambiguous')",
    "failure_attempts INTEGER NOT NULL CHECK (failure_attempts > 0)",
    "failure_next_attempt_at REAL NOT NULL",
    "failure_updated_at REAL NOT NULL",
    "reservation_reserved_at REAL NOT NULL",
    "resolution_code TEXT NOT NULL CHECK ( resolution_code = "
    "'native_create_unrecoverable' )",
    "evidence_kind TEXT NOT NULL CHECK ( evidence_kind = "
    "'codex_inventory_marker_and_recovery_zero_no_rollout' )",
    "evidence_version INTEGER NOT NULL CHECK (evidence_version = 1)",
    "evidence_digest TEXT NOT NULL CHECK ( length(evidence_digest) = 64 "
    "AND evidence_digest NOT GLOB '*[^0-9a-f]*' )",
    "resolved_at REAL NOT NULL",
    "CHECK (resolved_at >= failure_updated_at)",
)
_SIDEBAR_V2_ATTEMPT_ZERO_LEDGER_COLUMNS = (
    "job_id", "idempotency_key", "source_session_id", "bridge_id",
    "failure_state", "failure_code", "failure_attempts",
    "failure_next_attempt_at", "failure_updated_at", "reservation_reserved_at",
    "reservation_reconciliation_proof_digest",
    "reservation_reconciliation_generation", "proof_completed_at",
    "proof_expires_at", "proof_inventory_digest", "resolution_code",
    "evidence_kind", "evidence_version", "evidence_digest", "resolved_at",
)
_SIDEBAR_V2_ATTEMPT_ZERO_LEDGER_SQL_REQUIREMENTS = (
    "job_id TEXT PRIMARY KEY REFERENCES session_sidebar_jobs(id) "
    "ON UPDATE RESTRICT ON DELETE RESTRICT",
    "idempotency_key TEXT NOT NULL UNIQUE",
    "source_session_id TEXT NOT NULL UNIQUE",
    "bridge_id TEXT NOT NULL UNIQUE",
    "failure_state TEXT NOT NULL CHECK (failure_state = 'sidebar_failed')",
    "failure_code TEXT NOT NULL CHECK (failure_code = 'native_create_ambiguous')",
    "failure_attempts INTEGER NOT NULL CHECK (failure_attempts = 0)",
    "failure_next_attempt_at REAL NOT NULL",
    "failure_updated_at REAL NOT NULL",
    "reservation_reserved_at REAL NOT NULL",
    "reservation_reconciliation_proof_digest TEXT NOT NULL UNIQUE REFERENCES "
    "session_sidebar_reconciliation_proofs(proof_digest) ON UPDATE RESTRICT "
    "ON DELETE RESTRICT",
    "reservation_reconciliation_generation TEXT NOT NULL",
    "proof_completed_at REAL NOT NULL",
    "proof_expires_at REAL NOT NULL",
    "proof_inventory_digest TEXT NOT NULL CHECK ( "
    "length(proof_inventory_digest) = 64 AND proof_inventory_digest NOT GLOB "
    "'*[^0-9a-f]*' )",
    "resolution_code TEXT NOT NULL CHECK ( resolution_code = "
    "'v2_attempt_zero_create_unrecoverable' )",
    "evidence_kind TEXT NOT NULL CHECK ( evidence_kind = "
    "'codex_inventory_marker_and_recovery_zero_with_bound_absence_proof' )",
    "evidence_version INTEGER NOT NULL CHECK (evidence_version = 1)",
    "evidence_digest TEXT NOT NULL CHECK ( length(evidence_digest) = 64 "
    "AND evidence_digest NOT GLOB '*[^0-9a-f]*' )",
    "resolved_at REAL NOT NULL",
    "CHECK (proof_expires_at > proof_completed_at)",
    "CHECK (resolved_at >= failure_updated_at)",
    "CHECK (resolved_at >= proof_completed_at)",
    "CHECK (resolved_at <= proof_expires_at)",
)
PUBLIC_SIDEBAR_STATE = {
    SidebarJobState.PENDING.value: "pending",
    SidebarJobState.LEASED.value: "pending",
    SidebarJobState.RETRY.value: "retrying",
    SidebarJobState.VISIBLE.value: "visible",
    SidebarJobState.FAILED.value: "failed",
}
_PUBLIC_CODEX_THREAD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,511}")
_SIDEBAR_RETRY_DELAYS_SECONDS = (60.0, 120.0, 240.0, 480.0, 900.0)
_SIDEBAR_LEASE_SECONDS = 300
# Four maximum-size broker batches bound malformed-row cleanup per write lock.
_SIDEBAR_CLAIM_SCAN_LIMIT = 40
# Delivery latency summarizes only the newest durable visible jobs, keeping status
# reads bounded while preserving a useful recent operational window.
_SIDEBAR_LATENCY_SAMPLE_LIMIT = 512


NativeProjectionCursor = tuple[float, str]
SidebarCandidateCursor = tuple[float, str]


def sidebar_terminal_evidence_digest(
    *,
    job: Mapping[str, Any],
    reservation: Mapping[str, Any],
) -> str:
    """Hash the exact durable snapshot and fixed provider proof."""

    thread_id = job.get("codex_thread_id")
    document = {
        "evidence_kind": SIDEBAR_TERMINAL_EVIDENCE_KIND,
        "evidence_version": SIDEBAR_TERMINAL_EVIDENCE_VERSION,
        "job": {
            key: job.get(key)
            for key in (
                "id",
                "idempotency_key",
                "source_session_id",
                "bridge_id",
                "state",
                "attempts",
                "next_attempt_at",
                "lease_digest",
                "lease_expires_at",
                "completion_digest",
                "codex_thread_id",
                "error_code",
                "eligible_at",
                "created_at",
                "updated_at",
                "visible_at",
            )
        },
        "provider_probe": [
            {
                "method": "thread/read",
                "params": {"threadId": thread_id, "includeTurns": True},
                "error": {
                    "code": -32600,
                    "message": f"thread not loaded: {thread_id}",
                },
            },
            {
                "method": "thread/resume",
                "params": {"threadId": thread_id},
                "error": {
                    "code": -32600,
                    "message": f"no rollout found for thread id {thread_id}",
                },
            },
        ],
        "reservation": {
            key: reservation.get(key)
            for key in (
                "version",
                "job_id",
                "source_session_id",
                "bridge_id",
                "recovery_key",
                "reserved_at",
            )
        },
        "resolution_code": SIDEBAR_TERMINAL_RESOLUTION_CODE,
    }
    encoded = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sidebar_precreate_terminal_evidence_digest(
    *,
    job: Mapping[str, Any],
    reservation: Mapping[str, Any],
    cutover: Mapping[str, Any],
    candidate: SidebarCandidate,
) -> str:
    """Hash one exact pre-cutover quarantine and both zero-result probes."""

    document = {
        "cutover": {
            key: cutover.get(key)
            for key in ("version", "applied_at", "quarantined_job_ids")
        },
        "candidate": {
            "source_session_id": candidate.source_session_id,
            "provider": candidate.provider.value,
            "bridge_id": candidate.bridge_id,
            "title": candidate.title,
            "cwd": candidate.cwd,
            "git_root": candidate.git_root,
            "git_branch": candidate.git_branch,
            "git_head": candidate.git_head,
            "worktree_id": candidate.worktree_id,
            "eligible_at": candidate.eligible_at,
        },
        "evidence_kind": SIDEBAR_PRECREATE_EVIDENCE_KIND,
        "evidence_version": SIDEBAR_PRECREATE_EVIDENCE_VERSION,
        "job": {
            key: job.get(key)
            for key in (
                "id",
                "idempotency_key",
                "source_session_id",
                "bridge_id",
                "state",
                "attempts",
                "next_attempt_at",
                "lease_digest",
                "lease_expires_at",
                "completion_digest",
                "codex_thread_id",
                "error_code",
                "eligible_at",
                "created_at",
                "updated_at",
                "visible_at",
            )
        },
        "provider_probe": [
            {"method": "signed_marker_inventory", "result": None},
            {"method": "recovery_key_inventory", "result": None},
        ],
        "reservation": {
            key: reservation.get(key)
            for key in (
                "version",
                "job_id",
                "source_session_id",
                "bridge_id",
                "recovery_key",
                "reserved_at",
            )
        },
        "resolution_code": SIDEBAR_PRECREATE_RESOLUTION_CODE,
    }
    encoded = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sidebar_unbound_terminal_evidence_digest(
    *,
    job: Mapping[str, Any],
    reservation: Mapping[str, Any],
    candidate: SidebarCandidate,
) -> str:
    """Hash one exact post-dispatch failure and both zero-result probes."""

    document = {
        "candidate": {
            "source_session_id": candidate.source_session_id,
            "provider": candidate.provider.value,
            "bridge_id": candidate.bridge_id,
            "title": candidate.title,
            "cwd": candidate.cwd,
            "git_root": candidate.git_root,
            "git_branch": candidate.git_branch,
            "git_head": candidate.git_head,
            "worktree_id": candidate.worktree_id,
            "eligible_at": candidate.eligible_at,
        },
        "evidence_kind": SIDEBAR_UNBOUND_EVIDENCE_KIND,
        "evidence_version": SIDEBAR_UNBOUND_EVIDENCE_VERSION,
        "job": {
            key: job.get(key)
            for key in (
                "id",
                "idempotency_key",
                "source_session_id",
                "bridge_id",
                "state",
                "attempts",
                "next_attempt_at",
                "lease_digest",
                "lease_expires_at",
                "completion_digest",
                "codex_thread_id",
                "error_code",
                "eligible_at",
                "created_at",
                "updated_at",
                "visible_at",
            )
        },
        "provider_probe": [
            {"method": "signed_marker_inventory", "result": None},
            {"method": "recovery_key_inventory", "result": None},
        ],
        "reservation": {
            key: reservation.get(key)
            for key in (
                "version",
                "job_id",
                "source_session_id",
                "bridge_id",
                "recovery_key",
                "reserved_at",
            )
        },
        "resolution_code": SIDEBAR_UNBOUND_RESOLUTION_CODE,
    }
    encoded = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sidebar_v2_attempt_zero_terminal_evidence_digest(
    *,
    job: Mapping[str, Any],
    reservation: Mapping[str, Any],
    proof: Mapping[str, Any],
    candidate: SidebarCandidate,
) -> str:
    """Hash the exact v2 reservation, bound proof, failure, and zero probes."""

    document = {
        "candidate": {
            "source_session_id": candidate.source_session_id,
            "provider": candidate.provider.value,
            "bridge_id": candidate.bridge_id,
            "title": candidate.title,
            "cwd": candidate.cwd,
            "git_root": candidate.git_root,
            "git_branch": candidate.git_branch,
            "git_head": candidate.git_head,
            "worktree_id": candidate.worktree_id,
            "eligible_at": candidate.eligible_at,
        },
        "evidence_kind": SIDEBAR_V2_ATTEMPT_ZERO_EVIDENCE_KIND,
        "evidence_version": SIDEBAR_V2_ATTEMPT_ZERO_EVIDENCE_VERSION,
        "job": {
            key: job.get(key)
            for key in (
                "id", "idempotency_key", "source_session_id", "bridge_id",
                "state", "attempts", "next_attempt_at", "lease_digest",
                "lease_expires_at", "completion_digest", "codex_thread_id",
                "error_code", "eligible_at", "created_at", "updated_at",
                "visible_at", "reconciliation_proof_digest",
            )
        },
        "provider_probe": [
            {"method": "signed_marker_inventory", "result": None},
            {"method": "recovery_key_inventory", "result": None},
        ],
        "reconciliation_proof": {
            key: proof.get(key)
            for key in (
                "proof_digest", "job_id", "source_session_id", "bridge_id",
                "marker_digest", "placement_generation", "delivery_generation",
                "reconciliation_generation", "completed_at", "expires_at",
                "inventory_digest", "state", "match_count",
                "recovered_thread_id", "fixed_reason", "created_at",
            )
        },
        "reservation": {
            key: reservation.get(key)
            for key in (
                "version", "job_id", "source_session_id", "bridge_id",
                "recovery_key", "reserved_at", "reconciliation_proof_digest",
                "reconciliation_generation",
            )
        },
        "resolution_code": SIDEBAR_V2_ATTEMPT_ZERO_RESOLUTION_CODE,
    }
    return hashlib.sha256(
        json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _claude_error_detail(value: object) -> str:
    from .context_pack import _redact

    if not isinstance(value, str):
        return "Claude visibility operation failed"
    compact = " ".join(_redact(value).split())
    return (compact or "Claude visibility operation failed")[:512]


def _canonical_snapshot_value(value: object) -> list[Any]:
    """Encode persisted SQLite/JSON values without lossy type coercion."""

    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        if math.isnan(value):
            encoded_float = "nan"
        elif math.isinf(value):
            encoded_float = "infinity" if value > 0 else "-infinity"
        else:
            encoded_float = value.hex()
        return ["float", encoded_float]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, bytes):
        return ["bytes", value.hex()]
    if isinstance(value, Mapping):
        items = [
            (_canonical_snapshot_value(key), _canonical_snapshot_value(item))
            for key, item in value.items()
        ]
        items.sort(
            key=lambda pair: json.dumps(
                pair[0], separators=(",", ":"), ensure_ascii=False
            )
        )
        return ["mapping", items]
    if isinstance(value, Sequence):
        return ["sequence", [_canonical_snapshot_value(item) for item in value]]
    raise TypeError(f"unsupported native snapshot value: {type(value).__name__}")


_NativeCopy = TypeVar("_NativeCopy")


def _dedupe_native_session_copies(
    items: Sequence[_NativeCopy],
    *,
    identity: Callable[[_NativeCopy], str],
    richness: Callable[[_NativeCopy], float] | None = None,
) -> list[_NativeCopy]:
    """Keep one copy per native Hermes session identity.

    One Hermes session legitimately lives in more than one database: the
    root/profile split writes the same session to this store's own database
    and to a profile database. Measured 2026-08-23 on the live box, 3,190 of
    the 5,543 sessions in ``profiles/main/state.db`` are also in the root
    ``state.db`` -- so this is the normal shape of the catalog, not damage.
    Every call site here used to raise ``duplicate native Hermes session
    identity across profiles`` instead, which took the whole lane down rather
    than one row: ``session_search`` failed on every query, ``session_get``
    failed on all 3,202 duplicated ids, and the sidebar candidate page failed
    above a limit of roughly 50.

    First occurrence wins, and ``_native_hermes_databases()`` yields this
    store's own database first, so the root copy is canonical on a tie. The
    two copies are frequently NOT identical -- one side often carries the
    transcript while the other holds a stub row -- so callers that return
    transcript content to a reader pass ``richness`` to keep the copy that
    actually has the messages. Callers whose result is joined back against
    root-owned tables (worktree snapshots, blocked ids, reconciliation
    proofs) leave ``richness`` unset and take the root copy unconditionally,
    because a profile copy would not resolve against those joins.
    """

    chosen: dict[str, _NativeCopy] = {}
    order: list[str] = []
    for item in items:
        key = identity(item)
        current = chosen.get(key)
        if current is None:
            chosen[key] = item
            order.append(key)
        elif richness is not None and richness(item) > richness(current):
            chosen[key] = item
    return [chosen[key] for key in order]


def _native_session_snapshot_identity(
    session_row: Mapping[str, Any],
    message_rows: Sequence[Mapping[str, Any]],
    *,
    decode_content: Callable[[Any], Any],
) -> dict[str, str]:
    session_payload = {key: session_row[key] for key in _NATIVE_SESSION_SNAPSHOT_FIELDS}
    messages_payload: list[dict[str, Any]] = []
    for row in message_rows:
        message = {key: row[key] for key in _NATIVE_MESSAGE_SNAPSHOT_FIELDS}
        message["content"] = decode_content(message.get("content"))
        messages_payload.append(message)
    canonical = _canonical_snapshot_value({
        "session": session_payload,
        "messages": messages_payload,
    })
    encoded = json.dumps(
        canonical,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    source_hash = hashlib.sha256(encoded).hexdigest()
    last_message_id = messages_payload[-1]["id"] if messages_payload else 0
    return {
        "cursor": (
            f"hermes:{len(messages_payload)}:{last_message_id}:{source_hash[:16]}"
        ),
        "source_hash": source_hash,
    }


class _MirrorWorkerFileLock:
    """Crash-releasing lock handle for mirror worker critical sections."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream: BinaryIO | None = stream

    def release(self) -> None:
        stream = self._stream
        if stream is None:
            return
        self._stream = None
        try:
            stream.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def _mirror_worker_lock_contended(exc: OSError) -> bool:
    if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
        return True
    return sys.platform == "win32" and getattr(exc, "winerror", None) in {
        32,
        33,
        36,
    }


class NativeProjectionPage(list[SessionProjection]):
    """A bounded newest-first page of minimal mirror-eligibility evidence."""

    def __init__(
        self,
        projections: Sequence[SessionProjection] = (),
        *,
        has_more: bool = False,
        next_cursor: NativeProjectionCursor | None = None,
    ) -> None:
        super().__init__(projections)
        self.has_more = has_more
        self.next_cursor = next_cursor


@dataclass(frozen=True)
class SidebarSource:
    source_session_id: str
    projection: SessionProjection
    git_root: str | None
    git_head: str | None
    worktree_id: str | None
    automation_only: bool
    subagent_only: bool
    indexed_at: float | None = None


class SidebarSourcePage(list[SidebarSource]):
    """A bounded newest-first page of sidebar-classification inputs."""

    def __init__(
        self,
        sources: Sequence[SidebarSource] = (),
        *,
        has_more: bool = False,
        next_cursor: SidebarCandidateCursor | None = None,
    ) -> None:
        super().__init__(sources)
        self.has_more = has_more
        self.next_cursor = next_cursor


class LocalSessionOwnsCanonicalId(ValueError):
    """A local, non-bridge session already materialises this canonical id.

    Hermes writes its own Codex-provider sessions to ``codex:<native_id>`` --
    the same namespace the bridge uses for imported native Codex threads -- so
    both systems can legitimately claim one id for the same underlying thread,
    materialised with different message representations.

    This is neither corruption nor a failed import. The local row holds
    authoritative content the bridge never wrote (delegation/heartbeat turns,
    thousands of messages), so it must never be adopted or overwritten. It is a
    known, benign condition: scans count it as an exclusion rather than a
    failure, because treating it as a failure degrades the provider and starves
    every downstream lane that depends on a healthy scan.
    """


class StaleExternalProjection(ValueError):
    """The incoming projection is older than the persisted activity watermark.

    ``_external_activity_state_key`` records the newest ``last_active`` ever
    projected for a session, and the importer refuses to move it backwards. The
    source can legitimately report a slightly EARLIER value on a later read --
    ``last_active`` is ``max(summary_last_active, *message_timestamps)``, so a
    read that returns fewer messages (pagination, a degraded app-server, a
    truncated summary) yields a smaller max. Measured 2026-08-13 on
    ``codex:019feec9-f523-…``: a 13-second regression.

    This is a no-op, not corruption: the projection simply has nothing newer to
    contribute. Typed (rather than a bare ValueError) so scans can count it as a
    skip instead of a failure -- the generic handler aborts the whole batch AND
    re-stages the id, so one non-monotonic session re-poisons the provider every
    cycle forever. Same reasoning, and the same remedy, as
    ``LocalSessionOwnsCanonicalId`` above.
    """


class SessionBridgeStore:
    """Transactional persistence for the cross-harness session bridge."""

    def __init__(
        self,
        db: SessionDB,
        *,
        clock: Callable[[], float] = time.time,
        sidebar_token_factory: Callable[[], str] | None = None,
        sidebar_jitter: Callable[[float], float] | None = None,
        local_timezone: tzinfo | None = None,
        claude_lease_factory: Callable[[], str] | None = None,
        hermes_profile_db_paths: Callable[[], Sequence[tuple[str, Path]]] | None = None,
    ) -> None:
        self.db = db
        self._clock = clock
        self._sidebar_token_factory = sidebar_token_factory or (
            lambda: secrets.token_urlsafe(32)
        )
        self._sidebar_jitter = sidebar_jitter or (
            lambda bound: random.uniform(0.0, bound)
        )
        if local_timezone is not None and not isinstance(local_timezone, tzinfo):
            raise TypeError("local_timezone must be a tzinfo or None")
        self._local_timezone = local_timezone
        self._claude_lease_factory = claude_lease_factory or (
            lambda: secrets.token_urlsafe(32)
        )
        self._hermes_profile_db_paths = (
            hermes_profile_db_paths or self._discover_hermes_profile_db_paths
        )
        # Reused read-only handles on the other profiles' databases, keyed by
        # resolved path -> (database, file identity, opened_at).
        self._profile_db_cache: dict[
            str, tuple[SessionDB, tuple[int, int], float]
        ] = {}
        self._profile_db_lock = threading.Lock()
        # Sidebar candidate rows per profile database, keyed by db path ->
        # (PRAGMA data_version, rows). See _profile_sidebar_candidate_rows.
        self._profile_candidate_cache: dict[
            str, tuple[int, list[dict[str, Any]]]
        ] = {}
        self._profile_candidate_lock = threading.Lock()

    def enqueue_claude_visibility_job(
        self,
        candidate: ClaudeVisibilityCandidate,
        identity: ClaudeVisibilityIdentity,
        marker_secret: bytes,
    ) -> dict[str, Any]:
        from .claude_visibility import (
            ClaudeVisibilityCandidate,
            ClaudeVisibilityIdentity,
            validate_claude_visibility_identity_binding,
        )

        if not isinstance(candidate, ClaudeVisibilityCandidate):
            raise TypeError("candidate must be a ClaudeVisibilityCandidate")
        if not isinstance(identity, ClaudeVisibilityIdentity):
            raise TypeError("identity must be a ClaudeVisibilityIdentity")
        validate_claude_visibility_identity_binding(candidate, identity, marker_secret)

        def _write(conn):
            now = _finite_number(self._clock(), "clock")
            row, _created = self._insert_claude_visibility_job(
                conn, candidate, identity, marker_secret, now
            )
            return row

        return self.db._execute_write(_write)

    def enqueue_claude_visibility_characterization(
        self,
        candidate: Any,
        identity: Any,
        marker_secret: bytes,
        *,
        operation_id: str,
        evidence_digest: str,
    ) -> dict[str, Any]:
        """Atomically enqueue and mark one disposable characterization job.

        Generic delivery excludes jobs with a registered characterization event.
        The job insert and that event therefore must share one transaction: neither
        a concurrent worker nor a crash may observe a launchable synthetic job.
        """

        from .claude_visibility import (
            ClaudeVisibilityCandidate,
            ClaudeVisibilityIdentity,
            validate_claude_visibility_identity_binding,
        )

        if not isinstance(candidate, ClaudeVisibilityCandidate):
            raise TypeError("candidate must be a ClaudeVisibilityCandidate")
        if not isinstance(identity, ClaudeVisibilityIdentity):
            raise TypeError("identity must be a ClaudeVisibilityIdentity")
        validate_claude_visibility_identity_binding(candidate, identity, marker_secret)
        normalized_operation = _exact_nonempty_text(
            operation_id, "Claude characterization operation ID"
        )
        if (
            re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                normalized_operation,
            )
            is None
        ):
            raise ValueError("Claude characterization operation ID is invalid")
        normalized_evidence = _exact_nonempty_text(
            evidence_digest, "Claude characterization evidence digest"
        )
        if re.fullmatch(r"[0-9a-f]{64}", normalized_evidence) is None:
            raise ValueError(
                "Claude characterization evidence digest must be lowercase SHA-256"
            )
        if (
            candidate.source_provider is not Provider.CODEX
            or candidate.source_session_id != f"codex:{normalized_operation}"
        ):
            raise ValueError("Claude characterization identity mismatch")

        def _write(conn: Any) -> dict[str, Any]:
            recorded_at = _finite_number(self._clock(), "clock")
            row, created = self._insert_claude_visibility_job(
                conn, candidate, identity, marker_secret, recorded_at
            )
            existing = conn.execute(
                """SELECT *
                   FROM session_claude_visibility_characterization_events
                   WHERE job_id = ? AND event_kind = 'registered'""",
                (identity.job_id,),
            ).fetchone()
            expected_event = {
                "operation_id": normalized_operation,
                "source_session_id": candidate.source_session_id,
                "bridge_id": identity.bridge_id,
                "idempotency_key": identity.idempotency_key,
                "reserved_claude_uuid": identity.claude_uuid,
            }
            if existing is not None:
                if any(existing[key] != value for key, value in expected_event.items()):
                    raise ValueError("Claude characterization identity mismatch")
                return {
                    "status": "registered",
                    "job_id": identity.job_id,
                    "reserved_claude_uuid": identity.claude_uuid,
                }
            if created:
                other_open = conn.execute(
                    """SELECT 1
                       FROM session_claude_visibility_jobs AS job
                       WHERE job.id != ?
                         AND job.operator_cleared_at IS NULL
                         AND job.state IN (
                             'claude_pending', 'claude_leased',
                             'claude_retry', 'claude_failed'
                         )
                         AND NOT EXISTS (
                             SELECT 1
                             FROM session_claude_visibility_characterization_events AS event
                             WHERE event.job_id = job.id
                               AND event.event_kind IN (
                                   'cleanup_completed', 'launch_aborted'
                               )
                         )
                       LIMIT 1""",
                    (identity.job_id,),
                ).fetchone()
                if other_open is not None:
                    raise ValueError("Claude characterization requires idle delivery")
            else:
                # Schema 26 could persist an exact signed job before the
                # append-only characterization ledger was introduced.  Binding
                # that pre-ledger row is protective (generic delivery excludes it)
                # and must never reset its attempt or lease history.  An active
                # lease is the sole unsafe ownership state; an expired lease can
                # be reconciled later by the exact-ID claimant.
                state = row["state"]
                recoverable_preledger = (
                    state in {"claude_retry", "claude_failed", "claude_visible"}
                    or (
                        state == "claude_pending"
                        and int(row["attempts"]) == 0
                        and row["lease_digest"] is None
                    )
                    or (
                        state == "claude_leased"
                        and row["lease_expires_at"] is not None
                        and float(row["lease_expires_at"]) <= recorded_at
                    )
                )
                if not recoverable_preledger:
                    raise ValueError("Claude characterization registration race")
            conn.execute(
                """INSERT INTO session_claude_visibility_characterization_events (
                       job_id, event_kind, operation_id, source_session_id,
                       bridge_id, idempotency_key, reserved_claude_uuid,
                       evidence_digest, created_at
                   ) VALUES (?, 'registered', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    identity.job_id,
                    normalized_operation,
                    candidate.source_session_id,
                    identity.bridge_id,
                    identity.idempotency_key,
                    identity.claude_uuid,
                    normalized_evidence,
                    recorded_at,
                ),
            )
            return {
                "status": "registered",
                "job_id": identity.job_id,
                "reserved_claude_uuid": identity.claude_uuid,
            }

        return self.db._execute_write(_write)

    def _insert_claude_visibility_job(
        self,
        conn: Any,
        candidate: Any,
        identity: Any,
        marker_secret: bytes,
        now: float,
    ) -> tuple[dict[str, Any], bool]:
        from .claude_visibility import validate_claude_visibility_identity_binding

        validate_claude_visibility_identity_binding(candidate, identity, marker_secret)
        collisions = conn.execute(
            """SELECT * FROM session_claude_visibility_jobs
                   WHERE source_session_id = ? OR bridge_id = ?
                      OR idempotency_key = ? OR reserved_claude_uuid = ?
                   ORDER BY id LIMIT 5""",
            (
                candidate.source_session_id,
                identity.bridge_id,
                identity.idempotency_key,
                identity.claude_uuid,
            ),
        ).fetchall()
        if collisions:
            if len(collisions) == 1:
                existing = dict(collisions[0])
                immutable = {
                    "id": identity.job_id,
                    "source_session_id": candidate.source_session_id,
                    "bridge_id": identity.bridge_id,
                    "idempotency_key": identity.idempotency_key,
                    "reserved_claude_uuid": identity.claude_uuid,
                    "native_name": candidate.native_name,
                    "source_provider": candidate.source_provider.value,
                    "source_cwd": candidate.source_cwd,
                    "git_root": candidate.git_root,
                    "git_branch": candidate.git_branch,
                    "git_head": candidate.git_head,
                    "worktree_id": candidate.worktree_id,
                    "signed_marker": identity.signed_marker,
                    "eligible_at": candidate.eligible_at,
                }
                if all(existing[key] == value for key, value in immutable.items()):
                    return existing, False
            raise ValueError("Claude visibility identity collision")
        try:
            conn.execute(
                """INSERT INTO session_claude_visibility_jobs (
                       id, source_session_id, bridge_id, idempotency_key,
                       reserved_claude_uuid, native_name, source_provider,
                       source_cwd, git_root, git_branch, git_head, worktree_id,
                       signed_marker, state, attempts, next_attempt_at,
                       eligible_at, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                 'claude_pending', 0, ?, ?, ?, ?)""",
                (
                    identity.job_id,
                    candidate.source_session_id,
                    identity.bridge_id,
                    identity.idempotency_key,
                    identity.claude_uuid,
                    candidate.native_name,
                    candidate.source_provider.value,
                    candidate.source_cwd,
                    candidate.git_root,
                    candidate.git_branch,
                    candidate.git_head,
                    candidate.worktree_id,
                    identity.signed_marker,
                    candidate.eligible_at,
                    candidate.eligible_at,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Claude visibility identity collision") from exc
        return dict(
            conn.execute(
                "SELECT * FROM session_claude_visibility_jobs WHERE id = ?",
                (identity.job_id,),
            ).fetchone()
        ), True

    def enqueue_claude_visibility_batch_if_idle(
        self, items: Sequence[tuple[Any, Any]], marker_secret: bytes
    ) -> dict[str, Any]:
        from .claude_visibility import (
            ClaudeVisibilityCandidate,
            ClaudeVisibilityIdentity,
            validate_claude_visibility_identity_binding,
        )

        batch = tuple(items)
        if len(batch) > 10:
            raise ValueError("Claude visibility batch cannot exceed 10")
        for item in batch:
            if not isinstance(item, tuple) or len(item) != 2:
                raise TypeError("Claude visibility batch item must be a pair")
            candidate, identity = item
            if not isinstance(candidate, ClaudeVisibilityCandidate):
                raise TypeError("candidate must be a ClaudeVisibilityCandidate")
            if not isinstance(identity, ClaudeVisibilityIdentity):
                raise TypeError("identity must be a ClaudeVisibilityIdentity")
            validate_claude_visibility_identity_binding(
                candidate, identity, marker_secret
            )

        def _write(conn):
            grouped = conn.execute(
                """SELECT state, error_code, COUNT(*) AS count
                     FROM session_claude_visibility_jobs AS job
                    WHERE job.operator_cleared_at IS NULL AND NOT EXISTS (
                        SELECT 1
                        FROM session_claude_visibility_characterization_events AS event
                        WHERE event.job_id = job.id
                          AND event.event_kind IN (
                              'cleanup_completed', 'launch_aborted'
                          )
                    )
                    GROUP BY state, error_code"""
            ).fetchall()
            known = {
                "claude_pending",
                "claude_leased",
                "claude_retry",
                "claude_visible",
                "claude_failed",
            }
            unknown = [row for row in grouped if row["state"] not in known]
            if unknown:
                return {
                    "status": "fatal",
                    "inserted": 0,
                    "duplicates": 0,
                    "fatal_reasons": ["unknown_job_state"],
                }
            fatal_reasons: set[str] = set()
            for row in grouped:
                code = row["error_code"]
                if row["state"] == "claude_retry" and code is not None:
                    if code not in CLAUDE_VISIBILITY_RETRY_CODES:
                        fatal_reasons.add("unknown_retry_code")
                if row["state"] == "claude_failed" and code is not None:
                    fatal_reasons.add(
                        code
                        if code in CLAUDE_VISIBILITY_FATAL_CODES
                        else "unknown_failed_code"
                    )
                if (
                    row["state"] in {"claude_pending", "claude_visible"}
                    and code is not None
                ):
                    fatal_reasons.add("unknown_error_code")
                if (
                    row["state"] == "claude_leased"
                    and code is not None
                    and code
                    not in (
                        CLAUDE_VISIBILITY_RETRY_CODES | CLAUDE_VISIBILITY_FATAL_CODES
                    )
                ):
                    fatal_reasons.add("unknown_error_code")
            if fatal_reasons:
                return {
                    "status": "fatal",
                    "inserted": 0,
                    "duplicates": 0,
                    "fatal_reasons": sorted(fatal_reasons),
                }
            if any(
                row["state"]
                in {"claude_pending", "claude_leased", "claude_retry", "claude_failed"}
                and int(row["count"]) > 0
                for row in grouped
            ):
                return {"status": "open_work", "inserted": 0, "duplicates": 0}
            now = _finite_number(self._clock(), "clock")
            inserted = 0
            duplicates = 0
            for candidate, identity in batch:
                _row, created = self._insert_claude_visibility_job(
                    conn, candidate, identity, marker_secret, now
                )
                inserted += int(created)
                duplicates += int(not created)
            return {
                "status": "inserted",
                "inserted": inserted,
                "duplicates": duplicates,
            }

        return self.db._execute_write(_write)

    def inspect_due_claude_visibility_reconciliation(
        self, now: float
    ) -> ClaudeVisibilityClaim:
        """Return immutable exact-ID lookup work without leasing or reserving cost."""

        from .claude_visibility import ClaudeVisibilityClaim

        inspection_time = _finite_number(now, "now")
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            operation_time = _finite_number(self._clock(), "clock")
            due = conn.execute(
                """SELECT * FROM session_claude_visibility_jobs AS job
                   WHERE error_detail IS NOT 'exact terminal reconciliation in progress' AND (
                       (
                           state = 'claude_retry'
                           AND NOT EXISTS (
                               SELECT 1
                               FROM session_claude_visibility_reconciliations r
                               WHERE r.job_id = job.id
                                 AND r.attempt_ordinal = job.attempts
                                 AND r.reserved_claude_uuid = job.reserved_claude_uuid
                                 AND r.outcome = 'absent'
                                 AND r.consumed_at IS NULL
                           )
                           AND next_attempt_at <= ?
                       ) OR (
                           state = 'claude_leased' AND lease_expires_at <= ?
                       )
                   )
                     AND NOT EXISTS (
                         SELECT 1
                         FROM session_claude_visibility_characterization_events AS event
                         WHERE event.job_id = job.id
                           AND event.event_kind = 'registered'
                     )
                   ORDER BY next_attempt_at, eligible_at, id LIMIT 1""",
                (inspection_time, operation_time),
            ).fetchone()
        if due is None:
            return ClaudeVisibilityClaim(status="no_due_job")
        prior_error = (
            "lease_expired" if due["state"] == "claude_leased" else due["error_code"]
        )
        return ClaudeVisibilityClaim(
            status="reconciliation_required",
            job_id=due["id"],
            source_session_id=due["source_session_id"],
            source_provider=Provider(due["source_provider"]),
            reserved_claude_uuid=due["reserved_claude_uuid"],
            native_name=due["native_name"],
            source_cwd=due["source_cwd"],
            git_root=due["git_root"],
            git_branch=due["git_branch"],
            git_head=due["git_head"],
            worktree_id=due["worktree_id"],
            signed_marker=due["signed_marker"],
            attempt_ordinal=int(due["attempts"]),
            prior_error_code=prior_error,
            requires_exact_id_reconciliation=True,
        )

    def claim_claude_visibility_reconciliation(
        self,
        now: float,
        lease_seconds: float,
        *,
        expected_job_id: str | None = None,
    ) -> ClaudeVisibilityClaim:
        """Lease exact-ID reconciliation without reserving launch budget."""

        from .claude_visibility import ClaudeVisibilityClaim

        claim_time = _finite_number(now, "now")
        lease_duration = _finite_number(lease_seconds, "lease_seconds")
        if lease_duration <= 0:
            raise ValueError("lease_seconds must be positive")
        expected_id = (
            None
            if expected_job_id is None
            else _exact_nonempty_text(
                expected_job_id, "expected Claude visibility job ID"
            )
        )

        def _write(conn):
            operation_time = _finite_number(self._clock(), "clock")
            if expected_id is None:
                conn.execute(
                    """UPDATE session_claude_visibility_jobs
                       SET state = 'claude_retry', next_attempt_at = ?,
                           lease_digest = NULL, lease_expires_at = NULL,
                           lease_kind = NULL,
                           error_code = 'lease_expired',
                           error_detail = 'active lease expired before completion',
                           updated_at = ?
                       WHERE state = 'claude_leased' AND lease_expires_at <= ?
                         AND error_detail IS NOT 'exact terminal reconciliation in progress'
                         AND NOT EXISTS (
                             SELECT 1
                             FROM session_claude_visibility_characterization_events AS event
                             WHERE event.job_id = session_claude_visibility_jobs.id
                               AND event.event_kind = 'registered'
                         )""",
                    (claim_time, operation_time, operation_time),
                )
            else:
                conn.execute(
                    """UPDATE session_claude_visibility_jobs
                       SET state = 'claude_retry', next_attempt_at = ?,
                           lease_digest = NULL, lease_expires_at = NULL,
                           lease_kind = NULL,
                           error_code = 'lease_expired',
                           error_detail = 'active lease expired before completion',
                           updated_at = ?
                       WHERE id = ? AND state = 'claude_leased'
                         AND lease_expires_at <= ?
                         AND error_detail IS NOT 'exact terminal reconciliation in progress'
                         AND NOT EXISTS (
                             SELECT 1
                             FROM session_claude_visibility_characterization_events AS event
                             WHERE event.job_id = session_claude_visibility_jobs.id
                               AND event.event_kind IN (
                                   'cleanup_completed', 'launch_aborted'
                               )
                         )""",
                    (claim_time, operation_time, expected_id, operation_time),
                )
                other_open = conn.execute(
                    """SELECT 1 FROM session_claude_visibility_jobs AS job
                       WHERE id != ? AND operator_cleared_at IS NULL AND state IN (
                           'claude_pending', 'claude_leased',
                           'claude_retry', 'claude_failed'
                       ) AND NOT EXISTS (
                           SELECT 1
                           FROM session_claude_visibility_characterization_events AS event
                           WHERE event.job_id = job.id
                             AND event.event_kind IN (
                                 'cleanup_completed', 'launch_aborted'
                             )
                       ) LIMIT 1""",
                    (expected_id,),
                ).fetchone()
                if other_open is not None:
                    return ClaudeVisibilityClaim(
                        status="not_sole_open_job", job_id=expected_id
                    )
            characterization_filter = (
                "event.event_kind = 'registered'"
                if expected_id is None
                else "event.event_kind IN ('cleanup_completed', 'launch_aborted')"
            )
            due = conn.execute(
                f"""SELECT * FROM session_claude_visibility_jobs AS job
                   WHERE (? IS NULL OR id = ?)
                      AND error_detail IS NOT 'exact terminal reconciliation in progress'
                      AND (
                          state = 'claude_retry'
                          OR (
                              ? IS NOT NULL
                              AND state = 'claude_pending'
                              AND attempts = 0
                              AND EXISTS (
                                  SELECT 1
                                  FROM session_claude_visibility_characterization_events AS registered
                                  WHERE registered.job_id = job.id
                                    AND registered.event_kind = 'registered'
                              )
                          )
                      )
                      AND next_attempt_at <= ?
                     AND NOT EXISTS (
                         SELECT 1
                         FROM session_claude_visibility_reconciliations r
                         WHERE r.job_id = job.id
                           AND r.attempt_ordinal = job.attempts
                           AND r.reserved_claude_uuid = job.reserved_claude_uuid
                           AND r.outcome = 'absent' AND r.consumed_at IS NULL
                     )
                     AND NOT EXISTS (
                         SELECT 1
                         FROM session_claude_visibility_characterization_events AS event
                         WHERE event.job_id = job.id
                           AND {characterization_filter}
                     )
                   ORDER BY next_attempt_at, eligible_at, id LIMIT 1""",
                (expected_id, expected_id, expected_id, claim_time),
            ).fetchone()
            if due is None:
                return ClaudeVisibilityClaim(status="no_due_job")
            return self._lease_claude_visibility_reconciliation(
                conn,
                due,
                claim_time=operation_time,
                lease_duration=lease_duration,
            )

        return self.db._execute_write(_write)

    def record_claude_visibility_exact_id_absent(
        self,
        job_id: str,
        lease_digest: str,
        reserved_claude_uuid: str,
        attempt_ordinal: int,
        evidence_digest: str,
    ) -> dict[str, Any]:
        """Persist exact reserved-UUID absence under a reconciliation lease."""

        normalized_job_id = _exact_nonempty_text(job_id, "Claude visibility job ID")
        normalized_lease = _exact_nonempty_text(
            lease_digest, "Claude visibility lease digest"
        )
        normalized_uuid = _exact_nonempty_text(
            reserved_claude_uuid, "reserved Claude UUID"
        )
        if (
            not isinstance(attempt_ordinal, int)
            or isinstance(attempt_ordinal, bool)
            or attempt_ordinal < 0
        ):
            raise ValueError("attempt ordinal must be a non-negative integer")
        evidence = _exact_nonempty_text(
            evidence_digest, "Claude reconciliation evidence digest"
        )
        if re.fullmatch(r"[0-9a-f]{64}", evidence) is None:
            raise ValueError(
                "Claude reconciliation evidence digest must be lowercase SHA-256"
            )

        def _write(conn):
            reconciled_at = _finite_number(self._clock(), "clock")
            inserted = conn.execute(
                """INSERT INTO session_claude_visibility_reconciliations (
                       job_id, reserved_claude_uuid, attempt_ordinal, outcome,
                       evidence_digest, checked_at, consumed_at
                   )
                   SELECT id, reserved_claude_uuid, attempts, 'absent', ?, ?, NULL
                   FROM session_claude_visibility_jobs
                   WHERE id = ? AND state = 'claude_leased'
                     AND lease_digest = ? AND lease_expires_at > ?
                     AND lease_kind = 'reconciliation'
                     AND error_detail IS NOT 'exact terminal reconciliation in progress'
                     AND reserved_claude_uuid = ? AND attempts = ?""",
                (
                    evidence,
                    reconciled_at,
                    normalized_job_id,
                    normalized_lease,
                    reconciled_at,
                    normalized_uuid,
                    attempt_ordinal,
                ),
            )
            if inserted.rowcount != 1:
                raise ValueError("exact active Claude reconciliation lease required")
            cursor = conn.execute(
                """UPDATE session_claude_visibility_jobs
                   SET state = 'claude_retry', next_attempt_at = ?,
                       lease_digest = NULL, lease_expires_at = NULL,
                       lease_kind = NULL, updated_at = ?
                   WHERE id = ? AND state = 'claude_leased'
                     AND lease_digest = ? AND lease_expires_at > ?
                     AND lease_kind = 'reconciliation'
                     AND error_detail IS NOT 'exact terminal reconciliation in progress'""",
                (
                    reconciled_at,
                    reconciled_at,
                    normalized_job_id,
                    normalized_lease,
                    reconciled_at,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("exact active Claude reconciliation lease required")
            return dict(
                conn.execute(
                    "SELECT * FROM session_claude_visibility_jobs WHERE id = ?",
                    (normalized_job_id,),
                ).fetchone()
            )

        return self.db._execute_write(_write)

    def claim_claude_visibility_job(
        self,
        now: float,
        lease_seconds: float,
        daily_limit: int,
        cost_limit: object,
        reserved_cost: object,
        max_attempts: int = 5,
        *,
        expected_job_id: str | None = None,
    ) -> ClaudeVisibilityClaim:
        from .claude_visibility import (
            ClaudeVisibilityClaim,
            canonical_usd,
            usd_microdollars,
        )

        claim_time = _finite_number(now, "now")
        lease_duration = _finite_number(lease_seconds, "lease_seconds")
        if lease_duration <= 0:
            raise ValueError("lease_seconds must be positive")
        if (
            not isinstance(daily_limit, int)
            or isinstance(daily_limit, bool)
            or daily_limit < 1
        ):
            raise ValueError("daily_limit must be a positive integer")
        # Defense-in-depth against config typos; the emergency cost gate is
        # the real spend bound. Raised from 25 on 2026-08-23: the old ceiling
        # starved account-switch catch-up days.
        if daily_limit > 100:
            raise ValueError("daily_limit cannot exceed 100")
        if (
            not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or max_attempts < 1
        ):
            raise ValueError("max_attempts must be a positive integer")
        maximum_cost = usd_microdollars(cost_limit, "cost_limit")
        attempt_cost = usd_microdollars(reserved_cost, "reserved_cost")
        expected_id = (
            None
            if expected_job_id is None
            else _exact_nonempty_text(
                expected_job_id, "expected Claude visibility job ID"
            )
        )

        def _write(conn):
            operation_time = _finite_number(self._clock(), "clock")
            local_day = self._claude_visibility_local_day(operation_time)
            if expected_id is None:
                conn.execute(
                    """UPDATE session_claude_visibility_jobs
                       SET state = 'claude_retry', next_attempt_at = ?,
                           lease_digest = NULL, lease_expires_at = NULL,
                           lease_kind = NULL,
                           error_code = 'lease_expired',
                           error_detail = 'active lease expired before completion',
                           updated_at = ?
                       WHERE state = 'claude_leased' AND lease_expires_at <= ?
                         AND error_detail IS NOT 'exact terminal reconciliation in progress'
                         AND NOT EXISTS (
                             SELECT 1
                             FROM session_claude_visibility_characterization_events AS event
                             WHERE event.job_id = session_claude_visibility_jobs.id
                               AND event.event_kind = 'registered'
                         )""",
                    (claim_time, operation_time, operation_time),
                )
            else:
                conn.execute(
                    """UPDATE session_claude_visibility_jobs
                       SET state = 'claude_retry', next_attempt_at = ?,
                           lease_digest = NULL, lease_expires_at = NULL,
                           lease_kind = NULL,
                           error_code = 'lease_expired',
                           error_detail = 'active lease expired before completion',
                           updated_at = ?
                       WHERE id = ? AND state = 'claude_leased'
                         AND lease_expires_at <= ?
                         AND error_detail IS NOT 'exact terminal reconciliation in progress'
                         AND NOT EXISTS (
                             SELECT 1
                             FROM session_claude_visibility_characterization_events AS event
                             WHERE event.job_id = session_claude_visibility_jobs.id
                               AND event.event_kind IN (
                                   'cleanup_completed', 'launch_aborted'
                               )
                         )""",
                    (claim_time, operation_time, expected_id, operation_time),
                )
                other_open = conn.execute(
                    """SELECT 1 FROM session_claude_visibility_jobs AS job
                       WHERE id != ? AND operator_cleared_at IS NULL AND state IN (
                           'claude_pending', 'claude_leased',
                           'claude_retry', 'claude_failed'
                       ) AND NOT EXISTS (
                           SELECT 1
                           FROM session_claude_visibility_characterization_events AS event
                           WHERE event.job_id = job.id
                             AND event.event_kind IN (
                                 'cleanup_completed', 'launch_aborted'
                             )
                       ) LIMIT 1""",
                    (expected_id,),
                ).fetchone()
                if other_open is not None:
                    return ClaudeVisibilityClaim(
                        status="not_sole_open_job", job_id=expected_id
                    )
            if expected_id is None:
                due = conn.execute(
                    """SELECT * FROM session_claude_visibility_jobs AS job
                       WHERE state IN ('claude_pending', 'claude_retry')
                         AND next_attempt_at <= ?
                         AND NOT EXISTS (
                             SELECT 1
                             FROM session_claude_visibility_characterization_events AS event
                             WHERE event.job_id = job.id
                               AND event.event_kind = 'registered'
                         )
                       ORDER BY next_attempt_at, eligible_at, id LIMIT 1""",
                    (claim_time,),
                ).fetchone()
            else:
                due = conn.execute(
                    """SELECT * FROM session_claude_visibility_jobs AS job
                       WHERE id = ?
                         AND state IN ('claude_pending', 'claude_retry')
                         AND next_attempt_at <= ?
                         AND NOT EXISTS (
                             SELECT 1
                             FROM session_claude_visibility_characterization_events AS event
                             WHERE event.job_id = job.id
                               AND event.event_kind IN (
                                   'cleanup_completed', 'launch_aborted'
                               )
                         ) LIMIT 1""",
                    (expected_id, claim_time),
                ).fetchone()
            if due is None:
                return ClaudeVisibilityClaim(status="no_due_job")
            fresh_pending = (
                due["state"] == "claude_pending" and int(due["attempts"]) == 0
            )
            absence = conn.execute(
                """SELECT rowid FROM session_claude_visibility_reconciliations
                   WHERE job_id = ? AND reserved_claude_uuid = ?
                     AND attempt_ordinal = ? AND outcome = 'absent'
                     AND consumed_at IS NULL
                   ORDER BY checked_at DESC LIMIT 1""",
                (due["id"], due["reserved_claude_uuid"], due["attempts"]),
            ).fetchone()
            launch_permitted = fresh_pending or absence is not None
            if not launch_permitted:
                return self._lease_claude_visibility_reconciliation(
                    conn,
                    due,
                    claim_time=operation_time,
                    lease_duration=lease_duration,
                )
            operator_recovery = (
                absence is not None
                and due["error_code"] == "creation_ambiguous"
                and due["error_detail"]
                == "operator authorized exact UUID reconciliation"
            )
            # session_claude_registration_usage is append-only and its
            # UNIQUE(job_id, attempt_ordinal) key does not carry local_day, so the
            # ledger -- not the mutable counter -- is the authority on how many paid
            # attempts a job has actually spent. A hand-repair that re-queues a job
            # can zero attempts while leaving the ledger intact; trusting the counter
            # then bypasses this guard and every later INSERT collides forever.
            max_ordinal_row = conn.execute(
                """SELECT MAX(attempt_ordinal) AS max_ordinal
                   FROM session_claude_registration_usage
                   WHERE job_id = ?""",
                (due["id"],),
            ).fetchone()
            max_ordinal = (
                0
                if max_ordinal_row is None or max_ordinal_row["max_ordinal"] is None
                else int(max_ordinal_row["max_ordinal"])
            )
            attempts_spent = max(int(due["attempts"]), max_ordinal)
            if attempts_spent >= max_attempts and not operator_recovery:
                cursor = conn.execute(
                    """UPDATE session_claude_visibility_jobs
                       SET state = 'claude_failed', attempts = ?,
                           lease_digest = NULL,
                           lease_expires_at = NULL, lease_kind = NULL,
                           error_code = 'max_attempts_exhausted',
                           error_detail = 'maximum paid launch attempts exhausted',
                           updated_at = ?
                       WHERE id = ? AND state = ? AND attempts = ?""",
                    (
                        attempts_spent,
                        operation_time,
                        due["id"],
                        due["state"],
                        due["attempts"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("stale Claude visibility exhaustion transition")
                return ClaudeVisibilityClaim(
                    status="max_attempts_exhausted", job_id=due["id"]
                )
            usage = conn.execute(
                """SELECT reserved_estimated_cost_usd
                   FROM session_claude_registration_usage
                   WHERE local_day = ?""",
                (local_day,),
            ).fetchall()
            if len(usage) >= daily_limit:
                return ClaudeVisibilityClaim(status="daily_limit")
            spent = sum(
                usd_microdollars(
                    row["reserved_estimated_cost_usd"],
                    "persisted reserved cost",
                )
                for row in usage
            )
            if spent + attempt_cost > maximum_cost:
                return ClaudeVisibilityClaim(status="cost_limit")

            lease_digest = hashlib.sha256(
                self._claude_lease_factory().encode("utf-8")
            ).hexdigest()
            if (
                conn.execute(
                    """SELECT 1 FROM session_claude_visibility_jobs
                   WHERE lease_digest = ? LIMIT 1""",
                    (lease_digest,),
                ).fetchone()
                is not None
            ):
                raise ValueError("Claude visibility lease factory returned a duplicate")
            attempt = attempts_spent + 1
            prior_error_code = due["error_code"]
            if absence is not None:
                consumed = conn.execute(
                    """UPDATE session_claude_visibility_reconciliations
                       SET consumed_at = ?
                       WHERE rowid = ? AND consumed_at IS NULL""",
                    (operation_time, absence["rowid"]),
                )
                if consumed.rowcount != 1:
                    raise ValueError("stale Claude reconciliation authorization")
            cursor = conn.execute(
                """UPDATE session_claude_visibility_jobs
                   SET state = 'claude_leased', attempts = ?, lease_digest = ?,
                       lease_expires_at = ?, lease_kind = 'launch', updated_at = ?
                   WHERE id = ? AND state = ? AND attempts = ?""",
                (
                    attempt,
                    lease_digest,
                    operation_time + lease_duration,
                    operation_time,
                    due["id"],
                    due["state"],
                    due["attempts"],
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("stale Claude visibility claim")
            conn.execute(
                """INSERT INTO session_claude_registration_usage (
                   local_day, job_id, attempt_ordinal,
                   reserved_estimated_cost_usd, reserved_at
                   ) VALUES (?, ?, ?, ?, ?)""",
                (
                    local_day,
                    due["id"],
                    attempt,
                    canonical_usd(attempt_cost),
                    operation_time,
                ),
            )
            return ClaudeVisibilityClaim(
                status="claimed",
                lease_kind="launch",
                job_id=due["id"],
                source_session_id=due["source_session_id"],
                source_provider=Provider(due["source_provider"]),
                reserved_claude_uuid=due["reserved_claude_uuid"],
                native_name=due["native_name"],
                source_cwd=due["source_cwd"],
                git_root=due["git_root"],
                git_branch=due["git_branch"],
                git_head=due["git_head"],
                worktree_id=due["worktree_id"],
                signed_marker=due["signed_marker"],
                lease_digest=lease_digest,
                attempt_ordinal=attempt,
                prior_error_code=prior_error_code,
                requires_exact_id_reconciliation=False,
                registration_reserved=True,
                launch_permitted=True,
            )

        return self.db._execute_write(_write)

    def _lease_claude_visibility_reconciliation(
        self,
        conn: Any,
        due: Any,
        *,
        claim_time: float,
        lease_duration: float,
    ) -> ClaudeVisibilityClaim:
        from .claude_visibility import ClaudeVisibilityClaim

        prior_error_code = due["error_code"]
        lease_digest = hashlib.sha256(
            self._claude_lease_factory().encode("utf-8")
        ).hexdigest()
        if (
            conn.execute(
                """SELECT 1 FROM session_claude_visibility_jobs
               WHERE lease_digest = ? LIMIT 1""",
                (lease_digest,),
            ).fetchone()
            is not None
        ):
            raise ValueError("Claude visibility lease factory returned a duplicate")
        cursor = conn.execute(
            """UPDATE session_claude_visibility_jobs
               SET state = 'claude_leased', lease_digest = ?,
                   lease_expires_at = ?,
                   lease_kind = 'reconciliation', updated_at = ?
               WHERE id = ? AND state = ? AND attempts = ?""",
            (
                lease_digest,
                claim_time + lease_duration,
                claim_time,
                due["id"],
                due["state"],
                due["attempts"],
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("stale Claude visibility reconciliation claim")
        return ClaudeVisibilityClaim(
            status="claimed",
            lease_kind="reconciliation",
            job_id=due["id"],
            source_session_id=due["source_session_id"],
            source_provider=Provider(due["source_provider"]),
            reserved_claude_uuid=due["reserved_claude_uuid"],
            native_name=due["native_name"],
            source_cwd=due["source_cwd"],
            git_root=due["git_root"],
            git_branch=due["git_branch"],
            git_head=due["git_head"],
            worktree_id=due["worktree_id"],
            signed_marker=due["signed_marker"],
            lease_digest=lease_digest,
            attempt_ordinal=int(due["attempts"]),
            prior_error_code=prior_error_code,
            requires_exact_id_reconciliation=True,
            registration_reserved=False,
            launch_permitted=False,
        )

    def retry_claude_visibility_job(
        self,
        job_id: str,
        lease_digest: str,
        error_code: str,
        next_attempt_at: float,
        detail: str,
    ) -> dict[str, Any]:
        from .claude_visibility import normalized_claude_visibility_error

        next_at = _finite_number(next_attempt_at, "next_attempt_at")
        normalized_code, retryable = normalized_claude_visibility_error(error_code)
        return self._finish_claude_visibility_lease(
            job_id=job_id,
            lease_digest=lease_digest,
            state="claude_retry" if retryable else "claude_failed",
            error_code=normalized_code,
            error_detail=_claude_error_detail(detail),
            next_attempt_at=next_at,
        )

    def claim_claude_auth_recovery(
        self,
        *,
        job_id: str,
        reserved_claude_uuid: str,
        operation_id: str,
        evidence_digest: str,
        prompt_digest: str,
        now: float,
        lease_seconds: float,
        daily_limit: int,
        cost_limit: object,
        reserved_cost: object,
        max_attempts: int,
    ) -> dict[str, Any]:
        """Lease one paid, same-UUID authentication recovery attempt."""

        from .claude_visibility import canonical_usd, usd_microdollars

        normalized_job = _exact_nonempty_text(job_id, "Claude visibility job ID")
        normalized_uuid = _exact_nonempty_text(
            reserved_claude_uuid, "reserved Claude UUID"
        )
        normalized_operation = _exact_nonempty_text(
            operation_id, "characterization operation ID"
        )
        evidence = _sha256_text(evidence_digest, "authentication evidence digest")
        prompt = _sha256_text(prompt_digest, "authentication recovery prompt digest")
        claim_time = _finite_number(now, "now")
        lease_duration = _finite_number(lease_seconds, "lease_seconds")
        if lease_duration <= 0:
            raise ValueError("lease_seconds must be positive")
        if (
            not isinstance(daily_limit, int)
            or isinstance(daily_limit, bool)
            or daily_limit < 1
        ):
            raise ValueError("daily_limit must be a positive integer")
        # Must track claim_claude_visibility_job's ceiling exactly: cli.py hands
        # both paths the same policy.daily_registration_limit, so a lower bound
        # here turns an operator-raised limit into a raw ValueError out of an
        # unguarded call site instead of a status. Raised from 25 on 2026-08-23.
        if daily_limit > 100:
            raise ValueError("daily_limit cannot exceed 100")
        if (
            not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or max_attempts < 1
        ):
            raise ValueError("max_attempts must be positive")
        maximum_cost = usd_microdollars(cost_limit, "cost_limit")
        attempt_cost = usd_microdollars(reserved_cost, "reserved_cost")

        def _write(conn):
            operation_time = _finite_number(self._clock(), "clock")
            local_day = self._claude_visibility_local_day(operation_time)
            job = conn.execute(
                """SELECT * FROM session_claude_visibility_jobs AS job
                   WHERE id = ? AND reserved_claude_uuid = ?
                     AND NOT EXISTS (
                         SELECT 1
                         FROM session_claude_visibility_characterization_events AS event
                         WHERE event.job_id = job.id
                           AND event.event_kind IN (
                               'cleanup_completed', 'launch_aborted'
                           )
                     )""",
                (normalized_job, normalized_uuid),
            ).fetchone()
            if job is None:
                raise ValueError("exact failed Claude visibility job required")
            other_open = conn.execute(
                """SELECT 1 FROM session_claude_visibility_jobs AS job
                   WHERE id != ? AND operator_cleared_at IS NULL AND state IN (
                       'claude_pending', 'claude_leased',
                       'claude_retry', 'claude_failed'
                   ) AND NOT EXISTS (
                       SELECT 1
                       FROM session_claude_visibility_characterization_events AS event
                       WHERE event.job_id = job.id
                         AND event.event_kind IN (
                             'cleanup_completed', 'launch_aborted'
                         )
                   ) LIMIT 1""",
                (normalized_job,),
            ).fetchone()
            if other_open is not None:
                return {"status": "not_sole_open_job", "job_id": normalized_job}
            recovery = conn.execute(
                "SELECT * FROM session_claude_auth_recoveries WHERE job_id = ?",
                (normalized_job,),
            ).fetchone()
            if recovery is not None:
                if (
                    recovery["reserved_claude_uuid"] != normalized_uuid
                    or recovery["operation_id"] != normalized_operation
                    or recovery["evidence_digest"] != evidence
                    or recovery["prompt_digest"] != prompt
                ):
                    raise ValueError("Claude authentication recovery identity conflict")
                if (
                    recovery["state"] == "leased"
                    and recovery["lease_expires_at"] <= operation_time
                ):
                    conn.execute(
                        """UPDATE session_claude_auth_recoveries
                           SET state = 'retry', next_attempt_at = ?,
                               lease_digest = NULL, lease_expires_at = NULL,
                               updated_at = ? WHERE job_id = ? AND state = 'leased'
                               AND lease_expires_at <= ?""",
                        (
                            claim_time,
                            operation_time,
                            normalized_job,
                            operation_time,
                        ),
                    )
                    recovery = conn.execute(
                        "SELECT * FROM session_claude_auth_recoveries WHERE job_id = ?",
                        (normalized_job,),
                    ).fetchone()
                if recovery["state"] == "completed":
                    return {"status": "completed", "job_id": normalized_job}
                if not (
                    (
                        job["state"] == "claude_failed"
                        and job["error_code"] == "bridge_conflict"
                    )
                    or (
                        job["state"] == "claude_retry"
                        and job["error_code"] == "claude_authentication_unavailable"
                    )
                ):
                    raise ValueError("exact failed Claude visibility job required")
                if (
                    recovery["state"] != "retry"
                    or recovery["next_attempt_at"] > claim_time
                ):
                    return {"status": "no_due_job", "job_id": normalized_job}
                attempt = int(recovery["attempt_ordinal"])
                if recovery["call_started_at"] is not None:
                    attempts = int(job["attempts"])
                    if attempts >= max_attempts:
                        return {
                            "status": "max_attempts_exhausted",
                            "job_id": normalized_job,
                        }
                    usage = conn.execute(
                        """SELECT reserved_estimated_cost_usd
                           FROM session_claude_registration_usage
                           WHERE local_day = ?""",
                        (local_day,),
                    ).fetchall()
                    spent = sum(
                        usd_microdollars(row[0], "persisted reserved cost")
                        for row in usage
                    )
                    if len(usage) >= daily_limit:
                        return {"status": "daily_limit", "job_id": normalized_job}
                    if spent + attempt_cost > maximum_cost:
                        return {"status": "cost_limit", "job_id": normalized_job}
                    attempt = attempts + 1
                    conn.execute(
                        """INSERT INTO session_claude_registration_usage (
                               local_day, job_id, attempt_ordinal,
                               reserved_estimated_cost_usd, reserved_at
                           ) VALUES (?, ?, ?, ?, ?)""",
                        (
                            local_day,
                            normalized_job,
                            attempt,
                            canonical_usd(attempt_cost),
                            operation_time,
                        ),
                    )
                    updated = conn.execute(
                        """UPDATE session_claude_visibility_jobs
                           SET attempts = ?, updated_at = ?
                           WHERE id = ? AND reserved_claude_uuid = ?
                             AND attempts = ? AND (
                               (state = 'claude_failed'
                                AND error_code = 'bridge_conflict')
                               OR (state = 'claude_retry'
                                   AND error_code =
                                       'claude_authentication_unavailable')
                             )""",
                        (
                            attempt,
                            operation_time,
                            normalized_job,
                            normalized_uuid,
                            attempts,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise ValueError("stale Claude authentication recovery")
                lease_digest = self._new_claude_auth_recovery_lease(conn)
                cursor = conn.execute(
                    """UPDATE session_claude_auth_recoveries
                       SET state = 'leased', lease_digest = ?,
                           lease_expires_at = ?, attempt_ordinal = ?,
                           call_started_at = NULL, updated_at = ?
                       WHERE job_id = ? AND state = 'retry'""",
                    (
                        lease_digest,
                        operation_time + lease_duration,
                        attempt,
                        operation_time,
                        normalized_job,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("stale Claude authentication recovery")
                return {
                    "status": "claimed",
                    "job_id": normalized_job,
                    "reserved_claude_uuid": normalized_uuid,
                    "lease_digest": lease_digest,
                    "attempt_ordinal": attempt,
                    "operation_id": normalized_operation,
                    "prompt_digest": prompt,
                    "source_cwd": job["source_cwd"],
                }
            if not (
                (
                    job["state"] == "claude_failed"
                    and job["error_code"] == "bridge_conflict"
                )
                or (
                    job["state"] == "claude_retry"
                    and job["error_code"] == "claude_authentication_unavailable"
                )
            ):
                raise ValueError("exact failed Claude visibility job required")
            attempts = int(job["attempts"])
            if attempts >= max_attempts:
                return {"status": "max_attempts_exhausted", "job_id": normalized_job}
            usage = conn.execute(
                """SELECT reserved_estimated_cost_usd
                   FROM session_claude_registration_usage WHERE local_day = ?""",
                (local_day,),
            ).fetchall()
            spent = sum(
                usd_microdollars(row[0], "persisted reserved cost") for row in usage
            )
            if len(usage) >= daily_limit:
                return {"status": "daily_limit", "job_id": normalized_job}
            if spent + attempt_cost > maximum_cost:
                return {"status": "cost_limit", "job_id": normalized_job}
            attempt = attempts + 1
            lease_digest = self._new_claude_auth_recovery_lease(conn)
            conn.execute(
                """INSERT INTO session_claude_registration_usage (
                       local_day, job_id, attempt_ordinal,
                       reserved_estimated_cost_usd, reserved_at
                   ) VALUES (?, ?, ?, ?, ?)""",
                (
                    local_day,
                    normalized_job,
                    attempt,
                    canonical_usd(attempt_cost),
                    operation_time,
                ),
            )
            updated = conn.execute(
                """UPDATE session_claude_visibility_jobs SET attempts = ?, updated_at = ?
                   WHERE id = ? AND reserved_claude_uuid = ?
                     AND ((state = 'claude_failed' AND error_code = 'bridge_conflict')
                          OR (state = 'claude_retry' AND error_code =
                              'claude_authentication_unavailable'))
                     AND attempts = ?""",
                (
                    attempt,
                    operation_time,
                    normalized_job,
                    normalized_uuid,
                    attempts,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("stale failed Claude visibility job")
            conn.execute(
                """INSERT INTO session_claude_auth_recoveries (
                       job_id, reserved_claude_uuid, operation_id,
                       evidence_digest, prompt_digest, state, attempt_ordinal,
                       next_attempt_at, lease_digest, lease_expires_at, call_started_at,
                       created_at, updated_at, completed_at
                   ) VALUES (?, ?, ?, ?, ?, 'leased', ?, ?, ?, ?, NULL, ?, ?, NULL)""",
                (
                    normalized_job,
                    normalized_uuid,
                    normalized_operation,
                    evidence,
                    prompt,
                    attempt,
                    claim_time,
                    lease_digest,
                    operation_time + lease_duration,
                    operation_time,
                    operation_time,
                ),
            )
            return {
                "status": "claimed",
                "job_id": normalized_job,
                "reserved_claude_uuid": normalized_uuid,
                "lease_digest": lease_digest,
                "attempt_ordinal": attempt,
                "operation_id": normalized_operation,
                "prompt_digest": prompt,
                "source_cwd": job["source_cwd"],
            }

        return self.db._execute_write(_write)

    def begin_claude_auth_recovery(
        self, job_id: str, lease_digest: str
    ) -> dict[str, Any]:
        """Durably mark that one paid same-UUID resume call is about to begin."""

        normalized_job = _exact_nonempty_text(job_id, "Claude visibility job ID")
        normalized_lease = _exact_nonempty_text(
            lease_digest, "Claude authentication recovery lease"
        )

        def _write(conn):
            operation_time = _finite_number(self._clock(), "clock")
            cursor = conn.execute(
                """UPDATE session_claude_auth_recoveries
                   SET call_started_at = ?, updated_at = ?
                   WHERE job_id = ? AND state = 'leased' AND lease_digest = ?
                     AND lease_expires_at > ? AND call_started_at IS NULL""",
                (
                    operation_time,
                    operation_time,
                    normalized_job,
                    normalized_lease,
                    operation_time,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    "exact unstarted Claude authentication recovery required"
                )
            return {"state": "leased", "call_started_at": operation_time}

        return self.db._execute_write(_write)

    def _new_claude_auth_recovery_lease(self, conn: Any) -> str:
        lease = hashlib.sha256(self._claude_lease_factory().encode("utf-8")).hexdigest()
        if (
            conn.execute(
                """SELECT 1 FROM session_claude_visibility_jobs WHERE lease_digest = ?
               UNION ALL SELECT 1 FROM session_claude_auth_recoveries
               WHERE lease_digest = ? LIMIT 1""",
                (lease, lease),
            ).fetchone()
            is not None
        ):
            raise ValueError("Claude visibility lease factory returned a duplicate")
        return lease

    def retry_claude_auth_recovery(
        self,
        job_id: str,
        lease_digest: str,
        error_code: str,
        next_attempt_at: float,
    ) -> dict[str, Any]:
        normalized_job = _exact_nonempty_text(job_id, "Claude visibility job ID")
        normalized_lease = _exact_nonempty_text(
            lease_digest, "Claude authentication recovery lease"
        )
        from .claude_visibility import normalized_claude_visibility_error

        normalized_code, retryable = normalized_claude_visibility_error(error_code)
        next_at = _finite_number(next_attempt_at, "next_attempt_at")

        def _write(conn):
            operation_time = _finite_number(self._clock(), "clock")
            if not retryable:
                recovery = conn.execute(
                    """SELECT * FROM session_claude_auth_recoveries
                       WHERE job_id = ? AND state = 'leased' AND lease_digest = ?
                         AND lease_expires_at > ? AND call_started_at IS NOT NULL""",
                    (normalized_job, normalized_lease, operation_time),
                ).fetchone()
                if recovery is None:
                    raise ValueError(
                        "exact active Claude authentication recovery required"
                    )
                failed = conn.execute(
                    """UPDATE session_claude_visibility_jobs
                       SET state = 'claude_failed', error_code = ?,
                           error_detail = 'authentication recovery terminal failure',
                           updated_at = ?
                       WHERE id = ? AND reserved_claude_uuid = ? AND attempts = ?
                         AND ((state = 'claude_failed'
                               AND error_code = 'bridge_conflict')
                              OR (state = 'claude_retry' AND error_code =
                                  'claude_authentication_unavailable'))""",
                    (
                        normalized_code,
                        operation_time,
                        normalized_job,
                        recovery["reserved_claude_uuid"],
                        recovery["attempt_ordinal"],
                    ),
                )
                if failed.rowcount != 1:
                    raise ValueError("exact failed Claude visibility job required")
                closed = conn.execute(
                    """UPDATE session_claude_auth_recoveries
                       SET state = 'completed', lease_digest = NULL,
                           lease_expires_at = NULL, completed_at = ?, updated_at = ?
                       WHERE job_id = ? AND state = 'leased' AND lease_digest = ?""",
                    (
                        operation_time,
                        operation_time,
                        normalized_job,
                        normalized_lease,
                    ),
                )
                if closed.rowcount != 1:
                    raise ValueError("stale Claude authentication recovery failure")
                return {"state": "failed", "error_code": normalized_code}
            cursor = conn.execute(
                """UPDATE session_claude_auth_recoveries
                   SET state = 'retry', next_attempt_at = ?, lease_digest = NULL,
                       lease_expires_at = NULL, updated_at = ?
                   WHERE job_id = ? AND state = 'leased' AND lease_digest = ?
                     AND lease_expires_at > ?""",
                (
                    next_at,
                    operation_time,
                    normalized_job,
                    normalized_lease,
                    operation_time,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("exact active Claude authentication recovery required")
            return {"state": "retry", "error_code": normalized_code}

        return self.db._execute_write(_write)

    def commit_claude_auth_recovery(
        self,
        *,
        job_id: str,
        lease_digest: str,
        reserved_claude_uuid: str,
        transcript_digest: str,
        visible_at: float,
    ) -> dict[str, Any]:
        normalized_job = _exact_nonempty_text(job_id, "Claude visibility job ID")
        normalized_lease = _exact_nonempty_text(
            lease_digest, "Claude authentication recovery lease"
        )
        normalized_uuid = _exact_nonempty_text(
            reserved_claude_uuid, "reserved Claude UUID"
        )
        transcript = _sha256_text(transcript_digest, "recovered transcript digest")
        timestamp = _finite_number(visible_at, "visible_at")

        def _write(conn):
            operation_time = _finite_number(self._clock(), "clock")
            recovery = conn.execute(
                """SELECT * FROM session_claude_auth_recoveries
                   WHERE job_id = ? AND reserved_claude_uuid = ?
                     AND state = 'leased' AND lease_digest = ?
                     AND lease_expires_at > ? AND call_started_at IS NOT NULL""",
                (
                    normalized_job,
                    normalized_uuid,
                    normalized_lease,
                    operation_time,
                ),
            ).fetchone()
            if recovery is None:
                raise ValueError("exact active Claude authentication recovery required")
            cursor = conn.execute(
                """UPDATE session_claude_visibility_jobs
                   SET state = 'claude_visible', completion_digest = ?, visible_at = ?,
                       error_code = NULL, error_detail = NULL, updated_at = ?
                   WHERE id = ? AND reserved_claude_uuid = ?
                     AND ((state = 'claude_failed' AND error_code = 'bridge_conflict')
                          OR (state = 'claude_retry' AND error_code =
                              'claude_authentication_unavailable'))
                     AND attempts = ?""",
                (
                    transcript,
                    timestamp,
                    operation_time,
                    normalized_job,
                    normalized_uuid,
                    recovery["attempt_ordinal"],
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("exact failed Claude visibility job required")
            completed = conn.execute(
                """UPDATE session_claude_auth_recoveries
                   SET state = 'completed', lease_digest = NULL,
                       lease_expires_at = NULL, completed_at = ?, updated_at = ?
                   WHERE job_id = ? AND state = 'leased' AND lease_digest = ?""",
                (timestamp, operation_time, normalized_job, normalized_lease),
            )
            if completed.rowcount != 1:
                raise ValueError("stale Claude authentication recovery completion")
            lineage = _finalize_claude_visibility_lineage_if_production(
                conn,
                job_id=normalized_job,
                created_at=operation_time,
                source_identity_issue=self._claude_lineage_source_identity_issue,
            )
            if lineage["state"] == "blocked" and lineage["code"] != (
                _CLAUDE_LINEAGE_TARGET_MISSING
            ):
                raise ValueError(str(lineage["code"]))
            return dict(
                conn.execute(
                    "SELECT * FROM session_claude_visibility_jobs WHERE id = ?",
                    (normalized_job,),
                ).fetchone()
            )

        return self.db._execute_write(_write)

    def reconcile_claude_auth_recovery(
        self,
        *,
        job_id: str,
        reserved_claude_uuid: str,
        operation_id: str,
        evidence_digest: str,
        prompt_digest: str,
        transcript_digest: str,
        visible_at: float,
    ) -> dict[str, Any]:
        """Commit an exact recovered transcript after a post-call process crash."""

        normalized_job = _exact_nonempty_text(job_id, "Claude visibility job ID")
        normalized_uuid = _exact_nonempty_text(
            reserved_claude_uuid, "reserved Claude UUID"
        )
        normalized_operation = _exact_nonempty_text(
            operation_id, "characterization operation ID"
        )
        evidence = _sha256_text(evidence_digest, "authentication evidence digest")
        prompt = _sha256_text(prompt_digest, "authentication recovery prompt digest")
        transcript = _sha256_text(transcript_digest, "recovered transcript digest")
        timestamp = _finite_number(visible_at, "visible_at")

        def _write(conn):
            operation_time = _finite_number(self._clock(), "clock")
            recovery = conn.execute(
                """SELECT * FROM session_claude_auth_recoveries
                   WHERE job_id = ? AND reserved_claude_uuid = ?
                     AND operation_id = ? AND evidence_digest = ?
                     AND prompt_digest = ?""",
                (
                    normalized_job,
                    normalized_uuid,
                    normalized_operation,
                    evidence,
                    prompt,
                ),
            ).fetchone()
            if recovery is None:
                raise ValueError(
                    "exact Claude authentication recovery authority required"
                )
            if recovery["state"] == "completed":
                completed_job = conn.execute(
                    """SELECT * FROM session_claude_visibility_jobs
                       WHERE id = ? AND reserved_claude_uuid = ?
                         AND state = 'claude_visible' AND completion_digest = ?
                         AND attempts = ?""",
                    (
                        normalized_job,
                        normalized_uuid,
                        transcript,
                        recovery["attempt_ordinal"],
                    ),
                ).fetchone()
                if completed_job is None:
                    raise ValueError(
                        "exact Claude authentication recovery authority required"
                    )
                lineage = _finalize_claude_visibility_lineage_if_production(
                    conn,
                    job_id=normalized_job,
                    created_at=operation_time,
                    source_identity_issue=self._claude_lineage_source_identity_issue,
                )
                if lineage["state"] == "blocked" and lineage["code"] != (
                    _CLAUDE_LINEAGE_TARGET_MISSING
                ):
                    raise ValueError(str(lineage["code"]))
                return dict(completed_job)
            if (
                recovery["state"] not in ("leased", "retry")
                or recovery["call_started_at"] is None
            ):
                raise ValueError(
                    "exact Claude authentication recovery authority required"
                )
            cursor = conn.execute(
                """UPDATE session_claude_visibility_jobs
                   SET state = 'claude_visible', completion_digest = ?, visible_at = ?,
                       error_code = NULL, error_detail = NULL, updated_at = ?
                   WHERE id = ? AND reserved_claude_uuid = ? AND attempts = ?
                     AND ((state = 'claude_failed' AND error_code = 'bridge_conflict')
                          OR (state = 'claude_retry' AND error_code =
                              'claude_authentication_unavailable'))""",
                (
                    transcript,
                    timestamp,
                    operation_time,
                    normalized_job,
                    normalized_uuid,
                    recovery["attempt_ordinal"],
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("exact failed Claude visibility job required")
            completed = conn.execute(
                """UPDATE session_claude_auth_recoveries
                   SET state = 'completed', lease_digest = NULL,
                       lease_expires_at = NULL, completed_at = ?, updated_at = ?
                   WHERE job_id = ? AND state IN ('leased', 'retry')
                     AND call_started_at IS NOT NULL""",
                (timestamp, operation_time, normalized_job),
            )
            if completed.rowcount != 1:
                raise ValueError("stale Claude authentication recovery completion")
            lineage = _finalize_claude_visibility_lineage_if_production(
                conn,
                job_id=normalized_job,
                created_at=operation_time,
                source_identity_issue=self._claude_lineage_source_identity_issue,
            )
            if lineage["state"] == "blocked" and lineage["code"] != (
                _CLAUDE_LINEAGE_TARGET_MISSING
            ):
                raise ValueError(str(lineage["code"]))
            return dict(
                conn.execute(
                    "SELECT * FROM session_claude_visibility_jobs WHERE id = ?",
                    (normalized_job,),
                ).fetchone()
            )

        return self.db._execute_write(_write)

    def fail_claude_visibility_job(
        self,
        job_id: str,
        lease_digest: str,
        error_code: str,
        detail: str,
    ) -> dict[str, Any]:
        from .claude_visibility import normalized_claude_visibility_error

        normalized_code, _retryable = normalized_claude_visibility_error(error_code)
        return self._finish_claude_visibility_lease(
            job_id=job_id,
            lease_digest=lease_digest,
            state="claude_failed",
            error_code=normalized_code,
            error_detail=_claude_error_detail(detail),
            next_attempt_at=None,
        )

    def inspect_failed_claude_visibility_reconciliation(
        self,
        *,
        expected_job_id: str,
        expected_reserved_claude_uuid: str,
        expected_error_code: str,
    ) -> dict[str, Any]:
        """Inspect one exact sole terminal failure without acquiring authority."""

        normalized_job = _exact_nonempty_text(
            expected_job_id, "expected Claude visibility job ID"
        )
        normalized_uuid = _exact_nonempty_text(
            expected_reserved_claude_uuid, "expected reserved Claude UUID"
        )
        normalized_code = _exact_nonempty_text(
            expected_error_code, "expected Claude visibility error code"
        )
        if normalized_code != "bridge_conflict":
            raise ValueError("terminal repair requires bridge_conflict")
        operation_time = _finite_number(self._clock(), "clock")
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            # This must accept exactly what
            # claim_failed_claude_visibility_reconciliation accepts, including an
            # ABANDONED repair lease.  It backs the CLI's --dry-run, and a preview
            # that refuses what --apply would take reports the operation impossible
            # immediately before it succeeds -- measured live 2026-08-25, where it
            # sent an operator looking for a nonexistent escape hatch.
            row = conn.execute(
                """SELECT id, reserved_claude_uuid, error_code, attempts
                   FROM session_claude_visibility_jobs
                   WHERE id = ? AND reserved_claude_uuid = ?
                     AND error_code = ? AND operator_cleared_at IS NULL
                     AND (
                         state = 'claude_failed' OR (
                             state = 'claude_leased'
                             AND lease_kind = 'reconciliation'
                             AND lease_expires_at <= ?
                             AND error_detail =
                                 'exact terminal reconciliation in progress'
                         )
                     )""",
                (
                    normalized_job,
                    normalized_uuid,
                    normalized_code,
                    operation_time,
                ),
            ).fetchone()
            if row is None:
                raise ValueError("exact failed Claude visibility job required")
            other_open = conn.execute(
                """SELECT 1 FROM session_claude_visibility_jobs AS job
                   WHERE id != ? AND operator_cleared_at IS NULL AND state IN (
                       'claude_pending', 'claude_leased',
                       'claude_retry', 'claude_failed'
                   ) AND NOT EXISTS (
                       SELECT 1
                       FROM session_claude_visibility_characterization_events AS event
                       WHERE event.job_id = job.id
                         AND event.event_kind IN (
                             'cleanup_completed', 'launch_aborted'
                         )
                   ) LIMIT 1""",
                (normalized_job,),
            ).fetchone()
        if other_open is not None:
            raise ValueError("sole open Claude visibility job required")
        return {
            "status": "repairable",
            "job_id": row["id"],
            "reserved_claude_uuid": row["reserved_claude_uuid"],
            "error_code": row["error_code"],
            "attempts": int(row["attempts"]),
        }

    def claim_failed_claude_visibility_reconciliation(
        self,
        now: float,
        lease_seconds: float,
        *,
        expected_job_id: str,
        expected_reserved_claude_uuid: str,
        expected_error_code: str,
    ) -> ClaudeVisibilityClaim:
        """Lease one exact terminal failure for native-only reconciliation.

        The bounded detail marker keeps an abandoned repair lease out of the
        ordinary expiry/reclaim paths. Only this exact guarded API may replace
        that repair authority; a failed apply therefore stays fail-closed.
        """

        _finite_number(now, "now")
        lease_duration = _finite_number(lease_seconds, "lease_seconds")
        if lease_duration <= 0:
            raise ValueError("lease_seconds must be positive")
        normalized_job = _exact_nonempty_text(
            expected_job_id, "expected Claude visibility job ID"
        )
        normalized_uuid = _exact_nonempty_text(
            expected_reserved_claude_uuid, "expected reserved Claude UUID"
        )
        normalized_code = _exact_nonempty_text(
            expected_error_code, "expected Claude visibility error code"
        )
        if normalized_code != "bridge_conflict":
            raise ValueError("terminal repair requires bridge_conflict")

        def _write(conn):
            operation_time = _finite_number(self._clock(), "clock")
            due = conn.execute(
                """SELECT * FROM session_claude_visibility_jobs
                   WHERE id = ? AND reserved_claude_uuid = ?
                     AND error_code = ? AND operator_cleared_at IS NULL
                     AND (
                         state = 'claude_failed' OR (
                             state = 'claude_leased'
                             AND lease_kind = 'reconciliation'
                             AND lease_expires_at <= ?
                             AND error_detail =
                                 'exact terminal reconciliation in progress'
                         )
                     )""",
                (
                    normalized_job,
                    normalized_uuid,
                    normalized_code,
                    operation_time,
                ),
            ).fetchone()
            if due is None:
                raise ValueError("exact failed Claude visibility job required")
            other_open = conn.execute(
                """SELECT 1 FROM session_claude_visibility_jobs AS job
                   WHERE id != ? AND operator_cleared_at IS NULL AND state IN (
                       'claude_pending', 'claude_leased',
                       'claude_retry', 'claude_failed'
                   ) AND NOT EXISTS (
                       SELECT 1
                       FROM session_claude_visibility_characterization_events AS event
                       WHERE event.job_id = job.id
                         AND event.event_kind IN (
                             'cleanup_completed', 'launch_aborted'
                         )
                   ) LIMIT 1""",
                (normalized_job,),
            ).fetchone()
            if other_open is not None:
                raise ValueError("sole open Claude visibility job required")
            lease = self._lease_claude_visibility_reconciliation(
                conn,
                due,
                claim_time=operation_time,
                lease_duration=lease_duration,
            )
            conn.execute(
                """UPDATE session_claude_visibility_jobs
                   SET error_detail = 'exact terminal reconciliation in progress'
                   WHERE id = ? AND state = 'claude_leased'
                     AND lease_digest = ?""",
                (normalized_job, lease.lease_digest),
            )
            return lease

        return self.db._execute_write(_write)

    def requeue_failed_claude_visibility_reconciliation(
        self, job_id: str, reserved_claude_uuid: str
    ) -> dict[str, Any]:
        """Repair an exact reviewed failure without changing its native UUID."""

        normalized_job = _exact_nonempty_text(job_id, "Claude visibility job ID")
        normalized_uuid = _exact_nonempty_text(
            reserved_claude_uuid, "reserved Claude UUID"
        )

        def _write(conn):
            operation_time = _finite_number(self._clock(), "clock")
            other_open = conn.execute(
                """SELECT 1 FROM session_claude_visibility_jobs AS job
                   WHERE id != ? AND operator_cleared_at IS NULL AND state IN (
                       'claude_pending', 'claude_leased',
                       'claude_retry', 'claude_failed'
                   ) AND NOT EXISTS (
                       SELECT 1
                       FROM session_claude_visibility_characterization_events AS event
                       WHERE event.job_id = job.id
                         AND event.event_kind IN (
                             'cleanup_completed', 'launch_aborted'
                         )
                   ) LIMIT 1""",
                (normalized_job,),
            ).fetchone()
            if other_open is not None:
                raise ValueError("exact failed Claude visibility job required")
            cursor = conn.execute(
                """UPDATE session_claude_visibility_jobs
                   SET state = 'claude_retry', next_attempt_at = ?,
                       lease_digest = NULL, lease_expires_at = NULL,
                       lease_kind = NULL,
                       error_code = 'creation_ambiguous',
                       error_detail =
                           'operator authorized exact UUID reconciliation',
                       updated_at = ?
                   WHERE id = ? AND reserved_claude_uuid = ?
                     AND state = 'claude_failed'
                     AND (
                         (error_code = 'bridge_conflict'
                          AND error_detail IN (
                              'registration response malformed',
                              'exact transcript conflict'
                          ))
                         OR
                         (error_code = 'max_attempts_exhausted'
                          AND error_detail =
                              'maximum paid launch attempts exhausted')
                     )
                     AND attempts > 0""",
                (
                    operation_time,
                    operation_time,
                    normalized_job,
                    normalized_uuid,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("exact failed Claude visibility job required")
            return dict(
                conn.execute(
                    "SELECT * FROM session_claude_visibility_jobs WHERE id = ?",
                    (normalized_job,),
                ).fetchone()
            )

        return self.db._execute_write(_write)

    def commit_claude_visibility_job(
        self,
        job_id: str,
        lease_digest: str,
        transcript_digest: str,
        visible_at: float,
    ) -> dict[str, Any]:
        normalized_job_id = _exact_nonempty_text(job_id, "Claude visibility job ID")
        normalized_lease = _exact_nonempty_text(
            lease_digest, "Claude visibility lease digest"
        )
        completion = _sha256_text(transcript_digest, "transcript digest")
        timestamp = _finite_number(visible_at, "visible_at")

        def _write(conn):
            operation_time = _finite_number(self._clock(), "clock")
            active = conn.execute(
                """SELECT * FROM session_claude_visibility_jobs
                   WHERE id = ? AND state = 'claude_leased'
                     AND lease_digest = ? AND lease_expires_at > ?""",
                (normalized_job_id, normalized_lease, operation_time),
            ).fetchone()
            if active is None:
                raise ValueError("exact active Claude visibility lease required")
            if active["lease_kind"] == "reconciliation":
                conn.execute(
                    """INSERT INTO session_claude_visibility_reconciliations (
                           job_id, reserved_claude_uuid, attempt_ordinal, outcome,
                           evidence_digest, checked_at, consumed_at
                       ) VALUES (?, ?, ?, 'exact_match', ?, ?, NULL)""",
                    (
                        normalized_job_id,
                        active["reserved_claude_uuid"],
                        active["attempts"],
                        hashlib.sha256(completion.encode("utf-8")).hexdigest(),
                        operation_time,
                    ),
                )
            cursor = conn.execute(
                """UPDATE session_claude_visibility_jobs
                   SET state = 'claude_visible', lease_digest = NULL,
                       lease_expires_at = NULL, lease_kind = NULL,
                       completion_digest = ?,
                       error_code = NULL, error_detail = NULL,
                       visible_at = ?, updated_at = ?
                   WHERE id = ? AND state = 'claude_leased'
                     AND lease_digest = ? AND lease_expires_at > ?""",
                (
                    completion,
                    timestamp,
                    operation_time,
                    normalized_job_id,
                    normalized_lease,
                    operation_time,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("exact active Claude visibility lease required")
            lineage = _finalize_claude_visibility_lineage_if_production(
                conn,
                job_id=normalized_job_id,
                created_at=operation_time,
                source_identity_issue=self._claude_lineage_source_identity_issue,
            )
            if lineage["state"] == "blocked" and lineage["code"] != (
                _CLAUDE_LINEAGE_TARGET_MISSING
            ):
                raise ValueError(str(lineage["code"]))
            return dict(
                conn.execute(
                    "SELECT * FROM session_claude_visibility_jobs WHERE id = ?",
                    (normalized_job_id,),
                ).fetchone()
            )

        return self.db._execute_write(_write)

    def _finish_claude_visibility_lease(
        self,
        *,
        job_id: str,
        lease_digest: str,
        state: str,
        error_code: str,
        error_detail: str,
        next_attempt_at: float | None,
    ) -> dict[str, Any]:
        normalized_job_id = _exact_nonempty_text(job_id, "Claude visibility job ID")
        normalized_lease = _exact_nonempty_text(
            lease_digest, "Claude visibility lease digest"
        )

        def _write(conn):
            updated_at = _finite_number(self._clock(), "clock")
            active = conn.execute(
                """SELECT * FROM session_claude_visibility_jobs
                   WHERE id = ? AND state = 'claude_leased'
                     AND lease_digest = ? AND lease_expires_at > ?
                     AND error_detail IS NOT 'exact terminal reconciliation in progress'""",
                (normalized_job_id, normalized_lease, updated_at),
            ).fetchone()
            if active is None:
                raise ValueError("exact active Claude visibility lease required")
            if error_code == "uuid_conflict":
                if active["lease_kind"] != "reconciliation":
                    raise ValueError(
                        "UUID conflict requires a Claude reconciliation lease"
                    )
                conn.execute(
                    """INSERT INTO session_claude_visibility_reconciliations (
                           job_id, reserved_claude_uuid, attempt_ordinal, outcome,
                           evidence_digest, checked_at, consumed_at
                       ) VALUES (?, ?, ?, 'conflict', ?, ?, NULL)""",
                    (
                        normalized_job_id,
                        active["reserved_claude_uuid"],
                        active["attempts"],
                        hashlib.sha256(error_detail.encode("utf-8")).hexdigest(),
                        updated_at,
                    ),
                )
            cursor = conn.execute(
                """UPDATE session_claude_visibility_jobs
                   SET state = ?, next_attempt_at = COALESCE(?, next_attempt_at),
                       lease_digest = NULL, lease_expires_at = NULL,
                       lease_kind = NULL,
                       error_code = ?, error_detail = ?, updated_at = ?
                   WHERE id = ? AND state = 'claude_leased'
                     AND lease_digest = ? AND lease_expires_at > ?""",
                (
                    state,
                    next_attempt_at,
                    error_code,
                    error_detail,
                    updated_at,
                    normalized_job_id,
                    normalized_lease,
                    updated_at,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("exact active Claude visibility lease required")
            return dict(
                conn.execute(
                    "SELECT * FROM session_claude_visibility_jobs WHERE id = ?",
                    (normalized_job_id,),
                ).fetchone()
            )

        return self.db._execute_write(_write)

    def dismiss_claude_visibility_job(
        self, *, job_id: str, expected_error_code: str
    ) -> dict[str, Any]:
        """Retire one terminally failed job so the gates stop counting it open.

        A claude_failed job fail-closes discovery by design: the state is a
        member of the coordinator's open set and its error_code is promoted
        into the fatal set, so run_once skips discovery until an operator
        adjudicates it. There is no honest state to move such a job to --
        rows here are delete-guarded, and the only other terminal state,
        'claude_visible', requires a completion digest and would assert a
        registration that never happened. So the acknowledgement is recorded
        as a stamp BESIDE the verdict rather than overwriting it: state,
        attempts, error_code and error_detail all survive verbatim.

        The paid-attempt ledger in session_claude_registration_usage is never
        touched. Those rows are the cost record of real spend, and
        UNIQUE(job_id, attempt_ordinal) is precisely what stops a re-queued
        job from re-spending ordinals it has already used -- deleting them is
        what re-arms the livelock this stamp exists to end.

        *expected_error_code* must match the failure on the row, so an
        operator cannot clear a job whose verdict changed since they looked.
        """

        normalized_job = _exact_nonempty_text(job_id, "Claude visibility job ID")
        normalized_code = _exact_nonempty_text(
            expected_error_code, "Claude visibility error code"
        )

        def _write(conn):
            operation_time = _finite_number(self._clock(), "clock")
            cursor = conn.execute(
                """UPDATE session_claude_visibility_jobs
                   SET operator_cleared_at = ?, updated_at = ?
                   WHERE id = ? AND state = 'claude_failed' AND error_code = ?
                     AND operator_cleared_at IS NULL""",
                (operation_time, operation_time, normalized_job, normalized_code),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    "exact terminally failed Claude visibility job required"
                )
            return {
                "status": "dismissed",
                "job_id": normalized_job,
                "error_code": normalized_code,
                "operator_cleared_at": operation_time,
            }

        return self.db._execute_write(_write)

    def claude_visibility_status(self, now: float) -> dict[str, Any]:
        status_time = _finite_number(now, "now")
        local_day = self._claude_visibility_local_day(status_time)
        states = (
            "claude_pending",
            "claude_leased",
            "claude_retry",
            "claude_visible",
            "claude_failed",
        )
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            status_rows = conn.execute(
                """SELECT id, reserved_claude_uuid, state, error_code,
                          error_detail, lease_expires_at
                   FROM session_claude_visibility_jobs AS job
                   WHERE job.operator_cleared_at IS NULL AND NOT EXISTS (
                       SELECT 1
                       FROM session_claude_visibility_characterization_events AS event
                       WHERE event.job_id = job.id
                         AND event.event_kind IN (
                             'cleanup_completed', 'launch_aborted'
                         )
                   )
                   ORDER BY state, error_code, id"""
            ).fetchall()
            usage_rows = conn.execute(
                """SELECT reserved_estimated_cost_usd
                   FROM session_claude_registration_usage
                   WHERE local_day = ?""",
                (local_day,),
            ).fetchall()
            cycle_row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?",
                (_CLAUDE_VISIBILITY_CYCLE_STATE_KEY,),
            ).fetchone()
            characterization_rows = conn.execute(
                """SELECT job.id AS job_id, job.state AS state
                   FROM session_claude_visibility_jobs AS job
                   JOIN session_claude_visibility_characterization_events AS registered
                     ON registered.job_id = job.id
                    AND registered.event_kind = 'registered'
                   WHERE NOT EXISTS (
                       SELECT 1
                       FROM session_claude_visibility_characterization_events AS terminal
                       WHERE terminal.job_id = job.id
                         AND terminal.event_kind IN (
                             'cleanup_completed', 'launch_aborted'
                         )
                   )
                   ORDER BY job.id"""
            ).fetchall()
            lineage = _claude_visibility_lineage_status(
                conn,
                source_identity_issue=self._claude_lineage_source_identity_issue,
            )
        counts = {state: 0 for state in states}
        retry_codes: dict[str, int] = {}
        failed_codes: dict[str, int] = {}
        fatal_groups: dict[tuple[str, str | None, str | None], int] = {}
        repair_required: list[dict[str, Any]] = []

        def add_fatal(
            code: str, state: object, error_code: object, *, count: int = 1
        ) -> None:
            safe_state = _claude_status_token(state)
            safe_error = _claude_status_token(error_code, optional=True)
            key = (code, safe_state, safe_error)
            fatal_groups[key] = fatal_groups.get(key, 0) + count

        for row in status_rows:
            literal_state = row["state"]
            if literal_state not in counts:
                add_fatal("unknown_job_state", literal_state, row["error_code"])
                continue

            error_code = row["error_code"]
            if literal_state == "claude_leased":
                if (
                    error_code is not None
                    and error_code not in CLAUDE_VISIBILITY_RETRY_CODES
                ):
                    counts[literal_state] += 1
                    if (
                        error_code == "bridge_conflict"
                        and row["error_detail"]
                        == "exact terminal reconciliation in progress"
                    ):
                        # The detail marker excludes this row from every ordinary
                        # reclaim path, so an expired repair lease is not "in
                        # progress" -- it is waiting on an operator, and it waits
                        # forever unless this projection says so out loud.  A NULL
                        # expiry can never expire and is therefore also abandoned;
                        # reporting is authority-free, so naming it cannot free it.
                        lease_expires_at = row["lease_expires_at"]
                        abandoned = (
                            lease_expires_at is None
                            or float(lease_expires_at) <= status_time
                        )
                        add_fatal(
                            "reconciliation_repair_abandoned"
                            if abandoned
                            else "reconciliation_repair_active",
                            literal_state,
                            error_code,
                        )
                        if abandoned:
                            repair_required.append(
                                {
                                    "job_id": row["id"],
                                    "reserved_claude_uuid": row[
                                        "reserved_claude_uuid"
                                    ],
                                    "error_code": error_code,
                                }
                            )
                    else:
                        add_fatal("unknown_error_code", literal_state, error_code)
                    continue
                lease_expires_at = row["lease_expires_at"]
                if (
                    lease_expires_at is not None
                    and float(lease_expires_at) <= status_time
                ):
                    counts["claude_retry"] += 1
                    retry_codes["lease_expired"] = (
                        retry_codes.get("lease_expired", 0) + 1
                    )
                else:
                    counts[literal_state] += 1
                continue

            counts[literal_state] += 1
            if error_code is None:
                continue
            safe_code = _claude_status_token(error_code)
            assert safe_code is not None
            if literal_state == "claude_retry":
                retry_codes[safe_code] = retry_codes.get(safe_code, 0) + 1
            elif literal_state == "claude_failed":
                failed_codes[safe_code] = failed_codes.get(safe_code, 0) + 1
            else:
                add_fatal("unknown_error_code", literal_state, error_code)

        fatal = [
            {
                "code": code,
                "state": state,
                "error_code": error_code,
                "count": count,
            }
            for (code, state, error_code), count in fatal_groups.items()
        ]
        total_cost = sum(
            (Decimal(row["reserved_estimated_cost_usd"]) for row in usage_rows),
            Decimal("0"),
        )
        cycle = _decode_claude_visibility_cycle_state(
            cycle_row["value_json"] if cycle_row is not None else None
        )
        return {
            "counts": counts,
            "retry_codes": retry_codes,
            "failed_codes": failed_codes,
            "usage": {
                "local_day": local_day,
                "attempts": len(usage_rows),
                "reserved_cost_usd": str(total_cost),
            },
            "fatal": fatal,
            "repair_required": repair_required,
            "lineage": lineage,
            "characterizations": [
                {"job_id": row["job_id"], "state": row["state"]}
                for row in characterization_rows
            ],
            **_public_claude_visibility_cycle_state(cycle),
        }

    def reconcile_claude_visibility_lineage(
        self,
        *,
        limit: int,
        marker_secret: bytes,
        apply: bool = False,
        cursor: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Inspect or repair a bounded page of already-visible Claude lineage.

        This is deliberately store-local: it never performs provider calls or native
        creation. A row is repaired only after its existing source, exact reserved
        Claude target, authenticated bridge provenance, completion, and link identity
        all validate in the same transaction.
        """

        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("Claude lineage reconciliation limit must be an integer")
        if limit < 1 or limit > _CLAUDE_LINEAGE_RECONCILE_LIMIT_MAX:
            raise ValueError(
                "Claude lineage reconciliation limit must be between 1 and "
                f"{_CLAUDE_LINEAGE_RECONCILE_LIMIT_MAX}"
            )
        cursor_secret = _validated_claude_lineage_cursor_secret(marker_secret)
        normalized_cursor = _validated_claude_lineage_cursor(
            cursor,
            marker_secret=cursor_secret,
            apply=apply,
        )

        def _write(conn):
            if normalized_cursor is None:
                after_key = None
                high_water_key = _last_unlinked_claude_visibility_job_key(
                    conn,
                    source_identity_issue=self._claude_lineage_source_identity_issue,
                )
            else:
                after_key, high_water_key = normalized_cursor
                _validate_claude_lineage_cursor_anchors(
                    conn,
                    after_key=after_key,
                    high_water_key=high_water_key,
                )
            candidates = (
                []
                if high_water_key is None
                else _unlinked_claude_visibility_jobs(
                    conn,
                    limit=limit + 1,
                    after_key=after_key,
                    high_water_key=high_water_key,
                    source_identity_issue=self._claude_lineage_source_identity_issue,
                )
            )
            has_more = len(candidates) > limit
            rows = candidates[:limit]
            blocker_codes: dict[str, int] = {}
            repairable = 0
            repaired = 0
            operation_time = _finite_number(self._clock(), "clock")
            for row in rows:
                inspected = _inspect_claude_visibility_lineage(
                    conn,
                    row,
                    source_identity_issue=self._claude_lineage_source_identity_issue,
                )
                if inspected["state"] == "repairable":
                    repairable += 1
                    if apply:
                        finalized = _finalize_claude_visibility_lineage_if_indexed(
                            conn,
                            job_id=str(row["id"]),
                            created_at=operation_time,
                            source_identity_issue=(
                                self._claude_lineage_source_identity_issue
                            ),
                        )
                        if finalized["state"] not in {"linked", "already_linked"}:
                            raise ValueError(
                                str(finalized["code"] or _CLAUDE_LINEAGE_CONFLICT)
                            )
                        repaired += 1
                    continue
                code = str(inspected["code"] or _CLAUDE_LINEAGE_CONFLICT)
                blocker_codes[code] = blocker_codes.get(code, 0) + 1
            remaining = _count_unlinked_claude_visibility_jobs(
                conn,
                source_identity_issue=self._claude_lineage_source_identity_issue,
            )
            next_cursor = None
            if has_more and rows:
                last_key = _claude_lineage_job_key(rows[-1])
                assert high_water_key is not None
                next_cursor = _public_claude_lineage_cursor(
                    after_key=last_key,
                    high_water_key=high_water_key,
                    marker_secret=cursor_secret,
                    apply=apply,
                )
            return {
                "scanned": len(rows),
                "repairable": repairable,
                "repaired": repaired,
                "remaining": remaining,
                "blocker_codes": dict(sorted(blocker_codes.items())),
                "next_cursor": next_cursor,
                "has_more": has_more,
                "complete": remaining == 0,
            }

        return self.db._execute_write(_write)

    def record_claude_visibility_cycle(
        self,
        *,
        status: str,
        error_code: str | None,
        registrar_result: bool,
    ) -> None:
        safe_status = _claude_status_token(status)
        if safe_status in {None, "invalid", "redacted"}:
            safe_status = "degraded"
        if error_code is None:
            safe_error = None
        else:
            safe_error = (
                error_code
                if error_code in CLAUDE_VISIBILITY_PUBLIC_RESULT_ERROR_CODES
                else "unknown_error_code"
            )
        if not isinstance(registrar_result, bool):
            raise TypeError("registrar_result must be a boolean")

        def _write(conn):
            recorded_at = _finite_number(self._clock(), "clock")
            row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?",
                (_CLAUDE_VISIBILITY_CYCLE_STATE_KEY,),
            ).fetchone()
            previous = _decode_claude_visibility_cycle_state(
                row["value_json"] if row is not None else None
            )
            sequence = int(previous.get("sequence", 0)) + 1
            empty_verified = False
            if safe_status == "no_due_job":
                state_rows = conn.execute(
                    """SELECT state, COUNT(*) AS count
                       FROM session_claude_visibility_jobs AS job
                       WHERE job.operator_cleared_at IS NULL
                         AND NOT EXISTS (
                           SELECT 1
                           FROM session_claude_visibility_characterization_events AS event
                           WHERE event.job_id = job.id
                             AND event.event_kind IN (
                                 'cleanup_completed', 'launch_aborted'
                             )
                       )
                       GROUP BY state"""
                ).fetchall()
                empty_verified = all(
                    state_row["state"] == "claude_visible"
                    and int(state_row["count"]) >= 1
                    for state_row in state_rows
                )
            value: dict[str, Any] = {
                "version": _CLAUDE_VISIBILITY_CYCLE_STATE_VERSION,
                "sequence": sequence,
                "last_cycle_at": recorded_at,
                "last_result": {
                    "status": safe_status,
                    "error_code": safe_error,
                    "empty_verified": empty_verified,
                },
            }
            if "last_empty_cycle_at" in previous:
                value["last_empty_cycle_at"] = previous["last_empty_cycle_at"]
            if "last_registrar_result" in previous:
                value["last_registrar_result"] = previous["last_registrar_result"]
            if empty_verified:
                value["last_empty_cycle_at"] = recorded_at
            if registrar_result:
                value["last_registrar_result"] = {
                    "at": recorded_at,
                    "sequence": sequence,
                    "status": safe_status,
                    "error_code": safe_error,
                }
            value_json = json.dumps(value, sort_keys=True, separators=(",", ":"))
            conn.execute(
                """INSERT INTO session_bridge_state (key, value_json, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value_json = excluded.value_json,
                       updated_at = excluded.updated_at""",
                (_CLAUDE_VISIBILITY_CYCLE_STATE_KEY, value_json, recorded_at),
            )

        self.db._execute_write(_write)

    def has_claude_visibility_source(self, source_session_id: str) -> bool:
        normalized = _exact_nonempty_text(
            source_session_id, "Claude visibility source session ID"
        )
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            row = conn.execute(
                """SELECT 1 FROM session_claude_visibility_jobs
                   WHERE source_session_id = ? LIMIT 1""",
                (normalized,),
            ).fetchone()
        return row is not None

    def get_external_activity(self, session_id: str) -> float | None:
        """Return the indexed activity watermark for an external session."""
        key = _external_activity_state_key(session_id)
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        try:
            return _decode_external_activity(row["value_json"])
        except ValueError:
            return None

    def list_visible_claude_visibility_mirrors(self) -> list[dict[str, str]]:
        """Return (source_session_id, claude_uuid) for every visible mirror."""
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            rows = conn.execute(
                """SELECT source_session_id, reserved_claude_uuid
                   FROM session_claude_visibility_jobs
                   WHERE state = 'claude_visible'
                   ORDER BY source_session_id""",
            ).fetchall()
        return [
            {
                "source_session_id": row["source_session_id"],
                "claude_uuid": row["reserved_claude_uuid"],
            }
            for row in rows
        ]

    def _claude_visibility_local_day(self, timestamp: float) -> str:
        if self._local_timezone is None:
            local = datetime.fromtimestamp(timestamp).astimezone()
        else:
            local = datetime.fromtimestamp(timestamp, tz=self._local_timezone)
        return local.date().isoformat()

    def _discover_hermes_profile_db_paths(self) -> tuple[tuple[str, Path], ...]:
        profiles_root = self.db.db_path.parent / "profiles"
        if not profiles_root.is_dir():
            return ()
        return tuple(
            (entry.name, entry / "state.db")
            for entry in sorted(profiles_root.iterdir(), key=lambda path: path.name)
            if entry.is_dir() and (entry / "state.db").is_file()
        )

    # How long a cached profile handle may live before it is reopened. The
    # handle is only closed and rebuilt on the next use, which hands any
    # offline maintenance a periodic window to swap that profile's database
    # file underneath us (Windows refuses os.replace while a reader holds it).
    _PROFILE_DB_MAX_AGE_S = 30.0

    def _acquire_profile_database(self, path: Path, key: str) -> SessionDB | None:
        """Return a read-only handle for *path*, reusing an open one.

        Opening a SessionDB is not cheap: the first statement on a fresh
        connection makes SQLite parse the entire schema, and this ran for
        every profile on every call. Measured 2026-08-18: 18 profile
        databases, one of them 1.7 GB, re-opened and closed on each of seven
        call sites -- ~51% of the bridge's wall time landed in this path.

        A handle is discarded when the file's identity changes (a VACUUM swap
        replaces the file, so the old handle would serve stale bytes) or when
        it exceeds ``_PROFILE_DB_MAX_AGE_S``.
        """
        try:
            stat = path.stat()
        except OSError:
            return None
        identity = (int(stat.st_dev), int(stat.st_ino))
        now = time.monotonic()
        with self._profile_db_lock:
            cached = self._profile_db_cache.get(key)
            if cached is not None:
                database, cached_identity, opened_at = cached
                if (
                    cached_identity == identity
                    and (now - opened_at) < self._PROFILE_DB_MAX_AGE_S
                ):
                    return database
                # Retire by DROPPING the reference, never by closing it here:
                # a sibling worker thread may be mid-query on this handle, and
                # closing it under them raises ProgrammingError. Refcounting
                # closes the connection once the last user lets go.
                self._profile_db_cache.pop(key, None)
                # A VACUUM swap puts a DIFFERENT file behind this path, whose
                # data_version restarts and so can collide with the retired
                # file's. Drop the rows with the handle rather than trust it.
                with self._profile_candidate_lock:
                    self._profile_candidate_cache.pop(key, None)
            try:
                database = SessionDB(path, read_only=True)
            except Exception:
                return None
            self._install_profile_read_compatibility(database)
            self._profile_db_cache[key] = (database, identity, now)
            return database

    def close_profile_databases(self) -> None:
        """Release every cached cross-profile handle. Never raises."""
        with self._profile_candidate_lock:
            self._profile_candidate_cache.clear()
        with self._profile_db_lock:
            entries = list(self._profile_db_cache.values())
            self._profile_db_cache.clear()
        for database, _identity, _opened_at in entries:
            try:
                database.close()
            except Exception:
                pass

    @contextmanager
    def _native_hermes_databases(self):
        databases: list[tuple[str, SessionDB, bool]] = [("default", self.db, False)]
        seen = {str(self.db.db_path.resolve()).casefold()}
        for profile, raw_path in self._hermes_profile_db_paths():
            if not isinstance(profile, str) or not profile.strip():
                raise ValueError("Hermes profile name must be nonempty")
            path = Path(raw_path)
            key = str(path.resolve()).casefold()
            if key in seen or not path.is_file():
                continue
            seen.add(key)
            database = self._acquire_profile_database(path, key)
            if database is None:
                continue
            # The third element is a PROVENANCE marker -- "a foreign profile
            # database, not our own" -- which consumers branch on (e.g.
            # list_sidebar_candidates keeps only the `owned` entries). It is
            # not a lifetime flag: the cache owns these handles now and this
            # context manager deliberately no longer closes them.
            databases.append((profile.strip(), database, True))
        yield databases

    @staticmethod
    def _install_profile_read_compatibility(database: SessionDB) -> None:
        """Supply empty TEMP bridge tables for pre-bridge profile databases."""

        definitions = {
            "external_sessions": """(
                session_id TEXT, provider TEXT, native_id TEXT,
                native_status TEXT, first_indexed_at REAL, last_indexed_at REAL,
                origin_kind TEXT, origin_bridge_id TEXT, sync_error TEXT
            )""",
            "session_links": """(
                id TEXT, from_session_id TEXT, to_session_id TEXT,
                relation TEXT, bridge_id TEXT, created_at REAL,
                hydrated_at REAL, diverged_at REAL
            )""",
            "session_mirror_jobs": """(
                source_session_id TEXT, state TEXT
            )""",
            "session_sidebar_jobs": """(
                source_session_id TEXT, state TEXT, lease_expires_at REAL,
                codex_thread_id TEXT, error_code TEXT
            )""",
        }
        with database._lock:
            conn = database._conn
            assert conn is not None
            existing = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                ).fetchall()
            }
            for name, definition in definitions.items():
                if name not in existing:
                    conn.execute(f"CREATE TEMP TABLE {name} {definition}")

    @staticmethod
    def _database_columns(database: SessionDB, table: str) -> set[str]:
        with database._lock:
            conn = database._conn
            assert conn is not None
            return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

    @classmethod
    def _profile_catalog_compatible(cls, database: SessionDB) -> bool:
        session_columns = cls._database_columns(database, "sessions")
        message_columns = cls._database_columns(database, "messages")
        return {
            "id",
            "source",
            "model",
            "title",
            "started_at",
            "ended_at",
            "message_count",
            "cwd",
            "git_branch",
            "git_repo_root",
            "parent_session_id",
            "archived",
        }.issubset(session_columns) and {
            "id",
            "session_id",
            "role",
            "content",
            "tool_call_id",
            "tool_calls",
            "tool_name",
            "timestamp",
            "active",
            "compacted",
        }.issubset(message_columns)

    @classmethod
    def _profile_lineage_compatible(cls, database: SessionDB) -> bool:
        """Return whether profile bridge tables support lineage validation."""

        return {"session_id"}.issubset(
            cls._database_columns(database, "external_sessions")
        ) and {"to_session_id"}.issubset(
            cls._database_columns(database, "session_links")
        )

    def _profile_shadow_source_identity_issue(
        self,
        *,
        source_session_id: str,
        model_config: object,
    ) -> str | None:
        """Validate one root profile shadow against its read-only native authority."""

        if not isinstance(model_config, str) or not model_config:
            return "identity"
        try:
            shadow_config = json.loads(model_config)
        except (TypeError, ValueError):
            return "identity"
        if not isinstance(shadow_config, Mapping) or set(shadow_config) != {
            "_session_bridge_profile"
        }:
            return "identity"
        expected_profile = shadow_config["_session_bridge_profile"]
        if (
            not isinstance(expected_profile, str)
            or not expected_profile.strip()
            or expected_profile != expected_profile.strip()
        ):
            return "identity"

        matches: list[tuple[str, Mapping[str, Any], bool]] = []
        with self._native_hermes_databases() as databases:
            for profile, database, owned in databases:
                if (
                    not owned
                    or not self._profile_catalog_compatible(database)
                    or not self._profile_lineage_compatible(database)
                ):
                    continue
                with database._lock:
                    profile_conn = database._conn
                    assert profile_conn is not None
                    row = profile_conn.execute(
                        """SELECT s.source,
                                  e.session_id AS external_session_id
                           FROM sessions AS s
                           LEFT JOIN external_sessions AS e
                             ON e.session_id = s.id
                           WHERE s.id = ?""",
                        (source_session_id,),
                    ).fetchone()
                    if row is None:
                        continue
                    incoming = profile_conn.execute(
                        """SELECT 1 FROM session_links
                           WHERE to_session_id = ? LIMIT 1""",
                        (source_session_id,),
                    ).fetchone()
                matches.append((profile, row, incoming is not None))

        if len(matches) != 1 or matches[0][0] != expected_profile:
            return "identity"
        _profile, source, has_incoming = matches[0]
        session_source = source["source"]
        if (
            not isinstance(session_source, str)
            or not session_source.strip()
            or session_source != session_source.strip()
            or session_source
            in {
                Provider.CLAUDE.value,
                Provider.CODEX.value,
                _PROFILE_SHADOW_SOURCE,
            }
        ):
            return "identity"
        if source["external_session_id"] is not None or has_incoming:
            return "provenance"
        return None

    def _claude_lineage_source_identity_issue(
        self,
        conn: Any,
        *,
        source_session_id: object,
        source_provider: object,
    ) -> str | None:
        return _native_source_identity_issue(
            conn,
            source_session_id=source_session_id,
            source_provider=source_provider,
            profile_shadow_validator=self._profile_shadow_source_identity_issue,
        )

    def try_acquire_mirror_worker_lock(self) -> _MirrorWorkerFileLock | None:
        """Try to serialize mirror processing and reconciliation across processes."""

        return self._try_acquire_worker_file_lock("session-bridge-worker")

    def try_acquire_sidebar_worker_lock(self) -> _MirrorWorkerFileLock | None:
        """Try to serialize the complete native-sidebar delivery transaction."""

        return self._try_acquire_worker_file_lock("session-bridge-sidebar-worker")

    def _try_acquire_worker_file_lock(
        self,
        lock_name: str,
    ) -> _MirrorWorkerFileLock | None:
        lock_path = self.db.db_path.with_name(
            f"{self.db.db_path.name}.{lock_name}.lock"
        )
        stream = lock_path.open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(
                    stream.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
        except OSError as exc:
            stream.close()
            if _mirror_worker_lock_contended(exc):
                return None
            raise
        return _MirrorWorkerFileLock(stream)

    def upsert_projection(
        self,
        projection: SessionProjection,
        *,
        rebuild: bool = False,
    ) -> UpsertResult:
        provider = _external_provider(projection.provider)
        session_id = canonical_session_id(provider, projection.native_id)
        native_id = projection.native_id.strip()
        git_branch = (
            projection.git_branch.strip() if projection.git_branch is not None else None
        )
        git_branch = git_branch or None
        now = float(self._clock())
        activity_state_key = _external_activity_state_key(session_id)
        last_active = float(projection.last_active)
        activity_value_json = json.dumps(
            {"last_active": last_active},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        projected_messages = _snapshot_projected_messages(projection)
        projected_keys = [
            (message.native_event_id, message.ordinal)
            for message, _ in projected_messages
        ]
        if len(set(projected_keys)) != len(projected_keys):
            raise ValueError("projection contains duplicate native message identities")

        def _write(conn):
            session_row = conn.execute(
                "SELECT source FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            external_row = conn.execute(
                """SELECT provider, native_id, origin_kind, origin_bridge_id
                   FROM external_sessions WHERE session_id = ?""",
                (session_id,),
            ).fetchone()

            if session_row is not None:
                matching_identity = (
                    external_row is not None
                    and external_row["provider"] == provider.value
                    and external_row["native_id"] == native_id
                    and session_row["source"] == provider.value
                )
                if not matching_identity:
                    if (
                        external_row is None
                        and session_row["source"] == provider.value
                    ):
                        raise LocalSessionOwnsCanonicalId(
                            "session ID collision for imported session "
                            f"{session_id!r}"
                        )
                    raise ValueError(
                        f"session ID collision for imported session {session_id!r}"
                    )
            elif external_row is not None:
                raise ValueError(
                    f"session ID collision for imported session {session_id!r}"
                )

            activity_row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?",
                (activity_state_key,),
            ).fetchone()
            persisted_last_active = (
                _decode_external_activity(activity_row["value_json"])
                if activity_row is not None
                else None
            )
            if (
                not rebuild
                and external_row is not None
                and persisted_last_active is not None
                and last_active < persisted_last_active
            ):
                raise StaleExternalProjection(
                    f"stale projection for session {session_id!r}"
                )

            if rebuild:
                pending = projected_messages
                has_new_human_user = False
            else:
                existing_keys = _existing_message_keys(conn, session_id, projected_keys)
                pending = [
                    (message, row)
                    for message, row in projected_messages
                    if (message.native_event_id, message.ordinal) not in existing_keys
                ]
                has_new_human_user = any(
                    message.role == "user"
                    and isinstance(message.content, str)
                    and bool(message.content.strip())
                    for message, _ in pending
                )

            origin_kind, origin_bridge_id = _resolve_projection_provenance(
                external_row,
                projection.origin_kind,
                projection.origin_bridge_id,
                has_new_human_user=has_new_human_user,
            )

            first_seen = external_row is None
            self.db._upsert_session_row(
                conn,
                session_id,
                provider.value,
                cwd=projection.cwd,
                started_at=float(projection.started_at),
            )
            conn.execute(
                """UPDATE sessions
                   SET source = ?,
                       title = CASE
                           WHEN ? IS NULL THEN title
                           WHEN NOT EXISTS (
                               SELECT 1 FROM sessions AS other
                               WHERE other.title = ? AND other.id != ?
                           ) THEN ?
                           ELSE title
                       END,
                       cwd = COALESCE(?, cwd),
                       git_branch = COALESCE(?, git_branch),
                       started_at = MIN(started_at, ?)
                   WHERE id = ?""",
                (
                    provider.value,
                    projection.title,
                    projection.title,
                    session_id,
                    projection.title,
                    projection.cwd,
                    git_branch,
                    float(projection.started_at),
                    session_id,
                ),
            )

            conn.execute(
                """INSERT INTO external_sessions (
                   session_id, provider, native_id, native_path, native_status,
                   last_native_cursor, last_native_hash, first_indexed_at,
                   last_indexed_at, parser_version, origin_kind, origin_bridge_id,
                   sync_error
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(session_id) DO UPDATE SET
                       native_path = excluded.native_path,
                       native_status = excluded.native_status,
                       last_native_cursor = excluded.last_native_cursor,
                       last_native_hash = excluded.last_native_hash,
                       last_indexed_at = excluded.last_indexed_at,
                       parser_version = excluded.parser_version,
                       origin_kind = excluded.origin_kind,
                       origin_bridge_id = excluded.origin_bridge_id,
                       sync_error = NULL""",
                (
                    session_id,
                    provider.value,
                    native_id,
                    projection.native_path,
                    projection.native_status,
                    projection.native_cursor,
                    projection.native_hash,
                    now,
                    now,
                    projection.parser_version,
                    origin_kind,
                    origin_bridge_id,
                ),
            )

            if (
                provider is Provider.CLAUDE
                and origin_bridge_id is not None
                and origin_kind
                in {
                    OriginKind.BRIDGE_PLACEHOLDER,
                    OriginKind.BRIDGE_CONTINUATION,
                }
            ):
                _ensure_claude_visibility_lineage_row_if_known(
                    conn,
                    target_session_id=session_id,
                    target_native_id=native_id,
                    bridge_id=origin_bridge_id,
                    created_at=now,
                    source_identity_issue=(self._claude_lineage_source_identity_issue),
                )

            if rebuild:
                # Delete the rows THIS ingest owns: every message carrying a
                # native_event_key. Not the map join, and not the whole session.
                #
                # Not the map join, because external_message_map cascades from
                # external_sessions: a session whose map rows were lost kept its
                # existing copy while the insert below appended a second one --
                # the 2026-08-06..08-10 double-ingest, 287,351 rows over 1,551
                # sessions. native_event_key lives on the message row itself, so
                # it cannot be orphaned that way and the delete stays idempotent.
                #
                # Not the whole session, because "the bridge owns every message
                # row of an external session" is FALSE. Measured on the live root
                # state.db 2026-08-25: of 18,757 keyless rows across 66 external
                # sessions, 13,062 (70%) have no keyed twin -- unique messages a
                # session_id-wide delete destroys on every rebuild. One session
                # alone holds 6,235 of them against a single keyed row.
                #
                # Consequence, deliberate and STILL LIVE: a keyless row that
                # IS double-ingest residue -- one carrying a keyed twin --
                # survives here where the wide form cleared it. That is a
                # standing property of this predicate, not a one-off. Leaving
                # stale duplicates behind is the correct price for not
                # destroying unique rows, so anyone counting duplicates later
                # should know it was chosen, not overlooked.
                #
                # The residue this had ACCUMULATED by 2026-08-25 is gone: the
                # other 5,695 of the 18,757 were exactly that, and on
                # 2026-08-26 all 5,695 were deleted from the live root state.db
                # out-of-band, Diego-authorized -- each one verified
                # byte-identical to a surviving keyed twin across all 22
                # columns first. So they were NOT retained indefinitely, and
                # the 18,757/66 figures above are a dated 2026-08-25
                # measurement rather than current state: live now is 13,062
                # keyless rows over 60 sessions, every one of them twinless.
                # Re-measure before quoting either.
                #
                # Size any recurrence off the residue's own footprint, not that
                # 60: the 5,695 lived in 15 sessions, 4 of which held 5,144 of
                # them. 60 is the TWINLESS session count and scopes a very
                # different job.
                conn.execute(
                    "DELETE FROM messages "
                    "WHERE session_id = ? AND native_event_key IS NOT NULL",
                    (session_id,),
                )

            # native_event_key is the same identity external_message_map keys on
            # (native_event_id, ordinal). Carrying it onto the message row lets
            # idx_messages_native_event_key refuse a duplicate at the storage
            # layer, instead of relying on this method's delete-then-insert.
            rows_to_insert = []
            for message, row in pending:
                row = dict(row)
                row["native_event_key"] = (
                    f"{message.native_event_id}:{message.ordinal}"
                )
                rows_to_insert.append(row)
            inserted_ids, _ = self.db._insert_message_rows_with_ids(
                conn, session_id, rows_to_insert
            )
            for (message, _), message_id in zip(pending, inserted_ids, strict=True):
                conn.execute(
                    """INSERT INTO external_message_map (
                       session_id, native_event_id, ordinal, message_id
                       ) VALUES (?, ?, ?, ?)""",
                    (
                        session_id,
                        message.native_event_id,
                        message.ordinal,
                        message_id,
                    ),
                )

            message_count, tool_call_count = _active_message_counters(conn, session_id)
            conn.execute(
                """UPDATE sessions
                   SET message_count = ?, tool_call_count = ?
                   WHERE id = ?""",
                (message_count, tool_call_count, session_id),
            )
            conn.execute(
                """INSERT INTO session_bridge_state (key, value_json, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value_json = excluded.value_json,
                       updated_at = excluded.updated_at""",
                (activity_state_key, activity_value_json, now),
            )
            return UpsertResult(
                session_id=session_id,
                inserted_messages=len(inserted_ids),
                rebuilt=rebuild,
                first_seen=first_seen,
            )

        return self.db._execute_write(_write)

    def get_external_session(self, session_id: str) -> dict[str, Any] | None:
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            row = conn.execute(
                "SELECT * FROM external_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_native_projections(
        self,
        after: float,
        limit: int,
        *,
        cursor: NativeProjectionCursor | None = None,
    ) -> NativeProjectionPage:
        cutoff = _finite_number(after, "after")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("native projection limit must be between 1 and 1000")
        normalized_cursor = _validated_native_projection_cursor(cursor)
        cursor_clause = ""
        query_params: dict[str, Any] = {
            "activity_prefix": "session-bridge:external-activity:",
            "profile_shadow_source": _PROFILE_SHADOW_SOURCE,
            "origin_kind": OriginKind.NATIVE.value,
            "after": cutoff,
            "queued": MirrorJobState.QUEUED.value,
            "running": MirrorJobState.RUNNING.value,
            "retry": MirrorJobState.RETRY.value,
            "structured_prefix": _STRUCTURED_CONTENT_HEX_PREFIX,
            "trim_chars": _PYTHON_STRIP_CHARACTERS,
            "query_limit": limit + 1,
        }
        if normalized_cursor is not None:
            cursor_clause = """WHERE (
                candidate.last_active < :cursor_activity
                OR (
                    candidate.last_active = :cursor_activity
                    AND candidate.id > :cursor_session_id
                )
            )"""
            query_params["cursor_activity"] = normalized_cursor[0]
            query_params["cursor_session_id"] = normalized_cursor[1]

        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            session_rows = conn.execute(
                f"""WITH candidate_evidence AS (
                       SELECT s.id, s.title, s.cwd, s.started_at, s.git_branch,
                              e.provider, e.native_id, e.native_path,
                              e.native_status, e.last_native_cursor,
                              e.last_native_hash, e.parser_version,
                              e.origin_kind, e.origin_bridge_id,
                              activity.value_json AS activity_value_json,
                              CAST(json_extract(
                                  activity.value_json, '$.last_active'
                              ) AS REAL) AS last_active,
                              (
                                  SELECT map.message_id
                                  FROM external_message_map AS map
                                  JOIN messages AS message
                                    ON message.id = map.message_id
                                  WHERE map.session_id = e.session_id
                                    AND message.active = 1
                                    AND message.role = 'user'
                                    AND typeof(message.content) = 'text'
                                    AND length(trim(
                                        message.content, :trim_chars
                                    )) > 0
                                    AND substr(
                                        hex(message.content), 1, 12
                                    ) != :structured_prefix
                                  ORDER BY message.timestamp, map.message_id
                                  LIMIT 1
                              ) AS evidence_message_id
                       FROM external_sessions AS e
                       JOIN sessions AS s ON s.id = e.session_id
                       JOIN session_bridge_state AS activity
                         ON activity.key = :activity_prefix || e.session_id
                       WHERE e.origin_kind = :origin_kind
                         AND e.origin_bridge_id IS NULL
                         AND CAST(json_extract(
                             activity.value_json, '$.last_active'
                         ) AS REAL) >= :after
                         AND NOT EXISTS (
                             SELECT 1
                             FROM session_links AS link
                             JOIN external_sessions AS target
                               ON target.session_id = link.to_session_id
                             WHERE link.from_session_id = e.session_id
                               AND target.provider = CASE e.provider
                                   WHEN 'claude' THEN 'codex'
                                   ELSE 'claude'
                               END
                         )
                         AND NOT EXISTS (
                             SELECT 1
                             FROM session_mirror_jobs AS job
                             WHERE job.source_session_id = e.session_id
                               AND job.state IN (
                                   :queued, :running, :retry
                               )
                         )
                   ), eligible_candidates AS (
                       SELECT *
                       FROM candidate_evidence
                       WHERE evidence_message_id IS NOT NULL
                   )
                   SELECT candidate.*,
                          map.native_event_id AS evidence_native_event_id,
                          map.ordinal AS evidence_ordinal,
                          message.role AS evidence_role,
                          substr(trim(
                              message.content, :trim_chars
                          ), 1, 1) AS evidence_content,
                          message.timestamp AS evidence_timestamp
                   FROM eligible_candidates AS candidate
                   JOIN external_message_map AS map
                     ON map.session_id = candidate.id
                    AND map.message_id = candidate.evidence_message_id
                   JOIN messages AS message
                     ON message.id = candidate.evidence_message_id
                   {cursor_clause}
                   ORDER BY candidate.last_active DESC, candidate.id
                   LIMIT :query_limit""",
                query_params,
            ).fetchall()
            if not session_rows:
                return NativeProjectionPage()

        has_more = len(session_rows) > limit
        page_rows = session_rows[:limit]
        projections: list[SessionProjection] = []
        for row in page_rows:
            last_active = _decode_external_activity(row["activity_value_json"])
            if last_active < cutoff or last_active != float(row["last_active"]):
                raise ValueError("native projection activity ordering is invalid")
            evidence_content = row["evidence_content"]
            if not isinstance(evidence_content, str) or not evidence_content.strip():
                raise ValueError("native projection evidence is invalid")
            projections.append(
                SessionProjection(
                    provider=_external_provider(row["provider"]),
                    native_id=row["native_id"],
                    title=row["title"],
                    cwd=row["cwd"],
                    started_at=float(row["started_at"]),
                    last_active=last_active,
                    messages=(
                        ProjectedMessage(
                            native_event_id=row["evidence_native_event_id"],
                            ordinal=row["evidence_ordinal"],
                            role=row["evidence_role"],
                            content=evidence_content,
                            timestamp=float(row["evidence_timestamp"]),
                        ),
                    ),
                    native_path=row["native_path"],
                    native_status=row["native_status"],
                    native_cursor=row["last_native_cursor"],
                    native_hash=row["last_native_hash"],
                    parser_version=row["parser_version"],
                    origin_kind=OriginKind(row["origin_kind"]),
                    origin_bridge_id=row["origin_bridge_id"],
                    git_branch=row["git_branch"],
                )
            )
        next_cursor = (
            (projections[-1].last_active, page_rows[-1]["id"])
            if has_more and projections
            else None
        )
        return NativeProjectionPage(
            projections,
            has_more=has_more,
            next_cursor=next_cursor,
        )

    def list_existing_target_mappings(
        self,
        source_ids: Sequence[str],
    ) -> frozenset[tuple[str, Provider]]:
        normalized_source_ids = _bounded_exact_ids(
            source_ids,
            label="source_ids",
            maximum=1000,
        )
        if not normalized_source_ids:
            return frozenset()

        mappings: set[tuple[str, Provider]] = set()
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            for start in range(0, len(normalized_source_ids), 400):
                batch = normalized_source_ids[start : start + 400]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"""SELECT link.from_session_id, target.provider
                         FROM session_links AS link
                         JOIN external_sessions AS target
                           ON target.session_id = link.to_session_id
                         WHERE link.from_session_id IN ({placeholders})""",
                    batch,
                ).fetchall()
                mappings.update(
                    (row["from_session_id"], _external_provider(row["provider"]))
                    for row in rows
                )
        return frozenset(mappings)

    def record_sidebar_exclusion(
        self,
        source_session_id: str,
        provider: Provider,
        reason_code: str,
        now: float,
    ) -> dict[str, Any]:
        from .sidebar import sidebar_idempotency_key

        sidebar_idempotency_key(source_session_id)
        if not isinstance(provider, Provider) or provider not in (
            Provider.CLAUDE,
            Provider.HERMES,
        ):
            raise ValueError("sidebar exclusion provider must be Claude or Hermes")
        expected_provider = (
            Provider.CLAUDE
            if source_session_id.startswith(f"{Provider.CLAUDE.value}:")
            else Provider.HERMES
        )
        if provider is not expected_provider:
            raise ValueError("sidebar exclusion provider does not match source")
        if (
            not isinstance(reason_code, str)
            or reason_code not in SIDEBAR_EXCLUSION_REASONS
        ):
            raise ValueError("sidebar exclusion reason is not in the fixed allowlist")
        excluded_at = _finite_number(now, "now")
        identity_digest = _sidebar_exclusion_identity_digest(
            source_session_id,
            provider,
            reason_code,
        )
        launch_metadata = self.get_session_launch_metadata(source_session_id)
        profile = (
            launch_metadata.get("profile")
            if isinstance(launch_metadata, Mapping)
            else None
        )

        def _write(conn):
            source_row = conn.execute(
                "SELECT source, model_config FROM sessions WHERE id = ?",
                (source_session_id,),
            ).fetchone()
            if source_row is None:
                if not isinstance(profile, str) or not profile.strip():
                    raise KeyError(source_session_id)
                model_config = json.dumps(
                    {"_session_bridge_profile": profile.strip()},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                conn.execute(
                    """INSERT INTO sessions (
                           id, source, model_config, started_at, title, cwd
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        source_session_id,
                        _PROFILE_SHADOW_SOURCE,
                        model_config,
                        excluded_at,
                        launch_metadata.get("title") if launch_metadata else None,
                        launch_metadata.get("cwd") if launch_metadata else None,
                    ),
                )
            elif source_row["source"] == _PROFILE_SHADOW_SOURCE:
                try:
                    shadow_config = json.loads(source_row["model_config"] or "{}")
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError("invalid profile shadow identity") from exc
                if shadow_config.get("_session_bridge_profile") != profile:
                    raise ValueError("conflicting profile shadow identity")
            insert = conn.execute(
                """INSERT OR IGNORE INTO session_sidebar_exclusions (
                   source_session_id, provider, reason_code,
                   source_identity_digest, excluded_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    source_session_id,
                    provider.value,
                    reason_code,
                    identity_digest,
                    excluded_at,
                    excluded_at,
                ),
            )
            row = conn.execute(
                """SELECT source_session_id, provider, reason_code,
                          source_identity_digest, excluded_at, updated_at
                     FROM session_sidebar_exclusions
                    WHERE source_session_id = ?""",
                (source_session_id,),
            ).fetchone()
            if row is None or (
                row["source_session_id"] != source_session_id
                or row["provider"] != provider.value
                or row["reason_code"] != reason_code
                or row["source_identity_digest"] != identity_digest
            ):
                raise ValueError("conflicting sidebar exclusion")
            return {
                "source_session_id": row["source_session_id"],
                "reason_code": row["reason_code"],
                "created": insert.rowcount == 1,
            }

        return self.db._execute_write(_write)

    def sidebar_exclusion_counts(self) -> dict[str, Any]:
        by_reason = {reason: 0 for reason in SIDEBAR_EXCLUSION_REASONS}
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            total = conn.execute(
                "SELECT COUNT(*) AS count FROM session_sidebar_exclusions"
            ).fetchone()["count"]
            rows = conn.execute(
                """SELECT reason_code, COUNT(*) AS count
                     FROM session_sidebar_exclusions
                    GROUP BY reason_code"""
            ).fetchall()
        for row in rows:
            if row["reason_code"] in by_reason:
                by_reason[row["reason_code"]] = row["count"]
        return {"total": total, "by_reason": by_reason}

    def list_claude_visibility_hermes_sources(
        self, after: float, limit: int | None
    ) -> tuple[SidebarSource, ...]:
        """Read native Hermes sources independently of all delivery queues."""

        cutoff = _finite_number(after, "Claude visibility candidate cutoff")
        if limit is not None and (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
        ):
            raise ValueError(
                "Claude visibility candidate limit must be between 1 and 1000"
            )
        sources: list[SidebarSource] = []
        with self._native_hermes_databases() as databases:
            for _profile, database, _owned in databases:
                if not self._profile_catalog_compatible(database):
                    continue
                sources.extend(
                    self._list_claude_visibility_hermes_sources_from_db(
                        database, after=cutoff, limit=limit
                    )
                )
        # One Hermes session can legitimately appear in more than one database:
        # the root/profile split writes some sessions to both this store's own
        # database and a profile database. Keep the primary copy -- it is the
        # one _recorded_worktree_snapshots() resolves against. Raising here
        # instead let 7 duplicated identities out of 20,846 sources disable the
        # entire Claude visibility lane, because the caller reports any
        # exception from this path as a generic provider_degraded.
        sources = _dedupe_native_session_copies(
            sources, identity=lambda source: source.source_session_id
        )
        identities = [source.source_session_id for source in sources]
        snapshots = self._recorded_worktree_snapshots(identities)
        sources = [
            self._with_recorded_worktree_snapshot(
                source, snapshots.get(source.source_session_id)
            )
            for source in sources
        ]
        sources.sort(
            key=lambda source: (
                -source.projection.last_active,
                source.source_session_id,
            )
        )
        return tuple(sources if limit is None else sources[:limit])

    def list_claude_visibility_codex_sources(
        self, after: float, limit: int | None
    ) -> tuple[SidebarSource, ...]:
        """Reconstruct full indexed Codex sources for visibility discovery."""

        cutoff = _finite_number(after, "Claude visibility candidate cutoff")
        if limit is not None and (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
        ):
            raise ValueError(
                "Claude visibility candidate limit must be between 1 and 1000"
            )
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            rows = conn.execute(
                """SELECT s.id AS session_id, s.source, s.title, s.cwd,
                          s.started_at, s.git_branch, s.git_repo_root,
                          e.provider, e.native_id, e.native_path, e.native_status,
                          e.last_native_cursor, e.last_native_hash,
                          e.last_indexed_at, e.parser_version, e.origin_kind,
                          e.origin_bridge_id, activity.value_json AS activity_value_json
                     FROM external_sessions AS e
                     JOIN sessions AS s ON s.id = e.session_id
                     JOIN session_bridge_state AS activity
                       ON activity.key = ? || e.session_id
                    WHERE e.provider = ?
                      AND CAST(json_extract(
                          activity.value_json, '$.last_active'
                      ) AS REAL) >= ?
                    ORDER BY CAST(json_extract(
                        activity.value_json, '$.last_active'
                    ) AS REAL) DESC, s.id""",
                (
                    "session-bridge:external-activity:",
                    Provider.CODEX.value,
                    cutoff,
                ),
            ).fetchall()
            messages: dict[str, list[ProjectedMessage]] = {
                row["session_id"]: [] for row in rows
            }
            seen_message_keys: dict[str, set[tuple[str, int]]] = {
                row["session_id"]: set() for row in rows
            }
            session_ids = list(messages)
            for start in range(0, len(session_ids), _MESSAGE_KEY_QUERY_CHUNK):
                batch = session_ids[start : start + _MESSAGE_KEY_QUERY_CHUNK]
                placeholders = ",".join("?" for _ in batch)
                message_rows = conn.execute(
                    f"""SELECT message.session_id, map.native_event_id,
                               map.ordinal, message.role, message.content,
                               message.timestamp, message.id
                          FROM external_message_map AS map
                          JOIN messages AS message ON message.id = map.message_id
                         WHERE map.session_id IN ({placeholders})
                           AND message.role = 'user'
                           AND (message.active = 1 OR message.compacted = 1)
                         ORDER BY message.session_id, message.timestamp,
                                  map.native_event_id, map.ordinal, message.id""",
                    batch,
                ).fetchall()
                for message in message_rows:
                    session_id = message["session_id"]
                    native_event_id = message["native_event_id"]
                    ordinal = int(message["ordinal"])
                    key = (native_event_id, ordinal)
                    if (
                        not isinstance(native_event_id, str)
                        or not native_event_id
                        or key in seen_message_keys[session_id]
                    ):
                        raise ValueError(
                            "invalid indexed Codex message identity"
                        )
                    seen_message_keys[session_id].add(key)
                    decoded = self.db._decode_content(message["content"])
                    messages[session_id].append(
                        ProjectedMessage(
                            native_event_id=native_event_id,
                            ordinal=ordinal,
                            role=message["role"],
                            content=decoded if isinstance(decoded, str) else None,
                            timestamp=float(message["timestamp"]),
                        )
                    )

        sources: list[SidebarSource] = []
        identities: set[str] = set()
        native_ids: set[str] = set()
        for row in rows:
            native_id = row["native_id"]
            if not isinstance(native_id, str) or not native_id.strip():
                raise ValueError("invalid indexed Codex native identity")
            source_session_id = canonical_session_id(Provider.CODEX, native_id)
            if (
                row["session_id"] != source_session_id
                or row["source"] != Provider.CODEX.value
                or row["provider"] != Provider.CODEX.value
                or source_session_id in identities
                or native_id in native_ids
            ):
                raise ValueError("conflicting indexed Codex session identity")
            identities.add(source_session_id)
            native_ids.add(native_id)
            last_active = _decode_external_activity(row["activity_value_json"])
            if last_active < cutoff:
                raise ValueError("indexed Codex activity ordering is invalid")
            sources.append(
                SidebarSource(
                    source_session_id=source_session_id,
                    projection=SessionProjection(
                        provider=Provider.CODEX,
                        native_id=native_id,
                        title=row["title"],
                        cwd=row["cwd"],
                        started_at=float(row["started_at"]),
                        last_active=last_active,
                        messages=tuple(messages[source_session_id]),
                        native_path=row["native_path"],
                        native_status=row["native_status"],
                        native_cursor=row["last_native_cursor"],
                        native_hash=row["last_native_hash"],
                        parser_version=int(row["parser_version"]),
                        origin_kind=OriginKind(row["origin_kind"]),
                        origin_bridge_id=row["origin_bridge_id"],
                        git_branch=row["git_branch"],
                    ),
                    git_root=row["git_repo_root"],
                    git_head=None,
                    worktree_id=None,
                    automation_only=False,
                    subagent_only=False,
                    indexed_at=float(row["last_indexed_at"]),
                )
            )
        sources.sort(
            key=lambda source: (
                -source.projection.last_active,
                source.source_session_id,
            )
        )
        return tuple(sources if limit is None else sources[:limit])

    def list_claude_visibility_source_ids(self) -> frozenset[str]:
        """Return sources already represented by a Claude visibility job."""

        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            rows = conn.execute(
                """SELECT DISTINCT source_session_id
                     FROM session_claude_visibility_jobs"""
            ).fetchall()
        source_ids: set[str] = set()
        for row in rows:
            source_session_id = row["source_session_id"]
            if not isinstance(source_session_id, str) or not source_session_id.strip():
                raise ValueError("invalid Claude visibility source identity")
            source_ids.add(source_session_id)
        return frozenset(source_ids)

    def _recorded_worktree_snapshots(
        self, source_session_ids: Sequence[str]
    ) -> dict[str, WorktreeSnapshot]:

        key_to_source = {
            _worktree_snapshot_state_key(source_id): source_id
            for source_id in source_session_ids
        }
        delivery_key_to_source = {
            _sidebar_delivery_state_key(source_id): source_id
            for source_id in source_session_ids
        }
        snapshots: dict[str, WorktreeSnapshot] = {}
        keys = list(key_to_source)
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            delivery_keys = list(delivery_key_to_source)
            for start in range(0, len(delivery_keys), _MESSAGE_KEY_QUERY_CHUNK):
                batch = delivery_keys[start : start + _MESSAGE_KEY_QUERY_CHUNK]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"""SELECT key, value_json FROM session_bridge_state
                         WHERE key IN ({placeholders})""",
                    batch,
                ).fetchall()
                for row in rows:
                    source_id = delivery_key_to_source[row["key"]]
                    candidate = _decode_sidebar_delivery_candidate(row["value_json"])
                    if candidate.source_session_id != source_id:
                        raise ValueError("invalid sidebar delivery candidate identity")
                    if candidate.git_root is None or candidate.worktree_id is None:
                        continue
                    payload = json.dumps(
                        {
                            "version": 1,
                            "source_session_id": source_id,
                            "cwd": candidate.cwd,
                            "git_root": candidate.git_root,
                            "branch": candidate.git_branch,
                            "head": candidate.git_head,
                            "worktree_id": candidate.worktree_id,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    snapshots[source_id] = _decode_worktree_snapshot(payload, source_id)
            for start in range(0, len(keys), _MESSAGE_KEY_QUERY_CHUNK):
                batch = keys[start : start + _MESSAGE_KEY_QUERY_CHUNK]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"""SELECT key, value_json FROM session_bridge_state
                         WHERE key IN ({placeholders})""",
                    batch,
                ).fetchall()
                for row in rows:
                    source_id = key_to_source[row["key"]]
                    snapshot = _decode_worktree_snapshot(row["value_json"], source_id)
                    prior = snapshots.get(source_id)
                    if prior is not None and prior != snapshot:
                        raise ValueError(
                            "conflicting native Hermes worktree snapshot identity"
                        )
                    snapshots[source_id] = snapshot
        return snapshots

    @staticmethod
    def _with_recorded_worktree_snapshot(
        source: SidebarSource, snapshot: WorktreeSnapshot | None
    ) -> SidebarSource:
        if snapshot is None or snapshot.git_root is None:
            return source
        projection = source.projection
        if (
            projection.cwd != snapshot.cwd
            or (source.git_root is not None and source.git_root != snapshot.git_root)
            or (
                projection.git_branch is not None
                and projection.git_branch != snapshot.branch
            )
        ):
            raise ValueError("conflicting native Hermes worktree snapshot identity")
        return replace(
            source,
            projection=replace(
                projection, cwd=snapshot.cwd, git_branch=snapshot.branch
            ),
            git_root=snapshot.git_root,
            git_head=snapshot.head,
            worktree_id=snapshot.worktree_id,
        )

    @staticmethod
    def _list_claude_visibility_hermes_sources_from_db(
        database: SessionDB, *, after: float, limit: int | None
    ) -> list[SidebarSource]:
        with database._lock:
            conn = database._conn
            assert conn is not None
            rows = conn.execute(
                """WITH candidate AS (
                       SELECT s.id AS session_id, s.source, s.model_config,
                              s.title, s.cwd, s.started_at, s.git_branch,
                              s.git_repo_root,
                              COALESCE(
                                  (SELECT MAX(message.timestamp)
                                     FROM messages AS message
                                    WHERE message.session_id = s.id
                                      AND (message.active = 1 OR message.compacted = 1)),
                                  s.started_at
                              ) AS last_active,
                              CASE WHEN s.source = 'cron' THEN 1 ELSE 0 END
                                  AS automation_only,
                              CASE
                                  WHEN s.source IN ('subagent', 'tool') THEN 1
                                  WHEN json_extract(COALESCE(s.model_config, '{}'),
                                                    '$._delegate_from') IS NOT NULL THEN 1
                                  ELSE 0
                              END AS subagent_only,
                              (SELECT incoming.relation
                                 FROM session_links AS incoming
                                WHERE incoming.to_session_id = s.id
                                ORDER BY incoming.created_at, incoming.id LIMIT 1)
                                  AS incoming_relation,
                              (SELECT incoming.bridge_id
                                 FROM session_links AS incoming
                                WHERE incoming.to_session_id = s.id
                                ORDER BY incoming.created_at, incoming.id LIMIT 1)
                                  AS incoming_bridge_id
                         FROM sessions AS s
                         LEFT JOIN external_sessions AS e ON e.session_id = s.id
                        WHERE e.session_id IS NULL
                          AND s.id NOT LIKE 'claude:%'
                          AND s.id NOT LIKE 'codex:%'
                          AND s.source != ?
                   )
                   SELECT * FROM candidate
                    WHERE last_active IS NOT NULL AND last_active >= ?
                    ORDER BY last_active DESC, session_id
                    LIMIT ?""",
                (_PROFILE_SHADOW_SOURCE, after, -1 if limit is None else limit),
            ).fetchall()
            messages: dict[str, list[ProjectedMessage]] = {
                row["session_id"]: [] for row in rows
            }
            session_ids = list(messages)
            for start in range(0, len(session_ids), _MESSAGE_KEY_QUERY_CHUNK):
                batch = session_ids[start : start + _MESSAGE_KEY_QUERY_CHUNK]
                placeholders = ",".join("?" for _ in batch)
                message_rows = conn.execute(
                    f"""SELECT id, session_id, role, content, timestamp
                           FROM messages
                          WHERE session_id IN ({placeholders}) AND role = 'user'
                            AND (active = 1 OR compacted = 1)
                          ORDER BY session_id, timestamp, id""",
                    batch,
                ).fetchall()
                for message in message_rows:
                    message_id = int(message["id"])
                    decoded = database._decode_content(message["content"])
                    messages[message["session_id"]].append(
                        ProjectedMessage(
                            native_event_id=f"hermes-message:{message_id}",
                            ordinal=message_id,
                            role=message["role"],
                            content=decoded if isinstance(decoded, str) else None,
                            timestamp=float(message["timestamp"]),
                        )
                    )
        sources: list[SidebarSource] = []
        for row in rows:
            incoming_relation = row["incoming_relation"]
            if incoming_relation == Relation.MIRRORS.value:
                origin_kind = OriginKind.BRIDGE_PLACEHOLDER
            elif incoming_relation is not None:
                origin_kind = OriginKind.BRIDGE_CONTINUATION
            else:
                origin_kind = OriginKind.NATIVE
            source_session_id = row["session_id"]
            sources.append(
                SidebarSource(
                    source_session_id=source_session_id,
                    projection=SessionProjection(
                        provider=Provider.HERMES,
                        native_id=source_session_id,
                        title=row["title"],
                        cwd=row["cwd"],
                        started_at=float(row["started_at"]),
                        last_active=float(row["last_active"]),
                        messages=tuple(messages[source_session_id]),
                        native_status="active",
                        origin_kind=origin_kind,
                        origin_bridge_id=row["incoming_bridge_id"],
                        git_branch=row["git_branch"],
                    ),
                    git_root=row["git_repo_root"],
                    git_head=None,
                    worktree_id=None,
                    automation_only=bool(row["automation_only"]),
                    subagent_only=bool(row["subagent_only"]),
                )
            )
        return sources

    def list_sidebar_candidates(
        self,
        after: float | None,
        limit: int,
        *,
        cursor: SidebarCandidateCursor | None = None,
    ) -> SidebarSourcePage:
        cutoff = (
            None
            if after is None
            else _finite_number(after, "sidebar candidate cutoff")
        )
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("sidebar candidate limit must be between 1 and 1000")
        normalized_cursor = _validated_sidebar_candidate_cursor(cursor)
        claude_cursor_clause = ""
        hermes_cursor_clause = ""
        claude_cutoff_clause = ""
        hermes_cutoff_clause = ""
        params: dict[str, Any] = {
            "claude": Provider.CLAUDE.value,
            "native": OriginKind.NATIVE.value,
            "activity_prefix": _EXTERNAL_ACTIVITY_KEY_PREFIX,
            "activity_prefix_len": len(_EXTERNAL_ACTIVITY_KEY_PREFIX),
            "profile_shadow_source": _PROFILE_SHADOW_SOURCE,
            "query_limit": limit + 1,
        }
        if cutoff is not None:
            claude_cutoff_clause = f"AND {_SIDEBAR_ACTIVITY_EXPR} >= :after"
            hermes_cutoff_clause = "AND last_active >= :after"
            params["after"] = cutoff
        if normalized_cursor is not None:
            claude_cursor_clause = f"""AND (
                              {_SIDEBAR_ACTIVITY_EXPR} < :cursor_activity
                              OR (
                                  {_SIDEBAR_ACTIVITY_EXPR} = :cursor_activity
                                  AND substr(
                                      activity.key, :activity_prefix_len + 1
                                  ) > :cursor_session_id
                              )
                          )"""
            hermes_cursor_clause = """AND (
                              last_active < :cursor_activity
                              OR (
                                  last_active = :cursor_activity
                                  AND session_id > :cursor_session_id
                              )
                          )"""
            params["cursor_activity"] = normalized_cursor[0]
            params["cursor_session_id"] = normalized_cursor[1]

        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            rows = conn.execute(
                f"""WITH claude_candidate AS (
                       SELECT substr(
                                  activity.key, :activity_prefix_len + 1
                              ) AS session_id,
                              s.source, s.model_config,
                              s.title, s.cwd, s.started_at, s.git_branch,
                              s.git_repo_root,
                              e.provider AS external_provider,
                              e.native_id AS external_native_id,
                              e.native_path, e.native_status,
                              e.last_native_cursor, e.last_native_hash,
                              e.last_indexed_at, e.parser_version, e.origin_kind,
                              e.origin_bridge_id,
                              {_SIDEBAR_ACTIVITY_EXPR} AS last_active,
                              CASE WHEN s.source = 'cron' THEN 1 ELSE 0 END
                                  AS automation_only,
                              CASE
                                  WHEN s.source IN ('subagent', 'tool') THEN 1
                                  WHEN json_extract(
                                      COALESCE(s.model_config, '{{}}'),
                                      '$._delegate_from'
                                  ) IS NOT NULL THEN 1
                                  ELSE 0
                              END AS subagent_only
                         FROM session_bridge_state AS activity
                         INDEXED BY idx_session_bridge_state_activity_ordered
                         JOIN sessions AS s
                           ON s.id = substr(
                               activity.key, :activity_prefix_len + 1
                           )
                         JOIN external_sessions AS e
                           ON e.session_id = s.id
                        WHERE substr(
                                  activity.key, 1, :activity_prefix_len
                              ) = :activity_prefix
                          AND {_SIDEBAR_ACTIVITY_EXPR} IS NOT NULL
                          AND e.provider = :claude
                          AND e.origin_kind = :native
                          AND e.origin_bridge_id IS NULL
                          AND s.source != :profile_shadow_source
                          AND NOT EXISTS (
                              SELECT 1
                                FROM session_sidebar_jobs AS sidebar_job
                               WHERE sidebar_job.source_session_id = s.id
                          )
                          AND NOT EXISTS (
                              SELECT 1
                                FROM session_sidebar_exclusions AS exclusion
                               WHERE exclusion.source_session_id = s.id
                          )
                          {claude_cutoff_clause}
                          {claude_cursor_clause}
                        ORDER BY {_SIDEBAR_ACTIVITY_EXPR} DESC, activity.key
                        LIMIT :query_limit
                   ), hermes_metadata AS (
                       SELECT s.id AS session_id, s.source, s.model_config,
                              s.title, s.cwd, s.started_at, s.git_branch,
                              s.git_repo_root,
                              NULL AS external_provider,
                              NULL AS external_native_id,
                              NULL AS native_path, NULL AS native_status,
                              NULL AS last_native_cursor,
                              NULL AS last_native_hash,
                              NULL AS last_indexed_at, NULL AS parser_version,
                              NULL AS origin_kind, NULL AS origin_bridge_id,
                              COALESCE(
                                  (SELECT MAX(message.timestamp)
                                     FROM messages AS message
                                    WHERE message.session_id = s.id
                                      AND (
                                          message.active = 1
                                          OR message.compacted = 1
                                      )),
                                  s.started_at
                              ) AS last_active,
                              CASE WHEN s.source = 'cron' THEN 1 ELSE 0 END
                                  AS automation_only,
                              CASE
                                  WHEN s.source IN ('subagent', 'tool') THEN 1
                                  WHEN json_extract(
                                      COALESCE(s.model_config, '{{}}'),
                                      '$._delegate_from'
                                  ) IS NOT NULL THEN 1
                                  ELSE 0
                              END AS subagent_only
                         FROM sessions AS s
                         LEFT JOIN external_sessions AS e
                           ON e.session_id = s.id
                        WHERE e.session_id IS NULL
                          AND s.id NOT LIKE 'claude:%'
                          AND s.id NOT LIKE 'codex:%'
                          AND NOT EXISTS (
                              SELECT 1
                                FROM session_links AS incoming_link
                               WHERE incoming_link.to_session_id = s.id
                          )
                          AND s.source != :profile_shadow_source
                          AND NOT EXISTS (
                              SELECT 1
                                FROM session_sidebar_jobs AS sidebar_job
                               WHERE sidebar_job.source_session_id = s.id
                          )
                          AND NOT EXISTS (
                              SELECT 1
                                FROM session_sidebar_exclusions AS exclusion
                               WHERE exclusion.source_session_id = s.id
                          )
                   ), hermes_candidate AS (
                       SELECT * FROM hermes_metadata
                        WHERE last_active IS NOT NULL
                          {hermes_cutoff_clause}
                          {hermes_cursor_clause}
                        ORDER BY last_active DESC, session_id
                        LIMIT :query_limit
                   )
                   SELECT * FROM (
                       SELECT * FROM claude_candidate
                       UNION ALL
                       SELECT * FROM hermes_candidate
                   )
                    ORDER BY last_active DESC, session_id
                    LIMIT :query_limit""",
                params,
            ).fetchall()
            page_rows = rows[:limit]
            messages_by_session: dict[str, list[ProjectedMessage]] = {
                row["session_id"]: [] for row in page_rows
            }
            session_ids = list(messages_by_session)
            for start in range(0, len(session_ids), _MESSAGE_KEY_QUERY_CHUNK):
                batch = session_ids[start : start + _MESSAGE_KEY_QUERY_CHUNK]
                placeholders = ",".join("?" for _ in batch)
                message_rows = conn.execute(
                    f"""SELECT message.id, message.session_id, message.role,
                               message.content, message.timestamp,
                               map.native_event_id, map.ordinal
                          FROM messages AS message
                          LEFT JOIN external_message_map AS map
                            ON map.message_id = message.id
                         WHERE message.session_id IN ({placeholders})
                           AND message.role = 'user'
                           AND (message.active = 1 OR message.compacted = 1)
                         ORDER BY message.session_id,
                                  message.timestamp, message.id""",
                    batch,
                ).fetchall()
                for message_row in message_rows:
                    decoded_content = self.db._decode_content(message_row["content"])
                    content = (
                        decoded_content if isinstance(decoded_content, str) else None
                    )
                    message_id = int(message_row["id"])
                    messages_by_session[message_row["session_id"]].append(
                        ProjectedMessage(
                            native_event_id=(
                                message_row["native_event_id"]
                                or f"hermes-message:{message_id}"
                            ),
                            ordinal=(
                                int(message_row["ordinal"])
                                if message_row["ordinal"] is not None
                                else message_id
                            ),
                            role=message_row["role"],
                            content=content,
                            timestamp=float(message_row["timestamp"]),
                        )
                    )

        sources: list[SidebarSource] = []
        for row in page_rows:
            source_session_id = row["session_id"]
            provider = (
                Provider.CLAUDE
                if row["external_provider"] == Provider.CLAUDE.value
                else Provider.HERMES
            )
            native_id = (
                row["external_native_id"]
                if provider is Provider.CLAUDE
                else source_session_id
            )
            last_active = _finite_number(
                row["last_active"], "sidebar candidate activity"
            )
            projection = SessionProjection(
                provider=provider,
                native_id=native_id,
                title=row["title"],
                cwd=row["cwd"],
                started_at=float(row["started_at"]),
                last_active=last_active,
                messages=tuple(messages_by_session[source_session_id]),
                native_path=row["native_path"],
                native_status=row["native_status"] or "active",
                native_cursor=row["last_native_cursor"],
                native_hash=row["last_native_hash"],
                parser_version=int(row["parser_version"] or 1),
                origin_kind=OriginKind.NATIVE,
                origin_bridge_id=None,
                git_branch=row["git_branch"],
            )
            sources.append(
                SidebarSource(
                    source_session_id=source_session_id,
                    projection=projection,
                    git_root=row["git_repo_root"],
                    git_head=None,
                    worktree_id=None,
                    automation_only=bool(row["automation_only"]),
                    subagent_only=bool(row["subagent_only"]),
                    indexed_at=(
                        _finite_number(
                            row["last_indexed_at"],
                            "sidebar source indexed_at",
                        )
                        if provider is Provider.CLAUDE
                        and row["last_indexed_at"] is not None
                        else None
                    ),
                )
            )

        profile_has_more = False
        with self._native_hermes_databases() as databases:
            owned_databases = [
                (profile, profile_db)
                for profile, profile_db, owned in databases
                if owned
            ]
            # Hoisted out of _list_profile_sidebar_sources, which ran this
            # against the ROOT connection once per profile -- eighteen
            # identical queries per call on the live bridge.
            blocked = (
                self._blocked_sidebar_source_ids() if owned_databases else set()
            )
            for profile, profile_db in owned_databases:
                profile_sources, more = self._list_profile_sidebar_sources(
                    profile_db,
                    profile=profile,
                    after=cutoff,
                    limit=limit,
                    cursor=normalized_cursor,
                    blocked=blocked,
                )
                sources.extend(profile_sources)
                profile_has_more = profile_has_more or more

        # Same root/profile overlap as the Claude visibility lane above. The
        # root copy wins unconditionally: _blocked_sidebar_source_ids() and the
        # placement/reconciliation tables this page feeds are all root-owned,
        # so a profile copy would not resolve against them.
        sources = _dedupe_native_session_copies(
            sources, identity=lambda source: source.source_session_id
        )
        sources.sort(
            key=lambda source: (
                -source.projection.last_active,
                source.source_session_id,
            )
        )
        combined_has_more = len(sources) > limit
        sources = sources[:limit]
        has_more = len(rows) > limit or profile_has_more or combined_has_more
        next_cursor = (
            (sources[-1].projection.last_active, sources[-1].source_session_id)
            if has_more and sources
            else None
        )
        return SidebarSourcePage(
            sources,
            has_more=has_more,
            next_cursor=next_cursor,
        )

    def _blocked_sidebar_source_ids(self) -> set[str]:
        with self.db._lock:
            root_conn = self.db._conn
            assert root_conn is not None
            return {
                row[0]
                for row in root_conn.execute(
                    """SELECT source_session_id FROM session_sidebar_jobs
                       UNION SELECT source_session_id FROM session_sidebar_exclusions"""
                ).fetchall()
            }

    def _profile_sidebar_candidate_rows(
        self,
        profile_db: SessionDB,
        *,
        profile: str,
    ) -> list[dict[str, Any]]:
        """Return this profile's candidate rows, newest-first, reusing the scan.

        The scan cannot stop early. ``last_active`` is a correlated MAX over
        ``messages``, so SQLite has to compute it for EVERY session before it
        can order or cut them -- the query plan is a full SCAN of ``sessions``
        plus a per-row index seek plus a TEMP B-TREE sort, and ``LIMIT`` only
        truncates the output. Measured against the live 1.7 GB main profile on
        2026-08-19: 7,075 sessions, ~97 ms per page.

        That cost was paid per PAGE. The registration loop runs after every
        successful scan (``catalog_scan_seconds`` defaults to 3) and spends up
        to ``_SIDEBAR_REGISTRATION_QUERY_BUDGET`` pages; backfill spends up to
        ``_SIDEBAR_BACKFILL_QUERY_BUDGET`` -- 100 pages, ~9.7 s of CPU, to walk
        a list that had not changed. py-spy put 31.5% of process CPU here.

        The cutoff is deliberately NOT part of the query. ``after`` is derived
        from the wall clock (``registration_time - backfill_days * 86_400``), so
        it drifts on every call and would make the cache key miss every time.
        Cutoff and cursor are applied to the cached rows instead.

        Invalidation is ``PRAGMA data_version``, which SQLite bumps whenever
        ANOTHER connection commits to the file. These handles are opened
        read-only, so the bridge can never be the writer that this misses, and
        a cache hit therefore means the file is byte-for-byte what it was when
        the rows were read -- this trades no freshness away at all. Measured
        over 45 s on 2026-08-19: zero version changes across all 18 profile
        databases, so in practice this scan stops repeating entirely. A profile
        under constant write simply recomputes, exactly as before.
        """

        with profile_db._lock:
            conn = profile_db._conn
            assert conn is not None
            version = conn.execute("PRAGMA data_version").fetchone()[0]
            # Same key _profile_db_cache uses, so retiring a handle can evict
            # the rows that were read through it.
            key = str(profile_db.db_path.resolve()).casefold()
            with self._profile_candidate_lock:
                cached = self._profile_candidate_cache.get(key)
            if cached is not None and cached[0] == version:
                return cached[1]
            rows = [
                {
                    "session_id": row["session_id"],
                    "title": row["title"],
                    "cwd": row["cwd"],
                    "started_at": row["started_at"],
                    "git_branch": row["git_branch"],
                    "git_repo_root": row["git_repo_root"],
                    "last_active": row["last_active"],
                    "automation_only": row["automation_only"],
                    "subagent_only": row["subagent_only"],
                }
                for row in conn.execute(
                    """WITH candidate AS (
                       SELECT s.id AS session_id,
                              s.title, s.cwd, s.started_at, s.git_branch,
                              s.git_repo_root,
                              COALESCE(
                                  (SELECT MAX(message.timestamp)
                                     FROM messages AS message
                                    WHERE message.session_id = s.id
                                      AND (message.active = 1 OR message.compacted = 1)),
                                  s.started_at
                              ) AS last_active,
                              CASE WHEN s.source = 'cron' THEN 1 ELSE 0 END
                                  AS automation_only,
                              CASE
                                  WHEN s.source IN ('subagent', 'tool') THEN 1
                                  WHEN json_extract(COALESCE(s.model_config, '{}'),
                                                    '$._delegate_from') IS NOT NULL THEN 1
                                  ELSE 0
                              END AS subagent_only
                         FROM sessions AS s
                         LEFT JOIN external_sessions AS e ON e.session_id = s.id
                        WHERE e.session_id IS NULL
                          AND s.id NOT LIKE 'claude:%'
                          AND s.id NOT LIKE 'codex:%'
                          AND NOT EXISTS (
                              SELECT 1 FROM session_links AS incoming_link
                               WHERE incoming_link.to_session_id = s.id
                          )
                   )
                   SELECT * FROM candidate
                    WHERE last_active IS NOT NULL
                    ORDER BY last_active DESC, session_id"""
                ).fetchall()
            ]
        with self._profile_candidate_lock:
            self._profile_candidate_cache[key] = (version, rows)
        return rows

    def _list_profile_sidebar_sources(
        self,
        profile_db: SessionDB,
        *,
        profile: str,
        after: float | None,
        limit: int,
        cursor: SidebarCandidateCursor | None,
        blocked: set[str],
    ) -> tuple[list[SidebarSource], bool]:
        if not self._profile_catalog_compatible(profile_db):
            return [], False
        candidates = self._profile_sidebar_candidate_rows(
            profile_db,
            profile=profile,
        )
        selected: list[dict[str, Any]] = []
        for row in candidates:
            if row["session_id"] in blocked:
                continue
            last_active = row["last_active"]
            if after is not None and last_active < after:
                continue
            if cursor is not None and not (
                last_active < cursor[0]
                or (last_active == cursor[0] and row["session_id"] > cursor[1])
            ):
                continue
            selected.append(row)
            if len(selected) > limit:
                break
        page_rows = selected[:limit]

        messages: dict[str, list[ProjectedMessage]] = {
            row["session_id"]: [] for row in page_rows
        }
        if page_rows:
            session_ids = list(messages)
            with profile_db._lock:
                conn = profile_db._conn
                assert conn is not None
                for start in range(0, len(session_ids), _MESSAGE_KEY_QUERY_CHUNK):
                    batch = session_ids[start : start + _MESSAGE_KEY_QUERY_CHUNK]
                    placeholders = ",".join("?" for _ in batch)
                    message_rows = conn.execute(
                        f"""SELECT id, session_id, role, content, timestamp
                              FROM messages
                             WHERE session_id IN ({placeholders})
                               AND role = 'user'
                               AND (active = 1 OR compacted = 1)
                             ORDER BY session_id, timestamp, id""",
                        batch,
                    ).fetchall()
                    for message in message_rows:
                        message_id = int(message["id"])
                        decoded = profile_db._decode_content(message["content"])
                        messages[message["session_id"]].append(
                            ProjectedMessage(
                                native_event_id=f"hermes-message:{message_id}",
                                ordinal=message_id,
                                role=message["role"],
                                content=decoded if isinstance(decoded, str) else None,
                                timestamp=float(message["timestamp"]),
                            )
                        )
        sources = [
            SidebarSource(
                source_session_id=row["session_id"],
                projection=SessionProjection(
                    provider=Provider.HERMES,
                    native_id=row["session_id"],
                    title=row["title"],
                    cwd=row["cwd"],
                    started_at=float(row["started_at"]),
                    last_active=float(row["last_active"]),
                    messages=tuple(messages[row["session_id"]]),
                    native_status="active",
                    origin_kind=OriginKind.NATIVE,
                    git_branch=row["git_branch"],
                ),
                git_root=row["git_repo_root"],
                git_head=None,
                worktree_id=None,
                automation_only=bool(row["automation_only"]),
                subagent_only=bool(row["subagent_only"]),
            )
            for row in page_rows
        ]
        return sources, len(selected) > limit

    def get_session_launch_metadata(
        self, session_id: str
    ) -> dict[str, str | None] | None:
        normalized_session_id = _nonempty_text(session_id, "session ID")
        with self._native_hermes_databases() as databases:
            for profile, database, owned in databases:
                if owned and not self._profile_catalog_compatible(database):
                    continue
                with database._lock:
                    conn = database._conn
                    assert conn is not None
                    row = conn.execute(
                        "SELECT source, title, cwd FROM sessions WHERE id = ?",
                        (normalized_session_id,),
                    ).fetchone()
                if row is None:
                    continue
                if row["source"] == _PROFILE_SHADOW_SOURCE:
                    continue
                metadata = {"title": row["title"], "cwd": row["cwd"]}
                if owned:
                    metadata["profile"] = profile
                if any(
                    value is not None and not isinstance(value, str)
                    for value in metadata.values()
                ):
                    raise ValueError("invalid session launch metadata")
                return metadata
        return None

    def get_native_session_snapshot(self, session_id: str) -> dict[str, str] | None:
        """Return a stable snapshot identity for a native Hermes session.

        External harness sessions already carry provider cursors and hashes in
        ``external_sessions``. Native Hermes rows do not, so continuation uses
        this transactionally read digest instead of pretending they are an
        external provider session.
        """

        normalized_session_id = _nonempty_text(session_id, "session ID")
        with self._native_hermes_databases() as databases:
            matches: list[
                tuple[str, SessionDB, Mapping[str, Any], list[Mapping[str, Any]]]
            ] = []
            for profile, database, _owned in databases:
                if database is not self.db and not self._profile_catalog_compatible(
                    database
                ):
                    continue
                with database._lock:
                    conn = database._conn
                    assert conn is not None
                    session_row = conn.execute(
                        """SELECT s.id, s.source, s.model, s.title, s.started_at,
                          s.ended_at, s.end_reason, s.message_count,
                          s.tool_call_count, s.cwd, s.git_branch,
                          s.git_repo_root, s.rewind_count, s.archived,
                          e.session_id AS external_session_id
                     FROM sessions AS s
                     LEFT JOIN external_sessions AS e ON e.session_id = s.id
                    WHERE s.id = ?""",
                        (normalized_session_id,),
                    ).fetchone()
                    if session_row is None:
                        continue
                    if session_row["source"] == _PROFILE_SHADOW_SOURCE:
                        continue
                    message_rows = conn.execute(
                        """SELECT id, role, content, tool_call_id, tool_calls,
                          tool_name, timestamp, finish_reason, reasoning,
                          reasoning_details, codex_reasoning_items,
                          reasoning_content, codex_message_items, active,
                          compacted
                     FROM messages
                    WHERE session_id = ?
                    ORDER BY id""",
                        (normalized_session_id,),
                    ).fetchall()
                matches.append((profile, database, session_row, message_rows))
            if not matches:
                raise KeyError(normalized_session_id)
            # Continuation digests the transcript, so when the root/profile
            # split left a copy in both databases keep the one that actually
            # holds the messages; the root copy still wins a tie.
            matches = _dedupe_native_session_copies(
                matches,
                identity=lambda match: normalized_session_id,
                richness=lambda match: len(match[3]),
            )
            profile, database, session_row, message_rows = matches[0]
            if session_row["external_session_id"] is not None:
                return None

            identity = _native_session_snapshot_identity(
                dict(session_row),
                [dict(row) for row in message_rows],
                decode_content=database._decode_content,
            )
            return {
                "session_id": normalized_session_id,
                "provider": Provider.HERMES.value,
                "profile": profile,
                **identity,
            }

    def find_external_session_by_origin_bridge(
        self,
        bridge_id: str,
        provider: Provider,
    ) -> dict[str, Any] | None:
        normalized_bridge_id = _nonempty_text(bridge_id, "bridge ID")
        normalized_provider = _external_provider(provider)

        # _execute_read: resume path (continue_session) — retry transient WAL
        # locks instead of erroring the continuation.
        def _read(conn):
            assert conn is not None
            return conn.execute(
                """SELECT * FROM external_sessions
                   WHERE origin_bridge_id = ? AND provider = ?
                     AND origin_kind IN (?, ?)
                   ORDER BY session_id
                   LIMIT 2""",
                (
                    normalized_bridge_id,
                    normalized_provider.value,
                    OriginKind.BRIDGE_PLACEHOLDER.value,
                    OriginKind.BRIDGE_CONTINUATION.value,
                ),
            ).fetchall()

        rows = self.db._execute_read(_read)
        if len(rows) > 1:
            raise ValueError(
                "duplicate bridge provenance for provider and origin bridge ID"
            )
        return dict(rows[0]) if rows else None

    def get_bridge_summaries(
        self, session_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        unique_ids = list(dict.fromkeys(session_ids))
        if not unique_ids:
            return {}

        summaries: dict[str, dict[str, Any]] = {}
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            for start in range(0, len(unique_ids), 400):
                batch = unique_ids[start : start + 400]
                placeholders = ",".join("?" for _ in batch)
                session_rows = conn.execute(
                    f"""SELECT s.id, e.provider, e.native_id, e.origin_kind,
                               e.sync_error
                        FROM sessions AS s
                        LEFT JOIN external_sessions AS e ON e.session_id = s.id
                        WHERE s.id IN ({placeholders})""",
                    batch,
                ).fetchall()
                job_rows = conn.execute(
                    f"""SELECT source_session_id, state
                        FROM session_mirror_jobs
                        WHERE source_session_id IN ({placeholders})""",
                    batch,
                ).fetchall()
                link_rows = conn.execute(
                    f"""SELECT * FROM session_links
                        WHERE from_session_id IN ({placeholders})
                           OR to_session_id IN ({placeholders})""",
                    [*batch, *batch],
                ).fetchall()
                sidebar_rows = conn.execute(
                    f"""SELECT source_session_id, state, lease_expires_at,
                               codex_thread_id, error_code
                        FROM session_sidebar_jobs
                        WHERE source_session_id IN ({placeholders})""",
                    batch,
                ).fetchall()

                jobs_by_session: dict[str, list[str]] = {}
                for row in job_rows:
                    jobs_by_session.setdefault(row["source_session_id"], []).append(
                        row["state"]
                    )
                links_by_session: dict[str, list[dict[str, Any]]] = {}
                for row in link_rows:
                    link = dict(row)
                    links_by_session.setdefault(row["from_session_id"], []).append(link)
                    if row["to_session_id"] != row["from_session_id"]:
                        links_by_session.setdefault(row["to_session_id"], []).append(
                            link
                        )
                sidebar_by_session: dict[str, dict[str, Any]] = {}
                sidebar_now: float | None = None
                for row in sidebar_rows:
                    session_id = row["source_session_id"]
                    if session_id in sidebar_by_session:
                        raise ValueError("duplicate sidebar summary source identity")
                    public_state = PUBLIC_SIDEBAR_STATE.get(row["state"])
                    if public_state is None:
                        raise ValueError("invalid sidebar summary state")
                    error_code = row["error_code"]
                    thread_id = None
                    stale = False
                    if row["state"] == SidebarJobState.LEASED.value:
                        lease_expires_at = row["lease_expires_at"]
                        if (
                            not isinstance(lease_expires_at, (int, float))
                            or isinstance(lease_expires_at, bool)
                            or not math.isfinite(float(lease_expires_at))
                        ):
                            public_state = "failed"
                            error_code = "catalog_metadata_invalid"
                            stale = True
                        else:
                            if sidebar_now is None:
                                sidebar_now = _finite_number(
                                    self._clock(), "store clock"
                                )
                            stale = float(lease_expires_at) <= sidebar_now
                            if error_code is not None:
                                public_state = "failed"
                                error_code = "catalog_metadata_invalid"
                    elif row["state"] == SidebarJobState.VISIBLE.value:
                        visible_thread_id = _public_codex_thread_id(
                            row["codex_thread_id"]
                        )
                        if visible_thread_id is None or error_code is not None:
                            public_state = "failed"
                            error_code = "catalog_metadata_invalid"
                        else:
                            thread_id = visible_thread_id
                    sidebar_by_session[session_id] = {
                        "bridge_sidebar_state": public_state,
                        "bridge_sidebar_codex_thread_id": thread_id,
                        "bridge_sidebar_error": error_code,
                        "bridge_sidebar_stale": stale,
                    }

                for row in session_rows:
                    session_id = row["id"]
                    if row["provider"] is None:
                        summaries[session_id] = {
                            "bridge_provider": Provider.HERMES.value,
                            "bridge_mirror_state": None,
                            **sidebar_by_session.get(session_id, {}),
                        }
                        continue
                    links = links_by_session.get(session_id, [])
                    summaries[session_id] = {
                        "bridge_provider": row["provider"],
                        "bridge_native_id": row["native_id"],
                        "bridge_origin_kind": row["origin_kind"],
                        "bridge_mirror_state": _mirror_state(
                            jobs_by_session.get(session_id, []), links
                        ),
                        "bridge_sync_error": row["sync_error"],
                        "bridge_links": [dict(link) for link in links],
                        **sidebar_by_session.get(session_id, {}),
                    }
        return summaries

    def enqueue_sidebar_job(
        self,
        candidate: SidebarCandidate,
        *,
        worktree_snapshot: WorktreeSnapshot | None = None,
        indexed_at: float | None = None,
    ) -> dict[str, Any]:
        from .sidebar import (
            SidebarCandidate,
            sidebar_bridge_id,
            sidebar_idempotency_key,
        )

        if not isinstance(candidate, SidebarCandidate):
            raise ValueError("sidebar candidate is malformed")
        state_key = _sidebar_delivery_state_key(candidate.source_session_id)
        state_value_json = _encode_sidebar_delivery_candidate(candidate)
        worktree_key: str | None = None
        worktree_value_json: str | None = None
        if worktree_snapshot is not None:
            worktree_key = _worktree_snapshot_state_key(candidate.source_session_id)
            worktree_value_json = _encode_worktree_snapshot(
                candidate.source_session_id,
                candidate,
                worktree_snapshot,
            )
        idempotency_key = sidebar_idempotency_key(candidate.source_session_id)
        expected_provider = (
            Provider.CLAUDE
            if candidate.source_session_id.startswith("claude:")
            else Provider.HERMES
        )
        if candidate.provider is not expected_provider:
            raise ValueError("sidebar candidate provider does not match source")
        expected_bridge_id = sidebar_bridge_id(candidate.source_session_id)
        if candidate.bridge_id != expected_bridge_id:
            raise ValueError("sidebar candidate bridge ID does not match source")
        eligible_at = _finite_number(candidate.eligible_at, "sidebar eligible_at")
        now = _finite_number(self._clock(), "store clock")
        effective_indexed_at = (
            max(eligible_at, now)
            if indexed_at is None
            else _finite_number(indexed_at, "sidebar indexed_at")
        )
        if effective_indexed_at < eligible_at:
            raise ValueError("sidebar indexed_at precedes eligible_at")
        created_at = max(now, effective_indexed_at)
        job_id = f"sidebar-job:{hashlib.sha256(idempotency_key.encode()).hexdigest()}"
        launch_metadata = self.get_session_launch_metadata(candidate.source_session_id)
        profile = (
            launch_metadata.get("profile")
            if isinstance(launch_metadata, Mapping)
            else None
        )

        def _write(conn):
            source_row = conn.execute(
                "SELECT source, model_config FROM sessions WHERE id = ?",
                (candidate.source_session_id,),
            ).fetchone()
            if source_row is None:
                if not isinstance(profile, str) or not profile.strip():
                    raise KeyError(candidate.source_session_id)
                model_config = json.dumps(
                    {"_session_bridge_profile": profile.strip()},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                conn.execute(
                    """INSERT INTO sessions (
                           id, source, model_config, started_at, title, cwd
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        candidate.source_session_id,
                        _PROFILE_SHADOW_SOURCE,
                        model_config,
                        eligible_at,
                        launch_metadata.get("title") if launch_metadata else None,
                        candidate.cwd,
                    ),
                )
            elif source_row["source"] == _PROFILE_SHADOW_SOURCE:
                try:
                    shadow_config = json.loads(source_row["model_config"] or "{}")
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError("invalid profile shadow identity") from exc
                if shadow_config.get("_session_bridge_profile") != profile:
                    raise ValueError("conflicting profile shadow identity")
            insert = conn.execute(
                """INSERT OR IGNORE INTO session_sidebar_jobs (
                   id, idempotency_key, source_session_id, bridge_id, state,
                   attempts, next_attempt_at, eligible_at, indexed_at,
                   created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    idempotency_key,
                    candidate.source_session_id,
                    expected_bridge_id,
                    SidebarJobState.PENDING.value,
                    eligible_at,
                    eligible_at,
                    effective_indexed_at,
                    created_at,
                    created_at,
                ),
            )
            row = conn.execute(
                "SELECT * FROM session_sidebar_jobs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None or (
                row["id"] != job_id
                or row["source_session_id"] != candidate.source_session_id
                or row["bridge_id"] != expected_bridge_id
            ):
                raise ValueError("conflicting sidebar job identity")
            if worktree_key is not None and worktree_value_json is not None:
                worktree_row = conn.execute(
                    "SELECT value_json FROM session_bridge_state WHERE key = ?",
                    (worktree_key,),
                ).fetchone()
                if worktree_row is None:
                    conn.execute(
                        """INSERT INTO session_bridge_state (key, value_json, updated_at)
                           VALUES (?, ?, ?)""",
                        (worktree_key, worktree_value_json, now),
                    )
                elif worktree_row["value_json"] != worktree_value_json:
                    raise ValueError("conflicting worktree snapshot identity")
            state_row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?",
                (state_key,),
            ).fetchone()
            if state_row is None:
                conn.execute(
                    """INSERT INTO session_bridge_state (key, value_json, updated_at)
                       VALUES (?, ?, ?)""",
                    (state_key, state_value_json, now),
                )
            elif state_row["value_json"] != state_value_json:
                raise ValueError("conflicting sidebar delivery candidate")
            return {**dict(row), "created": insert.rowcount == 1}

        return self.db._execute_write(_write)

    def get_worktree_snapshot(self, source_session_id: str) -> WorktreeSnapshot | None:
        state_key = _worktree_snapshot_state_key(source_session_id)
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?",
                (state_key,),
            ).fetchone()
        if row is None:
            return None
        return _decode_worktree_snapshot(row["value_json"], source_session_id)

    def sidebar_execution_blockers(self) -> tuple[str, ...]:
        """Return durable hard stops that forbid claiming another sidebar job."""

        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            return self._sidebar_execution_blockers_in_connection(conn)

    @staticmethod
    def _sidebar_execution_blockers_in_connection(
        conn: sqlite3.Connection,
        resolution_stats: Mapping[str, Any] | None = None,
    ) -> tuple[str, ...]:
        retryable_codes = tuple(sorted(SIDEBAR_RETRYABLE_ERRORS))
        placeholders = ", ".join("?" for _ in retryable_codes)
        if resolution_stats is None:
            resolution_stats = (
                SessionBridgeStore._sidebar_terminal_resolution_stats_in_connection(
                    conn
                )
            )
        unknown_retry = conn.execute(
            f"""SELECT 1 FROM session_sidebar_jobs
                WHERE state = ? AND error_code IS NOT NULL
                  AND error_code NOT IN ({placeholders})
                LIMIT 1""",
            (SidebarJobState.RETRY.value, *retryable_codes),
        ).fetchone()

        blockers: list[str] = []
        if resolution_stats["ineffective_terminal_resolution_count"]:
            blockers.append("sidebar_terminal_resolution_mismatch")
        if not resolution_stats["ledger_valid"]:
            blockers.append("sidebar_terminal_resolution_ledger_invalid")
        if unknown_retry is not None:
            blockers.append("unknown_retry_code")
        return tuple(blockers)

    @staticmethod
    def _sidebar_terminal_resolution_stats_in_connection(
        conn: sqlite3.Connection,
    ) -> dict[str, Any]:
        failed_count = int(
            conn.execute(
                """SELECT COUNT(*) AS job_count FROM session_sidebar_jobs
                   WHERE state = ?""",
                (SidebarJobState.FAILED.value,),
            ).fetchone()["job_count"]
        )
        bound_effective = 0
        precreate_total = 0
        precreate_effective = 0
        unbound_total = 0
        unbound_effective = 0
        v2_attempt_zero_total = 0
        v2_attempt_zero_effective = 0
        try:
            if not SessionBridgeStore._sidebar_terminal_resolution_ledger_is_valid(
                conn
            ):
                return {
                    "total": 0,
                    "effective": 0,
                    "ineffective": 0,
                    "ledger_valid": False,
                    "blocking_failed_count": failed_count,
                    "terminally_resolved_failed_count": 0,
                    "ineffective_terminal_resolution_count": 0,
                    "by_resolution_code": {
                        SIDEBAR_TERMINAL_RESOLUTION_CODE: 0,
                        SIDEBAR_PRECREATE_RESOLUTION_CODE: 0,
                        SIDEBAR_UNBOUND_RESOLUTION_CODE: 0,
                        SIDEBAR_V2_ATTEMPT_ZERO_RESOLUTION_CODE: 0,
                    },
                }
            total = int(
                conn.execute(
                    "SELECT COUNT(*) AS resolution_count "
                    "FROM session_sidebar_terminal_resolutions"
                ).fetchone()["resolution_count"]
            )
            candidates = conn.execute(
                """SELECT job.*,
                          resolution.evidence_digest AS resolution_evidence_digest
                     FROM session_sidebar_jobs AS job
                     JOIN session_sidebar_terminal_resolutions AS resolution
                       ON resolution.job_id = job.id
                      AND resolution.idempotency_key = job.idempotency_key
                      AND resolution.source_session_id = job.source_session_id
                      AND resolution.bridge_id = job.bridge_id
                      AND resolution.codex_thread_id = job.codex_thread_id
                      AND resolution.failure_state = job.state
                      AND resolution.failure_code = job.error_code
                      AND resolution.failure_attempts = job.attempts
                      AND resolution.failure_next_attempt_at = job.next_attempt_at
                      AND resolution.failure_updated_at = job.updated_at
                      AND resolution.resolution_code = ?
                      AND resolution.evidence_kind = ?
                      AND resolution.evidence_version = ?
                    WHERE job.state = ?
                      AND job.error_code = ?
                      AND job.codex_thread_id IS NOT NULL
                      AND job.lease_digest IS NULL
                      AND job.lease_expires_at IS NULL
                      AND job.completion_digest IS NULL
                      AND job.visible_at IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM external_sessions AS external
                           WHERE external.provider = ?
                             AND external.native_id = job.codex_thread_id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM session_links AS link
                           WHERE link.bridge_id = job.bridge_id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM session_sidebar_exclusions AS exclusion
                           WHERE exclusion.source_session_id = job.source_session_id
                      )""",
                (
                    SIDEBAR_TERMINAL_RESOLUTION_CODE,
                    SIDEBAR_TERMINAL_EVIDENCE_KIND,
                    SIDEBAR_TERMINAL_EVIDENCE_VERSION,
                    SidebarJobState.FAILED.value,
                    "native_create_ambiguous",
                    Provider.CODEX.value,
                ),
            ).fetchall()
            effective = 0
            for candidate in candidates:
                try:
                    job = dict(candidate)
                    source_session_id = _exact_nonempty_text(
                        job["source_session_id"], "sidebar source session ID"
                    )
                    reservation_row = conn.execute(
                        "SELECT value_json FROM session_bridge_state WHERE key = ?",
                        (_sidebar_create_reservation_state_key(source_session_id),),
                    ).fetchone()
                    if reservation_row is None:
                        continue
                    reservation = _decode_sidebar_create_reservation(
                        reservation_row["value_json"],
                        expected_source_session_id=source_session_id,
                    )
                    if (
                        reservation["job_id"] != job["id"]
                        or reservation["bridge_id"] != job["bridge_id"]
                    ):
                        continue
                    canonical_evidence = sidebar_terminal_evidence_digest(
                        job=job,
                        reservation=reservation,
                    )
                    stored_evidence = job["resolution_evidence_digest"]
                    if not isinstance(stored_evidence, str):
                        continue
                    if hmac.compare_digest(stored_evidence, canonical_evidence):
                        effective += 1
                except (TypeError, ValueError):
                    # Corrupt current evidence snapshots cannot waive a failure.
                    continue
            bound_total = total
            bound_effective = effective
            precreate_total = int(
                conn.execute(
                    "SELECT COUNT(*) AS resolution_count "
                    "FROM session_sidebar_precreate_resolutions"
                ).fetchone()["resolution_count"]
            )
            precreate_candidates = conn.execute(
                """SELECT job.*,
                          resolution.evidence_digest AS resolution_evidence_digest,
                          resolution.cutover_applied_at AS resolution_cutover_applied_at,
                          resolution.reservation_reserved_at AS resolution_reserved_at
                     FROM session_sidebar_jobs AS job
                     JOIN session_sidebar_precreate_resolutions AS resolution
                       ON resolution.job_id = job.id
                      AND resolution.idempotency_key = job.idempotency_key
                      AND resolution.source_session_id = job.source_session_id
                      AND resolution.bridge_id = job.bridge_id
                      AND resolution.failure_state = job.state
                      AND resolution.failure_code = job.error_code
                      AND resolution.failure_attempts = job.attempts
                      AND resolution.failure_next_attempt_at = job.next_attempt_at
                      AND resolution.failure_updated_at = job.updated_at
                      AND resolution.resolution_code = ?
                      AND resolution.evidence_kind = ?
                      AND resolution.evidence_version = ?
                    WHERE job.state = ?
                      AND job.error_code = ?
                      AND job.attempts = 0
                      AND job.codex_thread_id IS NULL
                      AND job.lease_digest IS NULL
                      AND job.lease_expires_at IS NULL
                      AND job.completion_digest IS NULL
                      AND job.visible_at IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM external_sessions AS external
                           WHERE external.provider = ?
                             AND external.origin_bridge_id = job.bridge_id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM session_links AS link
                           WHERE link.bridge_id = job.bridge_id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM session_sidebar_exclusions AS exclusion
                           WHERE exclusion.source_session_id = job.source_session_id
                      )""",
                (
                    SIDEBAR_PRECREATE_RESOLUTION_CODE,
                    SIDEBAR_PRECREATE_EVIDENCE_KIND,
                    SIDEBAR_PRECREATE_EVIDENCE_VERSION,
                    SidebarJobState.FAILED.value,
                    "native_create_ambiguous",
                    Provider.CODEX.value,
                ),
            ).fetchall()
            precreate_effective = 0
            for candidate in precreate_candidates:
                try:
                    job = dict(candidate)
                    source_session_id = _exact_nonempty_text(
                        job["source_session_id"], "sidebar source session ID"
                    )
                    reservation_row = conn.execute(
                        "SELECT value_json FROM session_bridge_state WHERE key = ?",
                        (_sidebar_create_reservation_state_key(source_session_id),),
                    ).fetchone()
                    cutover_row = conn.execute(
                        "SELECT value_json FROM session_bridge_state WHERE key = ?",
                        (_SIDEBAR_CREATE_RESERVATION_CUTOVER_STATE_KEY,),
                    ).fetchone()
                    if reservation_row is None or cutover_row is None:
                        continue
                    reservation = _decode_sidebar_create_reservation(
                        reservation_row["value_json"],
                        expected_source_session_id=source_session_id,
                    )
                    cutover = _decode_sidebar_create_reservation_cutover(
                        cutover_row["value_json"]
                    )
                    delivery_candidate = _validated_sidebar_cutover_candidate(conn, job)
                    if (
                        reservation["job_id"] != job["id"]
                        or reservation["bridge_id"] != job["bridge_id"]
                        or job["id"] not in cutover["quarantined_job_ids"]
                        or reservation["reserved_at"] != cutover["applied_at"]
                        or candidate["resolution_cutover_applied_at"]
                        != cutover["applied_at"]
                        or candidate["resolution_reserved_at"]
                        != reservation["reserved_at"]
                    ):
                        continue
                    canonical_evidence = sidebar_precreate_terminal_evidence_digest(
                        job=job,
                        reservation=reservation,
                        cutover=cutover,
                        candidate=delivery_candidate,
                    )
                    stored_evidence = job["resolution_evidence_digest"]
                    if isinstance(stored_evidence, str) and hmac.compare_digest(
                        stored_evidence, canonical_evidence
                    ):
                        precreate_effective += 1
                except (TypeError, ValueError):
                    continue
            unbound_total = int(
                conn.execute(
                    "SELECT COUNT(*) AS resolution_count "
                    "FROM session_sidebar_unbound_resolutions"
                ).fetchone()["resolution_count"]
            )
            unbound_candidates = conn.execute(
                """SELECT job.*,
                          resolution.evidence_digest AS resolution_evidence_digest,
                          resolution.reservation_reserved_at AS resolution_reserved_at
                     FROM session_sidebar_jobs AS job
                     JOIN session_sidebar_unbound_resolutions AS resolution
                       ON resolution.job_id = job.id
                      AND resolution.idempotency_key = job.idempotency_key
                      AND resolution.source_session_id = job.source_session_id
                      AND resolution.bridge_id = job.bridge_id
                      AND resolution.failure_state = job.state
                      AND resolution.failure_code = job.error_code
                      AND resolution.failure_attempts = job.attempts
                      AND resolution.failure_next_attempt_at = job.next_attempt_at
                      AND resolution.failure_updated_at = job.updated_at
                      AND resolution.resolution_code = ?
                      AND resolution.evidence_kind = ?
                      AND resolution.evidence_version = ?
                    WHERE job.state = ?
                      AND job.error_code = ?
                      AND job.attempts > 0
                      AND job.codex_thread_id IS NULL
                      AND job.lease_digest IS NULL
                      AND job.lease_expires_at IS NULL
                      AND job.completion_digest IS NULL
                      AND job.visible_at IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM external_sessions AS external
                           WHERE external.provider = ?
                             AND external.origin_bridge_id = job.bridge_id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM session_links AS link
                           WHERE link.bridge_id = job.bridge_id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM session_sidebar_exclusions AS exclusion
                           WHERE exclusion.source_session_id = job.source_session_id
                      )""",
                (
                    SIDEBAR_UNBOUND_RESOLUTION_CODE,
                    SIDEBAR_UNBOUND_EVIDENCE_KIND,
                    SIDEBAR_UNBOUND_EVIDENCE_VERSION,
                    SidebarJobState.FAILED.value,
                    "native_create_ambiguous",
                    Provider.CODEX.value,
                ),
            ).fetchall()
            unbound_effective = 0
            for candidate in unbound_candidates:
                try:
                    job = dict(candidate)
                    source_session_id = _exact_nonempty_text(
                        job["source_session_id"], "sidebar source session ID"
                    )
                    reservation_row = conn.execute(
                        "SELECT value_json FROM session_bridge_state WHERE key = ?",
                        (_sidebar_create_reservation_state_key(source_session_id),),
                    ).fetchone()
                    if reservation_row is None:
                        continue
                    reservation = _decode_sidebar_create_reservation(
                        reservation_row["value_json"],
                        expected_source_session_id=source_session_id,
                    )
                    delivery_candidate = _validated_sidebar_cutover_candidate(conn, job)
                    if (
                        reservation["job_id"] != job["id"]
                        or reservation["bridge_id"] != job["bridge_id"]
                        or candidate["resolution_reserved_at"]
                        != reservation["reserved_at"]
                    ):
                        continue
                    canonical_evidence = sidebar_unbound_terminal_evidence_digest(
                        job=job,
                        reservation=reservation,
                        candidate=delivery_candidate,
                    )
                    stored_evidence = job["resolution_evidence_digest"]
                    if isinstance(stored_evidence, str) and hmac.compare_digest(
                        stored_evidence, canonical_evidence
                    ):
                        unbound_effective += 1
                except (TypeError, ValueError):
                    continue
            v2_attempt_zero_total = int(
                conn.execute(
                    "SELECT COUNT(*) AS resolution_count "
                    "FROM session_sidebar_v2_attempt_zero_resolutions"
                ).fetchone()["resolution_count"]
            )
            v2_candidates = conn.execute(
                """SELECT resolution.job_id
                     FROM session_sidebar_v2_attempt_zero_resolutions AS resolution
                     JOIN session_sidebar_jobs AS job
                       ON job.id = resolution.job_id
                      AND resolution.idempotency_key = job.idempotency_key
                      AND resolution.source_session_id = job.source_session_id
                      AND resolution.bridge_id = job.bridge_id
                      AND resolution.failure_state = job.state
                      AND resolution.failure_code = job.error_code
                      AND resolution.failure_attempts = job.attempts
                      AND resolution.failure_next_attempt_at = job.next_attempt_at
                      AND resolution.failure_updated_at = job.updated_at
                     JOIN session_sidebar_reconciliation_proofs AS proof
                       ON proof.proof_digest =
                          resolution.reservation_reconciliation_proof_digest
                      AND proof.job_id = resolution.job_id
                      AND proof.source_session_id = resolution.source_session_id
                      AND proof.bridge_id = resolution.bridge_id
                      AND proof.reconciliation_generation =
                          resolution.reservation_reconciliation_generation
                      AND proof.completed_at = resolution.proof_completed_at
                      AND proof.expires_at = resolution.proof_expires_at
                      AND proof.inventory_digest = resolution.proof_inventory_digest
                    WHERE resolution.resolution_code = ?
                      AND resolution.evidence_kind = ?
                      AND resolution.evidence_version = ?
                      AND job.state = ? AND job.error_code = ? AND job.attempts = 0
                      AND job.codex_thread_id IS NULL
                      AND job.lease_digest IS NULL AND job.lease_expires_at IS NULL
                      AND job.completion_digest IS NULL AND job.visible_at IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM external_sessions AS external
                           WHERE external.provider = ?
                             AND external.origin_bridge_id = job.bridge_id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM session_links AS link
                           WHERE link.bridge_id = job.bridge_id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM session_sidebar_exclusions AS exclusion
                           WHERE exclusion.source_session_id = job.source_session_id
                      )""",
                (
                    SIDEBAR_V2_ATTEMPT_ZERO_RESOLUTION_CODE,
                    SIDEBAR_V2_ATTEMPT_ZERO_EVIDENCE_KIND,
                    SIDEBAR_V2_ATTEMPT_ZERO_EVIDENCE_VERSION,
                    SidebarJobState.FAILED.value,
                    "native_create_ambiguous",
                    Provider.CODEX.value,
                ),
            ).fetchall()
            for row in v2_candidates:
                try:
                    job_row = conn.execute(
                        "SELECT * FROM session_sidebar_jobs WHERE id = ?", (row["job_id"],)
                    ).fetchone()
                    resolution_row = conn.execute(
                        "SELECT * FROM session_sidebar_v2_attempt_zero_resolutions "
                        "WHERE job_id = ?", (row["job_id"],)
                    ).fetchone()
                    if job_row is None or resolution_row is None:
                        continue
                    job = dict(job_row)
                    resolution = dict(resolution_row)
                    reservation_row = conn.execute(
                        "SELECT value_json FROM session_bridge_state WHERE key = ?",
                        (_sidebar_create_reservation_state_key(job["source_session_id"]),),
                    ).fetchone()
                    proof_row = conn.execute(
                        "SELECT * FROM session_sidebar_reconciliation_proofs "
                        "WHERE proof_digest = ?",
                        (resolution["reservation_reconciliation_proof_digest"],),
                    ).fetchone()
                    if reservation_row is None or proof_row is None:
                        continue
                    reservation = _decode_sidebar_create_reservation(
                        reservation_row["value_json"],
                        expected_source_session_id=job["source_session_id"],
                    )
                    proof = dict(proof_row)
                    candidate = _validated_sidebar_cutover_candidate(conn, job)
                    canonical = sidebar_v2_attempt_zero_terminal_evidence_digest(
                        job=job, reservation=reservation, proof=proof,
                        candidate=candidate,
                    )
                    if (
                        reservation.get("version") == 2
                        and set(reservation) == _SIDEBAR_CREATE_RESERVATION_FIELDS
                        and reservation.get("job_id") == resolution["job_id"]
                        and reservation.get("source_session_id")
                        == resolution["source_session_id"]
                        and reservation.get("bridge_id") == resolution["bridge_id"]
                        and reservation.get("reserved_at")
                        == resolution["reservation_reserved_at"]
                        and reservation.get("reconciliation_proof_digest")
                        == resolution["reservation_reconciliation_proof_digest"]
                        and reservation.get("reconciliation_generation")
                        == resolution["reservation_reconciliation_generation"]
                        and proof.get("job_id") == resolution["job_id"]
                        and proof.get("source_session_id")
                        == resolution["source_session_id"]
                        and proof.get("bridge_id") == resolution["bridge_id"]
                        and proof.get("reconciliation_generation")
                        == resolution["reservation_reconciliation_generation"]
                        and proof.get("state") == "absence_proven"
                        and proof.get("match_count") == 0
                        and proof.get("recovered_thread_id") is None
                        and proof.get("fixed_reason") is None
                        and resolution["proof_completed_at"] == proof["completed_at"]
                        and resolution["proof_expires_at"] == proof["expires_at"]
                        and resolution["proof_inventory_digest"] == proof["inventory_digest"]
                        and resolution["resolution_code"]
                        == SIDEBAR_V2_ATTEMPT_ZERO_RESOLUTION_CODE
                        and resolution["evidence_kind"]
                        == SIDEBAR_V2_ATTEMPT_ZERO_EVIDENCE_KIND
                        and resolution["evidence_version"]
                        == SIDEBAR_V2_ATTEMPT_ZERO_EVIDENCE_VERSION
                        and hmac.compare_digest(resolution["evidence_digest"], canonical)
                    ):
                        v2_attempt_zero_effective += 1
                except (TypeError, ValueError):
                    continue
            total = (
                bound_total + precreate_total + unbound_total + v2_attempt_zero_total
            )
            effective = (
                bound_effective + precreate_effective + unbound_effective
                + v2_attempt_zero_effective
            )
            ledger_valid = True
        except sqlite3.DatabaseError:
            # Missing or malformed ledgers are never authority to waive a failure.
            total = 0
            effective = 0
            ledger_valid = False
        ineffective = max(0, total - effective)
        by_resolution_code = {
            SIDEBAR_TERMINAL_RESOLUTION_CODE: (bound_effective if ledger_valid else 0)
        }
        if ledger_valid and precreate_total:
            by_resolution_code[SIDEBAR_PRECREATE_RESOLUTION_CODE] = precreate_effective
        if ledger_valid and unbound_total:
            by_resolution_code[SIDEBAR_UNBOUND_RESOLUTION_CODE] = unbound_effective
        if ledger_valid and v2_attempt_zero_total:
            by_resolution_code[SIDEBAR_V2_ATTEMPT_ZERO_RESOLUTION_CODE] = (
                v2_attempt_zero_effective
            )
        return {
            "total": total,
            "effective": effective,
            "ineffective": ineffective,
            "ledger_valid": ledger_valid,
            "blocking_failed_count": max(0, failed_count - effective),
            "terminally_resolved_failed_count": effective,
            "ineffective_terminal_resolution_count": ineffective,
            "by_resolution_code": by_resolution_code,
        }

    @staticmethod
    def _sidebar_terminal_resolution_ledger_is_valid(
        conn: sqlite3.Connection,
    ) -> bool:
        table = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("session_sidebar_terminal_resolutions",),
        ).fetchone()
        if table is None or not isinstance(table["sql"], str):
            return False
        normalized_table_sql = " ".join(table["sql"].split())
        if any(
            requirement not in normalized_table_sql
            for requirement in _SIDEBAR_TERMINAL_LEDGER_SQL_REQUIREMENTS
        ):
            return False
        columns = conn.execute(
            'PRAGMA table_info("session_sidebar_terminal_resolutions")'
        ).fetchall()
        if tuple(row["name"] for row in columns) != _SIDEBAR_TERMINAL_LEDGER_COLUMNS:
            return False
        if not columns or int(columns[0]["pk"]) != 1:
            return False
        foreign_keys = conn.execute(
            'PRAGMA foreign_key_list("session_sidebar_terminal_resolutions")'
        ).fetchall()
        if len(foreign_keys) != 1:
            return False
        foreign_key = foreign_keys[0]
        if (
            foreign_key["table"] != "session_sidebar_jobs"
            or foreign_key["from"] != "job_id"
            or foreign_key["to"] != "id"
            or foreign_key["on_update"] != "RESTRICT"
            or foreign_key["on_delete"] != "RESTRICT"
        ):
            return False
        triggers = conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'trigger' "
            "AND tbl_name = 'session_sidebar_terminal_resolutions'"
        ).fetchall()
        expected_trigger_sql = {
            "trg_session_sidebar_terminal_resolutions_no_replacement": (
                "CREATE TRIGGER trg_session_sidebar_terminal_resolutions_no_replacement "
                "BEFORE INSERT ON session_sidebar_terminal_resolutions WHEN EXISTS ( "
                "SELECT 1 FROM session_sidebar_terminal_resolutions AS existing "
                "WHERE existing.job_id = NEW.job_id OR existing.idempotency_key = "
                "NEW.idempotency_key OR existing.source_session_id = "
                "NEW.source_session_id OR existing.bridge_id = NEW.bridge_id OR "
                "existing.codex_thread_id = NEW.codex_thread_id ) BEGIN SELECT "
                "RAISE(ABORT, 'sidebar terminal resolutions are immutable'); END"
            ),
            "trg_session_sidebar_terminal_resolutions_no_update": (
                "CREATE TRIGGER trg_session_sidebar_terminal_resolutions_no_update "
                "BEFORE UPDATE ON session_sidebar_terminal_resolutions BEGIN SELECT "
                "RAISE(ABORT, 'sidebar terminal resolutions are immutable'); END"
            ),
            "trg_session_sidebar_terminal_resolutions_no_delete": (
                "CREATE TRIGGER trg_session_sidebar_terminal_resolutions_no_delete "
                "BEFORE DELETE ON session_sidebar_terminal_resolutions BEGIN SELECT "
                "RAISE(ABORT, 'sidebar terminal resolutions are immutable'); END"
            ),
            "trg_session_sidebar_terminal_resolutions_no_precreate_overlap": (
                "CREATE TRIGGER "
                "trg_session_sidebar_terminal_resolutions_no_precreate_overlap "
                "BEFORE INSERT ON session_sidebar_terminal_resolutions WHEN EXISTS ( "
                "SELECT 1 FROM session_sidebar_precreate_resolutions AS existing "
                "WHERE existing.job_id = NEW.job_id OR existing.idempotency_key = "
                "NEW.idempotency_key OR existing.source_session_id = "
                "NEW.source_session_id OR existing.bridge_id = NEW.bridge_id ) BEGIN "
                "SELECT RAISE(ABORT, 'sidebar terminal resolutions overlap precreate "
                "evidence'); END"
            ),
            "trg_session_sidebar_terminal_resolutions_no_unbound_overlap": (
                "CREATE TRIGGER "
                "trg_session_sidebar_terminal_resolutions_no_unbound_overlap "
                "BEFORE INSERT ON session_sidebar_terminal_resolutions WHEN EXISTS ( "
                "SELECT 1 FROM session_sidebar_unbound_resolutions AS existing "
                "WHERE existing.job_id = NEW.job_id OR existing.idempotency_key = "
                "NEW.idempotency_key OR existing.source_session_id = "
                "NEW.source_session_id OR existing.bridge_id = NEW.bridge_id ) BEGIN "
                "SELECT RAISE(ABORT, 'sidebar terminal resolutions overlap unbound "
                "evidence'); END"
            ),
            "trg_session_sidebar_terminal_resolutions_no_v2_attempt_zero_overlap": (
                "CREATE TRIGGER "
                "trg_session_sidebar_terminal_resolutions_no_v2_attempt_zero_overlap "
                "BEFORE INSERT ON session_sidebar_terminal_resolutions WHEN EXISTS ( "
                "SELECT 1 FROM session_sidebar_v2_attempt_zero_resolutions AS existing "
                "WHERE existing.job_id = NEW.job_id OR existing.idempotency_key = "
                "NEW.idempotency_key OR existing.source_session_id = "
                "NEW.source_session_id OR existing.bridge_id = NEW.bridge_id ) BEGIN "
                "SELECT RAISE(ABORT, 'sidebar terminal resolutions overlap v2 "
                "attempt-zero evidence'); END"
            ),
        }
        if {row["name"] for row in triggers} != set(expected_trigger_sql):
            return False
        for trigger in triggers:
            trigger_sql = trigger["sql"]
            if not isinstance(trigger_sql, str):
                return False
            normalized_trigger_sql = " ".join(trigger_sql.split())
            if normalized_trigger_sql != expected_trigger_sql[trigger["name"]]:
                return False
        return SessionBridgeStore._sidebar_precreate_resolution_ledger_is_valid(conn)

    @staticmethod
    def _sidebar_precreate_resolution_ledger_is_valid(
        conn: sqlite3.Connection,
    ) -> bool:
        table = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("session_sidebar_precreate_resolutions",),
        ).fetchone()
        if table is None or not isinstance(table["sql"], str):
            return False
        normalized_table_sql = " ".join(table["sql"].split())
        if any(
            requirement not in normalized_table_sql
            for requirement in _SIDEBAR_PRECREATE_LEDGER_SQL_REQUIREMENTS
        ):
            return False
        columns = conn.execute(
            'PRAGMA table_info("session_sidebar_precreate_resolutions")'
        ).fetchall()
        if tuple(row["name"] for row in columns) != _SIDEBAR_PRECREATE_LEDGER_COLUMNS:
            return False
        if not columns or int(columns[0]["pk"]) != 1:
            return False
        foreign_keys = conn.execute(
            'PRAGMA foreign_key_list("session_sidebar_precreate_resolutions")'
        ).fetchall()
        if len(foreign_keys) != 1:
            return False
        foreign_key = foreign_keys[0]
        if (
            foreign_key["table"] != "session_sidebar_jobs"
            or foreign_key["from"] != "job_id"
            or foreign_key["to"] != "id"
            or foreign_key["on_update"] != "RESTRICT"
            or foreign_key["on_delete"] != "RESTRICT"
        ):
            return False
        triggers = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = 'session_sidebar_precreate_resolutions'"
        ).fetchall()
        expected_trigger_sql = {
            "trg_session_sidebar_precreate_resolutions_no_replacement": (
                "CREATE TRIGGER "
                "trg_session_sidebar_precreate_resolutions_no_replacement "
                "BEFORE INSERT ON session_sidebar_precreate_resolutions WHEN EXISTS ( "
                "SELECT 1 FROM session_sidebar_precreate_resolutions AS existing "
                "WHERE existing.job_id = NEW.job_id OR existing.idempotency_key = "
                "NEW.idempotency_key OR existing.source_session_id = "
                "NEW.source_session_id OR existing.bridge_id = NEW.bridge_id ) OR "
                "EXISTS ( SELECT 1 FROM session_sidebar_terminal_resolutions AS "
                "existing WHERE existing.job_id = NEW.job_id OR "
                "existing.idempotency_key = NEW.idempotency_key OR "
                "existing.source_session_id = NEW.source_session_id OR "
                "existing.bridge_id = NEW.bridge_id ) BEGIN SELECT RAISE(ABORT, "
                "'sidebar precreate resolutions are immutable'); END"
            ),
            "trg_session_sidebar_precreate_resolutions_no_update": (
                "CREATE TRIGGER "
                "trg_session_sidebar_precreate_resolutions_no_update "
                "BEFORE UPDATE ON session_sidebar_precreate_resolutions BEGIN SELECT "
                "RAISE(ABORT, 'sidebar precreate resolutions are immutable'); END"
            ),
            "trg_session_sidebar_precreate_resolutions_no_delete": (
                "CREATE TRIGGER "
                "trg_session_sidebar_precreate_resolutions_no_delete "
                "BEFORE DELETE ON session_sidebar_precreate_resolutions BEGIN SELECT "
                "RAISE(ABORT, 'sidebar precreate resolutions are immutable'); END"
            ),
            "trg_session_sidebar_precreate_resolutions_no_unbound_overlap": (
                "CREATE TRIGGER "
                "trg_session_sidebar_precreate_resolutions_no_unbound_overlap "
                "BEFORE INSERT ON session_sidebar_precreate_resolutions WHEN EXISTS ( "
                "SELECT 1 FROM session_sidebar_unbound_resolutions AS existing "
                "WHERE existing.job_id = NEW.job_id OR existing.idempotency_key = "
                "NEW.idempotency_key OR existing.source_session_id = "
                "NEW.source_session_id OR existing.bridge_id = NEW.bridge_id ) BEGIN "
                "SELECT RAISE(ABORT, 'sidebar precreate resolutions overlap unbound "
                "evidence'); END"
            ),
            "trg_session_sidebar_precreate_resolutions_no_v2_attempt_zero_overlap": (
                "CREATE TRIGGER "
                "trg_session_sidebar_precreate_resolutions_no_v2_attempt_zero_overlap "
                "BEFORE INSERT ON session_sidebar_precreate_resolutions WHEN EXISTS ( "
                "SELECT 1 FROM session_sidebar_v2_attempt_zero_resolutions AS existing "
                "WHERE existing.job_id = NEW.job_id OR existing.idempotency_key = "
                "NEW.idempotency_key OR existing.source_session_id = "
                "NEW.source_session_id OR existing.bridge_id = NEW.bridge_id ) BEGIN "
                "SELECT RAISE(ABORT, 'sidebar precreate resolutions overlap v2 "
                "attempt-zero evidence'); END"
            ),
        }
        if {row["name"] for row in triggers} != set(expected_trigger_sql):
            return False
        for trigger in triggers:
            trigger_sql = trigger["sql"]
            if not isinstance(trigger_sql, str):
                return False
            normalized_trigger_sql = " ".join(trigger_sql.split())
            if normalized_trigger_sql != expected_trigger_sql[trigger["name"]]:
                return False
        return SessionBridgeStore._sidebar_unbound_resolution_ledger_is_valid(conn)

    @staticmethod
    def _sidebar_unbound_resolution_ledger_is_valid(
        conn: sqlite3.Connection,
    ) -> bool:
        table = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("session_sidebar_unbound_resolutions",),
        ).fetchone()
        if table is None or not isinstance(table["sql"], str):
            return False
        normalized_table_sql = " ".join(table["sql"].split())
        if any(
            requirement not in normalized_table_sql
            for requirement in _SIDEBAR_UNBOUND_LEDGER_SQL_REQUIREMENTS
        ):
            return False
        columns = conn.execute(
            'PRAGMA table_info("session_sidebar_unbound_resolutions")'
        ).fetchall()
        if tuple(row["name"] for row in columns) != _SIDEBAR_UNBOUND_LEDGER_COLUMNS:
            return False
        if not columns or int(columns[0]["pk"]) != 1:
            return False
        foreign_keys = conn.execute(
            'PRAGMA foreign_key_list("session_sidebar_unbound_resolutions")'
        ).fetchall()
        if len(foreign_keys) != 1:
            return False
        foreign_key = foreign_keys[0]
        if (
            foreign_key["table"] != "session_sidebar_jobs"
            or foreign_key["from"] != "job_id"
            or foreign_key["to"] != "id"
            or foreign_key["on_update"] != "RESTRICT"
            or foreign_key["on_delete"] != "RESTRICT"
        ):
            return False
        triggers = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = 'session_sidebar_unbound_resolutions'"
        ).fetchall()
        expected_trigger_sql = {
            "trg_session_sidebar_unbound_resolutions_no_replacement": (
                "CREATE TRIGGER "
                "trg_session_sidebar_unbound_resolutions_no_replacement "
                "BEFORE INSERT ON session_sidebar_unbound_resolutions WHEN EXISTS ( "
                "SELECT 1 FROM session_sidebar_unbound_resolutions AS existing "
                "WHERE existing.job_id = NEW.job_id OR existing.idempotency_key = "
                "NEW.idempotency_key OR existing.source_session_id = "
                "NEW.source_session_id OR existing.bridge_id = NEW.bridge_id ) OR "
                "EXISTS ( SELECT 1 FROM session_sidebar_terminal_resolutions AS "
                "existing WHERE existing.job_id = NEW.job_id OR "
                "existing.idempotency_key = NEW.idempotency_key OR "
                "existing.source_session_id = NEW.source_session_id OR "
                "existing.bridge_id = NEW.bridge_id ) OR EXISTS ( SELECT 1 FROM "
                "session_sidebar_precreate_resolutions AS existing WHERE "
                "existing.job_id = NEW.job_id OR existing.idempotency_key = "
                "NEW.idempotency_key OR existing.source_session_id = "
                "NEW.source_session_id OR existing.bridge_id = NEW.bridge_id ) BEGIN "
                "SELECT RAISE(ABORT, 'sidebar unbound resolutions are immutable'); END"
            ),
            "trg_session_sidebar_unbound_resolutions_no_update": (
                "CREATE TRIGGER trg_session_sidebar_unbound_resolutions_no_update "
                "BEFORE UPDATE ON session_sidebar_unbound_resolutions BEGIN SELECT "
                "RAISE(ABORT, 'sidebar unbound resolutions are immutable'); END"
            ),
            "trg_session_sidebar_unbound_resolutions_no_delete": (
                "CREATE TRIGGER trg_session_sidebar_unbound_resolutions_no_delete "
                "BEFORE DELETE ON session_sidebar_unbound_resolutions BEGIN SELECT "
                "RAISE(ABORT, 'sidebar unbound resolutions are immutable'); END"
            ),
            "trg_session_sidebar_unbound_resolutions_no_v2_attempt_zero_overlap": (
                "CREATE TRIGGER "
                "trg_session_sidebar_unbound_resolutions_no_v2_attempt_zero_overlap "
                "BEFORE INSERT ON session_sidebar_unbound_resolutions WHEN EXISTS ( "
                "SELECT 1 FROM session_sidebar_v2_attempt_zero_resolutions AS existing "
                "WHERE existing.job_id = NEW.job_id OR existing.idempotency_key = "
                "NEW.idempotency_key OR existing.source_session_id = "
                "NEW.source_session_id OR existing.bridge_id = NEW.bridge_id ) BEGIN "
                "SELECT RAISE(ABORT, 'sidebar unbound resolutions overlap v2 "
                "attempt-zero evidence'); END"
            ),
        }
        if {row["name"] for row in triggers} != set(expected_trigger_sql):
            return False
        for trigger in triggers:
            trigger_sql = trigger["sql"]
            if not isinstance(trigger_sql, str):
                return False
            if " ".join(trigger_sql.split()) != expected_trigger_sql[trigger["name"]]:
                return False
        return SessionBridgeStore._sidebar_v2_attempt_zero_resolution_ledger_is_valid(
            conn
        )

    @staticmethod
    def _sidebar_v2_attempt_zero_resolution_ledger_is_valid(
        conn: sqlite3.Connection,
    ) -> bool:
        table = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("session_sidebar_v2_attempt_zero_resolutions",),
        ).fetchone()
        if table is None or not isinstance(table["sql"], str):
            return False
        normalized_table_sql = " ".join(table["sql"].split())
        if any(
            requirement not in normalized_table_sql
            for requirement in _SIDEBAR_V2_ATTEMPT_ZERO_LEDGER_SQL_REQUIREMENTS
        ):
            return False
        columns = conn.execute(
            'PRAGMA table_info("session_sidebar_v2_attempt_zero_resolutions")'
        ).fetchall()
        if tuple(row["name"] for row in columns) != _SIDEBAR_V2_ATTEMPT_ZERO_LEDGER_COLUMNS:
            return False
        if not columns or int(columns[0]["pk"]) != 1:
            return False
        foreign_keys = conn.execute(
            'PRAGMA foreign_key_list("session_sidebar_v2_attempt_zero_resolutions")'
        ).fetchall()
        if {
            (row["from"], row["table"], row["to"], row["on_update"], row["on_delete"])
            for row in foreign_keys
        } != {
            ("job_id", "session_sidebar_jobs", "id", "RESTRICT", "RESTRICT"),
            (
                "reservation_reconciliation_proof_digest",
                "session_sidebar_reconciliation_proofs", "proof_digest",
                "RESTRICT", "RESTRICT",
            ),
        }:
            return False
        triggers = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = 'session_sidebar_v2_attempt_zero_resolutions'"
        ).fetchall()
        expected_trigger_sql = {
            "trg_session_sidebar_v2_attempt_zero_resolutions_no_replacement": (
                "CREATE TRIGGER "
                "trg_session_sidebar_v2_attempt_zero_resolutions_no_replacement "
                "BEFORE INSERT ON session_sidebar_v2_attempt_zero_resolutions WHEN "
                "EXISTS ( SELECT 1 FROM "
                "session_sidebar_v2_attempt_zero_resolutions AS existing WHERE "
                "existing.job_id = NEW.job_id OR existing.idempotency_key = "
                "NEW.idempotency_key OR existing.source_session_id = "
                "NEW.source_session_id OR existing.bridge_id = NEW.bridge_id ) OR "
                "EXISTS ( SELECT 1 FROM session_sidebar_terminal_resolutions AS "
                "existing WHERE existing.job_id = NEW.job_id OR "
                "existing.idempotency_key = NEW.idempotency_key OR "
                "existing.source_session_id = NEW.source_session_id OR "
                "existing.bridge_id = NEW.bridge_id ) OR EXISTS ( SELECT 1 FROM "
                "session_sidebar_precreate_resolutions AS existing WHERE "
                "existing.job_id = NEW.job_id OR existing.idempotency_key = "
                "NEW.idempotency_key OR existing.source_session_id = "
                "NEW.source_session_id OR existing.bridge_id = NEW.bridge_id ) OR "
                "EXISTS ( SELECT 1 FROM session_sidebar_unbound_resolutions AS "
                "existing WHERE existing.job_id = NEW.job_id OR "
                "existing.idempotency_key = NEW.idempotency_key OR "
                "existing.source_session_id = NEW.source_session_id OR "
                "existing.bridge_id = NEW.bridge_id ) BEGIN SELECT RAISE(ABORT, "
                "'sidebar v2 attempt-zero resolutions are immutable'); END"
            ),
            "trg_session_sidebar_v2_attempt_zero_resolutions_no_update": (
                "CREATE TRIGGER "
                "trg_session_sidebar_v2_attempt_zero_resolutions_no_update "
                "BEFORE UPDATE ON session_sidebar_v2_attempt_zero_resolutions BEGIN "
                "SELECT RAISE(ABORT, 'sidebar v2 attempt-zero resolutions are "
                "immutable'); END"
            ),
            "trg_session_sidebar_v2_attempt_zero_resolutions_no_delete": (
                "CREATE TRIGGER "
                "trg_session_sidebar_v2_attempt_zero_resolutions_no_delete "
                "BEFORE DELETE ON session_sidebar_v2_attempt_zero_resolutions BEGIN "
                "SELECT RAISE(ABORT, 'sidebar v2 attempt-zero resolutions are "
                "immutable'); END"
            ),
        }
        if {row["name"] for row in triggers} != set(expected_trigger_sql):
            return False
        for trigger in triggers:
            sql = trigger["sql"]
            if not isinstance(sql, str):
                return False
            if " ".join(sql.split()) != expected_trigger_sql[trigger["name"]]:
                return False
        return True

    def sidebar_has_active_lease(self, *, now: float) -> bool:
        """Return whether another worker owns an unexpired durable sidebar lease."""

        checked_at = _finite_number(now, "sidebar active lease time")
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            row = conn.execute(
                """SELECT 1 FROM session_sidebar_jobs
                   WHERE state = ? AND lease_expires_at > ? LIMIT 1""",
                (SidebarJobState.LEASED.value, checked_at),
            ).fetchone()
        return row is not None

    def claim_sidebar_jobs(
        self,
        *,
        now: float,
        limit: int,
        lease_seconds: int = _SIDEBAR_LEASE_SECONDS,
    ) -> list[dict[str, Any]]:
        claim_time = _finite_number(now, "now")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 10
        ):
            raise ValueError("sidebar claim limit must be between 1 and 10")
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or lease_seconds != _SIDEBAR_LEASE_SECONDS
        ):
            raise ValueError("sidebar lease duration must be exactly 300 seconds")

        def _write(conn):
            conn.execute(
                """UPDATE session_sidebar_jobs
                   SET state = ?, next_attempt_at = ?, lease_digest = NULL,
                       lease_expires_at = NULL, error_code = NULL, updated_at = ?
                   WHERE state = ? AND lease_expires_at <= ?""",
                (
                    SidebarJobState.RETRY.value,
                    claim_time,
                    claim_time,
                    SidebarJobState.LEASED.value,
                    claim_time,
                ),
            )
            if self._sidebar_execution_blockers_in_connection(conn):
                return []
            active_lease = conn.execute(
                """SELECT 1 FROM session_sidebar_jobs
                   WHERE state = ? AND lease_expires_at > ? LIMIT 1""",
                (SidebarJobState.LEASED.value, claim_time),
            ).fetchone()
            if active_lease is not None:
                return []
            due = conn.execute(
                """SELECT * FROM session_sidebar_jobs
                   WHERE state IN (?, ?) AND next_attempt_at <= ?
                   ORDER BY CASE WHEN state = ? THEN 0 ELSE 1 END,
                            next_attempt_at, eligible_at, id
                   LIMIT ?""",
                (
                    SidebarJobState.PENDING.value,
                    SidebarJobState.RETRY.value,
                    claim_time,
                    SidebarJobState.RETRY.value,
                    _SIDEBAR_CLAIM_SCAN_LIMIT,
                ),
            ).fetchall()
            found_invalid = False
            for raw_row in due:
                row = dict(raw_row)
                try:
                    _validated_sidebar_job_provider(row)
                except ValueError:
                    conn.execute(
                        """UPDATE session_sidebar_jobs
                           SET state = ?, error_code = ?, updated_at = ?
                           WHERE id = ? AND state = ?""",
                        (
                            SidebarJobState.FAILED.value,
                            "provider_mismatch",
                            claim_time,
                            row["id"],
                            row["state"],
                        ),
                    )
                    found_invalid = True
            if found_invalid:
                return []

            lane_row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?",
                (_SIDEBAR_PENDING_LANE_STATE_KEY,),
            ).fetchone()
            fresh_claims = 0
            if lane_row is not None:
                try:
                    lane_state = json.loads(lane_row["value_json"])
                except (json.JSONDecodeError, TypeError) as exc:
                    raise ValueError("invalid sidebar pending lane state") from exc
                if (
                    not isinstance(lane_state, dict)
                    or set(lane_state) != {
                        "version",
                        "fresh_claims_since_oldest",
                    }
                    or lane_state.get("version") != 1
                    or type(lane_state.get("fresh_claims_since_oldest")) is not int
                    or not 0
                    <= lane_state["fresh_claims_since_oldest"]
                    <= _SIDEBAR_FRESH_BURST
                ):
                    raise ValueError("invalid sidebar pending lane state")
                fresh_claims = lane_state["fresh_claims_since_oldest"]

            claimed: list[dict[str, Any]] = []
            while len(claimed) < limit:
                raw_row = conn.execute(
                    """SELECT * FROM session_sidebar_jobs
                       WHERE state = ? AND next_attempt_at <= ?
                       ORDER BY next_attempt_at, eligible_at, id
                       LIMIT 1""",
                    (SidebarJobState.RETRY.value, claim_time),
                ).fetchone()
                pending_claim = raw_row is None
                if pending_claim:
                    pending_order = (
                        "eligible_at, id"
                        if fresh_claims == _SIDEBAR_FRESH_BURST
                        else "eligible_at DESC, id DESC"
                    )
                    raw_row = conn.execute(
                        f"""SELECT * FROM session_sidebar_jobs
                            WHERE state = ? AND next_attempt_at <= ?
                            ORDER BY {pending_order}
                            LIMIT 1""",
                        (SidebarJobState.PENDING.value, claim_time),
                    ).fetchone()
                if raw_row is None:
                    break
                row = dict(raw_row)
                provider = _validated_sidebar_job_provider(row)
                lease_token, lease_digest = self._new_sidebar_lease(conn)
                lease_expires_at = claim_time + lease_seconds
                cursor = conn.execute(
                    """UPDATE session_sidebar_jobs
                       SET state = ?, lease_digest = ?, lease_expires_at = ?,
                           error_code = NULL, updated_at = ?
                       WHERE id = ? AND state = ? AND attempts = ?""",
                    (
                        SidebarJobState.LEASED.value,
                        lease_digest,
                        lease_expires_at,
                        claim_time,
                        row["id"],
                        row["state"],
                        row["attempts"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("stale sidebar job claim")
                claimed_row = dict(
                    conn.execute(
                        "SELECT * FROM session_sidebar_jobs WHERE id = ?",
                        (row["id"],),
                    ).fetchone()
                )
                claimed_row["provider"] = provider.value
                claimed_row["lease_token"] = lease_token
                claimed.append(claimed_row)
                if pending_claim:
                    fresh_claims = (
                        0
                        if fresh_claims == _SIDEBAR_FRESH_BURST
                        else fresh_claims + 1
                    )
                    lane_json = json.dumps(
                        {
                            "version": 1,
                            "fresh_claims_since_oldest": fresh_claims,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    conn.execute(
                        """INSERT INTO session_bridge_state (
                               key, value_json, updated_at
                           ) VALUES (?, ?, ?)
                           ON CONFLICT(key) DO UPDATE SET
                               value_json = excluded.value_json,
                               updated_at = excluded.updated_at""",
                        (
                            _SIDEBAR_PENDING_LANE_STATE_KEY,
                            lane_json,
                            claim_time,
                        ),
                    )
            return claimed

        return self.db._execute_write(_write)

    def apply_sidebar_create_reservation_cutover(
        self,
        *,
        marker_secret: bytes,
        now: float,
    ) -> dict[str, Any]:
        """One-time quarantine for jobs that predate durable create intent."""

        if type(marker_secret) is not bytes or not marker_secret:
            raise ValueError("sidebar cutover marker secret is malformed")
        applied_at = _finite_number(now, "sidebar create reservation cutover time")

        def _write(conn):
            active_lease = conn.execute(
                """SELECT 1 FROM session_sidebar_jobs
                   WHERE state = ? AND lease_expires_at > ? LIMIT 1""",
                (SidebarJobState.LEASED.value, applied_at),
            ).fetchone()
            if active_lease is not None:
                raise ValueError("active sidebar lease blocks create cutover")

            conn.execute(
                """UPDATE session_sidebar_jobs
                   SET state = ?, next_attempt_at = ?, lease_digest = NULL,
                       lease_expires_at = NULL, error_code = NULL, updated_at = ?
                   WHERE state = ? AND lease_expires_at <= ?""",
                (
                    SidebarJobState.RETRY.value,
                    applied_at,
                    applied_at,
                    SidebarJobState.LEASED.value,
                    applied_at,
                ),
            )

            ledger_row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?",
                (_SIDEBAR_CREATE_RESERVATION_CUTOVER_STATE_KEY,),
            ).fetchone()
            if ledger_row is not None:
                ledger = _decode_sidebar_create_reservation_cutover(
                    ledger_row["value_json"]
                )
                _validate_sidebar_create_reservation_cutover_replay(
                    conn,
                    ledger,
                    marker_secret=marker_secret,
                )
                return ledger, True

            rows = conn.execute(
                """SELECT * FROM session_sidebar_jobs
                   WHERE state IN (?, ?)
                   ORDER BY id""",
                (
                    SidebarJobState.PENDING.value,
                    SidebarJobState.RETRY.value,
                ),
            ).fetchall()
            quarantined_job_ids: list[str] = []
            for raw_row in rows:
                job = dict(raw_row)
                _validated_sidebar_cutover_candidate(conn, job)
                if (
                    job["lease_digest"] is not None
                    or job["lease_expires_at"] is not None
                    or job["completion_digest"] is not None
                    or job["visible_at"] is not None
                ):
                    raise ValueError("sidebar cutover job state is malformed")

                expected_recovery_key = _sidebar_cutover_recovery_key(
                    job,
                    marker_secret=marker_secret,
                )
                state_key = _sidebar_create_reservation_state_key(
                    job["source_session_id"]
                )
                reservation_row = conn.execute(
                    "SELECT value_json FROM session_bridge_state WHERE key = ?",
                    (state_key,),
                ).fetchone()
                if reservation_row is not None:
                    _validate_sidebar_cutover_reservation(
                        reservation_row["value_json"],
                        job=job,
                        expected_recovery_key=expected_recovery_key,
                    )
                    continue
                if job["codex_thread_id"] is not None:
                    _exact_nonempty_text(
                        job["codex_thread_id"],
                        "Codex thread ID",
                    )
                    continue

                pristine = (
                    job["state"] == SidebarJobState.PENDING.value
                    and int(job["attempts"]) == 0
                    and job["error_code"] is None
                    and float(job["next_attempt_at"]) == float(job["eligible_at"])
                    and float(job["updated_at"]) == float(job["created_at"])
                )
                if pristine:
                    continue

                reservation = {
                    "version": 1,
                    "job_id": job["id"],
                    "source_session_id": job["source_session_id"],
                    "bridge_id": job["bridge_id"],
                    "recovery_key": expected_recovery_key,
                    "reserved_at": applied_at,
                }
                conn.execute(
                    """INSERT INTO session_bridge_state (key, value_json, updated_at)
                       VALUES (?, ?, ?)""",
                    (
                        state_key,
                        json.dumps(
                            reservation,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                            allow_nan=False,
                        ),
                        applied_at,
                    ),
                )
                quarantined_job_ids.append(job["id"])

            ledger = {
                "version": 1,
                "applied_at": applied_at,
                "quarantined_job_ids": sorted(quarantined_job_ids),
            }
            conn.execute(
                """INSERT INTO session_bridge_state (key, value_json, updated_at)
                   VALUES (?, ?, ?)""",
                (
                    _SIDEBAR_CREATE_RESERVATION_CUTOVER_STATE_KEY,
                    json.dumps(
                        ledger,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ),
                    applied_at,
                ),
            )
            return ledger, False

        ledger, replayed = self.db._execute_write(_write)
        return {
            "version": 1,
            "applied_at": ledger["applied_at"],
            "quarantined": len(ledger["quarantined_job_ids"]),
            "replayed": replayed,
        }

    def reserve_sidebar_create(
        self,
        *,
        lease_token: str,
        recovery_key: str,
        reconciliation_proof_digest: str,
        reconciliation_generation: str,
        now: float,
    ) -> dict[str, Any]:
        """Persist immutable native-create intent before calling Codex."""

        token_digest = _sidebar_lease_digest(lease_token)
        normalized_key = _sidebar_create_recovery_key(recovery_key)
        normalized_proof_digest = _lowercase_sha256(
            reconciliation_proof_digest,
            "sidebar reconciliation proof digest",
        )
        normalized_generation = _exact_nonempty_text(
            reconciliation_generation,
            "sidebar reconciliation generation",
        )
        reserved_at = _finite_number(now, "sidebar create reservation time")

        def _write(conn):
            job, _ = _find_sidebar_job_by_digest(
                conn,
                token_digest,
                allow_completion=False,
            )
            if job is None:
                raise ValueError("invalid sidebar lease token")
            if float(job["lease_expires_at"]) <= reserved_at:
                _recover_one_expired_sidebar_lease(conn, job, now=reserved_at)
                return None, True
            if job["codex_thread_id"] is not None:
                raise ValueError(
                    "sidebar create reservation already has a native thread"
                )
            current_proof_digest = job["reconciliation_proof_digest"]
            if (
                not isinstance(current_proof_digest, str)
                or not hmac.compare_digest(
                    current_proof_digest,
                    normalized_proof_digest,
                )
            ):
                raise ValueError("sidebar reconciliation proof is stale")
            proof_row = conn.execute(
                """SELECT * FROM session_sidebar_reconciliation_proofs
                   WHERE proof_digest = ?""",
                (normalized_proof_digest,),
            ).fetchone()
            if proof_row is None:
                raise ValueError("sidebar reconciliation proof is missing")
            proof = dict(proof_row)
            if (
                proof["job_id"] != job["id"]
                or proof["source_session_id"] != job["source_session_id"]
                or proof["bridge_id"] != job["bridge_id"]
                or proof["state"] != "absence_proven"
                or proof["match_count"] != 0
                or proof["recovered_thread_id"] is not None
                or float(proof["expires_at"]) <= reserved_at
                or proof["placement_generation"] != 1
                or proof["delivery_generation"] != 1
                or proof["reconciliation_generation"] != normalized_generation
            ):
                raise ValueError("sidebar reconciliation proof is not create eligible")
            payload = {
                "version": 2,
                "job_id": job["id"],
                "source_session_id": job["source_session_id"],
                "bridge_id": job["bridge_id"],
                "recovery_key": normalized_key,
                "reconciliation_proof_digest": normalized_proof_digest,
                "reconciliation_generation": normalized_generation,
                "reserved_at": reserved_at,
            }
            state_key = _sidebar_create_reservation_state_key(job["source_session_id"])
            existing = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?",
                (state_key,),
            ).fetchone()
            if existing is not None:
                decoded = _decode_sidebar_create_reservation(
                    existing["value_json"],
                    expected_source_session_id=job["source_session_id"],
                )
                if (
                    decoded["version"] != 2
                    or decoded["job_id"] != job["id"]
                    or decoded["bridge_id"] != job["bridge_id"]
                    or not hmac.compare_digest(decoded["recovery_key"], normalized_key)
                    or not hmac.compare_digest(
                        decoded["reconciliation_proof_digest"],
                        normalized_proof_digest,
                    )
                    or decoded["reconciliation_generation"]
                    != normalized_generation
                ):
                    raise ValueError("conflicting sidebar create reservation")
                return decoded, False
            value_json = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            conn.execute(
                """INSERT INTO session_bridge_state (key, value_json, updated_at)
                   VALUES (?, ?, ?)""",
                (state_key, value_json, reserved_at),
            )
            return payload, False

        result, expired = self.db._execute_write(_write)
        if expired:
            raise ValueError("sidebar lease has expired")
        assert result is not None
        return result

    def get_sidebar_create_reservation(
        self,
        source_session_id: str,
    ) -> dict[str, Any] | None:
        source_id = _exact_nonempty_text(source_session_id, "sidebar source session ID")
        state_key = _sidebar_create_reservation_state_key(source_id)
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?",
                (state_key,),
            ).fetchone()
            if row is None:
                return None
            reservation = _decode_sidebar_create_reservation(
                row["value_json"],
                expected_source_session_id=source_id,
            )
            job = conn.execute(
                "SELECT id, bridge_id FROM session_sidebar_jobs WHERE source_session_id = ?",
                (source_id,),
            ).fetchone()
        if (
            job is None
            or job["id"] != reservation["job_id"]
            or job["bridge_id"] != reservation["bridge_id"]
        ):
            raise ValueError("invalid sidebar create reservation identity")
        return reservation

    def record_sidebar_reconciliation_proof(
        self,
        *,
        lease_token: str,
        evidence: SidebarReconciliationEvidence,
        marker_digest: str,
        placement_generation: int,
        delivery_generation: int,
        now: float,
    ) -> dict[str, Any]:
        """Append one authoritative proof and bind it to the current lease."""

        token_digest = _sidebar_lease_digest(lease_token)
        if not isinstance(evidence, SidebarReconciliationEvidence):
            raise TypeError("sidebar reconciliation evidence is malformed")
        evidence.validate()
        normalized_marker_digest = _lowercase_sha256(
            marker_digest,
            "sidebar reconciliation marker digest",
        )
        if not hmac.compare_digest(
            evidence.marker_digest,
            normalized_marker_digest,
        ):
            raise ValueError("sidebar reconciliation marker digest mismatch")
        if type(placement_generation) is not int or placement_generation <= 0:
            raise ValueError("sidebar placement generation is malformed")
        if type(delivery_generation) is not int or delivery_generation <= 0:
            raise ValueError("sidebar delivery generation is malformed")
        recorded_at = _finite_number(now, "sidebar reconciliation proof time")
        if evidence.expires_at <= recorded_at:
            raise ValueError("sidebar reconciliation evidence has expired")

        def _write(conn):
            job, _ = _find_sidebar_job_by_digest(
                conn,
                token_digest,
                allow_completion=False,
            )
            if job is None:
                raise ValueError("invalid sidebar lease token")
            if float(job["lease_expires_at"]) <= recorded_at:
                _recover_one_expired_sidebar_lease(conn, job, now=recorded_at)
                return None, True
            proof_input = SidebarReconciliationProofInput(
                job_id=job["id"],
                source_session_id=job["source_session_id"],
                bridge_id=job["bridge_id"],
                marker_digest=normalized_marker_digest,
                placement_generation=placement_generation,
                delivery_generation=delivery_generation,
                reconciliation_generation=evidence.generation,
                completed_at=evidence.completed_at,
                expires_at=evidence.expires_at,
                inventory_digest=evidence.inventory_digest,
                state=evidence.state,
                match_count=evidence.match_count,
                recovered_thread_id=evidence.recovered_thread_id,
                fixed_reason=evidence.fixed_reason,
            )
            proof_digest = sidebar_reconciliation_proof_digest(proof_input)
            proof = {
                "proof_digest": proof_digest,
                "job_id": proof_input.job_id,
                "source_session_id": proof_input.source_session_id,
                "bridge_id": proof_input.bridge_id,
                "marker_digest": proof_input.marker_digest,
                "placement_generation": proof_input.placement_generation,
                "delivery_generation": proof_input.delivery_generation,
                "reconciliation_generation": proof_input.reconciliation_generation,
                "completed_at": proof_input.completed_at,
                "expires_at": proof_input.expires_at,
                "inventory_digest": proof_input.inventory_digest,
                "state": proof_input.state.value,
                "match_count": proof_input.match_count,
                "recovered_thread_id": proof_input.recovered_thread_id,
                "fixed_reason": proof_input.fixed_reason,
                "created_at": recorded_at,
            }
            conn.execute(
                """INSERT INTO session_sidebar_reconciliation_proofs (
                       proof_digest, job_id, source_session_id, bridge_id,
                       marker_digest, placement_generation, delivery_generation,
                       reconciliation_generation, completed_at, expires_at,
                       inventory_digest, state, match_count, recovered_thread_id,
                       fixed_reason, created_at
                   ) VALUES (
                       :proof_digest, :job_id, :source_session_id, :bridge_id,
                       :marker_digest, :placement_generation, :delivery_generation,
                       :reconciliation_generation, :completed_at, :expires_at,
                       :inventory_digest, :state, :match_count,
                       :recovered_thread_id, :fixed_reason, :created_at
                   ) ON CONFLICT(proof_digest) DO NOTHING""",
                proof,
            )
            persisted = conn.execute(
                """SELECT * FROM session_sidebar_reconciliation_proofs
                   WHERE proof_digest = ?""",
                (proof_digest,),
            ).fetchone()
            if persisted is None:
                raise ValueError("sidebar reconciliation proof was not persisted")
            persisted_proof = dict(persisted)
            comparison = dict(proof)
            comparison["created_at"] = persisted_proof["created_at"]
            if persisted_proof != comparison:
                raise ValueError("conflicting sidebar reconciliation proof replay")
            cursor = conn.execute(
                """UPDATE session_sidebar_jobs
                   SET reconciliation_proof_digest = ?, updated_at = ?
                   WHERE id = ? AND state = ? AND lease_digest = ?""",
                (
                    proof_digest,
                    recorded_at,
                    job["id"],
                    SidebarJobState.LEASED.value,
                    token_digest,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("stale sidebar reconciliation proof lease")
            return persisted_proof, False

        proof, expired = self.db._execute_write(_write)
        if expired:
            raise ValueError("sidebar lease has expired")
        assert proof is not None
        return proof

    def get_sidebar_reconciliation_proof(
        self,
        *,
        lease_token: str,
    ) -> dict[str, Any] | None:
        """Read the proof currently bound to one exact active lease."""

        token_digest = _sidebar_lease_digest(lease_token)
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            job, _ = _find_sidebar_job_by_digest(
                conn,
                token_digest,
                allow_completion=False,
            )
            if job is None:
                raise ValueError("invalid sidebar lease token")
            proof_digest = job["reconciliation_proof_digest"]
            if proof_digest is None:
                return None
            proof = conn.execute(
                """SELECT * FROM session_sidebar_reconciliation_proofs
                   WHERE proof_digest = ?""",
                (proof_digest,),
            ).fetchone()
            if proof is None:
                raise ValueError("missing sidebar reconciliation proof")
            result = dict(proof)
            if (
                result["job_id"] != job["id"]
                or result["source_session_id"] != job["source_session_id"]
                or result["bridge_id"] != job["bridge_id"]
            ):
                raise ValueError("invalid sidebar reconciliation proof identity")
            return result

    def get_sidebar_reconciliation_proof_by_digest(
        self,
        proof_digest: object,
    ) -> dict[str, Any] | None:
        """Read one exact persisted reconciliation proof by authoritative digest."""

        normalized_digest = _lowercase_sha256(
            proof_digest,
            "sidebar reconciliation proof digest",
        )
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            proof = conn.execute(
                "SELECT * FROM session_sidebar_reconciliation_proofs "
                "WHERE proof_digest = ?",
                (normalized_digest,),
            ).fetchone()
            return None if proof is None else dict(proof)

    def clear_sidebar_create_reservation(
        self,
        *,
        lease_token: str,
        recovery_key: str,
        now: float,
    ) -> None:
        """Clear intent only after a conclusive pre-dispatch rejection."""

        token_digest = _sidebar_lease_digest(lease_token)
        normalized_key = _sidebar_create_recovery_key(recovery_key)
        cleared_at = _finite_number(now, "sidebar create reservation clear time")

        def _write(conn):
            job, _ = _find_sidebar_job_by_digest(
                conn,
                token_digest,
                allow_completion=False,
            )
            if job is None:
                raise ValueError("invalid sidebar lease token")
            if float(job["lease_expires_at"]) <= cleared_at:
                _recover_one_expired_sidebar_lease(conn, job, now=cleared_at)
                return True
            state_key = _sidebar_create_reservation_state_key(job["source_session_id"])
            row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?",
                (state_key,),
            ).fetchone()
            if row is None:
                return False
            reservation = _decode_sidebar_create_reservation(
                row["value_json"],
                expected_source_session_id=job["source_session_id"],
            )
            if (
                reservation["job_id"] != job["id"]
                or reservation["bridge_id"] != job["bridge_id"]
                or not hmac.compare_digest(reservation["recovery_key"], normalized_key)
            ):
                raise ValueError("conflicting sidebar create reservation")
            conn.execute(
                "DELETE FROM session_bridge_state WHERE key = ?",
                (state_key,),
            )
            return False

        expired = self.db._execute_write(_write)
        if expired:
            raise ValueError("sidebar lease has expired")

    def bind_sidebar_thread(
        self,
        *,
        lease_token: str,
        codex_thread_id: str,
        now: float,
    ) -> dict[str, Any]:
        """Durably reserve one native task identity before rename or commit."""

        token_digest = _sidebar_lease_digest(lease_token)
        thread_id = _exact_nonempty_text(codex_thread_id, "Codex thread ID")
        bind_time = _finite_number(now, "now")

        def _write(conn):
            job, _ = _find_sidebar_job_by_digest(
                conn,
                token_digest,
                allow_completion=False,
            )
            if job is None:
                raise ValueError("invalid sidebar lease token")
            job = _persist_sidebar_thread_identity(
                conn,
                job,
                thread_id=thread_id,
                now=bind_time,
            )
            if float(job["lease_expires_at"]) <= bind_time:
                _recover_one_expired_sidebar_lease(conn, job, now=bind_time)
                return dict(job), True
            return dict(job), False

        result, expired = self.db._execute_write(_write)
        if expired:
            raise ValueError("sidebar lease has expired")
        return result

    def commit_sidebar_job(
        self,
        *,
        lease_token: str,
        codex_thread_id: str,
        now: float,
    ) -> dict[str, Any]:
        token_digest = _sidebar_lease_digest(lease_token)
        thread_id = _exact_nonempty_text(codex_thread_id, "Codex thread ID")
        commit_time = _finite_number(now, "now")

        def _write(conn):
            job, matched_completion = _find_sidebar_job_by_digest(
                conn,
                token_digest,
                allow_completion=True,
            )
            if job is None:
                raise ValueError("invalid sidebar lease token")
            if matched_completion:
                if (
                    job["state"] == SidebarJobState.VISIBLE.value
                    and job["codex_thread_id"] == thread_id
                ):
                    return dict(job), False
                raise ValueError("conflicting sidebar completion replay")
            if job["state"] != SidebarJobState.LEASED.value:
                raise ValueError("sidebar job is not leased")
            if float(job["lease_expires_at"]) <= commit_time:
                _recover_one_expired_sidebar_lease(conn, job, now=commit_time)
                return dict(job), True
            conflict = conn.execute(
                """SELECT id FROM session_sidebar_jobs
                   WHERE codex_thread_id = ? AND id != ?""",
                (thread_id, job["id"]),
            ).fetchone()
            if conflict is not None:
                raise ValueError("conflicting Codex thread identity")
            cursor = conn.execute(
                """UPDATE session_sidebar_jobs
                   SET state = ?, completion_digest = lease_digest,
                       lease_digest = NULL, lease_expires_at = NULL,
                       codex_thread_id = ?, error_code = NULL,
                       visible_at = ?, updated_at = ?
                   WHERE id = ? AND state = ?""",
                (
                    SidebarJobState.VISIBLE.value,
                    thread_id,
                    commit_time,
                    commit_time,
                    job["id"],
                    SidebarJobState.LEASED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("stale sidebar job commit")
            return (
                dict(
                    conn.execute(
                        "SELECT * FROM session_sidebar_jobs WHERE id = ?",
                        (job["id"],),
                    ).fetchone()
                ),
                False,
            )

        result, expired = self.db._execute_write(_write)
        if expired:
            raise ValueError("sidebar lease has expired")
        return result

    def commit_sidebar_job_with_lineage(
        self,
        *,
        lease_token: str,
        codex_thread_id: str,
        source_session_id: str,
        bridge_id: str,
        placement_generation: int,
        now: float,
    ) -> dict[str, Any]:
        """Atomically bind verified lineage and commit one sidebar lease."""

        token_digest = _sidebar_lease_digest(lease_token)
        thread_id = _exact_nonempty_text(codex_thread_id, "Codex thread ID")
        source_id = _exact_nonempty_text(source_session_id, "sidebar source session ID")
        normalized_bridge_id = _exact_nonempty_text(bridge_id, "sidebar bridge ID")
        if type(placement_generation) is not int or placement_generation < 1:
            raise ValueError("sidebar placement generation must be a positive integer")
        commit_time = _finite_number(now, "now")

        def _write(conn):
            job, matched_completion = _find_sidebar_job_by_digest(
                conn,
                token_digest,
                allow_completion=True,
            )
            if job is None:
                raise ValueError("invalid sidebar lease token")
            if (
                job["source_session_id"] != source_id
                or job["bridge_id"] != normalized_bridge_id
            ):
                raise ValueError("source_identity_mismatch")
            if matched_completion:
                if (
                    job["state"] != SidebarJobState.VISIBLE.value
                    or job["codex_thread_id"] != thread_id
                    or job["placement_generation"] != placement_generation
                    or job["placement_verified_at"] is None
                ):
                    raise ValueError("conflicting sidebar completion replay")
                _ensure_sidebar_lineage_row(
                    conn,
                    source_session_id=source_id,
                    bridge_id=normalized_bridge_id,
                    codex_thread_id=thread_id,
                    created_at=commit_time,
                )
                return dict(job), False
            if job["state"] != SidebarJobState.LEASED.value:
                raise ValueError("sidebar job is not leased")
            if float(job["lease_expires_at"]) <= commit_time:
                _recover_one_expired_sidebar_lease(conn, job, now=commit_time)
                return dict(job), True
            conflict = conn.execute(
                """SELECT id FROM session_sidebar_jobs
                   WHERE codex_thread_id = ? AND id != ?""",
                (thread_id, job["id"]),
            ).fetchone()
            if conflict is not None:
                raise ValueError("conflicting Codex thread identity")
            _ensure_sidebar_lineage_row(
                conn,
                source_session_id=source_id,
                bridge_id=normalized_bridge_id,
                codex_thread_id=thread_id,
                created_at=commit_time,
            )
            cursor = conn.execute(
                """UPDATE session_sidebar_jobs
                   SET state = ?, completion_digest = lease_digest,
                       lease_digest = NULL, lease_expires_at = NULL,
                       codex_thread_id = ?, error_code = NULL,
                       visible_at = ?, updated_at = ?,
                       placement_generation = ?, placement_verified_at = ?
                   WHERE id = ? AND state = ?""",
                (
                    SidebarJobState.VISIBLE.value,
                    thread_id,
                    commit_time,
                    commit_time,
                    placement_generation,
                    commit_time,
                    job["id"],
                    SidebarJobState.LEASED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("stale sidebar job commit")
            return (
                dict(
                    conn.execute(
                        "SELECT * FROM session_sidebar_jobs WHERE id = ?",
                        (job["id"],),
                    ).fetchone()
                ),
                False,
            )

        result, expired = self.db._execute_write(_write)
        if expired:
            raise ValueError("sidebar lease has expired")
        return result

    def record_sidebar_placement_canary(
        self,
        *,
        status: str,
        placement_generation: int,
        verified_at: float,
        canary_identity: str,
    ) -> dict[str, Any]:
        if status not in {"passed", "failed"}:
            raise ValueError("sidebar placement canary status is invalid")
        if type(placement_generation) is not int or placement_generation < 1:
            raise ValueError("sidebar placement generation must be a positive integer")
        timestamp = _finite_number(
            verified_at,
            "sidebar placement canary verified_at",
        )
        if timestamp < 0:
            raise ValueError(
                "sidebar placement canary verified_at must be nonnegative"
            )
        identity = _exact_nonempty_text(
            canary_identity,
            "sidebar placement canary identity",
        )
        digest = hashlib.sha256(
            _SIDEBAR_PLACEMENT_CANARY_DIGEST_DOMAIN + identity.encode("utf-8")
        ).hexdigest()
        state = {
            "version": 1,
            "status": status,
            "placement_generation": placement_generation,
            "verified_at": timestamp,
            "canary_identity_digest": digest,
        }
        self.set_state(_SIDEBAR_PLACEMENT_CANARY_STATE_KEY, state)
        return dict(state)

    def lookup_sidebar_job_by_lease(self, lease_token: str) -> dict[str, Any]:
        token_digest = _sidebar_lease_digest(lease_token)
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            job, matched_completion = _find_sidebar_job_by_digest(
                conn,
                token_digest,
                allow_completion=True,
            )
            if job is None:
                raise ValueError("invalid sidebar lease token")
            state = job["state"]
            if matched_completion:
                if state != SidebarJobState.VISIBLE.value:
                    raise ValueError("invalid sidebar completion state")
            elif state != SidebarJobState.LEASED.value:
                raise ValueError("sidebar job is not leased")
            return {
                "id": job["id"],
                "source_session_id": job["source_session_id"],
                "bridge_id": job["bridge_id"],
                "state": state,
                "codex_thread_id": job["codex_thread_id"],
            }

    def fail_sidebar_job(
        self,
        *,
        lease_token: str,
        error_code: str,
        now: float,
        codex_thread_id: str | None = None,
    ) -> dict[str, Any]:
        if (
            type(error_code) is not str
            or error_code not in SIDEBAR_RETRYABLE_ERRORS | SIDEBAR_FATAL_ERRORS
        ):
            raise ValueError("sidebar error code is not in the fixed allowlist")
        token_digest = _sidebar_lease_digest(lease_token)
        failure_time = _finite_number(now, "now")
        thread_id = (
            None
            if codex_thread_id is None
            else _exact_nonempty_text(codex_thread_id, "Codex thread ID")
        )

        def _write(conn):
            job, _ = _find_sidebar_job_by_digest(
                conn,
                token_digest,
                allow_completion=False,
            )
            if job is None:
                raise ValueError("invalid sidebar lease token")
            if thread_id is not None:
                job = _persist_sidebar_thread_identity(
                    conn,
                    job,
                    thread_id=thread_id,
                    now=failure_time,
                )
            if float(job["lease_expires_at"]) <= failure_time:
                _recover_one_expired_sidebar_lease(conn, job, now=failure_time)
                return dict(job), True
            if error_code == "broker_time_budget":
                cursor = conn.execute(
                    """UPDATE session_sidebar_jobs
                       SET state = ?, next_attempt_at = ?, lease_digest = NULL,
                           lease_expires_at = NULL, error_code = ?, updated_at = ?
                       WHERE id = ? AND state = ?""",
                    (
                        SidebarJobState.PENDING.value,
                        failure_time,
                        error_code,
                        failure_time,
                        job["id"],
                        SidebarJobState.LEASED.value,
                    ),
                )
            elif error_code in SIDEBAR_FATAL_ERRORS:
                cursor = conn.execute(
                    """UPDATE session_sidebar_jobs
                       SET state = ?, lease_digest = NULL,
                           lease_expires_at = NULL, error_code = ?, updated_at = ?
                       WHERE id = ? AND state = ?""",
                    (
                        SidebarJobState.FAILED.value,
                        error_code,
                        failure_time,
                        job["id"],
                        SidebarJobState.LEASED.value,
                    ),
                )
            else:
                attempts = int(job["attempts"]) + 1
                base_delay = _SIDEBAR_RETRY_DELAYS_SECONDS[attempts - 1]
                jitter_bound = min(30.0, base_delay * 0.1)
                jitter = _finite_number(
                    self._sidebar_jitter(jitter_bound),
                    "sidebar retry jitter",
                )
                if not 0.0 <= jitter <= jitter_bound:
                    raise ValueError("sidebar retry jitter is outside its bound")
                next_attempt_at = failure_time + base_delay + jitter
                state = (
                    SidebarJobState.FAILED
                    if attempts >= len(_SIDEBAR_RETRY_DELAYS_SECONDS)
                    else SidebarJobState.RETRY
                )
                cursor = conn.execute(
                    """UPDATE session_sidebar_jobs
                       SET state = ?, attempts = ?, next_attempt_at = ?,
                           lease_digest = NULL, lease_expires_at = NULL,
                           error_code = ?, updated_at = ?
                       WHERE id = ? AND state = ?""",
                    (
                        state.value,
                        attempts,
                        next_attempt_at,
                        error_code,
                        failure_time,
                        job["id"],
                        SidebarJobState.LEASED.value,
                    ),
                )
            if cursor.rowcount != 1:
                raise ValueError("stale sidebar job failure")
            return (
                dict(
                    conn.execute(
                        "SELECT * FROM session_sidebar_jobs WHERE id = ?",
                        (job["id"],),
                    ).fetchone()
                ),
                False,
            )

        result, expired = self.db._execute_write(_write)
        if expired:
            raise ValueError("sidebar lease has expired")
        return result

    def release_sidebar_job(
        self,
        *,
        lease_token: str,
        now: float,
    ) -> dict[str, Any]:
        token_digest = _sidebar_lease_digest(lease_token)
        release_time = _finite_number(now, "now")

        def _write(conn):
            job, _ = _find_sidebar_job_by_digest(
                conn,
                token_digest,
                allow_completion=False,
            )
            if job is None:
                raise ValueError("invalid sidebar lease token")
            if float(job["lease_expires_at"]) <= release_time:
                _recover_one_expired_sidebar_lease(conn, job, now=release_time)
                return dict(job), True
            cursor = conn.execute(
                """UPDATE session_sidebar_jobs
                   SET state = ?, next_attempt_at = ?, lease_digest = NULL,
                       lease_expires_at = NULL, error_code = NULL, updated_at = ?
                   WHERE id = ? AND state = ?""",
                (
                    SidebarJobState.PENDING.value,
                    release_time,
                    release_time,
                    job["id"],
                    SidebarJobState.LEASED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("stale sidebar job release")
            return (
                dict(
                    conn.execute(
                        "SELECT * FROM session_sidebar_jobs WHERE id = ?",
                        (job["id"],),
                    ).fetchone()
                ),
                False,
            )

        result, expired = self.db._execute_write(_write)
        if expired:
            raise ValueError("sidebar lease has expired")
        return result

    def acknowledge_sidebar_terminal_resolution(
        self,
        *,
        job_id: object,
        codex_thread_id: object,
        expected_error_code: object,
        expected_attempts: object,
        expected_next_attempt_at: object,
        expected_updated_at: object,
        evidence_digest: object,
        now: object,
    ) -> dict[str, Any]:
        """Append exact evidence for one unrecoverable bound native thread."""

        expected_job_id = _exact_nonempty_text(job_id, "sidebar job ID")
        expected_thread_id = _exact_nonempty_text(codex_thread_id, "Codex thread ID")
        expected_error = _exact_nonempty_text(
            expected_error_code, "expected sidebar failure"
        )
        if expected_error != "native_create_ambiguous":
            raise ValueError("expected sidebar failure does not match")
        _nonnegative_integer(expected_attempts, "expected sidebar attempts")
        attempts = cast(int, expected_attempts)
        next_attempt_at = _finite_number(
            expected_next_attempt_at, "expected sidebar next attempt"
        )
        updated_at = _finite_number(expected_updated_at, "expected sidebar update time")
        evidence = _sha256_text(
            evidence_digest, "sidebar terminal resolution evidence digest"
        )
        resolved_at = _finite_number(now, "sidebar terminal resolution time")

        def _write(conn: sqlite3.Connection) -> dict[str, Any]:
            if not self._sidebar_terminal_resolution_ledger_is_valid(conn):
                raise ValueError("invalid sidebar terminal resolution ledger")
            job = conn.execute(
                "SELECT * FROM session_sidebar_jobs WHERE id = ?",
                (expected_job_id,),
            ).fetchone()
            if job is None:
                raise ValueError("expected sidebar terminal resolution does not match")

            from .sidebar import sidebar_bridge_id, sidebar_idempotency_key

            source_session_id = _exact_nonempty_text(
                job["source_session_id"], "sidebar source session ID"
            )
            idempotency_key = sidebar_idempotency_key(source_session_id)
            bridge_id = sidebar_bridge_id(source_session_id)
            canonical_job_id = (
                "sidebar-job:"
                + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
            )
            siblings = conn.execute(
                "SELECT id FROM session_sidebar_jobs "
                "WHERE source_session_id = ? ORDER BY id LIMIT 2",
                (source_session_id,),
            ).fetchall()
            if (
                canonical_job_id != expected_job_id
                or job["idempotency_key"] != idempotency_key
                or job["bridge_id"] != bridge_id
                or len(siblings) != 1
                or siblings[0]["id"] != expected_job_id
            ):
                raise ValueError("expected sidebar terminal resolution does not match")

            reservation_row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?",
                (_sidebar_create_reservation_state_key(source_session_id),),
            ).fetchone()
            if reservation_row is None:
                raise ValueError("expected sidebar terminal resolution does not match")
            reservation = _decode_sidebar_create_reservation(
                reservation_row["value_json"],
                expected_source_session_id=source_session_id,
            )
            if (
                reservation["job_id"] != expected_job_id
                or reservation["bridge_id"] != bridge_id
            ):
                raise ValueError("expected sidebar terminal resolution does not match")

            if (
                job["codex_thread_id"] != expected_thread_id
                or job["state"] != SidebarJobState.FAILED.value
                or job["error_code"] != expected_error
                or job["attempts"] != attempts
                or job["next_attempt_at"] != next_attempt_at
                or job["updated_at"] != updated_at
                or job["lease_digest"] is not None
                or job["lease_expires_at"] is not None
                or job["completion_digest"] is not None
                or job["visible_at"] is not None
            ):
                raise ValueError("expected sidebar terminal resolution does not match")
            materialized = conn.execute(
                """SELECT 1
                     WHERE EXISTS (
                         SELECT 1 FROM external_sessions AS external
                          WHERE external.provider = ?
                            AND external.native_id = ?
                     )
                        OR EXISTS (
                         SELECT 1 FROM session_links AS link
                          WHERE link.bridge_id = ?
                     )
                        OR EXISTS (
                         SELECT 1 FROM session_sidebar_exclusions AS exclusion
                          WHERE exclusion.source_session_id = ?
                     )""",
                (
                    Provider.CODEX.value,
                    expected_thread_id,
                    bridge_id,
                    source_session_id,
                ),
            ).fetchone()
            if materialized is not None:
                raise ValueError("expected sidebar terminal resolution does not match")

            canonical_evidence = sidebar_terminal_evidence_digest(
                job=dict(job),
                reservation=reservation,
            )
            if not hmac.compare_digest(evidence, canonical_evidence):
                raise ValueError("sidebar terminal resolution evidence does not match")
            if resolved_at < updated_at:
                raise ValueError("sidebar terminal resolution time precedes failure")

            expected_fields = {
                "job_id": expected_job_id,
                "idempotency_key": idempotency_key,
                "source_session_id": source_session_id,
                "bridge_id": bridge_id,
                "codex_thread_id": expected_thread_id,
                "failure_state": SidebarJobState.FAILED.value,
                "failure_code": expected_error,
                "failure_attempts": attempts,
                "failure_next_attempt_at": next_attempt_at,
                "failure_updated_at": updated_at,
                "resolution_code": SIDEBAR_TERMINAL_RESOLUTION_CODE,
                "evidence_kind": SIDEBAR_TERMINAL_EVIDENCE_KIND,
                "evidence_version": SIDEBAR_TERMINAL_EVIDENCE_VERSION,
                "evidence_digest": canonical_evidence,
            }
            resolution = conn.execute(
                "SELECT * FROM session_sidebar_terminal_resolutions WHERE job_id = ?",
                (expected_job_id,),
            ).fetchone()
            if resolution is not None:
                if any(
                    resolution[key] != value for key, value in expected_fields.items()
                ):
                    raise ValueError("conflicting sidebar terminal resolution")
                return {
                    "job_id": expected_job_id,
                    "state": SidebarJobState.FAILED.value,
                    "error_code": expected_error,
                    "resolution_code": SIDEBAR_TERMINAL_RESOLUTION_CODE,
                    "created": False,
                }

            try:
                cursor = conn.execute(
                    """INSERT INTO session_sidebar_terminal_resolutions (
                       job_id, idempotency_key, source_session_id, bridge_id,
                       codex_thread_id, failure_state, failure_code,
                       failure_attempts, failure_next_attempt_at,
                       failure_updated_at, resolution_code, evidence_kind,
                       evidence_version, evidence_digest, resolved_at
                   )
                   SELECT job.id, job.idempotency_key, job.source_session_id,
                          job.bridge_id, job.codex_thread_id, job.state,
                          job.error_code, job.attempts, job.next_attempt_at,
                          job.updated_at, ?, ?, ?, ?, ?
                     FROM session_sidebar_jobs AS job
                    WHERE job.id = ?
                      AND job.idempotency_key = ?
                      AND job.source_session_id = ?
                      AND job.bridge_id = ?
                      AND job.codex_thread_id = ?
                      AND job.state = ?
                      AND job.error_code = ?
                      AND job.attempts = ?
                      AND job.next_attempt_at = ?
                      AND job.updated_at = ?
                      AND job.lease_digest IS NULL
                      AND job.lease_expires_at IS NULL
                      AND job.completion_digest IS NULL
                      AND job.visible_at IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM external_sessions AS external
                           WHERE external.provider = ?
                             AND external.native_id = job.codex_thread_id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM session_links AS link
                           WHERE link.bridge_id = job.bridge_id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM session_sidebar_exclusions AS exclusion
                           WHERE exclusion.source_session_id = job.source_session_id
                      )""",
                    (
                        SIDEBAR_TERMINAL_RESOLUTION_CODE,
                        SIDEBAR_TERMINAL_EVIDENCE_KIND,
                        SIDEBAR_TERMINAL_EVIDENCE_VERSION,
                        canonical_evidence,
                        resolved_at,
                        expected_job_id,
                        idempotency_key,
                        source_session_id,
                        bridge_id,
                        expected_thread_id,
                        SidebarJobState.FAILED.value,
                        expected_error,
                        attempts,
                        next_attempt_at,
                        updated_at,
                        Provider.CODEX.value,
                    ),
                )
            except sqlite3.IntegrityError:
                raise ValueError("conflicting sidebar terminal resolution") from None
            if cursor.rowcount != 1:
                raise ValueError("expected sidebar terminal resolution does not match")
            resolution = conn.execute(
                "SELECT * FROM session_sidebar_terminal_resolutions WHERE job_id = ?",
                (expected_job_id,),
            ).fetchone()
            if resolution is None:
                raise ValueError("expected sidebar terminal resolution does not match")
            if (
                any(resolution[key] != value for key, value in expected_fields.items())
                or resolution["resolved_at"] != resolved_at
            ):
                raise ValueError("conflicting sidebar terminal resolution")
            return {
                "job_id": expected_job_id,
                "state": SidebarJobState.FAILED.value,
                "error_code": expected_error,
                "resolution_code": SIDEBAR_TERMINAL_RESOLUTION_CODE,
                "created": True,
            }

        return self.db._execute_write(_write)

    def record_claude_visibility_characterization(
        self,
        *,
        job_id: str,
        operation_id: str,
        source_session_id: str,
        bridge_id: str,
        idempotency_key: str,
        reserved_claude_uuid: str,
        native_name: str,
        source_cwd: str,
        signed_marker: str,
        evidence_digest: str,
        marker_secret: bytes,
        cleanup_completed: bool,
        launch_aborted: bool = False,
    ) -> dict[str, Any]:
        """Append authenticated lifecycle evidence for one disposable live probe.

        Characterization rows deliberately use a synthetic Codex source and must
        never enter production catalog lineage.  The event ledger preserves the
        exact job/UUID binding while allowing an explicitly cleaned probe to stop
        counting as a durable visible mirror.
        """

        normalized_job = _exact_nonempty_text(job_id, "Claude visibility job ID")
        normalized_operation = _exact_nonempty_text(
            operation_id, "Claude characterization operation ID"
        )
        if (
            re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                normalized_operation,
            )
            is None
        ):
            raise ValueError("Claude characterization operation ID is invalid")
        normalized_source = _exact_nonempty_text(
            source_session_id, "Claude characterization source session ID"
        )
        normalized_bridge = _exact_nonempty_text(
            bridge_id, "Claude characterization bridge ID"
        )
        normalized_idempotency = _exact_nonempty_text(
            idempotency_key, "Claude characterization idempotency key"
        )
        normalized_uuid = _exact_nonempty_text(
            reserved_claude_uuid, "reserved Claude UUID"
        )
        normalized_name = _exact_nonempty_text(
            native_name, "Claude characterization native name"
        )
        normalized_cwd = _exact_nonempty_text(
            source_cwd, "Claude characterization source cwd"
        )
        normalized_marker = _exact_nonempty_text(
            signed_marker, "Claude characterization signed marker"
        )
        normalized_evidence = _exact_nonempty_text(
            evidence_digest, "Claude characterization evidence digest"
        )
        if re.fullmatch(r"[0-9a-f]{64}", normalized_evidence) is None:
            raise ValueError(
                "Claude characterization evidence digest must be lowercase SHA-256"
            )
        if not isinstance(marker_secret, bytes) or not marker_secret:
            raise ValueError("Claude characterization marker secret must be nonempty")
        if type(cleanup_completed) is not bool:
            raise ValueError("Claude characterization cleanup flag must be boolean")
        if type(launch_aborted) is not bool:
            raise ValueError("Claude characterization abort flag must be boolean")
        if cleanup_completed and launch_aborted:
            raise ValueError(
                "Claude characterization cleanup and abort are mutually exclusive"
            )
        if normalized_source != f"codex:{normalized_operation}":
            raise ValueError("Claude characterization identity mismatch")

        def _write(conn):
            from .claude_visibility import (
                ClaudeVisibilityCandidate,
                derive_claude_visibility_identity,
                validate_claude_visibility_identity_binding,
            )

            row = conn.execute(
                "SELECT * FROM session_claude_visibility_jobs WHERE id = ?",
                (normalized_job,),
            ).fetchone()
            if row is None:
                raise ValueError("Claude characterization identity mismatch")
            expected = {
                "source_session_id": normalized_source,
                "bridge_id": normalized_bridge,
                "idempotency_key": normalized_idempotency,
                "reserved_claude_uuid": normalized_uuid,
                "native_name": normalized_name,
                "source_cwd": normalized_cwd,
                "signed_marker": normalized_marker,
            }
            if any(row[key] != value for key, value in expected.items()):
                raise ValueError("Claude characterization identity mismatch")
            if row["source_provider"] != Provider.CODEX.value:
                raise ValueError("Claude characterization identity mismatch")
            candidate = ClaudeVisibilityCandidate(
                source_session_id=row["source_session_id"],
                source_provider=Provider.CODEX,
                native_name=row["native_name"],
                source_cwd=row["source_cwd"],
                git_root=row["git_root"],
                git_branch=row["git_branch"],
                git_head=row["git_head"],
                worktree_id=row["worktree_id"],
                eligible_at=float(row["eligible_at"]),
            )
            derived = derive_claude_visibility_identity(candidate, marker_secret)
            validate_claude_visibility_identity_binding(
                candidate, derived, marker_secret
            )
            if (
                derived.job_id != normalized_job
                or derived.bridge_id != normalized_bridge
                or derived.idempotency_key != normalized_idempotency
                or derived.claude_uuid != normalized_uuid
                or not hmac.compare_digest(derived.signed_marker, normalized_marker)
            ):
                raise ValueError("Claude characterization identity mismatch")
            try:
                marker = decode_bridge_marker(normalized_marker, marker_secret)
            except (TypeError, ValueError):
                raise ValueError("Claude characterization identity mismatch") from None
            if (
                marker.source_session_id != normalized_source
                or marker.bridge_id != normalized_bridge
                or marker.target_provider is not Provider.CLAUDE
            ):
                raise ValueError("Claude characterization identity mismatch")

            recorded_at = _finite_number(self._clock(), "clock")
            identity_values = (
                normalized_job,
                normalized_operation,
                normalized_source,
                normalized_bridge,
                normalized_idempotency,
                normalized_uuid,
            )

            def append_event(kind: str, digest: str) -> None:
                existing = conn.execute(
                    """SELECT *
                       FROM session_claude_visibility_characterization_events
                       WHERE job_id = ? AND event_kind = ?""",
                    (normalized_job, kind),
                ).fetchone()
                if existing is not None:
                    if any(
                        existing[key] != value
                        for key, value in {
                            "operation_id": normalized_operation,
                            "source_session_id": normalized_source,
                            "bridge_id": normalized_bridge,
                            "idempotency_key": normalized_idempotency,
                            "reserved_claude_uuid": normalized_uuid,
                        }.items()
                    ):
                        raise ValueError("Claude characterization identity mismatch")
                    if kind != "registered" and not hmac.compare_digest(
                        existing["evidence_digest"], digest
                    ):
                        raise ValueError("Claude characterization evidence mismatch")
                    return
                conn.execute(
                    """INSERT INTO session_claude_visibility_characterization_events (
                           job_id, event_kind, operation_id, source_session_id,
                           bridge_id, idempotency_key, reserved_claude_uuid,
                           evidence_digest, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        *identity_values[:1],
                        kind,
                        *identity_values[1:],
                        digest,
                        recorded_at,
                    ),
                )

            append_event("registered", normalized_evidence)
            if cleanup_completed:
                append_event("cleanup_completed", normalized_evidence)
            if launch_aborted:
                existing_abort = conn.execute(
                    """SELECT evidence_digest
                       FROM session_claude_visibility_characterization_events
                       WHERE job_id = ? AND event_kind = 'launch_aborted'""",
                    (normalized_job,),
                ).fetchone()
                if existing_abort is not None:
                    append_event("launch_aborted", existing_abort["evidence_digest"])
                    return {
                        "status": "already_aborted",
                        "job_id": normalized_job,
                        "reserved_claude_uuid": normalized_uuid,
                    }
                absence = conn.execute(
                    """SELECT rowid, evidence_digest
                       FROM session_claude_visibility_reconciliations
                       WHERE job_id = ? AND reserved_claude_uuid = ?
                         AND attempt_ordinal = ? AND outcome = 'absent'
                         AND consumed_at IS NULL
                       ORDER BY checked_at DESC LIMIT 1""",
                    (normalized_job, normalized_uuid, row["attempts"]),
                ).fetchone()
                abortable_state = row["state"] == "claude_retry" or (
                    row["state"] == "claude_failed"
                    and row["error_code"] == "max_attempts_exhausted"
                )
                if not abortable_state or absence is None:
                    return {
                        "status": "reconciliation_required",
                        "job_id": normalized_job,
                        "reserved_claude_uuid": normalized_uuid,
                    }
                append_event("launch_aborted", absence["evidence_digest"])
                consumed = conn.execute(
                    """UPDATE session_claude_visibility_reconciliations
                       SET consumed_at = ?
                       WHERE rowid = ? AND consumed_at IS NULL""",
                    (recorded_at, absence["rowid"]),
                )
                if consumed.rowcount != 1:
                    raise ValueError("Claude characterization abort evidence is stale")
                return {
                    "status": "launch_aborted",
                    "job_id": normalized_job,
                    "reserved_claude_uuid": normalized_uuid,
                }
            return {
                "status": ("cleanup_completed" if cleanup_completed else "registered"),
                "job_id": normalized_job,
                "reserved_claude_uuid": normalized_uuid,
            }

        return self.db._execute_write(_write)

    def acknowledge_sidebar_precreate_resolution(
        self,
        *,
        job_id: object,
        expected_error_code: object,
        expected_attempts: object,
        expected_next_attempt_at: object,
        expected_updated_at: object,
        evidence_digest: object,
        marker_secret: object,
        now: object,
    ) -> dict[str, Any]:
        """Append evidence that one cutover reservation has no recoverable task."""

        expected_job_id = _exact_nonempty_text(job_id, "sidebar job ID")
        expected_error = _exact_nonempty_text(
            expected_error_code, "expected sidebar failure"
        )
        if expected_error != "native_create_ambiguous":
            raise ValueError("expected sidebar failure does not match")
        _nonnegative_integer(expected_attempts, "expected sidebar attempts")
        attempts = cast(int, expected_attempts)
        if attempts != 0:
            raise ValueError("precreate sidebar failure has attempts")
        next_attempt_at = _finite_number(
            expected_next_attempt_at, "expected sidebar next attempt"
        )
        updated_at = _finite_number(expected_updated_at, "expected sidebar update time")
        evidence = _sha256_text(
            evidence_digest, "sidebar precreate resolution evidence digest"
        )
        if type(marker_secret) is not bytes or not marker_secret:
            raise ValueError("sidebar precreate marker secret is malformed")
        expected_marker_secret = cast(bytes, marker_secret)
        resolved_at = _finite_number(now, "sidebar precreate resolution time")

        def _write(conn: sqlite3.Connection) -> dict[str, Any]:
            if not self._sidebar_terminal_resolution_ledger_is_valid(conn):
                raise ValueError("invalid sidebar terminal resolution ledger")
            job = conn.execute(
                "SELECT * FROM session_sidebar_jobs WHERE id = ?",
                (expected_job_id,),
            ).fetchone()
            if job is None:
                raise ValueError("expected sidebar precreate resolution does not match")

            from .sidebar import sidebar_bridge_id, sidebar_idempotency_key

            source_session_id = _exact_nonempty_text(
                job["source_session_id"], "sidebar source session ID"
            )
            idempotency_key = sidebar_idempotency_key(source_session_id)
            bridge_id = sidebar_bridge_id(source_session_id)
            canonical_job_id = (
                "sidebar-job:"
                + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
            )
            siblings = conn.execute(
                "SELECT id FROM session_sidebar_jobs "
                "WHERE source_session_id = ? ORDER BY id LIMIT 2",
                (source_session_id,),
            ).fetchall()
            if (
                canonical_job_id != expected_job_id
                or job["idempotency_key"] != idempotency_key
                or job["bridge_id"] != bridge_id
                or len(siblings) != 1
                or siblings[0]["id"] != expected_job_id
            ):
                raise ValueError("expected sidebar precreate resolution does not match")

            reservation_row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?",
                (_sidebar_create_reservation_state_key(source_session_id),),
            ).fetchone()
            cutover_row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?",
                (_SIDEBAR_CREATE_RESERVATION_CUTOVER_STATE_KEY,),
            ).fetchone()
            if reservation_row is None or cutover_row is None:
                raise ValueError("expected sidebar precreate resolution does not match")
            reservation = _decode_sidebar_create_reservation(
                reservation_row["value_json"],
                expected_source_session_id=source_session_id,
            )
            cutover = _decode_sidebar_create_reservation_cutover(
                cutover_row["value_json"]
            )
            delivery_candidate = _validated_sidebar_cutover_candidate(conn, dict(job))
            if (
                reservation["job_id"] != expected_job_id
                or reservation["bridge_id"] != bridge_id
                or expected_job_id not in cutover["quarantined_job_ids"]
                or reservation["reserved_at"] != cutover["applied_at"]
                or not hmac.compare_digest(
                    reservation["recovery_key"],
                    _sidebar_cutover_recovery_key(
                        dict(job),
                        marker_secret=expected_marker_secret,
                    ),
                )
            ):
                raise ValueError("expected sidebar precreate resolution does not match")
            if (
                job["codex_thread_id"] is not None
                or job["state"] != SidebarJobState.FAILED.value
                or job["error_code"] != expected_error
                or job["attempts"] != attempts
                or job["next_attempt_at"] != next_attempt_at
                or job["updated_at"] != updated_at
                or job["lease_digest"] is not None
                or job["lease_expires_at"] is not None
                or job["completion_digest"] is not None
                or job["visible_at"] is not None
            ):
                raise ValueError("expected sidebar precreate resolution does not match")
            materialized = conn.execute(
                """SELECT 1
                     WHERE EXISTS (
                         SELECT 1 FROM external_sessions AS external
                          WHERE external.provider = ?
                            AND external.origin_bridge_id = ?
                     )
                        OR EXISTS (
                         SELECT 1 FROM session_links AS link
                          WHERE link.bridge_id = ?
                     )
                        OR EXISTS (
                         SELECT 1 FROM session_sidebar_exclusions AS exclusion
                          WHERE exclusion.source_session_id = ?
                     )""",
                (
                    Provider.CODEX.value,
                    bridge_id,
                    bridge_id,
                    source_session_id,
                ),
            ).fetchone()
            if materialized is not None:
                raise ValueError("expected sidebar precreate resolution does not match")

            canonical_evidence = sidebar_precreate_terminal_evidence_digest(
                job=dict(job),
                reservation=reservation,
                cutover=cutover,
                candidate=delivery_candidate,
            )
            if not hmac.compare_digest(evidence, canonical_evidence):
                raise ValueError("sidebar precreate resolution evidence does not match")
            if resolved_at < updated_at:
                raise ValueError("sidebar precreate resolution time precedes failure")

            expected_fields = {
                "job_id": expected_job_id,
                "idempotency_key": idempotency_key,
                "source_session_id": source_session_id,
                "bridge_id": bridge_id,
                "failure_state": SidebarJobState.FAILED.value,
                "failure_code": expected_error,
                "failure_attempts": attempts,
                "failure_next_attempt_at": next_attempt_at,
                "failure_updated_at": updated_at,
                "cutover_applied_at": cutover["applied_at"],
                "reservation_reserved_at": reservation["reserved_at"],
                "resolution_code": SIDEBAR_PRECREATE_RESOLUTION_CODE,
                "evidence_kind": SIDEBAR_PRECREATE_EVIDENCE_KIND,
                "evidence_version": SIDEBAR_PRECREATE_EVIDENCE_VERSION,
                "evidence_digest": canonical_evidence,
            }
            resolution = conn.execute(
                "SELECT * FROM session_sidebar_precreate_resolutions WHERE job_id = ?",
                (expected_job_id,),
            ).fetchone()
            if resolution is not None:
                if any(
                    resolution[key] != value for key, value in expected_fields.items()
                ):
                    raise ValueError("conflicting sidebar precreate resolution")
                return {
                    "job_id": expected_job_id,
                    "state": SidebarJobState.FAILED.value,
                    "error_code": expected_error,
                    "resolution_code": SIDEBAR_PRECREATE_RESOLUTION_CODE,
                    "created": False,
                }

            try:
                cursor = conn.execute(
                    """INSERT INTO session_sidebar_precreate_resolutions (
                           job_id, idempotency_key, source_session_id, bridge_id,
                           failure_state, failure_code, failure_attempts,
                           failure_next_attempt_at, failure_updated_at,
                           cutover_applied_at, reservation_reserved_at,
                           resolution_code, evidence_kind, evidence_version,
                           evidence_digest, resolved_at
                       )
                       SELECT job.id, job.idempotency_key, job.source_session_id,
                              job.bridge_id, job.state, job.error_code,
                              job.attempts, job.next_attempt_at, job.updated_at,
                              ?, ?, ?, ?, ?, ?, ?
                         FROM session_sidebar_jobs AS job
                        WHERE job.id = ?
                          AND job.idempotency_key = ?
                          AND job.source_session_id = ?
                          AND job.bridge_id = ?
                          AND job.codex_thread_id IS NULL
                          AND job.state = ?
                          AND job.error_code = ?
                          AND job.attempts = 0
                          AND job.next_attempt_at = ?
                          AND job.updated_at = ?
                          AND job.lease_digest IS NULL
                          AND job.lease_expires_at IS NULL
                          AND job.completion_digest IS NULL
                          AND job.visible_at IS NULL
                          AND NOT EXISTS (
                              SELECT 1 FROM external_sessions AS external
                               WHERE external.provider = ?
                                 AND external.origin_bridge_id = job.bridge_id
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM session_links AS link
                               WHERE link.bridge_id = job.bridge_id
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM session_sidebar_exclusions AS exclusion
                               WHERE exclusion.source_session_id = job.source_session_id
                          )""",
                    (
                        cutover["applied_at"],
                        reservation["reserved_at"],
                        SIDEBAR_PRECREATE_RESOLUTION_CODE,
                        SIDEBAR_PRECREATE_EVIDENCE_KIND,
                        SIDEBAR_PRECREATE_EVIDENCE_VERSION,
                        canonical_evidence,
                        resolved_at,
                        expected_job_id,
                        idempotency_key,
                        source_session_id,
                        bridge_id,
                        SidebarJobState.FAILED.value,
                        expected_error,
                        next_attempt_at,
                        updated_at,
                        Provider.CODEX.value,
                    ),
                )
            except sqlite3.IntegrityError:
                raise ValueError("conflicting sidebar precreate resolution") from None
            if cursor.rowcount != 1:
                raise ValueError("expected sidebar precreate resolution does not match")
            return {
                "job_id": expected_job_id,
                "state": SidebarJobState.FAILED.value,
                "error_code": expected_error,
                "resolution_code": SIDEBAR_PRECREATE_RESOLUTION_CODE,
                "created": True,
            }

        return self.db._execute_write(_write)

    def acknowledge_sidebar_v2_attempt_zero_resolution(
        self,
        *,
        job_id: object,
        expected_error_code: object,
        expected_attempts: object,
        expected_next_attempt_at: object,
        expected_updated_at: object,
        expected_reconciliation_proof_digest: object,
        expected_reconciliation_generation: object,
        evidence_digest: object,
        marker_secret: object,
        now: object,
    ) -> dict[str, Any]:
        """Append one exact, fresh proof-bound v2 attempts-zero resolution."""

        from .sidebar import sidebar_bridge_id, sidebar_idempotency_key

        expected_job_id = _exact_nonempty_text(job_id, "sidebar job ID")
        expected_error = _exact_nonempty_text(
            expected_error_code, "expected sidebar failure"
        )
        if expected_error != "native_create_ambiguous":
            raise ValueError("expected sidebar failure does not match")
        _nonnegative_integer(expected_attempts, "expected sidebar attempts")
        attempts = cast(int, expected_attempts)
        if attempts != 0:
            raise ValueError("v2 attempt-zero sidebar failure has attempts")
        next_attempt_at = _finite_number(
            expected_next_attempt_at, "expected sidebar next attempt"
        )
        updated_at = _finite_number(expected_updated_at, "expected sidebar update time")
        proof_digest = _lowercase_sha256(
            expected_reconciliation_proof_digest,
            "expected sidebar reconciliation proof digest",
        )
        proof_generation = _exact_nonempty_text(
            expected_reconciliation_generation,
            "expected sidebar reconciliation generation",
        )
        evidence = _sha256_text(
            evidence_digest, "sidebar v2 attempt-zero resolution evidence digest"
        )
        if type(marker_secret) is not bytes or not marker_secret:
            raise ValueError("sidebar v2 attempt-zero marker secret is malformed")
        secret = cast(bytes, marker_secret)
        resolved_at = _finite_number(now, "sidebar v2 attempt-zero resolution time")

        def _write(conn: sqlite3.Connection) -> dict[str, Any]:
            if not self._sidebar_terminal_resolution_ledger_is_valid(conn):
                raise ValueError("invalid sidebar terminal resolution ledger")
            job = conn.execute(
                "SELECT * FROM session_sidebar_jobs WHERE id = ?", (expected_job_id,)
            ).fetchone()
            if job is None:
                raise ValueError("expected sidebar v2 attempt-zero resolution does not match")
            source_session_id = _exact_nonempty_text(
                job["source_session_id"], "sidebar source session ID"
            )
            idempotency_key = sidebar_idempotency_key(source_session_id)
            bridge_id = sidebar_bridge_id(source_session_id)
            canonical_job_id = (
                "sidebar-job:"
                + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
            )
            reservation_row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?",
                (_sidebar_create_reservation_state_key(source_session_id),),
            ).fetchone()
            proof_row = conn.execute(
                "SELECT * FROM session_sidebar_reconciliation_proofs "
                "WHERE proof_digest = ?",
                (proof_digest,),
            ).fetchone()
            if reservation_row is None or proof_row is None:
                raise ValueError("expected sidebar v2 attempt-zero resolution does not match")
            reservation = _decode_sidebar_create_reservation(
                reservation_row["value_json"],
                expected_source_session_id=source_session_id,
            )
            proof = dict(proof_row)
            candidate = _validated_sidebar_cutover_candidate(conn, dict(job))
            marker = encode_bridge_marker(
                BridgeMarkerPayload(
                    bridge_id=bridge_id,
                    source_session_id=source_session_id,
                    target_provider=Provider.CODEX,
                    policy_generation=1,
                ),
                secret,
            )
            if (
                canonical_job_id != expected_job_id
                or job["idempotency_key"] != idempotency_key
                or job["bridge_id"] != bridge_id
                or reservation.get("version") != 2
                or set(reservation) != _SIDEBAR_CREATE_RESERVATION_FIELDS
                or reservation.get("job_id") != expected_job_id
                or reservation.get("bridge_id") != bridge_id
                or reservation.get("reconciliation_proof_digest") != proof_digest
                or reservation.get("reconciliation_generation") != proof_generation
                or proof.get("job_id") != expected_job_id
                or proof.get("source_session_id") != source_session_id
                or proof.get("bridge_id") != bridge_id
                or proof.get("reconciliation_generation") != proof_generation
                or proof.get("state") != "absence_proven"
                or proof.get("match_count") != 0
                or proof.get("recovered_thread_id") is not None
                or proof.get("fixed_reason") is not None
                or job["reconciliation_proof_digest"] != proof_digest
                or not hmac.compare_digest(
                    proof["marker_digest"],
                    hashlib.sha256(marker.encode("utf-8")).hexdigest(),
                )
                or not hmac.compare_digest(
                    reservation["recovery_key"],
                    _sidebar_cutover_recovery_key(dict(job), marker_secret=secret),
                )
                or job["codex_thread_id"] is not None
                or job["state"] != SidebarJobState.FAILED.value
                or job["error_code"] != expected_error
                or job["attempts"] != attempts
                or job["next_attempt_at"] != next_attempt_at
                or job["updated_at"] != updated_at
                or job["lease_digest"] is not None
                or job["lease_expires_at"] is not None
                or job["completion_digest"] is not None
                or job["visible_at"] is not None
            ):
                raise ValueError("expected sidebar v2 attempt-zero resolution does not match")
            if resolved_at < max(updated_at, float(proof["completed_at"])):
                raise ValueError("sidebar v2 attempt-zero resolution time precedes evidence")
            if resolved_at > float(proof["expires_at"]):
                raise ValueError("sidebar v2 attempt-zero reconciliation proof expired")
            materialized = conn.execute(
                """SELECT 1 WHERE EXISTS (
                       SELECT 1 FROM external_sessions AS external
                        WHERE external.provider = ? AND external.origin_bridge_id = ?
                   ) OR EXISTS (
                       SELECT 1 FROM session_links AS link WHERE link.bridge_id = ?
                   ) OR EXISTS (
                       SELECT 1 FROM session_sidebar_exclusions AS exclusion
                        WHERE exclusion.source_session_id = ?
                   )""",
                (Provider.CODEX.value, bridge_id, bridge_id, source_session_id),
            ).fetchone()
            if materialized is not None:
                raise ValueError("expected sidebar v2 attempt-zero resolution does not match")
            canonical_evidence = sidebar_v2_attempt_zero_terminal_evidence_digest(
                job=dict(job), reservation=reservation, proof=proof, candidate=candidate
            )
            if not hmac.compare_digest(evidence, canonical_evidence):
                raise ValueError("sidebar v2 attempt-zero resolution evidence does not match")
            expected_fields = {
                "job_id": expected_job_id,
                "idempotency_key": idempotency_key,
                "source_session_id": source_session_id,
                "bridge_id": bridge_id,
                "failure_state": SidebarJobState.FAILED.value,
                "failure_code": expected_error,
                "failure_attempts": attempts,
                "failure_next_attempt_at": next_attempt_at,
                "failure_updated_at": updated_at,
                "reservation_reserved_at": reservation["reserved_at"],
                "reservation_reconciliation_proof_digest": proof_digest,
                "reservation_reconciliation_generation": proof_generation,
                "proof_completed_at": proof["completed_at"],
                "proof_expires_at": proof["expires_at"],
                "proof_inventory_digest": proof["inventory_digest"],
                "resolution_code": SIDEBAR_V2_ATTEMPT_ZERO_RESOLUTION_CODE,
                "evidence_kind": SIDEBAR_V2_ATTEMPT_ZERO_EVIDENCE_KIND,
                "evidence_version": SIDEBAR_V2_ATTEMPT_ZERO_EVIDENCE_VERSION,
                "evidence_digest": canonical_evidence,
            }
            existing = conn.execute(
                "SELECT * FROM session_sidebar_v2_attempt_zero_resolutions WHERE job_id = ?",
                (expected_job_id,),
            ).fetchone()
            if existing is not None:
                if any(existing[key] != value for key, value in expected_fields.items()):
                    raise ValueError("conflicting sidebar v2 attempt-zero resolution")
                return {
                    "job_id": expected_job_id,
                    "state": SidebarJobState.FAILED.value,
                    "error_code": expected_error,
                    "resolution_code": SIDEBAR_V2_ATTEMPT_ZERO_RESOLUTION_CODE,
                    "created": False,
                }
            try:
                cursor = conn.execute(
                    """INSERT INTO session_sidebar_v2_attempt_zero_resolutions (
                           job_id, idempotency_key, source_session_id, bridge_id,
                           failure_state, failure_code, failure_attempts,
                           failure_next_attempt_at, failure_updated_at,
                           reservation_reserved_at,
                           reservation_reconciliation_proof_digest,
                           reservation_reconciliation_generation,
                           proof_completed_at, proof_expires_at, proof_inventory_digest,
                           resolution_code, evidence_kind, evidence_version,
                           evidence_digest, resolved_at
                       ) VALUES (
                           :job_id, :idempotency_key, :source_session_id, :bridge_id,
                           :failure_state, :failure_code, :failure_attempts,
                           :failure_next_attempt_at, :failure_updated_at,
                           :reservation_reserved_at,
                           :reservation_reconciliation_proof_digest,
                           :reservation_reconciliation_generation,
                           :proof_completed_at, :proof_expires_at, :proof_inventory_digest,
                           :resolution_code, :evidence_kind, :evidence_version,
                           :evidence_digest, :resolved_at
                       )""",
                    {**expected_fields, "resolved_at": resolved_at},
                )
            except sqlite3.IntegrityError:
                raise ValueError("conflicting sidebar v2 attempt-zero resolution") from None
            if cursor.rowcount != 1:
                raise ValueError("expected sidebar v2 attempt-zero resolution does not match")
            return {
                "job_id": expected_job_id,
                "state": SidebarJobState.FAILED.value,
                "error_code": expected_error,
                "resolution_code": SIDEBAR_V2_ATTEMPT_ZERO_RESOLUTION_CODE,
                "created": True,
            }

        return self.db._execute_write(_write)

    def acknowledge_sidebar_unbound_resolution(
        self,
        *,
        job_id: object,
        expected_error_code: object,
        expected_attempts: object,
        expected_next_attempt_at: object,
        expected_updated_at: object,
        evidence_digest: object,
        marker_secret: object,
        now: object,
    ) -> dict[str, Any]:
        """Append exact-absence evidence for one post-dispatch unbound create."""

        expected_job_id = _exact_nonempty_text(job_id, "sidebar job ID")
        expected_error = _exact_nonempty_text(
            expected_error_code, "expected sidebar failure"
        )
        if expected_error != "native_create_ambiguous":
            raise ValueError("expected sidebar failure does not match")
        _nonnegative_integer(expected_attempts, "expected sidebar attempts")
        attempts = cast(int, expected_attempts)
        if attempts <= 0:
            raise ValueError("unbound sidebar failure has no dispatch attempts")
        next_attempt_at = _finite_number(
            expected_next_attempt_at, "expected sidebar next attempt"
        )
        updated_at = _finite_number(expected_updated_at, "expected sidebar update time")
        evidence = _sha256_text(
            evidence_digest, "sidebar unbound resolution evidence digest"
        )
        if type(marker_secret) is not bytes or not marker_secret:
            raise ValueError("sidebar unbound marker secret is malformed")
        expected_marker_secret = cast(bytes, marker_secret)
        resolved_at = _finite_number(now, "sidebar unbound resolution time")

        def _write(conn: sqlite3.Connection) -> dict[str, Any]:
            if not self._sidebar_terminal_resolution_ledger_is_valid(conn):
                raise ValueError("invalid sidebar terminal resolution ledger")
            job = conn.execute(
                "SELECT * FROM session_sidebar_jobs WHERE id = ?",
                (expected_job_id,),
            ).fetchone()
            if job is None:
                raise ValueError("expected sidebar unbound resolution does not match")

            from .sidebar import sidebar_bridge_id, sidebar_idempotency_key

            source_session_id = _exact_nonempty_text(
                job["source_session_id"], "sidebar source session ID"
            )
            idempotency_key = sidebar_idempotency_key(source_session_id)
            bridge_id = sidebar_bridge_id(source_session_id)
            canonical_job_id = (
                "sidebar-job:"
                + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
            )
            siblings = conn.execute(
                "SELECT id FROM session_sidebar_jobs "
                "WHERE source_session_id = ? ORDER BY id LIMIT 2",
                (source_session_id,),
            ).fetchall()
            if (
                canonical_job_id != expected_job_id
                or job["idempotency_key"] != idempotency_key
                or job["bridge_id"] != bridge_id
                or len(siblings) != 1
                or siblings[0]["id"] != expected_job_id
            ):
                raise ValueError("expected sidebar unbound resolution does not match")

            reservation_row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?",
                (_sidebar_create_reservation_state_key(source_session_id),),
            ).fetchone()
            if reservation_row is None:
                raise ValueError("expected sidebar unbound resolution does not match")
            reservation = _decode_sidebar_create_reservation(
                reservation_row["value_json"],
                expected_source_session_id=source_session_id,
            )
            delivery_candidate = _validated_sidebar_cutover_candidate(conn, dict(job))
            if (
                reservation["job_id"] != expected_job_id
                or reservation["bridge_id"] != bridge_id
                or not hmac.compare_digest(
                    reservation["recovery_key"],
                    _sidebar_cutover_recovery_key(
                        dict(job),
                        marker_secret=expected_marker_secret,
                    ),
                )
            ):
                raise ValueError("expected sidebar unbound resolution does not match")
            if (
                job["codex_thread_id"] is not None
                or job["state"] != SidebarJobState.FAILED.value
                or job["error_code"] != expected_error
                or job["attempts"] != attempts
                or job["next_attempt_at"] != next_attempt_at
                or job["updated_at"] != updated_at
                or job["lease_digest"] is not None
                or job["lease_expires_at"] is not None
                or job["completion_digest"] is not None
                or job["visible_at"] is not None
            ):
                raise ValueError("expected sidebar unbound resolution does not match")
            materialized = conn.execute(
                """SELECT 1
                     WHERE EXISTS (
                         SELECT 1 FROM external_sessions AS external
                          WHERE external.provider = ?
                            AND external.origin_bridge_id = ?
                     )
                        OR EXISTS (
                         SELECT 1 FROM session_links AS link
                          WHERE link.bridge_id = ?
                     )
                        OR EXISTS (
                         SELECT 1 FROM session_sidebar_exclusions AS exclusion
                          WHERE exclusion.source_session_id = ?
                     )""",
                (
                    Provider.CODEX.value,
                    bridge_id,
                    bridge_id,
                    source_session_id,
                ),
            ).fetchone()
            if materialized is not None:
                raise ValueError("expected sidebar unbound resolution does not match")

            canonical_evidence = sidebar_unbound_terminal_evidence_digest(
                job=dict(job),
                reservation=reservation,
                candidate=delivery_candidate,
            )
            if not hmac.compare_digest(evidence, canonical_evidence):
                raise ValueError("sidebar unbound resolution evidence does not match")
            if resolved_at < updated_at:
                raise ValueError("sidebar unbound resolution time precedes failure")

            expected_fields = {
                "job_id": expected_job_id,
                "idempotency_key": idempotency_key,
                "source_session_id": source_session_id,
                "bridge_id": bridge_id,
                "failure_state": SidebarJobState.FAILED.value,
                "failure_code": expected_error,
                "failure_attempts": attempts,
                "failure_next_attempt_at": next_attempt_at,
                "failure_updated_at": updated_at,
                "reservation_reserved_at": reservation["reserved_at"],
                "resolution_code": SIDEBAR_UNBOUND_RESOLUTION_CODE,
                "evidence_kind": SIDEBAR_UNBOUND_EVIDENCE_KIND,
                "evidence_version": SIDEBAR_UNBOUND_EVIDENCE_VERSION,
                "evidence_digest": canonical_evidence,
            }
            resolution = conn.execute(
                "SELECT * FROM session_sidebar_unbound_resolutions WHERE job_id = ?",
                (expected_job_id,),
            ).fetchone()
            if resolution is not None:
                if any(
                    resolution[key] != value for key, value in expected_fields.items()
                ):
                    raise ValueError("conflicting sidebar unbound resolution")
                return {
                    "job_id": expected_job_id,
                    "state": SidebarJobState.FAILED.value,
                    "error_code": expected_error,
                    "resolution_code": SIDEBAR_UNBOUND_RESOLUTION_CODE,
                    "created": False,
                }

            try:
                cursor = conn.execute(
                    """INSERT INTO session_sidebar_unbound_resolutions (
                           job_id, idempotency_key, source_session_id, bridge_id,
                           failure_state, failure_code, failure_attempts,
                           failure_next_attempt_at, failure_updated_at,
                           reservation_reserved_at, resolution_code,
                           evidence_kind, evidence_version, evidence_digest,
                           resolved_at
                       )
                       SELECT job.id, job.idempotency_key, job.source_session_id,
                              job.bridge_id, job.state, job.error_code,
                              job.attempts, job.next_attempt_at, job.updated_at,
                              ?, ?, ?, ?, ?, ?
                         FROM session_sidebar_jobs AS job
                        WHERE job.id = ?
                          AND job.idempotency_key = ?
                          AND job.source_session_id = ?
                          AND job.bridge_id = ?
                          AND job.codex_thread_id IS NULL
                          AND job.state = ?
                          AND job.error_code = ?
                          AND job.attempts = ?
                          AND job.attempts > 0
                          AND job.next_attempt_at = ?
                          AND job.updated_at = ?
                          AND job.lease_digest IS NULL
                          AND job.lease_expires_at IS NULL
                          AND job.completion_digest IS NULL
                          AND job.visible_at IS NULL""",
                    (
                        reservation["reserved_at"],
                        SIDEBAR_UNBOUND_RESOLUTION_CODE,
                        SIDEBAR_UNBOUND_EVIDENCE_KIND,
                        SIDEBAR_UNBOUND_EVIDENCE_VERSION,
                        canonical_evidence,
                        resolved_at,
                        expected_job_id,
                        idempotency_key,
                        source_session_id,
                        bridge_id,
                        SidebarJobState.FAILED.value,
                        expected_error,
                        attempts,
                        next_attempt_at,
                        updated_at,
                    ),
                )
            except sqlite3.IntegrityError:
                raise ValueError("conflicting sidebar unbound resolution") from None
            if cursor.rowcount != 1:
                raise ValueError("expected sidebar unbound resolution does not match")
            return {
                "job_id": expected_job_id,
                "state": SidebarJobState.FAILED.value,
                "error_code": expected_error,
                "resolution_code": SIDEBAR_UNBOUND_RESOLUTION_CODE,
                "created": True,
            }

        return self.db._execute_write(_write)

    def retry_failed_bound_sidebar_job(
        self,
        *,
        job_id: str,
        source_session_id: str,
        codex_thread_id: str,
        expected_error_code: str,
        confirmation: str,
        now: float,
    ) -> dict[str, Any]:
        """Requeue one exact failed bound task without permitting replacement."""

        from .sidebar import sidebar_bridge_id, sidebar_idempotency_key

        supplied_job_id = _exact_nonempty_text(job_id, "sidebar job ID")
        source_id = _exact_nonempty_text(
            source_session_id,
            "sidebar source session ID",
        )
        thread_id = _exact_nonempty_text(codex_thread_id, "Codex thread ID")
        error_code = _exact_nonempty_text(
            expected_error_code,
            "expected sidebar failure",
        )
        authority = _exact_nonempty_text(
            confirmation,
            "bound sidebar retry confirmation",
        )
        if not sidebar_bound_retry_authority_matches(error_code, authority):
            raise ValueError("expected bound sidebar failure does not match")
        retry_time = _finite_number(now, "now")
        idempotency_key = sidebar_idempotency_key(source_id)
        bridge_id = sidebar_bridge_id(source_id)
        expected_job_id = (
            f"sidebar-job:{hashlib.sha256(idempotency_key.encode()).hexdigest()}"
        )
        if supplied_job_id != expected_job_id:
            raise ValueError("expected bound sidebar failure does not match")

        def _write(conn):
            if not self._sidebar_terminal_resolution_ledger_is_valid(conn):
                raise ValueError("expected bound sidebar failure does not match")
            jobs = conn.execute(
                """SELECT * FROM session_sidebar_jobs
                   WHERE source_session_id = ? ORDER BY id LIMIT 2""",
                (source_id,),
            ).fetchall()
            job = jobs[0] if len(jobs) == 1 else None
            if job is None:
                raise ValueError("expected bound sidebar failure does not match")
            terminal_resolution = conn.execute(
                "SELECT 1 FROM session_sidebar_terminal_resolutions "
                "WHERE job_id = ? LIMIT 1",
                (job["id"],),
            ).fetchone()
            precreate_resolution = conn.execute(
                "SELECT 1 FROM session_sidebar_precreate_resolutions "
                "WHERE job_id = ? LIMIT 1",
                (job["id"],),
            ).fetchone()
            unbound_resolution = conn.execute(
                "SELECT 1 FROM session_sidebar_unbound_resolutions "
                "WHERE job_id = ? LIMIT 1",
                (job["id"],),
            ).fetchone()
            v2_attempt_zero_resolution = conn.execute(
                "SELECT 1 FROM session_sidebar_v2_attempt_zero_resolutions "
                "WHERE job_id = ? LIMIT 1",
                (job["id"],),
            ).fetchone()
            reservation_row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?",
                (_sidebar_create_reservation_state_key(source_id),),
            ).fetchone()
            reservation = (
                None
                if reservation_row is None
                else _decode_sidebar_create_reservation(
                    reservation_row["value_json"],
                    expected_source_session_id=source_id,
                )
            )
            if (
                job["id"] != expected_job_id
                or job["idempotency_key"] != idempotency_key
                or job["bridge_id"] != bridge_id
                or job["codex_thread_id"] != thread_id
                or job["state"] != SidebarJobState.FAILED.value
                or job["error_code"] != error_code
                or job["lease_digest"] is not None
                or job["lease_expires_at"] is not None
                or job["completion_digest"] is not None
                or job["visible_at"] is not None
                or terminal_resolution is not None
                or precreate_resolution is not None
                or unbound_resolution is not None
                or v2_attempt_zero_resolution is not None
                or reservation is None
                or reservation["job_id"] != expected_job_id
                or reservation["source_session_id"] != source_id
                or reservation["bridge_id"] != bridge_id
            ):
                raise ValueError("expected bound sidebar failure does not match")
            cursor = conn.execute(
                """UPDATE session_sidebar_jobs
                   SET state = ?, attempts = 0, next_attempt_at = ?,
                       lease_digest = NULL, lease_expires_at = NULL,
                       error_code = NULL, updated_at = ?
                   WHERE id = ? AND idempotency_key = ? AND bridge_id = ?
                     AND source_session_id = ? AND codex_thread_id = ?
                     AND state = ? AND error_code = ?
                     AND lease_digest IS NULL AND lease_expires_at IS NULL
                     AND completion_digest IS NULL AND visible_at IS NULL""",
                (
                    SidebarJobState.RETRY.value,
                    retry_time,
                    retry_time,
                    expected_job_id,
                    idempotency_key,
                    bridge_id,
                    source_id,
                    thread_id,
                    SidebarJobState.FAILED.value,
                    error_code,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("expected bound sidebar failure does not match")
            return dict(
                conn.execute(
                    "SELECT * FROM session_sidebar_jobs WHERE id = ?",
                    (expected_job_id,),
                ).fetchone()
            )

        return self.db._execute_write(_write)

    def retry_failed_sidebar_job(
        self,
        *,
        source_session_id: str,
        expected_error_code: str,
        now: float,
    ) -> dict[str, Any]:
        from .sidebar import sidebar_bridge_id, sidebar_idempotency_key

        source_id = _exact_nonempty_text(
            source_session_id,
            "sidebar source session ID",
        )
        error_code = _exact_nonempty_text(
            expected_error_code,
            "expected sidebar failure",
        )
        if error_code not in SIDEBAR_RETRYABLE_ERRORS | SIDEBAR_FATAL_ERRORS:
            raise ValueError("expected sidebar failure is not in the fixed allowlist")
        retry_time = _finite_number(now, "now")
        idempotency_key = sidebar_idempotency_key(source_id)
        bridge_id = sidebar_bridge_id(source_id)
        job_id = f"sidebar-job:{hashlib.sha256(idempotency_key.encode()).hexdigest()}"

        def _write(conn):
            if not self._sidebar_terminal_resolution_ledger_is_valid(conn):
                raise ValueError("expected sidebar failure does not match")
            jobs = conn.execute(
                """SELECT * FROM session_sidebar_jobs
                   WHERE source_session_id = ? ORDER BY id LIMIT 2""",
                (source_id,),
            ).fetchall()
            job = jobs[0] if len(jobs) == 1 else None
            terminal_resolution = (
                None
                if job is None
                else conn.execute(
                    "SELECT 1 FROM session_sidebar_terminal_resolutions "
                    "WHERE job_id = ? LIMIT 1",
                    (job["id"],),
                ).fetchone()
            )
            precreate_resolution = (
                None
                if job is None
                else conn.execute(
                    "SELECT 1 FROM session_sidebar_precreate_resolutions "
                    "WHERE job_id = ? LIMIT 1",
                    (job["id"],),
                ).fetchone()
            )
            unbound_resolution = (
                None
                if job is None
                else conn.execute(
                    "SELECT 1 FROM session_sidebar_unbound_resolutions "
                    "WHERE job_id = ? LIMIT 1",
                    (job["id"],),
                ).fetchone()
            )
            v2_attempt_zero_resolution = (
                None
                if job is None
                else conn.execute(
                    "SELECT 1 FROM session_sidebar_v2_attempt_zero_resolutions "
                    "WHERE job_id = ? LIMIT 1",
                    (job["id"],),
                ).fetchone()
            )
            if (
                job is None
                or job["id"] != job_id
                or job["idempotency_key"] != idempotency_key
                or job["bridge_id"] != bridge_id
                or job["state"] != SidebarJobState.FAILED.value
                or job["error_code"] != error_code
                or job["codex_thread_id"] is not None
                or job["completion_digest"] is not None
                or job["visible_at"] is not None
                or terminal_resolution is not None
                or precreate_resolution is not None
                or unbound_resolution is not None
                or v2_attempt_zero_resolution is not None
            ):
                raise ValueError("expected sidebar failure does not match")
            cursor = conn.execute(
                """UPDATE session_sidebar_jobs
                   SET state = ?, attempts = 0, next_attempt_at = ?,
                       lease_digest = NULL, lease_expires_at = NULL,
                       error_code = NULL, updated_at = ?
                   WHERE id = ? AND idempotency_key = ? AND bridge_id = ?
                     AND source_session_id = ? AND state = ? AND error_code = ?
                     AND codex_thread_id IS NULL AND completion_digest IS NULL
                     AND visible_at IS NULL""",
                (
                    SidebarJobState.PENDING.value,
                    retry_time,
                    retry_time,
                    job["id"],
                    idempotency_key,
                    bridge_id,
                    source_id,
                    SidebarJobState.FAILED.value,
                    error_code,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("expected sidebar failure does not match")
            return dict(
                conn.execute(
                    "SELECT * FROM session_sidebar_jobs WHERE id = ?",
                    (job["id"],),
                ).fetchone()
            )

        return self.db._execute_write(_write)

    def sidebar_job_counts(self) -> dict[str, int]:
        counts = {state.value: 0 for state in SidebarJobState}
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            rows = conn.execute(
                """SELECT state, COUNT(*) AS job_count
                   FROM session_sidebar_jobs GROUP BY state"""
            ).fetchall()
        for row in rows:
            counts[row["state"]] = int(row["job_count"])
        return counts

    def record_sidebar_broker_heartbeat(self, *, now: float) -> None:
        heartbeat = _finite_number(now, "sidebar broker heartbeat")
        updated_at = _finite_number(self._clock(), "current time")

        def _write(conn) -> None:
            row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?",
                (_SIDEBAR_BROKER_HEARTBEAT_STATE_KEY,),
            ).fetchone()
            persisted = heartbeat
            if row is not None:
                try:
                    existing = json.loads(row["value_json"])
                except (TypeError, ValueError):
                    existing = None
                if isinstance(existing, dict) and "at" in existing:
                    try:
                        existing_at = _finite_number(
                            existing["at"], "persisted sidebar broker heartbeat"
                        )
                    except (TypeError, ValueError):
                        pass
                    else:
                        persisted = max(existing_at, heartbeat)

            value_json = json.dumps(
                {"at": persisted},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            conn.execute(
                """INSERT INTO session_bridge_state (key, value_json, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value_json = excluded.value_json,
                       updated_at = excluded.updated_at""",
                (_SIDEBAR_BROKER_HEARTBEAT_STATE_KEY, value_json, updated_at),
            )

        self.db._execute_write(_write)

    def record_sidebar_recovery_progress(
        self,
        *,
        lane: str,
        status: str,
        now: float,
    ) -> None:
        if lane not in {"hydration", "registration"}:
            raise ValueError("invalid sidebar recovery lane")
        if status not in {"idle", "visible", "retry", "failed", "unsettled"}:
            raise ValueError("invalid sidebar recovery status")
        cycle_at = _finite_number(now, "sidebar recovery cycle time")
        if cycle_at < 0:
            raise ValueError("invalid sidebar recovery cycle time")
        value_json = json.dumps(
            {
                "version": 1,
                "lane": lane,
                "status": status,
                "at": cycle_at,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        updated_at = _finite_number(self._clock(), "current time")

        def _write(conn) -> None:
            conn.execute(
                """INSERT INTO session_bridge_state (key, value_json, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value_json = excluded.value_json,
                       updated_at = excluded.updated_at""",
                (
                    _SIDEBAR_RECOVERY_PROGRESS_STATE_KEY,
                    value_json,
                    updated_at,
                ),
            )

        self.db._execute_write(_write)

    def sidebar_delivery_status(
        self,
        *,
        now: float | None = None,
        inbox_cwd: str | None = None,
        placement_generation: int = 1,
    ) -> dict[str, Any]:
        status_time = _finite_number(self._clock() if now is None else now, "now")
        effective_inbox_cwd = (
            str(get_hermes_home())
            if inbox_cwd is None
            else _exact_nonempty_text(inbox_cwd, "sidebar inbox cwd")
        )
        if type(placement_generation) is not int or placement_generation < 1:
            raise ValueError("sidebar placement generation must be a positive integer")
        counts = self.sidebar_job_counts()
        counts["sidebar_excluded"] = self.sidebar_exclusion_counts()["total"]
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            provider_rows = conn.execute(
                """SELECT CASE
                              WHEN e.provider = ? THEN ?
                              WHEN e.session_id IS NULL THEN ?
                              ELSE 'invalid'
                          END AS provider,
                          COUNT(*) AS job_count
                     FROM session_sidebar_jobs AS job
                     JOIN sessions AS s ON s.id = job.source_session_id
                     LEFT JOIN external_sessions AS e ON e.session_id = s.id
                    GROUP BY provider""",
                (
                    Provider.CLAUDE.value,
                    Provider.CLAUDE.value,
                    Provider.HERMES.value,
                ),
            ).fetchall()
            oldest = conn.execute(
                """SELECT MIN(eligible_at) AS eligible_at,
                          MIN(CASE WHEN state = ? THEN updated_at ELSE eligible_at END)
                              AS actionable_at
                     FROM session_sidebar_jobs
                    WHERE state IN (?, ?, ?)""",
                (
                    SidebarJobState.LEASED.value,
                    SidebarJobState.PENDING.value,
                    SidebarJobState.RETRY.value,
                    SidebarJobState.LEASED.value,
                ),
            ).fetchone()
            last_visible = conn.execute(
                """SELECT codex_thread_id
                     FROM session_sidebar_jobs
                    WHERE state = ?
                    ORDER BY visible_at DESC, id DESC LIMIT 1""",
                (SidebarJobState.VISIBLE.value,),
            ).fetchone()
            error_rows = conn.execute(
                """SELECT error_code
                     FROM session_sidebar_jobs
                    WHERE error_code IS NOT NULL
                    ORDER BY updated_at DESC, id DESC LIMIT 10"""
            ).fetchall()
            latency_rows = conn.execute(
                """SELECT visible_at - eligible_at AS latency
                     FROM session_sidebar_jobs
                    WHERE state = ? AND visible_at IS NOT NULL
                    ORDER BY visible_at DESC, id DESC
                    LIMIT ?""",
                (SidebarJobState.VISIBLE.value, _SIDEBAR_LATENCY_SAMPLE_LIMIT),
            ).fetchall()
            stage_rows = conn.execute(
                """SELECT eligible_at,
                          COALESCE(indexed_at, created_at) AS effective_indexed_at,
                          created_at, visible_at
                     FROM session_sidebar_jobs
                    WHERE state = ? AND visible_at IS NOT NULL
                    ORDER BY visible_at DESC, id DESC
                    LIMIT ?""",
                (SidebarJobState.VISIBLE.value, _SIDEBAR_LATENCY_SAMPLE_LIMIT),
            ).fetchall()
            expired_row = conn.execute(
                """SELECT COUNT(*) AS job_count
                     FROM session_sidebar_jobs
                    WHERE state = ? AND lease_expires_at <= ?""",
                (SidebarJobState.LEASED.value, status_time),
            ).fetchone()
            resolution_stats = self._sidebar_terminal_resolution_stats_in_connection(
                conn
            )
            execution_blockers = self._sidebar_execution_blockers_in_connection(
                conn, resolution_stats
            )
            placement_row = conn.execute(
                """SELECT
                       SUM(CASE
                               WHEN state = ?
                                AND placement_generation = ?
                                AND placement_verified_at IS NOT NULL
                               THEN 1 ELSE 0
                           END) AS verified_visible,
                       SUM(CASE
                               WHEN state = ? AND error_code = ?
                               THEN 1 ELSE 0
                           END) AS mismatch_count
                   FROM session_sidebar_jobs""",
                (
                    SidebarJobState.VISIBLE.value,
                    placement_generation,
                    SidebarJobState.FAILED.value,
                    "placement_mismatch",
                ),
            ).fetchone()
            health_counts = conn.execute(
                """SELECT
                       SUM(CASE
                               WHEN job.state = ? AND job.error_code = ?
                               THEN 1 ELSE 0
                           END) AS ambiguous,
                       SUM(CASE
                               WHEN job.state = ?
                                AND job.codex_thread_id IS NOT NULL
                                AND (
                                    job.placement_generation IS NULL
                                    OR job.placement_generation < 1
                                    OR job.placement_verified_at IS NULL
                                )
                               THEN 1 ELSE 0
                           END) AS projectless_legacy_count
                     FROM session_sidebar_jobs AS job""",
                (
                    SidebarJobState.FAILED.value,
                    "native_create_ambiguous",
                    SidebarJobState.VISIBLE.value,
                ),
            ).fetchone()
            reconciliation_rows = conn.execute(
                """SELECT proof.state, COUNT(*) AS proof_count
                     FROM session_sidebar_jobs AS job
                     JOIN session_sidebar_reconciliation_proofs AS proof
                       ON proof.proof_digest = job.reconciliation_proof_digest
                    GROUP BY proof.state"""
            ).fetchall()
            reconciliation_blocked_rows = conn.execute(
                """SELECT CASE
                              WHEN job.error_code IN (
                                  'marker_conflict',
                                  'native_create_ambiguous',
                                  'bridge_temporarily_unavailable'
                              ) THEN job.error_code
                              WHEN proof.fixed_reason IN (
                                  'marker_conflict',
                                  'native_create_ambiguous',
                                  'bridge_temporarily_unavailable'
                              ) THEN proof.fixed_reason
                              ELSE 'bridge_temporarily_unavailable'
                          END AS reason_code,
                          COUNT(*) AS reason_count
                     FROM session_sidebar_jobs AS job
                     JOIN session_sidebar_reconciliation_proofs AS proof
                       ON proof.proof_digest = job.reconciliation_proof_digest
                    WHERE proof.state = 'blocked'
                    GROUP BY reason_code"""
            ).fetchall()
            reconciliation_wait_row = conn.execute(
                """SELECT MIN(job.eligible_at) AS eligible_at
                     FROM session_sidebar_jobs AS job
                     LEFT JOIN session_sidebar_reconciliation_proofs AS proof
                       ON proof.proof_digest = job.reconciliation_proof_digest
                    WHERE job.state IN (?, ?, ?)
                      AND (proof.proof_digest IS NULL OR proof.expires_at <= ?)""",
                (
                    SidebarJobState.PENDING.value,
                    SidebarJobState.RETRY.value,
                    SidebarJobState.LEASED.value,
                    status_time,
                ),
            ).fetchone()
            reconciliation_scan_row = conn.execute(
                """SELECT MAX(proof.completed_at) AS completed_at
                     FROM session_sidebar_jobs AS job
                     JOIN session_sidebar_reconciliation_proofs AS proof
                       ON proof.proof_digest = job.reconciliation_proof_digest"""
            ).fetchone()
            reconciliation_outcomes = conn.execute(
                """SELECT
                       SUM(CASE
                               WHEN job.state = ? AND proof.state = 'recovered'
                               THEN 1 ELSE 0
                           END) AS recovered_existing_total,
                       SUM(CASE
                               WHEN job.state = ?
                                AND proof.state = 'absence_proven'
                               THEN 1 ELSE 0
                           END) AS created_new_total
                     FROM session_sidebar_jobs AS job
                     JOIN session_sidebar_reconciliation_proofs AS proof
                       ON proof.proof_digest = job.reconciliation_proof_digest""",
                (
                    SidebarJobState.VISIBLE.value,
                    SidebarJobState.VISIBLE.value,
                ),
            ).fetchone()

        expired_leases = int(expired_row["job_count"])
        counts[SidebarJobState.LEASED.value] -= expired_leases
        counts[SidebarJobState.RETRY.value] += expired_leases
        counts["ambiguous"] = int(health_counts["ambiguous"] or 0)
        counts["needs_attention"] = resolution_stats["blocking_failed_count"]
        counts["projectless_legacy_count"] = int(
            health_counts["projectless_legacy_count"] or 0
        )
        reconciliation_counts = {
            state: 0 for state in ("recovered", "absence_proven", "blocked")
        }
        for row in reconciliation_rows:
            if row["state"] in reconciliation_counts:
                reconciliation_counts[row["state"]] = int(row["proof_count"])
        reconciliation_blocked_codes = {
            code: 0
            for code in (
                "marker_conflict",
                "native_create_ambiguous",
                "bridge_temporarily_unavailable",
            )
        }
        for row in reconciliation_blocked_rows:
            if row["reason_code"] in reconciliation_blocked_codes:
                reconciliation_blocked_codes[row["reason_code"]] = int(
                    row["reason_count"]
                )
        reconciliation_wait_at = reconciliation_wait_row["eligible_at"]
        oldest_reconciliation_wait_age = (
            max(0.0, status_time - float(reconciliation_wait_at))
            if reconciliation_wait_at is not None
            else None
        )
        reconciliation_completed_at = reconciliation_scan_row["completed_at"]
        reconciliation_scan_age = (
            max(0.0, status_time - float(reconciliation_completed_at))
            if reconciliation_completed_at is not None
            else None
        )

        eligible_by_provider = {
            Provider.CLAUDE.value: 0,
            Provider.HERMES.value: 0,
        }
        for row in provider_rows:
            if row["provider"] in eligible_by_provider:
                eligible_by_provider[row["provider"]] = int(row["job_count"])
        oldest_eligible_at = oldest["eligible_at"] if oldest is not None else None
        oldest_actionable_at = oldest["actionable_at"] if oldest is not None else None
        oldest_eligible_age = (
            max(0.0, status_time - float(oldest_eligible_at))
            if oldest_eligible_at is not None
            else None
        )
        oldest_age = (
            max(0.0, status_time - float(oldest_actionable_at))
            if oldest_actionable_at is not None
            else None
        )
        heartbeat = self.get_state("session-bridge:sidebar:broker-heartbeat")
        heartbeat_at = heartbeat.get("at") if isinstance(heartbeat, Mapping) else None
        if not isinstance(heartbeat_at, (int, float)) or isinstance(heartbeat_at, bool):
            heartbeat_at = None
        pending_lane = self.get_state(_SIDEBAR_PENDING_LANE_STATE_KEY)
        fresh_claims = 0
        if pending_lane is not None:
            if (
                set(pending_lane) != {"version", "fresh_claims_since_oldest"}
                or pending_lane.get("version") != 1
                or type(pending_lane.get("fresh_claims_since_oldest")) is not int
                or not 0
                <= pending_lane["fresh_claims_since_oldest"]
                <= _SIDEBAR_FRESH_BURST
            ):
                raise ValueError("invalid sidebar pending lane state")
            fresh_claims = pending_lane["fresh_claims_since_oldest"]
        recovery_progress = self.get_state(_SIDEBAR_RECOVERY_PROGRESS_STATE_KEY)
        recovery_lane: str | None = None
        recovery_status: str | None = None
        recovery_at: float | None = None
        if recovery_progress is not None:
            if (
                set(recovery_progress) != {"version", "lane", "status", "at"}
                or recovery_progress.get("version") != 1
                or recovery_progress.get("lane")
                not in {"hydration", "registration"}
                or recovery_progress.get("status")
                not in {"idle", "visible", "retry", "failed", "unsettled"}
                or not isinstance(recovery_progress.get("at"), (int, float))
                or isinstance(recovery_progress.get("at"), bool)
                or not math.isfinite(float(recovery_progress["at"]))
                or float(recovery_progress["at"]) < 0
            ):
                raise ValueError("invalid sidebar recovery progress state")
            recovery_lane = recovery_progress["lane"]
            recovery_status = recovery_progress["status"]
            recovery_at = float(recovery_progress["at"])
        allowed_codes = SIDEBAR_RETRYABLE_ERRORS | SIDEBAR_FATAL_ERRORS
        recent_codes: list[str] = []
        for row in error_rows:
            code = row["error_code"]
            if code in allowed_codes and code not in recent_codes:
                recent_codes.append(code)
        latencies = sorted(max(0.0, float(row["latency"])) for row in latency_rows)
        stage_latencies: dict[str, list[float]] = {
            "source_to_index": [],
            "index_to_queue": [],
            "queue_to_visible": [],
            "source_to_visible": [],
        }
        for row in stage_rows:
            eligible_at = float(row["eligible_at"])
            indexed_at = float(row["effective_indexed_at"])
            created_at = float(row["created_at"])
            visible_at = float(row["visible_at"])
            stage_latencies["source_to_index"].append(
                max(0.0, indexed_at - eligible_at)
            )
            stage_latencies["index_to_queue"].append(
                max(0.0, created_at - indexed_at)
            )
            stage_latencies["queue_to_visible"].append(
                max(0.0, visible_at - created_at)
            )
            stage_latencies["source_to_visible"].append(
                max(0.0, visible_at - eligible_at)
            )
        for values in stage_latencies.values():
            values.sort()
        canary = _sidebar_placement_canary_public_status(
            self.get_state(_SIDEBAR_PLACEMENT_CANARY_STATE_KEY),
            placement_generation=placement_generation,
        )
        return {
            "eligible_by_provider": eligible_by_provider,
            "counts": counts,
            "blocking_failed_count": resolution_stats["blocking_failed_count"],
            "terminally_resolved_failed_count": resolution_stats[
                "terminally_resolved_failed_count"
            ],
            "ineffective_terminal_resolution_count": resolution_stats[
                "ineffective_terminal_resolution_count"
            ],
            "terminal_resolution_ledger_valid": resolution_stats["ledger_valid"],
            "terminal_resolutions": {
                "total": resolution_stats["total"],
                "effective": resolution_stats["effective"],
                "ineffective": resolution_stats["ineffective"],
                "by_resolution_code": resolution_stats["by_resolution_code"],
            },
            "execution_blockers": list(execution_blockers),
            "oldest_eligible_age_seconds": oldest_eligible_age,
            "oldest_pending_age_seconds": oldest_age,
            "last_heartbeat_at": float(heartbeat_at)
            if heartbeat_at is not None
            else None,
            "last_visible_task_id": (
                last_visible["codex_thread_id"] if last_visible is not None else None
            ),
            "recent_error_codes": recent_codes,
            "reconciliation_counts": reconciliation_counts,
            "reconciliation_blocked_codes": reconciliation_blocked_codes,
            "oldest_reconciliation_wait_age_seconds": (
                oldest_reconciliation_wait_age
            ),
            "reconciliation_scan_age_seconds": reconciliation_scan_age,
            "recovered_existing_total": int(
                reconciliation_outcomes["recovered_existing_total"] or 0
            ),
            "created_new_total": int(
                reconciliation_outcomes["created_new_total"] or 0
            ),
            "delivery_latency_seconds": {
                "p50": _nearest_rank_percentile(latencies, 0.50),
                "p95": _nearest_rank_percentile(latencies, 0.95),
                "p99": _nearest_rank_percentile(latencies, 0.99),
            },
            "stage_latency_seconds": {
                stage: {
                    "p50": _nearest_rank_percentile(values, 0.50),
                    "p95": _nearest_rank_percentile(values, 0.95),
                }
                for stage, values in stage_latencies.items()
            },
            "scheduler": {
                "fresh_claims_since_oldest": fresh_claims,
                "next_lane": (
                    "oldest"
                    if fresh_claims == _SIDEBAR_FRESH_BURST
                    else "fresh"
                ),
            },
            "recovery": {
                "lane": recovery_lane,
                "status": recovery_status,
                "last_cycle_at": recovery_at,
            },
            "placement": {
                "inbox_cwd": effective_inbox_cwd,
                "generation": placement_generation,
                "verified_visible": int(placement_row["verified_visible"] or 0),
                "mismatch_count": int(placement_row["mismatch_count"] or 0),
                "canary": canary,
            },
        }

    def get_sidebar_job_for_source(
        self,
        source_session_id: str,
    ) -> dict[str, Any] | None:
        from .sidebar import sidebar_idempotency_key

        sidebar_idempotency_key(source_session_id)
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            row = conn.execute(
                """SELECT * FROM session_sidebar_jobs
                   WHERE source_session_id = ?""",
                (source_session_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def get_sidebar_job_by_id(self, job_id: str) -> dict[str, Any] | None:
        """Read one exact sidebar job without accepting a source identity alias."""

        normalized_job_id = _exact_nonempty_text(job_id, "sidebar job ID")
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            row = conn.execute(
                "SELECT * FROM session_sidebar_jobs WHERE id = ?",
                (normalized_job_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def get_sidebar_candidate_for_delivery(
        self,
        source_session_id: str,
    ) -> SidebarCandidate:
        """Read immutable, bounded delivery metadata for an already queued job."""

        source_id = _exact_nonempty_text(source_session_id, "sidebar source session ID")
        state_key = _sidebar_delivery_state_key(source_id)
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            row = conn.execute(
                """SELECT job.source_session_id, job.idempotency_key,
                          job.bridge_id, job.eligible_at, state.value_json
                     FROM session_sidebar_jobs AS job
                     LEFT JOIN session_bridge_state AS state ON state.key = ?
                    WHERE job.source_session_id = ?""",
                (state_key, source_id),
            ).fetchone()
        if row is None:
            raise KeyError(source_id)
        if row["value_json"] is None:
            raise ValueError("missing sidebar delivery candidate")
        candidate = _decode_sidebar_delivery_candidate(row["value_json"])
        expected_provider = _validated_sidebar_job_provider(dict(row))
        if (
            candidate.source_session_id != row["source_session_id"]
            or candidate.bridge_id != row["bridge_id"]
            or candidate.provider is not expected_provider
            or candidate.eligible_at != float(row["eligible_at"])
        ):
            raise ValueError("invalid sidebar delivery candidate identity")
        return candidate

    def get_sidebar_preview_source(
        self,
        source_session_id: str,
    ) -> dict[str, Any]:
        """Read indexed source metadata, active messages, and identity atomically."""
        source_id = _exact_nonempty_text(
            source_session_id,
            "sidebar source session ID",
        )
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            conn.execute("BEGIN")
            try:
                session_row = conn.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (source_id,),
                ).fetchone()
                message_rows = conn.execute(
                    "SELECT * FROM messages WHERE session_id = ? ORDER BY id",
                    (source_id,),
                ).fetchall()
                external_row = conn.execute(
                    "SELECT * FROM external_sessions WHERE session_id = ?",
                    (source_id,),
                ).fetchone()
            finally:
                conn.rollback()
        if session_row is None:
            raise KeyError(source_id)

        session = dict(session_row)
        message_records = [dict(row) for row in message_rows]
        decode_content = self.db._decode_content
        if session.get("source") == _PROFILE_SHADOW_SOURCE:
            profile_matches: list[
                tuple[dict[str, Any], list[dict[str, Any]], Callable[[Any], Any]]
            ] = []
            with self._native_hermes_databases() as databases:
                for _profile, database, owned in databases:
                    if not owned or not self._profile_catalog_compatible(database):
                        continue
                    with database._lock:
                        profile_conn = database._conn
                        assert profile_conn is not None
                        profile_session = profile_conn.execute(
                            "SELECT * FROM sessions WHERE id = ?",
                            (source_id,),
                        ).fetchone()
                        if profile_session is None:
                            continue
                        profile_messages = profile_conn.execute(
                            "SELECT * FROM messages WHERE session_id = ? ORDER BY id",
                            (source_id,),
                        ).fetchall()
                    profile_matches.append((
                        dict(profile_session),
                        [dict(row) for row in profile_messages],
                        database._decode_content,
                    ))
            if not profile_matches:
                # A root shadow row whose profile database no longer carries the
                # session. Measured 2026-08-24, 3 of the 38 shadows in the live
                # root database dangle this way. That is ABSENT, not ambiguous --
                # and this method already raises KeyError above for a root row it
                # cannot find, so an absent source keeps one error kind.
                raise KeyError(source_id)
            if len(profile_matches) != 1:
                raise ValueError("sidebar source identity is ambiguous")
            session, message_records, decode_content = profile_matches[0]
            external_row = None

        if external_row is not None:
            external = dict(external_row)
            if external.get("provider") != Provider.CLAUDE.value:
                raise ValueError("sidebar source provider mismatch")
            provider = Provider.CLAUDE
            cursor = external.get("last_native_cursor")
            source_hash = external.get("last_native_hash")
        else:
            provider = Provider.HERMES
            identity = _native_session_snapshot_identity(
                session,
                message_records,
                decode_content=decode_content,
            )
            cursor = identity["cursor"]
            source_hash = identity["source_hash"]
        if (
            not isinstance(cursor, str)
            or not cursor
            or not isinstance(source_hash, str)
            or not source_hash
        ):
            raise ValueError("sidebar source snapshot identity is unavailable")
        if (
            provider is Provider.CLAUDE
            and not source_id.startswith(f"{Provider.CLAUDE.value}:")
        ) or (
            provider is Provider.HERMES
            and source_id.startswith(("claude:", "codex:"))
        ):
            raise ValueError("sidebar source provider mismatch")

        messages: list[dict[str, Any]] = []
        timestamps: list[float] = []
        for raw_timestamp in (session.get("started_at"), session.get("ended_at")):
            if (
                isinstance(raw_timestamp, (int, float))
                and not isinstance(raw_timestamp, bool)
                and math.isfinite(float(raw_timestamp))
            ):
                timestamps.append(float(raw_timestamp))
        for message in message_records:
            message["content"] = decode_content(message.get("content"))
            if message.get("tool_calls"):
                try:
                    message["tool_calls"] = json.loads(message["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    message["tool_calls"] = []
            if message.get("active") != 1:
                continue
            timestamp = message.get("timestamp")
            if (
                isinstance(timestamp, (int, float))
                and not isinstance(timestamp, bool)
                and math.isfinite(float(timestamp))
            ):
                timestamps.append(float(timestamp))
            messages.append(message)

        return {
            "source_session_id": source_id,
            "provider": provider.value,
            "source_cursor": cursor,
            "source_hash": source_hash,
            "title": session.get("title"),
            "cwd": session.get("cwd"),
            "captured_at": max(timestamps, default=0.0),
            "messages": messages,
            "git_root": session.get("git_repo_root"),
            "git_branch": session.get("git_branch"),
        }

    def seed_sidebar_hydration_job(
        self,
        source_session_id: str,
        bridge_id: str,
        codex_thread_id: str,
        source_cursor: str,
        source_hash: str,
        preview_version: int,
        preview_digest: str,
        hydration_marker: str,
        now: float,
    ) -> dict[str, Any]:
        source_id = _exact_nonempty_text(source_session_id, "hydration source ID")
        normalized_bridge = _exact_nonempty_text(bridge_id, "hydration bridge ID")
        thread_id = _exact_nonempty_text(
            codex_thread_id,
            "hydration Codex thread ID",
        )
        cursor = _exact_nonempty_text(source_cursor, "hydration source cursor")
        source_identity_hash = _exact_nonempty_text(
            source_hash,
            "hydration source hash",
        )
        if type(preview_version) is not int or preview_version != 1:
            raise ValueError("hydration preview version must be 1")
        digest = _lowercase_sha256(preview_digest, "hydration preview digest")
        marker = _exact_nonempty_text(hydration_marker, "hydration marker")
        seeded_at = _finite_number(now, "hydration seed time")
        job_id = "sidebar-hydration:" + hashlib.sha256(
            f"{normalized_bridge}\0{thread_id}".encode("utf-8")
        ).hexdigest()

        def _write(conn):
            sidebar = conn.execute(
                """SELECT source_session_id, bridge_id, codex_thread_id, state
                     FROM session_sidebar_jobs
                    WHERE source_session_id = ? AND bridge_id = ?""",
                (source_id, normalized_bridge),
            ).fetchone()
            if sidebar is None or sidebar["state"] != SidebarJobState.VISIBLE.value:
                raise ValueError("sidebar hydration requires a visible sidebar job")
            if sidebar["codex_thread_id"] != thread_id:
                raise ValueError("sidebar hydration task identity mismatch")
            conn.execute(
                """INSERT OR IGNORE INTO session_sidebar_hydration_jobs (
                       id, source_session_id, bridge_id, codex_thread_id,
                       source_cursor, source_hash, preview_version, preview_digest,
                       hydration_marker, state, attempts, next_attempt_at,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                (
                    job_id,
                    source_id,
                    normalized_bridge,
                    thread_id,
                    cursor,
                    source_identity_hash,
                    preview_version,
                    digest,
                    marker,
                    SidebarHydrationState.PENDING.value,
                    seeded_at,
                    seeded_at,
                    seeded_at,
                ),
            )
            row = conn.execute(
                """SELECT * FROM session_sidebar_hydration_jobs
                   WHERE source_session_id = ?""",
                (source_id,),
            ).fetchone()
            if row is None:
                raise ValueError("sidebar hydration seed failed")
            result = dict(row)
            expected = {
                "id": job_id,
                "source_session_id": source_id,
                "bridge_id": normalized_bridge,
                "codex_thread_id": thread_id,
                "source_cursor": cursor,
                "source_hash": source_identity_hash,
                "preview_version": preview_version,
                "preview_digest": digest,
                "hydration_marker": marker,
            }
            if any(result[key] != value for key, value in expected.items()):
                raise ValueError("sidebar hydration seed identity conflict")
            return result

        return self.db._execute_write(_write)

    def list_sidebar_hydration_candidates(
        self,
        *,
        now: float,
        backfill_days: int | None,
        limit: int,
        after_visible_at: float | None = None,
        after_job_id: str | None = None,
    ) -> list[dict[str, Any]]:
        checked_at = _finite_number(now, "hydration inventory time")
        if backfill_days is not None and (
            type(backfill_days) is not int or not 0 <= backfill_days <= 3_650
        ):
            raise ValueError(
                "hydration inventory backfill days must be between 0 and 3650"
            )
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("hydration inventory limit must be between 1 and 500")
        if (after_visible_at is None) != (after_job_id is None):
            raise ValueError("hydration inventory cursor is incomplete")
        cursor_time: float | None = None
        cursor_job: str | None = None
        if after_visible_at is not None:
            cursor_time = _finite_number(
                after_visible_at,
                "hydration inventory cursor time",
            )
            cursor_job = _exact_nonempty_text(
                after_job_id,
                "hydration inventory cursor job ID",
            )

        reservation_cutover = self.get_state(
            _SIDEBAR_CREATE_RESERVATION_CUTOVER_STATE_KEY
        )
        signed_registration_sql = ""
        signed_registration_at: float | None = None
        if reservation_cutover is not None:
            if reservation_cutover.get("version") != 1:
                raise ValueError(
                    "sidebar create reservation cutover state has an invalid version"
                )
            signed_registration_at = _finite_number(
                reservation_cutover.get("applied_at"),
                "sidebar create reservation cutover time",
            )
            signed_registration_sql = " AND job.visible_at >= ?"

        cutoff = (
            None
            if backfill_days is None
            else checked_at - backfill_days * 86_400.0
        )
        cutoff_sql = ""
        pagination_sql = ""
        parameters: list[object] = [
            Provider.CLAUDE.value,
            OriginKind.NATIVE.value,
            Provider.CODEX.value,
            OriginKind.BRIDGE_PLACEHOLDER.value,
            Relation.MIRRORS.value,
            SidebarJobState.VISIBLE.value,
        ]
        if cutoff is not None:
            cutoff_sql = " AND job.eligible_at >= ?"
            parameters.append(cutoff)
        if signed_registration_at is not None:
            parameters.append(signed_registration_at)
        if cursor_time is not None and cursor_job is not None:
            pagination_sql = (
                " AND (job.visible_at < ?"
                " OR (job.visible_at = ? AND job.id > ?))"
            )
            parameters.extend((cursor_time, cursor_time, cursor_job))
        parameters.append(limit)

        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            rows = conn.execute(
                f"""SELECT job.id AS job_id,
                           job.source_session_id,
                           job.bridge_id,
                           job.codex_thread_id,
                           job.eligible_at,
                           job.visible_at
                      FROM session_sidebar_jobs AS job
                      JOIN external_sessions AS source
                        ON source.session_id = job.source_session_id
                       AND source.provider = ?
                       AND source.origin_kind = ?
                      JOIN external_sessions AS target
                        ON target.provider = ?
                       AND target.native_id = job.codex_thread_id
                       AND target.origin_kind = ?
                       AND target.origin_bridge_id = job.bridge_id
                      JOIN session_links AS link
                        ON link.from_session_id = job.source_session_id
                       AND link.to_session_id = target.session_id
                       AND link.bridge_id = job.bridge_id
                       AND link.relation = ?
                 LEFT JOIN session_sidebar_hydration_jobs AS hydration
                        ON hydration.source_session_id = job.source_session_id
                     WHERE job.state = ?
                       {cutoff_sql}
                       AND hydration.id IS NULL
                       {signed_registration_sql}
                       {pagination_sql}
                  ORDER BY job.visible_at DESC, job.id
                     LIMIT ?""",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_sidebar_hydration_jobs(
        self,
        *,
        now: float,
        limit: int = 1,
    ) -> list[dict[str, Any]]:
        claim_time = _finite_number(now, "hydration claim time")
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("hydration claim limit must be between 1 and 10")

        def _write(conn):
            conn.execute(
                """UPDATE session_sidebar_hydration_jobs
                   SET state = ?, next_attempt_at = ?, lease_digest = NULL,
                       lease_expires_at = NULL, updated_at = ?
                   WHERE state = ? AND lease_expires_at <= ?""",
                (
                    SidebarHydrationState.RETRY.value,
                    claim_time,
                    claim_time,
                    SidebarHydrationState.LEASED.value,
                    claim_time,
                ),
            )
            active = conn.execute(
                """SELECT 1 FROM session_sidebar_hydration_jobs
                   WHERE state = ? AND lease_expires_at > ? LIMIT 1""",
                (SidebarHydrationState.LEASED.value, claim_time),
            ).fetchone()
            if active is not None:
                return []
            row = conn.execute(
                """SELECT * FROM session_sidebar_hydration_jobs
                   WHERE state IN (?, ?) AND next_attempt_at <= ?
                   ORDER BY CASE WHEN send_reserved_at IS NOT NULL THEN 0 ELSE 1 END,
                            next_attempt_at, created_at, id
                   LIMIT 1""",
                (
                    SidebarHydrationState.PENDING.value,
                    SidebarHydrationState.RETRY.value,
                    claim_time,
                ),
            ).fetchone()
            if row is None:
                return []
            token = _exact_nonempty_text(
                self._sidebar_token_factory(),
                "hydration lease token",
            )
            lease_digest = _hydration_lease_digest(token)
            duplicate = conn.execute(
                """SELECT 1 FROM session_sidebar_hydration_jobs
                   WHERE lease_digest = ? OR completion_digest = ? LIMIT 1""",
                (lease_digest, _hydration_completion_digest(token)),
            ).fetchone()
            if duplicate is not None:
                raise ValueError("hydration lease token factory returned a duplicate")
            cursor = conn.execute(
                """UPDATE session_sidebar_hydration_jobs
                   SET state = ?, lease_digest = ?, lease_expires_at = ?,
                       error_code = NULL, updated_at = ?
                   WHERE id = ? AND state = ? AND attempts = ?""",
                (
                    SidebarHydrationState.LEASED.value,
                    lease_digest,
                    claim_time + _SIDEBAR_HYDRATION_LEASE_SECONDS,
                    claim_time,
                    row["id"],
                    row["state"],
                    row["attempts"],
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("stale sidebar hydration claim")
            claimed = dict(
                conn.execute(
                    "SELECT * FROM session_sidebar_hydration_jobs WHERE id = ?",
                    (row["id"],),
                ).fetchone()
            )
            claimed["lease_token"] = token
            claimed["send_reserved"] = claimed["send_reserved_at"] is not None
            return [claimed]

        return self.db._execute_write(_write)

    def reserve_sidebar_hydration_send(
        self,
        *,
        lease_token: str,
        now: float,
    ) -> dict[str, Any]:
        token_digest = _hydration_lease_digest(lease_token)
        reserved_at = _finite_number(now, "hydration reservation time")

        def _write(conn):
            job = _find_sidebar_hydration_by_lease(conn, token_digest)
            if job is None:
                raise ValueError("invalid hydration lease token")
            if float(job["lease_expires_at"]) <= reserved_at:
                _recover_expired_sidebar_hydration(conn, job, now=reserved_at)
                return None, True
            if job["send_reserved_at"] is None:
                cursor = conn.execute(
                    """UPDATE session_sidebar_hydration_jobs
                       SET send_reserved_at = ?, updated_at = ?
                       WHERE id = ? AND state = ? AND lease_digest = ?""",
                    (
                        reserved_at,
                        reserved_at,
                        job["id"],
                        SidebarHydrationState.LEASED.value,
                        token_digest,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("stale hydration send reservation")
            result = dict(
                conn.execute(
                    "SELECT * FROM session_sidebar_hydration_jobs WHERE id = ?",
                    (job["id"],),
                ).fetchone()
            )
            result["send_reserved"] = True
            return result, False

        result, expired = self.db._execute_write(_write)
        if expired:
            raise ValueError("hydration lease has expired")
        return result

    def commit_sidebar_hydration_job(
        self,
        *,
        lease_token: str,
        codex_thread_id: str,
        hydration_marker: str,
        now: float,
    ) -> dict[str, Any]:
        lease_digest = _hydration_lease_digest(lease_token)
        completion_digest = _hydration_completion_digest(lease_token)
        thread_id = _exact_nonempty_text(
            codex_thread_id,
            "hydration Codex thread ID",
        )
        marker = _exact_nonempty_text(hydration_marker, "hydration marker")
        committed_at = _finite_number(now, "hydration commit time")

        def _write(conn):
            job, matched_completion = _find_sidebar_hydration_for_completion(
                conn,
                lease_digest=lease_digest,
                completion_digest=completion_digest,
            )
            if job is None:
                raise ValueError("invalid hydration lease token")
            if (
                job["codex_thread_id"] != thread_id
                or not hmac.compare_digest(job["hydration_marker"], marker)
            ):
                raise ValueError("hydration task or marker mismatch")
            if matched_completion:
                if job["state"] != SidebarHydrationState.VISIBLE.value:
                    raise ValueError("invalid hydration completion state")
                return dict(job), False
            if float(job["lease_expires_at"]) <= committed_at:
                _recover_expired_sidebar_hydration(conn, job, now=committed_at)
                return None, True
            if job["send_reserved_at"] is None:
                raise ValueError("hydration send was not reserved")
            cursor = conn.execute(
                """UPDATE session_sidebar_hydration_jobs
                   SET state = ?, lease_digest = NULL, lease_expires_at = NULL,
                       sent_at = COALESCE(sent_at, ?), verified_at = ?,
                       completion_digest = ?, error_code = NULL, updated_at = ?
                   WHERE id = ? AND state = ? AND lease_digest = ?""",
                (
                    SidebarHydrationState.VISIBLE.value,
                    committed_at,
                    committed_at,
                    completion_digest,
                    committed_at,
                    job["id"],
                    SidebarHydrationState.LEASED.value,
                    lease_digest,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("stale hydration completion")
            return (
                dict(
                    conn.execute(
                        "SELECT * FROM session_sidebar_hydration_jobs WHERE id = ?",
                        (job["id"],),
                    ).fetchone()
                ),
                False,
            )

        result, expired = self.db._execute_write(_write)
        if expired:
            raise ValueError("hydration lease has expired")
        return result

    def fail_sidebar_hydration_job(
        self,
        *,
        lease_token: str,
        error_code: str,
        codex_thread_id: str,
        now: float,
    ) -> dict[str, Any]:
        if (
            type(error_code) is not str
            or error_code not in HYDRATION_RETRYABLE_ERRORS | HYDRATION_FATAL_ERRORS
        ):
            raise ValueError("hydration error code is not in the fixed allowlist")
        token_digest = _hydration_lease_digest(lease_token)
        thread_id = _exact_nonempty_text(
            codex_thread_id,
            "hydration Codex thread ID",
        )
        failure_time = _finite_number(now, "hydration failure time")

        def _write(conn):
            job = _find_sidebar_hydration_by_lease(conn, token_digest)
            if job is None:
                raise ValueError("invalid hydration lease token")
            if job["codex_thread_id"] != thread_id:
                raise ValueError("hydration task identity mismatch")
            if float(job["lease_expires_at"]) <= failure_time:
                _recover_expired_sidebar_hydration(conn, job, now=failure_time)
                return None, True
            if (
                error_code == "hydration_send_ambiguous"
                and job["send_reserved_at"] is None
            ):
                raise ValueError("ambiguous hydration send was not reserved")
            attempts = int(job["attempts"]) + 1
            if error_code in HYDRATION_FATAL_ERRORS:
                state = SidebarHydrationState.FAILED
                next_attempt_at = float(job["next_attempt_at"])
            elif attempts >= _SIDEBAR_HYDRATION_MAX_ATTEMPTS:
                state = SidebarHydrationState.FAILED
                next_attempt_at = failure_time
            else:
                state = SidebarHydrationState.RETRY
                if error_code == "hydration_send_ambiguous":
                    delay = 15.0
                elif error_code == "broker_time_budget":
                    delay = 0.0
                else:
                    delay = min(300.0, 5.0 * (2 ** (attempts - 1)))
                next_attempt_at = failure_time + delay
            cursor = conn.execute(
                """UPDATE session_sidebar_hydration_jobs
                   SET state = ?, attempts = ?, next_attempt_at = ?,
                       lease_digest = NULL, lease_expires_at = NULL,
                       error_code = ?, updated_at = ?
                   WHERE id = ? AND state = ? AND lease_digest = ?""",
                (
                    state.value,
                    attempts,
                    next_attempt_at,
                    error_code,
                    failure_time,
                    job["id"],
                    SidebarHydrationState.LEASED.value,
                    token_digest,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("stale hydration failure")
            return (
                dict(
                    conn.execute(
                        "SELECT * FROM session_sidebar_hydration_jobs WHERE id = ?",
                        (job["id"],),
                    ).fetchone()
                ),
                False,
            )

        result, expired = self.db._execute_write(_write)
        if expired:
            raise ValueError("hydration lease has expired")
        return result

    def recover_absent_sidebar_hydration_send(
        self,
        *,
        source_session_id: str,
        codex_thread_id: str,
        hydration_marker: str,
        evidence_digest: str,
        observed_turn_count: int,
        now: float,
    ) -> dict[str, Any]:
        source_id = _exact_nonempty_text(
            source_session_id,
            "hydration recovery source session ID",
        )
        thread_id = _exact_nonempty_text(
            codex_thread_id,
            "hydration recovery Codex thread ID",
        )
        marker = _exact_nonempty_text(
            hydration_marker,
            "hydration recovery marker",
        )
        evidence = _sha256_text(
            evidence_digest,
            "hydration recovery evidence digest",
        )
        if type(observed_turn_count) is not int or observed_turn_count != 1:
            raise ValueError(
                "hydration recovery requires exactly one observed placeholder turn"
            )
        recovered_at = _finite_number(now, "hydration recovery time")

        def _write(conn):
            job = conn.execute(
                """SELECT * FROM session_sidebar_hydration_jobs
                   WHERE source_session_id = ?""",
                (source_id,),
            ).fetchone()
            if job is None:
                raise ValueError("hydration recovery job does not exist")
            expected = {
                "codex_thread_id": thread_id,
                "hydration_marker": marker,
                "state": SidebarHydrationState.FAILED.value,
                "error_code": "hydration_send_ambiguous",
            }
            if (
                any(job[key] != value for key, value in expected.items())
                or job["send_reserved_at"] is None
                or job["completion_digest"] is not None
                or job["sent_at"] is not None
                or job["verified_at"] is not None
                or job["lease_digest"] is not None
                or job["lease_expires_at"] is not None
            ):
                raise ValueError("hydration recovery job is not exact failed ambiguity")

            state_key = (
                "session-bridge:sidebar:hydration-absent-recovery:"
                + str(job["id"])
            )
            marker_digest = hashlib.sha256(marker.encode("utf-8")).hexdigest()
            snapshot = {
                "version": 1,
                "source_session_id": source_id,
                "codex_thread_id": thread_id,
                "hydration_marker_digest": marker_digest,
                "evidence_digest": evidence,
                "observed_turn_count": observed_turn_count,
                "recovered_at": recovered_at,
            }
            value_json = json.dumps(
                snapshot,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            try:
                conn.execute(
                    """INSERT INTO session_bridge_state (
                       key, value_json, updated_at
                       ) VALUES (?, ?, ?)""",
                    (state_key, value_json, recovered_at),
                )
            except sqlite3.IntegrityError:
                raise ValueError(
                    "hydration absent-send recovery already exists"
                ) from None

            cursor = conn.execute(
                """UPDATE session_sidebar_hydration_jobs
                      SET state = ?, attempts = 0, next_attempt_at = ?,
                          lease_digest = NULL, lease_expires_at = NULL,
                          send_reserved_at = NULL, sent_at = NULL,
                          verified_at = NULL, completion_digest = NULL,
                          error_code = NULL, updated_at = ?
                    WHERE id = ? AND state = ? AND error_code = ?
                      AND send_reserved_at IS NOT NULL
                      AND completion_digest IS NULL""",
                (
                    SidebarHydrationState.PENDING.value,
                    recovered_at,
                    recovered_at,
                    job["id"],
                    SidebarHydrationState.FAILED.value,
                    "hydration_send_ambiguous",
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("hydration recovery compare-and-swap failed")
            return dict(
                conn.execute(
                    """SELECT * FROM session_sidebar_hydration_jobs
                       WHERE id = ?""",
                    (job["id"],),
                ).fetchone()
            )

        return self.db._execute_write(_write)

    def sidebar_hydration_status(self, now: float) -> dict[str, Any]:
        checked_at = _finite_number(now, "hydration status time")
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            counts = {
                state.value: 0
                for state in SidebarHydrationState
            }
            for row in conn.execute(
                """SELECT state, COUNT(*) AS count
                   FROM session_sidebar_hydration_jobs GROUP BY state"""
            ).fetchall():
                counts[row["state"]] = int(row["count"])
            health_counts_row = conn.execute(
                """SELECT
                       SUM(CASE WHEN state = ? THEN 1 ELSE 0 END) AS pending,
                       SUM(CASE WHEN state = ? THEN 1 ELSE 0 END) AS leased,
                       SUM(CASE
                               WHEN state = ?
                                AND (error_code IS NULL OR error_code != ?)
                               THEN 1 ELSE 0
                           END) AS retry,
                       SUM(CASE WHEN state = ? THEN 1 ELSE 0 END) AS committed,
                       SUM(CASE WHEN error_code = ? THEN 1 ELSE 0 END) AS ambiguous,
                       SUM(CASE
                               WHEN state = ? AND (error_code IS NULL OR error_code != ?)
                               THEN 1 ELSE 0
                           END) AS failed
                     FROM session_sidebar_hydration_jobs""",
                (
                    SidebarHydrationState.PENDING.value,
                    SidebarHydrationState.LEASED.value,
                    SidebarHydrationState.RETRY.value,
                    "hydration_send_ambiguous",
                    SidebarHydrationState.VISIBLE.value,
                    "hydration_send_ambiguous",
                    SidebarHydrationState.FAILED.value,
                    "hydration_send_ambiguous",
                ),
            ).fetchone()
            active = conn.execute(
                """SELECT 1 FROM session_sidebar_hydration_jobs
                   WHERE state = ? AND lease_expires_at > ? LIMIT 1""",
                (SidebarHydrationState.LEASED.value, checked_at),
            ).fetchone()
            reserved_reconciliation = conn.execute(
                """SELECT COUNT(*) AS count
                   FROM session_sidebar_hydration_jobs
                   WHERE send_reserved_at IS NOT NULL AND state IN (?, ?)""",
                (
                    SidebarHydrationState.LEASED.value,
                    SidebarHydrationState.RETRY.value,
                ),
            ).fetchone()
            oldest = conn.execute(
                """SELECT MIN(next_attempt_at) AS actionable_at
                     FROM session_sidebar_hydration_jobs
                    WHERE state IN (?, ?)""",
                (
                    SidebarHydrationState.PENDING.value,
                    SidebarHydrationState.RETRY.value,
                ),
            ).fetchone()
            error_rows = conn.execute(
                """SELECT error_code
                     FROM session_sidebar_hydration_jobs
                    WHERE error_code IS NOT NULL
                    ORDER BY updated_at DESC, id DESC LIMIT 10"""
            ).fetchall()
        oldest_at = oldest["actionable_at"] if oldest is not None else None
        recent_codes: list[str] = []
        allowed_codes = HYDRATION_RETRYABLE_ERRORS | HYDRATION_FATAL_ERRORS
        for row in error_rows:
            code = row["error_code"]
            if code in allowed_codes and code not in recent_codes:
                recent_codes.append(code)
        return {
            "counts": counts,
            "health_counts": {
                key: int(health_counts_row[key] or 0)
                for key in (
                    "pending",
                    "leased",
                    "retry",
                    "committed",
                    "ambiguous",
                    "failed",
                )
            },
            "active_lease": active is not None,
            "reserved_reconciliation": int(reserved_reconciliation["count"]),
            "oldest_pending_age_seconds": (
                max(0.0, checked_at - float(oldest_at))
                if oldest_at is not None
                else None
            ),
            "recent_error_codes": recent_codes,
        }

    def ensure_sidebar_lineage(
        self,
        *,
        source_session_id: str,
        bridge_id: str,
        codex_thread_id: str,
    ) -> dict[str, Any]:
        """Idempotently bind one verified native Codex task to its source."""

        source_id = _exact_nonempty_text(source_session_id, "sidebar source session ID")
        normalized_bridge_id = _exact_nonempty_text(bridge_id, "sidebar bridge ID")
        thread_id = _exact_nonempty_text(codex_thread_id, "Codex thread ID")

        def _write(conn):
            return _ensure_sidebar_lineage_row(
                conn,
                source_session_id=source_id,
                bridge_id=normalized_bridge_id,
                codex_thread_id=thread_id,
                created_at=float(self._clock()),
            )

        return self.db._execute_write(_write)

    def _new_sidebar_lease(self, conn: Any) -> tuple[str, str]:
        token = self._sidebar_token_factory()
        digest = _sidebar_lease_digest(token)
        existing, _ = _find_sidebar_job_by_digest(
            conn,
            digest,
            allow_completion=True,
        )
        if existing is not None:
            raise ValueError("sidebar lease token factory returned a duplicate")
        return token, digest

    def enqueue_mirror_job(
        self,
        source_session_id: str,
        target_provider: Provider,
        *,
        policy_generation: int,
    ) -> dict[str, Any]:
        provider = _external_provider(target_provider)
        if (
            not isinstance(policy_generation, int)
            or isinstance(policy_generation, bool)
            or policy_generation < 0
        ):
            raise ValueError("policy generation must be a non-negative integer")
        idempotency_key = _stable_id(
            "mirror-job",
            source_session_id,
            provider.value,
            str(policy_generation),
        )
        job_id = f"job:{idempotency_key}"
        now = float(self._clock())

        def _write(conn):
            conn.execute(
                """INSERT OR IGNORE INTO session_mirror_jobs (
                   id, idempotency_key, source_session_id, target_provider,
                   state, attempts, next_attempt_at, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                (
                    job_id,
                    idempotency_key,
                    source_session_id,
                    provider.value,
                    MirrorJobState.QUEUED.value,
                    now,
                    now,
                    now,
                ),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM session_mirror_jobs WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
            )

        return self.db._execute_write(_write)

    def list_mirror_jobs(
        self,
        states: Sequence[MirrorJobState | str],
        *,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("mirror job list limit must be between 1 and 1000")
        if isinstance(states, (str, bytes)) or not isinstance(states, Sequence):
            raise TypeError("mirror job states must be a sequence")
        normalized_states: list[MirrorJobState] = []
        for state in states:
            try:
                normalized = MirrorJobState(state)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"unknown mirror job state: {state!r}") from exc
            if normalized not in normalized_states:
                normalized_states.append(normalized)
        if not normalized_states:
            return []

        placeholders = ",".join("?" for _ in normalized_states)
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            rows = conn.execute(
                f"""SELECT * FROM session_mirror_jobs
                    WHERE state IN ({placeholders})
                    ORDER BY created_at, id
                    LIMIT ?""",
                [*(state.value for state in normalized_states), limit],
            ).fetchall()
        return [dict(row) for row in rows]

    def mirror_job_counts(self) -> dict[str, int]:
        counts = {state.value: 0 for state in MirrorJobState}
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            rows = conn.execute(
                """SELECT state, COUNT(*) AS job_count
                   FROM session_mirror_jobs
                   GROUP BY state"""
            ).fetchall()
        for row in rows:
            counts[row["state"]] = int(row["job_count"])
        return counts

    def claim_due_jobs(
        self,
        *,
        now: float,
        limit: int,
        policy: Any,
        job_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(float(now))
        ):
            raise ValueError("now must be a finite number")
        from .mirror import claim_due_mirror_jobs

        return claim_due_mirror_jobs(
            self,
            limit=limit,
            policy=policy,
            job_ids=job_ids,
        )

    def claim_due_jobs_with_limits(
        self,
        *,
        now: float,
        limit: int,
        policy: Any,
        job_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        claim_time = _finite_number(now, "now")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("claim limit must be a non-negative integer")
        normalized_job_ids = (
            None
            if job_ids is None
            else _bounded_exact_ids(job_ids, label="job_ids", maximum=1000)
        )
        policy_values = _validated_claim_policy(policy)
        if limit == 0 or normalized_job_ids == ():
            return []

        def _write(conn):
            automatic_creation = policy_values["automatic_creation"]
            breaker = _read_breaker_progress(conn)
            if _healthy_breaker_batch_completed(
                breaker,
                stop_after_attempts=policy_values["stop_after_attempts"],
                stop_error_rate=policy_values["stop_error_rate"],
            ):
                breaker = {"attempts": 0, "errors": 0, "pending": 0}
                _write_breaker_progress(conn, breaker, updated_at=claim_time)
            limited_allowed = not _breaker_is_halted(
                breaker,
                stop_after_attempts=policy_values["stop_after_attempts"],
                stop_error_rate=policy_values["stop_error_rate"],
            )
            automatic_allowed = automatic_creation and limited_allowed

            recent = _read_rate_attempts(conn, now=claim_time)
            capacity = min(
                limit,
                max(0, policy_values["creates_per_minute"] - len(recent)),
            )
            if capacity == 0:
                _write_rate_attempts(conn, recent, updated_at=claim_time)
                return []

            scan_limit = max(capacity * 4, capacity + 32)
            scope_clause = ""
            scope_params: list[Any] = []
            if normalized_job_ids is not None:
                placeholders = ",".join("?" for _ in normalized_job_ids)
                scope_clause = f" AND job.id IN ({placeholders})"
                scope_params.extend(normalized_job_ids)
            due = conn.execute(
                f"""SELECT job.* FROM session_mirror_jobs AS job
                   LEFT JOIN session_bridge_state AS authority
                     ON authority.key = ? || job.id
                   WHERE job.state IN (?, ?) AND job.next_attempt_at <= ?
                     {scope_clause}
                   ORDER BY
                     CASE WHEN authority.value_json LIKE ? THEN 0 ELSE 1 END,
                     job.next_attempt_at, job.created_at, job.id
                   LIMIT ?""",
                (
                    _MIRROR_AUTHORITY_STATE_PREFIX,
                    MirrorJobState.QUEUED.value,
                    MirrorJobState.RETRY.value,
                    claim_time,
                    *scope_params,
                    '{"authority":"manual",%',
                    scan_limit,
                ),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            limited_reserved = 0
            for job in due:
                if len(claimed) >= capacity:
                    break
                try:
                    authority = _read_claim_authority(conn, job)
                except KeyError:
                    _terminalize_unclaimable_job(
                        conn,
                        job,
                        now=claim_time,
                        code="authority_missing",
                        detail="mirror authority metadata is missing",
                    )
                    continue
                except ValueError:
                    _terminalize_unclaimable_job(
                        conn,
                        job,
                        now=claim_time,
                        code="authority_invalid",
                        detail="mirror authority metadata is invalid",
                    )
                    continue
                claim_authority = authority["authority"]
                rollout_limited = authority["rollout_limited"]
                is_limited = claim_authority == "automatic" or rollout_limited
                if claim_authority == "automatic":
                    if not automatic_allowed:
                        continue
                if is_limited:
                    if not limited_allowed or limited_reserved:
                        continue
                if claim_authority == "automatic" or authority["require_unmapped"]:
                    if _automatic_claim_denial(conn, job) is not None:
                        code = (
                            "automatic_authority_revoked"
                            if claim_authority == "automatic"
                            else "manual_authority_revoked"
                        )
                        detail = (
                            "automatic mirror authority is no longer valid"
                            if claim_authority == "automatic"
                            else "safe manual mirror authority is no longer valid"
                        )
                        _terminalize_unclaimable_job(
                            conn,
                            job,
                            now=claim_time,
                            code=code,
                            detail=detail,
                        )
                        continue
                cursor = conn.execute(
                    """UPDATE session_mirror_jobs
                       SET state = ?, attempts = attempts + 1, updated_at = ?
                       WHERE id = ? AND state = ? AND attempts = ?
                         AND idempotency_key = ?""",
                    (
                        MirrorJobState.RUNNING.value,
                        claim_time,
                        job["id"],
                        job["state"],
                        job["attempts"],
                        job["idempotency_key"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("stale mirror job claim")
                claimed_job = dict(
                    conn.execute(
                        "SELECT * FROM session_mirror_jobs WHERE id = ?",
                        (job["id"],),
                    ).fetchone()
                )
                claimed_job["claim_authority"] = claim_authority
                claimed_job["rollout_limited"] = rollout_limited
                if is_limited:
                    limited_reserved = 1
                    _create_breaker_reservation(
                        conn,
                        claimed_job,
                        updated_at=claim_time,
                    )
                claimed.append(claimed_job)

            if limited_reserved:
                breaker = {
                    "attempts": breaker["attempts"] + limited_reserved,
                    "errors": breaker["errors"],
                    "pending": breaker["pending"] + limited_reserved,
                }
                _write_breaker_progress(conn, breaker, updated_at=claim_time)

            _write_rate_attempts(
                conn,
                [*recent, *([claim_time] * len(claimed))],
                updated_at=claim_time,
            )
            return claimed

        return self.db._execute_write(_write)

    def get_mirror_breaker_progress(self) -> dict[str, int]:
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            progress = _read_breaker_progress(conn)
            return {
                "attempts": progress["attempts"],
                "errors": progress["errors"],
            }

    def accumulate_mirror_breaker_progress(
        self,
        *,
        attempts: int,
        errors: int,
        reset: bool = False,
    ) -> dict[str, int]:
        _nonnegative_integer(attempts, "breaker attempts")
        _nonnegative_integer(errors, "breaker errors")
        if errors > attempts:
            raise ValueError("breaker errors cannot exceed attempts")
        if type(reset) is not bool:
            raise ValueError("breaker reset must be a boolean")
        now = _finite_number(self._clock(), "store clock")

        def _write(conn):
            current = _read_breaker_progress(conn)
            if reset and current["pending"]:
                raise ValueError("cannot reset mirror breaker with pending attempts")
            base = {"attempts": 0, "errors": 0, "pending": 0} if reset else current
            updated = {
                "attempts": base["attempts"] + attempts,
                "errors": base["errors"] + errors,
                "pending": base["pending"],
            }
            if updated["errors"] > updated["attempts"]:
                raise ValueError("breaker errors cannot exceed attempts")
            _write_breaker_progress(conn, updated, updated_at=now)
            return {
                "attempts": updated["attempts"],
                "errors": updated["errors"],
            }

        return self.db._execute_write(_write)

    def complete_job(
        self,
        job_id: str,
        *,
        target_native_id: str,
        target_session_id: str,
        bridge_id: str,
    ) -> None:
        normalized_bridge_id = _nonempty_text(bridge_id, "bridge ID")
        now = float(self._clock())

        def _write(conn):
            job = conn.execute(
                "SELECT * FROM session_mirror_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None:
                raise KeyError(job_id)
            expected_target_id = canonical_session_id(
                Provider(job["target_provider"]), target_native_id
            )
            normalized_target_native_id = target_native_id.strip()
            if target_session_id != expected_target_id:
                raise ValueError(
                    "target session ID does not match the mirror job identity"
                )

            target = conn.execute(
                """SELECT s.source, e.provider, e.native_id,
                          e.origin_kind, e.origin_bridge_id
                   FROM sessions AS s
                   JOIN external_sessions AS e ON e.session_id = s.id
                   WHERE s.id = ?""",
                (target_session_id,),
            ).fetchone()
            if target is None or (
                target["source"] != job["target_provider"]
                or target["provider"] != job["target_provider"]
                or target["native_id"] != normalized_target_native_id
            ):
                raise ValueError(
                    "mirror completion requires a matching cataloged target identity"
                )
            if (
                target["origin_kind"]
                not in (
                    OriginKind.BRIDGE_PLACEHOLDER.value,
                    OriginKind.BRIDGE_CONTINUATION.value,
                )
                or target["origin_bridge_id"] != normalized_bridge_id
            ):
                raise ValueError(
                    "mirror completion requires authenticated exact bridge provenance"
                )

            if job["state"] == MirrorJobState.SUCCEEDED.value:
                exact_link = conn.execute(
                    """SELECT 1 FROM session_links
                       WHERE bridge_id = ? AND from_session_id = ?
                         AND to_session_id = ? AND relation = ?""",
                    (
                        normalized_bridge_id,
                        job["source_session_id"],
                        target_session_id,
                        Relation.MIRRORS.value,
                    ),
                ).fetchone()
                if (
                    job["target_native_id"] == normalized_target_native_id
                    and exact_link is not None
                ):
                    return
                raise ValueError("conflicting completion replay for succeeded job")
            if job["state"] != MirrorJobState.RUNNING.value:
                raise ValueError("mirror job must be running before completion")

            conn.execute(
                """UPDATE session_mirror_jobs
                   SET state = ?, target_native_id = ?, error_code = NULL,
                       error_detail = NULL, updated_at = ?
                   WHERE id = ?""",
                (
                    MirrorJobState.SUCCEEDED.value,
                    normalized_target_native_id,
                    now,
                    job_id,
                ),
            )
            self._create_link_row(
                conn,
                SessionLink(
                    id=f"link:{_stable_id('mirror-link', normalized_bridge_id, job['source_session_id'], target_session_id)}",
                    from_session_id=job["source_session_id"],
                    to_session_id=target_session_id,
                    relation=Relation.MIRRORS,
                    bridge_id=normalized_bridge_id,
                    source_cursor=None,
                    source_hash=None,
                    created_at=now,
                ),
            )
            _settle_breaker_reservation(
                conn,
                job,
                error=False,
                updated_at=now,
            )

        self.db._execute_write(_write)

    def retry_job(
        self,
        job_id: str,
        *,
        code: str,
        detail: str,
        next_attempt_at: float,
    ) -> None:
        self._set_job_failure(
            job_id,
            state=MirrorJobState.RETRY,
            code=code,
            detail=detail,
            next_attempt_at=float(next_attempt_at),
        )

    def fail_job_manually(
        self,
        job_id: str,
        *,
        code: str,
        detail: str,
    ) -> None:
        self._set_job_failure(
            job_id,
            state=MirrorJobState.MANUAL_FAILURE,
            code=code,
            detail=detail,
            next_attempt_at=None,
        )

    def _set_job_failure(
        self,
        job_id: str,
        *,
        state: MirrorJobState,
        code: str,
        detail: str,
        next_attempt_at: float | None,
    ) -> None:
        now = float(self._clock())

        def _write(conn):
            job = conn.execute(
                "SELECT * FROM session_mirror_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None:
                raise KeyError(job_id)

            current_state = MirrorJobState(job["state"])
            exact_replay = (
                current_state is state
                and job["error_code"] == code
                and job["error_detail"] == detail
                and (
                    state is MirrorJobState.MANUAL_FAILURE
                    or job["next_attempt_at"] == next_attempt_at
                )
            )
            if exact_replay:
                if state is MirrorJobState.RETRY:
                    conn.execute(
                        "DELETE FROM session_bridge_state WHERE key = ?",
                        (f"{_MIRROR_ATTEMPT_STATE_PREFIX}{job_id}",),
                    )
                return

            if current_state in (
                MirrorJobState.SUCCEEDED,
                MirrorJobState.MANUAL_FAILURE,
            ):
                raise ValueError("terminal mirror job cannot be overwritten")
            if state is MirrorJobState.RETRY:
                if current_state is not MirrorJobState.RUNNING:
                    raise ValueError("mirror job must be running before retry")
            elif current_state not in (
                MirrorJobState.RUNNING,
                MirrorJobState.RETRY,
            ):
                raise ValueError(
                    "mirror job must be running or retrying before manual failure"
                )

            if next_attempt_at is None:
                conn.execute(
                    """UPDATE session_mirror_jobs
                       SET state = ?, error_code = ?, error_detail = ?, updated_at = ?
                       WHERE id = ?""",
                    (state.value, code, detail, now, job_id),
                )
            else:
                conn.execute(
                    """UPDATE session_mirror_jobs
                       SET state = ?, error_code = ?, error_detail = ?,
                           next_attempt_at = ?, updated_at = ?
                       WHERE id = ?""",
                    (state.value, code, detail, next_attempt_at, now, job_id),
                )
            conn.execute(
                "DELETE FROM session_bridge_state WHERE key = ?",
                (f"{_MIRROR_ATTEMPT_STATE_PREFIX}{job_id}",),
            )
            _settle_breaker_reservation(
                conn,
                job,
                error=True,
                updated_at=now,
            )

        self.db._execute_write(_write)

    def create_link(self, link: SessionLink) -> dict[str, Any]:
        return self.db._execute_write(lambda conn: self._create_link_row(conn, link))

    def transition_link_to_continues(
        self,
        bridge_id: str,
        *,
        pack_id: str,
        target_cursor: str,
        target_hash: str,
    ) -> dict[str, Any]:
        normalized_bridge_id = _nonempty_text(bridge_id, "bridge ID")
        normalized_pack_id = _nonempty_text(pack_id, "context pack ID")
        normalized_target_cursor = _nonempty_text(target_cursor, "target cursor")
        normalized_target_hash = _nonempty_text(target_hash, "target hash")
        snapshot_key = _continuation_snapshot_state_key(normalized_bridge_id)
        now = float(self._clock())

        def _write(conn):
            pack = conn.execute(
                """SELECT * FROM session_context_packs
                   WHERE id = ? AND bridge_id = ?""",
                (normalized_pack_id, normalized_bridge_id),
            ).fetchone()
            if pack is None:
                raise KeyError(normalized_pack_id)
            if pack["target_session_id"] is None:
                raise ValueError("context pack target identity is missing")
            expected_snapshot = {
                "version": 1,
                "pack_id": normalized_pack_id,
                "source_session_id": pack["source_session_id"],
                "source_cursor": pack["source_cursor"],
                "source_hash": pack["source_hash"],
                "target_session_id": pack["target_session_id"],
                "target_cursor": normalized_target_cursor,
                "target_hash": normalized_target_hash,
            }
            snapshot_row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?",
                (snapshot_key,),
            ).fetchone()
            persisted_snapshot = (
                _decode_continuation_snapshot(snapshot_row["value_json"])
                if snapshot_row is not None
                else None
            )
            if persisted_snapshot is not None and (
                persisted_snapshot["target_cursor"] != normalized_target_cursor
                or persisted_snapshot["target_hash"] != normalized_target_hash
            ):
                raise ValueError("conflicting continuation target baseline")
            if (
                persisted_snapshot is not None
                and persisted_snapshot != expected_snapshot
            ):
                raise ValueError("conflicting continuation snapshot identity")

            links = conn.execute(
                """SELECT * FROM session_links
                   WHERE bridge_id = ? AND from_session_id = ?
                     AND to_session_id = ? AND relation IN (?, ?)
                   ORDER BY relation, id""",
                (
                    normalized_bridge_id,
                    pack["source_session_id"],
                    pack["target_session_id"],
                    Relation.MIRRORS.value,
                    Relation.CONTINUES.value,
                ),
            ).fetchall()
            mirror = next(
                (row for row in links if row["relation"] == Relation.MIRRORS.value),
                None,
            )
            continued = next(
                (row for row in links if row["relation"] == Relation.CONTINUES.value),
                None,
            )

            if continued is not None:
                if mirror is not None:
                    raise ValueError("conflicting continues link already exists")
                if (
                    continued["source_cursor"] == pack["source_cursor"]
                    and continued["source_hash"] == pack["source_hash"]
                ):
                    if (
                        pack["immutable_at"] is None
                        or continued["hydrated_at"] is None
                        or persisted_snapshot is None
                    ):
                        raise ValueError("incomplete continues link transition")
                    return dict(continued)
                raise ValueError("conflicting continues link source snapshot")
            if mirror is None:
                raise ValueError("context pack identity has no matching mirror link")
            if (
                mirror["source_cursor"] is not None
                or mirror["source_hash"] is not None
                or mirror["hydrated_at"] is not None
            ):
                raise ValueError("mirror link has conflicting source snapshot identity")
            if persisted_snapshot is not None:
                raise ValueError("continuation snapshot exists before link transition")

            target = conn.execute(
                """SELECT last_native_cursor, last_native_hash
                   FROM external_sessions WHERE session_id = ?""",
                (pack["target_session_id"],),
            ).fetchone()
            if target is None or (
                target["last_native_cursor"] != normalized_target_cursor
                or target["last_native_hash"] != normalized_target_hash
            ):
                raise ValueError(
                    "target baseline does not match cataloged target snapshot"
                )

            conn.execute(
                """UPDATE session_context_packs
                   SET immutable_at = COALESCE(immutable_at, ?)
                   WHERE id = ? AND bridge_id = ?""",
                (now, normalized_pack_id, normalized_bridge_id),
            )
            cursor = conn.execute(
                """UPDATE session_links
                   SET relation = ?, source_cursor = ?, source_hash = ?,
                       hydrated_at = COALESCE(hydrated_at, ?)
                   WHERE id = ? AND relation = ?""",
                (
                    Relation.CONTINUES.value,
                    pack["source_cursor"],
                    pack["source_hash"],
                    now,
                    mirror["id"],
                    Relation.MIRRORS.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("mirror link changed during transition")
            snapshot_json = json.dumps(
                expected_snapshot,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            conn.execute(
                """INSERT INTO session_bridge_state (key, value_json, updated_at)
                   VALUES (?, ?, ?)""",
                (snapshot_key, snapshot_json, now),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM session_links WHERE id = ?", (mirror["id"],)
                ).fetchone()
            )

        return self.db._execute_write(_write)

    @staticmethod
    def _create_link_row(conn, link: SessionLink) -> dict[str, Any]:
        conn.execute(
            """INSERT OR IGNORE INTO session_links (
               id, from_session_id, to_session_id, relation, bridge_id,
               source_cursor, source_hash, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                link.id,
                link.from_session_id,
                link.to_session_id,
                link.relation.value,
                link.bridge_id,
                link.source_cursor,
                link.source_hash,
                link.created_at,
            ),
        )
        row = conn.execute(
            """SELECT * FROM session_links
               WHERE bridge_id = ? AND from_session_id = ?
                 AND to_session_id = ? AND relation = ?""",
            (
                link.bridge_id,
                link.from_session_id,
                link.to_session_id,
                link.relation.value,
            ),
        ).fetchone()
        if row is None:
            raise ValueError(f"link ID collision for {link.id!r}")
        return dict(row)

    def mark_hydrated(
        self,
        bridge_id: str,
        *,
        source_cursor: str,
        source_hash: str,
        pack_id: str,
    ) -> None:
        now = float(self._clock())

        def _write(conn):
            pack = conn.execute(
                """SELECT id, source_session_id, target_session_id
                   FROM session_context_packs
                   WHERE id = ? AND bridge_id = ? AND source_cursor = ?
                     AND source_hash = ?""",
                (pack_id, bridge_id, source_cursor, source_hash),
            ).fetchone()
            if pack is None:
                raise KeyError(pack_id)
            link = conn.execute(
                """SELECT id FROM session_links
                   WHERE bridge_id = ? AND source_cursor = ? AND source_hash = ?
                     AND from_session_id = ? AND to_session_id = ?
                   LIMIT 1""",
                (
                    bridge_id,
                    source_cursor,
                    source_hash,
                    pack["source_session_id"],
                    pack["target_session_id"],
                ),
            ).fetchone()
            if link is None:
                raise ValueError("context pack has no matching link to hydrate")
            conn.execute(
                """UPDATE session_context_packs
                   SET immutable_at = COALESCE(immutable_at, ?)
                   WHERE id = ?""",
                (now, pack_id),
            )
            conn.execute(
                """UPDATE session_links
                   SET hydrated_at = COALESCE(hydrated_at, ?)
                   WHERE bridge_id = ? AND source_cursor = ? AND source_hash = ?
                     AND from_session_id = ? AND to_session_id = ?""",
                (
                    now,
                    bridge_id,
                    source_cursor,
                    source_hash,
                    pack["source_session_id"],
                    pack["target_session_id"],
                ),
            )

        self.db._execute_write(_write)

    def mark_diverged(self, bridge_id: str, *, at: float) -> None:
        def _write(conn):
            conn.execute(
                """UPDATE session_links
                   SET diverged_at = COALESCE(diverged_at, ?)
                   WHERE bridge_id = ?""",
                (float(at), bridge_id),
            )

        self.db._execute_write(_write)

    def put_context_pack(self, pack: ContextPack) -> dict[str, Any]:
        def _write(conn):
            row = conn.execute(
                """SELECT * FROM session_context_packs
                   WHERE bridge_id = ? AND source_cursor = ? AND source_hash = ?
                     AND budget_chars = ?""",
                (
                    pack.bridge_id,
                    pack.source_cursor,
                    pack.source_hash,
                    pack.budget_chars,
                ),
            ).fetchone()
            if row is not None:
                if row["source_session_id"] != pack.source_session_id:
                    raise ValueError("context pack source identity mismatch")
                if (
                    row["target_session_id"] is not None
                    and pack.target_session_id is not None
                    and row["target_session_id"] != pack.target_session_id
                ):
                    raise ValueError("context pack target identity mismatch")
                if row["immutable_at"] is None:
                    target_session_id = (
                        pack.target_session_id
                        if pack.target_session_id is not None
                        else row["target_session_id"]
                    )
                    conn.execute(
                        """UPDATE session_context_packs
                           SET target_session_id = ?, payload = ?, created_at = ?
                           WHERE id = ?""",
                        (
                            target_session_id,
                            pack.payload,
                            pack.created_at,
                            row["id"],
                        ),
                    )
                    row = conn.execute(
                        "SELECT * FROM session_context_packs WHERE id = ?",
                        (row["id"],),
                    ).fetchone()
                return dict(row)
            conn.execute(
                """INSERT INTO session_context_packs (
                   id, bridge_id, source_session_id, target_session_id,
                   source_cursor, source_hash, budget_chars, payload, created_at,
                   immutable_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    pack.id,
                    pack.bridge_id,
                    pack.source_session_id,
                    pack.target_session_id,
                    pack.source_cursor,
                    pack.source_hash,
                    pack.budget_chars,
                    pack.payload,
                    pack.created_at,
                    pack.immutable_at,
                ),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM session_context_packs WHERE id = ?", (pack.id,)
                ).fetchone()
            )

        return self.db._execute_write(_write)

    def get_context_pack(
        self, bridge_id: str, *, budget_chars: int
    ) -> dict[str, Any] | None:
        # _execute_read: this is on the resume path (continue_session), so a
        # transient WAL lock must be retried, not surfaced as "database is
        # locked".
        def _read(conn):
            assert conn is not None
            return conn.execute(
                """SELECT * FROM session_context_packs
                   WHERE bridge_id = ? AND budget_chars = ?
                   ORDER BY created_at DESC, id DESC LIMIT 1""",
                (bridge_id, budget_chars),
            ).fetchone()

        row = self.db._execute_read(_read)
        return dict(row) if row else None

    def set_state(self, key: str, value: Mapping[str, Any]) -> None:
        if not isinstance(value, Mapping):
            raise TypeError("bridge state must be a mapping")
        value_json = json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        snapshot = json.loads(value_json)
        if not isinstance(snapshot, dict):
            raise TypeError("bridge state must encode as a JSON object")
        now = float(self._clock())

        def _write(conn):
            conn.execute(
                """INSERT INTO session_bridge_state (key, value_json, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value_json = excluded.value_json,
                       updated_at = excluded.updated_at""",
                (key, value_json, now),
            )

        self.db._execute_write(_write)

    def get_state(self, key: str) -> dict[str, Any] | None:
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        value = json.loads(row["value_json"])
        if not isinstance(value, dict):
            raise ValueError(f"bridge state {key!r} is not a JSON object")
        return value

    def get_continuation_snapshot(self, bridge_id: str) -> dict[str, Any] | None:
        normalized_bridge_id = _nonempty_text(bridge_id, "bridge ID")
        state_key = _continuation_snapshot_state_key(normalized_bridge_id)

        # _execute_read: resume path — retry transient WAL locks. Identity
        # validation reads the same conn, so it stays inside the retried fn.
        def _read(conn):
            assert conn is not None
            state_row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?",
                (state_key,),
            ).fetchone()
            if state_row is None:
                return None
            snapshot = _decode_continuation_snapshot(state_row["value_json"])
            _validate_continuation_snapshot_identity(
                conn, normalized_bridge_id, snapshot
            )
            return snapshot

        return self.db._execute_read(_read)

    def list_continuation_snapshots(
        self,
        *,
        limit: int = 1000,
        after_bridge_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
        ):
            raise ValueError(
                "continuation snapshot list limit must be between 1 and 1000"
            )
        if after_bridge_id is None:
            lower_key = _CONTINUATION_SNAPSHOT_STATE_PREFIX
            comparison = ">="
        else:
            normalized_after = _nonempty_text(after_bridge_id, "after bridge ID")
            if normalized_after != after_bridge_id:
                raise ValueError("after bridge ID must be canonical")
            lower_key = _continuation_snapshot_state_key(normalized_after)
            comparison = ">"
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            rows = conn.execute(
                f"""SELECT key, value_json FROM session_bridge_state
                   WHERE key {comparison} ? AND key < ?
                   ORDER BY key LIMIT ?""",
                (
                    lower_key,
                    f"{_CONTINUATION_SNAPSHOT_STATE_PREFIX}\uffff",
                    limit,
                ),
            ).fetchall()
            snapshots: list[dict[str, Any]] = []
            for row in rows:
                raw_bridge_id = row["key"][len(_CONTINUATION_SNAPSHOT_STATE_PREFIX) :]
                bridge_id = _nonempty_text(
                    raw_bridge_id,
                    "continuation snapshot bridge ID",
                )
                if raw_bridge_id != bridge_id:
                    raise ValueError(
                        "continuation snapshot state key has noncanonical bridge ID"
                    )
                snapshot = _decode_continuation_snapshot(row["value_json"])
                _validate_continuation_snapshot_identity(conn, bridge_id, snapshot)
                snapshots.append({"bridge_id": bridge_id, **snapshot})
        return snapshots


def _finite_number(value: object, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def _exact_nonempty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be an exact nonempty string")
    return value


def _sha256_text(value: object, label: str) -> str:
    normalized = _exact_nonempty_text(value, label)
    if re.fullmatch(r"[0-9a-f]{64}", normalized) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")
    return normalized


def _lowercase_sha256(value: object, label: str) -> str:
    normalized = _exact_nonempty_text(value, label)
    if re.fullmatch(r"[0-9a-f]{64}", normalized) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")
    return normalized


def _hydration_lease_digest(lease_token: object) -> str:
    token = _exact_nonempty_text(lease_token, "hydration lease token")
    return hmac.new(
        _SIDEBAR_HYDRATION_LEASE_KEY,
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _hydration_completion_digest(lease_token: object) -> str:
    token = _exact_nonempty_text(lease_token, "hydration lease token")
    return hmac.new(
        _SIDEBAR_HYDRATION_COMPLETION_KEY,
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _find_sidebar_hydration_by_lease(
    conn: Any,
    lease_digest: str,
) -> Mapping[str, Any] | None:
    rows = conn.execute(
        """SELECT * FROM session_sidebar_hydration_jobs
           WHERE lease_digest = ? LIMIT 2""",
        (lease_digest,),
    ).fetchall()
    matches = [
        row
        for row in rows
        if isinstance(row["lease_digest"], str)
        and hmac.compare_digest(row["lease_digest"], lease_digest)
    ]
    if len(matches) > 1:
        raise ValueError("ambiguous hydration lease token")
    return matches[0] if matches else None


def _find_sidebar_hydration_for_completion(
    conn: Any,
    *,
    lease_digest: str,
    completion_digest: str,
) -> tuple[Mapping[str, Any] | None, bool]:
    lease = _find_sidebar_hydration_by_lease(conn, lease_digest)
    completion_rows = conn.execute(
        """SELECT * FROM session_sidebar_hydration_jobs
           WHERE completion_digest = ? LIMIT 2""",
        (completion_digest,),
    ).fetchall()
    completion = [
        row
        for row in completion_rows
        if isinstance(row["completion_digest"], str)
        and hmac.compare_digest(row["completion_digest"], completion_digest)
    ]
    matches = ([] if lease is None else [(lease, False)]) + [
        (row, True) for row in completion
    ]
    if len(matches) > 1:
        raise ValueError("ambiguous hydration lease token")
    return matches[0] if matches else (None, False)


def _recover_expired_sidebar_hydration(
    conn: Any,
    job: Mapping[str, Any],
    *,
    now: float,
) -> None:
    cursor = conn.execute(
        """UPDATE session_sidebar_hydration_jobs
           SET state = ?, next_attempt_at = ?, lease_digest = NULL,
               lease_expires_at = NULL, updated_at = ?
           WHERE id = ? AND state = ?""",
        (
            SidebarHydrationState.RETRY.value,
            now,
            now,
            job["id"],
            SidebarHydrationState.LEASED.value,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("stale expired hydration lease")


def _sidebar_lease_digest(lease_token: object) -> str:
    token = _exact_nonempty_text(lease_token, "sidebar lease token")
    return hashlib.sha256(token.encode()).hexdigest()


def _sidebar_exclusion_identity_digest(
    source_session_id: str,
    provider: Provider,
    reason_code: str,
) -> str:
    identity = "\0".join((source_session_id, provider.value, reason_code))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _ensure_sidebar_lineage_row(
    conn: Any,
    *,
    source_session_id: str,
    bridge_id: str,
    codex_thread_id: str,
    created_at: float,
) -> dict[str, Any]:
    target = conn.execute(
        """SELECT e.session_id, e.origin_kind, e.origin_bridge_id
             FROM external_sessions AS e
            WHERE e.provider = ? AND e.native_id = ?""",
        (Provider.CODEX.value, codex_thread_id),
    ).fetchone()
    if target is None:
        raise SidebarNativeTaskNotIndexed()
    if (
        target["origin_kind"] != OriginKind.BRIDGE_PLACEHOLDER.value
        or target["origin_bridge_id"] != bridge_id
    ):
        raise ValueError("source_identity_mismatch")
    conflicting = conn.execute(
        """SELECT 1 FROM session_links
            WHERE bridge_id = ? AND (
                from_session_id != ? OR to_session_id != ? OR relation != ?
            ) LIMIT 1""",
        (
            bridge_id,
            source_session_id,
            target["session_id"],
            Relation.MIRRORS.value,
        ),
    ).fetchone()
    if conflicting is not None:
        raise ValueError("source_identity_mismatch")
    link_digest = hashlib.sha256(
        f"{bridge_id}\0{source_session_id}\0{target['session_id']}".encode()
    ).hexdigest()
    link_id = f"sidebar-link:{link_digest}"
    conn.execute(
        """INSERT OR IGNORE INTO session_links (
               id, from_session_id, to_session_id, relation, bridge_id,
               source_cursor, source_hash, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            link_id,
            source_session_id,
            target["session_id"],
            Relation.MIRRORS.value,
            bridge_id,
            None,
            None,
            created_at,
        ),
    )
    row = conn.execute(
        """SELECT * FROM session_links
           WHERE bridge_id = ? AND from_session_id = ?
             AND to_session_id = ? AND relation = ?""",
        (
            bridge_id,
            source_session_id,
            target["session_id"],
            Relation.MIRRORS.value,
        ),
    ).fetchone()
    if row is None or row["id"] != link_id:
        raise ValueError(f"link ID collision for {link_id!r}")
    return dict(row)


def _ensure_claude_visibility_lineage_row_if_known(
    conn: Any,
    *,
    target_session_id: str,
    target_native_id: str,
    bridge_id: str,
    created_at: float,
    source_identity_issue: Callable[..., str | None] | None = None,
) -> dict[str, Any] | None:
    jobs = conn.execute(
        """SELECT id FROM session_claude_visibility_jobs
           WHERE bridge_id = ? AND reserved_claude_uuid = ?
             AND state = 'claude_visible'
           ORDER BY id LIMIT 2""",
        (bridge_id, target_native_id),
    ).fetchall()
    if not jobs:
        return None
    if len(jobs) != 1:
        raise ValueError(_CLAUDE_LINEAGE_TARGET_DUPLICATE)
    finalized = _finalize_claude_visibility_lineage_if_indexed(
        conn,
        job_id=str(jobs[0]["id"]),
        created_at=created_at,
        source_identity_issue=source_identity_issue,
    )
    if finalized["state"] == "blocked":
        raise ValueError(str(finalized["code"] or _CLAUDE_LINEAGE_CONFLICT))
    if finalized["state"] == "target_missing":
        return None
    return finalized.get("link")


def _claude_visibility_lineage_link_id(
    bridge_id: str,
    source_session_id: str,
    target_session_id: str,
) -> str:
    digest = hashlib.sha256(
        f"{bridge_id}\0{source_session_id}\0{target_session_id}".encode()
    ).hexdigest()
    return f"claude-visibility-link:{digest}"


def _claude_visibility_characterization_registered(conn: Any, job_id: str) -> bool:
    return (
        conn.execute(
            """SELECT 1
               FROM session_claude_visibility_characterization_events
               WHERE job_id = ? AND event_kind = 'registered'""",
            (job_id,),
        ).fetchone()
        is not None
    )


def _claude_visibility_characterization_terminal(conn: Any, job_id: str) -> bool:
    return (
        conn.execute(
            """SELECT 1
               FROM session_claude_visibility_characterization_events
               WHERE job_id = ? AND event_kind IN (
                   'cleanup_completed', 'launch_aborted'
               )""",
            (job_id,),
        ).fetchone()
        is not None
    )


def _inspect_claude_visibility_lineage(
    conn: Any,
    job: Mapping[str, Any],
    *,
    source_identity_issue: Callable[..., str | None] | None = None,
) -> dict[str, Any]:
    if _claude_visibility_characterization_registered(conn, str(job["id"])):
        return {"state": "already_linked", "code": None, "link": None}
    bridge_id = str(job["bridge_id"])
    source_session_id = str(job["source_session_id"])
    reserved_uuid = str(job["reserved_claude_uuid"])
    target_session_id = canonical_session_id(Provider.CLAUDE, reserved_uuid)
    expected_link_id = _claude_visibility_lineage_link_id(
        bridge_id,
        source_session_id,
        target_session_id,
    )

    source_validator = source_identity_issue or _native_source_identity_issue
    source_issue = source_validator(
        conn,
        source_session_id=source_session_id,
        source_provider=job["source_provider"],
    )
    if source_issue == "missing":
        return {
            "state": "blocked",
            "code": _CLAUDE_LINEAGE_MISSING_SOURCE,
            "link": None,
        }
    if source_issue == "identity":
        return {
            "state": "blocked",
            "code": _CLAUDE_LINEAGE_SOURCE_IDENTITY_MISMATCH,
            "link": None,
        }
    if source_issue == "provenance":
        return {
            "state": "blocked",
            "code": _CLAUDE_LINEAGE_SOURCE_PROVENANCE_MISMATCH,
            "link": None,
        }

    exact_target = conn.execute(
        "SELECT * FROM external_sessions WHERE session_id = ?",
        (target_session_id,),
    ).fetchone()
    bridge_targets = conn.execute(
        """SELECT * FROM external_sessions
           WHERE origin_bridge_id = ?
           ORDER BY session_id LIMIT 3""",
        (bridge_id,),
    ).fetchall()
    if exact_target is None:
        return {
            "state": "blocked",
            "code": (
                _CLAUDE_LINEAGE_TARGET_IDENTITY_MISMATCH
                if bridge_targets
                else _CLAUDE_LINEAGE_TARGET_MISSING
            ),
            "link": None,
        }
    if (
        exact_target["provider"] != Provider.CLAUDE.value
        or exact_target["native_id"] != reserved_uuid
    ):
        return {
            "state": "blocked",
            "code": _CLAUDE_LINEAGE_TARGET_IDENTITY_MISMATCH,
            "link": None,
        }
    if (
        exact_target["origin_kind"]
        not in {
            OriginKind.BRIDGE_PLACEHOLDER.value,
            OriginKind.BRIDGE_CONTINUATION.value,
        }
        or exact_target["origin_bridge_id"] != bridge_id
    ):
        return {
            "state": "blocked",
            "code": _CLAUDE_LINEAGE_TARGET_PROVENANCE_MISMATCH,
            "link": None,
        }
    if len(bridge_targets) != 1 or bridge_targets[0]["session_id"] != target_session_id:
        return {
            "state": "blocked",
            "code": _CLAUDE_LINEAGE_TARGET_DUPLICATE,
            "link": None,
        }
    completion = job["completion_digest"]
    if (
        not isinstance(completion, str)
        or len(completion) != 64
        or re.fullmatch(r"[0-9a-f]{64}", completion) is None
        or job["visible_at"] is None
    ):
        return {
            "state": "blocked",
            "code": _CLAUDE_LINEAGE_INVALID_COMPLETION,
            "link": None,
        }

    links = conn.execute(
        "SELECT * FROM session_links WHERE bridge_id = ? ORDER BY id LIMIT 3",
        (bridge_id,),
    ).fetchall()
    if not links:
        return {
            "state": "repairable",
            "code": None,
            "link": None,
            "link_id": expected_link_id,
            "source_session_id": source_session_id,
            "target_session_id": target_session_id,
            "bridge_id": bridge_id,
        }
    if len(links) != 1:
        return {
            "state": "blocked",
            "code": _CLAUDE_LINEAGE_CONFLICT,
            "link": None,
        }
    link = links[0]
    if (
        link["id"] != expected_link_id
        or link["from_session_id"] != source_session_id
        or link["to_session_id"] != target_session_id
        or link["relation"] not in {Relation.MIRRORS.value, Relation.CONTINUES.value}
    ):
        return {
            "state": "blocked",
            "code": _CLAUDE_LINEAGE_CONFLICT,
            "link": None,
        }
    return {"state": "already_linked", "code": None, "link": dict(link)}


def _finalize_claude_visibility_lineage_if_production(
    conn: Any,
    *,
    job_id: str,
    created_at: float,
    source_identity_issue: Callable[..., str | None] | None = None,
) -> dict[str, Any]:
    if _claude_visibility_characterization_registered(conn, job_id):
        return {"state": "already_linked", "code": None, "link": None}
    return _finalize_claude_visibility_lineage_if_indexed(
        conn,
        job_id=job_id,
        created_at=created_at,
        source_identity_issue=source_identity_issue,
    )


def _finalize_claude_visibility_lineage_if_indexed(
    conn: Any,
    *,
    job_id: str,
    created_at: float,
    source_identity_issue: Callable[..., str | None] | None = None,
) -> dict[str, Any]:
    job = conn.execute(
        """SELECT * FROM session_claude_visibility_jobs
           WHERE id = ? AND state = 'claude_visible'""",
        (job_id,),
    ).fetchone()
    if job is None:
        raise ValueError("exact visible Claude visibility job required")
    inspected = _inspect_claude_visibility_lineage(
        conn,
        job,
        source_identity_issue=source_identity_issue,
    )
    if inspected["state"] != "repairable":
        if inspected["code"] == _CLAUDE_LINEAGE_TARGET_MISSING:
            return {"state": "target_missing", "code": inspected["code"], "link": None}
        return inspected

    conn.execute(
        """INSERT OR IGNORE INTO session_links (
               id, from_session_id, to_session_id, relation, bridge_id,
               source_cursor, source_hash, created_at
           ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)""",
        (
            inspected["link_id"],
            inspected["source_session_id"],
            inspected["target_session_id"],
            Relation.MIRRORS.value,
            inspected["bridge_id"],
            created_at,
        ),
    )
    verified = _inspect_claude_visibility_lineage(
        conn,
        job,
        source_identity_issue=source_identity_issue,
    )
    if verified["state"] != "already_linked":
        return {
            "state": "blocked",
            "code": verified["code"] or _CLAUDE_LINEAGE_CONFLICT,
            "link": None,
        }
    return {"state": "linked", "code": None, "link": verified["link"]}


def _claude_lineage_job_key(job: Mapping[str, Any]) -> tuple[float, str]:
    return (
        _finite_number(job["visible_at"], "Claude lineage visible_at"),
        _exact_nonempty_text(job["id"], "Claude lineage job ID"),
    )


def _last_unlinked_claude_visibility_job_key(
    conn: Any,
    *,
    source_identity_issue: Callable[..., str | None] | None = None,
) -> tuple[float, str] | None:
    rows = conn.execute(
        """SELECT job.*
           FROM session_claude_visibility_jobs AS job
           WHERE job.state = 'claude_visible'
           ORDER BY job.visible_at DESC, job.id DESC"""
    )
    for row in rows:
        if (
            _inspect_claude_visibility_lineage(
                conn,
                row,
                source_identity_issue=source_identity_issue,
            )["state"]
            != "already_linked"
        ):
            return _claude_lineage_job_key(row)
    return None


def _unlinked_claude_visibility_jobs(
    conn: Any,
    *,
    limit: int,
    after_key: tuple[float, str] | None = None,
    high_water_key: tuple[float, str] | None = None,
    source_identity_issue: Callable[..., str | None] | None = None,
) -> list[Any]:
    selected: list[Any] = []
    clauses = ["job.state = 'claude_visible'"]
    params: list[Any] = []
    if after_key is not None:
        clauses.append(
            """(job.visible_at > ? OR
                 (job.visible_at = ? AND job.id > ?))"""
        )
        params.extend((after_key[0], after_key[0], after_key[1]))
    if high_water_key is not None:
        clauses.append(
            """(job.visible_at < ? OR
                 (job.visible_at = ? AND job.id <= ?))"""
        )
        params.extend((high_water_key[0], high_water_key[0], high_water_key[1]))
    rows = conn.execute(
        f"""SELECT job.*
           FROM session_claude_visibility_jobs AS job
           WHERE {" AND ".join(clauses)}
           ORDER BY job.visible_at, job.id""",
        params,
    )
    for row in rows:
        if (
            _inspect_claude_visibility_lineage(
                conn,
                row,
                source_identity_issue=source_identity_issue,
            )["state"]
            == "already_linked"
        ):
            continue
        selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def _validated_claude_lineage_cursor(
    cursor: Mapping[str, Any] | None,
    *,
    marker_secret: bytes,
    apply: bool,
) -> tuple[tuple[float, str], tuple[float, str]] | None:
    if cursor is None:
        return None
    if not isinstance(cursor, Mapping) or set(cursor) != _CLAUDE_LINEAGE_CURSOR_FIELDS:
        raise ValueError(
            "Claude lineage reconciliation cursor must have the exact fields"
        )
    if (
        isinstance(cursor["version"], bool)
        or cursor["version"] != _CLAUDE_LINEAGE_CURSOR_VERSION
        or isinstance(cursor["schema_version"], bool)
        or cursor["schema_version"] != SCHEMA_VERSION
        or cursor["operation"] != _CLAUDE_LINEAGE_CURSOR_OPERATION
        or cursor["mode"] != ("apply" if apply else "dry_run")
    ):
        raise ValueError("Claude lineage reconciliation cursor context is invalid")
    after_key = (
        _finite_number(
            cursor["after_visible_at"],
            "Claude lineage reconciliation cursor after_visible_at",
        ),
        _exact_nonempty_text(
            cursor["after_job_id"],
            "Claude lineage reconciliation cursor after_job_id",
        ),
    )
    high_water_key = (
        _finite_number(
            cursor["high_water_visible_at"],
            "Claude lineage reconciliation cursor high_water_visible_at",
        ),
        _exact_nonempty_text(
            cursor["high_water_job_id"],
            "Claude lineage reconciliation cursor high_water_job_id",
        ),
    )
    if after_key > high_water_key:
        raise ValueError(
            "Claude lineage reconciliation cursor exceeds its high-water mark"
        )
    signature = cursor["signature"]
    expected_signature = _claude_lineage_cursor_signature(
        {field: cursor[field] for field in _CLAUDE_LINEAGE_CURSOR_UNSIGNED_FIELDS},
        marker_secret,
    )
    if (
        not isinstance(signature, str)
        or re.fullmatch(r"[0-9a-f]{64}", signature) is None
        or not hmac.compare_digest(signature, expected_signature)
    ):
        raise ValueError("Claude lineage reconciliation cursor signature is invalid")
    return after_key, high_water_key


def _validated_claude_lineage_cursor_secret(marker_secret: bytes) -> bytes:
    if not isinstance(marker_secret, bytes):
        raise TypeError("Claude lineage reconciliation marker secret must be bytes")
    if not marker_secret:
        raise ValueError("Claude lineage reconciliation marker secret must be nonempty")
    return marker_secret


def _claude_lineage_cursor_signature(
    payload: Mapping[str, Any],
    marker_secret: bytes,
) -> str:
    encoded = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hmac.new(
        marker_secret,
        _CLAUDE_LINEAGE_CURSOR_DOMAIN + encoded,
        hashlib.sha256,
    ).hexdigest()


def _validate_claude_lineage_cursor_anchors(
    conn: Any,
    *,
    after_key: tuple[float, str],
    high_water_key: tuple[float, str],
) -> None:
    for visible_at, job_id in (after_key, high_water_key):
        row = conn.execute(
            """SELECT visible_at FROM session_claude_visibility_jobs
               WHERE id = ? AND state = 'claude_visible'""",
            (job_id,),
        ).fetchone()
        if row is None or row["visible_at"] != visible_at:
            raise ValueError(
                "Claude lineage reconciliation cursor is not durably anchored"
            )


def _public_claude_lineage_cursor(
    *,
    after_key: tuple[float, str],
    high_water_key: tuple[float, str],
    marker_secret: bytes,
    apply: bool,
) -> dict[str, Any]:
    payload = {
        "version": _CLAUDE_LINEAGE_CURSOR_VERSION,
        "schema_version": SCHEMA_VERSION,
        "operation": _CLAUDE_LINEAGE_CURSOR_OPERATION,
        "mode": "apply" if apply else "dry_run",
        "after_visible_at": after_key[0],
        "after_job_id": after_key[1],
        "high_water_visible_at": high_water_key[0],
        "high_water_job_id": high_water_key[1],
    }
    return {
        **payload,
        "signature": _claude_lineage_cursor_signature(payload, marker_secret),
    }


def _count_unlinked_claude_visibility_jobs(
    conn: Any,
    *,
    source_identity_issue: Callable[..., str | None] | None = None,
) -> int:
    count = 0
    rows = conn.execute(
        """SELECT job.*
           FROM session_claude_visibility_jobs AS job
           WHERE job.state = 'claude_visible'
           ORDER BY job.visible_at, job.id"""
    )
    for row in rows:
        if (
            _inspect_claude_visibility_lineage(
                conn,
                row,
                source_identity_issue=source_identity_issue,
            )["state"]
            != "already_linked"
        ):
            count += 1
    return count


def _claude_visibility_lineage_status(
    conn: Any,
    *,
    source_identity_issue: Callable[..., str | None] | None = None,
) -> dict[str, Any]:
    rows = _unlinked_claude_visibility_jobs(
        conn,
        limit=_CLAUDE_LINEAGE_RECONCILE_LIMIT_MAX,
        source_identity_issue=source_identity_issue,
    )
    total = _count_unlinked_claude_visibility_jobs(
        conn,
        source_identity_issue=source_identity_issue,
    )
    blocker_codes: dict[str, int] = {}
    repairable = 0
    for row in rows:
        inspected = _inspect_claude_visibility_lineage(
            conn,
            row,
            source_identity_issue=source_identity_issue,
        )
        if inspected["state"] == "repairable":
            repairable += 1
            continue
        code = str(inspected["code"] or _CLAUDE_LINEAGE_CONFLICT)
        blocker_codes[code] = blocker_codes.get(code, 0) + 1
    if total > len(rows):
        blocker_codes["claude_lineage_status_truncated"] = total - len(rows)
    return {
        "unlinked_visible": total,
        "repairable": repairable,
        "blocked": total - repairable,
        "blocker_codes": dict(sorted(blocker_codes.items())),
    }


def _find_sidebar_job_by_digest(
    conn: Any,
    token_digest: str,
    *,
    allow_completion: bool,
) -> tuple[Mapping[str, Any] | None, bool]:
    matches: list[tuple[Mapping[str, Any], bool]] = []
    lease_rows = conn.execute(
        """SELECT * FROM session_sidebar_jobs
           WHERE lease_digest = ? LIMIT 2""",
        (token_digest,),
    ).fetchall()
    for row in lease_rows:
        lease_digest = row["lease_digest"]
        if isinstance(lease_digest, str) and hmac.compare_digest(
            lease_digest,
            token_digest,
        ):
            matches.append((row, False))
    if allow_completion:
        completion_rows = conn.execute(
            """SELECT * FROM session_sidebar_jobs
               WHERE completion_digest = ? LIMIT 2""",
            (token_digest,),
        ).fetchall()
        for row in completion_rows:
            completion_digest = row["completion_digest"]
            if isinstance(completion_digest, str) and hmac.compare_digest(
                completion_digest,
                token_digest,
            ):
                matches.append((row, True))
    if len(matches) > 1:
        raise ValueError("ambiguous sidebar lease token")
    return matches[0] if matches else (None, False)


def _recover_one_expired_sidebar_lease(
    conn: Any,
    job: Mapping[str, Any],
    *,
    now: float,
) -> None:
    cursor = conn.execute(
        """UPDATE session_sidebar_jobs
           SET state = ?, next_attempt_at = ?, lease_digest = NULL,
               lease_expires_at = NULL, error_code = NULL, updated_at = ?
           WHERE id = ? AND state = ?""",
        (
            SidebarJobState.RETRY.value,
            now,
            now,
            job["id"],
            SidebarJobState.LEASED.value,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("stale expired sidebar lease")


def _persist_sidebar_thread_identity(
    conn: Any,
    job: Mapping[str, Any],
    *,
    thread_id: str,
    now: float,
) -> Mapping[str, Any]:
    """Bind an exact native ID before any lease-expiry transition."""

    existing = job["codex_thread_id"]
    if existing is not None:
        if existing == thread_id:
            return job
        raise ValueError("conflicting Codex thread identity")
    conflict = conn.execute(
        """SELECT id FROM session_sidebar_jobs
           WHERE codex_thread_id = ? AND id != ?""",
        (thread_id, job["id"]),
    ).fetchone()
    if conflict is not None:
        raise ValueError("conflicting Codex thread identity")
    cursor = conn.execute(
        """UPDATE session_sidebar_jobs
           SET codex_thread_id = ?, updated_at = ?
           WHERE id = ? AND state = ? AND codex_thread_id IS NULL""",
        (
            thread_id,
            now,
            job["id"],
            SidebarJobState.LEASED.value,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("stale sidebar thread binding")
    persisted = conn.execute(
        "SELECT * FROM session_sidebar_jobs WHERE id = ?",
        (job["id"],),
    ).fetchone()
    if persisted is None:
        raise ValueError("stale sidebar thread binding")
    return persisted


def _validated_sidebar_job_provider(job: Mapping[str, Any]) -> Provider:
    from .sidebar import sidebar_bridge_id, sidebar_idempotency_key

    source_session_id = job.get("source_session_id")
    if not isinstance(source_session_id, str):
        raise ValueError("invalid sidebar source identity")
    idempotency_key = sidebar_idempotency_key(source_session_id)
    bridge_id = sidebar_bridge_id(source_session_id)
    if (
        job.get("idempotency_key") != idempotency_key
        or job.get("bridge_id") != bridge_id
    ):
        raise ValueError("invalid sidebar job identity")
    if source_session_id.startswith("claude:"):
        return Provider.CLAUDE
    if source_session_id.startswith("codex:"):
        raise ValueError("Codex cannot be a sidebar source")
    return Provider.HERMES


def _sidebar_delivery_state_key(source_session_id: str) -> str:
    from .sidebar import sidebar_idempotency_key

    sidebar_idempotency_key(source_session_id)
    digest = hashlib.sha256(source_session_id.encode()).hexdigest()
    return f"{_SIDEBAR_DELIVERY_STATE_PREFIX}{digest}"


def _sidebar_create_reservation_state_key(source_session_id: str) -> str:
    from .sidebar import sidebar_idempotency_key

    sidebar_idempotency_key(source_session_id)
    digest = hashlib.sha256(source_session_id.encode("utf-8")).hexdigest()
    return f"{_SIDEBAR_CREATE_RESERVATION_PREFIX}{digest}"


def _validated_sidebar_cutover_candidate(
    conn: Any,
    job: Mapping[str, Any],
) -> SidebarCandidate:
    """Validate canonical job and persisted candidate identity for cutover."""

    _validated_sidebar_job_provider(job)
    job_id = _exact_nonempty_text(job.get("id"), "sidebar job ID")
    idempotency_key = _exact_nonempty_text(
        job.get("idempotency_key"),
        "sidebar idempotency key",
    )
    expected_job_id = (
        "sidebar-job:" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    )
    if job_id != expected_job_id:
        raise ValueError("invalid sidebar cutover job identity")
    source_session_id = _exact_nonempty_text(
        job.get("source_session_id"),
        "sidebar source session ID",
    )
    state_row = conn.execute(
        "SELECT value_json FROM session_bridge_state WHERE key = ?",
        (_sidebar_delivery_state_key(source_session_id),),
    ).fetchone()
    if state_row is None:
        raise ValueError("missing sidebar cutover delivery candidate")
    candidate = _decode_sidebar_delivery_candidate(state_row["value_json"])
    if (
        candidate.source_session_id != source_session_id
        or candidate.bridge_id != job["bridge_id"]
        or candidate.eligible_at != float(job["eligible_at"])
    ):
        raise ValueError("invalid sidebar cutover delivery candidate")
    expected_provider = (
        Provider.CLAUDE if source_session_id.startswith("claude:") else Provider.HERMES
    )
    if candidate.provider is not expected_provider:
        raise ValueError("invalid sidebar cutover delivery candidate")
    return candidate


def _sidebar_cutover_recovery_key(
    job: Mapping[str, Any],
    *,
    marker_secret: bytes,
) -> str:
    from .sidebar import sidebar_create_recovery_key

    marker = encode_bridge_marker(
        BridgeMarkerPayload(
            bridge_id=_exact_nonempty_text(job.get("bridge_id"), "sidebar bridge ID"),
            source_session_id=_exact_nonempty_text(
                job.get("source_session_id"),
                "sidebar source session ID",
            ),
            target_provider=Provider.CODEX,
            policy_generation=1,
        ),
        marker_secret,
    )
    return sidebar_create_recovery_key(marker, marker_secret)


def _validate_sidebar_cutover_reservation(
    value_json: object,
    *,
    job: Mapping[str, Any],
    expected_recovery_key: str,
    expected_reserved_at: float | None = None,
) -> dict[str, Any]:
    reservation = _decode_sidebar_create_reservation(
        value_json,
        expected_source_session_id=job["source_session_id"],
    )
    if (
        reservation["job_id"] != job["id"]
        or reservation["bridge_id"] != job["bridge_id"]
        or not hmac.compare_digest(
            reservation["recovery_key"],
            expected_recovery_key,
        )
        or (
            expected_reserved_at is not None
            and float(reservation["reserved_at"]) != expected_reserved_at
        )
    ):
        raise ValueError("sidebar cutover reservation conflict")
    return reservation


def _decode_sidebar_create_reservation_cutover(value_json: object) -> dict[str, Any]:
    if not isinstance(value_json, (str, bytes, bytearray)):
        raise ValueError("invalid sidebar create reservation cutover ledger")
    try:
        payload = json.loads(value_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid sidebar create reservation cutover ledger") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "applied_at",
        "quarantined_job_ids",
    }:
        raise ValueError("invalid sidebar create reservation cutover ledger")
    if type(payload.get("version")) is not int or payload["version"] != 1:
        raise ValueError("invalid sidebar create reservation cutover ledger")
    applied_at = _finite_number(
        payload.get("applied_at"),
        "sidebar create reservation cutover time",
    )
    raw_job_ids = payload.get("quarantined_job_ids")
    if not isinstance(raw_job_ids, list):
        raise ValueError("invalid sidebar create reservation cutover ledger")
    job_ids: list[str] = []
    for value in raw_job_ids:
        job_id = _exact_nonempty_text(value, "sidebar cutover job ID")
        if re.fullmatch(r"sidebar-job:[0-9a-f]{64}", job_id) is None:
            raise ValueError("invalid sidebar create reservation cutover ledger")
        job_ids.append(job_id)
    if job_ids != sorted(set(job_ids)):
        raise ValueError("invalid sidebar create reservation cutover ledger")
    return {
        "version": 1,
        "applied_at": applied_at,
        "quarantined_job_ids": job_ids,
    }


def _validate_sidebar_create_reservation_cutover_replay(
    conn: Any,
    ledger: Mapping[str, Any],
    *,
    marker_secret: bytes,
) -> None:
    applied_at = float(ledger["applied_at"])
    for job_id in ledger["quarantined_job_ids"]:
        row = conn.execute(
            "SELECT * FROM session_sidebar_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise ValueError("sidebar cutover reservation job is missing")
        job = dict(row)
        _validated_sidebar_cutover_candidate(conn, job)
        reservation_row = conn.execute(
            "SELECT value_json FROM session_bridge_state WHERE key = ?",
            (_sidebar_create_reservation_state_key(job["source_session_id"]),),
        ).fetchone()
        if reservation_row is None:
            raise ValueError("sidebar cutover reservation is missing")
        _validate_sidebar_cutover_reservation(
            reservation_row["value_json"],
            job=job,
            expected_recovery_key=_sidebar_cutover_recovery_key(
                job,
                marker_secret=marker_secret,
            ),
            expected_reserved_at=applied_at,
        )


def _sidebar_create_recovery_key(value: object) -> str:
    recovery_key = _exact_nonempty_text(value, "sidebar create recovery key")
    suffix = recovery_key.removeprefix(_SIDEBAR_CREATE_RECOVERY_PREFIX)
    if (
        suffix == recovery_key
        or not suffix
        or len(recovery_key) > 256
        or any(character in recovery_key for character in "\x00\r\n")
    ):
        raise ValueError("invalid sidebar create recovery key")
    return recovery_key


def _decode_sidebar_create_reservation(
    value_json: object,
    *,
    expected_source_session_id: str,
) -> dict[str, Any]:
    if not isinstance(value_json, (str, bytes, bytearray)):
        raise ValueError("invalid sidebar create reservation")
    try:
        payload = json.loads(value_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid sidebar create reservation") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid sidebar create reservation")
    version = payload.get("version")
    expected_fields = (
        _SIDEBAR_CREATE_RESERVATION_V1_FIELDS
        if version == 1 and not isinstance(version, bool)
        else _SIDEBAR_CREATE_RESERVATION_FIELDS
    )
    if (
        set(payload) != expected_fields
        or type(version) is not int
        or version not in {1, 2}
        or payload.get("source_session_id") != expected_source_session_id
    ):
        raise ValueError("invalid sidebar create reservation")
    job_id = _exact_nonempty_text(payload.get("job_id"), "sidebar reservation job ID")
    source_session_id = _exact_nonempty_text(
        payload.get("source_session_id"), "sidebar reservation source ID"
    )
    bridge_id = _exact_nonempty_text(
        payload.get("bridge_id"), "sidebar reservation bridge ID"
    )
    recovery_key = _sidebar_create_recovery_key(payload.get("recovery_key"))
    reserved_at = _finite_number(
        payload.get("reserved_at"), "sidebar create reservation time"
    )
    from .sidebar import sidebar_bridge_id, sidebar_idempotency_key

    sidebar_idempotency_key(source_session_id)
    if bridge_id != sidebar_bridge_id(source_session_id):
        raise ValueError("invalid sidebar create reservation identity")
    result = {
        "version": version,
        "job_id": job_id,
        "source_session_id": source_session_id,
        "bridge_id": bridge_id,
        "recovery_key": recovery_key,
        "reserved_at": reserved_at,
    }
    if version == 2:
        result["reconciliation_proof_digest"] = _lowercase_sha256(
            payload.get("reconciliation_proof_digest"),
            "sidebar reservation reconciliation proof digest",
        )
        result["reconciliation_generation"] = _exact_nonempty_text(
            payload.get("reconciliation_generation"),
            "sidebar reservation reconciliation generation",
        )
    return result


def _worktree_snapshot_state_key(source_session_id: str) -> str:
    from .sidebar import sidebar_idempotency_key

    sidebar_idempotency_key(source_session_id)
    digest = hashlib.sha256(source_session_id.encode("utf-8")).hexdigest()
    return f"{_WORKTREE_SNAPSHOT_STATE_PREFIX}{digest}"


def _encode_worktree_snapshot(
    source_session_id: str,
    candidate: SidebarCandidate,
    snapshot: WorktreeSnapshot,
) -> str:
    from .context_pack import _redact
    from .worktree import WorktreeSnapshot

    if not isinstance(snapshot, WorktreeSnapshot):
        raise ValueError("invalid worktree snapshot")
    if (
        candidate.cwd != snapshot.cwd
        or candidate.git_root != snapshot.git_root
        or candidate.git_branch != snapshot.branch
        or candidate.git_head != snapshot.head
        or candidate.worktree_id != snapshot.worktree_id
    ):
        raise ValueError("sidebar candidate worktree snapshot mismatch")
    required_values = (
        source_session_id,
        snapshot.cwd,
        snapshot.worktree_id,
    )
    if any(
        not isinstance(value, str)
        or not value
        or any(character in value for character in "\x00\r\n")
        or _redact(value) != value
        for value in required_values
    ):
        raise ValueError("invalid worktree snapshot")
    if snapshot.git_root is None:
        if snapshot.branch is not None or snapshot.head is not None:
            raise ValueError("invalid worktree snapshot")
    elif snapshot.branch is None:
        raise ValueError("invalid worktree snapshot")
    optional_values = (snapshot.git_root, snapshot.branch, snapshot.head)
    if any(
        value is not None
        and (
            not isinstance(value, str)
            or not value
            or any(character in value for character in "\x00\r\n")
            or _redact(value) != value
        )
        for value in optional_values
    ):
        raise ValueError("invalid worktree snapshot")
    payload = {
        "version": 1,
        "source_session_id": source_session_id,
        "cwd": snapshot.cwd,
        "git_root": snapshot.git_root,
        "branch": snapshot.branch,
        "head": snapshot.head,
        "worktree_id": snapshot.worktree_id,
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _decode_worktree_snapshot(
    value_json: object,
    expected_source_session_id: str,
) -> WorktreeSnapshot:
    from .context_pack import _redact
    from .worktree import WorktreeSnapshot

    if not isinstance(value_json, (str, bytes, bytearray)):
        raise ValueError("invalid worktree snapshot")
    try:
        payload = json.loads(value_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid worktree snapshot") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != _WORKTREE_SNAPSHOT_FIELDS
        or payload.get("version") != 1
        or isinstance(payload.get("version"), bool)
        or payload.get("source_session_id") != expected_source_session_id
    ):
        raise ValueError("invalid worktree snapshot")
    required_values = tuple(
        payload.get(field)
        for field in (
            "source_session_id",
            "cwd",
            "worktree_id",
        )
    )
    if any(
        not isinstance(value, str)
        or not value
        or any(character in value for character in "\x00\r\n")
        or _redact(value) != value
        for value in required_values
    ):
        raise ValueError("invalid worktree snapshot")
    optional_values = tuple(
        payload.get(field)
        for field in (
            "git_root",
            "branch",
            "head",
        )
    )
    if any(
        value is not None
        and (
            not isinstance(value, str)
            or not value
            or any(character in value for character in "\x00\r\n")
            or _redact(value) != value
        )
        for value in optional_values
    ):
        raise ValueError("invalid worktree snapshot")
    git_root, branch, head = optional_values
    if git_root is None:
        if branch is not None or head is not None:
            raise ValueError("invalid worktree snapshot")
    elif branch is None:
        raise ValueError("invalid worktree snapshot")
    return WorktreeSnapshot(
        cwd=payload["cwd"],
        git_root=payload["git_root"],
        branch=payload["branch"],
        head=payload["head"],
        worktree_id=payload["worktree_id"],
    )


def _encode_sidebar_delivery_candidate(candidate: SidebarCandidate) -> str:
    from .context_pack import _redact
    from .sidebar import _validate_candidate

    _validate_candidate(candidate)
    title = _exact_nonempty_text(candidate.title, "sidebar candidate title")
    _exact_nonempty_text(candidate.cwd, "sidebar candidate cwd")
    for value, label in (
        (candidate.git_root, "sidebar candidate git root"),
        (candidate.git_branch, "sidebar candidate git branch"),
        (candidate.git_head, "sidebar candidate git HEAD"),
        (candidate.worktree_id, "sidebar candidate worktree ID"),
    ):
        if value is not None:
            _exact_nonempty_text(value, label)
    if any(character in title for character in "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"):
        raise ValueError("sidebar candidate title must be a single line")
    if _redact(candidate.source_session_id) != candidate.source_session_id:
        raise ValueError("sidebar source identity cannot be persisted safely")
    if _redact(candidate.bridge_id) != candidate.bridge_id:
        raise ValueError("sidebar bridge identity cannot be persisted safely")

    def _safe(value: str | None) -> str | None:
        return None if value is None else _redact(value)

    payload = {
        "version": 1,
        "source_session_id": candidate.source_session_id,
        "provider": candidate.provider.value,
        "bridge_id": candidate.bridge_id,
        "title": _safe(title),
        "cwd": _safe(candidate.cwd),
        "git_root": _safe(candidate.git_root),
        "git_branch": _safe(candidate.git_branch),
        "git_head": _safe(candidate.git_head),
        "worktree_id": _safe(candidate.worktree_id),
        "eligible_at": _finite_number(candidate.eligible_at, "sidebar eligible_at"),
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _decode_sidebar_delivery_candidate(value_json: object) -> SidebarCandidate:
    from .context_pack import _redact
    from .sidebar import SidebarCandidate, _validate_candidate

    if not isinstance(value_json, (str, bytes, bytearray)):
        raise ValueError("invalid sidebar delivery candidate")
    try:
        payload = json.loads(value_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid sidebar delivery candidate") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != _SIDEBAR_DELIVERY_STATE_FIELDS
        or payload.get("version") != 1
        or isinstance(payload.get("version"), bool)
    ):
        raise ValueError("invalid sidebar delivery candidate")
    provider_value = payload.get("provider")
    try:
        provider = Provider(provider_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid sidebar delivery candidate") from exc
    required = (
        payload.get("source_session_id"),
        payload.get("bridge_id"),
        payload.get("title"),
        payload.get("cwd"),
    )
    optional = (
        payload.get("git_root"),
        payload.get("git_branch"),
        payload.get("git_head"),
        payload.get("worktree_id"),
    )
    if any(
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or _redact(value) != value
        for value in required
    ) or any(
        value is not None
        and (not isinstance(value, str) or not value.strip() or _redact(value) != value)
        for value in optional
    ):
        raise ValueError("invalid sidebar delivery candidate")
    title = payload["title"]
    if any(character in title for character in "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"):
        raise ValueError("invalid sidebar delivery candidate")
    try:
        candidate = SidebarCandidate(
            source_session_id=payload["source_session_id"],
            provider=provider,
            bridge_id=payload["bridge_id"],
            title=payload["title"],
            cwd=payload["cwd"],
            git_root=payload["git_root"],
            git_branch=payload["git_branch"],
            git_head=payload["git_head"],
            worktree_id=payload["worktree_id"],
            eligible_at=_finite_number(
                payload.get("eligible_at"), "sidebar eligible_at"
            ),
        )
        _validate_candidate(candidate)
        _exact_nonempty_text(candidate.title, "sidebar candidate title")
    except ValueError as exc:
        raise ValueError("invalid sidebar delivery candidate") from exc
    return candidate


def _nonnegative_integer(value: object, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")


def _validated_claim_policy(policy: object) -> dict[str, Any]:
    automatic_creation = getattr(policy, "automatic_creation", None)
    creates_per_minute = getattr(policy, "creates_per_minute", None)
    stop_after_attempts = getattr(policy, "stop_after_attempts", None)
    stop_error_rate = getattr(policy, "stop_error_rate", None)
    if type(automatic_creation) is not bool:
        raise ValueError("policy automatic_creation must be a boolean")
    if (
        not isinstance(creates_per_minute, int)
        or isinstance(creates_per_minute, bool)
        or creates_per_minute <= 0
    ):
        raise ValueError("policy creates_per_minute must be a positive integer")
    if (
        not isinstance(stop_after_attempts, int)
        or isinstance(stop_after_attempts, bool)
        or stop_after_attempts <= 0
    ):
        raise ValueError("policy stop_after_attempts must be a positive integer")
    error_rate = _finite_number(stop_error_rate, "policy stop_error_rate")
    if not 0.0 <= error_rate <= 1.0:
        raise ValueError("policy stop_error_rate must be between zero and one")
    return {
        "automatic_creation": automatic_creation,
        "creates_per_minute": creates_per_minute,
        "stop_after_attempts": stop_after_attempts,
        "stop_error_rate": error_rate,
    }


def _read_rate_attempts(conn: Any, *, now: float) -> list[float]:
    row = conn.execute(
        "SELECT value_json FROM session_bridge_state WHERE key = ?",
        (_MIRROR_RATE_STATE_KEY,),
    ).fetchone()
    if row is None:
        return []
    try:
        value = json.loads(row["value_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid mirror rate state") from exc
    if not isinstance(value, dict) or set(value) != {"version", "attempted_at"}:
        raise ValueError("invalid mirror rate state")
    if value["version"] != 1 or isinstance(value["version"], bool):
        raise ValueError("invalid mirror rate state")
    attempted_at = value["attempted_at"]
    if not isinstance(attempted_at, list):
        raise ValueError("invalid mirror rate state")
    recent: list[float] = []
    for raw_timestamp in attempted_at:
        timestamp = _finite_number(raw_timestamp, "mirror rate timestamp")
        if timestamp > now:
            raise ValueError("mirror rate timestamp cannot be in the future")
        if timestamp > now - 60.0:
            recent.append(timestamp)
    return recent


def _write_rate_attempts(
    conn: Any, attempted_at: Sequence[float], *, updated_at: float
) -> None:
    value_json = json.dumps(
        {"version": 1, "attempted_at": list(attempted_at)},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    conn.execute(
        """INSERT INTO session_bridge_state (key, value_json, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,
                                          updated_at = excluded.updated_at""",
        (_MIRROR_RATE_STATE_KEY, value_json, updated_at),
    )


def _read_breaker_progress(conn: Any) -> dict[str, int]:
    row = conn.execute(
        "SELECT value_json FROM session_bridge_state WHERE key = ?",
        (_MIRROR_BREAKER_STATE_KEY,),
    ).fetchone()
    if row is None:
        return {"attempts": 0, "errors": 0, "pending": 0}
    try:
        value = json.loads(row["value_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid mirror breaker progress") from exc
    if not isinstance(value, dict) or set(value) not in (
        {"version", "attempts", "errors"},
        {"version", "attempts", "errors", "pending"},
    ):
        raise ValueError("invalid mirror breaker progress")
    if value["version"] != 1 or isinstance(value["version"], bool):
        raise ValueError("invalid mirror breaker progress")
    attempts = value["attempts"]
    errors = value["errors"]
    pending = value.get("pending", 0)
    _nonnegative_integer(attempts, "mirror breaker progress attempts")
    _nonnegative_integer(errors, "mirror breaker progress errors")
    _nonnegative_integer(pending, "mirror breaker progress pending")
    if errors > attempts or pending > attempts:
        raise ValueError("invalid mirror breaker progress")
    return {"attempts": attempts, "errors": errors, "pending": pending}


def _write_breaker_progress(
    conn: Any, progress: Mapping[str, int], *, updated_at: float
) -> None:
    value_json = json.dumps(
        {
            "version": 1,
            "attempts": progress["attempts"],
            "errors": progress["errors"],
            "pending": progress["pending"],
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    conn.execute(
        """INSERT INTO session_bridge_state (key, value_json, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,
                                          updated_at = excluded.updated_at""",
        (_MIRROR_BREAKER_STATE_KEY, value_json, updated_at),
    )


def _healthy_breaker_batch_completed(
    progress: Mapping[str, int], *, stop_after_attempts: int, stop_error_rate: float
) -> bool:
    attempts = progress["attempts"]
    return (
        attempts >= stop_after_attempts
        and progress["pending"] == 0
        and (progress["errors"] == 0 or progress["errors"] / attempts < stop_error_rate)
    )


def _breaker_is_halted(
    progress: Mapping[str, int], *, stop_after_attempts: int, stop_error_rate: float
) -> bool:
    attempts = progress["attempts"]
    errors = progress["errors"]
    return (
        progress["pending"] > 0
        or attempts >= stop_after_attempts
        or (attempts > 0 and errors > 0 and errors / attempts >= stop_error_rate)
    )


def _create_breaker_reservation(
    conn: Any,
    job: Mapping[str, Any],
    *,
    updated_at: float,
) -> None:
    value_json = json.dumps(
        {"version": 1, "attempts": job["attempts"]},
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        conn.execute(
            """INSERT INTO session_bridge_state (key, value_json, updated_at)
               VALUES (?, ?, ?)""",
            (
                f"{_MIRROR_BREAKER_RESERVATION_PREFIX}{job['id']}",
                value_json,
                updated_at,
            ),
        )
    except Exception as exc:
        raise ValueError("mirror breaker reservation already exists") from exc


def _settle_breaker_reservation(
    conn: Any,
    job: Mapping[str, Any],
    *,
    error: bool,
    updated_at: float,
) -> None:
    key = f"{_MIRROR_BREAKER_RESERVATION_PREFIX}{job['id']}"
    row = conn.execute(
        "SELECT value_json FROM session_bridge_state WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return
    try:
        reservation = json.loads(row["value_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid mirror breaker reservation") from exc
    if (
        not isinstance(reservation, dict)
        or set(reservation) != {"version", "attempts"}
        or reservation.get("version") != 1
        or reservation.get("attempts") != job["attempts"]
    ):
        raise ValueError("invalid mirror breaker reservation")
    progress = _read_breaker_progress(conn)
    if progress["pending"] <= 0:
        raise ValueError("mirror breaker reservation is not pending")
    updated = {
        "attempts": progress["attempts"],
        "errors": progress["errors"] + int(error),
        "pending": progress["pending"] - 1,
    }
    _write_breaker_progress(conn, updated, updated_at=updated_at)
    conn.execute("DELETE FROM session_bridge_state WHERE key = ?", (key,))


def _read_claim_authority(conn: Any, job: Mapping[str, Any]) -> dict[str, Any]:
    row = conn.execute(
        "SELECT value_json FROM session_bridge_state WHERE key = ?",
        (f"{_MIRROR_AUTHORITY_STATE_PREFIX}{job['id']}",),
    ).fetchone()
    if row is None:
        raise KeyError(job["id"])
    try:
        value = json.loads(row["value_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid mirror authority metadata") from exc
    legacy_fields = {
        "authority",
        "idempotency_key",
        "policy_generation",
        "source_session_id",
        "target_provider",
    }
    safe_manual_fields = {*legacy_fields, "require_unmapped"}
    current_fields = {*safe_manual_fields, "rollout_limited"}
    if not isinstance(value, dict) or frozenset(value) not in {
        frozenset(legacy_fields),
        frozenset(safe_manual_fields),
        frozenset(current_fields),
    }:
        raise ValueError("invalid mirror authority metadata")
    require_unmapped = value.get("require_unmapped", False)
    rollout_limited = value.get("rollout_limited", False)
    if (
        type(require_unmapped) is not bool
        or type(rollout_limited) is not bool
        or (rollout_limited and not require_unmapped)
    ):
        raise ValueError("invalid mirror authority metadata")
    value["require_unmapped"] = require_unmapped
    value["rollout_limited"] = rollout_limited
    authority = value["authority"]
    generation = value["policy_generation"]
    if authority not in ("automatic", "manual") or (
        rollout_limited and authority != "manual"
    ):
        raise ValueError("invalid mirror authority metadata")
    _nonnegative_integer(generation, "mirror authority policy generation")
    provider = _external_provider(value["target_provider"])
    source_session_id = value["source_session_id"]
    expected_key = _stable_id(
        "mirror-job", source_session_id, provider.value, str(generation)
    )
    if (
        value["idempotency_key"] != expected_key
        or value["idempotency_key"] != job["idempotency_key"]
        or source_session_id != job["source_session_id"]
        or provider.value != job["target_provider"]
        or job["id"] != f"job:{expected_key}"
    ):
        raise ValueError("invalid mirror authority metadata")
    return value


def _terminalize_unclaimable_job(
    conn: Any,
    job: Mapping[str, Any],
    *,
    now: float,
    code: str,
    detail: str,
) -> None:
    cursor = conn.execute(
        """UPDATE session_mirror_jobs
           SET state = ?, error_code = ?, error_detail = ?, updated_at = ?
           WHERE id = ? AND state = ? AND attempts = ? AND idempotency_key = ?""",
        (
            MirrorJobState.MANUAL_FAILURE.value,
            code,
            detail,
            now,
            job["id"],
            job["state"],
            job["attempts"],
            job["idempotency_key"],
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("stale mirror job authority transition")


def _native_source_identity_issue(
    conn: Any,
    *,
    source_session_id: object,
    source_provider: object,
    profile_shadow_validator: Callable[..., str | None] | None = None,
) -> str | None:
    """Return a fixed reason when a claimed source is not exact native authority."""

    try:
        provider = Provider(source_provider)
    except (TypeError, ValueError):
        return "identity"
    if provider not in {Provider.CLAUDE, Provider.CODEX, Provider.HERMES}:
        return "identity"
    if not isinstance(source_session_id, str):
        return "identity"
    try:
        if provider in {Provider.CLAUDE, Provider.CODEX}:
            canonical_provider = _provider_from_canonical_session_id(source_session_id)
            if canonical_provider is not provider:
                return "identity"
            expected_native_id = source_session_id.split(":", 1)[1]
        else:
            if canonical_session_id(provider, source_session_id) != source_session_id:
                return "identity"
            expected_native_id = source_session_id
    except (TypeError, ValueError):
        return "identity"

    source = conn.execute(
        """SELECT s.source, s.model_config,
                  e.session_id AS external_session_id,
                  e.provider, e.native_id, e.origin_kind, e.origin_bridge_id
           FROM sessions AS s
           LEFT JOIN external_sessions AS e ON e.session_id = s.id
           WHERE s.id = ?""",
        (source_session_id,),
    ).fetchone()
    if source is None:
        return "missing"
    if provider in {Provider.CLAUDE, Provider.CODEX}:
        if (
            source["source"] != provider.value
            or source["external_session_id"] != source_session_id
            or source["provider"] != provider.value
            or source["native_id"] != expected_native_id
        ):
            return "identity"
        if (
            source["origin_kind"] != OriginKind.NATIVE.value
            or source["origin_bridge_id"] is not None
        ):
            return "provenance"
    else:
        session_source = source["source"]
        if session_source == _PROFILE_SHADOW_SOURCE:
            if source["external_session_id"] is not None:
                return "provenance"
            if profile_shadow_validator is None:
                return "identity"
            profile_issue = profile_shadow_validator(
                source_session_id=source_session_id,
                model_config=source["model_config"],
            )
            if profile_issue is not None:
                return profile_issue
        elif (
            not isinstance(session_source, str)
            or not session_source.strip()
            or session_source != session_source.strip()
            or session_source in {Provider.CLAUDE.value, Provider.CODEX.value}
            or source["external_session_id"] is not None
        ):
            return "identity"
    incoming = conn.execute(
        "SELECT 1 FROM session_links WHERE to_session_id = ? LIMIT 1",
        (source_session_id,),
    ).fetchone()
    return "provenance" if incoming is not None else None


def _automatic_claim_denial(conn: Any, job: Mapping[str, Any]) -> str | None:
    source_session_id = job["source_session_id"]
    try:
        source_provider = _provider_from_canonical_session_id(source_session_id)
        target_provider = _external_provider(job["target_provider"])
    except (TypeError, ValueError):
        return "automatic mirror authority is invalid"
    source_issue = _native_source_identity_issue(
        conn,
        source_session_id=source_session_id,
        source_provider=source_provider,
    )
    if source_issue in {"missing", "identity"}:
        return "automatic mirror source identity is not durable"
    if source_issue == "provenance":
        return "automatic mirror source origin is not native"
    mapped = conn.execute(
        """SELECT 1 FROM session_links AS link
           JOIN external_sessions AS target ON target.session_id = link.to_session_id
           WHERE link.from_session_id = ? AND target.provider = ? LIMIT 1""",
        (source_session_id, target_provider.value),
    ).fetchone()
    return "automatic mirror source is already mapped" if mapped is not None else None


def _model_config_has_delegate(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return False
    return isinstance(decoded, Mapping) and decoded.get("_delegate_from") is not None


def _nearest_rank_percentile(
    values: Sequence[float], percentile: float
) -> float | None:
    if not values:
        return None
    rank = max(1, math.ceil(percentile * len(values)))
    return float(values[rank - 1])


def _sidebar_placement_canary_public_status(
    state: Mapping[str, Any] | None,
    *,
    placement_generation: int,
) -> dict[str, Any]:
    if state is None:
        return {"status": "not_run", "verified_at": None}
    if (
        not isinstance(state, Mapping)
        or set(state) != _SIDEBAR_PLACEMENT_CANARY_STATE_FIELDS
        or state.get("version") != 1
        or isinstance(state.get("version"), bool)
        or state.get("status") not in {"passed", "failed"}
        or type(state.get("placement_generation")) is not int
        or state["placement_generation"] < 1
        or type(state.get("canary_identity_digest")) is not str
        or re.fullmatch(r"[0-9a-f]{64}", state["canary_identity_digest"]) is None
    ):
        raise ValueError("invalid sidebar placement canary state")
    verified_at = _finite_number(
        state.get("verified_at"),
        "sidebar placement canary verified_at",
    )
    if verified_at < 0:
        raise ValueError("invalid sidebar placement canary state")
    if state["placement_generation"] != placement_generation:
        return {"status": "not_run", "verified_at": None}
    return {"status": state["status"], "verified_at": verified_at}


def _provider_from_canonical_session_id(session_id: object) -> Provider:
    if not isinstance(session_id, str) or session_id != session_id.strip():
        raise ValueError("invalid external session ID")
    prefix, separator, native_id = session_id.partition(":")
    if not separator or not native_id or native_id != native_id.strip():
        raise ValueError("invalid external session ID")
    provider = _external_provider(prefix)
    if canonical_session_id(provider, native_id) != session_id:
        raise ValueError("invalid external session ID")
    return provider


def _external_provider(provider: Provider | str) -> Provider:
    normalized = Provider(provider)
    if normalized not in _EXTERNAL_PROVIDERS:
        raise ValueError("bridge provider must be Claude or Codex")
    return normalized


def _validated_native_projection_cursor(
    cursor: NativeProjectionCursor | None,
) -> NativeProjectionCursor | None:
    if cursor is None:
        return None
    if not isinstance(cursor, tuple) or len(cursor) != 2:
        raise ValueError("native projection cursor must be an exact pair")
    activity = _finite_number(cursor[0], "native projection cursor activity")
    session_id = cursor[1]
    _provider_from_canonical_session_id(session_id)
    return activity, session_id


def _validated_sidebar_candidate_cursor(
    cursor: SidebarCandidateCursor | None,
) -> SidebarCandidateCursor | None:
    if cursor is None:
        return None
    if not isinstance(cursor, tuple) or len(cursor) != 2:
        raise ValueError("sidebar candidate cursor must be an exact pair")
    activity = _finite_number(cursor[0], "sidebar candidate cursor activity")
    session_id = _exact_nonempty_text(cursor[1], "sidebar candidate cursor session ID")
    from .sidebar import sidebar_idempotency_key

    sidebar_idempotency_key(session_id)
    return activity, session_id


def _bounded_exact_ids(
    values: Sequence[str],
    *,
    label: str,
    maximum: int,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{label} must be a sequence")
    if len(values) > maximum:
        raise ValueError(f"{label} must contain at most {maximum} IDs")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(f"{label} must contain exact nonempty IDs")
        normalized.append(value)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{label} must not contain duplicates")
    return tuple(normalized)


def _public_codex_thread_id(value: object) -> str | None:
    if type(value) is not str or _PUBLIC_CODEX_THREAD_ID.fullmatch(value) is None:
        return None
    return value


def redact_codex_thread_id(value: object) -> str | None:
    """Return a fixed opaque tag only for a structurally safe native task ID."""

    thread_id = _public_codex_thread_id(value)
    if thread_id is None:
        return None
    digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:16]
    return f"task:{digest}"


def _nonempty_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must not be empty")
    return value.strip()


def _continuation_snapshot_state_key(bridge_id: str) -> str:
    return f"{_CONTINUATION_SNAPSHOT_STATE_PREFIX}{bridge_id}"


def _decode_continuation_snapshot(value_json: str) -> dict[str, Any]:
    try:
        value = json.loads(value_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid continuation snapshot encoding") from exc
    if not isinstance(value, dict) or set(value) != _CONTINUATION_SNAPSHOT_FIELDS:
        raise ValueError("invalid continuation snapshot schema")
    if (
        not isinstance(value["version"], int)
        or isinstance(value["version"], bool)
        or value["version"] != 1
    ):
        raise ValueError("invalid continuation snapshot version")
    for field in _CONTINUATION_SNAPSHOT_FIELDS - {"version"}:
        field_value = value[field]
        if (
            not isinstance(field_value, str)
            or not field_value.strip()
            or field_value != field_value.strip()
        ):
            raise ValueError(f"invalid continuation snapshot {field}")
    return value


def _validate_continuation_snapshot_identity(
    conn: Any,
    bridge_id: str,
    snapshot: Mapping[str, Any],
) -> None:
    pack = conn.execute(
        """SELECT 1 FROM session_context_packs
           WHERE id = ? AND bridge_id = ? AND source_session_id = ?
             AND target_session_id = ? AND source_cursor = ?
             AND source_hash = ? AND immutable_at IS NOT NULL""",
        (
            snapshot["pack_id"],
            bridge_id,
            snapshot["source_session_id"],
            snapshot["target_session_id"],
            snapshot["source_cursor"],
            snapshot["source_hash"],
        ),
    ).fetchone()
    link = conn.execute(
        """SELECT 1 FROM session_links
           WHERE bridge_id = ? AND from_session_id = ?
             AND to_session_id = ? AND relation = ?
             AND source_cursor = ? AND source_hash = ?
             AND hydrated_at IS NOT NULL""",
        (
            bridge_id,
            snapshot["source_session_id"],
            snapshot["target_session_id"],
            Relation.CONTINUES.value,
            snapshot["source_cursor"],
            snapshot["source_hash"],
        ),
    ).fetchone()
    if pack is None or link is None:
        raise ValueError("continuation snapshot durable identity mismatch")


def _existing_message_keys(
    conn: Any,
    session_id: str,
    projected_keys: list[tuple[str, int]],
) -> set[tuple[str, int]]:
    existing: set[tuple[str, int]] = set()
    for start in range(0, len(projected_keys), _MESSAGE_KEY_QUERY_CHUNK):
        chunk = projected_keys[start : start + _MESSAGE_KEY_QUERY_CHUNK]
        placeholders = ",".join("(?, ?)" for _ in chunk)
        params: list[Any] = [session_id]
        for native_event_id, ordinal in chunk:
            params.extend((native_event_id, ordinal))
        rows = conn.execute(
            f"""SELECT native_event_id, ordinal
                FROM external_message_map
                WHERE session_id = ?
                  AND (native_event_id, ordinal) IN ({placeholders})""",
            params,
        ).fetchall()
        existing.update((row["native_event_id"], row["ordinal"]) for row in rows)
    return existing


def _external_activity_state_key(session_id: str) -> str:
    return f"{_EXTERNAL_ACTIVITY_KEY_PREFIX}{session_id}"


def _decode_external_activity(value_json: str) -> float:
    try:
        value = json.loads(value_json)
        last_active = value["last_active"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("invalid external session activity watermark") from exc
    if not isinstance(last_active, (int, float)) or isinstance(last_active, bool):
        raise ValueError("invalid external session activity watermark")
    return float(last_active)


def _resolve_projection_provenance(
    existing: Mapping[str, Any] | None,
    incoming_kind: OriginKind,
    incoming_bridge_id: str | None,
    *,
    has_new_human_user: bool,
) -> tuple[str, str | None]:
    if incoming_kind is not OriginKind.NATIVE and not incoming_bridge_id:
        raise ValueError("non-native projection provenance requires a bridge ID")

    if existing is None or existing["origin_kind"] == OriginKind.NATIVE.value:
        return (
            incoming_kind.value,
            None if incoming_kind is OriginKind.NATIVE else incoming_bridge_id,
        )

    existing_kind = OriginKind(existing["origin_kind"])
    existing_bridge_id = existing["origin_bridge_id"]
    if (
        incoming_kind is not OriginKind.NATIVE
        and incoming_bridge_id != existing_bridge_id
    ):
        raise ValueError("projection provenance conflicts with persisted origin")

    if existing_kind is OriginKind.BRIDGE_CONTINUATION:
        return existing_kind.value, existing_bridge_id
    if incoming_kind is OriginKind.BRIDGE_CONTINUATION or (
        incoming_kind is OriginKind.NATIVE and has_new_human_user
    ):
        return OriginKind.BRIDGE_CONTINUATION.value, existing_bridge_id
    return existing_kind.value, existing_bridge_id


def _stable_id(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _snapshot_projected_messages(
    projection: SessionProjection,
) -> list[tuple[ProjectedMessage, dict[str, Any]]]:
    snapshot: list[tuple[ProjectedMessage, dict[str, Any]]] = []
    for message in projection.messages:
        tool_calls = message.tool_calls
        if tool_calls is not None:
            tool_calls = json.loads(
                json.dumps(
                    tool_calls,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
            )
        snapshot.append((
            message,
            {
                "role": message.role,
                "content": message.content,
                "timestamp": message.timestamp,
                "tool_name": message.tool_name,
                "tool_calls": tool_calls,
                "tool_call_id": message.tool_call_id,
                "reasoning": message.reasoning,
            },
        ))
    return snapshot


def _active_message_counters(conn, session_id: str) -> tuple[int, int]:
    rows = conn.execute(
        "SELECT tool_calls FROM messages WHERE session_id = ? AND active = 1",
        (session_id,),
    ).fetchall()
    tool_calls = 0
    for row in rows:
        if row["tool_calls"] is None:
            continue
        value = json.loads(row["tool_calls"])
        tool_calls += len(value) if isinstance(value, list) else 1
    return len(rows), tool_calls


def _claude_status_token(value: Any, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if (
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and re.fullmatch(r"[A-Za-z0-9_-]+", value) is not None
    ):
        folded = value.casefold()
        if any(
            fragment in folded
            for fragment in ("authorization", "bearer", "password", "secret", "token")
        ):
            return "redacted"
        return value
    return "invalid"


def _decode_claude_visibility_cycle_state(value_json: Any) -> dict[str, Any]:
    if value_json is None:
        return {}
    try:
        value = json.loads(value_json)
    except (TypeError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    version = value.get("version")
    if version is not None and (
        type(version) is not int
        or version not in (1, _CLAUDE_VISIBILITY_CYCLE_STATE_VERSION)
    ):
        return {}
    current_version = version == _CLAUDE_VISIBILITY_CYCLE_STATE_VERSION
    sequence = value.get("sequence")
    last_cycle_at = value.get("last_cycle_at")
    last_result = value.get("last_result")
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 1
        or not isinstance(last_cycle_at, (int, float))
        or isinstance(last_cycle_at, bool)
        or not math.isfinite(float(last_cycle_at))
        or not isinstance(last_result, dict)
    ):
        return {}
    status = _claude_status_token(last_result.get("status"))
    error_code = _claude_status_token(last_result.get("error_code"), optional=True)
    empty_verified = last_result.get("empty_verified") if current_version else False
    if (
        status in {None, "invalid", "redacted"}
        or error_code in {"invalid", "redacted"}
        or not isinstance(empty_verified, bool)
    ):
        return {}
    decoded: dict[str, Any] = {
        "version": (_CLAUDE_VISIBILITY_CYCLE_STATE_VERSION if current_version else 1),
        "sequence": sequence,
        "last_cycle_at": float(last_cycle_at),
        "last_result": {
            "status": status,
            "error_code": error_code,
            "empty_verified": empty_verified,
        },
    }
    empty_at = value.get("last_empty_cycle_at") if current_version else None
    if (
        isinstance(empty_at, (int, float))
        and not isinstance(empty_at, bool)
        and math.isfinite(float(empty_at))
    ):
        decoded["last_empty_cycle_at"] = float(empty_at)
    registrar = value.get("last_registrar_result")
    if isinstance(registrar, dict):
        registrar_status = _claude_status_token(registrar.get("status"))
        registrar_error = _claude_status_token(
            registrar.get("error_code"), optional=True
        )
        registrar_at = registrar.get("at")
        registrar_sequence = registrar.get("sequence")
        if (
            registrar_status not in {None, "invalid", "redacted"}
            and registrar_error not in {"invalid", "redacted"}
            and isinstance(registrar_at, (int, float))
            and not isinstance(registrar_at, bool)
            and math.isfinite(float(registrar_at))
            and isinstance(registrar_sequence, int)
            and not isinstance(registrar_sequence, bool)
            and registrar_sequence >= 1
        ):
            decoded["last_registrar_result"] = {
                "at": float(registrar_at),
                "sequence": registrar_sequence,
                "status": registrar_status,
                "error_code": registrar_error,
            }
    return decoded


def _public_claude_visibility_cycle_state(
    cycle: Mapping[str, Any],
) -> dict[str, Any]:
    if not cycle:
        return {
            "last_cycle": {"tracked": False, "value": None},
            "last_empty_cycle": {"tracked": False, "value": None},
            "last_registrar_result": {"tracked": False, "value": None},
        }
    last_result = cycle["last_result"]
    return {
        "last_cycle": {
            "tracked": True,
            "value": {
                "at": cycle["last_cycle_at"],
                "sequence": cycle["sequence"],
                "status": last_result["status"],
                "error_code": last_result["error_code"],
                "empty_verified": last_result["empty_verified"],
            },
        },
        "last_empty_cycle": {
            "tracked": "last_empty_cycle_at" in cycle,
            "value": cycle.get("last_empty_cycle_at"),
        },
        "last_registrar_result": {
            "tracked": "last_registrar_result" in cycle,
            "value": cycle.get("last_registrar_result"),
        },
    }


def _mirror_state(job_states: Sequence[str], links: Sequence[dict[str, Any]]) -> str:
    if any(link["diverged_at"] is not None for link in links):
        return "diverged"
    if any(
        link["relation"] in (Relation.CONTINUES.value, Relation.FORKS.value)
        for link in links
    ):
        return "continued"
    if any(link["relation"] == Relation.MIRRORS.value for link in links):
        return "mirrored"
    if MirrorJobState.MANUAL_FAILURE.value in job_states:
        return "failed"
    if any(
        state
        in (
            MirrorJobState.QUEUED.value,
            MirrorJobState.RUNNING.value,
            MirrorJobState.RETRY.value,
        )
        for state in job_states
    ):
        return "queued"
    if MirrorJobState.SUCCEEDED.value in job_states:
        return "mirrored"
    return "catalog_only"


__all__ = ["SessionBridgeStore", "SidebarNativeTaskNotIndexed"]
