from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Mapping

from sigil.order_intent.models import OrderSide, OrderType, TimeInForce


class ExecutionEnvironment(StrEnum):
    PAPER = "paper"
    SANDBOX = "sandbox"
    LIVE = "live"


class SubmissionAdmissionStatus(StrEnum):
    READY = "ready"
    BLOCKED = "blocked"


class ExecutionLifecycleStatus(StrEnum):
    NOT_SUBMITTED = "not_submitted"
    SUBMISSION_ATTEMPTED = "submission_attempted"
    ACKNOWLEDGED = "acknowledged"
    AWAITING_RECONCILIATION = "awaiting_reconciliation"
    RECONCILED = "reconciled"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class BrokerOrderStatus(StrEnum):
    ACCEPTED = "accepted"
    PENDING = "pending"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class FillReconciliationStatus(StrEnum):
    NOT_FILLED = "not_filled"
    PARTIALLY_FILLED = "partially_filled"
    FULLY_FILLED = "fully_filled"
    OVERFILLED = "overfilled"
    PENDING = "pending"
    FAILED = "failed"
    UNKNOWN = "unknown"


class DiscrepancySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


class DiscrepancyCategory(StrEnum):
    MISSING_ACKNOWLEDGEMENT = "missing_acknowledgement"
    REJECTED_ORDER = "rejected_order"
    CANCELLED_ORDER = "cancelled_order"
    EXPIRED_ORDER = "expired_order"
    PENDING_ORDER = "pending_order"
    UNKNOWN_ORDER_STATE = "unknown_order_state"
    ZERO_FILL = "zero_fill"
    PARTIAL_FILL = "partial_fill"
    OVERFILL = "overfill"
    SYMBOL_MISMATCH = "symbol_mismatch"
    SIDE_MISMATCH = "side_mismatch"
    ORDER_TYPE_MISMATCH = "order_type_mismatch"
    TIME_IN_FORCE_MISMATCH = "time_in_force_mismatch"
    QUANTITY_MISMATCH = "quantity_mismatch"
    LIMIT_PRICE_MISMATCH = "limit_price_mismatch"
    UNKNOWN_PROVIDER_ORDER = "unknown_provider_order"
    DUPLICATE_ACKNOWLEDGEMENT = "duplicate_acknowledgement"
    DUPLICATE_FILL = "duplicate_fill"
    FOREIGN_FILL = "foreign_fill"
    INVALID_PROVIDER_VALUE = "invalid_provider_value"
    STALE_EVIDENCE = "stale_evidence"
    EXCESSIVE_FEES = "excessive_fees"
    EXCESSIVE_SLIPPAGE = "excessive_slippage"
    AGGREGATE_NOTIONAL_MISMATCH = "aggregate_notional_mismatch"
    ADAPTER_ERROR = "adapter_error"
    APPROVAL_MISMATCH = "approval_mismatch"
    POLICY_VIOLATION = "policy_violation"


class ExecutionPackageStatus(StrEnum):
    BLOCKED_BEFORE_SUBMISSION = "blocked_before_submission"
    READY_FOR_SUBMISSION = "ready_for_submission"
    SUBMISSION_ATTEMPTED = "submission_attempted"
    AWAITING_RECONCILIATION = "awaiting_reconciliation"
    PARTIALLY_RECONCILED = "partially_reconciled"
    FULLY_RECONCILED = "fully_reconciled"
    RECONCILIATION_FAILED = "reconciliation_failed"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"


class AuditEventType(StrEnum):
    ADMISSION_EVALUATED = "admission_evaluated"
    SUBMISSION_BLOCKED = "submission_blocked"
    SUBMISSION_REQUEST_CREATED = "submission_request_created"
    SUBMISSION_ATTEMPTED = "submission_attempted"
    ACKNOWLEDGEMENT_RECEIVED = "acknowledgement_received"
    SUBMISSION_OUTCOME_UNCERTAIN = "submission_outcome_uncertain"
    BROKER_SNAPSHOT_RECEIVED = "broker_snapshot_received"
    FILL_RECEIVED = "fill_received"
    RECONCILIATION_COMPLETED = "reconciliation_completed"
    DISCREPANCY_DETECTED = "discrepancy_detected"
    PACKAGE_FINALIZED = "package_finalized"


