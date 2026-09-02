"""Normalize relative ``MEDIA:`` paths in the final response.

The system prompt asks the model to deliver files as ``MEDIA:/absolute/path``,
but a model working inside a project directory routinely emits relative paths
(``MEDIA: final_video.mp4``) instead. Every consumer downstream assumes
absolute: the messaging gateways' MEDIA regexes only match absolute paths (the
file silently isn't delivered), and the desktop renders a player that resolves
the path against the app's own cwd — a black, unplayable card (observed on a
Windows deployment; reproduced identically on macOS).

This module rewrites such lines at turn-finalize time, joining the relative
path onto the session's working directory. Three deliberate fences keep it
strictly no-worse-than-before:

1. Only the line form is touched — ``MEDIA: <path>`` alone on a line
   (leading whitespace allowed, quoted paths supported). Inline mentions in
   prose are left alone to keep the false-positive surface minimal.
2. Fenced code blocks (``` / ~~~) are never rewritten — those are examples,
   not deliveries.
3. A rewrite happens ONLY when the joined absolute path actually exists on
   disk. A wrong base-directory guess therefore degrades to "leave the text
   unchanged", never to a broken rewrite.

Base directories, in order: the session's live terminal cwd
(``tools.terminal_tool.get_session_cwd`` — tracks ``cd`` during the turn),
then the configured session cwd (``agent.runtime_cwd.resolve_agent_cwd``).
"""

from __future__ import annotations

import os
import re
from typing import List, Optional

_MEDIA_LINE_RE = re.compile(
    r"(?m)^(?P<lead>[\t ]*)MEDIA:[\t ]*"
    r"(?P<path>`[^`\n]+`|\"[^\"\n]+\"|'[^'\n]+'|\S+)"
    r"(?P<tail>[\t ]*)$"
)

_FENCE_LINE_RE = re.compile(r"^[\t ]*(?:```|~~~)")

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_DRIVE_RE = re.compile(r"^[A-Za-z]:[/\\]")


def _unquote(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "`\"'":
        return v[1:-1].strip()
    return v


def _is_relative_candidate(path: str) -> bool:
    """Only plain relative paths qualify; absolute/home/drive/URL forms never."""
    if not path or path.startswith(("/", "~", "\\")):
        return False
    if _DRIVE_RE.match(path):
        return False
    if _SCHEME_RE.match(path):
        return False
    return True


def _base_dirs(session_key: Optional[str]) -> List[str]:
    bases: List[str] = []
    try:
        from tools.terminal_tool import get_session_cwd

        live = get_session_cwd(session_key)
        if isinstance(live, str) and live.strip():
            bases.append(live)
    except Exception:
        pass
    try:
        from agent.runtime_cwd import resolve_agent_cwd

        bases.append(str(resolve_agent_cwd()))
    except Exception:
        pass
    seen = set()
    out: List[str] = []
    for b in bases:
        if b not in seen:
            seen.add(b)
            out.append(b)
    return out


def normalize_relative_media_paths(text: str, session_key: Optional[str] = None) -> str:
    """Rewrite line-form relative MEDIA paths to existing absolute paths.

    Never raises — on any surprise the original text is returned unchanged
    (delivering the response beats normalizing it).
    """
    try:
        if not isinstance(text, str) or "MEDIA:" not in text:
            return text

        bases = _base_dirs(session_key)
        if not bases:
            return text

        def rewrite_segment(segment: str) -> str:
            def repl(m: "re.Match[str]") -> str:
                raw = m.group("path")
                p = _unquote(raw)
                if not _is_relative_candidate(p):
                    return m.group(0)
                for base in bases:
                    candidate = os.path.normpath(os.path.join(base, p))
                    if os.path.isfile(candidate):
                        return f"{m.group('lead')}MEDIA: {candidate}{m.group('tail')}"
                return m.group(0)

            return _MEDIA_LINE_RE.sub(repl, segment)

        lines = text.split("\n")
        out_lines: List[str] = []
        buffer: List[str] = []
        in_fence = False
        for line in lines:
            if _FENCE_LINE_RE.match(line):
                if buffer:
                    joined = "\n".join(buffer)
                    out_lines.append(joined if in_fence else rewrite_segment(joined))
                    buffer = []
                out_lines.append(line)
                in_fence = not in_fence
                continue
            buffer.append(line)
        if buffer:
            joined = "\n".join(buffer)
            out_lines.append(joined if in_fence else rewrite_segment(joined))
        return "\n".join(out_lines)
    except Exception:
        return text
