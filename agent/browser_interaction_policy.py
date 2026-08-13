"""Central policy for agent-driven visible browser interaction.

Routine agent work is isolated/headless by default.  A human-owned foreground
session may opt into visible browser control by setting
``HERMES_BROWSER_INTERACTION=visible``.  Dispatcher workers and delegated
children always fail closed, even if they inherit that process setting.

Artifact presentation remains outside this policy: desktop/TUI user gestures
and ``open_preview`` do not call these helpers.
"""
from __future__ import annotations

import os
import re
from typing import Optional

_POLICY_ENV = "HERMES_BROWSER_INTERACTION"
_VISIBLE_VALUES = frozenset({"visible", "interactive", "headed"})


def _is_unattended_context() -> bool:
    if os.environ.get("HERMES_KANBAN_TASK"):
        return True
    try:
        from agent.delegation_context import (
            is_delegated_child_process_context,
            is_dispatcher_owned_worker_context,
        )

        if is_delegated_child_process_context():
            return True
        # Cron runs spawned in-process from a worker are marked non-owner.
        if os.environ.get("HERMES_KANBAN_TASK") and not is_dispatcher_owned_worker_context():
            return True
    except Exception:
        pass
    return False


def visible_browser_allowed() -> bool:
    """Whether this execution may control or launch a visible browser."""
    if _is_unattended_context():
        return False
    return os.environ.get(_POLICY_ENV, "isolated").strip().lower() in _VISIBLE_VALUES


_BROWSER_EXECUTABLE = re.compile(
    r"(?:^|[;&|]\s*|\b(?:env|command|nohup)\s+)(?:[^\n;&|]*\s+)?"
    r"(?:google-chrome|chrome|chromium|chromium-browser|brave|msedge|firefox)"
    r"(?:\.exe)?(?:\s|$)",
    re.IGNORECASE,
)
_OS_OPENER_URL = re.compile(
    r"(?:^|[;&|]\s*)(?:open|xdg-open|start)(?:\s+-a\s+[^\n;&|]+)?\s+"
    r"(?:['\"])?https?://",
    re.IGNORECASE,
)
_PYTHON_WEBBROWSER = re.compile(
    r"\bpython(?:3(?:\.\d+)?)?\s+-m\s+webbrowser\b", re.IGNORECASE
)
_HEADLESS_FLAG = re.compile(r"--headless(?:=\S+)?\b", re.IGNORECASE)


def blocked_visible_browser_command(command: str) -> Optional[str]:
    """Return a refusal reason for obvious GUI-browser shell launches.

    This is defense in depth at the generic terminal boundary.  Network CLI
    clients and browser processes carrying an explicit ``--headless`` flag are
    unaffected.
    """
    if visible_browser_allowed() or not isinstance(command, str):
        return None
    if _PYTHON_WEBBROWSER.search(command) or _OS_OPENER_URL.search(command):
        return "Visible browser launch blocked by the isolated research policy."
    if _BROWSER_EXECUTABLE.search(command) and not _HEADLESS_FLAG.search(command):
        return "Headed browser launch blocked by the isolated research policy."
    return None
