from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from hermes_cli.lanes import rate_limit, schema
from hermes_cli.lanes.errors import LaneRateLimitExceeded


def _now() -> datetime:
    return datetime(2026, 1, 1, 12, 30, tzinfo=timezone.utc)


def test_hourly_ingest_cap_enforced_across_calls(tmp_path):
    db = tmp_path / "kanban.db"
    assert rate_limit.check_and_increment(
        lane_id="dayroute",
        window_kind="hourly_ingest",
        increment=1,
        cap=2,
        db_path=db,
        now=_now(),
    )
    assert rate_limit.check_and_increment(
        lane_id="dayroute",
        window_kind="hourly_ingest",
        increment=1,
        cap=2,
        db_path=db,
        now=_now(),
    )
    assert not rate_limit.check_and_increment(
        lane_id="dayroute",
        window_kind="hourly_ingest",
        increment=1,
        cap=2,
        db_path=db,
        now=_now(),
    )


def test_daily_task_cap_enforced_across_calls(tmp_path):
    db = tmp_path / "kanban.db"
    outcomes = [
        rate_limit.check_and_increment(
            lane_id="dayroute",
            window_kind="daily_task",
            increment=1,
            cap=3,
            db_path=db,
            now=_now(),
        )
        for _ in range(4)
    ]
    assert outcomes == [True, True, True, False]


def test_daily_cost_cap_enforced_by_aud_total(tmp_path):
    db = tmp_path / "kanban.db"
    assert rate_limit.check_and_increment(
        lane_id="tihna",
        window_kind="daily_cost",
        increment=1.25,
        cap=2.0,
        db_path=db,
        now=_now(),
    )
    assert not rate_limit.check_and_increment(
        lane_id="tihna",
        window_kind="daily_cost",
        increment=0.76,
        cap=2.0,
        db_path=db,
        now=_now(),
    )
    assert rate_limit.read_bucket(
        lane_id="tihna",
        window_kind="daily_cost",
        db_path=db,
        now=_now(),
    ) == (0, 1.25)


def test_daily_cost_advisory_records_overspend_without_blocking(tmp_path):
    db = tmp_path / "kanban.db"
    assert rate_limit.record_cost_advisory(
        lane_id="tihna",
        increment=1.25,
        cap=2.0,
        db_path=db,
        now=_now(),
    )
    assert not rate_limit.record_cost_advisory(
        lane_id="tihna",
        increment=0.76,
        cap=2.0,
        db_path=db,
        now=_now(),
    )
    assert rate_limit.read_bucket(
        lane_id="tihna",
        window_kind="daily_cost",
        db_path=db,
        now=_now(),
    ) == (0, pytest.approx(2.01))


def test_rate_limit_windows_roll_over_at_UTC_boundary(tmp_path):
    db = tmp_path / "kanban.db"
    before = datetime(2026, 1, 1, 23, 59, 59, tzinfo=timezone.utc)
    after = before + timedelta(seconds=1)
    assert rate_limit.check_and_increment(
        lane_id="dayroute",
        window_kind="daily_task",
        increment=1,
        cap=1,
        db_path=db,
        now=before,
    )
    assert rate_limit.check_and_increment(
        lane_id="dayroute",
        window_kind="daily_task",
        increment=1,
        cap=1,
        db_path=db,
        now=after,
    )
    conn = schema.connect(db)
    assert conn.execute(
        "SELECT COUNT(*) FROM lane_rate_limit_state"
    ).fetchone()[0] == 2
    conn.close()


def test_rate_limit_state_atomic_under_concurrency(tmp_path):
    db = tmp_path / "kanban.db"
    schema.ensure_migrated(db)

    def attempt(_index):
        return rate_limit.check_and_increment(
            lane_id="dayroute",
            window_kind="hourly_ingest",
            increment=1,
            cap=30,
            db_path=db,
            now=_now(),
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(attempt, range(60)))
    assert sum(outcomes) == 30
    assert rate_limit.read_bucket(
        lane_id="dayroute",
        window_kind="hourly_ingest",
        db_path=db,
        now=_now(),
    ) == (30, 0.0)


def test_rate_limit_state_per_lane_isolation(tmp_path):
    db = tmp_path / "kanban.db"
    for lane in ("dayroute", "tihna"):
        assert rate_limit.check_and_increment(
            lane_id=lane,
            window_kind="daily_task",
            increment=1,
            cap=1,
            db_path=db,
            now=_now(),
        )
    assert not rate_limit.check_and_increment(
        lane_id="dayroute",
        window_kind="daily_task",
        increment=1,
        cap=1,
        db_path=db,
        now=_now(),
    )


def test_LaneRateLimitExceeded_raised_when_exceeded(tmp_path):
    db = tmp_path / "kanban.db"
    rate_limit.enforce(
        lane_id="green_captains",
        window_kind="hourly_ingest",
        increment=1,
        cap=1,
        db_path=db,
        now=_now(),
    )
    with pytest.raises(LaneRateLimitExceeded):
        rate_limit.enforce(
            lane_id="green_captains",
            window_kind="hourly_ingest",
            increment=1,
            cap=1,
            db_path=db,
            now=_now(),
        )


def test_rate_limit_bucket_read_no_write(tmp_path):
    db = tmp_path / "kanban.db"
    schema.ensure_migrated(db)
    before = db.read_bytes()
    assert rate_limit.read_bucket(
        lane_id="dayroute",
        window_kind="daily_task",
        db_path=db,
        now=_now(),
    ) == (0, 0.0)
    conn = schema.connect(db)
    assert conn.execute(
        "SELECT COUNT(*) FROM lane_rate_limit_state"
    ).fetchone()[0] == 0
    conn.close()
    assert db.read_bytes() == before


def test_rate_limit_cap_zero_behavior(tmp_path):
    db = tmp_path / "kanban.db"
    assert not rate_limit.check_and_increment(
        lane_id="dayroute",
        window_kind="daily_task",
        increment=1,
        cap=0,
        db_path=db,
        now=_now(),
    )
    assert rate_limit.read_bucket(
        lane_id="dayroute",
        window_kind="daily_task",
        db_path=db,
        now=_now(),
    ) == (0, 0.0)


def test_rate_limit_reset_via_manifest_update_creates_new_window(tmp_path):
    db = tmp_path / "kanban.db"
    assert rate_limit.check_and_increment(
        lane_id="dayroute",
        window_kind="daily_task",
        increment=1,
        cap=1,
        db_path=db,
        now=_now(),
    )
    policy_applied_next_day = _now() + timedelta(days=1)
    assert rate_limit.check_and_increment(
        lane_id="dayroute",
        window_kind="daily_task",
        increment=1,
        cap=2,
        db_path=db,
        now=policy_applied_next_day,
    )
    conn = schema.connect(db)
    starts = [
        row[0]
        for row in conn.execute(
            """SELECT window_start FROM lane_rate_limit_state
               ORDER BY window_start"""
        )
    ]
    conn.close()
    assert starts == ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"]
