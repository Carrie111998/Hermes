"""Route-local filters and script transforms for the webhook adapter."""

from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_SCRIPT_TIMEOUT_SECONDS = 30
MAX_SCRIPT_SNAPSHOT_BYTES = 64 * 1024
MAX_SCRIPT_INTERPRETER_SNAPSHOT_BYTES = 256 * 1024 * 1024
MAX_SCRIPT_OUTPUT_STREAM_BYTES = 1024 * 1024
MAX_SCRIPT_OUTPUT_COMBINED_BYTES = 1024 * 1024
MAX_FILTER_FILE_SNAPSHOT_BYTES = 1024 * 1024
_SCRIPT_PIPE_CHUNK_BYTES = 64 * 1024
_SCRIPT_TERMINATE_GRACE_SECONDS = 0.5
_SCRIPT_CWD_CONTRACT = "isolated-empty-home-v1"
_SCRIPT_CWD_ENV_TOKEN = "<isolated-cwd>"
_SCRIPT_ENVIRONMENT_VERSION = 1
_SCRIPT_SOURCE_HANDOFF_FILENAME = ".hermes-webhook-source.py"
MAX_FILTER_REGEX_BYTES = 4 * 1024
MAX_FILTER_REGEX_INPUT_BYTES = 256 * 1024
FILTER_REGEX_TIMEOUT_SECONDS = 0.1
FILTER_REGEX_STARTUP_TIMEOUT_SECONDS = 2.0
MAX_FILTER_NODES = 64
MAX_FILTER_DEPTH = 8
MAX_FILTER_REGEX_NODES = 8
_MISSING = object()
_REGEX_WORKER = """\
import json
import re
import sys

sys.stdout.write("R")
sys.stdout.flush()
pattern, value = json.load(sys.stdin)
try:
    matched = re.search(pattern, value) is not None
except (re.error, RecursionError):
    raise SystemExit(2)
sys.stdout.write("1" if matched else "0")
"""
_PYTHON_SNAPSHOT_LAUNCHER = f"""\
import hashlib
import os
import sys

source_path = {_SCRIPT_SOURCE_HANDOFF_FILENAME!r}
with open(source_path, "rb") as source_file:
    source_bytes = source_file.read()
os.unlink(source_path)
if hashlib.sha256(source_bytes).hexdigest() != sys.argv[1]:
    raise SystemExit("webhook script source handoff digest mismatch")
script_identifier = sys.argv[2]
original_interpreter = sys.argv[3]
sys.argv[:] = [script_identifier]
sys.executable = original_interpreter
if hasattr(sys, "_base_executable"):
    sys._base_executable = original_interpreter
__file__ = script_identifier
exec(compile(source_bytes.decode("utf-8"), __file__, "exec"), globals(), globals())
"""


class WebhookScriptDisposition(str, Enum):
    """Knowledge produced by one route-script attempt."""

    CONTINUE = "continue"
    IGNORED = "ignored"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class WebhookScriptResult:
    """Typed route-script result; failures cannot masquerade as silence."""

    disposition: WebhookScriptDisposition
    payload: Optional[dict] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class WebhookPreparedScript:
    """Exact route-script source and bounded, non-secret spawn contract."""

    path: str
    source: str
    source_sha256: str
    interpreter: str
    interpreter_kind: str
    interpreter_sha256: str
    environment: tuple[tuple[str, str], ...]
    cwd_contract: str
    execution_sha256: str


@dataclass(frozen=True)
class BoundedRegularFileSnapshot:
    """Bytes and descriptor identity captured from one bounded regular file."""

    content: bytes
    stat_result: os.stat_result


class BoundedFileSnapshotTooLarge(OSError):
    """A regular file exceeded its declared snapshot authority."""


class BoundedFileSnapshotNotRegular(OSError):
    """A snapshot path opened as a FIFO, device, directory, or socket."""


class BoundedFileSnapshotChanged(OSError):
    """A regular file changed while its bounded snapshot was being read."""


class BoundedFileSnapshotOutsideRoot(OSError):
    """A snapshot descriptor escaped its resolved path-containment root."""


@dataclass(frozen=True)
class _BoundedProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class _ScriptOutputLimitExceeded(RuntimeError):
    """A route script crossed its hard stdout/stderr authority bound."""


class _ScriptCancellationRequested(RuntimeError):
    """The owner cancelled a route script after its effect boundary."""


class _RegexWorkerStartupTimeout(RuntimeError):
    """The isolated regex worker did not become ready within its startup bound."""


def _captured_script_environment() -> tuple[tuple[str, str], ...]:
    """Build the complete non-secret environment for one route script.

    Webhook scripts are documented as bounded JSON filters/transforms, not as
    a trusted interactive shell.  In particular they must never inherit the
    gateway's live process environment: that environment can contain another
    multiplexed profile's credentials, shell startup hooks, import paths, or
    new capabilities added after a route key was bound.

    Keep the contract deliberately small and deterministic. ``HOME`` is a
    symbolic reference to the fresh execution directory, so home-directory
    lookup cannot rejoin mutable profile state. ``PATH`` is Python's platform
    default rather than the mutable process value. Locale/time values make
    text transforms reproducible. Windows needs its system-root variables to
    create child processes; those non-secret values are captured now and never
    reread at spawn time.
    """

    environment: dict[str, str] = {
        "HOME": _SCRIPT_CWD_ENV_TOKEN,
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.defpath,
        "TZ": "UTC",
    }
    if os.name == "nt":
        # Environment names are case-insensitive on Windows.  Preserve the
        # canonical spelling in the captured contract without forwarding any
        # unrelated process variable.
        process_values = {key.upper(): value for key, value in os.environ.items()}
        for name in ("SYSTEMROOT", "WINDIR"):
            value = process_values.get(name)
            if value:
                environment[name] = value
    return tuple(sorted(environment.items()))


def _prepared_script_argv(
    *,
    interpreter: str,
    interpreter_kind: str,
    source_sha256: str,
    source: str,
) -> tuple[str, ...]:
    """Return the one allowed interpreter invocation for captured source."""

    if len(source_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in source_sha256
    ):
        raise ValueError("prepared script source digest is invalid")
    suffix = ".sh" if interpreter_kind == "bash" else ".py"
    script_identifier = f"hermes-webhook-script:{source_sha256}{suffix}"

    if interpreter_kind == "bash":
        # BASH_ENV is absent from the minimal environment as the primary
        # non-interactive startup-hook fence.  These flags independently keep
        # profile and interactive rc files out of the execution contract.
        return (
            interpreter,
            "--noprofile",
            "--norc",
            "-c",
            source,
            script_identifier,
        )
    if interpreter_kind == "python":
        # -I ignores PYTHON* environment controls and removes the working
        # directory/user site from imports. -S also excludes mutable venv and
        # system site-packages. -X utf8 keeps JSON stdin/stdout deterministic
        # even under the deliberately minimal C locale. The captured source is
        # handed over through a private file that the launcher reads, verifies,
        # and unlinks before user code starts. Keeping source out of argv avoids
        # the per-argument OS limit while retaining synthetic __file__/argv[0].
        return (
            interpreter,
            "-I",
            "-S",
            "-X",
            "utf8",
            "-c",
            _PYTHON_SNAPSHOT_LAUNCHER,
            source_sha256,
            script_identifier,
            interpreter,
        )
    raise ValueError("prepared script interpreter kind is invalid")


def _script_execution_sha256(
    *,
    path: str,
    source_sha256: str,
    interpreter: str,
    interpreter_kind: str,
    interpreter_sha256: str,
    environment: tuple[tuple[str, str], ...],
    cwd_contract: str,
    argv: tuple[str, ...],
) -> str:
    """Digest every non-secret input that selects script capabilities."""

    canonical = json.dumps(
        {
            "v": _SCRIPT_ENVIRONMENT_VERSION,
            "path": path,
            "source_sha256": source_sha256,
            "interpreter": interpreter,
            "interpreter_kind": interpreter_kind,
            "interpreter_sha256": interpreter_sha256,
            "environment": [list(item) for item in environment],
            "cwd_contract": cwd_contract,
            "argv": list(argv),
        },
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _validate_prepared_script_environment(
    environment: tuple[tuple[str, str], ...],
) -> bool:
    """Reject any widened or non-canonical prepared environment."""

    if not isinstance(environment, tuple):
        return False
    allowed = {
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "TZ",
    }
    if os.name == "nt":
        allowed.update({"SYSTEMROOT", "WINDIR"})
    seen: set[str] = set()
    for item in environment:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
            or item[0] not in allowed
            or item[0] in seen
        ):
            return False
        seen.add(item[0])
    values = dict(environment)
    return (
        seen.issuperset({"HOME", "LANG", "LC_ALL", "PATH", "TZ"})
        and values["HOME"] == _SCRIPT_CWD_ENV_TOKEN
        and values["LANG"] == "C"
        and values["LC_ALL"] == "C"
        and values["PATH"] == os.defpath
        and values["TZ"] == "UTC"
        and tuple(sorted(environment)) == environment
    )


