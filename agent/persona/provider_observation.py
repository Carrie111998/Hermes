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
import unicodedata
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
    "STRUCTURAL_RESPONSE_ERROR",
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


class ResponseStructureError(ValueError):
    """The HTTP JSON decoded, but is not a supported provider response."""


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
    hypotheses: tuple[str, ...] = ()
    forbidden_inferences: tuple[str, ...] = ()
    authority_markers: tuple[str, ...] = ()
    role_markers: tuple[str, ...] = ()


@dataclass(frozen=True)
class PersonaClassification:
    fact_preservation: str
    observation_preservation: str
    unknown_preservation: str
    unsupported_inference: str
    authority_escalation: str
    role_violation: str
    hypothesis_preservation: str = "NOT_OBSERVED"
    unknown_ledger: "UnknownValidationResult | None" = None

    @property
    def passed(self) -> bool:
        return (
            self.fact_preservation == self.observation_preservation
            == self.unknown_preservation == "PASS"
            and self.hypothesis_preservation in {"PASS", "NOT_OBSERVED"}
            and self.unsupported_inference == self.authority_escalation
            == self.role_violation == "0"
        )


def validate_persona(payload: Mapping[str, object], expected: PersonaExpectation) -> PersonaClassification:
    def strings(key: str) -> tuple[str, ...]:
        value = payload.get(key, ())
        return tuple(value) if isinstance(value, list) and all(isinstance(x, str) for x in value) else ()
    facts, observations, unknowns = strings("facts"), strings("observations"), strings("unknowns")
    unsupported = strings("unsupported_inferences")
    authority = strings("authority_escalations")
    roles = strings("role_violations")
    hypotheses = strings("hypotheses")
    return PersonaClassification(
        "PASS" if facts == expected.facts else "FAIL",
        "PASS" if observations == expected.observations else "FAIL",
        "PASS" if unknowns == expected.unknowns else "FAIL",
        "0" if not unsupported else "detected",
        "0" if not authority else "detected",
        "0" if not roles else "detected",
        ("PASS" if hypotheses == expected.hypotheses else "FAIL")
        if expected.hypotheses else "NOT_OBSERVED",
    )


UNKNOWN_CLASSIFICATIONS = (
    "PRESERVED", "PARAPHRASED_PRESERVED", "RESOLVED_WITH_EVIDENCE",
    "DROPPED", "CERTAINTY_ESCALATED", "CONTRADICTED", "MUTATED", "UNVERIFIABLE",
)
_UNKNOWN_PASS = frozenset({"PRESERVED", "PARAPHRASED_PRESERVED", "RESOLVED_WITH_EVIDENCE"})
_EPISTEMIC_PATTERNS = (
    (re.compile(r"\bremains?\s+unknown\b"), "is unknown"),
    (re.compile(r"\bis\s+not\s+known\b"), "is unknown"),
    (re.compile(r"\bis\s+unresolved\b"), "is unknown"),
    (re.compile(r"\bcannot\s+be\s+determined\b"), "is unknown"),
    (re.compile(r"\bis\s+undetermined\b"), "is unknown"),
)
_UNKNOWN_MARKERS = ("unknown", "not known", "unresolved", "cannot be determined", "undetermined")
_NEGATION_GUARDS = ("not true that", "false that", "no longer unknown")
_CERTAINTY_MARKERS = ("is known", "was confirmed", "is confirmed", "was proven", "is proven")
_TOKEN_STOP = frozenset({
    "a", "an", "the", "of", "to", "is", "was", "are", "were", "be", "been",
    "unknown", "known", "not", "remains", "remain", "unresolved", "cannot",
    "determined", "undetermined", "fact", "observation", "hypothesis",
})


def normalize_unknown_text(value: str) -> str:
    """NFKC + casefold + line/space folding + enumerated punctuation removal.

    Negation, epistemic words, entity tokens, dates, and numbers are retained.
    No translation, stemming, fuzzy matching, or proposition reordering occurs.
    """
    value = unicodedata.normalize("NFKC", value).casefold().replace("\r", " ").replace("\n", " ")
    value = re.sub(r"[\t ]+", " ", value).strip()
    value = re.sub(r"^[\s\"'`()\[\]{}.,;:!?。、「」！？-]+|[\s\"'`()\[\]{}.,;:!?。、「」！？-]+$", "", value)
    return value


def _canonical_unknown_marker(value: str) -> str:
    result = value
    for pattern, replacement in _EPISTEMIC_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def _proposition_tokens(value: str) -> tuple[str, ...]:
    words = re.findall(r"[^\W_]+(?:[-:][^\W_]+)*", value, flags=re.UNICODE)
    return tuple(sorted({word for word in words if word not in _TOKEN_STOP}))


