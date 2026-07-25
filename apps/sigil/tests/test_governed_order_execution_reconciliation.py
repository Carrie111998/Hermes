from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from types import SimpleNamespace

from sigil.order_execution import (
    ApprovedOrder,
    ExecutionAdapterError,
    ExecutionContext,
    ExecutionLifecycleStatus,
    SubmissionAdmissionStatus,
    SubmissionOutcomeUncertainError,
    build_submission_requests,
    execute_admitted_orders,
)
from sigil.order_execution.input import ExecutionAdmission

from sigil.order_execution import (
    BrokerOrderSnapshot,
    BrokerOrderStatus,
    ExecutionFill,
    FillReconciliationStatus,
    SubmissionAcknowledgement,
    SubmissionRequest,
    reconcile_order,
)
from sigil.order_execution.models import (
    OrderSide,
    OrderType,
    TimeInForce,
)



from sigil.order_execution import (
    ExecutionEnvironment,
    ExecutionPolicy,
    audit_event_identity,
    canonical_json,
    deterministic_identifier,
)
from sigil.order_execution.models import (
    AuditEventType,
    ExecutionAuditEvent,
)


def test_package_exports_expected_public_api() -> None:
    import sigil.order_execution as order_execution

    expected = {
        "ExecutionPolicy",
        "ExecutionEnvironment",
        "SubmissionAdmissionStatus",
        "evaluate_execution_input",
        "build_submission_requests",
        "execute_admitted_orders",
        "reconcile_order",
        "compare_execution_packages",
    }

    assert expected.issubset(set(order_execution.__all__))


def test_deterministic_identifier_is_stable() -> None:
    first = deterministic_identifier(
        "client-order",
        "package-1",
        "intent-1",
        Decimal("10.00"),
    )
    second = deterministic_identifier(
        "client-order",
        "package-1",
        "intent-1",
        Decimal("10.00"),
    )

    assert first == second
    assert first.startswith("client-order-")


def test_deterministic_identifier_changes_with_inputs() -> None:
    first = deterministic_identifier(
        "client-order",
        "package-1",
        "intent-1",
    )
    second = deterministic_identifier(
        "client-order",
        "package-1",
        "intent-2",
    )

    assert first != second


def test_canonical_json_sorts_mapping_keys() -> None:
    left = canonical_json(
        {
            "beta": Decimal("2.00"),
            "alpha": Decimal("1.00"),
        }
    )
    right = canonical_json(
        {
            "alpha": Decimal("1.00"),
            "beta": Decimal("2.00"),
        }
    )

    assert left == right
    assert left == '{"alpha":"1.00","beta":"2.00"}'


def test_execution_policy_snapshot_is_deterministic() -> None:
    policy = ExecutionPolicy()

    first = canonical_json(policy.snapshot())
    second = canonical_json(policy.snapshot())

    assert first == second


def test_execution_policy_rejects_invalid_limits() -> None:
    with pytest.raises(ValueError):
        ExecutionPolicy(
            max_order_quantity=Decimal("0"),
        )


def test_execution_policy_rejects_negative_slippage_limit() -> None:
    with pytest.raises(ValueError):
        ExecutionPolicy(
            maximum_slippage_basis_points=Decimal("-1"),
        )


def test_audit_event_is_immutable() -> None:
    event = ExecutionAuditEvent(
        event_id="event-1",
        event_type=AuditEventType.ADMISSION_EVALUATED,
        occurred_at="2026-07-25T12:00:00Z",
        message="Admission evaluated",
        source_references=("package-1",),
        evidence_references=("evidence-1",),
    )

    with pytest.raises(FrozenInstanceError):
        event.message = "changed"  # type: ignore[misc]


def test_audit_event_identity_is_stable() -> None:
    event = ExecutionAuditEvent(
        event_id="event-1",
        event_type=AuditEventType.ADMISSION_EVALUATED,
        occurred_at="2026-07-25T12:00:00Z",
        message="Admission evaluated",
        source_references=("package-1",),
        evidence_references=("evidence-1",),
    )

    assert audit_event_identity(event) == audit_event_identity(event)


