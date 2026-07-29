#!/usr/bin/env python3
"""Rotate the fixed production unit-input authority without ad-hoc deletion.

The existing public stager and fixed-input bootstrap are deliberately
create-only.  This root-only edge transaction archives one complete,
cryptographically valid predecessor triplet, stages one owner-signed
successor plan and approval, derives the exact successor fixed-input document,
and publishes a terminal receipt only after all three live files agree.  The
existing bootstrap can subsequently exact-replay this already complete state.

The transaction is identified by the predecessor plan and successor
publication digests.  Every durable file is create-or-exact-resume, so the
same successor can finish an interrupted rotation while a different successor
fails closed.
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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from gateway import canonical_writer_production_cutover as cutover
from scripts.canary import package_production_cutover_artifacts as package
from scripts.canary import production_cutover_activation_lock as authority_lock
from scripts.canary import production_cutover_public_stager as public_stager


TRANSACTION_SCHEMA = "muncho-production-unit-input-rotation-transaction.v1"
RECEIPT_SCHEMA = "muncho-production-unit-input-rotation-receipt.v1"
AUDIT_DIRECTORY_NAME = "unit-input-authority-rotations"
TRANSACTION_FILE_NAME = "transaction.json"
PUBLICATION_FILE_NAME = "successor-publication.json"
RECEIPT_FILE_NAME = "rotation-receipt.json"
PREDECESSOR_DIRECTORY_NAME = "predecessor"
PRODUCTION_STAGED_ROOT = Path("/var/lib/muncho-production-legacy-cutover/staged")
MAX_FILE = 16 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TRANSACTION_DIRECTORY = re.compile(r"^[0-9a-f]{64}-[0-9a-f]{64}$")
_TEMPORARY_SUFFIX = re.compile(r"^[1-9][0-9]*$")
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_MAX_TEMPORARY_ALIASES = 64
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


def _recover_temporary_aliases(
    path: Path,
    payload: bytes,
    *,
    uid: int,
    gid: int,
    mode: int,
) -> Path:
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
    return candidates[0]


def _rename_noreplace(source: Path, destination: Path) -> bool:
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
            _AT_FDCWD,
            os.fsencode(source),
            _AT_FDCWD,
            os.fsencode(destination),
            _RENAME_NOREPLACE,
        )
        if result == 0:
            return True
        number = ctypes.get_errno()
        if number == errno.EEXIST:
            return False
        raise OSError(number, os.strerror(number), destination)
    try:
        os.link(source, destination, follow_symlinks=False)
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
    temporary_identity: tuple[int, int] | None = None
    try:
        if not os.path.lexists(path):
            if not os.path.lexists(temporary):
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                flags |= getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(temporary, flags, 0o600)
                opened = os.fstat(descriptor)
                temporary_identity = (opened.st_dev, opened.st_ino)
                os.fchown(descriptor, uid, gid)
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short unit-input rotation write")
                    view = view[written:]
                os.fchmod(descriptor, mode)
                os.fsync(descriptor)
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
            created = _rename_noreplace(temporary, path)
            if os.path.lexists(temporary):
                current = temporary.lstat()
                if (current.st_dev, current.st_ino) != temporary_identity:
                    raise UnitInputRotationError("unit_input_rotation_conflict")
                temporary.unlink()
            cutover.activation._fsync_directory(path.parent)
        observed = _read_exact(
            path,
            uid=uid,
            gid=gid,
            mode=mode,
            maximum=max(MAX_FILE, len(payload)),
        )
        if observed != payload:
            raise UnitInputRotationError("unit_input_rotation_conflict")
    except UnitInputRotationError:
        raise
    except OSError as exc:
        raise UnitInputRotationError("unit_input_rotation_write_failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            current = temporary.lstat()
            if temporary_identity is not None and (
                current.st_dev,
                current.st_ino,
            ) == temporary_identity:
                temporary.unlink()
        except (FileNotFoundError, OSError):
            pass
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
        plan = package.validate_unit_input_plan(_decode(plan_raw))
        approval = _validate_approval_without_lease(
            _decode(approval_raw),
            plan=plan,
        )
        fixed_inputs = package._unit_inputs_from_authority(plan, approval)
    except (
        PermissionError,
        TypeError,
        ValueError,
        package.PackagingError,
    ) as exc:
        raise UnitInputRotationError("unit_input_rotation_predecessor_invalid") from exc
    if fixed_raw != _canonical(fixed_inputs) + b"\n":
        raise UnitInputRotationError("unit_input_rotation_predecessor_invalid")
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
    if receipt != _receipt(path, transaction):
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
        plan = package.validate_unit_input_plan(_decode(plan_raw))
        approval = _validate_approval_without_lease(
            _decode(approval_raw),
            plan=plan,
        )
        fixed_inputs = package._unit_inputs_from_authority(plan, approval)
    except (
        PermissionError,
        TypeError,
        ValueError,
        package.PackagingError,
    ) as exc:
        raise UnitInputRotationError("unit_input_rotation_audit_invalid") from exc
    if (
        transaction["predecessor_revision"] != plan["release_revision"]
        or transaction["predecessor_plan_sha256"] != plan["plan_sha256"]
        or transaction["predecessor_approval_sha256"] != approval["approval_sha256"]
        or transaction["predecessor_fixed_inputs_sha256"] != _sha(fixed_raw)
        or fixed_raw != _canonical(fixed_inputs) + b"\n"
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
    successor: _Successor,
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


def _live_successor_complete(
    successor: _Successor,
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
    predecessor: _AuthorityTriplet,
    successor: _Successor,
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


def _publish_receipt(
    transaction_path: Path,
    transaction: Mapping[str, Any],
    successor: _Successor,
    *,
    uid: int,
    gid: int,
) -> Mapping[str, Any]:
    _validate_live_successor(successor, uid=uid, gid=gid)
    receipt = _receipt(transaction_path, transaction)
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
    if not isinstance(value, Mapping) or set(value) != _RECEIPT_FIELDS:
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
        value.get("schema") != RECEIPT_SCHEMA
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
    uid = 0 if require_root else os.geteuid()
    gid = 0 if require_root else os.getegid()
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
            if _live_successor_complete(successor, uid=uid, gid=gid):
                return _publish_receipt(
                    transaction_path,
                    transaction,
                    successor,
                    uid=uid,
                    gid=gid,
                )
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rotate one fixed owner-signed unit-input authority",
    )
    parser.parse_args(argv)
    raw = sys.stdin.buffer.read(public_stager.MAX_INPUT + 1)
    try:
        if not raw or len(raw) > public_stager.MAX_INPUT or raw.endswith(b"\n"):
            raise UnitInputRotationError("unit_input_rotation_input_invalid")
        receipt = rotate_unit_input_authority(_decode(raw))
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
