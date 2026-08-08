"""Skill slash-command helpers for the Hermes CLI (god-file decomposition campaign).

Module-level helpers lifted verbatim out of ``cli.py``: slash-command
detection (``_looks_like_slash_command``), the lazily-initialised skill
command/bundle registries (``get_skill_commands`` / ``get_skill_bundles``
with ``_ensure_skill_commands``, ``_get_plugin_cmd_handler_names`` and
``_parse_skills_argument``), and the invocation-message builders. The
module-level registries ``_skill_commands`` / ``_skill_bundles`` moved here
with their functions; ``cli.py`` re-imports every name from this module, so
``from cli import ...`` keeps working unchanged and ``cli._reload_skills``
writes the refreshed registry into ``hermes_cli.skill_command_helpers``.

Import discipline: all third-party imports are lazy (inside the functions),
matching the original cli.py bodies verbatim.
"""

# ============================================================================
# Slash-command detection helper
# ============================================================================

def _looks_like_slash_command(text: str) -> bool:
    """Return True if *text* looks like a slash command, not a file path.

    Slash commands are ``/help``, ``/model gpt-4``, ``/q``, etc.
    File paths like ``/Users/ironin/file.md:45-46 can you fix this?``
    also start with ``/`` but contain additional ``/`` characters in
    the first whitespace-delimited word.  This helper distinguishes
    the two so that pasted paths are sent to the agent instead of
    triggering "Unknown command".
    """
    if not text or not text.startswith("/"):
        return False
    first_word = text.split()[0]
    # After stripping the leading /, a command name has no slashes.
    # A path like /Users/foo/bar.md always does.
    return "/" not in first_word[1:]


# ============================================================================
# Skill Slash Commands — dynamic commands generated from installed skills
# ============================================================================

_skill_commands = None
_skill_bundles = None


def _ensure_skill_commands() -> dict:
    global _skill_commands
    if _skill_commands is None:
        from agent.skill_commands import scan_skill_commands

        _skill_commands = scan_skill_commands()
    return _skill_commands


def get_skill_commands() -> dict:
    return _ensure_skill_commands()


def build_skill_invocation_message(*args, **kwargs):
    from agent.skill_commands import build_skill_invocation_message as _impl

    return _impl(*args, **kwargs)


def build_preloaded_skills_prompt(*args, **kwargs):
    from agent.skill_commands import build_preloaded_skills_prompt as _impl

    return _impl(*args, **kwargs)


def get_skill_bundles() -> dict:
    global _skill_bundles
    if _skill_bundles is None:
        from agent.skill_bundles import get_skill_bundles as _impl

        _skill_bundles = _impl()
    return _skill_bundles


def build_bundle_invocation_message(*args, **kwargs):
    from agent.skill_bundles import build_bundle_invocation_message as _impl

    return _impl(*args, **kwargs)


def _get_plugin_cmd_handler_names() -> set:
    """Return plugin command names (without slash prefix) for dispatch matching."""
    try:
        from hermes_cli.plugins import get_plugin_commands
        return set(get_plugin_commands().keys())
    except Exception:
        return set()


def _parse_skills_argument(skills: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Normalize a CLI skills flag into a deduplicated list of skill identifiers."""
    if not skills:
        return []

    if isinstance(skills, str):
        raw_values = [skills]
    elif isinstance(skills, (list, tuple)):
        raw_values = [str(item) for item in skills if item is not None]
    else:
        raw_values = [str(skills)]

    parsed: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        for part in raw.split(","):
            normalized = part.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            parsed.append(normalized)
    return parsed
