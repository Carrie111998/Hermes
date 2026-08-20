from __future__ import annotations

import asyncio
import bisect
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
import hashlib
import inspect
import logging
import math
import os
import re
from pathlib import Path
import time
import traceback
from typing import Any, NoReturn, Protocol, cast
import uuid

import hermes_constants
from agent.redact import redact_sensitive_text

from .claude_adapter import (
    AmbiguousPlaceholderCreation,
    ClaudeCursor,
    PlaceholderCreationError,
    decode_claude_cursor,
    encode_claude_cursor,
)
from .claude_visibility import (
    CLAUDE_VISIBILITY_EXCLUSION_CODES,
    ClaudeVisibilityCandidate,
    ClaudeVisibilityClaim,
    ClaudeVisibilityIdentity,
    build_claude_visibility_candidate,
    derive_claude_visibility_identity,
    evaluate_claude_visibility,
)
from .claude_visibility_codes import (
    CLAUDE_VISIBILITY_FATAL_CODES,
    CLAUDE_VISIBILITY_RETRY_CODES,
)
from .codex_adapter import SidebarVerificationError
from .config import BridgeConfig
from .context_pack import ContextPackRequest
from .mirror import (
    DiscoveryMode,
    BatchProgress,
    EligibilityContext,
    MirrorPolicy,
    eligible_mirror_candidates,
    enqueue_mirror_job,
    load_continuous_watermark,
    persist_continuous_watermark,
    retry_delay_seconds,
    should_halt_batch,
)
from .models import (
    ContextPack,
    BridgeMarkerPayload,
    MirrorJobState,
    OriginKind,
    Provider,
    Relation,
    SessionLink,
    SessionProjection,
    UpsertResult,
    canonical_session_id,
)
from .sidebar import (
    SidebarCandidate,
    VerifiedSidebarThread,
    build_hydration_message,
    is_sidebar_session_eligible,
    sidebar_bridge_id,
    sidebar_title,
)
from .sidebar_placement import (
    SidebarPlacementError,
    placement_paths_equivalent,
    resolve_sidebar_placement,
)
from .sidebar_reconciliation import (
    SidebarReconciliationEvidence,
    SidebarReconciliationState,
)
from .preview import build_session_preview
from .store import (
    SIDEBAR_EXCLUSION_REASONS,
    LocalSessionOwnsCanonicalId,
    SessionBridgeStore,
    SidebarSource,
    SidebarSourcePage,
    StaleExternalProjection,
    redact_codex_thread_id,
)
from .worktree import (
    WorktreeSnapshot,
    WorktreeSnapshotError,
    capture_worktree_snapshot,
    validate_worktree_snapshot,
)


_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScanSummary:
    provider: Provider | None
    discovered: int
    indexed: int
    rebuilt: int
    failed: int
    duration_ms: float


@dataclass(frozen=True)
class ReconcileSummary:
    examined: int
    recovered: int
    retried: int
    failed: int


@dataclass(frozen=True)
class JobSummary:
    claimed: int
    succeeded: int
    retried: int
    manual_failure: int


@dataclass(frozen=True)
class SidebarRegistrationSummary:
    examined: int
    queued: int
    by_provider: Mapping[str, int]
    failed: int
    excluded: int = 0
    excluded_by_reason: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class SidebarDeliveryClaim:
    lease_token: str
    source_session_id: str
    bridge_id: str
    reconciliation_state: SidebarReconciliationState
    reconciliation_generation: str
    reconciliation_proof_digest: str
    recovered_thread_id: str | None
    create_eligible: bool
    rename_required: bool
    create_reserved: bool = False


@dataclass(frozen=True)
class SidebarHydrationClaim:
    lease_token: str
    source_session_id: str
    bridge_id: str
    codex_thread_id: str
    source_cursor: str
    source_hash: str
    preview_version: int
    preview_digest: str
    hydration_marker: str
    hydration_message: str
    cwd: str
    git_root: str | None
    send_reserved: bool


@dataclass(frozen=True)
class RefreshResult:
    session_id: str
    cursor: str
    source_hash: str
    stale: bool
    warning: str | None


@dataclass(frozen=True)
class ContinueRequest:
    session_id: str
    bridge_id: str
    target_provider: Provider
    context_budget_chars: int


@dataclass(frozen=True)
class ContinueResult:
    pack: ContextPack
    link: SessionLink
    warnings: Sequence[str]
    exact_cwd: str | None = None


@dataclass(frozen=True)
class ClaudeVisibilityCandidateResult:
    candidate: ClaudeVisibilityCandidate
    identity: ClaudeVisibilityIdentity
    activity: float


@dataclass(frozen=True)
class ClaudeVisibilityExclusion:
    source_session_id: str
    source_provider: str
    activity: float
    reason: str


@dataclass(frozen=True)
class ClaudeVisibilityDiscoveryResult:
    enabled: bool
    candidates: tuple[ClaudeVisibilityCandidateResult, ...] = ()
    exclusions: tuple[ClaudeVisibilityExclusion, ...] = ()
    degraded: bool = False
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClaudeVisibilityApplyResult:
    enabled: bool
    mode: str
    candidates: tuple[ClaudeVisibilityCandidateResult, ...] = ()
    exclusions: tuple[ClaudeVisibilityExclusion, ...] = ()
    applied: int = 0
    duplicates: int = 0
    degraded: bool = False
    open_reasons: tuple[str, ...] = ()
    fatal_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClaudeVisibilityRunResult:
    enabled: bool
    status: str
    job_id: str | None = None
    error_code: str | None = None
    degraded: bool = False
    fatal: bool = False
    discovery: ClaudeVisibilityApplyResult | None = None


class _ClaudeVisibilityInventory(Protocol):
    def __call__(self, after: float) -> Sequence[SidebarSource]: ...


class _ClaudeVisibilityRegistrar(Protocol):
    def process(self, claim: ClaudeVisibilityClaim) -> object: ...


class _ClaudeVisibilityStore(Protocol):
    def claude_visibility_status(self, now: float) -> Mapping[str, Any]: ...
    def has_claude_visibility_source(self, source_session_id: str) -> bool: ...
    def enqueue_claude_visibility_job(
        self,
        candidate: ClaudeVisibilityCandidate,
        identity: ClaudeVisibilityIdentity,
        marker_secret: bytes,
    ) -> object: ...
    def enqueue_claude_visibility_batch_if_idle(
        self,
        items: Sequence[tuple[ClaudeVisibilityCandidate, ClaudeVisibilityIdentity]],
        marker_secret: bytes,
    ) -> Mapping[str, Any]: ...
    def claim_claude_visibility_job(
        self,
        now: float,
        lease_seconds: float,
        daily_limit: int,
        cost_limit: object,
        reserved_cost: object,
        max_attempts: int,
        *,
        expected_job_id: str | None = None,
    ) -> ClaudeVisibilityClaim: ...
    def record_claude_visibility_cycle(
        self, *, status: str, error_code: str | None, registrar_result: bool
    ) -> None: ...


_CLAUDE_VISIBILITY_DISCOVERY_CODES = CLAUDE_VISIBILITY_EXCLUSION_CODES | {
    "outside_activity_window",
    "duplicate_source",
}
_CLAUDE_VISIBILITY_MANUAL_LIMIT = 10
_CLAUDE_VISIBILITY_OPEN_STATES = (
    "claude_pending",
    "claude_leased",
    "claude_retry",
    "claude_failed",
)
_CLAUDE_VISIBILITY_IDLE_CLAIM_STATUSES = frozenset({
    "no_due_job",
    "daily_limit",
    "cost_limit",
})
_CLAUDE_VISIBILITY_REGISTRAR_STATUSES = frozenset({
    "absent",
    "failed",
    "retry",
    "visible",
})


