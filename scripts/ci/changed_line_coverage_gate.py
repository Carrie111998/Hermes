#!/usr/bin/env python3
"""Fail when changed Python lines are not covered.

This is intentionally small and dependency-free. `coverage json` produces the
input file; this script maps added lines from `git diff --unified=0` onto that
coverage payload and enforces a changed-line percentage.

Default policy:
- only changed Python source files count;
- tests, virtualenvs and cache directories are ignored;
- excluded lines in coverage.py are neutral;
- if no in-scope Python lines changed, the gate passes.

Usage:
    coverage json -o coverage.json
    python scripts/ci/changed_line_coverage_gate.py --coverage-json coverage.json --base-ref origin/main --fail-under 100
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

DEFAULT_INCLUDE = ("*.py",)
DEFAULT_EXCLUDE = (
    "tests/**",
    "**/tests/**",
    ".venv/**",
    "venv/**",
    "**/__pycache__/**",
    "build/**",
    "dist/**",
)


@dataclass(frozen=True)
class ChangedLineResult:
    total: int
    covered: int
    missing: dict[str, list[int]]

    @property
    def percent(self) -> float:
        if self.total == 0:
            return 100.0
        return self.covered / self.total * 100.0


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def in_scope(path: str, include: Sequence[str] = DEFAULT_INCLUDE, exclude: Sequence[str] = DEFAULT_EXCLUDE) -> bool:
    normalized = path.replace("\\", "/")
    return _matches_any(normalized, include) and not _matches_any(normalized, exclude)


def parse_changed_lines(diff_text: str, include: Sequence[str] = DEFAULT_INCLUDE, exclude: Sequence[str] = DEFAULT_EXCLUDE) -> dict[str, set[int]]:
    """Return {new_path: {added_line_numbers}} from a unified diff."""
    changed: dict[str, set[int]] = {}
    current_file: str | None = None
    new_line: int | None = None
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            current_file = None
            new_line = None
            continue
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            if path == "/dev/null":
                current_file = None
                continue
            if path.startswith("b/"):
                path = path[2:]
            current_file = path if in_scope(path, include, exclude) else None
            if current_file is not None:
                changed.setdefault(current_file, set())
            continue
        match = hunk_re.match(raw)
        if match:
            new_line = int(match.group(1))
            continue
        if current_file is None or new_line is None:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            changed[current_file].add(new_line)
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            continue
        else:
            new_line += 1

    return {path: lines for path, lines in changed.items() if lines}


def load_coverage_lines(path: Path) -> dict[str, tuple[set[int], set[int], set[int]]]:
    """Return {filename: (executed_lines, missing_lines, excluded_lines)} from coverage json."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, tuple[set[int], set[int], set[int]]] = {}
    for filename, data in payload.get("files", {}).items():
        normalized = filename.replace("\\", "/")
        executed = set(data.get("executed_lines", []))
        missing = set(data.get("missing_lines", []))
        excluded = set(data.get("excluded_lines", []))
        out[normalized] = (executed, missing, excluded)
    return out


def _lookup_coverage(path: str, coverage: dict[str, tuple[set[int], set[int], set[int]]]) -> tuple[set[int], set[int], set[int]] | None:
    normalized = path.replace("\\", "/")
    if normalized in coverage:
        return coverage[normalized]
    suffix = "/" + normalized
    for filename, lines in coverage.items():
        if filename.endswith(suffix):
            return lines
    return None


def evaluate_changed_line_coverage(changed: dict[str, set[int]], coverage: dict[str, tuple[set[int], set[int], set[int]]]) -> ChangedLineResult:
    total = 0
    covered = 0
    missing: dict[str, list[int]] = {}
    for path, lines in sorted(changed.items()):
        coverage_lines = _lookup_coverage(path, coverage)
        if coverage_lines is None:
            total += len(lines)
            missing.setdefault(path, []).extend(sorted(lines))
            continue
        executed, missing_lines, excluded = coverage_lines
        executable = executed | missing_lines
        for line in sorted(lines):
            if line in excluded:
                continue
            if line not in executable:
                if not executable:
                    total += 1
                    missing.setdefault(path, []).append(line)
                continue
            total += 1
            if line in executed:
                covered += 1
            else:
                missing.setdefault(path, []).append(line)
    return ChangedLineResult(total=total, covered=covered, missing=missing)


def run_git_diff(repo: Path, base_ref: str) -> str:
    proc = subprocess.run(
        ["git", "diff", "--unified=0", "--diff-filter=ACMRT", f"{base_ref}...HEAD", "--", "*.py"],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git diff failed with exit {proc.returncode}")
    return proc.stdout


def render_result(result: ChangedLineResult) -> str:
    lines = [
        "# Changed-line coverage gate",
        "",
        f"Changed executable lines: {result.total}",
        f"Covered changed lines: {result.covered}",
        f"Changed-line coverage: {result.percent:.2f}%",
    ]
    if result.missing:
        lines.append("")
        lines.append("## Missing changed lines")
        for path, missing_lines in sorted(result.missing.items()):
            joined = ",".join(str(line) for line in missing_lines)
            lines.append(f"- `{path}`: {joined}")
    return "\n".join(lines) + "\n"


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gate changed Python lines against coverage json.")
    parser.add_argument("--coverage-json", type=Path, default=Path("coverage.json"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base-ref", default="origin/main")
    parser.add_argument("--fail-under", type=float, default=100.0)
    parser.add_argument("--diff-file", type=Path, help="Read a precomputed unified diff instead of invoking git.")
    parser.add_argument("--include", action="append", default=[], help="fnmatch include pattern; may repeat. Defaults to *.py")
    parser.add_argument("--exclude", action="append", default=[], help="fnmatch exclude pattern; may repeat in addition to defaults.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    include = tuple(args.include) if args.include else DEFAULT_INCLUDE
    exclude = (*DEFAULT_EXCLUDE, *tuple(args.exclude))
    diff_text = args.diff_file.read_text(encoding="utf-8") if args.diff_file else run_git_diff(args.repo, args.base_ref)
    changed = parse_changed_lines(diff_text, include=include, exclude=exclude)
    coverage = load_coverage_lines(args.coverage_json)
    result = evaluate_changed_line_coverage(changed, coverage)
    print(render_result(result), end="")
    if result.percent + 1e-9 < args.fail_under:
        print(f"error: changed-line coverage {result.percent:.2f}% is below required {args.fail_under:.2f}%", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
