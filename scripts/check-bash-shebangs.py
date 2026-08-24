#!/usr/bin/env python3
"""Find hardcoded ``#!/bin/bash`` shebangs in repository text files.

Hardcoded interpreter paths break on systems where bash is installed outside
``/bin``. The portable form is ``#!/usr/bin/env bash``.

Usage:
    python scripts/check-bash-shebangs.py
    python scripts/check-bash-shebangs.py --all
    python scripts/check-bash-shebangs.py --diff upstream/main
    python scripts/check-bash-shebangs.py path/to/file.md

The default scans staged files. ``--all`` scans the repository. ``--diff``
scans files changed from the given ref, including unstaged changes.

An intentional exception must carry a reason: the line immediately BEFORE the
shebang must contain ``# shebang: ok <reason>`` (a trailing marker on the
shebang line itself would be passed to bash as an interpreter argument and
break execution). A bare ``ok`` with nothing after it is rejected as a
violation, not honored. Non-shebang lines containing ``#!/bin/bash`` inside
generated script strings are flagged too, with the same above-line marker.
The checker is deliberately line-based so markdown code blocks,
generated-script strings, and test fixtures get the same rule.

Platform exceptions that do not fit the inline-marker model (for example a
Termux launcher shim that must reference the literal string in prose or a
regex) are handled by the path-based allowlist (``--allowlist FILE``), never
by weakening the shebang itself. See ALLOWLIST_FORMAT below.

Exit codes:
    0 - clean scan (or empty scope with --allow-empty)
    1 - violations found
    2 - tooling error (git lookup failed and the run cannot be trusted)
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent

# A suppression is only valid with a non-empty reason after `ok`. `ok` alone,
# `ok:` with nothing, or trailing whitespace does not suppress anything; that
# keeps the escape hatch auditable (every waiver names why it exists).
SUPPRESS_MARKER = re.compile(
    r"#\s*shebang\s*:\s*ok[\s:]+(?P<reason>\S.*?)\s*$", re.IGNORECASE
)

# A shebang is the FIRST line starting with `#!`. Anything else is an embedded
# string (a generated script, a doc block, a fixture) and is reported as such.
# Embedded matches allow any non-path-extending trailing byte (a quote, a
# newline escape, whitespace) so `"...#!/bin/bash\n..."` inside a generated
# string is still caught.
SHEBANG_LINE_RE = re.compile(r"^#![ \t]*/bin/bash(?:[ \t]|\Z)")
EMBEDDED_SHEBANG_RE = re.compile(r"#![ \t]*/bin/bash(?![A-Za-z0-9_.-])")

# Keep generated dependencies and build products out of the scan. Tracked
# source, docs, tests, and generated-script strings remain in scope.
EXCLUDED_DIRS = {
    ".git",
    "node_modules",
    "venv",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "site-packages",
}

EXCLUDED_FILES = {
    # This file documents the pattern it detects.
    "scripts/check-bash-shebangs.py",
}

TEXT_SUFFIXES = {
    ".bash",
    ".bat",
    ".cmd",
    ".css",
    ".html",
    ".js",
    ".jsx",
    ".json",
    ".md",
    ".nix",
    ".ps1",
    ".py",
    ".pyi",
    ".rs",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}

ALLOWLIST_FORMAT = """\
# allowlist format (one glob per line):
#   - blank lines and #-prefixed comments are ignored
#   - each line is a repo-relative glob matched against the POSIX path,
#     e.g. `optional-skills/**` or `plugins/security-guidance/*.py`
#   - a line may carry a trailing comment: `path/to/file.sh  # termux shim`
"""


def should_scan_file(path: Path) -> bool:
    """Return True when path is a supported source or documentation file."""
    if any(part in EXCLUDED_DIRS for part in path.parts):
        return False
    try:
        rel = path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        rel = ""
    if rel in EXCLUDED_FILES:
        return False
    return path.suffix.lower() in TEXT_SUFFIXES


def iter_files(paths: Iterable[Path]) -> Iterable[Path]:
    for path in paths:
        if path.is_file():
            if should_scan_file(path):
                yield path
            continue
        if not path.is_dir():
            continue
        for root, dirs, files in os.walk(path):
            dirs[:] = [name for name in dirs if name not in EXCLUDED_DIRS]
            for name in files:
                candidate = Path(root) / name
                if should_scan_file(candidate):
                    yield candidate


class GitLookupError(RuntimeError):
    """A git query needed to determine the scan scope failed."""


def git_paths(command: list[str]) -> list[Path]:
    """Return repository paths from a git command.

    Raises GitLookupError on failure: an empty result from a broken lookup
    would silently scan nothing and report success (fail-open). Callers
    decide whether to degrade to --all or exit nonzero.
    """
    try:
        output = subprocess.check_output(
            command,
            cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise GitLookupError(f"{' '.join(command)} failed: {exc}") from exc
    return [REPO_ROOT / name for name in output.splitlines() if name.strip()]


def get_staged_files() -> list[Path]:
    return git_paths(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"])


def get_diff_files(ref: str) -> list[Path]:
    return git_paths(["git", "diff", ref, "--name-only", "--diff-filter=ACMR"])


def load_allowlist(path: str) -> tuple[list[str], dict[str, str]]:
    """Parse the path-based allowlist file.

    Returns (glob patterns, {pattern: reason}) where reason may be ''.
    """
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"cannot read allowlist {path}: {exc}") from exc
    patterns: list[str] = []
    reasons: dict[str, str] = {}
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        # strip a trailing same-line comment for the reason, keep the glob
        code = stripped.split("#", 1)[0].strip()
        comment = stripped.split("#", 1)[1].strip() if "#" in stripped else ""
        if not code:
            continue
        patterns.append(code)
        reasons[code] = comment
    return patterns, reasons


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Flag hardcoded /bin/bash shebangs.")
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Specific files or directories. Default: staged changes.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Scan the full repository.",
    )
    parser.add_argument(
        "--diff",
        metavar="REF",
        help="Scan files changed from REF, including unstaged changes.",
    )
    parser.add_argument(
        "--allowlist",
        metavar="FILE",
        help=(
            "Path-based platform exceptions (repo-relative globs, one per "
            "line). Prefer this over inline markers for whole-file waivers."
        ),
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Treat an empty scan scope (e.g. no staged files) as OK.",
    )
    return parser.parse_args(argv)


def report(
    display_path: str,
    lineno: int,
    line: str,
    *,
    embedded: bool,
) -> None:
    kind = (
        "embedded /bin/bash shebang string"
        if embedded
        else "hardcoded /bin/bash shebang"
    )
    print(f"{display_path}:{lineno}: [{kind}]")
    print(f"    {line.strip()}")
    print("    - /bin/bash does not exist on every system. Use #!/usr/bin/env bash.")
    if embedded:
        print("    Fix: #!/usr/bin/env bash inside the generated string.")
    else:
        print("    Fix: #!/usr/bin/env bash")
    print(
        "    If intentional, add `# shebang: ok <reason>` on the line"
        " immediately ABOVE,"
    )
    print("    or add the file to the path-based allowlist (--allowlist).")
    print()


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    allowlist_patterns: list[str] = []
    try:
        if args.allowlist:
            allowlist_patterns, _ = load_allowlist(args.allowlist)
    except ValueError as exc:
        print(f"X {exc}", file=sys.stderr)
        return 2

    def allowed(rel_posix: str) -> bool:
        for pattern in allowlist_patterns:
            if fnmatch.fnmatch(rel_posix, pattern):
                return True
            # A pattern with no slash also matches by basename, so
            # `termux-shim.sh` waives that file at any repo depth.
            if "/" not in pattern and fnmatch.fnmatch(Path(rel_posix).name, pattern):
                return True
        return False

    try:
        if args.all:
            roots = [REPO_ROOT]
        elif args.diff:
            roots = get_diff_files(args.diff)
        elif args.paths:
            roots = [path.resolve() for path in args.paths]
        else:
            roots = get_staged_files()
            if not roots:
                if args.allow_empty:
                    print("OK no staged files to scan (--allow-empty)")
                    return 0
                print(
                    "No staged files to scan. Pass --all, --diff REF, or paths.",
                    file=sys.stderr,
                )
                return 2
    except GitLookupError as exc:
        # Fail closed: an unverifiable scope must not look like a pass.
        print(
            f"X git lookup failed, refusing to guess the scan scope: {exc}",
            file=sys.stderr,
        )
        return 2

    total_matches = 0
    files_scanned = 0
    for path in iter_files(roots):
        try:
            rel_posix = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            rel_posix = str(path)
        if allowed(rel_posix):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines(
                keepends=True
            )
        except OSError:
            continue
        files_scanned += 1
        for lineno, line in enumerate(lines, start=1):
            is_first_line = lineno == 1
            is_shebang = is_first_line and SHEBANG_LINE_RE.match(line) is not None
            suppressed = (
                lineno > 1 and SUPPRESS_MARKER.search(lines[lineno - 2]) is not None
            )
            if suppressed:
                continue
            if is_shebang:
                embedded = False
            else:
                if EMBEDDED_SHEBANG_RE.search(line) is None:
                    continue
                embedded = True
            display_path = rel_posix
            report(display_path, lineno, line, embedded=embedded)
            total_matches += 1

    if total_matches:
        print(
            f"\nX {total_matches} hardcoded /bin/bash shebang(s) found across "
            f"{files_scanned} file(s) scanned.",
            file=sys.stderr,
        )
        return 1

    print(f"OK no hardcoded /bin/bash shebangs ({files_scanned} file(s) scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
