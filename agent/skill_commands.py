"""Shared slash command helpers for skills.

Shared between CLI (cli.py) and gateway (gateway/run.py) so both surfaces
can invoke skills via /skill-name commands.
"""

import json
import logging
import os
import re
import stat
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_constants import display_hermes_home
from agent.skill_preprocessing import (
    expand_inline_shell as _expand_inline_shell,
    load_skills_config as _load_skills_config,
    substitute_template_vars as _substitute_template_vars,
)

logger = logging.getLogger(__name__)

_skill_commands: Dict[str, Dict[str, Any]] = {}
_skill_commands_platform: Optional[str] = None
_skill_commands_environment: tuple[tuple[str, bool], ...] | None = None
# The catalog also depends on the active profile's local skills root and its
# configured external roots.  Keep that part separate from the historical
# platform/environment fields so older integrations that inspect those fields
# retain their meaning.
_skill_commands_roots: tuple[str, ...] | None = None
# A gateway can serve distinct profile ContextVars concurrently.  Serialise the
# resolve -> walk -> commit sequence so one request cannot commit a catalog
# under another request's scope while it is still scanning.
_skill_commands_lock = threading.RLock()
# Patterns for sanitizing skill names into clean hyphen-separated slugs.
_SKILL_INVALID_CHARS = re.compile(r"[^a-z0-9-]")
_SKILL_MULTI_HYPHEN = re.compile(r"-{2,}")

# ---------------------------------------------------------------------------
# Skill-scaffolding markers and the canonical extractor.
#
# When a user invokes a /skill (or /bundle), Hermes expands the turn into a
# model-facing message that embeds the full skill body plus scaffolding. That
# expanded text is what flows into the agent loop — and into memory providers
# via MemoryManager. Providers that store or embed the raw user turn (mem0,
# openviking, hindsight, retaindb, byterover, honcho, supermemory) would
# otherwise capture the entire skill body instead of what the user actually
# asked. ``extract_user_instruction_from_skill_message`` recovers just the
# user's instruction so memory stays clean.
#
# These markers MUST stay byte-identical to the builders below
# (``_build_skill_message`` here, ``build_bundle_invocation_message`` in
# agent/skill_bundles.py). They are co-located with the single-skill builder
# on purpose, and the bundle markers are asserted against the bundle builder in
# tests/openviking_plugin/test_openviking.py::test_skill_markers_match_hermes_scaffolding.
# ---------------------------------------------------------------------------
_SKILL_INVOCATION_PREFIX = "[IMPORTANT: The user has invoked the "
_SINGLE_SKILL_MARKER = "The full skill content is loaded below.]"
_SINGLE_SKILL_INSTRUCTION = (
    "The user has provided the following instruction alongside the skill invocation: "
)
_RUNTIME_NOTE = "\n\n[Runtime note:"
_BUNDLE_MARKER = " skill bundle,"
_BUNDLE_USER_INSTRUCTION = "\nUser instruction: "
_BUNDLE_FIRST_SKILL_BLOCK = "\n\n[Loaded as part of the "

# The skill name sits in the first quoted span of the activation note, for both
# the single-skill and the bundle header ("work" / "/clean /work").
_SKILL_NAME_RE = re.compile(re.escape(_SKILL_INVOCATION_PREFIX) + r'"([^"]*)"')

# SQL LIKE pattern matching a skill-expanded turn, for listing queries that
# have to recognize scaffolding before the row reaches Python. The prefix
# contains no LIKE wildcards (`%`, `_`), so it needs no ESCAPE clause.
SKILL_SCAFFOLD_SQL_LIKE = _SKILL_INVOCATION_PREFIX + "%"

# Marks where a preview query joined the head and tail of a long scaffolded
# message. ``describe_skill_invocation`` may hand back a span that runs across
# the joint (a bundle instruction cut off by the head window); callers cut the
# description there rather than show the skill body on the far side.
SKILL_EXCERPT_JOINT = "\x1e"


def extract_user_instruction_from_skill_message(content: Any) -> Optional[str]:
    """Recover the user's instruction from a slash-skill-expanded turn.

    Returns:
        - The original string unchanged when it is NOT skill scaffolding
          (a normal user message passes straight through).
        - The extracted user instruction when the scaffolding carried one.
        - ``None`` when the content is skill scaffolding with no user
          instruction (i.e. a bare ``/skill`` invocation). Callers that feed
          memory providers should skip the turn in that case — there is no
          user content worth storing.
    """
    if not isinstance(content, str):
        return None

    if not content.startswith(_SKILL_INVOCATION_PREFIX):
        return content

    if _BUNDLE_MARKER in content:
        return _extract_bundle_user_instruction(content)

    if _SINGLE_SKILL_MARKER in content:
        return _extract_single_skill_user_instruction(content)

    return None


