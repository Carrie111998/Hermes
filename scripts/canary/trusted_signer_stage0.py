#!/usr/bin/env python3
"""Standard-library host bootstrap for the trusted observation runtime.

The only purpose of this stage-zero boundary is to verify the signed offline
owner-gate bundle, construct an isolated no-network Python runtime, publish it
under an immutable release path, and install one release-pinned sudoers policy.
It never receives private key material and has no service, IAM, activation,
network-fetch, or generic shell operation.
"""

from __future__ import annotations

import argparse
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
from contextlib import contextmanager
from typing import Any, Callable, Mapping, Sequence

from scripts.canary import owner_gate_stage0 as stage0


HOST_RELEASE_BASE = stage0.HOST_TRUSTED_OBSERVATION_RELEASE_BASE
HOST_CURRENT_LINK = Path("/opt/muncho-trusted-observation/current")
HOST_ACTIVATION_SEAL = Path("/etc/muncho/trusted-observation/enabled")
HOST_SUDOERS_PATH = Path("/etc/sudoers.d/muncho-host-observation-attestor")
HOST_STAGE0_LOCK = Path("/run/lock/muncho-host-trusted-runtime.lock")
HOST_SUDOERS_TEMPLATE = (
    "ops/muncho/owner-gate/muncho-host-observation-attestor.sudoers.in"
)
HOST_RUNTIME_RECEIPT_SCHEMA = "muncho-host-offline-trusted-runtime.v1"
HOST_RUNTIME_RECEIPT_FIELDS = frozenset({
    "schema",
    "release_revision",
    "package_sha256",
    "preflight_sha256",
    "release",
    "sudoers",
    "runtime_inventory_sha256",
    "runtime_interpreter",
    "host_attestor_entrypoint",
    "host_provisioner_entrypoint",
    "offline_runtime",
    "network_install_required",
    "generic_usr_bin_python3_runtime",
    "current_link_absent",
    "activation_seal_absent",
    "service_start_performed",
    "service_enablement_mutated",
    "iam_mutation_performed",
    "cloud_mutation_performed",
    "private_key_material_received",
    "private_key_digest_recorded",
    "receipt_sha256",
})
MAX_FILE_BYTES = 128 * 1024 * 1024
_RELEASE_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SUDOERS_RELEASE_PATH = re.compile(
    rb"/opt/muncho-trusted-observation/releases/([0-9a-f]{40})/"
)
_SUDOERS_STAGING_RELEASE_PATH = re.compile(
    rb"/opt/muncho-trusted-observation/releases/"
    rb"\.([0-9a-f]{40})\.bootstrap/"
)


class TrustedSignerStage0Error(RuntimeError):
    """Stable, secret-free host runtime bootstrap failure."""


def _error(code: str, exc: BaseException | None = None) -> None:
    del exc
    raise TrustedSignerStage0Error(code) from None


