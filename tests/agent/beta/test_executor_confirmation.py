import json

from agent.beta.orchestrator import _parse_executor_confirmation


EXPECTED_FINGERPRINT = "approved-operation"


def _confirmation(*, status: str, evidence: str, fingerprint: str = EXPECTED_FINGERPRINT):
    return {
        "operation_fingerprint": fingerprint,
        "status": status,
        "evidence": evidence,
    }


def test_structured_success_requires_explicit_status_and_evidence():
    status, evidence = _parse_executor_confirmation(
        _confirmation(status="completed", evidence="service active; health check=ok"),
        EXPECTED_FINGERPRINT,
    )

    assert status == "completed"
    assert evidence == "service active; health check=ok"


def test_structured_failure_never_becomes_completed():
    status, evidence = _parse_executor_confirmation(
        _confirmation(
            status="failed",
            evidence="restart failed because the unit timed out",
        ),
        EXPECTED_FINGERPRINT,
    )

    assert status == "failed"
    assert "restart failed" in evidence


def test_json_confirmation_is_validated():
    status, evidence = _parse_executor_confirmation(
        json.dumps(
            _confirmation(
                status="success",
                evidence="process restarted; readiness probe=passing",
            )
        ),
        EXPECTED_FINGERPRINT,
    )

    assert status == "completed"
    assert evidence == "process restarted; readiness probe=passing"


def test_plain_failure_text_fails_closed_without_becoming_confirmation():
    status, evidence = _parse_executor_confirmation(
        "error: service restart timed out",
        EXPECTED_FINGERPRINT,
    )

    assert status == "failed"
    assert "structured confirmation" in evidence


def test_plain_positive_text_fails_closed_without_explicit_status():
    status, evidence = _parse_executor_confirmation(
        "service active; health check=ok",
        EXPECTED_FINGERPRINT,
    )

    assert status == "failed"
    assert "structured confirmation" in evidence


def test_malformed_structured_confirmation_fails_closed():
    status, evidence = _parse_executor_confirmation(
        '{"status": "completed"',
        EXPECTED_FINGERPRINT,
    )

    assert status == "failed"
    assert "malformed structured confirmation" in evidence


def test_structured_success_with_failure_evidence_fails_closed():
    status, evidence = _parse_executor_confirmation(
        _confirmation(
            status="completed",
            evidence="health check failed after restart",
        ),
        EXPECTED_FINGERPRINT,
    )

    assert status == "failed"
    assert "health check failed" in evidence


def test_confirmation_for_another_operation_fails_closed():
    status, evidence = _parse_executor_confirmation(
        _confirmation(
            status="completed",
            evidence="service active; health check=ok",
            fingerprint="another-operation",
        ),
        EXPECTED_FINGERPRINT,
    )

    assert status == "failed"
    assert "does not match the approved operation" in evidence
