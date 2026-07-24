from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from hermes_cli.fleet.state import FleetStore
from hermes_cli.fleet.types import (
    AdapterKind,
    CapacityRead,
    CapacitySnapshot,
    Confidence,
    Freshness,
    LaneInputs,
    LaneProfile,
    MeasurementKind,
    OverageState,
    Qualification,
    ReasonCode,
    TaskSpec,
)


NOW = datetime(2026, 7, 24, tzinfo=timezone.utc)


def _candidate(
    lane_id: str = "chatgpt_codex",
    *,
    order: int = 0,
    remaining: str = "90",
    concurrency: int = 1,
) -> LaneInputs:
    profile = LaneProfile(
        lane_id=lane_id,
        order=order,
        adapter_kind=AdapterKind.NATIVE_PROVIDER,
        provider_id=f"{lane_id}-provider",
        ordered_models=("m1", "m2"),
        supported_efforts=("low", "high"),
        capabilities=frozenset({"workspace_write", "shell"}),
        allowed_auth_kinds=frozenset({"oauth_subscription"}),
    )
    qualification = Qualification(
        qualified=True,
        captured_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        auth_kind="oauth_subscription",
        auth_source=f"{lane_id}:subscription",
        overage_disabled=True,
        provider_id=profile.provider_id,
        models=profile.ordered_models,
        efforts=profile.supported_efforts,
        fast_off_supported=True,
        capabilities=profile.capabilities,
        evidence_id=f"qualification:{lane_id}",
        subscription_only_proven=True,
        paid_fallback_absent=True,
        overage_state=OverageState.OFF,
    )
    rem = Decimal(remaining).quantize(Decimal("0.001"))
    capacity = CapacitySnapshot(
        lane_id=lane_id,
        used_pct=Decimal("100") - rem,
        remaining_pct=rem,
        reserved_pct=Decimal("0"),
        effective_remaining_pct=rem,
        source_kind="bridge_file",
        source_id=f"bridge:{lane_id}#hash",
        captured_at=NOW,
        read_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        freshness=Freshness.FRESH,
        confidence=Confidence.HIGH,
        schema_version="1",
        overage_disabled=True,
        comparability_group="subscription-weekly",
        quota_window_id="2026-W30",
        measurement_kind=MeasurementKind.MEASURED,
    )
    return LaneInputs(
        profile=profile,
        enabled=True,
        adapter_found=True,
        qualification=qualification,
        capacity=CapacityRead(capacity, None),
        max_concurrency=concurrency,
        reserve_floor_pct=Decimal("10"),
    )


def _task(task_id: str, reservation: str = "5") -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        cwd=Path("C:/workspace"),
        required_capabilities=frozenset({"workspace_write", "shell"}),
        reservation_pct=Decimal(reservation),
        prompt_fingerprint=f"sha256:{task_id}",
    )


def test_store_is_lazy_and_creates_isolated_schema_only_on_mutation(tmp_path):
    db = tmp_path / "fleet" / "state.db"
    store = FleetStore(db)

    assert not db.exists()
    assert store.read_pin("missing") is None
    assert not db.exists()

    result = store.acquire(
        _task("task-1"),
        (_candidate(),),
        owner_uuid="owner-1",
        ttl_seconds=60,
        now=NOW,
    )

    assert result.reason is ReasonCode.MET
    assert db.exists()
    assert result.pin is not None
    assert result.lease is not None
    assert result.pin.lane_id == "chatgpt_codex"
    assert result.pin.model_id == "m1"
    assert result.pin.effort == "low"


def test_atomic_acquire_pins_reserves_rotates_and_audits(tmp_path):
    store = FleetStore(tmp_path / "state.db")
    candidates = (
        _candidate("chatgpt_codex", order=0, concurrency=2),
        _candidate("claude_code", order=1, concurrency=2),
    )

    first = store.acquire(
        _task("task-1", reservation="0"),
        candidates,
        owner_uuid="owner-1",
        ttl_seconds=60,
        now=NOW,
    )
    second = store.acquire(
        _task("task-2"),
        candidates,
        owner_uuid="owner-2",
        ttl_seconds=60,
        now=NOW,
    )

    assert first.pin is not None and first.pin.lane_id == "chatgpt_codex"
    assert second.pin is not None and second.pin.lane_id == "claude_code"
    assert store.active_reserved_pct("chatgpt_codex", now=NOW) == Decimal("0")
    assert store.active_reserved_pct("claude_code", now=NOW) == Decimal("5")
    assert store.rotation_cursor() == 0
    events = store.audit()
    assert [event["event_type"] for event in events].count("ROUTE_SELECTED") == 2
    assert all("sha256:task" not in json.dumps(event) for event in events)


