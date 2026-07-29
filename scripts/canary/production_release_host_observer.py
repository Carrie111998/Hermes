#!/usr/bin/env python3
"""Read-only, fail-closed host observation for Stage-C release updates.

The consumer inventory contract is deliberately pure.  This module is its
Linux/root collection boundary.  It takes two matching systemd snapshots
around a quiescent ``/proc`` scan, securely opens every observed unit
fragment/drop-in, and passes the resulting observations to the pure validator.

No process environment is read, and the systemd effective ``Environment``
property is deliberately not requested.  Raw unit/process evidence is used
only in-process; the returned receipt contains bounded metadata and evidence
digests, not command lines, unit contents, or other potentially sensitive
values.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping, NoReturn, Protocol, Sequence

from scripts.canary import production_release_consumer_inventory as inventory


RECEIPT_SCHEMA = "muncho.production-release-host-observation-receipt.v1"
SYSTEMCTL = "/usr/bin/systemctl"
PROC_ROOT = "/proc"
UNIT_FILE_ROOTS = (
    "/etc/systemd/system.control",
    "/etc/systemd/system",
    "/etc/systemd/system.attached",
    "/run/systemd/system.control",
    "/run/systemd/transient",
    "/run/systemd/system",
    "/run/systemd/system.attached",
    "/run/systemd/generator",
    "/run/systemd/generator.early",
    "/run/systemd/generator.late",
    "/usr/lib/systemd/system",
    "/usr/local/lib/systemd/system",
)

MAX_COMMAND_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_SYSTEMCTL_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_UNIT_FILE_BYTES = 4 * 1024 * 1024
MAX_UNIT_COUNT = 8192
MAX_SHOW_UNITS = 256
MAX_PROC_COUNT = 1_000_000
MAX_PROC_STAT_BYTES = 64 * 1024
MAX_PROC_CGROUP_BYTES = 64 * 1024
MAX_PROC_CMDLINE_BYTES = 1024 * 1024
MAX_PROC_MAPS_BYTES = 32 * 1024 * 1024
MAX_PROC_MAP_LINES = 262_144
MAX_PROC_FDS = 65_536
MAX_LINK_BYTES = 65_536

_FIXED_ENV = MappingProxyType({
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
})
_UNIT_NAME = re.compile(r"^[A-Za-z0-9_.@:-]+\.(?:service|socket|timer)$")
_SYSTEMD_UNIT_NAME = re.compile(
    r"^(?:[A-Za-z0-9_.@:-]|\\x[0-9a-fA-F]{2})+"
    r"\.(?:service|socket|timer)$"
)
_CGROUP_SERVICE_NAME = re.compile(r"^(?:[A-Za-z0-9_.@:-]|\\x[0-9a-fA-F]{2})+\.service$")
_PROPERTY_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
_UNIT_FILE_STATE = re.compile(r"^[a-z][a-z-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_BOOT_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)
_UNREAD_EFFECTIVE_PROPERTIES = frozenset({"Environment"})
_SYSTEMD_PROPERTIES = (
    "Id",
    *sorted(inventory.SYSTEMD_RELEASE_REF_PROPERTIES - _UNREAD_EFFECTIVE_PROPERTIES),
)
_COMMON_REQUIRED_SHOW_PROPERTIES = frozenset({
    "Id",
    "Names",
    "LoadState",
    "ActiveState",
    "SubState",
    "UnitFileState",
    "FragmentPath",
    "DropInPaths",
    "NeedDaemonReload",
    "Requires",
    "Wants",
    "BindsTo",
    "PartOf",
    "Before",
    "After",
    "TriggeredBy",
    "Triggers",
})
_KIND_REQUIRED_SHOW_PROPERTIES = MappingProxyType({
    "service": frozenset({"Sockets"}),
    "socket": frozenset({"Service"}),
    "timer": frozenset({"Unit"}),
})
_RELATIONSHIP_PROPERTIES = frozenset({
    "Names",
    "Requires",
    "Wants",
    "BindsTo",
    "PartOf",
    "Before",
    "After",
    "TriggeredBy",
    "Triggers",
    "Unit",
    "Service",
    "Sockets",
})
_SHOW_PROPERTY_ARGUMENT = "--property=" + ",".join(_SYSTEMD_PROPERTIES)
_LIST_UNIT_FILES_ARGV = (
    SYSTEMCTL,
    "list-unit-files",
    "--all",
    "--no-legend",
    "--no-pager",
    "--plain",
    "--full",
    "--type=service",
    "--type=socket",
    "--type=timer",
)
_LIST_UNITS_ARGV = (
    SYSTEMCTL,
    "list-units",
    "--all",
    "--no-legend",
    "--no-pager",
    "--plain",
    "--full",
    "--type=service",
    "--type=socket",
    "--type=timer",
)
_SHOW_PREFIX = (
    SYSTEMCTL,
    "show",
    "--no-pager",
    "--full",
    _SHOW_PROPERTY_ARGUMENT,
    "--",
)


class ProductionReleaseHostObserverError(RuntimeError):
    """Stable, non-secret collection failure."""

    def __init__(self, code: str, subject: str | None = None) -> None:
        self.code = code
        self.subject = subject
        super().__init__(code if subject is None else f"{code}:{subject}")


def _fail(code: str, subject: str | None = None) -> NoReturn:
    raise ProductionReleaseHostObserverError(code, subject)


def _valid_unit_name(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value.encode("ascii", errors="ignore")) == len(value)
        and 0 < len(value.encode("ascii")) <= 255
        and _UNIT_NAME.fullmatch(value) is not None
    )


def _valid_systemd_unit_name(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value.encode("ascii", errors="ignore")) == len(value)
        and 0 < len(value.encode("ascii")) <= 255
        and _SYSTEMD_UNIT_NAME.fullmatch(value) is not None
    )


def _valid_cgroup_service_name(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value.encode("ascii", errors="ignore")) == len(value)
        and 0 < len(value.encode("ascii")) <= 255
        and _CGROUP_SERVICE_NAME.fullmatch(value) is not None
    )


@dataclass(frozen=True)
class CommandResult:
    """Bounded command result returned by an injected command seam."""

    stdout: bytes
    stderr: bytes = b""
    returncode: int = 0


class CommandRunner(Protocol):
    """Read-only command seam used by tests and the production runner."""

    def run(self, argv: tuple[str, ...]) -> CommandResult: ...


class UnitFileReader(Protocol):
    """Stable-open unit-file seam."""

    def read(self, path: str) -> bytes: ...


@dataclass(frozen=True)
class CollectedProcess:
    """One start-time-fenced process observation."""

    observation: inventory.ProcessObservation
    start_time_ticks: int


class ProcSource(Protocol):
    """Process snapshot seam.  Implementations must never read environ."""

    def boot_id(self) -> str: ...

    def allocation_fence(self) -> int: ...

    def identities(self) -> Mapping[int, int]: ...

    def observe(self, pid: int, start_time_ticks: int) -> CollectedProcess: ...


@dataclass(frozen=True)
class HostObservationResult:
    """Validated in-memory evidence plus its secret-free receipt."""

    unit_observations: tuple[inventory.UnitObservation, ...]
    process_observations: tuple[inventory.ProcessObservation, ...]
    validation: inventory.InventoryValidationResult
    receipt: Mapping[str, Any]


@dataclass(frozen=True)
class _SystemdSnapshot:
    observations: tuple[inventory.UnitObservation, ...]
    enumerated_names: tuple[str, ...]
    canonical_names: tuple[str, ...]
    alias_name_count: int
    inert_masked_name_count: int
    non_runnable_template_name_count: int
    incompatible_unrelated_unit_count: int
    sha256: str


@dataclass(frozen=True)
class _ProcessSnapshot:
    selected: tuple[CollectedProcess, ...]
    scanned_process_count: int
    allocation_fence: int
    boot_id: str
    sha256: str


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProductionReleaseHostObserverError(
            "production_release_host_observation_not_canonical"
        ) from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _strict_utf8(raw: bytes, *, code: str) -> str:
    if not isinstance(raw, bytes):
        _fail(code)
    try:
        value = raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ProductionReleaseHostObserverError(code) from exc
    if "\x00" in value or "\r" in value:
        _fail(code)
    return value


def _validate_command_result(result: CommandResult) -> bytes:
    if (
        not isinstance(result, CommandResult)
        or not isinstance(result.stdout, bytes)
        or not isinstance(result.stderr, bytes)
        or type(result.returncode) is not int
        or result.returncode != 0
        or len(result.stdout) > MAX_COMMAND_OUTPUT_BYTES
        or len(result.stderr) > MAX_COMMAND_OUTPUT_BYTES
    ):
        _fail("production_release_host_systemctl_failed")
    if result.stderr:
        _fail("production_release_host_systemctl_stderr")
    return result.stdout


def _valid_show_argv(argv: tuple[str, ...]) -> bool:
    names = argv[len(_SHOW_PREFIX) :]
    return (
        argv[: len(_SHOW_PREFIX)] == _SHOW_PREFIX
        and 0 < len(names) <= MAX_SHOW_UNITS
        and tuple(sorted(names)) == names
        and len(set(names)) == len(names)
        and all(_valid_systemd_unit_name(name) for name in names)
    )


class _ProductionCommandRunner:
    """Execute only the observer's three fixed systemctl command shapes."""

    def run(self, argv: tuple[str, ...]) -> CommandResult:
        if argv not in {_LIST_UNIT_FILES_ARGV, _LIST_UNITS_ARGV} and not (
            len(argv) > len(_SHOW_PREFIX) and _valid_show_argv(argv)
        ):
            _fail("production_release_host_systemctl_argv_invalid")
        try:
            completed = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                timeout=20,
                env=dict(_FIXED_ENV),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProductionReleaseHostObserverError(
                "production_release_host_systemctl_failed"
            ) from exc
        return CommandResult(
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
        )


