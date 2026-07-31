#!/usr/bin/env python3
"""Security boundary primitives for the pinned production release builder.

The module deliberately does not run Git, a build backend, a wheel installer,
or target-package Python.  A root-owned updater may use the data-only
functions here to:

* validate a NUL-delimited ``git ls-tree -rz`` result;
* materialize exact blobs from already-open, verified regular-file
  descriptors;
* retain an exact wheel without reopening its attacker-selectable path;
* prove the dedicated builder unit and UID have no remaining processes; and
* convert a builder-owned candidate into a root-owned, read-only release.

The publication manifest covers every payload entry.  Its terminal receipt is
the final filesystem object created in the candidate and is not sufficient on
its own: consumers must also call :func:`verify_published_release`, which
requires the complete root-owned tree and its manifest to remain exact.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import (
    Any,
    Callable,
    Iterable,
    Literal,
    Mapping,
    NoReturn,
    Protocol,
    Sequence,
)


MANIFEST_SCHEMA = "muncho-production-release-whole-tree-manifest.v1"
RECEIPT_SCHEMA = "muncho-production-release-candidate-seal.v1"
PROCESS_FREE_EVIDENCE_SCHEMA = "muncho-release-builder-process-free.v2"
PROCESS_FREE_EVIDENCE_SET_SCHEMA = (
    "muncho-release-builder-process-free-promotion.v1"
)
MANIFEST_NAME = "production-release-whole-tree-manifest.json"
RECEIPT_NAME = "production-release-candidate-seal.json"
BUILDER_UID = 29104
BUILDER_GID = 29104

MAX_GIT_TREE_BYTES = 64 * 1024 * 1024
MAX_GIT_TREE_ENTRIES = 200_000
MAX_PATH_BYTES = 4096
MAX_COMPONENT_BYTES = 255
MAX_BLOB_BYTES = 2 * 1024 * 1024 * 1024
MAX_WHEEL_BYTES = 1024 * 1024 * 1024
MAX_RELEASE_ENTRIES = 500_000
MAX_RELEASE_BYTES = 8 * 1024 * 1024 * 1024
MAX_RECORD_BYTES = 64 * 1024 * 1024
MAX_CGROUP_PROCS_BYTES = 1024 * 1024

_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SAFE_WHEEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,239}\.whl$")
_SYSTEMD_UNIT = re.compile(
    r"^muncho-release-builder(?:-v[23])?@[A-Za-z0-9_.:@-]{1,128}\.service$"
)
_INVOCATION_ID = re.compile(r"^[0-9a-f]{32}$")
_GIT_RECORD = re.compile(
    rb"^(?P<mode>[0-9]{6}) (?P<type>[a-z]+) "
    rb"(?P<oid>[0-9a-f]+)\t(?P<path>.+)$",
    re.DOTALL,
)
_ALLOWED_GIT_MODES = frozenset({0o100644, 0o100755})
_BUILDER_FILE_MODES = frozenset({
    0o400,
    0o440,
    0o444,
    0o600,
    0o640,
    0o644,
    0o700,
    0o750,
    0o755,
})
_BUILDER_DIRECTORY_MODES = frozenset({0o500, 0o550, 0o555, 0o700, 0o750, 0o755})
_SEALED_FILE_MODES = frozenset({0o444, 0o555})
_SEALED_DIRECTORY_MODE = 0o555
_CGROUP_DIRECTORY_MODES = frozenset(
    mode for mode in range(0o400, 0o756) if not mode & 0o022
)
_RESERVED_ROOT_NAMES = frozenset({MANIFEST_NAME, RECEIPT_NAME})


class ProductionReleaseBuilderError(RuntimeError):
    """Stable, secret-free release builder boundary failure."""


def _error(code: str, exc: BaseException | None = None) -> NoReturn:
    del exc
    raise ProductionReleaseBuilderError(code) from None


def _read_posix_identity(name: Literal["geteuid", "getegid"]) -> int:
    reader = getattr(os, name, None)
    if not callable(reader):
        _error("production_release_builder_posix_identity_unavailable")
    try:
        value = reader()
    except (OSError, TypeError, ValueError) as exc:
        _error("production_release_builder_posix_identity_unavailable", exc)
    if type(value) is not int or value < 0:
        _error("production_release_builder_posix_identity_unavailable")
    return value


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
        _error("production_release_builder_json_invalid", exc)


def _decode_canonical_line(raw: bytes, *, maximum: int) -> Mapping[str, Any]:
    if not 0 < len(raw) <= maximum or not raw.endswith(b"\n") or b"\n" in raw[:-1]:
        _error("production_release_builder_record_invalid")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in items:
            if not isinstance(name, str) or name in result:
                raise ValueError("duplicate key")
            result[name] = value
        return result

    try:
        value = json.loads(
            raw[:-1].decode("ascii", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        _error("production_release_builder_record_invalid", exc)
    if not isinstance(value, Mapping) or raw != _canonical(value) + b"\n":
        _error("production_release_builder_record_invalid")
    return dict(value)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _required_open_flags(*, directory: bool = False) -> int:
    required = ["O_CLOEXEC", "O_NOFOLLOW"]
    if directory:
        required.append("O_DIRECTORY")
    if any(not hasattr(os, name) for name in required):
        _error("production_release_builder_secure_open_unavailable")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    if directory:
        flags |= os.O_DIRECTORY
    return flags


XattrReader = Callable[[int], Sequence[str | bytes]]


def _read_descriptor_xattrs(descriptor: int) -> Sequence[str | bytes]:
    reader = getattr(os, "listxattr", None)
    if not callable(reader):
        _error("production_release_builder_xattr_inspection_unavailable")
    try:
        value = reader(descriptor)
    except (OSError, TypeError, ValueError) as exc:
        _error("production_release_builder_xattr_inspection_unavailable", exc)
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(name, (str, bytes)) or not name for name in value
    ):
        _error("production_release_builder_xattr_inspection_unavailable")
    return value


def _assert_no_xattrs(
    descriptor: int,
    *,
    xattr_reader: XattrReader,
) -> None:
    try:
        names = xattr_reader(descriptor)
    except ProductionReleaseBuilderError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        _error("production_release_builder_xattr_inspection_unavailable", exc)
    if not isinstance(names, (list, tuple)) or any(
        not isinstance(name, (str, bytes)) or not name for name in names
    ):
        _error("production_release_builder_xattr_inspection_unavailable")
    if names:
        _error("production_release_builder_xattrs_present")


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    links: int
    size: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> FileIdentity:
        return cls(
            device=int(value.st_dev),
            inode=int(value.st_ino),
            mode=int(value.st_mode),
            uid=int(value.st_uid),
            gid=int(value.st_gid),
            links=int(value.st_nlink),
            size=int(value.st_size),
            modified_ns=int(value.st_mtime_ns),
            changed_ns=int(value.st_ctime_ns),
        )


def _hash_descriptor(
    descriptor: int,
    *,
    size: int,
    algorithm: str = "sha256",
) -> str:
    if size < 0 or not hasattr(os, "pread"):
        _error("production_release_builder_descriptor_invalid")
    try:
        digest = hashlib.new(algorithm)
    except ValueError as exc:
        _error("production_release_builder_digest_algorithm_invalid", exc)
    offset = 0
    try:
        while offset < size:
            chunk = os.pread(descriptor, min(1024 * 1024, size - offset), offset)
            if not chunk:
                _error("production_release_builder_file_changed")
            digest.update(chunk)
            offset += len(chunk)
        if os.pread(descriptor, 1, size):
            _error("production_release_builder_file_changed")
    except ProductionReleaseBuilderError:
        raise
    except OSError as exc:
        _error("production_release_builder_file_unavailable", exc)
    return digest.hexdigest()


@dataclass
class HeldRegularFile(AbstractContextManager["HeldRegularFile"]):
    """One regular file whose verified inode stays open until consumption."""

    path: Path
    descriptor: int
    identity: FileIdentity
    sha256: str
    _closed: bool = False

    def assert_stable(self, *, require_path_binding: bool = True) -> None:
        if self._closed:
            _error("production_release_builder_descriptor_closed")
        if require_path_binding:
            try:
                reachable = FileIdentity.from_stat(os.lstat(self.path))
            except OSError as exc:
                _error("production_release_builder_path_binding_changed", exc)
            if (
                reachable.device,
                reachable.inode,
            ) != (
                self.identity.device,
                self.identity.inode,
            ):
                _error("production_release_builder_path_binding_changed")
        try:
            current = FileIdentity.from_stat(os.fstat(self.descriptor))
        except OSError as exc:
            _error("production_release_builder_file_unavailable", exc)
        if current != self.identity:
            _error("production_release_builder_file_changed")
        if require_path_binding and reachable != self.identity:
            _error("production_release_builder_file_changed")

    def close(self) -> None:
        if not self._closed:
            os.close(self.descriptor)
            self._closed = True

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: Any,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()


def open_held_regular(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
    allowed_modes: frozenset[int],
    maximum_bytes: int,
    expected_sha256: str | None = None,
    require_nonempty: bool = True,
) -> HeldRegularFile:
    """Open and verify one regular file without releasing its inode."""

    path = Path(path)
    if (
        not path.is_absolute()
        or expected_uid < 0
        or expected_gid < 0
        or not allowed_modes
        or maximum_bytes < 1
        or (expected_sha256 is not None and _SHA256.fullmatch(expected_sha256) is None)
    ):
        _error("production_release_builder_file_contract_invalid")
    descriptor: int | None = None
    try:
        if path.resolve(strict=True) != path:
            _error("production_release_builder_path_binding_changed")
        before = FileIdentity.from_stat(os.lstat(path))
        if (
            not stat.S_ISREG(before.mode)
            or stat.S_ISLNK(before.mode)
            or before.uid != expected_uid
            or before.gid != expected_gid
            or before.links != 1
            or stat.S_IMODE(before.mode) not in allowed_modes
            or before.size > maximum_bytes
            or (require_nonempty and before.size < 1)
        ):
            _error("production_release_builder_file_invalid")
        descriptor = os.open(path, _required_open_flags())
        opened = FileIdentity.from_stat(os.fstat(descriptor))
        if before != opened:
            _error("production_release_builder_file_invalid")
        digest = _hash_descriptor(descriptor, size=opened.size)
        after = FileIdentity.from_stat(os.fstat(descriptor))
        reachable = FileIdentity.from_stat(os.lstat(path))
        if opened != after:
            _error("production_release_builder_file_changed")
        if opened != reachable:
            _error("production_release_builder_path_binding_changed")
        if expected_sha256 is not None and digest != expected_sha256:
            _error("production_release_builder_file_digest_mismatch")
        return HeldRegularFile(
            path=path,
            descriptor=descriptor,
            identity=opened,
            sha256=digest,
        )
    except ProductionReleaseBuilderError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        _error("production_release_builder_file_unavailable", exc)


@dataclass(frozen=True, order=True)
class GitTreeEntry:
    """One accepted regular blob from a recursive Git tree listing."""

    path: str
    mode: int
    object_id: str
    object_format: str

    @property
    def executable(self) -> bool:
        return self.mode == 0o100755


def _validate_relative_path(raw: bytes) -> str:
    if (
        not raw
        or len(raw) > MAX_PATH_BYTES
        or raw.startswith(b"/")
        or raw.endswith(b"/")
        or b"\\" in raw
        or b"\x00" in raw
    ):
        _error("production_release_builder_git_path_invalid")
    try:
        path = raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        _error("production_release_builder_git_path_invalid", exc)
    if unicodedata.normalize("NFC", path) != path or any(
        ord(character) < 0x20
        or ord(character) == 0x7F
        or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        for character in path
    ):
        _error("production_release_builder_git_path_invalid")
    pure = PurePosixPath(path)
    parts = pure.parts
    if (
        not parts
        or str(pure) != path
        or any(
            part in {"", ".", ".."}
            or part.casefold() == ".git"
            or len(part.encode("utf-8", errors="strict")) > MAX_COMPONENT_BYTES
            for part in parts
        )
        or (len(parts) == 1 and parts[0] in _RESERVED_ROOT_NAMES)
    ):
        _error("production_release_builder_git_path_invalid")
    return path


def parse_git_tree(
    raw: bytes,
    *,
    object_format: str = "sha1",
) -> tuple[GitTreeEntry, ...]:
    """Validate exact ``git ls-tree -rz --full-tree`` output.

    Only ordinary ``100644`` and ``100755`` blobs are accepted.  Symlinks,
    submodules, sparse/special entries, non-canonical paths, duplicates, and
    file/directory prefix collisions are rejected.
    """

    oid_pattern = _SHA1 if object_format == "sha1" else _SHA256
    if (
        object_format not in {"sha1", "sha256"}
        or not isinstance(raw, bytes)
        or not 0 < len(raw) <= MAX_GIT_TREE_BYTES
        or not raw.endswith(b"\x00")
    ):
        _error("production_release_builder_git_tree_invalid")
    records = raw[:-1].split(b"\x00")
    if not records or len(records) > MAX_GIT_TREE_ENTRIES:
        _error("production_release_builder_git_tree_invalid")
    entries: list[GitTreeEntry] = []
    previous_path_bytes: bytes | None = None
    paths: set[str] = set()
    files: set[tuple[str, ...]] = set()
    for record in records:
        match = _GIT_RECORD.fullmatch(record)
        if match is None:
            _error("production_release_builder_git_tree_invalid")
        try:
            mode = int(match.group("mode"), 8)
            object_type = match.group("type").decode("ascii", errors="strict")
            object_id = match.group("oid").decode("ascii", errors="strict")
        except (ValueError, UnicodeError) as exc:
            _error("production_release_builder_git_tree_invalid", exc)
        path_bytes = match.group("path")
        path = _validate_relative_path(path_bytes)
        parts = PurePosixPath(path).parts
        if (
            mode not in _ALLOWED_GIT_MODES
            or object_type != "blob"
            or oid_pattern.fullmatch(object_id) is None
            or path in paths
            or any(parts[:index] in files for index in range(1, len(parts)))
            or (previous_path_bytes is not None and path_bytes <= previous_path_bytes)
        ):
            _error("production_release_builder_git_tree_invalid")
        paths.add(path)
        files.add(parts)
        previous_path_bytes = path_bytes
        entries.append(
            GitTreeEntry(
                path=path,
                mode=mode,
                object_id=object_id,
                object_format=object_format,
            )
        )
    return tuple(entries)


def _git_blob_oid(
    descriptor: int,
    *,
    size: int,
    object_format: str,
) -> str:
    algorithm = "sha1" if object_format == "sha1" else "sha256"
    try:
        digest = hashlib.new(algorithm)
    except ValueError as exc:
        _error("production_release_builder_digest_algorithm_invalid", exc)
    digest.update(f"blob {size}\0".encode("ascii", errors="strict"))
    offset = 0
    try:
        while offset < size:
            chunk = os.pread(descriptor, min(1024 * 1024, size - offset), offset)
            if not chunk:
                _error("production_release_builder_file_changed")
            digest.update(chunk)
            offset += len(chunk)
        if os.pread(descriptor, 1, size):
            _error("production_release_builder_file_changed")
    except ProductionReleaseBuilderError:
        raise
    except OSError as exc:
        _error("production_release_builder_file_unavailable", exc)
    return digest.hexdigest()


@dataclass
class _GitTreeNode:
    children: dict[bytes, GitTreeEntry | "_GitTreeNode"]


def _reconstruct_git_tree_oid(entries: Sequence[GitTreeEntry]) -> str:
    """Reconstruct Git tree objects from a validated recursive blob listing."""

    if not entries or len({entry.object_format for entry in entries}) != 1:
        _error("production_release_builder_git_tree_invalid")
    object_format = entries[0].object_format
    algorithm = "sha1" if object_format == "sha1" else "sha256"
    oid_bytes = 20 if object_format == "sha1" else 32
    root = _GitTreeNode(children={})
    for entry in entries:
        parts = tuple(
            part.encode("utf-8", errors="strict")
            for part in PurePosixPath(entry.path).parts
        )
        node = root
        for component in parts[:-1]:
            existing = node.children.get(component)
            if existing is None:
                existing = _GitTreeNode(children={})
                node.children[component] = existing
            if not isinstance(existing, _GitTreeNode):
                _error("production_release_builder_git_tree_invalid")
            node = existing
        if parts[-1] in node.children:
            _error("production_release_builder_git_tree_invalid")
        node.children[parts[-1]] = entry

    def hash_node(node: _GitTreeNode) -> str:
        serialized: list[tuple[bytes, bytes]] = []
        for name, child in node.children.items():
            if isinstance(child, _GitTreeNode):
                mode = b"40000"
                object_id = hash_node(child)
                sort_key = name + b"/"
            else:
                mode = f"{child.mode:o}".encode("ascii", errors="strict")
                object_id = child.object_id
                sort_key = name
            try:
                raw_object_id = bytes.fromhex(object_id)
            except ValueError as exc:
                _error("production_release_builder_git_tree_invalid", exc)
            if len(raw_object_id) != oid_bytes:
                _error("production_release_builder_git_tree_invalid")
            serialized.append((
                sort_key,
                mode + b" " + name + b"\x00" + raw_object_id,
            ))
        payload = b"".join(value for _key, value in sorted(serialized))
        digest = hashlib.new(algorithm)
        digest.update(f"tree {len(payload)}\0".encode("ascii", errors="strict"))
        digest.update(payload)
        return digest.hexdigest()

    return hash_node(root)


class BlobOpener(Protocol):
    def __call__(
        self,
        entry: GitTreeEntry,
    ) -> AbstractContextManager[HeldRegularFile]: ...


def _validate_simple_name(name: str) -> None:
    try:
        raw = name.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        _error("production_release_builder_path_invalid", exc)
    if (
        not raw
        or len(raw) > MAX_COMPONENT_BYTES
        or "/" in name
        or "\\" in name
        or name in {".", ".."}
        or name.casefold() == ".git"
        or unicodedata.normalize("NFC", name) != name
        or any(
            ord(character) < 0x20
            or ord(character) == 0x7F
            or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in name
        )
    ):
        _error("production_release_builder_path_invalid")


def _stat_at(parent_descriptor: int, name: str) -> FileIdentity:
    try:
        return FileIdentity.from_stat(
            os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        )
    except OSError as exc:
        _error("production_release_builder_path_unavailable", exc)


def _unlink_created_if_still_bound(
    parent_descriptor: int,
    name: str,
    descriptor: int | None,
) -> None:
    """Remove only the exact inode created by this process."""

    if descriptor is None:
        return
    try:
        opened = FileIdentity.from_stat(os.fstat(descriptor))
        reachable = _stat_at(parent_descriptor, name)
        if (
            opened.device,
            opened.inode,
        ) == (
            reachable.device,
            reachable.inode,
        ):
            os.unlink(name, dir_fd=parent_descriptor)
    except (OSError, ProductionReleaseBuilderError):
        return


def _open_directory_path(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
    allowed_modes: frozenset[int],
) -> tuple[int, FileIdentity]:
    descriptor: int | None = None
    try:
        if path.resolve(strict=True) != path:
            _error("production_release_builder_directory_invalid")
        before = FileIdentity.from_stat(os.lstat(path))
        descriptor = os.open(path, _required_open_flags(directory=True))
        opened = FileIdentity.from_stat(os.fstat(descriptor))
        after = FileIdentity.from_stat(os.lstat(path))
        if (
            before != opened
            or opened != after
            or not stat.S_ISDIR(opened.mode)
            or stat.S_ISLNK(opened.mode)
            or opened.uid != expected_uid
            or opened.gid != expected_gid
            or stat.S_IMODE(opened.mode) not in allowed_modes
            or stat.S_IMODE(opened.mode) & 0o022
        ):
            _error("production_release_builder_directory_invalid")
        return descriptor, opened
    except ProductionReleaseBuilderError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        _error("production_release_builder_directory_unavailable", exc)


def _open_child_directory(
    parent_descriptor: int,
    name: str,
    *,
    expected_uid: int,
    expected_gid: int | None,
    allowed_modes: frozenset[int],
) -> tuple[int, FileIdentity]:
    _validate_simple_name(name)
    descriptor: int | None = None
    try:
        before = _stat_at(parent_descriptor, name)
        descriptor = os.open(
            name,
            _required_open_flags(directory=True),
            dir_fd=parent_descriptor,
        )
        opened = FileIdentity.from_stat(os.fstat(descriptor))
        after = _stat_at(parent_descriptor, name)
        if (
            before != opened
            or opened != after
            or not stat.S_ISDIR(opened.mode)
            or stat.S_ISLNK(opened.mode)
            or opened.uid != expected_uid
            or (expected_gid is not None and opened.gid != expected_gid)
            or stat.S_IMODE(opened.mode) not in allowed_modes
            or stat.S_IMODE(opened.mode) & 0o022
        ):
            _error("production_release_builder_directory_invalid")
        return descriptor, opened
    except ProductionReleaseBuilderError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        _error("production_release_builder_directory_unavailable", exc)


def _open_relative_directory(
    root_descriptor: int,
    parts: Sequence[str],
    *,
    expected_uid: int,
    expected_gid: int | None,
    create: bool,
) -> int:
    current = os.dup(root_descriptor)
    try:
        for part in parts:
            _validate_simple_name(part)
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
                except OSError as exc:
                    _error("production_release_builder_directory_create_failed", exc)
            child, _identity = _open_child_directory(
                current,
                part,
                expected_uid=expected_uid,
                expected_gid=expected_gid,
                allowed_modes=frozenset({0o700}),
            )
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _fchown_if_needed(
    descriptor: int,
    destination_uid: int,
    destination_gid: int,
) -> None:
    current = os.fstat(descriptor)
    if (current.st_uid, current.st_gid) != (
        destination_uid,
        destination_gid,
    ):
        os.fchown(descriptor, destination_uid, destination_gid)


def _copy_held_to_directory(
    held: HeldRegularFile,
    destination_descriptor: int,
    name: str,
    *,
    mode: int,
    destination_uid: int,
    destination_gid: int,
    expected_git_oid: str | None = None,
    object_format: str | None = None,
) -> Mapping[str, Any]:
    _validate_simple_name(name)
    if mode not in _SEALED_FILE_MODES:
        _error("production_release_builder_destination_mode_invalid")
    if (expected_git_oid is None) != (object_format is None):
        _error("production_release_builder_git_blob_contract_invalid")
    held.assert_stable()
    observed_sha256 = _hash_descriptor(
        held.descriptor,
        size=held.identity.size,
    )
    if observed_sha256 != held.sha256:
        _error("production_release_builder_file_changed")
    if expected_git_oid is not None:
        observed_git_oid = _git_blob_oid(
            held.descriptor,
            size=held.identity.size,
            object_format=str(object_format),
        )
        if observed_git_oid != expected_git_oid:
            _error("production_release_builder_git_blob_mismatch")
    else:
        observed_git_oid = None

    output: int | None = None
    created = False
    try:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        output = os.open(name, flags, 0o600, dir_fd=destination_descriptor)
        created = True
        offset = 0
        digest = hashlib.sha256()
        while offset < held.identity.size:
            chunk = os.pread(
                held.descriptor,
                min(1024 * 1024, held.identity.size - offset),
                offset,
            )
            if not chunk:
                _error("production_release_builder_file_changed")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(output, view)
                if written <= 0:
                    _error("production_release_builder_destination_write_failed")
                view = view[written:]
            offset += len(chunk)
        if (
            os.pread(held.descriptor, 1, held.identity.size)
            or digest.hexdigest() != held.sha256
        ):
            _error("production_release_builder_file_changed")
        held.assert_stable()
        _fchown_if_needed(output, destination_uid, destination_gid)
        os.fchmod(output, mode)
        os.fsync(output)
        final = FileIdentity.from_stat(os.fstat(output))
        reachable = _stat_at(destination_descriptor, name)
        if (
            final != reachable
            or not stat.S_ISREG(final.mode)
            or final.links != 1
            or final.uid != destination_uid
            or final.gid != destination_gid
            or stat.S_IMODE(final.mode) != mode
            or final.size != held.identity.size
            or _hash_descriptor(output, size=final.size) != held.sha256
        ):
            _error("production_release_builder_destination_invalid")
        return {
            "size": final.size,
            "sha256": held.sha256,
            "git_object_id": observed_git_oid,
            "mode": f"{mode:04o}",
            "uid": destination_uid,
            "gid": destination_gid,
        }
    except ProductionReleaseBuilderError:
        if created:
            _unlink_created_if_still_bound(
                destination_descriptor,
                name,
                output,
            )
        if output is not None:
            os.close(output)
            output = None
        raise
    except OSError as exc:
        if created:
            _unlink_created_if_still_bound(
                destination_descriptor,
                name,
                output,
            )
        if output is not None:
            os.close(output)
            output = None
        _error("production_release_builder_destination_write_failed", exc)
    finally:
        if output is not None:
            os.close(output)


def materialize_git_tree(
    entries: Sequence[GitTreeEntry],
    destination: Path,
    *,
    revision: str,
    source_tree_oid: str,
    open_blob: BlobOpener,
    destination_uid: int,
    destination_gid: int,
    parent_uid: int,
    parent_gid: int,
    _xattr_reader: XattrReader | None = None,
) -> Mapping[str, Any]:
    """Materialize exact Git blobs into one new immutable source tree."""

    destination = Path(destination)
    if (
        not destination.is_absolute()
        or destination.parent == destination
        or _REVISION.fullmatch(revision) is None
        or not entries
        or len(entries) > MAX_GIT_TREE_ENTRIES
        or destination_uid < 0
        or destination_gid < 0
    ):
        _error("production_release_builder_materialization_contract_invalid")
    _validate_simple_name(destination.name)
    if any(not isinstance(entry, GitTreeEntry) for entry in entries):
        _error("production_release_builder_git_tree_invalid")
    ordered = tuple(sorted(entries, key=lambda item: item.path.encode("utf-8")))
    if tuple(entries) != ordered or len({item.path for item in ordered}) != len(
        ordered
    ):
        _error("production_release_builder_git_tree_invalid")
    expected_tree_oid = _SHA1 if ordered[0].object_format == "sha1" else _SHA256
    if (
        not isinstance(source_tree_oid, str)
        or len({entry.object_format for entry in ordered}) != 1
        or expected_tree_oid.fullmatch(source_tree_oid) is None
    ):
        _error("production_release_builder_git_tree_invalid")
    for entry in ordered:
        if (
            not isinstance(entry, GitTreeEntry)
            or entry.mode not in _ALLOWED_GIT_MODES
            or entry.object_format not in {"sha1", "sha256"}
            or (
                (_SHA1 if entry.object_format == "sha1" else _SHA256).fullmatch(
                    entry.object_id
                )
                is None
            )
            or _validate_relative_path(entry.path.encode("utf-8", errors="strict"))
            != entry.path
        ):
            _error("production_release_builder_git_tree_invalid")
    if _reconstruct_git_tree_oid(ordered) != source_tree_oid:
        _error("production_release_builder_git_tree_oid_mismatch")
    xattr_reader = _read_descriptor_xattrs if _xattr_reader is None else _xattr_reader
    parent_descriptor, parent_identity = _open_directory_path(
        destination.parent,
        expected_uid=parent_uid,
        expected_gid=parent_gid,
        allowed_modes=frozenset({0o700, 0o750, 0o755}),
    )
    root_descriptor: int | None = None
    try:
        try:
            os.mkdir(destination.name, 0o700, dir_fd=parent_descriptor)
        except OSError as exc:
            _error("production_release_builder_destination_create_failed", exc)
        root_descriptor, _root_identity = _open_child_directory(
            parent_descriptor,
            destination.name,
            expected_uid=_read_posix_identity("geteuid"),
            expected_gid=None,
            allowed_modes=frozenset({0o700}),
        )
        records: list[Mapping[str, Any]] = []
        total = 0
        directories: set[tuple[str, ...]] = set()
        for entry in ordered:
            parts = PurePosixPath(entry.path).parts
            directories.update(parts[:index] for index in range(1, len(parts)))
            directory_descriptor = _open_relative_directory(
                root_descriptor,
                parts[:-1],
                expected_uid=_read_posix_identity("geteuid"),
                expected_gid=None,
                create=True,
            )
            try:
                with open_blob(entry) as held:
                    if not isinstance(held, HeldRegularFile):
                        _error("production_release_builder_blob_provider_invalid")
                    if held.identity.size > MAX_BLOB_BYTES:
                        _error("production_release_builder_materialization_oversized")
                    record = _copy_held_to_directory(
                        held,
                        directory_descriptor,
                        parts[-1],
                        mode=0o555 if entry.executable else 0o444,
                        destination_uid=destination_uid,
                        destination_gid=destination_gid,
                        expected_git_oid=entry.object_id,
                        object_format=entry.object_format,
                    )
            finally:
                os.close(directory_descriptor)
            records.append({"path": entry.path, **record})
            total += int(record["size"])
            if total > MAX_RELEASE_BYTES:
                _error("production_release_builder_materialization_oversized")

        for parts in sorted(directories, key=lambda value: (-len(value), value)):
            directory_descriptor = _open_relative_directory(
                root_descriptor,
                parts,
                expected_uid=_read_posix_identity("geteuid"),
                expected_gid=None,
                create=False,
            )
            try:
                _fchown_if_needed(
                    directory_descriptor,
                    destination_uid,
                    destination_gid,
                )
                os.fchmod(directory_descriptor, _SEALED_DIRECTORY_MODE)
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        _fchown_if_needed(
            root_descriptor,
            destination_uid,
            destination_gid,
        )
        os.fchmod(root_descriptor, _SEALED_DIRECTORY_MODE)
        os.fsync(root_descriptor)
        observed = _TreeAccumulator(entries=[])
        _seal_payload_directory_contents(
            root_descriptor,
            PurePosixPath("."),
            staging_uid=destination_uid,
            staging_gid=destination_gid,
            publication_uid=destination_uid,
            publication_gid=destination_gid,
            accumulator=observed,
            excluded_root_names=frozenset(),
            xattr_reader=xattr_reader,
        )
        _assert_no_xattrs(root_descriptor, xattr_reader=xattr_reader)
        observed_files = {
            str(item["path"]): item
            for item in observed.entries
            if item["kind"] == "file"
        }
        observed_directories = {
            str(item["path"])
            for item in observed.entries
            if item["kind"] == "directory"
        }
        expected_directories = {
            PurePosixPath(*parts).as_posix() for parts in directories
        }
        if (
            set(observed_files) != {str(item["path"]) for item in records}
            or observed_directories != expected_directories
            or any(
                observed_files[str(item["path"])]
                != {
                    "path": item["path"],
                    "kind": "file",
                    "mode": item["mode"],
                    "uid": item["uid"],
                    "gid": item["gid"],
                    "size": item["size"],
                    "sha256": item["sha256"],
                    "xattrs": [],
                }
                for item in records
            )
        ):
            _error("production_release_builder_materialization_changed")
        os.fsync(parent_descriptor)
        final_root = FileIdentity.from_stat(os.fstat(root_descriptor))
        reachable_root = _stat_at(parent_descriptor, destination.name)
        reachable_parent = FileIdentity.from_stat(os.fstat(parent_descriptor))
        external_root = FileIdentity.from_stat(os.lstat(destination))
        external_parent = FileIdentity.from_stat(os.lstat(destination.parent))
        if (
            final_root != reachable_root
            or final_root != external_root
            or _directory_binding(reachable_parent)
            != _directory_binding(parent_identity)
            or _directory_binding(external_parent)
            != _directory_binding(parent_identity)
            or final_root.uid != destination_uid
            or final_root.gid != destination_gid
            or stat.S_IMODE(final_root.mode) != _SEALED_DIRECTORY_MODE
        ):
            _error("production_release_builder_materialization_changed")
        unsigned = {
            "schema": "muncho-production-source-materialization.v1",
            "source_revision": revision,
            "source_tree_oid": source_tree_oid,
            "git_tree_sha256": _sha256_bytes(
                _canonical([
                    {
                        "path": entry.path,
                        "mode": f"{entry.mode:06o}",
                        "object_id": entry.object_id,
                        "object_format": entry.object_format,
                    }
                    for entry in ordered
                ])
            ),
            "entry_count": len(records),
            "tree_bytes": total,
            "entries": records,
            "tree_sha256": _sha256_bytes(_canonical(records)),
            "root_uid": destination_uid,
            "root_gid": destination_gid,
            "root_mode": f"{_SEALED_DIRECTORY_MODE:04o}",
        }
        return {
            **unsigned,
            "materialization_sha256": _sha256_bytes(_canonical(unsigned)),
        }
    finally:
        if root_descriptor is not None:
            os.close(root_descriptor)
        os.close(parent_descriptor)


def retain_verified_wheel(
    source: Path,
    destination_directory: Path,
    *,
    expected_sha256: str,
    builder_uid: int,
    builder_gid: int,
    destination_uid: int,
    destination_gid: int,
) -> Mapping[str, Any]:
    """Copy one exact held wheel into a trusted artifact directory."""

    source = Path(source)
    destination_directory = Path(destination_directory)
    if (
        _SAFE_WHEEL.fullmatch(source.name) is None
        or _SHA256.fullmatch(expected_sha256) is None
    ):
        _error("production_release_builder_wheel_contract_invalid")
    with open_held_regular(
        source,
        expected_uid=builder_uid,
        expected_gid=builder_gid,
        allowed_modes=_BUILDER_FILE_MODES,
        maximum_bytes=MAX_WHEEL_BYTES,
        expected_sha256=expected_sha256,
    ) as held:
        directory_descriptor, _identity = _open_directory_path(
            destination_directory,
            expected_uid=destination_uid,
            expected_gid=destination_gid,
            allowed_modes=frozenset({0o700, 0o750, 0o755}),
        )
        try:
            record = _copy_held_to_directory(
                held,
                directory_descriptor,
                source.name,
                mode=0o444,
                destination_uid=destination_uid,
                destination_gid=destination_gid,
            )
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    return {
        "schema": "muncho-production-retained-wheel.v1",
        "name": source.name,
        **record,
    }


@dataclass(frozen=True)
class ReleaseIdentities:
    """Logical production identities bound into the release receipt."""

    builder_uid: int
    builder_gid: int
    reserved_runtime_uids: tuple[int, ...]
    reserved_runtime_gids: tuple[int, ...]
    root_uid: int = 0
    root_gid: int = 0


def validate_release_identities(
    identities: ReleaseIdentities,
    *,
    require_effective_root: bool = False,
    effective_uid: int | None = None,
) -> ReleaseIdentities:
    """Require a dedicated builder, real root publisher, and runtime UIDs."""

    if not isinstance(identities, ReleaseIdentities):
        _error("production_release_builder_identity_contract_invalid")
    uid_values = (
        identities.root_uid,
        identities.builder_uid,
        *identities.reserved_runtime_uids,
    )
    gid_values = (
        identities.root_gid,
        identities.builder_gid,
        *identities.reserved_runtime_gids,
    )
    if (
        identities.root_uid != 0
        or identities.root_gid != 0
        or identities.builder_uid != BUILDER_UID
        or identities.builder_gid != BUILDER_GID
        or not identities.reserved_runtime_uids
        or not identities.reserved_runtime_gids
        or any(type(value) is not int or value < 0 for value in uid_values)
        or any(type(value) is not int or value < 0 for value in gid_values)
        or len(set(uid_values)) != len(uid_values)
        or len(set(gid_values)) != len(gid_values)
        or tuple(sorted(identities.reserved_runtime_uids))
        != identities.reserved_runtime_uids
        or tuple(sorted(identities.reserved_runtime_gids))
        != identities.reserved_runtime_gids
    ):
        _error("production_release_builder_identity_contract_invalid")
    observed_euid = (
        _read_posix_identity("geteuid")
        if effective_uid is None
        else effective_uid
    )
    if require_effective_root and observed_euid != identities.root_uid:
        _error("production_release_builder_root_authority_required")
    return identities


_SYSTEMD_EVIDENCE_FIELDS = frozenset({
    "Id",
    "FragmentPath",
    "DropInPaths",
    "LoadState",
    "ActiveState",
    "SubState",
    "MainPID",
    "ExecMainPID",
    "Result",
    "ExecMainCode",
    "ExecMainStatus",
    "InvocationID",
    "ControlGroup",
})


def _normalized_systemd_properties(
    value: Mapping[str, Any],
    *,
    expected_unit: str,
    expected_fragment: Path,
    expected_control_group: str,
) -> Mapping[str, str]:
    if (
        not isinstance(value, Mapping)
        or set(value) != _SYSTEMD_EVIDENCE_FIELDS
        or _SYSTEMD_UNIT.fullmatch(expected_unit) is None
        or not expected_fragment.is_absolute()
        or not expected_control_group.startswith("/")
        or ".." in PurePosixPath(expected_control_group).parts
    ):
        _error("production_release_builder_systemd_evidence_invalid")
    normalized: dict[str, str] = {}
    for name, item in value.items():
        if isinstance(item, int) and not isinstance(item, bool):
            normalized[name] = str(item)
        elif isinstance(item, str):
            normalized[name] = item
        else:
            _error("production_release_builder_systemd_evidence_invalid")
    completion_state = (
        normalized["ActiveState"],
        normalized["SubState"],
    )
    if (
        normalized["Id"] != expected_unit
        or normalized["FragmentPath"] != str(expected_fragment)
        or normalized["DropInPaths"] != ""
        or normalized["LoadState"] != "loaded"
        or completion_state
        not in {("inactive", "dead"), ("active", "exited")}
        or normalized["MainPID"] != "0"
        or normalized["ExecMainPID"] != "0"
        or normalized["Result"] != "success"
        or normalized["ExecMainCode"] != "exited"
        or normalized["ExecMainStatus"] != "0"
        or _INVOCATION_ID.fullmatch(normalized["InvocationID"]) is None
        or normalized["ControlGroup"] not in {"", expected_control_group}
    ):
        _error("production_release_builder_systemd_evidence_invalid")
    return normalized


def _directory_binding(value: FileIdentity) -> tuple[int, int, int, int, int]:
    return (value.device, value.inode, value.mode, value.uid, value.gid)


def _read_empty_cgroup_procs(
    directory_descriptor: int,
    *,
    authority_uid: int,
    authority_gid: int,
) -> None:
    name = "cgroup.procs"
    descriptor: int | None = None
    try:
        before = _stat_at(directory_descriptor, name)
        descriptor = os.open(
            name,
            _required_open_flags(),
            dir_fd=directory_descriptor,
        )
        opened = FileIdentity.from_stat(os.fstat(descriptor))
        if (
            before != opened
            or not stat.S_ISREG(opened.mode)
            or stat.S_ISLNK(opened.mode)
            or opened.uid != authority_uid
            or opened.gid != authority_gid
            or opened.links != 1
            or stat.S_IMODE(opened.mode) & 0o022
        ):
            _error("production_release_builder_cgroup_invalid")
        chunks: list[bytes] = []
        observed = 0
        while observed <= MAX_CGROUP_PROCS_BYTES:
            chunk = os.read(
                descriptor,
                MAX_CGROUP_PROCS_BYTES + 1 - observed,
            )
            if not chunk:
                break
            chunks.append(chunk)
            observed += len(chunk)
        payload = b"".join(chunks)
        after = FileIdentity.from_stat(os.fstat(descriptor))
        reachable = _stat_at(directory_descriptor, name)
        if (
            len(payload) > MAX_CGROUP_PROCS_BYTES
            or payload != b""
            or before != after
            or before != reachable
        ):
            _error("production_release_builder_cgroup_not_empty")
    except ProductionReleaseBuilderError:
        raise
    except OSError as exc:
        _error("production_release_builder_cgroup_invalid", exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _scan_empty_cgroup_tree(
    root: Path,
    *,
    authority_uid: int,
    authority_gid: int,
) -> tuple[str, ...]:
    root_descriptor, root_identity = _open_directory_path(
        root,
        expected_uid=authority_uid,
        expected_gid=authority_gid,
        allowed_modes=_CGROUP_DIRECTORY_MODES,
    )
    inspected: list[str] = []

    def visit(
        descriptor: int,
        relative: PurePosixPath,
        expected_identity: FileIdentity,
    ) -> None:
        try:
            names = sorted(os.listdir(descriptor))
        except OSError as exc:
            _error("production_release_builder_cgroup_invalid", exc)
        if "cgroup.procs" not in names:
            _error("production_release_builder_cgroup_invalid")
        _read_empty_cgroup_procs(
            descriptor,
            authority_uid=authority_uid,
            authority_gid=authority_gid,
        )
        inspected.append("/" if str(relative) == "." else relative.as_posix())
        directories: list[tuple[str, int, FileIdentity]] = []
        for name in names:
            _validate_simple_name(name)
            state = _stat_at(descriptor, name)
            if stat.S_ISDIR(state.mode) and not stat.S_ISLNK(state.mode):
                child, child_identity = _open_child_directory(
                    descriptor,
                    name,
                    expected_uid=authority_uid,
                    expected_gid=authority_gid,
                    allowed_modes=_CGROUP_DIRECTORY_MODES,
                )
                directories.append((name, child, child_identity))
            elif stat.S_ISLNK(state.mode):
                _error("production_release_builder_cgroup_invalid")
            elif not stat.S_ISREG(state.mode):
                _error("production_release_builder_cgroup_invalid")
        for name, child, child_identity in directories:
            try:
                visit(child, relative / name, child_identity)
            finally:
                os.close(child)
        try:
            final_names = sorted(os.listdir(descriptor))
            final_identity = FileIdentity.from_stat(os.fstat(descriptor))
        except OSError as exc:
            _error("production_release_builder_cgroup_invalid", exc)
        if final_names != names or _directory_binding(
            final_identity
        ) != _directory_binding(expected_identity):
            _error("production_release_builder_cgroup_changed")

    try:
        visit(root_descriptor, PurePosixPath("."), root_identity)
    finally:
        os.close(root_descriptor)
    return tuple(inspected)


def _cgroup_address_exists(
    cgroup_root: Path,
    relative: PurePosixPath,
    *,
    authority_uid: int,
    authority_gid: int,
) -> bool:
    """Resolve every cgroup component without following a symlink."""

    descriptor, _identity = _open_directory_path(
        cgroup_root,
        expected_uid=authority_uid,
        expected_gid=authority_gid,
        allowed_modes=_CGROUP_DIRECTORY_MODES,
    )
    try:
        components = relative.parts[1:]
        for index, name in enumerate(components):
            _validate_simple_name(name)
            try:
                state = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                if index == len(components) - 1:
                    return False
                _error("production_release_builder_cgroup_invalid")
            except OSError as exc:
                _error("production_release_builder_cgroup_invalid", exc)
            if not stat.S_ISDIR(state.st_mode) or stat.S_ISLNK(state.st_mode):
                _error("production_release_builder_cgroup_invalid")
            child, _child_identity = _open_child_directory(
                descriptor,
                name,
                expected_uid=authority_uid,
                expected_gid=authority_gid,
                allowed_modes=_CGROUP_DIRECTORY_MODES,
            )
            os.close(descriptor)
            descriptor = child
        return True
    finally:
        os.close(descriptor)


def _read_process_real_uid(
    process_path: Path,
    _directory_state: os.stat_result,
) -> int | None:
    """Read the kernel-reported real UID instead of trusting procfs ownership.

    Linux may deliberately present a nondumpable process directory as
    root-owned.  The first value in ``/proc/<pid>/status``'s ``Uid`` row is
    still the process real UID and is the authority used by this boundary.
    """

    descriptor: int | None = None
    try:
        descriptor = os.open(
            process_path / "status",
            _required_open_flags(),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(opened.st_mode)
            or opened.st_nlink != 1
        ):
            _error("production_release_builder_procfs_invalid")
        raw = os.read(descriptor, 256 * 1024 + 1)
        if len(raw) > 256 * 1024 or os.read(descriptor, 1):
            _error("production_release_builder_procfs_invalid")
    except FileNotFoundError:
        return None
    except ProductionReleaseBuilderError:
        raise
    except OSError as exc:
        _error("production_release_builder_procfs_unavailable", exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    uid_rows = [line for line in raw.splitlines() if line.startswith(b"Uid:")]
    if len(uid_rows) != 1:
        _error("production_release_builder_procfs_invalid")
    fields = uid_rows[0].split()
    if (
        len(fields) != 5
        or fields[0] != b"Uid:"
        or any(not item.isascii() or not item.isdigit() for item in fields[1:])
    ):
        _error("production_release_builder_procfs_invalid")
    return int(fields[1])


def _builder_processes(
    proc_root: Path,
    *,
    builder_uid: int,
    process_uid: Callable[[Path, os.stat_result], int | None] | None = None,
) -> tuple[int, ...]:
    try:
        root_state = os.lstat(proc_root)
        names = os.listdir(proc_root)
    except OSError as exc:
        _error("production_release_builder_procfs_unavailable", exc)
    if not stat.S_ISDIR(root_state.st_mode) or stat.S_ISLNK(root_state.st_mode):
        _error("production_release_builder_procfs_unavailable")
    result: list[int] = []
    for name in names:
        if not name.isascii() or not name.isdigit():
            continue
        pid = int(name)
        if pid <= 0:
            continue
        path = proc_root / name
        try:
            state = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            _error("production_release_builder_procfs_unavailable", exc)
        if stat.S_ISDIR(state.st_mode) and not stat.S_ISLNK(state.st_mode):
            uid_reader = (
                _read_process_real_uid if process_uid is None else process_uid
            )
            observed_uid = uid_reader(path, state)
            if observed_uid is None:
                continue
            if type(observed_uid) is not int or observed_uid < 0:
                _error("production_release_builder_procfs_invalid")
            if observed_uid == builder_uid:
                result.append(pid)
        else:
            _error("production_release_builder_procfs_invalid")
    return tuple(sorted(set(result)))


def validate_process_free_evidence(
    systemd_properties: Mapping[str, Any],
    *,
    expected_unit: str,
    expected_fragment: Path,
    expected_fragment_sha256: str,
    expected_wrapper: Path,
    expected_wrapper_sha256: str,
    expected_control_group: str,
    builder_uid: int,
    builder_gid: int,
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    proc_root: Path = Path("/proc"),
    authority_uid: int = 0,
    authority_gid: int = 0,
    process_uid: Callable[[Path, os.stat_result], int | None] | None = None,
    xattr_reader: XattrReader | None = None,
) -> Mapping[str, Any]:
    """Validate stopped-unit, recursively empty-cgroup, and UID-wide evidence."""

    expected_fragment = Path(expected_fragment)
    expected_wrapper = Path(expected_wrapper)
    cgroup_root = Path(cgroup_root)
    proc_root = Path(proc_root)
    if (
        builder_uid <= 0
        or builder_gid <= 0
        or authority_uid < 0
        or authority_gid < 0
        or builder_uid == authority_uid
        or _SHA256.fullmatch(expected_fragment_sha256) is None
        or _SHA256.fullmatch(expected_wrapper_sha256) is None
        or not expected_wrapper.is_absolute()
        or not cgroup_root.is_absolute()
        or not proc_root.is_absolute()
    ):
        _error("production_release_builder_process_evidence_contract_invalid")
    normalized = _normalized_systemd_properties(
        systemd_properties,
        expected_unit=expected_unit,
        expected_fragment=expected_fragment,
        expected_control_group=expected_control_group,
    )
    read_xattrs = (
        _read_descriptor_xattrs if xattr_reader is None else xattr_reader
    )
    with ExitStack() as stack:
        fragment = stack.enter_context(
            open_held_regular(
                expected_fragment,
                expected_uid=authority_uid,
                expected_gid=authority_gid,
                allowed_modes=frozenset({0o444}),
                maximum_bytes=1024 * 1024,
                expected_sha256=expected_fragment_sha256,
            )
        )
        wrapper = stack.enter_context(
            open_held_regular(
                expected_wrapper,
                expected_uid=authority_uid,
                expected_gid=authority_gid,
                allowed_modes=frozenset({0o555}),
                maximum_bytes=1024 * 1024,
                expected_sha256=expected_wrapper_sha256,
            )
        )
        _assert_no_xattrs(fragment.descriptor, xattr_reader=read_xattrs)
        _assert_no_xattrs(wrapper.descriptor, xattr_reader=read_xattrs)
        fragment.assert_stable()
        wrapper.assert_stable()

        first_builder_pids = _builder_processes(
            proc_root,
            builder_uid=builder_uid,
            process_uid=process_uid,
        )
        relative = PurePosixPath(expected_control_group)
        if (
            not relative.is_absolute()
            or relative.name != expected_unit
            or any(part in {"", ".", ".."} for part in relative.parts[1:])
        ):
            _error("production_release_builder_cgroup_invalid")
        physical_cgroup = cgroup_root.joinpath(*relative.parts[1:])
        cgroup_exists = _cgroup_address_exists(
            cgroup_root,
            relative,
            authority_uid=authority_uid,
            authority_gid=authority_gid,
        )
        if not cgroup_exists:
            inspected: tuple[str, ...] = ()
            cgroup_status = "removed"
        else:
            inspected = _scan_empty_cgroup_tree(
                physical_cgroup,
                authority_uid=authority_uid,
                authority_gid=authority_gid,
            )
            cgroup_status = "recursively-empty"
        second_builder_pids = _builder_processes(
            proc_root,
            builder_uid=builder_uid,
            process_uid=process_uid,
        )
        fragment.assert_stable()
        wrapper.assert_stable()
        _assert_no_xattrs(fragment.descriptor, xattr_reader=read_xattrs)
        _assert_no_xattrs(wrapper.descriptor, xattr_reader=read_xattrs)
    if first_builder_pids or second_builder_pids:
        _error("production_release_builder_uid_processes_present")
    unsigned = {
        "schema": PROCESS_FREE_EVIDENCE_SCHEMA,
        "unit": expected_unit,
        "fragment_path": str(expected_fragment),
        "fragment_sha256": expected_fragment_sha256,
        "drop_in_paths": [],
        "wrapper_path": str(expected_wrapper),
        "wrapper_sha256": expected_wrapper_sha256,
        "invocation_id": normalized["InvocationID"],
        "systemd_state": {
            "load": normalized["LoadState"],
            "active": normalized["ActiveState"],
            "sub": normalized["SubState"],
            "result": normalized["Result"],
            "main_pid": 0,
            "exec_main_pid": 0,
            "exec_main_code": normalized["ExecMainCode"],
            "exec_main_status": 0,
        },
        "control_group": expected_control_group,
        "cgroup_status": cgroup_status,
        "inspected_cgroups": list(inspected),
        "builder_uid": builder_uid,
        "builder_gid": builder_gid,
        "builder_uid_pids_before": [],
        "builder_uid_pids_after": [],
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {
        **unsigned,
        "evidence_sha256": _sha256_bytes(_canonical(unsigned)),
    }


def validate_process_free_evidence_record(
    value: Mapping[str, Any],
    *,
    builder_uid: int,
    builder_gid: int,
) -> Mapping[str, Any]:
    """Validate a previously collected process-free evidence record."""

    fields = frozenset({
        "schema",
        "unit",
        "fragment_path",
        "fragment_sha256",
        "drop_in_paths",
        "wrapper_path",
        "wrapper_sha256",
        "invocation_id",
        "systemd_state",
        "control_group",
        "cgroup_status",
        "inspected_cgroups",
        "builder_uid",
        "builder_gid",
        "builder_uid_pids_before",
        "builder_uid_pids_after",
        "secret_material_recorded",
        "secret_digest_recorded",
        "evidence_sha256",
    })
    if not isinstance(value, Mapping) or set(value) != fields:
        _error("production_release_builder_process_evidence_invalid")
    unsigned = {name: item for name, item in value.items() if name != "evidence_sha256"}
    state = value.get("systemd_state")
    inspected = value.get("inspected_cgroups")
    allowed_states = (
        {
            "load": "loaded",
            "active": "inactive",
            "sub": "dead",
            "result": "success",
            "main_pid": 0,
            "exec_main_pid": 0,
            "exec_main_code": "exited",
            "exec_main_status": 0,
        },
        {
            "load": "loaded",
            "active": "active",
            "sub": "exited",
            "result": "success",
            "main_pid": 0,
            "exec_main_pid": 0,
            "exec_main_code": "exited",
            "exec_main_status": 0,
        },
    )
    if (
        value.get("schema") != PROCESS_FREE_EVIDENCE_SCHEMA
        or _SYSTEMD_UNIT.fullmatch(str(value.get("unit"))) is None
        or not Path(str(value.get("fragment_path"))).is_absolute()
        or _SHA256.fullmatch(str(value.get("fragment_sha256"))) is None
        or value.get("drop_in_paths") != []
        or not Path(str(value.get("wrapper_path"))).is_absolute()
        or _SHA256.fullmatch(str(value.get("wrapper_sha256"))) is None
        or _INVOCATION_ID.fullmatch(str(value.get("invocation_id"))) is None
        or not str(value.get("control_group")).startswith("/")
        or value.get("cgroup_status") not in {"removed", "recursively-empty"}
        or not isinstance(inspected, list)
        or any(not isinstance(item, str) or not item for item in inspected)
        or value.get("builder_uid") != builder_uid
        or value.get("builder_gid") != builder_gid
        or value.get("builder_uid_pids_before") != []
        or value.get("builder_uid_pids_after") != []
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
        or not isinstance(state, Mapping)
        or state not in allowed_states
        or _SHA256.fullmatch(str(value.get("evidence_sha256"))) is None
        or value.get("evidence_sha256") != _sha256_bytes(_canonical(unsigned))
    ):
        _error("production_release_builder_process_evidence_invalid")
    return dict(value)


def build_process_free_evidence_set(
    initial: Mapping[str, Any],
    final: Mapping[str, Any],
    *,
    builder_uid: int,
    builder_gid: int,
) -> Mapping[str, Any]:
    """Bind both fresh observations used for one root publication."""

    first = validate_process_free_evidence_record(
        initial,
        builder_uid=builder_uid,
        builder_gid=builder_gid,
    )
    second = validate_process_free_evidence_record(
        final,
        builder_uid=builder_uid,
        builder_gid=builder_gid,
    )
    stable_fields = (
        "unit",
        "fragment_path",
        "fragment_sha256",
        "drop_in_paths",
        "wrapper_path",
        "wrapper_sha256",
        "invocation_id",
        "systemd_state",
        "control_group",
        "builder_uid",
        "builder_gid",
    )
    if any(first[name] != second[name] for name in stable_fields):
        _error("production_release_builder_process_evidence_changed")
    unsigned = {
        "schema": PROCESS_FREE_EVIDENCE_SET_SCHEMA,
        "initial": first,
        "final": second,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {
        **unsigned,
        "evidence_sha256": _sha256_bytes(_canonical(unsigned)),
    }


def validate_process_free_evidence_set_record(
    value: Mapping[str, Any],
    *,
    builder_uid: int,
    builder_gid: int,
) -> Mapping[str, Any]:
    """Validate the two-observation publication evidence set."""

    fields = frozenset({
        "schema",
        "initial",
        "final",
        "secret_material_recorded",
        "secret_digest_recorded",
        "evidence_sha256",
    })
    if not isinstance(value, Mapping) or set(value) != fields:
        _error("production_release_builder_process_evidence_set_invalid")
    if (
        value.get("schema") != PROCESS_FREE_EVIDENCE_SET_SCHEMA
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
    ):
        _error("production_release_builder_process_evidence_set_invalid")
    initial = value.get("initial")
    final = value.get("final")
    if not isinstance(initial, Mapping) or not isinstance(final, Mapping):
        _error("production_release_builder_process_evidence_set_invalid")
    expected = build_process_free_evidence_set(
        initial,
        final,
        builder_uid=builder_uid,
        builder_gid=builder_gid,
    )
    if dict(value) != expected:
        _error("production_release_builder_process_evidence_set_invalid")
    return dict(value)


@dataclass
class _TreeAccumulator:
    entries: list[Mapping[str, Any]]
    total_bytes: int = 0

    def add(self, entry: Mapping[str, Any], *, size: int = 0) -> None:
        self.entries.append(dict(entry))
        self.total_bytes += size
        if (
            len(self.entries) > MAX_RELEASE_ENTRIES
            or self.total_bytes > MAX_RELEASE_BYTES
        ):
            _error("production_release_builder_release_oversized")


def _identity_allowed_for_payload(
    identity: FileIdentity,
    *,
    directory: bool,
    staging_uid: int,
    staging_gid: int,
    publication_uid: int,
    publication_gid: int,
) -> str:
    owner = (identity.uid, identity.gid)
    mode = stat.S_IMODE(identity.mode)
    publication_modes = (
        frozenset({_SEALED_DIRECTORY_MODE}) if directory else _SEALED_FILE_MODES
    )
    if owner == (publication_uid, publication_gid) and mode in publication_modes:
        return "publication"
    if owner == (staging_uid, staging_gid):
        allowed = _BUILDER_DIRECTORY_MODES if directory else _BUILDER_FILE_MODES
        if mode not in allowed or mode & 0o022:
            _error("production_release_builder_release_mode_invalid")
        return "staging"
    if owner == (publication_uid, publication_gid):
        _error("production_release_builder_release_mode_invalid")
    _error("production_release_builder_release_owner_invalid")


def _open_payload_directory_at(
    parent_descriptor: int,
    name: str,
    *,
    staging_uid: int,
    staging_gid: int,
    publication_uid: int,
    publication_gid: int,
) -> tuple[int, FileIdentity, str]:
    _validate_simple_name(name)
    descriptor: int | None = None
    try:
        before = _stat_at(parent_descriptor, name)
        owner_state = _identity_allowed_for_payload(
            before,
            directory=True,
            staging_uid=staging_uid,
            staging_gid=staging_gid,
            publication_uid=publication_uid,
            publication_gid=publication_gid,
        )
        descriptor = os.open(
            name,
            _required_open_flags(directory=True),
            dir_fd=parent_descriptor,
        )
        opened = FileIdentity.from_stat(os.fstat(descriptor))
        after = _stat_at(parent_descriptor, name)
        if (
            before != opened
            or opened != after
            or not stat.S_ISDIR(opened.mode)
            or stat.S_ISLNK(opened.mode)
        ):
            _error("production_release_builder_release_tree_changed")
        return descriptor, opened, owner_state
    except ProductionReleaseBuilderError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        _error("production_release_builder_release_tree_unavailable", exc)


def _seal_regular_payload_at(
    parent_descriptor: int,
    name: str,
    relative: str,
    *,
    staging_uid: int,
    staging_gid: int,
    publication_uid: int,
    publication_gid: int,
    xattr_reader: XattrReader,
) -> tuple[Mapping[str, Any], int]:
    _validate_simple_name(name)
    descriptor: int | None = None
    try:
        before = _stat_at(parent_descriptor, name)
        owner_state = _identity_allowed_for_payload(
            before,
            directory=False,
            staging_uid=staging_uid,
            staging_gid=staging_gid,
            publication_uid=publication_uid,
            publication_gid=publication_gid,
        )
        if (
            not stat.S_ISREG(before.mode)
            or stat.S_ISLNK(before.mode)
            or before.links != 1
            or before.size < 0
        ):
            _error("production_release_builder_release_entry_invalid")
        descriptor = os.open(
            name,
            _required_open_flags(),
            dir_fd=parent_descriptor,
        )
        opened = FileIdentity.from_stat(os.fstat(descriptor))
        if before != opened:
            _error("production_release_builder_release_tree_changed")
        _assert_no_xattrs(descriptor, xattr_reader=xattr_reader)
        digest = _hash_descriptor(descriptor, size=opened.size)
        unchanged = FileIdentity.from_stat(os.fstat(descriptor))
        reachable = _stat_at(parent_descriptor, name)
        if opened != unchanged or opened != reachable:
            _error("production_release_builder_release_tree_changed")
        final_mode = 0o555 if stat.S_IMODE(opened.mode) & 0o111 else 0o444
        if owner_state == "staging":
            os.fchown(descriptor, publication_uid, publication_gid)
            os.fchmod(descriptor, final_mode)
            os.fsync(descriptor)
        final = FileIdentity.from_stat(os.fstat(descriptor))
        reachable_final = _stat_at(parent_descriptor, name)
        _assert_no_xattrs(descriptor, xattr_reader=xattr_reader)
        if (
            final != reachable_final
            or final.uid != publication_uid
            or final.gid != publication_gid
            or final.links != 1
            or stat.S_IMODE(final.mode) != final_mode
            or final.size != opened.size
            or _hash_descriptor(descriptor, size=final.size) != digest
        ):
            _error("production_release_builder_release_seal_failed")
        return (
            {
                "path": relative,
                "kind": "file",
                "mode": f"{final_mode:04o}",
                "uid": publication_uid,
                "gid": publication_gid,
                "size": final.size,
                "sha256": digest,
                "xattrs": [],
            },
            final.size,
        )
    except ProductionReleaseBuilderError:
        raise
    except OSError as exc:
        _error("production_release_builder_release_seal_failed", exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _seal_payload_directory_contents(
    descriptor: int,
    relative: PurePosixPath,
    *,
    staging_uid: int,
    staging_gid: int,
    publication_uid: int,
    publication_gid: int,
    accumulator: _TreeAccumulator,
    excluded_root_names: frozenset[str],
    xattr_reader: XattrReader,
) -> None:
    _assert_no_xattrs(descriptor, xattr_reader=xattr_reader)
    try:
        names = sorted(os.listdir(descriptor))
    except OSError as exc:
        _error("production_release_builder_release_tree_unavailable", exc)
    if str(relative) == ".":
        if excluded_root_names:
            names_to_process = [
                name for name in names if name not in excluded_root_names
            ]
        else:
            if any(name in _RESERVED_ROOT_NAMES for name in names):
                _error("production_release_builder_terminal_record_exists")
            names_to_process = names
    else:
        names_to_process = names
    for name in names_to_process:
        _validate_simple_name(name)
        state = _stat_at(descriptor, name)
        path = name if str(relative) == "." else (relative / name).as_posix()
        if stat.S_ISDIR(state.mode) and not stat.S_ISLNK(state.mode):
            child, initial, owner_state = _open_payload_directory_at(
                descriptor,
                name,
                staging_uid=staging_uid,
                staging_gid=staging_gid,
                publication_uid=publication_uid,
                publication_gid=publication_gid,
            )
            try:
                _seal_payload_directory_contents(
                    child,
                    PurePosixPath(path),
                    staging_uid=staging_uid,
                    staging_gid=staging_gid,
                    publication_uid=publication_uid,
                    publication_gid=publication_gid,
                    accumulator=accumulator,
                    excluded_root_names=frozenset(),
                    xattr_reader=xattr_reader,
                )
                try:
                    child_names_after = sorted(os.listdir(child))
                except OSError as exc:
                    _error(
                        "production_release_builder_release_tree_unavailable",
                        exc,
                    )
                if owner_state == "staging":
                    os.fchown(child, publication_uid, publication_gid)
                    os.fchmod(child, _SEALED_DIRECTORY_MODE)
                    os.fsync(child)
                final = FileIdentity.from_stat(os.fstat(child))
                reachable = _stat_at(descriptor, name)
                _assert_no_xattrs(child, xattr_reader=xattr_reader)
                if (
                    final != reachable
                    or final.uid != publication_uid
                    or final.gid != publication_gid
                    or stat.S_IMODE(final.mode) != _SEALED_DIRECTORY_MODE
                    or child_names_after != sorted(os.listdir(child))
                ):
                    _error("production_release_builder_release_seal_failed")
            finally:
                os.close(child)
            accumulator.add({
                "path": path,
                "kind": "directory",
                "mode": f"{_SEALED_DIRECTORY_MODE:04o}",
                "uid": publication_uid,
                "gid": publication_gid,
                "xattrs": [],
            })
        elif stat.S_ISREG(state.mode) and not stat.S_ISLNK(state.mode):
            entry, size = _seal_regular_payload_at(
                descriptor,
                name,
                path,
                staging_uid=staging_uid,
                staging_gid=staging_gid,
                publication_uid=publication_uid,
                publication_gid=publication_gid,
                xattr_reader=xattr_reader,
            )
            accumulator.add(entry, size=size)
        else:
            _error("production_release_builder_release_entry_invalid")
    try:
        final_names = sorted(os.listdir(descriptor))
    except OSError as exc:
        _error("production_release_builder_release_tree_unavailable", exc)
    if final_names != names:
        _error("production_release_builder_release_tree_changed")
    _assert_no_xattrs(descriptor, xattr_reader=xattr_reader)


def _write_record_at(
    directory_descriptor: int,
    name: str,
    value: Mapping[str, Any],
    *,
    publication_uid: int,
    publication_gid: int,
    xattr_reader: XattrReader,
) -> str:
    _validate_simple_name(name)
    payload = _canonical(value) + b"\n"
    if not 0 < len(payload) <= MAX_RECORD_BYTES:
        _error("production_release_builder_record_oversized")
    descriptor: int | None = None
    created = False
    try:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        descriptor = os.open(name, flags, 0o400, dir_fd=directory_descriptor)
        created = True
        _assert_no_xattrs(descriptor, xattr_reader=xattr_reader)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                _error("production_release_builder_record_write_failed")
            view = view[written:]
        os.fchown(descriptor, publication_uid, publication_gid)
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
        state = FileIdentity.from_stat(os.fstat(descriptor))
        reachable = _stat_at(directory_descriptor, name)
        digest = _hash_descriptor(descriptor, size=state.size)
        _assert_no_xattrs(descriptor, xattr_reader=xattr_reader)
        if (
            state != reachable
            or not stat.S_ISREG(state.mode)
            or state.links != 1
            or state.uid != publication_uid
            or state.gid != publication_gid
            or stat.S_IMODE(state.mode) != 0o444
            or state.size != len(payload)
            or digest != _sha256_bytes(payload)
        ):
            _error("production_release_builder_record_write_failed")
        return digest
    except ProductionReleaseBuilderError:
        if created:
            _unlink_created_if_still_bound(
                directory_descriptor,
                name,
                descriptor,
            )
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        raise
    except OSError as exc:
        if created:
            _unlink_created_if_still_bound(
                directory_descriptor,
                name,
                descriptor,
            )
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        _error("production_release_builder_record_write_failed", exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _identities_record(identities: ReleaseIdentities) -> Mapping[str, Any]:
    return {
        "release_owner": {
            "uid": identities.root_uid,
            "gid": identities.root_gid,
        },
        "builder_identity": {
            "user": "muncho-release-builder",
            "group": "muncho-release-builder",
            "uid": identities.builder_uid,
            "gid": identities.builder_gid,
        },
        "reserved_runtime_uids": list(identities.reserved_runtime_uids),
        "reserved_runtime_gids": list(identities.reserved_runtime_gids),
    }


def _validate_identities_record(value: Any) -> ReleaseIdentities:
    if (
        not isinstance(value, Mapping)
        or set(value)
        != {
            "release_owner",
            "builder_identity",
            "reserved_runtime_uids",
            "reserved_runtime_gids",
        }
        or not isinstance(value.get("release_owner"), Mapping)
        or set(value["release_owner"]) != {"uid", "gid"}
        or not isinstance(value.get("builder_identity"), Mapping)
        or set(value["builder_identity"]) != {"user", "group", "uid", "gid"}
        or value["builder_identity"].get("user") != "muncho-release-builder"
        or value["builder_identity"].get("group") != "muncho-release-builder"
        or not isinstance(value.get("reserved_runtime_uids"), list)
        or not isinstance(value.get("reserved_runtime_gids"), list)
    ):
        _error("production_release_builder_identity_record_invalid")
    try:
        identities = ReleaseIdentities(
            root_uid=value["release_owner"]["uid"],
            root_gid=value["release_owner"]["gid"],
            builder_uid=value["builder_identity"]["uid"],
            builder_gid=value["builder_identity"]["gid"],
            reserved_runtime_uids=tuple(value["reserved_runtime_uids"]),
            reserved_runtime_gids=tuple(value["reserved_runtime_gids"]),
        )
    except (KeyError, TypeError) as exc:
        _error("production_release_builder_identity_record_invalid", exc)
    return validate_release_identities(identities)


def _publish_release_filesystem(
    release_root: Path,
    *,
    revision: str,
    identities: ReleaseIdentities,
    process_free_evidence: Mapping[str, Any],
    staging_uid: int,
    staging_gid: int,
    publication_uid: int,
    publication_gid: int,
    checkpoint: Callable[[str], None] | None = None,
    _xattr_reader: XattrReader | None = None,
) -> Mapping[str, Any]:
    """Low-level real-filesystem publisher.

    The production promoter is the only authority that may call this private
    primitive.  Dependency injection exists solely so the exact inode and
    mutation behavior can be exercised on an unprivileged test filesystem.
    """

    release_root = Path(release_root)
    if (
        not release_root.is_absolute()
        or release_root.parent == release_root
        or _REVISION.fullmatch(revision) is None
        or min(staging_uid, staging_gid, publication_uid, publication_gid) < 0
    ):
        _error("production_release_builder_publication_contract_invalid")
    _validate_simple_name(release_root.name)
    xattr_reader = _read_descriptor_xattrs if _xattr_reader is None else _xattr_reader
    evidence = validate_process_free_evidence_set_record(
        process_free_evidence,
        builder_uid=identities.builder_uid,
        builder_gid=identities.builder_gid,
    )
    parent_descriptor, parent_initial = _open_directory_path(
        release_root.parent,
        expected_uid=publication_uid,
        expected_gid=publication_gid,
        allowed_modes=frozenset({0o555, 0o700, 0o750, 0o755}),
    )
    root_descriptor: int | None = None
    try:
        root_descriptor, root_initial, root_owner_state = _open_payload_directory_at(
            parent_descriptor,
            release_root.name,
            staging_uid=staging_uid,
            staging_gid=staging_gid,
            publication_uid=publication_uid,
            publication_gid=publication_gid,
        )
        if root_owner_state != "staging":
            _error("production_release_builder_candidate_not_staged")
        accumulator = _TreeAccumulator(entries=[])
        _seal_payload_directory_contents(
            root_descriptor,
            PurePosixPath("."),
            staging_uid=staging_uid,
            staging_gid=staging_gid,
            publication_uid=publication_uid,
            publication_gid=publication_gid,
            accumulator=accumulator,
            excluded_root_names=frozenset(),
            xattr_reader=xattr_reader,
        )
        entries = sorted(accumulator.entries, key=lambda item: str(item["path"]))
        if len({str(item["path"]) for item in entries}) != len(entries):
            _error("production_release_builder_release_entry_invalid")
        tree_sha256 = _sha256_bytes(_canonical(entries))
        os.fchown(root_descriptor, publication_uid, publication_gid)
        os.fchmod(root_descriptor, 0o700)
        _assert_no_xattrs(root_descriptor, xattr_reader=xattr_reader)
        manifest_unsigned = {
            "schema": MANIFEST_SCHEMA,
            "release_revision": revision,
            "release_root": str(release_root),
            "identities": _identities_record(identities),
            "process_free_evidence": evidence,
            "process_free_evidence_sha256": evidence["evidence_sha256"],
            "payload_entries": entries,
            "payload_entry_count": len(entries),
            "payload_bytes": accumulator.total_bytes,
            "payload_tree_sha256": tree_sha256,
            "physical_root_uid": publication_uid,
            "physical_root_gid": publication_gid,
            "root_xattrs": [],
            "secret_material_recorded": False,
            "secret_digest_recorded": False,
        }
        manifest = {
            **manifest_unsigned,
            "manifest_sha256": _sha256_bytes(_canonical(manifest_unsigned)),
        }
        manifest_file_sha256 = _write_record_at(
            root_descriptor,
            MANIFEST_NAME,
            manifest,
            publication_uid=publication_uid,
            publication_gid=publication_gid,
            xattr_reader=xattr_reader,
        )
        if checkpoint is not None:
            checkpoint("manifest_written")
        receipt_unsigned = {
            "schema": RECEIPT_SCHEMA,
            "release_revision": revision,
            "release_root": str(release_root),
            "manifest_name": MANIFEST_NAME,
            "manifest_sha256": manifest["manifest_sha256"],
            "manifest_file_sha256": manifest_file_sha256,
            "payload_tree_sha256": tree_sha256,
            "payload_entry_count": len(entries),
            "process_free_evidence": evidence,
            "process_free_evidence_sha256": evidence["evidence_sha256"],
            "root_uid": publication_uid,
            "root_gid": publication_gid,
            "root_mode": f"{_SEALED_DIRECTORY_MODE:04o}",
            "root_xattrs": [],
            "terminal": True,
            "secret_material_recorded": False,
            "secret_digest_recorded": False,
        }
        receipt = {
            **receipt_unsigned,
            "receipt_sha256": _sha256_bytes(_canonical(receipt_unsigned)),
        }
        _write_record_at(
            root_descriptor,
            RECEIPT_NAME,
            receipt,
            publication_uid=publication_uid,
            publication_gid=publication_gid,
            xattr_reader=xattr_reader,
        )
        if checkpoint is not None:
            checkpoint("terminal_receipt_written")
        os.fchmod(root_descriptor, _SEALED_DIRECTORY_MODE)
        os.fsync(root_descriptor)
        _assert_no_xattrs(root_descriptor, xattr_reader=xattr_reader)
        os.fsync(parent_descriptor)
        root_final = FileIdentity.from_stat(os.fstat(root_descriptor))
        root_reachable = _stat_at(parent_descriptor, release_root.name)
        parent_final = FileIdentity.from_stat(os.fstat(parent_descriptor))
        root_external = FileIdentity.from_stat(os.lstat(release_root))
        parent_external = FileIdentity.from_stat(os.lstat(release_root.parent))
        if (
            root_final != root_reachable
            or root_final != root_external
            or _directory_binding(parent_initial) != _directory_binding(parent_final)
            or _directory_binding(parent_initial) != _directory_binding(parent_external)
            or root_final.uid != publication_uid
            or root_final.gid != publication_gid
            or stat.S_IMODE(root_final.mode) != _SEALED_DIRECTORY_MODE
            or root_final.device != root_initial.device
            or root_final.inode != root_initial.inode
        ):
            _error("production_release_builder_publication_changed")
        return receipt
    except ProductionReleaseBuilderError:
        raise
    except OSError as exc:
        _error("production_release_builder_publication_failed", exc)
    finally:
        if root_descriptor is not None:
            os.close(root_descriptor)
        os.close(parent_descriptor)


def _publish_root_owned_release(
    release_root: Path,
    *,
    revision: str,
    identities: ReleaseIdentities,
    process_free_evidence: Mapping[str, Any],
    checkpoint: Callable[[str], None] | None = None,
) -> Mapping[str, Any]:
    """Private root-only publisher used by the fixed production promoter."""

    validated = validate_release_identities(
        identities,
        require_effective_root=True,
    )
    return _publish_release_filesystem(
        release_root,
        revision=revision,
        identities=validated,
        process_free_evidence=process_free_evidence,
        staging_uid=validated.builder_uid,
        staging_gid=validated.builder_gid,
        publication_uid=validated.root_uid,
        publication_gid=validated.root_gid,
        checkpoint=checkpoint,
    )


def _read_record_at(
    directory_descriptor: int,
    root: Path,
    name: str,
    *,
    expected_uid: int,
    expected_gid: int,
    xattr_reader: XattrReader,
) -> tuple[Mapping[str, Any], str]:
    path = root / name
    with open_held_regular(
        path,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        allowed_modes=frozenset({0o444}),
        maximum_bytes=MAX_RECORD_BYTES,
    ) as held:
        _assert_no_xattrs(held.descriptor, xattr_reader=xattr_reader)
        try:
            raw = os.pread(held.descriptor, held.identity.size, 0)
        except OSError as exc:
            _error("production_release_builder_record_unavailable", exc)
        held.assert_stable()
        _assert_no_xattrs(held.descriptor, xattr_reader=xattr_reader)
        reachable = _stat_at(directory_descriptor, name)
        if reachable != held.identity:
            _error("production_release_builder_record_changed")
        return (
            _decode_canonical_line(raw, maximum=MAX_RECORD_BYTES),
            held.sha256,
        )


def _verify_published_release_filesystem(
    release_root: Path,
    *,
    revision: str,
    expected_uid: int,
    expected_gid: int,
    require_logical_owner: bool,
    _xattr_reader: XattrReader | None = None,
) -> Mapping[str, Any]:
    """Verify the complete sealed payload, manifest, and terminal receipt."""

    release_root = Path(release_root)
    if (
        not release_root.is_absolute()
        or _REVISION.fullmatch(revision) is None
        or expected_uid < 0
        or expected_gid < 0
    ):
        _error("production_release_builder_verification_contract_invalid")
    xattr_reader = _read_descriptor_xattrs if _xattr_reader is None else _xattr_reader
    root_descriptor, root_identity = _open_directory_path(
        release_root,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        allowed_modes=frozenset({_SEALED_DIRECTORY_MODE}),
    )
    try:
        _assert_no_xattrs(root_descriptor, xattr_reader=xattr_reader)
        manifest, manifest_file_sha256 = _read_record_at(
            root_descriptor,
            release_root,
            MANIFEST_NAME,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            xattr_reader=xattr_reader,
        )
        receipt, _receipt_file_sha256 = _read_record_at(
            root_descriptor,
            release_root,
            RECEIPT_NAME,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            xattr_reader=xattr_reader,
        )
        manifest_unsigned = {
            name: item for name, item in manifest.items() if name != "manifest_sha256"
        }
        receipt_unsigned = {
            name: item for name, item in receipt.items() if name != "receipt_sha256"
        }
        manifest_fields = frozenset({
            "schema",
            "release_revision",
            "release_root",
            "identities",
            "process_free_evidence",
            "process_free_evidence_sha256",
            "payload_entries",
            "payload_entry_count",
            "payload_bytes",
            "payload_tree_sha256",
            "physical_root_uid",
            "physical_root_gid",
            "root_xattrs",
            "secret_material_recorded",
            "secret_digest_recorded",
            "manifest_sha256",
        })
        receipt_fields = frozenset({
            "schema",
            "release_revision",
            "release_root",
            "manifest_name",
            "manifest_sha256",
            "manifest_file_sha256",
            "payload_tree_sha256",
            "payload_entry_count",
            "process_free_evidence",
            "process_free_evidence_sha256",
            "root_uid",
            "root_gid",
            "root_mode",
            "root_xattrs",
            "terminal",
            "secret_material_recorded",
            "secret_digest_recorded",
            "receipt_sha256",
        })
        entries = manifest.get("payload_entries")
        logical_identities = _validate_identities_record(manifest.get("identities"))
        process_evidence_record = manifest.get("process_free_evidence")
        if not isinstance(process_evidence_record, Mapping):
            _error("production_release_builder_publication_record_invalid")
        process_evidence = validate_process_free_evidence_set_record(
            process_evidence_record,
            builder_uid=logical_identities.builder_uid,
            builder_gid=logical_identities.builder_gid,
        )
        if (
            set(manifest) != manifest_fields
            or manifest.get("schema") != MANIFEST_SCHEMA
            or manifest.get("release_revision") != revision
            or manifest.get("release_root") != str(release_root)
            or manifest.get("physical_root_uid") != expected_uid
            or manifest.get("physical_root_gid") != expected_gid
            or manifest.get("root_xattrs") != []
            or (
                require_logical_owner
                and (
                    logical_identities.root_uid != expected_uid
                    or logical_identities.root_gid != expected_gid
                )
            )
            or _SHA256.fullmatch(str(manifest.get("process_free_evidence_sha256")))
            is None
            or manifest.get("process_free_evidence_sha256")
            != process_evidence.get("evidence_sha256")
            or not isinstance(entries, list)
            or manifest.get("payload_entry_count") != len(entries)
            or type(manifest.get("payload_bytes")) is not int
            or manifest.get("payload_bytes", -1) < 0
            or manifest.get("payload_tree_sha256") != _sha256_bytes(_canonical(entries))
            or manifest.get("secret_material_recorded") is not False
            or manifest.get("secret_digest_recorded") is not False
            or manifest.get("manifest_sha256")
            != _sha256_bytes(_canonical(manifest_unsigned))
            or set(receipt) != receipt_fields
            or receipt.get("schema") != RECEIPT_SCHEMA
            or receipt.get("release_revision") != revision
            or receipt.get("release_root") != str(release_root)
            or receipt.get("manifest_name") != MANIFEST_NAME
            or receipt.get("manifest_sha256") != manifest.get("manifest_sha256")
            or receipt.get("manifest_file_sha256") != manifest_file_sha256
            or receipt.get("payload_tree_sha256") != manifest.get("payload_tree_sha256")
            or receipt.get("payload_entry_count") != manifest.get("payload_entry_count")
            or receipt.get("process_free_evidence")
            != manifest.get("process_free_evidence")
            or receipt.get("process_free_evidence_sha256")
            != manifest.get("process_free_evidence_sha256")
            or receipt.get("root_uid") != expected_uid
            or receipt.get("root_gid") != expected_gid
            or receipt.get("root_mode") != f"{_SEALED_DIRECTORY_MODE:04o}"
            or receipt.get("root_xattrs") != []
            or receipt.get("terminal") is not True
            or receipt.get("secret_material_recorded") is not False
            or receipt.get("secret_digest_recorded") is not False
            or receipt.get("receipt_sha256")
            != _sha256_bytes(_canonical(receipt_unsigned))
        ):
            _error("production_release_builder_publication_record_invalid")
        accumulator = _TreeAccumulator(entries=[])
        _seal_payload_directory_contents(
            root_descriptor,
            PurePosixPath("."),
            staging_uid=expected_uid,
            staging_gid=expected_gid,
            publication_uid=expected_uid,
            publication_gid=expected_gid,
            accumulator=accumulator,
            excluded_root_names=_RESERVED_ROOT_NAMES,
            xattr_reader=xattr_reader,
        )
        observed_entries = sorted(
            accumulator.entries,
            key=lambda item: str(item["path"]),
        )
        final_root = FileIdentity.from_stat(os.fstat(root_descriptor))
        _assert_no_xattrs(root_descriptor, xattr_reader=xattr_reader)
        if (
            observed_entries != entries
            or accumulator.total_bytes != manifest["payload_bytes"]
            or _sha256_bytes(_canonical(observed_entries))
            != manifest["payload_tree_sha256"]
            or final_root != root_identity
        ):
            _error("production_release_builder_published_tree_changed")
        return dict(receipt)
    finally:
        os.close(root_descriptor)


def verify_published_release(
    release_root: Path,
    *,
    revision: str,
) -> Mapping[str, Any]:
    """Verify one immutable root-owned production release."""

    return _verify_published_release_filesystem(
        release_root,
        revision=revision,
        expected_uid=0,
        expected_gid=0,
        require_logical_owner=True,
    )


__all__ = [
    "GitTreeEntry",
    "HeldRegularFile",
    "MANIFEST_NAME",
    "MANIFEST_SCHEMA",
    "PROCESS_FREE_EVIDENCE_SCHEMA",
    "PROCESS_FREE_EVIDENCE_SET_SCHEMA",
    "ProductionReleaseBuilderError",
    "RECEIPT_NAME",
    "RECEIPT_SCHEMA",
    "ReleaseIdentities",
    "materialize_git_tree",
    "open_held_regular",
    "parse_git_tree",
    "build_process_free_evidence_set",
    "retain_verified_wheel",
    "validate_process_free_evidence",
    "validate_process_free_evidence_record",
    "validate_process_free_evidence_set_record",
    "validate_release_identities",
    "verify_published_release",
]
