"""Fixed public error-code contract for Claude visibility."""

from __future__ import annotations


CLAUDE_VISIBILITY_RETRY_CODES = frozenset({
    "claude_executable_unavailable",
    "claude_authentication_unavailable",
    "desktop_unavailable",
    "pty_unavailable",
    "native_transcript_not_indexed",
    "clean_exit_not_observed",
    "session_bridge_unavailable",
    "creation_ambiguous",
    "lease_expired",
})
CLAUDE_VISIBILITY_FATAL_CODES = frozenset({
    "uuid_conflict",
    "source_conflict",
    "bridge_conflict",
    "provider_conflict",
    "cwd_conflict",
    "name_conflict",
    "marker_conflict",
    "duplicate_uuid",
    "duplicate_identity",
    "max_attempts_exhausted",
    "unknown_error_code",
})
CLAUDE_VISIBILITY_ERROR_CODES = (
    CLAUDE_VISIBILITY_RETRY_CODES | CLAUDE_VISIBILITY_FATAL_CODES
)
CLAUDE_VISIBILITY_PUBLIC_RESULT_ERROR_CODES = (
    CLAUDE_VISIBILITY_ERROR_CODES
    | frozenset({
        "claim_failed",
        "enqueue_failed",
        "invalid_visibility_status",
        "inventory_invalid",
        "provider_degraded",
        "registrar_failed",
        "unknown_claim_status",
        "unknown_failed_code",
        "unknown_job_state",
        "unknown_registrar_error_code",
        "unknown_registrar_status",
        "unknown_retry_code",
    })
)
