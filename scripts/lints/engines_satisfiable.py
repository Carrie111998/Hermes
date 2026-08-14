"""package.json ``engines`` must be satisfiable by a toolchain we ship.

``engine-strict=true`` in ``.npmrc`` makes ``engines`` a hard gate on
every ``npm ci`` / ``npm install`` — the installer's workspace step,
``hermes update``'s dependency refresh, and CI alike. A floor nobody's
toolchain can meet is not strict-hygiene; it is a total install outage.

That happened: ``engines.npm`` was raised to ``>=12.0.0`` while no Node
release bundled npm 12 (Node 26 shipped 11.17.0). Every fresh install
died at the first ``npm ci``. Migrated from
``tests/test_engines_satisfiable.py``, which lived in the *python* pytest
lane — a ``package.json``-only PR never ran it (the classifier gap this
always-on lint closes).

Behavioral, not a snapshot: each check asserts a relationship between
the floor we declare and the toolchain that has to satisfy it.
"""

from __future__ import annotations

import json
import re

from lints import REPO_ROOT, Finding, Lint

# npm releases bundled with a Node major, newest-per-major. Not a catalog
# snapshot: the point is that *some* real, shipping toolchain must clear
# the floor, and these are the ones users actually arrive with.
STOCK_NPM_BY_NODE_MAJOR = {
    20: "10.8.2",
    22: "10.9.8",
    24: "11.16.0",
    26: "11.17.0",
}

# npm 11.10-11.16 honor `min-release-age` but ignore
# `min-release-age-exclude`; .npmrc sets both, so that band fails installs
# with ETARGET on any freshly published dependency in the exclude list.
NPM_BAND_IGNORING_EXCLUDES = ("11.10.0", "11.12.1", "11.16.0")
NPM_VERSIONS_HANDLING_EXCLUDES = ("10.9.8", "11.17.0", "12.0.2")

# The tightest Node floor any apps/desktop dependency actually declares
# (react-router 8.3.0 -> >=22.22.0; Vite needs node:util.styleText). A
# desktop floor above the toolchain's own floor replaces working user
# toolchains for nothing.
DESKTOP_TOOLCHAIN_NODE = "22.22.0"


def _parse_mmp(version: str) -> tuple[int, int, int]:
    parts = version.split("-", 1)[0].split(".")
    nums = [int(p) for p in parts[:3]]
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]


def _satisfies_clause(version: str, clause: str) -> bool:
    """Evaluate one ``>=x.y.z`` / ``<x.y.z`` / ``^x.y.z`` comparator."""
    clause = clause.strip()
    if clause.startswith("^"):
        have, want = _parse_mmp(version), _parse_mmp(clause[1:].strip())
        return have[0] == want[0] and have >= want
    for op in (">=", "<=", "<", ">", "="):
        if clause.startswith(op):
            bound = clause[len(op) :].strip()
            break
    else:
        op, bound = "=", clause
    have, want = _parse_mmp(version), _parse_mmp(bound)
    return {
        ">=": have >= want,
        "<=": have <= want,
        "<": have < want,
        ">": have > want,
        "=": have == want,
    }[op]


def satisfies_range(version: str, spec: str) -> bool:
    """The ``A || B`` / space-joined-AND subset of semver we author."""
    for alternative in spec.split("||"):
        clauses = [c for c in alternative.strip().split() if c]
        if clauses and all(_satisfies_clause(version, c) for c in clauses):
            return True
    return False


def _finding(message: str, path: str = "package.json") -> Finding:
    return Finding(lint_id="engines-satisfiable", path=path, message=message)


def check() -> list[Finding]:
    findings: list[Finding] = []
    manifest = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    engines = manifest.get("engines", {})
    npm_range = engines.get("npm", "")
    node_range = engines.get("node", "")

    # 1. Some stock Node must bundle an npm our floor accepts, or a fresh
    #    install cannot run `npm ci` at all.
    if npm_range and not any(
        satisfies_range(npm, npm_range) for npm in STOCK_NPM_BY_NODE_MAJOR.values()
    ):
        findings.append(
            _finding(
                f"engines.npm is {npm_range!r}, which no shipping Node bundles "
                f"(checked {STOCK_NPM_BY_NODE_MAJOR}). With engine-strict=true "
                "every fresh install fails at the first `npm ci`."
            )
        )

    # 2. The npm band that ignores min-release-age-exclude must stay out.
    for bad in NPM_BAND_IGNORING_EXCLUDES:
        if npm_range and satisfies_range(bad, npm_range):
            findings.append(
                _finding(
                    f"engines.npm {npm_range!r} accepts npm {bad}, which honors "
                    "min-release-age but ignores min-release-age-exclude — it "
                    "fails ETARGET on every freshly published dependency in "
                    ".npmrc's exclude list."
                )
            )
    for good in NPM_VERSIONS_HANDLING_EXCLUDES:
        if npm_range and not satisfies_range(good, npm_range):
            findings.append(
                _finding(
                    f"engines.npm {npm_range!r} rejects npm {good}, which "
                    "handles .npmrc correctly and should be usable."
                )
            )

    # 3. The Node major install.sh provisions must clear engines.node.
    install_sh = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    managed_major = None
    for line in install_sh.splitlines():
        if line.startswith("NODE_VERSION="):
            managed_major = int(line.split("=", 1)[1].strip().strip("\"'"))
            break
    if managed_major is None:
        findings.append(
            _finding("install.sh no longer defines NODE_VERSION", "scripts/install.sh")
        )
    elif node_range:
        floor_majors = [int(m.group(1)) for m in re.finditer(r">=\s*v?(\d+)", node_range)]
        if not floor_majors:
            findings.append(
                _finding(f"cannot read a floor out of engines.node {node_range!r}")
            )
        elif managed_major < min(floor_majors):
            findings.append(
                _finding(
                    f"engines.node is {node_range!r} but install.sh provisions "
                    f"Node {managed_major}.x — the runtime we ship cannot "
                    "satisfy the floor we declare."
                )
            )

    # 4. apps/desktop must not demand more Node than its own build tools.
    desktop = json.loads(
        (REPO_ROOT / "apps" / "desktop" / "package.json").read_text(encoding="utf-8")
    )
    desktop_range = desktop.get("engines", {}).get("node", "")
    if desktop_range and not satisfies_range(DESKTOP_TOOLCHAIN_NODE, desktop_range):
        findings.append(
            _finding(
                f"apps/desktop engines.node is {desktop_range!r}, stricter than "
                f"its own toolchain floor ({DESKTOP_TOOLCHAIN_NODE}) — a floor "
                "above the build tools' replaces working user toolchains for "
                "nothing.",
                "apps/desktop/package.json",
            )
        )

    # 5. The lockfile's engines mirror must match, or a stale mirror
    #    re-imposes the old floor on `npm ci`.
    lock = json.loads((REPO_ROOT / "package-lock.json").read_text(encoding="utf-8"))
    if lock.get("packages", {}).get("", {}).get("engines") != engines:
        findings.append(
            _finding(
                "package-lock.json's root engines mirror differs from "
                "package.json — a stale mirror re-imposes the old floor on "
                "`npm ci`. Run `npm install --package-lock-only`.",
                "package-lock.json",
            )
        )

    return findings


LINT = Lint(
    id="engines-satisfiable",
    description=(
        "package.json engines floors must be satisfiable by a toolchain we "
        "actually ship (engine-strict makes them a hard install gate)."
    ),
    severity="blocking",
    autofix=False,
    check=check,
)
