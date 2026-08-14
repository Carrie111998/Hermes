"""Every npm project must carry its own age-gated ``.npmrc``.

``min-release-age`` is the supply-chain quarantine. npm refuses to
resolve a version that is less than N days old. A compromised release
must survive N days of public examination before it can enter a build.

npm reads this setting from the project, and it does not read a parent
directory. It reads the current directory, ``$HOME``, and the global
config. A request to search parent directories is still open
(npm/npm#11437).

The root ``.npmrc`` therefore protects the root install and no other.
A nested project has its own ``package-lock.json``. That project
resolves with an open age gate unless a ``.npmrc`` is beside the
lockfile. Review does not show this. The nested install works, and the
quarantine is absent.

A project is a directory that holds both ``package-lock.json`` and
``package.json``. ``npm install`` resolves against that pair. A
lockfile with no manifest beside it is a vendored artifact. For
example, ``nix/node-gyp-11-4-0-package-lock.json`` is an input to a
hash-pinned nix fetch, and that fetch never reads npm config.

This lint reads the required value from the root ``.npmrc``. It is not
a constant in this file. One edit to the root file therefore raises the
standard for every project. A nested project can be more strict. It
cannot be less strict.

This lint has no fixer. A new age gate changes what a fresh install
resolves in that project. That change needs review, not a bot patch.
"""

from __future__ import annotations

import re
import subprocess

from lints import REPO_ROOT, Finding, Lint

_MIN_AGE_RE = re.compile(r"^min-release-age=(?P<days>\d+)\s*$", re.MULTILINE)


def parse_min_age(npmrc_text: str) -> int | None:
    """Return the ``min-release-age`` value in an .npmrc, or None."""
    m = _MIN_AGE_RE.search(npmrc_text)
    return int(m.group("days")) if m else None


def _tracked(pattern: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", pattern, f"**/{pattern}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return list(dict.fromkeys(out.stdout.split()))


def npm_project_dirs() -> list[str]:
    """Return the repo-relative dirs that hold a lockfile and a manifest.

    The repo root is ``.``, which agrees with the relative paths that
    the other lints report.
    """
    manifests = {p.rsplit("/", 1)[0] if "/" in p else "." for p in _tracked("package.json")}
    locks = {p.rsplit("/", 1)[0] if "/" in p else "." for p in _tracked("package-lock.json")}
    return sorted(d for d in locks & manifests if not d.startswith("tests/"))


def check() -> list[Finding]:
    root_npmrc = REPO_ROOT / ".npmrc"
    required = (
        parse_min_age(root_npmrc.read_text(encoding="utf-8"))
        if root_npmrc.exists()
        else None
    )

    findings: list[Finding] = []
    for rel_dir in npm_project_dirs():
        base = REPO_ROOT if rel_dir == "." else REPO_ROOT / rel_dir
        npmrc = base / ".npmrc"
        where = ".npmrc" if rel_dir == "." else f"{rel_dir}/.npmrc"

        if not npmrc.exists():
            findings.append(
                Finding(
                    lint_id="npmrc-age-gate-coverage",
                    path=f"{where}",
                    message=(
                        f"npm project `{rel_dir}` has a package-lock.json and "
                        "no .npmrc beside it. Its installs resolve with no "
                        "min-release-age quarantine, because npm reads only "
                        "the project's own .npmrc and never a parent's. Add "
                        f"`min-release-age={required}` and "
                        "`engine-strict=true` beside the lockfile."
                    ),
                )
            )
            continue

        declared = parse_min_age(npmrc.read_text(encoding="utf-8"))
        if declared is None:
            findings.append(
                Finding(
                    lint_id="npmrc-age-gate-coverage",
                    path=where,
                    message=(
                        "no `min-release-age` directive. The installs of this "
                        "project resolve with the supply-chain quarantine "
                        f"disabled. Add `min-release-age={required}`."
                    ),
                )
            )
        elif required is not None and declared < required:
            findings.append(
                Finding(
                    lint_id="npmrc-age-gate-coverage",
                    path=where,
                    message=(
                        f"min-release-age={declared} is less than the repo "
                        f"standard of {required}, which the root .npmrc sets. "
                        "A nested project can be more strict. It cannot be "
                        "less strict."
                    ),
                )
            )
    return findings


LINT = Lint(
    id="npmrc-age-gate-coverage",
    description=(
        "every npm project needs its own .npmrc min-release-age, because "
        "npm does not read a parent directory's."
    ),
    severity="blocking",
    autofix=False,
    check=check,
)
