"""Immutable, observe-only contracts for the Phase 2 observer surface."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from plugins.agentops.control.models import AuthorityMode


_TARGET_ID = re.compile(r"^[a-z0-9][a-z0-9:._-]{2,199}$")
_COLLECTOR_NAME = re.compile(r"^[a-z][a-z0-9_.-]{1,80}$")
_OBSERVATION_ID = re.compile(r"^[a-f0-9-]{36}$")


def _freeze_value(value: Any) -> Any:
    """Recursively detach nested data from a caller before retaining it."""
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("mapping keys must be strings")
        return MappingProxyType({key: _freeze_value(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(child) for child in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("unsupported evidence value")


def thaw_value(value: Any) -> Any:
    """Create JSON-compatible detached values for a boundary crossing."""
    if isinstance(value, Mapping):
        return {key: thaw_value(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [thaw_value(child) for child in value]
    return value


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("mapping required")
    frozen = _freeze_value(value)
    if not isinstance(frozen, Mapping):
        raise ValueError("mapping required")
    return frozen


def _require_aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_asset_path(value: str | Path) -> Path:
    """Return a resolved asset identity before a collector binds a source."""
    path = Path(value).expanduser()
    return Path(os.path.abspath(os.fspath(path))).resolve(strict=False)


def asset_source_id(value: str | Path) -> str:
    canonical = os.fspath(canonical_asset_path(value)).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def target_allows_asset(target: "Target", value: str | Path) -> bool:
    candidate = canonical_asset_path(value)
    for observed in target.spec.observed_paths:
        root = Path(observed)
        try:
            candidate.relative_to(root)
            return True
        except ValueError:
            continue
    return False


class TargetKind(str, Enum):
    GATEWAY = "gateway"
    CRON = "cron"
    REPOSITORY = "repository"
    SQLITE = "sqlite"


class Criticality(str, Enum):
    CRITICAL = "critical"
    NONCRITICAL = "noncritical"


class CursorResetReason(str, Enum):
    INITIAL = "initial"
    CONTINUE = "continue"
    ROTATED = "rotated"
    TRUNCATED = "truncated"
    SOURCE_CHANGED = "source_changed"


@dataclass(frozen=True)
class TargetSpec:
    """The canonical assets that one target has authorized for observation."""

    target_id: str
    profile: str
    kind: TargetKind
    criticality: Criticality
    observed_paths: tuple[str, ...] = ()
    labels: Mapping[str, str] = field(default_factory=dict)
    existing_writer: str = "external-controller"

    def __post_init__(self) -> None:
        if not isinstance(self.target_id, str) or not _TARGET_ID.fullmatch(self.target_id):
            raise ValueError("invalid target id")
        if not isinstance(self.profile, str) or not self.profile.strip():
            raise ValueError("invalid target profile")
        if not isinstance(self.kind, TargetKind) or not isinstance(self.criticality, Criticality):
            raise ValueError("invalid target classification")
        if not self.observed_paths or not all(isinstance(path, str) and path for path in self.observed_paths):
            raise ValueError("target requires observed paths")
        if not isinstance(self.existing_writer, str) or not self.existing_writer.strip():
            raise ValueError("invalid existing writer")
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in self.labels.items()):
            raise ValueError("invalid target labels")
        canonical_paths = tuple(os.fspath(canonical_asset_path(path)) for path in self.observed_paths)
        object.__setattr__(self, "observed_paths", canonical_paths)
        object.__setattr__(self, "labels", _freeze_mapping(self.labels))

    @property
    def asset_ids(self) -> tuple[str, ...]:
        return tuple(asset_source_id(path) for path in self.observed_paths)


@dataclass(frozen=True)
class Target:
    """A registered target with authority intentionally fixed to observation."""

    spec: TargetSpec
    authority_mode: AuthorityMode = AuthorityMode.OBSERVE_ONLY

    def __post_init__(self) -> None:
        if self.authority_mode is not AuthorityMode.OBSERVE_ONLY:
            raise ValueError("target authority must be observe_only")

    @property
    def target_id(self) -> str:
        return self.spec.target_id


@dataclass(frozen=True)
class TargetSnapshot:
    target_id: str
    observed_at: datetime
    facts: Mapping[str, Any]
    collector_version: str = "phase2"

    def __post_init__(self) -> None:
        if not isinstance(self.target_id, str) or not _TARGET_ID.fullmatch(self.target_id):
            raise ValueError("invalid target id")
        object.__setattr__(self, "observed_at", _require_aware(self.observed_at))
        if not isinstance(self.collector_version, str) or not self.collector_version:
            raise ValueError("invalid collector version")
        object.__setattr__(self, "facts", _freeze_mapping(self.facts))


@dataclass(frozen=True)
class LogCursor:
    inode: int
    offset: int
    source_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.inode, int) or self.inode < 0:
            raise ValueError("invalid inode")
        if not isinstance(self.offset, int) or self.offset < 0:
            raise ValueError("invalid offset")
        if not isinstance(self.source_id, str) or (self.source_id and not self.source_id.startswith("sha256:")):
            raise ValueError("invalid cursor source")


@dataclass(frozen=True)
class CursorDecision:
    offset: int
    reason: CursorResetReason

    def __post_init__(self) -> None:
        if not isinstance(self.offset, int) or self.offset < 0:
            raise ValueError("invalid cursor offset")


@dataclass(frozen=True)
class RawSignal:
    target_id: str
    collector: str
    signal_type: str
    observed_at: datetime
    payload: Mapping[str, Any]
    severity: str = "info"

    def __post_init__(self) -> None:
        if not isinstance(self.target_id, str) or not _TARGET_ID.fullmatch(self.target_id):
            raise ValueError("invalid target id")
        if not isinstance(self.collector, str) or not _COLLECTOR_NAME.fullmatch(self.collector):
            raise ValueError("invalid collector name")
        if not isinstance(self.signal_type, str) or not _COLLECTOR_NAME.fullmatch(self.signal_type):
            raise ValueError("invalid signal type")
        if not isinstance(self.severity, str) or not self.severity:
            raise ValueError("invalid signal severity")
        object.__setattr__(self, "observed_at", _require_aware(self.observed_at))
        object.__setattr__(self, "payload", _freeze_mapping(self.payload))


@dataclass(frozen=True)
class Signal:
    signal_id: str
    target_id: str
    collector: str
    signal_type: str
    observed_at: datetime
    payload: Mapping[str, Any]
    severity: str = "info"
    redaction_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.signal_id, str) or not self.signal_id.startswith("sha256:"):
            raise ValueError("invalid signal id")
        RawSignal(
            target_id=self.target_id,
            collector=self.collector,
            signal_type=self.signal_type,
            observed_at=self.observed_at,
            payload=self.payload,
            severity=self.severity,
        )
        if not isinstance(self.redaction_version, int) or self.redaction_version < 1:
            raise ValueError("invalid redaction version")
        object.__setattr__(self, "observed_at", _require_aware(self.observed_at))
        object.__setattr__(self, "payload", _freeze_mapping(self.payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "target_id": self.target_id,
            "collector": self.collector,
            "signal_type": self.signal_type,
            "observed_at": self.observed_at.isoformat(),
            "payload": thaw_value(self.payload),
            "severity": self.severity,
            "redaction_version": self.redaction_version,
        }


@dataclass(frozen=True)
class CollectorHealth:
    healthy: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.reason is not None and (not isinstance(self.reason, str) or not self.reason):
            raise ValueError("invalid collector health reason")


@dataclass(frozen=True)
class CollectionBatch:
    target_id: str
    collector: str
    collected_at: datetime
    signals: tuple[Signal, ...]
    health: CollectorHealth
    next_cursor: LogCursor | None = None
    source_id: str = ""
    observation_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self) -> None:
        if not isinstance(self.target_id, str) or not _TARGET_ID.fullmatch(self.target_id):
            raise ValueError("invalid target id")
        if not isinstance(self.collector, str) or not _COLLECTOR_NAME.fullmatch(self.collector):
            raise ValueError("invalid collector name")
        if not all(isinstance(signal, Signal) and signal.target_id == self.target_id for signal in self.signals):
            raise ValueError("invalid batch signals")
        if not isinstance(self.health, CollectorHealth):
            raise ValueError("invalid collector health")
        if self.next_cursor is not None and not isinstance(self.next_cursor, LogCursor):
            raise ValueError("invalid batch cursor")
        if not isinstance(self.source_id, str) or (self.source_id and not self.source_id.startswith("sha256:")):
            raise ValueError("invalid source id")
        if self.next_cursor is not None and self.source_id and self.next_cursor.source_id != self.source_id:
            raise ValueError("cursor source does not match batch source")
        if not isinstance(self.observation_id, str) or not _OBSERVATION_ID.fullmatch(self.observation_id):
            raise ValueError("invalid observation id")
        object.__setattr__(self, "collected_at", _require_aware(self.collected_at))
        object.__setattr__(self, "signals", tuple(self.signals))


@dataclass(frozen=True)
class CronExecution:
    job_id: str
    observed_at: datetime
    exit_code: int | None
    completed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, str) or not self.job_id:
            raise ValueError("invalid cron job id")
        if self.exit_code is not None and not isinstance(self.exit_code, int):
            raise ValueError("invalid cron exit code")
        object.__setattr__(self, "observed_at", _require_aware(self.observed_at))


@dataclass(frozen=True)
class BusinessAssertion:
    name: str
    passed: bool
    evidence: Mapping[str, Any] = field(default_factory=dict)
    observed_at: datetime | None = None
    max_age_seconds: int = 300
    mandatory: bool = True
    severity: str = "warning"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("invalid business assertion")
        if not isinstance(self.passed, bool) or not isinstance(self.mandatory, bool):
            raise ValueError("invalid business assertion state")
        if not isinstance(self.max_age_seconds, int) or self.max_age_seconds <= 0:
            raise ValueError("invalid business assertion freshness")
        if not isinstance(self.severity, str) or not self.severity:
            raise ValueError("invalid assertion severity")
        if self.observed_at is not None:
            object.__setattr__(self, "observed_at", _require_aware(self.observed_at))
        object.__setattr__(self, "evidence", _freeze_mapping(self.evidence))


@dataclass(frozen=True)
class CronObservation:
    """A detached read-only Cron status record, not an executable callback."""

    execution: CronExecution
    assertions: tuple[BusinessAssertion, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.execution, CronExecution) or not all(
            isinstance(assertion, BusinessAssertion) for assertion in self.assertions
        ):
            raise ValueError("invalid cron observation")
        object.__setattr__(self, "assertions", tuple(self.assertions))


@dataclass(frozen=True)
class FleetCoverage:
    registered_targets: int
    snapshotted_targets: int
    coverage_percent: int


def stable_signal_id(
    *, target_id: str, collector: str, signal_type: str, payload: Mapping[str, Any]
) -> str:
    """Identity evidence without source path or observation time, for deduplication."""
    data = {
        "target_id": target_id,
        "collector": collector,
        "signal_type": signal_type,
        "payload": thaw_value(payload),
    }
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
