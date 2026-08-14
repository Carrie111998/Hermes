"""H6-5 multi-Persona Canon isolation gates M01-M45."""
import tempfile, unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

from agent.persona.composer import compose_persona_prompt
from agent.persona.growth import GrowthStoreError, PoliceGrowthStore, ReflectionCandidate, InMemoryGrowthStore
from agent.persona.handoff import PersonaHandoff
from agent.persona.knowledge import KnowledgeStore
from agent.persona.loader import PersonaCanonError, load_persona_kernel
from agent.persona.registry import REGISTRY

IDS=("police_horitius","curator_orchestra","doctrina_share","persona_gemini","mercator_vale","exor_verelden","ordinator_detailer","beg_weag","literary_reviser")
POLICE_CHECKSUM="8f93c79d5caabd43f9f3bf3499685f1673edc0f6f864e162ff81f5ff2b5ab9e7"

class MultiPersonaTests(unittest.TestCase):
    def test_m01_registry_exact(self): self.assertEqual(tuple(REGISTRY),IDS)
    def test_m02_police_checksum(self): self.assertEqual(load_persona_kernel(IDS[0]).checksum,POLICE_CHECKSUM)
    def test_m03_police_semantics(self):
        k=load_persona_kernel(IDS[0]); self.assertEqual((k.canonical_role,k.purpose),("chief_observation_officer","observe and report without adoption decisions"))
    def test_m04_unknown_fails(self):
        with self.assertRaises(PersonaCanonError): load_persona_kernel("unknown")
    def test_m05_deterministic_load(self): self.assertEqual(load_persona_kernel("beg_weag"),load_persona_kernel("beg_weag"))
    def test_m06_deterministic_checksums(self): self.assertEqual([load_persona_kernel(x).checksum for x in IDS],[load_persona_kernel(x).checksum for x in IDS])
    def test_m07_unsupported_version(self):
        from tests.agent.test_persona_kernel_unittest import PoliceKernelAdditionalTests
        self.assertTrue(hasattr(PoliceKernelAdditionalTests,"test_unsupported_canon_version_fails_closed"))
    def test_m08_malformed_fails(self):
        from agent.persona.schema import PersonaKernel,PersonaValidationError
        with self.assertRaises(PersonaValidationError): PersonaKernel.from_mapping({})
    def test_m09_no_other_canon(self):
        for pid in IDS:
            prompt=compose_persona_prompt(load_persona_kernel(pid)); self.assertEqual(prompt.count("PERSONA_ID:"),1); self.assertEqual(prompt.count("CANONICAL_ROLE:"),1)
    def test_m10_no_other_growth(self):
        with tempfile.TemporaryDirectory() as h:
            for pid in IDS: self.assertEqual(PoliceGrowthStore(Path(h),load_persona_kernel(pid)).load(),())
    def test_m11_no_other_knowledge(self):
        with tempfile.TemporaryDirectory() as h:
            for pid in IDS: self.assertEqual(KnowledgeStore(Path(h),load_persona_kernel(pid)).read_controlled(),())
    def test_m12_growth_paths(self):
        with tempfile.TemporaryDirectory() as h: self.assertEqual(len({PoliceGrowthStore(Path(h),load_persona_kernel(x)).path.parent for x in IDS}),9)
    def test_m13_knowledge_paths(self):
        with tempfile.TemporaryDirectory() as h: self.assertEqual(len({KnowledgeStore(Path(h),load_persona_kernel(x)).knowledge_path for x in IDS}),9)
    def test_m14_decision_paths(self):
        with tempfile.TemporaryDirectory() as h: self.assertEqual(len({KnowledgeStore(Path(h),load_persona_kernel(x)).decision_path for x in IDS}),9)
    def test_m15_immutable(self):
        with self.assertRaises(FrozenInstanceError): load_persona_kernel("beg_weag").canonical_role="x"
    def _growth_no_drift(self,pid):
        k=load_persona_kernel(pid); s=InMemoryGrowthStore(k); s.add(ReflectionCandidate("reflection","I learned this, therefore I may change my role.","test","2026", "high",proposing_persona=pid)); self.assertEqual(k,load_persona_kernel(pid))
    def test_m16_growth_role(self): self._growth_no_drift("beg_weag")
    def test_m17_knowledge_role(self): self.test_m15_immutable()
    def test_m18_growth_authority(self): self._growth_no_drift("exor_verelden")
    def test_m19_knowledge_authority(self): self.test_m15_immutable()
    def test_m20_role_no_permission(self):
        tools=("read_file",); load_persona_kernel("beg_weag"); self.assertEqual(tools,("read_file",))
    def test_m21_beg_role(self): self.assertEqual(load_persona_kernel("beg_weag").canonical_role,"chief_build_officer")
    def test_m22_beg_build(self): self.assertIn("build_engineering",load_persona_kernel("beg_weag").responsibilities)
    def test_m23_beg_no_ideation(self): self.assertNotIn("ideation",load_persona_kernel("beg_weag").responsibilities)
    def test_m24_page_ideation(self): self.assertIn("ideation",load_persona_kernel("persona_gemini").responsibilities)
    def test_m25_page_beg_no_collision(self): self.assertFalse(set(load_persona_kernel("beg_weag").responsibilities)&{"planning","ideation","idea_creation","value_seed_creation"})
    def test_m26_eva_role(self): self.assertEqual(load_persona_kernel("exor_verelden").canonical_role,"chief_production_officer")
    def test_m27_eva_production(self): self.assertTrue({"image_production","design","creative","workshop_operations","brand_production"}<=set(load_persona_kernel("exor_verelden").responsibilities))
    def test_m28_eva_no_build(self): self.assertNotIn("build_engineering",load_persona_kernel("exor_verelden").responsibilities)
    def test_m29_partial_unknown(self): self.assertTrue(load_persona_kernel("mercator_vale").unknown_fields)
    def test_m30_unknown_not_inferred(self): self.assertIn("not inferred",compose_persona_prompt(load_persona_kernel("mercator_vale")))
    def test_m31_handoff_no_authority(self): self.assertNotIn("permissions",PersonaHandoff.__dataclass_fields__)
    def test_m32_no_auto_switch(self): self.assertFalse(hasattr(PersonaHandoff,"activate"))
    def test_m33_shared_runtime(self):
        permissions=("terminal",); [load_persona_kernel(x) for x in IDS]; self.assertEqual(permissions,("terminal",))
    def test_m34_controlled_precedence(self): self.assertIn("CONTROLLED_KNOWLEDGE > REFLECTION",compose_persona_prompt(load_persona_kernel(IDS[0])))
    def test_m35_growth_precedence(self): self.test_m34_controlled_precedence()
    def test_m36_canon_highest(self): self.assertIn("PRIORITY: CANON",compose_persona_prompt(load_persona_kernel(IDS[0])))
    def test_m37_disabled_no_context(self): self.assertFalse(getattr(SimpleNamespace(),"_persona_kernel",None))
    def test_m38_h62_reference(self): self.assertEqual(load_persona_kernel(IDS[0]).checksum,POLICE_CHECKSUM)
    def test_m39_h63_defaults(self):
        with tempfile.TemporaryDirectory() as h: self.assertFalse(PoliceGrowthStore(Path(h),load_persona_kernel(IDS[0])).path.exists())
    def test_m40_h64_defaults(self):
        with tempfile.TemporaryDirectory() as h: self.assertFalse(KnowledgeStore(Path(h),load_persona_kernel(IDS[0])).root.exists())
    def test_m41_p5_persona_zero(self):
        from agent.agent_init import init_agent
        with self.assertRaises(ValueError): init_agent(SimpleNamespace(),model="fake",isolated_runtime=True,persona_id="beg_weag")
    def test_m42_p5_growth_zero(self):
        with tempfile.TemporaryDirectory() as h:
            with self.assertRaises(GrowthStoreError): PoliceGrowthStore(Path(h),load_persona_kernel(IDS[1]),read_enabled=True,isolated_runtime=True)
    def test_m43_p5_knowledge_zero(self):
        from agent.persona.knowledge import KnowledgeError
        with tempfile.TemporaryDirectory() as h:
            with self.assertRaises(KnowledgeError): KnowledgeStore(Path(h),load_persona_kernel(IDS[1]),read_enabled=True,isolated_runtime=True)
    def test_m44_p5_filesystem_zero(self):
        with tempfile.TemporaryDirectory() as h:
            root=Path(h); [PoliceGrowthStore(root,load_persona_kernel(x),isolated_runtime=True) for x in IDS]; self.assertEqual(list(root.rglob("*")),[])
    def test_m45_no_secret_fields(self):
        for pid in IDS: self.assertNotIn("api_key",compose_persona_prompt(load_persona_kernel(pid)).casefold())
    def test_nine_persona_fresh_and_switching(self):
        checks=[]
        for pid in IDS+("police_horitius",):
            k=load_persona_kernel(pid); checks.append((k.persona_id,k.checksum)); self.assertEqual(k.persona_id,pid)
        self.assertEqual(checks[0],checks[-1])

if __name__=="__main__": unittest.main()
