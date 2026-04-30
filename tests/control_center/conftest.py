"""Shared fixtures for control_center tests.

Each test gets its own temp APPROVAL_LOG + STATE_DB so they don't interfere
with the running production Control Center on this machine. We monkeypatch
the module-level path constants in storage so all functions read/write the
temp paths.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from control_center import storage


@pytest.fixture
def tmp_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect APPROVAL_LOG + STATE_DB to a tmp_path; return helper handle."""
    log = tmp_path / "approval-log.jsonl"
    state_db = tmp_path / "state.db"
    monkeypatch.setattr(storage, "APPROVAL_LOG", log)
    monkeypatch.setattr(storage, "STATE_DB", state_db)

    class Handle:
        def write_log(self, entries: list[dict]) -> None:
            log.write_text(
                "\n".join(json.dumps(e) for e in entries) + "\n",
                encoding="utf-8",
            )

        def record_resolution(self, item_id: str, decision: str) -> None:
            storage.record_resolution("approval", item_id, decision, "")

    return Handle()
