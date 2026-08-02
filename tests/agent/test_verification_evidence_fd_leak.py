"""The retired verification ledger must not open storage at all."""

from __future__ import annotations

import sqlite3

from agent import verification_evidence as ve


def test_retired_operations_open_no_sqlite_connections(monkeypatch, tmp_path) -> None:
    def must_not_connect(*_args, **_kwargs):
        raise AssertionError("retired verification ledger opened sqlite")

    monkeypatch.setattr(sqlite3, "connect", must_not_connect)

    assert ve.record_terminal_result(
        command="python -m pytest tests/test_calc.py",
        cwd=tmp_path,
        session_id="session",
        exit_code=0,
        output="passed",
    ) is None
    assert ve.mark_workspace_edited(
        session_id="session",
        cwd=tmp_path,
        paths=["src/test_calc.py"],
    ) is None
    assert ve.verification_status(session_id="session", cwd=tmp_path) == {
        "status": "not_applicable",
        "evidence": None,
    }
