"""Owner-controlled, data-only handoff artifacts between Persona boundaries."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Tuple

from .loader import load_persona_kernel
from .registry import REGISTRY
from .responsibility import ResponsibilityRow, build_responsibility_matrix

SCHEMA_VERSION = "1.0.0"
STATES = ("DRAFT", "PENDING_OWNER_REVIEW", "APPROVED", "DELIVERED", "REJECTED", "QUARANTINED", "INVALID")
CLASSIFICATIONS = ("FACT", "OBSERVATION", "HYPOTHESIS", "RECOMMENDATION", "REQUIREMENT", "CONSTRAINT", "UNKNOWN")
RESULTS = ("ALLOW_DELIVERY", "PENDING_OWNER_REVIEW", "DENY_ROLE_BOUNDARY", "DENY_FORBIDDEN_RESPONSIBILITY", "DENY_OWNER_AUTHORIZATION", "DENY_CHECKSUM_MISMATCH", "DENY_UNKNOWN_PERSONA", "DENY_UNRESOLVED_BOUNDARY", "DENY_AUTHORITY_TRANSFER", "DENY_PERMISSION_ESCALATION", "DENY_TOOL_TRANSFER", "DENY_CREDENTIAL_CONTENT", "QUARANTINE_CANON_CONFLICT", "POLICY_ERROR")
APPROVED_ROUTES = {("police_horitius", "curator_orchestra"), ("persona_gemini", "exor_verelden"), ("exor_verelden", "beg_weag")}
_DANGEROUS = {
    "DENY_AUTHORITY_TRANSFER": re.compile(r"owner authority|authority grant|you are now owner", re.I),
    "DENY_PERMISSION_ESCALATION": re.compile(r"permission grant|grant permissions?|growth mutation|knowledge mutation", re.I),
    "DENY_TOOL_TRANSFER": re.compile(r"inherit my tools|tool grant|transfer tools?", re.I),
    "DENY_CREDENTIAL_CONTENT": re.compile(r"(?:sk-[A-Za-z0-9_-]{12,}|api[_ -]?key\s*[:=]|authorization\s*:|use my credentials)", re.I),
    "QUARANTINE_CANON_CONFLICT": re.compile(r"ignore (?:your )?canon|change your role|canon mutation", re.I),
}

class HandoffValidationError(ValueError): pass

# H6-2 compatibility contract. This remains a non-executable Police pilot
# artifact; H6-7 delivery uses HandoffEnvelope and the Owner policy gate below.
_LEGACY_FORBIDDEN = {"canon", "canonical_role", "permissions", "owner_authority", "tool_permissions"}
_LEGACY_LISTS = ("facts", "hypotheses", "unknowns", "out_of_scope", "evidence_refs")

@dataclass(frozen=True)
class PersonaHandoff:
    handoff_id: str
    from_persona: str
    to_persona: str
    task_type: str
    facts: Tuple[str, ...]
    hypotheses: Tuple[str, ...]
    unknowns: Tuple[str, ...]
    requested_output: str
    out_of_scope: Tuple[str, ...]
    evidence_refs: Tuple[str, ...]
    canon_version: str
    approval_required: bool
    execution_requested: bool

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> "PersonaHandoff":
        forbidden = _LEGACY_FORBIDDEN & set(data)
        if forbidden: raise HandoffValidationError("handoff cannot alter " + ", ".join(sorted(forbidden)))
        required = set(cls.__dataclass_fields__)
        if set(data) != required: raise HandoffValidationError(f"invalid handoff fields; missing={sorted(required-set(data))}, extra={sorted(set(data)-required)}")
        if data["execution_requested"] is not False: raise HandoffValidationError("handoff does not authorize execution")
        if data["from_persona"] != "police_horitius": raise HandoffValidationError("Police pilot handoff must originate from police_horitius")
        converted = dict(data)
        for name in _LEGACY_LISTS:
            if not isinstance(data[name], (list, tuple)): raise HandoffValidationError(f"{name} must be a list")
            converted[name] = tuple(data[name])
        return cls(**converted)

@dataclass(frozen=True)
class ClassifiedItem:
    classification: str
    text: str

    @classmethod
    def parse(cls, value: Mapping[str, str]) -> "ClassifiedItem":
        if set(value) != {"classification", "text"} or value.get("classification") not in CLASSIFICATIONS or not str(value.get("text", "")).strip():
            raise HandoffValidationError("invalid classified payload item")
        return cls(value["classification"], value["text"].strip())

@dataclass(frozen=True)
class Provenance:
    source_persona_id: str
    source_canon_version: str
    source_canon_checksum: str
    creation_mechanism: str
    originating_reference: str
    created_at: str

@dataclass(frozen=True)
class OwnerAuthorization:
    authorization_source: str = ""
    authorization_decision: str = "PENDING"
    authorization_timestamp: str = ""
    authorized_handoff_checksum: str = ""

@dataclass(frozen=True)
class HandoffEnvelope:
    schema_version: str
    handoff_id: str
    created_at: str
    source_persona_id: str
    target_persona_id: str
    handoff_type: str
    subject: str
    findings: Tuple[ClassifiedItem, ...]
    evidence: Tuple[ClassifiedItem, ...]
    assumptions: Tuple[ClassifiedItem, ...]
    hypotheses: Tuple[ClassifiedItem, ...]
    uncertainties: Tuple[ClassifiedItem, ...]
    recommendations: Tuple[ClassifiedItem, ...]
    requirements: Tuple[ClassifiedItem, ...]
    constraints: Tuple[ClassifiedItem, ...]
    requested_work: Tuple[ClassifiedItem, ...]
    source_responsibility_ids: Tuple[str, ...]
    target_responsibility_ids: Tuple[str, ...]
    provenance: Provenance
    owner_authorization: OwnerAuthorization = OwnerAuthorization()
    status: str = "DRAFT"
    checksum: str = ""

    def content(self) -> dict[str, Any]:
        data = asdict(self); data.pop("checksum"); data.pop("owner_authorization"); data.pop("status")
        return data
    def calculated_checksum(self) -> str:
        return hashlib.sha256(json.dumps(self.content(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    def sealed(self) -> "HandoffEnvelope": return replace(self, checksum=self.calculated_checksum())

@dataclass(frozen=True)
class PolicyDecision:
    result: str
    reason: str

def _all_text(e: HandoffEnvelope) -> str:
    values = [e.subject]
    for name in ("findings", "evidence", "assumptions", "hypotheses", "uncertainties", "recommendations", "requirements", "constraints", "requested_work"):
        values.extend(x.text for x in getattr(e, name))
    return "\n".join(values)

def evaluate_handoff(e: HandoffEnvelope, matrix: Tuple[ResponsibilityRow, ...] | None = None) -> PolicyDecision:
    if e.source_persona_id not in REGISTRY or e.target_persona_id not in REGISTRY: return PolicyDecision("DENY_UNKNOWN_PERSONA", "registry binding failed")
    if e.source_persona_id == e.target_persona_id or (e.source_persona_id, e.target_persona_id) not in APPROVED_ROUTES: return PolicyDecision("DENY_UNRESOLVED_BOUNDARY", "route is not Owner-approved")
    if e.schema_version != SCHEMA_VERSION or e.status not in STATES or not e.handoff_id or not e.created_at: return PolicyDecision("POLICY_ERROR", "invalid envelope")
    if e.checksum != e.calculated_checksum(): return PolicyDecision("DENY_CHECKSUM_MISMATCH", "content checksum mismatch")
    source = load_persona_kernel(e.source_persona_id); target = load_persona_kernel(e.target_persona_id)
    p = e.provenance
    if not all((p.source_persona_id, p.source_canon_version, p.source_canon_checksum, p.creation_mechanism, p.originating_reference, p.created_at)): return PolicyDecision("POLICY_ERROR", "provenance required")
    if p.source_persona_id != source.persona_id or p.source_canon_version != source.canon_version or p.source_canon_checksum != source.checksum: return PolicyDecision("QUARANTINE_CANON_CONFLICT", "source Canon provenance mismatch")
    text = _all_text(e)
    for result, pattern in _DANGEROUS.items():
        if pattern.search(text): return PolicyDecision(result, "prohibited transfer content")
    rows = {r.persona_id: r for r in (matrix or build_responsibility_matrix())}; source_row = rows[e.source_persona_id]; row = rows[e.target_persona_id]
    source_ids = set(e.source_responsibility_ids)
    if not source_ids or not source_ids <= set(source_row.primary_responsibilities + source_row.secondary_responsibilities): return PolicyDecision("DENY_ROLE_BOUNDARY", "source responsibility binding failed")
    requested = set(e.target_responsibility_ids)
    if requested & set(row.forbidden_responsibilities): return PolicyDecision("DENY_FORBIDDEN_RESPONSIBILITY", "target forbidden responsibility")
    if not requested or not requested <= set(row.primary_responsibilities + row.secondary_responsibilities): return PolicyDecision("DENY_ROLE_BOUNDARY", "work is outside target responsibility")
    auth = e.owner_authorization
    if e.status in {"DRAFT", "PENDING_OWNER_REVIEW"}: return PolicyDecision("PENDING_OWNER_REVIEW", "Owner approval required")
    if auth.authorization_source != "owner_control_plane" or auth.authorization_decision != "APPROVED" or not auth.authorization_timestamp: return PolicyDecision("DENY_OWNER_AUTHORIZATION", "trusted Owner authorization absent")
    if auth.authorized_handoff_checksum != e.checksum: return PolicyDecision("DENY_CHECKSUM_MISMATCH", "approval is bound to another content checksum")
    if e.status not in {"APPROVED", "DELIVERED"}: return PolicyDecision("DENY_OWNER_AUTHORIZATION", "state cannot deliver")
    return PolicyDecision("ALLOW_DELIVERY", "validated external Persona artifact — data only")

def approve(e: HandoffEnvelope, *, authorization_source: str, timestamp: str) -> HandoffEnvelope:
    if authorization_source != "owner_control_plane": raise HandoffValidationError("Persona self-approval denied")
    if e.status != "PENDING_OWNER_REVIEW" or e.checksum != e.calculated_checksum(): raise HandoffValidationError("handoff is not approvable")
    return replace(e, status="APPROVED", owner_authorization=OwnerAuthorization(authorization_source, "APPROVED", timestamp, e.checksum))

def delivery_payload(e: HandoffEnvelope) -> Mapping[str, Any]:
    if evaluate_handoff(e).result != "ALLOW_DELIVERY": raise HandoffValidationError("delivery denied")
    return {"label": "EXTERNAL PERSONA ARTIFACT — DATA ONLY", "target_persona_id": e.target_persona_id, "target_canon_checksum": load_persona_kernel(e.target_persona_id).checksum, "artifact": e.content()}

class HandoffStore:
    def __init__(self, root: Path, *, read_enabled: bool = False, write_enabled: bool = False):
        self.root, self.read_enabled, self.write_enabled = root, read_enabled, write_enabled
    def write(self, e: HandoffEnvelope) -> Path:
        if not self.write_enabled: raise HandoffValidationError("handoff write disabled")
        decision = evaluate_handoff(e)
        if decision.result in {"DENY_CREDENTIAL_CONTENT", "DENY_AUTHORITY_TRANSFER", "DENY_PERMISSION_ESCALATION", "DENY_TOOL_TRANSFER"}:
            raise HandoffValidationError("prohibited handoff content cannot be persisted")
        folder = {"APPROVED":"approved", "DELIVERED":"delivered", "REJECTED":"rejected", "QUARANTINED":"quarantined"}.get(e.status, "drafts")
        path = self.root / "persona_handoffs" / folder / f"{e.handoff_id}.json"
        if path.exists(): raise HandoffValidationError("duplicate handoff ID")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(e), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".handoff-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f: f.write(payload); f.flush(); os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
        return path
    def read(self, path: Path) -> Mapping[str, Any]:
        if not self.read_enabled: raise HandoffValidationError("handoff read disabled")
        try: return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc: raise HandoffValidationError("corrupt handoff") from exc
