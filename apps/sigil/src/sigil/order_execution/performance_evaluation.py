from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Iterable

from .audit import deterministic_identifier
from .session_lifecycle import (
    PaperTradingSession,
    PaperTradingSessionStatus,
)


class PaperPerformanceRecommendation(StrEnum):
    PASS = "pass"
    REVIEW = "review"
    FAIL = "fail"


def _required(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty")
    return cleaned


def _deduplicate(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({value.strip() for value in values if value.strip()}))


@dataclass(frozen=True, slots=True)
class PaperTradeOutcome:
    trade_id: str
    realized_pnl: Decimal
    gross_profit: Decimal = Decimal("0")
    gross_loss: Decimal = Decimal("0")
    evidence_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "trade_id",
            _required(self.trade_id, "trade_id"),
        )
        object.__setattr__(
            self,
            "evidence_references",
            _deduplicate(self.evidence_references),
        )
        if self.gross_profit < 0:
            raise ValueError("gross_profit must be non-negative")
        if self.gross_loss < 0:
            raise ValueError("gross_loss must be non-negative")


@dataclass(frozen=True, slots=True)
class PaperPerformancePolicy:
    minimum_trades: int = 1
    minimum_net_pnl: Decimal = Decimal("0")
    minimum_win_rate: Decimal = Decimal("0")
    minimum_profit_factor: Decimal = Decimal("0")
    maximum_drawdown: Decimal = Decimal("1000000")
    maximum_fees: Decimal = Decimal("1000000")
    require_certified_session: bool = True
    require_complete_evidence: bool = True

    def __post_init__(self) -> None:
        if self.minimum_trades < 0:
            raise ValueError("minimum_trades must be non-negative")
        if not Decimal("0") <= self.minimum_win_rate <= Decimal("1"):
            raise ValueError("minimum_win_rate must be between 0 and 1")
        if self.minimum_profit_factor < 0:
            raise ValueError("minimum_profit_factor must be non-negative")
        if self.maximum_drawdown < 0:
            raise ValueError("maximum_drawdown must be non-negative")
        if self.maximum_fees < 0:
            raise ValueError("maximum_fees must be non-negative")


