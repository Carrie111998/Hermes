#!/usr/bin/env python3
"""
Skills Tool Module

This module provides tools for listing and viewing skill documents.
Skills are organized as directories containing a SKILL.md file (the main instructions)
and optional supporting files like references, templates, and examples.

Inspired by Anthropic's Claude Skills system with progressive disclosure architecture:
- Metadata (name ≤64 chars, description ≤1024 chars) - shown in skills_list
- Full Instructions - loaded via skill_view when needed
- Linked Files (references, templates) - loaded on demand

Directory Structure:
    skills/
    ├── my-skill/
    │   ├── SKILL.md           # Main instructions (required)
    │   ├── references/        # Supporting documentation
    │   │   ├── api.md
    │   │   └── examples.md
    │   ├── templates/         # Templates for output
    │   │   └── template.md
    │   └── assets/            # Supplementary files (agentskills.io standard)
    └── category/              # Category folder for organization
        └── another-skill/
            └── SKILL.md

SKILL.md Format (YAML Frontmatter, agentskills.io compatible):
    ---
    name: skill-name              # Required, max 64 chars
    description: Brief description # Required, max 1024 chars
    version: 1.0.0                # Optional
    license: MIT                  # Optional (agentskills.io)
    platforms: [macos]            # Optional — restrict to specific OS platforms
                                  #   Valid: macos, linux, windows
                                  #   Omit to load on all platforms (default)
    prerequisites:                # Optional — legacy runtime requirements
      env_vars: [API_KEY]         #   Legacy env var names are normalized into
                                  #   required_environment_variables on load.
      commands: [curl, jq]        #   Command checks remain advisory only.
    compatibility: Requires X     # Optional (agentskills.io)
    metadata:                     # Optional, arbitrary key-value (agentskills.io)
      hermes:
        tags: [fine-tuning, llm]
        related_skills: [peft, lora]
    ---

    # Skill Title

    Full instructions and content here...

Available tools:
- skills_list: List skills with metadata (progressive disclosure tier 1)
- skill_view: Load full skill content (progressive disclosure tier 2-3)

Usage:
    from tools.skills_tool import skills_list, skill_view, check_skills_requirements

    # List all skills (returns metadata only - token efficient)
    result = skills_list()

    # View a skill's main content (loads full instructions)
    content = skill_view("axolotl")

    # View a reference file within a skill (loads linked file)
    content = skill_view("axolotl", "references/dataset-formats.md")
"""

import hashlib
import json
import logging
import time
import threading

from hermes_constants import get_hermes_home, display_hermes_home
import os
import re
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Dict, Any, List, Optional, Set, Tuple

from tools.registry import registry, tool_error
from tools.oversized_result_formatters import (
    register_formatter as register_oversized_result_formatter,
)
from hermes_cli.config import cfg_get
from utils import env_var_enabled
from agent.skill_utils import (
    EXCLUDED_SKILL_DIRS as _EXCLUDED_SKILL_DIRS,
    is_skill_support_path as _is_skill_support_path,
)

logger = logging.getLogger(__name__)

# Per-session skill discovery cache.  _find_all_skills() re-reads every
# SKILL.md on every call; with hundreds of skills this is wasteful.
# Cache validation (mirrors hermes_cli/profiles.py::_count_skills, d5eee133e):
#   - signature = per-dir max mtime of the dir AND its immediate children
#     (one scandir per dir; catches skill add/remove inside categories,
#     which does NOT bump the root dir's mtime), plus the disabled-set
#     (config-driven — changes with no filesystem mtime bump at all)
#   - a short TTL bounds staleness from in-place SKILL.md edits, which
#     bump only the file's mtime, invisible to any directory signature.
# skip_disabled True/False are cached separately.
_SKILLS_CACHE: dict = {}          # {cache_key: (signature, timestamp, skills_list)}
_SKILLS_CACHE_TTL_SECONDS = 30.0
_SKILLS_CACHE_KEY_DISABLED = "with_disabled"
_SKILLS_CACHE_KEY_FILTERED = "filtered"


def _skills_scan_signature(dirs_to_scan, disabled) -> tuple:
    """Cheap change-signature for the skill scan inputs.

    O(#dirs + #categories) stat calls, not a recursive walk. Includes the
    platform the scan's ``skill_matches_platform`` filter will use (read
    from ``agent.skill_utils``'s ``sys`` so test patches of that module
    are honored) — the scan result is platform-dependent.
    """
    from agent import skill_utils as _skill_utils

    platform = getattr(getattr(_skill_utils, "sys", None), "platform", "")
    sig = []
    for d in dirs_to_scan:
        try:
            m = d.stat().st_mtime
        except OSError:
            continue
        try:
            with os.scandir(d) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            em = entry.stat(follow_symlinks=False).st_mtime
                            if em > m:
                                m = em
                    except OSError:
                        continue
        except OSError:
            pass
        sig.append((str(d), m))
    return (tuple(sig), frozenset(disabled), platform)


# All skills live in ~/.hermes/skills/ (seeded from bundled skills/ on install).
# This is the single source of truth -- agent edits, hub installs, and bundled
# skills all coexist here without polluting the git repo.
HERMES_HOME = get_hermes_home()
SKILLS_DIR = HERMES_HOME / "skills"
_SKILLS_DIR_AT_IMPORT = SKILLS_DIR


def _skills_dir() -> Path:
    """Return the active profile's skills directory at call time.

    Some long-lived runtimes import this module before the active profile has
    set HERMES_HOME. Keep the legacy SKILLS_DIR module attribute for tests and
    external patchers, but when it has not been patched, resolve from the live
    profile-scoped HERMES_HOME on every call.
    """
    configured = Path(SKILLS_DIR)
    if configured != _SKILLS_DIR_AT_IMPORT:
        return configured
    return get_hermes_home() / "skills"


# Anthropic-recommended limits for progressive disclosure efficiency
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024

# Platform identifiers for the 'platforms' frontmatter field.
# Maps user-friendly names to sys.platform prefixes.
_PLATFORM_MAP = {
    "macos": "darwin",
    "linux": "linux",
    "windows": "win32",
}
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REMOTE_ENV_BACKENDS = frozenset(
    {"docker", "singularity", "modal", "ssh", "daytona", "vercel_sandbox"}
)


def _is_remote_env_backend(backend: str) -> bool:
    """Built-in remote backends plus plugin backends declaring is_remote."""
    if backend in _REMOTE_ENV_BACKENDS:
        return True
    if not backend or backend == "local":
        return False
    try:
        from agent.terminal_env_registry import provider_flag

        return bool(provider_flag(backend, "is_remote", False))
    except Exception:
        return False
_secret_capture_callback = None


def _skill_lookup_path_error(name: str) -> Optional[str]:
    """Return an error if a local skill lookup *name* can escape search roots.

    The skill ``name`` is joined onto each trusted search dir to build the
    on-disk lookup path, so it must stay relative and free of ``..`` segments —
    otherwise ``name="../outside"`` or an absolute path could select a skill
    (and read files) outside the skills directory. Mirrors the ``file_path``
    validation done later via ``tools.path_security``. We also reject Windows
    drive paths (e.g. ``C:\\skills``), whose ``:`` would otherwise be misread as
    a plugin namespace separator.
    """
    from tools.path_security import has_traversal_component

    if not isinstance(name, str):
        return "Skill name must be a string."
    candidate = name.strip()
    if (
        PurePosixPath(candidate).is_absolute()
        or PureWindowsPath(candidate).is_absolute()
        or PureWindowsPath(candidate).drive
    ):
        return "Skill name must be a relative path within the skills directory."
    if has_traversal_component(candidate):
        return "Skill name cannot contain '..' path traversal components."
    return None


