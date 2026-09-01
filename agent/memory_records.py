from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any


class MemoryClass(Enum):
    U = "U"
    A = "A"
    E = "E"
    P = "P"
    T = "T"
    R = "R"
    C = "C"


class LifecycleStatus(Enum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    REJECTED = "rejected"


_SOURCE_TYPES = frozenset({"direct_user", "session_history", "provider", "model"})
_SOURCE_ACTORS = {
    "direct_user": frozenset({"user"}),
    "session_history": frozenset({"user", "assistant", "system"}),
    "provider": frozenset({"provider", "model"}),
    "model": frozenset({"model"}),
}


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be a nonblank string")
    return value


def _integer(value: object, name: str, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _timestamp(value: object, name: str) -> str:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, member in value.items():
            _text(key, "content mapping key")
            frozen[key] = _freeze(member)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(member) for member in value)
    if type(value) is str:
        return _text(value, "content string")
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("content floats must be finite")
        return value
    if value is None or type(value) in (int, bool):
        return value
    raise TypeError(f"unsupported content value: {type(value).__name__}")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(value[key]) for key in sorted(value)}
    if isinstance(value, tuple):
        return [_plain(member) for member in value]
    return value


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(_plain(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class Scope:
    profile_id: str
    namespace: str
    project_id: str

    def __post_init__(self) -> None:
        _text(self.profile_id, "profile_id")
        _text(self.namespace, "namespace")
        _text(self.project_id, "project_id")

    def to_dict(self) -> dict[str, str]:
        return {"profile_id": self.profile_id, "namespace": self.namespace, "project_id": self.project_id}


@dataclass(frozen=True)
class Provenance:
    source_type: str
    source_id: str
    actor: str
    derived: bool = False

    def __post_init__(self) -> None:
        _text(self.source_type, "source_type")
        if self.source_type not in _SOURCE_TYPES:
            raise ValueError("source_type is not supported")
        _text(self.source_id, "source_id")
        _text(self.actor, "actor")
        if self.actor not in _SOURCE_ACTORS[self.source_type]:
            raise ValueError("actor is not valid for source_type")
        if type(self.derived) is not bool:
            raise TypeError("derived must be bool")
        if self.source_type == "direct_user" and self.derived:
            raise ValueError("direct_user provenance cannot be derived")
        if self.source_type in {"provider", "model"} and not self.derived:
            raise ValueError(f"{self.source_type} provenance must be derived")

    def to_dict(self) -> dict[str, Any]:
        return {"source_type": self.source_type, "source_id": self.source_id, "actor": self.actor, "derived": self.derived}


@dataclass(frozen=True)
class Lifecycle:
    status: LifecycleStatus
    observed_at: str
    effective_at: str | None = None
    expires_at: str | None = None
    supersedes_record_id: str | None = None

    def __post_init__(self) -> None:
        status = self.status
        if type(status) is str:
            try:
                status = LifecycleStatus(status)
            except ValueError as exc:
                raise ValueError("status is not supported") from exc
            object.__setattr__(self, "status", status)
        elif type(status) is not LifecycleStatus:
            raise TypeError("status must be LifecycleStatus")

        observed = _timestamp(self.observed_at, "observed_at")
        object.__setattr__(self, "observed_at", observed)
        effective = None
        if self.effective_at is not None:
            effective = _timestamp(self.effective_at, "effective_at")
            object.__setattr__(self, "effective_at", effective)
        if status is LifecycleStatus.PROPOSED and effective is not None:
            raise ValueError("effective_at is forbidden while proposed")
        if status is not LifecycleStatus.PROPOSED and effective is None:
            raise ValueError("effective_at is required after proposal")
        if effective is not None and effective < observed:
            raise ValueError("effective_at cannot precede observed_at")
        if self.expires_at is not None:
            expires = _timestamp(self.expires_at, "expires_at")
            if expires < observed:
                raise ValueError("expires_at cannot precede observed_at")
            object.__setattr__(self, "expires_at", expires)
        else:
            expires = None
        if status is LifecycleStatus.EXPIRED and expires is None:
            raise ValueError("expires_at is required for expired lifecycle")
        if status is LifecycleStatus.EXPIRED and effective is not None and expires is not None and effective < expires:
            raise ValueError("effective_at cannot precede expires_at for expired lifecycle")
        if self.supersedes_record_id is not None:
            _text(self.supersedes_record_id, "supersedes_record_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "observed_at": self.observed_at,
            "effective_at": self.effective_at,
            "expires_at": self.expires_at,
            "supersedes_record_id": self.supersedes_record_id,
        }


@dataclass(frozen=True)
class Candidate:
    memory_class: MemoryClass
    scope: Scope
    provenance: Provenance
    lifecycle: Lifecycle
    content: Mapping[str, Any]
    confidence: float

    def __post_init__(self) -> None:
        if type(self.memory_class) is not MemoryClass:
            raise TypeError("memory_class must be MemoryClass")
        if type(self.scope) is not Scope or type(self.provenance) is not Provenance or type(self.lifecycle) is not Lifecycle:
            raise TypeError("candidate envelope fields have invalid types")
        if self.lifecycle.status is not LifecycleStatus.PROPOSED:
            raise ValueError("candidate lifecycle must be proposed")
        if not isinstance(self.content, Mapping):
            raise TypeError("content must be a mapping")
        if type(self.confidence) not in (int, float) or isinstance(self.confidence, bool) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between zero and one")
        if self.memory_class is MemoryClass.T and self.lifecycle.expires_at is None:
            raise ValueError("temporary memory requires expires_at")
        object.__setattr__(self, "content", _freeze(self.content))
        object.__setattr__(self, "confidence", float(self.confidence))

    def to_dict(self) -> dict[str, Any]:
        return {"memory_class": self.memory_class.value, "scope": self.scope.to_dict(), "provenance": self.provenance.to_dict(), "lifecycle": self.lifecycle.to_dict(), "content": _plain(self.content), "confidence": self.confidence}

    def content_hash(self) -> str:
        return _digest(self.to_dict())


@dataclass(frozen=True)
class ReviewPacket:
    packet_id: str
    scope: Scope
    candidate_ids: tuple[str, ...]
    base_version: int

    def __post_init__(self) -> None:
        _text(self.packet_id, "packet_id")
        if type(self.scope) is not Scope:
            raise TypeError("scope must be Scope")
        if type(self.candidate_ids) is not tuple or not self.candidate_ids:
            raise TypeError("candidate_ids must be a nonempty tuple")
        for candidate_id in self.candidate_ids:
            _text(candidate_id, "candidate_ids")
        _integer(self.base_version, "base_version", 0)

    def to_dict(self) -> dict[str, Any]:
        return {"packet_id": self.packet_id, "scope": self.scope.to_dict(), "candidate_ids": list(self.candidate_ids), "base_version": self.base_version}

    def content_hash(self) -> str:
        return _digest(self.to_dict())


@dataclass(frozen=True)
class Approval:
    packet_id: str
    approver: str
    exact: bool

    def __post_init__(self) -> None:
        _text(self.packet_id, "packet_id")
        _text(self.approver, "approver")
        if type(self.exact) is not bool:
            raise TypeError("exact must be bool")

    def to_dict(self) -> dict[str, Any]:
        return {"packet_id": self.packet_id, "approver": self.approver, "exact": self.exact}

    def content_hash(self) -> str:
        return _digest(self.to_dict())


@dataclass(frozen=True)
class CanonicalRecord:
    record_id: str
    version: int
    memory_class: MemoryClass
    scope: Scope
    provenance: Provenance
    lifecycle: Lifecycle
    content: Mapping[str, Any]

    def __post_init__(self) -> None:
        _text(self.record_id, "record_id")
        _integer(self.version, "version", 1)
        if type(self.memory_class) is not MemoryClass or type(self.scope) is not Scope or type(self.provenance) is not Provenance or type(self.lifecycle) is not Lifecycle:
            raise TypeError("record envelope fields have invalid types")
        if self.lifecycle.status is LifecycleStatus.PROPOSED:
            raise ValueError("canonical record lifecycle cannot be proposed")
        if not isinstance(self.content, Mapping):
            raise TypeError("content must be a mapping")
        object.__setattr__(self, "content", _freeze(self.content))

    def to_dict(self) -> dict[str, Any]:
        return {"record_id": self.record_id, "version": self.version, "memory_class": self.memory_class.value, "scope": self.scope.to_dict(), "provenance": self.provenance.to_dict(), "lifecycle": self.lifecycle.to_dict(), "content": _plain(self.content)}

    def content_hash(self) -> str:
        return _digest(self.to_dict())


@dataclass(frozen=True)
class TransitionReceipt:
    receipt_id: str
    scope: Scope
    record_id: str
    from_version: int
    to_version: int
    transition: str
    approval_hash: str

    def __post_init__(self) -> None:
        _text(self.receipt_id, "receipt_id")
        if type(self.scope) is not Scope:
            raise TypeError("scope must be Scope")
        _text(self.record_id, "record_id")
        _text(self.transition, "transition")
        _text(self.approval_hash, "approval_hash")
        _integer(self.from_version, "from_version", 0)
        _integer(self.to_version, "to_version", 1)
        if self.to_version != self.from_version + 1:
            raise ValueError("receipt versions must be consecutive")

    def to_dict(self) -> dict[str, Any]:
        return {"receipt_id": self.receipt_id, "scope": self.scope.to_dict(), "record_id": self.record_id, "from_version": self.from_version, "to_version": self.to_version, "transition": self.transition, "approval_hash": self.approval_hash}

    def content_hash(self) -> str:
        return _digest(self.to_dict())
