from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from hermes_cli.fleet.adapters.native_provider import NativeProviderAdapter
from hermes_cli.fleet.capacity import BridgeUsageAdapter
from hermes_cli.fleet.config import parse_fleet_config
from hermes_cli.fleet.service import FleetService
from hermes_cli.fleet.state import FleetStore
from hermes_cli.fleet.types import (
    AdapterKind,
    AdapterResult,
    LaneProfile,
    OverageState,
    Qualification,
    ReasonCode,
    TaskSpec,
)


NOW = datetime(2026, 7, 24, tzinfo=timezone.utc)


def _profile(lane_id: str, order: int) -> LaneProfile:
    return LaneProfile(
        lane_id=lane_id,
        order=order,
        adapter_kind=AdapterKind.NATIVE_PROVIDER,
        provider_id=f"{lane_id}-provider",
        ordered_models=("m1", "m2"),
        supported_efforts=("low", "high"),
        capabilities=frozenset({"workspace_write", "shell"}),
        allowed_auth_kinds=frozenset({"oauth_subscription"}),
    )


def _qualification(profile: LaneProfile) -> Qualification:
    return Qualification(
        qualified=True,
        captured_at=NOW,
        expires_at=NOW + timedelta(hours=2),
        auth_kind="oauth_subscription",
        auth_source=f"{profile.lane_id}:subscription",
        overage_disabled=True,
        provider_id=profile.provider_id,
        models=profile.ordered_models,
        efforts=profile.supported_efforts,
        fast_off_supported=True,
        capabilities=profile.capabilities,
        evidence_id=f"qualification:{profile.lane_id}",
        subscription_only_proven=True,
        paid_fallback_absent=True,
        overage_state=OverageState.OFF,
    )


def _bridge(path: Path, capacities: dict[str, str]) -> None:
    lanes = {
        lane_id: {
            "used_pct": str(Decimal("100") - Decimal(remaining)),
            "remaining_pct": remaining,
            "confidence": "high",
            "overage_disabled": True,
            "comparability_group": "subscription-weekly",
            "quota_window_id": "2026-W30",
            "measurement_kind": "measured",
        }
        for lane_id, remaining in capacities.items()
    }
    path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "captured_at": "2026-07-24T00:00:00Z",
                "lanes": lanes,
            }
        ),
        encoding="utf-8",
    )


def _task(task_id: str = "task-1") -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        cwd=Path.cwd(),
        required_capabilities=frozenset({"workspace_write", "shell"}),
        reservation_pct=Decimal("5"),
    )


class _CountingAdapter:
    def __init__(self, result: AdapterResult | None = None) -> None:
        self.calls = []
        self.result = result

    def execute(self, request, qualification):
        self.calls.append((request, qualification))
        return self.result or AdapterResult(
            ok=True,
            reason=ReasonCode.MET,
            provider_id=request.profile.provider_id,
            model_id=request.model,
            auth_kind=qualification.auth_kind,
            adapter_kind=request.profile.adapter_kind,
            output="worker complete",
        )


def _service(tmp_path, *, enabled=True, adapter=None):
    bridge = tmp_path / "usage.json"
    _bridge(bridge, {"chatgpt_codex": "80", "claude_code": "60"})
    profiles = (
        _profile("chatgpt_codex", 0),
        _profile("claude_code", 1),
    )
    adapter = adapter or _CountingAdapter()
    config = parse_fleet_config(
        {
            "fleet": {
                "enabled": enabled,
                "bridge_usage_file": str(bridge),
                "lanes": {
                    "chatgpt_codex": {"enabled": True},
                    "claude_code": {"enabled": True},
                },
            }
        }
    )
    service = FleetService(
        config=config,
        store=FleetStore(tmp_path / "home" / "fleet" / "state.db"),
        profiles=profiles,
        qualifications={
            profile.lane_id: _qualification(profile) for profile in profiles
        },
        adapters={
            "chatgpt_codex": adapter,
            "claude_code": adapter,
        },
        capacity_source=BridgeUsageAdapter(bridge),
        now=lambda: NOW,
        owner_uuid="service-owner",
    )
    return service, adapter, bridge


def test_disabled_run_creates_no_state_and_starts_no_adapter(tmp_path):
    service, adapter, _ = _service(tmp_path, enabled=False)

    result = service.run(_task(), prompt="bounded task")

    assert not result.ok
    assert result.reason is ReasonCode.FLEET_DISABLED
    assert adapter.calls == []
    assert not service.store.path.exists()


def test_new_task_selects_once_executes_then_heartbeats_and_releases(tmp_path):
    service, adapter, _ = _service(tmp_path)

    result = service.run(_task(), prompt="bounded task")

    assert result.ok
    assert result.pin is not None
    assert result.pin.lane_id == "chatgpt_codex"
    assert len(adapter.calls) == 1
    assert adapter.calls[0][0].model == "m1"
    assert adapter.calls[0][0].effort == "low"
    events = service.store.audit(task_id="task-1")
    assert [event["event_type"] for event in events].count("ROUTE_SELECTED") == 1
    assert {"LEASE_HEARTBEAT", "EXECUTION_STARTED", "EXECUTION_COMPLETED", "LEASE_RELEASED"} <= {
        event["event_type"] for event in events
    }


