#!/usr/bin/env python3
"""Install the dormant rotation-stager builder boundary, without activating it.

The installer copies a closed, hard-coded asset set from one exact Git commit,
creates only the dedicated builder identity and promotion lock, and stops.  It
never enables, starts, schedules, or reloads a service; it never changes a
release pointer, gateway, application data, or credentials.
"""

from __future__ import annotations

import argparse
import ast
import ctypes
import errno
import fcntl
import grp
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Never, Sequence

from scripts.canary import production_release_builder_phase as phase


INSTALL_RECEIPT_SCHEMA = "muncho-production-unit-input-rotation-stager-installation.v1"
REVISION_QUALIFIED_INSTALL_RECEIPT_SCHEMA = (
    "muncho-production-unit-input-rotation-stager-installation.v2"
)
LATCHED_REVISION_QUALIFIED_INSTALL_RECEIPT_SCHEMA = (
    "muncho-production-unit-input-rotation-stager-installation.v3"
)
SUCCESSOR_REBIND_FOUNDATION_INSTALL_RECEIPT_SCHEMA = (
    "muncho-production-unit-input-rotation-stager-installation.v4"
)
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_REMOTE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ASSETS: Mapping[str, tuple[str, int]] = {
    "scripts/__init__.py": (
        "library/scripts/__init__.py",
        0o444,
    ),
    "scripts/canary/__init__.py": (
        "library/scripts/canary/__init__.py",
        0o444,
    ),
    "scripts/canary/production_release_builder_runtime.py": (
        "library/scripts/canary/production_release_builder_runtime.py",
        0o444,
    ),
    "scripts/canary/production_release_builder_phase.py": (
        "library/scripts/canary/production_release_builder_phase.py",
        0o444,
    ),
    "scripts/canary/production_release_candidate_promoter.py": (
        "library/scripts/canary/production_release_candidate_promoter.py",
        0o444,
    ),
    "scripts/canary/production_release_rotation_stager_input_author.py": (
        "library/scripts/canary/production_release_rotation_stager_input_author.py",
        0o444,
    ),
    "scripts/canary/production_release_rotation_stager_launcher.py": (
        "library/scripts/canary/production_release_rotation_stager_launcher.py",
        0o444,
    ),
    "scripts/canary/production_release_rotation_stager_promoter.py": (
        "library/scripts/canary/production_release_rotation_stager_promoter.py",
        0o444,
    ),
    "scripts/canary/production_successor_rebind_owner_runtime_preexec.py": (
        "library/scripts/canary/production_successor_rebind_owner_runtime_preexec.py",
        0o444,
    ),
    "ops/muncho/release-updater/muncho-release-builder.sysusers": (
        "sysusers/muncho-release-builder.conf",
        0o444,
    ),
    "ops/muncho/release-updater/muncho-release-builder.tmpfiles": (
        "tmpfiles/muncho-release-builder.conf",
        0o444,
    ),
    "ops/muncho/release-updater/muncho-release-builder@.service": (
        "systemd/muncho-release-builder@.service",
        0o444,
    ),
    "ops/muncho/release-updater/muncho-release-builder-phase": (
        "libexec/muncho-release-builder-phase",
        0o555,
    ),
    "ops/muncho/release-updater/muncho-release-candidate-promoter": (
        "libexec/muncho-release-candidate-promoter",
        0o555,
    ),
    "ops/muncho/release-updater/muncho-release-unit-input-rotation-stager": (
        "libexec/muncho-release-unit-input-rotation-stager",
        0o555,
    ),
    "ops/muncho/release-updater/muncho-release-rotation-stager-input-author": (
        "libexec/muncho-release-rotation-stager-input-author",
        0o555,
    ),
}
_REVISION_LIBRARY_ASSETS: Mapping[str, tuple[str, int]] = {
    source: (target.removeprefix("library/"), mode)
    for source, (target, mode) in _SOURCE_ASSETS.items()
    if target.startswith("library/")
}
_REVISION_STATIC_ASSETS: Mapping[str, tuple[str, int]] = {
    "ops/muncho/release-updater/muncho-release-builder.sysusers": (
        "sysusers/muncho-release-builder.conf",
        0o444,
    ),
    "ops/muncho/release-updater/muncho-release-builder.tmpfiles": (
        "tmpfiles/muncho-release-builder.conf",
        0o444,
    ),
    "ops/muncho/release-updater/muncho-release-builder-v2@.service": (
        "systemd/muncho-release-builder-v2@.service",
        0o444,
    ),
    "ops/muncho/release-updater/muncho-release-foundation-exec-v2": (
        "libexec/muncho-release-foundation-exec-v2",
        0o555,
    ),
}
_LATCHED_REVISION_STATIC_ASSETS: Mapping[str, tuple[str, int]] = {
    "ops/muncho/release-updater/muncho-release-builder.sysusers": (
        "sysusers/muncho-release-builder.conf",
        0o444,
    ),
    "ops/muncho/release-updater/muncho-release-builder.tmpfiles": (
        "tmpfiles/muncho-release-builder.conf",
        0o444,
    ),
    "ops/muncho/release-updater/muncho-release-builder-v3@.service": (
        "systemd/muncho-release-builder-v3@.service",
        0o444,
    ),
    "ops/muncho/release-updater/muncho-release-foundation-exec-v3": (
        "libexec/muncho-release-foundation-exec-v3",
        0o555,
    ),
}
_SUCCESSOR_REBIND_STATIC_ASSETS: Mapping[str, tuple[str, int]] = {
    **_LATCHED_REVISION_STATIC_ASSETS,
    "ops/muncho/release-updater/muncho-release-foundation-exec-v4": (
        "libexec/muncho-release-foundation-exec-v4",
        0o555,
    ),
    "ops/muncho/release-updater/muncho-successor-runtime-foundation-exec": (
        "libexec/muncho-successor-runtime-foundation-exec",
        0o555,
    ),
}
_SUCCESSOR_RUNTIME_CONTROLLER_ASSETS: Mapping[str, tuple[str, int]] = {
    "gateway/__init__.py": ("gateway/__init__.py", 0o444),
    "gateway/production_owner_runtime.py": (
        "gateway/production_owner_runtime.py",
        0o444,
    ),
    "scripts/__init__.py": ("scripts/__init__.py", 0o444),
    "scripts/canary/__init__.py": ("scripts/canary/__init__.py", 0o444),
    "scripts/canary/package_production_owner_runtime.py": (
        "scripts/canary/package_production_owner_runtime.py",
        0o444,
    ),
    "scripts/canary/production_successor_rebind_owner_runtime.py": (
        "scripts/canary/production_successor_rebind_owner_runtime.py",
        0o444,
    ),
    "scripts/canary/production_successor_rebind_owner_runtime_preexec.py": (
        "scripts/canary/production_successor_rebind_owner_runtime_preexec.py",
        0o444,
    ),
    "scripts/canary/production_successor_rebind_owner_runtime_launcher.py": (
        "scripts/canary/production_successor_rebind_owner_runtime_launcher.py",
        0o444,
    ),
}
SUCCESSOR_RUNTIME_CONTROLLER_MANIFEST_SCHEMA = (
    "muncho-successor-runtime-controller-manifest.v1"
)
SUCCESSOR_RUNTIME_CONTROLLER_MANIFEST_NAME = "controller-manifest.json"
_RECOVERABLE_GIT_DIRECTORIES = frozenset({
    "branches",
    "hooks",
    "info",
    "logs",
    "objects",
    "refs",
})
_RECOVERABLE_GIT_FILES = frozenset({
    "COMMIT_EDITMSG",
    "FETCH_HEAD",
    "HEAD",
    "ORIG_HEAD",
    "config",
    "description",
    "index",
    "packed-refs",
    "shallow",
})


