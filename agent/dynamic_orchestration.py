"""Pure domain contracts for proposed dynamic multimodel orchestration.

This module intentionally has no runtime wiring, provider calls, persistence, or
credential-pool mutation. It establishes deterministic contracts that later
shadow-mode integration can consume.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import unicodedata
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

ROUTE_CANONICALIZATION_VERSION = "route-v1"
_ROUTE_FIELDS = (
    "canonicalization_version", "provider", "product", "surface", "account_id",
    "entitlement_id", "billing_pool_id", "quota_pool_id", "model", "endpoint",
    "variant", "region",
)
_REQUIRED_ROUTE_FIELDS = frozenset({
    "provider", "product", "surface", "account_id", "billing_pool_id",
    "quota_pool_id", "model", "endpoint", "region",
})
_SENSITIVE_TERMS = ("secret", "token", "credential", "authorization", "prompt", "raw_body", "raw_provider")


class DomainValidationError(ValueError):
    """A stable, public validation error safe for deterministic tests and audit."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class TaskState(str, Enum):
    NEW = "NEW"; PLANNED = "PLANNED"; ROUTED = "ROUTED"; DISPATCHED = "DISPATCHED"; VERIFYING = "VERIFYING"; COMPLETED = "COMPLETED"; FAILED = "FAILED"; WAITING_FOR_CAPACITY = "WAITING_FOR_CAPACITY"; CANCELLED = "CANCELLED"


class AttemptState(str, Enum):
    CREATED = "CREATED"; RESERVED = "RESERVED"; DISPATCHED = "DISPATCHED"; RUNNING = "RUNNING"; RESULT_RECORDED = "RESULT_RECORDED"; VERIFIED = "VERIFIED"; FAILED = "FAILED"; CANCELLED = "CANCELLED"


class RouteState(str, Enum):
    DISCOVERED = "DISCOVERED"; ELIGIBLE = "ELIGIBLE"; INELIGIBLE = "INELIGIBLE"; COOLDOWN = "COOLDOWN"; BREAKER_OPEN = "BREAKER_OPEN"; RETIRED = "RETIRED"


class CredentialState(str, Enum):
    AVAILABLE = "AVAILABLE"; EXHAUSTED = "EXHAUSTED"; COOLDOWN = "COOLDOWN"; DEAD = "DEAD"


class ReservationState(str, Enum):
    PENDING = "PENDING"; HELD = "HELD"; CONSUMED = "CONSUMED"; RELEASED = "RELEASED"; EXPIRED = "EXPIRED"; RECONCILED = "RECONCILED"


class ReviewState(str, Enum):
    NOT_REQUIRED = "NOT_REQUIRED"; PENDING = "PENDING"; IN_PROGRESS = "IN_PROGRESS"; PASSED = "PASSED"; FAILED = "FAILED"; HUMAN_APPROVED = "HUMAN_APPROVED"; HUMAN_REJECTED = "HUMAN_REJECTED"


class ErrorKind(str, Enum):
    CAPACITY_EXHAUSTED = "capacity_exhausted"


class DecisionRelation(str, Enum):
    INITIAL = "INITIAL"; FALLBACK = "FALLBACK"; REPLAN = "REPLAN"; WAITING = "WAITING"


def _normalized_text(value: object, *, required: bool = False, field_name: str = "value") -> str | None:
    if value is None:
        if required:
            raise DomainValidationError("route.identity_required", f"{field_name} is required")
        return None
    if not isinstance(value, str):
        raise DomainValidationError("route.value_invalid", f"{field_name} must be a string")
    normalized = unicodedata.normalize("NFC", value.strip(" \t\r\n\f\v")).casefold()
    if not normalized:
        if required:
            raise DomainValidationError("route.identity_required", f"{field_name} is required")
        return None
    return normalized


def _normalize_endpoint(value: object) -> str:
    raw = _normalized_text(value, required=True, field_name="endpoint")
    assert raw is not None
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise DomainValidationError("route.endpoint_invalid", "endpoint must be an absolute http(s) URL without credentials, query or fragment")
    host = parsed.hostname.casefold()
    port = parsed.port
    netloc = host if port is None or (parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80) else f"{host}:{port}"
    path = re.sub(r"/+$", "", posixpath.normpath(parsed.path or "/"))
    if not path.startswith("/"):
        path = "/" + path
    if path == "/.":
        path = "/"
    return urlunsplit((parsed.scheme.casefold(), netloc, path, "", ""))


@dataclass(frozen=True)
class RouteV1:
    provider: str
    product: str
    surface: str
    account_id: str
    billing_pool_id: str
    quota_pool_id: str
    model: str
    endpoint: str
    region: str
    entitlement_id: str | None = None
    variant: str | None = None
    canonicalization_version: str = ROUTE_CANONICALIZATION_VERSION

    def __post_init__(self) -> None:
        for name in _REQUIRED_ROUTE_FIELDS:
            value = getattr(self, name)
            normalized = _normalize_endpoint(value) if name == "endpoint" else _normalized_text(value, required=True, field_name=name)
            object.__setattr__(self, name, normalized)
        for name in ("entitlement_id", "variant"):
            object.__setattr__(self, name, _normalized_text(getattr(self, name), field_name=name))
        if self.canonicalization_version != ROUTE_CANONICALIZATION_VERSION:
            raise DomainValidationError("route.canonicalization_unknown", "canonicalization_version must be route-v1")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "RouteV1":
        _reject_sensitive(payload, "route")
        return cls(**{key: payload.get(key) for key in _ROUTE_FIELDS if key != "canonicalization_version"})

    def canonical_object(self) -> dict[str, str | None]:
        return {name: getattr(self, name) for name in _ROUTE_FIELDS}

    @property
    def canonical_json(self) -> str:
        return json.dumps(self.canonical_object(), ensure_ascii=False, separators=(",", ":"))

    @property
    def route_id(self) -> str:
        digest = hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()
        return f"{ROUTE_CANONICALIZATION_VERSION}:{digest}"


