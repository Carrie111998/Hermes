"""Bounded, zero-persistence observation for controlled Persona provider calls.

This module is deliberately transport-agnostic.  H6-13B exercises it only with
in-memory transports; a later Owner-authorized phase may supply one real
transport implementation.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Callable, Iterable, Mapping, Protocol, Sequence, TextIO


CHECKPOINTS = (
    "H13B_PRE_AUDIT_STARTED", "H13B_PRE_AUDIT_COMPLETE",
    "H13B_REQUEST_READY", "H13B_REQUEST_STARTED", "H13B_RESPONSE_HEADERS",
    "H13B_FIRST_BYTE", "H13B_RESPONSE_COMPLETE", "H13B_RESPONSE_PARSED",
    "H13B_PERSONA_VALIDATION_STARTED", "H13B_PERSONA_VALIDATION_COMPLETE",
    "H13B_POST_AUDIT_STARTED", "H13B_POST_AUDIT_COMPLETE", "H13B_COMPLETE",
)
_SECRET = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{8,}|authorization\s*:|bearer\s+[A-Za-z0-9._-]{8,}|"
    r"api[_ -]?key\s*[:=]|token\s*[:=]\s*\S+)", re.I,
)
_SAFE_ERRORS = {
    "CONNECT_OR_PRE_HEADER_TIMEOUT", "HEADER_RECEIVED_BODY_TIMEOUT",
    "FIRST_BYTE_RECEIVED_BODY_TIMEOUT", "PROCESSING_TIMEOUT", "AUDIT_TIMEOUT",
    "OUTER_TIMEOUT", "HTTP_400", "HTTP_401", "HTTP_404", "HTTP_429",
    "HTTP_500", "MALFORMED_RESPONSE", "MISSING_MODEL_IDENTITY",
    "PERSONA_VALIDATION_FAILED", "UNEXPECTED_WRITE", "TRANSPORT_ERROR",
}


class ObservationError(RuntimeError):
    def __init__(self, stage: str, safe_class: str):
        if safe_class not in _SAFE_ERRORS:
            safe_class = "TRANSPORT_ERROR"
        super().__init__(f"{stage}:{safe_class}")
        self.stage, self.safe_class = stage, safe_class


class TransportFailure(Exception):
    """A fake or future real transport's already-sanitized failure."""
    def __init__(self, safe_class: str):
        super().__init__(safe_class)
        self.safe_class = safe_class


@dataclass(frozen=True)
class AuditTarget:
    name: str
    path: Path
    hash_file: bool = True
    list_children: bool = False
    hash_children: bool = False


@dataclass(frozen=True)
class TargetState:
    exists: bool
    kind: str = "missing"
    size: int = 0
    mtime_ns: int = 0
    digest: str = ""
    children: tuple[str, ...] = ()


def default_audit_targets(hermes_home: Path) -> tuple[AuditTarget, ...]:
    """Known persistence surfaces only; directory reads are non-recursive."""
    h = Path(hermes_home)
    return (
        AuditTarget("session_db", h / "state.db"),
        AuditTarget("session_db_wal", h / "state.db-wal"),
        AuditTarget("session_db_shm", h / "state.db-shm"),
        AuditTarget("sessions_and_request_dumps", h / "sessions", False, True, True),
        AuditTarget("logs", h / "logs", False, True, True),
        AuditTarget("soul", h / "SOUL.md"),
        AuditTarget("tool_discovery_cache", h / "cache" / "tool_discovery_cache.json"),
        AuditTarget("delegation_state", h / "cache" / "delegation", False, True, True),
        AuditTarget("provider_metadata_cache", h / "provider_models_cache.json"),
        AuditTarget("model_metadata_cache", h / "models_dev_cache.json"),
        AuditTarget("growth_parent", h / "persona_growth", False, True),
        AuditTarget("police_growth", h / "persona_growth" / "police_horitius" / "records.json"),
        AuditTarget("knowledge_parent", h / "persona_knowledge", False, True),
        AuditTarget("police_knowledge", h / "persona_knowledge" / "police_horitius" / "controlled.json"),
        AuditTarget("police_knowledge_candidates", h / "persona_knowledge" / "police_horitius" / "candidates.json"),
        AuditTarget("police_knowledge_decisions", h / "persona_knowledge" / "police_horitius" / "owner_decisions.json"),
        AuditTarget("config", h / "config.yaml"),
        AuditTarget("env_metadata", h / ".env", False),
        AuditTarget("auth_metadata", h / "auth.json", False),
        AuditTarget("sessions_parent", h, False, True),
    )


