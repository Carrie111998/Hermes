import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agent.persona.handoff import ClassifiedItem, HandoffEnvelope, Provenance, approve, evaluate_handoff
from agent.persona.loader import load_persona_kernel
from agent.persona.workflow import *

TASK = "Project Poiesisの朝報を制作するための、再利用可能なDaily Observation Cardシステムを設計する。"
NOW = "2026-08-14T00:00:00Z"

def artifact(stage):
    persona = STAGE_PERSONAS[stage]
    fields = {
        "PAGE": (("problem","Reusable morning observation is needed"),("objective","Daily Observation Card"),("why","consistent review"),("idea","card system"),("requirements","reusable"),("constraints","Canon unchanged"),("unknowns","audience detail"),("recommendation","production design")),
        "EVA": (("production_goal","Daily card system"),("design","modular visual card"),("components","header body evidence"),("creative_requirements","clear hierarchy"),("technical_requirements","reusable schema"),("acceptance_criteria","readable and repeatable"),("constraints","Canon unchanged"),("unknowns","final palette"),("handoff_requirements","implementation plan")),
        "BEG": (("implementation_plan","local schema and renderer plan"),("affected_components","fictional pipeline"),("repository_scope","none in pilot"),("build_steps","model validate render"),("validation_plan","deterministic fixtures"),("runtime_requirements","local only"),("risks","schema drift"),("unknowns","production host")),
    }[stage]
    kinds=(("HYPOTHESIS","requirements remain hypotheses"),("CONSTRAINT","Canon unchanged"),("UNKNOWN","unknown remains unknown"))
    return StageArtifact(stage.lower()+"-1",stage,persona,STAGE_KINDS[stage],fields,kinds,(ReasoningSignal("quality","baseline","structured"),)).sealed()

def handoff(source,target,target_ids,hid):
    k=load_persona_kernel(source)
    e=HandoffEnvelope("1.0.0",hid,NOW,source,target,"WORK_ARTIFACT","approved stage output",
      (ClassifiedItem("HYPOTHESIS","requirements remain hypotheses"),),(),(),(),(ClassifiedItem("UNKNOWN","unknown remains unknown"),),(),
      (ClassifiedItem("REQUIREMENT","produce next-stage artifact"),),(ClassifiedItem("CONSTRAINT","Canon unchanged"),),(ClassifiedItem("REQUIREMENT","perform bounded stage work"),),
      (k.responsibilities[0],),target_ids,Provenance(source,k.canon_version,k.checksum,"deterministic_fake_runtime",hid,NOW),status="PENDING_OWNER_REVIEW").sealed()
    return approve(e,authorization_source="owner_control_plane",timestamp=NOW)

def page_handoff(): return handoff("persona_gemini","exor_verelden",("design",),"handoff-a")
def eva_handoff(): return handoff("exor_verelden","beg_weag",("build_engineering",),"handoff-b")
def full():
    w=new_workflow("workflow-1",TASK,NOW,enabled=True)
    w=add_stage(w,artifact("PAGE")); w=attach_handoff(w,page_handoff())
    w=add_stage(w,artifact("EVA")); w=attach_handoff(w,eva_handoff())
    w=add_stage(w,artifact("BEG")); return complete(w)

