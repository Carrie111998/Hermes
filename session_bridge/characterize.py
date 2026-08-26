from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any
import uuid

from agent.transports.codex_app_server import CodexAppServerClient
from hermes_constants import get_default_hermes_root

from .claude_adapter import (
    CLAUDE_PLACEHOLDER_MAX_BUDGET_USD,
    ClaudeMarkerSource,
    ClaudeReadableSource,
    ClaudeSourceAdapter,
    ClaudeTargetAdapter,
    PlaceholderCreationError,
    classify_claude_process_failure,
    resolve_claude_command,
)
from .codex_adapter import CodexSourceAdapter, CodexTargetAdapter
from .models import (
    BridgeMarkerPayload,
    InvalidBridgeMarker,
    OriginKind,
    Provider,
    SessionProjection,
    decode_bridge_marker,
)
from .claude_visibility import (
    ClaudeVisibilityCandidate,
    ClaudeVisibilityClaim,
    build_claude_visibility_candidate,
    build_claude_registration_prompt,
    derive_claude_visibility_identity,
)
from .claude_registrar import (
    _classify_exact_auth_recovery_messages,
    _is_exact_registered_text,
    build_characterization_auth_recovery_prompt,
)


_CLAUDE_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
_SENSITIVE_REPORT_KEYS = frozenset({
    "context",
    "marker",
    "prompt",
    "secret",
    "stderr",
    "stdout",
    "token",
    "transcript",
})
_MARKER_PREFIX = "HERMES_SESSION_BRIDGE_V1:"
_MAX_CLI_VERSION_BYTES = 4096
_CHARACTERIZATION_RECORD = ".claude-visibility-operation.json"
_CHARACTERIZATION_SENTINEL = ".session-bridge-characterization.json"
_ABORT_QUARANTINE_DIRECTORY = ".abort-quarantine"
_CLEANUP_QUARANTINE_DIRECTORY = ".cleanup-quarantine"
_ABORT_CLAIM_SCAN_LIMIT = 100
_CLEANUP_CLAIM_SCAN_LIMIT = 100
_CLEANUP_TTL_SECONDS = 7 * 24 * 60 * 60
_CODEX_ORIGIN_GUARD_DIRECTORY = ".codex-origin-guards"
_CODEX_ORIGIN_GUARD_LIMIT = 100
_PROVIDER_REQUIRED_FIELDS = frozenset({
    "create",
    "discover",
    "read",
    "resume",
    "used_registration_turn",
    "cleanup",
    "error_code",
})


def characterization_store_root() -> Path:
    """Canonical machine-global characterization store.

    Characterization reports prove facts about the machine's installed
    provider CLIs, so every process must resolve ONE store regardless of
    profile scoping.  ``get_hermes_home()`` resolves profile-scoped when
    ``HERMES_HOME`` (or the serve ``--config-home`` context override) names
    ``<root>/profiles/<name>``; that forked the store in two diverged
    directories on 2026-08-25, with which one a process read decided by its
    environment.  Anchoring on :func:`get_default_hermes_root` — the same
    repair ``events.paths`` made for the notification layer — keeps custom
    deployment homes (tests, Docker) hermetic while mapping any
    profile-scoped home back to its root.
    """

    return get_default_hermes_root() / "session-bridge" / "characterization"


def characterization_source_root() -> Path:
    """Claude visibility source records live inside the one store."""

    return characterization_store_root() / "claude-visibility-sources"


def _new_characterization_operation_id() -> str:
    return str(uuid.uuid4())


class CharacterizationAuthenticationFailure(RuntimeError):
    def __init__(self, evidence_digest: str) -> None:
        super().__init__("characterization_authentication_failure")
        self.evidence_digest = evidence_digest


class CharacterizationRecoveredTranscript(RuntimeError):
    def __init__(
        self, evidence_digest: str, prompt_digest: str, transcript_digest: str
    ) -> None:
        super().__init__("characterization_recovery_authority_required")
        self.evidence_digest = evidence_digest
        self.prompt_digest = prompt_digest
        self.transcript_digest = transcript_digest


_PROVIDER_ALLOWED_FIELDS = {
    "claude": _PROVIDER_REQUIRED_FIELDS
    | {
        "native_id",
        "create_cost_usd",
        "create_latency_ms",
        "create_num_turns",
        "resume_cost_usd",
        "resume_latency_ms",
        "resume_num_turns",
        "total_cost_usd",
        "total_latency_ms",
        "total_num_turns",
        "observed_cost_usd",
        "duration_ms",
        "num_turns",
    },
    "codex": _PROVIDER_REQUIRED_FIELDS
    | {
        "native_id",
        "create_latency_ms",
        "resume_latency_ms",
        "total_latency_ms",
    },
}


