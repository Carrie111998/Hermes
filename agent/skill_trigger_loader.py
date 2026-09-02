"""Trigger-based skill auto-loading using snapshot entries.

Matches skill triggers (regex patterns from frontmatter ``triggers:`` key)
against user message text.  Consumer of ``prompt_builder`` snapshot entries,
not a filesystem scanner — the snapshot pipeline already filters by platform,
environment, disabled, and conditions.

Cache safety
  Injected skill content rides the ``api_content`` sidecar on the user
  message (same path as memory prefetch and plugin context), never as a
  system message.  The system prompt stays byte-stable for the session's
  lifetime.  See ``agent/turn_context.py:build_turn_context``.

Fail-safe
  Any exception is caught, logged at DEBUG, and returns ``[]`` / ``""``.
"""

import logging
import re
from pathlib import Path
from typing import List, Optional

from hermes_constants import get_skills_dir

logger = logging.getLogger(__name__)

# Maximum number of skills to auto-load per turn from trigger matches.
_TRIGGER_MAX_SKILLS = 5


def get_triggered_skills(
    user_text: str,
    visible_entries: list[dict],
    max_results: int = _TRIGGER_MAX_SKILLS,
) -> list[dict]:
    """Match trigger patterns in *visible_entries* against *user_text*.

    Parameters
    ----------
    user_text : str
        The current user message text (string content only; non-string/multimodal
        content should be skipped before calling this function).
    visible_entries : list[dict]
        Filtered snapshot entries from ``prompt_builder.get_visible_skill_entries``.
    max_results : int
        Maximum number of matching skills to return (default 5).

    Returns
    -------
    list[dict]
        Matching entry dicts, at most *max_results*.  Empty list on no match
        or any error (fail-safe).
    """
    try:
        if not user_text or not visible_entries:
            return []

        matched: list[dict] = []
        for entry in visible_entries:
            if len(matched) >= max_results:
                break

            triggers = entry.get("triggers", []) or []
            if not triggers:
                continue

            # Try each trigger pattern; first match wins (one skill per turn).
            for pattern in triggers:
                if len(matched) >= max_results:
                    break
                if not isinstance(pattern, str) or not pattern.strip():
                    continue
                try:
                    if re.search(pattern, user_text, re.IGNORECASE | re.DOTALL):
                        matched.append(entry)
                        break  # each skill matched at most once per turn
                except re.error:
                    logger.debug(
                        "Invalid trigger regex %r in skill %s, skipping",
                        pattern,
                        entry.get("skill_name", "?"),
                    )
                    continue

        return matched
    except Exception:
        logger.debug("Skill trigger evaluation failed", exc_info=True)
        return []


def format_triggered_skill_content(
    entry: dict,
    skills_dir: "Path | None" = None,
) -> str:
    """Format a matched skill entry for sidecar injection.

    Loads the ``SKILL.md`` body via ``rel_path`` from the snapshot entry,
    strips frontmatter, and wraps it in an auto-load header.

    Parameters
    ----------
    entry : dict
        A snapshot entry dict with ``rel_path`` (and optionally ``skill_name``
        / ``frontmatter_name``).
    skills_dir : Path or None
        Base directory for resolving ``rel_path``.  Defaults to
        ``hermes_constants.get_skills_dir()``.

    Returns
    -------
    str
        Formatted markdown string, or ``""`` on any error (fail-safe).
    """
    try:
        rel_path = entry.get("rel_path", "") or ""
        if not rel_path:
            return ""

        base = skills_dir or get_skills_dir()
        skill_md_path = base / rel_path
        if not skill_md_path.exists():
            return ""

        content = skill_md_path.read_text(encoding="utf-8")
        body = _strip_frontmatter(content)
        if not body:
            return ""

        skill_name = (
            entry.get("frontmatter_name")
            or entry.get("skill_name")
            or "?"
        )
        return f"## Auto-loaded: {skill_name}\n\n{body}"
    except Exception:
        logger.debug("Failed to format triggered skill content", exc_info=True)
        return ""


def _strip_frontmatter(content: str) -> str:
    """Remove YAML frontmatter (``---`` delimited) from skill content.

    Returns the body text after the frontmatter, stripped of leading newlines.
    Returns ``""`` when the content is frontmatter-only, or the original
    content when no frontmatter is present.
    """
    if content.startswith("\ufeff"):
        content = content[1:]  # tolerate UTF-8 BOM (Windows editors)
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            # Skip past the closing --- and any trailing newline
            body = content[end + 4:].lstrip("\n")
            return body if body else ""
    return content