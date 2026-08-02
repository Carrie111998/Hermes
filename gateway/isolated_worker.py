"""Strict AF_UNIX execution boundary for an unprivileged isolated worker.

The protocol in this module is deliberately mechanical.  It does not choose
commands, classify requests, or make task decisions.  A caller supplies an
already model-authored command and an owner-sealed lease; the worker only
validates the fixed transport/session boundary and executes inside that
lease's pre-created workspace.

Network and filesystem namespace isolation are service-manager obligations.
``WorkerPolicy`` requires an attested network-isolated profile and forwards no
ambient environment or credential-file registration into child processes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


PROTOCOL = "muncho.isolated-worker.v1"
REQUEST_SCHEMA = "muncho.isolated-worker.request.v1"
RESPONSE_SCHEMA = "muncho.isolated-worker.response.v1"
PROOF_STATE_SCHEMA = "muncho.isolated-worker.proof-state.v1"
PROOF_RECEIPT_SCHEMA = "muncho.isolated-worker.proof-receipt.v1"
MAX_FRAME_BYTES = 256 * 1024
MAX_COMMAND_BYTES = 64 * 1024
MAX_STDIN_BYTES = 128 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_POLL_CHUNK_BYTES = 64 * 1024
MAX_REQUEST_CACHE = 256
MAX_ACTIVE_CONNECTIONS = 128
MAX_ACTIVE_JOBS_PER_CONNECTION = 64
MAX_ACTIVE_JOBS_PER_LEASE_LIMIT = 64
MAX_LEASES_LIMIT = 1024
MAX_LEASE_TTL_SECONDS = 86_400
MAX_LEASE_QUOTA_BYTES = 16 * 1024 * 1024 * 1024
MAX_LEASE_QUOTA_ENTRIES = 1_000_000
MAX_GLOBAL_QUOTA_BYTES = 16 * 1024 * 1024 * 1024
MAX_GLOBAL_QUOTA_ENTRIES = 2_000_000
DEFAULT_BWRAP_PATH = Path("/usr/bin/bwrap")
VIRTUAL_WORKSPACE_ROOT = Path("/workspace")
HOST_READ_ONLY_ROOT = Path("/opt/hermes-shared")
VIRTUAL_READ_ONLY_ROOT = Path("/opt/hermes-shared")
VIRTUAL_SHELL_PATH = Path("/run/hermes-shell")
FIXED_RUNTIME_ROOTS = (
    Path("/usr"),
    Path("/bin"),
    Path("/lib"),
    Path("/lib64"),
)
_DENIED_READ_ONLY_SOURCE_COMPONENTS = frozenset(
    {".hermes", "credentials", "memory", "memories", "plugins", "secrets", "skills"}
)

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")
_REQUEST_FIELDS = frozenset(
    {"schema", "protocol", "request_id", "lease_id", "operation", "parameters"}
)
_PARAMETER_FIELDS = {
    "exec.start": frozenset({"command", "cwd", "stdin_b64", "timeout_seconds"}),
    "exec.poll": frozenset({"session_id", "wait_milliseconds"}),
    "exec.cancel": frozenset({"session_id"}),
    "proof.status": frozenset(),
    "proof.mark_edited": frozenset({"paths", "observed_generation"}),
}
_PROOF_STATE_FIELDS = frozenset(
    {
        "schema",
        "lease_id",
        "edit_generation",
        "verified_generation",
        "pending_paths",
        "last_verification",
        "applicability",
        "project_root",
        "verify_commands_digest",
        "material_fingerprint",
    }
)
_PROOF_VERIFICATION_FIELDS = frozenset(
    {"canonical_command", "kind", "scope", "status"}
)
_PROOF_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "lease_id",
        "edit_generation",
        "verified_generation",
        "status",
        "mutation_detection",
        "changed_paths",
        "pending_paths",
        "verification",
        "applicability",
        "project_root",
        "verify_commands_digest",
        "material_fingerprint",
    }
)
_PROOF_STATUS_VALUES = frozenset({"unverified", "passed", "failed", "stale"})
_PROOF_APPLICABILITY_VALUES = frozenset(
    {"applicable", "not_applicable", "unknown"}
)
_PROOF_MUTATION_VALUES = frozenset(
    {"status", "explicit", "unchanged", "changed", "unknown"}
)
_PROOF_PRIVATE_DIR = ".hermes-runtime"
_PROOF_STATE_FILE = "state.json"
_MAX_PROOF_PATHS = 256
_MAX_MATERIAL_DIRTY_PATHS = 4096
_MAX_MATERIAL_HASH_BYTES = 128 * 1024 * 1024
_MAX_MATERIAL_WALK_ENTRIES = 65_536
_MAX_GIT_METADATA_BYTES = 8 * 1024 * 1024
# Quota sampling is deliberately one isolated policy knob.  The monitor
# schedules from scan *start* times so a slow walk never adds another fixed
# sleep on top.  Production's service-private tmpfs is the hard aggregate
# block/inode boundary; this sampler is the per-lease attribution/kill rail.
_QUOTA_SENTINEL_INTERVAL_SECONDS = 0.05
_QUOTA_NORMAL_SCAN_INTERVAL_SECONDS = 0.25
_QUOTA_NEAR_SCAN_INTERVAL_SECONDS = 0.05
_QUOTA_SPARSE_FALLBACK_SECONDS = 2.0
_QUOTA_NEAR_HIGH_WATERMARK = 0.80
_QUOTA_NEAR_LOW_WATERMARK = 0.70
_QUOTA_PROJECTED_BREACH_SECONDS = 2.0
_USAGE_EXACT_IDLE = "EXACT_IDLE"
_USAGE_DIRTY_ACTIVE = "DIRTY_ACTIVE"
_USAGE_POISONED = "POISONED"
_MATERIAL_SOFT_EXCLUDED_DIRS = frozenset(
    {
        ".next",
        "build",
        "coverage",
        "dist",
        "htmlcov",
        "target",
    }
)
_MATERIAL_HARD_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hermes-runtime",
        ".cache",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "node_modules",
        "venv",
    }
)
_MATERIAL_EXCLUDED_DIRS = (
    _MATERIAL_HARD_EXCLUDED_DIRS | _MATERIAL_SOFT_EXCLUDED_DIRS
)
_MATERIAL_EXCLUDED_FILES = frozenset(
    {".coverage", "coverage.xml", "lcov.info"}
)
_MATERIAL_SOURCE_SUFFIXES = frozenset(
    {
        ".bash", ".c", ".cc", ".cfg", ".cpp", ".cs", ".go", ".h",
        ".hpp", ".java", ".js", ".json", ".jsx", ".kt", ".php",
        ".proto", ".py", ".rb", ".rs", ".scala", ".sh", ".sql",
        ".swift", ".toml", ".ts", ".tsx", ".yaml", ".yml", ".zsh",
    }
)
_MOUNTINFO_ESCAPE = re.compile(r"\\([0-7]{3})")


def _decode_mountinfo_field(value: str) -> str:
    """Decode Linux mountinfo's octal field escaping without shell parsing."""

    def replace(match: re.Match[str]) -> str:
        character = chr(int(match.group(1), 8))
        if character == "\x00":
            raise ProtocolError("quota_mountinfo_field_invalid")
        return character

    decoded = _MOUNTINFO_ESCAPE.sub(replace, value)
    if "\\" in decoded or "\x00" in decoded:
        raise ProtocolError("quota_mountinfo_field_invalid")
    return decoded


class ProtocolError(RuntimeError):
    """A stable fail-closed protocol violation."""


