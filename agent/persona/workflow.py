"""Deterministic, Owner-controlled Page -> EVA -> BEG workflow pilot."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from typing import Mapping, Tuple

from .handoff import HandoffEnvelope, HandoffValidationError, delivery_payload, evaluate_handoff
from .loader import load_persona_kernel

SCHEMA_VERSION = "1.0.0"
STAGES = ("PAGE", "OWNER_GATE_A", "EVA", "OWNER_GATE_B", "BEG", "OWNER_FINAL_REVIEW", "COMPLETE")
STATUSES = ("DRAFT", "ACTIVE", "WAITING_OWNER", "REJECTED", "COMPLETED", "FAILED", "QUARANTINED")
STAGE_PERSONAS = {"PAGE": "persona_gemini", "EVA": "exor_verelden", "BEG": "beg_weag"}
STAGE_KINDS = {"PAGE": "PAGE_PROPOSAL", "EVA": "PRODUCTION_SPEC", "BEG": "BUILD_PLAN"}

class WorkflowValidationError(ValueError): pass

@dataclass(frozen=True)
class ReasoningSignal:
    name: str
    before: str
    after: str

@dataclass(frozen=True)
class StageArtifact:
    artifact_id: str
    stage: str
    persona_id: str
    artifact_type: str
    fields: Tuple[Tuple[str, str], ...]
    classifications: Tuple[Tuple[str, str], ...]
    reasoning_signals: Tuple[ReasoningSignal, ...] = ()
    checksum: str = ""

    def calculated_checksum(self) -> str:
        data = asdict(self); data.pop("checksum")
        return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    def sealed(self) -> "StageArtifact": return replace(self, checksum=self.calculated_checksum())
    def validate(self) -> None:
        if self.stage not in STAGE_PERSONAS or self.persona_id != STAGE_PERSONAS[self.stage] or self.artifact_type != STAGE_KINDS[self.stage]: raise WorkflowValidationError("stage responsibility boundary violation")
        if not self.artifact_id or not self.fields or self.checksum != self.calculated_checksum(): raise WorkflowValidationError("malformed or modified stage artifact")
        allowed = {"FACT", "OBSERVATION", "HYPOTHESIS", "RECOMMENDATION", "REQUIREMENT", "CONSTRAINT", "UNKNOWN"}
        if any(kind not in allowed for kind, _ in self.classifications): raise WorkflowValidationError("unclassified claim")

@dataclass(frozen=True)
class WorkflowDecision:
    gate: str
    decision: str
    reason: str
    handoff_checksum: str

@dataclass(frozen=True)
class PersonaWorkflow:
    workflow_id: str
    schema_version: str
    created_at: str
    task: str
    task_classification: str
    current_stage: str
    status: str
    owner_controlled: bool
    stages: Tuple[StageArtifact, ...]
    handoffs: Tuple[HandoffEnvelope, ...]
    artifacts: Tuple[str, ...]
    provenance: Tuple[str, ...]
    decisions: Tuple[WorkflowDecision, ...] = ()
    checksum: str = ""

    def content(self) -> Mapping[str, object]:
        data = asdict(self); data.pop("checksum")
        return data
    def calculated_checksum(self) -> str:
        return hashlib.sha256(json.dumps(self.content(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    def sealed(self) -> "PersonaWorkflow": return replace(self, checksum=self.calculated_checksum())
    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION or self.current_stage not in STAGES or self.status not in STATUSES or not self.owner_controlled: raise WorkflowValidationError("malformed workflow")
        if self.checksum != self.calculated_checksum(): raise WorkflowValidationError("workflow checksum mismatch")
        seen = set()
        order = []
        for artifact in self.stages:
            artifact.validate()
            if artifact.stage in seen: raise WorkflowValidationError("duplicate stage")
            seen.add(artifact.stage); order.append(artifact.stage)
        expected = [s for s in ("PAGE", "EVA", "BEG") if s in seen]
        if order != expected: raise WorkflowValidationError("skipped or reordered stage")
        if tuple(a.checksum for a in self.stages) != self.artifacts: raise WorkflowValidationError("artifact checksum chain mismatch")
        for index, handoff in enumerate(self.handoffs):
            if index >= len(self.decisions) or self.decisions[index].handoff_checksum != handoff.checksum: raise WorkflowValidationError("Owner decision checksum chain mismatch")

def new_workflow(workflow_id: str, task: str, created_at: str, *, enabled: bool = False) -> PersonaWorkflow:
    if not enabled: raise WorkflowValidationError("controlled workflow feature disabled")
    return PersonaWorkflow(workflow_id, SCHEMA_VERSION, created_at, task, "FICTIONAL_LOCAL_PILOT", "PAGE", "ACTIVE", True, (), (), (), ("OWNER_TASK",)).sealed()

def add_stage(workflow: PersonaWorkflow, artifact: StageArtifact) -> PersonaWorkflow:
    workflow.validate(); artifact.validate()
    expected = ("PAGE", "EVA", "BEG")[len(workflow.stages)] if len(workflow.stages) < 3 else None
    if artifact.stage != expected: raise WorkflowValidationError("skipped stage")
    gate = {"PAGE": "OWNER_GATE_A", "EVA": "OWNER_GATE_B", "BEG": "OWNER_FINAL_REVIEW"}[artifact.stage]
    return replace(workflow, stages=workflow.stages+(artifact,), artifacts=workflow.artifacts+(artifact.checksum,), provenance=workflow.provenance+(artifact.checksum,), current_stage=gate, status="WAITING_OWNER", checksum="").sealed()

def attach_handoff(workflow: PersonaWorkflow, handoff: HandoffEnvelope) -> PersonaWorkflow:
    workflow.validate()
    if workflow.current_stage not in {"OWNER_GATE_A", "OWNER_GATE_B"}: raise WorkflowValidationError("handoff not expected")
    expected_source = "persona_gemini" if workflow.current_stage == "OWNER_GATE_A" else "exor_verelden"
    if handoff.source_persona_id != expected_source: raise WorkflowValidationError("handoff source mismatch")
    decision = evaluate_handoff(handoff)
    if decision.result != "ALLOW_DELIVERY": raise WorkflowValidationError(decision.result)
    delivery_payload(handoff)
    gate = workflow.current_stage
    record = WorkflowDecision(gate, "ACCEPT", "explicit Owner control-plane approval", handoff.checksum)
    next_stage = "EVA" if gate == "OWNER_GATE_A" else "BEG"
    return replace(workflow, handoffs=workflow.handoffs+(handoff,), decisions=workflow.decisions+(record,), provenance=workflow.provenance+(handoff.checksum,"OWNER_ACCEPT"), current_stage=next_stage, status="ACTIVE", checksum="").sealed()

def reject_gate(workflow: PersonaWorkflow, reason: str) -> PersonaWorkflow:
    workflow.validate()
    if workflow.current_stage not in {"OWNER_GATE_A", "OWNER_GATE_B", "OWNER_FINAL_REVIEW"} or not reason.strip(): raise WorkflowValidationError("explicit gate rejection required")
    record = WorkflowDecision(workflow.current_stage, "REJECT", reason.strip(), "")
    return replace(workflow, decisions=workflow.decisions+(record,), status="REJECTED", checksum="").sealed()

def complete(workflow: PersonaWorkflow) -> PersonaWorkflow:
    workflow.validate()
    if workflow.current_stage != "OWNER_FINAL_REVIEW" or tuple(a.stage for a in workflow.stages) != ("PAGE","EVA","BEG"): raise WorkflowValidationError("workflow incomplete")
    return replace(workflow, current_stage="COMPLETE", status="COMPLETED", provenance=workflow.provenance+("OWNER_FINAL_ACCEPT",), checksum="").sealed()

def assert_persona_identity(artifact: StageArtifact) -> str:
    artifact.validate()
    return load_persona_kernel(artifact.persona_id).checksum
