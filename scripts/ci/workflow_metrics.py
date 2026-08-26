#!/usr/bin/env python3
"""Append validated workflow mission metrics to a JSONL file."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

_ROUTES = {"direct", "standard", "complete"}
_OUTCOMES = {"passed", "failed", "blocked", "rolled_back"}
_REQUIRED = {
    "mission_id",
    "route",
    "changed_files",
    "changed_lines",
    "sensitive",
    "required_readers",
    "replays",
    "duration_seconds",
    "outcome",
}


def validate_record(record: dict[str, Any]) -> list[str]:
    errors = sorted(_REQUIRED - record.keys())
    if record.get("route") not in _ROUTES:
        errors.append("route")
    if record.get("outcome") not in _OUTCOMES:
        errors.append("outcome")
    if not isinstance(record.get("mission_id"), str) or not record.get("mission_id", "").strip():
        errors.append("mission_id")
    for key in ("changed_files", "changed_lines", "replays"):
        value = record.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.append(key)
    duration = record.get("duration_seconds")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration < 0:
        errors.append("duration_seconds")
    if not isinstance(record.get("sensitive"), bool):
        errors.append("sensitive")
    readers = record.get("required_readers")
    if not isinstance(readers, list) or any(not isinstance(item, str) or not item for item in readers):
        errors.append("required_readers")
    return sorted(set(errors))


def append_record(path: Path, record: dict[str, Any]) -> None:
    errors = validate_record(record)
    if errors:
        raise ValueError(f"invalid workflow metric fields: {', '.join(errors)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", help="JSON record file")
    parser.add_argument("--output", required=True, help="append-only JSONL destination")
    args = parser.parse_args()
    record = json.loads(Path(args.record).read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise ValueError("record must be a JSON object")
    append_record(Path(args.output), record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
