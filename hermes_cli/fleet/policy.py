"""Pure eligibility and deterministic fleet selection policy."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from .types import (
    Confidence,
    Freshness,
    LaneEvaluation,
    LaneInputs,
    ReasonCode,
    RouteDecision,
    TaskSpec,
)


SWITCH_DELTA = Decimal("20.000")
_CONFIDENCE_RANK = {
    Confidence.LOW: 0,
    Confidence.MEDIUM: 1,
    Confidence.HIGH: 2,
}


def evaluate_lane(
    inputs: LaneInputs,
    task: TaskSpec,
    *,
    now: datetime | None = None,
    minimum_confidence: Confidence = Confidence.HIGH,
) -> LaneEvaluation:
    """Evaluate every gate without short-circuiting the reason matrix."""

    at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    profile = inputs.profile
    qualification = inputs.qualification
    reasons: list[ReasonCode] = []

    # 1. Basic eligibility.
    if not inputs.enabled:
        reasons.append(ReasonCode.LANE_DISABLED)
    if not profile.implemented:
        reasons.append(ReasonCode.ADAPTER_UNIMPLEMENTED)
    if not profile.platform_supported:
        reasons.append(ReasonCode.PLATFORM_UNSUPPORTED)
    if not inputs.adapter_found:
        reasons.append(ReasonCode.ADAPTER_NOT_FOUND)

    # 2. Subscription auth and billing policy.
    if qualification is None or qualification.auth_kind is None:
        reasons.append(ReasonCode.AUTH_MISSING)
    else:
        if qualification.auth_kind not in profile.allowed_auth_kinds:
            reasons.append(ReasonCode.AUTH_KIND_FORBIDDEN)
        if not qualification.auth_source:
            reasons.append(ReasonCode.AUTH_SOURCE_UNKNOWN)
        if qualification.overage_disabled is not True:
            reasons.append(ReasonCode.OVERAGE_STATUS_UNKNOWN_OR_ON)

    # 3. Qualification and model policy.
    selected_model: str | None = None
    selected_effort = profile.selected_effort
    if qualification is None or not qualification.qualified:
        reasons.append(ReasonCode.QUALIFICATION_FAILED)
    else:
        if qualification.expires_at.astimezone(timezone.utc) < at:
            reasons.append(ReasonCode.QUALIFICATION_STALE)
        if qualification.provider_id != profile.provider_id:
            reasons.append(ReasonCode.QUALIFICATION_FAILED)
        selected_model = next(
            (
                model
                for model in profile.ordered_models
                if model in qualification.models
            ),
            None,
        )
        if selected_model is None:
            reasons.append(ReasonCode.QUALIFICATION_FAILED)
        if (
            selected_effort is None
            or selected_effort not in qualification.efforts
            or len(tuple(dict.fromkeys(profile.supported_efforts))) < 2
        ):
            reasons.append(ReasonCode.EFFORT_POLICY_UNSATISFIED)
        if not profile.fast_off_verifiable or not qualification.fast_off_supported:
            reasons.append(ReasonCode.QUALIFICATION_FAILED)

    # 4. Capabilities must be proven by both profile and current evidence.
    qualified_capabilities = (
        qualification.capabilities if qualification is not None else frozenset()
    )
    if not task.required_capabilities.issubset(
        profile.capabilities & qualified_capabilities
    ):
        reasons.append(ReasonCode.CAPABILITY_MISMATCH)

    # 5. Occupancy.
    if inputs.active_leases >= inputs.max_concurrency:
        reasons.append(ReasonCode.OCCUPANCY_FULL)

    # 6. Protected reserve.
    snapshot = inputs.capacity.snapshot
    if snapshot is not None:
        after_reservation = (
            snapshot.remaining_pct
            - inputs.active_reserved_pct
            - task.reservation_pct
        )
        if after_reservation < inputs.reserve_floor_pct:
            reasons.append(ReasonCode.RESERVE_FLOOR)

    # 7. Capacity evidence.
    if snapshot is None:
        reasons.append(inputs.capacity.reason or ReasonCode.CAPACITY_MISSING)
    else:
        if snapshot.overage_disabled is not True:
            reasons.append(ReasonCode.OVERAGE_STATUS_UNKNOWN_OR_ON)
        if snapshot.freshness is not Freshness.FRESH:
            reasons.append(inputs.capacity.reason or ReasonCode.CAPACITY_STALE)
        if _CONFIDENCE_RANK[snapshot.confidence] < _CONFIDENCE_RANK[minimum_confidence]:
            reasons.append(ReasonCode.CAPACITY_CONFIDENCE_LOW)

    # 8. Cooldown.
    if inputs.cooldown_until is not None and inputs.cooldown_until > at:
        reasons.append(ReasonCode.LANE_COOLDOWN)

    # Preserve deterministic ordering while avoiding duplicate codes caused by
    # related auth/capacity evidence.
    unique_reasons = tuple(dict.fromkeys(reasons))
    eligible = not unique_reasons
    return LaneEvaluation(
        lane_id=profile.lane_id,
        profile=profile,
        capacity=snapshot,
        eligible=eligible,
        reasons=(ReasonCode.MET,) if eligible else unique_reasons,
        selected_model=selected_model,
        selected_effort=selected_effort,
        qualification_evidence_id=(
            qualification.evidence_id if qualification is not None else ""
        ),
    )


def select_lane(
    evaluations: tuple[LaneEvaluation, ...],
    *,
    rotation_index: int = 0,
    switch_delta: Decimal = SWITCH_DELTA,
) -> RouteDecision:
    """Select a lane deterministically without mutating rotation state."""

    ordered = tuple(sorted(evaluations, key=lambda item: item.profile.order))
    eligible = tuple(
        item
        for item in ordered
        if item.eligible and item.capacity is not None
    )
    if not eligible:
        return RouteDecision(
            lane_id=None,
            reason=ReasonCode.NO_ELIGIBLE_LANE,
            evaluations=ordered,
            rotation_index=rotation_index,
        )

    priority = eligible[0]
    best_remaining = max(
        item.capacity.effective_remaining_pct  # type: ignore[union-attr]
        for item in eligible
    )
    top = tuple(
        item
        for item in eligible
        if item.capacity is not None
        and item.capacity.effective_remaining_pct == best_remaining
    )
    difference = (
        best_remaining - priority.capacity.effective_remaining_pct
    )  # type: ignore[union-attr]

    # Exact top ties rotate even when the priority lane is in the tie. This is
    # the persisted fairness rule; a non-tied lane must clear the exact delta.
    if len(top) > 1:
        chosen_pos = rotation_index % len(top)
        chosen = top[chosen_pos]
        return RouteDecision(
            lane_id=chosen.lane_id,
            reason=ReasonCode.MET,
            evaluations=ordered,
            rotation_index=(chosen_pos + 1) % len(top),
            switch_applied=chosen is not priority,
        )

    if difference < switch_delta:
        return RouteDecision(
            lane_id=priority.lane_id,
            reason=ReasonCode.MET,
            evaluations=ordered,
            rotation_index=rotation_index,
        )

    chosen = top[0]
    return RouteDecision(
        lane_id=chosen.lane_id,
        reason=ReasonCode.MET,
        evaluations=ordered,
        rotation_index=rotation_index,
        switch_applied=chosen is not priority,
    )
