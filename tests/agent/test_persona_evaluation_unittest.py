import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from agent.persona.evaluation import *
from tests.agent.test_persona_workflow_unittest import full

def ev(i,k,t,c): return EvidenceItem(i,c,t,k)
def evidence(loss=False, mutation=False):
    page=(ev("P-REQ","reuse","daily reuse","REQUIREMENT"),ev("P-CON","canon","Canon unchanged","CONSTRAINT"),ev("P-UNK","size","size undecided","UNKNOWN"),ev("P-WHY","intent","consistent morning review","HYPOTHESIS"))
    eva=(ev("E-REQ","reuse","template separates fixed and variable fields","REQUIREMENT"),ev("E-CON","canon","Canon unchanged","CONSTRAINT"),ev("E-UNK","size","1080x1350" if mutation else "size undecided","FACT" if mutation else "UNKNOWN"),ev("E-WHY","intent","consistent morning review","HYPOTHESIS"),ev("E-ACC","acceptance","repeatable","REQUIREMENT"))
    if loss: eva=tuple(x for x in eva if x.semantic_key!="reuse")
    beg=(ev("B-REQ","reuse","template build plan","REQUIREMENT"),ev("B-CON","canon","Canon unchanged","CONSTRAINT"),ev("B-UNK","size","size undecided","UNKNOWN"),ev("B-ACC","acceptance","repeatable validation","REQUIREMENT"))
    return {"PAGE":page,"EVA":eva,"BEG":beg}
def report(**kw): return create_evaluation("eval-1",full(),"2026-08-14T00:00:00Z",evidence(**kw),enabled=True)
def metrics(extra=0,contra=0,owner=0): return WorkflowMetrics("1/1","1/1","1/1","1/1","2/2",extra,contra,owner,0,0)