def _absolute_normalized(path: str, *, code: str) -> Path:
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or "\x00" in path
        or "\n" in path
        or "\r" in path
        or ".." in PurePosixPath(path).parts
        or str(PurePosixPath(path)) != path
    ):
        _fail(code)
    return Path(path)


def _list_xattrs(path: Path) -> tuple[str, ...]:
    lister = getattr(os, "listxattr", None)
    if not callable(lister):
        _fail("production_release_host_xattr_inspection_unavailable")
    try:
        values = tuple(sorted(lister(path, follow_symlinks=False)))
    except OSError as exc:
        raise ProductionReleaseHostObserverError(
            "production_release_host_xattr_inspection_failed"
        ) from exc
    if any(not isinstance(value, str) for value in values):
        _fail("production_release_host_xattr_inspection_failed")
    return values


def _same_stat(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_uid,
        left.st_gid,
        left.st_nlink,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_uid,
        right.st_gid,
        right.st_nlink,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _read_fd_bounded(descriptor: int, maximum: int) -> bytes:
    result = bytearray()
    while len(result) <= maximum:
        try:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(result)))
        except OSError as exc:
            raise ProductionReleaseHostObserverError(
                "production_release_host_unit_read_failed"
            ) from exc
        if not chunk:
            break
        result.extend(chunk)
    if len(result) > maximum:
        _fail("production_release_host_unit_oversize")
    return bytes(result)


class _ProductionUnitFileReader:
    """Stable-open root-controlled unit fragments and drop-ins."""

    def _validate_parent_chain(self, path: Path) -> None:
        current = path
        while True:
            try:
                observed = os.lstat(current)
            except OSError as exc:
                raise ProductionReleaseHostObserverError(
                    "production_release_host_unit_parent_unavailable"
                ) from exc
            if (
                stat.S_ISLNK(observed.st_mode)
                or not stat.S_ISDIR(observed.st_mode)
                or observed.st_uid != 0
                or observed.st_gid != 0
                or stat.S_IMODE(observed.st_mode) & 0o022
                or _list_xattrs(current)
            ):
                _fail("production_release_host_unit_parent_untrusted", str(current))
            if current == current.parent:
                return
            current = current.parent

    def read(self, path: str) -> bytes:
        normalized = _absolute_normalized(
            path,
            code="production_release_host_unit_path_invalid",
        )
        if not any(
            normalized == Path(root) or Path(root) in normalized.parents
            for root in UNIT_FILE_ROOTS
        ):
            _fail("production_release_host_unit_root_invalid", path)
        self._validate_parent_chain(normalized.parent)
        try:
            before = os.lstat(normalized)
        except OSError as exc:
            raise ProductionReleaseHostObserverError(
                "production_release_host_unit_unavailable",
                path,
            ) from exc
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != 0
            or before.st_gid != 0
            or stat.S_IMODE(before.st_mode) & 0o022
            or _list_xattrs(normalized)
        ):
            _fail("production_release_host_unit_untrusted", path)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            _fail("production_release_host_no_follow_unavailable")
        flags |= nofollow
        try:
            descriptor = os.open(normalized, flags)
        except OSError as exc:
            raise ProductionReleaseHostObserverError(
                "production_release_host_unit_open_failed",
                path,
            ) from exc
        try:
            opened_before = os.fstat(descriptor)
            if not _same_stat(before, opened_before):
                _fail("production_release_host_unit_path_swapped", path)
            payload = _read_fd_bounded(descriptor, MAX_UNIT_FILE_BYTES)
            opened_after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        try:
            after = os.lstat(normalized)
        except OSError as exc:
            raise ProductionReleaseHostObserverError(
                "production_release_host_unit_path_swapped",
                path,
            ) from exc
        if (
            not _same_stat(opened_before, opened_after)
            or not _same_stat(before, after)
            or _list_xattrs(normalized)
        ):
            _fail("production_release_host_unit_path_swapped", path)
        return payload


