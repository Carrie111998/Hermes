"""Atomic JSON state helpers for event-bus subscribers.

Writes use tmp-file + rename to avoid partial writes that would corrupt
state on crash.  Reads return the provided default when the file is
missing or malformed (self-healing behaviour for operator comfort).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)


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
    os.replace(tmp_path, path)