def test_governed_execution_enums_have_expected_values() -> None:
    assert ExecutionEnvironment.PAPER.value == "paper"
    assert SubmissionAdmissionStatus.READY.value == "ready"
    assert SubmissionAdmissionStatus.BLOCKED.value == "blocked"


def submission_request(
    *,
    quantity: Decimal = Decimal("10"),
    reference_price: Decimal = Decimal("100"),
) -> SubmissionRequest:
    return SubmissionRequest(
        request_id="request-1",
        client_order_id="client-order-1",
        source_intent_id="intent-1",
        source_order_intent_package_id="intent-package-1",
        source_approval_request_id="approval-request-1",
        source_approval_record_id="approval-record-1",
        provider="paper",
        account_id="paper-account",
        environment=ExecutionEnvironment.PAPER,
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        quantity=quantity,
        reference_price=reference_price,
        notional=quantity * reference_price,
        limit_price=None,
        created_at="2026-07-25T12:00:00Z",
        evidence_references=("request-evidence",),
    )


def acknowledgement(
    request: SubmissionRequest,
    *,
    status: BrokerOrderStatus = BrokerOrderStatus.ACCEPTED,
) -> SubmissionAcknowledgement:
    return SubmissionAcknowledgement(
        acknowledgement_id="ack-1",
        request_id=request.request_id,
        client_order_id=request.client_order_id,
        provider_order_id="provider-order-1",
        status=status,
        acknowledged_at="2026-07-25T12:00:01Z",
        evidence_references=("ack-evidence",),
    )


def broker_snapshot(
    request: SubmissionRequest,
    *,
    filled_quantity: Decimal,
    status: BrokerOrderStatus,
    average_fill_price: Decimal | None,
) -> BrokerOrderSnapshot:
    return BrokerOrderSnapshot(
        snapshot_id="snapshot-1",
        provider_order_id="provider-order-1",
        client_order_id=request.client_order_id,
        symbol=request.symbol,
        side=request.side,
        order_type=request.order_type,
        time_in_force=request.time_in_force,
        requested_quantity=request.quantity,
        filled_quantity=filled_quantity,
        remaining_quantity=max(
            Decimal("0"),
            request.quantity - filled_quantity,
        ),
        limit_price=request.limit_price,
        average_fill_price=average_fill_price,
        status=status,
        observed_at="2026-07-25T12:00:05Z",
        evidence_references=("snapshot-evidence",),
    )


def execution_fill(
    request: SubmissionRequest,
    *,
    fill_id: str,
    quantity: Decimal,
    price: Decimal,
    fee: Decimal,
) -> ExecutionFill:
    return ExecutionFill(
        fill_id=fill_id,
        provider_order_id="provider-order-1",
        client_order_id=request.client_order_id,
        symbol=request.symbol,
        side=request.side,
        quantity=quantity,
        price=price,
        fee=fee,
        executed_at="2026-07-25T12:00:03Z",
        evidence_references=(f"{fill_id}-evidence",),
    )


def test_reconcile_full_fill_calculates_weighted_price_and_cash() -> None:
    request = submission_request()
    fills = (
        execution_fill(
            request,
            fill_id="fill-1",
            quantity=Decimal("4"),
            price=Decimal("99"),
            fee=Decimal("0.40"),
        ),
        execution_fill(
            request,
            fill_id="fill-2",
            quantity=Decimal("6"),
            price=Decimal("101"),
            fee=Decimal("0.60"),
        ),
    )

    result = reconcile_order(
        request=request,
        acknowledgement=acknowledgement(request),
        snapshot=broker_snapshot(
            request,
            filled_quantity=Decimal("10"),
            status=BrokerOrderStatus.FILLED,
            average_fill_price=Decimal("100.20"),
        ),
        fills=fills,
        policy=ExecutionPolicy(),
    )

    assert result.status is FillReconciliationStatus.FULLY_FILLED
    assert result.filled_quantity == Decimal("10")
    assert result.remaining_quantity == Decimal("0")
    assert result.weighted_average_fill_price == Decimal("100.2")
    assert result.gross_executed_notional == Decimal("1002")
    assert result.total_fees == Decimal("1.00")
    assert result.net_cash_effect == Decimal("-1003.00")
    assert result.slippage_amount == Decimal("0.2")
    assert result.slippage_basis_points == Decimal("20.0")
    assert result.blockers == ()


