#!/usr/bin/env python3
"""Fail-closed repository hygiene gate for one immutable Git tree.

The authoritative scan uses ``GITHUB_SHA`` in CI and ``HEAD`` locally. It
intentionally ignores the mutable index and worktree; callers that need a local
dirty-tree check must run a separate, non-authoritative check.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Mapping

_FORBIDDEN = (b"openclaw", b"paperclip")
_EXEMPT_PATHS = {
    b"scripts/check_retired_product_names.py",
    b"tests/repository/test_retired_product_names_gate.py",
}
_OBJECT_ID = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_CI_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def find_violations(entries: Iterable[tuple[bytes, bytes]]) -> list[tuple[str, str, str]]:
    """Return ``(path, location, name)`` tuples for path/content matches."""
    violations: list[tuple[str, str, str]] = []
    for raw_path, content in entries:
        if raw_path in _EXEMPT_PATHS:
            continue
        path_lower = raw_path.lower()
        content_lower = content.lower()
        display = raw_path.decode("utf-8", "backslashreplace")
        for name in _FORBIDDEN:
            label = name.decode("ascii")
            if name in path_lower:
                violations.append((display, "path", label))
            if name in content_lower:
                violations.append((display, "content", label))
    return violations


def _run_git(
    root: Path,
    *args: str,
    check: bool = True,
    input_data: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run read-only Git plumbing without replacement objects or optional locks."""
    env = os.environ.copy()
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            env=env,
            input=input_data,
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"Git command failed ({' '.join(args)}): {exc}") from exc


def _resolve_tree_oid(root: Path, revision: str) -> bytes:
    """Resolve a revision once and return its immutable tree object ID."""
    if not revision or "\x00" in revision:
        raise RuntimeError("cannot resolve an empty or NUL-containing Git revision")
    result = _run_git(
        root,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{revision}^{{tree}}",
    )
    output = result.stdout
    if not output.endswith(b"\n") or output.count(b"\n") != 1:
        raise RuntimeError("cannot resolve Git revision to one tree object ID")
    tree_oid = output[:-1]
    if _OBJECT_ID.fullmatch(tree_oid) is None:
        raise RuntimeError("Git returned an invalid tree object ID")
    return tree_oid


def _tree_records(root: Path, tree_oid: bytes) -> list[tuple[bytes, bytes, bytes]]:
    """Return exact ``(mode, oid, path)`` records from one pinned tree."""
    try:
        tree_arg = tree_oid.decode("ascii")
    except UnicodeDecodeError as exc:
        raise RuntimeError("tree object ID is not ASCII") from exc
    if _OBJECT_ID.fullmatch(tree_oid) is None:
        raise RuntimeError("invalid pinned tree object ID")

    result = _run_git(root, "ls-tree", "-rz", "--full-tree", tree_arg)
    if result.stdout and not result.stdout.endswith(b"\0"):
        raise RuntimeError("malformed git ls-tree output")
    raw_records = result.stdout[:-1].split(b"\0") if result.stdout else []
    records: list[tuple[bytes, bytes, bytes]] = []
    for record in raw_records:
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_type, oid = metadata.split(b" ")
        except ValueError as exc:
            raise RuntimeError("malformed git ls-tree output") from exc
        if not raw_path or _OBJECT_ID.fullmatch(oid) is None:
            raise RuntimeError("malformed git ls-tree output")
        if mode == b"160000" or object_type == b"commit":
            display = raw_path.decode("utf-8", "backslashreplace")
            raise RuntimeError(f"gitlink tree entry is unsupported: {display}")
        if object_type != b"blob" or mode not in {b"100644", b"100755", b"120000"}:
            display = raw_path.decode("utf-8", "backslashreplace")
            raise RuntimeError(
                f"unsupported tree entry {mode.decode('ascii', 'replace')} "
                f"{object_type.decode('ascii', 'replace')}: {display}"
            )
        records.append((mode, oid, raw_path))
    return records


def _cat_blobs(
    root: Path, records: list[tuple[bytes, bytes, bytes]]
) -> list[bytes]:
    """Read the exact blob IDs from the pinned tree in one batch."""
    if not records:
        return []
    request = b"".join(oid + b"\n" for _, oid, _ in records)
    batch = _run_git(root, "cat-file", "--batch", input_data=request)

    output = memoryview(batch.stdout)
    cursor = 0
    blobs: list[bytes] = []
    for _, expected_oid, raw_path in records:
        newline = batch.stdout.find(b"\n", cursor)
        if newline < 0:
            raise RuntimeError("truncated git cat-file response")
        header = bytes(output[cursor:newline]).split(b" ")
        if len(header) != 3 or header[1] != b"blob":
            display = raw_path.decode("utf-8", "backslashreplace")
            raise RuntimeError(f"tree object is not a readable blob: {display}")
        actual_oid, _, raw_size = header
        if actual_oid != expected_oid:
            raise RuntimeError("git cat-file returned an unexpected object")
        try:
            size = int(raw_size)
        except ValueError as exc:
            raise RuntimeError("invalid git cat-file blob size") from exc
        start = newline + 1
        end = start + size
        if end >= len(output) or output[end] != 0x0A:
            raise RuntimeError("truncated git cat-file blob")
        blobs.append(bytes(output[start:end]))
        cursor = end + 1
    if cursor != len(output):
        raise RuntimeError("unexpected trailing git cat-file output")
    return blobs


def tracked_entries(root: Path, revision: str = "HEAD") -> list[tuple[bytes, bytes]]:
    """Read one immutable commit/tree snapshot without consulting the index."""
    tree_oid = _resolve_tree_oid(root, revision)
    records = _tree_records(root, tree_oid)
    blobs = _cat_blobs(root, records)
    return [
        (raw_path, content)
        for (_, _, raw_path), content in zip(records, blobs, strict=True)
    ]


def _scan_revision(environment: Mapping[str, str]) -> str:
    """Return the local fallback or a strict full CI commit object ID."""
    if "GITHUB_SHA" not in environment:
        return "HEAD"
    revision = environment["GITHUB_SHA"]
    if _CI_OBJECT_ID.fullmatch(revision) is None:
        raise RuntimeError("GITHUB_SHA must be a full lowercase hexadecimal object ID")
    return revision


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    try:
        revision = _scan_revision(os.environ)
        violations = find_violations(tracked_entries(root, revision))
    except RuntimeError as exc:
        print(f"source hygiene gate failed closed: {exc}", file=sys.stderr)
        return 2
    if violations:
        for path, location, name in violations:
            print(f"forbidden retired name {name!r} found in tracked {location}: {path}")
        return 1
    print(
        "source hygiene gate: no forbidden retired product names in pinned commit tree"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
