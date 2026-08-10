"""Named subagent personas for ``delegate_task``.

A persona is a markdown file with YAML frontmatter that pre-declares how a
delegated child should be configured and briefed::

    ---
    name: scout
    description: Read-only reconnaissance inside one repo or directory tree.
    toolsets: [file, terminal]
    required_toolsets: [file]
    ---
    You are a read-only scout. Answer the orchestrator's question with
    evidence, without dumping whole files back into its context.

Why a file and not tool arguments
---------------------------------
Model-facing ``toolsets`` was deliberately removed from ``delegate_task`` in
ba0bc01d1f ("Toolset selection is a capability-scoping decision the model
should not control").  That decision stands: nothing here lets the model
choose a capability scope at call time.  A persona is authored by the *user*,
on disk, before any input is seen — so the scope is fixed by a human, and the
model may only select among the scopes that human already approved.

This closes the gap that commit left open: previously there was no way for a
user to obtain a genuinely read-only subagent, because children always
inherited the parent's full toolset.

Two invariants this module preserves
------------------------------------
- **Privilege reduction only.** A persona's ``toolsets`` are intersected with
  the parent's in ``_build_child_agent``; a persona can never grant a toolset
  the parent lacks.  ``required_toolsets`` exists so that silent stripping by
  that intersection becomes a loud spawn failure instead of a subagent that
  mysteriously cannot do its job.
- **Prompt caching stays intact.** Personas are resolved once at spawn, before
  the child's system prompt is built.  Nothing here mutates a live
  conversation's prefix.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.skill_utils import parse_frontmatter
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# Directory name searched under HERMES_HOME (user scope) and the working
# directory (project scope). Mirrors Claude Code's ``.claude/agents/``.
PERSONAS_DIR_NAME = "agents"

# Frontmatter keys a persona may declare. Unknown keys are ignored with a
# warning rather than rejected, so a persona written for a newer Hermes still
# loads on an older one.
_KNOWN_KEYS = frozenset(
    {
        "name",
        "description",
        "toolsets",
        "required_toolsets",
        "reasoning_effort",
        "max_iterations",
    }
)

# A persona name must be safe to embed in a tool-result line and to match
# against a filename. Same character class the skill loader accepts.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class PersonaError(ValueError):
    """Raised when a persona is missing, malformed, or unusable."""


# Problems seen this process, so a broken file doesn't warn on every spawn.
_WARNED: set = set()


def _warn_once(key: str, msg: str, *args: Any) -> None:
    """Log a warning the first time a given problem is seen."""
    if key in _WARNED:
        return
    _WARNED.add(key)
    logger.warning(msg, *args)


def get_persona_dirs(workdir: Optional[Path] = None) -> List[Path]:
    """Return persona directories in resolution order (highest priority first).

    Project scope (``<workdir>/.hermes/agents``) wins over user scope
    (``$HERMES_HOME/agents``) so a repo can pin its own reviewer/scout
    definitions for everyone working in it.
    """
    dirs: List[Path] = []
    if workdir is not None:
        try:
            dirs.append(Path(workdir).resolve() / ".hermes" / PERSONAS_DIR_NAME)
        except (OSError, RuntimeError):  # pragma: no cover - defensive
            logger.debug("Could not resolve workdir for personas", exc_info=True)
    try:
        dirs.append(get_hermes_home() / PERSONAS_DIR_NAME)
    except (OSError, RuntimeError):  # pragma: no cover - defensive
        logger.debug("Could not resolve HERMES_HOME for personas", exc_info=True)
    return dirs


def _coerce_toolsets(value: Any, field: str, name: str) -> Optional[List[str]]:
    """Normalise a toolsets-ish frontmatter value into a list of names."""
    if value is None:
        return None
    if isinstance(value, str):
        items = [p.strip() for p in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = [str(p).strip() for p in value]
    else:
        raise PersonaError(
            f"Persona '{name}': '{field}' must be a list or comma-separated "
            f"string, got {type(value).__name__}."
        )
    cleaned = [i for i in items if i]
    if not cleaned:
        return None
    return cleaned


def _parse_persona(path: Path, text: str) -> Dict[str, Any]:
    """Parse one persona file into a validated dict."""
    frontmatter, body = parse_frontmatter(text)
    stem = path.stem

    name = str(frontmatter.get("name") or stem).strip().lower()
    if not _NAME_RE.match(name):
        raise PersonaError(
            f"Persona at {path}: invalid name {name!r} — use lowercase "
            "letters, digits, hyphens, or underscores (max 64 chars)."
        )

    prompt = (body or "").strip()
    if not prompt:
        raise PersonaError(
            f"Persona '{name}' at {path} has an empty body; the body IS the "
            "subagent's system prompt."
        )

    unknown = set(frontmatter) - _KNOWN_KEYS
    if unknown:
        logger.warning(
            "Persona '%s' at %s declares unknown key(s) %s — ignored.",
            name,
            path,
            ", ".join(sorted(unknown)),
        )

    toolsets = _coerce_toolsets(frontmatter.get("toolsets"), "toolsets", name)
    required = _coerce_toolsets(
        frontmatter.get("required_toolsets"), "required_toolsets", name
    )

    if required and toolsets:
        missing = [t for t in required if t not in toolsets]
        if missing:
            raise PersonaError(
                f"Persona '{name}': required_toolsets {missing} are not listed "
                f"in toolsets {toolsets} — the persona could never satisfy its "
                "own contract."
            )

    max_iterations = frontmatter.get("max_iterations")
    if max_iterations is not None:
        try:
            max_iterations = int(max_iterations)
        except (TypeError, ValueError):
            raise PersonaError(
                f"Persona '{name}': max_iterations must be an integer, got "
                f"{max_iterations!r}."
            )
        if max_iterations < 1:
            raise PersonaError(
                f"Persona '{name}': max_iterations must be >= 1, got {max_iterations}."
            )

    effort = frontmatter.get("reasoning_effort")
    effort = str(effort).strip() if effort is not None else None

    return {
        "name": name,
        "description": str(frontmatter.get("description") or "").strip(),
        "prompt": prompt,
        "toolsets": toolsets,
        "required_toolsets": required,
        "reasoning_effort": effort or None,
        "max_iterations": max_iterations,
        "path": str(path),
    }


def discover_personas(workdir: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Load every readable persona, nearest scope winning on name collision.

    A malformed persona is skipped with a warning rather than breaking
    delegation entirely — one bad file must not disable the feature.  Each
    distinct problem is warned about only once per process: discovery runs on
    every delegation, and a broken file would otherwise flood the log.
    """
    found: Dict[str, Dict[str, Any]] = {}
    for directory in get_persona_dirs(workdir):
        if not directory.is_dir():
            continue
        try:
            entries = sorted(directory.rglob("*.md"))
        except OSError:  # pragma: no cover - defensive
            logger.debug("Could not list personas in %s", directory, exc_info=True)
            continue
        for path in entries:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                _warn_once(f"read:{path}", "Could not read persona file %s — skipped.", path)
                continue
            try:
                persona = _parse_persona(path, text)
            except PersonaError as exc:
                _warn_once(f"parse:{path}:{exc}", "%s — skipped.", exc)
                continue
            # First scope to define a name wins (project before user).
            found.setdefault(persona["name"], persona)
    return found


def load_persona(name: str, workdir: Optional[Path] = None) -> Dict[str, Any]:
    """Resolve one persona by name, or raise ``PersonaError`` listing options.

    The error names what is available because an unknown-agent failure is
    otherwise indistinguishable to the model from a broken environment, and it
    will retry identically.
    """
    requested = str(name or "").strip().lower()
    personas = discover_personas(workdir)
    if requested in personas:
        return personas[requested]

    available = ", ".join(sorted(personas)) if personas else "none defined"
    searched = ", ".join(str(d) for d in get_persona_dirs(workdir))
    raise PersonaError(
        f"Unknown agent {requested!r}; available: {available}. "
        f"Personas are markdown files with YAML frontmatter in: {searched}."
    )
