"""Hard consensus gate for Fusion v2."""

from __future__ import annotations

from .models import FusionGateResult, FusionRequest, FusionVerificationReport


def evaluate_consensus_gate(
    report: FusionVerificationReport,
    request: FusionRequest,
) -> FusionGateResult:
    successful = len(report.successful_participants)
    if successful < request.min_successful_participants:
        return FusionGateResult(
            passed=False,
            status="degraded_insufficient_participants",
            reasons=[
                f"Only {successful}/{request.participants} participants produced usable output; "
                f"minimum is {request.min_successful_participants}."
            ],
            conflicts=report.conflicts,
            candidate_id=report.candidate_id,
        )
    if successful == 1 and not request.allow_single_participant:
        return FusionGateResult(
            passed=False,
            status="degraded_insufficient_participants",
            reasons=["Single successful participant cannot pass Fusion hard consensus by default."],
            conflicts=report.conflicts,
            candidate_id=report.candidate_id,
        )

    material_conflicts = [conflict for conflict in report.conflicts if conflict.material]
    if report.votes:
        approved = set(report.approved_participants)
        successful_participants = set(report.successful_participants)
        if material_conflicts or approved != successful_participants:
            return FusionGateResult(
                passed=False,
                status="operator_decision",
                reasons=[
                    "Hard consensus failed: every successful participant must approve the current candidate "
                    "with no material dissent. Fusion never uses 2-of-3 majority to override a dissenter."
                ],
                conflicts=material_conflicts,
                candidate_id=report.candidate_id,
            )
        return FusionGateResult(
            passed=True,
            status="converged",
            reasons=["All successful participants approved the current candidate with no material dissent."],
            conflicts=[],
            candidate_id=report.candidate_id,
        )

    if material_conflicts:
        return FusionGateResult(
            passed=False,
            status="operator_decision",
            reasons=[
                "Hard consensus failed: every successful participant must agree on every material axis. "
                "Fusion never uses 2-of-3 majority to override a dissenter."
            ],
            conflicts=material_conflicts,
        )
    return FusionGateResult(
        passed=True,
        status="converged",
        reasons=["All successful participants converged on every material axis."],
        conflicts=[],
    )
