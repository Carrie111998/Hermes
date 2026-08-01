#!/usr/bin/env python3
"""Crash-recoverable offline release deployment reconciliation.

This module is deliberately stdlib-only.  A sealed copy is installed outside
the mutable Hermes release tree and is the sole recovery/gateway start gate
for one offline deployment transaction.  It never creates authority: the only
unit-input mutations it can perform are verbatim replays of two requests that
were fully validated and sealed before ``armed.json`` was published.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


MANIFEST_SCHEMA = "muncho-offline-deploy-manifest.v1"
PREPARED_SCHEMA = "muncho-offline-deploy-prepared.v1"
ARM_INTENT_SCHEMA = "muncho-offline-deploy-arm-intent.v1"
ARMED_SCHEMA = "muncho-offline-deploy-armed.v1"
ARM_GATEWAY_SCHEMA = "muncho-offline-deploy-arm-gateway.v1"
DRAIN_SAMPLE_SCHEMA = "muncho-offline-deploy-drain-zero-sample.v1"
STOP_SCHEMA = "muncho-offline-deploy-gateway-stop.v1"
STOPPED_ENTRY_SCHEMA = "muncho-offline-deploy-gateway-stopped-entry.v1"
TERMINAL_SCHEMA = "muncho-offline-deploy-terminal.v1"
EPOCH_RECEIPT_SCHEMA = "muncho-offline-deploy-drain-epoch.v1"
HEALTH_SCHEMA = "muncho-offline-deploy-final-health.v1"
MODEL_HEALTH_SCHEMA = "muncho-offline-deploy-model-probe.v1"
SECRET_MANAGER_HEALTH_SCHEMA = (
    "muncho-offline-deploy-secret-manager-probe.v1"
)
CLOUD_SQL_HEALTH_SCHEMA = "muncho-offline-deploy-cloud-sql-probe.v1"
CANONICAL_QUERY_HEALTH_SCHEMA = (
    "muncho-offline-deploy-canonical-query-probe.v1"
)
PRIVILEGED_WRITER_HEALTH_SCHEMA = (
    "muncho-offline-deploy-privileged-writer-probe.v1"
)
DISCORD_PERMIT_SCHEMA = "muncho-offline-deploy-discord-probe-permit.v1"
DISCORD_PERMIT_CONSUMPTION_SCHEMA = (
    "muncho-offline-deploy-discord-probe-permit-consumption.v1"
)
DISCORD_HEALTH_SCHEMA = "muncho-offline-deploy-discord-probe.v1"
CLEANUP_SCHEMA = "muncho-offline-deploy-cleanup.v1"
SCAFFOLD_ATTESTATION_SCHEMA = (
    "muncho-offline-deploy-scaffold-attestation.v1"
)
INSPECTION_SCHEMA = "muncho-offline-deploy-inspection.v1"
PREPARE_SPEC_SCHEMA = "muncho-offline-deploy-prepare-spec.v1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REVISION = re.compile(r"^[0-9a-f]{40}$")
TRANSACTION_ID = re.compile(r"^[0-9a-f]{64}$")
MAX_JSON = 16 * 1024 * 1024
MAX_FILE = 64 * 1024 * 1024
MAX_SYSTEMD_OBSERVATION = 1024 * 1024
FINALIZE_ACTION = "finalize-release-unit-inputs"
ABORT_ACTION = "abort-release-unit-inputs"
PREPARE_ACTION = "prepare-release-unit-inputs"
PREAUTHORIZE_ACTION = "preauthorize-release-unit-inputs"
RELEASE_PHASE_REQUEST_SCHEMA = (
    "muncho-production-release-unit-input-rotation-command.v1"
)
RELEASE_PHASE_RESULT_SCHEMA = (
    "muncho-production-release-unit-input-rotation-command-result.v1"
)
RELEASE_ACTIVATION_BEGIN_SCHEMA = (
    "muncho-production-release-unit-input-rotation-activation-begin.v1"
)
RELEASE_FINALIZED_RECEIPT_SCHEMA = (
    "muncho-production-release-unit-input-rotation-receipt.v4"
)
RELEASE_ABORTED_RECEIPT_SCHEMA = (
    "muncho-production-release-unit-input-rotation-aborted.v1"
)
RELEASE_FIXED_INPUTS_SCHEMA = (
    "muncho-production-release-unit-inputs.v4"
)
RELEASE_UNIT_PLAN_SCHEMA = (
    "muncho-production-release-unit-input-plan.v4"
)
RELEASE_UNIT_APPROVAL_SCHEMA = (
    "muncho-production-release-unit-input-approval.v4"
)
RELEASE_UNIT_PUBLICATION_SCHEMA = (
    "muncho-production-release-unit-input-publication.v4"
)
RELEASE_UPDATE_PLAN_SCHEMA = "muncho-production-release-update-plan.v5"
RELEASE_UPDATE_APPROVAL_SCHEMA = (
    "muncho-production-release-update-approval.v1"
)
RELEASE_UPDATE_PUBLICATION_SCHEMA = (
    "muncho-production-release-update-publication.v1"
)
AUDIT_BASE_FILES = {
    "transaction.json": 0o400,
    "successor-publication.json": 0o400,
    "successor-release-update-publication.json": 0o400,
    "predecessor-trust.json": 0o400,
    "prepared-receipt.json": 0o400,
    "mutation-begin.json": 0o400,
}
AUDIT_PREDECESSOR_FILES = {
    "unit-input-plan.json": 0o400,
    "unit-input-approval.json": 0o400,
    "production-unit-inputs.json": 0o444,
}
ACTIVATION_FILE = "activation-begin.json"
ABORT_FILE = "rotation-abort-receipt.json"
FINAL_FILE = "rotation-receipt.json"
AUDIT_TERMINAL_FILES = {ACTIVATION_FILE, ABORT_FILE, FINAL_FILE}
SYSTEMD_ROOT = Path("/etc/systemd/system")
SYSTEMD_SEARCH_ROOTS = (
    Path("/etc/systemd/system.control"),
    Path("/run/systemd/system.control"),
    Path("/run/systemd/transient"),
    Path("/run/systemd/generator.early"),
    SYSTEMD_ROOT,
    Path("/etc/systemd/system.attached"),
    Path("/run/systemd/system"),
    Path("/run/systemd/system.attached"),
    Path("/run/systemd/generator"),
    Path("/usr/local/lib/systemd/system"),
    Path("/usr/lib/systemd/system"),
    Path("/lib/systemd/system"),
    Path("/run/systemd/generator.late"),
)
OFFLINE_STATE_ROOT = Path("/var/lib/muncho-offline-release-transactions")
LIBEXEC_ROOT = Path("/usr/local/libexec")
SYSTEM_PYTHON = "/usr/bin/python3"
DEPLOY_LOCK_PATH = Path("/run/muncho-auto-deploy-release.lock")
AUTHORITY_ACTIVATION_LOCK_PATH = Path(
    "/run/muncho-writer-activation.lock"
)
GATEWAY_HOME = Path("/opt/adventico-ai-platform/hermes-home")
GATEWAY_STATE_PATH = GATEWAY_HOME / "gateway_state.json"
DRAIN_LOCK_PATH = GATEWAY_HOME / ".drain_request.lock"
DRAIN_MARKER_PATH = GATEWAY_HOME / ".drain_request.json"
RECOVERY_UNIT_PREFIX = "muncho-offline-release-recovery-"
GATEWAY_DROP_IN_PREFIX = "90-muncho-offline-recovery-"
RECONCILER_PREFIX = "muncho-offline-release-reconcile-"
UNIT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:-]*\.service$")
RECOVERY_SERVICE_NAME = re.compile(
    rf"^{RECOVERY_UNIT_PREFIX}([0-9a-f]{{64}})\.service$"
)
RECOVERY_TIMER_NAME = re.compile(
    rf"^{RECOVERY_UNIT_PREFIX}([0-9a-f]{{64}})\.timer$"
)
RECONCILER_NAME = re.compile(
    rf"^{RECONCILER_PREFIX}([0-9a-f]{{64}})\.py$"
)
GATEWAY_DROP_IN_NAME = re.compile(
    rf"^{GATEWAY_DROP_IN_PREFIX}([0-9a-f]{{64}})\.conf$"
)
REQUIRED_RUNTIME_RELATIVE_PATHS = (
    ".codex-source-commit",
    ".git/HEAD",
    ".git/index",
    "gateway/canonical_writer_boundary.py",
    "gateway/canonical_writer_client.py",
    "gateway/canonical_writer_readiness.py",
    "gateway/production_model_sovereignty_runtime.py",
    "gateway/run.py",
    "pyproject.toml",
    "scripts/canary/production_cutover_activation_lock.py",
    "scripts/canary/production_cutover_owner_launcher.py",
    "scripts/canary/production_cutover_unit_input_rotation.py",
    "scripts/canary/production_release_unit_inputs_v4.py",
    "scripts/canary/production_release_update_contract.py",
    "setup.py",
)
HEALTH_FIELDS = {
    "schema",
    "manifest_sha256",
    "decision",
    "revision",
    "gateway_incarnation",
    "model_probe",
    "secret_manager_probe",
    "cloud_sql_probe",
    "canonical_query_probe",
    "privileged_writer_probe",
    "discord_probe",
    "secret_material_recorded",
    "secret_digest_recorded",
    "receipt_sha256",
}
MANIFEST_FIELDS = {
    "schema",
    "transaction_id",
    "owner_release_revision",
    "target_revision",
    "predecessor_release",
    "successor_release",
    "active_link",
    "service_unit",
    "gateway_uid",
    "gateway_gid",
    "gateway_state_path",
    "drain_lock_path",
    "drain_marker_path",
    "deploy_lock_path",
    "audit_transaction_path",
    "transaction_sha256",
    "prepared_receipt_sha256",
    "mutation_begin_sha256",
    "predecessor_trust_sha256",
    "successor_publication_sha256",
    "release_update_publication_sha256",
    "successor_fixed_inputs_sha256",
    "successor_fixed_inputs_file_sha256",
    "authorization_checked_at_unix",
    "freshness_checked_at_unix",
    "audit_inventory",
    "audit_absent",
    "predecessor_triplet",
    "successor_triplet",
    "predecessor_runtime",
    "successor_runtime",
    "reconciler_runtime",
    "scaffolding_paths",
    "requests",
    "drain_marker_template",
    "drain_mutation_capability",
    "drain_mutation_capability_sha256",
    "initial_epoch",
    "initial_marker_sha256",
    "secret_material_recorded",
    "secret_digest_recorded",
    "manifest_payload_sha256",
}


class ReconcileError(RuntimeError):
    """One stable, secret-free reconciliation failure."""


def _fail(code: str) -> None:
    raise ReconcileError(code)


def _canonical(value: Any, *, newline: bool = False) -> bytes:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8", errors="strict")
    return raw + (b"\n" if newline else b"")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _self_digest(value: Mapping[str, Any], name: str, code: str) -> None:
    if (
        SHA256.fullmatch(str(value.get(name, ""))) is None
        or value.get(name)
        != _sha(_canonical({key: item for key, item in value.items() if key != name}))
    ):
        _fail(code)


def _decode(raw: bytes, code: str) -> Mapping[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                _fail(code)
            result[key] = value
        return result

    def constant(_value: str) -> None:
        _fail(code)

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=constant,
        )
    except ReconcileError:
        raise
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ReconcileError(code) from exc
    if not isinstance(value, Mapping) or raw != _canonical(value):
        _fail(code)
    return dict(value)


def _decode_object_relaxed(raw: bytes, code: str) -> Mapping[str, Any]:
    """Decode external canonical-agnostic JSON with duplicate rejection."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                _fail(code)
            result[key] = value
        return result

    def constant(_value: str) -> None:
        _fail(code)

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=constant,
        )
    except ReconcileError:
        raise
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ReconcileError(code) from exc
    if not isinstance(value, Mapping):
        _fail(code)
    return dict(value)


def _absolute(value: Any, code: str) -> Path:
    if not isinstance(value, str):
        _fail(code)
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        _fail(code)
    return path


def _xattrs(path: Path) -> tuple[str, ...]:
    if not hasattr(os, "listxattr"):
        return ()
    try:
        return tuple(os.listxattr(path, follow_symlinks=False))
    except OSError as exc:
        raise ReconcileError("OFFLINE_DEPLOY_XATTR_INSPECTION_FAILED") from exc


def _identity(state: os.stat_result) -> tuple[int, ...]:
    return (
        state.st_dev,
        state.st_ino,
        state.st_mode,
        state.st_nlink,
        state.st_uid,
        state.st_gid,
        state.st_size,
        state.st_mtime_ns,
        state.st_ctime_ns,
    )


def _directory(
    path: Path,
    *,
    uid: int,
    gid: int,
    mode: int,
    code: str,
) -> os.stat_result:
    try:
        before = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ReconcileError(code) from exc
    if (
        resolved != path
        or stat.S_ISLNK(before.st_mode)
        or not stat.S_ISDIR(before.st_mode)
        or before.st_uid != uid
        or before.st_gid != gid
        or stat.S_IMODE(before.st_mode) != mode
        or _xattrs(path)
    ):
        _fail(code)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        reachable = path.lstat()
    finally:
        os.close(descriptor)
    if _identity(before) != _identity(opened) or _identity(before) != _identity(
        reachable
    ):
        _fail(code)
    return before


def _stable_read(
    path: Path,
    *,
    uid: int,
    gid: int,
    mode: int,
    maximum: int = MAX_FILE,
    code: str,
) -> tuple[bytes, os.stat_result]:
    descriptor: int | None = None
    try:
        before = path.lstat()
        resolved = path.resolve(strict=True)
        if (
            resolved != path
            or stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != uid
            or before.st_gid != gid
            or stat.S_IMODE(before.st_mode) != mode
            or not 0 < before.st_size <= maximum
            or _xattrs(path)
        ):
            _fail(code)
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
        raw = b"".join(chunks)
        after_open = os.fstat(descriptor)
        reachable = path.lstat()
    except ReconcileError:
        raise
    except OSError as exc:
        raise ReconcileError(code) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (
        len(raw) != before.st_size
        or len(raw) > maximum
        or _identity(before) != _identity(opened)
        or _identity(before) != _identity(after_open)
        or _identity(before) != _identity(reachable)
    ):
        _fail(code)
    return raw, before


def _file_row(path: Path, raw: bytes, state: os.stat_result) -> dict[str, Any]:
    return {
        "path": str(path),
        "byte_sha256": _sha(raw),
        "size": len(raw),
        "mode": stat.S_IMODE(state.st_mode),
        "uid": state.st_uid,
        "gid": state.st_gid,
    }


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _create_or_exact(
    path: Path,
    raw: bytes,
    *,
    uid: int,
    gid: int,
    mode: int,
    code: str,
) -> bool:
    if not path.is_absolute():
        _fail(code)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor: int | None = None
    created = False
    try:
        if os.path.lexists(path):
            observed, _state = _stable_read(
                path,
                uid=uid,
                gid=gid,
                mode=mode,
                maximum=max(len(raw), 1),
                code=code,
            )
            if observed != raw:
                _fail(code)
            return False
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, mode)
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, mode)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        _fsync_directory(path.parent)
        created = True
        observed, _state = _stable_read(
            path,
            uid=uid,
            gid=gid,
            mode=mode,
            maximum=max(len(raw), 1),
            code=code,
        )
        if observed != raw:
            _fail(code)
        return True
    except ReconcileError:
        raise
    except (FileExistsError, OSError) as exc:
        if os.path.lexists(path):
            try:
                observed, _state = _stable_read(
                    path,
                    uid=uid,
                    gid=gid,
                    mode=mode,
                    maximum=max(len(raw), 1),
                    code=code,
                )
                if observed == raw:
                    return False
            except ReconcileError:
                pass
        raise ReconcileError(code) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        if created:
            _fsync_directory(path.parent)


def _read_json_file(
    path: Path,
    *,
    uid: int,
    gid: int,
    mode: int,
    code: str,
) -> tuple[Mapping[str, Any], bytes, os.stat_result]:
    raw, state = _stable_read(
        path,
        uid=uid,
        gid=gid,
        mode=mode,
        code=code,
    )
    return _decode(raw, code), raw, state


def _current_epoch(
    *,
    boot_id_path: Path = Path("/proc/sys/kernel/random/boot_id"),
    pid1_stat_path: Path = Path("/proc/1/stat"),
) -> str:
    boot_id = ""
    pid1_start = ""
    try:
        boot_id = boot_id_path.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    try:
        tail = pid1_stat_path.read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        pid1_start = tail[19]
    except (OSError, IndexError):
        pass
    if not boot_id and not pid1_start:
        _fail("OFFLINE_DEPLOY_EPOCH_UNAVAILABLE")
    return f"{boot_id}:{pid1_start}"


def _drain_marker(
    template: Mapping[str, Any],
    epoch: str,
    *,
    transaction_sha256: str,
    capability_sha256: str,
) -> Mapping[str, Any]:
    if (
        set(template)
        != {"action", "requested_at", "principal", "suppress_notification"}
        or template.get("action") != "drain"
        or not isinstance(template.get("requested_at"), str)
        or not isinstance(template.get("principal"), str)
        or not template["principal"].startswith("muncho-offline-release:")
        or template.get("suppress_notification") is not False
        or not isinstance(epoch, str)
        or not epoch
        or SHA256.fullmatch(transaction_sha256) is None
        or SHA256.fullmatch(capability_sha256) is None
    ):
        _fail("OFFLINE_DEPLOY_DRAIN_TEMPLATE_INVALID")
    return {
        **dict(template),
        "epoch": epoch,
        "held_transaction_sha256": transaction_sha256,
        "held_mutation_capability_sha256": capability_sha256,
    }