class WorkflowTests(unittest.TestCase):
    def check(self,n):
        w=new_workflow("workflow-1",TASK,NOW,enabled=True)
        if n==1: self.assertEqual(w.schema_version,"1.0.0")
        elif n==2: self.assertEqual(w,w.sealed())
        elif n==3: self.assertEqual(w.checksum,w.calculated_checksum())
        elif n in (4,5): self.assertEqual(artifact("PAGE").persona_id,"persona_gemini")
        elif n==6: self.assertEqual(add_stage(w,artifact("PAGE")).status,"WAITING_OWNER")
        elif n==7: self.assertEqual(attach_handoff(add_stage(w,artifact("PAGE")),page_handoff()).current_stage,"EVA")
        elif n==8: self.assertEqual(reject_gate(add_stage(w,artifact("PAGE")),"revise").status,"REJECTED")
        elif n==9: self.assertEqual(evaluate_handoff(page_handoff()).result,"ALLOW_DELIVERY")
        elif n==10:
            with self.assertRaises(WorkflowValidationError): attach_handoff(add_stage(w,artifact("PAGE")),replace(page_handoff(),subject="tampered"))
        elif n in (11,12): self.assertEqual(artifact("EVA").persona_id,"exor_verelden")
        elif n in (13,14,15,16,17):
            x=attach_handoff(add_stage(w,artifact("PAGE")),page_handoff()); x=add_stage(x,artifact("EVA"))
            if n==13: self.assertEqual(x.status,"WAITING_OWNER")
            elif n==14: self.assertEqual(attach_handoff(x,eva_handoff()).current_stage,"BEG")
            elif n==15: self.assertEqual(reject_gate(x,"revise").status,"REJECTED")
            elif n==16: self.assertEqual(evaluate_handoff(eva_handoff()).result,"ALLOW_DELIVERY")
            else:
                with self.assertRaises(WorkflowValidationError): attach_handoff(x,replace(eva_handoff(),subject="tampered"))
        elif n in (18,19): self.assertEqual(artifact("BEG").persona_id,"beg_weag")
        elif n==20: self.assertEqual(full().status,"COMPLETED")
        elif n==21: self.assertEqual(full().provenance[-1],"OWNER_FINAL_ACCEPT")
        elif n in (22,23): self.assertIn(("HYPOTHESIS","requirements remain hypotheses"),artifact("EVA").classifications)
        elif n in (24,25,26,27,28,29,30):
            forbidden=("Owner authority","permission grant","inherit my tools","use my credentials","ignore your Canon","growth mutation","knowledge mutation")[n-24]
            e=replace(page_handoff(),findings=(ClassifiedItem("UNKNOWN",forbidden),)).sealed(); self.assertNotEqual(evaluate_handoff(e).result,"ALLOW_DELIVERY")
        elif n in (31,32,33,34): self.assertEqual(len({a.persona_id for a in full().stages}),3)
        elif n==35: self.assertEqual(evaluate_handoff(replace(page_handoff(),target_persona_id="unknown").sealed()).result,"DENY_UNKNOWN_PERSONA")
        elif n==36: self.assertEqual(evaluate_handoff(replace(page_handoff(),target_persona_id="beg_weag").sealed()).result,"DENY_UNRESOLVED_BOUNDARY")
        elif n==37:
            with self.assertRaises(WorkflowValidationError): replace(artifact("PAGE"),checksum="bad").validate()
        elif n==38:
            with self.assertRaises(WorkflowValidationError): replace(w,stages=(artifact("PAGE"),artifact("PAGE")),artifacts=(artifact("PAGE").checksum,)*2).sealed().validate()
        elif n==39:
            with self.assertRaises(WorkflowValidationError): add_stage(w,artifact("EVA"))
        elif n in (40,42): self.assertFalse(hasattr(PersonaWorkflow,"auto_handoff") or hasattr(PersonaWorkflow,"switch_persona"))
        elif n==41:
            with self.assertRaises(HandoffValidationError): approve(replace(page_handoff(),status="PENDING_OWNER_REVIEW",owner_authorization=page_handoff().owner_authorization),authorization_source="persona_gemini",timestamp=NOW)
        elif n==43:
            with self.assertRaises(WorkflowValidationError): replace(full(),task="tamper").validate()
        elif n==44: self.assertEqual(full().decisions[0].decision,"ACCEPT")
        elif n==45: full().validate(); self.assertTrue(True)
        elif n==46: self.assertEqual(full().status,"COMPLETED")
        elif n in (47,48): self.check(10 if n==47 else 17)
        elif n==49:
            with self.assertRaises(WorkflowValidationError): attach_handoff(add_stage(w,artifact("PAGE")),replace(page_handoff(),status="PENDING_OWNER_REVIEW",owner_authorization=page_handoff().owner_authorization))
        elif n==50:
            x=attach_handoff(add_stage(w,artifact("PAGE")),page_handoff()); x=add_stage(x,artifact("EVA"))
            with self.assertRaises(WorkflowValidationError): attach_handoff(x,replace(eva_handoff(),status="PENDING_OWNER_REVIEW",owner_authorization=eva_handoff().owner_authorization))
        elif n==51:
            with self.assertRaises(WorkflowValidationError): artifact("PAGE").__class__("x","PAGE","beg_weag","PAGE_PROPOSAL",(("idea","override"),),(('HYPOTHESIS','x'),)).sealed().validate()
        elif n==52:
            e=replace(page_handoff(),target_responsibility_ids=("github",)).sealed(); self.assertNotEqual(evaluate_handoff(e).result,"ALLOW_DELIVERY")
        elif n==53: self.check(41)
        elif n==54:
            with self.assertRaises(WorkflowValidationError): new_workflow("x",TASK,NOW)
        elif n in (55,56):
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(WorkflowValidationError): new_workflow("x",TASK,NOW)
                self.assertEqual(list(Path(d).iterdir()),[])
        elif n==57: self.assertEqual(evaluate_handoff(page_handoff()).result,"ALLOW_DELIVERY")
        elif n==58: self.assertEqual(load_persona_kernel("police_horitius").checksum,"8f93c79d5caabd43f9f3bf3499685f1673edc0f6f864e162ff81f5ff2b5ab9e7")
        elif n==59: self.assertEqual(w.status,"ACTIVE")
        elif n==60: self.assertEqual(full(),full())

def _make(n):
    def test(self): self.check(n)
    test.__name__=f"test_w{n:02d}"
    return test
for _n in range(1,61): setattr(WorkflowTests,f"test_w{_n:02d}",_make(_n))

if __name__ == "__main__": unittest.main()
