from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from hermes_cli.fleet.policy import evaluate_lane, select_lane
from hermes_cli.fleet.types import (
    AdapterKind,
    CapacityRead,
    CapacitySnapshot,
    Confidence,
    Freshness,
    LaneInputs,
    LaneProfile,
    Qualification,
    ReasonCode,
    TaskSpec,
)


NOW = datetime(2026, 7, 24, tzinfo=timezone.utc)
TASK = TaskSpec(
    task_id="task-1",
    cwd=Path("."),
    required_capabilities=frozenset({"workspace_write", "shell"}),
    reservation_pct=Decimal("5.000"),
)


def _profile(lane_id: str = "chatgpt_codex", order: int = 0) -> LaneProfile:
    return LaneProfile(
        lane_id=lane_id,
        order=order,
        adapter_kind=AdapterKind.NATIVE_PROVIDER,
        provider_id=f"{lane_id}-provider",
        ordered_models=("m1", "m2", "m3"),
        supported_efforts=("low", "medium", "high", "max"),
        capabilities=frozenset({"workspace_write", "shell", "vision"}),
        allowed_auth_kinds=frozenset({"oauth_subscription"}),
    )


def _qualification(profile: LaneProfile) -> Qualification:
    return Qualification(
        qualified=True,
        captured_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(hours=1),
        auth_kind="oauth_subscription",
        auth_source=f"{profile.lane_id}:subscription",
        overage_disabled=True,
        provider_id=profile.provider_id,
        models=profile.ordered_models,
        efforts=profile.supported_efforts,
        fast_off_supported=True,
        capabilities=profile.capabilities,
        evidence_id=f"qualification:{profile.lane_id}",
    )


def _capacity(lane_id: str, remaining: str = "60.000") -> CapacityRead:
    rem = Decimal(remaining)
    return CapacityRead(
        CapacitySnapshot(
            lane_id=lane_id,
            used_pct=(Decimal("100.000") - rem),
            remaining_pct=rem,
            reserved_pct=Decimal("0"),
            effective_remaining_pct=rem,
            source_kind="bridge_file",
            source_id=f"bridge:{lane_id}#hash",
            captured_at=NOW - timedelta(minutes=5),
            read_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            freshness=Freshness.FRESH,
            confidence=Confidence.HIGH,
            schema_version="1",
            overage_disabled=True,
        ),
        None,
    )


