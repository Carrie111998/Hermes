"""Deterministic governed portfolio-risk engine."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation

from .input import GovernedRiskRequest
from .models import (
    GovernedRiskPackage,
    PositionSide,
    RiskDisposition,
    RiskMetric,
    RiskMetricKind,
    RiskProvenance,
    RiskSeverity,
    RiskValidationError,
)
from .policy import RiskPolicy


def _d(value: str, name: str) -> Decimal:
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise RiskValidationError(f"{name} must be decimal-compatible") from exc


def _severity(ratio: Decimal) -> RiskSeverity:
    if ratio <= Decimal("0.75"):
        return RiskSeverity.LOW
    if ratio <= Decimal("1.00"):
        return RiskSeverity.MODERATE
    if ratio <= Decimal("1.25"):
        return RiskSeverity.HIGH
    return RiskSeverity.CRITICAL


def _metric(
    *,
    kind: RiskMetricKind,
    value: Decimal,
    limit: Decimal,
    subject_id: str,
    explanation: str,
    evidence_references: tuple[str, ...] = (),
) -> RiskMetric:
    ratio = value / limit if limit else Decimal("999")
    severity = _severity(ratio)
    return RiskMetric(
        kind=kind,
        value=str(value.normalize()),
        limit=str(limit.normalize()),
        severity=severity,
        breached=value > limit,
        subject_id=subject_id,
        explanation=explanation,
        evidence_references=tuple(sorted(set(evidence_references))),
    )


def construct_governed_risk_package(
    request: GovernedRiskRequest,
    policy: RiskPolicy,
) -> GovernedRiskPackage:
    if request.policy_identity != policy.policy_identity:
        raise RiskValidationError("request policy identity does not match supplied policy")

    equity = _d(request.equity_value, "equity_value")
    if equity <= 0:
        raise RiskValidationError("equity_value must be greater than zero")

    long_value = Decimal("0")
    short_value = Decimal("0")
    issuer_values: dict[str, Decimal] = defaultdict(Decimal)
    sector_values: dict[str, Decimal] = defaultdict(Decimal)
    metrics: list[RiskMetric] = []
    all_evidence: list[str] = []

    weighted_volatility = Decimal("0")
    weighted_drawdown = Decimal("0")

    for position in request.positions:
        value = abs(_d(position.market_value, "market_value"))
        adv = _d(position.average_daily_volume_value, "average_daily_volume_value")
        volatility = _d(position.annualized_volatility, "annualized_volatility")
        drawdown = _d(position.peak_to_trough_drawdown, "peak_to_trough_drawdown")
        all_evidence.extend(position.evidence_references)

        if position.side is PositionSide.LONG:
            long_value += value
        else:
            short_value += value

        issuer_values[position.issuer_id] += value
        sector_values[position.sector_id] += value
        weight = value / equity
        weighted_volatility += weight * volatility
        weighted_drawdown += weight * drawdown

        metrics.append(
            _metric(
                kind=RiskMetricKind.POSITION_CONCENTRATION,
                value=weight,
                limit=_d(policy.max_position_concentration, "max_position_concentration"),
                subject_id=position.position_id,
                explanation="Absolute position market value divided by portfolio equity.",
                evidence_references=position.evidence_references,
            )
        )

        days = value / adv if adv > 0 else Decimal("999")
        metrics.append(
            _metric(
                kind=RiskMetricKind.LIQUIDITY,
                value=days,
                limit=_d(policy.max_days_to_liquidate, "max_days_to_liquidate"),
                subject_id=position.position_id,
                explanation="Estimated liquidation days using supplied average daily volume value.",
                evidence_references=position.evidence_references,
            )
        )

    gross_value = long_value + short_value
    net_value = long_value - short_value

    portfolio_metrics = (
        _metric(
            kind=RiskMetricKind.GROSS_EXPOSURE,
            value=gross_value / equity,
            limit=_d(policy.max_gross_exposure, "max_gross_exposure"),
            subject_id=request.portfolio_id,
            explanation="Gross long plus short exposure divided by portfolio equity.",
            evidence_references=tuple(all_evidence),
        ),
        _metric(
            kind=RiskMetricKind.NET_EXPOSURE,
            value=abs(net_value) / equity,
            limit=_d(policy.max_absolute_net_exposure, "max_absolute_net_exposure"),
            subject_id=request.portfolio_id,
            explanation="Absolute net exposure divided by portfolio equity.",
            evidence_references=tuple(all_evidence),
        ),
        _metric(
            kind=RiskMetricKind.LONG_EXPOSURE,
            value=long_value / equity,
            limit=_d(policy.max_gross_exposure, "max_gross_exposure"),
            subject_id=request.portfolio_id,
            explanation="Long market value divided by portfolio equity.",
            evidence_references=tuple(all_evidence),
        ),
        _metric(
            kind=RiskMetricKind.SHORT_EXPOSURE,
            value=short_value / equity,
            limit=_d(policy.max_gross_exposure, "max_gross_exposure"),
            subject_id=request.portfolio_id,
            explanation="Short market value divided by portfolio equity.",
            evidence_references=tuple(all_evidence),
        ),
        _metric(
            kind=RiskMetricKind.LEVERAGE,
            value=gross_value / equity,
            limit=_d(policy.max_leverage, "max_leverage"),
            subject_id=request.portfolio_id,
            explanation="Gross exposure used as deterministic leverage proxy.",
            evidence_references=tuple(all_evidence),
        ),
        _metric(
            kind=RiskMetricKind.VOLATILITY,
            value=weighted_volatility,
            limit=_d(policy.max_weighted_volatility, "max_weighted_volatility"),
            subject_id=request.portfolio_id,
            explanation="Market-value-weighted supplied annualized volatility.",
            evidence_references=tuple(all_evidence),
        ),
        _metric(
            kind=RiskMetricKind.DRAWDOWN,
            value=weighted_drawdown,
            limit=_d(policy.max_weighted_drawdown, "max_weighted_drawdown"),
            subject_id=request.portfolio_id,
            explanation="Market-value-weighted supplied peak-to-trough drawdown.",
            evidence_references=tuple(all_evidence),
        ),
    )
    metrics.extend(portfolio_metrics)

    for issuer_id, value in sorted(issuer_values.items()):
        metrics.append(
            _metric(
                kind=RiskMetricKind.ISSUER_CONCENTRATION,
                value=value / equity,
                limit=_d(policy.max_issuer_concentration, "max_issuer_concentration"),
                subject_id=issuer_id,
                explanation="Issuer aggregate market value divided by portfolio equity.",
            )
        )

    for sector_id, value in sorted(sector_values.items()):
        metrics.append(
            _metric(
                kind=RiskMetricKind.SECTOR_CONCENTRATION,
                value=value / equity,
                limit=_d(policy.max_sector_concentration, "max_sector_concentration"),
                subject_id=sector_id,
                explanation="Sector aggregate market value divided by portfolio equity.",
            )
        )

    metrics_tuple = tuple(
        sorted(metrics, key=lambda item: (item.kind.value, item.subject_id, item.value))
    )
    breached = tuple(
        f"{item.kind.value}:{item.subject_id}"
        for item in metrics_tuple
        if item.breached
    )

    missing_evidence = tuple(
        position.position_id
        for position in request.positions
        if policy.require_evidence and not position.evidence_references
    )

    severity_order = {
        RiskSeverity.LOW: 0,
        RiskSeverity.MODERATE: 1,
        RiskSeverity.HIGH: 2,
        RiskSeverity.CRITICAL: 3,
    }
    overall = max(
        (item.severity for item in metrics_tuple),
        key=lambda item: severity_order[item],
    )

    score = min(
        100,
        sum(
            {
                RiskSeverity.LOW: 2,
                RiskSeverity.MODERATE: 5,
                RiskSeverity.HIGH: 12,
                RiskSeverity.CRITICAL: 20,
            }[item.severity]
            for item in metrics_tuple
        ),
    )

    blockers = list(f"critical breach: {item}" for item in breached if any(
        metric.breached
        and metric.severity is RiskSeverity.CRITICAL
        and f"{metric.kind.value}:{metric.subject_id}" == item
        for metric in metrics_tuple
    ))
    blockers.extend(f"missing evidence: {item}" for item in missing_evidence)

    if blockers and policy.block_on_critical:
        disposition = RiskDisposition.BLOCKED
    elif breached or missing_evidence:
        disposition = RiskDisposition.REVIEW_REQUIRED
    else:
        disposition = RiskDisposition.ACCEPTABLE

    review_reasons = tuple(sorted(set(
        [f"limit breach: {item}" for item in breached]
        + [f"missing evidence: {item}" for item in missing_evidence]
    )))

    return GovernedRiskPackage(
        portfolio_id=request.portfolio_id,
        as_of=request.as_of,
        metrics=metrics_tuple,
        severity=overall,
        disposition=disposition,
        risk_score=score,
        breached_limits=breached,
        readiness_blockers=tuple(sorted(set(blockers))),
        review_reasons=review_reasons,
        provenance=RiskProvenance(
            request_identity=request.request_identity,
            policy_identity=policy.policy_identity,
            source_ids=tuple(sorted({p.source_id for p in request.positions})),
            input_position_identities=tuple(
                position.position_identity for position in request.positions
            ),
            upstream_package_identities=request.upstream_package_identities,
        ),
    )
