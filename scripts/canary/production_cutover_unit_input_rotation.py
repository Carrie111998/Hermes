#!/usr/bin/env python3
"""Rotate the fixed production unit-input authority without ad-hoc deletion.

The existing public stager and fixed-input bootstrap are deliberately
create-only.  This root-only edge transaction first persists a prepared
successor, then durably preauthorizes that exact transaction while the signed
approvals are fresh.  Finalization after a separate caller commit consumes the
durable preauthorization without consulting wall-clock freshness again.  A
preauthorization can instead receive an append-only terminal abort before any
live input mutation.  Finalization publishes a separate append-only activation
marker immediately before the first live write, so a rollback can never make
that transaction abort-eligible again.  The legacy one-call rotation remains
available for existing callers and receipts.

The activation-marker protocol and its final receipt use a distinct v5 audit
root.  Historical v4-root evidence is immutable and is never scanned, migrated,
or deleted by this implementation.

The transaction is identified by the predecessor plan and successor
publication digests.  Every durable file is create-or-exact-resume, so the
same successor can finish an interrupted rotation while a different successor
fails closed.
"""

from __future__ import annotations

import argparse
import copy
import ctypes
import errno
import hashlib
import json
import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from gateway import canonical_writer_production_cutover as cutover
from scripts.canary import package_production_cutover_artifacts as package
from scripts.canary import production_cutover_activation_lock as authority_lock
from scripts.canary import production_cutover_public_stager as public_stager
from scripts.canary import production_release_unit_inputs_v4 as release_inputs_v4
from scripts.canary import production_release_update_contract as release_update


TRANSACTION_SCHEMA = "muncho-production-unit-input-rotation-transaction.v1"
PREPARED_RECEIPT_SCHEMA = "muncho-production-unit-input-rotation-prepared.v1"
RECEIPT_SCHEMA = "muncho-production-unit-input-rotation-receipt.v1"
FINALIZED_RECEIPT_SCHEMA = "muncho-production-unit-input-rotation-receipt.v2"
RELEASE_TRANSACTION_SCHEMA = (
    "muncho-production-release-unit-input-rotation-transaction.v2"
)
RELEASE_PREPARED_RECEIPT_SCHEMA = (
    "muncho-production-release-unit-input-rotation-prepared.v2"
)
RELEASE_FINALIZED_RECEIPT_SCHEMA = (
    "muncho-production-release-unit-input-rotation-receipt.v4"
)
RELEASE_MUTATION_BEGIN_SCHEMA = (
    "muncho-production-release-unit-input-rotation-mutation-begin.v1"
)
RELEASE_ACTIVATION_BEGIN_SCHEMA = (
    "muncho-production-release-unit-input-rotation-activation-begin.v1"
)
RELEASE_ABORTED_RECEIPT_SCHEMA = (
    "muncho-production-release-unit-input-rotation-aborted.v1"
)
RELEASE_PHASE_REQUEST_SCHEMA = (
    "muncho-production-release-unit-input-rotation-command.v1"
)
RELEASE_PHASE_RESULT_SCHEMA = (
    "muncho-production-release-unit-input-rotation-command-result.v1"
)
RELEASE_PHASE_ACTIONS = frozenset({
    "prepare-release-unit-inputs",
    "preauthorize-release-unit-inputs",
    "finalize-release-unit-inputs",
    "abort-release-unit-inputs",
})
AUDIT_DIRECTORY_NAME = "unit-input-authority-rotations"
LEGACY_RELEASE_AUDIT_DIRECTORY_NAME = (
    "release-unit-input-authority-rotations-v4"
)
RELEASE_AUDIT_DIRECTORY_NAME = "release-unit-input-authority-rotations-v5"
TRANSACTION_FILE_NAME = "transaction.json"
PUBLICATION_FILE_NAME = "successor-publication.json"
RELEASE_UPDATE_PUBLICATION_FILE_NAME = (
    "successor-release-update-publication.json"
)
PREDECESSOR_TRUST_FILE_NAME = "predecessor-trust.json"
PREPARED_RECEIPT_FILE_NAME = "prepared-receipt.json"
MUTATION_BEGIN_FILE_NAME = "mutation-begin.json"
ACTIVATION_BEGIN_FILE_NAME = "activation-begin.json"
ABORT_RECEIPT_FILE_NAME = "rotation-abort-receipt.json"
RECEIPT_FILE_NAME = "rotation-receipt.json"
PREDECESSOR_DIRECTORY_NAME = "predecessor"
PRODUCTION_STAGED_ROOT = Path("/var/lib/muncho-production-legacy-cutover/staged")
MAX_FILE = 16 * 1024 * 1024
_RELEASE_PREPARE_PENDING_NAMES = frozenset({
    TRANSACTION_FILE_NAME,
    PUBLICATION_FILE_NAME,
    RELEASE_UPDATE_PUBLICATION_FILE_NAME,
    PREDECESSOR_TRUST_FILE_NAME,
    package.STAGED_UNIT_INPUT_PLAN_PATH.name,
    package.STAGED_UNIT_INPUT_APPROVAL_PATH.name,
    package.FIXED_UNIT_INPUTS_PATH.name,
    PREPARED_RECEIPT_FILE_NAME,
})
_RELEASE_PREAUTHORIZE_PENDING_NAMES = frozenset({
    MUTATION_BEGIN_FILE_NAME,
})
_RELEASE_FINALIZE_PENDING_NAMES = frozenset({
    ACTIVATION_BEGIN_FILE_NAME,
    RECEIPT_FILE_NAME,
})
_RELEASE_ABORT_PENDING_NAMES = frozenset({
    ABORT_RECEIPT_FILE_NAME,
})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TRANSACTION_DIRECTORY = re.compile(r"^[0-9a-f]{64}-[0-9a-f]{64}$")
_TEMPORARY_SUFFIX = re.compile(r"^[1-9][0-9]*$")
_RENAME_NOREPLACE = 1
_MAX_TEMPORARY_ALIASES = 64
_PENDING_TEMPORARY_MODE = 0o600
_TRANSACTION_FIELDS = frozenset({
    "schema",
    "predecessor_revision",
    "predecessor_plan_sha256",
    "predecessor_approval_sha256",
    "predecessor_fixed_inputs_sha256",
    "authorization_checked_at_unix",
    "successor_revision",
    "successor_publication_sha256",
    "successor_plan_sha256",
    "successor_approval_sha256",
    "successor_fixed_inputs_sha256",
    "secret_material_recorded",
    "secret_digest_recorded",
    "transaction_sha256",
})
_PREPARED_RECEIPT_FIELDS = frozenset({
    "schema",
    "predecessor_revision",
    "predecessor_plan_sha256",
    "predecessor_approval_sha256",
    "predecessor_fixed_inputs_sha256",
    "authorization_checked_at_unix",
    "transaction_sha256",
    "successor_revision",
    "successor_publication_sha256",
    "successor_plan_sha256",
    "successor_approval_sha256",
    "successor_fixed_inputs_sha256",
    "audit_transaction_path",
    "live_plan_path",
    "live_approval_path",
    "live_fixed_inputs_path",
    "live_triplet_unchanged",
    "mutation_performed",
    "secret_material_recorded",
    "secret_digest_recorded",
    "receipt_sha256",
})
_RECEIPT_FIELDS = frozenset({
    "schema",
    "predecessor_revision",
    "predecessor_plan_sha256",
    "predecessor_approval_sha256",
    "predecessor_fixed_inputs_sha256",
    "authorization_checked_at_unix",
    "transaction_sha256",
    "successor_revision",
    "successor_publication_sha256",
    "successor_plan_sha256",
    "successor_approval_sha256",
    "successor_fixed_inputs_sha256",
    "audit_transaction_path",
    "staged_plan_path",
    "staged_approval_path",
    "fixed_inputs_path",
    "successor_triplet_complete",
    "secret_material_recorded",
    "secret_digest_recorded",
    "receipt_sha256",
})
_FINALIZED_RECEIPT_FIELDS = _RECEIPT_FIELDS | frozenset({
    "prepared_receipt_sha256",
})
_RELEASE_PREDECESSOR_FIELDS = frozenset(
    {
        "authority_version",
        "revision",
        "plan_schema",
        "approval_schema",
        "fixed_inputs_schema",
        "plan_sha256",
        "approval_sha256",
        "fixed_inputs_sha256",
        "fixed_inputs_file_sha256",
    }
)
_RELEASE_SUCCESSOR_FIELDS = frozenset(
    {
        "authority_version",
        "revision",
        "publication_schema",
        "release_update_publication_schema",
        "plan_sha256",
        "approval_sha256",
        "publication_sha256",
        "release_update_publication_sha256",
        "fixed_inputs_sha256",
        "fixed_inputs_file_sha256",
    }
)
_RELEASE_TRANSACTION_FIELDS = frozenset(
    {
        "schema",
        "predecessor",
        "predecessor_trust_sha256",
        "authorization_checked_at_unix",
        "successor",
        "secret_material_recorded",
        "secret_digest_recorded",
        "transaction_sha256",
    }
)
_RELEASE_PREPARED_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "predecessor",
        "predecessor_trust_sha256",
        "authorization_checked_at_unix",
        "transaction_sha256",
        "successor",
        "audit_transaction_path",
        "live_plan_path",
        "live_approval_path",
        "live_fixed_inputs_path",
        "live_triplet_unchanged",
        "mutation_performed",
        "secret_material_recorded",
        "secret_digest_recorded",
        "receipt_sha256",
    }
)
_RELEASE_FINALIZED_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "predecessor",
        "predecessor_trust_sha256",
        "authorization_checked_at_unix",
        "transaction_sha256",
        "successor",
        "audit_transaction_path",
        "staged_plan_path",
        "staged_approval_path",
        "fixed_inputs_path",
        "successor_triplet_complete",
        "mutation_begin_sha256",
        "activation_begin_sha256",
        "prepared_receipt_sha256",
        "secret_material_recorded",
        "secret_digest_recorded",
        "receipt_sha256",
    }
)
_RELEASE_MUTATION_BEGIN_FIELDS = frozenset(
    {
        "schema",
        "transaction_sha256",
        "successor_publication_sha256",
        "release_update_publication_sha256",
        "successor_fixed_inputs_sha256",
        "freshness_checked_at_unix",
        "live_mutation_write_ahead_committed",
        "secret_material_recorded",
        "secret_digest_recorded",
        "mutation_begin_sha256",
    }
)
_RELEASE_ACTIVATION_BEGIN_FIELDS = frozenset(
    {
        "schema",
        "transaction_sha256",
        "mutation_begin_sha256",
        "successor_publication_sha256",
        "release_update_publication_sha256",
        "successor_fixed_inputs_sha256",
        "live_activation_write_ahead_committed",
        "secret_material_recorded",
        "secret_digest_recorded",
        "activation_begin_sha256",
    }
)
_RELEASE_ABORTED_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "transaction_sha256",
        "successor_publication_sha256",
        "release_update_publication_sha256",
        "successor_fixed_inputs_sha256",
        "audit_transaction_path",
        "prepared_receipt_sha256",
        "mutation_begin_sha256",
        "live_predecessor_unchanged",
        "live_mutation_performed",
        "secret_material_recorded",
        "secret_digest_recorded",
        "receipt_sha256",
    }
)


class UnitInputRotationError(RuntimeError):
    """One stable, secret-free unit-input rotation failure."""


@dataclass(frozen=True)
class _AuthorityTriplet:
    revision: str
    plan: Mapping[str, Any]
    approval: Mapping[str, Any]
    fixed_inputs: Mapping[str, Any]
    plan_raw: bytes
    approval_raw: bytes
    fixed_inputs_raw: bytes


@dataclass(frozen=True)
class _Successor:
    revision: str
    publication: Mapping[str, Any]
    plan: Mapping[str, Any]
    approval: Mapping[str, Any]
    publication_raw: bytes
    plan_raw: bytes
    approval_raw: bytes
    fixed_inputs_raw: bytes


@dataclass(frozen=True)
class _ReleaseAuthorityTriplet:
    authority_version: str
    revision: str
    plan: Mapping[str, Any]
    approval: Mapping[str, Any]
    fixed_inputs: Mapping[str, Any]
    plan_raw: bytes
    approval_raw: bytes
    fixed_inputs_raw: bytes
    fixed_inputs_sha256: str


@dataclass(frozen=True)
class _ReleaseSuccessor:
    revision: str
    publication: Mapping[str, Any]
    release_update_publication: Mapping[str, Any]
    trusted_predecessor: Mapping[str, Any]
    plan: Mapping[str, Any]
    approval: Mapping[str, Any]
    fixed_inputs: Mapping[str, Any]
    publication_raw: bytes
    release_update_publication_raw: bytes
    trusted_predecessor_raw: bytes
    plan_raw: bytes
    approval_raw: bytes
    fixed_inputs_raw: bytes


_AuthorityTripletLike = _AuthorityTriplet | _ReleaseAuthorityTriplet
_SuccessorLike = _Successor | _ReleaseSuccessor


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8", errors="strict")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _decode(raw: bytes, *, newline: bool = False) -> Mapping[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in items:
            if name in result:
                raise UnitInputRotationError("unit_input_rotation_duplicate_key")
            result[name] = value
        return result

    def constant(_value: str) -> None:
        raise UnitInputRotationError("unit_input_rotation_nonfinite_number")

    payload = raw[:-1] if newline and raw.endswith(b"\n") else raw
    if newline and (not raw.endswith(b"\n") or b"\n" in raw[:-1]):
        raise UnitInputRotationError("unit_input_rotation_json_invalid")
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=constant,
        )
    except UnitInputRotationError:
        raise
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise UnitInputRotationError("unit_input_rotation_json_invalid") from exc
    expected = _canonical(value) + (b"\n" if newline else b"")
    if not isinstance(value, Mapping) or raw != expected:
        raise UnitInputRotationError("unit_input_rotation_json_invalid")
    return value


def _identity(item: os.stat_result) -> tuple[int, ...]:
    return (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_nlink,
        item.st_uid,
        item.st_gid,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )


def _require_directory(path: Path, *, uid: int, gid: int) -> None:
    try:
        item = path.lstat()
    except OSError as exc:
        raise UnitInputRotationError("unit_input_rotation_directory_invalid") from exc
    if (
        path.resolve(strict=True) != path
        or stat.S_ISLNK(item.st_mode)
        or not stat.S_ISDIR(item.st_mode)
        or item.st_uid != uid
        or item.st_gid != gid
        or stat.S_IMODE(item.st_mode) != 0o700
    ):
        raise UnitInputRotationError("unit_input_rotation_directory_invalid")


def _ensure_directory(path: Path, *, uid: int, gid: int) -> None:
    if not path.is_absolute():
        raise UnitInputRotationError("unit_input_rotation_directory_invalid")
    if not os.path.lexists(path):
        try:
            path.mkdir(mode=0o700)
            os.chown(path, uid, gid)
            os.chmod(path, 0o700)
            cutover.activation._fsync_directory(path.parent)
        except FileExistsError:
            pass
        except OSError as exc:
            raise UnitInputRotationError(
                "unit_input_rotation_directory_invalid"
            ) from exc
    _require_directory(path, uid=uid, gid=gid)


def _read_exact(
    path: Path,
    *,
    uid: int,
    gid: int,
    mode: int,
    maximum: int = MAX_FILE,
) -> bytes:
    descriptor: int | None = None
    try:
        before = path.lstat()
        if before.st_nlink != 1:
            _heal_same_inode_aliases(
                path,
                uid=uid,
                gid=gid,
                mode=mode,
            )
            before = path.lstat()
        if (
            path.resolve(strict=True) != path
            or stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != uid
            or before.st_gid != gid
            or stat.S_IMODE(before.st_mode) != mode
            or not 0 < before.st_size <= maximum
        ):
            raise UnitInputRotationError("unit_input_rotation_file_identity_invalid")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        reachable = path.lstat()
    except UnitInputRotationError:
        raise
    except OSError as exc:
        raise UnitInputRotationError("unit_input_rotation_file_unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (
        len(payload) != before.st_size
        or len(payload) > maximum
        or _identity(before) != _identity(opened)
        or _identity(before) != _identity(after)
        or _identity(before) != _identity(reachable)
    ):
        raise UnitInputRotationError("unit_input_rotation_file_changed")
    return payload


def _optional_exact(
    path: Path,
    *,
    uid: int,
    gid: int,
    mode: int,
) -> bytes | None:
    if not os.path.lexists(path):
        return None
    return _read_exact(path, uid=uid, gid=gid, mode=mode)


def _reserved_temporaries(
    path: Path,
    *,
    tags: tuple[str, ...] = ("rotate",),
) -> list[Path]:
    prefixes = tuple(f".{path.name}.{tag}." for tag in tags)
    try:
        children = list(path.parent.iterdir())
    except OSError as exc:
        raise UnitInputRotationError("unit_input_rotation_write_failed") from exc
    result: list[Path] = []
    for child in children:
        prefix = next(
            (
                candidate
                for candidate in prefixes
                if child.name.startswith(candidate)
            ),
            None,
        )
        if prefix is None:
            continue
        suffix = child.name[len(prefix) :]
        if _TEMPORARY_SUFFIX.fullmatch(suffix) is None:
            raise UnitInputRotationError("unit_input_rotation_conflict")
        result.append(child)
    if len(result) > _MAX_TEMPORARY_ALIASES:
        raise UnitInputRotationError("unit_input_rotation_conflict")
    return sorted(result)


def _heal_same_inode_aliases(
    path: Path,
    *,
    uid: int,
    gid: int,
    mode: int,
) -> None:
    target = path.lstat()
    candidates = _reserved_temporaries(
        path,
        tags=("stage", "bootstrap", "rotate"),
    )
    if (
        not candidates
        or target.st_nlink != len(candidates) + 1
        or stat.S_ISLNK(target.st_mode)
        or not stat.S_ISREG(target.st_mode)
        or target.st_uid != uid
        or target.st_gid != gid
        or stat.S_IMODE(target.st_mode) != mode
    ):
        raise UnitInputRotationError("unit_input_rotation_file_identity_invalid")
    for candidate in candidates:
        item = candidate.lstat()
        if (
            candidate.resolve(strict=True) != candidate
            or stat.S_ISLNK(item.st_mode)
            or not stat.S_ISREG(item.st_mode)
            or item.st_nlink != target.st_nlink
            or item.st_uid != uid
            or item.st_gid != gid
            or stat.S_IMODE(item.st_mode) != mode
            or (item.st_dev, item.st_ino) != (target.st_dev, target.st_ino)
        ):
            raise UnitInputRotationError(
                "unit_input_rotation_file_identity_invalid"
            )
    try:
        for candidate in candidates:
            item = candidate.lstat()
            if (item.st_dev, item.st_ino) != (target.st_dev, target.st_ino):
                raise UnitInputRotationError(
                    "unit_input_rotation_file_identity_invalid"
                )
            candidate.unlink()
        cutover.activation._fsync_directory(path.parent)
    except UnitInputRotationError:
        raise
    except OSError as exc:
        raise UnitInputRotationError("unit_input_rotation_write_failed") from exc


def _require_no_temporary_extended_metadata(path: Path) -> None:
    listxattr = getattr(os, "listxattr", None)
    if listxattr is None:
        if sys.platform.startswith("linux"):
            raise UnitInputRotationError(
                "unit_input_rotation_conflict"
            )
        return
    try:
        attributes = listxattr(path, follow_symlinks=False)
    except OSError as exc:
        raise UnitInputRotationError("unit_input_rotation_conflict") from exc
    if attributes:
        raise UnitInputRotationError("unit_input_rotation_conflict")


def _pending_temporary_identity(
    path: Path,
    *,
    uid: int,
    gid: int,
    final_mode: int,
) -> tuple[int, ...] | None:
    try:
        item = path.lstat()
        observed_mode = stat.S_IMODE(item.st_mode)
        pending_mode = observed_mode == _PENDING_TEMPORARY_MODE
        if not pending_mode:
            return None
        if (
            path.resolve(strict=True) != path
            or stat.S_ISLNK(item.st_mode)
            or not stat.S_ISREG(item.st_mode)
            or item.st_nlink != 1
            or item.st_uid != uid
            or item.st_gid != gid
            or not 0 <= item.st_size <= MAX_FILE
        ):
            raise UnitInputRotationError(
                "unit_input_rotation_conflict"
            )
        _require_no_temporary_extended_metadata(path)
        return _identity(item)
    except UnitInputRotationError:
        raise
    except OSError as exc:
        raise UnitInputRotationError(
            "unit_input_rotation_conflict"
        ) from exc


def _remove_pending_temporary(
    path: Path,
    *,
    uid: int,
    gid: int,
    final_mode: int,
) -> bool:
    identity = _pending_temporary_identity(
        path,
        uid=uid,
        gid=gid,
        final_mode=final_mode,
    )
    if identity is None:
        return False
    try:
        current = path.lstat()
        if _identity(current) != identity:
            raise UnitInputRotationError(
                "unit_input_rotation_conflict"
            )
        path.unlink()
        cutover.activation._fsync_directory(path.parent)
    except UnitInputRotationError:
        raise
    except OSError as exc:
        raise UnitInputRotationError(
            "unit_input_rotation_conflict"
        ) from exc
    return True


def _prune_unpublished_pending_temporary(
    path: Path,
    *,
    uid: int,
    gid: int,
    final_mode: int,
) -> bool:
    if os.path.lexists(path):
        return False
    candidates = _reserved_temporaries(path)
    own_prefix = f".{path.name}.rotate."
    own = [
        candidate
        for candidate in candidates
        if candidate.name.startswith(own_prefix)
    ]
    if not candidates:
        return False
    if len(candidates) != 1 or candidates != own:
        raise UnitInputRotationError("unit_input_rotation_conflict")
    return _remove_pending_temporary(
        candidates[0],
        uid=uid,
        gid=gid,
        final_mode=final_mode,
    )


