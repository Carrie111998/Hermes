"""Tests for the trajectory quality decision store.

Uses real temp HERMES_HOME so the profile-aware path resolution is
exercised — no mocks for sqlite3.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from agent.trajectory_quality import (
    TrajectoryQualityDecision,
    TrajectoryQualityConfig,
)
from agent.trajectory_quality_store import TrajectoryQualityStore


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _sample_decision(**overrides) -> TrajectoryQualityDecision:
    defaults = dict(
        action="recommend_stronger_model",
        reason_code="two_identical_failures",
        level_before="continue",
        level_after="recommend_stronger_model",
        tool_name="terminal",
        args_hash="deadbeef",
        result_hash="cafebabe",
        count=2,
        explain="terminal failed 2x with identical args_hash",
        model="test/model",
        provider="test",
    )
    defaults.update(overrides)
    return TrajectoryQualityDecision(**defaults)


def test_record_and_list_roundtrip(tmp_home):
    store = TrajectoryQualityStore()
    decision = _sample_decision()
    decision_id = store.record(decision, session_id="sess-1")
    assert isinstance(decision_id, str) and len(decision_id) > 0

    rows = store.list_for_session("sess-1")
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == "recommend_stronger_model"
    assert row["reason_code"] == "two_identical_failures"
    assert row["tool_name"] == "terminal"
    assert row["count"] == 2


def test_record_creates_db_under_hermes_home(tmp_home):
    store = TrajectoryQualityStore()
    store.record(_sample_decision(), session_id="sess-1")
    db_path = tmp_home / "trajectory_quality.db"
    assert db_path.exists()


def test_no_raw_secret_in_stored_row(tmp_home):
    """A planted secret in the explain string must survive redaction."""
    store = TrajectoryQualityStore()
    secret = "sk-proj-abcdef1234567890abcdef1234567890"
    decision = _sample_decision(explain=f"terminal failed with key {secret}")
    store.record(decision, session_id="sess-1")

    rows = store.list_for_session("sess-1")
    row_json = str(rows[0])
    assert secret not in row_json


def test_list_filters_by_session(tmp_home):
    store = TrajectoryQualityStore()
    store.record(_sample_decision(), session_id="sess-A")
    store.record(_sample_decision(), session_id="sess-B")
    assert len(store.list_for_session("sess-A")) == 1
    assert len(store.list_for_session("sess-B")) == 1
    assert len(store.list_for_session("sess-C")) == 0


def test_retention_purge_removes_old_rows(tmp_home):
    store = TrajectoryQualityStore(retention_days=1)
    store.record(_sample_decision(), session_id="sess-old")
    # Manually backdate the row.
    import sqlite3

    db_path = tmp_home / "trajectory_quality.db"
    conn = sqlite3.connect(db_path)
    old_ts = "2020-01-01T00:00:00+00:00"
    conn.execute("UPDATE decisions SET created_at = ?", (old_ts,))
    conn.commit()
    conn.close()

    purged = store.purge_expired()
    assert purged >= 1
    assert len(store.list_for_session("sess-old")) == 0


def test_session_cap_trims_oldest(tmp_home):
    store = TrajectoryQualityStore(max_decisions_per_session=3)
    for i in range(5):
        store.record(_sample_decision(count=i), session_id="sess-cap")
    rows = store.list_for_session("sess-cap")
    assert len(rows) == 3
    # Should keep the most recent (highest count).
    counts = sorted(r["count"] for r in rows)
    assert counts == [2, 3, 4]