def _read_special_file(path: Path, *, maximum: int, code: str) -> bytes:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise ProductionReleaseHostObserverError(code) from exc
    try:
        result = bytearray()
        while len(result) <= maximum:
            try:
                chunk = os.read(
                    descriptor,
                    min(64 * 1024, maximum + 1 - len(result)),
                )
            except OSError as exc:
                raise ProductionReleaseHostObserverError(code) from exc
            if not chunk:
                break
            result.extend(chunk)
    finally:
        os.close(descriptor)
    if len(result) > maximum:
        _fail(code)
    return bytes(result)


def _parse_start_time(raw: bytes, *, expected_pid: int) -> int:
    text = _strict_utf8(
        raw,
        code="production_release_host_proc_stat_invalid",
    )
    prefix, separator, suffix = text.rpartition(")")
    pid_token, pid_separator, command = prefix.partition(" ")
    if (
        not separator
        or pid_separator != " "
        or pid_token != str(expected_pid)
        or not command.startswith("(")
    ):
        _fail("production_release_host_proc_stat_invalid")
    words = suffix.strip().split()
    try:
        value = int(words[19])
    except (IndexError, ValueError) as exc:
        raise ProductionReleaseHostObserverError(
            "production_release_host_proc_stat_invalid"
        ) from exc
    if value <= 0:
        _fail("production_release_host_proc_stat_invalid")
    return value


def _decode_proc_link(value: str) -> str:
    try:
        raw = os.fsencode(value)
        decoded = raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ProductionReleaseHostObserverError(
            "production_release_host_proc_link_invalid"
        ) from exc
    if len(raw) > MAX_LINK_BYTES or "\x00" in decoded:
        _fail("production_release_host_proc_link_invalid")
    return decoded


def _parse_cgroup(raw: bytes) -> str | None:
    text = _strict_utf8(
        raw,
        code="production_release_host_proc_cgroup_invalid",
    )
    lines = text.splitlines()
    if len(lines) != 1:
        _fail("production_release_host_proc_cgroup_invalid")
    hierarchy, separator, remainder = lines[0].partition(":")
    controllers, separator_two, path = remainder.partition(":")
    if (
        hierarchy != "0"
        or not separator
        or not separator_two
        or controllers
        or not path.startswith("/")
        or ".." in PurePosixPath(path).parts
    ):
        _fail("production_release_host_proc_cgroup_invalid")
    units = [
        component
        for component in PurePosixPath(path).parts
        if component.endswith(".service")
    ]
    if len(units) > 1 or any(not _valid_cgroup_service_name(unit) for unit in units):
        _fail("production_release_host_proc_cgroup_invalid")
    return units[0] if units else None


def _decode_cmdline(raw: bytes) -> tuple[str, ...]:
    if not raw:
        return ()
    if not raw.endswith(b"\x00"):
        _fail("production_release_host_proc_cmdline_invalid")
    try:
        values = tuple(
            item.decode("utf-8", errors="strict") for item in raw[:-1].split(b"\x00")
        )
    except UnicodeError as exc:
        raise ProductionReleaseHostObserverError(
            "production_release_host_proc_cmdline_invalid"
        ) from exc
    if len(values) > 4096 or any(
        not value or "\x00" in value or len(value.encode("utf-8")) > MAX_LINK_BYTES
        for value in values
    ):
        _fail("production_release_host_proc_cmdline_invalid")
    return values


def _decode_maps(raw: bytes) -> tuple[str, ...]:
    text = _strict_utf8(
        raw,
        code="production_release_host_proc_maps_invalid",
    )
    lines = tuple(text.splitlines())
    if len(lines) > MAX_PROC_MAP_LINES:
        _fail("production_release_host_proc_maps_oversize")
    if any(not line or len(line.encode("utf-8")) > MAX_LINK_BYTES for line in lines):
        _fail("production_release_host_proc_maps_invalid")
    return lines


