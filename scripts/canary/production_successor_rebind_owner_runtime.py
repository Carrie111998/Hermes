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
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from contextlib import contextmanager, nullcontext
from typing import Any, Callable, Mapping, Never

from gateway import production_owner_runtime as runtime
from scripts.canary import package_production_owner_runtime as package
from scripts.canary import production_successor_rebind_owner_runtime_preexec as preexec


STAGING_SCHEMA = "muncho-successor-rebind-owner-runtime-staging.v3"
PUBLICATION_SCHEMA = "muncho-successor-rebind-owner-runtime-publication.v3"
PROMOTION_INTENT_SCHEMA = "muncho-successor-rebind-owner-runtime-intent.v2"
PROMOTION_FRAME_SCHEMA = "muncho-successor-rebind-owner-runtime-promote-frame.v2"
LAUNCH_AUTHORITY_SCHEMA = (
    "muncho-dual-upstream-sync-successor-rebind-launch-authority.v1"
)
PHASE_FRAME_SCHEMA = "muncho-successor-rebind-owner-runtime-phase-frame.v1"
RELEASE_BASE = Path("/usr/lib/muncho-successor-rebind-runtime")
PUBLICATION_ROOT = Path("/var/lib/muncho-successor-rebind-runtime-publications")
REVISION_LIBRARY_BASE = Path("/usr/lib/muncho-release-updater-releases")
CONTROLLER_RELEASES_ROOT = Path(
    "/opt/adventico-ai-platform/hermes-agent-releases"
)
PRODUCTION_UV = Path("/usr/local/bin/uv")
BUILDER_UID = 29104
BUILDER_GID = 29104
STAGING_NAME = "staging-publication.json"
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PHASE_FRAME_MAX_BYTES = 64 * 1024


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


def _validate_launch_authority(value: Any, *, revision: str) -> dict[str, Any]:
    fields = {
        "schema",
        "operation",
        "request_sha256",
        "target_revision",
        "predecessor_revision",
        "predecessor_activation_receipt_sha256",
        "predecessor_trust_sha256",
        "stage_c_host_artifact_manifest_sha256",
        "stage_c_release_update_publication_sha256",
        "stage_c_builder_terminal_receipt_sha256",
        "launch_authority_sha256",
        "candidate_seal_receipt_sha256",
        "whole_tree_manifest_sha256",
        "input_internal_identities_sha256",
        "release_root",
        "source_tree_oid",
        "secret_material_recorded",
        "secret_digest_recorded",
        "launch_authority_sha256",
    }
    unsigned = (
        {
            name: item
            for name, item in value.items()
            if name != "launch_authority_sha256"
        }
        if isinstance(value, Mapping)
        else {}
    )
    digest_fields = fields - {
        "schema",
        "operation",
        "target_revision",
        "predecessor_revision",
        "release_root",
        "source_tree_oid",
        "secret_material_recorded",
        "secret_digest_recorded",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != fields
        or value.get("schema") != LAUNCH_AUTHORITY_SCHEMA
        or value.get("operation") != "dual-upstream-sync-successor-rebind"
        or value.get("target_revision") != revision
        or _REVISION.fullmatch(str(value.get("predecessor_revision", ""))) is None
        or _REVISION.fullmatch(str(value.get("source_tree_oid", ""))) is None
        or any(_SHA256.fullmatch(str(value.get(name, ""))) is None for name in digest_fields)
        or value.get("release_root")
        != str(CONTROLLER_RELEASES_ROOT / f"hermes-agent-{revision[:12]}")
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
        or value.get("launch_authority_sha256") != _digest(unsigned)
    ):
        _fail("successor_rebind_owner_runtime_launch_authority_invalid")
    return copy.deepcopy(dict(value))