def describe_skill_invocation(content: Any, separator: str = " — ") -> Optional[str]:
    """Render a slash-skill-expanded turn the way the user typed it.

    The expanded message embeds the whole skill body, so any surface that
    summarizes a user turn from its raw content — session titles, sidebar
    previews, the ``/rewind`` picker — otherwise shows the skill's own prose
    as if the user had written it. That is how a skill's opening line ends up
    as a session title.

    Returns ``"/work — fix the title leak"``, or ``"/work"`` for a bare
    invocation, or ``None`` when *content* is not skill scaffolding (the
    caller should then summarize it as an ordinary message).

    *separator* joins the command and the instruction. Previews use the
    default em dash; pass ``" "`` for the literal invocation the user typed,
    which is what chat transcripts render.
    """
    if not isinstance(content, str) or not content.startswith(_SKILL_INVOCATION_PREFIX):
        return None

    match = _SKILL_NAME_RE.match(content)
    name = (match.group(1) if match else "").strip()
    # Bundle headers already carry their typed "/a /b" keys; a single skill is
    # a bare name.
    label = name if name.startswith("/") else f"/{name}"

    instruction = extract_user_instruction_from_skill_message(content)
    if instruction and instruction is not content:
        # An excerpted message (head + tail, joined by SKILL_EXCERPT_JOINT) can
        # put the joint inside the matched span — keep only the side the
        # instruction marker was found on.
        instruction = instruction.split(SKILL_EXCERPT_JOINT)[0]
        instruction = " ".join(instruction.split())
        if instruction:
            return f"{label}{separator}{instruction}" if name else instruction

    return label if name else None


def _extract_single_skill_user_instruction(message: str) -> Optional[str]:
    # Single-skill format appends the user instruction after the skill body, so
    # the last occurrence is the user-provided one; the body may quote this text.
    marker_idx = message.rfind(_SINGLE_SKILL_INSTRUCTION)
    if marker_idx < 0:
        return None

    instruction = message[marker_idx + len(_SINGLE_SKILL_INSTRUCTION):]
    runtime_idx = instruction.find(_RUNTIME_NOTE)
    if runtime_idx >= 0:
        instruction = instruction[:runtime_idx]
    instruction = instruction.strip()
    return instruction or None


def _extract_bundle_user_instruction(message: str) -> Optional[str]:
    # Bundle format puts the user instruction before the loaded skills, so the
    # first occurrence is the user-provided one.
    marker_idx = message.find(_BUNDLE_USER_INSTRUCTION)
    if marker_idx < 0:
        return None

    instruction = message[marker_idx + len(_BUNDLE_USER_INSTRUCTION):]
    first_skill_idx = instruction.find(_BUNDLE_FIRST_SKILL_BLOCK)
    if first_skill_idx >= 0:
        instruction = instruction[:first_skill_idx]
    instruction = instruction.strip()
    return instruction or None


def _resolve_skill_commands_platform() -> Optional[str]:
    """Return the current platform scope used for disabled-skill filtering.

    Used to detect when the active platform has shifted so
    :func:`get_skill_commands` can drop a stale cache that was populated
    for a different platform's ``skills.platform_disabled`` view (#14536).

    Resolves from (in order) ``HERMES_PLATFORM`` env var and
    ``HERMES_SESSION_PLATFORM`` from the gateway session context. Returns
    ``None`` when no platform scope is active (e.g. classic CLI, RL
    rollouts, standalone scripts).
    """
    try:
        from gateway.session_context import get_session_env

        resolved_platform = (
            os.getenv("HERMES_PLATFORM")
            or get_session_env("HERMES_SESSION_PLATFORM")
        )
    except Exception:
        resolved_platform = os.getenv("HERMES_PLATFORM")
    return resolved_platform or None


def _resolve_skill_commands_environment() -> tuple[tuple[str, bool], ...]:
    """Return the offer-time environment state used by the slash catalog."""
    try:
        from agent.skill_utils import get_skill_environment_fingerprint

        return get_skill_environment_fingerprint()
    except Exception:
        return ()


def _canonical_skill_root(path: Path) -> str:
    """Return a stable, comparison-safe spelling of a configured root."""
    try:
        return str(path.expanduser().resolve())
    except (OSError, RuntimeError):
        return str(path.expanduser().absolute())


