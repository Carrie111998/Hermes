"""Helpers for narrowing Hermes to the VideoAgent/Ultra Studio skill catalog."""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from typing import Any


VIDEO_AGENT_CORE_SKILL_ALLOWLIST: tuple[str, ...] = (
    "workflow-router",
    "media-qa",
    "prompt-repair",
)

VIDEO_AGENT_WORKFLOW_SKILL_ALLOWLIST: tuple[str, ...] = (
    "infographic-md-flow",
)

VIDEO_AGENT_MARKETING_SKILL_ALLOWLIST: tuple[str, ...] = (
    "gpt-image-2-director",
    "marketing-studio-director",
    "higgsfield-content-factory",
)

DEFAULT_VIDEO_AGENT_SKILL_ALLOWLIST: tuple[str, ...] = (
    *VIDEO_AGENT_CORE_SKILL_ALLOWLIST,
    *VIDEO_AGENT_WORKFLOW_SKILL_ALLOWLIST,
    *VIDEO_AGENT_MARKETING_SKILL_ALLOWLIST,
)

# Backward-compatible name used by existing Ultra Studio profile helpers/tests.
DEFAULT_ULTRA_STUDIO_SKILL_ALLOWLIST = DEFAULT_VIDEO_AGENT_SKILL_ALLOWLIST


def _coerce_skill_name(skill: str | Mapping[str, Any]) -> str:
    if isinstance(skill, str):
        name = skill
    elif isinstance(skill, Mapping):
        raw_name = skill.get("name")
        if not isinstance(raw_name, str):
            raise ValueError(f"Skill row is missing a string name: {skill!r}")
        name = raw_name
    else:
        raise TypeError(f"Unsupported skill row type: {type(skill).__name__}")

    name = name.strip()
    if not name:
        raise ValueError(f"Skill name cannot be empty: {skill!r}")
    return name


def collect_skill_names(skills: Iterable[str | Mapping[str, Any]]) -> list[str]:
    """Return sorted unique skill names from discovery rows or plain names."""

    return sorted({_coerce_skill_name(skill) for skill in skills})


def compute_disabled_skills(
    installed_skills: Iterable[str | Mapping[str, Any]],
    allowlist: Iterable[str] = DEFAULT_ULTRA_STUDIO_SKILL_ALLOWLIST,
) -> list[str]:
    """Return skill names that should be disabled for the Ultra Studio profile."""

    allowed = {name.strip() for name in allowlist if name and name.strip()}
    return [name for name in collect_skill_names(installed_skills) if name not in allowed]


def build_disabled_skills_config(
    installed_skills: Iterable[str | Mapping[str, Any]],
    *,
    allowlist: Iterable[str] = DEFAULT_ULTRA_STUDIO_SKILL_ALLOWLIST,
    platform: str | None = None,
) -> dict[str, Any]:
    """Build the config fragment that hides all non-Ultra-Studio skills."""

    disabled = compute_disabled_skills(installed_skills, allowlist)
    if platform is None:
        return {"skills": {"disabled": disabled}}
    return {"skills": {"platform_disabled": {platform: disabled}}}


def apply_ultra_studio_allowlist(
    config: Mapping[str, Any],
    installed_skills: Iterable[str | Mapping[str, Any]],
    *,
    allowlist: Iterable[str] = DEFAULT_ULTRA_STUDIO_SKILL_ALLOWLIST,
    platform: str | None = None,
) -> dict[str, Any]:
    """Return a copy of ``config`` with non-video skills disabled.

    This helper is intentionally side-effect free. Callers that want to persist
    the result should pass it to the existing config save path.
    """

    updated = copy.deepcopy(dict(config))
    skills_cfg = updated.setdefault("skills", {})
    disabled = compute_disabled_skills(installed_skills, allowlist)
    if platform is None:
        skills_cfg["disabled"] = disabled
    else:
        skills_cfg.setdefault("platform_disabled", {})
        skills_cfg["platform_disabled"][platform] = disabled
    return updated