class _LinuxProcSource:
    """Linux ``/proc`` collector with PID allocation and start-time fences."""

    def __init__(self) -> None:
        self._root = Path(PROC_ROOT)

    def _pid_path(self, pid: int, name: str) -> Path:
        return self._root / str(pid) / name

    def _start_time(self, pid: int) -> int:
        return _parse_start_time(
            _read_special_file(
                self._pid_path(pid, "stat"),
                maximum=MAX_PROC_STAT_BYTES,
                code="production_release_host_proc_race",
            ),
            expected_pid=pid,
        )

    def boot_id(self) -> str:
        raw = _read_special_file(
            self._root / "sys/kernel/random/boot_id",
            maximum=128,
            code="production_release_host_boot_id_unavailable",
        )
        value = _strict_utf8(
            raw,
            code="production_release_host_boot_id_invalid",
        ).strip()
        if _BOOT_ID.fullmatch(value) is None:
            _fail("production_release_host_boot_id_invalid")
        return value

    def allocation_fence(self) -> int:
        raw = _read_special_file(
            self._root / "sys/kernel/ns_last_pid",
            maximum=64,
            code="production_release_host_pid_fence_unavailable",
        )
        text = _strict_utf8(
            raw,
            code="production_release_host_pid_fence_invalid",
        ).strip()
        if not text.isdigit():
            _fail("production_release_host_pid_fence_invalid")
        value = int(text)
        if value <= 0:
            _fail("production_release_host_pid_fence_invalid")
        return value

    def identities(self) -> Mapping[int, int]:
        try:
            entries = tuple(os.scandir(self._root))
        except OSError as exc:
            raise ProductionReleaseHostObserverError(
                "production_release_host_proc_scan_failed"
            ) from exc
        numeric = sorted(
            (entry for entry in entries if entry.name.isdigit()),
            key=lambda entry: int(entry.name),
        )
        if len(numeric) > MAX_PROC_COUNT:
            _fail("production_release_host_proc_scan_oversize")
        result: dict[int, int] = {}
        for entry in numeric:
            pid = int(entry.name)
            if pid <= 0 or pid in result:
                _fail("production_release_host_proc_scan_invalid")
            try:
                if not entry.is_dir(follow_symlinks=False):
                    _fail("production_release_host_proc_scan_invalid")
                result[pid] = self._start_time(pid)
            except OSError as exc:
                raise ProductionReleaseHostObserverError(
                    "production_release_host_proc_race",
                    str(pid),
                ) from exc
        return MappingProxyType(result)

    def _readlink(self, pid: int, name: str) -> str | None:
        try:
            return _decode_proc_link(os.readlink(self._pid_path(pid, name)))
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ProductionReleaseHostObserverError(
                "production_release_host_proc_link_unavailable",
                str(pid),
            ) from exc

    def _fds(self, pid: int) -> tuple[str, ...]:
        directory = self._pid_path(pid, "fd")
        try:
            entries = tuple(os.scandir(directory))
        except OSError as exc:
            raise ProductionReleaseHostObserverError(
                "production_release_host_proc_fds_unavailable",
                str(pid),
            ) from exc
        if len(entries) > MAX_PROC_FDS:
            _fail("production_release_host_proc_fds_oversize", str(pid))
        names: list[int] = []
        for entry in entries:
            if not entry.name.isdigit():
                _fail("production_release_host_proc_fds_invalid", str(pid))
            names.append(int(entry.name))
        if len(names) != len(set(names)):
            _fail("production_release_host_proc_fds_invalid", str(pid))
        targets: list[str] = []
        for fd_number in sorted(names):
            try:
                target = os.readlink(directory / str(fd_number))
            except OSError as exc:
                raise ProductionReleaseHostObserverError(
                    "production_release_host_proc_race",
                    str(pid),
                ) from exc
            targets.append(_decode_proc_link(target))
        return tuple(targets)

    def observe(self, pid: int, start_time_ticks: int) -> CollectedProcess:
        if type(pid) is not int or pid <= 0 or type(start_time_ticks) is not int:
            _fail("production_release_host_proc_identity_invalid")
        if self._start_time(pid) != start_time_ticks:
            _fail("production_release_host_proc_pid_reused", str(pid))
        cmdline_raw = _read_special_file(
            self._pid_path(pid, "cmdline"),
            maximum=MAX_PROC_CMDLINE_BYTES,
            code="production_release_host_proc_race",
        )
        maps_raw = _read_special_file(
            self._pid_path(pid, "maps"),
            maximum=MAX_PROC_MAPS_BYTES,
            code="production_release_host_proc_race",
        )
        cgroup_raw = _read_special_file(
            self._pid_path(pid, "cgroup"),
            maximum=MAX_PROC_CGROUP_BYTES,
            code="production_release_host_proc_race",
        )
        cmdline = _decode_cmdline(cmdline_raw)
        maps = _decode_maps(maps_raw)
        cgroup_unit = _parse_cgroup(cgroup_raw)
        fds = self._fds(pid)
        links = {name: self._readlink(pid, name) for name in ("exe", "cwd", "root")}
        missing_links = {name for name, value in links.items() if value is None}
        if missing_links:
            if cmdline or maps or fds or missing_links != {"exe", "cwd", "root"}:
                _fail("production_release_host_proc_race", str(pid))
            links = {"exe": "", "cwd": "", "root": ""}
        cgroup_after = _read_special_file(
            self._pid_path(pid, "cgroup"),
            maximum=MAX_PROC_CGROUP_BYTES,
            code="production_release_host_proc_race",
        )
        if cgroup_after != cgroup_raw or self._start_time(pid) != start_time_ticks:
            _fail("production_release_host_proc_race", str(pid))
        fields: Mapping[str, Any] = MappingProxyType({
            "exe": links["exe"],
            "cwd": links["cwd"],
            "root": links["root"],
            "cmdline": cmdline,
            "maps": maps,
            "fds": fds,
        })
        return CollectedProcess(
            observation=inventory.ProcessObservation(
                pid=pid,
                unit=cgroup_unit,
                fields=fields,
            ),
            start_time_ticks=start_time_ticks,
        )


def _parse_unit_file_listing(raw: bytes) -> Mapping[str, str]:
    text = _strict_utf8(
        raw,
        code="production_release_host_unit_file_listing_invalid",
    )
    result: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line != line.strip() or "\t" in line:
            _fail("production_release_host_unit_file_listing_invalid")
        words = line.split()
        if (
            len(words) not in {2, 3}
            or not _valid_systemd_unit_name(words[0])
            or _UNIT_FILE_STATE.fullmatch(words[1]) is None
            or words[0] in result
        ):
            _fail("production_release_host_unit_file_listing_invalid")
        if (
            len(words) == 3
            and words[2] != "-"
            and _UNIT_FILE_STATE.fullmatch(words[2]) is None
        ):
            _fail("production_release_host_unit_file_listing_invalid")
        result[words[0]] = words[1]
    if not result or len(result) > MAX_UNIT_COUNT:
        _fail("production_release_host_unit_file_listing_incomplete")
    return MappingProxyType(result)


def _parse_loaded_unit_listing(raw: bytes) -> tuple[str, ...]:
    text = _strict_utf8(
        raw,
        code="production_release_host_loaded_unit_listing_invalid",
    )
    result: list[str] = []
    for line in text.splitlines():
        if not line or line != line.strip() or "\t" in line:
            _fail("production_release_host_loaded_unit_listing_invalid")
        words = line.split(maxsplit=4)
        if (
            len(words) != 5
            or not _valid_systemd_unit_name(words[0])
            or any(not value for value in words[1:4])
            or words[0] in result
        ):
            _fail("production_release_host_loaded_unit_listing_invalid")
        result.append(words[0])
    if not result or len(result) > MAX_UNIT_COUNT:
        _fail("production_release_host_loaded_unit_listing_incomplete")
    return tuple(sorted(result))


def _parse_show_blocks(raw: bytes) -> tuple[Mapping[str, str], ...]:
    text = _strict_utf8(
        raw,
        code="production_release_host_systemctl_show_invalid",
    )
    stripped = text.rstrip("\n")
    if not stripped:
        _fail("production_release_host_systemctl_show_incomplete")
    blocks = stripped.split("\n\n")
    parsed: list[Mapping[str, str]] = []
    known = set(_SYSTEMD_PROPERTIES)
    for block in blocks:
        values: dict[str, str] = {}
        for line in block.splitlines():
            name, separator, value = line.partition("=")
            if (
                not separator
                or _PROPERTY_NAME.fullmatch(name) is None
                or name not in known
                or name in values
            ):
                _fail("production_release_host_systemctl_show_invalid")
            values[name] = value
        unit_id = values.get("Id")
        if not _valid_systemd_unit_name(unit_id):
            _fail("production_release_host_systemctl_show_invalid")
        assert isinstance(unit_id, str)
        kind = unit_id.rsplit(".", 1)[1]
        required = (
            _COMMON_REQUIRED_SHOW_PROPERTIES | _KIND_REQUIRED_SHOW_PROPERTIES[kind]
        )
        if not required.issubset(values):
            _fail("production_release_host_systemctl_show_incomplete")
        for property_name in known.difference(values):
            values[property_name] = ""
        if set(values) != known:
            _fail("production_release_host_systemctl_show_incomplete")
        parsed.append(MappingProxyType(values))
    if not parsed or len(parsed) > MAX_UNIT_COUNT:
        _fail("production_release_host_systemctl_show_incomplete")
    return tuple(parsed)