def _claude_visibility_enqueue_gates(
    status: object,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return fixed open/fatal reasons without exposing mutable store rows."""

    if not isinstance(status, Mapping):
        return (), ("invalid_visibility_status",)
    status_mapping = cast(Mapping[object, object], status)
    counts = status_mapping.get("counts")
    retry_codes = status_mapping.get("retry_codes")
    failed_codes = status_mapping.get("failed_codes")
    fatal_rows = status_mapping.get("fatal", [])
    if not isinstance(counts, Mapping):
        return (), ("invalid_visibility_status",)
    if not isinstance(retry_codes, Mapping):
        return (), ("invalid_visibility_status",)
    if not isinstance(failed_codes, Mapping):
        return (), ("invalid_visibility_status",)
    if not isinstance(fatal_rows, list):
        return (), ("invalid_visibility_status",)

    def _validated_counts(values: Mapping[object, object]) -> dict[str, int] | None:
        result: dict[str, int] = {}
        for key, value in values.items():
            if type(key) is not str or type(value) is not int or value < 0:
                return None
            result[key] = value
        return result

    count_values = _validated_counts(cast(Mapping[object, object], counts))
    retry_values = _validated_counts(cast(Mapping[object, object], retry_codes))
    failed_values = _validated_counts(cast(Mapping[object, object], failed_codes))
    if count_values is None or retry_values is None or failed_values is None:
        return (), ("invalid_visibility_status",)

    open_reasons = (
        ("open_visibility_work",)
        if any(
            count_values.get(state, 0) > 0 for state in _CLAUDE_VISIBILITY_OPEN_STATES
        )
        else ()
    )
    fatal: set[str] = set()
    for item in fatal_rows:
        if not isinstance(item, Mapping):
            fatal.add("invalid_visibility_status")
            continue
        fatal_item = cast(Mapping[object, object], item)
        code = fatal_item.get("code")
        if code in ("unknown_job_state", "unknown_error_code"):
            fatal.add(str(code))
        else:
            fatal.add("invalid_visibility_status")
    for code, count in retry_values.items():
        if count > 0 and code not in CLAUDE_VISIBILITY_RETRY_CODES:
            fatal.add("unknown_retry_code")
    for code, count in failed_values.items():
        if count <= 0:
            continue
        fatal.add(
            code if code in CLAUDE_VISIBILITY_FATAL_CODES else "unknown_failed_code"
        )
    return open_reasons, tuple(sorted(fatal))


class ClaudeVisibilityCoordinator:
    """Discovery and single-claim delivery orchestration for Claude visibility."""

    def __init__(
        self,
        *,
        config: BridgeConfig,
        store: _ClaudeVisibilityStore,
        inventory: _ClaudeVisibilityInventory,
        registrar: _ClaudeVisibilityRegistrar,
        marker_secret: bytes,
        continuous_inventory: _ClaudeVisibilityInventory | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(config, BridgeConfig):
            raise TypeError("config must be a BridgeConfig")
        if not isinstance(marker_secret, bytes) or not marker_secret:
            raise ValueError("marker_secret must be nonempty bytes")
        self._config = config
        self._store = store
        self._inventory = inventory
        self._continuous_inventory = continuous_inventory or inventory
        self._registrar = registrar
        self._marker_secret = marker_secret
        self._clock = clock

    def discover(self, *, days: int, limit: int) -> ClaudeVisibilityDiscoveryResult:
        return self._discover(days=days, limit=limit, manual=True)

    @staticmethod
    def _log_visibility_discovery_degraded(stage: str, exc: BaseException) -> None:
        """Surface the cause behind an otherwise opaque provider_degraded.

        Both discovery handlers collapse every unexpected failure into the same
        public reason, and previously logged nothing at all. Distinct faults --
        a duplicate session identity, a transient `database is locked` under
        WAL contention, an unreachable provider -- were therefore
        indistinguishable from the outside, and each one only became visible
        after the previous had been cleared. The public reason code is
        unchanged; only the operator-facing diagnosis improves.
        """
        try:
            _LOG.warning(
                "claude_visibility_discovery_degraded stage=%s exc=%s detail=%r",
                stage,
                type(exc).__name__,
                str(exc)[:200],
            )
        except Exception:
            pass

    def _discover(
        self, *, days: int, limit: int, manual: bool
    ) -> ClaudeVisibilityDiscoveryResult:
        if not self._config.claude_visibility.enabled:
            return ClaudeVisibilityDiscoveryResult(enabled=False, reasons=("disabled",))
        if type(days) is not int or days <= 0:
            raise ValueError("days must be a positive integer")
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be a positive integer")
        bounded_limit = (
            min(
                limit,
                _CLAUDE_VISIBILITY_MANUAL_LIMIT,
                self._config.claude_visibility.manual_batch_limit,
            )
            if manual
            else limit
        )
        try:
            now = float(self._clock())
            if not math.isfinite(now):
                raise ValueError("clock must be finite")
            after = now - days * 86400
        except Exception:
            return ClaudeVisibilityDiscoveryResult(
                enabled=True, degraded=True, reasons=("inventory_invalid",)
            )
        if not manual:
            try:
                status = self._store.claude_visibility_status(now)
                cursor = status.get("last_empty_cycle")
                if cursor is not None:
                    if not isinstance(cursor, Mapping):
                        raise ValueError("last empty cycle must be an object")
                    tracked = cursor.get("tracked")
                    value = cursor.get("value")
                    if tracked is True:
                        cycle_at = float(value)
                        if not math.isfinite(cycle_at) or cycle_at > now:
                            raise ValueError("last empty cycle must be finite and past")
                        after = max(after, cycle_at - 120.0)
                    elif tracked is not False or value is not None:
                        raise ValueError("last empty cycle is malformed")
            except (TypeError, ValueError):
                return ClaudeVisibilityDiscoveryResult(
                    enabled=True, degraded=True, reasons=("inventory_invalid",)
                )
            except Exception as exc:
                self._log_visibility_discovery_degraded("status_cursor", exc)
                return ClaudeVisibilityDiscoveryResult(
                    enabled=True, degraded=True, reasons=("provider_degraded",)
                )
        try:
            inventory = self._inventory if manual else self._continuous_inventory
            sources = tuple(inventory(after))
        except Exception as exc:
            self._log_visibility_discovery_degraded("inventory", exc)
            return ClaudeVisibilityDiscoveryResult(
                enabled=True, degraded=True, reasons=("provider_degraded",)
            )
        try:
            ordered = sorted(
                sources,
                key=lambda item: (
                    -float(item.projection.last_active),
                    item.source_session_id,
                    item.projection.provider.value,
                ),
            )
            candidates: list[ClaudeVisibilityCandidateResult] = []
            exclusions: list[ClaudeVisibilityExclusion] = []
            seen: set[tuple[str, Provider]] = set()
            for source in ordered:
                projection = source.projection
                activity = float(projection.last_active)
                if not math.isfinite(activity):
                    raise ValueError("activity must be finite")
                key = (source.source_session_id, projection.provider)
                if key in seen:
                    reason = "duplicate_source"
                elif activity < after:
                    reason = "outside_activity_window"
                else:
                    reason = evaluate_claude_visibility(
                        projection,
                        automation_only=source.automation_only,
                        subagent_only=source.subagent_only,
                    )
                    if (
                        reason == "eligible"
                        and self._store.has_claude_visibility_source(
                            source.source_session_id
                        )
                    ):
                        reason = "duplicate_source"
                seen.add(key)
                if reason != "eligible":
                    if reason not in _CLAUDE_VISIBILITY_DISCOVERY_CODES:
                        raise ValueError("unknown exclusion")
                    exclusions.append(
                        ClaudeVisibilityExclusion(
                            source_session_id=source.source_session_id,
                            source_provider=projection.provider.value,
                            activity=activity,
                            reason=reason,
                        )
                    )
                    continue
                candidate = build_claude_visibility_candidate(
                    projection,
                    eligible_at=activity,
                    git_root=source.git_root,
                    git_head=source.git_head,
                    worktree_id=source.worktree_id,
                    automation_only=source.automation_only,
                    subagent_only=source.subagent_only,
                )
                identity = derive_claude_visibility_identity(
                    candidate, self._marker_secret
                )
                candidates.append(
                    ClaudeVisibilityCandidateResult(candidate, identity, activity)
                )
        except Exception:
            return ClaudeVisibilityDiscoveryResult(
                enabled=True, degraded=True, reasons=("inventory_invalid",)
            )
        return ClaudeVisibilityDiscoveryResult(
            enabled=True,
            candidates=tuple(candidates[:bounded_limit]),
            exclusions=tuple(exclusions),
        )

    def backfill(
        self, *, days: int, limit: int, apply: bool
    ) -> ClaudeVisibilityApplyResult:
        if not self._config.claude_visibility.enabled:
            return ClaudeVisibilityApplyResult(enabled=False, mode="disabled")
        discovery = self.discover(days=days, limit=limit)
        mode = "apply" if apply else "dry_run"
        if discovery.degraded:
            return ClaudeVisibilityApplyResult(
                enabled=True,
                mode=mode,
                candidates=discovery.candidates,
                exclusions=discovery.exclusions,
                degraded=True,
                fatal_reasons=discovery.reasons,
            )
        if not apply:
            return ClaudeVisibilityApplyResult(
                enabled=True,
                mode=mode,
                candidates=discovery.candidates,
                exclusions=discovery.exclusions,
            )
        try:
            batch = tuple(
                (item.candidate, item.identity) for item in discovery.candidates[:10]
            )
            atomic = self._store.enqueue_claude_visibility_batch_if_idle(
                batch, self._marker_secret
            )
            status = atomic.get("status")
            if status == "open_work":
                return ClaudeVisibilityApplyResult(
                    enabled=True,
                    mode=mode,
                    candidates=discovery.candidates,
                    exclusions=discovery.exclusions,
                    open_reasons=("open_visibility_work",),
                )
            if status != "inserted":
                fatal = atomic.get("fatal_reasons")
                return ClaudeVisibilityApplyResult(
                    enabled=True,
                    mode=mode,
                    candidates=discovery.candidates,
                    exclusions=discovery.exclusions,
                    degraded=True,
                    fatal_reasons=tuple(fatal)
                    if isinstance(fatal, (list, tuple))
                    else ("enqueue_failed",),
                )
            applied = int(atomic.get("inserted", 0))
            duplicates = int(atomic.get("duplicates", 0))
        except Exception:
            return ClaudeVisibilityApplyResult(
                enabled=True,
                mode=mode,
                candidates=discovery.candidates,
                exclusions=discovery.exclusions,
                degraded=True,
                fatal_reasons=("enqueue_failed",),
            )
        return ClaudeVisibilityApplyResult(
            enabled=True,
            mode=mode,
            candidates=discovery.candidates,
            exclusions=discovery.exclusions,
            applied=applied,
            duplicates=duplicates,
        )

    def continuous_once(self) -> ClaudeVisibilityApplyResult:
        if not self._config.claude_visibility.enabled:
            return ClaudeVisibilityApplyResult(enabled=False, mode="disabled")
        if not self._config.claude_visibility.continuous:
            return ClaudeVisibilityApplyResult(enabled=True, mode="continuous_disabled")
        discovery = self._discover(
            days=self._config.claude_visibility.backfill_days,
            limit=1,
            manual=False,
        )
        if discovery.degraded:
            return ClaudeVisibilityApplyResult(
                enabled=True,
                mode="continuous",
                degraded=True,
                candidates=discovery.candidates,
                exclusions=discovery.exclusions,
                fatal_reasons=discovery.reasons,
            )
        try:
            candidate = next(iter(discovery.candidates), None)
            if candidate is None:
                return ClaudeVisibilityApplyResult(
                    enabled=True,
                    mode="continuous",
                    candidates=discovery.candidates,
                    exclusions=discovery.exclusions,
                )
            atomic = self._store.enqueue_claude_visibility_batch_if_idle(
                ((candidate.candidate, candidate.identity),), self._marker_secret
            )
            if atomic.get("status") == "open_work":
                return ClaudeVisibilityApplyResult(
                    enabled=True,
                    mode="continuous",
                    candidates=discovery.candidates,
                    exclusions=discovery.exclusions,
                    open_reasons=("open_visibility_work",),
                )
            if atomic.get("status") != "inserted":
                return ClaudeVisibilityApplyResult(
                    enabled=True,
                    mode="continuous",
                    degraded=True,
                    candidates=discovery.candidates,
                    exclusions=discovery.exclusions,
                    fatal_reasons=("enqueue_failed",),
                )
            applied = int(atomic.get("inserted", 0))
            duplicates = int(atomic.get("duplicates", 0))
        except Exception:
            return ClaudeVisibilityApplyResult(
                enabled=True,
                mode="continuous",
                degraded=True,
                candidates=discovery.candidates,
                exclusions=discovery.exclusions,
                fatal_reasons=("enqueue_failed",),
            )
        return ClaudeVisibilityApplyResult(
            enabled=True,
            mode="continuous",
            candidates=discovery.candidates,
            exclusions=discovery.exclusions,
            applied=applied,
            duplicates=duplicates,
        )

    def run_once(
        self, *, discover_continuous: bool = False
    ) -> ClaudeVisibilityRunResult:
        if not self._config.claude_visibility.enabled:
            return ClaudeVisibilityRunResult(enabled=False, status="disabled")

        def recorded(
            result: ClaudeVisibilityRunResult, *, registrar_result: bool = False
        ) -> ClaudeVisibilityRunResult:
            try:
                self._store.record_claude_visibility_cycle(
                    status=result.status,
                    error_code=result.error_code,
                    registrar_result=registrar_result,
                )
            except Exception as exc:
                self._log_visibility_discovery_degraded("record_cycle", exc)
                return ClaudeVisibilityRunResult(
                    enabled=True,
                    status="degraded",
                    job_id=result.job_id,
                    error_code="provider_degraded",
                    degraded=True,
                    fatal=True,
                    discovery=result.discovery,
                )
            return result

        status_before_discovery: Mapping[str, Any] | None = None
        if discover_continuous:
            try:
                status_before_discovery = self._store.claude_visibility_status(
                    float(self._clock())
                )
                open_reasons, fatal_reasons = _claude_visibility_enqueue_gates(
                    status_before_discovery
                )
            except Exception as exc:
                self._log_visibility_discovery_degraded("enqueue_gates", exc)
                return recorded(
                    ClaudeVisibilityRunResult(
                        enabled=True,
                        status="degraded",
                        degraded=True,
                        error_code="claim_failed",
                    )
                )
            discovery = (
                None
                if open_reasons or fatal_reasons
                else self.continuous_once()
            )
        else:
            discovery = None
        if discovery is not None and discovery.degraded:
            discovery_error = (
                discovery.fatal_reasons[0]
                if discovery.fatal_reasons
                else "provider_degraded"
            )
            return recorded(
                ClaudeVisibilityRunResult(
                    enabled=True,
                    status="degraded",
                    error_code=discovery_error,
                    degraded=True,
                    discovery=discovery,
                )
            )
        policy = self._config.claude_visibility
        try:
            status = (
                status_before_discovery
                if discovery is None and status_before_discovery is not None
                else self._store.claude_visibility_status(float(self._clock()))
            )
            _open, fatal_reasons = _claude_visibility_enqueue_gates(status)
            if fatal_reasons:
                return recorded(
                    ClaudeVisibilityRunResult(
                        enabled=True,
                        status="degraded",
                        error_code=fatal_reasons[0],
                        degraded=True,
                        fatal=True,
                        discovery=discovery,
                    )
                )
            claim = self._store.claim_claude_visibility_job(
                float(self._clock()),
                policy.lease_seconds,
                policy.daily_registration_limit,
                policy.emergency_daily_cost_usd,
                policy.reserved_cost_per_attempt_usd,
                policy.max_attempts,
            )
        except Exception as exc:
            self._log_visibility_discovery_degraded("claim", exc)
            return recorded(
                ClaudeVisibilityRunResult(
                    enabled=True,
                    status="degraded",
                    degraded=True,
                    error_code="claim_failed",
                    discovery=discovery,
                )
            )
        if not claim.claimed:
            if claim.status == "max_attempts_exhausted":
                return recorded(
                    ClaudeVisibilityRunResult(
                        enabled=True,
                        status="failed",
                        job_id=claim.job_id,
                        error_code="max_attempts_exhausted",
                        degraded=True,
                        fatal=True,
                        discovery=discovery,
                    )
                )
            if claim.status not in _CLAUDE_VISIBILITY_IDLE_CLAIM_STATUSES:
                return recorded(
                    ClaudeVisibilityRunResult(
                        enabled=True,
                        status="degraded",
                        job_id=claim.job_id,
                        error_code="unknown_claim_status",
                        degraded=True,
                        fatal=True,
                        discovery=discovery,
                    )
                )
            return recorded(
                ClaudeVisibilityRunResult(
                    enabled=True,
                    status=claim.status,
                    job_id=claim.job_id,
                    discovery=discovery,
                )
            )
        try:
            outcome = self._registrar.process(claim)
            status = str(getattr(outcome, "status"))
            error_code = getattr(outcome, "error_code", None)
            if status not in _CLAUDE_VISIBILITY_REGISTRAR_STATUSES:
                return recorded(
                    ClaudeVisibilityRunResult(
                        enabled=True,
                        status="degraded",
                        job_id=claim.job_id,
                        error_code="unknown_registrar_status",
                        degraded=True,
                        fatal=True,
                        discovery=discovery,
                    )
                )
            if (
                status == "retry" and error_code not in CLAUDE_VISIBILITY_RETRY_CODES
            ) or (
                status == "failed" and error_code not in CLAUDE_VISIBILITY_FATAL_CODES
            ):
                return recorded(
                    ClaudeVisibilityRunResult(
                        enabled=True,
                        status="degraded",
                        job_id=claim.job_id,
                        error_code="unknown_registrar_error_code",
                        degraded=True,
                        fatal=True,
                        discovery=discovery,
                    )
                )
            return recorded(
                ClaudeVisibilityRunResult(
                    enabled=True,
                    status=status,
                    job_id=claim.job_id,
                    error_code=error_code if isinstance(error_code, str) else None,
                    degraded=status in {"retry", "failed"},
                    fatal=status == "failed",
                    discovery=discovery,
                ),
                registrar_result=True,
            )
        except Exception:
            return recorded(
                ClaudeVisibilityRunResult(
                    enabled=True,
                    status="degraded",
                    job_id=claim.job_id,
                    error_code="registrar_failed",
                    degraded=True,
                    discovery=discovery,
                )
            )


class ContinuationBlockedError(RuntimeError):
    """Visible fixed-code refusal before a continuation can create or hydrate."""

    def __init__(self, code: str, warning: str) -> None:
        if code not in {
            "source_cwd_missing",
            "source_identity_mismatch",
            "permission_preflight_failed",
        }:
            raise ValueError("invalid continuation blocking code")
        self.code = code
        self.warnings = (warning,)
        super().__init__(code)


_AsyncSleep = Callable[[float], Awaitable[None]]
_AWatchFactory = Callable[..., Any]


class _SidebarVerifier(Protocol):
    def verify_thread(
        self, *, thread_id: str, expected: BridgeMarkerPayload
    ) -> VerifiedSidebarThread: ...

    def reconcile_marker(
        self,
        expected: BridgeMarkerPayload,
        *,
        now: float,
        ttl_seconds: float,
    ) -> SidebarReconciliationEvidence: ...


class _SidebarExecutor(Protocol):
    def run_once(self) -> object: ...


_ProviderHealth = dict[str, float | str | None]
_RECENT_ERROR_LIMIT = 20
_CODEX_SCAN_FAILURE_CODE = "codex_scan_failed"
_CODEX_SCAN_LOCAL_OWNER_CODE = "codex_local_session_owns_id"
_CODEX_SCAN_STAGES = frozenset({
    "full_history_project",
    "immediate_project",
    "persistent_project",
})
_CODEX_STDERR_TAIL_LINES = 12
_CODEX_DIAGNOSTIC_MAX_LINES = 8
_CODEX_DIAGNOSTIC_MAX_CHARS = 2048
_WINDOWS_TERMINAL_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]+|[\\/]{2}(?:\?[\\/]+(?:UNC[\\/]+)?|[^\\/\s]+[\\/]+))"
    r"[^\r\n'\"<>|]*$"
)
_POSIX_TERMINAL_PATH_RE = re.compile(r"/(?:[^\r\n'\"<>|/]+/)*[^\r\n'\"<>|]*$")
_PATH_FRAGMENT_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]|/|(?:\.{1,2}|~)[\\/]|\b[^\s'\"<>|\\/]+[\\/])"
    r"(?:[^\s'\"<>|\\/]+[\\/])*[^\s'\"<>|]*"
)
_ATTEMPT_KEY_PREFIX = "session-bridge:attempt:"
_BREAKER_STATE_KEY = "session-bridge:mirror-breaker"
_RATE_STATE_KEY = "session-bridge:mirror-rate"
_CLAUDE_CURSOR_KEY = "session-bridge:scan:claude:cursors"
_CLAUDE_FINGERPRINT_KEY = "session-bridge:scan:claude:fingerprints"
_CLAUDE_STAGED_KEY = "session-bridge:scan:claude:staged-fingerprints"
_CODEX_SEEN_KEY = "session-bridge:scan:codex:seen"
_CONTINUATION_RECONCILE_CURSOR_KEY = "session-bridge:reconcile:continuation-cursor"
_CONTINUATION_RECONCILE_BATCH_SIZE = 5
_EXTERNAL_PROVIDERS = (Provider.CLAUDE, Provider.CODEX)
_PENDING_KEYS = {
    Provider.CLAUDE: "session-bridge:scan:claude:pending",
    Provider.CODEX: "session-bridge:scan:codex:pending",
}
_PROGRESS_KEYS = {
    Provider.CLAUDE: "session-bridge:scan:claude:progress",
    Provider.CODEX: "session-bridge:scan:codex:progress",
}
_BACKFILL_KEYS = {
    Provider.CLAUDE: "session-bridge:backfill:claude",
    Provider.CODEX: "session-bridge:backfill:codex",
}
_MIRROR_WORKER_LOCK_POLL_SECONDS = 0.05
_SIDEBAR_REGISTRATION_CURSOR_KEY = "session-bridge:sidebar:registration-cursor"
_SIDEBAR_REGISTRATION_PROBE_FRONTIER_KEY = (
    "session-bridge:sidebar:registration-probe-frontier"
)
_SIDEBAR_REGISTRATION_CATCHUP_STATE_KEY = (
    "session-bridge:sidebar:registration-catchup"
)
_SIDEBAR_REGISTRATION_CURSOR_VERSION = 1
# Bound database work independently from the number of jobs requested by a caller.
_SIDEBAR_REGISTRATION_QUERY_BUDGET = 4
_SIDEBAR_REGISTRATION_EXAMINED_BUDGET = 40
_SIDEBAR_REGISTRATION_PAGE_SIZE = 10
_SIDEBAR_NEWEST_PROBE_SIZE = 30
_CLAUDE_HOT_STAT_WINDOW_SECONDS = 86_400.0
_CLAUDE_COLD_STAT_SWEEP_SECONDS = 60.0
_SIDEBAR_BACKFILL_QUERY_BUDGET = 100
_SIDEBAR_BACKFILL_EXAMINED_BUDGET = 1000
_USE_CONFIGURED_BACKFILL = object()
# Foreground cancellation recovery is intentionally short. Unfinished ownership
# transfers to tracked background recovery, with the durable 300-second lease as
# the final fallback if a synchronous worker never returns.
_SIDEBAR_CANCELLATION_RECOVERY_SECONDS = 5.0


class SessionBridgeCoordinator:
    def __init__(
        self,
        *,
        config: BridgeConfig,
        store: object,
        adapters: Mapping[Provider, object],
        target_adapters: Mapping[Provider, object] | None = None,
        context_builder: object | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: _AsyncSleep = asyncio.sleep,
        awatch_factory: _AWatchFactory | None = None,
        scan_batch_size: int = 100,
        claude_projects_root: Path | None = None,
        watch_debounce_seconds: float = 0.25,
        refresh_timeout: float = 60.0,
        sidebar_verifier: _SidebarVerifier | None = None,
        sidebar_executor: _SidebarExecutor | None = None,
        sidebar_cancellation_recovery_timeout: float = (
            _SIDEBAR_CANCELLATION_RECOVERY_SECONDS
        ),
        permission_preflight: Callable[[str], bool] | None = None,
        mirror_float: object | None = None,
    ) -> None:
        if type(scan_batch_size) is not int or scan_batch_size <= 0:
            raise ValueError("scan_batch_size must be a positive integer")
        if claude_projects_root is not None and not isinstance(
            claude_projects_root, Path
        ):
            raise TypeError("claude_projects_root must be a Path or None")
        if (
            not isinstance(watch_debounce_seconds, (int, float))
            or isinstance(watch_debounce_seconds, bool)
            or not math.isfinite(float(watch_debounce_seconds))
            or watch_debounce_seconds <= 0
        ):
            raise ValueError("watch_debounce_seconds must be a positive number")
        if (
            not isinstance(refresh_timeout, (int, float))
            or isinstance(refresh_timeout, bool)
            or not math.isfinite(float(refresh_timeout))
            or refresh_timeout <= 0
        ):
            raise ValueError("refresh_timeout must be a positive number")
        if (
            not isinstance(sidebar_cancellation_recovery_timeout, (int, float))
            or isinstance(sidebar_cancellation_recovery_timeout, bool)
            or not math.isfinite(float(sidebar_cancellation_recovery_timeout))
            or not 0 < sidebar_cancellation_recovery_timeout <= 5.0
        ):
            raise ValueError(
                "sidebar cancellation recovery timeout must be between zero and five seconds"
            )
        self._config = config
        self._store = store
        self._adapters = dict(adapters)
        self._target_adapters = dict(target_adapters or {})
        self._context_builder = context_builder
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._awatch_factory = awatch_factory
        self._scan_batch_size = scan_batch_size
        self._claude_projects_root = claude_projects_root
        self._claude_stat_cache = _ClaudeStatCache(monotonic=monotonic)
        self._watch_debounce_seconds = float(watch_debounce_seconds)
        self._refresh_timeout = float(refresh_timeout)
        self._sidebar_verifier = sidebar_verifier
        if sidebar_executor is not None and not callable(
            getattr(sidebar_executor, "run_once", None)
        ):
            raise TypeError("sidebar_executor must provide run_once() or be None")
        self._sidebar_executor = sidebar_executor
        if mirror_float is not None and not callable(
            getattr(mirror_float, "run_once", None)
        ):
            raise TypeError("mirror_float must provide run_once() or be None")
        self._mirror_float = mirror_float
        self._sidebar_cancellation_recovery_timeout = float(
            sidebar_cancellation_recovery_timeout
        )
        if permission_preflight is not None and not callable(permission_preflight):
            raise TypeError("permission_preflight must be callable or None")
        self._permission_preflight = permission_preflight
        self._watch_stop_event: asyncio.Event | None = None
        self._watcher_state = "not_started"
        self._watcher_error_code: str | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._scan_locks = {
            provider: asyncio.Lock() for provider in _EXTERNAL_PROVIDERS
        }
        self._job_lock = asyncio.Lock()
        self._sidebar_registration_lock = asyncio.Lock()
        self._continuation_locks: dict[str, asyncio.Lock] = {}
        self._running = False
        self._initial_reconcile_done = asyncio.Event()
        self._background_tasks: list[asyncio.Task[None]] = []
        self._provider_tasks: set[asyncio.Task[Any]] = set()
        self._claude_immediate_cursors: dict[str, ClaudeCursor] = {}
        self._sidebar_recovery_tasks: set[asyncio.Task[Any]] = set()
        self._provider_health: dict[Provider, _ProviderHealth] = {
            provider: {
                "last_success": None,
                "lag_seconds": None,
                "degraded_reason": None,
            }
            for provider in (Provider.CLAUDE, Provider.CODEX)
        }
        self._recent_error_codes: list[str] = []
        self._backfill_progress: dict[Provider, dict[str, int | str]] = {}
        self._continuous_watermark: float | None = None
        self._registration_turn_fallback: bool | None = None
        self._sidebar_registration_counts = {
            "examined": 0,
            "queued": 0,
            Provider.CLAUDE.value: 0,
            Provider.HERMES.value: 0,
            "failed": 0,
            "excluded": 0,
            "excluded_by_reason": {reason: 0 for reason in SIDEBAR_EXCLUSION_REASONS},
        }

    @asynccontextmanager
    async def _mirror_worker_critical_section(self) -> AsyncIterator[None]:
        acquire = getattr(self._store, "try_acquire_mirror_worker_lock", None)
        if not callable(acquire):
            yield
            return

        handle = None
        while handle is None:
            handle = await asyncio.to_thread(acquire)
            if handle is None:
                await self._sleep(_MIRROR_WORKER_LOCK_POLL_SECONDS)
        release = getattr(handle, "release", None)
        if not callable(release):
            raise RuntimeError("mirror worker lock handle must provide release()")
        try:
            yield
        finally:
            release_task = asyncio.create_task(asyncio.to_thread(release))
            try:
                await asyncio.shield(release_task)
            except asyncio.CancelledError:
                await release_task
                raise

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._running:
                return
            while self._provider_tasks:
                await asyncio.gather(
                    *tuple(self._provider_tasks),
                    return_exceptions=True,
                )
            self._initial_reconcile_done = asyncio.Event()
            self._running = True
            self._background_tasks = [
                asyncio.create_task(self._reconcile_loop()),
                *(
                    asyncio.create_task(self._scan_loop(provider))
                    for provider in _EXTERNAL_PROVIDERS
                ),
            ]
            if self._claude_projects_root is not None:
                self._watch_stop_event = asyncio.Event()
                self._watcher_state = "running"
                self._watcher_error_code = None
                self._background_tasks.append(asyncio.create_task(self._watch_loop()))

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            if (
                not self._running
                and not self._background_tasks
                and not self._provider_tasks
                and not self._sidebar_recovery_tasks
            ):
                return
            self._running = False
            if self._watch_stop_event is not None:
                self._watch_stop_event.set()
            tasks, self._background_tasks = self._background_tasks, []
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            provider_tasks = tuple(self._provider_tasks)
            if provider_tasks:
                _, pending = await asyncio.wait(
                    provider_tasks,
                    timeout=self._refresh_timeout,
                )
                if pending:
                    self._record_error_code("provider_shutdown_pending")
            for task in tuple(self._sidebar_recovery_tasks):
                task.cancel()
            if self._claude_projects_root is not None:
                self._watcher_state = "stopped"

    async def scan_once(self, provider: Provider | None = None) -> ScanSummary:
        await self._ensure_continuous_watermark()
        if not self._config.catalog.enabled:
            return _zero_scan(provider)
        if provider is not None:
            normalized = Provider(provider)
            if normalized not in _EXTERNAL_PROVIDERS:
                raise ValueError("scan provider must be Claude or Codex")
            summary = await self._scan_provider(normalized)
            await self._after_successful_scan(summary)
            return summary

        summaries = [
            await self._scan_provider(candidate)
            for candidate in (Provider.CLAUDE, Provider.CODEX)
        ]
        summary = ScanSummary(
            provider=None,
            discovered=sum(summary.discovered for summary in summaries),
            indexed=sum(summary.indexed for summary in summaries),
            rebuilt=sum(summary.rebuilt for summary in summaries),
            failed=sum(summary.failed for summary in summaries),
            duration_ms=sum(summary.duration_ms for summary in summaries),
        )
        await self._after_successful_scan(summary)
        return summary

    async def scan_all_history(self, provider: Provider | None = None) -> ScanSummary:
        """Index one complete provider inventory without mirror side effects."""

        if not self._config.catalog.enabled:
            return _zero_scan(provider)
        if provider is not None:
            normalized = Provider(provider)
            if normalized not in _EXTERNAL_PROVIDERS:
                raise ValueError("scan provider must be Claude or Codex")
            return await self._scan_all_history_provider(normalized)

        summaries = [
            await self._scan_all_history_provider(candidate)
            for candidate in (Provider.CLAUDE, Provider.CODEX)
        ]
        return ScanSummary(
            provider=None,
            discovered=sum(summary.discovered for summary in summaries),
            indexed=sum(summary.indexed for summary in summaries),
            rebuilt=sum(summary.rebuilt for summary in summaries),
            failed=sum(summary.failed for summary in summaries),
            duration_ms=sum(summary.duration_ms for summary in summaries),
        )

    async def register_sidebar_jobs_once(
        self,
        *,
        now: float | None = None,
        limit: int = 100,
    ) -> SidebarRegistrationSummary:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("sidebar registration limit must be between 1 and 1000")
        registration_time = _finite_number(
            self._clock() if now is None else now,
            "now",
        )
        if not self._config.sidebar.enabled:
            summary = SidebarRegistrationSummary(
                examined=0,
                queued=0,
                by_provider={Provider.CLAUDE.value: 0, Provider.HERMES.value: 0},
                failed=0,
                excluded=0,
                excluded_by_reason={reason: 0 for reason in SIDEBAR_EXCLUSION_REASONS},
            )
            self._set_sidebar_registration_counts(summary)
            return summary

        async with self._sidebar_registration_lock:
            return await self._register_sidebar_jobs_locked(
                registration_time=registration_time,
                limit=limit,
            )

    async def backfill_sidebar_jobs_once(
        self,
        *,
        days: int | None = 30,
        limit: int = 10,
        apply: bool = False,
        now: float | None = None,
    ) -> SidebarRegistrationSummary:
        if days is not None and (type(days) is not int or not 1 <= days <= 30):
            raise ValueError("sidebar backfill days must be between 1 and 30")
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("sidebar backfill limit must be between 1 and 10")
        if type(apply) is not bool:
            raise ValueError("sidebar backfill apply flag must be a boolean")
        registration_time = _finite_number(
            self._clock() if now is None else now,
            "now",
        )
        if not self._config.sidebar.enabled:
            return SidebarRegistrationSummary(
                examined=0,
                queued=0,
                by_provider={Provider.CLAUDE.value: 0, Provider.HERMES.value: 0},
                failed=0,
                excluded=0,
                excluded_by_reason={reason: 0 for reason in SIDEBAR_EXCLUSION_REASONS},
            )
        async with self._sidebar_registration_lock:
            return await self._register_sidebar_jobs_locked(
                registration_time=registration_time,
                limit=limit,
                backfill_days=days,
                apply=apply,
                persist_cursor=False,
                record_summary=apply,
            )

    async def claim_sidebar_jobs_for_delivery(
        self,
        *,
        now: float | None = None,
        limit: int = 1,
    ) -> tuple[SidebarDeliveryClaim, ...]:
        if type(limit) is not int or limit != 1:
            raise ValueError("sidebar delivery limit must be exactly one")
        claim_time = _finite_number(self._clock() if now is None else now, "now")
        await self._record_sidebar_broker_heartbeat(claim_time)
        if not self._config.sidebar.enabled:
            return ()
        verifier = self._sidebar_verifier
        if verifier is None:
            return ()
        claim_task = asyncio.create_task(
            asyncio.to_thread(
                _call,
                self._store,
                "claim_sidebar_jobs",
                now=claim_time,
                limit=limit,
                lease_seconds=self._config.sidebar.lease_seconds,
            )
        )
        cancelled_claim: asyncio.CancelledError | None = None
        try:
            raw_claims = await asyncio.shield(claim_task)
        except asyncio.CancelledError as exc:
            cancelled_claim = exc
            raw_claims = None
        if cancelled_claim is not None:
            await self._finish_cancelled_sidebar_claim(
                claim_task,
                cancelled=cancelled_claim,
                limit=limit,
            )
        owned_tokens, malformed_claims = _sidebar_claim_tokens(
            raw_claims,
            limit=limit,
        )
        if malformed_claims:
            await self._cleanup_sidebar_delivery_claims(owned_tokens)
            raise ValueError("sidebar delivery claims are malformed")
        assert isinstance(raw_claims, list)
        known_thread_ids: dict[str, str] = {}

        try:
            delivery: list[SidebarDeliveryClaim] = []
            for raw_claim, lease_token in zip(raw_claims, owned_tokens, strict=True):
                assert isinstance(raw_claim, Mapping)
                source_session_id = _exact_sidebar_claim_text(
                    raw_claim.get("source_session_id"), "source session ID"
                )
                bridge_id = _exact_sidebar_claim_text(
                    raw_claim.get("bridge_id"), "bridge ID"
                )
                expected = BridgeMarkerPayload(
                    bridge_id=bridge_id,
                    source_session_id=source_session_id,
                    target_provider=Provider.CODEX,
                    policy_generation=1,
                )
                raw_reserved_thread_id = raw_claim.get("codex_thread_id")
                reserved_thread_id = (
                    None
                    if raw_reserved_thread_id is None
                    else _exact_sidebar_claim_text(
                        raw_reserved_thread_id,
                        "reserved Codex thread ID",
                    )
                )
                create_reserved = False
                if reserved_thread_id is not None:
                    known_thread_ids[lease_token] = reserved_thread_id
                if reserved_thread_id is None:
                    try:
                        reservation_method = getattr(
                            self._store,
                            "get_sidebar_create_reservation",
                        )
                        if not callable(reservation_method):
                            raise TypeError("sidebar reservation lookup is unavailable")
                        reservation = await asyncio.to_thread(
                            reservation_method,
                            source_session_id,
                        )
                    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                        raise
                    except Exception:
                        await self._fail_sidebar_delivery_claim(
                            lease_token,
                            "bridge_temporarily_unavailable",
                        )
                        continue
                    create_reserved = reservation is not None
                try:
                    reconcile_method = getattr(verifier, "reconcile_marker")
                    if not callable(reconcile_method):
                        raise TypeError("sidebar reconciler is unavailable")
                    evidence = await asyncio.to_thread(
                        reconcile_method,
                        expected,
                        now=claim_time,
                        ttl_seconds=_sidebar_reconciliation_proof_ttl_seconds(
                            self._config.service.reconcile_seconds
                        ),
                    )
                    if not isinstance(evidence, SidebarReconciliationEvidence):
                        raise TypeError("sidebar reconciliation evidence is malformed")
                    proof = await asyncio.to_thread(
                        _call,
                        self._store,
                        "record_sidebar_reconciliation_proof",
                        lease_token=lease_token,
                        evidence=evidence,
                        marker_digest=evidence.marker_digest,
                        placement_generation=self._config.sidebar.placement_generation,
                        delivery_generation=1,
                        now=claim_time,
                    )
                    if not isinstance(proof, Mapping):
                        raise TypeError("sidebar reconciliation proof is malformed")
                    proof_digest = _exact_sidebar_claim_text(
                        proof.get("proof_digest"),
                        "reconciliation proof digest",
                    )
                    proof_generation = _exact_sidebar_claim_text(
                        proof.get("reconciliation_generation"),
                        "reconciliation generation",
                    )
                    if (
                        proof_generation != evidence.generation
                        or proof.get("state") != evidence.state.value
                        or proof.get("recovered_thread_id")
                        != evidence.recovered_thread_id
                    ):
                        raise ValueError("sidebar reconciliation proof mismatch")
                except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                    raise
                except SidebarVerificationError as exc:
                    await self._fail_sidebar_delivery_claim(
                        lease_token,
                        _sidebar_reconciliation_failure_code(exc.code),
                        codex_thread_id=reserved_thread_id,
                    )
                    continue
                except Exception:
                    await self._fail_sidebar_delivery_claim(
                        lease_token,
                        "bridge_temporarily_unavailable",
                        codex_thread_id=reserved_thread_id,
                    )
                    continue

                if evidence.state is SidebarReconciliationState.BLOCKED:
                    assert evidence.fixed_reason is not None
                    await self._fail_sidebar_delivery_claim(
                        lease_token,
                        _sidebar_reconciliation_failure_code(evidence.fixed_reason),
                        codex_thread_id=reserved_thread_id,
                    )
                    continue
                if evidence.state is SidebarReconciliationState.ABSENCE_PROVEN:
                    if create_reserved:
                        await self._fail_sidebar_delivery_claim(
                            lease_token,
                            "native_create_ambiguous",
                        )
                        continue
                    if reserved_thread_id is not None:
                        await self._fail_sidebar_delivery_claim(
                            lease_token,
                            "source_identity_mismatch",
                            codex_thread_id=reserved_thread_id,
                        )
                        continue
                    recovered_thread_id = None
                    create_eligible = True
                    rename_required = False
                elif evidence.state is SidebarReconciliationState.RECOVERED:
                    recovered_thread_id = _exact_sidebar_claim_text(
                        evidence.recovered_thread_id,
                        "recovered Codex thread ID",
                    )
                    if (
                        reserved_thread_id is not None
                        and reserved_thread_id != recovered_thread_id
                    ):
                        await self._fail_sidebar_delivery_claim(
                            lease_token,
                            "source_identity_mismatch",
                            codex_thread_id=reserved_thread_id,
                        )
                        continue
                    known_thread_ids[lease_token] = recovered_thread_id
                    try:
                        await asyncio.to_thread(
                            _call,
                            self._store,
                            "bind_sidebar_thread",
                            lease_token=lease_token,
                            codex_thread_id=recovered_thread_id,
                            now=claim_time,
                        )
                    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                        raise
                    except Exception:
                        await self._fail_sidebar_delivery_claim(
                            lease_token,
                            "bridge_temporarily_unavailable",
                            codex_thread_id=recovered_thread_id,
                        )
                        continue
                    create_eligible = False
                    rename_required = True
                else:
                    await self._fail_sidebar_delivery_claim(
                        lease_token,
                        "bridge_temporarily_unavailable",
                        codex_thread_id=reserved_thread_id,
                    )
                    continue
                delivery.append(
                    SidebarDeliveryClaim(
                        lease_token=lease_token,
                        source_session_id=source_session_id,
                        bridge_id=bridge_id,
                        reconciliation_state=evidence.state,
                        reconciliation_generation=proof_generation,
                        reconciliation_proof_digest=proof_digest,
                        recovered_thread_id=recovered_thread_id,
                        create_eligible=create_eligible,
                        rename_required=rename_required,
                        create_reserved=False,
                    )
                )
            return tuple(delivery)
        except asyncio.CancelledError as cancelled:
            try:
                await self._cleanup_sidebar_delivery_claims(
                    owned_tokens,
                    known_thread_ids=known_thread_ids,
                )
            except BaseException:
                pass
            _raise_detached_cancelled(cancelled)
        except BaseException:
            await self._cleanup_sidebar_delivery_claims(
                owned_tokens,
                known_thread_ids=known_thread_ids,
            )
            raise

    async def claim_sidebar_hydration_for_delivery(
        self,
        *,
        limit: int = 1,
    ) -> tuple[SidebarHydrationClaim, ...]:
        if type(limit) is not int or limit != 1:
            raise ValueError("sidebar hydration delivery limit must be exactly one")
        claim_time = _finite_number(self._clock(), "now")
        await self._record_sidebar_broker_heartbeat(claim_time)
        if not self._config.sidebar.legacy_hydration_enabled:
            return ()
        raw_claims = await asyncio.to_thread(
            _call,
            self._store,
            "claim_sidebar_hydration_jobs",
            now=claim_time,
            limit=limit,
        )
        if not isinstance(raw_claims, list) or len(raw_claims) > 1:
            raise ValueError("sidebar hydration claims are malformed")

        delivery: list[SidebarHydrationClaim] = []
        for raw in raw_claims:
            if not isinstance(raw, Mapping):
                raise ValueError("sidebar hydration claim is malformed")
            lease_token = _exact_sidebar_claim_text(
                raw.get("lease_token"),
                "hydration lease token",
            )
            source_session_id = _exact_sidebar_claim_text(
                raw.get("source_session_id"),
                "hydration source session ID",
            )
            bridge_id = _exact_sidebar_claim_text(
                raw.get("bridge_id"),
                "hydration bridge ID",
            )
            thread_id = _exact_sidebar_claim_text(
                raw.get("codex_thread_id"),
                "hydration Codex thread ID",
            )
            source_cursor = _exact_sidebar_claim_text(
                raw.get("source_cursor"),
                "hydration source cursor",
            )
            source_hash = _exact_sidebar_claim_text(
                raw.get("source_hash"),
                "hydration source hash",
            )
            preview_digest = _exact_sidebar_claim_text(
                raw.get("preview_digest"),
                "hydration preview digest",
            )
            hydration_marker = _exact_sidebar_claim_text(
                raw.get("hydration_marker"),
                "hydration marker",
            )
            preview_version = raw.get("preview_version")
            send_reserved = raw.get("send_reserved")
            if type(preview_version) is not int or preview_version != 1:
                await self._fail_sidebar_hydration_claim(
                    lease_token,
                    "preview_digest_mismatch",
                    thread_id,
                )
                continue
            if type(send_reserved) is not bool:
                raise ValueError("hydration reservation flag is malformed")
            try:
                candidate = await asyncio.to_thread(
                    _call,
                    self._store,
                    "get_sidebar_candidate_for_delivery",
                    source_session_id,
                )
                if (
                    not isinstance(candidate, SidebarCandidate)
                    or candidate.source_session_id != source_session_id
                    or candidate.bridge_id != bridge_id
                ):
                    raise ValueError("hydration candidate identity mismatch")
                if send_reserved:
                    message = build_hydration_message(
                        preview_rendered=None,
                        source_session_id=source_session_id,
                        hydration_marker=hydration_marker,
                        send_reserved=True,
                    )
                else:
                    snapshot = await asyncio.to_thread(
                        _call,
                        self._store,
                        "get_sidebar_preview_source",
                        source_session_id,
                    )
                    if not isinstance(snapshot, Mapping):
                        raise ValueError("hydration source snapshot is malformed")
                    if (
                        snapshot.get("source_session_id") != source_session_id
                        or snapshot.get("provider") != candidate.provider.value
                        or snapshot.get("source_cursor") != source_cursor
                        or snapshot.get("source_hash") != source_hash
                    ):
                        raise ValueError("hydration source identity mismatch")
                    preview = build_session_preview(
                        source_session_id=source_session_id,
                        source_cursor=source_cursor,
                        source_hash=source_hash,
                        title=cast(str | None, snapshot.get("title")),
                        provider=candidate.provider.value,
                        cwd=candidate.cwd,
                        captured_at=snapshot.get("captured_at"),
                        messages=cast(
                            Sequence[Mapping[str, Any]],
                            snapshot.get("messages"),
                        ),
                        git_root=candidate.git_root,
                        git_branch=candidate.git_branch,
                        git_head=candidate.git_head,
                        worktree_id=candidate.worktree_id,
                        budget_chars=self._config.sidebar.preview_budget_chars,
                    )
                    if (
                        preview.version != preview_version
                        or preview.digest != preview_digest
                    ):
                        await self._fail_sidebar_hydration_claim(
                            lease_token,
                            "preview_digest_mismatch",
                            thread_id,
                        )
                        continue
                    message = build_hydration_message(
                        preview_rendered=preview.rendered,
                        source_session_id=source_session_id,
                        hydration_marker=hydration_marker,
                        send_reserved=False,
                    )
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                await self._fail_sidebar_hydration_claim(
                    lease_token,
                    "source_identity_mismatch",
                    thread_id,
                )
                continue
            delivery.append(
                SidebarHydrationClaim(
                    lease_token=lease_token,
                    source_session_id=source_session_id,
                    bridge_id=bridge_id,
                    codex_thread_id=thread_id,
                    source_cursor=source_cursor,
                    source_hash=source_hash,
                    preview_version=preview_version,
                    preview_digest=preview_digest,
                    hydration_marker=hydration_marker,
                    hydration_message=message,
                    cwd=candidate.cwd,
                    git_root=candidate.git_root,
                    send_reserved=send_reserved,
                )
            )
        return tuple(delivery)

    async def _record_sidebar_broker_heartbeat(self, now: float) -> None:
        heartbeat = getattr(self._store, "record_sidebar_broker_heartbeat", None)
        if callable(heartbeat):
            await asyncio.to_thread(heartbeat, now=now)

    async def _fail_sidebar_hydration_claim(
        self,
        lease_token: str,
        error_code: str,
        codex_thread_id: str,
    ) -> None:
        await asyncio.to_thread(
            _call,
            self._store,
            "fail_sidebar_hydration_job",
            lease_token=lease_token,
            error_code=error_code,
            codex_thread_id=codex_thread_id,
            now=_finite_number(self._clock(), "now"),
        )

    async def _finish_cancelled_sidebar_claim(
        self,
        claim_task: asyncio.Task[Any],
        *,
        cancelled: asyncio.CancelledError,
        limit: int,
    ) -> NoReturn:
        """Recover and release leases committed by a cancelled claim worker."""

        finished, _ = await self._wait_sidebar_recovery_task(claim_task)
        if not finished:
            recovery_task = asyncio.create_task(
                self._recover_sidebar_claim_in_background(claim_task, limit=limit)
            )
            self._track_sidebar_recovery_task(recovery_task)
            _raise_detached_cancelled(cancelled)
        worker_failed = False
        try:
            raw_claims = claim_task.result()
        except BaseException:
            worker_failed = True
            raw_claims = None
        if worker_failed:
            _raise_detached_cancelled(cancelled)
        extraction_failed = False
        try:
            owned_tokens, _ = _sidebar_claim_tokens(raw_claims, limit=limit)
        except BaseException:
            extraction_failed = True
            owned_tokens = []
        if extraction_failed:
            _raise_detached_cancelled(cancelled)
        try:
            await self._cleanup_sidebar_delivery_claims(owned_tokens)
        except BaseException:
            pass
        _raise_detached_cancelled(cancelled)

    async def _recover_sidebar_claim_in_background(
        self,
        claim_task: asyncio.Task[Any],
        *,
        limit: int,
    ) -> None:
        while not claim_task.done():
            try:
                await asyncio.shield(claim_task)
            except asyncio.CancelledError:
                # stop() must return promptly, but the wrapper retains ownership
                # so the worker result is still consumed if it eventually arrives.
                continue
            except BaseException:
                return
        try:
            raw_claims = claim_task.result()
        except BaseException:
            return
        try:
            owned_tokens, _ = _sidebar_claim_tokens(raw_claims, limit=limit)
            await self._cleanup_sidebar_delivery_claims(owned_tokens)
        except BaseException:
            return

    async def _wait_sidebar_recovery_task(
        self,
        task: asyncio.Task[Any],
    ) -> tuple[bool, asyncio.CancelledError | None]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._sidebar_cancellation_recovery_timeout
        cancelled: asyncio.CancelledError | None = None
        while not task.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait((task,), timeout=remaining)
            except asyncio.CancelledError as exc:
                if cancelled is None:
                    cancelled = exc
                continue
        return task.done(), cancelled

    def _track_sidebar_recovery_task(self, task: asyncio.Task[Any]) -> None:
        self._sidebar_recovery_tasks.add(task)
        task.add_done_callback(self._sidebar_recovery_done)

    def _sidebar_recovery_done(self, task: asyncio.Task[Any]) -> None:
        self._sidebar_recovery_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.exception()
        except BaseException:
            pass

    async def _fail_sidebar_delivery_claim(
        self,
        lease_token: str,
        error_code: str,
        *,
        codex_thread_id: str | None = None,
    ) -> bool:
        failure_time = _finite_number(self._clock(), "now")
        try:
            arguments: dict[str, object] = {
                "lease_token": lease_token,
                "error_code": error_code,
                "now": failure_time,
            }
            if codex_thread_id is not None:
                arguments["codex_thread_id"] = codex_thread_id
            await asyncio.to_thread(
                _call,
                self._store,
                "fail_sidebar_job",
                **arguments,
            )
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            self._record_error_code("sidebar_delivery_failure_stale")
            return False
        return True

    async def _cleanup_sidebar_delivery_claims(
        self,
        lease_tokens: Sequence[str],
        *,
        known_thread_ids: Mapping[str, str] | None = None,
    ) -> None:
        async def _cleanup() -> None:
            for lease_token in dict.fromkeys(lease_tokens):
                try:
                    arguments: dict[str, object] = {
                        "lease_token": lease_token,
                        "error_code": "broker_time_budget",
                        "now": _finite_number(self._clock(), "now"),
                    }
                    if known_thread_ids is not None:
                        thread_id = known_thread_ids.get(lease_token)
                        if thread_id is not None:
                            arguments["codex_thread_id"] = thread_id
                    await asyncio.to_thread(
                        _call,
                        self._store,
                        "fail_sidebar_job",
                        **arguments,
                    )
                except (KeyboardInterrupt, SystemExit):
                    raise
                except BaseException:
                    self._record_error_code("sidebar_delivery_failure_stale")
                    continue

        cleanup_task = asyncio.create_task(_cleanup())
        finished, cancelled = await self._wait_sidebar_recovery_task(cleanup_task)
        if not finished:
            self._track_sidebar_recovery_task(cleanup_task)
        elif cancelled is None:
            cleanup_task.result()
        elif not cleanup_task.cancelled():
            cleanup_task.exception()
        if cancelled is not None:
            _raise_detached_cancelled(cancelled)

    async def bind_sidebar_thread(
        self,
        *,
        lease_token: str,
        codex_thread_id: str,
    ) -> Mapping[str, Any]:
        token = _exact_sidebar_claim_text(lease_token, "lease token")
        thread_id = _exact_sidebar_claim_text(codex_thread_id, "Codex thread ID")
        result = await asyncio.to_thread(
            _call,
            self._store,
            "bind_sidebar_thread",
            lease_token=token,
            codex_thread_id=thread_id,
            now=_finite_number(self._clock(), "now"),
        )
        if (
            not isinstance(result, Mapping)
            or result.get("state") != "sidebar_leased"
            or result.get("codex_thread_id") != thread_id
        ):
            raise ValueError("sidebar bind result is malformed")
        return result

    async def reserve_sidebar_create_authoritatively(
        self,
        *,
        lease_token: str,
        reconciliation_proof_digest: str,
        reconciliation_generation: str,
        now: float | None = None,
    ) -> Mapping[str, Any]:
        """Reconcile once more, then bind recovery or reserve exact absence."""

        token = _exact_sidebar_claim_text(lease_token, "lease token")
        supplied_digest = _exact_sidebar_proof_digest(
            reconciliation_proof_digest
        )
        supplied_generation = _exact_sidebar_claim_text(
            reconciliation_generation,
            "reconciliation generation",
        )
        reserve_time = _finite_number(self._clock() if now is None else now, "now")
        verifier = self._sidebar_verifier
        if verifier is None:
            raise ValueError("sidebar reconciler is unavailable")

        raw_job = await asyncio.to_thread(
            _call,
            self._store,
            "lookup_sidebar_job_by_lease",
            token,
        )
        if not isinstance(raw_job, Mapping) or raw_job.get("state") != "sidebar_leased":
            raise ValueError("sidebar lease identity is malformed")
        job_id = _exact_sidebar_claim_text(raw_job.get("id"), "job ID")
        source_session_id = _exact_sidebar_claim_text(
            raw_job.get("source_session_id"), "source session ID"
        )
        bridge_id = _exact_sidebar_claim_text(raw_job.get("bridge_id"), "bridge ID")
        if raw_job.get("codex_thread_id") is not None:
            raise ValueError("sidebar lease already has a native thread")

        current_proof = await asyncio.to_thread(
            _call,
            self._store,
            "get_sidebar_reconciliation_proof",
            lease_token=token,
        )
        if not isinstance(current_proof, Mapping):
            raise ValueError("sidebar reconciliation proof is missing")
        if (
            current_proof.get("job_id") != job_id
            or current_proof.get("source_session_id") != source_session_id
            or current_proof.get("bridge_id") != bridge_id
            or current_proof.get("proof_digest") != supplied_digest
            or current_proof.get("reconciliation_generation")
            != supplied_generation
            or current_proof.get("state")
            != SidebarReconciliationState.ABSENCE_PROVEN.value
            or current_proof.get("recovered_thread_id") is not None
        ):
            raise ValueError("sidebar reconciliation proof is stale")

        existing_reservation = await asyncio.to_thread(
            _call,
            self._store,
            "get_sidebar_create_reservation",
            source_session_id,
        )
        if existing_reservation is not None:
            raise ValueError("sidebar create reservation already exists")

        expected = BridgeMarkerPayload(
            bridge_id=bridge_id,
            source_session_id=source_session_id,
            target_provider=Provider.CODEX,
            policy_generation=1,
        )
        try:
            evidence = await asyncio.to_thread(
                verifier.reconcile_marker,
                expected,
                now=reserve_time,
                ttl_seconds=_sidebar_reconciliation_proof_ttl_seconds(
                    self._config.service.reconcile_seconds
                ),
            )
            if not isinstance(evidence, SidebarReconciliationEvidence):
                raise TypeError("sidebar reconciliation evidence is malformed")
            fresh_proof = await asyncio.to_thread(
                _call,
                self._store,
                "record_sidebar_reconciliation_proof",
                lease_token=token,
                evidence=evidence,
                marker_digest=evidence.marker_digest,
                placement_generation=self._config.sidebar.placement_generation,
                delivery_generation=1,
                now=reserve_time,
            )
            if not isinstance(fresh_proof, Mapping):
                raise TypeError("sidebar reconciliation proof is malformed")
            fresh_digest = _exact_sidebar_proof_digest(
                fresh_proof.get("proof_digest")
            )
            fresh_generation = _exact_sidebar_claim_text(
                fresh_proof.get("reconciliation_generation"),
                "reconciliation generation",
            )
            if (
                fresh_proof.get("job_id") != job_id
                or fresh_proof.get("source_session_id") != source_session_id
                or fresh_proof.get("bridge_id") != bridge_id
                or fresh_generation != evidence.generation
                or fresh_proof.get("state") != evidence.state.value
                or fresh_proof.get("recovered_thread_id")
                != evidence.recovered_thread_id
            ):
                raise ValueError("sidebar reconciliation proof mismatch")
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except SidebarVerificationError as exc:
            await self._fail_sidebar_delivery_claim(
                token,
                _sidebar_reconciliation_failure_code(exc.code),
            )
            raise ValueError("sidebar authoritative reconciliation failed") from None
        except Exception:
            await self._fail_sidebar_delivery_claim(
                token,
                "bridge_temporarily_unavailable",
            )
            raise ValueError("sidebar authoritative reconciliation failed") from None

        if evidence.state is SidebarReconciliationState.RECOVERED:
            recovered_thread_id = _exact_sidebar_claim_text(
                evidence.recovered_thread_id,
                "recovered Codex thread ID",
            )
            await asyncio.to_thread(
                _call,
                self._store,
                "bind_sidebar_thread",
                lease_token=token,
                codex_thread_id=recovered_thread_id,
                now=reserve_time,
            )
            return {"state": "recovered", "codex_thread_id": recovered_thread_id}

        if evidence.state is SidebarReconciliationState.BLOCKED:
            assert evidence.fixed_reason is not None
            await self._fail_sidebar_delivery_claim(
                token,
                _sidebar_reconciliation_failure_code(evidence.fixed_reason),
            )
            raise ValueError("sidebar authoritative reconciliation blocked")

        if evidence.state is not SidebarReconciliationState.ABSENCE_PROVEN:
            await self._fail_sidebar_delivery_claim(
                token,
                "bridge_temporarily_unavailable",
            )
            raise ValueError("sidebar authoritative reconciliation failed")

        recovery_key = "hermes-session-bridge-create-v1:" + evidence.marker_digest
        reservation = await asyncio.to_thread(
            _call,
            self._store,
            "reserve_sidebar_create",
            lease_token=token,
            recovery_key=recovery_key,
            reconciliation_proof_digest=fresh_digest,
            reconciliation_generation=fresh_generation,
            now=reserve_time,
        )
        if (
            not isinstance(reservation, Mapping)
            or reservation.get("version") != 2
            or reservation.get("job_id") != job_id
            or reservation.get("source_session_id") != source_session_id
            or reservation.get("bridge_id") != bridge_id
            or reservation.get("recovery_key") != recovery_key
            or reservation.get("reconciliation_proof_digest") != fresh_digest
            or reservation.get("reconciliation_generation") != fresh_generation
        ):
            raise ValueError("sidebar create reservation is malformed")
        return {"state": "sidebar_leased", "create_reserved": True}

    async def commit_sidebar_job(
        self,
        *,
        lease_token: str,
        codex_thread_id: str,
        ensure_lineage: bool = False,
    ) -> Mapping[str, Any]:
        if type(ensure_lineage) is not bool:
            raise ValueError("sidebar lineage flag is malformed")
        token = _exact_sidebar_claim_text(lease_token, "lease token")
        thread_id = _exact_sidebar_claim_text(codex_thread_id, "Codex thread ID")
        verifier = self._sidebar_verifier
        if verifier is None:
            raise SidebarVerificationError("source_identity_mismatch")
        raw_identity = await asyncio.to_thread(
            _call,
            self._store,
            "lookup_sidebar_job_by_lease",
            token,
        )
        if not isinstance(raw_identity, Mapping):
            raise SidebarVerificationError("source_identity_mismatch")
        source_session_id = _exact_sidebar_claim_text(
            raw_identity.get("source_session_id"), "source session ID"
        )
        bridge_id = _exact_sidebar_claim_text(
            raw_identity.get("bridge_id"), "bridge ID"
        )
        expected = BridgeMarkerPayload(
            bridge_id=bridge_id,
            source_session_id=source_session_id,
            target_provider=Provider.CODEX,
            policy_generation=1,
        )
        if raw_identity.get("state") != "sidebar_visible":
            await asyncio.to_thread(
                _call,
                self._store,
                "bind_sidebar_thread",
                lease_token=token,
                codex_thread_id=thread_id,
                now=_finite_number(self._clock(), "now"),
            )
        verified = await asyncio.to_thread(
            verifier.verify_thread,
            thread_id=thread_id,
            expected=expected,
        )
        if (
            verified.thread_id != thread_id
            or verified.source_session_id != expected.source_session_id
            or verified.bridge_id != expected.bridge_id
        ):
            raise SidebarVerificationError("source_identity_mismatch")
        if ensure_lineage:
            projection = verified.projection
            if (
                not isinstance(projection, SessionProjection)
                or projection.provider is not Provider.CODEX
                or projection.native_id != thread_id
            ):
                raise SidebarVerificationError("source_identity_mismatch")
            try:
                await asyncio.to_thread(
                    _call,
                    self._store,
                    "get_sidebar_candidate_for_delivery",
                    source_session_id,
                )
                placement = await asyncio.to_thread(
                    resolve_sidebar_placement,
                    cast(str, self._config.sidebar.inbox_cwd),
                    hermes_constants.get_hermes_home(),
                    self._config.sidebar.placement_generation,
                    None,
                )
            except SidebarPlacementError as exc:
                raise SidebarVerificationError(exc.code) from None
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                raise SidebarVerificationError("source_identity_mismatch") from None
            if not placement_paths_equivalent(
                projection.cwd,
                placement.inbox_cwd,
            ):
                raise SidebarVerificationError("placement_mismatch")
            result = await asyncio.to_thread(
                _call,
                self._store,
                "commit_sidebar_job_with_lineage",
                lease_token=token,
                codex_thread_id=thread_id,
                source_session_id=source_session_id,
                bridge_id=bridge_id,
                placement_generation=self._config.sidebar.placement_generation,
                now=_finite_number(self._clock(), "now"),
            )
        else:
            result = await asyncio.to_thread(
                _call,
                self._store,
                "commit_sidebar_job",
                lease_token=token,
                codex_thread_id=thread_id,
                now=_finite_number(self._clock(), "now"),
            )
        if not isinstance(result, Mapping):
            raise ValueError("sidebar commit result is malformed")
        return result

    async def _register_sidebar_jobs_locked(
        self,
        *,
        registration_time: float,
        limit: int,
        backfill_days: int | None | object = _USE_CONFIGURED_BACKFILL,
        apply: bool = True,
        persist_cursor: bool = True,
        record_summary: bool = True,
    ) -> SidebarRegistrationSummary:
        effective_backfill_days = (
            self._config.sidebar.backfill_days
            if backfill_days is _USE_CONFIGURED_BACKFILL
            else backfill_days
        )
        if (
            effective_backfill_days is not None
            and type(effective_backfill_days) is not int
        ):
            raise ValueError("sidebar backfill days must be an integer or None")
        effective_backfill_days = cast(int | None, effective_backfill_days)
        after = (
            None
            if effective_backfill_days is None
            else registration_time - effective_backfill_days * 86_400
        )
        by_provider = {Provider.CLAUDE.value: 0, Provider.HERMES.value: 0}
        queued = 0
        failed = 0
        excluded = 0
        excluded_by_reason = {reason: 0 for reason in SIDEBAR_EXCLUSION_REASONS}
        examined = 0
        seen: set[str] = set()
        durable_cursor = (
            await self._load_sidebar_registration_cursor() if persist_cursor else None
        )
        probe_frontier = (
            await self._load_sidebar_registration_cursor(
                key=_SIDEBAR_REGISTRATION_PROBE_FRONTIER_KEY
            )
            if persist_cursor
            else None
        )
        catchup_state = (
            await self._load_sidebar_registration_catchup()
            if persist_cursor
            else None
        )
        catchup_cursor = catchup_state[0] if catchup_state is not None else None
        catchup_target = catchup_state[1] if catchup_state is not None else None
        catchup_head = catchup_state[2] if catchup_state is not None else None
        cursor: tuple[float, str] | None = None
        newest_probe = True
        catchup_active = False
        seen_backfill_cursors = {
            candidate
            for candidate in (
                durable_cursor,
                probe_frontier,
                catchup_cursor,
                catchup_target,
                catchup_head,
            )
            if candidate is not None
        }
        query_count = 0
        query_budget = (
            _SIDEBAR_REGISTRATION_QUERY_BUDGET
            if persist_cursor
            else _SIDEBAR_BACKFILL_QUERY_BUDGET
        )
        examined_budget = (
            _SIDEBAR_REGISTRATION_EXAMINED_BUDGET
            if persist_cursor
            else _SIDEBAR_BACKFILL_EXAMINED_BUDGET
        )
        while (
            queued < limit and query_count < query_budget and examined < examined_budget
        ):
            if persist_cursor and newest_probe:
                page_size = min(
                    _SIDEBAR_NEWEST_PROBE_SIZE,
                    examined_budget - examined,
                )
            else:
                remaining_queue_capacity = (
                    _SIDEBAR_REGISTRATION_PAGE_SIZE
                    if persist_cursor and catchup_active
                    else limit - queued
                    if persist_cursor
                    else _SIDEBAR_REGISTRATION_PAGE_SIZE
                )
                page_size = max(
                    1,
                    min(
                        remaining_queue_capacity,
                        _SIDEBAR_REGISTRATION_PAGE_SIZE,
                        examined_budget - examined,
                    ),
                )
            raw_page = await asyncio.to_thread(
                _call,
                self._store,
                "list_sidebar_candidates",
                after,
                page_size,
                cursor=cursor,
            )
            query_count += 1
            if not isinstance(raw_page, SidebarSourcePage):
                raise ValueError("sidebar candidate page is malformed")
            if len(raw_page) > page_size:
                raise ValueError("sidebar candidate page exceeds its limit")
            if type(raw_page.has_more) is not bool:
                raise ValueError("sidebar candidate page continuation is malformed")
            next_cursor: tuple[float, str] | None = None
            if raw_page.has_more:
                next_cursor = _validated_sidebar_page_cursor(raw_page.next_cursor)
                if cursor is not None and not _sidebar_cursor_advances(
                    cursor,
                    next_cursor,
                ):
                    raise ValueError("sidebar candidate cursor did not advance")
                if (
                    not newest_probe
                    and next_cursor in seen_backfill_cursors
                    and not (
                        catchup_active
                        and catchup_target is not None
                        and next_cursor == catchup_target
                    )
                ):
                    raise ValueError("sidebar candidate cursor repeated")
            elif raw_page.next_cursor is not None:
                raise ValueError("sidebar candidate cursor is unexpected")

            for raw_source in raw_page:
                examined += 1
                try:
                    if not isinstance(raw_source, SidebarSource):
                        raise ValueError("sidebar source candidate is malformed")
                    source = raw_source
                    if source.source_session_id in seen:
                        raise ValueError("duplicate sidebar source candidate")
                    seen.add(source.source_session_id)
                    projection = source.projection
                    if not is_sidebar_session_eligible(
                        projection,
                        now=registration_time,
                        backfill_days=effective_backfill_days,
                        automation_only=source.automation_only,
                        subagent_only=source.subagent_only,
                    ):
                        continue
                    canonical_source = canonical_session_id(
                        projection.provider,
                        projection.native_id,
                    )
                    if canonical_source != source.source_session_id:
                        raise ValueError("sidebar source identity is inconsistent")
                    getter = getattr(self._store, "get_sidebar_job_for_source", None)
                    existing = (
                        await asyncio.to_thread(getter, canonical_source)
                        if callable(getter)
                        else None
                    )
                    if existing is not None:
                        continue
                    enqueue_method = getattr(self._store, "enqueue_sidebar_job", None)
                    if apply and not callable(enqueue_method):
                        raise RuntimeError("sidebar enqueue is unavailable")
                    snapshot_aware = callable(enqueue_method) and (
                        "worktree_snapshot"
                        in inspect.signature(enqueue_method).parameters
                    )
                    indexed_at_aware = callable(enqueue_method) and (
                        "indexed_at" in inspect.signature(enqueue_method).parameters
                    )
                    try:
                        first_request = _first_sidebar_request(projection)
                        if (
                            not isinstance(projection.cwd, str)
                            or not projection.cwd.strip()
                        ):
                            raise WorktreeSnapshotError("source_cwd_missing")
                        candidate = SidebarCandidate(
                            source_session_id=canonical_source,
                            provider=projection.provider,
                            bridge_id=sidebar_bridge_id(canonical_source),
                            title=sidebar_title(
                                projection.provider,
                                projection.title,
                                first_request,
                            ),
                            cwd=projection.cwd,
                            git_root=source.git_root,
                            git_branch=projection.git_branch,
                            git_head=source.git_head,
                            worktree_id=source.worktree_id,
                            eligible_at=projection.last_active,
                        )
                        worktree_snapshot: WorktreeSnapshot | None = None
                        if snapshot_aware:
                            indexed_git_metadata = (
                                candidate.git_root is not None
                                or candidate.git_head is not None
                                or candidate.git_branch not in (None, "HEAD")
                            )
                            for capture_attempt in range(3):
                                try:
                                    worktree_snapshot = await asyncio.to_thread(
                                        capture_worktree_snapshot,
                                        candidate.cwd,
                                    )
                                    break
                                except WorktreeSnapshotError as exc:
                                    if (
                                        exc.code != "source_identity_mismatch"
                                        or capture_attempt == 2
                                    ):
                                        raise
                            assert worktree_snapshot is not None
                            if (
                                indexed_git_metadata
                                and worktree_snapshot.git_root is None
                            ):
                                raise WorktreeSnapshotError("source_identity_mismatch")
                            candidate = replace(
                                candidate,
                                cwd=worktree_snapshot.cwd,
                                git_root=worktree_snapshot.git_root,
                                git_branch=worktree_snapshot.branch,
                                git_head=worktree_snapshot.head,
                                worktree_id=worktree_snapshot.worktree_id,
                            )
                    except WorktreeSnapshotError as exc:
                        if exc.code != "source_cwd_missing":
                            raise
                        if apply:
                            recorder = getattr(
                                self._store,
                                "record_sidebar_exclusion",
                                None,
                            )
                            if not callable(recorder):
                                raise RuntimeError(
                                    "sidebar exclusion persistence is unavailable"
                                )
                            persisted = await asyncio.to_thread(
                                recorder,
                                source_session_id=canonical_source,
                                provider=projection.provider,
                                reason_code=exc.code,
                                now=registration_time,
                            )
                            if (
                                not isinstance(persisted, Mapping)
                                or type(persisted.get("created")) is not bool
                            ):
                                raise ValueError(
                                    "sidebar exclusion result is malformed"
                                )
                        excluded += 1
                        excluded_by_reason[exc.code] += 1
                        continue
                    if not apply:
                        queued += 1
                        by_provider[projection.provider.value] += 1
                    else:
                        assert callable(enqueue_method)
                        enqueue_kwargs: dict[str, Any] = {}
                        if worktree_snapshot is not None:
                            enqueue_kwargs["worktree_snapshot"] = worktree_snapshot
                        if indexed_at_aware:
                            enqueue_kwargs["indexed_at"] = max(
                                projection.last_active,
                                (
                                    _finite_number(
                                        source.indexed_at,
                                        "sidebar source indexed_at",
                                    )
                                    if source.indexed_at is not None
                                    else registration_time
                                ),
                            )
                        result = await asyncio.to_thread(
                            enqueue_method,
                            candidate,
                            **enqueue_kwargs,
                        )
                        if (
                            not isinstance(result, Mapping)
                            or type(result.get("created")) is not bool
                        ):
                            raise ValueError("sidebar enqueue result is malformed")
                        if result["created"]:
                            queued += 1
                            by_provider[projection.provider.value] += 1
                except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                    raise
                except Exception:
                    failed += 1
                    if apply:
                        self._record_error_code("sidebar_registration_candidate_failed")
                if queued >= limit:
                    break

            if queued >= limit:
                break
            if not raw_page.has_more:
                if persist_cursor:
                    await self._save_sidebar_registration_cursor(None)
                    await self._save_sidebar_registration_cursor(
                        None,
                        key=_SIDEBAR_REGISTRATION_PROBE_FRONTIER_KEY,
                    )
                    await self._save_sidebar_registration_catchup(None)
                break
            assert next_cursor is not None
            if newest_probe:
                newest_probe = False
                if catchup_cursor is not None:
                    catchup_active = True
                    cursor = catchup_cursor
                elif probe_frontier is not None:
                    if _sidebar_cursor_advances(next_cursor, probe_frontier):
                        catchup_active = True
                        catchup_cursor = next_cursor
                        catchup_target = probe_frontier
                        catchup_head = next_cursor
                        seen_backfill_cursors.add(next_cursor)
                        if persist_cursor:
                            await self._save_sidebar_registration_catchup(
                                (
                                    catchup_cursor,
                                    catchup_target,
                                    catchup_head,
                                )
                            )
                        cursor = catchup_cursor
                    else:
                        if next_cursor != probe_frontier:
                            probe_frontier = next_cursor
                            if persist_cursor:
                                await self._save_sidebar_registration_cursor(
                                    probe_frontier,
                                    key=_SIDEBAR_REGISTRATION_PROBE_FRONTIER_KEY,
                                )
                        if durable_cursor is None or _sidebar_cursor_advances(
                            durable_cursor,
                            next_cursor,
                        ):
                            durable_cursor = next_cursor
                            if persist_cursor:
                                await self._save_sidebar_registration_cursor(
                                    durable_cursor
                                )
                        cursor = durable_cursor
                elif durable_cursor is None:
                    durable_cursor = next_cursor
                    seen_backfill_cursors.add(next_cursor)
                    if persist_cursor:
                        await self._save_sidebar_registration_cursor(durable_cursor)
                    cursor = durable_cursor
                elif _sidebar_cursor_advances(
                    next_cursor,
                    durable_cursor,
                ):
                    catchup_active = True
                    catchup_cursor = next_cursor
                    catchup_target = durable_cursor
                    catchup_head = next_cursor
                    seen_backfill_cursors.add(next_cursor)
                    if persist_cursor:
                        await self._save_sidebar_registration_catchup(
                            (
                                catchup_cursor,
                                catchup_target,
                                catchup_head,
                            )
                        )
                    cursor = catchup_cursor
                else:
                    cursor = durable_cursor
            else:
                if catchup_active:
                    assert catchup_target is not None
                    assert catchup_head is not None
                if catchup_active and _sidebar_cursor_advances(
                    next_cursor,
                    catchup_target,
                ):
                    catchup_cursor = next_cursor
                    seen_backfill_cursors.add(next_cursor)
                    if persist_cursor:
                        await self._save_sidebar_registration_catchup(
                            (
                                catchup_cursor,
                                catchup_target,
                                catchup_head,
                            )
                        )
                    cursor = catchup_cursor
                else:
                    if catchup_active:
                        assert catchup_target is not None
                        assert catchup_head is not None
                        probe_frontier = catchup_head
                        if (
                            durable_cursor is None
                            or catchup_target == durable_cursor
                            or _sidebar_cursor_advances(durable_cursor, next_cursor)
                        ):
                            durable_cursor = next_cursor
                        if persist_cursor:
                            await self._save_sidebar_registration_cursor(
                                probe_frontier,
                                key=_SIDEBAR_REGISTRATION_PROBE_FRONTIER_KEY,
                            )
                            await self._save_sidebar_registration_catchup(None)
                    else:
                        durable_cursor = next_cursor
                    catchup_active = False
                    catchup_cursor = None
                    catchup_target = None
                    catchup_head = None
                    seen_backfill_cursors.add(next_cursor)
                    if persist_cursor:
                        await self._save_sidebar_registration_cursor(durable_cursor)
                    cursor = durable_cursor

        summary = SidebarRegistrationSummary(
            examined=examined,
            queued=queued,
            by_provider=by_provider,
            failed=failed,
            excluded=excluded,
            excluded_by_reason=excluded_by_reason,
        )
        if record_summary:
            self._set_sidebar_registration_counts(summary)
        return summary

    async def _load_sidebar_registration_cursor(
        self,
        *,
        key: str = _SIDEBAR_REGISTRATION_CURSOR_KEY,
    ) -> tuple[float, str] | None:
        raw_state = await asyncio.to_thread(
            _call,
            self._store,
            "get_state",
            key,
        )
        if raw_state is None:
            return None
        if not isinstance(raw_state, Mapping) or set(raw_state) != {
            "version",
            "activity",
            "session_id",
        }:
            raise ValueError("sidebar registration cursor state is malformed")
        if (
            type(raw_state["version"]) is not int
            or raw_state["version"] != _SIDEBAR_REGISTRATION_CURSOR_VERSION
        ):
            raise ValueError("sidebar registration cursor version is unsupported")
        activity = raw_state["activity"]
        session_id = raw_state["session_id"]
        if activity is None and session_id is None:
            return None
        if activity is None or session_id is None:
            raise ValueError("sidebar registration cursor state is malformed")
        return _validated_sidebar_page_cursor((activity, session_id))

    async def _save_sidebar_registration_cursor(
        self,
        cursor: tuple[float, str] | None,
        *,
        key: str = _SIDEBAR_REGISTRATION_CURSOR_KEY,
    ) -> None:
        state: dict[str, object] = {
            "version": _SIDEBAR_REGISTRATION_CURSOR_VERSION,
            "activity": None,
            "session_id": None,
        }
        if cursor is not None:
            activity, session_id = _validated_sidebar_page_cursor(cursor)
            state["activity"] = activity
            state["session_id"] = session_id
        await asyncio.to_thread(
            _call,
            self._store,
            "set_state",
            key,
            state,
        )

    async def _load_sidebar_registration_catchup(
        self,
    ) -> tuple[
        tuple[float, str],
        tuple[float, str],
        tuple[float, str],
    ] | None:
        raw_state = await asyncio.to_thread(
            _call,
            self._store,
            "get_state",
            _SIDEBAR_REGISTRATION_CATCHUP_STATE_KEY,
        )
        if raw_state is None:
            return None
        expected = {
            "version",
            "cursor_activity",
            "cursor_session_id",
            "target_activity",
            "target_session_id",
            "head_activity",
            "head_session_id",
        }
        if not isinstance(raw_state, Mapping) or set(raw_state) != expected:
            raise ValueError("sidebar registration catchup state is malformed")
        if (
            type(raw_state["version"]) is not int
            or raw_state["version"] != _SIDEBAR_REGISTRATION_CURSOR_VERSION
        ):
            raise ValueError("sidebar registration catchup version is unsupported")
        values = tuple(raw_state[key] for key in sorted(expected - {"version"}))
        if all(value is None for value in values):
            return None
        if any(value is None for value in values):
            raise ValueError("sidebar registration catchup state is malformed")
        return (
            _validated_sidebar_page_cursor(
                (raw_state["cursor_activity"], raw_state["cursor_session_id"])
            ),
            _validated_sidebar_page_cursor(
                (raw_state["target_activity"], raw_state["target_session_id"])
            ),
            _validated_sidebar_page_cursor(
                (raw_state["head_activity"], raw_state["head_session_id"])
            ),
        )

    async def _save_sidebar_registration_catchup(
        self,
        state: tuple[
            tuple[float, str],
            tuple[float, str],
            tuple[float, str],
        ]
        | None,
    ) -> None:
        document: dict[str, object] = {
            "version": _SIDEBAR_REGISTRATION_CURSOR_VERSION,
            "cursor_activity": None,
            "cursor_session_id": None,
            "target_activity": None,
            "target_session_id": None,
            "head_activity": None,
            "head_session_id": None,
        }
        if state is not None:
            cursor, target, head = (
                _validated_sidebar_page_cursor(value) for value in state
            )
            document.update(
                cursor_activity=cursor[0],
                cursor_session_id=cursor[1],
                target_activity=target[0],
                target_session_id=target[1],
                head_activity=head[0],
                head_session_id=head[1],
            )
        await asyncio.to_thread(
            _call,
            self._store,
            "set_state",
            _SIDEBAR_REGISTRATION_CATCHUP_STATE_KEY,
            document,
        )

    async def reconcile_once(self) -> ReconcileSummary:
        async with self._job_lock:
            async with self._mirror_worker_critical_section():
                jobs = await self._reconcile_jobs_locked()
        continuations = await self._reconcile_continuations()
        return ReconcileSummary(
            examined=jobs.examined + continuations.examined,
            recovered=jobs.recovered + continuations.recovered,
            retried=jobs.retried + continuations.retried,
            failed=jobs.failed + continuations.failed,
        )

    async def _reconcile_jobs_locked(self) -> ReconcileSummary:
        if not callable(getattr(self._store, "list_mirror_jobs", None)):
            return ReconcileSummary(examined=0, recovered=0, retried=0, failed=0)
        jobs = await asyncio.to_thread(
            _call,
            self._store,
            "list_mirror_jobs",
            [MirrorJobState.RUNNING],
            limit=1000,
        )
        recovered = 0
        retried = 0
        failed = 0
        policy = self._mirror_policy()
        for raw_job in jobs:
            job = _validated_job(raw_job)
            sidecar = await asyncio.to_thread(
                _call,
                self._store,
                "get_state",
                _attempt_key(job),
            )
            if sidecar is None:
                await self._retry_provider_call_not_started(job)
                retried += 1
                continue
            if (
                isinstance(sidecar, Mapping)
                and isinstance(sidecar.get("attempts"), int)
                and not isinstance(sidecar.get("attempts"), bool)
                and sidecar["attempts"] < job["attempts"]
            ):
                await self._retry_provider_call_not_started(job)
                retried += 1
                continue
            try:
                attempt = _validated_attempt_sidecar(
                    sidecar,
                    job,
                    policy_generation=policy.generation,
                )
            except (TypeError, ValueError, RuntimeError):
                await self._fail_job_manually(
                    job,
                    code="attempt_sidecar_invalid",
                    detail="mirror attempt sidecar is invalid",
                )
                failed += 1
                continue
            try:
                target = await asyncio.to_thread(
                    _call,
                    self._store,
                    "find_external_session_by_origin_bridge",
                    attempt["bridge_id"],
                    Provider(job["target_provider"]),
                )
                if target is None:
                    expected_native_id = attempt.get("expected_native_id")
                    if not isinstance(expected_native_id, str):
                        raise RuntimeError("target identity is not cataloged")
                    target = await self._index_exact_target(
                        Provider(job["target_provider"]),
                        expected_native_id,
                        bridge_id=attempt["bridge_id"],
                        source_session_id=job["source_session_id"],
                        policy_generation=policy.generation,
                        require_marker_payload=True,
                    )
                else:
                    native_id = target.get("native_id")
                    if not isinstance(native_id, str) or not native_id:
                        raise RuntimeError("target identity is not cataloged")
                    target = await self._index_exact_target(
                        Provider(job["target_provider"]),
                        native_id,
                        bridge_id=attempt["bridge_id"],
                        source_session_id=job["source_session_id"],
                        policy_generation=policy.generation,
                        require_marker_payload=True,
                    )
                await self._complete_cataloged_target(
                    job,
                    bridge_id=attempt["bridge_id"],
                    target=target,
                )
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                await self._fail_job_manually(
                    job,
                    code="target_identity_unproven",
                    detail="running mirror target identity could not be proven",
                )
                failed += 1
            else:
                recovered += 1
        return ReconcileSummary(
            examined=len(jobs),
            recovered=recovered,
            retried=retried,
            failed=failed,
        )

    async def _reconcile_continuations(self) -> ReconcileSummary:
        if not callable(getattr(self._store, "list_continuation_snapshots", None)):
            return ReconcileSummary(examined=0, recovered=0, retried=0, failed=0)
        after_bridge_id = await self._load_continuation_reconcile_cursor()
        snapshots = await asyncio.to_thread(
            _call,
            self._store,
            "list_continuation_snapshots",
            limit=_CONTINUATION_RECONCILE_BATCH_SIZE,
            after_bridge_id=after_bridge_id,
        )
        if not snapshots and after_bridge_id is not None:
            snapshots = await asyncio.to_thread(
                _call,
                self._store,
                "list_continuation_snapshots",
                limit=_CONTINUATION_RECONCILE_BATCH_SIZE,
                after_bridge_id=None,
            )
        examined = 0
        marked = 0
        failed = 0
        for raw_snapshot in snapshots:
            examined += 1
            try:
                snapshot = _validated_periodic_continuation_snapshot(raw_snapshot)
                source = await self._refresh_continuation_source(
                    snapshot["source_session_id"]
                )
                target = await self.refresh_session(
                    snapshot["target_session_id"],
                    timeout=self._refresh_timeout,
                )
                if source.stale or target.stale:
                    continue
                source_advanced = (source.cursor, source.source_hash) != (
                    snapshot["source_cursor"],
                    snapshot["source_hash"],
                )
                target_advanced = (target.cursor, target.source_hash) != (
                    snapshot["target_cursor"],
                    snapshot["target_hash"],
                )
                if source_advanced and target_advanced:
                    await asyncio.to_thread(
                        _call,
                        self._store,
                        "mark_diverged",
                        snapshot["bridge_id"],
                        at=float(self._clock()),
                    )
                    marked += 1
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                failed += 1
                self._record_error_code("continuation_reconcile_failed")
        next_cursor = (
            snapshots[-1].get("bridge_id")
            if len(snapshots) >= _CONTINUATION_RECONCILE_BATCH_SIZE
            and isinstance(snapshots[-1], Mapping)
            else None
        )
        await self._save_continuation_reconcile_cursor(next_cursor)
        return ReconcileSummary(
            examined=examined,
            recovered=marked,
            retried=0,
            failed=failed,
        )

    async def process_jobs_once(
        self,
        *,
        job_ids: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> JobSummary:
        normalized_job_ids = _validated_process_job_ids(job_ids)
        if limit is not None and (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("job processing limit must be between 1 and 1000")
        async with self._job_lock:
            async with self._mirror_worker_critical_section():
                return await self._process_jobs_locked(
                    job_ids=normalized_job_ids,
                    limit=limit,
                )

    async def _process_jobs_locked(
        self,
        *,
        job_ids: tuple[str, ...] | None,
        limit: int | None,
    ) -> JobSummary:
        policy = self._mirror_policy()
        requested_limit = policy.creates_per_minute if limit is None else limit
        now = float(self._clock())
        atomic_claim = getattr(self._store, "claim_due_jobs_with_limits", None)
        uses_atomic_controls = callable(atomic_claim)
        breaker = BatchProgress()
        effective_policy = policy
        if callable(atomic_claim):
            claim_kwargs: dict[str, Any] = {
                "now": now,
                "limit": requested_limit,
                "policy": policy,
            }
            if job_ids is not None:
                claim_kwargs["job_ids"] = job_ids
            jobs = await asyncio.to_thread(
                atomic_claim,
                **claim_kwargs,
            )
        else:
            capacity = await self._creation_capacity(policy, now=now)
            capacity = min(capacity, requested_limit)
            if capacity == 0:
                return JobSummary(
                    claimed=0,
                    succeeded=0,
                    retried=0,
                    manual_failure=0,
                )
            breaker = await self._load_breaker_progress()
            if _healthy_breaker_batch_completed(
                breaker,
                policy,
            ):
                breaker = BatchProgress()
                await self._save_breaker_progress(breaker)
            if policy.automatic_creation and should_halt_batch(breaker, policy):
                effective_policy = replace(policy, automatic_creation=False)
            claim_limit = 1 if effective_policy.automatic_creation else capacity
            fallback_claim_kwargs: dict[str, Any] = {
                "now": now,
                "limit": claim_limit,
                "policy": effective_policy,
            }
            if job_ids is not None:
                fallback_claim_kwargs["job_ids"] = job_ids
            jobs = await asyncio.to_thread(
                _call,
                self._store,
                "claim_due_jobs",
                **fallback_claim_kwargs,
            )
            await self._reserve_creation_capacity(jobs, now=now)
        succeeded = 0
        retried = 0
        manual_failure = 0
        limited_attempts = 0
        limited_errors = 0
        for raw_job in jobs:
            job = _validated_job(raw_job)
            outcome = await self._process_claimed_job(job, policy=effective_policy)
            fallback_authority = (
                "automatic" if effective_policy.automatic_creation else "manual"
            )
            limited_claim = job.get(
                "claim_authority", fallback_authority
            ) == "automatic" or job.get("rollout_limited", False)
            limited_attempts += int(limited_claim)
            if outcome is MirrorJobState.SUCCEEDED:
                succeeded += 1
            elif outcome is MirrorJobState.RETRY:
                retried += 1
                limited_errors += int(limited_claim)
            else:
                manual_failure += 1
                limited_errors += int(limited_claim)
        summary = JobSummary(
            claimed=len(jobs),
            succeeded=succeeded,
            retried=retried,
            manual_failure=manual_failure,
        )
        if limited_attempts and not uses_atomic_controls:
            await self._save_breaker_progress(
                BatchProgress(
                    attempts=breaker.attempts + limited_attempts,
                    errors=breaker.errors + limited_errors,
                )
            )
        return summary

    async def refresh_session(
        self,
        session_id: str,
        *,
        timeout: float,
    ) -> RefreshResult:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session ID must not be empty")
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(float(timeout))
            or timeout <= 0
        ):
            raise ValueError("refresh timeout must be a positive finite number")
        normalized_session_id = session_id.strip()
        durable = await asyncio.to_thread(
            _call,
            self._store,
            "get_external_session",
            normalized_session_id,
        )
        if durable is None:
            raise KeyError(normalized_session_id)
        if not isinstance(durable, Mapping):
            raise RuntimeError("external session record is invalid")
        try:
            provider = Provider(durable.get("provider"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("external session provider is invalid") from exc
        if provider not in _EXTERNAL_PROVIDERS:
            raise RuntimeError("external session provider is invalid")
        native_id = durable.get("native_id")
        if (
            not isinstance(native_id, str)
            or not native_id.strip()
            or canonical_session_id(provider, native_id) != normalized_session_id
        ):
            raise RuntimeError("external session identity is invalid")

        lock = self._scan_locks[provider]
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            await asyncio.wait_for(lock.acquire(), timeout=float(timeout))
        except TimeoutError:
            self._mark_refresh_failure(provider)
            return _stale_refresh(normalized_session_id, durable)
        remaining_timeout = float(timeout) - (loop.time() - started)
        if remaining_timeout <= 0:
            lock.release()
            self._mark_refresh_failure(provider)
            return _stale_refresh(normalized_session_id, durable)
        release_lock = True
        read_task = self._start_provider_call(
            _read_exact_projection,
            self._adapter(provider),
            provider,
            native_id.strip(),
        )
        try:
            done, _ = await asyncio.wait({read_task}, timeout=remaining_timeout)
            if not done:
                release_lock = False
                read_task.add_done_callback(lambda _task: lock.release())
                self._mark_refresh_failure(provider)
                return _stale_refresh(normalized_session_id, durable)
            projection = read_task.result()
            if (
                projection.provider is not provider
                or canonical_session_id(projection.provider, projection.native_id)
                != normalized_session_id
                or not isinstance(projection.native_cursor, str)
                or not projection.native_cursor.strip()
                or not isinstance(projection.native_hash, str)
                or not projection.native_hash.strip()
            ):
                raise RuntimeError("refreshed session identity is invalid")
            await asyncio.to_thread(
                _upsert,
                self._store,
                projection,
                True,
            )
            self._mark_provider_success(provider)
            return RefreshResult(
                session_id=normalized_session_id,
                cursor=projection.native_cursor.strip(),
                source_hash=projection.native_hash.strip(),
                stale=False,
                warning=None,
            )
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            if not read_task.done():
                release_lock = False
                read_task.add_done_callback(lambda _task: lock.release())
            raise
        except Exception:
            self._mark_refresh_failure(provider)
            return _stale_refresh(normalized_session_id, durable)
        finally:
            if release_lock:
                lock.release()

    async def continue_session(self, request: ContinueRequest) -> ContinueResult:
        _validate_continue_request(request)
        lock = self._continuation_locks.setdefault(request.bridge_id, asyncio.Lock())
        async with lock:
            return await self._continue_locked(request)

    async def _refresh_continuation_source(self, session_id: str) -> RefreshResult:
        native_snapshot_method = getattr(
            self._store, "get_native_session_snapshot", None
        )
        if callable(native_snapshot_method):
            native = await asyncio.to_thread(native_snapshot_method, session_id)
            if native is not None:
                if not isinstance(native, Mapping):
                    raise RuntimeError("native Hermes snapshot is invalid")
                cursor = native.get("cursor")
                source_hash = native.get("source_hash")
                if (
                    native.get("session_id") != session_id
                    or native.get("provider") != Provider.HERMES.value
                    or not isinstance(cursor, str)
                    or not cursor.strip()
                    or not isinstance(source_hash, str)
                    or not source_hash.strip()
                ):
                    raise RuntimeError("native Hermes snapshot is invalid")
                return RefreshResult(
                    session_id=session_id,
                    cursor=cursor.strip(),
                    source_hash=source_hash.strip(),
                    stale=False,
                    warning=None,
                )
        return await self.refresh_session(session_id, timeout=self._refresh_timeout)

    async def _continue_locked(self, request: ContinueRequest) -> ContinueResult:
        exact_worktree, worktree_warnings = await self._continuation_worktree(request)
        snapshot = await asyncio.to_thread(
            _call,
            self._store,
            "get_continuation_snapshot",
            request.bridge_id,
        )
        source = await self._refresh_continuation_source(request.session_id)
        pending_pack: ContextPack | None = None
        if snapshot is None:
            existing_pack_row = await asyncio.to_thread(
                _call,
                self._store,
                "get_context_pack",
                request.bridge_id,
                budget_chars=request.context_budget_chars,
            )
            if existing_pack_row is not None:
                pending_pack = _context_pack_from_row(existing_pack_row)
                if (
                    pending_pack.source_session_id != request.session_id
                    or pending_pack.target_session_id is None
                    or pending_pack.immutable_at is not None
                ):
                    raise RuntimeError("pending continuation pack identity is invalid")
                target_session_id = pending_pack.target_session_id
            else:
                target_row = await asyncio.to_thread(
                    _call,
                    self._store,
                    "find_external_session_by_origin_bridge",
                    request.bridge_id,
                    request.target_provider,
                )
                if not isinstance(target_row, Mapping):
                    raise RuntimeError("continuation target is not cataloged")
                target_session_id = _external_row_session_id(
                    target_row,
                    request.target_provider,
                )
        else:
            snapshot = _validated_continuation_snapshot(snapshot)
            if snapshot["source_session_id"] != request.session_id:
                raise ValueError("continuation source identity mismatch")
            target_session_id = snapshot["target_session_id"]

        if _provider_from_session_id(target_session_id) is not request.target_provider:
            raise ValueError("continuation target provider mismatch")

        target = await self.refresh_session(
            target_session_id,
            timeout=self._refresh_timeout,
        )
        warnings = tuple(
            warning
            for warning in (*worktree_warnings, source.warning, target.warning)
            if warning is not None
        )

        if snapshot is None:
            if pending_pack is not None:
                pack = pending_pack
                if not source.stale and (
                    pack.source_cursor != source.cursor
                    or pack.source_hash != source.source_hash
                ):
                    raise ValueError(
                        "pending continuation source advanced after context build"
                    )
            else:
                if self._context_builder is None:
                    raise RuntimeError("context pack builder is not configured")
                pack = await asyncio.to_thread(
                    _call,
                    self._context_builder,
                    "build",
                    ContextPackRequest(
                        source_session_id=request.session_id,
                        target_provider=request.target_provider,
                        bridge_id=request.bridge_id,
                        source_cursor=source.cursor,
                        source_hash=source.source_hash,
                        budget_chars=request.context_budget_chars,
                        stale=source.stale,
                        diverged=False,
                        exact_cwd=(
                            exact_worktree.cwd if exact_worktree is not None else None
                        ),
                        worktree_warnings=worktree_warnings,
                    ),
                )
                if not isinstance(pack, ContextPack):
                    raise RuntimeError("context pack builder returned no pack")
            if pack.target_session_id != target_session_id:
                raise RuntimeError("context pack target identity mismatch")
            target_cursor = target.cursor
            target_hash = target.source_hash
        else:
            pack_row = await asyncio.to_thread(
                _call,
                self._store,
                "get_context_pack",
                request.bridge_id,
                budget_chars=request.context_budget_chars,
            )
            if pack_row is None:
                raise ValueError("continuation context budget is already frozen")
            pack = _context_pack_from_row(pack_row)
            if pack.id != snapshot["pack_id"] or pack.immutable_at is None:
                raise RuntimeError("continuation pack identity is invalid")
            target_cursor = snapshot["target_cursor"]
            target_hash = snapshot["target_hash"]

        link_row = await asyncio.to_thread(
            _call,
            self._store,
            "transition_link_to_continues",
            request.bridge_id,
            pack_id=pack.id,
            target_cursor=target_cursor,
            target_hash=target_hash,
        )
        persisted_pack_row = await asyncio.to_thread(
            _call,
            self._store,
            "get_context_pack",
            request.bridge_id,
            budget_chars=request.context_budget_chars,
        )
        if persisted_pack_row is None:
            raise RuntimeError("continued context pack is unavailable")
        persisted_pack = _context_pack_from_row(persisted_pack_row)
        link = _session_link_from_row(link_row)

        if snapshot is not None and not source.stale and not target.stale:
            source_advanced = (source.cursor, source.source_hash) != (
                snapshot["source_cursor"],
                snapshot["source_hash"],
            )
            target_advanced = (target.cursor, target.source_hash) != (
                snapshot["target_cursor"],
                snapshot["target_hash"],
            )
            if source_advanced and target_advanced:
                await asyncio.to_thread(
                    _call,
                    self._store,
                    "mark_diverged",
                    request.bridge_id,
                    at=float(self._clock()),
                )
                warnings = (*warnings, "linked_sessions_diverged")
        if exact_worktree is not None:
            exact_worktree, final_worktree_warnings = await self._continuation_worktree(
                request
            )
            non_worktree_warnings = tuple(
                warning for warning in warnings if warning not in worktree_warnings
            )
            warnings = tuple(
                dict.fromkeys((*final_worktree_warnings, *non_worktree_warnings))
            )
        return ContinueResult(
            pack=persisted_pack,
            link=link,
            warnings=warnings,
            exact_cwd=(exact_worktree.cwd if exact_worktree is not None else None),
        )

    async def _continuation_worktree(
        self,
        request: ContinueRequest,
    ) -> tuple[WorktreeSnapshot | None, tuple[str, ...]]:
        if request.session_id.startswith("codex:"):
            return None, ()
        if request.bridge_id != sidebar_bridge_id(request.session_id):
            return None, ()
        try:
            recorded = await asyncio.to_thread(
                _call,
                self._store,
                "get_worktree_snapshot",
                request.session_id,
            )
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            raise ContinuationBlockedError(
                "source_identity_mismatch",
                "source_identity_mismatch: exact source worktree snapshot is invalid",
            ) from exc
        if recorded is None:
            raise ContinuationBlockedError(
                "source_identity_mismatch",
                "source_identity_mismatch: exact source worktree snapshot is unavailable",
            )
        if not isinstance(recorded, WorktreeSnapshot):
            raise ContinuationBlockedError(
                "source_identity_mismatch",
                "source_identity_mismatch: exact source worktree snapshot is invalid",
            )
        try:
            current, _initial_warnings = await asyncio.to_thread(
                validate_worktree_snapshot,
                recorded,
            )
        except WorktreeSnapshotError as exc:
            raise ContinuationBlockedError(
                exc.code,
                f"{exc.code}: exact source worktree identity validation failed",
            ) from exc
        permitted = await asyncio.to_thread(
            _filesystem_permission_preflight,
            current.cwd,
        )
        if permitted and self._permission_preflight is not None:
            try:
                permission_result = await asyncio.to_thread(
                    self._permission_preflight,
                    current.cwd,
                )
                permitted = permission_result is True
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                permitted = False
        if not permitted:
            raise ContinuationBlockedError(
                "permission_preflight_failed",
                "permission_preflight_failed: exact source cwd is not authorized",
            )
        try:
            final_current, final_warnings = await asyncio.to_thread(
                validate_worktree_snapshot,
                recorded,
            )
        except WorktreeSnapshotError as exc:
            raise ContinuationBlockedError(
                exc.code,
                f"{exc.code}: exact source worktree identity validation failed",
            ) from exc
        return final_current, final_warnings

    def health(self) -> dict[str, Any]:
        now = float(self._clock())
        recent_error_codes = list(self._recent_error_codes)
        providers: dict[str, _ProviderHealth] = {}
        for provider, state in self._provider_health.items():
            last_success = state["last_success"]
            providers[provider.value] = {
                "last_success": last_success,
                "lag_seconds": (
                    max(0.0, now - last_success)
                    if isinstance(last_success, (int, float))
                    else None
                ),
                "degraded_reason": state["degraded_reason"],
            }
        queue_counts: dict[str, int] = {}
        counter = getattr(self._store, "mirror_job_counts", None)
        if callable(counter):
            try:
                counts = counter()
                if isinstance(counts, Mapping):
                    queue_counts = {
                        state.value: int(counts.get(state.value, 0))
                        for state in MirrorJobState
                    }
            except Exception:
                recent_error_codes.append("mirror_queue_health_failed")
                del recent_error_codes[:-_RECENT_ERROR_LIMIT]
        return {
            "running": self._running,
            "providers": providers,
            "watcher_state": self._watcher_state,
            "watcher_error_code": self._watcher_error_code,
            "queue_counts": queue_counts,
            "mirror_mode": (
                "automatic" if self._config.mirrors.automatic_creation else "manual"
            ),
            "backfill_progress": {
                provider.value: dict(progress)
                for provider, progress in self._backfill_progress.items()
            },
            "registration_turn_fallback": self._registration_turn_fallback,
            "sidebar_registration_counts": dict(self._sidebar_registration_counts),
            "provider_calls_inflight": len(self._provider_tasks),
            "recent_error_codes": recent_error_codes,
        }

    def _mirror_policy(self) -> MirrorPolicy:
        mirrors = self._config.mirrors
        return MirrorPolicy(
            automatic_creation=mirrors.automatic_creation,
            backfill_days=mirrors.backfill_days,
            creates_per_minute=mirrors.creates_per_minute,
            max_attempts=mirrors.max_attempts,
            stop_after_attempts=mirrors.stop_after_attempts,
            stop_error_rate=mirrors.stop_error_rate,
        )

    def _set_sidebar_registration_counts(
        self,
        summary: SidebarRegistrationSummary,
    ) -> None:
        self._sidebar_registration_counts = {
            "examined": summary.examined,
            "queued": summary.queued,
            Provider.CLAUDE.value: int(
                summary.by_provider.get(Provider.CLAUDE.value, 0)
            ),
            Provider.HERMES.value: int(
                summary.by_provider.get(Provider.HERMES.value, 0)
            ),
            "failed": summary.failed,
            "excluded": summary.excluded,
            "excluded_by_reason": {
                reason: int(summary.excluded_by_reason.get(reason, 0))
                for reason in SIDEBAR_EXCLUSION_REASONS
            },
        }

    async def _after_successful_scan(self, summary: ScanSummary) -> None:
        if summary.failed:
            return
        sidebar_registered = await self._register_sidebar_after_successful_scan()
        providers_unhealthy = self._any_configured_provider_unhealthy()
        if (
            sidebar_registered
            and not providers_unhealthy
            and self._sidebar_executor is not None
        ):
            await self._run_post_scan_worker(
                self._sidebar_executor, "sidebar_executor_failed"
            )
        # Claude-side visibility is a separate lane from the Codex sidebar. The
        # 2026-07-17 claude-native-session-visibility design requires that it
        # "must not reuse or couple transitions to session_sidebar_jobs, which
        # remains specific to Codex" -- pausing the Codex sidebar is a supported
        # state and must not silently stop desktop registry records.
        #
        # It is deliberately outside the provider-health gate too: floating and
        # registering mirrors only reads the local state database and writes
        # local registry files, so it cannot depend on Codex reachability. Codex
        # scans hang on this host (codex_scan_failed), which otherwise starves
        # the Claude lane indefinitely.
        if self._mirror_float is not None:
            await self._run_post_scan_worker(
                self._mirror_float, "mirror_float_failed"
            )

    def _any_configured_provider_unhealthy(self) -> bool:
        configured_providers = set(self._adapters).intersection(_EXTERNAL_PROVIDERS)
        return any(
            self._provider_health[provider]["last_success"] is None
            or self._provider_health[provider]["degraded_reason"] is not None
            for provider in configured_providers
        )

    async def _run_post_scan_worker(self, worker: Any, error_code: str) -> None:
        worker_task = asyncio.create_task(asyncio.to_thread(worker.run_once))
        try:
            await asyncio.shield(worker_task)
        except asyncio.CancelledError:
            await asyncio.gather(worker_task, return_exceptions=True)
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            self._record_error_code(error_code)

    async def _register_sidebar_after_successful_scan(self) -> bool:
        sidebar = self._config.sidebar
        if not sidebar.enabled or not sidebar.continuous:
            return False
        try:
            registration = await self.register_sidebar_jobs_once(
                limit=sidebar.continuous_batch_limit,
            )
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            self._record_error_code("sidebar_registration_failed")
            return False
        if registration.failed:
            self._record_error_code("sidebar_registration_failed")
            return False
        return True

    async def _creation_capacity(
        self,
        policy: MirrorPolicy,
        *,
        now: float,
    ) -> int:
        if not _supports_scan_state(self._store):
            return policy.creates_per_minute
        state = await asyncio.to_thread(
            _call,
            self._store,
            "get_state",
            _RATE_STATE_KEY,
        )
        recent = _decode_creation_rate_state(state, now=now)
        return max(0, policy.creates_per_minute - len(recent))

    async def _reserve_creation_capacity(
        self,
        jobs: Sequence[object],
        *,
        now: float,
    ) -> None:
        if not jobs or not _supports_scan_state(self._store):
            return
        state = await asyncio.to_thread(
            _call,
            self._store,
            "get_state",
            _RATE_STATE_KEY,
        )
        recent = _decode_creation_rate_state(state, now=now)
        reserved = [*recent, *([now] * len(jobs))]
        await asyncio.to_thread(
            _call,
            self._store,
            "set_state",
            _RATE_STATE_KEY,
            {"version": 1, "attempted_at": reserved},
        )

    async def _load_breaker_progress(self) -> BatchProgress:
        if not _supports_scan_state(self._store):
            return BatchProgress()
        state = await asyncio.to_thread(
            _call,
            self._store,
            "get_state",
            _BREAKER_STATE_KEY,
        )
        return _decode_breaker_state(state)

    async def _save_breaker_progress(self, progress: BatchProgress) -> None:
        if not _supports_scan_state(self._store):
            return
        await asyncio.to_thread(
            _call,
            self._store,
            "set_state",
            _BREAKER_STATE_KEY,
            {
                "version": 1,
                "attempts": progress.attempts,
                "errors": progress.errors,
            },
        )

    async def _ensure_continuous_watermark(self) -> None:
        if self._continuous_watermark is not None:
            return
        if not isinstance(self._store, SessionBridgeStore):
            return
        watermark = await asyncio.to_thread(
            load_continuous_watermark,
            self._store,
        )
        if watermark is None:
            watermark = float(self._clock())
            await asyncio.to_thread(
                persist_continuous_watermark,
                self._store,
                watermark,
            )
        self._continuous_watermark = watermark

    async def _discovery_mode(self, provider: Provider) -> DiscoveryMode:
        if not self._config.mirrors.automatic_creation or not isinstance(
            self._store, SessionBridgeStore
        ):
            return DiscoveryMode.CONTINUOUS
        state = await asyncio.to_thread(
            _call,
            self._store,
            "get_state",
            _BACKFILL_KEYS[provider],
        )
        completed, _ = _decode_backfill_state(state)
        return DiscoveryMode.CONTINUOUS if completed else DiscoveryMode.INITIAL_BACKFILL

    async def _load_backfill_processed(
        self,
        provider: Provider,
        discovery_mode: DiscoveryMode,
    ) -> set[str]:
        if discovery_mode is not DiscoveryMode.INITIAL_BACKFILL:
            return set()
        state = await asyncio.to_thread(
            _call,
            self._store,
            "get_state",
            _BACKFILL_KEYS[provider],
        )
        _, processed = _decode_backfill_state(state)
        return processed

    async def _record_backfill_successes(
        self,
        provider: Provider,
        discovery_mode: DiscoveryMode,
        native_ids: Sequence[str],
    ) -> None:
        if discovery_mode is not DiscoveryMode.INITIAL_BACKFILL or not native_ids:
            return
        processed = await self._load_backfill_processed(provider, discovery_mode)
        processed.update(native_ids)
        await asyncio.to_thread(
            _call,
            self._store,
            "set_state",
            _BACKFILL_KEYS[provider],
            {
                "version": 1,
                "completed": False,
                "processed_native_ids": sorted(processed),
            },
        )

    async def _complete_backfill_if_drained(
        self,
        provider: Provider,
        discovery_mode: DiscoveryMode,
    ) -> None:
        if discovery_mode is not DiscoveryMode.INITIAL_BACKFILL:
            return
        pending = await self._load_pending(provider)
        if pending:
            return
        processed = await self._load_backfill_processed(provider, discovery_mode)
        await asyncio.to_thread(
            _call,
            self._store,
            "set_state",
            _BACKFILL_KEYS[provider],
            {
                "version": 1,
                "completed": True,
                "processed_native_ids": sorted(processed),
            },
        )

    async def _load_codex_seen_ids(self) -> set[str]:
        state = await asyncio.to_thread(
            _call,
            self._store,
            "get_state",
            _CODEX_SEEN_KEY,
        )
        return set(_decode_native_id_set_state(state, label="Codex seen"))

    async def _save_codex_seen_ids(self, native_ids: set[str]) -> None:
        await asyncio.to_thread(
            _call,
            self._store,
            "set_state",
            _CODEX_SEEN_KEY,
            {"version": 1, "native_ids": sorted(native_ids)},
        )

    async def _cataloged_codex_rows(
        self, native_ids: Sequence[str]
    ) -> dict[str, Mapping[str, Any]]:
        reader = getattr(self._store, "get_external_session", None)
        if not callable(reader):
            return {}
        cataloged: dict[str, Mapping[str, Any]] = {}
        for native_id in native_ids:
            row = await asyncio.to_thread(
                reader,
                canonical_session_id(Provider.CODEX, native_id),
            )
            if isinstance(row, Mapping):
                cataloged[native_id] = row
        return cataloged

    async def _load_continuation_reconcile_cursor(self) -> str | None:
        if not _supports_scan_state(self._store):
            return None
        state = await asyncio.to_thread(
            _call,
            self._store,
            "get_state",
            _CONTINUATION_RECONCILE_CURSOR_KEY,
        )
        return _decode_continuation_reconcile_cursor(state)

    async def _save_continuation_reconcile_cursor(
        self,
        after_bridge_id: object,
    ) -> None:
        if not _supports_scan_state(self._store):
            return
        normalized = (
            after_bridge_id.strip()
            if isinstance(after_bridge_id, str) and after_bridge_id.strip()
            else None
        )
        await asyncio.to_thread(
            _call,
            self._store,
            "set_state",
            _CONTINUATION_RECONCILE_CURSOR_KEY,
            {"version": 1, "after_bridge_id": normalized},
        )

    async def _maybe_enqueue_automatic(
        self,
        projection: SessionProjection,
        *,
        discovery_mode: DiscoveryMode,
    ) -> None:
        if not self._config.mirrors.automatic_creation:
            return
        await self._ensure_continuous_watermark()
        if self._continuous_watermark is None or not isinstance(
            self._store, SessionBridgeStore
        ):
            self._record_error_code("automatic_mirror_store_unavailable")
            return
        policy = self._mirror_policy()
        context = EligibilityContext(
            now=float(self._clock()),
            discovery_mode=discovery_mode,
            continuous_watermark=self._continuous_watermark,
            existing_target_mappings=frozenset(),
            policy=policy,
        )
        candidates = eligible_mirror_candidates((projection,), context)
        for candidate in candidates:
            try:
                await asyncio.to_thread(
                    enqueue_mirror_job,
                    self._store,
                    candidate.source_session_id,
                    candidate.target_provider,
                    policy=policy,
                    candidate=candidate,
                    context=context,
                )
            except PermissionError:
                continue
            except Exception as exc:
                self._record_error_code("automatic_mirror_enqueue_failed")
                raise RuntimeError("automatic mirror enqueue failed") from exc

    async def _process_claimed_job(
        self,
        job: dict[str, Any],
        *,
        policy: MirrorPolicy,
    ) -> MirrorJobState:
        target_provider = Provider(job["target_provider"])
        bridge_id = _bridge_id(job)
        expected_native_id = (
            _claude_native_id(job) if target_provider is Provider.CLAUDE else None
        )
        sidecar: dict[str, Any] = {
            "version": 1,
            "phase": "provider_call_started",
            "bridge_id": bridge_id,
            "target_provider": target_provider.value,
            "policy_generation": policy.generation,
            "attempts": job["attempts"],
        }
        if expected_native_id is not None:
            sidecar["expected_native_id"] = expected_native_id
        await asyncio.to_thread(
            _call,
            self._store,
            "set_state",
            _attempt_key(job),
            sidecar,
        )

        try:
            target_adapter = self._target_adapter(target_provider)
            metadata = await self._launch_metadata(job["source_session_id"])
            kwargs: dict[str, Any] = {
                "title": metadata["title"],
                "source_session_id": job["source_session_id"],
                "bridge_id": bridge_id,
                "policy_generation": policy.generation,
            }
            if metadata["cwd"] is not None:
                kwargs["cwd"] = metadata["cwd"]
            if expected_native_id is not None:
                kwargs["native_id"] = expected_native_id
            result = await self._provider_call(
                _call,
                target_adapter,
                "create_placeholder",
                **kwargs,
            )
            native_id = _placeholder_native_id(result)
            self._registration_turn_fallback = bool(
                getattr(result, "used_registration_turn", False)
            )
            require_marker_payload = False
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except AmbiguousPlaceholderCreation as exc:
            native_id = exc.native_id or expected_native_id
            require_marker_payload = True
            if native_id is None:
                await self._fail_job_manually(
                    job,
                    code="target_identity_unproven",
                    detail="target placeholder identity could not be proven",
                )
                return MirrorJobState.MANUAL_FAILURE
        except PlaceholderCreationError as exc:
            return await self._record_job_failure(job, policy=policy, code=exc.code)
        except Exception:
            await self._fail_job_manually(
                job,
                code="target_outcome_unknown",
                detail="target placeholder outcome could not be proven",
            )
            return MirrorJobState.MANUAL_FAILURE

        try:
            target = await self._index_exact_target(
                target_provider,
                native_id,
                bridge_id=bridge_id,
                source_session_id=job["source_session_id"],
                policy_generation=policy.generation,
                require_marker_payload=require_marker_payload,
            )
            await self._complete_cataloged_target(
                job,
                bridge_id=bridge_id,
                target=target,
            )
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            await self._fail_job_manually(
                job,
                code="target_identity_unproven",
                detail="created mirror target identity could not be proven",
            )
            return MirrorJobState.MANUAL_FAILURE
        return MirrorJobState.SUCCEEDED

    async def _record_job_failure(
        self,
        job: dict[str, Any],
        *,
        policy: MirrorPolicy,
        code: str,
    ) -> MirrorJobState:
        provider = Provider(job["target_provider"])
        safe_code = _safe_target_error_code(provider, code)
        self._record_error_code(f"{provider.value}_target_failed")
        if job["attempts"] >= policy.max_attempts:
            await self._fail_job_manually(
                job,
                code=safe_code,
                detail="target placeholder creation failed",
            )
            return MirrorJobState.MANUAL_FAILURE
        delay = retry_delay_seconds(job["idempotency_key"], job["attempts"])
        await asyncio.to_thread(
            _call,
            self._store,
            "retry_job",
            job["id"],
            code=safe_code,
            detail="target placeholder creation failed",
            next_attempt_at=float(self._clock()) + delay,
        )
        return MirrorJobState.RETRY

    async def _retry_provider_call_not_started(
        self,
        job: Mapping[str, Any],
    ) -> None:
        await asyncio.to_thread(
            _call,
            self._store,
            "retry_job",
            job["id"],
            code="provider_call_not_started",
            detail="provider call was not started before restart",
            next_attempt_at=float(self._clock()),
        )

    async def _fail_job_manually(
        self,
        job: Mapping[str, Any],
        *,
        code: str,
        detail: str,
    ) -> None:
        target_provider = job.get("target_provider")
        if isinstance(target_provider, str):
            self._record_error_code(f"{target_provider}_target_failed")
        await asyncio.to_thread(
            _call,
            self._store,
            "fail_job_manually",
            job["id"],
            code=code,
            detail=detail,
        )

    async def _index_exact_target(
        self,
        provider: Provider,
        native_id: str,
        *,
        bridge_id: str,
        source_session_id: str,
        policy_generation: int,
        require_marker_payload: bool,
    ) -> dict[str, Any]:
        adapter = self._adapter(provider)
        if provider is Provider.CLAUDE:
            source = await self._provider_call(
                _call,
                adapter,
                "find_native_session",
                native_id,
            )
            if source is None:
                raise RuntimeError("created Claude session is not readable")
            parsed = await self._provider_call(_call, adapter, "parse", source)
            projection = _projection_from_parse(parsed)
        else:
            summary = await self._provider_call(
                _call,
                adapter,
                "find_native_thread",
                native_id,
            )
            if summary is None:
                raise RuntimeError("created Codex thread is not readable")
            projection = await self._provider_call(
                _call,
                adapter,
                "project_thread",
                summary,
            )
        if (
            not isinstance(projection, SessionProjection)
            or projection.provider is not provider
            or projection.native_id != native_id
            or projection.origin_kind is not OriginKind.BRIDGE_PLACEHOLDER
            or projection.origin_bridge_id != bridge_id
        ):
            raise RuntimeError("created target provenance is not exact")
        if require_marker_payload:
            authenticated = await self._provider_call(
                _call,
                adapter,
                "projection_has_marker_payload",
                projection,
                BridgeMarkerPayload(
                    bridge_id=bridge_id,
                    source_session_id=source_session_id,
                    target_provider=provider,
                    policy_generation=policy_generation,
                ),
            )
            if authenticated is not True:
                raise RuntimeError("created target marker payload is not exact")
        result = await asyncio.to_thread(
            _upsert,
            self._store,
            projection,
            False,
        )
        return {
            "session_id": result.session_id,
            "provider": provider.value,
            "native_id": native_id,
            "origin_bridge_id": bridge_id,
        }

    async def _complete_cataloged_target(
        self,
        job: Mapping[str, Any],
        *,
        bridge_id: str,
        target: Mapping[str, Any],
    ) -> None:
        provider = Provider(job["target_provider"])
        native_id = target.get("native_id")
        session_id = target.get("session_id")
        if (
            target.get("provider") != provider.value
            or target.get("origin_bridge_id") != bridge_id
            or not isinstance(native_id, str)
            or not native_id
            or session_id != canonical_session_id(provider, native_id)
        ):
            raise RuntimeError("cataloged target identity is not exact")
        await asyncio.to_thread(
            _call,
            self._store,
            "complete_job",
            job["id"],
            target_native_id=native_id,
            target_session_id=session_id,
            bridge_id=bridge_id,
        )

    def _target_adapter(self, provider: Provider) -> object:
        try:
            return self._target_adapters[provider]
        except KeyError as exc:
            raise RuntimeError("target adapter is not configured") from exc

    async def _launch_metadata(self, source_session_id: str) -> dict[str, str | None]:
        reader = getattr(self._store, "get_session_launch_metadata", None)
        if not callable(reader):
            return {"title": "Hermes bridge placeholder", "cwd": None}
        raw = await asyncio.to_thread(reader, source_session_id)
        if not isinstance(raw, Mapping):
            return {"title": "Hermes bridge placeholder", "cwd": None}
        raw_title = raw.get("title")
        title = (
            " ".join(raw_title.split())
            if isinstance(raw_title, str) and raw_title.strip()
            else "Hermes bridge placeholder"
        )
        raw_cwd = raw.get("cwd")
        cwd = raw_cwd.strip() if isinstance(raw_cwd, str) and raw_cwd.strip() else None
        if cwd is not None and ("\r" in cwd or "\n" in cwd):
            cwd = None
        return {"title": title[:200], "cwd": cwd}

    async def _scan_provider(self, provider: Provider) -> ScanSummary:
        async with self._scan_locks[provider]:
            started = float(self._monotonic())
            discovery_mode = await self._discovery_mode(provider)
            try:
                if provider is Provider.CLAUDE:
                    summary = await self._scan_claude(discovery_mode)
                elif provider is Provider.CODEX:
                    summary = await self._scan_codex(discovery_mode)
                else:
                    raise ValueError("unsupported scan provider")
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                self._mark_scan_failure(provider, stage="incremental_scan", exc=exc)
                return ScanSummary(
                    provider=provider,
                    discovered=0,
                    indexed=0,
                    rebuilt=0,
                    failed=1,
                    duration_ms=self._elapsed_ms(started),
                )

            if summary.failed:
                self._mark_scan_failure(
                    provider, stage="incremental_summary_failed", summary=summary
                )
            else:
                await self._complete_backfill_if_drained(provider, discovery_mode)
                self._mark_scan_success(provider)
            return ScanSummary(
                provider=provider,
                discovered=summary.discovered,
                indexed=summary.indexed,
                rebuilt=summary.rebuilt,
                failed=summary.failed,
                duration_ms=self._elapsed_ms(started),
            )

    async def _scan_all_history_provider(self, provider: Provider) -> ScanSummary:
        async with self._scan_locks[provider]:
            started = float(self._monotonic())
            try:
                if provider is Provider.CLAUDE:
                    summary = await self._scan_all_claude_history()
                elif provider is Provider.CODEX:
                    summary = await self._scan_all_codex_history()
                else:
                    raise ValueError("unsupported scan provider")
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                self._mark_scan_failure(provider, stage="full_history_scan", exc=exc)
                return ScanSummary(
                    provider=provider,
                    discovered=0,
                    indexed=0,
                    rebuilt=0,
                    failed=1,
                    duration_ms=self._elapsed_ms(started),
                )

            if summary.failed:
                self._mark_scan_failure(
                    provider, stage="full_history_summary_failed", summary=summary
                )
            else:
                self._mark_scan_success(provider)
            return ScanSummary(
                provider=provider,
                discovered=summary.discovered,
                indexed=summary.indexed,
                rebuilt=summary.rebuilt,
                failed=summary.failed,
                duration_ms=self._elapsed_ms(started),
            )

    async def _scan_all_claude_history(self) -> ScanSummary:
        adapter = self._adapter(Provider.CLAUDE)
        discovered_paths = await self._provider_call(_call, adapter, "discover")
        ordered_paths, unavailable_paths, _fingerprints = await asyncio.to_thread(
            _sort_claude_paths,
            discovered_paths,
        )
        paths: list[Path] = []
        seen_native_ids: set[str] = set()
        for path in ordered_paths:
            if path.stem in seen_native_ids:
                continue
            seen_native_ids.add(path.stem)
            paths.append(path)

        indexed = 0
        rebuilt = 0
        failed = len(unavailable_paths)
        for path in paths:
            try:
                parsed = await self._provider_call(_call, adapter, "parse", path)
                projection = _projection_from_parse(parsed)
                result = await asyncio.to_thread(
                    _upsert,
                    self._store,
                    projection,
                    True,
                )
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                failed += 1
                continue
            indexed += 1
            rebuilt += int(not result.first_seen)
        return ScanSummary(
            provider=Provider.CLAUDE,
            discovered=len(paths) + len(unavailable_paths),
            indexed=indexed,
            rebuilt=rebuilt,
            failed=failed,
            duration_ms=0,
        )

    async def _scan_all_codex_history(self) -> ScanSummary:
        adapter = self._adapter(Provider.CODEX)
        discovered_summaries = await self._provider_call(
            _codex_full_inventory,
            adapter,
            include_archived=True,
        )
        summaries = _sort_codex_summaries(discovered_summaries)
        indexed = 0
        rebuilt = 0
        failed = 0
        locally_owned = 0
        deferred = 0
        for thread_summary in summaries:
            try:
                projection = await self._provider_call(
                    _call,
                    adapter,
                    "project_thread",
                    thread_summary,
                )
                result = await asyncio.to_thread(
                    _upsert,
                    self._store,
                    projection,
                    True,
                )
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except LocalSessionOwnsCanonicalId:
                # Hermes materialises its own Codex-provider sessions under the
                # same canonical id the bridge uses for imported native threads.
                # The local row is authoritative and is never adopted, but this
                # is a benign steady state -- counting it as a scan failure
                # degrades the provider and starves every downstream lane.
                locally_owned += 1
                continue
            except (TimeoutError, StaleExternalProjection) as exc:
                # 2026-08-13: same treatment as _scan_codex_persistent, which was the
                # only codex path that had it. A transport timeout says something about
                # the HOST (the app-server did not answer inside its 30s bound), and a
                # stale projection is a no-op -- neither means this thread is bad. Left
                # in the generic branch below they each count a failure, which degrades
                # the provider. Observed with the tail of the backfill:
                # ScanSummary(discovered=22, indexed=0, failed=1) recurring forever with
                # two app-server timeouts and none of the persistent path's deferral
                # lines, because the timeout was landing HERE instead.
                deferred += 1
                self._record_codex_scan_diagnostic(
                    stage="full_history_project",
                    native_id=getattr(thread_summary, "native_id", None),
                    exc=exc,
                    adapter=adapter,
                )
                continue
            except Exception as exc:
                self._record_codex_scan_diagnostic(
                    stage="full_history_project",
                    native_id=getattr(thread_summary, "native_id", None),
                    exc=exc,
                    adapter=adapter,
                )
                failed += 1
                continue
            indexed += 1
            rebuilt += int(not result.first_seen)
        if deferred:
            # Counted, never silent: retried next cycle, never a scan failure.
            try:
                _LOG.warning(
                    "codex_scan_diagnostic stage=full_history_project "
                    "code=app_server_timeout deferred=%d indexed=%d",
                    deferred,
                    indexed,
                )
            except Exception:
                pass
        if locally_owned:
            # Counted, never silent: these threads stay outside the bridge
            # catalog by design.
            try:
                _LOG.info(
                    "codex_scan_diagnostic stage=full_history_project code=%s excluded=%d",
                    _CODEX_SCAN_LOCAL_OWNER_CODE,
                    locally_owned,
                )
            except Exception:
                pass
        return ScanSummary(
            provider=Provider.CODEX,
            discovered=len(summaries),
            indexed=indexed,
            rebuilt=rebuilt,
            failed=failed,
            duration_ms=0,
        )

    async def _scan_claude(self, discovery_mode: DiscoveryMode) -> ScanSummary:
        if _supports_scan_state(self._store):
            return await self._scan_claude_persistent(discovery_mode)
        return await self._scan_claude_immediate(discovery_mode)

    async def _scan_claude_immediate(
        self,
        discovery_mode: DiscoveryMode,
    ) -> ScanSummary:
        adapter = self._adapter(Provider.CLAUDE)
        paths = await self._provider_call(_call, adapter, "discover")
        discovered = len(paths)
        indexed = 0
        rebuilt = 0
        failed = 0
        incremental = self._claude_incremental_reads(adapter)
        cursors = self._claude_immediate_cursors
        # Nothing persists on this path, so drop cursors for transcripts the
        # walk no longer sees rather than growing the map for the life of the
        # process.
        for stale_key in set(cursors) - {str(path) for path in paths}:
            cursors.pop(stale_key, None)
        for path in paths:
            cursor_key = str(path)
            try:
                previous = cursors.get(cursor_key) if incremental else None
                parsed = await self._parse_claude(adapter, path, previous)
                projection = _projection_from_parse(parsed)
                # ``parsed.rebuild`` is the adapter reporting that it fell back
                # to reading the whole file, so a rebuild here is always backed
                # by a complete projection. See _scan_claude_persistent for why
                # that pairing is the invariant that matters.
                should_rebuild = bool(getattr(parsed, "rebuild", False))
                result = await asyncio.to_thread(
                    _upsert,
                    self._store,
                    projection,
                    should_rebuild,
                )
                await self._maybe_enqueue_automatic(
                    projection,
                    discovery_mode=discovery_mode,
                )
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                failed += 1
                continue
            indexed += 1
            rebuilt += int(should_rebuild or result.rebuilt)
            # Only a committed upsert may advance the cursor: an offset moved
            # past bytes the store never accepted would skip them forever.
            _remember_claude_cursor(cursors, cursor_key, parsed)
        return ScanSummary(
            provider=Provider.CLAUDE,
            discovered=discovered,
            indexed=indexed,
            rebuilt=rebuilt,
            failed=failed,
            duration_ms=0,
        )

    async def _scan_codex(self, discovery_mode: DiscoveryMode) -> ScanSummary:
        if _supports_scan_state(self._store):
            return await self._scan_codex_persistent(discovery_mode)
        return await self._scan_codex_immediate(discovery_mode)

    async def _scan_codex_immediate(
        self,
        discovery_mode: DiscoveryMode,
    ) -> ScanSummary:
        adapter = self._adapter(Provider.CODEX)
        summaries = await self._provider_call(
            _codex_inventory,
            adapter,
            include_archived=self._config.catalog.include_archived_codex,
        )
        discovered = len(summaries)
        indexed = 0
        failed = 0
        locally_owned = 0
        deferred = 0
        for thread_summary in summaries:
            try:
                projection = await self._provider_call(
                    _call,
                    adapter,
                    "project_thread",
                    thread_summary,
                )
                await asyncio.to_thread(
                    _upsert,
                    self._store,
                    projection,
                    False,
                )
                await self._maybe_enqueue_automatic(
                    projection,
                    discovery_mode=discovery_mode,
                )
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except LocalSessionOwnsCanonicalId:
                # Hermes owns this canonical id; never adopted, never a failure.
                locally_owned += 1
                continue
            except (TimeoutError, StaleExternalProjection) as exc:
                # Host-side timeout or a no-op stale projection: retried next cycle,
                # never a provider-degrading failure. Mirrors _scan_codex_persistent.
                deferred += 1
                self._record_codex_scan_diagnostic(
                    stage="immediate_project",
                    native_id=getattr(thread_summary, "native_id", None),
                    exc=exc,
                    adapter=adapter,
                )
                continue
            except Exception as exc:
                self._record_codex_scan_diagnostic(
                    stage="immediate_project",
                    native_id=getattr(thread_summary, "native_id", None),
                    exc=exc,
                    adapter=adapter,
                )
                failed += 1
                continue
            indexed += 1
        if deferred:
            try:
                _LOG.warning(
                    "codex_scan_diagnostic stage=immediate_project "
                    "code=app_server_timeout deferred=%d indexed=%d",
                    deferred,
                    indexed,
                )
            except Exception:
                pass
        if locally_owned:
            try:
                _LOG.info(
                    "codex_scan_diagnostic stage=immediate_project code=%s excluded=%d",
                    _CODEX_SCAN_LOCAL_OWNER_CODE,
                    locally_owned,
                )
            except Exception:
                pass
        return ScanSummary(
            provider=Provider.CODEX,
            discovered=discovered,
            indexed=indexed,
            rebuilt=0,
            failed=failed,
            duration_ms=0,
        )

    async def _scan_claude_persistent(
        self,
        discovery_mode: DiscoveryMode,
    ) -> ScanSummary:
        provider = Provider.CLAUDE
        adapter = self._adapter(provider)
        pending_ids = await self._load_pending(provider)
        progress = await self._load_progress(provider)
        committed_fingerprints = await self._load_claude_fingerprints(
            _CLAUDE_FINGERPRINT_KEY
        )
        staged_fingerprints = await self._load_claude_fingerprints(_CLAUDE_STAGED_KEY)
        incremental = self._claude_incremental_reads(adapter)
        committed_cursors = await self._load_claude_cursors() if incremental else {}
        backfill_processed = await self._load_backfill_processed(
            provider,
            discovery_mode,
        )
        discovered_paths = await self._provider_call(_call, adapter, "discover")
        ordered_paths, unavailable_paths, discovered_fingerprints = (
            await asyncio.to_thread(
                _sort_claude_paths,
                discovered_paths,
                self._claude_stat_cache,
            )
        )
        paths_by_native_id: dict[str, Path] = {}
        current_fingerprints: dict[str, dict[str, int]] = {}
        changed_ids: list[str] = []
        unavailable_ids = {path.stem for path in unavailable_paths}
        for path in ordered_paths:
            native_id = path.stem
            if native_id in paths_by_native_id:
                continue
            fingerprint = discovered_fingerprints.get(native_id)
            if fingerprint is None:
                unavailable_ids.add(native_id)
                continue
            paths_by_native_id[native_id] = path
            current_fingerprints[native_id] = fingerprint
            if committed_fingerprints.get(native_id) != fingerprint or (
                discovery_mode is DiscoveryMode.INITIAL_BACKFILL
                and native_id not in backfill_processed
            ):
                changed_ids.append(native_id)

        pending_set = set(pending_ids)
        promoted_ids = [
            native_id
            for native_id in changed_ids
            if native_id not in pending_set
            or staged_fingerprints.get(native_id) != current_fingerprints.get(native_id)
        ]
        staged_ids = _merge_native_ids(promoted_ids, pending_ids)
        await self._save_pending(provider, staged_ids)
        await self._save_claude_fingerprints(
            _CLAUDE_STAGED_KEY,
            {
                native_id: current_fingerprints.get(
                    native_id, staged_fingerprints.get(native_id, {})
                )
                for native_id in staged_ids
            },
        )
        selected_ids = [
            native_id for native_id in staged_ids if native_id not in unavailable_ids
        ][: self._scan_batch_size]
        indexed = 0
        rebuilt = 0
        failed_ids: list[str] = []
        succeeded_ids: list[str] = []
        locally_owned = 0
        for native_id in selected_ids:
            try:
                path = paths_by_native_id.get(native_id)
                if path is None:
                    find_by_stem = getattr(
                        adapter,
                        "find_native_sessions_by_stem",
                        None,
                    )
                    if callable(find_by_stem):
                        stem_matches = await self._provider_call(
                            _call,
                            adapter,
                            "find_native_sessions_by_stem",
                            native_id,
                        )
                        try:
                            path = next(iter(stem_matches), None)
                        except TypeError as exc:
                            raise RuntimeError(
                                "Claude stem lookup returned no path list"
                            ) from exc
                    else:
                        path = await self._provider_call(
                            _call,
                            adapter,
                            "find_native_session",
                            native_id,
                        )
                if path is None:
                    # Complete discovery plus an exact filename-stem lookup
                    # proved that this persisted queue entry no longer has a
                    # source. Avoid the broader record-ID probe here: pending
                    # scan identities are transcript stems, and probing every
                    # unrelated transcript can block incremental discovery.
                    # Retire only the queue entry; a later reappearance is
                    # rediscovered from its fingerprint and no catalog/native
                    # history is deleted or fabricated here.
                    continue
                # Reuse the discovery-time fingerprint rather than re-stat.
                # It is also the SAFER value to commit: it is never newer than
                # the bytes we are about to parse, so a transcript that changed
                # in between simply re-stages next cycle. Committing a fresher
                # fingerprint than the content actually read could swallow that
                # change permanently.
                fingerprint = discovered_fingerprints.get(native_id)
                if fingerprint is None:
                    fingerprint = await asyncio.to_thread(
                        _claude_path_fingerprint, path
                    )
                current_fingerprints[native_id] = fingerprint
                committed_fingerprint = committed_fingerprints.get(native_id)
                shrank = (
                    committed_fingerprint is not None
                    and fingerprint["size"] < committed_fingerprint.get("size", 0)
                )
                # A shrunken transcript has to be re-read IN FULL, because the
                # rebuild below deletes every message mapped to the session
                # before re-inserting whatever the projection carries: pairing
                # a rebuild with a cursor-shortened projection would delete the
                # history and re-insert only the tail. The adapter's own
                # invalidation does not cover this. Past the 64 KiB head sample
                # a transcript can lose bytes that sit AFTER the cursor offset
                # while the head hash and the newline-boundary check both still
                # pass, and the read then returns rebuild=False with an empty
                # delta.
                previous = (
                    committed_cursors.get(native_id)
                    if incremental and not shrank
                    else None
                )
                parsed = await self._parse_claude(adapter, path, previous)
                projection = _projection_from_parse(parsed)
                # Rebuild only when the transcript was REWRITTEN, not merely
                # appended to. This was `native_id in committed_fingerprints`
                # -- a membership test, true for every session ever scanned,
                # and redundant besides: staging above only promotes ids whose
                # fingerprint already CHANGED, so every id reaching this loop
                # tripped it. upsert_projection's rebuild branch DELETEs every
                # message for the session and re-INSERTs the whole projection,
                # so each cycle rewrote entire transcripts. Live bridge on
                # 2026-08-18: 101.2M lifetime inserts against 1.02M live rows,
                # 6,563 inserts/min for a net +20, feeding the FTS merge.
                # A shrunken file is the rewrite signal that still matters:
                # the non-rebuild path only ADDS rows missing from
                # external_message_map, so records dropped by a compaction or
                # rewind would otherwise linger forever.
                # Both disjuncts imply a full read: ``shrank`` forced
                # previous=None above, and ``parsed.rebuild`` is the adapter
                # reporting that it fell back to one.
                should_rebuild = bool(shrank or getattr(parsed, "rebuild", False))
                result = await asyncio.to_thread(
                    _upsert,
                    self._store,
                    projection,
                    should_rebuild,
                )
                await self._maybe_enqueue_automatic(
                    projection,
                    discovery_mode=discovery_mode,
                )
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except (LocalSessionOwnsCanonicalId, StaleExternalProjection):
                # 2026-08-13: all three CODEX scan paths handled these; none of the
                # CLAUDE paths did. Hermes materialises its own claude-provider
                # sessions under the canonical id the bridge wants for an imported
                # transcript -- benign, never adopted. A stale projection is a no-op.
                # Left to the generic branch below each appends to failed_ids, which
                # _merge_native_ids rolls into remaining_ids and _save_pending
                # RE-STAGES, so the same transcript is retried every cycle forever:
                # claude sat at `remaining: 1` indefinitely with degraded_reason
                # scan_failed. Observed on claude:5dc2e902-... (a local 38-message
                # session). Excluding it here let claude's backfill reach 0 for the
                # first time.
                locally_owned += 1
                continue
            except Exception:
                failed_ids.append(native_id)
                continue
            indexed += 1
            rebuilt += int(should_rebuild or result.rebuilt)
            succeeded_ids.append(native_id)
            committed_fingerprints[native_id] = current_fingerprints[native_id]
            # Same rule as the immediate path: the cursor advances only once
            # the store has accepted the messages it covers.
            _remember_claude_cursor(committed_cursors, native_id, parsed)

        selected_set = set(selected_ids)
        unselected_ids = [
            native_id for native_id in staged_ids if native_id not in selected_set
        ]
        remaining_ids = _merge_native_ids(unselected_ids, failed_ids)
        await self._save_pending(provider, remaining_ids)
        await self._save_claude_fingerprints(
            _CLAUDE_STAGED_KEY,
            {
                native_id: current_fingerprints.get(
                    native_id, staged_fingerprints.get(native_id, {})
                )
                for native_id in remaining_ids
            },
        )
        await self._save_claude_fingerprints(
            _CLAUDE_FINGERPRINT_KEY,
            committed_fingerprints,
        )
        if incremental:
            # Pin the cursor map to the fingerprint key set so it cannot
            # outgrow the state row it rides beside.
            await self._save_claude_cursors({
                native_id: cursor
                for native_id, cursor in committed_cursors.items()
                if native_id in committed_fingerprints
            })
        await self._commit_success_progress(
            provider,
            succeeded_ids=succeeded_ids,
            remaining=len(remaining_ids),
            previous_progress=progress,
        )
        await self._record_backfill_successes(
            provider,
            discovery_mode,
            succeeded_ids,
        )
        return ScanSummary(
            provider=provider,
            discovered=len(set(staged_ids) | unavailable_ids),
            indexed=indexed,
            rebuilt=rebuilt,
            failed=len(failed_ids) + len(unavailable_ids),
            duration_ms=0,
        )

    async def _parse_claude(
        self,
        adapter: object,
        path: Path,
        previous: ClaudeCursor | None,
    ) -> Any:
        if previous is None:
            return await self._provider_call(_call, adapter, "parse", path)
        return await self._provider_call(_call, adapter, "parse", path, previous)

    def _claude_incremental_reads(self, adapter: object) -> bool:
        """Whether this cycle may hand the adapter a stored read cursor.

        Two gates. The adapter has to accept a second ``parse`` argument at all
        -- ``ClaudeReadableSource`` only promises ``parse(path)``, and a stand-in
        that never widened its signature must keep working. And automatic
        mirroring has to be off: ``classify_mirror_eligibility`` reads the
        session's FIRST meaningful user message straight out of the projection
        to clear its debounce window, which a delta projection cannot carry, so
        an incremental read would quietly stop every automatic mirror from
        being enqueued. Full reads stay the price of that feature.
        """

        if self._config.mirrors.automatic_creation:
            return False
        return _parse_accepts_cursor(adapter)

    async def _load_claude_cursors(self) -> dict[str, ClaudeCursor]:
        state = await asyncio.to_thread(
            _call, self._store, "get_state", _CLAUDE_CURSOR_KEY
        )
        return _decode_claude_cursors(state)

    async def _save_claude_cursors(
        self,
        cursors: Mapping[str, ClaudeCursor],
    ) -> None:
        encoded = {}
        for native_id, cursor in sorted(cursors.items()):
            payload = encode_claude_cursor(cursor)
            if payload is not None:
                encoded[native_id] = payload
        await asyncio.to_thread(
            _call,
            self._store,
            "set_state",
            _CLAUDE_CURSOR_KEY,
            {"version": 1, "sessions": encoded},
        )

    async def _load_claude_fingerprints(
        self,
        key: str,
    ) -> dict[str, dict[str, int]]:
        state = await asyncio.to_thread(_call, self._store, "get_state", key)
        return _decode_claude_fingerprints(state)

    async def _save_claude_fingerprints(
        self,
        key: str,
        fingerprints: Mapping[str, Mapping[str, int]],
    ) -> None:
        await asyncio.to_thread(
            _call,
            self._store,
            "set_state",
            key,
            {
                "version": 1,
                "sessions": {
                    native_id: dict(fingerprint)
                    for native_id, fingerprint in sorted(fingerprints.items())
                    if fingerprint
                },
            },
        )

    async def _commit_success_progress(
        self,
        provider: Provider,
        *,
        succeeded_ids: list[str],
        remaining: int,
        previous_progress: dict[str, int | str] | None,
    ) -> None:
        if succeeded_ids:
            indexed_total = (
                int(previous_progress["indexed_total"])
                if previous_progress is not None
                else 0
            ) + len(succeeded_ids)
            progress: dict[str, int | str] = {
                "version": 1,
                "last_committed_native_id": succeeded_ids[-1],
                "indexed_total": indexed_total,
                "remaining": remaining,
            }
        elif (
            previous_progress is not None
            and int(previous_progress["remaining"]) != remaining
        ):
            progress = {
                **previous_progress,
                "remaining": remaining,
            }
        else:
            return
        await asyncio.to_thread(
            _call,
            self._store,
            "set_state",
            _PROGRESS_KEYS[provider],
            progress,
        )
        self._backfill_progress[provider] = dict(progress)

    async def _scan_codex_persistent(
        self,
        discovery_mode: DiscoveryMode,
    ) -> ScanSummary:
        provider = Provider.CODEX
        adapter = self._adapter(provider)
        pending_ids = await self._load_pending(provider)
        progress = await self._load_progress(provider)
        seen_ids = await self._load_codex_seen_ids()
        recent_inventory = getattr(adapter, "list_recent_inventory", None)
        # An empty durable seen-set is a bootstrap or outage-recovery state.
        # The continuous watermark can be newer than every native task, so a
        # recent-only query would incorrectly declare recovery successful
        # without ever indexing the existing inventory.
        if (
            discovery_mode is DiscoveryMode.CONTINUOUS
            and self._continuous_watermark is not None
            and seen_ids
            and callable(recent_inventory)
        ):
            discovered_summaries = await self._provider_call(
                _codex_recent_inventory,
                adapter,
                include_archived=self._config.catalog.include_archived_codex,
                after=self._continuous_watermark,
                known_native_ids=frozenset(seen_ids),
            )
        else:
            discovered_summaries = await self._provider_call(
                _codex_inventory,
                adapter,
                include_archived=self._config.catalog.include_archived_codex,
            )
        ordered_summaries = _sort_codex_summaries(discovered_summaries)
        summaries_by_native_id: dict[str, object] = {}
        inventory_ids: list[str] = []
        for summary in ordered_summaries:
            native_id = _codex_native_id(summary)
            if native_id in summaries_by_native_id:
                continue
            summaries_by_native_id[native_id] = summary
            inventory_ids.append(native_id)

        cataloged_rows = await self._cataloged_codex_rows(inventory_ids)
        cataloged_ids = set(cataloged_rows)
        genuinely_new_ids = [
            native_id
            for native_id in inventory_ids
            if native_id not in seen_ids and native_id not in cataloged_ids
        ]
        trusted_origin_changed_ids: list[str] = []
        for native_id in inventory_ids:
            summary = summaries_by_native_id[native_id]
            trusted_bridge_id = getattr(
                summary, "trusted_origin_bridge_id", None
            )
            if trusted_bridge_id is None:
                continue
            cataloged = cataloged_rows.get(native_id)
            if cataloged is None:
                trusted_origin_changed_ids.append(native_id)
                continue
            if (
                cataloged.get("origin_bridge_id") != trusted_bridge_id
                or cataloged.get("origin_kind")
                not in {
                    OriginKind.BRIDGE_PLACEHOLDER.value,
                    OriginKind.BRIDGE_CONTINUATION.value,
                }
            ):
                trusted_origin_changed_ids.append(native_id)
        backfill_processed = await self._load_backfill_processed(
            provider,
            discovery_mode,
        )
        backfill_ids = (
            [
                native_id
                for native_id in inventory_ids
                if native_id not in backfill_processed
            ]
            if discovery_mode is DiscoveryMode.INITIAL_BACKFILL
            else []
        )
        staged_ids = _merge_native_ids(
            backfill_ids,
            genuinely_new_ids,
            trusted_origin_changed_ids,
            pending_ids,
        )
        await self._save_pending(provider, staged_ids)
        selected_ids = staged_ids[: self._scan_batch_size]
        indexed = 0
        locally_owned = 0
        deferred = 0
        vanished = 0
        for native_id in selected_ids:
            try:
                summary = summaries_by_native_id.get(native_id)
                if summary is None:
                    summary = await self._provider_call(
                        _call,
                        adapter,
                        "find_native_thread",
                        native_id,
                        **_cached_index_kwargs(adapter),
                    )
                if summary is None:
                    # 2026-08-14: a staged id the source can no longer resolve. This
                    # used to `raise RuntimeError`, which the generic handler below
                    # turns into failed=1 -- that aborts the rest of the batch AND
                    # leaves the id staged, so the same vanished thread was retried
                    # every cycle forever. Measured: 88 identical diagnostics for
                    # task:92a4c43cd63cdbff, ScanSummary(discovered=52, indexed=30,
                    # failed=1) on repeat, codex pinned at scan_failed with the tail of
                    # the backfill stuck behind it.
                    #
                    # A thread that no longer exists in Codex is not a scan failure and
                    # can never resolve: deleted, archived out of scope, or pruned since
                    # it was staged. Count it, log it, and fall through so
                    # _commit_scan_batch drains it from the staged set instead of
                    # re-staging it. Same reasoning as the LocalSessionOwnsCanonicalId /
                    # StaleExternalProjection / TimeoutError branches below.
                    vanished += 1
                    self._record_codex_scan_diagnostic(
                        stage="persistent_project",
                        native_id=native_id,
                        exc=RuntimeError("staged Codex thread is unavailable"),
                        adapter=adapter,
                    )
                    continue
                projection = await self._provider_call(
                    _call,
                    adapter,
                    "project_thread",
                    summary,
                )
                await asyncio.to_thread(
                    _upsert,
                    self._store,
                    projection,
                    False,
                )
                await self._maybe_enqueue_automatic(
                    projection,
                    discovery_mode=discovery_mode,
                )
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except LocalSessionOwnsCanonicalId:
                # Hermes owns this canonical id (its own Codex-provider session).
                # It is never adopted, but it must not poison the batch: this
                # handler returns on any error, so treating it as a failure
                # aborts the whole scan AND leaves the id staged, so every
                # subsequent cycle re-attempts the same thread forever and the
                # provider never leaves the degraded state.
                locally_owned += 1
                continue
            except (TimeoutError, StaleExternalProjection) as exc:
                # Same reasoning as the branch above, for the other two benign
                # conditions. A transport timeout describes the HOST, not this
                # thread; a stale projection is a no-op. The generic handler below
                # returns failed=1, which abandons the rest of the batch AND leaves
                # the id staged -- observed as one thread
                # (task:92a4c43cd63cdbff) failing every cycle with 22 items left.
                deferred += 1
                self._record_codex_scan_diagnostic(
                    stage="persistent_project",
                    native_id=native_id,
                    exc=exc,
                    adapter=adapter,
                )
                continue
            except Exception as exc:
                self._record_codex_scan_diagnostic(
                    stage="persistent_project",
                    native_id=native_id,
                    exc=exc,
                    adapter=adapter,
                )
                return ScanSummary(
                    provider=provider,
                    discovered=len(staged_ids),
                    indexed=indexed,
                    rebuilt=0,
                    failed=1,
                    duration_ms=0,
                )
            indexed += 1
        if vanished:
            # Counted, never silent. Unlike the timeout branch these ids are DROPPED
            # from staged rather than retried: the source cannot resolve them at all.
            try:
                _LOG.warning(
                    "codex_scan_diagnostic stage=persistent_project "
                    "code=staged_thread_vanished dropped=%d indexed=%d",
                    vanished,
                    indexed,
                )
            except Exception:
                pass
        if locally_owned:
            # Counted, never silent: these threads stay outside the catalog.
            try:
                _LOG.info(
                    "codex_scan_diagnostic stage=persistent_project code=%s excluded=%d",
                    _CODEX_SCAN_LOCAL_OWNER_CODE,
                    locally_owned,
                )
            except Exception:
                pass

        await self._commit_scan_batch(
            provider,
            staged_ids=staged_ids,
            selected_ids=selected_ids,
            previous_progress=progress,
        )
        await self._save_codex_seen_ids(seen_ids | set(inventory_ids))
        await self._record_backfill_successes(
            provider,
            discovery_mode,
            selected_ids,
        )
        return ScanSummary(
            provider=provider,
            discovered=len(staged_ids),
            indexed=indexed,
            rebuilt=0,
            failed=0,
            duration_ms=0,
        )

    async def _load_pending(self, provider: Provider) -> list[str]:
        state = await asyncio.to_thread(
            _call,
            self._store,
            "get_state",
            _PENDING_KEYS[provider],
        )
        return _decode_pending_state(state)

    async def _save_pending(
        self,
        provider: Provider,
        native_ids: list[str],
    ) -> None:
        await asyncio.to_thread(
            _call,
            self._store,
            "set_state",
            _PENDING_KEYS[provider],
            {"version": 1, "native_ids": list(native_ids)},
        )

    async def _load_progress(
        self,
        provider: Provider,
    ) -> dict[str, int | str] | None:
        state = await asyncio.to_thread(
            _call,
            self._store,
            "get_state",
            _PROGRESS_KEYS[provider],
        )
        progress = _decode_progress_state(state)
        if progress is not None:
            self._backfill_progress[provider] = dict(progress)
        return progress

    async def _commit_scan_batch(
        self,
        provider: Provider,
        *,
        staged_ids: list[str],
        selected_ids: list[str],
        previous_progress: dict[str, int | str] | None,
    ) -> None:
        if not selected_ids:
            return
        remaining_ids = staged_ids[len(selected_ids) :]
        indexed_total = (
            int(previous_progress["indexed_total"])
            if previous_progress is not None
            else 0
        ) + len(selected_ids)
        progress: dict[str, int | str] = {
            "version": 1,
            "last_committed_native_id": selected_ids[-1],
            "indexed_total": indexed_total,
            "remaining": len(remaining_ids),
        }
        await self._save_pending(provider, remaining_ids)
        await asyncio.to_thread(
            _call,
            self._store,
            "set_state",
            _PROGRESS_KEYS[provider],
            progress,
        )
        self._backfill_progress[provider] = dict(progress)

    async def _scan_loop(self, provider: Provider) -> None:
        await self._initial_reconcile_done.wait()
        while self._running:
            try:
                await self.scan_once(provider)
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                self._record_error_code("catalog_scan_loop_failed")
            await self._sleep(self._config.service.catalog_scan_seconds)

    async def _watch_loop(self) -> None:
        root = self._claude_projects_root
        stop_event = self._watch_stop_event
        if root is None or stop_event is None:
            return
        await self._initial_reconcile_done.wait()
        pending_scan: asyncio.Task[None] | None = None
        iterator: Any = None
        try:
            factory = self._awatch_factory or _load_default_awatch()
            iterator = factory(root, stop_event=stop_event)
            async for changes in iterator:
                if stop_event.is_set() or not self._running:
                    break
                if not changes:
                    continue
                if pending_scan is not None:
                    pending_scan.cancel()
                    await asyncio.gather(pending_scan, return_exceptions=True)
                pending_scan = asyncio.create_task(self._debounced_claude_scan())
        except asyncio.CancelledError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            self._mark_watcher_failure()
        finally:
            if pending_scan is not None:
                pending_scan.cancel()
                await asyncio.gather(pending_scan, return_exceptions=True)
            closer = getattr(iterator, "aclose", None)
            if callable(closer):
                try:
                    await closer()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if self._running and not stop_event.is_set():
                        self._mark_watcher_failure()

    async def _debounced_claude_scan(self) -> None:
        await self._sleep(self._watch_debounce_seconds)
        stop_event = self._watch_stop_event
        if self._running and (stop_event is None or not stop_event.is_set()):
            await self.scan_once(Provider.CLAUDE)

    async def _reconcile_loop(self) -> None:
        try:
            await asyncio.wait_for(
                self.reconcile_once(),
                timeout=self._refresh_timeout,
            )
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            self._record_error_code("mirror_reconcile_failed")
        finally:
            self._initial_reconcile_done.set()
        while self._running:
            await self._sleep(self._config.service.reconcile_seconds)
            if self._running:
                try:
                    await self.reconcile_once()
                except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                    raise
                except Exception:
                    self._record_error_code("mirror_reconcile_failed")
                try:
                    await self.process_jobs_once()
                except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                    raise
                except Exception:
                    self._record_error_code("mirror_job_processing_failed")

    def _adapter(self, provider: Provider) -> object:
        try:
            return self._adapters[provider]
        except KeyError as exc:
            raise RuntimeError("scan adapter is not configured") from exc

    def _start_provider_call(
        self,
        function: Callable[..., Any],
        *args: object,
        **kwargs: object,
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        self._provider_tasks.add(task)
        task.add_done_callback(self._provider_call_done)
        return task

    async def _provider_call(
        self,
        function: Callable[..., Any],
        *args: object,
        **kwargs: object,
    ) -> Any:
        task = self._start_provider_call(function, *args, **kwargs)
        return await asyncio.shield(task)

    def _provider_call_done(self, task: asyncio.Task[Any]) -> None:
        self._provider_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    def _mark_scan_failure(
        self,
        provider: Provider,
        *,
        stage: str = "unspecified",
        exc: BaseException | None = None,
        summary: object = None,
    ) -> None:
        self._provider_health[provider]["degraded_reason"] = "scan_failed"
        self._record_error_code(f"{provider.value}_scan_failed")
        self._record_scan_diagnostic(provider, stage=stage, exc=exc, summary=summary)

    def _record_scan_diagnostic(
        self,
        provider: Provider,
        *,
        stage: str,
        exc: BaseException | None,
        summary: object,
    ) -> None:
        """Emit a diagnostic for ANY provider scan failure.

        Codex had _record_codex_scan_diagnostic; claude had NO equivalent, so a
        claude scan failure produced `work_state=error, degraded_reason=scan_failed`
        with zero evidence anywhere in the logs -- undiagnosable without attaching a
        debugger. Both `except Exception` call sites also discarded the exception
        object entirely, so even its type was lost.

        Redaction reuses the codex helper (redact_sensitive_text + path scrubbing,
        capped at _CODEX_DIAGNOSTIC_MAX_CHARS); it is provider-agnostic despite the
        name, so transcript content and filesystem paths never reach the log.
        Best-effort throughout: instrumentation must never itself fail a scan.
        """
        try:
            exc_type = type(exc).__name__ if exc is not None else "none"
            detail = _redacted_codex_diagnostic_text(str(exc)) if exc is not None else ""
            frames: tuple[str, ...] = ()
            if exc is not None and exc.__traceback__ is not None:
                raw = traceback.format_tb(exc.__traceback__)[-_CODEX_DIAGNOSTIC_MAX_LINES:]
                frames = tuple(
                    _redacted_codex_diagnostic_text(" ".join(line.split()))
                    for line in raw
                )
            summary_text = (
                _redacted_codex_diagnostic_text(repr(summary))
                if summary is not None
                else ""
            )
            _LOG.warning(
                "provider_scan_diagnostic provider=%s stage=%s code=%s exc=%s "
                "detail=%r summary=%r tb=%r",
                provider.value,
                stage,
                f"{provider.value}_scan_failed",
                exc_type,
                detail,
                summary_text,
                frames,
            )
        except Exception:
            pass

    def _mark_scan_success(self, provider: Provider) -> None:
        self._mark_provider_success(provider)

    def _mark_refresh_failure(self, provider: Provider) -> None:
        self._provider_health[provider]["degraded_reason"] = "refresh_failed"
        self._record_error_code(f"{provider.value}_refresh_failed")

    def _mark_provider_success(self, provider: Provider) -> None:
        self._provider_health[provider]["last_success"] = float(self._clock())
        self._provider_health[provider]["degraded_reason"] = None

    def _mark_watcher_failure(self) -> None:
        self._watcher_state = "degraded"
        self._watcher_error_code = "claude_watcher_failed"
        self._record_error_code("claude_watcher_failed")

    def _record_error_code(self, code: str) -> None:
        self._recent_error_codes.append(code)
        del self._recent_error_codes[:-_RECENT_ERROR_LIMIT]

    def _record_codex_scan_diagnostic(
        self,
        *,
        stage: str,
        native_id: object,
        exc: BaseException,
        adapter: object,
    ) -> None:
        safe_stage = stage if stage in _CODEX_SCAN_STAGES else "diagnostic_unavailable"
        try:
            native_tag = redact_codex_thread_id(native_id) or "unknown"
        except Exception:
            native_tag = "unknown"
        try:
            exception_detail = _redacted_codex_diagnostic_text(str(exc))
        except Exception:
            exception_detail = "diagnostic_unavailable"
        try:
            stderr = _redacted_codex_stderr_tail(adapter)
        except Exception:
            stderr = ()
        try:
            _LOG.warning(
                "codex_scan_diagnostic stage=%s code=%s native=%s detail=%r stderr=%r stderr_lines=%d",
                safe_stage,
                _CODEX_SCAN_FAILURE_CODE,
                native_tag,
                exception_detail,
                stderr,
                len(stderr),
            )
        except Exception:
            pass
        self._record_error_code(_CODEX_SCAN_FAILURE_CODE)

    def _elapsed_ms(self, started: float) -> float:
        return max(0.0, (float(self._monotonic()) - started) * 1000.0)


def _redacted_codex_diagnostic_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    redacted = redact_sensitive_text(value, force=True, redact_url_credentials=True)
    redacted = _WINDOWS_TERMINAL_PATH_RE.sub("[REDACTED_PATH]", redacted)
    redacted = _POSIX_TERMINAL_PATH_RE.sub("[REDACTED_PATH]", redacted)
    redacted = _PATH_FRAGMENT_RE.sub("[REDACTED_PATH]", redacted)
    return redacted[:_CODEX_DIAGNOSTIC_MAX_CHARS]


def _redacted_codex_stderr_tail(adapter: object) -> tuple[str, ...]:
    try:
        getter = getattr(adapter, "stderr_tail", None)
        raw = getter(_CODEX_STDERR_TAIL_LINES) if callable(getter) else ()
    except Exception:
        return ()
    if not isinstance(raw, (list, tuple)):
        return ()
    safe: list[str] = []
    for line in raw[:_CODEX_STDERR_TAIL_LINES]:
        if not isinstance(line, str):
            continue
        redacted = _redacted_codex_diagnostic_text(line.strip())
        if redacted:
            safe.append(redacted)
        if len(safe) == _CODEX_DIAGNOSTIC_MAX_LINES:
            break
    return tuple(safe)


def _zero_scan(provider: Provider | None) -> ScanSummary:
    return ScanSummary(
        provider=provider,
        discovered=0,
        indexed=0,
        rebuilt=0,
        failed=0,
        duration_ms=0,
    )


def _call(instance: object, name: str, *args: object, **kwargs: object) -> Any:
    method = getattr(instance, name, None)
    if not callable(method):
        raise RuntimeError("scan adapter does not implement the required operation")
    return method(*args, **kwargs)


def _cached_index_kwargs(adapter: object) -> dict[str, bool]:
    """Opt scan resolution into the adapter's TTL'd inventory index, if it has one.

    Resolving a staged id that the batch inventory missed used to page the whole
    Codex inventory per id, which parked a backfill for 20+ minutes (see
    codex_adapter.find_native_thread). The index collapses that to one fetch per
    TTL, at the cost of a bounded-stale summary -- acceptable for scan indexing,
    NOT for refresh or characterization, which is why the adapter defaults it off
    and only this call site turns it on.

    Adapter dispatch is duck-typed, so detect the capability instead of assuming
    it: a double implementing the narrower signature must not be handed a keyword
    it cannot accept.
    """

    method = getattr(adapter, "find_native_thread", None)
    if not callable(method):
        return {}
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return {}
    if "allow_cached_index" in parameters:
        return {"allow_cached_index": True}
    return {}


def _filesystem_permission_preflight(cwd: str) -> bool:
    """Read-only filesystem sanity check; external authorization stays injected."""

    try:
        path = Path(cwd)
        resolved = path.resolve(strict=True)
        if not resolved.is_dir():
            return False
        for parent in (resolved, *resolved.parents):
            if not os.access(parent, os.R_OK | os.X_OK):
                return False
        with os.scandir(resolved) as entries:
            next(entries, None)
    except OSError:
        return False
    return True


def _exact_sidebar_claim_text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"sidebar claim {label} is malformed")
    if any(character in value for character in "\r\n"):
        raise ValueError(f"sidebar claim {label} is malformed")
    return value


def _exact_sidebar_proof_digest(value: object) -> str:
    digest = _exact_sidebar_claim_text(value, "reconciliation proof digest")
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError("sidebar claim reconciliation proof digest is malformed")
    return digest


def _sidebar_reconciliation_proof_ttl_seconds(value: object) -> float:
    interval = _finite_number(value, "sidebar reconciliation interval")
    if interval < 0:
        raise ValueError("sidebar reconciliation interval must be non-negative")
    return 30.0 if interval == 0 else min(30.0, interval)


def _sidebar_reconciliation_failure_code(code: object) -> str:
    if code in {
        "marker_conflict",
        "source_identity_mismatch",
        "provider_mismatch",
        "codex_thread_conflict",
        "placement_mismatch",
    }:
        return cast(str, code)
    return "bridge_temporarily_unavailable"


def _raise_detached_cancelled(cancelled: asyncio.CancelledError) -> NoReturn:
    """Raise the original cancellation with no reachable sensitive context."""

    cancelled.__cause__ = None
    cancelled.__context__ = None
    cancelled.__suppress_context__ = True
    raise cancelled from None


def _sidebar_claim_tokens(
    raw_claims: object,
    *,
    limit: int,
) -> tuple[list[str], bool]:
    """Extract every recoverable lease token before validating the batch."""

    if not isinstance(raw_claims, (list, tuple)):
        return [], True
    malformed = not isinstance(raw_claims, list) or len(raw_claims) > limit
    owned_tokens: list[str] = []
    for raw_claim in raw_claims:
        if not isinstance(raw_claim, Mapping):
            malformed = True
            continue
        claim = cast(Mapping[str, object], raw_claim)
        try:
            owned_tokens.append(
                _exact_sidebar_claim_text(claim.get("lease_token"), "lease token")
            )
        except Exception:
            malformed = True
    if len(set(owned_tokens)) != len(owned_tokens):
        malformed = True
    return owned_tokens, malformed


def _safe_target_error_code(provider: Provider, code: object) -> str:
    if (
        isinstance(code, str)
        and 0 < len(code) <= 64
        and "a" <= code[0] <= "z"
        and all(
            character.islower() or character.isdigit() or character == "_"
            for character in code
        )
    ):
        return code
    return f"{provider.value}_target_failed"


def _healthy_breaker_batch_completed(
    progress: BatchProgress,
    policy: MirrorPolicy,
) -> bool:
    return progress.attempts >= policy.stop_after_attempts and (
        progress.errors == 0
        or progress.errors / progress.attempts < policy.stop_error_rate
    )


def _projection_from_parse(parsed: object) -> SessionProjection:
    projection = getattr(parsed, "projection", None)
    if not isinstance(projection, SessionProjection):
        raise RuntimeError("Claude parser returned no session projection")
    return projection


def _upsert(
    store: object,
    projection: object,
    rebuild: bool,
) -> UpsertResult:
    if not isinstance(projection, SessionProjection):
        raise RuntimeError("scan adapter returned no session projection")
    result = _call(store, "upsert_projection", projection, rebuild=rebuild)
    if not isinstance(result, UpsertResult):
        raise RuntimeError("session store returned no upsert result")
    return result


def _supports_scan_state(store: object) -> bool:
    return callable(getattr(store, "get_state", None)) and callable(
        getattr(store, "set_state", None)
    )


def _load_default_awatch() -> _AWatchFactory:
    from watchfiles import awatch

    return awatch


class _ClaudeStatCache:
    """Stat the hot transcripts every cycle and rotate through the cold ones.

    ``_sort_claude_paths`` statted the WHOLE corpus on every scan cycle: 3,795
    transcripts here, ~950 ms of syscalls, at the ``catalog_scan_seconds``
    cadence of 3 s. py-spy measured that at 17.8% of process CPU on the live
    :7484 worker (2026-08-19), second only to the sidebar candidate query.

    The corpus is heavily skewed -- 3,140 of those 3,795 transcripts (83%) had
    not been written in over a week. Re-statting them twenty times a minute
    buys nothing.

    A directory-mtime signature is NOT available as a shortcut here. See
    ``ClaudeSourceAdapter.discover``, which rejected one because transcripts
    live at BOTH depth 1 and depth 3 under the projects root, so creating a
    file in a nested subdirectory leaves the immediate project directory's
    mtime untouched and the signature silently misses new sessions. This cache
    takes the same escape ``discover``'s TTL takes: it can only ever DELAY a
    change, never miss one.

    Three rules make that guarantee hold:

    * a path never seen before is ALWAYS statted, so new transcripts are never
      deferred;
    * a path written within ``hot_window_seconds`` of the NEWEST transcript is
      statted every cycle, so the sessions that actually move stay at full
      cadence -- the window is anchored to the corpus rather than to the wall
      clock, so a skewed clock or a wholly archival corpus cannot class the
      session being written right now as cold;
    * every remaining path is statted on a rotation that covers the whole cold
      set within ``sweep_seconds``, resuming from the last key swept rather
      than a positional index, so a churning corpus cannot starve any entry.

    A cold transcript that unexpectedly gains a write is therefore picked up at
    its next rotation slot -- late by at most ``sweep_seconds``, never skipped.
    The rotation slice is sized from the wall time actually elapsed between
    calls, not from an assumed cadence, so the sweep window holds whatever
    ``catalog_scan_seconds`` is configured to.
    """

    def __init__(
        self,
        *,
        hot_window_seconds: float = _CLAUDE_HOT_STAT_WINDOW_SECONDS,
        sweep_seconds: float = _CLAUDE_COLD_STAT_SWEEP_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(hot_window_seconds) or hot_window_seconds < 0:
            raise ValueError("Claude stat hot window must be non-negative")
        if not math.isfinite(sweep_seconds) or sweep_seconds <= 0:
            raise ValueError("Claude stat sweep window must be positive")
        self._hot_window = float(hot_window_seconds)
        self._sweep = float(sweep_seconds)
        self._monotonic = monotonic
        # key -> (mtime_ns, size, mtime_seconds)
        self._entries: dict[str, tuple[int, int, float]] = {}
        self._cold_cursor: str | None = None
        self._pass_size = 0
        self._swept_at: float | None = None

    def stat_paths(
        self,
        paths: Sequence[Path],
    ) -> tuple[dict[str, tuple[int, int]], list[Path]]:
        """Return ``key -> (mtime_ns, size)`` plus the paths that went away."""

        keys = [str(path) for path in paths]
        entries = self._entries
        live = set(keys)
        for gone in [key for key in entries if key not in live]:
            del entries[gone]

        now = self._monotonic()
        elapsed = self._sweep if self._swept_at is None else max(0.0, now - self._swept_at)
        self._swept_at = now
        fraction = 1.0 if elapsed >= self._sweep else elapsed / self._sweep

        # "Recent" is measured against the NEWEST transcript we know of, not
        # against the wall clock. Anchoring to the corpus keeps the split
        # meaningful when the two disagree: a clock that is skewed, or a corpus
        # that is entirely archival, would otherwise class every transcript
        # cold and defer the very session being written. In production the
        # newest transcript IS approximately now, so the split is unchanged.
        newest = max((entry[2] for entry in entries.values()), default=0.0)
        selected: set[str] = set()
        cold_keys: list[str] = []
        for key in keys:
            entry = entries.get(key)
            if entry is None or (newest - entry[2]) <= self._hot_window:
                selected.add(key)
            else:
                cold_keys.append(key)

        if cold_keys:
            cold_keys.sort()
            if self._cold_cursor is None:
                self._pass_size = len(cold_keys)
            # Size the slice against the set as it stood when this pass STARTED.
            # Sizing it against the CURRENT set decelerates as swept entries
            # turn hot and drop out of it, which silently stretches the sweep
            # far past its bound -- 100 changed transcripts took ~46 cycles to
            # cover under a 20-cycle window before this was pinned by
            # test_a_cold_transcript_change_is_delayed_but_never_missed.
            budget = max(self._pass_size, len(cold_keys))
            take = (
                len(cold_keys)
                if fraction >= 1.0
                else min(len(cold_keys), math.ceil(budget * fraction))
            )
            if take:
                start = (
                    0
                    if self._cold_cursor is None
                    else bisect.bisect_right(cold_keys, self._cold_cursor)
                )
                if start >= len(cold_keys):
                    start = 0
                    self._pass_size = len(cold_keys)
                stop = min(start + take, len(cold_keys))
                for index in range(start, stop):
                    selected.add(cold_keys[index])
                # Ending the pass explicitly (rather than wrapping modulo) is
                # what lets the next one re-measure its budget.
                self._cold_cursor = (
                    None if stop >= len(cold_keys) else cold_keys[stop - 1]
                )

        stats: dict[str, tuple[int, int]] = {}
        unavailable: list[Path] = []
        for path, key in zip(paths, keys):
            entry = entries.get(key)
            if key in selected:
                try:
                    stat = path.stat()
                except OSError:
                    entries.pop(key, None)
                    unavailable.append(path)
                    continue
                entry = (
                    int(stat.st_mtime_ns),
                    int(stat.st_size),
                    float(stat.st_mtime),
                )
                entries[key] = entry
            if entry is None:
                unavailable.append(path)
                continue
            stats[key] = (entry[0], entry[1])
        return stats, unavailable


def _stat_claude_paths(
    paths: Sequence[Path],
) -> tuple[dict[str, tuple[int, int]], list[Path]]:
    """Stat every path, with no caching -- used by the full-history rebuild."""

    stats: dict[str, tuple[int, int]] = {}
    unavailable: list[Path] = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            unavailable.append(path)
            continue
        stats[str(path)] = (int(stat.st_mtime_ns), int(stat.st_size))
    return stats, unavailable


def _sort_claude_paths(
    paths: object,
    cache: _ClaudeStatCache | None = None,
) -> tuple[list[Path], list[Path], dict[str, dict[str, int]]]:
    """Order transcripts newest-first, and hand back their fingerprints.

    The fingerprint comes from the SAME ``stat_result`` used to sort, because
    the caller needs both and a second stat buys nothing. This pass runs in a
    worker thread, so folding the fingerprints in here also takes them off the
    event loop -- ``_claude_path_fingerprint`` used to be called inline while
    scanning, statting the whole corpus synchronously under uvicorn.

    With no ``cache`` this stats every path, which is what the full-history
    rebuild wants. The periodic scan passes a ``_ClaudeStatCache`` so the cold
    83% of the corpus is statted on a rotation instead of on every cycle.
    """
    try:
        normalized = [Path(path) for path in paths]  # type: ignore[union-attr]
    except TypeError as exc:
        raise RuntimeError("Claude discovery returned no path list") from exc
    if cache is None:
        stats, unavailable = _stat_claude_paths(normalized)
    else:
        stats, unavailable = cache.stat_paths(normalized)
    sortable: list[tuple[int, str, str, Path]] = []
    fingerprints: dict[str, dict[str, int]] = {}
    for path in normalized:
        entry = stats.get(str(path))
        if entry is None:
            continue
        mtime_ns, size = entry
        sortable.append((-mtime_ns, path.stem, str(path), path))
        fingerprints.setdefault(path.stem, {"mtime_ns": mtime_ns, "size": size})
    sortable.sort()
    return [entry[3] for entry in sortable], unavailable, fingerprints


def _claude_path_fingerprint(path: Path) -> dict[str, int]:
    stat = Path(path).stat()
    return {"mtime_ns": int(stat.st_mtime_ns), "size": int(stat.st_size)}


def _parse_accepts_cursor(adapter: object) -> bool:
    """True when ``adapter.parse`` takes a previous-cursor argument.

    Mirrors how the scan already probes ``find_native_sessions_by_stem``: the
    cursor is an optional adapter capability, and an adapter without it simply
    gets full reads instead of a TypeError counted as a scan failure.
    """

    parse = getattr(adapter, "parse", None)
    if not callable(parse):
        return False
    try:
        signature = inspect.signature(parse)
    except (TypeError, ValueError):
        return False
    for index, parameter in enumerate(signature.parameters.values()):
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            return True
        if index == 1 and parameter.kind in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }:
            return True
    return False


def _remember_claude_cursor(
    cursors: dict[str, ClaudeCursor],
    key: str,
    parsed: object,
) -> None:
    cursor = getattr(parsed, "cursor", None)
    if encode_claude_cursor(cursor) is None:
        # A parser that hands back no usable cursor gets a full read next
        # cycle, which is exactly the behaviour before cursors existed.
        cursors.pop(key, None)
        return
    cursors[key] = cast("ClaudeCursor", cursor)


def _decode_claude_cursors(state: object) -> dict[str, ClaudeCursor]:
    """Decode the persisted cursor map.

    The envelope is validated as strictly as the fingerprint state it rides
    beside -- garbage there means something else is writing our key. Individual
    entries are only DROPPED when they fail to decode: a cursor is a pure read
    optimisation, and losing one costs a single full read that then rewrites it.
    """

    if state is None:
        return {}
    if not isinstance(state, Mapping):
        raise RuntimeError("invalid Claude cursor state")
    typed_state = cast("Mapping[str, Any]", state)
    if typed_state.get("version") != 1:
        raise RuntimeError("invalid Claude cursor state")
    sessions = typed_state.get("sessions")
    if not isinstance(sessions, Mapping):
        raise RuntimeError("invalid Claude cursor state")
    decoded: dict[str, ClaudeCursor] = {}
    for native_id, raw_cursor in sessions.items():
        if not isinstance(native_id, str) or not native_id.strip():
            raise RuntimeError("invalid Claude cursor state")
        cursor = decode_claude_cursor(raw_cursor)
        if cursor is not None:
            decoded[native_id.strip()] = cursor
    return decoded


def _decode_claude_fingerprints(
    state: object,
) -> dict[str, dict[str, int]]:
    if state is None:
        return {}
    if not isinstance(state, Mapping):
        raise RuntimeError("invalid Claude fingerprint state")
    typed_state = cast("Mapping[str, Any]", state)
    if typed_state.get("version") != 1:
        raise RuntimeError("invalid Claude fingerprint state")
    sessions = typed_state.get("sessions")
    if not isinstance(sessions, Mapping):
        raise RuntimeError("invalid Claude fingerprint state")
    decoded: dict[str, dict[str, int]] = {}
    for native_id, raw_fingerprint in sessions.items():
        if not isinstance(native_id, str) or not native_id.strip():
            raise RuntimeError("invalid Claude fingerprint state")
        if not isinstance(raw_fingerprint, Mapping):
            raise RuntimeError("invalid Claude fingerprint state")
        mtime_ns = raw_fingerprint.get("mtime_ns")
        size = raw_fingerprint.get("size")
        if (
            set(raw_fingerprint) != {"mtime_ns", "size"}
            or not isinstance(mtime_ns, int)
            or isinstance(mtime_ns, bool)
            or mtime_ns < 0
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise RuntimeError("invalid Claude fingerprint state")
        decoded[native_id.strip()] = {"mtime_ns": mtime_ns, "size": size}
    return decoded


def _decode_creation_rate_state(state: object, *, now: float) -> list[float]:
    if state is None:
        return []
    if not isinstance(state, Mapping):
        raise RuntimeError("invalid mirror creation rate state")
    typed_state = cast("Mapping[str, Any]", state)
    if set(typed_state) != {"version", "attempted_at"}:
        raise RuntimeError("invalid mirror creation rate state")
    if typed_state.get("version") != 1:
        raise RuntimeError("invalid mirror creation rate state")
    attempted_at = typed_state.get("attempted_at")
    if not isinstance(attempted_at, list):
        raise RuntimeError("invalid mirror creation rate state")
    recent: list[float] = []
    for raw_timestamp in attempted_at:
        timestamp = _finite_number(raw_timestamp, "mirror creation timestamp")
        if timestamp > now:
            raise RuntimeError("invalid mirror creation rate state")
        if timestamp > now - 60.0:
            recent.append(timestamp)
    return recent


def _decode_breaker_state(state: object) -> BatchProgress:
    if state is None:
        return BatchProgress()
    if not isinstance(state, Mapping):
        raise RuntimeError("invalid mirror breaker state")
    typed_state = cast("Mapping[str, Any]", state)
    if set(typed_state) != {
        "version",
        "attempts",
        "errors",
    }:
        raise RuntimeError("invalid mirror breaker state")
    if typed_state.get("version") != 1:
        raise RuntimeError("invalid mirror breaker state")
    attempts = typed_state.get("attempts")
    errors = typed_state.get("errors")
    if (
        not isinstance(attempts, int)
        or isinstance(attempts, bool)
        or not isinstance(errors, int)
        or isinstance(errors, bool)
    ):
        raise RuntimeError("invalid mirror breaker state")
    return BatchProgress(attempts=attempts, errors=errors)


def _sort_codex_summaries(summaries: object) -> list[object]:
    try:
        normalized = list(summaries)  # type: ignore[arg-type]
    except TypeError as exc:
        raise RuntimeError("Codex inventory returned no summary list") from exc
    return sorted(
        normalized,
        key=lambda summary: (
            -_codex_last_active(summary),
            _codex_native_id(summary),
        ),
    )


def _codex_inventory(
    adapter: object,
    *,
    include_archived: bool,
) -> list[object]:
    passes = [False, True] if include_archived else [False]
    merged: dict[str, object] = {}
    for archived in passes:
        summaries = _call(adapter, "list_inventory", archived=archived)
        try:
            batch = list(summaries)
        except TypeError as exc:
            raise RuntimeError("Codex inventory returned no summary list") from exc
        for summary in batch:
            native_id = _codex_native_id(summary)
            merged.setdefault(native_id, summary)
    return list(merged.values())


def _codex_recent_inventory(
    adapter: object,
    *,
    include_archived: bool,
    after: float,
    known_native_ids: frozenset[str],
) -> list[object]:
    passes = [False, True] if include_archived else [False]
    merged: dict[str, object] = {}
    for archived in passes:
        summaries = _call(
            adapter,
            "list_recent_inventory",
            archived=archived,
            after=after,
            known_native_ids=known_native_ids,
        )
        try:
            batch = list(summaries)
        except TypeError as exc:
            raise RuntimeError(
                "Codex recent inventory returned no summary list"
            ) from exc
        for summary in batch:
            native_id = _codex_native_id(summary)
            merged.setdefault(native_id, summary)
    return list(merged.values())


def _codex_full_inventory(
    adapter: object,
    *,
    include_archived: bool,
) -> list[object]:
    passes = [False, True] if include_archived else [False]
    merged: dict[str, object] = {}
    for archived in passes:
        summaries = _call(adapter, "list_full_inventory", archived=archived)
        try:
            batch = list(summaries)
        except TypeError as exc:
            raise RuntimeError("Codex full inventory returned no summary list") from exc
        for summary in batch:
            native_id = _codex_native_id(summary)
            merged.setdefault(native_id, summary)
    return list(merged.values())


def _codex_native_id(summary: object) -> str:
    native_id = getattr(summary, "native_id", None)
    if not isinstance(native_id, str) or not native_id.strip():
        raise RuntimeError("Codex inventory summary has no native identity")
    return native_id.strip()


def _codex_last_active(summary: object) -> float:
    value = getattr(summary, "last_active", None)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise RuntimeError("Codex inventory summary has no activity timestamp")
    return float(value)


def _merge_native_ids(*groups: Sequence[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for native_id in group:
            if native_id in seen:
                continue
            seen.add(native_id)
            merged.append(native_id)
    return merged


def _decode_pending_state(state: object) -> list[str]:
    if state is None:
        return []
    if not isinstance(state, dict):
        raise RuntimeError("invalid pending scan state")
    typed_state = cast("dict[str, Any]", state)
    if typed_state.get("version") != 1:
        raise RuntimeError("invalid pending scan state")
    native_ids = typed_state.get("native_ids")
    if not isinstance(native_ids, list):
        raise RuntimeError("invalid pending scan state")
    normalized: list[str] = []
    seen: set[str] = set()
    for native_id in native_ids:
        if not isinstance(native_id, str) or not native_id.strip():
            raise RuntimeError("invalid pending scan state")
        candidate = native_id.strip()
        if candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return normalized


def _decode_native_id_set_state(state: object, *, label: str) -> list[str]:
    if state is None:
        return []
    if not isinstance(state, Mapping):
        raise RuntimeError(f"invalid {label} state")
    typed_state = cast("dict[str, Any]", dict(state))
    if set(typed_state) != {"version", "native_ids"} or typed_state.get("version") != 1:
        raise RuntimeError(f"invalid {label} state")
    native_ids = typed_state.get("native_ids")
    if not isinstance(native_ids, list):
        raise RuntimeError(f"invalid {label} state")
    normalized: list[str] = []
    for native_id in native_ids:
        if (
            not isinstance(native_id, str)
            or not native_id.strip()
            or native_id != native_id.strip()
            or native_id in normalized
        ):
            raise RuntimeError(f"invalid {label} state")
        normalized.append(native_id)
    return normalized


def _decode_backfill_state(state: object) -> tuple[bool, set[str]]:
    if state is None:
        return False, set()
    if not isinstance(state, Mapping):
        raise RuntimeError("invalid backfill state")
    typed_state = cast("dict[str, Any]", dict(state))
    if set(typed_state) != {"version", "completed", "processed_native_ids"}:
        raise RuntimeError("invalid backfill state")
    if (
        typed_state.get("version") != 1
        or type(typed_state.get("completed")) is not bool
    ):
        raise RuntimeError("invalid backfill state")
    processed = _decode_native_id_set_state(
        {"version": 1, "native_ids": typed_state.get("processed_native_ids")},
        label="backfill processed IDs",
    )
    return bool(typed_state["completed"]), set(processed)


def _decode_continuation_reconcile_cursor(state: object) -> str | None:
    if state is None:
        return None
    if not isinstance(state, Mapping):
        raise RuntimeError("invalid continuation reconcile cursor")
    typed_state = cast("dict[str, Any]", dict(state))
    if (
        set(typed_state) != {"version", "after_bridge_id"}
        or typed_state.get("version") != 1
    ):
        raise RuntimeError("invalid continuation reconcile cursor")
    after_bridge_id = typed_state.get("after_bridge_id")
    if after_bridge_id is None:
        return None
    if (
        not isinstance(after_bridge_id, str)
        or not after_bridge_id.strip()
        or after_bridge_id != after_bridge_id.strip()
    ):
        raise RuntimeError("invalid continuation reconcile cursor")
    return after_bridge_id


def _decode_progress_state(state: object) -> dict[str, int | str] | None:
    if state is None:
        return None
    if not isinstance(state, dict):
        raise RuntimeError("invalid scan progress state")
    typed_state = cast("dict[str, Any]", state)
    if typed_state.get("version") != 1:
        raise RuntimeError("invalid scan progress state")
    native_id = typed_state.get("last_committed_native_id")
    indexed_total = typed_state.get("indexed_total")
    remaining = typed_state.get("remaining")
    if not isinstance(native_id, str) or not native_id.strip():
        raise RuntimeError("invalid scan progress state")
    if (
        not isinstance(indexed_total, int)
        or isinstance(indexed_total, bool)
        or indexed_total < 0
    ):
        raise RuntimeError("invalid scan progress state")
    if not isinstance(remaining, int) or isinstance(remaining, bool) or remaining < 0:
        raise RuntimeError("invalid scan progress state")
    return {
        "version": 1,
        "last_committed_native_id": native_id.strip(),
        "indexed_total": indexed_total,
        "remaining": remaining,
    }


def _read_exact_projection(
    adapter: object,
    provider: Provider,
    native_id: str,
) -> SessionProjection:
    if provider is Provider.CLAUDE:
        path = _call(adapter, "find_native_session", native_id)
        if path is None:
            raise RuntimeError("Claude session is unavailable")
        projection = _projection_from_parse(_call(adapter, "parse", path))
    elif provider is Provider.CODEX:
        exact_reader = getattr(adapter, "read_native_thread", None)
        if callable(exact_reader):
            projection = exact_reader(native_id)
        else:
            summary = _call(adapter, "find_native_thread", native_id)
            if summary is None:
                raise RuntimeError("Codex thread is unavailable")
            projection = _call(adapter, "project_thread", summary)
        if not isinstance(projection, SessionProjection):
            raise RuntimeError("Codex projector returned no session projection")
    else:
        raise RuntimeError("refresh provider is unsupported")
    return projection


def _stale_refresh(
    session_id: str,
    durable: Mapping[str, Any],
) -> RefreshResult:
    cursor = durable.get("last_native_cursor")
    source_hash = durable.get("last_native_hash")
    if (
        not isinstance(cursor, str)
        or not cursor.strip()
        or not isinstance(source_hash, str)
        or not source_hash.strip()
    ):
        raise RuntimeError("durable snapshot unavailable")
    return RefreshResult(
        session_id=session_id,
        cursor=cursor.strip(),
        source_hash=source_hash.strip(),
        stale=True,
        warning="source_refresh_failed_using_durable_snapshot",
    )


def _validate_continue_request(request: ContinueRequest) -> None:
    if not isinstance(request, ContinueRequest):
        raise TypeError("request must be a ContinueRequest")
    for label, value in (
        ("session ID", request.session_id),
        ("bridge ID", request.bridge_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must not be empty")
    if request.target_provider not in _EXTERNAL_PROVIDERS:
        raise ValueError("continuation target must be Claude or Codex")
    if (
        not isinstance(request.context_budget_chars, int)
        or isinstance(request.context_budget_chars, bool)
        or not 0 < request.context_budget_chars <= 100_000
    ):
        raise ValueError("context budget must be between 1 and 100000 characters")


def _external_row_session_id(
    row: Mapping[str, Any],
    provider: Provider,
) -> str:
    session_id = row.get("session_id")
    native_id = row.get("native_id")
    if (
        row.get("provider") != provider.value
        or not isinstance(session_id, str)
        or not isinstance(native_id, str)
        or session_id != canonical_session_id(provider, native_id)
    ):
        raise RuntimeError("continuation target identity is invalid")
    return session_id


def _provider_from_session_id(session_id: str) -> Provider:
    prefix, separator, native_id = session_id.partition(":")
    if not separator or not native_id:
        raise RuntimeError("external session identity is invalid")
    try:
        provider = Provider(prefix)
    except ValueError as exc:
        raise RuntimeError("external session identity is invalid") from exc
    if provider not in _EXTERNAL_PROVIDERS:
        raise RuntimeError("external session identity is invalid")
    if canonical_session_id(provider, native_id) != session_id:
        raise RuntimeError("external session identity is invalid")
    return provider


def _validated_continuation_snapshot(raw_snapshot: object) -> dict[str, Any]:
    if not isinstance(raw_snapshot, Mapping):
        raise RuntimeError("continuation snapshot is invalid")
    snapshot = dict(raw_snapshot)
    if snapshot.get("version") != 1:
        raise RuntimeError("continuation snapshot is invalid")
    for key in (
        "pack_id",
        "source_session_id",
        "source_cursor",
        "source_hash",
        "target_session_id",
        "target_cursor",
        "target_hash",
    ):
        value = snapshot.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError("continuation snapshot is invalid")
        snapshot[key] = value.strip()
    return snapshot


def _validated_periodic_continuation_snapshot(
    raw_snapshot: object,
) -> dict[str, Any]:
    snapshot = _validated_continuation_snapshot(raw_snapshot)
    assert isinstance(raw_snapshot, Mapping)
    typed_snapshot = cast("Mapping[str, Any]", raw_snapshot)
    bridge_id = typed_snapshot.get("bridge_id")
    if not isinstance(bridge_id, str) or not bridge_id.strip():
        raise RuntimeError("continuation snapshot is invalid")
    snapshot["bridge_id"] = bridge_id.strip()
    return snapshot


def _context_pack_from_row(raw_row: object) -> ContextPack:
    if not isinstance(raw_row, Mapping):
        raise RuntimeError("context pack record is invalid")
    row = dict(raw_row)
    required = {
        key: _required_mapping_text(row, key)
        for key in (
            "id",
            "bridge_id",
            "source_session_id",
            "source_cursor",
            "source_hash",
            "payload",
        )
    }
    target_session_id = row.get("target_session_id")
    if target_session_id is not None and (
        not isinstance(target_session_id, str) or not target_session_id.strip()
    ):
        raise RuntimeError("context pack record is invalid")
    budget_chars = row.get("budget_chars")
    if (
        not isinstance(budget_chars, int)
        or isinstance(budget_chars, bool)
        or budget_chars <= 0
    ):
        raise RuntimeError("context pack record is invalid")
    created_at = _mapping_number(row, "created_at")
    immutable_at_raw = row.get("immutable_at")
    immutable_at = (
        None
        if immutable_at_raw is None
        else _finite_number(immutable_at_raw, "immutable_at")
    )
    return ContextPack(
        id=required["id"],
        bridge_id=required["bridge_id"],
        source_session_id=required["source_session_id"],
        target_session_id=(
            target_session_id.strip() if isinstance(target_session_id, str) else None
        ),
        source_cursor=required["source_cursor"],
        source_hash=required["source_hash"],
        budget_chars=budget_chars,
        payload=required["payload"],
        created_at=created_at,
        immutable_at=immutable_at,
    )


def _session_link_from_row(raw_row: object) -> SessionLink:
    if not isinstance(raw_row, Mapping):
        raise RuntimeError("session link record is invalid")
    row = dict(raw_row)
    try:
        relation = Relation(row.get("relation"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("session link record is invalid") from exc
    source_cursor = row.get("source_cursor")
    source_hash = row.get("source_hash")
    if source_cursor is not None and not isinstance(source_cursor, str):
        raise RuntimeError("session link record is invalid")
    if source_hash is not None and not isinstance(source_hash, str):
        raise RuntimeError("session link record is invalid")
    return SessionLink(
        id=_required_mapping_text(row, "id"),
        from_session_id=_required_mapping_text(row, "from_session_id"),
        to_session_id=_required_mapping_text(row, "to_session_id"),
        relation=relation,
        bridge_id=_required_mapping_text(row, "bridge_id"),
        source_cursor=source_cursor,
        source_hash=source_hash,
        created_at=_mapping_number(row, "created_at"),
    )


def _first_sidebar_request(projection: SessionProjection) -> str:
    for message in projection.messages:
        if message.role != "user" or not isinstance(message.content, str):
            continue
        single_message_projection = replace(projection, messages=(message,))
        if is_sidebar_session_eligible(
            single_message_projection,
            now=projection.last_active,
            backfill_days=0,
        ):
            return message.content
    raise ValueError("eligible sidebar source has no meaningful user request")


def _validated_sidebar_page_cursor(value: object) -> tuple[float, str]:
    try:
        if not isinstance(value, tuple) or len(value) != 2:
            raise ValueError
        activity = _finite_number(value[0], "sidebar candidate cursor activity")
        session_id = value[1]
        if (
            not isinstance(session_id, str)
            or not session_id
            or session_id != session_id.strip()
        ):
            raise ValueError
        sidebar_bridge_id(session_id)
        return activity, session_id
    except (RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("sidebar candidate cursor is malformed") from exc


def _sidebar_cursor_advances(
    previous: tuple[float, str],
    current: tuple[float, str],
) -> bool:
    return current[0] < previous[0] or (
        current[0] == previous[0] and current[1] > previous[1]
    )


def _required_mapping_text(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("durable bridge record is invalid")
    return value.strip()


def _mapping_number(row: Mapping[str, Any], key: str) -> float:
    return _finite_number(row.get(key), key)


def _finite_number(value: object, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise RuntimeError(f"{label} is invalid")
    return float(value)


def _validated_process_job_ids(
    job_ids: Sequence[str] | None,
) -> tuple[str, ...] | None:
    if job_ids is None:
        return None
    if isinstance(job_ids, (str, bytes)) or not isinstance(job_ids, Sequence):
        raise TypeError("job_ids must be a sequence")
    if len(job_ids) > 1000:
        raise ValueError("job_ids must contain at most 1000 IDs")
    normalized: list[str] = []
    for job_id in job_ids:
        if not isinstance(job_id, str) or not job_id or job_id != job_id.strip():
            raise ValueError("job_ids must contain exact nonempty IDs")
        normalized.append(job_id)
    if len(set(normalized)) != len(normalized):
        raise ValueError("job_ids must not contain duplicates")
    return tuple(normalized)


def _validated_job(raw_job: object) -> dict[str, Any]:
    if not isinstance(raw_job, Mapping):
        raise RuntimeError("mirror job is invalid")
    job = dict(raw_job)
    for key in ("id", "idempotency_key", "source_session_id"):
        value = job.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError("mirror job is invalid")
        job[key] = value.strip()
    try:
        provider = Provider(job.get("target_provider"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("mirror job target provider is invalid") from exc
    if provider not in _EXTERNAL_PROVIDERS:
        raise RuntimeError("mirror job target provider is invalid")
    job["target_provider"] = provider.value
    attempts = job.get("attempts")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts <= 0:
        raise RuntimeError("mirror job attempts are invalid")
    rollout_limited = job.get("rollout_limited", False)
    if type(rollout_limited) is not bool:
        raise RuntimeError("mirror job rollout authority is invalid")
    job["rollout_limited"] = rollout_limited
    return job


def _attempt_key(job: Mapping[str, Any]) -> str:
    return f"{_ATTEMPT_KEY_PREFIX}{job['id']}"


def _bridge_id(job: Mapping[str, Any]) -> str:
    key = str(job["idempotency_key"])
    digest = hashlib.sha256(f"session-bridge:{key}".encode()).hexdigest()
    return f"bridge:{digest}"


def _claude_native_id(job: Mapping[str, Any]) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"hermes-session-bridge:{job['idempotency_key']}",
        )
    )


def _placeholder_native_id(result: object) -> str:
    native_id = getattr(result, "native_id", None)
    if not isinstance(native_id, str) or not native_id.strip():
        raise RuntimeError("placeholder result has no native identity")
    return native_id.strip()


def _validated_attempt_sidecar(
    raw_sidecar: object,
    job: Mapping[str, Any],
    *,
    policy_generation: int,
) -> dict[str, Any]:
    if not isinstance(raw_sidecar, Mapping):
        raise RuntimeError("mirror attempt sidecar is invalid")
    sidecar = dict(raw_sidecar)
    provider = Provider(job["target_provider"])
    expected_fields = {
        "version",
        "phase",
        "bridge_id",
        "target_provider",
        "policy_generation",
        "attempts",
    }
    if provider is Provider.CLAUDE:
        expected_fields.add("expected_native_id")
    bridge_id = sidecar.get("bridge_id")
    if (
        set(sidecar) != expected_fields
        or sidecar.get("version") != 1
        or sidecar.get("phase") != "provider_call_started"
        or not isinstance(bridge_id, str)
        or bridge_id != _bridge_id(job)
        or sidecar.get("target_provider") != job["target_provider"]
        or sidecar.get("policy_generation") != policy_generation
        or sidecar.get("attempts") != job["attempts"]
    ):
        raise RuntimeError("mirror attempt sidecar is invalid")
    expected_native_id = sidecar.get("expected_native_id")
    if provider is Provider.CLAUDE and expected_native_id != _claude_native_id(job):
        raise RuntimeError("mirror attempt sidecar is invalid")
    return sidecar
