"""Typed terminal lifecycle evidence for dispatcher-supervised workers."""

from __future__ import annotations

import json
import os
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional

SCHEMA_VERSION = 3
TRANSIENT_PROVIDER = "transient_provider"

_EVENT_PATH_ENV = "HERMES_WORKER_LIFECYCLE_EVENT_PATH"
_ATTEMPT_ENV = "HERMES_WORKER_LIFECYCLE_ATTEMPT"
_NONCE_ENV = "HERMES_WORKER_START_NONCE"
_TASK_ENV = "HERMES_KANBAN_TASK"
_RUN_ENV = "HERMES_KANBAN_RUN_ID"
_SESSION_ENV = "HERMES_WORKER_SESSION_ID"
_WORKTREE_ENV = "HERMES_KANBAN_WORKSPACE"
_PROFILE_ENV = "HERMES_PROFILE"
_PROVIDER_ENV = "HERMES_PROVIDER"
_CREDENTIAL_GENERATION_ENV = "HERMES_PROVIDER_CREDENTIAL_GENERATION"


class LifecycleEventType(str, Enum):
    TERMINAL = "terminal"
    IDENTITY = "identity"
    OWNED_PROCESS = "owned_process"
    FAILURE = "failure"
    TASK_DONE = "task_done"


class TerminalClassification(str, Enum):
    SUCCESS = "success"
    TRANSIENT_PROVIDER = TRANSIENT_PROVIDER
    RATE_LIMITED = "rate_limited"
    BILLING = "billing"
    FAILED = "failed"
    SUPERVISOR_FAILURE = "supervisor_failure"
    OWNERSHIP_LOSS = "ownership_loss"


class FailureReason(str, Enum):
    NONE = "none"
    TRANSIENT_PROVIDER = TRANSIENT_PROVIDER
    RATE_LIMIT = "rate_limit"
    BILLING = "billing"
    WORKER_FAILURE = "worker_failure"
    SUPERVISOR_FAILURE = "supervisor_failure"
    OWNERSHIP_LOSS = "ownership_loss"


class ExitKind(str, Enum):
    CODE = "code"
    SIGNAL = "signal"


def process_birth_token(pid: int) -> Optional[str]:
    """Return an OS-backed process birth identity, or fail closed."""
    try:
        process_id = int(pid)
        if process_id <= 0:
            return None

        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_process = kernel32.OpenProcess
            open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            open_process.restype = wintypes.HANDLE
            get_process_times = kernel32.GetProcessTimes
            get_process_times.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            )
            get_process_times.restype = wintypes.BOOL
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            close_handle.restype = wintypes.BOOL

            handle = open_process(0x1000, False, process_id)
            if not handle:
                return None
            try:
                creation = wintypes.FILETIME()
                exit_time = wintypes.FILETIME()
                kernel_time = wintypes.FILETIME()
                user_time = wintypes.FILETIME()
                if not get_process_times(
                    handle,
                    ctypes.byref(creation),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel_time),
                    ctypes.byref(user_time),
                ):
                    return None
                creation_ticks = (
                    int(creation.dwHighDateTime) << 32
                ) | int(creation.dwLowDateTime)
                return f"win32-filetime:{creation_ticks}"
            finally:
                close_handle(handle)

        if os.name == "posix":
            stat = Path(f"/proc/{process_id}/stat").read_text(encoding="ascii")
            closing_paren = stat.rfind(")")
            if closing_paren < 0:
                return None
            fields = stat[closing_paren + 1 :].split()
            start_ticks = fields[19]
            if not start_ticks.isdecimal():
                return None
            try:
                boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
                    encoding="ascii"
                ).strip()
            except OSError:
                boot_id = ""
            if boot_id:
                return f"proc-start:{boot_id}:{start_ticks}"
            return f"proc-start:{start_ticks}"
    except Exception:
        return None
    return None


def exit_kind_and_value(exit_code: int) -> tuple[ExitKind, int]:
    code = int(exit_code)
    if code < 0:
        return ExitKind.SIGNAL, -code
    return ExitKind.CODE, code


def worker_start_contract_configured() -> bool:
    """Return whether this process received any supervised-start contract."""

    return any(
        os.environ.get(name, "").strip()
        for name in (
            _EVENT_PATH_ENV,
            _ATTEMPT_ENV,
            _NONCE_ENV,
            _SESSION_ENV,
        )
    )


def _worker_contract() -> Optional[dict[str, Any]]:
    values = {
        "path": os.environ.get(_EVENT_PATH_ENV, "").strip(),
        "attempt": os.environ.get(_ATTEMPT_ENV, "").strip(),
        "nonce": os.environ.get(_NONCE_ENV, "").strip(),
        "task_id": os.environ.get(_TASK_ENV, "").strip(),
        "run_id": os.environ.get(_RUN_ENV, "").strip(),
        "session_id": os.environ.get(_SESSION_ENV, "").strip(),
        "worktree": os.environ.get(_WORKTREE_ENV, "").strip(),
    }
    if not all(values.values()):
        return None
    try:
        run_id = int(values["run_id"])
        attempt = int(values["attempt"])
    except (TypeError, ValueError):
        return None
    if run_id <= 0 or attempt <= 0:
        return None
    try:
        worktree = str(Path(values["worktree"]).resolve())
    except OSError:
        return None
    return {
        "path": Path(values["path"]),
        "attempt": attempt,
        "nonce": values["nonce"],
        "task_id": values["task_id"],
        "run_id": run_id,
        "session_id": values["session_id"],
        "worktree": worktree,
    }


