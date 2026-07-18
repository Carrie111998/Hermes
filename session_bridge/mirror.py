from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
import math
from typing import Any, Literal

from .models import (
    MirrorJobState,
    OriginKind,
    Provider,
    SessionProjection,
    canonical_session_id,
)
from .store import SessionBridgeStore


_EXTERNAL_PROVIDERS = (Provider.CLAUDE, Provider.CODEX)
_CONTINUOUS_WATERMARK_STATE_KEY = "session-bridge:continuous-watermark"
_MIRROR_AUTHORITY_KEY_PREFIX = "session-bridge:mirror-authority:"
_SECONDS_PER_DAY = 24 * 60 * 60
_RETRY_MAX_SECONDS = 300.0
_RETRY_JITTER_MIN = 0.8
_RETRY_JITTER_WIDTH = 0.4

EligibilityReason = Literal[
    "eligible",
    "too_old",
    "before_watermark",
    "empty",
    "unstable_identity",
    "bridge_origin",
    "already_mapped",
]


class DiscoveryMode(StrEnum):
    INITIAL_BACKFILL = "initial_backfill"
    CONTINUOUS = "continuous"


@dataclass(frozen=True)
class MirrorPolicy:
    generation: int = 1
    automatic_creation: bool = False
    backfill_days: int = 30
    debounce_seconds: float = 5.0
    claude_concurrency: int = 1
    codex_concurrency: int = 2
    creates_per_minute: int = 6
    max_attempts: int = 5
    stop_after_attempts: int = 20
    stop_error_rate: float = 0.25

    def __post_init__(self) -> None:
        if type(self.automatic_creation) is not bool:
            raise ValueError("automatic_creation must be a boolean")
        _nonnegative_integer("generation", self.generation)
        _nonnegative_integer("backfill_days", self.backfill_days)
        debounce_seconds = _finite_float("debounce_seconds", self.debounce_seconds)
        if debounce_seconds < 0:
            raise ValueError("debounce_seconds must be non-negative")
        object.__setattr__(self, "debounce_seconds", debounce_seconds)
        _positive_integer("claude_concurrency", self.claude_concurrency)
        _positive_integer("codex_concurrency", self.codex_concurrency)
        _positive_integer("creates_per_minute", self.creates_per_minute)
        _positive_integer("max_attempts", self.max_attempts)
        _positive_integer("stop_after_attempts", self.stop_after_attempts)
        error_rate = _finite_float("stop_error_rate", self.stop_error_rate)
        if not 0.0 <= error_rate <= 1.0:
            raise ValueError("stop_error_rate must be between zero and one")
        object.__setattr__(self, "stop_error_rate", error_rate)


@dataclass(frozen=True)
class EligibilityContext:
    now: float
    discovery_mode: DiscoveryMode
    continuous_watermark: float | None
    existing_target_mappings: frozenset[tuple[str, Provider]]
    policy: MirrorPolicy = field(default_factory=MirrorPolicy)

    def __post_init__(self) -> None:
        object.__setattr__(self, "now", _finite_float("now", self.now))
        try:
            mode = DiscoveryMode(self.discovery_mode)
        except (TypeError, ValueError) as exc:
            raise ValueError("unknown discovery mode") from exc
        object.__setattr__(self, "discovery_mode", mode)

        if self.continuous_watermark is not None:
            object.__setattr__(
                self,
                "continuous_watermark",
                _finite_float("continuous_watermark", self.continuous_watermark),
            )
        if not isinstance(self.policy, MirrorPolicy):
            raise TypeError("policy must be a MirrorPolicy")

        normalized_mappings: set[tuple[str, Provider]] = set()
        try:
            mappings = frozenset(self.existing_target_mappings)
        except TypeError as exc:
            raise TypeError("existing_target_mappings must be iterable") from exc
        for source_session_id, target_provider in mappings:
            if not isinstance(source_session_id, str) or not source_session_id.strip():
                raise ValueError("mapped source session ID must not be empty")
            target = _external_provider(target_provider)
            normalized_mappings.add((source_session_id.strip(), target))
        object.__setattr__(
            self, "existing_target_mappings", frozenset(normalized_mappings)
        )


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    target_provider: Provider
    reason: EligibilityReason

    def __post_init__(self) -> None:
        target = _external_provider(self.target_provider)
        object.__setattr__(self, "target_provider", target)
        allowed_reasons = {
            "eligible",
            "too_old",
            "before_watermark",
            "empty",
            "unstable_identity",
            "bridge_origin",
            "already_mapped",
        }
        if self.reason not in allowed_reasons:
            raise ValueError("unknown mirror eligibility reason")
        if self.eligible != (self.reason == "eligible"):
            raise ValueError("eligibility boolean and reason disagree")