def _is_sha256(value: Any) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _fsync_directory(path: Path) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0),
        )
        os.fsync(descriptor)
    except OSError as exc:
        _error("trusted_signer_stage0_sync_failed", exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _lock_parent_is_trusted(state: os.stat_result) -> bool:
    """Accept the exact Debian 12 /run/lock mode without widening the lock."""

    return (
        not stat.S_ISLNK(state.st_mode)
        and stat.S_ISDIR(state.st_mode)
        and state.st_uid == 0
        and state.st_gid == 0
        and stat.S_IMODE(state.st_mode) in {0o755, 0o775, 0o1777}
    )


@contextmanager
def _stage0_lock(path: Path = HOST_STAGE0_LOCK) -> Any:
    descriptor: int | None = None
    try:
        common = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        common |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, common | os.O_CREAT | os.O_EXCL, 0o600)
            os.fchmod(descriptor, 0o600)
            os.fchown(descriptor, 0, 0)
            os.fsync(descriptor)
            _fsync_directory(path.parent)
        except FileExistsError:
            descriptor = os.open(path, common)
        state = os.fstat(descriptor)
        parent = path.parent.lstat()
        if (
            not _lock_parent_is_trusted(parent)
            or not stat.S_ISREG(state.st_mode)
            or state.st_nlink != 1
            or state.st_uid != 0
            or state.st_gid != 0
            or stat.S_IMODE(state.st_mode) != 0o600
        ):
            _error("trusted_signer_stage0_lock_invalid")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except TrustedSignerStage0Error:
        raise
    except OSError as exc:
        _error("trusted_signer_stage0_lock_failed", exc)
    finally:
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Linux no-clobber directory publication (Debian 12 production)."""

    if os.path.lexists(destination):
        _error("trusted_signer_stage0_release_conflict")
    if sys.platform != "linux":
        # Tests may run on macOS, but the production contract is pinned Debian.
        # The cross-process lock and root-only parent still prevent a legitimate
        # concurrent publisher; retain a final lexists check before rename.
        if os.path.lexists(destination):
            _error("trusted_signer_stage0_release_conflict")
        os.rename(source, destination)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        _error("trusted_signer_stage0_noreplace_unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(destination),
        1,
    )
    if result != 0:
        failure = ctypes.get_errno()
        if failure == errno.EEXIST:
            _error("trusted_signer_stage0_release_conflict")
        _error("trusted_signer_stage0_publish_failed", OSError(failure, os.strerror(failure)))


def _exact_directory(path: Path, *, mode: int) -> Mapping[str, Any]:
    if not path.is_absolute() or ".." in path.parts:
        _error("trusted_signer_stage0_directory_invalid")
    try:
        if not path.exists() and not path.is_symlink():
            parent = path.parent.lstat()
            if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
                _error("trusted_signer_stage0_directory_invalid")
            path.mkdir(mode=mode)
            os.chown(path, 0, 0)
            os.chmod(path, mode)
            _fsync_directory(path.parent)
        state = path.lstat()
    except TrustedSignerStage0Error:
        raise
    except OSError as exc:
        _error("trusted_signer_stage0_directory_invalid", exc)
    if (
        stat.S_ISLNK(state.st_mode)
        or not stat.S_ISDIR(state.st_mode)
        or state.st_uid != 0
        or state.st_gid != 0
        or stat.S_IMODE(state.st_mode) != mode
    ):
        _error("trusted_signer_stage0_directory_invalid")
    return {"path": str(path), "uid": 0, "gid": 0, "mode": f"{mode:04o}"}


def _render_sudoers(
    source_release: Path,
    *,
    command_release: Path | None = None,
) -> bytes:
    selected_release = command_release or source_release
    if (
        source_release.parent != HOST_RELEASE_BASE
        or selected_release.parent != HOST_RELEASE_BASE
    ):
        _error("trusted_signer_stage0_sudoers_invalid")
    source = source_release / HOST_SUDOERS_TEMPLATE
    raw = stage0._read_regular(
        source,
        maximum=64 * 1024,
        expected_uid=0,
        allowed_modes=frozenset({0o444}),
    )
    placeholder = b"@RELEASE_SHA@"
    if raw.count(placeholder) < 1:
        _error("trusted_signer_stage0_sudoers_invalid")
    rendered = raw.replace(
        placeholder,
        selected_release.name.encode("ascii"),
    )
    if b"@" in rendered or b"/usr/bin/python3" in rendered or b"\r" in rendered:
        _error("trusted_signer_stage0_sudoers_invalid")
    expected_prefix = str(selected_release / "venv/bin/python").encode(
        "ascii"
    )
    if expected_prefix not in rendered:
        _error("trusted_signer_stage0_sudoers_invalid")
    return rendered


def _default_runner(argv: Sequence[str]) -> bytes:
    try:
        completed = subprocess.run(
            tuple(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C.UTF-8"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _error("trusted_signer_stage0_command_failed", exc)
    if completed.returncode != 0:
        _error("trusted_signer_stage0_command_failed")
    return completed.stdout


def _sudoers_snapshot(path: Path) -> tuple[bytes, tuple[int, ...]]:
    try:
        before = path.lstat()
        raw = stage0._read_regular(
            path,
            maximum=64 * 1024,
            expected_uid=0,
            expected_gid=0,
            allowed_modes=frozenset({0o440}),
        )
        after = path.lstat()
    except (OSError, stage0.OwnerGateStage0Error) as exc:
        _error("trusted_signer_stage0_sudoers_conflict", exc)
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_nlink,
        before.st_uid,
        before.st_gid,
        stat.S_IMODE(before.st_mode),
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_nlink,
        after.st_uid,
        after.st_gid,
        stat.S_IMODE(after.st_mode),
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if before_identity != after_identity:
        _error("trusted_signer_stage0_sudoers_conflict")
    return raw, after_identity


def _validate_predecessor_sudoers(
    raw: bytes,
    *,
    successor_release: Path,
) -> Path:
    revisions = set(_SUDOERS_RELEASE_PATH.findall(raw))
    staging_revisions = set(_SUDOERS_STAGING_RELEASE_PATH.findall(raw))
    if (
        successor_release.parent != HOST_RELEASE_BASE
        or _RELEASE_REVISION.fullmatch(successor_release.name) is None
        or (len(revisions), len(staging_revisions)) not in {(1, 0), (0, 1)}
    ):
        _error("trusted_signer_stage0_sudoers_conflict")
    try:
        predecessor_revision = next(
            iter(revisions or staging_revisions)
        ).decode(
            "ascii", errors="strict"
        )
    except UnicodeError:
        _error("trusted_signer_stage0_sudoers_conflict")
    predecessor = HOST_RELEASE_BASE / predecessor_revision
    try:
        state = predecessor.lstat()
    except OSError as exc:
        _error("trusted_signer_stage0_sudoers_conflict", exc)
    broken_staging = (
        HOST_RELEASE_BASE / f".{predecessor_revision}.bootstrap"
        if staging_revisions
        else None
    )
    if broken_staging is not None and os.path.lexists(broken_staging):
        _error("trusted_signer_stage0_sudoers_conflict")
    try:
        expected_payload = _render_sudoers(
            predecessor,
            command_release=broken_staging,
        )
    except TrustedSignerStage0Error as exc:
        _error("trusted_signer_stage0_sudoers_conflict", exc)
    if (
        predecessor == successor_release
        or stat.S_ISLNK(state.st_mode)
        or not stat.S_ISDIR(state.st_mode)
        or state.st_uid != 0
        or state.st_gid != 0
        or stat.S_IMODE(state.st_mode) != 0o555
        or expected_payload != raw
    ):
        _error("trusted_signer_stage0_sudoers_conflict")
    for relative in (
        "venv/bin/python",
        "bin/muncho-host-trusted-signer-provision",
        "bin/muncho-host-observation-attestor",
    ):
        stage0._read_regular(
            predecessor / relative,
            maximum=MAX_FILE_BYTES,
            expected_uid=0,
            expected_gid=0,
            allowed_modes=frozenset({0o555}),
        )
    return predecessor


def _replace_predecessor_sudoers(
    payload: bytes,
    *,
    successor_release: Path,
    destination: Path,
    temporary: Path,
    runner: Callable[[Sequence[str]], bytes],
    after_open: Callable[[], None] | None,
) -> None:
    predecessor_raw, predecessor_identity = _sudoers_snapshot(destination)
    _validate_predecessor_sudoers(
        predecessor_raw,
        successor_release=successor_release,
    )
    if (
        HOST_CURRENT_LINK.exists()
        or HOST_CURRENT_LINK.is_symlink()
        or HOST_ACTIVATION_SEAL.exists()
        or HOST_ACTIVATION_SEAL.is_symlink()
    ):
        _error("trusted_signer_stage0_sudoers_conflict")

    descriptor: int | None = None
    try:
        if temporary.exists() or temporary.is_symlink():
            staged_state = temporary.lstat()
            if (
                stat.S_ISREG(staged_state.st_mode)
                and staged_state.st_nlink == 1
                and staged_state.st_size == 0
                and staged_state.st_uid == 0
                and staged_state.st_gid == 0
                and not (stat.S_IMODE(staged_state.st_mode) & ~0o440)
            ):
                temporary.unlink()
                _fsync_directory(destination.parent)
            else:
                staged_raw = stage0._read_regular(
                    temporary,
                    maximum=64 * 1024,
                    expected_uid=0,
                    expected_gid=0,
                    allowed_modes=frozenset({0o440}),
                )
                staged_state = temporary.lstat()
                same_as_predecessor = (
                    staged_raw == predecessor_raw
                    and staged_state.st_nlink == 2
                    and (staged_state.st_dev, staged_state.st_ino)
                    == (
                        predecessor_identity[0],
                        predecessor_identity[1],
                    )
                )
                if same_as_predecessor:
                    temporary.unlink()
                    _fsync_directory(destination.parent)
                elif (
                    staged_state.st_nlink == 1
                    and staged_state.st_uid == 0
                    and staged_state.st_gid == 0
                    and stat.S_IMODE(staged_state.st_mode) == 0o440
                    and len(staged_raw) < len(payload)
                    and payload.startswith(staged_raw)
                ):
                    temporary.unlink()
                    _fsync_directory(destination.parent)
                elif staged_raw != payload or staged_state.st_nlink != 1:
                    _error("trusted_signer_stage0_sudoers_conflict")
        if not temporary.exists():
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temporary, flags, 0o440)
            if after_open is not None:
                after_open()
            os.fchmod(descriptor, 0o440)
            os.fchown(descriptor, 0, 0)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view[: min(len(view), 64)])
                if written <= 0:
                    raise OSError
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            _fsync_directory(destination.parent)
        staged_raw, _staged_identity = _sudoers_snapshot(temporary)
        if staged_raw != payload:
            _error("trusted_signer_stage0_sudoers_conflict")
        runner(("/usr/sbin/visudo", "-cf", str(temporary)))
        repeated_raw, repeated_identity = _sudoers_snapshot(destination)
        if (
            repeated_raw != predecessor_raw
            or repeated_identity != predecessor_identity
            or HOST_CURRENT_LINK.exists()
            or HOST_CURRENT_LINK.is_symlink()
            or HOST_ACTIVATION_SEAL.exists()
            or HOST_ACTIVATION_SEAL.is_symlink()
        ):
            _error("trusted_signer_stage0_sudoers_conflict")
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except TrustedSignerStage0Error:
        raise
    except (OSError, stage0.OwnerGateStage0Error) as exc:
        _error("trusted_signer_stage0_sudoers_install_failed", exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _install_sudoers(
    payload: bytes,
    *,
    successor_release: Path,
    destination: Path = HOST_SUDOERS_PATH,
    runner: Callable[[Sequence[str]], bytes] = _default_runner,
    after_open: Callable[[], None] | None = None,
) -> Mapping[str, Any]:
    temporary = destination.parent / f".{destination.name}.stage0-staged"
    descriptor: int | None = None
    try:
        if destination.exists() or destination.is_symlink():
            existing, _existing_identity = _sudoers_snapshot(destination)
            if existing != payload:
                _replace_predecessor_sudoers(
                    payload,
                    successor_release=successor_release,
                    destination=destination,
                    temporary=temporary,
                    runner=runner,
                    after_open=after_open,
                )
            if temporary.exists() or temporary.is_symlink():
                final_state = destination.lstat()
                staged_state = temporary.lstat()
                incomplete_open = (
                    stat.S_ISREG(staged_state.st_mode)
                    and staged_state.st_nlink == 1
                    and staged_state.st_size == 0
                    and staged_state.st_uid == 0
                    and staged_state.st_gid == 0
                    and not (stat.S_IMODE(staged_state.st_mode) & ~0o440)
                )
                if incomplete_open:
                    temporary.unlink()
                    _fsync_directory(destination.parent)
                    staged_state = None
                if (
                    stat.S_ISLNK(final_state.st_mode)
                    or (
                        staged_state is not None
                        and stat.S_ISLNK(staged_state.st_mode)
                    )
                    or not stat.S_ISREG(final_state.st_mode)
                    or (
                        staged_state is not None
                        and (
                            not stat.S_ISREG(staged_state.st_mode)
                            or staged_state.st_uid != 0
                            or staged_state.st_gid != 0
                            or stat.S_IMODE(staged_state.st_mode) != 0o440
                            or staged_state.st_nlink not in {1, 2}
                        )
                    )
                    or (
                        staged_state is not None
                        and staged_state.st_nlink == 2
                        and (staged_state.st_dev, staged_state.st_ino)
                        != (final_state.st_dev, final_state.st_ino)
                    )
                ):
                    _error("trusted_signer_stage0_sudoers_conflict")
                if staged_state is not None and staged_state.st_nlink == 2:
                    temporary.unlink()
                    _fsync_directory(destination.parent)
            raw = stage0._read_regular(
                destination,
                maximum=64 * 1024,
                expected_uid=0,
                allowed_modes=frozenset({0o440}),
            )
            state = destination.lstat()
            if raw != payload or state.st_gid != 0:
                _error("trusted_signer_stage0_sudoers_conflict")
            _fsync_directory(destination.parent)
            if temporary.exists() or temporary.is_symlink():
                staged_state = temporary.lstat()
                if (
                    stat.S_ISLNK(staged_state.st_mode)
                    or not stat.S_ISREG(staged_state.st_mode)
                    or staged_state.st_nlink not in {1, 2}
                    or staged_state.st_uid != 0
                    or staged_state.st_gid != 0
                    or stat.S_IMODE(staged_state.st_mode) != 0o440
                ):
                    _error("trusted_signer_stage0_sudoers_conflict")
                temporary.unlink()
                _fsync_directory(destination.parent)
        else:
            if temporary.exists() or temporary.is_symlink():
                try:
                    staged = stage0._read_regular(
                        temporary,
                        maximum=64 * 1024,
                        expected_uid=0,
                        allowed_modes=frozenset({0o440}),
                    )
                    staged_state = temporary.lstat()
                except stage0.OwnerGateStage0Error as exc:
                    staged_state = temporary.lstat()
                    if (
                        stat.S_ISLNK(staged_state.st_mode)
                        or not stat.S_ISREG(staged_state.st_mode)
                        or staged_state.st_nlink != 1
                        or staged_state.st_size != 0
                        or staged_state.st_uid != 0
                        or staged_state.st_gid != 0
                        or stat.S_IMODE(staged_state.st_mode) & ~0o440
                    ):
                        _error("trusted_signer_stage0_sudoers_conflict", exc)
                    temporary.unlink()
                    _fsync_directory(destination.parent)
                    staged = None
                if staged is None:
                    pass
                elif staged != payload:
                    if (
                        not stat.S_ISREG(staged_state.st_mode)
                        or staged_state.st_nlink != 1
                        or staged_state.st_uid != 0
                        or staged_state.st_gid != 0
                        or stat.S_IMODE(staged_state.st_mode) != 0o440
                        or len(staged) >= len(payload)
                        or not payload.startswith(staged)
                    ):
                        _error("trusted_signer_stage0_sudoers_conflict")
                    temporary.unlink()
                    _fsync_directory(destination.parent)
            if not temporary.exists():
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(temporary, flags, 0o440)
                if after_open is not None:
                    after_open()
                os.fchmod(descriptor, 0o440)
                os.fchown(descriptor, 0, 0)
                view = memoryview(payload)
                while view:
                    chunk = view[: min(len(view), 64)]
                    written = os.write(descriptor, chunk)
                    if written <= 0:
                        raise OSError
                    view = view[written:]
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = None
                _fsync_directory(destination.parent)
            runner(("/usr/sbin/visudo", "-cf", str(temporary)))
            try:
                os.link(temporary, destination, follow_symlinks=False)
            except FileExistsError:
                pass
            _fsync_directory(destination.parent)
            if temporary.exists() and not temporary.is_symlink():
                temporary.unlink()
                _fsync_directory(destination.parent)
    except TrustedSignerStage0Error:
        raise
    except OSError as exc:
        _error("trusted_signer_stage0_sudoers_install_failed", exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    raw = stage0._read_regular(
        destination,
        maximum=64 * 1024,
        expected_uid=0,
        allowed_modes=frozenset({0o440}),
    )
    state = destination.lstat()
    if raw != payload or state.st_gid != 0:
        _error("trusted_signer_stage0_sudoers_install_failed")
    return {
        "path": str(destination),
        "uid": 0,
        "gid": 0,
        "mode": "0440",
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _fsync_release_tree(root: Path) -> None:
    """Durably persist every runtime inode before no-replace publication."""

    if not root.is_absolute() or ".." in root.parts:
        _error("trusted_signer_stage0_release_invalid")
    paths = sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True)
    for path in paths:
        state = path.lstat()
        if stat.S_ISLNK(state.st_mode):
            continue
        if stat.S_ISREG(state.st_mode):
            descriptor: int | None = None
            try:
                descriptor = os.open(
                    path,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or (opened.st_dev, opened.st_ino)
                    != (state.st_dev, state.st_ino)
                    or opened.st_nlink != 1
                    or opened.st_uid != 0
                    or opened.st_gid != 0
                ):
                    _error("trusted_signer_stage0_release_invalid")
                os.fsync(descriptor)
                after = os.fstat(descriptor)
                if (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    _error("trusted_signer_stage0_release_invalid")
            except TrustedSignerStage0Error:
                raise
            except OSError as exc:
                _error("trusted_signer_stage0_sync_failed", exc)
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        elif not stat.S_ISDIR(state.st_mode):
            _error("trusted_signer_stage0_release_node_invalid")
    directories = [
        path for path in paths if stat.S_ISDIR(path.lstat().st_mode)
    ]
    directories.append(root)
    for directory in directories:
        _fsync_directory(directory)


def _seal_release(staging_or_release: Path, *, revision: str) -> Mapping[str, Any]:
    staging = HOST_RELEASE_BASE / f".{revision}.bootstrap"
    final = HOST_RELEASE_BASE / revision
    if staging_or_release not in {staging, final}:
        _error("trusted_signer_stage0_release_invalid")
    if os.path.lexists(final):
        final_state = final.lstat()
        if stat.S_ISLNK(final_state.st_mode):
            _error("trusted_signer_stage0_release_conflict")
        target = final
    else:
        target = staging
        if staging_or_release != staging:
            _error("trusted_signer_stage0_release_invalid")
    state = target.lstat()
    if (
        stat.S_ISLNK(state.st_mode)
        or not stat.S_ISDIR(state.st_mode)
        or state.st_uid != 0
        or state.st_gid != 0
        or stat.S_IMODE(state.st_mode) not in {0o700, 0o555}
    ):
        _error("trusted_signer_stage0_release_invalid")
    wheelhouse = target / ".bootstrap-wheelhouse"
    if wheelhouse.exists():
        if wheelhouse.is_symlink() or wheelhouse.parent != target:
            _error("trusted_signer_stage0_release_invalid")
        shutil.rmtree(wheelhouse)
    (target / ".bootstrap-wheelhouse-installed.json").unlink(missing_ok=True)
    for cache in sorted(target.rglob("__pycache__"), reverse=True):
        if cache.is_symlink():
            _error("trusted_signer_stage0_release_invalid")
        shutil.rmtree(cache)
    projection: list[dict[str, Any]] = []
    for path in sorted(target.rglob("*"), key=lambda item: str(item.relative_to(target))):
        relative = str(path.relative_to(target))
        item = path.lstat()
        if item.st_uid != 0 or item.st_gid != 0:
            _error("trusted_signer_stage0_release_owner_invalid")
        if stat.S_ISLNK(item.st_mode):
            link = os.readlink(path)
            if os.path.isabs(link) or ".." in Path(link).parts:
                _error("trusted_signer_stage0_release_symlink_invalid")
            projection.append({"path": relative, "type": "symlink", "target": link})
        elif stat.S_ISDIR(item.st_mode):
            projection.append({"path": relative, "type": "directory", "mode": "0555"})
        elif stat.S_ISREG(item.st_mode):
            mode = 0o555 if stat.S_IMODE(item.st_mode) & 0o111 else 0o444
            os.chmod(path, mode, follow_symlinks=False)
            raw = stage0._read_regular(
                path,
                maximum=MAX_FILE_BYTES,
                expected_uid=0,
                allowed_modes=frozenset({mode}),
                allow_empty=True,
            )
            projection.append({
                "path": relative,
                "type": "file",
                "mode": f"{mode:04o}",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            })
        else:
            _error("trusted_signer_stage0_release_node_invalid")
    for directory in sorted(
        (item for item in target.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        directory.chmod(0o555)
    target.chmod(0o555)
    _fsync_release_tree(target)
    if target == staging:
        _rename_noreplace(staging, final)
        _fsync_directory(HOST_RELEASE_BASE)
    final_state = final.lstat()
    if (
        final_state.st_uid != 0
        or final_state.st_gid != 0
        or stat.S_IMODE(final_state.st_mode) != 0o555
    ):
        _error("trusted_signer_stage0_release_invalid")
    return {
        "path": str(final),
        "uid": 0,
        "gid": 0,
        "mode": "0555",
        "projection_sha256": stage0.sha256_json(projection),
        "projection_count": len(projection),
    }


def _load_host_release_package(
    release: Path,
    *,
    release_revision: str,
) -> Mapping[str, Any]:
    try:
        raw = stage0._read_regular(
            release / "package-manifest.json",
            maximum=stage0.MAX_JSON_BYTES,
            expected_uid=0,
            expected_gid=0,
            allowed_modes=frozenset({0o444}),
        )
        manifest = stage0._load_canonical_json(raw)
    except stage0.OwnerGateStage0Error as exc:
        _error("trusted_signer_stage0_host_package_invalid", exc)
    unsigned = {
        name: item
        for name, item in manifest.items()
        if name != "package_sha256"
    }
    inventory = {
        name: manifest[name]
        for name in stage0.INVENTORY_FIELDS
        if name in manifest
    }
    if (
        frozenset(manifest) != stage0.MANIFEST_FIELDS
        or frozenset(inventory) != stage0.INVENTORY_FIELDS
        or manifest.get("schema") != stage0.PACKAGE_SCHEMA
        or manifest.get("release_revision") != release_revision
        or manifest.get("package_sha256") != stage0.sha256_json(unsigned)
        or manifest.get("package_inventory_sha256")
        != stage0.sha256_json(inventory)
        or manifest.get("activation_performed") is not False
        or manifest.get("cloud_mutation_performed") is not False
        or manifest.get("generic_shell_entrypoint") is not False
        or manifest.get("local_gcloud_runtime_fallback") is not False
    ):
        _error("trusted_signer_stage0_host_package_invalid")
    return dict(manifest)


def _verify_sealed_host_release(release: Path) -> Mapping[str, Any]:
    try:
        root_state = release.lstat()
    except OSError as exc:
        _error("trusted_signer_stage0_release_invalid", exc)
    if (
        stat.S_ISLNK(root_state.st_mode)
        or not stat.S_ISDIR(root_state.st_mode)
        or root_state.st_uid != 0
        or root_state.st_gid != 0
        or stat.S_IMODE(root_state.st_mode) != 0o555
        or (release / ".bootstrap-wheelhouse").exists()
        or (release / ".bootstrap-wheelhouse-installed.json").exists()
    ):
        _error("trusted_signer_stage0_release_invalid")
    projection: list[dict[str, Any]] = []
    try:
        paths = sorted(
            release.rglob("*"),
            key=lambda item: str(item.relative_to(release)),
        )
        for path in paths:
            relative = str(path.relative_to(release))
            item = path.lstat()
            if item.st_uid != 0 or item.st_gid != 0:
                _error("trusted_signer_stage0_release_owner_invalid")
            if stat.S_ISLNK(item.st_mode):
                target = os.readlink(path)
                if os.path.isabs(target) or ".." in Path(target).parts:
                    _error("trusted_signer_stage0_release_symlink_invalid")
                projection.append({
                    "path": relative,
                    "type": "symlink",
                    "target": target,
                })
            elif stat.S_ISDIR(item.st_mode):
                if stat.S_IMODE(item.st_mode) != 0o555:
                    _error("trusted_signer_stage0_release_invalid")
                projection.append({
                    "path": relative,
                    "type": "directory",
                    "mode": "0555",
                })
            elif stat.S_ISREG(item.st_mode):
                mode = stat.S_IMODE(item.st_mode)
                if mode not in {0o444, 0o555}:
                    _error("trusted_signer_stage0_release_invalid")
                raw = stage0._read_regular(
                    path,
                    maximum=MAX_FILE_BYTES,
                    expected_uid=0,
                    expected_gid=0,
                    allowed_modes=frozenset({mode}),
                    allow_empty=True,
                )
                projection.append({
                    "path": relative,
                    "type": "file",
                    "mode": f"{mode:04o}",
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "size": len(raw),
                })
            else:
                _error("trusted_signer_stage0_release_node_invalid")
    except TrustedSignerStage0Error:
        raise
    except OSError as exc:
        _error("trusted_signer_stage0_release_invalid", exc)
    try:
        final_state = release.lstat()
    except OSError as exc:
        _error("trusted_signer_stage0_release_invalid", exc)
    if (
        final_state.st_uid != 0
        or final_state.st_gid != 0
        or stat.S_IMODE(final_state.st_mode) != 0o555
    ):
        _error("trusted_signer_stage0_release_invalid")
    return {
        "path": str(release),
        "uid": 0,
        "gid": 0,
        "mode": "0555",
        "projection_sha256": stage0.sha256_json(projection),
        "projection_count": len(projection),
    }


def _verify_host_sudoers(
    release: Path,
    *,
    sudoers_path: Path,
) -> Mapping[str, Any]:
    expected = _render_sudoers(release)
    raw, _identity = _sudoers_snapshot(sudoers_path)
    if raw != expected:
        _error("trusted_signer_stage0_sudoers_conflict")
    return {
        "path": str(sudoers_path),
        "uid": 0,
        "gid": 0,
        "mode": "0440",
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def verify_host_offline_runtime(
    release_revision: str,
    *,
    expected_receipt: Mapping[str, Any],
    command_runner: Callable[..., bytes] = stage0._run,
    release_base: Path = HOST_RELEASE_BASE,
    sudoers_path: Path = HOST_SUDOERS_PATH,
    current_link: Path = HOST_CURRENT_LINK,
    activation_seal: Path = HOST_ACTIVATION_SEAL,
) -> Mapping[str, Any]:
    """Recompute one exact installed host-runtime receipt without mutation.

    ``preflight_sha256`` is an installation-time capability fact.  The frozen
    receipt supplies that historical value; every fact derivable from the
    installed release is recomputed and the complete receipt must remain
    byte-exact.
    """

    false_fields = (
        "network_install_required",
        "generic_usr_bin_python3_runtime",
        "service_start_performed",
        "service_enablement_mutated",
        "iam_mutation_performed",
        "cloud_mutation_performed",
        "private_key_material_received",
        "private_key_digest_recorded",
    )
    if (
        os.geteuid() != 0  # windows-footgun: ok — Debian root boundary
        or type(release_revision) is not str
        or _RELEASE_REVISION.fullmatch(release_revision) is None
        or not isinstance(expected_receipt, Mapping)
        or frozenset(expected_receipt) != HOST_RUNTIME_RECEIPT_FIELDS
        or expected_receipt.get("schema") != HOST_RUNTIME_RECEIPT_SCHEMA
        or expected_receipt.get("release_revision") != release_revision
        or not _is_sha256(expected_receipt.get("package_sha256"))
        or not _is_sha256(expected_receipt.get("preflight_sha256"))
        or not _is_sha256(
            expected_receipt.get("runtime_inventory_sha256")
        )
        or expected_receipt.get("offline_runtime") is not True
        or expected_receipt.get("current_link_absent") is not True
        or expected_receipt.get("activation_seal_absent") is not True
        or any(expected_receipt.get(name) is not False for name in false_fields)
        or expected_receipt.get("receipt_sha256")
        != stage0.sha256_json({
            name: item
            for name, item in expected_receipt.items()
            if name != "receipt_sha256"
        })
        or release_base != HOST_RELEASE_BASE
        or sudoers_path != HOST_SUDOERS_PATH
        or current_link != HOST_CURRENT_LINK
        or activation_seal != HOST_ACTIVATION_SEAL
    ):
        _error("trusted_signer_stage0_host_runtime_receipt_invalid")
    if (
        os.path.lexists(current_link)
        or os.path.lexists(activation_seal)
    ):
        _error("trusted_signer_stage0_activation_forbidden")
    release = release_base / release_revision
    package = _load_host_release_package(
        release,
        release_revision=release_revision,
    )
    if package["package_sha256"] != expected_receipt["package_sha256"]:
        _error("trusted_signer_stage0_host_runtime_receipt_invalid")
    release_evidence = _verify_sealed_host_release(release)
    sudoers_evidence = _verify_host_sudoers(
        release,
        sudoers_path=sudoers_path,
    )
    if (
        stage0.canonical_json_bytes(release_evidence)
        != stage0.canonical_json_bytes(expected_receipt["release"])
        or stage0.canonical_json_bytes(sudoers_evidence)
        != stage0.canonical_json_bytes(expected_receipt["sudoers"])
    ):
        _error("trusted_signer_stage0_host_runtime_receipt_mismatch")
    try:
        runtime_inventory_raw = command_runner(
            (
                str(release / "venv/bin/python"),
                "-I",
                "-B",
                "-c",
                stage0._runtime_inventory_probe_code(),
            ),
            env=stage0._pip_command_environment(),
        )
        runtime_inventory = stage0.validate_runtime_inventory(
            runtime_inventory_raw,
            venv=release / "venv",
            manifest=package,
        )
    except (OSError, subprocess.SubprocessError, stage0.OwnerGateStage0Error) as exc:
        _error("trusted_signer_stage0_runtime_inventory_invalid", exc)
    if (
        os.path.lexists(current_link)
        or os.path.lexists(activation_seal)
    ):
        _error("trusted_signer_stage0_activation_forbidden")
    post_package = _load_host_release_package(
        release,
        release_revision=release_revision,
    )
    post_release_evidence = _verify_sealed_host_release(release)
    post_sudoers_evidence = _verify_host_sudoers(
        release,
        sudoers_path=sudoers_path,
    )
    if (
        stage0.canonical_json_bytes(post_package)
        != stage0.canonical_json_bytes(package)
        or stage0.canonical_json_bytes(post_release_evidence)
        != stage0.canonical_json_bytes(expected_receipt["release"])
        or stage0.canonical_json_bytes(post_sudoers_evidence)
        != stage0.canonical_json_bytes(expected_receipt["sudoers"])
        or os.path.lexists(current_link)
        or os.path.lexists(activation_seal)
    ):
        _error("trusted_signer_stage0_host_runtime_receipt_mismatch")
    unsigned = {
        "schema": HOST_RUNTIME_RECEIPT_SCHEMA,
        "release_revision": release_revision,
        "package_sha256": package["package_sha256"],
        "preflight_sha256": expected_receipt["preflight_sha256"],
        "release": post_release_evidence,
        "sudoers": post_sudoers_evidence,
        "runtime_inventory_sha256": stage0.sha256_json(runtime_inventory),
        "runtime_interpreter": str(release / "venv/bin/python"),
        "host_attestor_entrypoint": str(
            release / "bin/muncho-host-observation-attestor"
        ),
        "host_provisioner_entrypoint": str(
            release / "bin/muncho-host-trusted-signer-provision"
        ),
        "offline_runtime": True,
        "network_install_required": False,
        "generic_usr_bin_python3_runtime": False,
        "current_link_absent": True,
        "activation_seal_absent": True,
        "service_start_performed": False,
        "service_enablement_mutated": False,
        "iam_mutation_performed": False,
        "cloud_mutation_performed": False,
        "private_key_material_received": False,
        "private_key_digest_recorded": False,
    }
    recomputed = {
        **unsigned,
        "receipt_sha256": stage0.sha256_json(unsigned),
    }
    if (
        stage0.canonical_json_bytes(recomputed)
        != stage0.canonical_json_bytes(expected_receipt)
    ):
        _error("trusted_signer_stage0_host_runtime_receipt_mismatch")
    return recomputed


def _install_host_offline_runtime_locked(
    bundle: Path,
    *,
    expected_uid: int = 0,
    stage0_runner: Callable[..., bytes] = stage0._run,
    command_runner: Callable[[Sequence[str]], bytes] = _default_runner,
    sudoers_path: Path = HOST_SUDOERS_PATH,
) -> Mapping[str, Any]:
    if os.geteuid() != 0:  # windows-footgun: ok — Debian root boundary
        _error("trusted_signer_stage0_root_required")
    if HOST_CURRENT_LINK.exists() or HOST_CURRENT_LINK.is_symlink():
        _error("trusted_signer_stage0_current_link_forbidden")
    if HOST_ACTIVATION_SEAL.exists() or HOST_ACTIVATION_SEAL.is_symlink():
        _error("trusted_signer_stage0_activation_forbidden")
    manifest = stage0.verify_bundle_stage0(
        bundle,
        expected_uid=expected_uid,
        runner=stage0_runner,
    )
    preflight = stage0.validate_target_capabilities(
        manifest,
        bundle=bundle,
        runner=stage0_runner,
        expected_bundle_uid=expected_uid,
    )
    _exact_directory(Path("/opt/muncho-trusted-observation"), mode=0o755)
    _exact_directory(HOST_RELEASE_BASE, mode=0o755)
    _exact_directory(Path("/etc/muncho/trusted-observation"), mode=0o700)
    _exact_directory(Path("/var/lib/muncho/trusted-observation"), mode=0o700)
    _exact_directory(Path("/var/lib/muncho/trusted-observation/receipts"), mode=0o700)
    _exact_directory(Path("/run/muncho-trusted-observation"), mode=0o700)
    staging = stage0.prepare_offline_runtime(
        bundle,
        manifest,
        release_base=HOST_RELEASE_BASE,
        runner=stage0_runner,
    )
    revision = str(manifest["release_revision"])
    release_evidence = _seal_release(staging, revision=revision)
    final = HOST_RELEASE_BASE / revision
    sudoers_payload = _render_sudoers(final)
    sudoers_evidence = _install_sudoers(
        sudoers_payload,
        successor_release=final,
        destination=sudoers_path,
        runner=command_runner,
    )
    runtime_inventory_raw = stage0_runner(
        (
            str(final / "venv/bin/python"),
            "-I",
            "-B",
            "-c",
            stage0._runtime_inventory_probe_code(),
        ),
        env=stage0._pip_command_environment(),
    )
    runtime_inventory = stage0.validate_runtime_inventory(
        runtime_inventory_raw,
        venv=final / "venv",
        manifest=manifest,
    )
    if HOST_CURRENT_LINK.exists() or HOST_CURRENT_LINK.is_symlink():
        _error("trusted_signer_stage0_current_link_forbidden")
    if HOST_ACTIVATION_SEAL.exists() or HOST_ACTIVATION_SEAL.is_symlink():
        _error("trusted_signer_stage0_activation_forbidden")
    unsigned = {
        "schema": HOST_RUNTIME_RECEIPT_SCHEMA,
        "release_revision": revision,
        "package_sha256": manifest["package_sha256"],
        "preflight_sha256": preflight["preflight_sha256"],
        "release": release_evidence,
        "sudoers": sudoers_evidence,
        "runtime_inventory_sha256": stage0.sha256_json(runtime_inventory),
        "runtime_interpreter": str(final / "venv/bin/python"),
        "host_attestor_entrypoint": str(final / "bin/muncho-host-observation-attestor"),
        "host_provisioner_entrypoint": str(final / "bin/muncho-host-trusted-signer-provision"),
        "offline_runtime": True,
        "network_install_required": False,
        "generic_usr_bin_python3_runtime": False,
        "current_link_absent": True,
        "activation_seal_absent": True,
        "service_start_performed": False,
        "service_enablement_mutated": False,
        "iam_mutation_performed": False,
        "cloud_mutation_performed": False,
        "private_key_material_received": False,
        "private_key_digest_recorded": False,
    }
    return {**unsigned, "receipt_sha256": stage0.sha256_json(unsigned)}


def install_host_offline_runtime(
    bundle: Path,
    *,
    expected_uid: int = 0,
    stage0_runner: Callable[..., bytes] = stage0._run,
    command_runner: Callable[[Sequence[str]], bytes] = _default_runner,
    sudoers_path: Path = HOST_SUDOERS_PATH,
    lock_path: Path = HOST_STAGE0_LOCK,
) -> Mapping[str, Any]:
    with _stage0_lock(lock_path):
        return _install_host_offline_runtime_locked(
            bundle,
            expected_uid=expected_uid,
            stage0_runner=stage0_runner,
            command_runner=command_runner,
            sudoers_path=sudoers_path,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False, add_help=False)
    parser.add_argument("operation", choices=("install",))
    parser.add_argument("--bundle", type=Path, required=True)
    arguments = parser.parse_args(argv)
    result = install_host_offline_runtime(arguments.bundle)
    print(stage0.canonical_json_bytes(result).decode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "HOST_ACTIVATION_SEAL",
    "HOST_CURRENT_LINK",
    "HOST_RELEASE_BASE",
    "HOST_RUNTIME_RECEIPT_FIELDS",
    "HOST_RUNTIME_RECEIPT_SCHEMA",
    "HOST_SUDOERS_PATH",
    "TrustedSignerStage0Error",
    "install_host_offline_runtime",
    "main",
    "verify_host_offline_runtime",
]
