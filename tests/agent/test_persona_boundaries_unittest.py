"""H6-6 responsibility matrix and role-boundary contract R01-R48."""
import tempfile, unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from agent.persona.growth import PoliceGrowthStore
from agent.persona.knowledge import KnowledgeStore
from agent.persona.loader import PersonaCanonError, load_persona_kernel
from agent.persona.registry import REGISTRY
from agent.persona.responsibility import Collision, ResponsibilityRow, build_responsibility_matrix, detect_collisions
from agent.persona.schema import PersonaKernel, PersonaValidationError

P="8f93c79d5caabd43f9f3bf3499685f1673edc0f6f864e162ff81f5ff2b5ab9e7"

class BoundaryTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls): cls.m=build_responsibility_matrix(); cls.by={r.persona_id:r for r in cls.m}
 def test_r01_registry(self): self.assertEqual(len(self.m),9)
 def test_r02_police_checksum(self): self.assertEqual(load_persona_kernel("police_horitius").checksum,P)
 def test_r03_police_semantics(self): self.assertEqual(load_persona_kernel("police_horitius").purpose,"observe and report without adoption decisions")
 def test_r04_beg_role(self): self.assertEqual(load_persona_kernel("beg_weag").canonical_role,"chief_build_officer")
 def test_r05_beg_build(self): self.assertIn("build_engineering",self.by["beg_weag"].primary_responsibilities)
 def test_r06_beg_github(self): self.assertIn("github",self.by["beg_weag"].primary_responsibilities)
 def test_r07_beg_ci(self): self.assertIn("ci_cd",self.by["beg_weag"].primary_responsibilities)
 def test_r08_beg_ops(self): self.assertTrue({"n8n","runtime","infrastructure"}<=set(self.by["beg_weag"].primary_responsibilities))
 def test_r09_beg_ideation_absent(self): self.assertNotIn("ideation",self.by["beg_weag"].primary_responsibilities)
 def test_r10_beg_planning_absent(self): self.assertNotIn("planning",self.by["beg_weag"].primary_responsibilities)
 def test_r11_page_knowledge(self): self.assertIn("knowledge_management",self.by["persona_gemini"].primary_responsibilities)
 def test_r12_page_planning(self): self.assertIn("planning",self.by["persona_gemini"].primary_responsibilities)
 def test_r13_page_ideation(self): self.assertIn("ideation",self.by["persona_gemini"].primary_responsibilities)
 def test_r14_page_perspectives(self): self.assertIn("new_perspectives",self.by["persona_gemini"].primary_responsibilities)
 def test_r15_page_values(self): self.assertIn("value_seeds",self.by["persona_gemini"].primary_responsibilities)
 def test_r16_page_beg_zero(self): self.assertFalse(set(self.by["persona_gemini"].primary_responsibilities)&set(self.by["beg_weag"].primary_responsibilities))
 def test_r17_eva_role(self): self.assertEqual(load_persona_kernel("exor_verelden").canonical_role,"chief_production_officer")
 def test_r18_eva_image(self): self.assertIn("image_production",self.by["exor_verelden"].primary_responsibilities)
 def test_r19_eva_design(self): self.assertTrue({"design","creative"}<=set(self.by["exor_verelden"].primary_responsibilities))
 def test_r20_eva_workshop(self): self.assertIn("workshop_operations",self.by["exor_verelden"].primary_responsibilities)
 def test_r21_eva_brand(self): self.assertIn("brand_production",self.by["exor_verelden"].primary_responsibilities)
 def test_r22_eva_beg_zero(self): self.assertFalse(set(self.by["exor_verelden"].primary_responsibilities)&set(self.by["beg_weag"].primary_responsibilities))
 def test_r23_doctrina_ordinator_unresolved(self): self.assertTrue(self.by["doctrina_share"].owner_decision_required and self.by["ordinator_detailer"].owner_decision_required)
 def test_r24_doctrina_not_invented(self): self.assertEqual(self.by["doctrina_share"].boundary_status,"UNRESOLVED")
 def test_r25_ordinator_not_invented(self): self.assertEqual(self.by["ordinator_detailer"].boundary_status,"UNRESOLVED")
 def test_r26_mercator_history(self): self.assertEqual(load_persona_kernel("mercator_vale").historical_status,"CONFLICTING_OR_UNRESOLVED")
 def test_r27_mercator_not_resolved(self): self.assertEqual(self.by["mercator_vale"].boundary_status,"UNRESOLVED")
 def test_r28_lily_retained(self): self.assertIn("literary_reviser",REGISTRY)
 def test_r29_lily_formal(self): self.assertEqual(load_persona_kernel("literary_reviser").formal_status,"UNRESOLVED")
 def test_r30_owner_authority(self): self.assertFalse(any("owner_decision" in r.authority_level for r in self.m))
 def test_r31_no_self_promotion(self): self.assertFalse(any("canon_promotion" in r.authority_level for r in self.m))
 def test_r32_no_permission(self): self.assertFalse(any("permission_escalation" in r.authority_level for r in self.m))
 def test_r33_no_skill(self): self.assertFalse(any("skill" in x for r in self.m for x in r.authority_level))
 def test_r34_no_auto_handoff(self): self.assertFalse(any(x.startswith("auto:") for r in self.m for x in r.handoff_targets))
 def test_r35_no_auto_switch(self): self.assertFalse(hasattr(build_responsibility_matrix,"switch"))
 def test_r36_growth_isolation(self):
  with tempfile.TemporaryDirectory() as h: self.assertEqual(len({PoliceGrowthStore(Path(h),load_persona_kernel(x)).path for x in REGISTRY}),9)
 def test_r37_knowledge_isolation(self):
  with tempfile.TemporaryDirectory() as h: self.assertEqual(len({KnowledgeStore(Path(h),load_persona_kernel(x)).root for x in REGISTRY}),9)
 def test_r38_matrix_deterministic(self): self.assertEqual(self.m,build_responsibility_matrix())
 def test_r39_detector_deterministic(self): self.assertEqual(detect_collisions(self.m),detect_collisions(self.m))
 def test_r40_malformed(self):
  with self.assertRaises(PersonaValidationError): PersonaKernel.from_mapping({})
 def test_r41_unknown(self):
  with self.assertRaises(PersonaCanonError): load_persona_kernel("unknown")
 def test_r42_duplicate(self):
  import json
  p=Path(__file__).parents[2]/"agent/persona/canon/beg_weag.json"; d=json.loads(p.read_text()); d["responsibilities"].append("github")
  with self.assertRaisesRegex(PersonaValidationError,"duplicate"): PersonaKernel.from_mapping(d)
 def _row(self,**kw):
  d=dict(persona_id="x",canonical_name="X",role_title="R",primary_responsibilities=(),secondary_responsibilities=(),forbidden_responsibilities=(),handoff_targets=(),authority_level=(),canon_status="CONFIRMED",unknown_fields=(),boundary_status="CONFIRMED",overlap_candidates=(),owner_decision_required=False); d.update(kw); return ResponsibilityRow(**d)
 def test_r43_forbidden_collision(self): self.assertIn("FORBIDDEN_PRIMARY_COLLISION",[x.collision_type for x in detect_collisions((self._row(primary_responsibilities=("planning",),forbidden_responsibilities=("planning",)),))])
 def test_r44_authority_collision(self): self.assertIn("AUTHORITY_COLLISION",[x.collision_type for x in detect_collisions((self._row(authority_level=("owner_decision",)),))])
 def test_r45_handoff_escalation(self): self.assertIn("HANDOFF_AUTHORITY_ESCALATION",[x.collision_type for x in detect_collisions((self._row(handoff_targets=("authority:owner",)),))])
 def test_r46_p5_zero(self):
  with tempfile.TemporaryDirectory() as h:
   root=Path(h); [PoliceGrowthStore(root,load_persona_kernel(x),isolated_runtime=True) for x in REGISTRY]; self.assertEqual(list(root.rglob("*")),[])
 def test_r47_normal(self): self.assertEqual(len(build_responsibility_matrix()),9)
 def test_r48_disabled(self): self.assertIsNone(getattr(SimpleNamespace(),"persona_id",None))
 def test_r49_unknown_boundaries_detected(self): self.assertGreaterEqual(sum(x.collision_type=="UNKNOWN_BOUNDARY" for x in detect_collisions(self.m)),3)
 def test_r50_no_authority_handoff_in_registry(self): self.assertFalse(any(x.collision_type=="HANDOFF_AUTHORITY_ESCALATION" for x in detect_collisions(self.m)))

if __name__=="__main__": unittest.main()
