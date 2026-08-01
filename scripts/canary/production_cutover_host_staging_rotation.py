#!/usr/bin/env python3
"""Crash-safe successor rotation for inert production host staging.

The host artifact producer is deliberately create-only.  This companion moves
one fully attested predecessor set into a fixed, digest-addressed archive while
holding the production activation lease, then invokes the exact successor
release producer.  It never accepts caller-selected paths, artifact bytes, or
service actions, and it refuses to run after freeze/cutover authority exists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from gateway import canonical_writer_production_cutover as cutover
from scripts.canary import package_production_cutover_artifacts as package
from scripts.canary import production_cutover_activation_lock as authority_lock
from scripts.canary import production_cutover_host_plan as host_plan


ROTATION_SCHEMA = "muncho-production-host-staging-rotation-intent.v1"
RECEIPT_SCHEMA = "muncho-production-host-staging-rotation-receipt.v1"
ROTATION_ROOT = cutover.EVIDENCE_ROOT / "host-staging-rotations"
HOST_STAGING_ROOT = host_plan.HOST_STAGING_ROOT
STAGING_RECEIPT_PATH = host_plan.STAGING_RECEIPT_PATH
MAX_FILE = 4 * 1024 * 1024
MAX_RECEIPT = 16 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class HostStagingRotationError(RuntimeError):
    """Stable, secret-free host-staging rotation failure."""


def _effective_identity() -> tuple[int, int] | None:
    geteuid = getattr(os, "geteuid", None)
    getegid = getattr(os, "getegid", None)
    if not callable(geteuid) or not callable(getegid):
        return None
    return int(geteuid()), int(getegid())


def _canonical(value: Any) -> bytes:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise HostStagingRotationError(
            "host_staging_rotation_json_invalid"
        ) from exc
    if len(raw) > MAX_RECEIPT:
        raise HostStagingRotationError("host_staging_rotation_json_oversized")
    return raw


def _decode(raw: bytes) -> Mapping[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, item in items:
            if name in result:
                raise HostStagingRotationError(
                    "host_staging_rotation_json_duplicate_key"
                )
            result[name] = item
        return result

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except HostStagingRotationError:
        raise
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise HostStagingRotationError(
            "host_staging_rotation_json_invalid"
        ) from exc
    if not isinstance(value, Mapping) or raw != _canonical(value):
        raise HostStagingRotationError(
            "host_staging_rotation_json_not_canonical"
        )
    return value


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _physical(logical: Path, filesystem_root: Path) -> Path:
    if not logical.is_absolute() or ".." in logical.parts:
        raise HostStagingRotationError("host_staging_rotation_path_invalid")
    if filesystem_root == Path("/"):
        return logical
    try:
        root = filesystem_root.resolve(strict=True)
    except OSError as exc:
        raise HostStagingRotationError(
            "host_staging_rotation_test_root_invalid"
        ) from exc
    return root.joinpath(*logical.parts[1:])


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


def _read_regular(
    logical: Path,
    *,
    filesystem_root: Path,
    uid: int,
    gid: int,
    mode: int,
    maximum: int,
) -> bytes:
    path = _physical(logical, filesystem_root)
    descriptor: int | None = None
    try:
        before = os.lstat(path)
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != uid
            or before.st_gid != gid
            or stat.S_IMODE(before.st_mode) != mode
            or not 0 < before.st_size <= maximum
        ):
            raise HostStagingRotationError(
                "host_staging_rotation_file_identity_invalid"
            )
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if _identity(before) != _identity(opened):
            raise HostStagingRotationError(
                "host_staging_rotation_file_changed"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, maximum + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise HostStagingRotationError(
                    "host_staging_rotation_file_oversized"
                )
            chunks.append(chunk)
        after = os.fstat(descriptor)
        reached = os.lstat(path)
        if _identity(opened) != _identity(after) or _identity(after) != _identity(reached):
            raise HostStagingRotationError(
                "host_staging_rotation_file_changed"
            )
        return b"".join(chunks)
    except HostStagingRotationError:
        raise
    except OSError as exc:
        raise HostStagingRotationError(
            "host_staging_rotation_file_unavailable"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _ensure_private_directory(
    logical: Path,
    *,
    filesystem_root: Path,
    uid: int,
    gid: int,
) -> None:
    path = _physical(logical, filesystem_root)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise HostStagingRotationError(
            "host_staging_rotation_directory_invalid"
        ) from exc
    try:
        observed = os.lstat(path)
    except OSError as exc:
        raise HostStagingRotationError(
            "host_staging_rotation_directory_invalid"
        ) from exc
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != uid
        or observed.st_gid != gid
        or stat.S_IMODE(observed.st_mode) != 0o700
    ):
        raise HostStagingRotationError(
            "host_staging_rotation_directory_invalid"
        )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _create_only(
    logical: Path,
    payload: bytes,
    *,
    filesystem_root: Path,
    uid: int,
    gid: int,
) -> None:
    path = _physical(logical, filesystem_root)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    created = False
    try:
        try:
            descriptor = os.open(path, flags, 0o400)
            created = True
        except FileExistsError:
            observed = _read_regular(
                logical,
                filesystem_root=filesystem_root,
                uid=uid,
                gid=gid,
                mode=0o400,
                maximum=MAX_RECEIPT,
            )
            if observed != payload:
                raise HostStagingRotationError(
                    "host_staging_rotation_conflict"
                )
            return
        os.fchown(descriptor, uid, gid)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short host staging rotation write")
            view = view[written:]
        os.fchmod(descriptor, 0o400)
        os.fsync(descriptor)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        if created:
            try:
                os.unlink(path)
            except OSError:
                pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
    _fsync_directory(path.parent)


def _validate_staging_snapshot(
    receipt: Mapping[str, Any],
    *,
    host_root: Path,
    receipt_path: Path,
    filesystem_root: Path,
    uid: int,
    gid: int,
) -> Mapping[str, Any]:
    unsigned = {
        name: item for name, item in receipt.items() if name != "receipt_sha256"
    }
    rows = receipt.get("staged_files")
    revision = receipt.get("release_revision")
    if (
        receipt.get("schema") != host_plan.STAGING_SCHEMA
        or package.REVISION.fullmatch(str(revision or "")) is None
        or not isinstance(rows, Mapping)
        or not rows
        or receipt.get("staged_file_count") != len(rows)
        or receipt.get("secret_material_recorded") is not False
        or receipt.get("secret_digest_recorded") is not False
        or receipt.get("staged_set_sha256")
        != _sha(_canonical({"files": rows}))
        or receipt.get("receipt_sha256") != _sha(_canonical(unsigned))
    ):
        raise HostStagingRotationError(
            "host_staging_rotation_receipt_invalid"
        )
    root = _physical(host_root, filesystem_root)
    try:
        root_state = os.lstat(root)
        entries = list(os.scandir(root))
    except OSError as exc:
        raise HostStagingRotationError(
            "host_staging_rotation_host_set_invalid"
        ) from exc
    if (
        stat.S_ISLNK(root_state.st_mode)
        or not stat.S_ISDIR(root_state.st_mode)
        or root_state.st_uid != uid
        or root_state.st_gid != gid
        or stat.S_IMODE(root_state.st_mode) != 0o700
    ):
        raise HostStagingRotationError(
            "host_staging_rotation_host_set_invalid"
        )
    expected_names: set[str] = set()
    for name, row in rows.items():
        if not isinstance(name, str) or not isinstance(row, Mapping):
            raise HostStagingRotationError(
                "host_staging_rotation_receipt_invalid"
            )
        expected_fields = {
            "sha256", "size", "staged_gid", "staged_mode", "staged_path",
            "staged_uid", "target_path",
        }
        staged_path = Path(str(row.get("staged_path", "")))
        if (
            set(row) != expected_fields
            or staged_path.parent != HOST_STAGING_ROOT
            or staged_path.name in expected_names
            or row.get("staged_uid") != uid
            or row.get("staged_gid") != gid
            or row.get("staged_mode") != 0o400
            or type(row.get("size")) is not int
            or not 0 < row["size"] <= MAX_FILE
            or _SHA256.fullmatch(str(row.get("sha256", ""))) is None
            or not Path(str(row.get("target_path", ""))).is_absolute()
        ):
            raise HostStagingRotationError(
                "host_staging_rotation_receipt_invalid"
            )
        expected_names.add(staged_path.name)
        archived_logical = host_root / staged_path.name
        raw = _read_regular(
            archived_logical,
            filesystem_root=filesystem_root,
            uid=uid,
            gid=gid,
            mode=0o400,
            maximum=MAX_FILE,
        )
        if len(raw) != row["size"] or _sha(raw) != row["sha256"]:
            raise HostStagingRotationError(
                "host_staging_rotation_host_set_invalid"
            )
    observed_names = {entry.name for entry in entries}
    if observed_names != expected_names or any(
        entry.is_symlink() or not entry.is_file(follow_symlinks=False)
        for entry in entries
    ):
        raise HostStagingRotationError(
            "host_staging_rotation_host_set_invalid"
        )
    receipt_raw = _read_regular(
        receipt_path,
        filesystem_root=filesystem_root,
        uid=uid,
        gid=gid,
        mode=0o400,
        maximum=MAX_RECEIPT,
    )
    if receipt_raw != _canonical(receipt):
        raise HostStagingRotationError(
            "host_staging_rotation_receipt_invalid"
        )
    return dict(receipt)


def _require_pre_freeze(*, filesystem_root: Path) -> None:
    for logical in (
        cutover.STAGED_FREEZE_PLAN_PATH,
        cutover.STAGED_FREEZE_APPROVAL_PATH,
        cutover.STAGED_CUTOVER_PLAN_PATH,
    ):
        if os.path.lexists(_physical(logical, filesystem_root)):
            raise HostStagingRotationError(
                "host_staging_rotation_after_freeze_forbidden"
            )


def _rotation_id(predecessor_sha256: str, successor_revision: str) -> str:
    return _sha(_canonical({
        "predecessor_receipt_sha256": predecessor_sha256,
        "successor_release_revision": successor_revision,
    }))


def _transaction_paths(rotation_id: str) -> Mapping[str, Path]:
    if _SHA256.fullmatch(rotation_id or "") is None:
        raise HostStagingRotationError("host_staging_rotation_id_invalid")
    root = ROTATION_ROOT / rotation_id
    return {
        "root": root,
        "intent": root / "intent.json",
        "predecessor_host": root / "predecessor-host",
        "predecessor_receipt": root / "predecessor-receipt.json",
        "receipt": root / "rotation-receipt.json",
    }


def _rename_exact(source: Path, target: Path, *, filesystem_root: Path) -> None:
    physical_source = _physical(source, filesystem_root)
    physical_target = _physical(target, filesystem_root)
    try:
        os.rename(physical_source, physical_target)
        _fsync_directory(physical_source.parent)
        if physical_target.parent != physical_source.parent:
            _fsync_directory(physical_target.parent)
    except OSError as exc:
        raise HostStagingRotationError(
            "host_staging_rotation_rename_failed"
        ) from exc


def _find_resumable_transaction(
    successor_revision: str,
    *,
    filesystem_root: Path,
    uid: int,
    gid: int,
) -> tuple[Mapping[str, Any], Mapping[str, Path]] | None:
    root = _physical(ROTATION_ROOT, filesystem_root)
    if not root.exists():
        return None
    candidates: list[tuple[Mapping[str, Any], Mapping[str, Path]]] = []
    try:
        entries = list(os.scandir(root))
    except OSError as exc:
        raise HostStagingRotationError(
            "host_staging_rotation_directory_invalid"
        ) from exc
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False) or _SHA256.fullmatch(entry.name) is None:
            raise HostStagingRotationError(
                "host_staging_rotation_directory_invalid"
            )
        paths = _transaction_paths(entry.name)
        intent_path = _physical(paths["intent"], filesystem_root)
        if not intent_path.exists():
            raise HostStagingRotationError(
                "host_staging_rotation_transaction_invalid"
            )
        intent = _decode(_read_regular(
            paths["intent"], filesystem_root=filesystem_root, uid=uid, gid=gid,
            mode=0o400, maximum=MAX_RECEIPT,
        ))
        unsigned = {name: item for name, item in intent.items() if name != "intent_sha256"}
        if (
            intent.get("schema") != ROTATION_SCHEMA
            or intent.get("rotation_id") != entry.name
            or intent.get("intent_sha256") != _sha(_canonical(unsigned))
        ):
            raise HostStagingRotationError(
                "host_staging_rotation_transaction_invalid"
            )
        if intent.get("successor_release_revision") == successor_revision:
            candidates.append((intent, paths))
    if len(candidates) > 1:
        raise HostStagingRotationError(
            "host_staging_rotation_successor_ambiguous"
        )
    return candidates[0] if candidates else None


def _build_rotation_receipt(
    *,
    intent: Mapping[str, Any],
    predecessor: Mapping[str, Any],
    successor: Mapping[str, Any],
) -> Mapping[str, Any]:
    unsigned = {
        "schema": RECEIPT_SCHEMA,
        "rotation_id": intent["rotation_id"],
        "intent_sha256": intent["intent_sha256"],
        "predecessor_release_revision": predecessor["release_revision"],
        "predecessor_staging_receipt_sha256": predecessor["receipt_sha256"],
        "successor_release_revision": successor["release_revision"],
        "successor_staging_receipt_sha256": successor["receipt_sha256"],
        "predecessor_preserved": True,
        "successor_readback_verified": True,
        "production_service_mutation_performed": False,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {**unsigned, "receipt_sha256": _sha(_canonical(unsigned))}


def _rotate_locked(
    successor_revision: str,
    *,
    filesystem_root: Path,
    require_root: bool,
    stage_successor: Callable[[str], Mapping[str, Any]],
) -> Mapping[str, Any]:
    if package.REVISION.fullmatch(successor_revision or "") is None:
        raise HostStagingRotationError(
            "host_staging_rotation_revision_invalid"
        )
    identity = _effective_identity()
    if require_root and (
        not sys.platform.startswith("linux")
        or identity != (0, 0)
        or filesystem_root != Path("/")
    ):
        raise HostStagingRotationError(
            "host_staging_rotation_requires_linux_root"
        )
    if identity is None:
        raise HostStagingRotationError(
            "host_staging_rotation_posix_identity_unavailable"
        )
    uid, gid = (0, 0) if require_root else identity
    _require_pre_freeze(filesystem_root=filesystem_root)
    for directory in (
        ROTATION_ROOT.parent,
        ROTATION_ROOT,
    ):
        _ensure_private_directory(
            directory, filesystem_root=filesystem_root, uid=uid, gid=gid
        )

    live_receipt_path = _physical(STAGING_RECEIPT_PATH, filesystem_root)
    live_host_path = _physical(HOST_STAGING_ROOT, filesystem_root)
    transaction = _find_resumable_transaction(
        successor_revision,
        filesystem_root=filesystem_root,
        uid=uid,
        gid=gid,
    )
    if live_receipt_path.exists():
        live_receipt = _decode(_read_regular(
            STAGING_RECEIPT_PATH,
            filesystem_root=filesystem_root,
            uid=uid,
            gid=gid,
            mode=0o400,
            maximum=MAX_RECEIPT,
        ))
        if live_receipt.get("release_revision") == successor_revision:
            validated = _validate_staging_snapshot(
                live_receipt,
                host_root=HOST_STAGING_ROOT,
                receipt_path=STAGING_RECEIPT_PATH,
                filesystem_root=filesystem_root,
                uid=uid,
                gid=gid,
            )
            if transaction is not None:
                intent, paths = transaction
                predecessor = _decode(_read_regular(
                    paths["predecessor_receipt"],
                    filesystem_root=filesystem_root,
                    uid=uid,
                    gid=gid,
                    mode=0o400,
                    maximum=MAX_RECEIPT,
                ))
                predecessor = _validate_staging_snapshot(
                    predecessor,
                    host_root=paths["predecessor_host"],
                    receipt_path=paths["predecessor_receipt"],
                    filesystem_root=filesystem_root,
                    uid=uid,
                    gid=gid,
                )
                if (
                    predecessor.get("receipt_sha256")
                    != intent.get("predecessor_receipt_sha256")
                    or predecessor.get("release_revision")
                    != intent.get("predecessor_release_revision")
                ):
                    raise HostStagingRotationError(
                        "host_staging_rotation_predecessor_archive_invalid"
                    )
                receipt = _build_rotation_receipt(
                    intent=intent,
                    predecessor=predecessor,
                    successor=validated,
                )
                _create_only(
                    paths["receipt"],
                    _canonical(receipt),
                    filesystem_root=filesystem_root,
                    uid=uid,
                    gid=gid,
                )
                return receipt
            return {
                "schema": RECEIPT_SCHEMA,
                "successor_release_revision": successor_revision,
                "successor_staging_receipt_sha256": validated["receipt_sha256"],
                "already_current": True,
                "predecessor_preserved": transaction is not None,
                "production_service_mutation_performed": False,
                "secret_material_recorded": False,
                "secret_digest_recorded": False,
            }
        if transaction is not None:
            intent, paths = transaction
            if (
                live_receipt.get("receipt_sha256")
                != intent.get("predecessor_receipt_sha256")
                or live_receipt.get("release_revision")
                != intent.get("predecessor_release_revision")
            ):
                raise HostStagingRotationError(
                    "host_staging_rotation_successor_conflict"
                )
            predecessor = live_receipt
        else:
            predecessor = _validate_staging_snapshot(
                live_receipt,
                host_root=HOST_STAGING_ROOT,
                receipt_path=STAGING_RECEIPT_PATH,
                filesystem_root=filesystem_root,
                uid=uid,
                gid=gid,
            )
        if transaction is None:
            rotation_id = _rotation_id(
                str(predecessor["receipt_sha256"]), successor_revision
            )
            paths = _transaction_paths(rotation_id)
            _ensure_private_directory(
                paths["root"], filesystem_root=filesystem_root, uid=uid, gid=gid
            )
            unsigned_intent = {
                "schema": ROTATION_SCHEMA,
                "rotation_id": rotation_id,
                "predecessor_release_revision": predecessor["release_revision"],
                "predecessor_receipt_sha256": predecessor["receipt_sha256"],
                "successor_release_revision": successor_revision,
                "host_staging_path": str(HOST_STAGING_ROOT),
                "staging_receipt_path": str(STAGING_RECEIPT_PATH),
                "predecessor_archive_path": str(paths["predecessor_host"]),
                "predecessor_receipt_archive_path": str(paths["predecessor_receipt"]),
                "production_service_mutation_authorized": False,
            }
            intent = {
                **unsigned_intent,
                "intent_sha256": _sha(_canonical(unsigned_intent)),
            }
            _create_only(
                paths["intent"], _canonical(intent), filesystem_root=filesystem_root,
                uid=uid, gid=gid,
            )
            transaction = (intent, paths)
    elif transaction is None:
        raise HostStagingRotationError(
            "host_staging_rotation_predecessor_unavailable"
        )

    intent, paths = transaction
    archived_host = _physical(paths["predecessor_host"], filesystem_root)
    archived_receipt = _physical(paths["predecessor_receipt"], filesystem_root)
    if live_host_path.exists() and not archived_host.exists():
        _rename_exact(
            HOST_STAGING_ROOT,
            paths["predecessor_host"],
            filesystem_root=filesystem_root,
        )
    if live_receipt_path.exists() and not archived_receipt.exists():
        _rename_exact(
            STAGING_RECEIPT_PATH,
            paths["predecessor_receipt"],
            filesystem_root=filesystem_root,
        )
    if not archived_host.exists() or not archived_receipt.exists():
        raise HostStagingRotationError(
            "host_staging_rotation_predecessor_archive_invalid"
        )
    predecessor = _decode(_read_regular(
        paths["predecessor_receipt"],
        filesystem_root=filesystem_root,
        uid=uid,
        gid=gid,
        mode=0o400,
        maximum=MAX_RECEIPT,
    ))
    _validate_staging_snapshot(
        predecessor,
        host_root=paths["predecessor_host"],
        receipt_path=paths["predecessor_receipt"],
        filesystem_root=filesystem_root,
        uid=uid,
        gid=gid,
    )
    if (
        predecessor.get("receipt_sha256")
        != intent.get("predecessor_receipt_sha256")
        or predecessor.get("release_revision")
        != intent.get("predecessor_release_revision")
    ):
        raise HostStagingRotationError(
            "host_staging_rotation_predecessor_archive_invalid"
        )

    try:
        produced = stage_successor(successor_revision)
    except HostStagingRotationError:
        raise
    except Exception as exc:
        raise HostStagingRotationError(
            "host_staging_rotation_successor_staging_failed"
        ) from exc
    if not isinstance(produced, Mapping):
        raise HostStagingRotationError(
            "host_staging_rotation_successor_staging_failed"
        )
    successor = _decode(_read_regular(
        STAGING_RECEIPT_PATH,
        filesystem_root=filesystem_root,
        uid=uid,
        gid=gid,
        mode=0o400,
        maximum=MAX_RECEIPT,
    ))
    successor = _validate_staging_snapshot(
        successor,
        host_root=HOST_STAGING_ROOT,
        receipt_path=STAGING_RECEIPT_PATH,
        filesystem_root=filesystem_root,
        uid=uid,
        gid=gid,
    )
    if (
        successor.get("release_revision") != successor_revision
        or dict(produced) != successor
    ):
        raise HostStagingRotationError(
            "host_staging_rotation_successor_receipt_invalid"
        )
    receipt = _build_rotation_receipt(
        intent=intent,
        predecessor=predecessor,
        successor=successor,
    )
    _create_only(
        paths["receipt"], _canonical(receipt), filesystem_root=filesystem_root,
        uid=uid, gid=gid,
    )
    return receipt


def rotate_host_staging(
    successor_revision: str,
    *,
    filesystem_root: Path = Path("/"),
    require_root: bool = True,
    lock_factory: Callable[[], Any] | None = None,
    stage_successor: Callable[[str], Mapping[str, Any]] | None = None,
) -> Mapping[str, Any]:
    """Rotate one exact inert predecessor into an exact successor set."""

    if stage_successor is None:
        stage_successor = host_plan._stage_fixed_host_artifacts_locked
    try:
        with authority_lock.authority_activation_lock(
            require_root=require_root,
            lock_factory=lock_factory,
        ):
            return _rotate_locked(
                successor_revision,
                filesystem_root=filesystem_root,
                require_root=require_root,
                stage_successor=stage_successor,
            )
    except authority_lock.AuthorityActivationLockError as exc:
        raise HostStagingRotationError(
            "host_staging_rotation_activation_lock_unavailable"
        ) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rotate", choices=("rotate",))
    parser.add_argument("--revision", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if not sys.stdin.isatty() and sys.stdin.buffer.read(1):
            raise HostStagingRotationError(
                "host_staging_rotation_input_forbidden"
            )
        result = rotate_host_staging(args.revision)
    except HostStagingRotationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(_canonical(result).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "HostStagingRotationError",
    "RECEIPT_SCHEMA",
    "ROTATION_ROOT",
    "ROTATION_SCHEMA",
    "main",
    "rotate_host_staging",
]
