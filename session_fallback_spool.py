from __future__ import annotations

import errno
import json
import os
import re
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from hashlib import blake2s
from pathlib import Path
from typing import Any, Mapping, Sequence

from hermes_constants import get_hermes_home
from hermes_state import SessionDBBatchMessage

try:  # pragma: no cover - Windows-only import path
    import fcntl  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - Windows-only fallback
    fcntl = None

try:  # pragma: no cover - POSIX-only import path
    import msvcrt  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - POSIX-only fallback
    msvcrt = None


SPOOL_ROOT_NAME = "session_fallback_spool"
ACTIVE_SPOOL_NAME = "active.spool"
LOCK_FILE_NAME = "append.lock"
QUARANTINE_DIR_NAME = "quarantine"
HEADER_MAGIC = b"HSPL"
HEADER_SIZE = 32
FRAME_VERSION = 0x01
RECORD_KIND_SESSION_PERSISTENCE_UNIT = 0x01
ROOT_MODE = 0o700
FILE_MODE = 0o600
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
MAX_FRAME_BYTES = MAX_PAYLOAD_BYTES + HEADER_SIZE
TOTAL_CAP_BYTES = 64 * 1024 * 1024
LOCK_TIMEOUT_SECONDS = 5.0
LOCK_RETRY_SECONDS = 0.02
_LOCK_CONTENTION_ERRNOS = {
    errno.EACCES,
    errno.EAGAIN,
    getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
}
_CURRENT_QUARANTINE_DIR_FD: int | None = None


class SpoolTailStatus(str, Enum):
    CLEAN = "clean"
    INCOMPLETE_EOF = "incomplete_eof"
    BAD_MAGIC = "bad_magic"
    BAD_VERSION = "bad_version"
    BAD_RECORD_KIND = "bad_record_kind"
    NONZERO_RESERVED = "nonzero_reserved"
    OVERSIZED_LENGTH = "oversized_length"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    INVALID_JSON = "invalid_json"
    INVALID_SCHEMA = "invalid_schema"
    SCAN_LIMIT_EXCEEDED = "scan_limit_exceeded"


@dataclass(frozen=True)
class SessionSpoolBootstrap:
    session_id: str | None
    source: str | None
    started_at: float | None
    model: str | None
    model_config: Any
    system_prompt: str | None
    parent_session_id: str | None
    cwd: str | None
    profile_name: str | None
    user_id: str | None
    session_key: str | None
    chat_id: str | None
    chat_type: str | None
    thread_id: str | None


@dataclass(frozen=True)
class SessionSpoolRecord:
    bootstrap: SessionSpoolBootstrap
    persist_attempt_id: str
    persist_attempt_unit_index: int
    canonical_failure: Mapping[str, Any]
    batch_messages: tuple[SessionDBBatchMessage, ...]


@dataclass(frozen=True)
class SpoolFrameReceipt:
    path: str
    offset: int
    frame_length: int
    payload_length: int
    checksum_hex: str


@dataclass(frozen=True)
class SpoolUnitAppendResult:
    persistence_unit_id: str
    message_keys: tuple[str, ...]
    receipt: SpoolFrameReceipt


@dataclass(frozen=True)
class SpoolAppendAttemptResult:
    unit_results: tuple[SpoolUnitAppendResult, ...]


@dataclass(frozen=True)
class SpoolScanResult:
    valid_prefix_bytes: int
    frame_count: int
    tail_status: SpoolTailStatus
    tail_offset: int | None


@dataclass(frozen=True)
class _AnchoredRuntime:
    home_path: Path
    root_path: Path
    quarantine_path: Path
    active_path: Path
    home_fd: int
    root_fd: int
    lock_fd: int


class SessionFallbackSpoolError(RuntimeError):
    pass


class SpoolPathSecurityError(SessionFallbackSpoolError):
    pass


class SpoolLockTimeoutError(SessionFallbackSpoolError):
    pass


class SpoolFrameTooLargeError(SessionFallbackSpoolError):
    def __init__(self, payload_bytes: int, frame_bytes: int):
        super().__init__(
            f"fallback spool frame too large: payload={payload_bytes} frame={frame_bytes}"
        )
        self.payload_bytes = payload_bytes
        self.frame_bytes = frame_bytes


class SpoolCapacityError(SessionFallbackSpoolError):
    def __init__(
        self,
        *,
        active_bytes: int,
        quarantine_bytes: int,
        requested_bytes: int,
        cap_bytes: int,
    ):
        super().__init__(
            "fallback spool capacity exceeded: "
            f"active_bytes={active_bytes} quarantine_bytes={quarantine_bytes} "
            f"requested_bytes={requested_bytes} cap_bytes={cap_bytes}"
        )
        self.active_bytes = active_bytes
        self.quarantine_bytes = quarantine_bytes
        self.requested_bytes = requested_bytes
        self.cap_bytes = cap_bytes


class SpoolDurabilityError(SessionFallbackSpoolError):
    pass


class SpoolAppendAttemptPartialError(SessionFallbackSpoolError):
    def __init__(
        self,
        durable_results: Sequence[SpoolUnitAppendResult],
        cause: BaseException,
    ):
        super().__init__(f"fallback spool append partially durable: {cause}")
        self.durable_results = tuple(durable_results)
        self.cause = cause


def _spool_root() -> Path:
    return get_hermes_home() / SPOOL_ROOT_NAME


def _active_spool_path() -> Path:
    return _spool_root() / ACTIVE_SPOOL_NAME


def _lock_path() -> Path:
    return _spool_root() / LOCK_FILE_NAME


def _quarantine_dir() -> Path:
    return _spool_root() / QUARANTINE_DIR_NAME


def _is_symlink(path: Path) -> bool:
    try:
        return stat.S_ISLNK(path.lstat().st_mode)
    except FileNotFoundError:
        return False



def _require_not_symlink(path: Path) -> None:
    if _is_symlink(path):
        raise SpoolPathSecurityError(f"symlinked fallback spool path refused: {path}")


def _require_existing_dir(path: Path) -> None:
    _require_not_symlink(path)
    try:
        st = path.stat()
    except FileNotFoundError as exc:
        raise SpoolDurabilityError(f"required directory missing: {path}") from exc
    if not stat.S_ISDIR(st.st_mode):
        raise SpoolPathSecurityError(f"fallback spool path is not a directory: {path}")
    if os.name == "posix":
        os.chmod(path, ROOT_MODE)


