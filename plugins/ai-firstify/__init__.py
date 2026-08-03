"""ai-firstify plugin — audit / re-engineer / bootstrap projects for AI-first design.

Ports the ``ai-firstify`` skill from ``techwolf-ai/ai-first-toolkit`` (MIT) into
the Hermes plugin surface. The plugin does two things when enabled:

1. **Registers the bundled skill** as ``ai-firstify:ai-firstify`` so the agent
   can load it on demand via ``skill_view``. The skill spans three modes —
   audit (read-only scored report across 7 dimensions), re-engineer (audit
   then active fixes in 7 phases), and bootstrap (scaffold a new AI-first
   project) — with reference material loaded progressively from
   ``skills/ai-firstify/references/``.

2. **Registers the ``/ai-firstify`` slash command** as a discovery affordance.
   Plugin-provided skills are intentionally *not* listed in the system
   prompt's ``<available_skills>`` index (they are explicit opt-in loads), so
   the slash command gives users a one-liner to kick off a run and reminds the
   agent which skill to load.

Plugins are opt-in: enable with ``hermes plugins enable ai-firstify``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SKILL_NAME = "ai-firstify"
# Resolve to an absolute path so the reference paths the slash command hands
# to the agent for read_file are valid regardless of the process CWD.
_SKILL_DIR = (Path(__file__).parent / "skills" / "ai-firstify").resolve()
_SKILL_MD = _SKILL_DIR / "SKILL.md"
_SKILL_DESC = (
    "Audit, re-engineer, or bootstrap a project to align with the 9 AI-first "
    "design principles and 7 design patterns from the TechWolf AI-First Bootcamp."
)

# Aliases → canonical mode. Mirrors the trigger words in SKILL.md.
_MODE_ALIASES = {
    "audit": "audit",
    "review": "audit",
    "analyze": "audit",
    "analyse": "audit",
    "check": "audit",
    "assess": "audit",
    "reengineer": "reengineer",
    "re-engineer": "reengineer",
    "fix": "reengineer",
    "improve": "reengineer",
    "transform": "reengineer",
    "bootstrap": "bootstrap",
    "start": "bootstrap",
    "new": "bootstrap",
    "scaffold": "bootstrap",
}

_HELP_TEXT = (
    "/ai-firstify [audit|reengineer|bootstrap] [path] — align a project with "
    "AI-first design principles.\n\n"
    "Modes:\n"
    "  audit       Read-only scored report across 7 dimensions (default).\n"
    "  reengineer  Run the audit, then actively fix issues in 7 phases.\n"
    "  bootstrap   Scaffold a new AI-first project through discovery questions.\n\n"
    "The bundled skill is registered as 'ai-firstify:ai-firstify'; the agent "
    "loads it with skill_view."
)


def _parse(raw_args: str) -> tuple[str, str]:
    """Return ``(mode, target)`` parsed from the slash-command arguments."""
    argv = (raw_args or "").strip().split()
    if not argv:
        return "audit", ""
    first = argv[0].lower()
    if first in {"help", "-h", "--help"}:
        return "help", ""
    if first in _MODE_ALIASES:
        return _MODE_ALIASES[first], " ".join(argv[1:]).strip()
    # No explicit mode — treat everything as the target, default to audit.
    return "audit", " ".join(argv).strip()


def _handle_slash(raw_args: str) -> Optional[str]:
    mode, target = _parse(raw_args)
    if mode == "help":
        return _HELP_TEXT

    scope = f" Target: {target}." if target else ""
    refs = _SKILL_DIR / "references"
    return (
        f"🐺 ai-firstify — {mode} mode.{scope}\n\n"
        "Load and follow the bundled skill:\n"
        '  1. skill_view(name="ai-firstify:ai-firstify") for the overview.\n'
        f'  2. read_file the mode playbook: {refs / f"mode-{mode}.md"}\n'
        f"Then carry out the {mode} procedure"
        f"{' on ' + target if target else ' on the current project'}, reading "
        f"further reference files from {refs}/ (principles, patterns, "
        "anti-patterns, assessment-rubric, ...) on demand.\n\n"
        "Note: reference files live on disk next to the skill — read them with "
        "read_file using the paths above, not skill_view file_path (plugin "
        "skills only serve their SKILL.md through skill_view).\n\n"
        "Modes: audit (read-only report) · reengineer (audit + active fixes) · "
        "bootstrap (scaffold a new project). Run `/ai-firstify help` for details."
    )


def register(ctx) -> None:
    """Plugin entrypoint — called by the PluginManager on load."""
    try:
        ctx.register_skill(_SKILL_NAME, _SKILL_MD, description=_SKILL_DESC)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("ai-firstify: failed to register skill: %s", exc)

    ctx.register_command(
        "ai-firstify",
        handler=_handle_slash,
        description="Audit, re-engineer, or bootstrap a project for AI-first design.",
        args_hint="[audit|reengineer|bootstrap] [path]",
    )