def _materialize_isolated_environment(
    environment: tuple[tuple[str, str], ...],
    *,
    isolated_cwd: str,
) -> dict[str, str]:
    """Replace the sole symbolic environment value for one isolated spawn."""

    materialized = dict(environment)
    materialized["HOME"] = isolated_cwd
    return materialized


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _stat_identity(stat_result: os.stat_result) -> tuple[int, ...]:
    """Return metadata that changes on replacement or ordinary mutation."""

    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_mode,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
    )


def _windows_final_path_from_fd(fd: int) -> Path:
    """Resolve a Windows CRT descriptor through its already-open HANDLE."""

    if os.name != "nt":
        raise OSError("Windows descriptor paths are unavailable")
    import ctypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = (
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
    )
    get_final_path.restype = ctypes.c_uint32
    handle = msvcrt.get_osfhandle(fd)
    required = get_final_path(handle, None, 0, 0)
    if required == 0:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(required + 1)
    written = get_final_path(handle, buffer, len(buffer), 0)
    if written == 0 or written >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    final_path = buffer.value
    if final_path.startswith("\\\\?\\UNC\\"):
        final_path = "\\\\" + final_path.removeprefix("\\\\?\\UNC\\")
    elif final_path.startswith("\\\\?\\"):
        final_path = final_path.removeprefix("\\\\?\\")
    return Path(final_path)


