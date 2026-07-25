from __future__ import annotations

import asyncio
import ctypes
import errno
import os
import re
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


@dataclass(frozen=True)
class DesktopParentContract:
    pid: int
    nonce: str


class _Server(Protocol):
    should_exit: bool


def parse_desktop_parent_contract(
    env: Mapping[str, str] | None = None,
    *,
    current_pid: int | None = None,
) -> DesktopParentContract | None:
    """Parse the optional Desktop parent-liveness contract.

    Older Desktop releases set only ``HERMES_DESKTOP=1``.  Absence of both
    contract fields therefore preserves compatibility; a partially supplied or
    malformed new contract fails closed instead of silently disabling the
    monitor.
    """
    values = os.environ if env is None else env
    if values.get("HERMES_DESKTOP") != "1":
        return None
    raw_pid = values.get("HERMES_DESKTOP_PARENT_PID", "")
    nonce = values.get("HERMES_DESKTOP_PARENT_NONCE", "")
    if not raw_pid and not nonce:
        return None
    if not raw_pid or not nonce:
        raise ValueError("incomplete Desktop parent-liveness contract")
    try:
        pid = int(raw_pid)
    except ValueError as exc:
        raise ValueError("invalid Desktop parent PID") from exc
    own_pid = os.getpid() if current_pid is None else current_pid
    if pid <= 1 or pid == own_pid:
        raise ValueError("invalid Desktop parent PID")
    if not _NONCE_RE.fullmatch(nonce):
        raise ValueError("invalid Desktop parent nonce")
    return DesktopParentContract(pid=pid, nonce=nonce)


def is_process_alive(pid: int) -> bool:
    """Return whether *pid* still identifies a live process without signalling it."""
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


async def monitor_desktop_parent(
    server: _Server,
    contract: DesktopParentContract,
    *,
    check_alive: Callable[[int], bool] = is_process_alive,
    interval_seconds: float = 3.0,
    missed_checks: int = 2,
) -> None:
    """Request graceful server shutdown after consecutive parent-liveness misses."""
    required_misses = max(1, int(missed_checks))
    misses = 0
    while not server.should_exit:
        try:
            alive = check_alive(contract.pid)
        except Exception:
            alive = False
        if alive:
            misses = 0
        else:
            misses += 1
            if misses >= required_misses:
                server.should_exit = True
                return
        await asyncio.sleep(max(0.0, float(interval_seconds)))