@dataclass(frozen=True)
class MirrorCandidate:
    source_session_id: str
    target_provider: Provider
    last_active: float
    projection: SessionProjection

    def __post_init__(self) -> None:
        if not isinstance(self.projection, SessionProjection):
            raise TypeError("mirror candidate projection must be a SessionProjection")
        try:
            expected_source = canonical_session_id(
                self.projection.provider, self.projection.native_id
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "mirror candidate source session identity is invalid"
            ) from exc
        if self.source_session_id != expected_source:
            raise ValueError("mirror candidate source session identity does not match")

        expected_target = _inverted_provider(self.projection.provider)
        target = _external_provider(self.target_provider)
        if target is not expected_target:
            raise ValueError(
                "mirror candidate target must be the exact inverse provider"
            )

        candidate_activity = _finite_float(
            "mirror candidate last_active", self.last_active
        )
        projection_activity = _finite_float(
            "projection.last_active", self.projection.last_active
        )
        if candidate_activity != projection_activity:
            raise ValueError("mirror candidate last activity does not match projection")
        try:
            origin_kind = OriginKind(self.projection.origin_kind)
        except (TypeError, ValueError) as exc:
            raise ValueError("mirror candidate requires a native projection") from exc
        if (
            origin_kind is not OriginKind.NATIVE
            or self.projection.origin_bridge_id is not None
        ):
            raise ValueError("mirror candidate requires a native projection")

        object.__setattr__(self, "target_provider", target)
        object.__setattr__(self, "last_active", candidate_activity)


@dataclass(frozen=True)
class BatchProgress:
    attempts: int = 0
    errors: int = 0

    def __post_init__(self) -> None:
        _nonnegative_integer("attempts", self.attempts)
        _nonnegative_integer("errors", self.errors)
        if self.errors > self.attempts:
            raise ValueError("errors cannot exceed attempts")


def mirror_idempotency_key(
    source_session_id: str,
    target: Provider,
    generation: int,
) -> str:
    if not isinstance(source_session_id, str) or not source_session_id.strip():
        raise ValueError("source session ID must not be empty")
    provider = _external_provider(target)
    _nonnegative_integer("generation", generation)
    return _stable_id(
        "mirror-job",
        source_session_id.strip(),
        provider.value,
        str(generation),
    )


def retry_delay_seconds(idempotency_key: str, attempts: int) -> float:
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise ValueError("idempotency key must not be empty")
    _positive_integer("attempts", attempts)
    base = min(_RETRY_MAX_SECONDS, 2.0 ** min(attempts - 1, 30))
    jitter_digest = hashlib.sha256(
        f"{len(idempotency_key)}:{idempotency_key}:{attempts}".encode()
    ).digest()
    jitter_unit = int.from_bytes(jitter_digest[:8], "big") / ((1 << 64) - 1)
    jitter = _RETRY_JITTER_MIN + _RETRY_JITTER_WIDTH * jitter_unit
    return min(_RETRY_MAX_SECONDS, base * jitter)


def classify_mirror_eligibility(
    projection: SessionProjection,
    context: EligibilityContext,
) -> Eligibility:
    target = _inverted_provider(projection.provider)
    native_id = projection.native_id
    if not isinstance(native_id, str) or not native_id.strip():
        return Eligibility(False, target, "unstable_identity")

    source_session_id = canonical_session_id(projection.provider, native_id)
    if (
        projection.origin_kind is not OriginKind.NATIVE
        or projection.origin_bridge_id is not None
    ):
        return Eligibility(False, target, "bridge_origin")
    if (source_session_id, target) in context.existing_target_mappings:
        return Eligibility(False, target, "already_mapped")

    timeline = _validated_projection_timeline(projection, context.now)
    if timeline is None:
        return Eligibility(False, target, "unstable_identity")
    started_at, last_active = timeline
    first_meaningful_at = _first_meaningful_user_timestamp(projection)
    if (
        first_meaningful_at is None
        or context.now - first_meaningful_at < context.policy.debounce_seconds
    ):
        return Eligibility(False, target, "empty")

    if context.discovery_mode is DiscoveryMode.INITIAL_BACKFILL:
        oldest = context.now - context.policy.backfill_days * _SECONDS_PER_DAY
        if last_active < oldest:
            return Eligibility(False, target, "too_old")
    else:
        watermark = context.continuous_watermark
        if watermark is None or started_at <= watermark:
            return Eligibility(False, target, "before_watermark")

    return Eligibility(True, target, "eligible")


def eligible_mirror_candidates(
    projections: Sequence[SessionProjection],
    context: EligibilityContext,
) -> tuple[MirrorCandidate, ...]:
    candidates: list[MirrorCandidate] = []
    for projection in projections:
        eligibility = classify_mirror_eligibility(projection, context)
        if not eligibility.eligible:
            continue
        candidates.append(
            MirrorCandidate(
                source_session_id=canonical_session_id(
                    projection.provider, projection.native_id
                ),
                target_provider=eligibility.target_provider,
                last_active=float(projection.last_active),
                projection=projection,
            )
        )
    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                -candidate.last_active,
                candidate.source_session_id,
                candidate.target_provider.value,
            ),
        )
    )


