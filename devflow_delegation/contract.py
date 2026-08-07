"""Canonical DEVFLOW_WORK_REQUEST contract (protocol v3) + v2 compatibility.

Pure module: no I/O, no imports beyond the stdlib. Every validation rule is
deterministic so control-plane decisions stay reproducible (spec:
docs/superpowers/specs/2026-08-06-devflow-delegation-plane-design.md,
"Canonical work-request contract").
"""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List

SCHEMA_VERSION = "3.0"
MSG_TYPE = "DEVFLOW_WORK_REQUEST"
COMPAT_MSG_TYPES = frozenset({"DEVFLOW_FIX_REQUEST"})

KINDS = ("bug", "improvement", "feature", "refactor", "task")
SEVERITIES = ("low", "medium", "high", "critical")
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
PRIORITIES = ("P0", "P1", "P2", "P3")

# v2 compat mappings (roadmap_devflow_intake.py priority words -> v3 values)
_V2_PRIORITY = {"high": "P1", "medium": "P2", "low": "P3"}
_V2_SEVERITY = {"high": "medium", "medium": "low", "low": "low"}


class ContractError(ValueError):
    """Contract violation with the full list of structured reasons."""

    def __init__(self, reasons: List[str]):
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


@dataclass
class Evidence:
    kind: str
    summary: str
    ref: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "ref": self.ref, "summary": self.summary}