def test_reconcile_partial_fill_creates_warning() -> None:
    request = submission_request()

    result = reconcile_order(
        request=request,
        acknowledgement=acknowledgement(request),
        snapshot=broker_snapshot(
            request,
            filled_quantity=Decimal("4"),
            status=BrokerOrderStatus.PARTIALLY_FILLED,
            average_fill_price=Decimal("100"),
        ),
        fills=(
            execution_fill(
                request,
                fill_id="fill-1",
                quantity=Decimal("4"),
                price=Decimal("100"),
                fee=Decimal("0.25"),
            ),
        ),
        policy=ExecutionPolicy(allow_partial_fills=True),
    )

    assert result.status is FillReconciliationStatus.PARTIALLY_FILLED
    assert result.filled_quantity == Decimal("4")
    assert result.remaining_quantity == Decimal("6")
    assert "Order was only partially filled" in result.warnings
    assert result.blockers == ()


def test_reconcile_overfill_is_blocking() -> None:
    request = submission_request()

    result = reconcile_order(
        request=request,
        acknowledgement=acknowledgement(request),
        snapshot=broker_snapshot(
            request,
            filled_quantity=Decimal("11"),
            status=BrokerOrderStatus.FILLED,
            average_fill_price=Decimal("100"),
        ),
        fills=(
            execution_fill(
                request,
                fill_id="fill-1",
                quantity=Decimal("11"),
                price=Decimal("100"),
                fee=Decimal("0.50"),
            ),
        ),
        policy=ExecutionPolicy(),
    )

    assert result.status is FillReconciliationStatus.OVERFILLED
    assert result.filled_quantity == Decimal("11")
    assert result.remaining_quantity == Decimal("0")
    assert "Order was filled above its approved quantity" in result.blockers



def ready_execution_admission() -> ExecutionAdmission:
    approved_order = ApprovedOrder(
        intent_id="intent-1",
        source_proposal_id="proposal-1",
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        quantity=Decimal("2"),
        reference_price=Decimal("100"),
        notional=Decimal("200"),
        limit_price=None,
        evidence_references=("intent-evidence",),
    )

    return ExecutionAdmission(
        status=SubmissionAdmissionStatus.READY,
        package=SimpleNamespace(package_id="intent-package-1"),
        approval_request=SimpleNamespace(request_id="approval-request-1"),
        approval_record=SimpleNamespace(record_id="approval-record-1"),
        context=ExecutionContext(
            provider="paper",
            account_id="paper-account",
            environment=ExecutionEnvironment.PAPER,
            requested_at="2026-07-25T12:00:00Z",
            operator_identity="operator-1",
            evidence_references=("context-evidence",),
        ),
        approved_orders=(approved_order,),
        blockers=(),
        warnings=(),
        evidence_references=("admission-evidence",),
    )


class SuccessfulExecutionAdapter:
    provider_name = "paper"

    def __init__(self) -> None:
        self.submitted_requests: list[SubmissionRequest] = []

    def submit_order(
        self,
        request: SubmissionRequest,
    ) -> SubmissionAcknowledgement:
        self.submitted_requests.append(request)
        return SubmissionAcknowledgement(
            acknowledgement_id="ack-1",
            request_id=request.request_id,
            client_order_id=request.client_order_id,
            provider_order_id="provider-order-1",
            status=BrokerOrderStatus.ACCEPTED,
            acknowledged_at="2026-07-25T12:00:01Z",
            evidence_references=("ack-evidence",),
        )

    def get_order(self, provider_order_id: str) -> BrokerOrderSnapshot:
        raise NotImplementedError

    def list_fills(
        self,
        provider_order_id: str,
    ) -> tuple[ExecutionFill, ...]:
        raise NotImplementedError


