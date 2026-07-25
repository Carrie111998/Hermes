from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import Iterable

from .audit import deterministic_identifier
from .models import ExecutionEnvironment


class PaperTradingSessionStatus(StrEnum):
    PREPARED = "prepared"
    ACTIVE = "active"
    PAUSED = "paused"
    CLOSING = "closing"
    CERTIFIED = "certified"
    FAILED = "failed"


class PaperTradingSessionEventType(StrEnum):
    PREPARED = "prepared"
    STARTED = "started"
    PAUSED = "paused"
    RESUMED = "resumed"
    CLOSING_REQUESTED = "closing_requested"
    CERTIFIED = "certified"
    FAILED = "failed"


class PaperTradingSessionTransitionError(RuntimeError):
    """Raised when a session lifecycle transition is not permitted."""


def _required(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty")
    return cleaned


def _deduplicate(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({value.strip() for value in values if value.strip()}))


@dataclass(frozen=True, slots=True)
class PaperTradingSessionPolicy:
    max_orders: int = 100
    max_gross_notional: Decimal = Decimal("100000")
    require_zero_open_orders_for_certification: bool = True
    require_reconciled_orders_for_certification: bool = True

    def __post_init__(self) -> None:
        if self.max_orders <= 0:
            raise ValueError("max_orders must be positive")
        if self.max_gross_notional <= 0:
            raise ValueError("max_gross_notional must be positive")


@dataclass(frozen=True, slots=True)
class PaperTradingSessionEvent:
    event_id: str
    session_id: str
    event_type: PaperTradingSessionEventType
    occurred_at: str
    actor_identity: str
    reason: str
    evidence_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "event_id",
            "session_id",
            "occurred_at",
            "actor_identity",
            "reason",
        ):
            object.__setattr__(
                self,
                field_name,
                _required(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "evidence_references",
            _deduplicate(self.evidence_references),
        )


@dataclass(frozen=True, slots=True)
class PaperTradingSession:
    session_id: str
    provider: str
    account_id: str
    environment: ExecutionEnvironment
    operator_identity: str
    prepared_at: str
    status: PaperTradingSessionStatus
    policy: PaperTradingSessionPolicy
    order_count: int = 0
    reconciled_order_count: int = 0
    open_order_count: int = 0
    gross_notional: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    started_at: str | None = None
    paused_at: str | None = None
    closing_requested_at: str | None = None
    closed_at: str | None = None
    failure_reason: str | None = None
    evidence_references: tuple[str, ...] = ()
    events: tuple[PaperTradingSessionEvent, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "session_id",
            "provider",
            "account_id",
            "operator_identity",
            "prepared_at",
        ):
            object.__setattr__(
                self,
                field_name,
                _required(getattr(self, field_name), field_name),
            )

        object.__setattr__(self, "provider", self.provider.lower())
        object.__setattr__(
            self,
            "evidence_references",
            _deduplicate(self.evidence_references),
        )

        if self.environment is not ExecutionEnvironment.PAPER:
            raise ValueError("paper trading sessions require PAPER environment")
        if self.provider != "paper":
            raise ValueError("paper trading sessions require provider='paper'")
        if min(
            self.order_count,
            self.reconciled_order_count,
            self.open_order_count,
        ) < 0:
            raise ValueError("session counters must be non-negative")
        if self.reconciled_order_count > self.order_count:
            raise ValueError(
                "reconciled_order_count must not exceed order_count"
            )
        if self.open_order_count > self.order_count:
            raise ValueError("open_order_count must not exceed order_count")
        if self.gross_notional < 0:
            raise ValueError("gross_notional must be non-negative")
        if self.fees < 0:
            raise ValueError("fees must be non-negative")


def _event(
    *,
    session: PaperTradingSession,
    event_type: PaperTradingSessionEventType,
    occurred_at: str,
    actor_identity: str,
    reason: str,
    evidence_references: tuple[str, ...] = (),
) -> PaperTradingSessionEvent:
    return PaperTradingSessionEvent(
        event_id=deterministic_identifier(
            "paper-session-event",
            session.session_id,
            event_type,
            occurred_at,
            len(session.events),
        ),
        session_id=session.session_id,
        event_type=event_type,
        occurred_at=occurred_at,
        actor_identity=actor_identity,
        reason=reason,
        evidence_references=evidence_references,
    )


def prepare_paper_trading_session(
    *,
    provider: str,
    account_id: str,
    operator_identity: str,
    prepared_at: str,
    policy: PaperTradingSessionPolicy | None = None,
    evidence_references: tuple[str, ...] = (),
) -> PaperTradingSession:
    provider_clean = _required(provider, "provider").lower()
    account_clean = _required(account_id, "account_id")
    operator_clean = _required(operator_identity, "operator_identity")
    prepared_clean = _required(prepared_at, "prepared_at")

    if provider_clean != "paper":
        raise ValueError("paper trading sessions require provider='paper'")

    session_id = deterministic_identifier(
        "paper-trading-session",
        provider_clean,
        account_clean,
        operator_clean,
        prepared_clean,
    )
    session = PaperTradingSession(
        session_id=session_id,
        provider=provider_clean,
        account_id=account_clean,
        environment=ExecutionEnvironment.PAPER,
        operator_identity=operator_clean,
        prepared_at=prepared_clean,
        status=PaperTradingSessionStatus.PREPARED,
        policy=policy or PaperTradingSessionPolicy(),
        evidence_references=evidence_references,
    )
    event = _event(
        session=session,
        event_type=PaperTradingSessionEventType.PREPARED,
        occurred_at=prepared_clean,
        actor_identity=operator_clean,
        reason="Governed paper trading session prepared",
        evidence_references=evidence_references,
    )
    return replace(session, events=(event,))


def start_paper_trading_session(
    session: PaperTradingSession,
    *,
    started_at: str,
    actor_identity: str,
    evidence_references: tuple[str, ...] = (),
) -> PaperTradingSession:
    if session.status is not PaperTradingSessionStatus.PREPARED:
        raise PaperTradingSessionTransitionError(
            "only prepared sessions may be started"
        )
    event = _event(
        session=session,
        event_type=PaperTradingSessionEventType.STARTED,
        occurred_at=started_at,
        actor_identity=actor_identity,
        reason="Governed paper trading session started",
        evidence_references=evidence_references,
    )
    return replace(
        session,
        status=PaperTradingSessionStatus.ACTIVE,
        started_at=_required(started_at, "started_at"),
        evidence_references=_deduplicate(
            (*session.evidence_references, *evidence_references)
        ),
        events=(*session.events, event),
    )


def pause_paper_trading_session(
    session: PaperTradingSession,
    *,
    paused_at: str,
    actor_identity: str,
    reason: str,
    evidence_references: tuple[str, ...] = (),
) -> PaperTradingSession:
    if session.status is not PaperTradingSessionStatus.ACTIVE:
        raise PaperTradingSessionTransitionError(
            "only active sessions may be paused"
        )
    event = _event(
        session=session,
        event_type=PaperTradingSessionEventType.PAUSED,
        occurred_at=paused_at,
        actor_identity=actor_identity,
        reason=reason,
        evidence_references=evidence_references,
    )
    return replace(
        session,
        status=PaperTradingSessionStatus.PAUSED,
        paused_at=_required(paused_at, "paused_at"),
        evidence_references=_deduplicate(
            (*session.evidence_references, *evidence_references)
        ),
        events=(*session.events, event),
    )


def resume_paper_trading_session(
    session: PaperTradingSession,
    *,
    resumed_at: str,
    actor_identity: str,
    evidence_references: tuple[str, ...] = (),
) -> PaperTradingSession:
    if session.status is not PaperTradingSessionStatus.PAUSED:
        raise PaperTradingSessionTransitionError(
            "only paused sessions may be resumed"
        )
    event = _event(
        session=session,
        event_type=PaperTradingSessionEventType.RESUMED,
        occurred_at=resumed_at,
        actor_identity=actor_identity,
        reason="Governed paper trading session resumed",
        evidence_references=evidence_references,
    )
    return replace(
        session,
        status=PaperTradingSessionStatus.ACTIVE,
        paused_at=None,
        evidence_references=_deduplicate(
            (*session.evidence_references, *evidence_references)
        ),
        events=(*session.events, event),
    )


def record_paper_trading_activity(
    session: PaperTradingSession,
    *,
    submitted_orders: int,
    reconciled_orders: int,
    open_orders: int,
    gross_notional_delta: Decimal,
    realized_pnl_delta: Decimal = Decimal("0"),
    fees_delta: Decimal = Decimal("0"),
    evidence_references: tuple[str, ...] = (),
) -> PaperTradingSession:
    if session.status is not PaperTradingSessionStatus.ACTIVE:
        raise PaperTradingSessionTransitionError(
            "activity may only be recorded for active sessions"
        )
    if min(submitted_orders, reconciled_orders, open_orders) < 0:
        raise ValueError("activity counters must be non-negative")
    if gross_notional_delta < 0:
        raise ValueError("gross_notional_delta must be non-negative")
    if fees_delta < 0:
        raise ValueError("fees_delta must be non-negative")

    order_count = session.order_count + submitted_orders
    reconciled_count = session.reconciled_order_count + reconciled_orders
    gross_notional = session.gross_notional + gross_notional_delta

    if order_count > session.policy.max_orders:
        raise PaperTradingSessionTransitionError(
            "paper session maximum order count would be exceeded"
        )
    if gross_notional > session.policy.max_gross_notional:
        raise PaperTradingSessionTransitionError(
            "paper session maximum gross notional would be exceeded"
        )
    if reconciled_count > order_count:
        raise ValueError(
            "cumulative reconciled orders must not exceed submitted orders"
        )
    if open_orders > order_count:
        raise ValueError("open_orders must not exceed submitted orders")

    return replace(
        session,
        order_count=order_count,
        reconciled_order_count=reconciled_count,
        open_order_count=open_orders,
        gross_notional=gross_notional,
        realized_pnl=session.realized_pnl + realized_pnl_delta,
        fees=session.fees + fees_delta,
        evidence_references=_deduplicate(
            (*session.evidence_references, *evidence_references)
        ),
    )


def request_paper_trading_session_close(
    session: PaperTradingSession,
    *,
    requested_at: str,
    actor_identity: str,
    reason: str,
    evidence_references: tuple[str, ...] = (),
) -> PaperTradingSession:
    if session.status not in {
        PaperTradingSessionStatus.ACTIVE,
        PaperTradingSessionStatus.PAUSED,
    }:
        raise PaperTradingSessionTransitionError(
            "only active or paused sessions may begin closing"
        )
    event = _event(
        session=session,
        event_type=PaperTradingSessionEventType.CLOSING_REQUESTED,
        occurred_at=requested_at,
        actor_identity=actor_identity,
        reason=reason,
        evidence_references=evidence_references,
    )
    return replace(
        session,
        status=PaperTradingSessionStatus.CLOSING,
        closing_requested_at=_required(requested_at, "requested_at"),
        evidence_references=_deduplicate(
            (*session.evidence_references, *evidence_references)
        ),
        events=(*session.events, event),
    )


def certify_paper_trading_session(
    session: PaperTradingSession,
    *,
    certified_at: str,
    actor_identity: str,
    evidence_references: tuple[str, ...] = (),
) -> PaperTradingSession:
    if session.status is not PaperTradingSessionStatus.CLOSING:
        raise PaperTradingSessionTransitionError(
            "only closing sessions may be certified"
        )
    if (
        session.policy.require_zero_open_orders_for_certification
        and session.open_order_count != 0
    ):
        raise PaperTradingSessionTransitionError(
            "session cannot be certified with open orders"
        )
    if (
        session.policy.require_reconciled_orders_for_certification
        and session.reconciled_order_count != session.order_count
    ):
        raise PaperTradingSessionTransitionError(
            "session cannot be certified until all orders are reconciled"
        )

    event = _event(
        session=session,
        event_type=PaperTradingSessionEventType.CERTIFIED,
        occurred_at=certified_at,
        actor_identity=actor_identity,
        reason="Governed paper trading session certified",
        evidence_references=evidence_references,
    )
    return replace(
        session,
        status=PaperTradingSessionStatus.CERTIFIED,
        closed_at=_required(certified_at, "certified_at"),
        evidence_references=_deduplicate(
            (*session.evidence_references, *evidence_references)
        ),
        events=(*session.events, event),
    )


def fail_paper_trading_session(
    session: PaperTradingSession,
    *,
    failed_at: str,
    actor_identity: str,
    reason: str,
    evidence_references: tuple[str, ...] = (),
) -> PaperTradingSession:
    if session.status in {
        PaperTradingSessionStatus.CERTIFIED,
        PaperTradingSessionStatus.FAILED,
    }:
        raise PaperTradingSessionTransitionError(
            "terminal sessions cannot be failed again"
        )
    reason_clean = _required(reason, "reason")
    event = _event(
        session=session,
        event_type=PaperTradingSessionEventType.FAILED,
        occurred_at=failed_at,
        actor_identity=actor_identity,
        reason=reason_clean,
        evidence_references=evidence_references,
    )
    return replace(
        session,
        status=PaperTradingSessionStatus.FAILED,
        closed_at=_required(failed_at, "failed_at"),
        failure_reason=reason_clean,
        evidence_references=_deduplicate(
            (*session.evidence_references, *evidence_references)
        ),
        events=(*session.events, event),
    )
