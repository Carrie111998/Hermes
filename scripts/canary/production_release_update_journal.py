#!/usr/bin/env python3
"""Root-owned append-only filesystem journal for one release update.

Each instance is bound to one signed authority record and one fixed transaction
directory.  The authority record is durably published before event zero.
Authority and event publication use private pending names plus no-replace hard
links so recovery can distinguish unpublished one-link inodes from exact
two-link publications whose directory durability barrier still needs retrying.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from scripts.canary import production_release_update_runtime as runtime


PRODUCTION_JOURNAL_ROOT = Path(
    "/var/lib/muncho-production-release-update/transactions"
)
DIRECTORY_MODE = 0o700
FILE_MODE = 0o400
MAX_EVENT_BYTES = 16 * 1024 * 1024
SEQUENCE_WIDTH = 8
ZERO_SEQUENCE_NAME = "0" * SEQUENCE_WIDTH
AUTHORITY_FILE_NAME = "authority-record.json"
AUTHORITY_PENDING_NAME = ".authority-record.pending"

_FINAL_NAME = re.compile(r"^([0-9]{8})\.json$")
_PENDING_NAME = re.compile(r"^\.([0-9]{8})\.pending$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

DURABLE_BOUNDARIES = (
    "transaction_directory_created",
    "transaction_directory_fsynced",
    "transaction_parent_fsynced",
    "pending_created",
    "pending_written",
    "pending_file_fsynced",
    "pending_directory_fsynced",
    "final_linked",
    "final_directory_fsynced",
    "pending_unlinked",
    "cleanup_directory_fsynced",
    "readback_validated",
)
RECOVERY_DURABLE_BOUNDARIES = (
    "recovery_uncommitted_pending_binding_validated",
    "recovery_uncommitted_pending_removed",
    "recovery_uncommitted_pending_cleanup_fsynced",
    "recovery_final_directory_fsynced",
    "recovery_pending_unlinked",
    "recovery_cleanup_directory_fsynced",
    "recovery_readback_validated",
)
AUTHORITY_DURABLE_BOUNDARIES = (
    "authority_pending_created",
    "authority_pending_written",
    "authority_pending_file_fsynced",
    "authority_pending_directory_fsynced",
    "authority_final_linked",
    "authority_final_directory_fsynced",
    "authority_pending_unlinked",
    "authority_cleanup_directory_fsynced",
    "authority_readback_validated",
)
AUTHORITY_RECOVERY_DURABLE_BOUNDARIES = (
    "authority_recovery_uncommitted_pending_removed",
    "authority_recovery_uncommitted_pending_cleanup_fsynced",
    "authority_recovery_final_directory_fsynced",
    "authority_recovery_pending_unlinked",
    "authority_recovery_cleanup_directory_fsynced",
    "authority_recovery_readback_validated",
)


class ProductionReleaseUpdateJournalError(RuntimeError):
    """Stable, secret-free release-update journal failure."""


def _posix_effective_uid(*, failure_code: str) -> int:
    getter = getattr(os, "geteuid", None)
    if not callable(getter):
        raise ProductionReleaseUpdateJournalError(failure_code)
    try:
        value = getter()
    except (OSError, TypeError, ValueError) as exc:
        raise ProductionReleaseUpdateJournalError(
            failure_code
        ) from exc
    if type(value) is not int or value < 0:
        raise ProductionReleaseUpdateJournalError(failure_code)
    return value


def _posix_effective_gid(*, failure_code: str) -> int:
    getter = getattr(os, "getegid", None)
    if not callable(getter):
        raise ProductionReleaseUpdateJournalError(failure_code)
    try:
        value = getter()
    except (OSError, TypeError, ValueError) as exc:
        raise ProductionReleaseUpdateJournalError(
            failure_code
        ) from exc
    if type(value) is not int or value < 0:
        raise ProductionReleaseUpdateJournalError(failure_code)
    return value


def _checkpoint(_name: str) -> None:
    """Test seam at each durable journal boundary."""


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProductionReleaseUpdateJournalError(
            "release_update_journal_json_invalid"
        ) from exc


def _decode_canonical(raw: bytes) -> Mapping[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for name, item in items:
            if name in value:
                raise ValueError("duplicate key")
            value[name] = item
        return value

    def constant(_value: str) -> None:
        raise ValueError("non-finite number")

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=constant,
        )
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ProductionReleaseUpdateJournalError(
            "release_update_journal_json_invalid"
        ) from exc
    if (
        not isinstance(value, Mapping)
        or not raw
        or len(raw) > MAX_EVENT_BYTES
        or canonical_json_bytes(value) != raw
    ):
        raise ProductionReleaseUpdateJournalError(
            "release_update_journal_json_invalid"
        )
    return dict(value)


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


_XattrReader = Callable[[int], Sequence[str | bytes]]


def _read_descriptor_xattrs(
    descriptor: int,
) -> Sequence[str | bytes]:
    reader = getattr(os, "listxattr", None)
    if not callable(reader):
        raise ProductionReleaseUpdateJournalError(
            "release_update_journal_extended_metadata_unavailable"
        )
    try:
        return reader(descriptor)
    except (OSError, TypeError, ValueError) as exc:
        raise ProductionReleaseUpdateJournalError(
            "release_update_journal_extended_metadata_unavailable"
        ) from exc


def _assert_no_extended_metadata(
    descriptor: int,
    *,
    xattr_reader: _XattrReader,
) -> None:
    try:
        names = xattr_reader(descriptor)
    except ProductionReleaseUpdateJournalError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise ProductionReleaseUpdateJournalError(
            "release_update_journal_extended_metadata_unavailable"
        ) from exc
    if not isinstance(names, (list, tuple)) or any(
        not isinstance(name, (str, bytes)) or not name
        for name in names
    ):
        raise ProductionReleaseUpdateJournalError(
            "release_update_journal_extended_metadata_unavailable"
        )
    if names:
        raise ProductionReleaseUpdateJournalError(
            "release_update_journal_extended_metadata_invalid"
        )


def _assert_name_no_extended_metadata(
    directory_descriptor: int,
    name: str,
    *,
    xattr_reader: _XattrReader,
) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_descriptor,
        )
        _assert_no_extended_metadata(
            descriptor,
            xattr_reader=xattr_reader,
        )
    except ProductionReleaseUpdateJournalError:
        raise
    except OSError as exc:
        raise ProductionReleaseUpdateJournalError(
            "release_update_journal_extended_metadata_unavailable"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


class ReleaseUpdateJournal:
    """Durable event store satisfying the Stage C journal protocol."""

    def __init__(
        self,
        *,
        authority_record: Mapping[str, Any],
    ) -> None:
        """Bind fresh execution to the fixed root-owned transaction directory."""

        self._configure(
            transaction_directory=None,
            authority_record=authority_record,
            require_root=True,
            create_transaction=True,
            xattr_reader=_read_descriptor_xattrs,
        )

    @classmethod
    def open_existing(
        cls,
        *,
        authority_record: Mapping[str, Any],
    ) -> ReleaseUpdateJournal:
        """Bind recovery to an existing exact production journal.

        Activation must finish a clean, final authority header before it
        publishes the active marker that makes this recovery path discoverable.
        """

        instance = object.__new__(cls)
        instance._configure(
            transaction_directory=None,
            authority_record=authority_record,
            require_root=True,
            create_transaction=False,
            xattr_reader=_read_descriptor_xattrs,
        )
        return instance

    @classmethod
    def _for_test(
        cls,
        transaction_directory: Path,
        *,
        authority_record: Mapping[str, Any],
        xattr_reader: _XattrReader | None = None,
    ) -> ReleaseUpdateJournal:
        """Construct a filesystem journal without production root authority."""

        instance = object.__new__(cls)
        instance._configure(
            transaction_directory=transaction_directory,
            authority_record=authority_record,
            require_root=False,
            create_transaction=True,
            xattr_reader=(
                (lambda _descriptor: ())
                if xattr_reader is None
                else xattr_reader
            ),
        )
        return instance

    @classmethod
    def _open_existing_for_test(
        cls,
        transaction_directory: Path,
        *,
        authority_record: Mapping[str, Any],
        xattr_reader: _XattrReader | None = None,
    ) -> ReleaseUpdateJournal:
        """Bind recovery to a private existing test journal."""

        instance = object.__new__(cls)
        instance._configure(
            transaction_directory=transaction_directory,
            authority_record=authority_record,
            require_root=False,
            create_transaction=False,
            xattr_reader=(
                (lambda _descriptor: ())
                if xattr_reader is None
                else xattr_reader
            ),
        )
        return instance

    def _configure(
        self,
        *,
        transaction_directory: Path | None,
        authority_record: Mapping[str, Any],
        require_root: bool,
        create_transaction: bool,
        xattr_reader: _XattrReader,
    ) -> None:
        try:
            validated_authority = runtime.validate_authority_record(
                authority_record
            )
        except runtime.ProductionReleaseUpdateRuntimeError as exc:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_authority_invalid"
            ) from exc
        validated_authority = deepcopy(validated_authority)
        validated_intent = validated_authority["intent"]
        expected = (
            PRODUCTION_JOURNAL_ROOT
            / str(validated_intent["intent_sha256"])
        )
        selected = expected if transaction_directory is None else (
            transaction_directory
        )
        if (
            not isinstance(selected, Path)
            or not selected.is_absolute()
            or not selected.name
            or selected.name in {".", ".."}
            or (
                require_root
                and selected != expected
            )
        ):
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_configuration_invalid"
            )
        if require_root and (
            not sys.platform.startswith("linux")
            or _posix_effective_uid(
                failure_code="release_update_journal_root_required"
            )
            != 0
            or _posix_effective_gid(
                failure_code="release_update_journal_root_required"
            )
            != 0
        ):
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_root_required"
            )
        self._transaction_directory = selected
        self._authority_record = validated_authority
        self._intent = validated_intent
        self._require_root = require_root
        self._create_transaction = create_transaction
        self._xattr_reader = xattr_reader
        self._uid = (
            0
            if require_root
            else _posix_effective_uid(
                failure_code=(
                    "release_update_journal_configuration_invalid"
                )
            )
        )
        if require_root:
            self._gid = 0
        else:
            try:
                self._gid = os.stat(
                    selected.parent,
                    follow_symlinks=False,
                ).st_gid
            except OSError as exc:
                raise ProductionReleaseUpdateJournalError(
                    "release_update_journal_configuration_invalid"
                ) from exc

    @property
    def transaction_directory(self) -> Path:
        return self._transaction_directory

    @property
    def intent(self) -> Mapping[str, Any]:
        return dict(self._intent)

    @property
    def authority_record(self) -> Mapping[str, Any]:
        return deepcopy(self._authority_record)

    def _trusted_owner(self, value: os.stat_result) -> bool:
        return (
            value.st_uid == self._uid
            and value.st_gid == self._gid
        )

    def _trusted_directory(self, value: os.stat_result) -> bool:
        return (
            stat.S_ISDIR(value.st_mode)
            and not stat.S_ISLNK(value.st_mode)
            and stat.S_IMODE(value.st_mode) == DIRECTORY_MODE
            and self._trusted_owner(value)
        )

    def _open_absolute_parent(
        self,
        *,
        missing_ok: bool = False,
    ) -> int | None:
        parts = self._transaction_directory.parent.parts
        if not parts or parts[0] != os.path.sep:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_configuration_invalid"
            )
        descriptor: int | None = None
        try:
            descriptor = os.open(os.path.sep, _directory_flags())
            for component in parts[1:]:
                child = os.open(
                    component,
                    _directory_flags(),
                    dir_fd=descriptor,
                )
                opened = os.fstat(child)
                if not stat.S_ISDIR(opened.st_mode):
                    os.close(child)
                    raise ProductionReleaseUpdateJournalError(
                        "release_update_journal_directory_invalid"
                    )
                os.close(descriptor)
                descriptor = child
            parent = os.fstat(descriptor)
            if not self._trusted_directory(parent):
                raise ProductionReleaseUpdateJournalError(
                    "release_update_journal_directory_invalid"
                )
            _assert_no_extended_metadata(
                descriptor,
                xattr_reader=self._xattr_reader,
            )
            return descriptor
        except FileNotFoundError as exc:
            if descriptor is not None:
                os.close(descriptor)
            if missing_ok:
                return None
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_directory_invalid"
            ) from exc
        except ProductionReleaseUpdateJournalError:
            if descriptor is not None:
                os.close(descriptor)
            raise
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_directory_invalid"
            ) from exc

    def _open_transaction(
        self,
        *,
        create: bool,
    ) -> tuple[int, int] | None:
        parent_fd = self._open_absolute_parent(missing_ok=not create)
        if parent_fd is None:
            return None
        name = self._transaction_directory.name
        created = False
        try:
            try:
                before = os.stat(
                    name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if not create:
                    os.close(parent_fd)
                    return None
                os.mkdir(name, DIRECTORY_MODE, dir_fd=parent_fd)
                created = True
                before = os.stat(
                    name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            transaction_fd = os.open(
                name,
                _directory_flags(),
                dir_fd=parent_fd,
            )
            opened = os.fstat(transaction_fd)
            after = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if created:
                os.fchown(transaction_fd, self._uid, self._gid)
                os.fchmod(transaction_fd, DIRECTORY_MODE)
                opened = os.fstat(transaction_fd)
                after = os.stat(
                    name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            if (
                (before.st_dev, before.st_ino)
                != (opened.st_dev, opened.st_ino)
                or (opened.st_dev, opened.st_ino)
                != (after.st_dev, after.st_ino)
                or not self._trusted_directory(opened)
                or not self._trusted_directory(after)
            ):
                raise ProductionReleaseUpdateJournalError(
                    "release_update_journal_directory_invalid"
                )
            _assert_no_extended_metadata(
                transaction_fd,
                xattr_reader=self._xattr_reader,
            )
            if created:
                _checkpoint("transaction_directory_created")
            if create:
                os.fsync(transaction_fd)
                self._boundary(
                    "transaction_directory_fsynced",
                    parent_fd,
                    transaction_fd,
                )
                os.fsync(parent_fd)
                self._boundary(
                    "transaction_parent_fsynced",
                    parent_fd,
                    transaction_fd,
                )
            return parent_fd, transaction_fd
        except ProductionReleaseUpdateJournalError:
            try:
                os.close(transaction_fd)
            except (OSError, UnboundLocalError):
                pass
            os.close(parent_fd)
            raise
        except OSError as exc:
            try:
                os.close(transaction_fd)
            except (OSError, UnboundLocalError):
                pass
            os.close(parent_fd)
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_directory_invalid"
            ) from exc
        except BaseException:
            try:
                os.close(transaction_fd)
            except (OSError, UnboundLocalError):
                pass
            os.close(parent_fd)
            raise

    def _verify_binding(self, parent_fd: int, transaction_fd: int) -> None:
        reopened_parent_fd: int | None = None
        try:
            parent = os.fstat(parent_fd)
            reopened_parent_fd = self._open_absolute_parent()
            if reopened_parent_fd is None:
                raise ProductionReleaseUpdateJournalError(
                    "release_update_journal_directory_changed"
                )
            reopened_parent = os.fstat(reopened_parent_fd)
            opened = os.fstat(transaction_fd)
            reachable = os.stat(
                self._transaction_directory.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except (OSError, ProductionReleaseUpdateJournalError) as exc:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_directory_changed"
            ) from exc
        finally:
            if reopened_parent_fd is not None:
                os.close(reopened_parent_fd)
        if (
            (parent.st_dev, parent.st_ino)
            != (reopened_parent.st_dev, reopened_parent.st_ino)
            or (opened.st_dev, opened.st_ino)
            != (reachable.st_dev, reachable.st_ino)
            or not self._trusted_directory(parent)
            or not self._trusted_directory(reopened_parent)
            or not self._trusted_directory(opened)
            or not self._trusted_directory(reachable)
        ):
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_directory_changed"
            )
        _assert_no_extended_metadata(
            parent_fd,
            xattr_reader=self._xattr_reader,
        )
        _assert_no_extended_metadata(
            transaction_fd,
            xattr_reader=self._xattr_reader,
        )

    def _boundary(
        self,
        name: str,
        parent_fd: int,
        transaction_fd: int,
    ) -> None:
        _checkpoint(name)
        self._verify_binding(parent_fd, transaction_fd)

    def _read_canonical_file(
        self,
        transaction_fd: int,
        name: str,
        *,
        expected_links: int,
    ) -> tuple[bytes, Mapping[str, Any]]:
        descriptor: int | None = None
        try:
            before = os.stat(
                name,
                dir_fd=transaction_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(before.st_mode)
                or stat.S_ISLNK(before.st_mode)
                or stat.S_IMODE(before.st_mode) != FILE_MODE
                or not self._trusted_owner(before)
                or before.st_nlink != expected_links
                or not 0 < before.st_size <= MAX_EVENT_BYTES
            ):
                raise ProductionReleaseUpdateJournalError(
                    "release_update_journal_file_invalid"
                )
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=transaction_fd,
            )
            opened = os.fstat(descriptor)
            chunks = bytearray()
            while len(chunks) <= MAX_EVENT_BYTES:
                chunk = os.read(
                    descriptor,
                    min(
                        1024 * 1024,
                        MAX_EVENT_BYTES + 1 - len(chunks),
                    ),
                )
                if not chunk:
                    break
                chunks.extend(chunk)
            raw = bytes(chunks)
            after = os.fstat(descriptor)
            reachable = os.stat(
                name,
                dir_fd=transaction_fd,
                follow_symlinks=False,
            )
            _assert_no_extended_metadata(
                descriptor,
                xattr_reader=self._xattr_reader,
            )
        except OSError as exc:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_file_invalid"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if (
            _identity(before) != _identity(opened)
            or _identity(opened) != _identity(after)
            or _identity(after) != _identity(reachable)
            or not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != FILE_MODE
            or not self._trusted_owner(opened)
            or opened.st_nlink != expected_links
            or not raw
            or len(raw) > MAX_EVENT_BYTES
            or len(raw) != opened.st_size
        ):
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_file_invalid"
            )
        return raw, _decode_canonical(raw)

    def _read_file(
        self,
        transaction_fd: int,
        name: str,
        *,
        expected_links: int,
    ) -> tuple[bytes, Mapping[str, Any]]:
        raw, decoded = self._read_canonical_file(
            transaction_fd,
            name,
            expected_links=expected_links,
        )
        try:
            event = runtime.validate_event(
                decoded,
                intent=self._intent,
            )
        except runtime.ProductionReleaseUpdateRuntimeError as exc:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_event_invalid"
            ) from exc
        return raw, event

    def _read_authority_file(
        self,
        transaction_fd: int,
        name: str,
        *,
        expected_links: int,
    ) -> tuple[bytes, Mapping[str, Any]]:
        raw, decoded = self._read_canonical_file(
            transaction_fd,
            name,
            expected_links=expected_links,
        )
        try:
            record = runtime.validate_authority_record(decoded)
        except runtime.ProductionReleaseUpdateRuntimeError as exc:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_authority_invalid"
            ) from exc
        if record != self._authority_record:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_authority_conflict"
            )
        return raw, record

    def _names(self, transaction_fd: int) -> list[str]:
        try:
            return sorted(os.listdir(transaction_fd))
        except OSError as exc:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_inventory_invalid"
            ) from exc

    def _publish_authority(
        self,
        parent_fd: int,
        transaction_fd: int,
    ) -> None:
        payload = canonical_json_bytes(self._authority_record)
        if not payload or len(payload) > MAX_EVENT_BYTES:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_authority_invalid"
            )
        try:
            descriptor = os.open(
                AUTHORITY_PENDING_NAME,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                FILE_MODE,
                dir_fd=transaction_fd,
            )
        except OSError as exc:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_authority_write_failed"
            ) from exc
        try:
            os.fchown(descriptor, self._uid, self._gid)
            os.fchmod(descriptor, FILE_MODE)
            _checkpoint("authority_pending_created")
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("short authority write")
                offset += written
            _checkpoint("authority_pending_written")
            os.fsync(descriptor)
            _checkpoint("authority_pending_file_fsynced")
        except OSError as exc:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_authority_write_failed"
            ) from exc
        finally:
            os.close(descriptor)
        try:
            os.fsync(transaction_fd)
            self._boundary(
                "authority_pending_directory_fsynced",
                parent_fd,
                transaction_fd,
            )
            os.link(
                AUTHORITY_PENDING_NAME,
                AUTHORITY_FILE_NAME,
                src_dir_fd=transaction_fd,
                dst_dir_fd=transaction_fd,
                follow_symlinks=False,
            )
            self._boundary(
                "authority_final_linked",
                parent_fd,
                transaction_fd,
            )
            os.fsync(transaction_fd)
            self._boundary(
                "authority_final_directory_fsynced",
                parent_fd,
                transaction_fd,
            )
            os.unlink(AUTHORITY_PENDING_NAME, dir_fd=transaction_fd)
            self._boundary(
                "authority_pending_unlinked",
                parent_fd,
                transaction_fd,
            )
            os.fsync(transaction_fd)
            self._boundary(
                "authority_cleanup_directory_fsynced",
                parent_fd,
                transaction_fd,
            )
        except FileExistsError as exc:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_authority_conflict"
            ) from exc
        except OSError as exc:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_authority_write_failed"
            ) from exc
        _raw, readback = self._read_authority_file(
            transaction_fd,
            AUTHORITY_FILE_NAME,
            expected_links=1,
        )
        if readback != self._authority_record:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_authority_readback_invalid"
            )
        self._boundary(
            "authority_readback_validated",
            parent_fd,
            transaction_fd,
        )

    def _discard_uncommitted_authority(
        self,
        parent_fd: int,
        transaction_fd: int,
    ) -> None:
        try:
            status = os.stat(
                AUTHORITY_PENDING_NAME,
                dir_fd=transaction_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_authority_recovery_invalid"
            ) from exc
        if (
            not stat.S_ISREG(status.st_mode)
            or stat.S_ISLNK(status.st_mode)
            or stat.S_IMODE(status.st_mode) != FILE_MODE
            or not self._trusted_owner(status)
            or status.st_nlink != 1
            or not 0 <= status.st_size <= MAX_EVENT_BYTES
        ):
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_authority_recovery_invalid"
            )
        _assert_name_no_extended_metadata(
            transaction_fd,
            AUTHORITY_PENDING_NAME,
            xattr_reader=self._xattr_reader,
        )
        try:
            os.unlink(AUTHORITY_PENDING_NAME, dir_fd=transaction_fd)
            self._boundary(
                "authority_recovery_uncommitted_pending_removed",
                parent_fd,
                transaction_fd,
            )
            os.fsync(transaction_fd)
            self._boundary(
                "authority_recovery_uncommitted_pending_cleanup_fsynced",
                parent_fd,
                transaction_fd,
            )
        except OSError as exc:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_authority_recovery_invalid"
            ) from exc

    def _recover_linked_authority(
        self,
        parent_fd: int,
        transaction_fd: int,
    ) -> None:
        try:
            pending_status = os.stat(
                AUTHORITY_PENDING_NAME,
                dir_fd=transaction_fd,
                follow_symlinks=False,
            )
            final_status = os.stat(
                AUTHORITY_FILE_NAME,
                dir_fd=transaction_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_authority_recovery_invalid"
            ) from exc
        if (
            pending_status.st_nlink != 2
            or final_status.st_nlink != 2
            or (pending_status.st_dev, pending_status.st_ino)
            != (final_status.st_dev, final_status.st_ino)
        ):
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_authority_recovery_invalid"
            )
        pending_raw, pending = self._read_authority_file(
            transaction_fd,
            AUTHORITY_PENDING_NAME,
            expected_links=2,
        )
        final_raw, final = self._read_authority_file(
            transaction_fd,
            AUTHORITY_FILE_NAME,
            expected_links=2,
        )
        if pending_raw != final_raw or pending != final:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_authority_recovery_invalid"
            )
        try:
            os.fsync(transaction_fd)
            self._boundary(
                "authority_recovery_final_directory_fsynced",
                parent_fd,
                transaction_fd,
            )
            os.unlink(AUTHORITY_PENDING_NAME, dir_fd=transaction_fd)
            self._boundary(
                "authority_recovery_pending_unlinked",
                parent_fd,
                transaction_fd,
            )
            os.fsync(transaction_fd)
            self._boundary(
                "authority_recovery_cleanup_directory_fsynced",
                parent_fd,
                transaction_fd,
            )
        except OSError as exc:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_authority_recovery_invalid"
            ) from exc
        _raw, readback = self._read_authority_file(
            transaction_fd,
            AUTHORITY_FILE_NAME,
            expected_links=1,
        )
        if readback != self._authority_record:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_authority_recovery_invalid"
            )
        self._boundary(
            "authority_recovery_readback_validated",
            parent_fd,
            transaction_fd,
        )

    def _ensure_authority_header(
        self,
        parent_fd: int,
        transaction_fd: int,
    ) -> None:
        names = self._names(transaction_fd)
        final_present = AUTHORITY_FILE_NAME in names
        pending_present = AUTHORITY_PENDING_NAME in names
        other_names = [
            name
            for name in names
            if name not in {AUTHORITY_FILE_NAME, AUTHORITY_PENDING_NAME}
        ]
        if pending_present:
            if other_names:
                raise ProductionReleaseUpdateJournalError(
                    "release_update_journal_authority_recovery_invalid"
                )
            if final_present:
                self._recover_linked_authority(
                    parent_fd,
                    transaction_fd,
                )
            else:
                self._discard_uncommitted_authority(
                    parent_fd,
                    transaction_fd,
                )
                self._publish_authority(parent_fd, transaction_fd)
            return
        if not final_present:
            if other_names:
                raise ProductionReleaseUpdateJournalError(
                    "release_update_journal_authority_missing"
                )
            self._publish_authority(parent_fd, transaction_fd)
            return
        self._read_authority_file(
            transaction_fd,
            AUTHORITY_FILE_NAME,
            expected_links=1,
        )

    def _require_existing_authority_header(
        self,
        transaction_fd: int,
    ) -> None:
        names = self._names(transaction_fd)
        if AUTHORITY_FILE_NAME not in names:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_authority_missing"
            )
        if AUTHORITY_PENDING_NAME in names:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_authority_recovery_invalid"
            )
        self._read_authority_file(
            transaction_fd,
            AUTHORITY_FILE_NAME,
            expected_links=1,
        )

    def _inventory(
        self,
        transaction_fd: int,
    ) -> tuple[list[str], str | None]:
        names = self._names(transaction_fd)
        if (
            AUTHORITY_FILE_NAME not in names
            or AUTHORITY_PENDING_NAME in names
        ):
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_authority_missing"
            )
        finals: list[str] = []
        pending: list[str] = []
        for name in names:
            if name == AUTHORITY_FILE_NAME:
                continue
            if _FINAL_NAME.fullmatch(name) is not None:
                finals.append(name)
            elif _PENDING_NAME.fullmatch(name) is not None:
                pending.append(name)
            else:
                raise ProductionReleaseUpdateJournalError(
                    "release_update_journal_inventory_invalid"
                )
        if len(pending) > 1 or len(finals) > 1_000:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_inventory_invalid"
            )
        return finals, None if not pending else pending[0]

    def _load_final_events(
        self,
        transaction_fd: int,
        *,
        linked_sequence: int | None = None,
    ) -> list[Mapping[str, Any]]:
        finals, _pending = self._inventory(transaction_fd)
        events: list[Mapping[str, Any]] = []
        for sequence, name in enumerate(finals):
            if name != f"{sequence:0{SEQUENCE_WIDTH}d}.json":
                raise ProductionReleaseUpdateJournalError(
                    "release_update_journal_sequence_invalid"
                )
            _raw, event = self._read_file(
                transaction_fd,
                name,
                expected_links=(
                    2 if linked_sequence == sequence else 1
                ),
            )
            if event["sequence"] != sequence:
                raise ProductionReleaseUpdateJournalError(
                    "release_update_journal_sequence_invalid"
                )
            events.append(event)
        try:
            runtime.load_state(intent=self._intent, events=events)
        except runtime.ProductionReleaseUpdateRuntimeError as exc:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_sequence_invalid"
            ) from exc
        return events

    def _discard_uncommitted_pending(
        self,
        parent_fd: int,
        transaction_fd: int,
        pending_name: str,
        *,
        final_name: str,
        status: os.stat_result,
    ) -> None:
        try:
            os.stat(
                final_name,
                dir_fd=transaction_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_recovery_invalid"
            ) from exc
        else:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_recovery_invalid"
            )
        if (
            not stat.S_ISREG(status.st_mode)
            or stat.S_ISLNK(status.st_mode)
            or stat.S_IMODE(status.st_mode) != FILE_MODE
            or not self._trusted_owner(status)
            or status.st_nlink != 1
            or not 0 <= status.st_size <= MAX_EVENT_BYTES
        ):
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_recovery_invalid"
            )
        _assert_name_no_extended_metadata(
            transaction_fd,
            pending_name,
            xattr_reader=self._xattr_reader,
        )
        try:
            self._boundary(
                "recovery_uncommitted_pending_binding_validated",
                parent_fd,
                transaction_fd,
            )
            os.unlink(pending_name, dir_fd=transaction_fd)
            self._boundary(
                "recovery_uncommitted_pending_removed",
                parent_fd,
                transaction_fd,
            )
            os.fsync(transaction_fd)
            self._boundary(
                "recovery_uncommitted_pending_cleanup_fsynced",
                parent_fd,
                transaction_fd,
            )
        except OSError as exc:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_recovery_invalid"
            ) from exc

    def _recover_pending(
        self,
        parent_fd: int,
        transaction_fd: int,
    ) -> None:
        finals, pending_name = self._inventory(transaction_fd)
        if pending_name is None:
            return
        match = _PENDING_NAME.fullmatch(pending_name)
        assert match is not None
        sequence = int(match.group(1))
        final_name = f"{sequence:0{SEQUENCE_WIDTH}d}.json"
        if any(
            name != f"{expected:0{SEQUENCE_WIDTH}d}.json"
            for expected, name in enumerate(finals)
        ):
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_recovery_invalid"
            )
        try:
            pending_status = os.stat(
                pending_name,
                dir_fd=transaction_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_recovery_invalid"
            ) from exc
        final_present = final_name in finals
        if pending_status.st_nlink == 1 and not final_present:
            if sequence != len(finals):
                raise ProductionReleaseUpdateJournalError(
                    "release_update_journal_recovery_invalid"
                )
            self._discard_uncommitted_pending(
                parent_fd,
                transaction_fd,
                pending_name,
                final_name=final_name,
                status=pending_status,
            )
            return
        if (
            pending_status.st_nlink != 2
            or not final_present
            or sequence != len(finals) - 1
        ):
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_recovery_invalid"
            )
        try:
            final_status = os.stat(
                final_name,
                dir_fd=transaction_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_recovery_invalid"
            ) from exc
        if (
            (pending_status.st_dev, pending_status.st_ino)
            != (final_status.st_dev, final_status.st_ino)
            or final_status.st_nlink != 2
        ):
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_recovery_invalid"
            )
        pending_raw, pending_event = self._read_file(
            transaction_fd,
            pending_name,
            expected_links=2,
        )
        final_raw, final_event = self._read_file(
            transaction_fd,
            final_name,
            expected_links=2,
        )
        if pending_raw != final_raw or pending_event != final_event:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_recovery_invalid"
            )
        events = self._load_final_events(
            transaction_fd,
            linked_sequence=sequence,
        )
        if (
            sequence >= len(events)
            or events[sequence] != pending_event
        ):
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_recovery_invalid"
            )
        try:
            os.fsync(transaction_fd)
            self._boundary(
                "recovery_final_directory_fsynced",
                parent_fd,
                transaction_fd,
            )
            os.unlink(pending_name, dir_fd=transaction_fd)
            self._boundary(
                "recovery_pending_unlinked",
                parent_fd,
                transaction_fd,
            )
            os.fsync(transaction_fd)
            self._boundary(
                "recovery_cleanup_directory_fsynced",
                parent_fd,
                transaction_fd,
            )
        except OSError as exc:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_recovery_invalid"
            ) from exc
        _raw, readback = self._read_file(
            transaction_fd,
            final_name,
            expected_links=1,
        )
        if readback != pending_event:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_recovery_invalid"
            )
        self._boundary(
            "recovery_readback_validated",
            parent_fd,
            transaction_fd,
        )

    def _locked_transaction(
        self,
        *,
        create: bool,
    ) -> tuple[int, int] | None:
        opened = self._open_transaction(create=create)
        if opened is None:
            return None
        parent_fd, transaction_fd = opened
        try:
            fcntl.flock(transaction_fd, fcntl.LOCK_EX)
            self._verify_binding(parent_fd, transaction_fd)
            return parent_fd, transaction_fd
        except BaseException:
            os.close(transaction_fd)
            os.close(parent_fd)
            raise

    @staticmethod
    def _close_locked(parent_fd: int, transaction_fd: int) -> None:
        try:
            fcntl.flock(transaction_fd, fcntl.LOCK_UN)
        finally:
            os.close(transaction_fd)
            os.close(parent_fd)

    def load(self) -> Sequence[Mapping[str, Any]]:
        opened = self._locked_transaction(
            create=self._create_transaction
        )
        if opened is None:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_not_found"
            )
        parent_fd, transaction_fd = opened
        try:
            if self._create_transaction:
                self._ensure_authority_header(
                    parent_fd,
                    transaction_fd,
                )
            else:
                self._require_existing_authority_header(
                    transaction_fd
                )
            self._recover_pending(parent_fd, transaction_fd)
            events = self._load_final_events(transaction_fd)
            os.fsync(transaction_fd)
            self._verify_binding(parent_fd, transaction_fd)
            return [dict(event) for event in events]
        finally:
            self._close_locked(parent_fd, transaction_fd)

    def append(
        self,
        event: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        try:
            validated = runtime.validate_event(
                event,
                intent=self._intent,
            )
        except runtime.ProductionReleaseUpdateRuntimeError as exc:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_event_invalid"
            ) from exc
        payload = canonical_json_bytes(validated)
        if not payload or len(payload) > MAX_EVENT_BYTES:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_event_invalid"
            )
        opened = self._locked_transaction(
            create=self._create_transaction
        )
        if opened is None:
            raise ProductionReleaseUpdateJournalError(
                "release_update_journal_not_found"
            )
        parent_fd, transaction_fd = opened
        try:
            if self._create_transaction:
                self._ensure_authority_header(
                    parent_fd,
                    transaction_fd,
                )
            else:
                self._require_existing_authority_header(
                    transaction_fd
                )
            self._recover_pending(parent_fd, transaction_fd)
            events = self._load_final_events(transaction_fd)
            sequence = validated["sequence"]
            if sequence < len(events):
                if events[sequence] != validated:
                    raise ProductionReleaseUpdateJournalError(
                        "release_update_journal_append_conflict"
                    )
                return dict(events[sequence])
            if sequence != len(events):
                raise ProductionReleaseUpdateJournalError(
                    "release_update_journal_sequence_invalid"
                )
            try:
                runtime.load_state(
                    intent=self._intent,
                    events=[*events, validated],
                )
            except runtime.ProductionReleaseUpdateRuntimeError as exc:
                raise ProductionReleaseUpdateJournalError(
                    "release_update_journal_sequence_invalid"
                ) from exc
            pending_name = (
                f".{sequence:0{SEQUENCE_WIDTH}d}.pending"
            )
            final_name = f"{sequence:0{SEQUENCE_WIDTH}d}.json"
            try:
                descriptor = os.open(
                    pending_name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    FILE_MODE,
                    dir_fd=transaction_fd,
                )
            except OSError as exc:
                raise ProductionReleaseUpdateJournalError(
                    "release_update_journal_write_failed"
                ) from exc
            try:
                os.fchown(descriptor, self._uid, self._gid)
                os.fchmod(descriptor, FILE_MODE)
                _checkpoint("pending_created")
                offset = 0
                while offset < len(payload):
                    written = os.write(descriptor, payload[offset:])
                    if written <= 0:
                        raise OSError("short journal write")
                    offset += written
                _checkpoint("pending_written")
                os.fsync(descriptor)
                _checkpoint("pending_file_fsynced")
            except OSError as exc:
                raise ProductionReleaseUpdateJournalError(
                    "release_update_journal_write_failed"
                ) from exc
            finally:
                os.close(descriptor)
            try:
                os.fsync(transaction_fd)
                self._boundary(
                    "pending_directory_fsynced",
                    parent_fd,
                    transaction_fd,
                )
                os.link(
                    pending_name,
                    final_name,
                    src_dir_fd=transaction_fd,
                    dst_dir_fd=transaction_fd,
                    follow_symlinks=False,
                )
                self._boundary(
                    "final_linked",
                    parent_fd,
                    transaction_fd,
                )
                os.fsync(transaction_fd)
                self._boundary(
                    "final_directory_fsynced",
                    parent_fd,
                    transaction_fd,
                )
                os.unlink(pending_name, dir_fd=transaction_fd)
                self._boundary(
                    "pending_unlinked",
                    parent_fd,
                    transaction_fd,
                )
                os.fsync(transaction_fd)
                self._boundary(
                    "cleanup_directory_fsynced",
                    parent_fd,
                    transaction_fd,
                )
            except FileExistsError as exc:
                raise ProductionReleaseUpdateJournalError(
                    "release_update_journal_append_conflict"
                ) from exc
            except OSError as exc:
                raise ProductionReleaseUpdateJournalError(
                    "release_update_journal_write_failed"
                ) from exc
            _raw, readback = self._read_file(
                transaction_fd,
                final_name,
                expected_links=1,
            )
            if readback != validated:
                raise ProductionReleaseUpdateJournalError(
                    "release_update_journal_readback_invalid"
                )
            self._boundary(
                "readback_validated",
                parent_fd,
                transaction_fd,
            )
            return dict(readback)
        finally:
            self._close_locked(parent_fd, transaction_fd)


__all__ = [
    "AUTHORITY_DURABLE_BOUNDARIES",
    "AUTHORITY_FILE_NAME",
    "AUTHORITY_PENDING_NAME",
    "AUTHORITY_RECOVERY_DURABLE_BOUNDARIES",
    "DIRECTORY_MODE",
    "DURABLE_BOUNDARIES",
    "FILE_MODE",
    "MAX_EVENT_BYTES",
    "PRODUCTION_JOURNAL_ROOT",
    "ProductionReleaseUpdateJournalError",
    "RECOVERY_DURABLE_BOUNDARIES",
    "ReleaseUpdateJournal",
    "canonical_json_bytes",
]
