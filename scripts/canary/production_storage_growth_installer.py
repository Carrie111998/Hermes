#!/usr/bin/env python3
"""Exact privileged installer and attestor for production storage growth.

There are only two install actions: create the fixed owner-side state root and
publish the fixed production guest entrypoint.  No path, command, target,
account, project, device, partition, filesystem, or size is caller-selected.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


OWNER_STATE_ROOT = Path("/var/lib/muncho-production-storage-growth")
OWNER_INSTALLATION_RECEIPT = OWNER_STATE_ROOT / ".installation.json"
OWNER_PUBLIC_READINESS = Path(
    "/var/lib/muncho-production-storage-growth.ready.json"
)
OWNER_STATE_HELPER = Path(
    "/usr/local/lib/muncho/production-storage-growth-state-helper"
)
OWNER_STATE_HELPER_SOURCE = Path(__file__).with_name(
    "production_storage_growth_state_helper.py"
)
OWNER_STATE_HELPER_SUDOERS = Path(
    "/etc/sudoers.d/muncho-production-storage-growth-state-helper"
)
GUEST_ENTRYPOINT = Path("/usr/local/lib/muncho/production-storage-growth-guest")
GUEST_INSTALLATION_ROOT = Path(
    "/var/lib/muncho-production-storage-growth-guest"
)
GUEST_INSTALLATION_RECEIPT = GUEST_INSTALLATION_ROOT / "installation.json"
GUEST_SUDOERS_PATH = Path("/etc/sudoers.d/muncho-production-storage-growth")
GUEST_SOURCE = Path(__file__).with_name("production_storage_growth_guest.py")
GUEST_INTERPRETER = Path("/usr/bin/python3")

OWNER_INSTALLATION_SCHEMA = (
    "muncho-production-storage-growth-owner-installation.v2"
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
    "state_root_device",
    "state_root_inode",
    "state_helper_path",
    "state_helper_sha256",
    "state_helper_sudoers_path",
    "state_helper_sudoers_sha256",
    "authorized_client_uid",
    "authorized_client_gid",
    "authority_key_attestation",
    "authority_key_attestation_sha256",
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
    "state_helper_sha256",
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
    "state_helper_sha256": (
        "scripts/canary/production_storage_growth_state_helper.py"
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


def _posix_effective_uid() -> int:
    getter = getattr(os, "geteuid", None)
    if not callable(getter):
        raise ProductionStorageInstallerError(
            "production_storage_owner_installer_invalid"
        ) from None
    try:
        value = getter()
    except (OSError, TypeError, ValueError):
        raise ProductionStorageInstallerError(
            "production_storage_owner_installer_invalid"
        ) from None
    if type(value) is not int or value < 0:
        raise ProductionStorageInstallerError(
            "production_storage_owner_installer_invalid"
        )
    return value


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


def decode_canonical_json(raw: bytes, *, maximum: int = 64 * 1024) -> Any:
    if (
        not isinstance(raw, bytes) or not raw
        or type(maximum) is not int or not 1 <= maximum <= 2 * 1024 * 1024
        or len(raw) > maximum
    ):
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


def _validate_authority_key_attestation(
    value: Mapping[str, Any],
    *,
    release_sha: str,
) -> Mapping[str, Any]:
    """Re-verify portable trust in the privileged installer process."""

    unsigned = {
        name: item for name, item in value.items() if name != "attestation_sha256"
    } if isinstance(value, Mapping) else {}
    if (
        not isinstance(value, Mapping)
        or set(value)
        != {
            "schema", "release_sha", "receipt_public_key_ed25519_hex",
            "receipt_public_key_id", "portable_trust_bundle_sha256",
            "portable_trust_bundle", "authority_manifest_sha256",
            "authority_host_receipt_sha256",
            "root_owned_trust_bundle_validated",
            "rotation_requires_new_release_and_owner_install",
            "attestation_sha256",
        }
        or value.get("schema")
        != "muncho-production-storage-authority-key-attestation.v1"
        or value.get("release_sha") != release_sha
        or value.get("attestation_sha256") != sha256_json(unsigned)
        or not isinstance(value.get("portable_trust_bundle"), Mapping)
    ):
        raise ProductionStorageInstallerError(
            "production_storage_authority_key_attestation_invalid"
        )
    try:
        if __package__ is None or __package__ == "":
            resolved = Path(__file__).resolve(strict=True)
            source_root = resolved.parents[2]
            support_root = source_root.parent
            site_root = support_root / "site-packages"
            standard = [
                item for item in sys.path
                if item and "site-packages" not in Path(item).parts
                and "dist-packages" not in Path(item).parts
            ]
            sys.path[:] = [str(source_root), str(site_root), *standard]
        from scripts.canary import production_cutover_passkey as cutover

        checked, receipt_key = cutover.validate_trust_bundle(
            value["portable_trust_bundle"]
        )
        raw = receipt_key.public_bytes_raw()
    except Exception:
        raise ProductionStorageInstallerError(
            "production_storage_authority_key_attestation_invalid"
        ) from None
    if (
        checked.get("authority_release_sha") != release_sha
        or checked.get("trust_bundle_sha256")
        != value.get("portable_trust_bundle_sha256")
        or raw.hex() != value.get("receipt_public_key_ed25519_hex")
        or hashlib.sha256(raw).hexdigest()
        != value.get("receipt_public_key_id")
        or value.get("root_owned_trust_bundle_validated") is not True
        or value.get("rotation_requires_new_release_and_owner_install")
        is not True
    ):
        raise ProductionStorageInstallerError(
            "production_storage_authority_key_attestation_invalid"
        )
    return dict(value)


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


def _install_validated_sudoers(
    path: Path,
    payload: bytes,
    *,
    uid: int,
    gid: int,
    runner: Callable[..., subprocess.CompletedProcess[bytes]],
) -> None:
    try:
        existing = path.read_bytes()
    except FileNotFoundError:
        existing = None
    except OSError:
        raise ProductionStorageInstallerError(
            "production_storage_state_helper_sudoers_invalid"
        ) from None
    if existing is not None:
        _validate_node(
            path,
            directory=False,
            mode=0o440,
            uid=uid,
            gid=gid,
        )
        if existing != payload:
            raise ProductionStorageInstallerError(
                "production_storage_state_helper_sudoers_conflict"
            )
    descriptor: int | None = None
    staged: str | None = None
    try:
        descriptor, staged = tempfile.mkstemp(
            prefix=".muncho-production-storage-growth-state-helper.",
            dir=path.parent,
        )
        os.fchmod(descriptor, 0o440)
        os.fchown(descriptor, uid, gid)
        os.write(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        checked = runner(
            ("/usr/sbin/visudo", "-cf", staged),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={
                "HOME": "/var/empty",
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            },
            timeout=30,
            check=False,
        )
        if checked.returncode != 0:
            raise OSError("visudo rejected fixed fragment")
    except (OSError, subprocess.SubprocessError):
        raise ProductionStorageInstallerError(
            "production_storage_state_helper_sudoers_invalid"
        ) from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if staged is not None:
            try:
                os.unlink(staged)
            except FileNotFoundError:
                pass
    if existing is None:
        _write_atomic(path, payload, mode=0o440, uid=uid, gid=gid)


def _snapshot_installed_file(
    path: Path,
    *,
    mode: int,
    uid: int,
    gid: int,
) -> bytes | None:
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise ProductionStorageInstallerError(
            "production_storage_installer_storage_invalid"
        ) from None
    _validate_node(
        path, directory=False, mode=mode, uid=uid, gid=gid
    )
    return payload


def _acquire_owner_execution_lock(
    state_root: Path,
    *,
    uid: int,
    gid: int,
) -> int:
    """Acquire the one validated lock shared by installer and root helper."""

    lock_path = state_root / ".execution.lock"
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    created = False
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | nofollow,
                0o600,
            )
            created = True
            os.fchmod(descriptor, 0o600)
            os.fchown(descriptor, uid, gid)
            os.fsync(descriptor)
            directory = os.open(state_root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except FileExistsError:
            descriptor = os.open(lock_path, os.O_RDWR | nofollow)
        info = os.fstat(descriptor)
        path_info = lock_path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != uid
            or info.st_gid != gid
            or info.st_nlink != 1
            or path_info.st_dev != info.st_dev
            or path_info.st_ino != info.st_ino
        ):
            raise OSError("invalid execution lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        path_info = lock_path.lstat()
        if path_info.st_dev != info.st_dev or path_info.st_ino != info.st_ino:
            raise OSError("execution lock identity changed")
        return descriptor
    except OSError:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if created:
            try:
                lock_path.unlink()
            except OSError:
                pass
        raise ProductionStorageInstallerError(
            "production_storage_owner_execution_lock_invalid"
        ) from None


def _release_owner_execution_lock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(descriptor)
    except OSError:
        pass


@dataclass(frozen=True)
class _ProcessIdentity:
    pid: int
    owner_uid: int
    start_time_ticks: int
    executable_device: int
    executable_inode: int
    argv: tuple[bytes, ...]


def _exact_predecessor_argv() -> frozenset[tuple[bytes, ...]]:
    helper = str(OWNER_STATE_HELPER).encode()
    return frozenset({
        (helper,),
        (b"/usr/bin/python3", helper),
        (
            b"/usr/bin/sudo", b"--non-interactive", b"--", helper,
        ),
        (b"sudo", b"--non-interactive", b"--", helper),
    })


def _read_process_identity(pid: int) -> _ProcessIdentity | None:
    if type(pid) is not int or pid <= 1:
        raise ProductionStorageInstallerError(
            "production_storage_predecessor_quiescence_invalid"
        )
    entry = Path("/proc") / str(pid)
    try:
        owner_uid = entry.stat().st_uid
        cmdline = (entry / "cmdline").read_bytes()
        raw_stat = (entry / "stat").read_bytes()
        executable = (entry / "exe").stat()
    except FileNotFoundError:
        return None
    except OSError:
        raise ProductionStorageInstallerError(
            "production_storage_predecessor_quiescence_invalid"
        ) from None
    try:
        stat_tail = raw_stat.rsplit(b") ", 1)[1].split()
        start_time_ticks = int(stat_tail[19])
    except (IndexError, ValueError):
        raise ProductionStorageInstallerError(
            "production_storage_predecessor_quiescence_invalid"
        ) from None
    argv = tuple(part for part in cmdline.split(b"\0") if part)
    if (
        len(cmdline) > 64 * 1024
        or len(raw_stat) > 64 * 1024
        or start_time_ticks <= 0
        or not stat.S_ISREG(executable.st_mode)
        or executable.st_ino <= 0
    ):
        raise ProductionStorageInstallerError(
            "production_storage_predecessor_quiescence_invalid"
        )
    return _ProcessIdentity(
        pid=pid,
        owner_uid=owner_uid,
        start_time_ticks=start_time_ticks,
        executable_device=executable.st_dev,
        executable_inode=executable.st_ino,
        argv=argv,
    )


def _validate_predecessor_identity(
    identity: _ProcessIdentity,
    *,
    authorized_client_uid: int,
) -> _ProcessIdentity:
    if (
        not isinstance(identity, _ProcessIdentity)
        or type(identity.pid) is not int
        or identity.pid <= 1
        or type(identity.owner_uid) is not int
        or identity.owner_uid < 0
        or type(identity.start_time_ticks) is not int
        or identity.start_time_ticks <= 0
        or type(identity.executable_device) is not int
        or identity.executable_device < 0
        or type(identity.executable_inode) is not int
        or identity.executable_inode <= 0
        or not isinstance(identity.argv, tuple)
        or any(not isinstance(part, bytes) or not part for part in identity.argv)
        or identity.pid == os.getpid()
        or identity.owner_uid not in {0, authorized_client_uid}
        or identity.argv not in _exact_predecessor_argv()
    ):
        raise ProductionStorageInstallerError(
            "production_storage_predecessor_quiescence_invalid"
        )
    return identity


def _list_predecessor_helper_processes(
    *,
    authorized_client_uid: int,
) -> tuple[_ProcessIdentity, ...]:
    """List only stable identities of exact fixed-helper invocations."""

    proc_root = Path("/proc")
    if not proc_root.is_dir():
        raise ProductionStorageInstallerError(
            "production_storage_predecessor_quiescence_invalid"
        )
    try:
        pids = tuple(
            int(entry.name)
            for entry in proc_root.iterdir()
            if entry.name.isascii() and entry.name.isdigit()
            and int(entry.name) > 1
            and int(entry.name) != os.getpid()
        )
    except OSError:
        raise ProductionStorageInstallerError(
            "production_storage_predecessor_quiescence_invalid"
        ) from None
    found: list[_ProcessIdentity] = []
    exact_argv = _exact_predecessor_argv()
    for pid in pids:
        identity = _read_process_identity(pid)
        if identity is None or identity.argv not in exact_argv:
            continue
        found.append(_validate_predecessor_identity(
            identity, authorized_client_uid=authorized_client_uid
        ))
    return tuple(sorted(found, key=lambda item: item.pid))


def _quiesce_predecessor_helpers(
    *,
    authorized_client_uid: int,
    process_lister: Callable[[], Sequence[_ProcessIdentity]] | None = None,
    identity_reader: Callable[[int], _ProcessIdentity | None] = (
        _read_process_identity
    ),
    pidfd_opener: Callable[[int, int], int] | None = None,
    pidfd_signaler: Callable[[int, int, Any, int], None] | None = None,
    fd_closer: Callable[[int], None] = os.close,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Drain helpers that cached predecessor authority before taking lock."""

    lister = process_lister or (
        lambda: _list_predecessor_helper_processes(
            authorized_client_uid=authorized_client_uid
        )
    )
    opener = pidfd_opener or getattr(os, "pidfd_open", None)
    signaler = pidfd_signaler or getattr(signal, "pidfd_send_signal", None)
    if not callable(opener) or not callable(signaler):
        raise ProductionStorageInstallerError(
            "production_storage_predecessor_pidfd_unavailable"
        )
    deadline = monotonic() + 10.0
    empty_observations = 0
    while monotonic() < deadline:
        try:
            identities = tuple(
                sorted(set(lister()), key=lambda item: item.pid)
            )
        except ProductionStorageInstallerError:
            raise
        except Exception:
            raise ProductionStorageInstallerError(
                "production_storage_predecessor_quiescence_invalid"
            ) from None
        identities = tuple(
            _validate_predecessor_identity(
                identity,
                authorized_client_uid=authorized_client_uid,
            )
            for identity in identities
        )
        if len({identity.pid for identity in identities}) != len(identities):
            raise ProductionStorageInstallerError(
                "production_storage_predecessor_quiescence_invalid"
            )
        if not identities:
            empty_observations += 1
            if empty_observations >= 2:
                return
            sleeper(0.05)
            continue
        empty_observations = 0
        handles: list[tuple[_ProcessIdentity, int]] = []
        batch_error: ProductionStorageInstallerError | None = None
        try:
            for identity in identities:
                try:
                    descriptor = opener(identity.pid, 0)
                except ProcessLookupError:
                    continue
                except OSError:
                    if batch_error is None:
                        batch_error = ProductionStorageInstallerError(
                            "production_storage_predecessor_pidfd_invalid"
                        )
                    continue
                retained = False
                try:
                    current = identity_reader(identity.pid)
                    if current is None or current != identity:
                        # The numeric PID disappeared or was reused after the
                        # pidfd was opened.  This descriptor is unconfirmed,
                        # so never signal it; a fresh discovery pass decides
                        # whether the replacement is itself an exact helper.
                        continue
                    _validate_predecessor_identity(
                        current,
                        authorized_client_uid=authorized_client_uid,
                    )
                    handles.append((identity, descriptor))
                    retained = True
                except ProcessLookupError:
                    continue
                except ProductionStorageInstallerError as error:
                    if batch_error is None:
                        batch_error = error
                except Exception:
                    if batch_error is None:
                        batch_error = ProductionStorageInstallerError(
                            "production_storage_predecessor_quiescence_invalid"
                        )
                finally:
                    if not retained:
                        try:
                            fd_closer(descriptor)
                        except OSError:
                            pass
            for _identity, descriptor in handles:
                try:
                    signaler(descriptor, signal.SIGTERM, None, 0)
                except ProcessLookupError:
                    pass
                except OSError:
                    if batch_error is None:
                        batch_error = ProductionStorageInstallerError(
                            "production_storage_predecessor_pidfd_invalid"
                        )
            sleeper(0.05)
            for _identity, descriptor in handles:
                try:
                    signaler(descriptor, signal.SIGKILL, None, 0)
                except ProcessLookupError:
                    pass
                except OSError:
                    if batch_error is None:
                        batch_error = ProductionStorageInstallerError(
                            "production_storage_predecessor_pidfd_invalid"
                        )
        finally:
            for _identity, descriptor in handles:
                try:
                    fd_closer(descriptor)
                except OSError:
                    pass
        if batch_error is not None:
            raise batch_error
        sleeper(0.05)
    raise ProductionStorageInstallerError(
        "production_storage_predecessor_quiescence_timeout"
    )


