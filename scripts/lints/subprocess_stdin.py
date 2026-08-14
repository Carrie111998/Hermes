"""TUI-context subprocess calls must set stdin= (wrapper).

The implementation is ``scripts/check_subprocess_stdin.py``. In TUI mode
the gateway child talks JSON-RPC to the Node parent over stdin; a
subprocess that inherits fd 0 can EOF the whole gateway mid-tool-call
(issue #14036). Until this registration the checker was wired into no
workflow at all — it existed but nothing ran it.

The script also scans user plugin dirs under ``get_hermes_home()`` when
they exist; in CI that home is empty, so the effective scope is the
repo's TUI-context dirs (agent/, tools/, plugins/, tui_gateway/).
"""

from __future__ import annotations

import re
import subprocess
import sys

from lints import REPO_ROOT, Finding, Lint

_SCRIPT = REPO_ROOT / "scripts" / "check_subprocess_stdin.py"

# Matches the script's per-violation line: ``  path/to/file.py:123: snippet``
_VIOLATION = re.compile(r"^\s+(?P<path>[^\s:][^:]*):(?P<line>\d+): (?P<snippet>.+)$")


def check() -> list[Finding]:
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
    )
    if proc.returncode == 0:
        return []

    findings = [
        Finding(
            lint_id="subprocess-stdin",
            path=m.group("path"),
            line=int(m.group("line")),
            message=(
                "subprocess call without stdin= inherits the TUI gateway's "
                "JSON-RPC fd — add stdin=subprocess.DEVNULL (or "
                "`# noqa: subprocess-stdin` when inheriting is intentional)"
            ),
        )
        for m in (_VIOLATION.match(line) for line in proc.stdout.splitlines())
        if m
    ]
    if not findings:
        raise RuntimeError(
            f"check_subprocess_stdin.py exited {proc.returncode} with no "
            f"parseable findings; stderr: {proc.stderr.strip()[:500]}"
        )
    return findings


LINT = Lint(
    id="subprocess-stdin",
    description="TUI-context subprocess calls must pass stdin= to avoid inheriting the JSON-RPC fd.",
    severity="blocking",
    autofix=False,
    check=check,
)