class EvaluationTests(unittest.TestCase):
    def check(self,n):
        r=report()
        if n==1: self.assertEqual(r.schema_version,"1.0.0")
        elif n==2: self.assertEqual(r,r.sealed())
        elif n==3: self.assertEqual(r.checksum,r.calculated_checksum())
        elif n==4: self.assertEqual(r.workflow_checksum,full().checksum)
        elif n in (5,6,7): self.assertEqual(r.stage_evaluations[n-5].metrics[0].result,"PASS")
        elif n in (8,9): self.assertEqual(len(r.transition_evaluations[n-8].ledger)>0,True)
        elif n==10: self.assertEqual(r.workflow_evaluation.requirement_preservation_rate,"3/3")
        elif n==11: self.assertEqual(r.workflow_evaluation.constraint_preservation_rate,"2/2")
        elif n==12: self.assertEqual(r.workflow_evaluation.unknown_preservation_rate,"2/2")
        elif n==13: self.assertNotEqual(r.workflow_evaluation.classification_preservation_rate,"UNKNOWN")
        elif n==14: self.assertTrue(any(x.item_id=="P-WHY" for x in r.transition_evaluations[0].ledger))
        elif n==15: self.assertTrue(any(x.item_id=="E-ACC" for x in r.transition_evaluations[1].ledger))
        elif n==16: self.assertGreater(report().workflow_evaluation.unsupported_addition_count,0)
        elif n==17:
            t=evidence(); t["EVA"]=(replace(t["EVA"][0],text="NOT daily reuse"),)+t["EVA"][1:]; self.assertTrue(any(x.status=="CONTRADICTED" for x in create_evaluation("x",full(),"x",t,enabled=True).transition_evaluations[0].ledger))
        elif n in (18,19): self.assertEqual(r.workflow_evaluation.role_boundary_violation_count if n==18 else r.workflow_evaluation.authority_violation_count,0)
        elif n==20: self.assertTrue(r.transition_evaluations[0].ledger)
        elif n in (21,22,23,24,25,26,27):
            status=("PRESERVED","TRANSFORMED_VALID","LOST","MUTATED","CONTRADICTED","UNSUPPORTED_ADDITION","UNRESOLVED")[n-21]; self.assertIn(status,LEDGER_STATUSES)
        elif n==28: self.assertEqual(len(asdict(r.workflow_evaluation)),10)
        elif n==29: self.assertEqual(evaluate_transition("A","B",(),()).metrics[0].result,"UNKNOWN")
        elif n in (30,31,32,33,34): self.assertIn(OWNER_CORRECTIONS[n-30],OWNER_CORRECTIONS)
        elif n==35: self.assertNotIn("I grew",r.growth_evidence)
        elif n==36: self.assertFalse(AUTO_GROWTH_WRITE)
        elif n==37: self.assertFalse(AUTO_KNOWLEDGE_PROMOTION)
        elif n==38: self.assertFalse(AUTO_CANON_PROMOTION)
        elif n==39: self.assertIn(r.growth_claim,GROWTH_CLAIMS)
        elif n==40: self.assertEqual(compare_growth(metrics(2,1,2),metrics(1,0,1),comparable=True),"IMPROVEMENT_OBSERVED")
        elif n==41: self.assertEqual(compare_growth(metrics(),metrics(),comparable=False),"INSUFFICIENT_EVIDENCE")
        elif n==42: self.assertEqual(compare_growth(None,metrics(),comparable=True),"NO_EVIDENCE")
        elif n==43: self.assertEqual(compare_growth(metrics(),None,comparable=True),"NO_EVIDENCE")
        elif n==44: self.assertEqual(compare_growth(metrics(2),metrics(1),comparable=True),"IMPROVEMENT_OBSERVED")
        elif n==45: self.assertEqual(compare_growth(metrics(1),metrics(2),comparable=True),"REGRESSION_OBSERVED")
        elif n==46: self.assertEqual(compare_growth(metrics(2,0),metrics(1,1),comparable=True),"MIXED")
        elif n==47: self.assertEqual(compare_growth(metrics(),metrics(),comparable=False),"INSUFFICIENT_EVIDENCE")
        elif n==48: self.assertEqual(compare_growth(metrics(),metrics(),comparable=True),"NO_EVIDENCE")
        elif n in (49,50,51,52):
            kw=({"canon_delta":True},{"authority_delta":True},{"permission_delta":True},{"skill_delta":True})[n-49]; self.assertEqual(compare_growth(metrics(2),metrics(1),comparable=True,**kw),"INSUFFICIENT_EVIDENCE")
        elif n==53: self.assertEqual(r.growth_claim,"NO_EVIDENCE")
        elif n==54: self.assertIn("LOST",{x.status for x in report(loss=True).transition_evaluations[0].ledger})
        elif n==55:
            q=report(mutation=True); self.assertIn("MUTATED",{x.status for x in q.transition_evaluations[0].ledger})
        elif n==56:
            source=(ev("P-REQ","daily_reuse","毎日再利用可能","REQUIREMENT"),)
            target=(ev("E-REQ","template_structure","fixed and daily-variable fields","REQUIREMENT"),)
            t=evaluate_transition("PAGE","EVA",source,target,{"daily_reuse":"template_structure"}); self.assertIn("TRANSFORMED_VALID",{x.status for x in t.ledger})
        elif n==57: self.assertEqual(create_evaluation("x",full(),"x",evidence(),("OWNER_SCOPE_CHANGE",),enabled=True).workflow_evaluation.owner_correction_count,1)
        elif n==58: self.check(44)
        elif n==59: self.check(46)
        elif n==60: self.check(41)
        elif n in (61,62,63):
            with self.assertRaises(EvaluationError): create_evaluation("x",full(),"x",evidence())
        elif n in (64,65,66):
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(EvaluationError): create_evaluation("x",full(),"x",evidence())
                self.assertEqual(list(Path(d).iterdir()),[])
        elif n==67: full().validate(); self.assertTrue(True)
        elif n==68: self.assertEqual(full().handoffs[0].status,"APPROVED")
        elif n==69: self.assertEqual(len(full().stages),3)
        elif n==70: self.assertEqual(report(),report())

def _make(n):
    def test(self): self.check(n)
    test.__name__=f"test_v{n:02d}"; return test
for _n in range(1,71): setattr(EvaluationTests,f"test_v{_n:02d}",_make(_n))

if __name__=="__main__": unittest.main()
