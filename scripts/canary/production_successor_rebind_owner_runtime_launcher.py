#!/usr/bin/env python3
"""Stdlib-only verifier/launcher for the sealed successor controller closure."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Mapping, Never, Sequence


LIBRARY_BASE = Path("/usr/lib/muncho-release-updater-releases")
ENTRY_RELATIVE = Path(
    "scripts/canary/production_successor_rebind_owner_runtime_launcher.py"
)
MANIFEST_NAME = "controller-manifest.json"
MANIFEST_SCHEMA = "muncho-successor-runtime-controller-manifest.v1"
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ACTIONS = frozenset({
    "prepare-runtime",
    "build-runtime-as-dedicated-builder",
    "promote-runtime",
})
_DIRECTORIES = ("gateway", "scripts", "scripts/canary")
_MAX_FILE_BYTES = 64 * 1024 * 1024


class SuccessorRuntimeFoundationLauncherError(RuntimeError):
    """Stable closed-launcher failure."""


def _fail() -> Never:
    raise SuccessorRuntimeFoundationLauncherError(
        "successor_runtime_foundation_launcher_invalid"
    )


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii", errors="strict")
    except (TypeError, ValueError, UnicodeError):
        _fail()


def _regular_bytes(path: Path, *, mode: int) -> bytes:
    descriptor: int | None = None
    try:
        before = os.lstat(path)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_uid != 0
            or before.st_gid != 0
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != mode
            or not 0 <= before.st_size <= _MAX_FILE_BYTES
        ):
            _fail()
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    except SuccessorRuntimeFoundationLauncherError:
        raise
    except OSError:
        _fail()
    finally:
        if descriptor is not None:
            os.close(descriptor)
    identity = lambda item: (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_uid,
        item.st_gid,
        item.st_nlink,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )
    if (
        identity(before) != identity(opened)
        or identity(before) != identity(after)
        or remaining
    ):
        _fail()
    return b"".join(chunks)


def _directory(path: Path, *, mode: int) -> None:
    try:
        item = os.lstat(path)
    except OSError:
        _fail()
    if (
        not stat.S_ISDIR(item.st_mode)
        or stat.S_ISLNK(item.st_mode)
        or item.st_uid != 0
        or item.st_gid != 0
        or stat.S_IMODE(item.st_mode) != mode
    ):
        _fail()


def _validate_controller(revision: str, expected_file_sha256: str) -> Path:
    try:
        current = Path(__file__).resolve(strict=True)
        controller = current.parents[2]
        library = controller.parent
    except (OSError, IndexError):
        _fail()
    expected = LIBRARY_BASE / revision / "controller" / ENTRY_RELATIVE
    if current != expected:
        _fail()
    _directory(LIBRARY_BASE, mode=0o755)
    _directory(library, mode=0o555)
    _directory(controller, mode=0o555)
    for relative in _DIRECTORIES:
        _directory(controller / relative, mode=0o555)

    manifest_raw = _regular_bytes(controller / MANIFEST_NAME, mode=0o444)
    if hashlib.sha256(manifest_raw).hexdigest() != expected_file_sha256:
        _fail()
    try:
        manifest = json.loads(manifest_raw.decode("ascii", errors="strict"))
    except (UnicodeError, json.JSONDecodeError):
        _fail()
    fields = {
        "schema",
        "release_revision",
        "directories",
        "files",
        "secret_material_recorded",
        "secret_digest_recorded",
        "manifest_sha256",
    }
    unsigned = (
        {name: value for name, value in manifest.items() if name != "manifest_sha256"}
        if isinstance(manifest, Mapping)
        else {}
    )
    if (
        not isinstance(manifest, Mapping)
        or set(manifest) != fields
        or manifest.get("schema") != MANIFEST_SCHEMA
        or manifest.get("release_revision") != revision
        or manifest.get("directories") != list(_DIRECTORIES)
        or manifest.get("secret_material_recorded") is not False
        or manifest.get("secret_digest_recorded") is not False
        or _SHA256.fullmatch(str(manifest.get("manifest_sha256", ""))) is None
        or manifest.get("manifest_sha256")
        != hashlib.sha256(_canonical(unsigned)).hexdigest()
        or manifest_raw != _canonical(manifest) + b"\n"
        or not isinstance(manifest.get("files"), list)
    ):
        _fail()

    observed_directories: set[str] = set()
    observed_files: set[str] = set()
    for root, directories, files in os.walk(
        controller, topdown=True, followlinks=False
    ):
        root_path = Path(root)
        for name in directories:
            path = root_path / name
            relative = path.relative_to(controller).as_posix()
            _directory(path, mode=0o555)
            observed_directories.add(relative)
        for name in files:
            relative = (root_path / name).relative_to(controller).as_posix()
            if relative != MANIFEST_NAME:
                observed_files.add(relative)
    records: dict[str, Mapping[str, Any]] = {}
    for value in manifest["files"]:
        if (
            not isinstance(value, Mapping)
            or set(value) != {"path", "mode", "size", "sha256"}
            or not isinstance(value.get("path"), str)
            or not isinstance(value.get("size"), int)
            or value["size"] < 0
            or value.get("mode") != "0444"
            or _SHA256.fullmatch(str(value.get("sha256", ""))) is None
            or value["path"] in records
            or value["path"].startswith("/")
            or ".." in Path(value["path"]).parts
        ):
            _fail()
        records[value["path"]] = value
    if observed_directories != set(_DIRECTORIES) or observed_files != set(records):
        _fail()
    for relative, record in records.items():
        raw = _regular_bytes(controller / relative, mode=0o444)
        if (
            len(raw) != record["size"]
            or hashlib.sha256(raw).hexdigest() != record["sha256"]
        ):
            _fail()
    return controller


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if (
        len(arguments) < 3
        or arguments[0] not in _ACTIONS
        or _REVISION.fullmatch(arguments[1] or "") is None
        or _SHA256.fullmatch(arguments[2] or "") is None
    ):
        _fail()
    action, revision, controller_manifest_file_sha256, *rest = arguments
    expected_tail = 2 if action == "build-runtime-as-dedicated-builder" else 0
    if len(rest) != expected_tail:
        _fail()
    controller = _validate_controller(revision, controller_manifest_file_sha256)
    # No package initializer or target module is imported before the complete
    # sealed controller closure and its externally supplied digest are proven.
    sys.path.insert(0, str(controller))
    from scripts.canary import (
        production_successor_rebind_owner_runtime as owner_runtime,
    )

    return owner_runtime.production_main((action, revision, *rest))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SuccessorRuntimeFoundationLauncherError:
        print(
            '{"error_code":"successor_runtime_foundation_launcher_failed","ok":false}',
            file=sys.stderr,
        )
        raise SystemExit(2) from None
