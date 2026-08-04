#!/usr/bin/env python3
"""Fail-closed receipt-driven GC for stopped Muncho canary releases.

Planning is read-only.  Apply requires the exact approved plan digest and runs
under the same host lifecycle lock as release build, publication, and writer
activation.  Evidence is append-only: every mutation is preceded by a durable
intent, logical deletion is receipted after no-replace tombstone moves, and a
crash may resume physical purge idempotently.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    ContextManager,
    Iterable,
    Iterator,
    Mapping,
    NoReturn,
    Sequence,
)

from gateway.canonical_writer_lifecycle_lock import host_release_lifecycle_lock


PLAN_SCHEMA = "muncho-canary-receipt-driven-gc-plan.v2"
RESULT_SCHEMA = "muncho-canary-receipt-driven-gc-result.v2"
PROTECTION_INVENTORY_SCHEMA = "muncho-canary-gc-protection-inventory.v1"
INTENT_SCHEMA = "muncho-canary-gc-delete-intent.v1"
LOGICAL_DELETE_SCHEMA = "muncho-canary-gc-logical-delete.v2"
PURGE_SCHEMA = "muncho-canary-gc-physical-purge.v1"
PLAN_ANCHOR_SCHEMA = "muncho-canary-gc-approved-plan-anchor.v1"
STOPPED_RECEIPT_SCHEMA = "muncho-canary-stopped-release-publication.v1"
RELEASE_MANIFEST_SCHEMA = "muncho-writer-only-release.v1"
FORK_REPOSITORY = "https://github.com/lomliev/hermes-agent.git"

DEFAULT_RELEASE_BASE = Path("/opt/muncho-canary-releases")
DEFAULT_SOURCE_BASE = Path("/opt/muncho-canary-source")
DEFAULT_EVIDENCE_BASE = Path("/var/lib/muncho-canary-release-evidence")

RECEIPT_NAME = "stopped-release-publication.json"
MANIFEST_NAME = "release-manifest.json"
INTENT_NAME = "gc-delete-intent.json"
LOGICAL_DELETE_NAME = "gc-logical-delete.json"
PURGE_NAME = "gc-physical-purge.json"
PLAN_ANCHOR_PREFIX = ".gc-approved-plan-"
TERMINAL_RETENTION_COUNT = 3
MAX_JSON_BYTES = 8 * 1024 * 1024
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class GCLayout:
    release_base: Path = DEFAULT_RELEASE_BASE
    source_base: Path = DEFAULT_SOURCE_BASE
    evidence_base: Path = DEFAULT_EVIDENCE_BASE
    current_links: tuple[Path, ...] = ()
    previous_links: tuple[Path, ...] = ()
    protected_refs: tuple[Path, ...] = ()
    protection_inventory_path: Path | None = None
    protection_inventory_file_sha256: str | None = None
    trusted_uid: int = 0
    trusted_gid: int = 0
    validate_parent_chain: bool = True

    def validate(self) -> None:
        roots = (self.release_base, self.source_base, self.evidence_base)
        if any(not path.is_absolute() or ".." in path.parts for path in roots):
            raise ValueError("GC roots must be absolute normalized paths")
        if len(set(roots)) != len(roots):
            raise ValueError("GC roots must be distinct")
        if type(self.trusted_uid) is not int or self.trusted_uid < 0:
            raise ValueError("GC trusted uid is invalid")
        if type(self.trusted_gid) is not int or self.trusted_gid < 0:
            raise ValueError("GC trusted gid is invalid")
        paths = (*self.current_links, *self.previous_links, *self.protected_refs)
        if any(not path.is_absolute() or ".." in path.parts for path in paths):
            raise ValueError("GC protection paths must be absolute normalized paths")
        if self.protection_inventory_path is not None:
            if (
                not self.protection_inventory_path.is_absolute()
                or ".." in self.protection_inventory_path.parts
            ):
                raise ValueError("GC protection inventory path must be absolute")
            if not self.protection_inventory_file_sha256:
                raise ValueError("GC protection inventory digest is required")


@dataclass(frozen=True)
class _PinnedRoot:
    path: Path
    fd: int
    identity: tuple[int, ...]
    mount_id: str


@dataclass(frozen=True)
class _TreeEntry:
    identity: tuple[int, ...]
    tree_sha256: str
    mount_id: str


@dataclass(frozen=True)
class _ReceiptState:
    status: str
    created_at_unix: int | None = None
    file_sha256: str | None = None
    producer_anchor_sha256: str | None = None


def _root_kwargs(layout: GCLayout) -> dict[str, int | bool]:
    return {
        "trusted_uid": layout.trusted_uid,
        "trusted_gid": layout.trusted_gid,
        "validate_parent_chain": layout.validate_parent_chain,
    }


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256_json(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _require_sha(value: str, *, label: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be one exact 40-character SHA")
    return value


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _directory_anchor(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
    )


def _mount_id(fd: int) -> str:
    try:
        raw = Path(f"/proc/self/fdinfo/{fd}").read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        if sys.platform.startswith("linux"):
            raise RuntimeError("mount identity is unavailable") from exc
        return f"dev:{os.fstat(fd).st_dev}"
    for line in raw.splitlines():
        if line.startswith("mnt_id:\t"):
            value = line.partition("\t")[2]
            if value.isdecimal():
                return f"mnt:{value}"
    raise RuntimeError("mount identity is unavailable")


def _list_xattrs(path: Path) -> tuple[str, ...]:
    lister = getattr(os, "listxattr", None)
    if not callable(lister):
        if sys.platform.startswith("linux"):
            raise RuntimeError("GC xattr inspection is unavailable")
        return ()
    return tuple(lister(path, follow_symlinks=False))


def _validate_trusted_parent_chain(
    path: Path,
    *,
    trusted_uid: int,
    trusted_gid: int,
) -> None:
    current = path
    while True:
        reached = os.lstat(current)
        if (
            stat.S_ISLNK(reached.st_mode)
            or not stat.S_ISDIR(reached.st_mode)
            or reached.st_uid != trusted_uid
            or reached.st_gid != trusted_gid
            or stat.S_IMODE(reached.st_mode) & 0o022
            or _list_xattrs(current)
        ):
            raise PermissionError("GC parent chain is not trusted")
        if current == current.parent:
            return
        current = current.parent


@contextmanager
def _open_root(
    path: Path,
    *,
    label: str,
    trusted_uid: int = 0,
    trusted_gid: int = 0,
    validate_parent_chain: bool = True,
) -> Iterator[_PinnedRoot]:
    if validate_parent_chain:
        _validate_trusted_parent_chain(
            path.parent,
            trusted_uid=trusted_uid,
            trusted_gid=trusted_gid,
        )
    before = os.lstat(path)
    if (
        not stat.S_ISDIR(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_uid != trusted_uid
        or before.st_gid != trusted_gid
        or stat.S_IMODE(before.st_mode) & 0o022
        or _list_xattrs(path)
    ):
        raise PermissionError(f"{label} is not an exact trusted directory")
    fd = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(fd)
        reached = os.lstat(path)
        if _stat_identity(opened) != _stat_identity(reached):
            raise RuntimeError(f"{label} identity changed while opening")
        yield _PinnedRoot(path, fd, _stat_identity(opened), _mount_id(fd))
        reached = os.lstat(path)
        if (
            _directory_anchor(os.fstat(fd)) != _directory_anchor(reached)
            or reached.st_uid != trusted_uid
            or reached.st_gid != trusted_gid
            or stat.S_IMODE(reached.st_mode) & 0o022
            or _list_xattrs(path)
        ):
            raise RuntimeError(f"{label} identity changed while in use")
    finally:
        os.close(fd)


def _open_child_directory(parent_fd: int, name: str, *, mount_id: str) -> int:
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    parent = os.fstat(parent_fd)
    if (
        not stat.S_ISDIR(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_uid != parent.st_uid
        or before.st_gid != parent.st_gid
        or stat.S_IMODE(before.st_mode) & 0o022
    ):
        raise RuntimeError("tree entry is not a real directory")
    fd = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    opened = os.fstat(fd)
    reached = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if _stat_identity(opened) != _stat_identity(reached) or _mount_id(fd) != mount_id:
        os.close(fd)
        raise RuntimeError("tree directory crosses or changed mount boundary")
    return fd


def _stable_file_at(
    parent_fd: int,
    name: str,
    *,
    maximum_bytes: int,
    exact_mode: int | None = None,
) -> bytes:
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    parent = os.fstat(parent_fd)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > maximum_bytes
        or before.st_uid != parent.st_uid
        or before.st_gid != parent.st_gid
        or stat.S_IMODE(before.st_mode) & 0o022
        or (exact_mode is not None and stat.S_IMODE(before.st_mode) != exact_mode)
    ):
        raise RuntimeError("protected artifact is not an exact regular file")
    fd = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    try:
        opened = os.fstat(fd)
        if _stat_identity(opened) != _stat_identity(before) or _mount_id(
            fd
        ) != _mount_id(parent_fd):
            raise RuntimeError("protected artifact changed while opening")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if len(raw) > maximum_bytes or _stat_identity(opened) != _stat_identity(after):
            raise RuntimeError("protected artifact changed during read")
        return raw
    finally:
        os.close(fd)


def _stable_file(
    path: Path,
    *,
    maximum_bytes: int,
    trusted_uid: int = 0,
    trusted_gid: int = 0,
    validate_parent_chain: bool = True,
) -> bytes:
    with _open_root(
        path.parent,
        label="protected artifact parent",
        trusted_uid=trusted_uid,
        trusted_gid=trusted_gid,
        validate_parent_chain=validate_parent_chain,
    ) as parent:
        return _stable_file_at(parent.fd, path.name, maximum_bytes=maximum_bytes)


def _decode_canonical_mapping(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} is not a JSON object")
    if raw != _canonical_bytes(value) + b"\n":
        raise RuntimeError(f"{label} is not canonical JSON")
    return value


def _snapshot_tree(fd: int, *, root_mount_id: str, prefix: str = "") -> str:
    started = os.fstat(fd)
    records: list[dict[str, Any]] = []
    with os.scandir(os.dup(fd)) as entries:
        for entry in sorted(entries, key=lambda item: item.name):
            name = entry.name
            reached = os.stat(name, dir_fd=fd, follow_symlinks=False)
            identity = list(_stat_identity(reached))
            relative = f"{prefix}/{name}" if prefix else name
            if stat.S_ISDIR(reached.st_mode):
                child_fd = _open_child_directory(fd, name, mount_id=root_mount_id)
                try:
                    child_digest = _snapshot_tree(
                        child_fd,
                        root_mount_id=root_mount_id,
                        prefix=relative,
                    )
                finally:
                    os.close(child_fd)
                records.append({
                    "path": relative,
                    "kind": "directory",
                    "identity": identity,
                    "children_sha256": child_digest,
                })
            elif stat.S_ISLNK(reached.st_mode):
                target = os.readlink(name, dir_fd=fd)
                after = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if _stat_identity(reached) != _stat_identity(after):
                    raise RuntimeError("tree symlink changed during inventory")
                records.append({
                    "path": relative,
                    "kind": "symlink",
                    "identity": identity,
                    "target": target,
                })
            elif stat.S_ISREG(reached.st_mode):
                child_fd = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=fd,
                )
                try:
                    if _mount_id(child_fd) != root_mount_id:
                        raise RuntimeError("tree file crosses mount boundary")
                    if _stat_identity(os.fstat(child_fd)) != _stat_identity(reached):
                        raise RuntimeError("tree file changed during inventory")
                finally:
                    os.close(child_fd)
                records.append({
                    "path": relative,
                    "kind": "file",
                    "identity": identity,
                })
            else:
                raise RuntimeError("tree contains an unsupported filesystem object")
    if _stat_identity(started) != _stat_identity(os.fstat(fd)):
        raise RuntimeError("tree directory changed during inventory")
    return hashlib.sha256(_canonical_bytes({"entries": records})).hexdigest()


def _inventory_root(
    root: _PinnedRoot,
) -> tuple[dict[str, _TreeEntry], dict[str, str], tuple[str, ...]]:
    valid: dict[str, _TreeEntry] = {}
    invalid: dict[str, str] = {}
    unknown: list[str] = []
    with os.scandir(os.dup(root.fd)) as entries:
        for entry in sorted(entries, key=lambda item: item.name):
            name = entry.name
            if _SHA_RE.fullmatch(name) is None:
                unknown.append(name)
                continue
            try:
                child_fd = _open_child_directory(root.fd, name, mount_id=root.mount_id)
                try:
                    opened = os.fstat(child_fd)
                    digest = _snapshot_tree(child_fd, root_mount_id=root.mount_id)
                    reached = os.stat(name, dir_fd=root.fd, follow_symlinks=False)
                    if _stat_identity(opened) != _stat_identity(reached):
                        raise RuntimeError("tree root changed during inventory")
                    valid[name] = _TreeEntry(
                        _stat_identity(opened), digest, root.mount_id
                    )
                finally:
                    os.close(child_fd)
            except (OSError, RuntimeError):
                invalid[name] = "entry_or_nested_mount_identity_invalid"
    return valid, invalid, tuple(sorted(unknown))


def _release_manifest_anchor(
    release_root: _PinnedRoot,
    revision: str,
    receipt: Mapping[str, Any],
) -> str:
    revision_fd = _open_child_directory(
        release_root.fd,
        revision,
        mount_id=release_root.mount_id,
    )
    try:
        raw = _stable_file_at(
            revision_fd,
            MANIFEST_NAME,
            maximum_bytes=MAX_JSON_BYTES,
            exact_mode=0o400,
        )
    finally:
        os.close(revision_fd)
    manifest = _decode_canonical_mapping(raw, label="release manifest")
    artifact = manifest.get("artifact_sha256")
    unsigned = dict(manifest)
    unsigned.pop("artifact_sha256", None)
    if (
        manifest.get("schema") != RELEASE_MANIFEST_SCHEMA
        or manifest.get("revision") != revision
        or manifest.get("artifact_root") != str(release_root.path / revision)
        or _SHA256_RE.fullmatch(str(artifact)) is None
        or artifact != hashlib.sha256(_canonical_bytes(unsigned)).hexdigest()
        or receipt.get("release_manifest_path")
        != str(release_root.path / revision / MANIFEST_NAME)
        or receipt.get("release_manifest_file_sha256")
        != hashlib.sha256(raw).hexdigest()
        or receipt.get("release_artifact_sha256") != artifact
    ):
        raise RuntimeError("release manifest producer anchor is invalid")
    return hashlib.sha256(raw).hexdigest()


def _receipt_state(
    layout: GCLayout,
    evidence_root: _PinnedRoot,
    release_root: _PinnedRoot,
    revision: str,
) -> _ReceiptState:
    try:
        revision_fd = _open_child_directory(
            evidence_root.fd,
            revision,
            mount_id=evidence_root.mount_id,
        )
        try:
            raw = _stable_file_at(
                revision_fd,
                RECEIPT_NAME,
                maximum_bytes=MAX_JSON_BYTES,
                exact_mode=0o400,
            )
        finally:
            os.close(revision_fd)
        receipt = _decode_canonical_mapping(raw, label="stopped-release receipt")
        unsigned = dict(receipt)
        digest = unsigned.pop("receipt_sha256", None)
        source = receipt.get("source")
        created = receipt.get("created_at_unix")
        service_before = receipt.get("service_state_before")
        service_after = receipt.get("service_state_after")
        host_path = receipt.get("host_identity_receipt_path")
        host_file_digest = receipt.get("host_identity_receipt_file_sha256")
        host_receipt_digest = receipt.get("host_identity_receipt_sha256")
        if (
            receipt.get("schema") != STOPPED_RECEIPT_SCHEMA
            or receipt.get("ok") is not True
            or receipt.get("state") != "published_services_stopped"
            or receipt.get("services_stopped_and_disabled") is not True
            or receipt.get("release_revision") != revision
            or receipt.get("release_root") != str(layout.release_base / revision)
            or receipt.get("receipt_path")
            != str(layout.evidence_base / revision / RECEIPT_NAME)
            or not isinstance(source, dict)
            or source.get("repository") != FORK_REPOSITORY
            or source.get("root") != str(layout.source_base / revision)
            or source.get("head_sha") != revision
            or _SHA_RE.fullmatch(str(source.get("tree_sha", ""))) is None
            or type(created) is not int
            or created < 0
            or not isinstance(service_before, list)
            or not service_before
            or service_before != service_after
            or not isinstance(host_path, str)
            or not Path(host_path).is_absolute()
            or _SHA256_RE.fullmatch(str(host_file_digest)) is None
            or _SHA256_RE.fullmatch(str(host_receipt_digest)) is None
            or _SHA256_RE.fullmatch(str(digest)) is None
            or digest != _sha256_json(unsigned)
        ):
            raise RuntimeError("stopped-release producer invariants are invalid")
        anchor = _release_manifest_anchor(release_root, revision, receipt)
    except (FileNotFoundError, RuntimeError, OSError, ValueError):
        return _ReceiptState("absent_or_invalid")
    return _ReceiptState(
        "terminal",
        created_at_unix=created,
        file_sha256=hashlib.sha256(raw).hexdigest(),
        producer_anchor_sha256=anchor,
    )


def _revision_from_target(layout: GCLayout, link: Path) -> str:
    with _open_root(
        link.parent,
        label="protected symlink parent",
        **_root_kwargs(layout),
    ) as parent:
        before = os.stat(link.name, dir_fd=parent.fd, follow_symlinks=False)
        if not stat.S_ISLNK(before.st_mode):
            raise RuntimeError("protected current/previous path is not a symlink")
        raw_target = os.readlink(link.name, dir_fd=parent.fd)
        after = os.stat(link.name, dir_fd=parent.fd, follow_symlinks=False)
        if _stat_identity(before) != _stat_identity(after):
            raise RuntimeError("protected current/previous symlink changed during read")
    target = Path(raw_target) if os.path.isabs(raw_target) else link.parent / raw_target
    target = Path(os.path.normpath(str(target)))
    for base in (layout.release_base, layout.source_base):
        if target.parent == base and _SHA_RE.fullmatch(target.name) is not None:
            return target.name
    raise RuntimeError("protected current/previous symlink target is outside GC roots")


def _walk_exact_revisions(value: Any, layout: GCLayout) -> Iterable[str]:
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_exact_revisions(child, layout)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_exact_revisions(child, layout)
    elif isinstance(value, str):
        if _SHA_RE.fullmatch(value) is not None:
            yield value
        else:
            candidate = Path(value)
            for base in (layout.release_base, layout.source_base):
                if candidate.parent == base and _SHA_RE.fullmatch(candidate.name):
                    yield candidate.name


def _protected_ref_revisions(layout: GCLayout) -> tuple[set[str], list[dict[str, Any]]]:
    revisions: set[str] = set()
    snapshots: list[dict[str, Any]] = []
    for path in layout.protected_refs:
        raw = _stable_file(
            path,
            maximum_bytes=MAX_JSON_BYTES,
            **_root_kwargs(layout),
        )
        value = _decode_canonical_mapping(raw, label="pending owner/cutover reference")
        found = sorted(set(_walk_exact_revisions(value, layout)))
        revisions.update(found)
        snapshots.append({
            "path": str(path),
            "file_sha256": hashlib.sha256(raw).hexdigest(),
            "revisions": found,
        })
    return revisions, snapshots


def _symlink_revisions(layout: GCLayout) -> tuple[set[str], list[dict[str, str]]]:
    revisions: set[str] = set()
    snapshots: list[dict[str, str]] = []
    for kind, paths in (
        ("current", layout.current_links),
        ("previous", layout.previous_links),
    ):
        for path in paths:
            revision = _revision_from_target(layout, path)
            revisions.add(revision)
            snapshots.append({"kind": kind, "path": str(path), "revision": revision})
    return revisions, snapshots


def load_protection_inventory(
    path: Path,
    *,
    trusted_uid: int = 0,
    trusted_gid: int = 0,
    validate_parent_chain: bool = True,
) -> GCLayout:
    raw = _stable_file(
        path,
        maximum_bytes=MAX_JSON_BYTES,
        trusted_uid=trusted_uid,
        trusted_gid=trusted_gid,
        validate_parent_chain=validate_parent_chain,
    )
    value = _decode_canonical_mapping(raw, label="GC protection inventory")
    unsigned = dict(value)
    digest = unsigned.pop("inventory_sha256", None)
    required = {"schema", "current_links", "previous_links", "protected_refs"}
    collections = [
        value.get(name)
        for name in ("current_links", "previous_links", "protected_refs")
    ]
    if (
        set(unsigned) != required
        or value.get("schema") != PROTECTION_INVENTORY_SCHEMA
        or _SHA256_RE.fullmatch(str(digest)) is None
        or digest != _sha256_json(unsigned)
        or any(not isinstance(items, list) or not items for items in collections)
        or any(
            not isinstance(item, str)
            or not Path(item).is_absolute()
            or ".." in Path(item).parts
            for items in collections
            for item in items
        )
        or any(len(items) != len(set(items)) for items in collections)
    ):
        raise RuntimeError("GC protection inventory is incomplete or invalid")
    return GCLayout(
        current_links=tuple(Path(item) for item in collections[0]),
        previous_links=tuple(Path(item) for item in collections[1]),
        protected_refs=tuple(Path(item) for item in collections[2]),
        protection_inventory_path=path,
        protection_inventory_file_sha256=hashlib.sha256(raw).hexdigest(),
        trusted_uid=trusted_uid,
        trusted_gid=trusted_gid,
        validate_parent_chain=validate_parent_chain,
    )


def _validate_inventory_binding(layout: GCLayout) -> None:
    if layout.protection_inventory_path is None:
        return
    current = load_protection_inventory(
        layout.protection_inventory_path,
        **_root_kwargs(layout),
    )
    if (
        current.current_links != layout.current_links
        or current.previous_links != layout.previous_links
        or current.protected_refs != layout.protected_refs
        or current.protection_inventory_file_sha256
        != layout.protection_inventory_file_sha256
    ):
        raise RuntimeError("GC protection inventory changed")


def build_plan(layout: GCLayout, *, production_sha: str) -> dict[str, Any]:
    """Return one deterministic, read-only GC plan."""

    layout.validate()
    _validate_inventory_binding(layout)
    production_sha = _require_sha(production_sha, label="production SHA")
    with (
        _open_root(
            layout.release_base,
            label="canary release base",
            **_root_kwargs(layout),
        ) as release_root,
        _open_root(
            layout.source_base,
            label="canary source base",
            **_root_kwargs(layout),
        ) as source_root,
        _open_root(
            layout.evidence_base,
            label="canary evidence base",
            **_root_kwargs(layout),
        ) as evidence_root,
    ):
        releases, invalid_releases, unknown_releases = _inventory_root(release_root)
        sources, invalid_sources, unknown_sources = _inventory_root(source_root)
        link_revisions, link_snapshots = _symlink_revisions(layout)
        ref_revisions, ref_snapshots = _protected_ref_revisions(layout)
        revisions = sorted(
            set(releases) | set(sources) | set(invalid_releases) | set(invalid_sources)
        )
        receipts = {
            revision: _receipt_state(layout, evidence_root, release_root, revision)
            for revision in revisions
        }
        complete_terminal_pairs = [
            revision
            for revision in revisions
            if revision in releases
            and revision in sources
            and revision not in invalid_releases
            and revision not in invalid_sources
            and receipts[revision].status == "terminal"
        ]
        complete_terminal_pairs.sort(
            key=lambda revision: (receipts[revision].created_at_unix, revision),
            reverse=True,
        )
        newest_terminal = set(complete_terminal_pairs[:TERMINAL_RETENTION_COUNT])
        protected = {production_sha} | link_revisions | ref_revisions | newest_terminal

        units: list[dict[str, Any]] = []
        for revision in revisions:
            reasons: list[str] = []
            receipt = receipts[revision]
            if revision in invalid_releases or revision in invalid_sources:
                reasons.append("invalid_release_or_source_entry")
            if revision not in releases or revision not in sources:
                reasons.append("release_source_pair_incomplete")
            if receipt.status != "terminal":
                reasons.append("receipt_absent_or_nonterminal")
            if revision == production_sha:
                reasons.append("production_sha")
            if revision in link_revisions:
                reasons.append("current_or_previous_target")
            if revision in ref_revisions:
                reasons.append("pending_owner_or_cutover_ref")
            if revision in newest_terminal:
                reasons.append("newest_terminal_retention")
            release_entry = releases.get(revision)
            source_entry = sources.get(revision)
            units.append({
                "revision": revision,
                "release_path": str(layout.release_base / revision),
                "source_path": str(layout.source_base / revision),
                "evidence_path": str(layout.evidence_base / revision / RECEIPT_NAME),
                "receipt_status": receipt.status,
                "receipt_created_at_unix": receipt.created_at_unix,
                "receipt_file_sha256": receipt.file_sha256,
                "producer_anchor_sha256": receipt.producer_anchor_sha256,
                "release_identity": list(release_entry.identity)
                if release_entry
                else None,
                "release_tree_sha256": release_entry.tree_sha256
                if release_entry
                else None,
                "source_identity": list(source_entry.identity)
                if source_entry
                else None,
                "source_tree_sha256": source_entry.tree_sha256
                if source_entry
                else None,
                "action": "delete_pair" if not reasons else "preserve",
                "reasons": reasons,
            })

        unsigned: dict[str, Any] = {
            "schema": PLAN_SCHEMA,
            "production_sha": production_sha,
            "roots": {
                "release_base": str(layout.release_base),
                "source_base": str(layout.source_base),
                "evidence_base": str(layout.evidence_base),
                "release_identity": list(_node_anchor(os.fstat(release_root.fd))),
                "source_identity": list(_node_anchor(os.fstat(source_root.fd))),
                "evidence_identity": list(_node_anchor(os.fstat(evidence_root.fd))),
                "release_mount_id": release_root.mount_id,
                "source_mount_id": source_root.mount_id,
                "evidence_mount_id": evidence_root.mount_id,
            },
            "protection_inventory": {
                "path": str(layout.protection_inventory_path)
                if layout.protection_inventory_path
                else None,
                "file_sha256": layout.protection_inventory_file_sha256,
            },
            "terminal_retention_count": TERMINAL_RETENTION_COUNT,
            "protected": {
                "newest_complete_terminal_pairs": sorted(newest_terminal),
                "newest_terminal_revisions": sorted(newest_terminal),
                "symlinks": link_snapshots,
                "structured_refs": ref_snapshots,
            },
            "unknown_entries": {
                "release_base": list(unknown_releases),
                "source_base": list(unknown_sources),
            },
            "units": units,
            "evidence_deletion_enabled": False,
        }
    return {**unsigned, "plan_sha256": _sha256_json(unsigned)}


def _write_all(fd: int, raw: bytes) -> None:
    offset = 0
    while offset < len(raw):
        written = os.write(fd, raw[offset:])
        if written <= 0:
            raise OSError("GC receipt write made no progress")
        offset += written


def _discard_incomplete_candidate(parent_fd: int, name: str) -> None:
    reached = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    parent = os.fstat(parent_fd)
    if (
        not stat.S_ISREG(reached.st_mode)
        or reached.st_nlink != 1
        or reached.st_uid != parent.st_uid
        or reached.st_gid != parent.st_gid
        or stat.S_IMODE(reached.st_mode) != 0o400
    ):
        raise RuntimeError("GC receipt candidate identity is invalid")
    os.unlink(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def _write_receipt_at(parent_fd: int, name: str, value: Mapping[str, Any]) -> None:
    """Publish one append-only receipt through a durable no-replace candidate."""

    raw = _canonical_bytes(value) + b"\n"
    if len(raw) > MAX_JSON_BYTES:
        raise RuntimeError("durable GC receipt exceeds size bound")
    candidate = f".{name}.{hashlib.sha256(raw).hexdigest()}.candidate"

    try:
        existing = _stable_file_at(
            parent_fd,
            name,
            maximum_bytes=MAX_JSON_BYTES,
            exact_mode=0o400,
        )
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if existing != raw:
            raise RuntimeError("durable GC receipt collision exists")
        try:
            candidate_raw = _stable_file_at(
                parent_fd,
                candidate,
                maximum_bytes=MAX_JSON_BYTES,
                exact_mode=0o400,
            )
        except FileNotFoundError:
            return
        if candidate_raw != raw:
            _discard_incomplete_candidate(parent_fd, candidate)
            return
        os.unlink(candidate, dir_fd=parent_fd)
        os.fsync(parent_fd)
        return

    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        fd = os.open(candidate, flags, 0o400, dir_fd=parent_fd)
    except FileExistsError:
        try:
            candidate_raw = _stable_file_at(
                parent_fd,
                candidate,
                maximum_bytes=MAX_JSON_BYTES,
                exact_mode=0o400,
            )
        except RuntimeError:
            _discard_incomplete_candidate(parent_fd, candidate)
            fd = os.open(candidate, flags, 0o400, dir_fd=parent_fd)
        else:
            if candidate_raw != raw:
                _discard_incomplete_candidate(parent_fd, candidate)
                fd = os.open(candidate, flags, 0o400, dir_fd=parent_fd)
            else:
                fd = -1
    try:
        if fd >= 0:
            _write_all(fd, raw)
            os.fchmod(fd, 0o400)
            os.fsync(fd)
    finally:
        if fd >= 0:
            os.close(fd)
    os.fsync(parent_fd)
    try:
        _rename_noreplace(parent_fd, candidate, parent_fd, name)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise
        existing = _stable_file_at(
            parent_fd,
            name,
            maximum_bytes=MAX_JSON_BYTES,
            exact_mode=0o400,
        )
        if existing != raw:
            raise RuntimeError("durable GC receipt collision exists") from exc
        os.unlink(candidate, dir_fd=parent_fd)
    os.fsync(parent_fd)
    published = _stable_file_at(
        parent_fd,
        name,
        maximum_bytes=MAX_JSON_BYTES,
        exact_mode=0o400,
    )
    if published != raw:
        raise RuntimeError("durable GC receipt publication drifted")


def _signed_receipt(
    schema: str, digest_field: str, body: Mapping[str, Any]
) -> dict[str, Any]:
    unsigned = {"schema": schema, **dict(body)}
    return {**unsigned, digest_field: _sha256_json(unsigned)}


def _validate_durable_receipt(
    value: Mapping[str, Any], schema: str, digest_field: str
) -> dict[str, Any]:
    receipt = dict(value)
    digest = receipt.pop(digest_field, None)
    if (
        receipt.get("schema") != schema
        or _SHA256_RE.fullmatch(str(digest)) is None
        or digest != _sha256_json(receipt)
    ):
        raise RuntimeError("durable GC receipt is invalid")
    return dict(value)


def _load_optional_receipt_at(
    parent_fd: int, name: str, schema: str, digest_field: str
) -> dict[str, Any] | None:
    try:
        raw = _stable_file_at(
            parent_fd,
            name,
            maximum_bytes=MAX_JSON_BYTES,
            exact_mode=0o400,
        )
    except FileNotFoundError:
        return None
    value = _decode_canonical_mapping(raw, label="durable GC receipt")
    return _validate_durable_receipt(value, schema, digest_field)


def _rename_noreplace(old_fd: int, old: str, new_fd: int, new: str) -> None:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("atomic no-replace rename requires Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise RuntimeError("renameat2(RENAME_NOREPLACE) is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if renameat2(old_fd, os.fsencode(old), new_fd, os.fsencode(new), 1) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), new)


def _entry_state(
    root: _PinnedRoot,
    name: str,
    expected_identity: Sequence[int],
    expected_tree: str,
    *,
    renamed: bool = False,
) -> bool:
    try:
        fd = _open_child_directory(root.fd, name, mount_id=root.mount_id)
    except FileNotFoundError:
        return False
    try:
        opened = os.fstat(fd)
        digest = _snapshot_tree(fd, root_mount_id=root.mount_id)
        actual_identity = _stat_identity(opened)
        identity_matches = (
            tuple(expected_identity)[:-1] == actual_identity[:-1]
            if renamed
            else tuple(expected_identity) == actual_identity
        )
        if not identity_matches or digest != expected_tree:
            raise RuntimeError("GC unit identity changed")
        return True
    finally:
        os.close(fd)


def _node_anchor(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
    )


def _entry_anchor_state(
    root: _PinnedRoot,
    name: str,
    expected_anchor: Sequence[int],
) -> bool:
    try:
        fd = _open_child_directory(root.fd, name, mount_id=root.mount_id)
    except FileNotFoundError:
        return False
    try:
        if tuple(expected_anchor) != _node_anchor(os.fstat(fd)):
            raise RuntimeError("GC tombstone identity changed")
        return True
    finally:
        os.close(fd)


def _unlink_pinned_leaf(
    parent_fd: int,
    name: str,
    before: os.stat_result,
    *,
    mount_id: str,
) -> None:
    if sys.platform.startswith("linux"):
        path_flag = getattr(os, "O_PATH", None)
        if path_flag is None:
            raise RuntimeError("Linux O_PATH is required for pinned GC purge")
        flags = path_flag
    elif stat.S_ISREG(before.st_mode):
        flags = os.O_RDONLY
    else:
        # Non-Linux execution exists only for filesystem-independent tests;
        # production apply is Linux-only.  The trusted, non-writable parent
        # plus exact double observation remains fail-closed in that test seam.
        reached = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _stat_identity(reached) != _stat_identity(before):
            raise RuntimeError("GC leaf identity changed before unlink")
        os.unlink(name, dir_fd=parent_fd)
        return
    leaf_fd = os.open(
        name,
        flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    try:
        if (
            _stat_identity(os.fstat(leaf_fd)) != _stat_identity(before)
            or _mount_id(leaf_fd) != mount_id
        ):
            raise RuntimeError("GC leaf identity changed while pinning")
        reached = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _stat_identity(reached) != _stat_identity(before):
            raise RuntimeError("GC leaf identity changed before unlink")
        os.unlink(name, dir_fd=parent_fd)
    finally:
        os.close(leaf_fd)


def _purge_open_directory(fd: int, *, mount_id: str) -> None:
    """Monotonically empty one pinned directory; safe to resume after a crash."""

    with os.scandir(os.dup(fd)) as entries:
        names = sorted((entry.name for entry in entries), reverse=True)
    for child in names:
        before = os.stat(child, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(before.st_mode):
            child_fd = _open_child_directory(fd, child, mount_id=mount_id)
            try:
                anchor = _node_anchor(os.fstat(child_fd))
                _purge_open_directory(child_fd, mount_id=mount_id)
                reached = os.stat(child, dir_fd=fd, follow_symlinks=False)
                if _node_anchor(reached) != anchor:
                    raise RuntimeError("GC nested directory identity changed")
                os.rmdir(child, dir_fd=fd)
            finally:
                os.close(child_fd)
            os.fsync(fd)
            continue
        if not (stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)):
            raise RuntimeError("GC tree contains an unsupported filesystem object")
        _unlink_pinned_leaf(fd, child, before, mount_id=mount_id)
        os.fsync(fd)
    os.fsync(fd)


def _purge_tree_at(
    root: _PinnedRoot,
    name: str,
    expected_anchor: Sequence[int],
) -> None:
    try:
        fd = _open_child_directory(root.fd, name, mount_id=root.mount_id)
    except FileNotFoundError:
        os.fsync(root.fd)
        return
    try:
        anchor = _node_anchor(os.fstat(fd))
        if anchor != tuple(expected_anchor):
            raise RuntimeError("GC tombstone identity changed before purge")
        _purge_open_directory(fd, mount_id=root.mount_id)
        reached = os.stat(name, dir_fd=root.fd, follow_symlinks=False)
        if _node_anchor(reached) != anchor:
            raise RuntimeError("GC tombstone identity changed before rmdir")
        os.rmdir(name, dir_fd=root.fd)
    finally:
        os.close(fd)
    os.fsync(root.fd)


def _ensure_tombstone(
    root: _PinnedRoot,
    revision: str,
    tombstone: str,
    identity: Sequence[int],
    tree_sha256: str,
) -> tuple[int, ...]:
    original = _entry_state(root, revision, identity, tree_sha256)
    tomb = _entry_state(root, tombstone, identity, tree_sha256, renamed=True)
    if original and tomb:
        raise RuntimeError("GC original and tombstone both exist")
    if tomb:
        fd = _open_child_directory(root.fd, tombstone, mount_id=root.mount_id)
        try:
            return _node_anchor(os.fstat(fd))
        finally:
            os.close(fd)
    if not original:
        raise RuntimeError("GC unit disappeared before logical deletion")
    _rename_noreplace(root.fd, revision, root.fd, tombstone)
    os.fsync(root.fd)
    if not _entry_state(root, tombstone, identity, tree_sha256, renamed=True):
        raise RuntimeError("GC tombstone identity is invalid")
    fd = _open_child_directory(root.fd, tombstone, mount_id=root.mount_id)
    try:
        return _node_anchor(os.fstat(fd))
    finally:
        os.close(fd)


def _protection_revisions(layout: GCLayout, production_sha: str) -> set[str]:
    links, _ = _symlink_revisions(layout)
    refs, _ = _protected_ref_revisions(layout)
    return {production_sha} | links | refs


def _require_unit_contract(
    layout: GCLayout,
    unit: Mapping[str, Any],
) -> tuple[str, list[int], list[int], str, str]:
    revision = _require_sha(str(unit.get("revision")), label="GC unit revision")
    release_identity = unit.get("release_identity")
    source_identity = unit.get("source_identity")
    release_tree = unit.get("release_tree_sha256")
    source_tree = unit.get("source_tree_sha256")
    if (
        unit.get("action") != "delete_pair"
        or unit.get("reasons") != []
        or unit.get("release_path") != str(layout.release_base / revision)
        or unit.get("source_path") != str(layout.source_base / revision)
        or unit.get("evidence_path")
        != str(layout.evidence_base / revision / RECEIPT_NAME)
        or unit.get("receipt_status") != "terminal"
        or not isinstance(release_identity, list)
        or len(release_identity) != 9
        or not all(type(item) is int for item in release_identity)
        or not isinstance(source_identity, list)
        or len(source_identity) != 9
        or not all(type(item) is int for item in source_identity)
        or not isinstance(release_tree, str)
        or _SHA256_RE.fullmatch(release_tree) is None
        or not isinstance(source_tree, str)
        or _SHA256_RE.fullmatch(source_tree) is None
        or _SHA256_RE.fullmatch(str(unit.get("receipt_file_sha256"))) is None
        or _SHA256_RE.fullmatch(str(unit.get("producer_anchor_sha256"))) is None
    ):
        raise RuntimeError("GC unit contract is invalid")
    return revision, release_identity, source_identity, release_tree, source_tree


def _logical_receipt_matches(
    logical: Mapping[str, Any],
    *,
    revision: str,
    approved_plan_sha256: str,
    intent_sha256: str,
    release_tombstone: str,
    source_tombstone: str,
) -> bool:
    return (
        logical.get("revision") == revision
        and logical.get("approved_plan_sha256") == approved_plan_sha256
        and logical.get("intent_sha256") == intent_sha256
        and logical.get("release_tombstone") == release_tombstone
        and logical.get("source_tombstone") == source_tombstone
        and isinstance(logical.get("release_tombstone_identity"), list)
        and len(logical["release_tombstone_identity"]) == 5
        and all(type(item) is int for item in logical["release_tombstone_identity"])
        and isinstance(logical.get("source_tombstone_identity"), list)
        and len(logical["source_tombstone_identity"]) == 5
        and all(type(item) is int for item in logical["source_tombstone_identity"])
    )


def _assert_absent_at(root: _PinnedRoot, *names: str) -> None:
    for name in names:
        try:
            os.stat(name, dir_fd=root.fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        raise RuntimeError("GC final absence postcondition failed")
    os.fsync(root.fd)


def _unit_has_physical_receipt(layout: GCLayout, revision: str) -> bool:
    with _open_root(
        layout.evidence_base,
        label="canary evidence base",
        **_root_kwargs(layout),
    ) as evidence_root:
        evidence_fd = _open_child_directory(
            evidence_root.fd,
            revision,
            mount_id=evidence_root.mount_id,
        )
        try:
            return (
                _load_optional_receipt_at(
                    evidence_fd,
                    PURGE_NAME,
                    PURGE_SCHEMA,
                    "purge_sha256",
                )
                is not None
            )
        finally:
            os.close(evidence_fd)


def _apply_unit(
    layout: GCLayout,
    unit: Mapping[str, Any],
    approved_plan_sha256: str,
) -> None:
    (
        revision,
        release_identity,
        source_identity,
        release_tree,
        source_tree,
    ) = _require_unit_contract(layout, unit)
    suffix = approved_plan_sha256[:20]
    release_tombstone = f".{revision}.{suffix}.release.gc"
    source_tombstone = f".{revision}.{suffix}.source.gc"
    intent_body = {
        "revision": revision,
        "approved_plan_sha256": approved_plan_sha256,
        "unit": dict(unit),
        "release_tombstone": release_tombstone,
        "source_tombstone": source_tombstone,
    }
    intent = _signed_receipt(INTENT_SCHEMA, "intent_sha256", intent_body)

    with (
        _open_root(
            layout.release_base,
            label="canary release base",
            **_root_kwargs(layout),
        ) as release_root,
        _open_root(
            layout.source_base,
            label="canary source base",
            **_root_kwargs(layout),
        ) as source_root,
        _open_root(
            layout.evidence_base,
            label="canary evidence base",
            **_root_kwargs(layout),
        ) as evidence_root,
    ):
        evidence_fd = _open_child_directory(
            evidence_root.fd, revision, mount_id=evidence_root.mount_id
        )
        try:
            _write_receipt_at(evidence_fd, INTENT_NAME, intent)
            stored_intent = _load_optional_receipt_at(
                evidence_fd, INTENT_NAME, INTENT_SCHEMA, "intent_sha256"
            )
            if stored_intent != intent:
                raise RuntimeError("GC intent binding changed")
            logical = _load_optional_receipt_at(
                evidence_fd,
                LOGICAL_DELETE_NAME,
                LOGICAL_DELETE_SCHEMA,
                "logical_delete_sha256",
            )
            physical = _load_optional_receipt_at(
                evidence_fd, PURGE_NAME, PURGE_SCHEMA, "purge_sha256"
            )
            if physical is not None:
                if logical is None or not _logical_receipt_matches(
                    logical,
                    revision=revision,
                    approved_plan_sha256=approved_plan_sha256,
                    intent_sha256=intent["intent_sha256"],
                    release_tombstone=release_tombstone,
                    source_tombstone=source_tombstone,
                ):
                    raise RuntimeError(
                        "GC physical-purge receipt lacks its logical-delete anchor"
                    )
                expected_physical = _signed_receipt(
                    PURGE_SCHEMA,
                    "purge_sha256",
                    {
                        "revision": revision,
                        "approved_plan_sha256": approved_plan_sha256,
                        "intent_sha256": intent["intent_sha256"],
                        "logical_delete_sha256": logical["logical_delete_sha256"],
                        "release_tree_absent": True,
                        "source_tree_absent": True,
                    },
                )
                if physical != expected_physical:
                    raise RuntimeError("GC physical-purge receipt binding is invalid")
                _assert_absent_at(release_root, revision, release_tombstone)
                _assert_absent_at(source_root, revision, source_tombstone)
                return
            if logical is None:
                source_anchor = _ensure_tombstone(
                    source_root,
                    revision,
                    source_tombstone,
                    source_identity,
                    source_tree,
                )
                release_anchor = _ensure_tombstone(
                    release_root,
                    revision,
                    release_tombstone,
                    release_identity,
                    release_tree,
                )
                logical = _signed_receipt(
                    LOGICAL_DELETE_SCHEMA,
                    "logical_delete_sha256",
                    {
                        "revision": revision,
                        "approved_plan_sha256": approved_plan_sha256,
                        "intent_sha256": intent["intent_sha256"],
                        "release_tombstone": release_tombstone,
                        "source_tombstone": source_tombstone,
                        "release_tombstone_identity": list(release_anchor),
                        "source_tombstone_identity": list(source_anchor),
                    },
                )
                _write_receipt_at(evidence_fd, LOGICAL_DELETE_NAME, logical)
            elif not _logical_receipt_matches(
                logical,
                revision=revision,
                approved_plan_sha256=approved_plan_sha256,
                intent_sha256=intent["intent_sha256"],
                release_tombstone=release_tombstone,
                source_tombstone=source_tombstone,
            ):
                raise RuntimeError("GC logical-delete receipt binding is invalid")
            if _entry_state(
                source_root, revision, source_identity, source_tree
            ) or _entry_state(release_root, revision, release_identity, release_tree):
                raise RuntimeError("GC original survived durable logical deletion")
            source_anchor = logical["source_tombstone_identity"]
            release_anchor = logical["release_tombstone_identity"]
            _entry_anchor_state(source_root, source_tombstone, source_anchor)
            _entry_anchor_state(release_root, release_tombstone, release_anchor)
            _purge_tree_at(source_root, source_tombstone, source_anchor)
            _purge_tree_at(release_root, release_tombstone, release_anchor)
            _assert_absent_at(release_root, revision, release_tombstone)
            _assert_absent_at(source_root, revision, source_tombstone)
            physical = _signed_receipt(
                PURGE_SCHEMA,
                "purge_sha256",
                {
                    "revision": revision,
                    "approved_plan_sha256": approved_plan_sha256,
                    "intent_sha256": intent["intent_sha256"],
                    "logical_delete_sha256": logical["logical_delete_sha256"],
                    "release_tree_absent": True,
                    "source_tree_absent": True,
                },
            )
            _write_receipt_at(evidence_fd, PURGE_NAME, physical)
            stored_physical = _load_optional_receipt_at(
                evidence_fd, PURGE_NAME, PURGE_SCHEMA, "purge_sha256"
            )
            if stored_physical != physical:
                raise RuntimeError("GC physical-purge receipt publication drifted")
        finally:
            os.close(evidence_fd)


def _plan_anchor_name(approved_plan_sha256: str) -> str:
    return f"{PLAN_ANCHOR_PREFIX}{approved_plan_sha256}.json"


def _load_plan_anchor_candidate_at(
    parent_fd: int,
    final_name: str,
) -> dict[str, Any] | None:
    prefix = f".{final_name}."
    suffix = ".candidate"
    with os.scandir(os.dup(parent_fd)) as entries:
        names = sorted(
            entry.name
            for entry in entries
            if entry.name.startswith(prefix) and entry.name.endswith(suffix)
        )
    if not names:
        return None
    if len(names) != 1:
        raise RuntimeError("approved GC plan anchor candidates are ambiguous")
    digest = names[0][len(prefix) : -len(suffix)]
    if _SHA256_RE.fullmatch(digest) is None:
        raise RuntimeError("approved GC plan anchor candidate name is invalid")
    raw = _stable_file_at(
        parent_fd,
        names[0],
        maximum_bytes=MAX_JSON_BYTES,
        exact_mode=0o400,
    )
    if hashlib.sha256(raw).hexdigest() != digest:
        _discard_incomplete_candidate(parent_fd, names[0])
        return None
    try:
        value = _decode_canonical_mapping(
            raw,
            label="approved GC plan anchor candidate",
        )
    except RuntimeError:
        _discard_incomplete_candidate(parent_fd, names[0])
        return None
    return _validate_durable_receipt(
        value,
        PLAN_ANCHOR_SCHEMA,
        "anchor_sha256",
    )


def _plan_candidates(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    units = plan.get("units")
    if not isinstance(units, list) or not all(isinstance(item, dict) for item in units):
        raise RuntimeError("approved GC plan units are invalid")
    return [dict(item) for item in units if item.get("action") == "delete_pair"]


def _validate_plan_anchor(
    layout: GCLayout,
    anchor: Mapping[str, Any],
    *,
    production_sha: str,
    approved_plan_sha256: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    value = _validate_durable_receipt(
        anchor,
        PLAN_ANCHOR_SCHEMA,
        "anchor_sha256",
    )
    plan = value.get("plan")
    candidates = value.get("candidates")
    if not isinstance(plan, dict) or not isinstance(candidates, list):
        raise RuntimeError("approved GC plan anchor payload is invalid")
    unsigned_plan = dict(plan)
    recorded_plan_sha = unsigned_plan.pop("plan_sha256", None)
    expected_candidates = _plan_candidates(plan)
    roots = plan.get("roots")
    protection = plan.get("protection_inventory")
    if (
        set(value)
        != {
            "schema",
            "production_sha",
            "approved_plan_sha256",
            "protection_inventory_file_sha256",
            "plan",
            "candidates",
            "anchor_sha256",
        }
        or value.get("production_sha") != production_sha
        or value.get("approved_plan_sha256") != approved_plan_sha256
        or value.get("protection_inventory_file_sha256")
        != layout.protection_inventory_file_sha256
        or plan.get("schema") != PLAN_SCHEMA
        or plan.get("production_sha") != production_sha
        or not isinstance(roots, dict)
        or roots.get("release_base") != str(layout.release_base)
        or roots.get("source_base") != str(layout.source_base)
        or roots.get("evidence_base") != str(layout.evidence_base)
        or not isinstance(protection, dict)
        or protection.get("path")
        != (
            str(layout.protection_inventory_path)
            if layout.protection_inventory_path
            else None
        )
        or protection.get("file_sha256") != layout.protection_inventory_file_sha256
        or plan.get("evidence_deletion_enabled") is not False
        or recorded_plan_sha != approved_plan_sha256
        or _sha256_json(unsigned_plan) != approved_plan_sha256
        or candidates != expected_candidates
    ):
        raise RuntimeError("approved GC plan anchor binding is invalid")
    for unit in expected_candidates:
        _require_unit_contract(layout, unit)
    return dict(plan), expected_candidates


def _publish_or_load_plan_anchor(
    layout: GCLayout,
    evidence_root: _PinnedRoot,
    *,
    production_sha: str,
    approved_plan_sha256: str,
    current_plan: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    name = _plan_anchor_name(approved_plan_sha256)
    existing = _load_optional_receipt_at(
        evidence_root.fd,
        name,
        PLAN_ANCHOR_SCHEMA,
        "anchor_sha256",
    )
    if existing is None:
        anchor = _load_plan_anchor_candidate_at(evidence_root.fd, name)
        if anchor is None:
            if (
                current_plan is None
                or current_plan.get("plan_sha256") != approved_plan_sha256
            ):
                raise PermissionError(
                    "approved GC plan digest does not match current state"
                )
            anchor = _signed_receipt(
                PLAN_ANCHOR_SCHEMA,
                "anchor_sha256",
                {
                    "production_sha": production_sha,
                    "approved_plan_sha256": approved_plan_sha256,
                    "protection_inventory_file_sha256": (
                        layout.protection_inventory_file_sha256
                    ),
                    "plan": dict(current_plan),
                    "candidates": _plan_candidates(current_plan),
                },
            )
        _validate_plan_anchor(
            layout,
            anchor,
            production_sha=production_sha,
            approved_plan_sha256=approved_plan_sha256,
        )
        _write_receipt_at(evidence_root.fd, name, anchor)
        existing = _load_optional_receipt_at(
            evidence_root.fd,
            name,
            PLAN_ANCHOR_SCHEMA,
            "anchor_sha256",
        )
        if existing != anchor:
            raise RuntimeError("approved GC plan anchor publication drifted")
    return _validate_plan_anchor(
        layout,
        existing,
        production_sha=production_sha,
        approved_plan_sha256=approved_plan_sha256,
    )


def _plan_root_anchor(identity: object) -> tuple[int, ...]:
    if (
        not isinstance(identity, list)
        or len(identity) not in {5, 9}
        or not all(type(item) is int for item in identity)
    ):
        raise RuntimeError("approved GC root identity is invalid")
    if len(identity) == 5:
        return tuple(identity)
    return (identity[0], identity[1], identity[2], identity[4], identity[5])


def _validate_plan_root_identities(layout: GCLayout, plan: Mapping[str, Any]) -> None:
    roots = plan.get("roots")
    if not isinstance(roots, dict):
        raise RuntimeError("approved GC plan roots are invalid")
    with (
        _open_root(
            layout.release_base,
            label="canary release base",
            **_root_kwargs(layout),
        ) as release_root,
        _open_root(
            layout.source_base,
            label="canary source base",
            **_root_kwargs(layout),
        ) as source_root,
        _open_root(
            layout.evidence_base,
            label="canary evidence base",
            **_root_kwargs(layout),
        ) as evidence_root,
    ):
        expected = {
            "release_identity": _node_anchor(os.fstat(release_root.fd)),
            "source_identity": _node_anchor(os.fstat(source_root.fd)),
            "evidence_identity": _node_anchor(os.fstat(evidence_root.fd)),
        }
    if any(
        _plan_root_anchor(roots.get(name)) != observed
        for name, observed in expected.items()
    ):
        raise RuntimeError("approved GC root identity changed")


def apply_plan(
    layout: GCLayout,
    *,
    production_sha: str,
    approved_plan_sha256: str,
    require_root_linux: bool = True,
    lifecycle_lock: Callable[[], ContextManager[int]] | None = None,
) -> dict[str, Any]:
    """Revalidate under the shared lock, then resume/apply exact approved units."""

    if require_root_linux and (
        not sys.platform.startswith("linux")
        or not hasattr(os, "geteuid")
        or os.geteuid() != 0  # windows-footgun: ok — guarded Linux root boundary
    ):
        raise PermissionError("canary GC apply requires root on Linux")
    if _SHA256_RE.fullmatch(str(approved_plan_sha256)) is None:
        raise ValueError("approved GC plan digest is invalid")
    production_sha = _require_sha(production_sha, label="production SHA")
    lock = lifecycle_lock or host_release_lifecycle_lock
    removed: list[str] = []
    with lock():
        layout.validate()
        _validate_inventory_binding(layout)
        with _open_root(
            layout.evidence_base,
            label="canary evidence base",
            **_root_kwargs(layout),
        ) as evidence_root:
            existing_anchor = _load_optional_receipt_at(
                evidence_root.fd,
                _plan_anchor_name(approved_plan_sha256),
                PLAN_ANCHOR_SCHEMA,
                "anchor_sha256",
            )
            if existing_anchor is None:
                existing_anchor = _load_plan_anchor_candidate_at(
                    evidence_root.fd,
                    _plan_anchor_name(approved_plan_sha256),
                )
        current = None
        if existing_anchor is None:
            current = build_plan(layout, production_sha=production_sha)
            if current["plan_sha256"] != approved_plan_sha256:
                raise PermissionError(
                    "approved GC plan digest does not match current state"
                )
        with _open_root(
            layout.evidence_base,
            label="canary evidence base",
            **_root_kwargs(layout),
        ) as evidence_root:
            plan, candidates = _publish_or_load_plan_anchor(
                layout,
                evidence_root,
                production_sha=production_sha,
                approved_plan_sha256=approved_plan_sha256,
                current_plan=current,
            )
        _validate_plan_root_identities(layout, plan)
        for approved_unit in candidates:
            _validate_plan_root_identities(layout, plan)
            revision = str(approved_unit["revision"])
            if _unit_has_physical_receipt(layout, revision):
                _apply_unit(layout, approved_unit, approved_plan_sha256)
                removed.append(revision)
                continue
            if revision in _protection_revisions(layout, production_sha):
                raise RuntimeError("approved GC unit became protected")
            _apply_unit(layout, approved_unit, approved_plan_sha256)
            removed.append(revision)

    result_unsigned = {
        "schema": RESULT_SCHEMA,
        "ok": True,
        "approved_plan_sha256": approved_plan_sha256,
        "removed_release_source_pairs": sorted(set(removed)),
        "evidence_deleted": False,
    }
    return {**result_unsigned, "result_sha256": _sha256_json(result_unsigned)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-sha", required=True)
    parser.add_argument("--protection-inventory", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approved-plan-sha256")
    return parser


def _die(message: str) -> NoReturn:
    raise SystemExit(message)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    layout = load_protection_inventory(args.protection_inventory)
    if not args.apply:
        if args.approved_plan_sha256 is not None:
            _die("--approved-plan-sha256 is valid only with --apply")
        with host_release_lifecycle_lock():
            report = build_plan(layout, production_sha=args.production_sha)
    else:
        if args.approved_plan_sha256 is None:
            _die("--apply requires --approved-plan-sha256")
        report = apply_plan(
            layout,
            production_sha=args.production_sha,
            approved_plan_sha256=args.approved_plan_sha256,
        )
    sys.stdout.buffer.write(_canonical_bytes(report) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
