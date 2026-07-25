from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Iterable

from .audit import deterministic_identifier
from .performance_evaluation import (
    PaperPerformanceEvaluation,
    PaperPerformanceRecommendation,
)


class PaperPromotionReadinessStatus(StrEnum):
    NOT_READY = "not_ready"
    REVIEW_REQUIRED = "review_required"
    PAPER_READY = "paper_ready"


def _required(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty")
    return cleaned


def _deduplicate(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({value.strip() for value in values if value.strip()}))


@dataclass(frozen=True, slots=True)
class PaperPromotionReadinessPolicy:
    minimum_evaluations: int = 3
    minimum_passed_evaluations: int = 3
    minimum_pass_rate: Decimal = Decimal("1")
    minimum_total_net_pnl: Decimal = Decimal("0")
    minimum_average_compliance_score: Decimal = Decimal("1")
    maximum_failed_evaluations: int = 0
    maximum_review_evaluations: int = 0
    require_unique_sessions: bool = True
    require_complete_evidence: bool = True

    def __post_init__(self) -> None:
        if self.minimum_evaluations < 1:
            raise ValueError("minimum_evaluations must be at least 1")
        if self.minimum_passed_evaluations < 0:
            raise ValueError("minimum_passed_evaluations must be non-negative")
        if not Decimal("0") <= self.minimum_pass_rate <= Decimal("1"):
            raise ValueError("minimum_pass_rate must be between 0 and 1")
        if not Decimal("0") <= self.minimum_average_compliance_score <= Decimal("1"):
            raise ValueError(
                "minimum_average_compliance_score must be between 0 and 1"
            )
        if self.maximum_failed_evaluations < 0:
            raise ValueError("maximum_failed_evaluations must be non-negative")
        if self.maximum_review_evaluations < 0:
            raise ValueError("maximum_review_evaluations must be non-negative")


@dataclass(frozen=True, slots=True)
class PaperPromotionReadinessAssessment:
    assessment_id: str
    assessed_at: str
    assessor_identity: str
    status: PaperPromotionReadinessStatus
    evaluation_count: int
    passed_evaluation_count: int
    review_evaluation_count: int
    failed_evaluation_count: int
    pass_rate: Decimal
    total_net_pnl: Decimal
    average_net_pnl: Decimal
    average_compliance_score: Decimal
    minimum_compliance_score: Decimal
    maximum_drawdown: Decimal
    session_ids: tuple[str, ...]
    passed_checks: tuple[str, ...]
    failed_checks: tuple[str, ...]
    evidence_references: tuple[str, ...]
    readiness_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in ("assessment_id", "assessed_at", "assessor_identity"):
            object.__setattr__(
                self,
                field_name,
                _required(getattr(self, field_name), field_name),
            )
        for field_name in (
            "session_ids",
            "passed_checks",
            "failed_checks",
            "evidence_references",
            "readiness_reasons",
        ):
            object.__setattr__(
                self,
                field_name,
                _deduplicate(getattr(self, field_name)),
            )


def assess_paper_trading_promotion_readiness(
    evaluations: tuple[PaperPerformanceEvaluation, ...],
    *,
    assessed_at: str,
    assessor_identity: str,
    policy: PaperPromotionReadinessPolicy | None = None,
    evidence_references: tuple[str, ...] = (),
) -> PaperPromotionReadinessAssessment:
    readiness_policy = policy or PaperPromotionReadinessPolicy()
    assessed_clean = _required(assessed_at, "assessed_at")
    assessor_clean = _required(assessor_identity, "assessor_identity")

    if not evaluations:
        raise ValueError("evaluations must not be empty")

    evaluation_ids = tuple(item.evaluation_id for item in evaluations)
    if len(set(evaluation_ids)) != len(evaluation_ids):
        raise ValueError("evaluation_id values must be unique")

    session_ids = tuple(item.session_id for item in evaluations)
    evaluation_count = len(evaluations)
    unique_session_count = len(set(session_ids))

    passed = tuple(
        item for item in evaluations
        if item.recommendation is PaperPerformanceRecommendation.PASS
    )
    reviews = tuple(
        item for item in evaluations
        if item.recommendation is PaperPerformanceRecommendation.REVIEW
    )
    failed = tuple(
        item for item in evaluations
        if item.recommendation is PaperPerformanceRecommendation.FAIL
    )

    passed_count = len(passed)
    review_count = len(reviews)
    failed_count = len(failed)
    pass_rate = Decimal(passed_count) / Decimal(evaluation_count)
    total_net_pnl = sum((item.net_pnl for item in evaluations), Decimal("0"))
    average_net_pnl = total_net_pnl / Decimal(evaluation_count)
    average_compliance_score = (
        sum((item.compliance_score for item in evaluations), Decimal("0"))
        / Decimal(evaluation_count)
    )
    minimum_compliance_score = min(
        item.compliance_score for item in evaluations
    )
    maximum_drawdown = max(item.maximum_drawdown for item in evaluations)

    combined_evidence = _deduplicate(
        (
            *evidence_references,
            *(
                reference
                for evaluation in evaluations
                for reference in evaluation.evidence_references
            ),
        )
    )

    checks = [
        (
            "minimum_evaluations",
            evaluation_count >= readiness_policy.minimum_evaluations,
        ),
        (
            "minimum_passed_evaluations",
            passed_count >= readiness_policy.minimum_passed_evaluations,
        ),
        ("minimum_pass_rate", pass_rate >= readiness_policy.minimum_pass_rate),
        (
            "minimum_total_net_pnl",
            total_net_pnl >= readiness_policy.minimum_total_net_pnl,
        ),
        (
            "minimum_average_compliance_score",
            average_compliance_score
            >= readiness_policy.minimum_average_compliance_score,
        ),
        (
            "maximum_failed_evaluations",
            failed_count <= readiness_policy.maximum_failed_evaluations,
        ),
        (
            "maximum_review_evaluations",
            review_count <= readiness_policy.maximum_review_evaluations,
        ),
        (
            "unique_sessions",
            not readiness_policy.require_unique_sessions
            or unique_session_count == evaluation_count,
        ),
        (
            "complete_evidence",
            not readiness_policy.require_complete_evidence
            or (
                bool(combined_evidence)
                and all(
                    bool(evaluation.evidence_references)
                    for evaluation in evaluations
                )
            ),
        ),
    ]

    passed_checks = tuple(name for name, ok in checks if ok)
    failed_checks = tuple(name for name, ok in checks if not ok)

    integrity_failures = {
        "unique_sessions",
        "complete_evidence",
        "maximum_failed_evaluations",
    } & set(failed_checks)

    if not failed_checks:
        status = PaperPromotionReadinessStatus.PAPER_READY
    elif integrity_failures:
        status = PaperPromotionReadinessStatus.NOT_READY
    else:
        status = PaperPromotionReadinessStatus.REVIEW_REQUIRED

    reasons = (
        ("all promotion readiness checks passed",)
        if status is PaperPromotionReadinessStatus.PAPER_READY
        else tuple(f"failed readiness check: {check}" for check in failed_checks)
    )

    assessment_id = deterministic_identifier(
        "paper-promotion-readiness",
        assessed_clean,
        assessor_clean,
        *sorted(evaluation_ids),
        status,
        pass_rate,
        total_net_pnl,
        average_compliance_score,
    )

    return PaperPromotionReadinessAssessment(
        assessment_id=assessment_id,
        assessed_at=assessed_clean,
        assessor_identity=assessor_clean,
        status=status,
        evaluation_count=evaluation_count,
        passed_evaluation_count=passed_count,
        review_evaluation_count=review_count,
        failed_evaluation_count=failed_count,
        pass_rate=pass_rate,
        total_net_pnl=total_net_pnl,
        average_net_pnl=average_net_pnl,
        average_compliance_score=average_compliance_score,
        minimum_compliance_score=minimum_compliance_score,
        maximum_drawdown=maximum_drawdown,
        session_ids=tuple(sorted(set(session_ids))),
        passed_checks=passed_checks,
        failed_checks=failed_checks,
        evidence_references=combined_evidence,
        readiness_reasons=reasons,
    )