def _inputs(profile: LaneProfile | None = None, remaining: str = "60.000"):
    profile = profile or _profile()
    return LaneInputs(
        profile=profile,
        enabled=True,
        adapter_found=True,
        qualification=_qualification(profile),
        capacity=_capacity(profile.lane_id, remaining),
        max_concurrency=1,
        reserve_floor_pct=Decimal("10.000"),
    )


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda i: replace(i, enabled=False), ReasonCode.LANE_DISABLED),
        (
            lambda i: replace(i, profile=replace(i.profile, implemented=False)),
            ReasonCode.ADAPTER_UNIMPLEMENTED,
        ),
        (
            lambda i: replace(
                i, profile=replace(i.profile, platform_supported=False)
            ),
            ReasonCode.PLATFORM_UNSUPPORTED,
        ),
        (lambda i: replace(i, adapter_found=False), ReasonCode.ADAPTER_NOT_FOUND),
        (lambda i: replace(i, qualification=None), ReasonCode.AUTH_MISSING),
        (
            lambda i: replace(
                i,
                qualification=replace(
                    i.qualification, auth_kind="api_key"  # type: ignore[arg-type]
                ),
            ),
            ReasonCode.AUTH_KIND_FORBIDDEN,
        ),
        (
            lambda i: replace(
                i,
                qualification=replace(
                    i.qualification, auth_source=None  # type: ignore[arg-type]
                ),
            ),
            ReasonCode.AUTH_SOURCE_UNKNOWN,
        ),
        (
            lambda i: replace(
                i,
                qualification=replace(
                    i.qualification, overage_disabled=None  # type: ignore[arg-type]
                ),
            ),
            ReasonCode.OVERAGE_STATUS_UNKNOWN_OR_ON,
        ),
        (
            lambda i: replace(
                i,
                qualification=replace(
                    i.qualification, qualified=False  # type: ignore[arg-type]
                ),
            ),
            ReasonCode.QUALIFICATION_FAILED,
        ),
        (
            lambda i: replace(
                i,
                qualification=replace(
                    i.qualification,
                    expires_at=NOW - timedelta(seconds=1),  # type: ignore[arg-type]
                ),
            ),
            ReasonCode.QUALIFICATION_STALE,
        ),
        (
            lambda i: replace(
                i, profile=replace(i.profile, supported_efforts=("max",))
            ),
            ReasonCode.EFFORT_POLICY_UNSATISFIED,
        ),
        (
            lambda i: replace(
                i,
                qualification=replace(
                    i.qualification,
                    capabilities=frozenset({"workspace_write"}),  # type: ignore[arg-type]
                ),
            ),
            ReasonCode.CAPABILITY_MISMATCH,
        ),
        (lambda i: replace(i, active_leases=1), ReasonCode.OCCUPANCY_FULL),
        (
            lambda i: replace(
                i,
                active_reserved_pct=Decimal("46"),
            ),
            ReasonCode.RESERVE_FLOOR,
        ),
        (
            lambda i: replace(
                i, capacity=CapacityRead(None, ReasonCode.CAPACITY_MISSING)
            ),
            ReasonCode.CAPACITY_MISSING,
        ),
        (
            lambda i: replace(
                i,
                capacity=replace(
                    i.capacity,
                    snapshot=replace(
                        i.capacity.snapshot, freshness=Freshness.STALE  # type: ignore[arg-type]
                    ),
                    reason=ReasonCode.CAPACITY_STALE,
                ),
            ),
            ReasonCode.CAPACITY_STALE,
        ),
        (
            lambda i: replace(
                i,
                capacity=replace(
                    i.capacity,
                    snapshot=replace(
                        i.capacity.snapshot, confidence=Confidence.LOW  # type: ignore[arg-type]
                    ),
                ),
            ),
            ReasonCode.CAPACITY_CONFIDENCE_LOW,
        ),
        (
            lambda i: replace(i, cooldown_until=NOW + timedelta(minutes=1)),
            ReasonCode.LANE_COOLDOWN,
        ),
    ],
)
def test_each_gate_fails_closed_and_preserves_a_reason_matrix(mutate, reason):
    evaluation = evaluate_lane(mutate(_inputs()), TASK, now=NOW)

    assert not evaluation.eligible
    assert reason in evaluation.reasons


def test_model_policy_uses_strongest_model_second_highest_effort_and_fast_off():
    evaluation = evaluate_lane(_inputs(), TASK, now=NOW)

    assert evaluation.eligible
    assert evaluation.reasons == (ReasonCode.MET,)
    assert evaluation.selected_model == "m1"
    assert evaluation.selected_effort == "high"


@pytest.mark.parametrize(
    ("delta", "expected_lane", "switched"),
    [
        ("19.999", "chatgpt_codex", False),
        ("20.000", "claude_code", True),
        ("20.001", "claude_code", True),
    ],
)
def test_exact_twenty_point_switch_boundary(delta, expected_lane, switched):
    priority = _inputs(_profile("chatgpt_codex", 0), "60.000")
    alternative = _inputs(
        _profile("claude_code", 1), str(Decimal("60.000") + Decimal(delta))
    )
    evaluations = tuple(
        evaluate_lane(item, TASK, now=NOW) for item in (priority, alternative)
    )

    decision = select_lane(evaluations, rotation_index=0)

    assert decision.lane_id == expected_lane
    assert decision.switch_applied is switched


