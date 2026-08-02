#!/usr/bin/env python3
"""Two-identity, create-only successor-rebind owner-runtime publication.

An unprivileged builder creates the sealed runtime under a root-created,
builder-owned staging directory.  A root process never runs that builder or
imports the staged wheel: the revision-qualified stdlib-only verifier checks
the complete staged manifest/tree, then this module rebases only canonical
manifest data, chowns/seals, and atomically renames the tree into its fixed
final path.  Existing-different final state is always refused.
"""

from __future__ import annotations

import copy
import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Never

from gateway import production_owner_runtime as runtime
from scripts.canary import package_production_owner_runtime as package
from scripts.canary import production_successor_rebind_owner_runtime_preexec as preexec


STAGING_SCHEMA = "muncho-successor-rebind-owner-runtime-staging.v2"
PUBLICATION_SCHEMA = "muncho-successor-rebind-owner-runtime-publication.v2"
RELEASE_BASE = Path("/usr/lib/muncho-successor-rebind-runtime")
PUBLICATION_ROOT = Path("/var/lib/muncho-successor-rebind-runtime-publications")
STAGING_NAME = "staging-publication.json"
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class SuccessorRebindOwnerRuntimeError(RuntimeError):
    """Stable, secret-free fixed-runtime publication failure."""


def _fail(code: str, _cause: BaseException | None = None) -> Never:
    raise SuccessorRebindOwnerRuntimeError(code) from None


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
        _fail("successor_rebind_owner_runtime_json_invalid", exc)


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


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


def _staging_base(release_base: Path, revision: str) -> Path:
    return release_base / f".{revision}.builder-staging"


def _metadata(
    path: Path,
    *,
    directory: bool,
    mode: int,
    uid: int,
    gid: int,
) -> os.stat_result:
    try:
        item = os.lstat(path)
    except OSError as exc:
        _fail("successor_rebind_owner_runtime_installation_invalid", exc)
    predicate = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not predicate(item.st_mode)
        or stat.S_ISLNK(item.st_mode)
        or item.st_uid != uid
        or item.st_gid != gid
        or stat.S_IMODE(item.st_mode) != mode
        or (not directory and item.st_nlink != 1)
    ):
        _fail("successor_rebind_owner_runtime_installation_invalid")
    return item