class FailingExecutionAdapter(SuccessfulExecutionAdapter):
    def submit_order(
        self,
        request: SubmissionRequest,
    ) -> SubmissionAcknowledgement:
        raise ExecutionAdapterError("provider rejected submission")


class UncertainExecutionAdapter(SuccessfulExecutionAdapter):
    def submit_order(
        self,
        request: SubmissionRequest,
    ) -> SubmissionAcknowledgement:
        raise SubmissionOutcomeUncertainError(
            "provider timeout after transmission"
        )


class WrongProviderExecutionAdapter(SuccessfulExecutionAdapter):
    provider_name = "different-provider"


def test_build_submission_requests_is_deterministic() -> None:
    admission = ready_execution_admission()

    first = build_submission_requests(admission)
    second = build_submission_requests(admission)

    assert first == second
    assert len(first) == 1
    assert first[0].client_order_id == second[0].client_order_id
    assert first[0].request_id == second[0].request_id
    assert first[0].provider == "paper"
    assert first[0].account_id == "paper-account"


def test_blocked_admission_prevents_submission() -> None:
    ready = ready_execution_admission()
    blocked = ExecutionAdmission(
        status=SubmissionAdmissionStatus.BLOCKED,
        package=ready.package,
        approval_request=ready.approval_request,
        approval_record=ready.approval_record,
        context=ready.context,
        approved_orders=ready.approved_orders,
        blockers=("human approval missing",),
        warnings=(),
        evidence_references=ready.evidence_references,
    )
    adapter = SuccessfulExecutionAdapter()

    result = execute_admitted_orders(blocked, adapter)

    assert result.lifecycle_status is ExecutionLifecycleStatus.NOT_SUBMITTED
    assert result.requests == ()
    assert result.acknowledgements == ()
    assert result.blockers == ("human approval missing",)
    assert adapter.submitted_requests == []
    assert result.submitted is False


def test_provider_mismatch_prevents_submission() -> None:
    result = execute_admitted_orders(
        ready_execution_admission(),
        WrongProviderExecutionAdapter(),
    )

    assert result.lifecycle_status is ExecutionLifecycleStatus.NOT_SUBMITTED
    assert result.requests == ()
    assert result.acknowledgements == ()
    assert result.blockers == (
        "execution adapter provider does not match admitted provider",
    )
    assert result.submitted is False


def test_successful_submission_awaits_reconciliation() -> None:
    adapter = SuccessfulExecutionAdapter()

    result = execute_admitted_orders(
        ready_execution_admission(),
        adapter,
    )

    assert (
        result.lifecycle_status
        is ExecutionLifecycleStatus.AWAITING_RECONCILIATION
    )
    assert len(result.requests) == 1
    assert len(result.acknowledgements) == 1
    assert len(adapter.submitted_requests) == 1
    assert result.blockers == ()
    assert result.submitted is True

    request = result.requests[0]
    acknowledgement = result.acknowledgements[0]

    assert acknowledgement.request_id == request.request_id
    assert acknowledgement.client_order_id == request.client_order_id


def test_adapter_failure_marks_submission_failed() -> None:
    result = execute_admitted_orders(
        ready_execution_admission(),
        FailingExecutionAdapter(),
    )

    assert result.lifecycle_status is ExecutionLifecycleStatus.FAILED
    assert len(result.requests) == 1
    assert result.acknowledgements == ()
    assert len(result.blockers) == 1
    assert "adapter submission failed" in result.blockers[0]
    assert result.submitted is False


def test_uncertain_submission_marks_lifecycle_uncertain() -> None:
    result = execute_admitted_orders(
        ready_execution_admission(),
        UncertainExecutionAdapter(),
    )

    assert result.lifecycle_status is ExecutionLifecycleStatus.UNCERTAIN
    assert len(result.requests) == 1
    assert result.acknowledgements == ()
    assert len(result.blockers) == 1
    assert "submission outcome uncertain" in result.blockers[0]
    assert result.submitted is False
