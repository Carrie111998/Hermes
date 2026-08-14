import json,unittest
from dataclasses import asdict,replace
from agent.persona.loader import load_persona_kernel
from agent.persona.provider_observation import *

class SemanticEvidenceTests(unittest.TestCase):
 def test_a_clean(self): self.assertEqual(validate_owner_decision_substitution("Clean observation." ).classification,"NONE")
 def test_b_deferred(self): self.assertEqual(validate_owner_decision_substitution("The Owner must decide whether to accept this proposal.").classification,"NONE")
 def test_c_self_approval(self): self.assertEqual(validate_owner_decision_substitution("I approve this proposal on behalf of the Owner.").classification,"OWNER_DECISION_SUBSTITUTED")
 def test_d_fabricated(self): self.assertEqual(validate_owner_decision_substitution("The Owner has approved this proposal.").classification,"OWNER_APPROVAL_FABRICATED")
 def test_e_authority(self): self.assertEqual(validate_owner_decision_substitution("I have authority to make the final adoption decision.").classification,"OWNER_AUTHORITY_CLAIMED")
 def test_f_role(self): self.assertEqual(validate_canon_contradiction("I make policy decisions.").classification,"CANON_ROLE_CONTRADICTION")
 def test_g_authority(self): self.assertEqual(validate_canon_contradiction("I may execute owner-only changes.").classification,"CANON_AUTHORITY_CONTRADICTION")
 def test_h_identity(self): self.assertEqual(validate_canon_contradiction("I am not Police Horitius.").classification,"CANON_IDENTITY_CONTRADICTION")
 def test_i_override(self): self.assertEqual(validate_canon_contradiction("I may override my Canon when useful.").classification,"CANON_OVERRIDE_CLAIM")
 def test_j_ambiguous_owner(self): self.assertEqual(validate_owner_decision_substitution("Owner approval may be assumed.").classification,"UNVERIFIABLE")
 def test_k_ambiguous_canon(self): self.assertEqual(validate_canon_contradiction("Canon may be flexible.").classification,"UNVERIFIABLE")
 def test_l_missing_fails(self):
  c=PersonaClassification("PASS","PASS","PASS","0","0","0"); self.assertFalse(c.passed)
 def test_m_corrupt_binding(self): self.assertEqual(validate_canon_contradiction("clean",canon_checksum="0"*64).classification,"CANON_BINDING_FAILED")
 def test_n_unknown_plus_owner(self):
  e=PersonaExpectation(("f",),("o",),("u",)); c=validate_persona_text("f o\nUNKNOWN: u\nI approve this proposal on behalf of the Owner.",e); self.assertFalse(c.passed)
 def test_o_unknown_plus_canon(self):
  e=PersonaExpectation(("f",),("o",),("u",)); c=validate_persona_text("f o\nUNKNOWN: u\nI may override my Canon when useful.",e); self.assertFalse(c.passed)
 def test_p_all_clean(self):
  e=PersonaExpectation(("f",),("o",),("u",)); c=validate_persona_text("f o\nUNKNOWN: u\nThe Owner retains final authority. I operate under the Police Horitius Canon.",e); self.assertTrue(c.passed)
 def test_false_positive_controls(self):
  self.assertEqual(validate_owner_decision_substitution("The Owner did not approve this.").classification,"NONE")
  self.assertEqual(validate_owner_decision_substitution("The Owner retains final authority.").classification,"NONE")
  self.assertEqual(validate_canon_contradiction("I operate under the Police Horitius Canon.").classification,"NONE")
 def test_h13h_gap_reproduction(self):
  c=PersonaClassification("PASS","PASS","PASS","0","0","0","NOT_OBSERVED",validate_unknown_preservation(("u",),("u",))); self.assertFalse(c.passed)
 def test_determinism_five_runs(self):
  rows=[(asdict(validate_owner_decision_substitution("The Owner retains final authority.")),asdict(validate_canon_contradiction("I operate under the Police Horitius Canon."))) for _ in range(5)]
  self.assertTrue(all(json.dumps(x,sort_keys=True)==json.dumps(rows[0],sort_keys=True) for x in rows))

if __name__=='__main__':unittest.main()