def _remove_new_fixed_file(
    path: Path,
    *,
    mode: int,
    uid: int,
    gid: int,
) -> None:
    _validate_node(
        path, directory=False, mode=mode, uid=uid, gid=gid
    )
    try:
        path.unlink()
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        raise ProductionStorageInstallerError(
            "production_storage_owner_install_rollback_failed"
        ) from None


def _rollback_owner_installation(
    snapshots: Sequence[tuple[Path, int, bytes | None]],
    *,
    sudoers_path: Path,
    quiesce_predecessors: Callable[[], None],
    uid: int,
    gid: int,
) -> None:
    sudoers_snapshot = tuple(
        item for item in snapshots if item[0] == sudoers_path
    )
    non_sudoers = tuple(
        item for item in snapshots if item[0] != sudoers_path
    )
    if len(sudoers_snapshot) != 1:
        raise ProductionStorageInstallerError(
            "production_storage_owner_install_rollback_failed"
        )
    try:
        if sudoers_path.exists():
            _remove_new_fixed_file(
                sudoers_path, mode=0o440, uid=uid, gid=gid
            )
        quiesce_predecessors()
        for path, mode, payload in (
            *reversed(non_sudoers),
            sudoers_snapshot[0],
        ):
            if payload is None:
                if path.exists():
                    _remove_new_fixed_file(
                        path, mode=mode, uid=uid, gid=gid
                    )
            else:
                _write_atomic(
                    path, payload, mode=mode, uid=uid, gid=gid
                )
    except ProductionStorageInstallerError:
        raise ProductionStorageInstallerError(
            "production_storage_owner_install_rollback_failed"
        ) from None


