#!/usr/bin/env python3
"""
Skill Manager Tool -- Agent-Managed Skill Creation & Editing

Allows the agent to create, update, and delete skills, turning successful
approaches into reusable procedural knowledge. New skills are created in
~/.hermes/skills/. Existing skills (bundled, hub-installed, or user-created)
can be modified or deleted wherever they live.

Skills are the agent's procedural memory: they capture *how to do a specific
type of task* based on proven experience. General memory (MEMORY.md, USER.md) is
broad and declarative. Skills are narrow and actionable.

Actions:
  create     -- Create a new skill (SKILL.md + directory structure)
  edit       -- Replace the SKILL.md content of a user skill (full rewrite)
  patch      -- Targeted find-and-replace within SKILL.md or any supporting file
  delete     -- Remove a user skill entirely
  write_file -- Add/overwrite a supporting file (reference, template, script, asset)
  remove_file-- Remove a supporting file from a user skill

Directory layout for user skills:
    ~/.hermes/skills/
    ├── my-skill/
    │   ├── SKILL.md
    │   ├── references/
    │   ├── templates/
    │   ├── scripts/
    │   └── assets/
    └── category-name/
        └── another-skill/
            └── SKILL.md
"""

import json
import hashlib
import logging
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
import contextvars as _ctxvars
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import (
    get_default_hermes_root,
    get_hermes_home,
    display_hermes_home,
)
from utils import is_truthy_value
from hermes_cli.config import cfg_get
from agent.skill_utils import (
    extract_skill_description,
    is_skill_description_truncated_for_prompt,
    parse_frontmatter as _parse_frontmatter,
    SKILL_PROMPT_DESC_LIMIT,
)

logger = logging.getLogger(__name__)
_skill_mutation_thread_locks_guard = threading.Lock()
_skill_mutation_thread_locks: dict[str, threading.Lock] = {}


def _fsync_committed(fd: int, operation: str) -> None:
    """Best-effort durability barrier after an already-visible mutation.

    The namespace change has committed before this call.  Propagating a later
    fsync error as an ordinary failure incorrectly tells callers that nothing
    changed, so retain the truthful committed result and leave diagnostics.
    """
    try:
        os.fsync(fd)
    except OSError as exc:
        logger.warning("%s committed but directory fsync failed: %s", operation, exc)

_background_review_read_paths: "_ctxvars.ContextVar[frozenset[str]]" = _ctxvars.ContextVar(
    "background_review_read_paths", default=frozenset()
)


def mark_background_review_skill_read(path: Path) -> None:
    """Record that the active background-review fork has read a skill file.

    The autonomous review fork is allowed to evolve skills, but it must not
    patch or rewrite content it has only inferred from the transcript.  The
    skill_view tool calls this after returning file content to the model; write
    paths below require the corresponding target path to be present when the
    current origin is ``background_review``.
    """
    try:
        from tools.skill_provenance import is_background_review
        if not is_background_review():
            return
    except Exception:
        return

    try:
        resolved = str(path.resolve())
    except Exception:
        resolved = str(path)
    current = set(_background_review_read_paths.get())
    current.add(resolved)
    _background_review_read_paths.set(frozenset(current))


def _background_review_has_read(path: Path) -> bool:
    try:
        resolved = str(path.resolve())
    except Exception:
        resolved = str(path)
    return resolved in _background_review_read_paths.get()


def _reset_background_review_read_marks() -> None:
    """Test helper: clear read-before-write marks for the current context."""
    _background_review_read_paths.set(frozenset())

# Import security scanner — external hub installs always get scanned;
# agent-created skills only get scanned when skills.guard_agent_created is on.
try:
    from tools.skills_guard import scan_skill, should_allow_install, format_scan_report
    _GUARD_AVAILABLE = True
except ImportError:
    _GUARD_AVAILABLE = False


def _guard_agent_created_enabled() -> bool:
    """Read skills.guard_agent_created from config (default False).

    Off by default because the agent can already execute the same code
    paths via terminal() with no gate, so the scan adds friction without
    meaningful security.  Users who want belt-and-suspenders can turn it
    on via `hermes config set skills.guard_agent_created true`.
    """
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        return is_truthy_value(
            cfg_get(cfg, "skills", "guard_agent_created"),
            default=False,
        )
    except Exception:
        return False


def _agent_created_security_scan_enabled() -> bool:
    """Whether agent-created mutations must run the security scanner.

    Keep this predicate as the single fast-path contract for both normal and
    descriptor-anchored scans.  In particular, the anchored path must not
    copy an entire skill tree merely to reach the same disabled guard inside
    :func:`_security_scan_skill`.
    """
    return _GUARD_AVAILABLE and _guard_agent_created_enabled()


def _security_scan_skill(skill_dir: Path) -> Optional[str]:
    """Scan a skill directory after write. Returns error string if blocked, else None.

    No-op when skills.guard_agent_created is disabled (the default).
    """
    if not _agent_created_security_scan_enabled():
        return None
    try:
        result = scan_skill(skill_dir, source="agent-created")
        allowed, reason = should_allow_install(result)
        if allowed is False:
            report = format_scan_report(result)
            return f"Security scan blocked this skill ({reason}):\n{report}"
        if allowed is None:
            # "ask" verdict — for agent-created skills this means dangerous
            # findings were detected.  Surface as an error so the agent can
            # retry with the flagged content removed.
            report = format_scan_report(result)
            logger.warning("Agent-created skill blocked (dangerous findings): %s", reason)
            return f"Security scan blocked this skill ({reason}):\n{report}"
    except Exception as e:
        logger.warning("Security scan failed for %s: %s", skill_dir, e, exc_info=True)
    return None

import yaml


# All skills live in ~/.hermes/skills/ (single source of truth)
HERMES_HOME = get_hermes_home()
SKILLS_DIR = HERMES_HOME / "skills"
_SKILLS_DIR_AT_IMPORT = SKILLS_DIR


def _skills_dir() -> Path:
    """Return the active profile's skills directory at call time.

    Long-lived multi-profile runtimes (Dashboard/TUI/Desktop backend, cron,
    kanban workers) import this module once under the launch HERMES_HOME and
    later bind a different profile per session (#40677). Honor an explicitly
    patched module-level ``SKILLS_DIR`` (tests), otherwise resolve from the
    live profile-scoped HERMES_HOME on every call.
    """
    configured = Path(SKILLS_DIR)
    if configured != _SKILLS_DIR_AT_IMPORT:
        return configured
    return get_hermes_home() / "skills"

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024


def _containing_skills_root(skill_path: Path) -> Path:
    """Return the skills root directory (local or external_dirs entry) that
    contains ``skill_path``.  Falls back to the local ``SKILLS_DIR`` if no
    match is found (defensive — callers should have located the skill via
    ``_find_skill`` first).
    """
    from agent.skill_utils import get_all_skills_dirs

    try:
        resolved = skill_path.resolve()
    except OSError:
        resolved = skill_path

    for root in get_all_skills_dirs():
        try:
            resolved.relative_to(root.resolve())
            return root
        except (ValueError, OSError):
            continue
    return _skills_dir()


def _is_path_redirect(path: Path) -> bool:
    """True when ``path`` is a symlink or (on Windows) a directory junction.

    Either form lets a poisoned skills tree redirect a subsequent
    ``shutil.rmtree`` to content outside the skills root. ``is_junction``
    only exists on Python 3.12+ Windows; gate with ``hasattr``.
    """
    try:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
    except OSError:
        return False


def _validate_delete_target(skill_dir: Path) -> Optional[str]:
    """Last-line guard before ``shutil.rmtree(skill_dir)`` in ``_delete_skill``.

    ``_find_skill`` already restricts ``skill_dir`` to a real ``SKILL.md``
    parent discovered by walking the skills roots, so the agent cannot inject
    an arbitrary path the way Kilo Code's HTTP endpoint could (their issue
    #11227: a built-in-skill sentinel resolved to the server cwd and a
    recursive delete wiped the user's entire working directory). This is the
    matching defense-in-depth for our agent-facing ``skill_manage`` delete
    path: even if discovery or a poisoned tree hands us a bad directory, never
    recursively delete

      1. a path that is not strictly *inside* one of the known skills roots,
      2. a skills root itself (would wipe every installed skill), or
      3. a directory reached via a symlink / junction (``rmtree`` would follow
         it into content outside the skills tree).

    Returns an error string to refuse on, or ``None`` when the delete is safe.
    """
    from agent.skill_utils import get_all_skills_dirs

    # (3) Reject symlink/junction redirects on the skill directory itself.
    if _is_path_redirect(skill_dir):
        return (
            f"Refusing to delete '{skill_dir}': the skill directory is a "
            f"symlink/junction. Remove the link target manually if intended."
        )

    try:
        resolved = skill_dir.resolve()
    except OSError as exc:
        return f"Refusing to delete '{skill_dir}': could not resolve path ({exc})."

    roots = []
    for root in get_all_skills_dirs():
        try:
            roots.append(root.resolve())
        except OSError:
            continue

    for root in roots:
        # (2) Never rmtree a skills root itself.
        if resolved == root:
            return (
                f"Refusing to delete '{skill_dir}': resolves to the skills root "
                f"itself, which would remove every installed skill."
            )
        # (1) Must be strictly inside a known root.
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            continue
        if rel.parts:  # at least one component below the root
            return None

    return (
        f"Refusing to delete '{skill_dir}': path does not resolve inside any "
        f"known skills root."
    )


def _pinned_guard(name: str) -> Optional[str]:
    """Return a refusal message if *name* is pinned, else None.

    Pin protects a skill from **deletion** — both the curator's auto-archive
    passes and the agent's ``skill_manage(action="delete")`` tool call. The
    agent can still patch/edit pinned skills; pin only guards against
    irrecoverable loss, not against content evolution.

    Best-effort: if the sidecar is unreadable we let the delete through
    rather than block on a broken telemetry file.
    """
    try:
        from tools import skill_usage
        rec = skill_usage.get_record(name)
        if rec.get("pinned"):
            return (
                f"Skill '{name}' is pinned and cannot be deleted by "
                f"skill_manage. Ask the user to run "
                f"`hermes curator unpin {name}` if they want to delete it. "
                f"Patches and edits are allowed on pinned skills; only "
                f"deletion is blocked."
            )
    except Exception:
        logger.debug("pinned-guard lookup failed for %s", name, exc_info=True)
    return None


def _background_review_write_guard(
    name: str,
    skill_dir: Path,
    action: str,
) -> Optional[Dict[str, Any]]:
    """Refuse autonomous curator writes to externally owned skills.

    Foreground agents may still perform user-directed edits to external,
    bundled, or hub-installed skills. The background review fork is different:
    it is autonomous lifecycle maintenance, so its write surface is restricted
    to local curator-owned sediment.
    """
    try:
        from tools.skill_provenance import is_background_review
        if not is_background_review():
            return None
    except Exception:
        return None

    # Pin must be respected by autonomous maintenance. The curator already
    # skips pinned skills from every auto-transition; the background review
    # fork is the same kind of autonomous, no-user-present actor, so it must
    # not write to a pinned skill either (issue #25839). This is stricter than
    # the foreground ``_pinned_guard`` (which only blocks deletion) precisely
    # because there is no user in the loop to consent to an edit here.
    try:
        from tools import skill_usage
        if skill_usage.get_record(name).get("pinned"):
            return {
                "success": False,
                "error": (
                    f"Refusing background curator {action} for pinned skill "
                    f"'{name}': pinned skills are off-limits to autonomous "
                    "maintenance. Ask the user to run "
                    f"`hermes curator unpin {name}` if they want it changed."
                ),
            }
    except Exception:
        logger.debug("pinned skill guard lookup failed for %s", name, exc_info=True)

    try:
        from agent.skill_utils import is_external_skill_path
        if is_external_skill_path(skill_dir):
            return {
                "success": False,
                "error": (
                    f"Refusing background curator {action} for skill '{name}': "
                    "the skill lives in skills.external_dirs, which are "
                    "externally owned and read-only to autonomous curation."
                ),
            }
    except Exception:
        logger.debug("external skill guard lookup failed for %s", name, exc_info=True)

    try:
        from tools import skill_usage
        if skill_usage.is_protected_builtin(name):
            return {
                "success": False,
                "error": (
                    f"Refusing background curator {action} for protected "
                    f"built-in skill '{name}'."
                ),
            }
        if skill_usage.is_hub_installed(name):
            return {
                "success": False,
                "error": (
                    f"Refusing background curator {action} for hub-installed "
                    f"skill '{name}'."
                ),
            }
        if skill_usage.is_bundled(name):
            return {
                "success": False,
                "error": (
                    f"Refusing background curator {action} for bundled "
                    f"skill '{name}'."
                ),
            }
        # Skills that are not curator-managed are off-limits to autonomous
        # curation. This prevents the LLM consolidation pass from mutating
        # skills the user owns (manually authored, URL-installed, or created by
        # a foreground `skill_manage(create)` at the user's request), which lack
        # the `created_by: "agent"` marker.
        #
        # A MISSING record and an explicit `created_by: null` must resolve
        # IDENTICALLY (issue #67140). Keying on `isinstance(usage_rec, dict)`
        # made the policy depend on the guard's own side effect: a local skill
        # with no telemetry record passed, the successful write called
        # bump_patch() which created a `created_by: null` record, and the very
        # same write was refused from then on. "Allowed exactly once" is not a
        # policy — it is a race with our own bookkeeping. Fail closed for both
        # shapes; `hermes curator adopt <name>` is the supported way in.
        usage_data = skill_usage.load_usage()
        usage_rec = usage_data.get(name)
        if not skill_usage._is_curator_managed_record(usage_rec):
            if isinstance(usage_rec, dict):
                _detail = f"created_by={usage_rec.get('created_by')!r}"
            else:
                _detail = "no usage record"
            return {
                "success": False,
                "error": (
                    f"Refusing background curator {action} for skill "
                    f"'{name}': the skill is not curator-managed ({_detail}). "
                    "User-owned skills are off-limits to autonomous curation. "
                    f"Run `hermes curator adopt {name}` to opt it in."
                ),
            }
    except Exception:
        logger.warning("owned skill guard lookup failed for %s", name, exc_info=True)
        return {
            "success": False,
            "error": (
                f"Refusing background curator {action} for skill '{name}': "
                "agent ownership could not be verified because the provenance "
                "record is unavailable or unreadable."
            ),
        }
    return None


def _background_review_read_before_write_guard(
    name: str,
    target: Path,
    action: str,
    file_label: str,
) -> Optional[Dict[str, Any]]:
    """Require review forks to load the exact target before mutating it."""
    try:
        from tools.skill_provenance import is_background_review
        if not is_background_review():
            return None
    except Exception:
        return None

    if _background_review_has_read(target):
        return None

    return {
        "success": False,
        "error": (
            f"Refusing background curator {action} for skill '{name}': "
            f"the current {file_label} content has not been loaded in this "
            "review turn. Call skill_view(name) for SKILL.md, or "
            "skill_view(name, file_path=...) for a supporting file, then "
            "retry the write using the content just returned."
        ),
        "_read_before_write_required": True,
    }


def _background_review_preflight(action: str, name: str) -> Optional[Dict[str, Any]]:
    if action not in {"edit", "patch", "delete", "write_file", "remove_file"}:
        return None
    existing = _find_skill(name)
    lookup_error = _skill_lookup_error(existing)
    if lookup_error:
        return lookup_error
    if not existing:
        return None
    return _background_review_write_guard(name, existing["path"], action)