def _property_words(value: str, *, code: str) -> tuple[str, ...]:
    if not isinstance(value, str):
        _fail(code)
    if not value:
        return ()
    words = tuple(value.split())
    if " ".join(words) != value or len(words) != len(set(words)):
        _fail(code)
    return words


def _incompatible_unit_touches_scope(
    *,
    properties: Mapping[str, str],
    files: Mapping[str, bytes],
    catalog: Mapping[str, inventory.ConsumerSpec],
) -> bool:
    combined = (properties, files)
    if inventory.extract_release_references(
        combined
    ) or inventory.contains_compatibility_release_reference(combined):
        return True
    expected_paths = {spec.fragment_path for spec in catalog.values()} | {
        path for spec in catalog.values() for path in spec.drop_in_paths
    }
    observed_paths = set(
        _property_words(
            properties["FragmentPath"],
            code="production_release_host_unit_paths_invalid",
        )
    ) | set(
        _property_words(
            properties["DropInPaths"],
            code="production_release_host_unit_paths_invalid",
        )
    )
    if expected_paths.intersection(observed_paths):
        return True
    relationships: set[str] = set()
    for property_name in _RELATIONSHIP_PROPERTIES:
        relationships.update(
            _property_words(
                properties[property_name],
                code="production_release_host_systemctl_relationship_invalid",
            )
        )
    return bool(relationships.intersection(catalog))


def _show_all(
    runner: CommandRunner,
    names: tuple[str, ...],
) -> tuple[Mapping[str, str], ...]:
    result: list[Mapping[str, str]] = []
    total_bytes = 0
    for offset in range(0, len(names), MAX_SHOW_UNITS):
        chunk = names[offset : offset + MAX_SHOW_UNITS]
        raw = _validate_command_result(runner.run((*_SHOW_PREFIX, *chunk)))
        total_bytes += len(raw)
        if total_bytes > MAX_SYSTEMCTL_SNAPSHOT_BYTES:
            _fail("production_release_host_systemctl_show_oversize")
        result.extend(_parse_show_blocks(raw))
    return tuple(result)


