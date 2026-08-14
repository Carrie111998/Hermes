"""H6-13B bounded observation harness tests (stdlib, network-free)."""
from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from agent.persona.provider_observation import (
    BoundedAuditor, PersonaExpectation, ProviderObservationHarness,
    ResponseHeaders, TimeoutBudget, TransportFailure, default_audit_targets,
    duration_summary, validate_persona,
)

EXPECTED = PersonaExpectation(("fact-a",), ("observation-a",), ("unknown-a",))
SUCCESS = {
    "model": "fake/actual", "facts": ["fact-a"],
    "observations": ["observation-a"], "unknowns": ["unknown-a"],
    "unsupported_inferences": [], "authority_escalations": [], "role_violations": [],
}


class FakeTransport:
    is_fake = True
    def __init__(self, *, status=200, failure=None, body=None, before_open=0, before_first=0,
                 before_end=0, routing="fake-router"):
        self.status, self.failure = status, failure
        self.body = json.dumps(SUCCESS).encode() if body is None else body
        self.before_open, self.before_first, self.before_end = before_open, before_first, before_end
        self.routing = routing
    def open(self, requested_model, provider_timeout):
        if self.before_open: time.sleep(self.before_open)
        if self.failure and self.failure[0] == "open": raise TransportFailure(self.failure[1])
        def chunks():
            if self.before_first: time.sleep(self.before_first)
            if self.failure and self.failure[0] == "headers": raise TransportFailure(self.failure[1])
            midpoint = max(1, len(self.body)//2)
            yield self.body[:midpoint]
            if self.before_end: time.sleep(self.before_end)
            if self.failure and self.failure[0] == "first": raise TransportFailure(self.failure[1])
            yield self.body[midpoint:]
        return ResponseHeaders(self.status, self.routing), chunks()


class FlushStream(io.StringIO):
    def __init__(self): super().__init__(); self.flush_count=0
    def flush(self): self.flush_count+=1; return super().flush()


class MutatingAuditor(BoundedAuditor):
    def __init__(self, targets, path): super().__init__(targets); self.calls=0; self.path=path
    def snapshot(self):
        result=super().snapshot(); self.calls+=1
        if self.calls==1: self.path.write_text("unexpected", encoding="utf-8")
        return result


class HarnessTests(unittest.TestCase):
    def run_case(self, transport, *, auditor_cls=BoundedAuditor, budget=None, parser=json.loads):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td); targets=default_audit_targets(home); stream=FlushStream()
            auditor = auditor_cls(targets) if auditor_cls is BoundedAuditor else auditor_cls(targets, home/"SOUL.md")
            harness=ProviderObservationHarness(auditor=auditor,stream=stream,budget=budget or TimeoutBudget(),parser=parser)
            result=harness.run(transport=transport,requested_model="fake/requested",expectation=EXPECTED)
            result.durations["flush_count"]=stream.flush_count
            return result,stream.getvalue(),len(targets)

    def test_success_checkpoint_order_flush_and_accounting(self):
        result,out,count=self.run_case(FakeTransport())
        self.assertEqual(result.checkpoints[-1],"H13B_COMPLETE")
        self.assertEqual(out.splitlines(),result.checkpoints)
        self.assertEqual(result.durations["flush_count"],len(result.checkpoints))
        self.assertEqual(out.count("H13B_COMPLETE"),1)
        self.assertEqual(result.http_attempt_count,0)
        self.assertEqual(result.fake_transport_attempt_count,1)
        self.assertEqual((result.retry_count,result.fallback_count),(0,0))
        self.assertEqual(result.http_status,200)
        self.assertEqual(result.response_model,"fake/actual")
        self.assertEqual(result.filesystem_delta,())
        self.assertEqual(count,20)

    def test_matrix_exact_terminal_checkpoints(self):
        malformed=b"{"
        missing=json.dumps({**SUCCESS,"model":""}).encode()
        bad=json.dumps({**SUCCESS,"facts":["invented"]}).encode()
        cases={
            "A":(FakeTransport(),"H13B_COMPLETE"),
            "B":(FakeTransport(before_open=.001),"H13B_COMPLETE"),
            "C":(FakeTransport(before_first=.001),"H13B_COMPLETE"),
            "D":(FakeTransport(before_end=.001),"H13B_COMPLETE"),
            "E":(FakeTransport(status=400),"H13D_FAILED:HTTP:HTTP_400"),
            "F":(FakeTransport(status=401),"H13D_FAILED:HTTP:HTTP_401"),
            "G":(FakeTransport(status=404),"H13D_FAILED:HTTP:HTTP_404"),
            "H":(FakeTransport(status=429),"H13D_FAILED:HTTP:HTTP_429"),
            "I":(FakeTransport(status=500),"H13D_FAILED:HTTP:HTTP_500"),
            "J":(FakeTransport(failure=("open","CONNECT_OR_PRE_HEADER_TIMEOUT")),"H13D_FAILED:HTTP:CONNECT_OR_PRE_HEADER_TIMEOUT"),
            "K":(FakeTransport(failure=("headers","HEADER_RECEIVED_BODY_TIMEOUT")),"H13D_FAILED:HTTP:HEADER_RECEIVED_BODY_TIMEOUT"),
            "L":(FakeTransport(failure=("first","FIRST_BYTE_RECEIVED_BODY_TIMEOUT")),"H13D_FAILED:HTTP:FIRST_BYTE_RECEIVED_BODY_TIMEOUT"),
            "M":(FakeTransport(body=malformed),"H13D_FAILED:PARSE:MALFORMED_RESPONSE"),
            "N":(FakeTransport(body=missing),"H13D_FAILED:PARSE:MISSING_MODEL_IDENTITY"),
            "O":(FakeTransport(body=bad),"H13D_FAILED:PERSONA:PERSONA_VALIDATION_FAILED"),
            "P":(FakeTransport(),"H13D_FAILED:AUDIT:UNEXPECTED_WRITE"),
        }
        for name,(transport,terminal) in cases.items():
            with self.subTest(name=name):
                cls=MutatingAuditor if name=="P" else BoundedAuditor
                result,out,_=self.run_case(transport,auditor_cls=cls)
                self.assertEqual(result.checkpoints[-1],terminal)
                self.assertEqual(sum(x==terminal for x in result.checkpoints),1)
                self.assertNotRegex(out.lower(),r"authorization:|bearer |api[_ -]?key")
                self.assertEqual(result.http_attempt_count,0)
                self.assertEqual(result.fake_transport_attempt_count,1)
                self.assertEqual((result.retry_count,result.fallback_count),(0,0))
                self.assertIn("H13B_POST_AUDIT_COMPLETE",result.checkpoints)
                if name!="P": self.assertEqual(result.filesystem_delta,())
                if name=="P": self.assertEqual(result.response_state,"RESPONSE_COMPLETE")

    def test_exact_persona_classification(self):
        result,_,_=self.run_case(FakeTransport())
        c=result.persona_validation
        self.assertEqual((c.fact_preservation,c.observation_preservation,c.unknown_preservation),("PASS","PASS","PASS"))
        self.assertEqual((c.unsupported_inference,c.authority_escalation,c.role_violation),("0","0","0"))

    def test_timeout_budget_invariant_and_processing_class(self):
        invalid=TimeoutBudget(outer_timeout=5.0)
        result,_,_=self.run_case(FakeTransport(),budget=invalid)
        self.assertEqual(result.checkpoints[-1],"H13D_FAILED:OUTER:OUTER_TIMEOUT")
        self.assertTrue(TimeoutBudget().valid())
        def slow_parser(body):
            time.sleep(.05)
            return json.loads(body)
        result,_,_=self.run_case(FakeTransport(),budget=replace(TimeoutBudget(),processing_timeout=.001),parser=slow_parser)
        self.assertEqual(result.checkpoints[-1],"H13D_FAILED:PARSE:PROCESSING_TIMEOUT")

    def test_bounded_directory_delta_detected_without_recursion(self):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td); (home/"logs").mkdir(); deep=home/"logs"/"known.log"; deep.write_text("a")
            auditor=BoundedAuditor(default_audit_targets(home)); before=auditor.snapshot()
            (home/"logs"/"new.log").write_text("b"); after=auditor.snapshot()
            self.assertIn("logs",auditor.changed(before,after))

    def test_duration_measurement_repeated_min_median_max(self):
        values={k:[] for k in ("pre_audit","fake_transport","processing","post_audit")}
        for _ in range(5):
            result,_,_=self.run_case(FakeTransport())
            for key in values: values[key].append(result.durations[key])
        for samples in values.values():
            summary=duration_summary(samples)
            self.assertLessEqual(summary["min"],summary["median"])
            self.assertLessEqual(summary["median"],summary["max"])

    def test_stdout_stderr_secret_safety(self):
        result,out,_=self.run_case(FakeTransport(failure=("open","unsafe-error-detail")))
        self.assertEqual(result.safe_error_class,"TRANSPORT_ERROR")
        self.assertNotIn("unsafe-error-detail",out)

    def test_requested_and_response_model_are_distinct(self):
        result,_,_=self.run_case(FakeTransport())
        self.assertEqual(result.requested_model,"fake/requested")
        self.assertEqual(result.response_model,"fake/actual")

    def test_all_persona_failure_dimensions_are_exact(self):
        payload={**SUCCESS,"observations":["changed"],"unknowns":[],
                 "unsupported_inferences":["guess"],"authority_escalations":["approve"],
                 "role_violations":["decide"]}
        c=validate_persona(payload,EXPECTED)
        self.assertEqual(c.fact_preservation,"PASS")
        self.assertEqual(c.observation_preservation,"FAIL")
        self.assertEqual(c.unknown_preservation,"FAIL")
        self.assertEqual((c.unsupported_inference,c.authority_escalation,c.role_violation),
                         ("detected","detected","detected"))


if __name__ == "__main__": unittest.main()
