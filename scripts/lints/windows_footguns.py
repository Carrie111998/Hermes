"""Windows cross-platform footguns (wrapper).

The implementation is ``scripts/check-windows-footguns.py`` — it stays a
standalone script so `python scripts/check-windows-footguns.py --diff main`
keeps working for pre-PR local runs. This wrapper adapts its full-repo
mode to the lint registry so it runs whenever the lints job runs.
"""

from __future__ import annotations

import re
import subprocess
import sys

from lints import REPO_ROOT, Finding, Lint

_SCRIPT = REPO_ROOT / "scripts" / "check-windows-footguns.py"

# Matches the script's per-finding header line: ``path/to/file.py:123: [rule name]``
_HEADER = re.compile(r"^(?P<path>[^\s:][^:]*):(?P<line>\d+): \[(?P<rule>.+)\]$")


def check() -> list[Finding]:
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--all"],
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
            lint_id="windows-footguns",
            path=m.group("path"),
            line=int(m.group("line")),
            message=(
                f"{m.group('rule')} — suppress an intentional use with "
                "`# windows-footgun: ok`; run "
                "`python scripts/check-windows-footguns.py --list` for the rules"
            ),
        )
        for m in (_HEADER.match(line) for line in proc.stdout.splitlines())
        if m
    ]
    if not findings:
        # Non-zero exit but no parseable findings — the script itself broke.
        # Surface that loudly instead of reporting a clean pass.
        raise RuntimeError(
            f"check-windows-footguns.py exited {proc.returncode} with no "
            f"parseable findings; stderr: {proc.stderr.strip()[:500]}"
        )
    return findings


LINT = Lint(
    id="windows-footguns",
    description="Windows-unsafe Python primitives (os.kill(pid,0), bare SIGKILL, open() without encoding=, ...).",
    severity="blocking",
    autofix=False,
    check=check,
)