@dataclass(frozen=True, slots=True)
class PaperPerformanceEvaluation:
    evaluation_id: str
    session_id: str
    evaluated_at: str
    evaluator_identity: str
    recommendation: PaperPerformanceRecommendation
    trade_count: int
    winning_trade_count: int
    losing_trade_count: int
    breakeven_trade_count: int
    win_rate: Decimal
    net_pnl: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    profit_factor: Decimal | None
    maximum_drawdown: Decimal
    ending_equity: Decimal
    fees: Decimal
    compliance_score: Decimal
    passed_checks: tuple[str, ...]
    failed_checks: tuple[str, ...]
    evidence_references: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "evaluation_id",
            "session_id",
            "evaluated_at",
            "evaluator_identity",
        ):
            object.__setattr__(
                self,
                field_name,
                _required(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "passed_checks",
            _deduplicate(self.passed_checks),
        )
        object.__setattr__(
            self,
            "failed_checks",
            _deduplicate(self.failed_checks),
        )
        object.__setattr__(
            self,
            "evidence_references",
            _deduplicate(self.evidence_references),
        )


def _maximum_drawdown(equity_curve: tuple[Decimal, ...]) -> Decimal:
    if not equity_curve:
        raise ValueError("equity_curve must not be empty")
    peak = equity_curve[0]
    maximum = Decimal("0")
    for value in equity_curve:
        if value > peak:
            peak = value
        drawdown = peak - value
        if drawdown > maximum:
            maximum = drawdown
    return maximum


def evaluate_paper_trading_performance(
    session: PaperTradingSession,
    *,
    trades: tuple[PaperTradeOutcome, ...],
    equity_curve: tuple[Decimal, ...],
    evaluated_at: str,
    evaluator_identity: str,
    policy: PaperPerformancePolicy | None = None,
    evidence_references: tuple[str, ...] = (),
) -> PaperPerformanceEvaluation:
    evaluation_policy = policy or PaperPerformancePolicy()
    evaluated_clean = _required(evaluated_at, "evaluated_at")
    evaluator_clean = _required(evaluator_identity, "evaluator_identity")

    if session.status not in {
        PaperTradingSessionStatus.CERTIFIED,
        PaperTradingSessionStatus.FAILED,
    }:
        raise ValueError(
            "performance evaluation requires a terminal paper session"
        )
    if not equity_curve:
        raise ValueError("equity_curve must not be empty")
    if len({trade.trade_id for trade in trades}) != len(trades):
        raise ValueError("trade_id values must be unique")

    winning = tuple(trade for trade in trades if trade.realized_pnl > 0)
    losing = tuple(trade for trade in trades if trade.realized_pnl < 0)
    breakeven = tuple(trade for trade in trades if trade.realized_pnl == 0)
    trade_count = len(trades)

    gross_profit = sum(
        (trade.gross_profit for trade in trades),
        Decimal("0"),
    )
    gross_loss = sum(
        (trade.gross_loss for trade in trades),
        Decimal("0"),
    )
    net_pnl = sum(
        (trade.realized_pnl for trade in trades),
        Decimal("0"),
    )
    win_rate = (
        Decimal(len(winning)) / Decimal(trade_count)
        if trade_count
        else Decimal("0")
    )
    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 0
        else None
    )
    maximum_drawdown = _maximum_drawdown(equity_curve)

    combined_evidence = _deduplicate(
        (
            *session.evidence_references,
            *evidence_references,
            *(
                reference
                for trade in trades
                for reference in trade.evidence_references
            ),
        )
    )

    checks: list[tuple[str, bool]] = [
        (
            "session_certified",
            (
                not evaluation_policy.require_certified_session
                or session.status is PaperTradingSessionStatus.CERTIFIED
            ),
        ),
        ("minimum_trades", trade_count >= evaluation_policy.minimum_trades),
        ("minimum_net_pnl", net_pnl >= evaluation_policy.minimum_net_pnl),
        ("minimum_win_rate", win_rate >= evaluation_policy.minimum_win_rate),
        (
            "minimum_profit_factor",
            (
                evaluation_policy.minimum_profit_factor == 0
                or (
                    profit_factor is not None
                    and profit_factor
                    >= evaluation_policy.minimum_profit_factor
                )
            ),
        ),
        (
            "maximum_drawdown",
            maximum_drawdown <= evaluation_policy.maximum_drawdown,
        ),
        ("maximum_fees", session.fees <= evaluation_policy.maximum_fees),
        (
            "complete_evidence",
            (
                not evaluation_policy.require_complete_evidence
                or bool(combined_evidence)
            ),
        ),
        (
            "session_pnl_matches_trades",
            session.realized_pnl == net_pnl,
        ),
    ]

    passed_checks = tuple(name for name, passed in checks if passed)
    failed_checks = tuple(name for name, passed in checks if not passed)
    compliance_score = (
        Decimal(len(passed_checks)) / Decimal(len(checks))
        if checks
        else Decimal("1")
    )

    if not failed_checks:
        recommendation = PaperPerformanceRecommendation.PASS
    elif {
        "session_certified",
        "complete_evidence",
        "session_pnl_matches_trades",
    } & set(failed_checks):
        recommendation = PaperPerformanceRecommendation.FAIL
    else:
        recommendation = PaperPerformanceRecommendation.REVIEW

    evaluation_id = deterministic_identifier(
        "paper-performance-evaluation",
        session.session_id,
        evaluated_clean,
        evaluator_clean,
        trade_count,
        net_pnl,
        maximum_drawdown,
        recommendation,
    )

    return PaperPerformanceEvaluation(
        evaluation_id=evaluation_id,
        session_id=session.session_id,
        evaluated_at=evaluated_clean,
        evaluator_identity=evaluator_clean,
        recommendation=recommendation,
        trade_count=trade_count,
        winning_trade_count=len(winning),
        losing_trade_count=len(losing),
        breakeven_trade_count=len(breakeven),
        win_rate=win_rate,
        net_pnl=net_pnl,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=profit_factor,
        maximum_drawdown=maximum_drawdown,
        ending_equity=equity_curve[-1],
        fees=session.fees,
        compliance_score=compliance_score,
        passed_checks=passed_checks,
        failed_checks=failed_checks,
        evidence_references=combined_evidence,
    )
