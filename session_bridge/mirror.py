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

    first_meaningful_at = _first_meaningful_user_timestamp(projection)
    if (
        first_meaningful_at is None
        or context.now - first_meaningful_at < context.policy.debounce_seconds
    ):
        return Eligibility(False, target, "empty")

    started_at = _finite_float("projection.started_at", projection.started_at)
    last_active = _finite_float("projection.last_active", projection.last_active)
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
) -> dict[str, Any]:
    expected_key = mirror_idempotency_key(
        source_session_id, target_provider, policy.generation
    )
    job = store.enqueue_mirror_job(
        source_session_id,
        target_provider,
        policy_generation=policy.generation,
    )
    if job.get("idempotency_key") != expected_key:
        raise RuntimeError("store returned an unexpected mirror idempotency key")
    return job


def record_mirror_failure(
    store: SessionBridgeStore,
    job: Mapping[str, Any],
    *,
    policy: MirrorPolicy,
    now: float,
    code: str,
    detail: str,
) -> MirrorJobState:
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

    if attempts >= policy.max_attempts:
        store.fail_job_manually(job_id, code=code, detail=detail)
        return MirrorJobState.MANUAL_FAILURE

    next_attempt_at = failure_time + retry_delay_seconds(idempotency_key, attempts)
    store.retry_job(
        job_id,
        code=code,
        detail=detail,
        next_attempt_at=next_attempt_at,
    )
    return MirrorJobState.RETRY


def provider_concurrency_limit(policy: MirrorPolicy, provider: Provider) -> int:
    normalized = _external_provider(provider)
    if normalized is Provider.CLAUDE:
        return policy.claude_concurrency
    return policy.codex_concurrency


def should_halt_batch(progress: BatchProgress, policy: MirrorPolicy) -> bool:
    if progress.attempts >= policy.stop_after_attempts:
        return True
    return (
        progress.attempts > 0
        and progress.errors / progress.attempts >= policy.stop_error_rate
    )


def select_creation_batch(
    candidates: Sequence[MirrorCandidate],
    *,
    policy: MirrorPolicy,
    now: float,
    in_flight_by_provider: Mapping[Provider | str, int],
    recent_creation_times: Sequence[float],
    progress: BatchProgress,
) -> tuple[MirrorCandidate, ...]:
    selection_time = _finite_float("now", now)
    if not policy.automatic_creation or should_halt_batch(progress, policy):
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
            timestamp = float(message.timestamp)
            if math.isfinite(timestamp):
                timestamps.append(timestamp)
    return min(timestamps) if timestamps else None


def _inverted_provider(provider: Provider) -> Provider:
    normalized = _external_provider(provider)
    if normalized is Provider.CLAUDE:
        return Provider.CODEX
    return Provider.CLAUDE


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
