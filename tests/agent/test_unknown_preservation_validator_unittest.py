"""H6-13G deterministic per-UNKNOWN preservation validator tests."""
from __future__ import annotations
import json
import unittest
from dataclasses import asdict
from agent.persona.provider_observation import ResolutionEvidence,validate_unknown_preservation

SRC="The source of the pulses is unknown."
SRC2="The number of observers is unknown."

class UnknownValidatorTests(unittest.TestCase):
    def one(self,candidate,expected,*,source=SRC):
        result=validate_unknown_preservation((source,),(candidate,) if candidate is not None else ())
        self.assertEqual(result.ledger[0].classification,expected)
        self.assertEqual(result.aggregate_pass,expected in {"PRESERVED","PARAPHRASED_PRESERVED","RESOLVED_WITH_EVIDENCE"})
        return result
    def test_a_exact(self): self.one(SRC,"PRESERVED")
    def test_b_case(self): self.one(SRC.upper(),"PRESERVED")
    def test_c_whitespace(self): self.one("  The  source of the pulses  is unknown.  ","PRESERVED")
    def test_d_punctuation(self): self.one("The source of the pulses is unknown!","PRESERVED")
    def test_e_enumerated_reformulation(self): self.one("The source of the pulses remains unknown.","PARAPHRASED_PRESERVED")
    def test_f_arbitrary_paraphrase_fails_closed(self): self.one("We do not know what produced the pulses.","UNVERIFIABLE")
    def test_g_omitted(self): self.one(None,"DROPPED")
    def test_h_unsupported_fact(self): self.one("The source of the pulses is the fictional beacon.","CERTAINTY_ESCALATED")
    def test_i_opposite_definite(self): self.one("The source of the pulses is known.","CERTAINTY_ESCALATED")
    def test_j_exact_substring_inside_negation(self): self.one("It is not true that the source of the pulses is unknown.","CONTRADICTED")
    def test_k_different_uncertainty(self): self.one("The timing of the pulses is unknown.","MUTATED")
    def test_l_partial_proposition(self): self.one("The source is unknown.","MUTATED")
    def test_m_two_preserved(self):
        r=validate_unknown_preservation((SRC,SRC2),(SRC,SRC2)); self.assertTrue(r.aggregate_pass); self.assertEqual(r.preserved_count,2)
    def test_n_one_omitted(self):
        r=validate_unknown_preservation((SRC,SRC2),(SRC,)); self.assertFalse(r.aggregate_pass); self.assertEqual([x.classification for x in r.ledger],["PRESERVED","DROPPED"])
    def test_o_reordered(self):
        r=validate_unknown_preservation((SRC,SRC2),(SRC2,SRC)); self.assertTrue(r.aggregate_pass); self.assertEqual(r.preserved_count,2)
    def test_p_one_contradicted(self):
        bad="It is not true that the number of observers is unknown."
        r=validate_unknown_preservation((SRC,SRC2),(SRC,bad)); self.assertFalse(r.aggregate_pass); self.assertEqual(r.contradicted_count,1)
    def test_q_resolution_with_bound_evidence(self):
        pre=validate_unknown_preservation((SRC,),())
        uid=pre.ledger[0].unknown_id; key=pre.ledger[0].source_proposition_key
        evidence=ResolutionEvidence("evidence-1",uid,key,"The source of the pulses is beacon A.")
        r=validate_unknown_preservation((SRC,),(evidence.resolved_text,),resolution_evidence=(evidence,))
        self.assertTrue(r.aggregate_pass); self.assertEqual(r.ledger[0].classification,"RESOLVED_WITH_EVIDENCE"); self.assertEqual(r.ledger[0].resolution_evidence_id,"evidence-1")
    def test_r_unbound_resolution(self): self.one("The source of the pulses is beacon A.","CERTAINTY_ESCALATED")
    def test_s_empty_candidate(self): self.one("","DROPPED")
    def test_t_unicode_japanese(self): self.one("光源は不明です！","PRESERVED",source="光源は不明です。")
    def test_u_repeated_execution_is_identical(self):
        a=validate_unknown_preservation((SRC,SRC2),(SRC2,SRC)); b=validate_unknown_preservation((SRC,SRC2),(SRC2,SRC))
        self.assertEqual(json.dumps(asdict(a),sort_keys=True),json.dumps(asdict(b),sort_keys=True))
    def test_v_duplicate_sources_have_stable_distinct_ids(self):
        r=validate_unknown_preservation((SRC,SRC),(SRC,)); self.assertEqual(r.preserved_count,1); self.assertEqual(r.dropped_count,1); self.assertNotEqual(r.ledger[0].unknown_id,r.ledger[1].unknown_id)
    def test_w_one_candidate_cannot_satisfy_overlapping_sources(self):
        other="The source of the red pulses is unknown."
        r=validate_unknown_preservation((SRC,other),(other,)); self.assertFalse(r.aggregate_pass); self.assertEqual(sum(x.classification in {"PRESERVED","PARAPHRASED_PRESERVED"} for x in r.ledger),1)

if __name__=="__main__": unittest.main()
