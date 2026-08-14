"""Every GitHub Actions ``uses:`` must be pinned to a commit SHA.

Repo policy (AGENTS.md, Dependency Pinning): third-party actions are
referenced as ``owner/repo@<40-char-sha>  # vN`` — a tag or branch ref
is a moving target the action's owner (or an attacker who compromises
them) can repoint at arbitrary code that runs with our workflow
permissions. Established after the litellm compromise and reinforced
after the Mini Shai-Hulud campaign; previously enforced only by review.

Exempt: repo-local actions (``./.github/...``) and ``docker://`` images
pinned by digest.

Not autofixable: resolving a tag to the right SHA needs the network and
a trust decision — exactly what a reviewer should do.
"""

from __future__ import annotations

import re
from pathlib import Path

from lints import REPO_ROOT, Finding, Lint

_USES_RE = re.compile(r"""^\s*-?\s*uses:\s*['"]?(?P<ref>[^'"\s#]+)""")
_SHA_PIN_RE = re.compile(r"^[^@]+@[0-9a-f]{40}$")
_DIGEST_PIN_RE = re.compile(r"^docker://.+@sha256:[0-9a-f]{64}$")


def _workflow_files() -> list[Path]:
    files: list[Path] = []
    for pattern in ("*.yml", "*.yaml"):
        files += sorted((REPO_ROOT / ".github" / "workflows").glob(pattern))
    actions_dir = REPO_ROOT / ".github" / "actions"
    if actions_dir.is_dir():
        for pattern in ("action.yml", "action.yaml"):
            files += sorted(actions_dir.glob(f"*/{pattern}"))
    return files


def unpinned_uses(text: str) -> list[tuple[int, str]]:
    """``(lineno, ref)`` for every uses: ref that is not SHA-pinned."""
    problems: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = _USES_RE.match(line)
        if not m:
            continue
        ref = m.group("ref")
        if ref.startswith("./"):
            continue  # repo-local composite action — pinned by the checkout
        if ref.startswith("docker://"):
            if not _DIGEST_PIN_RE.match(ref):
                problems.append((lineno, ref))
            continue
        if not _SHA_PIN_RE.match(ref):
            problems.append((lineno, ref))
    return problems


def check() -> list[Finding]:
    findings: list[Finding] = []
    for path in _workflow_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, ref in unpinned_uses(path.read_text(encoding="utf-8")):
            findings.append(
                Finding(
                    lint_id="workflow-sha-pins",
                    path=rel,
                    line=lineno,
                    message=(
                        f"`uses: {ref}` is not pinned to a commit SHA — tags "
                        "and branches are moving targets that run with this "
                        "workflow's permissions. Pin as "
                        "`owner/repo@<40-char-sha>  # vN`."
                    ),
                )
            )
    return findings


LINT = Lint(
    id="workflow-sha-pins",
    description=(
        "GitHub Actions uses: refs must be commit-SHA pinned (tags are "
        "repointable supply-chain targets)."
    ),
    severity="blocking",
    autofix=False,
    check=check,
)