def _append_jsonl_record(target: Path, event: Mapping[str, Any]) -> None:
    """Append one complete JSONL record without replacing prior evidence."""

    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(target, flags, 0o600)
    try:
        written = os.write(descriptor, encoded)
        if written != len(encoded):
            raise OSError("short lifecycle JSONL append")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def emit_start_identity_event(*, session_id: Optional[str] = None) -> bool:
    """Emit the one child-start identity after exact resume preload."""

    contract = _worker_contract()
    if contract is None:
        return False
    actual_session = str(session_id or "").strip()
    if actual_session != contract["session_id"]:
        return False
    try:
        actual_worktree = str(Path.cwd().resolve())
    except OSError:
        return False
    if actual_worktree != contract["worktree"]:
        return False
    root_pid = os.getpid()
    birth_token = process_birth_token(root_pid)
    if birth_token is None:
        return False

    target = contract["path"]
    if target.exists():
        try:
            for line in target.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                if isinstance(record, dict) and record.get("kind") == "identity":
                    return False
        except (OSError, TypeError, ValueError):
            return False
    _append_jsonl_record(
        target,
        {
            "schema_version": SCHEMA_VERSION,
            "kind": LifecycleEventType.IDENTITY.value,
            "nonce": contract["nonce"],
            "task_id": contract["task_id"],
            "run_id": contract["run_id"],
            "attempt": contract["attempt"],
            "worktree": actual_worktree,
            "observed_session_id": actual_session,
            "root_pid": root_pid,
            "process_birth_token": birth_token,
        },
    )
    return True


def build_terminal_event(
    result: Any,
    *,
    session_id: Optional[str] = None,
    exit_code: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Build identity-bound terminal evidence when a worker contract is present."""
    contract = _worker_contract()
    if contract is None:
        return None
    actual_session = str(session_id or "").strip()
    if not actual_session:
        return None
    try:
        actual_worktree = str(Path.cwd().resolve())
    except OSError:
        return None
    if actual_worktree != contract["worktree"]:
        return None

    failed = bool(isinstance(result, Mapping) and result.get("failed"))
    raw_reason = str(result.get("failure_reason") or "").strip() if failed else ""
    if not failed:
        classification = TerminalClassification.SUCCESS
        failure_reason = FailureReason.NONE
    elif raw_reason == FailureReason.TRANSIENT_PROVIDER.value:
        classification = TerminalClassification.TRANSIENT_PROVIDER
        failure_reason = FailureReason.TRANSIENT_PROVIDER
    elif raw_reason == FailureReason.RATE_LIMIT.value:
        classification = TerminalClassification.RATE_LIMITED
        failure_reason = FailureReason.RATE_LIMIT
    elif raw_reason == FailureReason.BILLING.value:
        classification = TerminalClassification.BILLING
        failure_reason = FailureReason.BILLING
    else:
        classification = TerminalClassification.FAILED
        failure_reason = FailureReason.WORKER_FAILURE
    actual_exit = int(exit_code if exit_code is not None else (1 if failed else 0))
    exit_kind, exit_value = exit_kind_and_value(actual_exit)
    root_pid = os.getpid()
    birth_token = process_birth_token(root_pid)
    if birth_token is None:
        return None
    event = {
        "schema_version": SCHEMA_VERSION,
        "kind": LifecycleEventType.TERMINAL.value,
        "nonce": contract["nonce"],
        "task_id": contract["task_id"],
        "run_id": contract["run_id"],
        "attempt": contract["attempt"],
        "expected_session_id": contract["session_id"],
        "observed_session_id": actual_session,
        "worktree": actual_worktree,
        "root_pid": root_pid,
        "process_birth_token": birth_token,
        "exit_kind": exit_kind.value,
        "exit_value": exit_value,
        "failure_reason": failure_reason.value,
        "classification": classification.value,
    }
    profile = os.environ.get(_PROFILE_ENV, "").strip()
    provider = os.environ.get(_PROVIDER_ENV, "").strip()
    raw_generation = os.environ.get(_CREDENTIAL_GENERATION_ENV, "").strip()
    if profile and provider and raw_generation.isdecimal():
        generation = int(raw_generation)
        if generation > 0:
            event.update(
                profile=profile,
                provider=provider,
                credential_generation=generation,
            )
    return event


def emit_terminal_event(
    result: Any,
    *,
    session_id: Optional[str] = None,
    exit_code: Optional[int] = None,
) -> bool:
    """Safely append terminal evidence after the child-start identity."""
    event = build_terminal_event(
        result, session_id=session_id, exit_code=exit_code
    )
    if event is None:
        return False
    target = Path(os.environ[_EVENT_PATH_ENV])
    _append_jsonl_record(target, event)
    return True
