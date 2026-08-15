#!/usr/bin/env python3
"""Repository-wide 2K Law and KILL LOCK enforcement.

Usage:
  python scripts/check_2k_law.py --baseline .github/godfile-baseline.json
  python scripts/check_2k_law.py --write-baseline .github/godfile-baseline.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
EXCLUDES = {
    ".git", "node_modules", "vendor", "dist", "build", ".venv", "venv",
    "__pycache__", "tests", "test", "fixtures", "snapshots", "generated",
}
AUTHORITY = (
    re.compile(r"\b(?:sqlite3|BEGIN IMMEDIATE|SELECT |INSERT |UPDATE |DELETE )"),
    re.compile(r"\b(?:subprocess|Popen|docker|podman|terminal)\b"),
    re.compile(r"\b(?:token|secret|credential|authorization|api[_-]?key)\b", re.I),
    re.compile(r"\b(?:requests|httpx|urllib|aiohttp|socket|webhook)\b"),
    re.compile(r"\b(?:dispatch|handler|hook|plugin|mcp|approve|publish)\b", re.I),
)


def records(root: Path, ceiling: int) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUFFIXES:
            continue
        rel = path.relative_to(root)
        if any(part in EXCLUDES for part in rel.parts):
            continue
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        lines = len(text.splitlines())
        categories = sum(bool(pattern.search(text)) for pattern in AUTHORITY)
        authority_exception = lines >= 800 and categories >= 4
        values.append(
            {
                "path": rel.as_posix(),
                "lines": lines,
                "sha256": hashlib.sha256(data).hexdigest(),
                "unresolved_godfile": lines > ceiling,
                "authority_exception": authority_exception,
            }
        )
    return values


def touched(root: Path, base: str) -> set[str]:
    completed = subprocess.run(
        ["git", "-C", str(root), "diff", "--name-only", f"{base}...HEAD"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return {line for line in completed.stdout.splitlines() if line}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--baseline")
    group.add_argument("--write-baseline")
    parser.add_argument("--root", default=".")
    parser.add_argument("--ceiling", type=int, default=1999)
    parser.add_argument("--base", default="origin/main")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    current = records(root, args.ceiling)
    payload = {
        "schema": "hermes.godfile-inventory.v1",
        "ceiling": args.ceiling,
        "source": subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip(),
        "records": current,
    }
    if args.write_baseline:
        target = root / args.write_baseline
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(target)
        return 0

    baseline = json.loads((root / args.baseline).read_text())
    if int(baseline.get("ceiling", args.ceiling)) != args.ceiling:
        print("baseline ceiling differs from enforcement ceiling", file=sys.stderr)
        return 2
    old = {row["path"]: row for row in baseline["records"]}
    now = {row["path"]: row for row in current}
    failures: list[str] = []
    for path, row in now.items():
        prior = old.get(path)
        if row["unresolved_godfile"] and prior is None:
            failures.append(f"new godfile: {path} ({row['lines']} lines)")
        if prior and prior.get("unresolved_godfile") and row["lines"] > prior["lines"]:
            failures.append(f"KILL LOCK growth: {path}: {prior['lines']} -> {row['lines']}")
    try:
        changed = touched(root, args.base)
    except subprocess.CalledProcessError:
        changed = set()
    for path in changed:
        row = now.get(path)
        if row and row["lines"] > args.ceiling:
            failures.append(f"touched file remains over ceiling: {path} ({row['lines']})")
    if failures:
        print("\n".join(sorted(set(failures))), file=sys.stderr)
        return 1
    print(f"2K Law pass: {len(current)} production source files checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