def _clean_required(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty")
    return cleaned


def _normalize_symbol(value: str) -> str:
    return _clean_required(value, "symbol").upper()


def _deduplicate(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(set(value.strip() for value in values if value.strip())))


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    provider: str
    account_id: str
    environment: ExecutionEnvironment
    requested_at: str
    operator_identity: str
    account_class: str = "standard"
    live_execution_explicitly_requested: bool = False
    evidence_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider",
            _clean_required(self.provider, "provider").lower(),
        )
        object.__setattr__(
            self,
            "account_id",
            _clean_required(self.account_id, "account_id"),
        )
        object.__setattr__(
            self,
            "requested_at",
            _clean_required(self.requested_at, "requested_at"),
        )
        object.__setattr__(
            self,
            "operator_identity",
            _clean_required(
                self.operator_identity,
                "operator_identity",
            ),
        )
        object.__setattr__(
            self,
            "account_class",
            _clean_required(self.account_class, "account_class").lower(),
        )
        object.__setattr__(
            self,
            "evidence_references",
            _deduplicate(self.evidence_references),
        )

        if (
            self.environment is ExecutionEnvironment.LIVE
            and not self.live_execution_explicitly_requested
        ):
            raise ValueError(
                "live execution requires explicit live execution request"
            )


@dataclass(frozen=True, slots=True)
class ApprovedOrder:
    intent_id: str
    source_proposal_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    time_in_force: TimeInForce
    quantity: Decimal
    reference_price: Decimal
    notional: Decimal
    limit_price: Decimal | None
    evidence_references: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "intent_id",
            _clean_required(self.intent_id, "intent_id"),
        )
        object.__setattr__(
            self,
            "source_proposal_id",
            _clean_required(
                self.source_proposal_id,
                "source_proposal_id",
            ),
        )
        object.__setattr__(self, "symbol", _normalize_symbol(self.symbol))
        object.__setattr__(
            self,
            "evidence_references",
            _deduplicate(self.evidence_references),
        )

        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.reference_price <= 0:
            raise ValueError("reference_price must be positive")
        if self.notional <= 0:
            raise ValueError("notional must be positive")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError("limit_price must be positive")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit orders require limit_price")
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise ValueError("market orders must not define limit_price")


@dataclass(frozen=True, slots=True)
class SubmissionRequest:
    request_id: str
    client_order_id: str
    source_intent_id: str
    source_order_intent_package_id: str
    source_approval_request_id: str
    source_approval_record_id: str
    provider: str
    account_id: str
    environment: ExecutionEnvironment
    symbol: str
    side: OrderSide
    order_type: OrderType
    time_in_force: TimeInForce
    quantity: Decimal
    reference_price: Decimal
    notional: Decimal
    limit_price: Decimal | None
    created_at: str
    evidence_references: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "request_id",
            "client_order_id",
            "source_intent_id",
            "source_order_intent_package_id",
            "source_approval_request_id",
            "source_approval_record_id",
            "provider",
            "account_id",
            "created_at",
        ):
            object.__setattr__(
                self,
                field_name,
                _clean_required(getattr(self, field_name), field_name),
            )

        object.__setattr__(self, "provider", self.provider.lower())
        object.__setattr__(self, "symbol", _normalize_symbol(self.symbol))
        object.__setattr__(
            self,
            "evidence_references",
            _deduplicate(self.evidence_references),
        )

        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.reference_price <= 0:
            raise ValueError("reference_price must be positive")
        if self.notional <= 0:
            raise ValueError("notional must be positive")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError("limit_price must be positive")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit orders require limit_price")
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise ValueError("market orders must not define limit_price")