class RotationStagerInstallerError(RuntimeError):
    """Stable, secret-free installation failure."""


def _fail(code: str, cause: BaseException | None = None) -> Never:
    del cause
    raise RotationStagerInstallerError(code) from None


def _read_posix_identity(name: Literal["geteuid", "getegid"]) -> int:
    reader = getattr(os, name, None)
    if not callable(reader):
        _fail("rotation_stager_installer_posix_identity_unavailable")
    try:
        value = reader()
    except (OSError, TypeError, ValueError) as exc:
        _fail("rotation_stager_installer_posix_identity_unavailable", exc)
    if type(value) is not int or value < 0:
        _fail("rotation_stager_installer_posix_identity_unavailable")
    return value


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
        _fail("rotation_stager_installer_json_invalid", exc)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class InstallerRoots:
    library: Path = Path("/usr/lib/muncho-release-updater")
    sysusers: Path = Path("/usr/lib/sysusers.d")
    tmpfiles: Path = Path("/usr/lib/tmpfiles.d")
    systemd: Path = Path("/etc/systemd/system")
    libexec: Path = Path("/usr/libexec")
    job_root: Path = phase.PRODUCTION_JOB_ROOT
    promotion_lock: Path = Path("/run/lock/muncho-release-builder-promotion.lock")
    library_releases: Path = Path("/usr/lib/muncho-release-updater-releases")


@dataclass(frozen=True)
class _RecoverableSnapshotEntry:
    kind: Literal["regular", "symlink", "gitlink"]
    symlink_target: bytes | None = None


@dataclass(frozen=True)
class _RecoverableSnapshotLayout:
    entries: Mapping[str, _RecoverableSnapshotEntry]
    directories: frozenset[str]


def _target(roots: InstallerRoots, relative: str) -> Path:
    namespace, remainder = relative.split("/", 1)
    root = getattr(roots, namespace)
    return root / remainder


