#!/usr/bin/env python3
"""Stdlib-only pre-exec proof for the fixed successor-rebind owner runtime."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Mapping, Never, Sequence


MANIFEST_SCHEMA = "muncho-production-owner-runtime-manifest.v1"
ATTESTATION_SCHEMA = "muncho-production-owner-runtime-attestation.v1"
MANIFEST_NAME = "production-owner-runtime-manifest.json"
RUNTIME_BASE = Path("/usr/lib/muncho-successor-rebind-runtime")
PYTHON_VERSION = "3.11.15"
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_MANIFEST = 32 * 1024 * 1024
_MAX_ENTRIES = 200_000
_MAX_BYTES = 4 * 1024 * 1024 * 1024
_MANIFEST_FIELDS = frozenset({
    "schema",
    "revision",
    "artifact_root",
    "python_version",
    "interpreter",
    "pyvenv_cfg",
    "site_packages",
    "sys_path",
    "required_modules",
    "entries",
    "entry_count",
    "tree_bytes",
    "tree_sha256",
    "root_uid",
    "root_gid",
    "root_mode",
    "secret_material_recorded",
    "secret_digest_recorded",
    "manifest_sha256",
})


class SuccessorRuntimePreExecError(RuntimeError):
    """Stable pre-exec proof failure."""


def _fail(code: str, _cause: BaseException | None = None) -> Never:
    raise SuccessorRuntimePreExecError(code) from None


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        _fail("successor_runtime_preexec_json_invalid", exc)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _identity(item: os.stat_result) -> tuple[int, ...]:
    return (
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


def _hash_regular(
    path: Path,
    *,
    maximum: int = _MAX_BYTES,
    uid: int = 0,
    gid: int = 0,
) -> tuple[str, os.stat_result]:
    descriptor: int | None = None
    try:
        before = os.lstat(path)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_uid != uid
            or before.st_gid != gid
            or before.st_nlink != 1
            or before.st_size < 0
            or before.st_size > maximum
            or stat.S_IMODE(before.st_mode) & 0o222
        ):
            _fail("successor_runtime_preexec_tree_invalid")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        digest = hashlib.sha256()
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    except SuccessorRuntimePreExecError:
        raise
    except OSError as exc:
        _fail("successor_runtime_preexec_tree_invalid", exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (
        _identity(before) != _identity(opened)
        or _identity(before) != _identity(after)
        or remaining != 0
    ):
        _fail("successor_runtime_preexec_tree_changed")
    return digest.hexdigest(), before


def _directory(
    path: Path,
    *,
    mode: int | None = None,
    uid: int = 0,
    gid: int = 0,
    writable_allowed: bool = False,
) -> os.stat_result:
    try:
        item = os.lstat(path)
    except OSError as exc:
        _fail("successor_runtime_preexec_tree_invalid", exc)
    if (
        not stat.S_ISDIR(item.st_mode)
        or stat.S_ISLNK(item.st_mode)
        or item.st_uid != uid
        or item.st_gid != gid
        or (not writable_allowed and stat.S_IMODE(item.st_mode) & 0o222)
        or (mode is not None and stat.S_IMODE(item.st_mode) != mode)
    ):
        _fail("successor_runtime_preexec_tree_invalid")
    return item


def _within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def _entry(
    path: Path,
    root: Path,
    *,
    uid: int,
    gid: int,
) -> tuple[dict[str, Any], int]:
    try:
        item = os.lstat(path)
    except OSError as exc:
        _fail("successor_runtime_preexec_tree_invalid", exc)
    relative = path.relative_to(root).as_posix()
    common = {
        "path": relative,
        "mode": f"{stat.S_IMODE(item.st_mode):04o}",
        "uid": item.st_uid,
        "gid": item.st_gid,
    }
    if item.st_uid != uid or item.st_gid != gid:
        _fail("successor_runtime_preexec_tree_invalid")
    if stat.S_ISDIR(item.st_mode):
        if stat.S_IMODE(item.st_mode) & 0o222:
            _fail("successor_runtime_preexec_tree_invalid")
        return {**common, "kind": "directory"}, 0
    if stat.S_ISREG(item.st_mode):
        digest, checked = _hash_regular(path, uid=uid, gid=gid)
        return {
            **common,
            "kind": "file",
            "size": checked.st_size,
            "sha256": digest,
        }, checked.st_size
    if stat.S_ISLNK(item.st_mode):
        try:
            target = os.readlink(path)
            resolved = path.resolve(strict=True)
        except OSError as exc:
            _fail("successor_runtime_preexec_tree_invalid", exc)
        if not target or not _within(resolved, root):
            _fail("successor_runtime_preexec_tree_invalid")
        return {**common, "kind": "symlink", "target": target}, 0
    _fail("successor_runtime_preexec_tree_invalid")


def _collect(
    root: Path,
    *,
    uid: int,
    gid: int,
) -> tuple[list[dict[str, Any]], int]:
    _directory(root, mode=0o555, uid=uid, gid=gid)
    entries: list[dict[str, Any]] = []
    total = 0
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        directories.sort()
        files.sort()
        for name in (*directories, *files):
            path = Path(current) / name
            if path == root / MANIFEST_NAME:
                continue
            value, size = _entry(path, root, uid=uid, gid=gid)
            entries.append(value)
            total += size
            if len(entries) > _MAX_ENTRIES or total > _MAX_BYTES:
                _fail("successor_runtime_preexec_tree_oversized")
    entries.sort(key=lambda item: item["path"])
    if len({item["path"] for item in entries}) != len(entries):
        _fail("successor_runtime_preexec_tree_invalid")
    return entries, total


def _manifest(root: Path, *, uid: int, gid: int) -> Mapping[str, Any]:
    path = root / MANIFEST_NAME
    digest, item = _hash_regular(
        path,
        maximum=_MAX_MANIFEST,
        uid=uid,
        gid=gid,
    )
    if stat.S_IMODE(item.st_mode) != 0o444 or item.st_size == 0:
        _fail("successor_runtime_preexec_manifest_invalid")
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("ascii", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail("successor_runtime_preexec_manifest_invalid", exc)
    if not isinstance(value, Mapping) or raw != _canonical(value) + b"\n":
        _fail("successor_runtime_preexec_manifest_invalid")
    unsigned = {name: item for name, item in value.items() if name != "manifest_sha256"}
    if (
        set(value) != _MANIFEST_FIELDS
        or value.get("manifest_sha256") != _sha(_canonical(unsigned))
        or digest != _sha(raw)
    ):
        _fail("successor_runtime_preexec_manifest_invalid")
    return value


def verify(
    *,
    revision: str,
    expected_manifest_sha256: str,
    expected_tree_sha256: str,
    expected_interpreter_sha256: str,
    expected_attestation_sha256: str,
    runtime_base: Path = RUNTIME_BASE,
    uid: int = 0,
    gid: int = 0,
    runtime_base_mode: int = 0o755,
    physical_root: Path | None = None,
) -> Path:
    if _REVISION.fullmatch(revision or "") is None or any(
        _SHA256.fullmatch(value or "") is None
        for value in (
            expected_manifest_sha256,
            expected_tree_sha256,
            expected_interpreter_sha256,
            expected_attestation_sha256,
        )
    ):
        _fail("successor_runtime_preexec_identity_invalid")
    _directory(
        runtime_base,
        mode=runtime_base_mode,
        uid=uid,
        gid=gid,
        writable_allowed=True,
    )
    logical_root = runtime_base / revision
    root = physical_root or logical_root
    manifest = _manifest(root, uid=uid, gid=gid)
    entries, total = _collect(root, uid=uid, gid=gid)
    interpreter = root / "venv/bin/python"
    interpreter_sha256, interpreter_state = _hash_regular(
        interpreter,
        uid=uid,
        gid=gid,
    )
    pyvenv = root / "venv/pyvenv.cfg"
    pyvenv_sha256, pyvenv_state = _hash_regular(
        pyvenv,
        uid=uid,
        gid=gid,
    )
    site_packages = root / "venv/lib/python3.11/site-packages"
    logical_site_packages = logical_root / "venv/lib/python3.11/site-packages"
    _directory(site_packages, uid=uid, gid=gid)
    if list(site_packages.glob("*.pth")) or list(site_packages.glob("*.egg-link")):
        _fail("successor_runtime_preexec_dynamic_site_path_forbidden")
    for direct_url in site_packages.glob("*.dist-info/direct_url.json"):
        try:
            value = json.loads(direct_url.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            _fail("successor_runtime_preexec_direct_url_invalid", exc)
        directory = value.get("dir_info") if isinstance(value, Mapping) else None
        url = value.get("url") if isinstance(value, Mapping) else None
        if (isinstance(directory, Mapping) and directory.get("editable") is True) or (
            isinstance(url, str)
            and url.startswith("file://")
            and not _within(Path(url.removeprefix("file://")), logical_root)
        ):
            _fail("successor_runtime_preexec_direct_url_invalid")
    required = manifest.get("required_modules")
    sys_path = manifest.get("sys_path")
    if (
        manifest.get("schema") != MANIFEST_SCHEMA
        or manifest.get("revision") != revision
        or manifest.get("artifact_root") != str(logical_root)
        or manifest.get("python_version") != PYTHON_VERSION
        or manifest.get("site_packages") != str(logical_site_packages)
        or manifest.get("root_uid") != uid
        or manifest.get("root_gid") != gid
        or manifest.get("root_mode") != "0555"
        or manifest.get("entries") != entries
        or manifest.get("entry_count") != len(entries)
        or manifest.get("tree_bytes") != total
        or manifest.get("tree_sha256") != _sha(_canonical(entries))
        or manifest.get("tree_sha256") != expected_tree_sha256
        or manifest.get("manifest_sha256") != expected_manifest_sha256
        or not isinstance(required, Mapping)
        or not required
        or not isinstance(sys_path, list)
        or not sys_path
        or len(sys_path) != len(set(sys_path))
        or str(logical_site_packages) not in sys_path
        or manifest.get("secret_material_recorded") is not False
        or manifest.get("secret_digest_recorded") is not False
    ):
        _fail("successor_runtime_preexec_manifest_invalid")
    interpreter_record = manifest.get("interpreter")
    pyvenv_record = manifest.get("pyvenv_cfg")
    if (
        not isinstance(interpreter_record, Mapping)
        or interpreter_record.get("path") != str(logical_root / "venv/bin/python")
        or interpreter_record.get("realpath") != str(logical_root / "venv/bin/python")
        or interpreter_record.get("mode") != "0555"
        or stat.S_IMODE(interpreter_state.st_mode) != 0o555
        or interpreter_record.get("size") != interpreter_state.st_size
        or interpreter_record.get("sha256") != interpreter_sha256
        or interpreter_sha256 != expected_interpreter_sha256
        or not isinstance(pyvenv_record, Mapping)
        or pyvenv_record.get("path") != str(logical_root / "venv/pyvenv.cfg")
        or pyvenv_record.get("mode") != "0444"
        or stat.S_IMODE(pyvenv_state.st_mode) != 0o444
        or pyvenv_record.get("size") != pyvenv_state.st_size
        or pyvenv_record.get("sha256") != pyvenv_sha256
    ):
        _fail("successor_runtime_preexec_interpreter_invalid")
    entry_by_path = {item["path"]: item for item in entries}
    for record in required.values():
        if (
            not isinstance(record, Mapping)
            or set(record) != {"origin", "relative_path", "sha256"}
            or record.get("origin")
            != str(logical_root / str(record.get("relative_path")))
            or entry_by_path.get(record.get("relative_path"), {}).get("sha256")
            != record.get("sha256")
        ):
            _fail("successor_runtime_preexec_required_module_invalid")
    if any(
        not isinstance(item, str)
        or not os.path.isabs(item)
        or not _within(Path(item), logical_root)
        for item in sys_path
    ):
        _fail("successor_runtime_preexec_sys_path_invalid")
    attestation_unsigned = {
        "schema": ATTESTATION_SCHEMA,
        "revision": revision,
        "manifest_sha256": manifest["manifest_sha256"],
        "tree_sha256": manifest["tree_sha256"],
        "interpreter_sha256": interpreter_sha256,
        "pyvenv_cfg_sha256": pyvenv_sha256,
        "sys_path_sha256": _sha(_canonical(sys_path)),
        "required_modules_sha256": _sha(_canonical(required)),
        "module_origins_release_local": True,
        "ambient_python_environment_present": False,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    if _sha(_canonical(attestation_unsigned)) != expected_attestation_sha256:
        _fail("successor_runtime_preexec_attestation_invalid")
    return interpreter


def verify_staged(
    *,
    root: Path,
    revision: str,
    expected_manifest_sha256: str,
    expected_tree_sha256: str,
    expected_interpreter_sha256: str,
    expected_attestation_sha256: str,
    uid: int,
    gid: int,
) -> Mapping[str, Any]:
    """Verify builder-owned staging without executing its interpreter."""

    if root != root.parent / revision:
        _fail("successor_runtime_preexec_identity_invalid")
    verify(
        revision=revision,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_tree_sha256=expected_tree_sha256,
        expected_interpreter_sha256=expected_interpreter_sha256,
        expected_attestation_sha256=expected_attestation_sha256,
        runtime_base=root.parent,
        uid=uid,
        gid=gid,
        runtime_base_mode=0o700,
    )
    return dict(_manifest(root, uid=uid, gid=gid))


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 5:
        _fail("successor_runtime_preexec_argv_invalid")
    revision, manifest_sha, tree_sha, interpreter_sha, attestation_sha = arguments
    interpreter = verify(
        revision=revision,
        expected_manifest_sha256=manifest_sha,
        expected_tree_sha256=tree_sha,
        expected_interpreter_sha256=interpreter_sha,
        expected_attestation_sha256=attestation_sha,
    )
    environment = {
        "HOME": "/root",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    }
    os.execve(
        interpreter,
        (
            str(interpreter),
            "-I",
            "-B",
            "-m",
            "gateway.production_owner_runtime",
            "--revision",
            revision,
            "run",
            "--",
            "upstream-sync-successor-owner-apply",
            "--revision",
            revision,
        ),
        environment,
    )
    _fail("successor_runtime_preexec_exec_returned")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SuccessorRuntimePreExecError:
        print(
            '{"error_code":"successor_runtime_preexec_failed","ok":false}',
            file=sys.stderr,
        )
        raise SystemExit(2) from None


__all__ = [
    "RUNTIME_BASE",
    "SuccessorRuntimePreExecError",
    "main",
    "verify",
    "verify_staged",
]
