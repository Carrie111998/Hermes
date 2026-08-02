"""One-shot, provenance-locked SQLite to PostgreSQL history importer.

This module is deliberately not runtime wired.  The CLI obtains its secret DSN
from ``HERMES_POSTGRES_MIGRATION_DSN`` (or stdin); it never accepts it in argv.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import errno
import fcntl
import hashlib
import importlib.resources
import inspect
import os
import json
import math
import shutil
import signal
import sqlite3
import ssl
import stat
import struct
import sys
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote_to_bytes, urlsplit

DEFAULT_ROW_BYTES = 128 * 1024 * 1024
DEFAULT_BATCH_BYTES = 16 * 1024 * 1024
DEFAULT_CEILING_BYTES = 536_870_912
DEFAULT_SAFETY_MARGIN_BYTES = 30_875_648
DEFAULT_STAGE_MARGIN_BYTES = 64 * 1024 * 1024
MIN_COST_MULTIPLIER = 1.5
FIXED_ROW_OVERHEAD_BYTES = 128
@dataclass(frozen=True)
class SnapshotApproval:
    """The complete, immutable approval required to import a snapshot pair."""

    state_sha256: str
    agent_logs_sha256: str
    state_schema_fingerprint: str
    agent_logs_schema_fingerprint: str


# The production snapshot values are intentionally literal approval gates.
_PRODUCTION_STATE_SHA256 = "31e24563b32e095c1cbcc9ef14c5f231c6a96618c2d50ad73e5be72e051a0a4a"
_PRODUCTION_AGENT_LOGS_SHA256 = "e3db70bf54765db22f8d7bb3d96d8ac6539fe0480dc45936357de768a5f48ae3"
# These approvals bind the exact bytes of the offline, canonical snapshot main
# files.  This is not a SQLite logical backup importer: a renamed WAL is never
# claimed to be imported.  Canonical <source>-wal checks reject visible pending
# WALs, while the approved staged main-file bytes are the migration authority.
# SHA-256 of canonical JSON fingerprints generated from the verified snapshots
# above. FTS virtual tables, their SQLite implementation tables, and FTS
# triggers are intentionally excluded because they are derived search indexes.
_PRODUCTION_STATE_SCHEMA_FINGERPRINT = "17f71bcd91b10b05520fe283f73aadb6502df5ba333881f4a9ed510e1e3a6dd5"
_PRODUCTION_AGENT_LOGS_SCHEMA_FINGERPRINT = "74b09b1c5bb16604bcf98f7ce5b3860cbca6e6bfb05a9cb764b0d442795cd029"
PRODUCTION_APPROVAL = SnapshotApproval(
    _PRODUCTION_STATE_SHA256,
    _PRODUCTION_AGENT_LOGS_SHA256,
    _PRODUCTION_STATE_SCHEMA_FINGERPRINT,
    _PRODUCTION_AGENT_LOGS_SCHEMA_FINGERPRINT,
)
# Compatibility/display aliases only. Production paths use PRODUCTION_APPROVAL,
# never these independently reassignable module names.
PRODUCTION_STATE_SHA256 = PRODUCTION_APPROVAL.state_sha256
PRODUCTION_AGENT_LOGS_SHA256 = PRODUCTION_APPROVAL.agent_logs_sha256
PRODUCTION_STATE_SCHEMA_FINGERPRINT = PRODUCTION_APPROVAL.state_schema_fingerprint
PRODUCTION_AGENT_LOGS_SCHEMA_FINGERPRINT = PRODUCTION_APPROVAL.agent_logs_schema_fingerprint
# Generated from a fresh PostgreSQL 17 application of 0001_hermes_hot.sql.
PRODUCTION_TARGET_SCHEMA_FINGERPRINT = "09f372108ca765cea0645ddc04ea7ea2522ac3bfb07bcabc7f1f688c7f286335"
EXPECTED_DOMAIN_CHECK_DEFINITION = "CHECK ((domain = ANY (ARRAY['sessions'::text, 'messages'::text, 'session_model_usage'::text, 'async_delegations'::text, 'compression_locks'::text, 'gateway_routing'::text, 'state_meta'::text, 'agent_logs'::text])))"

TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
 "sessions": ("id","source","user_id","session_key","chat_id","chat_type","thread_id","model","model_config","system_prompt","parent_session_id","started_at","ended_at","end_reason","message_count","tool_call_count","input_tokens","output_tokens","cache_read_tokens","cache_write_tokens","reasoning_tokens","cwd","git_branch","git_repo_root","billing_provider","billing_base_url","billing_mode","estimated_cost_usd","actual_cost_usd","cost_status","cost_source","pricing_version","title","api_call_count","handoff_state","handoff_platform","handoff_error","compression_failure_cooldown_until","compression_failure_error","rewind_count","archived","display_name","origin_json","expiry_finalized","compression_fallback_streak","compression_ineffective_count","profile_name","pinned"),
 "messages": ("id","session_id","role","content","tool_call_id","tool_calls","tool_name","timestamp","token_count","finish_reason","reasoning","reasoning_content","reasoning_details","codex_reasoning_items","codex_message_items","platform_message_id","observed","active","compacted","effect_disposition","api_content","display_kind","display_metadata"),
 "session_model_usage": ("session_id","model","billing_provider","billing_base_url","billing_mode","task","api_call_count","input_tokens","output_tokens","cache_read_tokens","cache_write_tokens","reasoning_tokens","estimated_cost_usd","actual_cost_usd","cost_status","cost_source","first_seen","last_seen"),
 "async_delegations": ("delegation_id","origin_session","origin_ui_session_id","parent_session_id","state","dispatched_at","completed_at","updated_at","event_json","result_json","delivery_state","delivery_attempts","delivered_at","owner_pid","owner_started_at","task_json","delivery_claim","delivery_claimed_at","origin_session_id"),
 "compression_locks": ("session_id","holder","acquired_at","expires_at"), "gateway_routing": ("scope","session_key","entry_json","updated_at"), "state_meta": ("key","value"), "agent_logs": ("id","agent_name","task_description","model_used","status","created_at","trace_id","span_id"),
}
TARGET_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "sessions": frozenset({"id", "source", "started_at", "rewind_count", "archived", "compression_fallback_streak", "compression_ineffective_count", "pinned"}),
    "messages": frozenset({"id", "session_id", "role", "timestamp", "active", "compacted"}),
    "session_model_usage": frozenset({"session_id", "model", "billing_provider", "billing_base_url", "billing_mode", "task", "api_call_count", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "estimated_cost_usd", "actual_cost_usd"}),
    "async_delegations": frozenset({"delegation_id", "origin_session", "origin_ui_session_id", "state", "dispatched_at", "updated_at", "delivery_state", "delivery_attempts"}),
    "compression_locks": frozenset({"session_id", "holder", "acquired_at", "expires_at"}),
    "gateway_routing": frozenset({"scope", "session_key", "entry_json", "updated_at"}),
    "state_meta": frozenset({"key"}),
    "agent_logs": frozenset({"id", "agent_name", "task_description", "status", "created_at"}),
}
@dataclass(frozen=True)
class DomainSpec:
    columns: tuple[str, ...]
    order_keys: tuple[tuple[str, str], ...]

# Order-key kinds are domain-local: a column called ``id`` is not universally
# textual (messages.id is SQLite INTEGER / PostgreSQL bigint).
DOMAIN_SPECS = {
    name: DomainSpec(columns, ((columns[0], "integer" if name == "messages" else "text"),))
    for name, columns in TABLE_COLUMNS.items()
}
DOMAIN_SPECS["session_model_usage"] = DomainSpec(TABLE_COLUMNS["session_model_usage"], (("session_id", "text"), ("model", "text"), ("billing_provider", "text"), ("billing_base_url", "bytes"), ("billing_mode", "text"), ("task", "text")))
DOMAIN_SPECS["gateway_routing"] = DomainSpec(TABLE_COLUMNS["gateway_routing"], (("scope", "text"), ("session_key", "text")))
BINARY_COLUMNS = frozenset({"model_config","system_prompt","cwd","git_repo_root","billing_base_url","title","handoff_error","compression_failure_error","display_name","origin_json","content","tool_calls","reasoning","reasoning_content","reasoning_details","codex_reasoning_items","codex_message_items","api_content","display_metadata","event_json","result_json","task_json","entry_json","value","task_description"})
TIMESTAMP_COLUMNS = {"sessions":"started_at", "messages":"timestamp", "session_model_usage":"first_seen", "async_delegations":"dispatched_at", "compression_locks":"acquired_at", "gateway_routing":"updated_at"}

class MigrationError(RuntimeError):
    """Stable, deliberately non-sensitive failure."""


_LEASE_SIGNAL_LOCK = threading.Lock()
_LEASE_SIGNAL_OFFSET = 1

@dataclass(frozen=True, repr=False)
class TargetConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    ssl: ssl.SSLContext | bool
    def __repr__(self) -> str:
        return "TargetConfig(password=<redacted>)"


@dataclass(frozen=True)
class ArtifactIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int

@dataclass(frozen=True)
class ScratchStage:
    path: Path
    parent_fd: int
    parent_identity: tuple[int, int]
    identity: tuple[int, int]
    stage_fd: int
    stage_name: str
    files: dict[str, tuple[int, int, int, int]]

@dataclass(frozen=True)
class DomainManifest:
    name: str
    columns: tuple[str, ...]
    digest: str
    row_count: int
    nul_values: int
    payload_bytes: int
    min_id: str | int | None
    max_id: str | int | None
    min_timestamp: float | None
    max_timestamp: float | None
    source_sha256: str

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as f:
            while chunk := f.read(1024 * 1024): digest.update(chunk)
    except OSError as exc: raise MigrationError("source artifact cannot be read") from exc
    return digest.hexdigest()


def _artifact_identity(path: Path) -> ArtifactIdentity:
    try:
        stat = path.stat()
    except OSError as exc:
        raise MigrationError("source artifact cannot be read") from exc
    if not path.is_file():
        raise MigrationError("source artifact is not a regular file")
    return ArtifactIdentity(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _require_artifact_identity(path: Path, expected: ArtifactIdentity) -> None:
    if _artifact_identity(path) != expected:
        raise MigrationError("source artifact changed during migration")

def _open_safe_scratch_dir(path: Path) -> tuple[int, tuple[int, int]]:
    """Pin a private scratch parent before creating any cleanup-able path."""
    try:
        before = os.lstat(path)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise MigrationError("scratch directory is unsafe")
        mode = stat.S_IMODE(before.st_mode)
        if before.st_uid != os.getuid() or mode & 0o077 or mode & 0o500 != 0o500:
            raise MigrationError("scratch directory is unsafe")
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
        after = os.fstat(fd)
        identity = (before.st_dev, before.st_ino)
        if (after.st_dev, after.st_ino) != identity:
            os.close(fd)
            raise MigrationError("scratch directory is unsafe")
        return fd, identity
    except MigrationError:
        raise
    except OSError as exc:
        raise MigrationError("scratch directory is unavailable") from exc

def _safe_scratch_dir(path: Path) -> None:
    """Compatibility validator; callers that create stages must retain the fd."""
    fd, _ = _open_safe_scratch_dir(path)
    os.close(fd)


def _create_stage(parent_fd: int, scratch_dir: Path) -> ScratchStage:
    """Create and pin a private stage through the already-verified parent fd."""
    for _ in range(128):
        name = f"hermes-hot-stage-{uuid.uuid4().hex}"
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        except OSError as exc:
            raise MigrationError("scratch directory is unavailable") from exc
        try:
            stage_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            info = os.fstat(stage_fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                os.close(stage_fd)
                raise MigrationError("scratch directory is unsafe")
            parent = os.fstat(parent_fd)
            return ScratchStage(
                scratch_dir / name,
                parent_fd,
                (parent.st_dev, parent.st_ino),
                (info.st_dev, info.st_ino),
                stage_fd,
                name,
                {},
            )
        except Exception:
            with contextlib.suppress(OSError):
                os.rmdir(name, dir_fd=parent_fd)
            raise
    raise MigrationError("scratch directory is unavailable")


def _cleanup_stage(stage: ScratchStage) -> None:
    """Remove only descriptor-pinned files; never recursively delete a path."""
    try:
        parent = os.fstat(stage.parent_fd)
        named_stage = os.stat(stage.stage_name, dir_fd=stage.parent_fd, follow_symlinks=False)
        if ((parent.st_dev, parent.st_ino) != stage.parent_identity
                or (named_stage.st_dev, named_stage.st_ino) != stage.identity):
            raise MigrationError("cleanup safety error; stage evidence retained")
        if set(os.listdir(stage.stage_fd)) != set(stage.files):
            raise MigrationError("cleanup safety error; stage evidence retained")
        for name, expected in stage.files.items():
            info = os.stat(name, dir_fd=stage.stage_fd, follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode)
                    or (info.st_dev, info.st_ino, info.st_nlink, info.st_uid) != expected):
                raise MigrationError("cleanup safety error; stage evidence retained")
        for name in stage.files:
            os.unlink(name, dir_fd=stage.stage_fd)
        os.fsync(stage.stage_fd)
        named_stage = os.stat(stage.stage_name, dir_fd=stage.parent_fd, follow_symlinks=False)
        if (named_stage.st_dev, named_stage.st_ino) != stage.identity:
            raise MigrationError("cleanup safety error; stage evidence retained")
        os.rmdir(stage.stage_name, dir_fd=stage.parent_fd)
    except MigrationError:
        raise
    except OSError as exc:
        raise MigrationError("cleanup safety error; stage evidence retained") from exc
    finally:
        with contextlib.suppress(OSError): os.close(stage.stage_fd)
        with contextlib.suppress(OSError): os.close(stage.parent_fd)

def _reject_pending_sqlite_sidecars(path: Path) -> None:
    """Reject every canonical SQLite transaction sidecar, even if empty."""
    try:
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = path.with_name(path.name + suffix)
            try:
                sidecar.lstat()
            except FileNotFoundError:
                continue
            raise MigrationError("source has pending SQLite sidecar")
    except MigrationError:
        raise
    except OSError as exc:
        raise MigrationError("source artifact cannot be read") from exc

def _copy_or_reflink(source_fd: int, destination: Path, source_size: int, *, before_chunk: object | None = None) -> tuple[str, tuple[int, int]]:
    """Make a 0600 private byte snapshot; its verified digest is authority."""
    out = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.ioctl(out, 0x40049409, source_fd)  # Linux FICLONE
            os.fsync(out); info = os.fstat(out); return "reflink", (info.st_dev, info.st_ino)
        except OSError as exc:
            if exc.errno not in {errno.EOPNOTSUPP, errno.ENOTTY, errno.EXDEV, errno.EINVAL, errno.ENOSYS}: raise
        filesystem = os.fstatvfs(out)
        block_size = filesystem.f_frsize or filesystem.f_bsize
        free_bytes = filesystem.f_bavail * block_size
        if free_bytes < source_size + DEFAULT_STAGE_MARGIN_BYTES:
            raise MigrationError("insufficient scratch space")
        os.lseek(source_fd, 0, os.SEEK_SET)
        while True:
            if callable(before_chunk): before_chunk()
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk: break
            offset = 0
            while offset < len(chunk):
                written = os.write(out, chunk[offset:])
                if written <= 0:
                    raise OSError(errno.EIO, "short write")
                offset += written
        os.fsync(out); info = os.fstat(out); return "copy", (info.st_dev, info.st_ino)
    except OSError as exc: raise MigrationError("source artifact cannot be staged") from exc
    finally: os.close(out)

@contextlib.contextmanager
def _source_read_lease(fd: int) -> Iterator[object]:
    """Use a dedicated RT signal; generic SIGIO remains owned by its caller."""
    required = ("F_SETLEASE", "F_SETSIG", "F_GETSIG", "F_SETOWN", "F_GETOWN")
    if (not sys.platform.startswith("linux") or not all(hasattr(fcntl, name) for name in required)
            or not hasattr(signal, "SIGRTMIN") or not hasattr(signal, "SIGRTMAX")):
        raise MigrationError("source lease protection is unavailable")
    if threading.current_thread() is not threading.main_thread():
        raise MigrationError("source lease protection is unavailable")
    if not _LEASE_SIGNAL_LOCK.acquire(blocking=False):
        raise MigrationError("source lease protection is unavailable")
    dedicated = int(signal.SIGRTMIN) + _LEASE_SIGNAL_OFFSET
    if dedicated > int(signal.SIGRTMAX):
        _LEASE_SIGNAL_LOCK.release()
        raise MigrationError("source lease protection is unavailable")
    broken = [False]
    previous_handler = signal.getsignal(dedicated)
    previous_signal = previous_owner = None

    def on_lease_signal(signum: int, frame: object) -> None:
        broken[0] = True
        if callable(previous_handler):
            previous_handler(signum, frame)
    try:
        previous_signal = fcntl.fcntl(fd, fcntl.F_GETSIG)
        previous_owner = fcntl.fcntl(fd, fcntl.F_GETOWN)
        signal.signal(dedicated, on_lease_signal)
        fcntl.fcntl(fd, fcntl.F_SETOWN, os.getpid())
        fcntl.fcntl(fd, fcntl.F_SETSIG, dedicated)
        fcntl.fcntl(fd, fcntl.F_SETLEASE, fcntl.F_RDLCK)
    except (OSError, ValueError) as exc:
        if previous_signal is not None:
            with contextlib.suppress(OSError): fcntl.fcntl(fd, fcntl.F_SETSIG, previous_signal)
        if previous_owner is not None:
            with contextlib.suppress(OSError): fcntl.fcntl(fd, fcntl.F_SETOWN, previous_owner)
        with contextlib.suppress(ValueError): signal.signal(dedicated, previous_handler)
        _LEASE_SIGNAL_LOCK.release()
        raise MigrationError("source lease protection is unavailable") from exc
    try:
        yield lambda: broken[0]
        if broken[0]:
            raise MigrationError("source lease was broken during staging")
    finally:
        with contextlib.suppress(OSError): fcntl.fcntl(fd, fcntl.F_SETLEASE, fcntl.F_UNLCK)
        with contextlib.suppress(OSError): fcntl.fcntl(fd, fcntl.F_SETSIG, previous_signal)
        with contextlib.suppress(OSError): fcntl.fcntl(fd, fcntl.F_SETOWN, previous_owner)
        with contextlib.suppress(ValueError): signal.signal(dedicated, previous_handler)
        _LEASE_SIGNAL_LOCK.release()


def _stage_artifact(source: Path, destination: Path, expected_hash: str, label: str, *, before_chunk: object | None = None) -> tuple[str, tuple[int, int, int, int]]:
    _reject_pending_sqlite_sidecars(source); fd = -1
    destination_identity: tuple[int, int] | None = None
    try:
        fd = os.open(source, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)); info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1: raise MigrationError("source artifact is not a singly linked regular file")
        try: fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc: raise MigrationError("source artifact is busy") from exc
        with _source_read_lease(fd) as lease_broken:
            mode, destination_identity = _copy_or_reflink(fd, destination, info.st_size, before_chunk=before_chunk)
            _require_hash(expected_hash, _sha256_file(destination), label)
            _reject_pending_sqlite_sidecars(source)
            if lease_broken() or os.fstat(fd).st_nlink != 1:
                raise MigrationError("source artifact changed during staging")
            staged = os.stat(destination, follow_symlinks=False)
            if not stat.S_ISREG(staged.st_mode) or staged.st_nlink != 1 or staged.st_uid != os.getuid():
                raise MigrationError("source artifact cannot be staged")
            return mode, (staged.st_dev, staged.st_ino, staged.st_nlink, staged.st_uid)
    except MigrationError:
        with contextlib.suppress(OSError):
            current = os.lstat(destination)
            if destination_identity == (current.st_dev, current.st_ino): destination.unlink()
        raise
    except OSError as exc: raise MigrationError("source artifact cannot be read") from exc
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError): os.close(fd)

def _inventory_partial_stage_files(stage: ScratchStage) -> None:
    """Record safe partial outputs so failure cleanup can remove only our files."""
    expected_names = {"state.sqlite", "agent_logs.sqlite"}
    partial: dict[str, tuple[int, int, int, int]] = {}
    try:
        names = set(os.listdir(stage.stage_fd))
    except OSError:
        return
    for name in names & expected_names:
        try:
            info = os.stat(name, dir_fd=stage.stage_fd, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid():
            partial[name] = (info.st_dev, info.st_ino, info.st_nlink, info.st_uid)
    object.__setattr__(stage, "files", partial)


def _stage_sources(source: Path, logs: Path, scratch_dir: Path, state_hash: str | None, logs_hash: str | None, *, before_chunk: object | None = None) -> tuple[ScratchStage, dict[str, str]]:
    parent_fd, parent_identity = _open_safe_scratch_dir(scratch_dir); _require_hash(state_hash, state_hash or "", "state"); _require_hash(logs_hash, logs_hash or "", "agent logs")
    stage: ScratchStage | None = None
    try:
        stage = _create_stage(parent_fd, scratch_dir)
        if stage.parent_identity != parent_identity:
            raise MigrationError("scratch directory is unsafe")
        state_mode, state_file = _stage_artifact(source, Path(f"/proc/self/fd/{stage.stage_fd}/state.sqlite"), state_hash or "", "state", before_chunk=before_chunk)
        object.__setattr__(stage, "files", {"state.sqlite": state_file})
        logs_mode, logs_file = _stage_artifact(logs, Path(f"/proc/self/fd/{stage.stage_fd}/agent_logs.sqlite"), logs_hash or "", "agent logs", before_chunk=before_chunk)
        object.__setattr__(stage, "files", {"state.sqlite": state_file, "agent_logs.sqlite": logs_file})
        return stage, {"state": state_mode, "agent_logs": logs_mode}
    except Exception as primary:
        if stage is not None:
            _inventory_partial_stage_files(stage)
            _cleanup_after_failure(stage, primary)
        else: os.close(parent_fd)
        raise


@dataclass
class SourceArtifact:
    """Pinned source descriptor and SQLite read transaction."""
    fd: int
    identity: ArtifactIdentity
    conn: sqlite3.Connection

    def hash(self) -> str:
        digest = hashlib.sha256()
        try:
            with os.fdopen(os.dup(self.fd), "rb", closefd=True) as stream:
                stream.seek(0)
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
        except OSError as exc:
            raise MigrationError("source artifact cannot be read") from exc
        return digest.hexdigest()

    def verify_unchanged(self, expected_hash: str) -> None:
        stat = os.fstat(self.fd)
        identity = ArtifactIdentity(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if identity != self.identity or self.hash() != expected_hash:
            raise MigrationError("source artifact changed during migration")

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error): self.conn.close()
        with contextlib.suppress(OSError): os.close(self.fd)


def _open_source(path: Path) -> SourceArtifact:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(path, flags)
        stat = os.fstat(fd)
        if not __import__("stat").S_ISREG(stat.st_mode):
            raise MigrationError("source artifact is not a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            raise MigrationError("source artifact is busy") from exc
        conn = sqlite3.connect(f"file:/proc/self/fd/{fd}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        return SourceArtifact(fd, ArtifactIdentity(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns), conn)
    except MigrationError:
        with contextlib.suppress(OSError): os.close(fd)
        raise
    except (OSError, sqlite3.Error) as exc:
        with contextlib.suppress(OSError): os.close(fd)
        raise MigrationError("source artifact cannot be read") from exc

def _require_hash(value: str | None, actual: str, label: str) -> None:
    if value is None or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise MigrationError(f"expected {label} SHA256 is required")
    if value != actual: raise MigrationError(f"{label} SHA256 does not match source")


def _verify_approved_sources(source: Path, logs: Path, approval: SnapshotApproval) -> None:
    """Reject a non-approved pair before staging or target configuration I/O."""
    _require_hash(approval.state_sha256, _sha256_file(source), "state")
    _require_hash(approval.agent_logs_sha256, _sha256_file(logs), "agent logs")


def _validate_inputs(*, cutoff: object, row_byte_limit: object, batch_byte_limit: object | None = None,
                     ceiling_bytes: object | None = None, safety_margin_bytes: object | None = None,
                     cost_multiplier: object | None = None) -> None:
    def finite(value: object) -> bool:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        try:
            return math.isfinite(float(value))
        except OverflowError:
            return False

    if not finite(cutoff):
        raise MigrationError("invalid migration limits")
    limits = ((row_byte_limit, False),)
    if batch_byte_limit is not None:
        limits += ((batch_byte_limit, False),)
    if ceiling_bytes is not None:
        limits += ((ceiling_bytes, False),)
    if safety_margin_bytes is not None:
        limits += ((safety_margin_bytes, True),)
    if any(isinstance(value, bool) or not isinstance(value, int) or (value < 0 if allow_zero else value <= 0)
           for value, allow_zero in limits):
        raise MigrationError("invalid migration limits")
    if ceiling_bytes is not None and safety_margin_bytes is not None and safety_margin_bytes >= ceiling_bytes:
        raise MigrationError("invalid migration limits")
    if cost_multiplier is not None and (not finite(cost_multiplier) or cost_multiplier < MIN_COST_MULTIPLIER):
        raise MigrationError("invalid migration limits")


def _cleanup_after_failure(stage: ScratchStage, primary: BaseException) -> None:
    """Keep the root failure visible if forensic stage retention also fails."""
    try:
        _cleanup_stage(stage)
    except MigrationError as cleanup:
        primary.add_note("cleanup safety error; stage evidence retained")
        raise primary from cleanup


TARGET_CLOSE_TIMEOUT_SECONDS = 5


def _close_accepts_timeout(close: object) -> bool:
    """Keep lightweight test doubles usable while requiring asyncpg's timeout."""
    try:
        parameters = inspect.signature(close).parameters.values()
    except (TypeError, ValueError):
        # asyncpg's public Connection.close() supports the keyword. If a
        # production implementation cannot be introspected, retain that safe
        # production contract rather than silently dropping the bound.
        return True
    return any(parameter.name == "timeout" or parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters)


