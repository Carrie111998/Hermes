"""Verify and reapply the Hermes semantic-route authority overlay.

This module is deliberately independent from Hermes runtime imports. The
installed checkout can be half-updated when this code runs, so the manager
uses only the Python standard library and Git's three-way apply operation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def _safe_relative(value: Any) -> Path | None:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        return None
    path = Path(value)
    if ".." in path.parts or path == Path("."):
        return None
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(manifest_path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"cannot read manifest: {exc}"
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        return None, "unsupported authority manifest"
    return raw, ""


def _validate_layout(
    manifest_path: Path, manifest: dict[str, Any], install_root: Path
) -> tuple[Path | None, list[tuple[Path, str]], str]:
    authority_root = manifest_path.parent.resolve()
    declared_authority = manifest.get("authority_root")
    if declared_authority and Path(str(declared_authority)).resolve() != authority_root:
        return None, [], "manifest authority_root does not match its directory"

    patch_rel = _safe_relative(manifest.get("patch_file"))
    if patch_rel is None:
        return None, [], "manifest patch_file is not a safe relative path"
    patch_path = (authority_root / patch_rel).resolve()
    if authority_root not in patch_path.parents or not patch_path.is_file():
        return None, [], "authority patch is missing or outside authority_root"

    entries = manifest.get("managed_files")
    if not isinstance(entries, list) or not entries:
        return None, [], "manifest managed_files is empty or invalid"
    managed: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            return None, [], "manifest managed_files contains a non-object"
        rel = _safe_relative(entry.get("path"))
        expected = entry.get("sha256")
        if rel is None or not isinstance(expected, str) or len(expected) != 64:
            return None, [], "manifest contains an invalid managed file entry"
        if rel in seen:
            return None, [], f"duplicate managed file: {rel}"
        seen.add(rel)
        managed.append((rel, expected))

    try:
        patch_text = patch_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, [], f"cannot read authority patch: {exc}"
    patch_paths: set[Path] = set()
    for line in patch_text.splitlines():
        if not line.startswith("diff --git a/"):
            continue
        fields = line.split()
        if len(fields) != 4 or not fields[2].startswith("a/") or not fields[3].startswith("b/"):
            return None, [], "authority patch contains an invalid diff header"
        left = _safe_relative(fields[2][2:])
        right = _safe_relative(fields[3][2:])
        if left is None or right is None or left != right:
            return None, [], "authority patch contains an unsafe rename or path"
        patch_paths.add(right)
    managed_paths = {rel for rel, _ in managed}
    if not patch_paths:
        return None, [], "authority patch contains no file entries"
    if patch_paths != managed_paths:
        return None, [], "authority patch paths do not match manifest managed_files"

    declared_install = manifest.get("install_root")
    if declared_install and Path(str(declared_install)).resolve() != install_root.resolve():
        return None, [], "manifest install_root does not match the update target"
    return patch_path, managed, ""


def verify_manifest(manifest_path: Path, install_root: Path) -> tuple[bool, str]:
    """Verify authority metadata and managed target checksums without mutation."""

    manifest, error = _load_manifest(manifest_path)
    if manifest is None:
        return False, error
    _, managed, error = _validate_layout(manifest_path, manifest, install_root)
    if error:
        return False, error
    for rel, expected in managed:
        target = install_root.resolve() / rel
        if not target.is_file():
            return False, f"managed file is missing: {rel}"
        if _sha256(target) != expected:
            return False, f"managed file checksum mismatch: {rel}"
    return True, "authority manifest and managed files verified"


def _is_git_root(install_root: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=install_root,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and Path(result.stdout.strip()).resolve() == install_root.resolve()


def apply_authority(manifest_path: Path, install_root: Path) -> int:
    """Apply the authority patch, or return success when already applied."""

    manifest, error = _load_manifest(manifest_path)
    if manifest is None:
        print(f"authority: {error}", file=sys.stderr)
        return 1
    patch_path, _, error = _validate_layout(manifest_path, manifest, install_root)
    if error or patch_path is None:
        print(f"authority: {error}", file=sys.stderr)
        return 1
    ok, detail = verify_manifest(manifest_path, install_root)
    if ok:
        print(f"authority: {detail}")
        return 0
    if not _is_git_root(install_root):
        print("authority: install target is not a Git worktree", file=sys.stderr)
        return 1

    result = subprocess.run(
        ["git", "apply", "--3way", "--whitespace=nowarn", str(patch_path)],
        cwd=install_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        diagnostic = (result.stderr or result.stdout).strip()
        print(f"authority: patch apply failed: {diagnostic}", file=sys.stderr)
        return 1
    ok, detail = verify_manifest(manifest_path, install_root)
    if not ok:
        print(f"authority: post-apply verification failed: {detail}", file=sys.stderr)
        return 1
    print("authority: patch applied and verified")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify", "apply"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--install-root", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "verify":
        ok, detail = verify_manifest(args.manifest, args.install_root)
        print(f"authority: {detail}", file=None if ok else sys.stderr)
        return 0 if ok else 1
    return apply_authority(args.manifest, args.install_root)


if __name__ == "__main__":
    raise SystemExit(main())
