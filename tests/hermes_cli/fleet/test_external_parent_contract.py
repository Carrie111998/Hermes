from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from hermes_cli.fleet.live import FleetQualificationDoctor
from hermes_cli.fleet.policy import evaluate_lane
from hermes_cli.fleet.profiles import profile_map
from hermes_cli.fleet.types import (
    AdapterKind,
    CapacityRead,
    LaneInputs,
    OverageState,
    ReasonCode,
    RoutePurpose,
    TaskSpec,
)


NOW = datetime(2026, 7, 24, tzinfo=timezone.utc)


def _doctor() -> FleetQualificationDoctor:
    def command(argv):
        if argv[1] == "--version":
            return 0, "agy 1.1.6", ""
        if argv[1] == "models":
            return 0, "gemini-3.1-pro-high", ""
        raise AssertionError(argv)

    return FleetQualificationDoctor(
        which=lambda _: "C:/tools/agy.exe",
        command=command,
        environment={},
        billing_status=lambda _: {"overage_state": "off"},
        now=lambda: NOW,
    )


def test_one_shot_agy_receipt_never_claims_parent_session_proof():
    qualification = _doctor().qualify((profile_map()["antigravity"],))[
        "antigravity"
    ]

    assert qualification.qualified
    assert qualification.parent_session_proven is False
    assert "--conversation" in qualification.detail
    assert "--continue" in qualification.detail
    assert "--remote-control" in qualification.detail
    assert "two-turn" in qualification.detail
    assert "served-model" in qualification.detail


def test_external_parent_support_flag_cannot_bypass_continuity_proof():
    profile = replace(
        profile_map()["antigravity"],
        supports_parent_session=True,
    )
    qualification = _doctor().qualify((profile,))["antigravity"]
    evaluation = evaluate_lane(
        LaneInputs(
            profile=profile,
            enabled=True,
            adapter_found=True,
            qualification=qualification,
            capacity=CapacityRead(None, ReasonCode.CAPACITY_MISSING),
            max_concurrency=1,
            reserve_floor_pct=Decimal("0"),
        ),
        TaskSpec(
            task_id="external-parent-contract",
            cwd=Path("."),
            required_capabilities=frozenset({"workspace_write", "shell"}),
        ),
        now=NOW,
        purpose=RoutePurpose.DESKTOP_PARENT,
    )

    assert not evaluation.eligible
    assert ReasonCode.PARENT_SESSION_UNPROVEN in evaluation.reasons


def test_antigravity_remains_truthful_external_worker_until_live_gate_passes():
    profile = profile_map()["antigravity"]

    assert profile.adapter_kind is AdapterKind.EXTERNAL_CLI
    assert profile.supports_task_worker is True
    assert profile.supports_parent_session is False