def _unit_observation_digest(
    observations: Sequence[inventory.UnitObservation],
) -> str:
    rows: list[Mapping[str, Any]] = []
    for observation in observations:
        rows.append({
            "name": observation.name,
            "properties": dict(sorted(observation.properties.items())),
            "files": [
                {
                    "path": path,
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
                for path, payload in sorted(observation.files.items())
            ],
        })
    return _sha256(rows)


def _collect_systemd_snapshot(
    runner: CommandRunner,
    file_reader: UnitFileReader,
    *,
    catalog: Mapping[str, inventory.ConsumerSpec],
) -> _SystemdSnapshot:
    unit_files = _parse_unit_file_listing(
        _validate_command_result(runner.run(_LIST_UNIT_FILES_ARGV))
    )
    loaded = _parse_loaded_unit_listing(
        _validate_command_result(runner.run(_LIST_UNITS_ARGV))
    )
    enumerated_names = tuple(sorted(set(unit_files).union(loaded)))
    if not enumerated_names or len(enumerated_names) > MAX_UNIT_COUNT:
        _fail("production_release_host_systemctl_listing_incomplete")
    template_names = tuple(name for name in enumerated_names if "@." in name)
    template_name_set = frozenset(template_names)
    if template_name_set.intersection(catalog):
        _fail("production_release_host_expected_unit_is_template")
    # systemd 252 rejects an uninstantiated ``name@.service`` in ``show``.
    # Such a definition is not a loaded/runnable unit identity; any concrete
    # instance is independently present in one of the two enumerations.
    names = tuple(name for name in enumerated_names if name not in template_name_set)
    if not names:
        _fail("production_release_host_systemctl_listing_incomplete")
    raw_blocks = _show_all(runner, names)

    blocks_by_id: dict[str, Mapping[str, str]] = {}
    observed_aliases: set[str] = set()
    for block in raw_blocks:
        unit_id = block["Id"]
        aliases = set(
            _property_words(
                block["Names"],
                code="production_release_host_systemctl_names_invalid",
            )
        )
        if unit_id not in aliases or any(
            not _valid_systemd_unit_name(alias) for alias in aliases
        ):
            _fail("production_release_host_systemctl_names_invalid", unit_id)
        observed_aliases.update(aliases)
        previous = blocks_by_id.get(unit_id)
        if previous is not None and dict(previous) != dict(block):
            _fail("production_release_host_systemctl_show_ambiguous", unit_id)
        blocks_by_id[unit_id] = block

    missing_names = set(names).difference(observed_aliases)
    unenumerated_aliases = observed_aliases.difference(names)
    if missing_names or unenumerated_aliases:
        subject = sorted(missing_names or unenumerated_aliases)[0]
        _fail("production_release_host_systemctl_show_coverage_invalid", subject)

    observations: list[inventory.UnitObservation] = []
    inert_masked = 0
    incompatible_unrelated = 0
    for unit_id in sorted(blocks_by_id):
        raw_properties = blocks_by_id[unit_id]
        properties = dict(raw_properties)
        properties.pop("Id")
        for property_name in _UNREAD_EFFECTIVE_PROPERTIES:
            properties[property_name] = ""
        properties["Names"] = unit_id
        fragment_words = _property_words(
            properties["FragmentPath"],
            code="production_release_host_unit_paths_invalid",
        )
        drop_ins = _property_words(
            properties["DropInPaths"],
            code="production_release_host_unit_paths_invalid",
        )
        is_inert_masked = (
            unit_id not in catalog
            and properties["LoadState"] == "masked"
            and properties["ActiveState"] == "inactive"
            and fragment_words in {(), ("/dev/null",)}
            and not drop_ins
            and not inventory.extract_release_references(properties)
            and not inventory.contains_compatibility_release_reference(properties)
        )
        if is_inert_masked:
            inert_masked += 1
            continue
        if len(fragment_words) != 1:
            _fail("production_release_host_unit_paths_invalid", unit_id)
        paths = (fragment_words[0], *drop_ins)
        if len(paths) != len(set(paths)):
            _fail("production_release_host_unit_paths_invalid", unit_id)
        files: dict[str, bytes] = {}
        for path in paths:
            payload = file_reader.read(path)
            if not isinstance(payload, bytes) or len(payload) > MAX_UNIT_FILE_BYTES:
                _fail("production_release_host_unit_read_invalid", unit_id)
            files[path] = payload
        if not _valid_unit_name(unit_id):
            if _incompatible_unit_touches_scope(
                properties=properties,
                files=files,
                catalog=catalog,
            ):
                _fail(
                    "production_release_host_incompatible_unit_touches_scope",
                    unit_id,
                )
            incompatible_unrelated += 1
            continue
        observations.append(
            inventory.UnitObservation(
                name=unit_id,
                properties=MappingProxyType(properties),
                files=MappingProxyType(files),
            )
        )

    canonical_names = tuple(observation.name for observation in observations)
    digest = _unit_observation_digest(observations)
    return _SystemdSnapshot(
        observations=tuple(observations),
        enumerated_names=enumerated_names,
        canonical_names=canonical_names,
        alias_name_count=len(names) - len(blocks_by_id),
        inert_masked_name_count=inert_masked,
        non_runnable_template_name_count=len(template_names),
        incompatible_unrelated_unit_count=incompatible_unrelated,
        sha256=digest,
    )


def _process_evidence_mapping(process: CollectedProcess) -> Mapping[str, Any]:
    observation = process.observation
    return {
        "pid": observation.pid,
        "start_time_ticks": process.start_time_ticks,
        "unit": observation.unit,
        "fields": dict(observation.fields),
    }


def _is_selected_process(
    process: CollectedProcess,
    *,
    execution_units: frozenset[str],
) -> bool:
    observation = process.observation
    return (
        observation.unit in execution_units
        or bool(inventory.extract_release_references(observation.fields))
        or inventory.contains_compatibility_release_reference(observation.fields)
    )


def _capture_process_pass(
    source: ProcSource,
    identities: Mapping[int, int],
    *,
    execution_units: frozenset[str],
) -> Mapping[int, CollectedProcess]:
    selected: dict[int, CollectedProcess] = {}
    for pid, start_time in sorted(identities.items()):
        process = source.observe(pid, start_time)
        if (
            not isinstance(process, CollectedProcess)
            or process.start_time_ticks != start_time
            or process.observation.pid != pid
        ):
            _fail("production_release_host_proc_identity_invalid", str(pid))
        if _is_selected_process(process, execution_units=execution_units):
            selected[pid] = process
    return MappingProxyType(selected)


def _process_selection_digest(
    selected: Mapping[int, CollectedProcess],
) -> str:
    return _sha256([
        _process_evidence_mapping(selected[pid]) for pid in sorted(selected)
    ])


def _collect_process_snapshot(
    source: ProcSource,
    *,
    catalog: Mapping[str, inventory.ConsumerSpec],
) -> _ProcessSnapshot:
    boot_before = source.boot_id()
    if not isinstance(boot_before, str) or _BOOT_ID.fullmatch(boot_before) is None:
        _fail("production_release_host_boot_id_invalid")
    fence_before = source.allocation_fence()
    identities_before = source.identities()
    if (
        type(fence_before) is not int
        or fence_before <= 0
        or not isinstance(identities_before, Mapping)
        or len(identities_before) > MAX_PROC_COUNT
        or any(
            type(pid) is not int or pid <= 0 or type(start) is not int or start <= 0
            for pid, start in identities_before.items()
        )
    ):
        _fail("production_release_host_proc_identity_invalid")
    execution_units = frozenset(
        name for name, spec in catalog.items() if spec.executes_release
    )
    first = _capture_process_pass(
        source,
        identities_before,
        execution_units=execution_units,
    )
    identities_middle = source.identities()
    fence_middle = source.allocation_fence()
    if (
        dict(identities_middle) != dict(identities_before)
        or fence_middle != fence_before
    ):
        _fail("production_release_host_proc_snapshot_raced")
    second = _capture_process_pass(
        source,
        identities_middle,
        execution_units=execution_units,
    )
    identities_after = source.identities()
    fence_after = source.allocation_fence()
    boot_after = source.boot_id()
    if (
        dict(identities_after) != dict(identities_before)
        or fence_after != fence_before
        or boot_after != boot_before
    ):
        _fail("production_release_host_proc_snapshot_raced")
    first_digest = _process_selection_digest(first)
    second_digest = _process_selection_digest(second)
    if first_digest != second_digest:
        _fail("production_release_host_proc_evidence_raced")
    return _ProcessSnapshot(
        selected=tuple(second[pid] for pid in sorted(second)),
        scanned_process_count=len(identities_before),
        allocation_fence=fence_before,
        boot_id=boot_before,
        sha256=second_digest,
    )


def _validation_mapping(
    value: inventory.InventoryValidationResult,
) -> Mapping[str, Any]:
    return {
        "phase": value.phase.value,
        "expected_unit_count": value.expected_unit_count,
        "execution_service_count": value.execution_service_count,
        "long_running_service_count": value.long_running_service_count,
        "startup_oneshot_service_count": value.startup_oneshot_service_count,
        "triggered_oneshot_service_count": (value.triggered_oneshot_service_count),
        "oneshot_service_count": value.oneshot_service_count,
        "trigger_unit_count": value.trigger_unit_count,
        "observed_expected_unit_count": value.observed_expected_unit_count,
        "ignored_unrelated_unit_count": value.ignored_unrelated_unit_count,
        "observed_process_count": value.observed_process_count,
        "unit_release_revision_prefixes": list(value.unit_release_revision_prefixes),
        "process_release_revision_prefixes": list(
            value.process_release_revision_prefixes
        ),
    }


def _build_receipt(
    *,
    phase: inventory.InventoryPhase,
    predecessor_revision: str,
    target_revision: str,
    observed_at_unix_ns: int,
    systemd: _SystemdSnapshot,
    processes: _ProcessSnapshot,
    validation: inventory.InventoryValidationResult,
) -> Mapping[str, Any]:
    payload: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "phase": phase.value,
        "predecessor_revision": predecessor_revision,
        "target_revision": target_revision,
        "observed_at_unix_ns": observed_at_unix_ns,
        "systemd": {
            "enumerated_name_count": len(systemd.enumerated_names),
            "canonical_unit_count": len(systemd.canonical_names),
            "alias_name_count": systemd.alias_name_count,
            "inert_masked_name_count": systemd.inert_masked_name_count,
            "non_runnable_template_name_count": (
                systemd.non_runnable_template_name_count
            ),
            "incompatible_unrelated_unit_count": (
                systemd.incompatible_unrelated_unit_count
            ),
            "enumerated_names_sha256": _sha256(list(systemd.enumerated_names)),
            "canonical_names_sha256": _sha256(list(systemd.canonical_names)),
            "observation_sha256": systemd.sha256,
        },
        "processes": {
            "boot_id": processes.boot_id,
            "allocation_fence": processes.allocation_fence,
            "scanned_process_count": processes.scanned_process_count,
            "selected_process_count": len(processes.selected),
            "observation_sha256": processes.sha256,
            "identities": [
                {
                    "pid": process.observation.pid,
                    "start_time_ticks": process.start_time_ticks,
                    "unit": process.observation.unit,
                    "evidence_sha256": _sha256(_process_evidence_mapping(process)),
                }
                for process in processes.selected
            ],
        },
        "validation": dict(_validation_mapping(validation)),
    }
    payload["receipt_sha256"] = _sha256(payload)
    return MappingProxyType(payload)


