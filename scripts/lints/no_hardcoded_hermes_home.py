"""No hardcoded ``~/.hermes`` paths in production code.

Policy (AGENTS.md, Known Pitfalls): state paths go through
``get_hermes_home()`` from ``hermes_constants`` (and user-facing
messages through ``display_hermes_home()``). Hardcoding
``Path.home() / ".hermes"`` or ``expanduser("~/.hermes")`` breaks
profiles — each profile has its own HERMES_HOME — and was the source of
five bugs fixed in PR #3575.

Legitimate uses exist and stay: the ``except ImportError`` fallback
inside ``hermes_constants`` helpers, and code that is deliberately
HOME-anchored rather than profile-anchored (the desktop-ssh token root,
profile-root comparisons, the pytest auth seatbelt). Those sites all
name ``get_hermes_home`` / ``HERMES_HOME`` within a few lines, so the
scanner treats a nearby mention as a guard hint — same philosophy as
check-windows-footguns: false negatives are fine, the inline marker is
the authoritative override.

Suppress an intentional use with ``# hermes-home: ok`` on the line.
"""

from __future__ import annotations

import re
from pathlib import Path

from lints import REPO_ROOT, Finding, Lint

SUPPRESS_MARKER = re.compile(r"#\s*hermes-home\s*:\s*ok\b", re.IGNORECASE)

# The literal shapes that break profiles.
_PATTERNS = (
    re.compile(r"Path\.home\(\)\s*/\s*['\"]\.hermes['\"]"),
    re.compile(r"expanduser\(\s*['\"]~/\.hermes"),
    re.compile(r"Path\(\s*['\"]~/\.hermes"),
)

# A mention of the profile machinery near the match means the author is
# working WITH the profile system (fallbacks, deliberate HOME anchors),
# not around it.
_GUARD_HINTS = ("get_hermes_home", "display_hermes_home", "HERMES_HOME",
                "hermes_constants", "get_default_hermes_root")
_GUARD_WINDOW = 8  # lines of preceding context searched for a hint

# Production Python roots (mirrors check-windows-footguns --all).
_SCAN_ROOTS = (
    "hermes_cli", "gateway", "tools", "cron", "agent", "plugins",
    "acp_adapter", "tui_gateway",
)
_ROOT_MODULES_GLOB = "*.py"  # top-level modules (run_agent.py, cli.py, ...)

# hermes_constants IS the abstraction — its body defines the fallback.
_EXEMPT_FILES = {"hermes_constants.py"}
_EXCLUDED_DIRS = {"__pycache__", "node_modules", ".venv", "venv", "tests"}


def _iter_files() -> list[Path]:
    files: list[Path] = []
    for root in _SCAN_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if not any(part in _EXCLUDED_DIRS for part in path.parts):
                files.append(path)
    for path in sorted(REPO_ROOT.glob(_ROOT_MODULES_GLOB)):
        if path.name not in _EXEMPT_FILES:
            files.append(path)
    return files


def scan_text(text: str) -> list[tuple[int, str]]:
    """``(lineno, matched_snippet)`` for unsuppressed hardcoded paths."""
    lines = text.splitlines()
    problems: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        # Comments and prose strings (guidance text) are not code paths.
        if stripped.startswith(("#", '"', "'")):
            continue
        if not any(p.search(line) for p in _PATTERNS):
            continue
        if SUPPRESS_MARKER.search(line):
            continue
        window = lines[max(0, i - _GUARD_WINDOW) : i + 1]
        if any(hint in wline for wline in window for hint in _GUARD_HINTS):
            continue
        problems.append((i + 1, line.strip()[:120]))
    return problems


def check() -> list[Finding]:
    findings: list[Finding] = []
    for path in _iter_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, snippet in scan_text(path.read_text(encoding="utf-8", errors="replace")):
            findings.append(
                Finding(
                    lint_id="no-hardcoded-hermes-home",
                    path=rel,
                    line=lineno,
                    message=(
                        f"hardcoded ~/.hermes path ({snippet!r}) breaks "
                        "profiles — use get_hermes_home() from "
                        "hermes_constants (display_hermes_home() for "
                        "user-facing text), or mark a deliberate HOME anchor "
                        "with `# hermes-home: ok`."
                    ),
                )
            )
    return findings


LINT = Lint(
    id="no-hardcoded-hermes-home",
    description=(
        "hardcoded ~/.hermes paths break profiles — state paths go through "
        "get_hermes_home()."
    ),
    severity="blocking",
    autofix=False,
    check=check,
)