@contextmanager
def _shared_drain_lock(
    path: Path,
    *,
    uid: int,
    gid: int,
    create: bool,
) -> Iterator[os.stat_result]:
    if create and not os.path.lexists(path):
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
            os.fchown(descriptor, uid, gid)
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            os.close(descriptor)
            _fsync_directory(path.parent)
        except FileExistsError:
            pass
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        reachable = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(reachable.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != uid
            or opened.st_gid != gid
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (opened.st_dev, opened.st_ino)
            != (reachable.st_dev, reachable.st_ino)
        ):
            _fail("OFFLINE_DEPLOY_DRAIN_LOCK_INVALID")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        reachable = path.stat(follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (
            reachable.st_dev,
            reachable.st_ino,
        ):
            _fail("OFFLINE_DEPLOY_DRAIN_LOCK_CHANGED")
        yield opened
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _replace_marker(
    path: Path,
    raw: bytes,
    *,
    uid: int,
    gid: int,
) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, 0o600)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        _fsync_directory(path.parent)
        observed, _state = _stable_read(
            path,
            uid=uid,
            gid=gid,
            mode=0o600,
            maximum=max(len(raw), 1),
            code="OFFLINE_DEPLOY_DRAIN_MARKER_INVALID",
        )
        if observed != raw:
            _fail("OFFLINE_DEPLOY_DRAIN_MARKER_INVALID")
    except ReconcileError:
        raise
    except OSError as exc:
        raise ReconcileError("OFFLINE_DEPLOY_DRAIN_MARKER_WRITE_FAILED") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _validate_republishable_marker(
    raw: bytes,
    manifest: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Accept only this transaction's exact held marker, at any epoch.

    Republish is an epoch refresh, not a generic drain-marker overwrite
    capability.  A manual drain, another transaction, or malformed state must
    remain untouched for an operator/reconciler with the matching authority.
    """

    if (
        not raw.endswith(b"\n")
        or not raw[:-1]
        or b"\n" in raw[:-1]
    ):
        _fail("OFFLINE_DEPLOY_DRAIN_MARKER_FOREIGN")
    marker = _decode(
        raw[:-1],
        "OFFLINE_DEPLOY_DRAIN_MARKER_FOREIGN",
    )
    epoch = marker.get("epoch")
    if (
        not isinstance(epoch, str)
        or not epoch
        or marker
        != _drain_marker(
            manifest["drain_marker_template"],
            epoch,
            transaction_sha256=manifest["transaction_sha256"],
            capability_sha256=manifest[
                "drain_mutation_capability_sha256"
            ],
        )
    ):
        _fail("OFFLINE_DEPLOY_DRAIN_MARKER_FOREIGN")
    return marker


def _audit_documents(
    path: Path,
    *,
    uid: int,
    gid: int,
    allow_terminal: bool,
) -> tuple[dict[str, Mapping[str, Any]], list[dict[str, Any]]]:
    _directory(
        path,
        uid=uid,
        gid=gid,
        mode=0o700,
        code="OFFLINE_DEPLOY_AUDIT_DIRECTORY_INVALID",
    )
    entries = {item.name for item in path.iterdir()}
    expected = set(AUDIT_BASE_FILES) | {"predecessor"}
    extras = entries - expected
    if (
        not allow_terminal
        and extras
        or allow_terminal
        and not extras <= AUDIT_TERMINAL_FILES
        or any(name.startswith(".") for name in entries)
    ):
        _fail("OFFLINE_DEPLOY_AUDIT_INVENTORY_INVALID")
    predecessor = path / "predecessor"
    _directory(
        predecessor,
        uid=uid,
        gid=gid,
        mode=0o700,
        code="OFFLINE_DEPLOY_AUDIT_PREDECESSOR_INVALID",
    )
    if {item.name for item in predecessor.iterdir()} != set(
        AUDIT_PREDECESSOR_FILES
    ):
        _fail("OFFLINE_DEPLOY_AUDIT_PREDECESSOR_INVALID")
    documents: dict[str, Mapping[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    for name, mode in AUDIT_BASE_FILES.items():
        value, raw, state = _read_json_file(
            path / name,
            uid=uid,
            gid=gid,
            mode=mode,
            code="OFFLINE_DEPLOY_AUDIT_FILE_INVALID",
        )
        documents[name] = value
        rows.append(_file_row(path / name, raw, state))
    for name, mode in AUDIT_PREDECESSOR_FILES.items():
        value, raw, state = _read_json_file(
            predecessor / name,
            uid=uid,
            gid=gid,
            mode=mode,
            code="OFFLINE_DEPLOY_AUDIT_PREDECESSOR_FILE_INVALID",
        )
        documents[f"predecessor/{name}"] = value
        rows.append(_file_row(predecessor / name, raw, state))
    return documents, sorted(rows, key=lambda row: row["path"])


def _validate_replay_request(
    *,
    action: str,
    request: Mapping[str, Any],
) -> Mapping[str, Any]:
    expected_fields = {
        "schema",
        "action",
        "owner_release_revision",
        "remote_stager_revision",
        "unit_input_publication",
        "release_update_publication",
        "trusted_predecessor",
        "expected_predecessor_trust_sha256",
        "prepared_receipt",
        "preauthorization_receipt",
        "expected_transaction_sha256",
        "secret_material_recorded",
        "secret_digest_recorded",
        "request_sha256",
    }
    if (
        action not in {FINALIZE_ACTION, ABORT_ACTION}
        or not isinstance(request, Mapping)
        or set(request) != expected_fields
        or request.get("schema") != RELEASE_PHASE_REQUEST_SCHEMA
        or request.get("action") != action
        or REVISION.fullmatch(
            str(request.get("owner_release_revision", ""))
        )
        is None
        or REVISION.fullmatch(
            str(request.get("remote_stager_revision", ""))
        )
        is None
        or not isinstance(
            request.get("unit_input_publication"),
            Mapping,
        )
        or not isinstance(
            request.get("release_update_publication"),
            Mapping,
        )
        or not isinstance(
            request.get("trusted_predecessor"),
            Mapping,
        )
        or not isinstance(
            request.get("prepared_receipt"),
            Mapping,
        )
        or not isinstance(
            request.get("preauthorization_receipt"),
            Mapping,
        )
        or SHA256.fullmatch(
            str(
                request.get(
                    "expected_predecessor_trust_sha256",
                    "",
                )
            )
        )
        is None
        or SHA256.fullmatch(
            str(request.get("expected_transaction_sha256", ""))
        )
        is None
        or request.get("secret_material_recorded") is not False
        or request.get("secret_digest_recorded") is not False
        or request.get("request_sha256")
        != _sha(
            _canonical({
                name: item
                for name, item in request.items()
                if name != "request_sha256"
            })
        )
    ):
        _fail("OFFLINE_DEPLOY_SEALED_REQUEST_INVALID")
    return dict(request)


def _runtime_row(
    path: Path,
    *,
    uid: int,
    gid: int,
) -> Mapping[str, Any]:
    raw, state = _stable_read(
        path,
        uid=uid,
        gid=gid,
        mode=stat.S_IMODE(path.lstat().st_mode),
        code="OFFLINE_DEPLOY_RUNTIME_FILE_INVALID",
    )
    if stat.S_IMODE(state.st_mode) & 0o022:
        _fail("OFFLINE_DEPLOY_RUNTIME_FILE_INVALID")
    return _file_row(path, raw, state)


def _row_matches(row: Mapping[str, Any], *, path: Path | None = None) -> bool:
    try:
        target = _absolute(
            str(path) if path is not None else row["path"],
            "OFFLINE_DEPLOY_ROW_INVALID",
        )
        raw, state = _stable_read(
            target,
            uid=int(row["uid"]),
            gid=int(row["gid"]),
            mode=int(row["mode"]),
            maximum=max(int(row["size"]), 1),
            code="OFFLINE_DEPLOY_ROW_INVALID",
        )
    except (KeyError, TypeError, ValueError, ReconcileError):
        return False
    return (
        len(raw) == row.get("size")
        and _sha(raw) == row.get("byte_sha256")
    )


def _manifest_value(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if (
        set(value) != MANIFEST_FIELDS
        or value.get("schema") != MANIFEST_SCHEMA
        or TRANSACTION_ID.fullmatch(str(value.get("transaction_id", "")))
        is None
        or REVISION.fullmatch(
            str(value.get("owner_release_revision", ""))
        )
        is None
        or REVISION.fullmatch(str(value.get("target_revision", ""))) is None
        or value.get("owner_release_revision") == value.get("target_revision")
        or type(value.get("gateway_uid")) is not int
        or type(value.get("gateway_gid")) is not int
        or not isinstance(value.get("drain_mutation_capability"), str)
        or len(value["drain_mutation_capability"].encode("utf-8")) != 64
        or value.get("drain_mutation_capability_sha256")
        != _sha(value["drain_mutation_capability"].encode("utf-8"))
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
    ):
        _fail("OFFLINE_DEPLOY_MANIFEST_INVALID")
    _self_digest(
        value,
        "manifest_payload_sha256",
        "OFFLINE_DEPLOY_MANIFEST_INVALID",
    )
    for name in (
        "transaction_sha256",
        "prepared_receipt_sha256",
        "mutation_begin_sha256",
        "predecessor_trust_sha256",
        "successor_publication_sha256",
        "release_update_publication_sha256",
        "successor_fixed_inputs_sha256",
        "successor_fixed_inputs_file_sha256",
        "drain_mutation_capability_sha256",
        "initial_marker_sha256",
    ):
        if SHA256.fullmatch(str(value.get(name, ""))) is None:
            _fail("OFFLINE_DEPLOY_MANIFEST_INVALID")
    return dict(value)


def load_manifest(
    path: Path,
    *,
    expected_sha256: str | None = None,
) -> Mapping[str, Any]:
    value, raw, _state = _read_json_file(
        path,
        uid=0,
        gid=0,
        mode=0o400,
        code="OFFLINE_DEPLOY_MANIFEST_INVALID",
    )
    if expected_sha256 is not None and (
        SHA256.fullmatch(expected_sha256) is None
        or _sha(raw) != expected_sha256
    ):
        _fail("OFFLINE_DEPLOY_MANIFEST_DIGEST_MISMATCH")
    return {
        **_manifest_value(value),
        "_manifest_file_sha256": _sha(raw),
    }


def _validate_audit_semantics(
    documents: Mapping[str, Mapping[str, Any]],
    audit_path: Path,
    owner_revision: str,
    target_revision: str,
) -> None:
    transaction = documents["transaction.json"]
    unit_publication = documents["successor-publication.json"]
    update_publication = documents[
        "successor-release-update-publication.json"
    ]
    trust = documents["predecessor-trust.json"]
    prepared = documents["prepared-receipt.json"]
    mutation = documents["mutation-begin.json"]
    for value, digest_name in (
        (transaction, "transaction_sha256"),
        (unit_publication, "publication_sha256"),
        (update_publication, "publication_sha256"),
        (trust, "trust_sha256"),
        (prepared, "receipt_sha256"),
        (mutation, "mutation_begin_sha256"),
    ):
        _self_digest(value, digest_name, "OFFLINE_DEPLOY_AUDIT_SEMANTICS_INVALID")
    predecessor = transaction.get("predecessor")
    successor = transaction.get("successor")
    if (
        REVISION.fullmatch(owner_revision) is None
        or REVISION.fullmatch(target_revision) is None
        or owner_revision == target_revision
        or not isinstance(predecessor, Mapping)
        or not isinstance(successor, Mapping)
        or audit_path.name
        != (
            f"{predecessor.get('plan_sha256')}-"
            f"{successor.get('publication_sha256')}"
        )
        or predecessor.get("revision") != owner_revision
        or successor.get("revision") != target_revision
        or unit_publication.get("release_revision") != target_revision
        or update_publication.get("release_revision") != target_revision
        or transaction.get("predecessor_trust_sha256")
        != trust.get("trust_sha256")
        or prepared.get("transaction_sha256")
        != transaction.get("transaction_sha256")
        or mutation.get("transaction_sha256")
        != transaction.get("transaction_sha256")
        or prepared.get("audit_transaction_path") != str(audit_path)
        or prepared.get("successor") != successor
        or prepared.get("predecessor") != predecessor
        or mutation.get("successor_publication_sha256")
        != successor.get("publication_sha256")
        or mutation.get("release_update_publication_sha256")
        != successor.get("release_update_publication_sha256")
        or mutation.get("successor_fixed_inputs_sha256")
        != successor.get("fixed_inputs_sha256")
    ):
        _fail("OFFLINE_DEPLOY_AUDIT_SEMANTICS_INVALID")


def _validate_sealed_request_bindings(
    request: Mapping[str, Any],
    *,
    action: str,
    owner_revision: str,
    target_revision: str,
    documents: Mapping[str, Mapping[str, Any]],
) -> None:
    """Bind an owner-produced exact request to the audited transaction.

    This intentionally does not construct a request.  The owner launcher is
    the only builder; the reconciler consumes, validates, hashes, and later
    replays those exact bytes verbatim.
    """
    if (
        request.get("action") != action
        or request.get("owner_release_revision") != owner_revision
        or request.get("remote_stager_revision") != target_revision
        or request.get("unit_input_publication")
        != documents["successor-publication.json"]
        or request.get("release_update_publication")
        != documents["successor-release-update-publication.json"]
        or request.get("trusted_predecessor")
        != documents["predecessor-trust.json"]
        or request.get("expected_predecessor_trust_sha256")
        != documents["predecessor-trust.json"]["trust_sha256"]
        or request.get("prepared_receipt")
        != documents["prepared-receipt.json"]
        or request.get("expected_transaction_sha256")
        != documents["transaction.json"]["transaction_sha256"]
        or request.get("preauthorization_receipt")
        != documents["mutation-begin.json"]
    ):
        _fail("OFFLINE_DEPLOY_SEALED_REQUEST_BINDING_INVALID")


def _prepared_receipt(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    unsigned = {
        "schema": PREPARED_SCHEMA,
        "manifest_sha256": manifest["_manifest_file_sha256"],
        "transaction_id": manifest["transaction_id"],
        "owner_release_revision": manifest["owner_release_revision"],
        "target_revision": manifest["target_revision"],
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {**unsigned, "receipt_sha256": _sha(_canonical(unsigned))}


def prepare_manifest(
    spec: Mapping[str, Any],
    *,
    require_root: bool = True,
    runner: Any = subprocess.run,
) -> Mapping[str, Any]:
    """Validate all pre-arm evidence and publish an immutable manifest."""

    if require_root and (not sys.platform.startswith("linux") or os.geteuid() != 0):
        _fail("OFFLINE_DEPLOY_ROOT_REQUIRED")
    expected_spec = {
        "schema",
        "transaction_id",
        "owner_release_revision",
        "target_revision",
        "predecessor_release",
        "successor_release",
        "active_link",
        "service_unit",
        "gateway_uid",
        "gateway_gid",
        "gateway_state_path",
        "drain_lock_path",
        "drain_marker_path",
        "deploy_lock_path",
        "audit_transaction_path",
        "control_dir",
        "manifest_path",
        "sealed_finalize_request_path",
        "sealed_abort_request_path",
        "runtime_relative_paths",
        "scaffolding_paths",
        "drain_marker_template",
        "secret_material_recorded",
        "secret_digest_recorded",
    }
    if (
        set(spec) != expected_spec
        or spec.get("schema") != PREPARE_SPEC_SCHEMA
        or TRANSACTION_ID.fullmatch(str(spec.get("transaction_id", ""))) is None
        or REVISION.fullmatch(str(spec.get("owner_release_revision", "")))
        is None
        or REVISION.fullmatch(str(spec.get("target_revision", ""))) is None
        or spec.get("owner_release_revision") == spec.get("target_revision")
        or type(spec.get("gateway_uid")) is not int
        or type(spec.get("gateway_gid")) is not int
        or not isinstance(spec.get("runtime_relative_paths"), list)
        or not spec["runtime_relative_paths"]
        or not isinstance(spec.get("scaffolding_paths"), Mapping)
        or spec.get("secret_material_recorded") is not False
        or spec.get("secret_digest_recorded") is not False
    ):
        _fail("OFFLINE_DEPLOY_PREPARE_SPEC_INVALID")
    owner_revision = str(spec["owner_release_revision"])
    target_revision = str(spec["target_revision"])
    gateway_uid = int(spec["gateway_uid"])
    gateway_gid = int(spec["gateway_gid"])
    predecessor_release = _absolute(
        spec["predecessor_release"], "OFFLINE_DEPLOY_PREPARE_SPEC_INVALID"
    )
    successor_release = _absolute(
        spec["successor_release"], "OFFLINE_DEPLOY_PREPARE_SPEC_INVALID"
    )
    active_link = _absolute(
        spec["active_link"], "OFFLINE_DEPLOY_PREPARE_SPEC_INVALID"
    )
    audit_path = _absolute(
        spec["audit_transaction_path"], "OFFLINE_DEPLOY_PREPARE_SPEC_INVALID"
    )
    control_dir = _absolute(
        spec["control_dir"], "OFFLINE_DEPLOY_PREPARE_SPEC_INVALID"
    )
    manifest_path = _absolute(
        spec["manifest_path"], "OFFLINE_DEPLOY_PREPARE_SPEC_INVALID"
    )
    gateway_state_path = _absolute(
        spec["gateway_state_path"],
        "OFFLINE_DEPLOY_PREPARE_SPEC_INVALID",
    )
    deploy_lock_path = _absolute(
        spec["deploy_lock_path"],
        "OFFLINE_DEPLOY_PREPARE_SPEC_INVALID",
    )
    drain_lock_path = _absolute(
        spec["drain_lock_path"],
        "OFFLINE_DEPLOY_PREPARE_SPEC_INVALID",
    )
    drain_marker_path = _absolute(
        spec["drain_marker_path"],
        "OFFLINE_DEPLOY_PREPARE_SPEC_INVALID",
    )
    expected_control_dir = (
        OFFLINE_STATE_ROOT / str(spec["transaction_id"])
    )
    if (
        spec.get("service_unit")
        != "hermes-cloud-gateway.service"
        or control_dir != expected_control_dir
        or manifest_path
        != expected_control_dir / "transaction-manifest.json"
        or manifest_path.parent != control_dir
        or active_link
        != Path("/opt/adventico-ai-platform/hermes-agent")
        or gateway_state_path
        != GATEWAY_STATE_PATH
        or deploy_lock_path != DEPLOY_LOCK_PATH
        or drain_lock_path != DRAIN_LOCK_PATH
        or drain_marker_path != DRAIN_MARKER_PATH
        or successor_release.parent != predecessor_release.parent
        or predecessor_release.name
        != f"hermes-agent-{owner_revision[:12]}"
        or successor_release.name != f"hermes-agent-{target_revision[:12]}"
        or active_link.resolve(strict=True) != predecessor_release
    ):
        _fail("OFFLINE_DEPLOY_PREPARE_ADDRESS_INVALID")
    if os.path.lexists(manifest_path):
        existing = load_manifest(manifest_path)
        expected_bindings = {
            "transaction_id": spec["transaction_id"],
            "owner_release_revision": owner_revision,
            "target_revision": target_revision,
            "predecessor_release": str(predecessor_release),
            "successor_release": str(successor_release),
            "active_link": str(active_link),
            "service_unit": spec["service_unit"],
            "gateway_uid": gateway_uid,
            "gateway_gid": gateway_gid,
            "gateway_state_path": str(gateway_state_path),
            "drain_lock_path": str(
                drain_lock_path
            ),
            "drain_marker_path": str(
                drain_marker_path
            ),
            "deploy_lock_path": str(
                deploy_lock_path
            ),
            "audit_transaction_path": str(audit_path),
            "drain_marker_template": spec["drain_marker_template"],
            "scaffolding_paths": {
                role: str(
                    _absolute(
                        raw_path,
                        "OFFLINE_DEPLOY_SCAFFOLDING_INVALID",
                    )
                )
                for role, raw_path in sorted(
                    spec["scaffolding_paths"].items()
                )
            },
        }
        if any(
            existing.get(name) != expected
            for name, expected in expected_bindings.items()
        ):
            _fail("OFFLINE_DEPLOY_PREPARE_REPLAY_CONFLICT")
        _validate_manifest_bindings(
            existing,
            allow_terminal=False,
            runner=runner,
        )
        return _prepared_receipt(existing)
    _directory(
        control_dir,
        uid=0,
        gid=0,
        mode=0o700,
        code="OFFLINE_DEPLOY_CONTROL_DIRECTORY_INVALID",
    )
    documents, audit_rows = _audit_documents(
        audit_path,
        uid=0,
        gid=0,
        allow_terminal=False,
    )
    _validate_audit_semantics(
        documents,
        audit_path,
        owner_revision,
        target_revision,
    )
    finalize_path = control_dir / "finalize-request.json"
    abort_path = control_dir / "abort-request.json"
    transferred_finalize, _finalize_state = _stable_read(
        _absolute(
            spec["sealed_finalize_request_path"],
            "OFFLINE_DEPLOY_FINALIZE_REQUEST_INVALID",
        ),
        uid=0,
        gid=0,
        mode=0o400,
        maximum=MAX_JSON,
        code="OFFLINE_DEPLOY_FINALIZE_REQUEST_INVALID",
    )
    transferred_abort, _abort_state = _stable_read(
        _absolute(
            spec["sealed_abort_request_path"],
            "OFFLINE_DEPLOY_ABORT_REQUEST_INVALID",
        ),
        uid=0,
        gid=0,
        mode=0o400,
        maximum=MAX_JSON,
        code="OFFLINE_DEPLOY_ABORT_REQUEST_INVALID",
    )
    finalize_request = _decode(
        transferred_finalize,
        "OFFLINE_DEPLOY_FINALIZE_REQUEST_INVALID",
    )
    abort_request = _decode(
        transferred_abort,
        "OFFLINE_DEPLOY_ABORT_REQUEST_INVALID",
    )
    for action, request in (
        (FINALIZE_ACTION, finalize_request),
        (ABORT_ACTION, abort_request),
    ):
        _validate_replay_request(
            action=action,
            request=request,
        )
        _validate_sealed_request_bindings(
            request,
            action=action,
            owner_revision=owner_revision,
            target_revision=target_revision,
            documents=documents,
        )
    finalize_raw = transferred_finalize
    abort_raw = transferred_abort
    _create_or_exact(
        finalize_path,
        transferred_finalize,
        uid=0,
        gid=0,
        mode=0o400,
        code="OFFLINE_DEPLOY_FINALIZE_REQUEST_INVALID",
    )
    _create_or_exact(
        abort_path,
        transferred_abort,
        uid=0,
        gid=0,
        mode=0o400,
        code="OFFLINE_DEPLOY_ABORT_REQUEST_INVALID",
    )
    relative_paths = spec["runtime_relative_paths"]
    if (
        tuple(relative_paths)
        != REQUIRED_RUNTIME_RELATIVE_PATHS
        or any(
            not isinstance(item, str)
            or not item
            or item.startswith("/")
            or ".." in Path(item).parts
            for item in relative_paths
        )
    ):
        _fail("OFFLINE_DEPLOY_RUNTIME_PATH_INVALID")
    predecessor_rows = []
    successor_rows = []
    for relative in relative_paths:
        predecessor = predecessor_release / relative
        successor = successor_release / relative
        predecessor_rows.append(
            _runtime_row(predecessor, uid=gateway_uid, gid=gateway_gid)
        )
        successor_rows.append(
            _runtime_row(successor, uid=gateway_uid, gid=gateway_gid)
        )
    for release, revision in (
        (predecessor_release, owner_revision),
        (successor_release, target_revision),
    ):
        marker_raw, _marker_state = _stable_read(
            release / ".codex-source-commit",
            uid=gateway_uid,
            gid=gateway_gid,
            mode=next(
                int(row["mode"])
                for row in (
                    predecessor_rows
                    if release == predecessor_release
                    else successor_rows
                )
                if row["path"]
                == str(release / ".codex-source-commit")
            ),
            maximum=41,
            code="OFFLINE_DEPLOY_RUNTIME_SOURCE_IDENTITY_INVALID",
        )
        if marker_raw != f"{revision}\n".encode("ascii"):
            _fail("OFFLINE_DEPLOY_RUNTIME_SOURCE_IDENTITY_INVALID")
    required_roles = {
        "reconciler",
        "gateway_dropin",
        "recovery_unit",
        "recovery_timer",
    }
    if set(spec["scaffolding_paths"]) != required_roles:
        _fail("OFFLINE_DEPLOY_SCAFFOLDING_INVALID")
    scaffolding_paths = {
        role: str(
            _absolute(raw_path, "OFFLINE_DEPLOY_SCAFFOLDING_INVALID")
        )
        for role, raw_path in sorted(spec["scaffolding_paths"].items())
    }
    reconciler_runtime = _runtime_row(
        Path(scaffolding_paths["reconciler"]),
        uid=0,
        gid=0,
    )
    reconciler_sha256 = reconciler_runtime["byte_sha256"]
    recovery_stem = (
        f"{RECOVERY_UNIT_PREFIX}{spec['transaction_id']}"
    )
    expected_scaffolding_paths = {
        "reconciler": str(
            LIBEXEC_ROOT
            / f"{RECONCILER_PREFIX}{reconciler_sha256}.py"
        ),
        "gateway_dropin": str(
            SYSTEMD_ROOT
            / "hermes-cloud-gateway.service.d"
            / (
                f"{GATEWAY_DROP_IN_PREFIX}"
                f"{spec['transaction_id']}.conf"
            )
        ),
        "recovery_unit": str(
            SYSTEMD_ROOT / f"{recovery_stem}.service"
        ),
        "recovery_timer": str(
            SYSTEMD_ROOT / f"{recovery_stem}.timer"
        ),
    }
    if scaffolding_paths != expected_scaffolding_paths:
        _fail("OFFLINE_DEPLOY_SCAFFOLDING_INVALID")
    prepared = documents["prepared-receipt.json"]
    mutation = documents["mutation-begin.json"]
    transaction = documents["transaction.json"]
    successor = transaction["successor"]
    predecessor_dir = audit_path / "predecessor"
    live_paths = {
        "plan": _absolute(
            prepared["live_plan_path"], "OFFLINE_DEPLOY_TRIPLET_INVALID"
        ),
        "approval": _absolute(
            prepared["live_approval_path"], "OFFLINE_DEPLOY_TRIPLET_INVALID"
        ),
        "fixed": _absolute(
            prepared["live_fixed_inputs_path"], "OFFLINE_DEPLOY_TRIPLET_INVALID"
        ),
    }
    predecessor_triplet = []
    for logical, name in (
        ("plan", "unit-input-plan.json"),
        ("approval", "unit-input-approval.json"),
        ("fixed", "production-unit-inputs.json"),
    ):
        audit_row = next(
            row
            for row in audit_rows
            if row["path"] == str(predecessor_dir / name)
        )
        live_raw, live_state = _stable_read(
            live_paths[logical],
            uid=0,
            gid=0,
            mode=AUDIT_PREDECESSOR_FILES[name],
            code="OFFLINE_DEPLOY_LIVE_PREDECESSOR_INVALID",
        )
        if _sha(live_raw) != audit_row["byte_sha256"]:
            _fail("OFFLINE_DEPLOY_LIVE_PREDECESSOR_INVALID")
        predecessor_triplet.append({
            "logical": logical,
            "live_path": str(live_paths[logical]),
            "audit_path": audit_row["path"],
            "byte_sha256": audit_row["byte_sha256"],
            "size": len(live_raw),
            "mode": stat.S_IMODE(live_state.st_mode),
        })
    unit_publication = documents["successor-publication.json"]
    successor_triplet = [
        {
            "logical": "plan",
            "live_path": str(live_paths["plan"]),
            "byte_sha256": _sha(_canonical(unit_publication["plan"])),
            "mode": 0o400,
        },
        {
            "logical": "approval",
            "live_path": str(live_paths["approval"]),
            "byte_sha256": _sha(_canonical(unit_publication["approval"])),
            "mode": 0o400,
        },
        {
            "logical": "fixed",
            "live_path": str(live_paths["fixed"]),
            "byte_sha256": successor["fixed_inputs_file_sha256"],
            "mode": 0o444,
        },
    ]
    epoch = _current_epoch()
    marker_template = spec["drain_marker_template"]
    mutation_capability = secrets.token_hex(32)
    mutation_capability_sha256 = _sha(
        mutation_capability.encode("utf-8")
    )
    initial_marker_raw = _canonical(
        _drain_marker(
            marker_template,
            epoch,
            transaction_sha256=transaction["transaction_sha256"],
            capability_sha256=mutation_capability_sha256,
        ),
        newline=True,
    )
    request_rows = {
        "finalize": {
            "action": FINALIZE_ACTION,
            "path": str(finalize_path),
            "byte_sha256": _sha(finalize_raw),
            "size": len(finalize_raw),
            "request_sha256": finalize_request["request_sha256"],
        },
        "abort": {
            "action": ABORT_ACTION,
            "path": str(abort_path),
            "byte_sha256": _sha(abort_raw),
            "size": len(abort_raw),
            "request_sha256": abort_request["request_sha256"],
        },
    }
    unsigned = {
        "schema": MANIFEST_SCHEMA,
        "transaction_id": spec["transaction_id"],
        "owner_release_revision": owner_revision,
        "target_revision": target_revision,
        "predecessor_release": str(predecessor_release),
        "successor_release": str(successor_release),
        "active_link": str(active_link),
        "service_unit": spec["service_unit"],
        "gateway_uid": gateway_uid,
        "gateway_gid": gateway_gid,
        "gateway_state_path": str(gateway_state_path),
        "drain_lock_path": str(
            drain_lock_path
        ),
        "drain_marker_path": str(
            drain_marker_path
        ),
        "deploy_lock_path": str(
            deploy_lock_path
        ),
        "audit_transaction_path": str(audit_path),
        "transaction_sha256": transaction["transaction_sha256"],
        "prepared_receipt_sha256": prepared["receipt_sha256"],
        "mutation_begin_sha256": mutation["mutation_begin_sha256"],
        "predecessor_trust_sha256": transaction[
            "predecessor_trust_sha256"
        ],
        "successor_publication_sha256": successor[
            "publication_sha256"
        ],
        "release_update_publication_sha256": successor[
            "release_update_publication_sha256"
        ],
        "successor_fixed_inputs_sha256": successor["fixed_inputs_sha256"],
        "successor_fixed_inputs_file_sha256": successor[
            "fixed_inputs_file_sha256"
        ],
        "authorization_checked_at_unix": transaction[
            "authorization_checked_at_unix"
        ],
        "freshness_checked_at_unix": mutation[
            "freshness_checked_at_unix"
        ],
        "audit_inventory": audit_rows,
        "audit_absent": [
            str(audit_path / name)
            for name in sorted(AUDIT_TERMINAL_FILES)
        ],
        "predecessor_triplet": predecessor_triplet,
        "successor_triplet": successor_triplet,
        "predecessor_runtime": predecessor_rows,
        "successor_runtime": successor_rows,
        "reconciler_runtime": reconciler_runtime,
        "scaffolding_paths": scaffolding_paths,
        "requests": request_rows,
        "drain_marker_template": dict(marker_template),
        "drain_mutation_capability": mutation_capability,
        "drain_mutation_capability_sha256": mutation_capability_sha256,
        "initial_epoch": epoch,
        "initial_marker_sha256": _sha(initial_marker_raw),
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    manifest = {
        **unsigned,
        "manifest_payload_sha256": _sha(_canonical(unsigned)),
    }
    _manifest_value(manifest)
    _create_or_exact(
        manifest_path,
        _canonical(manifest),
        uid=0,
        gid=0,
        mode=0o400,
        code="OFFLINE_DEPLOY_MANIFEST_INVALID",
    )
    return _prepared_receipt(load_manifest(manifest_path))


def _read_bound_request(
    manifest: Mapping[str, Any],
    name: str,
) -> tuple[Mapping[str, Any], bytes]:
    descriptor = manifest["requests"].get(name)
    if not isinstance(descriptor, Mapping):
        _fail("OFFLINE_DEPLOY_REQUEST_DESCRIPTOR_INVALID")
    path = _absolute(
        descriptor.get("path"), "OFFLINE_DEPLOY_REQUEST_DESCRIPTOR_INVALID"
    )
    raw, _state = _stable_read(
        path,
        uid=0,
        gid=0,
        mode=0o400,
        maximum=int(descriptor.get("size", 0)),
        code="OFFLINE_DEPLOY_REQUEST_INVALID",
    )
    if (
        len(raw) != descriptor.get("size")
        or _sha(raw) != descriptor.get("byte_sha256")
    ):
        _fail("OFFLINE_DEPLOY_REQUEST_INVALID")
    value = _decode(raw, "OFFLINE_DEPLOY_REQUEST_INVALID")
    if (
        value.get("action") != descriptor.get("action")
        or value.get("request_sha256") != descriptor.get("request_sha256")
    ):
        _fail("OFFLINE_DEPLOY_REQUEST_INVALID")
    return value, raw


def _validate_manifest_bindings(
    manifest: Mapping[str, Any],
    *,
    allow_terminal: bool,
    runner: Any = subprocess.run,
) -> None:
    audit_path = Path(manifest["audit_transaction_path"])
    _documents, rows = _audit_documents(
        audit_path,
        uid=0,
        gid=0,
        allow_terminal=allow_terminal,
    )
    expected_rows = manifest["audit_inventory"]
    if rows != expected_rows:
        _fail("OFFLINE_DEPLOY_AUDIT_BINDING_CHANGED")
    for row in manifest["predecessor_runtime"]:
        if not _row_matches(row):
            _fail("OFFLINE_DEPLOY_PREDECESSOR_RUNTIME_CHANGED")
    for row in manifest["successor_runtime"]:
        if not _row_matches(row):
            _fail("OFFLINE_DEPLOY_SUCCESSOR_RUNTIME_CHANGED")
    if not _row_matches(manifest["reconciler_runtime"]):
        _fail("OFFLINE_DEPLOY_RECONCILER_CHANGED")
    for name in ("finalize", "abort"):
        request, _raw = _read_bound_request(manifest, name)
        _validate_replay_request(
            action=request["action"],
            request=request,
        )
        _validate_sealed_request_bindings(
            request,
            action=request["action"],
            owner_revision=manifest["owner_release_revision"],
            target_revision=manifest["target_revision"],
            documents=_documents,
        )


def _armed_path(manifest_path: Path) -> Path:
    return manifest_path.with_name("armed.json")


def _arm_intent_path(manifest_path: Path) -> Path:
    return manifest_path.with_name("arm-intent.json")


def _terminal_path(manifest_path: Path) -> Path:
    return manifest_path.with_name("terminal.json")


def _health_dir(manifest_path: Path) -> Path:
    return manifest_path.with_name("final-health")


def _health_path(manifest_path: Path, incarnation_sha256: str) -> Path:
    if SHA256.fullmatch(incarnation_sha256) is None:
        _fail("OFFLINE_DEPLOY_HEALTH_INVALID")
    return _health_dir(manifest_path) / f"{incarnation_sha256}.json"


def _cleanup_path(manifest_path: Path) -> Path:
    return manifest_path.with_name("cleanup-commit.json")


def _scaffold_attestation_path(manifest_path: Path) -> Path:
    return manifest_path.with_name("scaffold-attestation.json")


def _arm_gateway_path(manifest_path: Path) -> Path:
    return manifest_path.with_name("arm-gateway.json")


def _drain_sample_path(manifest_path: Path, index: int) -> Path:
    if index not in {1, 2}:
        _fail("OFFLINE_DEPLOY_DRAIN_SAMPLE_INVALID")
    return manifest_path.with_name(f"drain-zero-sample-{index}.json")


def _stop_path(manifest_path: Path) -> Path:
    return manifest_path.with_name("gateway-stop.json")


def _epoch_dir(manifest_path: Path) -> Path:
    return manifest_path.parent / "drain-epochs"


def _validate_arm_intent(
    path: Path,
    manifest: Mapping[str, Any],
) -> Mapping[str, Any]:
    value, _raw, _state = _read_json_file(
        path,
        uid=0,
        gid=0,
        mode=0o400,
        code="OFFLINE_DEPLOY_ARM_INTENT_INVALID",
    )
    if (
        set(value)
        != {
            "schema",
            "manifest_sha256",
            "transaction_id",
            "initial_epoch_sha256",
            "arm_gateway_receipt_sha256",
            "secret_material_recorded",
            "secret_digest_recorded",
            "intent_sha256",
        }
        or value.get("schema") != ARM_INTENT_SCHEMA
        or value.get("manifest_sha256") != manifest["_manifest_file_sha256"]
        or value.get("transaction_id") != manifest["transaction_id"]
        or value.get("initial_epoch_sha256")
        != _sha(manifest["initial_epoch"].encode("utf-8"))
        or value.get("arm_gateway_receipt_sha256")
        != _read_arm_gateway(path.with_name("transaction-manifest.json"), manifest)[
            "receipt_sha256"
        ]
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
    ):
        _fail("OFFLINE_DEPLOY_ARM_INTENT_INVALID")
    _self_digest(
        value,
        "intent_sha256",
        "OFFLINE_DEPLOY_ARM_INTENT_INVALID",
    )
    return value


def _validate_armed(path: Path, manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    value, _raw, _state = _read_json_file(
        path,
        uid=0,
        gid=0,
        mode=0o400,
        code="OFFLINE_DEPLOY_ARMED_INVALID",
    )
    if (
        set(value)
        != {
            "schema",
            "manifest_sha256",
            "transaction_id",
            "arm_gateway_receipt_sha256",
            "secret_material_recorded",
            "secret_digest_recorded",
            "armed_sha256",
        }
        or value.get("schema") != ARMED_SCHEMA
        or value.get("manifest_sha256") != manifest["_manifest_file_sha256"]
        or value.get("transaction_id") != manifest["transaction_id"]
        or value.get("arm_gateway_receipt_sha256")
        != _read_arm_gateway(path.with_name("transaction-manifest.json"), manifest)[
            "receipt_sha256"
        ]
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
    ):
        _fail("OFFLINE_DEPLOY_ARMED_INVALID")
    _self_digest(value, "armed_sha256", "OFFLINE_DEPLOY_ARMED_INVALID")
    return value


def _systemctl(
    args: Sequence[str],
    *,
    runner: Any = subprocess.run,
) -> str:
    try:
        completed = runner(
            ("/usr/bin/systemctl", *args),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin"},
            shell=False,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReconcileError("OFFLINE_DEPLOY_SYSTEMD_ATTESTATION_FAILED") from exc
    if completed.returncode != 0 or not isinstance(completed.stdout, bytes):
        _fail("OFFLINE_DEPLOY_SYSTEMD_ATTESTATION_FAILED")
    try:
        return completed.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeError as exc:
        raise ReconcileError("OFFLINE_DEPLOY_SYSTEMD_ATTESTATION_FAILED") from exc


def _systemd_unit_bytes(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8")


def _scaffold_contract(
    manifest_path: Path,
    manifest: Mapping[str, Any],
) -> Mapping[str, Any]:
    transaction_id = str(manifest["transaction_id"])
    gateway_unit = str(manifest["service_unit"])
    reconciler_row = manifest.get("reconciler_runtime")
    if (
        gateway_unit != "hermes-cloud-gateway.service"
        or UNIT_NAME.fullmatch(gateway_unit) is None
        or not isinstance(reconciler_row, Mapping)
        or SHA256.fullmatch(
            str(reconciler_row.get("byte_sha256", ""))
        )
        is None
    ):
        _fail("OFFLINE_DEPLOY_SCAFFOLDING_INVALID")
    reconciler_sha256 = str(reconciler_row["byte_sha256"])
    state_directory = OFFLINE_STATE_ROOT / transaction_id
    reconciler_path = LIBEXEC_ROOT / (
        f"{RECONCILER_PREFIX}{reconciler_sha256}.py"
    )
    recovery_stem = f"{RECOVERY_UNIT_PREFIX}{transaction_id}"
    recovery_service = f"{recovery_stem}.service"
    recovery_timer = f"{recovery_stem}.timer"
    recovery_unit_path = SYSTEMD_ROOT / recovery_service
    recovery_timer_path = SYSTEMD_ROOT / recovery_timer
    gateway_dropin_path = (
        SYSTEMD_ROOT
        / f"{gateway_unit}.d"
        / f"{GATEWAY_DROP_IN_PREFIX}{transaction_id}.conf"
    )
    expected_roles = {
        "reconciler": str(reconciler_path),
        "gateway_dropin": str(gateway_dropin_path),
        "recovery_unit": str(recovery_unit_path),
        "recovery_timer": str(recovery_timer_path),
    }
    if (
        manifest_path != state_directory / "transaction-manifest.json"
        or manifest.get("active_link")
        != "/opt/adventico-ai-platform/hermes-agent"
        or manifest.get("scaffolding_paths") != expected_roles
        or reconciler_row.get("path") != str(reconciler_path)
    ):
        _fail("OFFLINE_DEPLOY_SCAFFOLDING_INVALID")

    def argv(verb: str) -> tuple[str, ...]:
        return (
            SYSTEM_PYTHON,
            "-I",
            "-S",
            "-B",
            str(reconciler_path),
            verb,
            f"--manifest={manifest_path}",
            "--manifest-sha256="
            f"{manifest['_manifest_file_sha256']}",
        )

    authorize_argv = argv("authorize-start")
    reconcile_argv = argv("reconcile")
    recovery_content = _systemd_unit_bytes(
        "[Unit]",
        "Description=Recover one armed Muncho offline release transaction",
        "DefaultDependencies=no",
        "Requires=local-fs.target",
        "After=local-fs.target",
        f"Before={gateway_unit} shutdown.target",
        "Conflicts=shutdown.target",
        "StartLimitIntervalSec=0",
        "RequiresMountsFor="
        f"{state_directory} {reconciler_path} "
        "/opt/adventico-ai-platform/hermes-agent",
        "",
        "[Service]",
        "Type=oneshot",
        "User=root",
        "Group=root",
        "UMask=0077",
        "ExecStart=" + " ".join(reconcile_argv),
        "TimeoutStartSec=240s",
        "TimeoutStopSec=30s",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
    )
    timer_content = _systemd_unit_bytes(
        "[Unit]",
        "Description=Retry one armed Muncho offline release recovery",
        "After=local-fs.target",
        "",
        "[Timer]",
        "OnActiveSec=5s",
        "OnUnitActiveSec=30s",
        "OnUnitInactiveSec=30s",
        "AccuracySec=1s",
        "RandomizedDelaySec=0",
        f"Unit={recovery_service}",
        "",
        "[Install]",
        "WantedBy=timers.target",
    )
    gateway_content = _systemd_unit_bytes(
        "# Managed by one armed Muncho offline release transaction.",
        "# Removing this before durable final health is forbidden.",
        "[Unit]",
        f"Wants={recovery_service}",
        f"After={recovery_service}",
        "",
        "[Service]",
        "ExecCondition=+"
        + " ".join(authorize_argv),
    )
    return {
        "state_directory": str(state_directory),
        "reconciler_path": str(reconciler_path),
        "recovery_service": recovery_service,
        "recovery_timer": recovery_timer,
        "gateway_unit": gateway_unit,
        "authorize_argv": authorize_argv,
        "reconcile_argv": reconcile_argv,
        "artifacts": {
            str(recovery_unit_path): recovery_content,
            str(recovery_timer_path): timer_content,
            str(gateway_dropin_path): gateway_content,
        },
    }


def _parse_systemd_properties(
    raw: str,
    expected: frozenset[str],
) -> Mapping[str, str]:
    if (
        not raw
        or len(raw.encode("utf-8", errors="strict"))
        > MAX_SYSTEMD_OBSERVATION
    ):
        _fail("OFFLINE_DEPLOY_SYSTEMD_ATTESTATION_FAILED")
    observed: dict[str, str] = {}
    for line in raw.splitlines():
        name, separator, value = line.partition("=")
        if (
            not separator
            or not name
            or name in observed
        ):
            _fail("OFFLINE_DEPLOY_SYSTEMD_ATTESTATION_FAILED")
        observed[name] = value
    if set(observed) != expected:
        _fail("OFFLINE_DEPLOY_SYSTEMD_ATTESTATION_FAILED")
    return observed


def _parse_exec_ex(value: str) -> Mapping[str, Any]:
    if not value.startswith("{ ") or not value.endswith(" }"):
        _fail("OFFLINE_DEPLOY_SYSTEMD_ATTESTATION_FAILED")
    fields: dict[str, str] = {}
    for component in value[2:-2].split(" ; "):
        name, separator, item = component.partition("=")
        if not separator or not name or name in fields:
            _fail("OFFLINE_DEPLOY_SYSTEMD_ATTESTATION_FAILED")
        fields[name] = item
    if set(fields) != {
        "path",
        "argv[]",
        "flags",
        "start_time",
        "stop_time",
        "pid",
        "code",
        "status",
    }:
        _fail("OFFLINE_DEPLOY_SYSTEMD_ATTESTATION_FAILED")
    argv = tuple(fields["argv[]"].split(" "))
    if (
        not argv
        or any(not item for item in argv)
        or fields["path"] != argv[0]
        or not fields["pid"].isdigit()
    ):
        _fail("OFFLINE_DEPLOY_SYSTEMD_ATTESTATION_FAILED")
    return {
        "argv": argv,
        "flags": fields["flags"],
    }


def _reserved_files(
    directory: Path,
    pattern: re.Pattern[str],
) -> frozenset[str]:
    try:
        entries = tuple(directory.iterdir())
    except FileNotFoundError:
        return frozenset()
    except OSError as exc:
        raise ReconcileError(
            "OFFLINE_DEPLOY_RESERVED_NAMESPACE_INVALID"
        ) from exc
    return frozenset(
        str(entry)
        for entry in entries
        if pattern.fullmatch(entry.name) is not None
    )


def _reserved_gateway_dropins() -> frozenset[str]:
    observed: set[str] = set()
    for root in SYSTEMD_SEARCH_ROOTS:
        try:
            parents = tuple(root.iterdir())
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ReconcileError(
                "OFFLINE_DEPLOY_RESERVED_NAMESPACE_INVALID"
            ) from exc
        for parent in parents:
            if (
                not parent.name.endswith(".service.d")
                or not parent.is_dir()
            ):
                continue
            try:
                entries = tuple(parent.iterdir())
            except OSError as exc:
                raise ReconcileError(
                    "OFFLINE_DEPLOY_RESERVED_NAMESPACE_INVALID"
                ) from exc
            observed.update(
                str(entry)
                for entry in entries
                if GATEWAY_DROP_IN_NAME.fullmatch(entry.name)
                is not None
            )
    return frozenset(observed)


def _listed_unit_files(
    raw: str,
    pattern: re.Pattern[str],
) -> Mapping[str, str]:
    states: dict[str, str] = {}
    if len(raw.encode("utf-8", errors="strict")) > MAX_SYSTEMD_OBSERVATION:
        _fail("OFFLINE_DEPLOY_RESERVED_NAMESPACE_INVALID")
    for line in raw.splitlines():
        fields = line.split()
        if (
            len(fields) not in {2, 3}
            or pattern.fullmatch(fields[0]) is None
            or fields[0] in states
        ):
            _fail("OFFLINE_DEPLOY_RESERVED_NAMESPACE_INVALID")
        states[fields[0]] = fields[1]
    return states


def _listed_loaded_units(
    raw: str,
    pattern: re.Pattern[str],
) -> Mapping[str, tuple[str, str, str]]:
    states: dict[str, tuple[str, str, str]] = {}
    if len(raw.encode("utf-8", errors="strict")) > MAX_SYSTEMD_OBSERVATION:
        _fail("OFFLINE_DEPLOY_RESERVED_NAMESPACE_INVALID")
    for line in raw.splitlines():
        fields = line.split()
        if (
            len(fields) < 4
            or pattern.fullmatch(fields[0]) is None
            or fields[0] in states
        ):
            _fail("OFFLINE_DEPLOY_RESERVED_NAMESPACE_INVALID")
        states[fields[0]] = (fields[1], fields[2], fields[3])
    return states


def _reserved_namespace(
    contract: Mapping[str, Any],
    *,
    runner: Any,
) -> Mapping[str, list[str]]:
    service_unit_files = _listed_unit_files(
        _systemctl(
            (
                "list-unit-files",
                "--full",
                "--no-legend",
                "--no-pager",
                "--type=service",
                f"{RECOVERY_UNIT_PREFIX}*.service",
            ),
            runner=runner,
        ),
        RECOVERY_SERVICE_NAME,
    )
    timer_unit_files = _listed_unit_files(
        _systemctl(
            (
                "list-unit-files",
                "--full",
                "--no-legend",
                "--no-pager",
                "--type=timer",
                f"{RECOVERY_UNIT_PREFIX}*.timer",
            ),
            runner=runner,
        ),
        RECOVERY_TIMER_NAME,
    )
    loaded_services = _listed_loaded_units(
        _systemctl(
            (
                "list-units",
                "--full",
                "--all",
                "--plain",
                "--no-legend",
                "--no-pager",
                "--type=service",
                f"{RECOVERY_UNIT_PREFIX}*.service",
            ),
            runner=runner,
        ),
        RECOVERY_SERVICE_NAME,
    )
    loaded_timers = _listed_loaded_units(
        _systemctl(
            (
                "list-units",
                "--full",
                "--all",
                "--plain",
                "--no-legend",
                "--no-pager",
                "--type=timer",
                f"{RECOVERY_UNIT_PREFIX}*.timer",
            ),
            runner=runner,
        ),
        RECOVERY_TIMER_NAME,
    )
    expected_service = str(contract["recovery_service"])
    expected_timer = str(contract["recovery_timer"])
    if (
        service_unit_files != {expected_service: "enabled"}
        or timer_unit_files != {expected_timer: "enabled"}
        or set(loaded_services) != {expected_service}
        or loaded_services[expected_service][0] != "loaded"
        or loaded_services[expected_service][1]
        not in {"inactive", "activating", "active"}
        or set(loaded_timers) != {expected_timer}
        or loaded_timers[expected_timer]
        != ("loaded", "active", "waiting")
    ):
        _fail("OFFLINE_DEPLOY_RESERVED_NAMESPACE_INVALID")
    active_services = frozenset(
        name
        for name, (_load, active, _sub) in loaded_services.items()
        if active in {"activating", "active"}
    )
    active_timers = frozenset(
        name
        for name, (_load, active, _sub) in loaded_timers.items()
        if active == "active"
    )
    observed = {
        "recovery_service_files": frozenset().union(*(
            _reserved_files(root, RECOVERY_SERVICE_NAME)
            for root in SYSTEMD_SEARCH_ROOTS
        )),
        "recovery_timer_files": frozenset().union(*(
            _reserved_files(root, RECOVERY_TIMER_NAME)
            for root in SYSTEMD_SEARCH_ROOTS
        )),
        "gateway_drop_in_files": _reserved_gateway_dropins(),
        "reconciler_files": _reserved_files(
            LIBEXEC_ROOT,
            RECONCILER_NAME,
        ),
        "transaction_state_directories": _reserved_files(
            OFFLINE_STATE_ROOT,
            SHA256,
        ),
        "unit_file_recovery_services": frozenset(
            service_unit_files
        ),
        "unit_file_recovery_timers": frozenset(
            timer_unit_files
        ),
        "loaded_recovery_services": frozenset(loaded_services),
        "loaded_recovery_timers": frozenset(loaded_timers),
        "active_recovery_services": active_services,
        "active_recovery_timers": active_timers,
    }
    expected = {
        "recovery_service_files": frozenset({
            str(
                SYSTEMD_ROOT
                / contract["recovery_service"]
            )
        }),
        "recovery_timer_files": frozenset({
            str(
                SYSTEMD_ROOT
                / contract["recovery_timer"]
            )
        }),
        "gateway_drop_in_files": frozenset({
            next(
                path
                for path in contract["artifacts"]
                if path.endswith(".conf")
            )
        }),
        "reconciler_files": frozenset({
            contract["reconciler_path"]
        }),
        "transaction_state_directories": frozenset({
            contract["state_directory"]
        }),
        "unit_file_recovery_services": frozenset({
            contract["recovery_service"]
        }),
        "unit_file_recovery_timers": frozenset({
            contract["recovery_timer"]
        }),
        "loaded_recovery_services": frozenset({
            contract["recovery_service"]
        }),
        "loaded_recovery_timers": frozenset({
            contract["recovery_timer"]
        }),
        "active_recovery_timers": frozenset({
            contract["recovery_timer"]
        }),
    }
    if any(
        observed[name] != wanted
        for name, wanted in expected.items()
    ) or observed["active_recovery_services"] not in {
        frozenset(),
        frozenset({contract["recovery_service"]}),
    }:
        _fail("OFFLINE_DEPLOY_RESERVED_NAMESPACE_INVALID")
    observed["active_recovery_services"] = frozenset()
    return {
        name: sorted(values)
        for name, values in sorted(observed.items())
    }


def _attest_systemd(
    manifest_path: Path,
    manifest: Mapping[str, Any],
    *,
    runner: Any = subprocess.run,
    allow_create: bool = True,
) -> Mapping[str, Any]:
    contract = _scaffold_contract(manifest_path, manifest)
    scaffold_rows: list[Mapping[str, Any]] = []
    for raw_path, expected in sorted(contract["artifacts"].items()):
        path = Path(raw_path)
        raw, state = _stable_read(
            path,
            uid=0,
            gid=0,
            mode=0o644,
            maximum=max(len(expected), 1),
            code="OFFLINE_DEPLOY_SCAFFOLDING_INVALID",
        )
        if raw != expected:
            _fail("OFFLINE_DEPLOY_SCAFFOLDING_INVALID")
        scaffold_rows.append(_file_row(path, raw, state))
    if not _row_matches(
        manifest["reconciler_runtime"],
        path=Path(contract["reconciler_path"]),
    ):
        _fail("OFFLINE_DEPLOY_SCAFFOLDING_INVALID")
    scaffold_rows.append(dict(manifest["reconciler_runtime"]))

    if (
        _systemctl(
            (
                "is-enabled",
                "--",
                contract["recovery_service"],
            ),
            runner=runner,
        )
        != "enabled"
        or _systemctl(
            (
                "is-enabled",
                "--",
                contract["recovery_timer"],
            ),
            runner=runner,
        )
        != "enabled"
        or _systemctl(
            (
                "is-active",
                "--",
                contract["recovery_timer"],
            ),
            runner=runner,
        )
        != "active"
    ):
        _fail("OFFLINE_DEPLOY_SYSTEMD_ATTESTATION_FAILED")
    gateway_show = _parse_systemd_properties(
        _systemctl(
            (
                "show",
                "--property=LoadState",
                "--property=NeedDaemonReload",
                "--property=DropInPaths",
                "--property=Wants",
                "--property=After",
                "--property=ExecConditionEx",
                "--",
                contract["gateway_unit"],
            ),
            runner=runner,
        ),
        frozenset({
            "LoadState",
            "NeedDaemonReload",
            "DropInPaths",
            "Wants",
            "After",
            "ExecConditionEx",
        }),
    )
    recovery_show = _parse_systemd_properties(
        _systemctl(
        (
            "show",
            "--property=LoadState",
            "--property=NeedDaemonReload",
            "--property=FragmentPath",
            "--property=Before",
            "--property=DropInPaths",
            "--property=ExecStartEx",
            "--",
            contract["recovery_service"],
        ),
        runner=runner,
        ),
        frozenset({
            "LoadState",
            "NeedDaemonReload",
            "FragmentPath",
            "Before",
            "DropInPaths",
            "ExecStartEx",
        }),
    )
    timer_show = _parse_systemd_properties(
        _systemctl(
        (
            "show",
            "--property=LoadState",
            "--property=NeedDaemonReload",
            "--property=FragmentPath",
            "--property=Triggers",
            "--property=DropInPaths",
            "--",
            contract["recovery_timer"],
        ),
        runner=runner,
        ),
        frozenset({
            "LoadState",
            "NeedDaemonReload",
            "FragmentPath",
            "Triggers",
            "DropInPaths",
        }),
    )
    gateway_condition = _parse_exec_ex(
        gateway_show["ExecConditionEx"]
    )
    recovery_exec = _parse_exec_ex(recovery_show["ExecStartEx"])
    gateway_dropins = frozenset(
        gateway_show["DropInPaths"].split()
    )
    gateway_wants = frozenset(gateway_show["Wants"].split())
    gateway_after = frozenset(gateway_show["After"].split())
    recovery_before = frozenset(recovery_show["Before"].split())
    recovery_dropins = frozenset(
        recovery_show["DropInPaths"].split()
    )
    timer_dropins = frozenset(timer_show["DropInPaths"].split())
    timer_triggers = frozenset(timer_show["Triggers"].split())
    transaction_dropins = frozenset(
        path
        for path in gateway_dropins
        if GATEWAY_DROP_IN_NAME.fullmatch(Path(path).name)
        is not None
    )
    transaction_wants = frozenset(
        unit
        for unit in gateway_wants
        if RECOVERY_SERVICE_NAME.fullmatch(unit) is not None
    )
    transaction_after = frozenset(
        unit
        for unit in gateway_after
        if RECOVERY_SERVICE_NAME.fullmatch(unit) is not None
    )
    if (
        any(
            properties["LoadState"] != "loaded"
            or properties["NeedDaemonReload"] != "no"
            for properties in (
                gateway_show,
                recovery_show,
                timer_show,
            )
        )
        or recovery_show["FragmentPath"]
        != str(
            SYSTEMD_ROOT / contract["recovery_service"]
        )
        or timer_show["FragmentPath"]
        != str(SYSTEMD_ROOT / contract["recovery_timer"])
        or transaction_dropins
        != frozenset({
            next(
                path
                for path in contract["artifacts"]
                if path.endswith(".conf")
            )
        })
        or transaction_wants
        != frozenset({contract["recovery_service"]})
        or transaction_after
        != frozenset({contract["recovery_service"]})
        or contract["gateway_unit"] not in recovery_before
        or recovery_dropins
        or timer_dropins
        or timer_triggers
        != frozenset({contract["recovery_service"]})
        or gateway_condition
        != {
            "argv": contract["authorize_argv"],
            "flags": "fully-privileged",
        }
        or recovery_exec
        != {
            "argv": contract["reconcile_argv"],
            "flags": "",
        }
    ):
        _fail("OFFLINE_DEPLOY_SYSTEMD_ATTESTATION_FAILED")
    loaded_observation = {
        "gateway_drop_in_paths": sorted(gateway_dropins),
        "gateway_wants": sorted(gateway_wants),
        "gateway_after": sorted(gateway_after),
        "gateway_exec_condition": {
            "argv": list(gateway_condition["argv"]),
            "flags": gateway_condition["flags"],
        },
        "recovery_fragment_path": recovery_show["FragmentPath"],
        "recovery_before": sorted(recovery_before),
        "recovery_drop_in_paths": sorted(recovery_dropins),
        "recovery_exec_start": {
            "argv": list(recovery_exec["argv"]),
            "flags": recovery_exec["flags"],
        },
        "timer_fragment_path": timer_show["FragmentPath"],
        "timer_drop_in_paths": sorted(timer_dropins),
        "timer_triggers": sorted(timer_triggers),
        "recovery_service_enabled": True,
        "recovery_timer_enabled": True,
        "recovery_timer_active": True,
    }
    reserved_namespace = _reserved_namespace(
        contract,
        runner=runner,
    )
    unsigned = {
        "schema": SCAFFOLD_ATTESTATION_SCHEMA,
        "manifest_sha256": manifest["_manifest_file_sha256"],
        "scaffold_rows": sorted(
            scaffold_rows,
            key=lambda row: row["path"],
        ),
        "loaded_observation": loaded_observation,
        "reserved_namespace": reserved_namespace,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    receipt = {
        **unsigned,
        "receipt_sha256": _sha(_canonical(unsigned)),
    }
    attestation_path = _scaffold_attestation_path(manifest_path)
    if not allow_create and not os.path.lexists(attestation_path):
        _fail("OFFLINE_DEPLOY_SCAFFOLD_ATTESTATION_INVALID")
    _create_or_exact(
        attestation_path,
        _canonical(receipt),
        uid=0,
        gid=0,
        mode=0o400,
        code="OFFLINE_DEPLOY_SCAFFOLD_ATTESTATION_INVALID",
    )
    return receipt


def _read_proc_identity(
    pid: int,
    *,
    proc_root: Path,
) -> tuple[str, Path, bytes]:
    stat_path = proc_root / str(pid) / "stat"
    exe_path = proc_root / str(pid) / "exe"
    cmdline_path = proc_root / str(pid) / "cmdline"
    try:
        first_stat = stat_path.read_text(encoding="utf-8")
        first_start = first_stat.rsplit(")", 1)[1].split()[19]
        first_exe = exe_path.resolve(strict=True)
        first_cmdline = cmdline_path.read_bytes()
        second_stat = stat_path.read_text(encoding="utf-8")
        second_start = second_stat.rsplit(")", 1)[1].split()[19]
        second_exe = exe_path.resolve(strict=True)
        second_cmdline = cmdline_path.read_bytes()
    except (OSError, IndexError) as exc:
        raise ReconcileError(
            "OFFLINE_DEPLOY_GATEWAY_INCARNATION_INVALID"
        ) from exc
    if (
        not first_start.isdigit()
        or first_start != second_start
        or first_exe != second_exe
        or not first_cmdline
        or first_cmdline != second_cmdline
        or len(first_cmdline) > MAX_SYSTEMD_OBSERVATION
    ):
        _fail("OFFLINE_DEPLOY_GATEWAY_INCARNATION_INVALID")
    return first_start, first_exe, first_cmdline


def _decode_proc_cmdline(raw: bytes) -> tuple[str, ...]:
    if not raw.endswith(b"\0"):
        _fail("OFFLINE_DEPLOY_GATEWAY_INCARNATION_INVALID")
    try:
        argv = tuple(
            item.decode("utf-8", errors="strict")
            for item in raw[:-1].split(b"\0")
        )
    except UnicodeError as exc:
        raise ReconcileError(
            "OFFLINE_DEPLOY_GATEWAY_INCARNATION_INVALID"
        ) from exc
    if not argv or any(not item for item in argv):
        _fail("OFFLINE_DEPLOY_GATEWAY_INCARNATION_INVALID")
    return argv


def _running_gateway_identity(
    manifest: Mapping[str, Any],
    *,
    revision: str,
    release: Path,
    runner: Any,
    proc_root: Path,
) -> Mapping[str, Any]:
    def read_properties() -> Mapping[str, str]:
        return _parse_systemd_properties(
            _systemctl(
                (
                    "show",
                    "--property=LoadState",
                    "--property=NeedDaemonReload",
                    "--property=FragmentPath",
                    "--property=ActiveState",
                    "--property=SubState",
                    "--property=MainPID",
                    "--property=InvocationID",
                    "--property=ControlGroup",
                    "--property=NRestarts",
                    "--property=WorkingDirectory",
                    "--property=ExecStartEx",
                    "--",
                    manifest["service_unit"],
                ),
                runner=runner,
            ),
            frozenset({
                "LoadState",
                "NeedDaemonReload",
                "FragmentPath",
                "ActiveState",
                "SubState",
                "MainPID",
                "InvocationID",
                "ControlGroup",
                "NRestarts",
                "WorkingDirectory",
                "ExecStartEx",
            }),
        )

    properties = read_properties()
    try:
        pid = int(properties["MainPID"])
    except ValueError as exc:
        raise ReconcileError(
            "OFFLINE_DEPLOY_GATEWAY_INCARNATION_INVALID"
        ) from exc
    control_group = properties["ControlGroup"]
    invocation_id = properties["InvocationID"]
    fragment_path = Path(properties["FragmentPath"])
    if (
        properties["LoadState"] != "loaded"
        or properties["NeedDaemonReload"] != "no"
        or fragment_path
        != SYSTEMD_ROOT / manifest["service_unit"]
        or properties["ActiveState"] != "active"
        or properties["SubState"] != "running"
        or pid <= 1
        or re.fullmatch(r"[0-9a-f]{32}", invocation_id) is None
        or not control_group.startswith("/")
        or ".." in Path(control_group).parts
        or properties["NRestarts"] != "0"
        or properties["WorkingDirectory"] != str(release)
        or REVISION.fullmatch(revision) is None
    ):
        _fail("OFFLINE_DEPLOY_GATEWAY_INCARNATION_INVALID")
    execution = _parse_exec_ex(properties["ExecStartEx"])
    argv = tuple(execution["argv"])
    runtime_executable = release / ".venv/bin/python"
    expected_prefix = (
        str(runtime_executable),
        "-B",
        "-P",
        "-s",
        "-m",
        "gateway.run",
        "--config",
        "/opt/adventico-ai-platform/hermes-home/config.yaml",
        "--require-production-model-sovereignty",
        "--production-release-revision",
        revision,
        "--production-config-sha256",
    )
    if (
        execution["flags"] != ""
        or len(argv) != len(expected_prefix) + 1
        or argv[:-1] != expected_prefix
        or SHA256.fullmatch(argv[-1]) is None
    ):
        _fail("OFFLINE_DEPLOY_GATEWAY_INCARNATION_INVALID")
    start_ticks, executable_target, cmdline_raw = _read_proc_identity(
        pid,
        proc_root=proc_root,
    )
    try:
        expected_target = runtime_executable.resolve(strict=True)
        active_target = Path(manifest["active_link"]).resolve(
            strict=True
        )
    except OSError as exc:
        raise ReconcileError(
            "OFFLINE_DEPLOY_GATEWAY_INCARNATION_INVALID"
        ) from exc
    if (
        executable_target != expected_target
        or active_target != release
        or _decode_proc_cmdline(cmdline_raw) != argv
        or read_properties() != properties
    ):
        _fail("OFFLINE_DEPLOY_GATEWAY_INCARNATION_INVALID")
    fragment_raw, fragment_state = _stable_read(
        fragment_path,
        uid=0,
        gid=0,
        mode=0o644,
        maximum=MAX_SYSTEMD_OBSERVATION,
        code="OFFLINE_DEPLOY_GATEWAY_INCARNATION_INVALID",
    )
    unsigned = {
        "service_unit": manifest["service_unit"],
        "revision": revision,
        "release": str(release),
        "pid": pid,
        "systemd_invocation_id": invocation_id,
        "control_group": control_group,
        "process_start_ticks": start_ticks,
        "runtime_executable": str(runtime_executable),
        "runtime_executable_target": str(expected_target),
        "exec_start_argv": list(argv),
        "exec_start_flags": execution["flags"],
        "process_cmdline_sha256": _sha(cmdline_raw),
        "gateway_fragment": _file_row(
            fragment_path,
            fragment_raw,
            fragment_state,
        ),
    }
    return {
        **unsigned,
        "gateway_incarnation_sha256": _sha(_canonical(unsigned)),
    }


def _capture_arm_gateway(
    manifest_path: Path,
    manifest: Mapping[str, Any],
    *,
    runner: Any,
    proc_root: Path,
) -> Mapping[str, Any]:
    incarnation = _running_gateway_identity(
        manifest,
        revision=manifest["owner_release_revision"],
        release=Path(manifest["predecessor_release"]),
        runner=runner,
        proc_root=proc_root,
    )
    unsigned = {
        "schema": ARM_GATEWAY_SCHEMA,
        "manifest_sha256": manifest["_manifest_file_sha256"],
        "incarnation": incarnation,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    receipt = {
        **unsigned,
        "receipt_sha256": _sha(_canonical(unsigned)),
    }
    _create_or_exact(
        _arm_gateway_path(manifest_path),
        _canonical(receipt),
        uid=0,
        gid=0,
        mode=0o400,
        code="OFFLINE_DEPLOY_ARM_GATEWAY_INVALID",
    )
    return receipt


def _read_arm_gateway(
    manifest_path: Path,
    manifest: Mapping[str, Any],
) -> Mapping[str, Any]:
    value, _raw, _state = _read_json_file(
        _arm_gateway_path(manifest_path),
        uid=0,
        gid=0,
        mode=0o400,
        code="OFFLINE_DEPLOY_ARM_GATEWAY_INVALID",
    )
    incarnation = value.get("incarnation")
    if (
        set(value)
        != {
            "schema",
            "manifest_sha256",
            "incarnation",
            "secret_material_recorded",
            "secret_digest_recorded",
            "receipt_sha256",
        }
        or value.get("schema") != ARM_GATEWAY_SCHEMA
        or value.get("manifest_sha256")
        != manifest["_manifest_file_sha256"]
        or not isinstance(incarnation, Mapping)
        or incarnation.get("revision")
        != manifest["owner_release_revision"]
        or incarnation.get("release")
        != manifest["predecessor_release"]
        or SHA256.fullmatch(
            str(incarnation.get("gateway_incarnation_sha256", ""))
        )
        is None
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
    ):
        _fail("OFFLINE_DEPLOY_ARM_GATEWAY_INVALID")
    unsigned_incarnation = {
        name: item
        for name, item in incarnation.items()
        if name != "gateway_incarnation_sha256"
    }
    if incarnation["gateway_incarnation_sha256"] != _sha(
        _canonical(unsigned_incarnation)
    ):
        _fail("OFFLINE_DEPLOY_ARM_GATEWAY_INVALID")
    _self_digest(
        value,
        "receipt_sha256",
        "OFFLINE_DEPLOY_ARM_GATEWAY_INVALID",
    )
    return value


def _read_gateway_zero_state(
    manifest: Mapping[str, Any],
    *,
    expected_incarnation: Mapping[str, Any],
) -> Mapping[str, Any]:
    path = Path(manifest["gateway_state_path"])
    try:
        initial = path.lstat()
    except OSError as exc:
        raise ReconcileError(
            "OFFLINE_DEPLOY_DRAIN_ZERO_INVALID"
        ) from exc
    mode = stat.S_IMODE(initial.st_mode)
    if mode & 0o022:
        _fail("OFFLINE_DEPLOY_DRAIN_ZERO_INVALID")
    raw, state = _stable_read(
        path,
        uid=manifest["gateway_uid"],
        gid=manifest["gateway_gid"],
        mode=mode,
        maximum=MAX_SYSTEMD_OBSERVATION,
        code="OFFLINE_DEPLOY_DRAIN_ZERO_INVALID",
    )
    value = _decode_object_relaxed(
        raw,
        "OFFLINE_DEPLOY_DRAIN_ZERO_INVALID",
    )
    acknowledgment = value.get("external_drain_ack")
    expected_ack_fields = {
        "marker_sha256",
        "transaction_sha256",
        "mutation_capability_sha256",
        "epoch",
        "process_start_ticks",
        "systemd_invocation_id",
        "ack_sequence",
    }
    current_epoch = _current_epoch()
    expected_marker_sha256 = _sha(
        _canonical(
            _drain_marker(
                manifest["drain_marker_template"],
                current_epoch,
                transaction_sha256=manifest["transaction_sha256"],
                capability_sha256=manifest[
                    "drain_mutation_capability_sha256"
                ],
            ),
            newline=True,
        )
    )
    if (
        value.get("pid") != expected_incarnation["pid"]
        or value.get("gateway_state") != "draining"
        or type(value.get("active_agents")) is not int
        or value.get("active_agents") != 0
        or value.get("active_session_keys") != []
        or not isinstance(acknowledgment, Mapping)
        or set(acknowledgment) != expected_ack_fields
        or acknowledgment.get("marker_sha256")
        != expected_marker_sha256
        or acknowledgment.get("transaction_sha256")
        != manifest["transaction_sha256"]
        or acknowledgment.get("mutation_capability_sha256")
        != manifest["drain_mutation_capability_sha256"]
        or acknowledgment.get("epoch") != current_epoch
        or acknowledgment.get("process_start_ticks")
        != expected_incarnation["process_start_ticks"]
        or acknowledgment.get("systemd_invocation_id")
        != expected_incarnation["systemd_invocation_id"]
        or type(acknowledgment.get("ack_sequence")) is not int
        or acknowledgment["ack_sequence"] <= 0
    ):
        _fail("OFFLINE_DEPLOY_DRAIN_ZERO_INVALID")
    return {
        "gateway_state_byte_sha256": _sha(raw),
        "gateway_state_size": len(raw),
        "gateway_state_mode": mode,
        "gateway_state_inode": state.st_ino,
        "external_drain_ack": dict(acknowledgment),
    }


def _read_drain_sample(
    manifest_path: Path,
    manifest: Mapping[str, Any],
    index: int,
) -> Mapping[str, Any]:
    value, _raw, _state = _read_json_file(
        _drain_sample_path(manifest_path, index),
        uid=0,
        gid=0,
        mode=0o400,
        code="OFFLINE_DEPLOY_DRAIN_SAMPLE_INVALID",
    )
    previous_sample = (
        None
        if index == 1
        else _read_drain_sample(
            manifest_path,
            manifest,
            1,
        )
    )
    expected_previous = (
        None
        if previous_sample is None
        else previous_sample["receipt_sha256"]
    )
    if (
        set(value)
        != {
            "schema",
            "manifest_sha256",
            "sample_index",
            "previous_sample_receipt_sha256",
            "arm_gateway_receipt_sha256",
            "gateway_incarnation_sha256",
            "gateway_state",
            "secret_material_recorded",
            "secret_digest_recorded",
            "receipt_sha256",
        }
        or value.get("schema") != DRAIN_SAMPLE_SCHEMA
        or value.get("manifest_sha256")
        != manifest["_manifest_file_sha256"]
        or value.get("sample_index") != index
        or value.get("previous_sample_receipt_sha256")
        != expected_previous
        or value.get("arm_gateway_receipt_sha256")
        != _read_arm_gateway(manifest_path, manifest)[
            "receipt_sha256"
        ]
        or value.get("gateway_incarnation_sha256")
        != _read_arm_gateway(manifest_path, manifest)[
            "incarnation"
        ]["gateway_incarnation_sha256"]
        or not isinstance(value.get("gateway_state"), Mapping)
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
    ):
        _fail("OFFLINE_DEPLOY_DRAIN_SAMPLE_INVALID")
    state = value["gateway_state"]
    if (
        set(state)
        != {
            "gateway_state_byte_sha256",
            "gateway_state_size",
            "gateway_state_mode",
            "gateway_state_inode",
            "external_drain_ack",
        }
        or SHA256.fullmatch(
            str(state.get("gateway_state_byte_sha256", ""))
        )
        is None
        or type(state.get("gateway_state_size")) is not int
        or state["gateway_state_size"] <= 0
        or type(state.get("gateway_state_mode")) is not int
        or type(state.get("gateway_state_inode")) is not int
    ):
        _fail("OFFLINE_DEPLOY_DRAIN_SAMPLE_INVALID")
    acknowledgment = state["external_drain_ack"]
    acknowledgment_fields = {
        "marker_sha256",
        "transaction_sha256",
        "mutation_capability_sha256",
        "epoch",
        "process_start_ticks",
        "systemd_invocation_id",
        "ack_sequence",
    }
    if (
        not isinstance(acknowledgment, Mapping)
        or set(acknowledgment) != acknowledgment_fields
        or any(
            SHA256.fullmatch(str(acknowledgment.get(field, "")))
            is None
            for field in (
                "marker_sha256",
                "transaction_sha256",
                "mutation_capability_sha256",
            )
        )
        or not isinstance(acknowledgment.get("epoch"), str)
        or not acknowledgment["epoch"]
        or not isinstance(
            acknowledgment.get("process_start_ticks"),
            str,
        )
        or not acknowledgment["process_start_ticks"].isdigit()
        or re.fullmatch(
            r"[0-9a-f]{32}",
            str(acknowledgment.get("systemd_invocation_id", "")),
        )
        is None
        or type(acknowledgment.get("ack_sequence")) is not int
        or acknowledgment["ack_sequence"] <= 0
    ):
        _fail("OFFLINE_DEPLOY_DRAIN_SAMPLE_INVALID")
    if previous_sample is not None:
        previous_ack = previous_sample["gateway_state"][
            "external_drain_ack"
        ]
        if (
            {
                key: item
                for key, item in acknowledgment.items()
                if key != "ack_sequence"
            }
            != {
                key: item
                for key, item in previous_ack.items()
                if key != "ack_sequence"
            }
            or acknowledgment["ack_sequence"]
            <= previous_ack["ack_sequence"]
        ):
            _fail("OFFLINE_DEPLOY_DRAIN_SAMPLE_INVALID")
    _self_digest(
        value,
        "receipt_sha256",
        "OFFLINE_DEPLOY_DRAIN_SAMPLE_INVALID",
    )
    return value


def _sample_drain_zero(
    manifest_path: Path,
    manifest: Mapping[str, Any],
    *,
    runner: Any,
    proc_root: Path,
) -> tuple[bool, Mapping[str, Any]]:
    if not _marker_current(manifest_path, manifest):
        _fail("OFFLINE_DEPLOY_DRAIN_MARKER_INVALID")
    arm_gateway = _read_arm_gateway(manifest_path, manifest)
    incarnation = _running_gateway_identity(
        manifest,
        revision=manifest["owner_release_revision"],
        release=Path(manifest["predecessor_release"]),
        runner=runner,
        proc_root=proc_root,
    )
    if (
        incarnation["gateway_incarnation_sha256"]
        != arm_gateway["incarnation"]["gateway_incarnation_sha256"]
    ):
        _fail("OFFLINE_DEPLOY_GATEWAY_INCARNATION_CHANGED")
    state = _read_gateway_zero_state(
        manifest,
        expected_incarnation=incarnation,
    )
    first_path = _drain_sample_path(manifest_path, 1)
    index = 2 if os.path.lexists(first_path) else 1
    previous = (
        _read_drain_sample(manifest_path, manifest, 1)
        if index == 2
        else None
    )
    if previous is not None:
        previous_ack = previous["gateway_state"][
            "external_drain_ack"
        ]
        current_ack = state["external_drain_ack"]
        if (
            {
                key: item
                for key, item in current_ack.items()
                if key != "ack_sequence"
            }
            != {
                key: item
                for key, item in previous_ack.items()
                if key != "ack_sequence"
            }
        ):
            _fail("OFFLINE_DEPLOY_DRAIN_ZERO_INVALID")
        if current_ack["ack_sequence"] <= previous_ack["ack_sequence"]:
            return False, previous
    unsigned = {
        "schema": DRAIN_SAMPLE_SCHEMA,
        "manifest_sha256": manifest["_manifest_file_sha256"],
        "sample_index": index,
        "previous_sample_receipt_sha256": (
            previous["receipt_sha256"]
            if previous is not None
            else None
        ),
        "arm_gateway_receipt_sha256": arm_gateway["receipt_sha256"],
        "gateway_incarnation_sha256": incarnation[
            "gateway_incarnation_sha256"
        ],
        "gateway_state": state,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    receipt = {
        **unsigned,
        "receipt_sha256": _sha(_canonical(unsigned)),
    }
    _create_or_exact(
        _drain_sample_path(manifest_path, index),
        _canonical(receipt),
        uid=0,
        gid=0,
        mode=0o400,
        code="OFFLINE_DEPLOY_DRAIN_SAMPLE_INVALID",
    )
    return index == 2, receipt


def _read_cgroup_empty(
    control_group: str,
    *,
    cgroup_root: Path,
    trusted_uid: int = 0,
    trusted_gid: int = 0,
) -> Mapping[str, Any]:
    if (
        not control_group.startswith("/")
        or ".." in Path(control_group).parts
    ):
        _fail("OFFLINE_DEPLOY_STOP_PROOF_INVALID")
    directory = cgroup_root / control_group.lstrip("/")
    if not os.path.lexists(directory):
        return {
            "control_group": control_group,
            "cgroup_present": False,
            "cgroup_procs_sha256": None,
            "cgroup_events_sha256": None,
            "cgroup_populated": None,
            "cgroup_node_count": 0,
            "cgroup_nodes": [],
        }

    def read_control(path: Path) -> tuple[bytes, os.stat_result]:
        descriptor: int | None = None
        try:
            before = path.lstat()
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            chunks: list[bytes] = []
            remaining = 64 * 1024
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(descriptor)
            reachable = path.lstat()
        except OSError as exc:
            raise ReconcileError(
                "OFFLINE_DEPLOY_STOP_PROOF_INVALID"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != trusted_uid
            or opened.st_gid != trusted_gid
            or len(raw) >= 64 * 1024
            or _identity(before) != _identity(opened)
            or _identity(before) != _identity(after)
            or _identity(before) != _identity(reachable)
        ):
            _fail("OFFLINE_DEPLOY_STOP_PROOF_INVALID")
        return raw, opened

    def parse_events(raw: bytes) -> Mapping[str, int]:
        values: dict[str, int] = {}
        try:
            lines = raw.decode("ascii", errors="strict").splitlines()
        except UnicodeError as exc:
            raise ReconcileError(
                "OFFLINE_DEPLOY_STOP_PROOF_INVALID"
            ) from exc
        for line in lines:
            fields = line.split()
            if (
                len(fields) != 2
                or fields[0] in values
                or not fields[0]
                or not fields[1].isdigit()
            ):
                _fail("OFFLINE_DEPLOY_STOP_PROOF_INVALID")
            values[fields[0]] = int(fields[1])
        if values.get("populated") != 0:
            _fail("OFFLINE_DEPLOY_STOP_PROOF_INVALID")
        return values

    def scan_tree() -> list[Mapping[str, Any]]:
        try:
            resolved_root = cgroup_root.resolve(strict=True)
            resolved_directory = directory.resolve(strict=True)
        except OSError as exc:
            raise ReconcileError(
                "OFFLINE_DEPLOY_STOP_PROOF_INVALID"
            ) from exc
        try:
            resolved_directory.relative_to(resolved_root)
        except ValueError:
            _fail("OFFLINE_DEPLOY_STOP_PROOF_INVALID")
        pending = [directory]
        observations: list[Mapping[str, Any]] = []
        maximum_nodes = 1024
        maximum_depth = 64
        while pending:
            current = pending.pop()
            try:
                before = current.lstat()
                resolved = current.resolve(strict=True)
                resolved.relative_to(resolved_directory)
            except (OSError, ValueError) as exc:
                raise ReconcileError(
                    "OFFLINE_DEPLOY_STOP_PROOF_INVALID"
                ) from exc
            relative = current.relative_to(directory)
            if (
                stat.S_ISLNK(before.st_mode)
                or not stat.S_ISDIR(before.st_mode)
                or before.st_uid != trusted_uid
                or before.st_gid != trusted_gid
                or len(relative.parts) > maximum_depth
                or len(observations) >= maximum_nodes
            ):
                _fail("OFFLINE_DEPLOY_STOP_PROOF_INVALID")
            procs_raw, procs_state = read_control(
                current / "cgroup.procs"
            )
            events_raw, events_state = read_control(
                current / "cgroup.events"
            )
            if procs_raw.strip():
                _fail("OFFLINE_DEPLOY_STOP_PROOF_INVALID")
            events = parse_events(events_raw)
            observations.append({
                "relative_path": (
                    "."
                    if not relative.parts
                    else relative.as_posix()
                ),
                "directory_inode": before.st_ino,
                "directory_mode": stat.S_IMODE(before.st_mode),
                "cgroup_procs_sha256": _sha(procs_raw),
                "cgroup_procs_inode": procs_state.st_ino,
                "cgroup_events_sha256": _sha(events_raw),
                "cgroup_events_inode": events_state.st_ino,
                "populated": events["populated"],
            })
            try:
                with os.scandir(current) as iterator:
                    entries = sorted(
                        iterator,
                        key=lambda entry: entry.name,
                    )
            except OSError as exc:
                raise ReconcileError(
                    "OFFLINE_DEPLOY_STOP_PROOF_INVALID"
                ) from exc
            children: list[Path] = []
            for entry in entries:
                try:
                    if entry.is_symlink():
                        _fail("OFFLINE_DEPLOY_STOP_PROOF_INVALID")
                    if entry.is_dir(follow_symlinks=False):
                        children.append(Path(entry.path))
                except OSError as exc:
                    raise ReconcileError(
                        "OFFLINE_DEPLOY_STOP_PROOF_INVALID"
                    ) from exc
            pending.extend(reversed(children))
        return sorted(
            observations,
            key=lambda item: str(item["relative_path"]),
        )

    try:
        first = scan_tree()
        second = scan_tree()
    except ReconcileError:
        raise
    except OSError as exc:
        raise ReconcileError(
            "OFFLINE_DEPLOY_STOP_PROOF_INVALID"
        ) from exc
    if first != second or not first or first[0]["relative_path"] != ".":
        _fail("OFFLINE_DEPLOY_STOP_PROOF_INVALID")
    root = first[0]
    return {
        "control_group": control_group,
        "cgroup_present": True,
        "cgroup_procs_sha256": root["cgroup_procs_sha256"],
        "cgroup_events_sha256": root["cgroup_events_sha256"],
        "cgroup_populated": root["populated"],
        "cgroup_node_count": len(first),
        "cgroup_nodes": first,
    }


def _stopped_gateway_observation(
    manifest: Mapping[str, Any],
    arm_gateway: Mapping[str, Any],
    *,
    runner: Any,
    cgroup_root: Path,
) -> Mapping[str, Any]:
    def read_properties() -> Mapping[str, str]:
        return _parse_systemd_properties(
            _systemctl(
                (
                    "show",
                    "--property=LoadState",
                    "--property=ActiveState",
                    "--property=SubState",
                    "--property=MainPID",
                    "--property=ControlGroup",
                    "--",
                    manifest["service_unit"],
                ),
                runner=runner,
            ),
            frozenset({
                "LoadState",
                "ActiveState",
                "SubState",
                "MainPID",
                "ControlGroup",
            }),
        )

    properties = read_properties()
    arm_control_group = arm_gateway["incarnation"]["control_group"]
    if (
        properties["LoadState"] != "loaded"
        or properties["ActiveState"] != "inactive"
        or properties["SubState"] != "dead"
        or properties["MainPID"] != "0"
        or properties["ControlGroup"]
        not in {"", arm_control_group}
    ):
        _fail("OFFLINE_DEPLOY_STOP_PROOF_INVALID")
    cgroup = _read_cgroup_empty(
        arm_control_group,
        cgroup_root=cgroup_root,
    )
    if read_properties() != properties:
        _fail("OFFLINE_DEPLOY_STOP_PROOF_INVALID")
    return {
        "load_state": properties["LoadState"],
        "active_state": properties["ActiveState"],
        "sub_state": properties["SubState"],
        "main_pid": 0,
        "control_group": properties["ControlGroup"],
        "arm_control_group": arm_control_group,
        "cgroup": cgroup,
    }


def _run_gateway_stop(
    service_unit: str,
    *,
    runner: Any,
) -> None:
    try:
        completed = runner(
            (
                "/usr/bin/systemctl",
                "stop",
                "--",
                service_unit,
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd="/",
            env={"PATH": "/usr/bin:/bin"},
            shell=False,
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReconcileError(
            "OFFLINE_DEPLOY_GATEWAY_STOP_FAILED"
        ) from exc
    if completed.returncode != 0:
        _fail("OFFLINE_DEPLOY_GATEWAY_STOP_FAILED")


def _read_stop(
    manifest_path: Path,
    manifest: Mapping[str, Any],
) -> Mapping[str, Any]:
    value, _raw, _state = _read_json_file(
        _stop_path(manifest_path),
        uid=0,
        gid=0,
        mode=0o400,
        code="OFFLINE_DEPLOY_STOP_PROOF_INVALID",
    )
    stable_fields = {
        "schema",
        "manifest_sha256",
        "arm_gateway_receipt_sha256",
        "second_sample_receipt_sha256",
        "stopped_observation",
        "secret_material_recorded",
        "secret_digest_recorded",
        "receipt_sha256",
    }
    stopped_entry_fields = {
        "schema",
        "manifest_sha256",
        "arm_gateway_receipt_sha256",
        "stopped_entry_epoch_sha256",
        "stopped_observation",
        "secret_material_recorded",
        "secret_digest_recorded",
        "receipt_sha256",
    }
    common_invalid = (
        value.get("manifest_sha256")
        != manifest["_manifest_file_sha256"]
        or value.get("arm_gateway_receipt_sha256")
        != _read_arm_gateway(manifest_path, manifest)[
            "receipt_sha256"
        ]
        or not isinstance(value.get("stopped_observation"), Mapping)
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
    )
    if value.get("schema") == STOP_SCHEMA:
        invalid = (
            set(value) != stable_fields
            or common_invalid
            or value.get("second_sample_receipt_sha256")
            != _read_drain_sample(
                manifest_path,
                manifest,
                2,
            )["receipt_sha256"]
        )
    elif value.get("schema") == STOPPED_ENTRY_SCHEMA:
        invalid = (
            set(value) != stopped_entry_fields
            or common_invalid
            or SHA256.fullmatch(
                str(value.get("stopped_entry_epoch_sha256", ""))
            )
            is None
        )
    else:
        invalid = True
    if invalid:
        _fail("OFFLINE_DEPLOY_STOP_PROOF_INVALID")
    _self_digest(
        value,
        "receipt_sha256",
        "OFFLINE_DEPLOY_STOP_PROOF_INVALID",
    )
    return value


def _gateway_runtime_state(
    manifest: Mapping[str, Any],
    *,
    runner: Any,
) -> str:
    properties = _parse_systemd_properties(
        _systemctl(
            (
                "show",
                "--property=LoadState",
                "--property=ActiveState",
                "--property=SubState",
                "--property=MainPID",
                "--",
                manifest["service_unit"],
            ),
            runner=runner,
        ),
        frozenset({
            "LoadState",
            "ActiveState",
            "SubState",
            "MainPID",
        }),
    )
    if (
        properties["LoadState"] == "loaded"
        and properties["ActiveState"] == "active"
        and properties["SubState"] == "running"
        and properties["MainPID"].isdigit()
        and int(properties["MainPID"]) > 1
    ):
        return "running"
    if (
        properties["LoadState"] == "loaded"
        and properties["ActiveState"] == "inactive"
        and properties["SubState"] == "dead"
        and properties["MainPID"] == "0"
    ):
        return "stopped"
    _fail("OFFLINE_DEPLOY_GATEWAY_RUNTIME_STATE_INVALID")
    raise AssertionError("unreachable")


def _ensure_gateway_stopped(
    manifest_path: Path,
    manifest: Mapping[str, Any],
    *,
    runner: Any,
    proc_root: Path,
    cgroup_root: Path,
) -> tuple[bool, Mapping[str, Any]]:
    arm_gateway = _read_arm_gateway(manifest_path, manifest)
    if os.path.lexists(_stop_path(manifest_path)):
        stop = _read_stop(manifest_path, manifest)
        current = _stopped_gateway_observation(
            manifest,
            arm_gateway,
            runner=runner,
            cgroup_root=cgroup_root,
        )
        if current != stop["stopped_observation"]:
            _fail("OFFLINE_DEPLOY_STOP_PROOF_INVALID")
        return True, stop
    state = _gateway_runtime_state(manifest, runner=runner)
    stopped_at_entry = state == "stopped"
    if state == "running":
        stable, sample = _sample_drain_zero(
            manifest_path,
            manifest,
            runner=runner,
            proc_root=proc_root,
        )
        if not stable:
            return False, sample
        if not _marker_current(manifest_path, manifest):
            _fail("OFFLINE_DEPLOY_DRAIN_MARKER_INVALID")
        _run_gateway_stop(
            manifest["service_unit"],
            runner=runner,
        )
    second_sample = (
        None
        if stopped_at_entry
        and not os.path.lexists(
            _drain_sample_path(manifest_path, 2)
        )
        else _read_drain_sample(
            manifest_path,
            manifest,
            2,
        )
    )
    stopped = _stopped_gateway_observation(
        manifest,
        arm_gateway,
        runner=runner,
        cgroup_root=cgroup_root,
    )
    if second_sample is None:
        unsigned = {
            "schema": STOPPED_ENTRY_SCHEMA,
            "manifest_sha256": manifest["_manifest_file_sha256"],
            "arm_gateway_receipt_sha256": arm_gateway[
                "receipt_sha256"
            ],
            "stopped_entry_epoch_sha256": _sha(
                _current_epoch().encode("utf-8")
            ),
            "stopped_observation": stopped,
            "secret_material_recorded": False,
            "secret_digest_recorded": False,
        }
    else:
        unsigned = {
            "schema": STOP_SCHEMA,
            "manifest_sha256": manifest["_manifest_file_sha256"],
            "arm_gateway_receipt_sha256": arm_gateway[
                "receipt_sha256"
            ],
            "second_sample_receipt_sha256": second_sample[
                "receipt_sha256"
            ],
            "stopped_observation": stopped,
            "secret_material_recorded": False,
            "secret_digest_recorded": False,
        }
    receipt = {
        **unsigned,
        "receipt_sha256": _sha(_canonical(unsigned)),
    }
    _create_or_exact(
        _stop_path(manifest_path),
        _canonical(receipt),
        uid=0,
        gid=0,
        mode=0o400,
        code="OFFLINE_DEPLOY_STOP_PROOF_INVALID",
    )
    return True, receipt


def _republish_epoch_marker(
    manifest_path: Path,
    manifest: Mapping[str, Any],
) -> Mapping[str, Any]:
    marker_path = Path(manifest["drain_marker_path"])
    lock_path = Path(manifest["drain_lock_path"])
    epoch = _current_epoch()
    marker_raw = _canonical(
        _drain_marker(
            manifest["drain_marker_template"],
            epoch,
            transaction_sha256=manifest["transaction_sha256"],
            capability_sha256=manifest[
                "drain_mutation_capability_sha256"
            ],
        ),
        newline=True,
    )
    with _shared_drain_lock(
        lock_path,
        uid=manifest["gateway_uid"],
        gid=manifest["gateway_gid"],
        create=True,
    ):
        previous_sha = None
        if os.path.lexists(marker_path):
            previous, _state = _stable_read(
                marker_path,
                uid=manifest["gateway_uid"],
                gid=manifest["gateway_gid"],
                mode=0o600,
                code="OFFLINE_DEPLOY_DRAIN_MARKER_INVALID",
            )
            _validate_republishable_marker(previous, manifest)
            previous_sha = _sha(previous)
        _replace_marker(
            marker_path,
            marker_raw,
            uid=manifest["gateway_uid"],
            gid=manifest["gateway_gid"],
        )
        epoch_directory = _epoch_dir(manifest_path)
        if not os.path.lexists(epoch_directory):
            epoch_directory.mkdir(mode=0o700)
            os.chown(epoch_directory, 0, 0)
            os.chmod(epoch_directory, 0o700)
            _fsync_directory(epoch_directory.parent)
        _directory(
            epoch_directory,
            uid=0,
            gid=0,
            mode=0o700,
            code="OFFLINE_DEPLOY_EPOCH_DIRECTORY_INVALID",
        )
        unsigned = {
            "schema": EPOCH_RECEIPT_SCHEMA,
            "manifest_sha256": manifest["_manifest_file_sha256"],
            "epoch_sha256": _sha(epoch.encode("utf-8")),
            "previous_marker_sha256": previous_sha,
            "marker_sha256": _sha(marker_raw),
            "secret_material_recorded": False,
            "secret_digest_recorded": False,
        }
        receipt = {
            **unsigned,
            "receipt_sha256": _sha(_canonical(unsigned)),
        }
        receipt_path = epoch_directory / f"{receipt['receipt_sha256']}.json"
        _create_or_exact(
            receipt_path,
            _canonical(receipt),
            uid=0,
            gid=0,
            mode=0o400,
            code="OFFLINE_DEPLOY_EPOCH_RECEIPT_INVALID",
        )
    return receipt


def _publish_armed(
    manifest_path: Path,
    manifest: Mapping[str, Any],
) -> Mapping[str, Any]:
    arm_gateway = _read_arm_gateway(manifest_path, manifest)
    unsigned = {
        "schema": ARMED_SCHEMA,
        "manifest_sha256": manifest["_manifest_file_sha256"],
        "transaction_id": manifest["transaction_id"],
        "arm_gateway_receipt_sha256": arm_gateway["receipt_sha256"],
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    armed = {**unsigned, "armed_sha256": _sha(_canonical(unsigned))}
    _create_or_exact(
        _armed_path(manifest_path),
        _canonical(armed),
        uid=0,
        gid=0,
        mode=0o400,
        code="OFFLINE_DEPLOY_ARMED_INVALID",
    )
    return armed


def arm(
    manifest_path: Path,
    *,
    expected_manifest_sha256: str | None = None,
    require_root: bool = True,
    runner: Any = subprocess.run,
    inherited_lock_fd: int | None = None,
    proc_root: Path = Path("/proc"),
) -> Mapping[str, Any]:
    if require_root and (not sys.platform.startswith("linux") or os.geteuid() != 0):
        _fail("OFFLINE_DEPLOY_ROOT_REQUIRED")
    if inherited_lock_fd is None:
        _fail("OFFLINE_DEPLOY_ARM_REQUIRES_INHERITED_LOCK")
    manifest = load_manifest(
        manifest_path,
        expected_sha256=expected_manifest_sha256,
    )
    with _deploy_lock(
        Path(manifest["deploy_lock_path"]),
        inherited_fd=inherited_lock_fd,
    ):
        _validate_manifest_bindings(
            manifest,
            allow_terminal=False,
            runner=runner,
        )
        for absent in manifest["audit_absent"]:
            if os.path.lexists(absent):
                _fail("OFFLINE_DEPLOY_PREARM_TERMINAL_PRESENT")
        if _current_epoch() != manifest["initial_epoch"]:
            _fail("OFFLINE_DEPLOY_PREARM_EPOCH_CHANGED")
        _attest_systemd(manifest_path, manifest, runner=runner)
        arm_gateway = _capture_arm_gateway(
            manifest_path,
            manifest,
            runner=runner,
            proc_root=proc_root,
        )
        intent_unsigned = {
            "schema": ARM_INTENT_SCHEMA,
            "manifest_sha256": manifest["_manifest_file_sha256"],
            "transaction_id": manifest["transaction_id"],
            "initial_epoch_sha256": _sha(
                manifest["initial_epoch"].encode("utf-8")
            ),
            "arm_gateway_receipt_sha256": arm_gateway[
                "receipt_sha256"
            ],
            "secret_material_recorded": False,
            "secret_digest_recorded": False,
        }
        intent = {
            **intent_unsigned,
            "intent_sha256": _sha(_canonical(intent_unsigned)),
        }
        _create_or_exact(
            _arm_intent_path(manifest_path),
            _canonical(intent),
            uid=0,
            gid=0,
            mode=0o400,
            code="OFFLINE_DEPLOY_ARM_INTENT_INVALID",
        )
        _republish_epoch_marker(manifest_path, manifest)
        return _publish_armed(manifest_path, manifest)


def _triplet_state(
    manifest: Mapping[str, Any],
    *,
    trusted_uid: int = 0,
    trusted_gid: int = 0,
) -> str:
    def matches(rows: Sequence[Mapping[str, Any]]) -> bool:
        for row in rows:
            path = Path(row["live_path"])
            try:
                raw, _state = _stable_read(
                    path,
                    uid=trusted_uid,
                    gid=trusted_gid,
                    mode=int(row["mode"]),
                    maximum=MAX_JSON,
                    code="OFFLINE_DEPLOY_LIVE_TRIPLET_INVALID",
                )
            except ReconcileError:
                return False
            if _sha(raw) != row["byte_sha256"]:
                return False
        return True

    predecessor = matches(manifest["predecessor_triplet"])
    successor = matches(manifest["successor_triplet"])
    if predecessor and not successor:
        return "predecessor"
    if successor and not predecessor:
        return "successor"
    return "mutated"


def _active_target(manifest: Mapping[str, Any]) -> str:
    active = Path(manifest["active_link"])
    try:
        target = active.resolve(strict=True)
    except OSError:
        return "unknown"
    if target == Path(manifest["predecessor_release"]):
        return "predecessor"
    if target == Path(manifest["successor_release"]):
        return "successor"
    return "unknown"


def _audit_terminal_presence(manifest: Mapping[str, Any]) -> Mapping[str, bool]:
    root = Path(manifest["audit_transaction_path"])
    return {
        "activation": os.path.lexists(root / ACTIVATION_FILE),
        "abort": os.path.lexists(root / ABORT_FILE),
        "final": os.path.lexists(root / FINAL_FILE),
    }


def _marker_current(
    manifest_path: Path,
    manifest: Mapping[str, Any],
) -> bool:
    marker_path = Path(manifest["drain_marker_path"])
    try:
        raw, _state = _stable_read(
            marker_path,
            uid=manifest["gateway_uid"],
            gid=manifest["gateway_gid"],
            mode=0o600,
            code="OFFLINE_DEPLOY_DRAIN_MARKER_INVALID",
        )
        marker = json.loads(raw)
    except (ReconcileError, ValueError, json.JSONDecodeError):
        return False
    expected = _drain_marker(
        manifest["drain_marker_template"],
        _current_epoch(),
        transaction_sha256=manifest["transaction_sha256"],
        capability_sha256=manifest["drain_mutation_capability_sha256"],
    )
    if marker != expected:
        return False
    digest = _sha(raw)
    if digest == manifest["initial_marker_sha256"]:
        return True
    epoch_directory = _epoch_dir(manifest_path)
    if not epoch_directory.is_dir():
        return False
    for path in epoch_directory.glob("*.json"):
        try:
            value, _receipt_raw, _state = _read_json_file(
                path,
                uid=0,
                gid=0,
                mode=0o400,
                code="OFFLINE_DEPLOY_EPOCH_RECEIPT_INVALID",
            )
            _self_digest(
                value,
                "receipt_sha256",
                "OFFLINE_DEPLOY_EPOCH_RECEIPT_INVALID",
            )
        except ReconcileError:
            return False
        if (
            value.get("schema") == EPOCH_RECEIPT_SCHEMA
            and value.get("manifest_sha256") == manifest["_manifest_file_sha256"]
            and value.get("marker_sha256") == digest
            and value.get("epoch_sha256")
            == _sha(_current_epoch().encode("utf-8"))
        ):
            return True
    return False


def _read_terminal(
    manifest_path: Path,
    manifest: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    path = _terminal_path(manifest_path)
    if not os.path.lexists(path):
        return None
    value, _raw, _state = _read_json_file(
        path,
        uid=0,
        gid=0,
        mode=0o400,
        code="OFFLINE_DEPLOY_TERMINAL_INVALID",
    )
    expected_fields = {
        "schema",
        "manifest_sha256",
        "decision",
        "selected_request_sha256",
        "phase_result_sha256",
        "canonical_receipt_sha256",
        "activation_begin_sha256",
        "required_revision",
        "active_link_target",
        "secret_material_recorded",
        "secret_digest_recorded",
        "receipt_sha256",
    }
    decision = value.get("decision")
    request_name = "finalize" if decision == "finalized" else "abort"
    expected_request = manifest["requests"].get(request_name, {})
    expected_target = (
        manifest["successor_release"]
        if decision == "finalized"
        else manifest["predecessor_release"]
    )
    if (
        set(value) != expected_fields
        or value.get("schema") != TERMINAL_SCHEMA
        or value.get("manifest_sha256") != manifest["_manifest_file_sha256"]
        or decision not in {"aborted", "finalized"}
        or not isinstance(expected_request, Mapping)
        or value.get("selected_request_sha256")
        != expected_request.get("request_sha256")
        or SHA256.fullmatch(str(value.get("phase_result_sha256", "")))
        is None
        or SHA256.fullmatch(
            str(value.get("canonical_receipt_sha256", ""))
        )
        is None
        or (
            decision == "finalized"
            and SHA256.fullmatch(
                str(value.get("activation_begin_sha256", ""))
            )
            is None
        )
        or (
            decision == "aborted"
            and value.get("activation_begin_sha256") is not None
        )
        or value.get("required_revision")
        != (
            manifest["target_revision"]
            if decision == "finalized"
            else manifest["owner_release_revision"]
        )
        or value.get("active_link_target") != expected_target
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
    ):
        _fail("OFFLINE_DEPLOY_TERMINAL_INVALID")
    _self_digest(value, "receipt_sha256", "OFFLINE_DEPLOY_TERMINAL_INVALID")
    return value


def inspect(
    manifest_path: Path,
    *,
    expected_manifest_sha256: str | None = None,
    runner: Any = subprocess.run,
) -> Mapping[str, Any]:
    manifest = load_manifest(
        manifest_path,
        expected_sha256=expected_manifest_sha256,
    )
    armed_path = _armed_path(manifest_path)
    if not os.path.lexists(armed_path):
        state = "prepared_unarmed"
        armed = False
    else:
        _validate_armed(armed_path, manifest)
        state = "armed"
        armed = True
    terminal = _read_terminal(manifest_path, manifest) if armed else None
    presence = _audit_terminal_presence(manifest)
    triplet = _triplet_state(manifest)
    active = _active_target(manifest)
    if terminal is not None:
        state = terminal["decision"]
    elif armed:
        if (
            presence["final"]
            or presence["activation"]
            or triplet != "predecessor"
        ):
            state = "must_finalize"
        elif presence["abort"]:
            state = "must_abort"
        else:
            state = "may_abort"
    return {
        "schema": INSPECTION_SCHEMA,
        "manifest_sha256": manifest["_manifest_file_sha256"],
        "state": state,
        "armed": armed,
        "triplet": triplet,
        "active_link": active,
        "activation_present": presence["activation"],
        "abort_present": presence["abort"],
        "final_present": presence["final"],
        "marker_current": _marker_current(manifest_path, manifest),
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }


def authorize_start(
    manifest_path: Path,
    *,
    expected_manifest_sha256: str | None = None,
    runner: Any = subprocess.run,
) -> bool:
    """Read-only gateway ExecCondition."""

    manifest = load_manifest(
        manifest_path,
        expected_sha256=expected_manifest_sha256,
    )
    armed_path = _armed_path(manifest_path)
    if not os.path.lexists(armed_path):
        if os.path.lexists(_arm_intent_path(manifest_path)):
            _validate_arm_intent(
                _arm_intent_path(manifest_path),
                manifest,
            )
            return False
        return (
            _active_target(manifest) == "predecessor"
            and _triplet_state(manifest) == "predecessor"
        )
    _validate_armed(armed_path, manifest)
    terminal = _read_terminal(manifest_path, manifest)
    if terminal is None:
        return False
    required = (
        "successor" if terminal["decision"] == "finalized" else "predecessor"
    )
    if (
        _active_target(manifest) != required
        or _triplet_state(manifest) != required
    ):
        return False
    if os.path.lexists(_cleanup_path(manifest_path)):
        try:
            _read_cleanup(manifest_path, manifest, terminal)
        except ReconcileError:
            return False
        return True
    try:
        _attest_systemd(
            manifest_path,
            manifest,
            runner=runner,
            allow_create=False,
        )
    except ReconcileError:
        return False
    return _marker_current(manifest_path, manifest)


def _replay_documents(
    manifest: Mapping[str, Any],
) -> Mapping[str, Mapping[str, Any]]:
    audit_path = Path(manifest["audit_transaction_path"])
    documents, rows = _audit_documents(
        audit_path,
        uid=0,
        gid=0,
        allow_terminal=True,
    )
    if rows != manifest["audit_inventory"]:
        _fail("OFFLINE_DEPLOY_AUDIT_BINDING_CHANGED")
    _validate_audit_semantics(
        documents,
        audit_path,
        manifest["owner_release_revision"],
        manifest["target_revision"],
    )
    return documents


def _derive_successor_triplet_bytes(
    manifest: Mapping[str, Any],
    request: Mapping[str, Any],
) -> Mapping[str, bytes]:
    unit_publication = request["unit_input_publication"]
    update_publication = request["release_update_publication"]
    plan = unit_publication.get("plan")
    approval = unit_publication.get("approval")
    update_plan = update_publication.get("plan")
    update_approval = update_publication.get("approval")
    payload = plan.get("unit_inputs") if isinstance(plan, Mapping) else None
    if (
        unit_publication.get("schema")
        != RELEASE_UNIT_PUBLICATION_SCHEMA
        or update_publication.get("schema")
        != RELEASE_UPDATE_PUBLICATION_SCHEMA
        or not isinstance(plan, Mapping)
        or plan.get("schema") != RELEASE_UNIT_PLAN_SCHEMA
        or not isinstance(approval, Mapping)
        or approval.get("schema") != RELEASE_UNIT_APPROVAL_SCHEMA
        or not isinstance(update_plan, Mapping)
        or update_plan.get("schema") != RELEASE_UPDATE_PLAN_SCHEMA
        or not isinstance(update_approval, Mapping)
        or update_approval.get("schema")
        != RELEASE_UPDATE_APPROVAL_SCHEMA
        or not isinstance(payload, Mapping)
        or payload.get("schema")
        != "muncho-production-release-unit-input-payload.v4"
    ):
        _fail("OFFLINE_DEPLOY_SUCCESSOR_TRIPLET_INVALID")
    for value, digest_name in (
        (plan, "plan_sha256"),
        (approval, "approval_sha256"),
        (update_plan, "plan_sha256"),
        (update_approval, "approval_sha256"),
    ):
        _self_digest(
            value,
            digest_name,
            "OFFLINE_DEPLOY_SUCCESSOR_TRIPLET_INVALID",
        )
    fixed_prefix = {
        "schema": RELEASE_FIXED_INPUTS_SCHEMA,
        "predecessor_revision": plan.get("predecessor_revision"),
        "predecessor_trust_sha256": plan.get(
            "predecessor_trust_sha256"
        ),
        "predecessor_authority_plan_sha256": plan.get(
            "predecessor_authority_plan_sha256"
        ),
        "predecessor_authority_approval_sha256": plan.get(
            "predecessor_authority_approval_sha256"
        ),
        "predecessor_fixed_inputs_sha256": plan.get(
            "predecessor_fixed_inputs_sha256"
        ),
        "predecessor_activation_receipt_sha256": plan.get(
            "predecessor_activation_receipt_sha256"
        ),
        "release_revision": plan.get("release_revision"),
        "unit_input_authority_plan_sha256": plan.get("plan_sha256"),
        "unit_input_authority_approval_sha256": approval.get(
            "approval_sha256"
        ),
        "unit_input_authority_publication_sha256": (
            unit_publication.get("publication_sha256")
        ),
        "release_update_plan_sha256": update_plan.get(
            "plan_sha256"
        ),
        "release_update_approval_sha256": update_approval.get(
            "approval_sha256"
        ),
        "release_update_publication_sha256": (
            update_publication.get("publication_sha256")
        ),
    }
    payload_values = {
        name: item
        for name, item in payload.items()
        if name != "schema"
    }
    if set(fixed_prefix) & set(payload_values):
        _fail("OFFLINE_DEPLOY_SUCCESSOR_TRIPLET_INVALID")
    fixed_unsigned = {**fixed_prefix, **payload_values}
    fixed = {
        **fixed_unsigned,
        "fixed_inputs_sha256": _sha(_canonical(fixed_unsigned)),
    }
    raws = {
        "plan": _canonical(plan),
        "approval": _canonical(approval),
        "fixed": _canonical(fixed, newline=True),
    }
    transaction_successor = request["prepared_receipt"].get(
        "successor"
    )
    if (
        not isinstance(transaction_successor, Mapping)
        or plan.get("release_revision")
        != manifest["target_revision"]
        or unit_publication.get("release_revision")
        != manifest["target_revision"]
        or update_publication.get("release_revision")
        != manifest["target_revision"]
        or transaction_successor.get("plan_sha256")
        != plan.get("plan_sha256")
        or transaction_successor.get("approval_sha256")
        != approval.get("approval_sha256")
        or transaction_successor.get("publication_sha256")
        != unit_publication.get("publication_sha256")
        or transaction_successor.get(
            "release_update_publication_sha256"
        )
        != update_publication.get("publication_sha256")
        or transaction_successor.get("fixed_inputs_sha256")
        != fixed["fixed_inputs_sha256"]
        or transaction_successor.get("fixed_inputs_file_sha256")
        != _sha(raws["fixed"])
        or manifest["successor_fixed_inputs_sha256"]
        != fixed["fixed_inputs_sha256"]
        or manifest["successor_fixed_inputs_file_sha256"]
        != _sha(raws["fixed"])
    ):
        _fail("OFFLINE_DEPLOY_SUCCESSOR_TRIPLET_INVALID")
    expected_rows = {
        row["logical"]: row
        for row in manifest["successor_triplet"]
    }
    if set(expected_rows) != set(raws) or any(
        expected_rows[name].get("byte_sha256") != _sha(raw)
        or expected_rows[name].get("mode")
        != (0o444 if name == "fixed" else 0o400)
        for name, raw in raws.items()
    ):
        _fail("OFFLINE_DEPLOY_SUCCESSOR_TRIPLET_INVALID")
    return raws


def _predecessor_triplet_bytes(
    manifest: Mapping[str, Any],
) -> Mapping[str, bytes]:
    raws: dict[str, bytes] = {}
    for row in manifest["predecessor_triplet"]:
        logical = row.get("logical")
        if logical not in {"plan", "approval", "fixed"} or logical in raws:
            _fail("OFFLINE_DEPLOY_PREDECESSOR_TRIPLET_INVALID")
        raw, _state = _stable_read(
            Path(row["audit_path"]),
            uid=0,
            gid=0,
            mode=int(row["mode"]),
            maximum=int(row["size"]),
            code="OFFLINE_DEPLOY_PREDECESSOR_TRIPLET_INVALID",
        )
        if _sha(raw) != row["byte_sha256"]:
            _fail("OFFLINE_DEPLOY_PREDECESSOR_TRIPLET_INVALID")
        raws[str(logical)] = raw
    if set(raws) != {"plan", "approval", "fixed"}:
        _fail("OFFLINE_DEPLOY_PREDECESSOR_TRIPLET_INVALID")
    return raws


def _activation_begin_value(
    manifest: Mapping[str, Any],
) -> Mapping[str, Any]:
    unsigned = {
        "schema": RELEASE_ACTIVATION_BEGIN_SCHEMA,
        "transaction_sha256": manifest["transaction_sha256"],
        "mutation_begin_sha256": manifest["mutation_begin_sha256"],
        "successor_publication_sha256": manifest[
            "successor_publication_sha256"
        ],
        "release_update_publication_sha256": manifest[
            "release_update_publication_sha256"
        ],
        "successor_fixed_inputs_sha256": manifest[
            "successor_fixed_inputs_sha256"
        ],
        "live_activation_write_ahead_committed": True,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {
        **unsigned,
        "activation_begin_sha256": _sha(_canonical(unsigned)),
    }


def _abort_receipt_value(
    manifest: Mapping[str, Any],
) -> Mapping[str, Any]:
    unsigned = {
        "schema": RELEASE_ABORTED_RECEIPT_SCHEMA,
        "transaction_sha256": manifest["transaction_sha256"],
        "successor_publication_sha256": manifest[
            "successor_publication_sha256"
        ],
        "release_update_publication_sha256": manifest[
            "release_update_publication_sha256"
        ],
        "successor_fixed_inputs_sha256": manifest[
            "successor_fixed_inputs_sha256"
        ],
        "audit_transaction_path": manifest["audit_transaction_path"],
        "prepared_receipt_sha256": manifest[
            "prepared_receipt_sha256"
        ],
        "mutation_begin_sha256": manifest["mutation_begin_sha256"],
        "live_predecessor_unchanged": True,
        "live_mutation_performed": False,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {**unsigned, "receipt_sha256": _sha(_canonical(unsigned))}


def _final_receipt_value(
    manifest: Mapping[str, Any],
    documents: Mapping[str, Mapping[str, Any]],
    activation: Mapping[str, Any],
) -> Mapping[str, Any]:
    transaction = documents["transaction.json"]
    prepared = documents["prepared-receipt.json"]
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
        "audit_transaction_path": manifest["audit_transaction_path"],
        "staged_plan_path": prepared["live_plan_path"],
        "staged_approval_path": prepared["live_approval_path"],
        "fixed_inputs_path": prepared["live_fixed_inputs_path"],
        "successor_triplet_complete": True,
        "mutation_begin_sha256": manifest["mutation_begin_sha256"],
        "activation_begin_sha256": activation[
            "activation_begin_sha256"
        ],
        "prepared_receipt_sha256": manifest[
            "prepared_receipt_sha256"
        ],
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {**unsigned, "receipt_sha256": _sha(_canonical(unsigned))}


def _remove_exact_triplet_file(
    path: Path,
    expected: bytes,
    *,
    mode: int,
    trusted_uid: int = 0,
    trusted_gid: int = 0,
) -> None:
    raw, state = _stable_read(
        path,
        uid=trusted_uid,
        gid=trusted_gid,
        mode=mode,
        maximum=max(len(expected), 1),
        code="OFFLINE_DEPLOY_TRIPLET_REPLAY_FAILED",
    )
    try:
        reachable = path.lstat()
        if raw != expected or _identity(state) != _identity(reachable):
            _fail("OFFLINE_DEPLOY_TRIPLET_REPLAY_FAILED")
        path.unlink()
        _fsync_directory(path.parent)
    except OSError as exc:
        raise ReconcileError(
            "OFFLINE_DEPLOY_TRIPLET_REPLAY_FAILED"
        ) from exc


def _triplet_parent(
    manifest: Mapping[str, Any],
    *,
    trusted_uid: int,
    trusted_gid: int,
) -> Path:
    rows = [
        *manifest["predecessor_triplet"],
        *manifest["successor_triplet"],
    ]
    paths = [Path(str(row.get("live_path", ""))) for row in rows]
    expected_names = {
        "unit-input-plan.json",
        "unit-input-approval.json",
        "production-unit-inputs.json",
    }
    parents = {path.parent for path in paths}
    if (
        len(rows) != 6
        or len(parents) != 1
        or any(not path.is_absolute() for path in paths)
        or {path.name for path in paths} != expected_names
        or len(set(paths)) != 3
    ):
        _fail("OFFLINE_DEPLOY_TRIPLET_REPLAY_FAILED")
    parent = parents.pop()
    _directory(
        parent,
        uid=trusted_uid,
        gid=trusted_gid,
        mode=0o700,
        code="OFFLINE_DEPLOY_TRIPLET_REPLAY_FAILED",
    )
    return parent


def _converge_successor_triplet(
    manifest: Mapping[str, Any],
    *,
    predecessor: Mapping[str, bytes],
    successor: Mapping[str, bytes],
    trusted_uid: int = 0,
    trusted_gid: int = 0,
) -> None:
    parent = _triplet_parent(
        manifest,
        trusted_uid=trusted_uid,
        trusted_gid=trusted_gid,
    )
    rows = {
        row["logical"]: row
        for row in manifest["successor_triplet"]
    }
    if set(rows) != {"plan", "approval", "fixed"}:
        _fail("OFFLINE_DEPLOY_TRIPLET_REPLAY_FAILED")
    for logical in ("fixed", "approval", "plan"):
        row = rows[logical]
        path = Path(row["live_path"])
        mode = int(row["mode"])
        if os.path.lexists(path):
            raw, _state = _stable_read(
                path,
                uid=trusted_uid,
                gid=trusted_gid,
                mode=mode,
                maximum=max(
                    len(predecessor[logical]),
                    len(successor[logical]),
                ),
                code="OFFLINE_DEPLOY_TRIPLET_REPLAY_FAILED",
            )
            if raw == successor[logical]:
                continue
            if raw != predecessor[logical]:
                _fail("OFFLINE_DEPLOY_TRIPLET_REPLAY_FAILED")
            _remove_exact_triplet_file(
                path,
                predecessor[logical],
                mode=mode,
                trusted_uid=trusted_uid,
                trusted_gid=trusted_gid,
            )
    for logical in ("plan", "approval", "fixed"):
        row = rows[logical]
        _create_or_exact(
            Path(row["live_path"]),
            successor[logical],
            uid=trusted_uid,
            gid=trusted_gid,
            mode=int(row["mode"]),
            code="OFFLINE_DEPLOY_TRIPLET_REPLAY_FAILED",
        )
    _directory(
        parent,
        uid=trusted_uid,
        gid=trusted_gid,
        mode=0o700,
        code="OFFLINE_DEPLOY_TRIPLET_REPLAY_FAILED",
    )
    if (
        _triplet_state(
            manifest,
            trusted_uid=trusted_uid,
            trusted_gid=trusted_gid,
        )
        != "successor"
    ):
        _fail("OFFLINE_DEPLOY_TRIPLET_REPLAY_FAILED")


def _audit_terminal_binding(
    audit_path: Path,
    documents: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    transaction = documents["transaction.json"]
    prepared = documents["prepared-receipt.json"]
    mutation = documents["mutation-begin.json"]
    successor = transaction.get("successor")
    if not isinstance(successor, Mapping):
        _fail("OFFLINE_DEPLOY_PARALLEL_TRANSACTION_INVALID")
    return {
        "transaction_sha256": transaction.get("transaction_sha256"),
        "prepared_receipt_sha256": prepared.get("receipt_sha256"),
        "mutation_begin_sha256": mutation.get("mutation_begin_sha256"),
        "successor_publication_sha256": successor.get(
            "publication_sha256"
        ),
        "release_update_publication_sha256": successor.get(
            "release_update_publication_sha256"
        ),
        "successor_fixed_inputs_sha256": successor.get(
            "fixed_inputs_sha256"
        ),
        "audit_transaction_path": str(audit_path),
    }


def _require_other_audit_transactions_terminal(
    manifest: Mapping[str, Any],
    *,
    trusted_uid: int = 0,
    trusted_gid: int = 0,
) -> None:
    current = Path(manifest["audit_transaction_path"])
    root = current.parent
    before = _directory(
        root,
        uid=trusted_uid,
        gid=trusted_gid,
        mode=0o700,
        code="OFFLINE_DEPLOY_PARALLEL_TRANSACTION_INVALID",
    )
    try:
        directories = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise ReconcileError(
            "OFFLINE_DEPLOY_PARALLEL_TRANSACTION_INVALID"
        ) from exc
    if (
        current not in directories
        or any(
            path.name.startswith(".")
            or not re.fullmatch(r"[0-9a-f]{64}-[0-9a-f]{64}", path.name)
            for path in directories
        )
    ):
        _fail("OFFLINE_DEPLOY_PARALLEL_TRANSACTION_INVALID")
    observed_directories: list[tuple[str, tuple[int, ...]]] = []
    for directory in directories:
        state = _directory(
            directory,
            uid=trusted_uid,
            gid=trusted_gid,
            mode=0o700,
            code="OFFLINE_DEPLOY_PARALLEL_TRANSACTION_INVALID",
        )
        observed_directories.append((directory.name, _identity(state)))
        if directory == current:
            continue
        try:
            documents, _rows = _audit_documents(
                directory,
                uid=trusted_uid,
                gid=trusted_gid,
                allow_terminal=True,
            )
        except ReconcileError as exc:
            raise ReconcileError(
                "OFFLINE_DEPLOY_PARALLEL_TRANSACTION_INVALID"
            ) from exc
        transaction = documents["transaction.json"]
        predecessor = transaction.get("predecessor")
        successor = transaction.get("successor")
        if (
            not isinstance(predecessor, Mapping)
            or not isinstance(successor, Mapping)
        ):
            _fail("OFFLINE_DEPLOY_PARALLEL_TRANSACTION_INVALID")
        try:
            _validate_audit_semantics(
                documents,
                directory,
                str(predecessor.get("revision", "")),
                str(successor.get("revision", "")),
            )
        except ReconcileError as exc:
            raise ReconcileError(
                "OFFLINE_DEPLOY_PARALLEL_TRANSACTION_INVALID"
            ) from exc
        binding = _audit_terminal_binding(directory, documents)
        activation_path = directory / ACTIVATION_FILE
        abort_path = directory / ABORT_FILE
        final_path = directory / FINAL_FILE
        activation_present = os.path.lexists(activation_path)
        abort_present = os.path.lexists(abort_path)
        final_present = os.path.lexists(final_path)
        if abort_present:
            if activation_present or final_present:
                _fail("OFFLINE_DEPLOY_PARALLEL_TRANSACTION_INVALID")
            abort, _raw, _state = _read_json_file(
                abort_path,
                uid=trusted_uid,
                gid=trusted_gid,
                mode=0o400,
                code="OFFLINE_DEPLOY_PARALLEL_TRANSACTION_INVALID",
            )
            if abort != _abort_receipt_value(binding):
                _fail("OFFLINE_DEPLOY_PARALLEL_TRANSACTION_INVALID")
            continue
        if not activation_present or not final_present:
            _fail("OFFLINE_DEPLOY_PARALLEL_TRANSACTION_INVALID")
        activation, _raw, _state = _read_json_file(
            activation_path,
            uid=trusted_uid,
            gid=trusted_gid,
            mode=0o400,
            code="OFFLINE_DEPLOY_PARALLEL_TRANSACTION_INVALID",
        )
        expected_activation = _activation_begin_value(binding)
        if activation != expected_activation:
            _fail("OFFLINE_DEPLOY_PARALLEL_TRANSACTION_INVALID")
        final, _raw, _state = _read_json_file(
            final_path,
            uid=trusted_uid,
            gid=trusted_gid,
            mode=0o400,
            code="OFFLINE_DEPLOY_PARALLEL_TRANSACTION_INVALID",
        )
        if final != _final_receipt_value(
            binding,
            documents,
            expected_activation,
        ):
            _fail("OFFLINE_DEPLOY_PARALLEL_TRANSACTION_INVALID")
    after = _directory(
        root,
        uid=trusted_uid,
        gid=trusted_gid,
        mode=0o700,
        code="OFFLINE_DEPLOY_PARALLEL_TRANSACTION_INVALID",
    )
    try:
        after_directories = sorted(
            (
                path.name,
                _identity(path.lstat()),
            )
            for path in root.iterdir()
        )
    except OSError as exc:
        raise ReconcileError(
            "OFFLINE_DEPLOY_PARALLEL_TRANSACTION_INVALID"
        ) from exc
    if (
        _identity(before) != _identity(after)
        or observed_directories != after_directories
    ):
        _fail("OFFLINE_DEPLOY_PARALLEL_TRANSACTION_INVALID")


def _phase_result_value(
    request: Mapping[str, Any],
    *,
    receipt: Mapping[str, Any],
    activation: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    unsigned = {
        "schema": RELEASE_PHASE_RESULT_SCHEMA,
        "action": request["action"],
        "owner_release_revision": request[
            "owner_release_revision"
        ],
        "remote_stager_revision": request[
            "remote_stager_revision"
        ],
        "request_sha256": request["request_sha256"],
        "transaction_sha256": request[
            "expected_transaction_sha256"
        ],
        "audit_transaction_path": request[
            "prepared_receipt"
        ]["audit_transaction_path"],
        "canonical_receipt": dict(receipt),
        "canonical_receipt_sha256": receipt["receipt_sha256"],
        "activation_begin": (
            dict(activation) if activation is not None else None
        ),
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {**unsigned, "result_sha256": _sha(_canonical(unsigned))}


@contextmanager
def _authority_activation_lock() -> Iterator[None]:
    with _deploy_lock(
        AUTHORITY_ACTIVATION_LOCK_PATH,
        inherited_fd=None,
        expected_path=AUTHORITY_ACTIVATION_LOCK_PATH,
    ):
        yield


def _invoke_request(
    manifest: Mapping[str, Any],
    name: str,
    *,
    runner: Any = subprocess.run,
) -> Mapping[str, Any]:
    del runner
    request, _raw = _read_bound_request(manifest, name)
    request = _validate_replay_request(
        action=request["action"],
        request=request,
    )
    documents = _replay_documents(manifest)
    _validate_sealed_request_bindings(
        request,
        action=request["action"],
        owner_revision=manifest["owner_release_revision"],
        target_revision=manifest["target_revision"],
        documents=documents,
    )
    predecessor = _predecessor_triplet_bytes(manifest)
    successor = _derive_successor_triplet_bytes(manifest, request)
    audit_path = Path(manifest["audit_transaction_path"])
    with _authority_activation_lock():
        _require_other_audit_transactions_terminal(manifest)
        _triplet_parent(
            manifest,
            trusted_uid=0,
            trusted_gid=0,
        )
        if name == "abort":
            if request["action"] != ABORT_ACTION:
                _fail("OFFLINE_DEPLOY_PHASE_REPLAY_FAILED")
            presence = _audit_terminal_presence(manifest)
            if (
                presence["activation"]
                or presence["final"]
                or _triplet_state(manifest) != "predecessor"
            ):
                _fail("OFFLINE_DEPLOY_PHASE_REPLAY_FAILED")
            receipt = _abort_receipt_value(manifest)
            _create_or_exact(
                audit_path / ABORT_FILE,
                _canonical(receipt),
                uid=0,
                gid=0,
                mode=0o400,
                code="OFFLINE_DEPLOY_PHASE_REPLAY_FAILED",
            )
            activation = None
        elif name == "finalize":
            if (
                request["action"] != FINALIZE_ACTION
                or _audit_terminal_presence(manifest)["abort"]
            ):
                _fail("OFFLINE_DEPLOY_PHASE_REPLAY_FAILED")
            activation = _activation_begin_value(manifest)
            _create_or_exact(
                audit_path / ACTIVATION_FILE,
                _canonical(activation),
                uid=0,
                gid=0,
                mode=0o400,
                code="OFFLINE_DEPLOY_PHASE_REPLAY_FAILED",
            )
            _converge_successor_triplet(
                manifest,
                predecessor=predecessor,
                successor=successor,
            )
            receipt = _final_receipt_value(
                manifest,
                documents,
                activation,
            )
            _create_or_exact(
                audit_path / FINAL_FILE,
                _canonical(receipt),
                uid=0,
                gid=0,
                mode=0o400,
                code="OFFLINE_DEPLOY_PHASE_REPLAY_FAILED",
            )
        else:
            _fail("OFFLINE_DEPLOY_PHASE_REPLAY_FAILED")
    return _phase_result_value(
        request,
        receipt=receipt,
        activation=activation,
    )


def _select_link(manifest: Mapping[str, Any], decision: str) -> Path:
    active = Path(manifest["active_link"])
    target = Path(
        manifest[
            "successor_release"
            if decision == "finalized"
            else "predecessor_release"
        ]
    )
    current = _active_target(manifest)
    if current == "unknown":
        _fail("OFFLINE_DEPLOY_ACTIVE_LINK_UNKNOWN")
    if active.resolve(strict=True) == target:
        return target
    temporary = active.with_name(f".{active.name}.{os.getpid()}.next")
    try:
        os.symlink(target, temporary)
        os.replace(temporary, active)
        _fsync_directory(active.parent)
    except OSError as exc:
        raise ReconcileError("OFFLINE_DEPLOY_ACTIVE_LINK_FAILED") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    if active.resolve(strict=True) != target:
        _fail("OFFLINE_DEPLOY_ACTIVE_LINK_FAILED")
    return target


@contextmanager
def _deploy_lock(
    path: Path,
    *,
    inherited_fd: int | None,
    trusted_uid: int = 0,
    trusted_gid: int = 0,
    expected_path: Path = DEPLOY_LOCK_PATH,
) -> Iterator[None]:
    if path != expected_path or not path.is_absolute():
        _fail("OFFLINE_DEPLOY_DEPLOY_LOCK_INVALID")
    try:
        parent_before = path.parent.lstat()
        parent_resolved = path.parent.resolve(strict=True)
    except OSError as exc:
        raise ReconcileError(
            "OFFLINE_DEPLOY_DEPLOY_LOCK_INVALID"
        ) from exc
    if (
        parent_resolved != path.parent
        or not stat.S_ISDIR(parent_before.st_mode)
        or stat.S_ISLNK(parent_before.st_mode)
        or parent_before.st_uid != trusted_uid
        or parent_before.st_gid != trusted_gid
        or stat.S_IMODE(parent_before.st_mode) & 0o022
        or _xattrs(path.parent)
    ):
        _fail("OFFLINE_DEPLOY_DEPLOY_LOCK_INVALID")
    parent_descriptor = os.open(
        path.parent,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        parent_opened = os.fstat(parent_descriptor)
        parent_reachable = path.parent.lstat()
    finally:
        os.close(parent_descriptor)
    if (
        _identity(parent_before) != _identity(parent_opened)
        or _identity(parent_before) != _identity(parent_reachable)
    ):
        _fail("OFFLINE_DEPLOY_DEPLOY_LOCK_INVALID")

    def validate(descriptor: int) -> os.stat_result:
        try:
            before = path.lstat()
            opened = os.fstat(descriptor)
            reachable = path.lstat()
        except OSError as exc:
            raise ReconcileError(
                "OFFLINE_DEPLOY_DEPLOY_LOCK_INVALID"
            ) from exc
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != trusted_uid
            or opened.st_gid != trusted_gid
            or stat.S_IMODE(opened.st_mode) != 0o600
            or _xattrs(path)
            or _identity(before) != _identity(opened)
            or _identity(before) != _identity(reachable)
        ):
            _fail("OFFLINE_DEPLOY_DEPLOY_LOCK_INVALID")
        return opened

    descriptor: int | None = None
    if inherited_fd is not None:
        validate(inherited_fd)
        probe = os.open(
            path,
            os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                fcntl.flock(probe, fcntl.LOCK_UN)
                _fail("OFFLINE_DEPLOY_DEPLOY_LOCK_NOT_HELD")
        finally:
            os.close(probe)
        try:
            fcntl.flock(
                inherited_fd,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            raise ReconcileError(
                "OFFLINE_DEPLOY_DEPLOY_LOCK_NOT_HELD"
            ) from exc
        validate(inherited_fd)
        try:
            yield
        finally:
            validate(inherited_fd)
        return
    if not os.path.lexists(path):
        create_flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        create_flags |= getattr(os, "O_CLOEXEC", 0)
        create_flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            created_descriptor = os.open(path, create_flags, 0o600)
            try:
                os.fchown(
                    created_descriptor,
                    trusted_uid,
                    trusted_gid,
                )
                os.fchmod(created_descriptor, 0o600)
                os.fsync(created_descriptor)
            finally:
                os.close(created_descriptor)
            _fsync_directory(path.parent)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ReconcileError(
                "OFFLINE_DEPLOY_DEPLOY_LOCK_INVALID"
            ) from exc
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        validate(descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        validate(descriptor)
        try:
            yield
        finally:
            validate(descriptor)
    finally:
        if descriptor is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _enqueue_gateway_start(
    service_unit: str,
    *,
    runner: Any = subprocess.run,
) -> None:
    if (
        not isinstance(service_unit, str)
        or not service_unit
        or "/" in service_unit
        or not service_unit.endswith(".service")
    ):
        _fail("OFFLINE_DEPLOY_SERVICE_UNIT_INVALID")
    try:
        completed = runner(
            (
                "/usr/bin/systemctl",
                "start",
                "--no-block",
                "--",
                service_unit,
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd="/",
            env={"PATH": "/usr/bin:/bin"},
            shell=False,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReconcileError("OFFLINE_DEPLOY_GATEWAY_START_FAILED") from exc
    if completed.returncode != 0:
        _fail("OFFLINE_DEPLOY_GATEWAY_START_FAILED")


def _require_terminal_evidence(
    manifest: Mapping[str, Any],
    decision: str,
) -> None:
    presence = _audit_terminal_presence(manifest)
    expected_triplet = (
        "successor" if decision == "finalized" else "predecessor"
    )
    if (
        (
            decision == "finalized"
            and (
                not presence["activation"]
                or not presence["final"]
                or presence["abort"]
            )
        )
        or (
            decision == "aborted"
            and (
                presence["activation"]
                or presence["final"]
                or not presence["abort"]
            )
        )
        or _triplet_state(manifest) != expected_triplet
    ):
        _fail("OFFLINE_DEPLOY_TERMINAL_EVIDENCE_INVALID")


def _replay_terminal_phase(
    manifest_path: Path,
    manifest: Mapping[str, Any],
    *,
    request_name: str,
    runner: Any,
) -> Mapping[str, Any]:
    if request_name not in {"finalize", "abort"}:
        _fail("OFFLINE_DEPLOY_PHASE_REPLAY_FAILED")
    decision = "finalized" if request_name == "finalize" else "aborted"
    result = _invoke_request(
        manifest,
        request_name,
        runner=runner,
    )
    _require_terminal_evidence(manifest, decision)
    target = _select_link(manifest, decision)
    activation = result.get("activation_begin")
    request = manifest["requests"][request_name]
    unsigned = {
        "schema": TERMINAL_SCHEMA,
        "manifest_sha256": manifest["_manifest_file_sha256"],
        "decision": decision,
        "selected_request_sha256": request["request_sha256"],
        "phase_result_sha256": result["result_sha256"],
        "canonical_receipt_sha256": result[
            "canonical_receipt_sha256"
        ],
        "activation_begin_sha256": (
            activation["activation_begin_sha256"]
            if isinstance(activation, Mapping)
            else None
        ),
        "required_revision": (
            manifest["target_revision"]
            if decision == "finalized"
            else manifest["owner_release_revision"]
        ),
        "active_link_target": str(target),
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    terminal = {
        **unsigned,
        "receipt_sha256": _sha(_canonical(unsigned)),
    }
    _create_or_exact(
        _terminal_path(manifest_path),
        _canonical(terminal),
        uid=0,
        gid=0,
        mode=0o400,
        code="OFFLINE_DEPLOY_TERMINAL_INVALID",
    )
    return terminal


def quiesce(
    manifest_path: Path,
    *,
    expected_manifest_sha256: str | None = None,
    require_root: bool = True,
    inherited_lock_fd: int | None = None,
    runner: Any = subprocess.run,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> Mapping[str, Any]:
    """Owner-held preactivation drain/stop step; never selects F or S."""

    if require_root and (
        not sys.platform.startswith("linux") or os.geteuid() != 0
    ):
        _fail("OFFLINE_DEPLOY_ROOT_REQUIRED")
    if inherited_lock_fd is None:
        _fail("OFFLINE_DEPLOY_QUIESCE_REQUIRES_INHERITED_LOCK")
    manifest = load_manifest(
        manifest_path,
        expected_sha256=expected_manifest_sha256,
    )
    with _deploy_lock(
        Path(manifest["deploy_lock_path"]),
        inherited_fd=inherited_lock_fd,
    ):
        _validate_armed(_armed_path(manifest_path), manifest)
        if _read_terminal(manifest_path, manifest) is not None:
            _fail("OFFLINE_DEPLOY_QUIESCE_AFTER_TERMINAL")
        _validate_manifest_bindings(
            manifest,
            allow_terminal=False,
            runner=runner,
        )
        _attest_systemd(
            manifest_path,
            manifest,
            runner=runner,
            allow_create=False,
        )
        _republish_epoch_marker(manifest_path, manifest)
        stopped, receipt = _ensure_gateway_stopped(
            manifest_path,
            manifest,
            runner=runner,
            proc_root=proc_root,
            cgroup_root=cgroup_root,
        )
        return {
            "schema": "muncho-offline-deploy-quiesce.v1",
            "manifest_sha256": manifest["_manifest_file_sha256"],
            "stopped": stopped,
            "evidence_receipt_sha256": receipt["receipt_sha256"],
            "stop_evidence_schema": receipt["schema"],
            "activation_ready": (
                stopped and receipt["schema"] == STOP_SCHEMA
            ),
            "secret_material_recorded": False,
            "secret_digest_recorded": False,
        }


def activate_finalize(
    manifest_path: Path,
    *,
    expected_manifest_sha256: str | None = None,
    require_root: bool = True,
    inherited_lock_fd: int | None = None,
    runner: Any = subprocess.run,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> Mapping[str, Any]:
    """Owner-only normal activation under the continuously held deploy lock."""

    if require_root and (
        not sys.platform.startswith("linux") or os.geteuid() != 0
    ):
        _fail("OFFLINE_DEPLOY_ROOT_REQUIRED")
    if inherited_lock_fd is None:
        _fail("OFFLINE_DEPLOY_ACTIVATE_REQUIRES_INHERITED_LOCK")
    manifest = load_manifest(
        manifest_path,
        expected_sha256=expected_manifest_sha256,
    )
    with _deploy_lock(
        Path(manifest["deploy_lock_path"]),
        inherited_fd=inherited_lock_fd,
    ):
        _validate_armed(_armed_path(manifest_path), manifest)
        existing = _read_terminal(manifest_path, manifest)
        if existing is not None:
            if existing["decision"] != "finalized":
                _fail("OFFLINE_DEPLOY_ACTIVATE_AFTER_ABORT")
            converged = existing
        else:
            _validate_manifest_bindings(
                manifest,
                allow_terminal=False,
                runner=runner,
            )
            _attest_systemd(
                manifest_path,
                manifest,
                runner=runner,
                allow_create=False,
            )
            _republish_epoch_marker(manifest_path, manifest)
            stopped, stop_evidence = _ensure_gateway_stopped(
                manifest_path,
                manifest,
                runner=runner,
                proc_root=proc_root,
                cgroup_root=cgroup_root,
            )
            presence = _audit_terminal_presence(manifest)
            if (
                not stopped
                or stop_evidence.get("schema") != STOP_SCHEMA
                or any(presence.values())
                or _triplet_state(manifest) != "predecessor"
                or _active_target(manifest) != "predecessor"
            ):
                _fail("OFFLINE_DEPLOY_ACTIVATE_PRECONDITION_INVALID")
            converged = _replay_terminal_phase(
                manifest_path,
                manifest,
                request_name="finalize",
                runner=runner,
            )
    _enqueue_gateway_start(manifest["service_unit"], runner=runner)
    return converged


def reconcile(
    manifest_path: Path,
    *,
    expected_manifest_sha256: str | None = None,
    require_root: bool = True,
    inherited_lock_fd: int | None = None,
    runner: Any = subprocess.run,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> Mapping[str, Any]:
    if require_root and (not sys.platform.startswith("linux") or os.geteuid() != 0):
        _fail("OFFLINE_DEPLOY_ROOT_REQUIRED")
    manifest = load_manifest(
        manifest_path,
        expected_sha256=expected_manifest_sha256,
    )
    if (
        not os.path.lexists(_armed_path(manifest_path))
        and not os.path.lexists(_arm_intent_path(manifest_path))
    ):
        return inspect(
            manifest_path,
            expected_manifest_sha256=manifest[
                "_manifest_file_sha256"
            ],
            runner=runner,
        )
    converged: Mapping[str, Any]
    should_enqueue = True
    with _deploy_lock(
        Path(manifest["deploy_lock_path"]),
        inherited_fd=inherited_lock_fd,
    ):
        if not os.path.lexists(_armed_path(manifest_path)):
            _validate_arm_intent(
                _arm_intent_path(manifest_path),
                manifest,
            )
            _validate_manifest_bindings(
                manifest,
                allow_terminal=True,
                runner=runner,
            )
            _attest_systemd(
                manifest_path,
                manifest,
                runner=runner,
                allow_create=False,
            )
            _republish_epoch_marker(manifest_path, manifest)
            _publish_armed(manifest_path, manifest)
        _validate_armed(_armed_path(manifest_path), manifest)
        _attest_systemd(
            manifest_path,
            manifest,
            runner=runner,
            allow_create=False,
        )
        terminal = _read_terminal(manifest_path, manifest)
        if terminal is not None:
            _validate_manifest_bindings(
                manifest,
                allow_terminal=True,
                runner=runner,
            )
            _republish_epoch_marker(manifest_path, manifest)
            _require_terminal_evidence(manifest, terminal["decision"])
            _select_link(manifest, terminal["decision"])
            converged = terminal
        else:
            _validate_manifest_bindings(
                manifest,
                allow_terminal=True,
                runner=runner,
            )
            _republish_epoch_marker(manifest_path, manifest)
            stopped, stop_evidence = _ensure_gateway_stopped(
                manifest_path,
                manifest,
                runner=runner,
                proc_root=proc_root,
                cgroup_root=cgroup_root,
            )
            if not stopped:
                should_enqueue = False
                converged = {
                    "schema": "muncho-offline-deploy-quiesce.v1",
                    "manifest_sha256": manifest[
                        "_manifest_file_sha256"
                    ],
                    "stopped": False,
                    "evidence_receipt_sha256": stop_evidence[
                        "receipt_sha256"
                    ],
                    "stop_evidence_schema": stop_evidence["schema"],
                    "activation_ready": False,
                    "secret_material_recorded": False,
                    "secret_digest_recorded": False,
                }
            else:
                presence = _audit_terminal_presence(manifest)
                triplet = _triplet_state(manifest)
                must_finalize = (
                    presence["final"]
                    or presence["activation"]
                    or triplet != "predecessor"
                )
                if must_finalize and presence["abort"]:
                    _fail("OFFLINE_DEPLOY_TERMINAL_CONFLICT")
                converged = _replay_terminal_phase(
                    manifest_path,
                    manifest,
                    request_name=(
                        "finalize" if must_finalize else "abort"
                    ),
                    runner=runner,
                )
    if should_enqueue:
        _enqueue_gateway_start(manifest["service_unit"], runner=runner)
    return converged


def _service_incarnation(
    manifest: Mapping[str, Any],
    terminal: Mapping[str, Any],
    *,
    runner: Any,
    proc_root: Path,
) -> Mapping[str, Any]:
    def read_properties() -> dict[str, str]:
        output = _systemctl(
            (
                "show",
                manifest["service_unit"],
                "--property=ActiveState,SubState,MainPID,InvocationID",
            ),
            runner=runner,
        )
        observed: dict[str, str] = {}
        for line in output.splitlines():
            name, separator, item = line.partition("=")
            if not separator or name in observed:
                _fail("OFFLINE_DEPLOY_GATEWAY_INCARNATION_INVALID")
            observed[name] = item
        if set(observed) != {
            "ActiveState",
            "SubState",
            "MainPID",
            "InvocationID",
        }:
            _fail("OFFLINE_DEPLOY_GATEWAY_INCARNATION_INVALID")
        return observed

    properties = read_properties()
    try:
        pid = int(properties["MainPID"])
    except ValueError as exc:
        raise ReconcileError(
            "OFFLINE_DEPLOY_GATEWAY_INCARNATION_INVALID"
        ) from exc
    invocation_id = properties["InvocationID"]
    if (
        properties["ActiveState"] != "active"
        or properties["SubState"] != "running"
        or pid <= 1
        or re.fullmatch(r"[0-9a-f]{32}", invocation_id) is None
    ):
        _fail("OFFLINE_DEPLOY_GATEWAY_INCARNATION_INVALID")
    stat_path = proc_root / str(pid) / "stat"
    exe_path = proc_root / str(pid) / "exe"
    try:
        first_stat = stat_path.read_text(encoding="utf-8")
        first_start = first_stat.rsplit(")", 1)[1].split()[19]
        first_exe = exe_path.resolve(strict=True)
        second_stat = stat_path.read_text(encoding="utf-8")
        second_start = second_stat.rsplit(")", 1)[1].split()[19]
        second_exe = exe_path.resolve(strict=True)
    except (OSError, IndexError) as exc:
        raise ReconcileError(
            "OFFLINE_DEPLOY_GATEWAY_INCARNATION_INVALID"
        ) from exc
    runtime_executable = (
        Path(terminal["active_link_target"]) / ".venv/bin/python"
    )
    try:
        expected_exe = runtime_executable.resolve(strict=True)
    except OSError as exc:
        raise ReconcileError(
            "OFFLINE_DEPLOY_GATEWAY_INCARNATION_INVALID"
        ) from exc
    if (
        not first_start.isdigit()
        or first_start != second_start
        or first_exe != second_exe
        or first_exe != expected_exe
        or Path(manifest["active_link"]).resolve(strict=True)
        != Path(terminal["active_link_target"])
        or read_properties() != properties
    ):
        _fail("OFFLINE_DEPLOY_GATEWAY_INCARNATION_INVALID")
    unsigned = {
        "service_unit": manifest["service_unit"],
        "pid": pid,
        "systemd_invocation_id": invocation_id,
        "process_start_ticks": first_start,
        "runtime_executable": str(runtime_executable),
        "runtime_executable_target": str(expected_exe),
        "active_link_target": terminal["active_link_target"],
        "revision": terminal["required_revision"],
    }
    return {
        **unsigned,
        "gateway_incarnation_sha256": _sha(_canonical(unsigned)),
    }


def _health_probe_header(
    value: Any,
    *,
    expected_fields: set[str],
    schema: str,
    manifest: Mapping[str, Any],
    terminal: Mapping[str, Any],
    gateway_incarnation_sha256: str,
) -> Mapping[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != expected_fields
        or value.get("schema") != schema
        or value.get("manifest_sha256")
        != manifest["_manifest_file_sha256"]
        or value.get("gateway_incarnation_sha256")
        != gateway_incarnation_sha256
        or value.get("revision") != terminal["required_revision"]
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
    ):
        _fail("OFFLINE_DEPLOY_HEALTH_INVALID")
    _self_digest(
        value,
        "receipt_sha256",
        "OFFLINE_DEPLOY_HEALTH_INVALID",
    )
    return value


def _validate_model_probe(
    value: Any,
    *,
    manifest: Mapping[str, Any],
    terminal: Mapping[str, Any],
    gateway_incarnation_sha256: str,
) -> None:
    probe = _health_probe_header(
        value,
        expected_fields={
            "schema",
            "manifest_sha256",
            "gateway_incarnation_sha256",
            "revision",
            "provider",
            "requested_model",
            "response_model",
            "api_status_code",
            "completed",
            "input_tokens",
            "output_tokens",
            "request_id_sha256",
            "response_id_sha256",
            "secret_material_recorded",
            "secret_digest_recorded",
            "receipt_sha256",
        },
        schema=MODEL_HEALTH_SCHEMA,
        manifest=manifest,
        terminal=terminal,
        gateway_incarnation_sha256=gateway_incarnation_sha256,
    )
    if (
        probe.get("provider") != "openai-codex"
        or probe.get("requested_model") != "gpt-5.6-sol"
        or probe.get("response_model") != "gpt-5.6-sol"
        or probe.get("api_status_code") != 200
        or probe.get("completed") is not True
        or type(probe.get("input_tokens")) is not int
        or not 1 <= probe["input_tokens"] <= 10_000_000
        or type(probe.get("output_tokens")) is not int
        or not 1 <= probe["output_tokens"] <= 1_000_000
        or SHA256.fullmatch(str(probe.get("request_id_sha256", "")))
        is None
        or SHA256.fullmatch(str(probe.get("response_id_sha256", "")))
        is None
    ):
        _fail("OFFLINE_DEPLOY_HEALTH_INVALID")


def _validate_secret_manager_probe(
    value: Any,
    *,
    manifest: Mapping[str, Any],
    terminal: Mapping[str, Any],
    gateway_incarnation_sha256: str,
) -> None:
    probe = _health_probe_header(
        value,
        expected_fields={
            "schema",
            "manifest_sha256",
            "gateway_incarnation_sha256",
            "revision",
            "project_id",
            "access_principal_sha256",
            "required_resource_set_sha256",
            "required_resource_count",
            "access_attempt_count",
            "access_success_count",
            "rpc_status_code",
            "secret_payload_material_recorded",
            "secret_payload_digest_recorded",
            "secret_version_recorded",
            "secret_material_recorded",
            "secret_digest_recorded",
            "receipt_sha256",
        },
        schema=SECRET_MANAGER_HEALTH_SCHEMA,
        manifest=manifest,
        terminal=terminal,
        gateway_incarnation_sha256=gateway_incarnation_sha256,
    )
    required = probe.get("required_resource_count")
    if (
        probe.get("project_id") != "adventico-ai-platform"
        or SHA256.fullmatch(
            str(probe.get("access_principal_sha256", ""))
        )
        is None
        or SHA256.fullmatch(
            str(probe.get("required_resource_set_sha256", ""))
        )
        is None
        or type(required) is not int
        or not 1 <= required <= 64
        or type(probe.get("access_attempt_count")) is not int
        or probe.get("access_attempt_count") != required
        or type(probe.get("access_success_count")) is not int
        or probe.get("access_success_count") != required
        or type(probe.get("rpc_status_code")) is not int
        or probe.get("rpc_status_code") != 0
        or probe.get("secret_payload_material_recorded") is not False
        or probe.get("secret_payload_digest_recorded") is not False
        or probe.get("secret_version_recorded") is not False
    ):
        _fail("OFFLINE_DEPLOY_HEALTH_INVALID")


def _validate_cloud_sql_probe(
    value: Any,
    *,
    manifest: Mapping[str, Any],
    terminal: Mapping[str, Any],
    gateway_incarnation_sha256: str,
) -> None:
    probe = _health_probe_header(
        value,
        expected_fields={
            "schema",
            "manifest_sha256",
            "gateway_incarnation_sha256",
            "revision",
            "project_id",
            "database",
            "instance_identity_sha256",
            "tls_server_name_sha256",
            "server_ca_sha256",
            "peer_certificate_spki_sha256",
            "tls_in_use",
            "tls_verify_full",
            "tls_protocol",
            "server_version_num",
            "backend_pid",
            "connect_status_code",
            "sqlstate",
            "secret_material_recorded",
            "secret_digest_recorded",
            "receipt_sha256",
        },
        schema=CLOUD_SQL_HEALTH_SCHEMA,
        manifest=manifest,
        terminal=terminal,
        gateway_incarnation_sha256=gateway_incarnation_sha256,
    )
    if (
        probe.get("project_id") != "adventico-ai-platform"
        or probe.get("database") != "ai_platform_brain"
        or probe.get("tls_in_use") is not True
        or probe.get("tls_verify_full") is not True
        or probe.get("tls_protocol") not in {"TLSv1.2", "TLSv1.3"}
        or type(probe.get("server_version_num")) is not int
        or not 120_000 <= probe["server_version_num"] <= 200_000
        or type(probe.get("backend_pid")) is not int
        or probe["backend_pid"] <= 1
        or type(probe.get("connect_status_code")) is not int
        or probe.get("connect_status_code") != 0
        or probe.get("sqlstate") != "00000"
        or any(
            SHA256.fullmatch(str(probe.get(name, ""))) is None
            for name in (
                "instance_identity_sha256",
                "tls_server_name_sha256",
                "server_ca_sha256",
                "peer_certificate_spki_sha256",
            )
        )
    ):
        _fail("OFFLINE_DEPLOY_HEALTH_INVALID")


def _validate_canonical_query_probe(
    value: Any,
    *,
    manifest: Mapping[str, Any],
    terminal: Mapping[str, Any],
    gateway_incarnation_sha256: str,
) -> None:
    probe = _health_probe_header(
        value,
        expected_fields={
            "schema",
            "manifest_sha256",
            "gateway_incarnation_sha256",
            "revision",
            "service",
            "protocol",
            "database_identity",
            "operation",
            "request_id_sha256",
            "sentinel_case_id_sha256",
            "result_count",
            "protocol_status_code",
            "read_only",
            "secret_material_recorded",
            "secret_digest_recorded",
            "receipt_sha256",
        },
        schema=CANONICAL_QUERY_HEALTH_SCHEMA,
        manifest=manifest,
        terminal=terminal,
        gateway_incarnation_sha256=gateway_incarnation_sha256,
    )
    if (
        probe.get("service") != "canonical_writer"
        or probe.get("protocol") != "v1"
        or probe.get("database_identity")
        != "canonical_brain_migration_owner"
        or probe.get("operation") != "case.query"
        or type(probe.get("result_count")) is not int
        or probe.get("result_count") != 0
        or type(probe.get("protocol_status_code")) is not int
        or probe.get("protocol_status_code") != 0
        or probe.get("read_only") is not True
        or SHA256.fullmatch(str(probe.get("request_id_sha256", "")))
        is None
        or SHA256.fullmatch(
            str(probe.get("sentinel_case_id_sha256", ""))
        )
        is None
    ):
        _fail("OFFLINE_DEPLOY_HEALTH_INVALID")


def _validate_privileged_writer_probe(
    value: Any,
    *,
    manifest: Mapping[str, Any],
    terminal: Mapping[str, Any],
    gateway_incarnation_sha256: str,
) -> None:
    probe = _health_probe_header(
        value,
        expected_fields={
            "schema",
            "manifest_sha256",
            "gateway_incarnation_sha256",
            "revision",
            "database",
            "session_user_sha256",
            "transaction_sha256",
            "advisory_lock_key_sha256",
            "inserted_event_id_sha256",
            "isolation_level",
            "advisory_lock_acquired",
            "append_only",
            "insert_row_count",
            "readback_row_count",
            "update_row_count",
            "delete_row_count",
            "rollback_completed",
            "transaction_committed",
            "fresh_session_read_count",
            "transaction_sqlstate",
            "rollback_sqlstate",
            "fresh_read_sqlstate",
            "secret_material_recorded",
            "secret_digest_recorded",
            "receipt_sha256",
        },
        schema=PRIVILEGED_WRITER_HEALTH_SCHEMA,
        manifest=manifest,
        terminal=terminal,
        gateway_incarnation_sha256=gateway_incarnation_sha256,
    )
    if (
        probe.get("database") != "ai_platform_brain"
        or probe.get("isolation_level") != "serializable"
        or probe.get("advisory_lock_acquired") is not True
        or probe.get("append_only") is not True
        or any(
            type(probe.get(name)) is not int
            for name in (
                "insert_row_count",
                "readback_row_count",
                "update_row_count",
                "delete_row_count",
                "fresh_session_read_count",
            )
        )
        or probe.get("insert_row_count") != 1
        or probe.get("readback_row_count") != 1
        or probe.get("update_row_count") != 0
        or probe.get("delete_row_count") != 0
        or probe.get("rollback_completed") is not True
        or probe.get("transaction_committed") is not False
        or probe.get("fresh_session_read_count") != 0
        or probe.get("transaction_sqlstate") != "00000"
        or probe.get("rollback_sqlstate") != "00000"
        or probe.get("fresh_read_sqlstate") != "00000"
        or any(
            SHA256.fullmatch(str(probe.get(name, ""))) is None
            for name in (
                "session_user_sha256",
                "transaction_sha256",
                "advisory_lock_key_sha256",
                "inserted_event_id_sha256",
            )
        )
    ):
        _fail("OFFLINE_DEPLOY_HEALTH_INVALID")


def _snowflake(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.isdecimal()
        and 17 <= len(value) <= 20
        and int(value) > 0
    )


def _validate_discord_probe(
    value: Any,
    *,
    manifest: Mapping[str, Any],
    terminal: Mapping[str, Any],
    gateway_incarnation_sha256: str,
) -> None:
    probe = _health_probe_header(
        value,
        expected_fields={
            "schema",
            "manifest_sha256",
            "gateway_incarnation_sha256",
            "revision",
            "guild_id",
            "channel_id",
            "request_author_user_id",
            "request_message_id",
            "request_content_sha256",
            "response_message_id",
            "response_author_id_sha256",
            "response_content_sha256",
            "reply_reference_message_id",
            "response_count",
            "round_trip_milliseconds",
            "request_delivery_status_code",
            "response_observation_status_code",
            "permit",
            "permit_consumption",
            "secret_material_recorded",
            "secret_digest_recorded",
            "receipt_sha256",
        },
        schema=DISCORD_HEALTH_SCHEMA,
        manifest=manifest,
        terminal=terminal,
        gateway_incarnation_sha256=gateway_incarnation_sha256,
    )
    permit = probe.get("permit")
    consumption = probe.get("permit_consumption")
    permit_fields = {
        "schema",
        "manifest_sha256",
        "transaction_sha256",
        "mutation_capability_sha256",
        "epoch_sha256",
        "gateway_incarnation_sha256",
        "guild_id",
        "channel_id",
        "request_author_user_id",
        "request_content_sha256",
        "probe_nonce_sha256",
        "maximum_uses",
        "issued_by_root",
        "secret_material_recorded",
        "secret_digest_recorded",
        "receipt_sha256",
    }
    if (
        not isinstance(permit, Mapping)
        or set(permit) != permit_fields
        or permit.get("schema") != DISCORD_PERMIT_SCHEMA
        or permit.get("manifest_sha256")
        != manifest["_manifest_file_sha256"]
        or permit.get("transaction_sha256")
        != manifest["transaction_sha256"]
        or permit.get("mutation_capability_sha256")
        != manifest["drain_mutation_capability_sha256"]
        or permit.get("epoch_sha256")
        != _sha(_current_epoch().encode("utf-8"))
        or permit.get("gateway_incarnation_sha256")
        != gateway_incarnation_sha256
        or permit.get("guild_id") != "1282725267068157972"
        or permit.get("channel_id") != "1504852355588423801"
        or permit.get("request_author_user_id") != "1279454038731264061"
        or permit.get("request_content_sha256")
        != probe.get("request_content_sha256")
        or SHA256.fullmatch(str(permit.get("probe_nonce_sha256", "")))
        is None
        or type(permit.get("maximum_uses")) is not int
        or permit.get("maximum_uses") != 1
        or permit.get("issued_by_root") is not True
        or permit.get("secret_material_recorded") is not False
        or permit.get("secret_digest_recorded") is not False
    ):
        _fail("OFFLINE_DEPLOY_HEALTH_INVALID")
    _self_digest(
        permit,
        "receipt_sha256",
        "OFFLINE_DEPLOY_HEALTH_INVALID",
    )
    consumption_fields = {
        "schema",
        "permit_receipt_sha256",
        "gateway_incarnation_sha256",
        "request_message_id",
        "consumed_uses",
        "remaining_uses",
        "atomic_create",
        "secret_material_recorded",
        "secret_digest_recorded",
        "receipt_sha256",
    }
    if (
        not isinstance(consumption, Mapping)
        or set(consumption) != consumption_fields
        or consumption.get("schema")
        != DISCORD_PERMIT_CONSUMPTION_SCHEMA
        or consumption.get("permit_receipt_sha256")
        != permit["receipt_sha256"]
        or consumption.get("gateway_incarnation_sha256")
        != gateway_incarnation_sha256
        or consumption.get("request_message_id")
        != probe.get("request_message_id")
        or type(consumption.get("consumed_uses")) is not int
        or consumption.get("consumed_uses") != 1
        or type(consumption.get("remaining_uses")) is not int
        or consumption.get("remaining_uses") != 0
        or consumption.get("atomic_create") is not True
        or consumption.get("secret_material_recorded") is not False
        or consumption.get("secret_digest_recorded") is not False
    ):
        _fail("OFFLINE_DEPLOY_HEALTH_INVALID")
    _self_digest(
        consumption,
        "receipt_sha256",
        "OFFLINE_DEPLOY_HEALTH_INVALID",
    )
    if (
        probe.get("guild_id") != permit["guild_id"]
        or probe.get("channel_id") != permit["channel_id"]
        or probe.get("request_author_user_id")
        != permit["request_author_user_id"]
        or not _snowflake(probe.get("request_message_id"))
        or not _snowflake(probe.get("response_message_id"))
        or probe.get("request_message_id")
        == probe.get("response_message_id")
        or probe.get("reply_reference_message_id")
        != probe.get("request_message_id")
        or type(probe.get("response_count")) is not int
        or probe.get("response_count") != 1
        or type(probe.get("round_trip_milliseconds")) is not int
        or not 0 <= probe["round_trip_milliseconds"] <= 30_000
        or type(probe.get("request_delivery_status_code")) is not int
        or probe.get("request_delivery_status_code") != 0
        or type(probe.get("response_observation_status_code")) is not int
        or probe.get("response_observation_status_code") != 0
        or any(
            SHA256.fullmatch(str(probe.get(name, ""))) is None
            for name in (
                "request_content_sha256",
                "response_author_id_sha256",
                "response_content_sha256",
            )
        )
    ):
        _fail("OFFLINE_DEPLOY_HEALTH_INVALID")


def _validate_final_health(
    value: Any,
    *,
    manifest: Mapping[str, Any],
    terminal: Mapping[str, Any],
    incarnation: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != HEALTH_FIELDS
        or value.get("schema") != HEALTH_SCHEMA
        or value.get("manifest_sha256")
        != manifest["_manifest_file_sha256"]
        or value.get("decision") != terminal["decision"]
        or value.get("revision") != terminal["required_revision"]
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
    ):
        _fail("OFFLINE_DEPLOY_HEALTH_INVALID")
    gateway = value.get("gateway_incarnation")
    gateway_fields = {
        "service_unit",
        "pid",
        "systemd_invocation_id",
        "process_start_ticks",
        "runtime_executable",
        "runtime_executable_target",
        "active_link_target",
        "revision",
        "gateway_incarnation_sha256",
    }
    if (
        not isinstance(gateway, Mapping)
        or set(gateway) != gateway_fields
        or gateway.get("service_unit") != manifest["service_unit"]
        or gateway.get("revision") != terminal["required_revision"]
        or gateway.get("active_link_target")
        != terminal["active_link_target"]
        or gateway.get("runtime_executable")
        != str(
            Path(terminal["active_link_target"]) / ".venv/bin/python"
        )
        or not isinstance(gateway.get("runtime_executable_target"), str)
        or not Path(gateway["runtime_executable_target"]).is_absolute()
        or ".." in Path(gateway["runtime_executable_target"]).parts
        or type(gateway.get("pid")) is not int
        or gateway["pid"] <= 1
        or not isinstance(gateway.get("process_start_ticks"), str)
        or not gateway["process_start_ticks"].isdecimal()
        or int(gateway["process_start_ticks"]) <= 0
        or not isinstance(gateway.get("systemd_invocation_id"), str)
        or len(gateway["systemd_invocation_id"]) != 32
        or any(
            character not in "0123456789abcdef"
            for character in gateway["systemd_invocation_id"]
        )
    ):
        _fail("OFFLINE_DEPLOY_HEALTH_INVALID")
    _self_digest(
        gateway,
        "gateway_incarnation_sha256",
        "OFFLINE_DEPLOY_HEALTH_INVALID",
    )
    if incarnation is not None and gateway != incarnation:
        _fail("OFFLINE_DEPLOY_HEALTH_INVALID")
    digest = str(gateway["gateway_incarnation_sha256"])
    for validator, field in (
        (_validate_model_probe, "model_probe"),
        (_validate_secret_manager_probe, "secret_manager_probe"),
        (_validate_cloud_sql_probe, "cloud_sql_probe"),
        (_validate_canonical_query_probe, "canonical_query_probe"),
        (_validate_privileged_writer_probe, "privileged_writer_probe"),
        (_validate_discord_probe, "discord_probe"),
    ):
        validator(
            value.get(field),
            manifest=manifest,
            terminal=terminal,
            gateway_incarnation_sha256=digest,
        )
    _self_digest(
        value,
        "receipt_sha256",
        "OFFLINE_DEPLOY_HEALTH_INVALID",
    )
    return dict(value)


def commit_health(
    manifest_path: Path,
    receipt_path: Path,
    *,
    expected_manifest_sha256: str | None = None,
    require_root: bool = True,
    runner: Any = subprocess.run,
    proc_root: Path = Path("/proc"),
) -> Mapping[str, Any]:
    if require_root and (not sys.platform.startswith("linux") or os.geteuid() != 0):
        _fail("OFFLINE_DEPLOY_ROOT_REQUIRED")
    manifest = load_manifest(
        manifest_path,
        expected_sha256=expected_manifest_sha256,
    )
    terminal = _read_terminal(manifest_path, manifest)
    if terminal is None:
        _fail("OFFLINE_DEPLOY_HEALTH_BEFORE_TERMINAL")
    _validate_manifest_bindings(
        manifest,
        allow_terminal=True,
        runner=runner,
    )
    _require_terminal_evidence(manifest, terminal["decision"])
    if (
        _active_target(manifest)
        != (
            "successor"
            if terminal["decision"] == "finalized"
            else "predecessor"
        )
        or not _marker_current(manifest_path, manifest)
    ):
        _fail("OFFLINE_DEPLOY_HEALTH_PHYSICAL_STATE_INVALID")
    incarnation = _service_incarnation(
        manifest,
        terminal,
        runner=runner,
        proc_root=proc_root,
    )
    value, raw, _state = _read_json_file(
        receipt_path,
        uid=0,
        gid=0,
        mode=0o400,
        code="OFFLINE_DEPLOY_HEALTH_INVALID",
    )
    _validate_final_health(
        value,
        manifest=manifest,
        terminal=terminal,
        incarnation=incarnation,
    )
    health_dir = _health_dir(manifest_path)
    if not os.path.lexists(health_dir):
        health_dir.mkdir(mode=0o700)
        os.chown(health_dir, 0, 0)
        os.chmod(health_dir, 0o700)
        _fsync_directory(health_dir.parent)
    _directory(
        health_dir,
        uid=0,
        gid=0,
        mode=0o700,
        code="OFFLINE_DEPLOY_HEALTH_INVALID",
    )
    _create_or_exact(
        _health_path(
            manifest_path,
            value["gateway_incarnation"]["gateway_incarnation_sha256"],
        ),
        raw,
        uid=0,
        gid=0,
        mode=0o400,
        code="OFFLINE_DEPLOY_HEALTH_INVALID",
    )
    return value


def _read_health(
    manifest_path: Path,
    manifest: Mapping[str, Any],
    terminal: Mapping[str, Any],
    *,
    incarnation_sha256: str | None = None,
    receipt_sha256: str | None = None,
) -> Mapping[str, Any]:
    directory = _health_dir(manifest_path)
    if not os.path.lexists(directory):
        _fail("OFFLINE_DEPLOY_HEALTH_MISSING")
    _directory(
        directory,
        uid=0,
        gid=0,
        mode=0o700,
        code="OFFLINE_DEPLOY_HEALTH_INVALID",
    )
    if (
        incarnation_sha256 is not None
        and SHA256.fullmatch(incarnation_sha256) is None
    ) or (
        receipt_sha256 is not None
        and SHA256.fullmatch(receipt_sha256) is None
    ):
        _fail("OFFLINE_DEPLOY_HEALTH_INVALID")
    matches: list[Mapping[str, Any]] = []
    for path in sorted(directory.iterdir()):
        if (
            not path.name.endswith(".json")
            or SHA256.fullmatch(path.name[:-5]) is None
        ):
            _fail("OFFLINE_DEPLOY_HEALTH_INVALID")
        value, _raw, _state = _read_json_file(
            path,
            uid=0,
            gid=0,
            mode=0o400,
            code="OFFLINE_DEPLOY_HEALTH_INVALID",
        )
        _validate_final_health(
            value,
            manifest=manifest,
            terminal=terminal,
            incarnation=None,
        )
        gateway_incarnation_sha256 = value["gateway_incarnation"][
            "gateway_incarnation_sha256"
        ]
        if gateway_incarnation_sha256 != path.name[:-5]:
            _fail("OFFLINE_DEPLOY_HEALTH_INVALID")
        if (
            incarnation_sha256 is not None
            and gateway_incarnation_sha256 != incarnation_sha256
        ) or (
            receipt_sha256 is not None
            and value["receipt_sha256"] != receipt_sha256
        ):
            continue
        matches.append(value)
    if len(matches) != 1:
        _fail("OFFLINE_DEPLOY_HEALTH_MISSING")
    return matches[0]


def _read_cleanup(
    manifest_path: Path,
    manifest: Mapping[str, Any],
    terminal: Mapping[str, Any],
) -> Mapping[str, Any]:
    value, _raw, _state = _read_json_file(
        _cleanup_path(manifest_path),
        uid=0,
        gid=0,
        mode=0o400,
        code="OFFLINE_DEPLOY_CLEANUP_INVALID",
    )
    health = _read_health(manifest_path, manifest, terminal)
    if (
        set(value)
        != {
            "schema",
            "manifest_sha256",
            "terminal_receipt_sha256",
            "health_receipt_sha256",
            "required_revision",
            "secret_material_recorded",
            "secret_digest_recorded",
            "receipt_sha256",
        }
        or value.get("schema") != CLEANUP_SCHEMA
        or value.get("manifest_sha256") != manifest["_manifest_file_sha256"]
        or value.get("terminal_receipt_sha256")
        != terminal["receipt_sha256"]
        or value.get("health_receipt_sha256")
        != health["receipt_sha256"]
        or value.get("required_revision") != terminal["required_revision"]
        or value.get("secret_material_recorded") is not False
        or value.get("secret_digest_recorded") is not False
    ):
        _fail("OFFLINE_DEPLOY_CLEANUP_INVALID")
    _self_digest(value, "receipt_sha256", "OFFLINE_DEPLOY_CLEANUP_INVALID")
    return value


def cleanup(
    manifest_path: Path,
    *,
    expected_manifest_sha256: str | None = None,
    require_root: bool = True,
    runner: Any = subprocess.run,
) -> Mapping[str, Any]:
    if require_root and (not sys.platform.startswith("linux") or os.geteuid() != 0):
        _fail("OFFLINE_DEPLOY_ROOT_REQUIRED")
    manifest = load_manifest(
        manifest_path,
        expected_sha256=expected_manifest_sha256,
    )
    terminal = _read_terminal(manifest_path, manifest)
    if terminal is None:
        _fail("OFFLINE_DEPLOY_CLEANUP_BEFORE_TERMINAL")
    health = _read_health(manifest_path, manifest, terminal)
    unsigned = {
        "schema": CLEANUP_SCHEMA,
        "manifest_sha256": manifest["_manifest_file_sha256"],
        "terminal_receipt_sha256": terminal["receipt_sha256"],
        "health_receipt_sha256": health["receipt_sha256"],
        "required_revision": terminal["required_revision"],
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    commit = {
        **unsigned,
        "receipt_sha256": _sha(_canonical(unsigned)),
    }
    _create_or_exact(
        _cleanup_path(manifest_path),
        _canonical(commit),
        uid=0,
        gid=0,
        mode=0o400,
        code="OFFLINE_DEPLOY_CLEANUP_INVALID",
    )
    marker = Path(manifest["drain_marker_path"])
    with _shared_drain_lock(
        Path(manifest["drain_lock_path"]),
        uid=manifest["gateway_uid"],
        gid=manifest["gateway_gid"],
        create=False,
    ):
        try:
            marker.unlink()
            _fsync_directory(marker.parent)
        except FileNotFoundError:
            pass
    roles = {
        role: Path(path)
        for role, path in manifest["scaffolding_paths"].items()
    }
    recovery_unit = roles["recovery_unit"].name
    recovery_timer = roles["recovery_timer"].name
    try:
        runner(
            (
                "/usr/bin/systemctl",
                "disable",
                "--now",
                recovery_timer,
                recovery_unit,
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin"},
            shell=False,
            check=False,
            timeout=30,
        )
        for role in ("gateway_dropin", "recovery_timer", "recovery_unit"):
            try:
                roles[role].unlink()
                _fsync_directory(roles[role].parent)
            except FileNotFoundError:
                pass
        runner(
            ("/usr/bin/systemctl", "daemon-reload"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin"},
            shell=False,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReconcileError("OFFLINE_DEPLOY_CLEANUP_SYSTEMD_FAILED") from exc
    return commit


def _load_spec(path: Path) -> Mapping[str, Any]:
    raw = path.read_bytes()
    if len(raw) > MAX_JSON:
        _fail("OFFLINE_DEPLOY_PREPARE_SPEC_INVALID")
    return _decode(raw, "OFFLINE_DEPLOY_PREPARE_SPEC_INVALID")


def _emit(value: Mapping[str, Any]) -> None:
    sys.stdout.write(_canonical(value).decode("utf-8") + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile one sealed offline Muncho deploy transaction",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare-manifest")
    prepare.add_argument("--spec", type=Path, required=True)
    arm_parser = subparsers.add_parser("arm")
    arm_parser.add_argument("--manifest", type=Path, required=True)
    arm_parser.add_argument("--manifest-sha256", required=True)
    arm_parser.add_argument("--lock-fd", type=int)
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--manifest", type=Path, required=True)
    inspect_parser.add_argument("--manifest-sha256", required=True)
    reconcile_parser = subparsers.add_parser("reconcile")
    reconcile_parser.add_argument("--manifest", type=Path, required=True)
    reconcile_parser.add_argument("--manifest-sha256", required=True)
    reconcile_parser.add_argument("--lock-fd", type=int)
    quiesce_parser = subparsers.add_parser("quiesce")
    quiesce_parser.add_argument("--manifest", type=Path, required=True)
    quiesce_parser.add_argument("--manifest-sha256", required=True)
    quiesce_parser.add_argument("--lock-fd", type=int, required=True)
    activate_parser = subparsers.add_parser("activate-finalize")
    activate_parser.add_argument("--manifest", type=Path, required=True)
    activate_parser.add_argument("--manifest-sha256", required=True)
    activate_parser.add_argument("--lock-fd", type=int, required=True)
    authorize = subparsers.add_parser("authorize-start")
    authorize.add_argument("--manifest", type=Path, required=True)
    authorize.add_argument("--manifest-sha256", required=True)
    health = subparsers.add_parser("commit-health")
    health.add_argument("--manifest", type=Path, required=True)
    health.add_argument("--manifest-sha256", required=True)
    health.add_argument("--receipt", type=Path, required=True)
    cleanup_parser = subparsers.add_parser("cleanup")
    cleanup_parser.add_argument("--manifest", type=Path, required=True)
    cleanup_parser.add_argument("--manifest-sha256", required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "prepare-manifest":
            result = prepare_manifest(_load_spec(arguments.spec))
        elif arguments.command == "arm":
            result = arm(
                arguments.manifest,
                expected_manifest_sha256=arguments.manifest_sha256,
                inherited_lock_fd=arguments.lock_fd,
            )
        elif arguments.command == "inspect":
            result = inspect(
                arguments.manifest,
                expected_manifest_sha256=arguments.manifest_sha256,
            )
        elif arguments.command == "reconcile":
            result = reconcile(
                arguments.manifest,
                expected_manifest_sha256=arguments.manifest_sha256,
                inherited_lock_fd=arguments.lock_fd,
            )
        elif arguments.command == "quiesce":
            result = quiesce(
                arguments.manifest,
                expected_manifest_sha256=arguments.manifest_sha256,
                inherited_lock_fd=arguments.lock_fd,
            )
        elif arguments.command == "activate-finalize":
            result = activate_finalize(
                arguments.manifest,
                expected_manifest_sha256=arguments.manifest_sha256,
                inherited_lock_fd=arguments.lock_fd,
            )
        elif arguments.command == "authorize-start":
            return (
                0
                if authorize_start(
                    arguments.manifest,
                    expected_manifest_sha256=arguments.manifest_sha256,
                )
                else 1
            )
        elif arguments.command == "commit-health":
            result = commit_health(
                arguments.manifest,
                arguments.receipt,
                expected_manifest_sha256=arguments.manifest_sha256,
            )
        else:
            result = cleanup(
                arguments.manifest,
                expected_manifest_sha256=arguments.manifest_sha256,
            )
    except (OSError, ReconcileError, TypeError, ValueError):
        sys.stderr.write("OFFLINE_DEPLOY_RECONCILIATION_FAILED\n")
        return 2
    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