class BoundedAuditor:
    def __init__(self, targets: Sequence[AuditTarget], *, max_hash_bytes: int = 1_048_576):
        self.targets = tuple(targets)
        self.max_hash_bytes = max_hash_bytes

    def snapshot(self) -> Mapping[str, TargetState]:
        result: dict[str, TargetState] = {}
        for target in self.targets:
            p = target.path
            if not p.exists():
                result[target.name] = TargetState(False)
                continue
            stat = p.stat()
            if p.is_dir():
                children = ()
                if target.list_children:
                    direct = []
                    for child in p.iterdir():
                        child_stat = child.stat()
                        child_hash = ""
                        if (target.hash_children and child.is_file()
                                and child_stat.st_size <= self.max_hash_bytes):
                            child_hash = hashlib.sha256(child.read_bytes()).hexdigest()
                        direct.append(
                            f"{child.name}|{'d' if child.is_dir() else 'f'}|"
                            f"{child_stat.st_size}|{child_stat.st_mtime_ns}|{child_hash}"
                        )
                    children = tuple(sorted(direct))
                result[target.name] = TargetState(True, "directory", 0, stat.st_mtime_ns, "", children)
            else:
                digest = ""
                if target.hash_file and stat.st_size <= self.max_hash_bytes:
                    digest = hashlib.sha256(p.read_bytes()).hexdigest()
                result[target.name] = TargetState(True, "file", stat.st_size, stat.st_mtime_ns, digest)
        return result

    @staticmethod
    def changed(before: Mapping[str, TargetState], after: Mapping[str, TargetState]) -> tuple[str, ...]:
        return tuple(sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k)))


@dataclass(frozen=True)
class ResponseHeaders:
    status: int | None
    routing_provider: str = ""


class ObservationTransport(Protocol):
    is_fake: bool
    def open(self, requested_model: str, provider_timeout: float) -> tuple[ResponseHeaders, Iterable[bytes]]: ...


@dataclass(frozen=True)
class PersonaExpectation:
    facts: tuple[str, ...]
    observations: tuple[str, ...]
    unknowns: tuple[str, ...]


@dataclass(frozen=True)
class PersonaClassification:
    fact_preservation: str
    observation_preservation: str
    unknown_preservation: str
    unsupported_inference: str
    authority_escalation: str
    role_violation: str

    @property
    def passed(self) -> bool:
        return self == PersonaClassification("PASS", "PASS", "PASS", "0", "0", "0")


def validate_persona(payload: Mapping[str, object], expected: PersonaExpectation) -> PersonaClassification:
    def strings(key: str) -> tuple[str, ...]:
        value = payload.get(key, ())
        return tuple(value) if isinstance(value, list) and all(isinstance(x, str) for x in value) else ()
    facts, observations, unknowns = strings("facts"), strings("observations"), strings("unknowns")
    unsupported = strings("unsupported_inferences")
    authority = strings("authority_escalations")
    roles = strings("role_violations")
    return PersonaClassification(
        "PASS" if facts == expected.facts else "FAIL",
        "PASS" if observations == expected.observations else "FAIL",
        "PASS" if unknowns == expected.unknowns else "FAIL",
        "0" if not unsupported else "detected",
        "0" if not authority else "detected",
        "0" if not roles else "detected",
    )


@dataclass(frozen=True)
class TimeoutBudget:
    pre_audit_max: float = 1.0
    provider_timeout: float = 1.0
    processing_timeout: float = 1.0
    post_audit_max: float = 1.0
    termination_margin: float = 1.0
    outer_timeout: float = 6.0

    def valid(self) -> bool:
        return self.outer_timeout > sum((self.pre_audit_max, self.provider_timeout,
                                         self.processing_timeout, self.post_audit_max,
                                         self.termination_margin))


@dataclass
class ObservationResult:
    checkpoints: list[str] = field(default_factory=list)
    provider_request_budget: int = 0
    http_attempt_count: int = 0
    fake_transport_attempt_count: int = 0
    retry_count: int = 0
    fallback_count: int = 0
    http_status: int | None = None
    requested_model: str = ""
    response_model: str = "UNKNOWN"
    routing_provider: str = "UNKNOWN"
    response_state: str = "PRE_DISPATCH"
    parse_result: str = "NOT_OBSERVED"
    persona_validation: PersonaClassification | None = None
    filesystem_delta: tuple[str, ...] = ()
    durations: dict[str, float] = field(default_factory=dict)
    failure_stage: str = ""
    safe_error_class: str = ""


class CheckpointEmitter:
    def __init__(self, stream: TextIO, result: ObservationResult):
        self.stream, self.result = stream, result
        self.terminal = False

    def emit(self, value: str, *, terminal: bool = False) -> None:
        if self.terminal:
            return
        if _SECRET.search(value):
            raise ObservationError("OUTPUT", "TRANSPORT_ERROR")
        print(value, file=self.stream, flush=True)
        self.result.checkpoints.append(value)
        if terminal:
            self.terminal = True


