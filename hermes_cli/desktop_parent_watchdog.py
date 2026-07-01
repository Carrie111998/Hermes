"""Desktop-owned dashboard backend parent watchdog.

The Electron app spawns ``hermes dashboard --port 0`` as a local backend. If
Electron crashes or is force-quit, Python is re-parented to the OS service
manager and keeps serving a stale, token-protected dashboard port. That orphan can
burn CPU and leave the next desktop launch talking to a dead/stale backend.

This module is intentionally tiny and stdlib-only so it can start early in the
``dashboard`` command without dragging in the web server.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from typing import Optional

_LOG = logging.getLogger(__name__)
_ORPHAN_PARENT_PIDS = {0, 1}
_TRUE_VALUES = {"1", "true", "yes", "on"}


def _parse_parent_pid(raw: object) -> Optional[int]:
    try:
        pid = int(str(raw or "").strip())
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _should_exit_for_parent(
    parent_pid: int,
    *,
    getppid: Callable[[], int] = os.getppid,
    pid_exists: Callable[[int], bool] = _pid_exists,
) -> bool:
    """Return True when the desktop parent is gone.

    Prefer the direct parent relationship: while Electron owns this backend,
    ``os.getppid()`` equals the PID Electron passed in. If the process is
    re-parented to launchd/init (0/1), the desktop is gone even if the old PID
    has already been recycled. For unusual launch wrappers where the immediate
    parent differs but the Electron PID still exists, stay alive.
    """

    current_parent = getppid()
    if current_parent == parent_pid:
        return False
    if current_parent in _ORPHAN_PARENT_PIDS:
        return True
    return not pid_exists(parent_pid)


def start_desktop_parent_watchdog(
    env: Mapping[str, str] | None = None,
    *,
    interval_s: float = 5.0,
    exit_fn: Callable[[int], None] = os._exit,
) -> threading.Thread | None:
    """Start a daemon thread that exits when the Electron parent disappears.

    Only enabled for desktop-spawned dashboard backends that set both
    ``HERMES_DESKTOP=1`` and ``HERMES_DESKTOP_PARENT_PID``. Returns the thread in
    tests/diagnostics and ``None`` when disabled or misconfigured.
    """

    env = env or os.environ
    if str(env.get("HERMES_DESKTOP", "")).strip().lower() not in _TRUE_VALUES:
        return None

    parent_pid = _parse_parent_pid(env.get("HERMES_DESKTOP_PARENT_PID"))
    if parent_pid is None:
        return None

    def _watch() -> None:
        while True:
            time.sleep(max(0.25, interval_s))
            if _should_exit_for_parent(parent_pid):
                _LOG.warning(
                    "Desktop parent PID %s disappeared; exiting orphaned dashboard backend",
                    parent_pid,
                )
                exit_fn(0)
                return

    thread = threading.Thread(
        target=_watch,
        name="desktop-parent-watchdog",
        daemon=True,
    )
    thread.start()
    return thread
