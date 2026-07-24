"""Strict validation for the credential-free ``fleet:`` config subtree."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from .types import Confidence
from .usage_paths import resolve_usage_path


LANE_ORDER = (
    "chatgpt_codex",
    "claude_code",
    "grok",
    "antigravity",
    "kimi",
)
DEFERRED_LANES = frozenset({"kimi"})

DEFAULT_FLEET_CONFIG: dict[str, Any] = {
    "enabled": False,
    "parent_desktop_enabled": False,
    # Empty → resolve to {hermes_home}/fleet/usage-weekly.json at parse time.
    "bridge_usage_file": "",
    "switch_delta_pct": 20.0,
    "minimum_confidence": "high",
    "rotation_without_fresh_capacity": False,
    "lease_ttl_seconds": 1800,
    "execution_timeout_seconds": 1800,
    "default_reservation_pct": 5.0,
    "lanes": {
        "chatgpt_codex": {
            "enabled": True,
            "max_concurrency": 1,
            "reserve_floor_pct": 10.0,
        },
        "claude_code": {
            "enabled": False,
            "max_concurrency": 1,
            "reserve_floor_pct": 10.0,
        },
        "grok": {
            "enabled": False,
            "max_concurrency": 1,
            "reserve_floor_pct": 10.0,
        },
        "antigravity": {
            "enabled": False,
            "max_concurrency": 1,
            "reserve_floor_pct": 10.0,
        },
        "kimi": {
            "enabled": False,
            "max_concurrency": 1,
            "reserve_floor_pct": 10.0,
        },
    },
}

_ROOT_KEYS = frozenset(DEFAULT_FLEET_CONFIG)
_LANE_KEYS = frozenset({"enabled", "max_concurrency", "reserve_floor_pct"})
_SENSITIVE_FRAGMENTS = (
    "api_key",
    "token",
    "secret",
    "password",
    "credential",
    "auth_",
)


class FleetConfigError(ValueError):
    pass


@dataclass(frozen=True)
class LaneConfig:
    enabled: bool
    max_concurrency: int
    reserve_floor_pct: Decimal


@dataclass(frozen=True)
class FleetConfig:
    enabled: bool
    parent_desktop_enabled: bool
    bridge_usage_file: Path
    switch_delta_pct: Decimal
    minimum_confidence: Confidence
    rotation_without_fresh_capacity: bool
    lease_ttl_seconds: int
    execution_timeout_seconds: int
    default_reservation_pct: Decimal
    lanes: Mapping[str, LaneConfig]


def _find_sensitive(value: object, prefix: str = "fleet") -> str | None:
    if not isinstance(value, Mapping):
        return None
    for raw_key, child in value.items():
        key = str(raw_key).lower()
        path = f"{prefix}.{raw_key}"
        if any(fragment in key for fragment in _SENSITIVE_FRAGMENTS):
            return path
        nested = _find_sensitive(child, path)
        if nested:
            return nested
    return None


def _bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise FleetConfigError(f"{field} must be true or false")
    return value


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FleetConfigError(f"{field} must be a positive integer")
    return value


def _percentage(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        raise FleetConfigError(f"{field} must be a percentage")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise FleetConfigError(f"{field} must be a percentage") from exc
    if not parsed.is_finite() or parsed < 0 or parsed > 100:
        raise FleetConfigError(f"{field} must be between 0 and 100")
    return parsed.quantize(Decimal("0.001"))


def parse_fleet_config(config: Mapping[str, Any] | None) -> FleetConfig:
    """Parse a full Hermes config mapping without reading credentials or env."""

    root = dict((config or {}).get("fleet") or {})
    sensitive = _find_sensitive(root)
    if sensitive:
        raise FleetConfigError(
            f"credential-like field is forbidden in fleet config: {sensitive}"
        )
    unknown = set(root) - _ROOT_KEYS
    if unknown:
        raise FleetConfigError(f"unknown fleet option: {sorted(unknown)[0]}")

    enabled = _bool(root.get("enabled", False), "fleet.enabled")
    parent_desktop_enabled = _bool(
        root.get("parent_desktop_enabled", False),
        "fleet.parent_desktop_enabled",
    )
    bridge = root.get(
        "bridge_usage_file", DEFAULT_FLEET_CONFIG["bridge_usage_file"]
    )
    if bridge is None:
        bridge = ""
    if not isinstance(bridge, str):
        raise FleetConfigError("bridge_usage_file must be a path string")
    # Blank means the profile-safe native default under Hermes home.
    resolved_bridge = resolve_usage_path(bridge.strip() or None)
    switch = _percentage(
        root.get("switch_delta_pct", 20.0), "switch_delta_pct"
    )
    if switch != Decimal("20.000"):
        raise FleetConfigError("switch_delta_pct must equal exactly 20.0 in v1")
    minimum = root.get("minimum_confidence", "high")
    if minimum != Confidence.HIGH.value:
        raise FleetConfigError("minimum_confidence must be high in v1")
    rotation_without_fresh_capacity = _bool(
        root.get("rotation_without_fresh_capacity", False),
        "rotation_without_fresh_capacity",
    )
    ttl = _positive_int(
        root.get("lease_ttl_seconds", 1800), "lease_ttl_seconds"
    )
    timeout = _positive_int(
        root.get("execution_timeout_seconds", 1800),
        "execution_timeout_seconds",
    )
    reservation = _percentage(
        root.get("default_reservation_pct", 5.0),
        "default_reservation_pct",
    )
    raw_lanes = root.get("lanes") or {}
    if not isinstance(raw_lanes, Mapping):
        raise FleetConfigError("fleet.lanes must be a mapping")
    unknown_lanes = set(raw_lanes) - set(LANE_ORDER)
    if unknown_lanes:
        raise FleetConfigError(f"unknown lane: {sorted(unknown_lanes)[0]}")

    lanes: dict[str, LaneConfig] = {}
    for lane_id in LANE_ORDER:
        defaults = DEFAULT_FLEET_CONFIG["lanes"][lane_id]
        supplied = raw_lanes.get(lane_id) or {}
        if not isinstance(supplied, Mapping):
            raise FleetConfigError(f"lane {lane_id} must be a mapping")
        unknown_options = set(supplied) - _LANE_KEYS
        if unknown_options:
            raise FleetConfigError(
                f"unknown lane option for {lane_id}: {sorted(unknown_options)[0]}"
            )
        lane_enabled = _bool(
            supplied.get("enabled", defaults["enabled"]),
            f"{lane_id}.enabled",
        )
        if lane_id in DEFERRED_LANES and lane_enabled:
            raise FleetConfigError(f"{lane_id} is deferred and cannot be enabled")
        lanes[lane_id] = LaneConfig(
            enabled=lane_enabled,
            max_concurrency=_positive_int(
                supplied.get(
                    "max_concurrency", defaults.get("max_concurrency", 1)
                ),
                f"{lane_id}.max_concurrency",
            ),
            reserve_floor_pct=_percentage(
                supplied.get(
                    "reserve_floor_pct", defaults.get("reserve_floor_pct", 10.0)
                ),
                f"{lane_id}.reserve_floor_pct",
            ),
        )

    return FleetConfig(
        enabled=enabled,
        parent_desktop_enabled=parent_desktop_enabled,
        bridge_usage_file=resolved_bridge,
        switch_delta_pct=switch,
        minimum_confidence=Confidence.HIGH,
        # Accepted for one compatibility release, but intentionally ignored.
        # Missing/stale usage now always falls back to deterministic rotation.
        rotation_without_fresh_capacity=False,
        lease_ttl_seconds=ttl,
        execution_timeout_seconds=timeout,
        default_reservation_pct=reservation,
        lanes=lanes,
    )
