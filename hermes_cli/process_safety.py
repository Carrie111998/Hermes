"""Process safety utilities for PID birth-time verification and safe signaling.

Guards against PID recycling attacks and race conditions where a terminated or
crashed process PID is reassigned to an unrelated system process.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessIdentity:
    """Immutable record of process identity."""
    pid: int
    process_start_time: Optional[int]


def get_process_start_time(pid: int) -> Optional[int]:
    """Return a stable per-process start-time fingerprint, or None.

    On Linux, reads field 22 (/proc/<pid>/stat), clock ticks since boot.
    On macOS/Windows (or environments without /proc), falls back to
    psutil.Process(pid).create_time() quantized to centiseconds (int).
    """
    if not pid or pid <= 0:
        return None

    stat_path = Path(f"/proc/{pid}/stat")
    try:
        # Field 22 in /proc/<pid>/stat is process start time (clock ticks).
        return int(stat_path.read_text(encoding="utf-8").split()[21])
    except (FileNotFoundError, IndexError, PermissionError, ValueError, OSError):
        pass

    try:
        import psutil  # type: ignore
        return int(round(psutil.Process(pid).create_time() * 100))
    except Exception:
        return None


def is_pid_alive(pid: int) -> bool:
    """Check if process is alive without dangerous side effects."""
    if not pid or pid <= 0:
        return False
    try:
        import psutil  # type: ignore
        return psutil.pid_exists(pid)
    except Exception:
        pass

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def classify_process_identity(
    pid: Optional[int],
    expected_start_time: Optional[int],
    *,
    alive: Optional[bool] = None,
    observed_start_time: Optional[int] = None,
) -> str:
    """Classify the identity state of a target PID relative to expected birth time.

    Returns:
      - "not_applicable": PID is invalid (<=0, None) or process is not alive
      - "unavailable": Expected start time is None or OS could not retrieve start time
      - "matched": Live process start time exactly matches expected start time
      - "mismatch": Live process start time differs from expected start time (recycled PID)
    """
    if not pid or pid <= 0:
        return "not_applicable"

    alive_status = is_pid_alive(pid) if alive is None else alive
    if not alive_status:
        return "not_applicable"

    observed = observed_start_time if observed_start_time is not None else get_process_start_time(int(pid))
    if expected_start_time is None or observed is None:
        return "unavailable"

    if int(expected_start_time) == int(observed):
        return "matched"
    return "mismatch"


def safe_terminate_process(
    pid: Optional[int],
    expected_start_time: Optional[int],
    *,
    signal_fn: Optional[Callable[[int, int], None]] = None,
    timeout_seconds: float = 5.0,
    poll_interval: float = 0.5,
) -> dict[str, Any]:
    """Safely terminate a host process with strict PID birth-time validation.

    Validates process identity immediately prior to SIGTERM. If identity matches,
    issues SIGTERM and polls until exit or timeout. If still alive after timeout,
    re-validates process identity immediately prior to SIGKILL.
    Fails closed (refuses to signal) if identity is 'mismatch' or 'unavailable'.
    """
    info: dict[str, Any] = {
        "prev_pid": int(pid) if pid else None,
        "expected_start_time": expected_start_time,
        "observed_start_time": None,
        "process_identity": "not_applicable",
        "termination_attempted": False,
        "terminated": False,
        "sigkill": False,
    }

    if not pid or pid <= 0:
        return info

    alive = is_pid_alive(pid)
    observed = get_process_start_time(int(pid)) if alive else None
    info["observed_start_time"] = observed

    identity = classify_process_identity(
        pid, expected_start_time, alive=alive, observed_start_time=observed
    )
    info["process_identity"] = identity

    # PID-reuse guard: fail closed if identity mismatch or unavailable
    if identity in ("mismatch", "unavailable"):
        logger.warning(
            "safe_terminate_process: refusing to signal PID %s (identity classification: %s)",
            pid, identity
        )
        return info

    if not alive:
        info["terminated"] = True
        return info

    kill = signal_fn if signal_fn is not None else os.kill
    info["termination_attempted"] = True

    try:
        kill(int(pid), signal.SIGTERM)
    except ProcessLookupError:
        info["terminated"] = True
        return info
    except OSError as e:
        logger.error("safe_terminate_process: failed to send SIGTERM to PID %s: %s", pid, e)
        return info

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not is_pid_alive(pid):
            info["terminated"] = True
            return info
        time.sleep(poll_interval)

    # If still alive, re-validate before SIGKILL (recycled PID check)
    if is_pid_alive(pid):
        recheck_start_time = get_process_start_time(int(pid))
        recheck_identity = classify_process_identity(
            pid, expected_start_time, alive=True, observed_start_time=recheck_start_time
        )
        if recheck_identity != "matched":
            logger.warning(
                "safe_terminate_process: refusing SIGKILL on PID %s after timeout (recheck: %s)",
                pid, recheck_identity
            )
            return info

        try:
            sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
            kill(int(pid), sigkill)
            info["sigkill"] = True
        except (ProcessLookupError, OSError) as e:
            logger.error("safe_terminate_process: SIGKILL error on PID %s: %s", pid, e)
            return info

    info["terminated"] = not is_pid_alive(pid)
    return info
