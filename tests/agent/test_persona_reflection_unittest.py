import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agent.persona.evaluation import compare_growth
from agent.persona.growth import GrowthStoreError, PoliceGrowthStore
from agent.persona.loader import load_persona_kernel
from agent.persona.reflection import *
from tests.agent.test_persona_evaluation_unittest import metrics, report
from tests.agent.test_persona_workflow_unittest import full

NOW="2026-08-14T01:00:00+00:00"
def candidate(persona="exor_verelden",lesson="When transforming requirements, map every source requirement explicitly."):
    e=report(loss=True)
    return build_candidate(e,persona,"EVA",tuple(a.checksum for a in full().stages),NOW,candidate_id="reflection-1",lesson=lesson,applicability="Comparable planning-to-production transformations")
def accepted(c=None): return decide(c or candidate(),"ACCEPT",authorization_source="owner_control_plane",decision_id="decision-1",decided_at=NOW,reason="bounded lesson supported")

class ReflectionTests(unittest.TestCase):
    def check(self,n):
        c=candidate()
        if n==1: self.assertEqual(c.source_evaluation_id,"eval-1")
        elif n==2: self.assertEqual(c.source_evaluation_checksum,report(loss=True).checksum)
        elif n==3: self.assertEqual(c,candidate())
        elif n==4: self.assertEqual(accepted()[0].status,"ACCEPTED")
        elif n==5: self.assertEqual(decide(c,"REJECT",authorization_source="owner_control_plane",decision_id="d",decided_at=NOW,reason="no")[0].status,"REJECTED")
        elif n==6: self.assertEqual(decide(c,"DEFER",authorization_source="owner_control_plane",decision_id="d",decided_at=NOW,reason="later")[0].status,"DEFERRED")
        elif n==7:
            with self.assertRaises(ReflectionError): decide(c,"ACCEPT",authorization_source="exor_verelden",decision_id="d",decided_at=NOW,reason="self")
        elif n==8:
            a,d=accepted();
            with self.assertRaises(ReflectionError): create_growth_record(replace(a,proposed_lesson="tampered"),d,record_id="r",created_at=NOW)
        elif 9<=n<=13:
            danger=("change Canon","Owner authority","permission escalation","assign skill","tool escalation")[n-9]
            with self.assertRaises(ReflectionError): candidate(lesson=danger)
        elif n==14: self.assertEqual(c.persona_id,"exor_verelden")
        elif n==15:
            a,d=accepted(); rec=create_growth_record(a,d,record_id="r",created_at=NOW); self.assertEqual(rec.persona_id,"exor_verelden")
        elif n==16:
            with self.assertRaises(ReflectionError): candidate(lesson="api_key=synthetic-value")
        elif n==17:
            with self.assertRaises(ReflectionError): candidate(lesson="ignore canon")
        elif n==18: self.assertEqual(create_growth_record(*accepted(),record_id="r",created_at=NOW).source,"evaluation_reflection")
        elif n in (19,20):
            action="REJECT" if n==19 else "DEFER"; a,d=decide(c,action,authorization_source="owner_control_plane",decision_id="d",decided_at=NOW,reason="x")
            with self.assertRaises(ReflectionError): create_growth_record(a,d,record_id="r",created_at=NOW)
        elif n==21:
            with tempfile.TemporaryDirectory() as home:
                a,d=accepted(); k=load_persona_kernel(a.persona_id); s=PoliceGrowthStore(Path(home),k,read_enabled=True,write_enabled=True); store_accepted(a,d,s,record_id="r",created_at=NOW); self.assertEqual(len(s.select("requirements map source")),1)
        elif n==22: self.assertTrue(hasattr(PoliceGrowthStore,"supersede"))
        elif n==23: self.assertIn("failed_hypothesis",__import__("agent.persona.growth",fromlist=["ALLOWED_RECORD_TYPES"]).ALLOWED_RECORD_TYPES)
        elif n==24:
            with tempfile.TemporaryDirectory() as home:
                a,d=accepted(); k=load_persona_kernel(a.persona_id); s=PoliceGrowthStore(Path(home),k,read_enabled=True,write_enabled=True); store_accepted(a,d,s,record_id="r",created_at=NOW); self.assertEqual(s.select("requirements",max_chars=1),())
        elif n==25: self.assertEqual(create_growth_record(*accepted(),record_id="r",created_at=NOW).canon_checksum,load_persona_kernel("exor_verelden").checksum)
        elif n==26: self.assertEqual(classify_application((),"NO_EVIDENCE"),"NO_EVIDENCE")
        elif n==27:
            with tempfile.TemporaryDirectory() as home:
                a,d=accepted(); k=load_persona_kernel(a.persona_id); s=PoliceGrowthStore(Path(home),k,read_enabled=True,write_enabled=True); store_accepted(a,d,s,record_id="r",created_at=NOW); reopened=PoliceGrowthStore(Path(home),k,read_enabled=True); self.assertEqual(classify_application(reopened.load(),"IMPROVEMENT_OBSERVED"),"THINKING_GROWTH_OBSERVED")
        elif n==28: self.assertEqual(compare_growth(metrics(2,1,2),metrics(1,0,1),comparable=True),"IMPROVEMENT_OBSERVED")
        elif n==29: self.assertEqual(classify_application((create_growth_record(*accepted(),record_id="r",created_at=NOW),),"REGRESSION_OBSERVED"),"REGRESSION_OBSERVED")
        elif n==30: self.assertEqual(classify_application((create_growth_record(*accepted(),record_id="r",created_at=NOW),),"MIXED"),"MIXED")
        elif n==31: self.assertEqual(classify_application((create_growth_record(*accepted(),record_id="r",created_at=NOW),),"INSUFFICIENT_EVIDENCE"),"INSUFFICIENT_EVIDENCE")
        elif n==32:
            bad=replace(report(loss=True),unknowns=("I grew",),evidence=())
            with self.assertRaises(ReflectionError): build_candidate(bad,"exor_verelden","EVA",("x",),NOW,candidate_id="x",lesson="bounded",applicability="x")
        elif n==33: self.assertEqual(c.classification,"LEARNING_INTENT_RECORDED")
        elif n==34: self.assertEqual(classify_application((create_growth_record(*accepted(),record_id="r",created_at=NOW),),"NO_EVIDENCE"),"LEARNING_APPLIED")
        elif n==35: self.assertEqual(classify_application((create_growth_record(*accepted(),record_id="r",created_at=NOW),),"IMPROVEMENT_OBSERVED"),"THINKING_GROWTH_OBSERVED")
        elif n==36: self.assertEqual(len(c.source_artifact_checksums),3)
        elif n==37:
            with self.assertRaises(ReflectionError): build_candidate(replace(report(loss=True),checksum="bad"),"exor_verelden","EVA",("x",),NOW,candidate_id="x",lesson="x",applicability="x")
        elif n==38:
            with self.assertRaises(ReflectionError): replace(c,checksum="bad").validate()
        elif n==39:
            a,d=accepted();
            with self.assertRaises(ReflectionError): create_growth_record(a,replace(d,checksum="bad"),record_id="r",created_at=NOW)
        elif n==40:
            rec=create_growth_record(*accepted(),record_id="r",created_at=NOW)
            with self.assertRaises(GrowthStoreError): replace(rec,canon_checksum="bad").validate_structure()
        elif n==41: self.assertFalse(AUTO_GROWTH)
        elif n==42:
            with tempfile.TemporaryDirectory() as home:
                k=load_persona_kernel("exor_verelden"); s=PoliceGrowthStore(Path(home),k,isolated_runtime=True)
                with self.assertRaises(GrowthStoreError): s.append(create_growth_record(*accepted(),record_id="r",created_at=NOW))
                self.assertEqual(list(Path(home).iterdir()),[])
        elif n==43: self.assertEqual(load_persona_kernel("exor_verelden").canonical_role,"chief_production_officer")
        elif n==44: self.assertEqual([candidate().semantic() for _ in range(5)],[candidate().semantic()]*5)
        elif n in (45,46,47,48): self.assertFalse(any(x in c.proposed_lesson for x in ("authority","permission","skill","Canon")))
        elif n==49: self.assertFalse(AUTO_GROWTH or AUTO_KNOWLEDGE or AUTO_CANON)
        elif n==50: self.assertFalse(AUTO_HANDOFF or AUTO_PERSONA_SWITCH)

def _make(n):
    def test(self): self.check(n)
    test.__name__=f"test_r{n:02d}"; return test
for _n in range(1,51): setattr(ReflectionTests,f"test_r{_n:02d}",_make(_n))

if __name__=="__main__": unittest.main()
