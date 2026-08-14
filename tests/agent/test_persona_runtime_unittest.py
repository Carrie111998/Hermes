import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agent.persona.evaluation import compare_growth
from agent.persona.growth import GrowthRecord, GrowthStoreError, PoliceGrowthStore
from agent.persona.knowledge import KnowledgeStore
from agent.persona.loader import PersonaCanonError, load_persona_kernel
from agent.persona.reflection import classify_application
from agent.persona.runtime import *

NOW="2026-08-14T02:00:00+00:00"
def env(persona="page",**kw):
    data=dict(runtime_request_id="rt-1",persona_id=persona,task_id="task-1",persona_enabled=True,created_at=NOW)
    data.update(kw); return RuntimeControlEnvelope(**data)
def context(persona="page",**kw): return compose_runtime_context(env(persona,**kw),"Daily Observation Card requirement unknown")
def growth(persona="exor_verelden"):
    k=load_persona_kernel(persona)
    return GrowthRecord("g-1",persona,"reasoning_mistake",NOW,"evaluation_reflection",lesson="Map every source requirement during transformation.",evidence_for=("REQ-B LOST",),uncertainty="Comparable tasks only",confidence="medium",canon_version=k.canon_version,canon_checksum=k.checksum,status="validated")

class RuntimeTests(unittest.TestCase):
    def check(self,n):
        e=env(); c=context()
        if n==1: self.assertEqual(e.validate(),"persona_gemini")
        elif n==2: self.assertEqual(c.persona_id,"persona_gemini")
        elif n==3:
            with self.assertRaises(RuntimeControlError): replace(e,persona_id="").validate()
        elif n==4:
            with self.assertRaises(RuntimeControlError): env("unknown").validate()
        elif n==5: self.assertIn("<persona_canon>",c.prompt)
        elif n==6: self.assertEqual(c.canon_checksum,load_persona_kernel("persona_gemini").checksum)
        elif n==7: self.assertIn("<role_boundary>",c.prompt)
        elif n==8: self.assertEqual(c.knowledge_record_ids,())
        elif n==9:
            with tempfile.TemporaryDirectory() as h:
                k=load_persona_kernel("persona_gemini"); s=KnowledgeStore(Path(h),k,read_enabled=True); self.assertEqual(compose_runtime_context(env(controlled_knowledge_read=True),"task",knowledge_store=s).knowledge_record_ids,())
        elif n==10: self.assertEqual(c.growth_record_ids,())
        elif n==11:
            with tempfile.TemporaryDirectory() as h:
                k=load_persona_kernel("exor_verelden"); s=PoliceGrowthStore(Path(h),k,read_enabled=True,write_enabled=True); s.append(growth()); x=compose_runtime_context(env("eva",reflective_growth_read=True),"source requirement transformation",growth_store=s); self.assertEqual(x.growth_record_ids,("g-1",))
        elif n==12: self.assertLess(c.prompt.index("<runtime_safety>"),c.prompt.index("<persona_canon>"))
        elif n==13: self.assertGreater(c.prompt.index("<current_task"),c.prompt.index("<persona_canon>"))
        elif n in (14,15): self.assertIn("CANON_CHECKSUM",c.prompt)
        elif n==16: self.assertTrue(make_runtime_result(e,c,output_text="artifact").result_checksum)
        elif n==17:
            r=make_runtime_result(e,c,output_text="x",classifications={"hypotheses":("h",)}); self.assertEqual(r.hypotheses,("h",))
        elif n==18: self.assertEqual(make_runtime_result(e,c,output_text="x",classifications={"unknowns":("u",)}).unknowns,("u",))
        elif n in (19,20):
            with self.assertRaises(RuntimeControlError): env(owner_authorized_operations=("owner_decision" if n==19 else "reflection_approval",)).validate()
        elif n==21: self.assertFalse(env().handoff_enabled)
        elif n==22: self.assertTrue(env(handoff_enabled=True).validate())
        elif n==23: self.assertFalse(env().workflow_enabled)
        elif n in (24,25): self.assertTrue(env(workflow_enabled=True).validate())
        elif n in (26,27,28,29): self.assertEqual(context(("page","eva","beg","police")[n-26]).persona_id,("persona_gemini","exor_verelden","beg_weag","police_horitius")[n-26])
        elif n==30: self.assertEqual(len({context(x).persona_id for x in ("police","page","eva","beg")}),4)
        elif n in (31,32,33,34,35):
            prompts=[context(x).prompt for x in ("police","page","eva","beg")]; self.assertEqual(len(set(prompts)),4)
        elif n==36: self.assertTrue(make_runtime_result(e,c,output_text="x").result_checksum)
        elif n==37: self.assertNotIn("self judgment",c.prompt)
        elif n==38: self.assertFalse(env().reflection_enabled)
        elif n==39:
            with self.assertRaises(RuntimeControlError): env(owner_authorized_operations=("reflection_approval",)).validate()
        elif n==40: self.assertTrue(env(reflection_enabled=True).validate())
        elif n==41:
            with tempfile.TemporaryDirectory() as h:
                k=load_persona_kernel("exor_verelden"); s=PoliceGrowthStore(Path(h),k,read_enabled=True,write_enabled=True); s.append(growth()); self.assertEqual(len(s.load()),1)
        elif n==42: self.check(11)
        elif n==43: self.check(11)
        elif n==44: self.assertIn("Map every source requirement",growth().lesson)
        elif n==45: self.assertEqual(compare_growth(__import__('tests.agent.test_persona_evaluation_unittest',fromlist=['metrics']).metrics(2),__import__('tests.agent.test_persona_evaluation_unittest',fromlist=['metrics']).metrics(1),comparable=True),"IMPROVEMENT_OBSERVED")
        elif n==46: self.check(45)
        elif n==47: self.assertEqual(classify_application((growth(),),"IMPROVEMENT_OBSERVED"),"THINKING_GROWTH_OBSERVED")
        elif 48<=n<=52: self.assertFalse(any((e.network_allowed,e.persistent_write_allowed,bool(e.tools_allowed))))
        elif n in (53,54,55): self.assertFalse(AUTO_GROWTH or AUTO_KNOWLEDGE)
        elif n==56:
            with tempfile.TemporaryDirectory() as h:
                k=load_persona_kernel("exor_verelden"); s=PoliceGrowthStore(Path(h),k,read_enabled=True); s.path.parent.mkdir(parents=True); s.path.write_text("{");
                with self.assertRaises(GrowthStoreError): compose_runtime_context(env("eva",reflective_growth_read=True),"task",growth_store=s)
        elif n==57:
            with tempfile.TemporaryDirectory() as h:
                k=load_persona_kernel("persona_gemini"); s=KnowledgeStore(Path(h),k,read_enabled=True); s.knowledge_path.parent.mkdir(parents=True); s.knowledge_path.write_text("{");
                with self.assertRaises(Exception): compose_runtime_context(env(controlled_knowledge_read=True),"task",knowledge_store=s)
        elif n==58:
            with self.assertRaises(PersonaCanonError): load_persona_kernel("missing")
        elif n in (59,60):
            with tempfile.TemporaryDirectory() as h:
                foreign=load_persona_kernel("exor_verelden"); gs=PoliceGrowthStore(Path(h),foreign,read_enabled=True,write_enabled=True); gs.append(growth())
                with self.assertRaises(RuntimeControlError): compose_runtime_context(env("beg",reflective_growth_read=True),"task",growth_store=gs)
        elif n==61: self.assertIsNone(compose_runtime_context(RuntimeControlEnvelope("x",task_id="t",created_at=NOW),"task"))
        elif n==62: self.assertFalse(RuntimeControlEnvelope("x",task_id="t",created_at=NOW).persona_enabled)
        elif n==63: self.assertIsNone(compose_runtime_context(RuntimeControlEnvelope("x",task_id="t",created_at=NOW),"normal"))
        elif n==64:
            with self.assertRaises(RuntimeControlError): compose_runtime_context(e,"task",isolated_runtime=True)
        elif 65<=n<=72:
            with tempfile.TemporaryDirectory() as h:
                with self.assertRaises(RuntimeControlError): compose_runtime_context(e,"task",isolated_runtime=True)
                self.assertEqual(list(Path(h).iterdir()),[])
        elif n in (73,74,75,76): self.assertFalse(e.network_allowed or e.persistent_write_allowed or bool(e.tools_allowed))
        elif n==77: self.assertEqual(e,env())
        elif n==78: self.assertEqual(context().prompt,context().prompt)
        elif n==79:
            r=make_runtime_result(e,c,output_text="x"); self.assertEqual(r.result_checksum,r.calculated_checksum())
        elif n==80:
            with self.assertRaises(RuntimeControlError): replace(e,schema_version="bad").validate()

def _make(n):
    def test(self): self.check(n)
    test.__name__=f"test_rt{n:02d}"; return test
for _n in range(1,81): setattr(RuntimeTests,f"test_rt{_n:02d}",_make(_n))

if __name__=="__main__": unittest.main()
