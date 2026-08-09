"""Stable, in-memory registration for Phase 2 observation targets."""

from __future__ import annotations

from datetime import datetime
import hashlib
import os
import plistlib
import json
import re
import stat
from pathlib import Path
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
        self._process_health: dict[str, tuple[datetime, str, bool]] = {}
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

    def record_process_result(self, batch) -> None:
        from plugins.agentops.control.observer_models import CollectionBatch, TargetKind, utc_now
        import os
        if not isinstance(batch, CollectionBatch) or batch.collector != "processes" or not batch.source_id:
            raise TargetRegistrationError("process result must be a processes CollectionBatch")
        target_id, healthy = batch.target_id, batch.health.healthy
        target = self.get_target(target_id)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", batch.source_id) or any(
            signal.collector != "processes" or signal.target_id != target_id or signal.signal_type != "process.snapshot"
            or signal.observed_at < batch.collected_at - __import__("datetime").timedelta(seconds=5)
            or signal.observed_at > batch.collected_at + __import__("datetime").timedelta(seconds=5)
            or not {"command_fingerprint", "profile_marker", "owner_uid"}.issubset(signal.payload)
            or signal.payload.get("command_fingerprint") != target.spec.labels.get("command_fingerprint")
            or signal.payload.get("profile_marker") != target.spec.labels.get("process_marker")
            or int(signal.payload.get("owner_uid", -1)) != os.getuid()
            for signal in batch.signals
        ):
            raise TargetRegistrationError("process result evidence binding rejected")
        previous = self._process_health.get(target_id)
        if previous is not None and batch.collected_at < previous[0]:
            raise TargetRegistrationError("stale process result")
        self._process_health[target_id] = (batch.collected_at, batch.source_id, bool(healthy and batch.signals))

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
        out_scope = tuple(sorted(target.target_id for target in self._targets.values() if target.spec.labels.get("g2_scope") == "out_of_scope"))
        core_targets = [target for target in self._targets.values() if target.spec.labels.get("g2_scope", "core") == "core"]
        core_snapshots = 0
        for target in core_targets:
            snapshot = self._snapshots.get(target.target_id)
            process = self._process_health.get(target.target_id)
            if snapshot is not None and process is not None and process[2] and process[0] >= snapshot.observed_at:
                core_snapshots += 1
        disabled_count = sum(1 for target in core_targets if target.spec.labels.get("process_observation") == "disabled")
        coverage = 0 if not core_targets else (max(0, core_snapshots - disabled_count) * 100) // len(core_targets)
        disabled = ("processes",) if disabled_count else ()
        return FleetCoverage(len(core_targets), core_snapshots, coverage, disabled, out_scope)


_PROFILE_TARGETS: tuple[tuple[str, str, str, Criticality, str], ...] = (
    ("default", "ai.hermes.gateway", "~/.hermes/logs/", Criticality.CRITICAL, "default"),
    ("feishu3", "ai.hermes.gateway-feishu3", "~/.hermes/profiles/feishu3/logs/", Criticality.NONCRITICAL, "feishu3"),
    ("feishu4", "ai.hermes.gateway-feishu4", "~/.hermes/profiles/feishu4/logs/", Criticality.NONCRITICAL, "feishu4"),
    ("feishu5", "ai.hermes.gateway-feishu5", "~/.hermes/profiles/feishu5/logs/", Criticality.NONCRITICAL, "feishu5"),
    ("newbot", "ai.hermes.gateway-newbot", "~/.hermes/profiles/newbot/logs/", Criticality.NONCRITICAL, "newbot"),
)


def _parse_deployment_asset(raw: bytes, label: str) -> list[str]:
    try:
        try:
            data = plistlib.loads(raw)
            args = data.get("ProgramArguments")
        except (plistlib.InvalidFileException, ValueError):
            args = json.loads(raw.decode("utf-8"))
        if isinstance(args, dict):
            args = args.get("ProgramArguments")
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise ValueError("deployment arguments invalid")
        return args
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, plistlib.InvalidFileException) as exc:
        raise ValueError("deployment asset rejected") from exc


def bootstrap_gateway_registry() -> FleetRegistry:
    """Return the fixed five-profile inventory recorded during Phase 0."""
    def spec_for(profile, label, logs_path, criticality, label_profile):
        plist = Path(f"~/Library/LaunchAgents/{label}.plist").expanduser()
        labels = {"service_label": label, "profile": label_profile, "g2_scope": "core" if profile == "default" else "out_of_scope"}
        try:
            meta = plist.lstat()
            if stat.S_ISREG(meta.st_mode) and not stat.S_ISLNK(meta.st_mode) and meta.st_uid == os.getuid() and meta.st_nlink == 1 and meta.st_size <= 1024 * 1024:
                raw = plist.read_bytes()
                try:
                    data = plistlib.loads(raw)
                    args = data.get("ProgramArguments")
                except (plistlib.InvalidFileException, ValueError, UnicodeDecodeError, TypeError):
                    args = _parse_deployment_asset(raw, label)
                    data = {"Label": label, "ProgramArguments": args}
                if data.get("Label") == label and isinstance(args, list) and all(isinstance(x, str) for x in args):
                    expected = ["/Users/molly/Desktop/Hermes/venv/bin/python", "-m", "hermes_cli.main", "gateway", "run", "--replace"]
                    if profile == "default" and args != expected:
                        labels["process_observation"] = "disabled"
                        return TargetSpec(target_id=f"hermes:profile:{profile}:gateway", profile=profile, kind=TargetKind.GATEWAY, criticality=criticality, observed_paths=(logs_path, f"~/Library/LaunchAgents/{label}.plist"), labels=labels, existing_writer="launchd+hermes_gateway_watchdog")
                    digest = hashlib.sha256("\x00".join(args).encode()).hexdigest()
                    labels.update(process_marker=label_profile, command_fingerprint="sha256:" + digest, process_observation="enabled")
                    if profile == "default":
                        labels.update(process_command_label_optional="true", process_marker_optional="true", process_name_contains="python3.11")
                else:
                    labels["process_observation"] = "disabled"
            else:
                labels["process_observation"] = "disabled"
        except (OSError, plistlib.InvalidFileException, ValueError, TypeError):
            labels["process_observation"] = "disabled"
        return TargetSpec(
            target_id=f"hermes:profile:{profile}:gateway",
            profile=profile,
            kind=TargetKind.GATEWAY,
            criticality=criticality,
            observed_paths=(logs_path, f"~/Library/LaunchAgents/{label}.plist"),
            labels=labels,
            existing_writer="launchd+hermes_gateway_watchdog",
        )
    specs = tuple(spec_for(*item) for item in _PROFILE_TARGETS)
    return FleetRegistry(specs)


def snapshot_all_targets(registry: FleetRegistry, *, observed_at: datetime, facts: dict[str, object]) -> None:
    """Small deterministic helper used by the first-fleet snapshot workflow."""
    for target in registry.list_targets():
        registry.record_target_snapshot(
            TargetSnapshot(target_id=target.target_id, observed_at=observed_at, facts=facts)
        )
