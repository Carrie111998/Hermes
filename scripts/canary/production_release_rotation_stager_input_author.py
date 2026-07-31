#!/usr/bin/env python3
"""Author one immutable offline-builder input for the rotation stager.

This is the privileged, create-only edge of the otherwise unprivileged
release-builder boundary.  It reads one exact Git commit, an independently
verified complete Linux/x86_64 wheelhouse, and pinned ``uv``/system-Python
executables.  It publishes only the fixed builder input tree and one empty
builder-owned output directory.  It does not run the builder, promote a
candidate, change a release pointer, or touch a service, credential, or data
store.

Wheel resolution is intentionally outside this command.  The caller supplies
an already-verified, self-hashed wheelhouse manifest.  This command verifies
the exact closed inventory and every byte, but never reaches a package index.
"""

from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Never, Sequence

from scripts.canary import production_release_builder_phase as phase
from scripts.canary import production_release_builder_runtime as builder


WHEELHOUSE_SCHEMA = "muncho-production-unit-input-rotation-stager-wheelhouse.v1"
AUTHOR_RECEIPT_SCHEMA = (
    "muncho-production-unit-input-rotation-stager-input-authoring.v1"
)
TARGET = {
    "operating_system": "linux",
    "architecture": "x86_64",
    "python_implementation": "cpython",
    "python_abi": "cp311",
}
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_GIT_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_TOTAL_INPUT_BYTES = 8 * 1024 * 1024 * 1024

_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REMOTE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WHEEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,239}\.whl$")
_WHEELHOUSE_FIELDS = frozenset({
    "schema",
    "release_revision",
    "target",
    "complete_transitive_closure",
    "network_required",
    "source_build_allowed",
    "installation",
    "wheels",
    "verification_receipt_sha256",
    "secret_material_recorded",
    "secret_digest_recorded",
    "manifest_sha256",
})
_WHEEL_FIELDS = frozenset({"filename", "sha256", "size"})


class RotationStagerInputAuthorError(RuntimeError):
    """Stable, deliberately secret-free authoring failure."""


def _fail(code: str, cause: BaseException | None = None) -> Never:
    del cause
    raise RotationStagerInputAuthorError(code) from None


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        _fail("rotation_stager_input_json_invalid", exc)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class AuthorRoots:
    job_root: Path = phase.PRODUCTION_JOB_ROOT