@dataclass
class WorkRequest:
    request_id: str
    idempotency_key: str
    dedup_fingerprint: str
    created_at: str
    source_agent: str
    source_kind: str
    finding_id: str
    kind: str
    title: str
    problem_statement: str
    evidence: List[Evidence]
    target_repo: str
    target_subsystem: str
    severity: str
    priority: str
    confidence: float
    proposed_approach: str = ""
    acceptance_criteria: List[str] = field(default_factory=list)
    safety_notes: List[str] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    def to_envelope(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "type": MSG_TYPE,
            "request_id": self.request_id,
            "idempotency_key": self.idempotency_key,
            "dedup_fingerprint": self.dedup_fingerprint,
            "created_at": self.created_at,
            "source": {
                "agent": self.source_agent,
                "kind": self.source_kind,
                "finding_id": self.finding_id,
            },
            "kind": self.kind,
            "title": self.title,
            "problem_statement": self.problem_statement,
            "evidence": [e.to_dict() for e in self.evidence],
            "target": {"repo": self.target_repo, "subsystem": self.target_subsystem},
            "severity": self.severity,
            "priority": self.priority,
            "confidence": self.confidence,
            "proposed_approach": self.proposed_approach,
            "acceptance_criteria": list(self.acceptance_criteria),
            "safety_notes": list(self.safety_notes),
        }


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def _nonempty_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def validate_payload(payload: Dict[str, Any]) -> List[str]:
    """Return the list of contract violations ([] = valid).

    Deliberately does NOT validate target or idempotency_key: target
    resolution (allowlist) and idempotency-key derivation are emitter gates.
    """
    errs: List[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        errs.append("unsupported_schema_version")
    if payload.get("type") != MSG_TYPE:
        errs.append("unsupported_type")

    src = payload.get("source")
    if not isinstance(src, dict) or not _nonempty_str(src.get("agent")):
        errs.append("missing_source")

    if payload.get("kind") not in KINDS:
        errs.append("invalid_kind")
    if not _nonempty_str(payload.get("title")):
        errs.append("missing_title")
    if not _nonempty_str(payload.get("problem_statement")):
        errs.append("missing_problem_statement")

    ev = payload.get("evidence")
    if (
        not isinstance(ev, list)
        or not ev
        or not all(
            isinstance(e, dict) and _nonempty_str(e.get("kind")) and _nonempty_str(e.get("summary"))
            for e in ev
        )
    ):
        errs.append("missing_evidence")

    ac = payload.get("acceptance_criteria")
    if not isinstance(ac, list) or not ac or not all(_nonempty_str(c) for c in ac):
        errs.append("missing_acceptance_criteria")

    if payload.get("severity") not in SEVERITIES:
        errs.append("invalid_severity")
    if payload.get("priority") not in PRIORITIES:
        errs.append("invalid_priority")

    conf = payload.get("confidence")
    if not isinstance(conf, (int, float)) or isinstance(conf, bool) or not (0.0 <= float(conf) <= 1.0):
        errs.append("invalid_confidence")

    return errs


def compute_fingerprint(
    *,
    target_repo: str,
    target_subsystem: str,
    kind: str,
    title: str,
    problem_statement: str,
) -> str:
    """Versioned sha256 over normalized source-independent issue identity.

    Source metadata and volatile timestamps are deliberately excluded so a
    re-observation of the same underlying problem deduplicates. Bump the
    'v1' prefix to supersede intentionally (spec: Flood control).
    """
    ident = "|".join(
        ["v1", _norm(target_repo), _norm(target_subsystem), _norm(kind), _norm(title), _norm(problem_statement)]
    )
    return "sha256:" + hashlib.sha256(ident.encode("utf-8")).hexdigest()


def parse_request(payload: Dict[str, Any]) -> WorkRequest:
    """Validate a v3 payload and build a WorkRequest.

    Requires 'idempotency_key' to already be set (the emitter derives the
    default 'auto:<fingerprint>' key when the producer did not supply one).
    Raises ContractError listing every violation.
    """
    errs = validate_payload(payload)
    idem = _nonempty_str(payload.get("idempotency_key"))
    if not idem:
        errs.append("missing_idempotency_key")
    if errs:
        raise ContractError(errs)

    src = payload["source"]
    target = payload.get("target") or {}
    evidence = [
        Evidence(kind=e["kind"].strip(), summary=e["summary"].strip(), ref=_nonempty_str(e.get("ref")))
        for e in payload["evidence"]
    ]
    return WorkRequest(
        request_id=f"dwr_{uuid.uuid4().hex[:20]}",
        idempotency_key=idem,
        dedup_fingerprint=compute_fingerprint(
            target_repo=target["repo"],
            target_subsystem=target["subsystem"],
            kind=payload["kind"],
            title=payload["title"],
            problem_statement=payload["problem_statement"],
        ),
        created_at=datetime.now(timezone.utc).isoformat(),
        source_agent=src["agent"].strip(),
        source_kind=_nonempty_str(src.get("kind")) or src["agent"].strip(),
        finding_id=_nonempty_str(src.get("finding_id")),
        kind=payload["kind"],
        title=payload["title"].strip(),
        problem_statement=payload["problem_statement"].strip(),
        evidence=evidence,
        target_repo=target["repo"].strip(),
        target_subsystem=target["subsystem"].strip(),
        severity=payload["severity"],
        priority=payload["priority"],
        confidence=float(payload["confidence"]),
        proposed_approach=_nonempty_str(payload.get("proposed_approach")),
        acceptance_criteria=[c.strip() for c in payload["acceptance_criteria"]],
        safety_notes=[_nonempty_str(s) for s in (payload.get("safety_notes") or []) if _nonempty_str(s)],
    )


def parse_v2_fix_request(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a legacy DEVFLOW_FIX_REQUEST (protocol 2.0) envelope — as
    emitted by profiles/main/scripts/roadmap_devflow_intake.py — into a v3
    payload dict, preserving its idempotency key.

    Adapters may enrich incomplete native findings but must never fabricate
    evidence: the roadmap row's own text and traces ARE the evidence; the
    derived acceptance criteria restate the row's task + reversibility.
    """
    if envelope.get("type") != "DEVFLOW_FIX_REQUEST":
        raise ContractError(["not_a_v2_fix_request"])
    p = envelope.get("payload") or {}
    issue = _nonempty_str(p.get("issue"))
    task = _nonempty_str(p.get("task"))
    if not issue or not task:
        raise ContractError(["v2_missing_issue_or_task"])

    ev_src = p.get("evidence") or {}
    ref = f"{ev_src.get('roadmap', '')}:L{ev_src.get('source_row', '')}"
    traces = _nonempty_str(ev_src.get("traces_to"))
    prio_word = p.get("priority") or "medium"
    recovery = _nonempty_str(p.get("recovery"))
    safety = _nonempty_str(p.get("safety")) or "No autonomous merge/deploy."

    return {
        "schema_version": SCHEMA_VERSION,
        "type": MSG_TYPE,
        "idempotency_key": _nonempty_str(envelope.get("idempotency_key")) or f"v2:{issue.lower()}:v1",
        "source": {
            "agent": _nonempty_str(envelope.get("from")) or "v2-producer",
            "kind": "arch-review",
            "finding_id": issue,
        },
        "kind": "task",
        "title": f"{issue}: {task}"[:160],
        "problem_statement": task + (f"\nRecovery/reversibility: {recovery}" if recovery else ""),
        "evidence": [
            {"kind": "roadmap_row", "ref": ref, "summary": (traces or task[:200])}
        ],
        "target": {"repo": "hermes", "subsystem": "roadmap"},
        "severity": _V2_SEVERITY.get(prio_word, "low"),
        "priority": _V2_PRIORITY.get(prio_word, "P2"),
        "confidence": 0.8,
        "proposed_approach": "",
        "acceptance_criteria": [
            f"Resolve roadmap item {issue} as described in its row text.",
            "Preserve the row's reversibility/recovery constraints: " + (recovery or "unspecified"),
        ],
        "safety_notes": [safety],
    }
