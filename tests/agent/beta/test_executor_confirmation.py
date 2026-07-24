import json

from agent.beta.orchestrator import _parse_executor_confirmation


def test_structured_success_requires_explicit_status_and_evidence():
    status, evidence = _parse_executor_confirmation({
        "status": "completed",
        "evidence": "service active; health check=ok",
    })

    assert status == "completed"
    assert evidence == "service active; health check=ok"


def test_structured_failure_never_becomes_completed():
    status, evidence = _parse_executor_confirmation({
        "status": "failed",
        "evidence": "restart failed because the unit timed out",
    })

    assert status == "failed"
    assert "restart failed" in evidence


def test_json_confirmation_is_validated():
    status, evidence = _parse_executor_confirmation(json.dumps({
        "status": "success",
        "evidence": "process restarted; readiness probe=passing",
    }))

    assert status == "completed"
    assert evidence == "process restarted; readiness probe=passing"


def test_plain_failure_text_fails_closed():
    status, evidence = _parse_executor_confirmation("error: service restart timed out")

    assert status == "failed"
    assert evidence == "error: service restart timed out"


def test_malformed_structured_confirmation_fails_closed():
    status, evidence = _parse_executor_confirmation('{"status": "completed"')

    assert status == "failed"
    assert "malformed structured confirmation" in evidence


def test_structured_success_with_failure_evidence_fails_closed():
    status, evidence = _parse_executor_confirmation({
        "status": "completed",
        "evidence": "health check failed after restart",
    })

    assert status == "failed"
    assert "health check failed" in evidence
