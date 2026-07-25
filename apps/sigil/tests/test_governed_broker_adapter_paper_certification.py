from __future__ import annotations

from decimal import Decimal

import pytest

from sigil.order_execution import (
    BrokerOrderStatus,
    ExecutionAdapterError,
    ExecutionEnvironment,
    GovernedPaperExecutionAdapter,
    PaperExecutionPolicy,
    SubmissionRequest,
    certify_paper_execution_adapter,
)
from sigil.order_intent.models import OrderSide, OrderType, TimeInForce


def request(
    *,
    provider: str = "paper",
    environment: ExecutionEnvironment = ExecutionEnvironment.PAPER,
) -> SubmissionRequest:
    return SubmissionRequest(
        request_id="request-step24",
        client_order_id="client-order-step24",
        source_intent_id="intent-step24",
        source_order_intent_package_id="intent-package-step24",
        source_approval_request_id="approval-request-step24",
        source_approval_record_id="approval-record-step24",
        provider=provider,
        account_id="paper-account",
        environment=environment,
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        quantity=Decimal("2"),
        reference_price=Decimal("100"),
        notional=Decimal("200"),
        limit_price=None,
        created_at="2026-07-25T12:00:00Z",
        evidence_references=("step24-request-evidence",),
    )


def test_paper_adapter_full_fill_round_trip() -> None:
    adapter = GovernedPaperExecutionAdapter(
        clock=lambda: "2026-07-25T12:00:01Z"
    )
    acknowledgement = adapter.submit_order(request())

    assert acknowledgement.status is BrokerOrderStatus.FILLED
    assert acknowledgement.provider_order_id is not None

    snapshot = adapter.get_order(acknowledgement.provider_order_id)
    fills = adapter.list_fills(acknowledgement.provider_order_id)

    assert snapshot.status is BrokerOrderStatus.FILLED
    assert snapshot.filled_quantity == Decimal("2")
    assert snapshot.remaining_quantity == Decimal("0")
    assert len(fills) == 1
    assert fills[0].quantity == Decimal("2")
    assert fills[0].price == Decimal("100")


def test_paper_adapter_submission_is_idempotent() -> None:
    adapter = GovernedPaperExecutionAdapter()
    submitted = request()

    assert adapter.submit_order(submitted) == adapter.submit_order(submitted)


def test_paper_adapter_rejects_non_paper_environment() -> None:
    adapter = GovernedPaperExecutionAdapter()

    with pytest.raises(
        ExecutionAdapterError,
        match="non-paper execution environments",
    ):
        adapter.submit_order(
            request(environment=ExecutionEnvironment.SANDBOX)
        )


def test_paper_adapter_rejects_unknown_provider_order() -> None:
    adapter = GovernedPaperExecutionAdapter()

    with pytest.raises(ExecutionAdapterError, match="not found"):
        adapter.get_order("missing-provider-order")


def test_paper_policy_applies_fee_and_price_offset() -> None:
    adapter = GovernedPaperExecutionAdapter(
        policy=PaperExecutionPolicy(
            commission_per_order=Decimal("1.25"),
            fill_price_offset_basis_points=Decimal("10"),
        )
    )
    acknowledgement = adapter.submit_order(request())
    assert acknowledgement.provider_order_id is not None
    fill = adapter.list_fills(acknowledgement.provider_order_id)[0]

    assert fill.price == Decimal("100.100")
    assert fill.fee == Decimal("1.25")


def test_paper_adapter_certification_passes() -> None:
    result = certify_paper_execution_adapter(
        adapter=GovernedPaperExecutionAdapter(
            clock=lambda: "2026-07-25T12:00:01Z"
        ),
        request=request(),
    )

    assert result.certified is True
    assert result.blockers == ()
    assert "submission is idempotent" in result.checks
    assert "paper fill fully reconciled" in result.checks


def test_certification_refuses_non_paper_request() -> None:
    result = certify_paper_execution_adapter(
        adapter=GovernedPaperExecutionAdapter(),
        request=request(provider="alpaca"),
    )

    assert result.certified is False
    assert "certification request provider must be paper" in result.blockers