@dataclass(frozen=True, slots=True)
class SubmissionAcknowledgement:
    acknowledgement_id: str
    request_id: str
    client_order_id: str
    provider_order_id: str | None
    status: BrokerOrderStatus
    acknowledged_at: str
    provider_message: str | None = None
    evidence_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "acknowledgement_id",
            "request_id",
            "client_order_id",
            "acknowledged_at",
        ):
            object.__setattr__(
                self,
                field_name,
                _clean_required(getattr(self, field_name), field_name),
            )

        if self.provider_order_id is not None:
            object.__setattr__(
                self,
                "provider_order_id",
                _clean_required(
                    self.provider_order_id,
                    "provider_order_id",
                ),
            )

        if self.provider_message is not None:
            cleaned = self.provider_message.strip()
            object.__setattr__(
                self,
                "provider_message",
                cleaned or None,
            )

        object.__setattr__(
            self,
            "evidence_references",
            _deduplicate(self.evidence_references),
        )


@dataclass(frozen=True, slots=True)
class ExecutionFill:
    fill_id: str
    provider_order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    fee: Decimal
    executed_at: str
    evidence_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "fill_id",
            "provider_order_id",
            "client_order_id",
            "executed_at",
        ):
            object.__setattr__(
                self,
                field_name,
                _clean_required(getattr(self, field_name), field_name),
            )

        object.__setattr__(self, "symbol", _normalize_symbol(self.symbol))
        object.__setattr__(
            self,
            "evidence_references",
            _deduplicate(self.evidence_references),
        )

        if self.quantity <= 0:
            raise ValueError("fill quantity must be positive")
        if self.price <= 0:
            raise ValueError("fill price must be positive")
        if self.fee < 0:
            raise ValueError("fill fee must be non-negative")


@dataclass(frozen=True, slots=True)
class BrokerOrderSnapshot:
    snapshot_id: str
    provider_order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    time_in_force: TimeInForce
    requested_quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    limit_price: Decimal | None
    average_fill_price: Decimal | None
    status: BrokerOrderStatus
    observed_at: str
    evidence_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "snapshot_id",
            "provider_order_id",
            "client_order_id",
            "observed_at",
        ):
            object.__setattr__(
                self,
                field_name,
                _clean_required(getattr(self, field_name), field_name),
            )

        object.__setattr__(self, "symbol", _normalize_symbol(self.symbol))
        object.__setattr__(
            self,
            "evidence_references",
            _deduplicate(self.evidence_references),
        )

        if self.requested_quantity <= 0:
            raise ValueError("requested_quantity must be positive")
        if self.filled_quantity < 0:
            raise ValueError("filled_quantity must be non-negative")
        if self.remaining_quantity < 0:
            raise ValueError("remaining_quantity must be non-negative")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError("limit_price must be positive")
        if (
            self.average_fill_price is not None
            and self.average_fill_price <= 0
        ):
            raise ValueError("average_fill_price must be positive")


@dataclass(frozen=True, slots=True)
class ReconciliationDiscrepancy:
    discrepancy_id: str
    category: DiscrepancyCategory
    severity: DiscrepancySeverity
    message: str
    expected: str | None
    observed: str | None
    evidence_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "discrepancy_id",
            _clean_required(self.discrepancy_id, "discrepancy_id"),
        )
        object.__setattr__(
            self,
            "message",
            _clean_required(self.message, "message"),
        )

        if self.expected is not None:
            object.__setattr__(
                self,
                "expected",
                self.expected.strip() or None,
            )
        if self.observed is not None:
            object.__setattr__(
                self,
                "observed",
                self.observed.strip() or None,
            )

        object.__setattr__(
            self,
            "evidence_references",
            _deduplicate(self.evidence_references),
        )


