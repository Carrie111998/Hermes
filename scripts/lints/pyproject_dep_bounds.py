"""Every pyproject dependency needs an upper bound; pins must agree.

Repo policy (AGENTS.md, Dependency Pinning, established after the
litellm compromise): every PyPI dependency is declared
``>=floor,<next_major`` (or ``==exact`` / ``~=``); git URLs are pinned
to a commit SHA. A bare ``>=X.Y.Z`` — or no specifier at all — lets the
next malicious release walk straight into a fresh install.

Also migrated from ``tests/test_packaging_metadata.py``: no package may
be exact-pinned to two different versions across
``[project.dependencies]`` and the extras (a package appearing in
several extras must use the SAME version everywhere — divergent pins
mean the installed version depends on install order).

Not autofixable: choosing a ceiling or reconciling divergent pins is a
judgment call.
"""

from __future__ import annotations

import re
import tomllib

from lints import REPO_ROOT, Finding, Lint


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _split_requirement(req: str) -> tuple[str, str, str]:
    """``(name, url_part, specifier_part)`` from a PEP 508 string."""
    body = req.split(";", 1)[0].strip()  # drop environment markers
    if "@" in body:
        name_part, url = body.split("@", 1)
        name = name_part.split("[", 1)[0].strip()
        return name, url.strip(), ""
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(.*)$", body)
    if not m:
        return body, "", ""
    return m.group(1), "", m.group(2).strip()


def has_upper_bound(specifier: str) -> bool:
    """True when the specifier set caps the admissible versions."""
    if not specifier:
        return False
    for clause in specifier.split(","):
        clause = clause.strip()
        if clause.startswith(("==", "~=", "<", "<=", "===")):
            return True
    return False


_GIT_SHA_RE = re.compile(r"@[0-9a-f]{40}$")

# Exact-pin extraction for the consistency check ("name==version",
# tolerant of extras and trailing markers).
_PIN_RE = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*([^\s;,#]+)"
)


def _all_requirements(pyproject: dict) -> list[tuple[str, str]]:
    """``(where, requirement)`` for core deps + every extra."""
    project = pyproject.get("project", {})
    out = [("project.dependencies", r) for r in project.get("dependencies", [])]
    for extra, reqs in project.get("optional-dependencies", {}).items():
        out += [(f"optional-dependencies.{extra}", r) for r in reqs]
    return out


def check() -> list[Finding]:
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    findings: list[Finding] = []
    project_name = _canonical(pyproject.get("project", {}).get("name", ""))

    pins: dict[str, dict[str, set[str]]] = {}
    for where, req in _all_requirements(pyproject):
        name, url, specifier = _split_requirement(req)
        # Self-referential extras (`hermes-agent[mcp]` inside [all]) are
        # recursive includes of this same project, not external deps —
        # there is no version to bound.
        if _canonical(name) == project_name:
            continue
        if url:
            if url.startswith("git+") and not _GIT_SHA_RE.search(url):
                findings.append(
                    Finding(
                        lint_id="pyproject-dep-bounds",
                        path="pyproject.toml",
                        message=(
                            f"[{where}] {req!r}: git dependency is not pinned "
                            "to a 40-char commit SHA — a branch/tag ref is a "
                            "repointable supply-chain target."
                        ),
                    )
                )
            continue
        if not has_upper_bound(specifier):
            findings.append(
                Finding(
                    lint_id="pyproject-dep-bounds",
                    path="pyproject.toml",
                    message=(
                        f"[{where}] {req!r} has no upper bound — declare "
                        "`>=floor,<next_major` (or ==exact / ~=) so the next "
                        "compromised release can't walk into a fresh install."
                    ),
                )
            )
        if m := _PIN_RE.match(req):
            pins.setdefault(_canonical(m.group(1)), {}).setdefault(
                m.group(2), set()
            ).add(where)

    for name, versions in sorted(pins.items()):
        if len(versions) > 1:
            detail = "; ".join(
                f"{v} in {', '.join(sorted(wheres))}"
                for v, wheres in sorted(versions.items())
            )
            findings.append(
                Finding(
                    lint_id="pyproject-dep-bounds",
                    path="pyproject.toml",
                    message=(
                        f"{name} is exact-pinned to different versions across "
                        f"pyproject sections ({detail}) — the installed version "
                        "would depend on install order. Align them."
                    ),
                )
            )
    return findings


LINT = Lint(
    id="pyproject-dep-bounds",
    description=(
        "pyproject dependencies need upper bounds (>=floor,<ceiling), git "
        "deps need SHA pins, and exact pins must agree across extras."
    ),
    severity="blocking",
    autofix=False,
    check=check,
)