async def _close_target_connection(conn: object, diagnostics: list[str]) -> None:
    """Bound asyncpg close and forcibly release a connection that does not close."""
    close = conn.close
    try:
        if _close_accepts_timeout(close):
            await close(timeout=TARGET_CLOSE_TIMEOUT_SECONDS)
        else:
            # Minimal fakes predate asyncpg's timeout keyword. Production
            # connections are always called through the bounded branch above.
            await close()
    except BaseException:
        diagnostics.append("target connection close failed")
        terminate = getattr(conn, "terminate", None)
        if callable(terminate):
            try:
                terminate()
            except BaseException:
                diagnostics.append("target connection terminate failed")


async def _finalize_migration_resources(
    conn: object | None,
    state: SourceArtifact,
    logs: SourceArtifact,
    stage: ScratchStage,
    *,
    primary: BaseException | None = None,
) -> None:
    """Close every local resource once without replacing a migration result."""
    diagnostics: list[str] = []
    if conn is not None:
        await _close_target_connection(conn, diagnostics)
    for label, artifact in (("state", state), ("agent logs", logs)):
        try:
            artifact.close()
        except BaseException:
            diagnostics.append(f"{label} source close failed")
    if primary is not None:
        for diagnostic in diagnostics:
            primary.add_note(diagnostic)
        try:
            _cleanup_after_failure(stage, primary)
        except BaseException as cleanup:
            # _cleanup_after_failure deliberately re-raises primary with a
            # sanitized cleanup cause.  Never let a mocked/unexpected cleanup
            # failure displace the original migration diagnosis either.
            if cleanup is not primary:
                primary.add_note("stage cleanup failed; stage evidence retained")
        return
    try:
        _cleanup_stage(stage)
    except BaseException:
        diagnostics.append("stage cleanup failed; stage evidence retained")
    if diagnostics:
        cleanup_error = MigrationError("migration committed but local cleanup failed")
        for diagnostic in diagnostics:
            cleanup_error.add_note(diagnostic)
        raise cleanup_error