def _publish_owner_installation_transaction(
    *,
    state_helper_path: Path,
    state_helper_payload: bytes,
    sudoers_path: Path,
    sudoers_payload: bytes,
    receipt_path: Path,
    receipt_payload: bytes,
    public_readiness_path: Path,
    build_public_readiness: Callable[[], Mapping[str, Any]],
    attest_public_readiness: Callable[[], Mapping[str, Any]],
    quiesce_predecessors: Callable[[], None],
    uid: int,
    gid: int,
    sudoers_validator: Callable[..., subprocess.CompletedProcess[bytes]],
) -> Mapping[str, Any]:
    """Publish the four fixed artifacts as one rollback-safe transaction."""

    specs = (
        (state_helper_path, 0o555),
        (sudoers_path, 0o440),
        (receipt_path, 0o600),
        (public_readiness_path, 0o444),
    )
    snapshots = tuple(
        (
            path,
            mode,
            _snapshot_installed_file(
                path, mode=mode, uid=uid, gid=gid
            ),
        )
        for path, mode in specs
    )
    try:
        if sudoers_path.exists():
            _remove_new_fixed_file(
                sudoers_path, mode=0o440, uid=uid, gid=gid
            )
        quiesce_predecessors()
        _write_atomic(
            state_helper_path,
            state_helper_payload,
            mode=0o555,
            uid=uid,
            gid=gid,
        )
        _write_atomic(
            receipt_path,
            receipt_payload,
            mode=0o600,
            uid=uid,
            gid=gid,
        )
        public = dict(build_public_readiness())
        _write_atomic(
            public_readiness_path,
            canonical_json_bytes(public) + b"\n",
            mode=0o444,
            uid=uid,
            gid=gid,
        )
        attested = dict(attest_public_readiness())
        if attested != public:
            raise ProductionStorageInstallerError(
                "production_storage_owner_readiness_invalid"
            )
        _install_validated_sudoers(
            sudoers_path,
            sudoers_payload,
            uid=uid,
            gid=gid,
            runner=sudoers_validator,
        )
        return public
    except Exception as error:
        _rollback_owner_installation(
            snapshots,
            sudoers_path=sudoers_path,
            quiesce_predecessors=quiesce_predecessors,
            uid=uid,
            gid=gid,
        )
        if isinstance(error, ProductionStorageInstallerError):
            raise
        raise ProductionStorageInstallerError(
            "production_storage_owner_install_failed"
        ) from None


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
            "state_helper_sha256": artifacts["state_helper"]["sha256"],
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
    authority_key_attestation: Mapping[str, Any] | None = None,
    state_root: Path = OWNER_STATE_ROOT,
    installation_receipt: Path | None = None,
    expected_uid: int = 0,
    expected_gid: int = 0,
    effective_uid: Callable[[], int] = _posix_effective_uid,
    wall_clock: Callable[[], int] = lambda: int(time.time()),
    artifact_verifier: Callable[..., Mapping[str, Any]] = (
        verify_owner_artifact_binding_on_disk
    ),
    state_helper_source: Path = OWNER_STATE_HELPER_SOURCE,
    state_helper_path: Path = OWNER_STATE_HELPER,
    public_readiness_path: Path = OWNER_PUBLIC_READINESS,
    sudoers_path: Path = OWNER_STATE_HELPER_SUDOERS,
    authorized_client_uid: int | None = None,
    authorized_client_gid: int | None = None,
    sudoers_validator: Callable[..., subprocess.CompletedProcess[bytes]] = (
        subprocess.run
    ),
) -> Mapping[str, Any]:
    """Install only the exact durable local root and its immutable identity."""

    receipt_path = installation_receipt or state_root / ".installation.json"
    binding = validate_owner_artifact_binding(
        sealed_artifact_binding,
        release_sha=release_sha,
    )
    try:
        import pwd

        if authorized_client_uid is None:
            client_uid = (
                int(os.environ["SUDO_UID"])
                if expected_uid == 0
                else os.getuid()
            )
        else:
            client_uid = authorized_client_uid
        if authorized_client_gid is None:
            client_gid = (
                int(os.environ["SUDO_GID"])
                if expected_uid == 0
                else os.getgid()
            )
        else:
            client_gid = authorized_client_gid
        client_name = pwd.getpwuid(client_uid).pw_name
    except (ImportError, KeyError, ValueError, TypeError, OverflowError):
        raise ProductionStorageInstallerError(
            "production_storage_owner_installer_invalid"
        ) from None
    production_paths = state_root == OWNER_STATE_ROOT
    if (
        _SHA40.fullmatch(release_sha or "") is None
        or production_paths
        and receipt_path != OWNER_INSTALLATION_RECEIPT
        or production_paths
        and (
            state_helper_source != OWNER_STATE_HELPER_SOURCE
            or state_helper_path != OWNER_STATE_HELPER
            or public_readiness_path != OWNER_PUBLIC_READINESS
            or sudoers_path != OWNER_STATE_HELPER_SUDOERS
        )
        or receipt_path.parent != state_root
        or effective_uid() != 0
        or type(expected_uid) is not int
        or type(expected_gid) is not int
        or not callable(artifact_verifier)
        or type(client_uid) is not int
        or client_uid <= 0
        or type(client_gid) is not int
        or client_gid < 0
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", client_name) is None
    ):
        raise ProductionStorageInstallerError(
            "production_storage_owner_installer_invalid"
        )
    binding = artifact_verifier(binding, release_sha=release_sha)
    production_install = expected_uid == 0
    if production_install:
        if authority_key_attestation is None:
            raise ProductionStorageInstallerError(
                "production_storage_authority_key_attestation_invalid"
            )
        authority_key_attestation = _validate_authority_key_attestation(
            authority_key_attestation,
            release_sha=release_sha,
        )
    else:
        authority_key_attestation = {
            "attestation_sha256": "0" * 64,
        }
    _ensure_directory(
        state_root,
        mode=0o700,
        uid=expected_uid,
        gid=expected_gid,
    )
    execution_lock = _acquire_owner_execution_lock(
        state_root, uid=expected_uid, gid=expected_gid
    )
    try:
        try:
            helper_payload = state_helper_source.read_bytes()
        except OSError:
            raise ProductionStorageInstallerError(
                "production_storage_owner_artifact_binding_invalid"
            ) from None
        observed_helper_sha256 = hashlib.sha256(helper_payload).hexdigest()
        if (
            production_install
            and observed_helper_sha256 != binding["state_helper_sha256"]
        ):
            raise ProductionStorageInstallerError(
                "production_storage_owner_artifact_binding_invalid"
            )
        helper_sha256 = (
            observed_helper_sha256
            if production_install else binding["state_helper_sha256"]
        )
        sudoers_payload = (
            f"{client_name} ALL=(root) NOPASSWD: {state_helper_path}\n"
        ).encode("ascii", errors="strict")
        if production_install:
            _ensure_directory(
                state_helper_path.parent,
                mode=0o755,
                uid=expected_uid,
                gid=expected_gid,
            )
        root = _validate_node(
            state_root,
            directory=True,
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
            "state_root_device": root.st_dev,
            "state_root_inode": root.st_ino,
            "state_helper_path": str(OWNER_STATE_HELPER),
            "state_helper_sha256": helper_sha256,
            "state_helper_sudoers_path": str(OWNER_STATE_HELPER_SUDOERS),
            "state_helper_sudoers_sha256": hashlib.sha256(
                sudoers_payload
            ).hexdigest(),
            "authorized_client_uid": client_uid,
            "authorized_client_gid": client_gid,
            "authority_key_attestation": dict(authority_key_attestation),
            "authority_key_attestation_sha256": authority_key_attestation[
                "attestation_sha256"
            ],
        }
        receipt = {
            **unsigned,
            "installation_receipt_sha256": sha256_json(unsigned),
        }
        receipt_payload = canonical_json_bytes(receipt) + b"\n"
        if not production_install:
            _write_atomic(
                receipt_path,
                receipt_payload,
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

        def build_public_readiness() -> Mapping[str, Any]:
            private_readiness = attest_owner_state_root(
                release_sha,
                state_root=state_root,
                installation_receipt=receipt_path,
                sealed_artifact_binding=binding,
                expected_uid=expected_uid,
                expected_gid=expected_gid,
            )
            public_unsigned = {
                **private_readiness,
                "schema": (
                    "muncho-production-storage-growth-"
                    "owner-state-public-ready.v1"
                ),
                "private_installation_receipt_sha256": receipt[
                    "installation_receipt_sha256"
                ],
                "state_root_device": root.st_dev,
                "state_root_inode": root.st_ino,
                "state_helper_path": str(OWNER_STATE_HELPER),
                "state_helper_sha256": helper_sha256,
                "authorized_client_uid": client_uid,
                "authorized_client_gid": client_gid,
                "authority_key_attestation_sha256": (
                    authority_key_attestation["attestation_sha256"]
                ),
                "receipt_public_key_id": authority_key_attestation[
                    "receipt_public_key_id"
                ],
            }
            return {
                **public_unsigned,
                "public_readiness_sha256": sha256_json(public_unsigned),
            }

        return _publish_owner_installation_transaction(
            state_helper_path=state_helper_path,
            state_helper_payload=helper_payload,
            sudoers_path=sudoers_path,
            sudoers_payload=sudoers_payload,
            receipt_path=receipt_path,
            receipt_payload=receipt_payload,
            public_readiness_path=public_readiness_path,
            build_public_readiness=build_public_readiness,
            attest_public_readiness=lambda: attest_owner_state_public(
                release_sha,
                sealed_artifact_binding=binding,
                state_root=state_root,
                public_readiness_path=public_readiness_path,
                expected_uid=expected_uid,
                expected_gid=expected_gid,
            ),
            quiesce_predecessors=lambda: _quiesce_predecessor_helpers(
                authorized_client_uid=client_uid
            ),
            uid=expected_uid,
            gid=expected_gid,
            sudoers_validator=sudoers_validator,
        )
    finally:
        _release_owner_execution_lock(execution_lock)


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
        or receipt.get("state_root_device") != root.st_dev
        or receipt.get("state_root_inode") != root.st_ino
        or receipt.get("state_helper_path") != str(OWNER_STATE_HELPER)
        or _SHA256.fullmatch(
            str(receipt.get("state_helper_sha256") or "")
        ) is None
        or receipt.get("state_helper_sha256")
        != binding["state_helper_sha256"]
        or receipt.get("state_helper_sudoers_path")
        != str(OWNER_STATE_HELPER_SUDOERS)
        or _SHA256.fullmatch(
            str(receipt.get("state_helper_sudoers_sha256") or "")
        ) is None
        or type(receipt.get("authorized_client_uid")) is not int
        or receipt["authorized_client_uid"] <= 0
        or type(receipt.get("authorized_client_gid")) is not int
        or receipt["authorized_client_gid"] < 0
        or not isinstance(receipt.get("authority_key_attestation"), Mapping)
        or receipt.get("authority_key_attestation_sha256")
        != receipt["authority_key_attestation"].get("attestation_sha256")
        or _SHA256.fullmatch(
            str(receipt.get("authority_key_attestation_sha256") or "")
        ) is None
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
        "state_root_device": root.st_dev,
        "state_root_inode": root.st_ino,
        "state_helper_path": receipt["state_helper_path"],
        "state_helper_sha256": receipt["state_helper_sha256"],
        "authorized_client_uid": receipt["authorized_client_uid"],
        "authorized_client_gid": receipt["authorized_client_gid"],
        "ready": True,
    }
    return {**unsigned, "readiness_sha256": sha256_json(unsigned)}