def validate_host_observation_receipt(value: Any) -> Mapping[str, Any]:
    """Validate exact shape and self-hash of a host observation receipt."""

    if not isinstance(value, Mapping):
        _fail("production_release_host_receipt_invalid")
    expected = {
        "schema",
        "phase",
        "predecessor_revision",
        "target_revision",
        "observed_at_unix_ns",
        "systemd",
        "processes",
        "validation",
        "receipt_sha256",
    }
    if set(value) != expected or value.get("schema") != RECEIPT_SCHEMA:
        _fail("production_release_host_receipt_invalid")
    digest = value.get("receipt_sha256")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        _fail("production_release_host_receipt_invalid")
    payload = dict(value)
    payload.pop("receipt_sha256")
    if _sha256(payload) != digest:
        _fail("production_release_host_receipt_hash_invalid")
    systemd = value.get("systemd")
    processes = value.get("processes")
    validation = value.get("validation")
    systemd_keys = {
        "enumerated_name_count",
        "canonical_unit_count",
        "alias_name_count",
        "inert_masked_name_count",
        "non_runnable_template_name_count",
        "incompatible_unrelated_unit_count",
        "enumerated_names_sha256",
        "canonical_names_sha256",
        "observation_sha256",
    }
    process_keys = {
        "boot_id",
        "allocation_fence",
        "scanned_process_count",
        "selected_process_count",
        "observation_sha256",
        "identities",
    }
    validation_keys = {
        "phase",
        "expected_unit_count",
        "execution_service_count",
        "long_running_service_count",
        "startup_oneshot_service_count",
        "triggered_oneshot_service_count",
        "oneshot_service_count",
        "trigger_unit_count",
        "observed_expected_unit_count",
        "ignored_unrelated_unit_count",
        "observed_process_count",
        "unit_release_revision_prefixes",
        "process_release_revision_prefixes",
    }
    if (
        value.get("phase") not in {phase.value for phase in inventory.InventoryPhase}
        or not isinstance(value.get("predecessor_revision"), str)
        or _REVISION.fullmatch(value["predecessor_revision"]) is None
        or not isinstance(value.get("target_revision"), str)
        or _REVISION.fullmatch(value["target_revision"]) is None
        or value["predecessor_revision"] == value["target_revision"]
        or value["predecessor_revision"][:12] == value["target_revision"][:12]
        or type(value.get("observed_at_unix_ns")) is not int
        or value["observed_at_unix_ns"] <= 0
        or not isinstance(systemd, Mapping)
        or set(systemd) != systemd_keys
        or not isinstance(processes, Mapping)
        or set(processes) != process_keys
        or not isinstance(validation, Mapping)
        or set(validation) != validation_keys
    ):
        _fail("production_release_host_receipt_invalid")
    assert isinstance(systemd, Mapping)
    assert isinstance(processes, Mapping)
    assert isinstance(validation, Mapping)
    systemd_counts = (
        systemd["enumerated_name_count"],
        systemd["canonical_unit_count"],
        systemd["alias_name_count"],
        systemd["inert_masked_name_count"],
        systemd["non_runnable_template_name_count"],
        systemd["incompatible_unrelated_unit_count"],
    )
    if (
        any(type(count) is not int or count < 0 for count in systemd_counts)
        or systemd["enumerated_name_count"] <= 0
        or systemd["canonical_unit_count"] <= 0
        or systemd["canonical_unit_count"]
        + systemd["alias_name_count"]
        + systemd["inert_masked_name_count"]
        + systemd["non_runnable_template_name_count"]
        + systemd["incompatible_unrelated_unit_count"]
        != systemd["enumerated_name_count"]
        or any(
            not isinstance(systemd[name], str)
            or _SHA256.fullmatch(systemd[name]) is None
            for name in (
                "enumerated_names_sha256",
                "canonical_names_sha256",
                "observation_sha256",
            )
        )
    ):
        _fail("production_release_host_receipt_invalid")
    identities = processes["identities"]
    if (
        not isinstance(processes["boot_id"], str)
        or _BOOT_ID.fullmatch(processes["boot_id"]) is None
        or type(processes["allocation_fence"]) is not int
        or processes["allocation_fence"] <= 0
        or type(processes["scanned_process_count"]) is not int
        or not 0 <= processes["scanned_process_count"] <= MAX_PROC_COUNT
        or type(processes["selected_process_count"]) is not int
        or not 0 <= processes["selected_process_count"] <= MAX_PROC_COUNT
        or processes["selected_process_count"] > processes["scanned_process_count"]
        or not isinstance(processes["observation_sha256"], str)
        or _SHA256.fullmatch(processes["observation_sha256"]) is None
        or not isinstance(identities, list)
        or len(identities) != processes["selected_process_count"]
    ):
        _fail("production_release_host_receipt_invalid")
    previous_pid = 0
    for identity in identities:
        if (
            not isinstance(identity, Mapping)
            or set(identity) != {"pid", "start_time_ticks", "unit", "evidence_sha256"}
            or type(identity["pid"]) is not int
            or identity["pid"] <= previous_pid
            or type(identity["start_time_ticks"]) is not int
            or identity["start_time_ticks"] <= 0
            or (
                identity["unit"] is not None
                and (
                    not isinstance(identity["unit"], str)
                    or not _valid_unit_name(identity["unit"])
                )
            )
            or not isinstance(identity["evidence_sha256"], str)
            or _SHA256.fullmatch(identity["evidence_sha256"]) is None
        ):
            _fail("production_release_host_receipt_invalid")
        previous_pid = identity["pid"]
    count_fields = (
        "expected_unit_count",
        "execution_service_count",
        "long_running_service_count",
        "startup_oneshot_service_count",
        "triggered_oneshot_service_count",
        "oneshot_service_count",
        "trigger_unit_count",
        "observed_expected_unit_count",
        "ignored_unrelated_unit_count",
        "observed_process_count",
    )
    prefix_fields = (
        "unit_release_revision_prefixes",
        "process_release_revision_prefixes",
    )
    if (
        validation["phase"] != value["phase"]
        or validation["expected_unit_count"] != inventory.EXPECTED_UNIT_COUNT
        or validation["execution_service_count"]
        != inventory.EXPECTED_EXECUTION_SERVICE_COUNT
        or validation["long_running_service_count"]
        != inventory.EXPECTED_LONG_RUNNING_SERVICE_COUNT
        or validation["startup_oneshot_service_count"]
        != inventory.EXPECTED_STARTUP_ONESHOT_SERVICE_COUNT
        or validation["triggered_oneshot_service_count"]
        != inventory.EXPECTED_TRIGGERED_ONESHOT_SERVICE_COUNT
        or validation["oneshot_service_count"]
        != inventory.EXPECTED_ONESHOT_SERVICE_COUNT
        or validation["trigger_unit_count"] != inventory.EXPECTED_TRIGGER_UNIT_COUNT
        or validation["observed_expected_unit_count"] != inventory.EXPECTED_UNIT_COUNT
        or any(
            type(validation[name]) is not int or validation[name] < 0
            for name in count_fields
        )
        or validation["observed_process_count"] != processes["selected_process_count"]
        or any(
            not isinstance(validation[name], list)
            or validation[name] != sorted(set(validation[name]))
            or any(
                not isinstance(prefix, str)
                or re.fullmatch(r"[0-9a-f]{12}", prefix) is None
                for prefix in validation[name]
            )
            for name in prefix_fields
        )
    ):
        _fail("production_release_host_receipt_invalid")
    # Round-trip through canonical JSON to reject values with non-JSON types.
    _canonical_bytes(dict(value))
    return MappingProxyType(dict(value))


