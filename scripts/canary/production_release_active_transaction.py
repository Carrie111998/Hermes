#!/usr/bin/env python3
"""Durable root-owned registry for the one active release transaction.

The registry is deliberately a dormant storage primitive.  It has no
entrypoint, timer, unit, or boot activation policy.  The recovery coordinator
can normalize an interrupted publication, discover the exact signed runtime
authority that owns the sole active transaction, and retire that exact marker
only after the transaction runtime has revalidated a terminal host state.

Publication is create-only: a private pending inode is written and fsynced,
then hard-linked without replacement to the canonical marker, the directory
is fsynced, and the pending link is removed and fsynced.  Recovery accepts
only the two states produced by that protocol.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn, Sequence

from scripts.canary import production_release_update_runtime as runtime


PRODUCTION_REGISTRY_ROOT = Path(
    "/var/lib/muncho-production-release-update"
)
ACTIVE_MARKER_NAME = "active-transaction.json"
ACTIVE_PENDING_NAME = ".active-transaction.pending"
ACTIVE_MARKER_SCHEMA = (
    "muncho-production-release-active-transaction.v1"
)
DIRECTORY_MODE = 0o700
FILE_MODE = 0o400
MAX_MARKER_BYTES = 16 * 1024 * 1024

# These are the only namespaces already owned beneath the shared production
# root.  The registry never creates or mutates them.
ALLOWED_SIBLING_DIRECTORIES = frozenset(
    {"authority", "inputs", "transactions"}
)

PUBLICATION_DURABLE_BOUNDARIES = (
    "active_pending_created",
    "active_pending_written",
    "active_pending_file_fsynced",
    "active_pending_directory_fsynced",
    "active_final_linked",
    "active_final_directory_fsynced",
    "active_pending_unlinked",
    "active_cleanup_directory_fsynced",
    "active_readback_validated",
)
UNCOMMITTED_RECOVERY_DURABLE_BOUNDARIES = (
    "active_recovery_uncommitted_pending_removed",
    "active_recovery_uncommitted_cleanup_fsynced",
)
LINKED_RECOVERY_DURABLE_BOUNDARIES = (
    "active_recovery_final_directory_fsynced",
    "active_recovery_pending_unlinked",
    "active_recovery_cleanup_directory_fsynced",
    "active_recovery_readback_validated",
)
EXISTING_NORMALIZATION_DURABLE_BOUNDARIES = (
    "active_existing_directory_fsynced",
    "active_existing_readback_validated",
)
RETIREMENT_DURABLE_BOUNDARIES = (
    "active_retirement_binding_validated",
    "active_retirement_marker_unlinked",
    "active_retirement_directory_fsynced",
    "active_retirement_absence_validated",
)

_MARKER_FIELDS = frozenset(
    {
        "schema",
        "intent_sha256",
        "authority_record_sha256",
        "authority_record",
        "secret_material_recorded",
        "secret_digest_recorded",
        "marker_sha256",
    }
)

_XattrReader = Callable[[int], Sequence[str | bytes]]


class ProductionReleaseActiveTransactionError(RuntimeError):
    """Stable, secret-free active-transaction registry failure."""


def _fail(code: str) -> NoReturn:
    raise ProductionReleaseActiveTransactionError(code) from None


def _checkpoint(_name: str) -> None:
    """Private test seam at each durable registry boundary."""


def _posix_identity(name: str, *, failure_code: str) -> int:
    getter = getattr(os, name, None)
    if not callable(getter):
        _fail(failure_code)
    try:
        value = getter()
    except (OSError, TypeError, ValueError):
        _fail(failure_code)
    if type(value) is not int or value < 0:
        _fail(failure_code)
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Return the only accepted byte representation for registry documents."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError):
        _fail("release_active_transaction_json_invalid")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _decode_canonical(raw: bytes) -> Mapping[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        decoded: dict[str, Any] = {}
        for name, item in items:
            if name in decoded:
                raise ValueError("duplicate key")
            decoded[name] = item
        return decoded

    def constant(_value: str) -> None:
        raise ValueError("non-finite number")

    try:
        decoded = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=constant,
        )
    except (UnicodeError, ValueError, TypeError):
        _fail("release_active_transaction_json_invalid")
    if (
        not isinstance(decoded, Mapping)
        or not raw
        or len(raw) > MAX_MARKER_BYTES
        or canonical_json_bytes(decoded) != raw
    ):
        _fail("release_active_transaction_json_invalid")
    return dict(decoded)


def _build_marker(
    authority_record: Mapping[str, Any],
) -> Mapping[str, Any]:
    try:
        authority = runtime.validate_authority_record(authority_record)
    except (
        runtime.ProductionReleaseUpdateRuntimeError,
        TypeError,
        ValueError,
    ):
        _fail("release_active_transaction_authority_invalid")
    authority = deepcopy(authority)
    intent_sha256 = authority["intent"]["intent_sha256"]
    authority_sha256 = authority["authority_record_sha256"]
    unsigned = {
        "schema": ACTIVE_MARKER_SCHEMA,
        "intent_sha256": intent_sha256,
        "authority_record_sha256": authority_sha256,
        "authority_record": authority,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    marker = {
        **unsigned,
        "marker_sha256": _sha256(canonical_json_bytes(unsigned)),
    }
    return _validate_marker(marker)


def _validate_marker(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _MARKER_FIELDS:
        _fail("release_active_transaction_marker_invalid")
    raw = dict(value)
    authority_value = raw.get("authority_record")
    if not isinstance(authority_value, Mapping):
        _fail("release_active_transaction_marker_invalid")
    try:
        authority = runtime.validate_authority_record(authority_value)
    except (
        runtime.ProductionReleaseUpdateRuntimeError,
        TypeError,
        ValueError,
    ):
        _fail("release_active_transaction_marker_invalid")
    authority = deepcopy(authority)
    unsigned = {
        name: item
        for name, item in raw.items()
        if name != "marker_sha256"
    }
    if (
        raw.get("schema") != ACTIVE_MARKER_SCHEMA
        or raw.get("intent_sha256")
        != authority["intent"]["intent_sha256"]
        or raw.get("authority_record_sha256")
        != authority["authority_record_sha256"]
        or raw.get("authority_record") != authority
        or raw.get("secret_material_recorded") is not False
        or raw.get("secret_digest_recorded") is not False
        or raw.get("marker_sha256")
        != _sha256(canonical_json_bytes(unsigned))
    ):
        _fail("release_active_transaction_marker_invalid")
    return {**raw, "authority_record": authority}


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _identity(value: os.stat_result) -> tuple[int, ...]:
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


def _same_inode(*values: os.stat_result) -> bool:
    if not values:
        return False
    expected = (values[0].st_dev, values[0].st_ino)
    return all(
        (value.st_dev, value.st_ino) == expected
        for value in values[1:]
    )


def _read_descriptor_xattrs(
    descriptor: int,
) -> Sequence[str | bytes]:
    reader = getattr(os, "listxattr", None)
    if not callable(reader):
        _fail("release_active_transaction_extended_metadata_unavailable")
    try:
        return reader(descriptor)
    except (OSError, TypeError, ValueError):
        _fail("release_active_transaction_extended_metadata_unavailable")


def _assert_no_extended_metadata(
    descriptor: int,
    *,
    xattr_reader: _XattrReader,
) -> None:
    try:
        names = xattr_reader(descriptor)
    except ProductionReleaseActiveTransactionError:
        raise
    except (OSError, TypeError, ValueError):
        _fail("release_active_transaction_extended_metadata_unavailable")
    if (
        not isinstance(names, (list, tuple))
        or any(
            not isinstance(name, (str, bytes)) or not name
            for name in names
        )
    ):
        _fail("release_active_transaction_extended_metadata_unavailable")
    if names:
        _fail("release_active_transaction_extended_metadata_invalid")


class _ActiveTransactionRegistry:
    def __init__(
        self,
        *,
        root: Path,
        require_root: bool,
        xattr_reader: _XattrReader,
    ) -> None:
        if (
            not isinstance(root, Path)
            or not root.is_absolute()
            or not root.name
            or root.name in {".", ".."}
            or (require_root and root != PRODUCTION_REGISTRY_ROOT)
        ):
            _fail("release_active_transaction_configuration_invalid")
        if require_root and (
            not sys.platform.startswith("linux")
            or _posix_identity(
                "geteuid",
                failure_code="release_active_transaction_root_required",
            )
            != 0
            or _posix_identity(
                "getegid",
                failure_code="release_active_transaction_root_required",
            )
            != 0
        ):
            _fail("release_active_transaction_root_required")
        self._root = root
        self._require_root = require_root
        self._xattr_reader = xattr_reader
        self._uid = (
            0
            if require_root
            else _posix_identity(
                "geteuid",
                failure_code=(
                    "release_active_transaction_configuration_invalid"
                ),
            )
        )
        if require_root:
            self._gid = 0
        else:
            try:
                parent = os.stat(root.parent, follow_symlinks=False)
            except OSError:
                _fail("release_active_transaction_configuration_invalid")
            if (
                not stat.S_ISDIR(parent.st_mode)
                or stat.S_ISLNK(parent.st_mode)
            ):
                _fail("release_active_transaction_configuration_invalid")
            self._gid = parent.st_gid

    def _trusted_parent(self, value: os.stat_result) -> bool:
        return (
            stat.S_ISDIR(value.st_mode)
            and not stat.S_ISLNK(value.st_mode)
            and value.st_uid == self._uid
            and value.st_gid == self._gid
            and stat.S_IMODE(value.st_mode) & 0o022 == 0
        )

    def _trusted_root(self, value: os.stat_result) -> bool:
        return (
            stat.S_ISDIR(value.st_mode)
            and not stat.S_ISLNK(value.st_mode)
            and value.st_uid == self._uid
            and value.st_gid == self._gid
            and stat.S_IMODE(value.st_mode) == DIRECTORY_MODE
            and value.st_nlink >= 2
        )

    def _trusted_sibling_directory(
        self,
        value: os.stat_result,
    ) -> bool:
        mode = stat.S_IMODE(value.st_mode)
        return (
            stat.S_ISDIR(value.st_mode)
            and not stat.S_ISLNK(value.st_mode)
            and value.st_uid == self._uid
            and value.st_gid == self._gid
            and mode & 0o500 == 0o500
            and mode & 0o022 == 0
        )

    def _trusted_file(
        self,
        value: os.stat_result,
        *,
        expected_links: int,
        allow_empty: bool = False,
    ) -> bool:
        size_valid = (
            0 <= value.st_size <= MAX_MARKER_BYTES
            if allow_empty
            else 0 < value.st_size <= MAX_MARKER_BYTES
        )
        return (
            stat.S_ISREG(value.st_mode)
            and not stat.S_ISLNK(value.st_mode)
            and value.st_uid == self._uid
            and value.st_gid == self._gid
            and stat.S_IMODE(value.st_mode) == FILE_MODE
            and value.st_nlink == expected_links
            and size_valid
        )

    def _open_parent(self) -> int:
        descriptor: int | None = None
        try:
            if (
                not hasattr(os, "O_DIRECTORY")
                or not hasattr(os, "O_CLOEXEC")
                or not hasattr(os, "O_NOFOLLOW")
                or self._root.parent.resolve(strict=True)
                != self._root.parent
            ):
                _fail("release_active_transaction_parent_invalid")
            before = os.stat(
                self._root.parent,
                follow_symlinks=False,
            )
            descriptor = os.open(
                self._root.parent,
                _directory_flags(),
            )
            opened = os.fstat(descriptor)
            after = os.stat(
                self._root.parent,
                follow_symlinks=False,
            )
        except OSError:
            if descriptor is not None:
                os.close(descriptor)
            _fail("release_active_transaction_parent_invalid")
        if (
            not _same_inode(before, opened, after)
            or not self._trusted_parent(before)
            or not self._trusted_parent(opened)
            or not self._trusted_parent(after)
        ):
            os.close(descriptor)
            _fail("release_active_transaction_parent_invalid")
        return descriptor

    def _open_root(
        self,
        *,
        create: bool,
    ) -> tuple[int, int] | None:
        parent_fd = self._open_parent()
        root_fd: int | None = None
        created = False
        try:
            try:
                before = os.stat(
                    self._root.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if not create:
                    os.close(parent_fd)
                    return None
                try:
                    os.mkdir(
                        self._root.name,
                        DIRECTORY_MODE,
                        dir_fd=parent_fd,
                    )
                    created = True
                except FileExistsError:
                    # Another registry opener may have created the exact
                    # fixed root after our no-follow lookup.  Open it and let
                    # the root-directory flock serialize initialization and
                    # all later inventory/recovery decisions.
                    created = False
                before = os.stat(
                    self._root.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            root_fd = os.open(
                self._root.name,
                _directory_flags(),
                dir_fd=parent_fd,
            )
            self._lock_root(root_fd)
            if created:
                os.fchown(root_fd, self._uid, self._gid)
                os.fchmod(root_fd, DIRECTORY_MODE)
            opened = os.fstat(root_fd)
            reached = os.stat(
                self._root.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                (
                    not created
                    and (
                        not _same_inode(before, opened)
                        or not self._trusted_root(before)
                    )
                )
                or not _same_inode(opened, reached)
                or not self._trusted_root(opened)
                or not self._trusted_root(reached)
            ):
                _fail("release_active_transaction_directory_invalid")
            _assert_no_extended_metadata(
                root_fd,
                xattr_reader=self._xattr_reader,
            )
            if created:
                os.fsync(root_fd)
                os.fsync(parent_fd)
            self._verify_binding(parent_fd, root_fd)
            return parent_fd, root_fd
        except ProductionReleaseActiveTransactionError:
            if root_fd is not None:
                os.close(root_fd)
            os.close(parent_fd)
            raise
        except OSError:
            if root_fd is not None:
                os.close(root_fd)
            os.close(parent_fd)
            _fail("release_active_transaction_directory_invalid")
        except BaseException:
            if root_fd is not None:
                os.close(root_fd)
            os.close(parent_fd)
            raise

    def _lock_root(self, root_fd: int) -> None:
        """Serialize registry access below the outer activation authority lock.

        A future production caller must acquire the global authority
        activation lock first and this root-directory lock second.  This
        module intentionally does not acquire that outer lock, so it cannot
        invert the ordering when composed into the boot recovery gate.
        """

        try:
            fcntl.flock(root_fd, fcntl.LOCK_EX)
        except (OSError, TypeError, ValueError):
            _fail("release_active_transaction_lock_unavailable")

    def _verify_binding(self, parent_fd: int, root_fd: int) -> None:
        reopened_parent_fd: int | None = None
        try:
            parent = os.fstat(parent_fd)
            reopened_parent_fd = self._open_parent()
            reopened_parent = os.fstat(reopened_parent_fd)
            opened = os.fstat(root_fd)
            reached = os.stat(
                self._root.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except (OSError, ProductionReleaseActiveTransactionError):
            if reopened_parent_fd is not None:
                os.close(reopened_parent_fd)
            _fail("release_active_transaction_directory_changed")
        if reopened_parent_fd is not None:
            os.close(reopened_parent_fd)
        if (
            not _same_inode(parent, reopened_parent)
            or not _same_inode(opened, reached)
            or not self._trusted_parent(parent)
            or not self._trusted_parent(reopened_parent)
            or not self._trusted_root(opened)
            or not self._trusted_root(reached)
        ):
            _fail("release_active_transaction_directory_changed")
        _assert_no_extended_metadata(
            root_fd,
            xattr_reader=self._xattr_reader,
        )

    def _boundary(
        self,
        name: str,
        parent_fd: int,
        root_fd: int,
    ) -> None:
        _checkpoint(name)
        self._verify_binding(parent_fd, root_fd)

    def _validate_sibling_directory(
        self,
        root_fd: int,
        name: str,
    ) -> None:
        descriptor: int | None = None
        try:
            before = os.stat(
                name,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            descriptor = os.open(
                name,
                _directory_flags(),
                dir_fd=root_fd,
            )
            opened = os.fstat(descriptor)
            reached = os.stat(
                name,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            _assert_no_extended_metadata(
                descriptor,
                xattr_reader=self._xattr_reader,
            )
        except ProductionReleaseActiveTransactionError:
            raise
        except OSError:
            _fail("release_active_transaction_inventory_invalid")
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if (
            not _same_inode(before, opened, reached)
            or not self._trusted_sibling_directory(before)
            or not self._trusted_sibling_directory(opened)
            or not self._trusted_sibling_directory(reached)
        ):
            _fail("release_active_transaction_inventory_invalid")

    def _inventory(self, root_fd: int) -> tuple[bool, bool]:
        try:
            names = os.listdir(root_fd)
        except OSError:
            _fail("release_active_transaction_inventory_invalid")
        allowed = {
            ACTIVE_MARKER_NAME,
            ACTIVE_PENDING_NAME,
            *ALLOWED_SIBLING_DIRECTORIES,
        }
        if (
            len(names) > len(allowed)
            or len(names) != len(set(names))
            or any(name not in allowed for name in names)
        ):
            _fail("release_active_transaction_inventory_invalid")
        for name in sorted(set(names) & ALLOWED_SIBLING_DIRECTORIES):
            self._validate_sibling_directory(root_fd, name)
        return (
            ACTIVE_MARKER_NAME in names,
            ACTIVE_PENDING_NAME in names,
        )

    def _open_marker_file(
        self,
        root_fd: int,
        name: str,
        *,
        expected_links: int,
    ) -> tuple[int, bytes, Mapping[str, Any], os.stat_result]:
        descriptor: int | None = None
        try:
            before = os.stat(
                name,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            if not self._trusted_file(
                before,
                expected_links=expected_links,
            ):
                _fail("release_active_transaction_file_invalid")
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_fd,
            )
            opened = os.fstat(descriptor)
            chunks = bytearray()
            while len(chunks) <= MAX_MARKER_BYTES:
                chunk = os.read(
                    descriptor,
                    min(
                        1024 * 1024,
                        MAX_MARKER_BYTES + 1 - len(chunks),
                    ),
                )
                if not chunk:
                    break
                chunks.extend(chunk)
            raw = bytes(chunks)
            after = os.fstat(descriptor)
            reached = os.stat(
                name,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            _assert_no_extended_metadata(
                descriptor,
                xattr_reader=self._xattr_reader,
            )
        except ProductionReleaseActiveTransactionError:
            if descriptor is not None:
                os.close(descriptor)
            raise
        except OSError:
            if descriptor is not None:
                os.close(descriptor)
            _fail("release_active_transaction_file_invalid")
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            raise
        if (
            _identity(before) != _identity(opened)
            or _identity(opened) != _identity(after)
            or _identity(after) != _identity(reached)
            or not self._trusted_file(
                opened,
                expected_links=expected_links,
            )
            or len(raw) != opened.st_size
        ):
            if descriptor is not None:
                os.close(descriptor)
            _fail("release_active_transaction_file_invalid")
        try:
            marker = _validate_marker(_decode_canonical(raw))
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            raise
        if descriptor is None:
            _fail("release_active_transaction_file_invalid")
        return descriptor, raw, marker, after

    def _read_marker_file(
        self,
        root_fd: int,
        name: str,
        *,
        expected_links: int,
    ) -> tuple[bytes, Mapping[str, Any], os.stat_result]:
        descriptor, raw, marker, status = self._open_marker_file(
            root_fd,
            name,
            expected_links=expected_links,
        )
        try:
            return raw, marker, status
        finally:
            os.close(descriptor)

    def _revalidate_pinned_marker(
        self,
        root_fd: int,
        descriptor: int,
        *,
        pinned_status: os.stat_result,
        expected_raw: bytes,
        expected_marker: Mapping[str, Any],
    ) -> None:
        try:
            before = os.fstat(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            chunks = bytearray()
            while len(chunks) <= MAX_MARKER_BYTES:
                chunk = os.read(
                    descriptor,
                    min(
                        1024 * 1024,
                        MAX_MARKER_BYTES + 1 - len(chunks),
                    ),
                )
                if not chunk:
                    break
                chunks.extend(chunk)
            raw = bytes(chunks)
            after = os.fstat(descriptor)
            reached = os.stat(
                ACTIVE_MARKER_NAME,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            _assert_no_extended_metadata(
                descriptor,
                xattr_reader=self._xattr_reader,
            )
        except ProductionReleaseActiveTransactionError:
            raise
        except OSError:
            _fail("release_active_transaction_file_changed")
        if (
            _identity(pinned_status) != _identity(before)
            or _identity(before) != _identity(after)
            or _identity(after) != _identity(reached)
            or not _same_inode(pinned_status, before, after, reached)
            or not self._trusted_file(after, expected_links=1)
            or len(raw) != after.st_size
        ):
            _fail("release_active_transaction_file_changed")
        marker = _validate_marker(_decode_canonical(raw))
        self._assert_expected(
            raw,
            marker,
            expected_raw=expected_raw,
            expected_marker=expected_marker,
        )

    def _assert_pending_shape(
        self,
        root_fd: int,
        *,
        expected_links: int,
    ) -> None:
        descriptor: int | None = None
        try:
            before = os.stat(
                ACTIVE_PENDING_NAME,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            if not self._trusted_file(
                before,
                expected_links=expected_links,
                allow_empty=True,
            ):
                _fail("release_active_transaction_recovery_invalid")
            descriptor = os.open(
                ACTIVE_PENDING_NAME,
                os.O_RDONLY
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_fd,
            )
            opened = os.fstat(descriptor)
            reached = os.stat(
                ACTIVE_PENDING_NAME,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            _assert_no_extended_metadata(
                descriptor,
                xattr_reader=self._xattr_reader,
            )
        except ProductionReleaseActiveTransactionError:
            raise
        except OSError:
            _fail("release_active_transaction_recovery_invalid")
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if (
            _identity(before) != _identity(opened)
            or _identity(opened) != _identity(reached)
            or not self._trusted_file(
                opened,
                expected_links=expected_links,
                allow_empty=True,
            )
        ):
            _fail("release_active_transaction_recovery_invalid")

    def _assert_expected(
        self,
        raw: bytes,
        marker: Mapping[str, Any],
        *,
        expected_raw: bytes,
        expected_marker: Mapping[str, Any],
    ) -> None:
        if (
            marker.get("intent_sha256")
            != expected_marker.get("intent_sha256")
        ):
            _fail("release_active_transaction_conflict")
        if raw != expected_raw or marker != expected_marker:
            _fail("release_active_transaction_exact_replay_conflict")

    def _discard_uncommitted_pending(
        self,
        parent_fd: int,
        root_fd: int,
    ) -> None:
        self._assert_pending_shape(root_fd, expected_links=1)
        self._verify_binding(parent_fd, root_fd)
        try:
            os.unlink(ACTIVE_PENDING_NAME, dir_fd=root_fd)
            self._boundary(
                "active_recovery_uncommitted_pending_removed",
                parent_fd,
                root_fd,
            )
            os.fsync(root_fd)
            self._boundary(
                "active_recovery_uncommitted_cleanup_fsynced",
                parent_fd,
                root_fd,
            )
        except ProductionReleaseActiveTransactionError:
            raise
        except OSError:
            _fail("release_active_transaction_recovery_invalid")

    def _recover_linked_pending(
        self,
        parent_fd: int,
        root_fd: int,
        *,
        expected_raw: bytes,
        expected_marker: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        try:
            pending_status = os.stat(
                ACTIVE_PENDING_NAME,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            final_status = os.stat(
                ACTIVE_MARKER_NAME,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
        except OSError:
            _fail("release_active_transaction_recovery_invalid")
        if (
            pending_status.st_nlink != 2
            or final_status.st_nlink != 2
            or (pending_status.st_dev, pending_status.st_ino)
            != (final_status.st_dev, final_status.st_ino)
        ):
            _fail("release_active_transaction_recovery_invalid")
        pending_raw, pending, _pending_status = self._read_marker_file(
            root_fd,
            ACTIVE_PENDING_NAME,
            expected_links=2,
        )
        final_raw, final, _final_status = self._read_marker_file(
            root_fd,
            ACTIVE_MARKER_NAME,
            expected_links=2,
        )
        if pending_raw != final_raw or pending != final:
            _fail("release_active_transaction_recovery_invalid")
        self._assert_expected(
            final_raw,
            final,
            expected_raw=expected_raw,
            expected_marker=expected_marker,
        )
        try:
            os.fsync(root_fd)
            self._boundary(
                "active_recovery_final_directory_fsynced",
                parent_fd,
                root_fd,
            )
            os.unlink(ACTIVE_PENDING_NAME, dir_fd=root_fd)
            self._boundary(
                "active_recovery_pending_unlinked",
                parent_fd,
                root_fd,
            )
            os.fsync(root_fd)
            self._boundary(
                "active_recovery_cleanup_directory_fsynced",
                parent_fd,
                root_fd,
            )
        except ProductionReleaseActiveTransactionError:
            raise
        except OSError:
            _fail("release_active_transaction_recovery_invalid")
        final_raw, final, _final_status = self._read_marker_file(
            root_fd,
            ACTIVE_MARKER_NAME,
            expected_links=1,
        )
        self._assert_expected(
            final_raw,
            final,
            expected_raw=expected_raw,
            expected_marker=expected_marker,
        )
        self._boundary(
            "active_recovery_readback_validated",
            parent_fd,
            root_fd,
        )
        return final

    def _publish(
        self,
        parent_fd: int,
        root_fd: int,
        *,
        expected_raw: bytes,
        expected_marker: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                ACTIVE_PENDING_NAME,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                FILE_MODE,
                dir_fd=root_fd,
            )
            os.fchown(descriptor, self._uid, self._gid)
            os.fchmod(descriptor, FILE_MODE)
            _checkpoint("active_pending_created")
            offset = 0
            while offset < len(expected_raw):
                written = os.write(descriptor, expected_raw[offset:])
                if written <= 0:
                    raise OSError("short active marker write")
                offset += written
            _checkpoint("active_pending_written")
            _assert_no_extended_metadata(
                descriptor,
                xattr_reader=self._xattr_reader,
            )
            status = os.fstat(descriptor)
            if not self._trusted_file(status, expected_links=1):
                _fail("release_active_transaction_write_failed")
            os.fsync(descriptor)
            _checkpoint("active_pending_file_fsynced")
        except ProductionReleaseActiveTransactionError:
            raise
        except OSError:
            _fail("release_active_transaction_write_failed")
        finally:
            if descriptor is not None:
                os.close(descriptor)
        try:
            os.fsync(root_fd)
            self._boundary(
                "active_pending_directory_fsynced",
                parent_fd,
                root_fd,
            )
            os.link(
                ACTIVE_PENDING_NAME,
                ACTIVE_MARKER_NAME,
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
                follow_symlinks=False,
            )
            self._boundary(
                "active_final_linked",
                parent_fd,
                root_fd,
            )
            os.fsync(root_fd)
            self._boundary(
                "active_final_directory_fsynced",
                parent_fd,
                root_fd,
            )
            os.unlink(ACTIVE_PENDING_NAME, dir_fd=root_fd)
            self._boundary(
                "active_pending_unlinked",
                parent_fd,
                root_fd,
            )
            os.fsync(root_fd)
            self._boundary(
                "active_cleanup_directory_fsynced",
                parent_fd,
                root_fd,
            )
        except FileExistsError:
            _fail("release_active_transaction_conflict")
        except ProductionReleaseActiveTransactionError:
            raise
        except OSError:
            _fail("release_active_transaction_write_failed")
        final_present, pending_present = self._inventory(root_fd)
        if not final_present or pending_present:
            _fail("release_active_transaction_inventory_invalid")
        raw, marker, _marker_status = self._read_marker_file(
            root_fd,
            ACTIVE_MARKER_NAME,
            expected_links=1,
        )
        self._assert_expected(
            raw,
            marker,
            expected_raw=expected_raw,
            expected_marker=expected_marker,
        )
        self._boundary(
            "active_readback_validated",
            parent_fd,
            root_fd,
        )
        return marker

    def create_or_replay(
        self,
        *,
        authority_record: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        expected_marker = _build_marker(authority_record)
        expected_raw = canonical_json_bytes(expected_marker)
        if not 0 < len(expected_raw) <= MAX_MARKER_BYTES:
            _fail("release_active_transaction_marker_invalid")
        opened = self._open_root(create=True)
        if opened is None:
            _fail("release_active_transaction_directory_invalid")
        parent_fd, root_fd = opened
        try:
            final_present, pending_present = self._inventory(root_fd)
            self._verify_binding(parent_fd, root_fd)
            if pending_present and final_present:
                return deepcopy(
                    self._recover_linked_pending(
                        parent_fd,
                        root_fd,
                        expected_raw=expected_raw,
                        expected_marker=expected_marker,
                    )
                )
            if pending_present:
                self._discard_uncommitted_pending(
                    parent_fd,
                    root_fd,
                )
                return deepcopy(
                    self._publish(
                        parent_fd,
                        root_fd,
                        expected_raw=expected_raw,
                        expected_marker=expected_marker,
                    )
                )
            if final_present:
                raw, marker, _marker_status = self._read_marker_file(
                    root_fd,
                    ACTIVE_MARKER_NAME,
                    expected_links=1,
                )
                self._assert_expected(
                    raw,
                    marker,
                    expected_raw=expected_raw,
                    expected_marker=expected_marker,
                )
                self._verify_binding(parent_fd, root_fd)
                if self._inventory(root_fd) != (True, False):
                    _fail("release_active_transaction_inventory_invalid")
                return deepcopy(marker)
            return deepcopy(
                self._publish(
                    parent_fd,
                    root_fd,
                    expected_raw=expected_raw,
                    expected_marker=expected_marker,
                )
            )
        finally:
            os.close(root_fd)
            os.close(parent_fd)

    def recover_existing(self) -> Mapping[str, Any] | None:
        """Normalize and discover existing publication state without creating.

        A one-link pending inode has never been published and is discarded.
        A two-link pending/final pair is the exact committed marker whose
        directory durability and cleanup barriers are replayed.  A clean final
        marker is fsynced before it is returned so recovery never begins from a
        publication whose last directory barrier may have been interrupted.
        """

        opened = self._open_root(create=False)
        if opened is None:
            return None
        parent_fd, root_fd = opened
        try:
            final_present, pending_present = self._inventory(root_fd)
            self._verify_binding(parent_fd, root_fd)
            if pending_present and final_present:
                pending_raw, pending, _pending_status = self._read_marker_file(
                    root_fd,
                    ACTIVE_PENDING_NAME,
                    expected_links=2,
                )
                final_raw, marker, _marker_status = self._read_marker_file(
                    root_fd,
                    ACTIVE_MARKER_NAME,
                    expected_links=2,
                )
                if pending_raw != final_raw or pending != marker:
                    _fail("release_active_transaction_recovery_invalid")
                return deepcopy(
                    self._recover_linked_pending(
                        parent_fd,
                        root_fd,
                        expected_raw=final_raw,
                        expected_marker=marker,
                    )
                )
            if pending_present:
                self._discard_uncommitted_pending(
                    parent_fd,
                    root_fd,
                )
                self._verify_binding(parent_fd, root_fd)
                if self._inventory(root_fd) != (False, False):
                    _fail("release_active_transaction_inventory_invalid")
                return None
            try:
                os.fsync(root_fd)
                self._boundary(
                    "active_existing_directory_fsynced",
                    parent_fd,
                    root_fd,
                )
            except ProductionReleaseActiveTransactionError:
                raise
            except OSError:
                _fail("release_active_transaction_recovery_invalid")
            if not final_present:
                if self._inventory(root_fd) != (False, False):
                    _fail("release_active_transaction_inventory_invalid")
                self._boundary(
                    "active_existing_readback_validated",
                    parent_fd,
                    root_fd,
                )
                return None
            raw, marker, _marker_status = self._read_marker_file(
                root_fd,
                ACTIVE_MARKER_NAME,
                expected_links=1,
            )
            self._verify_binding(parent_fd, root_fd)
            if self._inventory(root_fd) != (True, False):
                _fail("release_active_transaction_inventory_invalid")
            self._boundary(
                "active_existing_readback_validated",
                parent_fd,
                root_fd,
            )
            if raw != canonical_json_bytes(marker):
                _fail("release_active_transaction_marker_invalid")
            return deepcopy(marker)
        finally:
            os.close(root_fd)
            os.close(parent_fd)

    def read(self) -> Mapping[str, Any]:
        opened = self._open_root(create=False)
        if opened is None:
            _fail("release_active_transaction_not_found")
        parent_fd, root_fd = opened
        try:
            final_present, pending_present = self._inventory(root_fd)
            self._verify_binding(parent_fd, root_fd)
            if not final_present:
                if pending_present:
                    _fail("release_active_transaction_recovery_required")
                _fail("release_active_transaction_not_found")
            if pending_present:
                try:
                    pending_status = os.stat(
                        ACTIVE_PENDING_NAME,
                        dir_fd=root_fd,
                        follow_symlinks=False,
                    )
                    final_status = os.stat(
                        ACTIVE_MARKER_NAME,
                        dir_fd=root_fd,
                        follow_symlinks=False,
                    )
                except OSError:
                    _fail("release_active_transaction_recovery_invalid")
                if (
                    pending_status.st_nlink != 2
                    or final_status.st_nlink != 2
                    or (pending_status.st_dev, pending_status.st_ino)
                    != (final_status.st_dev, final_status.st_ino)
                ):
                    _fail("release_active_transaction_recovery_invalid")
                pending_raw, pending, _pending_status = self._read_marker_file(
                    root_fd,
                    ACTIVE_PENDING_NAME,
                    expected_links=2,
                )
                final_raw, marker, _marker_status = self._read_marker_file(
                    root_fd,
                    ACTIVE_MARKER_NAME,
                    expected_links=2,
                )
                if pending_raw != final_raw or pending != marker:
                    _fail("release_active_transaction_recovery_invalid")
                self._verify_binding(parent_fd, root_fd)
                if self._inventory(root_fd) != (True, True):
                    _fail("release_active_transaction_inventory_invalid")
                return deepcopy(marker)
            _raw, marker, _marker_status = self._read_marker_file(
                root_fd,
                ACTIVE_MARKER_NAME,
                expected_links=1,
            )
            self._verify_binding(parent_fd, root_fd)
            if self._inventory(root_fd) != (True, False):
                _fail("release_active_transaction_inventory_invalid")
            return deepcopy(marker)
        finally:
            os.close(root_fd)
            os.close(parent_fd)

    def retire_exact(
        self,
        *,
        authority_record: Mapping[str, Any],
    ) -> None:
        """Retire only the exact clean marker selected by ``authority_record``.

        The caller owns the outer activation lock and must prove terminal host
        state before entering this destructive boundary.  The immutable
        transaction journal remains the audit record; only its active pointer
        is removed.
        """

        expected_marker = _build_marker(authority_record)
        expected_raw = canonical_json_bytes(expected_marker)
        opened = self._open_root(create=False)
        if opened is None:
            _fail("release_active_transaction_not_found")
        parent_fd, root_fd = opened
        marker_fd: int | None = None
        try:
            final_present, pending_present = self._inventory(root_fd)
            self._verify_binding(parent_fd, root_fd)
            if not final_present:
                if pending_present:
                    _fail("release_active_transaction_recovery_required")
                _fail("release_active_transaction_not_found")
            if pending_present:
                _fail("release_active_transaction_recovery_required")
            marker_fd, raw, marker, pinned_status = self._open_marker_file(
                root_fd,
                ACTIVE_MARKER_NAME,
                expected_links=1,
            )
            self._assert_expected(
                raw,
                marker,
                expected_raw=expected_raw,
                expected_marker=expected_marker,
            )
            self._boundary(
                "active_retirement_binding_validated",
                parent_fd,
                root_fd,
            )
            self._verify_binding(parent_fd, root_fd)
            if self._inventory(root_fd) != (True, False):
                _fail("release_active_transaction_inventory_invalid")
            # Re-read after every other pre-unlink check and immediately before
            # the unlink so a path replacement cannot turn validation of one
            # marker into retirement of another.
            self._revalidate_pinned_marker(
                root_fd,
                marker_fd,
                pinned_status=pinned_status,
                expected_raw=expected_raw,
                expected_marker=expected_marker,
            )
            try:
                os.unlink(ACTIVE_MARKER_NAME, dir_fd=root_fd)
                unlinked = os.fstat(marker_fd)
                if (
                    not _same_inode(pinned_status, unlinked)
                    or unlinked.st_nlink != 0
                    or not stat.S_ISREG(unlinked.st_mode)
                    or stat.S_IMODE(unlinked.st_mode) != FILE_MODE
                    or unlinked.st_uid != self._uid
                    or unlinked.st_gid != self._gid
                    or unlinked.st_size != pinned_status.st_size
                ):
                    _fail("release_active_transaction_retirement_invalid")
                self._boundary(
                    "active_retirement_marker_unlinked",
                    parent_fd,
                    root_fd,
                )
                os.fsync(root_fd)
                self._boundary(
                    "active_retirement_directory_fsynced",
                    parent_fd,
                    root_fd,
                )
            except ProductionReleaseActiveTransactionError:
                raise
            except OSError:
                _fail("release_active_transaction_retirement_invalid")
            if self._inventory(root_fd) != (False, False):
                _fail("release_active_transaction_retirement_invalid")
            self._boundary(
                "active_retirement_absence_validated",
                parent_fd,
                root_fd,
            )
        finally:
            if marker_fd is not None:
                os.close(marker_fd)
            os.close(root_fd)
            os.close(parent_fd)


def create_or_replay_active_transaction(
    *,
    authority_record: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Create or exactly replay the sole production active marker."""

    registry = _ActiveTransactionRegistry(
        root=PRODUCTION_REGISTRY_ROOT,
        require_root=True,
        xattr_reader=_read_descriptor_xattrs,
    )
    return registry.create_or_replay(authority_record=authority_record)