@dataclass(frozen=True)
class ResolutionEvidence:
    evidence_id: str
    unknown_id: str
    proposition_key: tuple[str, ...]
    resolved_text: str


@dataclass(frozen=True)
class UnknownLedgerEntry:
    unknown_id: str
    source_text: str
    normalized_source: str
    candidate_text: str
    normalized_candidate: str
    classification: str
    evidence: tuple[str, ...]
    evidence_rule: str
    confidence_class: str
    source_proposition_key: tuple[str, ...]
    resolution_evidence_id: str = ""


@dataclass(frozen=True)
class UnknownValidationResult:
    ledger: tuple[UnknownLedgerEntry, ...]
    unknown_total: int
    preserved_count: int
    paraphrased_preserved_count: int
    resolved_with_evidence_count: int
    dropped_count: int
    certainty_escalated_count: int
    contradicted_count: int
    mutated_count: int
    unverifiable_count: int
    aggregate_pass: bool


def _unknown_id(normalized_source: str, occurrence: int) -> str:
    digest = hashlib.sha256(normalized_source.encode("utf-8")).hexdigest()[:12]
    return f"unknown-{digest}-{occurrence}"


def _has_unknown_marker(value: str) -> bool:
    return any(marker in value for marker in _UNKNOWN_MARKERS)


def _classify_unknown(
    unknown_id: str, source: str, candidate: str,
    resolution_evidence: Sequence[ResolutionEvidence],
) -> UnknownLedgerEntry:
    ns, nc = normalize_unknown_text(source), normalize_unknown_text(candidate)
    key = _proposition_tokens(ns)
    candidate_key = set(_proposition_tokens(nc))
    evidence: tuple[str, ...] = ()
    classification, rule, confidence, evidence_id = "UNVERIFIABLE", "fail_closed", "INSUFFICIENT", ""
    if not candidate:
        classification, rule, confidence = "DROPPED", "no_candidate", "DETERMINISTIC"
    elif any(marker in nc for marker in _NEGATION_GUARDS):
        classification, rule, confidence = "CONTRADICTED", "explicit_negation_guard", "DETERMINISTIC"
        evidence = tuple(marker for marker in _NEGATION_GUARDS if marker in nc)
    elif any(marker in nc for marker in _CERTAINTY_MARKERS) and set(key) <= candidate_key:
        classification, rule, confidence = "CERTAINTY_ESCALATED", "epistemic_reversal_guard", "DETERMINISTIC"
        evidence = tuple(marker for marker in _CERTAINTY_MARKERS if marker in nc)
    else:
        for item in resolution_evidence:
            if (item.unknown_id == unknown_id and item.proposition_key == key
                    and normalize_unknown_text(item.resolved_text) == nc):
                classification, rule, confidence = "RESOLVED_WITH_EVIDENCE", "bound_resolution_evidence", "DETERMINISTIC"
                evidence, evidence_id = (item.evidence_id,), item.evidence_id
                break
        else:
            canonical_source = _canonical_unknown_marker(ns)
            canonical_candidate = _canonical_unknown_marker(nc)
            if nc == ns:
                classification, rule, confidence = "PRESERVED", "normalized_exact", "DETERMINISTIC"
            elif canonical_candidate == canonical_source:
                classification, rule, confidence = "PARAPHRASED_PRESERVED", "enumerated_epistemic_equivalence", "DETERMINISTIC"
            elif set(key) <= candidate_key and not _has_unknown_marker(nc):
                classification, rule, confidence = "CERTAINTY_ESCALATED", "proposition_without_unknown_marker", "DETERMINISTIC"
            elif _has_unknown_marker(nc) and set(key) & candidate_key and not set(key) <= candidate_key:
                classification, rule, confidence = "MUTATED", "partial_or_changed_proposition", "DETERMINISTIC"
            elif _has_unknown_marker(nc) and not (set(key) & candidate_key):
                classification, rule, confidence = "MUTATED", "different_unknown_substituted", "DETERMINISTIC"
            elif set(key) <= candidate_key:
                classification, rule, confidence = "UNVERIFIABLE", "non_enumerated_reformulation", "INSUFFICIENT"
    return UnknownLedgerEntry(
        unknown_id, source, ns, candidate, nc, classification, evidence, rule,
        confidence, key, evidence_id,
    )