def _require_production_host() -> None:
    if sys.platform != "linux":
        _fail("production_release_host_requires_linux")
    geteuid = getattr(os, "geteuid", None)
    if not callable(geteuid) or geteuid() != 0:
        _fail("production_release_host_requires_root")
    if str(Path(PROC_ROOT)) != "/proc" or any(
        not root.startswith("/") or ".." in PurePosixPath(root).parts
        for root in UNIT_FILE_ROOTS
    ):
        _fail("production_release_host_fixed_roots_invalid")
    try:
        observed = os.lstat(SYSTEMCTL)
    except OSError as exc:
        raise ProductionReleaseHostObserverError(
            "production_release_host_systemctl_unavailable"
        ) from exc
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or observed.st_uid != 0
        or observed.st_gid != 0
        or stat.S_IMODE(observed.st_mode) & 0o022
        or _list_xattrs(Path(SYSTEMCTL))
    ):
        _fail("production_release_host_systemctl_untrusted")
    _ProductionUnitFileReader()._validate_parent_chain(  # noqa: SLF001
        Path(SYSTEMCTL).parent
    )


def _observe_and_validate_release_host(
    *,
    phase: inventory.InventoryPhase | str,
    predecessor_revision: str,
    target_revision: str,
    command_runner: CommandRunner | None = None,
    unit_file_reader: UnitFileReader | None = None,
    proc_source: ProcSource | None = None,
    production: bool = True,
    observed_at_unix_ns: int | None = None,
    catalog: Mapping[str, inventory.ConsumerSpec] | None = None,
) -> HostObservationResult:
    """Internal collector shared by the closed production and test APIs."""

    try:
        selected_phase = inventory.InventoryPhase(phase)
    except (TypeError, ValueError) as exc:
        raise ProductionReleaseHostObserverError(
            "production_release_host_phase_invalid"
        ) from exc
    if (
        not isinstance(predecessor_revision, str)
        or _REVISION.fullmatch(predecessor_revision) is None
        or not isinstance(target_revision, str)
        or _REVISION.fullmatch(target_revision) is None
        or predecessor_revision == target_revision
        or predecessor_revision[:12] == target_revision[:12]
    ):
        _fail("production_release_host_revision_invalid")
    selected_catalog = (
        inventory.expected_consumer_catalog() if catalog is None else catalog
    )
    if production:
        if (
            command_runner is not None
            or unit_file_reader is not None
            or proc_source is not None
            or catalog is not None
            or observed_at_unix_ns is not None
        ):
            _fail("production_release_host_injection_forbidden")
        _require_production_host()
        command_runner = _ProductionCommandRunner()
        unit_file_reader = _ProductionUnitFileReader()
        proc_source = _LinuxProcSource()
        observed_at_unix_ns = time.time_ns()
    elif (
        command_runner is None
        or unit_file_reader is None
        or proc_source is None
        or observed_at_unix_ns is None
    ):
        _fail("production_release_host_test_seams_required")
    if type(observed_at_unix_ns) is not int or observed_at_unix_ns <= 0:
        _fail("production_release_host_observation_time_invalid")

    assert command_runner is not None
    assert unit_file_reader is not None
    assert proc_source is not None
    systemd_before = _collect_systemd_snapshot(
        command_runner,
        unit_file_reader,
        catalog=selected_catalog,
    )
    processes = _collect_process_snapshot(
        proc_source,
        catalog=selected_catalog,
    )
    systemd_after = _collect_systemd_snapshot(
        command_runner,
        unit_file_reader,
        catalog=selected_catalog,
    )
    if systemd_before != systemd_after:
        _fail("production_release_host_systemd_snapshot_raced")

    process_observations = tuple(process.observation for process in processes.selected)
    try:
        validation = inventory.validate_release_consumer_inventory(
            unit_observations=systemd_after.observations,
            process_observations=process_observations,
            phase=selected_phase,
            predecessor_revision=predecessor_revision,
            target_revision=target_revision,
            catalog=selected_catalog,
        )
    except inventory.ProductionReleaseConsumerInventoryError as exc:
        raise ProductionReleaseHostObserverError(
            "production_release_host_inventory_invalid",
            exc.code,
        ) from exc
    receipt = _build_receipt(
        phase=selected_phase,
        predecessor_revision=predecessor_revision,
        target_revision=target_revision,
        observed_at_unix_ns=observed_at_unix_ns,
        systemd=systemd_after,
        processes=processes,
        validation=validation,
    )
    validate_host_observation_receipt(receipt)
    return HostObservationResult(
        unit_observations=systemd_after.observations,
        process_observations=process_observations,
        validation=validation,
        receipt=receipt,
    )


def observe_and_validate_release_host(
    *,
    phase: inventory.InventoryPhase | str,
    predecessor_revision: str,
    target_revision: str,
) -> HostObservationResult:
    """Collect one production snapshot from fixed Linux/root sources."""

    return _observe_and_validate_release_host(
        phase=phase,
        predecessor_revision=predecessor_revision,
        target_revision=target_revision,
        production=True,
    )


def _observe_and_validate_release_host_for_test(
    *,
    phase: inventory.InventoryPhase | str,
    predecessor_revision: str,
    target_revision: str,
    command_runner: CommandRunner,
    unit_file_reader: UnitFileReader,
    proc_source: ProcSource,
    observed_at_unix_ns: int,
    catalog: Mapping[str, inventory.ConsumerSpec] | None = None,
) -> HostObservationResult:
    """Exercise the collector with explicit non-production evidence seams."""

    return _observe_and_validate_release_host(
        phase=phase,
        predecessor_revision=predecessor_revision,
        target_revision=target_revision,
        command_runner=command_runner,
        unit_file_reader=unit_file_reader,
        proc_source=proc_source,
        production=False,
        observed_at_unix_ns=observed_at_unix_ns,
        catalog=catalog,
    )


__all__ = [
    "CommandResult",
    "CollectedProcess",
    "HostObservationResult",
    "PROC_ROOT",
    "ProductionReleaseHostObserverError",
    "RECEIPT_SCHEMA",
    "SYSTEMCTL",
    "UNIT_FILE_ROOTS",
    "observe_and_validate_release_host",
    "validate_host_observation_receipt",
]