def _curator_consolidation_delete_guard(
    name: str, absorbed_into: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Fail closed on unverified deletes during the curator consolidation pass.

    The curator's forked review agent (``is_background_review()``) runs the
    LLM umbrella-building pass. Its only legitimate ``skill_manage(delete)`` is
    a *verified consolidation*: the skill's content was absorbed into an
    umbrella, declared via ``absorbed_into=<umbrella>`` where the umbrella
    exists on disk (validated separately in ``_delete_skill``).

    A delete with no forwarding target — ``absorbed_into`` omitted (``None``)
    or empty (``""``) — is the fail-open behavior reported in #29912: the
    consolidation pass archived whole clusters of active skills with zero
    verified consolidations (``consolidated_this_run == 0``), leaving active
    automations pointing at names that no longer resolve. The deterministic
    inactivity prune is the only legitimate prune path, and it archives via
    ``skill_usage.archive_skill()`` directly without ever calling
    ``skill_manage`` — so a bare prune reaching here can only be the LLM pass
    pruning without consolidation evidence. Refuse it; keep the skill active.

    Returns an error dict to abort the delete, or ``None`` when the delete is
    allowed to proceed (not the curator pass, or a declared consolidation).
    """
    try:
        from tools.skill_provenance import is_background_review
        if not is_background_review():
            return None
    except Exception:
        return None

    declared = isinstance(absorbed_into, str) and absorbed_into.strip()
    if declared:
        return None

    return {
        "success": False,
        "error": (
            f"Refusing background curator delete of skill '{name}': the "
            "consolidation pass may only archive a skill it has absorbed into "
            "an umbrella. Pass absorbed_into=<umbrella> (the umbrella must "
            "already exist) to record a verified consolidation. Pruning a "
            "skill with no forwarding target is not permitted here — the "
            "deterministic inactivity prune handles staleness archival "
            "separately. Keeping '{name}' active.".format(name=name)
        ),
        "_fail_closed": True,
    }


MAX_SKILL_CONTENT_CHARS = 100_000   # ~36k tokens at 2.75 chars/token
MAX_SKILL_FILE_BYTES = 1_048_576    # 1 MiB per supporting file

# Characters allowed in skill names (filesystem-safe, URL-friendly)
VALID_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]*$')

# Subdirectories allowed for write_file/remove_file
ALLOWED_SUBDIRS = {"references", "templates", "scripts", "assets"}


# =============================================================================
# Validation helpers
# =============================================================================

def _validate_name(name: str) -> Optional[str]:
    """Validate a skill name. Returns error message or None if valid."""
    if not name:
        return "Skill name is required."
    if len(name) > MAX_NAME_LENGTH:
        return f"Skill name exceeds {MAX_NAME_LENGTH} characters."
    if not VALID_NAME_RE.match(name):
        return (
            f"Invalid skill name '{name}'. Use lowercase letters, numbers, "
            f"hyphens, dots, and underscores. Must start with a letter or digit."
        )
    return None


def _validate_category(category: Optional[str]) -> Optional[str]:
    """Validate an optional category name used as a single directory segment."""
    if category is None:
        return None
    if not isinstance(category, str):
        return "Category must be a string."

    category = category.strip()
    if not category:
        return None
    if "/" in category or "\\" in category:
        return (
            f"Invalid category '{category}'. Use lowercase letters, numbers, "
            "hyphens, dots, and underscores. Categories must be a single directory name."
        )
    if len(category) > MAX_NAME_LENGTH:
        return f"Category exceeds {MAX_NAME_LENGTH} characters."
    if not VALID_NAME_RE.match(category):
        return (
            f"Invalid category '{category}'. Use lowercase letters, numbers, "
            "hyphens, dots, and underscores. Categories must be a single directory name."
        )
    return None


def _normalize_skill_content(content: str) -> str:
    """Normalize the one encoding artifact accepted by the runtime parser."""
    return content.removeprefix("\ufeff")


def _validate_frontmatter(
    content: str,
    *,
    new_skill: bool = False,
    expected_name: Optional[str] = None,
) -> Optional[str]:
    """
    Validate that SKILL.md content has proper frontmatter with required fields.
    Returns error message or None if valid.

    When ``new_skill`` is True (create path only), the description must also
    fit the 60-char system-prompt budget (SKILL_PROMPT_DESC_LIMIT) so newly
    authored skills never lose routing signal to index truncation. Edit and
    patch paths deliberately skip this so existing over-limit skills remain
    maintainable while their descriptions are cleaned up.
    """
    if not content.strip():
        return "Content cannot be empty."

    # Tolerate exactly one leading UTF-8 BOM (Windows editors) before the
    # fence. The create path persists this normalized form so validation and
    # runtime parsing cannot disagree.
    content = _normalize_skill_content(content)

    opening_match = re.match(r"---[ \t]*\r?\n", content)
    if opening_match is None:
        return "SKILL.md must start with YAML frontmatter (---). See existing skills for format."

    remainder = content[opening_match.end():]
    end_match = re.search(r'\r?\n---[ \t]*(?:\r?\n|$)', remainder)
    if not end_match:
        return "SKILL.md frontmatter is not closed. Ensure you have a closing '---' line."

    yaml_content = remainder[:end_match.start()]

    try:
        parsed = yaml.safe_load(yaml_content)
    except yaml.YAMLError as e:
        return f"YAML frontmatter parse error: {e}"

    if not isinstance(parsed, dict):
        return "Frontmatter must be a YAML mapping (key: value pairs)."

    if "name" not in parsed:
        return "Frontmatter must include 'name' field."
    if "description" not in parsed:
        return "Frontmatter must include 'description' field."
    frontmatter_name = parsed["name"]
    if not isinstance(frontmatter_name, str) or not frontmatter_name.strip():
        return "Frontmatter 'name' must be a non-empty string."
    if new_skill:
        name_error = _validate_name(frontmatter_name)
        if name_error:
            return f"Invalid frontmatter 'name': {name_error}"
    if expected_name is not None and frontmatter_name != expected_name:
        return (
            f"Frontmatter name '{frontmatter_name}' must match the requested "
            f"skill name '{expected_name}'."
        )

    desc_value = parsed["description"]
    if not isinstance(desc_value, str) or not desc_value.strip():
        return "Frontmatter 'description' must be a non-empty string."
    desc = desc_value
    if len(desc) > MAX_DESCRIPTION_LENGTH:
        return f"Description exceeds {MAX_DESCRIPTION_LENGTH} characters."
    if new_skill and len(desc.strip().strip("'\"")) > SKILL_PROMPT_DESC_LIMIT:
        return (
            f"Description is {len(desc.strip())} chars — new skills must fit the "
            f"{SKILL_PROMPT_DESC_LIMIT}-char system-prompt budget (one sentence, "
            f"trigger first, ends with a period). The skill index truncates "
            f"longer descriptions to {SKILL_PROMPT_DESC_LIMIT - 3} chars + '...', "
            f"destroying the routing signal. Move detail into the skill body."
        )

    body = remainder[end_match.end():].strip()
    if not body:
        return "SKILL.md must have content after the frontmatter (instructions, procedures, etc.)."

    return None


def _validate_content_size(content: str, label: str = "SKILL.md") -> Optional[str]:
    """Check that content doesn't exceed the character limit for agent writes.

    Returns an error message or None if within bounds.
    """
    if len(content) > MAX_SKILL_CONTENT_CHARS:
        return (
            f"{label} content is {len(content):,} characters "
            f"(limit: {MAX_SKILL_CONTENT_CHARS:,}). "
            f"Consider splitting into a smaller SKILL.md with supporting files "
            f"in references/ or templates/."
        )
    return None


def _resolve_skill_dir(name: str, category: str = None) -> Path:
    """Build the directory path for a new skill, optionally under a category."""
    if category:
        return _skills_dir() / category / name
    return _skills_dir() / name


def _cleanup_created_skill_dir(
    skill_dir: Path,
    *,
    skill_md: Optional[Path] = None,
    created_category: Optional[Path] = None,
) -> None:
    """Remove only artifacts owned by the current create attempt."""
    if skill_md is not None:
        try:
            skill_md.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        skill_dir.rmdir()
    except OSError:
        # Preserve any pre-existing or concurrently-added files.
        pass
    if created_category is not None:
        try:
            created_category.rmdir()
        except OSError:
            pass


def _directory_entry_matches_fd(parent_fd: Any, name: str, child_fd: Any) -> bool:
    """Return whether ``parent_fd/name`` is the directory held by ``child_fd``."""
    if os.name == "nt":
        try:
            return (
                parent_fd.entry_identity(name, directory=True)
                == child_fd.identity
            )
        except OSError:
            return False
    try:
        entry_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        held_stat = os.fstat(child_fd)
    except OSError:
        return False
    return (
        stat.S_ISDIR(entry_stat.st_mode)
        and entry_stat.st_dev == held_stat.st_dev
        and entry_stat.st_ino == held_stat.st_ino
    )


def _secure_directory_create_supported() -> bool:
    """Whether this runtime supports no-follow, dir-fd-relative creation."""
    if os.name == "nt":
        from tools.nt_secure_fs_optional import is_available

        return is_available()
    return (
        os.name == "posix"
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
        and os.rmdir in os.supports_dir_fd
    )


@contextmanager
def _skill_mutation_lock(name: str):
    """Serialize one skill's complete mutation transaction.

    The in-process lock covers threads because ``flock`` is process-scoped on
    some platforms.  The hashed lock file covers other Hermes processes while
    avoiding user-controlled skill names in lock paths.  Callers must hold this
    from lookup/read through write, scan, and any rollback.
    """
    lock_key = hashlib.sha256(name.encode("utf-8")).hexdigest()
    with _skill_mutation_thread_locks_guard:
        thread_lock = _skill_mutation_thread_locks.setdefault(
            lock_key, threading.Lock()
        )

    configured = Path(SKILLS_DIR)
    lock_root = (
        configured.parent
        if configured != _SKILLS_DIR_AT_IMPORT
        else get_default_hermes_root()
    )
    lock_dir = lock_root / ".hermes-skill-mutation-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{lock_key}.lock"
    with thread_lock:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        locked = False
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
            elif os.name == "nt":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                    os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                raise OSError(
                    "cross-process skill mutation locking is unavailable"
                )
            locked = True
            yield
        finally:
            try:
                if locked and os.name == "posix":
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
                elif locked and os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            finally:
                os.close(fd)


def _physical_skill_lock_name(existing: Dict[str, Any]) -> str:
    """Return a process-stable lock key for one already-resolved skill.

    A skill can be addressed by both its directory basename and its
    frontmatter ``name``.  Request-name locks alone therefore do not protect
    the physical directory from transactions started through different
    aliases.  Directory identity gives every alias the same second lock
    without trusting either user-controlled name. Do not include the path:
    archive and restore rename the same directory, and a path-derived key
    would split its lock while lifecycle metadata is being reconciled.
    """
    directory_identity = existing.get("_dir_identity")
    if not isinstance(directory_identity, tuple) or len(directory_identity) != 2:
        raise OSError("resolved skill identity is unavailable")
    device, inode = directory_identity
    return f"physical\0{device}:{inode}"


def _revalidate_locked_skill_alias(
    name: str,
    existing: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Fail closed if an alias stopped naming the locked physical skill."""
    try:
        with _open_existing_skill_directory(existing) as (skill_fd, _resolved):
            if existing["path"].name == name:
                return None
            frontmatter, _ = _parse_frontmatter(
                _read_canonical_skill_md(skill_fd)
            )
            if frontmatter.get("name") == name:
                return None
    except Exception as exc:
        return {
            "success": False,
            "error": f"Skill target changed while reserving mutation: {exc}",
        }
    return {
        "success": False,
        "error": (
            f"Skill alias '{name}' changed while reserving mutation; "
            "refusing to mutate a stale target."
        ),
    }


@contextmanager
def _existing_skill_mutation_lock(name: str):
    """Lock an existing skill by request alias, then by physical identity.

    Every caller takes locks in the same alias-then-physical order.  The alias
    lock preserves create/same-name exclusion, while the physical lock makes
    directory-name and frontmatter-name aliases share one complete
    transaction.  Identity is revalidated after waiting for the physical
    lock, before any caller reads or writes.
    """
    with _skill_mutation_lock(name):
        existing = _find_skill(name)
        lookup_error = _skill_lookup_error(existing)
        if lookup_error:
            yield None, lookup_error
            return
        if not existing:
            yield None, {
                "success": False,
                "error": _skill_not_found_error(name),
            }
            return

        with _skill_mutation_lock(_physical_skill_lock_name(existing)):
            revalidation_error = _revalidate_locked_skill_alias(name, existing)
            if revalidation_error:
                yield None, revalidation_error
                return
            yield existing, None


_ARCHIVE_SCAN_MAX_DIRECTORIES = 256
_ARCHIVE_SCAN_MAX_DEPTH = 4


def _archive_directory_alias_matches(directory_name: str, requested_name: str) -> bool:
    """Match only an exact archive basename or our collision suffix form."""
    if directory_name == requested_name:
        return True
    prefix = f"{requested_name}-"
    if not directory_name.startswith(prefix):
        return False
    suffix = directory_name[len(prefix):]
    if len(suffix) == 14 and suffix.isdigit():
        return True
    stamp, separator, counter = suffix.partition("-")
    return bool(separator and len(stamp) == 14 and stamp.isdigit() and counter.isdigit())


def _has_legacy_archive_timestamp_suffix(directory_name: str) -> bool:
    """Whether a basename is indistinguishable from the old collision form."""
    return bool(re.fullmatch(r".+-\d{14}(?:-\d+)?", directory_name))


def _find_archived_skill(
    name: str, skills_root: Optional[Path] = None
) -> Optional[Dict[str, Any]]:
    """Find one archived skill by canonical frontmatter name or exact alias.

    Recovery cannot trust a physical basename after an archive move.  Scan a
    bounded archive tree through no-follow directory descriptors and read the
    canonical metadata from that held directory.  Ambiguity is always refused.
    """
    if os.name == "nt":
        from tools.nt_secure_fs_optional import open_directory

        root_path = Path(skills_root or _skills_dir())
        try:
            root_handle = open_directory(
                root_path.resolve(strict=True), writable=False
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            return {
                "error": f"could not securely open skills directory: {exc}",
                "paths": [],
            }
        try:
            try:
                archive_handle = root_handle.open_dir(
                    ".archive", writable=False
                )
            except FileNotFoundError:
                return None
            matches: list[Dict[str, Any]] = []
            seen_directories = 0

            def scan(
                directory,
                relative: tuple[str, ...],
                depth: int,
            ) -> None:
                nonlocal seen_directories
                for entry in sorted(
                    directory.list_entries(),
                    key=lambda item: item.name.casefold(),
                ):
                    if not entry.is_dir or entry.is_reparse:
                        continue
                    seen_directories += 1
                    if seen_directories > _ARCHIVE_SCAN_MAX_DIRECTORIES:
                        raise OSError(
                            "archive scan exceeds the safe directory limit"
                        )
                    with directory.open_dir(
                        entry.name, writable=False
                    ) as child:
                        child_relative = relative + (entry.name,)
                        try:
                            content = _read_canonical_skill_md(child)
                        except FileNotFoundError:
                            content = None
                        if content is not None:
                            frontmatter, _ = _parse_frontmatter(content)
                            canonical_name = frontmatter.get("name")
                            if (
                                not isinstance(canonical_name, str)
                                or _validate_name(canonical_name) is not None
                            ):
                                raise OSError(
                                    "archived SKILL.md has no valid "
                                    "canonical name"
                                )
                            if (
                                canonical_name == name
                                or _archive_directory_alias_matches(
                                    entry.name, name
                                )
                            ):
                                matches.append(
                                    {
                                        "path": root_path
                                        / ".archive"
                                        / Path(*child_relative),
                                        "_resolved_path": child.final_path(),
                                        "_dir_identity": child.identity,
                                        "_archive_relative_parts": child_relative,
                                        "_canonical_name": canonical_name,
                                    }
                                )
                            continue
                        if depth >= _ARCHIVE_SCAN_MAX_DEPTH:
                            raise OSError(
                                "archive scan exceeds the safe nesting limit"
                            )
                        scan(child, child_relative, depth + 1)

            try:
                with archive_handle:
                    scan(archive_handle, (), 0)
            except (OSError, UnicodeError, yaml.YAMLError) as exc:
                return {
                    "error": (
                        "archive lookup is incomplete; refusing recovery: "
                        f"{exc}"
                    ),
                    "paths": [],
                }
            if not matches:
                return None
            if len(matches) > 1:
                paths = sorted(str(match["path"]) for match in matches)
                return {
                    "error": (
                        f"Archived skill alias '{name}' is ambiguous; it "
                        f"matches multiple directories: {', '.join(paths)}"
                    ),
                    "paths": paths,
                }
            return matches[0]
        finally:
            root_handle.close()

    if not _secure_directory_create_supported():
        return {"error": "secure archived skill lookup is unavailable on this platform", "paths": []}
    root_path = Path(skills_root or _skills_dir())
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        root_fd = os.open(root_path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return {"error": f"could not securely open skills directory: {exc}", "paths": []}
    try:
        try:
            archive_fd = os.open(".archive", flags, dir_fd=root_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            return {"error": f"could not securely open archive directory: {exc}", "paths": []}
        try:
            if not _directory_entry_matches_fd(root_fd, ".archive", archive_fd):
                return {"error": "archive directory changed during lookup", "paths": []}
            archive_path = root_path / ".archive"
            matches: list[Dict[str, Any]] = []
            seen_directories = 0

            def scan(directory_fd: int, relative: tuple[str, ...], depth: int) -> None:
                nonlocal seen_directories
                with os.scandir(directory_fd) as entries:
                    for entry in entries:
                        entry_stat = os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
                        if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISDIR(entry_stat.st_mode):
                            continue
                        seen_directories += 1
                        if seen_directories > _ARCHIVE_SCAN_MAX_DIRECTORIES:
                            raise OSError("archive scan exceeds the safe directory limit")
                        child_fd = os.open(entry.name, flags, dir_fd=directory_fd)
                        try:
                            if not _directory_entry_matches_fd(directory_fd, entry.name, child_fd):
                                raise OSError("archive directory changed during lookup")
                            child_relative = relative + (entry.name,)
                            try:
                                metadata_stat = os.stat("SKILL.md", dir_fd=child_fd, follow_symlinks=False)
                            except FileNotFoundError:
                                metadata_stat = None
                            if metadata_stat is not None and stat.S_ISREG(metadata_stat.st_mode):
                                frontmatter, _ = _parse_frontmatter(_read_canonical_skill_md(child_fd))
                                canonical_name = frontmatter.get("name")
                                if (
                                    not isinstance(canonical_name, str)
                                    or _validate_name(canonical_name) is not None
                                ):
                                    raise OSError("archived SKILL.md has no valid canonical name")
                                if canonical_name == name or _archive_directory_alias_matches(entry.name, name):
                                    child_stat = os.fstat(child_fd)
                                    matches.append({
                                        "path": archive_path.joinpath(*child_relative),
                                        "_resolved_path": archive_path.joinpath(*child_relative),
                                        "_dir_identity": (child_stat.st_dev, child_stat.st_ino),
                                        "_archive_relative_parts": child_relative,
                                        "_canonical_name": canonical_name,
                                    })
                                # Skill folders are leaves: do not treat resources as skills.
                                continue
                            if metadata_stat is not None:
                                raise OSError("archived canonical metadata is not a regular file")
                            if depth >= _ARCHIVE_SCAN_MAX_DEPTH:
                                raise OSError("archive scan exceeds the safe nesting limit")
                            scan(child_fd, child_relative, depth + 1)
                        finally:
                            os.close(child_fd)

            try:
                scan(archive_fd, (), 0)
            except (OSError, UnicodeError, yaml.YAMLError) as exc:
                return {"error": f"archive lookup is incomplete; refusing recovery: {exc}", "paths": []}
            if not matches:
                return None
            if len(matches) > 1:
                paths = sorted(str(match["path"]) for match in matches)
                return {"error": f"Archived skill alias '{name}' is ambiguous; it matches multiple directories: {', '.join(paths)}", "paths": paths}
            return matches[0]
        finally:
            os.close(archive_fd)
    finally:
        os.close(root_fd)


@contextmanager
def _open_archived_skill_directory(existing: Dict[str, Any], skills_root: Path):
    """Hold one archived skill using no-follow archive-relative descriptors."""
    relative = existing.get("_archive_relative_parts")
    identity = existing.get("_dir_identity")
    if not isinstance(relative, tuple) or not relative or not isinstance(identity, tuple):
        raise OSError("archived skill identity is unavailable")
    if os.name == "nt":
        from tools.nt_secure_fs_optional import open_directory

        root_handle = open_directory(
            Path(skills_root).resolve(strict=True), writable=True
        )
        archive_handle = parent_handle = source_handle = None
        try:
            archive_handle = root_handle.open_dir(
                ".archive", writable=True
            )
            parent_handle = archive_handle
            for component in relative[:-1]:
                child = parent_handle.open_dir(
                    component, writable=True
                )
                if parent_handle is not archive_handle:
                    parent_handle.close()
                parent_handle = child
            source_name = relative[-1]
            source_handle = parent_handle.open_dir(
                source_name, writable=True
            )
            if source_handle.identity != tuple(identity):
                raise OSError("archive source changed before mutation")
            yield (
                source_handle,
                parent_handle,
                root_handle,
                archive_handle,
                source_name,
            )
        finally:
            if source_handle is not None:
                source_handle.close()
            if (
                parent_handle is not None
                and parent_handle is not archive_handle
            ):
                parent_handle.close()
            if archive_handle is not None:
                archive_handle.close()
            root_handle.close()
        return

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(Path(skills_root), flags)
    archive_fd = parent_fd = source_fd = None
    try:
        archive_fd = os.open(".archive", flags, dir_fd=root_fd)
        if not _directory_entry_matches_fd(root_fd, ".archive", archive_fd):
            raise OSError("archive directory changed before mutation")
        parent_fd = archive_fd
        for component in relative[:-1]:
            child_fd = os.open(component, flags, dir_fd=parent_fd)
            if not _directory_entry_matches_fd(parent_fd, component, child_fd):
                os.close(child_fd)
                raise OSError("archive parent changed before mutation")
            if parent_fd != archive_fd:
                os.close(parent_fd)
            parent_fd = child_fd
        source_name = relative[-1]
        source_fd = os.open(source_name, flags, dir_fd=parent_fd)
        if not _directory_entry_matches_fd(parent_fd, source_name, source_fd):
            raise OSError("archive source changed before mutation")
        held_stat = os.fstat(source_fd)
        if (held_stat.st_dev, held_stat.st_ino) != identity:
            raise OSError("archive source changed before mutation")
        yield source_fd, parent_fd, root_fd, archive_fd, source_name
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if parent_fd is not None and parent_fd != archive_fd:
            os.close(parent_fd)
        if archive_fd is not None:
            os.close(archive_fd)
        os.close(root_fd)


def _revalidate_locked_archived_alias(name: str, existing: Dict[str, Any], skills_root: Path) -> Optional[Dict[str, Any]]:
    try:
        with _open_archived_skill_directory(existing, skills_root) as (source_fd, _parent_fd, _root_fd, _archive_fd, source_name):
            frontmatter, _ = _parse_frontmatter(_read_canonical_skill_md(source_fd))
            canonical_name = frontmatter.get("name")
            if canonical_name == existing.get("_canonical_name") and (
                canonical_name == name or _archive_directory_alias_matches(source_name, name)
            ):
                return None
    except Exception as exc:
        return {"success": False, "error": f"Archived skill target changed while reserving mutation: {exc}"}
    return {"success": False, "error": f"Archived skill alias '{name}' changed while reserving mutation; refusing a stale target."}


@contextmanager
def _archived_skill_mutation_lock(name: str, skills_root: Optional[Path] = None):
    """Lock an archived skill by canonical identity, then physical identity.

    An archived directory can be addressed by a historical physical basename.
    Resolve that alias before locking, then reserve its canonical frontmatter
    name so restore serializes with canonical create/restore calls.  Re-resolve
    after taking the canonical lock: the preliminary lookup is intentionally
    only a lock-key discovery step and must never authorize a mutation.
    """
    root = Path(skills_root or _skills_dir())
    preliminary = _find_archived_skill(name, root)
    preliminary_error = _skill_lookup_error(preliminary)
    if preliminary_error:
        yield None, preliminary_error
        return
    if not preliminary:
        yield None, {"success": False, "error": f"archived skill '{name}' not found"}
        return
    canonical_name = preliminary.get("_canonical_name")
    if not isinstance(canonical_name, str) or not canonical_name:
        yield None, {
            "success": False,
            "error": "archived skill has no valid canonical identity",
        }
        return

    # Every restore caller now takes the same canonical -> physical order.
    # This avoids the physical-alias -> canonical inversion that could deadlock
    # a physical-alias restore against a canonical-name restore/create.
    with _skill_mutation_lock(canonical_name):
        existing = _find_archived_skill(name, root)
        lookup_error = _skill_lookup_error(existing)
        if lookup_error:
            yield None, lookup_error
            return
        if not existing:
            yield None, {"success": False, "error": f"archived skill '{name}' not found"}
            return
        if existing.get("_canonical_name") != canonical_name:
            yield None, {
                "success": False,
                "error": (
                    "Archived skill canonical identity changed while reserving "
                    "mutation; refusing a stale target."
                ),
            }
            return
        with _skill_mutation_lock(_physical_skill_lock_name(existing)):
            revalidation_error = _revalidate_locked_archived_alias(name, existing, root)
            if revalidation_error:
                yield None, revalidation_error
                return
            yield existing, None


def _windows_final_path(handle: int) -> str:
    """Return a normalized final path for a Windows directory handle."""
    import ctypes
    from ctypes import wintypes

    get_final_path = ctypes.windll.kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    get_final_path.restype = wintypes.DWORD
    size = get_final_path(handle, None, 0, 0)
    if not size:
        raise ctypes.WinError()
    buffer = ctypes.create_unicode_buffer(size + 1)
    written = get_final_path(handle, buffer, len(buffer), 0)
    if not written or written >= len(buffer):
        raise ctypes.WinError()
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return os.path.normcase(os.path.abspath(value))


def _open_windows_directory_guard(path: Path, expected: Path) -> int:
    """Open a non-reparse Windows directory while denying rename/delete."""
    import ctypes
    from ctypes import wintypes

    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    get_attributes = ctypes.windll.kernel32.GetFileAttributesW
    get_attributes.argtypes = [wintypes.LPCWSTR]
    get_attributes.restype = wintypes.DWORD
    close_handle = ctypes.windll.kernel32.CloseHandle

    file_share_read = 0x00000001
    file_share_write = 0x00000002
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    file_flag_open_reparse_point = 0x00200000
    file_attribute_reparse_point = 0x00000400
    invalid_handle = wintypes.HANDLE(-1).value

    attrs = get_attributes(str(path))
    if attrs == 0xFFFFFFFF or attrs & file_attribute_reparse_point:
        raise OSError(f"Refusing redirected directory path: {path}")
    handle = create_file(
        str(path),
        0,
        file_share_read | file_share_write,
        None,
        open_existing,
        file_flag_backup_semantics | file_flag_open_reparse_point,
        None,
    )
    if handle == invalid_handle:
        raise ctypes.WinError()
    try:
        if _windows_handle_is_reparse_point(handle):
            raise OSError(f"Refusing redirected directory path: {path}")
        if _windows_final_path(handle) != os.path.normcase(
            os.path.abspath(str(expected))
        ):
            raise OSError(f"Directory path changed during creation: {path}")
    except Exception:
        close_handle(handle)
        raise
    return handle


def _windows_handle_is_reparse_point(handle: int) -> bool:
    """Validate reparse metadata on the object opened by ``CreateFileW``.

    The pre-open ``GetFileAttributesW`` check is only an early rejection: a
    junction can be swapped in before ``CreateFileW``.  Querying
    ``FileAttributeTagInfo`` on the returned handle closes that race.
    """
    import ctypes
    from ctypes import wintypes

    class FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", wintypes.DWORD),
            ("ReparseTag", wintypes.DWORD),
        ]

    get_information = ctypes.windll.kernel32.GetFileInformationByHandleEx
    get_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    get_information.restype = wintypes.BOOL
    info = FILE_ATTRIBUTE_TAG_INFO()
    file_attribute_tag_info = 9
    if not get_information(
        handle,
        file_attribute_tag_info,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        raise ctypes.WinError()
    return bool(info.FileAttributes & 0x00000400 or info.ReparseTag)


@contextmanager
def _open_existing_skill_directory(
    existing: Dict[str, Any],
):
    """Hold the exact discovered skill directory for a canonical mutation."""
    if os.name == "nt":
        if not _secure_directory_create_supported():
            raise OSError(
                "Secure canonical SKILL.md mutation is unavailable on Windows: "
                "the NT handle-relative backend could not be initialized."
            )
        from tools.nt_secure_fs_optional import open_directory

        resolved_dir = Path(
            existing.get("_resolved_path") or Path(existing["path"]).resolve()
        )
        expected_identity = existing.get("_dir_identity")
        skill_handle = open_directory(resolved_dir, writable=True)
        try:
            if expected_identity and skill_handle.identity != tuple(
                expected_identity
            ):
                raise OSError("skill directory changed before mutation")
            yield skill_handle, resolved_dir
        finally:
            skill_handle.close()
        return

    if not _secure_directory_create_supported():
        raise OSError(
            "Secure canonical SKILL.md mutation is unavailable on this platform."
        )

    resolved_dir = Path(
        existing.get("_resolved_path") or Path(existing["path"]).resolve()
    )
    expected_identity = existing.get("_dir_identity")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    skill_fd = os.open(resolved_dir, directory_flags)
    try:
        held_stat = os.fstat(skill_fd)
        if expected_identity and (
            held_stat.st_dev,
            held_stat.st_ino,
        ) != tuple(expected_identity):
            raise OSError("skill directory changed before mutation")
        yield skill_fd, resolved_dir
    finally:
        os.close(skill_fd)


def _secure_delete_held_directory_contents(directory_fd: int) -> None:
    """Delete a held directory tree without resolving any child path.

    Symlinks are unlinked as directory entries; they are never traversed.
    Every real child directory is reopened with ``O_NOFOLLOW`` and checked
    against the preceding ``lstat`` before recursion.
    """
    if os.name == "nt":
        from tools.nt_secure_fs_optional import delete_tree

        delete_tree(directory_fd)
        return

    for entry in os.scandir(directory_fd):
        entry_stat = os.stat(
            entry.name, dir_fd=directory_fd, follow_symlinks=False
        )
        if stat.S_ISDIR(entry_stat.st_mode):
            child_fd = os.open(
                entry.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                held_stat = os.fstat(child_fd)
                if (held_stat.st_dev, held_stat.st_ino) != (
                    entry_stat.st_dev,
                    entry_stat.st_ino,
                ):
                    raise OSError("skill child directory changed during deletion")
                _secure_delete_held_directory_contents(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(entry.name, dir_fd=directory_fd)
        else:
            os.unlink(entry.name, dir_fd=directory_fd)
    _fsync_committed(directory_fd, "recursive skill deletion")


def _secure_delete_existing_skill(
    existing: Dict[str, Any],
    skills_root: Path,
) -> None:
    """Delete one revalidated skill via held no-follow directory descriptors."""
    if os.name == "nt":
        from tools.nt_secure_fs_optional import open_directory

        with _open_existing_skill_directory(existing) as (
            skill_handle,
            resolved_dir,
        ):
            parent_path = resolved_dir.parent
            with open_directory(parent_path, writable=True) as parent:
                if not _directory_entry_matches_fd(
                    parent, resolved_dir.name, skill_handle
                ):
                    raise OSError("skill directory changed before deletion")
                _secure_delete_held_directory_contents(skill_handle)
                skill_handle.mark_delete(is_directory=True)

                # Preserve empty-category cleanup. Compare the held parent to
                # the physical skills root, and remove only an empty real
                # category still named by its held grandparent.
                with open_directory(
                    Path(skills_root).resolve(strict=True), writable=True
                ) as root:
                    if parent.identity == root.identity:
                        return
                if parent.list_entries():
                    return
                with open_directory(
                    parent_path.parent, writable=True
                ) as grandparent:
                    if not _directory_entry_matches_fd(
                        grandparent, parent_path.name, parent
                    ):
                        raise OSError(
                            "skill category changed during cleanup"
                        )
                    parent.mark_delete(is_directory=True)
        return

    if not _secure_directory_create_supported():
        raise OSError(
            "Secure skill deletion is unavailable on this platform."
        )

    with _open_existing_skill_directory(existing) as (skill_fd, resolved_dir):
        parent_path = resolved_dir.parent
        parent_fd = os.open(
            parent_path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        try:
            if not _directory_entry_matches_fd(
                parent_fd, resolved_dir.name, skill_fd
            ):
                raise OSError("skill directory changed before deletion")
            _secure_delete_held_directory_contents(skill_fd)
            os.rmdir(resolved_dir.name, dir_fd=parent_fd)
            _fsync_committed(parent_fd, "skill deletion")

            # Preserve the legacy empty-category cleanup without trusting the
            # mutable display path used for discovery.
            root_stat = os.stat(skills_root)
            parent_stat = os.fstat(parent_fd)
            with os.scandir(parent_fd) as entries:
                parent_is_empty = not any(entries)
            if (
                (parent_stat.st_dev, parent_stat.st_ino)
                == (root_stat.st_dev, root_stat.st_ino)
                or not parent_is_empty
            ):
                return
            grandparent_path = parent_path.parent
            grandparent_fd = os.open(
                grandparent_path,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            try:
                if not _directory_entry_matches_fd(
                    grandparent_fd, parent_path.name, parent_fd
                ):
                    raise OSError("skill category changed during cleanup")
                os.rmdir(parent_path.name, dir_fd=grandparent_fd)
                _fsync_committed(grandparent_fd, "skill category cleanup")
            finally:
                os.close(grandparent_fd)
        finally:
            os.close(parent_fd)


def _secure_archive_existing_skill(
    existing: Dict[str, Any],
    skills_root: Path,
) -> tuple[bool, str]:
    """Archive a curator skill through its revalidated physical directory.

    This deliberately does not call ``skill_usage.archive_skill``: that helper
    resolves the name anew and moves a path, which would escape the held-dirfd
    transaction that protects alias-based mutations here.
    """
    if os.name == "nt":
        if not _secure_directory_create_supported():
            return (
                False,
                "secure curator archiving is unavailable on this platform",
            )
        return _secure_archive_existing_skill_windows(existing, skills_root)
    if not _secure_directory_create_supported() or os.rename not in os.supports_dir_fd:
        return False, "secure curator archiving is unavailable on this platform"

    with _open_existing_skill_directory(existing) as (skill_fd, resolved_dir):
        frontmatter, _ = _parse_frontmatter(_read_canonical_skill_md(skill_fd))
        skill_name = frontmatter.get("name")
        if not isinstance(skill_name, str) or not skill_name:
            return False, "canonical SKILL.md has no valid skill name"

        from agent.skill_utils import is_external_skill_path
        from tools.skill_usage import (
            is_bundled,
            is_curation_eligible,
            is_hub_installed,
            is_protected_builtin,
            persist_lifecycle_move_metadata_strict,
        )

        if is_external_skill_path(resolved_dir):
            return False, (
                f"skill '{skill_name}' lives in skills.external_dirs; "
                "external skills are read-only to the curator"
            )
        if not is_curation_eligible(skill_name, resolved_dir):
            if is_protected_builtin(skill_name):
                return False, (
                    f"skill '{skill_name}' is a protected built-in; it backs "
                    "load-bearing UX and is never archived or consolidated"
                )
            if is_hub_installed(skill_name):
                return False, (
                    f"skill '{skill_name}' is hub-installed; never archive"
                )
            return False, (
                f"skill '{skill_name}' is a bundled built-in; enable "
                "curator.prune_builtins to allow pruning it"
            )

        root_path = skills_root.resolve()
        root_fd = os.open(
            root_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        source_parent_path = resolved_dir.parent
        source_parent_fd = os.open(
            source_parent_path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        try:
            if not _directory_entry_matches_fd(
                source_parent_fd, resolved_dir.name, skill_fd
            ):
                return False, "skill directory changed before archiving"
            try:
                os.mkdir(".archive", dir_fd=root_fd)
            except FileExistsError:
                pass
            archive_fd = os.open(
                ".archive",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
            try:
                if not _directory_entry_matches_fd(root_fd, ".archive", archive_fd):
                    return False, "archive directory changed before archiving"
                destination = resolved_dir.name
                destination_parent_fd = archive_fd
                collision_fd = container_fd = None
                destination_parent_path = root_path / ".archive"
                container = None
                moved = False
                try:
                    os.stat(destination, dir_fd=archive_fd, follow_symlinks=False)
                except FileNotFoundError:
                    destination_collision = False
                else:
                    destination_collision = True
                if (
                    destination_collision
                    or _has_legacy_archive_timestamp_suffix(destination)
                ):
                    # A flat timestamp suffix cannot distinguish an archive
                    # collision from a user's legitimate timestamped physical
                    # alias. Keep the leaf basename unchanged under a private
                    # collision container instead, so restore has an exact,
                    # unambiguous physical name to put back.
                    try:
                        os.mkdir(".collisions", dir_fd=archive_fd)
                    except FileExistsError:
                        pass
                    collision_fd = os.open(
                        ".collisions",
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=archive_fd,
                    )
                    if not _directory_entry_matches_fd(
                        archive_fd, ".collisions", collision_fd
                    ):
                        return False, "archive collision directory changed before archiving"
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
                    for _ in range(16):
                        container = (
                            f"{resolved_dir.name}-{stamp}-"
                            f"{secrets.token_hex(8)}"
                        )
                        try:
                            os.mkdir(container, dir_fd=collision_fd)
                            break
                        except FileNotFoundError:
                            return False, "archive collision directory disappeared before archiving"
                        except FileExistsError:
                            continue
                    else:
                        return False, "could not reserve a unique archive collision container"
                    container_fd = os.open(
                        container,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=collision_fd,
                    )
                    if not _directory_entry_matches_fd(
                        collision_fd, container, container_fd
                    ):
                        return False, "archive collision container changed before archiving"
                    destination_parent_fd = container_fd
                    destination_parent_path = (
                        root_path / ".archive" / ".collisions" / container
                    )
                if not _directory_entry_matches_fd(
                    source_parent_fd, resolved_dir.name, skill_fd
                ):
                    return False, "skill directory changed before archiving"
                try:
                    os.stat(
                        destination,
                        dir_fd=destination_parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    return False, "archive destination appeared before archiving"
                os.rename(
                    resolved_dir.name,
                    destination,
                    src_dir_fd=source_parent_fd,
                    dst_dir_fd=destination_parent_fd,
                )
                moved = True
                _fsync_committed(source_parent_fd, "skill archive")
                _fsync_committed(destination_parent_fd, "skill archive")
                if destination_parent_fd != archive_fd:
                    _fsync_committed(archive_fd, "skill archive collision container")
            finally:
                if container_fd is not None:
                    if not moved and collision_fd is not None and container is not None:
                        try:
                            if _directory_entry_matches_fd(
                                collision_fd, container, container_fd
                            ):
                                with os.scandir(container_fd) as entries:
                                    container_is_empty = not any(entries)
                                if container_is_empty:
                                    os.rmdir(container, dir_fd=collision_fd)
                                    _fsync_committed(
                                        collision_fd, "archive collision rollback"
                                    )
                        except OSError:
                            logger.debug("could not remove empty archive collision container", exc_info=True)
                    os.close(container_fd)
                if collision_fd is not None:
                    os.close(collision_fd)
                os.close(archive_fd)
        finally:
            os.close(source_parent_fd)
            os.close(root_fd)

    try:
        persist_lifecycle_move_metadata_strict(
            skill_name, "archived", suppressed=is_bundled(skill_name)
        )
    except Exception as exc:
        return False, (
            "archive filesystem move committed but lifecycle metadata was not "
            f"durably recorded: {exc}"
        )
    return True, f"archived to {destination_parent_path / destination}"


def _secure_archive_existing_skill_windows(
    existing: Dict[str, Any],
    skills_root: Path,
) -> tuple[bool, str]:
    """Windows archive implementation using only held NT handles."""
    from agent.skill_utils import is_external_skill_path
    from tools.nt_secure_fs_optional import open_directory
    from tools.skill_usage import (
        is_bundled,
        is_curation_eligible,
        is_hub_installed,
        is_protected_builtin,
        persist_lifecycle_move_metadata_strict,
    )

    destination_path: Optional[Path] = None
    skill_name: Optional[str] = None
    with _open_existing_skill_directory(existing) as (
        skill_handle,
        resolved_dir,
    ):
        frontmatter, _ = _parse_frontmatter(
            _read_canonical_skill_md(skill_handle)
        )
        skill_name = frontmatter.get("name")
        if not isinstance(skill_name, str) or not skill_name:
            return False, "canonical SKILL.md has no valid skill name"
        if is_external_skill_path(resolved_dir):
            return False, (
                f"skill '{skill_name}' lives in skills.external_dirs; "
                "external skills are read-only to the curator"
            )
        if not is_curation_eligible(skill_name, resolved_dir):
            if is_protected_builtin(skill_name):
                return False, (
                    f"skill '{skill_name}' is a protected built-in; it backs "
                    "load-bearing UX and is never archived or consolidated"
                )
            if is_hub_installed(skill_name):
                return False, (
                    f"skill '{skill_name}' is hub-installed; never archive"
                )
            return False, (
                f"skill '{skill_name}' is a bundled built-in; enable "
                "curator.prune_builtins to allow pruning it"
            )

        root_path = Path(skills_root).resolve(strict=True)
        with open_directory(root_path, writable=True) as root_handle, (
            open_directory(resolved_dir.parent, writable=True)
        ) as source_parent:
            if not _directory_entry_matches_fd(
                source_parent, resolved_dir.name, skill_handle
            ):
                return False, "skill directory changed before archiving"
            try:
                archive = root_handle.open_dir(
                    ".archive", writable=True
                )
            except FileNotFoundError:
                archive = root_handle.open_dir(
                    ".archive", create=True, writable=True
                )
            with archive:
                destination = resolved_dir.name
                destination_parent = archive
                destination_parent_path = root_path / ".archive"
                collision = container = None
                try:
                    destination_collision = archive.exists(destination)
                    if (
                        destination_collision
                        or _has_legacy_archive_timestamp_suffix(destination)
                    ):
                        try:
                            collision = archive.open_dir(
                                ".collisions", writable=True
                            )
                        except FileNotFoundError:
                            collision = archive.open_dir(
                                ".collisions",
                                create=True,
                                writable=True,
                            )
                        stamp = datetime.now(timezone.utc).strftime(
                            "%Y%m%d%H%M%S"
                        )
                        for _ in range(16):
                            container_name = (
                                f"{resolved_dir.name}-{stamp}-"
                                f"{secrets.token_hex(8)}"
                            )
                            try:
                                container = collision.open_dir(
                                    container_name,
                                    create=True,
                                    writable=True,
                                )
                                break
                            except FileExistsError:
                                continue
                        if container is None:
                            return False, (
                                "could not reserve a unique archive "
                                "collision container"
                            )
                        destination_parent = container
                        destination_parent_path = (
                            root_path
                            / ".archive"
                            / ".collisions"
                            / container_name
                        )
                    if destination_parent.exists(destination):
                        return False, (
                            "archive destination appeared before archiving"
                        )
                    if not _directory_entry_matches_fd(
                        source_parent, resolved_dir.name, skill_handle
                    ):
                        return False, (
                            "skill directory changed before archiving"
                        )
                    skill_handle.rename_to(
                        destination_parent,
                        destination,
                        replace=False,
                    )
                    destination_path = (
                        destination_parent_path / destination
                    )
                finally:
                    if container is not None:
                        container.close()
                    if collision is not None:
                        collision.close()

    assert skill_name is not None and destination_path is not None
    try:
        persist_lifecycle_move_metadata_strict(
            skill_name, "archived", suppressed=is_bundled(skill_name)
        )
    except Exception as exc:
        return False, (
            "archive filesystem move committed but lifecycle metadata was not "
            f"durably recorded: {exc}"
        )
    return True, f"archived to {destination_path}"


def _secure_restore_archived_skill(
    archived: Dict[str, Any],
    skill_name: str,
    skills_root: Path,
) -> tuple[bool, str]:
    """Restore one archived directory using no-follow descriptors only.

    Direct curator/CLI restore callers hold the normal request-alias lock;
    manager delete never calls this helper, so no lock is acquired here.
    """
    if os.name == "nt":
        if not _secure_directory_create_supported():
            return (
                False,
                "secure curator restore is unavailable on this platform",
            )
        return _secure_restore_archived_skill_windows(
            archived, skill_name, skills_root
        )
    if not _secure_directory_create_supported() or os.rename not in os.supports_dir_fd:
        return False, "secure curator restore is unavailable on this platform"
    root_path = Path(skills_root)
    try:
        with _open_archived_skill_directory(archived, root_path) as (
            source_fd,
            source_parent_fd,
            root_fd,
            archive_fd,
            source_name,
        ):
            frontmatter, _ = _parse_frontmatter(_read_canonical_skill_md(source_fd))
            if frontmatter.get("name") != skill_name:
                return False, "archive source canonical name changed before restore"
            # The caller holds the canonical alias lock before the archived
            # physical lock.  Check every active configured root while that
            # alias is reserved: an active directory may use a different
            # physical basename yet still own this lifecycle identity.  Do not
            # take its physical lock here (that would invert alias->physical
            # ordering); discovery plus the held canonical alias is enough to
            # prevent cooperative create/restore races, and an incomplete scan
            # fails closed against out-of-band filesystem changes.
            active = _find_skill(skill_name)
            active_lookup_error = _skill_lookup_error(active)
            if active_lookup_error:
                return False, (
                    "cannot safely restore while checking active canonical "
                    f"identity: {active_lookup_error['error']}"
                )
            if active:
                return False, (
                    f"canonical skill name '{skill_name}' is already active at "
                    f"{active['path']}; refusing restore"
                )
            relative = archived.get("_archive_relative_parts")
            is_new_collision_layout = (
                isinstance(relative, tuple)
                and len(relative) == 3
                and relative[0] == ".collisions"
            )
            if (
                not is_new_collision_layout
                and _has_legacy_archive_timestamp_suffix(source_name)
            ):
                return False, (
                    "legacy timestamped archive has no trustworthy record "
                    "of its original physical basename; refusing restore"
                )
            # New collision archives preserve the physical basename as their
            # leaf under .archive/.collisions/<unique>/, so no suffix parsing
            # is ever needed. Other physical aliases are restored unchanged.
            destination_name = source_name
            destination = root_path / destination_name
            try:
                os.stat(destination_name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                return False, f"destination already exists: {destination}"
            if not _directory_entry_matches_fd(source_parent_fd, source_name, source_fd):
                return False, "archive source changed before restore"
            os.rename(
                source_name,
                destination_name,
                src_dir_fd=source_parent_fd,
                dst_dir_fd=root_fd,
            )
            _fsync_committed(source_parent_fd, "skill restore")
            _fsync_committed(root_fd, "skill restore")
            _cleanup_empty_archive_collision_container(
                source_parent_fd, archive_fd, archived
            )
    except OSError as exc:
        return False, f"refusing unsafe archive source: {exc}"
    try:
        from tools.skill_usage import persist_lifecycle_move_metadata_strict

        persist_lifecycle_move_metadata_strict(
            skill_name, "active", suppressed=False
        )
    except Exception as exc:
        return False, (
            "restore filesystem move committed but lifecycle metadata was not "
            f"durably recorded: {exc}"
        )
    return True, f"restored to {destination}"


def _secure_restore_archived_skill_windows(
    archived: Dict[str, Any],
    skill_name: str,
    skills_root: Path,
) -> tuple[bool, str]:
    """Restore an archived package with a handle-relative NT rename."""
    root_path = Path(skills_root)
    destination: Optional[Path] = None
    try:
        with _open_archived_skill_directory(archived, root_path) as (
            source,
            source_parent,
            root,
            archive,
            source_name,
        ):
            frontmatter, _ = _parse_frontmatter(
                _read_canonical_skill_md(source)
            )
            if frontmatter.get("name") != skill_name:
                return (
                    False,
                    "archive source canonical name changed before restore",
                )
            active = _find_skill(skill_name)
            active_lookup_error = _skill_lookup_error(active)
            if active_lookup_error:
                return False, (
                    "cannot safely restore while checking active canonical "
                    f"identity: {active_lookup_error['error']}"
                )
            if active:
                return False, (
                    f"canonical skill name '{skill_name}' is already active "
                    f"at {active['path']}; refusing restore"
                )
            relative = archived.get("_archive_relative_parts")
            is_new_collision_layout = (
                isinstance(relative, tuple)
                and len(relative) == 3
                and relative[0] == ".collisions"
            )
            if (
                not is_new_collision_layout
                and _has_legacy_archive_timestamp_suffix(source_name)
            ):
                return False, (
                    "legacy timestamped archive has no trustworthy record "
                    "of its original physical basename; refusing restore"
                )
            destination_name = source_name
            destination = root_path / destination_name
            if root.exists(destination_name):
                return False, f"destination already exists: {destination}"
            if not _directory_entry_matches_fd(
                source_parent, source_name, source
            ):
                return False, "archive source changed before restore"
            source.rename_to(root, destination_name, replace=False)
            _cleanup_empty_archive_collision_container(
                source_parent, archive, archived
            )
    except OSError as exc:
        return False, f"refusing unsafe archive source: {exc}"

    try:
        from tools.skill_usage import persist_lifecycle_move_metadata_strict

        persist_lifecycle_move_metadata_strict(
            skill_name, "active", suppressed=False
        )
    except Exception as exc:
        return False, (
            "restore filesystem move committed but lifecycle metadata was not "
            f"durably recorded: {exc}"
        )
    return True, f"restored to {destination}"


def _cleanup_empty_archive_collision_container(
    source_parent_fd: Any,
    archive_fd: Any,
    archived: Dict[str, Any],
) -> None:
    """Best-effort cleanup for the private collision container we created."""
    relative = archived.get("_archive_relative_parts")
    if (
        not isinstance(relative, tuple)
        or len(relative) != 3
        or relative[0] != ".collisions"
    ):
        return
    if os.name == "nt":
        try:
            collisions = archive_fd.open_dir(
                ".collisions", writable=True
            )
        except OSError:
            return
        try:
            container = relative[1]
            if not _directory_entry_matches_fd(
                collisions, container, source_parent_fd
            ):
                return
            if source_parent_fd.list_entries():
                return
            source_parent_fd.mark_delete(is_directory=True)
            if collisions.list_entries():
                return
            collisions.mark_delete(is_directory=True)
        except OSError as exc:
            logger.debug(
                "could not clean archive collision container: %s", exc
            )
        finally:
            collisions.close()
        return

    try:
        collisions_fd = os.open(
            ".collisions",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=archive_fd,
        )
    except OSError:
        return
    try:
        container = relative[1]
        if not _directory_entry_matches_fd(
            collisions_fd, container, source_parent_fd
        ):
            return
        with os.scandir(source_parent_fd) as entries:
            if any(entries):
                return
        os.rmdir(container, dir_fd=collisions_fd)
        _fsync_committed(collisions_fd, "archive collision cleanup")
        with os.scandir(collisions_fd) as entries:
            if any(entries):
                return
        if _directory_entry_matches_fd(archive_fd, ".collisions", collisions_fd):
            os.rmdir(".collisions", dir_fd=archive_fd)
            _fsync_committed(archive_fd, "archive collision root cleanup")
    except OSError as exc:
        logger.debug("could not clean archive collision container: %s", exc)
    finally:
        os.close(collisions_fd)


def _secure_cleanup_empty_support_parent(
    parent_fd: Any,
    resolved_skill_dir: Path,
    file_path: str,
) -> None:
    """Remove the one empty supporting directory legacy remove_file cleaned."""
    parts = Path(file_path).parts
    parent_relative = Path(*parts[:-1])
    parent_path = resolved_skill_dir / parent_relative
    if os.name == "nt":
        from tools.nt_secure_fs_optional import open_directory

        if parent_fd.list_entries():
            return
        with open_directory(
            parent_path.parent, writable=True
        ) as grandparent:
            if not _directory_entry_matches_fd(
                grandparent, parent_path.name, parent_fd
            ):
                raise OSError(
                    "supporting directory changed during cleanup"
                )
            parent_fd.mark_delete(is_directory=True)
        return

    with os.scandir(parent_fd) as entries:
        if any(entries):
            return
    grandparent_fd = os.open(
        parent_path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        if not _directory_entry_matches_fd(
            grandparent_fd, parent_path.name, parent_fd
        ):
            raise OSError("supporting directory changed during cleanup")
        os.rmdir(parent_path.name, dir_fd=grandparent_fd)
        _fsync_committed(grandparent_fd, "supporting-directory cleanup")
    finally:
        os.close(grandparent_fd)


def _remove_held_regular_file(parent_fd: Any, filename: str) -> None:
    """Remove a regular entry relative to a held parent on either backend."""
    if os.name == "nt":
        with parent_fd.open_file(filename, writable=True) as target:
            if not target.stat().is_file:
                raise OSError("supporting target must be a regular file")
            target.mark_delete(is_directory=False)
        return
    target_stat = os.stat(
        filename, dir_fd=parent_fd, follow_symlinks=False
    )
    if not stat.S_ISREG(target_stat.st_mode):
        raise OSError("supporting target must be a regular file")
    os.unlink(filename, dir_fd=parent_fd)
    _fsync_committed(parent_fd, "supporting-file removal")


def _read_canonical_skill_md(skill_fd: Any) -> str:
    """Read canonical metadata relative to a held, verified directory."""
    if os.name == "nt":
        from tools.nt_secure_fs_optional import read_regular_file

        payload, _metadata = read_regular_file(skill_fd, "SKILL.md")
        return payload.decode("utf-8")

    file_flags = os.O_RDONLY | os.O_NOFOLLOW
    file_fd = os.open("SKILL.md", file_flags, dir_fd=skill_fd)
    try:
        target_stat = os.fstat(file_fd)
        if not stat.S_ISREG(target_stat.st_mode):
            raise OSError("canonical SKILL.md must be a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_fd, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")
    finally:
        os.close(file_fd)


def _read_held_regular_text(parent_fd: Any, filename: str) -> str:
    """Read one regular file without following a mutable path."""
    if os.name == "nt":
        from tools.nt_secure_fs_optional import read_regular_file

        payload, _metadata = read_regular_file(parent_fd, filename)
        return payload.decode("utf-8")

    file_fd = os.open(
        filename,
        os.O_RDONLY | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    try:
        target_stat = os.fstat(file_fd)
        if not stat.S_ISREG(target_stat.st_mode):
            raise OSError("supporting target must be a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_fd, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")
    finally:
        os.close(file_fd)


def _replace_held_regular_text(
    parent_fd: Any,
    filename: str,
    content: str,
    *,
    require_existing: bool,
) -> None:
    """Atomically replace a regular file relative to a held parent dirfd."""
    if os.name == "nt":
        from tools.nt_secure_fs_optional import replace_regular_file

        replace_regular_file(
            parent_fd,
            filename,
            content.encode("utf-8"),
            require_existing=require_existing,
            temp_name=f".tmp_support_{secrets.token_hex(8)}",
        )
        return

    try:
        target_stat = os.stat(
            filename,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        if require_existing:
            raise
    else:
        if not stat.S_ISREG(target_stat.st_mode):
            raise OSError("supporting target must be a regular file")

    temp_name = f".tmp_support_{secrets.token_hex(8)}"
    file_fd = os.open(
        temp_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=parent_fd,
    )
    try:
        payload = content.encode("utf-8")
        offset = 0
        while offset < len(payload):
            written = os.write(file_fd, payload[offset:])
            if written <= 0:
                raise OSError("short write while replacing supporting file")
            offset += written
        os.fsync(file_fd)
    except BaseException:
        try:
            os.unlink(temp_name, dir_fd=parent_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(file_fd)

    try:
        os.replace(
            temp_name,
            filename,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
    except BaseException:
        try:
            os.unlink(temp_name, dir_fd=parent_fd)
        except OSError:
            pass
        raise
    _fsync_committed(parent_fd, "supporting-file replacement")


@contextmanager
def _open_supporting_file_parent(
    existing: Dict[str, Any],
    file_path: str,
    *,
    create_parents: bool,
):
    """Hold every directory segment leading to a supporting file.

    POSIX uses ``dir_fd`` plus ``O_NOFOLLOW``. Windows walks each component
    from the held skill handle through the NT-native backend. Both variants
    bind the transaction to the discovered skill and verified child edges.
    """
    if os.name == "nt":
        if not _secure_directory_create_supported():
            raise OSError(
                "Secure supporting-file mutation is unavailable on Windows: "
                "the NT handle-relative backend could not be initialized."
            )
        parts = Path(file_path).parts
        if len(parts) < 2:
            raise OSError(
                "supporting file path must include a parent directory"
            )
        with _open_existing_skill_directory(existing) as (
            skill_handle,
            resolved_dir,
        ):
            opened = []
            current = skill_handle
            try:
                for component in parts[:-1]:
                    try:
                        child = current.open_dir(
                            component, writable=True
                        )
                    except FileNotFoundError:
                        if not create_parents:
                            raise
                        child = current.open_dir(
                            component, create=True, writable=True
                        )
                    opened.append(child)
                    current = child
                edges = [
                    (
                        skill_handle if index == 0 else opened[index - 1],
                        parts[index],
                        opened[index],
                    )
                    for index in range(len(opened))
                ]

                def path_is_current() -> bool:
                    try:
                        with _open_existing_skill_directory(existing) as (
                            current_skill,
                            _,
                        ):
                            if current_skill.identity != skill_handle.identity:
                                return False
                    except OSError:
                        return False
                    return all(
                        _directory_entry_matches_fd(parent, name, child)
                        for parent, name, child in edges
                    )

                if not path_is_current():
                    raise OSError(
                        "supporting path changed while its directories were opened"
                    )
                yield (
                    skill_handle,
                    current,
                    parts[-1],
                    resolved_dir,
                    path_is_current,
                )
            finally:
                for handle in reversed(opened):
                    handle.close()
        return

    if not _secure_directory_create_supported():
        raise OSError(
            "Secure supporting-file mutation is unavailable on this platform."
        )

    parts = Path(file_path).parts
    if len(parts) < 2:
        raise OSError("supporting file path must include a parent directory")

    with _open_existing_skill_directory(existing) as (
        skill_fd,
        resolved_dir,
    ):
        opened_fds: list[int] = []
        directory_edges: list[tuple[int, str, int]] = []
        current_fd = os.dup(skill_fd)
        opened_fds.append(current_fd)
        try:
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            for component in parts[:-1]:
                try:
                    child_fd = os.open(
                        component,
                        directory_flags,
                        dir_fd=current_fd,
                    )
                except FileNotFoundError:
                    if not create_parents:
                        raise
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                    child_fd = os.open(
                        component,
                        directory_flags,
                        dir_fd=current_fd,
                    )
                child_stat = os.fstat(child_fd)
                if not stat.S_ISDIR(child_stat.st_mode):
                    os.close(child_fd)
                    raise OSError(
                        f"supporting path component '{component}' is not a directory"
                    )
                directory_edges.append((current_fd, component, child_fd))
                opened_fds.append(child_fd)
                current_fd = child_fd

            def path_is_current() -> bool:
                try:
                    logical_stat = os.stat(
                        existing["path"],
                        follow_symlinks=False,
                    )
                    held_skill_stat = os.fstat(skill_fd)
                except OSError:
                    return False
                if (
                    not stat.S_ISDIR(logical_stat.st_mode)
                    or logical_stat.st_dev != held_skill_stat.st_dev
                    or logical_stat.st_ino != held_skill_stat.st_ino
                ):
                    return False
                return all(
                    _directory_entry_matches_fd(
                        parent_fd, component, child_fd
                    )
                    for parent_fd, component, child_fd in directory_edges
                )

            if not path_is_current():
                raise OSError(
                    "supporting path changed while its directories were opened"
                )
            yield (
                skill_fd,
                current_fd,
                parts[-1],
                resolved_dir,
                path_is_current,
            )
        finally:
            for opened_fd in reversed(opened_fds):
                try:
                    os.close(opened_fd)
                except OSError:
                    pass


def _replace_canonical_skill_md(skill_fd: Any, content: str) -> None:
    """Replace canonical metadata relative to a held directory descriptor."""
    if os.name == "nt":
        from tools.nt_secure_fs_optional import replace_regular_file

        replace_regular_file(
            skill_fd,
            "SKILL.md",
            content.encode("utf-8"),
            require_existing=True,
            temp_name=f".tmp_SKILL_{secrets.token_hex(8)}",
        )
        return

    payload = content.encode("utf-8")
    target_stat = os.stat(
        "SKILL.md",
        dir_fd=skill_fd,
        follow_symlinks=False,
    )
    if not stat.S_ISREG(target_stat.st_mode):
        raise OSError("canonical SKILL.md must be a regular file")

    temp_name = f".tmp_SKILL_{secrets.token_hex(8)}"
    file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    file_fd = os.open(temp_name, file_flags, 0o600, dir_fd=skill_fd)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(file_fd, payload[offset:])
            if written <= 0:
                raise OSError("short write while replacing SKILL.md")
            offset += written
        os.fsync(file_fd)
    except BaseException:
        try:
            os.unlink(temp_name, dir_fd=skill_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(file_fd)

    try:
        os.replace(
            temp_name,
            "SKILL.md",
            src_dir_fd=skill_fd,
            dst_dir_fd=skill_fd,
        )
    except BaseException:
        try:
            os.unlink(temp_name, dir_fd=skill_fd)
        except OSError:
            pass
        raise
    _fsync_committed(skill_fd, "canonical SKILL.md replacement")


def _security_scan_held_skill_impl(skill_fd: Any) -> Optional[str]:
    """Scan a no-follow snapshot copied from a held skill directory."""
    with tempfile.TemporaryDirectory(prefix=".hermes-skill-scan-") as temp_dir:
        snapshot = Path(temp_dir) / "skill"
        if os.name == "nt":
            from tools.nt_secure_fs_optional import copy_tree_no_reparse

            copy_tree_no_reparse(skill_fd, snapshot)
            return _security_scan_skill(snapshot)

        snapshot.mkdir()
        for root, dirs, files, root_fd in os.fwalk(
            ".",
            topdown=True,
            follow_symlinks=False,
            dir_fd=skill_fd,
        ):
            relative_root = Path() if root == "." else Path(root)
            destination_root = snapshot / relative_root
            destination_root.mkdir(parents=True, exist_ok=True)

            for dirname in list(dirs):
                entry_stat = os.stat(
                    dirname,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
                destination = destination_root / dirname
                if stat.S_ISLNK(entry_stat.st_mode):
                    dirs.remove(dirname)
                    destination.symlink_to(
                        ".hermes-blocked-symlink",
                        target_is_directory=True,
                    )
                elif stat.S_ISDIR(entry_stat.st_mode):
                    destination.mkdir(exist_ok=True)
                else:
                    dirs.remove(dirname)

            for filename in files:
                entry_stat = os.stat(
                    filename,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
                destination = destination_root / filename
                if stat.S_ISLNK(entry_stat.st_mode):
                    destination.symlink_to(
                        ".hermes-blocked-symlink"
                    )
                    continue
                if not stat.S_ISREG(entry_stat.st_mode):
                    continue
                source_fd = os.open(
                    filename,
                    os.O_RDONLY | os.O_NOFOLLOW,
                    dir_fd=root_fd,
                )
                try:
                    with os.fdopen(os.dup(source_fd), "rb") as source, open(
                        destination, "wb"
                    ) as target:
                        shutil.copyfileobj(source, target)
                finally:
                    os.close(source_fd)
        return _security_scan_skill(snapshot)


def _security_scan_held_skill(skill_fd: int) -> Optional[str]:
    """Fail closed if an enabled anchored scan snapshot cannot be built.

    Disabled scans are intentionally a true no-op: do not ``fwalk`` or copy
    the held tree before discovering that ``guard_agent_created`` is off.
    """
    if not _agent_created_security_scan_enabled():
        return None
    try:
        return _security_scan_held_skill_impl(skill_fd)
    except Exception as exc:
        logger.warning(
            "Could not securely snapshot skill for scanning: %s",
            exc,
            exc_info=True,
        )
        return f"Could not securely scan updated skill: {exc}"


def _secure_replace_existing_skill_md(
    existing: Dict[str, Any],
    content: str,
    *,
    skill_fd: Optional[int] = None,
) -> Optional[str]:
    """Replace canonical SKILL.md without following a mutable target path."""
    if skill_fd is not None:
        try:
            _replace_canonical_skill_md(skill_fd, content)
            return None
        except Exception as exc:
            return f"Could not securely replace SKILL.md: {exc}"

    try:
        with _open_existing_skill_directory(existing) as (opened_fd, _):
            _replace_canonical_skill_md(opened_fd, content)
        return None
    except Exception as exc:
        return f"Could not securely replace SKILL.md: {exc}"


def _secure_create_and_write_skill_windows(
    name: str,
    category: Optional[str],
    content: str,
) -> tuple[Optional[Path], Optional[str]]:
    """Create, scan, and (on failure) roll back through NT directory handles."""
    if os.name != "nt":
        return None, (
            "Secure skill creation is unavailable on Windows because the "
            "NT handle-relative backend is not running on this host."
        )
    from tools.nt_secure_fs_optional import (
        delete_tree,
        open_directory,
        replace_regular_file,
    )

    root = _skills_dir()
    skill_dir = root / category / name if category else root / name
    root_handle = parent_handle = skill_handle = None
    created_category = False
    created_skill = False

    def cleanup() -> None:
        nonlocal skill_handle
        if skill_handle is not None and created_skill:
            try:
                delete_tree(skill_handle)
                skill_handle.mark_delete(is_directory=True)
            except OSError:
                logger.debug(
                    "could not roll back Windows skill directory",
                    exc_info=True,
                )
        if (
            parent_handle is not None
            and created_category
            and parent_handle is not root_handle
        ):
            try:
                if not parent_handle.list_entries():
                    parent_handle.mark_delete(is_directory=True)
            except OSError:
                logger.debug(
                    "could not roll back Windows skill category",
                    exc_info=True,
                )

    try:
        root.mkdir(parents=True, exist_ok=True)
        root_path = root.resolve(strict=True)
        root_handle = open_directory(root_path, writable=True)
        if category:
            try:
                parent_handle = root_handle.open_dir(
                    category, writable=True
                )
            except FileNotFoundError:
                parent_handle = root_handle.open_dir(
                    category, create=True, writable=True
                )
                created_category = True
        else:
            parent_handle = root_handle

        try:
            skill_handle = parent_handle.open_dir(
                name, create=True, writable=True
            )
            created_skill = True
        except FileExistsError:
            return None, f"A file or directory already exists at {skill_dir}."

        replace_regular_file(
            skill_handle,
            "SKILL.md",
            content.encode("utf-8"),
            require_existing=False,
            temp_name=f".tmp_SKILL_{secrets.token_hex(8)}",
        )
        if (
            parent_handle.entry_identity(name, directory=True)
            != skill_handle.identity
        ):
            cleanup()
            return None, (
                "Skill path changed during creation; the write was rolled back."
            )
        scan_error = _security_scan_held_skill(skill_handle)
        if scan_error:
            cleanup()
            return None, scan_error
        if (
            parent_handle.entry_identity(name, directory=True)
            != skill_handle.identity
        ):
            cleanup()
            return None, (
                "Skill path changed during security scanning; the write was "
                "rolled back."
            )
        return skill_dir, None
    except FileExistsError:
        cleanup()
        return None, f"A file or directory already exists at {skill_dir}."
    except Exception as exc:
        cleanup()
        return None, f"Could not securely create skill: {exc}"
    finally:
        if skill_handle is not None:
            skill_handle.close()
        if (
            parent_handle is not None
            and parent_handle is not root_handle
        ):
            parent_handle.close()
        if root_handle is not None:
            root_handle.close()


def _secure_create_and_write_skill(
    name: str,
    category: Optional[str],
    content: str,
) -> tuple[Optional[Path], Optional[str]]:
    """Create and write a skill without resolving mutable parent path strings.

    On POSIX, all child operations are relative to already-open directory
    descriptors with ``O_NOFOLLOW``. A category can therefore be renamed or
    replaced by a symlink during creation without redirecting the write.
    """
    if os.name == "nt":
        if not _secure_directory_create_supported():
            return None, (
                "Secure skill creation is unavailable on Windows because "
                "the NT handle-relative backend could not be initialized."
            )
        return _secure_create_and_write_skill_windows(
            name, category, content
        )
    if not _secure_directory_create_supported():
        return None, (
            "Secure skill creation is unavailable on this platform."
        )

    root = _skills_dir()
    root_fd = parent_fd = skill_fd = None
    created_category = False
    created_skill = False
    temp_name: Optional[str] = None
    skill_dir = root / category / name if category else root / name

    def cleanup_owned_entries() -> None:
        if skill_fd is not None:
            for filename in (temp_name, "SKILL.md"):
                if not filename:
                    continue
                try:
                    os.unlink(filename, dir_fd=skill_fd)
                except OSError:
                    pass
        if (
            created_skill
            and parent_fd is not None
            and skill_fd is not None
            and _directory_entry_matches_fd(parent_fd, name, skill_fd)
        ):
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                pass
        if (
            created_category
            and root_fd is not None
            and parent_fd is not None
            and category
            and _directory_entry_matches_fd(root_fd, category, parent_fd)
        ):
            try:
                os.rmdir(category, dir_fd=root_fd)
            except OSError:
                pass

    try:
        root.mkdir(parents=True, exist_ok=True)
        root_resolved = root.resolve()
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root_fd = os.open(root_resolved, directory_flags)

        if category:
            if _is_path_redirect(root / category):
                return None, (
                    f"Refusing to create a skill through redirected category "
                    f"'{category}'."
                )
            try:
                os.mkdir(category, dir_fd=root_fd)
                created_category = True
            except FileExistsError:
                pass
            parent_fd = os.open(category, directory_flags, dir_fd=root_fd)
        else:
            parent_fd = os.dup(root_fd)

        try:
            os.mkdir(name, dir_fd=parent_fd)
            created_skill = True
        except FileExistsError:
            cleanup_owned_entries()
            return None, f"A file or directory already exists at {skill_dir}."
        skill_fd = os.open(name, directory_flags, dir_fd=parent_fd)

        temp_name = f".tmp_SKILL_{secrets.token_hex(8)}"
        file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        file_fd = os.open(temp_name, file_flags, 0o600, dir_fd=skill_fd)
        try:
            payload = content.encode("utf-8")
            offset = 0
            while offset < len(payload):
                written = os.write(file_fd, payload[offset:])
                if written <= 0:
                    raise OSError("short write while creating SKILL.md")
                offset += written
            os.fsync(file_fd)
        finally:
            os.close(file_fd)

        os.replace(
            temp_name,
            "SKILL.md",
            src_dir_fd=skill_fd,
            dst_dir_fd=skill_fd,
        )
        temp_name = None
        _fsync_committed(skill_fd, "skill creation")

        try:
            logical_stat = os.stat(skill_dir, follow_symlinks=False)
            held_stat = os.fstat(skill_fd)
            path_is_current = (
                stat.S_ISDIR(logical_stat.st_mode)
                and logical_stat.st_dev == held_stat.st_dev
                and logical_stat.st_ino == held_stat.st_ino
                and skill_dir.resolve().is_relative_to(root_resolved)
            )
        except OSError:
            path_is_current = False
        if not path_is_current:
            cleanup_owned_entries()
            return None, (
                "Skill path changed during creation; the write was rolled back."
            )

        scan_error = _security_scan_held_skill(skill_fd)
        if scan_error:
            cleanup_owned_entries()
            return None, scan_error

        # The scan is anchored, but the returned logical path must still name
        # the held object when creation reports success.
        try:
            logical_stat = os.stat(skill_dir, follow_symlinks=False)
            held_stat = os.fstat(skill_fd)
            path_is_current = (
                logical_stat.st_dev == held_stat.st_dev
                and logical_stat.st_ino == held_stat.st_ino
            )
        except OSError:
            path_is_current = False
        if not path_is_current:
            cleanup_owned_entries()
            return None, (
                "Skill path changed during security scanning; the write was rolled back."
            )
        return skill_dir, None
    except FileExistsError:
        cleanup_owned_entries()
        return None, f"A file or directory already exists at {skill_dir}."
    except Exception as exc:
        cleanup_owned_entries()
        return None, f"Could not securely create skill: {exc}"
    finally:
        for fd in (skill_fd, parent_fd, root_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _create_skill_directory(
    name: str, category: Optional[str]
) -> tuple[Optional[Path], Optional[Path], Optional[str]]:
    """Exclusively create a new local skill directory inside the skills root."""
    root = _skills_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
        root_resolved = root.resolve()
    except OSError as exc:
        return None, None, f"Could not prepare skills directory: {exc}"

    parent = root
    created_category: Optional[Path] = None
    if category:
        parent = root / category
        if parent.exists() or parent.is_symlink():
            if _is_path_redirect(parent):
                return (
                    None,
                    None,
                    f"Refusing to create a skill through redirected category '{category}'.",
                )
            if not parent.is_dir():
                return None, None, f"Skill category path is not a directory: {parent}"
        else:
            try:
                parent.mkdir(exist_ok=False)
                created_category = parent
            except FileExistsError:
                return None, None, f"Skill category path changed during creation: {parent}"
            except OSError as exc:
                return None, None, f"Could not create skill category '{category}': {exc}"

    try:
        parent.resolve().relative_to(root_resolved)
    except (OSError, ValueError):
        if created_category is not None:
            try:
                created_category.rmdir()
            except OSError:
                pass
        return None, None, "Refusing to create a skill outside the active skills directory."

    skill_dir = parent / name
    try:
        skill_dir.mkdir(exist_ok=False)
    except FileExistsError:
        if created_category is not None:
            try:
                created_category.rmdir()
            except OSError:
                pass
        return None, None, f"A file or directory already exists at {skill_dir}."
    except OSError as exc:
        if created_category is not None:
            try:
                created_category.rmdir()
            except OSError:
                pass
        return None, None, f"Could not create skill directory: {exc}"

    try:
        skill_dir.resolve().relative_to(root_resolved)
    except (OSError, ValueError):
        _cleanup_created_skill_dir(
            skill_dir, created_category=created_category
        )
        return None, None, "Refusing to create a skill outside the active skills directory."

    return skill_dir, created_category, None


def _find_skill(name: str) -> Optional[Dict[str, Any]]:
    """
    Find a skill by name across all skill directories.

    Searches the local skills dir (~/.hermes/skills/) first, then any
    external dirs configured via skills.external_dirs.  Returns
    {"path": Path} or None.
    """
    from agent.skill_utils import (
        SkillsConfigError,
        get_all_skills_dirs,
        iter_skill_index_files,
    )

    try:
        skill_roots = get_all_skills_dirs(require_valid_config=True)
    except SkillsConfigError as exc:
        return {
            "error": (
                "Skill lookup is incomplete because the configured skills "
                f"scope could not be read safely: {exc}"
            ),
            "paths": [],
        }
    matches: list[tuple[Path, Path, tuple[int, int]]] = []
    seen_paths: set[str] = set()
    external_roots = {
        os.path.normcase(os.path.abspath(str(root)))
        for root in skill_roots[1:]
    }
    scan_failures: list[str] = []
    for skills_dir in skill_roots:
        root_key = os.path.normcase(os.path.abspath(str(skills_dir)))
        is_external = root_key in external_roots
        try:
            root_stat = skills_dir.stat()
        except FileNotFoundError as exc:
            if is_external:
                scan_failures.append(f"{skills_dir}: {exc}")
            continue
        except OSError as exc:
            scan_failures.append(f"{skills_dir}: {exc}")
            continue
        if not stat.S_ISDIR(root_stat.st_mode):
            scan_failures.append(f"{skills_dir}: not a directory")
            continue

        walk_errors: list[OSError] = []
        for skill_md in iter_skill_index_files(
            skills_dir,
            "SKILL.md",
            on_error=walk_errors.append,
        ):
            # Canonical metadata is executable agent guidance. Never treat a
            # symlink/reparse-point file as a mutable skill definition.
            if skill_md.is_symlink():
                continue
            try:
                resolved_path = skill_md.parent.resolve()
                if _secure_directory_create_supported():
                    if os.name == "nt":
                        from tools.nt_secure_fs_optional import open_directory

                        with open_directory(
                            resolved_path, writable=False
                        ) as candidate_fd:
                            directory_stat = candidate_fd.stat()
                            with candidate_fd.open_file(
                                "SKILL.md", writable=False
                            ) as canonical:
                                if not canonical.stat().is_file:
                                    scan_failures.append(
                                        f"{skill_md}: canonical metadata is "
                                        "not a regular file"
                                    )
                                    continue
                            matched = skill_md.parent.name == name
                            if not matched:
                                frontmatter, _ = _parse_frontmatter(
                                    _read_canonical_skill_md(candidate_fd)
                                )
                                matched = frontmatter.get("name") == name
                    else:
                        directory_flags = (
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                        )
                        candidate_fd = os.open(
                            resolved_path, directory_flags
                        )
                        try:
                            directory_stat = os.fstat(candidate_fd)
                            canonical_stat = os.stat(
                                "SKILL.md",
                                dir_fd=candidate_fd,
                                follow_symlinks=False,
                            )
                            if not stat.S_ISREG(canonical_stat.st_mode):
                                scan_failures.append(
                                    f"{skill_md}: canonical metadata is not "
                                    "a regular file"
                                )
                                continue
                            matched = skill_md.parent.name == name
                            if not matched:
                                frontmatter, _ = _parse_frontmatter(
                                    _read_canonical_skill_md(candidate_fd)
                                )
                                matched = frontmatter.get("name") == name
                        finally:
                            os.close(candidate_fd)
                else:
                    directory_stat = resolved_path.stat()
                    matched = skill_md.parent.name == name
                    if not matched:
                        frontmatter, _ = _parse_frontmatter(
                            skill_md.read_text(encoding="utf-8")
                        )
                        matched = frontmatter.get("name") == name
                if not matched:
                    continue
                identity = str(resolved_path)
            except (OSError, UnicodeError) as exc:
                scan_failures.append(f"{skill_md}: {exc}")
                continue
            if identity not in seen_paths:
                seen_paths.add(identity)
                matches.append(
                    (
                        skill_md.parent,
                        resolved_path,
                        (directory_stat.st_dev, directory_stat.st_ino),
                    )
                )
        if walk_errors:
            scan_failures.extend(
                f"{skills_dir}: {error}" for error in walk_errors
            )

    if scan_failures:
        return {
            "error": (
                "Skill lookup is incomplete because a configured skills root "
                "could not be scanned; refusing a local-only mutation that "
                "could collide with a hidden skill: "
                + "; ".join(scan_failures[:3])
            ),
            "paths": [],
        }
    if not matches:
        return None
    if len(matches) > 1:
        paths = sorted(str(path) for path, _, _ in matches)
        return {
            "error": (
                f"Skill name '{name}' is ambiguous; it matches multiple "
                f"directories: {', '.join(paths)}"
            ),
            "paths": paths,
        }
    path, resolved_path, directory_identity = matches[0]
    return {
        "path": path,
        "_resolved_path": resolved_path,
        "_dir_identity": directory_identity,
    }


def _skill_lookup_error(existing: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Convert an ambiguous lookup marker into a normal tool error."""
    if existing and existing.get("error"):
        return {"success": False, "error": existing["error"]}
    return None


def _maybe_auto_propose_org_edit(name: str, skill_path: Path) -> Optional[str]:
    """Submit an org-skill edit upstream when `sync.org_auto_propose` is on.

    Returns a short note for the tool result, or None when nothing happened.
    Never raises: an offline/failed submission must not fail the edit itself —
    the change is already saved locally and can be proposed later.
    """
    try:
        from agent.skill_utils import is_org_mirror_path
        from tools import skills_sync_client as ssc

        if not is_org_mirror_path(skill_path, _skills_dir()):
            return None
        if not ssc.sync_org_auto_propose():
            return (
                f"This skill is shared by your organisation. Your edit is "
                f"saved locally and will not be overwritten by org updates. "
                f"Run `hermes sync propose {name}` to share it back."
            )
        result = ssc.propose_skill(name)
        if result.get("proposal_pending"):
            return (
                f"Auto-proposed to your organisation as proposal "
                f"#{result.get('proposal_id')} (pending admin review)."
            )
        return "Auto-proposed to your organisation (merged into the shared set)."
    except Exception as e:
        logger.debug("auto-propose skipped for %s: %s", name, e)
        return (
            f"Edit saved locally. Could not submit it to your organisation "
            f"right now — run `hermes sync propose {name}` to retry."
        )


def _org_mirror_write_guard(name: str, skill_path: Path, action: str) -> Optional[Dict[str, Any]]:
    """Org-shared skills are EDITABLE IN PLACE — this only blocks deletion.

    Earlier versions refused every write to `_org/`, which broke the learning
    loop exactly where it matters most: the agent is told to patch a skill the
    moment it finds a gap, and shared skills are the ones the most people use.
    Blocking that froze org skills while personal ones kept improving, and the
    "fork it into a personal skill" alternative is not something an agent does
    mid-task — so improvements were simply lost.

    Now an edit lands in the mirror and is protected from being overwritten by
    the next org pull (see the baseline sidecar in skills_sync_client). It
    reaches the organisation when the user runs `hermes sync propose`, or
    immediately if `sync.org_auto_propose` is on.

    Deletion is still refused: the mirror is a materialized view of the org
    HEAD, so a local delete is meaningless (the next pull restores it) and
    removing a skill for the organisation is an admin action, not a local one.
    """
    if action not in {"delete", "remove_file"}:
        return None
    try:
        from agent.skill_utils import is_org_mirror_path

        if is_org_mirror_path(skill_path, _skills_dir()):
            return {
                "success": False,
                "error": (
                    f"Cannot {action} '{name}' locally: it is shared by your "
                    "organisation, so a local delete would just come back on "
                    "the next sync. Ask an org admin to remove it for "
                    "everyone. (Editing it IS allowed — your changes are kept "
                    "and can be proposed back with `hermes sync propose "
                    f"{name}`.)"
                ),
            }
    except Exception:
        logger.debug("org mirror guard lookup failed for %s", name, exc_info=True)
    return None


def _find_skill_in_other_profiles(name: str) -> List[Tuple[str, Path]]:
    """Look for ``name`` under SKILL.md across OTHER Hermes profiles.

    Returns a list of ``(profile_name, skill_dir)`` pairs. Used to make
    the "Skill X not found" error explain when the user is editing the
    wrong profile. Empty list when no other profile has the skill (or
    when profile discovery fails — fail-quiet, the caller falls back to
    the plain "not found" error).
    """
    matches: List[Tuple[str, Path]] = []
    try:
        from hermes_constants import get_default_hermes_root
        from agent.skill_utils import is_excluded_skill_path
    except Exception:
        return matches

    try:
        root = get_default_hermes_root()
    except Exception:
        return matches

    # Collect (profile_name, skills_dir) for every profile EXCEPT the
    # one whose skills dir we already searched in _find_skill().
    _active = _skills_dir()
    active_dir = _active.resolve() if _active.exists() else _active
    candidates: List[Tuple[str, Path]] = []

    # Default profile (~/.hermes/skills) — only consider when active is non-default.
    default_skills = root / "skills"
    try:
        if default_skills.resolve() != active_dir:
            candidates.append(("default", default_skills))
    except (OSError, RuntimeError):
        pass

    # All named profiles (~/.hermes/profiles/*/skills)
    profiles_root = root / "profiles"
    if profiles_root.is_dir():
        try:
            for entry in profiles_root.iterdir():
                if not entry.is_dir():
                    continue
                pskills = entry / "skills"
                try:
                    if pskills.resolve() == active_dir:
                        continue
                except (OSError, RuntimeError):
                    continue
                candidates.append((entry.name, pskills))
        except OSError:
            pass

    for profile_name, skills_dir in candidates:
        if not skills_dir.is_dir():
            continue
        try:
            for skill_md in skills_dir.rglob("SKILL.md"):
                if is_excluded_skill_path(skill_md):
                    continue
                if skill_md.parent.name == name:
                    matches.append((profile_name, skill_md.parent))
                    break  # one match per profile is enough
        except OSError:
            continue
    return matches


def _skill_not_found_error(name: str, suffix: str = "") -> str:
    """Build a "skill not found" error that names other profiles holding
    the same skill, so the agent can recognize a profile-scoping mistake.

    ``suffix`` is appended after the cross-profile hint if present
    (e.g. ``" Create it first with action='create'."``).
    """
    from agent.file_safety import _resolve_active_profile_name
    active = _resolve_active_profile_name()
    base = f"Skill '{name}' not found in active profile '{active}'."

    others = _find_skill_in_other_profiles(name)
    if others:
        if len(others) == 1:
            other_profile, other_path = others[0]
            base += (
                f" A skill by that name exists in profile "
                f"'{other_profile}' ({other_path}). To edit a skill in "
                f"another profile, switch profiles (`hermes -p "
                f"{other_profile}`) or operate via explicit file tools "
                f"with ``cross_profile=True``."
            )
        else:
            names = ", ".join(f"'{p}'" for p, _ in others)
            base += (
                f" Skills by that name exist in other profiles: {names}. "
                f"Switch profiles (`hermes -p <name>`) to edit there, or "
                f"operate via explicit file tools with ``cross_profile=True``."
            )
    else:
        base += " Use skills_list() to see available skills."

    if suffix:
        base += suffix
    return base


def _validate_file_path(
    file_path: str,
    skill_name: Optional[str] = None,
) -> Optional[str]:
    """
    Validate a file path for write_file/remove_file.
    Must be under an allowed subdirectory and not escape the skill dir.
    """
    from tools.path_security import has_traversal_component

    if not file_path:
        return "file_path is required."

    normalized = Path(file_path)

    # Prevent path traversal (checked before any allow-listing so the SKILL.md
    # exception below can never be reached by a traversal-laden path).
    if has_traversal_component(file_path):
        return "Path traversal ('..') is not allowed."

    # SKILL.md is the canonical skill file and lives at the skill root. The
    # optional name-prefixed spelling must match the target skill exactly;
    # accepting any two-part path would bypass the supporting-dir allowlist.
    if normalized.parts == ("SKILL.md",):
        return None
    if skill_name and normalized.parts == (skill_name, "SKILL.md"):
        return None

    # Must be under an allowed subdirectory
    if not normalized.parts or normalized.parts[0] not in ALLOWED_SUBDIRS:
        allowed = ", ".join(sorted(ALLOWED_SUBDIRS))
        return f"File must be under one of: {allowed}. Got: '{file_path}'"

    # Must have a filename (not just a directory)
    if len(normalized.parts) < 2:
        return f"Provide a file path, not just a directory. Example: '{normalized.parts[0]}/myfile.md'"

    return None


def _targets_skill_md(file_path: Optional[str], skill_name: str) -> bool:
    if not file_path:
        return False
    normalized = Path(file_path)
    return normalized.parts == ("SKILL.md",) or normalized.parts == (
        skill_name,
        "SKILL.md",
    )


def _resolve_skill_target(skill_dir: Path, file_path: str) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve a supporting-file path and ensure it stays within the skill directory."""
    from tools.path_security import validate_within_dir

    target = skill_dir / file_path
    error = validate_within_dir(target, skill_dir)
    if error:
        return None, error
    return target, None


# =============================================================================
# Core actions
# =============================================================================


def _add_description_prompt_preview(result: Dict[str, Any], content: str) -> None:
    """Append a system_prompt_preview field when the description will be truncated."""
    fm, _ = _parse_frontmatter(content)
    if is_skill_description_truncated_for_prompt(fm):
        result["system_prompt_preview"] = (
            f"System prompt will show: \"{extract_skill_description(fm)}\" — "
            f"keep the trigger self-contained in the first "
            f"{SKILL_PROMPT_DESC_LIMIT - 3} chars."
        )


def _create_skill(name: str, content: str, category: str = None) -> Dict[str, Any]:
    """Create a new user skill with SKILL.md content."""
    # Validate name
    err = _validate_name(name)
    if err:
        return {"success": False, "error": err}

    err = _validate_category(category)
    if err:
        return {"success": False, "error": err}
    category = (category.strip() or None) if isinstance(category, str) else None

    # Normalize before both validation and persistence so a BOM accepted here
    # cannot make runtime parsing lose the frontmatter later.
    content = _normalize_skill_content(content)

    # Validate content and keep the API/directory/frontmatter identity aligned.
    err = _validate_frontmatter(
        content,
        new_skill=True,
        expected_name=name,
    )
    if err:
        return {"success": False, "error": err}

    err = _validate_content_size(content)
    if err:
        return {"success": False, "error": err}

    # Reserve the global frontmatter identity across processes. Directory
    # exclusivity alone is insufficient because the same name can otherwise
    # be created concurrently under two different categories.
    try:
        with _skill_mutation_lock(name):
            existing = _find_skill(name)
            lookup_error = _skill_lookup_error(existing)
            if lookup_error:
                return lookup_error
            if existing:
                return {
                    "success": False,
                    "error": (
                        f"A skill named '{name}' already exists at "
                        f"{existing['path']}."
                    ),
                }

            # Create and write through held directory handles. This cannot be
            # redirected by swapping a checked parent for a symlink before the
            # file write, and the final directory reservation remains exclusive.
            skill_dir, create_error = _secure_create_and_write_skill(
                name, category, content
            )
    except OSError as exc:
        return {
            "success": False,
            "error": f"Could not reserve skill identity: {exc}",
        }
    if create_error or skill_dir is None:
        return {"success": False, "error": create_error or "Could not create skill."}
    skill_md = skill_dir / "SKILL.md"

    # Extract description from frontmatter for verbose notifications
    _desc = ""
    try:
        _fm_end = re.search(r'\n---\s*\n', content[3:])
        if _fm_end:
            _parsed = yaml.safe_load(content[3:_fm_end.start() + 3])
            _desc = str(_parsed.get("description", ""))[:120]
    except Exception:
        pass

    result = {
        "success": True,
        "message": f"Skill '{name}' created.",
        "path": str(skill_dir.relative_to(_skills_dir())),
        "skill_md": str(skill_md),
        "_change": {"description": _desc},
    }
    if category:
        result["category"] = category
    result["hint"] = (
        "To add reference files, templates, or scripts, use "
        "skill_manage(action='write_file', name='{}', file_path='references/example.md', file_content='...')".format(name)
    )
    _add_description_prompt_preview(result, content)
    return result


def _edit_skill(name: str, content: str) -> Dict[str, Any]:
    """Serialize a full canonical rewrite transaction for one skill."""
    try:
        with _existing_skill_mutation_lock(name) as (existing, lock_error):
            if lock_error:
                return lock_error
            assert existing is not None
            return _edit_skill_transaction(name, content, existing=existing)
    except OSError as exc:
        return {
            "success": False,
            "error": f"Could not reserve skill mutation: {exc}",
        }


def _edit_skill_transaction(
    name: str,
    content: str,
    *,
    existing: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Replace the SKILL.md of any existing skill (full rewrite)."""
    content = _normalize_skill_content(content)
    err = _validate_frontmatter(content, expected_name=name)
    if err:
        return {"success": False, "error": err}

    err = _validate_content_size(content)
    if err:
        return {"success": False, "error": err}

    if existing is None:
        existing = _find_skill(name)
        lookup_error = _skill_lookup_error(existing)
        if lookup_error:
            return lookup_error
        if not existing:
            return {"success": False, "error": _skill_not_found_error(name)}
    org_guard = _org_mirror_write_guard(name, existing["path"], "edit")
    if org_guard:
        return org_guard
    guard = _background_review_write_guard(name, existing["path"], "edit")
    if guard:
        return guard

    skill_md = existing["path"] / "SKILL.md"
    read_guard = _background_review_read_before_write_guard(
        name, skill_md, "edit", "SKILL.md"
    )
    if read_guard:
        return read_guard

    try:
        with _open_existing_skill_directory(existing) as (
            skill_fd,
            _resolved_dir,
        ):
            # Read, write, scan, and rollback all stay attached to this exact
            # directory object even if an external skill-root symlink moves.
            original_content = _read_canonical_skill_md(skill_fd)
            write_error = _secure_replace_existing_skill_md(
                existing,
                content,
                skill_fd=skill_fd,
            )
            if write_error:
                return {"success": False, "error": write_error}

            scan_error = _security_scan_held_skill(skill_fd)
            if scan_error:
                rollback_error = _secure_replace_existing_skill_md(
                    existing,
                    original_content,
                    skill_fd=skill_fd,
                )
                if rollback_error:
                    return {
                        "success": False,
                        "error": f"{scan_error}; rollback failed: {rollback_error}",
                    }
                return {"success": False, "error": scan_error}
    except Exception as exc:
        return {
            "success": False,
            "error": f"Could not securely mutate SKILL.md: {exc}",
        }

    # Extract description from new content for verbose notifications
    _desc = ""
    try:
        _fm_end = re.search(r'\n---\s*\n', content[3:])
        if _fm_end:
            _parsed = yaml.safe_load(content[3:_fm_end.start() + 3])
            _desc = str(_parsed.get("description", ""))[:120]
    except Exception:
        pass

    result = {
        "success": True,
        "message": f"Skill '{name}' updated (full rewrite).",
        "path": str(existing.get("_resolved_path") or existing["path"]),
        "_change": {"description": _desc},
    }
    org_note = _maybe_auto_propose_org_edit(name, existing["path"])
    if org_note:
        result["org_sharing"] = org_note
        result["message"] = f"{result['message']} {org_note}"
    _add_description_prompt_preview(result, content)
    return result


def _apply_skill_patch(
    *,
    name: str,
    existing: Dict[str, Any],
    skill_dir: Path,
    target: Path,
    file_path: Optional[str],
    patches_main_file: bool,
    content: str,
    old_string: str,
    new_string: str,
    replace_all: bool,
    skill_fd: Optional[int] = None,
    support_parent_fd: Optional[int] = None,
    support_filename: Optional[str] = None,
    support_path_is_current: Optional[Any] = None,
) -> Dict[str, Any]:
    """Apply a validated patch while the caller owns any canonical dirfd."""
    from tools.fuzzy_match import fuzzy_find_and_replace

    new_content, match_count, _strategy, match_error = fuzzy_find_and_replace(
        content, old_string, new_string, replace_all
    )
    if match_error:
        preview = content[:500] + ("..." if len(content) > 500 else "")
        err_msg = match_error
        try:
            from tools.fuzzy_match import format_no_match_hint

            err_msg += format_no_match_hint(
                match_error, match_count, old_string, content
            )
        except Exception:
            pass
        return {
            "success": False,
            "error": err_msg,
            "file_preview": preview,
        }

    if patches_main_file:
        new_content = _normalize_skill_content(new_content)

    target_label = "SKILL.md" if patches_main_file else file_path
    err = _validate_content_size(new_content, label=target_label)
    if err:
        return {"success": False, "error": err}

    if patches_main_file:
        err = _validate_frontmatter(new_content, expected_name=name)
        if err:
            return {
                "success": False,
                "error": f"Patch would break SKILL.md structure: {err}",
            }

    original_content = content
    if patches_main_file:
        assert skill_fd is not None
        write_error = _secure_replace_existing_skill_md(
            existing,
            new_content,
            skill_fd=skill_fd,
        )
        if write_error:
            return {"success": False, "error": write_error}
    else:
        assert support_parent_fd is not None
        assert support_filename is not None
        _replace_held_regular_text(
            support_parent_fd,
            support_filename,
            new_content,
            require_existing=True,
        )

    if (
        not patches_main_file
        and support_path_is_current is not None
        and not support_path_is_current()
    ):
        scan_error = (
            "Supporting path changed during mutation; the write was rolled back."
        )
    else:
        scan_error = _security_scan_held_skill(skill_fd)
    if (
        not scan_error
        and not patches_main_file
        and support_path_is_current is not None
        and not support_path_is_current()
    ):
        scan_error = (
            "Supporting path changed during security scanning; "
            "the write was rolled back."
        )
    if scan_error:
        if patches_main_file:
            assert skill_fd is not None
            rollback_error = _secure_replace_existing_skill_md(
                existing,
                original_content,
                skill_fd=skill_fd,
            )
            if rollback_error:
                return {
                    "success": False,
                    "error": f"{scan_error}; rollback failed: {rollback_error}",
                }
        else:
            assert support_parent_fd is not None
            assert support_filename is not None
            _replace_held_regular_text(
                support_parent_fd,
                support_filename,
                original_content,
                require_existing=True,
            )
        return {"success": False, "error": scan_error}

    result = {
        "success": True,
        "message": (
            f"Patched {'SKILL.md' if patches_main_file else file_path} in "
            f"skill '{name}' ({match_count} replacement"
            f"{'s' if match_count > 1 else ''})."
        ),
        "_change": {
            "old": old_string[:200]
            + ("…" if len(old_string) > 200 else ""),
            "new": new_string[:200]
            + ("…" if len(new_string) > 200 else ""),
        },
    }
    org_note = _maybe_auto_propose_org_edit(name, skill_dir)
    if org_note:
        result["org_sharing"] = org_note
        result["message"] = f"{result['message']} {org_note}"
    return result


def _patch_skill(
    name: str,
    old_string: str,
    new_string: str,
    file_path: str = None,
    replace_all: bool = False,
) -> Dict[str, Any]:
    """Serialize a full targeted-update transaction for one skill."""
    try:
        with _existing_skill_mutation_lock(name) as (existing, lock_error):
            if lock_error:
                return lock_error
            assert existing is not None
            return _patch_skill_transaction(
                name,
                old_string,
                new_string,
                file_path,
                replace_all,
                existing=existing,
            )
    except OSError as exc:
        return {
            "success": False,
            "error": f"Could not reserve skill mutation: {exc}",
        }


def _patch_skill_transaction(
    name: str,
    old_string: str,
    new_string: str,
    file_path: str = None,
    replace_all: bool = False,
    *,
    existing: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Targeted find-and-replace within a skill file.

    Defaults to SKILL.md. Use file_path to patch a supporting file instead.
    Requires a unique match unless replace_all is True.
    """
    if not old_string:
        return {"success": False, "error": "old_string is required for 'patch'."}
    if new_string is None:
        return {"success": False, "error": "new_string is required for 'patch'. Use an empty string to delete matched text."}

    if existing is None:
        existing = _find_skill(name)
        lookup_error = _skill_lookup_error(existing)
        if lookup_error:
            return lookup_error
        if not existing:
            return {"success": False, "error": _skill_not_found_error(name)}

    skill_dir = existing["path"]
    org_guard = _org_mirror_write_guard(name, skill_dir, "patch")
    if org_guard:
        return org_guard
    guard = _background_review_write_guard(name, skill_dir, "patch")
    if guard:
        return guard

    patches_main_file = not file_path or _targets_skill_md(file_path, name)
    if file_path and not patches_main_file:
        # Patching a supporting file
        err = _validate_file_path(file_path)
        if err:
            return {"success": False, "error": err}
        target, err = _resolve_skill_target(skill_dir, file_path)
        if err:
            return {"success": False, "error": err}
        assert target is not None
    else:
        # Patching SKILL.md
        target = skill_dir / "SKILL.md"

    if not target.exists():
        return {"success": False, "error": f"File not found: {target.relative_to(skill_dir)}"}

    read_guard = _background_review_read_before_write_guard(
        name,
        target,
        "patch",
        "SKILL.md" if not file_path else file_path,
    )
    if read_guard:
        return read_guard

    if patches_main_file:
        try:
            with _open_existing_skill_directory(existing) as (
                skill_fd,
                _resolved_dir,
            ):
                content = _read_canonical_skill_md(skill_fd)
                return _apply_skill_patch(
                    name=name,
                    existing=existing,
                    skill_dir=skill_dir,
                    target=target,
                    file_path=file_path,
                    patches_main_file=True,
                    content=content,
                    old_string=old_string,
                    new_string=new_string,
                    replace_all=replace_all,
                    skill_fd=skill_fd,
                )
        except Exception as exc:
            return {
                "success": False,
                "error": f"Could not securely patch SKILL.md: {exc}",
            }

    try:
        assert file_path is not None
        with _open_supporting_file_parent(
            existing,
            file_path,
            create_parents=False,
        ) as (
            skill_fd,
            parent_fd,
            filename,
            _resolved_dir,
            path_is_current,
        ):
            content = _read_held_regular_text(parent_fd, filename)
            return _apply_skill_patch(
                name=name,
                existing=existing,
                skill_dir=skill_dir,
                target=target,
                file_path=file_path,
                patches_main_file=False,
                content=content,
                old_string=old_string,
                new_string=new_string,
                replace_all=replace_all,
                skill_fd=skill_fd,
                support_parent_fd=parent_fd,
                support_filename=filename,
                support_path_is_current=path_is_current,
            )
    except Exception as exc:
        return {
            "success": False,
            "error": (
                "Could not securely patch supporting file; a redirected path "
                f"may escape the skill directory: {exc}"
            ),
        }


def _delete_skill(name: str, absorbed_into: Optional[str] = None) -> Dict[str, Any]:
    """Delete a skill.

    ``absorbed_into`` declares intent:
      - ``None`` / missing  → caller didn't declare (legacy / non-curator path);
        accepted for backward compat but logs a warning because the curator
        classification pipeline can't tell consolidation from pruning without it.
      - ``""`` (empty)      → explicit "truly pruned, no forwarding target".
      - ``"<skill-name>"``  → content was absorbed into that umbrella; the
        target must exist on disk. Validated here so the model can't claim an
        umbrella that doesn't exist.
    """
    try:
        with _existing_skill_mutation_lock(name) as (existing, lock_error):
            if not lock_error:
                assert existing is not None
                return _delete_skill_transaction(
                    name, absorbed_into, existing=existing
                )
            active_error = lock_error
    except OSError as exc:
        return {
            "success": False,
            "error": f"Could not reserve skill deletion: {exc}",
        }

    # A curator delete is an archive.  If the namespace move committed before
    # strict lifecycle metadata did, retrying the same canonical/physical alias
    # must reconcile that archived directory rather than report a false miss or
    # perform another move. Foreground hard deletes deliberately do not adopt
    # pre-existing archived content.
    try:
        from tools.skill_provenance import is_background_review
        curator_pass = is_background_review()
    except Exception:
        curator_pass = False
    if not curator_pass or "not found" not in active_error["error"].lower():
        return active_error
    try:
        with _archived_skill_mutation_lock(name, _skills_dir()) as (archived, archive_error):
            if archive_error:
                return active_error
            assert archived is not None
            canonical_name = archived["_canonical_name"]
            from tools.skill_usage import is_bundled, persist_lifecycle_move_metadata_strict

            try:
                persist_lifecycle_move_metadata_strict(
                    canonical_name, "archived", suppressed=is_bundled(canonical_name)
                )
            except Exception as exc:
                return {
                    "success": False,
                    "error": (
                        "archive already moved; lifecycle metadata reconciliation "
                        f"failed: {exc}"
                    ),
                }
            return {
                "success": True,
                "message": (
                    f"Skill '{canonical_name}' was already archived; lifecycle "
                    "metadata reconciled."
                ),
                "_archived": True,
            }
    except OSError as exc:
        return {
            "success": False,
            "error": f"Could not securely reconcile archived skill deletion: {exc}",
        }


def _delete_skill_transaction(
    name: str,
    absorbed_into: Optional[str] = None,
    *,
    existing: Dict[str, Any],
) -> Dict[str, Any]:
    """Delete one skill while its alias and physical locks are held."""
    org_guard = _org_mirror_write_guard(name, existing["path"], "delete")
    if org_guard:
        return org_guard
    guard = _background_review_write_guard(name, existing["path"], "delete")
    if guard:
        return guard

    # Fail closed on unverified deletes during the curator consolidation pass.
    # A bare prune (no absorbed_into) from the LLM umbrella pass is the
    # fail-open behavior reported in #29912 — refuse it; keep the skill active.
    fail_closed = _curator_consolidation_delete_guard(name, absorbed_into)
    if fail_closed:
        return fail_closed

    pinned_err = _pinned_guard(name)
    if pinned_err:
        return {"success": False, "error": pinned_err}

    # Validate absorbed_into target when declared non-empty
    absorbed_target = (
        absorbed_into.strip()
        if absorbed_into is not None and isinstance(absorbed_into, str)
        else ""
    )
    is_consolidation = bool(absorbed_target)
    if is_consolidation:
        target_name = absorbed_target
        if target_name == name:
            return {
                "success": False,
                "error": f"absorbed_into='{target_name}' cannot equal the skill being deleted.",
            }
        target = _find_skill(target_name)
        target_lookup_error = _skill_lookup_error(target)
        if target_lookup_error:
            return target_lookup_error
        if not target:
            return {
                "success": False,
                "error": (
                    f"absorbed_into='{target_name}' does not exist. "
                    f"Create or patch the umbrella skill first, then retry the delete."
                ),
            }

    skill_dir = existing["path"]
    skills_root = _containing_skills_root(skill_dir)

    # Defense-in-depth before the recursive delete (port of Kilo Code #11240).
    unsafe = _validate_delete_target(skill_dir)
    if unsafe:
        return {"success": False, "error": unsafe}

    # During the curator consolidation pass, a verified consolidation must be
    # RECOVERABLE: archival into ~/.hermes/skills/.archive/ is documented as
    # the maximum destructive action the curator may take, and
    # `hermes curator restore` promises the skill can be brought back. Route
    # through the recoverable archive primitive instead of permanent rmtree so
    # a misjudged consolidation can be undone (#29912). Foreground,
    # user-directed deletes keep their existing hard-delete semantics.
    try:
        from tools.skill_provenance import is_background_review
        curator_pass = is_background_review()
    except Exception:
        curator_pass = False

    if curator_pass:
        try:
            ok, archive_msg = _secure_archive_existing_skill(
                existing, skills_root
            )
        except Exception as e:
            return {"success": False, "error": f"failed to archive '{name}': {e}"}
        if not ok:
            return {"success": False, "error": archive_msg}
        message = f"Skill '{name}' archived ({archive_msg})."
        if is_consolidation:
            message += f" Content absorbed into '{absorbed_target}'."
        return {"success": True, "message": message, "_archived": True}

    try:
        _secure_delete_existing_skill(existing, skills_root)
    except OSError as exc:
        return {
            "success": False,
            "error": f"Could not securely delete skill '{name}': {exc}",
        }

    message = f"Skill '{name}' deleted."
    if is_consolidation:
        message += f" Content absorbed into '{absorbed_target}'."

    return {
        "success": True,
        "message": message,
    }


def _write_file(name: str, file_path: str, file_content: str) -> Dict[str, Any]:
    """Serialize supporting or canonical writes for one skill."""
    try:
        with _existing_skill_mutation_lock(name) as (existing, lock_error):
            if lock_error:
                return lock_error
            assert existing is not None
            return _write_file_transaction(
                name,
                file_path,
                file_content,
                existing=existing,
            )
    except OSError as exc:
        return {
            "success": False,
            "error": f"Could not reserve skill mutation: {exc}",
        }


def _write_file_transaction(
    name: str,
    file_path: str,
    file_content: str,
    *,
    existing: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Add or overwrite a supporting file within any skill directory."""
    err = _validate_file_path(file_path, skill_name=name)
    if err:
        return {"success": False, "error": err}

    if not file_content and file_content != "":
        return {"success": False, "error": "file_content is required."}

    # Keep the canonical file on the same validated full-rewrite path as edit;
    # write_file is otherwise only a supporting-file convenience.
    if _targets_skill_md(file_path, name):
        return _edit_skill_transaction(
            name,
            file_content,
            existing=existing,
        )

    # Check size limits
    content_bytes = len(file_content.encode("utf-8"))
    if content_bytes > MAX_SKILL_FILE_BYTES:
        return {
            "success": False,
            "error": (
                f"File content is {content_bytes:,} bytes "
                f"(limit: {MAX_SKILL_FILE_BYTES:,} bytes / 1 MiB). "
                f"Consider splitting into smaller files."
            ),
        }
    err = _validate_content_size(file_content, label=file_path)
    if err:
        return {"success": False, "error": err}

    if existing is None:
        existing = _find_skill(name)
        lookup_error = _skill_lookup_error(existing)
        if lookup_error:
            return lookup_error
        if not existing:
            return {"success": False, "error": _skill_not_found_error(name, " Create it first with action='create'.")}
    org_guard = _org_mirror_write_guard(name, existing["path"], "write_file")
    if org_guard:
        return org_guard
    guard = _background_review_write_guard(name, existing["path"], "write_file")
    if guard:
        return guard

    target, err = _resolve_skill_target(existing["path"], file_path)
    if err:
        return {"success": False, "error": err}
    assert target is not None
    if target.exists():
        read_guard = _background_review_read_before_write_guard(
            name, target, "write_file", file_path
        )
        if read_guard:
            return read_guard
    try:
        with _open_supporting_file_parent(
            existing,
            file_path,
            create_parents=True,
        ) as (
            skill_fd,
            parent_fd,
            filename,
            _resolved_dir,
            path_is_current,
        ):
            try:
                original_content = _read_held_regular_text(
                    parent_fd, filename
                )
            except FileNotFoundError:
                original_content = None

            _replace_held_regular_text(
                parent_fd,
                filename,
                file_content,
                require_existing=False,
            )

            # Scan and rollback remain anchored to the held skill and parent
            # directory descriptors even if a checked path is retargeted.
            if not path_is_current():
                scan_error = (
                    "Supporting path changed during mutation; "
                    "the write was rolled back."
                )
            else:
                scan_error = _security_scan_held_skill(skill_fd)
            if not scan_error and not path_is_current():
                scan_error = (
                    "Supporting path changed during security scanning; "
                    "the write was rolled back."
                )
            if scan_error:
                try:
                    if original_content is not None:
                        _replace_held_regular_text(
                            parent_fd,
                            filename,
                            original_content,
                            require_existing=True,
                        )
                    else:
                        _remove_held_regular_file(parent_fd, filename)
                except Exception as rollback_exc:
                    return {
                        "success": False,
                        "error": (
                            f"{scan_error}; rollback failed: {rollback_exc}"
                        ),
                    }
                return {"success": False, "error": scan_error}
    except Exception as exc:
        return {
            "success": False,
            "error": (
                "Could not securely write supporting file; a redirected path "
                f"may escape the skill directory: {exc}"
            ),
        }

    result = {
        "success": True,
        "message": f"File '{file_path}' written to skill '{name}'.",
        "path": str(target),
    }
    org_note = _maybe_auto_propose_org_edit(name, existing["path"])
    if org_note:
        result["org_sharing"] = org_note
        result["message"] = f"{result['message']} {org_note}"
    return result


def _remove_file(name: str, file_path: str) -> Dict[str, Any]:
    """Remove a supporting file from any skill directory."""
    err = _validate_file_path(file_path, skill_name=name)
    if err:
        return {"success": False, "error": err}
    if _targets_skill_md(file_path, name):
        return {
            "success": False,
            "error": (
                "SKILL.md cannot be removed as a supporting file; use the "
                "'delete' action to remove the whole skill."
            ),
        }

    try:
        with _existing_skill_mutation_lock(name) as (existing, lock_error):
            if lock_error:
                return lock_error
            assert existing is not None
            return _remove_file_transaction(name, file_path, existing=existing)
    except OSError as exc:
        return {
            "success": False,
            "error": f"Could not reserve skill mutation: {exc}",
        }


def _remove_file_transaction(
    name: str,
    file_path: str,
    *,
    existing: Dict[str, Any],
) -> Dict[str, Any]:
    """Remove a supporting file while its alias and physical locks are held."""

    skill_dir = existing["path"]
    guard = _background_review_write_guard(name, skill_dir, "remove_file")
    if guard:
        return guard

    target, err = _resolve_skill_target(skill_dir, file_path)
    if err:
        return {"success": False, "error": err}
    assert target is not None
    if not target.exists():
        # Keep error payloads under the same deterministic preview budget as
        # skill_view and slash invocation messages.
        from tools.skills_tool import build_linked_files_manifest

        available_by_category, files_summary = build_linked_files_manifest(
            skill_dir
        )
        available = [
            path
            for category in sorted(available_by_category)
            for path in available_by_category[category]
        ]
        return {
            "success": False,
            "error": f"File '{file_path}' not found in skill '{name}'.",
            "available_files": available if available else None,
            "linked_files_summary": files_summary,
        }

    read_guard = _background_review_read_before_write_guard(
        name, target, "remove_file", file_path
    )
    if read_guard:
        return read_guard

    try:
        with _open_supporting_file_parent(
            existing,
            file_path,
            create_parents=False,
        ) as (
            _skill_fd,
            parent_fd,
            filename,
            resolved_dir,
            path_is_current,
        ):
            if os.name == "nt":
                try:
                    with parent_fd.open_file(
                        filename, writable=False
                    ) as target_handle:
                        target_is_regular = target_handle.stat().is_file
                except (FileNotFoundError, IsADirectoryError):
                    target_is_regular = False
            else:
                target_stat = os.stat(
                    filename, dir_fd=parent_fd, follow_symlinks=False
                )
                target_is_regular = stat.S_ISREG(target_stat.st_mode)
            if not target_is_regular:
                return {
                    "success": False,
                    "error": "Supporting target must be a regular file.",
                }
            if not path_is_current():
                return {
                    "success": False,
                    "error": "Supporting path changed before removal.",
                }
            _remove_held_regular_file(parent_fd, filename)
            try:
                _secure_cleanup_empty_support_parent(
                    parent_fd, resolved_dir, file_path
                )
            except OSError as exc:
                # The requested deletion has already committed. Empty-folder
                # cleanup is convenience only; never report that completed
                # mutation as a failure just because cleanup raced or failed.
                logger.debug(
                    "Could not clean empty supporting directory for %s: %s",
                    file_path,
                    exc,
                )
    except FileNotFoundError:
        return {
            "success": False,
            "error": f"File '{file_path}' not found in skill '{name}'.",
        }
    except OSError as exc:
        return {
            "success": False,
            "error": (
                "Could not securely remove supporting file; a redirected path "
                f"may escape the skill directory: {exc}"
            ),
        }

    return {
        "success": True,
        "message": f"File '{file_path}' removed from skill '{name}'.",
    }


# =============================================================================
# Main entry point
# =============================================================================

# ContextVar bypass: set while replaying an already-approved staged skill write
# so skill_manage() does not re-gate (and re-stage) it.
import contextvars as _ctxvars
_skill_gate_bypass: "_ctxvars.ContextVar[bool]" = _ctxvars.ContextVar(
    "skill_gate_bypass", default=False
)


def _apply_skill_write_gate(action, name, **payload_kwargs):
    """Evaluate the skill write gate. Returns a JSON tool-result string when the
    write should NOT proceed (blocked or staged), or None to perform the real
    write. Bypassed during approved-pending replay.
    """
    if action not in {"create", "edit", "patch", "delete", "write_file", "remove_file"}:
        return None
    if _skill_gate_bypass.get():
        return None

    try:
        from tools import write_approval as wa
    except Exception:
        return None  # fail open

    decision = wa.evaluate_gate(wa.SKILLS)
    if decision.allow:
        return None
    if decision.blocked:
        return tool_error(decision.message, success=False)

    # stage — record the full skill_manage kwargs so approval can replay it.
    payload = {"action": action, "name": name}
    payload.update({k: v for k, v in payload_kwargs.items() if v is not None})
    gist = wa.skill_gist(
        action, name,
        content=payload_kwargs.get("content") or "",
        file_path=payload_kwargs.get("file_path") or "",
        old_string=payload_kwargs.get("old_string") or "",
        new_string=payload_kwargs.get("new_string") or "",
    )
    record = wa.stage_write(wa.SKILLS, payload, summary=gist, origin=wa.current_origin())
    return json.dumps(
        {"success": True, "staged": True, "pending_id": record["id"],
         "gist": gist, "message": decision.message},
        ensure_ascii=False,
    )


def apply_skill_pending(payload: Dict[str, Any]) -> str:
    """Replay a staged skill write, bypassing the gate. Returns the tool result
    JSON string. Called by the /skills approve handler.
    """
    token = _skill_gate_bypass.set(True)
    try:
        return skill_manage(
            action=payload.get("action", ""),
            name=payload.get("name", ""),
            content=payload.get("content"),
            category=payload.get("category"),
            file_path=payload.get("file_path"),
            file_content=payload.get("file_content"),
            old_string=payload.get("old_string"),
            new_string=payload.get("new_string"),
            replace_all=payload.get("replace_all", False),
            absorbed_into=payload.get("absorbed_into"),
        )
    finally:
        _skill_gate_bypass.reset(token)


# Debounce state for the sync push hook. A burst of skill_manage writes
# (e.g. create + several write_file calls) collapses into a single push after
# a short quiet window, on a daemon timer so the agent write never blocks.
_sync_push_timer = None
_sync_push_lock = None
_SYNC_PUSH_DEBOUNCE_S = 5.0


def _maybe_debounced_sync_push(skill_name: str) -> None:
    """Schedule a debounced best-effort sync push after a skill write.

    Cheap fast-path: if the skill isn't opted into sync, do nothing (no auth,
    no network). Otherwise (re)arm a daemon timer; the actual push runs through
    ``skills_sync_client.maybe_push_skills`` which enforces the access gate
    and swallows all errors. Never blocks the caller (M1-C: agent never blocks
    on sync).
    """
    global _sync_push_timer, _sync_push_lock
    try:
        from tools.skill_usage import is_sync_enabled

        if not is_sync_enabled(skill_name):
            return
    except Exception:
        return

    import threading

    if _sync_push_lock is None:
        _sync_push_lock = threading.Lock()

    def _fire():
        try:
            from tools.skills_sync_client import maybe_push_skills

            maybe_push_skills(message=f"sync: {skill_name}")
        except Exception:
            pass

    with _sync_push_lock:
        if _sync_push_timer is not None:
            try:
                _sync_push_timer.cancel()
            except Exception:
                pass
        _sync_push_timer = threading.Timer(_SYNC_PUSH_DEBOUNCE_S, _fire)
        _sync_push_timer.daemon = True
        _sync_push_timer.start()


def skill_manage(
    action: str,
    name: str,
    content: str = None,
    category: str = None,
    file_path: str = None,
    file_content: str = None,
    old_string: str = None,
    new_string: str = None,
    replace_all: bool = False,
    absorbed_into: str = None,
) -> str:
    """
    Manage user-created skills. Dispatches to the appropriate action handler.

    Returns JSON string with results.
    """
    preflight = _background_review_preflight(action, name)
    if preflight is not None:
        return json.dumps(preflight, ensure_ascii=False)

    # Approval gate: when on, stages the write for review (skills are too large
    # to review inline, so they always stage regardless of origin); when off
    # (default) passes straight through. The gate is bypassed when this call is
    # itself replaying an already-approved staged write (_skill_apply_pending).
    gate_result = _apply_skill_write_gate(
        action, name, content=content, category=category,
        file_path=file_path, file_content=file_content,
        old_string=old_string, new_string=new_string,
        replace_all=replace_all, absorbed_into=absorbed_into,
    )
    if gate_result is not None:
        return gate_result

    if action == "create":
        if not content:
            return tool_error("content is required for 'create'. Provide the full SKILL.md text (frontmatter + body).", success=False)
        result = _create_skill(name, content, category)

    elif action == "edit":
        if not content:
            return tool_error("content is required for 'edit'. Provide the full updated SKILL.md text.", success=False)
        result = _edit_skill(name, content)

    elif action == "patch":
        if not old_string:
            return tool_error("old_string is required for 'patch'. Provide the text to find.", success=False)
        if new_string is None:
            return tool_error("new_string is required for 'patch'. Use empty string to delete matched text.", success=False)
        result = _patch_skill(name, old_string, new_string, file_path, replace_all)

    elif action == "delete":
        result = _delete_skill(name, absorbed_into=absorbed_into)

    elif action == "write_file":
        if not file_path:
            return tool_error("file_path is required for 'write_file'. Example: 'references/api-guide.md'", success=False)
        if file_content is None:
            return tool_error("file_content is required for 'write_file'.", success=False)
        result = _write_file(name, file_path, file_content)

    elif action == "remove_file":
        if not file_path:
            return tool_error("file_path is required for 'remove_file'.", success=False)
        result = _remove_file(name, file_path)

    else:
        result = {"success": False, "error": f"Unknown action '{action}'. Use: create, edit, patch, delete, write_file, remove_file"}

    if result.get("success"):
        try:
            from agent.prompt_builder import clear_skills_system_prompt_cache
            clear_skills_system_prompt_cache(clear_snapshot=True)
        except Exception:
            pass
        # Curator telemetry: bump patch_count on edit/patch/write_file (the actions
        # that mutate an existing skill's guidance), drop the record on delete.
        # Only mark a skill as agent-created when the background self-improvement
        # review fork creates it — foreground `skill_manage(create)` calls are
        # user-directed, and those skills belong to the user (the curator must
        # not touch them). Best-effort; telemetry failures never break the tool.
        try:
            from tools.skill_usage import bump_patch, forget, mark_agent_created
            from tools.skill_provenance import is_background_review
            if action == "create":
                if is_background_review():
                    mark_agent_created(name)
            elif action in {"patch", "edit", "write_file", "remove_file"}:
                bump_patch(name)
            elif action == "delete":
                # A recoverable curator archive (routed through archive_skill)
                # keeps its usage record as STATE_ARCHIVED so `hermes curator
                # status`/`restore` still see it. Only a hard delete forgets.
                if not result.get("_archived"):
                    forget(name)
        except Exception:
            pass

        # Sync push hook (debounced, best-effort). Fires only AFTER the
        # write gate passed (staged/unapproved writes never reach here -- the
        # gate returns early above), so we never push un-reviewed content.
        # Inert unless the access gate is open (the user is a Nous admin on the
        # token), a sync base URL is configured, and the skill is opted into
        # sync. Debounced so a burst of edits collapses to one push. Never
        # raises -- an agent write must never block on sync (M1-C invariant).
        try:
            _maybe_debounced_sync_push(name)
        except Exception:
            pass

    return json.dumps(result, ensure_ascii=False)


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

SKILL_MANAGE_SCHEMA = {
    "name": "skill_manage",
    "description": (
        "Manage skills (create, update, delete). Skills are your procedural "
        "memory — reusable approaches for recurring task types. "
        f"New skills go to {display_hermes_home()}/skills/; existing skills can be modified wherever they live.\n\n"
        "Actions: create (full SKILL.md + optional category), "
        "patch (old_string/new_string — preferred for fixes), "
        "edit (full SKILL.md rewrite — major overhauls only), "
        "delete, write_file, remove_file.\n\n"
        "On delete, pass `absorbed_into=<umbrella>` when you're merging this "
        "skill's content into another one, or `absorbed_into=\"\"` when you're "
        "pruning it with no forwarding target. This lets the curator tell "
        "consolidation from pruning without guessing, so downstream consumers "
        "(cron jobs that reference the old skill name, etc.) get updated "
        "correctly. The target you name in `absorbed_into` must already "
        "exist — create/patch the umbrella first, then delete.\n\n"
        "Create when: complex task succeeded (5+ calls), errors overcome, "
        "user-corrected approach worked, non-trivial workflow discovered, "
        "or user asks you to remember a procedure.\n"
        "Update when: instructions stale/wrong, OS-specific failures, "
        "missing steps or pitfalls found during use. "
        "If you used a skill and hit issues not covered by it, patch it immediately.\n\n"
        "After difficult/iterative tasks, offer to save as a skill. "
        "Skip for simple one-offs. Confirm with user before creating/deleting.\n\n"
        "Generate the smallest reusable skill that captures the proven workflow. "
        "Put routing signal in the description; put procedures in the body; move "
        "large examples or reference material into supporting files. Good bodies "
        "use imperative steps, explicit prerequisites, pitfalls, and verification. "
        "Use skill_view() to inspect nearby examples before generating.\n\n"
        "For create, SKILL.md frontmatter `name` must exactly match the tool's "
        "`name`, and `description` must be a non-empty string of at most 60 "
        "characters. Make the description a concise capability/trigger sentence "
        "whose routing signal survives in the skill index. Do not duplicate the "
        "procedure in the description.\n\n"
        "Pinned skills are protected from deletion only — skill_manage(action='delete') "
        "will refuse with a message pointing the user to `hermes curator unpin <name>`. "
        "Patches and edits go through on pinned skills so you can still improve them as "
        "pitfalls come up; pin only guards against irrecoverable loss."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "patch", "edit", "delete", "write_file", "remove_file"],
                "description": "The action to perform."
            },
            "name": {
                "type": "string",
                "description": (
                    "Skill name (lowercase, hyphens/underscores, max 64 chars). "
                    "Must match an existing skill for patch/edit/delete/write_file/remove_file."
                )
            },
            "content": {
                "type": "string",
                "description": (
                    "Full SKILL.md content (YAML frontmatter + markdown body). "
                    "Required for 'create' and 'edit'. On create, frontmatter name "
                    "must exactly equal the `name` argument and description must be "
                    "a non-empty routing sentence of at most 60 characters. For "
                    "'edit', read the skill first with skill_view() and provide the "
                    "complete updated text."
                )
            },
            "old_string": {
                "type": "string",
                "description": (
                    "Text to find in the file (required for 'patch'). Must be unique "
                    "unless replace_all=true. Include enough surrounding context to "
                    "ensure uniqueness."
                )
            },
            "new_string": {
                "type": "string",
                "description": (
                    "Replacement text (required for 'patch'). Can be empty string "
                    "to delete the matched text."
                )
            },
            "replace_all": {
                "type": "boolean",
                "description": "For 'patch': replace all occurrences instead of requiring a unique match (default: false)."
            },
            "category": {
                "type": "string",
                "description": (
                    "Optional category/domain for organizing the skill (e.g., 'devops', "
                    "'data-science', 'mlops'). Creates a subdirectory grouping. "
                    "Only used with 'create'."
                )
            },
            "file_path": {
                "type": "string",
                "description": (
                    "Path to a supporting file within the skill directory. "
                    "For 'write_file'/'remove_file': required, must be under references/, "
                    "templates/, scripts/, or assets/. "
                    "For 'patch': optional, defaults to SKILL.md if omitted."
                )
            },
            "file_content": {
                "type": "string",
                "description": "Content for the file. Required for 'write_file'."
            },
            "absorbed_into": {
                "type": "string",
                "description": (
                    "For 'delete' only — declares intent so the curator can "
                    "tell consolidation from pruning without guessing. "
                    "Pass the umbrella skill name when this skill's content "
                    "was merged into another (the target must already exist). "
                    "Pass an empty string when the skill is truly stale and "
                    "being pruned with no forwarding target. Omitting the arg "
                    "on delete is supported for backward compatibility but "
                    "downstream tooling (e.g. cron-job skill reference "
                    "rewriting) will have to guess at intent."
                )
            },
        },
        "required": ["action", "name"],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="skill_manage",
    toolset="skills",
    schema=SKILL_MANAGE_SCHEMA,
    handler=lambda args, **kw: skill_manage(
        action=args.get("action", ""),
        name=args.get("name", ""),
        content=args.get("content"),
        category=args.get("category"),
        file_path=args.get("file_path"),
        file_content=args.get("file_content"),
        old_string=args.get("old_string"),
        new_string=args.get("new_string"),
        replace_all=args.get("replace_all", False),
        absorbed_into=args.get("absorbed_into")),
    emoji="📝",
)