def _require_existing_file(path: Path) -> None:
    _require_not_symlink(path)
    try:
        st = path.stat()
    except FileNotFoundError as exc:
        raise SpoolDurabilityError(f"required file missing: {path}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise SpoolPathSecurityError(f"fallback spool path is not a regular file: {path}")
    if os.name == "posix":
        os.chmod(path, FILE_MODE)


def _supports_directory_fsync() -> bool:
    return os.name == "posix"


def _dir_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _file_open_flags(base: int) -> int:
    if hasattr(os, "O_NOFOLLOW"):
        base |= os.O_NOFOLLOW
    return base


def _fsync_fd(fd: int) -> None:
    os.fsync(fd)


def _fsync_directory(path: Path) -> None:
    if not _supports_directory_fsync():
        raise SpoolDurabilityError(
            f"directory fsync unavailable for fallback spool path: {path}"
        )
    _require_existing_dir(path)
    directory_fd = os.open(str(path), _dir_open_flags())
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _ensure_directory(path: Path, *, mode: int = ROOT_MODE) -> bool:
    _require_not_symlink(path)
    if path.exists():
        _require_existing_dir(path)
        return False
    parent = path.parent
    _require_existing_dir(parent)
    try:
        os.mkdir(path, mode)
    except FileExistsError:
        _require_existing_dir(path)
        return False
    try:
        if os.name == "posix":
            os.chmod(path, mode)
        _fsync_directory(parent)
    except OSError as exc:
        raise SpoolDurabilityError(
            f"unable to durably create fallback spool directory {path}: {exc}"
        ) from exc
    return True


def _same_file_stat(lhs: os.stat_result, rhs: os.stat_result) -> bool:
    return lhs.st_dev == rhs.st_dev and lhs.st_ino == rhs.st_ino


def _optional_str(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _json_compatible(value: Any) -> bool:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


def _validate_message_payload(payload: Mapping[str, Any]) -> bool:
    expected_keys = {
        "persistence_message_key",
        "persistence_ordinal",
        "role",
        "content",
        "timestamp",
        "tool_name",
        "tool_calls",
        "tool_call_id",
        "finish_reason",
        "reasoning",
        "reasoning_content",
        "reasoning_details",
        "codex_reasoning_items",
        "codex_message_items",
        "api_content",
        "display_kind",
        "display_metadata",
    }
    if set(payload.keys()) != expected_keys:
        return False
    if not isinstance(payload.get("persistence_message_key"), str) or not payload.get(
        "persistence_message_key"
    ):
        return False
    ordinal = payload.get("persistence_ordinal")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
        return False
    if not isinstance(payload.get("role"), str) or not payload.get("role"):
        return False
    if payload.get("content") is not None and not isinstance(payload.get("content"), str):
        return False
    timestamp = payload.get("timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        return False
    if not _optional_str(payload.get("tool_name")):
        return False
    if payload.get("tool_calls") is not None and not isinstance(payload.get("tool_calls"), list):
        return False
    if payload.get("tool_calls") is not None and not _json_compatible(payload.get("tool_calls")):
        return False
    if not _optional_str(payload.get("tool_call_id")):
        return False
    if not _optional_str(payload.get("finish_reason")):
        return False
    if not _optional_str(payload.get("reasoning")):
        return False
    if not _json_compatible(payload.get("reasoning_content")):
        return False
    if not _json_compatible(payload.get("reasoning_details")):
        return False
    if not _json_compatible(payload.get("codex_reasoning_items")):
        return False
    if not _json_compatible(payload.get("codex_message_items")):
        return False
    if payload.get("api_content") is not None and not _json_compatible(payload.get("api_content")):
        return False
    if not _optional_str(payload.get("display_kind")):
        return False
    if payload.get("display_metadata") is not None and not isinstance(
        payload.get("display_metadata"), dict
    ):
        return False
    if payload.get("display_metadata") is not None and not _json_compatible(
        payload.get("display_metadata")
    ):
        return False
    return True


def _validate_payload_schema(payload_obj: Any) -> bool:
    if not isinstance(payload_obj, dict):
        return False
    if set(payload_obj.keys()) != {
        "schema_version",
        "record_type",
        "persist_attempt_id",
        "persist_attempt_unit_index",
        "session",
        "canonical_failure",
        "unit",
    }:
        return False
    if payload_obj.get("schema_version") != 1:
        return False
    if payload_obj.get("record_type") != "session_persistence_unit":
        return False
    persist_attempt_id = payload_obj.get("persist_attempt_id")
    if not isinstance(persist_attempt_id, str) or not re.fullmatch(
        r"[0-9a-f]{32}", persist_attempt_id
    ):
        return False
    attempt_index = payload_obj.get("persist_attempt_unit_index")
    if isinstance(attempt_index, bool) or not isinstance(attempt_index, int) or attempt_index < 0:
        return False

    session = payload_obj.get("session")
    if not isinstance(session, dict) or set(session.keys()) != {
        "session_id",
        "source",
        "started_at",
        "model",
        "model_config",
        "system_prompt",
        "parent_session_id",
        "cwd",
        "profile_name",
        "user_id",
        "session_key",
        "chat_id",
        "chat_type",
        "thread_id",
    }:
        return False
    if not all(
        _optional_str(session.get(key))
        for key in (
            "session_id",
            "source",
            "model",
            "system_prompt",
            "parent_session_id",
            "cwd",
            "profile_name",
            "user_id",
            "session_key",
            "chat_id",
            "chat_type",
            "thread_id",
        )
    ):
        return False
    if session.get("started_at") is not None and not isinstance(
        session.get("started_at"), (int, float)
    ):
        return False
    if not _json_compatible(session.get("model_config")):
        return False

    canonical_failure = payload_obj.get("canonical_failure")
    if not isinstance(canonical_failure, dict) or set(canonical_failure.keys()) != {
        "stage",
        "error_class",
        "error_message",
        "session_row_created",
    }:
        return False
    if canonical_failure.get("stage") not in {
        "session_row_create",
        "append_messages_batch",
    }:
        return False
    if not isinstance(canonical_failure.get("error_class"), str) or not canonical_failure.get(
        "error_class"
    ):
        return False
    error_message = canonical_failure.get("error_message")
    if error_message is None or not isinstance(error_message, str):
        return False
    if "\n" in error_message or "\r" in error_message:
        return False
    if len(error_message.encode("utf-8", errors="ignore")) > 512:
        return False
    if not isinstance(canonical_failure.get("session_row_created"), bool):
        return False

    unit = payload_obj.get("unit")
    if not isinstance(unit, dict) or set(unit.keys()) != {
        "persistence_unit_id",
        "message_count",
        "messages",
    }:
        return False
    if not isinstance(unit.get("persistence_unit_id"), str) or not unit.get(
        "persistence_unit_id"
    ):
        return False
    message_count = unit.get("message_count")
    messages = unit.get("messages")
    if isinstance(message_count, bool) or not isinstance(message_count, int) or message_count < 1:
        return False
    if not isinstance(messages, list) or len(messages) != message_count:
        return False
    if not all(isinstance(message, dict) and _validate_message_payload(message) for message in messages):
        return False
    ordinals = [message["persistence_ordinal"] for message in messages]
    if ordinals != list(range(len(messages))):
        return False
    keys = [message["persistence_message_key"] for message in messages]
    if len(set(keys)) != len(keys):
        return False
    return True


def _message_payload(message: SessionDBBatchMessage) -> dict[str, Any]:
    return {
        "persistence_message_key": message.persistence_message_key,
        "persistence_ordinal": message.persistence_ordinal,
        "role": message.role,
        "content": message.content,
        "timestamp": message.timestamp,
        "tool_name": message.tool_name,
        "tool_calls": message.tool_calls,
        "tool_call_id": message.tool_call_id,
        "finish_reason": message.finish_reason,
        "reasoning": message.reasoning,
        "reasoning_content": message.reasoning_content,
        "reasoning_details": message.reasoning_details,
        "codex_reasoning_items": message.codex_reasoning_items,
        "codex_message_items": message.codex_message_items,
        "api_content": message.api_content,
        "display_kind": message.display_kind,
        "display_metadata": message.display_metadata,
    }


def _payload_dict_for_record(record: SessionSpoolRecord) -> dict[str, Any]:
    if not isinstance(record.persist_attempt_id, str) or not re.fullmatch(
        r"[0-9a-f]{32}", record.persist_attempt_id
    ):
        raise SpoolDurabilityError("invalid fallback spool persist_attempt_id")
    if (
        isinstance(record.persist_attempt_unit_index, bool)
        or not isinstance(record.persist_attempt_unit_index, int)
        or record.persist_attempt_unit_index < 0
    ):
        raise SpoolDurabilityError("invalid fallback spool persist_attempt_unit_index")
    if not record.batch_messages:
        raise SpoolDurabilityError("fallback spool record requires at least one message")

    unit_id = record.batch_messages[0].persistence_unit_id
    if not isinstance(unit_id, str) or not unit_id:
        raise SpoolDurabilityError("invalid fallback spool persistence_unit_id")
    seen_keys: set[str] = set()
    payload_messages = []
    for expected_ordinal, message in enumerate(record.batch_messages):
        if message.persistence_unit_id != unit_id:
            raise SpoolDurabilityError("mixed persistence_unit_id values are not allowed")
        if message.persistence_ordinal != expected_ordinal:
            raise SpoolDurabilityError(
                "fallback spool messages must be stored in ordinal order 0..n-1"
            )
        if not isinstance(message.persistence_message_key, str) or not message.persistence_message_key:
            raise SpoolDurabilityError("invalid fallback spool persistence_message_key")
        if message.persistence_message_key in seen_keys:
            raise SpoolDurabilityError("duplicate fallback spool persistence_message_key")
        seen_keys.add(message.persistence_message_key)
        payload_messages.append(_message_payload(message))

    payload = {
        "schema_version": 1,
        "record_type": "session_persistence_unit",
        "persist_attempt_id": record.persist_attempt_id,
        "persist_attempt_unit_index": record.persist_attempt_unit_index,
        "session": {
            "session_id": record.bootstrap.session_id,
            "source": record.bootstrap.source,
            "started_at": record.bootstrap.started_at,
            "model": record.bootstrap.model,
            "model_config": record.bootstrap.model_config,
            "system_prompt": record.bootstrap.system_prompt,
            "parent_session_id": record.bootstrap.parent_session_id,
            "cwd": record.bootstrap.cwd,
            "profile_name": record.bootstrap.profile_name,
            "user_id": record.bootstrap.user_id,
            "session_key": record.bootstrap.session_key,
            "chat_id": record.bootstrap.chat_id,
            "chat_type": record.bootstrap.chat_type,
            "thread_id": record.bootstrap.thread_id,
        },
        "canonical_failure": {
            "stage": record.canonical_failure.get("stage"),
            "error_class": record.canonical_failure.get("error_class"),
            "error_message": record.canonical_failure.get("error_message"),
            "session_row_created": record.canonical_failure.get(
                "session_row_created"
            ),
        },
        "unit": {
            "persistence_unit_id": unit_id,
            "message_count": len(payload_messages),
            "messages": payload_messages,
        },
    }
    if not _validate_payload_schema(payload):
        raise SpoolDurabilityError("invalid fallback spool record payload")
    return payload


class _DuplicateJsonKeyError(ValueError):
    pass


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise _DuplicateJsonKeyError(key)
        obj[key] = value
    return obj


def _payload_bytes_for_record(record: SessionSpoolRecord) -> bytes:
    payload = json.dumps(
        _payload_dict_for_record(record),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise SpoolFrameTooLargeError(len(payload), len(payload) + HEADER_SIZE)
    return payload


def _frame_from_payload_bytes(
    payload: bytes,
    *,
    record_kind: int = RECORD_KIND_SESSION_PERSISTENCE_UNIT,
    reserved_bytes: bytes = b"\x00\x00",
) -> bytes:
    payload_len = len(payload)
    if payload_len == 0 or payload_len > MAX_PAYLOAD_BYTES:
        raise SpoolFrameTooLargeError(payload_len, payload_len + HEADER_SIZE)
    if len(reserved_bytes) != 2:
        raise SpoolDurabilityError("reserved header field must be exactly two bytes")
    header_prefix = bytes([FRAME_VERSION, record_kind]) + reserved_bytes + payload_len.to_bytes(
        8, "big"
    )
    digest = blake2s(header_prefix + payload, digest_size=16).digest()
    frame = HEADER_MAGIC + header_prefix + digest + payload
    if len(frame) > MAX_FRAME_BYTES:
        raise SpoolFrameTooLargeError(payload_len, len(frame))
    return frame


def _frame_bytes_for_record(record: SessionSpoolRecord) -> bytes:
    return _frame_from_payload_bytes(_payload_bytes_for_record(record))


def _require_secure_path_primitives() -> None:
    if os.name != "posix":
        raise SpoolPathSecurityError(
            "descriptor-anchored fallback spool path security is unavailable on this platform"
        )
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise SpoolPathSecurityError(
            "descriptor-anchored fallback spool path security requires O_NOFOLLOW and O_DIRECTORY"
        )


def _open_home_dir_fd(home_path: Path) -> int:
    _require_secure_path_primitives()
    try:
        fd = os.open(str(home_path), os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise SpoolDurabilityError(
            f"unable to open HERMES_HOME for fallback spool security: {home_path}: {exc}"
        ) from exc
    home_stat = os.fstat(fd)
    if not stat.S_ISDIR(home_stat.st_mode):
        os.close(fd)
        raise SpoolPathSecurityError(f"HERMES_HOME is not a directory: {home_path}")
    return fd


def _fsync_directory_fd(fd: int, label: Path | str) -> None:
    if not _supports_directory_fsync():
        raise SpoolDurabilityError(
            f"directory fsync unavailable for fallback spool path: {label}"
        )
    try:
        os.fsync(fd)
    except OSError as exc:
        raise SpoolDurabilityError(
            f"unable to fsync fallback spool directory {label}: {exc}"
        ) from exc


def _close_fd_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _open_dir_at(
    parent_fd: int,
    name: str,
    *,
    full_path: Path,
    mode: int,
    create: bool,
    parent_label: Path | str,
    fsync_parent_on_open_existing: bool = False,
) -> tuple[int, bool]:
    open_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    created = False
    while True:
        try:
            fd = os.open(name, open_flags, dir_fd=parent_fd)
            break
        except FileNotFoundError:
            if not create:
                raise SpoolDurabilityError(
                    f"required fallback spool directory missing: {full_path}"
                )
            try:
                os.mkdir(name, mode, dir_fd=parent_fd)
            except FileExistsError:
                continue
            except OSError as exc:
                raise SpoolDurabilityError(
                    f"unable to create fallback spool directory {full_path}: {exc}"
                ) from exc
            created = True
            continue
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise SpoolPathSecurityError(
                    f"symlinked fallback spool directory refused: {full_path}"
                ) from exc
            raise SpoolDurabilityError(
                f"unable to open fallback spool directory {full_path}: {exc}"
            ) from exc
    try:
        dir_stat = os.fstat(fd)
        if not stat.S_ISDIR(dir_stat.st_mode):
            raise SpoolPathSecurityError(
                f"fallback spool path is not a directory: {full_path}"
            )
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        if created:
            _fsync_directory_fd(parent_fd, parent_label)
        elif fsync_parent_on_open_existing:
            _fsync_directory_fd(parent_fd, parent_label)
        _assert_entry_matches_fd(parent_fd, name, fd, expect="dir", label=str(full_path))
    except SessionFallbackSpoolError:
        _close_fd_quietly(fd)
        raise
    except OSError as exc:
        _close_fd_quietly(fd)
        raise SpoolDurabilityError(
            f"unable to durably open fallback spool directory {full_path}: {exc}"
        ) from exc
    except BaseException:
        _close_fd_quietly(fd)
        raise
    return fd, created


def _open_file_at(
    parent_fd: int,
    name: str,
    *,
    full_path: Path,
    mode: int,
    create: bool,
    fsync_parent_on_create: bool,
    fsync_file_on_create: bool,
    parent_label: Path | str,
    fsync_parent_on_open_existing: bool = False,
) -> tuple[int, bool]:
    open_flags = os.O_RDWR | os.O_NOFOLLOW
    created = False
    while True:
        try:
            fd = os.open(name, open_flags, dir_fd=parent_fd)
            break
        except FileNotFoundError:
            if not create:
                raise SpoolDurabilityError(
                    f"required fallback spool file missing: {full_path}"
                )
            try:
                fd = os.open(name, open_flags | os.O_CREAT | os.O_EXCL, mode, dir_fd=parent_fd)
            except FileExistsError:
                continue
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise SpoolPathSecurityError(
                        f"symlinked fallback spool file refused: {full_path}"
                    ) from exc
                raise SpoolDurabilityError(
                    f"unable to create fallback spool file {full_path}: {exc}"
                ) from exc
            created = True
            break
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise SpoolPathSecurityError(
                    f"symlinked fallback spool file refused: {full_path}"
                ) from exc
            raise SpoolDurabilityError(
                f"unable to open fallback spool file {full_path}: {exc}"
            ) from exc
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise SpoolPathSecurityError(
                f"fallback spool path is not a regular file: {full_path}"
            )
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        if created:
            if fsync_file_on_create:
                _fsync_fd(fd)
            if fsync_parent_on_create:
                _fsync_directory_fd(parent_fd, parent_label)
        elif fsync_parent_on_open_existing:
            _fsync_directory_fd(parent_fd, parent_label)
        _assert_entry_matches_fd(parent_fd, name, fd, expect="file", label=str(full_path))
    except SessionFallbackSpoolError:
        _close_fd_quietly(fd)
        raise
    except OSError as exc:
        _close_fd_quietly(fd)
        action = "create" if created else "open"
        raise SpoolDurabilityError(
            f"unable to durably {action} fallback spool file {full_path}: {exc}"
        ) from exc
    except BaseException:
        _close_fd_quietly(fd)
        raise
    return fd, created


def _assert_home_matches_fd(home_path: Path, home_fd: int) -> None:
    try:
        current_stat = home_path.stat()
    except FileNotFoundError as exc:
        raise SpoolDurabilityError(
            f"HERMES_HOME disappeared during fallback spool append: {home_path}"
        ) from exc
    if not _same_file_stat(current_stat, os.fstat(home_fd)):
        raise SpoolPathSecurityError(
            f"HERMES_HOME changed during fallback spool append: {home_path}"
        )


def _assert_entry_matches_fd(
    parent_fd: int,
    name: str,
    fd: int,
    *,
    expect: str,
    label: str,
) -> None:
    try:
        entry_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise SpoolDurabilityError(
            f"fallback spool entry disappeared before durability was confirmed: {label}"
        ) from exc
    except OSError as exc:
        raise SpoolDurabilityError(
            f"unable to restat fallback spool entry {label}: {exc}"
        ) from exc
    target_stat = os.fstat(fd)
    if expect == "dir":
        if not stat.S_ISDIR(entry_stat.st_mode):
            raise SpoolPathSecurityError(f"fallback spool path is not a directory: {label}")
    elif expect == "file":
        if not stat.S_ISREG(entry_stat.st_mode):
            raise SpoolPathSecurityError(f"fallback spool path is not a regular file: {label}")
    else:  # pragma: no cover - internal misuse guard
        raise ValueError(f"unknown expectation: {expect}")
    if not _same_file_stat(entry_stat, target_stat):
        raise SpoolPathSecurityError(
            f"fallback spool entry was swapped during append: {label}"
        )


def _is_lock_contention_error(exc: OSError) -> bool:
    return exc.errno in _LOCK_CONTENTION_ERRNOS


@contextmanager
def _append_lock(lock_fd: int, lock_label: str):
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    locked = False
    try:
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif msvcrt is not None:  # pragma: no cover - Windows-only branch
                    msvcrt.locking(lock_fd, msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover - unsupported platform
                    raise SpoolDurabilityError("no secure file-locking primitive available")
                locked = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SpoolLockTimeoutError(
                        f"timed out waiting for fallback spool append lock: {lock_label}"
                    )
                time.sleep(LOCK_RETRY_SECONDS)
            except OSError as exc:
                if _is_lock_contention_error(exc):
                    if time.monotonic() >= deadline:
                        raise SpoolLockTimeoutError(
                            f"timed out waiting for fallback spool append lock: {lock_label}"
                        ) from exc
                    time.sleep(LOCK_RETRY_SECONDS)
                    continue
                raise SpoolDurabilityError(
                    f"unexpected fallback spool lock failure for {lock_label}: {exc}"
                ) from exc
        yield
    finally:
        if locked:
            try:
                if fcntl is not None:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                elif msvcrt is not None:  # pragma: no cover - Windows-only branch
                    msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass


def _read_exact_from_fd(fd: int, *, offset: int, length: int) -> bytes:
    if length <= 0:
        return b""
    chunks = bytearray()
    while len(chunks) < length:
        remaining = length - len(chunks)
        try:
            if hasattr(os, "pread"):
                chunk = os.pread(fd, remaining, offset + len(chunks))
            else:  # pragma: no cover - fallback for runtimes without pread
                os.lseek(fd, offset + len(chunks), os.SEEK_SET)
                chunk = os.read(fd, remaining)
        except InterruptedError:
            continue
        if not chunk:
            break
        chunks.extend(chunk)
    return bytes(chunks)


def _scan_fd(
    fd: int,
    *,
    max_file_bytes: int = TOTAL_CAP_BYTES,
    max_frame_bytes: int = MAX_FRAME_BYTES,
) -> SpoolScanResult:
    file_size = os.fstat(fd).st_size
    if file_size <= 0:
        return SpoolScanResult(
            valid_prefix_bytes=0,
            frame_count=0,
            tail_status=SpoolTailStatus.CLEAN,
            tail_offset=None,
        )
    payload_cap = max_frame_bytes - HEADER_SIZE
    budget = max_file_bytes if max_file_bytes > 0 else file_size
    offset = 0
    frame_count = 0
    while offset < file_size:
        if offset + HEADER_SIZE > budget:
            return SpoolScanResult(
                valid_prefix_bytes=offset,
                frame_count=frame_count,
                tail_status=SpoolTailStatus.SCAN_LIMIT_EXCEEDED,
                tail_offset=offset,
            )
        header = _read_exact_from_fd(fd, offset=offset, length=HEADER_SIZE)
        if len(header) < HEADER_SIZE:
            return SpoolScanResult(
                valid_prefix_bytes=offset,
                frame_count=frame_count,
                tail_status=SpoolTailStatus.INCOMPLETE_EOF,
                tail_offset=offset,
            )
        if header[:4] != HEADER_MAGIC:
            return SpoolScanResult(
                valid_prefix_bytes=offset,
                frame_count=frame_count,
                tail_status=SpoolTailStatus.BAD_MAGIC,
                tail_offset=offset,
            )
        if header[4] != FRAME_VERSION:
            return SpoolScanResult(
                valid_prefix_bytes=offset,
                frame_count=frame_count,
                tail_status=SpoolTailStatus.BAD_VERSION,
                tail_offset=offset,
            )
        if header[5] != RECORD_KIND_SESSION_PERSISTENCE_UNIT:
            return SpoolScanResult(
                valid_prefix_bytes=offset,
                frame_count=frame_count,
                tail_status=SpoolTailStatus.BAD_RECORD_KIND,
                tail_offset=offset,
            )
        if header[6:8] != b"\x00\x00":
            return SpoolScanResult(
                valid_prefix_bytes=offset,
                frame_count=frame_count,
                tail_status=SpoolTailStatus.NONZERO_RESERVED,
                tail_offset=offset,
            )
        payload_len = int.from_bytes(header[8:16], "big")
        if payload_len == 0 or payload_len > payload_cap:
            return SpoolScanResult(
                valid_prefix_bytes=offset,
                frame_count=frame_count,
                tail_status=SpoolTailStatus.OVERSIZED_LENGTH,
                tail_offset=offset,
            )
        frame_len = HEADER_SIZE + payload_len
        if offset + frame_len > budget:
            return SpoolScanResult(
                valid_prefix_bytes=offset,
                frame_count=frame_count,
                tail_status=SpoolTailStatus.SCAN_LIMIT_EXCEEDED,
                tail_offset=offset,
            )
        payload = _read_exact_from_fd(fd, offset=offset + HEADER_SIZE, length=payload_len)
        if len(payload) < payload_len:
            return SpoolScanResult(
                valid_prefix_bytes=offset,
                frame_count=frame_count,
                tail_status=SpoolTailStatus.INCOMPLETE_EOF,
                tail_offset=offset,
            )
        expected_digest = blake2s(header[4:16] + payload, digest_size=16).digest()
        if header[16:32] != expected_digest:
            return SpoolScanResult(
                valid_prefix_bytes=offset,
                frame_count=frame_count,
                tail_status=SpoolTailStatus.CHECKSUM_MISMATCH,
                tail_offset=offset,
            )
        try:
            payload_obj = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKeyError):
            return SpoolScanResult(
                valid_prefix_bytes=offset,
                frame_count=frame_count,
                tail_status=SpoolTailStatus.INVALID_JSON,
                tail_offset=offset,
            )
        if not _validate_payload_schema(payload_obj):
            return SpoolScanResult(
                valid_prefix_bytes=offset,
                frame_count=frame_count,
                tail_status=SpoolTailStatus.INVALID_SCHEMA,
                tail_offset=offset,
            )
        frame_count += 1
        offset += frame_len
    if offset < file_size:
        return SpoolScanResult(
            valid_prefix_bytes=offset,
            frame_count=frame_count,
            tail_status=SpoolTailStatus.SCAN_LIMIT_EXCEEDED,
            tail_offset=offset,
        )
    return SpoolScanResult(
        valid_prefix_bytes=offset,
        frame_count=frame_count,
        tail_status=SpoolTailStatus.CLEAN,
        tail_offset=None,
    )


def scan_spool(
    path: Path,
    *,
    max_file_bytes: int = TOTAL_CAP_BYTES,
    max_frame_bytes: int = MAX_FRAME_BYTES,
) -> SpoolScanResult:
    if not path.exists():
        return SpoolScanResult(
            valid_prefix_bytes=0,
            frame_count=0,
            tail_status=SpoolTailStatus.CLEAN,
            tail_offset=None,
        )
    _require_existing_file(path)
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        return _scan_fd(fd, max_file_bytes=max_file_bytes, max_frame_bytes=max_frame_bytes)
    finally:
        os.close(fd)


def _iter_quarantine_entries(quarantine_dir: Path):
    if _CURRENT_QUARANTINE_DIR_FD is not None:
        return list(os.scandir(_CURRENT_QUARANTINE_DIR_FD))
    return list(quarantine_dir.iterdir())


def _next_quarantine_sequence(quarantine_dir: Path) -> int:
    max_seq = 0
    for entry in _iter_quarantine_entries(quarantine_dir):
        name = entry.name if hasattr(entry, "name") else entry.name
        match = re.match(r"^(\d{6})-", name)
        if match:
            max_seq = max(max_seq, int(match.group(1)))
    return max_seq + 1


def _parse_quarantine_spool_name(name: str) -> tuple[int, str, int] | None:
    match = re.fullmatch(r"(\d{6})-([a-z0-9_]+)-vp(\d+)\.spool", name)
    if not match:
        return None
    seq = int(match.group(1))
    status = match.group(2)
    valid_prefix = int(match.group(3))
    return seq, status, valid_prefix


def _quarantine_sidecar_payload_from_file(
    quarantine_dir: Path,
    spool_name: str,
    *,
    directory_fd: int,
) -> dict[str, Any]:
    parsed = _parse_quarantine_spool_name(spool_name)
    if parsed is None:
        raise SpoolDurabilityError(
            f"invalid quarantine spool filename for reconciliation: {spool_name}"
        )
    sequence, expected_status, expected_valid_prefix = parsed
    spool_fd = os.open(spool_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        spool_stat = os.fstat(spool_fd)
        if not stat.S_ISREG(spool_stat.st_mode):
            raise SpoolPathSecurityError(
                f"quarantine evidence is not a regular file: {quarantine_dir / spool_name}"
            )
        scan = _scan_fd(spool_fd)
    finally:
        os.close(spool_fd)
    if scan.tail_status.value != expected_status or scan.valid_prefix_bytes != expected_valid_prefix:
        raise SpoolDurabilityError(
            "quarantine evidence no longer matches its durable filename metadata: "
            f"{quarantine_dir / spool_name}"
        )
    return {
        "sequence": sequence,
        "tail_status": expected_status,
        "valid_prefix_bytes": expected_valid_prefix,
        "original_size": int(spool_stat.st_size),
        "quarantined_at": float(spool_stat.st_mtime),
    }


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    written = 0
    while written < len(view):
        try:
            chunk = os.write(fd, bytes(view[written:]))
        except InterruptedError:
            continue
        if chunk <= 0:
            raise SpoolDurabilityError("short write while appending fallback spool frame")
        written += chunk


def _write_sidecar_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    directory_fd: int | None = None,
) -> None:
    temp_name = f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    fd = -1
    temp_path = path.parent / temp_name
    if directory_fd is None:
        _require_existing_dir(path.parent)
        fd = os.open(
            str(temp_path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            FILE_MODE,
        )
        dir_fd = os.open(str(path.parent), _dir_open_flags())
        cleanup_by_name = False
    else:
        dir_fd = directory_fd
        fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            FILE_MODE,
            dir_fd=dir_fd,
        )
        cleanup_by_name = True
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, FILE_MODE)
        data = json.dumps(
            dict(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        _write_all(fd, data)
        _fsync_fd(fd)
        os.close(fd)
        fd = -1
        try:
            if directory_fd is None:
                os.link(str(temp_path), str(path), follow_symlinks=False)
            else:
                os.link(temp_name, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
        except FileExistsError as exc:
            raise SpoolPathSecurityError(
                f"fallback spool sidecar destination already exists or was swapped: {path}"
            ) from exc
        _fsync_directory_fd(dir_fd, path.parent)
        if directory_fd is None:
            os.unlink(temp_path)
        else:
            os.unlink(temp_name, dir_fd=dir_fd)
        _fsync_directory_fd(dir_fd, path.parent)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            if cleanup_by_name:
                os.unlink(temp_name, dir_fd=dir_fd)
            else:
                temp_path.unlink()
        except OSError:
            pass
        raise
    finally:
        if directory_fd is None:
            os.close(dir_fd)


def _reconcile_missing_sidecars(quarantine_dir: Path, *, quarantine_fd: int) -> None:
    for entry in os.scandir(quarantine_fd):
        if entry.is_symlink():
            raise SpoolPathSecurityError(
                f"symlinked quarantine entry refused: {quarantine_dir / entry.name}"
            )
    spool_names = sorted(
        entry.name
        for entry in os.scandir(quarantine_fd)
        if entry.is_file(follow_symlinks=False) and entry.name.endswith(".spool")
    )
    for spool_name in spool_names:
        sidecar_name = f"{spool_name[:-6]}.json"
        try:
            sidecar_stat = os.stat(sidecar_name, dir_fd=quarantine_fd, follow_symlinks=False)
        except FileNotFoundError:
            sidecar_payload = _quarantine_sidecar_payload_from_file(
                quarantine_dir,
                spool_name,
                directory_fd=quarantine_fd,
            )
            _write_sidecar_json(
                quarantine_dir / sidecar_name,
                sidecar_payload,
                directory_fd=quarantine_fd,
            )
            continue
        if not stat.S_ISREG(sidecar_stat.st_mode):
            raise SpoolPathSecurityError(
                f"quarantine sidecar path is not a regular file: {quarantine_dir / sidecar_name}"
            )


def _find_quarantine_hardlink(
    quarantine_fd: int,
    *,
    target_stat: os.stat_result,
) -> str | None:
    for entry in os.scandir(quarantine_fd):
        if not entry.name.endswith(".spool") or not entry.is_file(follow_symlinks=False):
            continue
        entry_stat = entry.stat(follow_symlinks=False)
        if _same_file_stat(entry_stat, target_stat):
            return entry.name
    return None


def _quarantine_spool_bytes(quarantine_dir: Path) -> int:
    total = 0
    if _CURRENT_QUARANTINE_DIR_FD is not None:
        for entry in os.scandir(_CURRENT_QUARANTINE_DIR_FD):
            if entry.is_symlink():
                raise SpoolPathSecurityError(
                    f"symlinked quarantine entry refused: {quarantine_dir / entry.name}"
                )
            if entry.is_file(follow_symlinks=False) and entry.name.endswith(".spool"):
                total += int(entry.stat(follow_symlinks=False).st_size)
        return total
    if not quarantine_dir.exists():
        return 0
    for path in quarantine_dir.glob("*.spool"):
        _require_existing_file(path)
        total += path.stat().st_size
    return total


def _quarantine_active_file(
    active_path: Path,
    quarantine_dir: Path,
    scan_result: SpoolScanResult,
    *,
    runtime: _AnchoredRuntime,
    quarantine_fd: int,
    active_fd: int,
) -> None:
    _assert_entry_matches_fd(
        runtime.root_fd,
        QUARANTINE_DIR_NAME,
        quarantine_fd,
        expect="dir",
        label=str(quarantine_dir),
    )
    active_stat = os.fstat(active_fd)
    duplicate_name = _find_quarantine_hardlink(quarantine_fd, target_stat=active_stat)
    if duplicate_name is not None:
        sidecar_name = f"{duplicate_name[:-6]}.json"
        try:
            os.stat(sidecar_name, dir_fd=quarantine_fd, follow_symlinks=False)
        except FileNotFoundError:
            _write_sidecar_json(
                quarantine_dir / sidecar_name,
                _quarantine_sidecar_payload_from_file(
                    quarantine_dir,
                    duplicate_name,
                    directory_fd=quarantine_fd,
                ),
                directory_fd=quarantine_fd,
            )
        _assert_home_matches_fd(runtime.home_path, runtime.home_fd)
        _assert_entry_matches_fd(
            runtime.home_fd,
            SPOOL_ROOT_NAME,
            runtime.root_fd,
            expect="dir",
            label=str(runtime.root_path),
        )
        _assert_entry_matches_fd(
            runtime.root_fd,
            ACTIVE_SPOOL_NAME,
            active_fd,
            expect="file",
            label=str(active_path),
        )
        os.unlink(ACTIVE_SPOOL_NAME, dir_fd=runtime.root_fd)
        _fsync_directory_fd(runtime.root_fd, runtime.root_path)
        return

    seq = _next_quarantine_sequence(quarantine_dir)
    _assert_entry_matches_fd(
        runtime.root_fd,
        QUARANTINE_DIR_NAME,
        quarantine_fd,
        expect="dir",
        label=str(quarantine_dir),
    )
    while True:
        base = f"{seq:06d}-{scan_result.tail_status.value}-vp{scan_result.valid_prefix_bytes}"
        spool_name = f"{base}.spool"
        sidecar_path = quarantine_dir / f"{base}.json"
        try:
            os.link(
                ACTIVE_SPOOL_NAME,
                spool_name,
                src_dir_fd=runtime.root_fd,
                dst_dir_fd=quarantine_fd,
                follow_symlinks=False,
            )
            break
        except FileExistsError:
            seq += 1
            continue
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise SpoolPathSecurityError(
                    f"symlinked quarantine target refused: {quarantine_dir / spool_name}"
                ) from exc
            raise SpoolDurabilityError(
                f"unable to quarantine fallback spool evidence {quarantine_dir / spool_name}: {exc}"
            ) from exc
    _fsync_directory_fd(quarantine_fd, quarantine_dir)
    _write_sidecar_json(
        sidecar_path,
        {
            "sequence": seq,
            "tail_status": scan_result.tail_status.value,
            "valid_prefix_bytes": scan_result.valid_prefix_bytes,
            "original_size": int(active_stat.st_size),
            "quarantined_at": time.time(),
        },
        directory_fd=quarantine_fd,
    )
    _assert_home_matches_fd(runtime.home_path, runtime.home_fd)
    _assert_entry_matches_fd(
        runtime.home_fd,
        SPOOL_ROOT_NAME,
        runtime.root_fd,
        expect="dir",
        label=str(runtime.root_path),
    )
    _assert_entry_matches_fd(
        runtime.root_fd,
        ACTIVE_SPOOL_NAME,
        active_fd,
        expect="file",
        label=str(active_path),
    )
    os.unlink(ACTIVE_SPOOL_NAME, dir_fd=runtime.root_fd)
    _fsync_directory_fd(runtime.root_fd, runtime.root_path)


def _open_locked_runtime() -> _AnchoredRuntime:
    home_path = Path(get_hermes_home())
    root_path = _spool_root()
    active_path = _active_spool_path()
    quarantine_path = _quarantine_dir()
    home_fd = _open_home_dir_fd(home_path)
    root_fd = -1
    lock_fd = -1
    try:
        root_fd, _ = _open_dir_at(
            home_fd,
            SPOOL_ROOT_NAME,
            full_path=root_path,
            mode=ROOT_MODE,
            create=True,
            parent_label=home_path,
            fsync_parent_on_open_existing=True,
        )
        lock_fd, _ = _open_file_at(
            root_fd,
            LOCK_FILE_NAME,
            full_path=_lock_path(),
            mode=FILE_MODE,
            create=True,
            fsync_parent_on_create=True,
            fsync_file_on_create=False,
            parent_label=root_path,
            fsync_parent_on_open_existing=True,
        )
        return _AnchoredRuntime(
            home_path=home_path,
            root_path=root_path,
            quarantine_path=quarantine_path,
            active_path=active_path,
            home_fd=home_fd,
            root_fd=root_fd,
            lock_fd=lock_fd,
        )
    except BaseException:
        if lock_fd >= 0:
            os.close(lock_fd)
        if root_fd >= 0:
            os.close(root_fd)
        os.close(home_fd)
        raise


def append_records(records: Sequence[SessionSpoolRecord]) -> SpoolAppendAttemptResult:
    if not records:
        return SpoolAppendAttemptResult(unit_results=())

    frames = []
    for record in records:
        frames.append((record, _frame_bytes_for_record(record)))

    runtime = _open_locked_runtime()
    quarantine_fd = -1
    active_fd = -1
    global _CURRENT_QUARANTINE_DIR_FD
    try:
        with _append_lock(runtime.lock_fd, str(_lock_path())):
            _assert_home_matches_fd(runtime.home_path, runtime.home_fd)
            _assert_entry_matches_fd(
                runtime.home_fd,
                SPOOL_ROOT_NAME,
                runtime.root_fd,
                expect="dir",
                label=str(runtime.root_path),
            )
            quarantine_fd, _ = _open_dir_at(
                runtime.root_fd,
                QUARANTINE_DIR_NAME,
                full_path=runtime.quarantine_path,
                mode=ROOT_MODE,
                create=True,
                parent_label=runtime.root_path,
                fsync_parent_on_open_existing=True,
            )
            _assert_entry_matches_fd(
                runtime.root_fd,
                QUARANTINE_DIR_NAME,
                quarantine_fd,
                expect="dir",
                label=str(runtime.quarantine_path),
            )
            _CURRENT_QUARANTINE_DIR_FD = quarantine_fd
            _reconcile_missing_sidecars(runtime.quarantine_path, quarantine_fd=quarantine_fd)

            active_fd, _ = _open_file_at(
                runtime.root_fd,
                ACTIVE_SPOOL_NAME,
                full_path=runtime.active_path,
                mode=FILE_MODE,
                create=True,
                fsync_parent_on_create=True,
                fsync_file_on_create=True,
                parent_label=runtime.root_path,
                fsync_parent_on_open_existing=True,
            )
            scan = _scan_fd(active_fd)
            if scan.tail_status is not SpoolTailStatus.CLEAN:
                _quarantine_active_file(
                    runtime.active_path,
                    runtime.quarantine_path,
                    scan,
                    runtime=runtime,
                    quarantine_fd=quarantine_fd,
                    active_fd=active_fd,
                )
                os.close(active_fd)
                active_fd = -1
                active_fd, _ = _open_file_at(
                    runtime.root_fd,
                    ACTIVE_SPOOL_NAME,
                    full_path=runtime.active_path,
                    mode=FILE_MODE,
                    create=True,
                    fsync_parent_on_create=True,
                    fsync_file_on_create=True,
                    parent_label=runtime.root_path,
                    fsync_parent_on_open_existing=True,
                )

            active_bytes = os.fstat(active_fd).st_size
            quarantine_bytes = _quarantine_spool_bytes(runtime.quarantine_path)
            requested_bytes = sum(len(frame) for _record, frame in frames)
            if active_bytes + quarantine_bytes + requested_bytes > TOTAL_CAP_BYTES:
                raise SpoolCapacityError(
                    active_bytes=active_bytes,
                    quarantine_bytes=quarantine_bytes,
                    requested_bytes=requested_bytes,
                    cap_bytes=TOTAL_CAP_BYTES,
                )

            durable_results: list[SpoolUnitAppendResult] = []
            for record, frame in frames:
                offset = os.lseek(active_fd, 0, os.SEEK_END)
                try:
                    _write_all(active_fd, frame)
                    _fsync_fd(active_fd)
                    _fsync_directory_fd(runtime.root_fd, runtime.root_path)
                    _fsync_directory_fd(runtime.home_fd, runtime.home_path)
                    _assert_home_matches_fd(runtime.home_path, runtime.home_fd)
                    _assert_entry_matches_fd(
                        runtime.home_fd,
                        SPOOL_ROOT_NAME,
                        runtime.root_fd,
                        expect="dir",
                        label=str(runtime.root_path),
                    )
                    _assert_entry_matches_fd(
                        runtime.root_fd,
                        ACTIVE_SPOOL_NAME,
                        active_fd,
                        expect="file",
                        label=str(runtime.active_path),
                    )
                except BaseException as exc:
                    cause = (
                        exc
                        if isinstance(exc, SessionFallbackSpoolError)
                        else SpoolDurabilityError(str(exc))
                    )
                    if durable_results:
                        raise SpoolAppendAttemptPartialError(durable_results, cause) from exc
                    if isinstance(cause, SessionFallbackSpoolError):
                        raise cause from exc
                    raise SpoolDurabilityError(str(exc)) from exc
                receipt = SpoolFrameReceipt(
                    path=str(runtime.active_path),
                    offset=offset,
                    frame_length=len(frame),
                    payload_length=len(frame) - HEADER_SIZE,
                    checksum_hex=frame[16:32].hex(),
                )
                durable_results.append(
                    SpoolUnitAppendResult(
                        persistence_unit_id=record.batch_messages[0].persistence_unit_id,
                        message_keys=tuple(
                            message.persistence_message_key for message in record.batch_messages
                        ),
                        receipt=receipt,
                    )
                )
            return SpoolAppendAttemptResult(unit_results=tuple(durable_results))
    finally:
        _CURRENT_QUARANTINE_DIR_FD = None
        if active_fd >= 0:
            os.close(active_fd)
        if quarantine_fd >= 0:
            os.close(quarantine_fd)
        os.close(runtime.lock_fd)
        os.close(runtime.root_fd)
        os.close(runtime.home_fd)
