"""No ``\\033[K`` (ANSI erase-to-EOL) in Python display code.

Policy (AGENTS.md, Known Pitfalls): under ``prompt_toolkit``'s
``patch_stdout`` the erase-to-EOL escape leaks as literal ``?[K`` text.
Spinner/status lines clear with space-padding instead:
``f"\\r{line}{' ' * pad}"``.

Comments explaining the pitfall are fine (agent/display.py documents
it); only the escape inside a string literal on a code line counts.
Suppress a deliberate use with ``# ansi-erase: ok``.
"""

from __future__ import annotations

import re
from pathlib import Path

from lints import REPO_ROOT, Finding, Lint

SUPPRESS_MARKER = re.compile(r"#\s*ansi-erase\s*:\s*ok\b", re.IGNORECASE)

# The escape as written in source: \033[K, \x1b[K, \e[K (any of the EL
# variants \033[0K/\033[1K/\033[2K included).
_ESCAPE_RE = re.compile(r"\\(?:033|x1b|e)\[[0-2]?K")

_SCAN_ROOTS = ("agent", "hermes_cli", "gateway", "tools", "cron", "tui_gateway")
_ROOT_MODULES = ("cli.py", "run_agent.py", "batch_runner.py")
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
    files += [REPO_ROOT / m for m in _ROOT_MODULES if (REPO_ROOT / m).exists()]
    return files


def scan_text(text: str) -> list[tuple[int, str]]:
    problems: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue  # comments ABOUT the pitfall are how it's documented
        if not _ESCAPE_RE.search(line):
            continue
        if SUPPRESS_MARKER.search(line):
            continue
        problems.append((i, line.strip()[:120]))
    return problems


def check() -> list[Finding]:
    findings: list[Finding] = []
    for path in _iter_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, snippet in scan_text(path.read_text(encoding="utf-8", errors="replace")):
            findings.append(
                Finding(
                    lint_id="no-ansi-erase-eol",
                    path=rel,
                    line=lineno,
                    message=(
                        f"\\033[K escape in display code ({snippet!r}) leaks "
                        "as literal `?[K` under prompt_toolkit's patch_stdout "
                        "— clear with space-padding instead: "
                        "f\"\\r{line}{' ' * pad}\". Suppress a deliberate use "
                        "with `# ansi-erase: ok`."
                    ),
                )
            )
    return findings


LINT = Lint(
    id="no-ansi-erase-eol",
    description=(
        "\\033[K in display code leaks as literal `?[K` under "
        "prompt_toolkit's patch_stdout — use space-padding."
    ),
    severity="blocking",
    autofix=False,
    check=check,
)
