#!/usr/bin/env python3
"""Exact privileged installer and attestor for production storage growth.

There are only two install actions: create the fixed owner-side state root and
publish the fixed production guest entrypoint.  No path, command, target,
account, project, device, partition, filesystem, or size is caller-selected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


OWNER_STATE_ROOT = Path("/var/lib/muncho-production-storage-growth")
OWNER_INSTALLATION_RECEIPT = OWNER_STATE_ROOT / ".installation.json"
GUEST_ENTRYPOINT = Path("/usr/local/lib/muncho/production-storage-growth-guest")
GUEST_INSTALLATION_ROOT = Path(
    "/var/lib/muncho-production-storage-growth-guest"
)
GUEST_INSTALLATION_RECEIPT = GUEST_INSTALLATION_ROOT / "installation.json"
GUEST_SUDOERS_PATH = Path("/etc/sudoers.d/muncho-production-storage-growth")
GUEST_SOURCE = Path(__file__).with_name("production_storage_growth_guest.py")
GUEST_INTERPRETER = Path("/usr/bin/python3")

OWNER_INSTALLATION_SCHEMA = (
    "muncho-production-storage-growth-owner-installation.v1"
)
OWNER_ARTIFACT_BINDING_SCHEMA = (
    "muncho-production-storage-growth-owner-artifact-binding.v1"
)
OWNER_READINESS_SCHEMA = "muncho-production-storage-growth-owner-state-ready.v1"
GUEST_INSTALL_REQUEST_SCHEMA = (
    "muncho-production-storage-growth-guest-install-request.v1"
)
GUEST_INSTALLATION_SCHEMA = (
    "muncho-production-storage-growth-guest-installation.v1"
)
GUEST_READINESS_SCHEMA = "muncho-production-storage-growth-guest-readiness.v1"
GUEST_REQUEST_SCHEMA = "muncho-production-storage-growth-guest-request.v1"
GUEST_RESPONSE_SCHEMA = "muncho-production-storage-growth-guest-response.v1"

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OWNER_INSTALLATION_FIELDS = frozenset({
    "schema",
    "release_sha",
    "state_root",
    "installer_sha256",
    "sealed_artifact_binding",
    "sealed_artifact_binding_sha256",
    "installed_at_unix",
    "installation_receipt_sha256",
})
_OWNER_ARTIFACT_BINDING_FIELDS = frozenset({
    "schema",
    "release_sha",
    "owner_support_manifest_sha256",
    "owner_support_source_tree_oid",
    "plan_builder_sha256",
    "owner_cli_sha256",
    "owner_route_sha256",
    "executor_sha256",
    "adapter_sha256",
    "production_cutover_sha256",
    "guest_sha256",
    "installer_sha256",
    "runtime_artifact_attestation_sha256",
    "binding_sha256",
})
_OWNER_ARTIFACT_RELATIVES = {
    "plan_builder_sha256": (
        "scripts/canary/production_storage_growth_owner_cli.py"
    ),
    "owner_cli_sha256": (
        "scripts/canary/production_storage_growth_owner_cli.py"
    ),
    "owner_route_sha256": "scripts/canary/full_canary_owner_launcher.py",
    "executor_sha256": (
        "scripts/canary/production_storage_growth_executor.py"
    ),
    "adapter_sha256": "scripts/canary/production_storage_growth_adapter.py",
    "production_cutover_sha256": (
        "scripts/canary/production_cutover_owner_launcher.py"
    ),
    "guest_sha256": "scripts/canary/production_storage_growth_guest.py",
    "installer_sha256": (
        "scripts/canary/production_storage_growth_installer.py"
    ),
}
_GUEST_INSTALL_REQUEST_FIELDS = frozenset({
    "schema",
    "release_sha",
    "guest_source_sha256",
    "installer_sha256",
    "request_sha256",
})
_GUEST_READINESS_FIELDS = frozenset({
    "schema",
    "release_sha",
    "entrypoint",
    "entrypoint_sha256",
    "entrypoint_uid",
    "entrypoint_gid",
    "entrypoint_mode",
    "entrypoint_link_count",
    "installer_sha256",
    "interpreter_path",
    "interpreter_resolved_path",
    "interpreter_sha256",
    "installation_receipt_sha256",
    "sudoers_path",
    "sudoers_required",
    "sudoers_absent",
    "root_transport_required",
    "ready",
    "readiness_sha256",
})


class ProductionStorageInstallerError(RuntimeError):
    """Stable, secret-free installer failure."""


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for name, item in pairs:
        if name in value:
            raise ProductionStorageInstallerError(
                "production_storage_installer_json_invalid"
            )
        value[name] = item
    return value


def _reject_number(_value: str) -> None:
    raise ProductionStorageInstallerError(
        "production_storage_installer_json_invalid"
    )


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError):
        raise ProductionStorageInstallerError(
            "production_storage_installer_json_invalid"
        ) from None


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def decode_canonical_json(raw: bytes) -> Any:
    if not isinstance(raw, bytes) or not raw or len(raw) > 64 * 1024:
        raise ProductionStorageInstallerError(
            "production_storage_installer_json_invalid"
        )
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_strict_pairs,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except (json.JSONDecodeError, UnicodeError):
        raise ProductionStorageInstallerError(
            "production_storage_installer_json_invalid"
        ) from None
    if canonical_json_bytes(value) != raw:
        raise ProductionStorageInstallerError(
            "production_storage_installer_json_invalid"
        )
    return value


def source_sha256(path: Path) -> str:
    try:
        info = path.lstat()
        payload = path.read_bytes()
    except OSError:
        raise ProductionStorageInstallerError(
            "production_storage_installer_source_invalid"
        ) from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ProductionStorageInstallerError(
            "production_storage_installer_source_invalid"
        )
    return hashlib.sha256(payload).hexdigest()


def interpreter_identity(
    path: Path,
    *,
    expected_uid: int = 0,
    expected_gid: int = 0,
) -> Mapping[str, Any]:
    try:
        resolved = Path(os.path.realpath(path, strict=True))
        info = resolved.lstat()
        payload = resolved.read_bytes()
    except OSError:
        raise ProductionStorageInstallerError(
            "production_storage_guest_interpreter_invalid"
        ) from None
    if (
        path != GUEST_INTERPRETER
        or not resolved.is_absolute()
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) & 0o022
        or info.st_uid != expected_uid
        or info.st_gid != expected_gid
        or not payload
    ):
        raise ProductionStorageInstallerError(
            "production_storage_guest_interpreter_invalid"
        )
    return {
        "interpreter_path": str(GUEST_INTERPRETER),
        "interpreter_resolved_path": str(resolved),
        "interpreter_sha256": hashlib.sha256(payload).hexdigest(),
    }


def _validate_node(
    path: Path,
    *,
    directory: bool,
    mode: int,
    uid: int,
    gid: int,
) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError:
        raise ProductionStorageInstallerError(
            "production_storage_installer_storage_invalid"
        ) from None
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not expected_kind(info.st_mode)
        or stat.S_IMODE(info.st_mode) != mode
        or info.st_uid != uid
        or info.st_gid != gid
        or not directory
        and info.st_nlink != 1
    ):
        raise ProductionStorageInstallerError(
            "production_storage_installer_storage_invalid"
        )
    return info


def _ensure_directory(path: Path, *, mode: int, uid: int, gid: int) -> None:
    try:
        parent = path.parent.lstat()
    except OSError:
        raise ProductionStorageInstallerError(
            "production_storage_installer_storage_invalid"
        ) from None
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != uid
        or parent.st_gid != gid
        or stat.S_IMODE(parent.st_mode) & 0o022
    ):
        raise ProductionStorageInstallerError(
            "production_storage_installer_storage_invalid"
        )
    try:
        path.mkdir(mode=mode, exist_ok=False)
        os.chown(path, uid, gid, follow_symlinks=False)
        os.chmod(path, mode, follow_symlinks=False)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except FileExistsError:
        pass
    except OSError:
        raise ProductionStorageInstallerError(
            "production_storage_installer_storage_invalid"
        ) from None
    _validate_node(path, directory=True, mode=mode, uid=uid, gid=gid)


def _write_atomic(
    path: Path,
    payload: bytes,
    *,
    mode: int,
    uid: int,
    gid: int,
) -> None:
    if path.exists():
        _validate_node(path, directory=False, mode=mode, uid=uid, gid=gid)
    fd: int | None = None
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        os.fchmod(fd, mode)
        os.fchown(fd, uid, gid)
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError("short installer write")
            offset += written
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(temporary, path)
        temporary = None
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        raise ProductionStorageInstallerError(
            "production_storage_installer_write_failed"
        ) from None
    _validate_node(path, directory=False, mode=mode, uid=uid, gid=gid)


def _read_receipt(path: Path) -> Mapping[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError:
        raise ProductionStorageInstallerError(
            "production_storage_installer_receipt_invalid"
        ) from None
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        raise ProductionStorageInstallerError(
            "production_storage_installer_receipt_invalid"
        )
    value = decode_canonical_json(raw[:-1])
    if not isinstance(value, Mapping):
        raise ProductionStorageInstallerError(
            "production_storage_installer_receipt_invalid"
        )
    return value


def build_owner_artifact_binding(
    release_sha: str,
    runtime_artifact_attestation: Mapping[str, Any],
) -> Mapping[str, Any]:
    try:
        artifacts = runtime_artifact_attestation["artifacts"]
        unsigned = {
            "schema": OWNER_ARTIFACT_BINDING_SCHEMA,
            "release_sha": release_sha,
            "owner_support_manifest_sha256": runtime_artifact_attestation[
                "owner_support_manifest_sha256"
            ],
            "owner_support_source_tree_oid": runtime_artifact_attestation[
                "owner_support_source_tree_oid"
            ],
            "plan_builder_sha256": artifacts["plan_builder"]["sha256"],
            "owner_cli_sha256": artifacts["owner_cli"]["sha256"],
            "owner_route_sha256": artifacts["owner_route"]["sha256"],
            "executor_sha256": artifacts["executor"]["sha256"],
            "adapter_sha256": artifacts["adapter"]["sha256"],
            "production_cutover_sha256": artifacts["production_cutover"][
                "sha256"
            ],
            "guest_sha256": artifacts["guest"]["sha256"],
            "installer_sha256": artifacts["installer"]["sha256"],
            "runtime_artifact_attestation_sha256": (
                runtime_artifact_attestation["attestation_sha256"]
            ),
        }
    except (KeyError, TypeError):
        raise ProductionStorageInstallerError(
            "production_storage_owner_artifact_binding_invalid"
        ) from None
    return validate_owner_artifact_binding({
        **unsigned,
        "binding_sha256": sha256_json(unsigned),
    }, release_sha=release_sha)


def validate_owner_artifact_binding(
    value: Any,
    *,
    release_sha: str,
) -> Mapping[str, Any]:
    unsigned = {
        name: item for name, item in value.items() if name != "binding_sha256"
    } if isinstance(value, Mapping) else {}
    hash_fields = _OWNER_ARTIFACT_BINDING_FIELDS - {
        "schema",
        "release_sha",
        "owner_support_source_tree_oid",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != _OWNER_ARTIFACT_BINDING_FIELDS
        or value.get("schema") != OWNER_ARTIFACT_BINDING_SCHEMA
        or value.get("release_sha") != release_sha
        or _SHA40.fullmatch(release_sha or "") is None
        or _SHA40.fullmatch(
            str(value.get("owner_support_source_tree_oid") or "")
        )
        is None
        or any(
            _SHA256.fullmatch(str(value.get(name) or "")) is None
            for name in hash_fields
        )
        or value.get("plan_builder_sha256") != value.get("owner_cli_sha256")
        or value.get("binding_sha256") != sha256_json(unsigned)
    ):
        raise ProductionStorageInstallerError(
            "production_storage_owner_artifact_binding_invalid"
        )
    return dict(value)


def verify_owner_artifact_binding_on_disk(
    value: Mapping[str, Any],
    *,
    release_sha: str,
    installer_path: Path = Path(__file__),
) -> Mapping[str, Any]:
    binding = validate_owner_artifact_binding(value, release_sha=release_sha)
    try:
        resolved_installer = installer_path.resolve(strict=True)
        source_root = resolved_installer.parents[2]
        support_root = source_root.parent
        root = support_root.lstat()
        source = source_root.lstat()
        manifest_path = support_root / "owner-support.json"
        manifest_node = manifest_path.lstat()
        manifest_raw = manifest_path.read_bytes()
    except (OSError, IndexError):
        raise ProductionStorageInstallerError(
            "production_storage_owner_artifact_provenance_invalid"
        ) from None
    if (
        support_root.name != f"owner-support-{release_sha}"
        or resolved_installer
        != source_root / _OWNER_ARTIFACT_RELATIVES["installer_sha256"]
        or not stat.S_ISDIR(root.st_mode)
        or stat.S_IMODE(root.st_mode) != 0o500
        or not stat.S_ISDIR(source.st_mode)
        or stat.S_IMODE(source.st_mode) != 0o500
        or source.st_uid != root.st_uid
        or source.st_gid != root.st_gid
        or not stat.S_ISREG(manifest_node.st_mode)
        or stat.S_IMODE(manifest_node.st_mode) != 0o400
        or manifest_node.st_uid != root.st_uid
        or manifest_node.st_gid != root.st_gid
        or manifest_node.st_nlink != 1
        or not manifest_raw.endswith(b"\n")
        or manifest_raw.endswith(b"\n\n")
    ):
        raise ProductionStorageInstallerError(
            "production_storage_owner_artifact_provenance_invalid"
        )
    manifest = decode_canonical_json(manifest_raw[:-1])
    manifest_unsigned = {
        name: item for name, item in manifest.items() if name != "manifest_sha256"
    } if isinstance(manifest, Mapping) else {}
    if (
        not isinstance(manifest, Mapping)
        or manifest.get("release_sha") != release_sha
        or manifest.get("source_tree_oid")
        != binding["owner_support_source_tree_oid"]
        or manifest.get("manifest_sha256")
        != binding["owner_support_manifest_sha256"]
        or manifest.get("manifest_sha256") != sha256_json(manifest_unsigned)
    ):
        raise ProductionStorageInstallerError(
            "production_storage_owner_artifact_provenance_invalid"
        )
    for field, relative in _OWNER_ARTIFACT_RELATIVES.items():
        path = source_root / relative
        try:
            node = path.lstat()
            resolved = path.resolve(strict=True)
            payload = path.read_bytes()
        except OSError:
            raise ProductionStorageInstallerError(
                "production_storage_owner_artifact_provenance_invalid"
            ) from None
        if (
            resolved != path
            or not stat.S_ISREG(node.st_mode)
            or stat.S_IMODE(node.st_mode) != 0o400
            or node.st_uid != root.st_uid
            or node.st_gid != root.st_gid
            or node.st_nlink != 1
            or hashlib.sha256(payload).hexdigest() != binding[field]
        ):
            raise ProductionStorageInstallerError(
                "production_storage_owner_artifact_provenance_invalid"
            )
    return binding


def install_owner_state_root(
    release_sha: str,
    *,
    sealed_artifact_binding: Mapping[str, Any],
    state_root: Path = OWNER_STATE_ROOT,
    installation_receipt: Path | None = None,
    expected_uid: int = 0,
    expected_gid: int = 0,
    effective_uid: Callable[[], int] = os.geteuid,
    wall_clock: Callable[[], int] = lambda: int(time.time()),
    artifact_verifier: Callable[..., Mapping[str, Any]] = (
        verify_owner_artifact_binding_on_disk
    ),
) -> Mapping[str, Any]:
    """Install only the exact durable local root and its immutable identity."""

    receipt_path = installation_receipt or state_root / ".installation.json"
    binding = validate_owner_artifact_binding(
        sealed_artifact_binding,
        release_sha=release_sha,
    )
    if (
        _SHA40.fullmatch(release_sha or "") is None
        or state_root == OWNER_STATE_ROOT
        and receipt_path != OWNER_INSTALLATION_RECEIPT
        or receipt_path.parent != state_root
        or effective_uid() != 0
        or type(expected_uid) is not int
        or type(expected_gid) is not int
        or not callable(artifact_verifier)
    ):
        raise ProductionStorageInstallerError(
            "production_storage_owner_installer_invalid"
        )
    binding = artifact_verifier(binding, release_sha=release_sha)
    _ensure_directory(
        state_root,
        mode=0o700,
        uid=expected_uid,
        gid=expected_gid,
    )
    now_unix = wall_clock()
    unsigned = {
        "schema": OWNER_INSTALLATION_SCHEMA,
        "release_sha": release_sha,
        "state_root": str(OWNER_STATE_ROOT),
        "installer_sha256": binding["installer_sha256"],
        "sealed_artifact_binding": binding,
        "sealed_artifact_binding_sha256": binding["binding_sha256"],
        "installed_at_unix": now_unix,
    }
    receipt = {
        **unsigned,
        "installation_receipt_sha256": sha256_json(unsigned),
    }
    _write_atomic(
        receipt_path,
        canonical_json_bytes(receipt) + b"\n",
        mode=0o600,
        uid=expected_uid,
        gid=expected_gid,
    )
    return attest_owner_state_root(
        release_sha,
        state_root=state_root,
        installation_receipt=receipt_path,
        sealed_artifact_binding=binding,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )


def attest_owner_state_root(
    release_sha: str,
    *,
    sealed_artifact_binding: Mapping[str, Any],
    state_root: Path = OWNER_STATE_ROOT,
    installation_receipt: Path | None = None,
    expected_uid: int = 0,
    expected_gid: int = 0,
) -> Mapping[str, Any]:
    binding = validate_owner_artifact_binding(
        sealed_artifact_binding,
        release_sha=release_sha,
    )
    receipt_path = installation_receipt or state_root / ".installation.json"
    root = _validate_node(
        state_root,
        directory=True,
        mode=0o700,
        uid=expected_uid,
        gid=expected_gid,
    )
    _validate_node(
        receipt_path,
        directory=False,
        mode=0o600,
        uid=expected_uid,
        gid=expected_gid,
    )
    receipt = _read_receipt(receipt_path)
    unsigned_receipt = {
        name: item
        for name, item in receipt.items()
        if name != "installation_receipt_sha256"
    }
    if (
        _SHA40.fullmatch(release_sha or "") is None
        or set(receipt) != _OWNER_INSTALLATION_FIELDS
        or receipt.get("schema") != OWNER_INSTALLATION_SCHEMA
        or receipt.get("release_sha") != release_sha
        or receipt.get("state_root") != str(OWNER_STATE_ROOT)
        or receipt.get("installer_sha256") != binding["installer_sha256"]
        or receipt.get("sealed_artifact_binding") != binding
        or receipt.get("sealed_artifact_binding_sha256")
        != binding["binding_sha256"]
        or type(receipt.get("installed_at_unix")) is not int
        or receipt["installed_at_unix"] <= 0
        or receipt.get("installation_receipt_sha256")
        != sha256_json(unsigned_receipt)
    ):
        raise ProductionStorageInstallerError(
            "production_storage_owner_readiness_invalid"
        )
    unsigned = {
        "schema": OWNER_READINESS_SCHEMA,
        "release_sha": release_sha,
        "state_root": str(OWNER_STATE_ROOT),
        "state_root_uid": root.st_uid,
        "state_root_gid": root.st_gid,
        "state_root_mode": "0700",
        "installer_sha256": receipt["installer_sha256"],
        "sealed_artifact_binding_sha256": binding["binding_sha256"],
        "owner_support_manifest_sha256": binding[
            "owner_support_manifest_sha256"
        ],
        "installation_receipt_sha256": receipt[
            "installation_receipt_sha256"
        ],
        "ready": True,
    }
    return {**unsigned, "readiness_sha256": sha256_json(unsigned)}


def build_guest_install_request(release_sha: str) -> Mapping[str, Any]:
    if _SHA40.fullmatch(release_sha or "") is None:
        raise ProductionStorageInstallerError(
            "production_storage_guest_install_request_invalid"
        )
    unsigned = {
        "schema": GUEST_INSTALL_REQUEST_SCHEMA,
        "release_sha": release_sha,
        "guest_source_sha256": source_sha256(GUEST_SOURCE),
        "installer_sha256": source_sha256(Path(__file__)),
    }
    return {**unsigned, "request_sha256": sha256_json(unsigned)}


def validate_guest_readiness(
    value: Any,
    *,
    release_sha: str,
    guest_source_sha256: str,
    installer_sha256: str,
    expected_uid: int = 0,
    expected_gid: int = 0,
    expected_interpreter: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _GUEST_READINESS_FIELDS:
        raise ProductionStorageInstallerError(
            "production_storage_guest_readiness_invalid"
        )
    unsigned = {
        name: item for name, item in value.items() if name != "readiness_sha256"
    }
    interpreter_path = value.get("interpreter_path")
    interpreter_resolved_path = value.get("interpreter_resolved_path")
    interpreter_sha256 = value.get("interpreter_sha256")
    interpreter_matches = (
        interpreter_path == str(GUEST_INTERPRETER)
        and isinstance(interpreter_resolved_path, str)
        and re.fullmatch(r"/usr/bin/python3(?:\.[0-9]+)?", interpreter_resolved_path)
        is not None
        and _SHA256.fullmatch(str(interpreter_sha256 or "")) is not None
    )
    if expected_interpreter is not None:
        interpreter_matches = interpreter_matches and all(
            value.get(name) == expected_interpreter.get(name)
            for name in (
                "interpreter_path",
                "interpreter_resolved_path",
                "interpreter_sha256",
            )
        )
    if (
        value.get("schema") != GUEST_READINESS_SCHEMA
        or value.get("release_sha") != release_sha
        or value.get("entrypoint") != str(GUEST_ENTRYPOINT)
        or value.get("entrypoint_sha256") != guest_source_sha256
        or value.get("entrypoint_uid") != expected_uid
        or value.get("entrypoint_gid") != expected_gid
        or value.get("entrypoint_mode") != "0755"
        or value.get("entrypoint_link_count") != 1
        or value.get("installer_sha256") != installer_sha256
        or not interpreter_matches
        or _SHA256.fullmatch(
            str(value.get("installation_receipt_sha256") or "")
        )
        is None
        or value.get("sudoers_path") != str(GUEST_SUDOERS_PATH)
        or value.get("sudoers_required") is not False
        or value.get("sudoers_absent") is not True
        or value.get("root_transport_required") is not True
        or value.get("ready") is not True
        or value.get("readiness_sha256") != sha256_json(unsigned)
    ):
        raise ProductionStorageInstallerError(
            "production_storage_guest_readiness_invalid"
        )
    return dict(value)


def install_guest(
    request: Mapping[str, Any],
    *,
    source: Path = GUEST_SOURCE,
    entrypoint: Path = GUEST_ENTRYPOINT,
    installation_root: Path = GUEST_INSTALLATION_ROOT,
    installation_receipt: Path = GUEST_INSTALLATION_RECEIPT,
    sudoers_path: Path = GUEST_SUDOERS_PATH,
    interpreter: Path = GUEST_INTERPRETER,
    expected_uid: int = 0,
    expected_gid: int = 0,
    expected_interpreter_uid: int = 0,
    expected_interpreter_gid: int = 0,
    effective_uid: Callable[[], int] = os.geteuid,
    wall_clock: Callable[[], int] = lambda: int(time.time()),
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> Mapping[str, Any]:
    if (
        not isinstance(request, Mapping)
        or set(request) != _GUEST_INSTALL_REQUEST_FIELDS
        or request.get("schema") != GUEST_INSTALL_REQUEST_SCHEMA
        or _SHA40.fullmatch(str(request.get("release_sha") or "")) is None
        or request.get("guest_source_sha256") != source_sha256(source)
        or request.get("installer_sha256") != source_sha256(Path(__file__))
        or request.get("request_sha256")
        != sha256_json({
            name: item for name, item in request.items() if name != "request_sha256"
        })
        or effective_uid() != 0
        or (expected_uid, expected_gid) == (0, 0)
        and (
            entrypoint != GUEST_ENTRYPOINT
            or installation_root != GUEST_INSTALLATION_ROOT
            or installation_receipt != GUEST_INSTALLATION_RECEIPT
            or sudoers_path != GUEST_SUDOERS_PATH
        )
        or installation_receipt.parent != installation_root
    ):
        raise ProductionStorageInstallerError(
            "production_storage_guest_install_request_invalid"
        )
    try:
        sudoers_path.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        raise ProductionStorageInstallerError(
            "production_storage_guest_sudoers_state_invalid"
        ) from None
    else:
        raise ProductionStorageInstallerError(
            "production_storage_guest_sudoers_state_invalid"
        )
    interpreter_receipt = interpreter_identity(
        interpreter,
        expected_uid=expected_interpreter_uid,
        expected_gid=expected_interpreter_gid,
    )
    _ensure_directory(
        entrypoint.parent,
        mode=0o755,
        uid=expected_uid,
        gid=expected_gid,
    )
    _ensure_directory(
        installation_root,
        mode=0o700,
        uid=expected_uid,
        gid=expected_gid,
    )
    payload = source.read_bytes()
    if not payload.startswith(b"#!/usr/bin/python3\n"):
        raise ProductionStorageInstallerError(
            "production_storage_guest_interpreter_invalid"
        )
    _write_atomic(
        entrypoint,
        payload,
        mode=0o755,
        uid=expected_uid,
        gid=expected_gid,
    )
    now_unix = wall_clock()
    unsigned_receipt = {
        "schema": GUEST_INSTALLATION_SCHEMA,
        "release_sha": request["release_sha"],
        "entrypoint": str(GUEST_ENTRYPOINT),
        "entrypoint_sha256": request["guest_source_sha256"],
        "installer_sha256": request["installer_sha256"],
        **interpreter_receipt,
        "installed_at_unix": now_unix,
        "sudoers_path": str(GUEST_SUDOERS_PATH),
        "sudoers_installed": False,
    }
    receipt = {
        **unsigned_receipt,
        "installation_receipt_sha256": sha256_json(unsigned_receipt),
    }
    _write_atomic(
        installation_receipt,
        canonical_json_bytes(receipt) + b"\n",
        mode=0o600,
        uid=expected_uid,
        gid=expected_gid,
    )
    readiness_unsigned = {
        "schema": GUEST_REQUEST_SCHEMA,
        "operation": "readiness",
        "document": {},
    }
    readiness_request = {
        **readiness_unsigned,
        "request_sha256": sha256_json(readiness_unsigned),
    }
    try:
        completed = runner(
            (str(entrypoint),),
            input=canonical_json_bytes(readiness_request),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
        response = decode_canonical_json(completed.stdout)
    except (OSError, subprocess.SubprocessError):
        raise ProductionStorageInstallerError(
            "production_storage_guest_install_probe_failed"
        ) from None
    if (
        completed.returncode != 0
        or not isinstance(response, Mapping)
        or set(response)
        != {"schema", "operation", "ok", "document", "response_sha256"}
        or response.get("schema") != GUEST_RESPONSE_SCHEMA
        or response.get("operation") != "readiness"
        or response.get("ok") is not True
        or not isinstance(response.get("document"), Mapping)
        or response.get("response_sha256")
        != sha256_json({
            name: item
            for name, item in response.items()
            if name != "response_sha256"
        })
    ):
        raise ProductionStorageInstallerError(
            "production_storage_guest_install_probe_failed"
        )
    return validate_guest_readiness(
        response["document"],
        release_sha=request["release_sha"],
        guest_source_sha256=request["guest_source_sha256"],
        installer_sha256=request["installer_sha256"],
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        expected_interpreter=interpreter_receipt,
    )


def _emit(value: Mapping[str, Any]) -> None:
    sys.stdout.buffer.write(canonical_json_bytes(value) + b"\n")
    sys.stdout.buffer.flush()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    commands = parser.add_subparsers(dest="operation", required=True)
    owner = commands.add_parser("install-owner-state", allow_abbrev=False)
    owner.add_argument("--release-sha", required=True)
    commands.add_parser("install-guest", allow_abbrev=False)
    arguments = parser.parse_args(argv)
    try:
        if arguments.operation == "install-owner-state":
            raw = sys.stdin.buffer.read(64 * 1024 + 1)
            request = decode_canonical_json(raw)
            if (
                not isinstance(request, Mapping)
                or set(request) != {"sealed_artifact_binding"}
                or not isinstance(
                    request.get("sealed_artifact_binding"), Mapping
                )
            ):
                raise ProductionStorageInstallerError(
                    "production_storage_owner_installer_invalid"
                )
            result = install_owner_state_root(
                arguments.release_sha,
                sealed_artifact_binding=request["sealed_artifact_binding"],
            )
        else:
            raw = sys.stdin.buffer.read(64 * 1024 + 1)
            request = decode_canonical_json(raw)
            if not isinstance(request, Mapping):
                raise ProductionStorageInstallerError(
                    "production_storage_guest_install_request_invalid"
                )
            result = install_guest(request)
        _emit(result)
        return 0
    except ProductionStorageInstallerError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "GUEST_ENTRYPOINT",
    "GUEST_INSTALLATION_RECEIPT",
    "GUEST_INSTALLATION_ROOT",
    "GUEST_INSTALL_REQUEST_SCHEMA",
    "GUEST_READINESS_SCHEMA",
    "GUEST_SOURCE",
    "GUEST_SUDOERS_PATH",
    "OWNER_INSTALLATION_RECEIPT",
    "OWNER_ARTIFACT_BINDING_SCHEMA",
    "OWNER_READINESS_SCHEMA",
    "OWNER_STATE_ROOT",
    "ProductionStorageInstallerError",
    "attest_owner_state_root",
    "build_owner_artifact_binding",
    "build_guest_install_request",
    "canonical_json_bytes",
    "decode_canonical_json",
    "install_guest",
    "install_owner_state_root",
    "main",
    "source_sha256",
    "validate_owner_artifact_binding",
    "validate_guest_readiness",
    "verify_owner_artifact_binding_on_disk",
]
