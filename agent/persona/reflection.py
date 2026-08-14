"""Owner-gated conversion of evaluation evidence into reflective growth."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from typing import Tuple

from .evaluation import WorkflowEvaluation
from .growth import GrowthRecord, GrowthStoreError, PoliceGrowthStore
from .loader import load_persona_kernel

SCHEMA_VERSION = "1.0.0"
STATUSES = ("PENDING","ACCEPTED","REJECTED","DEFERRED","QUARANTINED")
_DANGEROUS = re.compile(r"ignore canon|canon change|change canon|change your role|owner authority|grant yourself permission|permission escalation|assign skill|tool escalation|automatic handoff|automatic persona switch|cross-persona|security boundary|credential|api[_ -]?key|authorization\s*:|bearer\s+|persistence policy bypass",re.I)

class ReflectionError(ValueError): pass

@dataclass(frozen=True)
class ReflectionCandidate:
    reflection_candidate_id: str
    persona_id: str
    workflow_id: str
    stage_id: str
    source_evaluation_id: str
    source_evaluation_checksum: str
    source_artifact_checksums: Tuple[str,...]
    created_at: str
    status: str
    problem_type: str
    evidence: Tuple[str,...]
    observed_failure: str
    observed_success: str
    hypothesis: str
    proposed_lesson: str
    counter_evidence: Tuple[str,...]
    uncertainty: str
    applicability: str
    non_applicability: str
    classification: str
    owner_review_required: bool = True
    checksum: str = ""

    def semantic(self):
        data=asdict(self); data.pop("checksum"); data.pop("created_at"); data.pop("status")
        return data
    def calculated_checksum(self): return hashlib.sha256(json.dumps(self.semantic(),ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    def sealed(self): return replace(self,checksum=self.calculated_checksum())
    def validate(self):
        if self.status not in STATUSES or not self.owner_review_required or self.checksum!=self.calculated_checksum(): raise ReflectionError("corrupt reflection candidate")
        if not all((self.reflection_candidate_id,self.persona_id,self.workflow_id,self.stage_id,self.source_evaluation_id,self.source_evaluation_checksum,self.source_artifact_checksums,self.evidence,self.hypothesis,self.proposed_lesson,self.applicability)): raise ReflectionError("reflection provenance incomplete")
        if _DANGEROUS.search(" ".join((self.hypothesis,self.proposed_lesson,self.uncertainty,self.applicability,self.non_applicability,*self.evidence,*self.counter_evidence))): raise ReflectionError("QUARANTINED")

@dataclass(frozen=True)
class OwnerReflectionDecision:
    decision_id: str
    decision: str
    authorization_source: str
    decided_at: str
    candidate_checksum: str
    reason: str
    checksum: str = ""
    def calculated_checksum(self):
        data=asdict(self); data.pop("checksum")
        return hashlib.sha256(json.dumps(data,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    def sealed(self): return replace(self,checksum=self.calculated_checksum())
    def validate(self):
        if self.decision not in {"ACCEPT","REJECT","DEFER"} or self.authorization_source!="owner_control_plane" or not self.decided_at or self.checksum!=self.calculated_checksum(): raise ReflectionError("invalid Owner decision")

def build_candidate(evaluation: WorkflowEvaluation, persona_id: str, stage_id: str, artifact_checksums: Tuple[str,...], created_at: str, *, candidate_id: str, lesson: str, applicability: str, hypothesis: str="Evaluation evidence indicates a bounded reasoning issue.") -> ReflectionCandidate:
    if evaluation.checksum!=evaluation.calculated_checksum() or evaluation.growth_claim not in {"NO_EVIDENCE","IMPROVEMENT_OBSERVED","REGRESSION_OBSERVED","MIXED","INSUFFICIENT_EVIDENCE"}: raise ReflectionError("invalid evaluation")
    load_persona_kernel(persona_id)
    evidence=tuple(evaluation.unknowns) or evaluation.evidence
    if not evidence or any("I grew" in x or "I am better" in x for x in evidence): raise ReflectionError("self-judgment is not evidence")
    candidate=ReflectionCandidate(candidate_id,persona_id,evaluation.workflow_id,stage_id,evaluation.evaluation_id,evaluation.checksum,artifact_checksums,created_at,"PENDING","reasoning_mistake",evidence,"structured loss or mutation observed","",hypothesis,lesson,(),"Evidence is limited to evaluated workflows.",applicability,"Different task class or changed Owner constraints.","LEARNING_INTENT_RECORDED").sealed()
    candidate.validate(); return candidate

def decide(candidate: ReflectionCandidate, action: str, *, authorization_source: str, decision_id: str, decided_at: str, reason: str) -> tuple[ReflectionCandidate,OwnerReflectionDecision]:
    candidate.validate()
    if authorization_source!="owner_control_plane": raise ReflectionError("self-approval denied")
    decision=OwnerReflectionDecision(decision_id,action,authorization_source,decided_at,candidate.checksum,reason).sealed(); decision.validate()
    status={"ACCEPT":"ACCEPTED","REJECT":"REJECTED","DEFER":"DEFERRED"}[action]
    return replace(candidate,status=status),decision

def create_growth_record(candidate: ReflectionCandidate, decision: OwnerReflectionDecision, *, record_id: str, created_at: str) -> GrowthRecord:
    candidate.validate(); decision.validate()
    if candidate.status!="ACCEPTED" or decision.decision!="ACCEPT": raise ReflectionError("accepted Owner decision required")
    if decision.candidate_checksum!=candidate.checksum: raise ReflectionError("OWNER_DECISION_STALE")
    kernel=load_persona_kernel(candidate.persona_id)
    return GrowthRecord(record_id,candidate.persona_id,"reasoning_mistake",created_at,"evaluation_reflection",observation=candidate.observed_failure,hypothesis=candidate.hypothesis,evidence_for=candidate.evidence,evidence_against=candidate.counter_evidence,uncertainty=candidate.uncertainty,reasoning="Owner-controlled reflection",outcome="LEARNING_INTENT_RECORDED",lesson=candidate.proposed_lesson,confidence="medium",canon_version=kernel.canon_version,canon_checksum=kernel.checksum,status="validated")

def store_accepted(candidate: ReflectionCandidate, decision: OwnerReflectionDecision, store: PoliceGrowthStore, *, record_id: str, created_at: str) -> GrowthRecord:
    return store.append(create_growth_record(candidate,decision,record_id=record_id,created_at=created_at))

def classify_application(records: Tuple[GrowthRecord,...], comparison: str) -> str:
    if not records: return "NO_EVIDENCE"
    if comparison=="IMPROVEMENT_OBSERVED": return "THINKING_GROWTH_OBSERVED"
    if comparison in {"REGRESSION_OBSERVED","MIXED","INSUFFICIENT_EVIDENCE"}: return comparison
    return "LEARNING_APPLIED"

AUTO_GROWTH=False
AUTO_KNOWLEDGE=False
AUTO_CANON=False
AUTO_HANDOFF=False
AUTO_PERSONA_SWITCH=False