async def _finish_migration_cleanup(finalizer: object) -> None:
    """Run one finalizer task to completion despite repeated task cancellation."""
    task = asyncio.create_task(finalizer)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()

def _strict_unquote(value: str) -> str:
    if any(value[index] == "%" and (index + 2 >= len(value) or any(char not in "0123456789abcdefABCDEF" for char in value[index + 1:index + 3])) for index in range(len(value))):
        raise ValueError("bad escape")
    return unquote_to_bytes(value).decode("utf-8", "strict")


def parse_target_dsn(dsn: str, *, allow_insecure_loopback: bool = False) -> TargetConfig:
    """Parse only the narrow supported DSN grammar without leaking its text."""
    try:
        p = urlsplit(dsn)
        if p.scheme not in {"postgres", "postgresql"} or p.fragment or p.netloc.count("@") != 1:
            raise ValueError("invalid URL")
        host, port, username, password = p.hostname, p.port, p.username, p.password
        port = 5432 if port is None else port
        if not 1 <= port <= 65535: raise ValueError("invalid port")
        if not host or username is None or password is None or p.path.count("/") != 1 or not p.path[1:]:
            raise ValueError("missing component")
        user, password, database = (_strict_unquote(part) for part in (username, password, p.path[1:]))
        pairs = [] if not p.query else [part.split("=", 1) for part in p.query.split("&")]
        if any(len(pair) != 2 or not pair[0] for pair in pairs): raise ValueError("bad query")
        opts = {_strict_unquote(key): _strict_unquote(value) for key, value in pairs}
        if len(opts) != len(pairs) or set(opts) - {"sslmode", "sslrootcert"}: raise ValueError("bad query")
        loopback = host.lower() in {"localhost", "127.0.0.1", "::1"}
        if loopback and allow_insecure_loopback:
            if opts: raise ValueError("TLS options on loopback")
            return TargetConfig(host, port, user, password, database, False)
        if opts.get("sslmode") != "verify-full" or not opts.get("sslrootcert"): raise ValueError("TLS required")
        ca = Path(opts["sslrootcert"])
        if not ca.is_file(): raise ValueError("CA unavailable")
        context = ssl.create_default_context(cafile=str(ca))
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        return TargetConfig(host, port, user, password, database, context)
    except (OSError, UnicodeError, ValueError, ssl.SSLError, TypeError) as exc:
        raise MigrationError("invalid target configuration") from None