def test_existing_task_is_pinned_and_never_reselected_when_capacity_reverses(tmp_path):
    store = FleetStore(tmp_path / "state.db")
    original = (
        _candidate("chatgpt_codex", order=0, remaining="90"),
        _candidate("claude_code", order=1, remaining="10"),
    )
    acquired = store.acquire(
        _task("task-1"),
        original,
        owner_uuid="owner-1",
        ttl_seconds=60,
        now=NOW,
    )
    assert acquired.lease is not None
    store.release(acquired.lease, outcome="completed", now=NOW)

    reversed_capacity = (
        _candidate("chatgpt_codex", order=0, remaining="1"),
        _candidate("claude_code", order=1, remaining="100"),
    )
    continued = store.acquire(
        _task("task-1"),
        reversed_capacity,
        owner_uuid="owner-2",
        ttl_seconds=60,
        now=NOW + timedelta(seconds=1),
    )

    assert continued.pin is not None
    assert continued.pin.lane_id == "chatgpt_codex"
    assert continued.reason is ReasonCode.PINNED_LANE_UNAVAILABLE
    assert continued.lease is None


def test_owner_generation_guards_heartbeat_and_release(tmp_path):
    store = FleetStore(tmp_path / "state.db")
    acquired = store.acquire(
        _task("task-1"),
        (_candidate(),),
        owner_uuid="owner-1",
        ttl_seconds=60,
        now=NOW,
    )
    assert acquired.lease is not None

    renewed = store.heartbeat(
        acquired.lease, ttl_seconds=60, now=NOW + timedelta(seconds=30)
    )
    stale = replace(acquired.lease, owner_uuid="stale-owner")

    assert renewed is not None
    assert renewed.expires_at == NOW + timedelta(seconds=90)
    assert store.heartbeat(stale, ttl_seconds=60, now=NOW) is None
    assert not store.release(stale, outcome="failed", now=NOW)
    assert store.release(renewed, outcome="completed", now=NOW)
    assert not store.release(renewed, outcome="completed", now=NOW)


def test_expiry_reap_and_cooldown_are_reason_coded(tmp_path):
    store = FleetStore(tmp_path / "state.db")
    acquired = store.acquire(
        _task("task-1"),
        (_candidate(),),
        owner_uuid="owner-1",
        ttl_seconds=60,
        now=NOW,
    )
    assert acquired.lease is not None
    store.set_cooldown(
        "chatgpt_codex",
        until=NOW + timedelta(minutes=5),
        reason="PROVIDER_RATE_LIMIT",
        now=NOW,
    )

    assert store.reap_expired(now=NOW + timedelta(seconds=61)) == 1
    assert store.cooldown("chatgpt_codex", now=NOW + timedelta(minutes=1)) == (
        NOW + timedelta(minutes=5),
        "PROVIDER_RATE_LIMIT",
    )
    assert store.cooldown("chatgpt_codex", now=NOW + timedelta(minutes=6)) is None
    reasons = {event["reason_code"] for event in store.audit()}
    assert {"PROVIDER_RATE_LIMIT", "LEASE_TTL_EXPIRED"} <= reasons


def test_injected_transaction_failure_rolls_back_pin_lease_cursor_and_audit(tmp_path):
    store = FleetStore(tmp_path / "state.db")

    with pytest.raises(RuntimeError, match="injected"):
        store.acquire(
            _task("task-1"),
            (
                _candidate("chatgpt_codex", order=0),
                _candidate("claude_code", order=1),
            ),
            owner_uuid="owner-1",
            ttl_seconds=60,
            now=NOW,
            inject_failure=True,
        )

    assert store.read_pin("task-1") is None
    assert store.active_reserved_pct("chatgpt_codex", now=NOW) == 0
    assert store.rotation_cursor() == 0
    assert store.audit() == []


def test_32_concurrent_selectors_cannot_double_reserve_capacity_one(tmp_path):
    db = tmp_path / "state.db"

    def attempt(index: int):
        return FleetStore(db).acquire(
            _task(f"task-{index}"),
            (_candidate(concurrency=1),),
            owner_uuid=f"owner-{index}",
            ttl_seconds=60,
            now=NOW,
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(attempt, range(32)))

    winners = [result for result in results if result.lease is not None]
    assert len(winners) == 1
    assert {
        result.reason for result in results if result.lease is None
    } == {ReasonCode.NO_ELIGIBLE_LANE}
    assert FleetStore(db).active_reserved_pct("chatgpt_codex", now=NOW) == 5


def test_parent_pin_read_is_read_only_on_an_absent_database(tmp_path):
    store = FleetStore(tmp_path / "fleet" / "state.db")

    assert store.read_parent_pin("default", "lineage-1") is None
    assert not store.path.exists()
