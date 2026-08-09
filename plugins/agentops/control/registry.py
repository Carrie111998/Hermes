"""Stable, in-memory registration for Phase 2 observation targets."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from plugins.agentops.control.observer_models import (
    Criticality,
    FleetCoverage,
    Target,
    TargetKind,
    TargetSnapshot,
    TargetSpec,
)


class TargetRegistrationError(ValueError):
    """Raised when the immutable fleet inventory would become ambiguous."""


class FleetRegistry:
    """Registry with no lifecycle, authority, or target mutation interface."""

    def __init__(self, targets: Iterable[TargetSpec] = ()) -> None:
        self._targets: dict[str, Target] = {}
        self._snapshots: dict[str, TargetSnapshot] = {}
        for spec in targets:
            self.register_target(spec)

    def register_target(self, spec: TargetSpec) -> Target:
        if not isinstance(spec, TargetSpec):
            raise TargetRegistrationError("invalid target specification")
        if spec.target_id in self._targets:
            raise TargetRegistrationError("target id already registered")
        target = Target(spec=spec)
        self._targets[target.target_id] = target
        return target

    def record_target_snapshot(self, snapshot: TargetSnapshot) -> None:
        if not isinstance(snapshot, TargetSnapshot) or snapshot.target_id not in self._targets:
            raise TargetRegistrationError("target snapshot is not registered")
        existing = self._snapshots.get(snapshot.target_id)
        if existing is not None and snapshot.observed_at < existing.observed_at:
            raise TargetRegistrationError("target snapshot is older than current snapshot")
        self._snapshots[snapshot.target_id] = snapshot

    def get_target(self, target_id: str) -> Target:
        try:
            return self._targets[target_id]
        except KeyError as exc:
            raise TargetRegistrationError("target is not registered") from exc

    def list_targets(self) -> tuple[Target, ...]:
        return tuple(self._targets[target_id] for target_id in sorted(self._targets))

    def get_snapshot(self, target_id: str) -> TargetSnapshot | None:
        self.get_target(target_id)
        return self._snapshots.get(target_id)

    def coverage_report(self) -> FleetCoverage:
        registered = len(self._targets)
        snapshotted = len(self._snapshots)
        coverage = 0 if registered == 0 else (snapshotted * 100) // registered
        disabled = ("processes",) if any(target.spec.labels.get("process_observation") == "disabled" for target in self._targets.values()) else ()
        return FleetCoverage(registered, snapshotted, coverage, disabled)


_PROFILE_TARGETS: tuple[tuple[str, str, str, Criticality, str], ...] = (
    ("default", "ai.hermes.gateway", "~/.hermes/logs/", Criticality.CRITICAL, "default"),
    ("feishu3", "ai.hermes.gateway-feishu3", "~/.hermes/profiles/feishu3/logs/", Criticality.NONCRITICAL, "feishu3"),
    ("feishu4", "ai.hermes.gateway-feishu4", "~/.hermes/profiles/feishu4/logs/", Criticality.NONCRITICAL, "feishu4"),
    ("feishu5", "ai.hermes.gateway-feishu5", "~/.hermes/profiles/feishu5/logs/", Criticality.NONCRITICAL, "feishu5"),
    ("newbot", "ai.hermes.gateway-newbot", "~/.hermes/profiles/newbot/logs/", Criticality.NONCRITICAL, "newbot"),
)


def bootstrap_gateway_registry() -> FleetRegistry:
    """Return the fixed five-profile inventory recorded during Phase 0."""
    specs = (
        TargetSpec(
            target_id=f"hermes:profile:{profile}:gateway",
            profile=profile,
            kind=TargetKind.GATEWAY,
            criticality=criticality,
            observed_paths=(logs_path, f"~/Library/LaunchAgents/{label}.plist"),
            labels={"service_label": label, "profile": label_profile, "process_observation": "disabled"},
            existing_writer="launchd+hermes_gateway_watchdog",
        )
        for profile, label, logs_path, criticality, label_profile in _PROFILE_TARGETS
    )
    return FleetRegistry(specs)


def snapshot_all_targets(registry: FleetRegistry, *, observed_at: datetime, facts: dict[str, object]) -> None:
    """Small deterministic helper used by the first-fleet snapshot workflow."""
    for target in registry.list_targets():
        registry.record_target_snapshot(
            TargetSnapshot(target_id=target.target_id, observed_at=observed_at, facts=facts)
        )