def attest_owner_state_public(
    release_sha: str,
    *,
    sealed_artifact_binding: Mapping[str, Any],
    state_root: Path = OWNER_STATE_ROOT,
    public_readiness_path: Path = OWNER_PUBLIC_READINESS,
    expected_uid: int = 0,
    expected_gid: int = 0,
) -> Mapping[str, Any]:
    """Attest root-owned readiness without traversing the 0700 private root."""

    binding = validate_owner_artifact_binding(
        sealed_artifact_binding,
        release_sha=release_sha,
    )
    root = _validate_node(
        state_root,
        directory=True,
        mode=0o700,
        uid=expected_uid,
        gid=expected_gid,
    )
    _validate_node(
        public_readiness_path,
        directory=False,
        mode=0o444,
        uid=expected_uid,
        gid=expected_gid,
    )
    value = _read_receipt(public_readiness_path)
    unsigned = {
        name: item
        for name, item in value.items()
        if name != "public_readiness_sha256"
    } if isinstance(value, Mapping) else {}
    if (
        not isinstance(value, Mapping)
        or value.get("schema")
        != "muncho-production-storage-growth-owner-state-public-ready.v1"
        or value.get("release_sha") != release_sha
        or value.get("state_root") != str(OWNER_STATE_ROOT)
        or value.get("state_root_uid") != expected_uid
        or value.get("state_root_gid") != expected_gid
        or value.get("state_root_mode") != "0700"
        or value.get("state_root_device") != root.st_dev
        or value.get("state_root_inode") != root.st_ino
        or value.get("sealed_artifact_binding_sha256")
        != binding["binding_sha256"]
        or value.get("owner_support_manifest_sha256")
        != binding["owner_support_manifest_sha256"]
        or value.get("state_helper_path") != str(OWNER_STATE_HELPER)
        or value.get("state_helper_sha256")
        != binding["state_helper_sha256"]
        or not _SHA256.fullmatch(
            str(value.get("private_installation_receipt_sha256") or "")
        )
        or type(value.get("authorized_client_uid")) is not int
        or value["authorized_client_uid"] <= 0
        or type(value.get("authorized_client_gid")) is not int
        or value["authorized_client_gid"] < 0
        or value.get("ready") is not True
        or value.get("public_readiness_sha256") != sha256_json(unsigned)
    ):
        raise ProductionStorageInstallerError(
            "production_storage_owner_readiness_invalid"
        )
    return dict(value)


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
    effective_uid: Callable[[], int] = _posix_effective_uid,
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
            raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
            request = decode_canonical_json(
                raw, maximum=2 * 1024 * 1024
            )
            if (
                not isinstance(request, Mapping)
                or set(request)
                != {"sealed_artifact_binding", "authority_key_attestation"}
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
                authority_key_attestation=request[
                    "authority_key_attestation"
                ],
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
    "OWNER_PUBLIC_READINESS",
    "OWNER_STATE_HELPER",
    "OWNER_STATE_HELPER_SOURCE",
    "OWNER_STATE_HELPER_SUDOERS",
    "OWNER_ARTIFACT_BINDING_SCHEMA",
    "OWNER_READINESS_SCHEMA",
    "OWNER_STATE_ROOT",
    "ProductionStorageInstallerError",
    "attest_owner_state_root",
    "attest_owner_state_public",
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
