"""Trajectory saving utilities and static helpers.

_convert_to_trajectory_format stays as an AIAgent method (batch_runner.py
calls agent._convert_to_trajectory_format). Only the static helpers and
the file-write logic live here.
"""

import contextlib
import gzip
import json
import logging
import os
import threading
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


@contextlib.contextmanager
def _trajectory_append_lock(filename):
    """Serialize trajectory appends within and across processes."""
    lock_key = os.path.abspath(os.fspath(filename))
    with _trajectory_lock_guard:
        thread_lock = _trajectory_locks.setdefault(lock_key, threading.Lock())

    with thread_lock:
        lock_path = f"{lock_key}.lock"
        with open(lock_path, "a+b") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            elif msvcrt is not None:
                lock_file.seek(0)
                lock_file.write(b"\0")
                lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


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
        with _trajectory_append_lock(filename):
            opener = gzip.open if str(filename).endswith(".gz") else open
            with opener(filename, "at", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info("Trajectory saved to %s", filename)
    except Exception as e:
        logger.warning("Failed to save trajectory: %s", e)