def validate_unknown_preservation(
    source_unknowns: Sequence[str], candidate_unknowns: Sequence[str],
    *, resolution_evidence: Sequence[ResolutionEvidence] = (),
) -> UnknownValidationResult:
    """Create a deterministic one-to-one ledger, then aggregate fail-closed."""
    seen: dict[str, int] = {}
    sources: list[tuple[str, str]] = []
    for source in source_unknowns:
        normalized = normalize_unknown_text(source)
        seen[normalized] = seen.get(normalized, 0) + 1
        sources.append((_unknown_id(normalized, seen[normalized]), source))
    # Global evidence-first matching prevents an earlier overlapping source
    # from consuming a later source's exact candidate. Ties are stable by
    # source index then candidate index; one candidate can bind only once.
    pairs: list[tuple[int, int, int, UnknownLedgerEntry]] = []
    for source_index, (unknown_id, source) in enumerate(sources):
        for candidate_index, candidate in enumerate(candidate_unknowns):
            entry = _classify_unknown(unknown_id, source, candidate, resolution_evidence)
            overlap = set(entry.source_proposition_key) & set(_proposition_tokens(entry.normalized_candidate))
            if entry.classification == "UNVERIFIABLE" and not overlap:
                continue
            pairs.append((UNKNOWN_CLASSIFICATIONS.index(entry.classification), source_index, candidate_index, entry))
    assigned_sources: dict[int, UnknownLedgerEntry] = {}
    assigned_candidates: set[int] = set()
    for _, source_index, candidate_index, entry in sorted(pairs):
        if source_index in assigned_sources or candidate_index in assigned_candidates:
            continue
        assigned_sources[source_index] = entry
        assigned_candidates.add(candidate_index)
    ledger = [
        assigned_sources.get(index, _classify_unknown(unknown_id, source, "", resolution_evidence))
        for index, (unknown_id, source) in enumerate(sources)
    ]
    counts = {name: sum(item.classification == name for item in ledger) for name in UNKNOWN_CLASSIFICATIONS}
    return UnknownValidationResult(
        tuple(ledger), len(ledger), counts["PRESERVED"], counts["PARAPHRASED_PRESERVED"],
        counts["RESOLVED_WITH_EVIDENCE"], counts["DROPPED"], counts["CERTAINTY_ESCALATED"],
        counts["CONTRADICTED"], counts["MUTATED"], counts["UNVERIFIABLE"],
        bool(ledger) and all(item.classification in _UNKNOWN_PASS for item in ledger),
    )


def _extract_unknown_candidates(text: str) -> tuple[str, ...]:
    labelled = []
    for line in text.splitlines():
        match = re.match(r"\s*(?:[-*]\s*)?unknown\s*:\s*(.+?)\s*$", line, re.I)
        if match:
            labelled.append(match.group(1))
    return tuple(labelled)


@dataclass(frozen=True)
class NormalizedResponse:
    assistant_text: str
    response_model: str = "UNKNOWN"
    routing_provider: str = "UNKNOWN"


def _normalize_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or not content:
        raise ResponseStructureError("missing or unsupported assistant content")
    text_parts: list[str] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") not in {"text", "output_text"}:
            raise ResponseStructureError("unsupported structured content part")
        text = part.get("text")
        if not isinstance(text, str):
            raise ResponseStructureError("structured content text must be a string")
        text_parts.append(text)
    return "".join(text_parts)


def normalize_openrouter_response(raw: bytes) -> NormalizedResponse:
    """Decode an OpenRouter envelope without requiring JSON assistant text."""
    try:
        envelope = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ObservationError("PARSE", "MALFORMED_RESPONSE") from None
    if not isinstance(envelope, dict):
        raise ResponseStructureError("response envelope must be an object")
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ResponseStructureError("response choices missing")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ResponseStructureError("response message missing")
    if "content" not in message:
        raise ResponseStructureError("response content missing")
    content = _normalize_content(message["content"])
    model = envelope.get("model")
    provider = envelope.get("provider")
    return NormalizedResponse(
        content,
        model if isinstance(model, str) and model else "UNKNOWN",
        provider if isinstance(provider, str) and provider else "UNKNOWN",
    )


