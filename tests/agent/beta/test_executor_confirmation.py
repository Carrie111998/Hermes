import json

from agent.beta.orchestrator import _parse_executor_confirmation


EXPECTED_FINGERPRINT = "approved-operation"
EXPECTED_NONCE = "approval-receipt-nonce"


def _confirmation(
    *,
    status: str,
    evidence: str,
    fingerprint: str = EXPECTED_FINGERPRINT,
    approval_nonce: str = EXPECTED_NONCE,
):
    return {
        "operation_fingerprint": fingerprint,
        "approval_nonce": approval_nonce,
        "status": status,
        "evidence": evidence,
    }


def _parse(raw):
    return _parse_executor_confirmation(raw, EXPECTED_FINGERPRINT, EXPECTED_NONCE)


def test_structured_success_requires_explicit_status_and_evidence():
    status, evidence = _parse(
        _confirmation(status="completed", evidence="service active; health check=ok")
    )
    assert status == "completed"
    assert evidence == "service active; health check=ok"


def test_structured_failure_never_becomes_completed():
    status, evidence = _parse(
        _confirmation(
            status="failed",
            evidence="restart failed because the unit timed out",
        )
    )
    assert status == "failed"
    assert "restart failed" in evidence


def test_json_confirmation_is_validated():
    status, evidence = _parse(
        json.dumps(
            _confirmation(
                status="success",
                evidence="process restarted; readiness probe=passing",
            )
        )
    )
    assert status == "completed"
    assert evidence == "process restarted; readiness probe=passing"


def test_plain_failure_text_fails_closed_without_becoming_confirmation():
    status, evidence = _parse("error: service restart timed out")
    assert status == "failed"
    assert "structured confirmation" in evidence


def test_plain_positive_text_fails_closed_without_explicit_status():
    status, evidence = _parse("service active; health check=ok")
    assert status == "failed"
    assert "structured confirmation" in evidence


def test_malformed_structured_confirmation_fails_closed():
    status, evidence = _parse('{"status": "completed"')
    assert status == "failed"
    assert "malformed structured confirmation" in evidence


def test_structured_success_with_failure_evidence_fails_closed():
    status, evidence = _parse(
        _confirmation(
            status="completed",
            evidence="health check failed after restart",
        )
    )
    assert status == "failed"
    assert "health check failed" in evidence


def test_confirmation_for_another_operation_fails_closed():
    status, evidence = _parse(
        _confirmation(
            status="completed",
            evidence="service active; health check=ok",
            fingerprint="another-operation",
        )
    )
    assert status == "failed"
    assert "does not match the approved operation" in evidence


def test_confirmation_for_another_approval_receipt_fails_closed():
    status, evidence = _parse(
        _confirmation(
            status="completed",
            evidence="service active; health check=ok",
            approval_nonce="older-receipt-nonce",
        )
    )
    assert status == "failed"
    assert "does not match the approval receipt" in evidence