def _decode_exact(path: Path, *, uid: int, gid: int) -> Mapping[str, Any]:
    item = _metadata(path, directory=False, mode=0o444, uid=uid, gid=gid)
    if not 0 < item.st_size <= runtime.MAX_MANIFEST_BYTES:
        _fail("successor_rebind_owner_runtime_publication_invalid")
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("ascii", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail("successor_rebind_owner_runtime_publication_invalid", exc)
    if not isinstance(value, Mapping) or raw != _canonical(value) + b"\n":
        _fail("successor_rebind_owner_runtime_publication_invalid")
    return value


def _write_exact(path: Path, value: Mapping[str, Any], *, uid: int, gid: int) -> None:
    raw = _canonical(value) + b"\n"
    if path.exists() or path.is_symlink():
        if _decode_exact(path, uid=uid, gid=gid) != value:
            _fail("successor_rebind_owner_runtime_publication_conflict")
        return
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o444)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
    except OSError as exc:
        _fail("successor_rebind_owner_runtime_publication_failed", exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    _metadata(path, directory=False, mode=0o444, uid=uid, gid=gid)


def _validate_digest_fields(value: Mapping[str, Any], names: tuple[str, ...]) -> bool:
    return all(
        _SHA256.fullmatch(str(value.get(name, ""))) is not None for name in names
    )


def validate_staging_publication(
    value: Any,
    *,
    revision: str,
    staging_root: Path,
) -> dict[str, Any]:
    fields = {
        "schema",
        "release_revision",
        "source_tree_oid",
        "stage_c_builder_terminal_receipt_sha256",
        "staging_root",
        "manifest_sha256",
        "attestation_sha256",
        "tree_sha256",
        "interpreter_sha256",
        "pyvenv_cfg_sha256",
        "owner_runtime_builder_receipt_sha256",
        "owner_runtime_wheel_sha256",
        "builder_unprivileged",
        "root_build_performed",
        "secret_material_recorded",
        "secret_digest_recorded",
        "publication_sha256",
    }
    unsigned = (
        {name: item for name, item in value.items() if name != "publication_sha256"}
        if isinstance(value, Mapping)
        else {}
    )
    if (
        not isinstance(value, Mapping)
        or set(value) != fields
        or value.get("schema") != STAGING_SCHEMA
        or value.get("release_revision") != revision
        or _REVISION.fullmatch(str(value.get("source_tree_oid", ""))) is None
        or value.get("staging_root") != str(staging_root)
        or not _validate_digest_fields(
            value,
            (
                "manifest_sha256",
                "attestation_sha256",
                "tree_sha256",
                "interpreter_sha256",
                "pyvenv_cfg_sha256",
                "stage_c_builder_terminal_receipt_sha256",
                "owner_runtime_builder_receipt_sha256",
                "owner_runtime_wheel_sha256",
                "publication_sha256",
            ),
        )
        or value.get("builder_unprivileged") is not True
        or value.get("root_build_performed") is not False
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
        or value.get("publication_sha256") != _digest(unsigned)
    ):
        _fail("successor_rebind_owner_runtime_staging_invalid")
    return copy.deepcopy(dict(value))


def validate_publication(
    value: Any,
    *,
    revision: str,
    release_base: Path = RELEASE_BASE,
) -> dict[str, Any]:
    fields = {
        "schema",
        "release_revision",
        "source_tree_oid",
        "runtime_root",
        "staging_publication_sha256",
        "staging_manifest_sha256",
        "staging_attestation_sha256",
        "staging_tree_sha256",
        "staging_interpreter_sha256",
        "staging_pyvenv_cfg_sha256",
        "stage_c_builder_terminal_receipt_sha256",
        "owner_runtime_builder_receipt_sha256",
        "owner_runtime_wheel_sha256",
        "manifest_sha256",
        "attestation_sha256",
        "tree_sha256",
        "interpreter_sha256",
        "pyvenv_cfg_sha256",
        "runtime_root_mode",
        "interpreter_mode",
        "manifest_mode",
        "root_owned",
        "create_only",
        "systemd_daemon_reload_performed",
        "unit_enabled",
        "unit_started",
        "unit_scheduled",
        "deployment_performed",
        "secret_material_recorded",
        "secret_digest_recorded",
        "publication_sha256",
    }
    unsigned = (
        {name: item for name, item in value.items() if name != "publication_sha256"}
        if isinstance(value, Mapping)
        else {}
    )
    if (
        not isinstance(value, Mapping)
        or set(value) != fields
        or value.get("schema") != PUBLICATION_SCHEMA
        or value.get("release_revision") != revision
        or _REVISION.fullmatch(str(value.get("source_tree_oid", ""))) is None
        or value.get("runtime_root") != str(release_base / revision)
        or not _validate_digest_fields(
            value,
            (
                "manifest_sha256",
                "attestation_sha256",
                "tree_sha256",
                "interpreter_sha256",
                "pyvenv_cfg_sha256",
                "staging_publication_sha256",
                "staging_manifest_sha256",
                "staging_attestation_sha256",
                "staging_tree_sha256",
                "staging_interpreter_sha256",
                "staging_pyvenv_cfg_sha256",
                "stage_c_builder_terminal_receipt_sha256",
                "owner_runtime_builder_receipt_sha256",
                "owner_runtime_wheel_sha256",
                "publication_sha256",
            ),
        )
        or value.get("runtime_root_mode") != "0555"
        or value.get("interpreter_mode") != "0555"
        or value.get("manifest_mode") != "0444"
        or value.get("root_owned") is not True
        or value.get("create_only") is not True
        or any(
            value.get(name) is not False
            for name in (
                "systemd_daemon_reload_performed",
                "unit_enabled",
                "unit_started",
                "unit_scheduled",
                "deployment_performed",
                "secret_material_recorded",
                "secret_digest_recorded",
            )
        )
        or value.get("publication_sha256") != _digest(unsigned)
    ):
        _fail("successor_rebind_owner_runtime_publication_invalid")
    return copy.deepcopy(dict(value))


def prepare_staging_for_test(
    *,
    revision: str,
    release_base: Path,
    root_uid: int,
    root_gid: int,
    builder_uid: int,
    builder_gid: int,
) -> Path:
    """Root-only directory preparation; it executes no target source."""

    if _REVISION.fullmatch(revision or "") is None:
        _fail("successor_rebind_owner_runtime_contract_invalid")
    if release_base.exists() or release_base.is_symlink():
        _metadata(
            release_base,
            directory=True,
            mode=0o755,
            uid=root_uid,
            gid=root_gid,
        )
    else:
        release_base.mkdir(parents=True, mode=0o755)
        release_base.chmod(0o755)
    staging = _staging_base(release_base, revision)
    if staging.exists() or staging.is_symlink():
        _metadata(
            staging,
            directory=True,
            mode=0o700,
            uid=builder_uid,
            gid=builder_gid,
        )
    else:
        staging.mkdir(mode=0o700)
        os.chown(staging, builder_uid, builder_gid)
        staging.chmod(0o700)
    return staging


def build_staged_for_test(
    *,
    revision: str,
    source_root: Path,
    release_base: Path,
    builder_uid: int,
    builder_gid: int,
    effective_uid: int,
    effective_gid: int,
    source_tree_oid: str,
    stage_c_builder_terminal_receipt_sha256: str,
    builder: Callable[[package.OwnerRuntimeBuildSpec], Mapping[str, Any]] = (
        package.build_owner_runtime
    ),
) -> dict[str, Any]:
    """Unprivileged build step; root execution is forbidden."""

    if (
        effective_uid != builder_uid
        or effective_gid != builder_gid
        or effective_uid == 0
        or _REVISION.fullmatch(source_tree_oid or "") is None
        or _SHA256.fullmatch(stage_c_builder_terminal_receipt_sha256 or "") is None
    ):
        _fail("successor_rebind_owner_runtime_builder_identity_invalid")
    staging_base = _staging_base(release_base, revision)
    _metadata(
        staging_base,
        directory=True,
        mode=0o700,
        uid=builder_uid,
        gid=builder_gid,
    )
    spec = package.OwnerRuntimeBuildSpec(
        revision=revision,
        source_root=source_root,
        release_base=staging_base,
    )
    try:
        receipt = package.validate_publication_receipt(builder(spec), spec=spec)
        manifest = runtime._decode_manifest(  # noqa: SLF001
            (spec.release_root / runtime.MANIFEST_NAME).read_bytes()
        )
    except (
        OSError,
        package.ProductionOwnerRuntimePackagingError,
        runtime.ProductionOwnerRuntimeError,
    ) as exc:
        _fail("successor_rebind_owner_runtime_build_failed", exc)
    unsigned = {
        "schema": STAGING_SCHEMA,
        "release_revision": revision,
        "source_tree_oid": source_tree_oid,
        "stage_c_builder_terminal_receipt_sha256": (
            stage_c_builder_terminal_receipt_sha256
        ),
        "staging_root": str(spec.release_root),
        "manifest_sha256": receipt["manifest_sha256"],
        "attestation_sha256": receipt["attestation_sha256"],
        "tree_sha256": manifest["tree_sha256"],
        "interpreter_sha256": receipt["interpreter_sha256"],
        "pyvenv_cfg_sha256": receipt["pyvenv_cfg_sha256"],
        "owner_runtime_builder_receipt_sha256": receipt["receipt_sha256"],
        "owner_runtime_wheel_sha256": receipt["wheel_sha256"],
        "builder_unprivileged": True,
        "root_build_performed": False,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    publication = validate_staging_publication(
        {**unsigned, "publication_sha256": _digest(unsigned)},
        revision=revision,
        staging_root=spec.release_root,
    )
    _write_exact(
        staging_base / STAGING_NAME,
        publication,
        uid=builder_uid,
        gid=builder_gid,
    )
    return publication


def _rebase_manifest(
    value: Mapping[str, Any],
    *,
    staging_root: Path,
    final_root: Path,
    root_uid: int,
    root_gid: int,
) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    result["artifact_root"] = str(final_root)
    result["root_uid"] = root_uid
    result["root_gid"] = root_gid
    result["root_mode"] = "0555"
    for entry in result["entries"]:
        entry["uid"] = root_uid
        entry["gid"] = root_gid
    result["tree_sha256"] = hashlib.sha256(_canonical(result["entries"])).hexdigest()
    for name in ("interpreter", "pyvenv_cfg"):
        for field in ("path", "realpath"):
            if field in result[name]:
                result[name][field] = str(
                    final_root / Path(result[name][field]).relative_to(staging_root)
                )
    result["site_packages"] = str(
        final_root / Path(result["site_packages"]).relative_to(staging_root)
    )
    result["sys_path"] = [
        str(final_root / Path(item).relative_to(staging_root))
        for item in result["sys_path"]
    ]
    for record in result["required_modules"].values():
        record["origin"] = str(final_root / record["relative_path"])
    unsigned = {
        name: item for name, item in result.items() if name != "manifest_sha256"
    }
    result["manifest_sha256"] = hashlib.sha256(_canonical(unsigned)).hexdigest()
    return result


def _attestation(manifest: Mapping[str, Any]) -> dict[str, Any]:
    unsigned = {
        "schema": runtime.ATTESTATION_SCHEMA,
        "revision": manifest["revision"],
        "manifest_sha256": manifest["manifest_sha256"],
        "tree_sha256": manifest["tree_sha256"],
        "interpreter_sha256": manifest["interpreter"]["sha256"],
        "pyvenv_cfg_sha256": manifest["pyvenv_cfg"]["sha256"],
        "sys_path_sha256": hashlib.sha256(_canonical(manifest["sys_path"])).hexdigest(),
        "required_modules_sha256": hashlib.sha256(
            _canonical(manifest["required_modules"])
        ).hexdigest(),
        "module_origins_release_local": True,
        "ambient_python_environment_present": False,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {
        **unsigned,
        "attestation_sha256": hashlib.sha256(_canonical(unsigned)).hexdigest(),
    }


def _copy_regular_exact(
    source: Path,
    target: Path,
    entry: Mapping[str, Any],
    *,
    builder_uid: int,
    builder_gid: int,
    root_uid: int,
    root_gid: int,
) -> None:
    source_descriptor: int | None = None
    target_descriptor: int | None = None
    try:
        before = os.lstat(source)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        source_descriptor = os.open(source, flags)
        opened = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != builder_uid
            or before.st_gid != builder_gid
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != int(entry["mode"], 8)
            or before.st_size != entry["size"]
        ):
            _fail("successor_rebind_owner_runtime_copy_source_invalid")
        target_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        target_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        target_descriptor = os.open(target, target_flags, 0o600)
        digest = hashlib.sha256()
        remaining = opened.st_size
        while remaining:
            chunk = os.read(source_descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(target_descriptor, view)
                if written <= 0:
                    raise OSError("short copy")
                view = view[written:]
            remaining -= len(chunk)
        after = os.fstat(source_descriptor)
        if (
            _identity(before) != _identity(opened)
            or _identity(before) != _identity(after)
            or remaining != 0
            or digest.hexdigest() != entry["sha256"]
        ):
            _fail("successor_rebind_owner_runtime_copy_source_changed")
        os.fchown(target_descriptor, root_uid, root_gid)
        os.fchmod(target_descriptor, int(entry["mode"], 8))
        os.fsync(target_descriptor)
    except SuccessorRebindOwnerRuntimeError:
        raise
    except (KeyError, OSError, TypeError, ValueError) as exc:
        _fail("successor_rebind_owner_runtime_copy_failed", exc)
    finally:
        if target_descriptor is not None:
            os.close(target_descriptor)
        if source_descriptor is not None:
            os.close(source_descriptor)


def _copy_tree_exact(
    *,
    source: Path,
    destination: Path,
    manifest: Mapping[str, Any],
    builder_uid: int,
    builder_gid: int,
    root_uid: int,
    root_gid: int,
) -> None:
    try:
        destination.mkdir(mode=0o700)
        os.chown(destination, root_uid, root_gid)
        entries = tuple(manifest["entries"])
        directories = tuple(item for item in entries if item.get("kind") == "directory")
        others = tuple(item for item in entries if item.get("kind") != "directory")
        for entry in directories:
            target = destination / entry["path"]
            target.mkdir(mode=0o700)
            os.chown(target, root_uid, root_gid)
        for entry in others:
            relative = entry["path"]
            source_path = source / relative
            target_path = destination / relative
            if entry.get("kind") == "file":
                _copy_regular_exact(
                    source_path,
                    target_path,
                    entry,
                    builder_uid=builder_uid,
                    builder_gid=builder_gid,
                    root_uid=root_uid,
                    root_gid=root_gid,
                )
            elif entry.get("kind") == "symlink":
                source_state = os.lstat(source_path)
                link = os.readlink(source_path)
                if (
                    source_state.st_uid != builder_uid
                    or source_state.st_gid != builder_gid
                    or link != entry.get("target")
                ):
                    _fail("successor_rebind_owner_runtime_copy_source_invalid")
                target_path.symlink_to(link)
                os.chown(target_path, root_uid, root_gid, follow_symlinks=False)
            else:
                _fail("successor_rebind_owner_runtime_copy_source_invalid")
        for entry in reversed(directories):
            (destination / entry["path"]).chmod(int(entry["mode"], 8))
    except SuccessorRebindOwnerRuntimeError:
        raise
    except (KeyError, OSError, TypeError, ValueError) as exc:
        if destination.exists() and getattr(
            shutil.rmtree, "avoids_symlink_attacks", False
        ):
            shutil.rmtree(destination)
        _fail("successor_rebind_owner_runtime_copy_failed", exc)


def _rename_noreplace(source: Path, target: Path) -> None:
    if not sys.platform.startswith("linux"):
        _fail("successor_rebind_owner_runtime_rename_noreplace_unavailable")
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        result = renameat2(
            -100,
            os.fsencode(source),
            -100,
            os.fsencode(target),
            1,
        )
    except (AttributeError, OSError) as exc:
        _fail("successor_rebind_owner_runtime_rename_noreplace_unavailable", exc)
    if result != 0:
        observed = ctypes.get_errno()
        if observed == errno.EEXIST:
            _fail("successor_rebind_owner_runtime_existing_invalid")
        _fail("successor_rebind_owner_runtime_promotion_failed")


def _require_exact_staging_authority(
    staging: Mapping[str, Any],
    *,
    expected_staging_publication_sha256: str,
    expected_source_tree_oid: str,
    expected_stage_c_builder_terminal_receipt_sha256: str,
    expected_owner_runtime_builder_receipt_sha256: str,
    expected_owner_runtime_wheel_sha256: str,
    expected_staging_manifest_sha256: str,
    expected_staging_attestation_sha256: str,
    expected_staging_tree_sha256: str,
    expected_staging_interpreter_sha256: str,
    expected_staging_pyvenv_cfg_sha256: str,
) -> None:
    """Bind builder-owned data to a separately trusted Stage-C authority."""

    expected = {
        "publication_sha256": expected_staging_publication_sha256,
        "source_tree_oid": expected_source_tree_oid,
        "stage_c_builder_terminal_receipt_sha256": (
            expected_stage_c_builder_terminal_receipt_sha256
        ),
        "owner_runtime_builder_receipt_sha256": (
            expected_owner_runtime_builder_receipt_sha256
        ),
        "owner_runtime_wheel_sha256": expected_owner_runtime_wheel_sha256,
        "manifest_sha256": expected_staging_manifest_sha256,
        "attestation_sha256": expected_staging_attestation_sha256,
        "tree_sha256": expected_staging_tree_sha256,
        "interpreter_sha256": expected_staging_interpreter_sha256,
        "pyvenv_cfg_sha256": expected_staging_pyvenv_cfg_sha256,
    }
    if (
        _REVISION.fullmatch(expected_source_tree_oid or "") is None
        or any(
            _SHA256.fullmatch(value or "") is None
            for name, value in expected.items()
            if name != "source_tree_oid"
        )
        or any(staging.get(name) != value for name, value in expected.items())
    ):
        _fail("successor_rebind_owner_runtime_staging_authority_invalid")


def _require_exact_publication_provenance(
    publication: Mapping[str, Any],
    *,
    expected_staging_publication_sha256: str,
    expected_source_tree_oid: str,
    expected_stage_c_builder_terminal_receipt_sha256: str,
    expected_owner_runtime_builder_receipt_sha256: str,
    expected_owner_runtime_wheel_sha256: str,
    expected_staging_manifest_sha256: str,
    expected_staging_attestation_sha256: str,
    expected_staging_tree_sha256: str,
    expected_staging_interpreter_sha256: str,
    expected_staging_pyvenv_cfg_sha256: str,
) -> None:
    expected = {
        "staging_publication_sha256": expected_staging_publication_sha256,
        "source_tree_oid": expected_source_tree_oid,
        "stage_c_builder_terminal_receipt_sha256": (
            expected_stage_c_builder_terminal_receipt_sha256
        ),
        "owner_runtime_builder_receipt_sha256": (
            expected_owner_runtime_builder_receipt_sha256
        ),
        "owner_runtime_wheel_sha256": expected_owner_runtime_wheel_sha256,
        "staging_manifest_sha256": expected_staging_manifest_sha256,
        "staging_attestation_sha256": expected_staging_attestation_sha256,
        "staging_tree_sha256": expected_staging_tree_sha256,
        "staging_interpreter_sha256": expected_staging_interpreter_sha256,
        "staging_pyvenv_cfg_sha256": expected_staging_pyvenv_cfg_sha256,
    }
    if any(publication.get(name) != value for name, value in expected.items()):
        _fail("successor_rebind_owner_runtime_staging_authority_invalid")


def promote_staged_for_test(
    *,
    revision: str,
    release_base: Path,
    publication_root: Path,
    builder_uid: int,
    builder_gid: int,
    root_uid: int,
    root_gid: int,
    expected_staging_publication_sha256: str,
    expected_source_tree_oid: str,
    expected_stage_c_builder_terminal_receipt_sha256: str,
    expected_owner_runtime_builder_receipt_sha256: str,
    expected_owner_runtime_wheel_sha256: str,
    expected_staging_manifest_sha256: str,
    expected_staging_attestation_sha256: str,
    expected_staging_tree_sha256: str,
    expected_staging_interpreter_sha256: str,
    expected_staging_pyvenv_cfg_sha256: str,
    rename_noreplace: Callable[[Path, Path], None] = _rename_noreplace,
) -> dict[str, Any]:
    """Data-only root promotion; no staged interpreter or wheel is executed."""

    final_root = release_base / revision
    publication_path = publication_root / f"{revision}.json"
    if final_root.exists() or final_root.is_symlink():
        publication = validate_publication(
            _decode_exact(publication_path, uid=root_uid, gid=root_gid),
            revision=revision,
            release_base=release_base,
        )
        _require_exact_publication_provenance(
            publication,
            expected_staging_publication_sha256=(expected_staging_publication_sha256),
            expected_source_tree_oid=expected_source_tree_oid,
            expected_stage_c_builder_terminal_receipt_sha256=(
                expected_stage_c_builder_terminal_receipt_sha256
            ),
            expected_owner_runtime_builder_receipt_sha256=(
                expected_owner_runtime_builder_receipt_sha256
            ),
            expected_owner_runtime_wheel_sha256=(expected_owner_runtime_wheel_sha256),
            expected_staging_manifest_sha256=expected_staging_manifest_sha256,
            expected_staging_attestation_sha256=(expected_staging_attestation_sha256),
            expected_staging_tree_sha256=expected_staging_tree_sha256,
            expected_staging_interpreter_sha256=(expected_staging_interpreter_sha256),
            expected_staging_pyvenv_cfg_sha256=(expected_staging_pyvenv_cfg_sha256),
        )
        preexec.verify(
            revision=revision,
            expected_manifest_sha256=publication["manifest_sha256"],
            expected_tree_sha256=publication["tree_sha256"],
            expected_interpreter_sha256=publication["interpreter_sha256"],
            expected_attestation_sha256=publication["attestation_sha256"],
            runtime_base=release_base,
            uid=root_uid,
            gid=root_gid,
        )
        return publication
    staging_base = _staging_base(release_base, revision)
    staging_root = staging_base / revision
    staging = validate_staging_publication(
        _decode_exact(
            staging_base / STAGING_NAME,
            uid=builder_uid,
            gid=builder_gid,
        ),
        revision=revision,
        staging_root=staging_root,
    )
    _require_exact_staging_authority(
        staging,
        expected_staging_publication_sha256=expected_staging_publication_sha256,
        expected_source_tree_oid=expected_source_tree_oid,
        expected_stage_c_builder_terminal_receipt_sha256=(
            expected_stage_c_builder_terminal_receipt_sha256
        ),
        expected_owner_runtime_builder_receipt_sha256=(
            expected_owner_runtime_builder_receipt_sha256
        ),
        expected_owner_runtime_wheel_sha256=expected_owner_runtime_wheel_sha256,
        expected_staging_manifest_sha256=expected_staging_manifest_sha256,
        expected_staging_attestation_sha256=expected_staging_attestation_sha256,
        expected_staging_tree_sha256=expected_staging_tree_sha256,
        expected_staging_interpreter_sha256=(expected_staging_interpreter_sha256),
        expected_staging_pyvenv_cfg_sha256=(expected_staging_pyvenv_cfg_sha256),
    )
    staged_manifest = preexec.verify_staged(
        root=staging_root,
        revision=revision,
        expected_manifest_sha256=staging["manifest_sha256"],
        expected_tree_sha256=staging["tree_sha256"],
        expected_interpreter_sha256=staging["interpreter_sha256"],
        expected_attestation_sha256=staging["attestation_sha256"],
        uid=builder_uid,
        gid=builder_gid,
    )
    rebased = _rebase_manifest(
        staged_manifest,
        staging_root=staging_root,
        final_root=final_root,
        root_uid=root_uid,
        root_gid=root_gid,
    )
    incomplete = release_base / f".{revision}.root-copy-incomplete"
    if incomplete.exists() or incomplete.is_symlink():
        _fail("successor_rebind_owner_runtime_incomplete_conflict")
    _copy_tree_exact(
        source=staging_root,
        destination=incomplete,
        manifest=staged_manifest,
        builder_uid=builder_uid,
        builder_gid=builder_gid,
        root_uid=root_uid,
        root_gid=root_gid,
    )
    # A builder-held descriptor can mutate only staging.  Re-verifying it
    # after the stable copy detects any source write/swap that overlapped the
    # copy; the fresh root-owned destination is independent of those fds.
    preexec.verify_staged(
        root=staging_root,
        revision=revision,
        expected_manifest_sha256=staging["manifest_sha256"],
        expected_tree_sha256=staging["tree_sha256"],
        expected_interpreter_sha256=staging["interpreter_sha256"],
        expected_attestation_sha256=staging["attestation_sha256"],
        uid=builder_uid,
        gid=builder_gid,
    )
    manifest_path = incomplete / runtime.MANIFEST_NAME
    _write_exact(manifest_path, rebased, uid=root_uid, gid=root_gid)
    incomplete.chmod(0o555)
    attestation = _attestation(rebased)
    preexec.verify(
        revision=revision,
        expected_manifest_sha256=rebased["manifest_sha256"],
        expected_tree_sha256=rebased["tree_sha256"],
        expected_interpreter_sha256=rebased["interpreter"]["sha256"],
        expected_attestation_sha256=attestation["attestation_sha256"],
        runtime_base=release_base,
        uid=root_uid,
        gid=root_gid,
        physical_root=incomplete,
    )
    try:
        rename_noreplace(incomplete, final_root)
    except SuccessorRebindOwnerRuntimeError:
        raise
    except OSError as exc:
        _fail("successor_rebind_owner_runtime_promotion_failed", exc)
    preexec.verify(
        revision=revision,
        expected_manifest_sha256=rebased["manifest_sha256"],
        expected_tree_sha256=rebased["tree_sha256"],
        expected_interpreter_sha256=rebased["interpreter"]["sha256"],
        expected_attestation_sha256=attestation["attestation_sha256"],
        runtime_base=release_base,
        uid=root_uid,
        gid=root_gid,
    )
    if publication_root.exists() or publication_root.is_symlink():
        _metadata(
            publication_root,
            directory=True,
            mode=0o755,
            uid=root_uid,
            gid=root_gid,
        )
    else:
        publication_root.mkdir(parents=True, mode=0o755)
        publication_root.chmod(0o755)
    unsigned = {
        "schema": PUBLICATION_SCHEMA,
        "release_revision": revision,
        "source_tree_oid": staging["source_tree_oid"],
        "runtime_root": str(final_root),
        "staging_publication_sha256": staging["publication_sha256"],
        "staging_manifest_sha256": staging["manifest_sha256"],
        "staging_attestation_sha256": staging["attestation_sha256"],
        "staging_tree_sha256": staging["tree_sha256"],
        "staging_interpreter_sha256": staging["interpreter_sha256"],
        "staging_pyvenv_cfg_sha256": staging["pyvenv_cfg_sha256"],
        "stage_c_builder_terminal_receipt_sha256": staging[
            "stage_c_builder_terminal_receipt_sha256"
        ],
        "owner_runtime_builder_receipt_sha256": staging[
            "owner_runtime_builder_receipt_sha256"
        ],
        "owner_runtime_wheel_sha256": staging["owner_runtime_wheel_sha256"],
        "manifest_sha256": rebased["manifest_sha256"],
        "attestation_sha256": attestation["attestation_sha256"],
        "tree_sha256": rebased["tree_sha256"],
        "interpreter_sha256": rebased["interpreter"]["sha256"],
        "pyvenv_cfg_sha256": rebased["pyvenv_cfg"]["sha256"],
        "runtime_root_mode": "0555",
        "interpreter_mode": "0555",
        "manifest_mode": "0444",
        "root_owned": True,
        "create_only": True,
        "systemd_daemon_reload_performed": False,
        "unit_enabled": False,
        "unit_started": False,
        "unit_scheduled": False,
        "deployment_performed": False,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    publication = validate_publication(
        {**unsigned, "publication_sha256": _digest(unsigned)},
        revision=revision,
        release_base=release_base,
    )
    _write_exact(
        publication_path,
        publication,
        uid=root_uid,
        gid=root_gid,
    )
    return publication


def validate_active_installation(
    *,
    revision: str,
    expected_publication_sha256: str,
    expected_manifest_sha256: str,
    expected_attestation_sha256: str,
    expected_tree_sha256: str,
    expected_interpreter_sha256: str,
    expected_staging_publication_sha256: str,
    expected_source_tree_oid: str,
    expected_stage_c_builder_terminal_receipt_sha256: str,
    expected_owner_runtime_builder_receipt_sha256: str,
    expected_owner_runtime_wheel_sha256: str,
    expected_staging_manifest_sha256: str,
    expected_staging_attestation_sha256: str,
    expected_staging_tree_sha256: str,
    expected_staging_interpreter_sha256: str,
    expected_staging_pyvenv_cfg_sha256: str,
    release_base: Path = RELEASE_BASE,
    publication_root: Path = PUBLICATION_ROOT,
    uid: int = 0,
    gid: int = 0,
) -> dict[str, Any]:
    publication = validate_publication(
        _decode_exact(publication_root / f"{revision}.json", uid=uid, gid=gid),
        revision=revision,
        release_base=release_base,
    )
    expected = {
        "publication_sha256": expected_publication_sha256,
        "manifest_sha256": expected_manifest_sha256,
        "attestation_sha256": expected_attestation_sha256,
        "tree_sha256": expected_tree_sha256,
        "interpreter_sha256": expected_interpreter_sha256,
    }
    if any(publication[name] != value for name, value in expected.items()):
        _fail("successor_rebind_owner_runtime_identity_invalid")
    _require_exact_publication_provenance(
        publication,
        expected_staging_publication_sha256=expected_staging_publication_sha256,
        expected_source_tree_oid=expected_source_tree_oid,
        expected_stage_c_builder_terminal_receipt_sha256=(
            expected_stage_c_builder_terminal_receipt_sha256
        ),
        expected_owner_runtime_builder_receipt_sha256=(
            expected_owner_runtime_builder_receipt_sha256
        ),
        expected_owner_runtime_wheel_sha256=expected_owner_runtime_wheel_sha256,
        expected_staging_manifest_sha256=expected_staging_manifest_sha256,
        expected_staging_attestation_sha256=expected_staging_attestation_sha256,
        expected_staging_tree_sha256=expected_staging_tree_sha256,
        expected_staging_interpreter_sha256=(expected_staging_interpreter_sha256),
        expected_staging_pyvenv_cfg_sha256=(expected_staging_pyvenv_cfg_sha256),
    )
    active = runtime.require_active_owner_runtime(revision)
    if any(
        active[name] != publication[name]
        for name in (
            "attestation_sha256",
            "tree_sha256",
            "interpreter_sha256",
            "manifest_sha256",
        )
    ):
        _fail("successor_rebind_owner_runtime_active_identity_invalid")
    return publication


__all__ = [
    "PUBLICATION_ROOT",
    "PUBLICATION_SCHEMA",
    "RELEASE_BASE",
    "STAGING_SCHEMA",
    "SuccessorRebindOwnerRuntimeError",
    "build_staged_for_test",
    "prepare_staging_for_test",
    "promote_staged_for_test",
    "validate_active_installation",
    "validate_publication",
    "validate_staging_publication",
]