def _expected_type(column: str) -> str:
    if column in {"started_at","ended_at","timestamp","estimated_cost_usd","actual_cost_usd","compression_failure_cooldown_until","first_seen","last_seen","dispatched_at","completed_at","updated_at","delivered_at","delivery_claimed_at","acquired_at","expires_at"}: return "REAL"
    if column in {"id"} and False: return "INTEGER"
    if column in {"message_count","tool_call_count","input_tokens","output_tokens","cache_read_tokens","cache_write_tokens","reasoning_tokens","api_call_count","rewind_count","archived","expiry_finalized","compression_fallback_streak","compression_ineffective_count","pinned","token_count","observed","active","compacted","delivery_attempts","owner_pid","owner_started_at"}: return "INTEGER"
    return "TEXT"

def _fingerprint(value: object) -> str:
    """Hash a structured schema contract with deterministic JSON encoding."""
    def normalize(item: object) -> object:
        if isinstance(item, bytes):
            return {"bytes_hex": item.hex()}
        if isinstance(item, tuple):
            return [normalize(value) for value in item]
        if isinstance(item, list):
            return [normalize(value) for value in item]
        if isinstance(item, dict):
            return {str(key): normalize(value) for key, value in item.items()}
        return item

    return hashlib.sha256(json.dumps(normalize(value), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _sqlite_schema_contract(conn: sqlite3.Connection, tables: Sequence[str]) -> dict[str, object]:
    """Include columns, indexes, and foreign keys; FTS-derived objects excluded."""
    names = tuple(sorted(tables))
    found = tuple(
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        if not row[0].startswith("messages_fts") and row[0] not in {"schema_version", "sqlite_sequence"}
    )
    definitions: dict[str, object] = {}
    for table in names:
        quote = table.replace('"', '""')
        indexes = []
        for index in conn.execute(f'PRAGMA index_list("{quote}")'):
            index_name = str(index[1]).replace('"', '""')
            indexes.append({"list": tuple(index), "xinfo": [tuple(row) for row in conn.execute(f'PRAGMA index_xinfo("{index_name}")')]})
        definitions[table] = {
            "columns": [tuple(row) for row in conn.execute(f'PRAGMA table_info("{quote}")')],
            "indexes": indexes,
            "foreign_keys": [tuple(row) for row in conn.execute(f'PRAGMA foreign_key_list("{quote}")')],
        }
    return {"tables": found, "definitions": definitions}


def _validate_schema(conn: sqlite3.Connection, tables: Sequence[str], expected_fingerprint: str) -> None:
    contract = _sqlite_schema_contract(conn, tables)
    if tuple(contract["tables"]) != tuple(sorted(tables)) or _fingerprint(contract) != expected_fingerprint:
        raise MigrationError("source schema mismatch")

def _as_bytes(v: object) -> bytes:
    if isinstance(v, bytes): return v
    if isinstance(v, str): return v.encode()
    raise MigrationError("invalid text payload")
def _canon(v: object) -> bytes:
    if v is None: return b"N"
    if isinstance(v, bool): return b"I" + (b"1" if v else b"0")
    if isinstance(v, int): return b"I" + str(v).encode()
    if isinstance(v, float): return b"F" + struct.pack("!d",v)
    if isinstance(v,(str,bytes)): return b"B"+_as_bytes(v)
    raise MigrationError("unsupported source value")
def digest_rows(rows: Iterator[Sequence[object]]) -> tuple[str, int, int, int, str | None, str | None, float | None, float | None]:
    """Compatibility helper used by tests; domains use explicit timestamp fields."""
    d = hashlib.sha256(); count = nuls = payload = 0; lo = hi = None
    for row in rows:
        count += 1; d.update(struct.pack("!I", len(row)))
        for value in row:
            encoded = _canon(value); d.update(struct.pack("!Q", len(encoded))); d.update(encoded)
            if isinstance(value, (str, bytes)):
                raw = _as_bytes(value); payload += len(raw); nuls += raw.count(b"\0")
        if row and row[0] is not None:
            ident = str(row[0]); lo = ident if lo is None or ident < lo else lo; hi = ident if hi is None or ident > hi else hi
    return d.hexdigest(), count, nuls, payload, lo, hi, None, None
def _order_by(table:str, *, postgres: bool = False)->str:
    expressions: list[str] = []
    for key, kind in DOMAIN_SPECS[table].order_keys:
        quoted = f'"{key}"'
        if kind in {"integer", "bytes"}: expressions.append(quoted)
        elif postgres: expressions.append(f"convert_to({quoted}, 'UTF8')")
        else: expressions.append(f"CAST({quoted} AS BLOB)")
    return ", ".join(expressions)
def _rows(conn:sqlite3.Connection, table:str, cutoff:float|None)->Iterator[tuple[object,...]]:
    cols=TABLE_COLUMNS[table]; quoted=", ".join(f'"{c}"' for c in cols)
    if table=="messages": sql, params=f"SELECT {quoted} FROM messages WHERE timestamp>=? ORDER BY {_order_by(table)}",(cutoff,)
    elif table in {"sessions","session_model_usage"}: sql,params=f"SELECT {quoted} FROM {table} WHERE session_id IN (SELECT DISTINCT session_id FROM messages WHERE timestamp>=?) ORDER BY {_order_by(table)}",(cutoff,) if table=="session_model_usage" else (cutoff,); sql = sql.replace("WHERE session_id", "WHERE id") if table=="sessions" else sql
    else: sql,params=f"SELECT {quoted} FROM \"{table}\" ORDER BY {_order_by(table)}",()
    yield from (tuple(r) for r in conn.execute(sql,params))
def _check_row(table: str, columns: Sequence[str], row: Sequence[object], limit: int) -> None:
    required = TARGET_REQUIRED_COLUMNS[table]
    for col, value in zip(columns, row, strict=True):
        if col in required and value is None:
            raise MigrationError("source row violates target nullability")
        if col not in BINARY_COLUMNS and isinstance(value, str) and "\0" in value:
            raise MigrationError("NUL in scalar identifier")
    if sum(len(_canon(value)) + 8 for value in row) > limit:
        raise MigrationError("source row exceeds configured byte limit")
def _manifest(conn:sqlite3.Connection, table:str,cutoff:float|None,limit:int,source_hash:str)->DomainManifest:
    d=hashlib.sha256(); count=nuls=payload=0; lo=hi=None; min_ts=max_ts=None; tscol=TIMESTAMP_COLUMNS.get(table); ti=TABLE_COLUMNS[table].index(tscol) if tscol else None
    for row in _rows(conn,table,cutoff):
        _check_row(table, TABLE_COLUMNS[table], row, limit); count+=1; d.update(struct.pack("!I",len(row)))
        for v in row:
            e=_canon(v);d.update(struct.pack("!Q",len(e)));d.update(e)
            if isinstance(v,(str,bytes)): payload+=len(_as_bytes(v));nuls+=_as_bytes(v).count(b"\0")
        identity = row[0] if DOMAIN_SPECS[table].order_keys[0][1] == "integer" else str(row[0])
        lo=identity if lo is None or identity<lo else lo;hi=identity if hi is None or identity>hi else hi
        if ti is not None and row[ti] is not None: stamp=float(row[ti]);min_ts=stamp if min_ts is None or stamp<min_ts else min_ts;max_ts=stamp if max_ts is None or stamp>max_ts else max_ts
    return DomainManifest(table,TABLE_COLUMNS[table],d.hexdigest(),count,nuls,payload,lo,hi,min_ts,max_ts,source_hash)
def _validate_source_integrity(conn: sqlite3.Connection) -> None:
    try:
        quick_check = conn.execute("PRAGMA quick_check").fetchone()
        foreign_key_error = conn.execute("PRAGMA foreign_key_check").fetchone()
    except sqlite3.DatabaseError as exc:
        raise MigrationError("source integrity audit failed") from exc
    if quick_check is None or quick_check[0] != "ok":
        raise MigrationError("source integrity audit failed")
    if foreign_key_error is not None:
        raise MigrationError("source referential integrity audit failed")


def _validate_hot_relations(conn: sqlite3.Connection, cutoff: float) -> None:
    orphan = conn.execute(
        """SELECT 1
           FROM messages AS message
           LEFT JOIN sessions AS session ON session.id = message.session_id
           WHERE message.timestamp >= ? AND session.id IS NULL
           LIMIT 1""",
        (cutoff,),
    ).fetchone()
    if orphan is not None:
        raise MigrationError("source referential integrity audit failed")


def _preflight_open(state: SourceArtifact, logs: SourceArtifact, cutoff: float, row_byte_limit: int, *, approval: SnapshotApproval) -> tuple[tuple[str, str], dict[str, DomainManifest]]:
    _validate_inputs(cutoff=cutoff, row_byte_limit=row_byte_limit)
    state_hash, logs_hash = state.hash(), logs.hash()
    _require_hash(approval.state_sha256,state_hash,"state"); _require_hash(approval.agent_logs_sha256,logs_hash,"agent logs")
    names=tuple(x for x in TABLE_COLUMNS if x!="agent_logs")
    _validate_schema(state.conn, names, approval.state_schema_fingerprint)
    _validate_schema(logs.conn, ("agent_logs",), approval.agent_logs_schema_fingerprint)
    _validate_source_integrity(state.conn)
    _validate_source_integrity(logs.conn)
    _validate_hot_relations(state.conn, cutoff)
    bad=state.conn.execute("SELECT 1 FROM sessions s WHERE COALESCE(message_count,0)!=(SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id AND m.active=1) LIMIT 1").fetchone()
    if bad: raise MigrationError("source active message count audit failed")
    manifests={x:_manifest(state.conn,x,cutoff if x in {"sessions","messages","session_model_usage"} else None,row_byte_limit,state_hash) for x in names}
    manifests["sessions"] = _source_parity_manifest(state.conn, manifests["sessions"], cutoff, row_byte_limit)
    manifests["agent_logs"]=_manifest(logs.conn,"agent_logs",None,row_byte_limit,logs_hash)
    return (state_hash,logs_hash),manifests

def _preflight_approved(source:Path,logs:Path,cutoff:float,row_byte_limit:int=DEFAULT_ROW_BYTES,*,scratch_dir: Path, approval: SnapshotApproval, _before_stage_chunk: object | None = None)->tuple[tuple[str,str],dict[str,DomainManifest]]:
    """Test-only approved path; production must enter through preflight()."""
    _validate_inputs(cutoff=cutoff, row_byte_limit=row_byte_limit)
    _verify_approved_sources(source, logs, approval)
    state_hash, logs_hash = approval.state_sha256, approval.agent_logs_sha256
    stage, _ = _stage_sources(source, logs, scratch_dir, state_hash, logs_hash, before_chunk=_before_stage_chunk)
    try:
        state = _open_source(Path(f"/proc/self/fd/{stage.stage_fd}/state.sqlite")); agent_logs = _open_source(Path(f"/proc/self/fd/{stage.stage_fd}/agent_logs.sqlite"))
    except Exception as primary:
        _cleanup_after_failure(stage, primary)
        raise
    succeeded = False
    try:
        result = _preflight_open(state, agent_logs, cutoff, row_byte_limit, approval=approval)
        succeeded = True
        return result
    except Exception as primary:
        state.close(); agent_logs.close(); _cleanup_after_failure(stage, primary)
        raise
    finally:
        if succeeded:
            state.close(); agent_logs.close(); _cleanup_stage(stage)


def preflight(source:Path,logs:Path,cutoff:float,row_byte_limit:int=DEFAULT_ROW_BYTES,*,scratch_dir: Path, _before_stage_chunk: object | None = None)->tuple[tuple[str,str],dict[str,DomainManifest]]:
    return _preflight_approved(source, logs, cutoff, row_byte_limit, scratch_dir=scratch_dir,
                               approval=PRODUCTION_APPROVAL, _before_stage_chunk=_before_stage_chunk)

def _migration_sql()->str:return importlib.resources.files("hermes_cli").joinpath("migrations/0001_hermes_hot.sql").read_text()
async def _copy(conn:object,src:sqlite3.Connection,m:DomainManifest,cutoff:float|None,row_limit:int,batch_limit:int)->None:
    batch=[];size=0; cols=TABLE_COLUMNS[m.name]
    for raw in _rows(src,m.name,cutoff):
        _check_row(m.name, cols, raw, row_limit); row=tuple(_as_bytes(x) if c in BINARY_COLUMNS and x is not None else x for c,x in zip(cols,raw,strict=True)); outcols=cols
        if m.name=="sessions":
            full=src.execute("SELECT COUNT(*) FROM messages WHERE session_id=? AND active=1",(raw[0],)).fetchone()[0];hot=src.execute("SELECT COUNT(*) FROM messages WHERE session_id=? AND active=1 AND timestamp>=?",(raw[0],cutoff)).fetchone()[0]
            row=(*row,full,hot);outcols=(*cols,"source_active_message_count","hot_active_message_count")
        row_size=sum(len(_canon(x))+8 for x in raw)
        if row_size>batch_limit: raise MigrationError("source row exceeds configured batch byte limit")
        if batch and size+row_size>batch_limit: await conn.copy_records_to_table(m.name,records=batch,columns=outcols,schema_name="hermes_hot");batch=[];size=0
        batch.append(row);size+=row_size
    if batch: await conn.copy_records_to_table(m.name,records=batch,columns=outcols,schema_name="hermes_hot")
async def _target_manifest(conn:object,m:DomainManifest)->DomainManifest:
    cols=m.columns
    # Counters participate in target digest, unlike source storage columns.
    d=hashlib.sha256(); count=nuls=payload=0;lo=hi=None;mn=mx=None; ts=TIMESTAMP_COLUMNS.get(m.name);ti=cols.index(ts) if ts else None
    async for r in conn.cursor(f'SELECT {", ".join(chr(34)+x+chr(34) for x in cols)} FROM hermes_hot."{m.name}" ORDER BY {_order_by(m.name, postgres=True)}'):
        row=tuple(r);count+=1;d.update(struct.pack("!I",len(row)))
        for v in row:
            e=_canon(v);d.update(struct.pack("!Q",len(e)));d.update(e)
            if isinstance(v,(str,bytes)):payload+=len(_as_bytes(v));nuls+=_as_bytes(v).count(b"\0")
        ident = row[0] if DOMAIN_SPECS[m.name].order_keys[0][1] == "integer" else str(row[0])
        lo=ident if lo is None or ident<lo else lo;hi=ident if hi is None or ident>hi else hi
        if ti is not None and row[ti] is not None: v=float(row[ti]);mn=v if mn is None or v<mn else mn;mx=v if mx is None or v>mx else mx
    # Source digest must use same derived session fields.
    return DomainManifest(m.name,cols,d.hexdigest(),count,nuls,payload,lo,hi,mn,mx,m.source_sha256)
def _source_parity_manifest(src:sqlite3.Connection,m:DomainManifest,cutoff:float|None,limit:int)->DomainManifest:
    if m.name!="sessions":return m
    # derive counters before digest just as target does
    d=hashlib.sha256();n=p=nu=0;lo=hi=None;mn=mx=None
    for source_row in _rows(src,"sessions",cutoff):
        _check_row("sessions", TABLE_COLUMNS["sessions"], source_row, limit)
        full=src.execute("SELECT COUNT(*) FROM messages WHERE session_id=? AND active=1",(source_row[0],)).fetchone()[0]
        hot=src.execute("SELECT COUNT(*) FROM messages WHERE session_id=? AND active=1 AND timestamp>=?",(source_row[0],cutoff)).fetchone()[0]
        r = (*source_row, full, hot)
        n+=1;d.update(struct.pack("!I",len(r)))
        for v in r:e=_canon(v);d.update(struct.pack("!Q",len(e)));d.update(e);p+=len(_as_bytes(v)) if isinstance(v,(str,bytes)) else 0;nu+=_as_bytes(v).count(b"\0") if isinstance(v,(str,bytes)) else 0
        x=str(r[0]);lo=x if lo is None or x<lo else lo;hi=x if hi is None or x>hi else hi;v=float(r[11]);mn=v if mn is None or v<mn else mn;mx=v if mx is None or v>mx else mx
    columns = m.columns if m.columns[-2:] == ("source_active_message_count", "hot_active_message_count") else (*m.columns,"source_active_message_count","hot_active_message_count")
    return DomainManifest(m.name,columns,d.hexdigest(),n,nu,p,lo,hi,mn,mx,m.source_sha256)
async def _target_schema_contract(conn: object) -> dict[str, object]:
    """Capture every owned PG17 table, column, constraint, and index property."""
    tables = await conn.fetch("""SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='hermes_hot' AND c.relkind='r' ORDER BY c.relname""")
    columns = await conn.fetch("""SELECT c.relname AS table_name, a.attnum, a.attname,
        pg_catalog.format_type(a.atttypid,a.atttypmod) AS type, a.attnotnull, pg_get_expr(ad.adbin,ad.adrelid) AS default,
        a.attidentity, a.attgenerated, coll.collname AS collation
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_attribute a ON a.attrelid=c.oid
        LEFT JOIN pg_attrdef ad ON ad.adrelid=a.attrelid AND ad.adnum=a.attnum
        LEFT JOIN pg_collation coll ON coll.oid=a.attcollation
        WHERE n.nspname='hermes_hot' AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped
        ORDER BY c.relname,a.attnum""")
    constraints = await conn.fetch("""SELECT r.relname AS table_name, con.conname, con.contype,
        pg_get_constraintdef(con.oid,true) AS definition, con.condeferrable, con.condeferred, con.convalidated
        FROM pg_constraint con JOIN pg_class r ON r.oid=con.conrelid JOIN pg_namespace n ON n.oid=r.relnamespace
        WHERE n.nspname='hermes_hot' ORDER BY r.relname,con.conname""")
    indexes = await conn.fetch("""SELECT r.relname AS table_name, i.relname AS index_name, am.amname AS method,
        ix.indisunique, ix.indisprimary, ix.indimmediate, pg_get_expr(ix.indpred,ix.indrelid) AS predicate,
        COALESCE(ts.spcname,'') AS tablespace, pg_get_indexdef(i.oid) AS definition,
        ix.indkey::text AS key_numbers, ix.indoption::text AS key_options, ix.indclass::text AS opclasses
        FROM pg_index ix JOIN pg_class i ON i.oid=ix.indexrelid JOIN pg_class r ON r.oid=ix.indrelid
        JOIN pg_namespace n ON n.oid=r.relnamespace JOIN pg_am am ON am.oid=i.relam
        LEFT JOIN pg_tablespace ts ON ts.oid=i.reltablespace
        WHERE n.nspname='hermes_hot' ORDER BY r.relname,i.relname""")
    return {
        "tables": [record["relname"] for record in tables],
        "columns": [dict(record) for record in columns],
        # The domain allow-list is checked independently below so the
        # historical base-schema fingerprint remains comparable.
        "constraints": [dict(record) for record in constraints if not (record["table_name"] == "domain_manifests" and record["contype"] == "c")],
        "indexes": [dict(record) for record in indexes],
    }


def _acls_are_exact_defaults(rows: Sequence[dict[str, object]]) -> bool:
    """Every expected object must exist, be current-user-owned, and default ACLed."""
    expected = {("schema", "hermes_hot")} | {("table", name) for name in set(TABLE_COLUMNS) | {"migration_runs", "domain_manifests"}}
    actual = {(row.get("kind"), row.get("name")) for row in rows}
    return len(rows) == len(expected) and actual == expected and all(
        row.get("owner") == row.get("current_owner") and row.get("acl_is_default") is True
        for row in rows
    )

async def _verify_target_acls(conn: object) -> None:
    rows = await conn.fetch("""WITH expected(kind, name, acl_kind) AS (
        VALUES ('schema'::text, 'hermes_hot'::name, 'n'::"char"),
        ('table','sessions','r'), ('table','messages','r'), ('table','session_model_usage','r'),
        ('table','async_delegations','r'), ('table','compression_locks','r'), ('table','gateway_routing','r'),
        ('table','state_meta','r'), ('table','agent_logs','r'), ('table','migration_runs','r'), ('table','domain_manifests','r')
    ), objects AS (
        SELECT e.kind, e.name::text name, e.acl_kind, n.nspowner owner, n.nspacl acl
          FROM expected e LEFT JOIN pg_catalog.pg_namespace n ON e.kind='schema' AND n.nspname=e.name
          WHERE e.kind='schema'
        UNION ALL
        SELECT e.kind, e.name::text, e.acl_kind, c.relowner, c.relacl
          FROM expected e LEFT JOIN pg_catalog.pg_class c ON e.kind='table' AND c.relname=e.name
          LEFT JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace AND n.nspname='hermes_hot'
          WHERE e.kind='table' AND n.oid IS NOT NULL
    ) SELECT kind, name, owner, current_user::regrole::oid current_owner,
        owner IS NOT NULL AND COALESCE(acl, pg_catalog.acldefault(acl_kind, owner)) = pg_catalog.acldefault(acl_kind, owner) AS acl_is_default
      FROM objects""")
    if not _acls_are_exact_defaults([dict(row) for row in rows]): raise MigrationError("target schema mismatch")

async def _verify_target_schema(conn: object) -> None:
    contract = await _target_schema_contract(conn)
    if _fingerprint(contract) != PRODUCTION_TARGET_SCHEMA_FINGERPRINT:
        raise MigrationError("target schema mismatch")
    expected_relations = set(TABLE_COLUMNS) | {"migration_runs", "domain_manifests"}
    expected_indexes = {
        "migration_runs_pkey", "migration_runs_state_sha256_agent_logs_sha256_cutoff_key", "domain_manifests_pkey",
        "sessions_pkey", "messages_pkey", "messages_session_timestamp", "session_model_usage_pkey",
        "async_delegations_pkey", "compression_locks_pkey", "gateway_routing_pkey", "state_meta_pkey", "agent_logs_pkey",
    }
    relations = await conn.fetch("""SELECT c.relname, c.relkind::text AS relkind FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='hermes_hot'""")
    expected_objects = {(name, "r") for name in expected_relations} | {(name, "i") for name in expected_indexes}
    if {(row["relname"], row["relkind"]) for row in relations} != expected_objects:
        raise MigrationError("target schema mismatch")
    extras = await conn.fetchval("""SELECT EXISTS(
        SELECT 1 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='hermes_hot'
        UNION ALL SELECT 1 FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='hermes_hot' AND NOT t.tgisinternal
        UNION ALL SELECT 1 FROM pg_catalog.pg_policy p JOIN pg_catalog.pg_class c ON c.oid=p.polrelid JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='hermes_hot')""")
    if extras: raise MigrationError("target schema mismatch")
    await _verify_target_acls(conn)
    # This full definition is part of the catalog contract, not a token check.
    domains = await conn.fetch("""SELECT con.conname, pg_get_constraintdef(con.oid, false) AS definition
        FROM pg_catalog.pg_constraint con JOIN pg_catalog.pg_class c ON c.oid=con.conrelid
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='hermes_hot' AND c.relname='domain_manifests' AND con.contype='c' ORDER BY con.conname""")
    if not _domain_check_matches([dict(row) for row in domains]): raise MigrationError("target schema mismatch")
    durability_bad = await conn.fetchval("""WITH table_row_types AS (
        SELECT t.oid AS type_oid, t.typarray FROM pg_catalog.pg_type t JOIN pg_catalog.pg_class c ON c.oid=t.typrelid
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='hermes_hot' AND c.relkind='r'
    ) SELECT EXISTS(
        SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_catalog.pg_am am ON am.oid=c.relam LEFT JOIN pg_catalog.pg_tablespace ts ON ts.oid=c.reltablespace
        WHERE n.nspname='hermes_hot' AND c.relkind='r' AND (
            c.relowner <> (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_user)
            OR c.relpersistence <> 'p' OR c.relrowsecurity OR c.relforcerowsecurity OR c.relreplident <> 'd'
            OR c.relispartition OR COALESCE(am.amname, 'heap') <> 'heap' OR COALESCE(ts.spcname,'') <> ''
        )
        UNION ALL SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_catalog.pg_am am ON am.oid=c.relam LEFT JOIN pg_catalog.pg_tablespace ts ON ts.oid=c.reltablespace
        WHERE n.nspname='hermes_hot' AND c.relkind='i' AND (c.relowner <> (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_user) OR COALESCE(am.amname, '') <> 'btree' OR COALESCE(ts.spcname,'') <> '')
        UNION ALL SELECT 1 FROM pg_catalog.pg_namespace n WHERE n.nspname='hermes_hot' AND (
            n.nspowner <> (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_user))
        UNION ALL SELECT 1 FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid=t.typnamespace
            WHERE n.nspname='hermes_hot' AND NOT EXISTS (SELECT 1 FROM table_row_types base WHERE (t.typtype='c' AND t.oid=base.type_oid) OR (t.typtype='b' AND t.oid=base.typarray))
    )""")
    if durability_bad: raise MigrationError("target schema mismatch")

def _domain_check_matches(constraints: Sequence[dict[str, object]]) -> bool:
    """Exact catalog definition comparison; accepting a superset is unsafe."""
    return list(constraints) == [{"conname": "domain_manifests_domain_check", "definition": EXPECTED_DOMAIN_CHECK_DEFINITION}]

def _projected_storage(manifests: dict[str, DomainManifest], multiplier: float) -> int:
    _validate_inputs(cutoff=0, row_byte_limit=1, cost_multiplier=multiplier)
    payload = sum(manifest.payload_bytes for manifest in manifests.values())
    rows = sum(manifest.row_count for manifest in manifests.values())
    # Fixed tuples plus 25% index/TOAST/alignment headroom, then operator buffer.
    try:
        estimate = (payload + FIXED_ROW_OVERHEAD_BYTES * rows) * 1.25 * multiplier
    except OverflowError as exc:
        raise MigrationError("invalid migration limits") from exc
    if not math.isfinite(estimate):
        raise MigrationError("invalid migration limits")
    return int(estimate)
async def _migrate_approved(*,source:Path,agent_logs:Path,scratch_dir: Path,target:TargetConfig|None=None,target_dsn:str|None=None,cutoff:float,approval: SnapshotApproval,allow_insecure_loopback:bool=False,row_byte_limit:int=DEFAULT_ROW_BYTES,batch_byte_limit:int=DEFAULT_BATCH_BYTES,ceiling_bytes:int=DEFAULT_CEILING_BYTES,safety_margin_bytes:int=DEFAULT_SAFETY_MARGIN_BYTES,cost_multiplier:float=MIN_COST_MULTIPLIER,_before_commit: object | None = None, _before_stage_chunk: object | None = None)->dict[str,object]:
    """Test-only approved path; production must enter through migrate()."""
    _validate_inputs(cutoff=cutoff, row_byte_limit=row_byte_limit, batch_byte_limit=batch_byte_limit,
                     ceiling_bytes=ceiling_bytes, safety_margin_bytes=safety_margin_bytes,
                     cost_multiplier=cost_multiplier)
    state_hash, logs_hash = approval.state_sha256, approval.agent_logs_sha256
    _verify_approved_sources(source, agent_logs, approval)
    if target is None:
        if target_dsn is None: raise MigrationError("target configuration is required")
        target=parse_target_dsn(target_dsn,allow_insecure_loopback=allow_insecure_loopback)
    stage, stage_modes = _stage_sources(source, agent_logs, scratch_dir, state_hash, logs_hash, before_chunk=_before_stage_chunk)
    try:
      state = _open_source(Path(f"/proc/self/fd/{stage.stage_fd}/state.sqlite")); logs = _open_source(Path(f"/proc/self/fd/{stage.stage_fd}/agent_logs.sqlite"))
    except Exception as primary:
      _cleanup_after_failure(stage, primary); raise
    try:
      hashes,manifests = _preflight_open(state, logs, cutoff, row_byte_limit, approval=approval)
      projected = _projected_storage(manifests, cost_multiplier)
      if projected>ceiling_bytes-safety_margin_bytes: raise MigrationError("projected storage cost exceeds ceiling")
    except Exception as primary:
      state.close(); logs.close(); _cleanup_after_failure(stage, primary); raise
    conn = None
    primary: BaseException | None = None
    try:
      import asyncpg
      try:
        conn=await asyncpg.connect(host=target.host,port=target.port,user=target.user,password=target.password,database=target.database,ssl=target.ssl,command_timeout=60)
      except Exception as exc:
        raise MigrationError("target connection failed") from exc
      async with conn.transaction():
        await conn.execute("SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='60s'");await conn.execute("SELECT pg_advisory_xact_lock(hashtext('hermes_hot_importer_v2'))");await conn.execute(_migration_sql());await _verify_target_schema(conn)
        if int(await conn.fetchval("SELECT pg_database_size(current_database())"))+projected>ceiling_bytes-safety_margin_bytes:raise MigrationError("actual storage cost exceeds ceiling")
        existing=await conn.fetchrow("SELECT run_id FROM hermes_hot.migration_runs WHERE state_sha256=$1 AND agent_logs_sha256=$2 AND cutoff=$3",*hashes,cutoff)
        if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM hermes_hot.migration_runs WHERE state_sha256<>$1 OR agent_logs_sha256<>$2 OR cutoff<>$3)",*hashes,cutoff):raise MigrationError("target has conflicting provenance")
        run_id=existing["run_id"] if existing else uuid.uuid4()
        if not existing:
          for m in manifests.values():
            await _copy(conn,logs.conn if m.name=="agent_logs" else state.conn,m,cutoff if m.name in {"sessions","messages","session_model_usage"} else None,row_byte_limit,batch_byte_limit)
          await conn.execute("INSERT INTO hermes_hot.migration_runs VALUES($1,$2,$3,$4,'completed',clock_timestamp(),clock_timestamp())",run_id,*hashes,cutoff)
        for m in manifests.values():
          actual=await _target_manifest(conn,m)
          if actual!=m:raise MigrationError("target parity check failed")
          stored=await conn.fetchrow("SELECT row_count,min_id,max_id,min_timestamp,max_timestamp,nul_values,payload_bytes,digest_sha256,source_sha256,state_sha256,agent_logs_sha256 FROM hermes_hot.domain_manifests WHERE run_id=$1 AND domain=$2",run_id,m.name)
          persisted_min = str(m.min_id) if m.min_id is not None else None
          persisted_max = str(m.max_id) if m.max_id is not None else None
          values=(m.row_count,persisted_min,persisted_max,m.min_timestamp,m.max_timestamp,m.nul_values,m.payload_bytes,m.digest,m.source_sha256,*hashes)
          if existing and (stored is None or tuple(stored)!=values):raise MigrationError("stored provenance manifest mismatch")
          if not existing:await conn.execute("INSERT INTO hermes_hot.domain_manifests(run_id,domain,row_count,min_id,max_id,min_timestamp,max_timestamp,nul_values,payload_bytes,digest_sha256,source_sha256,state_sha256,agent_logs_sha256) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)",run_id,m.name,m.row_count,persisted_min,persisted_max,m.min_timestamp,m.max_timestamp,m.nul_values,m.payload_bytes,m.digest,m.source_sha256,*hashes)
        domains={row["domain"] for row in await conn.fetch("SELECT domain FROM hermes_hot.domain_manifests WHERE run_id=$1",run_id)}
        if domains != set(TABLE_COLUMNS): raise MigrationError("stored provenance manifest mismatch")
        if callable(_before_commit): _before_commit()
        state.verify_unchanged(hashes[0]); logs.verify_unchanged(hashes[1])
        final=int(await conn.fetchval("SELECT pg_database_size(current_database())"));
        if final>ceiling_bytes-safety_margin_bytes:raise MigrationError("actual storage cost exceeds ceiling")
        sizes={str(r["relname"]):int(r["bytes"]) for r in await conn.fetch("SELECT c.relname,pg_total_relation_size(c.oid) bytes FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='hermes_hot' AND c.relkind IN ('r','i')")}
    except BaseException as exc:
      if isinstance(exc, MigrationError):
        primary = exc
        raise
      if isinstance(exc, Exception):
        primary = MigrationError("target migration failed")
        raise primary from exc
      primary = exc
      primary.add_note("migration cancelled/interrupted; commit outcome may be ambiguous; exact replay is safe")
      raise
    finally:
      await _finish_migration_cleanup(_finalize_migration_resources(conn, state, logs, stage, primary=primary))
    return {"state_sha256":hashes[0],"agent_logs_sha256":hashes[1],"run_id":str(run_id),"database_bytes":final,"relation_sizes":sizes,"staging":stage_modes,"domains":{n:{"rows":m.row_count,"digest":m.digest} for n,m in manifests.items()}}


async def migrate(*,source:Path,agent_logs:Path,scratch_dir: Path,target:TargetConfig|None=None,target_dsn:str|None=None,cutoff:float,allow_insecure_loopback:bool=False,row_byte_limit:int=DEFAULT_ROW_BYTES,batch_byte_limit:int=DEFAULT_BATCH_BYTES,ceiling_bytes:int=DEFAULT_CEILING_BYTES,safety_margin_bytes:int=DEFAULT_SAFETY_MARGIN_BYTES,cost_multiplier:float=MIN_COST_MULTIPLIER,_before_commit: object | None = None, _before_stage_chunk: object | None = None)->dict[str,object]:
    return await _migrate_approved(source=source, agent_logs=agent_logs, scratch_dir=scratch_dir,
        target=target, target_dsn=target_dsn, cutoff=cutoff, approval=PRODUCTION_APPROVAL,
        allow_insecure_loopback=allow_insecure_loopback, row_byte_limit=row_byte_limit,
        batch_byte_limit=batch_byte_limit, ceiling_bytes=ceiling_bytes,
        safety_margin_bytes=safety_margin_bytes, cost_multiplier=cost_multiplier,
        _before_commit=_before_commit, _before_stage_chunk=_before_stage_chunk)

def main(argv:Sequence[str]|None=None)->int:
    p=argparse.ArgumentParser(description="One-shot importer; target secret is read from HERMES_POSTGRES_MIGRATION_DSN or stdin.");p.add_argument("--source",type=Path,required=True);p.add_argument("--agent-logs",type=Path,required=True);p.add_argument("--scratch-dir",type=Path,required=True);p.add_argument("--cutoff",type=float,required=True);p.add_argument("--allow-insecure-loopback",action="store_true")
    a=p.parse_args(argv)
    try:
      _validate_inputs(cutoff=a.cutoff, row_byte_limit=DEFAULT_ROW_BYTES,
                       batch_byte_limit=DEFAULT_BATCH_BYTES, ceiling_bytes=DEFAULT_CEILING_BYTES,
                       safety_margin_bytes=DEFAULT_SAFETY_MARGIN_BYTES, cost_multiplier=MIN_COST_MULTIPLIER)
      # Choose the sole production approval before any secret, DSN, or CA I/O.
      approval = PRODUCTION_APPROVAL
      if not isinstance(approval, SnapshotApproval): raise MigrationError("production approval is invalid")
      secret=os.environ.get("HERMES_POSTGRES_MIGRATION_DSN") or sys.stdin.readline().rstrip("\r\n")
      target=parse_target_dsn(secret,allow_insecure_loopback=a.allow_insecure_loopback);e=asyncio.run(migrate(source=a.source,agent_logs=a.agent_logs,scratch_dir=a.scratch_dir,target=target,cutoff=a.cutoff))
    except MigrationError as exc: print(f"migration failed: {exc}",file=sys.stderr);return 2
    print(f"migration completed: run={e['run_id']} database_bytes={e['database_bytes']}");return 0
if __name__=="__main__":raise SystemExit(main())