def _resolve_skill_command_roots() -> tuple[Path, tuple[Path, ...], tuple[str, ...]]:
    """Snapshot the live local and external roots for one catalog scan.

    ``tools.skills_tool.SKILLS_DIR`` is a legacy import-time compatibility
    attribute.  Its ``_skills_dir()`` helper resolves the active
    profile/HERMES_HOME at call time (while continuing to honour patched
    ``SKILLS_DIR`` in tests and integrations), so slash discovery must use it
    too.  Return the actual paths alongside their fingerprint to ensure the
    scan walks exactly the roots it compared and later commits.
    """
    from agent.skill_utils import get_external_skills_dirs
    from tools.skills_tool import _skills_dir

    primary_root = Path(_skills_dir())
    external_roots = tuple(Path(path) for path in get_external_skills_dirs())
    roots = (primary_root, *external_roots)
    return primary_root, external_roots, tuple(
        _canonical_skill_root(root) for root in roots
    )


def _skill_command_roots_are_accessible(
    primary_root: Path, external_roots: tuple[Path, ...]
) -> bool:
    """Return whether a cached catalog can still describe this exact root scope.

    A missing *primary* root is the normal empty-local-skills case.  Every
    other error, and a missing configured external root, means this call cannot
    prove that its catalog is complete.  In particular, ``Path.exists()`` is
    deliberately not used here because it turns permission failures into
    ``False`` and would let an external-only catalog replace or reuse a
    complete one.
    """
    try:
        primary_stat = primary_root.stat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Primary skill root is inaccessible: %s", exc)
        return False
    else:
        if not stat.S_ISDIR(primary_stat.st_mode):
            logger.warning("Primary skill root is not a directory: %s", primary_root)
            return False

    for external_root in external_roots:
        try:
            external_stat = external_root.stat()
        except OSError as exc:
            # An external root was part of this resolved scope.  Treat removal
            # the same as denial: returning a cached map would advertise skills
            # that may no longer be available.
            logger.warning("External skill root is inaccessible: %s", exc)
            return False
        if not stat.S_ISDIR(external_stat.st_mode):
            logger.warning(
                "External skill root is not a directory: %s", external_root
            )
            return False
    return True

def _load_skill_payload(skill_identifier: str, task_id: str | None = None) -> tuple[dict[str, Any], Path | None, str] | None:
    """Load a skill by name/path and return (loaded_payload, skill_dir, display_name)."""
    raw_identifier = (skill_identifier or "").strip()
    if not raw_identifier:
        return None

    try:
        from tools.skills_tool import _skills_dir, skill_view
        from agent.skill_utils import normalize_skill_lookup_name

        normalized = normalize_skill_lookup_name(raw_identifier)

        # ``skill_view`` owns the package snapshot for every modern skill.
        # Keep preprocessing there too: it can expand inline shell while its
        # dirfd still names the inspected package.  Re-rendering below with a
        # ``Path`` would reopen a directory an attacker could replace after
        # this call returned (slash, stacked, and preload all use this path).
        loaded_skill = json.loads(skill_view(normalized, task_id=task_id))
    except Exception:
        return None

    if not loaded_skill.get("success"):
        return None

    skill_name = str(loaded_skill.get("name") or normalized)
    skill_path = str(loaded_skill.get("path") or "")
    skill_dir = None
    # Prefer the absolute skill_dir returned by skill_view() — this is
    # correct for both local and external skills.  Fall back to the old
    # SKILLS_DIR-relative reconstruction only when skill_dir is absent
    # (e.g. legacy skill_view responses).
    abs_skill_dir = loaded_skill.get("skill_dir")
    if abs_skill_dir:
        skill_dir = Path(abs_skill_dir)
    elif skill_path:
        try:
            skill_dir = _skills_dir() / Path(skill_path).parent
        except Exception:
            skill_dir = None

    return loaded_skill, skill_dir, skill_name


def _inject_skill_config(loaded_skill: dict[str, Any], parts: list[str]) -> None:
    """Resolve and inject skill-declared config values into the message parts.

    If the loaded skill's frontmatter declares ``metadata.hermes.config``
    entries, their current values (from config.yaml or defaults) are appended
    as a ``[Skill config: ...]`` block so the agent knows the configured values
    without needing to read config.yaml itself.
    """
    try:
        from agent.skill_utils import (
            extract_skill_config_vars,
            parse_frontmatter,
            resolve_skill_config_values,
        )

        # The loaded_skill dict contains the raw content which includes frontmatter
        raw_content = str(loaded_skill.get("raw_content") or loaded_skill.get("content") or "")
        if not raw_content:
            return

        frontmatter, _ = parse_frontmatter(raw_content)
        config_vars = extract_skill_config_vars(frontmatter)
        if not config_vars:
            return

        resolved = resolve_skill_config_values(config_vars)
        if not resolved:
            return

        lines = ["", f"[Skill config (from {display_hermes_home()}/config.yaml):"]
        for key, value in resolved.items():
            display_val = str(value) if value else "(not set)"
            lines.append(f"  {key} = {display_val}")
        lines.append("]")
        parts.extend(lines)
    except Exception:
        pass  # Non-critical — skill still loads without config injection


