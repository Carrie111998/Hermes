"""Every package.json dependency must be an exact version.

Same supply-chain rationale as the pyproject upper-bound policy, taken
to the stance npm's lockfile model makes cheap: a range specifier
r(``^1.2.3``, ``~1.2.3``, ``>=1``) means ``npm install`` / ``npm update``
can silently float onto a release published five minutes ago, and the
diff that admits it is a lockfile churn nobody reads. Exact versions
make every upgrade an explicit, reviewable one-line change, with
`min-release-age` and the autofix bot handling the update cadence.

Allowed values: an exact semver version (``1.2.3``, with an optional
prerelease or build suffix), an alias to an exact version
(``npm:@scope/pkg@1.2.3``), and a local reference (``file:``,
``link:``, ``workspace:``). Every other value is a finding: a range, a
tag such as ``latest``, a bare ``*``, and a remote URL.

This lint reads ``overrides`` with the three dependency fields, and
that field matters more than the other three. An override forces a
version on every transitive consumer. A range in an override therefore
re-floats packages that the manifest does not name. Override values
also nest, because a key can map to a table of child overrides instead
of a specifier. The walk is recursive for that reason. The reserved
``.`` key inside a nested table is a specifier like any other.

This lint reads every tracked package.json, except the fixtures under
tests/. It has no fixer. A pin changes what installs, the correct pin
is the locked version, and that choice needs review.
"""

from __future__ import annotations

import json
import re
import subprocess

from lints import REPO_ROOT, Finding, Lint

_DEP_FIELDS = ("dependencies", "devDependencies", "optionalDependencies")
# An override forces a version on transitive deps. A range here is
# therefore wider than a range in a direct dependency, not narrower.
_OVERRIDE_FIELDS = ("overrides",)
_EXACT_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
# `npm:<name>@<version>` points a dependency at a different package.
# The version tail obeys the same rule as a plain specifier.
_ALIAS_RE = re.compile(r"^npm:(?P<name>@?[^@]+)@(?P<version>.+)$")
_LOCAL_PREFIXES = ("file:", "link:", "workspace:")


def is_exact(spec: str) -> bool:
    """Return True when the specifier admits one published version."""
    if spec.startswith(_LOCAL_PREFIXES):
        return True
    if m := _ALIAS_RE.match(spec):
        return bool(_EXACT_RE.match(m.group("version")))
    return bool(_EXACT_RE.match(spec))


def _tracked_manifests() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "package.json", "**/package.json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return [
        p
        for p in dict.fromkeys(out.stdout.split())
        if not p.startswith("tests/")
    ]


def _walk(node: dict, prefix: str, problems: list[tuple[str, str, str]]) -> None:
    for name, spec in node.items():
        where = f"{prefix}.{name}"
        if isinstance(spec, dict):
            # A nested override table. Recurse into it. Its "." key,
            # when present, is the specifier for the parent package.
            _walk(spec, where, problems)
            continue
        if not isinstance(spec, str):
            problems.append((prefix, name, repr(spec)))
            continue
        if not is_exact(spec):
            problems.append((prefix, name, spec))


def non_exact_deps(manifest: dict) -> list[tuple[str, str, str]]:
    """``(field, name, spec)`` for every non-exact dependency value."""
    problems: list[tuple[str, str, str]] = []
    for field in _DEP_FIELDS + _OVERRIDE_FIELDS:
        _walk(manifest.get(field) or {}, field, problems)
    return problems


def check() -> list[Finding]:
    findings: list[Finding] = []
    for rel in _tracked_manifests():
        try:
            manifest = json.loads((REPO_ROOT / rel).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for field, name, spec in non_exact_deps(manifest):
            findings.append(
                Finding(
                    lint_id="packagejson-exact-versions",
                    path=rel,
                    message=(
                        f"{field}.{name} = {spec!r} is not an exact version. "
                        "A range floats onto a release that is minutes old, "
                        "through lockfile churn that nobody reads. Pin the "
                        "locked version exactly. min-release-age controls "
                        "the update cadence."
                    ),
                )
            )
    return findings


LINT = Lint(
    id="packagejson-exact-versions",
    description=(
        "package.json dependencies and overrides must be exact versions, "
        "never ranges or tags."
    ),
    severity="blocking",
    autofix=False,
    check=check,
)
