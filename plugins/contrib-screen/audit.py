from __future__ import annotations

import json
import os
from pathlib import Path

from hermes_constants import get_hermes_home


def default_log_path() -> Path:
    return get_hermes_home() / "contrib-screen" / "log.jsonl"


def log_path(override: str | Path | None = None) -> Path:
    if override:
        return Path(override)
    env = os.environ.get("CONTRIB_SCREEN_LOG")
    if env:
        return Path(env)
    return default_log_path()


def append_record(record: dict, path: str | Path | None = None) -> Path:
    target = log_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")
    return target