def _build_skill_message(
    loaded_skill: dict[str, Any],
    skill_dir: Path | None,
    activation_note: str,
    user_instruction: str = "",
    runtime_note: str = "",
    session_id: str | None = None,
) -> str:
    """Format a loaded skill into a user/system message payload."""
    from tools.skills_tool import _skills_dir

    content = str(loaded_skill.get("content") or "")

    # Modern skill_view payloads were rendered while their package dirfd was
    # bound to the discovery snapshot.  Do not use the returned path to run
    # inline shell after that fd has been closed.  Keep this legacy fallback
    # for integrations which construct a payload directly.
    if not loaded_skill.get("preprocessed"):
        skills_cfg = _load_skills_config()
        if skills_cfg.get("template_vars", True):
            content = _substitute_template_vars(content, skill_dir, session_id)
        if skills_cfg.get("inline_shell", False):
            timeout = int(skills_cfg.get("inline_shell_timeout", 10) or 10)
            content = _expand_inline_shell(content, skill_dir, timeout)

    parts = [activation_note, "", content.strip()]

    # ── Inject the absolute skill directory so the agent can reference
    #    bundled scripts without an extra skill_view() round-trip. ──
    if skill_dir and not loaded_skill.get("package_bound"):
        parts.append("")
        parts.append(f"[Skill directory: {skill_dir}]")
        parts.append(
            "Resolve any relative paths in this skill (e.g. `scripts/foo.js`, "
            "`templates/config.yaml`) against that directory, then run them "
            "with the terminal tool using the absolute path."
        )

    # ── Inject resolved skill config values ──
    _inject_skill_config(loaded_skill, parts)

    if loaded_skill.get("setup_skipped"):
        parts.extend(
            [
                "",
                "[Skill setup note: Required environment setup was skipped. Continue loading the skill and explain any reduced functionality if it matters.]",
            ]
        )
    elif loaded_skill.get("gateway_setup_hint"):
        parts.extend(
            [
                "",
                f"[Skill setup note: {loaded_skill['gateway_setup_hint']}]",
            ]
        )
    elif loaded_skill.get("setup_needed") and loaded_skill.get("setup_note"):
        parts.extend(
            [
                "",
                f"[Skill setup note: {loaded_skill['setup_note']}]",
            ]
        )

    supporting = []
    linked_files = loaded_skill.get("linked_files") or {}
    for entries in linked_files.values():
        if isinstance(entries, list):
            supporting.extend(entries)

    if not supporting and skill_dir and not loaded_skill.get("package_bound"):
        try:
            from tools.skills_tool import build_linked_files_manifest

            linked_files, fallback_summary = build_linked_files_manifest(skill_dir)
            for entries in linked_files.values():
                supporting.extend(entries)
            if not loaded_skill.get("linked_files_summary"):
                loaded_skill["linked_files_summary"] = fallback_summary
        except Exception:
            logger.debug(
                "Could not build linked-file manifest for %s",
                skill_dir,
                exc_info=True,
            )

    if supporting and skill_dir:
        # A bound package must not advertise mutable absolute paths for direct
        # execution.  Support files remain readable through a fresh, checked
        # skill_view request, which binds its own package snapshot.
        if loaded_skill.get("package_bound"):
            skill_view_target = str(
                loaded_skill.get("lookup_name") or loaded_skill.get("name") or "skill"
            )
            parts.append("")
            parts.append("[This skill has supporting files:]")
            for sf in supporting:
                parts.append(f"- {sf}")
            parts.append(
                f'\nLoad any of these with skill_view(name="{skill_view_target}", '
                'file_path="<path>").'
            )
            linked_summary = loaded_skill.get("linked_files_summary") or {}
            if linked_summary.get("truncated"):
                categories = ", ".join(linked_summary.get("truncated_categories") or [])
                suffix = f" ({categories})" if categories else ""
                parts.append(
                    "[Supporting-file preview truncated"
                    f"{suffix}; use skill_view with an explicit file_path for files "
                    "not shown.]"
                )
            if user_instruction:
                parts.append("")
                parts.append(f"The user has provided the following instruction alongside the skill invocation: {user_instruction}")
            if runtime_note:
                parts.append("")
                parts.append(f"[Runtime note: {runtime_note}]")
            return "\n".join(parts)
        try:
            skill_view_target = str(skill_dir.relative_to(_skills_dir()))
        except ValueError:
            # Skill is from an external dir — use the skill name instead
            skill_view_target = skill_dir.name
        parts.append("")
        parts.append("[This skill has supporting files:]")
        for sf in supporting:
            parts.append(f"- {sf}  ->  {skill_dir / sf}")
        parts.append(
            f'\nLoad any of these with skill_view(name="{skill_view_target}", '
            f'file_path="<path>"), or run scripts directly by absolute path '
            f"(e.g. `node {skill_dir}/scripts/foo.js`)."
        )
        linked_summary = loaded_skill.get("linked_files_summary") or {}
        if linked_summary.get("truncated"):
            categories = ", ".join(linked_summary.get("truncated_categories") or [])
            suffix = f" ({categories})" if categories else ""
            parts.append(
                "[Supporting-file preview truncated"
                f"{suffix}; use skill_view with an explicit file_path for files "
                "not shown.]"
            )

    if user_instruction:
        parts.append("")
        parts.append(f"The user has provided the following instruction alongside the skill invocation: {user_instruction}")

    if runtime_note:
        parts.append("")
        parts.append(f"[Runtime note: {runtime_note}]")

    return "\n".join(parts)