@dataclass(frozen=True, slots=True)
class OrderReconciliation:
    reconciliation_id: str
    source_intent_id: str
    client_order_id: str
    provider_order_id: str | None
    status: FillReconciliationStatus
    approved_quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    weighted_average_fill_price: Decimal | None
    gross_executed_notional: Decimal
    total_fees: Decimal
    net_cash_effect: Decimal
    slippage_amount: Decimal | None
    slippage_basis_points: Decimal | None
    discrepancies: tuple[ReconciliationDiscrepancy, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    evidence_references: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "reconciliation_id",
            "source_intent_id",
            "client_order_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _clean_required(getattr(self, field_name), field_name),
            )

        if self.provider_order_id is not None:
            object.__setattr__(
                self,
                "provider_order_id",
                _clean_required(
                    self.provider_order_id,
                    "provider_order_id",
                ),
            )

        if self.approved_quantity <= 0:
            raise ValueError("approved_quantity must be positive")
        if self.filled_quantity < 0:
            raise ValueError("filled_quantity must be non-negative")
        if self.remaining_quantity < 0:
            raise ValueError("remaining_quantity must be non-negative")
        if self.weighted_average_fill_price is not None:
            if self.weighted_average_fill_price <= 0:
                raise ValueError(
                    "weighted_average_fill_price must be positive"
                )
        if self.gross_executed_notional < 0:
            raise ValueError(
                "gross_executed_notional must be non-negative"
            )
        if self.total_fees < 0:
            raise ValueError("total_fees must be non-negative")

        object.__setattr__(
            self,
            "discrepancies",
            tuple(
                sorted(
                    self.discrepancies,
                    key=lambda item: item.discrepancy_id,
                )
            ),
        )
        object.__setattr__(self, "blockers", _deduplicate(self.blockers))
        object.__setattr__(self, "warnings", _deduplicate(self.warnings))
        object.__setattr__(
            self,
            "evidence_references",
            _deduplicate(self.evidence_references),
        )


@dataclass(frozen=True, slots=True)
class ExecutionAuditEvent:
    event_id: str
    event_type: AuditEventType
    occurred_at: str
    message: str
    source_references: tuple[str, ...] = ()
    evidence_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "event_id",
            "occurred_at",
            "message",
        ):
            object.__setattr__(
                self,
                field_name,
                _clean_required(getattr(self, field_name), field_name),
            )

        object.__setattr__(
            self,
            "source_references",
            _deduplicate(self.source_references),
        )
        object.__setattr__(
            self,
            "evidence_references",
            _deduplicate(self.evidence_references),
        )