class ProviderObservationHarness:
    def __init__(self, *, auditor: BoundedAuditor, stream: TextIO, budget: TimeoutBudget,
                 clock: Callable[[], float] = time.monotonic,
                 parser: Callable[[bytes], object] = json.loads):
        self.auditor, self.stream, self.budget, self.clock = auditor, stream, budget, clock
        self.parser = parser

    def run(self, *, transport: ObservationTransport, requested_model: str,
            expectation: PersonaExpectation, provider_request_budget: int = 0) -> ObservationResult:
        result = ObservationResult(provider_request_budget=provider_request_budget,
                                   requested_model=requested_model)
        emit = CheckpointEmitter(self.stream, result)
        started = self.clock()
        before: Mapping[str, TargetState] = {}
        try:
            if not self.budget.valid():
                raise ObservationError("OUTER", "OUTER_TIMEOUT")
            emit.emit("H13B_PRE_AUDIT_STARTED")
            t = self.clock(); before = self.auditor.snapshot(); result.durations["pre_audit"] = self.clock() - t
            if result.durations["pre_audit"] > self.budget.pre_audit_max:
                raise ObservationError("AUDIT", "AUDIT_TIMEOUT")
            emit.emit("H13B_PRE_AUDIT_COMPLETE")
            emit.emit("H13B_REQUEST_READY")
            emit.emit("H13B_REQUEST_STARTED")
            if transport.is_fake:
                result.fake_transport_attempt_count += 1
            else:
                if provider_request_budget < 1 or result.http_attempt_count >= provider_request_budget:
                    raise ObservationError("HTTP", "TRANSPORT_ERROR")
                result.http_attempt_count += 1
            t = self.clock()
            try:
                headers, chunks = transport.open(requested_model, self.budget.provider_timeout)
                result.http_status = headers.status
                result.routing_provider = headers.routing_provider or "UNKNOWN"
                emit.emit("H13B_RESPONSE_HEADERS"); result.response_state = "RESPONSE_HEADERS"
                if headers.status is not None and headers.status >= 400:
                    safe = f"HTTP_{headers.status}" if f"HTTP_{headers.status}" in _SAFE_ERRORS else "TRANSPORT_ERROR"
                    raise ObservationError("HTTP", safe)
                body = bytearray()
                first = True
                for chunk in chunks:
                    if first:
                        emit.emit("H13B_FIRST_BYTE"); result.response_state = "FIRST_BYTE"; first = False
                    body.extend(chunk)
                    if self.clock() - t > self.budget.provider_timeout:
                        cls = "FIRST_BYTE_RECEIVED_BODY_TIMEOUT" if not first else "HEADER_RECEIVED_BODY_TIMEOUT"
                        raise ObservationError("HTTP", cls)
                if first:
                    emit.emit("H13B_FIRST_BYTE"); result.response_state = "FIRST_BYTE"
                emit.emit("H13B_RESPONSE_COMPLETE"); result.response_state = "RESPONSE_COMPLETE"
                result.durations["fake_transport" if transport.is_fake else "provider"] = self.clock() - t
            except TransportFailure as exc:
                raise ObservationError("HTTP", exc.safe_class) from None
            t = self.clock()
            try:
                payload = self.parser(bytes(body))
                if not isinstance(payload, dict):
                    raise ValueError
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                raise ObservationError("PARSE", "MALFORMED_RESPONSE") from None
            if self.clock() - t > self.budget.processing_timeout:
                raise ObservationError("PARSE", "PROCESSING_TIMEOUT")
            result.parse_result = "PASS"; emit.emit("H13B_RESPONSE_PARSED")
            model = payload.get("model")
            result.response_model = model if isinstance(model, str) and model else "UNKNOWN"
            if result.response_model == "UNKNOWN":
                raise ObservationError("PARSE", "MISSING_MODEL_IDENTITY")
            emit.emit("H13B_PERSONA_VALIDATION_STARTED")
            result.persona_validation = validate_persona(payload, expectation)
            if not result.persona_validation.passed:
                raise ObservationError("PERSONA", "PERSONA_VALIDATION_FAILED")
            emit.emit("H13B_PERSONA_VALIDATION_COMPLETE")
            result.durations["processing"] = self.clock() - t
            emit.emit("H13B_POST_AUDIT_STARTED")
            t = self.clock(); after = self.auditor.snapshot(); result.durations["post_audit"] = self.clock() - t
            if result.durations["post_audit"] > self.budget.post_audit_max:
                raise ObservationError("AUDIT", "AUDIT_TIMEOUT")
            result.filesystem_delta = self.auditor.changed(before, after)
            if result.filesystem_delta:
                raise ObservationError("AUDIT", "UNEXPECTED_WRITE")
            emit.emit("H13B_POST_AUDIT_COMPLETE")
            if self.clock() - started > self.budget.outer_timeout:
                raise ObservationError("OUTER", "OUTER_TIMEOUT")
            emit.emit("H13B_COMPLETE", terminal=True)
        except ObservationError as exc:
            result.failure_stage, result.safe_error_class = exc.stage, exc.safe_class
            emit.emit(f"H13B_FAILED:{exc.stage}:{exc.safe_class}", terminal=True)
        return result


def duration_summary(values: Sequence[float]) -> Mapping[str, float]:
    if not values:
        return {"min": 0.0, "median": 0.0, "max": 0.0}
    return {"min": min(values), "median": median(values), "max": max(values)}
