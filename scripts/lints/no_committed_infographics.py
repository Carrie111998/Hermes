"""No PR-infographic images committed to the repo.

PR infographics are rendered to an image-provider URL (fal.media) and
embedded in the PR *description* — the PR body is the archive; the
binary never belongs in git history.

This leaked repeatedly: #48261 removed the first batch, #54564 added a
`.gitignore` rule, nine more PNGs (~14MB) landed in the four weeks AFTER
that rule, and #70552 caught an `infograficos/` spelling that sidestepped
the pattern entirely. A passive ignore rule cannot enforce a policy.

Migrated from ``.github/workflows/infographic-check.yml`` — same match
(the IMAGE, not a directory name: any infographic-ish path segment in
any spelling at any depth), one fewer workflow + runner.

Not autofixable: the fix is ``git rm --cached`` — index surgery the
autofix bot must not perform.
"""

from __future__ import annotations

import re
import subprocess

from lints import REPO_ROOT, Finding, Lint

# Keying on `infographic/` is what let `infograficos/` through in #70552.
_DIR_RE = re.compile(r"(^|/)(infograph|infograf)[^/]*/", re.IGNORECASE)
_IMG_RE = re.compile(r"\.(png|jpe?g|webp|gif)$", re.IGNORECASE)


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return out.stdout.splitlines()


def check() -> list[Finding]:
    return [
        Finding(
            lint_id="no-committed-infographics",
            path=path,
            message=(
                "PR infographic committed to the repo — the image belongs in "
                "the PR description (provider URL), never in git history. "
                "Fix: `git rm --cached <path>` (keeps your local copy), then "
                "embed the provider URL in the PR body."
            ),
        )
        for path in _tracked_files()
        if _DIR_RE.search(path) and _IMG_RE.search(path)
    ]


LINT = Lint(
    id="no-committed-infographics",
    description=(
        "PR-infographic images belong in the PR description, never in git "
        "history (.gitignore alone cannot enforce this)."
    ),
    severity="blocking",
    autofix=False,
    check=check,
)
