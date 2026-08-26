"""Tests for append-only workflow mission metrics."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "workflow_metrics.py"
_spec = importlib.util.spec_from_file_location("workflow_metrics", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load workflow_metrics.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
append_record = _mod.append_record
validate_record = _mod.validate_record


def _record():
    return {
        "mission_id": "mission-1",
        "route": "direct",
        "changed_files": 1,
        "changed_lines": 3,
        "sensitive": False,
        "required_readers": ["general_review"],
        "replays": 0,
        "duration_seconds": 12.5,
        "outcome": "passed",
    }


def test_valid_record_appends_jsonl_without_overwriting(tmp_path: Path):
    path = tmp_path / "metrics.jsonl"

    append_record(path, _record())
    second = _record()
    second["mission_id"] = "mission-2"
    append_record(path, second)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["mission_id"] for line in lines] == ["mission-1", "mission-2"]


def test_invalid_record_is_rejected():
    record = _record()
    record["route"] = "invented"
    record["duration_seconds"] = -1

    errors = validate_record(record)

    assert "route" in errors
    assert "duration_seconds" in errors
