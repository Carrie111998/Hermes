"""Trajectory saving utilities and static helpers.

_convert_to_trajectory_format stays as an AIAgent method (batch_runner.py
calls agent._convert_to_trajectory_format). Only the static helpers and
the file-write logic live here.
"""

import contextlib
import errno
import gzip
import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX
    msvcrt = None


_trajectory_lock_guard = threading.Lock()
_trajectory_locks: dict[str, threading.Lock] = {}
_TRAJECTORY_LOCK_TIMEOUT_SECONDS = 10.0
_TRAJECTORY_LOCK_POLL_SECONDS = 0.05


def _acquire_os_lock(lock_file, deadline: float) -> None:
    while True:
        if fcntl is not None:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                pass
        elif msvcrt is not None:
            try:
                lock_file.seek(0, os.SEEK_END)
                if lock_file.tell() == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
        else:  # pragma: no cover - all supported hosts provide one
            raise RuntimeError("cross-process trajectory locking unavailable")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out waiting for trajectory append lock")
        time.sleep(min(_TRAJECTORY_LOCK_POLL_SECONDS, remaining))


def _release_os_lock(lock_file) -> None:
    if fcntl is not None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def _trajectory_append_lock(filename):
    """Serialize appends with a bounded thread/OS lock acquisition."""
    lock_key = os.path.abspath(os.fspath(filename))
    with _trajectory_lock_guard:
        thread_lock = _trajectory_locks.setdefault(lock_key, threading.Lock())

    deadline = time.monotonic() + _TRAJECTORY_LOCK_TIMEOUT_SECONDS
    if not thread_lock.acquire(timeout=_TRAJECTORY_LOCK_TIMEOUT_SECONDS):
        raise TimeoutError("timed out waiting for in-process trajectory append lock")
    try:
        lock_path = f"{lock_key}.lock"
        with open(lock_path, "a+b") as lock_file:
            _acquire_os_lock(lock_file, deadline)
            try:
                yield
            finally:
                _release_os_lock(lock_file)
    finally:
        thread_lock.release()


def _write_all(stream, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = stream.write(remaining)
        if not written:
            raise OSError("short trajectory append write")
        remaining = remaining[written:]


def _append_payload(filename, payload: bytes) -> None:
    """Durably append one complete member/line or restore the old length."""
    with open(filename, "a+b", buffering=0) as stream:
        original_size = os.fstat(stream.fileno()).st_size
        try:
            _write_all(stream, payload)
            os.fsync(stream.fileno())
        except Exception:
            try:
                os.ftruncate(stream.fileno(), original_size)
                os.fsync(stream.fileno())
            except Exception:
                logger.error(
                    "Failed to roll back partial trajectory append in %s",
                    filename,
                    exc_info=True,
                )
            raise


def convert_scratchpad_to_think(content: str) -> str:
    """Convert <REASONING_SCRATCHPAD> tags to <think> tags."""
    if not content or "<REASONING_SCRATCHPAD>" not in content:
        return content
    return content.replace("<REASONING_SCRATCHPAD>", "<think>").replace("</REASONING_SCRATCHPAD>", "</think>")


def has_incomplete_scratchpad(content: str) -> bool:
    """Check if content has an opening <REASONING_SCRATCHPAD> without a closing tag."""
    if not content:
        return False
    return "<REASONING_SCRATCHPAD>" in content and "</REASONING_SCRATCHPAD>" not in content


def save_trajectory(trajectory: List[Dict[str, Any]], model: str,
                    completed: bool, filename: str = None):
    """Append a trajectory entry to a lossless JSONL file.

    Args:
        trajectory: The ShareGPT-format conversation list.
        model: Model name for metadata.
        completed: Whether the conversation completed successfully.
        filename: Override output filename. Defaults to a gzip-compressed
                  ``.jsonl.gz`` file based on ``completed``. Explicit paths
                  ending in ``.jsonl`` retain the legacy plain-text format.

    Returns:
        ``True`` only after the complete append has been flushed and synced.
        Returns ``False`` after lock, serialization, write, or sync failure.
    """
    if filename is None:
        filename = (
            "trajectory_samples.jsonl.gz"
            if completed
            else "failed_trajectories.jsonl.gz"
        )

    entry = {
        "conversations": trajectory,
        "timestamp": datetime.now().isoformat(),
        "model": model,
        "completed": completed,
    }

    try:
        line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
        payload = gzip.compress(line) if str(filename).endswith(".gz") else line
        with _trajectory_append_lock(filename):
            _append_payload(filename, payload)
        logger.info("Trajectory saved to %s", filename)
        return True
    except Exception as e:
        logger.warning("Failed to save trajectory: %s", e)
        return False