def build_phase_frame(
    *,
    operation: str,
    revision: str,
    launch_authority: Mapping[str, Any],
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    authority = _validate_launch_authority(launch_authority, revision=revision)
    unsigned = {
        "schema": PHASE_FRAME_SCHEMA,
        "operation": operation,
        "release_revision": revision,
        "launch_authority": authority,
        "payload": copy.deepcopy(dict(payload)) if payload is not None else None,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {**unsigned, "frame_sha256": _digest(unsigned)}


def _validate_phase_frame(
    value: Any,
    *,
    operation: str,
    revision: str,
) -> dict[str, Any]:
    fields = {
        "schema",
        "operation",
        "release_revision",
        "launch_authority",
        "payload",
        "secret_material_recorded",
        "secret_digest_recorded",
        "frame_sha256",
    }
    unsigned = (
        {name: item for name, item in value.items() if name != "frame_sha256"}
        if isinstance(value, Mapping)
        else {}
    )
    if (
        not isinstance(value, Mapping)
        or set(value) != fields
        or value.get("schema") != PHASE_FRAME_SCHEMA
        or value.get("operation") != operation
        or value.get("release_revision") != revision
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
        or _SHA256.fullmatch(str(value.get("frame_sha256", ""))) is None
        or value.get("frame_sha256") != _digest(unsigned)
    ):
        _fail("successor_rebind_owner_runtime_phase_frame_invalid")
    result = copy.deepcopy(dict(value))
    result["launch_authority"] = _validate_launch_authority(
        result["launch_authority"],
        revision=revision,
    )
    return result


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


@contextmanager
def _exclusive_parent_lock(path: Path, *, uid: int, gid: int):
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        state = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(state.st_mode)
            or state.st_uid != uid
            or state.st_gid != gid
            or stat.S_IMODE(state.st_mode) & 0o022
        ):
            _fail("successor_rebind_owner_runtime_publication_conflict")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except SuccessorRebindOwnerRuntimeError:
        raise
    except OSError as exc:
        _fail("successor_rebind_owner_runtime_publication_failed", exc)
    finally:
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _read_publication_bytes(
    path: Path,
    *,
    uid: int,
    gid: int,
    modes: frozenset[int],
    nlinks: frozenset[int],
) -> tuple[bytes, os.stat_result]:
    descriptor: int | None = None
    try:
        before = os.lstat(path)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_uid != uid
            or before.st_gid != gid
            or stat.S_IMODE(before.st_mode) not in modes
            or before.st_nlink not in nlinks
            or before.st_size < 0
            or before.st_size > runtime.MAX_MANIFEST_BYTES
        ):
            _fail("successor_rebind_owner_runtime_publication_conflict")
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        reached = os.lstat(path)
    except SuccessorRebindOwnerRuntimeError:
        raise
    except OSError as exc:
        _fail("successor_rebind_owner_runtime_publication_conflict", exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (
        remaining
        or len({_identity(item) for item in (before, opened, after, reached)}) != 1
    ):
        _fail("successor_rebind_owner_runtime_publication_conflict")
    return b"".join(chunks), before


def _finalize_recovered_publication_pending(
    path: Path,
    *,
    raw: bytes,
    state: os.stat_result,
    uid: int,
    gid: int,
) -> None:
    """Durably finalize a complete recovered pending inode before linking."""

    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(state):
            _fail("successor_rebind_owner_runtime_publication_conflict")
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        reached = os.lstat(path)
    except SuccessorRebindOwnerRuntimeError:
        raise
    except OSError as exc:
        _fail("successor_rebind_owner_runtime_publication_conflict", exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    stable = (after, reached)
    if any(
        not stat.S_ISREG(item.st_mode)
        or stat.S_ISLNK(item.st_mode)
        or item.st_dev != state.st_dev
        or item.st_ino != state.st_ino
        or item.st_uid != uid
        or item.st_gid != gid
        or item.st_nlink != 1
        or item.st_size != len(raw)
        or stat.S_IMODE(item.st_mode) != 0o444
        for item in stable
    ):
        _fail("successor_rebind_owner_runtime_publication_conflict")
    observed, _ = _read_publication_bytes(
        path,
        uid=uid,
        gid=gid,
        modes=frozenset({0o444}),
        nlinks=frozenset({1}),
    )
    if observed != raw:
        _fail("successor_rebind_owner_runtime_publication_conflict")


def _write_exact(
    path: Path,
    value: Mapping[str, Any],
    *,
    uid: int,
    gid: int,
    after_pending_fsync: Callable[[], None] | None = None,
    after_final_link: Callable[[], None] | None = None,
    before_pending_finalize: Callable[[], None] | None = None,
    before_pending_fsync: Callable[[], None] | None = None,
    parent_locked: bool = False,
) -> None:
    raw = _canonical(value) + b"\n"
    digest = hashlib.sha256(raw).hexdigest()
    pending = path.with_name(f".{path.name}.{digest}.pending")
    lock = (
        nullcontext()
        if parent_locked
        else _exclusive_parent_lock(path, uid=uid, gid=gid)
    )
    with lock:
        pending_pattern = re.compile(
            rf"^\.{re.escape(path.name)}\.([0-9a-f]{{64}})\.pending$"
        )
        try:
            conflicts = tuple(
                name
                for name in os.listdir(path.parent)
                if pending_pattern.fullmatch(name) and name != pending.name
            )
        except OSError as exc:
            _fail("successor_rebind_owner_runtime_publication_conflict", exc)
        if conflicts:
            _fail("successor_rebind_owner_runtime_publication_conflict")
        if os.path.lexists(path):
            target_raw, target_state = _read_publication_bytes(
                path,
                uid=uid,
                gid=gid,
                modes=frozenset({0o444}),
                nlinks=frozenset({1, 2}),
            )
            if target_raw != raw:
                _fail("successor_rebind_owner_runtime_publication_conflict")
            if os.path.lexists(pending):
                pending_raw, pending_state = _read_publication_bytes(
                    pending,
                    uid=uid,
                    gid=gid,
                    modes=frozenset({0o444}),
                    nlinks=frozenset({2}),
                )
                if (
                    pending_raw != raw
                    or target_state.st_dev != pending_state.st_dev
                    or target_state.st_ino != pending_state.st_ino
                ):
                    _fail("successor_rebind_owner_runtime_publication_conflict")
                pending.unlink()
                _fsync_directory(path.parent)
            _read_publication_bytes(
                path,
                uid=uid,
                gid=gid,
                modes=frozenset({0o444}),
                nlinks=frozenset({1}),
            )
            return

        descriptor: int | None = None
        try:
            if os.path.lexists(pending):
                prefix, _state = _read_publication_bytes(
                    pending,
                    uid=uid,
                    gid=gid,
                    modes=frozenset({0o600, 0o444}),
                    nlinks=frozenset({1}),
                )
                if not raw.startswith(prefix):
                    _fail("successor_rebind_owner_runtime_publication_conflict")
                if prefix == raw:
                    _finalize_recovered_publication_pending(
                        pending,
                        raw=raw,
                        state=_state,
                        uid=uid,
                        gid=gid,
                    )
                    view = memoryview(b"")
                else:
                    pending.unlink()
                    _fsync_directory(path.parent)
                    descriptor = os.open(
                        pending,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                    )
                    os.fchmod(descriptor, 0o600)
                    os.fchown(descriptor, uid, gid)
                    view = memoryview(raw)
            else:
                descriptor = os.open(
                    pending,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                os.fchmod(descriptor, 0o600)
                os.fchown(descriptor, uid, gid)
                view = memoryview(raw)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            if descriptor is not None:
                if before_pending_finalize is not None:
                    before_pending_finalize()
                os.fchmod(descriptor, 0o444)
                if before_pending_fsync is not None:
                    before_pending_fsync()
                os.fsync(descriptor)
                if after_pending_fsync is not None:
                    after_pending_fsync()
        except SuccessorRebindOwnerRuntimeError:
            raise
        except OSError as exc:
            _fail("successor_rebind_owner_runtime_publication_failed", exc)
        finally:
            if descriptor is not None:
                os.close(descriptor)
        pending_raw, _ = _read_publication_bytes(
            pending,
            uid=uid,
            gid=gid,
            modes=frozenset({0o444}),
            nlinks=frozenset({1}),
        )
        if pending_raw != raw:
            _fail("successor_rebind_owner_runtime_publication_conflict")
        try:
            os.link(pending, path, follow_symlinks=False)
        except OSError as exc:
            _fail("successor_rebind_owner_runtime_publication_failed", exc)
        _fsync_directory(path.parent)
        if after_final_link is not None:
            after_final_link()
        pending.unlink()
        _fsync_directory(path.parent)
        final_raw, _ = _read_publication_bytes(
            path,
            uid=uid,
            gid=gid,
            modes=frozenset({0o444}),
            nlinks=frozenset({1}),
        )
        if final_raw != raw:
            _fail("successor_rebind_owner_runtime_publication_conflict")


def _fsync_directory(path: Path) -> None:
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        os.fsync(descriptor)
    except OSError as exc:
        _fail("successor_rebind_owner_runtime_durability_failed", exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _fsync_runtime_tree(root: Path, manifest: Mapping[str, Any]) -> None:
    try:
        directories = tuple(
            root / entry["path"]
            for entry in manifest["entries"]
            if entry.get("kind") == "directory"
        )
    except (KeyError, TypeError) as exc:
        _fail("successor_rebind_owner_runtime_durability_failed", exc)
    for directory in reversed(directories):
        _fsync_directory(directory)
    _fsync_directory(root)


def _ensure_publication_root(
    publication_root: Path,
    *,
    root_uid: int,
    root_gid: int,
) -> None:
    if publication_root.exists() or publication_root.is_symlink():
        _metadata(
            publication_root,
            directory=True,
            mode=0o755,
            uid=root_uid,
            gid=root_gid,
        )
        return
    try:
        publication_root.mkdir(mode=0o755)
        publication_root.chmod(0o755)
    except OSError as exc:
        _fail("successor_rebind_owner_runtime_publication_failed", exc)
    _metadata(
        publication_root,
        directory=True,
        mode=0o755,
        uid=root_uid,
        gid=root_gid,
    )
    _fsync_directory(publication_root.parent)
    _fsync_directory(publication_root)


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
        "launch_authority_sha256",
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
                "launch_authority_sha256",
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
        "launch_authority_sha256",
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
                "launch_authority_sha256",
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


def validate_promotion_intent(
    value: Any,
    *,
    revision: str,
    release_base: Path = RELEASE_BASE,
) -> dict[str, Any]:
    fields = {
        "schema",
        "release_revision",
        "runtime_root",
        "publication",
        "intent_sha256",
    }
    unsigned = (
        {name: item for name, item in value.items() if name != "intent_sha256"}
        if isinstance(value, Mapping)
        else {}
    )
    publication = (
        validate_publication(
            value.get("publication"),
            revision=revision,
            release_base=release_base,
        )
        if isinstance(value, Mapping)
        else {}
    )
    if (
        not isinstance(value, Mapping)
        or set(value) != fields
        or value.get("schema") != PROMOTION_INTENT_SCHEMA
        or value.get("release_revision") != revision
        or value.get("runtime_root") != str(release_base / revision)
        or value.get("publication") != publication
        or _SHA256.fullmatch(str(value.get("intent_sha256", ""))) is None
        or value.get("intent_sha256") != _digest(unsigned)
    ):
        _fail("successor_rebind_owner_runtime_intent_invalid")
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
    launch_authority_sha256: str = "0" * 64,
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
        or _SHA256.fullmatch(launch_authority_sha256 or "") is None
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
        "launch_authority_sha256": launch_authority_sha256,
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
        _fail("successor_rebind_owner_runtime_copy_failed", exc)


def _quarantine_recoverable_root_copy(
    *,
    selected: Path,
    quarantine: Path,
    staging_root: Path,
    staged_manifest: Mapping[str, Any],
    rebased_manifest: Mapping[str, Any],
    root_uid: int,
    root_gid: int,
) -> None:
    """Quarantine only an intent-bound exact prefix of our root copy."""

    try:
        root_state = os.lstat(selected)
        if (
            not stat.S_ISDIR(root_state.st_mode)
            or stat.S_ISLNK(root_state.st_mode)
            or root_state.st_uid != root_uid
            or root_state.st_gid != root_gid
            or stat.S_IMODE(root_state.st_mode) not in {0o700, 0o555}
        ):
            _fail("successor_rebind_owner_runtime_incomplete_conflict")
        entries = {
            str(item["path"]): item for item in staged_manifest["entries"]
        }
        manifest_raw = _canonical(rebased_manifest) + b"\n"
        manifest_digest = hashlib.sha256(manifest_raw).hexdigest()
        manifest_pending = f".{runtime.MANIFEST_NAME}.{manifest_digest}.pending"
        manifest_final_path = selected / runtime.MANIFEST_NAME
        manifest_pending_path = selected / manifest_pending
        final_exists = os.path.lexists(manifest_final_path)
        pending_exists = os.path.lexists(manifest_pending_path)
        special_states: dict[str, os.stat_result] = {}

        def read_manifest_member(
            path: Path,
            *,
            modes: frozenset[int],
            nlinks: frozenset[int],
        ) -> tuple[bytes, os.stat_result]:
            try:
                return _read_publication_bytes(
                    path,
                    uid=root_uid,
                    gid=root_gid,
                    modes=modes,
                    nlinks=nlinks,
                )
            except SuccessorRebindOwnerRuntimeError:
                _fail("successor_rebind_owner_runtime_incomplete_conflict")

        if final_exists and pending_exists:
            final_raw, final_state = read_manifest_member(
                manifest_final_path,
                modes=frozenset({0o444}),
                nlinks=frozenset({2}),
            )
            pending_raw, pending_state = read_manifest_member(
                manifest_pending_path,
                modes=frozenset({0o444}),
                nlinks=frozenset({2}),
            )
            if (
                final_raw != manifest_raw
                or pending_raw != manifest_raw
                or final_state.st_dev != pending_state.st_dev
                or final_state.st_ino != pending_state.st_ino
            ):
                _fail("successor_rebind_owner_runtime_incomplete_conflict")
            special_states[runtime.MANIFEST_NAME] = final_state
            special_states[manifest_pending] = pending_state
        elif final_exists:
            final_raw, final_state = read_manifest_member(
                manifest_final_path,
                modes=frozenset({0o444}),
                nlinks=frozenset({1}),
            )
            if final_raw != manifest_raw:
                _fail("successor_rebind_owner_runtime_incomplete_conflict")
            special_states[runtime.MANIFEST_NAME] = final_state
        elif pending_exists:
            pending_raw, pending_state = read_manifest_member(
                manifest_pending_path,
                modes=frozenset({0o600, 0o444}),
                nlinks=frozenset({1}),
            )
            if not manifest_raw.startswith(pending_raw):
                _fail("successor_rebind_owner_runtime_incomplete_conflict")
            special_states[manifest_pending] = pending_state
        for current, directories, files in os.walk(
            selected,
            topdown=True,
            followlinks=False,
        ):
            current_path = Path(current)
            for name in (*directories, *files):
                path = current_path / name
                relative = path.relative_to(selected).as_posix()
                state = os.lstat(path)
                entry = entries.get(relative)
                if entry is None:
                    expected_special = special_states.get(relative)
                    if (
                        expected_special is None
                        or _identity(state) != _identity(expected_special)
                    ):
                        _fail("successor_rebind_owner_runtime_incomplete_conflict")
                    continue
                expected_mode = int(str(entry["mode"]), 8)
                if state.st_uid != root_uid or state.st_gid != root_gid:
                    _fail("successor_rebind_owner_runtime_incomplete_conflict")
                if entry["kind"] == "directory":
                    if (
                        not stat.S_ISDIR(state.st_mode)
                        or stat.S_ISLNK(state.st_mode)
                        or stat.S_IMODE(state.st_mode) not in {0o700, expected_mode}
                    ):
                        _fail("successor_rebind_owner_runtime_incomplete_conflict")
                elif entry["kind"] == "file":
                    if (
                        not stat.S_ISREG(state.st_mode)
                        or stat.S_ISLNK(state.st_mode)
                        or state.st_nlink != 1
                        or stat.S_IMODE(state.st_mode) not in {0o600, expected_mode}
                        or state.st_size > int(entry["size"])
                    ):
                        _fail("successor_rebind_owner_runtime_incomplete_conflict")
                    source_raw = (staging_root / relative).read_bytes()
                    observed = path.read_bytes()
                    if (
                        len(source_raw) != int(entry["size"])
                        or hashlib.sha256(source_raw).hexdigest() != entry["sha256"]
                        or not source_raw.startswith(observed)
                    ):
                        _fail("successor_rebind_owner_runtime_incomplete_conflict")
                elif entry["kind"] == "symlink":
                    if (
                        not stat.S_ISLNK(state.st_mode)
                        or os.readlink(path) != entry.get("target")
                    ):
                        _fail("successor_rebind_owner_runtime_incomplete_conflict")
                else:
                    _fail("successor_rebind_owner_runtime_incomplete_conflict")
        if selected != quarantine:
            if os.path.lexists(quarantine):
                _fail("successor_rebind_owner_runtime_incomplete_conflict")
            _rename_noreplace(selected, quarantine)
            _fsync_directory(selected.parent)
        if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
            _fail("successor_rebind_owner_runtime_incomplete_conflict")
        shutil.rmtree(quarantine)
        _fsync_directory(quarantine.parent)
    except SuccessorRebindOwnerRuntimeError:
        raise
    except (KeyError, OSError, TypeError, ValueError) as exc:
        _fail("successor_rebind_owner_runtime_incomplete_conflict", exc)


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
    expected_launch_authority_sha256: str,
) -> None:
    """Bind builder-owned data to a separately trusted Stage-C authority."""

    expected = {
        "publication_sha256": expected_staging_publication_sha256,
        "source_tree_oid": expected_source_tree_oid,
        "stage_c_builder_terminal_receipt_sha256": (
            expected_stage_c_builder_terminal_receipt_sha256
        ),
        "launch_authority_sha256": expected_launch_authority_sha256,
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
    expected_launch_authority_sha256: str,
) -> None:
    expected = {
        "staging_publication_sha256": expected_staging_publication_sha256,
        "source_tree_oid": expected_source_tree_oid,
        "stage_c_builder_terminal_receipt_sha256": (
            expected_stage_c_builder_terminal_receipt_sha256
        ),
        "launch_authority_sha256": expected_launch_authority_sha256,
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


def _publication_from_staging(
    *,
    revision: str,
    release_base: Path,
    staging: Mapping[str, Any],
    rebased: Mapping[str, Any],
    attestation: Mapping[str, Any],
) -> dict[str, Any]:
    unsigned = {
        "schema": PUBLICATION_SCHEMA,
        "release_revision": revision,
        "source_tree_oid": staging["source_tree_oid"],
        "runtime_root": str(release_base / revision),
        "staging_publication_sha256": staging["publication_sha256"],
        "staging_manifest_sha256": staging["manifest_sha256"],
        "staging_attestation_sha256": staging["attestation_sha256"],
        "staging_tree_sha256": staging["tree_sha256"],
        "staging_interpreter_sha256": staging["interpreter_sha256"],
        "staging_pyvenv_cfg_sha256": staging["pyvenv_cfg_sha256"],
        "stage_c_builder_terminal_receipt_sha256": staging[
            "stage_c_builder_terminal_receipt_sha256"
        ],
        "launch_authority_sha256": staging["launch_authority_sha256"],
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
    return validate_publication(
        {**unsigned, "publication_sha256": _digest(unsigned)},
        revision=revision,
        release_base=release_base,
    )


def _promotion_intent(
    *,
    revision: str,
    release_base: Path,
    publication: Mapping[str, Any],
) -> dict[str, Any]:
    unsigned = {
        "schema": PROMOTION_INTENT_SCHEMA,
        "release_revision": revision,
        "runtime_root": str(release_base / revision),
        "publication": copy.deepcopy(dict(publication)),
    }
    return validate_promotion_intent(
        {**unsigned, "intent_sha256": _digest(unsigned)},
        revision=revision,
        release_base=release_base,
    )


def _verify_final_tree(
    publication: Mapping[str, Any],
    *,
    revision: str,
    release_base: Path,
    root_uid: int,
    root_gid: int,
) -> None:
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
    expected_launch_authority_sha256: str = "0" * 64,
    rename_noreplace: Callable[[Path, Path], None] = _rename_noreplace,
    after_rename: Callable[[], None] | None = None,
    after_intent_pending_fsync: Callable[[], None] | None = None,
    after_intent_link: Callable[[], None] | None = None,
    after_publication_pending_fsync: Callable[[], None] | None = None,
    after_publication_link: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Data-only root promotion under one durable, serialized intent."""

    final_root = release_base / revision
    publication_path = publication_root / f"{revision}.json"
    intent_path = publication_root / f"{revision}.promotion-intent.json"
    incomplete = release_base / f".{revision}.root-copy-incomplete"
    _ensure_publication_root(
        publication_root,
        root_uid=root_uid,
        root_gid=root_gid,
    )

    def require_external_authority(publication: Mapping[str, Any]) -> None:
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
            expected_staging_interpreter_sha256=expected_staging_interpreter_sha256,
            expected_staging_pyvenv_cfg_sha256=expected_staging_pyvenv_cfg_sha256,
            expected_launch_authority_sha256=expected_launch_authority_sha256,
        )

    def publish_verified(publication: Mapping[str, Any]) -> dict[str, Any]:
        _verify_final_tree(
            publication,
            revision=revision,
            release_base=release_base,
            root_uid=root_uid,
            root_gid=root_gid,
        )
        _write_exact(
            publication_path,
            publication,
            uid=root_uid,
            gid=root_gid,
            after_pending_fsync=after_publication_pending_fsync,
            after_final_link=after_publication_link,
            parent_locked=True,
        )
        return copy.deepcopy(dict(publication))

    with _exclusive_parent_lock(
        release_base / ".successor-runtime-promotion.lock",
        uid=root_uid,
        gid=root_gid,
    ):
        with _exclusive_parent_lock(
            publication_root / ".successor-runtime-promotion.lock",
            uid=root_uid,
            gid=root_gid,
        ):
            if os.path.lexists(final_root):
                if os.path.lexists(publication_path):
                    publication = validate_publication(
                        _decode_exact(publication_path, uid=root_uid, gid=root_gid),
                        revision=revision,
                        release_base=release_base,
                    )
                    require_external_authority(publication)
                    _verify_final_tree(
                        publication,
                        revision=revision,
                        release_base=release_base,
                        root_uid=root_uid,
                        root_gid=root_gid,
                    )
                    return publication
                intent = validate_promotion_intent(
                    _decode_exact(intent_path, uid=root_uid, gid=root_gid),
                    revision=revision,
                    release_base=release_base,
                )
                publication = intent["publication"]
                require_external_authority(publication)
                return publish_verified(publication)
            if os.path.lexists(publication_path):
                _fail("successor_rebind_owner_runtime_existing_invalid")

            existing_intent = None
            if os.path.lexists(intent_path):
                existing_intent = validate_promotion_intent(
                    _decode_exact(intent_path, uid=root_uid, gid=root_gid),
                    revision=revision,
                    release_base=release_base,
                )

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
                expected_staging_publication_sha256=(
                    expected_staging_publication_sha256
                ),
                expected_source_tree_oid=expected_source_tree_oid,
                expected_stage_c_builder_terminal_receipt_sha256=(
                    expected_stage_c_builder_terminal_receipt_sha256
                ),
                expected_owner_runtime_builder_receipt_sha256=(
                    expected_owner_runtime_builder_receipt_sha256
                ),
                expected_owner_runtime_wheel_sha256=(
                    expected_owner_runtime_wheel_sha256
                ),
                expected_staging_manifest_sha256=expected_staging_manifest_sha256,
                expected_staging_attestation_sha256=(
                    expected_staging_attestation_sha256
                ),
                expected_staging_tree_sha256=expected_staging_tree_sha256,
                expected_staging_interpreter_sha256=(
                    expected_staging_interpreter_sha256
                ),
                expected_staging_pyvenv_cfg_sha256=(
                    expected_staging_pyvenv_cfg_sha256
                ),
                expected_launch_authority_sha256=(
                    expected_launch_authority_sha256
                ),
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
            attestation = _attestation(rebased)
            publication = _publication_from_staging(
                revision=revision,
                release_base=release_base,
                staging=staging,
                rebased=rebased,
                attestation=attestation,
            )
            require_external_authority(publication)
            intent = _promotion_intent(
                revision=revision,
                release_base=release_base,
                publication=publication,
            )
            if existing_intent is not None and existing_intent != intent:
                _fail("successor_rebind_owner_runtime_existing_invalid")
            # The exact intent is durable before the first incomplete inode.
            _write_exact(
                intent_path,
                intent,
                uid=root_uid,
                gid=root_gid,
                after_pending_fsync=after_intent_pending_fsync,
                after_final_link=after_intent_link,
                parent_locked=True,
            )

            quarantine = release_base / (
                f".{revision}.{intent['intent_sha256']}.root-copy-quarantine"
            )
            if os.path.lexists(quarantine):
                _quarantine_recoverable_root_copy(
                    selected=quarantine,
                    quarantine=quarantine,
                    staging_root=staging_root,
                    staged_manifest=staged_manifest,
                    rebased_manifest=rebased,
                    root_uid=root_uid,
                    root_gid=root_gid,
                )
            incomplete_ready = False
            if os.path.lexists(incomplete):
                try:
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
                    incomplete_ready = True
                except preexec.SuccessorRuntimePreExecError:
                    _quarantine_recoverable_root_copy(
                        selected=incomplete,
                        quarantine=quarantine,
                        staging_root=staging_root,
                        staged_manifest=staged_manifest,
                        rebased_manifest=rebased,
                        root_uid=root_uid,
                        root_gid=root_gid,
                    )
            if not incomplete_ready:
                _copy_tree_exact(
                    source=staging_root,
                    destination=incomplete,
                    manifest=staged_manifest,
                    builder_uid=builder_uid,
                    builder_gid=builder_gid,
                    root_uid=root_uid,
                    root_gid=root_gid,
                )
                # Detect source mutation overlapping the stable copy.
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
                _write_exact(
                    incomplete / runtime.MANIFEST_NAME,
                    rebased,
                    uid=root_uid,
                    gid=root_gid,
                )
                incomplete.chmod(0o555)
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
            _fsync_runtime_tree(incomplete, rebased)
            _fsync_directory(release_base)
            try:
                rename_noreplace(incomplete, final_root)
            except SuccessorRebindOwnerRuntimeError:
                raise
            except OSError as exc:
                _fail("successor_rebind_owner_runtime_promotion_failed", exc)
            _fsync_directory(release_base)
            if after_rename is not None:
                after_rename()
            return publish_verified(publication)

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
    expected_launch_authority_sha256: str,
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
        expected_launch_authority_sha256=expected_launch_authority_sha256,
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


def _require_os_identity(*, uid: int, gid: int) -> None:
    try:
        observed_uid = os.geteuid()
        observed_gid = os.getegid()
    except (AttributeError, OSError) as exc:
        _fail("successor_rebind_owner_runtime_identity_unavailable", exc)
    if observed_uid != uid or observed_gid != gid or (uid == 0) != (gid == 0):
        _fail("successor_rebind_owner_runtime_identity_invalid")


def _production_source_root(revision: str) -> Path:
    return REVISION_LIBRARY_BASE / revision / "source"


def _require_exact_production_source(
    *,
    revision: str,
    source_tree_oid: str,
) -> Path:
    source = _production_source_root(revision)
    try:
        state = os.lstat(source)
    except OSError as exc:
        _fail("successor_rebind_owner_runtime_source_invalid", exc)
    if (
        _REVISION.fullmatch(revision or "") is None
        or _REVISION.fullmatch(source_tree_oid or "") is None
        or not stat.S_ISDIR(state.st_mode)
        or stat.S_ISLNK(state.st_mode)
        or state.st_uid != 0
        or state.st_gid != 0
        or stat.S_IMODE(state.st_mode) & 0o022
    ):
        _fail("successor_rebind_owner_runtime_source_invalid")
    environment = {
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }

    def git(*arguments: str) -> bytes:
        try:
            completed = subprocess.run(
                (
                    "/usr/bin/git",
                    "-c",
                    f"safe.directory={source}",
                    "-C",
                    str(source),
                    *arguments,
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=60,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _fail("successor_rebind_owner_runtime_source_invalid", exc)
        if (
            completed.returncode != 0
            or completed.stderr
            or len(completed.stdout) > 1024 * 1024
        ):
            _fail("successor_rebind_owner_runtime_source_invalid")
        return completed.stdout

    try:
        head = git("rev-parse", "HEAD").decode("ascii", errors="strict").strip()
        tree = git("rev-parse", "HEAD^{tree}").decode("ascii", errors="strict").strip()
    except UnicodeError as exc:
        _fail("successor_rebind_owner_runtime_source_invalid", exc)
    if (
        head != revision
        or tree != source_tree_oid
        or git("status", "--porcelain=v1", "--untracked-files=all")
    ):
        _fail("successor_rebind_owner_runtime_source_invalid")
    return source


def prepare_runtime(revision: str) -> Path:
    """Fixed production root phase; no target or build code is executed."""

    _require_os_identity(uid=0, gid=0)
    return prepare_staging_for_test(
        revision=revision,
        release_base=RELEASE_BASE,
        root_uid=0,
        root_gid=0,
        builder_uid=BUILDER_UID,
        builder_gid=BUILDER_GID,
    )


def build_runtime_as_dedicated_builder(
    revision: str,
    *,
    source_tree_oid: str,
    stage_c_builder_terminal_receipt_sha256: str,
    launch_authority_sha256: str = "0" * 64,
) -> dict[str, Any]:
    """Fixed production builder phase; the root identity is always rejected."""

    _require_os_identity(uid=BUILDER_UID, gid=BUILDER_GID)
    source_root = _require_exact_production_source(
        revision=revision,
        source_tree_oid=source_tree_oid,
    )

    def fixed_builder(spec: package.OwnerRuntimeBuildSpec) -> Mapping[str, Any]:
        if (
            spec.revision != revision
            or spec.source_root != source_root
            or spec.release_base != _staging_base(RELEASE_BASE, revision)
        ):
            _fail("successor_rebind_owner_runtime_builder_contract_invalid")
        return package.build_owner_runtime(
            package.OwnerRuntimeBuildSpec(
                revision=revision,
                source_root=source_root,
                release_base=spec.release_base,
                uv_executable=PRODUCTION_UV,
                git_executable=Path("/usr/bin/git"),
            )
        )

    return build_staged_for_test(
        revision=revision,
        source_root=source_root,
        release_base=RELEASE_BASE,
        builder_uid=BUILDER_UID,
        builder_gid=BUILDER_GID,
        effective_uid=os.geteuid(),
        effective_gid=os.getegid(),
        source_tree_oid=source_tree_oid,
        stage_c_builder_terminal_receipt_sha256=(
            stage_c_builder_terminal_receipt_sha256
        ),
        launch_authority_sha256=launch_authority_sha256,
        builder=fixed_builder,
    )


def validate_promotion_frame(value: Any, *, revision: str) -> dict[str, Any]:
    digest_fields = (
        "launch_authority_sha256",
        "staging_publication_sha256",
        "stage_c_builder_terminal_receipt_sha256",
        "owner_runtime_builder_receipt_sha256",
        "owner_runtime_wheel_sha256",
        "staging_manifest_sha256",
        "staging_attestation_sha256",
        "staging_tree_sha256",
        "staging_interpreter_sha256",
        "staging_pyvenv_cfg_sha256",
    )
    fields = {
        "schema",
        "release_revision",
        "source_tree_oid",
        *digest_fields,
        "secret_material_recorded",
        "secret_digest_recorded",
        "frame_sha256",
    }
    unsigned = (
        {name: item for name, item in value.items() if name != "frame_sha256"}
        if isinstance(value, Mapping)
        else {}
    )
    if (
        not isinstance(value, Mapping)
        or set(value) != fields
        or value.get("schema") != PROMOTION_FRAME_SCHEMA
        or value.get("release_revision") != revision
        or _REVISION.fullmatch(str(value.get("source_tree_oid", ""))) is None
        or not _validate_digest_fields(value, digest_fields + ("frame_sha256",))
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
        or value.get("frame_sha256") != _digest(unsigned)
    ):
        _fail("successor_rebind_owner_runtime_promotion_frame_invalid")
    return copy.deepcopy(dict(value))


def build_promotion_frame(
    *,
    revision: str,
    source_tree_oid: str,
    staging_publication_sha256: str,
    stage_c_builder_terminal_receipt_sha256: str,
    owner_runtime_builder_receipt_sha256: str,
    owner_runtime_wheel_sha256: str,
    staging_manifest_sha256: str,
    staging_attestation_sha256: str,
    staging_tree_sha256: str,
    staging_interpreter_sha256: str,
    staging_pyvenv_cfg_sha256: str,
    launch_authority_sha256: str,
) -> dict[str, Any]:
    unsigned = {
        "schema": PROMOTION_FRAME_SCHEMA,
        "release_revision": revision,
        "source_tree_oid": source_tree_oid,
        "launch_authority_sha256": launch_authority_sha256,
        "staging_publication_sha256": staging_publication_sha256,
        "stage_c_builder_terminal_receipt_sha256": (
            stage_c_builder_terminal_receipt_sha256
        ),
        "owner_runtime_builder_receipt_sha256": (owner_runtime_builder_receipt_sha256),
        "owner_runtime_wheel_sha256": owner_runtime_wheel_sha256,
        "staging_manifest_sha256": staging_manifest_sha256,
        "staging_attestation_sha256": staging_attestation_sha256,
        "staging_tree_sha256": staging_tree_sha256,
        "staging_interpreter_sha256": staging_interpreter_sha256,
        "staging_pyvenv_cfg_sha256": staging_pyvenv_cfg_sha256,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return validate_promotion_frame(
        {**unsigned, "frame_sha256": _digest(unsigned)},
        revision=revision,
    )


def promote_runtime(frame: Mapping[str, Any]) -> dict[str, Any]:
    """Fixed production root phase driven only by bound provenance bytes."""

    _require_os_identity(uid=0, gid=0)
    revision = str(frame.get("release_revision", ""))
    value = validate_promotion_frame(frame, revision=revision)
    return promote_staged_for_test(
        revision=revision,
        release_base=RELEASE_BASE,
        publication_root=PUBLICATION_ROOT,
        builder_uid=BUILDER_UID,
        builder_gid=BUILDER_GID,
        root_uid=0,
        root_gid=0,
        expected_staging_publication_sha256=value["staging_publication_sha256"],
        expected_source_tree_oid=value["source_tree_oid"],
        expected_stage_c_builder_terminal_receipt_sha256=value[
            "stage_c_builder_terminal_receipt_sha256"
        ],
        expected_owner_runtime_builder_receipt_sha256=value[
            "owner_runtime_builder_receipt_sha256"
        ],
        expected_owner_runtime_wheel_sha256=value["owner_runtime_wheel_sha256"],
        expected_staging_manifest_sha256=value["staging_manifest_sha256"],
        expected_staging_attestation_sha256=value["staging_attestation_sha256"],
        expected_staging_tree_sha256=value["staging_tree_sha256"],
        expected_staging_interpreter_sha256=value["staging_interpreter_sha256"],
        expected_staging_pyvenv_cfg_sha256=value["staging_pyvenv_cfg_sha256"],
        expected_launch_authority_sha256=value["launch_authority_sha256"],
    )


def _bind_launch_authority(
    authority: Mapping[str, Any],
    *,
    revision: str,
    create: bool,
) -> dict[str, Any]:
    checked = _validate_launch_authority(authority, revision=revision)
    path = PUBLICATION_ROOT / f"{revision}.launch-authority.json"
    if create:
        _ensure_publication_root(PUBLICATION_ROOT, root_uid=0, root_gid=0)
        _write_exact(path, checked, uid=0, gid=0)
    existing = _decode_exact(path, uid=0, gid=0)
    if existing != checked:
        _fail("successor_rebind_owner_runtime_launch_authority_conflict")
    return checked


def _read_phase_frame_stdin(*, operation: str, revision: str) -> dict[str, Any]:
    raw = sys.stdin.buffer.read(_PHASE_FRAME_MAX_BYTES + 1)
    if len(raw) > _PHASE_FRAME_MAX_BYTES or not raw.endswith(b"\n"):
        _fail("successor_rebind_owner_runtime_phase_frame_invalid")
    try:
        decoded = json.loads(raw[:-1].decode("ascii", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        _fail("successor_rebind_owner_runtime_phase_frame_invalid", exc)
    if raw != _canonical(decoded) + b"\n":
        _fail("successor_rebind_owner_runtime_phase_frame_invalid")
    return _validate_phase_frame(
        decoded,
        operation=operation,
        revision=revision,
    )


def production_main(argv: tuple[str, ...] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    try:
        if len(arguments) == 2 and arguments[0] == "prepare-runtime":
            phase = _read_phase_frame_stdin(
                operation=arguments[0],
                revision=arguments[1],
            )
            if phase["payload"] is not None:
                _fail("successor_rebind_owner_runtime_phase_frame_invalid")
            _bind_launch_authority(
                phase["launch_authority"],
                revision=arguments[1],
                create=True,
            )
            result: Any = {"staging_base": str(prepare_runtime(arguments[1]))}
        elif (
            len(arguments) == 4 and arguments[0] == "build-runtime-as-dedicated-builder"
        ):
            phase = _read_phase_frame_stdin(
                operation=arguments[0],
                revision=arguments[1],
            )
            authority = _bind_launch_authority(
                phase["launch_authority"],
                revision=arguments[1],
                create=False,
            )
            if (
                phase["payload"] is not None
                or authority["source_tree_oid"] != arguments[2]
                or authority["stage_c_builder_terminal_receipt_sha256"]
                != arguments[3]
            ):
                _fail("successor_rebind_owner_runtime_phase_frame_invalid")
            result = build_runtime_as_dedicated_builder(
                arguments[1],
                source_tree_oid=arguments[2],
                stage_c_builder_terminal_receipt_sha256=arguments[3],
                launch_authority_sha256=authority[
                    "launch_authority_sha256"
                ],
            )
        elif len(arguments) == 2 and arguments[0] == "promote-runtime":
            phase = _read_phase_frame_stdin(
                operation=arguments[0],
                revision=arguments[1],
            )
            authority = _bind_launch_authority(
                phase["launch_authority"],
                revision=arguments[1],
                create=False,
            )
            payload = phase["payload"]
            if (
                not isinstance(payload, Mapping)
                or payload.get("source_tree_oid") != authority["source_tree_oid"]
                or payload.get("stage_c_builder_terminal_receipt_sha256")
                != authority["stage_c_builder_terminal_receipt_sha256"]
                or payload.get("launch_authority_sha256")
                != authority["launch_authority_sha256"]
            ):
                _fail("successor_rebind_owner_runtime_phase_frame_invalid")
            result = promote_runtime(payload)
        else:
            _fail("successor_rebind_owner_runtime_action_invalid")
    except (OSError, SuccessorRebindOwnerRuntimeError):
        print(
            '{"error_code":"successor_rebind_owner_runtime_foundation_failed","ok":false}',
            file=sys.stderr,
        )
        return 2
    print((_canonical(result) + b"\n").decode("ascii"), end="")
    return 0


__all__ = [
    "PUBLICATION_ROOT",
    "PUBLICATION_SCHEMA",
    "PROMOTION_INTENT_SCHEMA",
    "PROMOTION_FRAME_SCHEMA",
    "RELEASE_BASE",
    "STAGING_SCHEMA",
    "SuccessorRebindOwnerRuntimeError",
    "build_promotion_frame",
    "build_runtime_as_dedicated_builder",
    "build_staged_for_test",
    "prepare_staging_for_test",
    "prepare_runtime",
    "production_main",
    "promote_runtime",
    "promote_staged_for_test",
    "validate_active_installation",
    "validate_promotion_intent",
    "validate_promotion_frame",
    "validate_publication",
    "validate_staging_publication",
]
