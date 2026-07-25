from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from .adapter import ExecutionAdapterError
from .audit import deterministic_identifier
from .models import (
    BrokerOrderSnapshot,
    BrokerOrderStatus,
    ExecutionEnvironment,
    ExecutionFill,
    SubmissionAcknowledgement,
    SubmissionRequest,
)


@dataclass(frozen=True, slots=True)
class PaperExecutionPolicy:
    commission_per_order: Decimal = Decimal("0")
    fill_price_offset_basis_points: Decimal = Decimal("0")
    auto_fill: bool = True

    def __post_init__(self) -> None:
        if self.commission_per_order < 0:
            raise ValueError("commission_per_order must be non-negative")
        if abs(self.fill_price_offset_basis_points) > Decimal("10000"):
            raise ValueError(
                "fill_price_offset_basis_points must be between -10000 and 10000"
            )


@dataclass(slots=True)
class _PaperOrder:
    request: SubmissionRequest
    acknowledgement: SubmissionAcknowledgement
    snapshot: BrokerOrderSnapshot
    fills: tuple[ExecutionFill, ...]


class GovernedPaperExecutionAdapter:
    provider_name = "paper"

    def __init__(
        self,
        *,
        policy: PaperExecutionPolicy | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._policy = policy or PaperExecutionPolicy()
        self._clock = clock or (lambda: "1970-01-01T00:00:00Z")
        self._orders_by_provider_id: dict[str, _PaperOrder] = {}
        self._provider_id_by_client_id: dict[str, str] = {}

    def submit_order(
        self,
        request: SubmissionRequest,
    ) -> SubmissionAcknowledgement:
        if request.provider != self.provider_name:
            raise ExecutionAdapterError(
                "paper adapter only accepts provider='paper'"
            )
        if request.environment is not ExecutionEnvironment.PAPER:
            raise ExecutionAdapterError(
                "paper adapter refuses non-paper execution environments"
            )

        existing_provider_id = self._provider_id_by_client_id.get(
            request.client_order_id
        )
        if existing_provider_id is not None:
            existing = self._orders_by_provider_id[existing_provider_id]
            if existing.request != request:
                raise ExecutionAdapterError(
                    "client_order_id was reused with different order material"
                )
            return existing.acknowledgement

        provider_order_id = deterministic_identifier(
            "paper-provider-order",
            request.client_order_id,
            request.request_id,
        )
        now = self._clock()

        acknowledgement = SubmissionAcknowledgement(
            acknowledgement_id=deterministic_identifier(
                "paper-acknowledgement",
                provider_order_id,
                request.request_id,
            ),
            request_id=request.request_id,
            client_order_id=request.client_order_id,
            provider_order_id=provider_order_id,
            status=(
                BrokerOrderStatus.FILLED
                if self._policy.auto_fill
                else BrokerOrderStatus.ACCEPTED
            ),
            acknowledged_at=now,
            provider_message="Governed paper broker accepted order",
            evidence_references=(
                f"paper-order:{provider_order_id}",
                f"paper-request:{request.request_id}",
            ),
        )

        if self._policy.auto_fill:
            multiplier = (
                Decimal("1")
                + self._policy.fill_price_offset_basis_points
                / Decimal("10000")
            )
            fill_price = request.reference_price * multiplier
            fill = ExecutionFill(
                fill_id=deterministic_identifier(
                    "paper-fill",
                    provider_order_id,
                    request.quantity,
                    fill_price,
                ),
                provider_order_id=provider_order_id,
                client_order_id=request.client_order_id,
                symbol=request.symbol,
                side=request.side,
                quantity=request.quantity,
                price=fill_price,
                fee=self._policy.commission_per_order,
                executed_at=now,
                evidence_references=(f"paper-fill:{provider_order_id}",),
            )
            fills = (fill,)
            filled_quantity = request.quantity
            remaining_quantity = Decimal("0")
            average_fill_price = fill_price
            status = BrokerOrderStatus.FILLED
        else:
            fills = ()
            filled_quantity = Decimal("0")
            remaining_quantity = request.quantity
            average_fill_price = None
            status = BrokerOrderStatus.ACCEPTED

        snapshot = BrokerOrderSnapshot(
            snapshot_id=deterministic_identifier(
                "paper-snapshot",
                provider_order_id,
                status,
                filled_quantity,
            ),
            provider_order_id=provider_order_id,
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            time_in_force=request.time_in_force,
            requested_quantity=request.quantity,
            filled_quantity=filled_quantity,
            remaining_quantity=remaining_quantity,
            limit_price=request.limit_price,
            average_fill_price=average_fill_price,
            status=status,
            observed_at=now,
            evidence_references=(f"paper-snapshot:{provider_order_id}",),
        )

        order = _PaperOrder(
            request=request,
            acknowledgement=acknowledgement,
            snapshot=snapshot,
            fills=fills,
        )
        self._orders_by_provider_id[provider_order_id] = order
        self._provider_id_by_client_id[
            request.client_order_id
        ] = provider_order_id

        return acknowledgement

    def get_order(
        self,
        provider_order_id: str,
    ) -> BrokerOrderSnapshot:
        try:
            return self._orders_by_provider_id[
                provider_order_id
            ].snapshot
        except KeyError:
            raise ExecutionAdapterError(
                "paper provider order was not found"
            ) from None

    def list_fills(
        self,
        provider_order_id: str,
    ) -> tuple[ExecutionFill, ...]:
        try:
            return self._orders_by_provider_id[
                provider_order_id
            ].fills
        except KeyError:
            raise ExecutionAdapterError(
                "paper provider order was not found"
            ) from None