def validate_persona_text(text: str, expected: PersonaExpectation) -> PersonaClassification:
    """Classify only explicit evidence supplied by the caller; infer nothing."""
    folded = text.casefold()
    def preserved(items: tuple[str, ...]) -> str:
        return "PASS" if items and all(item.casefold() in folded for item in items) else "FAIL"
    def detected(markers: tuple[str, ...]) -> str:
        return "detected" if any(marker.casefold() in folded for marker in markers) else "0"
    unknown_result = validate_unknown_preservation(
        expected.unknowns, _extract_unknown_candidates(text)
    )
    return PersonaClassification(
        preserved(expected.facts), preserved(expected.observations),
        "PASS" if unknown_result.aggregate_pass else "FAIL", detected(expected.forbidden_inferences),
        detected(expected.authority_markers), detected(expected.role_markers),
        preserved(expected.hypotheses) if expected.hypotheses else "NOT_OBSERVED",
        unknown_result,
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
    primary_failure_stage: str = ""
    primary_failure_class: str = ""
    audit_failure_class: str = ""
    assistant_text: str = ""


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
                 parser: Callable[[bytes], object] = normalize_openrouter_response):
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
            try:
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
                except ResponseStructureError:
                    raise ObservationError("PARSE", "STRUCTURAL_RESPONSE_ERROR") from None
                except ObservationError:
                    raise
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                    raise ObservationError("PARSE", "MALFORMED_RESPONSE") from None
                if self.clock() - t > self.budget.processing_timeout:
                    raise ObservationError("PARSE", "PROCESSING_TIMEOUT")
                result.parse_result = "PASS"; emit.emit("H13B_RESPONSE_PARSED")
                if isinstance(payload, NormalizedResponse):
                    result.response_model = payload.response_model
                    if result.routing_provider == "UNKNOWN":
                        result.routing_provider = payload.routing_provider
                    result.assistant_text = payload.assistant_text
                elif isinstance(payload, Mapping):
                    model = payload.get("model")
                    result.response_model = model if isinstance(model, str) and model else "UNKNOWN"
                else:
                    raise ObservationError("PARSE", "STRUCTURAL_RESPONSE_ERROR")
                if result.response_model == "UNKNOWN":
                    raise ObservationError("PARSE", "MISSING_MODEL_IDENTITY")
                emit.emit("H13B_PERSONA_VALIDATION_STARTED")
                if isinstance(payload, NormalizedResponse):
                    result.persona_validation = validate_persona_text(payload.assistant_text, expectation)
                else:
                    result.persona_validation = validate_persona(payload, expectation)
                if not result.persona_validation.passed:
                    raise ObservationError("PERSONA", "PERSONA_VALIDATION_FAILED")
                emit.emit("H13B_PERSONA_VALIDATION_COMPLETE")
                result.durations["processing"] = self.clock() - t
                if result.durations["processing"] > self.budget.processing_timeout:
                    raise ObservationError("PARSE", "PROCESSING_TIMEOUT")
            except ObservationError as exc:
                result.primary_failure_stage = exc.stage
                result.primary_failure_class = exc.safe_class

            # Once dispatch starts, locally recoverable failures never skip audit.
            emit.emit("H13B_POST_AUDIT_STARTED")
            try:
                t = self.clock(); after = self.auditor.snapshot(); result.durations["post_audit"] = self.clock() - t
                if result.durations["post_audit"] > self.budget.post_audit_max:
                    raise ObservationError("AUDIT", "AUDIT_TIMEOUT")
                result.filesystem_delta = self.auditor.changed(before, after)
                emit.emit("H13B_POST_AUDIT_COMPLETE")
                if result.filesystem_delta:
                    raise ObservationError("AUDIT", "UNEXPECTED_WRITE")
            except ObservationError as exc:
                result.audit_failure_class = exc.safe_class
            except (OSError, RuntimeError):
                result.audit_failure_class = "AUDIT_TIMEOUT"

            if self.clock() - started > self.budget.outer_timeout and not result.primary_failure_class:
                result.primary_failure_stage = "OUTER"
                result.primary_failure_class = "OUTER_TIMEOUT"
            if result.primary_failure_class:
                result.failure_stage = result.primary_failure_stage
                result.safe_error_class = result.primary_failure_class
                emit.emit(
                    f"H13D_FAILED:{result.primary_failure_stage}:{result.primary_failure_class}",
                    terminal=True,
                )
            elif result.audit_failure_class:
                result.failure_stage = "AUDIT"
                result.safe_error_class = result.audit_failure_class
                emit.emit(f"H13D_FAILED:AUDIT:{result.audit_failure_class}", terminal=True)
            else:
                emit.emit("H13B_COMPLETE", terminal=True)
        except ObservationError as exc:
            result.failure_stage, result.safe_error_class = exc.stage, exc.safe_class
            emit.emit(f"H13D_FAILED:{exc.stage}:{exc.safe_class}", terminal=True)
        return result


def duration_summary(values: Sequence[float]) -> Mapping[str, float]:
    if not values:
        return {"min": 0.0, "median": 0.0, "max": 0.0}
    return {"min": min(values), "median": median(values), "max": max(values)}