def persist_continuous_watermark(store: SessionBridgeStore, watermark: float) -> None:
    normalized = _finite_float("continuous_watermark", watermark)
    value_json = json.dumps(
        {"continuous_watermark": normalized},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    updated_at = _finite_float("store clock", store._clock())

    def _write(connection: Any) -> None:
        row = connection.execute(
            "SELECT value_json FROM session_bridge_state WHERE key = ?",
            (_CONTINUOUS_WATERMARK_STATE_KEY,),
        ).fetchone()
        if row is not None:
            existing = _decode_watermark_state(row["value_json"])
            if normalized < existing:
                raise ValueError("continuous watermark cannot move backwards")
        connection.execute(
            """INSERT INTO session_bridge_state (key, value_json, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value_json = excluded.value_json,
                   updated_at = excluded.updated_at""",
            (_CONTINUOUS_WATERMARK_STATE_KEY, value_json, updated_at),
        )

    store.db._execute_write(_write)


def load_continuous_watermark(store: SessionBridgeStore) -> float | None:
    state = store.get_state(_CONTINUOUS_WATERMARK_STATE_KEY)
    if state is None:
        return None
    return _decode_watermark_mapping(state)


def enqueue_mirror_job(
    store: SessionBridgeStore,
    source_session_id: str,
    target_provider: Provider,
    *,
    policy: MirrorPolicy,
    manual_authorized: bool = False,
    candidate: MirrorCandidate | None = None,
    context: EligibilityContext | None = None,
    retry_failed: bool = False,
    require_unmapped: bool = False,
    rollout_limited: bool = False,
) -> dict[str, Any]:
    if not isinstance(policy, MirrorPolicy):
        raise TypeError("policy must be a MirrorPolicy")
    if type(manual_authorized) is not bool:
        raise ValueError("manual_authorized must be a boolean")
    if type(retry_failed) is not bool:
        raise ValueError("retry_failed must be a boolean")
    if type(require_unmapped) is not bool:
        raise ValueError("require_unmapped must be a boolean")
    if type(rollout_limited) is not bool:
        raise ValueError("rollout_limited must be a boolean")
    if retry_failed and not manual_authorized:
        raise ValueError("retry_failed requires manual_authorized=True")
    if require_unmapped and not manual_authorized:
        raise ValueError("require_unmapped requires manual_authorized=True")
    if rollout_limited and not require_unmapped:
        raise ValueError("rollout_limited requires require_unmapped=True")
    source_provider = _canonical_source_provider(source_session_id)
    target = _external_provider(target_provider)
    if target is not _inverted_provider(source_provider):
        raise ValueError("mirror target must be the exact inverse provider")
    if not policy.automatic_creation and not manual_authorized:
        raise PermissionError("mirror enqueue requires automatic or manual authority")

    expected_key = mirror_idempotency_key(source_session_id, target, policy.generation)
    if not manual_authorized:
        _validate_automatic_candidate(
            source_session_id,
            target,
            policy=policy,
            candidate=candidate,
            context=context,
        )
    return _enqueue_authorized_job(
        store,
        source_session_id,
        source_provider,
        target,
        policy=policy,
        idempotency_key=expected_key,
        authority="manual" if manual_authorized else "automatic",
        candidate=candidate,
        context=context,
        retry_failed=retry_failed,
        require_unmapped=require_unmapped,
        rollout_limited=rollout_limited,
    )


def claim_due_mirror_jobs(
    store: SessionBridgeStore,
    *,
    limit: int,
    policy: MirrorPolicy,
    job_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(policy, MirrorPolicy):
        raise TypeError("policy must be a MirrorPolicy")
    _nonnegative_integer("claim limit", limit)
    normalized_job_ids = _exact_job_ids(job_ids)
    if limit == 0 or normalized_job_ids == ():
        return []

    def _write(connection: Any) -> list[dict[str, Any]]:
        now = _finite_float("store clock", store._clock())
        scan_limit = max(limit * 4, limit + 32)
        scope_clause = ""
        scope_params: list[Any] = []
        if normalized_job_ids is not None:
            placeholders = ",".join("?" for _ in normalized_job_ids)
            scope_clause = f" AND job.id IN ({placeholders})"
            scope_params.extend(normalized_job_ids)
        due = connection.execute(
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
                _MIRROR_AUTHORITY_KEY_PREFIX,
                MirrorJobState.QUEUED.value,
                MirrorJobState.RETRY.value,
                now,
                *scope_params,
                '{"authority":"manual",%',
                scan_limit,
            ),
        ).fetchall()
        claimed: list[dict[str, Any]] = []
        for job in due:
            if len(claimed) >= limit:
                break
            try:
                authority = _read_mirror_authority(connection, job)
            except _MissingMirrorAuthority:
                _terminalize_unclaimable_job(
                    connection,
                    job,
                    now=now,
                    code="authority_missing",
                    detail="mirror authority metadata is missing",
                )
                continue
            except _InvalidMirrorAuthority:
                _terminalize_unclaimable_job(
                    connection,
                    job,
                    now=now,
                    code="authority_invalid",
                    detail="mirror authority metadata is invalid",
                )
                continue

            claim_authority = authority["authority"]
            if claim_authority == "automatic" and not policy.automatic_creation:
                continue
            if claim_authority == "automatic" or authority["require_unmapped"]:
                try:
                    source_provider = _canonical_source_provider(
                        job["source_session_id"]
                    )
                    target_provider = _external_provider(job["target_provider"])
                    denial = _automatic_authority_denial(
                        connection,
                        job["source_session_id"],
                        source_provider,
                        target_provider,
                    )
                except (TypeError, ValueError):
                    denial = "automatic mirror authority is invalid"
                if denial is not None:
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
                        connection,
                        job,
                        now=now,
                        code=code,
                        detail=detail,
                    )
                    continue

            cursor = connection.execute(
                """UPDATE session_mirror_jobs
                   SET state = ?, attempts = attempts + 1, updated_at = ?
                   WHERE id = ? AND state = ? AND attempts = ?
                     AND idempotency_key = ?""",
                (
                    MirrorJobState.RUNNING.value,
                    now,
                    job["id"],
                    job["state"],
                    job["attempts"],
                    job["idempotency_key"],
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("stale mirror job claim")
            claimed_job = dict(
                connection.execute(
                    "SELECT * FROM session_mirror_jobs WHERE id = ?",
                    (job["id"],),
                ).fetchone()
            )
            claimed_job["claim_authority"] = claim_authority
            claimed_job["rollout_limited"] = authority["rollout_limited"]
            claimed.append(claimed_job)
        return claimed

    return store.db._execute_write(_write)


def record_mirror_failure(
    store: SessionBridgeStore,
    job: Mapping[str, Any],
    *,
    policy: MirrorPolicy,
    now: float,
    code: str,
    detail: str,
) -> MirrorJobState:
    if not isinstance(policy, MirrorPolicy):
        raise TypeError("policy must be a MirrorPolicy")
    if (
        not isinstance(code, str)
        or not code.strip()
        or not isinstance(detail, str)
        or not detail.strip()
    ):
        raise ValueError("mirror failure code and detail must be nonempty strings")
    job_id = job.get("id")
    idempotency_key = job.get("idempotency_key")
    attempts = job.get("attempts")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("mirror job ID must not be empty")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise ValueError("mirror job idempotency key must not be empty")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts <= 0:
        raise ValueError("job attempts must be a positive integer")
    failure_time = _finite_float("now", now)

    def _write(connection: Any) -> MirrorJobState:
        authoritative_now = _finite_float("store clock", store._clock())
        durable = connection.execute(
            "SELECT * FROM session_mirror_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if durable is None:
            raise KeyError(job_id)
        durable_attempts = durable["attempts"]
        durable_key = durable["idempotency_key"]
        durable_updated_at = _finite_float(
            "durable mirror job updated_at", durable["updated_at"]
        )
        if not durable_updated_at <= failure_time <= authoritative_now:
            raise ValueError("caller time is stale or in the future")
        if (
            durable["state"] != MirrorJobState.RUNNING.value
            or durable_attempts != attempts
            or durable_key != idempotency_key
        ):
            raise ValueError("stale mirror job claim")
        if (
            not isinstance(durable_attempts, int)
            or isinstance(durable_attempts, bool)
            or durable_attempts <= 0
            or not isinstance(durable_key, str)
            or not durable_key
        ):
            raise ValueError("invalid durable mirror job claim")

        if durable_attempts >= policy.max_attempts:
            next_state = MirrorJobState.MANUAL_FAILURE
            cursor = connection.execute(
                """UPDATE session_mirror_jobs
                   SET state = ?, error_code = ?, error_detail = ?, updated_at = ?
                   WHERE id = ? AND state = ? AND attempts = ?
                     AND idempotency_key = ?""",
                (
                    next_state.value,
                    code,
                    detail,
                    authoritative_now,
                    job_id,
                    MirrorJobState.RUNNING.value,
                    durable_attempts,
                    durable_key,
                ),
            )
        else:
            next_state = MirrorJobState.RETRY
            next_attempt_at = authoritative_now + retry_delay_seconds(
                durable_key, durable_attempts
            )
            cursor = connection.execute(
                """UPDATE session_mirror_jobs
                   SET state = ?, error_code = ?, error_detail = ?,
                       next_attempt_at = ?, updated_at = ?
                   WHERE id = ? AND state = ? AND attempts = ?
                     AND idempotency_key = ?""",
                (
                    next_state.value,
                    code,
                    detail,
                    next_attempt_at,
                    authoritative_now,
                    job_id,
                    MirrorJobState.RUNNING.value,
                    durable_attempts,
                    durable_key,
                ),
            )
        if cursor.rowcount != 1:
            raise ValueError("stale mirror job claim")
        return next_state

    return store.db._execute_write(_write)


def provider_concurrency_limit(policy: MirrorPolicy, provider: Provider) -> int:
    normalized = _external_provider(provider)
    if normalized is Provider.CLAUDE:
        return policy.claude_concurrency
    return policy.codex_concurrency


def should_halt_batch(progress: BatchProgress, policy: MirrorPolicy) -> bool:
    if progress.attempts >= policy.stop_after_attempts:
        return True
    return (
        progress.errors > 0
        and progress.errors / progress.attempts >= policy.stop_error_rate
    )


def select_creation_batch(
    candidates: Sequence[MirrorCandidate],
    *,
    policy: MirrorPolicy,
    context: EligibilityContext | None = None,
    now: float,
    in_flight_by_provider: Mapping[Provider | str, int],
    recent_creation_times: Sequence[float],
    progress: BatchProgress,
) -> tuple[MirrorCandidate, ...]:
    selection_time = _finite_float("now", now)
    if not policy.automatic_creation:
        return ()
    if not isinstance(context, EligibilityContext):
        raise ValueError("automatic creation requires an eligibility context")
    if context.policy != policy or context.now != selection_time:
        raise ValueError("eligibility context must match policy and selection time")
    if should_halt_batch(progress, policy):
        return ()

    normalized_creation_times: list[float] = []
    for created_at in recent_creation_times:
        normalized_created_at = _finite_float("creation time", created_at)
        if normalized_created_at > selection_time:
            raise ValueError("creation time cannot be in the future")
        normalized_creation_times.append(normalized_created_at)
    recent_count = sum(
        selection_time - 60.0 < created_at for created_at in normalized_creation_times
    )
    remaining_attempts = policy.stop_after_attempts - progress.attempts
    rate_capacity = min(
        remaining_attempts,
        max(0, policy.creates_per_minute - recent_count),
    )
    if rate_capacity == 0:
        return ()

    normalized_in_flight = {provider: 0 for provider in _EXTERNAL_PROVIDERS}
    for raw_provider, raw_in_flight in in_flight_by_provider.items():
        provider = _external_provider(raw_provider)
        _nonnegative_integer(f"{provider.value} in-flight count", raw_in_flight)
        normalized_in_flight[provider] += raw_in_flight

    available: dict[Provider, int] = {}
    for provider in _EXTERNAL_PROVIDERS:
        available[provider] = max(
            0,
            provider_concurrency_limit(policy, provider)
            - normalized_in_flight[provider],
        )

    selected: list[MirrorCandidate] = []
    seen_keys: set[str] = set()
    ordered = sorted(
        candidates,
        key=lambda candidate: (
            -candidate.last_active,
            candidate.source_session_id,
            candidate.target_provider.value,
        ),
    )
    for candidate in ordered:
        eligibility = classify_mirror_eligibility(candidate.projection, context)
        if (
            not eligibility.eligible
            or eligibility.target_provider is not candidate.target_provider
        ):
            continue
        target = _external_provider(candidate.target_provider)
        key = mirror_idempotency_key(
            candidate.source_session_id, target, policy.generation
        )
        if key in seen_keys or available[target] <= 0:
            continue
        selected.append(candidate)
        seen_keys.add(key)
        available[target] -= 1
        if len(selected) >= rate_capacity:
            break
    return tuple(selected)


def _first_meaningful_user_timestamp(projection: SessionProjection) -> float | None:
    timestamps: list[float] = []
    for message in projection.messages:
        if (
            message.role == "user"
            and isinstance(message.content, str)
            and bool(message.content.strip())
        ):
            timestamps.append(_finite_float("message timestamp", message.timestamp))
    return min(timestamps) if timestamps else None


def _validated_projection_timeline(
    projection: SessionProjection, now: float
) -> tuple[float, float] | None:
    try:
        started_at = _finite_float("projection.started_at", projection.started_at)
        last_active = _finite_float("projection.last_active", projection.last_active)
    except ValueError:
        return None
    if started_at > last_active or started_at > now or last_active > now:
        return None
    for message in projection.messages:
        try:
            timestamp = _finite_float("message timestamp", message.timestamp)
        except (AttributeError, ValueError):
            return None
        if timestamp < started_at or timestamp > last_active or timestamp > now:
            return None
    return started_at, last_active


def _inverted_provider(provider: Provider) -> Provider:
    normalized = _external_provider(provider)
    if normalized is Provider.CLAUDE:
        return Provider.CODEX
    return Provider.CLAUDE


def _canonical_source_provider(source_session_id: str) -> Provider:
    if (
        not isinstance(source_session_id, str)
        or not source_session_id
        or source_session_id != source_session_id.strip()
    ):
        raise ValueError("source must be a canonical Claude or Codex session ID")
    prefix, separator, native_id = source_session_id.partition(":")
    if not separator or not native_id or native_id != native_id.strip():
        raise ValueError("source must be a canonical Claude or Codex session ID")
    try:
        provider = _external_provider(prefix)
        canonical = canonical_session_id(provider, native_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "source must be a canonical Claude or Codex session ID"
        ) from exc
    if canonical != source_session_id:
        raise ValueError("source must be a canonical Claude or Codex session ID")
    return provider


def _validate_automatic_candidate(
    source_session_id: str,
    target: Provider,
    *,
    policy: MirrorPolicy,
    candidate: MirrorCandidate | None,
    context: EligibilityContext | None,
) -> None:
    if not isinstance(candidate, MirrorCandidate) or not isinstance(
        context, EligibilityContext
    ):
        raise ValueError("automatic enqueue requires candidate and context")
    if context.policy != policy:
        raise ValueError("automatic enqueue context policy is stale")
    if (
        candidate.source_session_id != source_session_id
        or candidate.target_provider is not target
    ):
        raise ValueError("automatic enqueue candidate identity does not match")
    eligibility = classify_mirror_eligibility(candidate.projection, context)
    if not eligibility.eligible or eligibility.target_provider is not target:
        raise PermissionError(
            f"automatic enqueue is not eligible: {eligibility.reason}"
        )


def _enqueue_authorized_job(
    store: SessionBridgeStore,
    source_session_id: str,
    source_provider: Provider,
    target_provider: Provider,
    *,
    policy: MirrorPolicy,
    idempotency_key: str,
    authority: Literal["automatic", "manual"],
    candidate: MirrorCandidate | None,
    context: EligibilityContext | None,
    retry_failed: bool,
    require_unmapped: bool,
    rollout_limited: bool,
) -> dict[str, Any]:
    job_id = f"job:{idempotency_key}"

    def _write(connection: Any) -> dict[str, Any]:
        now = _finite_float("store clock", store._clock())
        if authority == "automatic":
            assert candidate is not None
            assert context is not None
            continuous_watermark = context.continuous_watermark
            if context.discovery_mode is DiscoveryMode.CONTINUOUS:
                watermark_row = connection.execute(
                    "SELECT value_json FROM session_bridge_state WHERE key = ?",
                    (_CONTINUOUS_WATERMARK_STATE_KEY,),
                ).fetchone()
                if watermark_row is None:
                    raise PermissionError(
                        "automatic enqueue is not eligible: before_watermark"
                    )
                continuous_watermark = _decode_watermark_state(
                    watermark_row["value_json"]
                )
            current_context = EligibilityContext(
                now=now,
                discovery_mode=context.discovery_mode,
                continuous_watermark=continuous_watermark,
                existing_target_mappings=context.existing_target_mappings,
                policy=policy,
            )
            eligibility = classify_mirror_eligibility(
                candidate.projection, current_context
            )
            if not eligibility.eligible:
                raise PermissionError(
                    f"automatic enqueue is not eligible: {eligibility.reason}"
                )
            denial = _automatic_authority_denial(
                connection,
                source_session_id,
                source_provider,
                target_provider,
            )
            if denial is not None:
                raise PermissionError(denial)
        elif require_unmapped:
            denial = _automatic_authority_denial(
                connection,
                source_session_id,
                source_provider,
                target_provider,
            )
            if denial is not None:
                raise PermissionError(denial.replace("automatic", "safe manual", 1))

        pair_jobs = connection.execute(
            """SELECT * FROM session_mirror_jobs
               WHERE source_session_id = ? AND target_provider = ?
               ORDER BY created_at, id""",
            (source_session_id, target_provider.value),
        ).fetchall()
        exact_job = next(
            (job for job in pair_jobs if job["idempotency_key"] == idempotency_key),
            None,
        )
        if exact_job is not None:
            existing_authority = _read_mirror_authority(connection, exact_job)
            if authority == "manual" and (
                existing_authority["authority"] == "automatic"
                or (require_unmapped and not existing_authority["require_unmapped"])
                or (rollout_limited and not existing_authority["rollout_limited"])
            ):
                promoted_value = _encode_mirror_authority(
                    authority="manual",
                    idempotency_key=exact_job["idempotency_key"],
                    source_session_id=exact_job["source_session_id"],
                    target_provider=_external_provider(exact_job["target_provider"]),
                    policy_generation=existing_authority["policy_generation"],
                    require_unmapped=(
                        bool(existing_authority["require_unmapped"]) or require_unmapped
                    ),
                    rollout_limited=(
                        bool(existing_authority["rollout_limited"]) or rollout_limited
                    ),
                )
                cursor = connection.execute(
                    """UPDATE session_bridge_state
                       SET value_json = ?, updated_at = ?
                       WHERE key = ?""",
                    (
                        promoted_value,
                        now,
                        _mirror_authority_state_key(exact_job["id"]),
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("stale mirror authority promotion")

            if MirrorJobState(exact_job["state"]) is MirrorJobState.MANUAL_FAILURE:
                if not retry_failed:
                    raise ValueError("manual failure recovery requires retry_failed")
                cursor = connection.execute(
                    """UPDATE session_mirror_jobs
                       SET state = ?, attempts = 0, next_attempt_at = ?,
                           error_code = NULL, error_detail = NULL, updated_at = ?
                       WHERE id = ? AND state = ? AND attempts = ?
                         AND idempotency_key = ?""",
                    (
                        MirrorJobState.QUEUED.value,
                        now,
                        now,
                        exact_job["id"],
                        MirrorJobState.MANUAL_FAILURE.value,
                        exact_job["attempts"],
                        exact_job["idempotency_key"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("stale mirror job recovery")
                exact_job = connection.execute(
                    "SELECT * FROM session_mirror_jobs WHERE id = ?",
                    (exact_job["id"],),
                ).fetchone()
            return dict(exact_job)
        for prior in pair_jobs:
            prior_state = MirrorJobState(prior["state"])
            if prior_state is MirrorJobState.MANUAL_FAILURE:
                if not retry_failed:
                    raise ValueError(
                        "different-generation manual failure requires retry_failed"
                    )
                continue
            if prior_state is MirrorJobState.SUCCEEDED:
                raise ValueError("prior different-generation job succeeded")
            raise ValueError("different-generation mirror job is still active")

        authority_key = _mirror_authority_state_key(job_id)
        orphaned_authority = connection.execute(
            "SELECT value_json FROM session_bridge_state WHERE key = ?",
            (authority_key,),
        ).fetchone()
        if orphaned_authority is not None:
            raise ValueError("orphaned mirror authority state")

        connection.execute(
            """INSERT INTO session_mirror_jobs (
               id, idempotency_key, source_session_id, target_provider,
               state, attempts, next_attempt_at, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)""",
            (
                job_id,
                idempotency_key,
                source_session_id,
                target_provider.value,
                MirrorJobState.QUEUED.value,
                now,
                now,
                now,
            ),
        )
        authority_value = _encode_mirror_authority(
            authority=authority,
            idempotency_key=idempotency_key,
            source_session_id=source_session_id,
            target_provider=target_provider,
            policy_generation=policy.generation,
            require_unmapped=require_unmapped,
            rollout_limited=rollout_limited,
        )
        connection.execute(
            """INSERT INTO session_bridge_state (key, value_json, updated_at)
               VALUES (?, ?, ?)""",
            (authority_key, authority_value, now),
        )
        job = connection.execute(
            "SELECT * FROM session_mirror_jobs WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if job is None or (
            job["id"] != job_id
            or job["source_session_id"] != source_session_id
            or job["target_provider"] != target_provider.value
        ):
            raise RuntimeError("conflicting mirror job replay")
        return dict(job)

    return store.db._execute_write(_write)


def _automatic_authority_denial(
    connection: Any,
    source_session_id: str,
    source_provider: Provider,
    target_provider: Provider,
) -> str | None:
    source = connection.execute(
        """SELECT s.source, e.provider, e.native_id, e.origin_kind,
                  e.origin_bridge_id
           FROM sessions AS s
           JOIN external_sessions AS e ON e.session_id = s.id
           WHERE s.id = ?""",
        (source_session_id,),
    ).fetchone()
    expected_native_id = source_session_id.split(":", 1)[1]
    if source is None or (
        source["source"] != source_provider.value
        or source["provider"] != source_provider.value
        or source["native_id"] != expected_native_id
    ):
        return "automatic enqueue source identity is not durable"
    if (
        source["origin_kind"] != OriginKind.NATIVE.value
        or source["origin_bridge_id"] is not None
    ):
        return "automatic enqueue durable source origin is not native"
    mapped = connection.execute(
        """SELECT 1
           FROM session_links AS link
           JOIN external_sessions AS target
             ON target.session_id = link.to_session_id
           WHERE link.from_session_id = ? AND target.provider = ?
           LIMIT 1""",
        (source_session_id, target_provider.value),
    ).fetchone()
    if mapped is not None:
        return "automatic enqueue source is already mapped"
    return None


class _MissingMirrorAuthority(ValueError):
    pass


class _InvalidMirrorAuthority(ValueError):
    pass


def _mirror_authority_state_key(job_id: str) -> str:
    return f"{_MIRROR_AUTHORITY_KEY_PREFIX}{job_id}"


def _encode_mirror_authority(
    *,
    authority: Literal["automatic", "manual"],
    idempotency_key: str,
    source_session_id: str,
    target_provider: Provider,
    policy_generation: int,
    require_unmapped: bool = False,
    rollout_limited: bool = False,
) -> str:
    return json.dumps(
        {
            "authority": authority,
            "idempotency_key": idempotency_key,
            "policy_generation": policy_generation,
            "require_unmapped": require_unmapped,
            "rollout_limited": rollout_limited,
            "source_session_id": source_session_id,
            "target_provider": target_provider.value,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _read_mirror_authority(connection: Any, job: Mapping[str, Any]) -> dict[str, Any]:
    row = connection.execute(
        "SELECT value_json FROM session_bridge_state WHERE key = ?",
        (_mirror_authority_state_key(job["id"]),),
    ).fetchone()
    if row is None:
        raise _MissingMirrorAuthority("mirror authority metadata is missing")
    try:
        value = json.loads(row["value_json"])
    except (TypeError, ValueError) as exc:
        raise _InvalidMirrorAuthority("invalid mirror authority metadata") from exc
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
        raise _InvalidMirrorAuthority("invalid mirror authority metadata")
    require_unmapped = value.get("require_unmapped", False)
    rollout_limited = value.get("rollout_limited", False)
    if (
        type(require_unmapped) is not bool
        or type(rollout_limited) is not bool
        or (rollout_limited and not require_unmapped)
    ):
        raise _InvalidMirrorAuthority("invalid mirror authority metadata")
    value["require_unmapped"] = require_unmapped
    value["rollout_limited"] = rollout_limited
    authority = value["authority"]
    generation = value["policy_generation"]
    if authority not in ("automatic", "manual") or (
        rollout_limited and authority != "manual"
    ):
        raise _InvalidMirrorAuthority("invalid mirror authority metadata")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 0
    ):
        raise _InvalidMirrorAuthority("invalid mirror authority metadata")
    try:
        target = _external_provider(value["target_provider"])
        expected_key = mirror_idempotency_key(
            value["source_session_id"], target, generation
        )
    except (TypeError, ValueError) as exc:
        raise _InvalidMirrorAuthority("invalid mirror authority metadata") from exc
    if (
        value["idempotency_key"] != expected_key
        or value["idempotency_key"] != job["idempotency_key"]
        or value["source_session_id"] != job["source_session_id"]
        or target.value != job["target_provider"]
        or job["id"] != f"job:{expected_key}"
    ):
        raise _InvalidMirrorAuthority("invalid mirror authority metadata")
    return value


def _terminalize_unclaimable_job(
    connection: Any,
    job: Mapping[str, Any],
    *,
    now: float,
    code: str,
    detail: str,
) -> None:
    cursor = connection.execute(
        """UPDATE session_mirror_jobs
           SET state = ?, error_code = ?, error_detail = ?, updated_at = ?
           WHERE id = ? AND state = ? AND attempts = ?
             AND idempotency_key = ?""",
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


def _external_provider(provider: Provider | str) -> Provider:
    try:
        normalized = Provider(provider)
    except (TypeError, ValueError) as exc:
        raise ValueError("mirror provider must be Claude or Codex") from exc
    if normalized not in _EXTERNAL_PROVIDERS:
        raise ValueError("mirror provider must be Claude or Codex")
    return normalized


def _stable_id(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _decode_watermark_state(value_json: object) -> float:
    if not isinstance(value_json, str):
        raise ValueError("invalid continuous watermark state")
    try:
        state = json.loads(value_json)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid continuous watermark state") from exc
    if not isinstance(state, dict):
        raise ValueError("invalid continuous watermark state")
    return _decode_watermark_mapping(state)


def _decode_watermark_mapping(state: Mapping[str, Any]) -> float:
    if set(state) != {"continuous_watermark"}:
        raise ValueError("invalid continuous watermark state")
    return _finite_float("continuous_watermark", state["continuous_watermark"])


def _finite_float(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{name} must be a finite number")
    return normalized


def _exact_job_ids(job_ids: Sequence[str] | None) -> tuple[str, ...] | None:
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


def _nonnegative_integer(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _positive_integer(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


__all__ = [
    "BatchProgress",
    "DiscoveryMode",
    "Eligibility",
    "EligibilityContext",
    "EligibilityReason",
    "MirrorCandidate",
    "MirrorPolicy",
    "claim_due_mirror_jobs",
    "classify_mirror_eligibility",
    "eligible_mirror_candidates",
    "enqueue_mirror_job",
    "load_continuous_watermark",
    "mirror_idempotency_key",
    "persist_continuous_watermark",
    "provider_concurrency_limit",
    "record_mirror_failure",
    "retry_delay_seconds",
    "select_creation_batch",
    "should_halt_batch",
]