@dataclass(frozen=True, slots=True)
class GovernedExecutionPackage:
    package_id: str
    source_order_intent_package_id: str
    source_approval_request_id: str
    source_approval_record_id: str
    provider: str
    account_id: str
    environment: ExecutionEnvironment
    admission_status: SubmissionAdmissionStatus
    final_status: ExecutionPackageStatus
    submission_requests: tuple[SubmissionRequest, ...]
    acknowledgements: tuple[SubmissionAcknowledgement, ...]
    broker_snapshots: tuple[BrokerOrderSnapshot, ...]
    fills: tuple[ExecutionFill, ...]
    reconciliations: tuple[OrderReconciliation, ...]
    aggregate_approved_buy_notional: Decimal
    aggregate_approved_sell_notional: Decimal
    aggregate_approved_turnover: Decimal
    aggregate_executed_buy_notional: Decimal
    aggregate_executed_sell_notional: Decimal
    aggregate_executed_turnover: Decimal
    total_fees: Decimal
    aggregate_cash_effect: Decimal
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    evidence_references: tuple[str, ...]
    policy_snapshot: Mapping[str, str]
    audit_trail: tuple[ExecutionAuditEvent, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "package_id",
            "source_order_intent_package_id",
            "source_approval_request_id",
            "source_approval_record_id",
            "provider",
            "account_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _clean_required(getattr(self, field_name), field_name),
            )

        object.__setattr__(self, "provider", self.provider.lower())

        for field_name in (
            "aggregate_approved_buy_notional",
            "aggregate_approved_sell_notional",
            "aggregate_approved_turnover",
            "aggregate_executed_buy_notional",
            "aggregate_executed_sell_notional",
            "aggregate_executed_turnover",
            "total_fees",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must be non-negative")

        object.__setattr__(
            self,
            "submission_requests",
            tuple(
                sorted(
                    self.submission_requests,
                    key=lambda item: item.client_order_id,
                )
            ),
        )
        object.__setattr__(
            self,
            "acknowledgements",
            tuple(
                sorted(
                    self.acknowledgements,
                    key=lambda item: item.acknowledgement_id,
                )
            ),
        )
        object.__setattr__(
            self,
            "broker_snapshots",
            tuple(
                sorted(
                    self.broker_snapshots,
                    key=lambda item: item.snapshot_id,
                )
            ),
        )
        object.__setattr__(
            self,
            "fills",
            tuple(sorted(self.fills, key=lambda item: item.fill_id)),
        )
        object.__setattr__(
            self,
            "reconciliations",
            tuple(
                sorted(
                    self.reconciliations,
                    key=lambda item: item.client_order_id,
                )
            ),
        )
        object.__setattr__(
            self,
            "audit_trail",
            tuple(
                sorted(
                    self.audit_trail,
                    key=lambda item: (item.occurred_at, item.event_id),
                )
            ),
        )
        object.__setattr__(self, "blockers", _deduplicate(self.blockers))
        object.__setattr__(self, "warnings", _deduplicate(self.warnings))
        object.__setattr__(
            self,
            "evidence_references",
            _deduplicate(self.evidence_references),
        )
        object.__setattr__(
            self,
            "policy_snapshot",
            dict(sorted(self.policy_snapshot.items())),
        )


@dataclass(frozen=True, slots=True)
class ExecutionComparison:
    left_package_id: str
    right_package_id: str
    added_client_order_ids: tuple[str, ...] = ()
    removed_client_order_ids: tuple[str, ...] = ()
    changed_provider_order_ids: Mapping[
        str,
        tuple[str | None, str | None],
    ] = field(default_factory=dict)
    changed_statuses: Mapping[
        str,
        tuple[FillReconciliationStatus, FillReconciliationStatus],
    ] = field(default_factory=dict)
    changed_filled_quantities: Mapping[
        str,
        tuple[Decimal, Decimal],
    ] = field(default_factory=dict)
    changed_average_prices: Mapping[
        str,
        tuple[Decimal | None, Decimal | None],
    ] = field(default_factory=dict)
    changed_fees: Mapping[
        str,
        tuple[Decimal, Decimal],
    ] = field(default_factory=dict)
    changed_cash_effects: Mapping[
        str,
        tuple[Decimal, Decimal],
    ] = field(default_factory=dict)
    added_discrepancy_ids: tuple[str, ...] = ()
    removed_discrepancy_ids: tuple[str, ...] = ()
    blockers_changed: bool = False
    policy_changed: bool = False
    reconciliation_status_changed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "left_package_id",
            _clean_required(
                self.left_package_id,
                "left_package_id",
            ),
        )
        object.__setattr__(
            self,
            "right_package_id",
            _clean_required(
                self.right_package_id,
                "right_package_id",
            ),
        )
        object.__setattr__(
            self,
            "added_client_order_ids",
            _deduplicate(self.added_client_order_ids),
        )
        object.__setattr__(
            self,
            "removed_client_order_ids",
            _deduplicate(self.removed_client_order_ids),
        )
        object.__setattr__(
            self,
            "added_discrepancy_ids",
            _deduplicate(self.added_discrepancy_ids),
        )
        object.__setattr__(
            self,
            "removed_discrepancy_ids",
            _deduplicate(self.removed_discrepancy_ids),
        )