def characterize_claude_visibility(
    *,
    source_root: Path,
    projects_root: Path,
    reserve: Callable[[SessionProjection], ClaudeVisibilityClaim],
    reconcile_existing: Callable[[SessionProjection], ClaudeVisibilityClaim]
    | None = None,
    registration_is_visible: Callable[[Mapping[str, Any]], bool] | None = None,
    registrar: Any,
    restarted_source: Callable[[], ClaudeReadableSource],
    marker_secret: bytes,
    recover_auth_failure: Callable[[Mapping[str, Any], str, str], Mapping[str, Any]]
    | None = None,
    complete_auth_recovery: Callable[[Mapping[str, Any], str], None] | None = None,
    reconcile_auth_recovery: Callable[[Mapping[str, Any], str, str, str], None]
    | None = None,
    record_writer: Callable[[Path, dict[str, Any], bytes], None] | None = None,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Serialize one root-wide characterization creation or resume operation."""

    _require_secret(marker_secret)
    root = _prepare_safe_root(source_root, create=True)
    with _exclusive_cleanup_lock(root, "characterization-root"):
        return _characterize_claude_visibility_locked(
            source_root=root,
            projects_root=projects_root,
            reserve=reserve,
            reconcile_existing=reconcile_existing,
            registration_is_visible=registration_is_visible,
            registrar=registrar,
            restarted_source=restarted_source,
            marker_secret=marker_secret,
            recover_auth_failure=recover_auth_failure,
            complete_auth_recovery=complete_auth_recovery,
            reconcile_auth_recovery=reconcile_auth_recovery,
            record_writer=record_writer,
            now=now,
        )


def _characterize_claude_visibility_locked(
    *,
    source_root: Path,
    projects_root: Path,
    reserve: Callable[[SessionProjection], ClaudeVisibilityClaim],
    reconcile_existing: Callable[[SessionProjection], ClaudeVisibilityClaim]
    | None = None,
    registration_is_visible: Callable[[Mapping[str, Any]], bool] | None = None,
    registrar: Any,
    restarted_source: Callable[[], ClaudeReadableSource],
    marker_secret: bytes,
    recover_auth_failure: Callable[[Mapping[str, Any], str, str], Mapping[str, Any]]
    | None = None,
    complete_auth_recovery: Callable[[Mapping[str, Any], str], None] | None = None,
    reconcile_auth_recovery: Callable[[Mapping[str, Any], str, str, str], None]
    | None = None,
    record_writer: Callable[[Path, dict[str, Any], bytes], None] | None = None,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Register and safely remove one disposable native Claude mirror.

    The caller owns the durable reservation transaction and registrar.  This
    function deliberately calls each exactly once, then constructs a fresh
    source adapter to prove restart-safe, exact-ID discovery before deleting
    only the transcript whose complete identity has been verified.
    """

    _require_secret(marker_secret)
    writer = record_writer or _write_characterization_record
    root = _prepare_safe_root(source_root, create=True)
    project_root = _prepare_safe_root(projects_root, create=False)
    _assert_no_unresolved_abort_claims(root, marker_secret)
    _assert_no_unresolved_cleanup_claims(root, marker_secret)
    active_path = root / _CHARACTERIZATION_RECORD
    timestamp = float(now())
    renew_ready_authority = False
    registration_proven = False
    state: dict[str, Any]
    if active_path.exists():
        state = _read_characterization_record(active_path, marker_secret)
        _validate_operation_state(state, root=root, now=timestamp)
        disposable = _validate_disposable(state, root)
        if state["phase"] == "prepared":
            projection = _bind_prepared_characterization_identity(state, marker_secret)
            writer(active_path, state, marker_secret)
            outcome = _process_characterization_reservation(
                state=state,
                projection=projection,
                reserve=reserve,
                registrar=registrar,
            )
            if getattr(outcome, "status", None) != "visible":
                raise RuntimeError("characterization_registration_failed")
            registration_proven = True
            state["phase"] = "launched"
            writer(active_path, state, marker_secret)
        elif state["phase"] == "reserved":
            # Legacy crash boundary: exact identity was durable before launch.
            state["phase"] = "launching"
            writer(active_path, state, marker_secret)
        renew_ready_authority = state["phase"] == "ready" and timestamp > float(
            state["expires_at"]
        )
        if state["phase"] == "launching":
            try:
                _validate_characterization_transcript(
                    restarted=restarted_source(),
                    projects_root=project_root,
                    reserved_uuid=_required_state_text(state, "reserved_claude_uuid"),
                    native_name=_required_state_text(state, "native_name"),
                    source_cwd=_required_state_text(state, "source_cwd"),
                    signed_marker=_required_state_text(state, "signed_marker"),
                    marker_secret=marker_secret,
                )
                if registration_is_visible is not None and registration_is_visible(
                    state
                ):
                    registration_proven = True
                    state["phase"] = "launched"
                    writer(active_path, state, marker_secret)
                else:
                    if reconcile_existing is None:
                        raise RuntimeError(
                            "characterization_registration_state_unverified"
                        )
                    projection = _characterization_projection(state)
                    claim = reconcile_existing(projection)
                    _validate_characterization_recovery_claim(
                        claim, state, require_reconciliation=True
                    )
                    outcome = registrar.process(claim)
                    _validate_characterization_outcome(outcome, state)
                    if getattr(outcome, "status", None) != "visible":
                        raise RuntimeError("characterization_registration_failed")
                    registration_proven = True
                    state["phase"] = "launched"
                    writer(active_path, state, marker_secret)
            except CharacterizationRecoveredTranscript as exc:
                if reconcile_auth_recovery is None:
                    raise
                reconcile_auth_recovery(
                    state,
                    exc.evidence_digest,
                    exc.prompt_digest,
                    exc.transcript_digest,
                )
                registration_proven = True
                state["phase"] = "launched"
                writer(active_path, state, marker_secret)
            except CharacterizationAuthenticationFailure as exc:
                if recover_auth_failure is None or complete_auth_recovery is None:
                    raise
                recovery_prompt = build_characterization_auth_recovery_prompt(
                    _required_state_text(state, "reserved_claude_uuid"),
                    _required_state_text(state, "signed_marker"),
                )
                recovery = recover_auth_failure(
                    state, exc.evidence_digest, recovery_prompt
                )
                if (
                    recovery.get("status") != "recovered"
                    or recovery.get("job_id") != state.get("job_id")
                    or recovery.get("reserved_claude_uuid")
                    != state.get("reserved_claude_uuid")
                ):
                    raise RuntimeError("characterization_registration_failed")
                recovered_transcript = _validate_characterization_transcript(
                    restarted=restarted_source(),
                    projects_root=project_root,
                    reserved_uuid=_required_state_text(state, "reserved_claude_uuid"),
                    native_name=_required_state_text(state, "native_name"),
                    source_cwd=_required_state_text(state, "source_cwd"),
                    signed_marker=_required_state_text(state, "signed_marker"),
                    marker_secret=marker_secret,
                    allow_recovered=True,
                )
                complete_auth_recovery(recovery, _sha256_file(recovered_transcript))
                registration_proven = True
                state["phase"] = "launched"
                writer(active_path, state, marker_secret)
            except RuntimeError as exc:
                if str(exc) != "characterization_identity_mismatch:exact_uuid":
                    raise
                projection = _characterization_projection(state)
                claim = reserve(projection)
                _validate_characterization_recovery_claim(claim, state)
                outcome = registrar.process(claim)
                _validate_characterization_outcome(outcome, state)
                if getattr(outcome, "status", None) == "absent":
                    # ``absent`` is returned only after the registrar durably
                    # records exact-UUID absence and releases its reconciliation
                    # lease.  Only the store's next paid launch lease authorizes
                    # relaunch, and every persisted identity must remain exact.
                    claim = reserve(projection)
                    _validate_characterization_recovery_claim(
                        claim, state, require_launch=True
                    )
                    outcome = registrar.process(claim)
                    _validate_characterization_outcome(outcome, state)
                if getattr(outcome, "status", None) != "visible":
                    raise RuntimeError("characterization_registration_failed")
                registration_proven = True
                state["phase"] = "launched"
                writer(active_path, state, marker_secret)
    else:
        operation_id = _new_characterization_operation_id()
        disposable = root / f"claude-visibility-{operation_id}"
        os.mkdir(disposable)
        sentinel_nonce = secrets.token_urlsafe(32)
        _write_identity_sentinel(disposable, operation_id, sentinel_nonce)
        expiry = timestamp + _CLEANUP_TTL_SECONDS
        state = {
            "schema_version": 2,
            "operation_id": operation_id,
            "phase": "prepared",
            "created_at": timestamp,
            "expires_at": expiry,
            "source_provider": Provider.CODEX.value,
            "source_session_id": None,
            "bridge_id": None,
            "job_id": None,
            "reserved_claude_uuid": None,
            "native_name": None,
            "source_cwd": str(disposable),
            "signed_marker": None,
            "transcript_path": None,
            "transcript_identity": None,
            "sentinel_nonce": sentinel_nonce,
            "cleanup_authorized_at": None,
            "cleanup_capability_hash": _cleanup_capability_hash(
                marker_secret, operation_id, expiry
            ),
        }
        writer(active_path, state, marker_secret)
        projection = _characterization_projection(state)
        _bind_prepared_characterization_identity(state, marker_secret)
        writer(active_path, state, marker_secret)
        outcome = _process_characterization_reservation(
            state=state,
            projection=projection,
            reserve=reserve,
            registrar=registrar,
        )
        if getattr(outcome, "status", None) != "visible":
            raise RuntimeError("characterization_registration_failed")
        registration_proven = True
        state["phase"] = "launched"
        writer(active_path, state, marker_secret)

    reserved_uuid = _required_state_text(state, "reserved_claude_uuid")
    native_name = _required_state_text(state, "native_name")
    source_cwd = _required_state_text(state, "source_cwd")
    signed_marker = _required_state_text(state, "signed_marker")
    _validated_characterization_marker(
        signed_marker,
        marker_secret,
        source_session_id=_required_state_text(state, "source_session_id"),
        bridge_id=_required_state_text(state, "bridge_id"),
    )

    restarted = restarted_source()
    resolved_transcript = _validate_characterization_transcript(
        restarted=restarted,
        projects_root=project_root,
        reserved_uuid=reserved_uuid,
        native_name=native_name,
        source_cwd=source_cwd,
        signed_marker=signed_marker,
        marker_secret=marker_secret,
        allow_recovered=True,
        allow_post_ready_continuations=state["phase"] == "ready",
    )
    if not registration_proven:
        if registration_is_visible is not None and registration_is_visible(state):
            registration_proven = True
        else:
            if reconcile_existing is None:
                raise RuntimeError("characterization_registration_state_unverified")
            projection = _characterization_projection(state)
            claim = reconcile_existing(projection)
            _validate_characterization_recovery_claim(
                claim, state, require_reconciliation=True
            )
            outcome = registrar.process(claim)
            _validate_characterization_outcome(outcome, state)
            if getattr(outcome, "status", None) != "visible":
                raise RuntimeError("characterization_registration_failed")
            registration_proven = True
    if state["transcript_path"] not in (None, str(resolved_transcript)):
        raise RuntimeError("characterization_identity_mismatch:path_changed")
    transcript_identity = list(_path_identity(resolved_transcript))
    previous_transcript_identity = state["transcript_identity"]
    if previous_transcript_identity is not None and _recorded_object_identity(
        previous_transcript_identity
    ) != _object_identity(resolved_transcript):
        raise RuntimeError("characterization_identity_mismatch:path_changed")
    state["transcript_path"] = str(resolved_transcript)
    state["transcript_identity"] = transcript_identity
    if renew_ready_authority:
        renewed_expiry = timestamp + _CLEANUP_TTL_SECONDS
        state["expires_at"] = renewed_expiry
        state["cleanup_capability_hash"] = _cleanup_capability_hash(
            marker_secret, _required_state_text(state, "operation_id"), renewed_expiry
        )
    state["phase"] = "ready"
    writer(active_path, state, marker_secret)
    operation_id = _required_state_text(state, "operation_id")
    cleanup_token = _cleanup_capability(
        marker_secret, operation_id, float(state["expires_at"])
    )
    return {
        "passed": True,
        "source_provider": Provider.CODEX.value,
        "source_cwd": str(disposable),
        "reserved_claude_uuid": reserved_uuid,
        "native_name": native_name,
        "restart_exact_id_verified": True,
        "operator_checks": [
            "Run /resume in Claude Code and select the deterministic characterization name.",
            "Press Ctrl+A in /resume to verify the exact session across all projects.",
            f"Resume the exact ID with: claude --resume {reserved_uuid}",
            "After all checks pass, rerun with --cleanup-token '<the returned JSON object>'.",
        ],
        "verification": "pending_operator_checks",
        "cleanup": "pending_explicit_confirmation",
        "cleanup_expires_at": state["expires_at"],
        "cleanup_token": {
            "id": operation_id,
            "capability": cleanup_token,
        },
    }


def _characterization_projection(state: Mapping[str, Any]) -> SessionProjection:
    operation_id = _required_state_text(state, "operation_id")
    source_cwd = _required_state_text(state, "source_cwd")
    created_at = _required_state_number(state, "created_at")
    return SessionProjection(
        provider=Provider.CODEX,
        native_id=operation_id,
        title="Claude native visibility characterization",
        cwd=source_cwd,
        started_at=created_at,
        last_active=created_at,
        messages=[_characterization_message(created_at)],
        native_path=str(Path(source_cwd) / "source.json"),
        native_hash="0" * 64,
        origin_kind=OriginKind.NATIVE,
    )


def _bind_prepared_characterization_identity(
    state: dict[str, Any], marker_secret: bytes
) -> SessionProjection:
    """Persist the deterministic exact identity before any store/lease mutation."""

    projection = _characterization_projection(state)
    candidate = build_claude_visibility_candidate(
        projection, eligible_at=projection.last_active
    )
    identity = derive_claude_visibility_identity(candidate, marker_secret)
    state.update({
        "phase": "launching",
        "source_session_id": candidate.source_session_id,
        "bridge_id": identity.bridge_id,
        "job_id": identity.job_id,
        "reserved_claude_uuid": identity.claude_uuid,
        "native_name": candidate.native_name,
        "signed_marker": identity.signed_marker,
    })
    return projection


def _process_characterization_reservation(
    *,
    state: Mapping[str, Any],
    projection: SessionProjection,
    reserve: Callable[[SessionProjection], ClaudeVisibilityClaim],
    registrar: Any,
) -> Any:
    """Use only store-authorized exact launch/reconciliation leases."""

    claim = reserve(projection)
    _validate_characterization_recovery_claim(claim, state)
    outcome = registrar.process(claim)
    _validate_characterization_outcome(outcome, state)
    if getattr(outcome, "status", None) == "absent":
        # Durable exact absence authorizes only the store's next launch lease,
        # which remains bound to the same deterministic UUID.
        claim = reserve(projection)
        _validate_characterization_recovery_claim(claim, state, require_launch=True)
        outcome = registrar.process(claim)
        _validate_characterization_outcome(outcome, state)
    return outcome


def _validate_characterization_recovery_claim(
    claim: Any,
    state: Mapping[str, Any],
    *,
    require_launch: bool = False,
    require_reconciliation: bool = False,
) -> None:
    if require_launch and require_reconciliation:
        raise ValueError("conflicting characterization lease requirements")
    lease_kind = getattr(claim, "lease_kind", None)
    valid_authority = (
        not require_reconciliation
        and lease_kind == "launch"
        and getattr(claim, "launch_permitted", None) is True
        and getattr(claim, "registration_reserved", None) is True
        and getattr(claim, "requires_exact_id_reconciliation", None) is False
    ) or (
        not require_launch
        and lease_kind == "reconciliation"
        and getattr(claim, "launch_permitted", None) is False
        and getattr(claim, "registration_reserved", None) is False
        and getattr(claim, "requires_exact_id_reconciliation", None) is True
    )
    if (
        not isinstance(claim, ClaudeVisibilityClaim)
        or not claim.claimed
        or not valid_authority
        or claim.job_id != state.get("job_id")
        or claim.source_session_id != state.get("source_session_id")
        or claim.source_provider is not Provider.CODEX
        or claim.source_cwd != state.get("source_cwd")
        or claim.reserved_claude_uuid != state.get("reserved_claude_uuid")
        or claim.native_name != state.get("native_name")
        or claim.signed_marker != state.get("signed_marker")
    ):
        raise RuntimeError("characterization_reservation_invalid")


def _validate_characterization_outcome(outcome: Any, state: Mapping[str, Any]) -> None:
    if getattr(outcome, "job_id", None) != state.get("job_id") or getattr(
        outcome, "reserved_claude_uuid", None
    ) != state.get("reserved_claude_uuid"):
        raise RuntimeError("characterization_registration_failed")


def cleanup_characterized_claude_visibility(
    *,
    cleanup_token: Mapping[str, Any],
    source_root: Path,
    projects_root: Path,
    restarted_source: Callable[[], ClaudeReadableSource],
    marker_secret: bytes,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Explicit second phase after the operator completes native picker checks."""

    _require_secret(marker_secret)
    operation_id, capability = _parse_cleanup_token(cleanup_token)
    root = _prepare_safe_root(source_root, create=False)
    project_root = _prepare_safe_root(projects_root, create=False)
    with _exclusive_cleanup_lock(root, "characterization-root"):
        with _exclusive_cleanup_lock(root, operation_id):
            return _cleanup_characterized_claude_visibility_locked(
                operation_id=operation_id,
                capability=capability,
                root=root,
                project_root=project_root,
                restarted_source=restarted_source,
                marker_secret=marker_secret,
                timestamp=float(now()),
            )


def claim_claude_visibility_characterization_abort(
    *,
    source_root: Path,
    marker_secret: bytes,
    expected_job_id: str,
    expected_reserved_claude_uuid: str,
    expected_operation_id: str,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Durably move one authenticated active probe into abort-only ownership."""

    _require_secret(marker_secret)
    if not isinstance(expected_job_id, str) or not expected_job_id:
        raise RuntimeError("characterization_abort_identity_mismatch")
    if (
        not isinstance(expected_reserved_claude_uuid, str)
        or not expected_reserved_claude_uuid
    ):
        raise RuntimeError("characterization_abort_identity_mismatch")
    try:
        if str(uuid.UUID(expected_operation_id)) != expected_operation_id:
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise RuntimeError("characterization_abort_identity_mismatch") from None
    root = _prepare_safe_root(source_root, create=False)
    with _exclusive_cleanup_lock(root, "characterization-root"):
        with _exclusive_cleanup_lock(root, expected_operation_id):
            claims = root / ".abort-claims"
            completed = root / ".abort-completed"
            claims.mkdir(exist_ok=True)
            completed.mkdir(exist_ok=True)
            _require_plain_directory(claims)
            _require_plain_directory(completed)
            active = root / _CHARACTERIZATION_RECORD
            claimed = claims / f"{expected_operation_id}.json"
            done = completed / f"{expected_operation_id}.json"
            if done.exists():
                state = _read_characterization_record(done, marker_secret)
                _validate_aborted_characterization_identity(
                    state,
                    root=root,
                    expected_operation_id=expected_operation_id,
                    expected_job_id=expected_job_id,
                    expected_reserved_claude_uuid=expected_reserved_claude_uuid,
                    marker_secret=marker_secret,
                    allowed_phases={"aborted"},
                )
                return {
                    "status": "already_aborted",
                    "job_id": expected_job_id,
                    "reserved_claude_uuid": expected_reserved_claude_uuid,
                    "operation": dict(state),
                }
            if not claimed.exists():
                if not active.exists():
                    raise RuntimeError("characterization_abort_identity_mismatch")
                state = _read_characterization_record(active, marker_secret)
                if state.get("phase") in {"reserved", "launching"}:
                    _validate_operation_state(state, root=root, now=float(now()))
                _validate_aborted_characterization_identity(
                    state,
                    root=root,
                    expected_operation_id=expected_operation_id,
                    expected_job_id=expected_job_id,
                    expected_reserved_claude_uuid=expected_reserved_claude_uuid,
                    marker_secret=marker_secret,
                    allowed_phases={"reserved", "launching"},
                )
                os.replace(active, claimed)
            state = _read_characterization_record(claimed, marker_secret)
            _validate_aborted_characterization_identity(
                state,
                root=root,
                expected_operation_id=expected_operation_id,
                expected_job_id=expected_job_id,
                expected_reserved_claude_uuid=expected_reserved_claude_uuid,
                marker_secret=marker_secret,
                allowed_phases={
                    "reserved",
                    "launching",
                    "abort_disposable_removing",
                    "abort_disposable_removed",
                },
            )
            return {
                "status": "claimed",
                "job_id": expected_job_id,
                "reserved_claude_uuid": expected_reserved_claude_uuid,
                "operation": dict(state),
            }


def retire_aborted_claude_visibility_characterization(
    *,
    source_root: Path,
    marker_secret: bytes,
    expected_job_id: str,
    expected_reserved_claude_uuid: str,
    expected_operation_id: str | None = None,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Crash-safely archive one exact-absence probe without touching Claude data."""

    _require_secret(marker_secret)
    if not isinstance(expected_job_id, str) or not expected_job_id:
        raise RuntimeError("characterization_abort_identity_mismatch")
    if (
        not isinstance(expected_reserved_claude_uuid, str)
        or not expected_reserved_claude_uuid
    ):
        raise RuntimeError("characterization_abort_identity_mismatch")
    root = _prepare_safe_root(source_root, create=False)
    operation_id = expected_operation_id
    if operation_id is None:
        active = root / _CHARACTERIZATION_RECORD
        if not active.exists():
            raise RuntimeError("characterization_abort_identity_mismatch")
        operation_id = _required_state_text(
            _read_characterization_record(active, marker_secret), "operation_id"
        )
    try:
        if str(uuid.UUID(operation_id)) != operation_id:
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise RuntimeError("characterization_abort_identity_mismatch") from None

    with _exclusive_cleanup_lock(root, "characterization-root"):
        with _exclusive_cleanup_lock(root, operation_id):
            return _retire_aborted_claude_visibility_characterization_locked(
                root=root,
                marker_secret=marker_secret,
                expected_job_id=expected_job_id,
                expected_reserved_claude_uuid=expected_reserved_claude_uuid,
                operation_id=operation_id,
                now=now,
            )


def _retire_aborted_claude_visibility_characterization_locked(
    *,
    root: Path,
    marker_secret: bytes,
    expected_job_id: str,
    expected_reserved_claude_uuid: str,
    operation_id: str,
    now: Callable[[], float],
) -> dict[str, Any]:
    claims = root / ".abort-claims"
    completed = root / ".abort-completed"
    quarantine_root = root / _ABORT_QUARANTINE_DIRECTORY
    claims.mkdir(exist_ok=True)
    completed.mkdir(exist_ok=True)
    quarantine_root.mkdir(exist_ok=True)
    _require_plain_directory(claims)
    _require_plain_directory(completed)
    _require_plain_directory(quarantine_root)
    active = root / _CHARACTERIZATION_RECORD
    claimed = claims / f"{operation_id}.json"
    done = completed / f"{operation_id}.json"
    quarantined = quarantine_root / operation_id

    if done.exists():
        state = _read_characterization_record(done, marker_secret)
        _validate_aborted_characterization_identity(
            state,
            root=root,
            expected_operation_id=operation_id,
            expected_job_id=expected_job_id,
            expected_reserved_claude_uuid=expected_reserved_claude_uuid,
            marker_secret=marker_secret,
            allowed_phases={"aborted"},
        )
        if claimed.exists():
            claimed_state = _read_characterization_record(claimed, marker_secret)
            _validate_aborted_characterization_identity(
                claimed_state,
                root=root,
                expected_operation_id=operation_id,
                expected_job_id=expected_job_id,
                expected_reserved_claude_uuid=expected_reserved_claude_uuid,
                marker_secret=marker_secret,
                allowed_phases={
                    "reserved",
                    "launching",
                    "abort_disposable_removing",
                    "abort_disposable_removed",
                },
            )
            claimed.unlink()
        _best_effort_remove_disposable(quarantined, state)
        return _aborted_characterization_result(state)

    if not claimed.exists():
        if not active.exists():
            raise RuntimeError("characterization_abort_identity_mismatch")
        state = _read_characterization_record(active, marker_secret)
        if state.get("phase") in {"reserved", "launching"}:
            _validate_operation_state(state, root=root, now=float(now()))
        _validate_aborted_characterization_identity(
            state,
            root=root,
            expected_operation_id=operation_id,
            expected_job_id=expected_job_id,
            expected_reserved_claude_uuid=expected_reserved_claude_uuid,
            marker_secret=marker_secret,
            allowed_phases={
                "reserved",
                "launching",
                "abort_disposable_removing",
                "abort_disposable_removed",
            },
        )
        # Move the authenticated record first while it is still in a phase
        # accepted on either side. A crash before or after this atomic rename
        # therefore leaves one replayable source of truth.
        os.replace(active, claimed)

    state = _read_characterization_record(claimed, marker_secret)
    _validate_aborted_characterization_identity(
        state,
        root=root,
        expected_operation_id=operation_id,
        expected_job_id=expected_job_id,
        expected_reserved_claude_uuid=expected_reserved_claude_uuid,
        marker_secret=marker_secret,
        allowed_phases={
            "reserved",
            "launching",
            "abort_disposable_removing",
            "abort_disposable_removed",
        },
    )
    if state["phase"] in {"reserved", "launching"}:
        state["phase"] = "abort_disposable_removing"
        _write_characterization_record(claimed, state, marker_secret)
    disposable = _bound_disposable_path(state, root)
    if state["phase"] == "abort_disposable_removing":
        if disposable.exists() and quarantined.exists():
            raise RuntimeError("characterization_abort_identity_mismatch")
        if disposable.exists():
            _validate_disposable(state, root)
            # The whole authenticated disposable is quarantined atomically.
            # Recursive deletion happens only after terminal publication, so
            # a mid-delete crash can never strand the active operation.
            os.replace(disposable, quarantined)
        if not quarantined.exists():
            raise RuntimeError("characterization_abort_identity_mismatch")
        _validate_disposable_path(quarantined, state)
        state["phase"] = "abort_disposable_removed"
        _write_characterization_record(claimed, state, marker_secret)
    elif not quarantined.exists():
        raise RuntimeError("characterization_abort_identity_mismatch")
    else:
        _validate_disposable_path(quarantined, state)
    state["phase"] = "aborted"
    _write_characterization_record(done, state, marker_secret)
    claimed.unlink()
    _best_effort_remove_disposable(quarantined, state)
    return _aborted_characterization_result(state)


def _assert_no_unresolved_abort_claims(root: Path, marker_secret: bytes) -> None:
    claims = root / ".abort-claims"
    if not claims.exists():
        return
    _require_plain_directory(claims)
    paths = sorted(
        path for path in claims.iterdir() if path.is_file() and path.suffix == ".json"
    )
    if len(paths) > _ABORT_CLAIM_SCAN_LIMIT:
        raise RuntimeError("characterization_abort_record_limit")
    for path in paths:
        state = _read_characterization_record(path, marker_secret)
        operation_id = _required_state_text(state, "operation_id")
        _validate_aborted_characterization_identity(
            state,
            root=root,
            expected_operation_id=operation_id,
            expected_job_id=_required_state_text(state, "job_id"),
            expected_reserved_claude_uuid=_required_state_text(
                state, "reserved_claude_uuid"
            ),
            marker_secret=marker_secret,
            allowed_phases={
                "reserved",
                "launching",
                "abort_disposable_removing",
                "abort_disposable_removed",
            },
        )
    if paths:
        raise RuntimeError("characterization_abort_in_progress")


def _assert_no_unresolved_cleanup_claims(root: Path, marker_secret: bytes) -> None:
    claims = root / ".cleanup-claims"
    if not claims.exists():
        return
    _require_plain_directory(claims)
    paths = sorted(
        path for path in claims.iterdir() if path.is_file() and path.suffix == ".json"
    )
    if len(paths) > _CLEANUP_CLAIM_SCAN_LIMIT:
        raise RuntimeError("characterization_cleanup_record_limit")
    for path in paths:
        state = _read_characterization_record(path, marker_secret)
        operation_id = _required_state_text(state, "operation_id")
        if path.stem != operation_id:
            raise RuntimeError("characterization_cleanup_token_invalid")
        _validate_cleanup_claim_identity(
            state,
            root=root,
            expected_operation_id=operation_id,
            marker_secret=marker_secret,
            allowed_phases={
                "ready",
                "transcript_removing",
                "transcript_removed",
                "disposable_quarantining",
                "disposable_quarantined",
                "disposable_removed",
            },
        )
    if paths:
        raise RuntimeError("characterization_cleanup_in_progress")


def _validate_aborted_characterization_identity(
    state: Mapping[str, Any],
    *,
    root: Path,
    expected_operation_id: str,
    expected_job_id: str,
    expected_reserved_claude_uuid: str,
    marker_secret: bytes,
    allowed_phases: set[str],
) -> None:
    if (
        state.get("schema_version") != 2
        or state.get("operation_id") != expected_operation_id
        or state.get("phase") not in allowed_phases
        or state.get("source_provider") != Provider.CODEX.value
        or state.get("source_session_id") != f"codex:{expected_operation_id}"
        or state.get("job_id") != expected_job_id
        or state.get("reserved_claude_uuid") != expected_reserved_claude_uuid
    ):
        raise RuntimeError("characterization_abort_identity_mismatch")
    candidate = ClaudeVisibilityCandidate(
        source_session_id=_required_state_text(state, "source_session_id"),
        source_provider=Provider.CODEX,
        native_name=_required_state_text(state, "native_name"),
        source_cwd=_required_state_text(state, "source_cwd"),
        git_root=None,
        git_branch=None,
        git_head=None,
        worktree_id=None,
        eligible_at=float(_required_state_number(state, "created_at")),
    )
    identity = derive_claude_visibility_identity(candidate, marker_secret)
    if (
        identity.job_id != expected_job_id
        or identity.claude_uuid != expected_reserved_claude_uuid
        or identity.bridge_id != state.get("bridge_id")
        or identity.signed_marker != state.get("signed_marker")
    ):
        raise RuntimeError("characterization_abort_identity_mismatch")
    _bound_disposable_path(state, root)
    _validated_characterization_marker(
        identity.signed_marker,
        marker_secret,
        source_session_id=candidate.source_session_id,
        bridge_id=identity.bridge_id,
    )


def _aborted_characterization_result(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": "retired",
        "job_id": state["job_id"],
        "reserved_claude_uuid": state["reserved_claude_uuid"],
        "active_record_retired": True,
    }


def _cleanup_characterized_claude_visibility_locked(
    *,
    operation_id: str,
    capability: str,
    root: Path,
    project_root: Path,
    restarted_source: Callable[[], ClaudeReadableSource],
    marker_secret: bytes,
    timestamp: float,
) -> dict[str, Any]:
    claims = root / ".cleanup-claims"
    completed = root / ".cleanup-completed"
    quarantine_root = root / _CLEANUP_QUARANTINE_DIRECTORY
    claims.mkdir(exist_ok=True)
    completed.mkdir(exist_ok=True)
    quarantine_root.mkdir(exist_ok=True)
    _require_plain_directory(claims)
    _require_plain_directory(completed)
    _require_plain_directory(quarantine_root)
    active = root / _CHARACTERIZATION_RECORD
    claimed = claims / f"{operation_id}.json"
    done = completed / f"{operation_id}.json"
    quarantined = quarantine_root / operation_id
    if done.exists():
        state = _read_characterization_record(done, marker_secret)
        _validate_cleanup_authority(
            state,
            operation_id,
            capability,
            marker_secret,
            timestamp,
            allow_expired_claim=True,
        )
        _validate_cleanup_claim_identity(
            state,
            root=root,
            expected_operation_id=operation_id,
            marker_secret=marker_secret,
            allowed_phases={"completed"},
        )
        if claimed.exists():
            claimed_state = _read_characterization_record(claimed, marker_secret)
            _validate_cleanup_claim_identity(
                claimed_state,
                root=root,
                expected_operation_id=operation_id,
                marker_secret=marker_secret,
                allowed_phases={
                    "ready",
                    "transcript_removing",
                    "transcript_removed",
                    "disposable_quarantining",
                    "disposable_quarantined",
                    "disposable_removed",
                },
            )
            claimed.unlink()
        _best_effort_remove_disposable(quarantined, state)
        return _cleanup_result(state)
    if not claimed.exists():
        if not active.exists():
            raise RuntimeError("characterization_cleanup_token_invalid")
        state = _read_characterization_record(active, marker_secret)
        _validate_cleanup_authority(
            state, operation_id, capability, marker_secret, timestamp
        )
        state["cleanup_authorized_at"] = timestamp
        _write_characterization_record(active, state, marker_secret)
        os.replace(active, claimed)
    state = _read_characterization_record(claimed, marker_secret)
    _validate_cleanup_authority(
        state,
        operation_id,
        capability,
        marker_secret,
        timestamp,
        allow_expired_claim=True,
    )
    _validate_cleanup_claim_identity(
        state,
        root=root,
        expected_operation_id=operation_id,
        marker_secret=marker_secret,
        allowed_phases={
            "ready",
            "transcript_removing",
            "transcript_removed",
            "disposable_quarantining",
            "disposable_quarantined",
            "disposable_removed",
        },
    )
    disposable = _bound_disposable_path(state, root)
    if state["phase"] not in {
        "disposable_quarantining",
        "disposable_quarantined",
        "disposable_removed",
        "completed",
    }:
        _validate_disposable(state, root)
    _validated_characterization_marker(
        _required_state_text(state, "signed_marker"),
        marker_secret,
        source_session_id=_required_state_text(state, "source_session_id"),
        bridge_id=_required_state_text(state, "bridge_id"),
    )
    if state["phase"] == "ready":
        transcript = _validate_characterization_transcript(
            restarted=restarted_source(),
            projects_root=project_root,
            reserved_uuid=_required_state_text(state, "reserved_claude_uuid"),
            native_name=_required_state_text(state, "native_name"),
            source_cwd=_required_state_text(state, "source_cwd"),
            signed_marker=_required_state_text(state, "signed_marker"),
            marker_secret=marker_secret,
            allow_recovered=True,
            allow_post_ready_continuations=True,
        )
        if transcript != _safe_contained_file(
            project_root, _required_state_text(state, "transcript_path")
        ):
            raise RuntimeError("characterization_identity_mismatch:path_changed")
        if _recorded_object_identity(
            state.get("transcript_identity")
        ) != _object_identity(transcript):
            raise RuntimeError("characterization_identity_mismatch:path_changed")
        state["transcript_identity"] = list(_path_identity(transcript))
        state["phase"] = "transcript_removing"
        _write_characterization_record(claimed, state, marker_secret)
    if state["phase"] == "transcript_removing":
        transcript = _absolute_without_resolving(
            _required_state_text(state, "transcript_path")
        )
        try:
            transcript.relative_to(project_root)
        except ValueError:
            raise RuntimeError(
                "characterization_identity_mismatch:path_changed"
            ) from None
        quarantine = transcript.with_name(f".{transcript.name}.{operation_id}.cleanup")
        expected_identity = tuple(state.get("transcript_identity") or ())
        if transcript.exists():
            _safe_contained_file(project_root, transcript)
            if _path_identity(transcript) != expected_identity:
                raise RuntimeError("characterization_identity_mismatch:path_changed")
            if quarantine.exists() or _path_is_redirect(quarantine):
                raise RuntimeError("characterization_identity_mismatch:path_changed")
            os.replace(transcript, quarantine)
        if quarantine.exists():
            _safe_contained_file(project_root, quarantine)
            if _path_identity(quarantine) != expected_identity:
                raise RuntimeError("characterization_identity_mismatch:path_changed")
            quarantine.unlink()
        state["phase"] = "transcript_removed"
        _write_characterization_record(claimed, state, marker_secret)
    elif state["phase"] not in {
        "transcript_removed",
        "disposable_quarantining",
        "disposable_quarantined",
        "disposable_removed",
    }:
        raise RuntimeError("characterization_cleanup_token_invalid")
    if state["phase"] == "transcript_removed":
        state["phase"] = "disposable_quarantining"
        _write_characterization_record(claimed, state, marker_secret)
    if state["phase"] == "disposable_quarantining":
        if disposable.exists() and quarantined.exists():
            raise RuntimeError("characterization_identity_mismatch:disposable")
        if disposable.exists():
            _validate_disposable(state, root)
            os.replace(disposable, quarantined)
        if not quarantined.exists():
            raise RuntimeError("characterization_identity_mismatch:disposable")
        _validate_disposable_path(quarantined, state)
        state["phase"] = "disposable_quarantined"
        _write_characterization_record(claimed, state, marker_secret)
    elif state["phase"] == "disposable_quarantined":
        if not quarantined.exists():
            raise RuntimeError("characterization_identity_mismatch:disposable")
        _validate_disposable_path(quarantined, state)
    elif state["phase"] != "disposable_removed":
        raise RuntimeError("characterization_cleanup_token_invalid")
    state["phase"] = "completed"
    _write_characterization_record(done, state, marker_secret)
    claimed.unlink()
    _best_effort_remove_disposable(quarantined, state)
    return _cleanup_result(state)


def _cleanup_result(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "passed": True,
        "reserved_claude_uuid": state["reserved_claude_uuid"],
        "restart_exact_id_verified": True,
        "verification": "operator_confirmed",
        "cleanup": "removed_exact_characterization",
    }


@contextmanager
def _exclusive_cleanup_lock(root: Path, operation_id: str) -> Any:
    """Serialize one cleanup operation with an OS-released crash-safe lock."""

    locks = root / ".cleanup-locks"
    locks.mkdir(exist_ok=True)
    _require_plain_directory(locks)
    lock_path = locks / f"{operation_id}.lock"
    if _path_is_redirect(lock_path):
        raise RuntimeError("unsafe_characterization_root")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        current = os.lstat(lock_path)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or _metadata_is_redirect(current)
            or _identity_tuple(metadata) != _identity_tuple(current)
        ):
            raise RuntimeError("unsafe_characterization_root")
        if metadata.st_size == 0:
            os.write(descriptor, b"0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _validate_characterization_transcript(
    *,
    restarted: ClaudeReadableSource,
    projects_root: Path,
    reserved_uuid: str,
    native_name: str,
    source_cwd: str,
    signed_marker: str,
    marker_secret: bytes,
    allow_recovered: bool = False,
    allow_post_ready_continuations: bool = False,
) -> Path:
    finder = getattr(restarted, "find_native_sessions", None)
    paths = (
        list(finder(reserved_uuid))
        if callable(finder)
        else [
            found
            for found in [restarted.find_native_session(reserved_uuid)]
            if found is not None
        ]
    )
    if len(paths) != 1:
        raise RuntimeError("characterization_identity_mismatch:exact_uuid")
    transcript = _safe_contained_file(projects_root, paths[0])
    if transcript.name != f"{reserved_uuid}.jsonl":
        raise RuntimeError("characterization_identity_mismatch:path")
    before = _path_identity(transcript)
    parsed = restarted.parse(transcript)
    native = parsed.projection
    try:
        projected_path = _safe_contained_file(projects_root, native.native_path or "")
    except (OSError, RuntimeError):
        raise RuntimeError("characterization_identity_mismatch:path") from None
    marker_check = getattr(restarted, "projection_has_exact_marker", None)
    exact_marker = callable(marker_check) and marker_check(native, signed_marker)
    marker_payload = _validated_characterization_marker(
        signed_marker, marker_secret, source_session_id=None
    )
    if (
        native.provider is not Provider.CLAUDE
        or native.native_id != reserved_uuid
        or native.title != native_name
        or native.cwd != source_cwd
        or projected_path != transcript
        or not exact_marker
        or marker_payload.target_provider is not Provider.CLAUDE
    ):
        raise RuntimeError("characterization_identity_mismatch:metadata")
    recovery_prompt = build_characterization_auth_recovery_prompt(
        reserved_uuid, signed_marker
    )
    candidate = ClaudeVisibilityCandidate(
        source_session_id=marker_payload.source_session_id,
        source_provider=Provider.CODEX,
        native_name=native_name,
        source_cwd=source_cwd,
        git_root=None,
        git_branch=None,
        git_head=None,
        worktree_id=None,
        eligible_at=0.0,
    )
    identity = derive_claude_visibility_identity(candidate, marker_secret)
    if identity.signed_marker != signed_marker:
        raise RuntimeError("characterization_identity_mismatch:marker")
    expected_prompt = build_claude_registration_prompt(
        candidate, identity, marker_secret
    )
    messages = list(native.messages)
    exact_structure = not any(
        message.ordinal != 0
        or not isinstance(message.native_event_id, str)
        or not message.native_event_id
        or message.tool_name is not None
        or message.tool_calls is not None
        or message.tool_call_id is not None
        or message.reasoning is not None
        for message in messages
    ) and len({message.native_event_id for message in messages}) == len(messages)
    normal = (
        len(messages) == 2
        and exact_structure
        and messages[0].role == "user"
        and messages[0].content == expected_prompt
        and messages[1].role == "assistant"
        and _is_exact_registered_text(messages[1].content)
    )
    recovery_kind = _classify_exact_auth_recovery_messages(
        messages, expected_prompt, recovery_prompt
    )
    auth_failure = recovery_kind == "auth_pending"
    recovered = recovery_kind == "recovered"
    post_ready_continuation = (
        allow_post_ready_continuations
        and allow_recovered
        and exact_structure
        and _has_exact_post_ready_continuations(
            messages, expected_prompt, recovery_prompt
        )
    )
    if auth_failure:
        first_response = messages[1]
        assert isinstance(first_response.content, str)
        if _path_identity(transcript) != before:
            raise RuntimeError("characterization_identity_mismatch:path_changed")
        raise CharacterizationAuthenticationFailure(
            hashlib.sha256(first_response.content.encode("utf-8")).hexdigest()
        )
    if not normal and not recovered and not post_ready_continuation:
        raise RuntimeError("characterization_identity_mismatch:response")
    if _path_identity(transcript) != before:
        raise RuntimeError("characterization_identity_mismatch:path_changed")
    if recovered and not allow_recovered:
        first_response = messages[1]
        assert isinstance(first_response.content, str)
        transcript_digest = _sha256_file(transcript)
        if _path_identity(transcript) != before:
            raise RuntimeError("characterization_identity_mismatch:path_changed")
        raise CharacterizationRecoveredTranscript(
            hashlib.sha256(first_response.content.encode("utf-8")).hexdigest(),
            hashlib.sha256(recovery_prompt.encode("utf-8")).hexdigest(),
            transcript_digest,
        )
    return transcript


def _has_exact_post_ready_continuations(
    messages: Sequence[Any], expected_prompt: str, recovery_prompt: str
) -> bool:
    """Accept complete strict operator turns only after a valid ready prefix."""

    for prefix_length in range(2, len(messages), 1):
        prefix = list(messages[:prefix_length])
        normal_prefix = (
            len(prefix) == 2
            and prefix[0].role == "user"
            and prefix[0].content == expected_prompt
            and prefix[1].role == "assistant"
            and _is_exact_registered_text(prefix[1].content)
        )
        recovered_prefix = (
            _classify_exact_auth_recovery_messages(
                prefix, expected_prompt, recovery_prompt
            )
            == "recovered"
        )
        suffix = list(messages[prefix_length:])
        if (
            not (normal_prefix or recovered_prefix)
            or len(suffix) < 2
            or len(suffix) % 2 != 0
        ):
            continue
        if all(
            suffix[index].role == "user"
            and isinstance(suffix[index].content, str)
            and bool(suffix[index].content)
            and suffix[index + 1].role == "assistant"
            and isinstance(suffix[index + 1].content, str)
            and bool(suffix[index + 1].content)
            for index in range(0, len(suffix), 2)
        ):
            return True
    return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_secret(secret: bytes) -> None:
    if not isinstance(secret, bytes) or not secret:
        raise ValueError("marker_secret must be nonempty bytes")


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _record_signature(payload: Mapping[str, Any], secret: bytes) -> str:
    return (
        base64
        .urlsafe_b64encode(
            hmac.new(secret, _canonical_json(payload), hashlib.sha256).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )


def _write_characterization_record(
    path: Path, payload: dict[str, Any], secret: bytes
) -> None:
    _require_secret(secret)
    parent = Path(path).parent
    _require_plain_directory(parent)
    if path.exists() and _path_is_redirect(path):
        raise RuntimeError("unsafe_characterization_record")
    envelope = {"payload": payload, "signature": _record_signature(payload, secret)}
    data = _canonical_json(envelope)
    temporary = parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_characterization_record(path: Path, secret: bytes) -> dict[str, Any]:
    try:
        if _path_is_redirect(path):
            raise RuntimeError("characterization_cleanup_token_invalid")
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 65_536:
            raise RuntimeError("characterization_cleanup_token_invalid")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            data = os.read(descriptor, opened.st_size + 1)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        current = os.lstat(path)
        if (
            len(data) != opened.st_size
            or _identity_tuple(before) != _identity_tuple(opened)
            or _identity_tuple(after) != _identity_tuple(opened)
            or _identity_tuple(current) != _identity_tuple(opened)
        ):
            raise RuntimeError("characterization_cleanup_token_invalid")
        envelope = json.loads(
            data.decode("utf-8"), object_pairs_hook=_unique_json_object
        )
    except (OSError, UnicodeError, json.JSONDecodeError, _DuplicateJsonKey):
        raise RuntimeError("characterization_cleanup_token_invalid") from None
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "signature"}:
        raise RuntimeError("characterization_cleanup_token_invalid")
    payload = envelope["payload"]
    signature = envelope["signature"]
    if (
        not isinstance(payload, dict)
        or not isinstance(signature, str)
        or not hmac.compare_digest(signature, _record_signature(payload, secret))
    ):
        raise RuntimeError("characterization_cleanup_token_invalid")
    return payload


def _cleanup_capability(secret: bytes, operation_id: str, expires_at: float) -> str:
    digest = hmac.new(
        secret,
        f"claude-characterization-cleanup:{operation_id}:{expires_at:.6f}".encode(),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _cleanup_capability_hash(
    secret: bytes, operation_id: str, expires_at: float
) -> str:
    return hashlib.sha256(
        _cleanup_capability(secret, operation_id, expires_at).encode()
    ).hexdigest()


def _parse_cleanup_token(token: Mapping[str, Any]) -> tuple[str, str]:
    if not isinstance(token, Mapping) or set(token) != {"id", "capability"}:
        raise RuntimeError("characterization_cleanup_token_invalid")
    try:
        operation_id = str(uuid.UUID(token["id"]))
    except (TypeError, ValueError, AttributeError):
        raise RuntimeError("characterization_cleanup_token_invalid") from None
    capability = token["capability"]
    if not isinstance(capability, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{43}", capability
    ):
        raise RuntimeError("characterization_cleanup_token_invalid")
    return operation_id, capability


def _validate_cleanup_authority(
    state: Mapping[str, Any],
    operation_id: str,
    capability: str,
    secret: bytes,
    now: float,
    *,
    allow_expired_claim: bool = False,
) -> None:
    if state.get("operation_id") != operation_id:
        raise RuntimeError("characterization_cleanup_token_invalid")
    expiry = state.get("expires_at")
    if (
        not isinstance(expiry, (int, float))
        or isinstance(expiry, bool)
        or not math.isfinite(expiry)
    ):
        raise RuntimeError("characterization_cleanup_token_expired")
    expected = _cleanup_capability(secret, operation_id, float(expiry))
    if (
        not hmac.compare_digest(capability, expected)
        or state.get("cleanup_capability_hash")
        != hashlib.sha256(capability.encode()).hexdigest()
    ):
        raise RuntimeError("characterization_cleanup_token_invalid")
    authorized_at = state.get("cleanup_authorized_at")
    durable_claim = (
        allow_expired_claim
        and isinstance(authorized_at, (int, float))
        and not isinstance(authorized_at, bool)
        and math.isfinite(authorized_at)
        and authorized_at <= expiry
    )
    if now > expiry and not durable_claim:
        raise RuntimeError("characterization_cleanup_token_expired")


def _validate_cleanup_claim_identity(
    state: Mapping[str, Any],
    *,
    root: Path,
    expected_operation_id: str,
    marker_secret: bytes,
    allowed_phases: set[str],
) -> None:
    if (
        state.get("schema_version") != 2
        or state.get("operation_id") != expected_operation_id
        or state.get("phase") not in allowed_phases
        or state.get("source_provider") != Provider.CODEX.value
        or state.get("source_session_id") != f"codex:{expected_operation_id}"
    ):
        raise RuntimeError("characterization_cleanup_token_invalid")
    try:
        if str(uuid.UUID(expected_operation_id)) != expected_operation_id:
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise RuntimeError("characterization_cleanup_token_invalid") from None
    created_at = _required_state_number(state, "created_at")
    expires_at = _required_state_number(state, "expires_at")
    authorized_at = state.get("cleanup_authorized_at")
    if (
        expires_at <= created_at
        or not isinstance(authorized_at, (int, float))
        or isinstance(authorized_at, bool)
        or not math.isfinite(authorized_at)
        or float(authorized_at) > expires_at
    ):
        raise RuntimeError("characterization_cleanup_token_invalid")
    candidate = ClaudeVisibilityCandidate(
        source_session_id=_required_state_text(state, "source_session_id"),
        source_provider=Provider.CODEX,
        native_name=_required_state_text(state, "native_name"),
        source_cwd=_required_state_text(state, "source_cwd"),
        git_root=None,
        git_branch=None,
        git_head=None,
        worktree_id=None,
        eligible_at=created_at,
    )
    identity = derive_claude_visibility_identity(candidate, marker_secret)
    _validated_characterization_marker(
        _required_state_text(state, "signed_marker"),
        marker_secret,
        source_session_id=candidate.source_session_id,
        bridge_id=_required_state_text(state, "bridge_id"),
    )
    if (
        identity.job_id != state.get("job_id")
        or identity.bridge_id != state.get("bridge_id")
        or identity.claude_uuid != state.get("reserved_claude_uuid")
        or identity.signed_marker != state.get("signed_marker")
    ):
        raise RuntimeError("characterization_cleanup_token_invalid")
    _bound_disposable_path(state, root)


def _validate_operation_state(
    state: Mapping[str, Any], *, root: Path, now: float
) -> None:
    required = {
        "schema_version",
        "operation_id",
        "phase",
        "created_at",
        "expires_at",
        "source_provider",
        "source_session_id",
        "bridge_id",
        "job_id",
        "reserved_claude_uuid",
        "native_name",
        "source_cwd",
        "signed_marker",
        "transcript_path",
        "transcript_identity",
        "sentinel_nonce",
        "cleanup_authorized_at",
        "cleanup_capability_hash",
    }
    if set(state) != required or state.get("schema_version") != 2:
        raise RuntimeError("characterization_cleanup_token_invalid")
    try:
        if str(uuid.UUID(state["operation_id"])) != state["operation_id"]:
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise RuntimeError("characterization_cleanup_token_invalid") from None
    if state.get("source_provider") != Provider.CODEX.value:
        raise RuntimeError("characterization_cleanup_token_invalid")
    created_at = _required_state_number(state, "created_at")
    expires_at = _required_state_number(state, "expires_at")
    if expires_at <= created_at:
        raise RuntimeError("characterization_cleanup_token_invalid")
    authorized_at = state.get("cleanup_authorized_at")
    if authorized_at is not None and (
        not isinstance(authorized_at, (int, float))
        or isinstance(authorized_at, bool)
        or not math.isfinite(authorized_at)
        or float(authorized_at) > expires_at
    ):
        raise RuntimeError("characterization_cleanup_token_invalid")
    if state.get("phase") not in {
        "prepared",
        "reserved",
        "launching",
        "launched",
        "ready",
        "transcript_removing",
        "transcript_removed",
        "disposable_removed",
        "completed",
    }:
        raise RuntimeError("characterization_cleanup_token_invalid")
    _validate_disposable(state, root)


def _validated_characterization_marker(
    marker: str,
    secret: bytes,
    *,
    source_session_id: str | None,
    bridge_id: str | None = None,
) -> BridgeMarkerPayload:
    try:
        payload = decode_bridge_marker(marker, secret)
    except InvalidBridgeMarker:
        raise RuntimeError("characterization_identity_mismatch:marker") from None
    if (
        payload.target_provider is not Provider.CLAUDE
        or (
            source_session_id is not None
            and payload.source_session_id != source_session_id
        )
        or (bridge_id is not None and payload.bridge_id != bridge_id)
    ):
        raise RuntimeError("characterization_identity_mismatch:marker")
    return payload


def _required_state_text(state: Mapping[str, Any], key: str) -> str:
    value = state.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError("characterization_cleanup_token_invalid")
    return value


def _required_state_number(state: Mapping[str, Any], key: str) -> float:
    value = state.get(key)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
    ):
        raise RuntimeError("characterization_cleanup_token_invalid")
    return float(value)


def _absolute_without_resolving(path: Path | str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _prepare_safe_root(path: Path, *, create: bool) -> Path:
    root = _absolute_without_resolving(path)
    _require_plain_existing_ancestry(root)
    if create:
        root.mkdir(parents=True, exist_ok=True)
    _require_plain_existing_ancestry(root)
    _require_plain_directory(root)
    return root


def _require_plain_existing_ancestry(path: Path) -> None:
    parts = path.parts
    if not parts:
        raise RuntimeError("unsafe_characterization_root")
    current = Path(parts[0])
    for part in parts[1:]:
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            continue
        except OSError:
            raise RuntimeError("unsafe_characterization_root") from None
        if not stat.S_ISDIR(metadata.st_mode) or _metadata_is_redirect(metadata):
            raise RuntimeError("unsafe_characterization_root")


def _require_plain_directory(path: Path) -> None:
    try:
        metadata = os.lstat(path)
    except OSError:
        raise RuntimeError("unsafe_characterization_root") from None
    if not stat.S_ISDIR(metadata.st_mode) or _metadata_is_redirect(metadata):
        raise RuntimeError("unsafe_characterization_root")


def _metadata_is_redirect(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _write_identity_sentinel(directory: Path, operation_id: str, nonce: str) -> None:
    sentinel = directory / _CHARACTERIZATION_SENTINEL
    data = _canonical_json({"operation_id": operation_id, "nonce": nonce})
    descriptor = os.open(sentinel, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_disposable(state: Mapping[str, Any], root: Path) -> Path:
    disposable = _bound_disposable_path(state, root)
    _validate_disposable_path(disposable, state)
    return disposable


def _validate_disposable_path(disposable: Path, state: Mapping[str, Any]) -> None:
    _require_plain_directory(disposable)
    sentinel = disposable / _CHARACTERIZATION_SENTINEL
    if _path_is_redirect(sentinel):
        raise RuntimeError("characterization_identity_mismatch:sentinel")
    try:
        value = json.loads(sentinel.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RuntimeError("characterization_identity_mismatch:sentinel") from None
    if value != {
        "operation_id": state.get("operation_id"),
        "nonce": state.get("sentinel_nonce"),
    }:
        raise RuntimeError("characterization_identity_mismatch:sentinel")


def _bound_disposable_path(state: Mapping[str, Any], root: Path) -> Path:
    disposable = _absolute_without_resolving(_required_state_text(state, "source_cwd"))
    if disposable.parent != root or not disposable.name.startswith(
        "claude-visibility-"
    ):
        raise RuntimeError("characterization_identity_mismatch:disposable")
    return disposable


def _safe_contained_file(root: Path, path: Path | str) -> Path:
    candidate = _absolute_without_resolving(path)
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        raise RuntimeError("characterization_identity_mismatch:path") from None
    current = root
    for part in relative.parts:
        current = current / part
        try:
            metadata = os.lstat(current)
        except OSError:
            raise RuntimeError("characterization_identity_mismatch:path") from None
        if _metadata_is_redirect(metadata):
            raise RuntimeError("characterization_identity_mismatch:path")
    if not stat.S_ISREG(os.lstat(candidate).st_mode):
        raise RuntimeError("characterization_identity_mismatch:path")
    return candidate


def _identity_tuple(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)


def _path_identity(path: Path) -> tuple[int, int, int, int]:
    return _identity_tuple(os.lstat(path))


def _object_identity(path: Path) -> tuple[int, int]:
    metadata = os.lstat(path)
    return (metadata.st_dev, metadata.st_ino)


def _recorded_object_identity(value: Any) -> tuple[int, int]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or any(not isinstance(item, int) or isinstance(item, bool) for item in value)
    ):
        raise RuntimeError("characterization_identity_mismatch:path_changed")
    return (value[0], value[1])


def _safe_remove_disposable(disposable: Path, state: Mapping[str, Any]) -> None:
    _validate_disposable_path(disposable, state)
    # Delete bottom-up, never following a redirect. A swapped directory causes
    # rmdir to fail rather than traversing an attacker-controlled target.
    for current_root, dirs, files in os.walk(
        disposable, topdown=False, followlinks=False
    ):
        current = Path(current_root)
        if _path_is_redirect(current):
            raise RuntimeError("characterization_identity_mismatch:disposable")
        for name in files:
            item = current / name
            metadata = os.lstat(item)
            if _metadata_is_redirect(metadata) or not stat.S_ISREG(metadata.st_mode):
                raise RuntimeError("characterization_identity_mismatch:disposable")
            item.unlink()
        for name in dirs:
            item = current / name
            metadata = os.lstat(item)
            if _metadata_is_redirect(metadata) or not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError("characterization_identity_mismatch:disposable")
            item.rmdir()
    disposable.rmdir()


def _best_effort_remove_disposable(disposable: Path, state: Mapping[str, Any]) -> None:
    if not disposable.exists():
        return
    try:
        _safe_remove_disposable(disposable, state)
    except (OSError, RuntimeError):
        # Terminal state is already durably published before this cleanup runs.
        # On identity loss or an OS error, retain the deterministic quarantine
        # for manual inspection rather than interpreting ambiguity as authority
        # to delete anything.
        return


def _characterization_message(timestamp: float) -> Any:
    from .models import ProjectedMessage

    return ProjectedMessage(
        "characterization-request",
        0,
        "user",
        "Verify native Claude session visibility and exact-ID resume metadata.",
        timestamp,
    )


_PROVIDER_NUMBER_FIELDS = frozenset({
    "create_cost_usd",
    "create_latency_ms",
    "resume_cost_usd",
    "resume_latency_ms",
    "total_cost_usd",
    "total_latency_ms",
    "observed_cost_usd",
    "duration_ms",
})
_PROVIDER_INTEGER_FIELDS = frozenset({
    "create_num_turns",
    "resume_num_turns",
    "total_num_turns",
    "num_turns",
})
_SECRET_RE = re.compile(
    r"(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{12,}|"
    r"(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{12,}|"
    r"(?i:bearer\s+)[A-Za-z0-9._~+/-]{12,})"
)


class UnsafeCharacterizationCleanup(RuntimeError):
    pass


class LiveCharacterizationError(RuntimeError):
    def __init__(self, report_path: Path, failures: list[str]) -> None:
        self.report_path = report_path
        self.failures = tuple(failures)
        super().__init__(
            "live_characterization_failed:"
            + ",".join(self.failures)
            + f"; report={report_path}"
        )


# ---------------------------------------------------------------------------
# bridge_revision -- which bridge code a characterization proof was made against
# ---------------------------------------------------------------------------
#
# A characterization report proves that the *installed CLI versions* drive *this
# bridge*.  Until now only the first half was recorded, so a report could age
# past the code it was proving without any signal.  ``bridge_revision`` records
# the second half: a digest, per provider, of the bridge modules that implement
# how the bridge drives and reads that provider.
#
# The manifest is deliberately narrower than the import closure.  ``store``,
# ``context_pack`` and the ``sidebar`` modules are the catalog/mirroring surface;
# live characterization never opens a store, builds a context pack or places a
# sidebar -- it creates a placeholder, discovers the native session file, reads
# it, resumes it and disposes of it.  They enter the closure only as type and
# helper imports, and they are the highest-churn files in the package, so
# tracking them would invalidate every proof for changes it never measured.
BRIDGE_REVISION_EXCLUDED_MODULES = frozenset({
    "context_pack",
    "sidebar",
    "sidebar_placement",
    "sidebar_reconciliation",
    "store",
})

# The adapter modules each provider's characterization actually drives.  The
# manifest below is their import closure; a test re-derives it so a new import
# cannot silently escape the digest.
BRIDGE_REVISION_ROOT_MODULES: Mapping[str, tuple[str, ...]] = {
    "claude": ("claude_adapter", "claude_registrar", "claude_visibility"),
    "codex": ("codex_adapter",),
}

# ``characterize`` is in both manifests: it is the harness, so it defines what
# the proof measured.  Its own top-level imports are not walked -- it imports
# both providers' adapters, which would collapse the per-provider split.
BRIDGE_REVISION_MODULES: Mapping[str, tuple[str, ...]] = {
    "claude": (
        "characterize",
        "claude_adapter",
        "claude_registrar",
        "claude_visibility",
        "claude_visibility_codes",
        "models",
    ),
    "codex": (
        "characterize",
        "claude_adapter",
        "codex_adapter",
        "models",
    ),
}


def current_bridge_revisions(
    *,
    package_root: Path | None = None,
    providers: Sequence[str] = ("claude", "codex"),
) -> dict[str, str]:
    """Digest the installed bridge modules that each provider's proof depends on.

    Content-addressed rather than git-derived on purpose: an editable install
    runs the working tree, so a commit id would call an uncommitted change
    unchanged.  Line endings are normalised so a checkout with different EOL
    settings is not mistaken for a behaviour change.
    """

    root = (
        Path(package_root)
        if package_root is not None
        else Path(__file__).resolve().parent
    )
    revisions: dict[str, str] = {}
    for provider in providers:
        modules = BRIDGE_REVISION_MODULES.get(provider)
        if modules is None:
            raise CharacterizationGateError(
                "invalid", "characterization_bridge_revision_unavailable"
            )
        digest = hashlib.sha256()
        for module in sorted(modules):
            try:
                payload = (root / f"{module}.py").read_bytes()
            except OSError:
                raise CharacterizationGateError(
                    "invalid", "characterization_bridge_revision_unavailable"
                ) from None
            payload = payload.replace(b"\r\n", b"\n")
            digest.update(f"{module}\n{len(payload)}\n".encode("utf-8"))
            digest.update(payload)
        revisions[provider] = f"sha256:{digest.hexdigest()}"
    return revisions


@dataclass(frozen=True)
class CharacterizationGate:
    """A passing characterization proof, per provider, for the installed CLIs."""

    report_path: Path
    characterization_id: str
    codex_registration_turn_required: bool
    provider_characterization_ids: Mapping[str, str]


@dataclass(frozen=True)
class _ValidatedGateReport:
    path: Path
    report: dict[str, Any]
    created_at: datetime
    characterization_id: str
    codex_registration_turn_required: bool
    providers: frozenset[str]
    provider_passed: Mapping[str, bool]
    versions: Mapping[str, str]
    bridge_revision: Mapping[str, str] | None


class CharacterizationGateError(RuntimeError):
    """Stable failure from the fail-closed live-characterization gate."""

    _CODES = frozenset({
        "missing",
        "invalid",
        "failed",
        "version_drift",
        "revision_drift",
    })

    def __init__(self, code: str, detail: str) -> None:
        if code not in self._CODES:
            raise ValueError("invalid characterization gate error code")
        self.code = code
        super().__init__(detail)


def resolve_characterization_gate(
    *,
    report_root: Path | None = None,
    current_versions: Mapping[str, str] | None = None,
    current_bridge_revision: Mapping[str, str] | None = None,
) -> CharacterizationGate:
    """Require each provider's newest report to pass for its installed CLI.

    Providers resolve independently, so a provider-scoped refresh leaves the
    other provider's standing proof in force rather than forcing a second live
    session.  "Newest recording the provider" is evaluated *before* the pass
    check, so a later failing run still buries an earlier passing one.

    A proof that is not the newest report on disk -- the only way scoping ever
    reuses an older one -- must additionally have been made against the
    installed bridge revision.  That closes the hole scoping would otherwise
    open: reusing a Claude proof taken against bridge code that has since
    changed in the Claude-handling path.  The newest report is deliberately
    exempt; holding it to the same rule would demand a live run per bridge
    commit, which this package's churn cannot pay for, and it would newly block
    what the pair-report gate always allowed.
    """

    root = _gate_report_root(report_root)
    _require_safe_report_root(root)
    reports = _read_validated_gate_reports(root)
    newest = max(reports, key=_gate_report_order)
    observed_versions = (
        _current_cli_versions()
        if current_versions is None
        else _validated_version_mapping(current_versions, source="current")
    )
    resolved: dict[str, _ValidatedGateReport] = {}
    for provider in ("claude", "codex"):
        candidates = [report for report in reports if provider in report.providers]
        if not candidates:
            raise CharacterizationGateError(
                "missing", "characterization_report_missing"
            )
        latest = max(candidates, key=_gate_report_order)
        if not latest.provider_passed[provider]:
            raise CharacterizationGateError("failed", "characterization_report_failed")
        if latest.versions[provider] != observed_versions[provider]:
            raise CharacterizationGateError(
                "version_drift", "characterization_version_mismatch"
            )
        if latest is not newest:
            _require_current_bridge_revision(
                latest,
                provider=provider,
                current_bridge_revision=current_bridge_revision,
            )
        resolved[provider] = latest
    winner = max(resolved.values(), key=_gate_report_order)
    return CharacterizationGate(
        report_path=winner.path,
        characterization_id=winner.characterization_id,
        codex_registration_turn_required=(
            resolved["codex"].codex_registration_turn_required
        ),
        provider_characterization_ids={
            provider: report.characterization_id
            for provider, report in sorted(resolved.items())
        },
    )


def _gate_report_order(report: _ValidatedGateReport) -> tuple[datetime, str]:
    return (report.created_at, report.characterization_id)


def _require_current_bridge_revision(
    report: _ValidatedGateReport,
    *,
    provider: str,
    current_bridge_revision: Mapping[str, str] | None,
) -> None:
    recorded = (
        None
        if report.bridge_revision is None
        else report.bridge_revision.get(provider)
    )
    installed = (
        current_bridge_revisions(providers=(provider,))
        if current_bridge_revision is None
        else current_bridge_revision
    )
    expected = installed.get(provider)
    if recorded is None or expected is None or recorded != expected:
        raise CharacterizationGateError(
            "revision_drift", "characterization_bridge_revision_mismatch"
        )


def _gate_report_root(report_root: Path | None) -> Path:
    return (
        Path(
            report_root
            if report_root is not None
            else characterization_store_root()
        )
        .expanduser()
        .absolute()
    )


def _newest_validated_gate_report(root: Path) -> _ValidatedGateReport:
    _require_safe_report_root(root)
    return max(
        _read_validated_gate_reports(root),
        key=lambda candidate: (candidate.created_at, candidate.characterization_id),
    )


def describe_characterization_gate(
    *,
    report_root: Path | None = None,
    current_versions: Mapping[str, str] | None = None,
    current_bridge_revision: Mapping[str, str] | None = None,
) -> tuple[int, str]:
    """Resolve the gate and describe the outcome for an installer.

    Returns ``(exit_code, message)``.  A passing gate yields ``(0, id)``, matching
    what installers previously printed.  A rejection yields a non-zero code and an
    operator-readable reason that names *which* provider drifted: the gate compares
    the whole ``versions`` map at once, so a one-provider bump is indistinguishable
    from both drifting unless the failure says so.

    The gate itself is unchanged -- this only describes its verdict.
    """

    root = _gate_report_root(report_root)
    try:
        gate = resolve_characterization_gate(
            report_root=root,
            current_versions=current_versions,
            current_bridge_revision=current_bridge_revision,
        )
    except CharacterizationGateError as exc:
        return 1, _describe_gate_rejection(
            exc,
            root=root,
            current_versions=current_versions,
            current_bridge_revision=current_bridge_revision,
        )
    return 0, gate.characterization_id


def _describe_gate_rejection(
    exc: CharacterizationGateError,
    *,
    root: Path,
    current_versions: Mapping[str, str] | None,
    current_bridge_revision: Mapping[str, str] | None = None,
) -> str:
    lines = [f"characterization gate rejected: {exc.code} ({exc})"]
    refresh_provider = "all"
    if exc.code == "version_drift":
        try:
            drift_lines, drifted = _describe_version_drift(root, current_versions)
            lines.extend(drift_lines)
            # Only one CLI moved -- offer the one-session refresh, but only if
            # it would actually clear the gate.  A scoped refresh demotes the
            # current report, so the other provider's half of it then faces the
            # bridge-revision check; if that half could not survive it, the
            # operator would pay a session and still be sent to a full refresh.
            if len(drifted) == 1 and _scoped_refresh_would_suffice(
                root,
                refreshed=drifted[0],
                current_bridge_revision=current_bridge_revision,
            ):
                refresh_provider = drifted[0]
        except Exception:
            # A diagnostic must never mask the rejection it is describing.
            pass
    if exc.code == "revision_drift":
        try:
            lines.extend(_describe_revision_drift(root, current_bridge_revision))
        except Exception:
            pass
    if exc.code in ("missing", "failed", "version_drift", "revision_drift"):
        lines.append(
            "refresh with: uv run --no-sync hermes-session-bridge characterize "
            f"--provider {refresh_provider}"
        )
        lines.append(
            "note: a refresh creates and then disposes one real session per "
            "provider -- confirm before running it"
        )
    return "\n".join(lines)


def _scoped_refresh_would_suffice(
    root: Path,
    *,
    refreshed: str,
    current_bridge_revision: Mapping[str, str] | None,
) -> bool:
    reports = _gate_provider_reports(root)
    for provider in ("claude", "codex"):
        if provider == refreshed:
            continue
        report = reports.get(provider)
        if report is None:
            return False
        recorded = (
            None
            if report.bridge_revision is None
            else report.bridge_revision.get(provider)
        )
        installed = (
            current_bridge_revisions(providers=(provider,))
            if current_bridge_revision is None
            else current_bridge_revision
        ).get(provider)
        if recorded is None or installed is None or recorded != installed:
            return False
    return True


def _describe_version_drift(
    root: Path,
    current_versions: Mapping[str, str] | None,
) -> tuple[list[str], list[str]]:
    """Per-provider version comparison plus the providers that actually drifted."""

    expected = _expected_gate_versions(root)
    try:
        observed = (
            _current_cli_versions()
            if current_versions is None
            else _validated_version_mapping(current_versions, source="current")
        )
    except CharacterizationGateError:
        observed = None
    if expected is None or observed is None:
        return [], []
    lines: list[str] = []
    drifted: list[str] = []
    for provider in ("claude", "codex"):
        report_version = expected.get(provider)
        installed_version = observed[provider]
        if report_version is None:
            lines.append(f"  {provider}: no report records this provider")
            drifted.append(provider)
        elif report_version == installed_version:
            lines.append(f"  {provider}: unchanged at {report_version!r}")
        else:
            lines.append(
                f"  {provider}: report {report_version!r} "
                f"!= installed {installed_version!r}"
            )
            drifted.append(provider)
    return lines, drifted


def _describe_revision_drift(
    root: Path,
    current_bridge_revision: Mapping[str, str] | None,
) -> list[str]:
    """Name the providers whose standing proof predates the installed bridge."""

    reports = _gate_provider_reports(root)
    newest = _newest_validated_gate_report(root)
    lines: list[str] = []
    for provider in ("claude", "codex"):
        report = reports.get(provider)
        if report is None or report is newest:
            continue
        recorded = (
            None
            if report.bridge_revision is None
            else report.bridge_revision.get(provider)
        )
        installed = (
            current_bridge_revisions(providers=(provider,))
            if current_bridge_revision is None
            else current_bridge_revision
        ).get(provider)
        if recorded == installed and recorded is not None:
            continue
        source = "not recorded" if recorded is None else recorded
        lines.append(
            f"  {provider}: proof {report.characterization_id} was made against "
            f"bridge revision {source}, installed is {installed}"
        )
    if lines:
        lines.insert(
            0,
            "a standing proof predates the installed bridge code; scoped reuse "
            "is refused",
        )
    return lines


def _gate_provider_reports(root: Path) -> dict[str, _ValidatedGateReport]:
    """The newest report recording each provider, for diagnostics."""

    reports = _read_validated_gate_reports(root)
    resolved: dict[str, _ValidatedGateReport] = {}
    for provider in ("claude", "codex"):
        candidates = [report for report in reports if provider in report.providers]
        if candidates:
            resolved[provider] = max(candidates, key=_gate_report_order)
    return resolved


def _expected_gate_versions(root: Path) -> dict[str, str] | None:
    """Best-effort report versions for a drift diagnostic.

    Each provider is described from the report that would actually satisfy it,
    which under provider scoping need not be the same report.
    """

    return {
        provider: report.versions[provider]
        for provider, report in _gate_provider_reports(root).items()
    }


def _require_safe_report_root(root: Path) -> None:
    for candidate in (root, *root.parents):
        if _path_is_redirect(candidate):
            raise CharacterizationGateError("invalid", "characterization_report_unsafe")
    try:
        metadata = os.lstat(root)
    except FileNotFoundError:
        raise CharacterizationGateError(
            "missing", "characterization_report_missing"
        ) from None
    except OSError:
        raise CharacterizationGateError(
            "invalid", "characterization_report_unsafe"
        ) from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise CharacterizationGateError("invalid", "characterization_report_unsafe")


def _read_validated_gate_reports(root: Path) -> list[_ValidatedGateReport]:
    try:
        paths = sorted(
            (path for path in root.iterdir() if path.suffix == ".json"),
            key=lambda path: path.name,
        )
    except OSError:
        raise CharacterizationGateError(
            "invalid", "characterization_report_unsafe"
        ) from None
    if not paths:
        raise CharacterizationGateError("missing", "characterization_report_missing")
    return [
        _validate_gate_report(_read_report_safely(path), report_path=path)
        for path in paths
    ]


def load_codex_characterization_origins(
    *,
    report_root: Path | None = None,
    marker_secret: bytes | None = None,
) -> dict[str, str]:
    """Return exact Codex native IDs created by trusted characterization reports.

    Characterization uses a short-lived marker key, so its native Codex threads
    cannot be authenticated later with the production marker key.  The safely
    read, schema-validated report is therefore the durable provenance authority.
    Every valid report counts, including failed characterizations: once a report
    records an exact native ID, that ID must never be treated as native user work.
    """

    root = (
        Path(
            report_root
            if report_root is not None
            else characterization_store_root()
        )
        .expanduser()
        .absolute()
    )
    try:
        _require_safe_report_root(root)
    except CharacterizationGateError as exc:
        if exc.code == "missing":
            return {}
        raise
    try:
        reports = _read_validated_gate_reports(root)
    except CharacterizationGateError as exc:
        if exc.code == "missing":
            reports = []
        else:
            raise

    origins: dict[str, str] = {}
    reports_by_id: dict[str, _ValidatedGateReport] = {}
    for report in reports:
        reports_by_id[report.characterization_id] = report
        provider = report.report["providers"].get("codex")
        if provider is None:
            # A Claude-only refresh records no Codex thread, so it contributes
            # nothing to the ledger -- but it is still a valid report.
            continue
        native_id = provider.get("native_id")
        if native_id is None:
            continue
        bridge_id = f"characterization-{report.characterization_id}-codex"
        prior = origins.get(native_id)
        if prior is not None and prior != bridge_id:
            raise CharacterizationGateError(
                "invalid", "characterization_native_identity_conflict"
            )
        origins[native_id] = bridge_id

    for guard in _read_codex_origin_guards(root, marker_secret=marker_secret):
        characterization_id = guard["characterization_id"]
        native_id = guard["native_id"]
        report = reports_by_id.get(characterization_id)
        if report is None:
            raise CharacterizationGateError(
                "failed", "characterization_codex_origin_unresolved"
            )
        if report is not None:
            codex_status = report.report["providers"].get("codex")
            if codex_status is None:
                # A guard without a Codex block in its own report is
                # unexplainable provenance: fail closed.
                raise CharacterizationGateError(
                    "failed", "characterization_codex_origin_unresolved"
                )
            reported_native_id = codex_status.get("native_id")
            if (
                native_id is not None
                and reported_native_id is not None
                and native_id != reported_native_id
            ):
                raise CharacterizationGateError(
                    "invalid", "characterization_native_identity_conflict"
                )
            if native_id is not None and reported_native_id is None:
                raise CharacterizationGateError(
                    "invalid", "characterization_native_identity_conflict"
                )
            if native_id is None and reported_native_id is None:
                raise CharacterizationGateError(
                    "failed", "characterization_codex_origin_unresolved"
                )
            continue
    return origins


def _read_codex_origin_guards(
    report_root: Path,
    *,
    marker_secret: bytes | None,
) -> list[dict[str, Any]]:
    guard_root = report_root / _CODEX_ORIGIN_GUARD_DIRECTORY
    if _path_is_redirect(guard_root):
        raise CharacterizationGateError(
            "invalid", "characterization_codex_origin_guard_invalid"
        )
    if not guard_root.exists():
        return []
    if marker_secret is None:
        raise CharacterizationGateError(
            "invalid", "characterization_codex_origin_key_unavailable"
        )
    try:
        _require_secret(marker_secret)
        _require_safe_report_root(guard_root)
        paths = sorted(
            (path for path in guard_root.iterdir() if path.suffix == ".json"),
            key=lambda path: path.name,
        )
    except CharacterizationGateError:
        raise
    except (OSError, ValueError):
        raise CharacterizationGateError(
            "invalid", "characterization_codex_origin_guard_invalid"
        ) from None
    if len(paths) > _CODEX_ORIGIN_GUARD_LIMIT:
        raise CharacterizationGateError(
            "invalid", "characterization_codex_origin_guard_invalid"
        )
    guards: list[dict[str, Any]] = []
    for path in paths:
        try:
            payload = _read_characterization_record(path, marker_secret)
            guards.append(_validate_codex_origin_guard(payload, path=path))
        except CharacterizationGateError:
            raise
        except (RuntimeError, TypeError, ValueError):
            raise CharacterizationGateError(
                "invalid", "characterization_codex_origin_guard_invalid"
            ) from None
    return guards


def _validate_codex_origin_guard(
    payload: Mapping[str, Any], *, path: Path
) -> dict[str, Any]:
    if set(payload) != {
        "schema_version",
        "characterization_id",
        "bridge_id",
        "phase",
        "native_id",
    } or payload.get("schema_version") != 1:
        raise CharacterizationGateError(
            "invalid", "characterization_codex_origin_guard_invalid"
        )
    try:
        characterization_id = _canonical_uuid(payload["characterization_id"])
    except ValueError:
        raise CharacterizationGateError(
            "invalid", "characterization_codex_origin_guard_invalid"
        ) from None
    bridge_id = f"characterization-{characterization_id}-codex"
    native_id = payload["native_id"]
    phase = payload["phase"]
    if (
        path.stem != characterization_id
        or payload["bridge_id"] != bridge_id
        or phase not in {"creating", "created"}
        or (phase == "creating" and native_id is not None)
    ):
        raise CharacterizationGateError(
            "invalid", "characterization_codex_origin_guard_invalid"
        )
    if native_id is not None:
        try:
            native_id = _canonical_uuid(native_id)
        except ValueError:
            raise CharacterizationGateError(
                "invalid", "characterization_codex_origin_guard_invalid"
            ) from None
        if phase != "created":
            raise CharacterizationGateError(
                "invalid", "characterization_codex_origin_guard_invalid"
            )
    return {
        "schema_version": 1,
        "characterization_id": characterization_id,
        "bridge_id": bridge_id,
        "phase": phase,
        "native_id": native_id,
    }


def _prepare_codex_origin_guard(
    report_root: Path,
    *,
    characterization_id: str,
    marker_secret: bytes,
) -> Path:
    _require_secret(marker_secret)
    record_id = _canonical_uuid(characterization_id)
    root = _safe_directory_root(
        Path(report_root).expanduser(),
        error_code="unsafe_characterization_report",
    )
    guard_root = _safe_directory_root(
        root / _CODEX_ORIGIN_GUARD_DIRECTORY,
        error_code="unsafe_characterization_origin_guard",
    )
    path = guard_root / f"{record_id}.json"
    if path.exists() or _path_is_redirect(path):
        raise RuntimeError("unsafe_characterization_origin_guard:already_exists")
    _write_characterization_record(
        path,
        {
            "schema_version": 1,
            "characterization_id": record_id,
            "bridge_id": f"characterization-{record_id}-codex",
            "phase": "creating",
            "native_id": None,
        },
        marker_secret,
    )
    return path


def _bind_codex_origin_guard(
    path: Path,
    *,
    native_id: str,
    marker_secret: bytes,
) -> None:
    expected_id = _canonical_uuid(native_id)
    try:
        current = _validate_codex_origin_guard(
            _read_characterization_record(path, marker_secret),
            path=path,
        )
    except (CharacterizationGateError, RuntimeError):
        raise RuntimeError("unsafe_characterization_origin_guard:invalid") from None
    prior = current["native_id"]
    if prior is not None and prior != expected_id:
        raise RuntimeError("unsafe_characterization_origin_guard:identity_conflict")
    current["phase"] = "created"
    current["native_id"] = expected_id
    _write_characterization_record(path, current, marker_secret)


def _retire_codex_origin_guard(
    path: Path,
    *,
    marker_secret: bytes,
    expected_native_id: str,
    expected_bridge_id: str,
) -> None:
    try:
        guard = _validate_codex_origin_guard(
            _read_characterization_record(path, marker_secret),
            path=path,
        )
        if (
            guard["native_id"] != _canonical_uuid(expected_native_id)
            or guard["bridge_id"] != expected_bridge_id
            or _path_is_redirect(path)
        ):
            raise RuntimeError
        path.unlink()
    except (CharacterizationGateError, OSError, RuntimeError):
        raise RuntimeError("unsafe_characterization_origin_guard:retire_failed") from None


def _read_report_safely(path: Path) -> dict[str, Any]:
    try:
        before = os.lstat(path)
    except OSError:
        raise CharacterizationGateError(
            "invalid", "characterization_report_unsafe"
        ) from None
    if _path_is_redirect(path) or not stat.S_ISREG(before.st_mode):
        raise CharacterizationGateError("invalid", "characterization_report_unsafe")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size > 1_048_576
        ):
            raise CharacterizationGateError("invalid", "characterization_report_unsafe")
        payload = os.read(descriptor, opened.st_size + 1)
        after = os.fstat(descriptor)
        current = os.lstat(path)
        current_attributes = getattr(current, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            stat.S_ISLNK(current.st_mode)
            or bool(current_attributes & reparse_flag)
            or not stat.S_ISREG(current.st_mode)
            or current.st_dev != opened.st_dev
            or current.st_ino != opened.st_ino
            or current.st_size != opened.st_size
            or current.st_mtime_ns != opened.st_mtime_ns
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or len(payload) != opened.st_size
        ):
            raise CharacterizationGateError("invalid", "characterization_report_unsafe")
    except CharacterizationGateError:
        raise
    except OSError:
        raise CharacterizationGateError(
            "invalid", "characterization_report_unsafe"
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        report = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_unique_json_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey):
        raise CharacterizationGateError(
            "invalid", "characterization_report_malformed"
        ) from None
    if not isinstance(report, dict):
        raise CharacterizationGateError("invalid", "characterization_report_malformed")
    return report


class _DuplicateJsonKey(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey(key)
        value[key] = item
    return value


def _validate_gate_report(
    report: dict[str, Any], *, report_path: Path
) -> _ValidatedGateReport:
    base_keys = {
        "schema_version",
        "characterization_id",
        "created_at",
        "automatic_mirroring_enabled",
        "versions",
        "providers",
    }
    if type(report.get("schema_version")) is not int:
        raise CharacterizationGateError("invalid", "characterization_report_malformed")
    schema_version = report["schema_version"]
    # v1 reports predate provider scoping: they record both providers and no
    # bridge revision.  They stay valid so an existing report root is not
    # invalidated wholesale -- one malformed file fails the whole gate closed.
    if schema_version == 1:
        expected_keys = base_keys
    elif schema_version == 2:
        expected_keys = base_keys | {"bridge_revision"}
    else:
        raise CharacterizationGateError("invalid", "characterization_report_malformed")
    if (
        set(report) != expected_keys
        or report["automatic_mirroring_enabled"] is not False
    ):
        raise CharacterizationGateError("invalid", "characterization_report_malformed")
    try:
        characterization_id = _canonical_uuid(report["characterization_id"])
    except ValueError:
        raise CharacterizationGateError(
            "invalid", "characterization_report_malformed"
        ) from None
    if report_path.stem != characterization_id:
        raise CharacterizationGateError("invalid", "characterization_report_malformed")
    created_at = report["created_at"]
    try:
        created = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        raise CharacterizationGateError(
            "invalid", "characterization_report_malformed"
        ) from None
    if created.tzinfo is None or created.utcoffset() is None:
        raise CharacterizationGateError("invalid", "characterization_report_malformed")
    providers = report["providers"]
    if not isinstance(providers, dict):
        raise CharacterizationGateError("invalid", "characterization_report_malformed")
    provider_names = set(providers)
    if schema_version == 1:
        if provider_names != {"claude", "codex"}:
            raise CharacterizationGateError(
                "invalid", "characterization_report_malformed"
            )
    elif not provider_names or not provider_names.issubset({"claude", "codex"}):
        raise CharacterizationGateError("invalid", "characterization_report_malformed")
    versions = _validated_version_mapping(
        report["versions"], source="report", expected=provider_names
    )
    bridge_revision = (
        None
        if schema_version == 1
        else _validated_revision_mapping(
            report["bridge_revision"], expected=provider_names
        )
    )
    provider_passed: dict[str, bool] = {}
    codex_registration = False
    for name in sorted(provider_names):
        used_registration_turn, passed = _validate_provider_status(
            providers[name], provider=name
        )
        if name == "claude" and used_registration_turn:
            raise CharacterizationGateError(
                "invalid", "characterization_report_malformed"
            )
        if name == "codex":
            codex_registration = used_registration_turn
        provider_passed[name] = passed
    return _ValidatedGateReport(
        path=report_path,
        report=report,
        created_at=created.astimezone(timezone.utc),
        characterization_id=characterization_id,
        codex_registration_turn_required=codex_registration,
        providers=frozenset(provider_names),
        provider_passed=provider_passed,
        versions=versions,
        bridge_revision=bridge_revision,
    )


def _validated_revision_mapping(value: Any, *, expected: set[str]) -> dict[str, str]:
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or not all(isinstance(value[key], str) and value[key] for key in value)
    ):
        raise CharacterizationGateError("invalid", "characterization_report_malformed")
    return {key: value[key] for key in sorted(value)}


def _validate_provider_status(value: Any, *, provider: str) -> tuple[bool, bool]:
    allowed = _PROVIDER_ALLOWED_FIELDS[provider]
    if (
        not isinstance(value, dict)
        or not _PROVIDER_REQUIRED_FIELDS.issubset(value)
        or not set(value).issubset(allowed)
    ):
        raise CharacterizationGateError("invalid", "characterization_report_malformed")
    if type(value["used_registration_turn"]) is not bool:
        raise CharacterizationGateError("invalid", "characterization_report_malformed")
    if (
        not isinstance(value["cleanup"], str)
        or not value["cleanup"]
        or value["cleanup"] != value["cleanup"].strip()
        or "\n" in value["cleanup"]
        or "\r" in value["cleanup"]
    ):
        raise CharacterizationGateError("invalid", "characterization_report_malformed")
    for stage in ("create", "discover", "read", "resume"):
        if type(value[stage]) is not bool:
            raise CharacterizationGateError(
                "invalid", "characterization_report_malformed"
            )
    error_code = value["error_code"]
    if error_code is not None and (
        not isinstance(error_code, str)
        or not error_code
        or error_code != error_code.strip()
        or "\n" in error_code
        or "\r" in error_code
    ):
        raise CharacterizationGateError("invalid", "characterization_report_malformed")
    native_id = value.get("native_id")
    if native_id is not None:
        try:
            _canonical_uuid(native_id)
        except ValueError:
            raise CharacterizationGateError(
                "invalid", "characterization_report_malformed"
            ) from None
    for field in _PROVIDER_NUMBER_FIELDS & value.keys():
        field_value = value[field]
        if field_value is not None and (
            not isinstance(field_value, (int, float))
            or isinstance(field_value, bool)
            or not math.isfinite(float(field_value))
            or float(field_value) < 0
        ):
            raise CharacterizationGateError(
                "invalid", "characterization_report_malformed"
            )
    for field in _PROVIDER_INTEGER_FIELDS & value.keys():
        field_value = value[field]
        if field_value is not None and (
            not isinstance(field_value, int)
            or isinstance(field_value, bool)
            or field_value < 0
        ):
            raise CharacterizationGateError(
                "invalid", "characterization_report_malformed"
            )
    passed = (
        all(value[stage] is True for stage in ("create", "discover", "read", "resume"))
        and error_code is None
    )
    return value["used_registration_turn"], passed


def _validated_version_mapping(
    value: Any, *, source: str, expected: set[str] | None = None
) -> dict[str, str]:
    required = {"claude", "codex"} if expected is None else expected
    if (
        not isinstance(value, Mapping)
        or set(value) != required
        or not all(isinstance(value[key], str) and value[key] for key in value)
    ):
        code = "invalid" if source == "report" else "version_drift"
        detail = (
            "characterization_report_malformed"
            if source == "report"
            else "characterization_version_unavailable"
        )
        raise CharacterizationGateError(code, detail)
    return {key: value[key] for key in sorted(required)}


def _current_cli_versions() -> dict[str, str]:
    try:
        claude = _cli_version([*resolve_cli_executable("claude"), "--version"])
        codex = _cli_version([*resolve_cli_executable("codex"), "--version"])
    except (RuntimeError, ValueError):
        claude = codex = None
    if claude is None or codex is None:
        raise CharacterizationGateError(
            "version_drift", "characterization_version_unavailable"
        )
    return {"claude": claude, "codex": codex}


def write_characterization_report(
    report: Mapping[str, Any],
    *,
    report_root: Path | None = None,
    characterization_id: str,
) -> Path:
    record_id = _canonical_uuid(characterization_id)
    resolved_report_root = (
        Path(report_root)
        if report_root is not None
        else characterization_store_root()
    )
    root = _safe_directory_root(
        resolved_report_root.expanduser(),
        error_code="unsafe_characterization_report",
    )
    report_path = root / f"{record_id}.json"
    if report_path.exists() or _path_is_redirect(report_path):
        raise RuntimeError("unsafe_characterization_report:final_exists")
    sanitized = _sanitize_report_value(dict(report))
    payload = json.dumps(
        sanitized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{record_id}.", suffix=".tmp", dir=root
        )
        temporary = Path(temporary_name)
        if temporary.parent.resolve() != root.resolve() or _path_is_redirect(temporary):
            raise RuntimeError("unsafe_characterization_report:temp_redirect")
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(payload + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if report_path.exists() or _path_is_redirect(report_path):
            raise RuntimeError("unsafe_characterization_report:final_exists")
        os.link(temporary, report_path, follow_symlinks=False)
        temporary.unlink()
    except RuntimeError:
        raise
    except (OSError, ValueError):
        raise RuntimeError("unsafe_characterization_report:write_failed") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None and temporary.exists():
            temporary.unlink(missing_ok=True)
    return report_path


def _safe_directory_root(path: Path, *, error_code: str) -> Path:
    root = path.absolute()
    for candidate in (root, *root.parents):
        if _path_is_redirect(candidate):
            raise RuntimeError(f"{error_code}:redirect_parent")
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise RuntimeError(f"{error_code}:root_unavailable") from None
    if _path_is_redirect(root) or not root.is_dir():
        raise RuntimeError(f"{error_code}:unsafe_root")
    return root


def _path_is_redirect(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def quarantine_claude_transcript(
    source_adapter: ClaudeMarkerSource,
    *,
    native_id: str,
    bridge_id: str,
    source_session_id: str,
    policy_generation: int,
    projects_root: Path = _CLAUDE_PROJECTS_ROOT,
    quarantine_root: Path | None = None,
) -> Path:
    expected_id = _canonical_uuid(native_id)
    if not isinstance(bridge_id, str) or not bridge_id.strip():
        raise UnsafeCharacterizationCleanup("bridge identity is missing")
    expected_payload = BridgeMarkerPayload(
        bridge_id=bridge_id.strip(),
        source_session_id=source_session_id,
        target_provider=Provider.CLAUDE,
        policy_generation=policy_generation,
    )
    path = source_adapter.find_native_session(expected_id)
    if path is None:
        raise UnsafeCharacterizationCleanup("exact Claude transcript was not found")
    candidate = Path(path).resolve()
    allowed_root = Path(projects_root).expanduser().resolve()
    try:
        candidate.relative_to(allowed_root)
    except ValueError as exc:
        raise UnsafeCharacterizationCleanup(
            "Claude transcript is outside the projects root"
        ) from exc
    try:
        projection = source_adapter.parse(candidate).projection
    except Exception as exc:
        raise UnsafeCharacterizationCleanup(
            "Claude transcript could not be parsed safely"
        ) from exc
    if projection.native_id != expected_id:
        raise UnsafeCharacterizationCleanup("Claude transcript UUID mismatch")
    if (
        projection.origin_kind
        not in (OriginKind.BRIDGE_PLACEHOLDER, OriginKind.BRIDGE_CONTINUATION)
        or projection.origin_bridge_id != bridge_id.strip()
        or not source_adapter.projection_has_marker_payload(
            projection, expected_payload
        )
    ):
        raise UnsafeCharacterizationCleanup("Claude signed marker mismatch")

    destination_root = (
        Path(quarantine_root).expanduser()
        if quarantine_root is not None
        else characterization_store_root() / "quarantine"
    )
    try:
        destination_root = _safe_directory_root(
            destination_root, error_code="unsafe_claude_quarantine"
        )
    except RuntimeError:
        raise UnsafeCharacterizationCleanup(
            "Claude quarantine parent is a symlink or unsafe"
        ) from None
    destination = destination_root / f"{expected_id}.jsonl"
    if destination.exists() or _path_is_redirect(destination):
        raise UnsafeCharacterizationCleanup("Claude quarantine target already exists")
    descriptor = -1
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        with candidate.open("rb") as source, os.fdopen(descriptor, "wb") as target:
            descriptor = -1
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        candidate.unlink()
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        destination.unlink(missing_ok=True)
        raise UnsafeCharacterizationCleanup(
            "Claude quarantine move failed safely"
        ) from None
    return destination


def run_live_characterization(
    *,
    report_root: Path | None = None,
    claude_projects_root: Path = _CLAUDE_PROJECTS_ROOT,
    claude_executable: str = "claude",
    codex_executable: str = "codex",
    cwd: Path | None = None,
    provenance_secret: bytes | None = None,
    live_tests_enabled: bool = False,
    providers: Sequence[str] = ("claude", "codex"),
) -> Path:
    """Characterize the selected providers, each in its own real session.

    ``providers`` exists so a single drifted CLI can be re-proven without
    creating and disposing a session for the provider that did not move.  The
    two providers are characterized independently -- separate adapters,
    separate failure capture, no shared mutable state -- so a scoped run is the
    same measurement, not a weaker one.
    """

    if not live_tests_enabled:
        raise RuntimeError("live_characterization_not_enabled")
    selected = tuple(providers)
    if (
        not selected
        or len(set(selected)) != len(selected)
        or not set(selected).issubset({"claude", "codex"})
    ):
        raise RuntimeError("characterization_provider_invalid")
    versions: dict[str, str] = {}
    claude_command: tuple[str, ...] | None = None
    codex_command: tuple[str, ...] | None = None
    if "claude" in selected:
        claude_command = resolve_cli_executable(claude_executable)
        claude_version = _cli_version([*claude_command, "--version"])
        if claude_version is None:
            raise RuntimeError("claude_cli_preflight_failed")
        versions["claude"] = claude_version
    if "codex" in selected:
        codex_command = resolve_cli_executable(codex_executable)
        codex_version = _cli_version([*codex_command, "--version"])
        if codex_version is None:
            raise RuntimeError("codex_cli_preflight_failed")
        versions["codex"] = codex_version
    characterization_id = str(uuid.uuid4())
    if provenance_secret is None:
        from .mcp_server import resolve_marker_key

        provenance_secret = resolve_marker_key()
    _require_secret(provenance_secret)
    resolved_report_root = (
        Path(report_root)
        if report_root is not None
        else characterization_store_root()
    )
    title = f"[Hermes Bridge Characterization] {characterization_id}"
    marker_secret = secrets.token_bytes(32)
    working_directory = Path(cwd or Path.cwd()).resolve()
    ordered = tuple(sorted(selected))
    report: dict[str, Any] = {
        "schema_version": 2,
        "characterization_id": characterization_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "automatic_mirroring_enabled": False,
        "versions": {provider: versions[provider] for provider in ordered},
        # Records which bridge code proved this, so a later scoped reuse of the
        # report can tell whether that code has since moved.
        "bridge_revision": current_bridge_revisions(providers=ordered),
        "providers": {provider: _provider_report() for provider in ordered},
    }
    failures: list[str] = []
    if "claude" in selected:
        try:
            _characterize_claude(
                report["providers"]["claude"],
                characterization_id=characterization_id,
                title=title,
                marker_secret=marker_secret,
                projects_root=Path(claude_projects_root),
                report_root=resolved_report_root,
                executable=claude_command,
                cwd=working_directory,
            )
        except Exception as exc:
            code = _safe_error_code("claude", exc)
            report["providers"]["claude"]["error_code"] = code
            if isinstance(exc, PlaceholderCreationError):
                _record_claude_failure_diagnostics(report["providers"]["claude"], exc)
            failures.append(code)

    codex_origin_guard: Path | None = None
    if "codex" in selected:
        codex_origin_guard = _prepare_codex_origin_guard(
            resolved_report_root,
            characterization_id=characterization_id,
            marker_secret=provenance_secret,
        )
        guard_path = codex_origin_guard

        def record_codex_native_id(native_id: str) -> None:
            _bind_codex_origin_guard(
                guard_path,
                native_id=native_id,
                marker_secret=provenance_secret,
            )

        try:
            _characterize_codex(
                report["providers"]["codex"],
                characterization_id=characterization_id,
                title=title,
                marker_secret=marker_secret,
                executable=codex_command,
                cwd=working_directory,
                record_native_id=record_codex_native_id,
            )
        except Exception as exc:
            code = _safe_error_code("codex", exc)
            report["providers"]["codex"]["error_code"] = code
            failures.append(code)
        else:
            try:
                _canonical_uuid(report["providers"]["codex"].get("native_id"))
            except ValueError:
                code = "codex_characterization_identity_missing"
                report["providers"]["codex"]["error_code"] = code
                failures.append(code)

    report_path = write_characterization_report(
        report,
        report_root=resolved_report_root,
        characterization_id=characterization_id,
    )
    if codex_origin_guard is not None:
        codex_native_id = report["providers"]["codex"].get("native_id")
        if codex_native_id is not None:
            _retire_codex_origin_guard(
                codex_origin_guard,
                marker_secret=provenance_secret,
                expected_native_id=codex_native_id,
                expected_bridge_id=f"characterization-{characterization_id}-codex",
            )
    if failures:
        raise LiveCharacterizationError(report_path, failures)
    return report_path


def _characterize_claude(
    status: dict[str, Any],
    *,
    characterization_id: str,
    title: str,
    marker_secret: bytes,
    projects_root: Path,
    report_root: Path,
    executable: str | Sequence[str],
    cwd: Path,
) -> None:
    native_id = str(uuid.uuid4())
    bridge_id = f"characterization-{characterization_id}-claude"
    source_session_id = f"codex:characterization-{characterization_id}"
    status["native_id"] = native_id
    source = ClaudeSourceAdapter(projects_root, marker_secret=marker_secret)
    creation_processes: list[subprocess.CompletedProcess[str]] = []

    def _run_creation(
        args: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(args, **kwargs)
        creation_processes.append(completed)
        return completed

    try:
        creation_started = time.monotonic()
        result = ClaudeTargetAdapter(
            source,
            marker_secret=marker_secret,
            claude_executable=executable,
            runner=_run_creation,
            process_timeout=180.0,
            discovery_timeout=30.0,
        ).create_placeholder(
            native_id=native_id,
            title=title,
            source_session_id=source_session_id,
            bridge_id=bridge_id,
            policy_generation=1,
            cwd=cwd,
        )
        create_elapsed_ms = (time.monotonic() - creation_started) * 1000.0
        create_metrics = (
            _claude_result_metrics(creation_processes[-1]) if creation_processes else {}
        )
        status["create_cost_usd"] = create_metrics.get("cost_usd")
        status["create_latency_ms"] = create_metrics.get(
            "duration_ms", create_elapsed_ms
        )
        status["create_num_turns"] = create_metrics.get("num_turns")
        status["create"] = result.native_id == native_id
        path = source.find_native_session(native_id)
        status["discover"] = path is not None
        if path is None:
            raise RuntimeError("claude_discovery_failed")
        projection = source.parse(path).projection
        status["read"] = (
            projection.native_id == native_id
            and projection.origin_bridge_id == bridge_id
        )
        if not status["read"]:
            raise RuntimeError("claude_read_verification_failed")

        resume_started = time.monotonic()
        resume = _resume_claude_characterization(
            source,
            baseline_projection=projection,
            native_id=native_id,
            bridge_id=bridge_id,
            resume_nonce=secrets.token_hex(16),
            executable=executable,
            cwd=cwd,
        )
        resume_elapsed_ms = (time.monotonic() - resume_started) * 1000.0
        resume_metrics = _claude_result_metrics(resume) if resume is not None else {}
        status["resume_cost_usd"] = resume_metrics.get("cost_usd")
        status["resume_latency_ms"] = resume_metrics.get(
            "duration_ms", resume_elapsed_ms
        )
        status["resume_num_turns"] = resume_metrics.get("num_turns")
        costs = [
            value
            for value in (
                status.get("create_cost_usd"),
                status.get("resume_cost_usd"),
            )
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
        status["total_cost_usd"] = sum(costs) if costs else None
        status["total_latency_ms"] = float(status["create_latency_ms"]) + float(
            status["resume_latency_ms"]
        )
        turns = [
            value
            for value in (
                status.get("create_num_turns"),
                status.get("resume_num_turns"),
            )
            if isinstance(value, int) and not isinstance(value, bool)
        ]
        status["total_num_turns"] = sum(turns) if turns else None
        status["resume"] = True
    except PlaceholderCreationError as exc:
        _record_claude_failure_diagnostics(status, exc)
        raise
    finally:
        try:
            quarantine_claude_transcript(
                source,
                native_id=native_id,
                bridge_id=bridge_id,
                source_session_id=source_session_id,
                policy_generation=1,
                projects_root=projects_root,
                quarantine_root=report_root / "quarantine",
            )
            status["cleanup"] = "quarantined"
        except UnsafeCharacterizationCleanup:
            status["cleanup"] = "not_moved_safety_check"


def _resume_claude_characterization(
    source: ClaudeReadableSource,
    *,
    baseline_projection: SessionProjection,
    native_id: str,
    bridge_id: str,
    resume_nonce: str,
    executable: str | Sequence[str],
    cwd: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    process_timeout: float = 180.0,
    verification_timeout: float = 30.0,
    verification_poll_interval: float = 0.1,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> subprocess.CompletedProcess[str] | None:
    if not re.fullmatch(r"[0-9a-f]{32}", resume_nonce):
        raise ValueError("Claude resume nonce must be 32 lowercase hex characters")
    if (
        baseline_projection.native_id != native_id
        or baseline_projection.origin_bridge_id != bridge_id
        or baseline_projection.origin_kind
        not in (OriginKind.BRIDGE_PLACEHOLDER, OriginKind.BRIDGE_CONTINUATION)
    ):
        raise PlaceholderCreationError("claude_resume_baseline_mismatch")
    baseline_cursor = baseline_projection.native_cursor
    baseline_hash = baseline_projection.native_hash
    baseline_messages = _projection_message_identities(baseline_projection)
    if not baseline_cursor or not baseline_hash or not baseline_messages:
        raise PlaceholderCreationError("claude_resume_baseline_incomplete")

    prompt = (
        "Hermes Bridge live characterization resume verification tag "
        f"{resume_nonce}. Reply READY."
    )
    args = [
        *_immutable_argv_prefix(executable, label="Claude executable"),
        "--print",
        "--resume",
        native_id,
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--max-budget-usd",
        CLAUDE_PLACEHOLDER_MAX_BUDGET_USD,
        "--output-format",
        "json",
        prompt,
    ]
    completed: subprocess.CompletedProcess[str] | None = None
    process_failure: PlaceholderCreationError | None = None
    runner_failure: PlaceholderCreationError | None = None
    metrics: dict[str, int | float] = {}
    try:
        completed = runner(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=process_timeout,
            stdin=subprocess.DEVNULL,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired:
        process_failure = PlaceholderCreationError("claude_resume_timeout")
    except FileNotFoundError:
        runner_failure = PlaceholderCreationError("claude_resume_executable_not_found")
    except Exception:
        runner_failure = PlaceholderCreationError("claude_resume_process_failed")
    else:
        metrics = _claude_result_metrics(completed)
        if completed.returncode != 0:
            process_code = classify_claude_process_failure(completed)
            suffix = process_code.removeprefix("claude_process_")
            process_failure = _claude_resume_error(f"claude_resume_{suffix}", metrics)
    if runner_failure is not None:
        raise runner_failure

    deadline = monotonic() + verification_timeout
    last_code = "claude_resume_target_not_found"
    while True:
        path = source.find_native_session(native_id)
        if path is not None:
            parse_failure: PlaceholderCreationError | None = None
            try:
                projection = source.parse(path).projection
            except Exception:
                projection = None
                parse_failure = _claude_resume_error(
                    "claude_resume_target_unreadable", metrics
                )
            if parse_failure is not None:
                raise parse_failure
            assert projection is not None
            if projection.native_id != native_id:
                raise _claude_resume_error("claude_resume_identity_mismatch", metrics)
            if (
                projection.origin_bridge_id != bridge_id
                or projection.origin_kind
                not in (
                    OriginKind.BRIDGE_PLACEHOLDER,
                    OriginKind.BRIDGE_CONTINUATION,
                )
            ):
                raise _claude_resume_error("claude_resume_marker_mismatch", metrics)

            post_messages = _projection_message_identities(projection)
            post_fingerprint = (
                projection.native_cursor,
                projection.native_hash,
                post_messages,
            )
            baseline_fingerprint = (
                baseline_cursor,
                baseline_hash,
                baseline_messages,
            )
            new_messages = post_messages - baseline_messages
            advanced = (
                projection.origin_kind is OriginKind.BRIDGE_CONTINUATION
                and bool(projection.native_cursor)
                and bool(projection.native_hash)
                and projection.native_cursor != baseline_cursor
                and post_fingerprint != baseline_fingerprint
                and baseline_messages.issubset(post_messages)
                and bool(new_messages)
            )
            if advanced:
                nonce_found = any(
                    (message.native_event_id, message.ordinal) in new_messages
                    and message.role == "user"
                    and isinstance(message.content, str)
                    and resume_nonce in message.content
                    for message in projection.messages
                )
                if nonce_found:
                    return completed
                last_code = "claude_resume_nonce_mismatch"
            else:
                last_code = "claude_resume_not_advanced"
        if monotonic() >= deadline:
            if (
                last_code == "claude_resume_target_not_found"
                and process_failure is not None
            ):
                raise process_failure
            raise _claude_resume_error(last_code, metrics)
        sleep(verification_poll_interval)


def _projection_message_identities(
    projection: SessionProjection,
) -> frozenset[tuple[str, int]]:
    return frozenset(
        (message.native_event_id, message.ordinal)
        for message in projection.messages
        if isinstance(message.native_event_id, str)
        and message.native_event_id
        and isinstance(message.ordinal, int)
        and not isinstance(message.ordinal, bool)
        and message.ordinal >= 0
    )


def _claude_resume_error(
    code: str, metrics: Mapping[str, int | float]
) -> PlaceholderCreationError:
    cost = metrics.get("cost_usd")
    duration = metrics.get("duration_ms")
    turns = metrics.get("num_turns")
    return PlaceholderCreationError(
        code,
        observed_cost_usd=cost,
        duration_ms=duration,
        num_turns=turns if isinstance(turns, int) else None,
    )


def _characterize_codex(
    status: dict[str, Any],
    *,
    characterization_id: str,
    title: str,
    marker_secret: bytes,
    executable: Sequence[str],
    cwd: Path,
    record_native_id: Callable[[str], None],
) -> None:
    characterization_started = time.monotonic()
    codex_bin = _single_native_executable(executable, label="Codex")
    client = CodexAppServerClient(codex_bin=codex_bin)
    native_id: str | None = None
    try:
        source = CodexSourceAdapter(client, marker_secret=marker_secret)
        try:
            create_started = time.monotonic()
            result = CodexTargetAdapter(
                client,
                source_adapter=source,
                marker_secret=marker_secret,
                require_registration_turn=None,
                request_timeout=45.0,
            ).create_placeholder(
                title=title,
                source_session_id=f"claude:characterization-{characterization_id}",
                bridge_id=f"characterization-{characterization_id}-codex",
                policy_generation=1,
                cwd=cwd,
            )
            status["create_latency_ms"] = (time.monotonic() - create_started) * 1000.0
        except PlaceholderCreationError as exc:
            native_id = exc.native_id
            if native_id is not None:
                status["native_id"] = native_id
                record_native_id(native_id)
            raise
        native_id = result.native_id
        status["native_id"] = native_id
        record_native_id(native_id)
        status["create"] = True
        status["used_registration_turn"] = result.used_registration_turn
        summary = source.find_native_thread(
            native_id,
            source_kinds=("vscode", "appServer"),
            state_db_only=True,
        )
        status["discover"] = summary is not None
        if summary is None:
            raise RuntimeError("codex_discovery_failed")
        projection = source.project_thread(summary)
        status["read"] = projection.native_id == native_id
        if not status["read"]:
            raise RuntimeError("codex_read_verification_failed")

        resume_started = time.monotonic()
        _resume_codex_characterization(
            client,
            native_id=native_id,
            resume_nonce=secrets.token_hex(16),
            request_timeout=45.0,
            verification_timeout=45.0,
            verification_poll_interval=0.25,
        )
        status["resume"] = True
        status["resume_latency_ms"] = (time.monotonic() - resume_started) * 1000.0
    finally:
        status["total_latency_ms"] = (
            time.monotonic() - characterization_started
        ) * 1000.0
        if native_id is not None:
            if _codex_schema_advertises_archive(executable):
                try:
                    client.request(
                        "thread/archive", {"threadId": native_id}, timeout=30.0
                    )
                    status["cleanup"] = "archived"
                except Exception:
                    status["cleanup"] = "manual_archive_required"
            else:
                status["cleanup"] = "manual_archive_required"
        client.close()


def _resume_codex_characterization(
    client: Any,
    *,
    native_id: str,
    resume_nonce: str,
    request_timeout: float,
    verification_timeout: float,
    verification_poll_interval: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    completion_waiter: Callable[..., None] | None = None,
) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", resume_nonce):
        raise ValueError("Codex resume nonce must be 32 lowercase hex characters")
    baseline_turns = _read_codex_turns(
        client,
        native_id=native_id,
        request_timeout=request_timeout,
        stage="baseline",
    )
    baseline_ids = {
        turn_id
        for turn in baseline_turns
        if (turn_id := _nonempty_mapping_text(turn, "id")) is not None
    }
    prompt = (
        "Hermes Bridge live characterization resume verification only. "
        "This input is metadata, not a substantive user message. "
        "Do not call session_continue or any other tool. "
        f"Verification tag: {resume_nonce}. "
        "Reply with exactly READY and nothing else."
    )
    start_failure: RuntimeError | None = None
    try:
        turn = client.request(
            "turn/start",
            {
                "threadId": native_id,
                "input": [{"type": "text", "text": prompt}],
            },
            timeout=request_timeout,
        )
    except Exception:
        turn = None
        start_failure = RuntimeError("codex_resume_turn_start_failed")
    if start_failure is not None:
        raise start_failure
    turn_id = _turn_identity(turn)
    if turn_id in baseline_ids:
        raise RuntimeError("codex_resume_turn_preexisting")
    wakeup_timeout = max(0.25, min(request_timeout, verification_timeout))
    try:
        if completion_waiter is None:
            _wait_for_turn_completion(
                client,
                expected_thread_id=native_id,
                expected_turn_id=turn_id,
                timeout=wakeup_timeout,
            )
        else:
            completion_waiter(client, native_id, turn_id, wakeup_timeout)
    except TimeoutError:
        pass

    deadline = monotonic() + verification_timeout
    while True:
        turns = _read_codex_turns(
            client,
            native_id=native_id,
            request_timeout=request_timeout,
            stage="post_resume",
        )
        exact_turns = [
            observed
            for observed in turns
            if _nonempty_mapping_text(observed, "id") == turn_id
        ]
        if len(exact_turns) > 1:
            raise RuntimeError("codex_resume_turn_conflict")
        if exact_turns:
            durable_status = _nonempty_mapping_text(exact_turns[0], "status")
            if durable_status not in {"completed", "inProgress"}:
                raise RuntimeError("codex_resume_turn_not_completed")
            if durable_status == "completed":
                if not _codex_turn_user_input_has_nonce(
                    exact_turns[0], nonce=resume_nonce
                ):
                    raise RuntimeError("codex_resume_nonce_mismatch")
                return turn_id
        if monotonic() >= deadline:
            raise RuntimeError("codex_resume_turn_not_found")
        sleep(verification_poll_interval)


def _read_codex_turns(
    client: Any,
    *,
    native_id: str,
    request_timeout: float,
    stage: str,
) -> list[dict[str, Any]]:
    read_failure: RuntimeError | None = None
    try:
        read = client.request(
            "thread/read",
            {"threadId": native_id, "includeTurns": True},
            timeout=request_timeout,
        )
    except Exception:
        read = None
        read_failure = RuntimeError(f"codex_resume_{stage}_read_failed")
    if read_failure is not None:
        raise read_failure
    thread = read.get("thread") if isinstance(read, dict) else None
    if not isinstance(thread, dict) or thread.get("id") != native_id:
        raise RuntimeError("codex_resume_identity_mismatch")
    turns = thread.get("turns")
    if not isinstance(turns, list) or not all(isinstance(turn, dict) for turn in turns):
        raise RuntimeError("codex_resume_read_malformed")
    return turns


def _nonempty_mapping_text(value: Any, key: str) -> str | None:
    if not isinstance(value, dict):
        return None
    observed = value.get(key)
    return observed if isinstance(observed, str) and observed else None


def _codex_turn_user_input_has_nonce(turn: dict[str, Any], *, nonce: str) -> bool:
    items = turn.get("items")
    if not isinstance(items, list):
        return False
    occurrences = 0
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "userMessage":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str):
                occurrences += text.count(nonce)
    return occurrences == 1


def _provider_report() -> dict[str, Any]:
    return {
        "create": False,
        "discover": False,
        "read": False,
        "resume": False,
        "used_registration_turn": False,
        "cleanup": "not_started",
        "error_code": None,
    }


def _record_claude_failure_diagnostics(
    status: dict[str, Any], exc: PlaceholderCreationError
) -> None:
    for key in ("observed_cost_usd", "duration_ms"):
        value = getattr(exc, key, None)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) >= 0
        ):
            status[key] = float(value)
    num_turns = getattr(exc, "num_turns", None)
    if (
        isinstance(num_turns, int)
        and not isinstance(num_turns, bool)
        and num_turns >= 0
    ):
        status["num_turns"] = num_turns


def _claude_result_metrics(
    completed: subprocess.CompletedProcess[str],
) -> dict[str, int | float]:
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    metrics: dict[str, int | float] = {}
    for source_key, target_key in (
        ("total_cost_usd", "cost_usd"),
        ("duration_ms", "duration_ms"),
    ):
        value = payload.get(source_key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) >= 0
        ):
            metrics[target_key] = float(value)
    num_turns = payload.get("num_turns")
    if (
        isinstance(num_turns, int)
        and not isinstance(num_turns, bool)
        and num_turns >= 0
    ):
        metrics["num_turns"] = num_turns
    return metrics


def resolve_cli_executable(
    executable: str,
    *,
    which=shutil.which,
) -> tuple[str, ...]:
    if not isinstance(executable, str) or not executable.strip():
        raise ValueError("CLI executable must not be empty")
    normalized = executable.strip()
    resolved = str(which(normalized) or normalized)
    candidate = Path(resolved).expanduser()
    suffix = candidate.suffix.casefold()
    if candidate.stem.casefold() == "claude":
        return resolve_claude_command(normalized, which=which)
    if suffix not in {".cmd", ".ps1", ".bat"}:
        return (str(candidate.resolve()) if candidate.exists() else resolved,)

    command_name = candidate.stem.casefold()
    if command_name == "codex":
        if suffix == ".cmd" and candidate.is_file():
            return (str(candidate.resolve()),)
        raise RuntimeError("unsupported_shell_shim")
    raise RuntimeError("unsupported_shell_shim")


def _immutable_argv_prefix(
    command: str | Sequence[str], *, label: str
) -> tuple[str, ...]:
    entries: Sequence[str] = (command,) if isinstance(command, str) else command
    if not entries:
        raise ValueError(f"{label} must not be empty")
    normalized: list[str] = []
    for entry in entries:
        if (
            not isinstance(entry, str)
            or not entry.strip()
            or "\r" in entry
            or "\n" in entry
        ):
            raise ValueError(f"{label} entries must be non-empty and single-line")
        normalized.append(entry.strip())
    return tuple(normalized)


def _wait_for_turn_completion(
    client: CodexAppServerClient,
    *,
    expected_thread_id: str,
    expected_turn_id: str | None,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        notification = client.take_notification(timeout=0.25)
        if not isinstance(notification, dict):
            continue
        if notification.get("method") != "turn/completed":
            continue
        params = notification.get("params")
        turn = params.get("turn") if isinstance(params, dict) else None
        observed_thread = params.get("threadId") if isinstance(params, dict) else None
        observed = turn.get("id") if isinstance(turn, dict) else None
        if observed_thread == expected_thread_id and observed == expected_turn_id:
            return
    raise TimeoutError("codex_turn_completion_timeout")


def _turn_identity(response: Any) -> str:
    if not isinstance(response, dict):
        raise RuntimeError("codex_turn_start_malformed")
    turn = response.get("turn")
    turn_id = turn.get("id") if isinstance(turn, dict) else None
    if not isinstance(turn_id, str) or not turn_id:
        raise RuntimeError("codex_turn_start_missing_id")
    return turn_id


def _single_native_executable(command: Sequence[str], *, label: str) -> str:
    if len(command) != 1:
        raise RuntimeError(f"{label.casefold()}_direct_runtime_required")
    executable = command[0]
    if not isinstance(executable, str) or not executable.strip():
        raise RuntimeError(f"{label.casefold()}_executable_invalid")
    return executable


def _codex_schema_advertises_archive(executable: Sequence[str]) -> bool:
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-codex-schema-") as directory:
            completed = subprocess.run(
                [
                    *executable,
                    "app-server",
                    "generate-json-schema",
                    "--out",
                    directory,
                ],
                capture_output=True,
                text=True,
                timeout=60.0,
                stdin=subprocess.DEVNULL,
                shell=False,
                check=False,
            )
            if completed.returncode != 0:
                return False
            schema_path = Path(directory) / "ClientRequest.json"
            if not schema_path.is_file():
                return False
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            return "thread/archive" in _all_schema_strings(schema)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def _all_schema_strings(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        strings: set[str] = set()
        for key, item in value.items():
            strings.add(str(key))
            strings.update(_all_schema_strings(item))
        return strings
    if isinstance(value, list):
        strings: set[str] = set()
        for item in value:
            strings.update(_all_schema_strings(item))
        return strings
    return set()


def _cli_version(args: list[str]) -> str | None:
    try:
        # run_text_capture, not capture_output=True: the only callers pass
        # `claude --version` / `codex --version`, and both CLIs are installed
        # through npm, so on Windows they resolve to .cmd batch shims — cmd.exe
        # is the direct child and node.exe is already a grandchild. The
        # grandchild inherits the capture pipe handles and holds the write end
        # open, so the pipe never reaches EOF and the 15s never fires.
        from hermes_cli._subprocess_compat import run_text_capture

        completed = run_text_capture(
            args,
            timeout=15.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    stdout = completed.stdout
    if not isinstance(stdout, str):
        return None
    normalized = stdout.replace("\r\n", "\n").replace("\r", "\n").strip()
    if (
        not normalized
        or "\x00" in normalized
        or len(normalized.encode("utf-8")) > _MAX_CLI_VERSION_BYTES
    ):
        return None
    return normalized


def _safe_error_code(provider: str, exc: Exception) -> str:
    if isinstance(exc, PlaceholderCreationError):
        return exc.code
    message = str(exc)
    if re.fullmatch(r"[a-z0-9_:-]{1,100}", message):
        return f"{provider}_{message}"[:120]
    return f"{provider}_{type(exc).__name__.lower()}"


def _sanitize_report_value(value: Any, *, key: str | None = None) -> Any:
    if key is not None and key.lower() in _SENSITIVE_REPORT_KEYS:
        return None
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize_report_value(item, key=str(item_key))
            for item_key, item in value.items()
            if str(item_key).lower() not in _SENSITIVE_REPORT_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_report_value(item) for item in value]
    if isinstance(value, str):
        sanitized = value.replace(_MARKER_PREFIX, "[REDACTED_MARKER]:")
        return _SECRET_RE.sub("[REDACTED]", sanitized)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return type(value).__name__


def _canonical_uuid(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("characterization ID must be a UUID") from exc
    canonical = str(parsed)
    if not isinstance(value, str) or canonical != value.lower():
        raise ValueError("characterization ID must use canonical UUID syntax")
    return canonical
