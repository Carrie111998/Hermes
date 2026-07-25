from decimal import Decimal

from sigil.order_execution.governed_broker_submission import (
    BrokerSubmissionOutcomeUncertainError,
    BrokerSubmissionResponse,
    GovernedBrokerSubmissionPolicy,
    GovernedBrokerSubmissionRequest,
    GovernedBrokerSubmissionStatus,
    submit_governed_broker_order,
)
from sigil.order_execution.live_execution_handoff import (
    LiveExecutionEnvelope,
    LiveExecutionHandoff,
    LiveExecutionHandoffStatus,
)


def _handoff(
    *,
    status: LiveExecutionHandoffStatus = LiveExecutionHandoffStatus.READY,
    notional: Decimal = Decimal(5),
) -> LiveExecutionHandoff:
    envelope = LiveExecutionEnvelope(
        envelope_id="envelope-1",
        admission_id="admission-1",
        client_order_id="client-order-1",
        broker_name="public",
        account_identifier="account-1",
        asset_class="equity",
        symbol="AAPL",
        order_type="limit",
        side="buy",
        quantity=Decimal(1),
        limit_price=Decimal(5),
        estimated_notional=notional,
        execution_adapter_name="public-live",
        execution_environment="live",
        operator_identity="owner-1",
        operator_authorization_reference="launch-token-1",
        policy_version="live-execution-v1",
        evidence_references=("handoff-evidence",),
    )
    return LiveExecutionHandoff(
        handoff_id="handoff-1",
        request_id="handoff-request-1",
        admission_id="admission-1",
        status=status,
        admission_age_seconds=1,
        envelope=envelope,
        evidence_references=("handoff-evidence",),
        passed_checks=("ready",),
        failed_checks=(),
    )


def _request(**overrides: object) -> GovernedBrokerSubmissionRequest:
    values: dict[str, object] = {
        "request_id": "submission-request-1",
        "handoff_prepared_at_epoch": 1500,
        "requested_at_epoch": 1510,
        "owner_identity": "owner-1",
        "owner_confirmation_reference": "owner-confirmation-1",
        "launch_certification_reference": "launch-certification-1",
        "kill_switch_active": False,
        "policy_version": "broker-submission-v1",
        "evidence_references": ("submission-evidence",),
    }
    values.update(overrides)
    return GovernedBrokerSubmissionRequest(**values)  # type: ignore[arg-type]


def _accepted(_envelope: object) -> BrokerSubmissionResponse:
    return BrokerSubmissionResponse(
        accepted=True,
        broker_order_id="broker-order-1",
        broker_status="accepted",
        response_reference="public-response-1",
        evidence_references=("broker-evidence",),
    )


def test_submits_exactly_once_and_builds_immutable_receipt() -> None:
    calls: list[object] = []

    def submitter(envelope: object) -> BrokerSubmissionResponse:
        calls.append(envelope)
        return _accepted(envelope)

    receipt = submit_governed_broker_order(_handoff(), _request(), submitter)

    assert receipt.status is GovernedBrokerSubmissionStatus.SUBMITTED
    assert receipt.broker_order_id == "broker-order-1"
    assert receipt.retry_permitted is False
    assert len(calls) == 1


def test_kill_switch_blocks_without_calling_broker() -> None:
    calls: list[object] = []
    receipt = submit_governed_broker_order(
        _handoff(),
        _request(kill_switch_active=True),
        lambda envelope: calls.append(envelope) or _accepted(envelope),
    )
    assert receipt.status is GovernedBrokerSubmissionStatus.BLOCKED
    assert "kill_switch_clear" in receipt.failed_checks
    assert calls == []


def test_duplicate_envelope_is_blocked() -> None:
    receipt = submit_governed_broker_order(
        _handoff(),
        _request(),
        _accepted,
        prior_envelope_ids=("envelope-1",),
    )
    assert receipt.status is GovernedBrokerSubmissionStatus.BLOCKED
    assert "one_time_envelope" in receipt.failed_checks


def test_default_launch_cap_is_twenty_five_dollars() -> None:
    receipt = submit_governed_broker_order(
        _handoff(notional=Decimal(25)),
        _request(),
        _accepted,
    )
    assert receipt.status is GovernedBrokerSubmissionStatus.SUBMITTED


def test_order_above_launch_cap_is_rejected_without_submission() -> None:
    calls: list[object] = []
    receipt = submit_governed_broker_order(
        _handoff(notional=Decimal("25.01")),
        _request(),
        lambda envelope: calls.append(envelope) or _accepted(envelope),
    )
    assert receipt.status is GovernedBrokerSubmissionStatus.REJECTED
    assert "launch_notional_within_limit" in receipt.failed_checks
    assert calls == []


def test_stale_handoff_is_rejected() -> None:
    receipt = submit_governed_broker_order(
        _handoff(),
        _request(requested_at_epoch=1516),
        _accepted,
        policy=GovernedBrokerSubmissionPolicy(maximum_handoff_age_seconds=15),
    )
    assert receipt.status is GovernedBrokerSubmissionStatus.REJECTED
    assert "handoff_fresh" in receipt.failed_checks


def test_broker_rejection_is_recorded_without_retry() -> None:
    response = BrokerSubmissionResponse(
        accepted=False,
        broker_order_id="",
        broker_status="rejected",
        response_reference="public-rejection-1",
    )
    receipt = submit_governed_broker_order(
        _handoff(),
        _request(),
        lambda _envelope: response,
    )
    assert receipt.status is GovernedBrokerSubmissionStatus.BROKER_REJECTED
    assert receipt.retry_permitted is False


def test_uncertain_outcome_never_retries_automatically() -> None:
    def uncertain(_envelope: object) -> BrokerSubmissionResponse:
        raise BrokerSubmissionOutcomeUncertainError

    receipt = submit_governed_broker_order(
        _handoff(),
        _request(),
        uncertain,
    )
    assert receipt.status is GovernedBrokerSubmissionStatus.OUTCOME_UNCERTAIN
    assert receipt.retry_permitted is False
    assert receipt.broker_order_id is None