@dataclass(frozen=True)
class AuditedModelJustification:
    policy_version: str
    reason: str
    evidence_refs: tuple[str, ...]
    author: str
    expires_at: str

    def __post_init__(self) -> None:
        if not all((self.policy_version, self.reason, self.evidence_refs, self.author, self.expires_at)):
            raise DomainValidationError("task.justification_invalid", "audited model justification requires policy, reason, evidence, author and expiry")


@dataclass(frozen=True)
class TaskEnvelope:
    task_id: str
    objective: str
    deliverables: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    permissions: tuple[str, ...]
    context_limit: int
    privacy_classification: str
    risk_level: str
    effort: str
    budget: str
    verification_level: str
    policy_version: str
    audited_model_justification: AuditedModelJustification | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "TaskEnvelope":
        _reject_sensitive(payload, "task")
        forbidden = {key for key in payload if key in {"model", "provider", "route_id", "selected_route_id"}}
        justification = payload.get("audited_model_justification")
        if forbidden and not isinstance(justification, Mapping):
            raise DomainValidationError("task.unaudited_model_identity", "model/provider/route identity requires audited_model_justification")
        if isinstance(justification, Mapping):
            payload = dict(payload)
            payload["audited_model_justification"] = AuditedModelJustification(
                policy_version=str(justification.get("policy_version", "")), reason=str(justification.get("reason", "")),
                evidence_refs=tuple(justification.get("evidence_refs", ())), author=str(justification.get("author", "")), expires_at=str(justification.get("expires_at", "")),
            )
        allowed = {field.name for field in fields(cls)}
        return cls(**{key: value for key, value in payload.items() if key in allowed})


@dataclass(frozen=True)
class RuntimeErrorClassificationV1:
    kind: ErrorKind
    attempted_route_id: str
    quota_pool_id: str
    billing_pool_id: str | None = None
    evidence_code: str | None = None

    def __post_init__(self) -> None:
        if self.kind is not ErrorKind.CAPACITY_EXHAUSTED or not self.attempted_route_id or not self.quota_pool_id:
            raise DomainValidationError("classification.capacity_scope_required", "capacity exhaustion requires attempted_route_id and quota_pool_id")


@dataclass(frozen=True)
class CandidateEvaluation:
    route: RouteV1
    eligible: bool
    rejection_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _reject_sensitive({"rejection_codes": self.rejection_codes}, "candidate")


@dataclass(frozen=True)
class RouteDecisionV1:
    decision_id: str
    task_id: str
    attempt_id: str
    relation: DecisionRelation
    candidates: tuple[CandidateEvaluation, ...]
    selected_route_id: str | None
    trigger: RuntimeErrorClassificationV1
    reason_codes: tuple[str, ...]
    quality_compensation: tuple[str, ...] = ()
    recheck_evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _reject_sensitive(
            {
                "reason_codes": self.reason_codes,
                "quality_compensation": self.quality_compensation,
                "recheck_evidence": self.recheck_evidence,
            },
            "decision",
        )
        if self.relation in {DecisionRelation.FALLBACK, DecisionRelation.REPLAN} and (not self.selected_route_id or not self.quality_compensation):
            raise DomainValidationError("decision.quality_compensation_required", "fallback/replan requires selected route and quality compensation")
        if self.relation is DecisionRelation.WAITING and not self.recheck_evidence:
            raise DomainValidationError("replan.recheck_evidence_required", "waiting requires recheck evidence")


def replan_after_capacity_exhaustion(*, task_id: str, attempt_id: str, failed_route: RouteV1, classification: RuntimeErrorClassificationV1, candidates: Sequence[CandidateEvaluation], decision_id: str, quality_compensation: Sequence[str] = (), recheck_evidence: Sequence[str] = ()) -> RouteDecisionV1:
    if classification.attempted_route_id != failed_route.route_id or classification.quota_pool_id != failed_route.quota_pool_id:
        raise DomainValidationError("classification.capacity_scope_required", "classification must match attempted route and quota pool")
    eligible = [
        candidate
        for candidate in candidates
        if candidate.eligible and candidate.route.quota_pool_id != failed_route.quota_pool_id
    ]
    if eligible:
        return RouteDecisionV1(decision_id, task_id, attempt_id, DecisionRelation.FALLBACK, tuple(candidates), eligible[0].route.route_id, classification, ("route_capacity_exhausted",), tuple(quality_compensation))
    return RouteDecisionV1(decision_id, task_id, attempt_id, DecisionRelation.WAITING, tuple(candidates), None, classification, ("no_eligible_routes",), (), tuple(recheck_evidence))


def _reject_sensitive(value: object, location: str) -> None:
    def walk(item: object, key: str = "") -> None:
        lowered = key.casefold()
        if any(term in lowered for term in _SENSITIVE_TERMS):
            raise DomainValidationError("decision.sensitive_field_prohibited", f"sensitive field prohibited in {location}")
        if isinstance(item, Mapping):
            for child_key, child_value in item.items():
                walk(child_value, str(child_key))
        elif isinstance(item, (list, tuple, set)):
            for child in item:
                walk(child, key)
        elif isinstance(item, str) and re.search(r"(?:^|\s)(?:bearer\s+|sk-|gho_)", item, re.IGNORECASE):
            raise DomainValidationError("decision.sensitive_field_prohibited", f"sensitive value prohibited in {location}")
    walk(value)
