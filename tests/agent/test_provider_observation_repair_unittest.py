"""H6-13D response normalization and failure-audit repair tests."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from agent.persona.provider_observation import (
    BoundedAuditor, PersonaExpectation, ProviderObservationHarness,
    ResponseHeaders, TimeoutBudget, TransportFailure, default_audit_targets,
    normalize_openrouter_response,
)

FACT="Observation Station B recorded three blue light pulses at 21:14."
OBS="The third pulse lasted longer than the first two."
HYP="A malfunction in the fictional beacon system is one possible explanation."
UNK="The source of the pulses is unknown."
TEXT=f"Fact: {FACT}\nObservation: {OBS}\nHypothesis: {HYP}\nUnknown: {UNK}"
EXPECT=PersonaExpectation((FACT,),(OBS,),(UNK,),(HYP,),
                          ("the malfunction is confirmed",),("I authorize",),("I order",))

def envelope(content=TEXT, *, model="liquid/actual", provider="Liquid"):
    return json.dumps({"model":model,"provider":provider,"choices":[{"message":{"content":content}}]}).encode()

class FakeTransport:
    is_fake=True
    def __init__(self,body=b"",status=200,failure=None): self.body,self.status,self.failure=body,status,failure
    def open(self,requested_model,provider_timeout):
        if self.failure=="before": raise TransportFailure("CONNECT_OR_PRE_HEADER_TIMEOUT")
        def chunks():
            if self.failure=="headers": raise TransportFailure("HEADER_RECEIVED_BODY_TIMEOUT")
            yield self.body[:max(1,len(self.body)//2)]
            if self.failure=="first": raise TransportFailure("FIRST_BYTE_RECEIVED_BODY_TIMEOUT")
            yield self.body[max(1,len(self.body)//2):]
        return ResponseHeaders(self.status),chunks()

class MutatingAuditor(BoundedAuditor):
    def __init__(self,targets,path): super().__init__(targets); self.path=path; self.calls=0
    def snapshot(self):
        state=super().snapshot(); self.calls+=1
        if self.calls==1: self.path.write_text("unexpected",encoding="utf-8")
        return state

class RepairTests(unittest.TestCase):
    def run_case(self,transport,*,mutate=False):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td); targets=default_audit_targets(home); stream=io.StringIO()
            auditor=MutatingAuditor(targets,home/"SOUL.md") if mutate else BoundedAuditor(targets)
            result=ProviderObservationHarness(auditor=auditor,stream=stream,budget=TimeoutBudget()).run(
                transport=transport,requested_model="liquid/requested",expectation=EXPECT)
            return result,stream.getvalue()

    def test_plain_text_openrouter_envelope_parses(self):
        result,_=self.run_case(FakeTransport(envelope()))
        self.assertEqual(result.checkpoints[-1],"H13B_COMPLETE")
        self.assertEqual(result.assistant_text,TEXT)
        self.assertEqual(result.response_model,"liquid/actual")
        self.assertEqual(result.routing_provider,"Liquid")

    def test_supported_structured_text_content_parses(self):
        content=[{"type":"text","text":TEXT[:30]},{"type":"output_text","text":TEXT[30:]}]
        result,_=self.run_case(FakeTransport(envelope(content)))
        self.assertEqual(result.checkpoints[-1],"H13B_COMPLETE")
        self.assertEqual(result.assistant_text,TEXT)

    def test_old_strict_content_json_class_fails_but_new_normalizer_passes(self):
        with self.assertRaises(json.JSONDecodeError): json.loads(TEXT)
        self.assertEqual(normalize_openrouter_response(envelope()).assistant_text,TEXT)

    def test_invalid_http_json_fails_then_audits(self):
        result,_=self.run_case(FakeTransport(b"{"))
        self.assertEqual(result.checkpoints[-1],"H13D_FAILED:PARSE:MALFORMED_RESPONSE")
        self.assertIn("H13B_POST_AUDIT_COMPLETE",result.checkpoints)
        self.assertEqual(result.filesystem_delta,())

    def test_missing_choices_message_content_are_structural_errors_and_audit(self):
        bodies=(
            {"model":"m"},
            {"model":"m","choices":[{}]},
            {"model":"m","choices":[{"message":{}}]},
        )
        for payload in bodies:
            with self.subTest(payload=payload):
                result,_=self.run_case(FakeTransport(json.dumps(payload).encode()))
                self.assertEqual(result.checkpoints[-1],"H13D_FAILED:PARSE:STRUCTURAL_RESPONSE_ERROR")
                self.assertIn("H13B_POST_AUDIT_COMPLETE",result.checkpoints)

    def test_missing_model_preserves_content_and_audits(self):
        result,_=self.run_case(FakeTransport(envelope(model="")))
        self.assertEqual(result.response_model,"UNKNOWN")
        self.assertEqual(result.assistant_text,TEXT)
        self.assertEqual(result.checkpoints[-1],"H13D_FAILED:PARSE:MISSING_MODEL_IDENTITY")
        self.assertIn("H13B_POST_AUDIT_COMPLETE",result.checkpoints)

    def test_persona_semantic_failure_audits(self):
        result,_=self.run_case(FakeTransport(envelope("Fact: invented")))
        self.assertEqual(result.checkpoints[-1],"H13D_FAILED:PERSONA:PERSONA_VALIDATION_FAILED")
        self.assertIn("H13B_POST_AUDIT_COMPLETE",result.checkpoints)
        self.assertFalse(result.persona_validation.passed)

    def test_http_statuses_and_timeouts_all_audit_once(self):
        cases=[FakeTransport(b"",status=x) for x in (400,401,404,429,500)] + [
            FakeTransport(failure=x) for x in ("before","headers","first")]
        for transport in cases:
            with self.subTest(status=transport.status,failure=transport.failure):
                result,_=self.run_case(transport)
                self.assertTrue(result.checkpoints[-1].startswith("H13D_FAILED:HTTP:"))
                self.assertEqual(result.checkpoints.count("H13B_POST_AUDIT_COMPLETE"),1)
                self.assertEqual((result.http_attempt_count,result.retry_count,result.fallback_count),(0,0,0))
                self.assertEqual(result.fake_transport_attempt_count,1)
                self.assertEqual(result.filesystem_delta,())

    def test_primary_and_audit_failures_are_both_preserved(self):
        result,_=self.run_case(FakeTransport(b"{"),mutate=True)
        self.assertEqual(result.primary_failure_class,"MALFORMED_RESPONSE")
        self.assertEqual(result.audit_failure_class,"UNEXPECTED_WRITE")
        self.assertEqual(result.checkpoints[-1],"H13D_FAILED:PARSE:MALFORMED_RESPONSE")
        self.assertEqual(result.response_state,"RESPONSE_COMPLETE")

    def test_post_audit_write_preserves_successful_response_evidence(self):
        result,_=self.run_case(FakeTransport(envelope()),mutate=True)
        self.assertEqual(result.checkpoints[-1],"H13D_FAILED:AUDIT:UNEXPECTED_WRITE")
        self.assertEqual(result.response_state,"RESPONSE_COMPLETE")
        self.assertEqual(result.parse_result,"PASS")
        self.assertTrue(result.persona_validation.passed)

if __name__=="__main__": unittest.main()