def load_env() -> Dict[str, str]:
    """Load profile-scoped environment variables from HERMES_HOME/.env."""
    env_path = get_hermes_home() / ".env"
    env_vars: Dict[str, str] = {}
    if not env_path.exists():
        return env_vars

    # utf-8-sig: users hand-edit .env in Notepad, which prepends a BOM that
    # would otherwise glue U+FEFF onto the first key name (same dialect as
    # the canonical readers in hermes_cli/config.py).
    with env_path.open(encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                if line.startswith("export "):
                    line = line[7:]
                key, _, value = line.partition("=")
                env_vars[key.strip()] = value.strip().strip("\"'")
    return env_vars


class SkillReadinessStatus(str, Enum):
    AVAILABLE = "available"
    SETUP_NEEDED = "setup_needed"
    UNSUPPORTED = "unsupported"


# Prompt injection detection — shared by local-skill and plugin-skill paths.
_INJECTION_PATTERNS: list = [
    "ignore previous instructions",
    "ignore all previous",
    "you are now",
    "disregard your",
    "forget your instructions",
    "new instructions:",
    "system prompt:",
    "<system>",
    "]]>",
]


def set_secret_capture_callback(callback) -> None:
    global _secret_capture_callback
    _secret_capture_callback = callback


def skill_matches_platform(frontmatter: Dict[str, Any]) -> bool:
    """Check if a skill is compatible with the current OS platform.

    Delegates to ``agent.skill_utils.skill_matches_platform`` — kept here
    as a public re-export so existing callers don't need updating.
    """
    from agent.skill_utils import skill_matches_platform as _impl
    return _impl(frontmatter)


def skill_matches_environment(frontmatter: Dict[str, Any]) -> bool:
    """Check if a skill is relevant to the current runtime environment.

    Delegates to ``agent.skill_utils.skill_matches_environment`` — kept here
    as a public re-export so existing callers don't need updating. This is an
    offer-time relevance gate (kanban/docker/s6), NOT a hard-compatibility gate;
    explicit skill loads bypass it.
    """
    from agent.skill_utils import skill_matches_environment as _impl
    return _impl(frontmatter)


def _normalize_prerequisite_values(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    return [str(item) for item in value if str(item).strip()]


def _collect_prerequisite_values(
    frontmatter: Dict[str, Any],
) -> Tuple[List[str], List[str]]:
    prereqs = frontmatter.get("prerequisites")
    if not prereqs or not isinstance(prereqs, dict):
        return [], []
    return (
        _normalize_prerequisite_values(prereqs.get("env_vars")),
        _normalize_prerequisite_values(prereqs.get("commands")),
    )


def _normalize_setup_metadata(frontmatter: Dict[str, Any]) -> Dict[str, Any]:
    setup = frontmatter.get("setup")
    if not isinstance(setup, dict):
        return {"help": None, "collect_secrets": []}

    help_text = setup.get("help")
    normalized_help = (
        str(help_text).strip()
        if isinstance(help_text, str) and help_text.strip()
        else None
    )

    collect_secrets_raw = setup.get("collect_secrets")
    if isinstance(collect_secrets_raw, dict):
        collect_secrets_raw = [collect_secrets_raw]
    if not isinstance(collect_secrets_raw, list):
        collect_secrets_raw = []

    collect_secrets: List[Dict[str, Any]] = []
    for item in collect_secrets_raw:
        if not isinstance(item, dict):
            continue

        env_var = str(item.get("env_var") or "").strip()
        if not env_var:
            continue

        prompt = str(item.get("prompt") or f"Enter value for {env_var}").strip()
        provider_url = str(item.get("provider_url") or item.get("url") or "").strip()

        entry: Dict[str, Any] = {
            "env_var": env_var,
            "prompt": prompt,
            "secret": bool(item.get("secret", True)),
        }
        if provider_url:
            entry["provider_url"] = provider_url
        collect_secrets.append(entry)

    return {
        "help": normalized_help,
        "collect_secrets": collect_secrets,
    }


def _get_required_environment_variables(
    frontmatter: Dict[str, Any],
    legacy_env_vars: List[str] | None = None,
) -> List[Dict[str, Any]]:
    setup = _normalize_setup_metadata(frontmatter)
    required_raw = frontmatter.get("required_environment_variables")
    if isinstance(required_raw, dict):
        required_raw = [required_raw]
    if not isinstance(required_raw, list):
        required_raw = []

    required: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def _append_required(entry: Dict[str, Any]) -> None:
        env_name = str(entry.get("name") or entry.get("env_var") or "").strip()
        if not env_name or env_name in seen:
            return
        if not _ENV_VAR_NAME_RE.match(env_name):
            return

        normalized: Dict[str, Any] = {
            "name": env_name,
            "prompt": str(entry.get("prompt") or f"Enter value for {env_name}").strip(),
        }

        help_text = (
            entry.get("help")
            or entry.get("provider_url")
            or entry.get("url")
            or setup.get("help")
        )
        if isinstance(help_text, str) and help_text.strip():
            normalized["help"] = help_text.strip()

        required_for = entry.get("required_for")
        if isinstance(required_for, str) and required_for.strip():
            normalized["required_for"] = required_for.strip()

        if entry.get("optional"):
            normalized["optional"] = True

        seen.add(env_name)
        required.append(normalized)

    for item in required_raw:
        if isinstance(item, str):
            _append_required({"name": item})
            continue
        if isinstance(item, dict):
            _append_required(item)

    for item in setup["collect_secrets"]:
        _append_required(
            {
                "name": item.get("env_var"),
                "prompt": item.get("prompt"),
                "help": item.get("provider_url") or setup.get("help"),
            }
        )

    if legacy_env_vars is None:
        legacy_env_vars, _ = _collect_prerequisite_values(frontmatter)
    for env_var in legacy_env_vars:
        _append_required({"name": env_var})

    return required


def _capture_required_environment_variables(
    skill_name: str,
    missing_entries: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if not missing_entries:
        return {
            "missing_names": [],
            "setup_skipped": False,
            "gateway_setup_hint": None,
        }

    missing_names = [entry["name"] for entry in missing_entries]
    # Most gateway surfaces (messaging platforms) can't prompt for a secret, so
    # they short-circuit to the "unsupported" hint. Interactive gateway surfaces
    # — the desktop app / TUI — set HERMES_INTERACTIVE and register a
    # secret-capture callback that routes to a secure secret.request overlay, so
    # they fall through and actually prompt. (HERMES_INTERACTIVE is the same flag
    # tools/approval.py uses to tell an interactive surface from a messaging one.)
    if _is_gateway_surface() and not env_var_enabled("HERMES_INTERACTIVE"):
        return {
            "missing_names": missing_names,
            "setup_skipped": False,
            "gateway_setup_hint": _gateway_setup_hint(),
        }

    if _secret_capture_callback is None:
        return {
            "missing_names": missing_names,
            "setup_skipped": False,
            "gateway_setup_hint": None,
        }

    setup_skipped = False
    remaining_names: List[str] = []

    for entry in missing_entries:
        metadata = {"skill_name": skill_name}
        if entry.get("help"):
            metadata["help"] = entry["help"]
        if entry.get("required_for"):
            metadata["required_for"] = entry["required_for"]

        try:
            callback_result = _secret_capture_callback(
                entry["name"],
                entry["prompt"],
                metadata,
            )
        except Exception:
            logger.warning(
                f"Secret capture callback failed for {entry['name']}", exc_info=True
            )
            callback_result = {
                "success": False,
                "stored_as": entry["name"],
                "validated": False,
                "skipped": True,
            }

        success = isinstance(callback_result, dict) and bool(
            callback_result.get("success")
        )
        skipped = isinstance(callback_result, dict) and bool(
            callback_result.get("skipped")
        )
        if success and not skipped:
            continue

        setup_skipped = True
        remaining_names.append(entry["name"])

    return {
        "missing_names": remaining_names,
        "setup_skipped": setup_skipped,
        "gateway_setup_hint": None,
    }


def _is_gateway_surface() -> bool:
    if env_var_enabled("HERMES_GATEWAY_SESSION"):
        return True
    from gateway.session_context import get_session_env
    return bool(get_session_env("HERMES_SESSION_PLATFORM"))


def _get_terminal_backend_name() -> str:
    return str(os.getenv("TERMINAL_ENV", "local")).strip().lower() or "local"


def _is_env_var_persisted(
    var_name: str, env_snapshot: Dict[str, str] | None = None
) -> bool:
    if env_snapshot is None:
        env_snapshot = load_env()
    if var_name in env_snapshot:
        return bool(env_snapshot.get(var_name))
    return bool(os.getenv(var_name))


def _remaining_required_environment_names(
    required_env_vars: List[Dict[str, Any]],
    capture_result: Dict[str, Any],
    *,
    env_snapshot: Dict[str, str] | None = None,
) -> List[str]:
    missing_names = set(capture_result["missing_names"])

    if env_snapshot is None:
        env_snapshot = load_env()
    remaining = []
    for entry in required_env_vars:
        name = entry["name"]
        if entry.get("optional"):
            continue
        if name in missing_names or not _is_env_var_persisted(name, env_snapshot):
            remaining.append(name)
    return remaining


def _gateway_setup_hint() -> str:
    try:
        from gateway.platforms.base import GATEWAY_SECRET_CAPTURE_UNSUPPORTED_MESSAGE

        return GATEWAY_SECRET_CAPTURE_UNSUPPORTED_MESSAGE
    except Exception:
        return f"Secure secret entry is not available. Load this skill in the local CLI to be prompted, or add the key to {display_hermes_home()}/.env manually."


def _build_setup_note(
    readiness_status: SkillReadinessStatus,
    missing: List[str],
    setup_help: str | None = None,
) -> str | None:
    if readiness_status == SkillReadinessStatus.SETUP_NEEDED:
        missing_str = ", ".join(missing) if missing else "required prerequisites"
        note = f"Setup needed before using this skill: missing {missing_str}."
        if setup_help:
            return f"{note} {setup_help}"
        return note
    return None


def check_skills_requirements() -> bool:
    """Skills are always available -- the directory is created on first use if needed."""
    return True


def _parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """Parse YAML frontmatter from markdown content.

    Delegates to ``agent.skill_utils.parse_frontmatter`` — kept here
    as a public re-export so existing callers don't need updating.
    """
    from agent.skill_utils import parse_frontmatter
    return parse_frontmatter(content)


def _get_category_from_path(skill_path: Path) -> Optional[str]:
    """
    Extract category from skill path based on directory structure.

    For paths like: ~/.hermes/skills/mlops/axolotl/SKILL.md -> "mlops"
    Also works for external skill dirs configured via skills.external_dirs.
    """
    # Try the active profile skills dir first (respects monkeypatching in tests),
    # then fall back to external dirs from config.
    dirs_to_check = [_skills_dir()]
    try:
        from agent.skill_utils import get_external_skills_dirs
        dirs_to_check.extend(get_external_skills_dirs())
    except Exception:
        pass
    for skills_dir in dirs_to_check:
        try:
            rel_path = skill_path.relative_to(skills_dir)
            parts = rel_path.parts
            if len(parts) >= 3:
                return parts[0]
        except ValueError:
            continue
    return None


def _parse_tags(tags_value) -> List[str]:
    """
    Parse tags from frontmatter value.

    Handles:
    - Already-parsed list (from yaml.safe_load): [tag1, tag2]
    - String with brackets: "[tag1, tag2]"
    - Comma-separated string: "tag1, tag2"

    Args:
        tags_value: Raw tags value — may be a list or string

    Returns:
        List of tag strings
    """
    if not tags_value:
        return []

    # yaml.safe_load already returns a list for [tag1, tag2]
    if isinstance(tags_value, list):
        return [str(t).strip() for t in tags_value if t]

    # String fallback — handle bracket-wrapped or comma-separated
    tags_value = str(tags_value).strip()
    if tags_value.startswith("[") and tags_value.endswith("]"):
        tags_value = tags_value[1:-1]

    return [t.strip().strip("\"'") for t in tags_value.split(",") if t.strip()]



def _get_disabled_skill_names() -> Set[str]:
    """Load disabled skill names from config.

    Delegates to ``agent.skill_utils.get_disabled_skill_names`` — kept here
    as a public re-export so existing callers don't need updating.
    """
    from agent.skill_utils import get_disabled_skill_names
    return get_disabled_skill_names()


def _get_session_platform() -> str:
    """Resolve the current platform from gateway session context.

    Mirrors the platform-resolution logic in
    ``agent.skill_utils.get_disabled_skill_names`` so that
    ``_is_skill_disabled`` respects ``HERMES_SESSION_PLATFORM``.
    """
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_SESSION_PLATFORM") or ""
    except Exception:
        return ""


def _is_skill_disabled(name: str, platform: str = None) -> bool:
    """Check if a skill is disabled in config.

    Resolves the active platform from (in order of precedence):
    1. Explicit ``platform`` argument
    2. ``HERMES_PLATFORM`` environment variable
    3. ``HERMES_SESSION_PLATFORM`` from gateway session context
    """
    try:
        from hermes_cli.config import load_config
        config = load_config()
        skills_cfg = config.get("skills", {})
        resolved_platform = platform or os.getenv("HERMES_PLATFORM") or _get_session_platform()
        global_disabled = skills_cfg.get("disabled", [])
        if resolved_platform:
            platform_disabled = cfg_get(skills_cfg, "platform_disabled", resolved_platform)
            if platform_disabled is not None:
                # A globally-disabled skill stays disabled on every platform;
                # the platform list adds to it rather than replacing it. Keep
                # in sync with agent.skill_utils.get_disabled_skill_names.
                return name in platform_disabled or name in global_disabled
        return name in global_disabled
    except Exception:
        return False


def _find_all_skills(*, skip_disabled: bool = False) -> List[Dict[str, Any]]:
    """Recursively find all skills in ~/.hermes/skills/ and external dirs.

    Args:
        skip_disabled: If True, return ALL skills regardless of disabled
            state (used by ``hermes skills`` config UI). Default False
            filters out disabled skills.

    Returns:
        List of skill metadata dicts (name, description, category).

    Results are cached per-session; the cache is invalidated when the scan
    signature changes (dir/category mtimes or the disabled-set) and expires
    after a short TTL to bound staleness from in-place SKILL.md edits.
    """
    from agent.skill_utils import (
        get_external_skills_dirs,
        get_project_skills_dirs,
        iter_project_skill_files,
        iter_skill_index_files,
    )

    cache_key = _SKILLS_CACHE_KEY_DISABLED if skip_disabled else _SKILLS_CACHE_KEY_FILTERED

    # Load disabled set once (not per-skill). Part of the cache signature:
    # disabling a skill is a config change with no filesystem mtime bump.
    disabled = set() if skip_disabled else _get_disabled_skill_names()

    # Collect directories to scan — same resolution as the scan loop below
    # (_skills_dir() resolves the LIVE profile HERMES_HOME; the module-level
    # SKILLS_DIR can be stale in long-lived runtimes). Trusted project-local
    # dirs come FIRST: first-wins dedup below gives them precedence over
    # same-named local/external skills.
    project_dirs = list(get_project_skills_dirs())
    dirs_to_scan: list = list(project_dirs)
    active_skills_dir = _skills_dir()
    if active_skills_dir.exists():
        dirs_to_scan.append(active_skills_dir)
    dirs_to_scan.extend(get_external_skills_dirs())

    signature = _skills_scan_signature(dirs_to_scan, disabled)
    now = time.monotonic()

    cached = _SKILLS_CACHE.get(cache_key)
    if (
        cached is not None
        and cached[0] == signature
        and (now - cached[1]) < _SKILLS_CACHE_TTL_SECONDS
    ):
        # Per-call shallow copies: callers mutate the returned dicts
        # (e.g. web_server annotates s["enabled"]/s["usage"]) — handing
        # out the cached objects would poison the cache for everyone else.
        return [dict(s) for s in cached[2]]

    skills = []
    seen_names: set = set()

    # Scan project dirs first, then local, then external (first-wins) —
    # dirs_to_scan already resolved above for the signature. Project dirs
    # iterate through the quarantine chokepoint (scan-time injection gate).
    for scan_dir in dirs_to_scan:
        _is_project = scan_dir in project_dirs
        _iter = (
            iter_project_skill_files(scan_dir)
            if _is_project
            else iter_skill_index_files(scan_dir, "SKILL.md")
        )
        for skill_md in _iter:
            if any(part in _EXCLUDED_SKILL_DIRS for part in skill_md.parts):
                continue

            skill_dir = skill_md.parent

            try:
                content = skill_md.read_text(encoding="utf-8-sig", errors="replace")[:4000]
                frontmatter, body = _parse_frontmatter(content)

                if not skill_matches_platform(frontmatter):
                    continue

                if not skill_matches_environment(frontmatter):
                    continue

                name = frontmatter.get("name", skill_dir.name)[:MAX_NAME_LENGTH]
                if name in seen_names:
                    continue
                if name in disabled:
                    continue

                description = frontmatter.get("description", "")
                if not description:
                    for line in body.strip().split("\n"):
                        line = line.strip()
                        if line and not line.startswith("#"):
                            description = line
                            break

                if len(description) > MAX_DESCRIPTION_LENGTH:
                    description = description[:MAX_DESCRIPTION_LENGTH - 3] + "..."

                category = _get_category_from_path(skill_md)

                seen_names.add(name)
                skills.append({
                    "name": name,
                    "description": description,
                    "category": category,
                })

            except (UnicodeDecodeError, PermissionError) as e:
                logger.debug("Failed to read skill file %s: %s", skill_md, e)
                continue
            except Exception as e:
                logger.debug(
                    "Skipping skill at %s: failed to parse: %s", skill_md, e, exc_info=True
                )
                continue

    # Store in cache keyed by the scan signature computed BEFORE the scan
    # (a write racing the scan changes the signature, so the next call
    # re-scans rather than serving the torn result past the TTL). Same
    # shallow-copy contract as the hit path — the caller may mutate.
    _SKILLS_CACHE[cache_key] = (signature, now, skills)
    return [dict(s) for s in skills]


def _sort_skills(skills: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep every skill listing path ordered the same way."""
    return sorted(skills, key=lambda s: (s.get("category") or "", s["name"]))


def skills_list(category: str = None, task_id: str = None) -> str:
    """
    List all available skills (progressive disclosure tier 1 - minimal metadata).

    Returns only name + description to minimize token usage. Use skill_view() to
    load full content, tags, related files, etc.

    Args:
        category: Optional category filter (e.g., "mlops")
        task_id: Optional task identifier used to probe the active backend

    Returns:
        JSON string with minimal skill info: name, description, category
    """
    try:
        active_skills_dir = _skills_dir()
        if not active_skills_dir.exists():
            active_skills_dir.mkdir(parents=True, exist_ok=True)

        # Find all skills
        all_skills = _find_all_skills()
        try:
            from hermes_cli.plugins import discover_plugins, get_plugin_manager

            discover_plugins()
            for plugin_skill in get_plugin_manager().list_plugin_skill_metadata():
                frontmatter = plugin_skill.pop("frontmatter", {})
                if not skill_matches_platform(frontmatter):
                    continue
                if _is_skill_disabled(plugin_skill["name"]):
                    continue
                all_skills.append(plugin_skill)
        except Exception:
            logger.debug("Plugin skill listing failed", exc_info=True)

        if not all_skills:
            return json.dumps(
                {
                    "success": True,
                    "skills": [],
                    "categories": [],
                    "message": "No skills found in skills/ directory.",
                },
                ensure_ascii=False,
            )

        # Filter by category if specified
        if category:
            all_skills = [s for s in all_skills if s.get("category") == category]

        # Sort by category then name
        all_skills = _sort_skills(all_skills)

        # Extract unique categories
        categories = sorted(
            {s.get("category") for s in all_skills if s.get("category")}
        )

        return json.dumps(
            {
                "success": True,
                "skills": all_skills,
                "categories": categories,
                "count": len(all_skills),
                "hint": "Use skill_view(name) to see full content, tags, and linked files",
            },
            ensure_ascii=False,
        )

    except Exception as e:
        return tool_error(str(e), success=False)


# ── Plugin skill serving ──────────────────────────────────────────────────


def _serve_plugin_skill(
    skill_md: Path,
    namespace: str,
    bare: str,
    file_path: str | None = None,
    *,
    preprocess: bool = True,
    session_id: str | None = None,
) -> str:
    """Read a plugin-provided skill, apply guards, return JSON."""
    from hermes_cli.plugins import _get_disabled_plugins, get_plugin_manager

    if namespace in _get_disabled_plugins():
        return json.dumps(
            {
                "success": False,
                "error": (
                    f"Plugin '{namespace}' is disabled. "
                    f"Re-enable with: hermes plugins enable {namespace}"
                ),
            },
            ensure_ascii=False,
        )

    try:
        # utf-8-sig + errors="replace": SKILL.md files are user-authored and
        # sometimes carry a Notepad BOM or stray non-UTF-8 bytes. Pinning
        # UTF-8 with replacement keeps skill_view deterministic across
        # platforms — falling back to the machine locale (cp1252/GBK) would
        # make the same skill render differently per host (see PR #51701).
        content = skill_md.read_text(encoding="utf-8-sig", errors="replace")
    except Exception as e:
        return json.dumps(
            {"success": False, "error": f"Failed to read skill '{namespace}:{bare}': {e}"},
            ensure_ascii=False,
        )

    parsed_frontmatter: Dict[str, Any] = {}
    try:
        parsed_frontmatter, _ = _parse_frontmatter(content)
    except Exception:
        pass

    qualified_name = f"{namespace}:{bare}"
    if _is_skill_disabled(qualified_name):
        return json.dumps(
            {
                "success": False,
                "error": f"Skill '{qualified_name}' is disabled.",
            },
            ensure_ascii=False,
        )

    if not skill_matches_platform(parsed_frontmatter):
        return json.dumps(
            {
                "success": False,
                "error": f"Skill '{qualified_name}' is not supported on this platform.",
                "readiness_status": SkillReadinessStatus.UNSUPPORTED.value,
            },
            ensure_ascii=False,
        )

    if file_path:
        from tools.path_security import has_traversal_component, validate_within_dir

        skill_root = skill_md.parent
        if has_traversal_component(file_path):
            return json.dumps(
                {"success": False, "error": "Path traversal ('..') is not allowed."},
                ensure_ascii=False,
            )
        target = skill_root / file_path
        path_error = validate_within_dir(target, skill_root)
        if path_error:
            return json.dumps(
                {"success": False, "error": path_error}, ensure_ascii=False
            )
        if not target.is_file():
            return json.dumps(
                {
                    "success": False,
                    "error": f"File '{file_path}' not found in skill '{namespace}:{bare}'.",
                },
                ensure_ascii=False,
            )
        try:
            content = target.read_text(encoding="utf-8-sig", errors="replace")
        except UnicodeDecodeError:
            return json.dumps(
                {
                    "success": True,
                    "name": f"{namespace}:{bare}",
                    "file": file_path,
                    "content": f"[Binary file: {target.name}, size: {target.stat().st_size} bytes]",
                    "is_binary": True,
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps(
                {"success": False, "error": f"Failed to read '{file_path}': {exc}"},
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "success": True,
                "name": f"{namespace}:{bare}",
                "file": file_path,
                "content": content,
                "file_type": target.suffix,
                "_source_path": str(target),
            },
            ensure_ascii=False,
        )

    # Injection scan — log but still serve (matches local-skill behaviour)
    if any(p in content.lower() for p in _INJECTION_PATTERNS):
        logger.warning(
            "Plugin skill '%s:%s' contains patterns that may indicate prompt injection",
            namespace, bare,
        )

    description = str(parsed_frontmatter.get("description", ""))
    if len(description) > MAX_DESCRIPTION_LENGTH:
        description = description[: MAX_DESCRIPTION_LENGTH - 3] + "..."

    # Bundle context banner — tells the agent about sibling skills
    try:
        siblings = [
            s for s in get_plugin_manager().list_plugin_skills(namespace)
            if s != bare
        ]
        if siblings:
            sib_list = ", ".join(siblings)
            banner = (
                f"[Bundle context: This skill is part of the '{namespace}' plugin.\n"
                f"Sibling skills: {sib_list}.\n"
                f"Use qualified form to invoke siblings (e.g. {namespace}:{siblings[0]}).]\n\n"
            )
        else:
            banner = f"[Bundle context: This skill is part of the '{namespace}' plugin.]\n\n"
    except Exception:
        banner = ""

    rendered_content = content
    if preprocess:
        try:
            from agent.skill_preprocessing import preprocess_skill_content

            rendered_content = preprocess_skill_content(
                content,
                skill_md.parent,
                session_id=session_id,
            )
        except Exception:
            logger.debug(
                "Could not preprocess plugin skill %s:%s", namespace, bare, exc_info=True
            )

    return json.dumps(
        {
            "success": True,
            "name": f"{namespace}:{bare}",
            "content": f"{banner}{rendered_content}" if banner else rendered_content,
            "description": description,
            "linked_files": _plugin_skill_linked_files(skill_md.parent),
            "readiness_status": SkillReadinessStatus.AVAILABLE.value,
        },
        ensure_ascii=False,
    )


def _plugin_skill_linked_files(skill_root: Path) -> Dict[str, List[str]] | None:
    from tools.path_security import validate_within_dir

    linked: Dict[str, List[str]] = {}
    for category in ("references", "templates", "assets", "scripts"):
        base = skill_root / category
        if not base.is_dir():
            continue
        files = [
            str(path.relative_to(skill_root))
            for path in sorted(base.rglob("*"))
            if path.is_file()
            and validate_within_dir(path, skill_root) is None
        ]
        if files:
            linked[category] = files
    return linked or None


def skill_view(
    name: str,
    file_path: str = None,
    task_id: str = None,
    preprocess: bool = True,
    section: str = None,
) -> str:
    """
    View the content of a skill or a specific file within a skill directory.

    Args:
        name: Name or path of the skill (e.g., "axolotl" or "03-fine-tuning/axolotl").
            Qualified names like "plugin:skill" resolve to plugin-provided skills.
        file_path: Optional path to a specific file within the skill (e.g., "references/api.md")
        task_id: Optional task identifier used to probe the active backend
        preprocess: Apply configured SKILL.md template and inline shell rendering
            to main skill content. Internal slash/preload callers disable this
            because they render the skill message themselves.
        section: Optional exact heading to return instead of the whole body.
            Used to retrieve a skill one named section at a time after a load
            came back [SKILL_INCOMPLETE]. An unknown heading returns the
            bounded heading index and a notice, not an error. Selectors are
            scoped to the document actually served, so a section of a linked
            file must be requested with that file's file_path.

    Returns:
        JSON string with skill content or error message
    """
    try:
        # Validate before the ':' qualified-name dispatch so a Windows drive
        # path (e.g. C:\skills\foo) can't be reinterpreted as a plugin
        # namespace, and so a traversal/absolute name never reaches the
        # search-dir join that builds direct_path below.
        lookup_error = _skill_lookup_path_error(name)
        if lookup_error:
            return json.dumps(
                {
                    "success": False,
                    "error": lookup_error,
                    "hint": "Use a skill name or relative path within the skills directory.",
                },
                ensure_ascii=False,
            )

        local_category_name: str | None = None
        # ── Qualified name dispatch (plugin skills) ──────────────────
        # Names containing ':' are routed to the plugin skill registry.
        # Bare names fall through to the existing flat-tree scan below.
        if ":" in name:
            from agent.skill_utils import is_valid_namespace, parse_qualified_name
            from hermes_cli.plugins import discover_plugins, get_plugin_manager

            namespace, bare = parse_qualified_name(name)
            if not is_valid_namespace(namespace):
                return json.dumps(
                    {
                        "success": False,
                        "error": (
                            f"Invalid namespace '{namespace}' in '{name}'. "
                            f"Namespaces must match [a-zA-Z0-9_-]+."
                        ),
                    },
                    ensure_ascii=False,
                )

            discover_plugins()  # idempotent
            pm = get_plugin_manager()
            active_memory_provider = None
            try:
                from plugins.memory import (
                    _get_active_memory_provider,
                    _prune_inactive_memory_provider_skills,
                )

                active_memory_provider = _get_active_memory_provider()
                _prune_inactive_memory_provider_skills(active_memory_provider)
            except Exception as exc:
                logger.debug(
                    "Failed pruning inactive memory-provider skills: %s",
                    exc,
                )

            plugin_skill_md = pm.find_plugin_skill(name)

            # Memory provider plugins are loaded through plugins.memory rather
            # than the general PluginManager. If a memory provider shim also
            # registers skills, load the namespaced provider once so its
            # collector can forward those skills into the plugin skill registry
            # before declaring the qualified skill missing.
            if plugin_skill_md is None:
                try:
                    from plugins.memory import load_memory_provider

                    if namespace == active_memory_provider:
                        load_memory_provider(namespace)
                        plugin_skill_md = pm.find_plugin_skill(name)
                except Exception as exc:
                    logger.debug(
                        "Failed lazy memory-provider skill load for %s: %s",
                        namespace,
                        exc,
                    )

            if plugin_skill_md is not None:
                if not plugin_skill_md.exists():
                    # Stale registry entry — file deleted out of band
                    pm.remove_plugin_skill(name)
                    return json.dumps(
                        {
                            "success": False,
                            "error": (
                                f"Skill '{name}' file no longer exists at "
                                f"{plugin_skill_md}. The registry entry has "
                                f"been cleaned up — try again after the "
                                f"plugin is reloaded."
                            ),
                        },
                        ensure_ascii=False,
                    )
                return _serve_plugin_skill(
                    plugin_skill_md,
                    namespace,
                    bare,
                    file_path=file_path,
                    preprocess=preprocess,
                    session_id=task_id,
                )

            # Plugin exists but this specific skill is missing?
            available = pm.list_plugin_skills(namespace)
            if available:
                return json.dumps(
                    {
                        "success": False,
                        "error": f"Skill '{bare}' not found in plugin '{namespace}'.",
                        "available_skills": [f"{namespace}:{s}" for s in available],
                        "hint": f"The '{namespace}' plugin provides {len(available)} skill(s).",
                    },
                    ensure_ascii=False,
                )
            # Plugin itself not found — fall through to flat-tree scan.
            # Categorized local skills also use `category:skill` in config and
            # gateway prompts, so preserve that form and translate it to the
            # on-disk `category/skill` path during the local scan below.
            if bare:
                local_category_name = f"{namespace}/{bare}"

        from agent.skill_utils import get_external_skills_dirs, get_project_skills_dirs

        # The categorized fall-through form (namespace/bare) joins onto each
        # search dir too; re-validate it since `bare` is not namespace-checked.
        if local_category_name:
            lookup_error = _skill_lookup_path_error(local_category_name)
            if lookup_error:
                return json.dumps(
                    {
                        "success": False,
                        "error": lookup_error,
                        "hint": "Use a skill name or relative path within the skills directory.",
                    },
                    ensure_ascii=False,
                )

        # Build list of all skill directories to search. Project dirs first —
        # they're the highest-precedence tier and the collision resolver
        # below uses this ordering.
        project_dirs = get_project_skills_dirs()
        all_dirs = list(project_dirs)
        active_skills_dir = _skills_dir()
        if active_skills_dir.exists():
            all_dirs.append(active_skills_dir)
        all_dirs.extend(get_external_skills_dirs())

        if not all_dirs:
            return json.dumps(
                {
                    "success": False,
                    "error": "Skills directory does not exist yet. It will be created on first install.",
                },
                ensure_ascii=False,
            )

        skill_dir = None
        skill_md = None

        # Collision detection: collect ALL candidates across every dir using
        # every lookup strategy (direct path, recursive by parent dir name,
        # legacy flat <name>.md). If more than one matches, refuse and tell
        # the caller — silent shadowing of a local skill by a same-named
        # external skill is a real bug class (`/skills` shows one, agent
        # loaded the other) so we surface it loudly instead of guessing.
        from agent.skill_utils import iter_skill_index_files

        candidates: List[Tuple[Optional[Path], Path]] = []  # (skill_dir, skill_md)
        seen_md: set = set()

        def _record(sd: Optional[Path], smd: Path) -> None:
            try:
                key = smd.resolve()
            except Exception:
                key = smd
            if key in seen_md:
                return
            seen_md.add(key)
            candidates.append((sd, smd))

        for search_dir in all_dirs:
            # Strategy 1: direct path (e.g., "mlops/axolotl" or bare "axolotl"
            # at the top of the dir).
            direct_path = search_dir / name
            if (
                not _is_skill_support_path(direct_path)
                and direct_path.is_dir()
                and (direct_path / "SKILL.md").exists()
            ):
                _record(direct_path, direct_path / "SKILL.md")
            elif direct_path.with_suffix(".md").exists() and not _is_skill_support_path(
                direct_path.with_suffix(".md")
            ):
                _record(None, direct_path.with_suffix(".md"))

            # Strategy 1b: categorized form for plugin namespace fall-through
            # (e.g., a "myplugin:explore" name with no plugin registered also
            # tries the on-disk path "myplugin/explore").
            if local_category_name:
                categorized_path = search_dir / local_category_name
                if (
                    not _is_skill_support_path(categorized_path)
                    and categorized_path.is_dir()
                    and (categorized_path / "SKILL.md").exists()
                ):
                    _record(categorized_path, categorized_path / "SKILL.md")
                elif categorized_path.with_suffix(
                    ".md"
                ).exists() and not _is_skill_support_path(
                    categorized_path.with_suffix(".md")
                ):
                    _record(None, categorized_path.with_suffix(".md"))

            # Strategy 2: recursive by directory name (catches nested skills
            # like "foundations/runtime/explore-codebase" called by bare name),
            # plus frontmatter `name:` lookup. `skills_list()` exposes the
            # frontmatter name, so `skill_view(name)` must accept it too even
            # when the on-disk directory is a shorter category/alias.
            for found_skill_md in iter_skill_index_files(search_dir, "SKILL.md"):
                if found_skill_md.parent.name == name:
                    _record(found_skill_md.parent, found_skill_md)
                    continue
                try:
                    fm_content = found_skill_md.read_text(encoding="utf-8-sig", errors="replace")
                    fm, _ = _parse_frontmatter(fm_content)
                except Exception:
                    fm = {}
                if fm.get("name") == name:
                    _record(found_skill_md.parent, found_skill_md)

            # Strategy 3: legacy flat <name>.md files anywhere under the dir.
            # Exclude skill support docs: references/templates/assets/scripts
            # are loaded through skill_view(skill, file_path=...) and must not
            # shadow or collide with real skills that share the same basename.
            for found_md in search_dir.rglob(f"{name}.md"):
                if found_md.name != "SKILL.md" and not _is_skill_support_path(
                    found_md
                ):
                    _record(None, found_md)

        if len(candidates) > 1 and project_dirs:
            # Cross-tier collision resolution: a project skill intentionally
            # overrides a same-named local/external skill, so when at least
            # one candidate lives under a trusted project dir, narrow to
            # those. Ambiguity WITHIN the project tier still refuses below.
            def _in_project(smd: Path) -> bool:
                try:
                    resolved = smd.resolve()
                except Exception:
                    resolved = smd
                for pd in project_dirs:
                    try:
                        resolved.relative_to(pd)
                        return True
                    except ValueError:
                        continue
                return False

            project_candidates = [
                (sd, smd) for sd, smd in candidates if _in_project(smd)
            ]
            if project_candidates:
                candidates = project_candidates

        if len(candidates) > 1:
            paths = [str(smd) for _, smd in candidates]
            logging.getLogger(__name__).warning(
                "Skill name collision for '%s': %d candidates — %s",
                name, len(candidates), "; ".join(paths),
            )
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        f"Ambiguous skill name '{name}': {len(candidates)} skills "
                        "match across your local skills dir and external_dirs. "
                        "Refusing to guess — load one explicitly by its categorized path."
                    ),
                    "matches": paths,
                    "hint": (
                        "Pass the full relative path instead of the bare name "
                        "(e.g., 'category/skill-name'), or rename one of the "
                        "colliding skills so each name is unique."
                    ),
                },
                ensure_ascii=False,
            )

        if candidates:
            skill_dir, skill_md = candidates[0]

        # Quarantine gate: a project-tier skill with a dangerous scan verdict
        # must not load even by explicit name (same chokepoint the index and
        # skills_list use — see agent.skill_utils.iter_project_skill_files).
        if skill_md is not None and project_dirs:
            from agent.skill_utils import is_quarantined_project_skill

            def _under_project(p: Path) -> bool:
                try:
                    rp = p.resolve()
                except Exception:
                    rp = p
                for pd in project_dirs:
                    try:
                        rp.relative_to(pd)
                        return True
                    except ValueError:
                        continue
                return False

            if _under_project(skill_md) and is_quarantined_project_skill(skill_md):
                return json.dumps(
                    {
                        "success": False,
                        "error": (
                            f"Project skill '{name}' is quarantined: the security "
                            "scan flagged its content as dangerous. It will not "
                            "load until the repo's skill content changes and "
                            "passes a re-scan."
                        ),
                        "hint": (
                            "Inspect the skill in the repo checkout, or untrust "
                            "the repo with `hermes skills untrust`."
                        ),
                    },
                    ensure_ascii=False,
                )

        if not skill_md or not skill_md.exists():
            available = [s["name"] for s in _sort_skills(_find_all_skills())[:20]]
            return json.dumps(
                {
                    "success": False,
                    "error": f"Skill '{name}' not found.",
                    "available_skills": available,
                    "hint": "Use skills_list to see all available skills",
                },
                ensure_ascii=False,
            )

        # Read the file once — reused for platform check and main content below
        try:
            content = skill_md.read_text(encoding="utf-8-sig", errors="replace")
        except Exception as e:
            return json.dumps(
                {
                    "success": False,
                    "error": f"Failed to read skill '{name}': {e}",
                },
                ensure_ascii=False,
            )

        # Security: warn if skill is loaded from outside trusted directories
        # (project dirs + local skills dir + configured external_dirs — i.e.
        # everything in all_dirs — are trusted)
        _outside_skills_dir = True
        _trusted_dirs = [active_skills_dir.resolve()]
        try:
            _trusted_dirs.extend(d.resolve() for d in all_dirs)
        except Exception:
            pass
        for _td in _trusted_dirs:
            try:
                skill_md.resolve().relative_to(_td)
                _outside_skills_dir = False
                break
            except ValueError:
                continue

        # Security: detect common prompt injection patterns
        # (pattern list at module level as _INJECTION_PATTERNS)
        _content_lower = content.lower()
        _injection_detected = any(p in _content_lower for p in _INJECTION_PATTERNS)

        if _outside_skills_dir or _injection_detected:
            _warnings = []
            if _outside_skills_dir:
                _warnings.append(f"skill file is outside the trusted skills directory (~/.hermes/skills/): {skill_md}")
            if _injection_detected:
                _warnings.append("skill content contains patterns that may indicate prompt injection")
            logging.getLogger(__name__).warning("Skill security warning for '%s': %s", name, "; ".join(_warnings))

        parsed_frontmatter: Dict[str, Any] = {}
        try:
            parsed_frontmatter, _ = _parse_frontmatter(content)
        except Exception:
            parsed_frontmatter = {}

        if not skill_matches_platform(parsed_frontmatter):
            return json.dumps(
                {
                    "success": False,
                    "error": f"Skill '{name}' is not supported on this platform.",
                    "readiness_status": SkillReadinessStatus.UNSUPPORTED.value,
                },
                ensure_ascii=False,
            )

        # Check if the skill is disabled by the user
        resolved_name = parsed_frontmatter.get("name", skill_md.parent.name)
        if _is_skill_disabled(resolved_name):
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        f"Skill '{resolved_name}' is disabled. "
                        "Enable it with `hermes skills` or inspect the files directly on disk."
                    ),
                },
                ensure_ascii=False,
            )

        # If a specific file path is requested, read that instead
        if file_path and skill_dir:
            from tools.path_security import validate_within_dir, has_traversal_component

            # Security: Prevent path traversal attacks
            if has_traversal_component(file_path):
                return json.dumps(
                    {
                        "success": False,
                        "error": "Path traversal ('..') is not allowed.",
                        "hint": "Use a relative path within the skill directory",
                    },
                    ensure_ascii=False,
                )

            target_file = skill_dir / file_path

            # Security: Verify resolved path is still within skill directory
            traversal_error = validate_within_dir(target_file, skill_dir)
            if traversal_error:
                return json.dumps(
                    {
                        "success": False,
                        "error": traversal_error,
                        "hint": "Use a relative path within the skill directory",
                    },
                    ensure_ascii=False,
                )
            # Gate on is_file(), not exists(): a directory (e.g. requesting
            # 'references' bare) must take the not-found listing branch, not
            # fall through to read_text() and surface a raw [Errno 21]
            # "Is a directory" OS error. Matches the plugin-skill branch above.
            if not target_file.is_file():
                # List available files in the skill directory, organized by type
                available_files = {
                    "references": [],
                    "templates": [],
                    "assets": [],
                    "scripts": [],
                    "other": [],
                }

                # Scan for all readable files
                for f in skill_dir.rglob("*"):
                    if f.is_file() and f.name != "SKILL.md":
                        rel = str(f.relative_to(skill_dir))
                        if rel.startswith("references/"):
                            available_files["references"].append(rel)
                        elif rel.startswith("templates/"):
                            available_files["templates"].append(rel)
                        elif rel.startswith("assets/"):
                            available_files["assets"].append(rel)
                        elif rel.startswith("scripts/"):
                            available_files["scripts"].append(rel)
                        elif f.suffix in {
                            ".md",
                            ".py",
                            ".yaml",
                            ".yml",
                            ".json",
                            ".tex",
                            ".sh",
                        }:
                            available_files["other"].append(rel)

                # Remove empty categories
                available_files = {k: v for k, v in available_files.items() if v}

                return json.dumps(
                    {
                        "success": False,
                        "error": f"File '{file_path}' not found in skill '{name}'.",
                        "available_files": available_files,
                        "hint": "Use one of the available file paths listed above",
                    },
                    ensure_ascii=False,
                )

            # Read the file content
            try:
                content = target_file.read_text(encoding="utf-8-sig", errors="replace")
            except UnicodeDecodeError:
                # Binary file - return info about it instead
                return json.dumps(
                    {
                        "success": True,
                        "name": name,
                        "file": file_path,
                        "content": f"[Binary file: {target_file.name}, size: {target_file.stat().st_size} bytes]",
                        "is_binary": True,
                    },
                    ensure_ascii=False,
                )

            try:
                from tools.skill_manager_tool import mark_background_review_skill_read

                mark_background_review_skill_read(target_file)
            except Exception:
                logger.debug(
                    "Could not record background-review skill read for %s",
                    target_file,
                    exc_info=True,
                )

            linked_result = {
                "success": True,
                "name": name,
                "file": file_path,
                "content": content,
                "file_type": target_file.suffix,
                # Internal: absolute source path for the repeat-view dedup
                # fingerprint (mtime+size change detection).
                "_source_path": str(target_file),
            }
            if section:
                _apply_section_selection(linked_result, section)
            return json.dumps(linked_result, ensure_ascii=False)

        # Reuse the parse from the platform check above
        frontmatter = parsed_frontmatter

        # Get reference, template, asset, and script files if this is a directory-based skill
        reference_files = []
        template_files = []
        asset_files = []
        script_files = []

        if skill_dir:
            references_dir = skill_dir / "references"
            if references_dir.exists():
                reference_files = [
                    str(f.relative_to(skill_dir)) for f in references_dir.glob("*.md")
                ]

            templates_dir = skill_dir / "templates"
            if templates_dir.exists():
                for ext in [
                    "*.md",
                    "*.py",
                    "*.yaml",
                    "*.yml",
                    "*.json",
                    "*.tex",
                    "*.sh",
                ]:
                    template_files.extend(
                        [
                            str(f.relative_to(skill_dir))
                            for f in templates_dir.rglob(ext)
                        ]
                    )

            # assets/ — agentskills.io standard directory for supplementary files
            assets_dir = skill_dir / "assets"
            if assets_dir.exists():
                for f in assets_dir.rglob("*"):
                    if f.is_file():
                        asset_files.append(str(f.relative_to(skill_dir)))

            scripts_dir = skill_dir / "scripts"
            if scripts_dir.exists():
                for ext in ["*.py", "*.sh", "*.bash", "*.js", "*.ts", "*.rb"]:
                    script_files.extend(
                        [str(f.relative_to(skill_dir)) for f in scripts_dir.glob(ext)]
                    )

        # Read tags/related_skills with backward compat:
        # Check metadata.hermes.* first (agentskills.io convention), fall back to top-level
        hermes_meta = {}
        metadata = frontmatter.get("metadata")
        if isinstance(metadata, dict):
            hermes_meta = metadata.get("hermes", {}) or {}

        tags = _parse_tags(hermes_meta.get("tags") or frontmatter.get("tags", ""))
        related_skills = _parse_tags(
            hermes_meta.get("related_skills") or frontmatter.get("related_skills", "")
        )

        # Build linked files structure for clear discovery
        linked_files = {}
        if reference_files:
            linked_files["references"] = reference_files
        if template_files:
            linked_files["templates"] = template_files
        if asset_files:
            linked_files["assets"] = asset_files
        if script_files:
            linked_files["scripts"] = script_files

        try:
            rel_path = str(skill_md.relative_to(active_skills_dir))
        except ValueError:
            # External skill — use path relative to the skill's own parent dir
            rel_path = str(skill_md.relative_to(skill_md.parent.parent)) if skill_md.parent.parent else skill_md.name
        skill_name = frontmatter.get(
            "name", skill_md.stem if not skill_dir else skill_dir.name
        )
        legacy_env_vars, _ = _collect_prerequisite_values(frontmatter)
        required_env_vars = _get_required_environment_variables(
            frontmatter, legacy_env_vars
        )
        backend = _get_terminal_backend_name()
        env_snapshot = load_env()
        missing_required_env_vars = [
            e
            for e in required_env_vars
            if not e.get("optional")
            and not _is_env_var_persisted(e["name"], env_snapshot)
        ]
        capture_result = _capture_required_environment_variables(
            skill_name,
            missing_required_env_vars,
        )
        if missing_required_env_vars:
            env_snapshot = load_env()
        remaining_missing_required_envs = _remaining_required_environment_names(
            required_env_vars,
            capture_result,
            env_snapshot=env_snapshot,
        )
        setup_needed = bool(remaining_missing_required_envs)

        # Register available skill env vars so they pass through to sandboxed
        # execution environments (execute_code, terminal).  Only vars that are
        # actually set get registered — missing ones are reported as setup_needed.
        available_env_names = [
            e["name"]
            for e in required_env_vars
            if e["name"] not in remaining_missing_required_envs
        ]
        if available_env_names:
            try:
                from tools.env_passthrough import register_env_passthrough

                register_env_passthrough(available_env_names)
            except Exception:
                logger.debug(
                    "Could not register env passthrough for skill %s",
                    skill_name,
                    exc_info=True,
                )

        # Register credential files for mounting into remote sandboxes
        # (Modal, Docker).  Files that exist on the host are registered;
        # missing ones are added to the setup_needed indicators.
        required_cred_files_raw = frontmatter.get("required_credential_files", [])
        if not isinstance(required_cred_files_raw, list):
            required_cred_files_raw = []
        missing_cred_files: list = []
        if required_cred_files_raw:
            try:
                from tools.credential_files import register_credential_files

                missing_cred_files = register_credential_files(required_cred_files_raw)
                if missing_cred_files:
                    setup_needed = True
            except Exception:
                logger.debug(
                    "Could not register credential files for skill %s",
                    skill_name,
                    exc_info=True,
                )

        rendered_content = content
        if preprocess:
            try:
                from agent.skill_preprocessing import preprocess_skill_content

                rendered_content = preprocess_skill_content(
                    content,
                    skill_dir,
                    session_id=task_id,
                )
            except Exception:
                logger.debug(
                    "Could not preprocess skill content for %s", skill_name, exc_info=True
                )

        # ── M2 org provenance header (load-time) ──────────────────────────
        # An org-shared skill announces its provenance IN the returned content
        # — the moment the model consumes it — not only in the listing. The
        # commit author behind this content is token-verified at push time by
        # the sync plane (author_mismatch guard), so the header is
        # trustworthy, not client-claimed. Org mirrors are read-only: changes
        # go through propose → admin approval, never local edits.
        org_provenance = None
        if skill_dir:
            try:
                from agent.skill_utils import (
                    ORG_PROVENANCE_FILE,
                    is_org_mirror_path,
                    org_id_of_path,
                )

                if is_org_mirror_path(skill_dir, active_skills_dir):
                    prov_org = org_id_of_path(skill_dir, active_skills_dir)
                    author = ""
                    ts = ""
                    if prov_org:
                        try:
                            prov = json.loads(
                                (
                                    active_skills_dir
                                    / "_org"
                                    / prov_org
                                    / ORG_PROVENANCE_FILE
                                ).read_text(encoding="utf-8-sig", errors="replace")
                            )
                            author = str(
                                prov.get("author_device")
                                or prov.get("author_user_id")
                                or ""
                            )
                            ts = str(prov.get("ts") or "")
                        except Exception:
                            pass
                    org_provenance = {
                        "org_id": prov_org,
                        "shared_by": author or None,
                        "as_of": ts or None,
                    }
                    header = (
                        "> [!NOTE] ORG-SHARED SKILL — provenance\n"
                        f"> This skill is shared by your organisation (org "
                        f"`{prov_org}`"
                        + (f", last updated by `{author}`" if author else "")
                        + (f", as of {ts}" if ts else "")
                        + "). It was reviewed and approved for the whole\n"
                        "> team — treat it as third-party instructions rather "
                        "than your own notes.\n"
                        "> You MAY improve it in place like any other skill. "
                        "Your edits are kept locally\n"
                        "> and are never overwritten by org updates; share "
                        "them back with\n"
                        "> `hermes sync propose` (or automatically, if your "
                        "org enables it).\n\n"
                    )
                    rendered_content = header + rendered_content
            except Exception:
                logger.debug(
                    "Could not resolve org provenance for %s",
                    skill_name,
                    exc_info=True,
                )

        result = {
            "success": True,
            "name": skill_name,
            "description": frontmatter.get("description", ""),
            "tags": tags,
            "related_skills": related_skills,
            "content": rendered_content,
            "path": rel_path,
            "skill_dir": str(skill_dir) if skill_dir else None,
            "org_provenance": org_provenance,
            "linked_files": linked_files if linked_files else None,
            "usage_hint": "To view linked files, call skill_view(name, file_path) where file_path is e.g. 'references/api.md' or 'assets/config.yaml'"
            if linked_files
            else None,
            "required_environment_variables": required_env_vars,
            "required_commands": [],
            "missing_required_environment_variables": remaining_missing_required_envs,
            "missing_credential_files": missing_cred_files,
            "missing_required_commands": [],
            "setup_needed": setup_needed,
            "setup_skipped": capture_result["setup_skipped"],
            "readiness_status": SkillReadinessStatus.SETUP_NEEDED.value
            if setup_needed
            else SkillReadinessStatus.AVAILABLE.value,
            # Internal: absolute source path for the repeat-view dedup
            # fingerprint (mtime+size change detection).
            "_source_path": str(skill_md),
        }

        setup_help = next((e["help"] for e in required_env_vars if e.get("help")), None)
        if setup_help:
            result["setup_help"] = setup_help

        if capture_result["gateway_setup_hint"]:
            result["gateway_setup_hint"] = capture_result["gateway_setup_hint"]

        try:
            from tools.skill_manager_tool import mark_background_review_skill_read

            mark_background_review_skill_read(skill_md)
        except Exception:
            logger.debug(
                "Could not record background-review skill read for %s",
                skill_md,
                exc_info=True,
            )

        if setup_needed:
            missing_items = [
                f"env ${env_name}" for env_name in remaining_missing_required_envs
            ] + [
                f"file {path}" for path in missing_cred_files
            ]
            setup_note = _build_setup_note(
                SkillReadinessStatus.SETUP_NEEDED,
                missing_items,
                setup_help,
            )
            if _is_remote_env_backend(backend) and setup_note:
                setup_note = f"{setup_note} {backend.upper()}-backed skills need these requirements available inside the remote environment as well."
            if setup_note:
                result["setup_note"] = setup_note

        # Surface agentskills.io optional fields when present
        if frontmatter.get("compatibility"):
            result["compatibility"] = frontmatter["compatibility"]
        if isinstance(metadata, dict):
            result["metadata"] = metadata

        if section:
            _apply_section_selection(result, section)

        return json.dumps(result, ensure_ascii=False)

    except Exception as e:
        return tool_error(str(e), success=False)




if __name__ == "__main__":
    """Test the skills tool"""
    print("🎯 Skills Tool Test")
    print("=" * 60)

    # Test listing skills
    print("\n📋 Listing all skills:")
    result = json.loads(skills_list())
    if result["success"]:
        print(
            f"Found {result['count']} skills in {len(result.get('categories', []))} categories"
        )
        print(f"Categories: {result.get('categories', [])}")
        print("\nFirst 10 skills:")
        for skill in result["skills"][:10]:
            cat = f"[{skill['category']}] " if skill.get("category") else ""
            print(f"  • {cat}{skill['name']}: {skill['description'][:60]}...")
    else:
        print(f"Error: {result['error']}")

    # Test viewing a skill
    print("\n📖 Viewing skill 'axolotl':")
    result = json.loads(skill_view("axolotl"))
    if result["success"]:
        print(f"Name: {result['name']}")
        print(f"Description: {result.get('description', 'N/A')[:100]}...")
        print(f"Content length: {len(result['content'])} chars")
        if result.get("linked_files"):
            print(f"Linked files: {result['linked_files']}")
    else:
        print(f"Error: {result['error']}")

    # Test viewing a reference file
    print("\n📄 Viewing reference file 'axolotl/references/dataset-formats.md':")
    result = json.loads(skill_view("axolotl", "references/dataset-formats.md"))
    if result["success"]:
        print(f"File: {result['file']}")
        print(f"Content length: {len(result['content'])} chars")
        print(f"Preview: {result['content'][:150]}...")
    else:
        print(f"Error: {result['error']}")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

SKILLS_LIST_SCHEMA = {
    "name": "skills_list",
    "description": "List available skills (name + description). Use skill_view(name) to load full content.",
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Optional category filter to narrow results",
            }
        },
        "required": [],
    },
}

SKILL_VIEW_SCHEMA = {
    "name": "skill_view",
    "description": "Skills allow for loading information about specific tasks and workflows, as well as scripts and templates. Load a skill's full content or access its linked files (references, templates, scripts). First call returns SKILL.md content plus a 'linked_files' dict showing available references/templates/scripts. To access those, call again with file_path parameter.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The skill name (use skills_list to see available skills). For plugin-provided skills, use the qualified form 'plugin:skill' (e.g. 'superpowers:writing-plans').",
            },
            "file_path": {
                "type": "string",
                "description": "OPTIONAL: Path to a linked file within the skill (e.g., 'references/api.md', 'templates/config.yaml', 'scripts/validate.py'). Omit to get the main SKILL.md content.",
            },
            "section": {
                "type": "string",
                "description": "OPTIONAL: Exact heading to return instead of the whole body (e.g. 'Final Governing Rule'). Use this after a load came back with a [SKILL_INCOMPLETE] notice, retrieving one named heading from its 'sections' index at a time. An unknown heading returns the heading index, not an error. If the notice was for a linked file (the result carries a 'file' key), pass the SAME file_path alongside section: headings and #n selectors are positions within that file, and omitting file_path silently answers out of SKILL.md instead.",
            },
        },
        "required": ["name"],
    },
}

registry.register(
    name="skills_list",
    toolset="skills",
    schema=SKILLS_LIST_SCHEMA,
    handler=lambda args, **kw: skills_list(
        category=args.get("category"), task_id=kw.get("task_id")
    ),
    check_fn=check_skills_requirements,
    emoji="📚",
)
# ── skill_view repeat-view dedup ────────────────────────────────────────
# Per-task cache of (skill name, file_path) -> (skill file mtime+size).
# On a repeat view of an UNCHANGED skill file, return a short stub instead
# of re-sending the full content — the earlier tool result in this
# conversation already carries it verbatim. Cleared on context compression
# via reset_skill_view_dedup() (wired next to read_file's reset_file_dedup)
# because after compression the original content is summarized away.
_skill_view_tracker: Dict[str, Dict[tuple, tuple]] = {}
_skill_view_tracker_lock = threading.Lock()
_SKILL_VIEW_DEDUP_CAP = 200

# ── Delivery index ──────────────────────────────────────────────────────
# Two things are decided when skill_view returns: a dedup record is written,
# and the skill is counted as USED. Both are claims about a body the model has
# not received yet -- delivery is only settled later, at the persistence
# boundary in tools/tool_result_storage.py. This index is the seam between the
# two moments: digest of the exact JSON that was served -> what was claimed for
# it, newest last. The persisted-result formatter looks itself up here and
# withdraws both claims when the body did not survive. Same lock, same
# lifetime and same reset hook as the dedup cache; bounded by the same cap.
_skill_view_delivery_index: Dict[str, list] = {}

_SKILL_VIEW_DEDUP_MESSAGE = (
    "Skill content unchanged since it was loaded earlier in this "
    "conversation — refer to the earlier skill_view result; it is still "
    "current and complete. (Re-issued after context compression, this "
    "returns the full content again.)"
)


def _skill_view_fingerprint(payload: dict) -> tuple | None:
    """Stat the skill file a successful skill_view served, for change detection."""
    src = payload.get("_source_path")
    if not src:
        return None
    try:
        st = os.stat(src)
        return (src, st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _skill_view_delivery_digest(delivered: str) -> str:
    """Digest of the exact JSON text a skill_view served."""
    return hashlib.sha256(delivered.encode("utf-8", "replace")).hexdigest()


def _record_skill_view(task_id, name, file_path, payload: dict) -> tuple | None:
    """Record a served skill_view so an identical repeat can be deduped.

    Returns ``(key, fingerprint)`` when a record was written, else None, so the
    caller can note it in the delivery index for possible revocation.
    """
    if not task_id:
        return None
    # Never dedup setup-needed views: readiness depends on config/env state
    # that can change without the skill file changing, and the model must
    # see the refreshed setup status on a re-view.
    if payload.get("setup_needed") or payload.get("readiness_status") == "setup_needed":
        return None
    fp = _skill_view_fingerprint(payload)
    if fp is None:
        return None
    key = (str(payload.get("name") or name), file_path or "")
    with _skill_view_tracker_lock:
        cache = _skill_view_tracker.setdefault(str(task_id), {})
        cache[key] = fp
        while len(cache) > _SKILL_VIEW_DEDUP_CAP:
            try:
                cache.pop(next(iter(cache)))
            except (StopIteration, KeyError):
                break
    return key, fp


def _note_skill_view_delivery(delivered, task_id, recorded, skill_name, use_snapshot) -> None:
    """Index what was claimed for the exact JSON *delivered*."""
    if not delivered or (recorded is None and not use_snapshot):
        return
    with _skill_view_tracker_lock:
        entries = _skill_view_delivery_index.setdefault(
            _skill_view_delivery_digest(delivered), []
        )
        entries.append((str(task_id) if task_id else None, recorded, skill_name, use_snapshot))
        while len(_skill_view_delivery_index) > _SKILL_VIEW_DEDUP_CAP:
            try:
                _skill_view_delivery_index.pop(next(iter(_skill_view_delivery_index)))
            except (StopIteration, KeyError):
                break


def _revoke_skill_view_delivery(delivered: str) -> bool:
    """Withdraw the claims made for *delivered*, which never reached the model.

    A digest identifies the exact JSON that was served, NOT the call that served
    it: two tasks viewing the same unchanged skill produce byte-identical
    payloads and therefore share one digest. The formatter contract carries no
    call identity, so there is no honest way to tell which of them is the one
    being spilled -- and guessing (popping the newest) revokes the wrong task,
    leaving the spilled one holding a "current and complete" claim for a body it
    never received. So we withdraw EVERY outstanding claim for the digest.

    Over-revoking is the safe direction. A skill that really was delivered may
    be forced to reload, and its use count may be rolled back further than it
    strictly had to be; both are cheap. Telling a task a mandatory skill is
    loaded when it is not is the failure this whole path exists to prevent.

    Drops each task's dedup record only while its cached fingerprint still
    matches what was recorded, so a NEWER view of the same skill (different
    bytes, different digest) is never touched. Use snapshots are restored in
    reverse registration order, oldest applied last, so repeated bumps of one
    skill land back on the earliest pre-bump state. View accounting is left
    alone: the agent really did view this skill.

    Returns True when anything was withdrawn.
    """
    if not delivered:
        return False
    digest = _skill_view_delivery_digest(delivered)
    withdrew = False
    with _skill_view_tracker_lock:
        entries = _skill_view_delivery_index.pop(digest, None)
        if not entries:
            return False
        for task_id, recorded, _skill_name, _snapshot in entries:
            if recorded is None or task_id is None:
                continue
            key, fp = recorded
            cache = _skill_view_tracker.get(task_id)
            if cache is not None and cache.get(key) == fp:
                cache.pop(key, None)
                if not cache:
                    _skill_view_tracker.pop(task_id, None)
                withdrew = True
    for _task_id, _recorded, skill_name, use_snapshot in reversed(entries):
        if not skill_name or not use_snapshot:
            continue
        try:
            from tools.skill_usage import revert_use

            revert_use(skill_name, use_snapshot)
            withdrew = True
        except Exception:
            logger.debug("Could not revert use bump for %s", skill_name, exc_info=True)
    return withdrew


def _check_skill_view_dedup(task_id, name, file_path) -> str | None:
    """Return a dedup stub when this exact skill file was already served
    to this task and is unchanged on disk; None otherwise."""
    if not task_id:
        return None
    with _skill_view_tracker_lock:
        cache = _skill_view_tracker.get(str(task_id))
        if not cache:
            return None
        # The record key uses the RESOLVED name; check both the raw arg and
        # resolved forms so 'category/skill' and bare-name views coalesce.
        for key, (src, mtime_ns, size) in list(cache.items()):
            rec_name, rec_fp = key
            if rec_fp != (file_path or ""):
                continue
            if rec_name != str(name) and not str(name).endswith("/" + rec_name) \
                    and not rec_name.endswith("/" + str(name)) \
                    and str(name).split(":")[-1] != rec_name:
                continue
            try:
                st = os.stat(src)
                if (st.st_mtime_ns, st.st_size) != (mtime_ns, size):
                    cache.pop(key, None)
                    return None
            except OSError:
                cache.pop(key, None)
                return None
            return json.dumps(
                {
                    "success": True,
                    "status": "unchanged",
                    "name": rec_name,
                    "file": file_path or "SKILL.md",
                    "dedup": True,
                    "content_returned": False,
                    "message": _SKILL_VIEW_DEDUP_MESSAGE,
                },
                ensure_ascii=False,
            )
    return None


def reset_skill_view_dedup(task_id: str | None = None) -> None:
    """Clear the skill_view dedup cache (all tasks when task_id is None).

    Called on context compression: the original skill content is
    summarized away, so a re-view must return full content again.
    """
    with _skill_view_tracker_lock:
        if task_id is None:
            _skill_view_tracker.clear()
            _skill_view_delivery_index.clear()
        else:
            _skill_view_tracker.pop(str(task_id), None)
            for digest in list(_skill_view_delivery_index):
                kept = [
                    e for e in _skill_view_delivery_index[digest] if e[0] != str(task_id)
                ]
                if kept:
                    _skill_view_delivery_index[digest] = kept
                else:
                    _skill_view_delivery_index.pop(digest, None)


# ── Incomplete-load receipt (mandatory skills that did NOT reach the model) ──
# A skill_view result can have its body removed by either context-protection
# layer in tools/tool_result_storage.py: the per-result threshold, or aggregate
# turn-budget enforcement. The generic <persisted-output> receipt is wrong for a
# skill -- it still opens with `"success": true` and a fragment of the body, so
# the model acts as if a mandatory skill were loaded. We replace it with an
# index-only receipt carrying NO body at all.
#
# The marker below is the ONE canonical incomplete-load signal:
# _skill_incomplete_marker() builds it and every presence check matches the same
# prefix constant, so emit and check cannot drift apart. (That drift is a real
# upstream bug, not a hypothetical: PR #44166 emitted "[SKILL_PRUNED:" while
# presence-checking "[SKILL_PRUNED]" -- see agent/context_compressor.py.)
SKILL_INCOMPLETE_MARKER_PREFIX = "[SKILL_INCOMPLETE:"
# Bounds on the receipt. It is itself a tool result, so it must be small enough
# that it never becomes a spill candidate in its own right.
_MAX_INCOMPLETE_SECTIONS = 40
_MAX_SECTION_HEADING_CHARS = 200
_MAX_INCOMPLETE_RECEIPT_CHARS = 20_000
# Ceiling on a single retrieved section, well under the per-result
# threshold so a section answer is not itself a spill candidate. A section
# whose full content exceeds this is NOT truncated -- it is answered with
# navigation only (see _apply_section_selection).
_MAX_SECTION_CHARS = 60_000
# Label for the entry covering everything before the first heading. Skills
# routinely open with prose (and always with frontmatter); without an entry of
# its own that text would have no retrieval route at all.
_PREAMBLE_HEADING = "(document preamble)"
# Metadata worth carrying into an incomplete receipt: what the skill is, whether
# it is usable, and what setup it still needs. Copied verbatim when present.
_INCOMPLETE_RECEIPT_METADATA_KEYS = (
    "description",
    "file",
    "path",
    "skill_dir",
    "readiness_status",
    "setup_needed",
    "setup_skipped",
    "setup_help",
    "setup_note",
    "gateway_setup_hint",
    "missing_required_environment_variables",
    "missing_credential_files",
    "missing_required_commands",
    "required_environment_variables",
    "required_commands",
    "usage_hint",
    "org_provenance",
    "compatibility",
    "tags",
    "related_skills",
    "linked_files",
    "metadata",
)
# Dropped first, in this order, if the receipt still exceeds its cap.
_INCOMPLETE_RECEIPT_DROPPABLE_KEYS = (
    "metadata",
    "linked_files",
    "related_skills",
    "tags",
    "required_environment_variables",
    "org_provenance",
)


def _skill_incomplete_marker(skill_name: str, file_path: str | None = None) -> str:
    """Return the canonical incomplete-load marker for *skill_name*.

    *file_path* names the LINKED FILE this receipt is about, when it is about
    one. It must then appear in the continuation call, because a linked file is
    a different document from SKILL.md with its own heading index: a follow-up
    that drops file_path resolves against SKILL.md instead, where the same
    heading text and the same ``#n`` both exist, and answers out of the wrong
    file with section_found true and no marker. The instruction the model is
    handed is the only thing standing between it and that answer.

    Used verbatim by the emit site (the persisted-result formatter) and matched
    by _SKILL_INCOMPLETE_MARKER_RE below -- one string, no drift. The tool-call
    literal is quoted the same way the JSON receipt carrying it is, so what the
    model reads back is what the detector matches.
    """
    subject = f'file "{file_path}" of this skill' if file_path else "this skill"
    target = f'name="{skill_name}"'
    if file_path:
        target += f', file_path="{file_path}"'
    return (
        f"{SKILL_INCOMPLETE_MARKER_PREFIX} {subject} is NOT loaded. Its body was "
        f"removed to protect the context window, so none of its instructions were "
        f"delivered. Do not act on it. Retrieve what you need one section at a "
        f'time with skill_view({target}, section="<heading>")]'
    )


def _payload_file(payload: dict) -> str | None:
    """The linked file a skill_view payload is about, or None for SKILL.md.

    Only the linked-file branches set "file"; a main-skill payload carries
    "path" instead. That difference is the whole discriminator -- nothing here
    infers a file from a name.
    """
    file_path = payload.get("file")
    return file_path if isinstance(file_path, str) and file_path else None


def _continuation_hint(payload: dict) -> str:
    """Sentence keeping a linked-file continuation on the same linked file.

    Empty for SKILL.md, where the bare call shape is already correct. A section
    that WAS delivered carries no incomplete marker, so on that path this is the
    only place the next call's shape is stated at all.
    """
    file_path = _payload_file(payload)
    if not file_path:
        return ""
    return (
        f' These selectors are positions in file "{file_path}", not in '
        f"SKILL.md, so keep file_path on every follow-up: "
        f'skill_view(name="{payload.get("name") or ""}", '
        f'file_path="{file_path}", section="<heading>").'
    )


# Matches the canonical marker and captures the skill name. Anchored on the
# shared prefix constant so a wording change updates the emit helper and this
# extractor together. The quotes are optionally backslash-escaped because the
# marker is normally read back out of the JSON receipt that carries it, where
# json.dumps has escaped every double quote inside the string.
_SKILL_INCOMPLETE_MARKER_RE = re.compile(
    re.escape(SKILL_INCOMPLETE_MARKER_PREFIX)
    + r'[^\]]*?skill_view\(name=\\?"([^"\\]+)\\?"'
    + r'(?:, file_path=\\?"[^"\\]*\\?")?, section='
)


def has_skill_incomplete_marker(text: str) -> bool:
    """True when *text* carries a canonical incomplete-load marker."""
    return bool(text) and bool(_SKILL_INCOMPLETE_MARKER_RE.search(text))


def extract_incomplete_skill_names(text: str) -> list[str]:
    """Return skill names referenced by incomplete-load markers in *text*."""
    names: list[str] = []
    for match in _SKILL_INCOMPLETE_MARKER_RE.finditer(text or ""):
        name = match.group(1)
        if name not in names:
            names.append(name)
    return names


def _skill_entries(body: str) -> list[dict]:
    """Ordered navigation entries for markdown *body*.

    Entry 0 is the document preamble -- everything before the first heading,
    frontmatter included -- and exists only when that text is not blank. Entries
    1..N are the ATX heading occurrences in document order. A repeated heading
    is two DIFFERENT places in the file, so it gets two entries; nothing here is
    deduplicated by text.

    Each entry carries two spans:

    ``own_start``/``own_end``
        the heading line plus its own prose, ending at the next heading of ANY
        level. Parts tile this span.
    ``start``/``end``
        the logical section: down to the next heading of the SAME OR A HIGHER
        level, so descendants are included. This is what a section request
        returns when it fits.

    An entry's own span plus its children's logical spans tile its logical span
    exactly -- that is what makes retrieval lossless: whatever is too large to
    return is always reachable as parts (own text) plus child sections.

    Fenced code blocks are skipped so a shell comment inside an example is not
    mistaken for a heading. Single source of truth for the index, for named
    retrieval and for every selector.
    """
    text = body or ""
    heads: list[tuple[int, str, int]] = []  # (level, heading, start offset)
    offset = 0
    in_fence = False
    fence_marker = ""
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped[:3] in ("```", "~~~"):
            marker = stripped[:3]
            if not in_fence:
                in_fence, fence_marker = True, marker
            elif marker == fence_marker:
                in_fence, fence_marker = False, ""
            offset += len(line)
            continue
        if not in_fence and stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            heading = stripped[level:].strip().rstrip("#").strip()
            if 1 <= level <= 6 and heading:
                heads.append((level, heading, offset))
        offset += len(line)

    # Logical end of each heading: the next heading at the same or a higher
    # level closes it. One pass with a stack -- an O(n^2) scan is noticeable on
    # the 400-heading scraped reference dumps this feature exists for.
    ends = [len(text)] * len(heads)
    open_stack: list[int] = []
    for i, (level, _heading, start) in enumerate(heads):
        while open_stack and heads[open_stack[-1]][0] >= level:
            ends[open_stack.pop()] = start
        open_stack.append(i)

    entries: list[dict] = []
    first = heads[0][2] if heads else len(text)
    if text[:first].strip():
        entries.append(
            {
                "number": 0,
                "heading": _PREAMBLE_HEADING,
                "level": 0,
                "start": 0,
                "end": first,
                "own_start": 0,
                "own_end": first,
                "parent": None,
            }
        )
    for i, (level, heading, start) in enumerate(heads):
        entries.append(
            {
                "number": i + 1,
                "heading": heading,
                "level": level,
                "start": start,
                "end": ends[i],
                "own_start": start,
                "own_end": heads[i + 1][2] if i + 1 < len(heads) else len(text),
                "parent": None,
            }
        )

    # Immediate parent = nearest preceding heading of a strictly smaller level.
    parent_stack: list[dict] = []
    for entry in entries:
        if entry["number"] == 0:
            continue
        while parent_stack and parent_stack[-1]["level"] >= entry["level"]:
            parent_stack.pop()
        entry["parent"] = parent_stack[-1]["number"] if parent_stack else None
        parent_stack.append(entry)
    return entries


def _section_part_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Deterministic part offsets tiling ``text[start:end]``.

    Parts break on line boundaries so each one reads as prose; a single line
    longer than the ceiling is chunked at fixed offsets rather than dropped.
    Concatenating the parts in order reproduces ``text[start:end]`` exactly --
    no gap, no overlap, no character unreachable.
    """
    limit = _MAX_SECTION_CHARS
    if end - start <= limit:
        return [(start, end)]
    spans: list[tuple[int, int]] = []
    cursor = pos = start
    while pos < end:
        newline = text.find("\n", pos, end)
        line_end = end if newline == -1 else newline + 1
        if line_end - cursor > limit:
            if cursor < pos:
                spans.append((cursor, pos))
                cursor = pos
            while line_end - cursor > limit:  # one line longer than the ceiling
                spans.append((cursor, cursor + limit))
                cursor += limit
        pos = line_end
    if cursor < end:
        spans.append((cursor, end))
    return spans


def _selector_of(entry: dict) -> str:
    """Stable selector for *entry*, unique within this exact skill body."""
    return f"#{entry['number']}"


def _leaf_target(entry: dict) -> dict:
    """One retrievable navigation target, as the model sees it."""
    return {
        "selector": _selector_of(entry),
        "heading": entry["heading"][:_MAX_SECTION_HEADING_CHARS],
        "level": entry["level"],
        "chars": entry["end"] - entry["start"],
    }


def _group_span(count: int) -> int:
    """Entries per group so that at most _MAX_INCOMPLETE_SECTIONS groups fit.

    Always strictly smaller than *count* when *count* overflows the cap, so
    expanding a group makes progress and the recursion terminates.
    """
    span = _MAX_INCOMPLETE_SECTIONS
    while span * _MAX_INCOMPLETE_SECTIONS < count:
        span *= _MAX_INCOMPLETE_SECTIONS
    return span


def _chunk_groups(items: list, span: int, selector_of, covers_of) -> list[dict]:
    """Split *items* into bounded groups, each with its own selector."""
    groups = []
    for at in range(0, len(items), span):
        chunk = items[at : at + span]
        groups.append(
            {
                "selector": selector_of(chunk[0], chunk[-1]),
                "covers": covers_of(chunk[0], chunk[-1], at, len(chunk)),
            }
        )
    return groups


def _entry_range_groups(entries: list[dict], total: int, span: int | None = None) -> list[dict]:
    """Group selectors over a run of entries, expressed as ``#first-last``."""
    return _chunk_groups(
        entries,
        span or _group_span(len(entries)),
        lambda lo, hi: f"#{lo['number']}-{hi['number']}",
        lambda lo, hi, _at, n: (
            f"{n} entries, headings {lo['number']}-{hi['number']} of {total}"
            f" — starts at {lo['heading'][:60]!r}"
        ),
    )


def _entry_navigation(entries: list[dict], total: int) -> tuple[list, list]:
    """(leaf targets, group targets) for *entries* -- whichever stays bounded.

    Never both, and never neither while *entries* is non-empty: every entry is
    reachable either directly or by expanding exactly one returned group.
    """
    if len(entries) <= _MAX_INCOMPLETE_SECTIONS:
        return [_leaf_target(e) for e in entries], []
    return [], _entry_range_groups(entries, total)


def _part_navigation(entry: dict, numbered: list, total_parts: int) -> tuple[list, list]:
    """(leaf targets, group targets) for parts of *entry*'s own span.

    *numbered* is ``[(real part number, (start, end)), ...]`` -- the real number
    travels with each span so that expanding a part range keeps naming parts by
    their true position, not by their offset inside the page being expanded.
    """
    selector = _selector_of(entry)
    heading = entry["heading"][:_MAX_SECTION_HEADING_CHARS]
    if len(numbered) <= _MAX_INCOMPLETE_SECTIONS:
        return (
            [
                {
                    "selector": f"{selector}.part{n}",
                    "heading": f"part {n} of {total_parts} of {heading!r}",
                    "level": entry["level"],
                    "chars": hi - lo,
                }
                for n, (lo, hi) in numbered
            ],
            [],
        )
    return [], _chunk_groups(
        numbered,
        _group_span(len(numbered)),
        lambda lo, hi: f"{selector}.part{lo[0]}-{hi[0]}",
        lambda lo, hi, _at, n: (
            f"{n} parts, parts {lo[0]}-{hi[0]} of {total_parts} of {heading!r}"
        ),
    )


# Selector grammar. These are the ONLY shapes the model ever needs, and it
# never has to construct one -- every selector it can use was returned to it by
# a previous result. A heading whose literal text looks like a selector is
# still reachable through its own numeric selector, so nothing becomes
# unretrievable; the selector reading simply wins.
_SELECTOR_ONE_RE = re.compile(r"^#(\d+)$")
_SELECTOR_RANGE_RE = re.compile(r"^#(\d+)-(\d+)$")
_SELECTOR_PART_RE = re.compile(r"^#(\d+)\.part(\d+)$")
_SELECTOR_PART_RANGE_RE = re.compile(r"^#(\d+)\.part(\d+)-(\d+)$")


def _skill_section_navigation(body: str) -> tuple[list, list, int]:
    """Bounded, fully routed index of *body*: (leaves, groups, total entries)."""
    entries = _skill_entries(body)
    leaves, groups = _entry_navigation(entries, len(entries))
    return leaves, groups, len(entries)


def _set_navigation(payload: dict, notice: str, leaves: list, groups: list, total: int) -> None:
    """Answer with navigation only: no body, canonical marker, honest status.

    Every response that withholds content goes through here, so "content is
    absent" and "the incomplete marker is present" cannot come apart.
    """
    payload.pop("content", None)
    payload["load_status"] = "incomplete"
    payload["content_returned"] = False
    payload["notice"] = (
        f"{notice} "
        f"{_skill_incomplete_marker(str(payload.get('name') or ''), _payload_file(payload))}"
    )
    if leaves:
        payload["sections"] = leaves
    else:
        payload.pop("sections", None)
    if groups:
        payload["section_groups"] = groups
    else:
        payload.pop("section_groups", None)
    payload["sections_total"] = total


def _apply_section_selection(payload: dict, section: str) -> None:
    """Narrow *payload*'s content to the requested *section*, in place.

    Three kinds of request, one rule: content comes back only when the WHOLE of
    what was asked for fits. Otherwise the answer is navigation -- selectors
    that between them cover every character of the thing that did not fit --
    and no fragment of the body at all.

    * an exact heading, when it occurs exactly once;
    * ``#n`` / ``#n.partK`` -- a heading occurrence or one part of its own text;
    * ``#a-b`` / ``#n.partA-B`` -- an index page, navigation only.

    A miss, an ambiguous heading and an oversized section are all navigation
    answers rather than tool errors: the model's next move is another
    ``section=`` call either way.
    """
    wanted = str(section).strip()
    body = payload.get("content")
    if not isinstance(body, str):
        return
    payload["section"] = wanted
    entries = _skill_entries(body)
    total = len(entries)
    by_number = {e["number"]: e for e in entries}

    def index_answer(notice: str) -> None:
        """Nothing here answers to what was asked: whole index, no body.

        This is the ONLY path that reports section_found False. An ambiguous
        heading and an oversized section both exist -- they just cannot be
        returned as asked -- so they leave the key off rather than deny them.
        """
        leaves, groups = _entry_navigation(entries, total)
        _set_navigation(payload, notice, leaves, groups, total)
        payload["section_found"] = False

    # ── index pages: navigation only, never an instruction body ──────────
    match = _SELECTOR_RANGE_RE.match(wanted)
    if match:
        lo, hi = int(match.group(1)), int(match.group(2))
        chosen = [e for e in entries if lo <= e["number"] <= hi]
        if not chosen:
            return index_answer(
                f"Selector {wanted!r} covers no headings in this skill."
            )
        leaves, groups = _entry_navigation(chosen, total)
        return _set_navigation(
            payload,
            f"Headings {lo}-{hi} of {total} in this skill. This is an index, "
            f"not skill content — request an entry by its selector.",
            leaves,
            groups,
            total,
        )

    match = _SELECTOR_PART_RANGE_RE.match(wanted)
    if match:
        number, lo, hi = (int(g) for g in match.groups())
        entry = by_number.get(number)
        if entry is None:
            return index_answer(f"Selector {wanted!r} names no section here.")
        spans = _section_part_spans(body, entry["own_start"], entry["own_end"])
        numbered = [(n, s) for n, s in enumerate(spans, start=1) if lo <= n <= hi]
        if not numbered:
            return index_answer(f"Selector {wanted!r} covers no parts here.")
        leaves, groups = _part_navigation(entry, numbered, len(spans))
        return _set_navigation(
            payload,
            f"Parts {lo}-{hi} of {len(spans)} of section "
            f"{entry['heading']!r}. This is an index, not skill content.",
            leaves,
            groups,
            total,
        )

    # ── one part of a section's own text ─────────────────────────────────
    match = _SELECTOR_PART_RE.match(wanted)
    if match:
        number, wanted_part = int(match.group(1)), int(match.group(2))
        entry = by_number.get(number)
        if entry is None:
            return index_answer(f"Selector {wanted!r} names no section here.")
        spans = _section_part_spans(body, entry["own_start"], entry["own_end"])
        if not 1 <= wanted_part <= len(spans):
            return index_answer(
                f"Section {_selector_of(entry)} has {len(spans)} part(s); "
                f"{wanted!r} is out of range."
            )
        lo, hi = spans[wanted_part - 1]
        leaves, groups = _part_navigation(
            entry, list(enumerate(spans, start=1)), len(spans)
        )
        payload["section_found"] = True
        payload["content"] = body[lo:hi]
        payload["section_part"] = wanted_part
        payload["section_part_count"] = len(spans)
        payload["part_of"] = {
            "selector": _selector_of(entry),
            "heading": entry["heading"][:_MAX_SECTION_HEADING_CHARS],
            "level": entry["level"],
        }
        payload["notice"] = (
            f"This is part {wanted_part} of {len(spans)} of the text of section "
            f"{entry['heading']!r} ({_selector_of(entry)}). This part is "
            f"complete; the section is NOT. The remaining parts are listed in "
            f'"sections".' + _continuation_hint(payload)
        )
        payload["sections"] = leaves
        if groups:
            payload["section_groups"] = groups
        payload["sections_total"] = total
        return

    # ── a single heading occurrence ──────────────────────────────────────
    entry = None
    match = _SELECTOR_ONE_RE.match(wanted)
    if match:
        entry = by_number.get(int(match.group(1)))
        if entry is None:
            return index_answer(f"Selector {wanted!r} names no section here.")
    else:
        hits = [e for e in entries if e["heading"].strip() == wanted]
        if not hits:
            return index_answer(
                f"Section {wanted!r} was not found in this skill. The headings "
                f'that do exist are listed in "sections"; request one of those '
                f"exactly, or by its selector."
            )
        if len(hits) > 1:
            leaves, groups = _entry_navigation(hits, total)
            payload["section_ambiguous"] = True
            return _set_navigation(
                payload,
                f"Heading {wanted!r} occurs {len(hits)} times in this skill, so "
                f"an exact-heading request is ambiguous and no body was "
                f'returned. Each occurrence has its own selector in "sections" '
                f"— request the one you want.",
                leaves,
                groups,
                total,
            )
        entry = hits[0]

    # Both arms above either returned or bound a real entry.
    size = entry["end"] - entry["start"]
    if size <= _MAX_SECTION_CHARS:
        leaves, groups = _entry_navigation(entries, total)
        payload["section_found"] = True
        payload["section_selector"] = _selector_of(entry)
        payload["content"] = body[entry["start"] : entry["end"]]
        payload["notice"] = (
            f"Section {entry['heading']!r} ({_selector_of(entry)}) is returned "
            f"in full. It is one entry of {total} in this skill; the rest of "
            f'the skill is NOT loaded. The others are listed in "sections".'
            + _continuation_hint(payload)
        )
        payload["sections"] = leaves
        if groups:
            payload["section_groups"] = groups
        payload["sections_total"] = total
        return

    # Too large to return whole. NO fragment: the own text becomes parts and
    # the child headings become their own targets, and between them they cover
    # every character of the section.
    own_parts = _section_part_spans(body, entry["own_start"], entry["own_end"])
    part_leaves, part_groups = _part_navigation(
        entry, list(enumerate(own_parts, start=1)), len(own_parts)
    )
    children = [e for e in entries if e["parent"] == entry["number"]]
    child_leaves, child_groups = _entry_navigation(children, total)
    payload["section_selector"] = _selector_of(entry)
    payload["section_chars"] = size
    _set_navigation(
        payload,
        f"Section {entry['heading']!r} ({_selector_of(entry)}) is {size:,} "
        f"characters, larger than the {_MAX_SECTION_CHARS:,}-character result "
        f"ceiling, so NO part of it was returned. Its own text is available as "
        f'parts and its sub-sections as their own selectors, listed in '
        f'"sections"; together they cover the whole section.',
        part_leaves + child_leaves,
        part_groups + child_groups,
        total,
    )


def _skill_view_incomplete_result(content: str, *, tool_name: str = "skill_view") -> str | None:
    """Replacement receipt for a skill_view result whose body was NOT delivered.

    Called by tools/tool_result_storage.py only after that module has already
    decided, on its own, to persist this result -- i.e. the body is gone from
    the model's view no matter what we return. Emits an index-only receipt:
    typed marker, honest status, bounded heading index, availability metadata,
    and no fragment of the skill body.

    Returns None (meaning "use the generic receipt") for anything that is not a
    successful skill_view payload carrying content. Never raises.
    """
    try:
        payload, end = json.JSONDecoder().raw_decode(content)
    except ValueError:
        # Not a JSON payload at all (already-truncated text, a synthetic error
        # string, ...). The generic receipt is the honest answer.
        return None
    if not isinstance(payload, dict) or not payload.get("success"):
        return None
    body = payload.get("content")
    if not isinstance(body, str):
        # Nothing was going to be delivered anyway (dedup stub, error shape).
        return None

    # run_agent.append_toolguard_guidance can append text AFTER the JSON before
    # persistence. That guidance is a live instruction to the model; carry it.
    trailing = content[end:]

    # The dedup record for this exact payload is a promise the delivery just
    # broke. Revoke it BEFORE returning, so a retry re-loads for real instead
    # of being told the earlier result "is still current and complete".
    _revoke_skill_view_delivery(content[:end])

    name = str(payload.get("name") or "")
    leaves, groups, total = _skill_section_navigation(body)
    receipt = {
        "success": True,
        "name": name,
        "load_status": "incomplete",
        "content_returned": False,
        "notice": _skill_incomplete_marker(name, _payload_file(payload)),
        "sections_total": total,
    }
    if leaves:
        receipt["sections"] = leaves
    if groups:
        receipt["section_groups"] = groups
    for key in _INCOMPLETE_RECEIPT_METADATA_KEYS:
        if key in payload and payload[key] is not None:
            receipt[key] = payload[key]

    rendered = json.dumps(receipt, ensure_ascii=False)
    for key in _INCOMPLETE_RECEIPT_DROPPABLE_KEYS:
        if len(rendered) <= _MAX_INCOMPLETE_RECEIPT_CHARS:
            break
        if receipt.pop(key, None) is not None:
            rendered = json.dumps(receipt, ensure_ascii=False)

    # Still over the cap: coarsen the index into fewer, wider groups rather
    # than dropping entries off the end. A dropped entry is a heading the model
    # is told exists and given no way to reach; a wider group is one extra hop.
    entries = _skill_entries(body)
    span = None
    while len(rendered) > _MAX_INCOMPLETE_RECEIPT_CHARS and entries:
        span = _group_span(total) if span is None else span * _MAX_INCOMPLETE_SECTIONS
        receipt.pop("sections", None)
        receipt["section_groups"] = _entry_range_groups(entries, total, span)
        rendered = json.dumps(receipt, ensure_ascii=False)
        if len(receipt["section_groups"]) <= 1:
            break  # one group already covers everything; nothing left to merge

    return rendered + trailing


register_oversized_result_formatter("skill_view", _skill_view_incomplete_result)


def _skill_view_with_bump(args, **kw):
    """Invoke skill_view, then bump view_count on success. Best-effort: a
    telemetry failure never breaks the tool call."""
    name = args.get("name", "")
    task_id = kw.get("task_id")
    # ── Repeat-view dedup ────────────────────────────────────────────
    # Mirrors read_file's unchanged-stub: when this session already
    # loaded the SAME skill file and it hasn't changed on disk, return a
    # short stub instead of re-sending the full content (production
    # mining: ~286k tokens of verbatim repeat skill_view content in one
    # 400k-message window). The stub only ever replaces content that is
    # already fully present earlier in this conversation, so the
    # "skills must be loaded fully" rule is preserved — and the cache is
    # cleared on context compression (same hook as read_file's dedup)
    # so a post-compression re-view returns full content again.
    # A section request asks for content the earlier result may not have
    # carried, so it is never answered from the unchanged-stub.
    section = args.get("section")
    if not section:
        stub = _check_skill_view_dedup(task_id, name, args.get("file_path"))
        if stub is not None:
            return stub
    result = skill_view(
        name, file_path=args.get("file_path"), task_id=task_id, section=section
    )
    try:
        parsed = json.loads(result)
        if isinstance(parsed, dict) and parsed.get("success"):
            recorded = None
            if not section:
                recorded = _record_skill_view(task_id, name, args.get("file_path"), parsed)
            # Use the resolved skill name from the payload when present —
            # qualified forms ("plugin:skill") return with the canonical name.
            resolved = parsed.get("name") or name
            snapshot = None
            if resolved:
                from tools.skill_usage import bump_use, bump_view, get_record, use_snapshot
                # Both the dedup record above and the use bump below are claims
                # about a body the model has not received yet. Capture what the
                # use bump is about to overwrite so the persistence boundary can
                # put it back if the body never arrives.
                snapshot = use_snapshot(get_record(str(resolved)))
                bump_view(str(resolved))
                # A skill_view tool call is the agent actively loading the skill
                # to act on it — that counts as use, not just a browse/view.
                # Curator's stale timer keys off last_used_at (see agent/curator.py).
                bump_use(
                    str(resolved),
                    task_id=kw.get("task_id"),
                    session_id=kw.get("session_id"),
                )
            _note_skill_view_delivery(
                result, task_id, recorded, str(resolved or ""), snapshot
            )
    except Exception:
        pass
    return result


registry.register(
    name="skill_view",
    toolset="skills",
    schema=SKILL_VIEW_SCHEMA,
    handler=_skill_view_with_bump,
    check_fn=check_skills_requirements,
    emoji="📚",
)
