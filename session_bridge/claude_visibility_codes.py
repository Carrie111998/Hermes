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
# Fatal-group codes the read-only status may emit. THREE readers -- cli,
# mcp_server and coordinator -- each used to hardcode this set inline and
# flatten anything outside it to a generic "invalid status". That is how an
# abandoned repair lease came to report itself as malformed evidence instead of
# as the one condition it actually was, sending an operator hunting a schema bug
# that did not exist. Extend HERE, once; the readers derive from this.
CLAUDE_VISIBILITY_STATUS_FATAL_CODES = frozenset({
    "unknown_job_state",
    "unknown_error_code",
    "reconciliation_repair_active",
    "reconciliation_repair_abandoned",
})
# Why the startup preflight refused, one fixed code per gate. These never reach
# public CLI output -- `main` collapses ProviderDegraded to {"error":
# "provider_degraded"} on purpose -- they exist so the SERVICE LOG names the
# gate. Before this, ten independent refusals shared one message and telling
# "wrong version" from "not logged in" needed a bespoke probe.
CLAUDE_VISIBILITY_PREFLIGHT_FAILURE_CODES = frozenset({
    "claude_visibility_preflight_failed_config_dir_override",
    "claude_visibility_preflight_failed_forced_onboarding",
    "claude_visibility_preflight_failed_command_error",
    "claude_visibility_preflight_failed_version_unpinned",
    "claude_visibility_preflight_failed_auth_unavailable",
    "claude_visibility_preflight_failed_auth_output_invalid",
    "claude_visibility_preflight_failed_auth_output_too_large",
    "claude_visibility_preflight_failed_not_logged_in",
    "claude_visibility_preflight_failed_onboarding_incomplete",
    "claude_visibility_preflight_failed_theme_unavailable",
})
CLAUDE_VISIBILITY_PUBLIC_RESULT_ERROR_CODES = CLAUDE_VISIBILITY_ERROR_CODES | frozenset({
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
    "reconciliation_repair_active",
    "reconciliation_repair_abandoned",
})