def test_continuation_keeps_pin_when_capacity_reverses(tmp_path):
    service, adapter, bridge = _service(tmp_path)
    first = service.run(_task(), prompt="first")
    assert first.pin is not None
    _bridge(bridge, {"chatgpt_codex": "20", "claude_code": "100"})

    continued = service.run(_task(), prompt="continue")

    assert continued.ok
    assert continued.pin is not None
    assert continued.pin.lane_id == first.pin.lane_id == "chatgpt_codex"
    assert len(adapter.calls) == 2
    assert [
        event["event_type"] for event in service.store.audit(task_id="task-1")
    ].count("ROUTE_SELECTED") == 1


def test_pinned_lane_unavailable_does_not_migrate_or_start_another_adapter(tmp_path):
    service, adapter, _ = _service(tmp_path)
    first = service.run(_task(), prompt="first")
    assert first.ok
    service.qualifications.pop("chatgpt_codex")

    continued = service.run(_task(), prompt="continue")

    assert not continued.ok
    assert continued.reason is ReasonCode.PINNED_LANE_UNAVAILABLE
    assert continued.pin is not None
    assert continued.pin.lane_id == "chatgpt_codex"
    assert len(adapter.calls) == 1


def test_any_failed_gate_means_no_child_process(tmp_path):
    service, adapter, _ = _service(tmp_path)
    service.qualifications["chatgpt_codex"] = replace(
        service.qualifications["chatgpt_codex"], auth_kind="api_key"
    )
    service.qualifications["claude_code"] = replace(
        service.qualifications["claude_code"], overage_disabled=None
    )

    result = service.run(_task(), prompt="must not execute")

    assert not result.ok
    assert result.reason is ReasonCode.NO_ELIGIBLE_LANE
    assert adapter.calls == []
    reasons = {
        reason
        for evaluation in result.evaluations
        for reason in evaluation.reasons
    }
    assert ReasonCode.AUTH_KIND_FORBIDDEN in reasons
    assert ReasonCode.OVERAGE_STATUS_UNKNOWN_OR_ON in reasons


def test_rate_limit_result_sets_cooldown_and_releases_lease(tmp_path):
    failure = AdapterResult(
        ok=False,
        reason=ReasonCode.EXECUTION_FAILED,
        provider_id="chatgpt_codex-provider",
        model_id="m1",
        auth_kind="oauth_subscription",
        adapter_kind=AdapterKind.NATIVE_PROVIDER,
        metadata={"cooldown_seconds": 120, "classification": "rate_limit"},
    )
    adapter = _CountingAdapter(failure)
    service, _, _ = _service(tmp_path, adapter=adapter)

    result = service.run(_task(), prompt="bounded task")

    assert not result.ok
    assert service.store.cooldown("chatgpt_codex", now=NOW) == (
        NOW + timedelta(seconds=120),
        "PROVIDER_RATE_LIMIT",
    )
    assert service.store.active_reserved_pct("chatgpt_codex", now=NOW) == 0


def test_stale_rotation_rate_limit_keeps_cooldown_and_audit_contract(tmp_path):
    bridge = tmp_path / "usage.json"
    bridge.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "captured_at": "2026-07-23T00:00:00Z",
                "lanes": {
                    "grok": {
                        "used_pct": "1",
                        "remaining_pct": "99",
                        "confidence": "low",
                        "overage_disabled": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    profile = _profile("grok", 0)
    failure = AdapterResult(
        ok=False,
        reason=ReasonCode.EXECUTION_FAILED,
        provider_id=profile.provider_id,
        model_id="m1",
        auth_kind="oauth_subscription",
        adapter_kind=AdapterKind.NATIVE_PROVIDER,
        metadata={"cooldown_seconds": 120, "classification": "rate_limit"},
    )
    adapter = _CountingAdapter(failure)
    config = parse_fleet_config(
        {
            "fleet": {
                "enabled": True,
                "bridge_usage_file": str(bridge),
                "rotation_without_fresh_capacity": True,
                "lanes": {"grok": {"enabled": True}},
            }
        }
    )
    service = FleetService(
        config=config,
        store=FleetStore(tmp_path / "state.db"),
        profiles=(profile,),
        qualifications={"grok": _qualification(profile)},
        adapters={"grok": adapter},
        capacity_source=BridgeUsageAdapter(bridge),
        now=lambda: NOW,
        owner_uuid="service-owner",
    )

    result = service.run(_task(), prompt="bounded task")

    assert not result.ok
    assert result.evaluations[0].eligible
    assert ReasonCode.USAGE_STALE in result.evaluations[0].reasons
    assert service.store.cooldown("grok", now=NOW) == (
        NOW + timedelta(seconds=120),
        "PROVIDER_RATE_LIMIT",
    )
    route = next(
        event
        for event in service.store.audit(task_id="task-1")
        if event["event_type"] == "ROUTE_SELECTED"
    )
    assert route["reason_code"] == ReasonCode.ROTATION.value
    assert route["decision"]["selection_reason"] == route["reason_code"]
    assert route["decision"]["capacity_source"] is None


def test_plan_is_read_only_and_does_not_advance_rotation(tmp_path):
    service, _, _ = _service(tmp_path)

    first = service.plan(_task())
    second = service.plan(_task())

    assert first == second
    assert first.lane_id == "chatgpt_codex"
    assert not service.store.path.exists()