def scan_skill_commands() -> Dict[str, Dict[str, Any]]:
    """Scan ~/.hermes/skills/ and return a mapping of /command -> skill info.

    Returns:
        Dict mapping "/skill-name" to {name, description, skill_md_path, skill_dir}.
    """
    global _skill_commands, _skill_commands_platform, _skill_commands_environment, _skill_commands_roots
    with _skill_commands_lock:
        resolved_platform = _resolve_skill_commands_platform()
        resolved_environment = _resolve_skill_commands_environment()
        try:
            primary_root, external_roots, resolved_roots = _resolve_skill_command_roots()
        except Exception:
            logger.warning(
                "Skill command root resolution failed; refusing a stale catalog",
                exc_info=True,
            )
            return {}

        previous_scope_matches = (
            _skill_commands_platform == resolved_platform
            and _skill_commands_environment == resolved_environment
            and _skill_commands_roots == resolved_roots
        )
        new_commands: Dict[str, Dict[str, Any]] = {}
        scan_incomplete = False
        root_scan_failed = False

        def mark_scan_incomplete(error: OSError) -> None:
            nonlocal scan_incomplete
            scan_incomplete = True
            logger.warning("Skill command directory scan was incomplete: %s", error)

        try:
            from tools.skills_tool import (
                _get_disabled_skill_names,
                skill_matches_environment,
                skill_matches_platform,
            )
            from agent.skill_utils import (
                iter_skill_index_files,
                read_strict_skill_index_file,
            )
            from hermes_cli.commands import resolve_command

            disabled = _get_disabled_skill_names()
            seen_names: set = set()
            dirs_to_scan = []
            try:
                primary_stat = primary_root.stat()
            except FileNotFoundError:
                # A first-run profile need not have created its local skills
                # directory yet; it is an empty local root, not a partial scan.
                primary_stat = None
            except OSError as exc:
                root_scan_failed = True
                mark_scan_incomplete(exc)
                primary_stat = None
            if primary_stat is not None and stat.S_ISDIR(primary_stat.st_mode):
                dirs_to_scan.append(primary_root)
            elif primary_stat is not None:
                root_scan_failed = True
                mark_scan_incomplete(
                    NotADirectoryError(
                        f"Primary skill root is not a directory: {primary_root}"
                    )
                )

            for external_root in external_roots:
                try:
                    external_stat = external_root.stat()
                except OSError as exc:
                    # This path has already been selected into the scope.  A
                    # concurrent deletion is just as incomplete as a permission
                    # failure: do not publish an external-only subset.
                    root_scan_failed = True
                    mark_scan_incomplete(exc)
                    continue
                if stat.S_ISDIR(external_stat.st_mode):
                    dirs_to_scan.append(external_root)
                else:
                    root_scan_failed = True
                    mark_scan_incomplete(
                        NotADirectoryError(
                            f"External skill root is not a directory: {external_root}"
                        )
                    )

            # Walk the exact root snapshot that forms this cache scope.  A
            # subsequent request with another profile cannot interleave here:
            # the lock covers both this walk and the final global commit.
            for scan_dir in dirs_to_scan:
                for skill_md in iter_skill_index_files(
                    scan_dir, "SKILL.md", on_error=mark_scan_incomplete
                ):
                    if any(
                        part in {".git", ".github", ".hub", ".archive"}
                        for part in skill_md.parts
                    ):
                        continue
                    try:
                        _, frontmatter, body = read_strict_skill_index_file(skill_md)
                        if not skill_matches_platform(frontmatter):
                            continue
                        if not skill_matches_environment(frontmatter):
                            continue
                        name = frontmatter.get("name", skill_md.parent.name)
                        if name in seen_names or name in disabled:
                            continue
                        description = frontmatter.get("description", "")
                        if not description:
                            for line in body.strip().split("\n"):
                                line = line.strip()
                                if line and not line.startswith("#"):
                                    description = line[:80]
                                    break
                        seen_names.add(name)
                        cmd_name = name.lower().replace(" ", "-").replace("_", "-")
                        cmd_name = _SKILL_INVALID_CHARS.sub("", cmd_name)
                        cmd_name = _SKILL_MULTI_HYPHEN.sub("-", cmd_name).strip("-")
                        if not cmd_name:
                            continue
                        if resolve_command(cmd_name) is not None:
                            logger.warning(
                                "Skill %r generates slash command '/%s' which "
                                "collides with a core Hermes command; skipping "
                                "auto-registration. Use '/skill %s' instead.",
                                name,
                                cmd_name,
                                name,
                            )
                            continue
                        cmd_key = f"/{cmd_name}"
                        if cmd_key in new_commands:
                            logger.warning(
                                "Skill %r maps to slash command %s already claimed "
                                "by %r; keeping the first and skipping this one.",
                                name,
                                cmd_key,
                                new_commands[cmd_key]["name"],
                            )
                            continue
                        new_commands[cmd_key] = {
                            "name": name,
                            "description": description or f"Invoke the {name} skill",
                            "skill_md_path": str(skill_md),
                            "skill_dir": str(skill_md.parent),
                        }
                    except Exception:
                        scan_incomplete = True
                        logger.warning(
                            "Skipping unreadable skill command source %s",
                            skill_md,
                            exc_info=True,
                        )
        except Exception:
            logger.warning(
                "Skill command scan failed; keeping the previous catalog",
                exc_info=True,
            )
            return _skill_commands if previous_scope_matches else {}

        if root_scan_failed:
            logger.warning(
                "Skill command root scan was incomplete; refusing the cached catalog"
            )
            return {}

        if scan_incomplete:
            logger.warning("Skill command scan was incomplete; keeping the previous catalog")
            return _skill_commands if previous_scope_matches else {}

        _skill_commands = new_commands
        _skill_commands_platform = resolved_platform
        _skill_commands_environment = resolved_environment
        _skill_commands_roots = resolved_roots
        return _skill_commands


