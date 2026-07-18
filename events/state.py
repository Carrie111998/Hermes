"""Atomic JSON state helpers for event-bus subscribers.

Writes use tmp-file + rename to avoid partial writes that would corrupt
state on crash.  Reads return the provided default when the file is
missing or malformed (self-healing behaviour for operator comfort).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Bounded retry for the atomic replace in save_state(). On Windows a concurrent
# lock-free reader makes os.replace() fail transiently with PermissionError
# WinError 5, because CPython opens files without FILE_SHARE_DELETE; total
# worst-case wait is 20+40+60+80 = 200ms across 5 attempts. Mirrors the same
# fix in pipeline_state.manager._write_atomic (see that module for full detail).
# POSIX rename swaps inodes and never hits this, so the retry is a no-op there.
_REPLACE_MAX_ATTEMPTS = 5
_REPLACE_BACKOFF_SECONDS = 0.02


def load_state(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return dict(default)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return dict(default)
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("load_state(%s) falling back to default: %s", path, e)
        return dict(default)


def save_state(path: Path, data: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    # Bounded retry absorbs the transient Windows WinError 5 reader/writer race
    # on the replace (see _REPLACE_MAX_ATTEMPTS note above).
    for attempt in range(_REPLACE_MAX_ATTEMPTS):
        try:
            os.replace(tmp_path, path)
            return
        except PermissionError:
            if attempt == _REPLACE_MAX_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_BACKOFF_SECONDS * (attempt + 1))
