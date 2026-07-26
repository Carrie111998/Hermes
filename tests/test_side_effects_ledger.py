"""CS-02b side-effect idempotency-ledger acceptance tests."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_cli.side_effects import api, config, schema


@pytest.fixture
def side_effects_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "kanban.db"
    schema.migrate(db_path)
    return db_path


def _reserve(
    db_path: Path,
    **overrides,
) -> api.ReserveResult:
    values = {
        "task_id": "task-1",
        "lane": "platform",
        "action_type": "test.action",
        "payload": {"message": "hello"},
        "db_path": db_path,
    }
    values.update(overrides)
    return api.reserve(**values)


def _row(db_path: Path, row_id: int) -> dict:
    row = api.get_row(row_id, db_path=db_path)
    assert row is not None
    return row


def _at(value: datetime):
    return lambda: value


def _seed_gc_rows(db_path: Path) -> None:
    old = (api._now() - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = (api._now() - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with sqlite3.connect(db_path) as conn:
        for number in range(5):
            conn.execute(
                """
                INSERT INTO side_effects (
                    ts, updated_at, task_id, lane, action_type, payload_hash,
                    idempotency_key, status, attempt_number, vendor
                ) VALUES (?, ?, ?, 'platform', 'test.action', ?, ?, 'done', 1, 'test')
                """,
                (old, old, f"old-done-{number}", f"hash-{number}", f"key-{number}"),
            )
        for number in range(3):
            conn.execute(
                """
                INSERT INTO side_effects (
                    ts, updated_at, task_id, lane, action_type, payload_hash,
                    idempotency_key, status, attempt_number, vendor
                ) VALUES (?, ?, ?, 'platform', 'test.action', ?, ?, 'done', 1, 'test')
                """,
                (
                    recent,
                    recent,
                    f"recent-done-{number}",
                    f"recent-hash-{number}",
                    f"recent-key-{number}",
                ),
            )
        for number in range(2):
            conn.execute(
                """
                INSERT INTO side_effects (
                    ts, updated_at, task_id, lane, action_type, payload_hash,
                    idempotency_key, status, attempt_number, vendor
                ) VALUES (?, ?, ?, 'platform', 'test.action', ?, ?, 'in_flight', 1, 'test')
                """,
                (
                    old,
                    old,
                    f"old-flight-{number}",
                    f"flight-hash-{number}",
                    f"flight-key-{number}",
                ),
            )


def test_migration_creates_side_effects_table(side_effects_db: Path):
    with sqlite3.connect(side_effects_db) as conn:
        table = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='side_effects'"
        ).fetchone()
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='side_effects'"
            )
        }
    assert table == ("side_effects",)
    assert {
        "idx_side_effects_lookup",
        "idx_side_effects_task",
        "idx_side_effects_ts",
        "idx_side_effects_stale",
    }.issubset(indexes)


def test_unknown_action_type_rejected(side_effects_db: Path):
    with pytest.raises(ValueError, match="unknown action_type"):
        _reserve(side_effects_db, action_type="bogus")


def test_policy_stale_seconds_present_for_all_action_types():
    assert config.ACTION_POLICIES
    assert all(
        policy.stale_seconds is not None and policy.stale_seconds > 0
        for policy in config.ACTION_POLICIES.values()
    )


def test_reserve_returns_new_id_when_no_prior_row(side_effects_db: Path):
    result = _reserve(side_effects_db)
    assert result.reserved_id is not None
    assert result.already_done is None
    assert result.already_in_flight is None


def test_confirm_flips_status_to_done_with_external_ref(side_effects_db: Path):
    row_id = _reserve(side_effects_db).reserved_id
    assert row_id is not None
    api.confirm(
        reserved_id=row_id,
        external_ref="vendor-123",
        result_summary="sent",
        db_path=side_effects_db,
    )
    row = _row(side_effects_db, row_id)
    assert row["status"] == "done"
    assert row["external_ref"] == "vendor-123"
    assert row["result_summary"] == "sent"


def test_fail_flips_status_to_failed_and_records_error(side_effects_db: Path):
    row_id = _reserve(side_effects_db).reserved_id
    assert row_id is not None
    api.fail(
        reserved_id=row_id,
        error_class="vendor_timeout",
        error_message="timed out",
        db_path=side_effects_db,
    )
    row = _row(side_effects_db, row_id)
    assert row["status"] == "failed"
    assert row["error_class"] == "vendor_timeout"
    assert row["error_message"] == "timed out"


def test_mark_in_flight_flips_from_pending_to_in_flight(
    side_effects_db: Path,
):
    row_id = _reserve(side_effects_db).reserved_id
    assert row_id is not None
    api.mark_in_flight(reserved_id=row_id, db_path=side_effects_db)
    assert _row(side_effects_db, row_id)["status"] == "in_flight"


def test_second_reserve_with_same_key_returns_already_done_when_prior_done(
    side_effects_db: Path,
):
    first = _reserve(side_effects_db)
    assert first.reserved_id is not None
    api.confirm(
        reserved_id=first.reserved_id,
        external_ref="vendor-1",
        db_path=side_effects_db,
    )
    second = _reserve(side_effects_db)
    assert second.reserved_id is None
    assert second.already_done is not None
    assert second.already_done["id"] == first.reserved_id


def test_second_reserve_with_same_key_returns_already_in_flight_when_prior_in_flight(
    side_effects_db: Path,
):
    first = _reserve(side_effects_db)
    assert first.reserved_id is not None
    api.mark_in_flight(reserved_id=first.reserved_id, db_path=side_effects_db)
    second = _reserve(side_effects_db)
    assert second.reserved_id is None
    assert second.already_in_flight is not None
    assert second.already_in_flight["id"] == first.reserved_id


def test_second_reserve_after_stale_window_creates_new_attempt(
    side_effects_db: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    start = datetime(2026, 7, 25, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(api, "_now", _at(start))
    first_id = _reserve(side_effects_db).reserved_id
    assert first_id is not None
    api.mark_in_flight(reserved_id=first_id, db_path=side_effects_db)

    monkeypatch.setattr(api, "_now", _at(start + timedelta(seconds=61)))
    second_id = _reserve(side_effects_db).reserved_id
    assert second_id is not None
    assert second_id != first_id
    assert _row(side_effects_db, first_id)["status"] == "stale"
    assert _row(side_effects_db, second_id)["attempt_number"] == 2


def test_telegram_send_hour_bucket_dedup(
    side_effects_db: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    first_hour = datetime(2026, 7, 25, 10, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(api, "_now", _at(first_hour))
    first = _reserve(
        side_effects_db,
        action_type="telegram.send",
        payload={"alert": "cap exceeded"},
    )
    assert first.reserved_id is not None
    api.confirm(
        reserved_id=first.reserved_id,
        external_ref=None,
        db_path=side_effects_db,
    )
    duplicate = _reserve(
        side_effects_db,
        action_type="telegram.send",
        payload={"alert": "cap exceeded"},
    )
    assert duplicate.already_done is not None

    monkeypatch.setattr(api, "_now", _at(first_hour + timedelta(hours=1)))
    next_hour = _reserve(
        side_effects_db,
        action_type="telegram.send",
        payload={"alert": "cap exceeded"},
    )
    assert next_hour.reserved_id is not None
    assert next_hour.reserved_id != first.reserved_id


def test_allow_duplicate_creates_second_done_row(side_effects_db: Path):
    ids: list[int] = []
    for _ in range(2):
        result = _reserve(side_effects_db, allow_duplicate=True)
        assert result.reserved_id is not None
        ids.append(result.reserved_id)
        api.confirm(
            reserved_id=result.reserved_id,
            external_ref=f"vendor-{result.reserved_id}",
            db_path=side_effects_db,
        )
    rows = api.list_rows(status="done", db_path=side_effects_db)
    assert len(rows) == 2
    assert ids[0] != ids[1]
    assert rows[0]["idempotency_key"] != rows[1]["idempotency_key"]


def test_per_action_type_stale_windows_respected(
    side_effects_db: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    start = datetime(2026, 7, 25, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(api, "_now", _at(start))
    calendar = _reserve(
        side_effects_db,
        task_id="calendar-task",
        action_type="calendar.create",
        payload={"event": "cleaning"},
    )
    gbp = _reserve(
        side_effects_db,
        task_id="gbp-task",
        action_type="gbp.post",
        payload={"post": "open today"},
    )
    assert calendar.reserved_id is not None
    assert gbp.reserved_id is not None
    api.mark_in_flight(
        reserved_id=calendar.reserved_id,
        db_path=side_effects_db,
    )
    api.mark_in_flight(reserved_id=gbp.reserved_id, db_path=side_effects_db)

    monkeypatch.setattr(api, "_now", _at(start + timedelta(seconds=400)))
    calendar_retry = _reserve(
        side_effects_db,
        task_id="calendar-task",
        action_type="calendar.create",
        payload={"event": "cleaning"},
    )
    gbp_retry = _reserve(
        side_effects_db,
        task_id="gbp-task",
        action_type="gbp.post",
        payload={"post": "open today"},
    )
    assert calendar_retry.reserved_id is not None
    assert _row(side_effects_db, calendar.reserved_id)["status"] == "stale"
    assert gbp_retry.already_in_flight is not None
    assert _row(side_effects_db, gbp.reserved_id)["status"] == "in_flight"


def test_reconcile_external_ref_verifiable_no_verify_fn_returns_unknown(
    side_effects_db: Path,
):
    result = _reserve(side_effects_db, action_type="sms.send")
    assert result.reserved_id is not None
    api.mark_in_flight(
        reserved_id=result.reserved_id,
        external_ref="sms-1",
        db_path=side_effects_db,
    )
    before = _row(side_effects_db, result.reserved_id)
    outcome = api.reconcile_external_ref(
        row=before,
        db_path=side_effects_db,
    )
    assert outcome == "unknown"
    assert _row(side_effects_db, result.reserved_id) == before


def test_reconcile_external_ref_calls_verify_and_confirms(
    side_effects_db: Path,
):
    result = _reserve(side_effects_db, action_type="sms.send")
    assert result.reserved_id is not None
    api.mark_in_flight(
        reserved_id=result.reserved_id,
        external_ref="sms-2",
        db_path=side_effects_db,
    )
    seen: list[str] = []

    def verify(external_ref: str) -> str:
        seen.append(external_ref)
        return "done"

    outcome = api.reconcile_external_ref(
        row=_row(side_effects_db, result.reserved_id),
        verify_fn=verify,
        db_path=side_effects_db,
    )
    assert outcome == "done"
    assert seen == ["sms-2"]
    assert _row(side_effects_db, result.reserved_id)["status"] == "done"


def test_gc_deletes_terminal_rows_older_than_cutoff(side_effects_db: Path):
    _seed_gc_rows(side_effects_db)
    result = api.gc(older_than_days=90, db_path=side_effects_db)
    assert result == {"would_delete": 0, "deleted": 5}
    rows = api.list_rows(limit=20, db_path=side_effects_db)
    assert len(rows) == 5
    assert sum(row["status"] == "in_flight" for row in rows) == 2


def test_gc_dry_run_deletes_nothing(side_effects_db: Path):
    _seed_gc_rows(side_effects_db)
    result = api.gc(
        older_than_days=90,
        dry_run=True,
        db_path=side_effects_db,
    )
    assert result == {"would_delete": 5, "deleted": 0}
    assert len(api.list_rows(limit=20, db_path=side_effects_db)) == 10


def test_cli_list_and_show_and_mark_abandoned(
    tmp_path: Path,
):
    hermes_home = tmp_path / "hermes-home"
    db_path = hermes_home / "kanban.db"
    result = _reserve(db_path, task_id="cli-task")
    assert result.reserved_id is not None
    executable = Path(__file__).parents[1] / ".venv" / "bin" / "hermes"
    env = {**os.environ, "HERMES_HOME": str(hermes_home)}

    listed = subprocess.run(
        [str(executable), "side-effects", "list"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert "cli-task" in listed.stdout
    assert "test.action" in listed.stdout

    shown = subprocess.run(
        [
            str(executable),
            "side-effects",
            "show",
            str(result.reserved_id),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert json.loads(shown.stdout)["id"] == result.reserved_id

    subprocess.run(
        [
            str(executable),
            "side-effects",
            "mark-abandoned",
            str(result.reserved_id),
            "--reason",
            "foo",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    abandoned = subprocess.run(
        [
            str(executable),
            "side-effects",
            "list",
            "--status",
            "abandoned",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert "abandoned" in abandoned.stdout
    assert "cli-task" in abandoned.stdout