def _git_environment() -> Mapping[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _git(
    source: Path,
    *arguments: str,
    maximum: int = MAX_GIT_OUTPUT_BYTES,
) -> bytes:
    try:
        completed = subprocess.run(
            ("/usr/bin/git", "-C", str(source), *arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=300,
            env=_git_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _fail("rotation_stager_input_git_invalid", exc)
    if completed.returncode != 0 or completed.stderr or len(completed.stdout) > maximum:
        _fail("rotation_stager_input_git_invalid")
    return completed.stdout


def _write_git_blobs(
    source: Path,
    object_ids: Sequence[str],
    destination: Path,
    *,
    object_format: str,
    initial_total: int,
) -> tuple[dict[str, Mapping[str, Any]], int]:
    """Stream one exact unique blob set through a single local Git process."""

    if (
        not object_ids
        or len(object_ids) > phase.MAX_SOURCE_BLOBS
        or tuple(object_ids) != tuple(sorted(set(object_ids)))
        or object_format not in {"sha1", "sha256"}
    ):
        _fail("rotation_stager_input_source_blob_invalid")
    oid_length = 40 if object_format == "sha1" else 64
    oid_pattern = re.compile(rf"^[0-9a-f]{{{oid_length}}}$")
    if any(oid_pattern.fullmatch(item) is None for item in object_ids):
        _fail("rotation_stager_input_source_blob_invalid")
    process: subprocess.Popen[bytes] | None = None
    records: dict[str, Mapping[str, Any]] = {}
    total = initial_total
    try:
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                ("/usr/bin/git", "-C", str(source), "cat-file", "--batch"),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=errors,
                env=_git_environment(),
            )
            if process.stdin is None or process.stdout is None:
                _fail("rotation_stager_input_git_invalid")
            for expected_oid in object_ids:
                process.stdin.write(expected_oid.encode("ascii") + b"\n")
                process.stdin.flush()
                header = process.stdout.readline(256)
                try:
                    decoded = header.decode("ascii", errors="strict").rstrip("\n")
                    observed_oid, object_type, size_text = decoded.split(" ")
                    size = int(size_text, 10)
                except (UnicodeError, ValueError, TypeError) as exc:
                    _fail("rotation_stager_input_source_blob_invalid", exc)
                if (
                    not header.endswith(b"\n")
                    or observed_oid != expected_oid
                    or object_type != "blob"
                    or size < 0
                    or size > builder.MAX_BLOB_BYTES
                ):
                    _fail("rotation_stager_input_source_blob_invalid")
                chunks: list[bytes] = []
                remaining = size
                while remaining:
                    chunk = process.stdout.read(min(1024 * 1024, remaining))
                    if not chunk:
                        _fail("rotation_stager_input_source_blob_invalid")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if process.stdout.read(1) != b"\n":
                    _fail("rotation_stager_input_source_blob_invalid")
                raw = b"".join(chunks)
                digest = hashlib.new(object_format)
                digest.update(f"blob {len(raw)}\0".encode("ascii"))
                digest.update(raw)
                if digest.hexdigest() != expected_oid:
                    _fail("rotation_stager_input_source_blob_invalid")
                name = f"{expected_oid}.blob"
                _write_exclusive(destination / name, raw, mode=0o444)
                records[expected_oid] = {
                    "object_id": expected_oid,
                    "filename": name,
                    "sha256": sha256_bytes(raw),
                    "size": len(raw),
                }
                total += len(raw)
                if total > MAX_TOTAL_INPUT_BYTES:
                    _fail("rotation_stager_input_oversized")
            process.stdin.close()
            if process.stdout.read(1):
                _fail("rotation_stager_input_source_blob_invalid")
            process.stdout.close()
            if process.wait(timeout=300) != 0:
                _fail("rotation_stager_input_git_invalid")
            errors.seek(0, os.SEEK_END)
            if errors.tell() != 0:
                _fail("rotation_stager_input_git_invalid")
    except RotationStagerInputAuthorError:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        _fail("rotation_stager_input_git_invalid", exc)
    return records, total


def _ascii_line(raw: bytes, code: str) -> str:
    try:
        value = raw.decode("ascii", errors="strict").strip()
    except UnicodeError as exc:
        _fail(code, exc)
    if not value or "\n" in value or "\r" in value:
        _fail(code)
    return value


def _read_canonical_manifest(path: Path) -> Mapping[str, Any]:
    try:
        state = os.lstat(path)
        raw = path.read_bytes()
    except OSError as exc:
        _fail("rotation_stager_input_wheelhouse_invalid", exc)
    if (
        not stat.S_ISREG(state.st_mode)
        or stat.S_ISLNK(state.st_mode)
        or state.st_nlink != 1
        or not 1 < len(raw) <= MAX_MANIFEST_BYTES
        or not raw.endswith(b"\n")
        or b"\n" in raw[:-1]
    ):
        _fail("rotation_stager_input_wheelhouse_invalid")
    try:
        value = json.loads(raw[:-1].decode("ascii", errors="strict"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        _fail("rotation_stager_input_wheelhouse_invalid", exc)
    if not isinstance(value, Mapping) or raw != canonical_bytes(value) + b"\n":
        _fail("rotation_stager_input_wheelhouse_invalid")
    return dict(value)


def _validate_production_manifest_file(path: Path) -> None:
    try:
        state = os.lstat(path)
    except OSError as exc:
        _fail("rotation_stager_input_wheelhouse_invalid", exc)
    if (
        not stat.S_ISREG(state.st_mode)
        or stat.S_ISLNK(state.st_mode)
        or state.st_uid != 0
        or state.st_gid != 0
        or state.st_nlink != 1
        or stat.S_IMODE(state.st_mode) not in {0o400, 0o440, 0o444}
    ):
        _fail("rotation_stager_input_wheelhouse_invalid")


def validate_wheelhouse_manifest(
    value: Any,
    *,
    release_revision: str,
) -> tuple[Mapping[str, Any], tuple[Mapping[str, Any], ...]]:
    if not isinstance(value, Mapping) or set(value) != _WHEELHOUSE_FIELDS:
        _fail("rotation_stager_input_wheelhouse_invalid")
    raw = dict(value)
    unsigned = {name: item for name, item in raw.items() if name != "manifest_sha256"}
    wheels = raw.get("wheels")
    if (
        raw.get("schema") != WHEELHOUSE_SCHEMA
        or raw.get("release_revision") != release_revision
        or raw.get("target") != TARGET
        or raw.get("complete_transitive_closure") is not True
        or raw.get("network_required") is not False
        or raw.get("source_build_allowed") is not False
        or raw.get("installation") != phase._INSTALLATION
        or not isinstance(wheels, list)
        or not 0 < len(wheels) <= phase.MAX_WHEELS
        or _SHA256.fullmatch(str(raw.get("verification_receipt_sha256", ""))) is None
        or raw.get("secret_material_recorded") is not False
        or raw.get("secret_digest_recorded") is not False
        or _SHA256.fullmatch(str(raw.get("manifest_sha256", ""))) is None
        or raw.get("manifest_sha256") != sha256_bytes(canonical_bytes(unsigned))
    ):
        _fail("rotation_stager_input_wheelhouse_invalid")
    checked: list[Mapping[str, Any]] = []
    for item in wheels:
        if not isinstance(item, Mapping) or set(item) != _WHEEL_FIELDS:
            _fail("rotation_stager_input_wheelhouse_invalid")
        filename = item.get("filename")
        if (
            not isinstance(filename, str)
            or _WHEEL.fullmatch(filename) is None
            or _SHA256.fullmatch(str(item.get("sha256", ""))) is None
            or type(item.get("size")) is not int
            or not 0 < item["size"] <= builder.MAX_WHEEL_BYTES
        ):
            _fail("rotation_stager_input_wheelhouse_invalid")
        checked.append(dict(item))
    if checked != sorted(checked, key=lambda item: str(item["filename"])) or len({
        str(item["filename"]) for item in checked
    }) != len(checked):
        _fail("rotation_stager_input_wheelhouse_invalid")
    return raw, tuple(checked)


def _root_regular(
    path: Path,
    *,
    executable: bool,
    expected_sha256: str,
) -> tuple[str, int]:
    try:
        state = os.lstat(path)
    except OSError as exc:
        _fail("rotation_stager_input_pinned_executable_invalid", exc)
    allowed = {0o500, 0o550, 0o555} if executable else {0o400, 0o440, 0o444}
    if (
        not path.is_absolute()
        or ".." in path.parts
        or not stat.S_ISREG(state.st_mode)
        or stat.S_ISLNK(state.st_mode)
        or state.st_uid != 0
        or state.st_gid != 0
        or state.st_nlink != 1
        or stat.S_IMODE(state.st_mode) not in allowed
        or state.st_size <= 0
        or state.st_size > phase.MAX_UV_BYTES
    ):
        _fail("rotation_stager_input_pinned_executable_invalid")
    digest = _sha256_file(path)
    if digest != expected_sha256:
        _fail("rotation_stager_input_pinned_executable_invalid")
    return digest, state.st_size


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb", buffering=0) as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        _fail("rotation_stager_input_file_unavailable", exc)
    return digest.hexdigest()


def _write_exclusive(path: Path, raw: bytes, *, mode: int) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        view = memoryview(raw)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                _fail("rotation_stager_input_publication_failed")
            view = view[count:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    except RotationStagerInputAuthorError:
        raise
    except OSError as exc:
        _fail("rotation_stager_input_publication_failed", exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _copy_exclusive(
    source: Path,
    target: Path,
    *,
    mode: int,
    expected_source_uid: int,
    expected_source_gid: int,
    allowed_source_modes: frozenset[int],
) -> tuple[str, int]:
    try:
        state = os.lstat(source)
    except OSError as exc:
        _fail("rotation_stager_input_source_file_invalid", exc)
    if (
        not stat.S_ISREG(state.st_mode)
        or stat.S_ISLNK(state.st_mode)
        or state.st_nlink != 1
        or state.st_uid != expected_source_uid
        or state.st_gid != expected_source_gid
        or stat.S_IMODE(state.st_mode) not in allowed_source_modes
    ):
        _fail("rotation_stager_input_source_file_invalid")
    raw = source.read_bytes()
    if len(raw) != state.st_size:
        _fail("rotation_stager_input_source_file_changed")
    _write_exclusive(target, raw, mode=mode)
    return sha256_bytes(raw), len(raw)


def _verify_wheelhouse_tree(
    root: Path,
    wheels: Sequence[Mapping[str, Any]],
    *,
    expected_uid: int,
    production: bool,
) -> None:
    try:
        state = os.lstat(root)
        names = tuple(sorted(os.listdir(root)))
    except OSError as exc:
        _fail("rotation_stager_input_wheelhouse_invalid", exc)
    expected_names = tuple(str(item["filename"]) for item in wheels)
    allowed_modes = {0o500, 0o550, 0o555, 0o700, 0o750, 0o755}
    if (
        not root.is_absolute()
        or ".." in root.parts
        or not stat.S_ISDIR(state.st_mode)
        or stat.S_ISLNK(state.st_mode)
        or state.st_uid != expected_uid
        or stat.S_IMODE(state.st_mode) not in allowed_modes
        or (production and stat.S_IMODE(state.st_mode) & 0o222)
        or names != expected_names
    ):
        _fail("rotation_stager_input_wheelhouse_invalid")
    for item in wheels:
        selected = root / str(item["filename"])
        try:
            file_state = os.lstat(selected)
        except OSError as exc:
            _fail("rotation_stager_input_wheelhouse_invalid", exc)
        if (
            not stat.S_ISREG(file_state.st_mode)
            or stat.S_ISLNK(file_state.st_mode)
            or file_state.st_uid != expected_uid
            or file_state.st_nlink != 1
            or stat.S_IMODE(file_state.st_mode) not in {0o400, 0o440, 0o444}
            or file_state.st_size != item["size"]
            or _sha256_file(selected) != item["sha256"]
        ):
            _fail("rotation_stager_input_wheelhouse_invalid")


def _builder_identity() -> tuple[int, int]:
    try:
        user = pwd.getpwnam(phase.BUILDER_USER)
        group = grp.getgrnam(phase.BUILDER_GROUP)
    except KeyError as exc:
        _fail("rotation_stager_input_builder_identity_invalid", exc)
    if (
        user.pw_uid != phase.BUILDER_UID
        or user.pw_gid != phase.BUILDER_GID
        or group.gr_gid != phase.BUILDER_GID
    ):
        _fail("rotation_stager_input_builder_identity_invalid")
    return user.pw_uid, group.gr_gid


def _ensure_job_parent(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
) -> None:
    try:
        path.mkdir(mode=0o755, parents=True, exist_ok=True)
        state = os.lstat(path)
    except OSError as exc:
        _fail("rotation_stager_input_job_parent_invalid", exc)
    if (
        not stat.S_ISDIR(state.st_mode)
        or stat.S_ISLNK(state.st_mode)
        or state.st_uid != expected_uid
        or state.st_gid != expected_gid
        or stat.S_IMODE(state.st_mode) not in {0o755, 0o750, 0o700}
        or stat.S_IMODE(state.st_mode) & 0o022
    ):
        _fail("rotation_stager_input_job_parent_invalid")


def _fsync_directory(path: Path) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_DIRECTORY", 0),
        )
        os.fsync(descriptor)
    except OSError as exc:
        _fail("rotation_stager_input_seal_failed", exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _assert_no_xattrs(path: Path) -> None:
    reader = getattr(os, "listxattr", None)
    if not callable(reader):
        if sys.platform.startswith("linux"):
            _fail("rotation_stager_input_xattr_inspection_failed")
        return
    try:
        names = reader(path, follow_symlinks=False)
    except (OSError, TypeError, ValueError) as exc:
        _fail("rotation_stager_input_xattr_inspection_failed", exc)
    if names:
        _fail("rotation_stager_input_xattrs_present")


def _verify_sealed_job(
    *,
    job_root: Path,
    input_root: Path,
    output_root: Path,
    authority_uid: int,
    authority_gid: int,
    builder_uid: int,
    builder_gid: int,
) -> None:
    try:
        if tuple(sorted(os.listdir(job_root))) != ("input", "output"):
            _fail("rotation_stager_input_seal_invalid")
        for path, uid, gid, mode in (
            (job_root, authority_uid, authority_gid, 0o555),
            (input_root, authority_uid, authority_gid, 0o555),
            (output_root, builder_uid, builder_gid, 0o700),
        ):
            state = os.lstat(path)
            if (
                not stat.S_ISDIR(state.st_mode)
                or stat.S_ISLNK(state.st_mode)
                or state.st_uid != uid
                or state.st_gid != gid
                or stat.S_IMODE(state.st_mode) != mode
            ):
                _fail("rotation_stager_input_seal_invalid")
            _assert_no_xattrs(path)
        if tuple(os.listdir(output_root)):
            _fail("rotation_stager_input_output_not_empty")
        expected_root_names = tuple(sorted(phase._INPUT_ROOT_NAMES))
        if tuple(sorted(os.listdir(input_root))) != expected_root_names:
            _fail("rotation_stager_input_seal_invalid")
        for current, directories, files in os.walk(
            input_root,
            topdown=True,
            followlinks=False,
        ):
            selected = Path(current)
            if selected != input_root:
                state = os.lstat(selected)
                if (
                    not stat.S_ISDIR(state.st_mode)
                    or stat.S_ISLNK(state.st_mode)
                    or state.st_uid != authority_uid
                    or state.st_gid != authority_gid
                    or stat.S_IMODE(state.st_mode) != 0o555
                ):
                    _fail("rotation_stager_input_seal_invalid")
                _assert_no_xattrs(selected)
            for name in (*directories, *files):
                child = selected / name
                state = os.lstat(child)
                if stat.S_ISDIR(state.st_mode):
                    continue
                expected_mode = 0o555 if child.name == phase.UV_NAME else 0o444
                if (
                    not stat.S_ISREG(state.st_mode)
                    or stat.S_ISLNK(state.st_mode)
                    or state.st_uid != authority_uid
                    or state.st_gid != authority_gid
                    or state.st_nlink != 1
                    or stat.S_IMODE(state.st_mode) != expected_mode
                ):
                    _fail("rotation_stager_input_seal_invalid")
                _assert_no_xattrs(child)
    except RotationStagerInputAuthorError:
        raise
    except OSError as exc:
        _fail("rotation_stager_input_seal_invalid", exc)


def _author_for_test(
    *,
    source_root: Path,
    source_remote: str,
    repository_url: str,
    release_revision: str,
    wheelhouse_root: Path,
    wheelhouse_manifest_path: Path,
    uv_path: Path,
    expected_uv_sha256: str,
    python_executable_path: Path,
    expected_python_sha256: str,
    roots: AuthorRoots,
    production: bool = True,
    authority_uid: int = 0,
    authority_gid: int = 0,
    builder_uid: int | None = None,
    builder_gid: int | None = None,
) -> Mapping[str, Any]:
    if (
        not isinstance(roots, AuthorRoots)
        or _REVISION.fullmatch(release_revision or "") is None
        or _REMOTE.fullmatch(source_remote or "") is None
        or not isinstance(repository_url, str)
        or not repository_url
        or "\x00" in repository_url
        or _SHA256.fullmatch(expected_uv_sha256 or "") is None
        or _SHA256.fullmatch(expected_python_sha256 or "") is None
        or any(
            not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts
            for path in (
                source_root,
                wheelhouse_root,
                wheelhouse_manifest_path,
                uv_path,
                python_executable_path,
                roots.job_root,
            )
        )
        or type(production) is not bool
        or (production and (os.geteuid() != 0 or not sys.platform.startswith("linux")))
    ):
        _fail("rotation_stager_input_contract_invalid")
    if production and roots.job_root != phase.PRODUCTION_JOB_ROOT:
        _fail("rotation_stager_input_contract_invalid")
    if production:
        builder_uid, builder_gid = _builder_identity()
    elif builder_uid is None or builder_gid is None:
        builder_uid = authority_uid
        builder_gid = authority_gid
    if any(
        type(value) is not int or value < 0
        for value in (authority_uid, authority_gid, builder_uid, builder_gid)
    ):
        _fail("rotation_stager_input_contract_invalid")

    top = _ascii_line(
        _git(source_root, "rev-parse", "--show-toplevel"),
        "rotation_stager_input_git_invalid",
    )
    try:
        if Path(top).resolve(strict=True) != source_root.resolve(strict=True):
            _fail("rotation_stager_input_git_invalid")
    except OSError as exc:
        _fail("rotation_stager_input_git_invalid", exc)
    urls = tuple(
        item
        for item in _git(source_root, "remote", "get-url", "--all", source_remote)
        .decode("utf-8", errors="strict")
        .splitlines()
        if item
    )
    if urls != (repository_url,):
        _fail("rotation_stager_input_repository_mismatch")
    commit = _ascii_line(
        _git(source_root, "rev-parse", "--verify", f"{release_revision}^{{commit}}"),
        "rotation_stager_input_revision_invalid",
    )
    if commit != release_revision:
        _fail("rotation_stager_input_revision_invalid")
    object_format = _ascii_line(
        _git(source_root, "rev-parse", "--show-object-format"),
        "rotation_stager_input_git_invalid",
    )
    if object_format not in {"sha1", "sha256"}:
        _fail("rotation_stager_input_git_invalid")
    source_tree_oid = _ascii_line(
        _git(source_root, "rev-parse", f"{release_revision}^{{tree}}"),
        "rotation_stager_input_revision_invalid",
    )
    listing = _git(
        source_root,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        source_tree_oid,
        maximum=builder.MAX_GIT_TREE_BYTES,
    )
    try:
        entries = builder.parse_git_tree(listing, object_format=object_format)
        reconstructed = builder._reconstruct_git_tree_oid(entries)
    except builder.ProductionReleaseBuilderError as exc:
        _fail("rotation_stager_input_source_tree_invalid", exc)
    if (
        not entries
        or reconstructed != source_tree_oid
        or not any(
            entry.path == phase.UNIT_INPUT_ROTATION_STAGER_ENTRYPOINT_RELATIVE_PATH
            for entry in entries
        )
    ):
        _fail("rotation_stager_input_source_tree_invalid")

    if production:
        _validate_production_manifest_file(wheelhouse_manifest_path)
    wheelhouse_manifest = _read_canonical_manifest(wheelhouse_manifest_path)
    _, wheels = validate_wheelhouse_manifest(
        wheelhouse_manifest,
        release_revision=release_revision,
    )
    expected_source_uid = 0 if production else authority_uid
    _verify_wheelhouse_tree(
        wheelhouse_root,
        wheels,
        expected_uid=expected_source_uid,
        production=production,
    )
    if production:
        uv_sha256, uv_size = _root_regular(
            uv_path,
            executable=True,
            expected_sha256=expected_uv_sha256,
        )
        python_sha256, python_size = _root_regular(
            python_executable_path,
            executable=True,
            expected_sha256=expected_python_sha256,
        )
    else:
        uv_sha256, uv_size = _sha256_file(uv_path), os.lstat(uv_path).st_size
        python_sha256, python_size = (
            _sha256_file(python_executable_path),
            os.lstat(python_executable_path).st_size,
        )
        if uv_sha256 != expected_uv_sha256 or python_sha256 != expected_python_sha256:
            _fail("rotation_stager_input_pinned_executable_invalid")

    _ensure_job_parent(
        roots.job_root,
        expected_uid=0 if production else authority_uid,
        expected_gid=0 if production else authority_gid,
    )
    job_root = roots.job_root / release_revision
    input_root = job_root / "input"
    output_root = job_root / "output"
    blob_root = input_root / phase.SOURCE_BLOB_DIRECTORY_NAME
    wheel_root = input_root / phase.RUNTIME_WHEEL_DIRECTORY_NAME
    try:
        os.mkdir(job_root, 0o700)
        os.mkdir(input_root, 0o700)
        os.mkdir(output_root, 0o700)
        os.chown(output_root, int(builder_uid), int(builder_gid))
        os.mkdir(blob_root, 0o700)
        os.mkdir(wheel_root, 0o700)
    except OSError as exc:
        _fail("rotation_stager_input_publication_conflict", exc)

    blob_records, total_bytes = _write_git_blobs(
        source_root,
        sorted({entry.object_id for entry in entries}),
        blob_root,
        object_format=object_format,
        initial_total=len(listing),
    )
    _write_exclusive(input_root / phase.TREE_LISTING_NAME, listing, mode=0o444)

    for item in wheels:
        digest, size = _copy_exclusive(
            wheelhouse_root / str(item["filename"]),
            wheel_root / str(item["filename"]),
            mode=0o444,
            expected_source_uid=expected_source_uid,
            expected_source_gid=0 if production else authority_gid,
            allowed_source_modes=frozenset({0o400, 0o440, 0o444}),
        )
        if digest != item["sha256"] or size != item["size"]:
            _fail("rotation_stager_input_wheelhouse_changed")
        total_bytes += size
        if total_bytes > MAX_TOTAL_INPUT_BYTES:
            _fail("rotation_stager_input_oversized")
    copied_uv_sha256, copied_uv_size = _copy_exclusive(
        uv_path,
        input_root / phase.UV_NAME,
        mode=0o555,
        expected_source_uid=expected_source_uid,
        expected_source_gid=0 if production else authority_gid,
        allowed_source_modes=frozenset({0o500, 0o550, 0o555}),
    )
    if copied_uv_sha256 != uv_sha256 or copied_uv_size != uv_size:
        _fail("rotation_stager_input_pinned_executable_changed")

    source_unsigned = {
        "schema": phase.SOURCE_V3_MANIFEST_SCHEMA,
        "release_revision": release_revision,
        "source_tree_oid": source_tree_oid,
        "object_format": object_format,
        "tree_listing_name": phase.TREE_LISTING_NAME,
        "tree_listing_sha256": sha256_bytes(listing),
        "tree_listing_size": len(listing),
        "tree_entry_count": len(entries),
        "blob_directory_name": phase.SOURCE_BLOB_DIRECTORY_NAME,
        "blobs": [blob_records[name] for name in sorted(blob_records)],
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    source_manifest = {
        **source_unsigned,
        "manifest_sha256": sha256_bytes(canonical_bytes(source_unsigned)),
    }
    source_raw = canonical_bytes(source_manifest) + b"\n"
    _write_exclusive(input_root / phase.SOURCE_MANIFEST_NAME, source_raw, mode=0o444)
    runtime_unsigned = {
        "schema": phase.RUNTIME_DEPENDENCY_MANIFEST_SCHEMA,
        "release_revision": release_revision,
        "wheel_directory_name": phase.RUNTIME_WHEEL_DIRECTORY_NAME,
        "wheels": [dict(item) for item in wheels],
        "installation": dict(phase._INSTALLATION),
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    runtime_manifest = {
        **runtime_unsigned,
        "manifest_sha256": sha256_bytes(canonical_bytes(runtime_unsigned)),
    }
    runtime_raw = canonical_bytes(runtime_manifest) + b"\n"
    _write_exclusive(input_root / phase.RUNTIME_MANIFEST_NAME, runtime_raw, mode=0o444)
    request_unsigned = {
        "schema": phase.UNIT_INPUT_ROTATION_STAGER_REQUEST_SCHEMA,
        "purpose": phase.UNIT_INPUT_ROTATION_STAGER_PURPOSE,
        "job_id": release_revision,
        "release_revision": release_revision,
        "source_tree_oid": source_tree_oid,
        "source_v3_manifest_name": phase.SOURCE_MANIFEST_NAME,
        "source_v3_manifest_sha256": sha256_bytes(source_raw),
        "runtime_dependency_manifest_name": phase.RUNTIME_MANIFEST_NAME,
        "runtime_dependency_manifest_sha256": sha256_bytes(runtime_raw),
        "uv_name": phase.UV_NAME,
        "uv_sha256": uv_sha256,
        "uv_size": uv_size,
        "python_executable_path": str(python_executable_path),
        "python_executable_sha256": python_sha256,
        "python_executable_size": python_size,
        "candidate_name": phase.CANDIDATE_NAME,
        "interpreter_relative_path": phase.INTERPRETER_RELATIVE_PATH,
        "entrypoint_relative_path": (
            phase.UNIT_INPUT_ROTATION_STAGER_ENTRYPOINT_RELATIVE_PATH
        ),
        "builder_identity": dict(phase._BUILDER_IDENTITY),
        "resume_policy": "reject-nonempty-output-requires-root-cleanup",
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    request = {
        **request_unsigned,
        "request_sha256": sha256_bytes(canonical_bytes(request_unsigned)),
    }
    try:
        phase.validate_request(request, expected_job_id=release_revision)
        phase.validate_source_manifest(source_manifest, request=request)
        phase.validate_runtime_manifest(runtime_manifest, request=request)
    except phase.ProductionReleaseBuilderPhaseError as exc:
        _fail("rotation_stager_input_internal_contract_invalid", exc)
    request_raw = canonical_bytes(request) + b"\n"
    _write_exclusive(input_root / phase.REQUEST_NAME, request_raw, mode=0o444)

    try:
        for directory in (blob_root, wheel_root, input_root, job_root):
            os.chmod(directory, 0o555)
        if tuple(os.listdir(output_root)):
            _fail("rotation_stager_input_output_not_empty")
        for directory in (
            blob_root,
            wheel_root,
            input_root,
            output_root,
            job_root,
            roots.job_root,
        ):
            _fsync_directory(directory)
        _verify_sealed_job(
            job_root=job_root,
            input_root=input_root,
            output_root=output_root,
            authority_uid=0 if production else authority_uid,
            authority_gid=0 if production else authority_gid,
            builder_uid=int(builder_uid),
            builder_gid=int(builder_gid),
        )
    except OSError as exc:
        _fail("rotation_stager_input_seal_failed", exc)

    unsigned_receipt = {
        "schema": AUTHOR_RECEIPT_SCHEMA,
        "repository_url": repository_url,
        "source_remote": source_remote,
        "release_revision": release_revision,
        "source_tree_oid": source_tree_oid,
        "wheelhouse_manifest_sha256": wheelhouse_manifest["manifest_sha256"],
        "builder_request_sha256": request["request_sha256"],
        "builder_request_file_sha256": sha256_bytes(request_raw),
        "uv_sha256": uv_sha256,
        "python_executable_sha256": python_sha256,
        "input_root": str(input_root),
        "input_root_owner": "root:root"
        if production
        else f"{authority_uid}:{authority_gid}",
        "input_root_mode": "0555",
        "output_root": str(output_root),
        "output_owner": f"{builder_uid}:{builder_gid}",
        "output_empty": True,
        "builder_started": False,
        "candidate_promoted": False,
        "activation_performed": False,
        "release_pointer_mutated": False,
        "gateway_mutated": False,
        "data_mutated": False,
        "credentials_mutated": False,
        "network_access_performed": False,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {
        **unsigned_receipt,
        "receipt_sha256": sha256_bytes(canonical_bytes(unsigned_receipt)),
    }


def author_rotation_stager_input(**kwargs: Any) -> Mapping[str, Any]:
    return _author_for_test(roots=AuthorRoots(), production=True, **kwargs)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-remote", required=True)
    parser.add_argument("--repository-url", required=True)
    parser.add_argument("--release-revision", required=True)
    parser.add_argument("--wheelhouse-root", type=Path, required=True)
    parser.add_argument("--wheelhouse-manifest", type=Path, required=True)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--expected-uv-sha256", required=True)
    parser.add_argument(
        "--python-executable", type=Path, default=Path("/usr/bin/python3.11")
    )
    parser.add_argument("--expected-python-sha256", required=True)
    arguments = parser.parse_args(argv)
    try:
        receipt = author_rotation_stager_input(
            source_root=arguments.source_root,
            source_remote=arguments.source_remote,
            repository_url=arguments.repository_url,
            release_revision=arguments.release_revision,
            wheelhouse_root=arguments.wheelhouse_root,
            wheelhouse_manifest_path=arguments.wheelhouse_manifest,
            uv_path=arguments.uv,
            expected_uv_sha256=arguments.expected_uv_sha256,
            python_executable_path=arguments.python_executable,
            expected_python_sha256=arguments.expected_python_sha256,
        )
    except (OSError, RotationStagerInputAuthorError):
        print(
            '{"error_code":"rotation_stager_input_authoring_failed","ok":false}',
            file=sys.stderr,
        )
        return 2
    print(canonical_bytes(receipt).decode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AUTHOR_RECEIPT_SCHEMA",
    "AuthorRoots",
    "RotationStagerInputAuthorError",
    "TARGET",
    "WHEELHOUSE_SCHEMA",
    "author_rotation_stager_input",
    "canonical_bytes",
    "sha256_bytes",
    "validate_wheelhouse_manifest",
]