def _open_windows_read_locked(path: Path) -> int:
    """Open a Windows file while denying concurrent writes and replacement."""

    if os.name != "nt":
        raise OSError("Windows locked file opens are unavailable")
    path_text = os.fspath(path)
    if "\x00" in path_text:
        raise ValueError("embedded NUL character in path")

    import ctypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int

    generic_read = 0x80000000
    file_share_read = 0x00000001
    open_existing = 3
    file_attribute_normal = 0x00000080
    file_flag_sequential_scan = 0x08000000
    handle = create_file(
        path_text,
        generic_read,
        file_share_read,
        None,
        open_existing,
        file_attribute_normal | file_flag_sequential_scan,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor_flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
        # Ownership of a successfully converted HANDLE transfers to the CRT
        # descriptor and is released by os.close().
        return msvcrt.open_osfhandle(int(handle), descriptor_flags)
    except BaseException:
        close_handle(handle)
        raise


def _open_readonly_descriptor(path: Path, flags: int) -> int:
    if os.name == "nt":
        return _open_windows_read_locked(path)
    return os.open(path, flags)


def _secure_open_beneath_resolved_root(
    path: Path,
    *,
    resolved_root: Path,
    flags: int,
) -> int:
    """Open a resolved path without following a post-resolution escape.

    On POSIX, walk every already-resolved component from the filesystem root
    using ``dir_fd`` plus ``O_NOFOLLOW``. Existing in-root symlinks remain
    supported because callers resolve them before this walk; replacing any
    canonical component with a symlink after resolution is rejected.

    On Windows the opened target is share-locked against writes/deletion and its
    kernel-resolved final path is checked beneath the root. Other platforms use
    a descriptor/path identity fallback; they cannot provide the same race-free
    path walk as POSIX or the same mandatory sharing lock as Windows.
    """

    path = Path(path)
    resolved_root = Path(resolved_root)
    if not path.is_absolute() or not resolved_root.is_absolute():
        raise ValueError("snapshot containment paths must be absolute")
    try:
        relative = path.relative_to(resolved_root)
    except ValueError as exc:
        raise BoundedFileSnapshotOutsideRoot(
            f"snapshot path resolves outside {resolved_root}: {path}"
        ) from exc

    secure_walk_available = (
        os.name == "posix"
        and os.open in getattr(os, "supports_dir_fd", set())
        and bool(getattr(os, "O_DIRECTORY", 0))
        and bool(getattr(os, "O_NOFOLLOW", 0))
    )
    if secure_walk_available:
        directory_flags = (
            os.O_RDONLY
            | int(getattr(os, "O_CLOEXEC", 0))
            | int(getattr(os, "O_DIRECTORY", 0))
            | int(getattr(os, "O_NOFOLLOW", 0))
        )
        anchor = Path(resolved_root.anchor)
        directory_fd = os.open(anchor, directory_flags)
        try:
            root_parts = resolved_root.parts[1:]
            for part in root_parts:
                next_fd = os.open(
                    part,
                    directory_flags,
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = next_fd
            relative_parts = relative.parts
            for part in relative_parts[:-1]:
                next_fd = os.open(
                    part,
                    directory_flags,
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = next_fd
            if not relative_parts:
                return os.dup(directory_fd)
            return os.open(
                relative_parts[-1],
                flags | int(getattr(os, "O_NOFOLLOW", 0)),
                dir_fd=directory_fd,
            )
        finally:
            os.close(directory_fd)

    fd = _open_readonly_descriptor(path, flags)
    try:
        opened_stat = os.fstat(fd)
        if os.name == "nt":
            final_path = _windows_final_path_from_fd(fd)
            try:
                final_path.relative_to(resolved_root)
            except ValueError as exc:
                raise BoundedFileSnapshotOutsideRoot(
                    f"snapshot descriptor is outside {resolved_root}: {final_path}"
                ) from exc
            return fd
        try:
            resolved_after_open = path.resolve(strict=True)
            resolved_after_open.relative_to(resolved_root)
            path_stat = os.stat(resolved_after_open, follow_symlinks=False)
        except (OSError, ValueError) as exc:
            raise BoundedFileSnapshotOutsideRoot(
                f"snapshot descriptor is outside {resolved_root}: {path}"
            ) from exc
        if (
            opened_stat.st_dev,
            opened_stat.st_ino,
        ) != (
            path_stat.st_dev,
            path_stat.st_ino,
        ):
            raise BoundedFileSnapshotChanged(
                f"snapshot path changed while being opened: {path}"
            )
        return fd
    except BaseException:
        os.close(fd)
        raise


def _write_all_to_fd(fd: int, content: bytes) -> None:
    view = memoryview(content)
    offset = 0
    while offset < len(view):
        written = os.write(fd, view[offset:])
        if written <= 0:
            raise OSError("short executable snapshot write")
        offset += written


def _stable_regular_fd_digest(
    fd: int,
    *,
    path: Path,
    copy_fd: Optional[int] = None,
) -> str:
    """Boundedly hash one descriptor, optionally copying the exact bytes."""

    initial_stat = os.fstat(fd)
    if not stat.S_ISREG(initial_stat.st_mode):
        raise BoundedFileSnapshotNotRegular(
            f"interpreter is not a regular file: {path}"
        )
    if initial_stat.st_size > MAX_SCRIPT_INTERPRETER_SNAPSHOT_BYTES:
        raise BoundedFileSnapshotTooLarge(
            "interpreter exceeds "
            f"{MAX_SCRIPT_INTERPRETER_SNAPSHOT_BYTES} byte snapshot limit: {path}"
        )
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    total = 0
    remaining = MAX_SCRIPT_INTERPRETER_SNAPSHOT_BYTES + 1
    while remaining > 0:
        chunk = os.read(fd, min(remaining, 1024 * 1024))
        if not chunk:
            break
        digest.update(chunk)
        if copy_fd is not None:
            _write_all_to_fd(copy_fd, chunk)
        total += len(chunk)
        remaining -= len(chunk)
    if total > MAX_SCRIPT_INTERPRETER_SNAPSHOT_BYTES:
        raise BoundedFileSnapshotTooLarge(
            "interpreter grew beyond "
            f"{MAX_SCRIPT_INTERPRETER_SNAPSHOT_BYTES} byte snapshot limit: {path}"
        )
    final_stat = os.fstat(fd)
    if _stat_identity(initial_stat) != _stat_identity(final_stat) or (
        total != initial_stat.st_size
    ):
        raise BoundedFileSnapshotChanged(
            f"interpreter changed while being hashed: {path}"
        )
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def _open_stable_regular_file_digest(path: Path) -> tuple[int, str]:
    """Open and boundedly hash one regular file, retaining its descriptor."""

    flags = os.O_RDONLY
    for optional_flag in ("O_NONBLOCK", "O_CLOEXEC", "O_BINARY"):
        flags |= int(getattr(os, optional_flag, 0))
    fd = _open_readonly_descriptor(path, flags)
    try:
        return fd, _stable_regular_fd_digest(fd, path=path)
    except BaseException:
        os.close(fd)
        raise


def _stable_regular_file_sha256(path: Path) -> str:
    """Hash one stable regular-file descriptor and close it."""

    fd, digest = _open_stable_regular_file_digest(path)
    os.close(fd)
    return digest


def _verified_interpreter_fd_path(fd: int) -> Optional[str]:
    """Return an executable inherited-fd path where the platform exposes one."""

    if os.name != "posix":
        return None
    descriptor_stat = os.fstat(fd)
    for prefix in ("/proc/self/fd", "/dev/fd"):
        candidate = f"{prefix}/{fd}"
        try:
            candidate_stat = os.stat(candidate)
        except OSError:
            continue
        if (candidate_stat.st_dev, candidate_stat.st_ino) == (
            descriptor_stat.st_dev,
            descriptor_stat.st_ino,
        ):
            return candidate
    return None


def _create_posix_executable_snapshot_fd() -> tuple[int, Optional[str], bool]:
    """Create a private writable destination for an executable snapshot."""

    if os.name != "posix":
        raise OSError("executable snapshots require POSIX")
    if hasattr(os, "memfd_create"):
        memfd_flags = int(getattr(os, "MFD_CLOEXEC", 0)) | int(
            getattr(os, "MFD_ALLOW_SEALING", 0)
        )
        try:
            return os.memfd_create("hermes-webhook-interpreter", memfd_flags), None, True
        except OSError:
            pass
    fd, path = tempfile.mkstemp(prefix="hermes-webhook-interpreter-")
    return fd, path, False


def _snapshot_posix_executable(path: Path, expected_sha256: Optional[str]) -> tuple[int, str]:
    """Copy one verified executable into a private immutable execution inode."""

    source_flags = (
        os.O_RDONLY
        | int(getattr(os, "O_CLOEXEC", 0))
        | int(getattr(os, "O_NONBLOCK", 0))
    )
    source_fd = os.open(path, source_flags)
    snapshot_fd = -1
    snapshot_path: Optional[str] = None
    is_memfd = False
    try:
        snapshot_fd, snapshot_path, is_memfd = _create_posix_executable_snapshot_fd()
        digest = _stable_regular_fd_digest(
            source_fd,
            path=path,
            copy_fd=snapshot_fd,
        )
        if expected_sha256 is not None and digest != expected_sha256:
            raise BoundedFileSnapshotChanged(
                f"interpreter content changed after snapshot: {path}"
            )
        os.fchmod(snapshot_fd, 0o500)
        if is_memfd:
            import fcntl

            seals = (
                int(getattr(fcntl, "F_SEAL_SEAL", 0x0001))
                | int(getattr(fcntl, "F_SEAL_SHRINK", 0x0002))
                | int(getattr(fcntl, "F_SEAL_GROW", 0x0004))
                | int(getattr(fcntl, "F_SEAL_WRITE", 0x0008))
            )
            fcntl.fcntl(
                snapshot_fd,
                int(getattr(fcntl, "F_ADD_SEALS", 1033)),
                seals,
            )
        else:
            assert snapshot_path is not None
            readonly_fd = os.open(
                snapshot_path,
                os.O_RDONLY | int(getattr(os, "O_CLOEXEC", 0)),
            )
            os.close(snapshot_fd)
            snapshot_fd = readonly_fd
        os.lseek(snapshot_fd, 0, os.SEEK_SET)
        return snapshot_fd, digest
    except BaseException:
        if snapshot_fd >= 0:
            os.close(snapshot_fd)
        raise
    finally:
        os.close(source_fd)
        if snapshot_path is not None:
            try:
                os.unlink(snapshot_path)
            except FileNotFoundError:
                pass


_REGEX_INTERPRETER_PATH = sys.executable
_REGEX_INTERPRETER_CAPTURE_PATH = (
    "/proc/self/exe"
    if os.name == "posix" and Path("/proc/self/exe").exists()
    else _REGEX_INTERPRETER_PATH
)


def _capture_regex_interpreter_authority(
    *,
    platform_name: Optional[str] = None,
) -> tuple[int, str, str, tuple[int, ...]]:
    """Capture the startup regex executable, closing provisional FDs on failure."""

    platform = os.name if platform_name is None else platform_name
    interpreter_fd = -1
    try:
        interpreter_fd, interpreter_sha256 = _open_stable_regular_file_digest(
            Path(_REGEX_INTERPRETER_CAPTURE_PATH)
        )
        if platform == "posix":
            runtime_path = str(Path(_REGEX_INTERPRETER_PATH).absolute())
        elif platform == "nt":
            runtime_path = str(_windows_final_path_from_fd(interpreter_fd))
        else:
            runtime_path = _REGEX_INTERPRETER_PATH
        os.set_inheritable(interpreter_fd, False)
        identity = _stat_identity(os.fstat(interpreter_fd))
        return interpreter_fd, interpreter_sha256, runtime_path, identity
    except BaseException:
        if interpreter_fd >= 0:
            os.close(interpreter_fd)
        raise


try:
    # Capture one bounded interpreter authority at module startup. POSIX keeps
    # the already-running image descriptor; Windows keeps its canonical target
    # share-locked against writes and deletion for the gateway lifetime.
    (
        _REGEX_INTERPRETER_FD,
        _REGEX_INTERPRETER_SHA256,
        _REGEX_INTERPRETER_RUNTIME_PATH,
        _REGEX_INTERPRETER_IDENTITY,
    ) = _capture_regex_interpreter_authority()
except (AttributeError, OSError, ValueError):
    _REGEX_INTERPRETER_FD = -1
    _REGEX_INTERPRETER_SHA256: Optional[str] = None
    _REGEX_INTERPRETER_RUNTIME_PATH: Optional[str] = None
    _REGEX_INTERPRETER_IDENTITY: Optional[tuple[int, ...]] = None


def read_bounded_regular_file_snapshot(
    path: Path,
    *,
    max_bytes: int,
    resolved_root: Optional[Path] = None,
) -> BoundedRegularFileSnapshot:
    """Read one descriptor-stable, bounded regular-file snapshot.

    ``O_NONBLOCK`` makes opening a FIFO prompt even when it has no writer.  The
    descriptor is type-checked before the first read, so devices and directories
    cannot masquerade as small files through a zero ``st_size``.  Symlinks remain
    supported for existing profile/config workflows, but their opened target
    must be a regular file.
    """

    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")

    flags = os.O_RDONLY
    for optional_flag in ("O_NONBLOCK", "O_CLOEXEC", "O_BINARY"):
        flags |= int(getattr(os, optional_flag, 0))

    fd = (
        _secure_open_beneath_resolved_root(
            path,
            resolved_root=resolved_root,
            flags=flags,
        )
        if resolved_root is not None
        else _open_readonly_descriptor(path, flags)
    )
    try:
        stat_result = os.fstat(fd)
        if not stat.S_ISREG(stat_result.st_mode):
            raise BoundedFileSnapshotNotRegular(
                f"snapshot path is not a regular file: {path}"
            )
        if stat_result.st_size > max_bytes:
            raise BoundedFileSnapshotTooLarge(
                f"snapshot file exceeds {max_bytes} bytes: {path}"
            )

        def read_once() -> bytes:
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(fd, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
            if len(content) > max_bytes:
                raise BoundedFileSnapshotTooLarge(
                    f"snapshot file grew beyond {max_bytes} bytes: {path}"
                )
            return content

        first_content = read_once()
        middle_stat = os.fstat(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        second_content = read_once()
        final_stat = os.fstat(fd)
        if (
            _stat_identity(middle_stat) != _stat_identity(stat_result)
            or _stat_identity(final_stat) != _stat_identity(stat_result)
            or len(first_content) != stat_result.st_size
            or len(second_content) != stat_result.st_size
            or first_content != second_content
        ):
            raise BoundedFileSnapshotChanged(
                f"snapshot file changed while being read: {path}"
            )
        return BoundedRegularFileSnapshot(second_content, final_stat)
    finally:
        os.close(fd)


def _terminate_script_process(process: subprocess.Popen[bytes]) -> None:
    """Terminate the isolated script process group, escalating when needed."""

    if os.name == "posix":
        if process.returncode is not None:
            # poll()/wait() already reaped the leader. Its numeric PID is no
            # longer a safely reserved process-group identifier.
            return
        # Do not call wait()/poll() during the TERM grace period. Keeping the
        # leader unreaped reserves its PID, so its process-group ID cannot be
        # recycled before the group-wide SIGKILL below.
        leader_is_unreaped = process.returncode is None
        try:
            os.killpg(process.pid, signal.SIGTERM)  # windows-footgun: ok - POSIX-only branch
        except ProcessLookupError:
            pass
        except (OSError, RuntimeError):
            try:
                process.terminate()
            except (OSError, RuntimeError):
                pass
        if leader_is_unreaped:
            time.sleep(_SCRIPT_TERMINATE_GRACE_SECONDS)
        try:
            os.killpg(process.pid, signal.SIGKILL)  # windows-footgun: ok - POSIX-only branch
        except ProcessLookupError:
            pass
        except (OSError, RuntimeError):
            try:
                process.kill()
            except (OSError, RuntimeError):
                pass
        try:
            process.wait(timeout=_SCRIPT_TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            logger.error("[webhook] route script did not exit after forced termination")
        return

    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=_SCRIPT_TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except (OSError, RuntimeError):
        pass
    try:
        process.wait(timeout=_SCRIPT_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        logger.error("[webhook] route script did not exit after forced termination")


def _script_process_has_exited(process: subprocess.Popen[bytes]) -> bool:
    """Observe POSIX leader exit without releasing its PID/process-group ID."""

    if process.returncode is not None:
        return True
    if _can_observe_posix_exit_without_reaping():
        try:
            status = os.waitid(
                os.P_PID,
                process.pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except ChildProcessError:
            return process.poll() is not None
        return status is not None
    return process.poll() is not None


def _can_observe_posix_exit_without_reaping() -> bool:
    return (
        os.name == "posix"
        and hasattr(os, "waitid")
        and hasattr(os, "WNOWAIT")
        and hasattr(os, "P_PID")
    )


def _reap_finished_script_process_group(
    process: subprocess.Popen[bytes],
) -> None:
    """End descendants before reaping a normally exited script leader."""

    if os.name == "posix" and _can_observe_posix_exit_without_reaping():
        # The leader was observed with waitid(WNOWAIT), so its PID still
        # reserves this PGID. Once the authoritative program has exited, any
        # remaining descendant is outside the route-script contract.
        try:
            os.killpg(process.pid, signal.SIGKILL)  # windows-footgun: ok - POSIX-only branch
        except ProcessLookupError:
            pass
        except (OSError, RuntimeError):
            pass
    try:
        process.wait(timeout=_SCRIPT_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _terminate_script_process(process)


def _write_script_source_handoff(isolated_cwd: str, source_bytes: bytes) -> None:
    """Create the private Python source handoff consumed before user code."""

    handoff_path = Path(isolated_cwd) / _SCRIPT_SOURCE_HANDOFF_FILENAME
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    for optional_flag in ("O_CLOEXEC", "O_BINARY"):
        flags |= int(getattr(os, optional_flag, 0))
    fd = os.open(handoff_path, flags, 0o600)
    try:
        view = memoryview(source_bytes)
        offset = 0
        while offset < len(view):
            written = os.write(fd, view[offset:])
            if written <= 0:
                raise OSError("short route script source handoff write")
            offset += written
    finally:
        os.close(fd)


_WINDOWS_PEEK_NAMED_PIPE: Any = None


def _windows_pipe_available(fd: int) -> tuple[int, bool]:
    """Return queued anonymous-pipe bytes and whether its writer is gone."""

    if sys.platform != "win32":
        raise OSError("Windows pipe inspection is unavailable")
    import ctypes
    import msvcrt

    global _WINDOWS_PEEK_NAMED_PIPE
    if _WINDOWS_PEEK_NAMED_PIPE is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        peek_named_pipe = kernel32.PeekNamedPipe
        peek_named_pipe.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        )
        peek_named_pipe.restype = ctypes.c_int
        _WINDOWS_PEEK_NAMED_PIPE = peek_named_pipe

    available = ctypes.c_uint32()
    handle = msvcrt.get_osfhandle(fd)
    if _WINDOWS_PEEK_NAMED_PIPE(
        handle,
        None,
        0,
        None,
        ctypes.byref(available),
        None,
    ):
        return int(available.value), False
    error = ctypes.get_last_error()
    if error in {38, 109, 232, 233}:  # EOF, broken/no-data/disconnected pipe
        return 0, True
    raise ctypes.WinError(error)


def _read_available_script_pipe(
    pipe: Any,
    *,
    nonblocking: bool,
) -> Optional[bytes]:
    """Read one ready chunk without ever parking a route-worker thread."""

    fd = pipe.fileno()
    if nonblocking:
        try:
            return os.read(fd, _SCRIPT_PIPE_CHUNK_BYTES)
        except BlockingIOError:
            return None
    if sys.platform == "win32":
        available, closed = _windows_pipe_available(fd)
        if closed:
            return b""
        if available <= 0:
            return None
        return os.read(fd, min(available, _SCRIPT_PIPE_CHUNK_BYTES))
    if os.name == "posix":
        import select

        readable, _, _ = select.select([fd], [], [], 0)
        if not readable:
            return None
        return os.read(fd, _SCRIPT_PIPE_CHUNK_BYTES)
    raise OSError("safe script pipe polling is unsupported")


def _run_bounded_script_process(
    argv: list[str],
    *,
    input_bytes: bytes,
    timeout_seconds: float,
    cwd: str,
    env: dict[str, str],
    cancellation_event: Optional[threading.Event] = None,
    pass_fds: tuple[int, ...] = (),
) -> _BoundedProcessResult:
    """Run a script with bounded, single-worker output polling."""

    if cancellation_event is not None and cancellation_event.is_set():
        raise _ScriptCancellationRequested("route script was cancelled before spawn")

    popen_kwargs: dict[str, Any] = {}
    if os.name == "posix":
        # A private session lets timeout/output-limit handling terminate helpers
        # that inherited the pipes instead of waiting forever for their EOF.
        popen_kwargs["start_new_session"] = True
        if pass_fds:
            popen_kwargs["pass_fds"] = pass_fds
    elif sys.platform == "win32":
        popen_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    elif pass_fds:
        raise OSError("verified interpreter descriptors are unsupported")
    input_file = tempfile.TemporaryFile(mode="w+b")
    process: Optional[subprocess.Popen[bytes]] = None
    try:
        _write_all_to_fd(input_file.fileno(), input_bytes)
        input_file.flush()
        input_file.seek(0)
        if cancellation_event is not None and cancellation_event.is_set():
            raise _ScriptCancellationRequested(
                "route script was cancelled before spawn"
            )
        process = subprocess.Popen(
            argv,
            stdin=input_file,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
            cwd=cwd,
            env=env,
            **popen_kwargs,
        )
        if process.stdout is None or process.stderr is None:
            _terminate_script_process(process)
            raise OSError("route script output pipes are unavailable")

        pipes_nonblocking = True
        try:
            for pipe in (process.stdout, process.stderr):
                os.set_blocking(pipe.fileno(), False)
        except (AttributeError, OSError, ValueError):
            # Python 3.11 cannot make anonymous Windows pipes nonblocking.
            # PeekNamedPipe (or select on POSIX) gates every subsequent read.
            pipes_nonblocking = False

        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        combined_size = 0
        output_limit = False
        timed_out = False
        cancelled = False
        pipe_error: Optional[Exception] = None
        streams = (("stdout", process.stdout), ("stderr", process.stderr))

        def drain_one(name: str, pipe: Any) -> bool:
            nonlocal combined_size, output_limit, pipe_error
            try:
                chunk = _read_available_script_pipe(
                    pipe,
                    nonblocking=pipes_nonblocking,
                )
            except (OSError, ValueError) as exc:
                pipe_error = exc
                return False
            if chunk is None or chunk == b"":
                return False
            stream_remaining = MAX_SCRIPT_OUTPUT_STREAM_BYTES - len(buffers[name])
            combined_remaining = MAX_SCRIPT_OUTPUT_COMBINED_BYTES - combined_size
            accepted = min(
                len(chunk),
                max(0, stream_remaining),
                max(0, combined_remaining),
            )
            if accepted:
                buffers[name].extend(chunk[:accepted])
                combined_size += accepted
            if accepted != len(chunk):
                output_limit = True
            return True

        deadline = time.monotonic() + timeout_seconds
        leader_exited_normally = False
        while True:
            if cancellation_event is not None and cancellation_event.is_set():
                cancelled = True
                _terminate_script_process(process)
                break

            for name, pipe in streams:
                drain_one(name, pipe)
                if output_limit or pipe_error is not None:
                    break
            if output_limit or pipe_error is not None:
                _terminate_script_process(process)
                break

            if _script_process_has_exited(process):
                leader_exited_normally = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_script_process(process)
                break
            delay = min(0.01, remaining)
            if cancellation_event is None:
                time.sleep(delay)
            else:
                cancellation_event.wait(delay)

        if leader_exited_normally:
            _reap_finished_script_process_group(process)
            # Drain only bytes already queued by the authoritative leader. An
            # escaped descendant retaining a writer handle cannot keep this
            # request or its global worker slot alive.
            while True:
                made_progress = False
                for name, pipe in streams:
                    made_progress = drain_one(name, pipe) or made_progress
                    if output_limit or pipe_error is not None:
                        break
                if not made_progress or output_limit or pipe_error is not None:
                    break

        if cancellation_event is not None and cancellation_event.is_set():
            cancelled = True
        if cancelled:
            raise _ScriptCancellationRequested("route script cancellation requested")
        if output_limit:
            raise _ScriptOutputLimitExceeded(
                "route script output exceeded its byte limit"
            )
        if timed_out:
            raise subprocess.TimeoutExpired(argv, timeout_seconds)
        if pipe_error is not None:
            raise OSError(f"route script pipe failed: {pipe_error}")
        return _BoundedProcessResult(
            returncode=int(process.returncode),
            stdout=bytes(buffers["stdout"]),
            stderr=bytes(buffers["stderr"]),
        )
    finally:
        if process is not None:
            if not _script_process_has_exited(process):
                _terminate_script_process(process)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except (OSError, ValueError):
                        pass
        input_file.close()


def _validate_json_filter_value(
    value: Any,
    active_containers: Optional[set[int]] = None,
) -> None:
    """Reject values that could only acquire regex meaning through ``str``."""

    value_type = type(value)
    if value is None or value_type in {str, bool, int}:
        return
    if value_type is float:
        if not math.isfinite(value):
            raise ValueError("filter regex numeric values must be finite")
        return
    if value_type not in {dict, list}:
        raise TypeError("filter regex values must use JSON-compatible types")

    if active_containers is None:
        active_containers = set()
    identity = id(value)
    if identity in active_containers:
        raise ValueError("filter regex values must not contain cycles")
    active_containers.add(identity)
    try:
        if value_type is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise TypeError("filter regex object keys must be strings")
                _validate_json_filter_value(item, active_containers)
        else:
            for item in value:
                _validate_json_filter_value(item, active_containers)
    finally:
        active_containers.remove(identity)


def _stringify_filter_value(value: Any) -> str:
    if value is _MISSING:
        return ""
    _validate_json_filter_value(value)
    if isinstance(value, (dict, list)):
        # Preserve non-ASCII text so the regex boundary's strict UTF-8 check
        # also rejects lone surrogates nested inside containers.  Escaping them
        # here would turn invalid text into matchable ASCII such as ``\\ud800``.
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return str(value)


def _bounded_regex_search(pattern: str, value: str) -> Optional[bool]:
    """Evaluate a route regex outside the gateway process with hard deadlines."""

    try:
        pattern_size = len(pattern.encode("utf-8"))
        value_size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        logger.warning("[webhook] Filter regex pattern/input must be valid UTF-8")
        return None
    if pattern_size > MAX_FILTER_REGEX_BYTES:
        logger.warning(
            "[webhook] Filter regex exceeds %d UTF-8 bytes",
            MAX_FILTER_REGEX_BYTES,
        )
        return None
    if value_size > MAX_FILTER_REGEX_INPUT_BYTES:
        logger.warning(
            "[webhook] Filter regex input exceeds %d UTF-8 bytes",
            MAX_FILTER_REGEX_INPUT_BYTES,
        )
        return None
    interpreter_fd = -1
    process: Optional[subprocess.Popen[bytes]] = None
    isolated_directory: Optional[tempfile.TemporaryDirectory[str]] = None
    try:
        if (
            _REGEX_INTERPRETER_FD < 0
            or _REGEX_INTERPRETER_SHA256 is None
            or _REGEX_INTERPRETER_IDENTITY is None
        ):
            raise OSError("filter regex interpreter authority is unavailable")
        interpreter_fd = os.dup(_REGEX_INTERPRETER_FD)
        if _stat_identity(os.fstat(interpreter_fd)) != _REGEX_INTERPRETER_IDENTITY:
            raise OSError("filter regex interpreter descriptor identity changed")

        runtime_interpreter = _REGEX_INTERPRETER_RUNTIME_PATH
        run_kwargs: dict[str, Any] = {}
        if os.name == "posix":
            interpreter_fd_path = _verified_interpreter_fd_path(interpreter_fd)
            if interpreter_fd_path is None:
                raise OSError(
                    "verified filter regex interpreter descriptor cannot be executed"
                )
            runtime_interpreter = interpreter_fd_path
            run_kwargs["pass_fds"] = (interpreter_fd,)
        if runtime_interpreter is None:
            raise OSError("filter regex interpreter runtime path is unavailable")

        input_bytes = json.dumps(
            [pattern, value],
            ensure_ascii=False,
        ).encode("utf-8")

        isolated_directory = tempfile.TemporaryDirectory(
            prefix="hermes-webhook-regex-"
        )
        isolated_cwd = isolated_directory.name
        process = subprocess.Popen(
            [
                runtime_interpreter,
                "-I",
                "-S",
                "-X",
                "utf8",
                "-c",
                _REGEX_WORKER,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
            cwd=isolated_cwd,
            env=_materialize_isolated_environment(
                _captured_script_environment(),
                isolated_cwd=isolated_cwd,
            ),
            **run_kwargs,
        )
        if process.stdout is None or process.stderr is None:
            raise OSError("filter regex worker pipes are unavailable")

        stdout_nonblocking = True
        try:
            os.set_blocking(process.stdout.fileno(), False)
        except (AttributeError, OSError, ValueError):
            # Python 3.11 cannot make anonymous Windows pipes nonblocking.
            # The shared pipe helper uses PeekNamedPipe there and select on
            # POSIX, so the startup deadline remains enforceable.
            stdout_nonblocking = False

        startup_deadline = time.monotonic() + FILTER_REGEX_STARTUP_TIMEOUT_SECONDS
        ready = bytearray()
        while ready != b"R":
            chunk = _read_available_script_pipe(
                process.stdout,
                nonblocking=stdout_nonblocking,
            )
            if chunk:
                ready.extend(chunk)
                if ready != b"R":
                    raise OSError("filter regex worker sent an invalid READY frame")
                break
            if process.poll() is not None:
                raise OSError("filter regex worker exited before READY")
            remaining = startup_deadline - time.monotonic()
            if remaining <= 0:
                raise _RegexWorkerStartupTimeout
            time.sleep(min(0.005, remaining))

        if stdout_nonblocking:
            os.set_blocking(process.stdout.fileno(), True)
        stdout, _stderr = process.communicate(
            input=input_bytes,
            timeout=FILTER_REGEX_TIMEOUT_SECONDS,
        )
        returncode = process.returncode
        process = None
    except _RegexWorkerStartupTimeout:
        logger.warning(
            "[webhook] Filter regex worker startup exceeded %.3f seconds",
            FILTER_REGEX_STARTUP_TIMEOUT_SECONDS,
        )
        return None
    except subprocess.TimeoutExpired:
        logger.warning(
            "[webhook] Filter regex match exceeded %.3f seconds",
            FILTER_REGEX_TIMEOUT_SECONDS,
        )
        return None
    except (OSError, ValueError) as exc:
        logger.warning("[webhook] Filter regex worker failed: %s", exc)
        return None
    finally:
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
            except (OSError, RuntimeError):
                pass
            try:
                process.communicate(timeout=_SCRIPT_TERMINATE_GRACE_SECONDS)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        if isolated_directory is not None:
            try:
                isolated_directory.cleanup()
            except OSError as exc:
                logger.warning(
                    "[webhook] Filter regex working directory cleanup failed: %s",
                    exc,
                )
        if interpreter_fd >= 0:
            try:
                os.close(interpreter_fd)
            except OSError:
                pass
    if returncode == 2:
        logger.warning("[webhook] Invalid webhook filter regex")
        return None
    if returncode != 0 or stdout not in {b"0", b"1"}:
        logger.warning(
            "[webhook] Filter regex worker returned status %d",
            returncode,
        )
        return None
    return stdout == b"1"


def _filter_shape_is_bounded(spec: Any) -> bool:
    """Bound recursive filter work before evaluating operator semantics."""

    roots = spec if isinstance(spec, list) else [spec]
    if len(roots) > MAX_FILTER_NODES:
        return False
    stack = [(item, 1) for item in reversed(roots)]
    nodes = 0
    regex_nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_FILTER_NODES or depth > MAX_FILTER_DEPTH:
            return False
        if not isinstance(item, dict):
            continue
        if "regex" in item:
            regex_nodes += 1
            if regex_nodes > MAX_FILTER_REGEX_NODES:
                return False
        for operator in ("all", "any"):
            children = item.get(operator)
            if not isinstance(children, list):
                continue
            if len(children) > MAX_FILTER_NODES - nodes:
                return False
            stack.extend((child, depth + 1) for child in reversed(children))
        nested = item.get("not")
        if isinstance(nested, dict):
            stack.append((nested, depth + 1))
    return True


def _resolve_profile_path(path_value: Any) -> Optional[Path]:
    """Resolve a user path, mapping ~/.hermes to the active profile home."""
    if not isinstance(path_value, str):
        return None
    raw = os.path.expandvars(path_value.strip())
    if not raw:
        return None
    from hermes_constants import get_hermes_home

    hermes_home = get_hermes_home()
    if raw == "~/.hermes":
        return hermes_home
    if raw.startswith("~/.hermes/"):
        return hermes_home / raw.removeprefix("~/.hermes/")
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return hermes_home / path


def _resolve_script_path(script_value: Any) -> tuple[Optional[Path], Optional[str]]:
    """Resolve a route script under HERMES_HOME/scripts."""
    if not isinstance(script_value, str) or not script_value.strip():
        return None, "script path is empty"
    from hermes_constants import get_hermes_home

    try:
        scripts_root = (get_hermes_home() / "scripts").resolve()
        raw_text = os.path.expandvars(script_value.strip())
        if raw_text == "~/.hermes" or raw_text.startswith("~/.hermes/"):
            mapped = _resolve_profile_path(raw_text)
            candidate = mapped.resolve() if mapped is not None else scripts_root
        else:
            raw = Path(raw_text).expanduser()
            candidate = (
                raw.resolve()
                if raw.is_absolute()
                else (scripts_root / raw).resolve()
            )
    except (OSError, RuntimeError, ValueError) as exc:
        return None, f"script path is invalid: {exc}"
    try:
        candidate.relative_to(scripts_root)
    except ValueError:
        return None, f"script path resolves outside {scripts_root}"
    if not candidate.exists():
        return None, f"script not found: {candidate}"
    if not candidate.is_file():
        return None, f"script path is not a file: {candidate}"
    return candidate, None


def _load_filter_file_values(path_value: Any) -> Optional[list[Any]]:
    """Load one compatibility ``in_file`` operand under a finite file cap.

    Authority publication replaces these lookups with frozen values before a
    production request runs.  The compatibility evaluator still needs the
    same regular-file and 1 MiB safety boundary so direct callers cannot block
    on FIFOs/devices or allocate an unbounded file.
    """

    try:
        path = _resolve_profile_path(path_value)
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning("[webhook] filter in_file path is invalid: %s", exc)
        return None
    if path is None:
        return None
    try:
        raw_bytes = read_bounded_regular_file_snapshot(
            path,
            max_bytes=MAX_FILTER_FILE_SNAPSHOT_BYTES,
        ).content
        raw = raw_bytes.decode("utf-8")
    except BoundedFileSnapshotTooLarge:
        logger.warning(
            "[webhook] filter in_file exceeds %d bytes: %s",
            MAX_FILTER_FILE_SNAPSHOT_BYTES,
            path,
        )
        return None
    except UnicodeDecodeError:
        logger.warning("[webhook] filter in_file must be UTF-8: %s", path)
        return None
    except (OSError, ValueError) as exc:
        logger.warning("[webhook] filter in_file read failed for %s: %s", path, exc)
        return None
    try:
        data = json.loads(raw)
    except RecursionError:
        logger.warning("[webhook] filter in_file JSON nesting is too deep: %s", path)
        return None
    except json.JSONDecodeError:
        return [line.strip() for line in raw.splitlines() if line.strip()]
    except ValueError:
        logger.warning("[webhook] filter in_file JSON value is too large: %s", path)
        return None
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return list(data.keys())
    return [data]


class WebhookRouteProcessor:
    """Evaluate declarative filters and optional script transforms."""

    def __init__(
        self,
        *,
        script_timeout_seconds: int = DEFAULT_SCRIPT_TIMEOUT_SECONDS,
    ) -> None:
        self.script_timeout_seconds = max(1, int(script_timeout_seconds))

    def resolve_filter_field(
        self,
        field: Any,
        payload: dict,
        event_type: str,
        headers: Any,
    ) -> Any:
        """Resolve a dotted filter field against payload/event/headers context."""
        if not isinstance(field, str) or not field.strip():
            return _MISSING
        parts = [part for part in field.strip().split(".") if part]
        if not parts:
            return _MISSING
        header_dict = dict(headers or {})
        context = {
            "payload": payload.get("payload", payload),
            "event": event_type,
            "event_type": event_type,
            "headers": header_dict,
        }
        if parts[0] in context:
            value: Any = context[parts[0]]
            parts = parts[1:]
        else:
            value = payload
        for part in parts:
            if isinstance(value, dict):
                value = value.get(part, _MISSING)
            elif isinstance(value, list) and part.isdigit():
                idx = int(part)
                value = value[idx] if 0 <= idx < len(value) else _MISSING
            else:
                return _MISSING
            if value is _MISSING:
                return _MISSING
        return value

    def _evaluate_filter(
        self,
        spec: Any,
        payload: dict,
        event_type: str,
        headers: Any,
    ) -> Optional[bool]:
        """Return a match verdict, or ``None`` for malformed filter syntax.

        Negation needs this third state: treating a malformed nested filter as
        an ordinary non-match would let ``not`` invert configuration failure
        into authorization.
        """
        if not isinstance(spec, dict):
            logger.warning("[webhook] Ignoring invalid filter spec: %r", spec)
            return None

        operators = {
            "all",
            "any",
            "not",
            "exists",
            "missing",
            "equals",
            "not_equals",
            "contains",
            "in",
            "in_file",
            "regex",
        }
        selected = operators.intersection(spec)
        if len(selected) != 1:
            logger.warning(
                "[webhook] filter must select exactly one operator: %r",
                sorted(selected),
            )
            return None

        if "all" in spec:
            items = spec.get("all")
            if not isinstance(items, list) or not items:
                logger.warning("[webhook] 'all' filter must contain items")
                return None
            results = [
                self._evaluate_filter(item, payload, event_type, headers)
                for item in items
            ]
            return None if any(result is None for result in results) else all(results)
        if "any" in spec:
            items = spec.get("any")
            if not isinstance(items, list) or not items:
                logger.warning("[webhook] 'any' filter must contain items")
                return None
            results = [
                self._evaluate_filter(item, payload, event_type, headers)
                for item in items
            ]
            return None if any(result is None for result in results) else any(results)
        if "not" in spec:
            nested = spec.get("not")
            if not isinstance(nested, dict):
                logger.warning("[webhook] 'not' filter must contain an object")
                return None
            nested_result = self._evaluate_filter(nested, payload, event_type, headers)
            return None if nested_result is None else not nested_result

        field = spec.get("field")
        if not isinstance(field, str) or not field.strip():
            logger.warning("[webhook] filter field must be a non-empty string")
            return None

        value = self.resolve_filter_field(field, payload, event_type, headers)

        if "exists" in spec:
            expected = spec.get("exists")
            if not isinstance(expected, bool):
                logger.warning("[webhook] filter 'exists' must be a boolean")
                return None
            exists = value is not _MISSING
            return exists is expected
        if spec.get("missing") is True:
            return value is _MISSING
        if "equals" in spec:
            return value is not _MISSING and value == spec.get("equals")
        if "not_equals" in spec:
            return value is not _MISSING and value != spec.get("not_equals")
        if "contains" in spec:
            needle = spec.get("contains")
            if value is _MISSING:
                return False
            if isinstance(value, (list, tuple, set, dict)):
                return needle in value
            return str(needle) in _stringify_filter_value(value)
        if "in" in spec:
            haystack = spec.get("in")
            if not isinstance(haystack, list):
                logger.warning("[webhook] filter 'in' must contain a list")
                return None
            return value in haystack
        if "in_file" in spec:
            file_values = _load_filter_file_values(spec.get("in_file"))
            return None if file_values is None else value in file_values
        if "regex" in spec:
            if value is _MISSING:
                return False
            pattern = spec.get("regex")
            if not isinstance(pattern, str):
                logger.warning("[webhook] filter 'regex' must be a string")
                return None
            return _bounded_regex_search(
                pattern,
                _stringify_filter_value(value),
            )

        logger.warning("[webhook] Filter spec has no supported operator: %r", spec)
        return None

    def filter_matches(
        self,
        spec: Any,
        payload: dict,
        event_type: str,
        headers: Any,
    ) -> bool:
        """Evaluate one declarative filter; malformed syntax fails closed."""

        if not _filter_shape_is_bounded(spec):
            logger.warning(
                "[webhook] Filter exceeds the %d-node, %d-depth, or %d-regex limit",
                MAX_FILTER_NODES,
                MAX_FILTER_DEPTH,
                MAX_FILTER_REGEX_NODES,
            )
            return False
        try:
            result = self._evaluate_filter(spec, payload, event_type, headers)
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            logger.warning("[webhook] Filter value cannot be evaluated safely: %s", exc)
            return False
        if result is None:
            return False
        return result

    def route_filters_match(
        self,
        route_config: dict,
        payload: dict,
        event_type: str,
        headers: Any,
    ) -> bool:
        if "filters" not in route_config:
            return True
        filters = route_config.get("filters")
        if not _filter_shape_is_bounded(filters):
            logger.warning(
                "[webhook] Route filters exceed the configured complexity limits"
            )
            return False
        if filters == []:
            return True
        if isinstance(filters, dict):
            if not filters:
                logger.warning("[webhook] filters object must not be empty")
                return False
            return self.filter_matches(filters, payload, event_type, headers)
        if not isinstance(filters, list):
            logger.warning("[webhook] filters must be a list or object")
            return False
        return all(
            self.filter_matches(spec, payload, event_type, headers) for spec in filters
        )

    def prepare_route_script(
        self, script_value: Any
    ) -> tuple[Optional[WebhookPreparedScript], Optional[str]]:
        """Snapshot one exact script before any subprocess can start."""

        path, error = _resolve_script_path(script_value)
        if error or path is None:
            return None, error or "script path cannot be resolved"

        from hermes_constants import get_hermes_home

        try:
            scripts_root = (get_hermes_home() / "scripts").resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            return None, f"script root cannot be resolved safely: {exc}"
        try:
            path.relative_to(scripts_root)
        except ValueError:
            return None, f"script path resolves outside {scripts_root}"
        try:
            source_bytes = read_bounded_regular_file_snapshot(
                path,
                max_bytes=MAX_SCRIPT_SNAPSHOT_BYTES,
                resolved_root=scripts_root,
            ).content
        except BoundedFileSnapshotTooLarge:
            return None, (
                f"script exceeds {MAX_SCRIPT_SNAPSHOT_BYTES} byte snapshot limit"
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return None, f"script cannot be read: {exc}"
        if not source_bytes:
            return None, "script is empty"
        try:
            source = source_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return None, "script must be UTF-8"
        if "\x00" in source:
            return None, "script contains a NUL byte"

        suffix = path.suffix.lower()
        if suffix in {".sh", ".bash"}:
            bash = shutil.which("bash", path=os.defpath) or (
                "/bin/bash" if os.path.isfile("/bin/bash") else None
            )
            if bash is None:
                return None, "bash not found"
            interpreter_kind = "bash"
            interpreter_path = Path(bash)
        else:
            interpreter_kind = "python"
            interpreter_path = Path(sys.executable)

        try:
            if interpreter_kind == "python":
                # Keep the venv launcher path: resolving its symlink to the
                # base interpreter loses pyvenv.cfg discovery and therefore
                # the current interpreter's installed packages.
                interpreter = str(interpreter_path.absolute())
                if not Path(interpreter).is_file():
                    raise FileNotFoundError(interpreter)
            else:
                interpreter = str(interpreter_path.resolve(strict=True))
        except (OSError, RuntimeError, ValueError) as exc:
            return None, f"script interpreter is unavailable: {exc}"

        try:
            interpreter_sha256 = _stable_regular_file_sha256(Path(interpreter))
        except (OSError, RuntimeError, ValueError) as exc:
            return None, f"script interpreter cannot be snapshotted: {exc}"

        environment = _captured_script_environment()
        cwd_contract = _SCRIPT_CWD_CONTRACT
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        argv = _prepared_script_argv(
            interpreter=interpreter,
            interpreter_kind=interpreter_kind,
            source_sha256=source_sha256,
            source=source,
        )
        execution_sha256 = _script_execution_sha256(
            path=str(path),
            source_sha256=source_sha256,
            interpreter=interpreter,
            interpreter_kind=interpreter_kind,
            interpreter_sha256=interpreter_sha256,
            environment=environment,
            cwd_contract=cwd_contract,
            argv=argv,
        )

        return (
            WebhookPreparedScript(
                path=str(path),
                source=source,
                source_sha256=source_sha256,
                interpreter=interpreter,
                interpreter_kind=interpreter_kind,
                interpreter_sha256=interpreter_sha256,
                environment=environment,
                cwd_contract=cwd_contract,
                execution_sha256=execution_sha256,
            ),
            None,
        )

    def run_prepared_script(
        self,
        prepared: WebhookPreparedScript,
        payload: dict,
        *,
        cancellation_event: Optional[threading.Event] = None,
    ) -> WebhookScriptResult:
        """Execute only the exact source captured by ``prepare_route_script``."""

        if cancellation_event is not None and cancellation_event.is_set():
            return WebhookScriptResult(
                WebhookScriptDisposition.INDETERMINATE,
                error="script was cancelled after execution started",
            )

        try:
            source_bytes = prepared.source.encode("utf-8")
        except (AttributeError, UnicodeEncodeError):
            return WebhookScriptResult(
                WebhookScriptDisposition.FAILED,
                error="prepared script source is invalid",
            )
        if hashlib.sha256(source_bytes).hexdigest() != prepared.source_sha256:
            return WebhookScriptResult(
                WebhookScriptDisposition.FAILED,
                error="prepared script source does not match its snapshot digest",
            )

        try:
            if prepared.cwd_contract != _SCRIPT_CWD_CONTRACT:
                raise ValueError("prepared script cwd contract is invalid")
            if not _valid_sha256(prepared.interpreter_sha256):
                raise ValueError("prepared script interpreter digest is invalid")
            if not _validate_prepared_script_environment(prepared.environment):
                raise ValueError("prepared script environment is invalid")
            argv = _prepared_script_argv(
                interpreter=prepared.interpreter,
                interpreter_kind=prepared.interpreter_kind,
                source_sha256=prepared.source_sha256,
                source=prepared.source,
            )
            execution_sha256 = _script_execution_sha256(
                path=prepared.path,
                source_sha256=prepared.source_sha256,
                interpreter=prepared.interpreter,
                interpreter_kind=prepared.interpreter_kind,
                interpreter_sha256=prepared.interpreter_sha256,
                environment=prepared.environment,
                cwd_contract=prepared.cwd_contract,
                argv=argv,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            logger.warning("[webhook] prepared script contract is invalid: %s", exc)
            return WebhookScriptResult(
                WebhookScriptDisposition.FAILED,
                error="prepared script execution contract is invalid",
            )
        if execution_sha256 != prepared.execution_sha256:
            return WebhookScriptResult(
                WebhookScriptDisposition.FAILED,
                error=(
                    "prepared script execution contract does not match "
                    "its snapshot digest"
                ),
            )

        path = Path(prepared.path)

        try:
            input_bytes = json.dumps(payload).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
            logger.warning("[webhook] script payload cannot be serialized: %s", exc)
            return WebhookScriptResult(
                WebhookScriptDisposition.FAILED,
                error="script payload cannot be serialized safely",
            )

        try:
            if os.name == "posix":
                if (
                    prepared.interpreter_sha256 == _REGEX_INTERPRETER_SHA256
                    and _REGEX_INTERPRETER_FD >= 0
                    and _REGEX_INTERPRETER_IDENTITY is not None
                ):
                    interpreter_fd = os.dup(_REGEX_INTERPRETER_FD)
                    if (
                        _stat_identity(os.fstat(interpreter_fd))
                        != _REGEX_INTERPRETER_IDENTITY
                    ):
                        os.close(interpreter_fd)
                        raise BoundedFileSnapshotChanged(
                            "retained interpreter descriptor identity changed"
                        )
                    live_interpreter_sha256 = prepared.interpreter_sha256
                else:
                    interpreter_fd, live_interpreter_sha256 = (
                        _snapshot_posix_executable(
                            Path(prepared.interpreter),
                            expected_sha256=prepared.interpreter_sha256,
                        )
                    )
            else:
                interpreter_fd, live_interpreter_sha256 = (
                    _open_stable_regular_file_digest(Path(prepared.interpreter))
                )
        except BoundedFileSnapshotChanged as exc:
            logger.warning("[webhook] prepared script interpreter changed: %s", exc)
            return WebhookScriptResult(
                WebhookScriptDisposition.FAILED,
                error="prepared script interpreter content changed after snapshot",
            )
        except (OSError, ValueError) as exc:
            logger.warning("[webhook] prepared script interpreter is unavailable: %s", exc)
            return WebhookScriptResult(
                WebhookScriptDisposition.FAILED,
                error="prepared script interpreter cannot be verified",
            )
        if live_interpreter_sha256 != prepared.interpreter_sha256:
            os.close(interpreter_fd)
            return WebhookScriptResult(
                WebhookScriptDisposition.FAILED,
                error="prepared script interpreter content changed after snapshot",
            )

        runtime_argv = list(argv)
        inherited_interpreter_fds: tuple[int, ...] = ()
        if os.name == "posix":
            interpreter_fd_path = _verified_interpreter_fd_path(interpreter_fd)
            if interpreter_fd_path is None:
                os.close(interpreter_fd)
                return WebhookScriptResult(
                    WebhookScriptDisposition.FAILED,
                    error="verified interpreter descriptor cannot be executed safely",
                )
            runtime_argv[0] = interpreter_fd_path
            inherited_interpreter_fds = (interpreter_fd,)
        elif os.name == "nt":
            # CreateProcess has no execute-by-handle API. The descriptor was
            # opened without FILE_SHARE_WRITE/FILE_SHARE_DELETE, and its final
            # target path avoids a mutable reparse-point alias. Retain that lock
            # through process creation and execution.
            try:
                runtime_argv[0] = str(_windows_final_path_from_fd(interpreter_fd))
            except (OSError, ValueError) as exc:
                os.close(interpreter_fd)
                logger.warning(
                    "[webhook] verified interpreter path is unavailable: %s",
                    exc,
                )
                return WebhookScriptResult(
                    WebhookScriptDisposition.FAILED,
                    error="verified interpreter descriptor cannot be executed safely",
                )
        else:
            os.close(interpreter_fd)
            interpreter_fd = -1

        try:
            # A fresh 0700 directory has no pre-existing helpers to import or
            # source.  Its random path does not carry authority; the frozen
            # capability is specifically "isolated empty cwd".  Keep it alive
            # until bounded process-group cleanup has completed.
            with tempfile.TemporaryDirectory(
                prefix="hermes-webhook-script-"
            ) as isolated_cwd:
                if prepared.interpreter_kind == "python":
                    _write_script_source_handoff(isolated_cwd, source_bytes)
                execution_environment = _materialize_isolated_environment(
                    prepared.environment,
                    isolated_cwd=isolated_cwd,
                )
                result = _run_bounded_script_process(
                    runtime_argv,
                    input_bytes=input_bytes,
                    timeout_seconds=self.script_timeout_seconds,
                    cwd=isolated_cwd,
                    env=execution_environment,
                    cancellation_event=cancellation_event,
                    pass_fds=inherited_interpreter_fds,
                )
        except _ScriptCancellationRequested:
            logger.warning("[webhook] script cancelled: %s", path)
            return WebhookScriptResult(
                WebhookScriptDisposition.INDETERMINATE,
                error="script was cancelled after execution started",
            )
        except _ScriptOutputLimitExceeded:
            logger.warning("[webhook] script output exceeded its limit: %s", path)
            return WebhookScriptResult(
                WebhookScriptDisposition.INDETERMINATE,
                error="script output exceeded its byte limit after execution started",
            )
        except subprocess.TimeoutExpired:
            logger.warning("[webhook] script timed out: %s", path)
            return WebhookScriptResult(
                WebhookScriptDisposition.INDETERMINATE,
                error="script timed out after execution started",
            )
        except Exception as exc:
            logger.warning("[webhook] script execution failed: %s", exc)
            return WebhookScriptResult(
                WebhookScriptDisposition.INDETERMINATE,
                error=f"script execution failed: {exc}",
            )
        finally:
            if interpreter_fd >= 0:
                try:
                    os.close(interpreter_fd)
                except OSError:
                    pass

        stdout = (result.stdout or b"").decode("utf-8", errors="replace").strip()
        stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        try:
            from agent.redact import redact_sensitive_text

            stdout = redact_sensitive_text(stdout)
            stderr = redact_sensitive_text(stderr)
            if not isinstance(stdout, str) or not isinstance(stderr, str):
                raise TypeError("script output redactor must return text")
        except Exception as exc:
            logger.warning("[webhook] Failed to redact script output: %s", exc)
            return WebhookScriptResult(
                WebhookScriptDisposition.INDETERMINATE,
                error="script output redaction failed after execution started",
            )
        if result.returncode != 0:
            logger.info(
                "[webhook] script outcome indeterminate path=%s code=%s stderr=%s",
                path.name,
                result.returncode,
                stderr[:200],
            )
            return WebhookScriptResult(
                WebhookScriptDisposition.INDETERMINATE,
                error=f"script exited with status {result.returncode}",
            )
        if not stdout or stdout == "[SILENT]":
            return WebhookScriptResult(WebhookScriptDisposition.IGNORED)

        try:
            transformed = json.loads(stdout)
        except (json.JSONDecodeError, RecursionError, ValueError):
            transformed = {**payload, "script_output": stdout}
        if not isinstance(transformed, dict):
            logger.warning("[webhook] script stdout must be a JSON object or text")
            return WebhookScriptResult(
                WebhookScriptDisposition.INDETERMINATE,
                error="script stdout is not a JSON object or text",
            )
        if (
            transformed.get("[SILENT]") is True
            or transformed.get("__hermes_ignore__") is True
        ):
            return WebhookScriptResult(WebhookScriptDisposition.IGNORED)
        return WebhookScriptResult(
            WebhookScriptDisposition.CONTINUE,
            payload=transformed,
        )

    def run_route_script(self, script_value: Any, payload: dict) -> WebhookScriptResult:
        """Compatibility wrapper over exact preflight and execution."""

        prepared, error = self.prepare_route_script(script_value)
        if error or prepared is None:
            logger.warning("[webhook] script preparation failed: %s", error)
            return WebhookScriptResult(
                WebhookScriptDisposition.FAILED,
                error=error or "script path cannot be resolved",
            )
        return self.run_prepared_script(prepared, payload)