def _git_environment() -> Mapping[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _git_command(source: Path, *arguments: str) -> tuple[str, ...]:
    return (
        "/usr/bin/git",
        "-c",
        f"safe.directory={source}",
        "-C",
        str(source),
        *arguments,
    )


def _git(
    source: Path,
    *arguments: str,
    maximum: int = 64 * 1024 * 1024,
    preserve_fd: int | None = None,
) -> bytes:
    try:
        completed = subprocess.run(
            _git_command(source, *arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=300,
            env=_git_environment(),
            pass_fds=(() if preserve_fd is None else (preserve_fd,)),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _fail("rotation_stager_installer_git_invalid", exc)
    if completed.returncode != 0 or completed.stderr or len(completed.stdout) > maximum:
        _fail("rotation_stager_installer_git_invalid")
    return completed.stdout


def _line(raw: bytes) -> str:
    try:
        value = raw.decode("ascii", errors="strict").strip()
    except UnicodeError as exc:
        _fail("rotation_stager_installer_git_invalid", exc)
    if not value or "\n" in value or "\r" in value:
        _fail("rotation_stager_installer_git_invalid")
    return value


def _install_exact_source_snapshot(
    *,
    source_root: Path,
    destination: Path,
    release_revision: str,
    source_tree_oid: str,
    expected_uid: int,
    expected_gid: int,
) -> bool:
    """Create one fixed, local-only Git source snapshot for the builder."""

    intent = _source_snapshot_clone_intent(
        source_root=source_root,
        destination=destination,
        release_revision=release_revision,
        source_tree_oid=source_tree_oid,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    intent_raw = _canonical(intent) + b"\n"
    intent_sha256 = str(intent["intent_sha256"])
    lock_path = destination.with_name(f".{destination.name}.clone.lock")
    intent_path = destination.with_name(f".{destination.name}.clone-intent.json")
    intent_pending = destination.with_name(f".{destination.name}.clone-intent.pending")
    incomplete = destination.with_name(
        f".{destination.name}.{intent_sha256}.incomplete"
    )
    quarantine = destination.with_name(
        f".{destination.name}.{intent_sha256}.quarantine"
    )
    recoverable_layout = _recoverable_snapshot_layout(
        source_root=source_root,
        release_revision=release_revision,
    )

    def validate(selected: Path, *, preserve_fd: int) -> None:
        try:
            state = os.lstat(selected)
        except OSError as exc:
            _fail("rotation_stager_installer_source_snapshot_invalid", exc)
        if (
            not stat.S_ISDIR(state.st_mode)
            or stat.S_ISLNK(state.st_mode)
            or state.st_uid != expected_uid
            or state.st_gid != expected_gid
            or stat.S_IMODE(state.st_mode) & 0o022
            or _line(_git(selected, "rev-parse", "HEAD", preserve_fd=preserve_fd))
            != release_revision
            or _line(
                _git(
                    selected,
                    "rev-parse",
                    "HEAD^{tree}",
                    preserve_fd=preserve_fd,
                )
            )
            != source_tree_oid
            or _git(
                selected,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                preserve_fd=preserve_fd,
            )
        ):
            _fail("rotation_stager_installer_source_snapshot_invalid")
        _validate_local_snapshot_tree(
            selected,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
        _validate_recoverable_snapshot_tree(
            selected,
            layout=recoverable_layout,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )

    lock_descriptor = _acquire_source_snapshot_lock(
        lock_path,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    try:
        final_intent_exists = os.path.lexists(intent_path)
        pending_intent_exists = os.path.lexists(intent_pending)
        try:
            reserved_transactions = {
                entry.name
                for entry in os.scandir(destination.parent)
                if entry.name.startswith(f".{destination.name}.")
                and entry.name.endswith((".incomplete", ".quarantine"))
            }
        except OSError as exc:
            _fail("rotation_stager_installer_source_snapshot_invalid", exc)
        allowed_transactions = {incomplete.name, quarantine.name}
        if reserved_transactions - allowed_transactions:
            _fail("rotation_stager_installer_source_snapshot_conflict")
        if not final_intent_exists and (
            os.path.lexists(destination)
            or os.path.lexists(incomplete)
            or os.path.lexists(quarantine)
        ):
            # The durable intent is published before any clone-side mutation.
            # Without it, a same-named tree is foreign even if its bytes happen
            # to look usable; preserve it for an owner to inspect.
            _fail("rotation_stager_installer_source_snapshot_conflict")
        if pending_intent_exists and os.path.lexists(destination):
            _fail("rotation_stager_installer_source_snapshot_conflict")
        _publish_clone_intent_exact(
            intent_path,
            intent_pending,
            intent_raw,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
        created = False
        if os.path.lexists(destination):
            validate(destination, preserve_fd=lock_descriptor)
            if os.path.lexists(incomplete) or os.path.lexists(quarantine):
                _fail("rotation_stager_installer_source_snapshot_conflict")
            return False

        if os.path.lexists(quarantine):
            _remove_quarantined_source_snapshot(
                quarantine,
                layout=recoverable_layout,
                expected_uid=expected_uid,
                expected_gid=expected_gid,
            )
        if os.path.lexists(incomplete):
            try:
                validate(incomplete, preserve_fd=lock_descriptor)
            except RotationStagerInstallerError:
                _quarantine_and_remove_incomplete_source_snapshot(
                    incomplete,
                    quarantine,
                    layout=recoverable_layout,
                    expected_uid=expected_uid,
                    expected_gid=expected_gid,
                )
            else:
                _fsync_snapshot_tree(incomplete)
                _rename_directory_noreplace(incomplete, destination)
                _fsync_directory(destination.parent)
                validate(destination, preserve_fd=lock_descriptor)
                return True

        if not os.path.lexists(incomplete):
            try:
                completed = subprocess.run(
                    (
                        "/usr/bin/git",
                        "-c",
                        "protocol.file.allow=always",
                        "clone",
                        "--quiet",
                        "--local",
                        "--no-hardlinks",
                        "--no-checkout",
                        "--",
                        str(source_root),
                        str(incomplete),
                    ),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=300,
                    env=_git_environment(),
                    pass_fds=(lock_descriptor,),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                _fail("rotation_stager_installer_source_snapshot_invalid", exc)
            if completed.returncode != 0 or completed.stdout or completed.stderr:
                if os.path.lexists(incomplete):
                    _quarantine_and_remove_incomplete_source_snapshot(
                        incomplete,
                        quarantine,
                        layout=recoverable_layout,
                        expected_uid=expected_uid,
                        expected_gid=expected_gid,
                    )
                _fail("rotation_stager_installer_source_snapshot_invalid")
        try:
            state = os.lstat(incomplete)
        except OSError as exc:
            _fail("rotation_stager_installer_source_snapshot_invalid", exc)
        if (
            not stat.S_ISDIR(state.st_mode)
            or stat.S_ISLNK(state.st_mode)
            or state.st_uid != expected_uid
            or state.st_gid != expected_gid
            or stat.S_IMODE(state.st_mode) & 0o022
        ):
            _fail("rotation_stager_installer_source_snapshot_invalid")
        _git(
            incomplete,
            "checkout",
            "--quiet",
            "--force",
            "--detach",
            release_revision,
            preserve_fd=lock_descriptor,
        )
        validate(incomplete, preserve_fd=lock_descriptor)
        _fsync_snapshot_tree(incomplete)
        _rename_directory_noreplace(incomplete, destination)
        _fsync_directory(destination.parent)
        created = True
        validate(destination, preserve_fd=lock_descriptor)
        return created
    finally:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(lock_descriptor)


def _source_snapshot_clone_intent(
    *,
    source_root: Path,
    destination: Path,
    release_revision: str,
    source_tree_oid: str,
    expected_uid: int,
    expected_gid: int,
) -> Mapping[str, Any]:
    unsigned = {
        "schema": "muncho-release-source-snapshot-clone-intent.v1",
        "source_root": str(source_root),
        "destination": str(destination),
        "release_revision": release_revision,
        "source_tree_oid": source_tree_oid,
        "expected_uid": expected_uid,
        "expected_gid": expected_gid,
        "clone_transport": "local-path-only",
        "clone_no_hardlinks": True,
        "clone_checkout": False,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {
        **unsigned,
        "intent_sha256": _sha256(_canonical(unsigned)),
    }


def _acquire_source_snapshot_lock(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
) -> int:
    descriptor: int | None = None
    created = False
    try:
        try:
            descriptor = os.open(
                path,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            created = True
        except FileExistsError:
            descriptor = os.open(
                path,
                os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        state = os.fstat(descriptor)
        if (
            not stat.S_ISREG(state.st_mode)
            or state.st_uid != expected_uid
            or state.st_gid != expected_gid
            or state.st_nlink != 1
            or stat.S_IMODE(state.st_mode) != 0o600
            or state.st_size != 0
        ):
            _fail("rotation_stager_installer_source_snapshot_lock_invalid")
        if created:
            os.fsync(descriptor)
            _fsync_directory(path.parent)
        return descriptor
    except RotationStagerInstallerError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        _fail("rotation_stager_installer_source_snapshot_lock_invalid", exc)


def _read_exact_regular_file(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
    allowed_modes: frozenset[int],
    maximum: int,
) -> tuple[bytes, os.stat_result]:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        state = os.fstat(descriptor)
        if (
            not stat.S_ISREG(state.st_mode)
            or state.st_uid != expected_uid
            or state.st_gid != expected_gid
            or stat.S_IMODE(state.st_mode) not in allowed_modes
            or state.st_size > maximum
        ):
            _fail("rotation_stager_installer_source_snapshot_intent_invalid")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining > 0:
            raw = os.read(descriptor, min(remaining, 64 * 1024))
            if not raw:
                break
            chunks.append(raw)
            remaining -= len(raw)
        value = b"".join(chunks)
        if len(value) > maximum:
            _fail("rotation_stager_installer_source_snapshot_intent_invalid")
        return value, state
    except RotationStagerInstallerError:
        raise
    except OSError as exc:
        _fail("rotation_stager_installer_source_snapshot_intent_invalid", exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _publish_clone_intent_exact(
    path: Path,
    pending: Path,
    raw: bytes,
    *,
    expected_uid: int,
    expected_gid: int,
) -> None:
    maximum = max(len(raw), 1)
    try:
        final_exists = os.path.lexists(path)
        pending_exists = os.path.lexists(pending)
    except OSError as exc:
        _fail("rotation_stager_installer_source_snapshot_intent_invalid", exc)

    if final_exists:
        existing, final_state = _read_exact_regular_file(
            path,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            allowed_modes=frozenset({0o444}),
            maximum=maximum,
        )
        if existing != raw:
            _fail("rotation_stager_installer_source_snapshot_intent_conflict")
        if not pending_exists:
            if final_state.st_nlink != 1:
                _fail("rotation_stager_installer_source_snapshot_intent_invalid")
            return
        staged, staged_state = _read_exact_regular_file(
            pending,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
            allowed_modes=frozenset({0o444, 0o600}),
            maximum=maximum,
        )
        if staged != raw:
            _fail("rotation_stager_installer_source_snapshot_intent_conflict")
        same_inode = (
            final_state.st_dev == staged_state.st_dev
            and final_state.st_ino == staged_state.st_ino
        )
        if not same_inode:
            _fail("rotation_stager_installer_source_snapshot_intent_conflict")
        if final_state.st_nlink != 2 or staged_state.st_nlink != 2:
            _fail("rotation_stager_installer_source_snapshot_intent_invalid")
        try:
            os.unlink(pending)
            _fsync_directory(path.parent)
        except OSError as exc:
            _fail("rotation_stager_installer_source_snapshot_intent_invalid", exc)
        return

    if not pending_exists:
        descriptor: int | None = None
        try:
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
            os.fchown(descriptor, expected_uid, expected_gid)
            os.fsync(descriptor)
            _fsync_directory(path.parent)
        except OSError as exc:
            _fail("rotation_stager_installer_source_snapshot_intent_invalid", exc)
        finally:
            if descriptor is not None:
                os.close(descriptor)

    staged, staged_state = _read_exact_regular_file(
        pending,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        allowed_modes=frozenset({0o444, 0o600}),
        maximum=maximum,
    )
    if staged_state.st_nlink != 1:
        _fail("rotation_stager_installer_source_snapshot_intent_invalid")
    if stat.S_IMODE(staged_state.st_mode) == 0o600:
        if not raw.startswith(staged):
            _fail("rotation_stager_installer_source_snapshot_intent_conflict")
        descriptor = None
        try:
            descriptor = os.open(
                pending,
                os.O_WRONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            state = os.fstat(descriptor)
            if (
                not stat.S_ISREG(state.st_mode)
                or state.st_uid != expected_uid
                or state.st_gid != expected_gid
                or state.st_nlink != 1
                or stat.S_IMODE(state.st_mode) != 0o600
            ):
                _fail("rotation_stager_installer_source_snapshot_intent_invalid")
            os.ftruncate(descriptor, 0)
            view = memoryview(raw)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    _fail("rotation_stager_installer_source_snapshot_intent_invalid")
                view = view[written:]
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o444)
            os.fsync(descriptor)
        except RotationStagerInstallerError:
            raise
        except OSError as exc:
            _fail("rotation_stager_installer_source_snapshot_intent_invalid", exc)
        finally:
            if descriptor is not None:
                os.close(descriptor)
    elif staged != raw:
        _fail("rotation_stager_installer_source_snapshot_intent_conflict")

    try:
        os.link(pending, path, follow_symlinks=False)
        _fsync_directory(path.parent)
        os.unlink(pending)
        _fsync_directory(path.parent)
    except FileExistsError:
        _fail("rotation_stager_installer_source_snapshot_intent_conflict")
    except OSError as exc:
        _fail("rotation_stager_installer_source_snapshot_intent_invalid", exc)
    existing, state = _read_exact_regular_file(
        path,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        allowed_modes=frozenset({0o444}),
        maximum=maximum,
    )
    if existing != raw or state.st_nlink != 1:
        _fail("rotation_stager_installer_source_snapshot_intent_invalid")


def _validate_local_snapshot_tree(
    root: Path,
    *,
    expected_uid: int,
    expected_gid: int,
) -> None:
    if os.path.lexists(root / ".git/objects/info/alternates"):
        _fail("rotation_stager_installer_source_snapshot_invalid")

    def walk_error(cause: OSError) -> Never:
        _fail("rotation_stager_installer_source_snapshot_invalid", cause)

    try:
        for current, names, files in os.walk(
            root,
            followlinks=False,
            onerror=walk_error,
        ):
            selected = Path(current)
            for name in (*names, *files):
                state = os.lstat(selected / name)
                if state.st_uid != expected_uid or state.st_gid != expected_gid:
                    _fail("rotation_stager_installer_source_snapshot_invalid")
                if stat.S_ISLNK(state.st_mode):
                    continue
                if not stat.S_ISREG(state.st_mode) and not stat.S_ISDIR(state.st_mode):
                    _fail("rotation_stager_installer_source_snapshot_invalid")
                if stat.S_IMODE(state.st_mode) & 0o022 or (
                    stat.S_ISREG(state.st_mode) and state.st_nlink != 1
                ):
                    _fail("rotation_stager_installer_source_snapshot_invalid")
    except RotationStagerInstallerError:
        raise
    except OSError as exc:
        _fail("rotation_stager_installer_source_snapshot_invalid", exc)


def _recoverable_snapshot_layout(
    *,
    source_root: Path,
    release_revision: str,
) -> _RecoverableSnapshotLayout:
    raw = _git(
        source_root,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        release_revision,
    )
    entries: dict[str, _RecoverableSnapshotEntry] = {}
    directories: set[str] = set()
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, path_raw = record.split(b"\t", 1)
            mode_raw, type_raw, oid_raw = metadata.split(b" ", 2)
            mode = mode_raw.decode("ascii", errors="strict")
            object_type = type_raw.decode("ascii", errors="strict")
            oid = oid_raw.decode("ascii", errors="strict")
            relative = os.fsdecode(path_raw)
        except (UnicodeError, ValueError) as exc:
            _fail("rotation_stager_installer_source_snapshot_layout_invalid", exc)
        parts = Path(relative).parts
        if (
            not relative
            or relative.startswith("/")
            or not parts
            or any(part in {"", ".", "..", ".git"} for part in parts)
            or relative in entries
        ):
            _fail("rotation_stager_installer_source_snapshot_layout_invalid")
        if mode in {"100644", "100755"} and object_type == "blob":
            selected = _RecoverableSnapshotEntry(kind="regular")
        elif mode == "120000" and object_type == "blob":
            target = _git(source_root, "cat-file", "blob", oid, maximum=64 * 1024)
            if not target or b"\x00" in target:
                _fail("rotation_stager_installer_source_snapshot_layout_invalid")
            selected = _RecoverableSnapshotEntry(
                kind="symlink",
                symlink_target=target,
            )
        elif mode == "160000" and object_type == "commit":
            selected = _RecoverableSnapshotEntry(kind="gitlink")
        else:
            _fail("rotation_stager_installer_source_snapshot_layout_invalid")
        entries[relative] = selected
        parent = Path(relative).parent
        while parent != Path("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return _RecoverableSnapshotLayout(
        entries=entries,
        directories=frozenset(directories),
    )


def _validate_recoverable_snapshot_tree(
    root: Path,
    *,
    layout: _RecoverableSnapshotLayout,
    expected_uid: int,
    expected_gid: int,
) -> None:
    _validate_owned_snapshot_directory(
        root,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )

    def walk_error(cause: OSError) -> Never:
        _fail("rotation_stager_installer_source_snapshot_conflict", cause)

    try:
        for current, names, files in os.walk(
            root,
            followlinks=False,
            onerror=walk_error,
        ):
            selected_root = Path(current)
            for name in (*names, *files):
                selected = selected_root / name
                relative = selected.relative_to(root).as_posix()
                state = os.lstat(selected)
                if state.st_uid != expected_uid or state.st_gid != expected_gid:
                    _fail("rotation_stager_installer_source_snapshot_conflict")
                if relative == ".git" or relative.startswith(".git/"):
                    parts = relative.split("/")
                    if len(parts) == 1:
                        known_git_entry = stat.S_ISDIR(state.st_mode)
                    elif parts[1] in _RECOVERABLE_GIT_DIRECTORIES:
                        known_git_entry = len(parts) >= 2
                    else:
                        root_name = parts[1]
                        lock_base = root_name.removesuffix(".lock")
                        known_git_entry = len(parts) == 2 and (
                            root_name in _RECOVERABLE_GIT_FILES
                            or (
                                root_name.endswith(".lock")
                                and lock_base in _RECOVERABLE_GIT_FILES
                            )
                        )
                    if not known_git_entry:
                        _fail("rotation_stager_installer_source_snapshot_conflict")
                    if stat.S_ISREG(state.st_mode):
                        if state.st_nlink != 1 or stat.S_IMODE(state.st_mode) & 0o022:
                            _fail("rotation_stager_installer_source_snapshot_conflict")
                    elif stat.S_ISDIR(state.st_mode):
                        if stat.S_IMODE(state.st_mode) & 0o022:
                            _fail("rotation_stager_installer_source_snapshot_conflict")
                    else:
                        _fail("rotation_stager_installer_source_snapshot_conflict")
                    continue
                expected = layout.entries.get(relative)
                if expected is None:
                    if relative not in layout.directories or not stat.S_ISDIR(
                        state.st_mode
                    ):
                        _fail("rotation_stager_installer_source_snapshot_conflict")
                    if stat.S_IMODE(state.st_mode) & 0o022:
                        _fail("rotation_stager_installer_source_snapshot_conflict")
                    continue
                if expected.kind == "regular":
                    if (
                        not stat.S_ISREG(state.st_mode)
                        or state.st_nlink != 1
                        or stat.S_IMODE(state.st_mode) & 0o022
                    ):
                        _fail("rotation_stager_installer_source_snapshot_conflict")
                elif expected.kind == "symlink":
                    if not stat.S_ISLNK(state.st_mode):
                        _fail("rotation_stager_installer_source_snapshot_conflict")
                    target = os.fsencode(os.readlink(selected))
                    if target != expected.symlink_target:
                        _fail("rotation_stager_installer_source_snapshot_conflict")
                elif expected.kind == "gitlink":
                    if not stat.S_ISDIR(state.st_mode) or (
                        stat.S_IMODE(state.st_mode) & 0o022
                    ):
                        _fail("rotation_stager_installer_source_snapshot_conflict")
                else:
                    _fail("rotation_stager_installer_source_snapshot_conflict")
    except RotationStagerInstallerError:
        raise
    except (OSError, ValueError) as exc:
        _fail("rotation_stager_installer_source_snapshot_conflict", exc)


def _quarantine_and_remove_incomplete_source_snapshot(
    incomplete: Path,
    quarantine: Path,
    *,
    layout: _RecoverableSnapshotLayout,
    expected_uid: int,
    expected_gid: int,
) -> None:
    _validate_recoverable_snapshot_tree(
        incomplete,
        layout=layout,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    if os.path.lexists(quarantine):
        _fail("rotation_stager_installer_source_snapshot_conflict")
    _rename_directory_noreplace(incomplete, quarantine)
    _fsync_directory(incomplete.parent)
    _remove_quarantined_source_snapshot(
        quarantine,
        layout=layout,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )


def _validate_owned_snapshot_directory(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
) -> None:
    try:
        state = os.lstat(path)
    except OSError as exc:
        _fail("rotation_stager_installer_source_snapshot_invalid", exc)
    if (
        not stat.S_ISDIR(state.st_mode)
        or stat.S_ISLNK(state.st_mode)
        or state.st_uid != expected_uid
        or state.st_gid != expected_gid
        or stat.S_IMODE(state.st_mode) & 0o022
    ):
        _fail("rotation_stager_installer_source_snapshot_conflict")


def _remove_quarantined_source_snapshot(
    quarantine: Path,
    *,
    layout: _RecoverableSnapshotLayout,
    expected_uid: int,
    expected_gid: int,
) -> None:
    _validate_recoverable_snapshot_tree(
        quarantine,
        layout=layout,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    parent_descriptor: int | None = None
    directory_descriptor: int | None = None
    try:
        parent_descriptor = os.open(
            quarantine.parent,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        directory_descriptor = os.open(
            quarantine.name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        _clear_owned_directory_fd(
            directory_descriptor,
            expected_uid=expected_uid,
            expected_gid=expected_gid,
        )
        os.close(directory_descriptor)
        directory_descriptor = None
        os.rmdir(quarantine.name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    except RotationStagerInstallerError:
        raise
    except OSError as exc:
        _fail("rotation_stager_installer_source_snapshot_cleanup_failed", exc)
    finally:
        if directory_descriptor is not None:
            os.close(directory_descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def _clear_owned_directory_fd(
    descriptor: int,
    *,
    expected_uid: int,
    expected_gid: int,
) -> None:
    try:
        entries = tuple(os.scandir(descriptor))
    except OSError as exc:
        _fail("rotation_stager_installer_source_snapshot_cleanup_failed", exc)
    for entry in entries:
        try:
            state = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
        except OSError as exc:
            _fail("rotation_stager_installer_source_snapshot_cleanup_failed", exc)
        if state.st_uid != expected_uid or state.st_gid != expected_gid:
            _fail("rotation_stager_installer_source_snapshot_conflict")
        if stat.S_ISDIR(state.st_mode):
            child = None
            try:
                child = os.open(
                    entry.name,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
                _clear_owned_directory_fd(
                    child,
                    expected_uid=expected_uid,
                    expected_gid=expected_gid,
                )
            finally:
                if child is not None:
                    os.close(child)
            os.rmdir(entry.name, dir_fd=descriptor)
        elif stat.S_ISREG(state.st_mode):
            if state.st_nlink != 1:
                _fail("rotation_stager_installer_source_snapshot_conflict")
            os.unlink(entry.name, dir_fd=descriptor)
        elif stat.S_ISLNK(state.st_mode):
            os.unlink(entry.name, dir_fd=descriptor)
        else:
            _fail("rotation_stager_installer_source_snapshot_conflict")
    try:
        os.fsync(descriptor)
    except OSError as exc:
        _fail("rotation_stager_installer_source_snapshot_cleanup_failed", exc)


def _fsync_directory(path: Path) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        os.fsync(descriptor)
    except OSError as exc:
        _fail("rotation_stager_installer_source_snapshot_durability_failed", exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _fsync_snapshot_tree(root: Path) -> None:
    directories: list[Path] = []

    def walk_error(cause: OSError) -> Never:
        _fail("rotation_stager_installer_source_snapshot_durability_failed", cause)

    try:
        for current, names, files in os.walk(
            root,
            followlinks=False,
            onerror=walk_error,
        ):
            selected = Path(current)
            directories.append(selected)
            for name in (*names, *files):
                path = selected / name
                state = os.lstat(path)
                if not stat.S_ISREG(state.st_mode):
                    continue
                descriptor = os.open(
                    path,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        for directory in reversed(directories):
            _fsync_directory(directory)
    except OSError as exc:
        _fail("rotation_stager_installer_source_snapshot_durability_failed", exc)


def _rename_directory_noreplace(source: Path, destination: Path) -> None:
    try:
        library = ctypes.CDLL(None, use_errno=True)
        if sys.platform.startswith("linux"):
            function = library.renameat2
            function.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            function.restype = ctypes.c_int
            result = function(
                -100,
                os.fsencode(source),
                -100,
                os.fsencode(destination),
                1,
            )
        elif sys.platform == "darwin":
            function = library.renamex_np
            function.argtypes = (
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            function.restype = ctypes.c_int
            result = function(os.fsencode(source), os.fsencode(destination), 0x00000004)
        else:
            _fail("rotation_stager_installer_source_snapshot_rename_unsupported")
    except (AttributeError, OSError) as exc:
        _fail("rotation_stager_installer_source_snapshot_rename_unsupported", exc)
    if result != 0:
        selected_errno = ctypes.get_errno()
        if selected_errno == errno.EEXIST:
            _fail("rotation_stager_installer_source_snapshot_conflict")
        _fail("rotation_stager_installer_source_snapshot_rename_failed")


def successor_runtime_controller_manifest_from_bytes(
    *,
    release_revision: str,
    assets: Mapping[str, bytes],
) -> Mapping[str, Any]:
    files = []
    for source_relative, (target_relative, mode) in sorted(
        _SUCCESSOR_RUNTIME_CONTROLLER_ASSETS.items()
    ):
        raw = assets.get(source_relative)
        if not isinstance(raw, bytes):
            _fail("rotation_stager_installer_controller_manifest_invalid")
        files.append({
            "path": target_relative,
            "mode": f"{mode:04o}",
            "size": len(raw),
            "sha256": _sha256(raw),
        })
    unsigned = {
        "schema": SUCCESSOR_RUNTIME_CONTROLLER_MANIFEST_SCHEMA,
        "release_revision": release_revision,
        "directories": ["gateway", "scripts", "scripts/canary"],
        "files": files,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {
        **unsigned,
        "manifest_sha256": _sha256(_canonical(unsigned)),
    }


def _successor_runtime_controller_manifest(
    *,
    source_root: Path,
    release_revision: str,
) -> Mapping[str, Any]:
    return successor_runtime_controller_manifest_from_bytes(
        release_revision=release_revision,
        assets={
            source_relative: _git(
                source_root,
                "cat-file",
                "blob",
                f"{release_revision}:{source_relative}",
            )
            for source_relative in _SUCCESSOR_RUNTIME_CONTROLLER_ASSETS
        },
    )


def _publish_exact(
    path: Path,
    raw: bytes,
    *,
    mode: int,
    expected_uid: int,
    expected_gid: int,
) -> bool:
    try:
        state = os.lstat(path)
    except FileNotFoundError:
        state = None
    except OSError as exc:
        _fail("rotation_stager_installer_target_invalid", exc)
    if state is not None:
        if (
            not stat.S_ISREG(state.st_mode)
            or stat.S_ISLNK(state.st_mode)
            or state.st_uid != expected_uid
            or state.st_gid != expected_gid
            or state.st_nlink != 1
            or stat.S_IMODE(state.st_mode) != mode
        ):
            _fail("rotation_stager_installer_target_conflict")
        try:
            existing = path.read_bytes()
        except OSError as exc:
            _fail("rotation_stager_installer_target_invalid", exc)
        if existing != raw:
            _fail("rotation_stager_installer_target_conflict")
        return False
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                _fail("rotation_stager_installer_write_failed")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, expected_uid, expected_gid)
        os.fsync(descriptor)
    except RotationStagerInstallerError:
        raise
    except OSError as exc:
        _fail("rotation_stager_installer_write_failed", exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return True


def _ensure_directory(path: Path, *, create: bool, production: bool) -> None:
    if create:
        try:
            path.mkdir(mode=0o755, parents=True, exist_ok=True)
        except OSError as exc:
            _fail("rotation_stager_installer_directory_invalid", exc)
    try:
        state = os.lstat(path)
    except OSError as exc:
        _fail("rotation_stager_installer_directory_invalid", exc)
    expected_uid = 0 if production else _read_posix_identity("geteuid")
    expected_gid = 0 if production else _read_posix_identity("getegid")
    if (
        not path.is_absolute()
        or ".." in path.parts
        or not stat.S_ISDIR(state.st_mode)
        or stat.S_ISLNK(state.st_mode)
        or state.st_uid != expected_uid
        or state.st_gid != expected_gid
        or stat.S_IMODE(state.st_mode) & 0o022
    ):
        _fail("rotation_stager_installer_directory_invalid")


def _system_command(argv: Sequence[str]) -> None:
    try:
        completed = subprocess.run(
            tuple(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "HOME": "/nonexistent"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _fail("rotation_stager_installer_foundation_failed", exc)
    if completed.returncode != 0:
        _fail("rotation_stager_installer_foundation_failed")


def _validate_builder_identity() -> None:
    try:
        user = pwd.getpwnam(phase.BUILDER_USER)
        group = grp.getgrnam(phase.BUILDER_GROUP)
    except KeyError as exc:
        _fail("rotation_stager_installer_identity_invalid", exc)
    if (
        user.pw_uid != phase.BUILDER_UID
        or user.pw_gid != phase.BUILDER_GID
        or group.gr_gid != phase.BUILDER_GID
        or user.pw_dir != "/nonexistent"
        or user.pw_shell != "/usr/sbin/nologin"
    ):
        _fail("rotation_stager_installer_identity_invalid")


def _validate_foundation(
    roots: InstallerRoots,
    *,
    root_uid: int,
    root_gid: int,
    builder_gid: int,
) -> None:
    try:
        job = os.lstat(roots.job_root)
        lock = os.lstat(roots.promotion_lock)
    except OSError as exc:
        _fail("rotation_stager_installer_foundation_invalid", exc)
    if (
        not stat.S_ISDIR(job.st_mode)
        or stat.S_ISLNK(job.st_mode)
        or job.st_uid != root_uid
        or job.st_gid != root_gid
        or stat.S_IMODE(job.st_mode) != 0o755
        or not stat.S_ISREG(lock.st_mode)
        or stat.S_ISLNK(lock.st_mode)
        or lock.st_uid != root_uid
        or lock.st_gid != builder_gid
        or lock.st_nlink != 1
        or stat.S_IMODE(lock.st_mode) != 0o440
    ):
        _fail("rotation_stager_installer_foundation_invalid")


def _install_for_test(
    *,
    source_root: Path,
    source_remote: str,
    repository_url: str,
    release_revision: str,
    roots: InstallerRoots,
    production: bool = True,
    command_runner: Callable[[Sequence[str]], None] = _system_command,
    identity_validator: Callable[[], None] = _validate_builder_identity,
    foundation_validator: Callable[[InstallerRoots], None] | None = None,
    revision_qualified: bool = False,
    revision_qualified_v3: bool = False,
    revision_qualified_v4: bool = False,
) -> Mapping[str, Any]:
    revisioned = revision_qualified or revision_qualified_v3 or revision_qualified_v4
    if (
        not isinstance(roots, InstallerRoots)
        or _REVISION.fullmatch(release_revision or "") is None
        or _REMOTE.fullmatch(source_remote or "") is None
        or not isinstance(repository_url, str)
        or not repository_url
        or "\x00" in repository_url
        or not source_root.is_absolute()
        or ".." in source_root.parts
        or type(production) is not bool
        or type(revision_qualified) is not bool
        or type(revision_qualified_v3) is not bool
        or type(revision_qualified_v4) is not bool
        or sum(
            int(item)
            for item in (
                revision_qualified,
                revision_qualified_v3,
                revision_qualified_v4,
            )
        )
        > 1
        or (
            production
            and (
                not sys.platform.startswith("linux")
                or _read_posix_identity("geteuid") != 0
            )
        )
        or any(
            not path.is_absolute() or ".." in path.parts
            for path in (
                roots.library,
                roots.sysusers,
                roots.tmpfiles,
                roots.systemd,
                roots.libexec,
                roots.job_root,
                roots.promotion_lock,
                roots.library_releases,
            )
        )
    ):
        _fail("rotation_stager_installer_contract_invalid")
    if production and roots != InstallerRoots():
        _fail("rotation_stager_installer_contract_invalid")
    top = _line(_git(source_root, "rev-parse", "--show-toplevel"))
    try:
        if Path(top).resolve(strict=True) != source_root.resolve(strict=True):
            _fail("rotation_stager_installer_git_invalid")
    except OSError as exc:
        _fail("rotation_stager_installer_git_invalid", exc)
    urls = tuple(
        item
        for item in _git(source_root, "remote", "get-url", "--all", source_remote)
        .decode("utf-8", errors="strict")
        .splitlines()
        if item
    )
    if urls != (repository_url,):
        _fail("rotation_stager_installer_repository_mismatch")
    commit = _line(
        _git(source_root, "rev-parse", "--verify", f"{release_revision}^{{commit}}")
    )
    tree_oid = _line(_git(source_root, "rev-parse", f"{release_revision}^{{tree}}"))
    if commit != release_revision:
        _fail("rotation_stager_installer_revision_mismatch")
    rotation_raw = _git(
        source_root,
        "cat-file",
        "blob",
        (
            f"{release_revision}:"
            "scripts/canary/production_cutover_unit_input_rotation.py"
        ),
    )
    launcher_raw = _git(
        source_root,
        "cat-file",
        "blob",
        (
            f"{release_revision}:"
            "scripts/canary/production_release_rotation_stager_launcher.py"
        ),
    )
    promoter_raw = _git(
        source_root,
        "cat-file",
        "blob",
        (f"{release_revision}:scripts/canary/production_release_candidate_promoter.py"),
    )
    builder_unit_source = (
        "ops/muncho/release-updater/muncho-release-builder-v3@.service"
        if revision_qualified_v3 or revision_qualified_v4
        else "ops/muncho/release-updater/muncho-release-builder-v2@.service"
        if revision_qualified
        else "ops/muncho/release-updater/muncho-release-builder@.service"
    )
    builder_wrapper_source = (
        "ops/muncho/release-updater/muncho-release-foundation-exec-v3"
        if revision_qualified_v3 or revision_qualified_v4
        else "ops/muncho/release-updater/muncho-release-foundation-exec-v2"
        if revision_qualified
        else "ops/muncho/release-updater/muncho-release-builder-phase"
    )
    builder_unit_raw = _git(
        source_root,
        "cat-file",
        "blob",
        f"{release_revision}:{builder_unit_source}",
    )
    builder_wrapper_raw = _git(
        source_root,
        "cat-file",
        "blob",
        f"{release_revision}:{builder_wrapper_source}",
    )
    successor_raw = b""
    preexec_raw = b""
    successor_wrapper_raw = b""
    successor_runtime_wrapper_raw = b""
    successor_runtime_launcher_raw = b""
    if revision_qualified_v4:
        successor_raw = _git(
            source_root,
            "cat-file",
            "blob",
            (
                f"{release_revision}:"
                "scripts/canary/upstream_sync_rail_successor_rebind.py"
            ),
        )
        preexec_raw = _git(
            source_root,
            "cat-file",
            "blob",
            (
                f"{release_revision}:"
                "scripts/canary/production_successor_rebind_owner_runtime_preexec.py"
            ),
        )
        successor_wrapper_raw = _git(
            source_root,
            "cat-file",
            "blob",
            (
                f"{release_revision}:"
                "ops/muncho/release-updater/muncho-release-foundation-exec-v4"
            ),
        )
        successor_runtime_wrapper_raw = _git(
            source_root,
            "cat-file",
            "blob",
            (
                f"{release_revision}:"
                "ops/muncho/release-updater/"
                "muncho-successor-runtime-foundation-exec"
            ),
        )
        successor_runtime_launcher_raw = _git(
            source_root,
            "cat-file",
            "blob",
            (
                f"{release_revision}:"
                "scripts/canary/"
                "production_successor_rebind_owner_runtime_launcher.py"
            ),
        )
    try:
        rotation_tree = ast.parse(rotation_raw.decode("utf-8", errors="strict"))
        launcher_tree = ast.parse(launcher_raw.decode("utf-8", errors="strict"))
        promoter_tree = ast.parse(promoter_raw.decode("utf-8", errors="strict"))
        successor_tree = (
            ast.parse(successor_raw.decode("utf-8", errors="strict"))
            if revision_qualified_v4
            else None
        )

        def literal_set(tree: ast.AST, name: str) -> frozenset[str]:
            for node in getattr(tree, "body", ()):
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == name
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id == "frozenset"
                    and len(node.value.args) == 1
                    and isinstance(node.value.args[0], ast.Set)
                ):
                    values = {
                        ast.literal_eval(item) for item in node.value.args[0].elts
                    }
                    if all(isinstance(item, str) for item in values):
                        return frozenset(values)
            _fail("rotation_stager_installer_protocol_drift")

        def literal_string(tree: ast.AST, name: str) -> str:
            for node in getattr(tree, "body", ()):
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == name
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    return node.value.value
            _fail("rotation_stager_installer_asset_binding_invalid")

        if literal_set(rotation_tree, "RELEASE_PHASE_ACTIONS") != literal_set(
            launcher_tree, "_PHASE_ACTIONS"
        ):
            _fail("rotation_stager_installer_protocol_drift")
        unit_hash_name = (
            "PRODUCTION_LATCHED_REVISION_BUILDER_UNIT_FRAGMENT_SHA256"
            if revision_qualified_v3 or revision_qualified_v4
            else "PRODUCTION_REVISION_BUILDER_UNIT_FRAGMENT_SHA256"
            if revision_qualified
            else "PRODUCTION_BUILDER_UNIT_FRAGMENT_SHA256"
        )
        wrapper_hash_name = (
            "PRODUCTION_LATCHED_REVISION_BUILDER_WRAPPER_SHA256"
            if revision_qualified_v3 or revision_qualified_v4
            else "PRODUCTION_REVISION_BUILDER_WRAPPER_SHA256"
            if revision_qualified
            else "PRODUCTION_BUILDER_WRAPPER_SHA256"
        )
        if literal_string(
            promoter_tree,
            unit_hash_name,
        ) != _sha256(builder_unit_raw) or literal_string(
            promoter_tree,
            wrapper_hash_name,
        ) != _sha256(builder_wrapper_raw):
            _fail("rotation_stager_installer_asset_binding_invalid")
        if revision_qualified_v4 and (
            successor_tree is None
            or literal_string(
                successor_tree,
                "FOUNDATION_V4_WRAPPER_SHA256",
            )
            != _sha256(successor_wrapper_raw)
            or literal_string(
                successor_tree,
                "PREEXEC_VERIFIER_SHA256",
            )
            != _sha256(preexec_raw)
            or literal_string(
                successor_tree,
                "SUCCESSOR_RUNTIME_FOUNDATION_WRAPPER_SHA256",
            )
            != _sha256(successor_runtime_wrapper_raw)
            or literal_string(
                successor_tree,
                "SUCCESSOR_RUNTIME_FOUNDATION_LAUNCHER_SHA256",
            )
            != _sha256(successor_runtime_launcher_raw)
        ):
            _fail("rotation_stager_installer_asset_binding_invalid")
    except (SyntaxError, UnicodeError, ValueError, TypeError) as exc:
        _fail("rotation_stager_installer_protocol_drift", exc)

    library_root = (
        roots.library_releases / release_revision if revisioned else roots.library
    )
    for root in (
        roots.library_releases if revisioned else roots.library,
        roots.sysusers,
        roots.tmpfiles,
        roots.systemd,
        roots.libexec,
    ):
        _ensure_directory(
            root,
            create=(
                root == (roots.library_releases if revisioned else roots.library)
                or not production
            ),
            production=production,
        )
    library_children = (
        library_root,
        library_root / "scripts",
        library_root / "scripts/canary",
        *(
            (
                library_root / "controller",
                library_root / "controller/gateway",
                library_root / "controller/scripts",
                library_root / "controller/scripts/canary",
            )
            if revision_qualified_v4
            else ()
        ),
    )
    for selected in library_children:
        _ensure_directory(selected, create=True, production=production)

    installed: list[Mapping[str, Any]] = []
    created_count = 0
    selected_assets = (
        {**_REVISION_LIBRARY_ASSETS, **_SUCCESSOR_REBIND_STATIC_ASSETS}
        if revision_qualified_v4
        else {**_REVISION_LIBRARY_ASSETS, **_LATCHED_REVISION_STATIC_ASSETS}
        if revision_qualified_v3
        else {**_REVISION_LIBRARY_ASSETS, **_REVISION_STATIC_ASSETS}
        if revision_qualified
        else _SOURCE_ASSETS
    )
    selected_asset_items = list(sorted(selected_assets.items()))
    if revision_qualified_v4:
        selected_asset_items.extend(
            (
                source_relative,
                (f"controller/{target_relative}", mode),
            )
            for source_relative, (target_relative, mode) in sorted(
                _SUCCESSOR_RUNTIME_CONTROLLER_ASSETS.items()
            )
        )
    for source_relative, (target_relative, mode) in selected_asset_items:
        raw = _git(
            source_root,
            "cat-file",
            "blob",
            f"{release_revision}:{source_relative}",
        )
        target = (
            library_root / target_relative
            if revisioned
            and (
                source_relative in _REVISION_LIBRARY_ASSETS
                or target_relative.startswith("controller/")
            )
            else _target(roots, target_relative)
        )
        created = _publish_exact(
            target,
            raw,
            mode=mode,
            expected_uid=(0 if production else _read_posix_identity("geteuid")),
            expected_gid=(0 if production else _read_posix_identity("getegid")),
        )
        created_count += int(created)
        installed.append({
            "source_relative_path": source_relative,
            "target_path": str(target),
            "mode": f"{mode:04o}",
            "sha256": _sha256(raw),
            "created": created,
        })
    source_snapshot_created = False
    controller_manifest: Mapping[str, Any] | None = None
    if revision_qualified_v4:
        source_snapshot_created = _install_exact_source_snapshot(
            source_root=source_root,
            destination=library_root / "source",
            release_revision=release_revision,
            source_tree_oid=tree_oid,
            expected_uid=(0 if production else _read_posix_identity("geteuid")),
            expected_gid=(0 if production else _read_posix_identity("getegid")),
        )
        controller_manifest = _successor_runtime_controller_manifest(
            source_root=source_root,
            release_revision=release_revision,
        )
        controller_manifest_path = (
            library_root / "controller" / SUCCESSOR_RUNTIME_CONTROLLER_MANIFEST_NAME
        )
        controller_manifest_created = _publish_exact(
            controller_manifest_path,
            _canonical(controller_manifest) + b"\n",
            mode=0o444,
            expected_uid=(0 if production else _read_posix_identity("geteuid")),
            expected_gid=(0 if production else _read_posix_identity("getegid")),
        )
        created_count += int(controller_manifest_created)
        installed.append({
            "source_relative_path": "<generated-controller-manifest>",
            "target_path": str(controller_manifest_path),
            "mode": "0444",
            "sha256": _sha256(_canonical(controller_manifest) + b"\n"),
            "created": controller_manifest_created,
        })
    for selected in reversed(library_children):
        os.chmod(selected, 0o555)
    os.chmod(library_root, 0o555)
    if revisioned:
        # The root-owned namespace must remain traversable and extensible by a
        # later privileged installer so a second exact revision can coexist.
        # Individual revision directories are sealed 0555 above; unprivileged
        # identities still cannot create, replace, or mutate entries here.
        os.chmod(roots.library_releases, 0o755)

    sysusers_path = roots.sysusers / "muncho-release-builder.conf"
    tmpfiles_path = roots.tmpfiles / "muncho-release-builder.conf"
    command_runner(("/usr/bin/systemd-sysusers", str(sysusers_path)))
    identity_validator()
    command_runner(("/usr/bin/systemd-tmpfiles", "--create", str(tmpfiles_path)))
    if foundation_validator is None:
        _validate_foundation(
            roots,
            root_uid=0,
            root_gid=0,
            builder_gid=phase.BUILDER_GID,
        )
    else:
        foundation_validator(roots)

    deterministic_assets = [
        {
            name: item[name]
            for name in ("source_relative_path", "target_path", "mode", "sha256")
        }
        for item in installed
    ]
    unsigned = {
        "schema": (
            SUCCESSOR_REBIND_FOUNDATION_INSTALL_RECEIPT_SCHEMA
            if revision_qualified_v4
            else LATCHED_REVISION_QUALIFIED_INSTALL_RECEIPT_SCHEMA
            if revision_qualified_v3
            else REVISION_QUALIFIED_INSTALL_RECEIPT_SCHEMA
            if revision_qualified
            else INSTALL_RECEIPT_SCHEMA
        ),
        "repository_url": repository_url,
        "source_remote": source_remote,
        "release_revision": release_revision,
        "source_tree_oid": tree_oid,
        "assets": installed,
        "asset_count": len(installed),
        "created_asset_count": created_count,
        "builder_identity": dict(phase._BUILDER_IDENTITY),
        "builder_identity_installed": True,
        "job_root": str(roots.job_root),
        "job_root_owner": (
            "root:root"
            if production
            else (
                f"{_read_posix_identity('geteuid')}:{_read_posix_identity('getegid')}"
            )
        ),
        "job_root_mode": "0755",
        "job_root_installed": True,
        "promotion_lock": str(roots.promotion_lock),
        "promotion_lock_installed": True,
        "systemd_daemon_reload_performed": False,
        "unit_enabled": False,
        "unit_started": False,
        "unit_scheduled": False,
        "activation_performed": False,
        "release_pointer_mutated": False,
        "gateway_mutated": False,
        "data_mutated": False,
        "credentials_mutated": False,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    if revisioned:
        unsigned = {
            **unsigned,
            "foundation_layout": (
                "successor-rebind-revision-qualified-v4"
                if revision_qualified_v4
                else "latched-revision-qualified-v3"
                if revision_qualified_v3
                else "revision-qualified-v2"
            ),
            "foundation_asset_manifest_sha256": _sha256(
                _canonical(deterministic_assets)
            ),
        }
    if revision_qualified_v4:
        assert controller_manifest is not None
        unsigned = {
            **unsigned,
            "successor_runtime_source_root": str(library_root / "source"),
            "successor_runtime_source_snapshot_created": source_snapshot_created,
            "successor_runtime_controller_manifest_sha256": controller_manifest[
                "manifest_sha256"
            ],
            "successor_runtime_controller_manifest_file_sha256": _sha256(
                _canonical(controller_manifest) + b"\n"
            ),
        }
    return {**unsigned, "receipt_sha256": _sha256(_canonical(unsigned))}


def install_rotation_stager_foundation(**kwargs: Any) -> Mapping[str, Any]:
    return _install_for_test(roots=InstallerRoots(), production=True, **kwargs)


def install_revision_qualified_foundation(**kwargs: Any) -> Mapping[str, Any]:
    return _install_for_test(
        roots=InstallerRoots(),
        production=True,
        revision_qualified=True,
        **kwargs,
    )


def install_latched_revision_qualified_foundation(
    **kwargs: Any,
) -> Mapping[str, Any]:
    return _install_for_test(
        roots=InstallerRoots(),
        production=True,
        revision_qualified_v3=True,
        **kwargs,
    )


def install_successor_rebind_foundation(
    **kwargs: Any,
) -> Mapping[str, Any]:
    """Install only exact v4 foundation assets; never activate them."""

    return _install_for_test(
        roots=InstallerRoots(),
        production=True,
        revision_qualified_v4=True,
        **kwargs,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-remote", required=True)
    parser.add_argument("--repository-url", required=True)
    parser.add_argument("--release-revision", required=True)
    generation = parser.add_mutually_exclusive_group()
    generation.add_argument("--revision-qualified", action="store_true")
    generation.add_argument("--revision-qualified-v3", action="store_true")
    generation.add_argument("--revision-qualified-v4", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        installer = (
            install_successor_rebind_foundation
            if arguments.revision_qualified_v4
            else install_latched_revision_qualified_foundation
            if arguments.revision_qualified_v3
            else install_revision_qualified_foundation
            if arguments.revision_qualified
            else install_rotation_stager_foundation
        )
        receipt = installer(
            source_root=arguments.source_root,
            source_remote=arguments.source_remote,
            repository_url=arguments.repository_url,
            release_revision=arguments.release_revision,
        )
    except (OSError, RotationStagerInstallerError):
        print(
            '{"error_code":"rotation_stager_installation_failed","ok":false}',
            file=sys.stderr,
        )
        return 2
    print(_canonical(receipt).decode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "INSTALL_RECEIPT_SCHEMA",
    "LATCHED_REVISION_QUALIFIED_INSTALL_RECEIPT_SCHEMA",
    "REVISION_QUALIFIED_INSTALL_RECEIPT_SCHEMA",
    "SUCCESSOR_REBIND_FOUNDATION_INSTALL_RECEIPT_SCHEMA",
    "InstallerRoots",
    "RotationStagerInstallerError",
    "install_latched_revision_qualified_foundation",
    "install_revision_qualified_foundation",
    "install_rotation_stager_foundation",
    "install_successor_rebind_foundation",
]
