"""SKILL.md frontmatter meets the authoring standards.

Policy (AGENTS.md, "Skill authoring standards (HARDLINE)"), previously
enforced only by reviewers rejecting PRs:

1. ``description`` — present, <= 60 characters, ends with a period, no
   marketing words ("powerful", "comprehensive", "seamless", "advanced").
2. ``name`` — present.
3. ``platforms:`` — required when the skill's scripts import POSIX-only
   primitives (fcntl, termios, os.setsid, os.killpg, bare
   signal.SIGKILL, /proc paths). Default posture is fix-it-
   cross-platform; the gate just refuses the silent third option of
   shipping POSIX-bound code with no declaration.

Scans ``skills/`` and ``optional-skills/``. Third-party skills in a
user's ~/.hermes are out of scope — this is a repo-tree standard.
"""

from __future__ import annotations

import re
from pathlib import Path

from lints import REPO_ROOT, Finding, Lint

MAX_DESCRIPTION = 60
MARKETING_WORDS = ("powerful", "comprehensive", "seamless", "advanced")

_DESC_RE = re.compile(r"^description:\s*(.+)$", re.MULTILINE)
_NAME_RE = re.compile(r"^name:\s*\S", re.MULTILINE)
_PLATFORMS_RE = re.compile(r"^platforms:\s*\S", re.MULTILINE)

# POSIX-only primitives that make a script platform-bound (subset of the
# AGENTS.md list that is reliably greppable without AST work).
_POSIX_RE = re.compile(
    r"\b(?:import fcntl|import termios|os\.setsid\b|os\.killpg\b"
    r"|signal\.SIGKILL\b|['\"]/proc/)"
)


def _skill_dirs() -> list[Path]:
    dirs: set[Path] = set()
    for root in ("skills", "optional-skills"):
        base = REPO_ROOT / root
        if base.is_dir():
            dirs.update(p.parent for p in base.rglob("SKILL.md"))
    return sorted(dirs)


def _frontmatter_line(text: str, pattern: re.Pattern) -> tuple[int, str] | None:
    m = pattern.search(text)
    if not m:
        return None
    return text[: m.start()].count("\n") + 1, m.group(0)


def check_skill(skill_dir: Path) -> list[tuple[int | None, str]]:
    """``(lineno, message)`` violations for one skill directory."""
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    problems: list[tuple[int | None, str]] = []

    if not _NAME_RE.search(text):
        problems.append((None, "SKILL.md frontmatter has no `name:` field"))

    desc_hit = _frontmatter_line(text, _DESC_RE)
    if desc_hit is None:
        problems.append((None, "SKILL.md frontmatter has no `description:` field"))
    else:
        lineno, raw = desc_hit
        desc = raw.split(":", 1)[1].strip().strip("\"'")
        if len(desc) > MAX_DESCRIPTION:
            problems.append(
                (
                    lineno,
                    f"description is {len(desc)} chars (max {MAX_DESCRIPTION}) "
                    "— long descriptions bloat skill listings and dilute "
                    "model attention",
                )
            )
        if desc and not desc.endswith("."):
            problems.append((lineno, "description must end with a period"))
        lowered = desc.lower()
        hits = [w for w in MARKETING_WORDS if w in lowered]
        if hits:
            problems.append(
                (
                    lineno,
                    f"description contains marketing words {hits} — state the "
                    "capability, not the sales pitch",
                )
            )

    if not _PLATFORMS_RE.search(text):
        for script in sorted(skill_dir.rglob("*.py")):
            src = script.read_text(encoding="utf-8", errors="replace")
            m = _POSIX_RE.search(src)
            if m:
                problems.append(
                    (
                        None,
                        f"{script.relative_to(skill_dir)} uses POSIX-only "
                        f"primitive `{m.group(0)}` but SKILL.md declares no "
                        "`platforms:` — fix it cross-platform or gate the "
                        "skill (e.g. `platforms: [linux, macos]`)",
                    )
                )
                break

    return problems


def check() -> list[Finding]:
    findings: list[Finding] = []
    for skill_dir in _skill_dirs():
        rel = (skill_dir / "SKILL.md").relative_to(REPO_ROOT).as_posix()
        for lineno, message in check_skill(skill_dir):
            findings.append(
                Finding(
                    lint_id="skill-frontmatter-standards",
                    path=rel,
                    line=lineno,
                    message=message,
                )
            )
    return findings


LINT = Lint(
    id="skill-frontmatter-standards",
    description=(
        "SKILL.md descriptions stay <= 60 chars/end with a period, and "
        "POSIX-bound scripts declare platforms."
    ),
    severity="blocking",
    autofix=False,
    check=check,
)