def read_active_transaction() -> Mapping[str, Any]:
    """Read the existing production marker without creating filesystem state."""

    registry = _ActiveTransactionRegistry(
        root=PRODUCTION_REGISTRY_ROOT,
        require_root=True,
        xattr_reader=_read_descriptor_xattrs,
    )
    return registry.read()


def recover_existing_active_transaction() -> Mapping[str, Any] | None:
    """Normalize and return existing production state without creating it."""

    registry = _ActiveTransactionRegistry(
        root=PRODUCTION_REGISTRY_ROOT,
        require_root=True,
        xattr_reader=_read_descriptor_xattrs,
    )
    return registry.recover_existing()


def retire_active_transaction(
    *,
    authority_record: Mapping[str, Any],
) -> None:
    """Retire the exact production marker after external terminal proof."""

    registry = _ActiveTransactionRegistry(
        root=PRODUCTION_REGISTRY_ROOT,
        require_root=True,
        xattr_reader=_read_descriptor_xattrs,
    )
    registry.retire_exact(authority_record=authority_record)


def _create_or_replay_for_test(
    root: Path,
    *,
    authority_record: Mapping[str, Any],
    xattr_reader: _XattrReader | None = None,
) -> Mapping[str, Any]:
    registry = _ActiveTransactionRegistry(
        root=root,
        require_root=False,
        xattr_reader=(
            (lambda _descriptor: ())
            if xattr_reader is None
            else xattr_reader
        ),
    )
    return registry.create_or_replay(authority_record=authority_record)


