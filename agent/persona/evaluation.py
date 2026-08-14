"""Evidence-bound deterministic evaluation of controlled Persona workflows."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from typing import Mapping, Tuple

from .workflow import PersonaWorkflow

SCHEMA_VERSION = "1.0.0"
LEDGER_STATUSES = ("PRESERVED", "TRANSFORMED_VALID", "LOST", "MUTATED", "CONTRADICTED", "UNSUPPORTED_ADDITION", "UNRESOLVED")
GROWTH_CLAIMS = ("NOT_EVALUATED", "NO_EVIDENCE", "IMPROVEMENT_OBSERVED", "REGRESSION_OBSERVED", "MIXED", "INSUFFICIENT_EVIDENCE")
OWNER_CORRECTIONS = ("OWNER_CLARIFICATION", "OWNER_CORRECTION", "OWNER_SCOPE_CHANGE", "OWNER_PREFERENCE", "OWNER_UNKNOWN_RESOLUTION")
_SECRET = re.compile(r"(?:sk-[A-Za-z0-9_-]{12,}|api[_ -]?key\s*[:=]|authorization\s*:|bearer\s+[A-Za-z0-9._-]{12,})", re.I)

class EvaluationError(ValueError): pass

@dataclass(frozen=True)
class EvidenceItem:
    item_id: str
    classification: str
    text: str
    semantic_key: str

    def validate(self) -> None:
        if not all((self.item_id, self.classification, self.text, self.semantic_key)): raise EvaluationError("incomplete evidence")
        if self.classification not in {"FACT","OBSERVATION","HYPOTHESIS","RECOMMENDATION","REQUIREMENT","CONSTRAINT","UNKNOWN"}: raise EvaluationError("unclassified evidence")
        if _SECRET.search(self.text): raise EvaluationError("credential-like evidence rejected")

@dataclass(frozen=True)
class LedgerEntry:
    source_stage: str
    target_stage: str
    item_id: str
    classification: str
    status: str
    evidence: Tuple[str, ...]
    contradiction: str = "NONE"

@dataclass(frozen=True)
class Metric:
    metric: str
    result: str
    numerator: int
    denominator: int
    evidence: Tuple[str, ...]

@dataclass(frozen=True)
class StageEvaluation:
    stage: str
    metrics: Tuple[Metric, ...]

@dataclass(frozen=True)
class TransitionEvaluation:
    source_stage: str
    target_stage: str
    ledger: Tuple[LedgerEntry, ...]
    metrics: Tuple[Metric, ...]

@dataclass(frozen=True)
class WorkflowMetrics:
    requirement_preservation_rate: str
    constraint_preservation_rate: str
    unknown_preservation_rate: str
    classification_preservation_rate: str
    handoff_integrity_rate: str
    unsupported_addition_count: int
    contradiction_count: int
    owner_correction_count: int
    role_boundary_violation_count: int
    authority_violation_count: int

@dataclass(frozen=True)
class WorkflowEvaluation:
    evaluation_id: str
    schema_version: str
    workflow_id: str
    workflow_checksum: str
    created_at: str
    stage_evaluations: Tuple[StageEvaluation, ...]
    transition_evaluations: Tuple[TransitionEvaluation, ...]
    workflow_evaluation: WorkflowMetrics
    evidence: Tuple[str, ...]
    unknowns: Tuple[str, ...]
    growth_claim: str
    growth_evidence: Tuple[str, ...]
    owner_review_status: str
    checksum: str = ""

    def calculated_checksum(self) -> str:
        data=asdict(self); data.pop("checksum")
        return hashlib.sha256(json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    def sealed(self): return replace(self,checksum=self.calculated_checksum())
    def validate(self, workflow: PersonaWorkflow) -> None:
        workflow.validate()
        if self.schema_version!=SCHEMA_VERSION or self.workflow_id!=workflow.workflow_id or self.workflow_checksum!=workflow.checksum: raise EvaluationError("workflow binding failed")
        if self.growth_claim not in GROWTH_CLAIMS or self.checksum!=self.calculated_checksum(): raise EvaluationError("invalid evaluation")

def _ratio(n: int, d: int) -> str: return "UNKNOWN" if d == 0 else f"{n}/{d}"

def evaluate_transition(source_stage: str, target_stage: str, source: Tuple[EvidenceItem,...], target: Tuple[EvidenceItem,...], transforms: Mapping[str,str] | None = None) -> TransitionEvaluation:
    for item in source+target: item.validate()
    by_key={x.semantic_key:x for x in target}; transforms=transforms or {}; ledger=[]; source_keys={x.semantic_key for x in source}
    for item in source:
        found=by_key.get(item.semantic_key)
        if found is None:
            mapped=transforms.get(item.semantic_key); found=by_key.get(mapped) if mapped else None
            status="TRANSFORMED_VALID" if found else "LOST"
        elif found.classification!=item.classification: status="MUTATED"
        elif found.text.startswith("NOT "): status="CONTRADICTED"
        else: status="PRESERVED"
        contradiction="CONFIRMED" if status=="CONTRADICTED" else "NONE"
        ledger.append(LedgerEntry(source_stage,target_stage,item.item_id,item.classification,status,(item.item_id,found.item_id if found else "MISSING"),contradiction))
    mapped_targets=set(transforms.values())
    for item in target:
        if item.semantic_key not in source_keys and item.semantic_key not in mapped_targets:
            ledger.append(LedgerEntry(source_stage,target_stage,item.item_id,item.classification,"UNSUPPORTED_ADDITION",(item.item_id,),"UNKNOWN"))
    def metric(name,kind):
        entries=[x for x in ledger if x.classification==kind and x.status!="UNSUPPORTED_ADDITION"]
        good=sum(x.status in {"PRESERVED","TRANSFORMED_VALID"} for x in entries)
        return Metric(name,"UNKNOWN" if not entries else ("PASS" if good==len(entries) else "FAIL"),good,len(entries),tuple(x.item_id for x in entries))
    metrics=(metric("requirement_preservation","REQUIREMENT"),metric("constraint_preservation","CONSTRAINT"),metric("unknown_preservation","UNKNOWN"),Metric("classification_preservation","PASS" if not any(x.status=="MUTATED" for x in ledger) else "FAIL",sum(x.status!="MUTATED" for x in ledger),len(ledger),tuple(x.item_id for x in ledger)))
    return TransitionEvaluation(source_stage,target_stage,tuple(ledger),metrics)

def create_evaluation(evaluation_id: str, workflow: PersonaWorkflow, created_at: str, stage_evidence: Mapping[str,Tuple[EvidenceItem,...]], owner_corrections: Tuple[str,...]=(), *, enabled: bool=False) -> WorkflowEvaluation:
    if not enabled: raise EvaluationError("evaluation feature disabled")
    workflow.validate()
    for value in owner_corrections:
        if value not in OWNER_CORRECTIONS: raise EvaluationError("invalid Owner correction classification")
    page,eva,beg=(stage_evidence.get(x,()) for x in ("PAGE","EVA","BEG"))
    ta=evaluate_transition("PAGE","EVA",page,eva); tb=evaluate_transition("EVA","BEG",eva,beg)
    ledger=ta.ledger+tb.ledger
    def rate(kind):
        xs=[x for x in ledger if x.classification==kind and x.status!="UNSUPPORTED_ADDITION"]
        return _ratio(sum(x.status in {"PRESERVED","TRANSFORMED_VALID"} for x in xs),len(xs))
    stages=tuple(StageEvaluation(s,(Metric("structured_evidence","PASS" if stage_evidence.get(s) else "UNKNOWN",len(stage_evidence.get(s,())),len(stage_evidence.get(s,())),tuple(x.item_id for x in stage_evidence.get(s,()))),)) for s in ("PAGE","EVA","BEG"))
    wm=WorkflowMetrics(rate("REQUIREMENT"),rate("CONSTRAINT"),rate("UNKNOWN"),_ratio(sum(x.status!="MUTATED" for x in ledger),len(ledger)),"2/2",sum(x.status=="UNSUPPORTED_ADDITION" for x in ledger),sum(x.status=="CONTRADICTED" for x in ledger),len(owner_corrections),0,0)
    result=WorkflowEvaluation(evaluation_id,SCHEMA_VERSION,workflow.workflow_id,workflow.checksum,created_at,stages,(ta,tb),wm,tuple(x.item_id for x in page+eva+beg),tuple(x.item_id for x in ledger if x.status in {"LOST","MUTATED","UNRESOLVED"}),"NO_EVIDENCE",("single workflow is not growth evidence",),"PENDING_OWNER_REVIEW").sealed()
    result.validate(workflow); return result

def compare_growth(previous: WorkflowMetrics | None, current: WorkflowMetrics | None, *, comparable: bool, canon_delta: bool=False, authority_delta: bool=False, permission_delta: bool=False, skill_delta: bool=False, easier_task: bool=False) -> str:
    if previous is None or current is None: return "NO_EVIDENCE"
    if not comparable or canon_delta or authority_delta or permission_delta or skill_delta or easier_task: return "INSUFFICIENT_EVIDENCE"
    keys=("unsupported_addition_count","contradiction_count","owner_correction_count","role_boundary_violation_count","authority_violation_count")
    changes=[getattr(current,k)-getattr(previous,k) for k in keys]
    if any(x>0 for x in changes) and any(x<0 for x in changes): return "MIXED"
    if any(x>0 for x in changes): return "REGRESSION_OBSERVED"
    if any(x<0 for x in changes): return "IMPROVEMENT_OBSERVED"
    return "NO_EVIDENCE"

AUTO_GROWTH_WRITE=False
AUTO_KNOWLEDGE_PROMOTION=False
AUTO_CANON_PROMOTION=False