def _fsync_exact_temporary(
    path: Path,
    *,
    uid: int,
    gid: int,
    mode: int,
) -> None:
    descriptor: int | None = None
    try:
        before = path.lstat()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            path.resolve(strict=True) != path
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != uid
            or before.st_gid != gid
            or stat.S_IMODE(before.st_mode) != mode
            or _identity(before) != _identity(opened)
        ):
            raise UnitInputRotationError(
                "unit_input_rotation_conflict"
            )
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        reachable = path.lstat()
        if (
            _identity(before) != _identity(after)
            or _identity(before) != _identity(reachable)
        ):
            raise UnitInputRotationError(
                "unit_input_rotation_conflict"
            )
    except UnitInputRotationError:
        raise
    except OSError as exc:
        raise UnitInputRotationError(
            "unit_input_rotation_write_failed"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _recover_temporary_aliases(
    path: Path,
    payload: bytes,
    *,
    uid: int,
    gid: int,
    mode: int,
) -> Path:
    _prune_unpublished_pending_temporary(
        path,
        uid=uid,
        gid=gid,
        final_mode=mode,
    )
    candidates = _reserved_temporaries(
        path,
        tags=("stage", "bootstrap", "rotate"),
    )
    own_prefix = f".{path.name}.rotate."
    own = [
        candidate
        for candidate in candidates
        if candidate.name.startswith(own_prefix)
    ]
    current = path.with_name(f".{path.name}.rotate.{os.getpid()}")
    if not candidates:
        return current
    if os.path.lexists(path):
        if path.lstat().st_nlink == 1:
            raise UnitInputRotationError("unit_input_rotation_conflict")
        observed = _read_exact(
            path,
            uid=uid,
            gid=gid,
            mode=mode,
            maximum=max(MAX_FILE, len(payload)),
        )
        if observed != payload:
            raise UnitInputRotationError("unit_input_rotation_conflict")
        return current
    if len(candidates) != 1 or candidates != own:
        raise UnitInputRotationError("unit_input_rotation_conflict")
    observed = _read_exact(
        candidates[0],
        uid=uid,
        gid=gid,
        mode=mode,
        maximum=max(MAX_FILE, len(payload)),
    )
    if observed != payload:
        raise UnitInputRotationError("unit_input_rotation_conflict")
    _require_no_temporary_extended_metadata(candidates[0])
    _fsync_exact_temporary(
        candidates[0],
        uid=uid,
        gid=gid,
        mode=mode,
    )
    return candidates[0]


def _rename_noreplace(
    source: Path,
    destination: Path,
    *,
    directory_fd: int,
    expected_identity: tuple[int, int],
) -> bool:
    if source.parent != destination.parent:
        raise OSError(errno.EXDEV, "unit-input rotation parent mismatch")
    source_state = os.stat(
        source.name,
        dir_fd=directory_fd,
        follow_symlinks=False,
    )
    if (source_state.st_dev, source_state.st_ino) != expected_identity:
        raise UnitInputRotationError("unit_input_rotation_conflict")
    if sys.platform.startswith("linux"):
        try:
            renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
        except (AttributeError, OSError) as exc:
            raise OSError(
                errno.ENOSYS,
                "renameat2(RENAME_NOREPLACE) unavailable",
            ) from exc
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        ctypes.set_errno(0)
        result = renameat2(
            directory_fd,
            os.fsencode(source.name),
            directory_fd,
            os.fsencode(destination.name),
            _RENAME_NOREPLACE,
        )
        if result == 0:
            return True
        number = ctypes.get_errno()
        if number == errno.EEXIST:
            return False
        raise OSError(number, os.strerror(number), destination)
    try:
        os.link(
            source.name,
            destination.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except FileExistsError:
        return False
    return True


def _install_exact(
    path: Path,
    payload: bytes,
    *,
    uid: int,
    gid: int,
    mode: int,
) -> bool:
    _require_directory(path.parent, uid=uid, gid=gid)
    temporary = _recover_temporary_aliases(
        path,
        payload,
        uid=uid,
        gid=gid,
        mode=mode,
    )
    created = False
    descriptor: int | None = None
    directory_descriptor: int | None = None
    temporary_descriptor: int | None = None
    temporary_identity: tuple[int, int] | None = None
    checkpoint_prefix = f"install_exact:{path.name}"
    try:
        parent_before = path.parent.lstat()
        directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        directory_descriptor = os.open(path.parent, directory_flags)
        parent_opened = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(parent_opened.st_mode)
            or _identity(parent_before) != _identity(parent_opened)
        ):
            raise UnitInputRotationError(
                "unit_input_rotation_directory_invalid"
            )
        if not os.path.lexists(path):
            if not os.path.lexists(temporary):
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                flags |= getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(
                    temporary.name,
                    flags,
                    0o600,
                    dir_fd=directory_descriptor,
                )
                opened = os.fstat(descriptor)
                temporary_identity = (opened.st_dev, opened.st_ino)
                os.fchown(descriptor, uid, gid)
                os.fchmod(descriptor, _PENDING_TEMPORARY_MODE)
                _checkpoint(f"{checkpoint_prefix}:temporary_created")
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short unit-input rotation write")
                    view = view[written:]
                    _checkpoint(
                        f"{checkpoint_prefix}:temporary_write_progress"
                    )
                _checkpoint(f"{checkpoint_prefix}:temporary_written")
                os.fchmod(descriptor, mode)
                _checkpoint(f"{checkpoint_prefix}:temporary_chmod")
                os.fsync(descriptor)
                _checkpoint(f"{checkpoint_prefix}:temporary_fsynced")
                os.close(descriptor)
                descriptor = None
            temporary_payload = _read_exact(
                temporary,
                uid=uid,
                gid=gid,
                mode=mode,
                maximum=max(MAX_FILE, len(payload)),
            )
            temporary_state = temporary.lstat()
            temporary_identity = (
                temporary_state.st_dev,
                temporary_state.st_ino,
            )
            if temporary_payload != payload:
                raise UnitInputRotationError("unit_input_rotation_conflict")
            if temporary_identity is None:
                raise UnitInputRotationError(
                    "unit_input_rotation_conflict"
                )
            temporary_flags = os.O_RDONLY | getattr(
                os,
                "O_CLOEXEC",
                0,
            )
            temporary_flags |= getattr(os, "O_NOFOLLOW", 0)
            temporary_descriptor = os.open(
                temporary.name,
                temporary_flags,
                dir_fd=directory_descriptor,
            )
            temporary_opened = os.fstat(temporary_descriptor)
            if _identity(temporary_opened) != _identity(temporary_state):
                raise UnitInputRotationError(
                    "unit_input_rotation_conflict"
                )
            # The canonical activation lock plus this owner-only 0700
            # directory is the writer concurrency boundary.  The retained
            # dirfd anchors both names, while the retained source descriptor
            # prevents an unlinked source inode from being reused during
            # publication.  Bind the source immediately before publication
            # and require the destination to be that same inode.  A violated
            # boundary is rolled back without accepting the substituted
            # destination.
            created = _rename_noreplace(
                temporary,
                path,
                directory_fd=directory_descriptor,
                expected_identity=temporary_identity,
            )
            _checkpoint(f"{checkpoint_prefix}:destination_installed")
            if created:
                installed = os.stat(
                    path.name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if (
                    installed.st_dev,
                    installed.st_ino,
                ) != temporary_identity:
                    installed_identity = (
                        installed.st_dev,
                        installed.st_ino,
                    )
                    current = os.stat(
                        path.name,
                        dir_fd=directory_descriptor,
                        follow_symlinks=False,
                    )
                    if (
                        current.st_dev,
                        current.st_ino,
                    ) != installed_identity:
                        raise UnitInputRotationError(
                            "unit_input_rotation_conflict"
                        )
                    os.unlink(
                        path.name,
                        dir_fd=directory_descriptor,
                    )
                    os.fsync(directory_descriptor)
                    raise UnitInputRotationError("unit_input_rotation_conflict")
                os.fsync(directory_descriptor)
            observed = _read_exact(
                path,
                uid=uid,
                gid=gid,
                mode=mode,
                maximum=max(MAX_FILE, len(payload)),
            )
            if observed != payload:
                raise UnitInputRotationError(
                    "unit_input_rotation_conflict"
                )
            try:
                current = os.stat(
                    temporary.name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                current = None
            if current is not None:
                if (
                    current.st_dev,
                    current.st_ino,
                ) != temporary_identity:
                    raise UnitInputRotationError(
                        "unit_input_rotation_conflict"
                    )
                os.unlink(
                    temporary.name,
                    dir_fd=directory_descriptor,
                )
                os.fsync(directory_descriptor)
        else:
            observed = _read_exact(
                path,
                uid=uid,
                gid=gid,
                mode=mode,
                maximum=max(MAX_FILE, len(payload)),
            )
            if observed != payload:
                raise UnitInputRotationError(
                    "unit_input_rotation_conflict"
                )
    except UnitInputRotationError:
        raise
    except OSError as exc:
        raise UnitInputRotationError("unit_input_rotation_write_failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if directory_descriptor is not None:
            os.close(directory_descriptor)
    return created


def _remove_if_exact(
    path: Path,
    payload: bytes,
    *,
    uid: int,
    gid: int,
    mode: int,
) -> bool:
    if not os.path.lexists(path):
        return False
    observed = _read_exact(path, uid=uid, gid=gid, mode=mode)
    if observed != payload:
        raise UnitInputRotationError("unit_input_rotation_conflict")
    try:
        path.unlink()
        cutover.activation._fsync_directory(path.parent)
    except OSError as exc:
        raise UnitInputRotationError("unit_input_rotation_remove_failed") from exc
    return True


def _validate_approval_without_lease(
    value: Any,
    *,
    plan: Mapping[str, Any],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise UnitInputRotationError("unit_input_rotation_authority_invalid")
    issued = value.get("issued_at_unix")
    if type(issued) is not int:
        raise UnitInputRotationError("unit_input_rotation_authority_invalid")
    try:
        return package.validate_unit_input_approval(
            value,
            plan=plan,
            now_unix=issued,
        )
    except (PermissionError, TypeError, ValueError, package.PackagingError) as exc:
        raise UnitInputRotationError("unit_input_rotation_authority_invalid") from exc


def _validate_v3_authority_triplet(
    plan_value: Mapping[str, Any],
    approval_value: Mapping[str, Any],
    fixed_raw: bytes,
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    """Validate current v3 or the one frozen pre-edge-expansion v3 shape.

    V3 authority files predate the backup and SEO operational edges.  Their
    self-hashes and owner signatures remain authoritative, so historical
    validation must use the exact nine-domain vocabulary that was active when
    they were signed.  Successor publications still use the current catalog;
    this compatibility path is read-only and cannot author a legacy shape.
    """

    for operational_edge_domains in (
        None,
        package.LEGACY_V3_OPERATIONAL_EDGE_DOMAINS,
    ):
        try:
            plan = package.validate_unit_input_plan(
                plan_value,
                operational_edge_domains=operational_edge_domains,
            )
            approval = _validate_approval_without_lease(
                approval_value,
                plan=plan,
            )
            fixed = package._unit_inputs_from_authority(
                plan,
                approval,
                operational_edge_domains=operational_edge_domains,
            )
        except (
            PermissionError,
            TypeError,
            ValueError,
            package.PackagingError,
            UnitInputRotationError,
        ):
            continue
        if fixed_raw == _canonical(fixed) + b"\n":
            return plan, approval, fixed
    raise UnitInputRotationError("unit_input_rotation_authority_invalid")


def _successor(
    value: Mapping[str, Any],
    *,
    now_unix: int,
    require_fresh: bool,
) -> _Successor:
    documents = value.get("documents") if isinstance(value, Mapping) else None
    if (
        not isinstance(documents, Mapping)
        or value.get("action") != "unit-input-authority"
        or set(documents) != {"plan", "approval"}
    ):
        raise UnitInputRotationError("unit_input_rotation_publication_invalid")
    try:
        plan = package.validate_unit_input_plan(documents["plan"])
        approval = _validate_approval_without_lease(
            documents["approval"],
            plan=plan,
        )
        expected = public_stager.build_publication(
            action="unit-input-authority",
            release_revision=plan["release_revision"],
            documents={"plan": plan, "approval": approval},
            now_unix=approval["issued_at_unix"],
        )
        if require_fresh:
            package.validate_unit_input_approval(
                approval,
                plan=plan,
                now_unix=now_unix,
            )
    except (
        PermissionError,
        TypeError,
        ValueError,
        package.PackagingError,
        public_stager.PublicStagingError,
    ) as exc:
        raise UnitInputRotationError("unit_input_rotation_publication_invalid") from exc
    if value != expected:
        raise UnitInputRotationError("unit_input_rotation_publication_invalid")
    fixed = package._unit_inputs_from_authority(plan, approval)
    return _Successor(
        revision=str(plan["release_revision"]),
        publication=dict(value),
        plan=plan,
        approval=approval,
        publication_raw=_canonical(value),
        plan_raw=_canonical(plan),
        approval_raw=_canonical(approval),
        fixed_inputs_raw=_canonical(fixed) + b"\n",
    )


def _triplet(
    *,
    uid: int,
    gid: int,
) -> _AuthorityTriplet:
    plan_raw = _read_exact(
        package.STAGED_UNIT_INPUT_PLAN_PATH,
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    approval_raw = _read_exact(
        package.STAGED_UNIT_INPUT_APPROVAL_PATH,
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    fixed_raw = _read_exact(
        package.FIXED_UNIT_INPUTS_PATH,
        uid=uid,
        gid=gid,
        mode=package.FIXED_UNIT_INPUTS_MODE,
    )
    try:
        plan, approval, fixed_inputs = _validate_v3_authority_triplet(
            _decode(plan_raw),
            _decode(approval_raw),
            fixed_raw,
        )
    except UnitInputRotationError as exc:
        raise UnitInputRotationError("unit_input_rotation_predecessor_invalid") from exc
    return _AuthorityTriplet(
        revision=str(plan["release_revision"]),
        plan=plan,
        approval=approval,
        fixed_inputs=fixed_inputs,
        plan_raw=plan_raw,
        approval_raw=approval_raw,
        fixed_inputs_raw=fixed_raw,
    )


def _transaction_unsigned(
    predecessor: _AuthorityTriplet,
    successor: _Successor,
    *,
    authorization_checked_at_unix: int,
) -> Mapping[str, Any]:
    return {
        "schema": TRANSACTION_SCHEMA,
        "predecessor_revision": predecessor.revision,
        "predecessor_plan_sha256": predecessor.plan["plan_sha256"],
        "predecessor_approval_sha256": predecessor.approval["approval_sha256"],
        "predecessor_fixed_inputs_sha256": _sha(predecessor.fixed_inputs_raw),
        "authorization_checked_at_unix": authorization_checked_at_unix,
        "successor_revision": successor.revision,
        "successor_publication_sha256": successor.publication["publication_sha256"],
        "successor_plan_sha256": successor.plan["plan_sha256"],
        "successor_approval_sha256": successor.approval["approval_sha256"],
        "successor_fixed_inputs_sha256": _sha(successor.fixed_inputs_raw),
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }


def _transaction_value(
    predecessor: _AuthorityTriplet,
    successor: _Successor,
    *,
    authorization_checked_at_unix: int,
) -> Mapping[str, Any]:
    unsigned = _transaction_unsigned(
        predecessor,
        successor,
        authorization_checked_at_unix=authorization_checked_at_unix,
    )
    return {
        **unsigned,
        "transaction_sha256": _sha(_canonical(unsigned)),
    }


def _validate_transaction(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _TRANSACTION_FIELDS:
        raise UnitInputRotationError("unit_input_rotation_transaction_invalid")
    unsigned = {
        name: item for name, item in value.items() if name != "transaction_sha256"
    }
    if (
        value.get("schema") != TRANSACTION_SCHEMA
        or package.REVISION.fullmatch(str(value.get("predecessor_revision", "")))
        is None
        or package.REVISION.fullmatch(str(value.get("successor_revision", ""))) is None
        or type(value.get("authorization_checked_at_unix")) is not int
        or value["authorization_checked_at_unix"] <= 0
        or any(
            _SHA256.fullmatch(str(value.get(name, ""))) is None
            for name in (
                "predecessor_plan_sha256",
                "predecessor_approval_sha256",
                "predecessor_fixed_inputs_sha256",
                "successor_publication_sha256",
                "successor_plan_sha256",
                "successor_approval_sha256",
                "successor_fixed_inputs_sha256",
                "transaction_sha256",
            )
        )
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
        or value.get("transaction_sha256") != _sha(_canonical(unsigned))
    ):
        raise UnitInputRotationError("unit_input_rotation_transaction_invalid")
    return dict(value)


def _audit_root(*, uid: int, gid: int) -> Path:
    root = cutover.EVIDENCE_ROOT / AUDIT_DIRECTORY_NAME
    if not os.path.lexists(root):
        _ensure_directory(root, uid=uid, gid=gid)
    else:
        _require_directory(root, uid=uid, gid=gid)
    return root


def _transaction_directories(
    root: Path,
    *,
    uid: int,
    gid: int,
) -> list[Path]:
    result: list[Path] = []
    try:
        children = sorted(root.iterdir())
    except OSError as exc:
        raise UnitInputRotationError("unit_input_rotation_audit_invalid") from exc
    for child in children:
        if _TRANSACTION_DIRECTORY.fullmatch(child.name) is None:
            raise UnitInputRotationError("unit_input_rotation_audit_invalid")
        _require_directory(child, uid=uid, gid=gid)
        result.append(child)
    return result


def _load_transaction(
    path: Path,
    *,
    uid: int,
    gid: int,
) -> Mapping[str, Any] | None:
    transaction_path = path / TRANSACTION_FILE_NAME
    if not os.path.lexists(transaction_path):
        return None
    value = _validate_transaction(
        _decode(
            _read_exact(
                transaction_path,
                uid=uid,
                gid=gid,
                mode=0o400,
            )
        )
    )
    if path.name != (
        f"{value['predecessor_plan_sha256']}-{value['successor_publication_sha256']}"
    ):
        raise UnitInputRotationError("unit_input_rotation_transaction_invalid")
    return value


def _load_receipt(
    path: Path,
    *,
    transaction: Mapping[str, Any],
    publication: Mapping[str, Any],
    uid: int,
    gid: int,
) -> Mapping[str, Any] | None:
    receipt_path = path / RECEIPT_FILE_NAME
    if not os.path.lexists(receipt_path):
        return None
    receipt = validate_rotation_receipt(
        _decode(
            _read_exact(
                receipt_path,
                uid=uid,
                gid=gid,
                mode=0o400,
            )
        ),
        publication=publication,
    )
    if receipt["schema"] == RECEIPT_SCHEMA:
        expected = _receipt(path, transaction)
    else:
        prepared = _load_prepared_receipt(
            path,
            transaction=transaction,
            publication=publication,
            uid=uid,
            gid=gid,
        )
        if prepared is None:
            raise UnitInputRotationError(
                "unit_input_rotation_receipt_invalid"
            )
        expected = _finalized_receipt(path, transaction, prepared)
    if receipt != expected:
        raise UnitInputRotationError("unit_input_rotation_receipt_invalid")
    return receipt


def _archived_triplet(
    transaction_path: Path,
    transaction: Mapping[str, Any],
    *,
    uid: int,
    gid: int,
) -> _AuthorityTriplet:
    predecessor_root = transaction_path / PREDECESSOR_DIRECTORY_NAME
    _require_directory(predecessor_root, uid=uid, gid=gid)
    plan_raw = _read_exact(
        predecessor_root / package.STAGED_UNIT_INPUT_PLAN_PATH.name,
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    approval_raw = _read_exact(
        predecessor_root / package.STAGED_UNIT_INPUT_APPROVAL_PATH.name,
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    fixed_raw = _read_exact(
        predecessor_root / package.FIXED_UNIT_INPUTS_PATH.name,
        uid=uid,
        gid=gid,
        mode=package.FIXED_UNIT_INPUTS_MODE,
    )
    try:
        plan, approval, fixed_inputs = _validate_v3_authority_triplet(
            _decode(plan_raw),
            _decode(approval_raw),
            fixed_raw,
        )
    except UnitInputRotationError as exc:
        raise UnitInputRotationError("unit_input_rotation_audit_invalid") from exc
    if (
        transaction["predecessor_revision"] != plan["release_revision"]
        or transaction["predecessor_plan_sha256"] != plan["plan_sha256"]
        or transaction["predecessor_approval_sha256"] != approval["approval_sha256"]
        or transaction["predecessor_fixed_inputs_sha256"] != _sha(fixed_raw)
    ):
        raise UnitInputRotationError("unit_input_rotation_audit_invalid")
    return _AuthorityTriplet(
        revision=str(plan["release_revision"]),
        plan=plan,
        approval=approval,
        fixed_inputs=fixed_inputs,
        plan_raw=plan_raw,
        approval_raw=approval_raw,
        fixed_inputs_raw=fixed_raw,
    )


def _prepare_transaction(
    *,
    transaction_path: Path,
    predecessor: _AuthorityTriplet,
    successor: _Successor,
    authorization_checked_at_unix: int,
    authorization_clock: Callable[[], int] | None = None,
    uid: int,
    gid: int,
) -> Mapping[str, Any]:
    _ensure_directory(transaction_path, uid=uid, gid=gid)
    _checkpoint("before_transaction_authorized")
    if authorization_clock is not None:
        authorization_checked_at_unix = authorization_clock()
        _require_fresh(
            successor,
            now_unix=authorization_checked_at_unix,
        )
    transaction = _transaction_value(
        predecessor,
        successor,
        authorization_checked_at_unix=authorization_checked_at_unix,
    )
    _install_exact(
        transaction_path / TRANSACTION_FILE_NAME,
        _canonical(transaction),
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    _checkpoint("transaction_authorized")
    _install_exact(
        transaction_path / PUBLICATION_FILE_NAME,
        successor.publication_raw,
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    _checkpoint("successor_publication_archived")
    predecessor_root = transaction_path / PREDECESSOR_DIRECTORY_NAME
    _ensure_directory(predecessor_root, uid=uid, gid=gid)
    _install_exact(
        predecessor_root / package.STAGED_UNIT_INPUT_PLAN_PATH.name,
        predecessor.plan_raw,
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    _checkpoint("predecessor_plan_archived")
    _install_exact(
        predecessor_root / package.STAGED_UNIT_INPUT_APPROVAL_PATH.name,
        predecessor.approval_raw,
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    _checkpoint("predecessor_approval_archived")
    _install_exact(
        predecessor_root / package.FIXED_UNIT_INPUTS_PATH.name,
        predecessor.fixed_inputs_raw,
        uid=uid,
        gid=gid,
        mode=package.FIXED_UNIT_INPUTS_MODE,
    )
    _checkpoint("predecessor_fixed_inputs_archived")
    _archived_triplet(
        transaction_path,
        transaction,
        uid=uid,
        gid=gid,
    )
    return transaction


def _prepared_receipt(
    transaction_path: Path,
    transaction: Mapping[str, Any],
) -> Mapping[str, Any]:
    unsigned = {
        "schema": PREPARED_RECEIPT_SCHEMA,
        "predecessor_revision": transaction["predecessor_revision"],
        "predecessor_plan_sha256": transaction["predecessor_plan_sha256"],
        "predecessor_approval_sha256": transaction[
            "predecessor_approval_sha256"
        ],
        "predecessor_fixed_inputs_sha256": transaction[
            "predecessor_fixed_inputs_sha256"
        ],
        "authorization_checked_at_unix": transaction[
            "authorization_checked_at_unix"
        ],
        "transaction_sha256": transaction["transaction_sha256"],
        "successor_revision": transaction["successor_revision"],
        "successor_publication_sha256": transaction[
            "successor_publication_sha256"
        ],
        "successor_plan_sha256": transaction["successor_plan_sha256"],
        "successor_approval_sha256": transaction[
            "successor_approval_sha256"
        ],
        "successor_fixed_inputs_sha256": transaction[
            "successor_fixed_inputs_sha256"
        ],
        "audit_transaction_path": str(transaction_path),
        "live_plan_path": str(package.STAGED_UNIT_INPUT_PLAN_PATH),
        "live_approval_path": str(package.STAGED_UNIT_INPUT_APPROVAL_PATH),
        "live_fixed_inputs_path": str(package.FIXED_UNIT_INPUTS_PATH),
        "live_triplet_unchanged": True,
        "mutation_performed": False,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {**unsigned, "receipt_sha256": _sha(_canonical(unsigned))}


def validate_prepared_rotation_receipt(
    value: Any,
    *,
    publication: Mapping[str, Any],
) -> Mapping[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != _PREPARED_RECEIPT_FIELDS
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_prepared_receipt_invalid"
        )
    unsigned = {
        name: item for name, item in value.items() if name != "receipt_sha256"
    }
    try:
        successor = _successor(
            publication,
            now_unix=0,
            require_fresh=False,
        )
    except UnitInputRotationError as exc:
        raise UnitInputRotationError(
            "unit_input_rotation_prepared_receipt_invalid"
        ) from exc
    transaction_unsigned = {
        "schema": TRANSACTION_SCHEMA,
        "predecessor_revision": value.get("predecessor_revision"),
        "predecessor_plan_sha256": value.get("predecessor_plan_sha256"),
        "predecessor_approval_sha256": value.get(
            "predecessor_approval_sha256"
        ),
        "predecessor_fixed_inputs_sha256": value.get(
            "predecessor_fixed_inputs_sha256"
        ),
        "authorization_checked_at_unix": value.get(
            "authorization_checked_at_unix"
        ),
        "successor_revision": value.get("successor_revision"),
        "successor_publication_sha256": value.get(
            "successor_publication_sha256"
        ),
        "successor_plan_sha256": value.get("successor_plan_sha256"),
        "successor_approval_sha256": value.get(
            "successor_approval_sha256"
        ),
        "successor_fixed_inputs_sha256": value.get(
            "successor_fixed_inputs_sha256"
        ),
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    predecessor_plan = str(value.get("predecessor_plan_sha256", ""))
    expected_audit = (
        cutover.EVIDENCE_ROOT
        / AUDIT_DIRECTORY_NAME
        / (f"{predecessor_plan}-{successor.publication['publication_sha256']}")
    )
    if (
        value.get("schema") != PREPARED_RECEIPT_SCHEMA
        or package.REVISION.fullmatch(
            str(value.get("predecessor_revision", ""))
        )
        is None
        or value.get("predecessor_revision") == successor.revision
        or type(value.get("authorization_checked_at_unix")) is not int
        or not successor.approval["issued_at_unix"]
        <= value["authorization_checked_at_unix"]
        < successor.approval["expires_at_unix"]
        or value.get("successor_revision") != successor.revision
        or value.get("successor_publication_sha256")
        != successor.publication["publication_sha256"]
        or value.get("successor_plan_sha256")
        != successor.plan["plan_sha256"]
        or value.get("successor_approval_sha256")
        != successor.approval["approval_sha256"]
        or any(
            _SHA256.fullmatch(str(value.get(name, ""))) is None
            for name in (
                "predecessor_plan_sha256",
                "predecessor_approval_sha256",
                "predecessor_fixed_inputs_sha256",
                "successor_plan_sha256",
                "successor_approval_sha256",
                "successor_fixed_inputs_sha256",
                "transaction_sha256",
                "receipt_sha256",
            )
        )
        or value.get("transaction_sha256")
        != _sha(_canonical(transaction_unsigned))
        or value.get("audit_transaction_path") != str(expected_audit)
        or value.get("live_plan_path")
        != str(package.STAGED_UNIT_INPUT_PLAN_PATH)
        or value.get("live_approval_path")
        != str(package.STAGED_UNIT_INPUT_APPROVAL_PATH)
        or value.get("live_fixed_inputs_path")
        != str(package.FIXED_UNIT_INPUTS_PATH)
        or value.get("successor_fixed_inputs_sha256")
        != _sha(successor.fixed_inputs_raw)
        or value.get("live_triplet_unchanged") is not True
        or value.get("mutation_performed") is not False
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
        or value.get("receipt_sha256") != _sha(_canonical(unsigned))
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_prepared_receipt_invalid"
        )
    return dict(value)


def _load_prepared_receipt(
    transaction_path: Path,
    *,
    transaction: Mapping[str, Any],
    publication: Mapping[str, Any],
    uid: int,
    gid: int,
) -> Mapping[str, Any] | None:
    path = transaction_path / PREPARED_RECEIPT_FILE_NAME
    if not os.path.lexists(path):
        return None
    receipt = validate_prepared_rotation_receipt(
        _decode(
            _read_exact(
                path,
                uid=uid,
                gid=gid,
                mode=0o400,
            )
        ),
        publication=publication,
    )
    if receipt != _prepared_receipt(transaction_path, transaction):
        raise UnitInputRotationError(
            "unit_input_rotation_prepared_receipt_invalid"
        )
    return receipt


def _persisted_successor(
    transaction_path: Path,
    transaction: Mapping[str, Any],
    *,
    uid: int,
    gid: int,
) -> _Successor:
    raw = _read_exact(
        transaction_path / PUBLICATION_FILE_NAME,
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    persisted = _successor(
        _decode(raw),
        now_unix=0,
        require_fresh=False,
    )
    if (
        transaction["successor_revision"] != persisted.revision
        or transaction["successor_publication_sha256"]
        != persisted.publication["publication_sha256"]
        or transaction["successor_plan_sha256"] != persisted.plan["plan_sha256"]
        or transaction["successor_approval_sha256"]
        != persisted.approval["approval_sha256"]
        or transaction["successor_fixed_inputs_sha256"]
        != _sha(persisted.fixed_inputs_raw)
    ):
        raise UnitInputRotationError("unit_input_rotation_transaction_invalid")
    _validate_transaction_authorization(transaction, persisted)
    return persisted


def _validate_transaction_authorization(
    transaction: Mapping[str, Any],
    successor: _Successor,
) -> None:
    try:
        package.validate_unit_input_approval(
            successor.approval,
            plan=successor.plan,
            now_unix=transaction["authorization_checked_at_unix"],
        )
    except (PermissionError, TypeError, ValueError, package.PackagingError) as exc:
        raise UnitInputRotationError(
            "unit_input_rotation_transaction_invalid"
        ) from exc


def _successor_from_transaction(
    transaction_path: Path,
    transaction: Mapping[str, Any],
    incoming: _Successor,
    *,
    uid: int,
    gid: int,
) -> _Successor:
    persisted = _persisted_successor(
        transaction_path,
        transaction,
        uid=uid,
        gid=gid,
    )
    if persisted.publication_raw != incoming.publication_raw:
        raise UnitInputRotationError("unit_input_rotation_successor_conflict")
    return persisted


def _checkpoint(_name: str) -> None:
    """Test seam for failures at durable transaction boundaries."""


def _require_fresh(successor: _Successor, *, now_unix: int) -> None:
    try:
        package.validate_unit_input_approval(
            successor.approval,
            plan=successor.plan,
            now_unix=now_unix,
        )
    except (PermissionError, TypeError, ValueError, package.PackagingError) as exc:
        raise UnitInputRotationError("unit_input_rotation_publication_expired") from exc


def _validate_live_successor(
    successor: _SuccessorLike,
    *,
    uid: int,
    gid: int,
) -> None:
    observed = (
        _read_exact(
            package.STAGED_UNIT_INPUT_PLAN_PATH,
            uid=uid,
            gid=gid,
            mode=0o400,
        ),
        _read_exact(
            package.STAGED_UNIT_INPUT_APPROVAL_PATH,
            uid=uid,
            gid=gid,
            mode=0o400,
        ),
        _read_exact(
            package.FIXED_UNIT_INPUTS_PATH,
            uid=uid,
            gid=gid,
            mode=package.FIXED_UNIT_INPUTS_MODE,
        ),
    )
    if observed != (
        successor.plan_raw,
        successor.approval_raw,
        successor.fixed_inputs_raw,
    ):
        raise UnitInputRotationError("unit_input_rotation_terminal_state_invalid")


def _validate_live_predecessor(
    predecessor: _AuthorityTripletLike,
    *,
    uid: int,
    gid: int,
) -> None:
    observed = (
        _read_exact(
            package.STAGED_UNIT_INPUT_PLAN_PATH,
            uid=uid,
            gid=gid,
            mode=0o400,
        ),
        _read_exact(
            package.STAGED_UNIT_INPUT_APPROVAL_PATH,
            uid=uid,
            gid=gid,
            mode=0o400,
        ),
        _read_exact(
            package.FIXED_UNIT_INPUTS_PATH,
            uid=uid,
            gid=gid,
            mode=package.FIXED_UNIT_INPUTS_MODE,
        ),
    )
    if observed != (
        predecessor.plan_raw,
        predecessor.approval_raw,
        predecessor.fixed_inputs_raw,
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_prepared_state_invalid"
        )


def _publish_prepared_receipt(
    transaction_path: Path,
    transaction: Mapping[str, Any],
    predecessor: _AuthorityTriplet,
    successor: _Successor,
    *,
    uid: int,
    gid: int,
) -> Mapping[str, Any]:
    _validate_live_predecessor(predecessor, uid=uid, gid=gid)
    receipt = _prepared_receipt(transaction_path, transaction)
    _install_exact(
        transaction_path / PREPARED_RECEIPT_FILE_NAME,
        _canonical(receipt),
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    _checkpoint("prepared_receipt_published")
    _validate_live_predecessor(predecessor, uid=uid, gid=gid)
    persisted = _load_prepared_receipt(
        transaction_path,
        transaction=transaction,
        publication=successor.publication,
        uid=uid,
        gid=gid,
    )
    if persisted is None:
        raise UnitInputRotationError(
            "unit_input_rotation_prepared_receipt_invalid"
        )
    return persisted


def _live_successor_complete(
    successor: _SuccessorLike,
    *,
    uid: int,
    gid: int,
) -> bool:
    return (
        _optional_exact(
            package.STAGED_UNIT_INPUT_PLAN_PATH,
            uid=uid,
            gid=gid,
            mode=0o400,
        ),
        _optional_exact(
            package.STAGED_UNIT_INPUT_APPROVAL_PATH,
            uid=uid,
            gid=gid,
            mode=0o400,
        ),
        _optional_exact(
            package.FIXED_UNIT_INPUTS_PATH,
            uid=uid,
            gid=gid,
            mode=package.FIXED_UNIT_INPUTS_MODE,
        ),
    ) == (
        successor.plan_raw,
        successor.approval_raw,
        successor.fixed_inputs_raw,
    )


def _rollback(
    predecessor: _AuthorityTripletLike,
    successor: _SuccessorLike,
    *,
    uid: int,
    gid: int,
) -> None:
    fixed = _optional_exact(
        package.FIXED_UNIT_INPUTS_PATH,
        uid=uid,
        gid=gid,
        mode=package.FIXED_UNIT_INPUTS_MODE,
    )
    if fixed == successor.fixed_inputs_raw:
        _remove_if_exact(
            package.FIXED_UNIT_INPUTS_PATH,
            successor.fixed_inputs_raw,
            uid=uid,
            gid=gid,
            mode=package.FIXED_UNIT_INPUTS_MODE,
        )
    elif fixed not in {None, predecessor.fixed_inputs_raw}:
        raise UnitInputRotationError("unit_input_rotation_rollback_conflict")
    for path, old, new in (
        (
            package.STAGED_UNIT_INPUT_APPROVAL_PATH,
            predecessor.approval_raw,
            successor.approval_raw,
        ),
        (
            package.STAGED_UNIT_INPUT_PLAN_PATH,
            predecessor.plan_raw,
            successor.plan_raw,
        ),
    ):
        observed = _optional_exact(path, uid=uid, gid=gid, mode=0o400)
        if observed == new:
            _remove_if_exact(path, new, uid=uid, gid=gid, mode=0o400)
        elif observed not in {None, old}:
            raise UnitInputRotationError("unit_input_rotation_rollback_conflict")
    _install_exact(
        package.STAGED_UNIT_INPUT_PLAN_PATH,
        predecessor.plan_raw,
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    _install_exact(
        package.STAGED_UNIT_INPUT_APPROVAL_PATH,
        predecessor.approval_raw,
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    _install_exact(
        package.FIXED_UNIT_INPUTS_PATH,
        predecessor.fixed_inputs_raw,
        uid=uid,
        gid=gid,
        mode=package.FIXED_UNIT_INPUTS_MODE,
    )


def _activate_successor_triplet(
    predecessor: _AuthorityTripletLike,
    successor: _SuccessorLike,
    *,
    uid: int,
    gid: int,
) -> None:
    if _live_successor_complete(successor, uid=uid, gid=gid):
        return
    _checkpoint("audit_prepared")
    try:
        fixed = _optional_exact(
            package.FIXED_UNIT_INPUTS_PATH,
            uid=uid,
            gid=gid,
            mode=package.FIXED_UNIT_INPUTS_MODE,
        )
        if fixed == predecessor.fixed_inputs_raw:
            _remove_if_exact(
                package.FIXED_UNIT_INPUTS_PATH,
                predecessor.fixed_inputs_raw,
                uid=uid,
                gid=gid,
                mode=package.FIXED_UNIT_INPUTS_MODE,
            )
        elif fixed not in {None, successor.fixed_inputs_raw}:
            raise UnitInputRotationError("unit_input_rotation_conflict")
        _checkpoint("predecessor_fixed_inputs_removed")
        for name, path, old, new in (
            (
                "predecessor_approval_removed",
                package.STAGED_UNIT_INPUT_APPROVAL_PATH,
                predecessor.approval_raw,
                successor.approval_raw,
            ),
            (
                "predecessor_plan_removed",
                package.STAGED_UNIT_INPUT_PLAN_PATH,
                predecessor.plan_raw,
                successor.plan_raw,
            ),
        ):
            observed = _optional_exact(
                path,
                uid=uid,
                gid=gid,
                mode=0o400,
            )
            if observed == old:
                _remove_if_exact(
                    path,
                    old,
                    uid=uid,
                    gid=gid,
                    mode=0o400,
                )
            elif observed not in {None, new}:
                raise UnitInputRotationError("unit_input_rotation_conflict")
            _checkpoint(name)
        _install_exact(
            package.STAGED_UNIT_INPUT_PLAN_PATH,
            successor.plan_raw,
            uid=uid,
            gid=gid,
            mode=0o400,
        )
        _checkpoint("successor_plan_staged")
        _install_exact(
            package.STAGED_UNIT_INPUT_APPROVAL_PATH,
            successor.approval_raw,
            uid=uid,
            gid=gid,
            mode=0o400,
        )
        _checkpoint("successor_approval_staged")
        _install_exact(
            package.FIXED_UNIT_INPUTS_PATH,
            successor.fixed_inputs_raw,
            uid=uid,
            gid=gid,
            mode=package.FIXED_UNIT_INPUTS_MODE,
        )
        _checkpoint("successor_fixed_inputs_staged")
        _validate_live_successor(
            successor,
            uid=uid,
            gid=gid,
        )
    except Exception as primary:
        try:
            _rollback(
                predecessor,
                successor,
                uid=uid,
                gid=gid,
            )
        except Exception as rollback:
            raise ExceptionGroup(
                "unit-input rotation and rollback failed",
                [primary, rollback],
            ) from primary
        raise


def _receipt(
    transaction_path: Path,
    transaction: Mapping[str, Any],
) -> Mapping[str, Any]:
    unsigned = {
        "schema": RECEIPT_SCHEMA,
        "predecessor_revision": transaction["predecessor_revision"],
        "predecessor_plan_sha256": transaction["predecessor_plan_sha256"],
        "predecessor_approval_sha256": transaction["predecessor_approval_sha256"],
        "predecessor_fixed_inputs_sha256": transaction[
            "predecessor_fixed_inputs_sha256"
        ],
        "authorization_checked_at_unix": transaction[
            "authorization_checked_at_unix"
        ],
        "transaction_sha256": transaction["transaction_sha256"],
        "successor_revision": transaction["successor_revision"],
        "successor_publication_sha256": transaction["successor_publication_sha256"],
        "successor_plan_sha256": transaction["successor_plan_sha256"],
        "successor_approval_sha256": transaction["successor_approval_sha256"],
        "successor_fixed_inputs_sha256": transaction["successor_fixed_inputs_sha256"],
        "audit_transaction_path": str(transaction_path),
        "staged_plan_path": str(package.STAGED_UNIT_INPUT_PLAN_PATH),
        "staged_approval_path": str(package.STAGED_UNIT_INPUT_APPROVAL_PATH),
        "fixed_inputs_path": str(package.FIXED_UNIT_INPUTS_PATH),
        "successor_triplet_complete": True,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {**unsigned, "receipt_sha256": _sha(_canonical(unsigned))}


def _finalized_receipt(
    transaction_path: Path,
    transaction: Mapping[str, Any],
    prepared_receipt: Mapping[str, Any],
) -> Mapping[str, Any]:
    legacy = _receipt(transaction_path, transaction)
    unsigned = {
        **{
            name: item
            for name, item in legacy.items()
            if name not in {"schema", "receipt_sha256"}
        },
        "schema": FINALIZED_RECEIPT_SCHEMA,
        "prepared_receipt_sha256": prepared_receipt["receipt_sha256"],
    }
    return {**unsigned, "receipt_sha256": _sha(_canonical(unsigned))}


def _publish_receipt(
    transaction_path: Path,
    transaction: Mapping[str, Any],
    successor: _Successor,
    *,
    uid: int,
    gid: int,
) -> Mapping[str, Any]:
    _validate_live_successor(successor, uid=uid, gid=gid)
    prepared = _load_prepared_receipt(
        transaction_path,
        transaction=transaction,
        publication=successor.publication,
        uid=uid,
        gid=gid,
    )
    receipt = (
        _receipt(transaction_path, transaction)
        if prepared is None
        else _finalized_receipt(
            transaction_path,
            transaction,
            prepared,
        )
    )
    _install_exact(
        transaction_path / RECEIPT_FILE_NAME,
        _canonical(receipt),
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    return validate_rotation_receipt(
        receipt,
        publication=successor.publication,
    )


def _publish_finalized_receipt(
    transaction_path: Path,
    transaction: Mapping[str, Any],
    predecessor: _AuthorityTriplet,
    successor: _Successor,
    prepared_receipt: Mapping[str, Any],
    *,
    uid: int,
    gid: int,
) -> Mapping[str, Any]:
    _archived_triplet(
        transaction_path,
        transaction,
        uid=uid,
        gid=gid,
    )
    _validate_live_successor(successor, uid=uid, gid=gid)
    persisted_prepared = _load_prepared_receipt(
        transaction_path,
        transaction=transaction,
        publication=successor.publication,
        uid=uid,
        gid=gid,
    )
    if (
        persisted_prepared is None
        or persisted_prepared != prepared_receipt
        or persisted_prepared["predecessor_plan_sha256"]
        != predecessor.plan["plan_sha256"]
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_finalize_authorization_invalid"
        )
    receipt = _finalized_receipt(
        transaction_path,
        transaction,
        persisted_prepared,
    )
    _install_exact(
        transaction_path / RECEIPT_FILE_NAME,
        _canonical(receipt),
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    return validate_rotation_receipt(
        receipt,
        publication=successor.publication,
    )


def validate_rotation_receipt(
    value: Any,
    *,
    publication: Mapping[str, Any],
) -> Mapping[str, Any]:
    schema = value.get("schema") if isinstance(value, Mapping) else None
    fields = (
        _RECEIPT_FIELDS
        if schema == RECEIPT_SCHEMA
        else _FINALIZED_RECEIPT_FIELDS
        if schema == FINALIZED_RECEIPT_SCHEMA
        else frozenset()
    )
    if not isinstance(value, Mapping) or set(value) != fields:
        raise UnitInputRotationError("unit_input_rotation_receipt_invalid")
    unsigned = {name: item for name, item in value.items() if name != "receipt_sha256"}
    try:
        successor = _successor(
            publication,
            now_unix=0,
            require_fresh=False,
        )
    except UnitInputRotationError as exc:
        raise UnitInputRotationError("unit_input_rotation_receipt_invalid") from exc
    transaction_unsigned = {
        "schema": TRANSACTION_SCHEMA,
        "predecessor_revision": value.get("predecessor_revision"),
        "predecessor_plan_sha256": value.get("predecessor_plan_sha256"),
        "predecessor_approval_sha256": value.get(
            "predecessor_approval_sha256"
        ),
        "predecessor_fixed_inputs_sha256": value.get(
            "predecessor_fixed_inputs_sha256"
        ),
        "authorization_checked_at_unix": value.get(
            "authorization_checked_at_unix"
        ),
        "successor_revision": value.get("successor_revision"),
        "successor_publication_sha256": value.get(
            "successor_publication_sha256"
        ),
        "successor_plan_sha256": value.get("successor_plan_sha256"),
        "successor_approval_sha256": value.get(
            "successor_approval_sha256"
        ),
        "successor_fixed_inputs_sha256": value.get(
            "successor_fixed_inputs_sha256"
        ),
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    predecessor_plan = str(value.get("predecessor_plan_sha256", ""))
    expected_audit = (
        cutover.EVIDENCE_ROOT
        / AUDIT_DIRECTORY_NAME
        / (f"{predecessor_plan}-{successor.publication['publication_sha256']}")
    )
    if (
        schema not in {RECEIPT_SCHEMA, FINALIZED_RECEIPT_SCHEMA}
        or package.REVISION.fullmatch(str(value.get("predecessor_revision", "")))
        is None
        or type(value.get("authorization_checked_at_unix")) is not int
        or not successor.approval["issued_at_unix"]
        <= value["authorization_checked_at_unix"]
        < successor.approval["expires_at_unix"]
        or value.get("successor_revision") != successor.revision
        or value.get("successor_publication_sha256")
        != successor.publication["publication_sha256"]
        or value.get("successor_plan_sha256") != successor.plan["plan_sha256"]
        or value.get("successor_approval_sha256")
        != successor.approval["approval_sha256"]
        or any(
            _SHA256.fullmatch(str(value.get(name, ""))) is None
            for name in (
                "predecessor_plan_sha256",
                "predecessor_approval_sha256",
                "predecessor_fixed_inputs_sha256",
                "successor_plan_sha256",
                "successor_approval_sha256",
                "successor_fixed_inputs_sha256",
                "transaction_sha256",
                "receipt_sha256",
            )
        )
        or (
            schema == FINALIZED_RECEIPT_SCHEMA
            and _SHA256.fullmatch(
                str(value.get("prepared_receipt_sha256", ""))
            )
            is None
        )
        or value.get("transaction_sha256")
        != _sha(_canonical(transaction_unsigned))
        or value.get("audit_transaction_path") != str(expected_audit)
        or value.get("staged_plan_path") != str(package.STAGED_UNIT_INPUT_PLAN_PATH)
        or value.get("staged_approval_path")
        != str(package.STAGED_UNIT_INPUT_APPROVAL_PATH)
        or value.get("fixed_inputs_path") != str(package.FIXED_UNIT_INPUTS_PATH)
        or value.get("successor_fixed_inputs_sha256")
        != _sha(successor.fixed_inputs_raw)
        or value.get("successor_triplet_complete") is not True
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
        or value.get("receipt_sha256") != _sha(_canonical(unsigned))
    ):
        raise UnitInputRotationError("unit_input_rotation_receipt_invalid")
    return dict(value)


def _rotation_identity(*, require_root: bool) -> tuple[int, int]:
    if require_root and (
        not sys.platform.startswith("linux")
        or os.geteuid() != 0  # windows-footgun: ok — Linux production boundary
    ):
        raise UnitInputRotationError("unit_input_rotation_requires_linux_root")
    uid = 0 if require_root else os.geteuid()  # windows-footgun: ok
    gid = 0 if require_root else os.getegid()  # windows-footgun: ok
    if require_root and (
        package.CUTOVER_STAGED_ROOT != PRODUCTION_STAGED_ROOT
        or package.STAGED_UNIT_INPUT_PLAN_PATH
        != PRODUCTION_STAGED_ROOT / "unit-input-plan.json"
        or package.STAGED_UNIT_INPUT_APPROVAL_PATH
        != PRODUCTION_STAGED_ROOT / "unit-input-approval.json"
        or package.FIXED_UNIT_INPUTS_PATH
        != PRODUCTION_STAGED_ROOT / "production-unit-inputs.json"
        or cutover.EVIDENCE_ROOT
        != Path("/var/lib/muncho-production-legacy-cutover")
    ):
        raise UnitInputRotationError("unit_input_rotation_boundary_invalid")
    staged_root = package.STAGED_UNIT_INPUT_PLAN_PATH.parent
    if (
        package.STAGED_UNIT_INPUT_APPROVAL_PATH.parent != staged_root
        or package.FIXED_UNIT_INPUTS_PATH.parent != staged_root
    ):
        raise UnitInputRotationError("unit_input_rotation_boundary_invalid")
    return uid, gid


def _require_same_successor(
    transaction: Mapping[str, Any],
    incoming: _Successor,
) -> None:
    if (
        transaction["successor_revision"] != incoming.revision
        or transaction["successor_publication_sha256"]
        != incoming.publication["publication_sha256"]
        or transaction["successor_plan_sha256"]
        != incoming.plan["plan_sha256"]
        or transaction["successor_approval_sha256"]
        != incoming.approval["approval_sha256"]
        or transaction["successor_fixed_inputs_sha256"]
        != _sha(incoming.fixed_inputs_raw)
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_successor_conflict"
        )


def prepare_unit_input_authority_rotation(
    publication: Mapping[str, Any],
    *,
    require_root: bool = True,
    now_unix: int | None = None,
    lock_factory: Callable[[], Any] | None = None,
) -> Mapping[str, Any]:
    """Authorize and archive one exact rotation without mutating live inputs."""

    uid, gid = _rotation_identity(require_root=require_root)
    incoming = _successor(
        publication,
        now_unix=0,
        require_fresh=False,
    )
    try:
        context = authority_lock.authority_activation_lock(
            require_root=require_root,
            lock_factory=lock_factory,
        )
        with context:

            def gate_now() -> int:
                return int(time.time()) if now_unix is None else now_unix

            staged_root = package.STAGED_UNIT_INPUT_PLAN_PATH.parent
            _require_directory(staged_root, uid=uid, gid=gid)
            root = _audit_root(uid=uid, gid=gid)
            completed_match: tuple[
                Path,
                Mapping[str, Any],
                _Successor,
            ] | None = None
            incomplete: list[
                tuple[Path, Mapping[str, Any] | None]
            ] = []
            for directory in _transaction_directories(
                root,
                uid=uid,
                gid=gid,
            ):
                transaction = _load_transaction(
                    directory,
                    uid=uid,
                    gid=gid,
                )
                if (
                    transaction is None
                    or not os.path.lexists(directory / RECEIPT_FILE_NAME)
                ):
                    incomplete.append((directory, transaction))
                    continue
                persisted = _persisted_successor(
                    directory,
                    transaction,
                    uid=uid,
                    gid=gid,
                )
                _archived_triplet(
                    directory,
                    transaction,
                    uid=uid,
                    gid=gid,
                )
                receipt = _load_receipt(
                    directory,
                    transaction=transaction,
                    publication=persisted.publication,
                    uid=uid,
                    gid=gid,
                )
                if receipt is None:
                    raise UnitInputRotationError(
                        "unit_input_rotation_receipt_invalid"
                    )
                if persisted.publication_raw == incoming.publication_raw:
                    if completed_match is not None:
                        raise UnitInputRotationError(
                            "unit_input_rotation_recovery_ambiguous"
                        )
                    completed_match = (
                        directory,
                        transaction,
                        persisted,
                    )
            if incomplete:
                if len(incomplete) != 1:
                    raise UnitInputRotationError(
                        "unit_input_rotation_recovery_ambiguous"
                    )
                transaction_path, transaction = incomplete[0]
                expected_suffix = (
                    "-" + incoming.publication["publication_sha256"]
                )
                if not transaction_path.name.endswith(expected_suffix):
                    raise UnitInputRotationError(
                        "unit_input_rotation_successor_conflict"
                    )
                if transaction is None:
                    predecessor = _triplet(uid=uid, gid=gid)
                    if not transaction_path.name.startswith(
                        predecessor.plan["plan_sha256"] + "-"
                    ):
                        raise UnitInputRotationError(
                            "unit_input_rotation_recovery_ambiguous"
                        )
                    authorization_checked_at_unix = gate_now()
                    _require_fresh(
                        incoming,
                        now_unix=authorization_checked_at_unix,
                    )
                    transaction = _prepare_transaction(
                        transaction_path=transaction_path,
                        predecessor=predecessor,
                        successor=incoming,
                        authorization_checked_at_unix=(
                            authorization_checked_at_unix
                        ),
                        authorization_clock=gate_now,
                        uid=uid,
                        gid=gid,
                    )
                else:
                    _require_same_successor(transaction, incoming)
                    _validate_transaction_authorization(
                        transaction,
                        incoming,
                    )
                    try:
                        predecessor = _archived_triplet(
                            transaction_path,
                            transaction,
                            uid=uid,
                            gid=gid,
                        )
                    except UnitInputRotationError:
                        predecessor = _triplet(uid=uid, gid=gid)
                        if (
                            _transaction_value(
                                predecessor,
                                incoming,
                                authorization_checked_at_unix=transaction[
                                    "authorization_checked_at_unix"
                                ],
                            )
                            != transaction
                        ):
                            raise UnitInputRotationError(
                                "unit_input_rotation_recovery_ambiguous"
                            ) from None
                        transaction = _prepare_transaction(
                            transaction_path=transaction_path,
                            predecessor=predecessor,
                            successor=incoming,
                            authorization_checked_at_unix=transaction[
                                "authorization_checked_at_unix"
                            ],
                            uid=uid,
                            gid=gid,
                        )
                successor = _successor_from_transaction(
                    transaction_path,
                    transaction,
                    incoming,
                    uid=uid,
                    gid=gid,
                )
                predecessor = _archived_triplet(
                    transaction_path,
                    transaction,
                    uid=uid,
                    gid=gid,
                )
            elif completed_match is not None:
                transaction_path, transaction, successor = completed_match
                prepared = _load_prepared_receipt(
                    transaction_path,
                    transaction=transaction,
                    publication=successor.publication,
                    uid=uid,
                    gid=gid,
                )
                if prepared is None:
                    raise UnitInputRotationError(
                        "unit_input_rotation_already_finalized"
                    )
                _validate_live_successor(successor, uid=uid, gid=gid)
                return prepared
            else:
                predecessor = _triplet(uid=uid, gid=gid)
                if predecessor.revision == incoming.revision:
                    raise UnitInputRotationError(
                        "unit_input_rotation_successor_conflict"
                    )
                authorization_checked_at_unix = gate_now()
                _require_fresh(
                    incoming,
                    now_unix=authorization_checked_at_unix,
                )
                transaction_path = root / (
                    f"{predecessor.plan['plan_sha256']}-"
                    f"{incoming.publication['publication_sha256']}"
                )
                if os.path.lexists(transaction_path):
                    raise UnitInputRotationError(
                        "unit_input_rotation_successor_conflict"
                    )
                transaction = _prepare_transaction(
                    transaction_path=transaction_path,
                    predecessor=predecessor,
                    successor=incoming,
                    authorization_checked_at_unix=(
                        authorization_checked_at_unix
                    ),
                    authorization_clock=gate_now,
                    uid=uid,
                    gid=gid,
                )
                successor = incoming
            persisted_prepared = _load_prepared_receipt(
                transaction_path,
                transaction=transaction,
                publication=successor.publication,
                uid=uid,
                gid=gid,
            )
            if persisted_prepared is not None:
                if _live_successor_complete(
                    successor,
                    uid=uid,
                    gid=gid,
                ):
                    return persisted_prepared
                _validate_live_predecessor(
                    predecessor,
                    uid=uid,
                    gid=gid,
                )
                return persisted_prepared
            return _publish_prepared_receipt(
                transaction_path,
                transaction,
                predecessor,
                successor,
                uid=uid,
                gid=gid,
            )
    except UnitInputRotationError:
        raise
    except (OSError, RuntimeError, package.PackagingError) as exc:
        raise UnitInputRotationError("unit_input_rotation_failed") from exc


def finalize_prepared_unit_input_authority_rotation(
    publication: Mapping[str, Any],
    prepared_receipt: Mapping[str, Any],
    *,
    expected_transaction_sha256: str,
    require_root: bool = True,
    lock_factory: Callable[[], Any] | None = None,
) -> Mapping[str, Any]:
    """Apply only one exact persisted prepared authorization."""

    uid, gid = _rotation_identity(require_root=require_root)
    incoming = _successor(
        publication,
        now_unix=0,
        require_fresh=False,
    )
    supplied = validate_prepared_rotation_receipt(
        prepared_receipt,
        publication=incoming.publication,
    )
    if (
        _SHA256.fullmatch(str(expected_transaction_sha256 or "")) is None
        or supplied["transaction_sha256"]
        != expected_transaction_sha256
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_finalize_authorization_invalid"
        )
    try:
        context = authority_lock.authority_activation_lock(
            require_root=require_root,
            lock_factory=lock_factory,
        )
        with context:
            staged_root = package.STAGED_UNIT_INPUT_PLAN_PATH.parent
            _require_directory(staged_root, uid=uid, gid=gid)
            root = cutover.EVIDENCE_ROOT / AUDIT_DIRECTORY_NAME
            _require_directory(root, uid=uid, gid=gid)
            transaction_path = Path(supplied["audit_transaction_path"])
            _require_directory(transaction_path, uid=uid, gid=gid)
            transaction = _load_transaction(
                transaction_path,
                uid=uid,
                gid=gid,
            )
            if transaction is None:
                raise UnitInputRotationError(
                    "unit_input_rotation_finalize_authorization_invalid"
                )
            _require_same_successor(transaction, incoming)
            if (
                transaction["transaction_sha256"]
                != expected_transaction_sha256
            ):
                raise UnitInputRotationError(
                    "unit_input_rotation_finalize_authorization_invalid"
                )
            for directory in _transaction_directories(
                root,
                uid=uid,
                gid=gid,
            ):
                if (
                    directory != transaction_path
                    and not os.path.lexists(directory / RECEIPT_FILE_NAME)
                ):
                    raise UnitInputRotationError(
                        "unit_input_rotation_recovery_ambiguous"
                    )
            successor = _successor_from_transaction(
                transaction_path,
                transaction,
                incoming,
                uid=uid,
                gid=gid,
            )
            predecessor = _archived_triplet(
                transaction_path,
                transaction,
                uid=uid,
                gid=gid,
            )
            persisted_prepared = _load_prepared_receipt(
                transaction_path,
                transaction=transaction,
                publication=successor.publication,
                uid=uid,
                gid=gid,
            )
            if (
                persisted_prepared is None
                or persisted_prepared != supplied
            ):
                raise UnitInputRotationError(
                    "unit_input_rotation_finalize_authorization_invalid"
                )
            if os.path.lexists(transaction_path / RECEIPT_FILE_NAME):
                receipt = _load_receipt(
                    transaction_path,
                    transaction=transaction,
                    publication=successor.publication,
                    uid=uid,
                    gid=gid,
                )
                if (
                    receipt is None
                    or receipt["schema"] != FINALIZED_RECEIPT_SCHEMA
                    or receipt["prepared_receipt_sha256"]
                    != supplied["receipt_sha256"]
                ):
                    raise UnitInputRotationError(
                        "unit_input_rotation_finalize_authorization_invalid"
                    )
                _validate_live_successor(successor, uid=uid, gid=gid)
                return receipt
            _activate_successor_triplet(
                predecessor,
                successor,
                uid=uid,
                gid=gid,
            )
            return _publish_finalized_receipt(
                transaction_path,
                transaction,
                predecessor,
                successor,
                supplied,
                uid=uid,
                gid=gid,
            )
    except UnitInputRotationError:
        raise
    except ExceptionGroup as exc:
        raise UnitInputRotationError(
            "unit_input_rotation_rollback_failed"
        ) from exc
    except (OSError, RuntimeError, package.PackagingError) as exc:
        raise UnitInputRotationError("unit_input_rotation_failed") from exc


def rotate_unit_input_authority(
    value: Mapping[str, Any],
    *,
    require_root: bool = True,
    now_unix: int | None = None,
    lock_factory: Callable[[], Any] | None = None,
) -> Mapping[str, Any]:
    """Archive one predecessor triplet and stage one successor authority."""

    if require_root and (
        not sys.platform.startswith("linux")
        or os.geteuid() != 0  # windows-footgun: ok — Linux production boundary
    ):
        raise UnitInputRotationError("unit_input_rotation_requires_linux_root")
    uid = 0 if require_root else os.geteuid()  # windows-footgun: ok — POSIX boundary
    gid = 0 if require_root else os.getegid()  # windows-footgun: ok — POSIX boundary
    if require_root and (
        package.CUTOVER_STAGED_ROOT != PRODUCTION_STAGED_ROOT
        or package.STAGED_UNIT_INPUT_PLAN_PATH
        != PRODUCTION_STAGED_ROOT / "unit-input-plan.json"
        or package.STAGED_UNIT_INPUT_APPROVAL_PATH
        != PRODUCTION_STAGED_ROOT / "unit-input-approval.json"
        or package.FIXED_UNIT_INPUTS_PATH
        != PRODUCTION_STAGED_ROOT / "production-unit-inputs.json"
        or cutover.EVIDENCE_ROOT != Path("/var/lib/muncho-production-legacy-cutover")
    ):
        raise UnitInputRotationError("unit_input_rotation_boundary_invalid")
    incoming = _successor(value, now_unix=0, require_fresh=False)
    try:
        context = authority_lock.authority_activation_lock(
            require_root=require_root,
            lock_factory=lock_factory,
        )
        with context:

            def gate_now() -> int:
                return int(time.time()) if now_unix is None else now_unix

            staged_root = package.STAGED_UNIT_INPUT_PLAN_PATH.parent
            if (
                package.STAGED_UNIT_INPUT_APPROVAL_PATH.parent != staged_root
                or package.FIXED_UNIT_INPUTS_PATH.parent != staged_root
            ):
                raise UnitInputRotationError("unit_input_rotation_boundary_invalid")
            _require_directory(staged_root, uid=uid, gid=gid)
            root = _audit_root(uid=uid, gid=gid)
            directories = _transaction_directories(
                root,
                uid=uid,
                gid=gid,
            )
            completed_match: tuple[
                _Successor,
                Mapping[str, Any],
            ] | None = None
            incomplete: list[tuple[Path, Mapping[str, Any] | None]] = []
            for directory in directories:
                transaction = _load_transaction(
                    directory,
                    uid=uid,
                    gid=gid,
                )
                if transaction is None:
                    incomplete.append((directory, None))
                    continue
                receipt_path = directory / RECEIPT_FILE_NAME
                if os.path.lexists(receipt_path):
                    persisted = _persisted_successor(
                        directory,
                        transaction,
                        uid=uid,
                        gid=gid,
                    )
                    _archived_triplet(
                        directory,
                        transaction,
                        uid=uid,
                        gid=gid,
                    )
                    receipt = _load_receipt(
                        directory,
                        transaction=transaction,
                        publication=persisted.publication,
                        uid=uid,
                        gid=gid,
                    )
                    if receipt is None:
                        raise UnitInputRotationError(
                            "unit_input_rotation_receipt_invalid"
                        )
                    if persisted.publication_raw == incoming.publication_raw:
                        if completed_match is not None:
                            raise UnitInputRotationError(
                                "unit_input_rotation_recovery_ambiguous"
                            )
                        completed_match = (persisted, receipt)
                else:
                    incomplete.append((directory, transaction))
            if incomplete:
                if len(incomplete) != 1:
                    raise UnitInputRotationError(
                        "unit_input_rotation_recovery_ambiguous"
                    )
                transaction_path, transaction = incomplete[0]
                expected_suffix = "-" + incoming.publication["publication_sha256"]
                if not transaction_path.name.endswith(expected_suffix):
                    raise UnitInputRotationError(
                        "unit_input_rotation_successor_conflict"
                    )
                if transaction is None:
                    predecessor = _triplet(uid=uid, gid=gid)
                    if not transaction_path.name.startswith(
                        predecessor.plan["plan_sha256"] + "-"
                    ):
                        raise UnitInputRotationError(
                            "unit_input_rotation_recovery_ambiguous"
                        )
                    successor = incoming
                    authorization_checked_at_unix = gate_now()
                    _require_fresh(
                        successor,
                        now_unix=authorization_checked_at_unix,
                    )
                    transaction = _prepare_transaction(
                        transaction_path=transaction_path,
                        predecessor=predecessor,
                        successor=successor,
                        authorization_checked_at_unix=(
                            authorization_checked_at_unix
                        ),
                        authorization_clock=gate_now,
                        uid=uid,
                        gid=gid,
                    )
                else:
                    if (
                        transaction["successor_revision"] != incoming.revision
                        or transaction["successor_publication_sha256"]
                        != incoming.publication["publication_sha256"]
                        or transaction["successor_plan_sha256"]
                        != incoming.plan["plan_sha256"]
                        or transaction["successor_approval_sha256"]
                        != incoming.approval["approval_sha256"]
                        or transaction["successor_fixed_inputs_sha256"]
                        != _sha(incoming.fixed_inputs_raw)
                    ):
                        raise UnitInputRotationError(
                            "unit_input_rotation_successor_conflict"
                        )
                    _validate_transaction_authorization(
                        transaction,
                        incoming,
                    )
                    try:
                        predecessor = _archived_triplet(
                            transaction_path,
                            transaction,
                            uid=uid,
                            gid=gid,
                        )
                    except UnitInputRotationError:
                        predecessor = _triplet(uid=uid, gid=gid)
                        if (
                            _transaction_value(
                                predecessor,
                                incoming,
                                authorization_checked_at_unix=transaction[
                                    "authorization_checked_at_unix"
                                ],
                            )
                            != transaction
                        ):
                            raise UnitInputRotationError(
                                "unit_input_rotation_recovery_ambiguous"
                            ) from None
                        transaction = _prepare_transaction(
                            transaction_path=transaction_path,
                            predecessor=predecessor,
                            successor=incoming,
                            authorization_checked_at_unix=transaction[
                                "authorization_checked_at_unix"
                            ],
                            uid=uid,
                            gid=gid,
                        )
                    successor = _successor_from_transaction(
                        transaction_path,
                        transaction,
                        incoming,
                        uid=uid,
                        gid=gid,
                    )
                    predecessor = _archived_triplet(
                        transaction_path,
                        transaction,
                        uid=uid,
                        gid=gid,
                    )
            elif completed_match is not None:
                persisted, receipt = completed_match
                _validate_live_successor(
                    persisted,
                    uid=uid,
                    gid=gid,
                )
                return receipt
            else:
                predecessor = _triplet(uid=uid, gid=gid)
                if predecessor.revision == incoming.revision:
                    raise UnitInputRotationError(
                        "unit_input_rotation_successor_conflict"
                    )
                successor = incoming
                authorization_checked_at_unix = gate_now()
                _require_fresh(
                    successor,
                    now_unix=authorization_checked_at_unix,
                )
                transaction_path = root / (
                    f"{predecessor.plan['plan_sha256']}-"
                    f"{successor.publication['publication_sha256']}"
                )
                if os.path.lexists(transaction_path):
                    raise UnitInputRotationError(
                        "unit_input_rotation_successor_conflict"
                    )
                transaction = _prepare_transaction(
                    transaction_path=transaction_path,
                    predecessor=predecessor,
                    successor=successor,
                    authorization_checked_at_unix=(
                        authorization_checked_at_unix
                    ),
                    authorization_clock=gate_now,
                    uid=uid,
                    gid=gid,
                )
            _activate_successor_triplet(
                predecessor,
                successor,
                uid=uid,
                gid=gid,
            )
            return _publish_receipt(
                transaction_path,
                transaction,
                successor,
                uid=uid,
                gid=gid,
            )
    except UnitInputRotationError:
        raise
    except ExceptionGroup as exc:
        raise UnitInputRotationError("unit_input_rotation_rollback_failed") from exc
    except (OSError, RuntimeError, package.PackagingError) as exc:
        raise UnitInputRotationError("unit_input_rotation_failed") from exc


def _release_predecessor_trust(
    value: Mapping[str, Any],
    *,
    expected_predecessor_trust_sha256: str,
) -> Mapping[str, Any]:
    try:
        return release_update.validate_predecessor_trust(
            value,
            expected_trust_sha256=expected_predecessor_trust_sha256,
        )
    except (
        TypeError,
        ValueError,
        release_update.ProductionReleaseUpdateContractError,
    ) as exc:
        raise UnitInputRotationError(
            "unit_input_rotation_predecessor_trust_invalid"
        ) from exc


def _release_historical_v4_triplet(
    *,
    plan_value: Mapping[str, Any],
    approval_value: Mapping[str, Any],
    fixed_value: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    """Validate a historical v4 triplet without resurrecting its old lease.

    The current externally pinned predecessor envelope binds the resulting
    authority digests.  This validator therefore checks the complete v4
    document shapes, self-hashes, payload invariants, chronology, and owner
    signature, but deliberately does not require the predecessor's approval
    lease to still be live.
    """

    try:
        plan = release_inputs_v4._self_hashed(
            plan_value,
            fields=release_inputs_v4._PLAN_FIELDS,
            digest_field="plan_sha256",
            code="release_unit_inputs_v4_plan_invalid",
        )
        payload = release_inputs_v4.validate_payload(plan.get("unit_inputs"))
        predecessor_revision = str(plan.get("predecessor_revision", ""))
        revision = str(plan.get("release_revision", ""))
        public = str(plan.get("owner_public_key_ed25519_hex", ""))
        created = plan.get("created_at_unix")
        if (
            plan.get("schema") != release_inputs_v4.PLAN_SCHEMA
            or package.REVISION.fullmatch(predecessor_revision) is None
            or package.REVISION.fullmatch(revision) is None
            or predecessor_revision == revision
            or predecessor_revision[:12] == revision[:12]
            or any(
                _SHA256.fullmatch(str(plan.get(name, ""))) is None
                for name in (
                    "predecessor_trust_sha256",
                    "predecessor_authority_plan_sha256",
                    "predecessor_authority_approval_sha256",
                    "predecessor_fixed_inputs_sha256",
                    "predecessor_activation_receipt_sha256",
                    "owner_subject_sha256",
                    "owner_key_id",
                )
            )
            or _SHA256.fullmatch(public) is None
            or plan.get("owner_key_id") != _sha(bytes.fromhex(public))
            or type(created) is not int
            or created <= 0
            or plan.get("secret_material_recorded") is not False
            or plan.get("secret_digest_recorded") is not False
            or payload["discord_reconciliation_intent"]["release_revision"]
            != revision
        ):
            raise ValueError("historical v4 plan invalid")

        approval = release_inputs_v4._self_hashed(
            approval_value,
            fields=release_inputs_v4._APPROVAL_FIELDS,
            digest_field="approval_sha256",
            code="release_unit_inputs_v4_approval_invalid",
        )
        issued = approval.get("issued_at_unix")
        expires = approval.get("expires_at_unix")
        signature = str(approval.get("signature_ed25519_hex", ""))
        if (
            approval.get("schema") != release_inputs_v4.APPROVAL_SCHEMA
            or approval.get("purpose") != release_inputs_v4.APPROVAL_PURPOSE
            or approval.get("plan_sha256") != plan["plan_sha256"]
            or approval.get("predecessor_revision")
            != plan["predecessor_revision"]
            or approval.get("predecessor_trust_sha256")
            != plan["predecessor_trust_sha256"]
            or approval.get("release_revision") != revision
            or approval.get("owner_subject_sha256")
            != plan["owner_subject_sha256"]
            or approval.get("owner_public_key_ed25519_hex") != public
            or approval.get("owner_key_id") != plan["owner_key_id"]
            or _SHA256.fullmatch(str(approval.get("nonce_sha256", "")))
            is None
            or type(issued) is not int
            or type(expires) is not int
            or not 0
            <= issued - created
            <= release_inputs_v4.MAX_PLAN_AGE_AT_APPROVAL_SECONDS
            or not 1
            <= expires - issued
            <= release_inputs_v4.MAX_APPROVAL_LIFETIME_SECONDS
            or approval.get("approved") is not True
            or re.fullmatch(r"^[0-9a-f]{128}$", signature) is None
        ):
            raise ValueError("historical v4 approval invalid")
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public)).verify(
            bytes.fromhex(signature),
            release_inputs_v4.approval_signature_payload(approval),
        )

        fixed = release_inputs_v4._validate_fixed_shape(fixed_value)
        if (
            fixed["predecessor_revision"] != predecessor_revision
            or fixed["predecessor_trust_sha256"]
            != plan["predecessor_trust_sha256"]
            or fixed["predecessor_authority_plan_sha256"]
            != plan["predecessor_authority_plan_sha256"]
            or fixed["predecessor_authority_approval_sha256"]
            != plan["predecessor_authority_approval_sha256"]
            or fixed["predecessor_fixed_inputs_sha256"]
            != plan["predecessor_fixed_inputs_sha256"]
            or fixed["predecessor_activation_receipt_sha256"]
            != plan["predecessor_activation_receipt_sha256"]
            or fixed["release_revision"] != revision
            or fixed["unit_input_authority_plan_sha256"]
            != plan["plan_sha256"]
            or fixed["unit_input_authority_approval_sha256"]
            != approval["approval_sha256"]
            or any(
                fixed[name] != payload[name]
                for name in release_inputs_v4._PAYLOAD_FIELDS
                if name != "schema"
            )
        ):
            raise ValueError("historical v4 fixed inputs invalid")
    except (
        InvalidSignature,
        KeyError,
        TypeError,
        ValueError,
        release_inputs_v4.ProductionReleaseUnitInputsV4Error,
    ) as exc:
        raise UnitInputRotationError(
            "unit_input_rotation_predecessor_invalid"
        ) from exc
    return plan, approval, fixed


def _release_triplet(
    *,
    uid: int,
    gid: int,
    trusted_predecessor: Mapping[str, Any],
    root: Path | None = None,
) -> _ReleaseAuthorityTriplet:
    base = (
        package.STAGED_UNIT_INPUT_PLAN_PATH.parent
        if root is None
        else root
    )
    if root is None:
        _release_require_live_triplet_no_extended_metadata(
            uid=uid,
            gid=gid,
            require_complete=True,
        )
    plan_path = base / package.STAGED_UNIT_INPUT_PLAN_PATH.name
    approval_path = base / package.STAGED_UNIT_INPUT_APPROVAL_PATH.name
    fixed_path = base / package.FIXED_UNIT_INPUTS_PATH.name
    plan_raw = _read_exact(plan_path, uid=uid, gid=gid, mode=0o400)
    approval_raw = _read_exact(
        approval_path,
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    fixed_raw = _read_exact(
        fixed_path,
        uid=uid,
        gid=gid,
        mode=package.FIXED_UNIT_INPUTS_MODE,
    )
    plan_value = _decode(plan_raw)
    approval_value = _decode(approval_raw)
    fixed_value = _decode(fixed_raw, newline=True)
    if root is None:
        _release_require_live_triplet_no_extended_metadata(
            uid=uid,
            gid=gid,
            require_complete=True,
        )
    plan_schema = plan_value.get("schema")
    approval_schema = approval_value.get("schema")
    fixed_schema = fixed_value.get("schema")
    try:
        if plan_schema == package.UNIT_INPUT_PLAN_SCHEMA:
            if (
                approval_schema != package.UNIT_INPUT_APPROVAL_SCHEMA
                or fixed_schema != package.UNIT_INPUT_SCHEMA
            ):
                raise UnitInputRotationError(
                    "unit_input_rotation_predecessor_invalid"
                )
            plan, approval, fixed = _validate_v3_authority_triplet(
                plan_value,
                approval_value,
                fixed_raw,
            )
            authority_version = "v3"
            fixed_inputs_sha256 = _sha(fixed_raw)
        elif plan_schema == release_inputs_v4.PLAN_SCHEMA:
            if (
                approval_schema != release_inputs_v4.APPROVAL_SCHEMA
                or fixed_schema != release_inputs_v4.FIXED_INPUTS_SCHEMA
            ):
                raise UnitInputRotationError(
                    "unit_input_rotation_predecessor_invalid"
                )
            plan, approval, fixed = _release_historical_v4_triplet(
                plan_value=plan_value,
                approval_value=approval_value,
                fixed_value=fixed_value,
            )
            if fixed_raw != _canonical(fixed) + b"\n":
                raise UnitInputRotationError(
                    "unit_input_rotation_predecessor_invalid"
                )
            authority_version = "v4"
            fixed_inputs_sha256 = str(fixed["fixed_inputs_sha256"])
        else:
            raise UnitInputRotationError(
                "unit_input_rotation_predecessor_invalid"
            )
    except (
        PermissionError,
        KeyError,
        TypeError,
        ValueError,
        package.PackagingError,
    ) as exc:
        raise UnitInputRotationError(
            "unit_input_rotation_predecessor_invalid"
        ) from exc

    if (
        str(plan["release_revision"])
        != trusted_predecessor["release_revision"]
        or plan["plan_sha256"]
        != trusted_predecessor["authority_plan_sha256"]
        or approval["approval_sha256"]
        != trusted_predecessor["authority_approval_sha256"]
        or fixed_inputs_sha256 != trusted_predecessor["fixed_inputs_sha256"]
        or plan["owner_subject_sha256"]
        != trusted_predecessor["owner_subject_sha256"]
        or plan["owner_public_key_ed25519_hex"]
        != trusted_predecessor["owner_public_key_ed25519_hex"]
        or plan["owner_key_id"] != trusted_predecessor["owner_key_id"]
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_predecessor_trust_invalid"
        )
    return _ReleaseAuthorityTriplet(
        authority_version=authority_version,
        revision=str(plan["release_revision"]),
        plan=plan,
        approval=approval,
        fixed_inputs=fixed,
        plan_raw=plan_raw,
        approval_raw=approval_raw,
        fixed_inputs_raw=fixed_raw,
        fixed_inputs_sha256=fixed_inputs_sha256,
    )


def _release_candidate_envelope(
    unit_input_publication: Mapping[str, Any],
    release_update_publication: Mapping[str, Any],
    trusted_predecessor: Mapping[str, Any],
    *,
    expected_predecessor_trust_sha256: str,
) -> tuple[bytes, bytes, bytes, str, str]:
    trusted = _release_predecessor_trust(
        trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
    )
    try:
        unit = release_inputs_v4._self_hashed(
            unit_input_publication,
            fields=release_inputs_v4._PUBLICATION_FIELDS,
            digest_field="publication_sha256",
            code="release_unit_inputs_v4_publication_invalid",
        )
        update = release_update._self_hashed(
            release_update_publication,
            fields=release_update._PUBLICATION_FIELDS,
            digest_field="publication_sha256",
            code="release_update_publication_invalid",
        )
        if (
            unit.get("schema") != release_inputs_v4.PUBLICATION_SCHEMA
            or unit.get("action") != release_inputs_v4.PUBLICATION_ACTION
            or update.get("schema") != release_update.PUBLICATION_SCHEMA
            or update.get("action") != release_update.PUBLICATION_ACTION
            or _SHA256.fullmatch(
                str(unit.get("publication_sha256", ""))
            )
            is None
            or _SHA256.fullmatch(
                str(update.get("publication_sha256", ""))
            )
            is None
        ):
            raise ValueError("release publication envelope invalid")
        unit_raw = _canonical(unit)
        update_raw = _canonical(update)
        trusted_raw = _canonical(trusted)
    except (
        KeyError,
        TypeError,
        ValueError,
        release_inputs_v4.ProductionReleaseUnitInputsV4Error,
        release_update.ProductionReleaseUpdateContractError,
    ) as exc:
        raise UnitInputRotationError(
            "unit_input_rotation_publication_invalid"
        ) from exc
    return (
        unit_raw,
        update_raw,
        trusted_raw,
        str(unit["publication_sha256"]),
        str(update["publication_sha256"]),
    )


def _release_approval_freshness(
    publication: Mapping[str, Any],
    *,
    now_unix: int,
) -> None:
    approval = publication.get("approval")
    if not isinstance(approval, Mapping):
        raise UnitInputRotationError(
            "unit_input_rotation_publication_invalid"
        )
    issued = approval.get("issued_at_unix")
    expires = approval.get("expires_at_unix")
    if (
        type(now_unix) is not int
        or type(issued) is not int
        or type(expires) is not int
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_publication_invalid"
        )
    if not issued <= now_unix < expires:
        raise UnitInputRotationError(
            "unit_input_rotation_publication_expired"
        )


def _release_successor(
    unit_input_publication: Mapping[str, Any],
    release_update_publication: Mapping[str, Any],
    trusted_predecessor: Mapping[str, Any],
    *,
    expected_predecessor_trust_sha256: str,
    now_unix: int,
) -> _ReleaseSuccessor:
    trusted = _release_predecessor_trust(
        trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
    )
    _release_approval_freshness(
        unit_input_publication,
        now_unix=now_unix,
    )
    _release_approval_freshness(
        release_update_publication,
        now_unix=now_unix,
    )
    try:
        publication = release_inputs_v4.validate_publication(
            unit_input_publication,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=(
                expected_predecessor_trust_sha256
            ),
            now_unix=now_unix,
        )
        update_publication = release_update.validate_publication(
            release_update_publication,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=(
                expected_predecessor_trust_sha256
            ),
            now_unix=now_unix,
        )
        fixed = release_inputs_v4.derive_fixed_inputs(
            unit_input_publication=publication,
            release_update_publication=update_publication,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=(
                expected_predecessor_trust_sha256
            ),
            now_unix=now_unix,
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        release_inputs_v4.ProductionReleaseUnitInputsV4Error,
        release_update.ProductionReleaseUpdateContractError,
    ) as exc:
        raise UnitInputRotationError(
            "unit_input_rotation_publication_invalid"
        ) from exc
    plan = publication["plan"]
    approval = publication["approval"]
    return _ReleaseSuccessor(
        revision=str(publication["release_revision"]),
        publication=publication,
        release_update_publication=update_publication,
        trusted_predecessor=trusted,
        plan=plan,
        approval=approval,
        fixed_inputs=fixed,
        publication_raw=_canonical(publication),
        release_update_publication_raw=_canonical(update_publication),
        trusted_predecessor_raw=_canonical(trusted),
        plan_raw=_canonical(plan),
        approval_raw=_canonical(approval),
        fixed_inputs_raw=_canonical(fixed) + b"\n",
    )


def _release_predecessor_record(
    predecessor: _ReleaseAuthorityTriplet,
) -> Mapping[str, Any]:
    return {
        "authority_version": predecessor.authority_version,
        "revision": predecessor.revision,
        "plan_schema": predecessor.plan["schema"],
        "approval_schema": predecessor.approval["schema"],
        "fixed_inputs_schema": predecessor.fixed_inputs["schema"],
        "plan_sha256": predecessor.plan["plan_sha256"],
        "approval_sha256": predecessor.approval["approval_sha256"],
        "fixed_inputs_sha256": predecessor.fixed_inputs_sha256,
        "fixed_inputs_file_sha256": _sha(predecessor.fixed_inputs_raw),
    }


def _release_successor_record(
    successor: _ReleaseSuccessor,
) -> Mapping[str, Any]:
    return {
        "authority_version": "v4",
        "revision": successor.revision,
        "publication_schema": successor.publication["schema"],
        "release_update_publication_schema": (
            successor.release_update_publication["schema"]
        ),
        "plan_sha256": successor.plan["plan_sha256"],
        "approval_sha256": successor.approval["approval_sha256"],
        "publication_sha256": successor.publication["publication_sha256"],
        "release_update_publication_sha256": (
            successor.release_update_publication["publication_sha256"]
        ),
        "fixed_inputs_sha256": successor.fixed_inputs[
            "fixed_inputs_sha256"
        ],
        "fixed_inputs_file_sha256": _sha(successor.fixed_inputs_raw),
    }


def _validate_release_predecessor_record(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _RELEASE_PREDECESSOR_FIELDS:
        raise UnitInputRotationError(
            "unit_input_rotation_transaction_invalid"
        )
    version = value.get("authority_version")
    if version == "v3":
        expected_schemas = (
            package.UNIT_INPUT_PLAN_SCHEMA,
            package.UNIT_INPUT_APPROVAL_SCHEMA,
            package.UNIT_INPUT_SCHEMA,
        )
    elif version == "v4":
        expected_schemas = (
            release_inputs_v4.PLAN_SCHEMA,
            release_inputs_v4.APPROVAL_SCHEMA,
            release_inputs_v4.FIXED_INPUTS_SCHEMA,
        )
    else:
        raise UnitInputRotationError(
            "unit_input_rotation_transaction_invalid"
        )
    if (
        value.get("plan_schema") != expected_schemas[0]
        or value.get("approval_schema") != expected_schemas[1]
        or value.get("fixed_inputs_schema") != expected_schemas[2]
        or package.REVISION.fullmatch(str(value.get("revision", "")))
        is None
        or any(
            _SHA256.fullmatch(str(value.get(name, ""))) is None
            for name in (
                "plan_sha256",
                "approval_sha256",
                "fixed_inputs_sha256",
                "fixed_inputs_file_sha256",
            )
        )
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_transaction_invalid"
        )
    return dict(value)


def _validate_release_successor_record(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _RELEASE_SUCCESSOR_FIELDS:
        raise UnitInputRotationError(
            "unit_input_rotation_transaction_invalid"
        )
    if (
        value.get("authority_version") != "v4"
        or value.get("publication_schema")
        != release_inputs_v4.PUBLICATION_SCHEMA
        or value.get("release_update_publication_schema")
        != release_update.PUBLICATION_SCHEMA
        or package.REVISION.fullmatch(str(value.get("revision", "")))
        is None
        or any(
            _SHA256.fullmatch(str(value.get(name, ""))) is None
            for name in (
                "plan_sha256",
                "approval_sha256",
                "publication_sha256",
                "release_update_publication_sha256",
                "fixed_inputs_sha256",
                "fixed_inputs_file_sha256",
            )
        )
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_transaction_invalid"
        )
    return dict(value)


def _release_transaction_unsigned(
    predecessor: _ReleaseAuthorityTriplet,
    successor: _ReleaseSuccessor,
    *,
    authorization_checked_at_unix: int,
) -> Mapping[str, Any]:
    return {
        "schema": RELEASE_TRANSACTION_SCHEMA,
        "predecessor": _release_predecessor_record(predecessor),
        "predecessor_trust_sha256": successor.trusted_predecessor[
            "trust_sha256"
        ],
        "authorization_checked_at_unix": authorization_checked_at_unix,
        "successor": _release_successor_record(successor),
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }


def _release_transaction_value(
    predecessor: _ReleaseAuthorityTriplet,
    successor: _ReleaseSuccessor,
    *,
    authorization_checked_at_unix: int,
) -> Mapping[str, Any]:
    unsigned = _release_transaction_unsigned(
        predecessor,
        successor,
        authorization_checked_at_unix=authorization_checked_at_unix,
    )
    return {
        **unsigned,
        "transaction_sha256": _sha(_canonical(unsigned)),
    }


def _validate_release_transaction(value: Any) -> Mapping[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != _RELEASE_TRANSACTION_FIELDS
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_transaction_invalid"
        )
    predecessor = _validate_release_predecessor_record(
        value.get("predecessor")
    )
    successor = _validate_release_successor_record(value.get("successor"))
    unsigned = {
        name: item
        for name, item in value.items()
        if name != "transaction_sha256"
    }
    checked = value.get("authorization_checked_at_unix")
    if (
        value.get("schema") != RELEASE_TRANSACTION_SCHEMA
        or predecessor["revision"] == successor["revision"]
        or predecessor["revision"][:12] == successor["revision"][:12]
        or _SHA256.fullmatch(
            str(value.get("predecessor_trust_sha256", ""))
        )
        is None
        or type(checked) is not int
        or checked <= 0
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
        or _SHA256.fullmatch(
            str(value.get("transaction_sha256", ""))
        )
        is None
        or value.get("transaction_sha256") != _sha(_canonical(unsigned))
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_transaction_invalid"
        )
    return {
        **dict(value),
        "predecessor": predecessor,
        "successor": successor,
    }


def _release_audit_root(*, uid: int, gid: int) -> Path:
    _release_require_no_extended_metadata(
        cutover.EVIDENCE_ROOT,
        uid=uid,
        gid=gid,
    )
    root = cutover.EVIDENCE_ROOT / RELEASE_AUDIT_DIRECTORY_NAME
    if not os.path.lexists(root):
        _ensure_directory(root, uid=uid, gid=gid)
    else:
        _require_directory(root, uid=uid, gid=gid)
    _release_require_no_extended_metadata(root, uid=uid, gid=gid)
    return root


def _release_require_no_extended_metadata(
    path: Path,
    *,
    uid: int,
    gid: int,
) -> None:
    """Reject ACLs, capabilities, and all xattrs on production evidence."""

    if not _release_extended_metadata_required(uid=uid, gid=gid):
        return
    try:
        listxattr = getattr(os, "listxattr")
        attributes = listxattr(path, follow_symlinks=False)
    except (AttributeError, OSError) as exc:
        raise UnitInputRotationError(
            "unit_input_rotation_audit_invalid"
        ) from exc
    if attributes:
        raise UnitInputRotationError(
            "unit_input_rotation_audit_invalid"
        )


def _release_extended_metadata_required(*, uid: int, gid: int) -> bool:
    return uid == 0 and gid == 0 and sys.platform.startswith("linux")


def _release_require_live_triplet_no_extended_metadata(
    *,
    uid: int,
    gid: int,
    require_complete: bool,
) -> None:
    staged_root = package.STAGED_UNIT_INPUT_PLAN_PATH.parent
    _release_require_no_extended_metadata(
        staged_root,
        uid=uid,
        gid=gid,
    )
    for path in (
        package.STAGED_UNIT_INPUT_PLAN_PATH,
        package.STAGED_UNIT_INPUT_APPROVAL_PATH,
        package.FIXED_UNIT_INPUTS_PATH,
    ):
        if not os.path.lexists(path):
            if require_complete:
                raise UnitInputRotationError(
                    "unit_input_rotation_file_unavailable"
                )
            continue
        _release_require_no_extended_metadata(path, uid=uid, gid=gid)


def _release_inventory_entry(
    child: Path,
    *,
    expected_modes: Mapping[str, int],
    uid: int,
    gid: int,
) -> str:
    logical_name = child.name
    if logical_name not in expected_modes:
        matched: str | None = None
        for name in expected_modes:
            prefix = f".{name}.rotate."
            if not logical_name.startswith(prefix):
                continue
            suffix = logical_name[len(prefix) :]
            if _TEMPORARY_SUFFIX.fullmatch(suffix) is None:
                break
            matched = name
            break
        if matched is None:
            raise UnitInputRotationError(
                "unit_input_rotation_audit_invalid"
            )
        logical_name = matched
    try:
        item = child.lstat()
    except OSError as exc:
        raise UnitInputRotationError(
            "unit_input_rotation_audit_invalid"
        ) from exc
    if (
        child.resolve(strict=True) != child
        or stat.S_ISLNK(item.st_mode)
        or not stat.S_ISREG(item.st_mode)
        or item.st_uid != uid
        or item.st_gid != gid
        or stat.S_IMODE(item.st_mode) != expected_modes[logical_name]
        or not 0 < item.st_size <= MAX_FILE
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_audit_invalid"
        )
    _release_require_no_extended_metadata(child, uid=uid, gid=gid)
    return logical_name


def _release_inventory_names(
    path: Path,
    *,
    expected_modes: Mapping[str, int],
    uid: int,
    gid: int,
) -> set[str]:
    try:
        children = sorted(path.iterdir())
    except OSError as exc:
        raise UnitInputRotationError(
            "unit_input_rotation_audit_invalid"
        ) from exc
    names: set[str] = set()
    groups: dict[str, list[Path]] = {}
    for child in children:
        logical = _release_inventory_entry(
            child,
            expected_modes=expected_modes,
            uid=uid,
            gid=gid,
        )
        groups.setdefault(logical, []).append(child)
        names.add(logical)
    _release_validate_inventory_alias_groups(groups)
    return names


def _release_validate_inventory_alias_groups(
    groups: Mapping[str, list[Path]],
) -> None:
    for logical, paths in groups.items():
        if len(paths) == 1:
            if paths[0].lstat().st_nlink != 1:
                raise UnitInputRotationError(
                    "unit_input_rotation_audit_invalid"
                )
            continue
        exact = [path for path in paths if path.name == logical]
        temporary = [
            path
            for path in paths
            if path.name.startswith(f".{logical}.rotate.")
        ]
        if len(paths) != 2 or len(exact) != 1 or len(temporary) != 1:
            raise UnitInputRotationError(
                "unit_input_rotation_audit_invalid"
            )
        first = exact[0].lstat()
        second = temporary[0].lstat()
        if (
            first.st_nlink != 2
            or second.st_nlink != 2
            or (first.st_dev, first.st_ino) != (second.st_dev, second.st_ino)
        ):
            raise UnitInputRotationError(
                "unit_input_rotation_audit_invalid"
            )


def _release_inventory_observation(
    child: Path,
    *,
    expected_modes: Mapping[str, int],
    uid: int,
    gid: int,
) -> tuple[str, bool]:
    logical_name = child.name
    temporary = False
    if logical_name not in expected_modes:
        matched: str | None = None
        for name in expected_modes:
            prefix = f".{name}.rotate."
            if not logical_name.startswith(prefix):
                continue
            suffix = logical_name[len(prefix) :]
            if _TEMPORARY_SUFFIX.fullmatch(suffix) is None:
                break
            matched = name
            temporary = True
            break
        if matched is None:
            raise UnitInputRotationError(
                "unit_input_rotation_audit_invalid"
            )
        logical_name = matched
    try:
        item = child.lstat()
    except OSError as exc:
        raise UnitInputRotationError(
            "unit_input_rotation_audit_invalid"
        ) from exc
    mode = stat.S_IMODE(item.st_mode)
    pending = (
        temporary
        and mode == _PENDING_TEMPORARY_MODE
        and 0 <= item.st_size <= MAX_FILE
    )
    sealed = mode == expected_modes[logical_name] and (
        0 < item.st_size <= MAX_FILE
    )
    if (
        child.resolve(strict=True) != child
        or stat.S_ISLNK(item.st_mode)
        or not stat.S_ISREG(item.st_mode)
        or item.st_uid != uid
        or item.st_gid != gid
        or not (pending or sealed)
        or (pending and item.st_nlink != 1)
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_audit_invalid"
        )
    _release_require_no_extended_metadata(child, uid=uid, gid=gid)
    return logical_name, pending


def _release_prune_valid_pending_leaf(
    transaction_path: Path,
    *,
    file_modes: Mapping[str, int],
    predecessor_modes: Mapping[str, int],
    recover_pending_names: frozenset[str],
    uid: int,
    gid: int,
) -> None:
    predecessor_root = transaction_path / PREDECESSOR_DIRECTORY_NAME
    try:
        outer_children = sorted(transaction_path.iterdir())
    except OSError as exc:
        raise UnitInputRotationError(
            "unit_input_rotation_audit_invalid"
        ) from exc
    predecessor_present = os.path.lexists(predecessor_root)
    predecessor_children: list[Path] = []
    if predecessor_present:
        _require_directory(predecessor_root, uid=uid, gid=gid)
        _release_require_no_extended_metadata(
            predecessor_root,
            uid=uid,
            gid=gid,
        )
        try:
            predecessor_children = sorted(predecessor_root.iterdir())
        except OSError as exc:
            raise UnitInputRotationError(
                "unit_input_rotation_audit_invalid"
            ) from exc

    observed: set[str] = set()
    pending: list[tuple[str, Path, int]] = []
    groups: dict[str, list[Path]] = {}

    def observe(
        child: Path,
        *,
        location: str,
        expected_modes: Mapping[str, int],
    ) -> None:
        logical, is_pending = _release_inventory_observation(
            child,
            expected_modes=expected_modes,
            uid=uid,
            gid=gid,
        )
        key = f"{location}:{logical}"
        observed.add(key)
        groups.setdefault(key, []).append(child)
        if is_pending:
            pending.append((key, child, expected_modes[logical]))

    for child in outer_children:
        if child.name == PREDECESSOR_DIRECTORY_NAME:
            continue
        observe(child, location="outer", expected_modes=file_modes)
    for child in predecessor_children:
        observe(
            child,
            location="predecessor",
            expected_modes=predecessor_modes,
        )

    for key, paths in groups.items():
        pending_paths = [
            path
            for path in paths
            if any(path == candidate for _, candidate, _ in pending)
        ]
        if pending_paths:
            if len(paths) != 1:
                raise UnitInputRotationError(
                    "unit_input_rotation_audit_invalid"
                )
            continue
        logical = key.split(":", 1)[1]
        _release_validate_inventory_alias_groups({logical: paths})

    if not pending:
        return
    if len(pending) != 1:
        raise UnitInputRotationError(
            "unit_input_rotation_audit_invalid"
        )

    base = [
        f"outer:{TRANSACTION_FILE_NAME}",
        f"outer:{PUBLICATION_FILE_NAME}",
        f"outer:{RELEASE_UPDATE_PUBLICATION_FILE_NAME}",
        f"outer:{PREDECESSOR_TRUST_FILE_NAME}",
    ]
    predecessor = [
        f"predecessor:{name}" for name in predecessor_modes
    ]
    prepared = f"outer:{PREPARED_RECEIPT_FILE_NAME}"
    mutation = f"outer:{MUTATION_BEGIN_FILE_NAME}"
    activation = f"outer:{ACTIVATION_BEGIN_FILE_NAME}"
    aborted = f"outer:{ABORT_RECEIPT_FILE_NAME}"
    final = f"outer:{RECEIPT_FILE_NAME}"
    next_leaf: dict[frozenset[str], set[str]] = {}
    for index, name in enumerate(base):
        next_leaf[frozenset(base[:index])] = {name}
    complete_base = frozenset(base)
    for index, name in enumerate(predecessor):
        next_leaf[
            frozenset({*complete_base, *predecessor[:index]})
        ] = {name}
    complete_predecessor = frozenset({*complete_base, *predecessor})
    next_leaf[complete_predecessor] = {prepared}
    prepared_state = frozenset({*complete_predecessor, prepared})
    next_leaf[prepared_state] = {mutation}
    mutation_state = frozenset({*prepared_state, mutation})
    next_leaf[mutation_state] = {activation, aborted}
    activation_state = frozenset({*mutation_state, activation})
    next_leaf[activation_state] = {final}

    pending_key, pending_path, final_mode = pending[0]
    pending_name = pending_key.split(":", 1)[1]
    durable_state = frozenset(observed - {pending_key})
    if (
        pending_name not in recover_pending_names
        or pending_key not in next_leaf.get(durable_state, set())
        or (
            predecessor_present
            and not complete_base.issubset(durable_state)
        )
        or (
            not predecessor_present
            and pending_key.startswith("predecessor:")
        )
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_audit_invalid"
        )
    try:
        removed = _remove_pending_temporary(
            pending_path,
            uid=uid,
            gid=gid,
            final_mode=final_mode,
        )
    except UnitInputRotationError as exc:
        raise UnitInputRotationError(
            "unit_input_rotation_audit_invalid"
        ) from exc
    if not removed:
        raise UnitInputRotationError(
            "unit_input_rotation_audit_invalid"
        )


def _require_release_transaction_inventory(
    path: Path,
    *,
    uid: int,
    gid: int,
    recover_pending_names: frozenset[str] = frozenset(),
) -> None:
    """Validate every allowed crash-state inventory and nothing else."""

    _require_directory(path, uid=uid, gid=gid)
    _release_require_no_extended_metadata(path, uid=uid, gid=gid)
    file_modes = {
        TRANSACTION_FILE_NAME: 0o400,
        PUBLICATION_FILE_NAME: 0o400,
        RELEASE_UPDATE_PUBLICATION_FILE_NAME: 0o400,
        PREDECESSOR_TRUST_FILE_NAME: 0o400,
        PREPARED_RECEIPT_FILE_NAME: 0o400,
        MUTATION_BEGIN_FILE_NAME: 0o400,
        ACTIVATION_BEGIN_FILE_NAME: 0o400,
        ABORT_RECEIPT_FILE_NAME: 0o400,
        RECEIPT_FILE_NAME: 0o400,
    }
    predecessor_modes = {
        package.STAGED_UNIT_INPUT_PLAN_PATH.name: 0o400,
        package.STAGED_UNIT_INPUT_APPROVAL_PATH.name: 0o400,
        package.FIXED_UNIT_INPUTS_PATH.name: package.FIXED_UNIT_INPUTS_MODE,
    }
    _release_prune_valid_pending_leaf(
        path,
        file_modes=file_modes,
        predecessor_modes=predecessor_modes,
        recover_pending_names=recover_pending_names,
        uid=uid,
        gid=gid,
    )
    try:
        children = sorted(path.iterdir())
    except OSError as exc:
        raise UnitInputRotationError(
            "unit_input_rotation_audit_invalid"
        ) from exc
    predecessor_root = path / PREDECESSOR_DIRECTORY_NAME
    outer_files = [
        child
        for child in children
        if child.name != PREDECESSOR_DIRECTORY_NAME
    ]
    outer_names: set[str] = set()
    # Directories and regular files have different contracts, so inventory
    # the outer regular files explicitly and handle the sole directory below.
    outer_groups: dict[str, list[Path]] = {}
    for child in outer_files:
        logical = _release_inventory_entry(
            child,
            expected_modes=file_modes,
            uid=uid,
            gid=gid,
        )
        outer_groups.setdefault(logical, []).append(child)
        outer_names.add(logical)
    _release_validate_inventory_alias_groups(outer_groups)
    predecessor_present = os.path.lexists(predecessor_root)
    if predecessor_present:
        _require_directory(predecessor_root, uid=uid, gid=gid)
        _release_require_no_extended_metadata(
            predecessor_root,
            uid=uid,
            gid=gid,
        )
    base = [
        TRANSACTION_FILE_NAME,
        PUBLICATION_FILE_NAME,
        RELEASE_UPDATE_PUBLICATION_FILE_NAME,
        PREDECESSOR_TRUST_FILE_NAME,
    ]
    allowed_outer: list[set[str]] = [
        set(base[:index]) for index in range(len(base) + 1)
    ]
    predecessor_sequence = list(predecessor_modes)
    predecessor_names: set[str] = set()
    if predecessor_present:
        predecessor_names = _release_inventory_names(
            predecessor_root,
            expected_modes=predecessor_modes,
            uid=uid,
            gid=gid,
        )
        if predecessor_names not in [
            set(predecessor_sequence[:index])
            for index in range(len(predecessor_sequence) + 1)
        ]:
            raise UnitInputRotationError(
                "unit_input_rotation_audit_invalid"
            )
        allowed_outer = [set(base)]
    if outer_names not in allowed_outer:
        complete_predecessor = predecessor_names == set(
            predecessor_sequence
        )
        terminal_sets = [
            set(base),
            {*base, PREPARED_RECEIPT_FILE_NAME},
            {
                *base,
                PREPARED_RECEIPT_FILE_NAME,
                MUTATION_BEGIN_FILE_NAME,
            },
            {
                *base,
                PREPARED_RECEIPT_FILE_NAME,
                MUTATION_BEGIN_FILE_NAME,
                ACTIVATION_BEGIN_FILE_NAME,
            },
            {
                *base,
                PREPARED_RECEIPT_FILE_NAME,
                MUTATION_BEGIN_FILE_NAME,
                ABORT_RECEIPT_FILE_NAME,
            },
            {
                *base,
                PREPARED_RECEIPT_FILE_NAME,
                MUTATION_BEGIN_FILE_NAME,
                ACTIVATION_BEGIN_FILE_NAME,
                RECEIPT_FILE_NAME,
            },
        ]
        if not (
            predecessor_present
            and complete_predecessor
            and outer_names in terminal_sets
        ):
            raise UnitInputRotationError(
                "unit_input_rotation_audit_invalid"
            )


def _release_logical_file_present(path: Path, name: str) -> bool:
    if os.path.lexists(path / name):
        return True
    prefix = f".{name}.rotate."
    try:
        children = path.iterdir()
        return any(
            child.name.startswith(prefix)
            and _TEMPORARY_SUFFIX.fullmatch(
                child.name[len(prefix) :]
            )
            is not None
            for child in children
        )
    except OSError as exc:
        raise UnitInputRotationError(
            "unit_input_rotation_audit_invalid"
        ) from exc


def _release_transaction_directories(
    root: Path,
    *,
    uid: int,
    gid: int,
    recover_pending_names: frozenset[str] = frozenset(),
) -> list[Path]:
    _release_require_no_extended_metadata(root, uid=uid, gid=gid)
    directories = _transaction_directories(root, uid=uid, gid=gid)
    for directory in directories:
        _release_require_no_extended_metadata(
            directory,
            uid=uid,
            gid=gid,
        )
        _require_release_transaction_inventory(
            directory,
            uid=uid,
            gid=gid,
            recover_pending_names=recover_pending_names,
        )
    return directories


def _load_release_transaction(
    path: Path,
    *,
    uid: int,
    gid: int,
    recover_pending_names: frozenset[str] = frozenset(),
) -> Mapping[str, Any] | None:
    _require_release_transaction_inventory(
        path,
        uid=uid,
        gid=gid,
        recover_pending_names=recover_pending_names,
    )
    transaction_path = path / TRANSACTION_FILE_NAME
    if not os.path.lexists(transaction_path):
        return None
    transaction = _validate_release_transaction(
        _decode(
            _read_exact(
                transaction_path,
                uid=uid,
                gid=gid,
                mode=0o400,
            )
        )
    )
    if path.name != (
        f"{transaction['predecessor']['plan_sha256']}-"
        f"{transaction['successor']['publication_sha256']}"
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_transaction_invalid"
        )
    return transaction


def _release_require_same_successor(
    transaction: Mapping[str, Any],
    successor: _ReleaseSuccessor,
) -> None:
    if (
        transaction["successor"] != _release_successor_record(successor)
        or transaction["predecessor_trust_sha256"]
        != successor.trusted_predecessor["trust_sha256"]
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_successor_conflict"
        )


def _release_validate_archived_predecessor(
    transaction_path: Path,
    transaction: Mapping[str, Any],
    successor: _ReleaseSuccessor,
    *,
    uid: int,
    gid: int,
) -> _ReleaseAuthorityTriplet:
    predecessor_root = transaction_path / PREDECESSOR_DIRECTORY_NAME
    _require_directory(predecessor_root, uid=uid, gid=gid)
    predecessor = _release_triplet(
        uid=uid,
        gid=gid,
        trusted_predecessor=successor.trusted_predecessor,
        root=predecessor_root,
    )
    names = _release_inventory_names(
        predecessor_root,
        expected_modes={
            package.STAGED_UNIT_INPUT_PLAN_PATH.name: 0o400,
            package.STAGED_UNIT_INPUT_APPROVAL_PATH.name: 0o400,
            package.FIXED_UNIT_INPUTS_PATH.name: (
                package.FIXED_UNIT_INPUTS_MODE
            ),
        },
        uid=uid,
        gid=gid,
    )
    if (
        names
        != {
            package.STAGED_UNIT_INPUT_PLAN_PATH.name,
            package.STAGED_UNIT_INPUT_APPROVAL_PATH.name,
            package.FIXED_UNIT_INPUTS_PATH.name,
        }
        or _release_predecessor_record(predecessor)
        != transaction["predecessor"]
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_audit_invalid"
        )
    return predecessor


def _persisted_release_successor(
    transaction_path: Path,
    transaction: Mapping[str, Any],
    *,
    uid: int,
    gid: int,
) -> _ReleaseSuccessor:
    unit_raw = _read_exact(
        transaction_path / PUBLICATION_FILE_NAME,
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    update_raw = _read_exact(
        transaction_path / RELEASE_UPDATE_PUBLICATION_FILE_NAME,
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    trust_raw = _read_exact(
        transaction_path / PREDECESSOR_TRUST_FILE_NAME,
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    successor = _release_successor(
        _decode(unit_raw),
        _decode(update_raw),
        _decode(trust_raw),
        expected_predecessor_trust_sha256=transaction[
            "predecessor_trust_sha256"
        ],
        now_unix=transaction["authorization_checked_at_unix"],
    )
    _release_require_same_successor(transaction, successor)
    if (
        unit_raw != successor.publication_raw
        or update_raw != successor.release_update_publication_raw
        or trust_raw != successor.trusted_predecessor_raw
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_transaction_invalid"
        )
    return successor


def _release_successor_from_transaction(
    transaction_path: Path,
    transaction: Mapping[str, Any],
    incoming_raw: tuple[bytes, bytes, bytes],
    *,
    uid: int,
    gid: int,
) -> _ReleaseSuccessor:
    successor = _persisted_release_successor(
        transaction_path,
        transaction,
        uid=uid,
        gid=gid,
    )
    if incoming_raw != (
        successor.publication_raw,
        successor.release_update_publication_raw,
        successor.trusted_predecessor_raw,
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_successor_conflict"
        )
    return successor


def _prepare_release_transaction(
    *,
    transaction_path: Path,
    predecessor: _ReleaseAuthorityTriplet,
    successor: _ReleaseSuccessor,
    authorization_checked_at_unix: int,
    uid: int,
    gid: int,
) -> Mapping[str, Any]:
    transaction = _release_transaction_value(
        predecessor,
        successor,
        authorization_checked_at_unix=authorization_checked_at_unix,
    )
    _ensure_directory(transaction_path, uid=uid, gid=gid)
    _require_release_transaction_inventory(
        transaction_path,
        uid=uid,
        gid=gid,
    )
    _install_exact(
        transaction_path / TRANSACTION_FILE_NAME,
        _canonical(transaction),
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    _checkpoint("v4_transaction_authorized")
    _install_exact(
        transaction_path / PUBLICATION_FILE_NAME,
        successor.publication_raw,
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    _checkpoint("v4_successor_publication_archived")
    _install_exact(
        transaction_path / RELEASE_UPDATE_PUBLICATION_FILE_NAME,
        successor.release_update_publication_raw,
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    _checkpoint("v4_release_update_publication_archived")
    _install_exact(
        transaction_path / PREDECESSOR_TRUST_FILE_NAME,
        successor.trusted_predecessor_raw,
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    _checkpoint("v4_predecessor_trust_archived")
    predecessor_root = transaction_path / PREDECESSOR_DIRECTORY_NAME
    _ensure_directory(predecessor_root, uid=uid, gid=gid)
    for checkpoint, path, raw, mode in (
        (
            "v4_predecessor_plan_archived",
            predecessor_root / package.STAGED_UNIT_INPUT_PLAN_PATH.name,
            predecessor.plan_raw,
            0o400,
        ),
        (
            "v4_predecessor_approval_archived",
            predecessor_root / package.STAGED_UNIT_INPUT_APPROVAL_PATH.name,
            predecessor.approval_raw,
            0o400,
        ),
        (
            "v4_predecessor_fixed_inputs_archived",
            predecessor_root / package.FIXED_UNIT_INPUTS_PATH.name,
            predecessor.fixed_inputs_raw,
            package.FIXED_UNIT_INPUTS_MODE,
        ),
    ):
        _install_exact(path, raw, uid=uid, gid=gid, mode=mode)
        _checkpoint(checkpoint)
    archived = _release_validate_archived_predecessor(
        transaction_path,
        transaction,
        successor,
        uid=uid,
        gid=gid,
    )
    if archived != predecessor:
        raise UnitInputRotationError("unit_input_rotation_audit_invalid")
    _require_release_transaction_inventory(
        transaction_path,
        uid=uid,
        gid=gid,
    )
    return transaction


def _release_receipt_transaction(
    value: Mapping[str, Any],
) -> Mapping[str, Any]:
    return _validate_release_transaction(
        {
            "schema": RELEASE_TRANSACTION_SCHEMA,
            "predecessor": value.get("predecessor"),
            "predecessor_trust_sha256": value.get(
                "predecessor_trust_sha256"
            ),
            "authorization_checked_at_unix": value.get(
                "authorization_checked_at_unix"
            ),
            "successor": value.get("successor"),
            "secret_material_recorded": False,
            "secret_digest_recorded": False,
            "transaction_sha256": value.get("transaction_sha256"),
        }
    )


def _release_prepared_receipt(
    transaction_path: Path,
    transaction: Mapping[str, Any],
) -> Mapping[str, Any]:
    unsigned = {
        "schema": RELEASE_PREPARED_RECEIPT_SCHEMA,
        "predecessor": transaction["predecessor"],
        "predecessor_trust_sha256": transaction[
            "predecessor_trust_sha256"
        ],
        "authorization_checked_at_unix": transaction[
            "authorization_checked_at_unix"
        ],
        "transaction_sha256": transaction["transaction_sha256"],
        "successor": transaction["successor"],
        "audit_transaction_path": str(transaction_path),
        "live_plan_path": str(package.STAGED_UNIT_INPUT_PLAN_PATH),
        "live_approval_path": str(package.STAGED_UNIT_INPUT_APPROVAL_PATH),
        "live_fixed_inputs_path": str(package.FIXED_UNIT_INPUTS_PATH),
        "live_triplet_unchanged": True,
        "mutation_performed": False,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {**unsigned, "receipt_sha256": _sha(_canonical(unsigned))}


def _release_predecessor_record_matches_trust(
    predecessor: Mapping[str, Any],
    trusted_predecessor: Mapping[str, Any],
) -> bool:
    return (
        predecessor["revision"] == trusted_predecessor["release_revision"]
        and predecessor["plan_sha256"]
        == trusted_predecessor["authority_plan_sha256"]
        and predecessor["approval_sha256"]
        == trusted_predecessor["authority_approval_sha256"]
        and predecessor["fixed_inputs_sha256"]
        == trusted_predecessor["fixed_inputs_sha256"]
    )


def _release_finalized_receipt(
    transaction_path: Path,
    transaction: Mapping[str, Any],
    prepared_receipt: Mapping[str, Any],
    mutation_begin: Mapping[str, Any],
    activation_begin: Mapping[str, Any],
) -> Mapping[str, Any]:
    unsigned = {
        "schema": RELEASE_FINALIZED_RECEIPT_SCHEMA,
        "predecessor": transaction["predecessor"],
        "predecessor_trust_sha256": transaction[
            "predecessor_trust_sha256"
        ],
        "authorization_checked_at_unix": transaction[
            "authorization_checked_at_unix"
        ],
        "transaction_sha256": transaction["transaction_sha256"],
        "successor": transaction["successor"],
        "audit_transaction_path": str(transaction_path),
        "staged_plan_path": str(package.STAGED_UNIT_INPUT_PLAN_PATH),
        "staged_approval_path": str(
            package.STAGED_UNIT_INPUT_APPROVAL_PATH
        ),
        "fixed_inputs_path": str(package.FIXED_UNIT_INPUTS_PATH),
        "successor_triplet_complete": True,
        "mutation_begin_sha256": mutation_begin[
            "mutation_begin_sha256"
        ],
        "activation_begin_sha256": activation_begin[
            "activation_begin_sha256"
        ],
        "prepared_receipt_sha256": prepared_receipt["receipt_sha256"],
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {**unsigned, "receipt_sha256": _sha(_canonical(unsigned))}


def validate_release_prepared_rotation_receipt(
    value: Any,
    *,
    unit_input_publication: Mapping[str, Any],
    release_update_publication: Mapping[str, Any],
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
) -> Mapping[str, Any]:
    """Validate one Stage C prepared receipt against exact v4 authorities."""

    if (
        not isinstance(value, Mapping)
        or set(value) != _RELEASE_PREPARED_RECEIPT_FIELDS
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_prepared_receipt_invalid"
        )
    try:
        transaction = _release_receipt_transaction(value)
        successor = _release_successor(
            unit_input_publication,
            release_update_publication,
            trusted_predecessor,
            expected_predecessor_trust_sha256=(
                expected_predecessor_trust_sha256
            ),
            now_unix=transaction["authorization_checked_at_unix"],
        )
    except UnitInputRotationError as exc:
        raise UnitInputRotationError(
            "unit_input_rotation_prepared_receipt_invalid"
        ) from exc
    predecessor = transaction["predecessor"]
    expected_audit = (
        cutover.EVIDENCE_ROOT
        / RELEASE_AUDIT_DIRECTORY_NAME
        / (
            f"{predecessor['plan_sha256']}-"
            f"{successor.publication['publication_sha256']}"
        )
    )
    unsigned = {
        name: item
        for name, item in value.items()
        if name != "receipt_sha256"
    }
    if (
        value.get("schema") != RELEASE_PREPARED_RECEIPT_SCHEMA
        or transaction["successor"] != _release_successor_record(successor)
        or transaction["predecessor_trust_sha256"]
        != successor.trusted_predecessor["trust_sha256"]
        or not _release_predecessor_record_matches_trust(
            predecessor,
            successor.trusted_predecessor,
        )
        or value.get("audit_transaction_path") != str(expected_audit)
        or value.get("live_plan_path")
        != str(package.STAGED_UNIT_INPUT_PLAN_PATH)
        or value.get("live_approval_path")
        != str(package.STAGED_UNIT_INPUT_APPROVAL_PATH)
        or value.get("live_fixed_inputs_path")
        != str(package.FIXED_UNIT_INPUTS_PATH)
        or value.get("live_triplet_unchanged") is not True
        or value.get("mutation_performed") is not False
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
        or _SHA256.fullmatch(str(value.get("receipt_sha256", "")))
        is None
        or value.get("receipt_sha256") != _sha(_canonical(unsigned))
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_prepared_receipt_invalid"
        )
    return dict(value)


def validate_release_rotation_receipt(
    value: Any,
    *,
    unit_input_publication: Mapping[str, Any],
    release_update_publication: Mapping[str, Any],
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
    prepared_receipt: Mapping[str, Any],
    mutation_begin: Mapping[str, Any],
    activation_begin: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Validate one completed Stage C v4 unit-input rotation receipt."""

    if (
        not isinstance(value, Mapping)
        or set(value) != _RELEASE_FINALIZED_RECEIPT_FIELDS
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_receipt_invalid"
        )
    try:
        transaction = _release_receipt_transaction(value)
        successor = _release_successor(
            unit_input_publication,
            release_update_publication,
            trusted_predecessor,
            expected_predecessor_trust_sha256=(
                expected_predecessor_trust_sha256
            ),
            now_unix=transaction["authorization_checked_at_unix"],
        )
    except UnitInputRotationError as exc:
        raise UnitInputRotationError(
            "unit_input_rotation_receipt_invalid"
        ) from exc
    predecessor = transaction["predecessor"]
    expected_audit = (
        cutover.EVIDENCE_ROOT
        / RELEASE_AUDIT_DIRECTORY_NAME
        / (
            f"{predecessor['plan_sha256']}-"
            f"{successor.publication['publication_sha256']}"
        )
    )
    try:
        validated_prepared = validate_release_prepared_rotation_receipt(
            prepared_receipt,
            unit_input_publication=unit_input_publication,
            release_update_publication=release_update_publication,
            trusted_predecessor=trusted_predecessor,
            expected_predecessor_trust_sha256=(
                expected_predecessor_trust_sha256
            ),
        )
        validated_mutation = _validate_release_mutation_begin(
            mutation_begin,
            transaction=transaction,
            successor=successor,
        )
        validated_activation = _validate_release_activation_begin(
            activation_begin,
            transaction=transaction,
            successor=successor,
            preauthorization_receipt=validated_mutation,
        )
    except UnitInputRotationError as exc:
        raise UnitInputRotationError(
            "unit_input_rotation_receipt_invalid"
        ) from exc
    expected_prepared = _release_prepared_receipt(
        expected_audit,
        transaction,
    )
    expected_mutation = _release_mutation_begin_value(
        transaction,
        successor,
        freshness_checked_at_unix=validated_mutation[
            "freshness_checked_at_unix"
        ],
    )
    expected_activation = _release_activation_begin_value(
        transaction,
        successor,
        validated_mutation,
    )
    unsigned = {
        name: item
        for name, item in value.items()
        if name != "receipt_sha256"
    }
    if (
        value.get("schema") != RELEASE_FINALIZED_RECEIPT_SCHEMA
        or transaction["successor"] != _release_successor_record(successor)
        or transaction["predecessor_trust_sha256"]
        != successor.trusted_predecessor["trust_sha256"]
        or not _release_predecessor_record_matches_trust(
            predecessor,
            successor.trusted_predecessor,
        )
        or value.get("audit_transaction_path") != str(expected_audit)
        or value.get("staged_plan_path")
        != str(package.STAGED_UNIT_INPUT_PLAN_PATH)
        or value.get("staged_approval_path")
        != str(package.STAGED_UNIT_INPUT_APPROVAL_PATH)
        or value.get("fixed_inputs_path")
        != str(package.FIXED_UNIT_INPUTS_PATH)
        or value.get("successor_triplet_complete") is not True
        or validated_prepared != expected_prepared
        or validated_mutation != expected_mutation
        or validated_activation != expected_activation
        or value.get("mutation_begin_sha256")
        != validated_mutation["mutation_begin_sha256"]
        or value.get("activation_begin_sha256")
        != validated_activation["activation_begin_sha256"]
        or value.get("prepared_receipt_sha256")
        != validated_prepared["receipt_sha256"]
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
        or _SHA256.fullmatch(str(value.get("receipt_sha256", "")))
        is None
        or value.get("receipt_sha256") != _sha(_canonical(unsigned))
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_receipt_invalid"
        )
    return dict(value)


def _load_release_prepared_receipt(
    transaction_path: Path,
    *,
    transaction: Mapping[str, Any],
    successor: _ReleaseSuccessor,
    uid: int,
    gid: int,
) -> Mapping[str, Any] | None:
    path = transaction_path / PREPARED_RECEIPT_FILE_NAME
    if not os.path.lexists(path):
        return None
    receipt = validate_release_prepared_rotation_receipt(
        _decode(_read_exact(path, uid=uid, gid=gid, mode=0o400)),
        unit_input_publication=successor.publication,
        release_update_publication=successor.release_update_publication,
        trusted_predecessor=successor.trusted_predecessor,
        expected_predecessor_trust_sha256=transaction[
            "predecessor_trust_sha256"
        ],
    )
    if receipt != _release_prepared_receipt(
        transaction_path,
        transaction,
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_prepared_receipt_invalid"
        )
    return receipt


def _release_mutation_begin_value(
    transaction: Mapping[str, Any],
    successor: _ReleaseSuccessor,
    *,
    freshness_checked_at_unix: int,
) -> Mapping[str, Any]:
    unsigned = {
        "schema": RELEASE_MUTATION_BEGIN_SCHEMA,
        "transaction_sha256": transaction["transaction_sha256"],
        "successor_publication_sha256": successor.publication[
            "publication_sha256"
        ],
        "release_update_publication_sha256": (
            successor.release_update_publication["publication_sha256"]
        ),
        "successor_fixed_inputs_sha256": successor.fixed_inputs[
            "fixed_inputs_sha256"
        ],
        "freshness_checked_at_unix": freshness_checked_at_unix,
        "live_mutation_write_ahead_committed": True,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {
        **unsigned,
        "mutation_begin_sha256": _sha(_canonical(unsigned)),
    }


def _validate_release_mutation_begin(
    value: Any,
    *,
    transaction: Mapping[str, Any],
    successor: _ReleaseSuccessor,
) -> Mapping[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != _RELEASE_MUTATION_BEGIN_FIELDS
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_mutation_begin_invalid"
        )
    checked = value.get("freshness_checked_at_unix")
    unsigned = {
        name: item
        for name, item in value.items()
        if name != "mutation_begin_sha256"
    }
    try:
        _release_approval_freshness(
            successor.publication,
            now_unix=checked,
        )
        _release_approval_freshness(
            successor.release_update_publication,
            now_unix=checked,
        )
    except UnitInputRotationError as exc:
        raise UnitInputRotationError(
            "unit_input_rotation_mutation_begin_invalid"
        ) from exc
    if (
        value.get("schema") != RELEASE_MUTATION_BEGIN_SCHEMA
        or value.get("transaction_sha256")
        != transaction["transaction_sha256"]
        or value.get("successor_publication_sha256")
        != successor.publication["publication_sha256"]
        or value.get("release_update_publication_sha256")
        != successor.release_update_publication["publication_sha256"]
        or value.get("successor_fixed_inputs_sha256")
        != successor.fixed_inputs["fixed_inputs_sha256"]
        or type(checked) is not int
        or checked < transaction["authorization_checked_at_unix"]
        or value.get("live_mutation_write_ahead_committed") is not True
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
        or _SHA256.fullmatch(
            str(value.get("mutation_begin_sha256", ""))
        )
        is None
        or value.get("mutation_begin_sha256") != _sha(_canonical(unsigned))
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_mutation_begin_invalid"
        )
    return dict(value)


def _load_release_mutation_begin(
    transaction_path: Path,
    *,
    transaction: Mapping[str, Any],
    successor: _ReleaseSuccessor,
    uid: int,
    gid: int,
) -> Mapping[str, Any] | None:
    path = transaction_path / MUTATION_BEGIN_FILE_NAME
    if not os.path.lexists(path):
        return None
    value = _validate_release_mutation_begin(
        _decode(_read_exact(path, uid=uid, gid=gid, mode=0o400)),
        transaction=transaction,
        successor=successor,
    )
    expected = _release_mutation_begin_value(
        transaction,
        successor,
        freshness_checked_at_unix=value["freshness_checked_at_unix"],
    )
    if value != expected:
        raise UnitInputRotationError(
            "unit_input_rotation_mutation_begin_invalid"
        )
    return value


def validate_release_preauthorization_receipt(
    value: Any,
    *,
    unit_input_publication: Mapping[str, Any],
    release_update_publication: Mapping[str, Any],
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
    prepared_receipt: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Validate one exact durable release-rotation preauthorization."""

    try:
        prepared = validate_release_prepared_rotation_receipt(
            prepared_receipt,
            unit_input_publication=unit_input_publication,
            release_update_publication=release_update_publication,
            trusted_predecessor=trusted_predecessor,
            expected_predecessor_trust_sha256=(
                expected_predecessor_trust_sha256
            ),
        )
        transaction = _release_receipt_transaction(prepared)
        successor = _release_successor(
            unit_input_publication,
            release_update_publication,
            trusted_predecessor,
            expected_predecessor_trust_sha256=(
                expected_predecessor_trust_sha256
            ),
            now_unix=transaction["authorization_checked_at_unix"],
        )
        preauthorization = _validate_release_mutation_begin(
            value,
            transaction=transaction,
            successor=successor,
        )
    except UnitInputRotationError as exc:
        raise UnitInputRotationError(
            "unit_input_rotation_preauthorization_invalid"
        ) from exc
    expected = _release_mutation_begin_value(
        transaction,
        successor,
        freshness_checked_at_unix=preauthorization[
            "freshness_checked_at_unix"
        ],
    )
    if preauthorization != expected:
        raise UnitInputRotationError(
            "unit_input_rotation_preauthorization_invalid"
        )
    return preauthorization


def _release_activation_begin_value(
    transaction: Mapping[str, Any],
    successor: _ReleaseSuccessor,
    preauthorization_receipt: Mapping[str, Any],
) -> Mapping[str, Any]:
    unsigned = {
        "schema": RELEASE_ACTIVATION_BEGIN_SCHEMA,
        "transaction_sha256": transaction["transaction_sha256"],
        "mutation_begin_sha256": preauthorization_receipt[
            "mutation_begin_sha256"
        ],
        "successor_publication_sha256": successor.publication[
            "publication_sha256"
        ],
        "release_update_publication_sha256": (
            successor.release_update_publication["publication_sha256"]
        ),
        "successor_fixed_inputs_sha256": successor.fixed_inputs[
            "fixed_inputs_sha256"
        ],
        "live_activation_write_ahead_committed": True,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {
        **unsigned,
        "activation_begin_sha256": _sha(_canonical(unsigned)),
    }


def _validate_release_activation_begin(
    value: Any,
    *,
    transaction: Mapping[str, Any],
    successor: _ReleaseSuccessor,
    preauthorization_receipt: Mapping[str, Any],
) -> Mapping[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != _RELEASE_ACTIVATION_BEGIN_FIELDS
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_activation_begin_invalid"
        )
    expected = _release_activation_begin_value(
        transaction,
        successor,
        preauthorization_receipt,
    )
    if value != expected:
        raise UnitInputRotationError(
            "unit_input_rotation_activation_begin_invalid"
        )
    return dict(value)


def _load_release_activation_begin(
    transaction_path: Path,
    *,
    transaction: Mapping[str, Any],
    successor: _ReleaseSuccessor,
    preauthorization_receipt: Mapping[str, Any],
    uid: int,
    gid: int,
) -> Mapping[str, Any] | None:
    path = transaction_path / ACTIVATION_BEGIN_FILE_NAME
    if not os.path.lexists(path):
        return None
    value = _validate_release_activation_begin(
        _decode(_read_exact(path, uid=uid, gid=gid, mode=0o400)),
        transaction=transaction,
        successor=successor,
        preauthorization_receipt=preauthorization_receipt,
    )
    expected = _release_activation_begin_value(
        transaction,
        successor,
        preauthorization_receipt,
    )
    if value != expected:
        raise UnitInputRotationError(
            "unit_input_rotation_activation_begin_invalid"
        )
    return value


def _release_aborted_receipt(
    transaction_path: Path,
    transaction: Mapping[str, Any],
    prepared_receipt: Mapping[str, Any],
    preauthorization_receipt: Mapping[str, Any],
    successor: _ReleaseSuccessor,
) -> Mapping[str, Any]:
    unsigned = {
        "schema": RELEASE_ABORTED_RECEIPT_SCHEMA,
        "transaction_sha256": transaction["transaction_sha256"],
        "successor_publication_sha256": successor.publication[
            "publication_sha256"
        ],
        "release_update_publication_sha256": (
            successor.release_update_publication["publication_sha256"]
        ),
        "successor_fixed_inputs_sha256": successor.fixed_inputs[
            "fixed_inputs_sha256"
        ],
        "audit_transaction_path": str(transaction_path),
        "prepared_receipt_sha256": prepared_receipt["receipt_sha256"],
        "mutation_begin_sha256": preauthorization_receipt[
            "mutation_begin_sha256"
        ],
        "live_predecessor_unchanged": True,
        "live_mutation_performed": False,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {**unsigned, "receipt_sha256": _sha(_canonical(unsigned))}


def validate_release_rotation_abort_receipt(
    value: Any,
    *,
    unit_input_publication: Mapping[str, Any],
    release_update_publication: Mapping[str, Any],
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
    prepared_receipt: Mapping[str, Any],
    preauthorization_receipt: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Validate one append-only terminal release-rotation abort."""

    if (
        not isinstance(value, Mapping)
        or set(value) != _RELEASE_ABORTED_RECEIPT_FIELDS
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_abort_receipt_invalid"
        )
    try:
        prepared = validate_release_prepared_rotation_receipt(
            prepared_receipt,
            unit_input_publication=unit_input_publication,
            release_update_publication=release_update_publication,
            trusted_predecessor=trusted_predecessor,
            expected_predecessor_trust_sha256=(
                expected_predecessor_trust_sha256
            ),
        )
        transaction = _release_receipt_transaction(prepared)
        successor = _release_successor(
            unit_input_publication,
            release_update_publication,
            trusted_predecessor,
            expected_predecessor_trust_sha256=(
                expected_predecessor_trust_sha256
            ),
            now_unix=transaction["authorization_checked_at_unix"],
        )
        preauthorization = validate_release_preauthorization_receipt(
            preauthorization_receipt,
            unit_input_publication=unit_input_publication,
            release_update_publication=release_update_publication,
            trusted_predecessor=trusted_predecessor,
            expected_predecessor_trust_sha256=(
                expected_predecessor_trust_sha256
            ),
            prepared_receipt=prepared,
        )
    except UnitInputRotationError as exc:
        raise UnitInputRotationError(
            "unit_input_rotation_abort_receipt_invalid"
        ) from exc
    transaction_path = Path(prepared["audit_transaction_path"])
    expected = _release_aborted_receipt(
        transaction_path,
        transaction,
        prepared,
        preauthorization,
        successor,
    )
    if value != expected:
        raise UnitInputRotationError(
            "unit_input_rotation_abort_receipt_invalid"
        )
    return dict(value)


def _load_release_abort_receipt(
    transaction_path: Path,
    *,
    transaction: Mapping[str, Any],
    successor: _ReleaseSuccessor,
    prepared_receipt: Mapping[str, Any],
    preauthorization_receipt: Mapping[str, Any],
    uid: int,
    gid: int,
) -> Mapping[str, Any] | None:
    path = transaction_path / ABORT_RECEIPT_FILE_NAME
    if not os.path.lexists(path):
        return None
    receipt = validate_release_rotation_abort_receipt(
        _decode(_read_exact(path, uid=uid, gid=gid, mode=0o400)),
        unit_input_publication=successor.publication,
        release_update_publication=successor.release_update_publication,
        trusted_predecessor=successor.trusted_predecessor,
        expected_predecessor_trust_sha256=transaction[
            "predecessor_trust_sha256"
        ],
        prepared_receipt=prepared_receipt,
        preauthorization_receipt=preauthorization_receipt,
    )
    expected = _release_aborted_receipt(
        transaction_path,
        transaction,
        prepared_receipt,
        preauthorization_receipt,
        successor,
    )
    if receipt != expected:
        raise UnitInputRotationError(
            "unit_input_rotation_abort_receipt_invalid"
        )
    return receipt


def _publish_release_mutation_begin(
    transaction_path: Path,
    transaction: Mapping[str, Any],
    predecessor: _ReleaseAuthorityTriplet,
    successor: _ReleaseSuccessor,
    *,
    clock: Callable[[], int],
    uid: int,
    gid: int,
) -> Mapping[str, Any]:
    _release_require_live_triplet_no_extended_metadata(
        uid=uid,
        gid=gid,
        require_complete=True,
    )
    _validate_live_predecessor(predecessor, uid=uid, gid=gid)
    checked = clock()
    if type(checked) is not int or checked <= 0:
        raise UnitInputRotationError("unit_input_rotation_clock_invalid")
    if checked < transaction["authorization_checked_at_unix"]:
        raise UnitInputRotationError("unit_input_rotation_clock_invalid")
    fresh = _release_successor(
        successor.publication,
        successor.release_update_publication,
        successor.trusted_predecessor,
        expected_predecessor_trust_sha256=transaction[
            "predecessor_trust_sha256"
        ],
        now_unix=checked,
    )
    if (
        _release_successor_record(fresh) != transaction["successor"]
        or (
            fresh.publication_raw,
            fresh.release_update_publication_raw,
            fresh.trusted_predecessor_raw,
        )
        != (
            successor.publication_raw,
            successor.release_update_publication_raw,
            successor.trusted_predecessor_raw,
        )
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_finalize_authorization_invalid"
        )
    value = _release_mutation_begin_value(
        transaction,
        successor,
        freshness_checked_at_unix=checked,
    )
    if (
        _validate_release_mutation_begin(
            value,
            transaction=transaction,
            successor=successor,
        )
        != value
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_mutation_begin_invalid"
        )
    _install_exact(
        transaction_path / MUTATION_BEGIN_FILE_NAME,
        _canonical(value),
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    _checkpoint("v4_live_mutation_begun")
    _release_require_live_triplet_no_extended_metadata(
        uid=uid,
        gid=gid,
        require_complete=True,
    )
    _validate_live_predecessor(predecessor, uid=uid, gid=gid)
    _require_release_transaction_inventory(
        transaction_path,
        uid=uid,
        gid=gid,
    )
    persisted = _load_release_mutation_begin(
        transaction_path,
        transaction=transaction,
        successor=successor,
        uid=uid,
        gid=gid,
    )
    if persisted is None:
        raise UnitInputRotationError(
            "unit_input_rotation_mutation_begin_invalid"
        )
    return persisted


def _publish_release_abort_receipt(
    transaction_path: Path,
    transaction: Mapping[str, Any],
    predecessor: _ReleaseAuthorityTriplet,
    successor: _ReleaseSuccessor,
    prepared_receipt: Mapping[str, Any],
    preauthorization_receipt: Mapping[str, Any],
    *,
    uid: int,
    gid: int,
) -> Mapping[str, Any]:
    if (
        _release_logical_file_present(
            transaction_path,
            ACTIVATION_BEGIN_FILE_NAME,
        )
        or _release_logical_file_present(
            transaction_path,
            RECEIPT_FILE_NAME,
        )
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_abort_authorization_invalid"
        )
    _release_require_live_triplet_no_extended_metadata(
        uid=uid,
        gid=gid,
        require_complete=False,
    )
    if _release_live_mutation_started(
        predecessor,
        successor,
        uid=uid,
        gid=gid,
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_abort_authorization_invalid"
        )
    _release_require_live_triplet_no_extended_metadata(
        uid=uid,
        gid=gid,
        require_complete=True,
    )
    _validate_live_predecessor(predecessor, uid=uid, gid=gid)
    receipt = _release_aborted_receipt(
        transaction_path,
        transaction,
        prepared_receipt,
        preauthorization_receipt,
        successor,
    )
    _install_exact(
        transaction_path / ABORT_RECEIPT_FILE_NAME,
        _canonical(receipt),
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    _checkpoint("v4_preauthorization_aborted")
    if (
        _release_logical_file_present(
            transaction_path,
            ACTIVATION_BEGIN_FILE_NAME,
        )
        or _release_logical_file_present(
            transaction_path,
            RECEIPT_FILE_NAME,
        )
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_abort_authorization_invalid"
        )
    if _release_live_mutation_started(
        predecessor,
        successor,
        uid=uid,
        gid=gid,
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_abort_authorization_invalid"
        )
    _release_require_live_triplet_no_extended_metadata(
        uid=uid,
        gid=gid,
        require_complete=True,
    )
    _validate_live_predecessor(predecessor, uid=uid, gid=gid)
    _require_release_transaction_inventory(
        transaction_path,
        uid=uid,
        gid=gid,
    )
    persisted = _load_release_abort_receipt(
        transaction_path,
        transaction=transaction,
        successor=successor,
        prepared_receipt=prepared_receipt,
        preauthorization_receipt=preauthorization_receipt,
        uid=uid,
        gid=gid,
    )
    if persisted is None:
        raise UnitInputRotationError(
            "unit_input_rotation_abort_receipt_invalid"
        )
    return persisted


def _release_live_mutation_started(
    predecessor: _ReleaseAuthorityTriplet,
    successor: _ReleaseSuccessor,
    *,
    uid: int,
    gid: int,
) -> bool:
    _release_require_live_triplet_no_extended_metadata(
        uid=uid,
        gid=gid,
        require_complete=False,
    )
    observed = (
        _optional_exact(
            package.STAGED_UNIT_INPUT_PLAN_PATH,
            uid=uid,
            gid=gid,
            mode=0o400,
        ),
        _optional_exact(
            package.STAGED_UNIT_INPUT_APPROVAL_PATH,
            uid=uid,
            gid=gid,
            mode=0o400,
        ),
        _optional_exact(
            package.FIXED_UNIT_INPUTS_PATH,
            uid=uid,
            gid=gid,
            mode=package.FIXED_UNIT_INPUTS_MODE,
        ),
    )
    predecessor_values = (
        predecessor.plan_raw,
        predecessor.approval_raw,
        predecessor.fixed_inputs_raw,
    )
    successor_values = (
        successor.plan_raw,
        successor.approval_raw,
        successor.fixed_inputs_raw,
    )
    if any(
        value not in {None, old, new}
        for value, old, new in zip(
            observed,
            predecessor_values,
            successor_values,
            strict=True,
        )
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_terminal_state_invalid"
        )
    _release_require_live_triplet_no_extended_metadata(
        uid=uid,
        gid=gid,
        require_complete=False,
    )
    return observed != predecessor_values


def _publish_release_activation_begin(
    transaction_path: Path,
    transaction: Mapping[str, Any],
    predecessor: _ReleaseAuthorityTriplet,
    successor: _ReleaseSuccessor,
    preauthorization_receipt: Mapping[str, Any],
    *,
    uid: int,
    gid: int,
) -> Mapping[str, Any]:
    if (
        _release_logical_file_present(
            transaction_path,
            ABORT_RECEIPT_FILE_NAME,
        )
        or _release_logical_file_present(
            transaction_path,
            RECEIPT_FILE_NAME,
        )
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_activation_begin_invalid"
        )
    _release_require_live_triplet_no_extended_metadata(
        uid=uid,
        gid=gid,
        require_complete=False,
    )
    if _release_live_mutation_started(
        predecessor,
        successor,
        uid=uid,
        gid=gid,
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_activation_begin_invalid"
        )
    _release_require_live_triplet_no_extended_metadata(
        uid=uid,
        gid=gid,
        require_complete=True,
    )
    _validate_live_predecessor(predecessor, uid=uid, gid=gid)
    value = _release_activation_begin_value(
        transaction,
        successor,
        preauthorization_receipt,
    )
    if (
        _validate_release_activation_begin(
            value,
            transaction=transaction,
            successor=successor,
            preauthorization_receipt=preauthorization_receipt,
        )
        != value
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_activation_begin_invalid"
        )
    _install_exact(
        transaction_path / ACTIVATION_BEGIN_FILE_NAME,
        _canonical(value),
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    _checkpoint("v4_live_activation_begun")
    _require_release_transaction_inventory(
        transaction_path,
        uid=uid,
        gid=gid,
    )
    persisted = _load_release_activation_begin(
        transaction_path,
        transaction=transaction,
        successor=successor,
        preauthorization_receipt=preauthorization_receipt,
        uid=uid,
        gid=gid,
    )
    if persisted is None:
        raise UnitInputRotationError(
            "unit_input_rotation_activation_begin_invalid"
        )
    return persisted


def _load_release_final_receipt(
    transaction_path: Path,
    *,
    transaction: Mapping[str, Any],
    successor: _ReleaseSuccessor,
    prepared_receipt: Mapping[str, Any],
    mutation_begin: Mapping[str, Any],
    activation_begin: Mapping[str, Any],
    uid: int,
    gid: int,
) -> Mapping[str, Any] | None:
    path = transaction_path / RECEIPT_FILE_NAME
    if not os.path.lexists(path):
        return None
    receipt = validate_release_rotation_receipt(
        _decode(_read_exact(path, uid=uid, gid=gid, mode=0o400)),
        unit_input_publication=successor.publication,
        release_update_publication=successor.release_update_publication,
        trusted_predecessor=successor.trusted_predecessor,
        expected_predecessor_trust_sha256=transaction[
            "predecessor_trust_sha256"
        ],
        prepared_receipt=prepared_receipt,
        mutation_begin=mutation_begin,
        activation_begin=activation_begin,
    )
    if receipt != _release_finalized_receipt(
        transaction_path,
        transaction,
        prepared_receipt,
        mutation_begin,
        activation_begin,
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_receipt_invalid"
        )
    return receipt


def _publish_release_prepared_receipt(
    transaction_path: Path,
    transaction: Mapping[str, Any],
    predecessor: _ReleaseAuthorityTriplet,
    successor: _ReleaseSuccessor,
    *,
    uid: int,
    gid: int,
) -> Mapping[str, Any]:
    _validate_live_predecessor(predecessor, uid=uid, gid=gid)
    receipt = _release_prepared_receipt(transaction_path, transaction)
    _install_exact(
        transaction_path / PREPARED_RECEIPT_FILE_NAME,
        _canonical(receipt),
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    _checkpoint("v4_prepared_receipt_published")
    _validate_live_predecessor(predecessor, uid=uid, gid=gid)
    _require_release_transaction_inventory(
        transaction_path,
        uid=uid,
        gid=gid,
    )
    persisted = _load_release_prepared_receipt(
        transaction_path,
        transaction=transaction,
        successor=successor,
        uid=uid,
        gid=gid,
    )
    if persisted is None:
        raise UnitInputRotationError(
            "unit_input_rotation_prepared_receipt_invalid"
        )
    return persisted


def _publish_release_final_receipt(
    transaction_path: Path,
    transaction: Mapping[str, Any],
    predecessor: _ReleaseAuthorityTriplet,
    successor: _ReleaseSuccessor,
    prepared_receipt: Mapping[str, Any],
    mutation_begin: Mapping[str, Any],
    activation_begin: Mapping[str, Any],
    *,
    uid: int,
    gid: int,
) -> Mapping[str, Any]:
    _release_require_live_triplet_no_extended_metadata(
        uid=uid,
        gid=gid,
        require_complete=True,
    )
    _release_validate_archived_predecessor(
        transaction_path,
        transaction,
        successor,
        uid=uid,
        gid=gid,
    )
    _validate_live_successor(successor, uid=uid, gid=gid)
    _release_require_live_triplet_no_extended_metadata(
        uid=uid,
        gid=gid,
        require_complete=True,
    )
    persisted_prepared = _load_release_prepared_receipt(
        transaction_path,
        transaction=transaction,
        successor=successor,
        uid=uid,
        gid=gid,
    )
    persisted_mutation = _load_release_mutation_begin(
        transaction_path,
        transaction=transaction,
        successor=successor,
        uid=uid,
        gid=gid,
    )
    persisted_activation = _load_release_activation_begin(
        transaction_path,
        transaction=transaction,
        successor=successor,
        preauthorization_receipt=mutation_begin,
        uid=uid,
        gid=gid,
    )
    if (
        persisted_prepared is None
        or persisted_prepared != prepared_receipt
        or persisted_mutation is None
        or persisted_mutation != mutation_begin
        or persisted_activation is None
        or persisted_activation != activation_begin
        or transaction["predecessor"]
        != _release_predecessor_record(predecessor)
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_finalize_authorization_invalid"
        )
    receipt = _release_finalized_receipt(
        transaction_path,
        transaction,
        persisted_prepared,
        mutation_begin,
        persisted_activation,
    )
    _install_exact(
        transaction_path / RECEIPT_FILE_NAME,
        _canonical(receipt),
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    _checkpoint("v4_final_receipt_published")
    _require_release_transaction_inventory(
        transaction_path,
        uid=uid,
        gid=gid,
    )
    persisted = _load_release_final_receipt(
        transaction_path,
        transaction=transaction,
        successor=successor,
        prepared_receipt=persisted_prepared,
        mutation_begin=mutation_begin,
        activation_begin=persisted_activation,
        uid=uid,
        gid=gid,
    )
    if persisted is None:
        raise UnitInputRotationError(
            "unit_input_rotation_receipt_invalid"
        )
    return persisted


def _prepare_release_unit_input_authority_rotation(
    unit_input_publication: Mapping[str, Any],
    release_update_publication: Mapping[str, Any],
    *,
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
    require_root: bool,
    clock: Callable[[], int],
    lock_factory: Callable[[], Any] | None,
) -> Mapping[str, Any]:
    """Prepare an exact v3→v4 or v4→v4 authority transition.

    Preparation persists the owner-signed unit-input and release-update
    publications, the externally pinned predecessor envelope, and an exact
    predecessor archive.  It never mutates the live triplet.
    """

    uid, gid = _rotation_identity(require_root=require_root)
    (
        incoming_unit_raw,
        incoming_update_raw,
        incoming_trust_raw,
        incoming_publication_sha256,
        _incoming_update_publication_sha256,
    ) = _release_candidate_envelope(
        unit_input_publication,
        release_update_publication,
        trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
    )
    incoming_raw = (
        incoming_unit_raw,
        incoming_update_raw,
        incoming_trust_raw,
    )
    trusted = _release_predecessor_trust(
        trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
    )
    try:
        context = authority_lock.authority_activation_lock(
            require_root=require_root,
            lock_factory=lock_factory,
        )
        with context:

            def gate_now() -> int:
                observed = clock()
                if type(observed) is not int or observed <= 0:
                    raise UnitInputRotationError(
                        "unit_input_rotation_clock_invalid"
                    )
                return observed

            staged_root = package.STAGED_UNIT_INPUT_PLAN_PATH.parent
            _require_directory(staged_root, uid=uid, gid=gid)
            _release_require_live_triplet_no_extended_metadata(
                uid=uid,
                gid=gid,
                require_complete=True,
            )
            root = _release_audit_root(uid=uid, gid=gid)
            completed_match: tuple[
                Path,
                Mapping[str, Any],
                _ReleaseSuccessor,
                Mapping[str, Any],
            ] | None = None
            incomplete: list[
                tuple[Path, Mapping[str, Any] | None]
            ] = []
            for directory in _release_transaction_directories(
                root,
                uid=uid,
                gid=gid,
                recover_pending_names=_RELEASE_PREPARE_PENDING_NAMES,
            ):
                transaction = _load_release_transaction(
                    directory,
                    uid=uid,
                    gid=gid,
                    recover_pending_names=(
                        _RELEASE_PREPARE_PENDING_NAMES
                    ),
                )
                if (
                    transaction is not None
                    and os.path.lexists(
                        directory / ABORT_RECEIPT_FILE_NAME
                    )
                ):
                    persisted = _persisted_release_successor(
                        directory,
                        transaction,
                        uid=uid,
                        gid=gid,
                    )
                    _release_validate_archived_predecessor(
                        directory,
                        transaction,
                        persisted,
                        uid=uid,
                        gid=gid,
                    )
                    prepared = _load_release_prepared_receipt(
                        directory,
                        transaction=transaction,
                        successor=persisted,
                        uid=uid,
                        gid=gid,
                    )
                    preauthorization = _load_release_mutation_begin(
                        directory,
                        transaction=transaction,
                        successor=persisted,
                        uid=uid,
                        gid=gid,
                    )
                    if prepared is None or preauthorization is None:
                        raise UnitInputRotationError(
                            "unit_input_rotation_abort_receipt_invalid"
                        )
                    aborted = _load_release_abort_receipt(
                        directory,
                        transaction=transaction,
                        successor=persisted,
                        prepared_receipt=prepared,
                        preauthorization_receipt=preauthorization,
                        uid=uid,
                        gid=gid,
                    )
                    if aborted is None:
                        raise UnitInputRotationError(
                            "unit_input_rotation_abort_receipt_invalid"
                        )
                    continue
                if (
                    transaction is None
                    or not os.path.lexists(directory / RECEIPT_FILE_NAME)
                ):
                    incomplete.append((directory, transaction))
                    continue
                persisted = _persisted_release_successor(
                    directory,
                    transaction,
                    uid=uid,
                    gid=gid,
                )
                _release_validate_archived_predecessor(
                    directory,
                    transaction,
                    persisted,
                    uid=uid,
                    gid=gid,
                )
                prepared = _load_release_prepared_receipt(
                    directory,
                    transaction=transaction,
                    successor=persisted,
                    uid=uid,
                    gid=gid,
                )
                if prepared is None:
                    raise UnitInputRotationError(
                        "unit_input_rotation_receipt_invalid"
                    )
                mutation_begin = _load_release_mutation_begin(
                    directory,
                    transaction=transaction,
                    successor=persisted,
                    uid=uid,
                    gid=gid,
                )
                if mutation_begin is None:
                    raise UnitInputRotationError(
                        "unit_input_rotation_receipt_invalid"
                    )
                activation_begin = _load_release_activation_begin(
                    directory,
                    transaction=transaction,
                    successor=persisted,
                    preauthorization_receipt=mutation_begin,
                    uid=uid,
                    gid=gid,
                )
                if activation_begin is None:
                    raise UnitInputRotationError(
                        "unit_input_rotation_receipt_invalid"
                    )
                final = _load_release_final_receipt(
                    directory,
                    transaction=transaction,
                    successor=persisted,
                    prepared_receipt=prepared,
                    mutation_begin=mutation_begin,
                    activation_begin=activation_begin,
                    uid=uid,
                    gid=gid,
                )
                if final is None:
                    raise UnitInputRotationError(
                        "unit_input_rotation_receipt_invalid"
                    )
                if incoming_raw == (
                    persisted.publication_raw,
                    persisted.release_update_publication_raw,
                    persisted.trusted_predecessor_raw,
                ):
                    if completed_match is not None:
                        raise UnitInputRotationError(
                            "unit_input_rotation_recovery_ambiguous"
                        )
                    completed_match = (
                        directory,
                        transaction,
                        persisted,
                        prepared,
                    )

            if incomplete:
                if len(incomplete) != 1:
                    raise UnitInputRotationError(
                        "unit_input_rotation_recovery_ambiguous"
                    )
                transaction_path, transaction = incomplete[0]
                if not transaction_path.name.endswith(
                    "-" + incoming_publication_sha256
                ):
                    raise UnitInputRotationError(
                        "unit_input_rotation_successor_conflict"
                    )
                if transaction is None:
                    predecessor = _release_triplet(
                        uid=uid,
                        gid=gid,
                        trusted_predecessor=trusted,
                    )
                    if not transaction_path.name.startswith(
                        predecessor.plan["plan_sha256"] + "-"
                    ):
                        raise UnitInputRotationError(
                            "unit_input_rotation_recovery_ambiguous"
                        )
                    authorization_checked_at_unix = gate_now()
                    successor = _release_successor(
                        unit_input_publication,
                        release_update_publication,
                        trusted,
                        expected_predecessor_trust_sha256=(
                            expected_predecessor_trust_sha256
                        ),
                        now_unix=authorization_checked_at_unix,
                    )
                    transaction = _prepare_release_transaction(
                        transaction_path=transaction_path,
                        predecessor=predecessor,
                        successor=successor,
                        authorization_checked_at_unix=(
                            authorization_checked_at_unix
                        ),
                        uid=uid,
                        gid=gid,
                    )
                else:
                    resume_persistence = False
                    try:
                        successor = _release_successor_from_transaction(
                            transaction_path,
                            transaction,
                            incoming_raw,
                            uid=uid,
                            gid=gid,
                        )
                    except UnitInputRotationError:
                        resume_persistence = True
                        successor = _release_successor(
                            unit_input_publication,
                            release_update_publication,
                            trusted,
                            expected_predecessor_trust_sha256=(
                                expected_predecessor_trust_sha256
                            ),
                            now_unix=transaction[
                                "authorization_checked_at_unix"
                            ],
                        )
                        _release_require_same_successor(
                            transaction,
                            successor,
                        )
                        if incoming_raw != (
                            successor.publication_raw,
                            successor.release_update_publication_raw,
                            successor.trusted_predecessor_raw,
                        ):
                            raise UnitInputRotationError(
                                "unit_input_rotation_successor_conflict"
                            ) from None
                    try:
                        predecessor = (
                            _release_validate_archived_predecessor(
                                transaction_path,
                                transaction,
                                successor,
                                uid=uid,
                                gid=gid,
                            )
                        )
                    except UnitInputRotationError:
                        predecessor = _release_triplet(
                            uid=uid,
                            gid=gid,
                            trusted_predecessor=(
                                successor.trusted_predecessor
                            ),
                        )
                        expected = _release_transaction_value(
                            predecessor,
                            successor,
                            authorization_checked_at_unix=transaction[
                                "authorization_checked_at_unix"
                            ],
                        )
                        if expected != transaction:
                            raise UnitInputRotationError(
                                "unit_input_rotation_recovery_ambiguous"
                            ) from None
                        transaction = _prepare_release_transaction(
                            transaction_path=transaction_path,
                            predecessor=predecessor,
                            successor=successor,
                            authorization_checked_at_unix=transaction[
                                "authorization_checked_at_unix"
                            ],
                            uid=uid,
                            gid=gid,
                        )
                    if resume_persistence:
                        transaction = _prepare_release_transaction(
                            transaction_path=transaction_path,
                            predecessor=predecessor,
                            successor=successor,
                            authorization_checked_at_unix=transaction[
                                "authorization_checked_at_unix"
                            ],
                            uid=uid,
                            gid=gid,
                        )
            elif completed_match is not None:
                (
                    _transaction_path,
                    _transaction,
                    successor,
                    prepared,
                ) = completed_match
                _validate_live_successor(successor, uid=uid, gid=gid)
                return prepared
            else:
                predecessor = _release_triplet(
                    uid=uid,
                    gid=gid,
                    trusted_predecessor=trusted,
                )
                authorization_checked_at_unix = gate_now()
                successor = _release_successor(
                    unit_input_publication,
                    release_update_publication,
                    trusted,
                    expected_predecessor_trust_sha256=(
                        expected_predecessor_trust_sha256
                    ),
                    now_unix=authorization_checked_at_unix,
                )
                if predecessor.revision == successor.revision:
                    raise UnitInputRotationError(
                        "unit_input_rotation_successor_conflict"
                    )
                transaction_path = root / (
                    f"{predecessor.plan['plan_sha256']}-"
                    f"{successor.publication['publication_sha256']}"
                )
                if os.path.lexists(transaction_path):
                    raise UnitInputRotationError(
                        "unit_input_rotation_successor_conflict"
                    )
                _checkpoint("v4_before_transaction_authorized")
                transaction = _prepare_release_transaction(
                    transaction_path=transaction_path,
                    predecessor=predecessor,
                    successor=successor,
                    authorization_checked_at_unix=(
                        authorization_checked_at_unix
                    ),
                    uid=uid,
                    gid=gid,
                )

            persisted_prepared = _load_release_prepared_receipt(
                transaction_path,
                transaction=transaction,
                successor=successor,
                uid=uid,
                gid=gid,
            )
            if persisted_prepared is not None:
                if _live_successor_complete(
                    successor,
                    uid=uid,
                    gid=gid,
                ):
                    return persisted_prepared
                _validate_live_predecessor(
                    predecessor,
                    uid=uid,
                    gid=gid,
                )
                return persisted_prepared
            return _publish_release_prepared_receipt(
                transaction_path,
                transaction,
                predecessor,
                successor,
                uid=uid,
                gid=gid,
            )
    except UnitInputRotationError:
        raise
    except (
        OSError,
        RuntimeError,
        package.PackagingError,
        release_inputs_v4.ProductionReleaseUnitInputsV4Error,
        release_update.ProductionReleaseUpdateContractError,
    ) as exc:
        raise UnitInputRotationError("unit_input_rotation_failed") from exc


def _release_require_other_transactions_terminal(
    root: Path,
    transaction_path: Path,
    *,
    uid: int,
    gid: int,
) -> None:
    for directory in _release_transaction_directories(
        root,
        uid=uid,
        gid=gid,
    ):
        if directory == transaction_path:
            continue
        transaction = _load_release_transaction(
            directory,
            uid=uid,
            gid=gid,
        )
        if transaction is None:
            raise UnitInputRotationError(
                "unit_input_rotation_recovery_ambiguous"
            )
        successor = _persisted_release_successor(
            directory,
            transaction,
            uid=uid,
            gid=gid,
        )
        _release_validate_archived_predecessor(
            directory,
            transaction,
            successor,
            uid=uid,
            gid=gid,
        )
        prepared = _load_release_prepared_receipt(
            directory,
            transaction=transaction,
            successor=successor,
            uid=uid,
            gid=gid,
        )
        preauthorization = _load_release_mutation_begin(
            directory,
            transaction=transaction,
            successor=successor,
            uid=uid,
            gid=gid,
        )
        if prepared is None or preauthorization is None:
            raise UnitInputRotationError(
                "unit_input_rotation_recovery_ambiguous"
            )
        if os.path.lexists(directory / ABORT_RECEIPT_FILE_NAME):
            aborted = _load_release_abort_receipt(
                directory,
                transaction=transaction,
                successor=successor,
                prepared_receipt=prepared,
                preauthorization_receipt=preauthorization,
                uid=uid,
                gid=gid,
            )
            if aborted is None:
                raise UnitInputRotationError(
                    "unit_input_rotation_abort_receipt_invalid"
                )
            continue
        activation_begin = _load_release_activation_begin(
            directory,
            transaction=transaction,
            successor=successor,
            preauthorization_receipt=preauthorization,
            uid=uid,
            gid=gid,
        )
        if activation_begin is None:
            raise UnitInputRotationError(
                "unit_input_rotation_recovery_ambiguous"
            )
        final = _load_release_final_receipt(
            directory,
            transaction=transaction,
            successor=successor,
            prepared_receipt=prepared,
            mutation_begin=preauthorization,
            activation_begin=activation_begin,
            uid=uid,
            gid=gid,
        )
        if final is None:
            raise UnitInputRotationError(
                "unit_input_rotation_recovery_ambiguous"
            )


def _preauthorize_prepared_release_unit_input_authority_rotation(
    unit_input_publication: Mapping[str, Any],
    release_update_publication: Mapping[str, Any],
    prepared_receipt: Mapping[str, Any],
    *,
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
    expected_transaction_sha256: str,
    require_root: bool,
    clock: Callable[[], int],
    lock_factory: Callable[[], Any] | None,
) -> Mapping[str, Any]:
    """Durably authorize one exact prepared rotation without live mutation."""

    uid, gid = _rotation_identity(require_root=require_root)
    supplied = validate_release_prepared_rotation_receipt(
        prepared_receipt,
        unit_input_publication=unit_input_publication,
        release_update_publication=release_update_publication,
        trusted_predecessor=trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
    )
    if (
        _SHA256.fullmatch(str(expected_transaction_sha256 or "")) is None
        or supplied["transaction_sha256"]
        != expected_transaction_sha256
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_preauthorization_invalid"
        )
    incoming = _release_candidate_envelope(
        unit_input_publication,
        release_update_publication,
        trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
    )
    incoming_raw = incoming[:3]
    try:
        context = authority_lock.authority_activation_lock(
            require_root=require_root,
            lock_factory=lock_factory,
        )
        with context:
            staged_root = package.STAGED_UNIT_INPUT_PLAN_PATH.parent
            _require_directory(staged_root, uid=uid, gid=gid)
            _release_require_live_triplet_no_extended_metadata(
                uid=uid,
                gid=gid,
                require_complete=False,
            )
            root = cutover.EVIDENCE_ROOT / RELEASE_AUDIT_DIRECTORY_NAME
            _require_directory(root, uid=uid, gid=gid)
            _release_require_no_extended_metadata(
                cutover.EVIDENCE_ROOT,
                uid=uid,
                gid=gid,
            )
            _release_require_no_extended_metadata(
                root,
                uid=uid,
                gid=gid,
            )
            transaction_path = Path(supplied["audit_transaction_path"])
            _require_directory(transaction_path, uid=uid, gid=gid)
            if _release_logical_file_present(
                transaction_path,
                ABORT_RECEIPT_FILE_NAME,
            ):
                raise UnitInputRotationError(
                    "unit_input_rotation_preauthorization_aborted"
                )
            transaction = _load_release_transaction(
                transaction_path,
                uid=uid,
                gid=gid,
                recover_pending_names=(
                    _RELEASE_PREAUTHORIZE_PENDING_NAMES
                ),
            )
            if (
                transaction is None
                or transaction["transaction_sha256"]
                != expected_transaction_sha256
            ):
                raise UnitInputRotationError(
                    "unit_input_rotation_preauthorization_invalid"
                )
            _release_require_other_transactions_terminal(
                root,
                transaction_path,
                uid=uid,
                gid=gid,
            )
            successor = _release_successor_from_transaction(
                transaction_path,
                transaction,
                incoming_raw,
                uid=uid,
                gid=gid,
            )
            predecessor = _release_validate_archived_predecessor(
                transaction_path,
                transaction,
                successor,
                uid=uid,
                gid=gid,
            )
            persisted_prepared = _load_release_prepared_receipt(
                transaction_path,
                transaction=transaction,
                successor=successor,
                uid=uid,
                gid=gid,
            )
            if (
                persisted_prepared is None
                or persisted_prepared != supplied
            ):
                raise UnitInputRotationError(
                    "unit_input_rotation_preauthorization_invalid"
                )
            preauthorization = _load_release_mutation_begin(
                transaction_path,
                transaction=transaction,
                successor=successor,
                uid=uid,
                gid=gid,
            )
            if _release_logical_file_present(
                transaction_path,
                ABORT_RECEIPT_FILE_NAME,
            ):
                raise UnitInputRotationError(
                    "unit_input_rotation_preauthorization_aborted"
                )
            final = None
            activation_begin = None
            if preauthorization is not None:
                activation_begin = _load_release_activation_begin(
                    transaction_path,
                    transaction=transaction,
                    successor=successor,
                    preauthorization_receipt=preauthorization,
                    uid=uid,
                    gid=gid,
                )
                if activation_begin is not None:
                    final = _load_release_final_receipt(
                        transaction_path,
                        transaction=transaction,
                        successor=successor,
                        prepared_receipt=persisted_prepared,
                        mutation_begin=preauthorization,
                        activation_begin=activation_begin,
                        uid=uid,
                        gid=gid,
                    )
            if final is not None:
                if preauthorization is None:
                    raise UnitInputRotationError(
                        "unit_input_rotation_preauthorization_invalid"
                    )
                _validate_live_successor(successor, uid=uid, gid=gid)
                _release_require_live_triplet_no_extended_metadata(
                    uid=uid,
                    gid=gid,
                    require_complete=True,
                )
                return preauthorization
            if preauthorization is None:
                return _publish_release_mutation_begin(
                    transaction_path,
                    transaction,
                    predecessor,
                    successor,
                    clock=clock,
                    uid=uid,
                    gid=gid,
                )
            if _release_live_mutation_started(
                predecessor,
                successor,
                uid=uid,
                gid=gid,
            ):
                return preauthorization
            _release_require_live_triplet_no_extended_metadata(
                uid=uid,
                gid=gid,
                require_complete=True,
            )
            _validate_live_predecessor(predecessor, uid=uid, gid=gid)
            return preauthorization
    except UnitInputRotationError:
        raise
    except (
        OSError,
        RuntimeError,
        package.PackagingError,
        release_inputs_v4.ProductionReleaseUnitInputsV4Error,
        release_update.ProductionReleaseUpdateContractError,
    ) as exc:
        raise UnitInputRotationError("unit_input_rotation_failed") from exc


def _finalize_preauthorized_release_unit_input_authority_rotation(
    unit_input_publication: Mapping[str, Any],
    release_update_publication: Mapping[str, Any],
    prepared_receipt: Mapping[str, Any],
    preauthorization_receipt: Mapping[str, Any],
    *,
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
    expected_transaction_sha256: str,
    require_root: bool,
    lock_factory: Callable[[], Any] | None,
) -> Mapping[str, Any]:
    """Finalize only the supplied exact durable preauthorization."""

    uid, gid = _rotation_identity(require_root=require_root)
    supplied_prepared = validate_release_prepared_rotation_receipt(
        prepared_receipt,
        unit_input_publication=unit_input_publication,
        release_update_publication=release_update_publication,
        trusted_predecessor=trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
    )
    supplied_preauthorization = (
        validate_release_preauthorization_receipt(
            preauthorization_receipt,
            unit_input_publication=unit_input_publication,
            release_update_publication=release_update_publication,
            trusted_predecessor=trusted_predecessor,
            expected_predecessor_trust_sha256=(
                expected_predecessor_trust_sha256
            ),
            prepared_receipt=supplied_prepared,
        )
    )
    if (
        _SHA256.fullmatch(str(expected_transaction_sha256 or "")) is None
        or supplied_prepared["transaction_sha256"]
        != expected_transaction_sha256
        or supplied_preauthorization["transaction_sha256"]
        != expected_transaction_sha256
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_finalize_authorization_invalid"
        )
    incoming_raw = _release_candidate_envelope(
        unit_input_publication,
        release_update_publication,
        trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
    )[:3]
    try:
        context = authority_lock.authority_activation_lock(
            require_root=require_root,
            lock_factory=lock_factory,
        )
        with context:
            staged_root = package.STAGED_UNIT_INPUT_PLAN_PATH.parent
            _require_directory(staged_root, uid=uid, gid=gid)
            _release_require_live_triplet_no_extended_metadata(
                uid=uid,
                gid=gid,
                require_complete=False,
            )
            root = cutover.EVIDENCE_ROOT / RELEASE_AUDIT_DIRECTORY_NAME
            _require_directory(root, uid=uid, gid=gid)
            _release_require_no_extended_metadata(
                cutover.EVIDENCE_ROOT,
                uid=uid,
                gid=gid,
            )
            _release_require_no_extended_metadata(
                root,
                uid=uid,
                gid=gid,
            )
            transaction_path = Path(
                supplied_prepared["audit_transaction_path"]
            )
            _require_directory(transaction_path, uid=uid, gid=gid)
            if _release_logical_file_present(
                transaction_path,
                ABORT_RECEIPT_FILE_NAME,
            ):
                raise UnitInputRotationError(
                    "unit_input_rotation_preauthorization_aborted"
                )
            transaction = _load_release_transaction(
                transaction_path,
                uid=uid,
                gid=gid,
                recover_pending_names=_RELEASE_FINALIZE_PENDING_NAMES,
            )
            if (
                transaction is None
                or transaction["transaction_sha256"]
                != expected_transaction_sha256
            ):
                raise UnitInputRotationError(
                    "unit_input_rotation_finalize_authorization_invalid"
                )
            _release_require_other_transactions_terminal(
                root,
                transaction_path,
                uid=uid,
                gid=gid,
            )
            successor = _release_successor_from_transaction(
                transaction_path,
                transaction,
                incoming_raw,
                uid=uid,
                gid=gid,
            )
            predecessor = _release_validate_archived_predecessor(
                transaction_path,
                transaction,
                successor,
                uid=uid,
                gid=gid,
            )
            persisted_prepared = _load_release_prepared_receipt(
                transaction_path,
                transaction=transaction,
                successor=successor,
                uid=uid,
                gid=gid,
            )
            persisted_preauthorization = _load_release_mutation_begin(
                transaction_path,
                transaction=transaction,
                successor=successor,
                uid=uid,
                gid=gid,
            )
            if (
                persisted_prepared is None
                or persisted_prepared != supplied_prepared
                or persisted_preauthorization is None
                or persisted_preauthorization
                != supplied_preauthorization
            ):
                raise UnitInputRotationError(
                    "unit_input_rotation_finalize_authorization_invalid"
                )
            if _release_logical_file_present(
                transaction_path,
                ABORT_RECEIPT_FILE_NAME,
            ):
                raise UnitInputRotationError(
                    "unit_input_rotation_preauthorization_aborted"
                )
            activation_begin = _load_release_activation_begin(
                transaction_path,
                transaction=transaction,
                successor=successor,
                preauthorization_receipt=persisted_preauthorization,
                uid=uid,
                gid=gid,
            )
            final = None
            if activation_begin is not None:
                final = _load_release_final_receipt(
                    transaction_path,
                    transaction=transaction,
                    successor=successor,
                    prepared_receipt=persisted_prepared,
                    mutation_begin=persisted_preauthorization,
                    activation_begin=activation_begin,
                    uid=uid,
                    gid=gid,
                )
            if final is not None:
                _validate_live_successor(successor, uid=uid, gid=gid)
                _release_require_live_triplet_no_extended_metadata(
                    uid=uid,
                    gid=gid,
                    require_complete=True,
                )
                return final
            if activation_begin is None:
                activation_begin = _publish_release_activation_begin(
                    transaction_path,
                    transaction,
                    predecessor,
                    successor,
                    persisted_preauthorization,
                    uid=uid,
                    gid=gid,
                )
            _activate_successor_triplet(
                predecessor,
                successor,
                uid=uid,
                gid=gid,
            )
            return _publish_release_final_receipt(
                transaction_path,
                transaction,
                predecessor,
                successor,
                persisted_prepared,
                persisted_preauthorization,
                activation_begin,
                uid=uid,
                gid=gid,
            )
    except UnitInputRotationError:
        raise
    except ExceptionGroup as exc:
        raise UnitInputRotationError(
            "unit_input_rotation_rollback_failed"
        ) from exc
    except (
        OSError,
        RuntimeError,
        package.PackagingError,
        release_inputs_v4.ProductionReleaseUnitInputsV4Error,
        release_update.ProductionReleaseUpdateContractError,
    ) as exc:
        raise UnitInputRotationError("unit_input_rotation_failed") from exc


def _abort_preauthorized_release_unit_input_authority_rotation(
    unit_input_publication: Mapping[str, Any],
    release_update_publication: Mapping[str, Any],
    prepared_receipt: Mapping[str, Any],
    preauthorization_receipt: Mapping[str, Any],
    *,
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
    expected_transaction_sha256: str,
    require_root: bool,
    lock_factory: Callable[[], Any] | None,
) -> Mapping[str, Any]:
    """Append one terminal abort before any live triplet mutation."""

    uid, gid = _rotation_identity(require_root=require_root)
    supplied_prepared = validate_release_prepared_rotation_receipt(
        prepared_receipt,
        unit_input_publication=unit_input_publication,
        release_update_publication=release_update_publication,
        trusted_predecessor=trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
    )
    supplied_preauthorization = (
        validate_release_preauthorization_receipt(
            preauthorization_receipt,
            unit_input_publication=unit_input_publication,
            release_update_publication=release_update_publication,
            trusted_predecessor=trusted_predecessor,
            expected_predecessor_trust_sha256=(
                expected_predecessor_trust_sha256
            ),
            prepared_receipt=supplied_prepared,
        )
    )
    if (
        _SHA256.fullmatch(str(expected_transaction_sha256 or "")) is None
        or supplied_prepared["transaction_sha256"]
        != expected_transaction_sha256
        or supplied_preauthorization["transaction_sha256"]
        != expected_transaction_sha256
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_abort_authorization_invalid"
        )
    incoming_raw = _release_candidate_envelope(
        unit_input_publication,
        release_update_publication,
        trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
    )[:3]
    try:
        context = authority_lock.authority_activation_lock(
            require_root=require_root,
            lock_factory=lock_factory,
        )
        with context:
            root = cutover.EVIDENCE_ROOT / RELEASE_AUDIT_DIRECTORY_NAME
            _require_directory(root, uid=uid, gid=gid)
            _release_require_no_extended_metadata(
                cutover.EVIDENCE_ROOT,
                uid=uid,
                gid=gid,
            )
            _release_require_no_extended_metadata(
                root,
                uid=uid,
                gid=gid,
            )
            transaction_path = Path(
                supplied_prepared["audit_transaction_path"]
            )
            _require_directory(transaction_path, uid=uid, gid=gid)
            if (
                _release_logical_file_present(
                    transaction_path,
                    ACTIVATION_BEGIN_FILE_NAME,
                )
                or _release_logical_file_present(
                    transaction_path,
                    RECEIPT_FILE_NAME,
                )
            ):
                raise UnitInputRotationError(
                    "unit_input_rotation_abort_authorization_invalid"
                )
            transaction = _load_release_transaction(
                transaction_path,
                uid=uid,
                gid=gid,
                recover_pending_names=_RELEASE_ABORT_PENDING_NAMES,
            )
            if (
                transaction is None
                or transaction["transaction_sha256"]
                != expected_transaction_sha256
            ):
                raise UnitInputRotationError(
                    "unit_input_rotation_abort_authorization_invalid"
                )
            successor = _release_successor_from_transaction(
                transaction_path,
                transaction,
                incoming_raw,
                uid=uid,
                gid=gid,
            )
            predecessor = _release_validate_archived_predecessor(
                transaction_path,
                transaction,
                successor,
                uid=uid,
                gid=gid,
            )
            persisted_prepared = _load_release_prepared_receipt(
                transaction_path,
                transaction=transaction,
                successor=successor,
                uid=uid,
                gid=gid,
            )
            persisted_preauthorization = _load_release_mutation_begin(
                transaction_path,
                transaction=transaction,
                successor=successor,
                uid=uid,
                gid=gid,
            )
            if (
                persisted_prepared is None
                or persisted_prepared != supplied_prepared
                or persisted_preauthorization is None
                or persisted_preauthorization
                != supplied_preauthorization
            ):
                raise UnitInputRotationError(
                    "unit_input_rotation_abort_authorization_invalid"
                )
            aborted = _load_release_abort_receipt(
                transaction_path,
                transaction=transaction,
                successor=successor,
                prepared_receipt=persisted_prepared,
                preauthorization_receipt=persisted_preauthorization,
                uid=uid,
                gid=gid,
            )
            if aborted is not None:
                return aborted
            _release_require_other_transactions_terminal(
                root,
                transaction_path,
                uid=uid,
                gid=gid,
            )
            if (
                _release_logical_file_present(
                    transaction_path,
                    ACTIVATION_BEGIN_FILE_NAME,
                )
                or _release_logical_file_present(
                    transaction_path,
                    RECEIPT_FILE_NAME,
                )
            ):
                raise UnitInputRotationError(
                    "unit_input_rotation_abort_authorization_invalid"
                )
            return _publish_release_abort_receipt(
                transaction_path,
                transaction,
                predecessor,
                successor,
                persisted_prepared,
                persisted_preauthorization,
                uid=uid,
                gid=gid,
            )
    except UnitInputRotationError:
        raise
    except (
        OSError,
        RuntimeError,
        package.PackagingError,
        release_inputs_v4.ProductionReleaseUnitInputsV4Error,
        release_update.ProductionReleaseUpdateContractError,
    ) as exc:
        raise UnitInputRotationError("unit_input_rotation_failed") from exc


def _finalize_prepared_release_unit_input_authority_rotation(
    unit_input_publication: Mapping[str, Any],
    release_update_publication: Mapping[str, Any],
    prepared_receipt: Mapping[str, Any],
    *,
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
    expected_transaction_sha256: str,
    require_root: bool,
    clock: Callable[[], int],
    lock_factory: Callable[[], Any] | None,
) -> Mapping[str, Any]:
    """Compatibility composition of durable preauthorization and finalize."""

    preauthorization = (
        _preauthorize_prepared_release_unit_input_authority_rotation(
            unit_input_publication,
            release_update_publication,
            prepared_receipt,
            trusted_predecessor=trusted_predecessor,
            expected_predecessor_trust_sha256=(
                expected_predecessor_trust_sha256
            ),
            expected_transaction_sha256=expected_transaction_sha256,
            require_root=require_root,
            clock=clock,
            lock_factory=lock_factory,
        )
    )
    return _finalize_preauthorized_release_unit_input_authority_rotation(
        unit_input_publication,
        release_update_publication,
        prepared_receipt,
        preauthorization,
        trusted_predecessor=trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
        expected_transaction_sha256=expected_transaction_sha256,
        require_root=require_root,
        lock_factory=lock_factory,
    )


def _production_clock() -> int:
    return int(time.time())


def prepare_release_unit_input_authority_rotation(
    unit_input_publication: Mapping[str, Any],
    release_update_publication: Mapping[str, Any],
    *,
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
) -> Mapping[str, Any]:
    """Root-only production entrypoint with a real clock and fixed lock."""

    return _prepare_release_unit_input_authority_rotation(
        unit_input_publication,
        release_update_publication,
        trusted_predecessor=trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
        require_root=True,
        clock=_production_clock,
        lock_factory=None,
    )


def preauthorize_prepared_release_unit_input_authority_rotation(
    unit_input_publication: Mapping[str, Any],
    release_update_publication: Mapping[str, Any],
    prepared_receipt: Mapping[str, Any],
    *,
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
    expected_transaction_sha256: str,
) -> Mapping[str, Any]:
    """Root-only durable authorization before an outer commit boundary."""

    return _preauthorize_prepared_release_unit_input_authority_rotation(
        unit_input_publication,
        release_update_publication,
        prepared_receipt,
        trusted_predecessor=trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
        expected_transaction_sha256=expected_transaction_sha256,
        require_root=True,
        clock=_production_clock,
        lock_factory=None,
    )


def finalize_preauthorized_release_unit_input_authority_rotation(
    unit_input_publication: Mapping[str, Any],
    release_update_publication: Mapping[str, Any],
    prepared_receipt: Mapping[str, Any],
    preauthorization_receipt: Mapping[str, Any],
    *,
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
    expected_transaction_sha256: str,
) -> Mapping[str, Any]:
    """Root-only exact finalizer with no current-time freshness gate."""

    return _finalize_preauthorized_release_unit_input_authority_rotation(
        unit_input_publication,
        release_update_publication,
        prepared_receipt,
        preauthorization_receipt,
        trusted_predecessor=trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
        expected_transaction_sha256=expected_transaction_sha256,
        require_root=True,
        lock_factory=None,
    )


def abort_preauthorized_release_unit_input_authority_rotation(
    unit_input_publication: Mapping[str, Any],
    release_update_publication: Mapping[str, Any],
    prepared_receipt: Mapping[str, Any],
    preauthorization_receipt: Mapping[str, Any],
    *,
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
    expected_transaction_sha256: str,
) -> Mapping[str, Any]:
    """Root-only append-only abort before any live triplet mutation."""

    return _abort_preauthorized_release_unit_input_authority_rotation(
        unit_input_publication,
        release_update_publication,
        prepared_receipt,
        preauthorization_receipt,
        trusted_predecessor=trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
        expected_transaction_sha256=expected_transaction_sha256,
        require_root=True,
        lock_factory=None,
    )


def finalize_prepared_release_unit_input_authority_rotation(
    unit_input_publication: Mapping[str, Any],
    release_update_publication: Mapping[str, Any],
    prepared_receipt: Mapping[str, Any],
    *,
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
    expected_transaction_sha256: str,
) -> Mapping[str, Any]:
    """Root-only compatibility composition of preauthorize and finalize."""

    return _finalize_prepared_release_unit_input_authority_rotation(
        unit_input_publication,
        release_update_publication,
        prepared_receipt,
        trusted_predecessor=trusted_predecessor,
        expected_predecessor_trust_sha256=(
            expected_predecessor_trust_sha256
        ),
        expected_transaction_sha256=expected_transaction_sha256,
        require_root=True,
        clock=_production_clock,
        lock_factory=None,
    )


def validate_release_unit_input_phase_request(
    action: str,
    value: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Validate one exact, replayable split-phase request without mutation."""

    common = {
        "schema",
        "action",
        "owner_release_revision",
        "remote_stager_revision",
        "unit_input_publication",
        "release_update_publication",
        "trusted_predecessor",
        "expected_predecessor_trust_sha256",
        "secret_material_recorded",
        "secret_digest_recorded",
        "request_sha256",
    }
    phase_fields = {
        "prepare-release-unit-inputs": common,
        "preauthorize-release-unit-inputs": common
        | {"prepared_receipt", "expected_transaction_sha256"},
        "finalize-release-unit-inputs": common
        | {
            "prepared_receipt",
            "preauthorization_receipt",
            "expected_transaction_sha256",
        },
        "abort-release-unit-inputs": common
        | {
            "prepared_receipt",
            "preauthorization_receipt",
            "expected_transaction_sha256",
        },
    }
    if (
        action not in RELEASE_PHASE_ACTIONS
        or not isinstance(value, Mapping)
        or set(value) != phase_fields[action]
        or value.get("schema") != RELEASE_PHASE_REQUEST_SCHEMA
        or value.get("action") != action
        or package.REVISION.fullmatch(
            str(value.get("owner_release_revision", ""))
        )
        is None
        or package.REVISION.fullmatch(
            str(value.get("remote_stager_revision", ""))
        )
        is None
        or not isinstance(value.get("unit_input_publication"), Mapping)
        or not isinstance(value.get("release_update_publication"), Mapping)
        or value["unit_input_publication"].get("release_revision")
        != value["remote_stager_revision"]
        or value["release_update_publication"].get("release_revision")
        != value["remote_stager_revision"]
        or not isinstance(value.get("trusted_predecessor"), Mapping)
        or _SHA256.fullmatch(
            str(value.get("expected_predecessor_trust_sha256", ""))
        )
        is None
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
        or value.get("request_sha256")
        != _sha(_canonical({
            name: item
            for name, item in value.items()
            if name != "request_sha256"
        }))
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_phase_request_invalid"
        )
    publications = (
        value["unit_input_publication"],
        value["release_update_publication"],
    )
    kwargs = {
        "trusted_predecessor": value["trusted_predecessor"],
        "expected_predecessor_trust_sha256": value[
            "expected_predecessor_trust_sha256"
        ],
    }
    if action != "prepare-release-unit-inputs":
        transaction_sha256 = value.get("expected_transaction_sha256")
        if (
            _SHA256.fullmatch(str(transaction_sha256 or "")) is None
            or not isinstance(value.get("prepared_receipt"), Mapping)
        ):
            raise UnitInputRotationError(
                "unit_input_rotation_phase_request_invalid"
            )
        prepared = validate_release_prepared_rotation_receipt(
            value["prepared_receipt"],
            unit_input_publication=publications[0],
            release_update_publication=publications[1],
            **kwargs,
        )
        if prepared["transaction_sha256"] != transaction_sha256:
            raise UnitInputRotationError(
                "unit_input_rotation_phase_request_invalid"
            )
        if action in {
            "finalize-release-unit-inputs",
            "abort-release-unit-inputs",
        }:
            if not isinstance(
                value.get("preauthorization_receipt"), Mapping
            ):
                raise UnitInputRotationError(
                    "unit_input_rotation_phase_request_invalid"
                )
            validate_release_preauthorization_receipt(
                value["preauthorization_receipt"],
                unit_input_publication=publications[0],
                release_update_publication=publications[1],
                prepared_receipt=prepared,
                **kwargs,
            )
    return copy.deepcopy(dict(value))


def validate_release_unit_input_phase_result(
    action: str,
    request: Mapping[str, Any],
    result: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Validate one phase result against the exact immutable request."""

    request = validate_release_unit_input_phase_request(action, request)
    expected_fields = {
        "schema",
        "action",
        "owner_release_revision",
        "remote_stager_revision",
        "request_sha256",
        "transaction_sha256",
        "audit_transaction_path",
        "canonical_receipt",
        "canonical_receipt_sha256",
        "activation_begin",
        "secret_material_recorded",
        "secret_digest_recorded",
        "result_sha256",
    }
    if not isinstance(result, Mapping) or set(result) != expected_fields:
        raise UnitInputRotationError(
            "unit_input_rotation_phase_result_invalid"
        )
    unsigned = {
        name: item
        for name, item in result.items()
        if name != "result_sha256"
    }
    receipt = result.get("canonical_receipt")
    activation_begin = result.get("activation_begin")
    digest_field = (
        "mutation_begin_sha256"
        if action == "preauthorize-release-unit-inputs"
        else "receipt_sha256"
    )
    if (
        result.get("schema") != RELEASE_PHASE_RESULT_SCHEMA
        or result.get("action") != action
        or result.get("owner_release_revision")
        != request["owner_release_revision"]
        or result.get("remote_stager_revision")
        != request["remote_stager_revision"]
        or result.get("request_sha256") != request["request_sha256"]
        or not isinstance(receipt, Mapping)
        or result.get("canonical_receipt_sha256")
        != receipt.get(digest_field)
        or _SHA256.fullmatch(
            str(result.get("canonical_receipt_sha256", ""))
        )
        is None
        or result.get("secret_material_recorded") is not False
        or result.get("secret_digest_recorded") is not False
        or result.get("result_sha256") != _sha(_canonical(unsigned))
        or (
            action != "prepare-release-unit-inputs"
            and result.get("transaction_sha256")
            != request["expected_transaction_sha256"]
        )
        or (
            action == "finalize-release-unit-inputs"
            and not isinstance(activation_begin, Mapping)
        )
        or (
            action != "finalize-release-unit-inputs"
            and activation_begin is not None
        )
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_phase_result_invalid"
        )
    publications = (
        request["unit_input_publication"],
        request["release_update_publication"],
    )
    kwargs = {
        "trusted_predecessor": request["trusted_predecessor"],
        "expected_predecessor_trust_sha256": request[
            "expected_predecessor_trust_sha256"
        ],
    }
    prepared = (
        validate_release_prepared_rotation_receipt(
            receipt,
            unit_input_publication=publications[0],
            release_update_publication=publications[1],
            **kwargs,
        )
        if action == "prepare-release-unit-inputs"
        else validate_release_prepared_rotation_receipt(
            request["prepared_receipt"],
            unit_input_publication=publications[0],
            release_update_publication=publications[1],
            **kwargs,
        )
    )
    if action == "prepare-release-unit-inputs":
        validated_receipt = prepared
    elif action == "preauthorize-release-unit-inputs":
        validated_receipt = validate_release_preauthorization_receipt(
            receipt,
            unit_input_publication=publications[0],
            release_update_publication=publications[1],
            prepared_receipt=prepared,
            **kwargs,
        )
    else:
        preauthorization = validate_release_preauthorization_receipt(
            request["preauthorization_receipt"],
            unit_input_publication=publications[0],
            release_update_publication=publications[1],
            prepared_receipt=prepared,
            **kwargs,
        )
        if action == "abort-release-unit-inputs":
            validated_receipt = validate_release_rotation_abort_receipt(
                receipt,
                unit_input_publication=publications[0],
                release_update_publication=publications[1],
                prepared_receipt=prepared,
                preauthorization_receipt=preauthorization,
                **kwargs,
            )
        else:
            validated_receipt = validate_release_rotation_receipt(
                receipt,
                unit_input_publication=publications[0],
                release_update_publication=publications[1],
                prepared_receipt=prepared,
                mutation_begin=preauthorization,
                activation_begin=activation_begin,
                **kwargs,
            )
    if (
        validated_receipt != receipt
        or result.get("transaction_sha256")
        != prepared["transaction_sha256"]
        or result.get("transaction_sha256")
        != receipt.get("transaction_sha256")
        or result.get("audit_transaction_path")
        != prepared["audit_transaction_path"]
        or (
            receipt.get("audit_transaction_path") is not None
            and result.get("audit_transaction_path")
            != receipt["audit_transaction_path"]
        )
        or (
            action == "finalize-release-unit-inputs"
            and receipt.get("activation_begin_sha256")
            != activation_begin.get("activation_begin_sha256")
        )
    ):
        raise UnitInputRotationError(
            "unit_input_rotation_phase_result_invalid"
        )
    return copy.deepcopy(dict(result))


def execute_release_unit_input_phase(
    action: str,
    value: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Dispatch one exact root-only release rotation phase.

    The outer transport supplies only canonical public documents.  This
    function deliberately exposes no clock, lock, path, or identity override;
    all mutation authority remains in the four production entrypoints above.
    """

    value = validate_release_unit_input_phase_request(action, value)
    kwargs = {
        "trusted_predecessor": value["trusted_predecessor"],
        "expected_predecessor_trust_sha256": value[
            "expected_predecessor_trust_sha256"
        ],
    }
    publications = (
        value["unit_input_publication"],
        value["release_update_publication"],
    )
    if action == "prepare-release-unit-inputs":
        receipt = prepare_release_unit_input_authority_rotation(
            *publications,
            **kwargs,
        )
        prepared = receipt
    else:
        transaction_sha256 = value.get("expected_transaction_sha256")
        if _SHA256.fullmatch(str(transaction_sha256 or "")) is None:
            raise UnitInputRotationError(
                "unit_input_rotation_phase_request_invalid"
            )
        if not isinstance(value.get("prepared_receipt"), Mapping):
            raise UnitInputRotationError(
                "unit_input_rotation_phase_request_invalid"
            )
        prepared = validate_release_prepared_rotation_receipt(
            value["prepared_receipt"],
            unit_input_publication=publications[0],
            release_update_publication=publications[1],
            **kwargs,
        )
        if action == "preauthorize-release-unit-inputs":
            receipt = (
                preauthorize_prepared_release_unit_input_authority_rotation(
                    *publications,
                    prepared,
                    **kwargs,
                    expected_transaction_sha256=transaction_sha256,
                )
            )
        else:
            if not isinstance(
                value.get("preauthorization_receipt"), Mapping
            ):
                raise UnitInputRotationError(
                    "unit_input_rotation_phase_request_invalid"
                )
            operation = (
                finalize_preauthorized_release_unit_input_authority_rotation
                if action == "finalize-release-unit-inputs"
                else abort_preauthorized_release_unit_input_authority_rotation
            )
            receipt = operation(
                *publications,
                prepared,
                value["preauthorization_receipt"],
                **kwargs,
                expected_transaction_sha256=transaction_sha256,
            )

    activation_begin = None
    if action == "finalize-release-unit-inputs":
        transaction = _release_receipt_transaction(prepared)
        successor = _release_successor(
            publications[0],
            publications[1],
            value["trusted_predecessor"],
            expected_predecessor_trust_sha256=value[
                "expected_predecessor_trust_sha256"
            ],
            now_unix=transaction["authorization_checked_at_unix"],
        )
        activation_begin = _load_release_activation_begin(
            Path(prepared["audit_transaction_path"]),
            transaction=transaction,
            successor=successor,
            preauthorization_receipt=value["preauthorization_receipt"],
            uid=0,
            gid=0,
        )
        if activation_begin is None:
            raise UnitInputRotationError(
                "unit_input_rotation_activation_begin_invalid"
            )
        validate_release_rotation_receipt(
            receipt,
            unit_input_publication=publications[0],
            release_update_publication=publications[1],
            trusted_predecessor=value["trusted_predecessor"],
            expected_predecessor_trust_sha256=value[
                "expected_predecessor_trust_sha256"
            ],
            prepared_receipt=prepared,
            mutation_begin=value["preauthorization_receipt"],
            activation_begin=activation_begin,
        )
    receipt_digest = (
        receipt["mutation_begin_sha256"]
        if action == "preauthorize-release-unit-inputs"
        else receipt["receipt_sha256"]
    )
    unsigned = {
        "schema": RELEASE_PHASE_RESULT_SCHEMA,
        "action": action,
        "owner_release_revision": value["owner_release_revision"],
        "remote_stager_revision": value["remote_stager_revision"],
        "request_sha256": value["request_sha256"],
        "transaction_sha256": prepared["transaction_sha256"],
        "audit_transaction_path": prepared["audit_transaction_path"],
        "canonical_receipt": receipt,
        "canonical_receipt_sha256": receipt_digest,
        "activation_begin": activation_begin,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    result = {
        **unsigned,
        "result_sha256": _sha(_canonical(unsigned)),
    }
    return validate_release_unit_input_phase_result(action, value, result)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rotate one fixed owner-signed unit-input authority",
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=tuple(sorted(RELEASE_PHASE_ACTIONS)),
    )
    arguments = parser.parse_args(argv)
    raw = sys.stdin.buffer.read(public_stager.MAX_INPUT + 1)
    try:
        if not raw or len(raw) > public_stager.MAX_INPUT or raw.endswith(b"\n"):
            raise UnitInputRotationError("unit_input_rotation_input_invalid")
        decoded = _decode(raw)
        receipt = (
            rotate_unit_input_authority(decoded)
            if arguments.action is None
            else execute_release_unit_input_phase(arguments.action, decoded)
        )
    except (OSError, UnitInputRotationError):
        print(
            '{"error_code":"unit_input_rotation_failed","ok":false}',
            file=sys.stderr,
        )
        return 2
    print(_canonical(receipt).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