@dataclass(frozen=True)
class ReadOnlyBind:
    """One operator-sealed, server-owned read-only tree.

    Bind declarations are loaded from the privileged service configuration;
    the wire protocol deliberately has no mount fields.  Sources must be
    immutable to the worker identity and may only appear below the dedicated
    ``/opt/hermes-shared`` namespace inside the sandbox.
    """

    source: Path
    destination: Path
    source_uid: int = 0
    source_gid: int = 0

    def __post_init__(self) -> None:
        source = Path(self.source)
        destination = Path(self.destination)
        if (
            not source.is_absolute()
            or source != Path(os.path.normpath(source))
            or not destination.is_absolute()
            or destination != Path(os.path.normpath(destination))
        ):
            raise ValueError("read_only_bind_path_invalid")
        try:
            destination_relative = destination.relative_to(VIRTUAL_READ_ONLY_ROOT)
        except ValueError as exc:
            raise ValueError("read_only_bind_destination_invalid") from exc
        if len(destination_relative.parts) != 1:
            raise ValueError("read_only_bind_destination_invalid")
        try:
            source_relative = source.relative_to(HOST_READ_ONLY_ROOT)
        except ValueError as exc:
            raise ValueError("read_only_bind_source_namespace_invalid") from exc
        if len(source_relative.parts) != 1:
            raise ValueError("read_only_bind_source_namespace_invalid")
        if any(
            component.lower() in _DENIED_READ_ONLY_SOURCE_COMPONENTS
            for component in source.parts
        ):
            raise ValueError("read_only_bind_source_forbidden")
        if type(self.source_uid) is not int or self.source_uid < 0:
            raise ValueError("read_only_bind_uid_invalid")
        if type(self.source_gid) is not int or self.source_gid < 0:
            raise ValueError("read_only_bind_gid_invalid")
        _verify_read_only_tree(
            source,
            expected_uid=self.source_uid,
            expected_gid=self.source_gid,
        )
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "destination", destination)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _exact_mapping(value: Any, fields: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(fields):
        raise ProtocolError(f"{label}_fields_not_exact")
    return value


def _bounded_text(value: Any, *, maximum: int, label: str) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > maximum:
        raise ProtocolError(f"{label}_invalid")
    if "\x00" in value:
        raise ProtocolError(f"{label}_invalid")
    return value


def _reject_constant(_value: str) -> None:
    raise ProtocolError("request_json_invalid")


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("request_json_duplicate_key")
        result[key] = value
    return result


def parse_request(frame: bytes) -> Mapping[str, Any]:
    """Parse one canonical request and reject aliases or extra fields."""

    if not frame or len(frame) > MAX_FRAME_BYTES or b"\n" in frame:
        raise ProtocolError("request_frame_invalid")
    try:
        value = json.loads(
            frame.decode("ascii", errors="strict"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ProtocolError("request_json_invalid") from exc
    if canonical_bytes(value) != frame:
        raise ProtocolError("request_not_canonical")
    raw = _exact_mapping(value, _REQUEST_FIELDS, "request")
    operation = raw["operation"]
    if (
        raw["schema"] != REQUEST_SCHEMA
        or raw["protocol"] != PROTOCOL
        or not isinstance(operation, str)
        or operation not in _PARAMETER_FIELDS
        or not isinstance(raw["request_id"], str)
        or _REQUEST_ID.fullmatch(raw["request_id"]) is None
        or not isinstance(raw["lease_id"], str)
        or _ID.fullmatch(raw["lease_id"]) is None
    ):
        raise ProtocolError("request_identity_invalid")
    params = _exact_mapping(
        raw["parameters"], _PARAMETER_FIELDS[operation], "request_parameters"
    )
    if operation == "exec.start":
        _bounded_text(params["command"], maximum=MAX_COMMAND_BYTES, label="command")
        _bounded_text(params["cwd"], maximum=4096, label="cwd")
        if not isinstance(params["stdin_b64"], str):
            raise ProtocolError("stdin_invalid")
        try:
            stdin = base64.b64decode(params["stdin_b64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise ProtocolError("stdin_invalid") from exc
        if len(stdin) > MAX_STDIN_BYTES:
            raise ProtocolError("stdin_invalid")
        timeout = params["timeout_seconds"]
        if type(timeout) is not int or not 1 <= timeout <= 300:
            raise ProtocolError("timeout_invalid")
    elif operation == "exec.poll":
        if (
            not isinstance(params["session_id"], str)
            or _ID.fullmatch(params["session_id"]) is None
            or type(params["wait_milliseconds"]) is not int
            or not 0 <= params["wait_milliseconds"] <= 1000
        ):
            raise ProtocolError("poll_parameters_invalid")
    elif operation == "exec.cancel" and (
        not isinstance(params["session_id"], str)
        or _ID.fullmatch(params["session_id"]) is None
    ):
        raise ProtocolError("cancel_parameters_invalid")
    elif operation == "proof.mark_edited":
        paths = params["paths"]
        if (
            not isinstance(paths, list)
            or not paths
            or len(paths) > _MAX_PROOF_PATHS
        ):
            raise ProtocolError("proof_paths_invalid")
        for path in paths:
            _bounded_text(path, maximum=4096, label="proof_path")
            candidate = Path(path)
            if (
                not candidate.is_absolute()
                or candidate != Path(os.path.normpath(path))
                or ".." in candidate.parts
            ):
                raise ProtocolError("proof_path_invalid")
            try:
                candidate.relative_to(VIRTUAL_WORKSPACE_ROOT)
            except ValueError as exc:
                raise ProtocolError("proof_path_outside_lease") from exc
        observed_generation = params["observed_generation"]
        if (
            observed_generation is not None
            and (
                type(observed_generation) is not int
                or observed_generation < 0
            )
        ):
            raise ProtocolError("proof_observed_generation_invalid")
    return raw


def _response(request: Mapping[str, Any], *, ok: bool, result: Mapping[str, Any]) -> bytes:
    value = {
        "schema": RESPONSE_SCHEMA,
        "protocol": PROTOCOL,
        "request_id": request["request_id"],
        "lease_id": request["lease_id"],
        "operation": request["operation"],
        "ok": ok,
        "result": dict(result),
    }
    payload = canonical_bytes(value)
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError("response_frame_too_large")
    return payload


def _read_frame(stream) -> bytes | None:
    frame = stream.readline(MAX_FRAME_BYTES + 2)
    if frame == b"":
        return None
    if len(frame) > MAX_FRAME_BYTES + 1 or not frame.endswith(b"\n"):
        raise ProtocolError("request_frame_invalid")
    return frame[:-1]


def _write_frame(stream, frame: bytes) -> None:
    stream.write(frame + b"\n")
    stream.flush()


def _peer_credentials(connection: socket.socket) -> tuple[int, int]:
    """Return peer uid/gid on Linux/BSD, or fail closed."""

    if hasattr(socket, "SO_PEERCRED"):
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        _pid, uid, gid = struct.unpack("3i", raw)
        return uid, gid
    getpeereid = getattr(connection, "getpeereid", None)
    if callable(getpeereid):
        uid, gid = getpeereid()
        return int(uid), int(gid)
    raise ProtocolError("peer_credentials_unavailable")


def _verify_read_only_tree(
    root: Path,
    *,
    expected_uid: int,
    expected_gid: int,
) -> None:
    """Reject mutable, linked, or special entries in a configured RO tree."""

    root = Path(root)
    root_state = os.lstat(root)
    if (
        not stat.S_ISDIR(root_state.st_mode)
        or stat.S_ISLNK(root_state.st_mode)
        or root_state.st_uid != expected_uid
        or root_state.st_gid != expected_gid
        or stat.S_IMODE(root_state.st_mode) & 0o222
        or root_state.st_nlink < 2
    ):
        raise ValueError("read_only_bind_identity_invalid")
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in (*directories, *files):
            candidate = current_path / name
            item = os.lstat(candidate)
            if (
                stat.S_ISLNK(item.st_mode)
                or not (stat.S_ISDIR(item.st_mode) or stat.S_ISREG(item.st_mode))
                or item.st_uid != expected_uid
                or item.st_gid != expected_gid
                or stat.S_IMODE(item.st_mode) & 0o222
                or (stat.S_ISREG(item.st_mode) and item.st_nlink != 1)
            ):
                raise ValueError("read_only_bind_tree_not_sealed")


@dataclass(frozen=True)
class WorkerPolicy:
    """Owner-sealed mechanical policy for one unprivileged worker instance."""

    expected_peer_uid: int
    expected_peer_gid: int
    socket_uid: int
    socket_gid: int
    lease_base: Path
    lease_uid: int
    lease_gid: int
    network_isolated: bool
    bwrap_path: Path
    bwrap_sha256: str
    shell_sha256: str
    bwrap_uid: int = 0
    shell: Path = Path("/bin/bash")
    shell_uid: int = 0
    runtime_roots: tuple[Path, ...] = FIXED_RUNTIME_ROOTS
    maximum_timeout_seconds: int = 300
    maximum_output_bytes: int = MAX_OUTPUT_BYTES
    maximum_active_leases: int = 128
    maximum_active_jobs_per_lease: int = 8
    lease_ttl_seconds: int = 900
    lease_quota_bytes: int = 4 * 1024 * 1024 * 1024
    lease_quota_entries: int = 100_000
    global_quota_bytes: int = 4 * 1024 * 1024 * 1024
    global_quota_entries: int = 200_000
    read_only_binds: tuple[ReadOnlyBind, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "expected_peer_uid",
            "expected_peer_gid",
            "socket_uid",
            "socket_gid",
            "bwrap_uid",
            "shell_uid",
            "lease_uid",
            "lease_gid",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name}_invalid")
        if self.network_isolated is not True:
            raise ValueError("worker_network_namespace_not_attested")
        lease_base = Path(self.lease_base)
        if (
            not lease_base.is_absolute()
            or lease_base != Path(os.path.normpath(lease_base))
        ):
            raise ValueError("lease_base_invalid")
        base_state = os.lstat(lease_base)
        if (
            not stat.S_ISDIR(base_state.st_mode)
            or stat.S_ISLNK(base_state.st_mode)
            or base_state.st_uid != self.lease_uid
            or base_state.st_gid != self.lease_gid
            or stat.S_IMODE(base_state.st_mode) != 0o700
            or base_state.st_nlink < 2
        ):
            raise ValueError("lease_base_identity_invalid")
        bwrap = Path(self.bwrap_path)
        if not bwrap.is_absolute() or bwrap != Path(os.path.normpath(bwrap)):
            raise ValueError("bwrap_path_invalid")
        shell = Path(self.shell)
        if not shell.is_absolute() or shell != Path(os.path.normpath(shell)):
            raise ValueError("shell_path_invalid")
        _verify_regular_digest(
            bwrap,
            expected_sha256=self.bwrap_sha256,
            expected_uid=self.bwrap_uid,
        )
        _verify_regular_digest(
            shell,
            expected_sha256=self.shell_sha256,
            expected_uid=self.shell_uid,
        )
        if not 1 <= self.maximum_timeout_seconds <= 300:
            raise ValueError("worker_timeout_invalid")
        if not 4096 <= self.maximum_output_bytes <= MAX_OUTPUT_BYTES:
            raise ValueError("worker_output_limit_invalid")
        if not 1 <= self.maximum_active_leases <= MAX_LEASES_LIMIT:
            raise ValueError("maximum_active_leases_invalid")
        if not 1 <= self.maximum_active_jobs_per_lease <= MAX_ACTIVE_JOBS_PER_LEASE_LIMIT:
            raise ValueError("maximum_active_jobs_per_lease_invalid")
        if not 1 <= self.lease_ttl_seconds <= MAX_LEASE_TTL_SECONDS:
            raise ValueError("lease_ttl_invalid")
        if not 4096 <= self.lease_quota_bytes <= MAX_LEASE_QUOTA_BYTES:
            raise ValueError("lease_quota_bytes_invalid")
        if not 1 <= self.lease_quota_entries <= MAX_LEASE_QUOTA_ENTRIES:
            raise ValueError("lease_quota_entries_invalid")
        if not 4096 <= self.global_quota_bytes <= MAX_GLOBAL_QUOTA_BYTES:
            raise ValueError("global_quota_bytes_invalid")
        if not 1 <= self.global_quota_entries <= MAX_GLOBAL_QUOTA_ENTRIES:
            raise ValueError("global_quota_entries_invalid")
        if self.lease_quota_bytes > self.global_quota_bytes:
            raise ValueError("lease_quota_exceeds_global_bytes")
        # The service-wide entry accounting includes each lease directory.
        # Keep one full lease representable by the aggregate policy.
        if self.lease_quota_entries + 1 > self.global_quota_entries:
            raise ValueError("lease_quota_exceeds_global_entries")
        if tuple(self.runtime_roots) != FIXED_RUNTIME_ROOTS:
            raise ValueError("runtime_roots_not_exact")
        if not isinstance(self.read_only_binds, tuple) or any(
            not isinstance(item, ReadOnlyBind) for item in self.read_only_binds
        ):
            raise ValueError("read_only_binds_invalid")
        destinations: set[Path] = set()
        for item in self.read_only_binds:
            if item.source_uid == self.lease_uid:
                raise ValueError("read_only_bind_mutable_by_worker")
            try:
                item.source.relative_to(lease_base)
            except ValueError:
                pass
            else:
                raise ValueError("read_only_bind_source_is_lease")
            if item.destination in destinations:
                raise ValueError("read_only_bind_destination_duplicate")
            destinations.add(item.destination)
        object.__setattr__(self, "lease_base", lease_base)
        object.__setattr__(self, "bwrap_path", bwrap)
        object.__setattr__(self, "shell", shell)


def _verify_regular_digest(
    path: Path,
    *,
    expected_sha256: str,
    expected_uid: int,
) -> os.stat_result:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        return _verify_open_regular_digest(
            path,
            descriptor,
            expected_sha256=expected_sha256,
            expected_uid=expected_uid,
        )
    finally:
        os.close(descriptor)


def _verify_open_regular_digest(
    path: Path,
    descriptor: int,
    *,
    expected_sha256: str,
    expected_uid: int,
) -> os.stat_result:
    """Verify the exact already-open descriptor later passed to bwrap."""

    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("executable_digest_invalid")
    before = os.lstat(path)
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1
        or before.st_uid != expected_uid
        or stat.S_IMODE(before.st_mode) & 0o022
        or not stat.S_IMODE(before.st_mode) & 0o111
    ):
        raise ValueError("executable_identity_invalid")
    opened = os.fstat(descriptor)
    with os.fdopen(os.dup(descriptor), "rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    after = os.lstat(path)
    identity = lambda item: (
        item.st_dev,
        item.st_ino,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )
    if identity(before) != identity(opened) or identity(before) != identity(after):
        raise ValueError("executable_changed_during_verification")
    if digest != expected_sha256:
        raise ValueError("executable_digest_mismatch")
    return before


@dataclass(frozen=True)
class _MaterialSnapshot:
    """A bounded, content-addressed view of material workspace files."""

    files: tuple[tuple[str, str], ...]
    scope: str
    hashed_bytes: int = 0


def _material_path_excluded(relative: Path) -> bool:
    return (
        any(part in _MATERIAL_EXCLUDED_DIRS for part in relative.parts[:-1])
        or relative.name in _MATERIAL_EXCLUDED_DIRS
        or relative.name in _MATERIAL_EXCLUDED_FILES
    )


def _material_path_soft_excluded(relative: Path) -> bool:
    return any(
        part in _MATERIAL_SOFT_EXCLUDED_DIRS
        for part in relative.parts
    )


def _material_path_hard_excluded(relative: Path) -> bool:
    return any(
        part in _MATERIAL_HARD_EXCLUDED_DIRS
        for part in relative.parts
    )


def _hash_material_file(root: Path, relative: Path) -> str:
    path = root / relative
    before = os.lstat(path)
    digest = hashlib.sha256()
    digest.update(str(stat.S_IMODE(before.st_mode)).encode("ascii"))
    digest.update(b"\0")
    if stat.S_ISLNK(before.st_mode):
        digest.update(b"symlink\0")
        digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
    elif stat.S_ISREG(before.st_mode):
        digest.update(b"regular\0")
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    else:
        raise ProtocolError("material_snapshot_special_file")
    after = os.lstat(path)
    identity = lambda item: (
        item.st_dev,
        item.st_ino,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
        item.st_mode,
    )
    if identity(before) != identity(after):
        raise ProtocolError("material_snapshot_raced")
    return digest.hexdigest()


def _run_git_metadata(
    git_root: Path,
    *arguments: str,
    allow_failure: bool = False,
) -> bytes | None:
    environment = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-c",
                "core.trustctime=true",
                "-c",
                "core.checkStat=default",
                "-C",
                str(git_root),
                *arguments,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if allow_failure:
            return None
        raise ProtocolError("material_git_scan_failed") from exc
    if result.returncode != 0:
        if allow_failure:
            return None
        raise ProtocolError("material_git_scan_failed")
    if len(result.stdout) > _MAX_GIT_METADATA_BYTES:
        raise ProtocolError("material_git_metadata_limit")
    return result.stdout


def _validated_git_root(lease_root: Path, probe_cwd: Path) -> Path | None:
    probe = _run_git_metadata(
        probe_cwd,
        "rev-parse",
        "--show-toplevel",
        allow_failure=True,
    )
    if probe is None:
        return None
    try:
        result = Path(
            probe.decode("utf-8", errors="strict").strip()
        ).resolve()
        result.relative_to(lease_root.resolve())
    except (UnicodeError, OSError, ValueError):
        raise ProtocolError("material_git_root_outside_lease")
    return result


def _git_repo_material_snapshot(
    lease_root: Path,
    git_root: Path,
    *,
    captured_nested_roots: Sequence[Path] = (),
) -> _MaterialSnapshot:
    root_prefix = git_root.relative_to(lease_root.resolve())
    meta_scope = (VIRTUAL_WORKSPACE_ROOT / root_prefix).as_posix()

    head = _run_git_metadata(
        git_root,
        "rev-parse",
        "--verify",
        "HEAD",
        allow_failure=True,
    )
    head_identity = (
        head.decode("ascii", errors="strict").strip()
        if head is not None
        else "UNBORN"
    )
    index_path_raw = _run_git_metadata(git_root, "rev-parse", "--git-path", "index")
    assert index_path_raw is not None
    try:
        index_path = Path(
            index_path_raw.decode("utf-8", errors="strict").strip()
        )
        if not index_path.is_absolute():
            index_path = git_root / index_path
        index_path = index_path.resolve()
        index_path.relative_to(lease_root.resolve())
    except (UnicodeError, OSError, ValueError) as exc:
        raise ProtocolError("material_git_index_invalid") from exc
    if not index_path.is_file() or index_path.stat().st_size > _MAX_GIT_METADATA_BYTES:
        raise ProtocolError("material_git_index_invalid")
    with index_path.open("rb") as stream:
        index_digest = hashlib.file_digest(stream, "sha256").hexdigest()

    status_raw = _run_git_metadata(
        git_root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=no",
        "--ignored=no",
        "--ignore-submodules=none",
    )
    assert status_raw is not None
    status_tokens = status_raw.split(b"\0")
    dirty: set[Path] = set()
    status_entries: list[str] = []
    index = 0
    while index < len(status_tokens):
        token = status_tokens[index]
        index += 1
        if not token:
            continue
        if len(token) < 4 or token[2:3] != b" ":
            raise ProtocolError("material_git_status_invalid")
        try:
            status = token[:2].decode("ascii", errors="strict")
            raw_paths = [token[3:].decode("utf-8", errors="strict")]
            if status[0] in {"R", "C"} or status[1] in {"R", "C"}:
                if index >= len(status_tokens) or not status_tokens[index]:
                    raise ProtocolError("material_git_status_invalid")
                raw_paths.append(
                    status_tokens[index].decode("utf-8", errors="strict")
                )
                index += 1
        except UnicodeError as exc:
            raise ProtocolError("material_path_encoding_invalid") from exc
        kept: list[str] = []
        for raw_path in raw_paths:
            item = Path(raw_path)
            if item.is_absolute() or ".." in item.parts:
                raise ProtocolError("material_git_path_invalid")
            relative = root_prefix / item
            # Git-tracked material is authoritative even under directories
            # commonly used for generated output.  Exclusions apply only to
            # untracked artifacts.
            if (
                status != "??"
                or not _material_path_excluded(relative)
                or (
                    _material_path_soft_excluded(relative)
                    and not _material_path_hard_excluded(relative)
                    and relative.suffix.lower() in _MATERIAL_SOURCE_SUFFIXES
                )
            ):
                dirty.add(relative)
                kept.append(relative.as_posix())
        if kept:
            status_entries.append(status + ":" + "->".join(kept))

    excluded_untracked_dirs = sorted(
        _MATERIAL_HARD_EXCLUDED_DIRS | _MATERIAL_SOFT_EXCLUDED_DIRS
    )
    untracked_raw = _run_git_metadata(
        git_root,
        "ls-files",
        "-z",
        "--others",
        "--exclude-standard",
        "--",
        ".",
        *(
            f":(exclude,glob)**/{directory}/**"
            for directory in excluded_untracked_dirs
        ),
    )
    assert untracked_raw is not None
    for raw in untracked_raw.split(b"\0"):
        if not raw:
            continue
        try:
            item = Path(raw.decode("utf-8", errors="strict"))
        except UnicodeError as exc:
            raise ProtocolError("material_path_encoding_invalid") from exc
        if item.is_absolute() or ".." in item.parts:
            raise ProtocolError("material_git_path_invalid")
        relative = root_prefix / item
        if not _material_path_excluded(relative):
            dirty.add(relative)
            status_entries.append("??:" + relative.as_posix())

    ignored_pathspecs = [
        f":(glob)**/{directory}/**/*{suffix}"
        for directory in sorted(_MATERIAL_SOFT_EXCLUDED_DIRS)
        for suffix in sorted(_MATERIAL_SOURCE_SUFFIXES)
    ]
    for marker, extra in (
        ("??", ()),
        ("!!", ("--ignored",)),
    ):
        soft_raw = _run_git_metadata(
            git_root,
            "ls-files",
            "-z",
            "--others",
            *extra,
            "--exclude-standard",
            "--",
            *ignored_pathspecs,
        )
        assert soft_raw is not None
        for raw in soft_raw.split(b"\0"):
            if not raw:
                continue
            try:
                item = Path(raw.decode("utf-8", errors="strict"))
            except UnicodeError as exc:
                raise ProtocolError("material_path_encoding_invalid") from exc
            if item.is_absolute() or ".." in item.parts:
                raise ProtocolError("material_git_path_invalid")
            relative = root_prefix / item
            if (
                _material_path_soft_excluded(relative)
                and not _material_path_hard_excluded(relative)
                and relative.suffix.lower() in _MATERIAL_SOURCE_SUFFIXES
            ):
                dirty.add(relative)
                status_entries.append(marker + ":" + relative.as_posix())

    if len(dirty) > _MAX_MATERIAL_DIRTY_PATHS:
        raise ProtocolError("material_dirty_path_limit")
    files: list[tuple[str, str]] = [
        (f"@git-head:{meta_scope}", head_identity),
        (f"@git-index:{meta_scope}", index_digest),
        (
            f"@git-status:{meta_scope}",
            hashlib.sha256(
                canonical_bytes(sorted(status_entries))
            ).hexdigest(),
        ),
    ]
    total_hashed = 0
    for relative in sorted(dirty, key=lambda item: item.as_posix()):
        path = lease_root / relative
        try:
            item = os.lstat(path)
        except FileNotFoundError:
            files.append((relative.as_posix(), "@deleted"))
            continue
        if stat.S_ISDIR(item.st_mode):
            resolved = path.resolve()
            if any(
                resolved == nested
                or resolved in nested.parents
                for nested in captured_nested_roots
            ):
                # The nested repository contributes its own exact snapshot.
                # Keep the outer status token, but do not hash a directory as
                # if it were a regular file.
                continue
        if stat.S_ISREG(item.st_mode):
            total_hashed += item.st_size
        elif stat.S_ISLNK(item.st_mode):
            total_hashed += item.st_size
        if total_hashed > _MAX_MATERIAL_HASH_BYTES:
            raise ProtocolError("material_hash_byte_limit")
        files.append(
            (relative.as_posix(), _hash_material_file(lease_root, relative))
        )
    return _MaterialSnapshot(
        tuple(files),
        meta_scope,
        total_hashed,
    )


def _git_material_snapshot_with_roots(
    lease_root: Path,
    scan_cwd: Path,
) -> tuple[_MaterialSnapshot | None, tuple[Path, ...]]:
    roots: list[Path] = []
    for probe_cwd in (lease_root, scan_cwd):
        root = _validated_git_root(lease_root, probe_cwd)
        if root is not None and root not in roots:
            roots.append(root)
    if not roots:
        return None, ()
    snapshots = [
        _git_repo_material_snapshot(
            lease_root,
            root,
            captured_nested_roots=tuple(
                other
                for other in roots
                if other != root and root in other.parents
            ),
        )
        for root in roots
    ]
    total_hashed = sum(snapshot.hashed_bytes for snapshot in snapshots)
    if total_hashed > _MAX_MATERIAL_HASH_BYTES:
        raise ProtocolError("material_hash_byte_limit")
    files = tuple(
        sorted(
            (
                item
                for snapshot in snapshots
                for item in snapshot.files
            ),
            key=lambda item: item[0],
        )
    )
    if (
        sum(1 for path, _digest in files if not path.startswith("@"))
        > _MAX_MATERIAL_DIRTY_PATHS
    ):
        raise ProtocolError("material_dirty_path_limit")
    # The nearest execution-cwd repo is last and owns project applicability;
    # outer metadata remains in the combined fingerprint.
    return (
        _MaterialSnapshot(files, snapshots[-1].scope, total_hashed),
        tuple(roots),
    )


def _git_material_snapshot(
    lease_root: Path,
    scan_cwd: Path,
) -> _MaterialSnapshot | None:
    snapshot, _roots = _git_material_snapshot_with_roots(
        lease_root,
        scan_cwd,
    )
    return snapshot


def _fallback_material_path_included(relative: Path) -> bool:
    if (
        _material_path_hard_excluded(relative)
        or relative.name in _MATERIAL_EXCLUDED_FILES
    ):
        return False
    if _material_path_soft_excluded(relative):
        return relative.suffix.lower() in _MATERIAL_SOURCE_SUFFIXES
    return True


def _fallback_material_paths(
    root: Path,
    *,
    captured_git_roots: Sequence[Path] = (),
) -> list[Path]:
    try:
        resolved_root = root.resolve()
        captured = {
            candidate.resolve().relative_to(resolved_root)
            for candidate in captured_git_roots
        }
    except (OSError, ValueError) as exc:
        raise ProtocolError("material_snapshot_root_invalid") from exc
    if Path(".") in captured:
        return []

    paths: list[Path] = []
    walked_entries = 0

    def walk_error(error: OSError) -> None:
        raise ProtocolError("material_snapshot_walk_failed") from error

    for current, directories, files in os.walk(
        root,
        topdown=True,
        onerror=walk_error,
        followlinks=False,
    ):
        current_path = Path(current)
        relative_current = current_path.relative_to(root)
        retained_directories: list[str] = []
        for name in sorted(directories):
            walked_entries += 1
            if walked_entries > _MAX_MATERIAL_WALK_ENTRIES:
                raise ProtocolError("material_snapshot_walk_entry_limit")
            relative = relative_current / name
            if (
                relative in captured
                or _material_path_hard_excluded(relative)
                or relative.name in _MATERIAL_EXCLUDED_FILES
            ):
                continue
            try:
                item = os.lstat(root / relative)
            except FileNotFoundError as exc:
                raise ProtocolError("material_snapshot_raced") from exc
            if stat.S_ISLNK(item.st_mode):
                if _fallback_material_path_included(relative):
                    paths.append(relative)
            elif stat.S_ISDIR(item.st_mode):
                # Soft build/output trees remain traversable because source
                # and config files within them are proof material.
                retained_directories.append(name)
            else:
                raise ProtocolError("material_snapshot_special_file")
            if len(paths) > _MAX_MATERIAL_DIRTY_PATHS:
                raise ProtocolError("material_snapshot_entry_limit")
        directories[:] = retained_directories
        for name in sorted(files):
            walked_entries += 1
            if walked_entries > _MAX_MATERIAL_WALK_ENTRIES:
                raise ProtocolError("material_snapshot_walk_entry_limit")
            relative = relative_current / name
            if _fallback_material_path_included(relative):
                paths.append(relative)
                if len(paths) > _MAX_MATERIAL_DIRTY_PATHS:
                    raise ProtocolError("material_snapshot_entry_limit")
    return paths


def _material_snapshot(root: Path, scan_cwd: Path | None = None) -> _MaterialSnapshot:
    """Return a git-aware snapshot, with a conservative non-git fallback."""

    git_snapshot, git_roots = _git_material_snapshot_with_roots(
        root,
        scan_cwd or root,
    )
    if git_snapshot is not None and root.resolve() in git_roots:
        return git_snapshot
    paths = _fallback_material_paths(
        root,
        captured_git_roots=git_roots,
    )
    unique = sorted(set(paths), key=lambda item: item.as_posix())
    if len(unique) > _MAX_MATERIAL_DIRTY_PATHS:
        raise ProtocolError("material_snapshot_entry_limit")
    files: list[tuple[str, str]] = []
    total_hashed = 0
    for relative in unique:
        try:
            item = os.lstat(root / relative)
            total_hashed += item.st_size
            if total_hashed > _MAX_MATERIAL_HASH_BYTES:
                raise ProtocolError("material_hash_byte_limit")
            digest = _hash_material_file(root, relative)
        except FileNotFoundError as exc:
            raise ProtocolError("material_snapshot_raced") from exc
        files.append((relative.as_posix(), digest))
    fallback = _MaterialSnapshot(
        tuple(files),
        str(VIRTUAL_WORKSPACE_ROOT),
        total_hashed,
    )
    if git_snapshot is None:
        return fallback
    combined_hashed = git_snapshot.hashed_bytes + fallback.hashed_bytes
    if combined_hashed > _MAX_MATERIAL_HASH_BYTES:
        raise ProtocolError("material_hash_byte_limit")
    combined_files = tuple(
        sorted(
            (*git_snapshot.files, *fallback.files),
            key=lambda item: item[0],
        )
    )
    if (
        sum(
            1
            for path, _digest in combined_files
            if not path.startswith("@")
        )
        > _MAX_MATERIAL_DIRTY_PATHS
    ):
        raise ProtocolError("material_snapshot_entry_limit")
    return _MaterialSnapshot(
        combined_files,
        git_snapshot.scope,
        combined_hashed,
    )


def _changed_material_paths(
    before: _MaterialSnapshot,
    after: _MaterialSnapshot,
) -> list[str]:
    before_map = dict(before.files)
    after_map = dict(after.files)
    changed = sorted(
        path
        for path in set(before_map) | set(after_map)
        if not path.startswith("@") and before_map.get(path) != after_map.get(path)
    )
    result = [
        str(VIRTUAL_WORKSPACE_ROOT / path)
        for path in changed[:_MAX_PROOF_PATHS]
    ]
    if before.files != after.files and not result:
        result = [after.scope or before.scope or str(VIRTUAL_WORKSPACE_ROOT)]
    return result


def _material_fingerprint(snapshot: _MaterialSnapshot) -> str:
    return hashlib.sha256(
        canonical_bytes(
            {
                "scope": snapshot.scope,
                "files": list(snapshot.files),
            }
        )
    ).hexdigest()


def _wrapped_execution_parts(command: str) -> tuple[str, str] | None:
    """Recognize the complete BaseEnvironment execution envelope."""

    if (
        not command.endswith("exit $__hermes_ec")
        or command.count("\n__hermes_ec=$?\n") != 1
    ):
        return None
    payload_match = re.search(
        r"(?:^|\n)eval '(.*)'\n__hermes_ec=\$\?\n",
        command,
        flags=re.DOTALL,
    )
    cd_matches = list(
        re.finditer(
            r"(?:^|\n)builtin cd -- (.+?) \|\| exit 126(?:\n|$)",
            command,
        )
    )
    if (
        payload_match is None
        or len(cd_matches) != 1
        or cd_matches[0].start() >= payload_match.start()
    ):
        return None
    return (
        payload_match.group(1).replace("'\\''", "'"),
        cd_matches[0].group(1),
    )


def _executed_virtual_cwd(command: str, fallback: Path) -> Path:
    wrapped = _wrapped_execution_parts(command)
    if wrapped is None:
        return fallback
    try:
        tokens = shlex.split(wrapped[1], posix=True)
    except ValueError as exc:
        raise ProtocolError("wrapped_cwd_invalid") from exc
    if len(tokens) != 1:
        raise ProtocolError("wrapped_cwd_invalid")
    candidate = Path(tokens[0])
    if (
        not candidate.is_absolute()
        or candidate != Path(os.path.normpath(candidate))
        or ".." in candidate.parts
    ):
        raise ProtocolError("wrapped_cwd_invalid")
    try:
        candidate.relative_to(VIRTUAL_WORKSPACE_ROOT)
    except ValueError as exc:
        raise ProtocolError("wrapped_cwd_outside_lease") from exc
    return candidate


def _validate_verification(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    raw = _exact_mapping(value, _PROOF_VERIFICATION_FIELDS, "proof_verification")
    result: dict[str, str] = {}
    for field_name in _PROOF_VERIFICATION_FIELDS:
        result[field_name] = _bounded_text(
            raw[field_name],
            maximum=4096,
            label=f"proof_verification_{field_name}",
        )
    if result["status"] not in {"passed", "failed"}:
        raise ProtocolError("proof_verification_status_invalid")
    return result


def _validate_proof_state(value: Any, lease_id: str) -> dict[str, Any]:
    raw = _exact_mapping(value, _PROOF_STATE_FIELDS, "proof_state")
    if raw["schema"] != PROOF_STATE_SCHEMA or raw["lease_id"] != lease_id:
        raise ProtocolError("proof_state_identity_invalid")
    edit_generation = raw["edit_generation"]
    verified_generation = raw["verified_generation"]
    if (
        type(edit_generation) is not int
        or edit_generation < 0
        or type(verified_generation) is not int
        or verified_generation < 0
        or verified_generation > edit_generation
    ):
        raise ProtocolError("proof_state_generation_invalid")
    pending = raw["pending_paths"]
    if not isinstance(pending, list) or len(pending) > _MAX_PROOF_PATHS:
        raise ProtocolError("proof_state_paths_invalid")
    normalized: list[str] = []
    for path in pending:
        _bounded_text(path, maximum=4096, label="proof_state_path")
        candidate = Path(path)
        try:
            candidate.relative_to(VIRTUAL_WORKSPACE_ROOT)
        except ValueError as exc:
            raise ProtocolError("proof_state_path_outside_lease") from exc
        if not candidate.is_absolute() or candidate != Path(os.path.normpath(path)):
            raise ProtocolError("proof_state_path_invalid")
        normalized.append(path)
    applicability = raw["applicability"]
    project_root = raw["project_root"]
    verify_commands_digest = raw["verify_commands_digest"]
    material_fingerprint = raw["material_fingerprint"]
    if applicability not in _PROOF_APPLICABILITY_VALUES:
        raise ProtocolError("proof_state_applicability_invalid")
    _bounded_text(project_root, maximum=4096, label="proof_state_project_root")
    if project_root:
        candidate_root = Path(project_root)
        try:
            candidate_root.relative_to(VIRTUAL_WORKSPACE_ROOT)
        except ValueError as exc:
            raise ProtocolError("proof_state_project_root_outside_lease") from exc
        if (
            not candidate_root.is_absolute()
            or candidate_root != Path(os.path.normpath(project_root))
        ):
            raise ProtocolError("proof_state_project_root_invalid")
    if verify_commands_digest and re.fullmatch(
        r"[0-9a-f]{64}", verify_commands_digest
    ) is None:
        raise ProtocolError("proof_state_verify_digest_invalid")
    if applicability == "applicable" and (
        not project_root or not verify_commands_digest
    ):
        raise ProtocolError("proof_state_project_binding_missing")
    if material_fingerprint and re.fullmatch(
        r"[0-9a-f]{64}", material_fingerprint
    ) is None:
        raise ProtocolError("proof_state_material_fingerprint_invalid")
    return {
        "schema": PROOF_STATE_SCHEMA,
        "lease_id": lease_id,
        "edit_generation": edit_generation,
        "verified_generation": verified_generation,
        "pending_paths": sorted(set(normalized)),
        "last_verification": _validate_verification(raw["last_verification"]),
        "applicability": applicability,
        "project_root": project_root,
        "verify_commands_digest": verify_commands_digest,
        "material_fingerprint": material_fingerprint,
    }


def _proof_status(state: Mapping[str, Any]) -> str:
    """Return the compatibility status for a structural-only receipt.

    Verification sufficiency is model-authored.  The v1 wire schema retains
    this field for compatibility, but the worker never derives it from command
    text or exit output and therefore always reports the neutral value.
    """

    del state
    return "unverified"


def _validate_proof_receipt(value: Any, lease_id: str) -> dict[str, Any]:
    raw = _exact_mapping(value, _PROOF_RECEIPT_FIELDS, "proof_receipt")
    if raw["schema"] != PROOF_RECEIPT_SCHEMA or raw["lease_id"] != lease_id:
        raise ProtocolError("proof_receipt_identity_invalid")
    state = _validate_proof_state(
        {
            "schema": PROOF_STATE_SCHEMA,
            "lease_id": lease_id,
            "edit_generation": raw["edit_generation"],
            "verified_generation": raw["verified_generation"],
            "pending_paths": raw["pending_paths"],
            "last_verification": raw["verification"],
            "applicability": raw["applicability"],
            "project_root": raw["project_root"],
            "verify_commands_digest": raw["verify_commands_digest"],
            "material_fingerprint": raw["material_fingerprint"],
        },
        lease_id,
    )
    status = raw["status"]
    detection = raw["mutation_detection"]
    if (
        status not in _PROOF_STATUS_VALUES
        or detection not in _PROOF_MUTATION_VALUES
        or status != _proof_status(state)
    ):
        raise ProtocolError("proof_receipt_status_invalid")
    changed = raw["changed_paths"]
    if not isinstance(changed, list) or len(changed) > _MAX_PROOF_PATHS:
        raise ProtocolError("proof_receipt_paths_invalid")
    for path in changed:
        _bounded_text(path, maximum=4096, label="proof_receipt_path")
        candidate = Path(path)
        try:
            candidate.relative_to(VIRTUAL_WORKSPACE_ROOT)
        except ValueError as exc:
            raise ProtocolError("proof_receipt_path_outside_lease") from exc
        if not candidate.is_absolute() or candidate != Path(os.path.normpath(path)):
            raise ProtocolError("proof_receipt_path_invalid")
    return {
        "schema": PROOF_RECEIPT_SCHEMA,
        "lease_id": lease_id,
        "edit_generation": state["edit_generation"],
        "verified_generation": state["verified_generation"],
        "status": status,
        "mutation_detection": detection,
        "changed_paths": list(changed),
        "pending_paths": state["pending_paths"],
        "verification": state["last_verification"],
        "applicability": state["applicability"],
        "project_root": state["project_root"],
        "verify_commands_digest": state["verify_commands_digest"],
        "material_fingerprint": state["material_fingerprint"],
    }


@dataclass
class _Lease:
    lease_id: str
    root: Path
    created_monotonic: float
    last_used_monotonic: float
    connections: int = 0
    jobs: int = 0
    proof_lock: threading.RLock = field(default_factory=threading.RLock)
    proof_state: dict[str, Any] | None = None
    # One lock owns every quota scan, the cached sample, active-writer
    # membership, and the monitor token.  Holding it through scan+commit makes
    # stale lower samples structurally impossible.
    usage_lock: threading.Lock = field(default_factory=threading.Lock)
    active_executions: list[Any] = field(default_factory=list)
    usage_state: str = _USAGE_POISONED
    usage_epoch: int = 0
    usage_sample: tuple[int, int] | None = None
    usage_sample_started_monotonic: float = 0.0
    quota_last_scan_started_monotonic: float = 0.0
    quota_sentinel_epoch_seen: int = 0
    quota_sentinel_dirty: bool = False
    quota_near_limit: bool = False
    quota_monitor_token: Any = None


@dataclass
class _QuotaMonitorToken:
    wake: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    epoch: int = 0


@dataclass
class _QuotaSentinelToken:
    wake: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


def canonical_lease_id(session_id: str) -> str:
    """Mechanically derive one stable, path-safe lease from a session id."""

    if not isinstance(session_id, str) or not session_id or len(session_id) > 4096:
        raise ValueError("session_id_invalid")
    return "lease-" + hashlib.sha256(session_id.encode("utf-8")).hexdigest()


@dataclass
class _Execution:
    lease: _Lease
    process: subprocess.Popen[bytes]
    timeout_seconds: int
    output_limit: int
    command: str
    pre_snapshot: _MaterialSnapshot | None
    host_cwd: Path
    started_monotonic: float = field(default_factory=time.monotonic)
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    stdout_sent: int = 0
    stderr_sent: int = 0
    state: str = "running"
    lock: threading.Lock = field(default_factory=threading.Lock)
    complete: threading.Event = field(default_factory=threading.Event)
    stdout_complete: threading.Event = field(default_factory=threading.Event)
    stderr_complete: threading.Event = field(default_factory=threading.Event)
    proof_receipt: dict[str, Any] | None = None
    proof_finalized: bool = False

    def terminate(self, state: str) -> None:
        with self.lock:
            if self.state != "running":
                return
            self.state = state
        try:
            os.killpg(self.process.pid, signal.SIGKILL)  # windows-footgun: ok — Linux AF_UNIX/bwrap worker boundary
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self.process.kill()
            except OSError:
                pass


class IsolatedWorkerServer:
    """Threaded connection handler for one pre-created AF_UNIX listener."""

    def __init__(self, policy: WorkerPolicy):
        self.policy = policy
        self._threads: set[threading.Thread] = set()
        self._threads_lock = threading.Lock()
        self._replay: dict[str, tuple[bytes, bytes]] = {}
        self._replay_lock = threading.Lock()
        self._leases: dict[str, _Lease] = {}
        self._leases_lock = threading.RLock()
        self._global_usage_entries = 0
        self._global_usage_bytes = 0
        self._usage_reconciled = False
        self._accounting_poisoned = False
        self._poisoned_usage_leases = 0
        self._quota_clock = time.monotonic
        self._quota_sentinel_signature: tuple[int, int, int, int] | None = None
        self._quota_sentinel_epoch = 0
        self._quota_sentinel_token: _QuotaSentinelToken | None = None
        self._quota_dirty_leases: dict[str, _QuotaMonitorToken] = {}
        self._quota_topology_attested = False
        self._leases_discovered = False
        self._lease_base_fd = os.open(
            self.policy.lease_base,
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        base = os.fstat(self._lease_base_fd)
        self._lease_base_identity = (base.st_dev, base.st_ino)
        self._proof_root_fd = -1
        self._proof_root_lock = threading.RLock()
        try:
            os.mkdir(
                _PROOF_PRIVATE_DIR,
                mode=0o700,
                dir_fd=self._lease_base_fd,
            )
        except FileExistsError:
            pass
        os.chown(
            _PROOF_PRIVATE_DIR,
            self.policy.lease_uid,
            self.policy.lease_gid,
            dir_fd=self._lease_base_fd,
            follow_symlinks=False,
        )
        os.chmod(
            _PROOF_PRIVATE_DIR,
            0o700,
            dir_fd=self._lease_base_fd,
            follow_symlinks=False,
        )
        self._proof_root_fd = os.open(
            _PROOF_PRIVATE_DIR,
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=self._lease_base_fd,
        )
        self._validate_proof_root()
        self._cleanup_proof_temps()
        self._read_only_bind_fds: list[
            tuple[ReadOnlyBind, int, tuple[int, int]]
        ] = []
        try:
            for bind in self.policy.read_only_binds:
                descriptor = os.open(
                    bind.source,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                state = os.fstat(descriptor)
                self._read_only_bind_fds.append(
                    (bind, descriptor, (state.st_dev, state.st_ino))
                )
        except BaseException:
            for _bind, descriptor, _identity in self._read_only_bind_fds:
                os.close(descriptor)
            if self._proof_root_fd >= 0:
                os.close(self._proof_root_fd)
            os.close(self._lease_base_fd)
            raise
        self._validate_lease_base()

    def close(self) -> None:
        for _bind, descriptor, _identity in self._read_only_bind_fds:
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._read_only_bind_fds.clear()
        if self._proof_root_fd >= 0:
            try:
                os.close(self._proof_root_fd)
            except OSError:
                pass
            self._proof_root_fd = -1
        try:
            os.close(self._lease_base_fd)
        except OSError:
            pass

    def _validate_lease_base(self) -> None:
        path_state = os.lstat(self.policy.lease_base)
        opened = os.fstat(self._lease_base_fd)
        if (
            not stat.S_ISDIR(path_state.st_mode)
            or stat.S_ISLNK(path_state.st_mode)
            or (path_state.st_dev, path_state.st_ino) != self._lease_base_identity
            or (opened.st_dev, opened.st_ino) != self._lease_base_identity
            or path_state.st_uid != self.policy.lease_uid
            or path_state.st_gid != self.policy.lease_gid
            or stat.S_IMODE(path_state.st_mode) != 0o700
            or path_state.st_nlink < 2
        ):
            raise ProtocolError("lease_base_identity_drifted")

    def _attest_quota_topology(self) -> None:
        """Require the exact kernel-bounded production tmpfs topology."""

        self._validate_lease_base()
        fdinfo = Path(f"/proc/self/fdinfo/{self._lease_base_fd}")
        try:
            fdinfo_payload = fdinfo.read_text(
                encoding="utf-8",
                errors="strict",
            )
        except (OSError, UnicodeError) as exc:
            raise ProtocolError("quota_topology_fdinfo_unavailable") from exc
        mount_ids: list[int] = []
        for raw_line in fdinfo_payload.splitlines():
            key, separator, raw_value = raw_line.partition(":")
            if key != "mnt_id":
                continue
            value = raw_value.strip()
            if separator != ":" or re.fullmatch(r"[0-9]+", value) is None:
                raise ProtocolError("quota_topology_fdinfo_invalid")
            mount_ids.append(int(value))
        if len(mount_ids) != 1 or mount_ids[0] <= 0:
            raise ProtocolError("quota_topology_fdinfo_invalid")
        expected_mount_id = mount_ids[0]

        mountinfo = Path("/proc/self/mountinfo")
        try:
            payload = mountinfo.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError) as exc:
            raise ProtocolError("quota_topology_mountinfo_unavailable") from exc
        expected = str(self.policy.lease_base)
        matched: list[tuple[set[str], str, tuple[int, int], str]] = []
        for raw_line in payload.splitlines():
            fields = raw_line.split()
            try:
                separator = fields.index("-")
            except ValueError:
                continue
            if separator < 6 or len(fields) <= separator + 3:
                continue
            if re.fullmatch(r"[0-9]+", fields[0]) is None:
                continue
            if int(fields[0]) != expected_mount_id:
                continue
            mount_point = _decode_mountinfo_field(fields[4])
            device_match = re.fullmatch(r"([0-9]+):([0-9]+)", fields[2])
            if device_match is None:
                raise ProtocolError("quota_topology_device_invalid")
            mount_options = set(fields[5].split(","))
            mount_options.update(fields[separator + 3].split(","))
            matched.append(
                (
                    mount_options,
                    fields[separator + 1],
                    (
                        int(device_match.group(1)),
                        int(device_match.group(2)),
                    ),
                    mount_point,
                )
            )
        if not matched:
            raise ProtocolError("quota_topology_mount_id_missing")
        if len(matched) != 1:
            raise ProtocolError("quota_topology_mount_id_ambiguous")
        mount_options, filesystem_type, device, mount_point = matched[0]
        if mount_point != expected:
            raise ProtocolError("quota_topology_exact_mountpoint_missing")
        opened = os.fstat(self._lease_base_fd)
        opened_device = (os.major(opened.st_dev), os.minor(opened.st_dev))
        if device != opened_device:
            raise ProtocolError("quota_topology_device_mismatch")
        if filesystem_type != "tmpfs":
            raise ProtocolError("quota_topology_not_tmpfs")
        if not {"rw", "nodev", "nosuid"}.issubset(mount_options):
            raise ProtocolError("quota_topology_mount_flags_invalid")
        if "noexec" in mount_options:
            raise ProtocolError("quota_topology_noexec_invalid")
        filesystem = os.fstatvfs(self._lease_base_fd)
        capacity_bytes = filesystem.f_blocks * filesystem.f_frsize
        capacity_entries = filesystem.f_files
        if (
            capacity_bytes <= 0
            or capacity_bytes > self.policy.global_quota_bytes
        ):
            raise ProtocolError("quota_topology_byte_capacity_invalid")
        # One inode is reserved for the mount root itself.  Proof sidecars
        # consume from the same harder kernel bound and therefore only reduce
        # the public workspace's possible overshoot.
        if (
            capacity_entries <= 0
            or capacity_entries > self.policy.global_quota_entries + 1
        ):
            raise ProtocolError("quota_topology_inode_capacity_invalid")

    def _quota_sentinel(self) -> tuple[int, int, int, int]:
        """Return one O(1) physical block/inode sentinel."""

        filesystem = os.fstatvfs(self._lease_base_fd)
        return (
            int(filesystem.f_bfree),
            int(filesystem.f_bavail),
            int(filesystem.f_ffree),
            int(filesystem.f_favail),
        )

    def _validate_proof_root(self) -> None:
        state = os.stat(
            _PROOF_PRIVATE_DIR,
            dir_fd=self._lease_base_fd,
            follow_symlinks=False,
        )
        opened = os.fstat(self._proof_root_fd)
        if (
            not stat.S_ISDIR(state.st_mode)
            or stat.S_ISLNK(state.st_mode)
            or (state.st_dev, state.st_ino) != (opened.st_dev, opened.st_ino)
            or state.st_uid != self.policy.lease_uid
            or state.st_gid != self.policy.lease_gid
            or stat.S_IMODE(state.st_mode) != 0o700
            or state.st_nlink < 2
        ):
            raise ProtocolError("proof_root_identity_invalid")

    def _cleanup_proof_temps(self) -> None:
        with self._proof_root_lock:
            for name in os.listdir(self._proof_root_fd):
                if re.fullmatch(
                    r"\.lease-[0-9a-f]{64}\.[0-9a-f]{32}\.tmp",
                    name,
                ) is None:
                    continue
                item = os.stat(
                    name,
                    dir_fd=self._proof_root_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(item.st_mode)
                    or stat.S_ISLNK(item.st_mode)
                    or item.st_uid != self.policy.lease_uid
                    or item.st_gid != self.policy.lease_gid
                    or stat.S_IMODE(item.st_mode) != 0o600
                    or item.st_nlink != 1
                ):
                    raise ProtocolError("proof_temp_file_invalid")
                os.unlink(name, dir_fd=self._proof_root_fd)
            os.fsync(self._proof_root_fd)

    def _validate_read_only_binds(self) -> None:
        for bind, descriptor, identity in self._read_only_bind_fds:
            _verify_read_only_tree(
                bind.source,
                expected_uid=bind.source_uid,
                expected_gid=bind.source_gid,
            )
            opened = os.fstat(descriptor)
            current = os.lstat(bind.source)
            if (
                (opened.st_dev, opened.st_ino) != identity
                or (current.st_dev, current.st_ino) != identity
            ):
                raise ProtocolError("read_only_bind_identity_drifted")

    @staticmethod
    def _canonical_dynamic_lease_id(lease_id: str) -> bool:
        return re.fullmatch(r"lease-[0-9a-f]{64}", lease_id) is not None

    def _lease_root_state(self, lease_id: str) -> os.stat_result:
        state = os.stat(lease_id, dir_fd=self._lease_base_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(state.st_mode)
            or stat.S_ISLNK(state.st_mode)
            or state.st_uid != self.policy.lease_uid
            or state.st_gid != self.policy.lease_gid
            or stat.S_IMODE(state.st_mode) != 0o700
            or state.st_nlink < 2
        ):
            raise ProtocolError("lease_root_identity_invalid")
        return state

    def _load_existing_leases_locked(self, now: float) -> None:
        self._validate_lease_base()
        for name in os.listdir(self._lease_base_fd):
            if name == _PROOF_PRIVATE_DIR:
                self._validate_proof_root()
                continue
            if not self._canonical_dynamic_lease_id(name):
                raise ProtocolError("lease_base_contains_unmanaged_entry")
            state = self._lease_root_state(name)
            persisted_age = max(0.0, time.time() - state.st_mtime)
            self._leases.setdefault(
                name,
                _Lease(
                    lease_id=name,
                    root=self.policy.lease_base / name,
                    created_monotonic=now - persisted_age,
                    last_used_monotonic=now - persisted_age,
                ),
            )

    @staticmethod
    def _proof_state_name(lease_id: str) -> str:
        if re.fullmatch(r"lease-[0-9a-f]{64}", lease_id) is None:
            raise ProtocolError("proof_lease_id_invalid")
        return f"{lease_id}.json"

    def _initial_proof_state(self, lease_id: str) -> dict[str, Any]:
        return {
            "schema": PROOF_STATE_SCHEMA,
            "lease_id": lease_id,
            "edit_generation": 0,
            "verified_generation": 0,
            "pending_paths": [],
            "last_verification": None,
            "applicability": "unknown",
            "project_root": "",
            "verify_commands_digest": "",
            "material_fingerprint": "",
        }

    @staticmethod
    def _structural_proof_state(state: Mapping[str, Any]) -> dict[str, Any]:
        """Project a legacy proof sidecar onto non-semantic runtime facts."""

        projected = dict(state)
        projected["verified_generation"] = 0
        projected["last_verification"] = None
        projected["applicability"] = "unknown"
        projected["project_root"] = ""
        projected["verify_commands_digest"] = ""
        return projected

    def _proof_authority_usage(self) -> tuple[int, int]:
        """Validate and bound the server-only persisted proof sidecars."""

        with self._proof_root_lock:
            self._validate_proof_root()
            entries = 0
            total_bytes = 0
            for name in sorted(os.listdir(self._proof_root_fd)):
                if re.fullmatch(r"lease-[0-9a-f]{64}\.json", name) is None:
                    raise ProtocolError("proof_root_contains_unmanaged_entry")
                item = os.stat(name, dir_fd=self._proof_root_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(item.st_mode)
                    or stat.S_ISLNK(item.st_mode)
                    or item.st_uid != self.policy.lease_uid
                    or item.st_gid != self.policy.lease_gid
                    or stat.S_IMODE(item.st_mode) != 0o600
                    or item.st_nlink != 1
                    or item.st_size > MAX_FRAME_BYTES
                ):
                    raise ProtocolError("proof_state_file_invalid")
                entries += 1
                total_bytes += item.st_size
                if entries > self.policy.maximum_active_leases:
                    raise ProtocolError("proof_state_entry_limit")
            return entries, total_bytes

    def _read_proof_state(self, lease: _Lease) -> dict[str, Any]:
        if lease.proof_state is not None:
            return dict(lease.proof_state)
        name = self._proof_state_name(lease.lease_id)
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._proof_root_fd,
            )
        except FileNotFoundError:
            state = self._initial_proof_state(lease.lease_id)
            try:
                snapshot = _material_snapshot(lease.root, lease.root)
                state["material_fingerprint"] = _material_fingerprint(snapshot)
            except (OSError, ProtocolError, ValueError):
                state["material_fingerprint"] = ""
            self._write_proof_state(lease, state)
            return dict(state)
        try:
            item = os.fstat(descriptor)
            if (
                not stat.S_ISREG(item.st_mode)
                or item.st_uid != self.policy.lease_uid
                or item.st_gid != self.policy.lease_gid
                or stat.S_IMODE(item.st_mode) != 0o600
                or item.st_nlink != 1
                or item.st_size > MAX_FRAME_BYTES
            ):
                raise ProtocolError("proof_state_file_invalid")
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                payload = stream.read(MAX_FRAME_BYTES + 1)
        finally:
            os.close(descriptor)
        if not payload or len(payload) > MAX_FRAME_BYTES:
            raise ProtocolError("proof_state_file_invalid")
        try:
            decoded = json.loads(
                payload.decode("ascii", errors="strict"),
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=_reject_constant,
            )
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ProtocolError("proof_state_json_invalid") from exc
        if canonical_bytes(decoded) != payload:
            raise ProtocolError("proof_state_not_canonical")
        state = self._structural_proof_state(
            _validate_proof_state(decoded, lease.lease_id)
        )
        lease.proof_state = dict(state)
        return dict(state)

    def _write_proof_state(
        self,
        lease: _Lease,
        state: Mapping[str, Any],
    ) -> dict[str, Any]:
        validated = _validate_proof_state(dict(state), lease.lease_id)
        payload = canonical_bytes(validated)
        if len(payload) > MAX_FRAME_BYTES:
            raise ProtocolError("proof_state_too_large")
        name = self._proof_state_name(lease.lease_id)
        temporary = f".{lease.lease_id}.{uuid.uuid4().hex}.tmp"
        with self._proof_root_lock:
            try:
                current = os.stat(
                    name,
                    dir_fd=self._proof_root_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                current = None
            if current is not None and (
                not stat.S_ISREG(current.st_mode)
                or stat.S_ISLNK(current.st_mode)
                or current.st_uid != self.policy.lease_uid
                or current.st_gid != self.policy.lease_gid
                or stat.S_IMODE(current.st_mode) != 0o600
                or current.st_nlink != 1
            ):
                raise ProtocolError("proof_state_file_invalid")
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=self._proof_root_fd,
            )
            try:
                try:
                    os.fchmod(descriptor, 0o600)
                    os.fchown(
                        descriptor,
                        self.policy.lease_uid,
                        self.policy.lease_gid,
                    )
                    written = 0
                    while written < len(payload):
                        count = os.write(descriptor, payload[written:])
                        if count <= 0:
                            raise OSError("proof_state_short_write")
                        written += count
                    os.fsync(descriptor)
                    item = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(item.st_mode)
                        or item.st_nlink != 1
                        or item.st_size != len(payload)
                    ):
                        raise ProtocolError("proof_temp_file_invalid")
                finally:
                    os.close(descriptor)
            except BaseException:
                try:
                    os.unlink(temporary, dir_fd=self._proof_root_fd)
                except FileNotFoundError:
                    pass
                raise
            try:
                os.replace(
                    temporary,
                    name,
                    src_dir_fd=self._proof_root_fd,
                    dst_dir_fd=self._proof_root_fd,
                )
                os.fsync(self._proof_root_fd)
            except BaseException:
                try:
                    os.unlink(temporary, dir_fd=self._proof_root_fd)
                except FileNotFoundError:
                    pass
                raise
        lease.proof_state = dict(validated)
        return dict(validated)

    @staticmethod
    def _merge_proof_paths(existing: Sequence[str], added: Sequence[str]) -> list[str]:
        return sorted(set(existing) | set(added))[:_MAX_PROOF_PATHS]

    def _proof_receipt(
        self,
        state: Mapping[str, Any],
        *,
        mutation_detection: str,
        changed_paths: Sequence[str] = (),
    ) -> dict[str, Any]:
        return _validate_proof_receipt(
            {
                "schema": PROOF_RECEIPT_SCHEMA,
                "lease_id": state["lease_id"],
                "edit_generation": state["edit_generation"],
                "verified_generation": state["verified_generation"],
                "status": _proof_status(state),
                "mutation_detection": mutation_detection,
                "changed_paths": list(changed_paths),
                "pending_paths": list(state["pending_paths"]),
                "verification": None,
                "applicability": "unknown",
                "project_root": "",
                "verify_commands_digest": "",
                "material_fingerprint": state["material_fingerprint"],
            },
            str(state["lease_id"]),
        )

    def _reconcile_material_state(
        self,
        lease: _Lease,
        state: dict[str, Any],
        *,
        scan_cwd: Path,
        snapshot: _MaterialSnapshot | None = None,
    ) -> dict[str, Any]:
        """Close the mutation→receipt crash window before trusting a sidecar."""

        try:
            snapshot = snapshot or _material_snapshot(lease.root, scan_cwd)
            fingerprint = _material_fingerprint(snapshot)
        except (OSError, ProtocolError, ValueError):
            # A previously observed material view becoming unreadable is a
            # structural uncertainty: advance the mutation generation without
            # interpreting why the snapshot failed.
            if state.get("material_fingerprint"):
                state["edit_generation"] += 1
                state["pending_paths"] = self._merge_proof_paths(
                    state["pending_paths"],
                    [str(VIRTUAL_WORKSPACE_ROOT)],
                )
            state["material_fingerprint"] = ""
            return self._write_proof_state(lease, state)

        previous = str(state.get("material_fingerprint") or "")
        if previous and previous != fingerprint:
            state["edit_generation"] += 1
            state["pending_paths"] = self._merge_proof_paths(
                state["pending_paths"],
                [snapshot.scope or str(VIRTUAL_WORKSPACE_ROOT)],
            )
        elif not previous and (
            state["pending_paths"]
            or state["edit_generation"] > 0
        ):
            state["edit_generation"] += 1
            state["pending_paths"] = self._merge_proof_paths(
                state["pending_paths"],
                [snapshot.scope or str(VIRTUAL_WORKSPACE_ROOT)],
            )
        state["material_fingerprint"] = fingerprint
        return self._write_proof_state(lease, state)

    def _proof_status_receipt(self, lease: _Lease) -> dict[str, Any]:
        with lease.proof_lock:
            state = self._read_proof_state(lease)
            state = self._reconcile_material_state(
                lease,
                state,
                scan_cwd=lease.root,
            )
            return self._proof_receipt(state, mutation_detection="status")

    def _proof_mark_edited(
        self,
        lease: _Lease,
        paths: Sequence[str],
        observed_generation: int | None,
    ) -> dict[str, Any]:
        normalized = sorted(set(str(path) for path in paths))[:_MAX_PROOF_PATHS]
        with lease.proof_lock:
            state = self._read_proof_state(lease)
            first = Path(normalized[0])
            relative = first.relative_to(VIRTUAL_WORKSPACE_ROOT)
            host_candidate = lease.root / relative
            host_cwd = host_candidate if host_candidate.is_dir() else host_candidate.parent
            already_observed = (
                observed_generation == state["edit_generation"]
                and set(normalized).issubset(set(state["pending_paths"]))
            )
            if not already_observed:
                state["edit_generation"] += 1
            state["pending_paths"] = self._merge_proof_paths(
                state["pending_paths"],
                normalized,
            )
            try:
                state["material_fingerprint"] = _material_fingerprint(
                    _material_snapshot(lease.root, host_cwd)
                )
            except (OSError, ProtocolError, ValueError):
                state["material_fingerprint"] = ""
            state = self._write_proof_state(lease, state)
            return self._proof_receipt(
                state,
                mutation_detection="explicit",
                changed_paths=normalized,
            )

    def _touch_lease_locked(self, lease: _Lease, now: float | None = None) -> None:
        self._lease_root_state(lease.lease_id)
        os.utime(
            lease.lease_id,
            None,
            dir_fd=self._lease_base_fd,
            follow_symlinks=False,
        )
        lease.last_used_monotonic = time.monotonic() if now is None else now

    def _ensure_lease(self, lease_id: str) -> _Lease:
        if not self._canonical_dynamic_lease_id(lease_id):
            raise ProtocolError("lease_id_not_canonical")
        now = time.monotonic()
        with self._leases_lock:
            if self._accounting_poisoned:
                raise ProtocolError("quota_usage_poisoned")
            existing = self._leases.get(lease_id)
            if existing is not None:
                self._lease_root_state(lease_id)
                with existing.proof_lock:
                    self._read_proof_state(existing)
                self._touch_lease_locked(existing, now)
                return existing
            if len(self._leases) >= self.policy.maximum_active_leases:
                raise ProtocolError("lease_capacity_exhausted")
            # Account for the lease root before creating it so an empty lease
            # cannot push the service above its aggregate inode/entry bound.
            if self._usage_reconciled:
                self._cached_global_usage_locked(additional_entries=1)
            try:
                os.mkdir(lease_id, mode=0o700, dir_fd=self._lease_base_fd)
            except FileExistsError:
                # Hot-path lease discovery is intentionally forbidden.  A
                # pre-existing root must have been loaded by the one startup
                # reconciliation; otherwise accepting it would create an
                # unaccounted cache entry.
                raise ProtocolError("lease_usage_not_reconciled")
            created_identity: tuple[int, int] | None = None
            try:
                created = os.stat(
                    lease_id,
                    dir_fd=self._lease_base_fd,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(created.st_mode) or stat.S_ISLNK(
                    created.st_mode
                ):
                    raise ProtocolError("lease_creation_identity_invalid")
                created_identity = (created.st_dev, created.st_ino)
                os.chown(
                    lease_id,
                    self.policy.lease_uid,
                    self.policy.lease_gid,
                    dir_fd=self._lease_base_fd,
                    follow_symlinks=False,
                )
                os.chmod(
                    lease_id,
                    0o700,
                    dir_fd=self._lease_base_fd,
                    follow_symlinks=False,
                )
                self._lease_root_state(lease_id)
                lease = _Lease(
                    lease_id=lease_id,
                    root=self.policy.lease_base / lease_id,
                    created_monotonic=now,
                    last_used_monotonic=now,
                    usage_state=_USAGE_EXACT_IDLE,
                    usage_sample=(0, 0),
                    usage_sample_started_monotonic=now,
                    quota_last_scan_started_monotonic=now,
                )
                with lease.proof_lock:
                    self._read_proof_state(lease)
                self._touch_lease_locked(lease, now)
            except BaseException:
                try:
                    current = os.stat(
                        lease_id,
                        dir_fd=self._lease_base_fd,
                        follow_symlinks=False,
                    )
                    if (
                        created_identity is None
                        or not stat.S_ISDIR(current.st_mode)
                        or stat.S_ISLNK(current.st_mode)
                        or (current.st_dev, current.st_ino)
                        != created_identity
                    ):
                        raise ProtocolError(
                            "lease_creation_cleanup_identity_changed"
                        )
                    shutil.rmtree(
                        lease_id,
                        dir_fd=self._lease_base_fd,
                    )
                    try:
                        os.unlink(
                            self._proof_state_name(lease_id),
                            dir_fd=self._proof_root_fd,
                        )
                    except FileNotFoundError:
                        pass
                except BaseException as cleanup_error:
                    self._accounting_poisoned = True
                    self._usage_reconciled = False
                    raise ProtocolError(
                        "lease_creation_cleanup_failed"
                    ) from cleanup_error
                raise
            self._leases[lease_id] = lease
            if self._usage_reconciled:
                self._global_usage_entries += 1
            return lease

    def reap_expired(self, *, now_monotonic: float | None = None) -> tuple[str, ...]:
        now = time.monotonic() if now_monotonic is None else now_monotonic
        removed: list[str] = []
        # Discovery is startup-only.  The sealed workspace mount cannot grow
        # sibling lease roots through a sandboxed child.
        with self._leases_lock:
            if not self._leases_discovered:
                self._load_existing_leases_locked(now)
                self._leases_discovered = True
            candidates = tuple(self._leases.values())
        for lease in candidates:
            with lease.usage_lock:
                lease_id = lease.lease_id
                with self._leases_lock:
                    if self._leases.get(lease_id) is not lease:
                        continue
                    if (
                        lease.connections
                        or lease.jobs
                        or lease.active_executions
                        or now - lease.last_used_monotonic
                        < self.policy.lease_ttl_seconds
                    ):
                        continue
                    self._validate_lease_base()
                    self._lease_root_state(lease_id)
                    shutil.rmtree(lease_id, dir_fd=self._lease_base_fd)
                    try:
                        os.unlink(
                            self._proof_state_name(lease_id),
                            dir_fd=self._proof_root_fd,
                        )
                    except FileNotFoundError:
                        pass
                    self._leases.pop(lease_id, None)
                    if self._usage_reconciled:
                        sample = lease.usage_sample
                        if sample is None:
                            raise ProtocolError("lease_usage_sample_missing")
                        self._global_usage_entries -= sample[0] + 1
                        self._global_usage_bytes -= sample[1]
                        if lease.usage_state == _USAGE_POISONED:
                            self._poisoned_usage_leases = max(
                                0, self._poisoned_usage_leases - 1
                            )
                    removed.append(lease_id)
        return tuple(sorted(removed))

    def _validate_cwd(self, lease: _Lease, cwd: str) -> Path:
        candidate = Path(cwd)
        if not candidate.is_absolute() or candidate != Path(os.path.normpath(cwd)):
            raise ProtocolError("cwd_invalid")
        try:
            relative = candidate.relative_to(VIRTUAL_WORKSPACE_ROOT)
        except ValueError as exc:
            raise ProtocolError("cwd_outside_lease") from exc
        descriptor = os.open(
            lease.lease_id,
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=self._lease_base_fd,
        )
        try:
            for component in relative.parts:
                if component in {"", ".", ".."}:
                    raise ProtocolError("cwd_invalid")
                child = os.open(
                    component,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = child
        except OSError as exc:
            raise ProtocolError("cwd_symlink_or_not_directory") from exc
        finally:
            os.close(descriptor)
        return candidate

    def _lease_usage(self, lease: _Lease) -> tuple[int, int]:
        entries = 0
        total_bytes = 0
        self._lease_root_state(lease.lease_id)
        for current, directories, files in os.walk(
            lease.root, topdown=True, followlinks=False
        ):
            directories.sort()
            files.sort()
            for name in (*directories, *files):
                state = os.lstat(Path(current) / name)
                entries += 1
                if stat.S_ISREG(state.st_mode):
                    total_bytes += state.st_size
                elif stat.S_ISLNK(state.st_mode):
                    total_bytes += state.st_size
                elif stat.S_ISFIFO(state.st_mode) or stat.S_ISSOCK(state.st_mode):
                    total_bytes += state.st_size
                elif not stat.S_ISDIR(state.st_mode):
                    raise ProtocolError("lease_contains_special_file")
                if (
                    entries > self.policy.lease_quota_entries
                    or total_bytes > self.policy.lease_quota_bytes
                ):
                    raise ProtocolError("lease_quota_exceeded")
        return entries, total_bytes

    def _cached_global_usage_locked(
        self,
        *,
        additional_entries: int = 0,
        additional_bytes: int = 0,
    ) -> tuple[int, int]:
        """O(1) aggregate admission check; performs no filesystem operation."""

        if self._accounting_poisoned:
            raise ProtocolError("quota_usage_poisoned")
        if not self._usage_reconciled:
            raise ProtocolError("quota_usage_not_reconciled")
        if self._poisoned_usage_leases:
            raise ProtocolError("quota_usage_poisoned")
        entries = self._global_usage_entries + additional_entries
        total_bytes = self._global_usage_bytes + additional_bytes
        if (
            entries > self.policy.global_quota_entries
            or total_bytes > self.policy.global_quota_bytes
        ):
            raise ProtocolError("global_quota_exceeded")
        return entries, total_bytes

    def _commit_lease_usage_locked(
        self,
        lease: _Lease,
        sample: tuple[int, int],
        *,
        scan_started_monotonic: float,
    ) -> tuple[int, int]:
        """Commit one serialized sample through an O(1) aggregate delta."""

        with self._leases_lock:
            if not self._usage_reconciled:
                raise ProtocolError("quota_usage_not_reconciled")
            if self._leases.get(lease.lease_id) is not lease:
                raise ProtocolError("lease_usage_identity_changed")
            previous = lease.usage_sample
            if previous is None:
                raise ProtocolError("lease_usage_sample_missing")
            self._global_usage_entries += sample[0] - previous[0]
            self._global_usage_bytes += sample[1] - previous[1]
            lease.usage_sample = sample
            lease.usage_sample_started_monotonic = scan_started_monotonic
            lease.quota_last_scan_started_monotonic = (
                scan_started_monotonic
            )
            entries = self._global_usage_entries
            total_bytes = self._global_usage_bytes
        if (
            entries > self.policy.global_quota_entries
            or total_bytes > self.policy.global_quota_bytes
        ):
            raise ProtocolError("global_quota_exceeded")
        return sample

    def _scan_and_commit_lease_usage_locked(
        self,
        lease: _Lease,
    ) -> tuple[int, int]:
        scan_started = self._quota_clock()
        sample = self._lease_usage(lease)
        return self._commit_lease_usage_locked(
            lease,
            sample,
            scan_started_monotonic=scan_started,
        )

    def _refresh_lease_usage(self, lease: _Lease) -> tuple[int, int]:
        """Public test/maintenance seam with the same single-flight lock."""

        with lease.usage_lock:
            return self._scan_and_commit_lease_usage_locked(lease)

    def _mark_usage_poisoned_locked(self, lease: _Lease) -> None:
        # A lost exact sample is not healed in-band: every later start stays
        # fail-closed until the idle lease is reaped or startup reconciliation
        # in a fresh worker establishes a new exact baseline.
        if lease.usage_state == _USAGE_POISONED:
            return
        lease.usage_state = _USAGE_POISONED
        with self._leases_lock:
            self._poisoned_usage_leases += 1

    def _global_usage(self) -> tuple[int, int]:
        """Perform the one exact all-lease reconciliation.

        This is the startup/restart boundary, not a monitor hot path.  No
        global lock is held while a workspace tree is walked.
        """

        now = self._quota_clock()
        with self._leases_lock:
            if not self._leases_discovered:
                self._load_existing_leases_locked(now)
                self._leases_discovered = True
            leases = tuple(
                self._leases[key] for key in sorted(self._leases)
            )
        self._proof_authority_usage()
        samples: dict[str, tuple[int, int]] = {}
        for lease in leases:
            with lease.usage_lock:
                if lease.active_executions:
                    raise ProtocolError("startup_reconcile_active_lease")
                try:
                    samples[lease.lease_id] = self._lease_usage(lease)
                except Exception:
                    lease.usage_state = _USAGE_POISONED
                    with self._leases_lock:
                        self._usage_reconciled = False
                    raise
        entries = len(leases)
        total_bytes = 0
        for sample in samples.values():
            entries += sample[0]
            total_bytes += sample[1]
        with self._leases_lock:
            if set(self._leases) != set(samples):
                raise ProtocolError("startup_lease_set_changed")
            self._global_usage_entries = entries
            self._global_usage_bytes = total_bytes
            self._accounting_poisoned = False
            self._poisoned_usage_leases = 0
            self._usage_reconciled = True
            for lease_id, sample in samples.items():
                lease = self._leases[lease_id]
                lease.usage_sample = sample
                lease.usage_sample_started_monotonic = now
                lease.quota_last_scan_started_monotonic = now
                lease.usage_state = _USAGE_EXACT_IDLE
        self._quota_sentinel_signature = self._quota_sentinel()
        if (
            entries > self.policy.global_quota_entries
            or total_bytes > self.policy.global_quota_bytes
        ):
            raise ProtocolError("global_quota_exceeded")
        return entries, total_bytes

    def _ensure_quota_sentinel_locked(self) -> _QuotaSentinelToken:
        token = self._quota_sentinel_token
        if token is not None:
            return token
        token = _QuotaSentinelToken()
        thread = threading.Thread(
            target=self._quota_sentinel_loop,
            args=(token,),
            daemon=True,
        )
        token.thread = thread
        self._quota_sentinel_token = token
        try:
            thread.start()
        except BaseException:
            self._quota_sentinel_token = None
            raise
        return token

    def _remove_quota_monitor_locked(
        self,
        lease: _Lease,
        token: _QuotaMonitorToken,
    ) -> None:
        if lease.quota_monitor_token is not token:
            return
        lease.quota_monitor_token = None
        with self._leases_lock:
            if self._quota_dirty_leases.get(lease.lease_id) is token:
                self._quota_dirty_leases.pop(lease.lease_id, None)
            sentinel = self._quota_sentinel_token
        if sentinel is not None:
            sentinel.wake.set()

    def _poison_active_lease_locked(
        self,
        lease: _Lease,
        token: _QuotaMonitorToken | None,
    ) -> list[_Execution]:
        self._mark_usage_poisoned_locked(lease)
        active = list(lease.active_executions)
        if token is not None:
            self._remove_quota_monitor_locked(lease, token)
        return active

    def _quota_sentinel_loop(self, token: _QuotaSentinelToken) -> None:
        try:
            self._quota_sentinel_loop_inner(token)
        except Exception:
            with self._leases_lock:
                dirty = tuple(
                    (
                        self._leases.get(lease_id),
                        lease_token,
                    )
                    for lease_id, lease_token
                    in self._quota_dirty_leases.items()
                )
                if self._quota_sentinel_token is token:
                    self._quota_sentinel_token = None
            victims: list[_Execution] = []
            for lease, lease_token in dirty:
                if lease is None:
                    continue
                with lease.usage_lock:
                    if lease.quota_monitor_token is lease_token:
                        victims.extend(
                            self._poison_active_lease_locked(
                                lease, lease_token
                            )
                        )
            for execution in victims:
                execution.terminate("quota_exceeded")

    def _quota_sentinel_loop_inner(
        self,
        token: _QuotaSentinelToken,
    ) -> None:
        next_started = self._quota_clock()
        while True:
            with self._leases_lock:
                if self._quota_sentinel_token is not token:
                    return
                if not self._quota_dirty_leases:
                    self._quota_sentinel_token = None
                    return
            now = self._quota_clock()
            delay = max(0.0, next_started - now)
            if token.wake.wait(delay):
                token.wake.clear()
                continue
            scan_started = self._quota_clock()
            try:
                signature = self._quota_sentinel()
            except Exception:
                with self._leases_lock:
                    dirty = tuple(
                        (
                            self._leases.get(lease_id),
                            lease_token,
                        )
                        for lease_id, lease_token
                        in self._quota_dirty_leases.items()
                    )
                    if self._quota_sentinel_token is token:
                        self._quota_sentinel_token = None
                victims: list[_Execution] = []
                for lease, lease_token in dirty:
                    if lease is None:
                        continue
                    with lease.usage_lock:
                        if lease.quota_monitor_token is lease_token:
                            victims.extend(
                                self._poison_active_lease_locked(
                                    lease, lease_token
                                )
                            )
                for execution in victims:
                    execution.terminate("quota_exceeded")
                return
            with self._leases_lock:
                if self._quota_sentinel_token is not token:
                    return
                changed = signature != self._quota_sentinel_signature
                self._quota_sentinel_signature = signature
                if changed:
                    self._quota_sentinel_epoch += 1
                    monitors = tuple(self._quota_dirty_leases.values())
                else:
                    monitors = ()
            for monitor in monitors:
                monitor.wake.set()
            next_started = (
                scan_started + _QUOTA_SENTINEL_INTERVAL_SECONDS
            )

    def _update_quota_pressure_locked(
        self,
        lease: _Lease,
        *,
        previous_sample: tuple[int, int],
        previous_started: float,
    ) -> None:
        sample = lease.usage_sample
        if sample is None:
            raise ProtocolError("lease_usage_sample_missing")
        entry_ratio = sample[0] / self.policy.lease_quota_entries
        byte_ratio = sample[1] / self.policy.lease_quota_bytes
        elapsed = max(
            0.000001,
            lease.usage_sample_started_monotonic - previous_started,
        )
        projected: list[float] = []
        entry_rate = max(0.0, (sample[0] - previous_sample[0]) / elapsed)
        byte_rate = max(0.0, (sample[1] - previous_sample[1]) / elapsed)
        if entry_rate > 0:
            projected.append(
                max(0, self.policy.lease_quota_entries - sample[0])
                / entry_rate
            )
        if byte_rate > 0:
            projected.append(
                max(0, self.policy.lease_quota_bytes - sample[1])
                / byte_rate
            )
        projected_breach = (
            min(projected) if projected else float("inf")
        )
        if lease.quota_near_limit:
            lease.quota_near_limit = not (
                entry_ratio < _QUOTA_NEAR_LOW_WATERMARK
                and byte_ratio < _QUOTA_NEAR_LOW_WATERMARK
                and projected_breach >= _QUOTA_PROJECTED_BREACH_SECONDS
            )
        else:
            lease.quota_near_limit = (
                entry_ratio >= _QUOTA_NEAR_HIGH_WATERMARK
                or byte_ratio >= _QUOTA_NEAR_HIGH_WATERMARK
                or projected_breach < _QUOTA_PROJECTED_BREACH_SECONDS
            )

    def _quota_monitor_loop(
        self,
        lease: _Lease,
        token: _QuotaMonitorToken,
    ) -> None:
        try:
            self._quota_monitor_loop_inner(lease, token)
        except Exception:
            victims: list[_Execution] = []
            with lease.usage_lock:
                if lease.quota_monitor_token is token:
                    victims = self._poison_active_lease_locked(
                        lease, token
                    )
            for execution in victims:
                execution.terminate("quota_exceeded")

    def _quota_monitor_loop_inner(
        self,
        lease: _Lease,
        token: _QuotaMonitorToken,
    ) -> None:
        while True:
            victims: list[_Execution] = []
            wait_seconds = _QUOTA_SPARSE_FALLBACK_SECONDS
            with lease.usage_lock:
                if lease.quota_monitor_token is not token:
                    return
                if not lease.active_executions:
                    self._remove_quota_monitor_locked(lease, token)
                    return
                with self._leases_lock:
                    sentinel_epoch = self._quota_sentinel_epoch
                if sentinel_epoch != lease.quota_sentinel_epoch_seen:
                    lease.quota_sentinel_epoch_seen = sentinel_epoch
                    lease.quota_sentinel_dirty = True
                now = self._quota_clock()
                since_scan = max(
                    0.0, now - lease.quota_last_scan_started_monotonic
                )
                if lease.quota_near_limit:
                    due_after = _QUOTA_NEAR_SCAN_INTERVAL_SECONDS
                elif lease.quota_sentinel_dirty:
                    due_after = _QUOTA_NORMAL_SCAN_INTERVAL_SECONDS
                else:
                    due_after = _QUOTA_SPARSE_FALLBACK_SECONDS
                if since_scan >= due_after:
                    previous_sample = lease.usage_sample
                    if previous_sample is None:
                        victims = self._poison_active_lease_locked(
                            lease, token
                        )
                    else:
                        with self._leases_lock:
                            sentinel_before = self._quota_sentinel_epoch
                        previous_started = (
                            lease.usage_sample_started_monotonic
                        )
                        try:
                            self._scan_and_commit_lease_usage_locked(lease)
                            self._update_quota_pressure_locked(
                                lease,
                                previous_sample=previous_sample,
                                previous_started=previous_started,
                            )
                            with self._leases_lock:
                                sentinel_after = self._quota_sentinel_epoch
                            lease.quota_sentinel_epoch_seen = sentinel_after
                            lease.quota_sentinel_dirty = (
                                sentinel_after != sentinel_before
                            )
                        except Exception:
                            victims = self._poison_active_lease_locked(
                                lease, token
                            )
                if not victims and lease.quota_monitor_token is token:
                    now = self._quota_clock()
                    if lease.quota_near_limit:
                        interval = _QUOTA_NEAR_SCAN_INTERVAL_SECONDS
                    elif lease.quota_sentinel_dirty:
                        interval = _QUOTA_NORMAL_SCAN_INTERVAL_SECONDS
                    else:
                        interval = _QUOTA_SPARSE_FALLBACK_SECONDS
                    wait_seconds = max(
                        0.0,
                        lease.quota_last_scan_started_monotonic
                        + interval
                        - now,
                    )
            for execution in victims:
                execution.terminate("quota_exceeded")
            if victims:
                return
            token.wake.wait(wait_seconds)
            token.wake.clear()

    def _start_quota_monitor_locked(
        self,
        lease: _Lease,
    ) -> _QuotaMonitorToken:
        if lease.quota_monitor_token is not None:
            return lease.quota_monitor_token
        lease.usage_epoch += 1
        token = _QuotaMonitorToken(epoch=lease.usage_epoch)
        thread = threading.Thread(
            target=self._quota_monitor_loop,
            args=(lease, token),
            daemon=True,
        )
        token.thread = thread
        lease.quota_monitor_token = token
        with self._leases_lock:
            self._quota_dirty_leases[lease.lease_id] = token
            try:
                self._ensure_quota_sentinel_locked()
            except BaseException:
                self._quota_dirty_leases.pop(lease.lease_id, None)
                lease.quota_monitor_token = None
                raise
        try:
            thread.start()
        except BaseException:
            with self._leases_lock:
                if self._quota_dirty_leases.get(lease.lease_id) is token:
                    self._quota_dirty_leases.pop(lease.lease_id, None)
                sentinel = self._quota_sentinel_token
            lease.quota_monitor_token = None
            if sentinel is not None:
                sentinel.wake.set()
            raise
        return token

    @staticmethod
    def _peer_uid(connection: socket.socket) -> int:
        return _peer_credentials(connection)[0]

    def _validate_listener(self, listener: socket.socket) -> None:
        path = listener.getsockname()
        if not isinstance(path, str) or not path:
            raise ProtocolError("worker_listener_path_invalid")
        item = os.lstat(path)
        if (
            not stat.S_ISSOCK(item.st_mode)
            or stat.S_ISLNK(item.st_mode)
            or item.st_uid != self.policy.socket_uid
            or item.st_gid != self.policy.socket_gid
            or stat.S_IMODE(item.st_mode) != 0o660
        ):
            raise ProtocolError("worker_listener_identity_invalid")

    def serve_connection(self, connection: socket.socket) -> None:
        executions: dict[str, _Execution] = {}
        bound_lease: _Lease | None = None
        try:
            peer_uid, peer_gid = _peer_credentials(connection)
            if (
                peer_uid != self.policy.expected_peer_uid
                or peer_gid != self.policy.expected_peer_gid
            ):
                raise ProtocolError("peer_uid_not_authorized")
            reader = connection.makefile("rb", buffering=0)
            writer = connection.makefile("wb", buffering=0)
            while True:
                raw = _read_frame(reader)
                if raw is None:
                    break
                request = parse_request(raw)
                request_lease_id = str(request["lease_id"])
                if bound_lease is None:
                    bound_lease = self._ensure_lease(request_lease_id)
                    with self._leases_lock:
                        bound_lease.connections += 1
                elif request_lease_id != bound_lease.lease_id:
                    raise ProtocolError("connection_lease_changed")
                with self._leases_lock:
                    self._touch_lease_locked(bound_lease)
                request_id = str(request["request_id"])
                with self._replay_lock:
                    cached = self._replay.get(request_id)
                    if cached is not None:
                        if cached[0] != raw:
                            raise ProtocolError("request_id_reused")
                        response = cached[1]
                    else:
                        response = b""
                if cached is not None:
                    _write_frame(writer, response)
                    continue
                response = self._dispatch(
                    request,
                    executions,
                    bound_lease,
                )
                with self._replay_lock:
                    if len(self._replay) >= MAX_REQUEST_CACHE:
                        self._replay.pop(next(iter(self._replay)))
                    self._replay[request_id] = (raw, response)
                _write_frame(writer, response)
        finally:
            for execution in tuple(executions.values()):
                execution.terminate("disconnected")
                execution.complete.wait(2)
            if bound_lease is not None:
                with self._leases_lock:
                    bound_lease.connections = max(0, bound_lease.connections - 1)
                    bound_lease.jobs = max(0, bound_lease.jobs - len(executions))
                    self._touch_lease_locked(bound_lease)
            try:
                connection.close()
            except OSError:
                pass

    def serve(self, listener: socket.socket, stop: threading.Event) -> None:
        self._validate_listener(listener)
        self._attest_quota_topology()
        self._quota_topology_attested = True
        # Startup readiness is fail-closed when recent persisted workspaces are
        # already above the service-wide quota.  Expired leases are reclaimed
        # first so a clean restart does not require operator intervention.
        self.reap_expired()
        self._global_usage()
        listener.settimeout(0.1)
        reap_interval = min(60.0, max(1.0, self.policy.lease_ttl_seconds / 2))
        next_reap = time.monotonic() + reap_interval
        while not stop.is_set():
            try:
                connection, _address = listener.accept()
            except socket.timeout:
                if time.monotonic() >= next_reap:
                    self.reap_expired()
                    next_reap = time.monotonic() + reap_interval
                continue

            with self._threads_lock:
                if len(self._threads) >= MAX_ACTIVE_CONNECTIONS:
                    connection.close()
                    continue

            def run_connection() -> None:
                try:
                    self.serve_connection(connection)
                finally:
                    with self._threads_lock:
                        self._threads.discard(threading.current_thread())

            thread = threading.Thread(target=run_connection, daemon=True)
            with self._threads_lock:
                self._threads.add(thread)
            thread.start()
        with self._threads_lock:
            threads = tuple(self._threads)
        for thread in threads:
            thread.join(timeout=2)

    def _dispatch(
        self,
        request: Mapping[str, Any],
        executions: dict[str, _Execution],
        bound_lease: _Lease,
    ) -> bytes:
        try:
            operation = request["operation"]
            params = request["parameters"]
            if operation == "exec.start":
                result = self._start(bound_lease, params, executions)
            elif operation == "exec.poll":
                result = self._poll(request["lease_id"], params, executions)
            elif operation == "exec.cancel":
                result = self._cancel(request["lease_id"], params, executions)
            elif operation == "proof.status":
                result = {
                    "proof_receipt": self._proof_status_receipt(
                        bound_lease
                    )
                }
            else:
                result = {
                    "proof_receipt": self._proof_mark_edited(
                        bound_lease,
                        params["paths"],
                        params["observed_generation"],
                    )
                }
            return _response(request, ok=True, result=result)
        except ProtocolError as exc:
            return _response(
                request,
                ok=False,
                result={"error_code": str(exc) or "protocol_error"},
            )
        except OSError:
            return _response(request, ok=False, result={"error_code": "worker_os_error"})
        except ValueError:
            return _response(request, ok=False, result={"error_code": "worker_value_error"})

    def _start(
        self,
        lease: _Lease,
        params: Mapping[str, Any],
        executions: dict[str, _Execution],
    ) -> Mapping[str, Any]:
        if len(executions) >= MAX_ACTIVE_JOBS_PER_CONNECTION:
            raise ProtocolError("active_job_limit_reached")
        request_virtual_cwd = self._validate_cwd(lease, params["cwd"])
        executed_virtual_cwd = _executed_virtual_cwd(
            params["command"],
            request_virtual_cwd,
        )
        self._validate_cwd(lease, str(executed_virtual_cwd))
        host_cwd = lease.root / executed_virtual_cwd.relative_to(
            VIRTUAL_WORKSPACE_ROOT
        )
        with lease.proof_lock:
            proof_state = self._read_proof_state(lease)
            try:
                pre_snapshot = _material_snapshot(lease.root, host_cwd)
            except (OSError, ProtocolError, ValueError):
                pre_snapshot = None
            proof_state = self._reconcile_material_state(
                lease,
                proof_state,
                scan_cwd=host_cwd,
                snapshot=pre_snapshot,
            )
        timeout = min(
            params["timeout_seconds"], self.policy.maximum_timeout_seconds
        )
        stdin = base64.b64decode(params["stdin_b64"], validate=True)
        # Exact allowlist: never merge os.environ or skill-declared
        # environment/credential-file registration.
        environment = {
            "HOME": str(lease.root),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "TMPDIR": str(lease.root),
        }
        session_id = f"job-{uuid.uuid4().hex}"
        execution: _Execution | None = None

        def drain(stream, target: bytearray, done: threading.Event) -> None:
            try:
                while True:
                    chunk = stream.read(8192)
                    if not chunk:
                        return
                    with execution.lock:
                        remaining = execution.output_limit - len(execution.stdout) - len(execution.stderr)
                        if remaining <= 0:
                            overflow = True
                        else:
                            target.extend(chunk[:remaining])
                            overflow = len(chunk) > remaining
                    if overflow:
                        execution.terminate("output_limit")
                        return
            finally:
                try:
                    stream.close()
                except OSError:
                    pass
                done.set()

        def feed() -> None:
            assert execution is not None
            assert process.stdin is not None
            try:
                process.stdin.write(stdin)
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        def monitor_execution() -> None:
            assert execution is not None
            try:
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        execution.terminate("timed_out")
                        break
                    time.sleep(0.05)
                process.wait()
                with lease.usage_lock:
                    try:
                        lease.active_executions.remove(execution)
                    except ValueError:
                        pass
                    quota_token = lease.quota_monitor_token
                    if lease.active_executions:
                        if quota_token is not None:
                            quota_token.wake.set()
                    else:
                        # The last writer pays exactly one reconciliation
                        # before its completion becomes observable.
                        try:
                            self._scan_and_commit_lease_usage_locked(lease)
                            if lease.usage_state != _USAGE_POISONED:
                                lease.usage_state = _USAGE_EXACT_IDLE
                                lease.quota_near_limit = False
                                lease.quota_sentinel_dirty = False
                        except Exception:
                            self._mark_usage_poisoned_locked(lease)
                            with execution.lock:
                                if execution.state == "running":
                                    execution.state = "quota_exceeded"
                        if quota_token is not None:
                            self._remove_quota_monitor_locked(
                                lease, quota_token
                            )
                with execution.lock:
                    if execution.state == "running":
                        execution.state = "exited"
            finally:
                execution.stdout_complete.wait(2)
                execution.stderr_complete.wait(2)
                execution.complete.set()

        process: subprocess.Popen[bytes]
        with lease.usage_lock:
            if lease.usage_state == _USAGE_POISONED:
                raise ProtocolError("lease_usage_poisoned")
            # Completed output remains reserved until its owning connection
            # drains/polls it (or disconnect cleanup runs).  Active process
            # membership drives quota monitoring, but the durable reservation
            # bounds retained execution/output objects across connections.
            if lease.jobs >= self.policy.maximum_active_jobs_per_lease:
                raise ProtocolError("lease_job_capacity_exhausted")
            with self._leases_lock:
                if self._leases.get(lease.lease_id) is not lease:
                    raise ProtocolError("lease_usage_identity_changed")
                self._cached_global_usage_locked()
            process = self._spawn_sandboxed(
                lease=lease,
                virtual_cwd=request_virtual_cwd,
                command=params["command"],
                environment=environment,
            )
            execution = _Execution(
                lease=lease,
                process=process,
                timeout_seconds=timeout,
                output_limit=self.policy.maximum_output_bytes,
                command=params["command"],
                pre_snapshot=pre_snapshot,
                host_cwd=host_cwd,
            )
            lease.active_executions.append(execution)
            if lease.usage_state == _USAGE_EXACT_IDLE:
                lease.usage_state = _USAGE_DIRTY_ACTIVE
                lease.quota_last_scan_started_monotonic = (
                    self._quota_clock()
                )
                with self._leases_lock:
                    lease.quota_sentinel_epoch_seen = (
                        self._quota_sentinel_epoch
                    )
                lease.quota_sentinel_dirty = False
                lease.quota_near_limit = False
            elif lease.usage_state != _USAGE_DIRTY_ACTIVE:
                lease.active_executions.remove(execution)
                execution.terminate("quota_exceeded")
                process.wait()
                raise ProtocolError("lease_usage_state_invalid")
            try:
                self._start_quota_monitor_locked(lease)
            except BaseException as exc:
                try:
                    lease.active_executions.remove(execution)
                except ValueError:
                    pass
                self._mark_usage_poisoned_locked(lease)
                execution.terminate("quota_exceeded")
                process.wait()
                raise ProtocolError("quota_monitor_start_failed") from exc
            executions[session_id] = execution
            try:
                assert process.stdout is not None and process.stderr is not None
                threading.Thread(
                    target=drain,
                    args=(
                        process.stdout,
                        execution.stdout,
                        execution.stdout_complete,
                    ),
                    daemon=True,
                ).start()
                threading.Thread(
                    target=drain,
                    args=(
                        process.stderr,
                        execution.stderr,
                        execution.stderr_complete,
                    ),
                    daemon=True,
                ).start()
                threading.Thread(target=feed, daemon=True).start()
                threading.Thread(
                    target=monitor_execution,
                    daemon=True,
                ).start()
            except BaseException as exc:
                executions.pop(session_id, None)
                token = lease.quota_monitor_token
                victims = self._poison_active_lease_locked(
                    lease,
                    token,
                )
                try:
                    lease.active_executions.remove(execution)
                except ValueError:
                    pass
                for victim in victims:
                    victim.terminate("quota_exceeded")
                process.wait()
                raise ProtocolError("execution_monitor_start_failed") from exc
            with self._leases_lock:
                lease.jobs += 1
                self._touch_lease_locked(lease)
        return {"session_id": session_id, "state": "running"}

    def _spawn_sandboxed(
        self,
        *,
        lease: _Lease,
        virtual_cwd: Path,
        command: str,
        environment: Mapping[str, str],
    ) -> subprocess.Popen[bytes]:
        """Launch through the exact verified bwrap inode; never raw-fallback."""

        lease_descriptor = -1
        descriptor = -1
        shell_descriptor = -1
        try:
            lease_descriptor = os.open(
                lease.lease_id,
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._lease_base_fd,
            )
            descriptor = os.open(
                self.policy.bwrap_path,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            shell_descriptor = os.open(
                self.policy.shell,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            self._validate_read_only_binds()
            opened = _verify_open_regular_digest(
                self.policy.bwrap_path,
                descriptor,
                expected_sha256=self.policy.bwrap_sha256,
                expected_uid=self.policy.bwrap_uid,
            )
            shell_opened = _verify_open_regular_digest(
                self.policy.shell,
                shell_descriptor,
                expected_sha256=self.policy.shell_sha256,
                expected_uid=self.policy.shell_uid,
            )
            current = os.lstat(self.policy.bwrap_path)
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise ProtocolError("bwrap_changed_before_exec")
            shell_current = os.lstat(self.policy.shell)
            if (shell_opened.st_dev, shell_opened.st_ino) != (
                shell_current.st_dev,
                shell_current.st_ino,
            ):
                raise ProtocolError("shell_changed_before_exec")
            executable = f"/proc/self/fd/{descriptor}"
            arguments = [
                executable,
                "--die-with-parent",
                "--new-session",
                "--unshare-all",
                "--cap-drop",
                "ALL",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--dir",
                "/run",
                "--dir",
                "/opt",
                "--dir",
                str(VIRTUAL_READ_ONLY_ROOT),
                "--ro-bind-fd",
                str(shell_descriptor),
                str(VIRTUAL_SHELL_PATH),
                "--tmpfs",
                "/tmp",
                "--dir",
                str(VIRTUAL_WORKSPACE_ROOT),
            ]
            for root in self.policy.runtime_roots:
                if root.exists():
                    arguments.extend(("--ro-bind", str(root), str(root)))
            for bind, bind_descriptor, _identity in self._read_only_bind_fds:
                arguments.extend(
                    (
                        "--ro-bind",
                        f"/proc/self/fd/{bind_descriptor}",
                        str(bind.destination),
                    )
                )
            arguments.extend(
                (
                    "--bind",
                    f"/proc/self/fd/{lease_descriptor}",
                    str(VIRTUAL_WORKSPACE_ROOT),
                    "--chdir",
                    str(virtual_cwd),
                    "--clearenv",
                )
            )
            for key, value in sorted(environment.items()):
                virtual_value = (
                    str(VIRTUAL_WORKSPACE_ROOT)
                    if value == str(lease.root)
                    else value
                )
                arguments.extend(("--setenv", key, virtual_value))
            arguments.extend(
                (
                    "--",
                    str(VIRTUAL_SHELL_PATH),
                    "--noprofile",
                    "--norc",
                    "-c",
                    command,
                )
            )
            return subprocess.Popen(
                arguments,
                env={},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(
                    descriptor,
                    shell_descriptor,
                    lease_descriptor,
                    *(item[1] for item in self._read_only_bind_fds),
                ),
                start_new_session=True,
            )
        finally:
            for opened_descriptor in (
                shell_descriptor,
                descriptor,
                lease_descriptor,
            ):
                if opened_descriptor >= 0:
                    os.close(opened_descriptor)

    def _finalize_execution_proof(self, execution: _Execution) -> dict[str, Any]:
        with execution.lock:
            if execution.proof_finalized:
                if execution.proof_receipt is None:
                    raise ProtocolError("proof_receipt_missing")
                return dict(execution.proof_receipt)
            execution.proof_finalized = True

        lease = execution.lease
        # Admission owns usage_lock before spawning.  Holding the same lock
        # through the final material snapshot and proof-state write prevents
        # a new writer from entering the workspace between those operations.
        # An already-active sibling is not waited on: its writable sandbox
        # makes this verification intrinsically non-authoritative.
        with lease.usage_lock, lease.proof_lock:
            sibling_writable = bool(lease.active_executions)
            state = self._read_proof_state(lease)
            changed_paths: list[str] = []
            mutation_detection = "unchanged"
            post_snapshot: _MaterialSnapshot | None = None
            try:
                if sibling_writable:
                    raise ProtocolError("proof_sibling_writer_active")
                post_snapshot = _material_snapshot(
                    lease.root,
                    execution.host_cwd,
                )
                post_fingerprint = _material_fingerprint(post_snapshot)
                if execution.pre_snapshot is not None:
                    changed_paths = _changed_material_paths(
                        execution.pre_snapshot,
                        post_snapshot,
                    )
                    if changed_paths:
                        mutation_detection = "changed"
                else:
                    prior_fingerprint = str(
                        state.get("material_fingerprint") or ""
                    )
                    if not prior_fingerprint:
                        mutation_detection = "unknown"
                        changed_paths = [
                            post_snapshot.scope
                            or str(VIRTUAL_WORKSPACE_ROOT)
                        ]
                    elif prior_fingerprint != post_fingerprint:
                        mutation_detection = "changed"
                        changed_paths = [
                            post_snapshot.scope
                            or str(VIRTUAL_WORKSPACE_ROOT)
                        ]
            except (OSError, ProtocolError, ValueError):
                mutation_detection = "unknown"
                changed_paths = [str(VIRTUAL_WORKSPACE_ROOT)]

            if mutation_detection in {"changed", "unknown"}:
                state["edit_generation"] += 1
                state["pending_paths"] = self._merge_proof_paths(
                    state["pending_paths"],
                    changed_paths,
                )

            state["material_fingerprint"] = (
                _material_fingerprint(post_snapshot)
                if post_snapshot is not None
                else ""
            )
            state = self._write_proof_state(lease, state)
            receipt = self._proof_receipt(
                state,
                mutation_detection=mutation_detection,
                changed_paths=changed_paths,
            )
        with execution.lock:
            execution.proof_receipt = dict(receipt)
        return receipt

    def _poll(
        self,
        lease_id: str,
        params: Mapping[str, Any],
        executions: dict[str, _Execution],
    ) -> Mapping[str, Any]:
        session_id = params["session_id"]
        execution = executions.get(session_id)
        if execution is None or execution.lease.lease_id != lease_id:
            raise ProtocolError("session_not_authorized")
        execution.complete.wait(params["wait_milliseconds"] / 1000)
        with execution.lock:
            stdout_end = min(
                len(execution.stdout), execution.stdout_sent + MAX_POLL_CHUNK_BYTES
            )
            stderr_end = min(
                len(execution.stderr), execution.stderr_sent + MAX_POLL_CHUNK_BYTES
            )
            stdout = bytes(execution.stdout[execution.stdout_sent:stdout_end])
            stderr = bytes(execution.stderr[execution.stderr_sent:stderr_end])
            execution.stdout_sent = stdout_end
            execution.stderr_sent = stderr_end
            state = execution.state
            returncode = execution.process.poll()
            drained = stdout_end == len(execution.stdout) and stderr_end == len(execution.stderr)
            complete = execution.complete.is_set()
        result = {
            "session_id": session_id,
            "state": state,
            "returncode": returncode,
            "stdout_b64": base64.b64encode(stdout).decode("ascii"),
            "stderr_b64": base64.b64encode(stderr).decode("ascii"),
            "drained": drained,
            "complete": complete,
        }
        if state != "running" and drained and complete:
            result["proof_receipt"] = self._finalize_execution_proof(execution)
            executions.pop(session_id, None)
            with self._leases_lock:
                execution.lease.jobs = max(0, execution.lease.jobs - 1)
                self._touch_lease_locked(execution.lease)
        return result

    def _cancel(
        self,
        lease_id: str,
        params: Mapping[str, Any],
        executions: dict[str, _Execution],
    ) -> Mapping[str, Any]:
        session_id = params["session_id"]
        execution = executions.get(session_id)
        if execution is None or execution.lease.lease_id != lease_id:
            raise ProtocolError("session_not_authorized")
        execution.terminate("cancelled")
        execution.complete.wait(1)
        return {"session_id": session_id, "state": execution.state}


class IsolatedWorkerClient:
    """No-fallback client for one lease-bound worker connection."""

    def __init__(
        self,
        socket_path: Path,
        *,
        lease_id: str,
        expected_server_uid: int,
        expected_server_gid: int,
        expected_socket_uid: int,
        expected_socket_gid: int,
    ):
        if _ID.fullmatch(lease_id) is None:
            raise ValueError("lease_id_invalid")
        self.socket_path = Path(socket_path)
        self.lease_id = lease_id
        self.expected_server_uid = expected_server_uid
        self.expected_server_gid = expected_server_gid
        self.expected_socket_uid = expected_socket_uid
        self.expected_socket_gid = expected_socket_gid
        self._socket: socket.socket | None = None
        self._reader = None
        self._writer = None
        self._lock = threading.Lock()

    def connect(self) -> None:
        item = os.lstat(self.socket_path)
        if (
            not stat.S_ISSOCK(item.st_mode)
            or stat.S_ISLNK(item.st_mode)
            or item.st_uid != self.expected_socket_uid
            or item.st_gid != self.expected_socket_gid
            or stat.S_IMODE(item.st_mode) != 0o660
        ):
            raise ProtocolError("worker_socket_invalid")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.connect(str(self.socket_path))
        uid, gid = _peer_credentials(connection)
        if uid != self.expected_server_uid or gid != self.expected_server_gid:
            connection.close()
            raise ProtocolError("worker_server_uid_invalid")
        self._socket = connection
        self._reader = connection.makefile("rb", buffering=0)
        self._writer = connection.makefile("wb", buffering=0)

    def close(self) -> None:
        if self._socket is not None:
            try:
                if self._writer is not None:
                    self._writer.close()
                if self._reader is not None:
                    self._reader.close()
                self._socket.close()
            finally:
                self._socket = None
                self._reader = None
                self._writer = None

    def request(self, operation: str, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
        if operation not in _PARAMETER_FIELDS:
            raise ValueError("operation_invalid")
        if self._socket is None:
            self.connect()
        request = {
            "schema": REQUEST_SCHEMA,
            "protocol": PROTOCOL,
            "request_id": uuid.uuid4().hex,
            "lease_id": self.lease_id,
            "operation": operation,
            "parameters": dict(parameters),
        }
        payload = canonical_bytes(request)
        parse_request(payload)
        with self._lock:
            _write_frame(self._writer, payload)
            raw = _read_frame(self._reader)
        if raw is None:
            raise ProtocolError("worker_disconnected")
        try:
            response = json.loads(raw.decode("ascii", errors="strict"))
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ProtocolError("response_invalid") from exc
        if canonical_bytes(response) != raw or set(response) != {
            "schema", "protocol", "request_id", "lease_id", "operation", "ok", "result"
        }:
            raise ProtocolError("response_invalid")
        if (
            response["schema"] != RESPONSE_SCHEMA
            or response["protocol"] != PROTOCOL
            or response["request_id"] != request["request_id"]
            or response["lease_id"] != self.lease_id
            or response["operation"] != operation
            or type(response["ok"]) is not bool
            or not isinstance(response["result"], Mapping)
        ):
            raise ProtocolError("response_identity_invalid")
        if not response["ok"]:
            raise ProtocolError(str(response["result"].get("error_code", "worker_error")))
        return dict(response["result"])

    def start(self, command: str, *, cwd: Path, timeout_seconds: int, stdin: bytes = b"") -> str:
        result = self.request(
            "exec.start",
            {
                "command": command,
                "cwd": str(cwd),
                "stdin_b64": base64.b64encode(stdin).decode("ascii"),
                "timeout_seconds": timeout_seconds,
            },
        )
        return str(result["session_id"])

    def poll(self, session_id: str, *, wait_milliseconds: int = 100) -> Mapping[str, Any]:
        result = self.request(
            "exec.poll",
            {"session_id": session_id, "wait_milliseconds": wait_milliseconds},
        )
        if (
            result.get("state") != "running"
            and result.get("drained") is True
            and result.get("complete") is True
        ):
            if "proof_receipt" not in result:
                raise ProtocolError("proof_receipt_missing")
            result["proof_receipt"] = _validate_proof_receipt(
                result["proof_receipt"],
                self.lease_id,
            )
        elif "proof_receipt" in result:
            raise ProtocolError("proof_receipt_unexpected")
        return result

    def cancel(self, session_id: str) -> Mapping[str, Any]:
        return self.request("exec.cancel", {"session_id": session_id})

    def proof_status(self) -> Mapping[str, Any]:
        result = self.request("proof.status", {})
        if set(result) != {"proof_receipt"}:
            raise ProtocolError("proof_status_result_invalid")
        return _validate_proof_receipt(result["proof_receipt"], self.lease_id)

    def mark_edited(
        self,
        paths: Sequence[str],
        *,
        observed_generation: int | None = None,
    ) -> Mapping[str, Any]:
        result = self.request(
            "proof.mark_edited",
            {
                "paths": list(paths),
                "observed_generation": observed_generation,
            },
        )
        if set(result) != {"proof_receipt"}:
            raise ProtocolError("proof_mark_result_invalid")
        receipt = _validate_proof_receipt(result["proof_receipt"], self.lease_id)
        if receipt["mutation_detection"] != "explicit":
            raise ProtocolError("proof_mark_receipt_invalid")
        return receipt


__all__ = [
    "IsolatedWorkerClient",
    "IsolatedWorkerServer",
    "MAX_ACTIVE_CONNECTIONS",
    "MAX_FRAME_BYTES",
    "PROTOCOL",
    "PROOF_RECEIPT_SCHEMA",
    "PROOF_STATE_SCHEMA",
    "ProtocolError",
    "ReadOnlyBind",
    "REQUEST_SCHEMA",
    "RESPONSE_SCHEMA",
    "WorkerPolicy",
    "HOST_READ_ONLY_ROOT",
    "canonical_bytes",
    "canonical_lease_id",
    "parse_request",
]