def _read_for_test(
    root: Path,
    *,
    xattr_reader: _XattrReader | None = None,
) -> Mapping[str, Any]:
    registry = _ActiveTransactionRegistry(
        root=root,
        require_root=False,
        xattr_reader=(
            (lambda _descriptor: ())
            if xattr_reader is None
            else xattr_reader
        ),
    )
    return registry.read()


def _recover_existing_for_test(
    root: Path,
    *,
    xattr_reader: _XattrReader | None = None,
) -> Mapping[str, Any] | None:
    registry = _ActiveTransactionRegistry(
        root=root,
        require_root=False,
        xattr_reader=(
            (lambda _descriptor: ())
            if xattr_reader is None
            else xattr_reader
        ),
    )
    return registry.recover_existing()


def _retire_for_test(
    root: Path,
    *,
    authority_record: Mapping[str, Any],
    xattr_reader: _XattrReader | None = None,
) -> None:
    registry = _ActiveTransactionRegistry(
        root=root,
        require_root=False,
        xattr_reader=(
            (lambda _descriptor: ())
            if xattr_reader is None
            else xattr_reader
        ),
    )
    registry.retire_exact(authority_record=authority_record)


__all__ = [
    "ACTIVE_MARKER_NAME",
    "ACTIVE_MARKER_SCHEMA",
    "ACTIVE_PENDING_NAME",
    "ALLOWED_SIBLING_DIRECTORIES",
    "DIRECTORY_MODE",
    "EXISTING_NORMALIZATION_DURABLE_BOUNDARIES",
    "FILE_MODE",
    "LINKED_RECOVERY_DURABLE_BOUNDARIES",
    "MAX_MARKER_BYTES",
    "PRODUCTION_REGISTRY_ROOT",
    "PUBLICATION_DURABLE_BOUNDARIES",
    "ProductionReleaseActiveTransactionError",
    "RETIREMENT_DURABLE_BOUNDARIES",
    "UNCOMMITTED_RECOVERY_DURABLE_BOUNDARIES",
    "canonical_json_bytes",
    "create_or_replay_active_transaction",
    "read_active_transaction",
    "recover_existing_active_transaction",
    "retire_active_transaction",
]
