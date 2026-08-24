"""Deterministic local compatibility evaluation for Wisdom packages."""

from __future__ import annotations

import importlib.metadata
import platform
import shutil
from dataclasses import dataclass, field
from typing import Any, Literal

from packaging.version import InvalidVersion, Version

from hermes_cli import __version__ as HERMES_VERSION

from .contract import SystemSpecification


CompatibilityOutcome = Literal[
    "compatible", "compatible_after_setup", "partial", "blocked_pending_action"
]


@dataclass(frozen=True)
class LocalCapabilities:
    hermes_version: str
    os: str
    architecture: str
    model_capabilities: frozenset[str] = frozenset()
    context_window: int | None = None
    enabled_tools: dict[str, str | None] = field(default_factory=dict)
    plugins: dict[str, str | None] = field(default_factory=dict)
    credentials: frozenset[str] = frozenset()
    connections: frozenset[str] = frozenset()
    runtime: dict[str, bool] = field(default_factory=dict)
    hardware: frozenset[str] = frozenset()
    is_admin: bool = False


@dataclass(frozen=True)
class CompatibilityResult:
    outcome: CompatibilityOutcome
    satisfied: tuple[str, ...]
    setup_actions: tuple[str, ...]
    limitations: tuple[str, ...]
    blocked: tuple[str, ...]


def detect_local_capabilities() -> LocalCapabilities:
    runtime = {
        "shell": shutil.which("sh") is not None or platform.system() == "Windows",
        "browser": False,
        "code": True,
        "sandbox": True,
    }
    return LocalCapabilities(
        hermes_version=HERMES_VERSION,
        os=platform.system().lower(),
        architecture=platform.machine().lower(),
        runtime=runtime,
    )


def _version_at_least(actual: str | None, minimum: str | None) -> bool:
    if minimum is None:
        return actual is not None
    if actual is None:
        return False
    try:
        return Version(actual) >= Version(minimum)
    except InvalidVersion:
        return actual == minimum


def evaluate(
    spec: SystemSpecification, local: LocalCapabilities
) -> CompatibilityResult:
    satisfied: list[str] = []
    setup: list[str] = []
    partial: list[str] = list(spec.known_limitations)
    blocked: list[str] = []
    if _version_at_least(local.hermes_version, spec.hermes.minimum_version):
        satisfied.append(f"Hermes >= {spec.hermes.minimum_version}")
    else:
        blocked.append(f"Hermes >= {spec.hermes.minimum_version}")
    if spec.platforms and local.os not in {item.lower() for item in spec.platforms}:
        blocked.append(f"platform in {', '.join(spec.platforms)}")
    if spec.architectures and local.architecture not in {
        item.lower() for item in spec.architectures
    }:
        blocked.append(f"architecture in {', '.join(spec.architectures)}")
    missing_caps = sorted(set(spec.model.capabilities) - set(local.model_capabilities))
    if missing_caps:
        partial.append("model capabilities: " + ", ".join(missing_caps))
    if spec.model.minimum_context_window and (
        local.context_window is None
        or local.context_window < spec.model.minimum_context_window
    ):
        partial.append(f"model context window >= {spec.model.minimum_context_window}")
    for requirement in spec.tools:
        actual = local.enabled_tools.get(requirement.name)
        label = requirement.name + (
            f">={requirement.minimum_version}" if requirement.minimum_version else ""
        )
        if requirement.requires_admin and not local.is_admin:
            blocked.append(f"administrator approval for tool {label}")
        elif not _version_at_least(actual, requirement.minimum_version):
            setup.append(f"enable tool {label}")
    for requirement in spec.plugins:
        actual = local.plugins.get(requirement.id)
        if not _version_at_least(actual, requirement.minimum_version):
            (setup if requirement.required else partial).append(
                f"install plugin {requirement.id}"
            )
    setup.extend(
        f"configure credential {item}"
        for item in spec.credentials
        if item not in local.credentials
    )
    setup.extend(
        f"connect {item}" for item in spec.connections if item not in local.connections
    )
    for capability, required in spec.runtime.model_dump().items():
        if required and not local.runtime.get(capability, False):
            blocked.append(f"runtime capability {capability}")
    missing_hw = sorted(set(spec.hardware) - set(local.hardware))
    blocked.extend(f"hardware {item}" for item in missing_hw)
    if blocked:
        outcome: CompatibilityOutcome = "blocked_pending_action"
    elif setup:
        outcome = "compatible_after_setup"
    elif partial:
        outcome = "partial"
    else:
        outcome = "compatible"
    return CompatibilityResult(
        outcome, tuple(satisfied), tuple(setup), tuple(partial), tuple(blocked)
    )
