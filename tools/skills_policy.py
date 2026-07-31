"""Skill content ownership and profile-scoped runtime-state helpers."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import yaml

from hermes_constants import get_config_path, get_skills_dir, get_skills_state_dir


SKILLS_CONTENT_READ_ONLY = "SKILLS_CONTENT_READ_ONLY"
SKILLS_CONFIG_UNAVAILABLE = "SKILLS_CONFIG_UNAVAILABLE"
# Keep legacy discovery-root metadata readers for the first two upstream
# releases containing this module. Remove them only after both releases have
# shipped and the review-bound migration follow-up has been completed.
LEGACY_SKILLS_STATE_COMPAT_RELEASES = 2

_VALID_CONTENT_MODES = {"writable", "read_only"}
_warned_legacy_sources: set[str] = set()
logger = logging.getLogger("hermes.skills_state")


class SkillsContentPolicyError(RuntimeError):
    """Base class for stable skill-content policy refusals."""

    reason_code: str

    def __init__(self, reason_code: str, detail: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"{reason_code}: {detail}")


def _read_raw_policy_config(config_path: Path) -> dict:
    """Read only the user-authored YAML without importing the CLI layer."""
    try:
        with config_path.open(encoding="utf-8") as stream:
            data = yaml.safe_load(stream) or {}
    except FileNotFoundError:
        return {}
    return data if isinstance(data, dict) else {}


def _strict_user_config_validation() -> Optional[SkillsContentPolicyError]:
    """Validate an existing user config before trusting a mutation policy."""
    config_path = get_config_path()
    try:
        config_path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return SkillsContentPolicyError(
            SKILLS_CONFIG_UNAVAILABLE,
            f"cannot access skills content policy in {config_path}: {exc}",
        )

    try:
        _read_raw_policy_config(config_path)
    except Exception as exc:
        return SkillsContentPolicyError(
            SKILLS_CONFIG_UNAVAILABLE,
            f"cannot read skills content policy in {config_path}: {exc}",
        )
    return None


def skills_content_mode() -> str:
    """Return the configured content mode, failing closed on invalid policy."""
    validation_error = _strict_user_config_validation()
    if validation_error is not None:
        raise validation_error

    try:
        # Do not use the CLI config layer here: a low-level policy gate must be
        # usable by gateway, packaged-agent, and headless tool runtimes without
        # importing CLI-only dependencies or initializing the profile.
        config = _read_raw_policy_config(get_config_path())
    except Exception as exc:
        raise SkillsContentPolicyError(
            SKILLS_CONFIG_UNAVAILABLE,
            f"cannot load skills content policy: {exc}",
        ) from exc

    skills = config.get("skills", {}) if isinstance(config, dict) else {}
    if not isinstance(skills, dict):
        raise SkillsContentPolicyError(
            SKILLS_CONFIG_UNAVAILABLE,
            "skills must be a mapping when skills.content_mode is configured",
        )
    mode = skills.get("content_mode", "writable")
    if not isinstance(mode, str) or mode not in _VALID_CONTENT_MODES:
        raise SkillsContentPolicyError(
            SKILLS_CONFIG_UNAVAILABLE,
            f"skills.content_mode must be one of {sorted(_VALID_CONTENT_MODES)}",
        )
    return mode


def require_skills_content_writable(operation: str) -> None:
    """Refuse *operation* before its first skill-content mutation."""
    if skills_content_mode() == "read_only":
        raise SkillsContentPolicyError(
            SKILLS_CONTENT_READ_ONLY,
            f"{operation} is disabled because skills.content_mode is read_only",
        )


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _targets_skills_content(path: str | Path) -> bool:
    """Return whether *path* names content below the active discovery root.

    Check both the lexical and resolved forms. The lexical check refuses a
    path entered through the discovery root even when an internal symlink
    points out of it; the resolved check catches aliases outside the discovery
    tree that point back into managed content.
    """
    candidate = Path(path).expanduser()
    root = get_skills_dir().expanduser()
    lexical_candidate = Path(
        os.path.normpath(os.path.abspath(os.fspath(candidate)))
    )
    lexical_root = Path(os.path.normpath(os.path.abspath(os.fspath(root))))
    if _is_within(lexical_candidate, lexical_root):
        return True

    try:
        resolved_candidate = candidate.resolve(strict=False)
        resolved_root = root.resolve(strict=False)
    except OSError:
        return False
    return _is_within(resolved_candidate, resolved_root)


def require_skills_path_writable(path: str | Path, operation: str) -> None:
    """Refuse a generic mutation that targets the discovery/content root.

    This policy is intentionally narrower than ``HERMES_HOME``: relocated
    runtime metadata under ``HERMES_HOME/state/skills`` must remain writable
    in read-only content mode.
    """
    if not os.path.isabs(os.fspath(path)):
        raise ValueError("skills content policy requires an absolute path")
    if _targets_skills_content(path):
        require_skills_content_writable(operation)


def skills_state_path(*parts: str) -> Path:
    """Return a path below the profile-aware writable skills state root."""
    return get_skills_state_dir().joinpath(*parts)


def legacy_skills_path(*parts: str) -> Path:
    """Return a legacy metadata path below the discovery root."""
    return get_skills_dir().joinpath(*parts)


def state_read_path(
    state_parts: tuple[str, ...],
    legacy_parts: tuple[str, ...],
) -> Path:
    """Prefer external state, then legacy metadata, without creating either."""
    state_path = skills_state_path(*state_parts)
    if state_path.exists():
        return state_path
    return legacy_skills_path(*legacy_parts)


def prepare_legacy_state_write(
    state_path: Path,
    *legacy_paths: Path,
) -> Optional[Path]:
    """Return the legacy source that a successful first state write migrates.

    Callers capture this immediately before an atomic write and pass the
    returned path to :func:`note_legacy_state_write` only after the write
    succeeds. The legacy source remains untouched for the compatibility
    window.
    """
    if state_path.exists():
        return None
    for legacy_path in legacy_paths:
        if legacy_path.exists():
            return legacy_path
    return None


def note_legacy_state_write(legacy_source: Optional[Path]) -> None:
    """Emit the once-per-source warning after a migration write succeeds."""
    if legacy_source is not None:
        warn_legacy_state_migrated(legacy_source)


def warn_legacy_state_migrated(path: Path) -> None:
    """Warn once per canonical legacy source in this process."""
    try:
        key = str(path.resolve())
    except OSError:
        key = str(path.absolute())
    if key in _warned_legacy_sources:
        return
    _warned_legacy_sources.add(key)
    logger.warning(
        "Migrated legacy skills runtime metadata from %s; compatibility "
        "fallback remains for %d releases",
        path,
        LEGACY_SKILLS_STATE_COMPAT_RELEASES,
    )
