"""Fail-closed controls for Mantis security-analysis workers.

Mantis analysis is descriptive by default. Reproducer and patch stages are
executable security work and therefore require an explicitly verified isolated
runtime with negative assertions for production credentials and internal
network access.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


MANTIS_CAPABILITIES = (
    "architecture",
    "threat_model",
    "research",
    "review",
    "reproduce",
    "patch",
)
MANTIS_FINDING_STATES = (
    "candidate",
    "reviewed",
    "reproduced",
    "validated",
    "accepted",
    "fixed",
    "verified",
    "dismissed",
)
_EXECUTABLE_CAPABILITIES = frozenset({"reproduce", "patch"})
_PLANE_PROJECTABLE_STATES = frozenset({"reviewed", "validated"})
_TRANSITIONS = {
    "candidate": frozenset({"reviewed", "dismissed"}),
    "reviewed": frozenset({"reproduced", "dismissed"}),
    "reproduced": frozenset({"validated", "dismissed"}),
    "validated": frozenset({"accepted", "dismissed"}),
    "accepted": frozenset({"fixed", "dismissed"}),
    "fixed": frozenset({"verified"}),
    "verified": frozenset(),
    "dismissed": frozenset(),
}


class FindingTransitionError(ValueError):
    """Raised when a finding attempts to skip a required review stage."""


@dataclass(frozen=True)
class MantisHealth:
    capability: str
    ready: bool
    blockers: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "ready": self.ready,
            "blockers": list(self.blockers),
        }


def _configured_capabilities(config: Mapping[str, Any]) -> frozenset[str]:
    raw = config.get("capabilities", ())
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(str(value).strip() for value in raw if str(value).strip())


def mantis_health(config: Mapping[str, Any] | None, capability: str) -> MantisHealth:
    """Return fail-closed readiness for one Mantis capability."""
    if capability not in MANTIS_CAPABILITIES:
        raise ValueError(f"unknown Mantis capability: {capability}")
    data: Mapping[str, Any] = config if isinstance(config, Mapping) else {}
    blockers: list[str] = []
    if data.get("enabled") is not True:
        blockers.append("mantis must be enabled")
    if capability not in _configured_capabilities(data):
        blockers.append(f"capability {capability!r} is not configured")
    if capability in _EXECUTABLE_CAPABILITIES:
        if data.get("isolated_runtime") is not True:
            blockers.append("isolated_runtime must be verified")
        if data.get("production_credentials") is not False:
            blockers.append("production_credentials must be false")
        if data.get("internal_network") is not False:
            blockers.append("internal_network must be false")
    return MantisHealth(capability, not blockers, tuple(blockers))


def advance_finding(current: str, target: str) -> str:
    """Validate a finding lifecycle transition without mutating storage."""
    if current not in MANTIS_FINDING_STATES:
        raise FindingTransitionError(f"unknown finding state: {current}")
    if target not in MANTIS_FINDING_STATES:
        raise FindingTransitionError(f"unknown finding state: {target}")
    if target not in _TRANSITIONS[current]:
        raise FindingTransitionError(
            f"invalid Mantis finding transition: {current} -> {target}"
        )
    return target


def plane_security_projection(
    findings: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a non-executing Plane projection of reviewed evidence only."""
    projected = [
        dict(finding)
        for finding in findings
        if finding.get("state") in _PLANE_PROJECTABLE_STATES
    ]
    return {
        "source": "mantis",
        "projection_only": True,
        "findings": projected,
    }


def read_profile_mantis_config(profile: str) -> Mapping[str, Any]:
    """Read only the named profile's non-secret Mantis config."""
    from hermes_cli.config import read_user_config_raw
    from hermes_cli.profiles import get_profile_dir

    config_path = get_profile_dir(profile) / "config.yaml"
    if not config_path.is_file():
        return {}
    try:
        raw = read_user_config_raw(config_path)
    except Exception:
        return {}
    section = raw.get("mantis", {}) if isinstance(raw, Mapping) else {}
    return section if isinstance(section, Mapping) else {}


def task_mantis_health(capability: str | None, profile: str | None) -> MantisHealth | None:
    """Return task readiness, or ``None`` for a non-Mantis task."""
    if capability is None:
        return None
    if capability not in MANTIS_CAPABILITIES:
        raise ValueError(f"unknown Mantis capability: {capability}")
    if not profile:
        return MantisHealth(capability, False, ("Mantis task requires an assignee",))
    return mantis_health(read_profile_mantis_config(profile), capability)
