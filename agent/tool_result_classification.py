"""Shared helpers for classifying tool result payloads."""

from __future__ import annotations

import json
import os
from typing import Any, Optional, Tuple


FILE_MUTATING_TOOL_NAMES = frozenset({"write_file", "patch"})


# A cheap, comparable snapshot of a path's on-disk state: (exists, mtime_ns, size).
PathFingerprint = Tuple[bool, int, int]


def path_change_fingerprint(
    path: str,
    base: Optional[str] = None,
) -> Optional[PathFingerprint]:
    """Snapshot *path*'s on-disk state so a later call can detect a change.

    This exists because ``FILE_MUTATING_TOOL_NAMES`` is — and can only ever
    be — an incomplete list.  ``write_file`` and ``patch`` are the tools the
    verifier can *watch*; they are not the only tools that can *write*.
    ``terminal`` (``python -c``, ``sed -i``, a shell redirect), the
    code-execution tool, an MCP server, and any plugin can all land a file
    without the verifier seeing a thing.  When a watched write fails and an
    unwatched one then succeeds on the same path, tool-level bookkeeping
    alone reports "NOT modified" about a file that was in fact modified — a
    false negative that teaches the user to ignore the footer, which is
    strictly worse than having no verifier at all.

    So the verifier asks the filesystem rather than the tool registry:
    fingerprint the path when a write fails, re-fingerprint when the footer
    renders, and drop the entry only if the two differ.

    Returns ``None`` when the path cannot be resolved or stat'd for any
    reason other than "does not exist".  Callers MUST treat ``None`` as
    *unknown* and keep warning — a verifier that goes quiet when confused is
    the exact failure mode this helper exists to prevent.  A path that simply
    does not exist is a perfectly comparable state and is reported as
    ``(False, 0, 0)``, so a failed create followed by a successful
    out-of-band create is still detected.
    """
    candidates = []
    if os.path.isabs(path):
        candidates.append(path)
    else:
        for anchor in (base, os.environ.get("TERMINAL_CWD"), os.getcwd()):
            if anchor:
                candidates.append(os.path.join(anchor, path))
        candidates.append(path)

    saw_missing = False
    for candidate in candidates:
        try:
            st = os.stat(candidate)
        except (FileNotFoundError, NotADirectoryError):
            saw_missing = True
            continue
        except Exception:
            return None
        return (True, st.st_mtime_ns, st.st_size)
    return (False, 0, 0) if saw_missing else None


# Tools whose interrupted/dangling execution is safe to discard because they
# cannot mutate either external state or Hermes session state. Unknown/plugin/
# MCP tools stay effect-capable by default.
NO_EFFECT_TOOL_NAMES = frozenset({
    "read_file", "search_files", "session_search", "skill_view", "skills_list",
    "web_extract", "web_search", "vision_analyze", "browser_snapshot",
    "browser_get_images", "browser_console", "read_terminal",
})


def tool_may_have_side_effect(tool_name: str) -> bool:
    return tool_name not in NO_EFFECT_TOOL_NAMES


def file_mutation_result_landed(tool_name: str, result: Any) -> bool:
    """Return True when a file mutation result proves the write landed."""
    if tool_name not in FILE_MUTATING_TOOL_NAMES or not isinstance(result, str):
        return False
    try:
        data = json.loads(result.strip())
    except Exception:
        return False
    if not isinstance(data, dict) or data.get("error"):
        return False
    if tool_name == "write_file":
        return "bytes_written" in data
    if tool_name == "patch":
        return data.get("success") is True
    return False