def test_exact_top_capacity_ties_rotate_in_fixed_order_without_hidden_mutation():
    evaluations = tuple(
        evaluate_lane(
            _inputs(_profile(lane_id, order), "90.000"), TASK, now=NOW
        )
        for order, lane_id in enumerate(
            ("chatgpt_codex", "claude_code", "grok")
        )
    )

    first = select_lane(evaluations, rotation_index=0)
    same_dry_run = select_lane(evaluations, rotation_index=0)
    second = select_lane(evaluations, rotation_index=first.rotation_index)

    assert first.lane_id == "chatgpt_codex"
    assert same_dry_run == first
    assert second.lane_id == "claude_code"


def test_live_capacity_shape_rotates_fallbacks_without_stale_override():
    candidates = (
        _inputs(_profile("chatgpt_codex", 0), "84.000"),
        _inputs(_profile("claude_code", 1), "11.000"),
        replace(
            _inputs(_profile("grok", 2), "99.000"),
            rotation_without_fresh_capacity=True,
            capacity=replace(
                _capacity("grok", "99.000"),
                snapshot=replace(
                    _capacity("grok", "99.000").snapshot,
                    freshness=Freshness.STALE,
                    confidence=Confidence.LOW,
                ),
                reason=ReasonCode.CAPACITY_STALE,
            ),
        ),
        replace(
            _inputs(_profile("antigravity", 3), "100.000"),
            rotation_without_fresh_capacity=True,
            capacity=replace(
                _capacity("antigravity", "100.000"),
                snapshot=replace(
                    _capacity("antigravity", "100.000").snapshot,
                    freshness=Freshness.STALE,
                    confidence=Confidence.LOW,
                ),
                reason=ReasonCode.CAPACITY_STALE,
            ),
        ),
    )
    evaluations = tuple(
        evaluate_lane(candidate, TASK, now=NOW) for candidate in candidates
    )

    claude, grok, agy = evaluations[1:]
    assert not claude.eligible and not claude.fallback_eligible
    assert ReasonCode.RESERVE_FLOOR in claude.reasons
    assert not grok.eligible and grok.fallback_eligible
    assert not agy.eligible and agy.fallback_eligible
    assert ReasonCode.ROTATION_WITHOUT_FRESH_CAPACITY in grok.reasons
    assert ReasonCode.MET not in grok.reasons

    first = select_lane(evaluations, rotation_index=0)
    grok_turn = select_lane(evaluations, rotation_index=1)
    agy_turn = select_lane(evaluations, rotation_index=2)

    assert (first.lane_id, first.rotation_index) == ("chatgpt_codex", 1)
    assert (grok_turn.lane_id, grok_turn.rotation_index) == ("grok", 2)
    assert grok_turn.reason is ReasonCode.ROTATION_WITHOUT_FRESH_CAPACITY
    assert (agy_turn.lane_id, agy_turn.rotation_index) == ("antigravity", 0)
    assert agy_turn.reason is ReasonCode.ROTATION_WITHOUT_FRESH_CAPACITY


def test_stale_fallback_capacity_never_participates_in_reserve_arithmetic():
    stale = replace(
        _inputs(_profile("grok", 0), "1.000"),
        rotation_without_fresh_capacity=True,
        capacity=replace(
            _capacity("grok", "1.000"),
            snapshot=replace(
                _capacity("grok", "1.000").snapshot,
                freshness=Freshness.STALE,
                confidence=Confidence.LOW,
            ),
            reason=ReasonCode.CAPACITY_STALE,
        ),
    )

    evaluation = evaluate_lane(stale, TASK, now=NOW)

    assert evaluation.fallback_eligible
    assert ReasonCode.RESERVE_FLOOR not in evaluation.reasons


def test_no_eligible_lane_returns_complete_evaluations():
    items = (
        replace(_inputs(_profile("chatgpt_codex", 0)), enabled=False),
        replace(_inputs(_profile("claude_code", 1)), adapter_found=False),
    )
    evaluations = tuple(evaluate_lane(item, TASK, now=NOW) for item in items)

    decision = select_lane(evaluations)

    assert decision.lane_id is None
    assert decision.reason is ReasonCode.NO_ELIGIBLE_LANE
    assert {item.lane_id for item in decision.evaluations} == {
        "chatgpt_codex",
        "claude_code",
    }
