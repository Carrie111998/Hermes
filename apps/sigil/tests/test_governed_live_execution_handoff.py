from decimal import Decimal

from sigil.order_execution import (
    LiveExecutionHandoffPolicy,
    LiveExecutionHandoffRequest,
    LiveExecutionHandoffStatus,
    LiveOrderAdmission,
    LiveOrderAdmissionStatus,
    prepare_live_execution_handoff,
)


def _admission(
    status: LiveOrderAdmissionStatus = LiveOrderAdmissionStatus.ADMITTED,
    **overrides,
) -> LiveOrderAdmission:
    values = {
        "admission_id": "admission-1",
        "launch_control_id": "launch-control-1",
        "request_id": "order-request-1",
        "client_order_id": "client-order-1",
        "status": status,
        "broker_name": "public",
        "account_identifier": "account-1",
        "asset_class": "equity",
        "symbol": "AAPL",
        "order_type": "limit",
        "side": "buy",
        "quantity": Decimal(1),
        "limit_price": Decimal(5),
        "estimated_notional": Decimal(5),
        "projected_live_capital": Decimal(5),
        "realized_daily_loss": Decimal(0),
        "projected_open_position_count": 1,
        "market_data_age_seconds": 10,
        "operator_authorization_reference": "launch-token-1",
        "evidence_references": ("admission-evidence",),
        "passed_checks": ("admission_approved",),
        "failed_checks": (),
    }
    values.update(overrides)
    return LiveOrderAdmission(**values)


def _request(**overrides) -> LiveExecutionHandoffRequest:
    values = {
        "request_id": "handoff-request-1",
        "execution_adapter_name": "public-live",
        "execution_environment": "live",
        "admission_evaluated_at_epoch": 1500,
        "requested_at_epoch": 1510,
        "operator_identity": "owner-1",
        "operator_authorization_reference": "launch-token-1",
        "policy_version": "live-execution-v1",
        "evidence_references": ("handoff-evidence",),
    }
    values.update(overrides)
    return LiveExecutionHandoffRequest(**values)


def test_prepares_ready_handoff() -> None:
    result = prepare_live_execution_handoff(_admission(), _request())
    assert result.status is LiveExecutionHandoffStatus.READY
    assert result.envelope is not None
    assert not result.failed_checks


def test_rejects_non_admitted_order() -> None:
    result = prepare_live_execution_handoff(
        _admission(LiveOrderAdmissionStatus.REJECTED),
        _request(),
    )
    assert result.status is LiveExecutionHandoffStatus.REJECTED
    assert result.envelope is None
    assert "admission_approved" in result.failed_checks


def test_marks_duplicate() -> None:
    result = prepare_live_execution_handoff(
        _admission(),
        _request(),
        prior_admission_ids=("admission-1",),
    )
    assert result.status is LiveExecutionHandoffStatus.DUPLICATE
    assert "not_duplicate" in result.failed_checks


def test_marks_expired() -> None:
    result = prepare_live_execution_handoff(
        _admission(),
        _request(requested_at_epoch=1520),
        policy=LiveExecutionHandoffPolicy(maximum_admission_age_seconds=15),
    )
    assert result.status is LiveExecutionHandoffStatus.EXPIRED
    assert "admission_fresh" in result.failed_checks


def test_rejects_future_timestamp() -> None:
    result = prepare_live_execution_handoff(
        _admission(),
        _request(admission_evaluated_at_epoch=1520),
    )
    assert result.status is LiveExecutionHandoffStatus.REJECTED
    assert "admission_not_from_future" in result.failed_checks


def test_rejects_mismatched_authorization() -> None:
    result = prepare_live_execution_handoff(
        _admission(),
        _request(operator_authorization_reference="wrong-token"),
    )
    assert result.status is LiveExecutionHandoffStatus.REJECTED
    assert "authorization_matches_admission" in result.failed_checks


def test_combines_evidence() -> None:
    result = prepare_live_execution_handoff(
        _admission(evidence_references=("shared", "admission-evidence")),
        _request(evidence_references=("shared", "handoff-evidence")),
    )
    assert result.evidence_references == (
        "admission-evidence",
        "handoff-evidence",
        "shared",
    )


def test_ids_are_deterministic() -> None:
    first = prepare_live_execution_handoff(_admission(), _request())
    second = prepare_live_execution_handoff(_admission(), _request())
    assert first.handoff_id == second.handoff_id
    assert first.envelope is not None
    assert second.envelope is not None
    assert first.envelope.envelope_id == second.envelope.envelope_id
