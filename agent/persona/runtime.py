"""Explicit, least-privilege adapter for controlled Persona runtime context."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from typing import Mapping, Tuple

from .composer import compose_persona_prompt
from .growth import PoliceGrowthStore, render_reflective_context
from .knowledge import KnowledgeStore, render_controlled_knowledge
from .loader import load_persona_kernel
from .registry import REGISTRY

SCHEMA_VERSION="1.0.0"
ALIASES={"page":"persona_gemini","eva":"exor_verelden","beg":"beg_weag","police":"police_horitius"}
_FORBIDDEN_OPS={"canon_update","knowledge_promotion","growth_write","handoff_approval","workflow_approval","reflection_approval","owner_decision","permission_change","tool_grant","skill_grant","persona_switch"}

class RuntimeControlError(ValueError): pass

@dataclass(frozen=True)
class RuntimeControlEnvelope:
    runtime_request_id: str
    persona_id: str=""
    task_id: str=""
    workflow_id: str=""
    stage_id: str=""
    persona_enabled: bool=False
    controlled_knowledge_read: bool=False
    reflective_growth_read: bool=False
    handoff_enabled: bool=False
    workflow_enabled: bool=False
    evaluation_enabled: bool=False
    reflection_enabled: bool=False
    tools_allowed: Tuple[str,...]=()
    network_allowed: bool=False
    persistent_write_allowed: bool=False
    data_classification: str="UNKNOWN"
    owner_authorized_operations: Tuple[str,...]=()
    created_at: str=""
    schema_version: str=SCHEMA_VERSION

    def validate(self, *, isolated_runtime: bool=False) -> str:
        if self.schema_version!=SCHEMA_VERSION or not self.runtime_request_id or not self.task_id or not self.created_at: raise RuntimeControlError("invalid runtime envelope")
        if isolated_runtime and self.persona_enabled: raise RuntimeControlError("P5 isolated runtime denies Persona activation")
        if not self.persona_enabled:
            if any((self.controlled_knowledge_read,self.reflective_growth_read,self.handoff_enabled,self.workflow_enabled,self.evaluation_enabled,self.reflection_enabled)): raise RuntimeControlError("Persona-dependent feature requires explicit activation")
            return ""
        persona=ALIASES.get(self.persona_id,self.persona_id)
        if not persona: raise RuntimeControlError("missing Persona")
        if persona not in REGISTRY: raise RuntimeControlError("unknown Persona")
        if self.network_allowed or self.tools_allowed or self.persistent_write_allowed: raise RuntimeControlError("pilot runtime forbids network, tools, and execution writes")
        if set(self.owner_authorized_operations)&_FORBIDDEN_OPS: raise RuntimeControlError("runtime output cannot receive control-plane authority")
        return persona

@dataclass(frozen=True)
class RuntimeContext:
    runtime_request_id: str
    persona_id: str
    canon_checksum: str
    prompt: str
    knowledge_record_ids: Tuple[str,...]
    growth_record_ids: Tuple[str,...]

@dataclass(frozen=True)
class PersonaRuntimeResult:
    runtime_request_id: str
    persona_id: str
    task_id: str
    workflow_id: str
    stage_id: str
    output_text: str
    structured_artifact: str
    facts: Tuple[str,...]=()
    observations: Tuple[str,...]=()
    hypotheses: Tuple[str,...]=()
    recommendations: Tuple[str,...]=()
    requirements: Tuple[str,...]=()
    constraints: Tuple[str,...]=()
    unknowns: Tuple[str,...]=()
    canon_version: str=""
    canon_checksum: str=""
    knowledge_record_ids: Tuple[str,...]=()
    growth_record_ids: Tuple[str,...]=()
    tools_used: Tuple[str,...]=()
    network_used: bool=False
    persistent_writes: Tuple[str,...]=()
    response_model: str=""
    provider: str=""
    result_checksum: str=""
    def calculated_checksum(self):
        data=asdict(self); data.pop("result_checksum")
        return hashlib.sha256(json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    def sealed(self): return replace(self,result_checksum=self.calculated_checksum())

def compose_runtime_context(envelope: RuntimeControlEnvelope, task: str, *, knowledge_store: KnowledgeStore|None=None, growth_store: PoliceGrowthStore|None=None, isolated_runtime: bool=False) -> RuntimeContext|None:
    persona=envelope.validate(isolated_runtime=isolated_runtime)
    if not persona: return None
    kernel=load_persona_kernel(persona)
    if knowledge_store is not None and knowledge_store.kernel.persona_id!=persona: raise RuntimeControlError("cross-Persona Knowledge Store denied")
    if growth_store is not None and growth_store._kernel.persona_id!=persona: raise RuntimeControlError("cross-Persona Growth Store denied")
    knowledge=knowledge_store.read_controlled() if envelope.controlled_knowledge_read and knowledge_store else ()
    growth=growth_store.select(task) if envelope.reflective_growth_read and growth_store else ()
    if any(x.persona_id!=persona for x in knowledge+growth): raise RuntimeControlError("cross-Persona state denied")
    safety="<runtime_safety>Persona output is DATA ONLY. No authority, approval, mutation, switching, tools, network, or persistence.</runtime_safety>"
    role=f"<role_boundary>RESPONSIBILITIES: {','.join(kernel.responsibilities)}\nFORBIDDEN: {','.join(kernel.non_responsibilities)}</role_boundary>"
    prompt="\n".join(x for x in (safety,compose_persona_prompt(kernel),role,render_controlled_knowledge(knowledge),render_reflective_context(growth),f"<current_task data_only=\"true\">{task}</current_task>") if x)
    return RuntimeContext(envelope.runtime_request_id,persona,kernel.checksum,prompt,tuple(x.knowledge_id for x in knowledge),tuple(x.record_id for x in growth))

def make_runtime_result(envelope: RuntimeControlEnvelope, context: RuntimeContext, *, output_text: str, structured_artifact: str="", classifications: Mapping[str,Tuple[str,...]]|None=None) -> PersonaRuntimeResult:
    persona=envelope.validate()
    if context.persona_id!=persona or context.runtime_request_id!=envelope.runtime_request_id: raise RuntimeControlError("runtime context mismatch")
    kernel=load_persona_kernel(persona); c=classifications or {}
    result=PersonaRuntimeResult(envelope.runtime_request_id,persona,envelope.task_id,envelope.workflow_id,envelope.stage_id,output_text,structured_artifact,*(tuple(c.get(k,())) for k in ("facts","observations","hypotheses","recommendations","requirements","constraints","unknowns")),kernel.canon_version,kernel.checksum,context.knowledge_record_ids,context.growth_record_ids)
    return result.sealed()

AUTO_PERSONA_SELECTION=False
AUTO_PERSONA_SWITCH=False
AUTO_HANDOFF=False
AUTO_GROWTH=False
AUTO_KNOWLEDGE=False