def get_skill_commands() -> Dict[str, Dict[str, Any]]:
    """Return the current skill commands mapping (scan first if empty).

    Rescans when the active platform scope or offer-time runtime environment
    changes, so long-lived processes do not retain a stale filtered view.
    """
    with _skill_commands_lock:
        try:
            primary_root, external_roots, resolved_roots = _resolve_skill_command_roots()
        except Exception:
            logger.warning(
                "Skill command root resolution failed; refusing a stale catalog",
                exc_info=True,
            )
            return {}
        if not _skill_command_roots_are_accessible(primary_root, external_roots):
            # Re-run the protected scanner so it records the failure under the
            # same lock and declines a stale exact-scope cache.
            return scan_skill_commands()
        if (
            not _skill_commands
            or _skill_commands_platform != _resolve_skill_commands_platform()
            or _skill_commands_environment != _resolve_skill_commands_environment()
            or _skill_commands_roots != resolved_roots
        ):
            return scan_skill_commands()
        return _skill_commands


def reload_skills() -> Dict[str, Any]:
    """Re-scan the skills directory and return a diff of what changed.

    Rescans ``~/.hermes/skills/`` and any ``skills.external_dirs`` so the
    slash-command map (``agent.skill_commands._skill_commands``) reflects
    skills added or removed on disk.

    This does NOT invalidate the skills system-prompt cache. Skills are
    called by name via ``/skill-name``, ``skills_list``, or ``skill_view``
    — they don't need to be in the system prompt for the model to use them.
    Keeping the prompt cache intact preserves prefix caching across the
    reload, so a user invoking ``/reload-skills`` pays no cache-reset cost.

    Returns:
        Dict with keys::

            {
              "added":      [{"name": str, "description": str}, ...],
              "removed":    [{"name": str, "description": str}, ...],
              "unchanged":  [skill names present before and after],
              "total":      total skill count after rescan,
              "commands":   total /slash-skill count after rescan,
            }

        ``description`` is the skill's full SKILL.md frontmatter
        ``description:`` field. Note: the system prompt skill index
        truncates this to the first 57 chars; see ``extract_skill_description``.
    """
    # Snapshot pre-reload state (name -> description) from the current
    # slash-command cache. Using dicts lets the post-rescan diff carry
    # descriptions for newly-visible or just-removed skills without a
    # second disk walk.
    def _snapshot(cmds: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for slash_key, info in cmds.items():
            bare = slash_key.lstrip("/")
            out[bare] = (info or {}).get("description") or ""
        return out

    before = _snapshot(_skill_commands)

    # Rescan the skills dir. ``scan_skill_commands`` resets
    # ``_skill_commands = {}`` internally and repopulates it.
    new_commands = scan_skill_commands()

    after = _snapshot(new_commands)

    added_names = sorted(set(after) - set(before))
    removed_names = sorted(set(before) - set(after))
    unchanged = sorted(set(after) & set(before))

    added = [{"name": n, "description": after[n]} for n in added_names]
    # For removed skills, use the description we had cached pre-rescan
    # (the skill file is gone so we can't re-read it).
    removed = [{"name": n, "description": before[n]} for n in removed_names]

    return {
        "added": added,
        "removed": removed,
        "unchanged": unchanged,
        "total": len(after),
        "commands": len(new_commands),
    }


def resolve_skill_command_key(command: str) -> Optional[str]:
    """Resolve a user-typed /command to its canonical skill_cmds key.

    Skills are always stored with hyphens — ``scan_skill_commands`` normalizes
    spaces and underscores to hyphens when building the key. Hyphens and
    underscores are treated interchangeably in user input: this matches
    ``_check_unavailable_skill`` and accommodates Telegram bot-command names
    (which disallow hyphens, so ``/claude-code`` is registered as
    ``/claude_code`` and comes back in the underscored form).

    Returns the matching ``/slug`` key from ``get_skill_commands()`` or
    ``None`` if no match.
    """
    if not command:
        return None
    cmd_key = f"/{command.replace('_', '-')}"
    return cmd_key if cmd_key in get_skill_commands() else None


def build_skill_invocation_message(
    cmd_key: str,
    user_instruction: str = "",
    task_id: str | None = None,
    runtime_note: str = "",
) -> Optional[str]:
    """Build the user message content for a skill slash command invocation.

    Args:
        cmd_key: The command key including leading slash (e.g., "/gif-search").
        user_instruction: Optional text the user typed after the command.

    Returns:
        The formatted message string, or None if the skill wasn't found.
    """
    commands = get_skill_commands()
    skill_info = commands.get(cmd_key)
    if not skill_info:
        return None

    loaded = _load_skill_payload(skill_info["skill_dir"], task_id=task_id)
    if not loaded:
        return None

    loaded_skill, skill_dir, skill_name = loaded

    # Track active usage for Curator lifecycle management (#17782)
    try:
        from tools.skill_usage import bump_use
        bump_use(skill_name)
    except Exception:
        pass  # Non-critical — skill invocation proceeds regardless

    activation_note = (
        f'[IMPORTANT: The user has invoked the "{skill_name}" skill, indicating they want '
        "you to follow its instructions. The full skill content is loaded below.]"
    )
    return _build_skill_message(
        loaded_skill,
        skill_dir,
        activation_note,
        user_instruction=user_instruction,
        runtime_note=runtime_note,
        session_id=task_id,
    )


# ---------------------------------------------------------------------------
# Stacked slash-skill invocations — `/skill-a /skill-b do XYZ` loads every
# leading skill (up to _MAX_STACKED_SKILLS), not just the first.
#
# Inspired by Claude Code v2.1.199 (July 2, 2026): "Stacked slash-skill
# invocations like /skill-a /skill-b do XYZ now load all leading skills
# (up to 5), not just the first."
#
# The generated message deliberately reuses the BUNDLE scaffolding markers
# ("skill bundle," header + "[Loaded as part of the " block prefix) so
# extract_user_instruction_from_skill_message() recovers the user's
# instruction without any new marker plumbing — memory providers keep
# storing what the user actually asked, not N skill bodies.
# ---------------------------------------------------------------------------
_MAX_STACKED_SKILLS = 5


def split_stacked_skill_commands(rest: str) -> tuple[list[str], str]:
    """Consume additional leading ``/skill`` tokens from *rest*.

    *rest* is the text that follows the FIRST matched skill command (the
    caller has already resolved that one). Leading whitespace-delimited
    tokens that start with ``/`` and resolve to installed skill commands are
    consumed, up to ``_MAX_STACKED_SKILLS`` total leading skills (i.e. at
    most ``_MAX_STACKED_SKILLS - 1`` extra keys here). Parsing stops at the
    first token that is not a resolvable skill command — that token and
    everything after it become the user instruction.

    Returns:
        ``(extra_cmd_keys, remaining_instruction)`` where ``extra_cmd_keys``
        are canonical ``/slug`` keys from :func:`get_skill_commands`.
    """
    keys: list[str] = []
    remaining = rest or ""
    while len(keys) < _MAX_STACKED_SKILLS - 1:
        stripped = remaining.lstrip()
        if not stripped.startswith("/"):
            break
        parts = stripped.split(None, 1)
        token = parts[0]
        tail = parts[1] if len(parts) > 1 else ""
        cmd_key = resolve_skill_command_key(token.lstrip("/"))
        if cmd_key is None or cmd_key in keys:
            break
        keys.append(cmd_key)
        remaining = tail
    return keys, remaining.strip()


def build_stacked_skill_invocation_message(
    cmd_keys: list[str],
    user_instruction: str = "",
    task_id: str | None = None,
) -> Optional[tuple[str, list[str], list[str]]]:
    """Build the user message for a stacked multi-skill slash invocation.

    Args:
        cmd_keys: Canonical ``/slug`` keys, in the order the user typed them.
        user_instruction: Text remaining after the leading skill commands.

    Returns:
        ``(message, loaded_skill_names, missing_skill_names)`` or ``None``
        when no skill could be loaded at all.
    """
    commands = get_skill_commands()

    loaded_names: list[str] = []
    missing: list[str] = []
    skill_blocks: list[str] = []
    seen: set[str] = set()

    for cmd_key in cmd_keys:
        if not cmd_key or cmd_key in seen:
            continue
        seen.add(cmd_key)

        skill_info = commands.get(cmd_key)
        if not skill_info:
            missing.append(cmd_key.lstrip("/"))
            continue

        loaded = _load_skill_payload(skill_info["skill_dir"], task_id=task_id)
        if not loaded:
            missing.append(cmd_key.lstrip("/"))
            continue
        loaded_skill, skill_dir, skill_name = loaded

        # Track active usage for Curator lifecycle management (#17782)
        try:
            from tools.skill_usage import bump_use
            bump_use(skill_name)
        except Exception:
            pass  # Non-critical

        # NOTE: must start with "[Loaded as part of the " — that prefix is
        # the bundle block marker the memory-scaffolding extractor cuts on.
        activation_note = (
            f'[Loaded as part of the stacked skill invocation "{skill_name}".]'
        )
        skill_blocks.append(
            _build_skill_message(
                loaded_skill,
                skill_dir,
                activation_note,
                session_id=task_id,
            )
        )
        loaded_names.append(skill_name)

    if not skill_blocks:
        return None

    # Header — must contain " skill bundle," so the bundle-format extractor
    # in extract_user_instruction_from_skill_message() applies unchanged.
    typed = " ".join(k for k in cmd_keys if k)
    header_lines = [
        f'[IMPORTANT: The user has invoked the "{typed}" stacked skill bundle, '
        f"loading {len(loaded_names)} skills together. Treat every skill below "
        "as active guidance for this turn.]",
        "",
        f"Skills loaded: {', '.join(loaded_names)}",
    ]
    if missing:
        header_lines.append(f"Skills missing (skipped): {', '.join(missing)}")
    if user_instruction:
        header_lines.extend(["", f"User instruction: {user_instruction}"])

    header = "\n".join(header_lines)
    return ("\n\n".join([header, *skill_blocks]), loaded_names, missing)


def build_preloaded_skills_prompt(
    skill_identifiers: list[str],
    task_id: str | None = None,
) -> tuple[str, list[str], list[str]]:
    """Load one or more skills for session-wide CLI/TUI preloading.

    Returns (prompt_text, loaded_skill_names, missing_identifiers).

    Disabled skills are treated the same as missing ones: this loads via a
    raw identifier straight into ``_load_skill_payload``, bypassing
    ``get_skill_commands()``'s scan-time disabled filter — mirrors the
    bundle-invocation gate (#59156). Without this, ``hermes -s <skill>`` or
    a deployment's ``HERMES_TUI_SKILLS`` env var could force-load a skill an
    operator disabled via ``skills.disabled``/``skills.platform_disabled``.
    """
    prompt_parts: list[str] = []
    loaded_names: list[str] = []
    missing: list[str] = []

    try:
        from agent.skill_utils import get_disabled_skill_names
        disabled_names = get_disabled_skill_names()
    except Exception:
        disabled_names = set()

    seen: set[str] = set()
    for raw_identifier in skill_identifiers:
        identifier = (raw_identifier or "").strip()
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)

        loaded = _load_skill_payload(identifier, task_id=task_id)
        if not loaded:
            missing.append(identifier)
            continue

        loaded_skill, skill_dir, skill_name = loaded

        if skill_name in disabled_names or identifier in disabled_names:
            missing.append(identifier)
            continue

        # Track active usage for Curator lifecycle management (#17782)
        try:
            from tools.skill_usage import bump_use
            bump_use(skill_name)
        except Exception:
            pass  # Non-critical

        activation_note = (
            f'[IMPORTANT: The user launched this CLI session with the "{skill_name}" skill '
            "preloaded. Treat its instructions as active guidance for the duration of this "
            "session unless the user overrides them.]"
        )
        prompt_parts.append(
            _build_skill_message(
                loaded_skill,
                skill_dir,
                activation_note,
                session_id=task_id,
            )
        )
        loaded_names.append(skill_name)

    return "\n\n".join(prompt_parts), loaded_names, missing
