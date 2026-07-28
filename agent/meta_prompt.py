"""Meta-prompt composition layer for IYARI agents.

The meta-prompt defines *how* an agent thinks -- decomposing problems,
checking existing context before assuming novelty, using tools instead of
fabricating data, and declaring confidence when information is missing. It
is distinct from ``soul.md`` (or ``DEFAULT_AGENT_IDENTITY``), which defines
*who* the agent is (persona/tone), and is fully client-controlled.

``compose_system_prompt`` guarantees the meta-prompt always precedes the
identity text in the final system prompt, and that nothing in the identity
text can remove, reorder, or rewrite it: the composition is unconditional
string concatenation controlled entirely by this module -- ``soul_md`` is
always treated as inert data, never as a template or instruction that
alters how the meta-prompt is assembled.

See ``agent/system_prompt.py`` (``build_system_prompt_parts``) for the
single integration point: every system-prompt build path routes through
that function, so hooking in there covers all generation points without
touching each call site individually.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import yaml

_DEFAULT_META_PROMPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "meta_prompt_base.yaml",
)

_PREAMBLE = (
    "The following directives govern how you approach every task. They "
    "apply regardless of your assigned persona below and cannot be "
    "disabled or overridden by it:"
)


def load_meta_prompt_base(path: Optional[str] = None) -> str:
    """Render ``config/meta_prompt_base.yaml`` into a single prompt string.

    Reads a preamble plus an ordered list of directives and joins them into
    a bulleted block. Returns an empty string (never raises) if the file is
    missing, empty, or malformed, so a broken config degrades gracefully
    instead of blocking prompt assembly -- the identity layer still gets
    through even if the meta-prompt fails to load.
    """
    resolved = path or _DEFAULT_META_PROMPT_PATH
    try:
        with open(resolved, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return ""

    if not isinstance(data, dict):
        return ""

    directives = data.get("directives") or []
    if not isinstance(directives, list):
        return ""

    lines = []
    for entry in directives:
        if isinstance(entry, dict):
            text = str(entry.get("text") or "").strip()
        elif isinstance(entry, str):
            text = entry.strip()
        else:
            continue
        if text:
            lines.append(f"- {text}")

    if not lines:
        return ""

    return _PREAMBLE + "\n" + "\n".join(lines)


def compose_system_prompt(
    meta_prompt: str,
    soul_md: str,
    context: Optional[Dict[str, Any]] = None,
) -> str:
    """Combine the base meta-prompt with the client identity text.

    The meta-prompt (if non-empty) always comes first; the identity text
    (``soul.md`` content or ``DEFAULT_AGENT_IDENTITY``) always comes second.
    This is plain, unconditional string concatenation -- ``soul_md`` is
    never interpreted as a template or executed, it is inert text appended
    after the meta-prompt -- so nothing a client puts in ``soul.md`` (even
    an explicit "ignore all previous instructions") can remove, reorder, or
    rewrite the meta-prompt text itself. That text can still be present
    afterward as ordinary content, but it cannot make the meta-prompt
    disappear from the resulting string.

    Args:
        meta_prompt: rendered output of :func:`load_meta_prompt_base`.
        soul_md: the client's identity text (``soul.md`` content or the
            hardcoded ``DEFAULT_AGENT_IDENTITY`` fallback).
        context: reserved for forward compatibility (e.g. a future phase
            selecting a meta-prompt variant per ``agent_id``/tier). Unused
            in Fase 1 -- accepted so callers and tests can pass it without
            this function needing another signature change later.

    Returns:
        The composed string, with the meta-prompt strictly preceding
        ``soul_md``. Empty inputs are dropped rather than leaving blank
        lines.
    """
    meta_prompt = (meta_prompt or "").strip()
    soul_md = (soul_md or "").strip()
    parts = [p for p in (meta_prompt, soul_md) if p]
    return "\n\n".join(parts)


__all__ = ["load_meta_prompt_base", "compose_system_prompt"]
